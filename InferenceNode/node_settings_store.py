"""Persistent node configuration - PostgreSQL authoritative (Phase 10).

Replaces node_settings.json for node identity, telemetry configuration and UI
preferences. One row per VALIDATED key (CHECK-constrained in the DB); each value is
validated per key here - never a free-form dump. Telemetry MQTT credentials are
encrypted at rest (config_secrets) and never returned to clients in clear.

Live telemetry SAMPLES are never persisted (runtime/metrics only) - only the config.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select

from . import config_secrets
from .auth.db import get_session
from .data_models import NodeSetting
from .pipeline_store import sanitize_config, unredact_into

logger = logging.getLogger("InferenceNode.node_settings_store")

KEY_NODE_IDENTITY = "node_identity"
KEY_TELEMETRY = "telemetry"
KEY_PREFERENCES = "preferences"
VALID_KEYS = (KEY_NODE_IDENTITY, KEY_TELEMETRY, KEY_PREFERENCES)


class SettingValidationError(ValueError):
    pass


def _validate(key: str, value: dict) -> dict:
    """Per-key schema. Unknown fields are rejected, types coerced/checked."""
    if key not in VALID_KEYS:
        raise SettingValidationError(f"unknown setting key {key!r}")
    if not isinstance(value, dict):
        raise SettingValidationError("value must be an object")
    if key == KEY_NODE_IDENTITY:
        allowed = {"node_id", "node_name"}
        out = {k: str(v) for k, v in value.items() if k in allowed and v is not None}
        if set(value) - allowed:
            raise SettingValidationError(f"unknown node_identity fields: {sorted(set(value) - allowed)}")
        return out
    if key == KEY_TELEMETRY:
        allowed = {"enabled", "publish_interval", "mqtt_server", "mqtt_port", "mqtt_topic",
                   "mqtt_username", "mqtt_password"}
        if set(value) - allowed:
            raise SettingValidationError(f"unknown telemetry fields: {sorted(set(value) - allowed)}")
        out: Dict[str, Any] = {}
        if "enabled" in value:
            out["enabled"] = bool(value["enabled"])
        if "publish_interval" in value and value["publish_interval"] is not None:
            iv = float(value["publish_interval"])
            if iv < 1:
                raise SettingValidationError("publish_interval must be >= 1 second")
            out["publish_interval"] = iv
        for k in ("mqtt_server", "mqtt_topic", "mqtt_username", "mqtt_password"):
            if k in value:
                out[k] = value[k] if value[k] is None else str(value[k])
        if "mqtt_port" in value and value["mqtt_port"] is not None:
            port = int(value["mqtt_port"])
            if not (1 <= port <= 65535):
                raise SettingValidationError("mqtt_port out of range")
            out["mqtt_port"] = port
        return out
    return dict(value)   # preferences: free JSON but still a validated top-level key


def get_setting(key: str, *, runtime: bool = False) -> Optional[dict]:
    """runtime=False -> redacted (API); runtime=True -> decrypted (internal use only)."""
    with get_session() as s:
        row = s.execute(select(NodeSetting).where(NodeSetting.key == key)).scalar_one_or_none()
        if row is None:
            return None
        if runtime:
            plain, ok = config_secrets.decrypt_config(row.value or {})
            plain["_secrets_ok"] = ok
            return plain
        return config_secrets.redact_config(row.value or {})    # key not needed for the API view


def set_setting(key: str, value: dict) -> dict:
    """Validated upsert with redaction-safe merge (a redacted echo keeps the stored
    secret; explicit null clears). Secrets encrypted before write."""
    incoming = _validate(key, value)
    with get_session() as s:
        row = s.execute(select(NodeSetting).where(NodeSetting.key == key)).scalar_one_or_none()
        stored_plain, _ok = config_secrets.decrypt_config((row.value if row else None) or {})
        merged = unredact_into(stored_plain, {**stored_plain, **incoming})
        enc = config_secrets.encrypt_config(merged)
        assert not config_secrets.contains_plaintext_secret(enc)
        if row is None:
            row = NodeSetting(key=key, value=enc)
            s.add(row)
        else:
            row.value = enc
        row.updated_at = datetime.utcnow()
        s.flush()
        return config_secrets.redact_config(row.value)
