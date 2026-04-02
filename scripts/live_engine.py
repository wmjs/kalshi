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

from api.client import KalshiClient
from api.websocket import KalshiWebSocket
from risk.manager import RiskLimits, RiskManager
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
            await engine.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kalshi temperature strategy live engine")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover today's markets and print — no orders placed")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
