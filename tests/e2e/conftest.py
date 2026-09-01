"""Browser E2E harness (Phase 15): a REAL ArmyEye server on an ephemeral port, isolated in
BOTH planes - a fixture-created PostgreSQL database (armeye_test_<hex>, alembic head) and a
fixture-created ARTIFACT_ROOT under the temp dir - plus an EMPTY legacy root so the node
never ingests development settings / models / media / thumbnails.

PostgreSQL comes from tests/pg_fixture.py (ARMYEYE_TEST_PG_ADMIN_URL, else a THROWAWAY
`postgres:16-alpine` container on 127.0.0.1:<random port> - a separate server, so the
development VMS-db is not even connected to).
Playwright (chromium) drives the browser. Unavailable runtime => readiness_required
(skip in dev runs, FAIL in ARMYEYE_READINESS_RUN=1).
"""
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (REPO, os.path.join(REPO, "InferenceNode"), os.path.join(REPO, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from conftest import readiness_required  # noqa: E402  (tests/conftest.py)

from e2e.harness import (E2E_ADMIN, E2E_ADMIN_PASSWORD, E2E_ADMIN_NEW_PASSWORD,  # noqa: E402
                         ConsoleLog, api, csrf_token)


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port


def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ PostgreSQL server
@pytest.fixture(scope="session")
def pg_server(isolated_pg):
    """The isolated_pg fixture (tests/pg_fixture.py) already provides a reachable server:
    ARMYEYE_TEST_PG_ADMIN_URL or a throwaway postgres:16-alpine container."""
    return {"url": os.environ.get("ARMYEYE_TEST_PG_ADMIN_URL")}


# ------------------------------------------------------------------ dev-data untouched proof
DEV_PATHS = [os.path.join(REPO, "InferenceNode", p) for p in
             ("data", "model_repository", "media", "pipelines", "node_settings.json")]


def _snapshot(paths):
    h = hashlib.sha256()
    for base in paths:
        if os.path.isfile(base):
            st = os.stat(base); h.update(f"{base}|{st.st_size}|{st.st_mtime_ns}\n".encode()); continue
        for dirpath, dirs, files in os.walk(base):
            dirs.sort()
            for f in sorted(files):
                p = os.path.join(dirpath, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                h.update(f"{os.path.relpath(p, REPO)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


# ------------------------------------------------------------------ the server
@pytest.fixture(scope="session")
def e2e_server(pg_server, isolated_pg, tmp_path_factory):
    """Boots the node as a subprocess. Yields dict(base_url, artifact_root, dbname, log)."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        readiness_required("playwright not installed - browser E2E unavailable")

    port = _free_port()
    legacy_root = tempfile.mkdtemp(prefix="armeye-e2e-legacy-")   # EMPTY: nothing to migrate
    key_file = os.path.join(tempfile.mkdtemp(prefix="armeye-e2e-key-"), "config.key")
    from InferenceNode import config_secrets as cs
    with open(key_file, "w") as f:
        f.write(cs.generate_key_line("armyeye-e2e-2026") + "\n")
    if os.name != "nt":
        os.chmod(key_file, 0o600)
    log_path = os.path.join(tempfile.mkdtemp(prefix="armeye-e2e-log-"), "node.log")

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ARMYEYE_DATABASE_URL", "ADMIN_", "FLASK_SECRET_KEY", "ENABLE_ENGINE_BUILDER"))}
    env.update({
        "ARMYEYE_DATABASE_URL": isolated_pg["url"],
        "ARMYEYE_ARTIFACT_ROOT": isolated_pg["artifact_root"],
        "ARMYEYE_LEGACY_ROOT": legacy_root,
        "ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE": key_file,
        "ADMIN_USERNAME": E2E_ADMIN, "ADMIN_PASSWORD": E2E_ADMIN_PASSWORD,
        "FLASK_SECRET_KEY": secrets.token_hex(32),
        "ENABLE_ENGINE_BUILDER": "1",
        "ARMYEYE_E2E_PORT": str(port),
        "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
    })
    dev_before = _snapshot(DEV_PATHS)
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "tests", "e2e", "run_node.py")],
                            cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 180
    while True:
        if proc.poll() is not None:
            log.flush()
            pytest.fail("E2E node exited during startup:\n" + open(log_path, encoding="utf-8", errors="replace").read()[-4000:])
        try:
            with urllib.request.urlopen(base_url + "/login", timeout=3) as r:
                if r.status == 200:
                    break
        except Exception:  # noqa: BLE001
            pass
        if time.time() > deadline:
            proc.kill()
            pytest.fail("E2E node did not become ready in 180s:\n" + open(log_path, encoding="utf-8", errors="replace").read()[-4000:])
        time.sleep(0.5)

    # ---- HARD GUARD (in the run log): the booted server uses the fixture DB + fixture root
    from sqlalchemy import create_engine, text
    with create_engine(isolated_pg["url"], future=True).connect() as c:
        current = c.execute(text("SELECT current_database()")).scalar_one()
    assert current == isolated_pg["dbname"], "HARD GUARD: E2E DB is not the fixture DB"
    assert os.path.realpath(isolated_pg["artifact_root"]).startswith(os.path.realpath(tempfile.gettempdir()))
    print(f"\n[E2E HARD GUARD] current_database()={current!r} artifact_root={isolated_pg['artifact_root']!r} "
          f"legacy_root={legacy_root!r} (empty) base_url={base_url}")

    yield {"base_url": base_url, "artifact_root": isolated_pg["artifact_root"], "dbname": isolated_pg["dbname"],
           "db_url": isolated_pg["url"], "log": log_path, "legacy_root": legacy_root, "dev_before": dev_before}

    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
    dev_after = _snapshot(DEV_PATHS)
    if dev_before != dev_after:
        raise AssertionError("E2E ISOLATION VIOLATION: development data changed during the E2E run")
    print(f"[E2E ISOLATION] dev data untouched (snapshot {dev_before[:12]} == {dev_after[:12]})")
    shutil.rmtree(legacy_root, ignore_errors=True)


# ------------------------------------------------------------------ browser
@pytest.fixture(scope="session")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        readiness_required("playwright not installed - browser E2E unavailable")
    pw = sync_playwright().start()
    b = None
    try:
        b = pw.chromium.launch(headless=True)
    except Exception:  # noqa: BLE001 - headless-shell build absent: use the full build in new headless mode
        try:
            b = pw.chromium.launch(headless=False, args=["--headless=new"])
        except Exception as e:  # noqa: BLE001
            pw.stop()
            readiness_required(f"chromium unavailable for playwright: {str(e)[:200]}")
    yield b
    b.close(); pw.stop()


@pytest.fixture(scope="session")
def admin_context(browser, e2e_server):
    """One logged-in ADMIN browser context for the session (first-login password change done)."""
    ctx = browser.new_context(base_url=e2e_server["base_url"])
    page = ctx.new_page()
    page.goto("/login")
    page.fill("input[name=username]", E2E_ADMIN)
    page.fill("input[name=password]", E2E_ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    if "change-password" in page.url or page.locator("input[name=new_password]").count():
        # forced first-login rotation
        if page.locator("input[name=current_password]").count():
            page.fill("input[name=current_password]", E2E_ADMIN_PASSWORD)
        page.fill("input[name=new_password]", E2E_ADMIN_NEW_PASSWORD)
        if page.locator("input[name=confirm_password]").count():
            page.fill("input[name=confirm_password]", E2E_ADMIN_NEW_PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
    assert "/login" not in page.url, f"admin login failed, at {page.url}"
    page.close()
    yield ctx
    ctx.close()


@pytest.fixture()
def admin_page(admin_context):
    page = admin_context.new_page()
    log = ConsoleLog().attach(page)
    page.console_log = log
    yield page
    page.close()


