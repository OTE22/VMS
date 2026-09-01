"""Centralized pipeline permission gate.

Authorization comes from pipeline_user_access, never from owner_id. Each route maps to
an EXPLICIT permission, delete is admin-only, and every refusal looks identical from
outside whether the pipeline is missing or merely forbidden.
"""
import os
import sys

import pytest
from flask import Flask, jsonify

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db          # noqa: E402
from InferenceNode.auth import service as svc          # noqa: E402
from InferenceNode.auth.models import Base             # noqa: E402
import InferenceNode.data_models                        # noqa: E402,F401
from InferenceNode import pipeline_store as ps          # noqa: E402
from InferenceNode.auth.flask_auth import setup_auth    # noqa: E402
from InferenceNode.pipeline_authz import register_pipeline_authz  # noqa: E402

TEMPLATES = os.path.join(REPO, "InferenceNode", "templates")
STATIC = os.path.join(REPO, "InferenceNode", "static")


class _Seed:
    id, username, role = 0, "seed", "admin"
    is_admin = True


@pytest.fixture()
def app_db(tmp_path):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'a.db'}")
    Base.metadata.create_all(engine)
    admin = svc.create_user(_Seed, username="root", password="rootpass1", role="admin",
                            must_change_password=False)
    joe = svc.create_user(_Seed, username="joe", password="joepass123", role="user",
                          must_change_password=False)
    svc.create_user(_Seed, username="amy", password="amypass123", role="user",
                    must_change_password=False)

    # Creation is admin-only; assignments decide who may do what.
    ps.create_pipeline(admin, pipeline_id="p1", config={})
    ps.create_pipeline(admin, pipeline_id="p2", config={})
    # joe may view + start p1, but not stop or edit it. Nobody is assigned p2.
    ps.set_access(admin, "p1", joe.id, {"can_view": True, "can_start": True})

    app = Flask(__name__, template_folder=TEMPLATES, static_folder=STATIC)
    app.secret_key = "t"; app.config["WTF_CSRF_ENABLED"] = False
    setup_auth(app, bootstrap=False)
    register_pipeline_authz(app)

    @app.route("/")
    def dashboard(): return "home", 200
    @app.route("/api/pipeline/<pid>", methods=["GET"])
    def get_pipe(pid): return jsonify({"pipeline_id": pid})
    @app.route("/api/pipeline/<pid>", methods=["PUT"])
    def update_pipe(pid): return jsonify({"updated": pid})
    @app.route("/api/pipeline/<pid>", methods=["DELETE"])
    def delete_pipe(pid): return jsonify({"deleted": pid})
    @app.route("/api/pipeline/<pid>/start", methods=["POST"])
    def start_pipe(pid): return jsonify({"started": pid})
    @app.route("/api/pipeline/<pid>/stop", methods=["POST"])
    def stop_pipe(pid): return jsonify({"stopped": pid})
    @app.route("/api/pipeline/<pid>/status", methods=["GET"])
    def status_pipe(pid): return jsonify({"status": pid})
    @app.route("/api/pipelines", methods=["GET"])
    def list_pipes(): return jsonify({"list": True})
    @app.route("/api/pipeline/create", methods=["POST"])
    def create_pipe(): return jsonify({"created": True})
    yield app
    auth_db._engine = None; auth_db._SessionLocal = None


def _login(app, u, p):
    c = app.test_client(); c.post("/login", data={"username": u, "password": p}); return c


def _uid(username):
    return [u for u in svc.list_users() if u.username == username][0].id


# --------------------------------------------------------------- per permission --
def test_assigned_user_gets_exactly_the_granted_permissions(app_db):
    c = _login(app_db, "joe", "joepass123")
    assert c.get("/api/pipeline/p1").status_code == 200          # can_view
    assert c.get("/api/pipeline/p1/status").status_code == 200   # can_view
    assert c.post("/api/pipeline/p1/start").status_code == 200   # can_start
    assert c.post("/api/pipeline/p1/stop").status_code == 404    # NOT granted
    assert c.put("/api/pipeline/p1").status_code == 404          # NOT granted


def test_unassigned_pipeline_is_indistinguishable_from_missing(app_db):
    """Knowing a pipeline_id must reveal nothing."""
    c = _login(app_db, "joe", "joepass123")
    unassigned = c.get("/api/pipeline/p2")
    missing = c.get("/api/pipeline/does-not-exist")
    assert unassigned.status_code == missing.status_code == 404
    assert unassigned.get_json() == missing.get_json()


def test_owner_id_alone_grants_nothing(app_db):
    """The creator column is metadata. Without an access row, admin-created pipelines
    are invisible to a normal user even though owner_username is set."""
    c = _login(app_db, "amy", "amypass123")
    assert c.get("/api/pipeline/p1").status_code == 404
    assert c.get("/api/pipeline/p2").status_code == 404


def test_admin_bypasses_assignment(app_db):
    c = _login(app_db, "root", "rootpass1")
    for pid in ("p1", "p2"):
        assert c.get(f"/api/pipeline/{pid}").status_code == 200
        assert c.post(f"/api/pipeline/{pid}/start").status_code == 200
        assert c.put(f"/api/pipeline/{pid}").status_code == 200


# ------------------------------------------------------------------- delete --
def test_delete_is_admin_only_even_with_can_edit(app_db):
    """DELETE is deliberately NOT mapped to can_edit."""
    joe_id = _uid("joe")
    ps.set_access(_Seed, "p1", joe_id,
                  {"can_view": True, "can_start": True, "can_stop": True, "can_edit": True})
    c = _login(app_db, "joe", "joepass123")
    assert c.put("/api/pipeline/p1").status_code == 200          # can_edit works
    assert c.delete("/api/pipeline/p1").status_code == 404       # but delete does not
    assert _login(app_db, "root", "rootpass1").delete("/api/pipeline/p1").status_code == 200


# ----------------------------------------------------------------- unknown --
def test_unknown_subpath_defaults_to_strictest(app_db):
    """A route added later without updating the table must not be silently public."""
    from InferenceNode.pipeline_authz import _required_permission
    assert _required_permission("brand/new/route", "GET") == "edit"
    assert _required_permission("", "DELETE") == "admin"
    assert _required_permission("", "GET") == "view"
    assert _required_permission("publisher/abc/enable", "POST") == "edit"


def test_collection_routes_not_gated(app_db):
    c = _login(app_db, "joe", "joepass123")
    assert c.get("/api/pipelines").status_code == 200
    assert c.post("/api/pipeline/create").status_code == 200


def test_anonymous_still_blocked_by_auth_gate(app_db):
    c = app_db.test_client()
    assert c.get("/api/pipeline/p1").status_code == 401
