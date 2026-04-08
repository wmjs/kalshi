"""
Telegram command bot.

Polls getUpdates and responds to commands sent to the bot.

Supported commands:
    status          — full system status report
    restart         — restart the live engine via systemd
    pnl             — open positions with unrealized P&L + today's realized
    exit <ticker>   — market-sell one specific position
    cancel <ticker> — cancel the entry order for one specific ticker

Run alongside the live engine as a systemd service (kalshi-bot.service).
Only responds to messages from TELEGRAM_CHAT_ID for security.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from api.client import KalshiAPIError, KalshiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
logger = logging.getLogger(__name__)

TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
KEY_ID   = os.getenv("KALSHI_KEY_ID")
KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

HELP_TEXT = (
    "Commands:\n"
    "  status          — full system status\n"
    "  restart         — restart the engine\n"
    "  pnl             — open positions + today's P&L\n"
    "  exit <ticker>   — market-sell one position\n"
    "  cancel <ticker> — cancel one entry bid\n"
)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

async def get_updates(tg: httpx.AsyncClient, offset: int) -> list[dict]:
    resp = await tg.get(
        f"https://api.telegram.org/bot{TOKEN}/getUpdates",
        params={"offset": offset, "timeout": 30},
        timeout=35.0,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


async def send(tg: httpx.AsyncClient, text: str) -> None:
    """Send plain text, splitting if needed (Telegram max 4096 chars)."""
    for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)]:
        await tg.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": chunk},
            timeout=10.0,
        )


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------

def _load_today_log() -> list[dict]:
    today    = datetime.now(timezone.utc).strftime("%Y%m%d")
    log_path = ROOT / "logs" / f"live_{today}.jsonl"
    if not log_path.exists():
        return []
    events = []
    with open(log_path) as f:
        for line in f:
            try:
                events.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                pass
    return events


def _entry_prices_from_log() -> dict[str, int]:
    """Return {ticker: entry_price_cents} from the last 'entered' event per ticker."""
    prices: dict[str, int] = {}
    for e in _load_today_log():
        if e.get("event") == "entered" and e.get("ticker") and e.get("entry_price") is not None:
            prices[e["ticker"]] = int(e["entry_price"])
    return prices


def _stop_targets_from_log() -> dict[str, dict]:
    """Return {ticker: {stop_price, target_price}} from the last 'entered' event."""
    meta: dict[str, dict] = {}
    for e in _load_today_log():
        if e.get("event") == "entered" and e.get("ticker"):
            meta[e["ticker"]] = {
                "stop":   e.get("stop_price"),
                "target": e.get("target_price"),
            }
    return meta


def _realized_pnl_from_log() -> float:
    """Sum net_pnl_cents from all exited/settled events today."""
    total = 0.0
    for e in _load_today_log():
        if e.get("event") in ("exited", "settled") and e.get("net_pnl_cents") is not None:
            total += float(e["net_pnl_cents"])
    return total


# ---------------------------------------------------------------------------
# Kalshi API actions (called directly by bot, independent of engine)
# ---------------------------------------------------------------------------

async def cmd_pnl() -> str:
    """Open positions with unrealized P&L and today's realized P&L."""
    entry_prices = _entry_prices_from_log()
    stop_targets = _stop_targets_from_log()
    realized     = _realized_pnl_from_log()

    lines: list[str] = []
    try:
        async with KalshiClient(KEY_ID, KEY_PATH) as k:
            resp      = await k.get_positions()
            positions = resp.get("market_positions", [])
            open_pos  = [
                p for p in positions
                if round(float(p.get("position_fp", 0))) != 0
            ]

            if not open_pos:
                lines.append("No open positions.")
            else:
                for p in open_pos:
                    ticker  = p.get("ticker", "?")
                    net_yes = round(float(p.get("position_fp", 0)))

                    # Current bid
                    bid: int | None = None
                    try:
                        mresp = await k.get_market(ticker)
                        mkt   = mresp.get("market", mresp)
                        raw   = mkt.get("yes_bid_dollars")
                        if raw is not None:
                            bid = round(float(raw) * 100)
                    except KalshiAPIError:
                        pass

                    entry  = entry_prices.get(ticker)
                    meta   = stop_targets.get(ticker, {})
                    stop   = meta.get("stop")
                    target = meta.get("target")

                    unreal_str = ""
                    if entry is not None and bid is not None:
                        unreal = (bid - entry) * net_yes
                        unreal_str = f"  unreal={unreal:+d}¢"

                    entry_str  = f"entry={entry}¢" if entry is not None else "entry=?"
                    bid_str    = f"bid={bid}¢"     if bid   is not None else "bid=?"
                    stop_str   = f"stop={stop}¢"   if stop  is not None else ""
                    target_str = f"target={target}¢" if target is not None else ""
                    bracket    = f"  ({stop_str}  {target_str})".rstrip() if (stop_str or target_str) else ""

                    lines.append(
                        f"{ticker}\n"
                        f"  net={net_yes:+d}  {entry_str}  {bid_str}{unreal_str}{bracket}"
                    )

    except KalshiAPIError as e:
        lines.append(f"API error: {e}")

    lines.append(f"\nRealized today: {realized:+.1f}¢  (${realized/100:+.4f})")
    return "\n".join(lines)



async def cmd_exit(ticker: str) -> str:
    """Market-sell one specific position."""
    try:
        async with KalshiClient(KEY_ID, KEY_PATH) as k:
            resp      = await k.get_positions()
            positions = resp.get("market_positions", [])
            pos       = next((p for p in positions if p.get("ticker") == ticker), None)

            if pos is None or round(float(pos.get("position_fp", 0))) == 0:
                return f"No open position found for {ticker}."

            net_yes = round(float(pos.get("position_fp", 0)))
            return await _exit_position(k, ticker, net_yes)
    except KalshiAPIError as e:
        return f"API error: {e}"


async def _exit_position(k: KalshiClient, ticker: str, net_yes: int) -> str:
    """
    Cancel any resting sell orders (target), then place a limit sell at 1¢.
    A limit sell at 1¢ on Kalshi fills immediately at the best available bid.
    """
    # Cancel resting sell orders (target) so they don't conflict
    try:
        orders_resp = await k.get_orders(status="resting", limit=50)
        for o in orders_resp.get("orders", []):
            if o.get("ticker") == ticker and o.get("action") == "sell":
                oid = o.get("order_id") or o.get("id")
                if oid:
                    try:
                        await k.cancel_order(oid)
                    except KalshiAPIError:
                        pass
    except KalshiAPIError:
        pass

    # Place limit sell at 1¢ — fills at best bid
    try:
        await k.create_order(
            ticker=ticker, side="yes", action="sell",
            count=net_yes, price=1,
        )
        return f"EXIT {ticker}: sell {net_yes} @ market (limit 1¢) placed."
    except KalshiAPIError as e:
        return f"EXIT {ticker}: FAILED — {e}"



async def cmd_cancel_ticker(ticker: str) -> str:
    """Cancel the resting entry (buy) order for a specific ticker."""
    try:
        async with KalshiClient(KEY_ID, KEY_PATH) as k:
            resp   = await k.get_orders(status="resting", limit=50)
            orders = [
                o for o in resp.get("orders", [])
                if o.get("ticker") == ticker and o.get("action") == "buy"
            ]

            if not orders:
                return f"No resting entry order found for {ticker}."

            results: list[str] = []
            for o in orders:
                oid   = o.get("order_id") or o.get("id")
                raw   = o.get("yes_price_dollars") or "?"
                price = round(float(raw) * 100) if raw != "?" else "?"
                results.append(await _cancel_order(k, oid, ticker, price))
            return "\n".join(results)
    except KalshiAPIError as e:
        return f"API error: {e}"


async def _cancel_order(k: KalshiClient, order_id: str, ticker: str, price) -> str:
    try:
        await k.cancel_order(order_id)
        return f"CANCEL {ticker} @{price}¢: cancelled."
    except KalshiAPIError as e:
        return f"CANCEL {ticker} @{price}¢: FAILED — {e}"


# ---------------------------------------------------------------------------
# System commands
# ---------------------------------------------------------------------------

def run_status() -> str:
    result = subprocess.run(
        [sys.executable, "scripts/status.py", "--no-ws"],
        capture_output=True, text=True, cwd=ROOT,
    )
    output = result.stdout + (result.stderr if result.returncode != 0 else "")
    return ANSI_RE.sub("", output).strip()


def restart_engine() -> str:
    result = subprocess.run(
        ["systemctl", "restart", "kalshi-engine.service"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return "Engine restarted via systemd (kalshi-engine.service)."
    return f"systemctl restart failed:\n{result.stderr.strip() or result.stdout.strip()}"


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

async def handle(tg: httpx.AsyncClient, msg: dict) -> None:
    chat_id = str(msg.get("chat", {}).get("id", ""))
    raw     = msg.get("text", "").strip()

    if chat_id != CHAT_ID:
        logger.warning("Ignoring message from unknown chat_id=%s", chat_id)
        return

    # Split into command and optional argument; preserve case for tickers
    parts  = raw.split(None, 1)
    cmd    = parts[0].lower() if parts else ""
    arg    = parts[1].strip() if len(parts) > 1 else ""

    logger.info("Command: %r  arg: %r", cmd, arg)

    if cmd == "status":
        await send(tg, "Running status...")
        await send(tg, run_status() or "(no output)")

    elif cmd == "restart":
        await send(tg, "Restarting engine...")
        reply = await asyncio.get_event_loop().run_in_executor(None, restart_engine)
        await send(tg, reply)

    elif cmd == "pnl":
        await send(tg, await cmd_pnl())

    elif cmd == "exit":
        if not arg:
            await send(tg, "Usage: exit <ticker>\nExample: exit KXHIGHMIA-26APR09-B76.5")
            return
        await send(tg, f"Exiting {arg}...")
        await send(tg, await cmd_exit(arg))

    elif cmd == "cancel":
        if not arg:
            await send(tg, "Usage: cancel <ticker>\nExample: cancel KXHIGHMIA-26APR09-B76.5")
            return
        await send(tg, f"Cancelling entry order for {arg}...")
        await send(tg, await cmd_cancel_ticker(arg))

    else:
        await send(tg, HELP_TEXT)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main() -> None:
    if not TOKEN or not CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        sys.exit(1)
    if not KEY_ID or not KEY_PATH:
        logger.error("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set in .env")
        sys.exit(1)

    logger.info("Bot started — polling for commands")
    offset = 0

    async with httpx.AsyncClient() as tg:
        while True:
            try:
                updates = await get_updates(tg, offset)
                for update in updates:
                    offset = update["update_id"] + 1
                    msg    = update.get("message", {})
                    if msg:
                        await handle(tg, msg)
            except httpx.HTTPError as e:
                logger.warning("HTTP error: %s — retrying in 5s", e)
                await asyncio.sleep(5)
            except Exception as e:
                logger.error("Unexpected error: %s — retrying in 5s", e)
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
