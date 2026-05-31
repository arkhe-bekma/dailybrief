#!/usr/bin/env bash
# One-shot install for a fresh Ubuntu 22.04 Lightsail box.
#
# Usage (on the Lightsail box, as user `ubuntu`):
#   git clone <repo> ~/dailybrief
#   cd ~/dailybrief
#   DAILYBRIEF_DOMAIN=your.domain.com bash scripts/deploy.sh
#
# What it does:
#   1. apt deps (python, git, caddy)
#   2. venv + pip install
#   3. .env from .env.example (you'll edit ANTHROPIC_API_KEY after)
#   4. systemd unit → enable + start
#   5. Caddyfile → /etc/caddy/Caddyfile with your domain → reload Caddy
#
# Idempotent — safe to re-run.

set -euo pipefail
APP_DIR="${APP_DIR:-$HOME/dailybrief}"
DOMAIN="${DAILYBRIEF_DOMAIN:-}"

cd "$APP_DIR"

if [ -z "$DOMAIN" ]; then
  echo "ERROR: set DAILYBRIEF_DOMAIN=your.domain.com before running this script" >&2
  exit 1
fi

# ── 1. apt deps ───────────────────────────────────────────────
if ! command -v caddy >/dev/null 2>&1; then
  echo "→ installing apt packages + caddy"
  sudo apt-get update -y
  sudo apt-get install -y python3-venv python3-pip git curl debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt-get update -y
  sudo apt-get install -y caddy
fi

# ── 2. venv + python deps ─────────────────────────────────────
if [ ! -d .venv ]; then
  echo "→ creating .venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

# ── 3. .env ────────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  echo "→ wrote .env — remember to drop your ANTHROPIC_API_KEY in (optional)"
fi

# ── 4. systemd unit ───────────────────────────────────────────
echo "→ installing systemd unit"
sudo cp scripts/dailybrief.service /etc/systemd/system/dailybrief.service
sudo systemctl daemon-reload
sudo systemctl enable dailybrief
sudo systemctl restart dailybrief

# ── 5. Caddy + TLS ────────────────────────────────────────────
echo "→ wiring Caddy for ${DOMAIN}"
sudo mkdir -p /var/log/caddy
sed "s/__DOMAIN__/${DOMAIN}/g" scripts/Caddyfile | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl reload caddy

echo
echo "✓ dailybrief is up. Logs:"
echo "    sudo journalctl -u dailybrief -f      # app"
echo "    sudo journalctl -u caddy -f           # proxy / TLS"
echo
echo "✓ Open: https://${DOMAIN}"
