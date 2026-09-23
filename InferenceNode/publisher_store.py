"""Publisher favorites + node-level destinations - PostgreSQL authoritative (Phase 10).

Replaces node_settings.json for `favorite_configs` and `publishers`. Secret-looking keys
inside `config` are stored ENCRYPTED via config_secrets (dedicated versioned key);
API responses are always sanitize_config()-redacted; the runtime destination builder is
the only reader of decrypted secrets.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, text

from ResultPublisher.config_validation import normalize_config
from . import config_secrets
from .auth.db import get_session
from .data_models import Publisher
from .pipeline_store import sanitize_config, unredact_into

logger = logging.getLogger("InferenceNode.publisher_store")


class FavoriteConflict(ValueError):
    pass


def _lock_favorites(session):
    # Serialize name checks and edits in the same transaction as their writes.
    if session.get_bind().dialect.name == 'postgresql':
        session.execute(text('SELECT pg_advisory_xact_lock(728194620)'))


def _check_name(session, name, publisher_id=None):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 255:
        raise ValueError('Favorite name must contain 1–255 characters')
    name = name.strip()
    rows = session.execute(select(Publisher.publisher_id, Publisher.name).where(Publisher.kind == 'favorite')).all()
    if any(pid != publisher_id and (other or '').casefold() == name.casefold() for pid, other in rows):
        raise FavoriteConflict('A favorite with this name already exists')
    return name


def _row_public(p: Publisher) -> dict:
    """Redacted view for API responses: secrets are never revealed, encrypted blobs are
    never exposed either - they appear as the sentinel like every other secret."""
    # Redact from the RAW stored config - never requires the key, so a missing key still
    # yields "***" and never null/plaintext.
    return {"id": p.publisher_id, "name": p.name, "description": p.description, "type": p.type, "kind": p.kind,
            "enabled": bool(p.enabled), "config": config_secrets.redact_config(p.config or {}),
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None}


def _row_runtime(p: Publisher) -> dict:
    """Decrypted view for the RUNTIME destination builder only. `secrets_ok=False`
    means a secret could not be decrypted (key missing/wrong): the config is returned
    with that secret as None and callers must NOT use the destination."""
    cfg, ok = config_secrets.decrypt_config(p.config or {})
    return {"id": p.publisher_id, "name": p.name, "description": p.description, "type": p.type, "kind": p.kind,
            "enabled": bool(p.enabled), "config": cfg, "secrets_ok": ok}


def list_publishers(kind: Optional[str] = None, *, runtime: bool = False, session=None) -> List[dict]:
    with get_session(session) as s:
        q = select(Publisher).order_by(Publisher.created_at)
        if kind:
            q = q.where(Publisher.kind == kind)
        rows = s.execute(q).scalars().all()
        return [(_row_runtime if runtime else _row_public)(p) for p in rows]


def get_publisher(publisher_id: str, *, runtime: bool = False, session=None) -> Optional[dict]:
    with get_session(session) as s:
        p = s.execute(select(Publisher).where(Publisher.publisher_id == publisher_id)).scalar_one_or_none()
        return None if p is None else (_row_runtime if runtime else _row_public)(p)


def create_publisher(*, name: str, type: str, config: dict, kind: str = "favorite",
                     enabled: bool = True, created_by: Optional[int] = None,
                     publisher_id: Optional[str] = None, description: Optional[str] = None,
                     session=None, validate=False) -> dict:
    """Secrets are encrypted before the row is written; a plaintext secret can never
    reach PostgreSQL (SecretsUnavailable is raised when no key is loaded)."""
    if validate:
        from ResultPublisher.config_validation import validate_favorite_config
        config = validate_favorite_config(type, config)
    enc = config_secrets.encrypt_config(normalize_config(type, config))
    assert not config_secrets.contains_plaintext_secret(enc)
    with get_session(session) as s:
        if kind == 'favorite':
            _lock_favorites(s)
            name = _check_name(s, name)
        p = Publisher(publisher_id=publisher_id or str(uuid.uuid4()), name=name, type=type, kind=kind,
                      enabled=bool(enabled), config=enc, created_by=created_by, description=description)
        s.add(p); s.flush()
        return _row_public(p)


def update_publisher(publisher_id: str, *, name=None, description=None, type=None,
                     config: Optional[dict] = None, enabled=None, session=None,
                     validate=False, replace_config=False) -> Optional[dict]:
    """Partial updates preserve omitted fields; full forms explicitly replace config."""
    with get_session(session) as s:
        _lock_favorites(s)
        p = s.execute(select(Publisher).where(Publisher.publisher_id == publisher_id).with_for_update()).scalar_one_or_none()
        if p is None:
            return None
        if name is not None:
            p.name = _check_name(s, name, publisher_id) if p.kind == 'favorite' else name
        if description is not None:
            p.description = description
        new_type = type if type is not None else p.type
        if config is not None or new_type != p.type:
            stored, ok = config_secrets.decrypt_config(p.config or {})
            if not ok:
                raise config_secrets.SecretsUnavailable('Existing credentials could not be decrypted')
            incoming = config if config is not None else {}
            if not isinstance(incoming, dict):
                raise ValueError('Configuration must be an object')
            if new_type != p.type:
                stored = {}
            merged = unredact_into(stored, incoming)
            if not replace_config and new_type == p.type:
                merged = {**stored, **merged}
            if validate:
                from ResultPublisher.config_validation import validate_favorite_config
                merged = validate_favorite_config(new_type, merged)
            p.config = config_secrets.encrypt_config(normalize_config(new_type, merged))
            assert not config_secrets.contains_plaintext_secret(p.config)
        p.type = new_type
        if enabled is not None:
            p.enabled = bool(enabled)
        p.updated_at = datetime.utcnow()
        s.flush()
        return _row_public(p)


def delete_publisher(publisher_id: str, *, session=None) -> bool:
    with get_session(session) as s:
        p = s.execute(select(Publisher).where(Publisher.publisher_id == publisher_id)).scalar_one_or_none()
        if p is None:
            return False
        s.delete(p)
        return True
