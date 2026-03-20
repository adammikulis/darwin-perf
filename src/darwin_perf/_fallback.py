"""CPU-only fallback backend when no GPU monitoring is available.

Uses _sysinfo for CPU/memory metrics. GPU functions return empty/zero.
"""

from __future__ import annotations

from typing import Any


def system_gpu_stats() -> dict[str, Any]:
    return {}


def system_stats() -> dict[str, Any]:
    from darwin_perf._sysinfo import system_memory, cpu_ticks
    result = system_memory()
    result.update(cpu_ticks())
    return result


def gpu_power(interval: float = 1.0) -> dict[str, Any]:
    return {}


def temperatures() -> dict[str, Any]:
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
    from darwin_perf._sysinfo import cpu_ticks
    ticks = cpu_ticks()
    return int((ticks.get("cpu_ticks_user", 0) + ticks.get("cpu_ticks_system", 0)) * 1e7)
