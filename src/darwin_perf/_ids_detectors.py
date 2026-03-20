"""IDS anomaly detector functions.

All detect_* functions live here. Constants, helpers, Alert, and Severity
are imported from _ids_rules.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
from datetime import datetime
from typing import Any

from ._ids_rules import (
    DANGEROUS_LINEAGE_PARENTS,
    MINING_PORTS,
    SENSITIVE_FILE_PATTERNS,
    SHELL_NAMES,
    SUSPICIOUS_PORTS,
    SUSPICIOUS_PROCESS_PATTERNS,
    SYSTEM_LISTEN_PORTS,
    Alert,
    Severity,
    _alert_id,
    _is_late_night,
    _is_private,
    _is_user_idle,
)

logger = logging.getLogger("darwin_perf.ids")


# ---------------------------------------------------------------------------
# Network anomaly detection
# ---------------------------------------------------------------------------

def detect_network_anomalies(
    delta: Any,  # NetworkDelta
    baseline: Any,  # BaselineTracker
    iface_counters: dict[str, dict] | None = None,
) -> list[Alert]:
    """Detect network-related anomalies.

    Args:
        delta: NetworkDelta from network_delta().
        baseline: BaselineTracker instance.
        iface_counters: Optional per-interface counters from net_io_per_iface().
            When provided, flags traffic on VPN/tunnel interfaces (utun*) if
            baseline shows no prior VPN usage.
    """
    alerts: list[Alert] = []
    now = time.time()
    hour = datetime.now().hour

    # --- Unusual traffic volume ---
    if baseline.is_warm():
        mean, std = baseline.net_bytes_stats(hour)
        total_rate = delta.bytes_sent_per_s + delta.bytes_recv_per_s
        if std > 0 and std != float("inf"):
            z_score = (total_rate - mean) / std
            if z_score > 3.0:
                alerts.append(Alert(
                    alert_id=_alert_id("net_volume", now),
                    timestamp=now,
                    category="network",
                    rule="unusual_traffic_volume",
                    severity=Severity.MEDIUM if z_score < 5 else Severity.HIGH,
                    description=(
                        f"Network traffic {total_rate/1024:.0f} KB/s is {z_score:.1f}\u03c3 "
                        f"above normal ({mean/1024:.0f} \u00b1 {std/1024:.0f} KB/s) for hour {hour}"
                    ),
                    evidence={
                        "bytes_per_s": round(total_rate),
                        "mean": round(mean),
                        "std": round(std),
                        "z_score": round(z_score, 1),
                        "hour": hour,
                    },
                ))

    # --- Large data exfiltration (>50MB/s upload) ---
    if delta.bytes_sent_per_s > 50 * 1024 * 1024:
        alerts.append(Alert(
            alert_id=_alert_id("data_exfil", now),
            timestamp=now,
            category="network",
            rule="large_upload",
            severity=Severity.HIGH,
            description=f"Large upload detected: {delta.bytes_sent_per_s / 1024 / 1024:.1f} MB/s",
            evidence={
                "bytes_sent_per_s": round(delta.bytes_sent_per_s),
                "mb_per_s": round(delta.bytes_sent_per_s / 1024 / 1024, 1),
            },
        ))

    # --- Network errors/drops spike ---
    if delta.errors > 100 or delta.drops > 100:
        alerts.append(Alert(
            alert_id=_alert_id("net_errors", now),
            timestamp=now,
            category="network",
            rule="network_errors",
            severity=Severity.LOW,
            description=f"Network errors: {delta.errors} errors, {delta.drops} drops in {delta.interval_s:.0f}s",
            evidence={"errors": delta.errors, "drops": delta.drops},
        ))

    # --- New listening ports ---
    for port_info in delta.listening_ports:
        port = port_info["port"]
        if not baseline.is_known_port(port) and port not in SYSTEM_LISTEN_PORTS and baseline.is_warm():
            alerts.append(Alert(
                alert_id=_alert_id(f"new_port_{port}", now),
                timestamp=now,
                category="network",
                rule="new_listening_port",
                severity=Severity.MEDIUM if port not in SUSPICIOUS_PORTS else Severity.HIGH,
                description=f"New listening port {port} opened by {port_info['name']} (pid {port_info['pid']})",
                evidence=port_info,
            ))

    # --- Connections to suspicious ports ---
    for conn in delta.active_connections:
        if conn.remote_port in SUSPICIOUS_PORTS and not _is_private(conn.remote_addr):
            alerts.append(Alert(
                alert_id=_alert_id(f"sus_port_{conn.remote_port}_{conn.pid}", now),
                timestamp=now,
                category="network",
                rule="suspicious_port_connection",
                severity=Severity.HIGH,
                description=(
                    f"{conn.name} (pid {conn.pid}) connected to suspicious port "
                    f"{conn.remote_addr}:{conn.remote_port}"
                ),
                evidence=conn.to_dict(),
            ))

        # Mining pool ports
        if conn.remote_port in MINING_PORTS and not _is_private(conn.remote_addr):
            alerts.append(Alert(
                alert_id=_alert_id(f"mining_{conn.pid}", now),
                timestamp=now,
                category="network",
                rule="crypto_mining_connection",
                severity=Severity.CRITICAL,
                description=(
                    f"{conn.name} (pid {conn.pid}) connected to likely mining pool "
                    f"{conn.remote_addr}:{conn.remote_port}"
                ),
                evidence=conn.to_dict(),
            ))

    # --- Many new connections (port scan / spray) ---
    if len(delta.new_connections) > 20:
        unique_remotes = {c.remote_addr for c in delta.new_connections if not _is_private(c.remote_addr)}
        unique_ports = {c.remote_port for c in delta.new_connections}
        if len(unique_ports) > 10 or len(unique_remotes) > 10:
            alerts.append(Alert(
                alert_id=_alert_id("conn_burst", now),
                timestamp=now,
                category="network",
                rule="connection_burst",
                severity=Severity.MEDIUM,
                description=(
                    f"{len(delta.new_connections)} new connections in {delta.interval_s:.0f}s "
                    f"({len(unique_remotes)} unique remotes, {len(unique_ports)} unique ports)"
                ),
                evidence={
                    "new_connections": len(delta.new_connections),
                    "unique_remotes": len(unique_remotes),
                    "unique_ports": len(unique_ports),
                },
            ))

    # --- Unknown remote addresses ---
    if baseline.is_warm():
        for conn in delta.new_connections:
            addr = conn.remote_addr
            if addr and not _is_private(addr) and not baseline.is_known_remote(addr):
                alerts.append(Alert(
                    alert_id=_alert_id(f"new_remote_{addr}", now),
                    timestamp=now,
                    category="network",
                    rule="new_remote_address",
                    severity=Severity.LOW,
                    description=f"Connection to previously unseen address: {addr}:{conn.remote_port} by {conn.name}",
                    evidence=conn.to_dict(),
                ))

    # --- VPN/tunnel interface traffic when baseline has none ---
    if iface_counters and baseline.is_warm():
        for iface, counters in iface_counters.items():
            if not iface.startswith("utun"):
                continue
            total_bytes = counters.get("bytes_sent", 0) + counters.get("bytes_recv", 0)
            if total_bytes <= 0:
                continue
            if not baseline.has_vpn_traffic():
                alerts.append(Alert(
                    alert_id=_alert_id(f"vpn_iface_{iface}", now),
                    timestamp=now,
                    category="network",
                    rule="unexpected_vpn_tunnel",
                    severity=Severity.HIGH,
                    description=(
                        f"Traffic on VPN/tunnel interface {iface} "
                        f"({total_bytes / 1024:.0f} KB) with no prior VPN baseline"
                    ),
                    evidence={
                        "interface": iface,
                        "bytes_sent": counters.get("bytes_sent", 0),
                        "bytes_recv": counters.get("bytes_recv", 0),
                    },
                ))
                break  # one alert for all utun interfaces is enough

    return alerts


# ---------------------------------------------------------------------------
# Temporal anomaly detection
# ---------------------------------------------------------------------------

def detect_temporal_anomalies(
    cpu_pct: float,
    gpu_pct: float,
    net_bytes_per_s: float,
    active_processes: list[dict],
    baseline: Any,  # BaselineTracker
) -> list[Alert]:
    """Detect anomalies related to time-of-day and user activity."""
    alerts: list[Alert] = []
    now = time.time()

    late_night = _is_late_night()
    user_idle = _is_user_idle()

    # --- Late night activity ---
    if late_night and (cpu_pct > 30 or gpu_pct > 20 or net_bytes_per_s > 1024 * 1024):
        alerts.append(Alert(
            alert_id=_alert_id("late_night", now),
            timestamp=now,
            category="temporal",
            rule="late_night_activity",
            severity=Severity.MEDIUM,
            description=(
                f"Significant activity at {datetime.now().strftime('%H:%M')}: "
                f"CPU {cpu_pct:.0f}%, GPU {gpu_pct:.0f}%, "
                f"Net {net_bytes_per_s/1024:.0f} KB/s"
            ),
            evidence={
                "hour": datetime.now().hour,
                "cpu_pct": round(cpu_pct, 1),
                "gpu_pct": round(gpu_pct, 1),
                "net_bytes_per_s": round(net_bytes_per_s),
                "top_processes": [
                    {"name": p.get("name", "?"), "cpu": p.get("cpu_percent", 0)}
                    for p in active_processes[:5]
                ],
            },
        ))

    # --- Activity while user is idle ---
    if user_idle and (net_bytes_per_s > 512 * 1024 or gpu_pct > 30):
        alerts.append(Alert(
            alert_id=_alert_id("idle_activity", now),
            timestamp=now,
            category="temporal",
            rule="idle_user_activity",
            severity=Severity.HIGH if net_bytes_per_s > 5 * 1024 * 1024 else Severity.MEDIUM,
            description=(
                f"Significant network/GPU activity while user idle: "
                f"Net {net_bytes_per_s/1024:.0f} KB/s, GPU {gpu_pct:.0f}%"
            ),
            evidence={
                "user_idle": True,
                "net_bytes_per_s": round(net_bytes_per_s),
                "gpu_pct": round(gpu_pct, 1),
            },
        ))

    # --- Unusual resource usage for time of day ---
    if baseline.is_warm():
        hour = datetime.now().hour
        cpu_mean, cpu_std = baseline.cpu_stats(hour)
        gpu_mean, gpu_std = baseline.gpu_stats(hour)

        if cpu_std > 0 and cpu_std != float("inf"):
            z = (cpu_pct - cpu_mean) / cpu_std
            if z > 4.0:
                alerts.append(Alert(
                    alert_id=_alert_id("cpu_anomaly", now),
                    timestamp=now,
                    category="temporal",
                    rule="unusual_cpu_for_hour",
                    severity=Severity.LOW,
                    description=(
                        f"CPU usage {cpu_pct:.0f}% is {z:.1f}\u03c3 above normal "
                        f"({cpu_mean:.0f}% \u00b1 {cpu_std:.0f}%) for {hour}:00"
                    ),
                    evidence={
                        "cpu_pct": round(cpu_pct, 1),
                        "mean": round(cpu_mean, 1),
                        "std": round(cpu_std, 1),
                        "z_score": round(z, 1),
                        "hour": hour,
                    },
                ))

        if gpu_std > 0 and gpu_std != float("inf"):
            z = (gpu_pct - gpu_mean) / gpu_std
            if z > 4.0:
                alerts.append(Alert(
                    alert_id=_alert_id("gpu_anomaly", now),
                    timestamp=now,
                    category="temporal",
                    rule="unusual_gpu_for_hour",
                    severity=Severity.LOW,
                    description=(
                        f"GPU usage {gpu_pct:.0f}% is {z:.1f}\u03c3 above normal "
                        f"({gpu_mean:.0f}% \u00b1 {gpu_std:.0f}%) for {hour}:00"
                    ),
                    evidence={
                        "gpu_pct": round(gpu_pct, 1),
                        "mean": round(gpu_mean, 1),
                        "std": round(gpu_std, 1),
                        "z_score": round(z, 1),
                        "hour": hour,
                    },
                ))

    return alerts


# ---------------------------------------------------------------------------
# Process anomaly detection
# ---------------------------------------------------------------------------

def detect_process_anomalies(
    processes: list[dict],
    baseline: Any,  # BaselineTracker
) -> list[Alert]:
    """Detect suspicious process behavior."""
    alerts: list[Alert] = []
    now = time.time()

    for proc in processes:
        name = proc.get("name", "")

        # --- Suspicious process names ---
        for pattern in SUSPICIOUS_PROCESS_PATTERNS:
            if re.search(pattern, name, re.IGNORECASE):
                alerts.append(Alert(
                    alert_id=_alert_id(f"sus_proc_{name}_{proc.get('pid', 0)}", now),
                    timestamp=now,
                    category="process",
                    rule="suspicious_process_name",
                    severity=Severity.HIGH,
                    description=f"Suspicious process detected: {name} (pid {proc.get('pid', 0)})",
                    evidence=proc,
                ))
                break

        # --- New unknown processes using GPU ---
        if baseline.is_warm() and not baseline.is_known_process(name):
            gpu_pct = proc.get("gpu_percent", 0)
            if gpu_pct > 5:
                alerts.append(Alert(
                    alert_id=_alert_id(f"new_gpu_proc_{name}", now),
                    timestamp=now,
                    category="process",
                    rule="unknown_gpu_process",
                    severity=Severity.MEDIUM,
                    description=f"Unknown process {name} using GPU ({gpu_pct:.1f}%)",
                    evidence=proc,
                ))

        # --- High GPU + high network (crypto mining pattern) ---
        gpu_pct = proc.get("gpu_percent", 0)
        cpu_pct = proc.get("cpu_percent", 0)
        if gpu_pct > 80 and cpu_pct > 50:
            alerts.append(Alert(
                alert_id=_alert_id(f"high_resource_{name}", now),
                timestamp=now,
                category="process",
                rule="high_resource_usage",
                severity=Severity.MEDIUM,
                description=(
                    f"{name} consuming GPU {gpu_pct:.0f}% + CPU {cpu_pct:.0f}% "
                    f"\u2014 possible mining or unauthorized compute"
                ),
                evidence=proc,
            ))

    return alerts


# ---------------------------------------------------------------------------
# Process lineage anomaly detection
# ---------------------------------------------------------------------------

def detect_lineage_anomalies(
    processes: list[dict],
    baseline: Any,  # BaselineTracker
) -> list[Alert]:
    """Detect suspicious process parent chains via proc_lineage().

    Calls proc_lineage(pid) for processes that match suspicious patterns or
    are unknown to the baseline. Alerts on chains like
    sshd -> bash -> nc (remote shell spawning netcat).
    """
    from ._native import proc_lineage

    alerts: list[Alert] = []
    now = time.time()
    checked_pids: set[int] = set()

    for proc in processes:
        pid = proc.get("pid", 0)
        if pid <= 0 or pid in checked_pids:
            continue
        name = proc.get("name", "")

        # Only check processes that are suspicious or unknown to baseline
        is_suspicious = any(
            re.search(p, name, re.IGNORECASE) for p in SUSPICIOUS_PROCESS_PATTERNS
        )
        is_unknown = baseline.is_warm() and not baseline.is_known_process(name)
        if not is_suspicious and not is_unknown:
            continue

        checked_pids.add(pid)

        try:
            chain = proc_lineage(pid)
        except Exception:
            continue

        if len(chain) < 2:
            continue

        chain_names = [entry["name"] for entry in chain]

        # Look for dangerous patterns: a known remote-access or server process
        # somewhere in the ancestry, with a shell in between
        has_dangerous_parent = False
        has_shell_in_chain = False
        dangerous_ancestor = ""

        for entry in chain[1:]:  # skip the process itself
            ancestor_name = entry["name"]
            if ancestor_name in SHELL_NAMES:
                has_shell_in_chain = True
            if ancestor_name in DANGEROUS_LINEAGE_PARENTS:
                has_dangerous_parent = True
                dangerous_ancestor = ancestor_name

        if has_dangerous_parent and (is_suspicious or has_shell_in_chain):
            chain_str = " -> ".join(chain_names)
            alerts.append(Alert(
                alert_id=_alert_id(f"lineage_{pid}_{dangerous_ancestor}", now),
                timestamp=now,
                category="process",
                rule="suspicious_lineage",
                severity=Severity.CRITICAL if is_suspicious else Severity.HIGH,
                description=(
                    f"Suspicious process chain: {chain_str} "
                    f"({dangerous_ancestor} spawned {name})"
                ),
                evidence={
                    "pid": pid,
                    "name": name,
                    "chain": chain,
                    "dangerous_ancestor": dangerous_ancestor,
                },
            ))

    return alerts


# ---------------------------------------------------------------------------
# File access anomaly detection
# ---------------------------------------------------------------------------

def detect_file_access_anomalies(
    processes: list[dict],
) -> list[Alert]:
    """Detect processes accessing sensitive files via proc_open_files().

    Calls proc_open_files(pid) for network-active processes and alerts
    when they have open file handles to keychains, SSH keys, credentials, etc.
    """
    from ._native import proc_open_files

    alerts: list[Alert] = []
    now = time.time()
    checked_pids: set[int] = set()

    for proc in processes:
        pid = proc.get("pid", 0)
        if pid <= 0 or pid in checked_pids:
            continue

        # Only check processes that have network connections
        has_network = (
            proc.get("connections", 0) > 0
            or proc.get("established", 0) > 0
            or proc.get("net_bytes", 0) > 0
        )
        # Also check suspicious processes regardless of network
        name = proc.get("name", "")
        is_suspicious = any(
            re.search(p, name, re.IGNORECASE) for p in SUSPICIOUS_PROCESS_PATTERNS
        )

        if not has_network and not is_suspicious:
            continue

        checked_pids.add(pid)

        try:
            open_files = proc_open_files(pid)
        except Exception:
            continue

        sensitive_hits: list[str] = []
        for fd_info in open_files:
            path = fd_info.get("path", "")
            if not path:
                continue
            for pattern in SENSITIVE_FILE_PATTERNS:
                if pattern in path:
                    sensitive_hits.append(path)
                    break

        if sensitive_hits:
            alerts.append(Alert(
                alert_id=_alert_id(f"sensitive_file_{pid}_{name}", now),
                timestamp=now,
                category="process",
                rule="sensitive_file_access",
                severity=Severity.CRITICAL if len(sensitive_hits) > 2 else Severity.HIGH,
                description=(
                    f"{name} (pid {pid}) has {len(sensitive_hits)} sensitive file(s) open: "
                    f"{', '.join(sensitive_hits[:5])}"
                ),
                evidence={
                    "pid": pid,
                    "name": name,
                    "sensitive_files": sensitive_hits[:20],
                    "total_open_fds": len(open_files),
                },
            ))

    return alerts


# ---------------------------------------------------------------------------
# Auth anomaly detection (macOS Unified Log)
# ---------------------------------------------------------------------------

# Module-level cache for auth monitoring
_auth_last_check: float = 0.0
_AUTH_CHECK_INTERVAL: float = 30.0  # Only check every 30 seconds


def detect_auth_anomalies(
    baseline: Any,  # BaselineTracker
) -> list[Alert]:
    """Detect authentication anomalies from macOS Unified Log.

    Reads recent auth events (last 60s) from the macOS ``log`` command,
    parsing for failed authentications, sudo usage, SSH attempts,
    screensaver unlock failures, and TCC permission grants.

    Rate-limited to one check every 30 seconds to avoid log spam.
    """
    global _auth_last_check

    now = time.time()
    if now - _auth_last_check < _AUTH_CHECK_INTERVAL:
        return []
    _auth_last_check = now

    alerts: list[Alert] = []

    try:
        result = subprocess.run(
            [
                "log", "show", "--predicate",
                '(subsystem == "com.apple.securityd") OR '
                '(subsystem == "com.apple.opendirectoryd") OR '
                '(eventMessage CONTAINS "sudo") OR '
                '(eventMessage CONTAINS "ssh") OR '
                '(eventMessage CONTAINS "Authentication failed") OR '
                '(eventMessage CONTAINS "screensaver")',
                "--last", "60s", "--style", "json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.debug("Auth log query failed: %s", e)
        return []

    if result.returncode != 0 or not result.stdout.strip():
        return []

    # Parse JSON output — macOS log outputs a JSON array
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Sometimes the output has trailing data; try to find the array
        try:
            start = result.stdout.index("[")
            end = result.stdout.rindex("]") + 1
            entries = json.loads(result.stdout[start:end])
        except (ValueError, json.JSONDecodeError):
            logger.debug("Failed to parse auth log JSON")
            return []

    if not isinstance(entries, list):
        return []

    # Categorize events
    failed_auth_count = 0
    sudo_events: list[dict] = []
    ssh_failed_count = 0
    ssh_success_count = 0
    screensaver_fail_count = 0
    tcc_grants: list[str] = []

    for entry in entries:
        msg = entry.get("eventMessage", "")
        subsystem = entry.get("subsystem", "")
        if not msg:
            continue

        msg_lower = msg.lower()

        if "authentication failed" in msg_lower or "auth failure" in msg_lower:
            failed_auth_count += 1

        if "sudo" in msg_lower:
            sudo_events.append({
                "message": msg[:200],
                "process": entry.get("processImagePath", ""),
            })

        if "ssh" in msg_lower:
            if "failed" in msg_lower or "invalid" in msg_lower or "denied" in msg_lower:
                ssh_failed_count += 1
            elif "accepted" in msg_lower or "authenticated" in msg_lower:
                ssh_success_count += 1

        if "screensaver" in msg_lower and ("fail" in msg_lower or "denied" in msg_lower):
            screensaver_fail_count += 1

        if subsystem == "com.apple.TCC" or "tcc" in msg_lower:
            if "grant" in msg_lower or "allowed" in msg_lower:
                tcc_grants.append(msg[:200])

    # --- Generate alerts ---

    if failed_auth_count > 3:
        alerts.append(Alert(
            alert_id=_alert_id("auth_failed_burst", now),
            timestamp=now,
            category="auth",
            rule="failed_auth_burst",
            severity=Severity.MEDIUM,
            description=(
                f"{failed_auth_count} failed authentication attempts in last 60s"
            ),
            evidence={
                "failed_count": failed_auth_count,
                "window_seconds": 60,
            },
        ))

    if sudo_events:
        alerts.append(Alert(
            alert_id=_alert_id("sudo_usage", now),
            timestamp=now,
            category="auth",
            rule="sudo_usage",
            severity=Severity.LOW,
            description=f"{len(sudo_events)} sudo event(s) in last 60s",
            evidence={
                "count": len(sudo_events),
                "events": sudo_events[:10],
            },
        ))

    if ssh_failed_count > 0:
        alerts.append(Alert(
            alert_id=_alert_id("ssh_failed", now),
            timestamp=now,
            category="auth",
            rule="ssh_failed_login",
            severity=Severity.MEDIUM,
            description=f"{ssh_failed_count} failed SSH login attempt(s) in last 60s",
            evidence={
                "failed_count": ssh_failed_count,
                "success_count": ssh_success_count,
            },
        ))
    if ssh_success_count > 0:
        alerts.append(Alert(
            alert_id=_alert_id("ssh_success", now),
            timestamp=now,
            category="auth",
            rule="ssh_successful_login",
            severity=Severity.INFO,
            description=f"{ssh_success_count} successful SSH login(s) in last 60s",
            evidence={"success_count": ssh_success_count},
        ))

    if screensaver_fail_count > 3:
        alerts.append(Alert(
            alert_id=_alert_id("screensaver_brute", now),
            timestamp=now,
            category="auth",
            rule="screensaver_brute_force",
            severity=Severity.MEDIUM,
            description=(
                f"{screensaver_fail_count} screensaver unlock failures in last 60s "
                f"— possible brute force"
            ),
            evidence={"fail_count": screensaver_fail_count},
        ))

    if tcc_grants:
        alerts.append(Alert(
            alert_id=_alert_id("tcc_grant", now),
            timestamp=now,
            category="auth",
            rule="tcc_permission_grant",
            severity=Severity.LOW,
            description=f"{len(tcc_grants)} TCC permission grant(s) detected",
            evidence={
                "count": len(tcc_grants),
                "grants": tcc_grants[:10],
            },
        ))

    return alerts


# ---------------------------------------------------------------------------
# Binary hash verification
# ---------------------------------------------------------------------------

# Module-level cache: path -> sha256 hex digest
_binary_hash_cache: dict[str, str] = {}


def _hash_binary(path: str) -> str | None:
    """SHA-256 hash a binary file. Returns hex digest or None on failure."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return None


def detect_binary_anomalies(
    processes: list[dict],
    baseline: Any,  # BaselineTracker
) -> list[Alert]:
    """Detect unknown or modified binaries via SHA-256 hash verification.

    For each running process:
    1. Resolve executable path via ``proc_pidpath(pid)``
    2. Hash the binary (cached after first read)
    3. Compare against baseline's ``known_binary_hashes``
    4. Alert MEDIUM if binary is unknown, HIGH if hash changed for known path
    """
    from ._native import proc_pidpath

    alerts: list[Alert] = []
    now = time.time()
    checked_paths: set[str] = set()

    for proc in processes:
        pid = proc.get("pid", 0)
        if pid <= 0:
            continue

        # Resolve executable path
        try:
            exe_path = proc_pidpath(pid)
        except Exception:
            continue

        if not exe_path or exe_path in checked_paths:
            continue
        checked_paths.add(exe_path)

        # Skip system frameworks and dylibs (too many, low risk)
        if exe_path.startswith("/System/") or exe_path.startswith("/usr/lib/"):
            continue

        # Get or compute hash
        if exe_path in _binary_hash_cache:
            current_hash = _binary_hash_cache[exe_path]
        else:
            current_hash = _hash_binary(exe_path)
            if current_hash is None:
                continue
            _binary_hash_cache[exe_path] = current_hash

        # Update baseline and check for anomalies
        if baseline.is_warm():
            if not baseline.is_known_binary(exe_path, current_hash):
                # Check if path was known with a different hash
                known_hash = baseline.get_binary_hash(exe_path)
                if known_hash is not None and known_hash != current_hash:
                    # Hash changed — binary was modified
                    alerts.append(Alert(
                        alert_id=_alert_id(f"binary_modified_{exe_path}", now),
                        timestamp=now,
                        category="process",
                        rule="binary_modified",
                        severity=Severity.HIGH,
                        description=(
                            f"Binary modified: {exe_path} hash changed "
                            f"(was {known_hash[:16]}..., now {current_hash[:16]}...)"
                        ),
                        evidence={
                            "path": exe_path,
                            "old_hash": known_hash,
                            "new_hash": current_hash,
                            "pid": pid,
                            "name": proc.get("name", ""),
                        },
                    ))
                elif known_hash is None:
                    # Completely unknown binary
                    alerts.append(Alert(
                        alert_id=_alert_id(f"unknown_binary_{exe_path}", now),
                        timestamp=now,
                        category="process",
                        rule="unknown_binary",
                        severity=Severity.MEDIUM,
                        description=f"Unknown binary: {exe_path} (pid {pid}, {proc.get('name', '')})",
                        evidence={
                            "path": exe_path,
                            "hash": current_hash,
                            "pid": pid,
                            "name": proc.get("name", ""),
                        },
                    ))

        # Always record in baseline for future comparisons
        baseline.update_binary_hash(exe_path, current_hash)

    return alerts
