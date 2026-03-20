"""Process coordination for darwin-perf.

Prevents conflicts when the menu bar app, daemon, CLI IDS, and Python
library are used simultaneously. Uses a lockfile with PID tracking.

The rule: only ONE active IDS monitor at a time (they'd duplicate alerts
and fight over baseline writes). Multiple read-only consumers (stats(),
snapshot(), TUI, etc.) are always fine — they just read from kernel APIs.

The lock is ADVISORY — it doesn't block reads, only prevents duplicate
IDS monitors. If the menu bar app is running IDS, the CLI --ids will
detect it and offer to connect instead of starting a second monitor.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path


LOCK_DIR = Path.home() / ".darwin_perf"
IDS_LOCK = LOCK_DIR / "ids.lock"


def _pid_alive(pid: int) -> bool:
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)  # signal 0 = check existence
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_ids_lock(source: str = "cli") -> bool:
    """Try to acquire the IDS lock. Returns True if acquired.

    Args:
        source: Who is acquiring ("cli", "daemon", "menubar", "library").

    Returns:
        True if lock acquired (you may run IDS).
        False if another IDS monitor is already running.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)

    # Check existing lock
    holder = get_ids_lock_holder()
    if holder is not None:
        pid = holder.get("pid", 0)
        if _pid_alive(pid):
            return False  # another monitor is running
        # Stale lock — previous holder died
        release_ids_lock()

    # Write our lock
    lock_data = {
        "pid": os.getpid(),
        "source": source,
        "started": time.time(),
        "started_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with open(IDS_LOCK, "w") as f:
        json.dump(lock_data, f)

    return True


def release_ids_lock() -> None:
    """Release the IDS lock."""
    try:
        IDS_LOCK.unlink(missing_ok=True)
    except Exception:
        pass


def get_ids_lock_holder() -> dict | None:
    """Get info about the current IDS lock holder, or None if unlocked.

    Returns:
        Dict with pid, source, started, started_iso. Or None.
    """
    if not IDS_LOCK.exists():
        return None

    try:
        with open(IDS_LOCK) as f:
            data = json.load(f)
        pid = data.get("pid", 0)
        if not _pid_alive(pid):
            # Stale lock
            release_ids_lock()
            return None
        return data
    except (json.JSONDecodeError, OSError):
        release_ids_lock()
        return None
