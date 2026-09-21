"""Sync SQLAlchemy engine for ArmyEye's OWN dedicated PostgreSQL database.

ArmyEye is fully self-contained: it owns its `users`, `audit_log`, `pipelines`, and
`models` tables in a database it controls end-to-end via ARMYEYE_DATABASE_URL. It has
NO dependency on FACE_DETECTOR's database. (The detection-webhook integration to
FACE_DETECTOR is unrelated and unaffected.)

If a `postgresql+asyncpg://...`-style URL is supplied we normalize it to a sync
driver (psycopg2). SQLite URLs pass through unchanged for tests.
"""
from __future__ import annotations

import os
import logging
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

logger = logging.getLogger("InferenceNode.auth")

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None


def normalize_database_url(url: str) -> str:
    """Convert an async/other DB URL into a sync psycopg2 URL usable by Flask.

    FACE_DETECTOR ships `postgresql+asyncpg://...`; a few examples:
      postgresql+asyncpg://u:p@h/db  -> postgresql+psycopg2://u:p@h/db
      postgresql+psycopg://u:p@h/db  -> postgresql+psycopg2://u:p@h/db
      postgresql://u:p@h/db          -> postgresql+psycopg2://u:p@h/db  (explicit)
      postgres://u:p@h/db            -> postgresql+psycopg2://u:p@h/db
    Non-postgres URLs (e.g. sqlite:// used by tests) are returned unchanged.
    """
    if not url:
        return url
    u = url.strip()
    if u.startswith("sqlite"):
        return u
    # Strip any async/alternate driver suffix, then force psycopg2 (installed here).
    if u.startswith("postgresql+"):
        u = "postgresql://" + u.split("://", 1)[1]
    elif u.startswith("postgres://"):
        u = "postgresql://" + u.split("://", 1)[1]
    if u.startswith("postgresql://"):
        u = "postgresql+psycopg2://" + u.split("://", 1)[1]
    return u


def _resolve_url() -> Optional[str]:
    """Resolve ARMYEYE_DATABASE_URL only (its own DB). Supports a *_FILE Docker
    secret. Deliberately NO fallback to DATABASE_URL / FACE_DETECTOR."""
    file_path = os.environ.get("ARMYEYE_DATABASE_URL_FILE")
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if raw:
                return raw
        except OSError as e:
            logger.error(f"Could not read ARMYEYE_DATABASE_URL_FILE: {e.__class__.__name__}")
    raw = os.environ.get("ARMYEYE_DATABASE_URL")
    return raw.strip() if raw else None


def init_engine(url: Optional[str] = None) -> Optional[Engine]:
    """Create ArmyEye's own DB engine once. Returns None if ARMYEYE_DATABASE_URL is
    not set (callers decide whether that is fatal; see require_configured())."""
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine
    raw = url or _resolve_url()
    if not raw:
        logger.error("ARMYEYE_DATABASE_URL is not set - ArmyEye requires its own database")
        return None
    normalized = normalize_database_url(raw)
    try:
        _engine = create_engine(
            normalized,
            pool_size=int(os.environ.get("ARMYEYE_DB_POOL_SIZE", "5")),
            max_overflow=int(os.environ.get("ARMYEYE_DB_MAX_OVERFLOW", "5")),
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True,
        )
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        safe = normalized.split("@")[-1] if "@" in normalized else normalized
        logger.info(f"Auth DB engine initialized (host/db: {safe})")
        return _engine
    except Exception as e:
        logger.error(f"Failed to initialize auth DB engine: {e}")
        _engine = None
        _SessionLocal = None
        return None


def is_configured() -> bool:
    return _SessionLocal is not None


def get_engine() -> Optional[Engine]:
    return _engine


def require_configured() -> None:
    """Raise a clear error if ArmyEye's DB is not configured. Call at startup to
    fail fast rather than limp along without a database."""
    if _SessionLocal is None:
        raise RuntimeError(
            "ArmyEye database is not configured. Set ARMYEYE_DATABASE_URL "
            "(ArmyEye's own PostgreSQL database) and restart.")


def is_postgres() -> bool:
    return _engine is not None and _engine.url.get_backend_name().startswith("postgresql")


@contextmanager
def advisory_lock(lock_key: int = 0x41524D59):  # 'ARMY'
    """Serialize a critical section (e.g. migrations, engine install) across
    processes using a PostgreSQL session advisory lock. No-op on non-Postgres
    (SQLite tests) where there is a single connection anyway."""
    if not is_postgres():
        yield
        return
    conn = _engine.connect()
    try:
        from sqlalchemy import text
        conn.exec_driver_sql("SELECT pg_advisory_lock(%s)", (lock_key,))
        yield
    finally:
        try:
            conn.exec_driver_sql("SELECT pg_advisory_unlock(%s)", (lock_key,))
        finally:
            conn.close()


@contextmanager
def get_session(session: Optional[Session] = None) -> Session:
    """Own a session/transaction, or borrow one without committing or closing it.

    The outer owner must let errors propagate to roll back the entire transaction.
    Raises RuntimeError if no engine is configured and no session was supplied.
    """
    # A caller-owned transaction is committed/rolled back only by its owner.
    if session is not None:
        yield session
        return
    if _SessionLocal is None:
        raise RuntimeError("Auth DB not configured (DATABASE_URL missing)")
    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
