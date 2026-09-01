"""Media discovery + source resolution.

The Pipeline Builder showed no Frame Sources because the frame-source library's
VideoFileCapture.discover() returns [] by design and ArmyEye had no media listing at
all. These tests cover the replacement, plus the legacy-path compatibility rules.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.media_library import (                # noqa: E402
    MediaLibrary, MediaError, resolve_frame_source, is_network_source,
)


@pytest.fixture
def root(tmp_path):
    (tmp_path / "site-a").mkdir()
    (tmp_path / "site-b").mkdir()
    for rel in ("camera1.mp4", "UPPER.MP4", "with space.mov", "naïve.mkv",
                "site-a/camera.mp4", "site-b/camera.mp4",
                "notes.txt", "model.pt"):
        p = tmp_path / rel
        p.write_bytes(b"x")
    return tmp_path


@pytest.fixture
def library(root):
    return MediaLibrary(str(root))


# ------------------------------------------------------------------ listing --
def test_lists_supported_media_recursively(library):
    sources, _ = library.list_media()
    rels = {s["relative_path"] for s in sources}
    assert rels == {"camera1.mp4", "UPPER.MP4", "with space.mov", "naïve.mkv",
                    "site-a/camera.mp4", "site-b/camera.mp4"}


def test_extension_matching_is_case_insensitive(library):
    sources, _ = library.list_media()
    assert any(s["display_name"] == "UPPER.MP4" for s in sources)


def test_unsupported_files_excluded_with_a_reason(library):
    sources, excluded = library.list_media()
    assert not any(s["display_name"].endswith((".txt", ".pt")) for s in sources)
    reasons = {e["relative_path"]: e["reason"] for e in excluded}
    assert reasons["notes.txt"] == "unsupported_extension"
    assert reasons["model.pt"] == "unsupported_extension"


def test_duplicate_basenames_stay_distinct_via_relative_path(library):
    sources, _ = library.list_media()
    dupes = [s for s in sources if s["display_name"] == "camera.mp4"]
    assert len(dupes) == 2
    assert {d["relative_path"] for d in dupes} == {"site-a/camera.mp4", "site-b/camera.mp4"}


def test_no_absolute_server_paths_are_exposed(library, root):
    sources, _ = library.list_media()
    blob = repr(sources)
    assert str(root) not in blob
    for s in sources:
        assert not os.path.isabs(s["relative_path"])


def test_new_file_appears_without_restart(library, root):
    before = len(library.list_media()[0])
    (root / "added_later.mp4").write_bytes(b"x")
    after = len(library.list_media()[0])
    assert after == before + 1


def test_missing_root_is_not_fatal(tmp_path):
    lib = MediaLibrary(str(tmp_path / "does-not-exist"))
    assert lib.list_media() == ([], [])


# --------------------------------------------------------------- resolution --
def test_relative_path_resolves_under_root(library, root):
    assert library.resolve_relative("site-a/camera.mp4") == \
        os.path.realpath(str(root / "site-a" / "camera.mp4"))


@pytest.mark.parametrize("evil", [
    "../outside.mp4", "../../etc/passwd", "site-a/../../escape.mp4",
    "/etc/passwd", "C:\\Windows\\win.ini", "..\\..\\win.ini",
])
def test_traversal_and_absolute_paths_rejected(library, evil):
    with pytest.raises(MediaError):
        library.resolve_relative(evil)


def test_symlink_escape_is_excluded(tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "secret.mp4").write_bytes(b"x")
    root = tmp_path / "media"; root.mkdir()
    (root / "ok.mp4").write_bytes(b"x")
    try:
        os.symlink(str(outside / "secret.mp4"), str(root / "link.mp4"))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks not permitted in this environment")
    sources, excluded = MediaLibrary(str(root)).list_media()
    assert {s["relative_path"] for s in sources} == {"ok.mp4"}
    assert any(e["reason"] == "outside_media_root" for e in excluded)


def test_missing_relative_file_is_classified(library):
    with pytest.raises(MediaError) as e:
        library.resolve_relative("nope.mp4")
    assert e.value.code == "MEDIA_FILE_MISSING"


# ------------------------------------------------- frame source resolution --
def test_relative_source_is_preferred(library, root):
    fs = {"capture_type": "video_file", "config": {"relative_source": "camera1.mp4"}}
    res = resolve_frame_source(fs, library)
    assert res["effective_source"] == os.path.realpath(str(root / "camera1.mp4"))
    assert res["source_fallback"] is False


def test_existing_absolute_path_is_used_as_is(library, root):
    real = str(root / "camera1.mp4")
    res = resolve_frame_source({"capture_type": "video_file", "config": {"source": real}}, library)
    assert res["effective_source"] == real
    assert res["source_fallback"] is False


def test_legacy_windows_path_falls_back_to_unique_basename(library, root):
    """The 4 existing pipelines store Windows paths that cannot exist in the container."""
    fs = {"capture_type": "video_file",
          "config": {"source": r"C:\Users\Raven\Desktop\ArmyEye\media\camera1.mp4"}}
    res = resolve_frame_source(fs, library)
    assert res["effective_source"] == str(root / "camera1.mp4")
    assert res["source_fallback"] is True
    assert res["fallback_reason"] == "configured_path_unavailable_basename_matched"
    # the configured value is reported, never rewritten
    assert res["configured_source"].startswith("C:\\")


def test_ambiguous_basename_refuses_to_guess(library):
    fs = {"capture_type": "video_file", "config": {"source": r"C:\somewhere\camera.mp4"}}
    with pytest.raises(MediaError) as e:
        resolve_frame_source(fs, library)
    assert e.value.code == "MEDIA_FILE_AMBIGUOUS"
    assert sorted(e.value.extra["candidates"]) == ["site-a/camera.mp4", "site-b/camera.mp4"]


def test_unknown_basename_is_missing(library):
    fs = {"capture_type": "video_file", "config": {"source": r"C:\x\ghost.mp4"}}
    with pytest.raises(MediaError) as e:
        resolve_frame_source(fs, library)
    assert e.value.code == "MEDIA_FILE_MISSING"


@pytest.mark.parametrize("url", [
    "rtsp://cam.local/stream", "rtsps://cam/s", "http://cam/feed.mjpg",
    "https://cam/feed", "rtmp://server/live",
])
def test_network_sources_are_never_basename_rewritten(library, url):
    res = resolve_frame_source({"capture_type": "video_file", "config": {"source": url}}, library)
    assert res["effective_source"] == url
    assert res["source_fallback"] is False


def test_camera_index_is_not_a_file_source(library):
    assert is_network_source(0) is True
    res = resolve_frame_source({"capture_type": "webcam", "config": {"source": 0}}, library)
    assert res["effective_source"] == 0
