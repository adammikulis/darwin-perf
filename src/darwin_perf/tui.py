"""darwin-perf TUI: Rich terminal system monitor with live graphs.

A full-screen terminal app showing per-process GPU utilization,
CPU/GPU power, frequencies, temperatures, and memory — with
sparkline history graphs. No sudo needed.

Usage:
    darwin-perf --tui              # all GPU-active processes
    darwin-perf --tui -i 1         # 1s update interval
    darwin-perf --tui --record f   # monitor + record to JSONL
"""

from __future__ import annotations

import json
import time as _time

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Header, Static

from . import _snapshot
from ._native import cpu_time_ns, proc_info, system_gpu_stats, system_stats, temperatures
from ._sampler import PowerSampler

# ---------------------------------------------------------------------------
# Sparkline renderer (unicode block chars)
# ---------------------------------------------------------------------------

_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float], width: int = 40) -> str:
    """Render a sparkline string from a list of 0-100 values."""
    if not values:
        return ""
    tail = values[-width:]
    mx = max(tail) if max(tail) > 0 else 1
    return "".join(
        _SPARK_CHARS[min(int(v / mx * (len(_SPARK_CHARS) - 1)), len(_SPARK_CHARS) - 1)]
        for v in tail
    )


def _fmt_bytes(b: int) -> str:
    """Format bytes as human-readable string."""
    if b < 1024:
        return f"{b}B"
    if b < 1024**2:
        return f"{b / 1024:.0f}K"
    if b < 1024**3:
        return f"{b / 1024**2:.0f}M"
    return f"{b / 1024**3:.1f}G"


def _bar(pct: float, width: int = 20, fill_color: str = "green") -> str:
    """Render a percentage bar."""
    bar_len = int(min(pct, 100) / (100 / width))
    return (
        f"[{fill_color}]" + "━" * bar_len + f"[/{fill_color}]"
        + "[dim]" + "╌" * (width - bar_len) + "[/dim]"
    )


# ---------------------------------------------------------------------------
# Process row widget
# ---------------------------------------------------------------------------


class ProcessRow(Static):
    """Single process row with name, GPU %, CPU %, memory, energy, bar, sparkline."""

    def __init__(self, pid: int, name: str) -> None:
        super().__init__()
        self.pid = pid
        self.proc_name = name
        self.history: list[float] = []
        self.current_pct: float = 0.0
        self.cpu_pct: float = 0.0
        self.mem_str: str = ""
        self.power_w: float = 0.0
        self._detail: dict = {}
        self._show_detail: bool = False

    def update_stats(
        self, gpu_pct: float, cpu_pct: float, mem_str: str, power_w: float,
        detail: dict | None = None,
    ) -> None:
        self.current_pct = gpu_pct
        self.cpu_pct = cpu_pct
        self.mem_str = mem_str
        self.power_w = power_w
        if detail is not None:
            self._detail = detail
        self.history.append(gpu_pct)
        if len(self.history) > 120:
            self.history = self.history[-120:]
        self.refresh_display()

    def refresh_display(self) -> None:
        pct = self.current_pct
        bar_len = int(min(pct, 100) / 2.5)  # 40 chars = 100%
        bar = "[green]" + "━" * bar_len + "[/green]" + "[dim]" + "╌" * (40 - bar_len) + "[/dim]"
        spark = _sparkline(self.history, 20)
        power_str = f"{self.power_w:.1f}W" if self.power_w >= 0.01 else ""
        lines = [
            f" {self.pid:>8}  {pct:>5.1f}%  {self.cpu_pct:>5.1f}%  "
            f"{self.mem_str:>6}  {power_str:>5}  {bar}  "
            f"[cyan]{self.proc_name:<18}[/cyan]  "
            f"[dim]{spark}[/dim]"
        ]

        if self._show_detail and self._detail:
            d = self._detail
            MB = 1024 * 1024
            threads = d.get("threads", 0)
            disk_r = d.get("disk_read_bytes", 0) / MB
            disk_w = d.get("disk_write_bytes", 0) / MB
            instr = d.get("instructions", 0)
            cycles = d.get("cycles", 0)
            ipc = instr / cycles if cycles > 0 else 0
            peak = d.get("peak_memory", 0) / MB
            wired = d.get("wired_size", 0) / MB
            neural = d.get("neural_footprint", 0) / MB
            idle_wk = d.get("idle_wakeups", 0)
            int_wk = d.get("interrupt_wakeups", 0)
            pageins = d.get("pageins", 0)
            cpu_user = d.get("cpu_user_ns", 0)
            cpu_sys = d.get("cpu_system_ns", 0)
            cpu_total = cpu_user + cpu_sys
            user_pct = cpu_user / cpu_total * 100 if cpu_total > 0 else 0
            sys_pct = cpu_sys / cpu_total * 100 if cpu_total > 0 else 0

            lines.append(
                f"          [dim]threads={threads}  disk: R={disk_r:.1f}M W={disk_w:.1f}M  "
                f"IPC={ipc:.2f}  peak={peak:.0f}M  wired={wired:.0f}M  neural={neural:.0f}M  "
                f"wakeups: idle={idle_wk} int={int_wk}  pageins={pageins}  "
                f"CPU: usr={user_pct:.0f}% sys={sys_pct:.0f}%[/dim]"
            )

        self.update("\n".join(lines))


# ---------------------------------------------------------------------------
# Summary bar
# ---------------------------------------------------------------------------


class SummaryBar(Static):
    """Top bar showing aggregate GPU stats + system metrics."""

    total_gpu = reactive(0.0)
    process_count = reactive(0)
    peak_gpu = reactive(0.0)
    model_name = reactive("")
    core_count = reactive(0)
    device_util = reactive(0)
    cpu_temp = reactive(0.0)
    gpu_temp = reactive(0.0)
    recording = reactive("")

    def render(self) -> str:
        rec = f"  │  [bold red]● REC {self.recording}[/bold red]" if self.recording else ""
        return (
            f"  [bold]{self.model_name}[/bold] ({self.core_count} cores)"
            f"  │  [bold]GPU:[/bold] [green]{self.device_util}%[/green]"
            f"  │  [bold]Sum:[/bold] [green]{self.total_gpu:5.1f}%[/green]"
            f"  │  [bold]Peak:[/bold] [yellow]{self.peak_gpu:5.1f}%[/yellow]"
            f"  │  [bold]CPU:[/bold] {self.cpu_temp:.0f}°C"
            f"  │  [bold]GPU:[/bold] {self.gpu_temp:.0f}°C"
            f"{rec}"
        )


# ---------------------------------------------------------------------------
# System GPU bar with history
# ---------------------------------------------------------------------------


class SystemGpuBar(Static):
    """System-wide GPU utilization with sparkline."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.history: list[float] = []

    def update_value(self, device_pct: float, alloc_mem: int, used_mem: int) -> None:
        self.history.append(device_pct)
        if len(self.history) > 120:
            self.history = self.history[-120:]
        spark = _sparkline(self.history, 60)
        bar_len = int(min(device_pct, 100) / 1.67)  # 60 chars = 100%
        bar = "[green]" + "█" * bar_len + "[/green]" + "[dim]" + "░" * (60 - bar_len) + "[/dim]"
        self.update(
            f"  [bold]Device GPU[/bold]  {device_pct:5.1f}%  {bar}"
            f"  VRAM: {_fmt_bytes(used_mem)}/{_fmt_bytes(alloc_mem)}\n"
            f"  [dim]History:[/dim]  {spark}"
        )


class TempPanel(Static):
    """Expandable temperature sensor panel."""

    def update_temps(self, temps: dict) -> None:
        lines = []
        for category, label, color in [
            ("cpu_sensors", "CPU", "red"),
            ("gpu_sensors", "GPU", "magenta"),
            ("system_sensors", "SYS", "blue"),
        ]:
            sensors = temps.get(category, {})
            if not sensors:
                continue
            avg_key = category.replace("_sensors", "_avg")
            avg = temps.get(avg_key, 0)
            items = sorted(sensors.items())
            vals = " ".join(f"{n}:{v:.0f}" for n, v in items)
            lines.append(
                f"  [{color}]{label}[/{color}] "
                f"[bold]{avg:.1f}°C[/bold]  "
                f"[dim]{vals}[/dim]"
            )
        self.update("\n".join(lines) if lines else "  [dim]No sensors[/dim]")


# ---------------------------------------------------------------------------
# Power & Frequency panel
# ---------------------------------------------------------------------------


class PowerPanel(Static):
    """CPU/GPU power, frequency, P-state residency."""

    def update_power(self, cpu_data: dict, gpu_data: dict) -> None:
        if not cpu_data and not gpu_data:
            self.update("  [dim]Waiting for power data...[/dim]")
            return

        lines = []
        # GPU power
        gpu_w = gpu_data.get("gpu_power_w", 0)
        gpu_freq = gpu_data.get("gpu_freq_mhz", 0)
        throttled = gpu_data.get("throttled", False)
        active_state = gpu_data.get("active_state", "")
        power_limit = gpu_data.get("power_limit_pct", 0)
        throttle_str = " [bold red]THROTTLED[/bold red]" if throttled else ""

        lines.append(
            f"  [bold magenta]GPU[/bold magenta]  {gpu_w:5.1f}W  {gpu_freq:4d}MHz"
            f"  state={active_state}  pwr_limit={power_limit}%{throttle_str}"
        )

        # GPU P-state residency
        gpu_states = gpu_data.get("frequency_states", [])
        if gpu_states:
            parts = []
            for s in gpu_states:
                freq = s.get("freq_mhz", 0)
                res = s.get("residency_pct", 0)
                if res > 0.5:
                    parts.append(f"{freq}MHz:{res:.0f}%")
            if parts:
                lines.append(f"  [dim]  P-states: {' '.join(parts)}[/dim]")

        # CPU power
        cpu_w = cpu_data.get("cpu_power_w", 0)
        lines.append(f"  [bold blue]CPU[/bold blue]  {cpu_w:5.1f}W")

        # Per-cluster
        clusters = cpu_data.get("clusters", {})
        for name in sorted(clusters.keys()):
            c = clusters[name]
            freq = c.get("freq_mhz", 0)
            active = c.get("active_pct", 0)
            lines.append(
                f"    {name}: {freq:4d}MHz  active={active:.0f}%"
            )
            c_states = c.get("frequency_states", [])
            if c_states:
                parts = []
                for s in c_states:
                    sf = s.get("freq_mhz", 0)
                    sr = s.get("residency_pct", 0)
                    if sr > 0.5:
                        parts.append(f"{sf}:{sr:.0f}%")
                if parts:
                    lines.append(f"    [dim]  {' '.join(parts)}[/dim]")

        self.update("\n".join(lines))


# ---------------------------------------------------------------------------
# GPU Detail panel
# ---------------------------------------------------------------------------


class GpuDetailPanel(Static):
    """Detailed GPU utilization: tiler, renderer, memory, recovery."""

    def update_gpu_detail(self, stats: dict) -> None:
        if not stats:
            self.update("  [dim]No GPU stats[/dim]")
            return

        device = stats.get("device_utilization", 0)
        tiler = stats.get("tiler_utilization", 0)
        renderer = stats.get("renderer_utilization", 0)
        alloc = stats.get("alloc_system_memory", 0)
        in_use = stats.get("in_use_system_memory", 0)
        in_use_drv = stats.get("in_use_system_memory_driver", 0)
        pb_size = stats.get("allocated_pb_size", 0)
        recovery = stats.get("recovery_count", 0)
        last_recovery = stats.get("last_recovery_time", 0)
        split_scene = stats.get("split_scene_count", 0)
        tiled_bytes = stats.get("tiled_scene_bytes", 0)

        lines = [
            f"  [bold]Utilization[/bold]  device={device}%  tiler={tiler}%  renderer={renderer}%",
            f"  {_bar(device, 30, 'green')}  dev   {_bar(tiler, 30, 'cyan')}  tiler",
            f"  {_bar(renderer, 30, 'magenta')}  rend",
            f"  [bold]Memory[/bold]  in_use={_fmt_bytes(in_use)}  alloc={_fmt_bytes(alloc)}"
            f"  driver={_fmt_bytes(in_use_drv)}  PB={_fmt_bytes(pb_size)}",
            f"  [bold]Recovery[/bold]  count={recovery}  last_time={last_recovery}"
            f"  │  split_scenes={split_scene}  tiled={_fmt_bytes(tiled_bytes)}",
        ]
        self.update("\n".join(lines))


# ---------------------------------------------------------------------------
# Memory Breakdown panel
# ---------------------------------------------------------------------------


class MemoryPanel(Static):
    """System memory breakdown: active, inactive, wired, compressed, free."""

    def update_memory(self, stats: dict) -> None:
        if not stats:
            self.update("  [dim]No memory stats[/dim]")
            return

        GB = 1024**3
        total = stats.get("memory_total", 0)
        active = stats.get("memory_active", 0)
        inactive = stats.get("memory_inactive", 0)
        wired = stats.get("memory_wired", 0)
        compressed = stats.get("memory_compressed", 0)
        free = stats.get("memory_free", 0)
        used = stats.get("memory_used", 0)
        available = stats.get("memory_available", 0)

        cpu_name = stats.get("cpu_name", "?")
        cpu_count = stats.get("cpu_count", 0)
        cpu_user = stats.get("cpu_user_pct", 0)
        cpu_sys = stats.get("cpu_system_pct", 0)
        cpu_idle = stats.get("cpu_idle_pct", 0)

        # Stacked bar (50 chars total)
        if total > 0:
            w = 50
            a_len = int(active / total * w)
            i_len = int(inactive / total * w)
            w_len = int(wired / total * w)
            c_len = int(compressed / total * w)
            f_len = w - a_len - i_len - w_len - c_len
            if f_len < 0:
                f_len = 0
            mem_bar = (
                f"[green]{'█' * a_len}[/green]"
                f"[yellow]{'█' * i_len}[/yellow]"
                f"[red]{'█' * w_len}[/red]"
                f"[cyan]{'█' * c_len}[/cyan]"
                f"[dim]{'░' * f_len}[/dim]"
            )
        else:
            mem_bar = "[dim]" + "░" * 50 + "[/dim]"

        lines = [
            f"  {mem_bar}  {_fmt_bytes(used)}/{_fmt_bytes(total)}",
            f"  [green]active[/green]={_fmt_bytes(active)}  "
            f"[yellow]inactive[/yellow]={_fmt_bytes(inactive)}  "
            f"[red]wired[/red]={_fmt_bytes(wired)}  "
            f"[cyan]compressed[/cyan]={_fmt_bytes(compressed)}  "
            f"[dim]free[/dim]={_fmt_bytes(free)}  "
            f"avail={_fmt_bytes(available)}",
            f"  [bold]CPU[/bold]  {cpu_name} ({cpu_count} cores)"
            f"  │  usr={cpu_user:.1f}%  sys={cpu_sys:.1f}%  idle={cpu_idle:.1f}%",
        ]
        self.update("\n".join(lines))


# ---------------------------------------------------------------------------
# Main TUI App
# ---------------------------------------------------------------------------


class GpuProcApp(App):
    """Per-process GPU monitor TUI."""

    CSS = """
    Screen {
        background: $surface;
    }
    #summary {
        height: 1;
        background: $primary-background;
        color: $text;
    }
    #system-bar {
        height: 3;
        padding: 0 1;
        border-bottom: solid $primary-lighten-2;
    }
    #header-row {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #temp-panel {
        padding: 0 1;
        border-bottom: solid $primary-lighten-2;
        display: none;
    }
    #temp-panel.visible {
        display: block;
    }
    #power-panel {
        padding: 0 1;
        border-bottom: solid $primary-lighten-2;
        display: none;
    }
    #power-panel.visible {
        display: block;
    }
    #gpu-detail-panel {
        padding: 0 1;
        border-bottom: solid $primary-lighten-2;
        display: none;
    }
    #gpu-detail-panel.visible {
        display: block;
    }
    #memory-panel {
        padding: 0 1;
        border-bottom: solid $primary-lighten-2;
        display: none;
    }
    #memory-panel.visible {
        display: block;
    }
    #process-list {
        height: 1fr;
        overflow-y: auto;
        padding: 0 1;
    }
    ProcessRow {
        height: auto;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("R", "reset", "Reset history"),
        ("r", "toggle_record", "Record"),
        ("t", "toggle_temps", "Temps"),
        ("p", "toggle_power", "Power"),
        ("g", "toggle_gpu_detail", "GPU detail"),
        ("m", "toggle_memory", "Memory"),
        ("d", "toggle_proc_detail", "Proc detail"),
    ]

    def __init__(
        self,
        pids: list[int] | None = None,
        interval: float = 2.0,
        top_n: int = 30,
        record_path: str | None = None,
    ) -> None:
        super().__init__()
        self._target_pids = pids
        self._interval = interval
        self._top_n = top_n
        self._prev_snap: dict[int, dict] = {}
        self._prev_cpu: dict[int, int] = {}
        self._prev_energy: dict[int, int] = {}
        self._prev_time: float = 0
        self._rows: dict[int, ProcessRow] = {}
        self._all_totals: list[float] = []
        self._record_path = record_path
        self._record_file: object | None = None
        self._record_count: int = 0
        self._power_sampler = PowerSampler(interval=max(interval, 1.0))
        self._show_proc_detail = False
        if record_path:
            self._record_file = open(record_path, "w")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield SummaryBar(id="summary")
        yield SystemGpuBar(id="system-bar")
        yield TempPanel(id="temp-panel")
        yield PowerPanel(id="power-panel")
        yield GpuDetailPanel(id="gpu-detail-panel")
        yield MemoryPanel(id="memory-panel")
        yield Static(
            "      PID  GPU %  CPU %    Mem  Power  "
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  "
            "Process             History",
            id="header-row",
        )
        yield Vertical(id="process-list")
        yield Footer()

    def on_mount(self) -> None:
        snap = _snapshot()
        self._prev_snap = snap
        for pid in snap:
            ns = cpu_time_ns(pid)
            self._prev_cpu[pid] = ns if ns >= 0 else 0
            info = proc_info(pid)
            self._prev_energy[pid] = info["energy_nj"] if info else 0
        self._prev_time = _time.monotonic()
        self._power_sampler.start()
        self.set_interval(self._interval, self._refresh)

    def _refresh(self) -> None:
        now = _time.monotonic()
        elapsed_s = now - self._prev_time
        if elapsed_s <= 0:
            return
        elapsed_ns = elapsed_s * 1_000_000_000

        snap = _snapshot()
        curr_cpu: dict[int, int] = {}
        curr_energy: dict[int, int] = {}
        proc_details: dict[int, dict] = {}
        for pid in snap:
            ns = cpu_time_ns(pid)
            curr_cpu[pid] = ns if ns >= 0 else 0
            info = proc_info(pid)
            curr_energy[pid] = info["energy_nj"] if info else 0
            if info:
                proc_details[pid] = info

        # Filter to specific PIDs if requested
        pids = set(snap.keys())
        if self._target_pids:
            pids = pids & set(self._target_pids)

        active: list[tuple[int, str, float, float, str, float, dict]] = []
        for pid in pids:
            c_gpu = snap.get(pid, {}).get("gpu_ns", 0)
            p_gpu = self._prev_snap.get(pid, {}).get("gpu_ns", c_gpu)
            gpu_delta = c_gpu - p_gpu

            cpu_delta = curr_cpu.get(pid, 0) - self._prev_cpu.get(pid, curr_cpu.get(pid, 0))
            energy_delta = curr_energy.get(pid, 0) - self._prev_energy.get(pid, curr_energy.get(pid, 0))

            gpu_pct = min(gpu_delta / elapsed_ns * 100, 100) if elapsed_ns > 0 else 0
            cpu_pct = cpu_delta / elapsed_ns * 100 if elapsed_ns > 0 else 0
            power_w = energy_delta / (elapsed_s * 1e9) if elapsed_s > 0 else 0

            info = proc_details.get(pid)
            mem_str = _fmt_bytes(info["memory"]) if info else "0"

            name = snap[pid]["name"]
            if gpu_pct >= 0.05 or gpu_delta > 0:
                active.append((pid, name, gpu_pct, cpu_pct, mem_str, power_w, info or {}))

        self._prev_snap = snap
        self._prev_cpu = curr_cpu
        self._prev_energy = curr_energy
        self._prev_time = now

        active.sort(key=lambda r: r[2], reverse=True)
        active = active[:self._top_n]

        total_pct = sum(r[2] for r in active)
        self._all_totals.append(total_pct)

        # System-wide GPU stats + temperatures
        sys_gpu = system_gpu_stats()
        temps = temperatures()
        sys_mem = system_stats()

        # Power data (cached from background thread)
        cpu_pwr, gpu_pwr = self._power_sampler.get()

        summary = self.query_one("#summary", SummaryBar)
        summary.total_gpu = total_pct
        summary.process_count = len(active)
        summary.peak_gpu = max(self._all_totals) if self._all_totals else 0
        summary.model_name = sys_gpu.get("model", "?")
        summary.core_count = sys_gpu.get("gpu_core_count", 0)
        summary.device_util = sys_gpu.get("device_utilization", 0)
        summary.cpu_temp = temps.get("cpu_avg", 0)
        summary.gpu_temp = temps.get("gpu_avg", 0)
        summary.recording = self._record_path if self._record_file else ""

        temp_panel = self.query_one("#temp-panel", TempPanel)
        if temp_panel.has_class("visible"):
            temp_panel.update_temps(temps)

        power_panel = self.query_one("#power-panel", PowerPanel)
        if power_panel.has_class("visible"):
            power_panel.update_power(cpu_pwr, gpu_pwr)

        gpu_detail = self.query_one("#gpu-detail-panel", GpuDetailPanel)
        if gpu_detail.has_class("visible"):
            gpu_detail.update_gpu_detail(sys_gpu)

        mem_panel = self.query_one("#memory-panel", MemoryPanel)
        if mem_panel.has_class("visible"):
            mem_panel.update_memory(sys_mem)

        # Write recording line if active
        if self._record_file:
            record = {
                "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "epoch": _time.time(),
                "interval": self._interval,
                "temperatures": temps,
                "memory": sys_mem,
                "gpu_stats": sys_gpu,
                "cpu_power": cpu_pwr,
                "gpu_power": gpu_pwr,
                "processes": [
                    {
                        "pid": pid, "name": name,
                        "gpu_percent": round(gpu_pct, 1),
                        "cpu_percent": round(cpu_pct, 1),
                        "memory": mem_str,
                        "energy_w": round(power_w, 2),
                        **(
                            {
                                "threads": detail.get("threads", 0),
                                "disk_read_bytes": detail.get("disk_read_bytes", 0),
                                "disk_write_bytes": detail.get("disk_write_bytes", 0),
                                "instructions": detail.get("instructions", 0),
                                "cycles": detail.get("cycles", 0),
                                "peak_memory": detail.get("peak_memory", 0),
                                "wired_size": detail.get("wired_size", 0),
                                "neural_footprint": detail.get("neural_footprint", 0),
                                "idle_wakeups": detail.get("idle_wakeups", 0),
                                "interrupt_wakeups": detail.get("interrupt_wakeups", 0),
                                "pageins": detail.get("pageins", 0),
                            }
                            if detail else {}
                        ),
                    }
                    for pid, name, gpu_pct, cpu_pct, mem_str, power_w, detail in active
                ],
            }
            self._record_file.write(json.dumps(record) + "\n")
            self._record_file.flush()
            self._record_count += 1

        sys_bar = self.query_one("#system-bar", SystemGpuBar)
        sys_bar.update_value(
            sys_gpu.get("device_utilization", 0),
            sys_gpu.get("alloc_system_memory", 0),
            sys_gpu.get("in_use_system_memory", 0),
        )

        container = self.query_one("#process-list")
        active_pids = {r[0] for r in active}

        for pid in list(self._rows.keys()):
            if pid not in active_pids:
                self._rows[pid].remove()
                del self._rows[pid]

        for pid, name, gpu_pct, cpu_pct, mem_str, power_w, detail in active:
            if pid in self._rows:
                row = self._rows[pid]
                row._show_detail = self._show_proc_detail
                row.update_stats(gpu_pct, cpu_pct, mem_str, power_w, detail)
            else:
                row = ProcessRow(pid, name)
                row._show_detail = self._show_proc_detail
                self._rows[pid] = row
                container.mount(row)
                row.update_stats(gpu_pct, cpu_pct, mem_str, power_w, detail)

    def action_reset(self) -> None:
        """Reset all history."""
        self._all_totals.clear()
        for row in self._rows.values():
            row.history.clear()
        sys_bar = self.query_one("#system-bar", SystemGpuBar)
        sys_bar.history.clear()

    def action_toggle_temps(self) -> None:
        """Toggle temperature sensor detail panel."""
        panel = self.query_one("#temp-panel", TempPanel)
        panel.toggle_class("visible")
        if panel.has_class("visible"):
            panel.update_temps(temperatures())

    def action_toggle_power(self) -> None:
        """Toggle power & frequency panel."""
        panel = self.query_one("#power-panel", PowerPanel)
        panel.toggle_class("visible")
        if panel.has_class("visible"):
            cpu_pwr, gpu_pwr = self._power_sampler.get()
            panel.update_power(cpu_pwr, gpu_pwr)

    def action_toggle_gpu_detail(self) -> None:
        """Toggle GPU detail panel."""
        panel = self.query_one("#gpu-detail-panel", GpuDetailPanel)
        panel.toggle_class("visible")
        if panel.has_class("visible"):
            panel.update_gpu_detail(system_gpu_stats())

    def action_toggle_memory(self) -> None:
        """Toggle memory breakdown panel."""
        panel = self.query_one("#memory-panel", MemoryPanel)
        panel.toggle_class("visible")
        if panel.has_class("visible"):
            panel.update_memory(system_stats())

    def action_toggle_proc_detail(self) -> None:
        """Toggle per-process detail rows."""
        self._show_proc_detail = not self._show_proc_detail
        for row in self._rows.values():
            row._show_detail = self._show_proc_detail
            row.refresh_display()

    def action_toggle_record(self) -> None:
        """Toggle recording on/off."""
        if self._record_file:
            self._record_file.close()
            self._record_file = None
            self.notify(
                f"Recording stopped ({self._record_count} samples → {self._record_path})",
                severity="information",
            )
        else:
            import time
            self._record_path = f"darwin-perf-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
            self._record_file = open(self._record_path, "w")
            self._record_count = 0
            self.notify(f"Recording to {self._record_path}", severity="warning")

    def on_unmount(self) -> None:
        self._power_sampler.stop()
        if self._record_file:
            self._record_file.close()


def run_tui(
    pids: list[int] | None = None,
    interval: float = 2.0,
    top_n: int = 30,
    record_path: str | None = None,
) -> None:
    """Launch the TUI app."""
    app = GpuProcApp(pids=pids, interval=interval, top_n=top_n, record_path=record_path)
    app.run()
