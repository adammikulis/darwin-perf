"""Background power sampling thread shared by TUI and GUI.

cpu_power() and gpu_power() block for their interval, so we run them
in a background thread and cache the last result for the UI to read.
"""

from __future__ import annotations

import threading
from typing import Any

from ._native import cpu_power, gpu_power


class PowerSampler:
    """Background thread that samples cpu_power/gpu_power and caches results."""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self.cpu: dict[str, Any] = {}
        self.gpu: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 2)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                cpu_data = cpu_power(self.interval)
                gpu_data = gpu_power(self.interval)
                with self._lock:
                    self.cpu = cpu_data
                    self.gpu = gpu_data
            except Exception:
                pass

    def get(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return (cpu_data, gpu_data) — last cached values."""
        with self._lock:
            return dict(self.cpu), dict(self.gpu)
