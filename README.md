# macos-gpu-proc

Per-process GPU utilization, CPU, memory, and energy monitoring for macOS Apple Silicon. **No sudo needed.**

Reads GPU client data directly from the IORegistry's AGXDeviceUserClient entries — the same data source Activity Monitor uses.

## Install

```bash
pip install macos-gpu-proc
```

## Quick Start

```python
from macos_gpu_proc import gpu_clients, system_gpu_stats, proc_info

# List all GPU-active processes
for c in gpu_clients():
    print(f"PID {c['pid']} ({c['name']}): {c['gpu_ns']/1e9:.1f}s GPU time")

# System-wide GPU stats (device utilization, VRAM, model name)
stats = system_gpu_stats()
print(f"{stats['model']} — {stats['device_utilization']}% device utilization")

# Per-process detail (CPU, memory, energy, disk I/O, threads)
info = proc_info(1234)
print(f"CPU: {info['cpu_ns']/1e9:.1f}s, Memory: {info['memory']/1e6:.0f}MB, Energy: {info['energy_nj']/1e9:.1f}J")
```

### GPU Utilization %

```python
from macos_gpu_proc import gpu_time_ns
import time

pid = 1234
t1 = gpu_time_ns(pid)
time.sleep(2)
t2 = gpu_time_ns(pid)
gpu_pct = (t2 - t1) / (2 * 1e9) * 100
print(f"GPU: {gpu_pct:.1f}%")
```

### GpuMonitor (continuous monitoring)

```python
from macos_gpu_proc import GpuMonitor

with GpuMonitor(pid=1234) as mon:
    mon.start(interval=2.0)
    # ... training loop ...
print(mon.summary())  # {'gpu_pct_avg': 42.1, 'gpu_pct_max': 87.3, ...}
```

## CLI

```bash
gpu-proc              # live per-process GPU monitor (like top for GPU)
gpu-proc --once       # single snapshot
gpu-proc --tui        # rich terminal UI with sparkline graphs
gpu-proc --gui        # native floating window monitor
gpu-proc -i 1         # 1-second update interval
gpu-proc --pid 1234   # monitor specific PID
```

## API Reference

### Low-level (C extension)

| Function | Description |
|----------|-------------|
| `gpu_time_ns(pid)` | Cumulative GPU nanoseconds for a PID |
| `gpu_time_ns_multi(pids)` | Batch GPU ns for multiple PIDs |
| `gpu_clients()` | All GPU clients: `[{'pid', 'name', 'gpu_ns'}, ...]` |
| `cpu_time_ns(pid)` | Cumulative CPU nanoseconds (user + system) |
| `proc_info(pid)` | Full process stats (CPU, memory, energy, disk, threads) |
| `system_gpu_stats()` | System GPU: utilization %, VRAM, model, core count |

### proc_info fields

| Field | Description |
|-------|-------------|
| `cpu_ns` | Cumulative CPU time (user + system) in nanoseconds |
| `cpu_user_ns` | User CPU time |
| `cpu_system_ns` | System/kernel CPU time |
| `memory` | Physical memory footprint (bytes) |
| `real_memory` | Resident memory (bytes) |
| `neural_footprint` | Neural Engine memory (bytes) |
| `disk_read_bytes` | Cumulative disk reads |
| `disk_write_bytes` | Cumulative disk writes |
| `energy_nj` | Cumulative energy (nanojoules) — delta for watts |
| `threads` | Current thread count |

### system_gpu_stats fields

| Field | Description |
|-------|-------------|
| `model` | GPU model name (e.g., "Apple M4 Max") |
| `gpu_core_count` | Number of GPU cores |
| `device_utilization` | Device utilization % (0-100) |
| `tiler_utilization` | Tiler utilization % |
| `renderer_utilization` | Renderer utilization % |
| `alloc_system_memory` | Total GPU-allocated memory |
| `in_use_system_memory` | Currently used GPU memory |

## How It Works

On macOS, every Metal GPU client (command queue) is registered as an `AGXDeviceUserClient` child of the AGX accelerator in the IORegistry. Each carries:

- `IOUserClientCreator` — the PID and process name
- `AppUsage` — array of `{API, accumulatedGPUTime}` entries

This data is world-readable from the IORegistry. No `task_for_pid`, no `sudo`, no SIP changes, no private frameworks.

CPU/memory/energy stats come from `proc_pid_rusage(RUSAGE_INFO_V6)` and `proc_pidinfo(PROC_PIDTASKINFO)`.

## Requirements

- macOS with Apple Silicon (M1/M2/M3/M4/M5)
- Python 3.9+
- No external dependencies for core functionality

## License

MIT
