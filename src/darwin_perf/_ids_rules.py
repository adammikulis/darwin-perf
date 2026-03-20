"""IDS rule constants, Alert/Severity types, and helper functions.

Detector functions (detect_*) live in _ids_detectors.py.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import logging

logger = logging.getLogger("darwin_perf.ids")


# ---------------------------------------------------------------------------
# Alert severity and types
# ---------------------------------------------------------------------------

class Severity:
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class Alert:
    """A detected security anomaly."""
    alert_id: str
    timestamp: float
    category: str  # network, process, resource, temporal, behavioral
    rule: str  # specific rule that triggered
    severity: str
    description: str
    evidence: dict[str, Any] = field(default_factory=dict)
    llm_assessment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "alert_id": self.alert_id,
            "timestamp": self.timestamp,
            "time": datetime.fromtimestamp(self.timestamp).isoformat(),
            "category": self.category,
            "rule": self.rule,
            "severity": self.severity,
            "description": self.description,
            "evidence": self.evidence,
        }
        if self.llm_assessment:
            d["llm_assessment"] = self.llm_assessment
        return d


def _alert_id(key: str, timestamp: float) -> str:
    """Generate a short, unique alert ID."""
    raw = f"{key}:{timestamp}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Well-known ports that are expected to be listening
SYSTEM_LISTEN_PORTS = {
    22, 53, 80, 443, 631,  # ssh, dns, http, https, ipp (printing)
    5000, 5353,  # AirPlay, mDNS
    8080, 8443, 3000, 3100, 5173,  # common dev servers
}

# Suspicious port ranges
SUSPICIOUS_PORTS = {
    4444, 4445, 5555, 6666, 6667, 6697,  # common C2/IRC
    31337, 12345, 1337,  # classic backdoor ports
    9001, 9050, 9150,  # Tor
}

# Known crypto mining pool ports
MINING_PORTS = {3333, 5555, 7777, 8888, 9999, 14433, 14444, 45560, 45700}

# Suspicious process names (partial matches)
SUSPICIOUS_PROCESS_PATTERNS = [
    r"nc\b", r"ncat\b", r"netcat",  # netcat variants
    r"socat\b",  # socket relay
    r"cryptonight", r"xmrig", r"minerd", r"cgminer", r"bfgminer",  # miners
    r"reverse.?shell", r"bind.?shell",
    r"chisel\b", r"ngrok\b", r"frp[cs]?\b",  # tunneling tools
    r"mimikatz", r"lazagne", r"hashcat",  # credential tools
    r"cobaltstrike", r"meterpreter", r"beacon",  # C2 frameworks
]

# Loopback and LAN ranges (not suspicious for outbound)
_PRIVATE_PREFIXES = ("127.", "10.", "172.16.", "172.17.", "172.18.", "172.19.",
                     "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
                     "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
                     "172.30.", "172.31.", "192.168.", "::1", "fe80:")

# Sensitive file paths that should trigger alerts when accessed by
# network-active processes
SENSITIVE_FILE_PATTERNS = [
    "/Library/Keychains/",
    "/Keychains/",
    ".ssh/",
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    ".aws/credentials",
    ".aws/config",
    ".gnupg/",
    ".kube/config",
    ".docker/config.json",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".env",
    ".gitconfig",
    "/.git/config",
    "/etc/krb5.keytab",
    ".bash_history",
    ".zsh_history",
]

# Dangerous parent processes in lineage chains — processes that should not
# normally spawn shells or network tools
DANGEROUS_LINEAGE_PARENTS = {
    "sshd", "telnetd", "rshd", "rlogind",  # remote access
    "httpd", "nginx", "apache2",  # web servers
    "postgres", "mysqld", "mongod",  # databases
    "java", "node", "ruby", "perl", "php",  # interpreters (when spawning shells)
}

SHELL_NAMES = {"bash", "sh", "zsh", "fish", "csh", "tcsh", "dash", "ksh"}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _is_private(addr: str) -> bool:
    return any(addr.startswith(p) for p in _PRIVATE_PREFIXES) or addr in ("", "0.0.0.0", "::", "*")


def _is_late_night() -> bool:
    """Check if current time is during typical sleep hours (midnight-6am)."""
    hour = datetime.now().hour
    return 0 <= hour < 6


def _is_user_idle(idle_threshold_s: float = 300) -> bool:
    """Check if user has been idle (no HID events) for threshold seconds.

    Uses native C hid_idle_ns() via IOKit — no subprocess calls.
    """
    try:
        from ._native import hid_idle_ns
        idle_ns = hid_idle_ns()
        return idle_ns / 1e9 > idle_threshold_s
    except Exception:
        return False
