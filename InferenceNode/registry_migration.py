"""One-time legacy -> PostgreSQL + ARTIFACT_ROOT migrations (Phase 6).

Markers live in app_state (versioned keys). A marker means: "every discovered legacy
record was deterministically processed and recorded with an explicit, fail-closed
state" - NOT that every artifact became AVAILABLE. MISSING/ambiguous rows are processed
records (state known, nothing served); unexpected exceptions, copy failures, hash
mismatches, DB failures and unrecorded artifacts BLOCK the marker. Idempotent and
restart-safe: rerunning re-verifies existing rows and resumes.

Legacy files are RETAINED (rollback) - nothing is deleted in this release.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

from . import app_state
from . import artifact_paths as ap
from .artifact_migration import (MigrationReport, enumerate_representation_dir,
                                 resolve_legacy_path, stage_copy_verify_promote)
from .artifact_states import ArtifactStatus as S, Reason, ValidationStatus as V
from . import model_registry

logger = logging.getLogger("InferenceNode.registry_migration")

MODELS_MARKER = "models_registry_to_postgres_v1"


def _safe_component(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)


def migrate_models_registry(models_metadata_json: str, legacy_models_dir: str,
                            *, force: bool = False) -> MigrationReport:
    """models_metadata.json (+ bytes) -> models/model_representations/model_artifacts +
    ARTIFACT_ROOT/models/<model_id>/... . Returns the report; sets the marker only when
    nothing blocked."""
    report = MigrationReport()
    if not force and app_state.get_state(MODELS_MARKER) == app_state.STATE_COMPLETED:
        logger.info("[MIGRATE] models registry already migrated (marker present)")
        return report
    ap.ensure_layout()

    try:
        with open(models_metadata_json, "r", encoding="utf-8") as f:
            legacy: Dict[str, dict] = json.load(f) or {}
    except FileNotFoundError:
        legacy = {}
    except Exception as e:  # noqa: BLE001
        logger.error(f"[MIGRATE] cannot read {models_metadata_json}: {e}")
        report.failed += 1
        return report

    report.discovered = len(legacy)
    for model_id, entry in legacy.items():
        try:
            _migrate_one_model(model_id, entry, legacy_models_dir, report)
            report.processed += 1
        except Exception as e:  # noqa: BLE001 - unexpected exception blocks the marker
            logger.error(f"[MIGRATE] model {model_id}: unexpected {e.__class__.__name__}: {e}")
            report.failed += 1
            report.details.append({"model_id": model_id, "status": "FAILED", "reason": str(e)})

    logger.info(f"[MIGRATE] models registry: {report.as_dict()}")
    if not report.blocking:
        app_state.set_state(MODELS_MARKER, app_state.STATE_COMPLETED)
    else:
        logger.error("[MIGRATE] models registry NOT marked complete (failed records present); rerun resumes")
    return report


def _migrate_one_model(model_id: str, entry: dict, legacy_dir: str, report: MigrationReport) -> None:
    name = entry.get("name") or model_id
    engine_type = entry.get("engine_type") or "ultralytics"
    stored_filename = entry.get("stored_filename") or entry.get("original_filename")
    model_registry.register_model(model_id=model_id, name=name, engine_type=engine_type,
                                  filename=entry.get("original_filename") or stored_filename,
                                  description=entry.get("description") or None,
                                  framework=engine_type, meta={"legacy": {k: v for k, v in entry.items()
                                                                          if k != "stored_path"}})
    safe_id = _safe_component(model_id)

    # ---- primary artifact (.pt / .onnx / ...)
    src, why = resolve_legacy_path(entry.get("stored_path"), [legacy_dir], stored_filename)
    ext = os.path.splitext(stored_filename or "")[1] or entry.get("file_extension") or ""
    fmt = (ext.lstrip(".") or "bin").lower()
    rel = f"{safe_id}/{_safe_component(stored_filename or (safe_id + ext))}"
    if src is None:
        status = S.MISSING
        model_registry.register_representation(model_id=model_id, format=fmt, kind="primary", required=True,
                                               files=[{"relative_path": rel, "status": status,
                                                       "validation_status": V.PENDING, "reason": why}])
        if why is Reason.AMBIGUOUS_LEGACY_PATH:
            report.ambiguous += 1
        else:
            report.missing += 1
        report.details.append({"model_id": model_id, "status": status.value, "reason": why.value})
        return
    mf = stage_copy_verify_promote("models", src, rel)
    model_registry.register_representation(model_id=model_id, format=fmt, kind="primary", required=True,
                                           files=[{"relative_path": mf.relative_path, "sha256": mf.sha256,
                                                   "size_bytes": mf.size_bytes, "status": mf.status,
                                                   "validation_status": mf.validation_status,
                                                   "reason": mf.reason, "fingerprint": mf.fingerprint}])
    if mf.status is S.AVAILABLE:
        report.available += 1
    elif mf.status is S.MISSING:
        report.missing += 1
    else:
        report.failed += 1
    report.details.append({"model_id": model_id, "status": mf.status.value,
                           "reason": mf.reason.value if mf.reason else None, "relative_path": mf.relative_path})

    # ---- derived multi-file representations next to the primary (e.g. <stem>_openvino_model/)
    stem = os.path.splitext(os.path.basename(src))[0]
    parent = os.path.dirname(src)
    for d in sorted(os.listdir(parent)) if os.path.isdir(parent) else []:
        full = os.path.join(parent, d)
        if not os.path.isdir(full) or not d.startswith(stem):
            continue
        dfmt, comps, other = enumerate_representation_dir(full)
        if not dfmt:
            continue
        for o in other:
            report.unregistered.append(o)                  # reported, left untouched
        files = []
        for c in comps:
            crel = f"{safe_id}/{_safe_component(d)}/{_safe_component(os.path.basename(c))}"
            cmf = stage_copy_verify_promote("models", c, crel)
            files.append({"relative_path": cmf.relative_path, "sha256": cmf.sha256, "size_bytes": cmf.size_bytes,
                          "status": cmf.status, "validation_status": cmf.validation_status,
                          "reason": cmf.reason, "fingerprint": cmf.fingerprint})
            if cmf.status is S.FAILED:
                report.failed += 1
        if files:
            model_registry.register_representation(model_id=model_id, format=dfmt, kind="derived", required=False,
                                                   files=files)


# ====================================================================== node_settings.json
NODE_SETTINGS_MARKER = "node_settings_to_postgres_v1"


def migrate_node_settings(node_settings_json: str, *, force: bool = False) -> MigrationReport:
    """node_settings.json -> node_settings (identity, telemetry) + publishers (favorites +
    node destinations). Secrets are encrypted on the way in (config_secrets); if no key is
    loaded and a secret is present the record FAILS (blocking) rather than being stored in
    clear. The JSON is retained read-only for one release; the app stops reading it as
    authoritative once the marker is set."""
    from . import node_settings_store as nss, publisher_store as pst
    from .config_secrets import SecretsUnavailable
    report = MigrationReport()
    if not force and app_state.get_state(NODE_SETTINGS_MARKER) == app_state.STATE_COMPLETED:
        return report
    try:
        with open(node_settings_json, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except FileNotFoundError:
        data = {}
    except Exception as e:  # noqa: BLE001
        logger.error(f"[MIGRATE] cannot read {node_settings_json}: {e}")
        report.failed += 1
        return report

    def _one(label, fn):
        report.discovered += 1
        try:
            fn(); report.processed += 1; report.available += 1
        except SecretsUnavailable as e:
            report.failed += 1
            report.details.append({"record": label, "status": "FAILED", "reason": Reason.ENCRYPTION_KEY_MISSING.value})
            logger.error(f"[MIGRATE] {label}: secret present but no encryption key loaded - refusing to store in clear")
        except Exception as e:  # noqa: BLE001
            report.failed += 1
            report.details.append({"record": label, "status": "FAILED", "reason": str(e)})
            logger.error(f"[MIGRATE] {label}: {e.__class__.__name__}: {e}")

    ident = {k: data[k] for k in ("node_id", "node_name") if data.get(k)}
    if ident:
        _one("node_identity", lambda: nss.set_setting(nss.KEY_NODE_IDENTITY, ident))
    tel = data.get("telemetry") or {}
    if tel:
        _one("telemetry", lambda: nss.set_setting(nss.KEY_TELEMETRY, tel))
    for fid, fav in (data.get("favorite_configs") or {}).items():
        def _mk(fid=fid, fav=fav):
            if pst.get_publisher(fid) is None:
                pst.create_publisher(publisher_id=fid, name=fav.get("name") or fav.get("type") or fid,
                                     type=fav.get("type") or "unknown", config=fav.get("config") or {},
                                     kind="favorite", enabled=True)
        _one(f"favorite:{fid}", _mk)
    for pub in (data.get("publishers") or []):
        pid = pub.get("id") or str(__import__("uuid").uuid4())
        def _mk2(pid=pid, pub=pub):
            if pst.get_publisher(pid) is None:
                pst.create_publisher(publisher_id=pid, name=pub.get("name") or pub.get("type") or pid,
                                     type=pub.get("type") or "unknown", config=pub.get("config") or {},
                                     kind="node_destination", enabled=pub.get("enabled", True))
        _one(f"publisher:{pid}", _mk2)

    logger.info(f"[MIGRATE] node settings: {report.as_dict()}")
    if not report.blocking:
        app_state.set_state(NODE_SETTINGS_MARKER, app_state.STATE_COMPLETED)
    return report


# ------------------------------------------------------------------ media + thumbnails (Phase 12)
MEDIA_MARKER = "media_registry_to_postgres_v1"
THUMBNAILS_MARKER = "thumbnails_registry_to_postgres_v1"


def migrate_media_registry(legacy_media_dir: str, *, force: bool = False) -> MigrationReport:
    """Legacy media dir -> ARTIFACT_ROOT/media (physical stage/verify/promote, relative
    paths preserved so `relative_source` keeps resolving) + media_assets rows. Marker means
    every discovered file was deterministically processed (failed>0 blocks it)."""
    from .media_registry import migrate_legacy_media
    report = MigrationReport()
    if not force and app_state.get_state(MEDIA_MARKER) == app_state.STATE_COMPLETED:
        return report
    ap.ensure_layout()
    try:
        migrate_legacy_media(legacy_media_dir, report)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[MIGRATE] media: unexpected {e.__class__.__name__}: {e}")
        report.failed += 1
    logger.info(f"[MIGRATE] media registry: {report.as_dict()}")
    if not report.blocking:
        app_state.set_state(MEDIA_MARKER, app_state.STATE_COMPLETED)
    else:
        logger.error("[MIGRATE] media registry NOT marked complete (failed records present); rerun resumes")
    return report


def migrate_thumbnails_registry(legacy_thumbs_dir: str, *, force: bool = False) -> MigrationReport:
    """Legacy pipelines/thumbnails -> ARTIFACT_ROOT/thumbnails + pipeline_thumbnails rows,
    registered ONLY for pipelines that still exist; orphans are reported (unregistered),
    never deleted."""
    from .media_registry import migrate_legacy_thumbnails
    report = MigrationReport()
    if not force and app_state.get_state(THUMBNAILS_MARKER) == app_state.STATE_COMPLETED:
        return report
    ap.ensure_layout()
    try:
        migrate_legacy_thumbnails(legacy_thumbs_dir, report)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[MIGRATE] thumbnails: unexpected {e.__class__.__name__}: {e}")
        report.failed += 1
    logger.info(f"[MIGRATE] thumbnails registry: {report.as_dict()} "
                f"(orphans reported: {len(report.unregistered)})")
    if not report.blocking:
        app_state.set_state(THUMBNAILS_MARKER, app_state.STATE_COMPLETED)
    else:
        logger.error("[MIGRATE] thumbnails registry NOT marked complete; rerun resumes")
    return report
