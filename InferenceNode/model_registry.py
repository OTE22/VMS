"""ModelRegistry - PostgreSQL is the AUTHORITATIVE registry for models.

Logical model (models) -> one or more representations (model_representations)
-> one row PER PHYSICAL FILE (model_artifacts). Bytes live under ARTIFACT_ROOT/models
and are only ever reached through artifact_paths.resolve().

Only this module sets `models.status`: recompute_model_status() implements the ONE
aggregation rule
    model AVAILABLE  <=>  metadata valid
                          AND ALL required representations are AVAILABLE
                          AND at least one usable (servable) representation exists
and is invoked after creation, validation, reconciliation, deletion, derived export and
corruption detection - no route decides model health on its own.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from . import artifact_paths as ap
from .artifact_migration import manifest_sha256, sha256_file
from .artifact_states import (ArtifactStatus as S, ValidationStatus as V, Reason,
                              can_transition, fingerprint, is_servable, transition)
from .auth.db import get_session
from .data_models import ModelArtifact, ModelRecord, ModelRepresentation

logger = logging.getLogger("InferenceNode.model_registry")


# ------------------------------------------------------------------ serialization
def _artifact_dict(a: ModelArtifact) -> dict:
    return {"id": a.id, "relative_path": a.relative_path, "sha256": a.sha256,
            "size_bytes": a.size_bytes, "status": a.status,
            "validation_status": a.validation_status, "reason": a.reason,
            "last_verified_at": a.last_verified_at.isoformat() if a.last_verified_at else None}


def _repr_dict(r: ModelRepresentation, artifacts: List[ModelArtifact]) -> dict:
    return {"id": r.id, "format": r.format, "kind": r.kind, "required": bool(r.required),
            "precision": r.precision, "device_family": r.device_family, "runtime": r.runtime,
            "manifest_sha256": r.manifest_sha256, "status": r.status,
            "validation_status": r.validation_status, "reason": r.reason,
            "artifacts": [_artifact_dict(a) for a in artifacts]}


def _model_dict(m: ModelRecord, reprs: List[dict]) -> dict:
    """Logical metadata only - NO absolute host paths leave the server."""
    return {
        "id": m.model_id, "model_id": m.model_id, "name": m.name, "description": m.description,
        "engine_type": m.engine_type, "task": m.task, "framework": m.framework, "version": m.version,
        "status": m.status, "validation_status": m.validation_status, "reason": m.reason,
        "original_filename": m.filename,
        "file_size": next((a["size_bytes"] for r in reprs if r["kind"] == "primary"
                           for a in r["artifacts"]), None),
        "file_extension": (os.path.splitext(m.filename)[1] if m.filename else None),
        "upload_date": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        "uploader_username": m.uploader_username,
        "representations": reprs,
        "meta": m.meta or {},
    }


# ------------------------------------------------------------------ registry writes
def register_model(*, model_id: str, name: str, engine_type: str, filename: str = None,
                   description: str = None, task: str = None, framework: str = None,
                   version: str = None, uploader_id: int = None, uploader_username: str = None,
                   meta: dict = None, status: S = S.STAGING) -> dict:
    """Create the logical model row (default STAGING). Idempotent on model_id."""
    with get_session() as s:
        m = s.execute(select(ModelRecord).where(ModelRecord.model_id == model_id)).scalar_one_or_none()
        if m is None:
            m = ModelRecord(model_id=model_id, name=name, engine_type=engine_type, filename=filename,
                            description=description, task=task, framework=framework, version=version,
                            uploader_id=uploader_id, uploader_username=uploader_username, meta=meta or {},
                            status=status.value, validation_status=V.PENDING.value)
            s.add(m)
        else:
            m.name = name or m.name; m.engine_type = engine_type or m.engine_type
            m.filename = filename or m.filename; m.description = description if description is not None else m.description
            m.task = task or m.task; m.framework = framework or m.framework; m.version = version or m.version
        s.flush()
        return {"row_id": m.id, "model_id": m.model_id}


def register_representation(*, model_id: str, format: str, kind: str = "primary", required: bool = None,
                            precision: str = None, device_family: str = None, runtime: str = None,
                            files: List[dict]) -> dict:
    """Register one representation and its component files (one artifact row per file).
    `files` items: {relative_path, sha256, size_bytes, status, validation_status, reason, fingerprint}.
    Idempotent per relative_path. Primary representations are required by default."""
    if required is None:
        required = (kind == "primary")
    with get_session() as s:
        m = s.execute(select(ModelRecord).where(ModelRecord.model_id == model_id)).scalar_one()
        rep = s.execute(select(ModelRepresentation).where(
            ModelRepresentation.model_id == m.id, ModelRepresentation.format == format,
            ModelRepresentation.kind == kind)).scalar_one_or_none()
        if rep is None:
            rep = ModelRepresentation(model_id=m.id, format=format, kind=kind, required=required,
                                      precision=precision, device_family=device_family, runtime=runtime,
                                      status=S.STAGING.value, validation_status=V.PENDING.value)
            s.add(rep); s.flush()
        comps = []
        all_available = True
        for f in files:
            a = s.execute(select(ModelArtifact).where(ModelArtifact.relative_path == f["relative_path"])).scalar_one_or_none()
            if a is None:
                a = ModelArtifact(model_id=m.id, representation_id=rep.id, relative_path=f["relative_path"])
                s.add(a)
            st = f.get("status", S.AVAILABLE); vs = f.get("validation_status", V.PASSED)
            a.sha256 = f.get("sha256") or None
            a.size_bytes = f.get("size_bytes")
            a.status = st.value if isinstance(st, S) else str(st)
            a.validation_status = vs.value if isinstance(vs, V) else str(vs)
            a.reason = (f.get("reason").value if isinstance(f.get("reason"), Reason) else f.get("reason"))
            fp = f.get("fingerprint") or {}
            for k, v in fp.items():
                setattr(a, k, v)
            if a.status == S.AVAILABLE.value:
                a.last_verified_at = datetime.utcnow()
                comps.append((a.relative_path, a.size_bytes or 0, a.sha256 or ""))
            else:
                all_available = False
        s.flush()
        if files and all_available:
            rep.manifest_sha256 = manifest_sha256(comps)
            rep.status = S.AVAILABLE.value; rep.validation_status = V.PASSED.value; rep.reason = None
            rep.last_verified_at = datetime.utcnow()
        else:
            worst = next((f for f in files if f.get("status") not in (S.AVAILABLE, "AVAILABLE")), None)
            st = worst.get("status", S.MISSING) if worst else S.MISSING
            rep.status = st.value if isinstance(st, S) else str(st)
            rep.validation_status = V.PENDING.value if rep.status == S.MISSING.value else V.FAILED.value
            r = worst.get("reason") if worst else Reason.COMPONENT_MISSING
            rep.reason = r.value if isinstance(r, Reason) else (r or Reason.COMPONENT_MISSING.value)
        s.flush()
        rep_id = rep.id
    recompute_model_status(model_id)
    return {"representation_id": rep_id}


# ------------------------------------------------------------------ THE aggregation rule
def recompute_model_status(model_id: str) -> str:
    """ALL required representations AVAILABLE AND >=1 usable representation => AVAILABLE."""
    with get_session() as s:
        m = s.execute(select(ModelRecord).where(ModelRecord.model_id == model_id)).scalar_one_or_none()
        if m is None:
            return ""
        reps = s.execute(select(ModelRepresentation).where(ModelRepresentation.model_id == m.id)).scalars().all()
        required = [r for r in reps if r.required]
        usable = [r for r in reps if r.status == S.AVAILABLE.value and r.validation_status == V.PASSED.value]
        if not reps:
            new, vs, reason = S.MISSING, V.PENDING, Reason.NO_USABLE_REPRESENTATION
        elif any(r.status != S.AVAILABLE.value for r in required):
            bad = next(r for r in required if r.status != S.AVAILABLE.value)
            new = S.CORRUPT if bad.status == S.CORRUPT.value else (S.FAILED if bad.status == S.FAILED.value else S.MISSING)
            vs = V.FAILED if new is not S.MISSING else V.PENDING
            reason = Reason.REQUIRED_REPRESENTATION_UNAVAILABLE
        elif not usable:
            new, vs, reason = S.MISSING, V.PENDING, Reason.NO_USABLE_REPRESENTATION
        else:
            new, vs, reason = S.AVAILABLE, V.PASSED, None
        # Legal-transition discipline: the derived state is reached ONLY through legal
        # edges. Every non-trivial move goes via VALIDATING (STAGING->VALIDATING->X,
        # CORRUPT->VALIDATING->AVAILABLE, ...); DELETING is terminal and never overridden.
        cur = S(m.status)
        if cur is S.DELETING:
            return m.status
        if cur is not new:
            if new not in {S.VALIDATING} and not can_transition(cur, new):
                cur = transition(cur, S.VALIDATING)      # hop through VALIDATING
            if cur is not new:
                new = transition(cur, new)
        m.status = new.value; m.validation_status = vs.value
        m.reason = reason.value if reason else None
        m.updated_at = datetime.utcnow()
        return m.status


# ------------------------------------------------------------------ reads
def get_model(model_id: str) -> Optional[dict]:
    with get_session() as s:
        m = s.execute(select(ModelRecord).where(ModelRecord.model_id == model_id)).scalar_one_or_none()
        if m is None:
            return None
        reps = s.execute(select(ModelRepresentation).where(ModelRepresentation.model_id == m.id)).scalars().all()
        out = []
        for r in reps:
            arts = s.execute(select(ModelArtifact).where(ModelArtifact.representation_id == r.id)).scalars().all()
            out.append(_repr_dict(r, arts))
        return _model_dict(m, out)


def list_models(*, only_available: bool = False) -> List[dict]:
    with get_session() as s:
        ms = s.execute(select(ModelRecord).order_by(ModelRecord.created_at)).scalars().all()
        ids = [m.model_id for m in ms if (not only_available or m.status == S.AVAILABLE.value)]
    return [get_model(i) for i in ids]


def servable_primary_path(model_id: str) -> Optional[str]:
    """Resolved path of the primary artifact IF it is servable (status/validation/
    fingerprint), else None. Loaders use this - never a stored absolute path."""
    m = get_model(model_id)
    if not m or m["status"] != S.AVAILABLE.value:
        return None
    for r in m["representations"]:
        if r["kind"] != "primary" or r["status"] != S.AVAILABLE.value:
            continue
        for a in r["artifacts"]:
            try:
                path = ap.resolve("models", a["relative_path"])
            except ap.ArtifactPathError:
                return None                          # poisoned/escaping row: refuse
            if not os.path.isfile(path):
                # registered file gone -> AVAILABLE -> VALIDATING -> MISSING (never served)
                return _revalidate_artifact(a["id"], path, model_id)
            with get_session() as s:
                row = s.execute(select(ModelArtifact).where(ModelArtifact.id == a["id"])).scalar_one()
                fp = {k: getattr(row, k) for k in ("verified_size_bytes", "verified_mtime_ns",
                                                   "verified_ctime_ns", "verified_inode", "verified_device")}
            if is_servable(a["status"], a["validation_status"], path, fp):
                return path
            # fingerprint drift -> revalidate now (AVAILABLE -> VALIDATING -> AVAILABLE|CORRUPT|MISSING)
            return _revalidate_artifact(a["id"], path, model_id)
    return None


def _revalidate_artifact(artifact_id: int, path: str, model_id: str) -> Optional[str]:
    with get_session() as s:
        a = s.execute(select(ModelArtifact).where(ModelArtifact.id == artifact_id)).scalar_one()
        a.status = transition(a.status, S.VALIDATING).value
        s.flush()
        if not os.path.isfile(path):
            a.status = transition(a.status, S.MISSING).value; a.reason = Reason.FILE_MISSING.value
            ok = False
        else:
            sha, size = sha256_file(path)
            if sha == a.sha256 and size == a.size_bytes:
                a.status = transition(a.status, S.AVAILABLE).value; a.validation_status = V.PASSED.value
                a.reason = None; a.last_verified_at = datetime.utcnow()
                for k, v in (fingerprint(path) or {}).items():
                    setattr(a, k, v)
                ok = True
            else:
                a.status = transition(a.status, S.CORRUPT).value
                a.validation_status = V.HASH_MISMATCH.value; a.reason = Reason.HASH_MISMATCH.value
                ok = False
        rep = s.execute(select(ModelRepresentation).where(ModelRepresentation.id == a.representation_id)).scalar_one()
        if not ok:
            rep.status = a.status; rep.validation_status = a.validation_status; rep.reason = a.reason
    recompute_model_status(model_id)
    return path if ok else None
