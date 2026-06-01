#!/usr/bin/env bash
# Cron-driven auto-deploy.
#
# Runs every minute via root crontab on the Lightsail box. If origin/main
# has a new commit, pulls and restarts the service. If nothing changed,
# does nothing (so no needless restart downtime).
#
# Install: see DEPLOY.md "Auto-deploy" section.

set -e
cd "$(dirname "$0")/.."

git fetch -q origin main || exit 0
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
  git pull -q --rebase
  # Pick up any new Python deps (e.g. aiosqlite, trafilatura updates).
  if [ -f /home/ubuntu/dailybrief/.venv/bin/pip ]; then
    /home/ubuntu/dailybrief/.venv/bin/pip install -q -r /home/ubuntu/dailybrief/requirements.txt \
      >> /var/log/dailybrief-deploy.log 2>&1 || true
  fi
  systemctl restart dailybrief
  echo "[auto-update] $(date -u +%FT%TZ) pulled $LOCAL → $REMOTE, restarted"
fi
