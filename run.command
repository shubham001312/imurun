#!/usr/bin/env bash
# IMURUN launcher for macOS Finder (double-click). Delegates to run.sh.
# If macOS refuses to open it: chmod +x run.command run.sh
cd "$(dirname "$0")"
exec bash ./run.sh "$@"
