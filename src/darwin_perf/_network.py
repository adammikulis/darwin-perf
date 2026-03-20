"""Network monitoring for darwin-perf IDS.

All native — uses the C extension for both I/O counters (sysctl NET_RT_IFLIST2)
and socket enumeration (proc_pidinfo). Zero subprocess calls, zero external deps.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ._native import net_io_counters, proc_connections


@dataclass
class ConnectionInfo:
    """A single network connection."""
    pid: int
    name: str
    local_addr: str
    local_port: int
    remote_addr: str
    remote_port: int
    status: str
    family: str  # "ipv4" or "ipv6"
    type: str  # "tcp" or "udp"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "name": self.name,
            "local": f"{self.local_addr}:{self.local_port}",
            "remote": f"{self.remote_addr}:{self.remote_port}",
            "status": self.status,
            "family": self.family,
            "type": self.type,
        }


@dataclass
class NetworkSnapshot:
    """Point-in-time network state."""
    timestamp: float
    bytes_sent: int = 0
    bytes_recv: int = 0
    packets_sent: int = 0
    packets_recv: int = 0
    errin: int = 0
    errout: int = 0
    dropin: int = 0
    dropout: int = 0
    connections: list[ConnectionInfo] = field(default_factory=list)
    listening_ports: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": round(self.timestamp, 3),
            "bytes_sent": self.bytes_sent,
            "bytes_recv": self.bytes_recv,
            "packets_sent": self.packets_sent,
            "packets_recv": self.packets_recv,
            "errin": self.errin,
            "errout": self.errout,
            "dropin": self.dropin,
            "dropout": self.dropout,
            "connection_count": len(self.connections),
            "listening_ports": self.listening_ports,
        }


@dataclass
class NetworkDelta:
    """Network activity over an interval."""
    interval_s: float
    bytes_sent: int = 0
    bytes_recv: int = 0
    bytes_sent_per_s: float = 0.0
    bytes_recv_per_s: float = 0.0
    packets_sent: int = 0
    packets_recv: int = 0
    new_connections: list[ConnectionInfo] = field(default_factory=list)
    closed_connections: list[ConnectionInfo] = field(default_factory=list)
    active_connections: list[ConnectionInfo] = field(default_factory=list)
    listening_ports: list[dict[str, Any]] = field(default_factory=list)
    errors: int = 0
    drops: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "interval_s": round(self.interval_s, 3),
            "bytes_sent": self.bytes_sent,
            "bytes_recv": self.bytes_recv,
            "bytes_sent_per_s": round(self.bytes_sent_per_s, 0),
            "bytes_recv_per_s": round(self.bytes_recv_per_s, 0),
            "packets_sent": self.packets_sent,
            "packets_recv": self.packets_recv,
            "active_connections": len(self.active_connections),
            "new_connections": len(self.new_connections),
            "closed_connections": len(self.closed_connections),
            "listening_ports": len(self.listening_ports),
            "errors": self.errors,
            "drops": self.drops,
        }


def _native_connections() -> list[ConnectionInfo]:
    """Get all sockets via native C proc_connections(0)."""
    raw = proc_connections(0)
    return [
        ConnectionInfo(
            pid=c["pid"],
            name=c["name"],
            local_addr=c["local_addr"],
            local_port=c["local_port"],
            remote_addr=c["remote_addr"],
            remote_port=c["remote_port"],
            status=c["status"],
            family=c["family"],
            type=c["type"],
        )
        for c in raw
    ]


def network_snapshot() -> NetworkSnapshot:
    """Take a snapshot of current network state.

    Uses the native C extension for both I/O counters (sysctl NET_RT_IFLIST2)
    and socket enumeration (proc_pidinfo). No subprocess calls.

    Returns:
        NetworkSnapshot with counters and active connections.
    """
    now = time.time()
    counters = net_io_counters()

    snap = NetworkSnapshot(
        timestamp=now,
        bytes_sent=counters["bytes_sent"],
        bytes_recv=counters["bytes_recv"],
        packets_sent=counters["packets_sent"],
        packets_recv=counters["packets_recv"],
        errin=counters["errin"],
        errout=counters["errout"],
        dropin=counters["dropin"],
        dropout=counters["dropout"],
    )

    conns = _native_connections()
    listening: list[dict[str, Any]] = []

    for info in conns:
        snap.connections.append(info)
        if info.status == "LISTEN":
            listening.append({
                "pid": info.pid,
                "name": info.name,
                "port": info.local_port,
                "addr": info.local_addr,
                "family": info.family,
            })

    snap.listening_ports = listening
    return snap


def network_delta(prev: NetworkSnapshot, curr: NetworkSnapshot) -> NetworkDelta:
    """Compute network activity between two snapshots.

    Args:
        prev: Earlier snapshot.
        curr: Later snapshot.

    Returns:
        NetworkDelta with rates, new/closed connections, etc.
    """
    interval = curr.timestamp - prev.timestamp
    if interval <= 0:
        interval = 1.0

    bs = curr.bytes_sent - prev.bytes_sent
    br = curr.bytes_recv - prev.bytes_recv
    ps = curr.packets_sent - prev.packets_sent
    pr = curr.packets_recv - prev.packets_recv
    errs = (curr.errin - prev.errin) + (curr.errout - prev.errout)
    drops = (curr.dropin - prev.dropin) + (curr.dropout - prev.dropout)

    prev_keys = {_conn_key(c) for c in prev.connections}
    curr_keys = {_conn_key(c) for c in curr.connections}
    curr_by_key = {_conn_key(c): c for c in curr.connections}
    prev_by_key = {_conn_key(c): c for c in prev.connections}

    new_conns = [curr_by_key[k] for k in curr_keys - prev_keys]
    closed_conns = [prev_by_key[k] for k in prev_keys - curr_keys]

    return NetworkDelta(
        interval_s=interval,
        bytes_sent=max(bs, 0),
        bytes_recv=max(br, 0),
        bytes_sent_per_s=max(bs, 0) / interval,
        bytes_recv_per_s=max(br, 0) / interval,
        packets_sent=max(ps, 0),
        packets_recv=max(pr, 0),
        new_connections=new_conns,
        closed_connections=closed_conns,
        active_connections=curr.connections,
        listening_ports=curr.listening_ports,
        errors=max(errs, 0),
        drops=max(drops, 0),
    )


def _conn_key(c: ConnectionInfo) -> tuple:
    return (c.pid, c.local_addr, c.local_port, c.remote_addr, c.remote_port, c.status)


def per_process_network() -> list[dict[str, Any]]:
    """Get per-process network connection summary.

    Uses native C proc_connections for socket enumeration.

    Returns:
        List of dicts sorted by connection count descending.
    """
    conns = _native_connections()
    by_pid: dict[int, dict[str, Any]] = {}

    for c in conns:
        if c.pid not in by_pid:
            by_pid[c.pid] = {
                "pid": c.pid,
                "name": c.name,
                "connections": 0,
                "established": 0,
                "listening": 0,
                "remote_endpoints": set(),
            }
        entry = by_pid[c.pid]
        entry["connections"] += 1
        if c.status == "ESTABLISHED":
            entry["established"] += 1
        if c.status == "LISTEN":
            entry["listening"] += 1
        if c.remote_addr and c.remote_addr not in ("", "0.0.0.0", "::"):
            entry["remote_endpoints"].add(f"{c.remote_addr}:{c.remote_port}")

    results = []
    for entry in by_pid.values():
        entry["remote_endpoints"] = list(entry["remote_endpoints"])[:10]
        results.append(entry)

    results.sort(key=lambda r: r["connections"], reverse=True)
    return results
