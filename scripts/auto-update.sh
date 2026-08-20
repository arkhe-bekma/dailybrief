#!/usr/bin/env bash
# Cron-driven auto-deploy.
#
# Runs every minute via root crontab on the Lightsail box. If origin/main
# has a new commit, syncs to it and restarts the service. If nothing
# changed, does nothing (so no needless restart downtime).
#
# Install: see DEPLOY.md "Auto-deploy" section.

set -e
cd "$(dirname "$0")/.."

APP_DIR="$(pwd)"
LOG_TAG="[auto-update] $(date -u +%FT%TZ)"

git fetch -q origin main || exit 0
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

[ "$LOCAL" = "$REMOTE" ] && exit 0

# The box drifted from git once already because someone edited files in
# place and the old `git pull --rebase` then refused to run, silently
# freezing deploys for weeks. Stash anything local into a timestamped
# branch (never discarded, never in the way) and hard-sync instead, so a
# stray on-server edit can't block a deploy again.
if ! git diff --quiet || ! git diff --cached --quiet; then
  SNAP="server-edits-$(date -u +%Y%m%d-%H%M%S)"
  git stash push -q -u -m "$SNAP" || true
  echo "$LOG_TAG stashed local server edits as '$SNAP' (git stash list)"
fi

git reset -q --hard "$REMOTE"

# Pick up any new Python deps (e.g. aiosqlite, trafilatura updates).
if [ -x "$APP_DIR/.venv/bin/pip" ]; then
  "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" || true
fi

systemctl restart dailybrief

# Confirm it actually came back up; a deploy that leaves the service dead
# should be loud in the log, not silent.
sleep 5
if systemctl is-active --quiet dailybrief; then
  echo "$LOG_TAG deployed $LOCAL -> $REMOTE, service active"
else
  echo "$LOG_TAG DEPLOY FAILED: dailybrief is not active after restart"
  systemctl status dailybrief --no-pager -n 20 || true
fi
