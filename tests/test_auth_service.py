"""Auth service tests (ArmyEye-owned users) on a temp SQLite DB: bcrypt, URL
normalization, seed_admin (empty-only, never resets), authenticate, account
management, transactional last-admin protection, and permissions_version bumps."""
import os
import sys

import bcrypt
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db            # noqa: E402
from InferenceNode.auth import service as svc             # noqa: E402
from InferenceNode.auth.models import User, Base          # noqa: E402
from sqlalchemy import select, func                        # noqa: E402


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, "_engine", None, raising=False)
    monkeypatch.setattr(auth_db, "_SessionLocal", None, raising=False)
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'a.db'}")
    Base.metadata.create_all(engine)
    yield
    monkeypatch.setattr(auth_db, "_engine", None, raising=False)
    monkeypatch.setattr(auth_db, "_SessionLocal", None, raising=False)


class _Actor:
    id, username = 1, "tester"


# --- URL normalization ---
@pytest.mark.parametrize("raw,expected", [
    ("postgresql+asyncpg://u:p@h:5432/db", "postgresql+psycopg2://u:p@h:5432/db"),
    ("postgresql://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
    ("postgres://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
    ("sqlite:///x.db", "sqlite:///x.db"),
])
def test_normalize_url(raw, expected):
    assert auth_db.normalize_database_url(raw) == expected


# --- bcrypt ---
def test_hash_and_verify():
    h = svc.hash_password("hunter2xx")
    assert svc.verify_password("hunter2xx", h) is True
    assert svc.verify_password("wrong", h) is False

def test_hash_min_length():
    with pytest.raises(svc.UserOpError):
        svc.hash_password("short")

def test_verify_rejects_non_bcrypt():
    assert svc.verify_password("x", "nope") is False
    assert svc.verify_password("x", None) is False


# --- seeding ---
def test_seed_admin_only_when_empty(sqlite_db, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "root")
    monkeypatch.setenv("ADMIN_PASSWORD", "rootpass1")
    assert svc.seed_admin() == "root"
    u = svc.authenticate("root", "rootpass1")
    assert u.is_admin and u.must_change_password is True
    # Second call is a no-op (never resets an existing directory)
    assert svc.seed_admin() is None

def test_seed_admin_never_resets_existing(sqlite_db, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "root")
    monkeypatch.setenv("ADMIN_PASSWORD", "rootpass1")
    svc.seed_admin()
    # change env "password" and re-seed: existing admin's password must be unchanged
    monkeypatch.setenv("ADMIN_PASSWORD", "different9")
    assert svc.seed_admin() is None
    assert svc.authenticate("root", "rootpass1").username == "root"

def test_seed_admin_noop_without_env(sqlite_db, monkeypatch):
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    assert svc.seed_admin() is None


# --- authenticate ---
def test_authenticate_paths(sqlite_db):
    svc.create_user(_Actor, username="joe", password="joepass12", role="user")
    assert svc.authenticate("joe", "joepass12").role == "user"
    with pytest.raises(svc.AuthError) as e1:
        svc.authenticate("joe", "bad")
    assert e1.value.code == "invalid"
    with pytest.raises(svc.AuthError) as e2:
        svc.authenticate("ghost", "whatever")
    assert e2.value.code == "invalid"

def test_authenticate_inactive(sqlite_db):
    admin = svc.create_user(_Actor, username="a", password="adminpass1", role="admin")
    u = svc.create_user(_Actor, username="bob", password="bobpass123", role="user")
    svc.set_active(admin, u.id, False)
    with pytest.raises(svc.AuthError) as e:
        svc.authenticate("bob", "bobpass123")
    assert e.value.code == "inactive"

def test_username_case_insensitive_unique(sqlite_db):
    svc.create_user(_Actor, username="Joe", password="joepass12")
    with pytest.raises(svc.UserOpError):
        svc.create_user(_Actor, username="joe", password="other123")
    # login works case-insensitively
    assert svc.authenticate("JOE", "joepass12").username == "Joe"


# --- last-admin protection ---
def test_cannot_deactivate_last_admin(sqlite_db):
    admin = svc.create_user(_Actor, username="root", password="rootpass1", role="admin")
    with pytest.raises(svc.UserOpError):
        svc.set_active(admin, admin.id, False)

def test_cannot_downgrade_last_admin(sqlite_db):
    admin = svc.create_user(_Actor, username="root", password="rootpass1", role="admin")
    with pytest.raises(svc.UserOpError):
        svc.set_role(admin, admin.id, "user")

def test_cannot_delete_last_admin(sqlite_db):
    admin = svc.create_user(_Actor, username="root", password="rootpass1", role="admin")
    with pytest.raises(svc.UserOpError):
        svc.delete_user(admin, admin.id)

def test_second_admin_allows_downgrade(sqlite_db):
    a1 = svc.create_user(_Actor, username="a1", password="a1pass123", role="admin")
    a2 = svc.create_user(_Actor, username="a2", password="a2pass123", role="admin")
    svc.set_role(a1, a2.id, "user")  # ok: a1 still an active admin
    assert svc.get_user_by_id(a2.id).role == "user"


# --- permissions_version bumps ---
def _ver(uid):
    return svc.current_permissions_version(uid)

def test_permissions_version_bumps(sqlite_db):
    admin = svc.create_user(_Actor, username="root", password="rootpass1", role="admin")
    u = svc.create_user(_Actor, username="joe", password="joepass12", role="user")
    v0 = _ver(u.id)
    svc.set_role(admin, u.id, "admin"); v1 = _ver(u.id); assert v1 == v0 + 1
    svc.set_active(admin, u.id, False); v2 = _ver(u.id)
    # deactivated -> current_permissions_version returns None (inactive)
    assert v2 is None
    svc.set_active(admin, u.id, True); v3 = _ver(u.id); assert v3 == v1 + 2
    svc.reset_password(admin, u.id, "brandnew1"); v4 = _ver(u.id); assert v4 == v3 + 1

def test_change_own_password(sqlite_db):
    u = svc.create_user(_Actor, username="joe", password="joepass12", role="user",
                        must_change_password=True)
    v0 = _ver(u.id)
    with pytest.raises(svc.UserOpError):
        svc.change_own_password(u.id, "wrongcur", "newpass12")
    svc.change_own_password(u.id, "joepass12", "newpass12")
    assert _ver(u.id) == v0 + 1
    after = svc.authenticate("joe", "newpass12")
    assert after.must_change_password is False


# --- audit ---
def test_audit_rows_written(sqlite_db):
    from InferenceNode.auth.models import AuditLog
    svc.create_user(_Actor, username="joe", password="joepass12")
    with auth_db.get_session() as s:
        actions = [r.action for r in s.execute(select(AuditLog)).scalars().all()]
    assert "user_created" in actions
