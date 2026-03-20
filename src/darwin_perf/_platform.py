"""Platform dispatcher — selects the right monitoring backend at import time.

Detection order:
1. macOS ARM64 → native C extension (_native)
2. NVIDIA GPU → pynvml backend (_nvidia)
3. AMD GPU → pyamdgpuinfo/amdsmi backend (_amd)
4. CPU-only → psutil fallback (_fallback)

All backends expose the same API surface so callers don't need to care.
"""

from __future__ import annotations

import logging
import platform
import types
from typing import Any

logger = logging.getLogger(__name__)

_backend: types.ModuleType | None = None
_backend_name: str = "none"


def _detect_backend() -> tuple[types.ModuleType, str]:
    """Detect and return the best available monitoring backend."""
    system = platform.system()
    machine = platform.machine()

    # macOS ARM64 — use native C extension (production, instant)
    if system == "Darwin" and machine == "arm64":
        try:
            from darwin_perf import _native
            return _native, "darwin_native"
        except ImportError:
            logger.warning("darwin-perf native extension not available, trying fallbacks")

    # NVIDIA — try pynvml
    try:
        import pynvml
        pynvml.nvmlInit()
        pynvml.nvmlDeviceGetHandleByIndex(0)
        pynvml.nvmlShutdown()
        from darwin_perf import _nvidia
        return _nvidia, "nvidia_pynvml"
    except ImportError:
        pass
    except Exception:
        pass

    # AMD — try pyamdgpuinfo or amdsmi
    try:
        import pyamdgpuinfo
        if pyamdgpuinfo.detect_gpus() > 0:
            from darwin_perf import _amd
            return _amd, "amd_pyamdgpuinfo"
    except ImportError:
        pass
    except Exception:
        pass

    try:
        import amdsmi
        amdsmi.amdsmi_init()
        handles = amdsmi.amdsmi_get_processor_handles()
        if handles:
            from darwin_perf import _amd
            return _amd, "amd_amdsmi"
    except ImportError:
        pass
    except Exception:
        pass

    # CPU-only fallback
    from darwin_perf import _fallback
    return _fallback, "cpu_fallback"


def get_backend() -> tuple[types.ModuleType, str]:
    """Get the active backend (cached after first call)."""
    global _backend, _backend_name
    if _backend is None:
        _backend, _backend_name = _detect_backend()
        logger.info("darwin-perf backend: %s", _backend_name)
    return _backend, _backend_name


def backend_name() -> str:
    """Return the name of the active backend."""
    _, name = get_backend()
    return name
