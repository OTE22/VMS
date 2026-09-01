"""Step 1 — CUDA/device selection correctness.

The defect, measured in the Step 0 baseline on a machine with no NVIDIA GPU: requesting
`cpu` produced an effective device of `openvino:INTEL:CPU`. Every `CPU`/`GPU`/`NPU` was
rewritten to `intel:*` and silently switched the engine to OpenVINO, because the
Intel-hardware check was commented out. On an NVIDIA box whose vendor detection failed,
`GPU` therefore ran Intel OpenVINO and CUDA was never touched.

The contract now:
  * explicit devices are honoured verbatim  - `cpu` is CPU, `cuda:1` is CUDA 1
  * OpenVINO only when explicitly asked for  - `intel:*`
  * ambiguous aliases (`GPU`, `NPU`) resolve against hardware, CUDA first
  * a CUDA request that cannot be satisfied FAILS LOUDLY - never a silent CPU fallback

Four of the five acceptance criteria are verifiable without a GPU and are covered here.
The fifth - "CUDA actually engages and GPU utilization rises" - requires NVIDIA hardware
and is tracked as UNVERIFIED in DOCS/STEP0_GPU_BASELINE.md.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceEngine.engines.ultralytics_engine import UltralyticsEngine  # noqa: E402
from InferenceNode.hardware_detector import HardwareDetector              # noqa: E402


def _engine(monkeypatch, cuda: bool):
    """An engine whose device resolution is exercised without loading any model."""
    monkeypatch.setattr(UltralyticsEngine, "_cuda_available", staticmethod(lambda: cuda))
    e = UltralyticsEngine.__new__(UltralyticsEngine)
    e.use_openvino = False
    return e


# ------------------------------------------------------------------ criterion 1: cpu means cpu
@pytest.mark.parametrize("requested", ["cpu", "CPU", " Cpu "])
def test_requesting_cpu_gives_plain_cpu_not_openvino(monkeypatch, requested):
    """The exact regression the Step 0 baseline caught."""
    e = _engine(monkeypatch, cuda=False)
    assert e._resolve_device(requested) == "cpu"
    assert e.use_openvino is False


def test_hardware_detector_no_longer_upgrades_cpu_to_intel():
    hd = HardwareDetector()
    assert hd.optimize_device_string("CPU") == "cpu"
    assert hd.format_for("ultralytics", "CPU") == "cpu"


# ------------------------------------------------------------------ criterion 2: explicit cuda honoured
@pytest.mark.parametrize("requested,expected", [
    ("cuda", "cuda"), ("cuda:0", "cuda:0"), ("cuda:1", "cuda:1"),
    ("CUDA:3", "cuda:3"), ("0", "0"), ("1", "1"),
])
def test_explicit_cuda_devices_pass_through_untouched(monkeypatch, requested, expected):
    e = _engine(monkeypatch, cuda=True)
    assert e._resolve_device(requested) == expected
    assert e.use_openvino is False


def test_explicit_cuda_is_not_rerouted_even_when_cuda_is_absent(monkeypatch):
    """Resolution must not second-guess an explicit request; the loud failure happens at
    pipeline start (criterion 4), not by silently rewriting the string here."""
    e = _engine(monkeypatch, cuda=False)
    assert e._resolve_device("cuda:0") == "cuda:0"
    assert e.use_openvino is False


def test_multi_gpu_index_is_preserved():
    hd = HardwareDetector()
    assert hd.optimize_device_string("GPU.1").endswith("1")


# ------------------------------------------------------------------ criterion 3: openvino is opt-in
@pytest.mark.parametrize("requested", ["intel:cpu", "intel:gpu", "INTEL:GPU", "intel:npu"])
def test_openvino_is_selected_only_when_explicitly_requested(monkeypatch, requested):
    e = _engine(monkeypatch, cuda=True)          # even with CUDA present, honour the request
    assert e._resolve_device(requested) == requested.lower()
    assert e.use_openvino is True


def test_ambiguous_gpu_prefers_cuda_when_available(monkeypatch):
    e = _engine(monkeypatch, cuda=True)
    assert e._resolve_device("GPU") == "cuda"
    assert e.use_openvino is False


def test_ambiguous_gpu_falls_back_to_intel_only_without_cuda(monkeypatch):
    e = _engine(monkeypatch, cuda=False)
    assert e._resolve_device("GPU") == "intel:gpu"
    assert e.use_openvino is True


def test_gpu_resolution_trusts_torch_over_string_scraped_detection(monkeypatch):
    """`_detect_nvidia_hardware` parses nvidia-smi/lspci/PowerShell output and can fail
    while CUDA works. torch is the second, independent signal - a detection miss must not
    silently route an NVIDIA box to Intel OpenVINO.

    monkeypatch (not a manual set/del) so the real classmethod is restored afterwards -
    deleting it outright would remove the only definition and poison later tests."""
    hd = HardwareDetector()
    monkeypatch.setitem(hd.hardware_info["nvidia"], "gpu", False)   # failed vendor probe
    monkeypatch.setattr(HardwareDetector, "_torch_cuda_available", staticmethod(lambda: True))
    assert hd.optimize_device_string("GPU") == "cuda"


# ------------------------------------------------------------------ criterion 4: fail loudly
def test_cuda_request_without_cuda_raises_instead_of_falling_back_to_cpu():
    """This machine has no CUDA, which makes it the correct place to test this."""
    import torch
    if torch.cuda.is_available():
        pytest.skip("host has CUDA; the silent-fallback path cannot be exercised here")

    src = open(os.path.join(REPO, "InferenceNode", "pipeline_manager.py"), encoding="utf-8").read()
    i = src.index("# A CUDA request that cannot be satisfied FAILS LOUDLY")
    body = src[i:i + 1200]
    assert "raise RuntimeError(" in body
    assert "device = 'cpu'" not in body, "silent CPU fallback is back"
    assert "Refusing to start" in body


def test_cuda_validation_covers_every_cuda_spelling():
    """The old guard matched a fixed list and missed 'cuda:1' entirely - so a request for
    a second GPU on a CPU-only node fell through unvalidated."""
    src = open(os.path.join(REPO, "InferenceNode", "pipeline_manager.py"), encoding="utf-8").read()
    i = src.index("# A CUDA request that cannot be satisfied FAILS LOUDLY")
    body = src[i:i + 600]
    assert "startswith(('cuda', 'nvidia:'))" in body
    assert "isdigit()" in body


# ------------------------------------------------------------------ no silent OpenVINO anywhere
def test_the_commented_out_intel_check_is_gone():
    """The root cause: a mapping table whose hardware condition was commented out."""
    src = open(os.path.join(REPO, "InferenceEngine", "engines", "ultralytics_engine.py"),
               encoding="utf-8").read()
    assert "'CPU': 'intel:cpu'," not in src
    assert "# if intel_hw[" not in src, "the commented-out hardware check is back"
    assert "_optimize_device_for_intel" not in src, "misleading name still present"


def test_engine_exposes_the_resolved_device_for_measurement():
    """Step 0's effective-device probe is what proves this step on real hardware."""
    from InferenceNode.pipeline import InferencePipeline

    class _Engine:
        device = "cuda:0"
        use_openvino = False
        model = None

    p = InferencePipeline()
    p.inference_engine = _Engine()
    assert p.get_effective_device() == "cuda:0"
