"""Prove the safety guardrails themselves (Phase 1)."""
import os

import pytest
from sqlalchemy import text



def test_dev_database_url_is_stripped_for_the_session():
    assert "ARMYEYE_DATABASE_URL" not in os.environ
    assert "ARMYEYE_DATABASE_URL_FILE" not in os.environ


def test_bare_init_engine_cannot_reach_dev_db():
    """Without the URL, the app's own resolver yields None -> no engine -> loud failure,
    never a silent connection to the live database."""
    from InferenceNode.auth import db as auth_db
    auth_db._engine = None; auth_db._SessionLocal = None
    assert auth_db.init_engine() is None
    with pytest.raises(RuntimeError):
        with auth_db.get_session():
            pass


def test_isolated_pg_is_a_fixture_created_database_with_migrations(pg_guard):
    with pg_guard["engine"].connect() as c:
        db = c.execute(text("SELECT current_database()")).scalar_one()
        assert db.startswith("armeye_test_") and db != "armeye"
        head = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert head  # migrations ran on real PostgreSQL
        tables = {r[0] for r in c.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"))}
    assert {"users", "pipelines", "pipeline_user_access", "models", "app_state"} <= tables


def test_artifact_root_is_isolated(pg_guard):
    root = os.environ["ARMYEYE_ARTIFACT_ROOT"]
    assert os.path.isdir(root) and "armeye-e2e-" in root


def test_in_process_alembic_does_not_leak_repo_dotenv(pg_guard, monkeypatch):
    """Running migrations in-process (bootstrap / fixtures) must not load the repo .env:
    that would re-inject the development DATABASE_URL and deployment variables such as
    WEBHOOK_BASE_URL into the test process after the session guard stripped them."""
    import os
    from InferenceNode.auth import bootstrap as bs
    monkeypatch.setenv("ARMYEYE_DATABASE_URL", pg_guard["url"])
    monkeypatch.delenv("WEBHOOK_BASE_URL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    bs._alembic_upgrade("head")                       # imports migrations/env.py in-process
    assert os.environ.get("ARMYEYE_DATABASE_URL") == pg_guard["url"]
    assert "WEBHOOK_BASE_URL" not in os.environ and "ADMIN_PASSWORD" not in os.environ
