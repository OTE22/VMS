"""Isolated PostgreSQL + isolated ARTIFACT_ROOT for persistence-contract tests.

Design (matches the plan's hard guard):

* The test database is CREATED by this fixture (name `armeye_test_<random>`), migrated with
  `alembic upgrade head` on real PostgreSQL, and DROPPED at session end. It is never the
  development `armeye` DB.
* The artifact root is a fresh temp directory `armeye-e2e-<random>/` created by the fixture.
* Before yielding, the guard verifies BOTH `SELECT current_database()` == the fixture DB
  AND `realpath(ARTIFACT_ROOT)` is inside the fixture temp dir. If either fails the session
  aborts - no fallback to development persistence is possible.

Where does the server come from?  ARMYEYE_TEST_PG_ADMIN_URL (an admin-capable URL on the same
PostgreSQL server as the dev DB, e.g. `postgresql://armeye:***@db:5432/postgres` inside the
VMS container, or `...@127.0.0.1:5433/postgres` on a host where the dev overlay publishes the
port). When unset and docker is available, the fixture starts a THROWAWAY `postgres:16-alpine`
container on 127.0.0.1:<random port> for the session (a separate server: the development
VMS-db is not even connected to) and removes it afterwards. When neither is possible,
PG-backed tests SKIP in ordinary runs and FAIL in readiness mode.
"""
import os
import secrets
import shutil
import subprocess
import sys
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import REPO, readiness_required


def _admin_url():
    return (os.environ.get("ARMYEYE_TEST_PG_ADMIN_URL") or "").strip()


def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _free_port() -> int:
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port


def _start_throwaway_pg():
    """Returns (admin_url, container_name) or (None, None)."""
    if not _docker_ok():
        return None, None
    import time
    port = _free_port(); pw = secrets.token_urlsafe(16); name = f"armeye-test-pg-{secrets.token_hex(3)}"
    run = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:5432",
                          "-e", "POSTGRES_USER=armeye_test", "-e", f"POSTGRES_PASSWORD={pw}",
                          "-e", "POSTGRES_DB=postgres", "postgres:16-alpine"], capture_output=True, text=True)
    if run.returncode != 0:
        return None, None
    url = f"postgresql://armeye_test:{pw}@127.0.0.1:{port}/postgres"
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            eng = create_engine(url, future=True)
            with eng.connect() as c:
                c.execute(text("SELECT 1"))
            eng.dispose()
            return url, name
        except Exception:  # noqa: BLE001
            time.sleep(1)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    return None, None


@pytest.fixture(scope="session")
def isolated_pg():
    """Yields dict(url=..., dbname=..., artifact_root=...). Session-scoped: one DB per run."""
    admin = _admin_url()
    throwaway = None
    if not admin:
        admin, throwaway = _start_throwaway_pg()
        if admin:
            os.environ["ARMYEYE_TEST_PG_ADMIN_URL"] = admin
    if not admin:
        readiness_required("ARMYEYE_TEST_PG_ADMIN_URL not set and no docker for a throwaway server - "
                           "isolated PostgreSQL unavailable")

    dbname = f"armeye_test_{secrets.token_hex(4)}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT", future=True)
    with admin_engine.connect() as c:
        c.execute(text(f'CREATE DATABASE "{dbname}"'))
    # render_as_string(hide_password=False): str(URL) masks the password as "***" in
    # SQLAlchemy 2, which is exactly what must NOT reach the alembic subprocess.
    test_url = make_url(admin).set(database=dbname).render_as_string(hide_password=False)

    artifact_root = tempfile.mkdtemp(prefix="armeye-e2e-")

    # Migrate with the real Alembic chain (this is the ONLY place tests exercise migrations).
    env = dict(os.environ, ARMYEYE_DATABASE_URL=test_url, ARMYEYE_ARTIFACT_ROOT=artifact_root)
    proc = subprocess.run([sys.executable, "-m", "alembic", "-c", os.path.join(REPO, "alembic.ini"),
                           "upgrade", "head"], cwd=REPO, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        _drop(admin_engine, dbname); shutil.rmtree(artifact_root, ignore_errors=True)
        pytest.fail(f"alembic upgrade head failed on isolated PG:\n{proc.stdout}\n{proc.stderr}")

    # ---- HARD GUARD: correct DB AND correct artifact root, else abort the session
    eng = create_engine(test_url, future=True)
    with eng.connect() as c:
        current = c.execute(text("SELECT current_database()")).scalar_one()
    if current != dbname:
        _drop(admin_engine, dbname); shutil.rmtree(artifact_root, ignore_errors=True)
        pytest.fail(f"HARD GUARD: connected to {current!r}, expected fixture DB {dbname!r} - aborting")
    if not os.path.realpath(artifact_root).startswith(os.path.realpath(tempfile.gettempdir())):
        _drop(admin_engine, dbname); shutil.rmtree(artifact_root, ignore_errors=True)
        pytest.fail("HARD GUARD: ARTIFACT_ROOT is not inside the temp directory - aborting")

    # Point the application at the fixture DB/root for the rest of the session.
    from InferenceNode.auth import db as auth_db
    auth_db._engine = None; auth_db._SessionLocal = None
    auth_db.init_engine(test_url)
    os.environ["ARMYEYE_ARTIFACT_ROOT"] = artifact_root
    yield {"url": test_url, "dbname": dbname, "artifact_root": artifact_root, "engine": eng}

    auth_db._engine = None; auth_db._SessionLocal = None
    eng.dispose()
    _drop(admin_engine, dbname)
    admin_engine.dispose()
    shutil.rmtree(artifact_root, ignore_errors=True)
    if throwaway:
        subprocess.run(["docker", "rm", "-f", throwaway], capture_output=True)


def _drop(admin_engine, dbname):
    with admin_engine.connect() as c:
        c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                       "WHERE datname = :d AND pid <> pg_backend_pid()"), {"d": dbname})
        c.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))


@pytest.fixture()
def pg_guard(isolated_pg):
    """Per-test re-assertion of the hard guard (cheap; catches any mid-session drift).
    Also re-points the application's DB engine + ARTIFACT_ROOT at the fixture: SQLite
    unit-test fixtures reset the global engine on teardown, and a session-scoped PG
    fixture must not depend on test ordering."""
    from InferenceNode.auth import db as auth_db
    if not auth_db.is_configured() or auth_db.get_engine() is None or             str(auth_db.get_engine().url.render_as_string(hide_password=False)) != isolated_pg["url"]:
        auth_db._engine = None; auth_db._SessionLocal = None
        auth_db.init_engine(isolated_pg["url"])
    os.environ["ARMYEYE_ARTIFACT_ROOT"] = isolated_pg["artifact_root"]
    with isolated_pg["engine"].connect() as c:
        assert c.execute(text("SELECT current_database()")).scalar_one() == isolated_pg["dbname"]
    root = os.environ.get("ARMYEYE_ARTIFACT_ROOT", "")
    assert os.path.realpath(root) == os.path.realpath(isolated_pg["artifact_root"])
    return isolated_pg
