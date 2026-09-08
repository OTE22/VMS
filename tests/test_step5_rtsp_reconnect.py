"""Step 5 — a camera that drops must not end its pipeline, and must not spin a CPU core.

Before this, `pipeline.py` handled a non-folder disconnect with:
    self.logger.warning("Source disconnected, ending pipeline"); break
so the first network blip, PoE reset or camera reboot killed the pipeline until a human
restarted it. A failed read took `continue` with no sleep, so a dead source burned a core.

The contract now:
  * a LIVE source (camera) reconnects with bounded exponential backoff, 1s -> 30s, and
    keeps trying indefinitely - a camera may come back hours later
  * a file/folder reaching its end is NOT a disconnect and still ends the pipeline
  * failed reads never spin: they sleep, and escalate to a reconnect once they persist
  * stop() is honoured immediately, even mid-backoff
"""
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.pipeline import InferencePipeline  # noqa: E402


def _p(capture_type="ipcam"):
    import logging
    p = InferencePipeline.__new__(InferencePipeline)
    p._frame_source_config = {"capture_type": capture_type}
    p._stop_requested = False
    p.logger = logging.getLogger("t")
    p.RECONNECT_INITIAL_DELAY = 0.001          # keep the tests fast; ratios still hold
    p.RECONNECT_MAX_DELAY = 0.008
    p.FAILED_READS_BEFORE_RECONNECT = 30
    p.FAILED_READ_SLEEP = 0.001
    p._reconnect_attempts = 0
    return p


class _Source:
    """A source that fails `fail_times` reconnects, then succeeds."""
    def __init__(self, fail_times=0, never=False):
        self.fail_times, self.never = fail_times, never
        self.connects = self.stops = 0
        self.opened = False
    def connect(self):
        self.connects += 1
        if self.never or self.connects <= self.fail_times:
            self.opened = False
            raise ConnectionError("camera unreachable")
        self.opened = True
    def isOpened(self): return self.opened
    def stop(self): self.stops += 1


# ------------------------------------------------------------------ which sources reconnect
@pytest.mark.parametrize("ct", ["ipcam", "ip_camera", "webcam", "realsense", "basler", "genicam"])
def test_camera_sources_are_reconnectable(ct):
    assert _p(ct)._is_live_source() is True


@pytest.mark.parametrize("ct", ["video_file", "folder", "image_folder", "", "screen"])
def test_files_and_folders_are_not_reconnectable(ct):
    """A video file reaching its end is not a disconnect - reopening it would silently
    restart playback, which is not what 'the stream ended' means."""
    p = _p(ct)
    assert p._is_live_source() is False
    assert p._reconnect_source() is False, "a file/folder must not be reconnected"


# ------------------------------------------------------------------ the reconnect itself
def test_reopening_the_handle_does_not_reset_the_backoff():
    """cv2.VideoCapture reports a DEAD RTSP stream as "opened", so a reopened handle is not
    evidence the camera is back. Measured against a real dead camera, resetting here gave
    78 reconnects in 200 s instead of backing off. Only a real frame resets it."""
    p = _p(); p.source = _Source(fail_times=0)
    assert p._reconnect_source() is True
    assert p.source.connects == 1
    assert p.source.stops == 1, "the stale handle must be released before reopening"
    assert p._reconnect_attempts == 1, "a reopened handle must NOT reset the backoff"


def test_backoff_escalates_against_a_camera_that_opens_but_yields_nothing():
    """The exact real-world case: the handle opens every time, frames never arrive."""
    p = _p(); p.source = _Source(fail_times=0)
    p.RECONNECT_INITIAL_DELAY, p.RECONNECT_MAX_DELAY = 0.01, 0.08
    waits = []
    for _ in range(5):
        t0 = time.perf_counter(); p._reconnect_source(); waits.append(time.perf_counter() - t0)
    assert waits[-1] > waits[0] * 1.5, f"backoff must escalate, not restart: {waits}"


def test_it_keeps_retrying_and_eventually_succeeds():
    """A camera down for several attempts must be picked up when it returns."""
    p = _p(); p.source = _Source(fail_times=3)
    outcomes = [p._reconnect_source() for _ in range(4)]
    assert outcomes[:3] == [True, True, True], "must keep trying, not give up"
    assert outcomes[3] is True and p.source.isOpened()


def test_attempts_are_unbounded_for_a_camera_that_never_returns():
    """Bounded DELAY, unbounded ATTEMPTS - the pipeline waits rather than dying."""
    p = _p(); p.source = _Source(never=True)
    assert all(p._reconnect_source() is True for _ in range(25))
    assert p.source.connects == 25


def test_backoff_grows_then_is_capped():
    p = _p(); p.source = _Source(never=True)
    p.RECONNECT_INITIAL_DELAY, p.RECONNECT_MAX_DELAY = 0.01, 0.04
    waits = []
    for _ in range(6):
        t0 = time.perf_counter(); p._reconnect_source(); waits.append(time.perf_counter() - t0)
    assert waits[1] > waits[0] * 1.5, f"backoff should grow: {waits[:2]}"
    assert max(waits) <= 0.04 * 3, f"backoff must be capped, saw {max(waits):.3f}s"


# ------------------------------------------------------------------ stopping wins
def test_stop_is_honoured_immediately_during_backoff():
    """A restart or shutdown must not wait out a 30 s backoff."""
    p = _p(); p.source = _Source(never=True)
    p.RECONNECT_INITIAL_DELAY = p.RECONNECT_MAX_DELAY = 5.0
    p._stop_requested = True
    t0 = time.perf_counter()
    assert p._reconnect_source() is False
    assert time.perf_counter() - t0 < 1.0, "stop must interrupt the backoff sleep"


def test_interruptible_sleep_returns_false_on_stop():
    p = _p()
    p._stop_requested = True
    t0 = time.perf_counter()
    assert p._sleep_interruptible(5.0) is False
    assert time.perf_counter() - t0 < 1.0


def test_interruptible_sleep_actually_sleeps_when_running():
    p = _p()
    t0 = time.perf_counter()
    assert p._sleep_interruptible(0.05) is True
    assert time.perf_counter() - t0 >= 0.04


# ------------------------------------------------------------------ wired into the loop
def test_the_run_loop_no_longer_dies_on_a_live_source_disconnect():
    src = open(os.path.join(REPO, "InferenceNode", "pipeline.py"), encoding="utf-8").read()
    i = src.index('self.logger.warning("Source disconnected, ending pipeline")')
    before = src[max(0, i - 500):i]
    assert "self._reconnect_source()" in before, "disconnect must attempt reconnect first"


def test_failed_reads_never_hot_spin():
    """The old code did a bare `continue` with no sleep on every failed read."""
    src = open(os.path.join(REPO, "InferenceNode", "pipeline.py"), encoding="utf-8").read()
    i = src.index("self._failed_read_count += 1")
    body = src[i:i + 1600]
    assert "_sleep_interruptible(self.FAILED_READ_SLEEP)" in body
    assert "FAILED_READS_BEFORE_RECONNECT" in body, "persistent failures must escalate to reconnect"


def test_a_successful_read_resets_the_backoff():
    src = open(os.path.join(REPO, "InferenceNode", "pipeline.py"), encoding="utf-8").read()
    i = src.index("# Frame successfully read")
    body = src[i:i + 600]
    assert "self._reconnect_attempts = 0" in body
    assert "Source recovered after" in body, "recovery should be visible in the log"
