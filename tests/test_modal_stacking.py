"""Guard against the stacking-context bug that made every ArmyEye modal unusable.

History: `.container-fluid { position: relative; z-index: 1 }` created a stacking context
on <main class="container-fluid">, which contains every modal. Bootstrap appends
.modal-backdrop (z-index 1050) to <body>, OUTSIDE that context, so the whole subtree -
including a z-index 1055 modal - painted beneath the backdrop. Dialogs were visible
through the translucent backdrop but every click, keystroke and close landed on the
backdrop instead. There was no JavaScript error, which made it look like a JS bug.

A child cannot escape its parent's stacking context, so no z-index on .modal can repair
this. The only fixes are: don't create the context, or move modals out of it.

These tests parse the CSS/templates rather than a live browser, so they run anywhere.
"""
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

CSS_DIR = os.path.join(REPO, "InferenceNode", "static", "css")
TPL_DIR = os.path.join(REPO, "InferenceNode", "templates")

# Selectors that are (or contain) an ancestor of every modal, because modals live inside
# {% block content %} -> <main class="container-fluid"> -> <body>.
MODAL_ANCESTOR_SELECTORS = {"body", "main", ".container-fluid", ".container", "main.container-fluid"}

# Properties that create a stacking context AND a containing block for position:fixed
# descendants (transform/filter/perspective/will-change/contain), or just a stacking
# context (position + non-auto z-index, isolation).
CONTEXT_PROPS = ("transform", "filter", "backdrop-filter", "perspective",
                 "will-change", "contain", "isolation")


def _css_files():
    return [os.path.join(CSS_DIR, f) for f in os.listdir(CSS_DIR) if f.endswith(".css")]


def _rules(css_text):
    """Yield (selector, body) for top-level rules, skipping @media/@keyframes blocks."""
    # Strip comments so commented-out examples never trip the guard.
    css_text = re.sub(r"/\*.*?\*/", "", css_text, flags=re.S)
    for m in re.finditer(r"([^{}@]+)\{([^{}]*)\}", css_text):
        yield m.group(1).strip(), m.group(2)


def _selector_parts(selector):
    return {s.strip() for s in selector.split(",") if s.strip()}


@pytest.mark.parametrize("css_path", _css_files(), ids=os.path.basename)
def test_no_stacking_context_on_modal_ancestors(css_path):
    """The regression itself: a modal ancestor must not form a stacking context."""
    offenders = []
    for selector, body in _rules(open(css_path, encoding="utf-8").read()):
        parts = _selector_parts(selector)
        if not (parts & MODAL_ANCESTOR_SELECTORS):
            continue

        decls = dict(re.findall(r"([a-z-]+)\s*:\s*([^;]+)", body, re.I))
        norm = {k.strip().lower(): v.strip().lower() for k, v in decls.items()}

        z = norm.get("z-index")
        position = norm.get("position", "static")
        if z and z not in ("auto", "initial") and position != "static":
            offenders.append(f"{selector}: position:{position} + z-index:{z}")

        for prop in CONTEXT_PROPS:
            val = norm.get(prop)
            if val and val not in ("none", "initial", "auto"):
                offenders.append(f"{selector}: {prop}:{val}")

    assert not offenders, (
        "A modal ancestor forms a stacking context; every Bootstrap modal in ArmyEye "
        "would render beneath the z-index:1050 backdrop and become unclickable:\n  "
        + "\n  ".join(offenders))


def test_container_fluid_keeps_position_but_not_zindex():
    """Pin the exact shape of the fix so it is not 'tidied' back into a regression."""
    css = open(os.path.join(CSS_DIR, "style.css"), encoding="utf-8").read()
    css_nc = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    body = None
    for selector, decls in _rules(css_nc):
        if ".container-fluid" in _selector_parts(selector):
            body = decls
            break
    assert body is not None, ".container-fluid rule disappeared from style.css"
    assert re.search(r"position\s*:\s*relative", body), \
        "position:relative was removed - the tactical body::before gradient would cover content"
    assert not re.search(r"z-index", body), \
        "z-index reintroduced on .container-fluid - this is the exact modal-killing bug"


def test_modals_are_not_given_a_zindex_workaround():
    """A z-index on .modal cannot escape an ancestor stacking context. If one appears it
    means somebody treated the symptom instead of the cause."""
    for path in _css_files():
        for selector, body in _rules(open(path, encoding="utf-8").read()):
            parts = _selector_parts(selector)
            if parts & {".modal", ".modal-backdrop"} and re.search(r"z-index", body):
                pytest.fail(
                    f"{os.path.basename(path)} sets z-index on {selector}. That cannot fix a "
                    "parent stacking context; remove the ancestor context instead.")


# --------------------------------------------------------------------------- #
# Row action (kebab) menus
#
# Separate defect from the modal one. A .dropdown-menu is absolutely positioned inside a
# <td>; `.card{overflow:hidden}` and `.table{overflow:hidden}` clipped it, and
# `.card:hover{transform}` turned the card into a containing block + stacking context at
# z-index auto, trapping the menu's z-index:1000 so the filter panel's
# `.card-body{z-index:1}` painted over the entire card. `.ae-menu-safe` opts a card out of
# all three.
# --------------------------------------------------------------------------- #
MENU_SAFE = ".ae-menu-safe"


def _admin_css():
    return open(os.path.join(CSS_DIR, "armyeye-admin.css"), encoding="utf-8").read()


def _decls_for(css_text, wanted_selector):
    """All declaration blocks whose selector list contains `wanted_selector`."""
    out = []
    for selector, body in _rules(css_text):
        if wanted_selector in _selector_parts(selector):
            out.append(body)
    return out


def test_menu_safe_lifts_overflow_on_the_whole_chain():
    """Every clipping ancestor between the card and the menu must be opened up."""
    css = _admin_css()
    needed = {".ae-menu-safe", ".ae-menu-safe .table-responsive", ".ae-menu-safe .table"}
    opened = set()
    for selector, body in _rules(css):
        if re.search(r"overflow\s*:\s*visible", body):
            opened |= (_selector_parts(selector) & needed)
    missing = needed - opened
    assert not missing, (
        "row action menus would still be clipped by: " + ", ".join(sorted(missing)))


def test_menu_safe_disables_the_hover_transform():
    """The hover lift makes .card a containing block + stacking context, which traps the
    menu behind sibling content. It must be neutralised for menu-hosting cards."""
    css = _admin_css()
    bodies = [b for sel, b in _rules(css)
              if any(p.startswith(".ae-menu-safe") and ":hover" in p for p in _selector_parts(sel))]
    assert bodies, ".ae-menu-safe:hover rule is missing"
    assert any(re.search(r"transform\s*:\s*none", b) for b in bodies), \
        ".ae-menu-safe:hover must set transform:none, or the menu is trapped again"


def test_menu_safe_restores_the_corner_radii():
    """overflow:hidden was the only thing rounding the card header and table corners."""
    css = _admin_css()
    assert re.search(r"\.ae-menu-safe\s*>\s*\.card-header[^{]*\{[^}]*border-top-left-radius", css), \
        "card header corners were not restored after removing overflow:hidden"
    assert re.search(r"\.ae-menu-safe\s+\.table\s+tbody[^{]*\{[^}]*border-bottom-\w+-radius", css), \
        "table bottom corners were not restored after removing overflow:hidden"


def test_dropdown_menu_declares_its_layer():
    css = open(os.path.join(CSS_DIR, "style.css"), encoding="utf-8").read()
    bodies = _decls_for(css, ".dropdown-menu")
    assert bodies, ".dropdown-menu rule disappeared"
    assert any("z-index" in b for b in bodies), \
        ".dropdown-menu should state its stacking layer explicitly"


def test_no_single_axis_overflow_on_menu_containers():
    """overflow-x:auto forces overflow-y:auto per spec, silently re-clipping the menu
    vertically. This is how the earlier responsive override reintroduced the bug."""
    css = _admin_css()
    for selector, body in _rules(css):
        if "ae-allow-overflow" in selector or "ae-menu-safe" in selector:
            for axis in ("overflow-x", "overflow-y"):
                m = re.search(axis + r"\s*:\s*(\w+)", body)
                if m and m.group(1) in ("auto", "scroll", "hidden"):
                    pytest.fail(
                        f"{selector} sets {axis}:{m.group(1)}; a single-axis overflow "
                        "computes the other axis to auto and clips the menu again.")


def test_users_table_card_opts_in():
    html = open(os.path.join(TPL_DIR, "admin_users.html"), encoding="utf-8").read()
    assert re.search(r'class="card[^"]*\bae-menu-safe\b', html), \
        "the users table card no longer opts into ae-menu-safe"


def test_every_template_modal_is_still_discoverable():
    """Inventory the modals this guard is protecting, so a new template with modals is
    covered by the ancestor rule above rather than silently forgotten."""
    found = {}
    for name in os.listdir(TPL_DIR):
        if not name.endswith(".html"):
            continue
        html = open(os.path.join(TPL_DIR, name), encoding="utf-8").read()
        n = len(re.findall(r'class="[^"]*\bmodal\b[^"]*fade', html))
        if n:
            found[name] = n
    # These are the templates known to contain modals; the count is informational, but a
    # template dropping to zero unexpectedly is worth noticing.
    assert "admin_users.html" in found, "admin_users.html lost its modals"
    assert sum(found.values()) >= 8, f"expected the app's modals to still exist, got {found}"
