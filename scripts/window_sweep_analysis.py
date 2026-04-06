"""
Entry Window Optimization: 2D Sweep (start_ttx × window_duration)
==================================================================
For each active (series, season) config, sweeps over all combinations of
window start TTX and window duration, computing N_trades and EV at each point.

Goal: determine whether the current (24h start, 6h duration → 24h→18h TTX)
is optimal, or whether a different window improves EV and/or trade volume.

Sweep grid:
    start_ttx_hrs  : [21, 24, 27, 30, 33, 36]  (hours before expiry)
    window_dur_hrs : [3, 6, 9, 12, 18]

Baseline reference point: start=24h, duration=6h (~499 trades/year).
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import duckdb
import numpy as np
import pandas as pd
from analysis.strategy_backtest import load_series_trades
from analysis.temperature_strategy import taker_fee, TAKER_FEE_RATE, SEASON_MAP
from strategies.temperature.config import CONFIGS

DB_PATH = Path("data/processed/kalshi.duckdb")

# ---------------------------------------------------------------------------
# Sweep grid
# ---------------------------------------------------------------------------
START_TTX_HRS  = [21, 24, 27, 30, 33, 36]
DURATION_HRS   = [3, 6, 9, 12, 18]

BASELINE_START    = 24
BASELINE_DURATION = 6


# ---------------------------------------------------------------------------
# Core entry simulation (parameterised)
# ---------------------------------------------------------------------------

def find_and_simulate(
    tdf: pd.DataFrame,
    result_yes: bool,
    band_lo: float,
    band_hi: float,
    target: float,
    stop_frac: float,
    start_ttx_sec: float,
    win_hrs: float,
    at_open_only: bool = False,
) -> dict | None:
    """
    Find the first qualifying entry in [start_ttx_sec, start_ttx_sec - win_hrs×3600]
    and simulate outcome. Returns None if no entry found.
    """
    eligible = tdf[tdf["time_to_expiry"] >= start_ttx_sec]
    if len(eligible) == 0:
        return None

    ws     = eligible.iloc[0]["_t"]
    we     = ws + pd.Timedelta(hours=win_hrs)
    open_p = float(eligible.iloc[0]["price"])

    # from_below filter: skip if price below band at window open
    if open_p < band_lo:
        return None

    if at_open_only:
        # Only enter if opening price is directly in the band
        if not (band_lo <= open_p < band_hi):
            return None
        entry_row = eligible.iloc[0]
    else:
        # Look for first in-band trade in the window
        win    = tdf[(tdf["_t"] >= ws) & (tdf["_t"] <= we)]
        in_band = win[(win["price"] >= band_lo) & (win["price"] < band_hi)]
        if len(in_band) == 0:
            return None
        entry_row = in_band.iloc[0]

    ep  = float(entry_row["price"])
    et  = entry_row["_t"]
    sp  = stop_frac * ep

    # Simulate to target/stop/expiry using remaining trades
    after  = tdf[tdf["_t"] > et]["price"].values
    outcome, xp = "expiry", None
    for p in after:
        if p >= target:
            outcome, xp = "target", float(target)
            break
        if p <= sp:
            outcome, xp = "stop", sp
            break

    if xp is None:
        xp = float(target) if result_yes else sp

    fe  = taker_fee(ep, TAKER_FEE_RATE)
    fx  = taker_fee(xp, TAKER_FEE_RATE)
    return {
        "entry_price": ep,
        "outcome":     outcome,
        "net_pnl":     xp - ep - fe - fx,
        "result_yes":  result_yes,
    }


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def run_sweep():
    con = duckdb.connect(str(DB_PATH), read_only=True)

    # Load all series once
    series_list = sorted({s for s, _ in CONFIGS})
    all_trades: dict[str, pd.DataFrame] = {}
    for series in series_list:
        df = load_series_trades(con, series)
        df["created_time"] = pd.to_datetime(df["created_time"])
        df["market_date"]  = pd.to_datetime(df["market_date"])

        # Assign rank
        daily = df.drop_duplicates("ticker")[["ticker", "market_date", "strike"]].copy()
        daily["rank"] = (
            daily.groupby("market_date")["strike"]
            .rank(method="first")
            .astype(int)
        )
        df = df.merge(daily[["ticker", "rank"]], on="ticker")
        all_trades[series] = df

    con.close()

    # -----------------------------------------------------------------------
    # Per-config sweep
    # -----------------------------------------------------------------------
    summary_rows = []

    for (series, season), cfg in sorted(CONFIGS.items()):
        rank         = cfg["rank"]
        band_lo      = cfg["band_lo"]
        band_hi      = cfg["band_hi"]
        target       = cfg["target"]
        stop_frac    = cfg["stop_frac"]
        at_open_only = cfg.get("at_open_only", False)

        df = all_trades[series]

        # Filter to season and rank
        df["_t"]     = df["created_time"].dt.tz_localize(None)
        df["season"] = df["market_date"].dt.month.map(SEASON_MAP)
        season_df = df[(df["season"] == season) & (df["rank"] == rank)].copy()

        if len(season_df) == 0:
            print(f"  WARNING: no data for ({series}, {season}, rank={rank})")
            continue

        # Group by ticker for fast lookup
        by_ticker = {
            ticker: grp.sort_values("created_time").reset_index(drop=True)
            for ticker, grp in season_df.groupby("ticker")
        }

        print(f"\n{'='*60}")
        print(f"{series} / {season}  rank={rank}  band=[{band_lo},{band_hi})  "
              f"target={target}  stop_frac={stop_frac}"
              + ("  [at_open_only]" if at_open_only else ""))
        print(f"{'='*60}")
        print(f"{'start':>6} {'dur':>4} {'N':>5} {'hit%':>6} {'EV':>7} {'totPnL':>9} {'resY%':>6}")
        print("-" * 50)

        for start_h in START_TTX_HRS:
            for dur_h in DURATION_HRS:
                # Skip if window would close before expiry makes sense
                if start_h - dur_h < 0:
                    continue

                start_sec = start_h * 3600
                records = []

                for ticker, tdf in by_ticker.items():
                    result_yes = bool(tdf["result_yes"].iloc[0])
                    res = find_and_simulate(
                        tdf, result_yes,
                        band_lo, band_hi, target, stop_frac,
                        start_sec, dur_h, at_open_only,
                    )
                    if res is not None:
                        records.append(res)

                if len(records) == 0:
                    continue

                trades    = pd.DataFrame(records)
                n         = len(trades)
                hit_rate  = (trades["outcome"] == "target").mean()
                ev        = trades["net_pnl"].mean()
                total_pnl = trades["net_pnl"].sum()
                res_y     = trades["result_yes"].mean()
                baseline  = "*" if (start_h == BASELINE_START and dur_h == BASELINE_DURATION) else " "

                marker = f"{baseline}"
                print(f"{start_h:>5}h {dur_h:>3}h {n:>5} {hit_rate:>6.1%} {ev:>7.2f} {total_pnl:>9.1f} {res_y:>6.1%} {marker}")

                summary_rows.append({
                    "series":    series,
                    "season":    season,
                    "rank":      rank,
                    "start_h":   start_h,
                    "dur_h":     dur_h,
                    "n":         n,
                    "hit_rate":  hit_rate,
                    "ev":        ev,
                    "total_pnl": total_pnl,
                    "res_y":     res_y,
                    "baseline":  (start_h == BASELINE_START and dur_h == BASELINE_DURATION),
                })

    # -----------------------------------------------------------------------
    # Aggregate summary across all configs
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("AGGREGATE: N_trades and total PnL across all configs")
    print(f"{'='*60}")

    agg = pd.DataFrame(summary_rows)
    agg_grouped = (
        agg.groupby(["start_h", "dur_h"])
        .agg(
            total_n=("n", "sum"),
            mean_ev=("ev", "mean"),
            total_pnl=("total_pnl", "sum"),
        )
        .reset_index()
        .sort_values(["start_h", "dur_h"])
    )

    baseline_n = agg_grouped[
        (agg_grouped["start_h"] == BASELINE_START) &
        (agg_grouped["dur_h"]   == BASELINE_DURATION)
    ]["total_n"].iloc[0] if len(agg_grouped) > 0 else 1

    print(f"\n{'start':>6} {'dur':>4} {'N_trades':>9} {'vs_base':>8} {'mean_EV':>8} {'total_PnL':>11}")
    print("-" * 55)
    for _, row in agg_grouped.iterrows():
        marker = " *" if (row["start_h"] == BASELINE_START and row["dur_h"] == BASELINE_DURATION) else ""
        delta  = row["total_n"] - baseline_n
        print(f"{row['start_h']:>5}h {row['dur_h']:>3}h {row['total_n']:>9.0f} "
              f"{delta:>+8.0f} {row['mean_ev']:>8.3f} {row['total_pnl']:>11.1f}{marker}")

    # -----------------------------------------------------------------------
    # Best per-config (by EV, minimum N >= 80% of baseline)
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("BEST WINDOW per config (max EV, N >= 80% of baseline N)")
    print(f"{'='*60}")

    for (series, season), cfg in sorted(CONFIGS.items()):
        sub = agg[
            (agg["series"] == series) &
            (agg["season"] == season)
        ].copy()
        if len(sub) == 0:
            continue

        base_n = sub[sub["baseline"]]["n"].values
        if len(base_n) == 0:
            continue
        min_n = 0.80 * base_n[0]
        base_ev = sub[sub["baseline"]]["ev"].values[0]

        eligible = sub[sub["n"] >= min_n]
        if len(eligible) == 0:
            continue
        best = eligible.loc[eligible["ev"].idxmax()]

        improvement = best["ev"] - base_ev
        print(f"\n{series}/{season} (rank={cfg['rank']}):")
        print(f"  Baseline (24h/6h):  N={base_n[0]:.0f}  EV={base_ev:.2f}¢")
        print(f"  Best window:        start={best['start_h']:.0f}h  dur={best['dur_h']:.0f}h  "
              f"N={best['n']:.0f}  EV={best['ev']:.2f}¢  (Δ={improvement:+.2f}¢)")

    print("\n--- DONE ---\n")


if __name__ == "__main__":
    run_sweep()
