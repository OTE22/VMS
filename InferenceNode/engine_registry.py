"""Inference-engine registry - PostgreSQL authoritative (Phase 11).

Two origins, two storage models, never mixed:

  builtin  shipped read-only in the image (InferenceEngine/engines/*.py); registry row
           holds logical metadata + shipped_sha256; relative_path/sha256 NULL; NOT
           deletable through the custom-engine API; no trash/quarantine semantics.
  custom   an executable .py artifact under ARTIFACT_ROOT/engines/<key>/engine.py
           (persistent, bind-mounted -> survives recreation); relative_path + sha256 +
           size NOT NULL (DB CHECK); creation/deletion state machines; imported ONLY
           when servable (AVAILABLE + PASSED + present + hash re-verified on load).

DB invariant (CHECK): enabled => status=AVAILABLE AND validation_status=PASSED.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import select

from . import artifact_paths as ap
from .artifact_states import (ArtifactStatus as S, ValidationStatus as V, Reason,
                              EngineOrigin, fingerprint, fingerprint_matches, transition)
from .auth.db import get_session
from .data_models import InferenceEngineRecord

logger = logging.getLogger("InferenceNode.engine_registry")


def _sha_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _dict(e: InferenceEngineRecord) -> dict:
    return {"engine_key": e.engine_key, "class_name": e.class_name, "display_name": e.display_name,
            "engine_type": e.engine_type, "version": e.version, "origin": e.origin,
            "status": e.status, "validation_status": e.validation_status, "reason": e.reason,
            "enabled": bool(e.enabled), "relative_path": e.relative_path, "sha256": e.sha256,
            "size_bytes": e.size_bytes, "shipped_sha256": e.shipped_sha256,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "last_verified_at": e.last_verified_at.isoformat() if e.last_verified_at else None}


def relative_path_for(engine_key: str) -> str:
    return f"{engine_key}/engine.py"


# ------------------------------------------------------------------ builtin registration
def register_builtin(engine_key: str, class_name: str, display_name: str = None,
                     source_path: str = None) -> dict:
    """Idempotent. Builtins are AVAILABLE/PASSED/enabled by definition of being shipped;
    shipped_sha256 recorded when the source path is known (drift is informational)."""
    sha = _sha_of(source_path) if source_path and os.path.isfile(source_path) else None
    with get_session() as s:
        e = s.execute(select(InferenceEngineRecord).where(InferenceEngineRecord.engine_key == engine_key)).scalar_one_or_none()
        if e is None:
            e = InferenceEngineRecord(engine_key=engine_key, class_name=class_name, display_name=display_name,
                                      origin=EngineOrigin.BUILTIN.value, status=S.AVAILABLE.value,
                                      validation_status=V.PASSED.value, enabled=True, shipped_sha256=sha)
            s.add(e)
        else:
            e.class_name = class_name or e.class_name; e.display_name = display_name or e.display_name
            if sha:
                e.shipped_sha256 = sha
        s.flush()
        return _dict(e)


# ------------------------------------------------------------------ custom lifecycle
def begin_custom(engine_key: str, class_name: str, display_name: str = None,
                 created_by: Optional[int] = None) -> dict:
    """Step 1 of creation: INSERT STAGING (nothing importable). Row carries the future
    relative_path + a placeholder sha256 to satisfy the custom-requires-artifact CHECK."""
    with get_session() as s:
        e = s.execute(select(InferenceEngineRecord).where(InferenceEngineRecord.engine_key == engine_key)).scalar_one_or_none()
        if e is not None:
            raise ValueError(f"engine key '{engine_key}' already registered")
        e = InferenceEngineRecord(engine_key=engine_key, class_name=class_name, display_name=display_name,
                                  origin=EngineOrigin.CUSTOM.value, status=S.STAGING.value,
                                  validation_status=V.PENDING.value, enabled=False,
                                  relative_path=relative_path_for(engine_key), sha256="0" * 64,
                                  size_bytes=0, created_by=created_by)
        s.add(e); s.flush()
        return _dict(e)


def set_state(engine_key: str, status: S, vstatus: V, reason: Optional[Reason] = None, *,
              sha256: str = None, size_bytes: int = None, fp: dict = None, enabled: bool = None) -> dict:
    with get_session() as s:
        e = s.execute(select(InferenceEngineRecord).where(InferenceEngineRecord.engine_key == engine_key)).scalar_one()
        if e.status != status.value:
            e.status = transition(e.status, status).value
        e.validation_status = vstatus.value
        e.reason = reason.value if reason else None
        if sha256 is not None:
            e.sha256 = sha256
        if size_bytes is not None:
            e.size_bytes = size_bytes
        for k, v in (fp or {}).items():
            setattr(e, k, v)
        if status is S.AVAILABLE and vstatus is V.PASSED:
            e.last_verified_at = datetime.utcnow()
            e.enabled = True if enabled is None else bool(enabled)
        else:
            e.enabled = False           # DB CHECK: enabled only when AVAILABLE+PASSED
        e.updated_at = datetime.utcnow()
        s.flush()
        return _dict(e)


def get(engine_key: str) -> Optional[dict]:
    with get_session() as s:
        e = s.execute(select(InferenceEngineRecord).where(InferenceEngineRecord.engine_key == engine_key)).scalar_one_or_none()
        return None if e is None else _dict(e)


def list_engines(origin: Optional[str] = None) -> List[dict]:
    with get_session() as s:
        q = select(InferenceEngineRecord).order_by(InferenceEngineRecord.created_at)
        if origin:
            q = q.where(InferenceEngineRecord.origin == origin)
        return [_dict(e) for e in s.execute(q).scalars().all()]


def remove(engine_key: str) -> bool:
    with get_session() as s:
        e = s.execute(select(InferenceEngineRecord).where(InferenceEngineRecord.engine_key == engine_key)).scalar_one_or_none()
        if e is None:
            return False
        s.delete(e)
        return True


# ------------------------------------------------------------------ serving / verification
def servable_custom_path(engine_key: str) -> Optional[str]:
    """Resolved path of a custom engine ONLY if AVAILABLE + PASSED + present + sha256
    re-verified now (executable code: always hash on load). Drift -> CORRUPT/MISSING."""
    e = get(engine_key)
    if not e or e["origin"] != EngineOrigin.CUSTOM.value:
        return None
    if e["status"] != S.AVAILABLE.value or e["validation_status"] != V.PASSED.value or not e["enabled"]:
        return None
    try:
        path = ap.resolve("engines", e["relative_path"])
    except ap.ArtifactPathError:
        return None
    if not os.path.isfile(path):
        set_state(engine_key, S.VALIDATING, V.PENDING)
        set_state(engine_key, S.MISSING, V.PENDING, Reason.FILE_MISSING)
        return None
    if _sha_of(path) != e["sha256"]:
        set_state(engine_key, S.VALIDATING, V.PENDING)
        set_state(engine_key, S.CORRUPT, V.HASH_MISMATCH, Reason.HASH_MISMATCH)
        return None
    return path


def verify_all() -> Dict[str, list]:
    """Startup / admin reconciliation for custom engines. Report only."""
    report = {"available_valid": [], "available_missing": [], "available_hash_mismatch": [],
              "staging": [], "failed": [], "deleting": [], "builtin": []}
    for e in list_engines():
        if e["origin"] == EngineOrigin.BUILTIN.value:
            report["builtin"].append(e["engine_key"]); continue
        if e["status"] == S.AVAILABLE.value:
            try:
                p = ap.resolve("engines", e["relative_path"])
            except ap.ArtifactPathError:
                report["available_missing"].append(e["engine_key"]); continue
            if not os.path.isfile(p):
                report["available_missing"].append(e["engine_key"])
                set_state(e["engine_key"], S.VALIDATING, V.PENDING)
                set_state(e["engine_key"], S.MISSING, V.PENDING, Reason.FILE_MISSING)
            elif _sha_of(p) != e["sha256"]:
                report["available_hash_mismatch"].append(e["engine_key"])
                set_state(e["engine_key"], S.VALIDATING, V.PENDING)
                set_state(e["engine_key"], S.CORRUPT, V.HASH_MISMATCH, Reason.HASH_MISMATCH)
            else:
                report["available_valid"].append(e["engine_key"])
        elif e["status"] == S.STAGING.value:
            report["staging"].append(e["engine_key"])
        elif e["status"] == S.FAILED.value:
            report["failed"].append(e["engine_key"])
        elif e["status"] == S.DELETING.value:
            report["deleting"].append(e["engine_key"])
    return report
