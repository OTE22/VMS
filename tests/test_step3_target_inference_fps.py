"""Step 3 — configurable target inference rate.

Before this, every frame read was inferred (`pipeline.py`: `if self._inference_enabled:`),
so a 25 fps camera cost 25 inferences/second and there was no way to ask for fewer. The
`fps` field on the RTSP source schema is documented as "informational" and throttles nothing.

The contract now:
  * frames are ALWAYS read, so the decoder is drained and the pipeline stays at the live edge
  * inference runs at most TARGET_INFERENCE_FPS times per second
  * a skipped frame still refreshes the preview, so video stays smooth while AI samples slower
  * 0 / unset  = infer every frame (unchanged historical behaviour)
  * no queueing, no sleeping, no catch-up bursts after a stall
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.pipeline import InferencePipeline  # noqa: E402


def _p(target=0.0):
    """A pipeline object without any of the runtime it does not need for gating."""
    p = InferencePipeline.__new__(InferencePipeline)
    p.TARGET_INFERENCE_FPS = target
    p._last_inference_at = None   # never inferred yet
    return p


# ------------------------------------------------------------------ default is unchanged
def test_unset_infers_every_frame():
    """The historical behaviour must survive: no configuration => nothing is skipped."""
    p = _p(0.0)
    assert all(p._due_for_inference(t) for t in (0.0, 0.001, 0.002, 0.003))


@pytest.mark.parametrize("bad", ["", "   ", "abc", "-5", "nonsense"])
def test_invalid_env_falls_back_to_every_frame(monkeypatch, bad):
    """An unreadable value must not silently reduce how much of the stream is analysed."""
    monkeypatch.setenv("ARMYEYE_TARGET_INFERENCE_FPS", bad)
    assert InferencePipeline._env_target_fps() == 0.0


@pytest.mark.parametrize("raw,want", [("5", 5.0), ("5.0", 5.0), ("0.5", 0.5), (" 10 ", 10.0), ("0", 0.0)])
def test_valid_env_is_read(monkeypatch, raw, want):
    monkeypatch.setenv("ARMYEYE_TARGET_INFERENCE_FPS", raw)
    assert InferencePipeline._env_target_fps() == want


def test_env_absent_is_zero(monkeypatch):
    monkeypatch.delenv("ARMYEYE_TARGET_INFERENCE_FPS", raising=False)
    assert InferencePipeline._env_target_fps() == 0.0


# ------------------------------------------------------------------ the gate itself
def test_five_fps_admits_five_frames_per_second():
    """The property that matters: a 25 fps stream yields ~5 inferences per second."""
    p = _p(5.0)
    inferred = 0
    for i in range(125):                      # 5 seconds of a 25 fps camera
        t = i / 25.0
        if p._due_for_inference(t):
            p._last_inference_at = t
            inferred += 1
    assert inferred == 25, f"expected ~5/s over 5s, got {inferred}"


@pytest.mark.parametrize("target,stream,secs", [(5, 25, 4), (1, 25, 6), (10, 30, 3), (2, 15, 5),
                                                (5, 30, 4), (3, 25, 4), (5, 25, 10)])
def test_rate_holds_across_targets_and_stream_rates(target, stream, secs):
    p = _p(float(target))
    inferred = 0
    for i in range(stream * secs):
        t = i / stream
        if p._due_for_inference(t):
            p._last_inference_at = t
            inferred += 1
    expected = target * secs
    assert abs(inferred - expected) <= 1, f"{target}fps from {stream}fps: got {inferred}, want ~{expected}"


def test_boundary_aligned_timestamps_do_not_lose_inferences():
    """Regression: without a boundary tolerance, timestamps landing exactly on an interval
    boundary failed the comparison by ~5e-17 and the pipeline ran measurably below the
    requested rate (10 fps from a 30 fps stream produced 25 inferences instead of 30)."""
    p = _p(10.0)
    n = sum(1 for i in range(90) if _due_and_mark(p, i / 30.0))   # 3 s of a 30 fps camera
    assert n == 30, f"boundary drift lost inferences: {n} instead of 30"


def test_target_above_stream_rate_infers_every_frame():
    """Asking for more than the camera delivers must not skip anything."""
    p = _p(60.0)
    assert all(_due_and_mark(p, i / 25.0) for i in range(50))


def _due_and_mark(p, t):
    ok = p._due_for_inference(t)
    if ok:
        p._last_inference_at = t
    return ok


# ------------------------------------------------------------------ no bursts, no backlog
def test_first_frame_after_start_is_always_inferred():
    """The monotonic clock counts from boot, so an early first frame must not be gated out
    by an initial timestamp of 0."""
    for target in (1.0, 5.0, 25.0):
        p = _p(target)
        assert p._due_for_inference(0.0), f"first frame skipped at {target} fps"
        assert p._due_for_inference(0.05), "gate must not consume the first-frame allowance on a read-only check"


def test_a_stall_does_not_cause_a_catch_up_burst():
    """After a long gap the gate must admit ONE frame, not one per missed interval.

    `_last_inference_at` is set to the real inference time rather than advanced by a fixed
    step, so a 10-second stall cannot be followed by 50 rapid-fire inferences.
    """
    p = _p(5.0)
    assert _due_and_mark(p, 0.0)
    # 10 s stall, then a full second of frames arriving normally at 25 fps.
    # A catch-up scheduler would fire ~50 times here (one per missed 0.2 s slot); the
    # correct behaviour is to simply resume at the target rate.
    admitted = [t for t in (10.0 + i / 25.0 for i in range(25)) if _due_and_mark(p, t)]
    assert len(admitted) <= 6, f"stall produced a burst of {len(admitted)} inferences"
    assert len(admitted) >= 4, f"rate did not resume after the stall ({len(admitted)})"
    # and the very first frame after the stall is taken immediately, not delayed
    assert admitted[0] == 10.0


def test_gate_is_stateless_about_skipped_frames():
    """Skipping must not accumulate anything - no queue, no pending list."""
    p = _p(5.0)
    before = dict(p.__dict__)
    for i in range(100):
        p._due_for_inference(i / 25.0)
    after = dict(p.__dict__)
    assert before.keys() == after.keys()
    assert after["_last_inference_at"] == before["_last_inference_at"], "gate mutated state on a read-only check"


# ------------------------------------------------------------------ per-pipeline override
def test_detection_config_override_is_validated():
    p = InferencePipeline.__new__(InferencePipeline)
    for attr, val in (("MIN_CONFIDENCE", 0.4), ("MIN_CONFIDENCE_FOR_PERSON", 0.5),
                      ("IMMEDIATE_SEND_CONFIDENCE", 0.9), ("SEND_BUFFER_SECONDS", 1.0),
                      ("MAX_COLLECT_SECONDS", 3.0), ("TRACK_TTL_SECONDS", 120.0),
                      ("PERSON_CAPTURE_COUNT", 3), ("PERSON_CAPTURE_INTERVAL_SECONDS", 2.0),
                      ("TRACK_LOST_TIMEOUT_SECONDS", 2.0), ("PUBLISH_MAX_RETRIES", 5),
                      ("PUBLISH_RETRY_DELAY_SECONDS", 1.0), ("PUBLISH_RETRY_BACKOFF", 2.0),
                      ("PUBLISHER_SHUTDOWN_TIMEOUT_SECONDS", 5.0), ("PUBLISH_QUEUE_SIZE", 1000),
                      ("TARGET_INFERENCE_FPS", 0.0)):
        setattr(p, attr, val)
    import logging
    p.logger = logging.getLogger("t")

    p._apply_detection_config({"target_inference_fps": 5})
    assert p.TARGET_INFERENCE_FPS == 5.0

    for bad in ({"target_inference_fps": -1}, {"target_inference_fps": 5000},
                {"target_inference_fps": "fast"}):
        p._apply_detection_config(bad)
        assert p.TARGET_INFERENCE_FPS == 5.0, f"{bad} should have been rejected, keeping 5.0"


def test_the_gate_is_actually_wired_into_the_run_loop():
    """A correct gate that nothing calls would pass every test above."""
    src = open(os.path.join(REPO, "InferenceNode", "pipeline.py"), encoding="utf-8").read()
    i = src.index("results = None")
    assert "_due_for_inference(now_perf)" in src[i:i + 900]
    # Step 9 replaced the direct assignment with a drift-free scheduler; the anchor must
    # still be recorded on every inference, just without compounding per-cycle overhead.
    assert "self._mark_inferred(now_perf)" in src[i:i + 900]
    # the frame must still be READ every iteration - gating happens after the read
    assert src.index("self.source.read()") < i, "inference gate must come after the frame read"
