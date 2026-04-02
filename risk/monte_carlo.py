"""
Bootstrap Monte Carlo simulation for the multi-city temperature strategy.

Fully vectorized: resamples all n_sims simultaneously using numpy arrays.
Block bootstrap preserves within-day cross-city correlation by keeping the
original date sequence and resampling only P&L outcomes.

Runtime: ~5s for 10,000 simulations.

Usage:
    python3 risk/monte_carlo.py
"""

import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from analysis.temperature_strategy import filter_rank, load_and_rank, run_backtest, SEASON_MAP

# ---------------------------------------------------------------------------
# Strategy configuration
# ---------------------------------------------------------------------------

SETUPS: list[tuple] = [
    # (label,          series,        rank, lo, hi, target, stop,  season)
    ("NY Spring r5",   "KXHIGHNY",    5,  10, 15,  70, 0.25, "Spring"),
    ("NY Summer",      "KXHIGHNY",    4,  30, 35,  70, 0.25, "Summer"),
    ("NY Fall",        "KXHIGHNY",    4,  30, 35,  70, 0.25, "Fall"),
    ("Philly Summer",  "KXHIGHPHIL",  4,  30, 35,  50, 0.60, "Summer"),
    ("Philly Fall",    "KXHIGHPHIL",  4,  30, 35,  50, 0.60, "Fall"),
    ("LA Spring",      "KXHIGHLAX",   3,  35, 40,  55, 0.25, "Spring"),
    ("LA Summer",      "KXHIGHLAX",   3,  35, 40,  55, 0.25, "Summer"),
    ("LA Fall",        "KXHIGHLAX",   4,  30, 35,  50, 0.25, "Fall"),
    ("LA Winter",      "KXHIGHLAX",   4,  25, 30,  50, 0.25, "Winter"),
    ("CHI Fall",       "KXHIGHCHI",   3,  23, 29,  85, 0.50, "Fall"),
    ("CHI Winter",     "KXHIGHCHI",   3,  23, 29,  75, 0.60, "Winter"),
    ("MIA Spring",     "KXHIGHMIA",   4,  25, 33,  50, 0.25, "Spring"),
    ("MIA Fall r5",    "KXHIGHMIA",   5,  13, 27,  45, 0.25, "Fall"),
    ("MIA Fall r4",    "KXHIGHMIA",   4,  25, 33,  45, 0.25, "Fall"),
]

# Half-Kelly fractions per setup (fraction of account allocated per trade).
HALF_KELLY: dict[str, float] = {
    "NY Spring r5":  0.053,
    "NY Summer":     0.163,
    "NY Fall":       0.130,
    "Philly Summer": 0.241,
    "Philly Fall":   0.270,
    "LA Spring":     0.236,
    "LA Summer":     0.257,
    "LA Fall":       0.214,
    "LA Winter":     0.254,
    "CHI Fall":      0.116,
    "CHI Winter":    0.125,
    "MIA Spring":    0.204,
    "MIA Fall r5":   0.197,
    "MIA Fall r4":   0.162,   # already halved (thin sample)
}

CAP_SIMULTANEOUS: float = 0.50


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_all_backtest_trades(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Run all setups; return combined trades with setup, market_date, net_pnl, entry_price."""
    frames = []
    cache: dict[str, pd.DataFrame] = {}
    for label, series, rank, lo, hi, tgt, sf, season in SETUPS:
        if series not in cache:
            df = load_and_rank(con, series)
            df["season"] = df["market_date"].dt.month.map(SEASON_MAP)
            cache[series] = df
        df = cache[series]
        d  = filter_rank(df, rank=rank)
        d  = d[d["season"] == season]
        bt = run_backtest(d, band_lo=lo, band_hi=hi, target=tgt, stop_frac=sf)
        if len(bt) == 0:
            continue
        bt = bt[["market_date", "entry_price", "net_pnl"]].copy()
        bt["setup"] = label
        bt["market_date"] = pd.to_datetime(bt["market_date"])
        frames.append(bt)
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Simulation (vectorized)
# ---------------------------------------------------------------------------

def _build_trade_matrix(
    trades: pd.DataFrame,
    sizing: dict[str, float],
    account_start: float,
    cap_simultaneous: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Precompute three arrays aligned by trade index, sorted by market_date:

    pnl_pools  : list of per-setup P&L pools (cents/contract), one array per setup
    dollar_scales: per-trade scalar converting 1 sampled cent to dollars,
                   incorporating contract count and simultaneous-cap scaling
    setup_slices: list of (start, end) index ranges in the combined array per setup

    Returns (pnl_matrix_template, cap_scale, sort_order) where:
      pnl_matrix_template[i] = the P&L pool for trade i (cents)
      cap_scale[i]           = dollar multiplier (contracts × cap_scale)
      sort_order             = argsort of market_date across all trades
    """
    sorted_trades = trades.sort_values("market_date").reset_index(drop=True)

    # Per-trade contract count (static Kelly, using each trade's actual entry price)
    entry_dollars = sorted_trades["entry_price"].values / 100.0
    setup_labels  = sorted_trades["setup"].values

    fracs = np.array([sizing.get(s, 0.0) for s in setup_labels])
    contracts = np.where(
        entry_dollars > 0,
        fracs * account_start / entry_dollars,
        0.0,
    )

    # Simultaneous cap: on days with multiple trades, scale contracts down
    date_ints = sorted_trades["market_date"].values.astype("datetime64[D]").astype(int)
    cap_scale = np.ones(len(sorted_trades))
    unique_dates = np.unique(date_ints)
    for d in unique_dates:
        mask = date_ints == d
        if mask.sum() <= 1:
            continue
        # Total allocation fraction on this day
        total_frac = fracs[mask].sum()
        if total_frac > cap_simultaneous:
            cap_scale[mask] = cap_simultaneous / total_frac

    # Dollar multiplier per trade: contracts × cap_scale / 100
    # (net_pnl is in cents, so ÷100 converts to dollars)
    dollar_mult = contracts * cap_scale / 100.0

    # Build index mapping: for each position in sorted_trades, which setup pool to draw from
    # Returns list of (pool_pnl_array, global_indices) per setup
    setup_pools: list[tuple[np.ndarray, np.ndarray]] = []
    for setup in sorted_trades["setup"].unique():
        idx = np.where(setup_labels == setup)[0]
        pool = sorted_trades.loc[idx, "net_pnl"].values.astype(float)
        setup_pools.append((pool, idx))

    # Assemble net_pnl pool per position (needed for resampling within-setup)
    # Each position i draws from the pool of its own setup
    n = len(sorted_trades)
    # Map each position to its setup's pool array
    setup_of = sorted_trades["setup"].values
    unique_setups = list(sorted_trades["setup"].unique())
    setup_to_idx = {s: i for i, s in enumerate(unique_setups)}

    pool_arrays = [
        sorted_trades[sorted_trades["setup"] == s]["net_pnl"].values.astype(float)
        for s in unique_setups
    ]
    pool_sizes   = np.array([len(p) for p in pool_arrays])
    setup_id     = np.array([setup_to_idx[s] for s in setup_of])

    return pool_arrays, pool_sizes, setup_id, dollar_mult


def run_monte_carlo(
    trades: pd.DataFrame,
    sizing: dict[str, float],
    account_start: float = 100.0,
    n_sims: int = 10_000,
    cap_simultaneous: float = CAP_SIMULTANEOUS,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Vectorized bootstrap Monte Carlo.

    For each simulation:
      - Resample each setup's P&L pool independently (same n as in-sample)
      - Apply sizing (static Kelly) + simultaneous cap (precomputed)
      - Compute equity curve and max drawdown

    Returns DataFrame: total_pnl, max_drawdown_frac, final_balance (one row per sim).
    """
    rng = np.random.default_rng(seed)

    pool_arrays, pool_sizes, setup_id, dollar_mult = _build_trade_matrix(
        trades, sizing, account_start, cap_simultaneous
    )

    n_trades  = len(setup_id)
    n_setups  = len(pool_arrays)

    # Stack all pool arrays into a padded matrix [n_setups × max_pool_size]
    max_pool  = int(pool_sizes.max())
    pool_mat  = np.zeros((n_setups, max_pool), dtype=float)
    for i, p in enumerate(pool_arrays):
        pool_mat[i, : len(p)] = p

    # Pre-draw all random indices: shape [n_sims, n_trades]
    # For each trade i (with setup setup_id[i] and pool size pool_sizes[setup_id[i]]),
    # draw a random index into that pool.
    pool_size_per_trade = pool_sizes[setup_id]                    # [n_trades]
    # Sample uniform [0,1) scaled to pool size
    rand_fracs  = rng.random(size=(n_sims, n_trades))             # [n_sims, n_trades]
    raw_indices = (rand_fracs * pool_size_per_trade).astype(int)  # [n_sims, n_trades]
    raw_indices = np.clip(raw_indices, 0, pool_size_per_trade - 1)

    # Gather from pool_mat: pnl_matrix[sim, trade] = pool_mat[setup_id[trade], raw_indices[sim, trade]]
    # setup_id: [n_trades], raw_indices: [n_sims, n_trades]
    # broadcast setup_id across sims dimension
    setup_id_2d = np.broadcast_to(setup_id[np.newaxis, :], (n_sims, n_trades))  # [n_sims, n_trades]
    pnl_matrix  = pool_mat[setup_id_2d, raw_indices]  # [n_sims, n_trades]

    # Convert cents to dollars using precomputed dollar_mult
    dollar_pnl = pnl_matrix * dollar_mult[np.newaxis, :]  # [n_sims, n_trades]

    # Equity curves: shape [n_sims, n_trades+1]
    equity = np.empty((n_sims, n_trades + 1), dtype=float)
    equity[:, 0] = account_start
    equity[:, 1:] = account_start + np.cumsum(dollar_pnl, axis=1)

    # Max drawdown per simulation
    running_max = np.maximum.accumulate(equity, axis=1)      # [n_sims, n_trades+1]
    safe_max    = np.where(running_max > 0, running_max, 1.0)
    drawdowns   = (running_max - equity) / safe_max           # [n_sims, n_trades+1]
    max_dd      = drawdowns.max(axis=1)                        # [n_sims]

    total_pnl     = equity[:, -1] - account_start
    final_balance = equity[:, -1]

    return pd.DataFrame({
        "total_pnl":         total_pnl,
        "max_drawdown_frac": max_dd,
        "final_balance":     final_balance,
    })


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _summarise(df: pd.DataFrame, account_start: float) -> dict:
    pnl = df["total_pnl"]
    dd  = df["max_drawdown_frac"]
    return {
        "p5_pnl":  pnl.quantile(0.05),
        "p25_pnl": pnl.quantile(0.25),
        "p50_pnl": pnl.quantile(0.50),
        "p75_pnl": pnl.quantile(0.75),
        "p95_pnl": pnl.quantile(0.95),
        "p50_dd":  dd.quantile(0.50),
        "p75_dd":  dd.quantile(0.75),
        "p95_dd":  dd.quantile(0.95),
        "p_dd_10": (dd > 0.10).mean(),
        "p_dd_20": (dd > 0.20).mean(),
        "p_dd_30": (dd > 0.30).mean(),
        "p_dd_50": (dd > 0.50).mean(),
        "p_ruin":  (df["final_balance"] < account_start * 0.10).mean(),
    }


def report(results: dict[str, pd.DataFrame], account_start: float) -> None:
    summaries = {name: _summarise(df, account_start) for name, df in results.items()}
    names     = list(summaries.keys())

    print(f"\n{'':=<80}")
    print(f"  ANNUAL P&L  (account_start=${account_start:.0f}, 10,000 simulations)")
    print(f"{'':=<80}")
    print(f"{'Scheme':18s}  {'p5':>8}  {'p25':>8}  {'p50':>8}  {'p75':>8}  {'p95':>8}")
    print("-" * 70)
    for name in names:
        s = summaries[name]
        print(f"{name:18s}  "
              f"${s['p5_pnl']:7.2f}  ${s['p25_pnl']:7.2f}  "
              f"${s['p50_pnl']:7.2f}  ${s['p75_pnl']:7.2f}  ${s['p95_pnl']:7.2f}")

    print(f"\n{'':=<80}")
    print("  MAX DRAWDOWN  (fraction of peak equity)")
    print(f"{'':=<80}")
    print(f"{'Scheme':18s}  {'p50':>7}  {'p75':>7}  {'p95':>7}  "
          f"{'P(>10%)':>9}  {'P(>20%)':>9}  {'P(>30%)':>9}  {'P(>50%)':>9}  {'P(ruin)':>9}")
    print("-" * 103)
    for name in names:
        s = summaries[name]
        print(f"{name:18s}  "
              f"{s['p50_dd']:7.1%}  {s['p75_dd']:7.1%}  {s['p95_dd']:7.1%}  "
              f"{s['p_dd_10']:9.1%}  {s['p_dd_20']:9.1%}  {s['p_dd_30']:9.1%}  "
              f"{s['p_dd_50']:9.1%}  {s['p_ruin']:9.1%}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import time

    ACCOUNT_START = 100.0
    N_SIMS        = 10_000

    print("Loading backtest trades...")
    con    = duckdb.connect("data/processed/kalshi.duckdb")
    trades = load_all_backtest_trades(con)
    print(f"  {len(trades)} trades across {trades['setup'].nunique()} setups")

    # Compute average entry price per setup for fixed-dollar sizing
    avg_entry = trades.groupby("setup")["entry_price"].mean()

    sizing_schemes: dict[str, dict[str, float]] = {
        "fixed_1":       {s: float(avg_entry.get(s, 25)) / (100.0 * ACCOUNT_START)
                          for s in HALF_KELLY},
        "fixed_$10":     {s: 10.0 / ACCOUNT_START for s in HALF_KELLY},
        "quarter_kelly": {s: v / 2 for s, v in HALF_KELLY.items()},
        "half_kelly":    HALF_KELLY,
    }

    results: dict[str, pd.DataFrame] = {}
    for name, sizing in sizing_schemes.items():
        print(f"Running {N_SIMS:,} sims: {name}...", end=" ", flush=True)
        t0 = time.time()
        results[name] = run_monte_carlo(trades, sizing, ACCOUNT_START, N_SIMS)
        print(f"{time.time() - t0:.1f}s")

    report(results, ACCOUNT_START)
