"""Passive DNS logging via macOS Unified Log mDNSResponder stream.

Maintains an in-memory LRU cache mapping IP -> hostname by parsing
mDNSResponder log entries in real time. No active DNS queries are made.

Usage::

    from darwin_perf._dns_cache import DNSCache

    dns = DNSCache()
    dns.start_log_stream()
    # ... later ...
    hostname = dns.resolve("93.184.216.34")  # -> "example.com" or None
    dns.stop()
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("darwin_perf.ids")

# Patterns to extract DNS response mappings from mDNSResponder logs.
# mDNSResponder logs vary by macOS version; we match common formats:
#   "hostname.example.com. Addr 93.184.216.34"
#   "hostname.example.com. AAAA 2606:2800:..."
#   "<hostname> -> <ip>"
_DNS_RESPONSE_PATTERNS = [
    # "name.example.com. Addr 1.2.3.4" or "name.example.com. AAAA 2606:..."
    re.compile(
        r"(?P<hostname>[\w.\-]+\.[\w.\-]+)\.\s+"
        r"(?:Addr|AAAA)\s+"
        r"(?P<ip>[^\s,;]+)"
    ),
    # "DNSServiceQueryRecord ... name.example.com -> 1.2.3.4"
    re.compile(
        r"(?P<hostname>[\w.\-]+\.[\w.\-]+)\s*->\s*(?P<ip>[^\s,;]+)"
    ),
    # "A/AAAA for name.example.com is 1.2.3.4"
    re.compile(
        r"for\s+(?P<hostname>[\w.\-]+\.[\w.\-]+)\s+is\s+(?P<ip>[^\s,;]+)"
    ),
]


class DNSCache:
    """Passive DNS cache populated from macOS mDNSResponder log stream.

    Thread-safe. Maintains an LRU dict of IP -> (hostname, expire_time)
    with a configurable max size and TTL.

    Args:
        max_size: Maximum number of cached entries (default 10000).
        ttl: Seconds before an entry expires (default 3600 = 1 hour).
    """

    def __init__(self, max_size: int = 10_000, ttl: float = 3600.0) -> None:
        self._max_size = max_size
        self._ttl = ttl
        # OrderedDict for LRU eviction: ip -> (hostname, expire_time)
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def resolve(self, ip: str) -> str | None:
        """Look up a hostname for an IP address from the passive cache.

        Returns the hostname if cached and not expired, else None.
        Does NOT perform active DNS queries.
        """
        with self._lock:
            entry = self._cache.get(ip)
            if entry is None:
                return None
            hostname, expire_time = entry
            if time.time() > expire_time:
                # Expired — remove it
                del self._cache[ip]
                return None
            # Move to end (most recently used)
            self._cache.move_to_end(ip)
            return hostname

    def put(self, ip: str, hostname: str) -> None:
        """Manually insert a DNS mapping (used by detectors or tests)."""
        self._insert(ip, hostname)

    def _insert(self, ip: str, hostname: str) -> None:
        """Insert or update a cache entry, evicting LRU if at capacity."""
        now = time.time()
        with self._lock:
            if ip in self._cache:
                self._cache.move_to_end(ip)
            self._cache[ip] = (hostname, now + self._ttl)
            # Evict oldest if over capacity
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    @property
    def size(self) -> int:
        """Number of entries currently in cache."""
        with self._lock:
            return len(self._cache)

    def start_log_stream(self) -> None:
        """Start a background thread streaming mDNSResponder log entries.

        Spawns ``log stream --predicate 'subsystem == "com.apple.mDNSResponder"'``
        and continuously parses DNS response mappings into the cache.
        """
        if self._thread is not None and self._thread.is_alive():
            return  # Already running

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._stream_loop,
            daemon=True,
            name="dns-cache-stream",
        )
        self._thread.start()
        logger.info("DNS cache log stream started")

    def _stream_loop(self) -> None:
        """Background loop: run ``log stream`` and parse output."""
        try:
            self._proc = subprocess.Popen(
                [
                    "log", "stream",
                    "--predicate", 'subsystem == "com.apple.mDNSResponder"',
                    "--style", "ndjson",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except (FileNotFoundError, OSError) as e:
            logger.warning("Failed to start DNS log stream: %s", e)
            return

        try:
            for line in self._proc.stdout:
                if self._stop_event.is_set():
                    break
                self._parse_line(line)
        except Exception as e:
            if not self._stop_event.is_set():
                logger.debug("DNS stream read error: %s", e)
        finally:
            self._kill_proc()

    def _parse_line(self, line: str) -> None:
        """Extract hostname->IP mappings from a single log line."""
        # Try JSON parse first (ndjson mode)
        msg = line
        if line.strip().startswith("{"):
            try:
                import json
                data = json.loads(line)
                msg = data.get("eventMessage", "")
            except (ValueError, KeyError):
                pass

        if not msg:
            return

        for pattern in _DNS_RESPONSE_PATTERNS:
            for match in pattern.finditer(msg):
                hostname = match.group("hostname").rstrip(".")
                ip = match.group("ip")
                if hostname and ip:
                    self._insert(ip, hostname)

    def _kill_proc(self) -> None:
        """Terminate the log stream subprocess if running."""
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def stop(self) -> None:
        """Stop the log stream and clean up."""
        self._stop_event.set()
        self._kill_proc()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("DNS cache stopped (%d entries)", self.size)
