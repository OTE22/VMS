"""Step 0 — benchmark instrumentation and harness statistics.

Two contracts are pinned here:

1. **The runtime exposes PRIMITIVES only.** `get_metrics()` returns raw counters and
   timestamps; it must not compute percentiles, rates or rollups. Keeping statistics out of
   the hot path is the point of Step 0, and a future edit that "helpfully" adds a p95 to the
   pipeline should fail this suite.
2. **All statistics live in `scripts/benchmark.py`** and are correct.

No production behaviour changes in Step 0, so these tests also assert the instrumentation is
inert: counters start at zero and the device probe never raises, whatever the engine is.
"""
import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_benchmark():
    """Import scripts/benchmark.py by path (it is a script, not a package module)."""
    path = os.path.join(REPO, "scripts", "benchmark.py")
    spec = importlib.util.spec_from_file_location("armyeye_benchmark", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bench = _load_benchmark()


# ------------------------------------------------------------------ statistics live in the harness
@pytest.mark.parametrize("values,q,expected", [
    ([10], 50, 10),
    ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50, 5),
    ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95, 10),
    ([5] * 100, 99, 5),
])
def test_percentile_is_computed_in_the_harness(values, q, expected):
    assert bench.pct(values, q) == expected


def test_percentile_handles_empty_series():
    assert bench.pct([], 95) == 0.0


def test_rate_is_counter_delta_over_wall_time():
    first = {"t": 100.0, "frame_count": 0}
    last = {"t": 110.0, "frame_count": 250}
    assert bench.rate(first, last, "frame_count") == pytest.approx(25.0)


def test_rate_is_zero_for_degenerate_or_missing_input():
    assert bench.rate(None, {"t": 1, "frame_count": 5}, "frame_count") == 0.0
    assert bench.rate({"t": 5.0, "frame_count": 0}, {"t": 5.0, "frame_count": 9}, "frame_count") == 0.0
    # a counter reset must never produce a negative rate
    assert bench.rate({"t": 1.0, "frame_count": 900}, {"t": 2.0, "frame_count": 0}, "frame_count") == 0.0


def _sample(t, age_ms, frames, infers, *, cpu=10.0, threads=20, failed=0):
    return {"t": t, "thread_count": threads, "cpu": cpu, "ram": 30.0, "gpus": [],
            "pipelines": {"p1": {"frame_count": frames, "inference_count": infers,
                                 "latency_ms": 12.0, "read_wait_ms": 40.0,
                                 "failed_read_count": failed, "effective_device": "cpu",
                                 "frame_age_ms": age_ms}}}


def test_summarize_derives_capture_and_inference_rates_separately():
    """The whole point of Step 3 is that these two diverge; Step 0 must measure them apart."""
    samples = [_sample(0.0, 100, 0, 0), _sample(10.0, 100, 250, 50)]
    out = bench.summarize(samples, n_requested=1, n_started=1, duration=10)
    assert out["capture_fps_per_cam"] == pytest.approx(25.0)
    assert out["ai_fps_per_cam"] == pytest.approx(5.0)
    assert out["aggregate_ai_fps"] == pytest.approx(5.0)
    assert out["devices"] == "cpu"


def test_growing_frame_age_is_reported_unstable_even_with_healthy_fps():
    """A pipeline can report perfect FPS while draining a backlog. That is a FAIL."""
    samples = ([_sample(float(i), 50, i * 25, i * 5) for i in range(5)] +
               [_sample(float(i), 40000, i * 25, i * 5) for i in range(5, 10)])
    out = bench.summarize(samples, n_requested=1, n_started=1, duration=10)
    assert out["ai_fps_per_cam"] > 0
    assert out["stable"] == "no (frame age growing)"


def test_steady_frame_age_is_reported_stable():
    samples = [_sample(float(i), 60, i * 25, i * 5) for i in range(10)]
    out = bench.summarize(samples, n_requested=1, n_started=1, duration=10)
    assert out["stable"] == "yes"
    assert out["frame_age_p95"] == pytest.approx(60)


def test_died_pipelines_are_counted_as_failures():
    samples = [_sample(0.0, 50, 0, 0), _sample(10.0, 50, 250, 50)]
    out = bench.summarize(samples, n_requested=5, n_started=5, duration=10)
    assert out["failures"] == 4 and out["stable"] == "no (pipelines died)"


def test_failed_reads_are_summed_as_a_delta():
    samples = [_sample(0.0, 50, 0, 0, failed=7), _sample(10.0, 50, 250, 50, failed=19)]
    assert bench.summarize(samples, 1, 1, 10)["drops"] == 12


def test_per_gpu_metrics_are_kept_separate_not_averaged():
    """A box where every pipeline lands on cuda:0 looks fine in an aggregate average."""
    def s(t):
        return {"t": t, "thread_count": 10, "cpu": 5, "ram": 5,
                "gpus": [{"id": 0, "name": "A", "util": 90, "mem_used_gb": 8.0, "mem_total_gb": 24.0},
                         {"id": 1, "name": "B", "util": 0, "mem_used_gb": 0.2, "mem_total_gb": 24.0}],
                "pipelines": {"p1": {"frame_count": int(t * 25), "inference_count": int(t * 5),
                                     "latency_ms": 1, "read_wait_ms": 1, "failed_read_count": 0,
                                     "effective_device": "cuda:0", "frame_age_ms": 10}}}
    out = bench.summarize([s(0.0), s(10.0)], 1, 1, 10)
    assert out["per_gpu"][0]["util_mean"] == 90 and out["per_gpu"][1]["util_mean"] == 0
    assert "gpu0:90%" in bench.gpu_cell(out, "util") and "gpu1:0%" in bench.gpu_cell(out, "util")


def test_summarize_survives_no_data_with_the_full_key_set():
    """A level where nothing started must still render: one failed level must not destroy
    the report for the levels that worked (it did, before the harness was hardened)."""
    out = bench.summarize([], 5, 0, 10)
    assert out["stable"].startswith("NO DATA")
    assert out["failures"] == 5 and out["capture_fps_per_cam"] == 0.0
    good = bench.summarize([_sample(0.0, 50, 0, 0), _sample(10.0, 50, 250, 50)], 1, 1, 10)
    assert set(out) == set(good), "no-data level must expose the same keys as a good one"


# ------------------------------------------------------------------ runtime exposes primitives only
def test_pipeline_exposes_the_four_primitives_and_no_statistics():
    src = open(os.path.join(REPO, "InferenceNode", "pipeline.py"), encoding="utf-8").read()
    start = src.index("def get_metrics(self)")
    body = src[start:src.index("def _calculate_rolling_fps", start)]
    for key in ("capture_timestamp", "read_wait_ms", "failed_read_count", "effective_device"):
        assert f'"{key}"' in body, f"{key} not exposed by get_metrics"
    # statistics must NOT be computed in the runtime
    for banned in ("percentile", "p95", "p99", "statistics."):
        assert banned not in body, f"statistic {banned!r} leaked into the production hot path"


def test_instrumentation_is_inert_on_a_fresh_pipeline():
    from InferenceNode.pipeline import InferencePipeline
    p = InferencePipeline()
    assert p._failed_read_count == 0
    assert p._last_capture_wall == 0.0
    assert p._last_read_wait_ms == 0.0
    # no engine configured yet - the probe must return None, never raise
    assert p.get_effective_device() is None
    m = p.get_metrics()
    assert m["failed_read_count"] == 0 and m["capture_timestamp"] == 0.0
    assert m["effective_device"] is None
    assert m["frame_count"] == 0 and m["inference_count"] == 0


def test_effective_device_prefers_the_loaded_model_over_configuration():
    """Step 1 is proven by what the model ACTUALLY loaded on, not what was requested."""
    from InferenceNode.pipeline import InferencePipeline

    class _Model:
        device = "cuda:1"

    class _Engine:
        device = "GPU"          # what was configured
        model = _Model()        # what actually happened

    p = InferencePipeline()
    p.inference_engine = _Engine()
    assert p.get_effective_device() == "cuda:1"


def test_effective_device_falls_back_and_flags_openvino():
    from InferenceNode.pipeline import InferencePipeline

    class _Engine:
        device = "intel:gpu"
        use_openvino = True
        model = None

    p = InferencePipeline()
    p.inference_engine = _Engine()
    assert p.get_effective_device() == "openvino:intel:gpu"


def test_effective_device_never_raises_on_a_hostile_engine():
    from InferenceNode.pipeline import InferencePipeline

    class _Engine:
        @property
        def model(self):
            raise RuntimeError("boom")

        @property
        def device(self):
            raise RuntimeError("boom")

    p = InferencePipeline()
    p.inference_engine = _Engine()
    assert p.get_effective_device() is None      # instrumentation must never break a pipeline


def test_metrics_endpoint_forwards_primitives_and_thread_count():
    src = open(os.path.join(REPO, "InferenceNode", "inference_node.py"), encoding="utf-8").read()
    i = src.index("def get_pipeline_metrics()")
    body = src[i:i + 4200]
    for key in ("inference_count", "capture_timestamp", "read_wait_ms",
                "failed_read_count", "effective_device", "thread_count", "sampled_at"):
        assert key in body, f"{key} not forwarded by /api/pipelines/metrics"


def test_telemetry_endpoint_exposes_structured_per_gpu_payload():
    """system.gpu_info is a stringified copy for the UI; tooling needs the parseable one."""
    src = open(os.path.join(REPO, "InferenceNode", "inference_node.py"), encoding="utf-8").read()
    i = src.index("telemetry_data = {")
    body = src[i:i + 2000]
    assert "'gpu': system_info.get('gpu', {})" in body


# ------------------------------------------------------------------ harness safety
def test_harness_only_ever_touches_its_own_pipelines():
    """It must never stop or delete a pipeline it did not create."""
    src = open(os.path.join(REPO, "scripts", "benchmark.py"), encoding="utf-8").read()
    assert 'BENCH_PREFIX = "BENCH_"' in src
    assert "startswith(BENCH_PREFIX)" in src, "leftover scan must filter by the bench prefix"
    assert "--force-clean" in src


def test_harness_uses_capture_type_not_type():
    """pipeline_manager reads `capture_type`; sending `type` silently falls back to webcam."""
    src = open(os.path.join(REPO, "scripts", "benchmark.py"), encoding="utf-8").read()
    i = src.index("def make_frame_source")
    body = src[i:i + 700]
    assert '"capture_type": "video_file"' in body and '"capture_type": "ip_camera"' in body


def test_file_mode_report_states_it_is_not_production_readiness():
    src = open(os.path.join(REPO, "scripts", "benchmark.py"), encoding="utf-8").read()
    assert "NOT" in src and "production readiness" in src


def test_harness_samples_only_its_own_pipelines():
    """A pipeline an operator starts in the UI mid-run must not enter the measurement:
    its device, latency and frame age would silently contaminate the report."""
    src = open(os.path.join(REPO, "scripts", "benchmark.py"), encoding="utf-8").read()
    i = src.index("def sample(")
    body = src[i:i + 1200]
    assert "own_ids" in body and "if own_ids is not None and pid not in own_ids" in body
    assert "own_ids=set(ids)" in src, "the run loop must pass its own ids to sample()"


def test_device_column_reports_steady_state_not_a_union():
    """Unioning every device seen across a run turns one transient reading into a
    permanently ambiguous cell (`CPU,cpu`). Steady state is the characterisation;
    anything else is reported separately as a transition."""
    samples = [
        {"t": 0.0, "thread_count": 5, "cpu": 1, "ram": 1, "gpus": [],
         "pipelines": {"p1": {"frame_count": 0, "inference_count": 0, "latency_ms": 1,
                              "read_wait_ms": 1, "failed_read_count": 0,
                              "effective_device": "CPU", "frame_age_ms": 10}}},
        {"t": 10.0, "thread_count": 5, "cpu": 1, "ram": 1, "gpus": [],
         "pipelines": {"p1": {"frame_count": 250, "inference_count": 50, "latency_ms": 1,
                              "read_wait_ms": 1, "failed_read_count": 0,
                              "effective_device": "cpu", "frame_age_ms": 10}}},
    ]
    out = bench.summarize(samples, 1, 1, 10)
    assert out["devices"] == "cpu", "steady-state device only"
    assert out["device_transitions"] == "CPU", "the transient value must still be surfaced"
