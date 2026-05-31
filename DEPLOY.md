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

## Day-2 ops

| Need | Command |
|---|---|
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
