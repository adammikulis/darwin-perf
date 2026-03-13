"""Basic tests for macos-gpu-proc."""

import os
import sys
import time

import pytest


@pytest.fixture(autouse=True)
def _skip_non_macos():
    if sys.platform != "darwin":
        pytest.skip("macOS only")


def test_gpu_time_ns_self():
    from macos_gpu_proc import gpu_time_ns

    ns = gpu_time_ns(0)
    assert ns >= 0, "Should be able to read own process GPU time"


def test_gpu_time_ns_self_explicit_pid():
    from macos_gpu_proc import gpu_time_ns

    ns = gpu_time_ns(os.getpid())
    assert ns >= 0


def test_gpu_time_ns_multi():
    from macos_gpu_proc import gpu_time_ns_multi

    result = gpu_time_ns_multi([0])
    assert 0 in result
    assert result[0] >= 0


def test_gpu_time_ns_multi_invalid_pid():
    from macos_gpu_proc import gpu_time_ns_multi

    result = gpu_time_ns_multi([999999999])
    assert result[999999999] == -1


def test_monitor_self():
    from macos_gpu_proc import GpuMonitor

    mon = GpuMonitor()
    s1 = mon.sample()
    assert s1 == 0.0  # first sample is always 0 (baseline)
    time.sleep(0.1)
    s2 = mon.sample()
    assert isinstance(s2, float)
    assert s2 >= 0.0


def test_monitor_context_manager():
    from macos_gpu_proc import GpuMonitor

    with GpuMonitor() as mon:
        mon.sample()  # baseline
        time.sleep(0.1)
        mon.sample()  # first real sample

    stats = mon.summary()
    assert "gpu_pct_avg" in stats
    assert "gpu_pct_max" in stats
    assert stats["num_samples"] >= 1


def test_monitor_background():
    from macos_gpu_proc import GpuMonitor

    mon = GpuMonitor()
    mon.start(interval=0.1)
    time.sleep(0.5)
    mon.stop()
    stats = mon.summary()
    assert stats["num_samples"] >= 2
