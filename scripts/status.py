"""
System status check for the Kalshi temperature strategy.

Checks REST API, WebSocket, account balance, open positions,
open orders, today's active setups, and today's trade log.

Usage:
    python3 scripts/status.py
    python3 scripts/status.py --no-ws   # skip WebSocket test
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from api.client import KalshiAPIError, KalshiClient
from api.websocket import KalshiWebSocket
from risk.manager import RiskLimits, RiskManager
from strategies.temperature.engine import TemperatureEngine
from strategies.temperature.config import ACTIVE_SERIES

KEY_ID   = os.getenv("KALSHI_KEY_ID")
KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")

RESET  = "\033[0m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"

def ok(msg: str)   -> str: return f"{GREEN}✓{RESET}  {msg}"
def err(msg: str)  -> str: return f"{RED}✗{RESET}  {msg}"
def warn(msg: str) -> str: return f"{YELLOW}!{RESET}  {msg}"
def hdr(msg: str)  -> str: return f"\n{BOLD}{msg}{RESET}"


async def check_credentials() -> bool:
    print(hdr("Credentials"))
    if not KEY_ID:
        print(err("KALSHI_KEY_ID not set in .env"))
        return False
    if not KEY_PATH:
        print(err("KALSHI_PRIVATE_KEY_PATH not set in .env"))
        return False
    key_file = Path(KEY_PATH).expanduser()
    if not key_file.exists():
        print(err(f"Private key file not found: {key_file}"))
        return False
    print(ok(f"Key ID:   {KEY_ID}"))
    print(ok(f"Key file: {key_file}"))
    return True


async def check_rest(client: KalshiClient) -> bool:
    print(hdr("REST API"))
    try:
        bal = await client.get_balance()
        balance_cents = bal.get("balance", 0)
        print(ok(f"Connected  balance=${balance_cents / 100:.2f}"))
        return True
    except KalshiAPIError as e:
        print(err(f"REST failed: {e}"))
        return False
    except Exception as e:
        print(err(f"REST error: {e}"))
        return False


async def check_websocket(ws_ticker: str) -> bool:
    print(hdr("WebSocket"))
    try:
        async with KalshiWebSocket(key_id=KEY_ID, private_key_path=KEY_PATH) as ws:
            await ws.subscribe(["orderbook_delta"], [ws_ticker])
            msg = await asyncio.wait_for(ws.__anext__(), timeout=10.0)
            msg_type = msg.get("type", msg.get("msg", {}).get("type", "unknown"))
            print(ok(f"Connected  received msg type='{msg_type}'  ticker={ws_ticker}"))
            return True
    except asyncio.TimeoutError:
        print(warn("Connected but no message received within 10s (market may be inactive)"))
        return True
    except Exception as e:
        print(err(f"WebSocket failed: {e}"))
        return False


async def check_positions(client: KalshiClient) -> None:
    print(hdr("Open Positions"))
    try:
        resp = await client.get_positions()
        positions = resp.get("market_positions", [])
        active = [p for p in positions if p.get("position", 0) != 0]
        if not active:
            print("   (none)")
        for p in active:
            ticker   = p.get("ticker", "?")
            net_yes  = p.get("position", 0)
            realized = p.get("realized_pnl", 0)
            print(f"   {ticker:40s}  net_yes={net_yes:+d}  realized=${realized/100:.2f}")
    except KalshiAPIError as e:
        print(err(f"Could not fetch positions: {e}"))


async def check_orders(client: KalshiClient) -> None:
    print(hdr("Open Orders"))
    try:
        resp   = await client.get_orders(status="resting", limit=50)
        orders = resp.get("orders", [])
        if not orders:
            print("   (none)")
        for o in orders:
            ticker = o.get("ticker", "?")
            side   = o.get("side", "?")
            action = o.get("action", "?")
            price  = o.get("price", "?")
            count  = o.get("remaining_count", o.get("count", "?"))
            oid    = o.get("order_id", o.get("id", "?"))[:8]
            print(f"   {ticker:40s}  {action:4s} {side:3s} @{price:2}¢  qty={count}  id={oid}...")
    except KalshiAPIError as e:
        print(err(f"Could not fetch orders: {e}"))


async def check_tomorrows_markets(client: KalshiClient) -> None:
    """Show all markets with TTX in (24h, 48h] — next day's tradeable window."""
    print(hdr("Tomorrow's Markets"))
    now = datetime.now(timezone.utc)

    found_any = False
    for series in ACTIVE_SERIES:
        try:
            resp    = await client.get_markets(series_ticker=series, limit=200)
            markets = resp.get("markets", [])
        except KalshiAPIError as e:
            print(err(f"{series}: failed to fetch markets: {e}"))
            continue

        tomorrows = []
        for m in markets:
            ct_raw = m.get("close_time", "")
            try:
                ct = datetime.fromisoformat(ct_raw.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            ttx_h = (ct - now).total_seconds() / 3600
            if 24 < ttx_h <= 48:
                tomorrows.append((m["ticker"], ct, ttx_h, m))

        if not tomorrows:
            continue

        found_any = True
        tomorrows.sort(key=lambda x: x[0])
        for ticker, ct, ttx_h, m in tomorrows:
            yes_ask   = m.get("yes_ask")
            yes_bid   = m.get("yes_bid")
            price_str = f"bid={yes_bid}  ask={yes_ask}" if yes_bid is not None else ""
            print(f"   {ticker:42s}  ttx={ttx_h:.1f}h  {price_str}")

    if not found_any:
        print("   (no markets found in 24-48h window)")


async def check_todays_setups(client: KalshiClient) -> str | None:
    """Returns a live ticker to use for the WebSocket test, or None."""
    print(hdr("Today's Active Setups"))
    risk   = RiskManager(RiskLimits(10, 40, 5.0, 15.0))
    engine = TemperatureEngine(client=client, ws=None, risk=risk, contracts=1)
    setups = await engine.discover_todays_markets()
    if not setups:
        print("   (no active setups today)")
        return None
    now       = datetime.now(timezone.utc)
    ws_ticker = None
    for s in setups:
        cfg   = s.config
        ttx_h = (s.close_time - now).total_seconds() / 3600
        at    = "  [at_open_only]" if cfg.get("at_open_only") else ""

        price_str = ""
        try:
            mresp = await client.get_market(s.ticker)
            m     = mresp.get("market", mresp)
            bid   = m.get("yes_bid_dollars")
            ask   = m.get("yes_ask_dollars")
            last  = m.get("last_price_dollars")
            if bid is not None and ask is not None:
                bid_c  = round(float(bid) * 100)
                ask_c  = round(float(ask) * 100)
                last_c = round(float(last) * 100) if last is not None else None
                last_s = f"  last={last_c}¢" if last_c is not None else ""
                price_str = f"  bid={bid_c}¢  ask={ask_c}¢{last_s}"
        except KalshiAPIError:
            pass

        print(f"   {s.ticker:40s}  rank={cfg['rank']}  ttx={ttx_h:.1f}h{price_str}  "
              f"band=[{cfg['band_lo']},{cfg['band_hi']})  "
              f"target={cfg['target']}  stop={cfg['stop_frac']:.0%}{at}")
        ws_ticker = s.ticker
    return ws_ticker


def check_engine_process() -> None:
    print(hdr("Live Engine"))
    import subprocess
    result = subprocess.run(["pgrep", "-a", "-f", "live_engine.py"], capture_output=True, text=True)
    pids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if pids:
        for line in pids:
            pid = line.split()[0]
            print(ok(f"Running  pid={pid}"))
    else:
        print(err("Not running  (start with: nohup python3 scripts/live_engine.py >> logs/run_$(date -u +%Y%m%d).log 2>&1 &)"))


def check_log() -> None:
    print(hdr("Today's Trade Log"))
    today    = datetime.now(timezone.utc).strftime("%Y%m%d")
    log_path = Path("logs") / f"live_{today}.jsonl"
    if not log_path.exists():
        print(f"   No log file yet ({log_path})")
        return
    events = []
    with open(log_path) as f:
        for line in f:
            try:
                events.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                pass
    if not events:
        print(f"   Log exists but is empty: {log_path}")
        return
    print(f"   {log_path}  ({len(events)} events)")
    for e in events:
        ts      = e.get("ts", "")[-15:-4] if e.get("ts") else ""
        event   = e.get("event", "")
        ticker  = e.get("ticker", "")
        outcome = e.get("outcome", "")
        pnl     = e.get("net_pnl_cents")
        pnl_str = f"  pnl={pnl:+.2f}¢" if pnl is not None else ""
        print(f"   {ts}  {event:20s}  {ticker:35s}  {outcome}{pnl_str}")

    # Print summary if present
    summary = next((e for e in reversed(events) if e.get("event") == "daily_summary"), None)
    if summary:
        total = summary.get("total_pnl_cents", 0)
        print(f"\n   {'─'*50}")
        print(f"   Total P&L: {total:+.2f}¢  (${total/100:+.4f})")


async def main(skip_ws: bool) -> None:
    now = datetime.now(timezone.utc)
    print(f"{BOLD}{'='*60}")
    print(f"  Kalshi Strategy Status  —  {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*60}{RESET}")

    ok_creds = await check_credentials()
    if not ok_creds:
        sys.exit(1)

    async with KalshiClient(key_id=KEY_ID, private_key_path=KEY_PATH) as client:
        rest_ok   = await check_rest(client)
        ws_ticker = await check_todays_setups(client)
        if rest_ok:
            await check_positions(client)
            await check_orders(client)
        await check_tomorrows_markets(client)

    check_engine_process()
    check_log()

    if not skip_ws:
        ticker_to_test = ws_ticker
        if ticker_to_test is None:
            # Fall back to a known liquid ticker for connectivity test
            ticker_to_test = "KXHIGHLAX-26APR03-T74"
        await check_websocket(ticker_to_test)

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Strategy system status check")
    parser.add_argument("--no-ws", action="store_true", help="Skip WebSocket connectivity test")
    args = parser.parse_args()
    asyncio.run(main(skip_ws=args.no_ws))
