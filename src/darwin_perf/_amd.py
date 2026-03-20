"""AMD GPU monitoring via pyamdgpuinfo/amdsmi — cross-platform backend.

Provides darwin-perf API compatibility on Linux with AMD GPUs.
No psutil — uses _sysinfo for CPU/memory.

Requires: pip install pyamdgpuinfo (or amdsmi)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_gpu = None
_backend: str = "none"
_initialized = False


def _ensure_init() -> bool:
    global _gpu, _backend, _initialized
    if _initialized:
        return _gpu is not None
    _initialized = True

    try:
        import pyamdgpuinfo
        if pyamdgpuinfo.detect_gpus() > 0:
            _gpu = pyamdgpuinfo.get_gpu(0)
            _backend = "pyamdgpuinfo"
            return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug("pyamdgpuinfo init failed: %s", e)

    try:
        import amdsmi
        amdsmi.amdsmi_init()
        handles = amdsmi.amdsmi_get_processor_handles()
        if handles:
            _gpu = handles[0]
            _backend = "amdsmi"
            return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug("amdsmi init failed: %s", e)

    return False


def system_gpu_stats() -> dict[str, Any]:
    if not _ensure_init():
        return {}

    result: dict[str, Any] = {}
    try:
        if _backend == "pyamdgpuinfo":
            result["model"] = _gpu.name or "AMD GPU"
            result["device_utilization"] = round(float(_gpu.query_load()) * 100.0, 1)
            vram = _gpu.memory_info.get("vram_size", 0)
            result["alloc_system_memory"] = vram
            try:
                result["in_use_system_memory"] = _gpu.query_vram_usage()
            except Exception:
                result["in_use_system_memory"] = 0
            result["gpu_core_count"] = 0
            result["memory_free"] = vram - result.get("in_use_system_memory", 0)
        elif _backend == "amdsmi":
            import amdsmi
            result["model"] = amdsmi.amdsmi_get_gpu_vendor_name(_gpu)
            try:
                util = amdsmi.amdsmi_get_gpu_activity(_gpu)
                result["device_utilization"] = float(util.get("gfx_activity", 0))
            except Exception:
                result["device_utilization"] = 0.0
            try:
                vram_total = amdsmi.amdsmi_get_gpu_memory_total(_gpu, amdsmi.AmsmiMemoryType.VRAM)
                vram_used = amdsmi.amdsmi_get_gpu_memory_usage(_gpu, amdsmi.AmsmiMemoryType.VRAM)
                result["alloc_system_memory"] = vram_total
                result["in_use_system_memory"] = vram_used
                result["memory_free"] = vram_total - vram_used
            except Exception:
                pass
            result["gpu_core_count"] = 0
    except Exception as e:
        logger.debug("AMD system_gpu_stats failed: %s", e)

    return result


def system_stats() -> dict[str, Any]:
    from darwin_perf._sysinfo import system_memory, cpu_ticks
    result = system_memory()
    result.update(cpu_ticks())
    return result


def gpu_power(interval: float = 1.0) -> dict[str, Any]:
    if not _ensure_init():
        return {}
    try:
        if _backend == "pyamdgpuinfo":
            power_uw = _gpu.query_power()
            freq_hz = _gpu.query_sclk()
            return {
                "watts": round(power_uw / 1e6, 1),
                "gpu_power_w": round(power_uw / 1e6, 1),
                "mhz": round(freq_hz / 1e6, 0),
                "gpu_freq_mhz": round(freq_hz / 1e6, 0),
                "throttled": False,
                "active_state": "",
                "frequency_states": [],
            }
        elif _backend == "amdsmi":
            import amdsmi
            try:
                power = amdsmi.amdsmi_get_power_info(_gpu)
                watts = power.get("average_socket_power", 0) / 1000.0
            except Exception:
                watts = 0.0
            try:
                freq = amdsmi.amdsmi_get_clock_info(_gpu, amdsmi.AmsmiClkType.GFX)
                mhz = freq.get("clk", 0)
            except Exception:
                mhz = 0
            return {
                "watts": round(watts, 1),
                "gpu_power_w": round(watts, 1),
                "mhz": mhz,
                "gpu_freq_mhz": mhz,
                "throttled": False,
                "active_state": "",
                "frequency_states": [],
            }
    except Exception as e:
        logger.debug("AMD gpu_power failed: %s", e)
    return {}


def temperatures() -> dict[str, Any]:
    if not _ensure_init():
        return {}
    try:
        if _backend == "pyamdgpuinfo":
            temp = _gpu.query_temperature()
            return {"gpu_avg": float(temp), "cpu_avg": 0.0, "system_avg": float(temp)}
        elif _backend == "amdsmi":
            import amdsmi
            temp = amdsmi.amdsmi_get_temp_metric(_gpu, amdsmi.AmsmiTemperatureType.EDGE, amdsmi.AmsmiTemperatureMetric.CURRENT)
            return {"gpu_avg": float(temp), "cpu_avg": 0.0, "system_avg": float(temp)}
    except Exception:
        pass
    return {}


def proc_info(pid: int) -> dict[str, Any] | None:
    from darwin_perf._sysinfo import process_info
    return process_info(pid)


def gpu_clients() -> list[dict[str, Any]]:
    return []


def gpu_time_ns(pid: int) -> int:
    return 0


def gpu_time_ns_multi(pids: list[int]) -> dict[int, int]:
    return {pid: 0 for pid in pids}


def ppid(pid: int) -> int:
    from darwin_perf._sysinfo import parent_pid
    return parent_pid(pid)


def cpu_power(interval: float = 1.0) -> dict[str, Any]:
    return {}


def cpu_time_ns() -> int:
    from darwin_perf._nvidia import cpu_time_ns as _ct
    return _ct()
