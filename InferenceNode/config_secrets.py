"""At-rest encryption for configuration secrets stored in PostgreSQL (publishers,
telemetry MQTT credentials, node settings).

Key architecture - DEDICATED and VERSIONED:

  * The key comes from a mounted file: ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE (compose
    secret). NEVER SECRET_KEY / Flask session / JWT material. NEVER stored in
    PostgreSQL. NEVER baked into the image. NEVER auto-generated as a replacement.
  * File format: one or more lines `key_id:<urlsafe-base64 fernet key>`; the FIRST line
    is the active key used for new encryptions, later lines are older keys kept for
    decryption (rotation = prepend a new line; re-encrypt lazily on next write; retire the
    old line once no row references its key_id).
  * Stored value shape (JSON-serialisable, internal only, never sent to clients):
        {"v": 1, "key_id": "<id>", "ct": "<fernet token>"}
  * Fail-safe: encrypted rows present but the key file is missing / has no matching
    key_id / cannot decrypt -> the SECRET is reported unavailable (reason
    ENCRYPTION_KEY_MISSING); the stored config is NOT destroyed, NOT re-encrypted, NOT
    served in clear. Nothing here ever logs a key or a plaintext secret.

Only keys matching pipeline_store._SECRET_KEY_RE (password/token/secret/api_key/...)
are encrypted; everything else is stored as-is.
"""
from __future__ import annotations

import base64
import logging
import os
import stat
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("InferenceNode.config_secrets")

KEY_FILE_ENV = "ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE"
STORED_VERSION = 1


class SecretsUnavailable(RuntimeError):
    """Raised when a decrypt is requested but no usable key is available."""


class _Keyring:
    def __init__(self):
        self._active_id: Optional[str] = None
        self._keys: Dict[str, Any] = {}
        self._loaded_from: Optional[str] = None

    def load(self, path: Optional[str] = None) -> bool:
        from cryptography.fernet import Fernet
        path = path or os.environ.get(KEY_FILE_ENV)
        self._active_id, self._keys, self._loaded_from = None, {}, None
        if not path or not os.path.isfile(path):
            return False
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if os.name != "nt" and mode & 0o077:
                logger.error(f"[SECRETS] key file {path} is group/world readable (mode {oct(mode)}) - refusing to load")
                return False
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
        except OSError as e:
            logger.error(f"[SECRETS] cannot read key file: {e.__class__.__name__}")
            return False
        for ln in lines:
            if ":" not in ln:
                logger.error("[SECRETS] malformed key line (expected key_id:key) - skipped")
                continue
            kid, key = ln.split(":", 1)
            try:
                self._keys[kid.strip()] = Fernet(key.strip().encode("ascii"))
            except Exception:
                logger.error(f"[SECRETS] key {kid.strip()!r} is not a valid Fernet key - skipped")
        if lines and ":" in lines[0]:
            self._active_id = lines[0].split(":", 1)[0].strip()
        self._loaded_from = path
        return bool(self._keys and self._active_id in self._keys)

    @property
    def available(self) -> bool:
        return bool(self._keys and self._active_id)

    def encrypt(self, plaintext: str) -> dict:
        if not self.available:
            raise SecretsUnavailable("no active encryption key")
        tok = self._keys[self._active_id].encrypt(plaintext.encode("utf-8")).decode("ascii")
        return {"v": STORED_VERSION, "key_id": self._active_id, "ct": tok}

    def decrypt(self, stored: dict) -> str:
        if not isinstance(stored, dict) or stored.get("v") != STORED_VERSION:
            raise SecretsUnavailable("unrecognised encrypted value")
        f = self._keys.get(str(stored.get("key_id")))
        if f is None:
            raise SecretsUnavailable(f"no key for key_id {stored.get('key_id')!r}")
        try:
            return f.decrypt(str(stored["ct"]).encode("ascii")).decode("utf-8")
        except Exception:
            raise SecretsUnavailable("decryption failed (wrong key?)")


_ring = _Keyring()


def reload_keys(path: Optional[str] = None) -> bool:
    return _ring.load(path)


def keys_available() -> bool:
    return _ring.available


def is_encrypted_value(v: Any) -> bool:
    return isinstance(v, dict) and v.get("v") == STORED_VERSION and "ct" in v and "key_id" in v


# ------------------------------------------------------------------ config-level helpers
def _secret_key(k: Any) -> bool:
    from .pipeline_store import _SECRET_KEY_RE
    return isinstance(k, str) and bool(_SECRET_KEY_RE.search(k))


def encrypt_config(config: Any) -> Any:
    """Encrypt every secret-looking leaf. Raises SecretsUnavailable when a secret is
    present but no key is loaded (we never store a plaintext secret silently)."""
    if isinstance(config, dict):
        out = {}
        for k, v in config.items():
            if _secret_key(k) and isinstance(v, str) and v and not is_encrypted_value(v):
                out[k] = _ring.encrypt(v)
            else:
                out[k] = encrypt_config(v)
        return out
    if isinstance(config, list):
        return [encrypt_config(v) for v in config]
    return config


def decrypt_config(config: Any) -> Tuple[Any, bool]:
    """Return (config_with_plaintext_secrets, all_ok). Undecryptable secrets are
    replaced by None and all_ok=False - the config is never destroyed or served wrong."""
    ok = True
    if isinstance(config, dict):
        out = {}
        for k, v in config.items():
            if is_encrypted_value(v):
                try:
                    out[k] = _ring.decrypt(v)
                except SecretsUnavailable:
                    out[k] = None; ok = False
            else:
                sub, sub_ok = decrypt_config(v)
                out[k] = sub; ok = ok and sub_ok
        return out, ok
    if isinstance(config, list):
        items = [decrypt_config(v) for v in config]
        return [i[0] for i in items], all(i[1] for i in items)
    return config, ok


def redact_config(config: Any) -> Any:
    """API view WITHOUT touching the key: every encrypted blob (and every plaintext
    secret-looking leaf) becomes the sentinel "***", so responses stay redacted even when
    the encryption key is unavailable. Non-secret values pass through sanitize_config."""
    from .pipeline_store import sanitize_config, REDACTED
    if isinstance(config, dict):
        out = {}
        for k, v in config.items():
            if is_encrypted_value(v):
                out[k] = REDACTED
            elif _secret_key(k):
                out[k] = REDACTED if v not in (None, "", [], {}) else v
            else:
                out[k] = redact_config(v)
        return out
    if isinstance(config, list):
        return [redact_config(v) for v in config]
    return sanitize_config(config)


def contains_plaintext_secret(config: Any) -> bool:
    """Guard used by tests and the store: True if any secret-looking key holds a bare
    string (i.e. was NOT encrypted)."""
    if isinstance(config, dict):
        for k, v in config.items():
            if _secret_key(k) and isinstance(v, str) and v:
                return True
            if contains_plaintext_secret(v):
                return True
    if isinstance(config, list):
        return any(contains_plaintext_secret(v) for v in config)
    return False


def generate_key_line(key_id: str) -> str:
    """Helper for operators/tests: produce a `key_id:<fernet key>` line. The application
    itself NEVER calls this to replace a missing key."""
    from cryptography.fernet import Fernet
    return f"{key_id}:{Fernet.generate_key().decode('ascii')}"
