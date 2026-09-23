"""Phase 6 - legacy models_metadata.json + bytes -> PostgreSQL registry + ARTIFACT_ROOT.

Runs on temp SQLite here (logic) and on isolated PostgreSQL via scripts/pg-test.sh
(same file - the fixture just points ARMYEYE_ARTIFACT_ROOT at a temp dir).
"""
import hashlib
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db                        # noqa: E402
from InferenceNode.auth.models import Base                          # noqa: E402
import InferenceNode.data_models                                    # noqa: E402,F401
from InferenceNode import app_state, artifact_paths as ap           # noqa: E402
from InferenceNode import model_registry as reg                     # noqa: E402
from InferenceNode.registry_migration import migrate_models_registry, MODELS_MARKER  # noqa: E402
from InferenceNode.artifact_states import ArtifactStatus as S, Reason  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'m.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("ARMYEYE_ARTIFACT_ROOT", str(tmp_path / "root"))
    ap.ensure_layout()
    legacy = tmp_path / "model_repository"
    (legacy / "models").mkdir(parents=True)
    yield {"tmp": tmp_path, "legacy": legacy, "models_dir": legacy / "models",
           "json": legacy / "models_metadata.json"}
    auth_db._engine = None; auth_db._SessionLocal = None


def _legacy_model(env, mid, name, data=b"weights", with_openvino=False, stored_path=None):
    fn = f"{mid}.pt"
    p = env["models_dir"] / fn
    p.write_bytes(data)
    entry = {"id": mid, "name": name, "original_filename": f"{name}.pt", "stored_filename": fn,
             "stored_path": stored_path or r"C:\Users\legacy\ArmyEye\InferenceNode\model_repository\models\\" + fn,
             "engine_type": "ultralytics", "description": "", "file_size": str(len(data)),
             "upload_date": "2025-12-13T20:17:25", "file_extension": ".pt"}
    if with_openvino:
        d = env["models_dir"] / f"{mid}_openvino_model"; d.mkdir()
        (d / f"{mid}.xml").write_bytes(b"<net/>"); (d / f"{mid}.bin").write_bytes(b"\x00\x01\x02")
        (d / "metadata.yaml").write_bytes(b"a: 1"); (d / "README.txt").write_bytes(b"junk")
    return entry


def _write_json(env, entries):
    env["json"].write_text(json.dumps({e["id"]: e for e in entries}))


def test_legacy_models_populate_registry_with_verified_bytes(env):
    _write_json(env, [_legacy_model(env, "yolov8n_03056081", "yolov8n", b"AAA", with_openvino=True)])
    rep = migrate_models_registry(str(env["json"]), str(env["models_dir"]))
    assert rep.discovered == 1 and rep.processed == 1 and rep.available == 1 and rep.failed == 0
    assert app_state.get_state(MODELS_MARKER) == app_state.STATE_COMPLETED
    m = reg.get_model("yolov8n_03056081")
    assert m["status"] == "AVAILABLE"
    prim = [r for r in m["representations"] if r["kind"] == "primary"][0]
    art = prim["artifacts"][0]
    path = ap.resolve("models", art["relative_path"])
    assert os.path.isfile(path)
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == art["sha256"] == hashlib.sha256(b"AAA").hexdigest()
    assert art["size_bytes"] == 3
    # legacy source retained
    assert (env["models_dir"] / "yolov8n_03056081.pt").exists()
    # OpenVINO: xml + bin (+metadata) registered separately, README reported not registered
    ov = [r for r in m["representations"] if r["format"] == "openvino"][0]
    names = sorted(os.path.basename(a["relative_path"]) for a in ov["artifacts"])
    assert names == ["metadata.yaml", "yolov8n_03056081.bin", "yolov8n_03056081.xml"]
    assert ov["manifest_sha256"] and len(ov["manifest_sha256"]) == 64
    assert any(u.endswith("README.txt") for u in rep.unregistered)
    assert ov["required"] is False and prim["required"] is True


def test_migration_run_twice_creates_no_duplicates(env):
    _write_json(env, [_legacy_model(env, "m1", "one"), _legacy_model(env, "m2", "two")])
    r1 = migrate_models_registry(str(env["json"]), str(env["models_dir"]))
    r2 = migrate_models_registry(str(env["json"]), str(env["models_dir"]), force=True)
    assert r1.available == 2 and r2.available == 2
    assert len(reg.list_models()) == 2
    for m in reg.list_models():
        assert len([r for r in m["representations"] if r["kind"] == "primary"]) == 1
        assert len(m["representations"][0]["artifacts"]) == 1


def test_ambiguous_legacy_path_is_missing_with_reason_and_does_not_block_marker(env):
    e = _legacy_model(env, "amb", "amb")
    e["stored_path"] = r"C:\gone\amb.pt"
    # make the basename ambiguous: two files named amb.pt under the search dir
    (env["models_dir"] / "sub").mkdir(); (env["models_dir"] / "sub" / "amb.pt").write_bytes(b"other")
    _write_json(env, [e])
    rep = migrate_models_registry(str(env["json"]), str(env["models_dir"]))
    assert rep.ambiguous == 1 and rep.failed == 0 and rep.processed == 1
    m = reg.get_model("amb")
    art = m["representations"][0]["artifacts"][0]
    assert art["status"] == "MISSING" and art["reason"] == Reason.AMBIGUOUS_LEGACY_PATH.value
    assert m["status"] != "AVAILABLE"
    assert app_state.get_state(MODELS_MARKER) == app_state.STATE_COMPLETED   # deterministically processed
    assert m["status"] in ("STAGING", "VALIDATING", "AVAILABLE", "FAILED", "MISSING", "CORRUPT", "DELETING")


def test_missing_legacy_file_is_missing_not_available(env):
    e = _legacy_model(env, "gone", "gone")
    os.remove(env["models_dir"] / "gone.pt")
    e["stored_path"] = r"C:\gone\gone.pt"
    _write_json(env, [e])
    rep = migrate_models_registry(str(env["json"]), str(env["models_dir"]))
    assert rep.missing == 1 and rep.available == 0
    m = reg.get_model("gone")
    assert m["status"] == "MISSING"
    assert m["representations"][0]["artifacts"][0]["reason"] == Reason.LEGACY_FILE_NOT_FOUND.value


def test_copy_failure_blocks_the_marker(env, monkeypatch):
    _write_json(env, [_legacy_model(env, "m1", "one")])
    import InferenceNode.registry_migration as rm
    from InferenceNode.artifact_migration import MigratedFile
    from InferenceNode.artifact_states import ValidationStatus as V
    def boom(kind, src, rel):
        return MigratedFile(rel, "", 0, S.FAILED, V.FAILED, Reason.COPY_FAILED, src)
    monkeypatch.setattr(rm, "stage_copy_verify_promote", boom)
    rep = migrate_models_registry(str(env["json"]), str(env["models_dir"]))
    assert rep.failed == 1 and rep.blocking
    assert app_state.get_state(MODELS_MARKER) != app_state.STATE_COMPLETED
    assert reg.get_model("m1")["status"] != "AVAILABLE"


def test_marker_makes_rerun_a_noop_unless_forced(env):
    _write_json(env, [_legacy_model(env, "m1", "one")])
    migrate_models_registry(str(env["json"]), str(env["models_dir"]))
    rep = migrate_models_registry(str(env["json"]), str(env["models_dir"]))
    assert rep.discovered == 0 and rep.processed == 0


# ------------------------------------------------------------------ aggregation rule
def _model_with(env, mid, reps):
    reg.register_model(model_id=mid, name=mid, engine_type="ultralytics")
    for fmt, kind, required, status in reps:
        reg.register_representation(model_id=mid, format=fmt, kind=kind, required=required,
                                    files=[{"relative_path": f"{mid}/{fmt}.bin", "sha256": "a" * 64, "size_bytes": 1,
                                            "status": status, "validation_status": "PASSED" if status is S.AVAILABLE else "FAILED",
                                            "reason": None if status is S.AVAILABLE else Reason.HASH_MISMATCH}])
    return reg.get_model(mid)["status"]


def test_optional_derived_failure_does_not_disable_model(env):
    assert _model_with(env, "a", [("pt", "primary", True, S.AVAILABLE), ("openvino", "derived", False, S.CORRUPT)]) == "AVAILABLE"


def test_all_required_representations_must_be_available(env):
    assert _model_with(env, "b", [("pt", "primary", True, S.AVAILABLE), ("engine", "derived", True, S.MISSING)]) != "AVAILABLE"


def test_no_usable_representation_disables_model(env):
    assert _model_with(env, "c", [("pt", "primary", True, S.MISSING), ("onnx", "derived", False, S.MISSING)]) == "MISSING"


def test_model_status_uses_only_registry_vocabulary(env):
    for st in ("a", "b", "c"):
        m = reg.get_model(st)
        if m:
            assert m["status"] in ("STAGING", "VALIDATING", "AVAILABLE", "FAILED", "MISSING", "CORRUPT", "DELETING")


# --------------------------------------------------------------------- extension preservation
# A legacy entry whose `stored_filename` had lost its extension migrated to a managed file
# with NO suffix. Ultralytics dispatches on the file suffix, so the engine raised
# "is not a supported model format" on every single frame; the engine catches that, so the
# pipeline stayed green and healthy while publishing zero detections. Found in production:
# model yolov8n_95a24496 stored as `.../yolov8n_95a24496` with representation format 'bin'.
#
# The helper above always wrote "<id>.pt", so no existing test could have caught it.
def _legacy_model_no_ext(env, mid, name, data=b"weights", *, original=None, file_ext=None):
    """A legacy entry whose stored_filename carries NO extension."""
    p = env["models_dir"] / mid                      # on-disk legacy name, also extensionless
    p.write_bytes(data)
    entry = {"id": mid, "name": name, "stored_filename": mid,
             "stored_path": str(p), "engine_type": "ultralytics", "description": "",
             "file_size": str(len(data)), "upload_date": "2025-12-13T20:17:25"}
    if original is not None:
        entry["original_filename"] = original
    if file_ext is not None:
        entry["file_extension"] = file_ext
    return entry


def _primary(model_id):
    m = reg.get_model(model_id)
    for r in m.get("representations", []):
        if r.get("kind") == "primary":
            return r
    raise AssertionError("no primary representation")


def test_extensionless_legacy_entry_still_lands_on_a_loadable_suffix(env):
    """The exact production case: stored_filename lost '.pt', original_filename still has it."""
    e = _legacy_model_no_ext(env, "yolov8n_95a24496", "yolov8n", original="yolov8n.pt")
    _write_json(env, [e])
    migrate_models_registry(str(env["json"]), str(env["models_dir"]))

    rep = _primary("yolov8n_95a24496")
    rel = rep["artifacts"][0]["relative_path"]
    assert rel.endswith(".pt"), f"migrated to {rel!r} - the engine loads by SUFFIX and will reject it"
    assert rep["format"] == "pt", f"format={rep['format']!r}; 'bin' is what made this invisible"
    assert rep["artifacts"][0]["status"] == S.AVAILABLE.value


def test_the_bytes_are_still_verified_after_the_rename(env):
    """Renaming the destination must not break the sha256/size contract."""
    data = b"a real checkpoint" * 100
    _write_json(env, [_legacy_model_no_ext(env, "m_1", "m", data=data, original="m.pt")])
    migrate_models_registry(str(env["json"]), str(env["models_dir"]))

    a = _primary("m_1")["artifacts"][0]
    assert a["sha256"] == hashlib.sha256(data).hexdigest()
    assert a["size_bytes"] == len(data)
    assert os.path.isfile(ap.resolve("models", a["relative_path"]))


def test_it_falls_back_to_the_recorded_file_extension(env):
    """Some legacy rows have no usable filename at all, only `file_extension`."""
    _write_json(env, [_legacy_model_no_ext(env, "m_2", "m", file_ext=".onnx")])
    migrate_models_registry(str(env["json"]), str(env["models_dir"]))

    rep = _primary("m_2")
    assert rep["artifacts"][0]["relative_path"].endswith(".onnx")
    assert rep["format"] == "onnx"


def test_a_correct_legacy_entry_is_left_alone(env):
    """No double-suffixing: '<id>.pt' must not become '<id>.pt.pt'."""
    _write_json(env, [_legacy_model(env, "m_3", "m")])
    migrate_models_registry(str(env["json"]), str(env["models_dir"]))

    rel = _primary("m_3")["artifacts"][0]["relative_path"]
    assert rel.endswith(".pt") and not rel.endswith(".pt.pt"), rel
    assert _primary("m_3")["format"] == "pt"


def test_an_entry_with_no_extension_anywhere_still_migrates(env):
    """Unknown format must not crash the migration - it degrades to 'bin', which is
    honest: we genuinely do not know what the bytes are."""
    _write_json(env, [_legacy_model_no_ext(env, "m_4", "m")])
    migrate_models_registry(str(env["json"]), str(env["models_dir"]))

    rep = _primary("m_4")
    assert rep["format"] == "bin"
    assert rep["artifacts"][0]["status"] == S.AVAILABLE.value


def test_uploads_were_never_affected(env):
    """store_model derives the stored name as f'{model_id}{ext}' from the ORIGINAL filename,
    so the upload path always preserved the suffix. Pinned so a 'consistency' refactor that
    aligns upload with the old migration behaviour cannot reintroduce the bug."""
    src = open(os.path.join(REPO, "InferenceNode", "model_repo.py"), encoding="utf-8").read()
    i = src.index("def _store_model_unlocked")
    body = src[i:i + 1500]
    assert 'ext = os.path.splitext(original_filename)[1]' in body
    assert 'stored_filename = f"{model_id}{ext}"' in body
