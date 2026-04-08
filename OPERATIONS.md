# Operations Guide — Kalshi Temperature Strategy

**Last updated:** 2026-04-08

---

## Overview

The strategy takes directional positions on daily temperature bracket markets across 5 cities (NY, LAX, MIA, PHI, CHI). It is **not** a market maker — it holds positions from a ~24h-before-close entry window until either a target price, stop loss, or settlement.

Edge source: at TTX > 24h, low-certainty bracket markets are mispriced relative to NOAA forecasts. Price tends to drift toward 0 or 100 as the outcome resolves. We buy depressed brackets that historical data says are likely to rally and either hit a target sell or settle at 100.

**Sizing:** 1 contract per market. Maximum 3–5 positions open at once.

---

## Architecture

```
Telegram Bot (kalshi-bot.service)
    ↕  commands / alerts
Live Engine (kalshi-engine.service)
    ↕  REST + WebSocket
Kalshi API
```

Both services are managed by systemd and restart automatically on failure.

- Engine logs (structured): `logs/live_YYYYMMDD.jsonl`
- Engine logs (raw stdout): `logs/engine.log`
- Bot logs: `logs/bot.log`

---

## Strategy Parameters

Configured in `strategies/temperature/config.py`. Per (series, season):

| Parameter | Description |
|---|---|
| `rank` | Which bracket to trade (1=coldest, 6=hottest) |
| `band_lo` / `band_hi` | Entry price range in cents. Only enter if opening price is in `[band_lo, band_hi)` or falls from above |
| `target` | Resting limit sell price in cents. Posted immediately on fill |
| `stop_frac` | Stop loss = `stop_frac × entry_price`. Monitored via REST polling every 30s |
| `at_open_only` | If true, only enter if price opens directly inside the band (no resting bid from above) |

**Entry window:** TTX 18–24h before market close. Entry order rests for up to 6 hours before cancellation.

**Stop loss:** Not a resting order. Polled every 30 seconds. When bid ≤ stop price, a limit sell at 1¢ is placed (fills at best available bid).

**Exit:** Target is a resting limit sell. No fees on either leg (KXHIGH series are not on Kalshi's maker-fee list).

---

## Active Setups (Spring 2026)

| Series | City | Rank | Band | Target | Stop |
|---|---|---|---|---|---|
| KXHIGHNY | New York | 5 | [10, 15)¢ | 70¢ | 25% of entry |
| KXHIGHLAX | Los Angeles | 3 | [35, 40)¢ | 55¢ | 25% of entry |
| KXHIGHMIA | Miami | 4 | [25, 33)¢ | 50¢ | 25% of entry |

NY Spring has `at_open_only` — only enters if the window opens with price already in the band. No resting bid from above.

---

## Telegram Bot Commands

The bot only responds to your configured `TELEGRAM_CHAT_ID`. It also sends proactive alerts when the engine acts.

### Commands

| Command | What it does |
|---|---|
| `status` | Runs `scripts/status.py` and returns: account balance, open positions (with stop/target), open orders, today's active setups with live bid/ask, tomorrow's markets, engine process status, and today's trade log |
| `restart` | Runs `systemctl restart kalshi-engine.service` — safely restarts the engine, which reconnects to broker state via reconcile on startup |
| anything else | Prints the help text |

### Proactive Alerts (sent automatically by the engine)

| Alert | Trigger |
|---|---|
| `ORDER {ticker} @{price}¢ approach={at_open\|from_above}` | Entry order posted (ENTRY_PENDING) |
| `ENTERED {ticker} @{price}¢ stop={X}¢ target={Y}¢` | Entry fill confirmed |
| `TARGET {ticker} @{price}¢ P&L: +X.Xc` | Target sell filled |
| `STOP {ticker} bid≤{X}¢ — selling at market` | Stop loss triggered |
| `{STOP/TARGET} {ticker} @{price}¢ P&L: ±X.Xc` | Exit fill confirmed |
| `ERROR {ticker}: target order FAILED...` | Target order failed after 2 attempts — manual action needed |

---

## Things to Watch For

### High priority

**"ORDER posted but no ENTERED alert"**
The entry order is resting but hasn't been filled. Normal. If 6 hours pass with no fill, the engine cancels it and sends no alert (logs `no_fill`). If the cancel returns 404 (order was filled but fill event was somehow missed), the engine now verifies via REST, recovers the position, and posts the target sell.

**"ENTERED alert but no TARGET or STOP for a long time"**
Check `status` — confirm the target order appears in Open Orders and the stop monitor's last-checked time is recent (should be < 60s ago while in ENTERED state). If the stop monitor is stale, the engine may have crashed — check with `status` to see if the engine process is running.

**Engine not running (`status` shows ✗)**
The engine is managed by systemd (`kalshi-engine.service`). On a fresh start it auto-discovers today's markets and reconciles any open positions. Send `restart` in Telegram or run `systemctl start kalshi-engine` on the VPS. It will **not** duplicate orders for already-filled entries — reconcile detects existing positions.

**Open position with no exit order**
If a position exists but no target sell is in Open Orders (check `status`), something went wrong with bracket order placement. The engine will log `ERROR ... target order FAILED`. Manually place a limit sell at the target price and a fallback limit sell at the stop price. Then `restart` the engine — reconcile will detect the existing sell order and attach to it.

**Stop loss not triggering**
Stop is polled every 30s via REST bid. If the market is very thin and the bid jumps over the stop (e.g., no trades between bid=15 and bid=5), the monitor will still catch it on the next 30s poll as long as bid ≤ stop at poll time. If you see the price blow through the stop with no alert, manually sell and then `restart` the engine.

### Lower priority

**"from_above" entries always buy at the top of the band**
When the market opens above the band (`price ≥ band_hi`), we post a resting bid at `band_hi - 1`. This is the intended behavior — we're waiting for price to fall into the band. EV is lower than the backtest assumed (which used the actual opening price), because we always fill at the highest point in the band. The 6h cancel handles the case where price never falls to that level; **but there is currently no cancel if price falls below `band_lo` while we're waiting**. If price blows through the band to the downside without filling us, the order will rest until the 6h window expires. Watch for this on volatile days.

**MIA stop_frac is 25% — tight**
A 25% stop on a 32¢ entry means stop at 8¢. MIA markets can move fast in both directions. If you're seeing stop-outs that reverse immediately, consider widening to 35–40% for MIA in the config.

---

## Useful VPS Commands

```bash
# SSH in
ssh -i ~/.ssh/hetzner root@5.161.111.138

# ── Service management ───────────────────────────────────────────────────────

# Check what's running
systemctl status kalshi-engine kalshi-bot --no-pager

# Restart engine (also available via Telegram: "restart")
systemctl restart kalshi-engine

# View live engine output
journalctl -u kalshi-engine -f

# ── Logs ─────────────────────────────────────────────────────────────────────

# Today's structured trade events
tail -f /opt/kalshi/logs/live_$(date -u +%Y%m%d).jsonl

# Raw engine stdout (errors, INFO lines)
tail -f /opt/kalshi/logs/engine.log

# ── Strategy ─────────────────────────────────────────────────────────────────
cd /opt/kalshi

# Dry run — see what markets would be traded today, no orders
.venv/bin/python3 scripts/live_engine.py --dry-run

# Full status report
.venv/bin/python3 scripts/status.py

# Historical P&L
.venv/bin/python3 scripts/trade_report.py

# ── Data ─────────────────────────────────────────────────────────────────────

# Pull recent data and rebuild DB
.venv/bin/python3 scripts/daily_refresh.py
.venv/bin/python3 scripts/build_db.py

# ── Emergency ────────────────────────────────────────────────────────────────

# Check current positions and orders
.venv/bin/python3 -c "
import asyncio, json, os, sys
from dotenv import load_dotenv; load_dotenv('.env'); sys.path.insert(0,'.')
from api.client import KalshiClient
async def main():
    async with KalshiClient(os.getenv('KALSHI_KEY_ID'), os.getenv('KALSHI_PRIVATE_KEY_PATH')) as c:
        pos = await c.get_positions()
        for p in pos.get('market_positions', []):
            if float(p.get('position_fp', 0)) != 0:
                print(p)
        orders = await c.get_orders(status='resting', limit=50)
        for o in orders.get('orders', []):
            print(o)
asyncio.run(main())
"
```

---

## Updating the Strategy Config

All entry parameters live in `strategies/temperature/config.py`. To change a band, target, or stop:

1. Edit the relevant `CONFIGS` entry locally
2. `git push`
3. On VPS: `git pull && systemctl restart kalshi-engine`

The engine reads config once at startup (during `discover_todays_markets`). A restart is required for config changes to take effect.

---

## Deployment

Both services start automatically on VPS boot. To check/re-enable:

```bash
systemctl enable kalshi-engine kalshi-bot
systemctl start kalshi-engine kalshi-bot
```

For initial setup or a full re-deploy, see `DEPLOYMENT.md`.
