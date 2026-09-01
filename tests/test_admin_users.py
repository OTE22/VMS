"""Admin Users API tests: RBAC, CRUD, last-admin protection, forged input."""
import os
import sys
import json

import pytest
from flask import Flask

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db            # noqa: E402
from InferenceNode.auth import service as svc            # noqa: E402
from InferenceNode.auth.models import Base               # noqa: E402
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
    svc.create_user(_Seed, username="root", password="rootpass1", role="admin", must_change_password=False)
    svc.create_user(_Seed, username="joe", password="joepass123", role="user", must_change_password=False)
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


def _post(c, url, body, method="POST"):
    return c.open(url, method=method, data=json.dumps(body),
                  content_type="application/json")


def test_non_admin_forbidden(app_db):
    c = _client(app_db, "joe", "joepass123")
    assert c.get("/admin/users").status_code == 403
    assert c.get("/api/users").status_code == 403
    assert _post(c, "/api/users", {"username": "x", "password": "y"}).status_code == 403


def test_admin_crud(app_db):
    c = _client(app_db, "root", "rootpass1")
    assert c.get("/admin/users").status_code == 200
    # create
    r = _post(c, "/api/users", {"username": "amy", "password": "amypass12", "role": "user"})
    assert r.status_code == 201
    uid = r.get_json()["user"]["id"]
    # list contains amy
    users = c.get("/api/users").get_json()["users"]
    assert any(u["username"] == "amy" for u in users)
    # promote
    assert _post(c, f"/api/users/{uid}/role", {"role": "admin"}, "PUT").get_json()["user"]["role"] == "admin"
    # deactivate
    assert _post(c, f"/api/users/{uid}/active", {"active": False}, "PUT").get_json()["user"]["is_active"] is False
    # reset pw
    assert _post(c, f"/api/users/{uid}/reset-password", {"password": "newpass12"}).status_code == 200
    # delete
    assert c.open(f"/api/users/{uid}", method="DELETE").status_code == 200


def test_duplicate_and_invalid_role(app_db):
    c = _client(app_db, "root", "rootpass1")
    assert _post(c, "/api/users", {"username": "joe", "password": "whatever1"}).status_code == 400  # dup
    assert _post(c, "/api/users", {"username": "z", "password": "zz", "role": "user"}).status_code == 400  # short pw
    r = _post(c, "/api/users", {"username": "q", "password": "qqqqqqqq", "role": "superuser"})
    assert r.status_code == 400  # invalid role


def test_last_admin_protection_via_api(app_db):
    c = _client(app_db, "root", "rootpass1")
    root = [u for u in svc.list_users() if u.username == "root"][0]
    # cannot downgrade / deactivate / delete the only admin
    assert _post(c, f"/api/users/{root.id}/role", {"role": "user"}, "PUT").status_code == 400
    assert _post(c, f"/api/users/{root.id}/active", {"active": False}, "PUT").status_code == 400
    assert c.open(f"/api/users/{root.id}", method="DELETE").status_code == 400


def test_forged_ids_and_missing_body(app_db):
    c = _client(app_db, "root", "rootpass1")
    # non-existent user id
    assert _post(c, "/api/users/999999/role", {"role": "user"}, "PUT").status_code == 400
    # missing role
    r = _post(c, "/api/users", {"username": "", "password": "abcdefgh"})
    assert r.status_code == 400
