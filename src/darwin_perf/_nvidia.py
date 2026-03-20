"""NVIDIA GPU monitoring via pynvml — cross-platform backend.

Provides darwin-perf API compatibility on Linux/Windows with NVIDIA GPUs.
All calls instant (<1ms) via NVML C library bindings.
No psutil — uses /proc or ctypes directly via _sysinfo.

Requires: pip install pynvml
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_pynvml = None
_handle = None
_initialized = False


def _ensure_init() -> bool:
    global _pynvml, _handle, _initialized
    if _initialized:
        return _pynvml is not None
    _initialized = True
    try:
        import pynvml
        pynvml.nvmlInit()
        _pynvml = pynvml
        _handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        return True
    except Exception as e:
        logger.debug("pynvml init failed: %s", e)
        return False


def system_gpu_stats() -> dict[str, Any]:
    """NVIDIA equivalent of darwin-perf system_gpu_stats()."""
    if not _ensure_init():
        return {}
    try:
        name = _pynvml.nvmlDeviceGetName(_handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8")
        mem = _pynvml.nvmlDeviceGetMemoryInfo(_handle)
        util = _pynvml.nvmlDeviceGetUtilizationRates(_handle)
        cores = 0
        try:
            cores = _pynvml.nvmlDeviceGetNumGpuCores(_handle)
        except Exception:
            pass
        return {
            "device_utilization": float(util.gpu),
            "model": name,
            "gpu_core_count": cores,
            "in_use_system_memory": mem.used,
            "alloc_system_memory": mem.total,
            "memory_free": mem.free,
        }
    except Exception as e:
        logger.debug("nvidia system_gpu_stats failed: %s", e)
        return {}


def system_stats() -> dict[str, Any]:
    """System-wide CPU and memory stats via /proc or ctypes."""
    from darwin_perf._sysinfo import system_memory, cpu_ticks

    result = system_memory()
    result.update(cpu_ticks())
    return result


def gpu_power(interval: float = 1.0) -> dict[str, Any]:
    """NVIDIA GPU power, frequency, and throttle state (instant via NVML)."""
    if not _ensure_init():
        return {}
    try:
        milliwatts = _pynvml.nvmlDeviceGetPowerUsage(_handle)
        freq = _pynvml.nvmlDeviceGetClockInfo(_handle, _pynvml.NVML_CLOCK_GRAPHICS)
        p_state = _pynvml.nvmlDeviceGetPerformanceState(_handle)
        throttled = p_state >= 8

        return {
            "watts": round(milliwatts / 1000.0, 1),
            "gpu_power_w": round(milliwatts / 1000.0, 1),
            "mhz": freq,
            "gpu_freq_mhz": freq,
            "throttled": throttled,
            "active_state": f"P{p_state}",
            "frequency_states": [],
        }
    except Exception as e:
        logger.debug("nvidia gpu_power failed: %s", e)
        return {}


def temperatures() -> dict[str, Any]:
    """NVIDIA GPU temperature."""
    if not _ensure_init():
        return {}
    try:
        temp = _pynvml.nvmlDeviceGetTemperature(_handle, _pynvml.NVML_TEMPERATURE_GPU)
        return {
            "gpu_avg": float(temp),
            "cpu_avg": 0.0,
            "system_avg": float(temp),
        }
    except Exception:
        return {}


def proc_info(pid: int) -> dict[str, Any] | None:
    """Per-process info via /proc or ctypes."""
    from darwin_perf._sysinfo import process_info
    return process_info(pid)


def gpu_clients() -> list[dict[str, Any]]:
    """List GPU client processes (NVIDIA compute + graphics processes)."""
    if not _ensure_init():
        return []
    try:
        procs = _pynvml.nvmlDeviceGetComputeRunningProcesses(_handle)
        graphics = _pynvml.nvmlDeviceGetGraphicsRunningProcesses(_handle)
        seen = set()
        result = []
        for p in list(procs) + list(graphics):
            if p.pid in seen:
                continue
            seen.add(p.pid)
            name = ""
            try:
                from darwin_perf._sysinfo import process_info
                info = process_info(p.pid)
                if info:
                    name = info.get("name", "")
            except Exception:
                pass
            result.append({
                "pid": p.pid,
                "name": name,
                "gpu_ns": 0,
                "gpu_memory": p.usedGpuMemory or 0,
                "api": "cuda",
            })
        return result
    except Exception:
        return []


def gpu_time_ns(pid: int) -> int:
    """GPU nanoseconds (not available via NVML)."""
    return 0


def gpu_time_ns_multi(pids: list[int]) -> dict[int, int]:
    return {pid: 0 for pid in pids}


def ppid(pid: int) -> int:
    from darwin_perf._sysinfo import parent_pid
    return parent_pid(pid)


def cpu_power(interval: float = 1.0) -> dict[str, Any]:
    return {}


def cpu_time_ns() -> int:
    """Total CPU nanoseconds from /proc/stat or GetSystemTimes."""
    from darwin_perf._sysinfo import cpu_ticks
    ticks = cpu_ticks()
    user = ticks.get("cpu_ticks_user", 0)
    system = ticks.get("cpu_ticks_system", 0)
    return int((user + system) * 1e7)  # ticks to ns (approximate)
