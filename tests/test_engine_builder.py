"""Engine builder tests: name/key derivation, preset generation (compile-checked),
static AST validation, atomic install (injected deps: temp dir, fake rediscover/verify),
and Flask gating (404 unless admin AND ENABLE_ENGINE_BUILDER)."""
import os
import sys
import contextlib

import pytest
from flask import Flask

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode import engine_builder as eb           # noqa: E402
from InferenceNode.auth import db as auth_db             # noqa: E402
from InferenceNode.auth import service as svc            # noqa: E402
from InferenceNode.auth.models import Base               # noqa: E402
from InferenceNode.auth.flask_auth import setup_auth     # noqa: E402

TEMPLATES = os.path.join(REPO, "InferenceNode", "templates")
STATIC = os.path.join(REPO, "InferenceNode", "static")


# --- derivation ---
@pytest.mark.parametrize("display,cls,key", [
    ("Thermal", "ThermalEngine", "thermal"),
    ("Thermal Detector", "ThermalDetectorEngine", "thermal_detector"),
    ("custom object detection", "CustomObjectDetectionEngine", "custom_object_detection"),
    # Acronyms mangle - this is the factory's documented behavior and the builder
    # must reproduce it exactly, otherwise the installed file would not be
    # discoverable under the key we report to the user.
    ("PG Verify Cam", "PGVerifyCamEngine", "p_g_verify_cam"),
])
def test_derivation(display, cls, key):
    c = eb.class_name_from_display(display)
    assert c == cls
    assert eb.key_from_class(c) == key


def test_key_matches_factory_converter():
    """The builder's key derivation must agree with InferenceEngineFactory's, or an
    installed engine would register under a different key than the UI promised."""
    from InferenceEngine.inference_engine_factory import InferenceEngineFactory as F
    for cls_name in ("ThermalEngine", "PGVerifyCamEngine", "MyAIEngine",
                     "CustomObjectDetectionEngine", "OnnxEngine"):
        assert eb.key_from_class(cls_name) == F._class_name_to_key(cls_name), cls_name


# --- generation compiles + validates for every preset ---
@pytest.mark.parametrize("preset", list(eb.PRESETS))
def test_generate_valid_and_compiles(preset):
    src = eb.generate_engine_source(preset, {"display_name": "Thermal Cam",
                                             "extensions": [".onnx"], "confidence": 0.5,
                                             "task": "detection", "draw_color": [10, 20, 30]})
    compile(src, "<gen>", "exec")           # valid Python (no execution)
    v = eb.validate_source(src)
    assert v["valid"] is True and v["class_name"] == "ThermalCamEngine"
    assert v["engine_key"] == "thermal_cam" and not v["missing_methods"]


# --- validation catches problems ---
def test_validate_syntax_error():
    assert eb.validate_source("def (:").get("valid") is False

def test_validate_no_subclass():
    v = eb.validate_source("class Foo:\n    pass\n")
    assert v["valid"] is False and "No BaseInferenceEngine" in v["error"]

def test_validate_missing_methods():
    code = ("class XEngine(BaseInferenceEngine):\n"
            "    def _load_model(self,m,d): return True\n")
    v = eb.validate_source(code)
    assert v["valid"] is False and "check_valid_model" in v["missing_methods"]

def test_validate_two_subclasses():
    src = eb.generate_engine_source("blank", {"display_name": "A"})
    src2 = src + "\nclass BEngine(BaseInferenceEngine):\n    pass\n"
    assert eb.validate_source(src2)["valid"] is False


# --- atomic install (injected) ---
def _lock():
    return contextlib.nullcontext()

def test_install_success(tmp_path):
    src = eb.generate_engine_source("blank", {"display_name": "Widget Cam"})
    calls = {"rediscover": 0}
    def rediscover(): calls["rediscover"] += 1
    info = eb.install_engine(src, engines_dir=str(tmp_path), existing_keys=lambda: set(),
                             rediscover=rediscover, verify_key=lambda k: True, lock=_lock, register=False)
    assert info["engine_key"] == "widget_cam"
    # managed layout: <root>/<key>/engine.py (never the source tree, never a flat file)
    assert os.path.exists(tmp_path / "widget_cam" / "engine.py")
    assert not os.path.exists(tmp_path / ".staging" / "widget_cam" / "engine.py")
    assert info["state"] == "AVAILABLE" and info["origin"] == "custom"
    assert len(info["sha256"]) == 64 and calls["rediscover"] == 1

def test_install_rejects_existing_key(tmp_path):
    src = eb.generate_engine_source("blank", {"display_name": "Dup"})
    with pytest.raises(eb.EngineInstallError):
        eb.install_engine(src, engines_dir=str(tmp_path), existing_keys=lambda: {"dup"},
                          rediscover=lambda: None, verify_key=lambda k: True, lock=_lock, register=False)

def test_install_rejects_existing_file(tmp_path):
    (tmp_path / "dup").mkdir(); (tmp_path / "dup" / "engine.py").write_text("x")
    src = eb.generate_engine_source("blank", {"display_name": "Dup"})
    with pytest.raises(eb.EngineInstallError):
        eb.install_engine(src, engines_dir=str(tmp_path), existing_keys=lambda: set(),
                          rediscover=lambda: None, verify_key=lambda k: True, lock=_lock, register=False)

def test_install_quarantines_on_rediscovery_failure(tmp_path):
    src = eb.generate_engine_source("blank", {"display_name": "Bad One"})
    with pytest.raises(eb.EngineInstallError) as ei:
        eb.install_engine(src, engines_dir=str(tmp_path), existing_keys=lambda: set(),
                          rediscover=lambda: None, verify_key=lambda k: False, lock=_lock, register=False)
    # structured failure state - no text matching needed by callers
    assert getattr(ei.value, "state", {}) == {"state": "FAILED", "quarantined": True}
    assert not os.path.exists(tmp_path / "bad_one" / "engine.py")
    assert os.path.exists(tmp_path / "bad_one" / "engine.py.quarantine")


# --- Flask gating ---
@pytest.fixture()
def gated_app(tmp_path, monkeypatch):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'g.db'}")
    Base.metadata.create_all(engine)
    class _S: id, username = 0, "seed"
    svc.create_user(_S, username="root", password="rootpass1", role="admin", must_change_password=False)
    svc.create_user(_S, username="joe", password="joepass123", role="user", must_change_password=False)
    app = Flask(__name__, template_folder=TEMPLATES, static_folder=STATIC)
    app.secret_key = "t"; app.config["WTF_CSRF_ENABLED"] = False
    setup_auth(app, bootstrap=False)
    eb.register_engine_builder(app)
    @app.route("/")
    def dashboard(): return "home", 200
    yield app, monkeypatch
    auth_db._engine = None; auth_db._SessionLocal = None

def _login(app, u, p):
    c = app.test_client(); c.post("/login", data={"username": u, "password": p}); return c

def test_builder_404_when_disabled(gated_app, monkeypatch):
    app, mp = gated_app
    monkeypatch.delenv("ENABLE_ENGINE_BUILDER", raising=False)
    c = _login(app, "root", "rootpass1")
    assert c.get("/create-engine").status_code == 404

def test_builder_404_for_non_admin(gated_app, monkeypatch):
    app, mp = gated_app
    monkeypatch.setenv("ENABLE_ENGINE_BUILDER", "true")
    c = _login(app, "joe", "joepass123")
    assert c.get("/create-engine").status_code == 404

def test_builder_page_and_preview_for_admin(gated_app, monkeypatch):
    app, mp = gated_app
    monkeypatch.setenv("ENABLE_ENGINE_BUILDER", "true")
    c = _login(app, "root", "rootpass1")
    assert c.get("/create-engine").status_code == 200
    r = c.post("/api/inference/engines/preview", json={"preset": "onnx",
               "fields": {"display_name": "Heat Cam"}})
    d = r.get_json()
    assert r.status_code == 200 and d["engine_key"] == "heat_cam" and "class HeatCamEngine" in d["code"]
