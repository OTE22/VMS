"""Phase 3 - authorization / CSRF hardening proofs.

Server-side enforcement is what matters ("a hidden button is not security"). Because
InferenceNode registers its routes as closures, the enforcement primitives are proven
here directly and through a mini app that mounts routes exactly the way the node does:

  * _admin_csrf  (admin_required + _require_csrf)  -> anon 401, user 403, admin w/o token 400,
                                                      admin with token 200
  * pipeline_authz gate: 'duplicate' -> view, 'thumbnail/generate' -> edit
  * get_pipeline_stats / get_pipeline_summary scoped to the caller's records
  * service password policy (>= 8) on create_user and reset_password
  * webhook_receiver no longer shadows /api/discovery/nodes/<id>/control (source-level guard)
"""
import os
import re
import sys

import pytest
from flask import Flask, jsonify

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db                    # noqa: E402
from InferenceNode.auth import service as svc                   # noqa: E402
from InferenceNode.auth.models import Base                      # noqa: E402
from InferenceNode.auth.flask_auth import setup_auth, admin_required  # noqa: E402
from InferenceNode.auth.admin_routes import _require_csrf       # noqa: E402
import InferenceNode.data_models                                # noqa: E402,F401
from InferenceNode import pipeline_store as ps                  # noqa: E402
from InferenceNode.pipeline_authz import _required_permission, _SUBPATH_PERMISSION  # noqa: E402
from InferenceNode.pipeline_manager import PipelineManager      # noqa: E402

TEMPLATES = os.path.join(REPO, "InferenceNode", "templates")
STATIC = os.path.join(REPO, "InferenceNode", "static")


class _Seed:
    id, username, role = 0, "seed", "admin"
    is_admin = True


@pytest.fixture()
def app(tmp_path):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'h.db'}")
    Base.metadata.create_all(engine)
    root = svc.create_user(_Seed, username="root", password="rootpass1", role="admin", must_change_password=False)
    joe = svc.create_user(_Seed, username="joe", password="joepass123", role="user", must_change_password=False)

    app = Flask(__name__, template_folder=TEMPLATES, static_folder=STATIC)
    app.secret_key = "t"
    app.config["WTF_CSRF_ENABLED"] = True          # we WANT the CSRF check live here
    setup_auth(app, bootstrap=False)

    # Mount a write route exactly like InferenceNode does with self._admin_csrf
    def _admin_csrf(view):
        from functools import wraps
        @wraps(view)
        def wrapper(*a, **k):
            _require_csrf()
            return view(*a, **k)
        return admin_required(wrapper)

    @app.route("/")
    def home(): return "home", 200

    @app.route("/api/guarded", methods=["POST"])
    @_admin_csrf
    def guarded(): return jsonify({"ok": True})

    app.users = {"root": root, "joe": joe}
    yield app
    auth_db._engine = None; auth_db._SessionLocal = None


def _login(app, u, p):
    """CSRF is ON in this app, so log in the way a browser does: fetch the form token."""
    c = app.test_client()
    html = c.get("/login").get_data(as_text=True)
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    data = {"username": u, "password": p}
    if m:
        data["csrf_token"] = m.group(1)
    r = c.post("/login", data=data)
    assert r.status_code in (200, 302), r.status_code
    return c


def _csrf_token(client):
    html = client.get("/change-password").get_data(as_text=True)
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


# ------------------------------------------------------------- _admin_csrf semantics

def test_guarded_write_route_rejects_anonymous_401(app):
    r = app.test_client().post("/api/guarded")
    assert r.status_code == 401


def test_guarded_write_route_rejects_non_admin_403(app):
    c = _login(app, "joe", "joepass123")
    assert c.post("/api/guarded").status_code == 403


def test_guarded_write_route_rejects_admin_without_csrf_400(app):
    c = _login(app, "root", "rootpass1")
    assert c.post("/api/guarded").status_code == 400


def test_guarded_write_route_accepts_admin_with_csrf(app):
    c = _login(app, "root", "rootpass1")
    tok = _csrf_token(c)
    assert tok, "csrf token must be obtainable from a rendered form"
    r = c.post("/api/guarded", headers={"X-CSRFToken": tok})
    assert r.status_code == 200 and r.get_json() == {"ok": True}


# ------------------------------------------------------------- pipeline_authz mappings

def test_duplicate_subpath_requires_view_on_source():
    assert _SUBPATH_PERMISSION["duplicate"] == "view"
    assert _required_permission("duplicate", "POST") == "view"


def test_thumbnail_generate_is_an_edit_not_a_read():
    assert _SUBPATH_PERMISSION["thumbnail/generate"] == "edit"


def test_unknown_subpath_stays_fail_strict_edit():
    assert _required_permission("something/new", "GET") == "edit"


# ------------------------------------------------------------- scoped stats/summary

def test_pipeline_stats_and_summary_are_scoped_to_caller_records(app, tmp_path):
    admin = app.users["root"]
    pm = PipelineManager(str(tmp_path / "repo"))
    for i in range(3):
        pid, d = pm.build_pipeline_definition({"name": f"p{i}", "frame_source": {"capture_type": "webcam", "config": {}},
                                               "model": {"id": "m", "engine_type": "ultralytics", "device": "cpu"},
                                               "destinations": []})
        ps.create_pipeline(admin, pipeline_id=pid, name=f"p{i}", config=d)
    everything = ps.repository.list(is_admin=True)
    assert pm.get_pipeline_stats()["total"] == 3                       # unscoped internal caller
    assert pm.get_pipeline_stats(records=[])["total"] == 0             # a user with no grants sees 0
    assert pm.get_pipeline_stats(records=everything[:1])["total"] == 1
    assert pm.get_pipeline_summary(records=[])["total_pipelines"] == 0
    assert pm.get_pipeline_summary(records=everything[:2])["total_pipelines"] == 2


# ------------------------------------------------------------- password policy server-side

def test_create_user_enforces_min_password_length(app):
    admin = app.users["root"]
    with pytest.raises(svc.UserOpError):
        svc.create_user(admin, username="short", password="a", role="user")


def test_reset_password_enforces_min_password_length(app):
    admin = app.users["root"]
    joe = app.users["joe"]
    with pytest.raises(svc.UserOpError):
        svc.reset_password(admin, joe.id, "abc")


# ------------------------------------------------------------- source-level guards

def test_discovery_control_route_is_bound_to_its_own_handler():
    src = open(os.path.join(REPO, "InferenceNode", "inference_node.py"), encoding="utf-8").read()
    # the control decorator must be immediately followed by ITS handler, not webhook_receiver
    m = re.search(r"@self\.app\.route\('/api/discovery/nodes/<node_id>/control', methods=\['POST'\]\)\s*\n\s*def (\w+)", src)
    assert m and m.group(1) == "control_discovered_node", (m and m.group(1))


def test_import_checks_admin_before_touching_files():
    src = open(os.path.join(REPO, "InferenceNode", "inference_node.py"), encoding="utf-8").read()
    body = src[src.index("def import_pipeline"):src.index("def import_pipeline") + 1500]
    assert body.index("require_admin") < body.index("request.files")


def test_state_changing_non_pipeline_routes_are_admin_csrf_guarded():
    src = open(os.path.join(REPO, "InferenceNode", "inference_node.py"), encoding="utf-8").read()
    for route in ["/api/models/upload", "/api/models/download-ultralytics", "/api/publisher/configure",
                  "/api/publisher/favorites', methods=['POST']", "/api/telemetry/configure",
                  "/api/node/config", "/api/node/restart", "/api/logs/clear", "/api/media/upload-video"]:
        i = src.index(f"@self.app.route('{route}")
        following = src[i:i + 300]
        assert "@self._admin_csrf" in following, f"{route} is not admin+CSRF guarded"


def test_pipeline_surfaces_are_sanitized_at_the_route():
    src = open(os.path.join(REPO, "InferenceNode", "inference_node.py"), encoding="utf-8").read()
    for fn in ("get_pipeline_full_status", "get_pipeline_publishers_status", "get_pipeline_summary", "export_pipeline"):
        body = src[src.index(f"def {fn}"):src.index(f"def {fn}") + 2500]
        assert "sanitize" in body, f"{fn} returns unsanitized config"


def test_admin_read_only_registry_surfaces_are_admin_guarded():
    """Reconciliation / verification reports name every artifact and every inconsistency:
    admin-only (login alone is not enough)."""
    src = open(os.path.join(REPO, "InferenceNode", "inference_node.py"), encoding="utf-8").read()
    for route in ["/api/registry/verify", "/api/models/verify"]:
        i = src.index(f"@self.app.route('{route}")
        following = src[i:i + 300]
        assert "@self._admin_required" in following or "@self._admin_csrf" in following, f"{route} is not admin guarded"
    eb_src = open(os.path.join(REPO, "InferenceNode", "engine_builder.py"), encoding="utf-8").read()
    i = eb_src.index('@app.route("/api/inference/engines/registry"')
    assert "@builder_gate" in eb_src[i:i + 200]
