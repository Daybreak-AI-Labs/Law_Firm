#!/usr/bin/env bash
# PIA Concierge — STANDALONE agent launcher (sold without the platform).
#
# One process on :8890: the ticketing intake, requester inbox + chat/voice
# questionnaire, and the OneTrust tenant where a human reviews & approves.
# No dashboard, no world-model governance, no signed audit chain, no privacy
# workspace — those are Lightwork features. Imports nothing from maverick.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORLD_PORT="${WORLD_PORT:-8890}"

# The switch that forces the standalone build even where maverick is installed.
export PIA_STANDALONE=1

# Local data lives beside the demo (delete for a clean slate).
export PIA_DATA_DIR="${PIA_DATA_DIR:-$HERE/.standalone-data}"

# OneTrust target: the in-process mock tenant by default; repoint at a real
# OneTrust sandbox and the agent files there instead (same POST).
export ONETRUST_HOSTNAME="${ONETRUST_HOSTNAME:-http://127.0.0.1:$WORLD_PORT/ot-api}"
export ONETRUST_TOKEN="${ONETRUST_TOKEN:-demo-bearer-token}"
export PIA_BASE_URL="${PIA_BASE_URL:-http://127.0.0.1:$WORLD_PORT}"

# Email: the requester inbox is in-process; a standalone build delivers there.
export EMAIL_USER="${EMAIL_USER:-privacy-office@company.com}"
export EMAIL_SMTP_HOST="${EMAIL_SMTP_HOST:-127.0.0.1}"
export EMAIL_SMTP_PORT="${EMAIL_SMTP_PORT:-1025}"

echo "PIA_DATA_DIR         → $PIA_DATA_DIR"
export WORLD_PORT
cd "$HERE"
exec python3 serve_standalone.py
