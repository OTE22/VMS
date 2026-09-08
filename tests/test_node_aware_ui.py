"""The UI must not offer to start another worker's camera.

Pipeline listing is scoped by USER PERMISSIONS, never by node, so every worker's UI shows
EVERY pipeline. With several workers sharing one database that is the realistic path to a
double-start: an admin on worker-2 sees worker-1's cameras with an enabled Start button,
clicks one, and the same camera is now decoded and published twice.

The server already refuses (PipelineManager._owns_pipeline), so this is about not inviting
the mistake - and about making ownership visible, since `pipelines.status` says "running"
without saying WHERE.

Failing open is deliberate throughout: if node identity cannot be determined the UI must
permit, never block. A single-node install has no node_id at all and must be unaffected.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(REPO, "InferenceNode", "templates", "pipeline_management.html")


def _page():
    return open(TPL, encoding="utf-8").read()


def _manager():
    return open(os.path.join(REPO, "InferenceNode", "pipeline_manager.py"), encoding="utf-8").read()


# ------------------------------------------------------------------ the data must arrive
def test_the_listing_carries_node_ownership():
    """Without this the page cannot tell whose camera it is looking at."""
    src = _manager()
    i = src.index("def list_pipelines")
    body = src[i:i + 2500]
    assert "pipeline_copy['node_id'] = record.get('node_id')" in body


def test_the_page_learns_which_worker_it_is_talking_to():
    s = _page()
    assert "/api/node/identity" in s
    assert "loadNodeIdentity" in s
    # identity must be resolved BEFORE the list renders, or the first paint is wrong
    i = s.index("const response = await fetch('/api/pipelines');")
    assert "loadNodeIdentity()" in s[max(0, i - 400):i]


# ------------------------------------------------------------------ ownership logic
def test_unassigned_pipelines_are_startable_anywhere():
    """node_id null/empty = unassigned = the single-node default, which must not change."""
    s = _page()
    i = s.index("function foreignOwner(")
    body = s[i:i + 400]
    assert "if (!owner) return null;" in body


def test_unknown_identity_permits_rather_than_blocks():
    """If /api/node/identity fails, the UI must not strand every pipeline."""
    s = _page()
    i = s.index("function foreignOwner(")
    body = s[i:i + 400]
    assert "MY_NODE_ID === null || MY_NODE_ID === undefined" in body and "return null" in body
    j = s.index("async function loadNodeIdentity(")
    assert "MY_NODE_ID = null" in s[j:j + 500], "a failed fetch must fall back to permitting"


def test_our_own_pipelines_are_not_treated_as_foreign():
    s = _page()
    i = s.index("function foreignOwner(")
    assert "owner === MY_NODE_ID ? null : owner" in s[i:i + 400]


# ------------------------------------------------------------------ the guard itself
def test_start_is_disabled_for_another_workers_pipeline():
    s = _page()
    assert "pipeline.status !== 'running' && foreignOwner(pipeline)) ? 'disabled'" in s, \
        "the Start button must be disabled when another worker owns the pipeline"


def test_stop_is_not_disabled_by_ownership():
    """Only Start is gated. A pipeline showing as running here is ours to stop, and
    disabling Stop could strand it."""
    s = _page()
    i = s.index("pipeline.status !== 'running' && foreignOwner(pipeline)) ? 'disabled'")
    assert "pipeline.status !== 'running'" in s[i:i + 80]


def test_clicking_start_anyway_explains_where_to_go():
    """Belt and braces: the button is disabled, but the handler must still refuse and say
    which worker owns it rather than failing silently."""
    s = _page()
    i = s.index("async function startPipeline(pipelineId) {")
    body = s[i:i + 600]
    assert "foreignOwner(" in body
    assert "runs on worker" in body and "return;" in body


def test_the_owner_is_visible_not_just_blocked():
    """`pipelines.status` says running without saying where; the badge is what makes
    ownership legible at a glance."""
    s = _page()
    assert "function ownerBadge(" in s
    assert s.count("${ownerBadge(pipeline)}") >= 2, "both the list and card views need it"
    i = s.index("function ownerBadge(")
    assert "fa-server" in s[i:i + 400]


def test_the_badge_is_absent_for_our_own_and_unassigned_pipelines():
    s = _page()
    i = s.index("function ownerBadge(")
    body = s[i:i + 400]
    assert "if (!owner) return '';" in body
