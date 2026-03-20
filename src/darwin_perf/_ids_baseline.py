"""IDS baseline tracker — learns normal behavior over time."""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("darwin_perf.ids")


class BaselineTracker:
    """Tracks normal patterns to detect deviations.

    Builds baselines for:
    - Network throughput by hour of day
    - Known processes (name + hash of behavior)
    - Normal listening ports
    - Typical connection destinations
    - CPU/GPU/memory usage patterns by hour
    - VPN/tunnel interface usage
    """

    def __init__(self, baseline_path: str | Path | None = None) -> None:
        self._path = Path(baseline_path) if baseline_path else None
        self.known_processes: set[str] = set()
        self.known_listening_ports: set[int] = set()
        self.known_remote_addrs: set[str] = set()
        self.hourly_net_bytes: dict[int, list[float]] = defaultdict(list)  # hour -> [bytes/s]
        self.hourly_cpu_pct: dict[int, list[float]] = defaultdict(list)
        self.hourly_gpu_pct: dict[int, list[float]] = defaultdict(list)
        self.known_binary_hashes: dict[str, str] = {}  # path -> sha256
        self._vpn_seen: bool = False
        self._samples = 0
        self._lock = threading.Lock()

        if self._path and self._path.exists():
            self._load()

    def update(
        self,
        processes: list[str],
        listening_ports: list[int],
        remote_addrs: list[str],
        net_bytes_per_s: float,
        cpu_pct: float,
        gpu_pct: float,
    ) -> None:
        """Update baselines with new observation."""
        hour = datetime.now().hour
        with self._lock:
            self.known_processes.update(processes)
            self.known_listening_ports.update(listening_ports)
            self.known_remote_addrs.update(remote_addrs)
            self.hourly_net_bytes[hour].append(net_bytes_per_s)
            self.hourly_cpu_pct[hour].append(cpu_pct)
            self.hourly_gpu_pct[hour].append(gpu_pct)
            # Keep last 1000 samples per hour
            for store in (self.hourly_net_bytes, self.hourly_cpu_pct, self.hourly_gpu_pct):
                if len(store[hour]) > 1000:
                    store[hour] = store[hour][-1000:]
            self._samples += 1

    def record_vpn_traffic(self) -> None:
        """Mark that VPN/tunnel traffic has been seen in baseline."""
        with self._lock:
            self._vpn_seen = True

    def has_vpn_traffic(self) -> bool:
        """Whether VPN/tunnel traffic has been observed in baseline."""
        with self._lock:
            return self._vpn_seen

    def is_warm(self) -> bool:
        """Whether we have enough data for meaningful baselines."""
        return self._samples >= 60  # ~1 min at 1s interval

    def net_bytes_stats(self, hour: int) -> tuple[float, float]:
        """Return (mean, stddev) of network bytes/s for a given hour."""
        with self._lock:
            vals = self.hourly_net_bytes.get(hour, [])
        if len(vals) < 10:
            return 0.0, float("inf")
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        return mean, var ** 0.5

    def cpu_stats(self, hour: int) -> tuple[float, float]:
        with self._lock:
            vals = self.hourly_cpu_pct.get(hour, [])
        if len(vals) < 10:
            return 0.0, float("inf")
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        return mean, var ** 0.5

    def gpu_stats(self, hour: int) -> tuple[float, float]:
        with self._lock:
            vals = self.hourly_gpu_pct.get(hour, [])
        if len(vals) < 10:
            return 0.0, float("inf")
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        return mean, var ** 0.5

    def is_known_process(self, name: str) -> bool:
        with self._lock:
            return name in self.known_processes

    def is_known_port(self, port: int) -> bool:
        with self._lock:
            return port in self.known_listening_ports

    def is_known_remote(self, addr: str) -> bool:
        with self._lock:
            return addr in self.known_remote_addrs

    def update_binary_hash(self, path: str, hash_hex: str) -> None:
        """Record or update the SHA-256 hash for a binary path."""
        with self._lock:
            self.known_binary_hashes[path] = hash_hex

    def is_known_binary(self, path: str, hash_hex: str) -> bool:
        """Check if this path+hash combination is already known."""
        with self._lock:
            return self.known_binary_hashes.get(path) == hash_hex

    def get_binary_hash(self, path: str) -> str | None:
        """Return the stored hash for a path, or None if not tracked."""
        with self._lock:
            return self.known_binary_hashes.get(path)

    def save(self) -> None:
        if not self._path:
            return
        with self._lock:
            data = {
                "known_processes": list(self.known_processes),
                "known_listening_ports": list(self.known_listening_ports),
                "known_remote_addrs": list(self.known_remote_addrs),
                "hourly_net_bytes": {
                    str(h): vals[-200:] for h, vals in self.hourly_net_bytes.items()
                },
                "hourly_cpu_pct": {
                    str(h): vals[-200:] for h, vals in self.hourly_cpu_pct.items()
                },
                "hourly_gpu_pct": {
                    str(h): vals[-200:] for h, vals in self.hourly_gpu_pct.items()
                },
                "known_binary_hashes": dict(self.known_binary_hashes),
                "samples": self._samples,
                "vpn_seen": self._vpn_seen,
            }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(data, f)

    def _load(self) -> None:
        try:
            with open(self._path) as f:
                data = json.load(f)
            self.known_processes = set(data.get("known_processes", []))
            self.known_listening_ports = set(data.get("known_listening_ports", []))
            self.known_remote_addrs = set(data.get("known_remote_addrs", []))
            for h, vals in data.get("hourly_net_bytes", {}).items():
                self.hourly_net_bytes[int(h)] = vals
            for h, vals in data.get("hourly_cpu_pct", {}).items():
                self.hourly_cpu_pct[int(h)] = vals
            for h, vals in data.get("hourly_gpu_pct", {}).items():
                self.hourly_gpu_pct[int(h)] = vals
            self.known_binary_hashes = data.get("known_binary_hashes", {})
            self._samples = data.get("samples", 0)
            self._vpn_seen = data.get("vpn_seen", False)
        except Exception as e:
            logger.warning("Failed to load baseline: %s", e)
