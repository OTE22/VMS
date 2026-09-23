"""Media assets registry (Phase 12): PostgreSQL tracks uploaded videos/images while the
bytes stay under ARTIFACT_ROOT/media (the MediaLibrary root after cutover).

`frame_source.config.relative_source` REMAINS the canonical pipeline reference (approved
Builder remediation); a media_asset_id FK is a designed future migration. Here we make
sure every file under the media root is registered with sha256/size/status so
"path exists" is never the only evidence, and new uploads go through the creation
machine (STAGING -> stage bytes -> VALIDATING -> hash -> promote -> AVAILABLE).
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import select

from . import artifact_paths as ap
from .artifact_migration import MigrationReport, sha256_file, stage_copy_verify_promote
from .artifact_states import ArtifactStatus as S, ValidationStatus as V, Reason, fingerprint, transition
from .auth.db import get_session
from .data_models import MediaAsset

logger = logging.getLogger("InferenceNode.media_registry")

MEDIA_EXTS = (".mp4", ".avi", ".mov", ".mkv", ".webm", ".jpg", ".jpeg", ".png", ".bmp")


def _row(m: MediaAsset) -> dict:
    return {"media_id": m.media_id, "relative_path": m.relative_path, "original_filename": m.original_filename,
            "media_type": m.media_type, "sha256": m.sha256, "size_bytes": m.size_bytes, "status": m.status,
            "validation_status": m.validation_status, "reason": m.reason,
            "created_at": m.created_at.isoformat() if m.created_at else None}


def get_by_path(relative_path: str) -> Optional[dict]:
    with get_session() as s:
        m = s.execute(select(MediaAsset).where(MediaAsset.relative_path == relative_path)).scalar_one_or_none()
        return None if m is None else _row(m)


def list_assets() -> List[dict]:
    with get_session() as s:
        return [_row(m) for m in s.execute(select(MediaAsset).order_by(MediaAsset.created_at)).scalars()]


def register_existing(relative_path: str, *, original_filename: str = None, created_by: int = None) -> dict:
    """Register a file that already sits under ARTIFACT_ROOT/media (upload path after
    promotion, or the physical migration). Hashes the bytes; AVAILABLE only if present."""
    path = ap.resolve("media", relative_path)
    with get_session() as s:
        m = s.execute(select(MediaAsset).where(MediaAsset.relative_path == relative_path)).scalar_one_or_none()
        if m is None:
            m = MediaAsset(media_id=str(uuid.uuid4()), relative_path=relative_path,
                           original_filename=original_filename or os.path.basename(relative_path),
                           media_type=os.path.splitext(relative_path)[1].lstrip(".").lower() or None,
                           status=S.STAGING.value, validation_status=V.PENDING.value, created_by=created_by)
            s.add(m); s.flush()
        if not os.path.isfile(path):
            if m.status in (S.STAGING.value,):
                m.status = transition(m.status, S.VALIDATING).value
            if m.status == S.VALIDATING.value:
                m.status = transition(m.status, S.MISSING).value
            m.reason = Reason.FILE_MISSING.value
            return _row(m)
        sha, size = sha256_file(path)
        if m.status == S.STAGING.value:
            m.status = transition(m.status, S.VALIDATING).value
        if m.status != S.AVAILABLE.value:
            m.status = transition(m.status, S.AVAILABLE).value if m.status == S.VALIDATING.value else m.status
        m.sha256, m.size_bytes = sha, size
        m.validation_status = V.PASSED.value; m.reason = None
        for k, v in (fingerprint(path) or {}).items():
            setattr(m, k, v)
        m.last_verified_at = datetime.utcnow()
        return _row(m)


class MediaIngestError(Exception):
    def __init__(self, message: str, *, code: str = "MEDIA_INGEST_FAILED", http_status: int = 400):
        super().__init__(message); self.code = code; self.http_status = http_status


UPLOAD_EXTS = {'.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg'}


def ingest_upload(file_storage, *, original_filename: str, created_by: int = None,
                  timestamp: str = None, verify_video: bool = False) -> dict:
    """Creation state machine (section 3a) for an uploaded media file:
       INSERT media_assets STAGING -> commit -> save bytes into media/.staging ->
       VALIDATING (extension/format, non-empty) -> sha256+size -> fsync -> atomic promote
       into ARTIFACT_ROOT/media/<safe name> -> AVAILABLE + PASSED + fingerprint.
    Any failure before AVAILABLE => FAILED (staged bytes removed); nothing is ever served
    from STAGING. Returns the registered row (relative_path is the canonical reference)."""
    from datetime import datetime as _dt
    from werkzeug.utils import secure_filename
    ext = os.path.splitext(original_filename or "")[1].lower()
    if ext not in UPLOAD_EXTS:
        raise MediaIngestError(f"Invalid file type. Allowed types: {', '.join(sorted(UPLOAD_EXTS))}",
                               code="MEDIA_TYPE_INVALID")
    safe = secure_filename(original_filename) or f"upload{ext}"
    stamp = timestamp or _dt.now().strftime('%Y%m%d_%H%M%S')
    rel = f"{stamp}_{uuid.uuid4().hex}_{safe}"
    final = ap.resolve('media', rel)
    staged = ap.staging_path("media", rel)
    os.makedirs(os.path.dirname(staged), exist_ok=True)
    media_id = str(uuid.uuid4())
    with get_session() as s:                                   # 1. STAGING row committed first
        s.add(MediaAsset(media_id=media_id, relative_path=rel, original_filename=original_filename,
                         media_type=ext.lstrip("."), status=S.STAGING.value,
                         validation_status=V.PENDING.value, created_by=created_by))

    def _fail(reason: Reason, vstatus: V, msg: str, code: str):
        try:
            if os.path.exists(staged):
                os.remove(staged)
        except OSError:
            pass
        with get_session() as s:
            m = s.execute(select(MediaAsset).where(MediaAsset.media_id == media_id)).scalar_one()
            if m.status == S.STAGING.value:
                m.status = transition(m.status, S.VALIDATING).value
            m.status = transition(m.status, S.FAILED).value
            m.validation_status = vstatus.value; m.reason = reason.value
        raise MediaIngestError(msg, code=code)

    try:
        file_storage.save(staged)                              # 2. bytes -> staging
        with open(staged, "rb+") as f:
            f.flush(); os.fsync(f.fileno())
    except Exception as e:  # noqa: BLE001
        _fail(Reason.COPY_FAILED, V.FAILED, f"Upload could not be stored: {e.__class__.__name__}", "MEDIA_STORE_FAILED")
    with get_session() as s:                                   # 3. VALIDATING
        m = s.execute(select(MediaAsset).where(MediaAsset.media_id == media_id)).scalar_one()
        m.status = transition(m.status, S.VALIDATING).value
    if os.path.getsize(staged) == 0:
        _fail(Reason.VALIDATION_FAILED, V.FORMAT_INVALID, "Uploaded file is empty", "MEDIA_EMPTY")
    if verify_video:
        import cv2
        capture = cv2.VideoCapture(staged)
        try:
            opened, frame = capture.read() if capture.isOpened() else (False, None)
        finally:
            capture.release()
        if not opened or frame is None:
            _fail(Reason.VALIDATION_FAILED, V.FORMAT_INVALID, 'Video could not be decoded; upload a supported playable video', 'MEDIA_FORMAT_INVALID')
    sha, size = sha256_file(staged)                            # 4. hash staged bytes
    try:
        os.makedirs(os.path.dirname(final), exist_ok=True)
        os.replace(staged, final)                              # 5. atomic promote
        try:
            dfd = os.open(os.path.dirname(final), os.O_RDONLY); os.fsync(dfd); os.close(dfd)
        except (OSError, AttributeError):
            pass
    except Exception as e:  # noqa: BLE001
        _fail(Reason.PROMOTE_INTERRUPTED, V.FAILED, f"Upload could not be promoted: {e.__class__.__name__}", "MEDIA_PROMOTE_FAILED")
    with get_session() as s:                                   # 6. AVAILABLE
        m = s.execute(select(MediaAsset).where(MediaAsset.media_id == media_id)).scalar_one()
        m.status = transition(m.status, S.AVAILABLE).value
        m.validation_status = V.PASSED.value; m.reason = None
        m.sha256, m.size_bytes = sha, size
        for k, v in (fingerprint(final) or {}).items():
            setattr(m, k, v)
        m.last_verified_at = datetime.utcnow()
        return _row(m)


def servable_path(relative_path: str) -> Optional[str]:
    """Resolved path only when the registered asset is AVAILABLE + PASSED + present with
    an unchanged fingerprint (changed fingerprint => re-hash; mismatch => CORRUPT)."""
    row = get_by_path(relative_path)
    if not row or row["status"] != S.AVAILABLE.value or row["validation_status"] != V.PASSED.value:
        return None
    try:
        path = ap.resolve("media", relative_path)
    except ap.ArtifactPathError:
        return None
    with get_session() as s:
        m = s.execute(select(MediaAsset).where(MediaAsset.media_id == row["media_id"])).scalar_one()
        fp = {k: getattr(m, k) for k in ("verified_size_bytes", "verified_mtime_ns", "verified_ctime_ns",
                                         "verified_inode", "verified_device")}
    if not os.path.isfile(path):
        _mark(row["media_id"], S.MISSING, V.PENDING, Reason.FILE_MISSING); return None
    from .artifact_states import fingerprint_matches
    if not fingerprint_matches(fp, fingerprint(path)):
        sha, size = sha256_file(path)
        if sha == row["sha256"] and size == row["size_bytes"]:
            with get_session() as s:
                m = s.execute(select(MediaAsset).where(MediaAsset.media_id == row["media_id"])).scalar_one()
                for k, v in (fingerprint(path) or {}).items():
                    setattr(m, k, v)
            return path
        _mark(row["media_id"], S.CORRUPT, V.HASH_MISMATCH, Reason.HASH_MISMATCH); return None
    return path


def _mark(media_id: str, status: S, vstatus: V, reason: Reason):
    with get_session() as s:
        m = s.execute(select(MediaAsset).where(MediaAsset.media_id == media_id)).scalar_one()
        if m.status == S.AVAILABLE.value:
            m.status = transition(m.status, S.VALIDATING).value
        m.status = transition(m.status, status).value
        m.validation_status = vstatus.value; m.reason = reason.value


def verify_all() -> Dict[str, list]:
    """Reconciliation report (never deletes): AVAILABLE valid/missing/hash-mismatch,
    non-available rows, unregistered files under the media root."""
    report = {"available_valid": [], "available_missing": [], "available_hash_mismatch": [],
              "not_available": [], "orphan_files": []}
    registered = set()
    for row in list_assets():
        registered.add(row["relative_path"])
        try:
            p = ap.resolve("media", row["relative_path"])
        except ap.ArtifactPathError:
            report["not_available"].append(row["relative_path"]); continue
        if row["status"] != S.AVAILABLE.value:
            report["not_available"].append(row["relative_path"]); continue
        if not os.path.isfile(p):
            report["available_missing"].append(row["relative_path"]); continue
        sha, size = sha256_file(p)
        (report["available_valid"] if (sha == row["sha256"] and size == row["size_bytes"])
         else report["available_hash_mismatch"]).append(row["relative_path"])
    root = ap.kind_root("media")
    if os.path.isdir(root):
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for f in files:
                rel = os.path.relpath(os.path.join(dirpath, f), root).replace("\\", "/")
                if rel not in registered and f.lower().endswith(MEDIA_EXTS):
                    report["orphan_files"].append(rel)
    return report


def referencing_pipelines(relative_path: str) -> List[dict]:
    """Pipelines whose frame source points at this media file.

    Media is the ONE artifact class the database cannot protect: models are guarded by
    `pipelines.model_id` with ON DELETE RESTRICT, but a pipeline references media by the
    STRING `frame_source.config.relative_source` inside its config JSON, so there is no FK
    to refuse the delete. This check is therefore not a convenience - it is the only thing
    standing between a delete and a silently broken pipeline.

    Legacy pipelines that still carry an absolute `source` path are matched on basename,
    because those resolve through the same file at runtime (media_library's legacy
    compatibility path).
    """
    from .pipeline_repository import repository
    from .media_library import is_network_source
    base = os.path.basename(relative_path)
    out = []
    for r in repository.list(is_admin=True):
        fs = (r.get("config") or {}).get("frame_source") or {}
        cfg = fs.get("config") or {}
        rel = cfg.get("relative_source")
        src = cfg.get("source")
        if rel:
            hit = os.path.normpath(rel.replace('\\', '/')) == os.path.normpath(relative_path)
        else:
            hit = (fs.get('capture_type') in ('video_file', 'video') and isinstance(src, str)
                   and not is_network_source(src) and os.path.basename(src.replace('\\', '/')) == base)
        if hit:
            out.append({"pipeline_id": r["pipeline_id"], "name": r.get("name"), "status": r.get("status")})
    return out


def delete_media(media_id: str, *, force: bool = False) -> dict:
    from .media_guard import lock
    with get_session() as session:
        lock(session)
        return _delete_media_locked(media_id, force=force)


def _delete_media_locked(media_id: str, *, force: bool = False) -> dict:
    """Retire a media asset: bytes to managed trash, then the row, then purge.

    Mirrors the model/engine deletion machine (section 3e of the readiness report):
        AVAILABLE -> DELETING -> move file to <media>/.trash -> delete row -> COMMIT -> purge
    A failure after the move restores the file, so the outcome is always either
    "deleted" or "the asset is intact" - never a row pointing at bytes that are gone.

    Outcomes: deleted | not_found | referenced | failed
    `force=True` deletes despite references; the caller is responsible for that decision
    and the referencing pipelines are still reported back.
    """
    with get_session() as s:
        m = s.execute(select(MediaAsset).where(MediaAsset.media_id == media_id)).scalar_one_or_none()
        if m is None:
            return {"outcome": "not_found", "media_id": media_id}
        rel, status, reason = m.relative_path, m.status, m.reason

    def restore_status():
        with get_session() as session:
            row = session.execute(select(MediaAsset).where(MediaAsset.media_id == media_id)).scalar_one_or_none()
            if row is not None:
                row.status, row.reason = status, reason

    users = referencing_pipelines(rel)
    if users and not force:
        return {"outcome": "referenced", "media_id": media_id, "relative_path": rel,
                "pipelines": users}

    # 1. mark DELETING so nothing serves it while the bytes are moving
    with get_session() as s:
        m = s.execute(select(MediaAsset).where(MediaAsset.media_id == media_id)).scalar_one()
        if m.status == S.AVAILABLE.value:
            m.status = transition(m.status, S.DELETING).value
            m.reason = Reason.DELETE_INTERRUPTED.value   # cleared on success; a crash leaves this visible

    # 2. move the bytes to managed trash (recoverable until the row is gone)
    moved = None
    try:
        final = ap.resolve("media", rel)
        if os.path.isfile(final):
            trash = ap.trash_path("media", rel)
            os.makedirs(os.path.dirname(trash), exist_ok=True)
            os.replace(final, trash)
            moved = (trash, final)
    except Exception as e:  # noqa: BLE001
        restore_status()
        logger.error(f"[MEDIA] could not stage {rel} for deletion: {e}")
        return {"outcome": "failed", "media_id": media_id, "relative_path": rel,
                "error": f"{e.__class__.__name__}: {e}"}

    # 3. remove the row; restore the bytes if that fails
    try:
        with get_session() as s:
            m = s.execute(select(MediaAsset).where(MediaAsset.media_id == media_id)).scalar_one()
            s.delete(m)
    except Exception as e:  # noqa: BLE001
        if moved and os.path.exists(moved[0]):
            os.makedirs(os.path.dirname(moved[1]), exist_ok=True)
            os.replace(moved[0], moved[1])
        restore_status()
        logger.error(f"[MEDIA] row delete failed for {rel}, file restored: {e}")
        return {"outcome": "failed", "media_id": media_id, "relative_path": rel,
                "error": f"{e.__class__.__name__}: {e}"}

    # 4. only now purge the trash copy
    if moved:
        try:
            os.remove(moved[0])
        except OSError:
            pass
    logger.info(f"[MEDIA] deleted {rel} (was {status}){' despite references' if users else ''}")
    return {"outcome": "deleted", "media_id": media_id, "relative_path": rel,
            "was_referenced_by": users}


def migrate_legacy_media(legacy_media_dir: str, report: Optional[MigrationReport] = None) -> MigrationReport:
    """Physical migration of the legacy media dir into ARTIFACT_ROOT/media (stage/fsync/
    verify/promote/register; legacy retained). Relative paths are preserved so existing
    `relative_source` values keep resolving after the MediaLibrary root moves."""
    report = report or MigrationReport()
    if not os.path.isdir(legacy_media_dir):
        return report
    for dirpath, _dirs, files in os.walk(legacy_media_dir):
        for f in files:
            if not f.lower().endswith(MEDIA_EXTS):
                report.unregistered.append(os.path.join(dirpath, f)); continue
            src = os.path.join(dirpath, f)
            rel = os.path.relpath(src, legacy_media_dir).replace("\\", "/")
            report.discovered += 1
            try:
                mf = stage_copy_verify_promote("media", src, rel)
                if mf.status is S.AVAILABLE:
                    register_existing(rel, original_filename=f)
                    report.available += 1
                elif mf.status is S.MISSING:
                    report.missing += 1
                else:
                    report.failed += 1
                report.processed += 1
            except Exception as e:  # noqa: BLE001
                logger.error(f"[MIGRATE] media {rel}: {e}")
                report.failed += 1
    return report


def migrate_legacy_thumbnails(legacy_thumbs_dir: str, report: Optional[MigrationReport] = None) -> MigrationReport:
    """Move thumbnail_<pipeline_id>.jpg files under ARTIFACT_ROOT/thumbnails and register
    ONLY those whose pipeline row exists; the rest are reported as orphans (never deleted)."""
    from . import thumbnail_registry as thumbs
    from .data_models import Pipeline
    report = report or MigrationReport()
    if not os.path.isdir(legacy_thumbs_dir):
        return report
    with get_session() as s:
        live = {p.pipeline_id for p in s.execute(select(Pipeline)).scalars()}
    for f in os.listdir(legacy_thumbs_dir):
        if not (f.startswith("thumbnail_") and f.endswith(".jpg")):
            continue
        report.discovered += 1
        pid = f[len("thumbnail_"):-len(".jpg")]
        src = os.path.join(legacy_thumbs_dir, f)
        if pid not in live:
            report.unregistered.append(src)          # orphan of a deleted pipeline: reported only
            report.processed += 1
            continue
        try:
            mf = stage_copy_verify_promote("thumbnails", src, f)
            if mf.status is S.AVAILABLE:
                # register via the thumbnail creation machine from the promoted file
                staged = ap.staging_path("thumbnails", f)
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                final = ap.resolve("thumbnails", f)
                # register_from_staged expects a staged path: copy final -> staging then promote
                import shutil
                shutil.copy2(final, staged)
                thumbs.register_from_staged(pid, staged)
                report.available += 1
            else:
                report.failed += 1
            report.processed += 1
        except Exception as e:  # noqa: BLE001
            logger.error(f"[MIGRATE] thumbnail {f}: {e}")
            report.failed += 1
    return report
