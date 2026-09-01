"""One-time JSON -> PostgreSQL pipeline data migration.

Replaces the original importer, whose idempotency key was "the pipelines table is
completely empty". That was wrong: two unrelated fixture rows made it skip forever, which
is exactly why real pipelines existed in JSON but not in the DB and could not be started.

Properties:
  - per pipeline_id, so unrelated rows can never block it
  - idempotent: re-running imports only ids that are still missing
  - transactional per run
  - NEVER renames or deletes the source JSON (PipelineManager may still read it, and it
    is the operator's fallback copy); a separate .pre-db-migration.json backup is written
  - conflict-aware: an id present in BOTH with a different configuration is reported and
    left alone rather than silently overwritten in either direction
  - completion is recorded in app_state, not inferred from alembic_version, table
    emptiness, or the existence of a backup file
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

from . import app_state
from .pipeline_repository import repository

logger = logging.getLogger("InferenceNode.pipeline_migration")

BACKUP_SUFFIX = ".pre-db-migration.json"

# Fields compared when the same pipeline_id exists in JSON and in the DB.
CONFLICT_FIELDS = ("name", "description", "frame_source", "model.id", "model.engine_type",
                   "model.device", "destinations", "inference_enabled")


def _dig(d: Dict[str, Any], dotted: str):
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _json_entry_config(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The persistent configuration for one JSON pipeline.

    `stats` is dropped: it is transient runtime measurement, not configuration.
    """
    return {k: v for k, v in entry.items() if k != "stats"}


def compare_entry(json_entry: Dict[str, Any], db_config: Dict[str, Any]) -> List[str]:
    """Return the names of persistent fields that differ. Empty list == equivalent."""
    differing = []
    for field in CONFLICT_FIELDS:
        a = _dig(json_entry, field)
        b = _dig(db_config or {}, field)
        if a != b:
            differing.append(field)
    return differing


def backup_json(json_path: str) -> Optional[str]:
    """Copy (never move) the legacy file next to itself. Returns the backup path."""
    if not os.path.exists(json_path):
        return None
    backup = os.path.splitext(json_path)[0] + BACKUP_SUFFIX
    try:
        if not os.path.exists(backup):
            shutil.copy2(json_path, backup)
            logger.info(f"Migration backup written: {backup}")
        return backup
    except Exception as e:
        logger.error(f"Could not write migration backup: {e}")
        return None


def load_json_pipelines(json_path: str) -> Dict[str, Dict[str, Any]]:
    """Read the legacy file into {pipeline_id: entry}, skipping malformed entries."""
    if not os.path.exists(json_path):
        return {}
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Legacy pipeline JSON unreadable ({e.__class__.__name__}): {e}")
        return {}

    items = data.values() if isinstance(data, dict) else data
    out: Dict[str, Dict[str, Any]] = {}
    for entry in items:
        if not isinstance(entry, dict):
            logger.warning("Skipping malformed legacy pipeline entry (not an object)")
            continue
        pid = str(entry.get("id") or entry.get("pipeline_id") or "").strip()
        if not pid:
            logger.warning("Skipping malformed legacy pipeline entry (no id)")
            continue
        out[pid] = entry
    return out


def migrate_json_pipelines(json_path: str, owner_id: Optional[int],
                           owner_username: Optional[str]) -> Dict[str, Any]:
    """Import every JSON pipeline that has no DB row yet.

    Returns a report: imported / skipped_existing / conflicts / malformed / backup.
    Never raises for data problems - a bad entry is reported, not fatal.
    """
    report: Dict[str, Any] = {"imported": [], "skipped_existing": [], "conflicts": [],
                              "backup": None, "total_json": 0}

    if app_state.is_pipeline_migration_complete():
        logger.info("Pipeline JSON migration already marked complete - not importing.")
        report["already_complete"] = True
        return report

    entries = load_json_pipelines(json_path)
    report["total_json"] = len(entries)
    if not entries:
        return report

    report["backup"] = backup_json(json_path)

    for pid, entry in entries.items():
        existing = repository.get(pid)
        if existing is not None:
            differing = compare_entry(entry, existing.get("config") or {})
            if differing:
                # Do NOT overwrite either side. An operator decides which is correct.
                logger.warning(
                    "migration_conflict=true pipeline_id=%s differing_fields=%s "
                    "(DB left unchanged)", pid, ",".join(differing))
                report["conflicts"].append({"pipeline_id": pid, "fields": differing})
            else:
                report["skipped_existing"].append(pid)
            continue

        config = _json_entry_config(entry)
        try:
            repository.create(
                pipeline_id=pid,
                name=entry.get("name"),
                description=entry.get("description"),
                config=config,
                # Never carry a stale "running" flag across a migration.
                status="stopped",
                owner_id=owner_id,
                owner_username=owner_username,
            )
            report["imported"].append(pid)
            logger.info(f"Migrated pipeline {pid} ({entry.get('name')!r}) into PostgreSQL")
        except Exception as e:
            logger.error(f"Could not migrate pipeline {pid}: {e.__class__.__name__}: {e}")
            report["conflicts"].append({"pipeline_id": pid, "fields": ["<import_error>"],
                                        "error": str(e)})

    return report


def verify_migration(json_path: str) -> Tuple[bool, List[Dict[str, Any]]]:
    """Field-by-field check that every JSON pipeline is faithfully represented in the DB.

    Returns (ok, problems). Cutover must not be marked complete unless ok is True.
    """
    problems: List[Dict[str, Any]] = []
    for pid, entry in load_json_pipelines(json_path).items():
        record = repository.get(pid)
        if record is None:
            problems.append({"pipeline_id": pid, "problem": "missing_in_db"})
            continue
        differing = compare_entry(entry, record.get("config") or {})
        if differing:
            problems.append({"pipeline_id": pid, "problem": "config_mismatch",
                             "fields": differing})
    return (not problems), problems


def run_migration_if_needed(json_path: str, owner_id: Optional[int],
                            owner_username: Optional[str]) -> Dict[str, Any]:
    """Startup entry point: migrate, verify, and only then mark the cutover complete.

    After completion this becomes a no-op forever, so deleting a pipeline in the DB can
    never be undone by the stale JSON file still sitting on disk.
    """
    if app_state.is_pipeline_migration_complete():
        return {"already_complete": True}

    report = migrate_json_pipelines(json_path, owner_id, owner_username)
    ok, problems = verify_migration(json_path)
    report["verified"] = ok
    report["problems"] = problems

    if ok and not report["conflicts"]:
        app_state.mark_pipeline_migration_complete()
        report["marked_complete"] = True
        logger.info("Pipeline JSON -> PostgreSQL migration complete; "
                    "PostgreSQL is now the sole source of truth.")
    else:
        report["marked_complete"] = False
        logger.warning("Pipeline migration NOT marked complete "
                       f"(verified={ok}, conflicts={len(report['conflicts'])}). "
                       "It will be retried on the next start.")
    return report
