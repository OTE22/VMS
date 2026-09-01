"""User service for ArmyEye's own auth database.

Owns account lifecycle (create/update/reset/list), bcrypt passwords, transactional
last-active-admin protection, `permissions_version` bumps for session invalidation,
seeded admin, and audit logging. All mutations write an AuditLog row in the SAME
transaction as the change.
"""
from __future__ import annotations

import os
import logging
from datetime import datetime
from typing import Optional, List

import bcrypt
from sqlalchemy import select, func

from .db import get_session, is_postgres
from .models import User, AuditLog, normalize_username, VALID_ROLES

logger = logging.getLogger("InferenceNode.auth")

_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


class AuthError(Exception):
    """Login refusal with a safe, user-facing message + code."""
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code  # invalid | inactive | unconfigured


class UserOpError(Exception):
    """Admin user-management operation refused (e.g. last-admin protection)."""


# --------------------------------------------------------------------------- #
# password helpers
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise UserOpError("Password must be at least 8 characters")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    if not password_hash or not isinstance(password_hash, str):
        return False
    if not password_hash.startswith(_BCRYPT_PREFIXES):
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #
def _audit(session, action, *, actor=None, target=None, detail=None, ip=None):
    session.add(AuditLog(
        actor_user_id=getattr(actor, "id", None),
        actor_username=getattr(actor, "username", None),
        action=action, target=target, detail=detail, ip_address=ip,
    ))


def record_audit(actor=None, action="", target=None, detail=None, ip=None) -> None:
    """Public audit write in its own transaction (used by non-user-mgmt features
    such as the engine builder)."""
    with get_session() as session:
        _audit(session, action, actor=actor, target=target, detail=detail, ip=ip)


def _lock_user(session, user_id: int) -> Optional[User]:
    """Load a user row, taking a row lock on Postgres to serialize concurrent
    authorization changes (last-admin protection)."""
    stmt = select(User).where(User.id == user_id)
    if is_postgres():
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def _active_admin_count(session, exclude_id: Optional[int] = None) -> int:
    stmt = select(func.count()).select_from(User).where(
        User.role == "admin", User.is_active.is_(True))
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return int(session.execute(stmt).scalar_one())


def _would_remove_last_admin(session, target: User) -> bool:
    """True if target is currently an active admin and no OTHER active admin exists."""
    if not (target.is_admin and target.is_active):
        return False
    return _active_admin_count(session, exclude_id=target.id) == 0


# --------------------------------------------------------------------------- #
# seeding
# --------------------------------------------------------------------------- #
def seed_admin() -> Optional[str]:
    """Create the initial admin ONLY when the users table is completely empty.
    Never resets an existing admin's password from the environment. Returns the
    created username, or None if seeding did not run."""
    username = (os.environ.get("ADMIN_USERNAME") or "").strip()
    password = os.environ.get("ADMIN_PASSWORD") or ""
    with get_session() as session:
        existing = session.execute(select(func.count()).select_from(User)).scalar_one()
        if existing:
            return None  # never touch an existing directory
        if not username or not password:
            logger.warning("No users exist and ADMIN_USERNAME/ADMIN_PASSWORD are not set - "
                           "no admin seeded; set them and restart to bootstrap.")
            return None
        try:
            pwd_hash = hash_password(password)
        except UserOpError as e:
            logger.error(f"Cannot seed admin: {e}")
            return None
        user = User(username=username, username_key=normalize_username(username),
                    password_hash=pwd_hash, role="admin", is_active=True,
                    must_change_password=True, permissions_version=1,
                    password_changed_at=None)
        session.add(user)
        _audit(session, "admin_seeded", target=username, detail={"source": "env"})
        logger.info(f"Seeded initial admin '{username}' (must change password on first login)")
        return username


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #
def authenticate(username: str, password: str) -> "UserSnapshot":
    username = (username or "").strip()
    if not username or not password:
        raise AuthError("Invalid username or password", "invalid")
    with get_session() as session:
        user = session.execute(
            select(User).where(User.username_key == normalize_username(username))
        ).scalar_one_or_none()
        candidate = user.password_hash if user else "$2b$12$" + "x" * 53
        ok = verify_password(password, candidate)
        if not user or not ok:
            raise AuthError("Invalid username or password", "invalid")
        if not user.is_active:
            raise AuthError("This account is disabled. Contact an administrator.", "inactive")
        user.last_login = datetime.utcnow()
        _audit(session, "login", actor=user, target=user.username)
        snap = UserSnapshot(user)
    logger.info(f"Login OK for '{snap.username}' (role={snap.role})")
    return snap


def get_user_by_id(user_id) -> Optional["UserSnapshot"]:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    with get_session() as session:
        user = session.get(User, uid)
        if user is None or not user.is_active:
            return None
        return UserSnapshot(user)


def current_permissions_version(user_id) -> Optional[int]:
    """Cheap read of the live permissions_version for session-invalidation checks."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    with get_session() as session:
        row = session.execute(
            select(User.permissions_version, User.is_active).where(User.id == uid)
        ).one_or_none()
        if row is None or not row.is_active:
            return None
        return int(row.permissions_version)


# --------------------------------------------------------------------------- #
# self-service password change
# --------------------------------------------------------------------------- #
def change_own_password(user_id: int, current_password: str, new_password: str) -> None:
    with get_session() as session:
        user = _lock_user(session, user_id)
        if user is None or not user.is_active:
            raise UserOpError("Account not found")
        if not verify_password(current_password, user.password_hash):
            raise UserOpError("Current password is incorrect")
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        user.password_changed_at = datetime.utcnow()
        user.permissions_version = int(user.permissions_version) + 1  # invalidate other sessions
        _audit(session, "password_changed", actor=user, target=user.username)


# --------------------------------------------------------------------------- #
# admin user management
# --------------------------------------------------------------------------- #
def list_users() -> List["UserSnapshot"]:
    with get_session() as session:
        rows = session.execute(select(User).order_by(User.username_key)).scalars().all()
        return [UserSnapshot(u) for u in rows]


MIN_PASSWORD_LENGTH = 8


def _check_password_policy(password: str) -> None:
    """Server-side policy. The admin UI enforces >= 8 client-side and /change-password
    via WTForms; a direct API call could bypass both, so the service enforces it too."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise UserOpError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")


def create_user(actor, *, username, password, role="user", email=None,
                full_name=None, must_change_password=True) -> "UserSnapshot":
    role = (role or "user").strip().lower()
    if role not in VALID_ROLES:
        raise UserOpError(f"Invalid role: {role}")
    uname = (username or "").strip()
    if not uname:
        raise UserOpError("Username is required")
    _check_password_policy(password)
    key = normalize_username(uname)
    with get_session() as session:
        exists = session.execute(select(func.count()).select_from(User)
                                 .where(User.username_key == key)).scalar_one()
        if exists:
            raise UserOpError("A user with that username already exists")
        user = User(username=uname, username_key=key, email=email, full_name=full_name,
                    password_hash=hash_password(password), role=role, is_active=True,
                    must_change_password=must_change_password, permissions_version=1)
        session.add(user)
        session.flush()
        _audit(session, "user_created", actor=actor, target=uname, detail={"role": role})
        return UserSnapshot(user)


def update_profile(actor, user_id: int, *, email=None, full_name=None) -> "UserSnapshot":
    """Edit non-authorization profile fields only (email / full_name).

    Deliberately cannot change username, role, is_active or password - those keep
    their dedicated, separately-audited routes. Passing None leaves a field as-is;
    passing an empty string clears it. Does NOT bump permissions_version: nothing
    here affects authorization, so existing sessions stay valid.
    """
    with get_session() as session:
        user = _lock_user(session, user_id)
        if user is None:
            raise UserOpError("User not found")

        changes = {}
        if email is not None:
            new_email = (email or "").strip() or None
            if new_email != user.email:
                changes["email"] = {"old": user.email, "new": new_email}
                user.email = new_email
        if full_name is not None:
            new_name = (full_name or "").strip() or None
            if new_name != user.full_name:
                changes["full_name"] = {"old": user.full_name, "new": new_name}
                user.full_name = new_name

        if changes:
            _audit(session, "profile_updated", actor=actor, target=user.username,
                   detail=changes)
        return UserSnapshot(user)


def set_role(actor, user_id: int, role: str) -> "UserSnapshot":
    role = (role or "").strip().lower()
    if role not in VALID_ROLES:
        raise UserOpError(f"Invalid role: {role}")
    with get_session() as session:
        user = _lock_user(session, user_id)
        if user is None:
            raise UserOpError("User not found")
        if user.role != role:
            # Downgrading the last active admin is refused transactionally.
            if user.is_admin and role != "admin" and _would_remove_last_admin(session, user):
                raise UserOpError("Cannot remove admin role from the last active administrator")
            old = user.role
            user.role = role
            user.permissions_version = int(user.permissions_version) + 1
            _audit(session, "role_changed", actor=actor, target=user.username,
                   detail={"old": old, "new": role})
        return UserSnapshot(user)


def set_active(actor, user_id: int, active: bool) -> "UserSnapshot":
    with get_session() as session:
        user = _lock_user(session, user_id)
        if user is None:
            raise UserOpError("User not found")
        if bool(user.is_active) != bool(active):
            if not active and _would_remove_last_admin(session, user):
                raise UserOpError("Cannot deactivate the last active administrator")
            user.is_active = bool(active)
            user.permissions_version = int(user.permissions_version) + 1
            _audit(session, "user_activated" if active else "user_deactivated",
                   actor=actor, target=user.username)
        return UserSnapshot(user)


def reset_password(actor, user_id: int, new_password: str, must_change: bool = True) -> None:
    _check_password_policy(new_password)
    with get_session() as session:
        user = _lock_user(session, user_id)
        if user is None:
            raise UserOpError("User not found")
        user.password_hash = hash_password(new_password)
        user.must_change_password = must_change
        user.password_changed_at = datetime.utcnow()
        user.permissions_version = int(user.permissions_version) + 1  # kill old sessions
        _audit(session, "password_reset", actor=actor, target=user.username)


def delete_user(actor, user_id: int) -> None:
    with get_session() as session:
        user = _lock_user(session, user_id)
        if user is None:
            raise UserOpError("User not found")
        if _would_remove_last_admin(session, user):
            raise UserOpError("Cannot delete the last active administrator")
        uname = user.username
        session.delete(user)
        _audit(session, "user_deleted", actor=actor, target=uname)


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #
class UserSnapshot:
    """Session-independent copy of the fields callers use (never the hash)."""
    __slots__ = ("id", "username", "email", "full_name", "role", "is_active",
                 "must_change_password", "permissions_version", "last_login",
                 "created_at")

    def __init__(self, user: User):
        self.id = user.id
        self.username = user.username
        self.email = user.email
        self.full_name = user.full_name
        self.role = user.role
        self.is_active = user.is_active
        self.must_change_password = user.must_change_password
        self.permissions_version = user.permissions_version
        self.last_login = user.last_login
        self.created_at = user.created_at

    @property
    def is_admin(self) -> bool:
        return str(self.role).strip().lower() == "admin"
