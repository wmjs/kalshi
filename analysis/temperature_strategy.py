"""
Core analysis functions for the daily temperature high binary market strategy.

Designed for Kalshi markets with structure: N brackets per day ranked by strike,
with an entry window defined by TTX > 24h and a 6-hour watch period.

Primary use: KXHIGHNY (NYC daily high). Rerunnable for any analogous series.

Usage (from project root):
    from analysis.temperature_strategy import load_and_rank, run_backtest, stop_sweep, ...
"""

import numpy as np
import pandas as pd
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENTRY_TTX_MIN  = 86400      # 24h in seconds; used in entry window logic
ENTRY_WIN_HRS  = 6.0        # hours to watch after window_start
TAKER_FEE_RATE = 0.07       # 0.07 × P × (1-P)
MAKER_FEE_RATE = 0.0        # KXHIGHNY is not on maker-fee list


def taker_fee(price: float, rate: float = TAKER_FEE_RATE) -> float:
    """Fee per contract in cents: rate × P × (1-P), P in dollars."""
    p = price / 100.0
    return rate * p * (1.0 - p) * 100.0


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_and_rank(con, series: str) -> pd.DataFrame:
    """
    Load trades for a series and assign daily strike ranks (1 = coldest, N = hottest).
    Returns the full trades DataFrame with a 'rank' column added.
    """
    from analysis.strategy_backtest import load_series_trades

    df = load_series_trades(con, series)
    df["market_date"] = pd.to_datetime(df["market_date"])
    df["_t"] = df["created_time"].dt.tz_localize(None)

    daily = df.drop_duplicates("ticker")[["ticker", "market_date", "strike"]].copy()
    daily["rank"] = (
        daily.groupby("market_date")["strike"]
        .rank(method="first")
        .astype(int)
    )
    df = df.merge(daily[["ticker", "rank"]], on="ticker")
    return df


def filter_rank(df: pd.DataFrame, rank: int) -> pd.DataFrame:
    return df[df["rank"] == rank].copy()


# ---------------------------------------------------------------------------
# Entry window helpers
# ---------------------------------------------------------------------------

def _iter_entries(df: pd.DataFrame, band_lo: float, band_hi: float,
                  from_below_filter: bool = True):
    """
    Yield per-market entry records.

    Yields dict with:
        ticker, market_date, result_yes, approach, entry_price, entry_time,
        after_prices (np.ndarray), open_price
    """
    for ticker, tdf in df.groupby("ticker"):
        tdf = tdf.sort_values("_t")
        result_yes  = bool(tdf["result_yes"].iloc[0])
        market_date = tdf["market_date"].iloc[0]

        eligible = tdf[tdf["time_to_expiry"] >= ENTRY_TTX_MIN]
        if len(eligible) == 0:
            continue

        ws       = eligible.iloc[0]["_t"]
        we       = ws + pd.Timedelta(hours=ENTRY_WIN_HRS)
        open_p   = eligible.iloc[0]["price"]
        win      = tdf[(tdf["_t"] >= ws) & (tdf["_t"] <= we)]
        if len(win) == 0:
            continue

        # Classify approach
        if open_p >= band_hi:
            approach = "from_above"
        elif open_p >= band_lo:
            approach = "at_open"
        else:
            approach = "from_below"

        if from_below_filter and approach == "from_below":
            continue

        # Find first trade in band
        in_band = win[(win["price"] >= band_lo) & (win["price"] < band_hi)]
        if len(in_band) == 0:
            continue

        row          = in_band.iloc[0]
        entry_price  = row["price"]
        entry_time   = row["_t"]
        after_prices = tdf[tdf["_t"] > entry_time]["price"].values

        yield {
            "ticker":       ticker,
            "market_date":  market_date,
            "result_yes":   result_yes,
            "approach":     approach,
            "open_price":   open_p,
            "entry_price":  entry_price,
            "entry_time":   entry_time,
            "after_prices": after_prices,
        }


def _simulate_outcome(entry_price, after_prices, target, stop_frac, result_yes):
    """Return (outcome, exit_price, net_pnl)."""
    sp = stop_frac * entry_price
    oc, xp = None, None
    for ap in after_prices:
        if ap <= sp:
            oc, xp = "stop",   sp
            break
        if ap >= target:
            oc, xp = "target", float(target)
            break
    if oc is None:
        oc, xp = ("target", float(target)) if result_yes else ("stop", sp)

    fe  = taker_fee(entry_price)
    fx  = taker_fee(xp)
    net = xp - entry_price - fe - fx
    return oc, xp, net


# ---------------------------------------------------------------------------
# 1. Run backtest
# ---------------------------------------------------------------------------

def run_backtest(df: pd.DataFrame, band_lo: float, band_hi: float,
                 target: float, stop_frac: float,
                 from_below_filter: bool = True) -> pd.DataFrame:
    """
    Run the directional backtest on rank-filtered trades.

    Returns DataFrame with one row per entered market.
    """
    records = []
    for e in _iter_entries(df, band_lo, band_hi, from_below_filter):
        oc, xp, net = _simulate_outcome(
            e["entry_price"], e["after_prices"], target, stop_frac, e["result_yes"]
        )
        records.append({
            "ticker":       e["ticker"],
            "market_date":  e["market_date"],
            "approach":     e["approach"],
            "entry_price":  e["entry_price"],
            "target":       target,
            "stop_frac":    stop_frac,
            "outcome":      oc,
            "exit_price":   xp,
            "net_pnl":      net,
            "gross_pnl":    xp - e["entry_price"],
        })
    return pd.DataFrame(records) if records else pd.DataFrame()


# ---------------------------------------------------------------------------
# 2. Entry decomposition (at_open / from_above / from_below)
# ---------------------------------------------------------------------------

def entry_decomposition(df: pd.DataFrame, band_lo: float, band_hi: float,
                        target: float, stop_frac: float) -> pd.DataFrame:
    """
    Run the backtest WITHOUT the from_below filter, then decompose by approach.
    Returns summary DataFrame with one row per approach type.
    """
    bt = run_backtest(df, band_lo, band_hi, target, stop_frac, from_below_filter=False)
    if len(bt) == 0:
        return pd.DataFrame()

    be_denom = (target - bt["entry_price"].mean()) + stop_frac * bt["entry_price"].mean()
    be_overall = (stop_frac * bt["entry_price"].mean()) / be_denom

    rows = []
    for approach in ["at_open", "from_above", "from_below"]:
        sub = bt[bt["approach"] == approach]
        if len(sub) == 0:
            continue
        n    = len(sub)
        hr   = (sub["outcome"] == "target").mean()
        ev   = sub["net_pnl"].mean()
        tot  = sub["net_pnl"].sum()
        e_mid = sub["entry_price"].mean()
        be   = (stop_frac * e_mid) / ((target - e_mid) + stop_frac * e_mid)
        rows.append({"approach": approach, "n": n, "hit_rate": hr,
                     "breakeven_hr": be, "ev_per_trade": ev, "total_pnl": tot})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 3. Stop fraction sweep
# ---------------------------------------------------------------------------

def stop_sweep(df: pd.DataFrame, band_lo: float, band_hi: float,
               target: float,
               stop_fracs: list | None = None) -> pd.DataFrame:
    """
    Grid search over stop fractions. Returns one row per stop_frac.
    """
    if stop_fracs is None:
        stop_fracs = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60, 0.75]

    rows = []
    for sf in stop_fracs:
        bt = run_backtest(df, band_lo, band_hi, target, sf)
        if len(bt) < 5:
            continue
        n    = len(bt)
        nw   = (bt["outcome"] == "target").sum()
        nl   = (bt["outcome"] == "stop").sum()
        ev   = bt["net_pnl"].mean()
        sd   = bt["net_pnl"].std()
        sh   = ev / sd * np.sqrt(n) if sd > 0 else 0
        aw   = bt[bt["outcome"] == "target"]["net_pnl"].mean() if nw > 0 else np.nan
        al   = bt[bt["outcome"] == "stop"]["net_pnl"].mean()   if nl > 0 else np.nan
        rows.append({
            "stop_frac": sf,
            "stop_price_approx": sf * bt["entry_price"].mean(),
            "n": n, "n_wins": nw, "n_stops": nl,
            "hit_rate": nw / n,
            "ev": ev, "total_pnl": bt["net_pnl"].sum(),
            "sharpe": sh,
            "avg_win": aw, "avg_loss": al,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 4. Target price sweep
# ---------------------------------------------------------------------------

def target_sweep(df: pd.DataFrame, band_lo: float, band_hi: float,
                 stop_frac: float,
                 targets: list | None = None) -> pd.DataFrame:
    """Sweep target prices at fixed stop fraction."""
    if targets is None:
        targets = list(range(45, 96, 5))

    rows = []
    for tgt in targets:
        bt = run_backtest(df, band_lo, band_hi, tgt, stop_frac)
        if len(bt) < 5:
            continue
        n  = len(bt); ev = bt["net_pnl"].mean(); sd = bt["net_pnl"].std()
        rows.append({
            "target": tgt, "n": n,
            "hit_rate": (bt["outcome"] == "target").mean(),
            "ev": ev, "total_pnl": bt["net_pnl"].sum(),
            "sharpe": ev / sd * np.sqrt(n) if sd > 0 else 0,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 5. Seasonal breakdown
# ---------------------------------------------------------------------------

SEASON_MAP = {3: "Spring", 4: "Spring", 5: "Spring",
              6: "Summer", 7: "Summer", 8: "Summer",
              9: "Fall",   10: "Fall",  11: "Fall",
              12: "Winter", 1: "Winter", 2: "Winter"}

SEASON_ORDER = ["Spring", "Summer", "Fall", "Winter"]


def seasonal_breakdown(df: pd.DataFrame, band_lo: float, band_hi: float,
                       target: float, stop_frac: float) -> pd.DataFrame:
    """Return per-season backtest summary."""
    bt = run_backtest(df, band_lo, band_hi, target, stop_frac)
    if len(bt) == 0:
        return pd.DataFrame()

    bt["season"] = bt["market_date"].dt.month.map(SEASON_MAP)
    rows = []
    for s in SEASON_ORDER:
        sub = bt[bt["season"] == s]
        if len(sub) == 0:
            continue
        n  = len(sub); ev = sub["net_pnl"].mean()
        rows.append({
            "season": s, "n": n,
            "hit_rate": (sub["outcome"] == "target").mean(),
            "ev": ev, "total_pnl": sub["net_pnl"].sum(),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 6. EV surface (entry bin × target)
# ---------------------------------------------------------------------------

def ev_surface(df: pd.DataFrame, stop_frac: float,
               bin_size: int = 5,
               targets: list | None = None) -> pd.DataFrame:
    """
    Build EV surface across entry bins and target prices.
    Returns DataFrame with columns: entry_bin, target, n, hit_rate, ev, be_hit_rate
    """
    if targets is None:
        targets = list(range(35, 96, 5))

    edges  = list(range(0, 101, bin_size))
    labels = [f"{lo}-{lo+bin_size}" for lo in range(0, 100, bin_size)]

    records = []
    for ticker, tdf in df.groupby("ticker"):
        tdf       = tdf.sort_values("_t")
        result_yes = bool(tdf["result_yes"].iloc[0])
        eligible   = tdf[tdf["time_to_expiry"] >= ENTRY_TTX_MIN]
        if len(eligible) == 0:
            continue
        ws  = eligible.iloc[0]["_t"]
        we  = ws + pd.Timedelta(hours=ENTRY_WIN_HRS)
        win = tdf[(tdf["_t"] >= ws) & (tdf["_t"] <= we)]
        if len(win) == 0:
            continue

        for lo, label in zip(range(0, 100, bin_size), labels):
            hi    = lo + bin_size
            inb   = win[(win["price"] >= lo) & (win["price"] < hi)]
            if len(inb) == 0:
                continue
            row    = inb.iloc[0]
            ep     = row["price"]
            at     = row["_t"]
            aps    = tdf[tdf["_t"] > at]["price"].values

            for tgt in targets:
                if tgt <= ep:
                    continue
                oc, xp, net = _simulate_outcome(ep, aps, tgt, stop_frac, result_yes)
                records.append({"entry_bin": label, "target": tgt,
                                 "entry_price": ep, "outcome": oc, "net_pnl": net})

    if not records:
        return pd.DataFrame()

    raw = pd.DataFrame(records)
    out = []
    for (b, t), g in raw.groupby(["entry_bin", "target"]):
        n   = len(g)
        hr  = (g["outcome"] == "target").mean()
        ev  = g["net_pnl"].mean()
        e_m = g["entry_price"].mean()
        be  = (stop_frac * e_m) / ((t - e_m) + stop_frac * e_m) if (t - e_m) > 0 else np.nan
        out.append({"entry_bin": b, "target": t, "n": n,
                    "hit_rate": hr, "ev": ev, "be_hit_rate": be})
    return pd.DataFrame(out)
