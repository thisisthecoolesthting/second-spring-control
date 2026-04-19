#!/usr/bin/env bash
# Second Spring Control launcher (macOS / Linux)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if [ ! -f .deps_installed ]; then
    echo "Installing dependencies..."
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
    touch .deps_installed
fi

if [ ! -f .env ]; then
    echo "No .env found. Copying .env.example. Edit before continuing."
    cp .env.example .env
    "${EDITOR:-nano}" .env
fi

# Open browser after a short delay (mac/linux variants).
( sleep 3 && (xdg-open http://localhost:8080 2>/dev/null || open http://localhost:8080 2>/dev/null) ) &

echo
echo "===================================================="
echo "  Second Spring Control: http://localhost:8080"
echo "  Press Ctrl+C to stop."
echo "===================================================="
echo

python -m uvicorn control.main:app --host 127.0.0.1 --port 8080
