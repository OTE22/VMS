"""Step 6 - stop copying 5.9 MB per frame when nothing reads it.

Step 3 gated inference to a target FPS but left the frame-storage code untouched, so the
`results is None` branch became the hottest path in the system: it fires at CAPTURE rate
(25 fps/camera) while the two inference branches fire at the gated rate (5 fps). Measured
on this host:

    1080p frame = 5.9 MB, copy = 0.251 ms
    750 frames/s  (30 cameras)  -> 0.19 core-seconds/s, 4.35 GB/s
    3000 frames/s (120 cameras) -> 0.75 core-seconds/s, 17.4 GB/s

The copy itself is NOT removed - `_latest_frame` is handed to other threads, and while
cv2 hands back a freshly allocated buffer per read() (verified: 8 consecutive reads gave 8
distinct pointers, each owning its data), the Basler/GenICam/RealSense sources may wrap a
driver buffer that gets requeued. Instead the WORK IS SKIPPED when nothing consumes it.

There is a correctness half to this too. `_deliver_job` reads `_latest_frame`
asynchronously for result_image destinations. The gated branch used to overwrite the
annotated frame stored by the drawing branch, so at 5 fps inference against 25 fps
capture, 4 of every 5 frames replaced the drawn output with an undrawn one.
"""
import os
import re
import sys
import threading

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.pipeline import InferencePipeline  # noqa: E402

SRC = open(os.path.join(REPO, "InferenceNode", "pipeline.py"), encoding="utf-8").read()


# ------------------------------------------------------------------ a minimal pipeline
class _Recorder:
    """Stands in for the run loop's frame-storage block so the decision can be exercised
    without a camera, a model, or a thread."""

    def __init__(self, streaming=False, thumb_captured=True, thumb_path="/tmp/t.jpg"):
        self._is_streaming = streaming
        self._thumbnail_captured = thumb_captured
        self._thumbnail_path = thumb_path
        self._frame_lock = threading.Lock()
        self._latest_frame = None
        self.copies = 0
        self.thumbnails = 0

    def capture_thumbnail(self, frame):
        self.thumbnails += 1
        self._thumbnail_captured = True

    def store_gated_frame(self, frame):
        """The exact condition the run loop now uses for a gated frame."""
        if self._is_streaming or (not self._thumbnail_captured and self._thumbnail_path):
            with self._frame_lock:
                self.copies += 1
                self._latest_frame = frame.copy()
                if not self._thumbnail_captured and self._thumbnail_path:
                    self.capture_thumbnail(frame)


def _frame(h=64, w=64):
    return np.zeros((h, w, 3), dtype=np.uint8)


# ------------------------------------------------------------------ the work is skipped
def test_no_viewer_and_thumbnail_done_means_no_copy():
    """The 750/s case: nobody is watching, so the copy buys nothing."""
    r = _Recorder(streaming=False, thumb_captured=True)
    for _ in range(100):
        r.store_gated_frame(_frame())
    assert r.copies == 0, "gated frames must not be copied when nothing reads them"


def test_a_live_viewer_still_gets_every_gated_frame():
    """Preview smoothness must not regress: with a viewer, behaviour is unchanged."""
    r = _Recorder(streaming=True, thumb_captured=True)
    for _ in range(100):
        r.store_gated_frame(_frame())
    assert r.copies == 100
    assert r._latest_frame is not None


def test_the_first_thumbnail_is_still_captured_without_a_viewer():
    """The thumbnail is a one-time consumer and must not be starved by the new guard."""
    r = _Recorder(streaming=False, thumb_captured=False)
    for _ in range(50):
        r.store_gated_frame(_frame())
    assert r.thumbnails == 1, "exactly one thumbnail, captured on the first gated frame"
    assert r.copies == 1, "and copying stops immediately afterwards"


def test_a_pipeline_with_no_thumbnail_path_does_not_copy():
    r = _Recorder(streaming=False, thumb_captured=False, thumb_path=None)
    for _ in range(20):
        r.store_gated_frame(_frame())
    assert r.copies == 0


def test_a_viewer_arriving_later_starts_getting_frames_again():
    """start_streaming() flips the flag; the next gated frame must repopulate
    _latest_frame well inside the preview route's 5 s wait."""
    r = _Recorder(streaming=False, thumb_captured=True)
    for _ in range(10):
        r.store_gated_frame(_frame())
    assert r._latest_frame is None
    r._is_streaming = True                       # start_streaming()
    r.store_gated_frame(_frame())
    assert r._latest_frame is not None, "the very next frame must repopulate the preview"


# ------------------------------------------------------------------ the copy still happens
def test_the_copy_is_not_removed_only_skipped():
    """Removing `.copy()` would alias whatever the source returned. cv2 allocates a fresh
    buffer per read(), but Basler/GenICam/RealSense may hand back a requeued driver
    buffer, so the copy must stay for the frames we do keep."""
    r = _Recorder(streaming=True, thumb_captured=True)
    f = _frame()
    r.store_gated_frame(f)
    stored = r._latest_frame
    f[:] = 255                                   # simulate the source reusing the buffer
    assert not np.array_equal(stored, f), "stored frame must not alias the capture buffer"


def test_stored_gated_frame_is_independent_of_later_frames():
    r = _Recorder(streaming=True, thumb_captured=True)
    a = _frame(); a[:] = 1
    r.store_gated_frame(a)
    first = r._latest_frame
    b = _frame(); b[:] = 2
    r.store_gated_frame(b)
    assert first[0, 0, 0] == 1, "an earlier stored frame must keep its own pixels"


# ------------------------------------------------------------------ wired into the loop
def test_the_run_loop_guards_the_gated_branch():
    i = SRC.index("# GATED frame: no inference ran")
    body = SRC[i:i + 1800]
    # Step 8 added a leading `frame is not None` term (a grabbed frame carries no pixels),
    # so assert on the consumer check itself rather than the exact line.
    assert "self._is_streaming" in body and "not self._thumbnail_captured and self._thumbnail_path" in body, \
        "the gated branch must be guarded by an actual-consumer check"
    assert "frame is not None and (self._is_streaming" in body, \
        "and must never copy a frame that was grabbed but not decoded"
    assert "self._latest_frame = frame.copy()" in body, "the copy itself must remain"


def test_the_inference_branches_are_untouched():
    """One bottleneck at a time: the drawing branch and the no-image branch fire at the
    gated inference rate and are deliberately NOT changed here."""
    i = SRC.index("if self.result_publisher.do_any_destinations_need_result_image() or self._is_streaming:")
    body = SRC[i:i + 700]
    assert "output = self.inference_engine.draw(frame, results)" in body
    assert "self._latest_frame = output.copy()" in body, "drawing branch must be unchanged"


def test_gated_frames_no_longer_clobber_the_annotated_image():
    """`_deliver_job` reads _latest_frame for result_image destinations. When a webhook
    wants the drawn image and nobody is streaming, a gated frame must not replace it."""
    r = _Recorder(streaming=False, thumb_captured=True)
    annotated = _frame(); annotated[:] = 7
    r._latest_frame = annotated                  # as the drawing branch would leave it
    for _ in range(20):
        r.store_gated_frame(_frame())            # 4-of-5 gated frames in between
    assert r._latest_frame is annotated, \
        "the annotated frame must survive until the next inference frame replaces it"


def test_deliver_job_still_reads_latest_frame_for_result_images():
    """Guard the consumer this change reasons about, so the reasoning stays valid."""
    i = SRC.index("def _deliver_job")
    body = SRC[i:i + 900]
    assert "result_img = self._latest_frame if need_result_image else None" in body
