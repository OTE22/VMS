"""Phase 12 - media + thumbnail registries (PostgreSQL rows + ARTIFACT_ROOT bytes).

Thumbnails: sha256/size/status/validation_status tracked; "file exists" is never proof;
creation via staged capture -> validate -> hash -> promote -> AVAILABLE; pipeline delete
coordinates the JPEG (DELETING -> trash -> row delete -> purge; DB failure restores).
Media: upload creation state machine (STAGING never served), physical migration of the
legacy media dir with relative paths preserved, registry-backed resolution.
SQLite here; the same file runs on isolated PostgreSQL via scripts/pg-test.sh."""
import hashlib
import io
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
from InferenceNode import pipeline_store as ps                      # noqa: E402
from InferenceNode import thumbnail_registry as thumbs              # noqa: E402
from InferenceNode import media_registry as media                   # noqa: E402
from InferenceNode import registry_migration as rm                  # noqa: E402
from InferenceNode import app_state                                 # noqa: E402
from InferenceNode.media_library import MediaLibrary, MediaError, default_media_root  # noqa: E402
from InferenceNode.pipeline_manager import PipelineManager          # noqa: E402
from InferenceNode.artifact_states import ArtifactStatus as S, ValidationStatus as V  # noqa: E402

# a minimal valid JPEG (2x2) so decode validation passes with or without cv2
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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'p.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("ARMYEYE_ARTIFACT_ROOT", str(tmp_path / "root"))
    monkeypatch.delenv("ARMYEYE_MEDIA_ROOT", raising=False)
    ap.ensure_layout()
    admin = svc.create_user(_Seed, username="root", password="rootpass1", role="admin",
                            must_change_password=False)
    viewer = svc.create_user(_Seed, username="joe", password="joepass123", role="user",
                             must_change_password=False)
    pm = PipelineManager(str(tmp_path / "repo"))
    yield {"admin": admin, "viewer": viewer, "pm": pm, "tmp": tmp_path}
    auth_db._engine = None; auth_db._SessionLocal = None


def _mk_pipeline(env, pid="p-1"):
    ps.create_pipeline(env["admin"], pipeline_id=pid, name=pid, config={"name": pid,
                       "frame_source": {"type": "video_file", "config": {"relative_source": "a.mp4"}},
                       "model": {"id": None}, "destinations": []})
    return pid


def _stage_thumb(pid, data=_JPEG):
    staged = ap.staging_path("thumbnails", thumbs.relative_path_for(pid))
    os.makedirs(os.path.dirname(staged), exist_ok=True)
    with open(staged, "wb") as f:
        f.write(data)
    return staged


# ------------------------------------------------------------------ thumbnails
def test_thumbnail_has_sha256_and_integrity_status(env):
    pid = _mk_pipeline(env)
    row = thumbs.register_from_staged(pid, _stage_thumb(pid))
    assert row["status"] == "AVAILABLE" and row["validation_status"] == "PASSED"
    assert row["sha256"] == hashlib.sha256(_JPEG).hexdigest() and row["size_bytes"] == len(_JPEG)
    final = ap.resolve("thumbnails", row["relative_path"])
    assert os.path.isfile(final) and not os.path.exists(ap.staging_path("thumbnails", row["relative_path"]))
    assert env["pm"].get_pipeline_thumbnail_path(pid) == final
    # tamper -> not served, CORRUPT + HASH_MISMATCH (file existing is not proof)
    with open(final, "ab") as f:
        f.write(b"x")
    assert env["pm"].get_pipeline_thumbnail_path(pid) is None
    t = thumbs.get_for_pipeline(pid)
    assert t["status"] == "CORRUPT" and t["validation_status"] == "HASH_MISMATCH"
    # missing -> MISSING
    thumbs.register_from_staged(pid, _stage_thumb(pid))
    os.remove(final)
    assert thumbs.servable_path(pid) is None and thumbs.get_for_pipeline(pid)["status"] == "MISSING"


def test_thumbnail_registration_failure_never_yields_available(env):
    pid = _mk_pipeline(env)
    assert thumbs.register_from_staged(pid, _stage_thumb(pid, b"not a jpeg")) is None
    t = thumbs.get_for_pipeline(pid)
    assert t["status"] == "FAILED" and t["validation_status"] == "FAILED"
    assert thumbs.servable_path(pid) is None
    assert not os.path.exists(ap.resolve("thumbnails", thumbs.relative_path_for(pid)))


def test_unregistered_thumbnail_file_is_not_served_and_is_reported(env):
    pid = _mk_pipeline(env)
    final = ap.resolve("thumbnails", thumbs.relative_path_for(pid))
    with open(final, "wb") as f:
        f.write(_JPEG)
    assert env["pm"].get_pipeline_thumbnail_path(pid) is None      # exists, but not registered
    assert thumbs.relative_path_for(pid) in thumbs.verify_all()["orphan_files"]


def test_pipeline_delete_cleans_registered_thumbnail_safely(env):
    pid = _mk_pipeline(env)
    thumbs.register_from_staged(pid, _stage_thumb(pid))
    final = ap.resolve("thumbnails", thumbs.relative_path_for(pid))
    assert env["pm"].delete_pipeline_coordinated(env["admin"], pid) == {"outcome": "deleted"}
    assert not ps.repository.exists(pid)
    assert not os.path.exists(final)
    assert not os.path.exists(ap.trash_path("thumbnails", thumbs.relative_path_for(pid)))
    assert thumbs.get_for_pipeline(pid) is None
    assert thumbs.verify_all()["orphan_files"] == []


def test_pipeline_delete_failure_restores_or_preserves_thumbnail(env, monkeypatch):
    pid = _mk_pipeline(env)
    row = thumbs.register_from_staged(pid, _stage_thumb(pid))
    final = ap.resolve("thumbnails", row["relative_path"])
    monkeypatch.setattr(ps, "delete_pipeline", lambda u, p: (_ for _ in ()).throw(RuntimeError("db down")))
    out = env["pm"].delete_pipeline_coordinated(env["admin"], pid)
    assert out["outcome"] == "db_failed"
    assert ps.repository.exists(pid), "row untouched"
    assert os.path.isfile(final), "JPEG restored from trash"
    assert hashlib.sha256(open(final, "rb").read()).hexdigest() == row["sha256"]
    t = thumbs.get_for_pipeline(pid)
    assert t is not None and t["status"] in ("DELETING", "AVAILABLE")     # never AVAILABLE+missing
    if t["status"] == "AVAILABLE":
        assert os.path.isfile(final)


def test_pipeline_delete_is_admin_only_and_touches_nothing_when_denied(env):
    pid = _mk_pipeline(env)
    thumbs.register_from_staged(pid, _stage_thumb(pid))
    final = ap.resolve("thumbnails", thumbs.relative_path_for(pid))
    assert env["pm"].delete_pipeline_coordinated(env["viewer"], pid) == {"outcome": "denied"}
    assert ps.repository.exists(pid) and os.path.isfile(final)
    assert thumbs.get_for_pipeline(pid)["status"] == "AVAILABLE"


# ------------------------------------------------------------------ media
class _Upload:
    def __init__(self, data): self._d = data
    def save(self, path):
        with open(path, "wb") as f:
            f.write(self._d)


def test_media_upload_creation_state_machine(env):
    row = media.ingest_upload(_Upload(b"\x00" * 1024), original_filename="cam 01.mp4", timestamp="20260101_000000")
    assert row["relative_path"] == "20260101_000000_cam_01.mp4"
    assert row["status"] == "AVAILABLE" and row["validation_status"] == "PASSED"
    assert row["sha256"] == hashlib.sha256(b"\x00" * 1024).hexdigest() and row["size_bytes"] == 1024
    final = ap.resolve("media", row["relative_path"])
    assert os.path.isfile(final) and not os.listdir(os.path.join(ap.kind_root("media"), ap.STAGING_DIR))
    assert MediaLibrary().resolve_relative(row["relative_path"]) == final
    assert any(s["relative_path"] == row["relative_path"] for s in MediaLibrary().list_media()[0])


def test_media_upload_rejects_bad_type_and_empty_file(env):
    with pytest.raises(media.MediaIngestError):
        media.ingest_upload(_Upload(b"x"), original_filename="evil.py")
    assert media.list_assets() == []
    with pytest.raises(media.MediaIngestError):
        media.ingest_upload(_Upload(b""), original_filename="empty.mp4", timestamp="20260101_000001")
    rows = media.list_assets()
    assert len(rows) == 1 and rows[0]["status"] == "FAILED"
    assert media.servable_path(rows[0]["relative_path"]) is None
    assert not os.path.exists(ap.resolve("media", rows[0]["relative_path"]))


def test_media_upload_failure_at_promote_never_available(env, monkeypatch):
    real_replace = os.replace
    def boom(src, dst):
        if ap.kind_root("media") in os.path.abspath(dst) and not os.path.basename(os.path.dirname(dst)).startswith("."):
            raise OSError("disk full")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(media.MediaIngestError):
        media.ingest_upload(_Upload(b"y" * 10), original_filename="v.mp4", timestamp="20260101_000002")
    row = media.list_assets()[0]
    assert row["status"] == "FAILED" and media.servable_path(row["relative_path"]) is None


def test_staging_media_is_never_served(env):
    rel = "20260101_000003_s.mp4"
    with ap.__dict__["os"].fdopen(os.open(ap.resolve("media", rel), os.O_WRONLY | os.O_CREAT), "wb") as f:
        f.write(b"z" * 5)
    from InferenceNode.auth.db import get_session
    from InferenceNode.data_models import MediaAsset
    with get_session() as s:
        s.add(MediaAsset(media_id="m-staging", relative_path=rel, status="STAGING", validation_status="PENDING"))
    assert media.servable_path(rel) is None
    with pytest.raises(MediaError) as ei:
        MediaLibrary().resolve_relative(rel)
    assert ei.value.code == "MEDIA_INTEGRITY"


def test_dropped_in_file_is_registered_on_first_use_and_tamper_detected(env):
    rel = "drop/cam.mp4"
    final = ap.resolve("media", rel)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    with open(final, "wb") as f:
        f.write(b"q" * 64)
    lib = MediaLibrary()
    assert lib.resolve_relative(rel) == final
    row = media.get_by_path(rel)
    assert row["status"] == "AVAILABLE" and row["sha256"] == hashlib.sha256(b"q" * 64).hexdigest()
    with open(final, "ab") as f:
        f.write(b"!")
    with pytest.raises(MediaError) as ei:
        lib.resolve_relative(rel)
    assert ei.value.code == "MEDIA_INTEGRITY" and media.get_by_path(rel)["status"] == "CORRUPT"


def test_default_media_root_is_artifact_root(env):
    assert os.path.realpath(default_media_root()) == os.path.realpath(ap.kind_root("media"))


# ------------------------------------------------------------------ physical migration
def test_media_migration_preserves_relative_paths_and_registers(env, tmp_path):
    legacy = tmp_path / "legacy_media"; (legacy / "sub").mkdir(parents=True)
    (legacy / "a.mp4").write_bytes(b"A" * 100)
    (legacy / "sub" / "b.mp4").write_bytes(b"B" * 200)
    (legacy / "notes.txt").write_bytes(b"n")
    rep = rm.migrate_media_registry(str(legacy))
    assert rep.discovered == 2 and rep.available == 2 and rep.failed == 0
    assert str(legacy / "notes.txt") in rep.unregistered
    assert app_state.get_state(rm.MEDIA_MARKER) == app_state.STATE_COMPLETED
    for rel, data in (("a.mp4", b"A" * 100), ("sub/b.mp4", b"B" * 200)):
        assert open(ap.resolve("media", rel), "rb").read() == data
        row = media.get_by_path(rel)
        assert row["status"] == "AVAILABLE" and row["sha256"] == hashlib.sha256(data).hexdigest()
        assert (legacy / rel).exists(), "legacy retained"
        assert MediaLibrary().resolve_relative(rel) == ap.resolve("media", rel)
    # idempotent
    rep2 = rm.migrate_media_registry(str(legacy), force=True)
    assert rep2.available == 2 and len(media.list_assets()) == 2


def test_media_migration_collision_with_different_hash_blocks_marker(env, tmp_path):
    legacy = tmp_path / "legacy_media"; legacy.mkdir()
    (legacy / "a.mp4").write_bytes(b"A" * 100)
    with open(ap.resolve("media", "a.mp4"), "wb") as f:
        f.write(b"DIFFERENT")
    rep = rm.migrate_media_registry(str(legacy))
    assert rep.failed == 1 and rep.blocking
    assert app_state.get_state(rm.MEDIA_MARKER) != app_state.STATE_COMPLETED
    assert open(ap.resolve("media", "a.mp4"), "rb").read() == b"DIFFERENT", "never overwritten"
    assert media.get_by_path("a.mp4") is None


def test_thumbnail_migration_registers_live_only_and_reports_orphans(env, tmp_path):
    live = _mk_pipeline(env, "live-1")
    legacy = tmp_path / "legacy_thumbs"; legacy.mkdir()
    (legacy / f"thumbnail_{live}.jpg").write_bytes(_JPEG)
    (legacy / "thumbnail_orphan-9.jpg").write_bytes(_JPEG)
    rep = rm.migrate_thumbnails_registry(str(legacy))
    assert rep.discovered == 2 and rep.available == 1 and rep.failed == 0
    assert any(u.endswith("thumbnail_orphan-9.jpg") for u in rep.unregistered)
    assert (legacy / "thumbnail_orphan-9.jpg").exists(), "orphan reported, not deleted"
    assert app_state.get_state(rm.THUMBNAILS_MARKER) == app_state.STATE_COMPLETED
    t = thumbs.get_for_pipeline(live)
    assert t["status"] == "AVAILABLE" and t["sha256"] == hashlib.sha256(_JPEG).hexdigest()
    assert env["pm"].get_pipeline_thumbnail_path(live) == ap.resolve("thumbnails", t["relative_path"])
    assert thumbs.get_for_pipeline("orphan-9") is None
    assert not os.path.exists(ap.resolve("thumbnails", "thumbnail_orphan-9.jpg"))


# ------------------------------------------------------------------ same contracts on isolated PostgreSQL
@pytest.fixture()
def pg_env(pg_guard, monkeypatch):
    """Isolated PG database + isolated ARTIFACT_ROOT (hard-guarded by pg_guard). Rows are
    created and removed inside the throwaway fixture DB only."""
    from sqlalchemy import text
    monkeypatch.delenv("ARMYEYE_MEDIA_ROOT", raising=False)
    ap.ensure_layout()
    with pg_guard["engine"].begin() as c:
        for t in ("pipeline_thumbnails", "media_assets", "pipeline_user_access", "pipelines"):
            c.execute(text(f"DELETE FROM {t}"))
        c.execute(text("DELETE FROM app_state WHERE key IN (:a, :b)"), {"a": rm.MEDIA_MARKER, "b": rm.THUMBNAILS_MARKER})
        c.execute(text("DELETE FROM users WHERE username IN ('pg_root','pg_joe')"))
    admin = svc.create_user(_Seed, username="pg_root", password="rootpass1", role="admin", must_change_password=False)
    viewer = svc.create_user(_Seed, username="pg_joe", password="joepass123", role="user", must_change_password=False)
    pm = PipelineManager(os.path.join(pg_guard["artifact_root"], "repo"))
    return {"admin": admin, "viewer": viewer, "pm": pm, "engine": pg_guard["engine"], "root": pg_guard["artifact_root"]}


def test_pg_thumbnail_lifecycle_and_cascade_delete(pg_env):
    from sqlalchemy import text
    pid = _mk_pipeline(pg_env, "pg-thumb-1")
    row = thumbs.register_from_staged(pid, _stage_thumb(pid))
    assert row["status"] == "AVAILABLE" and row["sha256"] == hashlib.sha256(_JPEG).hexdigest()
    with pg_env["engine"].connect() as c:
        db = c.execute(text("SELECT t.status, t.validation_status, t.sha256, t.size_bytes FROM pipeline_thumbnails t "
                            "JOIN pipelines p ON p.id = t.pipeline_id WHERE p.pipeline_id = :p"), {"p": pid}).one()
    assert tuple(db) == ("AVAILABLE", "PASSED", row["sha256"], len(_JPEG))
    final = ap.resolve("thumbnails", row["relative_path"])
    assert pg_env["pm"].get_pipeline_thumbnail_path(pid) == final
    assert pg_env["pm"].delete_pipeline_coordinated(pg_env["admin"], pid) == {"outcome": "deleted"}
    with pg_env["engine"].connect() as c:
        assert c.execute(text("SELECT count(*) FROM pipeline_thumbnails")).scalar_one() == 0   # FK cascade
        assert c.execute(text("SELECT count(*) FROM pipelines WHERE pipeline_id=:p"), {"p": pid}).scalar_one() == 0
    assert not os.path.exists(final) and not os.path.exists(ap.trash_path("thumbnails", row["relative_path"]))


def test_pg_pipeline_delete_db_failure_restores_thumbnail(pg_env, monkeypatch):
    pid = _mk_pipeline(pg_env, "pg-thumb-2")
    row = thumbs.register_from_staged(pid, _stage_thumb(pid))
    final = ap.resolve("thumbnails", row["relative_path"])
    monkeypatch.setattr(ps, "delete_pipeline", lambda u, p: (_ for _ in ()).throw(RuntimeError("db down")))
    assert pg_env["pm"].delete_pipeline_coordinated(pg_env["admin"], pid)["outcome"] == "db_failed"
    assert ps.repository.exists(pid) and os.path.isfile(final)
    assert thumbs.get_for_pipeline(pid)["status"] in ("DELETING", "AVAILABLE")


def test_pg_media_ingest_and_migration(pg_env, tmp_path):
    from sqlalchemy import text
    row = media.ingest_upload(_Upload(b"\x01" * 2048), original_filename="pg cam.mp4", timestamp="20260102_000000")
    with pg_env["engine"].connect() as c:
        db = c.execute(text("SELECT status, validation_status, sha256, size_bytes, relative_path FROM media_assets "
                            "WHERE media_id=:m"), {"m": row["media_id"]}).one()
    assert tuple(db) == ("AVAILABLE", "PASSED", hashlib.sha256(b"\x01" * 2048).hexdigest(), 2048, "20260102_000000_pg_cam.mp4")
    assert MediaLibrary().resolve_relative(row["relative_path"]) == ap.resolve("media", row["relative_path"])
    legacy = tmp_path / "legacy_media"; legacy.mkdir()
    (legacy / "old.mp4").write_bytes(b"O" * 300)
    rep = rm.migrate_media_registry(str(legacy))
    assert rep.available == 1 and rep.failed == 0
    with pg_env["engine"].connect() as c:
        assert c.execute(text("SELECT value FROM app_state WHERE key=:k"), {"k": rm.MEDIA_MARKER}).scalar_one() == app_state.STATE_COMPLETED
        assert c.execute(text("SELECT status FROM media_assets WHERE relative_path='old.mp4'")).scalar_one() == "AVAILABLE"
    with pg_env["engine"].begin() as c:   # undefined lifecycle value rejected by PG CHECK
        import sqlalchemy.exc
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            c.execute(text("INSERT INTO media_assets (media_id, relative_path, status, validation_status) "
                           "VALUES ('bad', 'bad.mp4', 'NEEDS_REVIEW', 'PENDING')"))
