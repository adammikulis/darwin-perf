"""Launchd daemon management for darwin-perf IDS.

Install, uninstall, and query a persistent launchd user agent that runs
the IDS monitor in the background.

Usage::

    from darwin_perf._daemon import install_daemon, uninstall_daemon, daemon_status

    install_daemon(interval=5, enable_llm=False, webhook_url="https://...")
    print(daemon_status())
    uninstall_daemon()
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.darwin-perf.ids"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / ".darwin_perf"
LOG_PATH = LOG_DIR / "daemon.log"


def install_daemon(
    interval: float = 5.0,
    enable_llm: bool = False,
    webhook_url: str | None = None,
) -> Path:
    """Generate and load a launchd plist for the IDS monitor.

    Args:
        interval: Monitoring interval in seconds.
        enable_llm: Whether to enable LLM analysis (default False).
        webhook_url: Optional webhook URL for alert notifications.

    Returns:
        Path to the installed plist file.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Build the command
    args = [sys.executable, "-m", "darwin_perf", "--ids", "--no-llm", "-i", str(interval)]

    if enable_llm:
        # Remove --no-llm
        args.remove("--no-llm")

    if webhook_url:
        args.extend(["--ids-webhook", webhook_url])

    plist: dict = {
        "Label": LABEL,
        "ProgramArguments": args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
    }

    # Unload first if already loaded (ignore errors)
    if PLIST_PATH.exists():
        subprocess.run(
            ["launchctl", "unload", str(PLIST_PATH)],
            capture_output=True,
        )

    # Write plist
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)

    # Load
    result = subprocess.run(
        ["launchctl", "load", str(PLIST_PATH)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"launchctl load failed (rc={result.returncode}): {result.stderr.strip()}"
        )

    return PLIST_PATH


def uninstall_daemon() -> None:
    """Unload and remove the launchd plist."""
    if PLIST_PATH.exists():
        subprocess.run(
            ["launchctl", "unload", str(PLIST_PATH)],
            capture_output=True,
        )
        PLIST_PATH.unlink()


def daemon_status() -> dict:
    """Query the daemon's current state.

    Returns:
        Dict with keys:
        - installed (bool): Whether the plist file exists.
        - loaded (bool): Whether launchctl reports the job.
        - pid (int | None): Running PID if active.
        - last_log_lines (list[str]): Last 10 lines of daemon.log.
    """
    installed = PLIST_PATH.exists()

    # Check launchctl list for our label
    loaded = False
    pid: int | None = None
    if installed:
        result = subprocess.run(
            ["launchctl", "list", LABEL],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            loaded = True
            # Parse PID from output (format: { "PID" = 1234; ... } or tabular)
            for line in result.stdout.splitlines():
                line = line.strip()
                if '"PID"' in line:
                    # plist-style: "PID" = 1234;
                    parts = line.split("=")
                    if len(parts) >= 2:
                        try:
                            pid = int(parts[1].strip().rstrip(";").strip())
                        except ValueError:
                            pass

    # Read last log lines
    last_log_lines: list[str] = []
    if LOG_PATH.exists():
        try:
            with open(LOG_PATH) as f:
                all_lines = f.readlines()
            last_log_lines = [l.rstrip() for l in all_lines[-10:]]
        except OSError:
            pass

    return {
        "installed": installed,
        "loaded": loaded,
        "pid": pid,
        "last_log_lines": last_log_lines,
    }
