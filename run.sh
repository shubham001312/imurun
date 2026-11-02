#!/usr/bin/env bash
# IMURUN launcher for Linux/macOS: sets everything up on first run, then starts.
# Usage: ./run.sh [start.py options, e.g. --browser --port 8800]
set -euo pipefail
cd "$(dirname "$0")"

VPY=".venv/bin/python"

find_python() {
    for cand in python3.12 python3.13 python3.11 python3.14 python3 python; do
        if command -v "$cand" >/dev/null 2>&1 &&
           "$cand" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)' >/dev/null 2>&1; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}

confirm() {
    [ -t 0 ] || return 0
    read -r -p "$1 [Y/n] " reply
    case "${reply:-y}" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

install_python() {
    if [ "$(uname)" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1; then
            confirm "Python 3.11+ was not found. Install Python 3.12 with Homebrew?" && brew install python@3.12
        else
            echo "Install Python 3.12 from https://www.python.org/downloads/macos/ (or install Homebrew first)."
        fi
    elif command -v apt-get >/dev/null 2>&1; then
        confirm "Python 3.11+ was not found. Install it with apt (needs sudo)?" &&
            sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
    elif command -v dnf >/dev/null 2>&1; then
        confirm "Python 3.11+ was not found. Install it with dnf (needs sudo)?" &&
            sudo dnf install -y python3 python3-pip
    elif command -v pacman >/dev/null 2>&1; then
        confirm "Python 3.11+ was not found. Install it with pacman (needs sudo)?" &&
            sudo pacman -S --needed --noconfirm python python-pip
    else
        echo "Install Python 3.11+ with your package manager, then run ./run.sh again."
    fi
}

if ! "$VPY" -c "import aiohttp, httpx, huggingface_hub, yaml" >/dev/null 2>&1; then
    echo "Setting up IMURUN (first run, or the .venv is missing/broken)..."
    PY="$(find_python || true)"
    if [ -z "$PY" ]; then
        install_python || true
        hash -r
        PY="$(find_python || true)"
    fi
    if [ -z "$PY" ]; then
        echo "Python 3.11+ is required. See docs/setup/README.md"
        exit 1
    fi
    "$PY" install.py
fi

exec "$VPY" start.py "$@"
