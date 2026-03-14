"""Basic tests for darwin-perf."""

import os
import sys
import time

import pytest


@pytest.fixture(autouse=True)
def _skip_non_macos():
    if sys.platform != "darwin":
        pytest.skip("macOS only")


def test_gpu_time_ns_self():
    from darwin_perf import gpu_time_ns

    ns = gpu_time_ns(0)
    assert ns >= 0, "Should be able to read own process GPU time"


def test_gpu_time_ns_self_explicit_pid():
    from darwin_perf import gpu_time_ns

    ns = gpu_time_ns(os.getpid())
    assert ns >= 0


def test_gpu_time_ns_multi():
    from darwin_perf import gpu_time_ns_multi

    result = gpu_time_ns_multi([0])
    assert 0 in result
    assert result[0] >= 0


def test_gpu_time_ns_multi_invalid_pid():
    from darwin_perf import gpu_time_ns_multi

    result = gpu_time_ns_multi([999999999])
    assert result[999999999] == 0


def test_monitor_self():
    from darwin_perf import GpuMonitor

    mon = GpuMonitor()
    s1 = mon.sample()
    assert s1 == 0.0  # first sample is always 0 (baseline)
    time.sleep(0.1)
    s2 = mon.sample()
    assert isinstance(s2, float)
    assert s2 >= 0.0


def test_monitor_context_manager():
    from darwin_perf import GpuMonitor

    with GpuMonitor() as mon:
        mon.sample()  # baseline
        time.sleep(0.1)
        mon.sample()  # first real sample

    stats = mon.summary()
    assert "gpu_pct_avg" in stats
    assert "gpu_pct_max" in stats
    assert stats["num_samples"] >= 1


def test_monitor_background():
    from darwin_perf import GpuMonitor

    mon = GpuMonitor()
    mon.start(interval=0.1)
    time.sleep(0.5)
    mon.stop()
    stats = mon.summary()
    assert stats["num_samples"] >= 2


def test_system_stats_memory():
    from darwin_perf import system_stats

    s = system_stats()
    assert s["memory_total"] > 0
    assert s["memory_used"] > 0
    assert s["memory_available"] >= 0
    assert s["memory_used"] <= s["memory_total"]
    # Compressed should be included in used
    assert s["memory_compressed"] >= 0


def test_system_stats_cpu_ticks():
    from darwin_perf import system_stats

    s = system_stats()
    assert s["cpu_ticks_user"] > 0
    assert s["cpu_ticks_system"] > 0
    assert s["cpu_ticks_idle"] > 0
    assert s["cpu_count"] > 0
    assert isinstance(s["cpu_name"], str)
    assert len(s["cpu_name"]) > 0


def test_system_stats_cpu_delta():
    from darwin_perf import system_stats

    s1 = system_stats()
    time.sleep(0.2)
    s2 = system_stats()
    # Ticks should advance
    assert s2["cpu_ticks_user"] >= s1["cpu_ticks_user"]
    assert s2["cpu_ticks_idle"] >= s1["cpu_ticks_idle"]
    total_delta = (
        (s2["cpu_ticks_user"] - s1["cpu_ticks_user"])
        + (s2["cpu_ticks_system"] - s1["cpu_ticks_system"])
        + (s2["cpu_ticks_idle"] - s1["cpu_ticks_idle"])
    )
    assert total_delta > 0


def test_proc_info_self():
    from darwin_perf import proc_info

    info = proc_info(os.getpid())
    assert info is not None
    assert info["cpu_ns"] >= 0
    assert info["memory"] > 0
    assert info["real_memory"] > 0
    assert info["threads"] >= 1


def test_system_gpu_stats():
    from darwin_perf import system_gpu_stats

    s = system_gpu_stats()
    assert "device_utilization" in s
    assert "model" in s
    assert "gpu_core_count" in s
    assert s["gpu_core_count"] > 0
