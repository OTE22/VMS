"""Tiny key/value store for APPLICATION state held in PostgreSQL.

Exists so data migrations have a completion flag that is independent of:
  - alembic_version   (tracks SCHEMA migrations only)
  - the audit log     (an event record is not a state machine)
  - table emptiness   (the bug that broke the original importer)
  - backup-file existence (a file on disk is not application state)
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from .auth.db import get_session
from .data_models import AppState

logger = logging.getLogger("InferenceNode.app_state")

# Data-migration keys (versioned so a future re-migration is a new key, never a reset).
PIPELINE_JSON_MIGRATION_KEY = "pipeline_json_migration_v1"
STATE_COMPLETED = "completed"


def get_state(key: str) -> Optional[str]:
    with get_session() as s:
        row = s.execute(select(AppState).where(AppState.key == key)).scalar_one_or_none()
        return row.value if row is not None else None


def set_state(key: str, value: str) -> None:
    with get_session() as s:
        row = s.execute(select(AppState).where(AppState.key == key)).scalar_one_or_none()
        if row is None:
            s.add(AppState(key=key, value=value))
        else:
            row.value = value
    logger.info(f"app_state[{key}] = {value}")


def is_pipeline_migration_complete() -> bool:
    """True once the JSON -> PostgreSQL pipeline migration has been verified.

    After this returns True the legacy JSON must never be auto-imported again,
    otherwise deleting a pipeline in the DB could be undone by the stale file.
    """
    return get_state(PIPELINE_JSON_MIGRATION_KEY) == STATE_COMPLETED


def mark_pipeline_migration_complete() -> None:
    set_state(PIPELINE_JSON_MIGRATION_KEY, STATE_COMPLETED)
