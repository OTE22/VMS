"""JSON -> PostgreSQL pipeline data migration.

The original importer keyed idempotency on "the pipelines table is empty", so two
unrelated fixture rows silently disabled it forever and left every real pipeline
unstartable. These tests pin the replacement behaviour.
"""
import json
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
from InferenceNode import app_state                     # noqa: E402
from InferenceNode import pipeline_migration as pm      # noqa: E402
from InferenceNode.pipeline_repository import repository  # noqa: E402


class _Seed:
    id, username, role = 0, "seed", "admin"
    is_admin = True


ENTRY_A = {"id": "uuid-1", "name": "A", "description": "first", "status": "stopped",
           "frame_source": {"capture_type": "video_file", "config": {"source": "a.mp4"}},
           "model": {"id": "m1", "engine_type": "ultralytics", "device": "intel:cpu"},
           "destinations": [], "inference_enabled": True,
           "stats": {"fps": 42}}
ENTRY_B = {"id": "uuid-2", "name": "B", "status": "stopped"}


@pytest.fixture
def db(tmp_path):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'m.db'}")
    Base.metadata.create_all(engine)
    admin = svc.create_user(_Seed, username="root", password="rootpass1", role="admin",
                            must_change_password=False)
    yield admin
    auth_db._engine = None; auth_db._SessionLocal = None


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "pipelines_metadata.json"
    path.write_text(json.dumps({"uuid-1": ENTRY_A, "uuid-2": ENTRY_B}), encoding="utf-8")
    return path


# ------------------------------------------------------------------- import --
def test_imports_all_pipelines(db, legacy):
    report = pm.migrate_json_pipelines(str(legacy), db.id, "root")
    assert set(report["imported"]) == {"uuid-1", "uuid-2"}
    assert repository.get("uuid-1")["name"] == "A"
    assert repository.get("uuid-1")["description"] == "first"


def test_unrelated_rows_do_not_block_migration(db, legacy):
    """The exact bug: fixture rows made the old importer skip everything."""
    repository.create(pipeline_id="pg-p1", name="fixture", config={})
    repository.create(pipeline_id="pg-p2", config={})
    report = pm.migrate_json_pipelines(str(legacy), db.id, "root")
    assert set(report["imported"]) == {"uuid-1", "uuid-2"}


def test_import_is_idempotent(db, legacy):
    pm.migrate_json_pipelines(str(legacy), db.id, "root")
    second = pm.migrate_json_pipelines(str(legacy), db.id, "root")
    assert second["imported"] == []
    assert set(second["skipped_existing"]) == {"uuid-1", "uuid-2"}
    assert len(repository.list(is_admin=True)) == 2


def test_source_json_is_never_renamed_or_deleted(db, legacy):
    pm.migrate_json_pipelines(str(legacy), db.id, "root")
    assert legacy.exists(), "the live JSON must survive migration"
    assert json.loads(legacy.read_text(encoding="utf-8"))  # unchanged and readable
    backup = legacy.parent / "pipelines_metadata.pre-db-migration.json"
    assert backup.exists()


def test_transient_stats_are_not_persisted(db, legacy):
    pm.migrate_json_pipelines(str(legacy), db.id, "root")
    assert "stats" not in repository.get("uuid-1")["config"]


def test_stale_running_status_is_not_carried_over(db, tmp_path):
    path = tmp_path / "pipelines_metadata.json"
    path.write_text(json.dumps({"x": {"id": "x", "name": "X", "status": "running"}}),
                    encoding="utf-8")
    pm.migrate_json_pipelines(str(path), db.id, "root")
    assert repository.get("x")["status"] == "stopped"


def test_malformed_entries_are_skipped_safely(db, tmp_path):
    path = tmp_path / "pipelines_metadata.json"
    path.write_text(json.dumps({
        "ok": {"id": "ok", "name": "Fine"},
        "no-id": {"name": "missing id"},
        "not-an-object": "just a string",
    }), encoding="utf-8")
    report = pm.migrate_json_pipelines(str(path), db.id, "root")
    assert report["imported"] == ["ok"]


def test_unreadable_json_is_not_fatal(db, tmp_path):
    path = tmp_path / "pipelines_metadata.json"
    path.write_text("{ this is not json", encoding="utf-8")
    report = pm.migrate_json_pipelines(str(path), db.id, "root")
    assert report["total_json"] == 0 and report["imported"] == []


# ----------------------------------------------------------------- conflicts --
def test_conflicting_config_is_reported_not_overwritten(db, legacy):
    repository.create(pipeline_id="uuid-1", name="DB VERSION",
                      config={"name": "DB VERSION", "model": {"id": "different"}})
    report = pm.migrate_json_pipelines(str(legacy), db.id, "root")

    assert [c["pipeline_id"] for c in report["conflicts"]] == ["uuid-1"]
    fields = report["conflicts"][0]["fields"]
    assert "name" in fields and "model.id" in fields
    # DB row untouched, JSON untouched
    assert repository.get("uuid-1")["name"] == "DB VERSION"
    assert json.loads(legacy.read_text(encoding="utf-8"))["uuid-1"]["name"] == "A"


def test_conflict_blocks_completion(db, legacy):
    repository.create(pipeline_id="uuid-1", name="DB VERSION", config={"name": "DB VERSION"})
    report = pm.run_migration_if_needed(str(legacy), db.id, "root")
    assert report["marked_complete"] is False
    assert app_state.is_pipeline_migration_complete() is False


def test_equivalent_config_is_not_a_conflict(db, legacy):
    repository.create(pipeline_id="uuid-2", name="B",
                      config={k: v for k, v in ENTRY_B.items()})
    report = pm.migrate_json_pipelines(str(legacy), db.id, "root")
    assert report["conflicts"] == []
    assert "uuid-2" in report["skipped_existing"]


# ---------------------------------------------------------------- completion --
def test_completion_flag_set_after_verified_migration(db, legacy):
    report = pm.run_migration_if_needed(str(legacy), db.id, "root")
    assert report["verified"] is True and report["marked_complete"] is True
    assert app_state.is_pipeline_migration_complete() is True


def test_completed_migration_never_reimports(db, legacy):
    pm.run_migration_if_needed(str(legacy), db.id, "root")
    again = pm.run_migration_if_needed(str(legacy), db.id, "root")
    assert again == {"already_complete": True}


def test_deleted_pipeline_is_not_resurrected_by_stale_json(db, legacy):
    """The reason completion state exists at all."""
    pm.run_migration_if_needed(str(legacy), db.id, "root")
    repository.delete("uuid-1")
    pm.run_migration_if_needed(str(legacy), db.id, "root")
    assert repository.get("uuid-1") is None


def test_completion_is_not_inferred_from_backup_or_table_contents(db, legacy):
    """Completion must be explicit application state, not a side effect."""
    pm.backup_json(str(legacy))
    repository.create(pipeline_id="unrelated", config={})
    assert app_state.is_pipeline_migration_complete() is False


# -------------------------------------------------------------- verification --
def test_verify_detects_missing_and_mismatched(db, legacy):
    ok, problems = pm.verify_migration(str(legacy))
    assert ok is False and len(problems) == 2      # nothing imported yet

    pm.migrate_json_pipelines(str(legacy), db.id, "root")
    ok, problems = pm.verify_migration(str(legacy))
    assert ok is True and problems == []

    repository.update("uuid-1", config={"name": "tampered"})
    ok, problems = pm.verify_migration(str(legacy))
    assert ok is False and problems[0]["problem"] == "config_mismatch"
