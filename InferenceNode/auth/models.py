"""ArmyEye-owned auth tables (its own SQLAlchemy Base + Alembic history).

ArmyEye fully owns `users` and `audit_log` in its dedicated database. These types
are portable (Integer/String/Boolean/DateTime + a JSON column) so the same models
run on SQLite for unit tests and PostgreSQL in production.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (Column, Integer, String, Boolean, DateTime, Text,
                        CheckConstraint, Index, func)
from sqlalchemy.types import JSON
from sqlalchemy.orm import declarative_base

# ArmyEye's OWN metadata/Base - Alembic autogenerate targets exactly this.
Base = declarative_base()

VALID_ROLES = ("admin", "user")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(100), nullable=False)
    # Normalized (lowercased) username for case-insensitive uniqueness.
    username_key = Column(String(100), nullable=False)
    email = Column(String(255), nullable=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=True)
    role = Column(String(20), nullable=False, default="user")
    is_active = Column(Boolean, nullable=False, default=True)
    must_change_password = Column(Boolean, nullable=False, default=False)
    # Bumped on any authorization-relevant change (password, role, active). The
    # session stores the value seen at login; a mismatch invalidates the session.
    permissions_version = Column(Integer, nullable=False, default=1)
    last_login = Column(DateTime, nullable=True)
    password_changed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                        onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("role in ('admin','user')", name="ck_users_role"),
        Index("uq_users_username_key", "username_key", unique=True),
        Index("idx_users_role", "role"),
        Index("idx_users_active", "is_active"),
        Index("idx_users_created", "created_at"),
    )

    @property
    def is_admin(self) -> bool:
        return str(self.role).strip().lower() == "admin"

    def __repr__(self) -> str:  # never include the hash
        return f"<User id={self.id} username={self.username!r} role={self.role!r}>"


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    # Denormalized actor so the record survives user deletion.
    actor_user_id = Column(Integer, nullable=True)
    actor_username = Column(String(100), nullable=True)
    action = Column(String(64), nullable=False)   # e.g. user_created, role_changed, engine_created
    target = Column(String(255), nullable=True)    # affected username / engine key / pipeline id
    detail = Column(JSON, nullable=True)           # structured extras (never secrets)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_audit_action_created", "action", "created_at"),
        Index("idx_audit_created", "created_at"),
    )


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()
