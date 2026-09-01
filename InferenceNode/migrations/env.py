"""Alembic environment for ArmyEye's own database.

Resolves the URL from ARMYEYE_DATABASE_URL (normalized to a sync driver) and targets
ArmyEye's own metadata only. Never touches any other database.
"""
import logging
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the repo importable when Alembic runs from the repo root.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

# Load .env so ARMYEYE_DATABASE_URL is available for standalone CLI `alembic` runs.
# ONLY when the URL is not already provided: when the application (or a test fixture)
# runs migrations in-process it has already chosen its database, and loading the repo
# .env here would leak deployment variables (dev DATABASE_URL, WEBHOOK_BASE_URL, ...)
# into that process - the test-isolation guard depends on this.
if not (os.environ.get("ARMYEYE_DATABASE_URL") or os.environ.get("ARMYEYE_DATABASE_URL_FILE")):
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, ".env"), override=False)
    except Exception:
        pass

from InferenceNode.auth.db import normalize_database_url  # noqa: E402
from InferenceNode.auth.models import Base as AuthBase     # noqa: E402
import InferenceNode.data_models  # noqa: E402,F401  (registers pipelines/models on the shared Base)

config = context.config
# Configure logging from alembic.ini ONLY for standalone `alembic` CLI runs.
# When the app runs migrations in-process at startup (auth/bootstrap.py ->
# command.upgrade), root already carries the application's handlers - and
# fileConfig() would REPLACE them wholesale (alembic.ini: root=WARNING,
# console(stderr)) and, with the default disable_existing_loggers=True,
# silence every already-created logger. That is exactly how infernode.log
# went dead after startup and PUBLISH_* INFO lines vanished.
if config.config_file_name is not None and not logging.getLogger().handlers:
    try:
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    except Exception:
        pass

# All ArmyEye-owned tables share this metadata (Phase 3 pipeline/model models
# import the same Base, so autogenerate sees everything ArmyEye owns).
target_metadata = AuthBase.metadata


def _url() -> str:
    raw = os.environ.get("ARMYEYE_DATABASE_URL") or os.environ.get("ARMYEYE_DATABASE_URL_FILE_VALUE")
    if not raw:
        raise RuntimeError("ARMYEYE_DATABASE_URL is not set - cannot run migrations")
    return normalize_database_url(raw)


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata,
                      literal_binds=True, compare_type=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
