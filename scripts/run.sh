#!/usr/bin/env bash
# Start dailybrief in dev mode. Reads PORT/HOST from .env if present.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "→ creating .venv"
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "→ wrote .env (edit it to add ANTHROPIC_API_KEY for LLM curation)"
fi

# shellcheck disable=SC1091
set -a; source .env; set +a
: "${PORT:=8000}"
: "${HOST:=0.0.0.0}"

export PYTHONPATH="$(pwd)"
echo "→ http://localhost:${PORT}"
exec uvicorn backend.main:app --reload --host "${HOST}" --port "${PORT}"
