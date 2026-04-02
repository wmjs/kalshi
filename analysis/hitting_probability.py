"""
Hitting probability surface and patient-entry backtest for Kalshi binary markets.

Core idea:
    Rather than always entering at the opening print, estimate P(price reaches target T
    | currently at price E) from historical data.  Compute EV(E, T) net of fees.
    Only enter during the entry window when a price E appears where EV > 0.

Entry window:
    First trade with TTX > entry_ttx_min (default 24h) defines window_start.
    Watch for entry_window_hours (default 6h) from window_start.
    If no EV-positive entry appears: sit out for the day.

Hitting probability (unconditional):
    For each (entry_bin, target) pair, aggregate across all historical markets:
        - Find first occurrence of a price in entry_bin during the entry window.
        - Simulate: does subsequent price action reach target before stop?
    No conditioning on TTX within the window or any other variable.

Note: to condition on TTX at entry time, pass the trades DataFrame filtered to
    the desired TTX range before calling build_hit_surface().
"""

import numpy as np
import pandas as pd
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BIN_EDGES  = list(range(10, 91, 10))   # [10, 20, ..., 90]
BIN_LABELS = [f"{lo}-{hi}" for lo, hi in zip(BIN_EDGES[:-1], BIN_EDGES[1:])]
BIN_MIDS   = [(lo + hi) / 2 for lo, hi in zip(BIN_EDGES[:-1], BIN_EDGES[1:])]

# Map label → midpoint for convenience
LABEL_TO_MID = dict(zip(BIN_LABELS, BIN_MIDS))


def _bin_price(price: float, edges=BIN_EDGES, labels=BIN_LABELS) -> str | None:
    """Return the label of the bin containing price, or None if out of range."""
    for lo, hi, label in zip(edges[:-1], edges[1:], labels):
        if lo <= price < hi:
            return label
    return None


def _taker_fee(price_cents: float, rate: float) -> float:
    """Fee per contract in cents: rate × P × (1-P), P in dollars."""
    p = price_cents / 100.0
    return rate * p * (1.0 - p) * 100.0


def _simulate_multi_target(
    after_prices: np.ndarray,
    targets: list[float],
    stop: float,
    last_price: float,
    result_yes: bool | None = None,
) -> dict[float, tuple[str, float]]:
    """
    Single-pass multi-target outcome simulation.

    For each target in `targets`, determines the outcome given a sequence of
    prices that occur after the entry.  Targets and stop are checked in order
    of appearance in the price sequence:

        - If price >= target: outcome = "target", exit_price = target
        - If price <= stop:   outcome = "stop",   exit_price = stop
                              (all remaining targets also resolve as "stop")
        - If neither before end: resolved via settlement (result_yes):
              result_yes=True  → "target", exit_price = target
              result_yes=False → "stop",   exit_price = stop
              result_yes=None  → "expiry", exit_price = last_price  (fallback)

    Binary markets always settle to 0 or 100, so result_yes should always be
    provided.  The None fallback exists only for testing with incomplete data.

    Returns dict mapping target → (outcome, exit_price).
    """
    outcomes: dict[float, tuple[str, float]] = {}
    pending = list(targets)      # targets not yet resolved

    for p in after_prices:
        if not pending:
            break
        # Stop check: if stop is hit, all remaining targets resolve as stop
        if p <= stop:
            for t in pending:
                outcomes[t] = ("stop", stop)
            pending = []
            break
        # Target checks: any target <= current price is hit
        still_pending = []
        for t in pending:
            if p >= t:
                outcomes[t] = ("target", float(t))
            else:
                still_pending.append(t)
        pending = still_pending

    # Resolve remaining via settlement — binary markets always go to 0 or 100
    for t in pending:
        if result_yes is True:
            outcomes[t] = ("target", float(t))
        elif result_yes is False:
            outcomes[t] = ("stop", stop)
        else:
            outcomes[t] = ("expiry", last_price)

    return outcomes


# ---------------------------------------------------------------------------
# 1. Build hitting probability surface
# ---------------------------------------------------------------------------

def build_hit_surface(
    all_trades: pd.DataFrame,
    entry_ttx_min: float = 86400,
    entry_window_hours: float = 6.0,
    stop_frac: float = 0.5,
    target_prices: list[float] | None = None,
    bin_edges: list[int] = BIN_EDGES,
    bin_labels: list[str] = BIN_LABELS,
    bin_mids: list[float] = BIN_MIDS,
) -> pd.DataFrame:
    """
    Estimate P(price hits target T before stop | entered at entry_bin) from
    historical trade data.

    Parameters
    ----------
    all_trades : DataFrame with columns [ticker, created_time, price,
                 time_to_expiry].  Loaded via load_series_trades() or equivalent.
    entry_ttx_min : seconds; entry window starts at first trade with TTX >= this.
    entry_window_hours : hours to watch for entries after window_start.
    stop_frac : stop level = stop_frac × entry_price (default 0.5).
    target_prices : list of target prices in cents.
    bin_edges / bin_labels / bin_mids : price binning scheme.

    Returns
    -------
    DataFrame with columns:
        entry_bin, entry_mid, target, n, hit_rate, stop_rate, expiry_rate, gross_ev
    """
    if target_prices is None:
        target_prices = list(range(35, 96, 5))

    label_to_mid = dict(zip(bin_labels, bin_mids))

    records = []

    for ticker, ticker_df in all_trades.groupby("ticker"):
        ticker_df = ticker_df.sort_values("created_time")

        # Entry window: first trade with TTX >= entry_ttx_min
        eligible = ticker_df[ticker_df["time_to_expiry"] >= entry_ttx_min]
        if len(eligible) == 0:
            continue

        window_start = eligible.iloc[0]["created_time"]
        window_end   = window_start + pd.Timedelta(hours=entry_window_hours)

        window_trades = ticker_df[
            (ticker_df["created_time"] >= window_start) &
            (ticker_df["created_time"] <= window_end)
        ]
        if len(window_trades) == 0:
            continue

        # After-entry price array for quick simulation
        all_prices   = ticker_df["price"].values
        all_times    = ticker_df["created_time"].dt.tz_localize(None).values
        last_price_overall = all_prices[-1]
        result_yes   = bool(ticker_df["result_yes"].iloc[0])

        # For each bin, find the first trade in the window that falls in this bin
        for label, mid in zip(bin_labels, bin_mids):
            lo = bin_edges[bin_labels.index(label)]
            hi = bin_edges[bin_labels.index(label) + 1]

            in_bin = window_trades[
                (window_trades["price"] >= lo) &
                (window_trades["price"] <  hi)
            ]
            if len(in_bin) == 0:
                continue

            entry_trade  = in_bin.iloc[0]
            entry_time   = entry_trade["created_time"].tz_localize(None) if hasattr(entry_trade["created_time"], "tz_localize") else entry_trade["created_time"].replace(tzinfo=None)
            entry_price  = entry_trade["price"]   # actual price, not midpoint
            stop         = stop_frac * entry_price

            # All prices after entry time
            after_mask   = all_times > entry_time
            after_prices = all_prices[after_mask]
            last_price   = after_prices[-1] if len(after_prices) > 0 else entry_price

            # Only simulate targets above the entry price
            valid_targets = [t for t in target_prices if t > entry_price]
            if not valid_targets:
                continue

            outcomes = _simulate_multi_target(after_prices, valid_targets, stop, last_price,
                                              result_yes=result_yes)

            for t, (outcome, exit_price) in outcomes.items():
                records.append({
                    "ticker":     ticker,
                    "entry_bin":  label,
                    "entry_mid":  mid,
                    "entry_price": entry_price,
                    "target":     t,
                    "outcome":    outcome,
                    "exit_price": exit_price,
                    "gross_pnl":  exit_price - entry_price,
                })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    surface = (
        df.groupby(["entry_bin", "entry_mid", "target"], observed=True)
        .apply(lambda g: pd.Series({
            "n":           len(g),
            "hit_rate":    (g["outcome"] == "target").mean(),
            "stop_rate":   (g["outcome"] == "stop").mean(),
            "expiry_rate": (g["outcome"] == "expiry").mean(),
            "gross_ev":    g["gross_pnl"].mean(),
        }), include_groups=False)
        .reset_index()
    )

    # Preserve bin ordering
    surface["entry_bin"] = pd.Categorical(
        surface["entry_bin"], categories=bin_labels, ordered=True
    )
    surface = surface.sort_values(["entry_bin", "target"])

    return surface


# ---------------------------------------------------------------------------
# 2. EV surface
# ---------------------------------------------------------------------------

def compute_ev_surface(
    hit_surface: pd.DataFrame,
    entry_fee_rate: float = 0.07,
    exit_fee_rate: float = 0.07,
    stop_frac: float = 0.5,
) -> pd.DataFrame:
    """
    Add net_ev column to hit_surface using the Kalshi fee formula:
        fee = rate × P × (1-P)   [P in dollars, result in cents]

    EV = hit_rate  × (T - E - fee_entry - fee_exit(T))
       + stop_rate × (-stop_frac×E - fee_entry - fee_exit(stop))
       + expiry_rate × (0 - fee_entry - fee_exit(E))

    Where E = entry_mid (bin midpoint), stop = stop_frac × E.
    """
    df   = hit_surface.copy()
    E    = df["entry_mid"].astype(float)
    T    = df["target"].astype(float)
    stop = E * stop_frac

    fee_entry       = _taker_fee(E, entry_fee_rate)
    fee_exit_hit    = _taker_fee(T, exit_fee_rate)
    fee_exit_stop   = _taker_fee(stop, exit_fee_rate)
    fee_exit_expiry = _taker_fee(E, exit_fee_rate)   # rough: assume exits near entry

    df["net_ev"] = (
        df["hit_rate"]    * (T - E    - fee_exit_hit)
        + df["stop_rate"] * (-stop_frac * E - fee_exit_stop)
        + df["expiry_rate"] * (0 - fee_exit_expiry)
        - fee_entry
    )
    df["entry_fee_rate"] = entry_fee_rate
    df["exit_fee_rate"]  = exit_fee_rate

    return df


# ---------------------------------------------------------------------------
# 3. Patient entry backtest
# ---------------------------------------------------------------------------

def simulate_patient_backtest(
    all_trades: pd.DataFrame,
    ev_surface: pd.DataFrame,
    train_cutoff,
    entry_ttx_min: float = 86400,
    entry_window_hours: float = 6.0,
    stop_frac: float = 0.5,
    min_ev: float = 0.0,
    entry_fee_rate: float = 0.07,
    exit_fee_rate: float = 0.07,
    bin_edges: list[int] = BIN_EDGES,
    bin_labels: list[str] = BIN_LABELS,
    bin_mids: list[float] = BIN_MIDS,
) -> pd.DataFrame:
    """
    Simulate the patient entry strategy on test markets.

    For each market with market_date >= train_cutoff:
        1. Identify entry window (first 6h from first TTX >= 24h trade).
        2. Scan trades in window sequentially.
        3. For each trade at price E (binned):
             - Look up best target T* = argmax_T net_ev(bin, T) s.t. net_ev > min_ev
             - If T* exists: enter at E, target T*, stop = stop_frac × E
             - Simulate outcome from entry point onward; record and stop.
        4. If no entry found: record as "no_entry".

    Returns DataFrame of per-market results.
    """
    # Build lookup: (entry_bin, target) → net_ev
    ev_lookup = ev_surface.set_index(["entry_bin", "target"])["net_ev"].to_dict()

    # Best target per bin (max net_ev, must be > min_ev)
    best_target_per_bin: dict[str, float | None] = {}
    for label in bin_labels:
        candidates = {
            t: ev_lookup.get((label, t), float("-inf"))
            for t in ev_surface["target"].unique()
        }
        best = max(candidates, key=candidates.get)
        best_target_per_bin[label] = best if candidates[best] > min_ev else None

    records = []

    for ticker, ticker_df in all_trades.groupby("ticker"):
        ticker_df = ticker_df.sort_values("created_time").copy()
        # Strip timezone for numpy comparisons
        times_naive = ticker_df["created_time"].dt.tz_localize(None)
        ticker_df["_time_naive"] = times_naive
        result_yes = bool(ticker_df["result_yes"].iloc[0])

        # Only test markets
        market_date = ticker_df["market_date"].iloc[0]
        if pd.Timestamp(market_date) < pd.Timestamp(train_cutoff):
            continue

        eligible = ticker_df[ticker_df["time_to_expiry"] >= entry_ttx_min]
        if len(eligible) == 0:
            continue

        window_start = eligible.iloc[0]["_time_naive"]
        window_end   = window_start + pd.Timedelta(hours=entry_window_hours)

        window_trades = ticker_df[
            (ticker_df["_time_naive"] >= window_start) &
            (ticker_df["_time_naive"] <= window_end)
        ]

        entered = False
        for _, row in window_trades.iterrows():
            price = row["price"]

            # Bin the price
            entry_bin = None
            for lo, hi, label in zip(bin_edges[:-1], bin_edges[1:], bin_labels):
                if lo <= price < hi:
                    entry_bin = label
                    break
            if entry_bin is None:
                continue

            best_t = best_target_per_bin.get(entry_bin)
            if best_t is None or best_t <= price:
                continue

            # Enter: simulate from this trade onward
            entry_time  = row["_time_naive"]
            entry_price = price
            stop        = stop_frac * entry_price
            target      = best_t

            after_mask   = ticker_df["_time_naive"].values > entry_time
            after_prices = ticker_df["price"].values[after_mask]
            last_price   = after_prices[-1] if len(after_prices) > 0 else entry_price

            outcomes = _simulate_multi_target(after_prices, [target], stop, last_price,
                                              result_yes=result_yes)
            outcome, exit_price = outcomes[target]

            fee_entry = _taker_fee(entry_price, entry_fee_rate)
            fee_exit  = _taker_fee(exit_price,  exit_fee_rate)
            gross_pnl = exit_price - entry_price
            net_pnl   = gross_pnl - fee_entry - fee_exit

            records.append({
                "ticker":       ticker,
                "market_date":  market_date,
                "entry_bin":    entry_bin,
                "entry_price":  entry_price,
                "target":       target,
                "stop":         stop,
                "outcome":      outcome,
                "exit_price":   exit_price,
                "gross_pnl":    gross_pnl,
                "net_pnl":      net_pnl,
                "entered":      True,
            })
            entered = True
            break   # one trade per market

        if not entered:
            records.append({
                "ticker":      ticker,
                "market_date": market_date,
                "entered":     False,
                "net_pnl":     0.0,
                "gross_pnl":   0.0,
                "outcome":     "no_entry",
            })

    return pd.DataFrame(records)
