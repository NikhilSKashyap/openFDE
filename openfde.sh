#!/usr/bin/env bash
# Thin development wrapper — avoids needing `pip install -e .` on system Python.
# Usage:
#   ./openfde.sh watch /path/to/repo
#   ./openfde.sh watch /path/to/repo --port 8080 --no-open
#
# For a proper CLI install (recommended for daily use):
#   python3 -m venv .venv && source .venv/bin/activate && pip install -e .
#   openfde watch /path/to/repo
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
exec python3 -m openfde "$@"
