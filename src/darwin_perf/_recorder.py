"""Time-series performance recorder with context markers and anomaly detection.

Records system stats at a configurable interval, links samples to application
code via span markers, and detects anomalous patterns (GPU oscillation,
dropout, memory pressure stalls).

Usage::

    import darwin_perf as dp

    rec = dp.Recorder(interval=0.5)
    rec.start()

    with rec.span("data_load"):
        load_data()

    for epoch in range(100):
        with rec.span("train_epoch", epoch=epoch):
            train()

    rec.stop()
    report = rec.report()
    print(report["anomalies"])  # detected issues
    rec.save("profile.json")   # full time-series + spans + anomalies
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Sample:
    """A single timestamped metrics sample."""
    timestamp: float  # monotonic seconds since recorder start
    wall_time: float  # epoch time for display
    gpu_util_pct: float = 0.0
    cpu_pct: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    gpu_power_w: float | None = None
    gpu_freq_mhz: float | None = None
    proc_gpu_pct: float = 0.0
    proc_cpu_pct: float = 0.0
    proc_memory_gb: float = 0.0
    net_bytes_sent: int = 0
    net_bytes_recv: int = 0
    net_connections: int = 0
    span: str | None = None  # active span name at sample time
    span_meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "t": round(self.timestamp, 3),
            "wall": round(self.wall_time, 3),
            "gpu_pct": round(self.gpu_util_pct, 1),
            "cpu_pct": round(self.cpu_pct, 1),
            "ram_gb": round(self.ram_used_gb, 1),
            "proc_gpu_pct": round(self.proc_gpu_pct, 1),
            "proc_cpu_pct": round(self.proc_cpu_pct, 1),
            "proc_mem_gb": round(self.proc_memory_gb, 1),
            "net_sent": self.net_bytes_sent,
            "net_recv": self.net_bytes_recv,
            "net_conns": self.net_connections,
        }
        if self.gpu_power_w is not None:
            d["gpu_w"] = round(self.gpu_power_w, 1)
        if self.gpu_freq_mhz is not None:
            d["gpu_mhz"] = round(self.gpu_freq_mhz)
        if self.span:
            d["span"] = self.span
            if self.span_meta:
                d["span_meta"] = self.span_meta
        return d


@dataclass
class SpanRecord:
    """A completed application span."""
    name: str
    start: float   # monotonic offset
    end: float     # monotonic offset
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "name": self.name,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration_s": round(self.duration, 3),
        }
        if self.meta:
            d["meta"] = self.meta
        return d


@dataclass
class Anomaly:
    """A detected performance anomaly."""
    kind: str           # "gpu_oscillation", "gpu_dropout", "memory_pressure", "gpu_sustained_low"
    start: float        # monotonic offset
    end: float          # monotonic offset
    severity: str       # "info", "warning", "critical"
    description: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration_s": round(self.end - self.start, 3),
            "severity": self.severity,
            "description": self.description,
            "details": self.details,
        }


class Recorder:
    """Time-series system performance recorder with span markers.

    Args:
        interval: Seconds between samples (default 0.5).
        pid: Process to monitor (0 = current, None = system only).
    """

    def __init__(self, interval: float = 0.5, pid: int = 0) -> None:
        self.interval = interval
        self.pid = pid
        self._samples: list[Sample] = []
        self._spans: list[SpanRecord] = []
        self._active_span: str | None = None
        self._active_span_meta: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0: float = 0.0
        self._wall_t0: float = 0.0
        # Live anomaly tracking state
        self._dropout_start: float | None = None
        self._prev_gpu: float = 100.0
        self._live_anomalies: list[dict[str, Any]] = []
        self._pressure_captured_at: float | None = None

    def start(self) -> None:
        """Start background sampling."""
        if self._thread is not None:
            return
        self._t0 = time.monotonic()
        self._wall_t0 = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop background sampling."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 2)
            self._thread = None

    def __enter__(self) -> Recorder:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @contextmanager
    def span(self, name: str, **meta: Any):
        """Mark a section of application code for correlation with metrics.

        Usage::

            with recorder.span("train_step", epoch=5, batch=32):
                # ... your code ...

        Samples taken during this span will have span=name in their data.
        After the span completes, it's recorded with start/end/duration.
        """
        start = time.monotonic() - self._t0
        with self._lock:
            self._active_span = name
            self._active_span_meta = dict(meta)
        try:
            yield
        finally:
            end = time.monotonic() - self._t0
            with self._lock:
                self._active_span = None
                self._active_span_meta = {}
                self._spans.append(SpanRecord(
                    name=name, start=start, end=end, meta=dict(meta),
                ))

    def mark(self, name: str, **meta: Any) -> None:
        """Insert a point-in-time marker (zero-duration span)."""
        t = time.monotonic() - self._t0
        with self._lock:
            self._spans.append(SpanRecord(
                name=name, start=t, end=t, meta=dict(meta),
            ))

    def _loop(self) -> None:
        from ._native import proc_info, system_gpu_stats, system_stats

        # Optional: GpuMonitor for per-process GPU%
        gpu_mon = None
        if self.pid is not None:
            from . import GpuMonitor
            actual_pid = self.pid if self.pid != 0 else os.getpid()
            gpu_mon = GpuMonitor(pid=actual_pid, children=True)
            gpu_mon.sample()  # baseline

        # Network baseline (optional — psutil may not be installed)
        prev_net = None
        try:
            from ._network import network_snapshot
            prev_net = network_snapshot()
        except Exception:
            pass

        while not self._stop.wait(self.interval):
            try:
                now = time.monotonic()
                t = now - self._t0
                wall = self._wall_t0 + t

                # System stats (instant, ~3µs)
                sys = system_stats()
                gpu = system_gpu_stats()

                # Per-process (instant, no sleep)
                proc_cpu = 0.0
                proc_mem = 0.0
                proc_gpu = 0.0
                if self.pid is not None:
                    actual_pid = self.pid if self.pid != 0 else os.getpid()
                    pi = proc_info(actual_pid)
                    if pi:
                        proc_mem = pi.get("real_memory", 0) / 1e9
                    if gpu_mon is not None:
                        proc_gpu = gpu_mon.sample()

                # Network stats (delta since last sample)
                net_sent = 0
                net_recv = 0
                net_conns = 0
                try:
                    from ._network import network_delta, network_snapshot
                    curr_net = network_snapshot()
                    if prev_net is not None:
                        nd = network_delta(prev_net, curr_net)
                        net_sent = nd.bytes_sent
                        net_recv = nd.bytes_recv
                        net_conns = len(nd.active_connections)
                    prev_net = curr_net
                except Exception:
                    pass

                with self._lock:
                    span_name = self._active_span
                    span_meta = dict(self._active_span_meta) if self._active_span_meta else {}

                sample = Sample(
                    timestamp=t,
                    wall_time=wall,
                    gpu_util_pct=gpu.get("device_utilization", 0),
                    cpu_pct=sys.get("cpu_pct", 0),
                    ram_used_gb=sys.get("memory_used", 0) / 1e9,
                    ram_total_gb=sys.get("memory_total", 0) / 1e9,
                    proc_gpu_pct=proc_gpu,
                    proc_cpu_pct=proc_cpu,
                    proc_memory_gb=proc_mem,
                    net_bytes_sent=net_sent,
                    net_bytes_recv=net_recv,
                    net_connections=net_conns,
                    span=span_name,
                    span_meta=span_meta,
                )

                with self._lock:
                    self._samples.append(sample)

                # --- Live anomaly detection ---
                # When GPU drops or RAM spikes, immediately capture a detailed
                # system snapshot with temps, power, all GPU clients, per-core CPU.
                self._check_live_anomaly(sample, t)

            except Exception:
                pass

    def _check_live_anomaly(self, sample: Sample, t: float) -> None:
        """Detect anomalies in real-time and capture detailed snapshots."""
        gpu = sample.gpu_util_pct
        ram_ratio = sample.ram_used_gb / max(sample.ram_total_gb, 1)

        if gpu <= 1.0:
            if self._dropout_start is None:
                self._dropout_start = t
        else:
            if self._dropout_start is not None and (t - self._dropout_start) >= 1.0:
                self._capture_anomaly_snapshot(
                    kind="gpu_dropout",
                    start=self._dropout_start,
                    end=t,
                    sample=sample,
                    ram_ratio=ram_ratio,
                )
            self._dropout_start = None

        # Memory pressure: RAM >90% — capture before it causes a dropout
        if ram_ratio > 0.90 and self._pressure_captured_at is None:
            self._pressure_captured_at = t
            self._capture_anomaly_snapshot(
                kind="memory_pressure_live",
                start=t,
                end=t,
                sample=sample,
                ram_ratio=ram_ratio,
            )
        elif ram_ratio <= 0.85:
            self._pressure_captured_at = None

        self._prev_gpu = gpu

    def _capture_anomaly_snapshot(
        self, kind: str, start: float, end: float,
        sample: Sample, ram_ratio: float,
    ) -> None:
        """Capture a detailed system snapshot at the moment of anomaly."""
        snapshot: dict[str, Any] = {
            "kind": kind,
            "timestamp": round(end, 3),
            "duration_s": round(end - start, 3),
            "sample": sample.to_dict(),
            "ram_pressure_pct": round(ram_ratio * 100, 1),
        }

        # Detailed snapshot — temperatures, power, GPU clients, per-core CPU
        try:
            from ._native import system_stats, temperatures as get_temps
            temps = get_temps()
            snapshot["temperatures"] = {
                "cpu_avg": round(temps.get("cpu_avg", 0), 1),
                "gpu_avg": round(temps.get("gpu_avg", 0), 1),
                "sensors": {k: round(v, 1) for k, v in temps.items()
                            if k not in ("cpu_avg", "gpu_avg", "system_avg")},
            }
        except Exception:
            pass

        try:
            from ._native import gpu_clients
            clients = gpu_clients()
            snapshot["gpu_clients"] = [
                {"pid": c["pid"], "name": c["name"], "gpu_ns": c["gpu_ns"]}
                for c in clients[:10]  # top 10
            ]
        except Exception:
            pass

        try:
            sys = system_stats()
            per_core = sys.get("per_core", [])
            if per_core:
                snapshot["per_core_cpu"] = [
                    {"core": c["core"], "active_pct": round(
                        100 * (c.get("ticks_user", 0) + c.get("ticks_system", 0)) /
                        max(c.get("ticks_user", 0) + c.get("ticks_system", 0) + c.get("ticks_idle", 1), 1),
                        1,
                    )}
                    for c in per_core
                ]
        except Exception:
            pass

        # Recent samples for context (last 5)
        with self._lock:
            recent = [s.to_dict() for s in self._samples[-5:]]
        snapshot["recent_samples"] = recent

        self._live_anomalies.append(snapshot)

    @property
    def live_anomalies(self) -> list[dict]:
        """Return anomalies detected during live sampling."""
        return self._live_anomalies

    def samples(self) -> list[Sample]:
        """Return a copy of all samples."""
        with self._lock:
            return list(self._samples)

    def detect_anomalies(self) -> list[Anomaly]:
        """Analyze recorded samples for performance anomalies.

        Detects:
            - gpu_oscillation: GPU alternates between high (>70%) and low (<20%)
              repeatedly over short intervals (the "on/off" pattern)
            - gpu_dropout: GPU drops to 0% for >2 seconds
            - memory_pressure: RAM usage >95% of total, correlated with GPU drops
            - gpu_sustained_low: GPU stays below 30% for >10 seconds during a
              span that should be GPU-intensive
        """
        with self._lock:
            samples = list(self._samples)

        if len(samples) < 4:
            return []

        anomalies: list[Anomaly] = []

        # --- GPU Oscillation Detection ---
        # Look for alternating high/low pattern with period 2-10 seconds
        gpu_vals = [(s.timestamp, s.gpu_util_pct) for s in samples]
        HIGH_THRESH = 70.0
        LOW_THRESH = 20.0

        # Classify each sample as high/low/mid
        states = []
        for t, g in gpu_vals:
            if g >= HIGH_THRESH:
                states.append((t, "high"))
            elif g <= LOW_THRESH:
                states.append((t, "low"))
            else:
                states.append((t, "mid"))

        # Find transitions between high and low
        transitions = []
        prev_state = None
        for t, state in states:
            if state in ("high", "low") and state != prev_state and prev_state is not None:
                transitions.append((t, prev_state, state))
            if state in ("high", "low"):
                prev_state = state

        # 4+ transitions in a short window = oscillation
        if len(transitions) >= 4:
            # Sliding window: find clusters of transitions
            window_size = 30.0  # seconds
            for i in range(len(transitions)):
                window_end = transitions[i][0] + window_size
                cluster = [tr for tr in transitions[i:] if tr[0] <= window_end]
                if len(cluster) >= 4:
                    period = (cluster[-1][0] - cluster[0][0]) / len(cluster)
                    anomalies.append(Anomaly(
                        kind="gpu_oscillation",
                        start=cluster[0][0],
                        end=cluster[-1][0],
                        severity="warning",
                        description=(
                            f"GPU oscillating between high ({HIGH_THRESH}%+) and low (<{LOW_THRESH}%) "
                            f"with ~{period:.1f}s period over {cluster[-1][0] - cluster[0][0]:.0f}s"
                        ),
                        details={
                            "transitions": len(cluster),
                            "period_s": round(period, 1),
                            "spans_active": list({
                                s.span for s in samples
                                if s.span and cluster[0][0] <= s.timestamp <= cluster[-1][0]
                            }),
                        },
                    ))
                    break  # report first oscillation window

        # --- GPU Dropout Detection ---
        # GPU at 0% for >2 consecutive seconds
        dropout_start = None
        for s in samples:
            if s.gpu_util_pct <= 1.0:
                if dropout_start is None:
                    dropout_start = s.timestamp
            else:
                if dropout_start is not None:
                    duration = s.timestamp - dropout_start
                    if duration >= 2.0:
                        # Check if memory pressure during dropout
                        pressure = any(
                            sa.ram_used_gb / max(sa.ram_total_gb, 1) > 0.95
                            for sa in samples
                            if dropout_start <= sa.timestamp <= s.timestamp
                        )
                        # Gather context: 3 samples before, during, and 3 after
                        context_before = [
                            sa.to_dict() for sa in samples
                            if dropout_start - 5.0 <= sa.timestamp < dropout_start
                        ][-3:]
                        context_during = [
                            sa.to_dict() for sa in samples
                            if dropout_start <= sa.timestamp <= s.timestamp
                        ]
                        context_after = [
                            sa.to_dict() for sa in samples
                            if s.timestamp < sa.timestamp <= s.timestamp + 5.0
                        ][:3]
                        # RAM stats during dropout
                        ram_during = [
                            sa.ram_used_gb for sa in samples
                            if dropout_start <= sa.timestamp <= s.timestamp
                        ]
                        anomalies.append(Anomaly(
                            kind="gpu_dropout",
                            start=dropout_start,
                            end=s.timestamp,
                            severity="critical" if duration >= 5.0 else "warning",
                            description=(
                                f"GPU at 0% for {duration:.1f}s"
                                + (" (memory pressure detected)" if pressure else "")
                            ),
                            details={
                                "duration_s": round(duration, 1),
                                "memory_pressure": pressure,
                                "ram_max_gb": round(max(ram_during), 1) if ram_during else None,
                                "ram_avg_gb": round(sum(ram_during) / len(ram_during), 1) if ram_during else None,
                                "span": next(
                                    (sa.span for sa in samples
                                     if sa.span and dropout_start <= sa.timestamp <= s.timestamp),
                                    None,
                                ),
                                "context_before": context_before,
                                "context_during": context_during,
                                "context_after": context_after,
                            },
                        ))
                    dropout_start = None

        # --- Memory Pressure Detection ---
        # RAM >95% for >5 seconds with GPU drops
        pressure_start = None
        for s in samples:
            ratio = s.ram_used_gb / max(s.ram_total_gb, 1)
            if ratio > 0.95:
                if pressure_start is None:
                    pressure_start = s.timestamp
            else:
                if pressure_start is not None:
                    duration = s.timestamp - pressure_start
                    if duration >= 5.0:
                        # Check for GPU drops during pressure
                        gpu_during = [
                            sa.gpu_util_pct for sa in samples
                            if pressure_start <= sa.timestamp <= s.timestamp
                        ]
                        min_gpu = min(gpu_during) if gpu_during else 0
                        avg_gpu = sum(gpu_during) / len(gpu_during) if gpu_during else 0
                        if min_gpu < 20:
                            anomalies.append(Anomaly(
                                kind="memory_pressure",
                                start=pressure_start,
                                end=s.timestamp,
                                severity="critical",
                                description=(
                                    f"RAM >95% for {duration:.0f}s, GPU dropped to {min_gpu:.0f}% "
                                    f"(avg {avg_gpu:.0f}%)"
                                ),
                                details={
                                    "duration_s": round(duration, 1),
                                    "ram_pct": round(ratio * 100, 1),
                                    "gpu_min_pct": round(min_gpu, 1),
                                    "gpu_avg_pct": round(avg_gpu, 1),
                                },
                            ))
                    pressure_start = None

        # --- Sustained Low GPU ---
        # GPU <30% for >10s while inside a span (indicates the span isn't using GPU)
        low_start = None
        for s in samples:
            if s.gpu_util_pct < 30 and s.span:
                if low_start is None:
                    low_start = s.timestamp
            else:
                if low_start is not None:
                    duration = s.timestamp - low_start
                    if duration >= 10.0:
                        anomalies.append(Anomaly(
                            kind="gpu_sustained_low",
                            start=low_start,
                            end=s.timestamp,
                            severity="info",
                            description=f"GPU <30% for {duration:.0f}s during active spans",
                            details={"duration_s": round(duration, 1)},
                        ))
                    low_start = None

        # --- Network Traffic Spike ---
        # Detect sudden bursts in network traffic (>10x average)
        net_totals = [s.net_bytes_sent + s.net_bytes_recv for s in samples]
        if net_totals and max(net_totals) > 0:
            avg_net = sum(net_totals) / len(net_totals)
            if avg_net > 0:
                for i, s in enumerate(samples):
                    total = net_totals[i]
                    if total > avg_net * 10 and total > 1024 * 1024:  # >10x avg and >1MB
                        anomalies.append(Anomaly(
                            kind="network_spike",
                            start=s.timestamp,
                            end=s.timestamp,
                            severity="warning",
                            description=(
                                f"Network traffic spike: {total / 1024 / 1024:.1f} MB "
                                f"({total / avg_net:.0f}x average)"
                            ),
                            details={
                                "bytes": total,
                                "avg_bytes": round(avg_net),
                                "multiplier": round(total / avg_net, 1),
                            },
                        ))

        return anomalies

    def report(self) -> dict[str, Any]:
        """Generate a full profiling report.

        Returns:
            Dict with keys: summary, anomalies, spans, samples_count,
            duration_s, gpu_avg_pct, gpu_min_pct, gpu_max_pct.
        """
        with self._lock:
            samples = list(self._samples)
            spans = list(self._spans)

        anomalies = self.detect_anomalies()

        if not samples:
            return {
                "summary": "No samples recorded",
                "anomalies": [],
                "spans": [],
                "samples_count": 0,
            }

        duration = samples[-1].timestamp - samples[0].timestamp if len(samples) > 1 else 0
        gpu_vals = [s.gpu_util_pct for s in samples]
        ram_vals = [s.ram_used_gb for s in samples]

        # Per-span GPU stats
        span_stats: dict[str, dict[str, Any]] = {}
        for sp in spans:
            span_samples = [s for s in samples if sp.start <= s.timestamp <= sp.end]
            if span_samples:
                sp_gpu = [s.gpu_util_pct for s in span_samples]
                key = sp.name
                if key not in span_stats:
                    span_stats[key] = {
                        "count": 0, "total_duration_s": 0, "gpu_avgs": [],
                    }
                span_stats[key]["count"] += 1
                span_stats[key]["total_duration_s"] += sp.duration
                span_stats[key]["gpu_avgs"].append(sum(sp_gpu) / len(sp_gpu))

        span_summary = {}
        for name, ss in span_stats.items():
            avg_gpu = sum(ss["gpu_avgs"]) / len(ss["gpu_avgs"]) if ss["gpu_avgs"] else 0
            span_summary[name] = {
                "count": ss["count"],
                "total_duration_s": round(ss["total_duration_s"], 1),
                "avg_gpu_pct": round(avg_gpu, 1),
            }

        return {
            "duration_s": round(duration, 1),
            "samples_count": len(samples),
            "gpu_avg_pct": round(sum(gpu_vals) / len(gpu_vals), 1),
            "gpu_min_pct": round(min(gpu_vals), 1),
            "gpu_max_pct": round(max(gpu_vals), 1),
            "ram_avg_gb": round(sum(ram_vals) / len(ram_vals), 1),
            "ram_max_gb": round(max(ram_vals), 1),
            "anomalies": [a.to_dict() for a in anomalies],
            "spans": span_summary,
            "anomaly_count": len(anomalies),
        }

    def save(self, path: str | Path) -> None:
        """Save full recording to JSON file.

        Includes all samples, spans, and detected anomalies.
        """
        with self._lock:
            samples = list(self._samples)
            spans = list(self._spans)

        anomalies = self.detect_anomalies()
        report = self.report()

        data = {
            "report": report,
            "anomalies": [a.to_dict() for a in anomalies],
            "live_anomaly_snapshots": self.live_anomalies,
            "spans": [s.to_dict() for s in spans],
            "samples": [s.to_dict() for s in samples],
        }

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
