#!/usr/bin/env python
"""Repair models whose managed artifact lost its file extension.

The legacy->PostgreSQL migration built the managed filename straight from the legacy
`stored_filename`. Where that name had no extension, the model was promoted to AVAILABLE
with a suffix-less path and its representation format recorded as 'bin'. Ultralytics (and
the ONNX/OpenVINO loaders) dispatch on the file SUFFIX, so the engine raises
"is not a supported model format" on EVERY frame. The engine catches that, so the pipeline
stays green and healthy and publishes nothing at all.

`registry_migration.py` no longer produces this, but models migrated before that fix are
still broken on disk and in the registry. This repairs them in place.

What it changes, per affected model, in ONE transaction:
    file    <id>/<name>            ->  <id>/<name><ext>      (rename inside ARTIFACT_ROOT)
    artifact  relative_path, verified_* fingerprint (rename changes ctime), last_verified_at
    represent format 'bin' -> real format, manifest_sha256 (it covers relative_path)
    model     filename gains the extension so file_extension derives correctly

The bytes are never rewritten: sha256 is re-read after the rename and MUST still match, or
the whole transaction rolls back and the file is renamed back.

    python scripts/repair_model_extension.py            # dry run - shows what it would do
    python scripts/repair_model_extension.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Loadable suffixes per engine family; 'bin' is what the migration wrote when it knew nothing.
KNOWN = {"pt", "onnx", "engine", "xml", "tflite", "pb"}


def _app_env():
    """Inside the container the app env lives on PID 1; docker exec does not inherit it."""
    try:
        for line in open("/proc/1/environ", "rb").read().split(b"\0"):
            if b"=" in line:
                k, v = line.decode("utf-8", "replace").split("=", 1)
                os.environ.setdefault(k, v)
    except OSError:
        pass


def main() -> int:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--apply", action="store_true", help="perform the repair (default: dry run)")
    args = ap_.parse_args()

    _app_env()
    from InferenceNode.auth import db
    db.init_engine()
    from InferenceNode import artifact_paths as ap
    from InferenceNode.artifact_migration import manifest_sha256, sha256_file
    from InferenceNode.artifact_states import fingerprint
    from InferenceNode.auth.db import get_session
    from InferenceNode.data_models import ModelArtifact, ModelRecord, ModelRepresentation
    from datetime import datetime
    from sqlalchemy import select

    planned, repaired, failed = [], 0, 0

    with get_session() as s:
        rows = s.execute(
            select(ModelRecord, ModelRepresentation, ModelArtifact)
            .join(ModelRepresentation, ModelRepresentation.model_id == ModelRecord.id)
            .join(ModelArtifact, ModelArtifact.representation_id == ModelRepresentation.id)
            .where(ModelRepresentation.kind == "primary")
        ).all()
        for m, rep, a in rows:
            cur_ext = os.path.splitext(a.relative_path)[1].lstrip(".").lower()
            if cur_ext in KNOWN:
                continue                                    # already loadable
            ext = (os.path.splitext(m.filename or "")[1]
                   or ("." + rep.format if rep.format in KNOWN else "")
                   or (".pt" if (m.engine_type or "").lower() == "ultralytics" else ""))
            if not ext:
                print(f"  SKIP  {m.model_id}: cannot determine an extension "
                      f"(filename={m.filename!r} format={rep.format!r} engine={m.engine_type!r})")
                continue
            planned.append((m.model_id, a.relative_path, a.relative_path + ext,
                            rep.format, ext.lstrip("."), m.filename))

    if not planned:
        print("No models need repair - every primary artifact already has a loadable suffix.")
        return 0

    print(f"{len(planned)} model(s) to repair:")
    for mid, old, new, oldfmt, newfmt, fn in planned:
        ext = os.path.splitext(new)[1]
        newfn = fn if (fn or "").lower().endswith(ext.lower()) else f"{fn}{ext}"
        print(f"  {mid}\n      path     {old}  ->  {new}\n      format   {oldfmt!r} -> {newfmt!r}"
              f"\n      filename {fn!r} -> {newfn!r}")
    if not args.apply:
        print("\nDRY RUN - nothing changed. Re-run with --apply.")
        return 0

    for mid, old_rel, new_rel, _oldfmt, newfmt, _fn in planned:
        old_abs, new_abs = ap.resolve("models", old_rel), ap.resolve("models", new_rel)
        if not os.path.isfile(old_abs):
            print(f"  FAIL  {mid}: {old_rel} missing on disk"); failed += 1; continue
        if os.path.exists(new_abs):
            print(f"  FAIL  {mid}: {new_rel} already exists"); failed += 1; continue
        before = sha256_file(old_abs)[0]
        os.rename(old_abs, new_abs)
        try:
            after = sha256_file(new_abs)[0]
            if after != before:                             # cannot happen; proves it anyway
                raise RuntimeError(f"sha256 changed across rename: {before} -> {after}")
            with get_session() as s:
                m = s.execute(select(ModelRecord).where(ModelRecord.model_id == mid)).scalar_one()
                rep = s.execute(select(ModelRepresentation).where(
                    ModelRepresentation.model_id == m.id,
                    ModelRepresentation.kind == "primary")).scalar_one()
                a = s.execute(select(ModelArtifact).where(
                    ModelArtifact.relative_path == old_rel)).scalar_one()
                a.relative_path = new_rel
                for k, v in (fingerprint(new_abs) or {}).items():
                    setattr(a, k, v)                        # rename changes ctime
                a.last_verified_at = datetime.utcnow()
                rep.format = newfmt
                rep.manifest_sha256 = manifest_sha256([(new_rel, a.size_bytes or 0, a.sha256 or "")])
                rep.last_verified_at = a.last_verified_at
                ext = os.path.splitext(new_rel)[1]
                if m.filename and not m.filename.lower().endswith(ext.lower()):
                    m.filename = m.filename + ext
                s.commit()
            print(f"  OK    {mid}: {new_rel}  (sha256 unchanged {after[:12]}…)")
            repaired += 1
        except Exception as e:
            os.rename(new_abs, old_abs)                     # put it back, exactly as it was
            print(f"  FAIL  {mid}: {e.__class__.__name__}: {e} (file restored)")
            failed += 1

    print(f"\nrepaired={repaired} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
