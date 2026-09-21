"""Alembic 0004 on REAL isolated PostgreSQL (Phase 4).

Proves: upgrade head succeeds with `models` empty (the live situation); the registry
tables and CHECK constraints exist; the vocabulary CHECKs reject undefined lifecycle
values; the engine `enabled => AVAILABLE + PASSED` invariant is enforced BY THE DATABASE;
pipelines.model_id exists WITHOUT an FK (0005 adds it later); downgrade to 0003 and
re-upgrade are safe.

Runs via scripts/pg-test.sh (isolated DB armeye_test_*, never the dev DB).
"""
import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, DBAPIError

from conftest import REPO


# The current migration head. Bump when a migration is added - the point of these
# tests is that upgrade/downgrade round-trips cleanly, not which revision is newest.
HEAD = "0008_publisher_description"


def _alembic(pg, *args):
    env = dict(os.environ, ARMYEYE_DATABASE_URL=pg["url"], ARMYEYE_ARTIFACT_ROOT=pg["artifact_root"])
    return subprocess.run([sys.executable, "-m", "alembic", "-c", os.path.join(REPO, "alembic.ini"), *args],
                          cwd=REPO, env=env, capture_output=True, text=True)


def test_head_is_0004_and_registry_tables_exist(pg_guard):
    with pg_guard["engine"].connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD
        tables = {r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))}
    assert {"model_representations", "model_artifacts", "inference_engines", "publishers",
            "node_settings", "media_assets", "pipeline_thumbnails"} <= tables


def test_models_table_was_extended_not_replaced(pg_guard):
    with pg_guard["engine"].connect() as c:
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='models'"))}
    assert {"model_id", "uploader_id", "status", "validation_status", "reason", "task",
            "framework", "version", "updated_at"} <= cols
    assert "models_v2" not in cols


def test_0004_alone_has_no_model_fk_and_0005_adds_it(pg_guard):
    """0004 = nullable column only; 0005 = FK ON DELETE RESTRICT + consistency CHECK."""
    down = _alembic(pg_guard, "downgrade", "0004_artifact_registry")
    assert down.returncode == 0, down.stdout + down.stderr
    with pg_guard["engine"].connect() as c:
        fks = [r[0] for r in c.execute(text(
            "SELECT conname FROM pg_constraint WHERE conrelid='pipelines'::regclass AND contype='f'"))]
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='pipelines'"))}
    assert "model_id" in cols and "fk_pipelines_model" not in fks
    up = _alembic(pg_guard, "upgrade", "head")
    assert up.returncode == 0, up.stdout + up.stderr
    with pg_guard["engine"].connect() as c:
        fks = [r[0] for r in c.execute(text(
            "SELECT conname FROM pg_constraint WHERE conrelid='pipelines'::regclass AND contype='f'"))]
        cks = {r[0]: r[1] for r in c.execute(text(
            "SELECT conname, convalidated FROM pg_constraint WHERE conrelid='pipelines'::regclass AND contype='c'"))}
    assert "fk_pipelines_model" in fks
    assert cks.get("ck_pipelines_model_ref_consistent") is True, "CHECK must be VALIDATED with 0 violators"


def test_undefined_lifecycle_status_is_rejected_by_the_database(pg_guard):
    with pg_guard["engine"].begin() as c:
        with pytest.raises((IntegrityError, DBAPIError)):
            c.execute(text("INSERT INTO models (model_id, status) VALUES ('m-bad', 'NEEDS_REVIEW')"))
    with pg_guard["engine"].begin() as c:
        with pytest.raises((IntegrityError, DBAPIError)):
            c.execute(text("INSERT INTO models (model_id, validation_status) VALUES ('m-bad2', 'MAYBE')"))


def test_valid_lifecycle_values_are_accepted(pg_guard):
    with pg_guard["engine"].begin() as c:
        c.execute(text("INSERT INTO models (model_id, status, validation_status, reason) "
                       "VALUES ('m-ok', 'MISSING', 'PENDING', 'AMBIGUOUS_LEGACY_PATH')"))
        c.execute(text("DELETE FROM models WHERE model_id='m-ok'"))


def test_pg_rejects_enabled_engine_without_passed_validation(pg_guard):
    """The exact invariant enabled => status=AVAILABLE AND validation_status=PASSED."""
    bad_rows = [
        ("AVAILABLE", "FAILED"),
        ("AVAILABLE", "PENDING"),
        ("STAGING", "PASSED"),
        ("CORRUPT", "HASH_MISMATCH"),
    ]
    for status, vstatus in bad_rows:
        with pg_guard["engine"].begin() as c:
            with pytest.raises((IntegrityError, DBAPIError)):
                c.execute(text(
                    "INSERT INTO inference_engines (engine_key, origin, status, validation_status, "
                    "enabled, relative_path, sha256) VALUES ('e', 'custom', :s, :v, TRUE, "
                    "'engines/e/engine.py', :h)"), {"s": status, "v": vstatus, "h": "a" * 64})
    with pg_guard["engine"].begin() as c:
        c.execute(text(
            "INSERT INTO inference_engines (engine_key, origin, status, validation_status, enabled, "
            "relative_path, sha256) VALUES ('e-ok', 'custom', 'AVAILABLE', 'PASSED', TRUE, "
            "'engines/e-ok/engine.py', :h)"), {"h": "b" * 64})
        c.execute(text("DELETE FROM inference_engines WHERE engine_key='e-ok'"))


def test_custom_engine_requires_artifact_path_and_hash(pg_guard):
    with pg_guard["engine"].begin() as c:
        with pytest.raises((IntegrityError, DBAPIError)):
            c.execute(text("INSERT INTO inference_engines (engine_key, origin, status, validation_status) "
                           "VALUES ('e2', 'custom', 'STAGING', 'PENDING')"))
    with pg_guard["engine"].begin() as c:   # builtin may have NULL path/hash
        c.execute(text("INSERT INTO inference_engines (engine_key, origin, status, validation_status) "
                       "VALUES ('e-builtin', 'builtin', 'AVAILABLE', 'PASSED')"))
        c.execute(text("DELETE FROM inference_engines WHERE engine_key='e-builtin'"))


def test_sha256_format_and_size_checks(pg_guard):
    with pg_guard["engine"].begin() as c:
        c.execute(text("INSERT INTO models (model_id, status, validation_status) VALUES ('m-a', 'STAGING', 'PENDING')"))
        mid = c.execute(text("SELECT id FROM models WHERE model_id='m-a'")).scalar_one()
        c.execute(text("INSERT INTO model_representations (model_id, format, kind, status, validation_status) "
                       "VALUES (:m, 'pt', 'primary', 'STAGING', 'PENDING')"), {"m": mid})
        rid = c.execute(text("SELECT id FROM model_representations WHERE model_id=:m"), {"m": mid}).scalar_one()
    for bad in ({"h": "not-a-hash", "s": 1}, {"h": "c" * 64, "s": -5}):
        with pg_guard["engine"].begin() as c:
            with pytest.raises((IntegrityError, DBAPIError)):
                c.execute(text("INSERT INTO model_artifacts (model_id, representation_id, relative_path, sha256, "
                               "size_bytes, status, validation_status) VALUES (:m, :r, 'models/m-a/x.pt', :h, :s, "
                               "'STAGING', 'PENDING')"), {"m": mid, "r": rid, **bad})
    with pg_guard["engine"].begin() as c:
        c.execute(text("DELETE FROM models WHERE model_id='m-a'"))   # cascades repr/artifacts


def test_downgrade_to_0003_and_reupgrade_are_safe(pg_guard):
    down = _alembic(pg_guard, "downgrade", "0003_pipeline_access")
    assert down.returncode == 0, down.stdout + down.stderr
    with pg_guard["engine"].connect() as c:
        tables = {r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))}
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='pipelines'"))}
    assert "model_artifacts" not in tables and "model_id" not in cols
    up = _alembic(pg_guard, "upgrade", "head")
    assert up.returncode == 0, up.stdout + up.stderr
    with pg_guard["engine"].connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD


# ------------------------------------------------------------------ registry migration ON PostgreSQL
def test_models_json_migration_populates_registry_on_postgres(pg_guard, tmp_path):
    """The real cutover path: legacy models_metadata.json + bytes -> PG rows + ARTIFACT_ROOT,
    executed against the isolated PostgreSQL database (CHECKs, FKs, BIGINT fingerprints)."""
    import hashlib, json, os
    from InferenceNode import artifact_paths as ap, model_registry as reg, app_state
    from InferenceNode.registry_migration import migrate_models_registry, MODELS_MARKER

    legacy = tmp_path / "model_repository"; (legacy / "models").mkdir(parents=True)
    (legacy / "models" / "pgm.pt").write_bytes(b"pg-model-bytes")
    d = legacy / "models" / "pgm_openvino_model"; d.mkdir()
    (d / "pgm.xml").write_bytes(b"<x/>"); (d / "pgm.bin").write_bytes(b"\x01\x02")
    (legacy / "models_metadata.json").write_text(json.dumps({"pgm": {
        "id": "pgm", "name": "pgm", "original_filename": "pgm.pt", "stored_filename": "pgm.pt",
        "stored_path": r"C:\legacy\pgm.pt", "engine_type": "ultralytics", "file_extension": ".pt"}}))
    rep = migrate_models_registry(str(legacy / "models_metadata.json"), str(legacy / "models"), force=True)
    assert rep.available == 1 and rep.failed == 0
    m = reg.get_model("pgm")
    assert m["status"] == "AVAILABLE"
    prim = next(r for r in m["representations"] if r["kind"] == "primary")
    path = ap.resolve("models", prim["artifacts"][0]["relative_path"])
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == prim["artifacts"][0]["sha256"]
    ov = next(r for r in m["representations"] if r["format"] == "openvino")
    assert len(ov["artifacts"]) == 2 and ov["manifest_sha256"]
    with pg_guard["engine"].connect() as c:
        from sqlalchemy import text
        n = c.execute(text("SELECT count(*) FROM model_artifacts")).scalar_one()
        assert n == 3   # pt + xml + bin, one row per physical file
        assert c.execute(text("SELECT status FROM models WHERE model_id='pgm'")).scalar_one() == "AVAILABLE"
    assert app_state.get_state(MODELS_MARKER) == app_state.STATE_COMPLETED


# ------------------------------------------------------------------ 0005 semantics on PostgreSQL
def _seed_model(c, mid):
    c.execute(text("INSERT INTO models (model_id, status, validation_status) VALUES (:m, 'AVAILABLE', 'PASSED') "
                   "ON CONFLICT DO NOTHING"), {"m": mid})


def test_pg_rejects_divergent_model_reference(pg_guard):
    """Direct-SQL negative test: the CHECK refuses model_id != config.model.id."""
    import json
    with pg_guard["engine"].begin() as c:          # seed in its OWN transaction
        _seed_model(c, "mA"); _seed_model(c, "mB")
    with pg_guard["engine"].begin() as c:          # the negative insert aborts only this one
        with pytest.raises((IntegrityError, DBAPIError)):
            c.execute(text("INSERT INTO pipelines (pipeline_id, name, config, status, model_id) "
                           "VALUES ('p-div', 'x', :cfg, 'stopped', 'mA')"),
                      {"cfg": json.dumps({"model": {"id": "mB"}})})
    with pg_guard["engine"].begin() as c:   # consistent pair accepted; NULL arms accepted
        c.execute(text("INSERT INTO pipelines (pipeline_id, name, config, status, model_id) "
                       "VALUES ('p-ok', 'x', :cfg, 'stopped', 'mA')"), {"cfg": json.dumps({"model": {"id": "mA"}})})
        c.execute(text("INSERT INTO pipelines (pipeline_id, name, config, status, model_id) "
                       "VALUES ('p-null', 'x', :cfg, 'stopped', 'mA')"), {"cfg": json.dumps({"model": {}})})
        c.execute(text("DELETE FROM pipelines WHERE pipeline_id IN ('p-ok','p-null')"))
        c.execute(text("DELETE FROM models WHERE model_id IN ('mA','mB')"))


def test_fk_restrict_blocks_deleting_a_referenced_model(pg_guard):
    import json
    with pg_guard["engine"].begin() as c:
        _seed_model(c, "mRef")
    with pg_guard["engine"].begin() as c:
        c.execute(text("INSERT INTO pipelines (pipeline_id, name, config, status, model_id) "
                       "VALUES ('p-ref', 'x', :cfg, 'stopped', 'mRef')"), {"cfg": json.dumps({"model": {"id": "mRef"}})})
    with pg_guard["engine"].begin() as c:
        with pytest.raises((IntegrityError, DBAPIError)):
            c.execute(text("DELETE FROM models WHERE model_id='mRef'"))
    with pg_guard["engine"].begin() as c:
        c.execute(text("DELETE FROM pipelines WHERE pipeline_id='p-ref'"))
        c.execute(text("DELETE FROM models WHERE model_id='mRef'"))


def test_repository_keeps_column_and_json_in_sync_on_postgres(pg_guard):
    """create / update / duplicate-shaped create all go through the one sync point."""
    from InferenceNode.pipeline_repository import repository
    with pg_guard["engine"].begin() as c:
        _seed_model(c, "mS1"); _seed_model(c, "mS2")
    r = repository.create(pipeline_id="p-sync", name="s", config={"model": {"id": "mS1", "device": "cpu"}})
    assert r["model_id"] == "mS1" and r["config"]["model"]["id"] == "mS1"
    r = repository.update("p-sync", config={"model": {"id": "mS2", "device": "cpu"}})
    assert r["model_id"] == "mS2" and r["config"]["model"]["id"] == "mS2"
    with pg_guard["engine"].connect() as c:
        col, cfg = c.execute(text("SELECT model_id, config->'model'->>'id' FROM pipelines WHERE pipeline_id='p-sync'")).one()
    assert col == cfg == "mS2"
    repository.delete("p-sync")
    with pg_guard["engine"].begin() as c:
        c.execute(text("DELETE FROM models WHERE model_id IN ('mS1','mS2')"))


def test_bootstrap_orders_0004_then_legacy_models_then_0005(pg_guard, tmp_path, monkeypatch):
    """A deployment below 0005 with pipelines referencing a legacy model: bootstrap must
    populate the model registry (from the legacy JSON + bytes) BEFORE 0005 audits and
    back-fills pipelines.model_id - otherwise every reference is UNKNOWN_MODEL/NULL."""
    import hashlib, json
    from InferenceNode.auth import bootstrap as bs
    from InferenceNode.auth import db as auth_db
    down = _alembic(pg_guard, "downgrade", "0003_pipeline_access")
    assert down.returncode == 0, down.stderr
    # legacy source: one model in models_metadata.json + bytes; one pipeline referencing it
    legacy = tmp_path / "legacy"; (legacy / "model_repository" / "models").mkdir(parents=True)
    weights = b"legacy-weights-" + b"x" * 100
    (legacy / "model_repository" / "models" / "yolo_legacy.pt").write_bytes(weights)
    (legacy / "model_repository" / "models_metadata.json").write_text(json.dumps({
        "yolo_legacy_1234": {"id": "yolo_legacy_1234", "name": "yolo_legacy", "engine_type": "ultralytics",
                             "original_filename": "yolo_legacy.pt", "stored_filename": "yolo_legacy.pt",
                             "stored_path": str(legacy / "model_repository" / "models" / "yolo_legacy.pt"),
                             "file_size": len(weights), "upload_date": "2026-01-01T00:00:00",
                             "description": "", "file_extension": ".pt"}}))
    with pg_guard["engine"].begin() as c:
        c.execute(text("DELETE FROM pipelines"))
        c.execute(text("INSERT INTO pipelines (pipeline_id, name, config, status, created_at, updated_at) VALUES "
                       "('legacy-p1', 'lp', :cfg, 'stopped', now(), now())"),
                  {"cfg": json.dumps({"name": "lp", "model": {"id": "yolo_legacy_1234"}, "frame_source": {}, "destinations": []})})
        c.execute(text("DELETE FROM app_state WHERE key='models_registry_to_postgres_v1'"))
    monkeypatch.setenv("ARMYEYE_ARTIFACT_ROOT", pg_guard["artifact_root"])
    monkeypatch.setenv("ARMYEYE_DATABASE_URL", pg_guard["url"])       # alembic env reads it in-process
    bs.bootstrap_database(legacy_root=str(legacy))
    with pg_guard["engine"].connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD
        assert c.execute(text("SELECT status FROM models WHERE model_id='yolo_legacy_1234'")).scalar_one() == "AVAILABLE"
        assert c.execute(text("SELECT model_id FROM pipelines WHERE pipeline_id='legacy-p1'")).scalar_one() == "yolo_legacy_1234"
        sha = c.execute(text("SELECT sha256 FROM model_artifacts")).scalar_one()
    assert sha == hashlib.sha256(weights).hexdigest()
    with pg_guard["engine"].begin() as c:
        c.execute(text("DELETE FROM pipelines WHERE pipeline_id='legacy-p1'"))
    # already at head: bootstrap is a plain no-op
    bs.bootstrap_database(legacy_root=str(legacy))


def test_publisher_description_migration_preserves_existing_rows(pg_guard):
    """An existing favorite survives upgrade; description is a nullable addition."""
    from InferenceNode import publisher_store as pst
    from InferenceNode.auth import db
    down = _alembic(pg_guard, "downgrade", "0007_reference_integrity")
    assert down.returncode == 0, down.stdout + down.stderr
    try:
        with pg_guard['engine'].begin() as c:
            c.execute(text("INSERT INTO publishers (publisher_id, name, type, kind, enabled, config) "
                           "VALUES ('migration-description', 'Existing', 'null', 'favorite', true, '{}')"))
    finally:
        up = _alembic(pg_guard, "upgrade", "head")
        assert up.returncode == 0, up.stdout + up.stderr
    try:
        row = pst.get_publisher('migration-description')
        assert row['name'] == 'Existing' and row['description'] is None
        pst.update_publisher(row['id'], description='Survives a new session')
        assert pst.get_publisher(row['id'])['description'] == 'Survives a new session'
        with pytest.raises(RuntimeError):
            with db.get_session() as session:
                pst.update_publisher(row['id'], description='must roll back', session=session)
                raise RuntimeError('later write failed')
        assert pst.get_publisher(row['id'])['description'] == 'Survives a new session'
    finally:
        pst.delete_publisher('migration-description')
