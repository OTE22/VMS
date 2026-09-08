"""Media deletion — the artifact class the database cannot protect.

Models are guarded by `pipelines.model_id` with ON DELETE RESTRICT: PostgreSQL itself
refuses to delete a referenced model. Media is referenced by the STRING
`frame_source.config.relative_source` inside pipeline config JSON, with no foreign key at
all, so the reference check in the application is the ONLY thing preventing a delete from
silently breaking a running pipeline.

Before this, media had no delete path whatsoever - retiring an asset meant direct database
access, which bypassed the trash flow and left no audit trail.

Deletion follows the same machine as models/engines:
    AVAILABLE -> DELETING -> file to <media>/.trash -> row deleted -> COMMIT -> purge
A failure after the move restores the file, so the outcome is always either "deleted" or
"the asset is intact" - never a row pointing at bytes that are gone.
"""
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
from InferenceNode import media_registry as media                   # noqa: E402
from InferenceNode import pipeline_store as ps                      # noqa: E402


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
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'m.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("ARMYEYE_ARTIFACT_ROOT", str(tmp_path / "root"))
    monkeypatch.delenv("ARMYEYE_MEDIA_ROOT", raising=False)
    ap.ensure_layout()
    admin = svc.create_user(_Seed, username="root", password="rootpass1", role="admin",
                            must_change_password=False)
    yield {"admin": admin, "tmp": tmp_path}
    auth_db._engine = None; auth_db._SessionLocal = None


def _upload(name="clip.mp4", stamp="20260101_000000"):
    return media.ingest_upload(_Upload(b"\x00" * 256), original_filename=name, timestamp=stamp)


def _pipeline(env, pid, rel=None, absolute=None):
    cfg = {"name": pid, "model": {"id": None}, "destinations": [],
           "frame_source": {"capture_type": "video_file", "config": {}}}
    if rel:
        cfg["frame_source"]["config"]["relative_source"] = rel
    if absolute:
        cfg["frame_source"]["config"]["source"] = absolute
    ps.create_pipeline(env["admin"], pipeline_id=pid, name=pid, config=cfg)


# ------------------------------------------------------------------ the happy path
def test_delete_removes_row_and_bytes_and_purges_trash(env):
    row = _upload()
    path = ap.resolve("media", row["relative_path"])
    assert os.path.isfile(path)

    r = media.delete_media(row["media_id"])
    assert r["outcome"] == "deleted"
    assert media.get_by_path(row["relative_path"]) is None, "registry row must be gone"
    assert not os.path.exists(path), "bytes must be gone"
    assert not os.path.exists(ap.trash_path("media", row["relative_path"])), "trash must be purged"


def test_deleting_an_unknown_id_is_not_found(env):
    assert media.delete_media("no-such-media")["outcome"] == "not_found"


def test_delete_leaves_the_registry_consistent(env):
    """The reconciliation must stay healthy - no row without bytes, no orphan file."""
    keep, drop = _upload("keep.mp4", "20260101_000001"), _upload("drop.mp4", "20260101_000002")
    media.delete_media(drop["media_id"])
    rep = media.verify_all()
    assert rep["available_missing"] == [] and rep["orphan_files"] == []
    assert rep["available_valid"] == [keep["relative_path"]]


# ------------------------------------------------------------------ reference protection
def test_delete_is_refused_while_a_pipeline_references_it(env):
    """The property the database cannot enforce for media."""
    row = _upload()
    _pipeline(env, "p-uses-it", rel=row["relative_path"])

    r = media.delete_media(row["media_id"])
    assert r["outcome"] == "referenced"
    assert [p["pipeline_id"] for p in r["pipelines"]] == ["p-uses-it"]
    # nothing was touched
    assert media.get_by_path(row["relative_path"]) is not None
    assert os.path.isfile(ap.resolve("media", row["relative_path"]))


def test_legacy_absolute_source_pipelines_are_also_detected(env):
    """Older pipelines store an absolute host path; at runtime they resolve to the same file
    through the basename fallback, so they must count as references too."""
    row = _upload()
    _pipeline(env, "p-legacy", absolute="C:\\\\old\\\\host\\\\media\\\\" + row["relative_path"])
    r = media.delete_media(row["media_id"])
    assert r["outcome"] == "referenced", "a legacy absolute reference must still protect the file"


def test_unrelated_pipelines_do_not_block_deletion(env):
    row = _upload("target.mp4", "20260101_000003")
    other = _upload("other.mp4", "20260101_000004")
    _pipeline(env, "p-other", rel=other["relative_path"])
    assert media.delete_media(row["media_id"])["outcome"] == "deleted"


def test_force_overrides_but_still_reports_what_it_broke(env):
    row = _upload()
    _pipeline(env, "p-uses-it", rel=row["relative_path"])
    r = media.delete_media(row["media_id"], force=True)
    assert r["outcome"] == "deleted"
    assert [p["pipeline_id"] for p in r["was_referenced_by"]] == ["p-uses-it"], \
        "a forced delete must still say which pipelines it affected"


# ------------------------------------------------------------------ failure safety
def test_a_row_delete_failure_restores_the_bytes(env, monkeypatch):
    """Never a registry row pointing at bytes that are gone."""
    row = _upload()
    path = ap.resolve("media", row["relative_path"])

    real = auth_db.get_session
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 3:                     # the row-deletion transaction
            raise RuntimeError("db down")
        return real()
    monkeypatch.setattr(media, "get_session", flaky)

    r = media.delete_media(row["media_id"])
    assert r["outcome"] == "failed"
    monkeypatch.undo()
    assert os.path.isfile(path), "the file must be restored from trash"
    assert media.get_by_path(row["relative_path"]) is not None, "the row must survive"


def test_deleting_an_already_missing_file_still_clears_the_row(env):
    """Cleaning up after exactly the inconsistency this API exists to prevent."""
    row = _upload()
    os.remove(ap.resolve("media", row["relative_path"]))       # simulate an `rm`
    assert media.delete_media(row["media_id"])["outcome"] == "deleted"
    assert media.get_by_path(row["relative_path"]) is None
    assert media.verify_all()["available_missing"] == []


# ------------------------------------------------------------------ the route
def test_route_is_admin_csrf_guarded_and_returns_409_when_referenced():
    src = open(os.path.join(REPO, "InferenceNode", "inference_node.py"), encoding="utf-8").read()
    i = src.index("@self.app.route('/api/media/<media_id>', methods=['DELETE'])")
    body = src[i:i + 2200]
    assert "@self._admin_csrf" in body, "media delete must be admin-only and CSRF-checked"
    assert "409" in body and "referenced" in body
    assert "404" in body
    assert "_audit_event('media_deleted'" in body, "deletion must be audited"
    assert "force" in body
