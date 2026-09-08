"""Step 9 - stop losing 17% of the requested inference rate to scheduling drift.

Inference can only happen when a frame arrives, so at a 25 fps stream the achievable rates
are 25/n: 5.00 (n=5) or 4.17 (n=6), with NOTHING in between. Anchoring the next deadline to
the ACTUAL inference time bakes in that frame's read + scheduling overhead, so the frame
that should trigger the next cycle misses by a few milliseconds and the cycle slips to the
sixth frame. It slipped every single time: 60 real RTSP cameras measured 4.17 fps against a
5.00 fps target.

The fix advances the anchor by exactly one period instead, keeping the schedule aligned to
the start rather than compounding per-cycle overhead.

The property that must NOT regress: no catch-up burst after a stall. That is why the
schedule is abandoned and re-anchored whenever we are already a full period behind.
"""
import logging
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.pipeline import InferencePipeline  # noqa: E402


def _p(target=5.0):
    p = InferencePipeline.__new__(InferencePipeline)
    p.logger = logging.getLogger("t")
    p.TARGET_INFERENCE_FPS = target
    p._last_inference_at = None
    return p


def _run(p, stream_fps, seconds, jitter=0.0):
    """Feed frames at `stream_fps`, each carrying `jitter` seconds of overhead - exactly
    what the real loop sees, and exactly what the ideal-timestamp tests never exercised."""
    n = 0
    for i in range(int(stream_fps * seconds)):
        t = i / stream_fps + jitter
        if p._due_for_inference(t):
            p._mark_inferred(t)
            n += 1
    return n


# ------------------------------------------------------------------ the bug
def test_five_fps_is_actually_five_not_four_seventeen():
    """With per-frame overhead, the old anchor slipped to every 6th frame (4.17 fps)."""
    p = _p(5.0)
    n = _run(p, 25, 10, jitter=0.0)
    assert n == 50, f"expected 5.00 fps over 10 s, got {n / 10:.2f} fps"


@pytest.mark.parametrize("overhead_ms", [0.5, 1.0, 3.0, 8.0])
def test_the_rate_survives_realistic_per_frame_overhead(overhead_ms):
    """The overhead that caused the slip is read + scheduling time, a few ms under GIL
    contention. The schedule must absorb it rather than compound it."""
    p = _p(5.0)
    n = 0
    for i in range(250):                       # 10 s of a 25 fps camera
        t = i / 25.0 + (overhead_ms / 1000.0) * (i % 3)   # varying, not constant
        if p._due_for_inference(t):
            p._mark_inferred(t)
            n += 1
    assert n >= 49, f"{overhead_ms} ms overhead cost {50 - n} inferences over 10 s"


def test_drift_does_not_compound_over_a_long_run():
    """5 minutes at 25 fps: a per-cycle bias of even 1 ms compounds into lost inferences."""
    p = _p(5.0)
    n = 0
    for i in range(25 * 300):
        t = i / 25.0 + 0.002                    # constant 2 ms overhead
        if p._due_for_inference(t):
            p._mark_inferred(t)
            n += 1
    assert abs(n - 1500) <= 2, f"expected ~1500 over 5 min, got {n}"


@pytest.mark.parametrize("target,stream", [(5, 25), (10, 30), (2, 25), (1, 25), (5, 30), (15, 30)])
def test_rate_holds_across_targets_and_stream_rates(target, stream):
    p = _p(float(target))
    n = _run(p, stream, 10)
    achievable = stream / max(1, round(stream / target))     # nearest stream_fps/n
    assert abs(n / 10.0 - achievable) < 0.35, \
        f"{target} fps from {stream} fps: got {n / 10.0:.2f}, nearest achievable {achievable:.2f}"


# ------------------------------------------------------------------ the property to keep
def test_a_stall_does_not_cause_a_catch_up_burst():
    """THE risk of a fixed-step schedule. After a 5 s stall the pipeline must resume at the
    normal rate, not fire repeatedly to make up lost ground."""
    p = _p(5.0)
    p._due_for_inference(0.0); p._mark_inferred(0.0)
    assert p._due_for_inference(5.0)
    p._mark_inferred(5.0)                        # long stall ends here
    fired = [t for t in (5.04, 5.08, 5.12, 5.16) if p._due_for_inference(t)]
    assert fired == [], f"burst after stall: fired at {fired}"
    assert p._due_for_inference(5.2), "and the normal cadence must resume"


def test_the_anchor_is_re_anchored_not_rewound_after_a_stall():
    p = _p(5.0)
    p._mark_inferred(0.0)
    p._mark_inferred(10.0)                       # a stall
    assert p._last_inference_at == 10.0, "must re-anchor to now, not to 0.2"


def test_a_single_missed_deadline_is_not_replayed():
    """Falling exactly one period behind re-anchors; it must not fire twice in a row."""
    p = _p(5.0)
    p._mark_inferred(0.0)
    p._mark_inferred(0.45)                       # 2.25 periods later
    assert p._last_inference_at == 0.45
    assert p._due_for_inference(0.5) is False


def test_first_inference_anchors_to_now():
    p = _p(5.0)
    assert p._last_inference_at is None
    p._mark_inferred(123.456)
    assert p._last_inference_at == 123.456


def test_infer_every_frame_mode_is_untouched():
    """TARGET_INFERENCE_FPS = 0 means infer every frame; the schedule must not engage."""
    p = _p(0.0)
    p._mark_inferred(1.0)
    assert p._last_inference_at == 1.0
    assert p._due_for_inference(1.0001) is True


def test_the_run_loop_uses_the_scheduler():
    src = open(os.path.join(REPO, "InferenceNode", "pipeline.py"), encoding="utf-8").read()
    assert "self._mark_inferred(now_perf)" in src
    assert "self._last_inference_at = now_perf" not in src, \
        "the run loop must not re-introduce the drifting anchor"
