"""Correct NVML access, in one place.

Every VRAM and GPU-utilisation reading in this system reported nothing on a host with an
RTX 5090 in it. Two independent bugs caused that, and both existed because this logic was
written twice - once in `telemetry.py` and once, differently, in `hardware_detector.py`:

  * the `nvidia-ml-py` distribution installs its module as **pynvml**, not `nvidia_ml_py`.
    `telemetry.py` imported the latter, so the primary branch always raised ImportError.
  * since nvidia-ml-py 11.5 the string getters return `str`, not `bytes`. The fallback
    branch called `.decode('utf-8')` on the result, raising AttributeError, which the
    broad `except Exception` swallowed into a generic path that shells out to `lspci` -
    a tool that is not installed in the container. The observable result was
    `{"available": False, "message": "No GPU detection method available"}`.

So the node could not report the one resource that actually limits how many cameras it
can run. This module exists so there is exactly one implementation to get right.

Two unit conventions are deliberately served at once, because the two consumers disagree
and silently converting at a call site is how this class of bug returns:
  * `memory_*_gb`    - floats, the telemetry/benchmark contract
  * `memory_*_bytes` - ints, the hardware/UI contract (rendered with formatBytes())

Everything here is best-effort. A probe failure returns "not available" rather than
raising: telemetry must never be able to take down the node it is reporting on.
"""
import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_nvml_module = None          # the imported module, once initialised
_nvml_failed = False         # remember failure so we retry the import at most once


def _load_nvml():
    """Import and nvmlInit() exactly once per process, under a lock.

    `pynvml` is tried FIRST because that is the module name `nvidia-ml-py` actually
    installs; `nvidia_ml_py` is kept only in case a future release adds that alias.
    """
    global _nvml_module, _nvml_failed
    if _nvml_module is not None or _nvml_failed:
        return _nvml_module
    with _lock:
        if _nvml_module is not None or _nvml_failed:
            return _nvml_module
        for name in ("pynvml", "nvidia_ml_py"):
            try:
                import importlib
                mod = importlib.import_module(name)
                mod.nvmlInit()
                _nvml_module = mod
                logger.debug("NVML initialised via %s", name)
                return _nvml_module
            except Exception as e:                      # not importable, or no driver
                logger.debug("NVML unavailable via %s: %s", name, e)
        _nvml_failed = True
        return None


def _text(value: Any) -> str:
    """NVML returns `str` on >=11.5 and `bytes` before that. Accept both."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _try(fn, default=None):
    """A single metric being unsupported must not lose the whole device."""
    try:
        return fn()
    except Exception:
        return default


def _device_entry(nvml, handle, index: int) -> Dict[str, Any]:
    mem = nvml.nvmlDeviceGetMemoryInfo(handle)
    total, used, free = int(mem.total), int(mem.used), int(mem.free)

    entry: Dict[str, Any] = {
        "id": index,
        "name": _text(nvml.nvmlDeviceGetName(handle)),
        "vendor": "NVIDIA",
        "memory_total_gb": round(total / (1024 ** 3), 2),
        "memory_used_gb": round(used / (1024 ** 3), 2),
        "memory_free_gb": round(free / (1024 ** 3), 2),
        "memory_total_bytes": total,
        "memory_used_bytes": used,
        "memory_free_bytes": free,
    }

    util = _try(lambda: nvml.nvmlDeviceGetUtilizationRates(handle))
    if util is not None:
        # 0% is a legitimate reading, so test for None rather than truthiness.
        entry["gpu_utilization_percent"] = util.gpu
        entry["memory_utilization_percent"] = util.memory

    temp = _try(lambda: nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU))
    if temp is not None:
        entry["temperature_c"] = temp

    power_mw = _try(lambda: nvml.nvmlDeviceGetPowerUsage(handle))
    if power_mw is not None:
        entry["power_usage_w"] = round(power_mw / 1000.0, 1)

    return entry


def _probe_nvml() -> Optional[Dict[str, Any]]:
    nvml = _load_nvml()
    if nvml is None:
        return None
    try:
        count = nvml.nvmlDeviceGetCount()
    except Exception as e:
        logger.debug("nvmlDeviceGetCount failed: %s", e)
        return None

    devices: List[Dict[str, Any]] = []
    for i in range(count):
        try:
            devices.append(_device_entry(nvml, nvml.nvmlDeviceGetHandleByIndex(i), i))
        except Exception as e:
            logger.debug("NVML probe of device %d failed: %s", i, e)

    if not devices:
        return None

    return {
        "available": True,
        "devices": devices,
        "driver_version": _try(lambda: _text(nvml.nvmlSystemGetDriverVersion())),
        "source": "nvml",
    }


def _probe_torch() -> Optional[Dict[str, Any]]:
    """Last-resort fallback when NVML is missing but CUDA works.

    Deliberately second: `mem_get_info` initialises a CUDA context on any device it
    touches, which itself costs VRAM, whereas NVML observes without allocating. It
    also cannot report utilisation - only memory.
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        devices = []
        for i in range(torch.cuda.device_count()):
            free, total = torch.cuda.mem_get_info(i)
            free, total = int(free), int(total)
            used = total - free
            devices.append({
                "id": i,
                "name": torch.cuda.get_device_name(i),
                "vendor": "NVIDIA",
                "memory_total_gb": round(total / (1024 ** 3), 2),
                "memory_used_gb": round(used / (1024 ** 3), 2),
                "memory_free_gb": round(free / (1024 ** 3), 2),
                "memory_total_bytes": total,
                "memory_used_bytes": used,
                "memory_free_bytes": free,
            })
        if not devices:
            return None
        return {"available": True, "devices": devices, "source": "torch"}
    except Exception as e:
        logger.debug("torch GPU probe failed: %s", e)
        return None


def probe() -> Dict[str, Any]:
    """Real per-device VRAM and utilisation, or an explicit "not available".

    Reported memory is DEVICE-WIDE, not this process's share: another tenant on the
    same card (an LLM server, another container) is counted, which is exactly what
    capacity planning needs to see.
    """
    return _probe_nvml() or _probe_torch() or {
        "available": False,
        "devices": [],
        "message": "No NVIDIA GPU visible (NVML and CUDA both unavailable)",
    }


def total_memory_bytes_by_index() -> Dict[int, int]:
    """Index -> total VRAM in bytes, for callers that only need capacity."""
    info = probe()
    if not info.get("available"):
        return {}
    return {d["id"]: d["memory_total_bytes"] for d in info["devices"]
            if d.get("memory_total_bytes")}
