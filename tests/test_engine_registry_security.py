"""Phase 11 - engine registry (PostgreSQL) + protected persistent artifact root.

Security boundary + lifecycle for CUSTOM engines (executable code):
  admin-only creation (Flask gating covered in test_engine_builder.py), CSRF, no path
  traversal / absolute-path injection / symlink escape / overwrite outside the engine
  root or of ArmyEye source modules, validation before activation, atomic promotion,
  sha256 stored, startup integrity (hash mismatch / missing artifact detected),
  builtin vs custom semantics, enabled => AVAILABLE + PASSED.
SQLite here; the same file runs on isolated PostgreSQL via scripts/pg-test.sh."""
import contextlib
import hashlib
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db                        # noqa: E402
from InferenceNode.auth.models import Base                          # noqa: E402
import InferenceNode.data_models  # noqa: E402,F401
from InferenceNode import artifact_paths as ap                      # noqa: E402
from InferenceNode import engine_builder as eb                      # noqa: E402
from InferenceNode import engine_registry as reg                    # noqa: E402
from InferenceNode.artifact_states import ArtifactStatus as S, ValidationStatus as V  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'e.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("ARMYEYE_ARTIFACT_ROOT", str(tmp_path / "root"))
    ap.ensure_layout()
    yield tmp_path
    auth_db._engine = None; auth_db._SessionLocal = None


def _lock():
    return contextlib.nullcontext()


def _install(name, **kw):
    src = eb.generate_engine_source("blank", {"display_name": name})
    return eb.install_engine(src, existing_keys=lambda: set(), rediscover=lambda: None,
                             verify_key=lambda k: True, lock=_lock, **kw)


# ------------------------------------------------------------------ happy path: registry + artifact root

def test_custom_engine_uses_artifact_root_and_registry(env):
    info = _install("Gate Cam")
    assert info["engine_key"] == "gate_cam" and info["origin"] == "custom" and info["state"] == "AVAILABLE"
    path = ap.resolve("engines", "gate_cam/engine.py")
    assert os.path.isfile(path)
    assert path.startswith(os.path.realpath(ap.kind_root("engines")))
    # never in the application source tree
    assert not os.path.exists(os.path.join(REPO, "InferenceEngine", "engines", "gate_cam_engine.py"))
    row = reg.get("gate_cam")
    assert row["origin"] == "custom" and row["status"] == "AVAILABLE" and row["validation_status"] == "PASSED"
    assert row["enabled"] is True and row["sha256"] == hashlib.sha256(open(path, "rb").read()).hexdigest()
    assert row["size_bytes"] == os.path.getsize(path) and row["relative_path"] == "gate_cam/engine.py"
    assert reg.servable_custom_path("gate_cam") == path


def test_engine_status_and_validation_status_are_separate(env):
    _install("Sep Cam")
    reg.set_state("sep_cam", S.VALIDATING, V.PENDING)
    reg.set_state("sep_cam", S.CORRUPT, V.HASH_MISMATCH)
    row = reg.get("sep_cam")
    assert row["status"] == "CORRUPT" and row["validation_status"] == "HASH_MISMATCH" and row["enabled"] is False


def test_non_available_engine_cannot_be_enabled(env):
    _install("Dis Cam")
    for st, vs in ((S.VALIDATING, V.PENDING), (S.FAILED, V.FAILED)):
        reg.set_state("dis_cam", st, vs, enabled=True)         # enabled request is IGNORED off-AVAILABLE
        assert reg.get("dis_cam")["enabled"] is False
        assert reg.servable_custom_path("dis_cam") is None


# ------------------------------------------------------------------ security boundary

@pytest.mark.parametrize("bad_name", ["../../evil", "..%2f..%2fevil", "/etc/evil", "C:/win/evil"])
def test_traversal_and_absolute_names_cannot_escape(env, bad_name):
    """Two independent guards. (1) The engine key is derived from a sanitized class name
    ([a-z0-9_] only), so traversal / absolute paths are UN-EXPRESSIBLE: the hostile
    display name collapses to a plain key and the artifact lands safely under
    <root>/<key>/engine.py. (2) The resolver rejects any escaping relative path anyway."""
    import re
    src = eb.generate_engine_source("blank", {"display_name": bad_name})
    info = eb.install_engine(src, existing_keys=lambda: set(), rediscover=lambda: None,
                             verify_key=lambda k: True, lock=_lock)
    assert re.fullmatch(r"[a-z][a-z0-9_]*", info["engine_key"]), info["engine_key"]
    root = os.path.realpath(ap.kind_root("engines"))
    path = ap.resolve("engines", info["relative_path"])
    assert path.startswith(root + os.sep) and os.path.isfile(path)
    assert not os.path.exists(os.path.join(REPO, "evil.py")) and not os.path.exists("/etc/evil.py")
    for rel in ("../../evil.py", "%2e%2e/evil.py", "/etc/evil.py", "C:/win/evil.py"):
        with pytest.raises(ap.ArtifactPathError):
            ap.resolve("engines", rel)


def test_existing_source_module_name_is_reserved(env):
    src = eb.generate_engine_source("blank", {"display_name": "Base"})     # -> base_engine.py (SKIP_NAMES)
    with pytest.raises(eb.EngineInstallError):
        eb.install_engine(src, existing_keys=lambda: set(), rediscover=lambda: None,
                          verify_key=lambda k: True, lock=_lock)


def test_invalid_python_is_rejected_before_any_write(env):
    with pytest.raises(eb.EngineInstallError):
        eb.install_engine("def broken(:", existing_keys=lambda: set(), rediscover=lambda: None,
                          verify_key=lambda k: True, lock=_lock)
    assert reg.list_engines(origin="custom") == []
    assert not os.listdir(os.path.join(ap.kind_root("engines"), ".staging"))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unsupported")
def test_symlink_escape_is_refused(env, tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    link = os.path.join(ap.kind_root("engines"), "esc")
    try:
        os.symlink(str(outside), link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlink here")
    with pytest.raises(ap.ArtifactPathError):
        ap.resolve("engines", "esc/engine.py")


def test_hash_mismatch_detected_and_engine_not_loaded(env):
    _install("Tamper Cam")
    path = ap.resolve("engines", "tamper_cam/engine.py")
    open(path, "a").write("\n# tampered\n")
    assert reg.servable_custom_path("tamper_cam") is None
    row = reg.get("tamper_cam")
    assert row["status"] == "CORRUPT" and row["validation_status"] == "HASH_MISMATCH" and row["enabled"] is False
    rep = reg.verify_all()
    assert "tamper_cam" not in rep["available_valid"]


def test_missing_artifact_detected(env):
    _install("Gone Cam")
    os.remove(ap.resolve("engines", "gone_cam/engine.py"))
    assert reg.servable_custom_path("gone_cam") is None
    assert reg.get("gone_cam")["status"] == "MISSING"


def test_failed_install_is_quarantined_and_registered_failed(env):
    src = eb.generate_engine_source("blank", {"display_name": "Bad Cam"})
    with pytest.raises(eb.EngineInstallError) as ei:
        eb.install_engine(src, existing_keys=lambda: set(), rediscover=lambda: None,
                          verify_key=lambda k: False, lock=_lock)
    assert ei.value.state == {"state": "FAILED", "quarantined": True}
    row = reg.get("bad_cam")
    assert row["status"] == "FAILED" and row["enabled"] is False
    assert not os.path.exists(ap.resolve("engines", "bad_cam/engine.py"))
    assert reg.servable_custom_path("bad_cam") is None


# ------------------------------------------------------------------ builtin vs custom

def test_builtin_engine_cannot_be_deleted_as_custom(env):
    reg.register_builtin("ultralytics", "UltralyticsEngine", "Ultralytics")
    row = reg.get("ultralytics")
    assert row["origin"] == "builtin" and row["relative_path"] is None and row["enabled"] is True
    with pytest.raises(eb.EngineInstallError):
        eb.delete_engine("ultralytics")
    assert reg.get("ultralytics") is not None
    assert reg.servable_custom_path("ultralytics") is None    # builtins are not custom artifacts


def test_custom_delete_is_batch_safe_and_removes_row(env):
    _install("Del Cam")
    path = ap.resolve("engines", "del_cam/engine.py")
    assert eb.delete_engine("del_cam")["state"] == "DELETED"
    assert reg.get("del_cam") is None and not os.path.exists(path)


def test_delete_failure_preserves_engine(env, monkeypatch):
    _install("Keep Cam")
    path = ap.resolve("engines", "keep_cam/engine.py")
    monkeypatch.setattr(reg, "remove", lambda k: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(eb.EngineInstallError):
        eb.delete_engine("keep_cam")
    assert os.path.exists(path), "artifact restored from trash after DB failure"
    assert reg.get("keep_cam") is not None


def test_install_succeeds_with_registry_gated_factory_discovery(env):
    """Regression (found by browser E2E): the factory loads ONLY AVAILABLE custom engines,
    so discoverability must be verified AFTER the AVAILABLE transition (dry-run import in
    VALIDATING) - otherwise every install quarantines itself. Uses the REAL factory
    rediscovery + verify path (no stubs)."""
    from InferenceEngine.inference_engine_factory import InferenceEngineFactory as F
    src = eb.generate_engine_source("blank", {"display_name": "Real Path Cam"})
    info = eb.install_engine(src, existing_keys=lambda: set(F.get_available_types()),
                             rediscover=F.rediscover_engines,
                             verify_key=lambda k: k in set(F.get_available_types()), lock=_lock)
    assert info["state"] == "AVAILABLE"
    row = reg.get("real_path_cam")
    assert row["status"] == "AVAILABLE" and row["validation_status"] == "PASSED" and row["enabled"] is True
    assert "real_path_cam" in set(F.get_available_types())
    # a post-promotion discoverability failure never leaves AVAILABLE behind
    src2 = eb.generate_engine_source("blank", {"display_name": "Ghost Cam"})
    with pytest.raises(eb.EngineInstallError):
        eb.install_engine(src2, existing_keys=lambda: set(), rediscover=lambda: None,
                          verify_key=lambda k: False, lock=_lock)
    g = reg.get("ghost_cam")
    assert g["status"] == "FAILED" and g["enabled"] is False
    assert not os.path.exists(ap.resolve("engines", "ghost_cam/engine.py"))
