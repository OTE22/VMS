"""Flask integration tests (ArmyEye-owned auth): login gate, RBAC, logout, rate
limit, forced password change, and permissions_version session invalidation.
Uses a temp SQLite DB seeded via the service; bootstrap disabled (tests own schema)."""
import os
import sys

import pytest
from flask import Flask, jsonify

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db                # noqa: E402
from InferenceNode.auth import service as svc                # noqa: E402
from InferenceNode.auth.models import Base                   # noqa: E402
from InferenceNode.auth.flask_auth import setup_auth, admin_required  # noqa: E402

TEMPLATES = os.path.join(REPO, "InferenceNode", "templates")
STATIC = os.path.join(REPO, "InferenceNode", "static")


class _Actor:
    id, username = 0, "seed"


@pytest.fixture(scope="module", autouse=True)
def shared_db(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("aauth") / "u.db"
    auth_db._engine = None
    auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    svc.create_user(_Actor, username="admin", password="adminpass1", role="admin",
                    must_change_password=False)
    svc.create_user(_Actor, username="joe", password="joepass123", role="user",
                    must_change_password=False)
    svc.create_user(_Actor, username="carol", password="carolpass1", role="user",
                    must_change_password=True)
    yield
    auth_db._engine = None
    auth_db._SessionLocal = None


def make_app():
    app = Flask(__name__, template_folder=TEMPLATES, static_folder=STATIC)
    app.secret_key = "test-secret"
    app.config["WTF_CSRF_ENABLED"] = False
    setup_auth(app, bootstrap=False)

    @app.route("/")
    def dashboard():
        return "home", 200

    @app.route("/api/secret")
    def secret():
        return jsonify({"ok": True})

    @app.route("/api/admin-only")
    @admin_required
    def admin_only():
        return jsonify({"admin": True})

    return app


def login(client, u, p):
    return client.post("/login", data={"username": u, "password": p})


def test_anonymous_redirect_and_401():
    c = make_app().test_client()
    assert c.get("/").status_code == 302
    assert c.get("/api/secret").status_code == 401


def test_login_and_access():
    c = make_app().test_client()
    assert login(c, "joe", "joepass123").status_code == 302
    assert c.get("/").status_code == 200
    assert c.get("/api/secret").get_json()["ok"] is True


def test_wrong_password():
    c = make_app().test_client()
    r = c.post("/login", data={"username": "joe", "password": "bad"})
    assert r.status_code == 200 and b"Invalid username or password" in r.data


def test_rbac():
    c = make_app().test_client()
    login(c, "joe", "joepass123")
    assert c.get("/api/admin-only").status_code == 403
    ca = make_app().test_client()
    login(ca, "admin", "adminpass1")
    assert ca.get("/api/admin-only").get_json()["admin"] is True


def test_logout():
    c = make_app().test_client()
    login(c, "joe", "joepass123")
    assert c.get("/").status_code == 200
    assert c.post("/logout").status_code == 302
    assert c.get("/").status_code == 302


def test_login_page_public():
    c = make_app().test_client()
    r = c.get("/login")
    assert r.status_code == 200 and b"Sign in" in r.data


def test_rate_limit():
    c = make_app().test_client()
    codes = [c.post("/login", data={"username": "joe", "password": "x"}).status_code
             for _ in range(12)]
    assert 429 in codes


def test_forced_password_change_flow():
    c = make_app().test_client()
    # login as a must_change user -> redirected to change-password
    r = login(c, "carol", "carolpass1")
    assert r.status_code == 302 and "/change-password" in r.headers["Location"]
    # dashboard is blocked until password changed
    assert "/change-password" in c.get("/").headers.get("Location", "")
    # change it -> then dashboard works
    r2 = c.post("/change-password", data={"current_password": "carolpass1",
                                          "new_password": "carolnew12",
                                          "confirm_password": "carolnew12"})
    assert r2.status_code == 302 and r2.headers["Location"].endswith("/")
    assert c.get("/").status_code == 200


def test_session_invalidated_on_deactivate():
    # joe logs in on client c; an admin deactivates joe -> c's session dies next request
    c = make_app().test_client()
    login(c, "joe", "joepass123")
    assert c.get("/").status_code == 200
    admin = svc.authenticate("admin", "adminpass1")
    joe = [u for u in svc.list_users() if u.username == "joe"][0]
    svc.set_active(admin, joe.id, False)
    assert c.get("/").status_code == 302          # page -> login
    assert c.get("/api/secret").status_code == 401  # api -> 401
    # restore for other tests
    svc.set_active(admin, joe.id, True)


def test_session_invalidated_on_role_change():
    c = make_app().test_client()
    login(c, "joe", "joepass123")
    assert c.get("/").status_code == 200
    admin = svc.authenticate("admin", "adminpass1")
    joe = [u for u in svc.list_users() if u.username == "joe"][0]
    svc.set_role(admin, joe.id, "admin")   # bumps permissions_version
    assert c.get("/").status_code == 302   # old session invalidated
    svc.set_role(admin, joe.id, "user")    # restore
