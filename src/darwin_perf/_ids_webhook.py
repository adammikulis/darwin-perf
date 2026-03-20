"""Webhook alerting for IDS HIGH/CRITICAL alerts.

Sends POST requests to Slack or generic webhook endpoints when
high-severity alerts fire. Rate-limited to 1 notification per rule
per 5 minutes.

Usage::

    from darwin_perf._ids_webhook import WebhookNotifier

    notifier = WebhookNotifier("https://hooks.slack.com/services/T.../B.../xxx")
    notifier.send_alert(alert)  # non-blocking, runs in background thread
    notifier.shutdown()
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._ids_rules import Alert

logger = logging.getLogger("darwin_perf.ids.webhook")


class WebhookNotifier:
    """Sends IDS alerts to a webhook URL.

    Auto-detects Slack vs. generic format from the URL. Rate-limits to at
    most one notification per *rule* per *rate_limit_seconds*.

    Args:
        url: Webhook endpoint URL.
        rate_limit_seconds: Minimum seconds between alerts for the same rule
            (default 300 = 5 minutes).
    """

    def __init__(self, url: str, rate_limit_seconds: float = 300.0) -> None:
        self.url = url
        self.rate_limit_seconds = rate_limit_seconds
        self._is_slack = "hooks.slack.com" in url
        self._last_sent: dict[str, float] = {}  # rule_key -> epoch
        self._lock = threading.Lock()
        self._executor_running = True

    def send_alert(self, alert: Alert) -> None:
        """Send an alert if it passes rate limiting.

        Only HIGH and CRITICAL severity alerts are sent. The actual HTTP
        request runs in a daemon thread so it never blocks the monitor loop.
        """
        if alert.severity not in ("high", "critical"):
            return

        rule_key = f"{alert.category}:{alert.rule}"
        now = time.time()

        with self._lock:
            last = self._last_sent.get(rule_key, 0.0)
            if now - last < self.rate_limit_seconds:
                return
            self._last_sent[rule_key] = now

        # Fire in background thread
        t = threading.Thread(
            target=self._do_send,
            args=(alert,),
            daemon=True,
            name="ids-webhook",
        )
        t.start()

    def _do_send(self, alert: Alert) -> None:
        """Perform the actual HTTP POST (runs in background thread)."""
        try:
            payload = self._format_payload(alert)
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                _ = resp.read()
            logger.debug("Webhook sent for %s/%s", alert.category, alert.rule)
        except Exception as e:
            logger.warning("Webhook delivery failed: %s", e)

    def _format_payload(self, alert: Alert) -> dict:
        """Build the JSON payload for the webhook.

        Slack-formatted if the URL matches hooks.slack.com, otherwise sends
        the full alert dict.
        """
        sev = alert.severity.upper()
        icon = "\U0001f6a8" if sev == "CRITICAL" else "\u26a0\ufe0f"

        if self._is_slack:
            return {
                "text": (
                    f"{icon} [{sev}] darwin-perf IDS: "
                    f"{alert.category}/{alert.rule}\n"
                    f"{alert.description}"
                ),
            }

        # Generic webhook: send full alert as JSON
        return alert.to_dict()

    def shutdown(self) -> None:
        """Mark the notifier as shut down (background threads are daemon)."""
        self._executor_running = False
