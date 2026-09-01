"""Unified registry <-> filesystem reconciliation (Phase 13, sections 3f/3g/9).

ONE report over every managed plane - models (multi-file aware), custom engines,
thumbnails, media, publisher/telemetry secrets, and the pipeline->model reference
invariant. Report ONLY: nothing is deleted or repaired here; repair/delete are explicit
admin actions through the owning service. Every finding names the artifact, so the admin
endpoint and the startup log tell exactly what is inconsistent.

Verdict rules (fail-closed):
  healthy   - no AVAILABLE artifact with missing/mismatched bytes, no engine tamper, no
              undecryptable secrets, no pipeline model_id/config divergence
  degraded  - anything above is present (details listed); orphan files and STAGING/FAILED
              leftovers are reported as findings but do not by themselves flip the verdict
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger("InferenceNode.artifact_verify")


def _safe(section: str, fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - one broken plane must not hide the others
        logger.error(f"[VERIFY] {section} failed: {e.__class__.__name__}: {e}")
        return {"error": f"{e.__class__.__name__}: {e}"}


def verify_models(model_repo) -> Dict[str, Any]:
    return _safe("models", model_repo.verify)


def verify_engines() -> Dict[str, Any]:
    from . import engine_registry
    return _safe("engines", engine_registry.verify_all)


def verify_thumbnails() -> Dict[str, Any]:
    from . import thumbnail_registry
    return _safe("thumbnails", thumbnail_registry.verify_all)


def verify_media() -> Dict[str, Any]:
    from . import media_registry
    return _safe("media", media_registry.verify_all)


def verify_secrets() -> Dict[str, Any]:
    """Publisher/telemetry rows whose encrypted credentials cannot be decrypted with the
    loaded keyring (key missing/wrong) - reported, never re-encrypted, never served."""
    def _run():
        from . import publisher_store as pst
        from . import node_settings_store as nss
        from . import config_secrets
        out = {"keys_available": config_secrets.keys_available(), "publishers_undecryptable": [],
               "telemetry_undecryptable": False}
        for row in pst.list_publishers(runtime=True):
            if not row.get("secrets_ok", True):
                out["publishers_undecryptable"].append(row["id"])
        tel = nss.get_setting(nss.KEY_TELEMETRY, runtime=True) or {}
        if tel.get("_secrets_ok") is False:
            out["telemetry_undecryptable"] = True
        return out
    return _safe("secrets", _run)


def verify_pipeline_model_refs() -> Dict[str, Any]:
    """pipelines.model_id (canonical) vs config.model.id (reflection) - must never
    silently diverge; unknown references reported, never guessed."""
    def _run():
        from sqlalchemy import select
        from .auth.db import get_session
        from .data_models import Pipeline, ModelRecord
        out = {"total": 0, "consistent": 0, "divergent": [], "unknown_model": [], "no_model": 0}
        with get_session() as s:
            known = {m.model_id for m in s.execute(select(ModelRecord)).scalars()}
            for p in s.execute(select(Pipeline)).scalars():
                out["total"] += 1
                cfg = p.config if isinstance(p.config, dict) else {}
                ref = ((cfg.get("model") or {}) if isinstance(cfg.get("model"), dict) else {}).get("id")
                if p.model_id is None and ref is None:
                    out["no_model"] += 1; continue
                if p.model_id is not None and ref is not None and str(ref) != str(p.model_id):
                    out["divergent"].append({"pipeline_id": p.pipeline_id, "model_id": p.model_id, "config_model_id": ref})
                    continue
                target = p.model_id or ref
                if target not in known:
                    out["unknown_model"].append({"pipeline_id": p.pipeline_id, "model_id": target})
                    continue
                out["consistent"] += 1
        return out
    return _safe("pipeline_model_refs", _run)


def _count(x) -> int:
    if isinstance(x, list):
        return len(x)
    if isinstance(x, int):
        return x
    return 0


def summarize(report: Dict[str, Any]) -> Dict[str, Any]:
    """Fail-closed verdict + per-plane counters (no filesystem access here)."""
    m = report.get("models") or {}
    e = report.get("engines") or {}
    t = report.get("thumbnails") or {}
    md = report.get("media") or {}
    sec = report.get("secrets") or {}
    refs = report.get("pipeline_model_refs") or {}
    problems: List[str] = []
    for plane, rep in (("models", m), ("engines", e), ("thumbnails", t), ("media", md)):
        if "error" in rep:
            problems.append(f"{plane}: verifier error"); continue
        for key in ("available_missing", "available_hash_mismatch"):
            n = _count(rep.get(key))
            if n:
                problems.append(f"{plane}: {n} AVAILABLE artifact(s) {key.replace('available_', '')}")
    if _count(m.get("representations_degraded")):
        problems.append(f"models: {_count(m.get('representations_degraded'))} degraded representation(s)")
    if _count(m.get("row_no_artifact")):
        problems.append(f"models: {_count(m.get('row_no_artifact'))} row(s) with unresolvable path")
    if _count(sec.get("publishers_undecryptable")) or sec.get("telemetry_undecryptable"):
        problems.append("secrets: undecryptable credentials present (encryption key missing/wrong)")
    if _count(refs.get("divergent")):
        problems.append(f"pipelines: {_count(refs.get('divergent'))} model reference(s) diverged")
    if _count(refs.get("unknown_model")):
        problems.append(f"pipelines: {_count(refs.get('unknown_model'))} reference(s) to unknown models")
    warnings: List[str] = []
    for plane, rep, key in (("models", m, "artifact_no_row"), ("engines", e, "orphan_files"),
                            ("thumbnails", t, "orphan_files"), ("media", md, "orphan_files")):
        n = _count(rep.get(key))
        if n:
            warnings.append(f"{plane}: {n} unregistered file(s) under the managed root (reported, not deleted)")
    for key in ("staging_staged", "staging_final", "failed_present", "deleting_trash"):
        n = _count(m.get(key))
        if n:
            warnings.append(f"models: {n} artifact(s) in {key}")
    return {"verdict": "degraded" if problems else "healthy", "problems": problems, "warnings": warnings}


def verify_all(model_repo) -> Dict[str, Any]:
    """The admin endpoint / startup entry point. Never mutates registry rows other than
    the lazy state downgrades the owning registries already perform on inspection."""
    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "artifact_root": _artifact_root_public(),
        "models": verify_models(model_repo),
        "engines": verify_engines(),
        "thumbnails": verify_thumbnails(),
        "media": verify_media(),
        "secrets": verify_secrets(),
        "pipeline_model_refs": verify_pipeline_model_refs(),
    }
    report["summary"] = summarize(report)
    return report


def _artifact_root_public() -> Dict[str, Any]:
    """Only whether the root is present/writable - never the host path itself."""
    from . import artifact_paths as ap
    try:
        root = ap.artifact_root()
        return {"present": os.path.isdir(root), "writable": os.access(root, os.W_OK),
                "kinds": {k: os.path.isdir(ap.kind_root(k)) for k in ap.KINDS}}
    except Exception as e:  # noqa: BLE001
        return {"present": False, "error": e.__class__.__name__}


def log_startup_report(model_repo, log=logger) -> Dict[str, Any]:
    rep = verify_all(model_repo)
    s = rep["summary"]
    line = f"[VERIFY] registry reconciliation: {s['verdict']}"
    if s["problems"]:
        log.error(line + " - " + "; ".join(s["problems"]))
    else:
        log.info(line)
    for w in s["warnings"]:
        log.warning(f"[VERIFY] {w}")
    return rep
