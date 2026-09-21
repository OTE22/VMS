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

from sqlalchemy import select

from ResultPublisher.config_validation import normalize_config
from . import config_secrets
from .auth.db import get_session
from .data_models import Publisher
from .pipeline_store import sanitize_config, unredact_into

logger = logging.getLogger("InferenceNode.publisher_store")


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
                     session=None) -> dict:
    """Secrets are encrypted before the row is written; a plaintext secret can never
    reach PostgreSQL (SecretsUnavailable is raised when no key is loaded)."""
    enc = config_secrets.encrypt_config(normalize_config(type, config))
    assert not config_secrets.contains_plaintext_secret(enc)
    with get_session(session) as s:
        p = Publisher(publisher_id=publisher_id or str(uuid.uuid4()), name=name, type=type, kind=kind,
                      enabled=bool(enabled), config=enc, created_by=created_by, description=description)
        s.add(p); s.flush()
        return _row_public(p)


def update_publisher(publisher_id: str, *, name=None, description=None, type=None,
                     config: Optional[dict] = None, enabled=None, session=None) -> Optional[dict]:
    """Redaction-safe merge (unredact_into: omitted/sentinel -> keep, new -> replace,
    null -> clear) against the DECRYPTED stored config, then re-encrypt. Honors `type`."""
    with get_session(session) as s:
        p = s.execute(select(Publisher).where(Publisher.publisher_id == publisher_id)).scalar_one_or_none()
        if p is None:
            return None
        if name is not None:
            p.name = name
        if description is not None:
            p.description = description
        if type is not None:
            p.type = type
        if enabled is not None:
            p.enabled = bool(enabled)
        if config is not None:
            stored_plain, _ok = config_secrets.decrypt_config(p.config or {})
            merged = unredact_into(stored_plain, config)
            enc = config_secrets.encrypt_config(normalize_config(p.type, merged))
            assert not config_secrets.contains_plaintext_secret(enc)
            p.config = enc
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
