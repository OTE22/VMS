"""Phase 13 - unified registry <-> filesystem reconciliation (artifact_verify).

One report over models / engines / thumbnails / media / secrets / pipeline->model refs.
Report ONLY (nothing deleted, nothing repaired); fail-closed verdict; no host paths."""
import contextlib
import hashlib
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db                        # noqa: E402
from InferenceNode.auth import service as svc                       # noqa: E402
from InferenceNode.auth.models import Base                          # noqa: E402
import InferenceNode.data_models  # noqa: E402,F401
from InferenceNode import artifact_paths as ap                      # noqa: E402
from InferenceNode import artifact_verify as av                     # noqa: E402
from InferenceNode import config_secrets as cs                      # noqa: E402
from InferenceNode import engine_builder as eb                      # noqa: E402
from InferenceNode import media_registry as media                   # noqa: E402
from InferenceNode import pipeline_store as ps                      # noqa: E402
from InferenceNode import publisher_store as pst                    # noqa: E402
from InferenceNode import thumbnail_registry as thumbs              # noqa: E402
from InferenceNode.model_repo import ModelRepository                # noqa: E402

_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912"
    "130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b0800"
    "02000201011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002"
    "010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f024"
    "33627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a"
    "737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6"
    "c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9")


class _Seed:
    id = None; username = "seed"; role = "admin"; is_authenticated = True


class _Upload:
    def __init__(self, data): self._d = data
    def save(self, path):
        with open(path, "wb") as f:
            f.write(self._d)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'v.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("ARMYEYE_ARTIFACT_ROOT", str(tmp_path / "root"))
    monkeypatch.delenv("ARMYEYE_MEDIA_ROOT", raising=False)
    ap.ensure_layout()
    key_file = tmp_path / "config.key"
    key_file.write_text(cs.generate_key_line("armyeye-config-2026-01") + "\n")
    if os.name != "nt":
        os.chmod(key_file, 0o600)
    monkeypatch.setenv(cs.KEY_FILE_ENV, str(key_file))
    assert cs.reload_keys()
    admin = svc.create_user(_Seed, username="root", password="rootpass1", role="admin", must_change_password=False)
    repo = ModelRepository(str(tmp_path / "legacy"))
    yield {"tmp": tmp_path, "admin": admin, "repo": repo}
    cs.reload_keys("/nonexistent")
    auth_db._engine = None; auth_db._SessionLocal = None


def _seed_all(env):
    """One healthy artifact per plane."""
    tmp = env["tmp"]
    src = tmp / "up" / "m.pt"; src.parent.mkdir(exist_ok=True); src.write_bytes(b"weights")
    model_id = env["repo"].store_model(str(src), "m.pt", "ultralytics", "d", "m", uploader_id=1, uploader_username="root")
    model = {"id": model_id}
    eng = eb.install_engine(eb.generate_engine_source("blank", {"display_name": "Ver Cam"}),
                            existing_keys=lambda: set(), rediscover=lambda: None,
                            verify_key=lambda k: True, lock=lambda: contextlib.nullcontext())
    ps.create_pipeline(env["admin"], pipeline_id="v-1", name="v", config={
        "name": "v", "frame_source": {"type": "video_file", "config": {"relative_source": "a.mp4"}},
        "model": {"id": model["id"]}, "destinations": []})
    staged = ap.staging_path("thumbnails", thumbs.relative_path_for("v-1"))
    os.makedirs(os.path.dirname(staged), exist_ok=True)
    open(staged, "wb").write(_JPEG)
    thumbs.register_from_staged("v-1", staged)
    med = media.ingest_upload(_Upload(b"\x00" * 100), original_filename="a.mp4", timestamp="20260101_000000")
    pub = pst.create_publisher(name="mq", type="mqtt", config={"server": "b", "password": "hunter2"})
    return {"model": model, "engine": eng, "media": med, "publisher": pub}


def test_healthy_state_reports_healthy(env):
    _seed_all(env)
    rep = av.verify_all(env["repo"])
    assert rep["summary"]["verdict"] == "healthy", rep["summary"]
    assert rep["summary"]["problems"] == []
    assert rep["models"]["available_valid"] >= 1
    assert "ver_cam" in rep["engines"]["available_valid"]
    assert rep["thumbnails"]["available_valid"] == [thumbs.relative_path_for("v-1")]
    assert rep["media"]["available_valid"] == ["20260101_000000_a.mp4"]
    assert rep["secrets"]["keys_available"] is True and rep["secrets"]["publishers_undecryptable"] == []
    assert rep["pipeline_model_refs"] == {"total": 1, "consistent": 1, "divergent": [], "unknown_model": [], "no_model": 0}


def test_report_contains_no_host_paths(env):
    _seed_all(env)
    rep = av.verify_all(env["repo"])
    blob = json.dumps(rep)
    root = os.path.realpath(ap.artifact_root())
    assert root not in blob and root.replace("\\", "/") not in blob and str(env["tmp"]) not in blob
    assert "artifact_root" in rep and rep["artifact_root"]["present"] is True


def test_tampered_and_missing_artifacts_are_detected_and_nothing_is_deleted(env):
    seeded = _seed_all(env)
    # tamper engine, delete thumbnail, tamper media, delete model bytes
    eng_path = ap.resolve("engines", "ver_cam/engine.py"); open(eng_path, "a").write("\n#x\n")
    thumb_path = ap.resolve("thumbnails", thumbs.relative_path_for("v-1")); os.remove(thumb_path)
    med_path = ap.resolve("media", "20260101_000000_a.mp4"); open(med_path, "ab").write(b"!")
    model_path = env["repo"].get_model_path(seeded["model"]["id"]); os.remove(model_path)
    before = sorted(os.listdir(ap.kind_root("engines"))) + sorted(os.listdir(ap.kind_root("media")))
    rep = av.verify_all(env["repo"])
    s = rep["summary"]
    assert s["verdict"] == "degraded"
    assert "ver_cam" in rep["engines"]["available_hash_mismatch"] or "ver_cam" not in rep["engines"]["available_valid"]
    assert rep["thumbnails"]["available_missing"] == [thumbs.relative_path_for("v-1")] or \
        thumbs.get_for_pipeline("v-1")["status"] == "MISSING"
    assert rep["media"]["available_hash_mismatch"] == ["20260101_000000_a.mp4"] or media.get_by_path("20260101_000000_a.mp4")["status"] == "CORRUPT"
    assert rep["models"]["available_missing"] >= 1 or rep["models"]["available_valid"] == 0
    assert any(p.startswith(("engines", "media", "thumbnails", "models")) for p in s["problems"])
    after = sorted(os.listdir(ap.kind_root("engines"))) + sorted(os.listdir(ap.kind_root("media")))
    assert before == after, "verifier never deletes"


def test_orphan_files_are_warnings_not_problems(env):
    _seed_all(env)
    open(os.path.join(ap.kind_root("thumbnails"), "thumbnail_ghost.jpg"), "wb").write(_JPEG)
    open(os.path.join(ap.kind_root("media"), "ghost.mp4"), "wb").write(b"g")
    rep = av.verify_all(env["repo"])
    assert rep["summary"]["verdict"] == "healthy"
    assert any("thumbnails" in w for w in rep["summary"]["warnings"])
    assert any("media" in w for w in rep["summary"]["warnings"])
    assert os.path.exists(os.path.join(ap.kind_root("thumbnails"), "thumbnail_ghost.jpg"))


def test_undecryptable_secrets_degrade_and_are_not_destroyed(env):
    seeded = _seed_all(env)
    cs.reload_keys("/nonexistent")
    rep = av.verify_all(env["repo"])
    assert rep["secrets"]["keys_available"] is False
    assert seeded["publisher"]["id"] in rep["secrets"]["publishers_undecryptable"]
    assert rep["summary"]["verdict"] == "degraded" and any("secrets" in p for p in rep["summary"]["problems"])
    assert json.dumps(rep).find("hunter2") == -1
    assert pst.get_publisher(seeded["publisher"]["id"]) is not None       # config retained


def test_pipeline_model_reference_drift_and_unknown_are_reported_not_fixed(env):
    _seed_all(env)
    from sqlalchemy import select
    from InferenceNode.auth.db import get_session
    from InferenceNode.data_models import Pipeline
    with get_session() as s:                       # simulate drift by direct write (bypassing the repository)
        p = s.execute(select(Pipeline).where(Pipeline.pipeline_id == "v-1")).scalar_one()
        cfg = dict(p.config); cfg["model"] = {"id": "somebody-else"}; p.config = cfg
    ps.create_pipeline(env["admin"], pipeline_id="v-2", name="v2", config={
        "name": "v2", "frame_source": {"type": "video_file", "config": {"relative_source": "a.mp4"}},
        "model": {"id": "unknown-model-xyz"}, "destinations": []})
    rep = av.verify_all(env["repo"])
    refs = rep["pipeline_model_refs"]
    assert [d["pipeline_id"] for d in refs["divergent"]] == ["v-1"]
    assert [u["pipeline_id"] for u in refs["unknown_model"]] == ["v-2"]
    assert rep["summary"]["verdict"] == "degraded"
    with get_session() as s:                       # nothing was "repaired"
        p = s.execute(select(Pipeline).where(Pipeline.pipeline_id == "v-1")).scalar_one()
        assert p.config["model"]["id"] == "somebody-else"


def test_one_broken_plane_does_not_hide_the_others(env, monkeypatch):
    _seed_all(env)
    from InferenceNode import engine_registry
    monkeypatch.setattr(engine_registry, "verify_all", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    rep = av.verify_all(env["repo"])
    assert "error" in rep["engines"]
    assert rep["thumbnails"]["available_valid"] and rep["media"]["available_valid"]
    assert rep["summary"]["verdict"] == "degraded" and "engines: verifier error" in rep["summary"]["problems"]
