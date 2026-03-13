# macos-gpu-proc

Per-process GPU utilization, CPU, memory, and energy monitoring for macOS Apple Silicon. **No sudo needed.**

Reads GPU client data directly from the IORegistry — the same data source Activity Monitor uses. Auto-discovers every process using the GPU.

## Install

```bash
pip install macos-gpu-proc
```

## Quick Start

```python
from macos_gpu_proc import gpu_clients, gpu_time_ns, proc_info, system_gpu_stats
import time

# Auto-discover all processes using the GPU — no PIDs needed
for c in gpu_clients():
    print(f"PID {c['pid']} ({c['name']}): {c['gpu_ns']/1e9:.1f}s GPU time")

# Example output:
#   PID 4245 (python3.12): 123.6s GPU time
#   PID  418 (WindowServer): 28.8s GPU time
#   PID  729 (Code Helper (GPU): 9.6s GPU time
```

### GPU Utilization %

All values are cumulative — take two snapshots and divide by elapsed time:

```python
# Pick a PID from gpu_clients(), or use your own
clients = gpu_clients()
pid = clients[0]['pid']  # highest GPU user

t1 = gpu_time_ns(pid)
time.sleep(2)
t2 = gpu_time_ns(pid)

gpu_pct = (t2 - t1) / (2 * 1e9) * 100
print(f"PID {pid} GPU: {gpu_pct:.1f}%")
```

### Per-Process Stats

`proc_info(pid)` returns CPU, memory, energy, disk I/O, and thread count for any process — no sudo needed for same-user processes:

```python
info = proc_info(pid)
print(f"CPU: {info['cpu_ns']/1e9:.1f}s")
print(f"Memory: {info['memory']/1e6:.0f}MB")
print(f"Energy: {info['energy_nj']/1e9:.1f}J")
print(f"Threads: {info['threads']}")
```

### System-Wide GPU

```python
stats = system_gpu_stats()
print(f"{stats['model']} ({stats['gpu_core_count']} cores)")
print(f"Device utilization: {stats['device_utilization']}%")
print(f"GPU VRAM in use: {stats['in_use_system_memory']/1e9:.1f}GB")
```

### GpuMonitor (continuous monitoring)

Monitor your own training process — no PID lookup needed:

```python
from macos_gpu_proc import GpuMonitor

mon = GpuMonitor()  # monitors the current process
for batch in dataloader:
    train(batch)
    print(f"GPU: {mon.sample():.1f}%")

# Or as a context manager:
with GpuMonitor() as mon:
    mon.start(interval=2.0)  # background sampling
    train()
print(mon.summary())  # {'gpu_pct_avg': 42.1, 'gpu_pct_max': 87.3, ...}
```

## CLI

```bash
gpu-proc              # live per-process GPU monitor — auto-discovers all GPU processes
gpu-proc --once       # single snapshot
gpu-proc --tui        # rich terminal UI with sparkline graphs (pip install macos-gpu-proc[tui])
gpu-proc --gui        # native floating window monitor (pip install macos-gpu-proc[gui])
gpu-proc -i 1         # 1-second update interval
gpu-proc --pid 1234   # monitor specific PID
```

## API Reference

### C Extension Functions

| Function | Description |
|----------|-------------|
| `gpu_clients()` | Auto-discover all GPU-active processes: `[{'pid', 'name', 'gpu_ns'}, ...]` |
| `gpu_time_ns(pid)` | Cumulative GPU nanoseconds for a PID |
| `gpu_time_ns_multi(pids)` | Batch GPU ns for multiple PIDs (single IORegistry scan) |
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
| `energy_nj` | Cumulative energy (nanojoules) — delta over time = watts |
| `threads` | Current thread count |

### system_gpu_stats fields

| Field | Description |
|-------|-------------|
| `model` | GPU model name (e.g., "Apple M4 Max") |
| `gpu_core_count` | Number of GPU cores |
| `device_utilization` | Device utilization % (0-100) |
| `tiler_utilization` | Tiler utilization % |
| `renderer_utilization` | Renderer utilization % |
| `alloc_system_memory` | Total GPU-allocated system memory |
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
- Zero dependencies

## License

MIT
