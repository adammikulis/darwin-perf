"""darwin-perf: System performance monitor and debugger for macOS.

Like `top` but for GPU, CPU power, temperatures, and per-process metrics.
Auto-discovers all processes using the GPU via IORegistry — no sudo needed.

Usage:
    darwin-perf              # live per-process GPU/CPU monitor
    darwin-perf --json       # JSON line per update (pipe to jq, etc.)
    darwin-perf --csv        # CSV output for spreadsheets
    darwin-perf --record f   # record full system state to JSONL
    darwin-perf --export f   # convert JSONL recording to CSV
    darwin-perf --replay f   # replay a recorded session
    darwin-perf --pid 1234   # monitor specific PID
    darwin-perf -i 1         # 1-second update interval
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from ._cli_modes import (
    _collect_snapshot,
    _format_table,
    _run_csv,
    _run_export,
    _run_ids,
    _run_ids_analyze,
    _run_ids_export_baseline,
    _run_ids_import_baseline,
    _run_json,
    _run_net,
    _run_record,
    _run_replay,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="darwin-perf",
        description="Live per-process GPU utilization monitor for macOS.",
    )
    parser.add_argument(
        "--pid", type=int, nargs="+", default=None, help="Monitor specific PIDs"
    )
    parser.add_argument(
        "--top", type=int, default=20, help="Show top N GPU consumers (default: 20)"
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=2.0,
        help="Update interval in seconds (default: 2)",
    )
    parser.add_argument(
        "-n", "--count", type=int, default=0, help="Number of iterations (0 = unlimited)"
    )
    parser.add_argument("-1", "--once", action="store_true", help="Print one snapshot and exit")
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Launch rich terminal UI with sparkline graphs",
    )
    parser.add_argument(
        "--gui", action="store_true", help="Launch native floating window monitor"
    )
    parser.add_argument(
        "--menubar", action="store_true",
        help="Run as macOS menu bar app (persistent monitoring)"
    )
    parser.add_argument(
        "--dock", action="store_true",
        help="Show Dock icon when running --menubar (default: menu bar only)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Output one JSON line per update"
    )
    parser.add_argument(
        "--csv", action="store_true", help="Output CSV (header + one row per process per update)"
    )
    parser.add_argument(
        "--record", type=str, metavar="FILE",
        help="Record detailed snapshots to a JSONL file"
    )
    parser.add_argument(
        "--replay", type=str, metavar="FILE",
        help="Replay a recorded JSONL file"
    )
    parser.add_argument(
        "--export", type=str, metavar="FILE",
        help="Export a recorded JSONL file to CSV (produces _system.csv and _processes.csv)"
    )
    # Network monitoring
    parser.add_argument(
        "--net", action="store_true",
        help="Live network monitoring: traffic rates, connections, listening ports"
    )
    # IDS (Intrusion Detection System)
    parser.add_argument(
        "--ids", action="store_true",
        help="Run intrusion detection: monitors network, processes, GPU for anomalies"
    )
    parser.add_argument(
        "--ids-analyze", type=str, metavar="FILE",
        help="Analyze a recorded JSONL file for security issues using local LLM"
    )
    parser.add_argument(
        "--ids-export-baseline", type=str, metavar="FILE",
        help="Export the current IDS baseline to a JSON file"
    )
    parser.add_argument(
        "--ids-import-baseline", type=str, metavar="FILE",
        help="Import a baseline JSON file (merges with existing)"
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Disable LLM analysis in IDS mode (rule-based only)"
    )
    parser.add_argument(
        "--llm-interval", type=float, default=300.0,
        help="Seconds between LLM analysis runs in IDS mode (default: 300)"
    )
    parser.add_argument(
        "--model", type=str, metavar="PATH",
        help="Path to GGUF model file for IDS LLM analysis"
    )
    parser.add_argument(
        "--ids-webhook", type=str, metavar="URL",
        help="Webhook URL for HIGH/CRITICAL IDS alerts (Slack or generic)"
    )
    parser.add_argument(
        "--ids-retention-days", type=int, default=30,
        help="Days to keep rotated IDS log files (default: 30)"
    )
    # Daemon management
    parser.add_argument(
        "--ids-install", action="store_true",
        help="Install a launchd daemon that runs IDS in the background"
    )
    parser.add_argument(
        "--ids-uninstall", action="store_true",
        help="Uninstall the IDS launchd daemon"
    )
    parser.add_argument(
        "--ids-status", action="store_true",
        help="Show status of the IDS launchd daemon"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose output (debug logging)"
    )
    args = parser.parse_args()

    # Menu bar app mode
    if args.menubar:
        from darwin_perf._menubar import run_menubar

        run_menubar(
            interval=args.interval,
            enable_ids=args.ids,
            show_dock=args.dock,
        )
        return

    # GUI mode
    if args.gui:
        from darwin_perf.gui import run_gui

        run_gui(interval=args.interval)
        return

    # TUI mode
    if args.tui:
        from darwin_perf.tui import run_tui

        run_tui(
            pids=args.pid, interval=args.interval,
            top_n=args.top, record_path=args.record,
        )
        return

    if args.once:
        args.count = 1

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    # IDS daemon management
    if args.ids_install:
        from darwin_perf._daemon import install_daemon
        plist = install_daemon(
            interval=args.interval,
            enable_llm=not args.no_llm,
            webhook_url=args.ids_webhook,
        )
        print(f"IDS daemon installed: {plist}")
        print("The daemon will start automatically on login.")
        return
    if args.ids_uninstall:
        from darwin_perf._daemon import uninstall_daemon
        uninstall_daemon()
        print("IDS daemon uninstalled.")
        return
    if args.ids_status:
        from darwin_perf._daemon import daemon_status
        status = daemon_status()
        state = "loaded" if status["loaded"] else ("installed (not loaded)" if status["installed"] else "not installed")
        print(f"IDS daemon: {state}")
        if status["pid"] is not None:
            print(f"  PID: {status['pid']}")
        if status["last_log_lines"]:
            print("  Recent log:")
            for line in status["last_log_lines"]:
                print(f"    {line}")
        return

    # IDS baseline export/import
    if args.ids_export_baseline:
        _run_ids_export_baseline(args)
        return
    if args.ids_import_baseline:
        _run_ids_import_baseline(args)
        return

    # IDS mode
    if args.ids:
        _run_ids(args)
        return

    # IDS analyze mode
    if args.ids_analyze:
        _run_ids_analyze(args)
        return

    # Network monitoring mode
    if args.net:
        _run_net(args)
        return

    # Export mode (no live data needed)
    if args.export:
        _run_export(args)
        return

    # Replay mode
    if args.replay:
        _run_replay(args)
        return

    # Record mode
    if args.record:
        _run_record(args)
        return

    # JSON streaming mode
    if args.json:
        _run_json(args)
        return

    # CSV streaming mode
    if args.csv:
        _run_csv(args)
        return

    # Default table mode
    from darwin_perf import _snapshot
    from darwin_perf._native import cpu_time_ns, proc_info

    # Initial snapshot
    prev = _snapshot()
    prev_cpu: dict[int, int] = {}
    for pid in prev:
        ns = cpu_time_ns(pid)
        prev_cpu[pid] = ns if ns >= 0 else 0
    prev_time = time.monotonic()
    time.sleep(args.interval)

    iteration = 0
    while True:
        now = time.monotonic()
        elapsed_s = now - prev_time
        elapsed_ns = elapsed_s * 1_000_000_000

        curr = _snapshot()
        curr_cpu: dict[int, int] = {}
        for pid in curr:
            ns = cpu_time_ns(pid)
            curr_cpu[pid] = ns if ns >= 0 else 0

        # Filter to specific PIDs if requested
        pids = set(curr.keys())
        if args.pid:
            pids = pids & set(args.pid)

        rows: list[tuple[int, str, float, float, float, float]] = []
        for pid in pids:
            c_gpu = curr.get(pid, {}).get("gpu_ns", 0)
            p_gpu = prev.get(pid, {}).get("gpu_ns", 0)
            gpu_delta = c_gpu - p_gpu

            c_cpu = curr_cpu.get(pid, 0)
            p_cpu = prev_cpu.get(pid, 0)
            cpu_delta = c_cpu - p_cpu

            gpu_pct = min(gpu_delta / elapsed_ns * 100, 100) if elapsed_ns > 0 else 0
            cpu_pct = cpu_delta / elapsed_ns * 100 if elapsed_ns > 0 else 0

            info = proc_info(pid)
            mem_mb = info["memory"] / (1024 * 1024) if info else 0
            real_mb = info["real_memory"] / (1024 * 1024) if info else 0

            name = curr[pid]["name"]
            if gpu_pct >= 0.1 or gpu_delta > 0:
                rows.append((pid, name, gpu_pct, cpu_pct, mem_mb, real_mb))

        rows.sort(key=lambda r: r[2], reverse=True)
        rows = rows[: args.top]

        if not args.once:
            print("\033[2J\033[H", end="")

        timestamp = time.strftime("%H:%M:%S")
        print(f"darwin-perf  {timestamp}  (every {args.interval}s)\n")

        if rows:
            print(_format_table(rows))
        else:
            print("  No GPU activity detected.")

        prev = curr
        prev_cpu = curr_cpu
        prev_time = now

        iteration += 1
        if 0 < args.count <= iteration:
            break

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
