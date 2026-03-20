"""darwin-perf menu bar app — persistent macOS status bar monitoring.

Shows GPU%, CPU%, and temperature in the menu bar. Click for a dropdown
with per-process GPU stats, IDS alerts, network connections, and system info.

Uses PyObjC (ships with macOS, also pip-installable) for native NSStatusBar.
No external GUI framework needed.

Usage:
    darwin-perf --menubar           # GPU% in menu bar, click for details
    darwin-perf --menubar --ids     # Include IDS monitoring
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from typing import Any


def _ensure_pyobjc():
    """Import PyObjC, auto-install if missing."""
    try:
        import AppKit  # noqa: F401
        import Foundation  # noqa: F401
        return True
    except ImportError:
        import subprocess
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pyobjc-framework-Cocoa", "-q"],
        )
        return True


def run_menubar(
    interval: float = 2.0,
    enable_ids: bool = False,
    show_dock: bool = False,
) -> None:
    """Launch the menu bar app.

    Args:
        interval: Update interval in seconds (default 2.0).
        enable_ids: Whether to run IDS monitoring in background.
        show_dock: Whether to show a Dock icon (default: menu bar only).
    """
    _ensure_pyobjc()

    import AppKit
    import Foundation

    from ._native import system_gpu_stats, system_stats, temperatures

    class StatusBarDelegate(AppKit.NSObject):
        """NSStatusBar delegate that manages the menu bar icon and dropdown."""

        statusItem = None
        timer = None
        ids_monitor = None
        _prev_snap = None
        _prev_cpu = None
        _prev_time = 0.0

        def applicationDidFinishLaunching_(self, notification):
            # Create status bar item
            self.statusItem = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
                AppKit.NSVariableStatusItemLength
            )
            self.statusItem.setHighlightMode_(True)

            # Initial menu
            self._build_menu([])

            # Start IDS if requested
            if enable_ids:
                try:
                    from ._ids import IDSMonitor
                    self.ids_monitor = IDSMonitor(
                        interval=interval,
                        enable_llm=False,
                    )
                    self.ids_monitor.start(source="menubar")
                except RuntimeError as e:
                    # Another IDS monitor is already running — just show its status
                    from ._lockfile import get_ids_lock_holder
                    holder = get_ids_lock_holder()
                    if holder:
                        print(f"IDS already running (PID {holder['pid']}, {holder['source']})")
                    self.ids_monitor = None
                except Exception:
                    self.ids_monitor = None

            # Start update timer
            self.timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                interval, self, "update:", None, True
            )
            # Fire immediately
            self.update_(None)

        def update_(self, timer):
            """Refresh menu bar title and menu contents."""
            try:
                gpu = system_gpu_stats()
                sys = system_stats()
                temps = temperatures()

                gpu_pct = gpu.get("device_utilization", 0)
                ram_used = sys.get("memory_used", 0) / (1024**3)
                ram_total = sys.get("memory_total", 0) / (1024**3)
                cpu_temp = temps.get("cpu_avg", 0)

                # Menu bar title
                title = f"GPU {gpu_pct}%  {cpu_temp:.0f}°C"
                self.statusItem.setTitle_(title)

                # Build dropdown with details
                procs = self._get_gpu_procs()
                self._build_menu(procs, gpu, sys, temps)

            except Exception as e:
                self.statusItem.setTitle_(f"dp: err")

        def _get_gpu_procs(self):
            """Get top GPU processes."""
            try:
                from . import _snapshot
                from ._native import cpu_time_ns, proc_info as _proc_info

                snap = _snapshot()
                procs = []
                for pid, info in snap.items():
                    pi = _proc_info(pid)
                    mem_mb = pi["real_memory"] / (1024 * 1024) if pi else 0
                    procs.append({
                        "pid": pid,
                        "name": info["name"],
                        "gpu_ns": info["gpu_ns"],
                        "mem_mb": round(mem_mb, 0),
                    })
                procs.sort(key=lambda p: p["gpu_ns"], reverse=True)
                return procs[:10]
            except Exception:
                return []

        def _build_menu(self, procs, gpu=None, sys_stats=None, temps=None):
            """Build the dropdown menu."""
            menu = AppKit.NSMenu.alloc().init()

            # Header
            header = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "darwin-perf", None, ""
            )
            header.setEnabled_(False)
            menu.addItem_(header)
            menu.addItem_(AppKit.NSMenuItem.separatorItem())

            # System stats
            if gpu:
                gpu_model = gpu.get("model", "?")
                gpu_cores = gpu.get("gpu_core_count", 0)
                gpu_pct = gpu.get("device_utilization", 0)
                vram = gpu.get("in_use_system_memory", 0) / (1024**3)
                self._add_item(menu, f"GPU: {gpu_model} ({gpu_cores} cores)")
                self._add_item(menu, f"  Utilization: {gpu_pct}%  VRAM: {vram:.1f} GB")

            if sys_stats:
                ram_used = sys_stats.get("memory_used", 0) / (1024**3)
                ram_total = sys_stats.get("memory_total", 0) / (1024**3)
                cpu_name = sys_stats.get("cpu_name", "?")
                self._add_item(menu, f"CPU: {cpu_name}")
                self._add_item(menu, f"  RAM: {ram_used:.0f}/{ram_total:.0f} GB")

            if temps:
                cpu_t = temps.get("cpu_avg", 0)
                gpu_t = temps.get("gpu_avg", 0)
                self._add_item(menu, f"  Temps: CPU {cpu_t:.0f}°C  GPU {gpu_t:.0f}°C")

            # Network
            try:
                from ._network import network_snapshot
                net = network_snapshot()
                self._add_item(menu, f"  Network: {len(net.connections)} connections, "
                               f"{len(net.listening_ports)} listening")
            except Exception:
                pass

            menu.addItem_(AppKit.NSMenuItem.separatorItem())

            # GPU processes
            if procs:
                self._add_item(menu, "Top GPU Processes:")
                for p in procs[:8]:
                    self._add_item(menu, f"  {p['pid']:>7}  {p['name']:<20}  {p['mem_mb']:.0f}MB")
            else:
                self._add_item(menu, "No GPU activity")

            # IDS status
            if self.ids_monitor:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                report = self.ids_monitor.report()
                n_alerts = report["total_alerts"]
                sev = report.get("severity_counts", {})
                high = sev.get("high", 0) + sev.get("critical", 0)
                self._add_item(menu, f"IDS: {n_alerts} alerts ({high} high/critical)")
                if report.get("baseline_warm"):
                    self._add_item(menu, f"  Baseline: warm ({report['baseline_samples']} samples)")
                else:
                    self._add_item(menu, f"  Baseline: building... ({report['baseline_samples']} samples)")

                # Recent alerts
                recent = report.get("recent_alerts", [])[-5:]
                if recent:
                    self._add_item(menu, "  Recent:")
                    for a in reversed(recent):
                        sev_str = a.get("severity", "?").upper()
                        desc = a.get("description", "?")[:60]
                        self._add_item(menu, f"    [{sev_str}] {desc}")

            # Footer
            menu.addItem_(AppKit.NSMenuItem.separatorItem())

            # Open TUI
            tui_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Open TUI...", "openTUI:", "t"
            )
            tui_item.setTarget_(self)
            menu.addItem_(tui_item)

            # Open GUI
            gui_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Open GUI Window...", "openGUI:", "g"
            )
            gui_item.setTarget_(self)
            menu.addItem_(gui_item)

            menu.addItem_(AppKit.NSMenuItem.separatorItem())

            quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Quit darwin-perf", "terminate:", "q"
            )
            menu.addItem_(quit_item)

            self.statusItem.setMenu_(menu)

        def _add_item(self, menu, title):
            """Add a disabled (info-only) menu item."""
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, None, ""
            )
            item.setEnabled_(False)
            # Use monospace font for alignment
            font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(11.0, 0.0)
            attrs = {AppKit.NSFontAttributeName: font}
            attributed = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                title, attrs
            )
            item.setAttributedTitle_(attributed)
            menu.addItem_(item)

        def openTUI_(self, sender):
            """Launch TUI in a new terminal window."""
            import subprocess
            subprocess.Popen([
                "osascript", "-e",
                'tell application "Terminal" to do script "darwin-perf --tui"'
            ])

        def openGUI_(self, sender):
            """Launch GUI window."""
            # Run in background thread since it blocks
            def _run():
                try:
                    from .gui import run_gui
                    run_gui()
                except Exception:
                    pass
            threading.Thread(target=_run, daemon=True).start()

        def applicationWillTerminate_(self, notification):
            if self.ids_monitor:
                self.ids_monitor.stop()

    # Run the app
    app = AppKit.NSApplication.sharedApplication()
    if show_dock:
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)  # dock icon
    else:
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no dock icon

    delegate = StatusBarDelegate.alloc().init()
    app.setDelegate_(delegate)

    # Handle Ctrl+C gracefully
    signal.signal(signal.SIGINT, lambda *_: app.terminate_(None))

    app.run()
