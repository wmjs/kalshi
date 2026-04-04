"""
Strategy configuration for the multi-city temperature directional strategy.

Entry rules:
  - Buy YES on target rank/bracket when window opens at TTX >= 24h
  - from_below filter: skip if opening price < band_lo (universal)
  - at_open_only: only enter if opening price is directly in band (NY Spring)
  - From_above: post resting bid at band_hi - 1, cancel after 6h if unfilled

Timing:
  - WINDOW_TTX: window opens when TTX first drops to 24h
  - WINDOW_BUFFER: poll/listen 1h early so we don't miss the first trade event
  - WINDOW_DURATION: 6h to fill an entry order before cancelling
"""

import re
from datetime import datetime, timezone

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from analysis.temperature_strategy import SEASON_MAP

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------

WINDOW_TTX      = 24 * 3600   # window opens when TTX <= this (seconds)
WINDOW_BUFFER   =  1 * 3600   # detect window when TTX <= WINDOW_TTX + WINDOW_BUFFER
WINDOW_DURATION =  6 * 3600   # cancel unfilled entry bids after this many seconds

# ---------------------------------------------------------------------------
# Setup config
# ---------------------------------------------------------------------------

CONFIGS: dict[tuple[str, str], dict] = {
    # (series, season) -> entry parameters
    # at_open_only=True: only enter if window opens directly inside the band
    ("KXHIGHNY",   "Spring"): {"rank": 5, "band_lo": 10, "band_hi": 15, "target": 70, "stop_frac": 0.25, "at_open_only": True},
    ("KXHIGHNY",   "Summer"): {"rank": 4, "band_lo": 30, "band_hi": 35, "target": 70, "stop_frac": 0.25},
    ("KXHIGHNY",   "Fall"):   {"rank": 4, "band_lo": 30, "band_hi": 35, "target": 70, "stop_frac": 0.25},
    ("KXHIGHPHIL", "Summer"): {"rank": 4, "band_lo": 30, "band_hi": 35, "target": 50, "stop_frac": 0.60},
    ("KXHIGHPHIL", "Fall"):   {"rank": 4, "band_lo": 30, "band_hi": 35, "target": 50, "stop_frac": 0.60},
    ("KXHIGHLAX",  "Spring"): {"rank": 3, "band_lo": 35, "band_hi": 40, "target": 55, "stop_frac": 0.25},
    ("KXHIGHLAX",  "Summer"): {"rank": 3, "band_lo": 35, "band_hi": 40, "target": 55, "stop_frac": 0.25},
    ("KXHIGHLAX",  "Fall"):   {"rank": 4, "band_lo": 30, "band_hi": 35, "target": 50, "stop_frac": 0.25},
    ("KXHIGHLAX",  "Winter"): {"rank": 4, "band_lo": 25, "band_hi": 30, "target": 50, "stop_frac": 0.25},
    ("KXHIGHCHI",  "Fall"):   {"rank": 3, "band_lo": 23, "band_hi": 29, "target": 85, "stop_frac": 0.50},
    ("KXHIGHCHI",  "Winter"): {"rank": 3, "band_lo": 23, "band_hi": 29, "target": 75, "stop_frac": 0.60},
    ("KXHIGHMIA",  "Spring"): {"rank": 4, "band_lo": 25, "band_hi": 33, "target": 50, "stop_frac": 0.25},
    ("KXHIGHMIA",  "Fall"):   {"rank": 5, "band_lo": 13, "band_hi": 27, "target": 45, "stop_frac": 0.25},
    # MIA Fall rank 4 omitted: half-size is meaningless at 1 contract
}

ACTIVE_SERIES: list[str] = [
    "KXHIGHNY",
    "KXHIGHPHIL",
    "KXHIGHLAX",
    "KXHIGHCHI",
    "KXHIGHMIA",
]

# ---------------------------------------------------------------------------
# Season helpers
# ---------------------------------------------------------------------------

def current_season(dt: datetime) -> str:
    """Return season string for a given UTC datetime."""
    return SEASON_MAP[dt.month]


def active_config(series: str, dt: datetime) -> dict | None:
    """Return strategy config for (series, season) if active today, else None."""
    return CONFIGS.get((series, current_season(dt)))


# ---------------------------------------------------------------------------
# Ticker parsing + rank assignment
# (Replicates parse_ticker from scripts/build_db.py)
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"(\d{2})([A-Z]{3})(\d{2})")
_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_strike(ticker: str) -> float:
    """Extract the numeric strike from a ticker like KXHIGHNY-26MAR28-T37 → 37.0."""
    bracket_str = ticker.split("-")[2]  # e.g. "T37" or "T36.5"
    return float(bracket_str[1:])


def _parse_market_date(ticker: str) -> datetime.date:
    """Extract the market date from a ticker like KXHIGHNY-26MAR28-T37 → 2026-03-28."""
    date_str = ticker.split("-")[1]
    m = _DATE_RE.match(date_str)
    year  = int("20" + m.group(1))
    month = _MONTH_MAP[m.group(2)]
    day   = int(m.group(3))
    return datetime(year, month, day).date()


def assign_rank(markets: list[dict]) -> dict[int, dict]:
    """
    Given a list of market dicts for a single series on a single date (all bracket_type="T"),
    sort by strike ascending and assign ranks 1..N (rank 1 = coldest bracket).

    Returns {rank: market_dict}.
    """
    all_markets = [m for m in markets if len(m.get("ticker", "").split("-")) == 3]
    all_markets.sort(key=lambda m: _parse_strike(m["ticker"]))
    return {rank: m for rank, m in enumerate(all_markets, start=1)}
