"""Step 8 - stop decoding 4 frames in 5 that nothing ever looks at.

cv2's read() is grab() + retrieve(). grab() pulls the next frame off the wire and decodes
it; retrieve() converts it to a BGR numpy array and copies it into Python. Running at
25 fps capture against a 5 fps inference target (Step 3), 4 frames in 5 were converted into
an array nobody read.

Measured against a REAL RTSP camera (mediamtx + libx264, 1080p25) - NOT a local file,
because the two differ by 2x and the file number is the flattering one:

    read()                6.45 ms CPU/frame
    grab() only           3.14 ms CPU/frame
    grab x5 + retrieve x1 3.57 ms CPU/frame     -> 1.81x cheaper capture
    (same test on a video file reads 3.84x - network + H.264 decode still happen under
     grab() on RTSP; only the conversion and copy are skipped)

Correctness on RTSP was verified before writing this: 25 grabs / 5 retrieves returned
valid 1080p frames, non-blank, each differing from the last.

Two things make this dangerous, and both are pinned below:
  * a VIDEO FILE paces its own playback inside read(); bypassing it makes files race
    through at full speed, so files stay on the original path
  * the inference gate is re-evaluated AFTER the read, so it can come due on a frame we
    chose not to decode - infer(None) would crash the pipeline
"""
import logging
import os
import sys
import threading

import time

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.pipeline import InferencePipeline  # noqa: E402

SRC = open(os.path.join(REPO, "InferenceNode", "pipeline.py"), encoding="utf-8").read()


class _Cap:
    def __init__(self, grab_ok=True, raises=False, retrieve_ok=True):
        self.grabs = self.retrieves = 0
        self.grab_ok, self.raises, self.retrieve_ok = grab_ok, raises, retrieve_ok

    def grab(self):
        if self.raises:
            raise RuntimeError("backend has no grab")
        self.grabs += 1
        return self.grab_ok

    def retrieve(self):
        self.retrieves += 1
        return (True, np.zeros((4, 4, 3), dtype=np.uint8)) if self.retrieve_ok else (False, None)


class _Source:
    def __init__(self, **kw):
        self.cap = _Cap(**kw)
        self.reads = 0

    def read(self):
        self.reads += 1
        return True, np.zeros((4, 4, 3), dtype=np.uint8)


def _p(capture_type="ipcam", *, streaming=False, thumb_captured=True,
       thumb_path="/tmp/t.jpg", enabled=True, **cap_kw):
    p = InferencePipeline.__new__(InferencePipeline)
    p._frame_source_config = {"capture_type": capture_type}
    p.logger = logging.getLogger("t")
    p.source = _Source(**cap_kw)
    p._skip_decode_supported = True
    p._is_streaming = streaming
    p._thumbnail_captured = thumb_captured
    p._thumbnail_path = thumb_path
    p._inference_enabled = enabled
    p.TARGET_INFERENCE_FPS = 5.0
    p._last_inference_at = None
    p._frame_lock = threading.Lock()
    p._latest_frame = None
    return p


# ------------------------------------------------------------------ which sources qualify
@pytest.mark.parametrize("ct", ["ipcam", "ip_camera", "webcam", "basler", "genicam"])
def test_live_cameras_can_skip_decode(ct):
    assert _p(ct)._can_skip_decode() is True


@pytest.mark.parametrize("ct", ["video_file", "folder", "image_folder", ""])
def test_files_and_folders_never_skip_decode(ct):
    """real_time pacing lives INSIDE read(); bypassing it makes a file play at full speed."""
    assert _p(ct)._can_skip_decode() is False


def test_a_source_without_a_cap_handle_never_skips():
    p = _p()
    p.source = object()
    assert p._can_skip_decode() is False


def test_the_env_kill_switch_disables_it(monkeypatch):
    """One env var reverts every pipeline to the old behaviour."""
    monkeypatch.setattr(InferencePipeline, "SKIP_DECODE_ENABLED", False)
    assert _p()._can_skip_decode() is False


# ------------------------------------------------------------------ when pixels are needed
def test_pixels_are_needed_when_inference_is_due():
    p = _p()
    assert p._want_decoded_frame(100.0) is True          # never inferred yet -> due


def test_pixels_are_not_needed_between_inferences():
    p = _p()
    p._last_inference_at = 100.0
    assert p._want_decoded_frame(100.05) is False        # 50 ms into a 200 ms period


def test_pixels_are_needed_again_once_the_period_elapses():
    p = _p()
    p._last_inference_at = 100.0
    assert p._want_decoded_frame(100.2) is True          # 1/5 s later


def test_a_live_viewer_forces_a_decode():
    """Preview smoothness must not regress while someone is watching."""
    p = _p(streaming=True)
    p._last_inference_at = 100.0
    assert p._want_decoded_frame(100.05) is True


def test_a_pending_thumbnail_forces_a_decode():
    p = _p(thumb_captured=False)
    p._last_inference_at = 100.0
    assert p._want_decoded_frame(100.05) is True


def test_disabled_inference_with_no_viewer_needs_no_pixels():
    p = _p(enabled=False)
    p._last_inference_at = 100.0
    assert p._want_decoded_frame(100.05) is False


# ------------------------------------------------------------------ the grab path itself
def test_a_frame_nobody_needs_is_grabbed_but_not_retrieved():
    p = _p()
    p._last_inference_at = time.perf_counter()          # not due
    ok, frame, wanted = p._grab_then_maybe_retrieve()
    assert ok is True and frame is None and wanted is False
    assert p.source.cap.grabs == 1
    assert p.source.cap.retrieves == 0, "the expensive conversion must NOT have run"
    assert p.source.reads == 0


def test_a_frame_that_is_due_is_grabbed_AND_retrieved():
    p = _p()
    p._last_inference_at = None                          # due
    ok, frame, wanted = p._grab_then_maybe_retrieve()
    assert ok is True and frame is not None and wanted is True
    assert p.source.cap.grabs == 1 and p.source.cap.retrieves == 1


def test_the_gate_is_evaluated_AFTER_the_grab_not_before():
    """THE timing bug. Deciding before the read used time from the top of the iteration -
    up to a full frame period stale - so a 'not due' verdict at t=195ms grabbed, and the
    inference slipped to the next frame. Measured cost on a real RTSP camera: 4.43 -> 3.62
    inferences/s against a 5 fps target. grab() must therefore happen FIRST."""
    p = _p()
    order = []
    real_grab = p.source.cap.grab
    p.source.cap.grab = lambda: (order.append("grab"), real_grab())[1]
    p._want_decoded_frame = lambda now: (order.append("decide"), True)[1]
    p._grab_then_maybe_retrieve()
    assert order == ["grab", "decide"], f"gate must be evaluated after the grab, got {order}"


def test_a_failed_grab_reports_failure_so_the_reconnect_path_still_fires():
    """Step 5 escalates persistent read failures to a reconnect; a dead camera must not be
    hidden just because we were skipping decodes."""
    p = _p(grab_ok=False)
    ok, frame, wanted = p._grab_then_maybe_retrieve()
    assert ok is False and frame is None
    assert wanted is True, "a failed grab must reach the failure path, not look like a skip"


def test_a_failed_retrieve_is_a_failed_read():
    p = _p(retrieve_ok=False)
    p._last_inference_at = None                          # due -> will retrieve
    ok, frame, wanted = p._grab_then_maybe_retrieve()
    assert ok is False and frame is None and wanted is True


def test_an_unsupported_backend_reverts_permanently_and_does_not_look_like_a_dead_camera():
    """If grab() raises, that is a capability problem, not a camera problem. Falling back
    to read() keeps frames flowing; reporting False would trigger a bogus reconnect."""
    p = _p(raises=True)
    ok, frame, wanted = p._grab_then_maybe_retrieve()
    assert ok is True and frame is not None, "must fall back to a real read()"
    assert p._skip_decode_supported is False, "and must not try grab() again"
    assert p._can_skip_decode() is False


def test_a_source_with_processors_stays_on_the_original_path():
    """read() applies attached frame processors; grab/retrieve bypasses them."""
    p = _p()
    p.source._processors = [object()]
    assert p._can_skip_decode() is False


# ------------------------------------------------------------------ wired into the loop
def test_the_loop_uses_grab_then_retrieve_for_live_sources():
    i = SRC.index("if self._can_skip_decode():")
    body = SRC[i:i + 400]
    assert "success, frame, _decode = self._grab_then_maybe_retrieve()" in body
    assert "success, frame = self.source.read()" in body, "files keep the original path"


def test_inference_never_runs_on_an_undecoded_frame():
    """The gate is re-checked against POST-read time, so it can come due on a frame we
    chose to skip. infer(None) would crash the pipeline."""
    i = SRC.index("if (frame is not None and self._inference_enabled")
    assert "self._due_for_inference(now_perf)" in SRC[i:i + 200]


def test_the_frame_store_never_stores_an_undecoded_frame():
    i = SRC.index("# GATED frame: no inference ran")
    body = SRC[i:i + 1900]
    assert "if frame is not None and (self._is_streaming" in body


def test_grabbed_frames_still_count_as_captured():
    """They really were pulled off the camera, so capture FPS and the reconnect backoff
    must treat them as genuine frames."""
    i = SRC.index("success, frame, _decode = self._grab_then_maybe_retrieve()")
    after = SRC[i:i + 2600]
    assert "self._frame_counter += 1" in after
    assert "self._reconnect_attempts = 0" in after


# ------------------------------------------------------------------ the bug this shipped with
def test_a_grabbed_frame_is_not_treated_as_a_failed_read():
    """THE regression. The loop's failure check was `if not success or frame is None:`, and a
    deliberately-grabbed frame has no array BY DESIGN. Every skipped frame therefore took the
    failure path - incrementing _failed_read_count, sleeping FAILED_READ_SLEEP, and never
    counting as captured. Measured against a real RTSP camera: capture collapsed from
    24.99 fps to 4.13 fps, i.e. the optimisation made capture 6x WORSE.

    Unit-testing the helpers in isolation did not catch this; only running it against an
    actual camera did.
    """
    i = SRC.index("if not success or (_decode and frame is None):")
    assert "if not success or frame is None:" not in SRC, \
        "a grabbed frame must not be classified as a missing frame"
    # and the failure path must still fire for a genuinely failed read
    body = SRC[i:i + 300]
    assert "self._failed_read_count += 1" in body


def test_a_real_read_failure_is_still_a_failure():
    """The narrowed condition must not hide a dead camera when we DID want pixels."""
    i = SRC.index("if not success or (_decode and frame is None):")
    # `not success` still covers a failed grab; `_decode and frame is None` covers a read
    # that returned nothing. Both remain.
    cond = SRC[i:i + 60]
    assert "not success" in cond and "_decode and frame is None" in cond
