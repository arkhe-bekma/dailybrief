# Deploying dailybrief on AWS Lightsail

End-to-end recipe to take this repo from "runs on my Mac" to
"`https://your.domain.com`". Tested on Ubuntu 22.04 LTS / Lightsail.

Monthly cost: **~$5** (Lightsail $5 instance) + domain registration.

## 1. Spin up a Lightsail instance

1. Open <https://lightsail.aws.amazon.com/>.
2. **Create instance** → Linux/Unix → **OS Only → Ubuntu 22.04 LTS**.
3. Plan: **$5 / month** (1 vCPU, 1 GB RAM, 40 GB SSD).
4. Name it `dailybrief`. Hit **Create**.
5. Wait ~30s for it to go *Running*.
6. Click the instance → **Networking** → **Create static IP**, attach
   to the instance. Note the IP — you'll point your domain at it.
7. **Networking → IPv4 Firewall**:
   - leave **22 / 80** (already open)
   - **add 443** (HTTPS)

## 2. Point your domain at the IP

Go to your domain registrar (Namecheap / Cloudflare / wherever) and
add an **A record**:

```
type: A
host: @  (or `news`, whatever subdomain you want)
value: <static IP from step 1>
TTL:  300
```

Wait 1–10 min for DNS to propagate. Test with:

```
dig +short your.domain.com
```

## 3. Push the code to GitHub

Run on **your Mac**, once:

```bash
gh auth login            # browser flow, paste the device code
cd ~/Projects/dailybrief
gh repo create dailybrief --private --source=. --remote=origin --push
```

## 4. SSH into Lightsail and install

Lightsail's web console has a **Connect using SSH** button — easiest. Or
download the default key from *Account → SSH keys* and:

```bash
ssh -i LightsailDefaultKey.pem ubuntu@<your-IP>
```

Then on the box:

```bash
# Clone (use HTTPS — read-only is fine, app pulls no secrets)
git clone https://github.com/<you>/dailybrief.git ~/dailybrief
cd ~/dailybrief

# One-shot install: apt deps + caddy + venv + systemd + TLS
DAILYBRIEF_DOMAIN=your.domain.com bash scripts/deploy.sh
```

The script:
- installs Python, git, **Caddy**
- creates `.venv`, installs `requirements.txt`
- registers `dailybrief.service` with systemd and starts it
- writes `/etc/caddy/Caddyfile` pointed at your domain
- reloads Caddy — Let's Encrypt issues a TLS cert within ~30s

When it's done:

```
✓ Open: https://your.domain.com
```

## 5. (Optional) drop in your Anthropic key

```bash
nano ~/dailybrief/.env
# set ANTHROPIC_API_KEY=sk-ant-...
sudo systemctl restart dailybrief
```

## Edge config (Caddy)

The live Caddy config is checked in at `scripts/Caddyfile.production` —
it is the file that runs at `/etc/caddy/Caddyfile`. Edit it in the repo,
then apply:

```bash
sudo cp scripts/Caddyfile.production /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```

`scripts/Caddyfile` is still the generic `__DOMAIN__` template that
`deploy.sh` uses on a fresh box.

**`/pro` is currently closed** — both `/pro` and `/pro/*` 302 to `/`. To
re-open, uncomment the `handle_path /pro/*` block in
`scripts/Caddyfile.production` and drop the two `@pro` redirect lines.
The static build lives in `frontend_pro/` and on the box at
`/srv/dailybrief_pro`.

## Auto-deploy (optional, recommended)

To have the Lightsail box pull every new commit on `main` automatically:

Install it in **ubuntu's** crontab, not root's — the checkout is owned
by `ubuntu`, and running git as root leaves root-owned objects in
`.git` that `ubuntu` can then no longer write to. The script uses
`sudo systemctl` for the restart, which `ubuntu` can do passwordlessly.

```bash
# On the Lightsail box, once:
chmod +x ~/dailybrief/scripts/auto-update.sh
sudo touch /var/log/dailybrief-deploy.log
sudo chown ubuntu:ubuntu /var/log/dailybrief-deploy.log
{ crontab -l 2>/dev/null | grep -v dailybrief || true; \
  echo "* * * * * /home/ubuntu/dailybrief/scripts/auto-update.sh \
>> /var/log/dailybrief-deploy.log 2>&1"; } | crontab -
```

Every minute, root checks `origin/main` for a new SHA. If there's one,
it syncs + restarts the service. No restart unless the SHA actually
changed, so there's no needless downtime.

**Never edit files directly on the box.** The box drifted out of git
once because someone did, and the old `git pull --rebase` then refused
to run — deploys silently froze for weeks while the repo and production
diverged in both directions. `auto-update.sh` now stashes any local
edits into a timestamped stash and hard-syncs to `origin/main`, so a
stray edit can't block a deploy again. Recover one with `git stash list`
/ `git stash show -p`.

Watch deploys land:

```bash
sudo tail -f /var/log/dailybrief-deploy.log
```

## Capacity

Measured on the live box (416 MB RAM, 2 vCPU, one uvicorn worker) on
2026-08-23, simulating readers paced the way a real session behaves: a
page load is 3 requests that reach Python, an idle reader costs
essentially nothing, and opening an article is one more request.

| concurrent readers | req/s | errors | p50 | p95 | peak load |
|---|---|---|---|---|---|
| 500  | 38 | 0        | 89 ms  | 3.4 s  | 0.79 / 2 cpu |
| 1000 | 61 | 16% (timeouts) | 659 ms | 21 s | 2.60 / 2 cpu |

**500 concurrent readers is comfortable. 1000 is past the edge.** At
1000 the two CPUs saturate and Caddy starts timing out upstream; the app
never crashes and recovers on its own the moment load drops.

Raw throughput ceiling is ~250 req/s. The gap between that and 61 req/s
at the breaking point is the *arrival burst* — 1000 readers landing
inside 12 seconds is ~250 req/s of page loads on its own.

If more headroom is needed, in order:

1. **Bigger instance.** 416 MB is the real limit and the cheapest thing
   to change. Nothing below is worth doing first.
2. **A second uvicorn worker** — but only *after* more RAM. The cache is
   per-process, so two workers means two copies of it, half the hit
   rate, and `brief:response` invalidation that no longer reaches both.
   That trade is bad at 416 MB and fine at 1 GB+.
3. **Move the cache to Redis** if it ever goes multi-worker for real.

Background workers (ranker, RSS ingest, body sweep) share those two
CPUs. The ranker alone is ~4 s of CPU over 6,000 rows every 20 minutes,
and it shows up as a p95 spike under load. On a busier box they should
move off the request path.

Re-run the load test with `scratchpad/realistic.py`-style pacing, not a
flat hammer — a synthetic hammer measures a number nobody experiences.

## Checkpoints

Tag a verified-good state so there is always something known-good to roll
back to:

```bash
git tag -a checkpoint-$(date +%Y%m%d)-slug -m "what was verified"
git push origin checkpoint-$(date +%Y%m%d)-slug
```

Roll back with `git reset --hard <tag> && git push --force-with-lease`;
the box syncs within a minute. Tag only states you have actually
verified against the live site — a checkpoint nobody trusts is worse
than none.

The old `checkpoints/` directory of hand-copied snapshots on the server
is obsolete and gitignored; it predates the box being a git checkout.

## Day-2 ops

| Need | Command |
|---|---|
| Is the app healthy? | open `https://your.domain.com/status` |
| Tail app logs | `sudo journalctl -u dailybrief -f` |
| Tail proxy/TLS logs | `sudo journalctl -u caddy -f` |
| Restart app | `sudo systemctl restart dailybrief` |
| Pull a new version | `cd ~/dailybrief && git pull && sudo systemctl restart dailybrief` |
| Edit secrets | `nano ~/dailybrief/.env && sudo systemctl restart dailybrief` |
| Bigger box | Lightsail → instance → *Stop* → *Change plan* → *Start* |

## Files this deploy depends on

- `scripts/deploy.sh` — the installer above
- `scripts/dailybrief.service` — systemd unit (App runs as `ubuntu` on
  127.0.0.1:8000, Caddy proxies 443→8000)
- `scripts/Caddyfile` — Caddy v2 config; `__DOMAIN__` placeholder is
  swapped at install time
- `.env.example` — `PORT`, `HOST`, `ANTHROPIC_API_KEY`, etc.

If you ever want to migrate off Lightsail, everything above also works
unchanged on an EC2 / DigitalOcean / Hetzner box.
