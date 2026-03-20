"""py2app setup script for building darwin-perf.app.

Usage:
    python setup_app.py py2app

Creates a standalone .app bundle in dist/ that can be dragged to Applications.
The app runs in the menu bar (no Dock icon).
"""

from setuptools import setup

APP = ["src/darwin_perf/_menubar.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/icon.icns" if __import__("os").path.exists("assets/icon.icns") else None,
    "plist": {
        "CFBundleName": "darwin-perf",
        "CFBundleDisplayName": "darwin-perf",
        "CFBundleIdentifier": "com.adammikulis.darwin-perf",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": True,  # no Dock icon — menu bar only
        "NSHighResolutionCapable": True,
        "CFBundleDocumentTypes": [],
    },
    "packages": ["darwin_perf"],
    "includes": [
        "darwin_perf._native",
        "darwin_perf._api",
        "darwin_perf._network",
        "darwin_perf._ids",
        "darwin_perf._ids_rules",
        "darwin_perf._ids_baseline",
        "darwin_perf._ids_llm",
        "darwin_perf._recorder",
        "darwin_perf._sampler",
        "darwin_perf.cli",
        "darwin_perf.tui",
        "darwin_perf.gui",
    ],
    "frameworks": [],  # IOKit and CoreFoundation linked by C extension
}

setup(
    app=APP,
    name="darwin-perf",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
