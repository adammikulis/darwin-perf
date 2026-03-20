"""Cross-platform CPU and memory stats without psutil.

Linux: reads /proc/stat, /proc/meminfo, /proc/{pid}/stat directly.
Windows: uses ctypes kernel32/ntdll calls.
macOS: uses os.sysconf (native C extension handles the rest).
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any


def system_memory() -> dict[str, Any]:
    """System memory in bytes: total, used, available, compressed."""
    system = platform.system()

    if system == "Linux":
        return _linux_meminfo()
    if system == "Windows":
        return _windows_memory()
    # macOS fallback (normally handled by native)
    return _posix_memory()


def cpu_ticks() -> dict[str, Any]:
    """CPU ticks: user, system, idle + per-core."""
    system = platform.system()

    if system == "Linux":
        return _linux_cpu_ticks()
    if system == "Windows":
        return _windows_cpu_ticks()
    return {}


def process_info(pid: int) -> dict[str, Any] | None:
    """Per-process memory, threads, CPU time."""
    system = platform.system()

    if system == "Linux":
        return _linux_proc_info(pid)
    if system == "Windows":
        return _windows_proc_info(pid)
    return None


def parent_pid(pid: int) -> int:
    """Get parent PID."""
    system = platform.system()

    if system == "Linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            fields = stat.split()
            return int(fields[3])  # ppid is field 4
        except Exception:
            return 0
    if system == "Windows":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return 0
            try:
                # Use NtQueryInformationProcess
                import ctypes as ct
                class PROCESS_BASIC_INFORMATION(ct.Structure):
                    _fields_ = [
                        ("Reserved1", ct.c_void_p),
                        ("PebBaseAddress", ct.c_void_p),
                        ("Reserved2", ct.c_void_p * 2),
                        ("UniqueProcessId", ct.POINTER(ct.c_ulong)),
                        ("InheritedFromUniqueProcessId", ct.POINTER(ct.c_ulong)),
                    ]
                pbi = PROCESS_BASIC_INFORMATION()
                ntdll = ct.windll.ntdll
                ntdll.NtQueryInformationProcess(handle, 0, ct.byref(pbi), ct.sizeof(pbi), None)
                return pbi.InheritedFromUniqueProcessId or 0
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return 0
    return 0


# ---------------------------------------------------------------------------
# Linux: /proc filesystem
# ---------------------------------------------------------------------------

def _linux_meminfo() -> dict[str, Any]:
    """Parse /proc/meminfo."""
    try:
        text = Path("/proc/meminfo").read_text()
        info: dict[str, int] = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                key = parts[0].rstrip(":")
                val = int(parts[1]) * 1024  # kB to bytes
                info[key] = val
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", 0)
        return {
            "memory_total": total,
            "memory_used": total - available,
            "memory_available": available,
            "memory_compressed": info.get("SwapCached", 0),
        }
    except Exception:
        return {}


def _linux_cpu_ticks() -> dict[str, Any]:
    """Parse /proc/stat for CPU ticks."""
    try:
        text = Path("/proc/stat").read_text()
        result: dict[str, Any] = {}
        per_core: list[dict[str, Any]] = []
        for line in text.splitlines():
            if line.startswith("cpu "):
                fields = line.split()
                result["cpu_ticks_user"] = int(fields[1])
                result["cpu_ticks_system"] = int(fields[3])
                result["cpu_ticks_idle"] = int(fields[4])
            elif line.startswith("cpu"):
                fields = line.split()
                core_num = int(fields[0][3:])
                per_core.append({
                    "core": core_num,
                    "ticks_user": int(fields[1]),
                    "ticks_system": int(fields[3]),
                    "ticks_idle": int(fields[4]),
                })
        result["per_core"] = per_core
        result["cpu_count"] = len(per_core)
        try:
            result["cpu_name"] = platform.processor() or ""
        except Exception:
            result["cpu_name"] = ""
        return result
    except Exception:
        return {}


def _linux_proc_info(pid: int) -> dict[str, Any] | None:
    """Read /proc/{pid}/stat and /proc/{pid}/statm."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat.split()
        # Extract name from between parens
        name_start = stat.index("(") + 1
        name_end = stat.index(")")
        name = stat[name_start:name_end]
        # Fields after the closing paren
        rest = stat[name_end + 2:].split()

        utime = int(rest[11])  # field 14 (0-indexed from after name)
        stime = int(rest[12])  # field 15
        threads = int(rest[17])  # field 20

        # Memory from statm
        page_size = os.sysconf("SC_PAGE_SIZE")
        statm = Path(f"/proc/{pid}/statm").read_text().split()
        rss_pages = int(statm[1])
        rss_bytes = rss_pages * page_size

        # IO stats
        read_bytes = 0
        write_bytes = 0
        try:
            io_text = Path(f"/proc/{pid}/io").read_text()
            for line in io_text.splitlines():
                if line.startswith("read_bytes:"):
                    read_bytes = int(line.split()[1])
                elif line.startswith("write_bytes:"):
                    write_bytes = int(line.split()[1])
        except (PermissionError, FileNotFoundError):
            pass

        CLK_TCK = os.sysconf("SC_CLK_TCK")
        cpu_ns = int((utime + stime) / CLK_TCK * 1e9)

        return {
            "pid": pid,
            "name": name,
            "memory": rss_bytes,
            "real_memory": rss_bytes,
            "peak_memory": rss_bytes,
            "threads": threads,
            "cpu_ns": cpu_ns,
            "energy_nj": 0,
            "wired_size": 0,
            "neural_footprint": 0,
            "disk_read_bytes": read_bytes,
            "disk_write_bytes": write_bytes,
            "instructions": 0,
            "cycles": 0,
            "idle_wakeups": 0,
            "pageins": 0,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Windows: ctypes kernel32
# ---------------------------------------------------------------------------

def _windows_memory() -> dict[str, Any]:
    """Windows memory via GlobalMemoryStatusEx."""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return {
            "memory_total": status.ullTotalPhys,
            "memory_used": status.ullTotalPhys - status.ullAvailPhys,
            "memory_available": status.ullAvailPhys,
            "memory_compressed": 0,
        }
    except Exception:
        return {}


def _windows_cpu_ticks() -> dict[str, Any]:
    """Windows CPU times via GetSystemTimes."""
    try:
        import ctypes
        from ctypes import wintypes

        idle = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        )

        def ft_to_ticks(ft: wintypes.FILETIME) -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        idle_t = ft_to_ticks(idle)
        kernel_t = ft_to_ticks(kernel)  # includes idle
        user_t = ft_to_ticks(user)
        system_t = kernel_t - idle_t

        return {
            "cpu_ticks_user": user_t,
            "cpu_ticks_system": system_t,
            "cpu_ticks_idle": idle_t,
            "cpu_count": os.cpu_count() or 1,
            "cpu_name": platform.processor() or "",
            "per_core": [],
        }
    except Exception:
        return {}


def _windows_proc_info(pid: int) -> dict[str, Any] | None:
    """Windows process info via OpenProcess + GetProcessMemoryInfo."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010

        handle = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
        )
        if not handle:
            return None
        try:
            # Memory info
            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(pmc), ctypes.sizeof(pmc)
            )

            # CPU times
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            )

            def ft_to_ns(ft: wintypes.FILETIME) -> int:
                return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) * 100

            cpu_ns = ft_to_ns(kernel_time) + ft_to_ns(user_time)

            return {
                "pid": pid,
                "name": "",
                "memory": pmc.WorkingSetSize,
                "real_memory": pmc.WorkingSetSize,
                "peak_memory": pmc.PeakWorkingSetSize,
                "threads": 0,
                "cpu_ns": cpu_ns,
                "energy_nj": 0,
                "wired_size": 0,
                "neural_footprint": 0,
                "disk_read_bytes": 0,
                "disk_write_bytes": 0,
                "instructions": 0,
                "cycles": 0,
                "idle_wakeups": 0,
                "pageins": pmc.PageFaultCount,
            }
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _posix_memory() -> dict[str, Any]:
    """POSIX memory via os.sysconf."""
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
        total = page_size * phys_pages
        return {
            "memory_total": total,
            "memory_used": 0,
            "memory_available": total,
            "memory_compressed": 0,
        }
    except Exception:
        return {}
