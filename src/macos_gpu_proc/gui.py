"""gpu-proc GUI: Native floating window GPU monitor.

A compact, resizable native window showing live system GPU utilization,
per-process CPU/memory, and history charts. Designed to tuck in a corner
while training.

Uses pywebview for a native macOS window (no browser chrome).

Usage:
    gpu-proc --gui
    gpu-proc --gui -i 1
"""

from __future__ import annotations

import json
import os
import threading
import time

_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: #0f172a; color: #e2e8f0; font-family: -apple-system, system-ui, sans-serif;
    font-size: 12px; padding: 8px; overflow: hidden; height: 100vh;
    display: flex; flex-direction: column;
}
.header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 4px 0 8px; border-bottom: 1px solid #1e293b; margin-bottom: 8px;
    flex-shrink: 0;
}
.title { font-size: 13px; font-weight: 600; color: #94a3b8; }
.stats { font-size: 11px; color: #64748b; }
.stats b { color: #10b981; }

.gpu-section { flex-shrink: 0; margin-bottom: 8px; }
.section-label { font-size: 10px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
.bar-row { display: flex; align-items: center; gap: 6px; margin-bottom: 3px; }
.bar-label { font-size: 11px; color: #94a3b8; width: 50px; flex-shrink: 0; }
.bar-track { flex: 1; height: 6px; background: #1e293b; border-radius: 3px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 3px; transition: width 0.5s ease; }
.bar-value { font-size: 11px; font-family: 'SF Mono', 'Menlo', monospace; color: #94a3b8; width: 55px; text-align: right; flex-shrink: 0; }

.fill-gpu { background: #8b5cf6; }
.fill-cpu { background: #64748b; }
.fill-mem { background: #10b981; }
.fill-proc { background: #10b981; }

canvas {
    flex: 1; min-height: 60px; width: 100%; border-radius: 4px;
    background: #1e293b; margin-top: 4px;
}
.proc-list { flex: 1; overflow-y: auto; min-height: 0; margin-top: 6px; }
.proc-row {
    display: flex; align-items: center; gap: 6px; padding: 2px 0;
    border-bottom: 1px solid #1e293b20;
}
.proc-name { font-size: 11px; color: #cbd5e1; width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.proc-pid { font-size: 10px; color: #475569; width: 50px; text-align: right; font-family: monospace; }
.proc-bar { flex: 1; height: 4px; background: #1e293b; border-radius: 2px; overflow: hidden; }
.proc-fill { height: 100%; background: #10b981; border-radius: 2px; transition: width 0.5s; }
.proc-val { font-size: 10px; color: #94a3b8; width: 50px; text-align: right; font-family: monospace; }
</style>
</head>
<body>
<div class="header">
    <span class="title">gpu-proc</span>
    <span class="stats" id="stats">--</span>
</div>

<div class="gpu-section">
    <div class="bar-row">
        <span class="bar-label">GPU</span>
        <div class="bar-track"><div class="bar-fill fill-gpu" id="gpu-bar" style="width:0%"></div></div>
        <span class="bar-value" id="gpu-val">--%</span>
    </div>
    <div class="bar-row">
        <span class="bar-label">CPU</span>
        <div class="bar-track"><div class="bar-fill fill-cpu" id="cpu-bar" style="width:0%"></div></div>
        <span class="bar-value" id="cpu-val">--%</span>
    </div>
    <div class="bar-row">
        <span class="bar-label">RAM</span>
        <div class="bar-track"><div class="bar-fill fill-mem" id="ram-bar" style="width:0%"></div></div>
        <span class="bar-value" id="ram-val">--</span>
    </div>
</div>

<div class="section-label">GPU History</div>
<canvas id="chart"></canvas>

<div class="section-label" style="margin-top:6px">Top Processes (CPU)</div>
<div class="proc-list" id="procs"></div>

<script>
const gpuHistory = [];
const MAX_HIST = 120;
const canvas = document.getElementById('chart');
const ctx = canvas.getContext('2d');

function drawChart() {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    if (gpuHistory.length < 2) return;

    const maxV = Math.max(100, ...gpuHistory);
    ctx.beginPath();
    ctx.moveTo(0, h);
    for (let i = 0; i < gpuHistory.length; i++) {
        const x = (i / (MAX_HIST - 1)) * w;
        const y = h - (gpuHistory[i] / maxV) * h;
        ctx.lineTo(x, y);
    }
    ctx.lineTo((gpuHistory.length - 1) / (MAX_HIST - 1) * w, h);
    ctx.closePath();
    ctx.fillStyle = 'rgba(139, 92, 246, 0.15)';
    ctx.fill();

    ctx.beginPath();
    for (let i = 0; i < gpuHistory.length; i++) {
        const x = (i / (MAX_HIST - 1)) * w;
        const y = h - (gpuHistory[i] / maxV) * h;
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.strokeStyle = '#8b5cf6';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Grid lines
    ctx.strokeStyle = '#1e293b80';
    ctx.lineWidth = 0.5;
    for (let pct of [25, 50, 75]) {
        const y = h - (pct / maxV) * h;
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
}

function update(data) {
    // System bars
    const gpu = data.gpu_percent ?? 0;
    const cpu = data.cpu_percent ?? 0;
    const ramPct = data.memory_total_gb > 0 ? (data.memory_used_gb / data.memory_total_gb) * 100 : 0;

    document.getElementById('gpu-bar').style.width = gpu + '%';
    document.getElementById('gpu-val').textContent = gpu.toFixed(1) + '%';
    document.getElementById('cpu-bar').style.width = Math.min(cpu, 100) + '%';
    document.getElementById('cpu-val').textContent = cpu.toFixed(0) + '%';
    document.getElementById('ram-bar').style.width = ramPct + '%';
    document.getElementById('ram-val').textContent = data.memory_used_gb.toFixed(0) + '/' + data.memory_total_gb.toFixed(0) + 'GB';

    // Stats
    const pw = data.gpu_power_w != null ? data.gpu_power_w.toFixed(0) + 'W' : '';
    const freq = data.gpu_freq_mhz != null ? data.gpu_freq_mhz.toFixed(0) + 'MHz' : '';
    const parts = [pw, freq].filter(Boolean).join(' / ');
    document.getElementById('stats').innerHTML = '<b>' + parts + '</b>' + (data.gpu_name ? '  ' + data.gpu_name : '');

    // History
    gpuHistory.push(gpu);
    if (gpuHistory.length > MAX_HIST) gpuHistory.shift();
    drawChart();

    // Process list
    const procs = data.processes || [];
    const el = document.getElementById('procs');
    el.innerHTML = procs.slice(0, 15).map(p =>
        '<div class="proc-row">' +
        '<span class="proc-pid">' + p.pid + '</span>' +
        '<span class="proc-name">' + p.name + '</span>' +
        '<div class="proc-bar"><div class="proc-fill" style="width:' + Math.min(p.cpu, 100) + '%"></div></div>' +
        '<span class="proc-val">' + p.cpu.toFixed(1) + '% cpu</span>' +
        '<span class="proc-val">' + p.mem.toFixed(1) + 'GB</span>' +
        '</div>'
    ).join('');
}

window.addEventListener('resize', drawChart);
</script>
</body>
</html>"""


class _GpuGuiApi:
    """Python API exposed to the webview JS context."""

    def __init__(self, interval: float = 2.0) -> None:
        self.interval = interval
        self._window = None

    def set_window(self, window: object) -> None:
        self._window = window

    def start_polling(self) -> None:
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()

    def _collect(self) -> dict:
        import platform
        import re
        import subprocess

        data: dict = {}

        # GPU via powermetrics (macOS) or nvidia-smi (Linux)
        gpu: dict[str, float] = {}
        if platform.system().lower() == "darwin":
            try:
                result = subprocess.run(
                    ["sudo", "powermetrics", "--samplers", "gpu_power", "-i", "500", "-n", "1"],
                    capture_output=True, text=True, timeout=3,
                )
                if result.returncode == 0:
                    out = result.stdout
                    m = re.search(r"GPU HW active residency:\s+([0-9]+(?:\.[0-9]+)?)%", out)
                    if m: gpu["gpu_active_pct"] = float(m.group(1))
                    m = re.search(r"GPU HW active frequency:\s+([0-9]+)\s*MHz", out)
                    if m: gpu["gpu_freq_mhz"] = float(m.group(1))
                    m = re.search(r"GPU Power:\s+([0-9]+)\s*mW", out)
                    if m: gpu["gpu_power_mw"] = float(m.group(1))
            except Exception:
                pass
        data["gpu_percent"] = gpu.get("gpu_active_pct")
        data["gpu_power_w"] = gpu.get("gpu_power_mw", 0) / 1000 if gpu.get("gpu_power_mw") else None
        data["gpu_freq_mhz"] = gpu.get("gpu_freq_mhz")

        # System stats via psutil
        try:
            import psutil
            mem = psutil.virtual_memory()
            data["memory_total_gb"] = round(mem.total / (1024**3), 1)
            data["memory_used_gb"] = round(mem.used / (1024**3), 1)
            data["cpu_percent"] = psutil.cpu_percent(interval=None)
            data["gpu_name"] = None

            # Top processes by CPU
            procs = []
            for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
                try:
                    info = p.info
                    cpu = info.get("cpu_percent") or 0
                    mem_info = info.get("memory_info")
                    mem_gb = round(mem_info.rss / (1024**3), 2) if mem_info else 0
                    if cpu > 0.1 or mem_gb > 0.1:
                        procs.append({
                            "pid": info["pid"],
                            "name": info["name"] or "?",
                            "cpu": cpu,
                            "mem": mem_gb,
                        })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            procs.sort(key=lambda p: p["cpu"], reverse=True)
            data["processes"] = procs[:15]
        except ImportError:
            data["memory_total_gb"] = 0
            data["memory_used_gb"] = 0
            data["cpu_percent"] = 0
            data["processes"] = []

        return data

    def _poll_loop(self) -> None:
        # Warm up psutil cpu_percent
        try:
            import psutil
            psutil.cpu_percent(interval=0.5)
        except Exception:
            pass

        while True:
            try:
                data = self._collect()
                if self._window:
                    js = f"update({json.dumps(data)})"
                    self._window.evaluate_js(js)
            except Exception as e:
                print(f"Poll error: {e}")
            time.sleep(self.interval)


def run_gui(interval: float = 2.0, width: int = 380, height: int = 520) -> None:
    """Launch native floating GPU monitor window."""
    import webview  # type: ignore

    api = _GpuGuiApi(interval=interval)
    window = webview.create_window(
        "gpu-proc",
        html=_HTML,
        width=width,
        height=height,
        resizable=True,
        on_top=True,
        frameless=False,
    )
    api.set_window(window)
    window.events.loaded += api.start_polling
    webview.start()
