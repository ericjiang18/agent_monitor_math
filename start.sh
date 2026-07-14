#!/usr/bin/env bash
# Start the Agent Monitor proving console.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "No .venv found — run ./setup.sh first." >&2; exit 1
fi
exec ./.venv/bin/python -m agent_monitor.console_server "$@"
