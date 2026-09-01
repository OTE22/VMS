"""Startup database bootstrap for ArmyEye's own DB.

Runs Alembic migrations to head (serialized across workers by a PostgreSQL advisory
lock so concurrent gunicorn workers don't race), then seeds the initial admin. Fails
clearly if ARMYEYE_DATABASE_URL is missing.
"""
from __future__ import annotations

import os
import logging
from typing import Optional

from . import db as auth_db
from .service import seed_admin

logger = logging.getLogger("InferenceNode.auth")

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


REGISTRY_FOUNDATION_REV = "0004_artifact_registry"
PIPELINE_INTEGRITY_REV = "0005_pipeline_model_integrity"


def _alembic_cfg():
    from alembic.config import Config
    cfg = Config(os.path.join(_REPO, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_REPO, "InferenceNode", "migrations"))
    return cfg


def _alembic_upgrade(target: str = "head") -> None:
    from alembic import command
    command.upgrade(_alembic_cfg(), target)


def _current_revisions() -> set:
    from alembic.runtime.migration import MigrationContext
    with auth_db.get_engine().connect() as conn:
        return set(MigrationContext.configure(conn).get_current_heads())


def _revision_applied(rev: str) -> bool:
    """True when `rev` is the current head or an ancestor of it."""
    from alembic.script import ScriptDirectory
    heads = _current_revisions()
    if not heads:
        return False
    script = ScriptDirectory.from_config(_alembic_cfg())
    for h in heads:
        if h == rev:
            return True
        for r in script.iterate_revisions(h, "base"):
            if r.revision == rev:
                return True
    return False


def _alembic_upgrade_head(legacy_root: Optional[str] = None) -> None:
    """Approved ordering (never combined): 0004 registry foundation -> legacy MODEL
    registry data migration (application code; needs the 0004 tables) -> 0005 pipeline
    ->model back-fill + FK RESTRICT + consistency CHECK. So on a deployment that is
    still below 0005, the model registry is populated BEFORE 0005 audits/back-fills
    pipeline references; otherwise pipelines referencing legacy models would all be
    classified UNKNOWN_MODEL and left NULL. Already-at-head deployments: plain no-op."""
    if _revision_applied(PIPELINE_INTEGRITY_REV):
        _alembic_upgrade("head")
        return
    _alembic_upgrade(REGISTRY_FOUNDATION_REV)
    logger.info("ArmyEye DB migrated to %s (registry foundation)", REGISTRY_FOUNDATION_REV)
    try:
        from InferenceNode.registry_migration import migrate_models_registry
        base = legacy_root or os.environ.get("ARMYEYE_LEGACY_ROOT") or os.path.join(_REPO, "InferenceNode")
        repo = os.path.join(base, "model_repository")
        rep = migrate_models_registry(os.path.join(repo, "models_metadata.json"), os.path.join(repo, "models"))
        logger.info("Legacy model registry migration before %s: %s", PIPELINE_INTEGRITY_REV, rep.as_dict())
        if rep.blocking:
            logger.error("Legacy model registry migration has FAILED records; %s will still run - "
                         "pipelines whose model is not registered are reported (never guessed)",
                         PIPELINE_INTEGRITY_REV)
    except Exception as e:  # noqa: BLE001 - reported; 0005 refuses to guess unknown references anyway
        logger.error("Legacy model registry migration before %s failed: %s", PIPELINE_INTEGRITY_REV, e)
    _alembic_upgrade("head")


def bootstrap_database(legacy_root: Optional[str] = None) -> None:
    """Migrate + seed under an advisory lock. Raises if the DB is not configured."""
    auth_db.require_configured()
    with auth_db.advisory_lock():
        try:
            _alembic_upgrade_head(legacy_root)
            logger.info("ArmyEye DB migrations applied (alembic upgrade head)")
        except Exception as e:
            # Dev/test fallback: create tables directly from metadata so a broken
            # alembic setup never hard-blocks startup. Production uses migrations.
            logger.warning(f"Alembic upgrade failed ({e}); falling back to create_all")
            from .models import Base
            import InferenceNode.data_models  # noqa: F401 (register pipelines/models on Base)
            Base.metadata.create_all(auth_db.get_engine())
        # Seeding is idempotent (only acts on an empty users table).
        seed_admin()
