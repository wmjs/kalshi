"""
Market data fetching: pulls historical snapshots and trades from Kalshi API
and stores them locally. Intended for offline analysis and backtesting.
"""

import asyncio
import json
import logging
from pathlib import Path

from api.client import KalshiClient

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data" / "raw"


class MarketDataFetcher:
    """
    Pulls market metadata, orderbook snapshots, and trade history.
    Writes newline-delimited JSON to data/raw/<ticker>/<type>.jsonl.
    """

    def __init__(self, client: KalshiClient, data_dir: Path = DATA_DIR) -> None:
        self.client = client
        self.data_dir = data_dir

    async def fetch_trades(self, ticker: str, max_pages: int = 50) -> list[dict]:
        """
        Fetches all available trade history for a market (paginated).
        Returns list of trade dicts sorted oldest-first.
        """
        trades = []
        cursor = None
        for _ in range(max_pages):
            resp = await self.client.get_trades(ticker, cursor=cursor, limit=100)
            batch = resp.get("trades", [])
            trades.extend(batch)
            cursor = resp.get("cursor")
            if not cursor:
                break
        trades.sort(key=lambda t: t["created_time"])
        return trades

    async def fetch_and_save_trades(self, ticker: str) -> Path:
        trades = await self.fetch_trades(ticker)
        out = self.data_dir / ticker
        out.mkdir(parents=True, exist_ok=True)
        path = out / "trades.jsonl"
        with open(path, "w") as f:
            for t in trades:
                f.write(json.dumps(t) + "\n")
        logger.info(f"Saved {len(trades)} trades for {ticker} -> {path}")
        return path

    async def fetch_markets_by_series(self, series_ticker: str) -> list[dict]:
        """Fetch all markets (including expired) for a series ticker."""
        markets = []
        cursor = None
        while True:
            resp = await self.client.get_markets(
                series_ticker=series_ticker,
                cursor=cursor,
                limit=100,
            )
            batch = resp.get("markets", [])
            markets.extend(batch)
            cursor = resp.get("cursor")
            if not cursor:
                break
        return markets
