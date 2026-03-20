#!/bin/bash
# darwin-perf installer — one-line install for macOS
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/adammikulis/darwin-perf/main/scripts/install.sh | bash
#
# What it does:
#   1. Installs darwin-perf via pip (builds the C extension)
#   2. Optionally installs extras (TUI, GUI, menu bar)
#   3. Optionally sets up IDS daemon (launchd)

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}darwin-perf installer${NC}"
echo "System performance monitoring + IDS for macOS Apple Silicon"
echo ""

# Check macOS
if [[ "$(uname)" != "Darwin" ]]; then
    echo -e "${RED}Error: darwin-perf only runs on macOS${NC}"
    exit 1
fi

# Check architecture
ARCH=$(uname -m)
if [[ "$ARCH" != "arm64" ]]; then
    echo -e "${YELLOW}Warning: darwin-perf is optimized for Apple Silicon (arm64)${NC}"
    echo "Your architecture: $ARCH"
fi

# Find Python
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [[ "$major" -ge 3 ]] && [[ "$minor" -ge 9 ]]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo -e "${RED}Error: Python 3.9+ required. Install via:${NC}"
    echo "  brew install python@3.12"
    exit 1
fi

echo -e "Using: ${GREEN}$($PYTHON --version)${NC}"
echo ""

# Install
echo -e "${CYAN}Installing darwin-perf...${NC}"
$PYTHON -m pip install darwin-perf 2>&1 | tail -3

# Check if it works
if ! $PYTHON -c "import darwin_perf; print(f'v{darwin_perf.__version__}')" 2>/dev/null; then
    echo -e "${RED}Installation failed. Try:${NC}"
    echo "  pip install --no-cache-dir darwin-perf"
    exit 1
fi

VERSION=$($PYTHON -c "import darwin_perf; print(darwin_perf.__version__)")
echo -e "${GREEN}darwin-perf v${VERSION} installed successfully!${NC}"
echo ""

# Optional extras
echo "Optional extras:"
echo "  [1] TUI mode (rich terminal UI with sparklines)"
echo "  [2] GUI mode (native floating window)"
echo "  [3] Menu bar app (persistent status bar monitoring)"
echo "  [4] All extras"
echo "  [5] Skip"
echo ""
read -rp "Install extras? [1-5, default=5]: " EXTRAS
EXTRAS=${EXTRAS:-5}

case "$EXTRAS" in
    1) $PYTHON -m pip install "darwin-perf[tui]" -q ;;
    2) $PYTHON -m pip install "darwin-perf[gui]" -q ;;
    3) $PYTHON -m pip install pyobjc-framework-Cocoa -q ;;
    4) $PYTHON -m pip install "darwin-perf[all]" pyobjc-framework-Cocoa -q ;;
    *) ;;
esac

# Offer IDS daemon setup
echo ""
read -rp "Set up IDS daemon (runs in background, starts on login)? [y/N]: " SETUP_IDS
if [[ "${SETUP_IDS,,}" == "y" ]]; then
    darwin-perf --ids-install 2>/dev/null || echo -e "${YELLOW}Run 'darwin-perf --ids-install' after v1.0 to set up daemon${NC}"
fi

echo ""
echo -e "${GREEN}Done!${NC} Quick start:"
echo ""
echo "  darwin-perf              # live GPU/CPU monitor"
echo "  darwin-perf --tui        # rich terminal UI"
echo "  darwin-perf --gui        # floating window"
echo "  darwin-perf --menubar    # menu bar app"
echo "  darwin-perf --net        # network monitor"
echo "  darwin-perf --ids        # intrusion detection"
echo "  darwin-perf --help       # all options"
echo ""
echo "Python API:"
echo "  import darwin_perf as dp"
echo "  s = dp.stats()"
echo "  print(f\"GPU: {s['gpu_util_pct']}%\")"
