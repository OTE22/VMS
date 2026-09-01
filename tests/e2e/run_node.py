"""E2E node launcher: boots a REAL ArmyEye server (InferenceNode) on an ephemeral port
against the isolated PostgreSQL + isolated ARTIFACT_ROOT + empty LEGACY_ROOT the fixture
provides through the environment. Never loads the repo .env (a dev DB URL must not leak in).

Required env (set by tests/e2e/conftest.py):
  ARMYEYE_DATABASE_URL, ARMYEYE_ARTIFACT_ROOT, ARMYEYE_LEGACY_ROOT,
  ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE, ADMIN_USERNAME, ADMIN_PASSWORD, FLASK_SECRET_KEY,
  ARMYEYE_E2E_PORT
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)


def create_test_node(port: int):
    """Factory seam: the same InferenceNode production code path, pointed at isolated
    resources by environment (fails loudly if any isolation variable is missing)."""
    for var in ("ARMYEYE_DATABASE_URL", "ARMYEYE_ARTIFACT_ROOT", "ARMYEYE_LEGACY_ROOT",
                "ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE"):
        if not os.environ.get(var):
            raise SystemExit(f"E2E isolation variable {var} is not set - refusing to start")
    if "armeye_test_" not in os.environ["ARMYEYE_DATABASE_URL"]:
        raise SystemExit("E2E refuses to start against a database that is not an armeye_test_* fixture DB")
    from InferenceNode.inference_node import InferenceNode
    return InferenceNode(node_name="e2e-node", port=port, legacy_root=os.environ["ARMYEYE_LEGACY_ROOT"])


if __name__ == "__main__":
    port = int(os.environ["ARMYEYE_E2E_PORT"])
    node = create_test_node(port)
    # No LAN discovery broadcast, no MQTT telemetry from a test node; production WSGI server.
    node.start(enable_discovery=False, enable_telemetry=False, production=True)
