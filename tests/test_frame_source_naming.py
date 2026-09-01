"""Guard the UI <-> frame-source-library capture-type naming.

ArmyEye's UI and every STORED pipeline config use 'ip_camera' and 'image_folder'; the
frame-source library uses 'ipcam' and 'folder'. When /api/frame-sources was rebuilt for
framesource 0.3.x it started advertising the LIBRARY names, so the Pipeline Builder looked
up 'ip_camera', found nothing, and told the user "ip_camera is not available" for a source
type that works perfectly.

The mapping now lives in exactly one place (pipeline_manager.UI_TO_LIBRARY_CAPTURE_TYPE)
and is consumed by the runtime, the frame-source listing and the discover endpoint. These
tests keep those in agreement.
"""
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.pipeline_manager import (            # noqa: E402
    UI_TO_LIBRARY_CAPTURE_TYPE, LIBRARY_TO_UI_CAPTURE_TYPE,
)

TPL = os.path.join(REPO, "InferenceNode", "templates")
NODE_PY = os.path.join(REPO, "InferenceNode", "inference_node.py")


def _factory():
    """The package renamed itself frame_source -> framesource in 0.3.x, and the host dev
    venv and the container can legitimately be on different versions. Support both."""
    try:
        from framesource import FrameSourceFactory
    except ImportError:
        try:
            from frame_source import FrameSourceFactory
        except ImportError:
            pytest.skip("frame-source library not installed in this environment")
    return FrameSourceFactory


def test_mapping_is_a_consistent_bijection():
    assert LIBRARY_TO_UI_CAPTURE_TYPE == {v: k for k, v in UI_TO_LIBRARY_CAPTURE_TYPE.items()}
    assert len(set(UI_TO_LIBRARY_CAPTURE_TYPE.values())) == len(UI_TO_LIBRARY_CAPTURE_TYPE), \
        "two UI names map onto the same library name - the inverse would be lossy"


def test_mapping_is_defined_once():
    """A second private copy is what let the API and the runtime disagree."""
    for path in (NODE_PY, os.path.join(REPO, "InferenceNode", "pipeline_manager.py")):
        src = open(path, encoding="utf-8").read()
        literal_maps = re.findall(r"['\"]ip_camera['\"]\s*:\s*['\"]ipcam['\"]", src)
        if path == NODE_PY:
            assert not literal_maps, "inference_node.py re-declares the capture-type mapping"
        else:
            assert len(literal_maps) == 1, \
                "pipeline_manager.py should declare the mapping exactly once"


def test_every_capture_type_the_ui_selects_is_reachable():
    """Whatever selectFrameSource() asks for must be a name the API can advertise:
    either a library name as-is, or a UI name we translate."""
    library_types = set(_factory().get_available_types() or [])
    advertised = {LIBRARY_TO_UI_CAPTURE_TYPE.get(t, t) for t in library_types}

    requested = set()
    for name in os.listdir(TPL):
        if name.endswith(".html"):
            html = open(os.path.join(TPL, name), encoding="utf-8").read()
            requested |= set(re.findall(r"selectFrameSource\(\s*['\"]([a-z_]+)['\"]", html))

    assert requested, "no selectFrameSource() calls found - did the builder change?"
    missing = sorted(requested - advertised)
    assert not missing, (
        f"the Pipeline Builder selects capture type(s) the API never advertises: {missing}. "
        f"Add them to UI_TO_LIBRARY_CAPTURE_TYPE or fix the template.")


@pytest.mark.parametrize("ui_name,lib_name", sorted(UI_TO_LIBRARY_CAPTURE_TYPE.items()))
def test_mapped_names_actually_exist_in_the_library(ui_name, lib_name):
    """Catches the library renaming a type out from under us."""
    types = set(_factory().get_available_types() or [])
    assert lib_name in types, (
        f"UI '{ui_name}' maps to library '{lib_name}', which the installed framesource "
        f"does not provide. Available: {sorted(types)}")


def test_api_supplies_every_field_the_card_renderer_reads():
    """The builder renders `type.description`, `type.icon`, etc. Any field the API does
    not supply renders literally as "undefined" on the card - which is exactly what
    happened when the endpoint was rebuilt and only returned type/name/available."""
    html = open(os.path.join(TPL, "pipeline_builder.html"), encoding="utf-8").read()

    # Scope to the FRAME-SOURCE renderers. The same `type.x` shape is used by the
    # destination and engine selectors, whose objects come from different endpoints.
    referenced = set()
    for fn in ("updateFrameSourceSelector", "updateFrameSourceQuickSearchBadges"):
        assert f"function {fn}" in html, f"{fn} disappeared from the builder"
        body = html.split(f"function {fn}", 1)[1].split("\nfunction ", 1)[0]
        # [a-z_]+ deliberately: it stops before the capital in `type.toUpperCase()`,
        # so exclude that artifact rather than letting it look like a field.
        referenced |= {m for m in re.findall(r"\btype\.([a-z_]+)", body)
                       if not re.search(rf"type\.{m}[A-Z]", body)}

    src = open(NODE_PY, encoding="utf-8").read()
    block = src.split("def _collect_frame_source_types", 1)[1].split("\n    def ", 1)[0]
    supplied = set(re.findall(r"['\"]([a-z_]+)['\"]\s*:", block))          # entry = {...}
    supplied |= set(re.findall(r"entry\[['\"]([a-z_]+)['\"]\]\s*=", block))  # entry['x'] = ...

    missing = sorted(referenced - supplied)
    assert not missing, (
        f"the frame-source card renderer reads {missing}, which /api/frame-sources never "
        f"supplies - those render as 'undefined'. Add them in _collect_frame_source_types.")


def test_stored_pipeline_configs_still_use_ui_names():
    """Existing pipelines persist capture_type in their config. If the API ever switched to
    library names, those saved pipelines would stop matching the builder too."""
    ui_names = set(UI_TO_LIBRARY_CAPTURE_TYPE)
    lib_names = set(LIBRARY_TO_UI_CAPTURE_TYPE)
    # The runtime must accept BOTH: UI names from the builder, library names untouched.
    for name in ui_names:
        assert UI_TO_LIBRARY_CAPTURE_TYPE.get(name) in lib_names
    for name in lib_names:
        assert UI_TO_LIBRARY_CAPTURE_TYPE.get(name, name) == name, \
            "a library name must pass through the mapping unchanged"
