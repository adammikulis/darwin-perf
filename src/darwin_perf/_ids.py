"""Intrusion Detection System for darwin-perf.

Combines rule-based anomaly detection with local LLM analysis via
llama-cpp-python. Monitors system activity for suspicious patterns:

- Network anomalies (unusual traffic, unknown destinations, data exfil)
- Activity during unusual hours (late night, user idle)
- New/unknown processes accessing GPU or network
- Suspicious process behavior (crypto mining signatures, reverse shells)
- Unusual resource consumption patterns
- Port scanning / lateral movement indicators
- Suspicious process lineage chains (sshd -> bash -> nc)
- Sensitive file access by network-active processes
- Unexpected VPN/tunnel interface traffic

The LLM (Qwen3.5-0.6B via GGUF) reads collected anomaly logs and
provides threat assessments, correlating weak signals into alerts.

Usage::

    from darwin_perf._ids import IDSMonitor

    ids = IDSMonitor()
    ids.start()
    # ... runs in background ...
    ids.stop()
    report = ids.report()
    print(report["alerts"])
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ._dns_cache import DNSCache
from ._ids_baseline import BaselineTracker
from ._ids_llm import LLMAnalyzer
from ._lockfile import acquire_ids_lock, get_ids_lock_holder, release_ids_lock
from ._ids_log import RotatingJsonlWriter
from ._ids_rules import (
    Alert,
    Severity,
    _is_private,
    _is_user_idle,
)
from ._ids_detectors import (
    detect_auth_anomalies,
    detect_binary_anomalies,
    detect_file_access_anomalies,
    detect_lineage_anomalies,
    detect_network_anomalies,
    detect_process_anomalies,
    detect_temporal_anomalies,
)

# Re-export for external callers
__all__ = [
    "Alert",
    "BaselineTracker",
    "DNSCache",
    "IDSMonitor",
    "LLMAnalyzer",
    "Severity",
]

logger = logging.getLogger("darwin_perf.ids")


class IDSMonitor:
    """Intrusion Detection System combining rule-based detection with LLM analysis.

    Runs a background thread that periodically:
    1. Collects system metrics (CPU, GPU, memory, network, processes)
    2. Runs rule-based anomaly detectors
    3. Periodically feeds accumulated alerts to the LLM for assessment

    Args:
        interval: Seconds between monitoring cycles (default 5).
        llm_interval: Seconds between LLM analysis runs (default 300 = 5 min).
        baseline_path: Path to save/load baseline data.
        model_path: Override path to GGUF model file.
        enable_llm: Whether to use LLM analysis (default True).
        log_path: Path to write alert log (JSONL).
    """

    def __init__(
        self,
        interval: float = 5.0,
        llm_interval: float = 300.0,
        baseline_path: str | Path | None = None,
        model_path: str | Path | None = None,
        enable_llm: bool = True,
        log_path: str | Path | None = None,
        webhook_url: str | None = None,
        retention_days: int = 30,
    ) -> None:
        self.interval = interval
        self.llm_interval = llm_interval
        self.enable_llm = enable_llm

        # Default paths
        data_dir = Path.home() / ".darwin_perf"
        if baseline_path is None:
            baseline_path = data_dir / "ids_baseline.json"
        if log_path is None:
            log_path = data_dir / "ids_alerts.jsonl"

        self.baseline = BaselineTracker(baseline_path)
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

        # Rotating log writer replaces raw open(..., "a")
        self._log_writer = RotatingJsonlWriter(
            self._log_path,
            retention_days=retention_days,
        )

        # Optional webhook notifier for HIGH/CRITICAL alerts
        self._webhook = None
        if webhook_url:
            from ._ids_webhook import WebhookNotifier
            self._webhook = WebhookNotifier(webhook_url)

        self._llm: LLMAnalyzer | None = None
        if enable_llm:
            self._llm = LLMAnalyzer(model_path=model_path)

        # Passive DNS cache for hostname enrichment on network alerts
        self._dns_cache = DNSCache()

        self._alerts: list[Alert] = []
        self._pending_llm_alerts: list[Alert] = []
        self._llm_assessments: list[dict] = []
        self._dedup_window: dict[str, float] = {}  # rule_key -> last_seen_time
        self._dedup_ttl: float = 30.0  # seconds to suppress duplicate alerts
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._llm_thread: threading.Thread | None = None
        self._last_llm_run: float = 0
        self._prev_net_snap = None
        self._prev_iface_counters: dict[str, dict] | None = None
        self._cycle_count = 0

    def start(self, source: str = "library") -> None:
        """Start IDS monitoring in background.

        Args:
            source: Who is starting ("cli", "daemon", "menubar", "library").

        Raises:
            RuntimeError: If another IDS monitor is already running.
        """
        if self._thread is not None:
            return

        # Acquire advisory lock — only one IDS monitor at a time
        if not acquire_ids_lock(source):
            holder = get_ids_lock_holder()
            if holder:
                raise RuntimeError(
                    f"Another IDS monitor is already running "
                    f"(PID {holder['pid']}, started by {holder['source']} "
                    f"at {holder.get('started_iso', '?')}). "
                    f"Stop it first or use the existing monitor."
                )
            raise RuntimeError("Another IDS monitor is already running.")

        logger.info("Starting IDS monitor (interval=%.1fs, llm=%s)", self.interval, self.enable_llm)
        self._stop.clear()
        self._dns_cache.start_log_stream()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="ids-monitor")
        self._thread.start()

    def stop(self) -> None:
        """Stop IDS monitoring and shut down managed llama-server."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)
            self._thread = None
        if self._llm_thread is not None:
            self._llm_thread.join(timeout=30)
            self._llm_thread = None
        self._dns_cache.stop()
        if self._llm is not None:
            self._llm.stop_server()
        if self._webhook is not None:
            self._webhook.shutdown()
        self._log_writer.close()
        release_ids_lock()
        self.baseline.save()
        logger.info("IDS monitor stopped. %d alerts recorded.", len(self._alerts))

    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        from ._network import network_delta, network_snapshot

        # Initial network snapshot
        self._prev_net_snap = network_snapshot()

        # Initial per-interface counters
        try:
            from ._native import net_io_per_iface
            self._prev_iface_counters = net_io_per_iface()
        except Exception:
            self._prev_iface_counters = None

        time.sleep(self.interval)

        while not self._stop.is_set():
            try:
                self._cycle(network_snapshot, network_delta)
            except Exception as e:
                logger.error("IDS cycle error: %s", e)
            self._stop.wait(self.interval)

    def _cycle(self, network_snapshot_fn, network_delta_fn) -> None:
        """Single monitoring cycle."""
        from ._native import system_gpu_stats, system_stats

        # --- Collect metrics ---
        sys = system_stats()
        gpu = system_gpu_stats()

        cpu_pct = sys.get("cpu_pct", 0)
        gpu_pct = gpu.get("device_utilization", 0)
        ram_used = sys.get("memory_used", 0)
        ram_total = sys.get("memory_total", 1)

        # Network delta
        curr_net = network_snapshot_fn()
        net_delta = network_delta_fn(self._prev_net_snap, curr_net)
        self._prev_net_snap = curr_net

        net_bytes_per_s = net_delta.bytes_sent_per_s + net_delta.bytes_recv_per_s

        # Per-interface counters for VPN detection
        iface_counters: dict[str, dict] | None = None
        try:
            from ._native import net_io_per_iface
            curr_iface = net_io_per_iface()
            if self._prev_iface_counters is not None:
                # Compute delta per interface
                iface_counters = {}
                for iface, counters in curr_iface.items():
                    prev = self._prev_iface_counters.get(iface, {})
                    iface_counters[iface] = {
                        "bytes_sent": max(counters.get("bytes_sent", 0) - prev.get("bytes_sent", 0), 0),
                        "bytes_recv": max(counters.get("bytes_recv", 0) - prev.get("bytes_recv", 0), 0),
                    }
            self._prev_iface_counters = curr_iface

            # Update baseline VPN flag if we see utun traffic
            if iface_counters:
                for iface, counters in iface_counters.items():
                    if iface.startswith("utun"):
                        total = counters.get("bytes_sent", 0) + counters.get("bytes_recv", 0)
                        if total > 0:
                            self.baseline.record_vpn_traffic()
                            break
        except Exception:
            pass

        # Process list (from snapshot if available)
        try:
            from . import snapshot as dp_snapshot
            procs = dp_snapshot(interval=0.5, active_only=False, detailed=True)
        except Exception:
            procs = []

        # --- Update baseline ---
        proc_names = [p.get("name", "") for p in procs]
        listening_ports = [lp["port"] for lp in curr_net.listening_ports]
        remote_addrs = [
            c.remote_addr for c in curr_net.connections
            if c.remote_addr and not _is_private(c.remote_addr)
        ]

        self.baseline.update(
            processes=proc_names,
            listening_ports=listening_ports,
            remote_addrs=remote_addrs,
            net_bytes_per_s=net_bytes_per_s,
            cpu_pct=cpu_pct,
            gpu_pct=gpu_pct,
        )

        # --- Run detectors ---
        cycle_alerts: list[Alert] = []

        cycle_alerts.extend(detect_network_anomalies(
            net_delta, self.baseline, iface_counters=iface_counters,
        ))
        cycle_alerts.extend(detect_temporal_anomalies(
            cpu_pct, gpu_pct, net_bytes_per_s, procs, self.baseline,
        ))
        cycle_alerts.extend(detect_process_anomalies(procs, self.baseline))
        cycle_alerts.extend(detect_lineage_anomalies(procs, self.baseline))
        cycle_alerts.extend(detect_file_access_anomalies(procs))
        cycle_alerts.extend(detect_auth_anomalies(self.baseline))
        cycle_alerts.extend(detect_binary_anomalies(procs, self.baseline))

        # Enrich network alerts with DNS hostname context
        for alert in cycle_alerts:
            if alert.category == "network" and "hostname" not in alert.evidence:
                # Try to resolve any IP in evidence
                for key in ("remote_addr", "ip"):
                    ip = alert.evidence.get(key)
                    if ip:
                        hostname = self._dns_cache.resolve(ip)
                        if hostname:
                            alert.evidence["hostname"] = hostname
                            break

        # --- Deduplicate alerts (same rule within window = skip) ---
        now = time.time()
        deduped: list[Alert] = []
        for alert in cycle_alerts:
            dedup_key = f"{alert.rule}:{alert.category}"
            last_seen = self._dedup_window.get(dedup_key, 0)
            if now - last_seen > self._dedup_ttl:
                deduped.append(alert)
                self._dedup_window[dedup_key] = now
        # Prune stale dedup entries every 100 cycles
        if self._cycle_count % 100 == 0:
            cutoff = now - self._dedup_ttl * 2
            self._dedup_window = {k: v for k, v in self._dedup_window.items() if v > cutoff}
        cycle_alerts = deduped

        # --- Record alerts ---
        if cycle_alerts:
            with self._lock:
                self._alerts.extend(cycle_alerts)
                self._pending_llm_alerts.extend(cycle_alerts)

            # Log to file (rotating)
            for alert in cycle_alerts:
                self._log_writer.write_line(json.dumps(alert.to_dict()))

            # Print high-severity alerts immediately + send webhooks
            for alert in cycle_alerts:
                if alert.severity in (Severity.HIGH, Severity.CRITICAL):
                    logger.warning(
                        "[%s] %s/%s: %s",
                        alert.severity.upper(),
                        alert.category,
                        alert.rule,
                        alert.description,
                    )
                    if self._webhook is not None:
                        self._webhook.send_alert(alert)

        # --- Periodic LLM analysis ---
        now = time.time()
        if (
            self.enable_llm
            and self._llm
            and self._pending_llm_alerts
            and now - self._last_llm_run > self.llm_interval
            and (self._llm_thread is None or not self._llm_thread.is_alive())
        ):
            self._last_llm_run = now
            alerts_batch = list(self._pending_llm_alerts)
            self._pending_llm_alerts.clear()
            self._llm_thread = threading.Thread(
                target=self._run_llm_analysis,
                args=(alerts_batch, {
                    "cpu_pct": cpu_pct,
                    "gpu_pct": gpu_pct,
                    "ram_pct": round(ram_used / ram_total * 100, 1),
                    "net_bytes_per_s": round(net_bytes_per_s),
                    "hour": datetime.now().hour,
                    "user_idle": _is_user_idle(),
                }),
                daemon=True,
                name="ids-llm",
            )
            self._llm_thread.start()

        self._cycle_count += 1
        # Save baseline periodically (every 60 cycles)
        if self._cycle_count % 60 == 0:
            self.baseline.save()

    def _run_llm_analysis(self, alerts: list[Alert], context: dict) -> None:
        """Run LLM analysis on a batch of alerts (in separate thread)."""
        try:
            assessment = self._llm.analyze_alerts(alerts, context)
            with self._lock:
                self._llm_assessments.append({
                    "timestamp": time.time(),
                    "time": datetime.now().isoformat(),
                    "alert_count": len(alerts),
                    "assessment": assessment,
                })
            logger.info("LLM Assessment:\n%s", assessment)

            # Log assessment (rotating)
            self._log_writer.write_line(json.dumps({
                "type": "llm_assessment",
                "timestamp": time.time(),
                "assessment": assessment,
                "alert_count": len(alerts),
            }))

        except Exception as e:
            logger.error("LLM analysis failed: %s", e)

    @property
    def alerts(self) -> list[Alert]:
        """Get all recorded alerts."""
        with self._lock:
            return list(self._alerts)

    @property
    def assessments(self) -> list[dict]:
        """Get all LLM assessments."""
        with self._lock:
            return list(self._llm_assessments)

    def report(self) -> dict[str, Any]:
        """Generate IDS report.

        Returns:
            Dict with alert summary, severity counts, LLM assessments.
        """
        with self._lock:
            alerts = list(self._alerts)
            assessments = list(self._llm_assessments)

        severity_counts: dict[str, int] = defaultdict(int)
        category_counts: dict[str, int] = defaultdict(int)
        rule_counts: dict[str, int] = defaultdict(int)
        for a in alerts:
            severity_counts[a.severity] += 1
            category_counts[a.category] += 1
            rule_counts[a.rule] += 1

        return {
            "total_alerts": len(alerts),
            "severity_counts": dict(severity_counts),
            "category_counts": dict(category_counts),
            "rule_counts": dict(rule_counts),
            "llm_assessments": assessments,
            "recent_alerts": [a.to_dict() for a in alerts[-20:]],
            "baseline_warm": self.baseline.is_warm(),
            "baseline_samples": self.baseline._samples,
            "monitoring_cycles": self._cycle_count,
            "dns_cache_size": self._dns_cache.size,
        }
