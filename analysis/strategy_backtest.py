"""
Backtest of the opening-price directional strategy.

Strategy:
    Day before expiry (TTX > 24h), observe the opening price E of the 2-3
    brackets closest to the NWS forecast (proxied by brackets whose opening
    price is nearest to 50c and within [entry_min, entry_max]).

    Entry : buy at E
    Target: sell at T (sweep T from E+5 to 95)
    Stop  : sell at S = 0.5 * E

    P&L per contract (cents, before fees):
        +( T - E )     if price reaches T before S
        -( 0.5 * E )   if price reaches S before T
        +( final - E ) if neither triggered before expiry (rare for binary)

    Fee applied on both entry and exit legs (taker fee per contract in cents).

All prices in cents (0-99).
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import date

import duckdb
import numpy as np
import pandas as pd

DB_PATH = Path("data/processed/kalshi.duckdb")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_series_trades(con: duckdb.DuckDBPyConnection, series: str) -> pd.DataFrame:
    return con.execute(f"""
        SELECT
            t.ticker,
            t.market_date,
            t.bracket_type,
            t.strike,
            t.created_time,
            t.yes_price   AS price,
            t.count,
            t.time_to_expiry,
            m.result_yes,
            m.result
        FROM trades t
        JOIN markets m USING (ticker)
        WHERE t.series = '{series}'
        ORDER BY t.ticker, t.created_time
    """).df()


# ---------------------------------------------------------------------------
# Entry identification
# ---------------------------------------------------------------------------

def find_entries(df: pd.DataFrame, ttx_min: float = 86400) -> pd.DataFrame:
    """First trade per market with TTX > ttx_min."""
    eligible = df[df["time_to_expiry"] > ttx_min].copy()
    return (
        eligible.sort_values("created_time")
        .groupby("ticker")
        .first()
        .reset_index()
        [["ticker", "market_date", "bracket_type", "strike",
          "created_time", "price", "time_to_expiry", "result_yes", "result"]]
        .rename(columns={"created_time": "entry_time", "price": "entry_price",
                         "time_to_expiry": "entry_ttx"})
    )


def select_atm_markets(
    entries: pd.DataFrame,
    n: int = 3,
    entry_min: float = 20.0,
    entry_max: float = 70.0,
) -> pd.DataFrame:
    """
    For each market date, select the N brackets whose entry price is
    closest to 50c, restricted to [entry_min, entry_max].

    The price window excludes deep OTM markets that are misclassified
    as ATM simply because all brackets on a given day happen to be cheap.
    """
    filtered = entries[
        (entries["entry_price"] >= entry_min) &
        (entries["entry_price"] <= entry_max)
    ].copy()
    filtered["dist_from_50"] = (filtered["entry_price"] - 50).abs()
    return (
        filtered.sort_values("dist_from_50")
        .groupby("market_date")
        .head(n)
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def taker_fee(price_cents: float, rate: float = 0.07) -> float:
    """
    Kalshi fee per contract in cents.
    Formula: rate × P × (1-P), where P = price in dollars.
    rate=0.07  → standard taker fee (max 1.75¢ at 50¢)
    rate=0.0175 → maker fee (max 0.4375¢ at 50¢)
    rate=0.0   → free (maker on markets without maker fees)
    """
    p = price_cents / 100.0
    return rate * p * (1.0 - p) * 100.0   # result in cents


def simulate_trade(
    ticker_trades: pd.DataFrame,
    entry_price: float,
    entry_time: pd.Timestamp,
    target: float,
    stop: float,
    entry_fee_rate: float = 0.07,
    exit_fee_rate: float = 0.07,
) -> dict:
    """
    Simulate a single position from entry_time onward.

    Fees are price-dependent per the Kalshi fee schedule (Feb 2026):
        fee_per_contract = rate × P × (1-P)   [P in dollars, result in cents]
    entry_fee_rate: 0.07 (taker) or 0.0175 (maker) for the entry leg
    exit_fee_rate:  same options for the exit leg
    """
    after_entry = ticker_trades[ticker_trades["created_time"] > entry_time]

    outcome    = "expiry"
    exit_price = None

    for _, row in after_entry.iterrows():
        p = row["price"]
        if p >= target:
            outcome    = "target"
            exit_price = target
            break
        if p <= stop:
            outcome    = "stop"
            exit_price = stop
            break

    if exit_price is None:
        exit_price = after_entry.iloc[-1]["price"] if len(after_entry) > 0 else entry_price

    gross_pnl   = exit_price - entry_price
    fee_entry   = taker_fee(entry_price, entry_fee_rate)
    fee_exit    = taker_fee(exit_price,  exit_fee_rate)
    total_fee   = fee_entry + fee_exit
    net_pnl     = gross_pnl - total_fee

    return {
        "outcome":    outcome,
        "exit_price": exit_price,
        "gross_pnl":  gross_pnl,
        "total_fee":  total_fee,
        "net_pnl":    net_pnl,
    }


# ---------------------------------------------------------------------------
# Full backtest sweep
# ---------------------------------------------------------------------------

def run_backtest(
    series: str = "KXHIGHNY",
    n_atm: int = 3,
    entry_ttx_min: float = 86400,
    entry_min: float = 20.0,
    entry_max: float = 70.0,
    target_prices: list[float] | None = None,
    entry_fee_rate: float = 0.07,    # taker=0.07, maker=0.0175, free=0.0
    exit_fee_rate: float = 0.07,     # taker=0.07, maker=0.0175, free=0.0
    train_cutoff: date | None = None,
) -> dict:
    """
    Returns:
        results     : per-trade results for every (ticker, target) combo
        ev_surface  : EV by (entry_price_bucket, target, split)
        entries     : one row per market in the universe
        train_cutoff: the cutoff date used

    Fee model (Kalshi fee schedule, Feb 2026):
        fee_per_contract = rate × P × (1-P)  [P in dollars, cents output]
        entry_fee_rate=0.07  → taker entry (aggressive/market order)
        exit_fee_rate=0.07   → taker exit
        exit_fee_rate=0.0    → maker exit (resting limit at target/stop; free if
                               KXHIGHNY not on maker-fee list)
        entry_fee_rate=0.0175 → maker entry (resting limit filled)
    """
    if target_prices is None:
        target_prices = list(range(35, 96, 5))

    con = duckdb.connect(str(DB_PATH), read_only=True)
    all_trades = load_series_trades(con, series)
    con.close()

    entries = find_entries(all_trades, ttx_min=entry_ttx_min)
    entries = select_atm_markets(entries, n=n_atm,
                                 entry_min=entry_min, entry_max=entry_max)

    # Train/test split — default 60/40 by calendar day
    if train_cutoff is None:
        dates = sorted(entries["market_date"].unique())
        cutoff_idx = int(len(dates) * 0.60)
        train_cutoff = dates[cutoff_idx]

    entries["split"] = np.where(entries["market_date"] < train_cutoff, "train", "test")

    print(f"Universe      : {len(entries)} markets")
    print(f"Entry range   : {entries['entry_price'].min():.0f}¢ – "
          f"{entries['entry_price'].max():.0f}¢  "
          f"(mean {entries['entry_price'].mean():.1f}¢)")
    print(f"Train         : {(entries['split']=='train').sum()} markets  "
          f"(before {train_cutoff})")
    print(f"Test          : {(entries['split']=='test').sum()} markets  "
          f"(from {train_cutoff})")
    ex = taker_fee(50.0, entry_fee_rate)
    xx = taker_fee(50.0, exit_fee_rate)
    print(f"Fee model     : entry_rate={entry_fee_rate} ({ex:.3f}¢ at 50¢), "
          f"exit_rate={exit_fee_rate} ({xx:.3f}¢ at 50¢) — round-trip at 50¢: {ex+xx:.3f}¢")
    print(f"Targets       : {target_prices}")

    trades_by_ticker = {
        ticker: grp.sort_values("created_time")
        for ticker, grp in all_trades.groupby("ticker")
    }

    records = []
    for _, entry in entries.iterrows():
        ticker      = entry["ticker"]
        E           = entry["entry_price"]
        stop        = E * 0.5
        entry_time  = entry["entry_time"]
        ticker_df   = trades_by_ticker.get(ticker, pd.DataFrame())

        for target in target_prices:
            if target <= E:
                continue

            res = simulate_trade(ticker_df, E, entry_time, target, stop,
                                 entry_fee_rate, exit_fee_rate)
            records.append({
                "ticker":        ticker,
                "market_date":   entry["market_date"],
                "bracket_type":  entry["bracket_type"],
                "strike":        entry["strike"],
                "entry_price":   E,
                "stop":          stop,
                "target":        target,
                "split":         entry["split"],
                "outcome":       res["outcome"],
                "exit_price":    res["exit_price"],
                "gross_pnl":     res["gross_pnl"],
                "total_fee":     res["total_fee"],
                "net_pnl":       res["net_pnl"],
                "result_yes":    entry["result_yes"],
            })

    results = pd.DataFrame(records)

    results["entry_bucket"] = pd.cut(
        results["entry_price"],
        bins=[20, 30, 40, 50, 60, 70],
        labels=["20-30", "30-40", "40-50", "50-60", "60-70"],
        include_lowest=True,
    )

    def agg_group(g):
        return pd.Series({
            "n":          len(g),
            "gross_ev":   g["gross_pnl"].mean(),
            "net_ev":     g["net_pnl"].mean(),
            "hit_rate":   (g["outcome"] == "target").mean(),
            "stop_rate":  (g["outcome"] == "stop").mean(),
            "expiry_rate":(g["outcome"] == "expiry").mean(),
        })

    ev_surface = (
        results.groupby(["split", "entry_bucket", "target"], observed=True)
        .apply(agg_group)
        .reset_index()
    )

    return {
        "results":       results,
        "ev_surface":    ev_surface,
        "entries":       entries,
        "train_cutoff":  train_cutoff,
        "entry_fee_rate": entry_fee_rate,
        "exit_fee_rate":  exit_fee_rate,
    }
