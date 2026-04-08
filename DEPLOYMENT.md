# Deployment Guide — Kalshi VPS

## Server

- **Host:** root@5.161.111.138
- **SSH key:** ~/.ssh/hetzner
- **Project path:** /opt/kalshi
- **OS:** Ubuntu 24.04

## Initial Setup

```bash
# SSH in
ssh -i ~/.ssh/hetzner root@5.161.111.138

# Clone repo (requires GitHub SSH key on VPS)
git clone git@github.com:wmjs/kalshi.git /opt/kalshi
cd /opt/kalshi

# Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create required directories (gitignored)
mkdir -p data/raw logs

# Create .env from template
cp .env.example .env   # then fill in credentials
```

## Environment Variables (`.env`)

```bash
# Kalshi API
KALSHI_KEY_ID=your_key_id_here
KALSHI_PRIVATE_KEY_PATH=/opt/kalshi/kalshi.pem

# Twilio SMS alerts
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_FROM=+1xxxxxxxxxx
TWILIO_TO=+1xxxxxxxxxx
```

The `.pem` private key file must be placed at the path specified by `KALSHI_PRIVATE_KEY_PATH`.

## Services

Both processes run as systemd services and restart automatically on failure.

```bash
# Enable and start both services (do this once on initial deploy)
systemctl enable kalshi-engine kalshi-bot
systemctl start kalshi-engine kalshi-bot

# Check status
systemctl status kalshi-engine kalshi-bot --no-pager

# View live logs
journalctl -u kalshi-engine -f
journalctl -u kalshi-bot -f
```

Service files:
- `/etc/systemd/system/kalshi-engine.service` — live trading engine
- `/etc/systemd/system/kalshi-bot.service` — Telegram command bot

## Code Updates (local → VPS)

```bash
# Local machine: push changes
git push

# VPS: pull changes
ssh -i ~/.ssh/hetzner root@5.161.111.138 'cd /opt/kalshi && git pull'
```

## Log Locations

| File | Contents |
|------|----------|
| `logs/engine.log` | Raw engine stdout/stderr (via systemd) |
| `logs/live_YYYYMMDD.jsonl` | Structured trade events (one JSON line per event) |
| `logs/bot.log` | Telegram bot stdout/stderr |

## Checking Status

```bash
# From local machine
ssh -i ~/.ssh/hetzner root@5.161.111.138 'cd /opt/kalshi && .venv/bin/python3 scripts/status.py'

# Check today's trade log
ssh -i ~/.ssh/hetzner root@5.161.111.138 'tail -f /opt/kalshi/logs/live_$(date -u +%Y%m%d).jsonl'

# View P&L report
ssh -i ~/.ssh/hetzner root@5.161.111.138 'cd /opt/kalshi && .venv/bin/python3 scripts/trade_report.py'
```

Or send `status` to the Telegram bot.

## Manual Engine Run

```bash
# Dry run (no orders placed)
.venv/bin/python3 scripts/live_engine.py --dry-run

# Restart via systemd (preferred — handles reconcile automatically)
systemctl restart kalshi-engine
```

## Troubleshooting

**Engine not running:**
```bash
systemctl status kalshi-engine
journalctl -u kalshi-engine --since "1 hour ago"
```

**Orphaned orders (prior crashed session):**
The engine runs `reconcile()` on startup: it loads any open positions into the risk manager and cancels resting orders for tickers not in today's active setups (while preserving orders on any ticker where a position is open).

**DuckDB rebuild:**
```bash
.venv/bin/python3 scripts/build_db.py
```

**Full data re-pull (last 7 days):**
```bash
.venv/bin/python3 scripts/daily_refresh.py --days 7
```
