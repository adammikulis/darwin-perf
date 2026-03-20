"""CLI mode implementations for darwin-perf.

All _run_* functions and helpers used by cli.main() dispatch.
"""

from __future__ import annotations

import json
import sys
import time


def _format_table(
    rows: list[tuple[int, str, float, float, float, float]],
) -> str:
    """Format rows as a table string.

    Each row: (pid, name, gpu_pct, cpu_pct, mem_mb, real_mem_mb)
    """
    lines = []
    lines.append(
        f"{'PID':>8}  {'GPU %':>7}  {'CPU %':>7}  {'Memory':>9}  {'Real Mem':>9}  {'Process'}"
    )
    lines.append(
        f"{'─' * 8}  {'─' * 7}  {'─' * 7}  {'─' * 9}  {'─' * 9}  {'─' * 25}"
    )
    for pid, name, gpu_pct, cpu_pct, mem_mb, real_mem_mb in rows:
        bar_len = int(min(gpu_pct, 100) / 5)  # 20 chars = 100%
        bar = "█" * bar_len + "░" * (20 - bar_len)
        lines.append(
            f"{pid:>8}  {gpu_pct:>6.1f}%  {cpu_pct:>6.1f}%  "
            f"{mem_mb:>7.1f}MB  {real_mem_mb:>7.1f}MB  {name:<20}  {bar}"
        )
    return "\n".join(lines)


def _collect_snapshot(args, prev, prev_cpu, prev_time):
    """Collect one snapshot and return (rows, curr, curr_cpu, now)."""
    from darwin_perf import _snapshot
    from darwin_perf._native import cpu_time_ns, proc_info

    now = time.monotonic()
    elapsed_s = now - prev_time
    elapsed_ns = elapsed_s * 1_000_000_000

    curr = _snapshot()
    curr_cpu: dict[int, int] = {}
    for pid in curr:
        ns = cpu_time_ns(pid)
        curr_cpu[pid] = ns if ns >= 0 else 0

    pids = set(curr.keys())
    if args.pid:
        pids = pids & set(args.pid)

    rows: list[dict] = []
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
        api = curr[pid].get("api", "unknown")
        if gpu_pct >= 0.1 or gpu_delta > 0:
            rows.append({
                "pid": pid,
                "name": name,
                "api": api,
                "gpu_pct": round(gpu_pct, 1),
                "cpu_pct": round(cpu_pct, 1),
                "mem_mb": round(mem_mb, 1),
                "real_mem_mb": round(real_mb, 1),
            })

    rows.sort(key=lambda r: r["gpu_pct"], reverse=True)
    rows = rows[: args.top]
    return rows, curr, curr_cpu, now


def _run_json(args):
    """JSON streaming mode: one JSON line per update."""
    from darwin_perf import _snapshot
    from darwin_perf._native import cpu_time_ns

    prev = _snapshot()
    prev_cpu: dict[int, int] = {}
    for pid in prev:
        ns = cpu_time_ns(pid)
        prev_cpu[pid] = ns if ns >= 0 else 0
    prev_time = time.monotonic()
    time.sleep(args.interval)

    iteration = 0
    while True:
        rows, prev, prev_cpu, prev_time = _collect_snapshot(args, prev, prev_cpu, prev_time)
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "epoch": time.time(),
            "processes": rows,
        }
        print(json.dumps(record), flush=True)

        iteration += 1
        if 0 < args.count <= iteration:
            break
        time.sleep(args.interval)


def _run_csv(args):
    """CSV streaming mode: header + one row per process per update."""
    from darwin_perf import _snapshot
    from darwin_perf._native import cpu_time_ns

    prev = _snapshot()
    prev_cpu: dict[int, int] = {}
    for pid in prev:
        ns = cpu_time_ns(pid)
        prev_cpu[pid] = ns if ns >= 0 else 0
    prev_time = time.monotonic()
    time.sleep(args.interval)

    header = "timestamp,pid,name,api,gpu_pct,cpu_pct,mem_mb,real_mem_mb"
    print(header, flush=True)

    iteration = 0
    while True:
        rows, prev, prev_cpu, prev_time = _collect_snapshot(args, prev, prev_cpu, prev_time)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        for r in rows:
            # Escape commas in name
            name = r["name"].replace(",", ";")
            print(f"{ts},{r['pid']},{name},{r['api']},{r['gpu_pct']},{r['cpu_pct']},{r['mem_mb']},{r['real_mem_mb']}", flush=True)

        iteration += 1
        if 0 < args.count <= iteration:
            break
        time.sleep(args.interval)


def _run_record(args):
    """Record full system snapshots to a JSONL file.

    Each line contains: CPU/GPU power+frequency, temperatures, memory,
    system GPU stats, and per-process GPU/CPU utilization.
    """
    from darwin_perf import snapshot

    with open(args.record, "w") as f:
        iteration = 0
        while True:
            data = snapshot(interval=args.interval, detailed=True, system=True)
            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "epoch": time.time(),
                "interval": args.interval,
                **data,
            }
            f.write(json.dumps(record) + "\n")
            f.flush()

            n_procs = len(data.get("processes", []))
            cpu_w = data.get("cpu", {}).get("cpu_power_w", 0)
            gpu_w = data.get("gpu", {}).get("gpu_power_w", 0)
            temps = data.get("temperatures", {})
            cpu_t = temps.get("cpu_avg", 0)
            gpu_t = temps.get("gpu_avg", 0)
            sys.stderr.write(
                f"\r[{time.strftime('%H:%M:%S')}] "
                f"CPU {cpu_w:.1f}W/{cpu_t:.0f}°C  "
                f"GPU {gpu_w:.1f}W/{gpu_t:.0f}°C  "
                f"{n_procs} procs  →  {args.record}"
            )
            sys.stderr.flush()

            iteration += 1
            if 0 < args.count <= iteration:
                break
        sys.stderr.write("\n")


def _run_export(args):
    """Export a recorded JSONL file to CSV.

    Produces two CSV files:
      <name>_system.csv  — one row per sample (CPU/GPU power, temps, memory)
      <name>_processes.csv — one row per process per sample
    """
    import csv
    from pathlib import Path

    inpath = Path(args.export)
    stem = inpath.stem
    outdir = inpath.parent

    sys_csv_path = outdir / f"{stem}_system.csv"
    proc_csv_path = outdir / f"{stem}_processes.csv"

    with open(inpath) as f:
        lines = f.readlines()

    if not lines:
        print("Empty recording file.", file=sys.stderr)
        return

    # Parse all records
    records = [json.loads(line.strip()) for line in lines if line.strip()]

    # --- System CSV ---
    sys_fields = [
        "timestamp", "epoch", "interval",
        "cpu_power_w", "cpu_energy_nj",
        "ecpu_freq_mhz", "ecpu_active_pct",
        "pcpu_freq_mhz", "pcpu_active_pct",
        "gpu_power_w", "gpu_freq_mhz", "gpu_throttled",
        "temp_cpu_avg", "temp_gpu_avg", "temp_system_avg",
        "memory_total", "memory_used", "memory_available", "memory_compressed",
        "gpu_device_utilization", "gpu_model",
    ]
    with open(sys_csv_path, "w", newline="") as sf:
        writer = csv.DictWriter(sf, fieldnames=sys_fields, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            cpu = r.get("cpu", {})
            gpu = r.get("gpu", {})
            temps = r.get("temperatures", {})
            mem = r.get("memory", {})
            gs = r.get("gpu_stats", {})
            clusters = cpu.get("clusters", {})
            ecpu = clusters.get("ECPU", {})
            pcpu = clusters.get("PCPU", {})

            writer.writerow({
                "timestamp": r.get("timestamp", ""),
                "epoch": r.get("epoch", 0),
                "interval": r.get("interval", 0),
                "cpu_power_w": cpu.get("cpu_power_w", 0),
                "cpu_energy_nj": cpu.get("cpu_energy_nj", 0),
                "ecpu_freq_mhz": ecpu.get("freq_mhz", 0),
                "ecpu_active_pct": ecpu.get("active_pct", 0),
                "pcpu_freq_mhz": pcpu.get("freq_mhz", 0),
                "pcpu_active_pct": pcpu.get("active_pct", 0),
                "gpu_power_w": gpu.get("gpu_power_w", 0),
                "gpu_freq_mhz": gpu.get("gpu_freq_mhz", 0),
                "gpu_throttled": gpu.get("throttled", False),
                "temp_cpu_avg": temps.get("cpu_avg", 0),
                "temp_gpu_avg": temps.get("gpu_avg", 0),
                "temp_system_avg": temps.get("system_avg", 0),
                "memory_total": mem.get("memory_total", 0),
                "memory_used": mem.get("memory_used", 0),
                "memory_available": mem.get("memory_available", 0),
                "memory_compressed": mem.get("memory_compressed", 0),
                "gpu_device_utilization": gs.get("device_utilization", 0),
                "gpu_model": gs.get("model", ""),
            })

    # --- Process CSV ---
    proc_fields = [
        "timestamp", "pid", "name",
        "gpu_percent", "cpu_percent", "memory_mb", "energy_w", "threads",
    ]
    # Check if any record has detailed fields
    has_detailed = any(
        "ipc" in p
        for r in records
        for p in r.get("processes", [])
    )
    if has_detailed:
        proc_fields += [
            "peak_memory_mb", "wired_mb", "neural_mb",
            "disk_read_mb", "disk_write_mb",
            "instructions", "cycles", "ipc",
            "idle_wakeups", "pageins",
        ]

    with open(proc_csv_path, "w", newline="") as pf:
        writer = csv.DictWriter(pf, fieldnames=proc_fields, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            ts = r.get("timestamp", "")
            for p in r.get("processes", []):
                row = {"timestamp": ts, **p}
                writer.writerow(row)

    print(f"Exported {len(records)} samples:")
    print(f"  System:    {sys_csv_path}")
    print(f"  Processes: {proc_csv_path}")


def _run_replay(args):
    """Replay a recorded JSONL file with original timing."""
    with open(args.replay) as f:
        lines = f.readlines()

    if not lines:
        print("Empty recording file.", file=sys.stderr)
        return

    prev_epoch = None
    for line_str in lines:
        record = json.loads(line_str.strip())
        epoch = record.get("epoch", 0)

        if prev_epoch is not None and not args.once:
            delay = epoch - prev_epoch
            if delay > 0:
                time.sleep(delay)
        prev_epoch = epoch

        ts = record.get("timestamp", "")
        procs = record.get("processes", [])

        if not args.once:
            print("\033[2J\033[H", end="")
        print(f"darwin-perf replay  {ts}\n")

        if procs:
            rows = []
            for p in procs:
                rows.append((
                    p.get("pid", 0),
                    p.get("name", "?"),
                    p.get("gpu_percent", 0),
                    p.get("cpu_percent", 0),
                    p.get("memory_mb", 0),
                    p.get("memory_mb", 0),  # real_mem not in snapshot output
                ))
            print(_format_table(rows))
        else:
            print("  No GPU activity.")

        if args.once:
            break


def _run_ids(args):
    """Run the Intrusion Detection System monitor."""
    import logging

    from darwin_perf._ids import IDSMonitor

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("darwin_perf.ids")

    print("darwin-perf IDS — Intrusion Detection System")
    print(f"  Monitoring interval: {args.interval}s")
    print(f"  LLM analysis: {'enabled' if not args.no_llm else 'disabled'}")
    if args.model:
        print(f"  Model: {args.model}")
    print(f"  Alert log: ~/.darwin_perf/ids_alerts.jsonl")
    print(f"  Baseline: ~/.darwin_perf/ids_baseline.json")
    print()
    print("Building baseline... (alerts may be noisy for the first ~60 samples)")
    print("Press Ctrl+C to stop.\n")

    ids = IDSMonitor(
        interval=args.interval,
        llm_interval=args.llm_interval,
        model_path=args.model,
        enable_llm=not args.no_llm,
        webhook_url=getattr(args, "ids_webhook", None),
        retention_days=getattr(args, "ids_retention_days", 30),
    )
    try:
        ids.start(source="cli")
    except RuntimeError as e:
        print(f"\n  {e}\n", file=sys.stderr)
        sys.exit(1)

    try:
        prev_count = 0
        while True:
            time.sleep(10)
            alerts = ids.alerts
            if len(alerts) > prev_count:
                new_alerts = alerts[prev_count:]
                for a in new_alerts:
                    sev = a.severity.upper()
                    color = {
                        "INFO": "\033[36m",     # cyan
                        "LOW": "\033[34m",      # blue
                        "MEDIUM": "\033[33m",   # yellow
                        "HIGH": "\033[31m",     # red
                        "CRITICAL": "\033[41m", # red bg
                    }.get(sev, "")
                    reset = "\033[0m"
                    ts = time.strftime("%H:%M:%S", time.localtime(a.timestamp))
                    print(f"  {color}[{sev}]{reset} {ts} {a.category}/{a.rule}: {a.description}")
                prev_count = len(alerts)

            # Print LLM assessments
            assessments = ids.assessments
            for assessment in assessments[prev_count:]:
                print(f"\n  \033[35m[LLM ASSESSMENT]\033[0m {assessment.get('time', '')}")
                print(f"  {assessment.get('assessment', '')}\n")

    except KeyboardInterrupt:
        print("\n\nStopping IDS monitor...")
        ids.stop()
        report = ids.report()
        print(f"\nSession summary:")
        print(f"  Total alerts: {report['total_alerts']}")
        print(f"  Severity: {report['severity_counts']}")
        print(f"  Categories: {report['category_counts']}")
        print(f"  Monitoring cycles: {report['monitoring_cycles']}")
        print(f"  Baseline samples: {report['baseline_samples']}")
        if report['llm_assessments']:
            print(f"  LLM assessments: {len(report['llm_assessments'])}")


def _run_ids_export_baseline(args):
    """Export the IDS baseline to a JSON file."""
    import shutil
    from pathlib import Path

    src = Path.home() / ".darwin_perf" / "ids_baseline.json"
    dst = Path(args.ids_export_baseline)

    if not src.exists():
        print(f"No baseline file found at {src}", file=sys.stderr)
        print("Run --ids first to build a baseline.", file=sys.stderr)
        sys.exit(1)

    shutil.copy2(src, dst)
    print(f"Baseline exported to {dst}")

    # Print summary
    with open(dst) as f:
        data = json.load(f)
    print(f"  Samples: {data.get('samples', 0)}")
    print(f"  Known processes: {len(data.get('known_processes', []))}")
    print(f"  Known ports: {len(data.get('known_listening_ports', []))}")
    print(f"  Known remotes: {len(data.get('known_remote_addrs', []))}")


def _run_ids_import_baseline(args):
    """Import and merge an IDS baseline from a JSON file."""
    from pathlib import Path

    src = Path(args.ids_import_baseline)
    if not src.exists():
        print(f"File not found: {src}", file=sys.stderr)
        sys.exit(1)

    with open(src) as f:
        imported = json.load(f)

    dst = Path.home() / ".darwin_perf" / "ids_baseline.json"
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Load existing baseline if present
    existing = {}
    if dst.exists():
        with open(dst) as f:
            existing = json.load(f)

    # Merge: union of sets, concatenate hourly stats (trim to 1000)
    merged = {
        "known_processes": list(
            set(existing.get("known_processes", []))
            | set(imported.get("known_processes", []))
        ),
        "known_listening_ports": list(
            set(existing.get("known_listening_ports", []))
            | set(imported.get("known_listening_ports", []))
        ),
        "known_remote_addrs": list(
            set(existing.get("known_remote_addrs", []))
            | set(imported.get("known_remote_addrs", []))
        ),
        "samples": existing.get("samples", 0) + imported.get("samples", 0),
    }

    # Merge hourly stats arrays
    for key in ("hourly_net_bytes", "hourly_cpu_pct", "hourly_gpu_pct"):
        existing_hourly = existing.get(key, {})
        imported_hourly = imported.get(key, {})
        all_hours = set(existing_hourly.keys()) | set(imported_hourly.keys())
        merged_hourly = {}
        for h in all_hours:
            combined = existing_hourly.get(h, []) + imported_hourly.get(h, [])
            merged_hourly[h] = combined[-1000:]  # keep last 1000
        merged[key] = merged_hourly

    with open(dst, "w") as f:
        json.dump(merged, f)

    print(f"Baseline imported and merged into {dst}")
    print(f"  Samples: {merged['samples']}")
    print(f"  Known processes: {len(merged['known_processes'])}")
    print(f"  Known ports: {len(merged['known_listening_ports'])}")
    print(f"  Known remotes: {len(merged['known_remote_addrs'])}")


def _run_ids_analyze(args):
    """Analyze a recorded JSONL file with the LLM for security issues."""
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    from darwin_perf._ids import LLMAnalyzer

    print(f"Analyzing {args.ids_analyze} with local LLM...")
    analyzer = LLMAnalyzer(model_path=args.model)
    assessment = analyzer.analyze_log_file(args.ids_analyze)
    print(f"\n{'='*60}")
    print("SECURITY ASSESSMENT")
    print(f"{'='*60}\n")
    print(assessment)
    print()


def _run_net(args):
    """Show live network monitoring."""
    from darwin_perf._network import network_delta, network_snapshot, per_process_network

    prev = network_snapshot()
    time.sleep(args.interval)

    iteration = 0
    while True:
        curr = network_snapshot()
        delta = network_delta(prev, curr)
        prev = curr

        if not args.once:
            print("\033[2J\033[H", end="")

        timestamp = time.strftime("%H:%M:%S")
        print(f"darwin-perf network  {timestamp}  (every {args.interval}s)\n")

        # Traffic rates
        sent_rate = delta.bytes_sent_per_s
        recv_rate = delta.bytes_recv_per_s
        print(f"  Upload:   {_fmt_rate(sent_rate)}")
        print(f"  Download: {_fmt_rate(recv_rate)}")
        print(f"  Connections: {len(delta.active_connections)}  "
              f"New: {len(delta.new_connections)}  "
              f"Closed: {len(delta.closed_connections)}")
        if delta.errors or delta.drops:
            print(f"  Errors: {delta.errors}  Drops: {delta.drops}")
        print()

        # Listening ports
        if delta.listening_ports:
            print("  Listening ports:")
            for lp in sorted(delta.listening_ports, key=lambda x: x["port"]):
                print(f"    :{lp['port']}  {lp['name']} (pid {lp['pid']})")
            print()

        # Active connections (non-loopback)
        external = [
            c for c in delta.active_connections
            if c.remote_addr and c.status == "ESTABLISHED"
            and not c.remote_addr.startswith("127.")
            and c.remote_addr not in ("", "::1")
        ]
        if external:
            print(f"  Active external connections ({len(external)}):")
            seen = set()
            for c in external[:20]:
                key = f"{c.name}:{c.remote_addr}:{c.remote_port}"
                if key in seen:
                    continue
                seen.add(key)
                print(f"    {c.name:<20} → {c.remote_addr}:{c.remote_port}  [{c.status}]")
            print()

        iteration += 1
        if 0 < args.count <= iteration:
            break
        time.sleep(args.interval)


def _fmt_rate(bytes_per_s: float) -> str:
    """Format bytes/s as human-readable rate."""
    if bytes_per_s < 1024:
        return f"{bytes_per_s:.0f} B/s"
    if bytes_per_s < 1024 * 1024:
        return f"{bytes_per_s / 1024:.1f} KB/s"
    if bytes_per_s < 1024 * 1024 * 1024:
        return f"{bytes_per_s / 1024 / 1024:.1f} MB/s"
    return f"{bytes_per_s / 1024 / 1024 / 1024:.1f} GB/s"
