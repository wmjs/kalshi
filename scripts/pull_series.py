"""
Bulk trade data pull for any Kalshi market series.

Fetches all markets since a start date and their full trade histories.
Resumes automatically — skips markets whose trade file already exists.

Output layout:
    data/raw/{SERIES}/
        markets.jsonl          — one market metadata record per line
        trades/<ticker>.jsonl  — one trade record per line, oldest first

Usage:
    python scripts/pull_series.py --series KXHIGHNY
    python scripts/pull_series.py --series KXLOWNY --start 2026-01-01
    python scripts/pull_series.py --series KXHIGHCHI --concurrency 4
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from api.client import KalshiAPIError, KalshiClient

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

RAW_BASE = Path(__file__).parent.parent / "data" / "raw"


async def fetch_all_markets(client: KalshiClient, series: str, start: datetime) -> list[dict]:
    markets, cursor = [], None
    while True:
        resp = await client.get_markets(series_ticker=series, status="settled", cursor=cursor, limit=100)
        batch = resp.get("markets", [])
        markets.extend(batch)
        cursor = resp.get("cursor")
        if not cursor or not batch:
            break

    active_resp = await client.get_markets(series_ticker=series, limit=100)
    markets.extend(active_resp.get("markets", []))

    filtered = [
        m for m in markets
        if m.get("close_time") and
        datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) >= start
    ]
    seen, out = set(), []
    for m in filtered:
        if m["ticker"] not in seen:
            seen.add(m["ticker"])
            out.append(m)
    return out


async def fetch_all_trades(client: KalshiClient, ticker: str) -> list[dict]:
    trades, cursor = [], None
    while True:
        for attempt in range(6):
            try:
                resp = await client.get_trades(ticker, cursor=cursor, limit=100)
                break
            except KalshiAPIError as e:
                if e.status == 429:
                    wait = 2 ** attempt
                    log.debug(f"{ticker}: 429, retrying in {wait}s")
                    await asyncio.sleep(wait)
                else:
                    raise
        else:
            raise KalshiAPIError(429, f"Max retries exceeded for {ticker}")

        batch = resp.get("trades", [])
        trades.extend(batch)
        cursor = resp.get("cursor")
        if not cursor or not batch:
            break
        await asyncio.sleep(0.1)

    trades.reverse()
    return trades


async def pull_market(
    client: KalshiClient,
    market: dict,
    trades_dir: Path,
    sem: asyncio.Semaphore,
) -> tuple[str, int]:
    ticker = market["ticker"]
    out_path = trades_dir / f"{ticker}.jsonl"

    if out_path.exists():
        return ticker, -1

    async with sem:
        try:
            trades = await fetch_all_trades(client, ticker)
            with open(out_path, "w") as f:
                for t in trades:
                    f.write(json.dumps(t) + "\n")
            return ticker, len(trades)
        except KalshiAPIError as e:
            log.warning(f"{ticker}: API error {e}")
            return ticker, 0
        except Exception as e:
            log.error(f"{ticker}: unexpected error {e}")
            return ticker, 0


async def main(series: str, start: datetime, concurrency: int) -> None:
    data_dir   = RAW_BASE / series
    trades_dir = data_dir / "trades"
    data_dir.mkdir(parents=True, exist_ok=True)
    trades_dir.mkdir(parents=True, exist_ok=True)

    key_id   = os.getenv("KALSHI_KEY_ID")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    async with KalshiClient(key_id=key_id, private_key_path=key_path) as client:
        log.info(f"Fetching market list for {series}...")
        markets = await fetch_all_markets(client, series, start)
        log.info(f"Found {len(markets)} markets since {start.date()}")

        meta_path = data_dir / "markets.jsonl"
        # Merge with existing metadata — never discard historical records
        existing: dict[str, dict] = {}
        if meta_path.exists():
            for line in meta_path.read_text().splitlines():
                if line.strip():
                    m = json.loads(line)
                    existing[m["ticker"]] = m
        for m in markets:
            existing[m["ticker"]] = m  # new data wins for updated fields
        with open(meta_path, "w") as f:
            for m in existing.values():
                f.write(json.dumps(m) + "\n")
        log.info(f"Market metadata saved → {meta_path} ({len(existing)} total markets)")

        sem = asyncio.Semaphore(concurrency)
        tasks = [pull_market(client, m, trades_dir, sem) for m in markets]

        done, skipped, total_trades = 0, 0, 0
        for coro in asyncio.as_completed(tasks):
            ticker, n = await coro
            if n == -1:
                skipped += 1
            else:
                done += 1
                total_trades += n
            finished = done + skipped
            if finished % 50 == 0 or finished == len(markets):
                log.info(
                    f"Progress: {finished}/{len(markets)}  "
                    f"fetched={done}  skipped(cached)={skipped}  "
                    f"trades_so_far={total_trades:,}"
                )

    log.info(f"Done. {done} markets fetched, {skipped} cached, {total_trades:,} total trades.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--series",      required=True, help="Series ticker, e.g. KXHIGHNY")
    parser.add_argument("--start",       default="2026-01-01")
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    asyncio.run(main(args.series, start, args.concurrency))
