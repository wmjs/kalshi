"""
Daily data refresh for the Kalshi temperature strategy.

Pulls settled markets and their trades for all active series since N days ago,
then rebuilds the DuckDB database. Designed to run before live_engine.py each day.

Resumes automatically — skips markets whose trade file already exists.

Usage:
    python3 scripts/daily_refresh.py           # pull last 2 days (default)
    python3 scripts/daily_refresh.py --days 7  # pull last 7 days

Environment variables (.env):
    KALSHI_KEY_ID               — API key ID from Kalshi dashboard
    KALSHI_PRIVATE_KEY_PATH     — path to PEM-encoded RSA private key
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from api.client import KalshiAPIError, KalshiClient
from scripts.pull_series import fetch_all_markets, fetch_all_trades
from scripts.build_db import build_series, build_duckdb
from strategies.temperature.config import ACTIVE_SERIES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

RAW_BASE = Path("data/raw")


async def refresh_series(
    client: KalshiClient,
    series: str,
    start: datetime,
) -> dict:
    """
    Pull settled markets and their trades for one series since start.
    Skips markets whose trade file already exists (idempotent).
    Returns a summary dict.
    """
    data_dir   = RAW_BASE / series
    trades_dir = data_dir / "trades"
    data_dir.mkdir(parents=True, exist_ok=True)
    trades_dir.mkdir(parents=True, exist_ok=True)

    log.info("%s: fetching market list since %s...", series, start.date())
    markets = await fetch_all_markets(client, series, start)
    log.info("%s: %d markets found", series, len(markets))

    # Overwrite markets.jsonl with fresh metadata
    meta_path = data_dir / "markets.jsonl"
    with open(meta_path, "w") as f:
        for m in markets:
            f.write(json.dumps(m) + "\n")

    fetched = skipped = 0
    for market in markets:
        ticker   = market["ticker"]
        out_path = trades_dir / f"{ticker}.jsonl"

        if out_path.exists():
            skipped += 1
            continue

        for attempt in range(6):
            try:
                trades = await fetch_all_trades(client, ticker)
                break
            except KalshiAPIError as e:
                if e.status == 429:
                    wait = 2 ** attempt
                    log.debug("%s: 429, retrying in %ds", ticker, wait)
                    await asyncio.sleep(wait)
                else:
                    log.warning("%s: API error %s", ticker, e)
                    trades = []
                    break
        else:
            log.warning("%s: max retries exceeded", ticker)
            trades = []

        with open(out_path, "w") as f:
            for t in trades:
                f.write(json.dumps(t) + "\n")
        fetched += 1

    log.info("%s: done  fetched=%d  skipped(cached)=%d", series, fetched, skipped)
    return {"series": series, "markets": len(markets), "fetched": fetched, "skipped": skipped}


async def main(days: int) -> None:
    key_id   = os.getenv("KALSHI_KEY_ID")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")

    if not key_id or not key_path:
        log.error("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set in .env")
        sys.exit(1)

    start    = datetime.now(timezone.utc) - timedelta(days=days)
    log_path = Path("logs") / f"refresh_{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    log_path.parent.mkdir(exist_ok=True)

    log.info("Starting daily refresh  days=%d  start=%s", days, start.date())

    summaries = []
    async with KalshiClient(key_id=key_id, private_key_path=key_path) as client:
        for series in ACTIVE_SERIES:
            summary = await refresh_series(client, series, start)
            summaries.append(summary)

    log.info("Rebuilding DuckDB...")
    build_duckdb()
    log.info("DuckDB rebuilt.")

    # Write refresh log
    record = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "event":   "refresh_complete",
        "days":    days,
        "series":  summaries,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")

    total_fetched = sum(s["fetched"] for s in summaries)
    total_markets = sum(s["markets"] for s in summaries)
    log.info("Refresh complete  total_markets=%d  newly_fetched=%d", total_markets, total_fetched)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily Kalshi data refresh")
    parser.add_argument("--days", type=int, default=2,
                        help="Pull markets settled in the last N days (default: 2)")
    args = parser.parse_args()
    asyncio.run(main(args.days))
