"""Step 10 - let several worker processes share one database without fighting.

One ArmyEye process tops out at ~220-250 inferences/second. At that ceiling the GPU sits
near 25% and the CPU near 5 of 20 cores, so the limit is the GIL, not the hardware. 60
cameras at 5 fps needs 300 inferences/second and is therefore impossible in a single
process; three processes of 20 cameras deliver it comfortably (measured on real RTSP:
60/60 cameras at 5.00 fps, 9.99/20 cores, 50% GPU, 3.4 GB of ArmyEye VRAM).

Nothing in the schema said which node runs a pipeline, so two instances sharing this
database would BOTH start the same camera - double-decoding the stream and publishing
every detection twice.

`pipelines.node_id`:
    NULL     -> unassigned, any node may run it (the original single-node behaviour, which
                is why this is purely additive and needs no back-fill)
    '<id>'   -> only the node whose ARMYEYE_NODE_ID matches may run it
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db                       # noqa: E402
from InferenceNode.auth import service as svc                      # noqa: E402
from InferenceNode.auth.models import Base                         # noqa: E402
import InferenceNode.data_models  # noqa: E402,F401
from InferenceNode import pipeline_store as ps                     # noqa: E402


class _Seed:
    id = None; username = "seed"; role = "admin"; is_authenticated = True


@pytest.fixture()
def env(tmp_path):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'n.db'}")
    Base.metadata.create_all(engine)
    admin = svc.create_user(_Seed, username="root", password="rootpass1", role="admin",
                            must_change_password=False)
    viewer = svc.create_user(admin, username="viewer", password="viewpass1", role="user",
                             must_change_password=False)
    yield {"admin": admin, "viewer": viewer}
    auth_db._engine = None; auth_db._SessionLocal = None


def _mk(admin, pid):
    ps.create_pipeline(admin, pipeline_id=pid, name=pid,
                       config={"name": pid, "model": {"id": None}, "destinations": [],
                               "frame_source": {"capture_type": "ipcam", "config": {}}})


# ------------------------------------------------------------------ the default is unchanged
def test_a_new_pipeline_is_unassigned(env):
    """Additive by design: existing installs keep running every pipeline on one node."""
    _mk(env["admin"], "p1")
    assert ps.get_pipeline_for_user("p1", env["admin"])["node_id"] is None


def test_an_unassigned_pipeline_runs_on_any_node():
    for node in ("node-a", "node-b", None):
        assert ps.runnable_on_node({"node_id": None}, node) is True


# ------------------------------------------------------------------ assignment
def test_assigning_pins_the_pipeline_to_one_node(env):
    _mk(env["admin"], "p1")
    r = ps.assign_pipeline_to_node(env["admin"], "p1", "node-a")
    assert r == {"pipeline_id": "p1", "node_id": "node-a", "previous_node_id": None}
    assert ps.get_pipeline_for_user("p1", env["admin"])["node_id"] == "node-a"


def test_only_the_owning_node_may_run_it():
    rec = {"node_id": "node-a"}
    assert ps.runnable_on_node(rec, "node-a") is True
    assert ps.runnable_on_node(rec, "node-b") is False, "this is what stops a double-start"
    assert ps.runnable_on_node(rec, None) is False


def test_reassignment_reports_the_previous_owner(env):
    """Moving a camera between workers must be auditable."""
    _mk(env["admin"], "p1")
    ps.assign_pipeline_to_node(env["admin"], "p1", "node-a")
    r = ps.assign_pipeline_to_node(env["admin"], "p1", "node-b")
    assert r["previous_node_id"] == "node-a" and r["node_id"] == "node-b"


def test_unassigning_releases_it_back_to_any_node(env):
    _mk(env["admin"], "p1")
    ps.assign_pipeline_to_node(env["admin"], "p1", "node-a")
    r = ps.assign_pipeline_to_node(env["admin"], "p1", None)
    assert r["node_id"] is None
    assert ps.runnable_on_node(ps.get_pipeline_for_user("p1", env["admin"]), "node-b") is True


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_assignment_means_unassigned_not_a_node_called_empty(env, blank):
    _mk(env["admin"], "p1")
    ps.assign_pipeline_to_node(env["admin"], "p1", "node-a")
    assert ps.assign_pipeline_to_node(env["admin"], "p1", blank)["node_id"] is None


def test_assigning_an_unknown_pipeline_raises(env):
    with pytest.raises(KeyError):
        ps.assign_pipeline_to_node(env["admin"], "no-such-pipeline", "node-a")


# ------------------------------------------------------------------ authorization
def test_assignment_is_admin_only(env):
    """Where work runs is an operational control, not a per-pipeline permission.

    AccessDenied deliberately says "not found or access denied" - a non-admin must not be
    able to probe which pipelines exist.
    """
    _mk(env["admin"], "p1")
    with pytest.raises(ps.AccessDenied):
        ps.assign_pipeline_to_node(env["viewer"], "p1", "node-a")
    assert ps.get_pipeline_for_user("p1", env["admin"])["node_id"] is None, "and nothing changed"


def test_assignment_is_audited(env):
    _mk(env["admin"], "p1")
    ps.assign_pipeline_to_node(env["admin"], "p1", "node-a")
    from InferenceNode.auth.db import get_session
    from InferenceNode.auth.models import AuditLog
    from sqlalchemy import select
    with get_session() as s:
        actions = [a.action for a in s.execute(select(AuditLog)).scalars()]
    assert "pipeline_node_assigned" in actions


# ------------------------------------------------------------------ wiring
def test_the_manager_refuses_to_start_another_nodes_pipeline():
    src = open(os.path.join(REPO, "InferenceNode", "pipeline_manager.py"), encoding="utf-8").read()
    i = src.index("def start_pipeline")
    body = src[i:i + 2500]
    assert "self._owns_pipeline(pipeline_id)" in body
    assert "Refusing to start pipeline" in body


def test_ownership_failure_does_not_block_startup():
    """An assignment lookup problem must never make a node unable to run anything."""
    src = open(os.path.join(REPO, "InferenceNode", "pipeline_manager.py"), encoding="utf-8").read()
    i = src.index("def _owns_pipeline")
    body = src[i:i + 700]
    assert "return True" in body, "a failed check must fail OPEN, not strand every pipeline"


def test_node_id_is_stable_across_restarts_when_configured():
    """A uuid4 per boot made assignment meaningless - nothing could be pinned to 'this node'
    and survive a restart."""
    src = open(os.path.join(REPO, "InferenceNode", "inference_node.py"), encoding="utf-8").read()
    assert 'os.environ.get("ARMYEYE_NODE_ID"' in src
    assert "self.node_id = node_id or str(uuid.uuid4())" not in src


def test_the_assignment_route_is_admin_and_csrf_guarded():
    src = open(os.path.join(REPO, "InferenceNode", "inference_node.py"), encoding="utf-8").read()
    i = src.index("@self.app.route('/api/pipeline/<pipeline_id>/node', methods=['PUT'])")
    body = src[i:i + 1800]
    assert "@self._admin_csrf" in body
    assert "404" in body and "400" in body
    # AccessDenied is NOT a PermissionError; catching the wrong type would have returned
    # 500 instead of the 404 the other pipeline routes use.
    assert "ps.AccessDenied" in body, "a non-admin must get the same 404 as a missing pipeline"
    assert "_audit_event('pipeline_node_assigned'" in body


def test_the_migration_is_additive_and_reversible():
    p = os.path.join(REPO, "InferenceNode", "migrations", "versions",
                     "0006_pipeline_node_assignment.py")
    src = open(p, encoding="utf-8").read()
    assert 'down_revision = "0005_pipeline_model_integrity"' in src
    assert "nullable=True" in src, "must not break existing rows"
    assert "def downgrade" in src and "drop_column" in src
    assert "ForeignKey" not in src, "nodes are processes, not rows - no FK"
