"""Central lifecycle vocabulary for every managed artifact / registry entity.

Three ORTHOGONAL fields, never conflated:

    status             lifecycle / operational state - the ONLY field that drives serving
    validation_status  outcome of the last validation
    reason             machine-readable diagnostic code explaining status (nullable)

Everything that persists one of these values - the ORM models, the Alembic CHECK
constraints, the migration code, the reconciler - imports the enums from HERE, so no
undefined value (e.g. an ad-hoc "NEEDS_REVIEW") can exist anywhere. Migration outcomes are
therefore expressed as a formal state plus a reason, e.g. status=MISSING,
reason=AMBIGUOUS_LEGACY_PATH.

Transitions are enforced by `transition()`; recovery ALWAYS goes through VALIDATING
(no direct FAILED/CORRUPT/MISSING -> AVAILABLE). Serving eligibility is decided by ONE
function, `is_servable()`, used by the model loader, engine loader, media resolver,
thumbnail handler and the migration alike.
"""
from __future__ import annotations

import enum
import os
from typing import Iterable, Optional


class ArtifactStatus(str, enum.Enum):
    STAGING = "STAGING"
    VALIDATING = "VALIDATING"
    AVAILABLE = "AVAILABLE"
    FAILED = "FAILED"
    MISSING = "MISSING"
    CORRUPT = "CORRUPT"
    DELETING = "DELETING"


class ValidationStatus(str, enum.Enum):
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    HASH_MISMATCH = "HASH_MISMATCH"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    FORMAT_INVALID = "FORMAT_INVALID"
    SECURITY_REJECTED = "SECURITY_REJECTED"


class Reason(str, enum.Enum):
    """Diagnostic codes. Extend here - never invent free-text status values."""
    LEGACY_FILE_NOT_FOUND = "LEGACY_FILE_NOT_FOUND"
    AMBIGUOUS_LEGACY_PATH = "AMBIGUOUS_LEGACY_PATH"
    LEGACY_PATH_UNRESOLVED = "LEGACY_PATH_UNRESOLVED"
    HASH_MISMATCH = "HASH_MISMATCH"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    COMPONENT_MISSING = "COMPONENT_MISSING"
    MANIFEST_MISMATCH = "MANIFEST_MISMATCH"
    ENCRYPTION_KEY_MISSING = "ENCRYPTION_KEY_MISSING"
    UNREGISTERED_ARTIFACT = "UNREGISTERED_ARTIFACT"
    SECURITY_REJECTED = "SECURITY_REJECTED"
    PROMOTE_INTERRUPTED = "PROMOTE_INTERRUPTED"
    NO_USABLE_REPRESENTATION = "NO_USABLE_REPRESENTATION"
    REQUIRED_REPRESENTATION_UNAVAILABLE = "REQUIRED_REPRESENTATION_UNAVAILABLE"
    FILE_MISSING = "FILE_MISSING"
    DELETE_INTERRUPTED = "DELETE_INTERRUPTED"
    COPY_FAILED = "COPY_FAILED"


class EngineOrigin(str, enum.Enum):
    BUILTIN = "builtin"
    CUSTOM = "custom"


class RepresentationKind(str, enum.Enum):
    PRIMARY = "primary"
    DERIVED = "derived"


STATUS_VALUES = tuple(s.value for s in ArtifactStatus)
VALIDATION_VALUES = tuple(v.value for v in ValidationStatus)
REASON_VALUES = tuple(r.value for r in Reason)
ORIGIN_VALUES = tuple(o.value for o in EngineOrigin)
KIND_VALUES = tuple(k.value for k in RepresentationKind)


# ---------------------------------------------------------------- transitions
_S = ArtifactStatus
ALLOWED_TRANSITIONS = {
    _S.STAGING:    {_S.VALIDATING, _S.FAILED},
    _S.VALIDATING: {_S.AVAILABLE, _S.FAILED, _S.CORRUPT, _S.MISSING},
    _S.AVAILABLE:  {_S.VALIDATING, _S.CORRUPT, _S.MISSING, _S.DELETING},
    _S.CORRUPT:    {_S.VALIDATING},
    _S.MISSING:    {_S.VALIDATING},
    _S.FAILED:     {_S.VALIDATING},
    _S.DELETING:   set(),          # only removal / tombstone follows DELETING
}


class IllegalTransition(ValueError):
    pass


def _coerce(value) -> ArtifactStatus:
    if isinstance(value, ArtifactStatus):
        return value
    try:
        return ArtifactStatus(str(value))
    except ValueError:
        raise IllegalTransition(f"unknown lifecycle status {value!r}")


def can_transition(current, new) -> bool:
    return _coerce(new) in ALLOWED_TRANSITIONS[_coerce(current)]


def transition(current, new) -> ArtifactStatus:
    """Return `new` if `current -> new` is a legal edge, else raise IllegalTransition.
    Callers persist ONLY the returned value, so an illegal edge cannot be stored."""
    cur, nxt = _coerce(current), _coerce(new)
    if nxt not in ALLOWED_TRANSITIONS[cur]:
        raise IllegalTransition(f"{cur.value} -> {nxt.value} is not allowed "
                                f"(recovery must pass through VALIDATING)")
    return nxt


# ---------------------------------------------------------------- serving eligibility
def fingerprint(path: str) -> Optional[dict]:
    """Cheap filesystem identity captured at verification time. SHA256 stays the
    authoritative content identity; this only decides whether a cached verdict may
    still be trusted (mtime alone is never sufficient)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    # inode/device are compared for EQUALITY only. Windows reports 64-bit unsigned file
    # ids and volume serials that overflow a signed BIGINT / SQLite INTEGER, so fold them
    # into 63 bits deterministically (same input -> same value; changed identity -> changed
    # value with overwhelming probability).
    def _fold(v: int) -> int:
        v = int(v or 0)
        return v if 0 <= v < (1 << 63) else (v & ((1 << 63) - 1)) ^ (v >> 63)
    return {
        "verified_size_bytes": int(st.st_size),
        "verified_mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
        "verified_ctime_ns": int(getattr(st, "st_ctime_ns", int(st.st_ctime * 1e9))),
        "verified_inode": _fold(getattr(st, "st_ino", 0)),
        "verified_device": _fold(getattr(st, "st_dev", 0)),
    }


def fingerprint_matches(recorded: dict, current: Optional[dict]) -> bool:
    if not recorded or not current:
        return False
    for key in ("verified_size_bytes", "verified_mtime_ns", "verified_ctime_ns"):
        if recorded.get(key) is None or recorded.get(key) != current.get(key):
            return False
    # inode/device where the platform reports them (0 on some Windows filesystems)
    for key in ("verified_inode", "verified_device"):
        r, c = recorded.get(key), current.get(key)
        if r and c and r != c:
            return False
    return True


def is_servable(status, validation_status, resolved_path: Optional[str],
                recorded_fingerprint: Optional[dict] = None,
                require_fingerprint: bool = True) -> bool:
    """THE serving rule. Nothing STAGING/FAILED/MISSING/CORRUPT/DELETING is ever consumed
    as healthy; AVAILABLE + PASSED + file present + (unchanged cheap fingerprint) only."""
    try:
        if _coerce(status) is not ArtifactStatus.AVAILABLE:
            return False
    except IllegalTransition:
        return False
    if str(validation_status) != ValidationStatus.PASSED.value:
        return False
    if not resolved_path or not os.path.isfile(resolved_path):
        return False
    if require_fingerprint:
        return fingerprint_matches(recorded_fingerprint or {}, fingerprint(resolved_path))
    return True


def all_known(values: Iterable) -> bool:
    return all(str(v) in STATUS_VALUES for v in values)
