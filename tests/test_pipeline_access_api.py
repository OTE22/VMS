"""Pipeline assignment API (HTTP layer).

Service-level rules are covered in test_pipeline_store.py; this pins the route
contract: admin-only, CSRF-protected, idempotent PUT, audited.
"""
import json
import os
import sys

import pytest
from flask import Flask
from sqlalchemy import select

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db              # noqa: E402
from InferenceNode.auth import service as svc              # noqa: E402
from InferenceNode.auth.models import Base, AuditLog       # noqa: E402
import InferenceNode.data_models                            # noqa: E402,F401
from InferenceNode import pipeline_store as ps              # noqa: E402
from InferenceNode.auth.flask_auth import setup_auth        # noqa: E402
from InferenceNode.pipeline_access_routes import register_pipeline_access  # noqa: E402

TEMPLATES = os.path.join(REPO, "InferenceNode", "templates")
STATIC = os.path.join(REPO, "InferenceNode", "static")


class _Seed:
    id, username, role = 0, "seed", "admin"
    is_admin = True


@pytest.fixture()
def app_db(tmp_path):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'acc.db'}")
    Base.metadata.create_all(engine)
    admin = svc.create_user(_Seed, username="root", password="rootpass1", role="admin",
                            must_change_password=False)
    svc.create_user(_Seed, username="joe", password="joepass123", role="user",
                    must_change_password=False)
    svc.create_user(_Seed, username="amy", password="amypass123", role="user",
                    must_change_password=False)
    ps.create_pipeline(admin, pipeline_id="p1", name="Main Gate", config={})

    app = Flask(__name__, template_folder=TEMPLATES, static_folder=STATIC)
    app.secret_key = "t"; app.config["WTF_CSRF_ENABLED"] = False
    setup_auth(app, bootstrap=False)
    register_pipeline_access(app)

    @app.route("/")
    def dashboard(): return "home", 200
    yield app
    auth_db._engine = None; auth_db._SessionLocal = None


def _login(app, u, p):
    c = app.test_client(); c.post("/login", data={"username": u, "password": p}); return c


def _uid(username):
    return [u for u in svc.list_users() if u.username == username][0].id


def _put(c, path, body):
    return c.put(path, data=json.dumps(body), content_type="application/json")


# ------------------------------------------------------------------- admin --
def test_admin_can_grant_list_and_revoke(app_db):
    c = _login(app_db, "root", "rootpass1")
    joe = _uid("joe")

    r = _put(c, f"/api/pipelines/p1/access/{joe}", {"can_view": True, "can_start": True})
    assert r.status_code == 200
    assert r.get_json()["access"]["can_start"] is True

    r = c.get("/api/pipelines/p1/access")
    rows = r.get_json()["access"]
    assert len(rows) == 1 and rows[0]["username"] == "joe"

    r = c.get(f"/api/users/{joe}/pipeline-access")
    assert [a["pipeline_id"] for a in r.get_json()["access"]] == ["p1"]

    r = c.delete(f"/api/pipelines/p1/access/{joe}")
    assert r.status_code == 200 and r.get_json()["removed"] is True
    assert c.get("/api/pipelines/p1/access").get_json()["access"] == []


def test_put_is_idempotent(app_db):
    c = _login(app_db, "root", "rootpass1")
    joe = _uid("joe")
    _put(c, f"/api/pipelines/p1/access/{joe}", {"can_view": True})
    _put(c, f"/api/pipelines/p1/access/{joe}", {"can_view": True, "can_edit": True})
    rows = c.get("/api/pipelines/p1/access").get_json()["access"]
    assert len(rows) == 1 and rows[0]["can_edit"] is True


def test_permissions_normalized_over_http(app_db):
    c = _login(app_db, "root", "rootpass1")
    joe = _uid("joe")
    r = _put(c, f"/api/pipelines/p1/access/{joe}", {"can_start": True, "can_view": False})
    assert r.get_json()["access"]["can_view"] is True


def test_unknown_permission_field_rejected(app_db):
    c = _login(app_db, "root", "rootpass1")
    joe = _uid("joe")
    r = _put(c, f"/api/pipelines/p1/access/{joe}", {"can_view": True, "is_admin": True})
    assert r.status_code == 400 and "Unsupported field" in r.get_json()["error"]


def test_unknown_pipeline_is_opaque(app_db):
    c = _login(app_db, "root", "rootpass1")
    joe = _uid("joe")
    assert c.get("/api/pipelines/ghost/access").status_code == 404
    assert _put(c, f"/api/pipelines/ghost/access/{joe}", {"can_view": True}).status_code == 404


# --------------------------------------------------------------- non-admin --
def test_non_admin_cannot_manage_assignments(app_db):
    c = _login(app_db, "joe", "joepass123")
    amy = _uid("amy")
    assert c.get("/api/pipelines/p1/access").status_code == 403
    assert _put(c, f"/api/pipelines/p1/access/{amy}", {"can_view": True}).status_code == 403
    assert c.delete(f"/api/pipelines/p1/access/{amy}").status_code == 403
    assert c.get(f"/api/users/{amy}/pipeline-access").status_code == 403
    assert c.get("/api/pipelines/assignable").status_code == 403


def test_non_admin_cannot_grant_themselves_access(app_db):
    joe = _uid("joe")
    c = _login(app_db, "joe", "joepass123")
    _put(c, f"/api/pipelines/p1/access/{joe}", {"can_view": True, "can_edit": True})
    assert ps.repository.get_access("p1", joe) is None


# --------------------------------------------------- access count column --
def test_user_list_carries_pipeline_access_count(app_db):
    """The users table shows an access count. It must come from the user-list response
    (one aggregate query), never from a request per row."""
    c = _login(app_db, "root", "rootpass1")
    joe, amy = _uid("joe"), _uid("amy")
    ps.create_pipeline(_Seed, pipeline_id="p2", name="Second", config={})

    _put(c, f"/api/pipelines/p1/access/{joe}", {"can_view": True})
    _put(c, f"/api/pipelines/p2/access/{joe}", {"can_view": True})
    _put(c, f"/api/pipelines/p1/access/{amy}", {"can_start": True})   # implies view

    users = {u["username"]: u for u in c.get("/api/users").get_json()["users"]}
    assert users["joe"]["pipeline_access_count"] == 2
    assert users["amy"]["pipeline_access_count"] == 1
    # Admin access is role-derived, so a number would be misleading -> None ("All pipelines")
    assert users["root"]["pipeline_access_count"] is None


def test_access_count_excludes_grants_without_view(app_db):
    c = _login(app_db, "root", "rootpass1")
    joe = _uid("joe")
    ps.repository.upsert_access("p1", joe, {"can_view": False})   # bypass normalisation
    users = {u["username"]: u for u in c.get("/api/users").get_json()["users"]}
    assert users["joe"]["pipeline_access_count"] == 0


def test_access_count_is_one_aggregate_query(app_db):
    """Guard the N+1: counting must not scale with the number of users."""
    from InferenceNode.pipeline_repository import repository
    joe = _uid("joe")
    repository.upsert_access("p1", joe, {"can_view": True})
    counts = repository.access_counts_by_user()
    assert counts == {joe: 1}, counts


# ------------------------------------------------------------------ audit --
def test_grant_and_revoke_are_audited(app_db):
    c = _login(app_db, "root", "rootpass1")
    joe = _uid("joe")
    _put(c, f"/api/pipelines/p1/access/{joe}", {"can_view": True})
    _put(c, f"/api/pipelines/p1/access/{joe}", {"can_view": True, "can_start": True})
    c.delete(f"/api/pipelines/p1/access/{joe}")

    with auth_db.get_session() as s:
        rows = s.execute(select(AuditLog).where(
            AuditLog.action.like("pipeline_access%"))).scalars().all()
        actions = [r.action for r in rows]
    assert actions == ["pipeline_access_granted", "pipeline_access_changed",
                       "pipeline_access_removed"]
    assert all(r.target == "p1" for r in rows)


# ------------------------------------------------------------------- csrf --
def test_csrf_enforced_on_writes(app_db):
    c = _login(app_db, "root", "rootpass1")
    joe = _uid("joe")
    app_db.config["WTF_CSRF_ENABLED"] = True
    assert _put(c, f"/api/pipelines/p1/access/{joe}", {"can_view": True}).status_code == 400
    assert c.delete(f"/api/pipelines/p1/access/{joe}").status_code == 400
    assert ps.repository.get_access("p1", joe) is None
