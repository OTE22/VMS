"""Pipeline config encryption at the persistence boundary; no runtime/API ciphertext."""
from . import config_secrets as secrets


def encrypt(config):
    from .pipeline_store import _SECRET_KEY_RE, redact_url

    def walk(value, sensitive=False):
        if secrets.is_encrypted_value(value):
            return value
        if isinstance(value, dict):
            return {key: walk(item, sensitive or bool(_SECRET_KEY_RE.search(str(key))))
                    for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item, sensitive) for item in value]
        if isinstance(value, str) and value and (sensitive or redact_url(value) != value):
            return secrets.encrypt_config({'password': value})['password']
        return value

    return walk(config)


def decrypt(config):
    def walk(value):
        if secrets.is_encrypted_value(value):
            result, ok = secrets.decrypt_config({'value': value})
            if not ok:
                raise secrets.SecretsUnavailable('Pipeline credentials unavailable: encryption key missing or invalid')
            return result['value']
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(config)


def migrate_existing():
    """Atomic, idempotent upgrade of existing rows; failure aborts startup.

    Lock rows while converting so a concurrent save cannot be overwritten.
    Validate existing ciphertext too: never silently substitute unavailable secrets.
    """
    from sqlalchemy import select
    from .auth.db import get_session
    from .data_models import Pipeline
    changed = 0
    with get_session() as session:
        for row in session.execute(select(Pipeline).order_by(Pipeline.id).with_for_update()).scalars():
            decrypt(row.config or {})
            encrypted = encrypt(row.config or {})
            if encrypted != row.config:
                row.config = encrypted
                changed += 1
        session.flush()
    return changed
