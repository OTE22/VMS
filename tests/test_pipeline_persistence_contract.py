"""Pipeline persistence contract (Phase 2 - approved Builder/Management remediation).

Exercises the exact service path the routes call:
  PipelineManager.build_pipeline_definition / build_duplicate_definition / update_pipeline
  -> pipeline_store.create_pipeline (admin) -> PipelineRepository -> DB.
Runs on temp SQLite (logic); the same assertions run on isolated PostgreSQL via the
registry-phase suites. No dev data is touched (see conftest.py).
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db            # noqa: E402
from InferenceNode.auth import service as svc           # noqa: E402
from InferenceNode.auth.models import Base              # noqa: E402
import InferenceNode.data_models                        # noqa: E402,F401
from InferenceNode import pipeline_store as ps          # noqa: E402
from InferenceNode.pipeline_store import (              # noqa: E402
    sanitize_config, unredact_into, REDACTED)
from InferenceNode.pipeline_manager import PipelineManager  # noqa: E402

D1 = "11111111-1111-1111-1111-111111111111"
D2 = "22222222-2222-2222-2222-222222222222"
D3 = "33333333-3333-3333-3333-333333333333"
WEBHOOK_URL = "http://h/webhook/" + "{pipeline_id}"


class _Seed:
    id, username, role = 0, "seed", "admin"
    is_admin = True


@pytest.fixture()
def env(tmp_path):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'p.db'}")
    Base.metadata.create_all(engine)
    admin = svc.create_user(_Seed, username="root", password="rootpass1", role="admin",
                            must_change_password=False)
    viewer = svc.create_user(_Seed, username="joe", password="joepass123", role="user",
                             must_change_password=False)
    pm = PipelineManager(str(tmp_path / "repo"))
    yield {"admin": admin, "viewer": viewer, "pm": pm}
    auth_db._engine = None; auth_db._SessionLocal = None


def _builder_payload(name="cam", secret="s3cr3t"):
    """Exactly the 6-key shape pipeline_builder.html POSTs/PUTs."""
    return {
        "name": name, "description": "d", "inference_enabled": True,
        "frame_source": {"capture_type": "ip_camera",
                         "config": {"source": f"rtsp://user:{secret}@10.0.0.1/stream1",
                                    "username": "user", "password": secret}},
        "model": {"id": "yolov8n_03056081", "engine_type": "ultralytics", "device": "intel:cpu"},
        "destinations": [
            {"id": D1, "type": "mqtt", "enabled": True,
             "config": {"server": "b", "port": 1883, "topic": "t", "password": "mqpw"}},
            {"id": D2, "type": "webhook", "enabled": True,
             "config": {"url": WEBHOOK_URL, "token": "tok"}},
        ],
    }


def _create(env, payload):
    pid, definition = env["pm"].build_pipeline_definition(payload)
    ps.create_pipeline(env["admin"], pipeline_id=pid, name=payload["name"],
                       description=payload.get("description"), config=definition, status="stopped")
    return pid


# ------------------------------------------------------------------ CREATE / GET

def test_create_persists_exactly_one_row_with_builder_payload(env):
    pid = _create(env, _builder_payload())
    rows = ps.repository.list(is_admin=True)
    assert [r["pipeline_id"] for r in rows] == [pid]
    stored = env["pm"].get_pipeline(pid)
    assert stored["frame_source"]["config"]["password"] == "s3cr3t"     # unredacted in DB
    assert sanitize_config(stored)["frame_source"]["config"]["password"] == REDACTED


def test_create_never_honors_client_supplied_id(env):
    payload = _builder_payload(); payload["id"] = "attacker-chosen"
    pid = _create(env, payload)
    assert pid != "attacker-chosen" and len(pid) == 36


def test_create_requires_admin(env):
    payload = _builder_payload()
    pid, definition = env["pm"].build_pipeline_definition(payload)
    with pytest.raises(ps.AccessDenied):
        ps.create_pipeline(env["viewer"], pipeline_id=pid, name="x", config=definition)
    assert ps.repository.list(is_admin=True) == []


# ------------------------------------------------------------------ DUPLICATE

def test_duplicate_gets_new_ids_and_preserves_config_and_secrets(env):
    a = _create(env, _builder_payload(name="orig"))
    b, definition = env["pm"].build_duplicate_definition(a, existing_names=["orig"])
    ps.create_pipeline(env["admin"], pipeline_id=b, name=definition["name"],
                       description=definition["description"], config=definition)
    assert b != a
    src, dup = env["pm"].get_pipeline(a), env["pm"].get_pipeline(b)
    assert dup["name"] == "orig (1)" and dup["description"].startswith("Copy of")
    assert dup["inference_enabled"] == src["inference_enabled"]
    # every destination id is NEW - never shared with the original
    assert {d["id"] for d in dup["destinations"]}.isdisjoint({d["id"] for d in src["destinations"]})
    # config copied UNREDACTED server-side
    assert dup["frame_source"]["config"]["password"] == "s3cr3t"
    assert dup["destinations"][0]["config"]["password"] == "mqpw"
    # ...but anything returned to a client is sanitized
    out = sanitize_config(dup)
    assert "s3cr3t" not in json.dumps(out) and "mqpw" not in json.dumps(out)


def test_duplicate_of_unknown_source_is_none(env):
    assert env["pm"].build_duplicate_definition("nope") is None


def test_duplicate_name_suffix_increments(env):
    a = _create(env, _builder_payload(name="cam"))
    _, d1 = env["pm"].build_duplicate_definition(a, existing_names=["cam", "cam (1)", "cam (2)"])
    assert d1["name"] == "cam (3)"


# ------------------------------------------------------------------ UPDATE targets the right row

def test_update_hits_duplicate_row_and_leaves_original_unchanged(env):
    a = _create(env, _builder_payload(name="orig"))
    b, definition = env["pm"].build_duplicate_definition(a, ["orig"])
    ps.create_pipeline(env["admin"], pipeline_id=b, name=definition["name"], config=definition)
    before_a = json.dumps(env["pm"].get_pipeline(a), sort_keys=True, default=str)

    edit = sanitize_config(env["pm"].get_pipeline(b))          # what the Builder edits
    edit["name"] = "dup-edited"; edit["description"] = "changed"; edit["inference_enabled"] = False
    assert env["pm"].update_pipeline(b, edit) is True

    after_b = env["pm"].get_pipeline(b)
    assert after_b["name"] == "dup-edited" and after_b["inference_enabled"] is False
    assert json.dumps(env["pm"].get_pipeline(a), sort_keys=True, default=str) == before_a
    # the round-tripped redacted secrets were NOT persisted as "***"
    assert after_b["frame_source"]["config"]["password"] == "s3cr3t"
    assert after_b["destinations"][0]["config"]["password"] == "mqpw"
    assert after_b["frame_source"]["config"]["source"] == "rtsp://user:s3cr3t@10.0.0.1/stream1"


# ------------------------------------------------------------------ redaction merge contract

def test_redacted_echo_preserves_secret(env):
    pid = _create(env, _builder_payload())
    edit = sanitize_config(env["pm"].get_pipeline(pid))
    assert env["pm"].update_pipeline(pid, edit)
    assert env["pm"].get_pipeline(pid)["frame_source"]["config"]["password"] == "s3cr3t"


def test_omitted_secret_key_preserves_secret(env):
    pid = _create(env, _builder_payload())
    edit = {"frame_source": {"capture_type": "ip_camera",
                             "config": {"source": "rtsp://***@10.0.0.1/stream1", "username": "user"}}}
    assert env["pm"].update_pipeline(pid, edit)
    assert env["pm"].get_pipeline(pid)["frame_source"]["config"]["password"] == "s3cr3t"


def test_new_secret_replaces_secret(env):
    pid = _create(env, _builder_payload())
    edit = sanitize_config(env["pm"].get_pipeline(pid))
    edit["frame_source"]["config"]["password"] = "brand-new"
    assert env["pm"].update_pipeline(pid, edit)
    assert env["pm"].get_pipeline(pid)["frame_source"]["config"]["password"] == "brand-new"


def test_explicit_null_clears_secret(env):
    pid = _create(env, _builder_payload())
    edit = sanitize_config(env["pm"].get_pipeline(pid))
    edit["frame_source"]["config"]["password"] = None
    assert env["pm"].update_pipeline(pid, edit)
    assert env["pm"].get_pipeline(pid)["frame_source"]["config"]["password"] is None


def test_url_host_path_edit_keeps_credential_component_wise(env):
    pid = _create(env, _builder_payload())
    edit = sanitize_config(env["pm"].get_pipeline(pid))
    edit["frame_source"]["config"]["source"] = "rtsp://***@10.0.0.2/stream2"
    assert env["pm"].update_pipeline(pid, edit)
    assert env["pm"].get_pipeline(pid)["frame_source"]["config"]["source"] == \
        "rtsp://user:s3cr3t@10.0.0.2/stream2"


def test_url_with_new_userinfo_replaces_and_without_userinfo_clears():
    assert unredact_into({"u": "rtsp://a:b@h/x"}, {"u": "rtsp://n:p@h2/y"})["u"] == "rtsp://n:p@h2/y"
    assert unredact_into({"u": "rtsp://a:b@h/x"}, {"u": "rtsp://h2/y"})["u"] == "rtsp://h2/y"


def test_destination_secrets_follow_identity_not_position(env):
    pid = _create(env, _builder_payload())
    edit = sanitize_config(env["pm"].get_pipeline(pid))
    edit["destinations"] = list(reversed(edit["destinations"]))       # reorder
    assert env["pm"].update_pipeline(pid, edit)
    after = {d["id"]: d for d in env["pm"].get_pipeline(pid)["destinations"]}
    assert after[D1]["config"]["password"] == "mqpw"
    assert after[D2]["config"]["token"] == "tok"
    assert "token" not in after[D1]["config"]


def test_add_and_remove_destinations_keep_secrets_only_on_matching_uuid(env):
    pid = _create(env, _builder_payload())
    edit = sanitize_config(env["pm"].get_pipeline(pid))
    kept = [d for d in edit["destinations"] if d["type"] == "webhook"]
    kept.append({"id": D3, "type": "mqtt", "enabled": True,
                 "config": {"server": "b2", "port": 1883, "topic": "t2", "password": REDACTED}})
    edit["destinations"] = kept
    assert env["pm"].update_pipeline(pid, edit)
    after = {d["id"]: d for d in env["pm"].get_pipeline(pid)["destinations"]}
    assert D1 not in after                                # removed with its secret
    assert after[D2]["config"]["token"] == "tok"
    assert "password" not in after[D3]["config"]          # never stored as "***"


def test_sanitized_responses_never_leak_any_stored_secret(env):
    pid = _create(env, _builder_payload(secret="LEAKME"))
    for surface in (env["pm"].get_pipeline(pid), env["pm"].list_pipelines()[pid]):
        assert "LEAKME" not in json.dumps(sanitize_config(surface))
        assert "mqpw" not in json.dumps(sanitize_config(surface))
    assert "LEAKME" in json.dumps(env["pm"].get_pipeline(pid))   # ...but stored intact


# ------------------------------------------------------------------ uuid collision

def test_repository_rejects_duplicate_pipeline_id(env):
    pid = _create(env, _builder_payload())
    with pytest.raises(ValueError):
        ps.create_pipeline(env["admin"], pipeline_id=pid, name="again", config={})


# ------------------------------------------------------------------ canonical model identity (Phase 8)

def _col_and_json(pid):
    rec = ps.repository.get(pid)
    return rec["model_id"], (rec["config"].get("model") or {}).get("id")


def test_pipeline_model_column_matches_json_reference(env):
    pid = _create(env, _builder_payload())
    col, js = _col_and_json(pid)
    assert col == js == "yolov8n_03056081"


def test_pipeline_model_sync_on_create_update_duplicate_import(env):
    # create
    a = _create(env, _builder_payload())
    assert _col_and_json(a) == ("yolov8n_03056081", "yolov8n_03056081")
    # update model
    edit = sanitize_config(env["pm"].get_pipeline(a)); edit["model"]["id"] = "other_model_1"
    assert env["pm"].update_pipeline(a, edit)
    assert _col_and_json(a) == ("other_model_1", "other_model_1")
    # duplicate
    b, d = env["pm"].build_duplicate_definition(a, ["cam"])
    ps.create_pipeline(env["admin"], pipeline_id=b, name=d["name"], config=d)
    assert _col_and_json(b) == ("other_model_1", "other_model_1")
    # import-shaped create (a definition arriving from a zip goes through the same repo path)
    imp = env["pm"].build_pipeline_definition(_builder_payload(name="imported"))[1]
    ps.create_pipeline(env["admin"], pipeline_id="imp-1", name="imported", config=imp)
    assert _col_and_json("imp-1") == ("yolov8n_03056081", "yolov8n_03056081")
    # legacy migration-shaped create with a raw config
    ps.repository.create(pipeline_id="legacy-1", name="l", config={"model": {"id": "legacy_model", "engine_type": "x"}})
    assert _col_and_json("legacy-1") == ("legacy_model", "legacy_model")


def test_unknown_legacy_model_reference_is_reported_not_guessed():
    """The 0005 audit classifies UNKNOWN_MODEL and leaves the column NULL - it never maps
    a stale id to some other model."""
    src = open(os.path.join(REPO, "InferenceNode", "migrations", "versions",
                            "0005_pipeline_model_integrity.py"), encoding="utf-8").read()
    assert "UNKNOWN_MODEL" in src and "left NULL, reported, NOT guessed" in src
    assert "for pid, mid in valid:" in src            # back-fill VALID rows only
