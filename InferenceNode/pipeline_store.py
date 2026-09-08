"""Pipeline authorization service (the single chokepoint above PipelineRepository).

Authorization comes from `pipeline_user_access` ONLY. `owner_id`/`owner_username` are
creator metadata and are deliberately never consulted when deciding access - a user who
created a pipeline in the past has no implicit rights today.

  admin        -> every pipeline, every operation (bypasses assignment, same code path)
  normal user  -> only pipelines with a matching pipeline_user_access row carrying the
                  specific permission the operation requires

External failures stay opaque (one 404 for missing *and* forbidden) so a caller can never
probe whether someone else's pipeline exists. The distinction is recorded in the log.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select

from .auth.db import get_session
from .data_models import ModelRecord, Pipeline
from .pipeline_repository import repository, normalize_permissions, PERMISSIONS  # noqa: F401

logger = logging.getLogger("InferenceNode.pipeline_store")


class AccessDenied(Exception):
    """Raised when a user may not perform the requested pipeline operation."""


# Operation -> required column. Explicit map: never inferred from the HTTP method,
# because DELETE is admin-only rather than "an edit".
PERMISSION_COLUMN = {
    "view": "can_view",
    "start": "can_start",
    "stop": "can_stop",
    "edit": "can_edit",
}
ADMIN_ONLY = ("create", "delete", "assign")


def _is_admin(user) -> bool:
    return bool(getattr(user, "is_admin", False)) or \
        str(getattr(user, "role", "")).strip().lower() == "admin"


# --------------------------------------------------------------------------- #
# secret redaction
# --------------------------------------------------------------------------- #
_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|authorization|api[_-]?key|credential)", re.I)
_CRED_URL_RE = re.compile(r"^([a-z][a-z0-9+.\-]*)://([^/@]+)@", re.I)


def redact_url(value: str) -> str:
    """Strip credentials from rtsp://user:pass@host/... style URLs."""
    if not isinstance(value, str) or not _CRED_URL_RE.match(value):
        return value
    try:
        parts = urlsplit(value)
        if not parts.hostname:
            return value
        netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, "***@" + netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return "***"


def sanitize_config(value: Any) -> Any:
    """Recursively redact secret-looking keys and credential-bearing URLs.

    Pipeline config can hold RTSP/HTTP credentials, webhook tokens and internal
    addresses, so it is never returned or logged raw.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = "***" if v not in (None, "", [], {}) else v
            else:
                out[k] = sanitize_config(v)
        return out
    if isinstance(value, list):
        return [sanitize_config(v) for v in value]
    if isinstance(value, str):
        return redact_url(value)
    return value


# --------------------------------------------------------------------------- #
# redaction-safe merge (the inverse of sanitize_config, applied on UPDATE)
# --------------------------------------------------------------------------- #
REDACTED = "***"
_REDACTED_USERINFO_RE = re.compile(r"^([a-z][a-z0-9+.\-]*)://\*\*\*@", re.I)


def _is_secret_key(key: Any) -> bool:
    return isinstance(key, str) and bool(_SECRET_KEY_RE.search(key))


def _merge_url_credentials(existing: Any, incoming: str) -> str:
    """Component-wise: credentials come from the STORED url, every other component
    (scheme/host/port/path/query/fragment) from the INCOMING url. So an operator who
    changes only the host or stream path keeps the stored password."""
    try:
        new = urlsplit(incoming)
        old = urlsplit(existing) if isinstance(existing, str) else None
    except Exception:
        return incoming
    if old is None or old.username is None:
        # Nothing stored to preserve; strip the sentinel rather than persist it.
        host = new.hostname or ""
        netloc = host + (f":{new.port}" if new.port else "")
        return urlunsplit((new.scheme, netloc, new.path, new.query, new.fragment))
    userinfo = old.username + (f":{old.password}" if old.password is not None else "")
    host = new.hostname or ""
    netloc = f"{userinfo}@{host}" + (f":{new.port}" if new.port else "")
    return urlunsplit((new.scheme, netloc, new.path, new.query, new.fragment))


def unredact_into(existing: Any, incoming: Any) -> Any:
    """Merge a client-submitted (possibly redacted) value onto the stored value.

    Explicit rules - four DISTINCT cases, never conflated:
      key omitted from a submitted dict      -> preserve the stored value
      value == "***" (secret-looking key)    -> preserve the stored secret (redacted echo)
      value is a genuine new string          -> replace
      value is None / ""                     -> CLEAR (explicit; sanitize_config never emits
                                                these for a stored secret, so a client can
                                                only mean "remove")
    URLs whose userinfo is the sentinel (`scheme://***@host...`) are merged
    component-wise: stored credential + incoming scheme/host/port/path/query.
    `destinations` (and any list of dicts carrying `id`) are matched by identity
    (`id`), NEVER by index, so a reorder can never move a secret between destinations.
    Non-secret keys keep plain replace semantics.
    """
    if isinstance(incoming, dict):
        base = existing if isinstance(existing, dict) else {}
        out: Dict[str, Any] = {}
        for k, v in incoming.items():
            if _is_secret_key(k):
                if v == REDACTED:
                    if k in base:
                        out[k] = base[k]          # redacted echo -> keep stored secret
                    # brand-new object echoing "***": drop the key, never store the literal
                elif isinstance(v, str) and _REDACTED_USERINFO_RE.match(v):
                    out[k] = _merge_url_credentials(base.get(k), v)
                else:
                    out[k] = v                     # new value or explicit None/"" clear
            elif isinstance(v, str) and _REDACTED_USERINFO_RE.match(v):
                out[k] = _merge_url_credentials(base.get(k), v)
            else:
                out[k] = unredact_into(base.get(k), v)
        # keys omitted by the client are preserved ONLY inside secret-bearing sub-objects
        # of the same shape - top-level omission semantics belong to the caller's merge.
        for k, v in base.items():
            if k not in out and _is_secret_key(k):
                out[k] = v
        return out
    if isinstance(incoming, list):
        base_list = existing if isinstance(existing, list) else []
        by_id = {d.get("id"): d for d in base_list if isinstance(d, dict) and d.get("id") is not None}
        result = []
        for item in incoming:
            if isinstance(item, dict) and item.get("id") is not None and item["id"] in by_id:
                result.append(unredact_into(by_id[item["id"]], item))   # matched by identity
            else:
                result.append(unredact_into(None, item))                 # new item: no secrets to inherit
        return result
    if isinstance(incoming, str) and _REDACTED_USERINFO_RE.match(incoming):
        return _merge_url_credentials(existing, incoming)
    return incoming


def pipeline_view(record: Dict[str, Any], *, is_admin: bool) -> Dict[str, Any]:
    """Response DTO. Admins get the (redacted) configuration; normal users get only
    what their permitted operations need - never the raw config blob."""
    base = {
        "pipeline_id": record["pipeline_id"],
        "name": record.get("name"),
        "description": record.get("description"),
        "status": record.get("status"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }
    cfg = record.get("config") or {}
    if is_admin:
        base["config"] = sanitize_config(cfg)
        base["owner_username"] = record.get("owner_username")
        # Which worker owns this pipeline; None = unassigned (any node may run it).
        base["node_id"] = record.get("node_id")
        return base
    model = cfg.get("model") or {}
    frame = cfg.get("frame_source") or {}
    base["summary"] = {
        "capture_type": frame.get("capture_type"),
        "engine_type": model.get("engine_type"),
        "device": model.get("device"),
        "inference_enabled": cfg.get("inference_enabled", True),
    }
    return base


# --------------------------------------------------------------------------- #
# authorization core
# --------------------------------------------------------------------------- #
def assign_pipeline_to_node(admin_user, pipeline_id: str, node_id: Optional[str]) -> Dict[str, Any]:
    """Pin a pipeline to one worker process, or release it with node_id=None.

    Assignment is the only thing stopping two nodes that share this database from starting
    the same camera twice. It is admin-only: it decides where work runs, which is an
    operational control, not a per-pipeline permission.
    """
    require_admin(admin_user, "assign pipeline to node")
    value = (node_id or "").strip() or None
    with get_session() as s:
        row = s.execute(select(Pipeline).where(Pipeline.pipeline_id == pipeline_id)).scalar_one_or_none()
        if row is None:
            raise KeyError(pipeline_id)
        before, row.node_id = row.node_id, value
        s.commit()
    _audit("pipeline_node_assigned", admin_user, pipeline_id,
           {"from": before, "to": value})
    return {"pipeline_id": pipeline_id, "node_id": value, "previous_node_id": before}


def runnable_on_node(record: Dict[str, Any], node_id: Optional[str]) -> bool:
    """An UNASSIGNED pipeline runs anywhere - that is the pre-existing single-node
    behaviour and why adding this column changed nothing for existing deployments."""
    assigned = record.get("node_id")
    return not assigned or assigned == node_id


def get_pipeline_for_user(pipeline_id: str, user, required_permission: str = "view",
                          *, require: bool = True) -> Optional[Dict[str, Any]]:
    """The single authorization chokepoint every pipeline operation passes through.

    Returns the full persistent record when allowed. On refusal raises AccessDenied
    (require=True) or returns None - callers must surface ONE opaque message for both
    "missing" and "forbidden".
    """
    column = PERMISSION_COLUMN.get(required_permission)
    if column is None:
        raise ValueError(f"Unknown permission: {required_permission}")

    uid = getattr(user, "id", None)
    role = getattr(user, "role", None)
    record = repository.get(pipeline_id)

    def _refuse(decision: str, assignment_exists: bool):
        logger.warning(
            "pipeline authz denied: pipeline_id=%s user_id=%s role=%s db_row_exists=%s "
            "assignment_exists=%s required_permission=%s decision=%s",
            pipeline_id, uid, role, record is not None, assignment_exists,
            required_permission, decision)
        if require:
            raise AccessDenied("Pipeline not found or access denied")
        return None

    if record is None:
        return _refuse("pipeline_missing", False)

    if _is_admin(user):
        return record

    if uid is None:
        return _refuse("pipeline_access_denied", False)

    access = repository.get_access(pipeline_id, uid)
    if access is None:
        return _refuse("pipeline_access_denied", False)
    if not access.get(column):
        return _refuse("permission_missing", True)
    return record


def require_admin(user, operation: str = "operation") -> None:
    if not _is_admin(user):
        logger.warning("admin-only refused: operation=%s user_id=%s role=%s",
                       operation, getattr(user, "id", None), getattr(user, "role", None))
        raise AccessDenied("Pipeline not found or access denied")


def list_pipelines_for_user(user) -> List[Dict[str, Any]]:
    """SQL-scoped listing. Never returns everything for later filtering in JS."""
    return repository.list(user_id=getattr(user, "id", None), is_admin=_is_admin(user))


# --------------------------------------------------------------------------- #
# mutations (all pass through the authorization core first)
# --------------------------------------------------------------------------- #
def _audit(action: str, actor, target: str, detail: dict = None) -> None:
    """Audit trail for pipeline lifecycle. Written AFTER the operation committed, so the
    log never claims a change that did not happen. Never raises: a failed audit write is
    logged, not propagated to the caller (the mutation already succeeded). Detail is
    structured metadata only - configuration values and secrets are NEVER recorded."""
    try:
        from .auth.service import record_audit
        record_audit(actor=actor, action=action, target=str(target), detail=detail or {})
    except Exception as e:  # noqa: BLE001
        logger.error(f"[AUDIT] could not record {action} for {target}: {e.__class__.__name__}: {e}")


def _config_shape(config: Optional[dict]) -> dict:
    """Non-sensitive shape of a pipeline config for the audit detail: which model, which
    kind of source, how many destinations - never URLs, credentials or tokens."""
    cfg = config if isinstance(config, dict) else {}
    fs = cfg.get("frame_source") if isinstance(cfg.get("frame_source"), dict) else {}
    fs_cfg = fs.get("config") if isinstance(fs.get("config"), dict) else {}
    model = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    dests = cfg.get("destinations") if isinstance(cfg.get("destinations"), list) else []
    return {
        "model_id": model.get("id"),
        "source_type": fs.get("capture_type") or fs.get("type"),
        "media_ref": fs_cfg.get("relative_source"),          # relative reference only
        "destination_count": len(dests),
        "destination_types": sorted({d.get("type") for d in dests if isinstance(d, dict) and d.get("type")}),
    }


def create_pipeline(user, *, pipeline_id: str, name: str = None, description: str = None,
                    config: dict = None, status: str = "stopped") -> Dict[str, Any]:
    """ADMIN ONLY. Creator metadata is taken from the authenticated user; any owner
    field supplied by the client is ignored."""
    require_admin(user, "pipeline_create")
    record = repository.create(pipeline_id=pipeline_id, name=name, description=description,
                               config=config, status=status,
                               owner_id=getattr(user, "id", None),
                               owner_username=getattr(user, "username", None))
    _audit("pipeline_created", user, pipeline_id,
           {"name": name, **_config_shape(record.get("config") if isinstance(record, dict) else config)})
    return record


def update_pipeline(user, pipeline_id: str, *, name=None, description=None,
                    config=None, status=None) -> Dict[str, Any]:
    before = get_pipeline_for_user(pipeline_id, user, "edit")
    record = repository.update(pipeline_id, name=name, description=description,
                               config=config, status=status)
    detail = {"fields": sorted(k for k, v in (("name", name), ("description", description),
                                              ("config", config), ("status", status)) if v is not None)}
    if config is not None:
        old_shape = _config_shape(before.get("config") if isinstance(before, dict) else None)
        new_shape = _config_shape(record.get("config") if isinstance(record, dict) else config)
        detail["changed"] = {k: {"old": old_shape.get(k), "new": new_shape.get(k)}
                             for k in new_shape if old_shape.get(k) != new_shape.get(k)}
    _audit("pipeline_updated", user, pipeline_id, detail)
    return record


def delete_pipeline(user, pipeline_id: str) -> None:
    """ADMIN ONLY - deliberately not can_edit."""
    require_admin(user, "pipeline_delete")
    if not repository.exists(pipeline_id):
        raise AccessDenied("Pipeline not found or access denied")
    try:
        record = repository.get(pipeline_id)
    except Exception:  # noqa: BLE001 - the delete itself is what matters
        record = None
    repository.delete(pipeline_id)
    detail = {"name": (record or {}).get("name")} if record else {}
    if record:
        detail.update(_config_shape(record.get("config")))
    _audit("pipeline_deleted", user, pipeline_id, detail)


def set_status(pipeline_id: str, status: str) -> None:
    """Runtime status write-back after an already-authorized start/stop."""
    repository.set_status(pipeline_id, status)


def transfer_ownership(admin_user, pipeline_id: str, new_owner_id: int,
                       new_owner_username: str) -> Dict[str, Any]:
    """Admin-only change of CREATOR METADATA. This grants no access by itself -
    use the assignment API to change who may operate the pipeline."""
    require_admin(admin_user, "pipeline_transfer_ownership")
    from .data_models import Pipeline
    with get_session() as s:
        p = s.execute(select(Pipeline).where(
            Pipeline.pipeline_id == str(pipeline_id))).scalar_one_or_none()
        if p is None:
            raise AccessDenied("Pipeline not found or access denied")
        p.owner_id = new_owner_id
        p.owner_username = new_owner_username
        s.flush()
        return {"pipeline_id": p.pipeline_id, "owner_id": p.owner_id,
                "owner_username": p.owner_username}


# --------------------------------------------------------------------------- #
# assignments (ADMIN ONLY)
# --------------------------------------------------------------------------- #
def list_access(admin_user, pipeline_id: str) -> List[Dict[str, Any]]:
    require_admin(admin_user, "pipeline_access_list")
    if not repository.exists(pipeline_id):
        raise AccessDenied("Pipeline not found or access denied")
    return repository.list_access_for_pipeline(pipeline_id)


def list_access_for_user(admin_user, user_id: int) -> List[Dict[str, Any]]:
    require_admin(admin_user, "pipeline_access_list_user")
    return repository.list_access_for_user(user_id)


def set_access(admin_user, pipeline_id: str, user_id: int, perms: Dict[str, Any]) -> Dict[str, Any]:
    """Idempotent per-user grant. Permissions are normalized server-side so an
    operating right can never exist without visibility."""
    require_admin(admin_user, "pipeline_access_set")
    result = repository.upsert_access(pipeline_id, user_id, perms,
                                      created_by_id=getattr(admin_user, "id", None))
    if result is None:
        raise AccessDenied("Pipeline not found or access denied")
    return result


def remove_access(admin_user, pipeline_id: str, user_id: int) -> bool:
    require_admin(admin_user, "pipeline_access_remove")
    if not repository.exists(pipeline_id):
        raise AccessDenied("Pipeline not found or access denied")
    return repository.delete_access(pipeline_id, user_id)


# --------------------------------------------------------------------------- #
# models (unchanged surface)
# --------------------------------------------------------------------------- #
def record_model(user, *, model_id, name=None, engine_type=None, filename=None,
                 path=None, meta=None) -> dict:
    with get_session() as s:
        exists = s.execute(select(ModelRecord).where(
            ModelRecord.model_id == str(model_id))).scalar_one_or_none()
        if exists is not None:
            raise ValueError("model_id already exists")
        m = ModelRecord(model_id=str(model_id), uploader_id=getattr(user, "id", None),
                        uploader_username=getattr(user, "username", None), name=name,
                        engine_type=engine_type, filename=filename, path=path, meta=meta)
        s.add(m); s.flush()
        return {"id": m.id, "model_id": m.model_id, "uploader_id": m.uploader_id,
                "uploader_username": m.uploader_username, "engine_type": m.engine_type}


def list_models() -> List[dict]:
    with get_session() as s:
        rows = s.execute(select(ModelRecord).order_by(ModelRecord.created_at)).scalars().all()
        return [{"model_id": m.model_id, "name": m.name, "engine_type": m.engine_type,
                 "uploader_username": m.uploader_username, "path": m.path} for m in rows]
