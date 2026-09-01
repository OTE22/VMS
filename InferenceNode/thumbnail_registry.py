"""Pipeline thumbnails as managed artifacts (Phase 12).

relative_path + sha256 + size + status + validation_status + fingerprint - "file exists"
is never proof it is the registered thumbnail. Bytes live under ARTIFACT_ROOT/thumbnails
(reached only via artifact_paths). Creation: staged image -> decode validation ->
sha256/size -> fsync -> atomic promote -> register AVAILABLE. Deletion is coordinated
with pipeline deletion through the managed trash flow (the FK cascade removes only the
row): DELETING -> move JPEG to trash -> delete pipeline row -> commit -> purge; a DB
failure restores the JPEG from trash.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import select

from . import artifact_paths as ap
from .artifact_migration import sha256_file
from .artifact_states import (ArtifactStatus as S, ValidationStatus as V, Reason,
                              fingerprint, fingerprint_matches, transition)
from .auth.db import get_session
from .data_models import Pipeline, PipelineThumbnail

logger = logging.getLogger("InferenceNode.thumbnail_registry")


def relative_path_for(pipeline_id: str) -> str:
    return f"thumbnail_{pipeline_id}.jpg"


def _row(t: PipelineThumbnail) -> dict:
    return {"id": t.id, "pipeline_row_id": t.pipeline_id, "relative_path": t.relative_path,
            "sha256": t.sha256, "size_bytes": t.size_bytes, "status": t.status,
            "validation_status": t.validation_status, "reason": t.reason,
            "last_verified_at": t.last_verified_at.isoformat() if t.last_verified_at else None}


def _pipeline_row_id(s, pipeline_id: str) -> Optional[int]:
    p = s.execute(select(Pipeline).where(Pipeline.pipeline_id == str(pipeline_id))).scalar_one_or_none()
    return None if p is None else p.id


def get_for_pipeline(pipeline_id: str) -> Optional[dict]:
    with get_session() as s:
        rid = _pipeline_row_id(s, pipeline_id)
        if rid is None:
            return None
        t = s.execute(select(PipelineThumbnail).where(PipelineThumbnail.pipeline_id == rid)).scalar_one_or_none()
        return None if t is None else _row(t)


def register_from_staged(pipeline_id: str, staged_path: str) -> Optional[dict]:
    """Creation machine for a freshly written thumbnail at `staged_path` (inside the
    thumbnails .staging area): validate decode -> sha256/size -> promote -> AVAILABLE."""
    rel = relative_path_for(pipeline_id)
    with get_session() as s:
        rid = _pipeline_row_id(s, pipeline_id)
        if rid is None:
            return None
        t = s.execute(select(PipelineThumbnail).where(PipelineThumbnail.pipeline_id == rid)).scalar_one_or_none()
        if t is None:
            t = PipelineThumbnail(pipeline_id=rid, relative_path=rel, status=S.STAGING.value,
                                  validation_status=V.PENDING.value)
            s.add(t)
        else:
            # re-capture: legal path is AVAILABLE/whatever -> VALIDATING
            if t.status != S.STAGING.value:
                t.status = transition(t.status, S.VALIDATING).value if t.status != S.VALIDATING.value else t.status
        s.flush()
    try:
        # decode validation (a JPEG that cv2 cannot read is not a thumbnail)
        try:
            import cv2
            img = cv2.imread(staged_path)
            if img is None:
                raise ValueError("thumbnail does not decode")
        except ImportError:
            with open(staged_path, "rb") as f:
                if f.read(2) != b"\xff\xd8":
                    raise ValueError("thumbnail is not a JPEG")
        sha, size = sha256_file(staged_path)
        final = ap.resolve("thumbnails", rel)
        os.makedirs(os.path.dirname(final), exist_ok=True)
        os.replace(staged_path, final)
        try:
            dfd = os.open(os.path.dirname(final), os.O_RDONLY); os.fsync(dfd); os.close(dfd)
        except (OSError, AttributeError):
            pass
        with get_session() as s:
            rid = _pipeline_row_id(s, pipeline_id)
            t = s.execute(select(PipelineThumbnail).where(PipelineThumbnail.pipeline_id == rid)).scalar_one()
            if t.status == S.STAGING.value:
                t.status = transition(t.status, S.VALIDATING).value
            t.status = transition(t.status, S.AVAILABLE).value
            t.validation_status = V.PASSED.value; t.reason = None
            t.sha256, t.size_bytes = sha, size
            for k, v in (fingerprint(final) or {}).items():
                setattr(t, k, v)
            t.last_verified_at = datetime.utcnow()
            return _row(t)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[THUMBS] registration failed for {pipeline_id}: {e}")
        try:
            os.remove(staged_path)
        except OSError:
            pass
        with get_session() as s:
            rid = _pipeline_row_id(s, pipeline_id)
            t = s.execute(select(PipelineThumbnail).where(PipelineThumbnail.pipeline_id == rid)).scalar_one_or_none()
            if t is not None:
                if t.status not in (S.FAILED.value,):
                    t.status = transition(t.status, S.FAILED).value if t.status in (S.STAGING.value, S.VALIDATING.value) else t.status
                t.validation_status = V.FAILED.value; t.reason = Reason.VALIDATION_FAILED.value
        return None


def servable_path(pipeline_id: str) -> Optional[str]:
    """Resolved path only if AVAILABLE + PASSED + present + unchanged fingerprint."""
    t = get_for_pipeline(pipeline_id)
    if not t or t["status"] != S.AVAILABLE.value or t["validation_status"] != V.PASSED.value:
        return None
    try:
        path = ap.resolve("thumbnails", t["relative_path"])
    except ap.ArtifactPathError:
        return None
    with get_session() as s:
        row = s.execute(select(PipelineThumbnail).where(PipelineThumbnail.id == t["id"])).scalar_one()
        fp = {k: getattr(row, k) for k in ("verified_size_bytes", "verified_mtime_ns", "verified_ctime_ns",
                                           "verified_inode", "verified_device")}
    if not os.path.isfile(path):
        _mark(t["id"], S.MISSING, V.PENDING, Reason.FILE_MISSING); return None
    if not fingerprint_matches(fp, fingerprint(path)):
        sha, size = sha256_file(path)
        if sha == t["sha256"] and size == t["size_bytes"]:
            with get_session() as s:
                row = s.execute(select(PipelineThumbnail).where(PipelineThumbnail.id == t["id"])).scalar_one()
                for k, v in (fingerprint(path) or {}).items():
                    setattr(row, k, v)
            return path
        _mark(t["id"], S.CORRUPT, V.HASH_MISMATCH, Reason.HASH_MISMATCH); return None
    return path


def _mark(thumb_id: int, status: S, vstatus: V, reason: Reason):
    with get_session() as s:
        row = s.execute(select(PipelineThumbnail).where(PipelineThumbnail.id == thumb_id)).scalar_one()
        if row.status == S.AVAILABLE.value:
            row.status = transition(row.status, S.VALIDATING).value
        row.status = transition(row.status, status).value
        row.validation_status = vstatus.value; row.reason = reason.value


# ------------------------------------------------------------------ pipeline delete coordination
def begin_delete(pipeline_id: str) -> Optional[dict]:
    """DELETING + move JPEG to managed trash. Returns {'trash': path, 'final': path} (or
    None when there is no registered thumbnail). Caller then deletes the pipeline row
    (cascade removes the thumbnail row) and calls finish_delete / abort_delete."""
    t = get_for_pipeline(pipeline_id)
    if t is None:
        return None
    with get_session() as s:
        row = s.execute(select(PipelineThumbnail).where(PipelineThumbnail.id == t["id"])).scalar_one()
        if row.status != S.DELETING.value:
            cur = row.status
            if cur not in (S.AVAILABLE.value,):
                # only AVAILABLE has a DELETING edge; route via VALIDATING->AVAILABLE is not
                # legal without verification, so treat non-available thumbs as plain files
                pass
            else:
                row.status = transition(cur, S.DELETING).value
    try:
        final = ap.resolve("thumbnails", t["relative_path"])
    except ap.ArtifactPathError:
        return {"trash": None, "final": None}
    trash = ap.trash_path("thumbnails", t["relative_path"])
    if os.path.exists(final):
        os.makedirs(os.path.dirname(trash), exist_ok=True)
        os.replace(final, trash)
        return {"trash": trash, "final": final}
    return {"trash": None, "final": final}


def finish_delete(handle: Optional[dict]) -> None:
    if handle and handle.get("trash"):
        try:
            os.remove(handle["trash"])
        except OSError:
            pass


def abort_delete(handle: Optional[dict]) -> None:
    """DB deletion failed: restore the JPEG from trash so nothing is lost."""
    if handle and handle.get("trash") and handle.get("final") and os.path.exists(handle["trash"]):
        os.makedirs(os.path.dirname(handle["final"]), exist_ok=True)
        os.replace(handle["trash"], handle["final"])


# ------------------------------------------------------------------ verification
def verify_all() -> Dict[str, list]:
    report = {"available_valid": [], "available_missing": [], "available_hash_mismatch": [],
              "row_no_file": [], "orphan_files": []}
    registered = set()
    with get_session() as s:
        rows = [(_row(t), t.pipeline_id) for t in s.execute(select(PipelineThumbnail)).scalars()]
    for t, _rid in rows:
        registered.add(t["relative_path"])
        try:
            p = ap.resolve("thumbnails", t["relative_path"])
        except ap.ArtifactPathError:
            report["row_no_file"].append(t["relative_path"]); continue
        if t["status"] == S.AVAILABLE.value:
            if not os.path.isfile(p):
                report["available_missing"].append(t["relative_path"])
            else:
                sha, size = sha256_file(p)
                (report["available_valid"] if (sha == t["sha256"] and size == t["size_bytes"])
                 else report["available_hash_mismatch"]).append(t["relative_path"])
    root = ap.kind_root("thumbnails")
    for f in os.listdir(root) if os.path.isdir(root) else []:
        if f in (ap.STAGING_DIR, ap.TRASH_DIR):
            continue
        if f not in registered:
            report["orphan_files"].append(f)     # reported, never deleted here
    return report
