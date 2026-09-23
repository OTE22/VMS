import os
import ast
import json
import shutil
import hashlib
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger("InferenceNode.model_repo")

# NOTE (Phase 9 cutover): models_metadata.json is NO LONGER an authoritative runtime
# source. It is read exactly once by registry_migration as the migration source and is
# retained read-only for rollback. All reads/writes go through PostgreSQL (model_registry)
# and ARTIFACT_ROOT/models (artifact_paths).

UNSAFE_LABELS_REASON = "Labels could not be read safely from this model."


def read_model_labels(repo: "ModelRepository", model_id: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Best-effort, SAFE read of a stored model's class labels.

    Returns (labels, reason). `labels` is None whenever the labels cannot be
    obtained without doing something unsafe - callers must report that honestly
    rather than inventing names.

    Safety rules, deliberately strict:
      - the model is resolved ONLY by id through the repository. There is no code
        path that accepts a caller-supplied filesystem path, so traversal is not
        expressible: an unknown id is simply "not found".
      - the resolved path must still live inside the repository's models dir.
        This is defence in depth against a poisoned metadata entry.
      - ONNX labels are read from graph metadata; the graph is never run.
      - .pt inspection uses the same loader the runtime already trusts for
        registered models, and never moves the model onto a GPU.
      - any other extension, any missing optional dependency, and any exception
        degrade to (None, reason). Nothing here is allowed to raise.

    Raises KeyError if the model id is not in the repository.
    """
    meta = repo.get_model_metadata(model_id)
    if not meta:
        raise KeyError(model_id)

    try:
        model_path = repo.get_model_path(model_id)
        if not model_path or not os.path.isfile(model_path):
            return None, UNSAFE_LABELS_REASON

        models_dir = os.path.abspath(repo.models_dir)
        resolved = os.path.abspath(os.path.realpath(model_path))
        if os.path.commonpath([resolved, models_dir]) != models_dir:
            logger.warning("Refusing to read labels from a path outside the model repository")
            return None, UNSAFE_LABELS_REASON

        ext = (meta.get('file_extension') or os.path.splitext(resolved)[1] or '').lower()
        if ext == '.onnx':
            labels = _labels_from_onnx(resolved)
        elif ext == '.pt':
            labels = _labels_from_ultralytics(resolved)
        else:
            labels = None
        return (labels, None) if labels else (None, UNSAFE_LABELS_REASON)
    except Exception as e:
        logger.debug(f"Label read unavailable: {e.__class__.__name__}")
        return None, UNSAFE_LABELS_REASON


def _coerce_labels(names) -> Optional[Dict[str, str]]:
    if isinstance(names, dict):
        out = {str(k): str(v) for k, v in names.items()}
    elif isinstance(names, (list, tuple)):
        out = {str(i): str(v) for i, v in enumerate(names)}
    else:
        return None
    return out or None


def _labels_from_onnx(path: str) -> Optional[Dict[str, str]]:
    """Metadata-only read - loads the protobuf, never executes the graph."""
    try:
        import onnx
    except ImportError:
        return None
    try:
        model = onnx.load(path, load_external_data=False)
        for prop in model.metadata_props:
            if prop.key in ('names', 'classes', 'class_names'):
                return _coerce_labels(ast.literal_eval(prop.value))
    except Exception as e:
        logger.debug(f"ONNX metadata read failed: {e.__class__.__name__}")
    return None


def _labels_from_ultralytics(path: str) -> Optional[Dict[str, str]]:
    """Read `names` from a registered .pt via the loader the runtime already
    uses. CPU only - no device is requested, so nothing lands on a GPU."""
    try:
        from ultralytics import YOLO
    except ImportError:
        return None
    try:
        return _coerce_labels(getattr(YOLO(path), 'names', None))
    except Exception as e:
        logger.debug(f"Ultralytics label read failed: {e.__class__.__name__}")
    return None


class ModelRepository:
    """PostgreSQL-backed model repository (Phase 9 cutover).

    Same method contract as before (store_model / get_model_path / get_model_metadata /
    list_models / delete_model / get_storage_stats) so every existing caller keeps
    working, but:

      * PostgreSQL (model_registry) is the AUTHORITATIVE registry - reads never touch
        models_metadata.json again; that file is a one-time migration source, retained
        read-only for rollback.
      * bytes live under ARTIFACT_ROOT/models/<model_id>/... and are reached ONLY through
        artifact_paths.resolve(); get_model_path() returns a path only when the primary
        artifact is servable (AVAILABLE + PASSED + present + unchanged fingerprint).
      * store_model() is the explicit creation state machine: INSERT STAGING -> stage
        bytes -> VALIDATING -> sha256/size -> fsync -> atomic promote -> dir fsync ->
        AVAILABLE. Failure anywhere -> FAILED, staged bytes removed, nothing served.
      * delete_model() is the batch-safe deletion machine: DELETING -> ALL artifacts to
        managed trash -> rows removed -> purge; partial failure restores or leaves
        DELETING with everything recoverable - never AVAILABLE + missing.
    """

    def __init__(self, repo_path: str, *, auto_migrate: bool = True):
        self.repo_path = repo_path                                # legacy root (migration source)
        self.legacy_models_dir = os.path.join(repo_path, 'models')
        self.metadata_file = os.path.join(repo_path, 'models_metadata.json')
        from . import artifact_paths as ap
        ap.ensure_layout()
        self.models_dir = ap.kind_root('models')                  # artifact root for models
        if auto_migrate:
            self._migrate_legacy_once()

    # ------------------------------------------------------------ one-time migration
    def _migrate_legacy_once(self):
        try:
            from . import app_state
            from .registry_migration import migrate_models_registry, MODELS_MARKER
            if app_state.get_state(MODELS_MARKER) == app_state.STATE_COMPLETED:
                return
            if not os.path.isfile(self.metadata_file):
                app_state.set_state(MODELS_MARKER, app_state.STATE_COMPLETED)   # nothing to migrate
                return
            report = migrate_models_registry(self.metadata_file, self.legacy_models_dir)
            logger.info(f"[MODELS] legacy registry migration: {report.as_dict()}")
        except Exception as e:  # noqa: BLE001 - never block startup; reported, rerun resumes
            logger.error(f"[MODELS] legacy registry migration failed (will retry next start): {e}")

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _safe(name: str) -> str:
        return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)

    def _generate_model_id(self, filename: str, file_content: bytes) -> str:
        """Stable logical id: <stem>_<md5[:8]> (unchanged from the legacy scheme so
        existing pipeline references keep matching)."""
        content_hash = hashlib.md5(file_content).hexdigest()[:8]
        base_name = os.path.splitext(filename)[0]
        return f"{base_name}_{content_hash}"

    # ------------------------------------------------------------ CREATE (state machine)
    def store_model(self, temp_file_path: str, original_filename: str, engine_type: str,
                    description: str = "", name: str = "", *, uploader_id=None,
                    uploader_username=None, model_id=None) -> str:
        from .model_uploads import model_ingest_lock, ModelConflictError, ModelInputError
        from . import model_registry as reg
        from .artifact_migration import sha256_file
        digest = hashlib.md5()
        with open(temp_file_path, 'rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        if not os.path.getsize(temp_file_path):
            raise ModelInputError('The model file is empty')
        identity = model_id or f"{os.path.splitext(original_filename)[0]}_{digest.hexdigest()[:8]}"
        with model_ingest_lock(identity):
            existing = reg.get_model(identity)
            if existing:
                if existing['engine_type'] != engine_type:
                    raise ModelConflictError('These model bytes are already registered for another engine')
                if existing['status'] == 'AVAILABLE':
                    path = self.get_model_path(identity)
                    if not path or sha256_file(path) != sha256_file(temp_file_path):
                        raise ModelConflictError('Existing model differs or is unavailable; refusing to overwrite it')
                    return identity  # Idempotent: preserve name, description and uploader.
                raise ModelConflictError('A previous upload exists but is unavailable. Remove it before retrying.')
            return self._store_model_unlocked(temp_file_path, original_filename, engine_type,
                description, name, uploader_id=uploader_id, uploader_username=uploader_username,
                model_id=identity)

    def _store_model_unlocked(self, temp_file_path: str, original_filename: str, engine_type: str,
                    description: str = "", name: str = "", *, uploader_id=None,
                    uploader_username=None, model_id=None) -> str:
        from . import artifact_paths as ap
        from . import model_registry as reg
        from .artifact_migration import sha256_file
        from .artifact_states import ArtifactStatus as S, ValidationStatus as V, Reason, fingerprint
        from .auth.db import get_session
        from .data_models import ModelArtifact, ModelRecord, ModelRepresentation
        from sqlalchemy import select

        with open(temp_file_path, 'rb') as f:
            file_content = f.read()
        model_id = model_id or self._generate_model_id(original_filename, file_content)
        ext = os.path.splitext(original_filename)[1].lower()
        fmt = (ext.lstrip('.') or 'bin').lower()
        stored_filename = f"{model_id}{ext}"
        rel = f"{self._safe(model_id)}/{self._safe(stored_filename)}"
        display_name = name.strip() if name and name.strip() else os.path.splitext(original_filename)[0]

        # 1. registry rows STAGING -> COMMIT (nothing served yet)
        reg.register_model(model_id=model_id, name=display_name, engine_type=engine_type,
                           filename=original_filename, description=description or None,
                           framework=engine_type, uploader_id=uploader_id,
                           uploader_username=uploader_username, status=S.STAGING)
        reg.register_representation(model_id=model_id, format=fmt, kind="primary", required=True,
                                    files=[{"relative_path": rel, "status": S.STAGING,
                                            "validation_status": V.PENDING}])
        try:
            # 2. stage bytes  3. VALIDATING (format check)  4. sha256/size + fsync
            staged = ap.staging_path("models", rel)
            os.makedirs(os.path.dirname(staged), exist_ok=True)
            with open(staged, 'wb') as out:
                out.write(file_content); out.flush(); os.fsync(out.fileno())
            self._set_artifact_state(rel, S.VALIDATING, V.PENDING, None)
            if fmt not in ("pt", "zip", "onnx", "engine", "xml", "bin", "tflite", "pb"):
                raise ValueError(f"unsupported model format .{fmt}")
            sha, size = sha256_file(staged)
            if size != len(file_content):
                raise ValueError("size mismatch after staging")
            # 5. atomic promote + dir fsync
            final = ap.resolve("models", rel)
            os.makedirs(os.path.dirname(final), exist_ok=True)
            os.replace(staged, final)
            try:
                dfd = os.open(os.path.dirname(final), os.O_RDONLY); os.fsync(dfd); os.close(dfd)
            except (OSError, AttributeError):
                pass
            # 6. VALIDATING -> AVAILABLE + PASSED with fingerprint  -> COMMIT
            self._set_artifact_state(rel, S.AVAILABLE, V.PASSED, None, sha=sha, size=size,
                                     fp=fingerprint(final))
            reg.recompute_model_status(model_id)
            return model_id
        except Exception as e:  # noqa: BLE001
            self._set_artifact_state(rel, S.FAILED, V.FAILED, Reason.VALIDATION_FAILED)
            try:
                staged = ap.staging_path("models", rel)
                if os.path.exists(staged):
                    os.remove(staged)
            except Exception:
                pass
            reg.recompute_model_status(model_id)
            raise Exception(f"Failed to store model: {e}")

    def _set_artifact_state(self, rel, status, vstatus, reason, *, sha=None, size=None, fp=None):
        from .artifact_states import transition
        from .auth.db import get_session
        from .data_models import ModelArtifact, ModelRepresentation
        from sqlalchemy import select
        with get_session() as s:
            a = s.execute(select(ModelArtifact).where(ModelArtifact.relative_path == rel)).scalar_one()
            a.status = transition(a.status, status).value if a.status != status.value else a.status
            a.validation_status = vstatus.value
            a.reason = reason.value if reason else None
            if sha is not None:
                a.sha256 = sha
            if size is not None:
                a.size_bytes = size
            for k, v in (fp or {}).items():
                setattr(a, k, v)
            if status.value == "AVAILABLE":
                a.last_verified_at = datetime.utcnow()
            rep = s.execute(select(ModelRepresentation).where(ModelRepresentation.id == a.representation_id)).scalar_one()
            rep.status = a.status; rep.validation_status = a.validation_status; rep.reason = a.reason
            if status.value == "AVAILABLE":
                from .artifact_migration import manifest_sha256
                rep.manifest_sha256 = manifest_sha256([(a.relative_path, a.size_bytes or 0, a.sha256 or "")])
                rep.last_verified_at = datetime.utcnow()

    # ------------------------------------------------------------ READS (PostgreSQL only)
    def get_model_path(self, model_id: str) -> Optional[str]:
        """Resolved path of the primary artifact ONLY if servable; else None."""
        from . import model_registry as reg
        return reg.servable_primary_path(model_id)

    def get_model_metadata(self, model_id: str) -> Optional[Dict[str, Any]]:
        from . import model_registry as reg
        return reg.get_model(model_id)

    def list_models(self) -> Dict[str, Any]:
        from . import model_registry as reg
        return {m["model_id"]: m for m in reg.list_models()}

    def get_storage_stats(self) -> Dict[str, Any]:
        models = self.list_models()
        total_size = 0
        engine_counts: Dict[str, int] = {}
        for m in models.values():
            for r in m.get("representations", []):
                for a in r.get("artifacts", []):
                    total_size += int(a.get("size_bytes") or 0)
            et = m.get("engine_type") or "unknown"
            engine_counts[et] = engine_counts.get(et, 0) + 1
        available = sum(1 for m in models.values() if m.get("status") == "AVAILABLE")
        return {
            'total_models': len(models),                 # registered (PostgreSQL rows)
            'available_models': available,               # AVAILABLE + PASSED per the registry
            'total_size_bytes': total_size,
            'total_size_mb': round(total_size / (1024 * 1024), 2),
            'engine_counts': engine_counts,
            # logical location only - never an absolute host path
            'repository_path': 'ARTIFACT_ROOT/models',
        }

    # ------------------------------------------------------------ DELETE (batch-safe)
    def delete_model(self, model_id: str) -> bool:
        from .model_uploads import model_ingest_lock
        with model_ingest_lock(model_id):
            return self._delete_model_unlocked(model_id)

    def _delete_model_unlocked(self, model_id: str) -> bool:
        """DELETING -> move EVERY registered artifact to managed trash -> only after ALL
        moves succeed remove the rows -> purge trash. Partial failure: restore moved files
        (hash-verified) and go back to AVAILABLE, else stay DELETING with everything
        recoverable. Never AVAILABLE + a registered file missing."""
        from . import artifact_paths as ap
        from . import model_registry as reg
        from .artifact_migration import sha256_file
        from .artifact_states import ArtifactStatus as S, Reason
        from .auth.db import get_session
        from .data_models import ModelArtifact, ModelRecord, ModelRepresentation
        from sqlalchemy import select

        m = reg.get_model(model_id)
        if not m:
            return False
        rel_paths = [a["relative_path"] for r in m["representations"] for a in r["artifacts"]]

        # 1. DELETING -> COMMIT
        with get_session() as s:
            row = s.execute(select(ModelRecord).where(ModelRecord.model_id == model_id)).scalar_one()
            row.status = S.DELETING.value
            for a in s.execute(select(ModelArtifact).where(ModelArtifact.model_id == row.id)).scalars():
                if a.status != S.DELETING.value:
                    a.status = S.DELETING.value
            for r in s.execute(select(ModelRepresentation).where(ModelRepresentation.model_id == row.id)).scalars():
                r.status = S.DELETING.value

        # 2. move ALL artifacts to trash (fsync); track what moved
        moved = []   # (final_path, trash_path)
        try:
            for rel in rel_paths:
                final = ap.resolve("models", rel)
                if not os.path.exists(final):
                    continue                                   # already gone: nothing to trash
                trash = ap.trash_path("models", rel)
                os.makedirs(os.path.dirname(trash), exist_ok=True)
                os.replace(final, trash)
                moved.append((final, trash))
        except Exception as e:  # noqa: BLE001 - restore what moved, back to AVAILABLE
            logger.error(f"[MODELS] delete of {model_id} interrupted at artifact move: {e}")
            ok = True
            for final, trash in moved:
                try:
                    os.makedirs(os.path.dirname(final), exist_ok=True)
                    os.replace(trash, final)
                except Exception:
                    ok = False
            with get_session() as s:
                row = s.execute(select(ModelRecord).where(ModelRecord.model_id == model_id)).scalar_one()
                # legal recovery path DELETING has no out-edge except removal, so this is the
                # one documented exception: a fully-restored delete returns the rows to their
                # prior AVAILABLE state; an unrestorable one stays DELETING + reason.
                if ok:
                    for a in s.execute(select(ModelArtifact).where(ModelArtifact.model_id == row.id)).scalars():
                        a.status = S.AVAILABLE.value
                    for r in s.execute(select(ModelRepresentation).where(ModelRepresentation.model_id == row.id)).scalars():
                        r.status = S.AVAILABLE.value
                    row.status = S.AVAILABLE.value; row.reason = None
                else:
                    row.reason = Reason.DELETE_INTERRUPTED.value
            return False

        # 3. ONLY after all moves succeeded: remove rows -> COMMIT
        try:
            with get_session() as s:
                row = s.execute(select(ModelRecord).where(ModelRecord.model_id == model_id)).scalar_one()
                s.delete(row)                                   # cascades representations/artifacts
        except Exception as e:  # noqa: BLE001 - DB failure: everything is safe in trash, stays DELETING
            logger.error(f"[MODELS] delete of {model_id}: DB removal failed, artifacts preserved in trash: {e}")
            return False

        # 4. purge trash (retry-safe; a crash here leaves harmless trash for the reconciler)
        for _final, trash in moved:
            try:
                os.remove(trash)
            except OSError:
                pass
        return True

    # ------------------------------------------------------------ VERIFY / RECONCILE
    def verify(self) -> Dict[str, Any]:
        """DB <-> filesystem reconciliation for models. Report only - never deletes."""
        from . import artifact_paths as ap
        from . import model_registry as reg
        from .artifact_migration import sha256_file
        from .artifact_states import ArtifactStatus as S
        report = {"available_valid": 0, "available_missing": 0, "available_hash_mismatch": 0,
                  "staging_staged": 0, "staging_final": 0, "failed_present": 0, "deleting_trash": 0,
                  "row_no_artifact": 0, "artifact_no_row": [], "representations_degraded": []}
        registered = set()
        for m in reg.list_models():
            for r in m["representations"]:
                comp_bad = []
                for a in r["artifacts"]:
                    registered.add(a["relative_path"])
                    try:
                        p = ap.resolve("models", a["relative_path"])
                    except ap.ArtifactPathError:
                        report["row_no_artifact"] += 1; comp_bad.append(a["relative_path"]); continue
                    exists = os.path.isfile(p)
                    st = a["status"]
                    if st == S.AVAILABLE.value:
                        if not exists:
                            report["available_missing"] += 1; comp_bad.append(a["relative_path"])
                        else:
                            sha, size = sha256_file(p)
                            if sha != a["sha256"] or size != a["size_bytes"]:
                                report["available_hash_mismatch"] += 1; comp_bad.append(a["relative_path"])
                            else:
                                report["available_valid"] += 1
                    elif st == S.STAGING.value:
                        staged = ap.staging_path("models", a["relative_path"])
                        report["staging_staged" if os.path.isfile(staged) else "staging_final"] += 1
                    elif st == S.FAILED.value and exists:
                        report["failed_present"] += 1
                    elif st == S.DELETING.value:
                        if os.path.isfile(ap.trash_path("models", a["relative_path"])):
                            report["deleting_trash"] += 1
                if comp_bad and r["status"] == S.AVAILABLE.value:
                    report["representations_degraded"].append(
                        {"model_id": m["model_id"], "format": r["format"], "failed_components": comp_bad})
        # orphan files under the models root (excluding managed areas)
        root = self.models_dir
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in (".staging", ".trash")]
            for f in files:
                rel = ap.to_relative("models", os.path.join(dirpath, f))
                if rel and rel not in registered:
                    report["artifact_no_row"].append(rel)
        return report
