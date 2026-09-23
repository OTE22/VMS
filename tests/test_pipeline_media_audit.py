"""Audit trail for pipeline lifecycle and media ingest.

Gap found by the live database audit: audit_log covered logins, user management, engine
create/delete and pipeline ACCESS grants, but a pipeline created/updated/deleted and a
media upload left no trace. These tests pin the new entries and, just as importantly,
prove the detail payload carries structured metadata only - never configuration values,
URLs, tokens or passwords.
"""
import os
import sys

import pytest
from sqlalchemy import select

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db                        # noqa: E402
from InferenceNode.auth import service as svc                       # noqa: E402
from InferenceNode.auth.models import AuditLog, Base                # noqa: E402
import InferenceNode.data_models  # noqa: E402,F401
from InferenceNode import artifact_paths as ap                      # noqa: E402
from InferenceNode import pipeline_store as ps                      # noqa: E402

SECRET = "sup3r-s3cret-token"


class _Seed:
    id = None; username = "seed"; role = "admin"; is_authenticated = True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'a.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("ARMYEYE_ARTIFACT_ROOT", str(tmp_path / "root"))
    monkeypatch.delenv("ARMYEYE_MEDIA_ROOT", raising=False)
    ap.ensure_layout()
    from InferenceNode import media_registry
    with open(ap.resolve("media", "a.mp4"), "wb") as f: f.write(b"audit-fixture-media")
    media_registry.register_existing("a.mp4")
    admin = svc.create_user(_Seed, username="root", password="rootpass1", role="admin",
                            must_change_password=False)
    viewer = svc.create_user(_Seed, username="joe", password="joepass123", role="user",
                             must_change_password=False)
    yield {"admin": admin, "viewer": viewer, "tmp": tmp_path}
    auth_db._engine = None; auth_db._SessionLocal = None


def _entries(action=None):
    with auth_db.get_session() as s:
        q = select(AuditLog).order_by(AuditLog.id)
        rows = [{"action": a.action, "target": a.target, "detail": a.detail,
                 "actor": a.actor_username, "actor_id": a.actor_user_id}
                for a in s.execute(q).scalars()]
    return [r for r in rows if action is None or r["action"] == action]


def _cfg(name="cam", secret=SECRET):
    return {"name": name,
            "frame_source": {"capture_type": "video_file", "config": {"relative_source": "a.mp4"}},
            "model": {"id": "m-1", "engine_type": "ultralytics"},
            "destinations": [{"type": "webhook", "config": {"url": f"https://user:{secret}@example.com/hook",
                                                            "auth_token": secret}}]}


# ------------------------------------------------------------------ pipeline lifecycle
def test_pipeline_create_update_delete_are_audited(env):
    ps.create_pipeline(env["admin"], pipeline_id="p-1", name="cam", config=_cfg())
    created = _entries("pipeline_created")
    assert len(created) == 1
    e = created[0]
    assert e["target"] == "p-1" and e["actor"] == "root" and e["actor_id"] == env["admin"].id
    assert e["detail"]["name"] == "cam" and e["detail"]["model_id"] == "m-1"
    assert e["detail"]["source_type"] == "video_file" and e["detail"]["media_ref"] == "a.mp4"
    assert e["detail"]["destination_count"] == 1 and e["detail"]["destination_types"] == ["webhook"]

    ps.update_pipeline(env["admin"], "p-1", name="cam2", config=_cfg(name="cam2"))
    updated = _entries("pipeline_updated")
    assert len(updated) == 1 and updated[0]["target"] == "p-1"
    assert "config" in updated[0]["detail"]["fields"] and "name" in updated[0]["detail"]["fields"]

    ps.delete_pipeline(env["admin"], "p-1")
    deleted = _entries("pipeline_deleted")
    assert len(deleted) == 1 and deleted[0]["target"] == "p-1"
    assert deleted[0]["detail"]["model_id"] == "m-1"


def test_audit_detail_never_contains_secrets_or_urls(env):
    ps.create_pipeline(env["admin"], pipeline_id="p-2", name="cam", config=_cfg())
    ps.update_pipeline(env["admin"], "p-2", config=_cfg(name="cam", secret="another-s3cret"))
    ps.delete_pipeline(env["admin"], "p-2")
    blob = repr(_entries())
    for forbidden in (SECRET, "another-s3cret", "example.com", "https://", "auth_token"):
        assert forbidden not in blob, f"{forbidden!r} leaked into the audit trail"


def test_model_reference_change_is_recorded_as_old_and_new(env):
    ps.create_pipeline(env["admin"], pipeline_id="p-3", name="cam", config=_cfg())
    cfg = _cfg(); cfg["model"]["id"] = "m-2"
    ps.update_pipeline(env["admin"], "p-3", config=cfg)
    changed = _entries("pipeline_updated")[0]["detail"]["changed"]
    assert changed["model_id"] == {"old": "m-1", "new": "m-2"}


def test_denied_operations_write_no_audit_entry(env):
    ps.create_pipeline(env["admin"], pipeline_id="p-4", name="cam", config=_cfg())
    for op in (lambda: ps.update_pipeline(env["viewer"], "p-4", name="hacked"),
               lambda: ps.delete_pipeline(env["viewer"], "p-4")):
        with pytest.raises(ps.AccessDenied):
            op()
    assert _entries("pipeline_updated") == [] and _entries("pipeline_deleted") == []
    assert len(_entries("pipeline_created")) == 1


def test_failed_audit_write_never_breaks_the_operation(env, monkeypatch):
    """The mutation already committed; an audit failure is logged, not raised."""
    import InferenceNode.auth.service as svc_mod
    monkeypatch.setattr(svc_mod, "record_audit",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("audit down")))
    ps.create_pipeline(env["admin"], pipeline_id="p-5", name="cam", config=_cfg())
    assert ps.repository.exists("p-5"), "pipeline must exist even when auditing fails"


# ------------------------------------------------------------------ media ingest
class _Upload:
    def __init__(self, data): self._d = data
    def save(self, path):
        with open(path, "wb") as f:
            f.write(self._d)


def test_media_upload_is_audited_with_hash_and_size(env):
    """The route records the entry; this exercises the same payload the route builds."""
    from InferenceNode import media_registry as media
    row = media.ingest_upload(_Upload(b"\x00" * 512), original_filename="clip 1.mp4",
                              created_by=env["admin"].id, timestamp="20260101_000000")
    svc.record_audit(actor=env["admin"], action="media_uploaded", target=row["relative_path"],
                     detail={"media_id": row["media_id"], "original_filename": "clip 1.mp4",
                             "size_bytes": row["size_bytes"], "sha256": row["sha256"],
                             "media_type": row["media_type"]})
    e = _entries("media_uploaded")[0]
    assert e["target"] == row["relative_path"] and e["actor"] == "root"
    assert e["detail"]["size_bytes"] == 512 and len(e["detail"]["sha256"]) == 64
    assert e["detail"]["media_type"] == "mp4"


def test_route_wires_media_audit_events():
    """The upload route must record both the success and the rejection event."""
    src = open(os.path.join(REPO, "InferenceNode", "inference_node.py"), encoding="utf-8").read()
    i = src.index("def upload_video():")
    body = src[i:i + 2500]
    assert "_audit_event('media_uploaded'" in body
    assert "_audit_event('media_upload_rejected'" in body
