"""
Convert raw JSONL data to per-series Parquet files and build a DuckDB
database with views that span all series automatically.

Running this after pulling a new series (via pull_series.py) will
incorporate it into the database without any manual changes.

Output:
    data/processed/
        {SERIES}/
            markets.parquet
            trades.parquet
        kalshi.duckdb          — views over all series via glob

Schema additions beyond raw API fields:
    trades + markets:
        series           str   e.g. "KXHIGHNY"
        market_date      date  parsed from ticker
        bracket_type     str   "T" (above) or "B" (between)
        strike           float temperature threshold

    trades only:
        yes_price        float yes_price_dollars * 100  (0-99 scale)
        no_price         float 100 - yes_price
        count            float contracts traded
        close_time       ts    from market metadata
        time_to_expiry   float seconds from trade to market close

    markets only:
        result_yes       bool  True if result == "yes"
        volume           float volume_fp cast to float

Usage:
    python scripts/build_db.py                        # rebuild all series
    python scripts/build_db.py --series KXHIGHNY      # rebuild one series only
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import duckdb
import pandas as pd

RAW_BASE       = Path("data/raw")
PROCESSED_BASE = Path("data/processed")
DB_PATH        = Path("data/processed/kalshi.duckdb")

DATE_RE = re.compile(r"(\d{2})([A-Z]{3})(\d{2})")
MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


# ---------------------------------------------------------------------------
# Ticker parsing
# ---------------------------------------------------------------------------

def parse_ticker(ticker: str) -> dict:
    """
    KXHIGHNY-26JAN01-T37   -> {market_date, bracket_type="T", strike=37.0}
    KXLOWCHI-26JAN01-B36.5 -> {market_date, bracket_type="B", strike=36.5}
    """
    parts = ticker.split("-")
    date_str   = parts[1]
    bracket_str = parts[2]

    m = DATE_RE.match(date_str)
    year  = int("20" + m.group(1))
    month = MONTH_MAP[m.group(2)]
    day   = int(m.group(3))
    market_date = datetime(year, month, day).date()

    bracket_type = bracket_str[0]
    strike       = float(bracket_str[1:])

    return {"market_date": market_date, "bracket_type": bracket_type, "strike": strike}


# ---------------------------------------------------------------------------
# Per-series build
# ---------------------------------------------------------------------------

def build_markets(series: str, raw_dir: Path) -> pd.DataFrame:
    records = [json.loads(l) for l in open(raw_dir / "markets.jsonl")]
    df = pd.DataFrame(records)

    df["series"]     = series
    df["close_time"] = pd.to_datetime(df["close_time"], utc=True, format="ISO8601")
    df["open_time"]  = pd.to_datetime(df["open_time"],  utc=True, format="ISO8601")

    parsed = df["ticker"].apply(parse_ticker).apply(pd.Series)
    df = pd.concat([df, parsed], axis=1)

    df["result_yes"] = df["result"] == "yes"
    df["volume"]     = pd.to_numeric(df["volume_fp"], errors="coerce")

    for col in ["yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars",
                "no_ask_dollars", "last_price_dollars", "previous_price_dollars"]:
        if col in df.columns:
            df[col.replace("_dollars", "_cents")] = pd.to_numeric(df[col], errors="coerce") * 100

    return df


def build_trades(series: str, raw_dir: Path, markets_df: pd.DataFrame) -> pd.DataFrame:
    close_times = markets_df.set_index("ticker")["close_time"].to_dict()

    files = sorted((raw_dir / "trades").glob("*.jsonl"))
    chunks = []
    for f in files:
        lines = f.read_text().splitlines()
        if not lines:
            continue
        chunks.append(pd.DataFrame([json.loads(l) for l in lines]))

    df = pd.concat(chunks, ignore_index=True)

    df["series"]       = series
    df["created_time"] = pd.to_datetime(df["created_time"], utc=True, format="ISO8601")
    df["yes_price"]    = df["yes_price_dollars"].astype(float) * 100
    df["no_price"]     = df["no_price_dollars"].astype(float) * 100
    df["count"]        = df["count_fp"].astype(float)

    parsed = df["ticker"].apply(parse_ticker).apply(pd.Series)
    df = pd.concat([df, parsed], axis=1)

    df["close_time"]     = df["ticker"].map(close_times)
    df["time_to_expiry"] = (df["close_time"] - df["created_time"]).dt.total_seconds()

    df = df.drop(columns=["yes_price_dollars", "no_price_dollars", "count_fp"])
    df = df.sort_values(["ticker", "created_time"]).reset_index(drop=True)

    return df


def build_series(series: str) -> tuple[Path, Path]:
    raw_dir       = RAW_BASE / series
    processed_dir = PROCESSED_BASE / series
    processed_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{series}] Building markets...", end=" ", flush=True)
    markets_df   = build_markets(series, raw_dir)
    markets_path = processed_dir / "markets.parquet"
    markets_df.to_parquet(markets_path, index=False)
    print(f"{len(markets_df)} markets  ({markets_path.stat().st_size / 1024:.0f} KB)")

    print(f"[{series}] Building trades...",  end=" ", flush=True)
    trades_df   = build_trades(series, raw_dir, markets_df)
    trades_path = processed_dir / "trades.parquet"
    trades_df.to_parquet(trades_path, index=False, compression="zstd")
    print(f"{len(trades_df):,} trades  ({trades_path.stat().st_size / 1e6:.1f} MB)")

    return markets_path.resolve(), trades_path.resolve()


# ---------------------------------------------------------------------------
# DuckDB — glob views across all series
# ---------------------------------------------------------------------------

def build_duckdb() -> None:
    # Discover all processed series by looking for parquet files
    markets_glob = str(PROCESSED_BASE.resolve() / "*" / "markets.parquet")
    trades_glob  = str(PROCESSED_BASE.resolve() / "*" / "trades.parquet")

    print(f"Building DuckDB → {DB_PATH}")
    con = duckdb.connect(str(DB_PATH))

    con.execute(f"""
        CREATE OR REPLACE VIEW markets AS
        SELECT * FROM read_parquet('{markets_glob}')
    """)

    con.execute(f"""
        CREATE OR REPLACE VIEW trades AS
        SELECT * FROM read_parquet('{trades_glob}')
    """)

    con.execute("""
        CREATE OR REPLACE VIEW trades_with_result AS
        SELECT
            t.*,
            m.result_yes,
            m.result,
            m.volume AS market_volume
        FROM trades t
        JOIN markets m USING (ticker)
    """)

    # Summary view useful for quickly browsing what's loaded
    con.execute("""
        CREATE OR REPLACE VIEW series_summary AS
        SELECT
            series,
            COUNT(DISTINCT ticker)  AS n_markets,
            MIN(market_date)        AS first_date,
            MAX(market_date)        AS last_date,
            COUNT(*)                AS n_trades,
            SUM(count)              AS total_contracts
        FROM trades
        GROUP BY series
        ORDER BY series
    """)

    n_trades  = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    n_markets = con.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    series    = con.execute("SELECT DISTINCT series FROM markets ORDER BY series").df()["series"].tolist()
    print(f"  Series loaded : {series}")
    print(f"  Total markets : {n_markets:,}")
    print(f"  Total trades  : {n_trades:,}")
    con.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--series", default=None,
                        help="Rebuild a single series. Omit to rebuild all.")
    args = parser.parse_args()

    if args.series:
        series_list = [args.series]
    else:
        # Discover all series with raw data
        series_list = sorted(d.name for d in RAW_BASE.iterdir()
                             if d.is_dir() and (d / "markets.jsonl").exists())
        print(f"Found {len(series_list)} series: {series_list}")

    for series in series_list:
        build_series(series)

    build_duckdb()
    print("Done.")
