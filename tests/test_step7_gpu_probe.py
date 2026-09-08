"""Step 7 - the node must be able to report the resource that limits it.

Before this, a host with an RTX 5090 reported
    {"available": False, "message": "No GPU detection method available"}
because of two bugs that hid each other:

  1. `telemetry.py` did `import nvidia_ml_py`, but the `nvidia-ml-py` distribution
     installs its module as `pynvml`. That branch could never run.
  2. the `pynvml` fallback called `.decode('utf-8')` on `nvmlDeviceGetName()`, which
     returns `str` since nvidia-ml-py 11.5. The AttributeError was swallowed by a broad
     `except Exception` into a generic path that shells out to `lspci` - not installed
     in the container.

Both are pinned here as regressions, with fakes rather than a real GPU so the suite runs
on CPU-only machines.

Units are the other trap: telemetry/benchmark read `memory_*_gb` (float GB) while the
hardware API and its UI read `memory_total` (bytes, rendered by formatBytes). Both are
asserted, because a silent unit swap here is invisible until someone reads a dashboard.
"""
import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode import gpu_probe  # noqa: E402

GB = 1024 ** 3


# ------------------------------------------------------------------ fakes
class _Mem:
    def __init__(self, total, used):
        self.total, self.used, self.free = total, used, total - used


class _Util:
    def __init__(self, gpu, mem):
        self.gpu, self.memory = gpu, mem


def _fake_nvml(names, *, total=32 * GB, used=14 * GB, util=7, driver="595.84",
               util_raises=False):
    """A stand-in NVML. `names` may be str (modern) or bytes (pre-11.5)."""
    m = types.SimpleNamespace()
    m.NVML_TEMPERATURE_GPU = 0
    m.nvmlInit = lambda: None
    m.nvmlDeviceGetCount = lambda: len(names)
    m.nvmlDeviceGetHandleByIndex = lambda i: i
    m.nvmlDeviceGetName = lambda h: names[h]
    m.nvmlDeviceGetMemoryInfo = lambda h: _Mem(total, used)
    m.nvmlSystemGetDriverVersion = lambda: driver

    def _util(h):
        if util_raises:
            raise RuntimeError("not supported on this device")
        return _Util(util, 3)
    m.nvmlDeviceGetUtilizationRates = _util
    m.nvmlDeviceGetTemperature = lambda h, s: 61
    m.nvmlDeviceGetPowerUsage = lambda h: 123456        # mW
    return m


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    """The module caches its NVML handle for the process lifetime."""
    gpu_probe._nvml_module = None
    gpu_probe._nvml_failed = False
    yield
    gpu_probe._nvml_module = None
    gpu_probe._nvml_failed = False


def _install(monkeypatch, nvml, available_names=("pynvml",)):
    import importlib
    real = importlib.import_module

    def fake_import(name, *a, **kw):
        if name in ("pynvml", "nvidia_ml_py"):
            if name in available_names:
                return nvml
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real(name, *a, **kw)
    monkeypatch.setattr(importlib, "import_module", fake_import)


# ------------------------------------------------------------------ the two original bugs
def test_it_finds_nvml_under_the_module_name_that_is_actually_installed(monkeypatch):
    """Regression: `nvidia-ml-py` installs `pynvml`. Importing `nvidia_ml_py` always failed."""
    _install(monkeypatch, _fake_nvml(["NVIDIA GeForce RTX 5090"]), available_names=("pynvml",))
    info = gpu_probe.probe()
    assert info["available"] is True
    assert info["source"] == "nvml"
    assert info["devices"][0]["name"] == "NVIDIA GeForce RTX 5090"


def test_a_str_device_name_does_not_break_the_probe(monkeypatch):
    """Regression: `.decode('utf-8')` on a `str` raised AttributeError, which was
    swallowed into the lspci path and reported as 'no GPU'."""
    _install(monkeypatch, _fake_nvml(["NVIDIA GeForce RTX 5090"]))
    assert gpu_probe.probe()["devices"][0]["name"] == "NVIDIA GeForce RTX 5090"


def test_a_bytes_device_name_still_works(monkeypatch):
    """Older NVML returns bytes; the fix must accept both, not swap one bug for another."""
    _install(monkeypatch, _fake_nvml([b"NVIDIA GeForce RTX 5090"]))
    assert gpu_probe.probe()["devices"][0]["name"] == "NVIDIA GeForce RTX 5090"


# ------------------------------------------------------------------ what it reports
def test_it_reports_real_memory_in_both_unit_conventions(monkeypatch):
    _install(monkeypatch, _fake_nvml(["RTX 5090"], total=32 * GB, used=14 * GB))
    d = gpu_probe.probe()["devices"][0]
    assert d["memory_total_gb"] == 32.0 and d["memory_used_gb"] == 14.0
    assert d["memory_free_gb"] == 18.0
    assert d["memory_total_bytes"] == 32 * GB
    assert d["memory_used_bytes"] == 14 * GB
    assert d["memory_free_bytes"] == 18 * GB


def test_used_memory_is_device_wide_not_this_process(monkeypatch):
    """A co-tenant on the same card (the FACE stack's ollama pins ~14 GiB) must be
    counted - that is the number capacity planning needs."""
    _install(monkeypatch, _fake_nvml(["RTX 5090"], total=32 * GB, used=14 * GB))
    d = gpu_probe.probe()["devices"][0]
    assert d["memory_used_bytes"] == 14 * GB, "co-tenant usage must not be filtered out"


def test_it_reports_utilisation_temperature_power_and_driver(monkeypatch):
    _install(monkeypatch, _fake_nvml(["RTX 5090"], util=7, driver="595.84"))
    info = gpu_probe.probe()
    d = info["devices"][0]
    assert d["gpu_utilization_percent"] == 7
    assert d["temperature_c"] == 61
    assert d["power_usage_w"] == 123.5
    assert info["driver_version"] == "595.84"


def test_zero_percent_utilisation_is_reported_not_dropped(monkeypatch):
    """An idle GPU reads 0%, which is falsy - the old code's truthiness test would
    have hidden exactly the reading that proves the GPU is idle."""
    _install(monkeypatch, _fake_nvml(["RTX 5090"], util=0))
    assert gpu_probe.probe()["devices"][0]["gpu_utilization_percent"] == 0


def test_multi_gpu_devices_are_enumerated_and_indexed(monkeypatch):
    _install(monkeypatch, _fake_nvml(["RTX 5090", "RTX 4090"]))
    devs = gpu_probe.probe()["devices"]
    assert [d["id"] for d in devs] == [0, 1]
    assert [d["name"] for d in devs] == ["RTX 5090", "RTX 4090"]


def test_an_unsupported_metric_does_not_lose_the_device(monkeypatch):
    """Memory is the point; utilisation is a bonus that some devices refuse."""
    _install(monkeypatch, _fake_nvml(["RTX 5090"], util_raises=True))
    d = gpu_probe.probe()["devices"][0]
    assert d["memory_total_bytes"] == 32 * GB
    assert "gpu_utilization_percent" not in d


def test_total_memory_by_index_helper(monkeypatch):
    _install(monkeypatch, _fake_nvml(["a", "b"], total=8 * GB))
    assert gpu_probe.total_memory_bytes_by_index() == {0: 8 * GB, 1: 8 * GB}


# ------------------------------------------------------------------ absence is explicit
def test_no_gpu_reports_unavailable_rather_than_raising(monkeypatch):
    _install(monkeypatch, None, available_names=())
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False)))
    info = gpu_probe.probe()
    assert info["available"] is False and info["devices"] == []
    assert "message" in info


def test_nvml_present_but_no_devices_is_not_available(monkeypatch):
    _install(monkeypatch, _fake_nvml([]))
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False)))
    assert gpu_probe.probe()["available"] is False


def test_torch_is_the_fallback_when_nvml_is_missing(monkeypatch):
    _install(monkeypatch, None, available_names=())
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        cuda=types.SimpleNamespace(
            is_available=lambda: True,
            device_count=lambda: 1,
            mem_get_info=lambda i: (18 * GB, 32 * GB),
            get_device_name=lambda i: "NVIDIA GeForce RTX 5090")))
    info = gpu_probe.probe()
    assert info["available"] is True and info["source"] == "torch"
    d = info["devices"][0]
    assert d["memory_total_bytes"] == 32 * GB and d["memory_used_bytes"] == 14 * GB


def test_nvml_is_preferred_over_torch(monkeypatch):
    """torch.mem_get_info allocates a CUDA context on every device it touches; NVML
    observes without allocating, so it must win when both are present."""
    _install(monkeypatch, _fake_nvml(["RTX 5090"]))
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        cuda=types.SimpleNamespace(
            is_available=lambda: True, device_count=lambda: 1,
            mem_get_info=lambda i: (1, 2), get_device_name=lambda i: "wrong")))
    assert gpu_probe.probe()["source"] == "nvml"


def test_a_broken_nvml_does_not_propagate(monkeypatch):
    """Telemetry must never be able to take down the node it reports on."""
    bad = types.SimpleNamespace(nvmlInit=lambda: (_ for _ in ()).throw(RuntimeError("driver")))
    _install(monkeypatch, bad)
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False)))
    assert gpu_probe.probe()["available"] is False


def test_the_import_is_attempted_once_not_on_every_poll(monkeypatch):
    """Telemetry is polled continuously; a failed import must not be retried each time."""
    import importlib
    real = importlib.import_module
    calls = {"n": 0}

    def counting(name, *a, **kw):
        if name in ("pynvml", "nvidia_ml_py"):
            calls["n"] += 1
            raise ModuleNotFoundError(name)
        return real(name, *a, **kw)
    monkeypatch.setattr(importlib, "import_module", counting)
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False)))

    for _ in range(5):
        gpu_probe.probe()
    assert calls["n"] <= 2, f"import retried {calls['n']} times; it must be cached"


# ------------------------------------------------------------------ the call sites
def test_telemetry_uses_the_shared_probe_and_not_its_own_nvml():
    src = open(os.path.join(REPO, "InferenceNode", "telemetry.py"), encoding="utf-8").read()
    i = src.index("def _get_gpu_info")
    body = src[i:src.index("def _get_generic_gpu_info")]
    assert "gpu_probe" in body
    assert "import nvidia_ml_py" not in body, "the wrong module name must not come back"
    assert ".decode('utf-8')" not in body, "the str/bytes bug must not come back"


def test_telemetry_keeps_the_generic_path_for_non_nvidia_hosts():
    src = open(os.path.join(REPO, "InferenceNode", "telemetry.py"), encoding="utf-8").read()
    i = src.index("def _get_gpu_info")
    assert "_get_generic_gpu_info()" in src[i:src.index("def _get_generic_gpu_info")]


def test_hardware_details_no_longer_hardcode_zero_for_a_detected_nvidia_gpu():
    src = open(os.path.join(REPO, "InferenceNode", "hardware_detector.py"), encoding="utf-8").read()
    i = src.index("def get_gpu_details")
    body = src[i:i + 3000]
    assert "_probe_nvidia_gpus()" in body, "NVIDIA details must come from a live probe"


def test_hardware_details_report_bytes_not_gigabytes(monkeypatch):
    """`node_info.html` renders this with formatBytes(); GB here would show ~32 bytes."""
    from InferenceNode.hardware_detector import HardwareDetector
    _install(monkeypatch, _fake_nvml(["RTX 5090"], total=32 * GB, used=14 * GB))

    d = HardwareDetector.__new__(HardwareDetector)
    rows = HardwareDetector._probe_nvidia_gpus(d)
    assert rows and rows[0]["memory_total"] == 32 * GB
    assert rows[0]["name"] == "RTX 5090"
    assert rows[0]["type"] == "NVIDIA"
    assert rows[0]["driver_version"] == "595.84"


def test_hardware_probe_returns_empty_when_there_is_no_gpu(monkeypatch):
    """So the caller falls back instead of printing zeroes as if they were measured."""
    from InferenceNode.hardware_detector import HardwareDetector
    _install(monkeypatch, None, available_names=())
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False)))
    d = HardwareDetector.__new__(HardwareDetector)
    assert HardwareDetector._probe_nvidia_gpus(d) == []


def test_a_boolean_gpu_flag_keeps_the_real_name_from_nvidia_smi(monkeypatch):
    """When NVML is unavailable the detector already knows the model from `nvidia-smi -L`;
    flattening it to the placeholder 'NVIDIA GPU' threw that away."""
    from InferenceNode.hardware_detector import HardwareDetector
    d = HardwareDetector.__new__(HardwareDetector)
    d.hardware_info = {"nvidia": {"gpu": True, "gpu_details": {
        "0": {"name": "NVIDIA GeForce RTX 5090"}}}}
    monkeypatch.setattr(HardwareDetector, "_probe_nvidia_gpus", lambda self: [])

    rows = [r for r in d.get_gpu_details() if r["type"] == "NVIDIA"]
    assert [r["name"] for r in rows] == ["NVIDIA GeForce RTX 5090"]


# ------------------------------------------------------------------ the dashboard
def test_node_info_renders_used_and_free_not_just_total():
    """A VRAM panel that shows only capacity cannot answer "will another camera fit?"."""
    s = open(os.path.join(REPO, "InferenceNode", "templates", "node_info.html"),
             encoding="utf-8").read()
    i = s.index("function updateGPUInfo")
    body = s[i:i + 1600]
    assert "gpu.memory_used" in body and "gpu.memory_free" in body
    assert "gpu.utilization_percent" in body


def test_node_info_does_not_print_zero_as_if_it_were_measured():
    """Intel/AMD rows carry no memory reading; `formatBytes(0)` would render "0 Bytes",
    which looks like a measurement of an empty GPU rather than an absent one."""
    s = open(os.path.join(REPO, "InferenceNode", "templates", "node_info.html"),
             encoding="utf-8").read()
    i = s.index("function updateGPUInfo")
    body = s[i:i + 1600]
    assert "formatBytes(gpu.memory_total || 0)" not in body, "0 must not be formatted as a size"
    assert "'Unknown'" in body
    # 0% utilisation and 0 bytes used are real readings, so the guard must be a
    # finiteness test, not truthiness.
    assert "Number.isFinite(gpu.utilization_percent)" in body
    assert "Number.isFinite(gpu.memory_used)" in body


# ------------------------------------------------------------------ the end-to-end contract
def test_probe_emits_every_key_the_benchmark_reads(monkeypatch):
    """The chain is:
        gpu_probe.probe() -> telemetry._get_gpu_info() -> system_info["gpu"]
          -> GET /api/telemetry ["gpu"] -> benchmark.sample() -> the GPU util / VRAM columns

    Nothing asserted that contract, which is how a broken probe stayed invisible: the
    benchmark just printed "n/a" and nobody could tell "idle GPU" from "no telemetry".
    These are the exact keys scripts/benchmark.py reads off each device.
    """
    _install(monkeypatch, _fake_nvml(["RTX 5090"]))
    d = gpu_probe.probe()["devices"][0]
    for key in ("id", "name", "gpu_utilization_percent", "memory_used_gb", "memory_total_gb"):
        assert key in d, f"scripts/benchmark.py reads {key!r}; the GPU columns go n/a without it"


def test_the_telemetry_endpoint_still_exposes_the_structured_gpu_payload():
    """`system.gpu_info` is a stringified copy for the old UI; `gpu` is the parseable one."""
    src = open(os.path.join(REPO, "InferenceNode", "inference_node.py"), encoding="utf-8").read()
    i = src.index("def get_telemetry_data")
    body = src[i:i + 6000]
    assert "'gpu': system_info.get('gpu', {})" in body, \
        "the per-device GPU payload must stay at the top level for tooling"
