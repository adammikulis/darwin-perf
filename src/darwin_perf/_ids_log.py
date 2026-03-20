"""Rotating JSONL log writer for IDS alerts.

Provides size-based rotation with gzip compression and time-based pruning.

Usage::

    from darwin_perf._ids_log import RotatingJsonlWriter

    writer = RotatingJsonlWriter(Path("~/.darwin_perf/ids_alerts.jsonl"))
    writer.write_line('{"alert": "test"}')
    writer.close()
"""

from __future__ import annotations

import gzip
import os
import time
from pathlib import Path
from typing import Any


def prune_old_logs(log_dir: Path, retention_days: int = 30) -> int:
    """Delete rotated log files older than *retention_days*.

    Scans for ``ids_alerts.jsonl.*`` files and removes those whose mtime
    exceeds the retention window.

    Returns:
        Number of files deleted.
    """
    if not log_dir.is_dir():
        return 0

    cutoff = time.time() - retention_days * 86400
    deleted = 0
    for f in log_dir.iterdir():
        if not f.name.startswith("ids_alerts.jsonl."):
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError:
            pass
    return deleted


class RotatingJsonlWriter:
    """JSONL file writer with size-based rotation and gzip compression.

    Args:
        path: Path to the primary JSONL file.
        max_bytes: Rotate when the file exceeds this size (default 10 MB).
        max_files: Number of compressed rotated copies to keep (default 5).
        retention_days: Auto-prune rotated files older than this (default 30).
    """

    def __init__(
        self,
        path: Path | str,
        max_bytes: int = 10 * 1024 * 1024,
        max_files: int = 5,
        retention_days: int = 30,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.retention_days = retention_days
        self._fd: Any = None

        # Ensure parent dir exists
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Prune old logs on startup
        prune_old_logs(self.path.parent, self.retention_days)

        # Open the current file for appending
        self._fd = open(self.path, "a")  # noqa: SIM115

    def write_line(self, line: str) -> None:
        """Write a single line (should already be JSON-encoded).

        Automatically rotates if the file exceeds *max_bytes*.
        """
        if self._fd is None:
            self._fd = open(self.path, "a")  # noqa: SIM115

        self._fd.write(line if line.endswith("\n") else line + "\n")
        self._fd.flush()

        # Check size after write
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size >= self.max_bytes:
            self._rotate()

    def _rotate(self) -> None:
        """Rotate the current log file.

        Shifts existing rotated files up by one index, gzip-compresses the
        current file into slot 1, then truncates the primary file.
        """
        self.close()

        # Shift existing rotated files: N -> N+1
        for i in range(self.max_files, 0, -1):
            src = self.path.parent / f"{self.path.name}.{i}.gz"
            if i == self.max_files:
                # Delete the oldest
                if src.exists():
                    src.unlink()
            else:
                dst = self.path.parent / f"{self.path.name}.{i + 1}.gz"
                if src.exists():
                    src.rename(dst)

        # Compress current file into slot 1
        slot1 = self.path.parent / f"{self.path.name}.1.gz"
        try:
            with open(self.path, "rb") as f_in, gzip.open(slot1, "wb") as f_out:
                while True:
                    chunk = f_in.read(65536)
                    if not chunk:
                        break
                    f_out.write(chunk)
        except OSError:
            pass

        # Truncate the primary file
        try:
            with open(self.path, "w"):
                pass
        except OSError:
            pass

        # Re-open for appending
        self._fd = open(self.path, "a")  # noqa: SIM115

    def close(self) -> None:
        """Flush and close the underlying file."""
        if self._fd is not None:
            try:
                self._fd.flush()
                self._fd.close()
            except OSError:
                pass
            self._fd = None
