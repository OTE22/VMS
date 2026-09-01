"""Pipeline authorization service + repository.

PostgreSQL is the single source of truth; access comes from pipeline_user_access only.
owner_id is creator metadata that must never grant anything on its own.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db          # noqa: E402
from InferenceNode.auth import service as svc          # noqa: E402
from InferenceNode.auth.models import Base             # noqa: E402
import InferenceNode.data_models                        # noqa: E402,F401
from InferenceNode import pipeline_store as ps          # noqa: E402
from InferenceNode.pipeline_repository import repository, normalize_permissions  # noqa: E402


class _Seed:
    id, username, role = 0, "seed", "admin"
    is_admin = True


@pytest.fixture
def db(tmp_path):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'p.db'}")
    Base.metadata.create_all(engine)
    admin = svc.create_user(_Seed, username="root", password="rootpass1", role="admin", must_change_password=False)
    joe = svc.create_user(_Seed, username="joe", password="joepass123", role="user", must_change_password=False)
    amy = svc.create_user(_Seed, username="amy", password="amypass123", role="user", must_change_password=False)
    yield {"admin": admin, "joe": joe, "amy": amy}
    auth_db._engine = None; auth_db._SessionLocal = None


# --------------------------------------------------------------------- create --
def test_create_is_admin_only(db):
    joe, admin = db["joe"], db["admin"]
    with pytest.raises(ps.AccessDenied):
        ps.create_pipeline(joe, pipeline_id="p1", config={})
    p = ps.create_pipeline(admin, pipeline_id="p1", name="Cam1",
                           config={"owner_id": 9999, "source": "webcam"})
    # creator metadata is server-derived; a forged owner_id in the body is ignored
    assert p["owner_id"] == admin.id and p["owner_username"] == "root"


def test_owner_id_grants_no_access(db):
    """The whole point of the migration: creator != authorization."""
    admin, joe = db["admin"], db["joe"]
    ps.create_pipeline(admin, pipeline_id="p1", config={})
    ps.transfer_ownership(admin, "p1", joe.id, "joe")     # joe is now the recorded owner
    assert ps.get_pipeline_for_user("p1", joe, "view", require=False) is None
    with pytest.raises(ps.AccessDenied):
        ps.get_pipeline_for_user("p1", joe, "view")


# ---------------------------------------------------------------- permissions --
def test_each_permission_is_enforced_separately(db):
    admin, joe = db["admin"], db["joe"]
    ps.create_pipeline(admin, pipeline_id="p1", config={})
    ps.set_access(admin, "p1", joe.id, {"can_view": True, "can_start": True})

    assert ps.get_pipeline_for_user("p1", joe, "view")["pipeline_id"] == "p1"
    assert ps.get_pipeline_for_user("p1", joe, "start")["pipeline_id"] == "p1"
    assert ps.get_pipeline_for_user("p1", joe, "stop", require=False) is None
    assert ps.get_pipeline_for_user("p1", joe, "edit", require=False) is None


def test_permission_consistency_normalized_server_side(db):
    """An operating right without visibility would let a user act on something they
    cannot even see, so it is normalized rather than trusted."""
    assert normalize_permissions({"can_start": True})["can_view"] is True
    assert normalize_permissions({"can_stop": True})["can_view"] is True
    assert normalize_permissions({"can_edit": True})["can_view"] is True
    assert normalize_permissions({})["can_view"] is False

    admin, joe = db["admin"], db["joe"]
    ps.create_pipeline(admin, pipeline_id="p1", config={})
    granted = ps.set_access(admin, "p1", joe.id, {"can_start": True, "can_view": False})
    assert granted["can_view"] is True


def test_admin_bypasses_assignment(db):
    admin = db["admin"]
    ps.create_pipeline(admin, pipeline_id="p1", config={})
    for perm in ("view", "start", "stop", "edit"):
        assert ps.get_pipeline_for_user("p1", admin, perm)["pipeline_id"] == "p1"


def test_unknown_permission_is_rejected(db):
    admin = db["admin"]
    ps.create_pipeline(admin, pipeline_id="p1", config={})
    with pytest.raises(ValueError):
        ps.get_pipeline_for_user("p1", admin, "sudo")


# --------------------------------------------------------------------- listing --
def test_list_is_scoped_to_assignments(db):
    admin, joe, amy = db["admin"], db["joe"], db["amy"]
    for pid in ("p1", "p2", "p3"):
        ps.create_pipeline(admin, pipeline_id=pid, config={})
    ps.set_access(admin, "p1", joe.id, {"can_view": True})
    ps.set_access(admin, "p2", amy.id, {"can_view": True})
    # granted but WITHOUT can_view -> must not appear in the list
    ps.set_access(admin, "p3", joe.id, {"can_view": False})

    assert {p["pipeline_id"] for p in ps.list_pipelines_for_user(joe)} == {"p1"}
    assert {p["pipeline_id"] for p in ps.list_pipelines_for_user(amy)} == {"p2"}
    assert {p["pipeline_id"] for p in ps.list_pipelines_for_user(admin)} == {"p1", "p2", "p3"}


# ------------------------------------------------------------------ mutations --
def test_update_requires_can_edit(db):
    admin, joe = db["admin"], db["joe"]
    ps.create_pipeline(admin, pipeline_id="p1", config={})
    ps.set_access(admin, "p1", joe.id, {"can_view": True})
    with pytest.raises(ps.AccessDenied):
        ps.update_pipeline(joe, "p1", name="hax")
    ps.set_access(admin, "p1", joe.id, {"can_edit": True})
    assert ps.update_pipeline(joe, "p1", name="ok")["name"] == "ok"


def test_delete_is_admin_only(db):
    admin, joe = db["admin"], db["joe"]
    ps.create_pipeline(admin, pipeline_id="p1", config={})
    ps.set_access(admin, "p1", joe.id,
                  {"can_view": True, "can_start": True, "can_stop": True, "can_edit": True})
    with pytest.raises(ps.AccessDenied):
        ps.delete_pipeline(joe, "p1")       # every permission, still not admin
    ps.delete_pipeline(admin, "p1")
    assert repository.get("p1") is None


# ---------------------------------------------------------------- assignments --
def test_assignment_management_is_admin_only(db):
    admin, joe, amy = db["admin"], db["joe"], db["amy"]
    ps.create_pipeline(admin, pipeline_id="p1", config={})
    for fn, args in (
        (ps.set_access, (joe, "p1", amy.id, {"can_view": True})),
        (ps.remove_access, (joe, "p1", amy.id)),
        (ps.list_access, (joe, "p1")),
    ):
        with pytest.raises(ps.AccessDenied):
            fn(*args)


def test_assignment_upsert_is_idempotent(db):
    admin, joe = db["admin"], db["joe"]
    ps.create_pipeline(admin, pipeline_id="p1", config={})
    ps.set_access(admin, "p1", joe.id, {"can_view": True})
    ps.set_access(admin, "p1", joe.id, {"can_view": True, "can_start": True})
    rows = ps.list_access(admin, "p1")
    assert len(rows) == 1 and rows[0]["can_start"] is True


def test_remove_assignment(db):
    admin, joe = db["admin"], db["joe"]
    ps.create_pipeline(admin, pipeline_id="p1", config={})
    ps.set_access(admin, "p1", joe.id, {"can_view": True})
    assert ps.remove_access(admin, "p1", joe.id) is True
    assert ps.list_access(admin, "p1") == []
    assert ps.get_pipeline_for_user("p1", joe, "view", require=False) is None


def test_deleting_pipeline_cascades_access_rows(db):
    admin, joe = db["admin"], db["joe"]
    ps.create_pipeline(admin, pipeline_id="p1", config={})
    ps.set_access(admin, "p1", joe.id, {"can_view": True})
    ps.delete_pipeline(admin, "p1")
    assert repository.list_access_for_user(joe.id) == []


def test_deleting_user_cascades_access_rows(db):
    admin, joe = db["admin"], db["joe"]
    ps.create_pipeline(admin, pipeline_id="p1", config={})
    ps.set_access(admin, "p1", joe.id, {"can_view": True})
    svc.delete_user(admin, joe.id)
    assert ps.list_access(admin, "p1") == []


def test_access_on_missing_pipeline_is_refused(db):
    admin, joe = db["admin"], db["joe"]
    with pytest.raises(ps.AccessDenied):
        ps.set_access(admin, "nope", joe.id, {"can_view": True})


# -------------------------------------------------------------------- secrets --
def test_config_secrets_are_redacted(db):
    cfg = {
        "frame_source": {"config": {"source": "rtsp://bob:hunter2@10.0.0.5:554/stream"}},
        "destinations": [{"type": "webhook",
                          "config": {"url": "https://x.io/h", "auth_token": "sekrit"}}],
    }
    clean = ps.sanitize_config(cfg)
    flat = repr(clean)
    assert "hunter2" not in flat and "sekrit" not in flat
    assert "10.0.0.5:554" in flat          # host kept, credentials stripped
    assert clean["destinations"][0]["config"]["auth_token"] == "***"


def test_normal_user_view_omits_raw_config(db):
    admin, joe = db["admin"], db["joe"]
    record = ps.create_pipeline(admin, pipeline_id="p1", name="Cam",
                                config={"model": {"engine_type": "ultralytics", "device": "intel:cpu"},
                                        "frame_source": {"capture_type": "video_file",
                                                         "config": {"source": "rtsp://u:p@h/s"}}})
    admin_view = ps.pipeline_view(record, is_admin=True)
    user_view = ps.pipeline_view(record, is_admin=False)
    assert "config" in admin_view and "config" not in user_view
    assert user_view["summary"]["engine_type"] == "ultralytics"
    assert "p@h" not in repr(admin_view)


# --------------------------------------------------------------------- models --
def test_model_uploader_server_derived(db):
    joe = db["joe"]
    m = ps.record_model(joe, model_id="m1", engine_type="ultralytics", name="yolo")
    assert m["uploader_id"] == joe.id and m["uploader_username"] == "joe"
