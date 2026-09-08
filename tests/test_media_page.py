"""Media management page and its listing API.

Media had no management surface at all: it appeared only in the Pipeline Builder as a
source picker, so the delete API added alongside this had no way to be used from the UI.

Two properties matter here beyond "the page renders":
  * media is referenced by a STRING in pipeline config with no foreign key, so the page
    must show references BEFORE the user clicks delete - otherwise the 409 is a surprise
  * the page must not re-implement esc()/apiCall()/fetchJSON(); those live in the shared
    layer and duplicating them is how escaping regressions get introduced
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

TPL = os.path.join(REPO, "InferenceNode", "templates")
NODE = os.path.join(REPO, "InferenceNode", "inference_node.py")


def _page():
    return open(os.path.join(TPL, "media.html"), encoding="utf-8").read()


def _node():
    return open(NODE, encoding="utf-8").read()


# ------------------------------------------------------------------ routes and guards
def test_media_page_route_is_admin_only():
    src = _node()
    i = src.index("@self.app.route('/media')")
    assert "@self._admin_required" in src[i:i + 200]
    assert "render_template('media.html'" in src[i:i + 300]


def test_listing_api_is_admin_only():
    """The listing exposes which pipelines use which asset - that is not public."""
    src = _node()
    i = src.index("@self.app.route('/api/media', methods=['GET'])")
    assert "@self._admin_required" in src[i:i + 200]


def test_listing_returns_relative_paths_and_no_host_paths():
    src = _node()
    i = src.index("def list_media_assets")
    body = src[i:i + 1600]
    assert '"relative_path"' in body
    for leak in ("/app/", "/home/", "artifact_root", "resolve("):
        assert leak not in body, f"listing must not expose {leak}"


def test_listing_includes_references_so_the_409_is_predictable():
    src = _node()
    i = src.index("def list_media_assets")
    body = src[i:i + 1600]
    assert "referencing_pipelines" in body
    assert '"references"' in body


def test_nav_links_to_the_page():
    base = open(os.path.join(TPL, "base.html"), encoding="utf-8").read()
    assert 'href="/media"' in base


# ------------------------------------------------------------------ the page itself
def test_page_shows_references_before_deleting():
    """The user must see 'used by X' without having to attempt the delete first."""
    s = _page()
    assert "refsCell" in s and "references" in s
    assert "Deletion will be <strong>refused</strong>" in s, "the warning must be explicit"
    assert "confirmDeleteMedia').disabled = !!inUse" in s, \
        "the confirm button must be disabled while the file is in use"


def test_page_handles_409_from_the_server_not_just_its_cache():
    """The cached view can be stale - a pipeline may start using the file between the page
    load and the click - so the server's 409 must be handled explicitly."""
    s = _page()
    assert "r.status === 409" in s
    assert "body.pipelines" in s, "the 409 response names the pipelines; show them"


def test_page_distinguishes_lifecycle_from_validation_status():
    s = _page()
    assert "mediaStatusBadge" in s
    assert "m.validation_status" in s and "m.status" in s
    assert "AVAILABLE" in s and "PASSED" in s


def test_page_escapes_every_dynamic_value():
    """Filenames and pipeline names are user-controlled."""
    s = _page()
    for field in ("m.relative_path", "m.original_filename", "m.media_id", "m.status"):
        assert f"esc({field})" in s, f"{field} is rendered unescaped"
    # no raw interpolation of a server value into HTML
    assert "${m.relative_path}" not in s.replace("${esc(m.relative_path)}", "")


def test_page_reuses_the_shared_helpers():
    s = _page()
    for dup in ("function esc(", "function apiCall(", "function fetchJSON(", "function showAlert("):
        assert dup not in s, f"media.html must not redefine {dup!r} - it is in the shared layer"
    for used in ("esc(", "apiCall(", "fetchJSON(", "showAlert("):
        assert used in s


def test_delete_uses_the_api_and_encodes_the_id():
    s = _page()
    assert re.search(r"apiCall\(`/api/media/\$\{encodeURIComponent\(pendingDelete\)\}`", s), \
        "the media id must be URL-encoded into the path"
    assert "method: 'DELETE'" in s


def test_page_refreshes_after_a_delete_attempt():
    """Whether it succeeded or was refused, the list must reflect reality afterwards."""
    s = _page()
    i = s.index("finally {")
    assert "loadMedia()" in s[i:i + 400]
