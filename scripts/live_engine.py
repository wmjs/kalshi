"""
Live temperature strategy engine — v1 (1 contract sizing).

Discovers today's active temperature markets, subscribes to WebSocket
trade and order_fill channels, and executes the directional entry/exit
strategy defined in strategies/temperature/.

Usage:
    python3 scripts/live_engine.py [--dry-run]

Dry run:
    Discovers today's markets and prints what would be traded.
    No WebSocket connection, no orders placed.

Environment variables (.env):
    KALSHI_KEY_ID               — API key ID from Kalshi dashboard
    KALSHI_PRIVATE_KEY_PATH     — path to PEM-encoded RSA private key
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from api.client import KalshiAPIError, KalshiClient
from api.websocket import KalshiWebSocket
from risk.manager import RiskLimits, RiskManager
from strategies.base import PositionState
from strategies.temperature.engine import TemperatureEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Risk limits
# ---------------------------------------------------------------------------

LIMITS = RiskLimits(
    max_position_per_market=10,   # 10 contracts max per ticker
    max_total_delta=40,           # gross exposure cap across all markets
    max_loss_per_market=5.0,      # $5 per market (stops further buying if a position goes very wrong)
    max_total_loss=15.0,          # 15% of $100 account — daily halt threshold
)


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------

async def reconcile(
    client: KalshiClient,
    risk: RiskManager,
    log_path: Path,
    active_tickers: set[str],
) -> tuple[list[dict], list[dict]]:
    """
    On (re)start: sync RiskManager state with any positions/orders left by a
    prior session, and cancel orphaned resting orders.

    Returns (positions, orders) — the raw broker data for active tickers so
    the engine can call reconcile_from_broker() to re-attach its state machine.

    1. Open positions — load into risk manager so the daily loss limit is
       correctly enforced from the start.
    2. Resting orders — cancel any whose ticker is not in today's active setups
       (orphans from a crashed prior session).
    3. Prior realized P&L from today's log — seed the risk manager so the
       daily loss limit accounts for trades already completed this session.
    """
    log.info("Reconciling prior state...")

    positions: list[dict] = []
    orders: list[dict] = []

    # ---- 1. Load open positions ----
    try:
        resp = await client.get_positions()
        for pos in resp.get("market_positions", []):
            ticker  = pos.get("ticker", "")
            net_yes = pos.get("position", 0)
            if net_yes != 0:
                avg_cost = pos.get("market_exposure", 0) / max(abs(net_yes), 1) / 100.0
                ps = PositionState(ticker=ticker, net_yes=net_yes,
                                   realized_pnl=0.0, avg_cost=avg_cost)
                risk.update_position(ps)
                if ticker not in active_tickers:
                    log.warning("Orphaned position: %s  net_yes=%d", ticker, net_yes)
                else:
                    log.info("Restored position: %s  net_yes=%d", ticker, net_yes)
                    positions.append(pos)
    except KalshiAPIError as e:
        log.warning("reconcile: get_positions failed: %s", e)

    # ---- 2. Cancel orphaned resting orders; collect active-ticker orders ----
    try:
        resp = await client.get_orders(status="resting")
        for order in resp.get("orders", []):
            ticker   = order.get("ticker", "")
            order_id = order.get("order_id") or order.get("id")
            if ticker not in active_tickers:
                if order_id:
                    log.warning("Cancelling orphaned order %s on %s", order_id, ticker)
                    try:
                        await client.cancel_order(order_id)
                    except KalshiAPIError as e:
                        log.warning("Failed to cancel %s: %s", order_id, e)
            else:
                orders.append(order)
    except KalshiAPIError as e:
        log.warning("reconcile: get_orders failed: %s", e)

    # ---- 3. Seed realized P&L from today's log ----
    if log_path.exists():
        realized = 0.0
        try:
            import json as _json
            with open(log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    if ev.get("event") in ("exited", "settled"):
                        realized += ev.get("net_pnl_cents", 0.0) or 0.0
        except OSError:
            pass
        if realized != 0.0:
            log.info("Seeding %.2f cents realized P&L from prior session", realized)
            ps = PositionState(ticker="__prior__", net_yes=0,
                               realized_pnl=realized / 100.0, avg_cost=0.0)
            risk.update_position(ps)

    log.info("Reconciliation complete.")
    return positions, orders


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(dry_run: bool) -> None:
    key_id   = os.getenv("KALSHI_KEY_ID")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")

    if not key_id or not key_path:
        log.error("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set in .env")
        sys.exit(1)

    log_path = Path("logs") / f"live_{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    log_path.parent.mkdir(exist_ok=True)

    risk = RiskManager(LIMITS)

    if dry_run:
        log.info("DRY RUN — no orders will be placed")
        async with KalshiClient(key_id=key_id, private_key_path=key_path) as client:
            engine = TemperatureEngine(
                client=client,
                ws=None,         # not needed for dry run
                risk=risk,
                contracts=1,
                log_path=log_path,
            )
            setups = await engine.discover_todays_markets()
            if not setups:
                print("No active setups for today.")
                return
            print(f"\n{'':=<70}")
            print(f"  TODAY'S SETUPS  ({datetime.now(timezone.utc).date()})")
            print(f"{'':=<70}")
            for s in setups:
                cfg = s.config
                at_open_flag = "  [at_open_only]" if cfg.get("at_open_only") else ""
                print(f"  {s.ticker:35s}  rank={cfg['rank']}  "
                      f"band=[{cfg['band_lo']},{cfg['band_hi']})  "
                      f"target={cfg['target']}  stop={cfg['stop_frac']:.0%}"
                      f"{at_open_flag}")
            print()
        return

    log.info("Starting live engine  log=%s", log_path)
    async with KalshiClient(key_id=key_id, private_key_path=key_path) as client:
        async with KalshiWebSocket(key_id=key_id, private_key_path=key_path) as ws:
            engine = TemperatureEngine(
                client=client,
                ws=ws,
                risk=risk,
                contracts=1,
                log_path=log_path,
            )
            # Discover markets first so reconcile knows which tickers are active today
            setups = await engine.discover_todays_markets()
            active_tickers = {s.ticker for s in setups}
            positions, orders = await reconcile(client, risk, log_path, active_tickers)
            await engine.reconcile_from_broker(positions, orders)
            await engine.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kalshi temperature strategy live engine")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover today's markets and print — no orders placed")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
