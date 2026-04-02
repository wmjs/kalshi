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

## Cron Setup (UTC)

```bash
crontab -e
```

Add:
```cron
# Daily: refresh data + run live engine at 07:00 UTC
0 7 * * * cd /opt/kalshi && bash scripts/run_daily.sh
```

The script:
1. Pulls the last 2 days of settled market data and rebuilds DuckDB
2. Runs the live engine, restarting up to 3 times on crash
3. Sends an SMS alert if all attempts fail

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
| `logs/run_YYYYMMDD.log` | Full stdout/stderr for each daily run |
| `logs/live_YYYYMMDD.jsonl` | Structured trade events (one JSON line per event) |
| `logs/refresh_YYYYMMDD.jsonl` | Data refresh summaries |

## Checking Status

```bash
# From local machine
ssh -i ~/.ssh/hetzner root@5.161.111.138 'cd /opt/kalshi && source .venv/bin/activate && python3 scripts/status.py'

# Check today's trade log
ssh -i ~/.ssh/hetzner root@5.161.111.138 'tail -f /opt/kalshi/logs/live_$(date -u +%Y%m%d).jsonl'

# View P&L report
ssh -i ~/.ssh/hetzner root@5.161.111.138 'cd /opt/kalshi && source .venv/bin/activate && python3 scripts/trade_report.py'
```

## Manual Engine Run

```bash
# Dry run (no orders placed)
python3 scripts/live_engine.py --dry-run

# Live run
python3 scripts/live_engine.py
```

## Troubleshooting

**Engine crashed — check logs:**
```bash
cat logs/run_$(date -u +%Y%m%d).log
```

**Orphaned orders (prior crashed session):**
The engine runs `reconcile()` on startup: it loads any open positions into the risk manager and cancels resting orders for tickers not in today's active setups.

**DuckDB rebuild:**
```bash
python3 scripts/build_db.py
```

**Full data re-pull (last 7 days):**
```bash
python3 scripts/daily_refresh.py --days 7
```
