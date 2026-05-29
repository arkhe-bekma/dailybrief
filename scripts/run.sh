#!/usr/bin/env bash
# Start dailybrief in dev mode.
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

export PYTHONPATH="$(pwd)"
echo "→ http://localhost:8000"
exec uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
