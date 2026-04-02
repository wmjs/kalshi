"""
Microstructure analysis for Kalshi binary prediction markets.

All functions take a DuckDB connection and a series ticker string.
They return DataFrames suitable for plotting or further analysis.

Notation:
    p       yes_price in cents (0-99)
    Δp      price change between consecutive trades within a market
    q       signed volume: +count if taker_side='yes', -count if taker_side='no'
    mid_t   approximated as the average of trade prices at t and t+1
    hs      effective half-spread
    λ       Kyle's lambda (price impact per unit signed volume)
    TTX     time to expiry in seconds
"""

import numpy as np
import pandas as pd
from scipy import stats
import duckdb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TTX_EDGES = [0, 600, 1800, 3600, 7200, 14400, 28800, 86400, np.inf]
TTX_LABELS = ["<10m", "10-30m", "30m-1h", "1-2h", "2-4h", "4-8h", "8-24h", ">24h"]


def ttx_bucket(s: pd.Series) -> pd.Series:
    return pd.cut(s, bins=TTX_EDGES, labels=TTX_LABELS, right=False)


def load_trades(con: duckdb.DuckDBPyConnection, series: str) -> pd.DataFrame:
    """
    Load all trades for a series with per-market lag/lead columns pre-computed.
    Computes within each market (partitioned by ticker):
        p_next          next trade price
        mid             (p + p_next) / 2
        dp              p - p_prev  (price change from previous trade)
        signed_vol      +count if yes-taker, -count if no-taker
        ttx_bucket      categorical TTX bin
    """
    df = con.execute(f"""
        SELECT
            ticker,
            market_date,
            created_time,
            yes_price                                           AS p,
            count,
            taker_side,
            time_to_expiry,
            bracket_type,
            strike,
            LEAD(yes_price) OVER w                             AS p_next,
            LAG(yes_price)  OVER w                             AS p_prev,
            LEAD(time_to_expiry) OVER w                        AS ttx_next
        FROM trades
        WHERE series = '{series}'
        WINDOW w AS (PARTITION BY ticker ORDER BY created_time)
    """).df()

    df["mid"]        = (df["p"] + df["p_next"]) / 2.0
    df["dp"]         = df["p"] - df["p_prev"]
    df["signed_vol"] = np.where(df["taker_side"] == "yes", df["count"], -df["count"])
    df["direction"]  = np.where(df["taker_side"] == "yes", 1, -1)
    df["ttx_bucket"] = ttx_bucket(df["time_to_expiry"])

    return df


# ---------------------------------------------------------------------------
# 1. Activity Profile
# ---------------------------------------------------------------------------

def activity_profile(df: pd.DataFrame) -> pd.DataFrame:
    """
    Contracts traded and trade count by TTX bucket.
    Normalized per market-day to make cross-bucket comparisons fair.
    """
    n_markets = df["ticker"].nunique()

    result = (
        df.groupby("ttx_bucket", observed=True)
        .agg(
            n_trades      = ("p", "count"),
            total_contracts = ("count", "sum"),
            avg_trade_size  = ("count", "mean"),
        )
        .reset_index()
    )
    result["trades_per_market"]    = result["n_trades"]    / n_markets
    result["contracts_per_market"] = result["total_contracts"] / n_markets
    return result


def hourly_activity(df: pd.DataFrame) -> pd.DataFrame:
    """Trade count and volume by hour of day (UTC)."""
    df = df.copy()
    df["hour_utc"] = df["created_time"].dt.hour
    return (
        df.groupby("hour_utc")
        .agg(
            n_trades        = ("p", "count"),
            total_contracts = ("count", "sum"),
            avg_price       = ("p", "mean"),
        )
        .reset_index()
    )


# ---------------------------------------------------------------------------
# 2. Volatility Term Structure
# ---------------------------------------------------------------------------

def volatility_term_structure(df: pd.DataFrame) -> pd.DataFrame:
    """
    Realized volatility of price changes (std of Δp) by TTX bucket.
    Also returns mean |Δp| and trade count per bucket.

    For binary markets, expect vol to spike when temperature is recorded
    (resolution event), then collapse to near zero near settlement.
    """
    valid = df.dropna(subset=["dp", "ttx_bucket"])
    result = (
        valid.groupby("ttx_bucket", observed=True)["dp"]
        .agg(
            n_trades    = "count",
            mean_abs_dp = lambda x: x.abs().mean(),
            std_dp      = "std",
            p5          = lambda x: x.quantile(0.05),
            p95         = lambda x: x.quantile(0.95),
        )
        .reset_index()
    )
    return result


# ---------------------------------------------------------------------------
# 3. Price Autocorrelation
# ---------------------------------------------------------------------------

def price_autocorrelation(df: pd.DataFrame, max_lag: int = 10) -> pd.DataFrame:
    """
    Serial autocorrelation of within-market price changes at lags 1..max_lag.

    Negative values indicate mean reversion (favorable for market making).
    Computed on pooled Δp across all markets (normalized by per-market std
    to avoid scale effects from different markets).
    """
    valid = df.dropna(subset=["dp"]).copy()

    # Normalize Δp within each market to equalize scale across strikes
    valid["dp_norm"] = valid.groupby("ticker")["dp"].transform(
        lambda x: (x - x.mean()) / x.std() if x.std() > 0 else 0
    )

    records = []
    series = valid.sort_values(["ticker", "created_time"])["dp_norm"]
    for lag in range(1, max_lag + 1):
        r, pval = stats.pearsonr(
            series.iloc[:-lag].values,
            series.iloc[lag:].values,
        )
        records.append({"lag": lag, "autocorr": r, "pvalue": pval})

    return pd.DataFrame(records)


def autocorrelation_by_ttx(df: pd.DataFrame) -> pd.DataFrame:
    """
    Lag-1 autocorrelation of price changes within each TTX bucket.
    Shows whether mean reversion/momentum varies by time-to-expiry.
    """
    valid = df.dropna(subset=["dp", "p_next"])
    records = []
    for bucket, grp in valid.groupby("ttx_bucket", observed=True):
        if len(grp) < 30:
            continue
        dp    = grp["dp"].values
        dp_fwd = grp.sort_values("created_time")["dp"].shift(-1).dropna().values
        dp_cur = grp["dp"].iloc[:len(dp_fwd)].values
        if len(dp_cur) < 10:
            continue
        r, pval = stats.pearsonr(dp_cur, dp_fwd)
        records.append({"ttx_bucket": bucket, "lag1_autocorr": r, "pvalue": pval, "n": len(dp_cur)})
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# 4. Kyle's Lambda (Price Impact)
# ---------------------------------------------------------------------------

def kyle_lambda(df: pd.DataFrame) -> dict:
    """
    Estimate Kyle's lambda via OLS: Δp = α + λ * signed_vol + ε

    λ > 0: buying pressure moves price up (expected).
    Larger λ = thinner market = more price impact per contract.

    Returns dict with lambda, t-stat, r-squared, and per-TTX-bucket estimates.
    """
    valid = df.dropna(subset=["dp", "signed_vol"])

    # Global estimate
    slope, intercept, r, p, se = stats.linregress(
        valid["signed_vol"], valid["dp"]
    )
    global_result = {
        "lambda":    slope,
        "intercept": intercept,
        "r_squared": r ** 2,
        "t_stat":    slope / se,
        "p_value":   p,
        "n":         len(valid),
    }

    # Per-TTX-bucket estimates
    bucket_records = []
    for bucket, grp in valid.groupby("ttx_bucket", observed=True):
        if len(grp) < 30:
            continue
        s, _, r, _, se = stats.linregress(grp["signed_vol"], grp["dp"])
        bucket_records.append({
            "ttx_bucket": bucket,
            "lambda":     s,
            "r_squared":  r ** 2,
            "n":          len(grp),
        })

    return {
        "global":     global_result,
        "by_ttx":     pd.DataFrame(bucket_records),
    }


# ---------------------------------------------------------------------------
# 5. Spread Decomposition
# ---------------------------------------------------------------------------

def spread_decomposition(df: pd.DataFrame) -> dict:
    """
    Estimates effective half-spread, realized spread, and adverse selection.

    Uses next-trade approximation for the true mid:
        mid_t  ≈ (p_t + p_{t+1}) / 2

    Effective half-spread:
        hs = direction * (p - mid_t)
           = direction * (p_t - p_{t+1}) / 2

    Realized half-spread (MM revenue, measured at t+1):
        realized_hs = direction * (p - p_{t+1})

    Adverse selection (information cost):
        adv_sel = hs - realized_hs = direction * (p_{t+1} - mid_t)
                = direction * (p_{t+1} - p_t) / 2

    All in cents. Positive hs = spread is profitable for passive side.
    Adverse selection = how much price moves against the MM after a fill.
    """
    valid = df.dropna(subset=["p_next", "direction"]).copy()

    valid["eff_hs"]      = valid["direction"] * (valid["p"] - valid["p_next"]) / 2.0
    valid["realized_hs"] = valid["direction"] * (valid["p"] - valid["p_next"])
    valid["adv_sel"]     = valid["direction"] * (valid["p_next"] - valid["p"]) / 2.0

    global_stats = {
        "eff_half_spread_mean":  valid["eff_hs"].mean(),
        "eff_half_spread_median": valid["eff_hs"].median(),
        "realized_hs_mean":      valid["realized_hs"].mean(),
        "adv_sel_mean":          valid["adv_sel"].mean(),
        "n":                     len(valid),
    }

    # By TTX bucket
    by_ttx = (
        valid.groupby("ttx_bucket", observed=True)
        .agg(
            eff_hs_mean    = ("eff_hs",      "mean"),
            realized_hs    = ("realized_hs", "mean"),
            adv_sel_mean   = ("adv_sel",     "mean"),
            n              = ("eff_hs",      "count"),
        )
        .reset_index()
    )

    return {"global": global_stats, "by_ttx": by_ttx}


# ---------------------------------------------------------------------------
# 6. Convergence Profile
# ---------------------------------------------------------------------------

def convergence_profile(df: pd.DataFrame) -> pd.DataFrame:
    """
    How does price behave as TTX → 0?

    For each TTX bucket, computes:
        - mean price distance from resolution (|p - 99| for yes, |p - 1| for no)
          joined from market results
        - price std across markets in that bucket
        - fraction of markets where price has "committed" (p > 90 or p < 10)
    """
    valid = df.dropna(subset=["ttx_bucket"])
    result = (
        valid.groupby("ttx_bucket", observed=True)
        .agg(
            mean_price      = ("p", "mean"),
            std_price       = ("p", "std"),
            pct_committed   = ("p", lambda x: ((x > 90) | (x < 10)).mean()),
            n_trades        = ("p", "count"),
        )
        .reset_index()
    )
    return result
