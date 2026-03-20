"""darwin-perf: Cross-platform system performance monitoring.

macOS Apple Silicon: GPU, CPU, memory, energy, temperature, and disk I/O
via Mach kernel APIs, IORegistry, and AppleSMC. No sudo needed.

Linux/Windows NVIDIA: GPU monitoring via pynvml (pip install pynvml).
Linux AMD: GPU monitoring via pyamdgpuinfo (pip install pyamdgpuinfo).
CPU-only: System metrics via psutil.

Quick start::

    import darwin_perf as dp

    # Everything in one call (works on ALL platforms)
    s = dp.stats()
    print(f"GPU: {s.get('gpu_util_pct', 'N/A')}%")
    print(f"RAM: {s.get('ram_used_gb', 'N/A')}GB")

    # Which backend is active?
    print(dp.backend_name())  # "darwin_native" | "nvidia_pynvml" | "amd_pyamdgpuinfo" | "cpu_fallback"

Lower-level APIs still available: proc_info, system_stats, system_gpu_stats,
gpu_power, cpu_power, temperatures, snapshot, GpuMonitor.
"""

from __future__ import annotations

import platform as _platform

# Detect platform and import from appropriate backend
_IS_DARWIN_ARM64 = (
    _platform.system() == "Darwin" and _platform.machine() == "arm64"
)

if _IS_DARWIN_ARM64:
    # macOS ARM64 — use native C extension (production, instant <100us)
    try:
        from ._native import (
            cpu_power,
            cpu_time_ns,
            gpu_clients,
            gpu_freq_table,
            gpu_power,
            gpu_time_ns,
            gpu_time_ns_multi,
            hid_idle_ns,
            net_io_counters,
            net_io_per_iface,
            ppid,
            proc_connections,
            proc_info,
            proc_lineage,
            proc_open_files,
            proc_pidpath,
            system_gpu_stats,
            system_stats,
            temperatures,
        )
        _NATIVE_AVAILABLE = True
    except ImportError:
        _NATIVE_AVAILABLE = False
else:
    _NATIVE_AVAILABLE = False

if not _NATIVE_AVAILABLE:
    # Cross-platform: detect GPU vendor and use appropriate Python backend
    from ._platform import get_backend as _get_backend

    _backend, _backend_name_str = _get_backend()

    # Import core functions from detected backend
    system_gpu_stats = _backend.system_gpu_stats
    system_stats = _backend.system_stats
    gpu_power = _backend.gpu_power
    temperatures = _backend.temperatures
    proc_info = _backend.proc_info
    gpu_clients = _backend.gpu_clients
    gpu_time_ns = _backend.gpu_time_ns
    gpu_time_ns_multi = _backend.gpu_time_ns_multi
    ppid = _backend.ppid
    cpu_power = _backend.cpu_power
    cpu_time_ns = _backend.cpu_time_ns

    # Functions only available on macOS native — provide stubs
    def gpu_freq_table():
        return []

    def hid_idle_ns():
        return 0

    def net_io_counters():
        return {}

    def net_io_per_iface():
        return {}

    def proc_connections(pid: int = 0):
        return []

    def proc_lineage(pid: int = 0):
        return []

    def proc_open_files(pid: int = 0):
        return []

    def proc_pidpath(pid: int = 0):
        return ""


# --- Recorder (pure Python, works everywhere) ---
try:
    from ._recorder import Anomaly, Recorder, Sample, SpanRecord
except ImportError:
    Anomaly = None  # type: ignore[assignment,misc]
    Recorder = None  # type: ignore[assignment,misc]
    Sample = None  # type: ignore[assignment,misc]
    SpanRecord = None  # type: ignore[assignment,misc]

# --- High-level Python API (works everywhere, uses whichever backend is active) ---
from ._api import (
    GpuMonitor,
    _snapshot,
    cpu_usage,
    gpu_percent,
    proc_usage,
    sample_gpu,
    snapshot,
    stats,
)

# --- Platform detection API ---
from ._platform import backend_name


__all__ = [
    # Classes
    "Anomaly",
    "GpuMonitor",
    "IDSMonitor",
    "Recorder",
    "Sample",
    "SpanRecord",
    # Core functions (cross-platform)
    "cpu_power",
    "cpu_time_ns",
    "gpu_clients",
    "gpu_freq_table",
    "gpu_power",
    "gpu_time_ns",
    "gpu_time_ns_multi",
    "hid_idle_ns",
    "net_io_counters",
    "net_io_per_iface",
    "ppid",
    "proc_connections",
    "proc_info",
    "proc_lineage",
    "proc_open_files",
    "proc_pidpath",
    "system_gpu_stats",
    "system_stats",
    "temperatures",
    # Python wrapper functions
    "backend_name",
    "cpu_usage",
    "gpu_percent",
    "network_snapshot",
    "proc_usage",
    "sample_gpu",
    "snapshot",
    "stats",
]

__version__ = "1.1.0"


def __getattr__(name: str):
    """Lazy imports for optional modules (IDS, network)."""
    if name == "_snapshot":
        from ._api import _snapshot
        return _snapshot
    if name == "IDSMonitor":
        from ._ids import IDSMonitor
        return IDSMonitor
    if name == "network_snapshot":
        from ._network import network_snapshot
        return network_snapshot
    raise AttributeError(f"module 'darwin_perf' has no attribute {name!r}")
