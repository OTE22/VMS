"""Frontend guards that must not silently rot.

1. Runs the Node test suite for static/js/armyeye-ui.js (esc/apiCall/fmtUtc) so
   it participates in the normal `pytest tests/` run. Skipped when node is absent.
2. Pins the create-engine wizard's JS name/key derivation against the real Python
   generator - the key it previews is the key the engine is permanently
   registered under, so a drift here would lie to the admin.
"""
import os
import re
import sys
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.engine_builder import (                # noqa: E402
    class_name_from_display, key_from_class, filename_for_key, SKIP_NAMES,
)

WIZARD = os.path.join(REPO, "InferenceNode", "templates", "create_engine.html")
UI_JS = os.path.join(REPO, "InferenceNode", "static", "js", "armyeye-ui.js")


# --------------------------------------------------------------------------- #
# node test suite
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("suite", ["armyeye_ui.test.mjs", "admin_users_render.test.mjs", "pipeline_builder_roundtrip.test.mjs", "frontend_api_contracts.test.mjs", "form_payload_audit.test.mjs"])
def test_node_suite(suite):
    proc = subprocess.run(
        ["node", "--test", os.path.join(REPO, "tests", suite)],
        capture_output=True, text=True, cwd=REPO, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --------------------------------------------------------------------------- #
# wizard key preview must match the generator exactly
# --------------------------------------------------------------------------- #
def _js_derivation():
    """Extract toClass/toKey from the wizard template so the test exercises the
    shipped code, not a copy of it."""
    html = open(WIZARD, encoding="utf-8").read()
    m = re.search(r"function toClass\(display\).*?\n}\n", html, re.S)
    n = re.search(r"function toKey\(cls\).*?\n}\n", html, re.S)
    assert m and n, "toClass/toKey not found in create_engine.html"
    return m.group(0) + n.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("display", [
    "Thermal Detector",
    "PG Verify Cam",          # acronym mangling: -> p_g_verify_cam
    "my custom engine",
    "  spaced   out  ",
    "3D Scanner",             # leading digit -> E-prefixed class
    "ABC",
    "Person Re-ID Engine",
    "night_vision v2",
    "Base",                   # produces the reserved base_engine.py
    "façade détecteur 9000",  # non-ASCII is dropped by both implementations
])
def test_js_key_preview_matches_python_generator(display):
    # Build the JS string literal via JSON so quoting and unicode are exact.
    import json
    script = _js_derivation() + (
        f"const d = {json.dumps(display)};\n"
        "const cls = toClass(d);\n"
        'console.log(JSON.stringify({cls, key: cls ? toKey(cls) : ""}));\n'
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip())

    expected_cls = class_name_from_display(display)
    assert got["cls"] == expected_cls, f"class drift for {display!r}"
    assert got["key"] == key_from_class(expected_cls), f"key drift for {display!r}"


def test_reserved_filenames_listed_in_wizard_match_the_backend():
    """The wizard pre-blocks names that would generate a reserved filename. If
    engine_builder.SKIP_NAMES grows, the wizard must grow with it."""
    html = open(WIZARD, encoding="utf-8").read()
    m = re.search(r"const RESERVED_FILENAMES = \[(.*?)\];", html, re.S)
    assert m, "RESERVED_FILENAMES not found in create_engine.html"
    listed = set(re.findall(r"'([^']+)'", m.group(1)))
    backend_files = {n for n in SKIP_NAMES if n.endswith(".py")}
    assert backend_files <= listed, f"wizard is missing {backend_files - listed}"
    # And the one an admin can actually trigger by typing a display name:
    assert filename_for_key(key_from_class(class_name_from_display("Base"))) in listed


# --------------------------------------------------------------------------- #
# the escaper must be present and used
# --------------------------------------------------------------------------- #
def test_admin_pages_do_not_use_native_confirm_or_prompt():
    """Native dialogs are unstyled, unlabelled and block the event loop - the
    redesign replaced every one with confirmDialog()."""
    for name in ("admin_users.html", "create_engine.html"):
        html = open(os.path.join(REPO, "InferenceNode", "templates", name), encoding="utf-8").read()
        assert not re.search(r"(?<![.\w])confirm\s*\(", html.replace("confirmDialog(", "")), name
        assert not re.search(r"(?<![.\w])prompt\s*\(", html), name


ADMIN_USERS_JS = os.path.join(REPO, "InferenceNode", "static", "js", "admin-users.js")

# Helpers that must live ONLY in the shared layer. A page-local copy is what drifts.
SHARED_HELPERS = ("esc", "apiCall", "confirmDialog", "setButtonLoading",
                  "attachPasswordToggle", "suggestPassword", "skeletonRows",
                  "stateRow", "fmtUtc", "isoTitle")


def test_esc_is_defined_once_and_shared():
    """Every dynamic string in the admin surfaces routes through one shared helper."""
    assert "function esc(value)" in open(UI_JS, encoding="utf-8").read()
    # create_engine still carries its script inline; users management is externalised.
    inline = open(os.path.join(REPO, "InferenceNode", "templates", "create_engine.html"),
                  encoding="utf-8").read()
    assert "function esc(" not in inline, "create_engine.html redefines esc()"
    assert "esc(" in inline, "create_engine.html never escapes anything"

    page = open(ADMIN_USERS_JS, encoding="utf-8").read()
    assert "esc(" in page, "admin-users.js never escapes anything"


def test_admin_users_does_not_duplicate_shared_helpers():
    """admin-users.js must CONSUME the shared layer, never re-implement part of it."""
    page = open(ADMIN_USERS_JS, encoding="utf-8").read()
    duplicated = [h for h in SHARED_HELPERS
                  if re.search(rf"(?:^|\n)\s*(?:async\s+)?function\s+{h}\s*\(", page)]
    assert not duplicated, f"admin-users.js redefines shared helpers: {duplicated}"


def test_users_template_has_no_inline_script_or_handlers():
    """Page behaviour is external, so the page needs no 'unsafe-inline' under a future CSP."""
    html = open(os.path.join(REPO, "InferenceNode", "templates", "admin_users.html"),
                encoding="utf-8").read()
    assert "js/admin-users.js" in html, "template no longer loads its external script"
    assert "<script>" not in html, "an inline <script> block came back into admin_users.html"
    for handler in ("onclick=", "onsubmit=", "onchange=", "onkeyup=", "oninput="):
        assert handler not in html, f"inline {handler} handler reintroduced"


def test_base_template_loads_the_shared_layer():
    base = open(os.path.join(REPO, "InferenceNode", "templates", "base.html"), encoding="utf-8").read()
    assert "js/armyeye-ui.js" in base
    assert "css/armyeye-admin.css" in base
    # app.js must come first: armyeye-ui.js calls fetchJSON/showAlert from it.
    assert base.index("js/app.js") < base.index("js/armyeye-ui.js")
