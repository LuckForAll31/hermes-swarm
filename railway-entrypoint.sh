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

# ── Auto-configure LLM provider on first run ────────────────────
# Uses SWARM_LLM_* env vars to call set-model non-interactively.
# Idempotent: skips if a model is already configured.
HERMES_CONFIG="${SWARM_DATA_DIR:-/data}/.hermes-shared"
if [ -n "${SWARM_LLM_API_KEY:-}" ] && [ -n "${SWARM_LLM_MODEL:-}" ]; then
    # Check if model already configured (doctor returns non-zero if not)
    if ! hermes-swarm doctor 2>/dev/null | grep -q "✓ Model:"; then
        echo "● Configuring LLM provider: ${SWARM_LLM_PROVIDER:-custom} / ${SWARM_LLM_MODEL}"
        hermes-swarm set-model \
            --provider "${SWARM_LLM_PROVIDER:-custom}" \
            --model "${SWARM_LLM_MODEL}" \
            ${SWARM_LLM_BASE_URL:+--base-url "${SWARM_LLM_BASE_URL}"} \
            --api-key "${SWARM_LLM_API_KEY}" || true
        echo "● LLM provider configured"
    else
        echo "● LLM provider already configured — skipping"
    fi
else
    echo "⚠ SWARM_LLM_API_KEY not set — run 'hermes-swarm setup' via Railway shell to configure a provider"
fi

# ── Auto-init teams on first run ────────────────────────────────
# Idempotent: skips already-existing teams.
CONFIG_FILE="${SWARM_DATA_DIR:-/data}/agents.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "● First run detected — scaffolding teams..."

    # ── KairHub (entreprise) ────────────────────────────────────
    echo "  → Creating team: kairhub"
    hermes-swarm init --team kairhub --team-name "KairHub" \
        --agent coordinator --agent-name "KairHub Coordinator" || true

    # ── MoodPlanner / LudiCare ──────────────────────────────────
    echo "  → Creating team: moodplanner"
    hermes-swarm init --team moodplanner --team-name "MoodPlanner / LudiCare" \
        --agent coordinator --agent-name "Mood Coordinator" || true

    # ── Cuisko ──────────────────────────────────────────────────
    echo "  → Creating team: cuisko"
    hermes-swarm init --team cuisko --team-name "Cuisko" \
        --agent coordinator --agent-name "Cuisko Coordinator" || true

    echo "● Teams created: kairhub, moodplanner, cuisko"
fi

# ── Start the swarm server ──────────────────────────────────────
echo "● Launching swarm server..."
exec hermes-swarm up
