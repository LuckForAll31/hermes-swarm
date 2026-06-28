#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────
# Railway Entrypoint — Hermes Swarm
# ────────────────────────────────────────────────────────────────
# Railway assigns a dynamic PORT. This script makes the swarm
# listen on $PORT and handles first-run auto-init.
# ────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Railway dynamic port ────────────────────────────────────────
export SWARM_PORT="${PORT:-8000}"
export SWARM_HOST="0.0.0.0"

echo "● Hermes Swarm starting on 0.0.0.0:$SWARM_PORT"
echo "  DATA_DIR = ${SWARM_DATA_DIR:-/data}"

# ── Auto-init on first run ──────────────────────────────────────
# If no teams config exists, scaffold a default team so the
# dashboard has something to show immediately.
CONFIG_FILE="${SWARM_DATA_DIR:-/data}/agents.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "● First run detected — scaffolding default team..."
    hermes-swarm init --team default --team-name "Default Team" || true
fi

# ── Start the swarm server ──────────────────────────────────────
echo "● Launching swarm server..."
exec hermes-swarm up
