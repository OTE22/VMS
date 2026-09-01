"""PATCH /api/users/<id> - profile-only edits.

Guards the deliberate narrowness of this route: it must edit email/full_name and
NOTHING else, audit what it changed, and leave permissions_version alone (a
profile edit is not an authorization change, so sessions must survive it).
"""
import os
import sys
import json

import pytest
from flask import Flask
from sqlalchemy import select

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db            # noqa: E402
from InferenceNode.auth import service as svc            # noqa: E402
from InferenceNode.auth.models import Base, AuditLog, User   # noqa: E402
from InferenceNode.auth.flask_auth import setup_auth     # noqa: E402

TEMPLATES = os.path.join(REPO, "InferenceNode", "templates")
STATIC = os.path.join(REPO, "InferenceNode", "static")


class _Seed:
    id, username = 0, "seed"


@pytest.fixture()
def app_db(tmp_path):
    auth_db._engine = None
    auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'u.db'}")
    Base.metadata.create_all(engine)
    svc.create_user(_Seed, username="root", password="rootpass1", role="admin",
                    must_change_password=False)
    svc.create_user(_Seed, username="joe", password="joepass123", role="user",
                    email="old@example.com", full_name="Old Name",
                    must_change_password=False)
    app = Flask(__name__, template_folder=TEMPLATES, static_folder=STATIC)
    app.secret_key = "t"
    app.config["WTF_CSRF_ENABLED"] = False
    setup_auth(app, bootstrap=False)

    @app.route("/")
    def dashboard():
        return "home", 200
    yield app
    auth_db._engine = None
    auth_db._SessionLocal = None


def _client(app, user, pw):
    c = app.test_client()
    c.post("/login", data={"username": user, "password": pw})
    return c


def _patch(c, uid, body):
    return c.open(f"/api/users/{uid}", method="PATCH", data=json.dumps(body),
                  content_type="application/json")


def _joe():
    return [u for u in svc.list_users() if u.username == "joe"][0]


def _audits(action):
    with auth_db.get_session() as s:
        return s.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()


def test_updates_email_and_full_name(app_db):
    c = _client(app_db, "root", "rootpass1")
    r = _patch(c, _joe().id, {"email": "new@example.com", "full_name": "New Name"})
    assert r.status_code == 200
    user = r.get_json()["user"]
    assert user["email"] == "new@example.com"
    assert user["full_name"] == "New Name"
    # persisted, not just echoed
    assert _joe().email == "new@example.com"


def test_empty_string_clears_a_field(app_db):
    c = _client(app_db, "root", "rootpass1")
    assert _patch(c, _joe().id, {"email": ""}).status_code == 200
    assert _joe().email is None
    # omitted fields are left alone
    assert _joe().full_name == "Old Name"


def test_change_is_audited_with_old_and_new(app_db):
    c = _client(app_db, "root", "rootpass1")
    _patch(c, _joe().id, {"full_name": "Renamed"})
    rows = _audits("profile_updated")
    assert len(rows) == 1
    assert rows[0].actor_username == "root"
    assert rows[0].target == "joe"
    assert rows[0].detail["full_name"] == {"old": "Old Name", "new": "Renamed"}


def test_no_op_is_not_audited(app_db):
    c = _client(app_db, "root", "rootpass1")
    assert _patch(c, _joe().id, {"full_name": "Old Name"}).status_code == 200
    assert _audits("profile_updated") == []


def test_does_not_bump_permissions_version(app_db):
    """A profile edit is not an authorization change - existing sessions must
    survive it, unlike role/active/reset-password."""
    c = _client(app_db, "root", "rootpass1")
    before = _joe().permissions_version
    _patch(c, _joe().id, {"full_name": "Still Joe", "email": "j@example.com"})
    assert _joe().permissions_version == before


@pytest.mark.parametrize("body", [
    {"role": "admin"},
    {"is_active": False},
    {"username": "hijacked"},
    {"password": "hunter2xx"},
    {"must_change_password": False},
    {"email": "ok@example.com", "role": "admin"},   # smuggled alongside a valid field
])
def test_rejects_privileged_fields(app_db, body):
    c = _client(app_db, "root", "rootpass1")
    joe = _joe()
    r = _patch(c, joe.id, body)
    assert r.status_code == 400
    assert "Unsupported field" in r.get_json()["error"]
    # and nothing at all changed
    after = _joe()
    assert (after.role, after.is_active, after.username, after.email) == \
           (joe.role, joe.is_active, joe.username, joe.email)


def test_non_admin_forbidden(app_db):
    c = _client(app_db, "joe", "joepass123")
    assert _patch(c, _joe().id, {"full_name": "Self Service"}).status_code == 403
    assert _joe().full_name == "Old Name"


def test_unknown_user_is_rejected(app_db):
    c = _client(app_db, "root", "rootpass1")
    r = _patch(c, 999999, {"full_name": "Ghost"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "User not found"


def test_csrf_is_enforced_when_enabled(app_db):
    """Flask aborts with an HTML 400 body here - the frontend's apiCall() has to
    tolerate a non-JSON error payload, so pin the actual behaviour."""
    c = _client(app_db, "root", "rootpass1")   # sign in first, then arm CSRF
    app_db.config["WTF_CSRF_ENABLED"] = True
    r = _patch(c, _joe().id, {"full_name": "No Token"})
    assert r.status_code == 400
    assert not r.is_json                       # HTML error page, not {"error": ...}
    assert _joe().full_name == "Old Name"
