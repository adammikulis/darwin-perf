# macos-gpu-proc

Per-process GPU utilization monitoring for macOS. Like Activity Monitor's GPU column, but accessible from Python and the command line.

Uses the Mach `task_info(TASK_POWER_INFO_V2)` API to read cumulative GPU time per process — the same kernel data source that Activity Monitor uses. Works on Apple Silicon and AMD GPUs.

## Install

```bash
pip install macos-gpu-proc
```

## CLI — `gpu-proc`

Live GPU process monitor, like `top` for GPU:

```bash
# Monitor all processes (needs sudo for other processes' GPU stats)
sudo gpu-proc

# Rich terminal UI with sparkline history graphs
sudo gpu-proc --tui

# Monitor only your own process (no sudo needed)
gpu-proc --self

# Monitor specific PIDs
gpu-proc --pid 1234 5678

# Top 5 GPU consumers, update every 1s
sudo gpu-proc --top 5 -i 1

# Single snapshot
sudo gpu-proc --once
```

### Plain output
```
gpu-proc  14:32:01  (every 2s)

     PID    GPU %  Process
────────  ───────  ──────────────────────────────
   12345   67.2%   python3.12            █████████████░░░░░░░
   41234   12.4%   WindowServer          ██░░░░░░░░░░░░░░░░░░
     618    3.1%   Code Helper (GPU)     ░░░░░░░░░░░░░░░░░░░░
────────  ───────
   Total   82.7%
```

### TUI mode (`--tui`)

Full-screen terminal dashboard with:
- Per-process GPU % with live bar charts
- Sparkline history graphs (last 120 samples per process)
- System-wide GPU utilization with history
- Aggregate stats: total, peak, average
- Auto-discovers new processes, removes exited ones
- Keyboard: `q` quit, `r` reset history

## Python API

### Quick one-shot

```python
from macos_gpu_proc import gpu_percent

# Your own process (no sudo needed)
pct = gpu_percent()  # blocks for 0.5s, returns GPU %
print(f"GPU: {pct:.1f}%")
```

### Continuous monitoring

```python
from macos_gpu_proc import GpuMonitor

monitor = GpuMonitor()

for batch in dataloader:
    output = model(batch)
    loss.backward()
    optimizer.step()
    print(f"GPU: {monitor.sample():.1f}%")
```

### Context manager with summary

```python
from macos_gpu_proc import GpuMonitor

with GpuMonitor() as mon:
    train(model, epochs=10)

stats = mon.summary()
print(f"Avg GPU: {stats['gpu_pct_avg']:.1f}%")
print(f"Peak GPU: {stats['gpu_pct_max']:.1f}%")
```

### Background thread

```python
monitor = GpuMonitor()
monitor.start(interval=2.0)  # samples every 2s in background

# ... do work ...

monitor.stop()
print(monitor.summary())
```

### Monitor with child processes

```python
# Sums GPU time across the process and all its children
# (e.g., training workers spawned by your script)
monitor = GpuMonitor(children=True)  # requires psutil
```

### Low-level: raw GPU nanoseconds

```python
from macos_gpu_proc import gpu_time_ns, gpu_time_ns_multi

# Cumulative GPU time for current process
ns = gpu_time_ns()

# Batch read multiple PIDs
results = gpu_time_ns_multi([0, 1234, 5678])
# {0: 123456789, 1234: 987654321, 5678: -1}  # -1 = permission denied
```

## Permissions

| Target | Sudo required? |
|--------|---------------|
| Own process (`pid=0`) | No |
| Child processes | No (if forked from your process) |
| Other users' processes | Yes (`sudo`) |

## How it works

Every macOS process has a cumulative GPU nanosecond counter maintained by the kernel (`task_power_info_v2.gpu_energy.task_gpu_utilisation`). This counter increments whenever the process submits work to the GPU (Metal, OpenCL, etc.).

`macos-gpu-proc` reads this counter via the Mach `task_info()` system call, samples it over time, and computes the percentage:

```
GPU % = (gpu_ns_now - gpu_ns_prev) / elapsed_ns × 100
```

This is the same mechanism Activity Monitor uses, but exposed as a Python API.

## Requirements

- macOS 12+ (Monterey or later)
- Python 3.9+
- Apple Silicon or AMD GPU
- `psutil` (optional, for process auto-discovery and `children=True`)
- `textual` (optional, for `--tui` mode)

Install all extras: `pip install macos-gpu-proc[all]`

## License

MIT
