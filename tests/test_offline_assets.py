"""Offline-capability guard.

ArmyEye is deployed on isolated networks, so every frontend asset must be served from
/static/. Nothing was vendored before this: Bootstrap, Font Awesome and Chart.js all came
from CDNs, and login.html loaded no local CSS at all - offline it rendered as unstyled HTML.
Chart.js was additionally UNPINNED (`/npm/chart.js` -> whatever is latest), the same silent
version-drift that already broke frame sources when FrameSource jumped 0.2.7 -> 0.3.0.

These tests scan the whole frontend surface, not just templates: a stray @import or a
dynamically-created <script src="https://..."> in a JS file would break offline just as
badly and is far easier to miss.
"""
import hashlib
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "InferenceNode", "static")
TPL = os.path.join(REPO, "InferenceNode", "templates")
VENDOR = os.path.join(STATIC, "vendor")

# Any absolute URL to somewhere that is not this server.
EXTERNAL_RE = re.compile(r"""(?:https?:)?//(?!\s)[a-z0-9.\-]+\.[a-z]{2,}""", re.I)

# Hosts that would be a genuine runtime dependency if referenced from an asset.
CDN_HINTS = ("cdn.jsdelivr.net", "cdnjs.cloudflare.com", "unpkg.com", "fonts.googleapis.com",
             "fonts.gstatic.com", "code.jquery.com", "stackpath.bootstrapcdn.com",
             "maxcdn.bootstrapcdn.com")


def _frontend_files():
    out = []
    for root in (TPL, os.path.join(STATIC, "css"), os.path.join(STATIC, "js")):
        for dirpath, _d, names in os.walk(root):
            if "vendor" in dirpath.replace(REPO, ""):
                continue                      # vendored libs are third-party, checked separately
            for n in names:
                if n.endswith((".html", ".css", ".js")):
                    out.append(os.path.join(dirpath, n))
    return sorted(out)


def _strip_comments(text, path):
    if path.endswith(".html"):
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    if path.endswith((".js", ".css")):
        # line comments (best effort - avoids flagging documentation URLs)
        text = re.sub(r"(?m)^\s*//.*$", "", text)
    return text


@pytest.mark.parametrize("path", _frontend_files(), ids=lambda p: os.path.basename(p))
def test_no_external_asset_references(path):
    """No CDN/off-origin asset may be referenced from any first-party frontend file."""
    text = _strip_comments(open(path, encoding="utf-8").read(), path)
    hits = [h for h in EXTERNAL_RE.findall(text) if any(c in h for c in CDN_HINTS)]
    assert not hits, (f"{os.path.relpath(path, REPO)} references external host(s): "
                      f"{sorted(set(hits))}. Vendor it under static/vendor/ instead.")


@pytest.mark.parametrize("path", _frontend_files(), ids=lambda p: os.path.basename(p))
def test_no_remote_at_import_or_url(path):
    """@import and url() must never point off-origin."""
    text = _strip_comments(open(path, encoding="utf-8").read(), path)
    for m in re.finditer(r"""@import\s+(?:url\()?['"]?([^'")\s;]+)""", text):
        assert not m.group(1).startswith(("http://", "https://", "//")), \
            f"{os.path.basename(path)} @imports a remote stylesheet: {m.group(1)}"
    for m in re.finditer(r"""url\(\s*['"]?([^'")]+)""", text):
        ref = m.group(1).strip()
        assert not ref.startswith(("http://", "https://", "//")), \
            f"{os.path.basename(path)} has a remote url(): {ref}"


@pytest.mark.parametrize("path", [p for p in _frontend_files() if p.endswith((".js", ".html"))],
                         ids=lambda p: os.path.basename(p))
def test_no_dynamically_injected_remote_assets(path):
    """A script/link created at runtime with a remote src would defeat a static scan."""
    text = _strip_comments(open(path, encoding="utf-8").read(), path)
    if not re.search(r"""createElement\(\s*['"](?:script|link)['"]""", text):
        return
    for m in re.finditer(r"""\.(?:src|href)\s*=\s*['"`]([^'"`]+)""", text):
        val = m.group(1)
        assert not val.startswith(("http://", "https://", "//")), \
            f"{os.path.basename(path)} injects a remote asset at runtime: {val}"


# --------------------------------------------------------------------------- #
# Vendored libraries
# --------------------------------------------------------------------------- #
def test_vendor_manifest_exists_and_is_not_markdown():
    """.dockerignore excludes *.md, so a VERSIONS.md would never reach the image."""
    manifest = os.path.join(VENDOR, "VERSIONS.txt")
    assert os.path.isfile(manifest), "static/vendor/VERSIONS.txt is missing"
    assert not os.path.exists(os.path.join(VENDOR, "VERSIONS.md")), \
        "VERSIONS.md would be stripped by .dockerignore - keep the manifest as .txt"


def test_vendored_files_match_recorded_checksums():
    """Detects accidental replacement or silent version drift."""
    manifest = open(os.path.join(VENDOR, "VERSIONS.txt"), encoding="utf-8").read()
    rows = re.findall(r"^\s*([0-9a-f]{64})\s+(\d+)\s+(\S+)\s*$", manifest, re.M)
    assert rows, "no checksum rows found in VERSIONS.txt"
    for digest, size, rel in rows:
        path = os.path.join(VENDOR, rel)
        assert os.path.isfile(path), f"vendored file missing: {rel}"
        blob = open(path, "rb").read()
        assert len(blob) == int(size), f"{rel} size changed ({len(blob)} vs {size})"
        assert hashlib.sha256(blob).hexdigest() == digest, f"{rel} checksum mismatch"


def test_versions_are_pinned_not_latest():
    manifest = open(os.path.join(VENDOR, "VERSIONS.txt"), encoding="utf-8").read()
    for lib in ("Bootstrap 5.3.0", "Font Awesome Free 6.4.0", "Chart.js 4.5.1"):
        assert lib in manifest, f"{lib} not recorded in VERSIONS.txt"
    # The unpinned form is what caused the drift hazard in the first place.
    assert "npm/chart.js\n" not in manifest and "/npm/chart.js\"" not in manifest


def test_every_font_awesome_url_reference_resolves():
    """all.min.css resolves fonts via ../webfonts/. If a single referenced file is absent
    the icons silently become empty boxes, so the CSS - not an assumption about which
    families are used - is the authority. (It references 8 files, including the
    easy-to-miss fa-v4compatibility.)"""
    css_path = os.path.join(VENDOR, "fontawesome", "css", "all.min.css")
    assert os.path.isfile(css_path), "vendored Font Awesome CSS is missing"
    css = open(css_path, encoding="utf-8").read()

    refs = set()
    for m in re.finditer(r"""url\(\s*['"]?([^'")]+)""", css):
        refs.add(m.group(1).strip().split("?")[0].split("#")[0])
    assert refs, "no url() references found - is this really the Font Awesome CSS?"

    missing = []
    for ref in sorted(refs):
        resolved = os.path.normpath(os.path.join(os.path.dirname(css_path), ref))
        if not os.path.isfile(resolved):
            missing.append(ref)
    assert not missing, f"fontawesome_missing_assets={len(missing)}: {missing}"


def test_font_awesome_layout_is_preserved():
    """css/ and webfonts/ must remain siblings or ../webfonts/ stops resolving."""
    assert os.path.isdir(os.path.join(VENDOR, "fontawesome", "css"))
    assert os.path.isdir(os.path.join(VENDOR, "fontawesome", "webfonts"))


# --------------------------------------------------------------------------- #
# Docker packaging
# --------------------------------------------------------------------------- #
def test_dockerignore_does_not_exclude_vendored_or_auth_assets():
    """Host files are not enough - they must reach the build context too."""
    di = os.path.join(REPO, ".dockerignore")
    if not os.path.isfile(di):
        pytest.skip("no .dockerignore")
    patterns = [l.strip() for l in open(di, encoding="utf-8")
                if l.strip() and not l.strip().startswith("#")]
    for bad in ("static/", "InferenceNode/static/", "static/vendor/",
                "InferenceNode/static/vendor/", "*.css", "*.js", "*.woff2", "*.ttf",
                "InferenceNode/static/img/", "*.webp"):
        assert bad not in patterns, f".dockerignore excludes {bad!r}, which would strip vendored assets"
