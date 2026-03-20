"""IDS LLM analyzer — local Qwen3.5-0.6B via llama-server (HTTP API)."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from ._ids_rules import Alert

logger = logging.getLogger("darwin_perf.ids")

DEFAULT_MODEL_REPO = "unsloth/Qwen3.5-0.6B-GGUF"
DEFAULT_MODEL_FILE = "Qwen3.5-0.6B-Q8_0.gguf"
DEFAULT_IDS_PORT = 8041  # dedicated port for IDS llama-server


def _find_llama_server() -> str:
    """Find llama-server binary."""
    import shutil
    path = shutil.which("llama-server")
    if path:
        return path
    # Common homebrew location
    for candidate in ["/opt/homebrew/bin/llama-server", "/usr/local/bin/llama-server"]:
        if os.path.isfile(candidate):
            return candidate
    raise FileNotFoundError(
        "llama-server not found. Install via: brew install llama.cpp"
    )


def _get_model_path(model_path: str | Path | None = None) -> Path:
    """Find or download the GGUF model for IDS analysis."""
    if model_path:
        p = Path(model_path)
        if p.exists():
            return p
        raise FileNotFoundError(f"Model not found: {p}")

    env_path = os.environ.get("DARWIN_PERF_IDS_MODEL")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    # Check local cache
    cache_dir = Path.home() / ".cache" / "darwin_perf" / "models"
    model_path_local = cache_dir / DEFAULT_MODEL_FILE
    if model_path_local.exists():
        return model_path_local

    # Download via huggingface-cli or huggingface_hub
    logger.info("Downloading IDS model %s/%s ...", DEFAULT_MODEL_REPO, DEFAULT_MODEL_FILE)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Try huggingface-cli first (no pip install needed if hf already present)
    import shutil
    import subprocess as _sp
    if shutil.which("huggingface-cli"):
        _sp.run([
            "huggingface-cli", "download",
            DEFAULT_MODEL_REPO, DEFAULT_MODEL_FILE,
            "--local-dir", str(cache_dir),
        ], check=True)
        if model_path_local.exists():
            return model_path_local

    # Fallback to huggingface_hub Python API
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        import sys
        _sp.check_call([sys.executable, "-m", "pip", "install", "huggingface_hub", "-q"])
        from huggingface_hub import hf_hub_download

    downloaded = hf_hub_download(
        repo_id=DEFAULT_MODEL_REPO,
        filename=DEFAULT_MODEL_FILE,
        local_dir=str(cache_dir),
        local_dir_use_symlinks=False,
    )
    return Path(downloaded)


def _chat_completion(port: int, messages: list[dict], max_tokens: int = 512) -> str:
    """Call llama-server's OpenAI-compatible /v1/chat/completions endpoint.

    Uses only stdlib urllib — no requests/httpx needed.
    """
    import urllib.request

    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    payload = json.dumps({
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())

    text = data["choices"][0]["message"]["content"]
    # Strip thinking blocks if present (Qwen3.5 thinking mode)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return text


class LLMAnalyzer:
    """Local LLM for analyzing anomaly logs and providing threat assessments.

    Auto-starts a dedicated llama-server instance with Qwen3.5-0.6B on a
    private port. Communicates via the OpenAI-compatible HTTP API.
    All data stays local — nothing leaves the machine.
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        port: int = DEFAULT_IDS_PORT,
        n_ctx: int = 4096,
    ) -> None:
        self._model_path = model_path
        self._port = port
        self._n_ctx = n_ctx
        self._server_proc: Any = None
        self._lock = threading.Lock()
        self._ready = False

    def _ensure_server(self) -> None:
        """Start llama-server if not already running."""
        if self._ready:
            return

        with self._lock:
            if self._ready:
                return

            # Check if something is already listening on the port
            if self._health_check():
                self._ready = True
                return

            import subprocess as _sp

            server_bin = _find_llama_server()
            model = _get_model_path(self._model_path)
            logger.info("Starting llama-server on port %d with %s", self._port, model.name)

            self._server_proc = _sp.Popen(
                [
                    server_bin,
                    "-m", str(model),
                    "--port", str(self._port),
                    "-c", str(self._n_ctx),
                    "-ngl", "99",  # offload all layers to GPU
                    "--log-disable",
                ],
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )

            # Wait for server to be ready (up to 30s)
            for _ in range(60):
                time.sleep(0.5)
                if self._health_check():
                    self._ready = True
                    logger.info("llama-server ready on port %d", self._port)
                    return
                if self._server_proc.poll() is not None:
                    raise RuntimeError(
                        f"llama-server exited with code {self._server_proc.returncode}"
                    )

            raise TimeoutError("llama-server did not become ready within 30s")

    def _health_check(self) -> bool:
        """Check if the server is responding."""
        import urllib.request
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._port}/health",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def stop_server(self) -> None:
        """Stop the managed llama-server process."""
        if self._server_proc is not None:
            self._server_proc.terminate()
            try:
                self._server_proc.wait(timeout=5)
            except Exception:
                self._server_proc.kill()
            self._server_proc = None
        self._ready = False

    def analyze_alerts(self, alerts: list[Alert], system_context: dict | None = None) -> str:
        """Have the LLM analyze a batch of alerts and provide assessment.

        Args:
            alerts: List of Alert objects to analyze.
            system_context: Optional dict with current system state.

        Returns:
            LLM's threat assessment as a string.
        """
        if not alerts:
            return "No alerts to analyze."

        self._ensure_server()

        alert_text = "\n".join(
            f"[{a.severity.upper()}] {a.category}/{a.rule}: {a.description}"
            + (f"\n  Evidence: {json.dumps(a.evidence, default=str)}" if a.evidence else "")
            for a in alerts[:20]
        )

        ctx_text = ""
        if system_context:
            ctx_text = f"\nSystem context: {json.dumps(system_context, default=str)}\n"

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a security analyst reviewing system monitoring alerts from a macOS workstation. "
                    "Analyze the alerts and provide:\n"
                    "1. A threat level assessment (SAFE / SUSPICIOUS / DANGEROUS)\n"
                    "2. Which alerts are most concerning and why\n"
                    "3. Whether alerts correlate to suggest a coordinated attack\n"
                    "4. Recommended actions\n"
                    "Be concise and specific. Focus on actionable findings."
                ),
            },
            {
                "role": "user",
                "content": f"{ctx_text}\nAlerts detected in the last monitoring period:\n\n{alert_text}\n\nProvide your security assessment.",
            },
        ]

        return _chat_completion(self._port, messages)

    def analyze_log_file(self, log_path: str | Path) -> str:
        """Analyze a recorded JSONL log file for security issues.

        Args:
            log_path: Path to a darwin-perf JSONL recording.

        Returns:
            LLM assessment of the recording.
        """
        path = Path(log_path)
        if not path.exists():
            return f"File not found: {path}"

        records = []
        with open(path) as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line.strip()))

        if not records:
            return "Empty log file."

        duration = records[-1].get("epoch", 0) - records[0].get("epoch", 0)
        all_procs: set[str] = set()
        max_gpu = 0.0
        max_cpu = 0.0
        total_net_bytes = 0

        for r in records:
            for p in r.get("processes", []):
                all_procs.add(p.get("name", "?"))
                max_gpu = max(max_gpu, p.get("gpu_percent", 0))
                max_cpu = max(max_cpu, p.get("cpu_percent", 0))
            net = r.get("network", {})
            total_net_bytes += net.get("bytes_sent", 0) + net.get("bytes_recv", 0)

        summary = (
            f"Recording: {len(records)} samples over {duration:.0f}s\n"
            f"Time range: {records[0].get('timestamp', '?')} to {records[-1].get('timestamp', '?')}\n"
            f"Processes seen: {', '.join(sorted(all_procs)[:20])}\n"
            f"Peak GPU: {max_gpu:.1f}%, Peak CPU: {max_cpu:.1f}%\n"
            f"Total network: {total_net_bytes / 1024 / 1024:.1f} MB"
        )

        self._ensure_server()

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a security analyst reviewing a system monitoring log from a macOS workstation. Look for:\n"
                    "- Unusual processes or resource usage patterns\n"
                    "- Signs of unauthorized access or data exfiltration\n"
                    "- Crypto mining indicators\n"
                    "- Network anomalies\n"
                    "- Activity during unusual hours\n"
                    "Be concise. Report findings with severity levels."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Analyze this system monitoring log summary for security issues:\n\n{summary}\n\n"
                    f"Detailed samples (first 10 and last 10):\n"
                    f"{json.dumps(records[:10], indent=1, default=str)}\n...\n"
                    f"{json.dumps(records[-10:], indent=1, default=str)}\n\n"
                    f"Provide your security assessment."
                ),
            },
        ]

        return _chat_completion(self._port, messages)
