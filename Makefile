# darwin-perf build targets
.PHONY: install install-all app tui gui menubar ids test clean

# Install the library
install:
	pip install -e .

# Install with all extras
install-all:
	pip install -e ".[all]" pyobjc-framework-Cocoa

# Build standalone .app bundle (requires py2app)
app:
	pip install py2app
	python setup_app.py py2app
	@echo "App built: dist/darwin-perf.app"
	@echo "Drag to /Applications to install"

# Run modes
tui:
	darwin-perf --tui

gui:
	darwin-perf --gui

menubar:
	darwin-perf --menubar

ids:
	darwin-perf --ids

# Install IDS daemon
daemon-install:
	darwin-perf --ids-install

daemon-uninstall:
	darwin-perf --ids-uninstall

daemon-status:
	darwin-perf --ids-status

# Run tests
test:
	python -m pytest tests/ -v

# Clean build artifacts
clean:
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	find . -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
