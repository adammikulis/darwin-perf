"""gpu-proc: Live per-process GPU utilization monitor for macOS.

Like `top` or `htop`, but for GPU. Auto-discovers all processes using
the GPU, ranked by utilization. Updates live.

Usage:
    gpu-proc              # monitor all GPU-active processes (needs sudo)
    gpu-proc --self       # monitor only this process (no sudo needed)
    gpu-proc --pid 1234   # monitor specific PID
    gpu-proc --top 10     # show top 10 GPU consumers
    gpu-proc -i 1         # update every 1 second
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time


def _get_all_pids() -> list[int]:
    """Get all running PIDs via psutil."""
    import psutil
    return [p.pid for p in psutil.process_iter(["pid"])]


def _pid_name(pid: int) -> str:
    """Get process name for a PID."""
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        return "?"


def _format_table(rows: list[tuple[int, str, float]], total_gpu: float | None) -> str:
    """Format rows as a table string."""
    lines = []
    lines.append(f"{'PID':>8}  {'GPU %':>7}  {'Process'}")
    lines.append(f"{'─' * 8}  {'─' * 7}  {'─' * 30}")
    for pid, name, pct in rows:
        bar_len = int(min(pct, 100) / 5)  # 20 chars = 100%
        bar = "█" * bar_len + "░" * (20 - bar_len)
        lines.append(f"{pid:>8}  {pct:>6.1f}%  {name:<20}  {bar}")
    if total_gpu is not None:
        lines.append(f"{'':>8}  {'─' * 7}")
        lines.append(f"{'Total':>8}  {total_gpu:>6.1f}%")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gpu-proc",
        description="Live per-process GPU utilization monitor for macOS.",
    )
    parser.add_argument("--self", action="store_true", dest="self_only",
                        help="Monitor only the current process (no sudo)")
    parser.add_argument("--pid", type=int, nargs="+", default=None,
                        help="Monitor specific PIDs")
    parser.add_argument("--top", type=int, default=20,
                        help="Show top N GPU consumers (default: 20)")
    parser.add_argument("-i", "--interval", type=float, default=2.0,
                        help="Update interval in seconds (default: 2)")
    parser.add_argument("-n", "--count", type=int, default=0,
                        help="Number of iterations (0 = unlimited)")
    parser.add_argument("-1", "--once", action="store_true",
                        help="Print one snapshot and exit")
    parser.add_argument("--tui", action="store_true",
                        help="Launch rich terminal UI with sparkline graphs")
    args = parser.parse_args()

    # TUI mode
    if args.tui:
        from macos_gpu_proc.tui import run_tui
        pids = args.pid if args.pid else None
        run_tui(pids=pids, self_only=args.self_only, interval=args.interval, top_n=args.top)
        return

    from macos_gpu_proc._native import gpu_time_ns, gpu_time_ns_multi

    # Resolve PIDs to monitor
    if args.self_only:
        pids = [0]
    elif args.pid:
        pids = args.pid
    else:
        # All processes — requires sudo for task_for_pid
        try:
            pids = _get_all_pids()
        except ImportError:
            print("Install psutil for all-process monitoring: pip install macos-gpu-proc[cli]", file=sys.stderr)
            print("Or use --self or --pid to monitor specific processes.", file=sys.stderr)
            sys.exit(1)

    if args.once:
        args.count = 1

    # Handle Ctrl+C gracefully
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    # Take initial sample
    prev = gpu_time_ns_multi(pids)
    prev_time = time.monotonic()
    time.sleep(args.interval)

    iteration = 0
    while True:
        now = time.monotonic()
        elapsed_s = now - prev_time

        # Re-discover processes if monitoring all
        if not args.self_only and not args.pid:
            try:
                pids = _get_all_pids()
            except Exception:
                pass

        curr = gpu_time_ns_multi(pids)
        elapsed_ns = elapsed_s * 1_000_000_000

        # Compute per-process GPU %
        rows: list[tuple[int, str, float]] = []
        for pid in set(list(prev.keys()) + list(curr.keys())):
            c = curr.get(pid, -1)
            p = prev.get(pid, -1)
            if c < 0 or p < 0:
                continue
            delta = c - p
            if delta <= 0:
                continue
            pct = min((delta / elapsed_ns) * 100, 100)
            if pct < 0.1:
                continue
            name = "self" if pid == 0 else _pid_name(pid)
            rows.append((pid if pid != 0 else os.getpid(), name, pct))

        # Sort by GPU % descending, take top N
        rows.sort(key=lambda r: r[2], reverse=True)
        rows = rows[:args.top]

        total = sum(r[2] for r in rows)

        # Clear screen for live mode
        if not args.once:
            print("\033[2J\033[H", end="")

        timestamp = time.strftime("%H:%M:%S")
        print(f"gpu-proc  {timestamp}  (every {args.interval}s)\n")

        if rows:
            print(_format_table(rows, total if len(rows) > 1 else None))
        else:
            print("  No GPU activity detected.")
            if not args.self_only and os.getuid() != 0:
                print("\n  Hint: run with sudo to see all processes.")

        prev = curr
        prev_time = now

        iteration += 1
        if 0 < args.count <= iteration:
            break

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
