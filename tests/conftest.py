"""Session-wide safety guardrails for the ArmyEye test suite.

Two invariants every test run must hold, whether it runs on the host or inside the
VMS container:

1. **The development/production PostgreSQL is never touched.** ArmyEye resolves its
   database from ARMYEYE_DATABASE_URL (auth/db.py::_resolve_url). Inside the container
   that variable points at the live `armeye` DB, and `setup_auth()` calls a bare
   `init_engine()` which would happily connect to it. We delete the variable for the
   whole session and reset the cached engine, so a fixture that forgets to supply an
   explicit URL fails loudly (RuntimeError from get_session) instead of writing into
   dev data.

2. **PostgreSQL-backed persistence tests use ONLY a fixture-created database plus a
   fixture-created ARTIFACT_ROOT.** See `pg_fixture.py`; the hard guard there checks
   `SELECT current_database()` and the resolved artifact root before any destructive
   test may proceed.

Readiness mode (`ARMYEYE_READINESS_RUN=1`) turns "runtime unavailable -> skip" into
"-> FAIL" for the proofs the production-readiness verdict depends on, so a required
proof can never be converted into a PASS by skipping.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "InferenceNode")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

READINESS = os.environ.get("ARMYEYE_READINESS_RUN", "").strip().lower() in ("1", "true", "yes", "on")

# Make the isolated-PostgreSQL fixtures available to every test module without package imports.
pytest_plugins = ["pg_fixture"]


@pytest.fixture(scope="session", autouse=True)
def _never_touch_dev_database():
    """Strip the live DB URL from the environment for the entire session and make sure no
    module-level engine leaks in from an earlier import."""
    saved = {k: os.environ.pop(k) for k in ("ARMYEYE_DATABASE_URL", "ARMYEYE_DATABASE_URL_FILE")
             if k in os.environ}
    try:
        from InferenceNode.auth import db as auth_db
        auth_db._engine = None
        auth_db._SessionLocal = None
    except Exception:  # pragma: no cover - import problems surface in the tests themselves
        pass
    yield
    # Do NOT restore: nothing after the session should silently reacquire the live URL.
    _ = saved


def readiness_required(reason: str):
    """Use instead of a bare skip for proofs the readiness verdict depends on.

    Ordinary run  -> pytest.skip(reason)
    Readiness run -> pytest.fail(reason)   (a required proof is NOT VERIFIED -> cannot PASS)
    """
    if READINESS:
        pytest.fail(f"READINESS: required proof not verified - {reason}")
    pytest.skip(reason)


def pytest_report_header(config):
    return f"ArmyEye readiness mode: {'ON (required proofs FAIL when unavailable)' if READINESS else 'off'}"


@pytest.fixture(autouse=True)
def _isolated_default_config_key(tmp_path):
    """Pipeline writes now require a key, just like publisher writes.

    Explicit key-loss/rotation fixtures remain free to replace or unload this key.
    Never read a deployment key as a fallback for tests.
    """
    from InferenceNode import config_secrets as cs
    if not cs.keys_available():
        key = tmp_path / 'default-test-config.key'
        key.write_text(cs.generate_key_line('isolated-test'))
        key.chmod(0o600)
        cs.reload_keys(str(key))
