"""
Convert raw JSONL data to monthly hive-partitioned Parquet files and build a
DuckDB database with views that span all series automatically.

Partition layout:
    data/processed/
        {SERIES}/
            markets.parquet
            trades/
                year=2026/
                    month=4/
                        trades.parquet
        kalshi.duckdb

Incremental: existing monthly partitions are skipped unless it is the current
month (which may have received new trades since the last run).

Schema additions beyond raw API fields:
    trades + markets:
        series           str   e.g. "KXHIGHNY"
        market_date      date  parsed from ticker
        bracket_type     str   "T" (above) or "B" (between)
        strike           float temperature threshold

    trades only:
        yes_price        int   yes_price_dollars * 100  (0-99 scale)
        no_price         int   100 - yes_price
        count            float contracts traded
        close_time       ts    from market metadata
        time_to_expiry   float seconds from trade to market close

    markets only:
        result_yes       bool  True if result == "yes"
        volume           float volume_fp cast to float

Usage:
    python scripts/build_db.py                        # incremental update all series
    python scripts/build_db.py --series KXHIGHNY      # incremental update one series
    python scripts/build_db.py --rebuild               # force full rebuild all series
    python scripts/build_db.py --series KXHIGHNY --rebuild  # force rebuild one series
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

RAW_BASE       = Path("data/raw")
PROCESSED_BASE = Path("data/processed")
DB_PATH        = Path("data/processed/kalshi.duckdb")

MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
MONTH_MAP_INV = {v: k for k, v in MONTH_MAP.items()}


# ---------------------------------------------------------------------------
# Vectorized ticker parsing
# ---------------------------------------------------------------------------

def parse_tickers_vectorized(ticker_series: pd.Series) -> pd.DataFrame:
    """
    Parse a Series of tickers into market_date, bracket_type, strike columns.
    Fully vectorized — no row-wise apply().

    KXHIGHNY-26APR02-B54.5 -> market_date=2026-04-02, bracket_type="B", strike=54.5
    KXHIGHNY-26APR02-T37   -> market_date=2026-04-02, bracket_type="T", strike=37.0
    """
    parts       = ticker_series.str.split("-", expand=True)
    date_str    = parts[1]   # "26APR02"
    bracket_str = parts[2]   # "B54.5" or "T37"

    year  = ("20" + date_str.str[:2]).astype(int)
    month = date_str.str[2:5].map(MONTH_MAP)
    day   = date_str.str[5:7].astype(int)

    market_date = pd.to_datetime(
        year.astype(str) + month.astype(str).str.zfill(2) + day.astype(str).str.zfill(2),
        format="%Y%m%d",
        utc=True,
    ).dt.date

    bracket_type = bracket_str.str[0]
    strike       = bracket_str.str[1:].astype(float)

    return pd.DataFrame({
        "market_date":  market_date,
        "bracket_type": bracket_type,
        "strike":       strike,
    })


def parse_date_from_filename(path: Path) -> tuple[int, int]:
    """Return (year, month) from a trade JSONL filename."""
    stem     = path.stem  # e.g. KXHIGHNY-26APR02-B54.5
    date_str = stem.split("-")[1]  # "26APR02"
    year     = int("20" + date_str[:2])
    month    = MONTH_MAP[date_str[2:5]]
    return year, month


# ---------------------------------------------------------------------------
# Per-series build
# ---------------------------------------------------------------------------

def build_markets(series: str, raw_dir: Path) -> pd.DataFrame:
    records = [json.loads(l) for l in open(raw_dir / "markets.jsonl")]
    df = pd.DataFrame(records)

    df["series"]     = series
    df["close_time"] = pd.to_datetime(df["close_time"], utc=True, format="ISO8601")
    df["open_time"]  = pd.to_datetime(df["open_time"],  utc=True, format="ISO8601")

    parsed = parse_tickers_vectorized(df["ticker"])
    df = pd.concat([df, parsed], axis=1)

    df["result_yes"] = df["result"] == "yes"
    df["volume"]     = pd.to_numeric(df["volume_fp"], errors="coerce")

    for col in ["yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars",
                "no_ask_dollars", "last_price_dollars", "previous_price_dollars"]:
        if col in df.columns:
            df[col.replace("_dollars", "_cents")] = pd.to_numeric(df[col], errors="coerce") * 100

    return df


def infer_close_time(market_date: pd.Series) -> pd.Series:
    """
    Infer close_time from market_date for markets missing from metadata.
    Temperature markets always close at market_date + 1 day @ 04:59:00 UTC.
    """
    next_day = pd.to_datetime(market_date.astype(str), utc=True) + pd.Timedelta(days=1)
    return next_day + pd.Timedelta(hours=4, minutes=59)


def _process_chunk(df: pd.DataFrame, close_times: dict, series: str) -> pd.DataFrame:
    df["series"]       = series
    df["created_time"] = pd.to_datetime(df["created_time"], utc=True, format="ISO8601")
    df["yes_price"]    = (df["yes_price_dollars"].astype(float) * 100).astype(int)
    df["no_price"]     = (df["no_price_dollars"].astype(float) * 100).astype(int)
    df["count"]        = df["count_fp"].astype(float)

    parsed = parse_tickers_vectorized(df["ticker"])
    df = pd.concat([df, parsed], axis=1)

    # Use metadata close_time where available; fall back to inferred for historical gaps
    df["close_time"] = df["ticker"].map(close_times)
    missing = df["close_time"].isna()
    if missing.any():
        df.loc[missing, "close_time"] = infer_close_time(df.loc[missing, "market_date"])

    df["time_to_expiry"] = (df["close_time"] - df["created_time"]).dt.total_seconds()

    df = df.drop(columns=["yes_price_dollars", "no_price_dollars", "count_fp"])
    return df


def build_trades_incremental(
    series: str,
    raw_dir: Path,
    markets_df: pd.DataFrame,
    trades_base: Path,
    rebuild: bool,
) -> tuple[int, int]:
    """
    Write monthly hive-partitioned Parquet under trades_base.

    Skips partitions that already exist unless rebuild=True or it is the
    current month (which may have new data).

    Returns (months_written, months_skipped).
    """
    close_times  = markets_df.set_index("ticker")["close_time"].to_dict()
    now          = datetime.now(timezone.utc)
    current_ym   = (now.year, now.month)

    files = sorted((raw_dir / "trades").glob("*.jsonl"))
    by_month: dict[tuple[int, int], list[Path]] = defaultdict(list)
    for f in files:
        by_month[parse_date_from_filename(f)].append(f)

    total    = len(by_month)
    written  = skipped = 0
    t_start  = time.time()

    for i, ((year, month), month_files) in enumerate(sorted(by_month.items()), 1):
        partition_dir  = trades_base / f"year={year}" / f"month={month}"
        partition_path = partition_dir / "trades.parquet"
        is_current     = (year, month) == current_ym
        label          = f"{year}-{MONTH_MAP_INV[month]}"

        if partition_path.exists() and not rebuild and not is_current:
            skipped += 1
            print(f"\r  [{i}/{total}] {label} skipped (cached)          ", end="", flush=True)
            continue

        partition_dir.mkdir(parents=True, exist_ok=True)

        rows = []
        for f in month_files:
            lines = f.read_text().splitlines()
            if lines:
                rows.extend(json.loads(l) for l in lines)

        if not rows:
            skipped += 1
            continue

        df    = pd.DataFrame(rows)
        df    = _process_chunk(df, close_times, series)
        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(table, partition_path, compression="zstd")
        written += 1

        elapsed = time.time() - t_start
        rate    = written / elapsed
        remaining = (total - i) / rate if rate > 0 else 0
        print(
            f"\r  [{i}/{total}] {label}  {len(df):,} trades  "
            f"~{remaining:.0f}s remaining          ",
            end="", flush=True,
        )

    print()
    return written, skipped


def build_series(series: str, rebuild: bool = False) -> None:
    raw_dir       = RAW_BASE / series
    processed_dir = PROCESSED_BASE / series
    trades_base   = processed_dir / "trades"
    processed_dir.mkdir(parents=True, exist_ok=True)
    trades_base.mkdir(parents=True, exist_ok=True)

    print(f"\n[{series}] Building markets...", end=" ", flush=True)
    markets_df   = build_markets(series, raw_dir)
    markets_path = processed_dir / "markets.parquet"
    markets_df.to_parquet(markets_path, index=False)
    print(f"{len(markets_df)} markets  ({markets_path.stat().st_size / 1024:.0f} KB)")

    print(f"[{series}] Building trades (monthly partitions):")
    t0 = time.time()
    written, skipped = build_trades_incremental(series, raw_dir, markets_df, trades_base, rebuild)
    elapsed = time.time() - t0
    total_partitions = sum(1 for _ in trades_base.rglob("trades.parquet"))
    print(
        f"[{series}] Done  written={written}  skipped={skipped}  "
        f"partitions={total_partitions}  elapsed={elapsed:.1f}s"
    )


# ---------------------------------------------------------------------------
# DuckDB — glob views across all series with hive partitioning
# ---------------------------------------------------------------------------

def build_duckdb() -> None:
    markets_glob = str(PROCESSED_BASE.resolve() / "*" / "markets.parquet")
    trades_glob  = str(PROCESSED_BASE.resolve() / "*" / "trades" / "**" / "trades.parquet")

    print(f"\nBuilding DuckDB views → {DB_PATH}")
    t0  = time.time()
    con = duckdb.connect(str(DB_PATH))

    con.execute(f"""
        CREATE OR REPLACE VIEW markets AS
        SELECT * FROM read_parquet('{markets_glob}')
    """)
    print("  markets view ... done")

    con.execute(f"""
        CREATE OR REPLACE VIEW trades AS
        SELECT * FROM read_parquet('{trades_glob}', hive_partitioning=true)
    """)
    print("  trades view ... done")

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
    print("  trades_with_result view ... done")

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
    print("  series_summary view ... done")

    print("  Counting rows...", end=" ", flush=True)
    n_trades  = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    n_markets = con.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    series    = con.execute("SELECT DISTINCT series FROM markets ORDER BY series").df()["series"].tolist()
    con.close()

    print(f"\n  Series  : {series}")
    print(f"  Markets : {n_markets:,}")
    print(f"  Trades  : {n_trades:,}")
    print(f"  Elapsed : {time.time() - t0:.1f}s")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--series",  default=None,
                        help="Target a single series. Omit to process all.")
    parser.add_argument("--rebuild", action="store_true",
                        help="Force rewrite all partitions (default: incremental).")
    args = parser.parse_args()

    if args.series:
        series_list = [args.series]
    else:
        series_list = sorted(
            d.name for d in RAW_BASE.iterdir()
            if d.is_dir() and (d / "markets.jsonl").exists()
        )
        print(f"Found {len(series_list)} series: {series_list}")

    t_total = time.time()
    for series in series_list:
        build_series(series, rebuild=args.rebuild)

    build_duckdb()
    print(f"\nTotal elapsed: {time.time() - t_total:.1f}s")
