"""
Full microstructure report for a Kalshi temperature series.

Produces a multi-panel figure covering:
    1. Activity profile (volume and trade count by TTX)
    2. Hourly activity (UTC)
    3. Volatility term structure
    4. Price autocorrelation (lags 1-10)
    5. Autocorrelation by TTX bucket
    6. Kyle's lambda by TTX bucket
    7. Spread decomposition by TTX bucket
    8. Price convergence profile

Usage:
    python examples/microstructure_report.py
    python examples/microstructure_report.py --series KXHIGHNY --out report.png
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import duckdb
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
import numpy as np
import pandas as pd

from analysis.microstructure import (
    TTX_LABELS,
    activity_profile,
    autocorrelation_by_ttx,
    convergence_profile,
    hourly_activity,
    kyle_lambda,
    load_trades,
    price_autocorrelation,
    spread_decomposition,
    volatility_term_structure,
)

DB_PATH = Path("data/processed/kalshi.duckdb")

# ---------------------------------------------------------------------------
# Plot style
# ---------------------------------------------------------------------------

BG    = "#0f0f0f"
PANEL = "#1a1a1a"
GRID  = "#2a2a2a"
TEXT  = "#cccccc"
BLUE  = "#4488cc"
GREEN = "#44cc88"
RED   = "#cc4444"
GOLD  = "#ccaa44"
WHITE = "#eeeeee"


def style_ax(ax):
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=TEXT, labelsize=8)
    ax.xaxis.label.set_color(TEXT)
    ax.yaxis.label.set_color(TEXT)
    ax.title.set_color(WHITE)
    for spine in ax.spines.values():
        spine.set_edgecolor(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.5, alpha=0.6)


def ttx_x(df, col="ttx_bucket"):
    """Return numeric x positions for TTX bucket labels."""
    cats = [l for l in TTX_LABELS if l in df[col].values]
    x    = np.arange(len(cats))
    return x, cats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(series: str, out: str) -> None:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    print(f"Loading trades for {series}...")
    df = load_trades(con, series)
    con.close()
    print(f"  {len(df):,} trades across {df['ticker'].nunique()} markets")

    print("Computing microstructure statistics...")
    act    = activity_profile(df)
    hourly = hourly_activity(df)
    vol_ts = volatility_term_structure(df)
    ac     = price_autocorrelation(df, max_lag=10)
    ac_ttx = autocorrelation_by_ttx(df)
    lam    = kyle_lambda(df)
    spread = spread_decomposition(df)
    conv   = convergence_profile(df)

    # Print global stats
    g = lam["global"]
    s = spread["global"]
    print(f"\n  Kyle's λ  = {g['lambda']:.4f}  (t={g['t_stat']:.1f}, R²={g['r_squared']:.4f})")
    print(f"  Eff half-spread (mean) = {s['eff_half_spread_mean']:.2f}¢")
    print(f"  Adverse selection      = {s['adv_sel_mean']:.2f}¢")
    print(f"  Realized half-spread   = {s['realized_hs_mean']:.2f}¢")
    print(f"  Lag-1 autocorr         = {ac.loc[ac['lag']==1, 'autocorr'].values[0]:.3f}")

    # ---------------------------------------------------------------------------
    # Figure
    # ---------------------------------------------------------------------------
    fig = plt.figure(figsize=(18, 20), facecolor=BG)
    fig.suptitle(
        f"Microstructure Analysis — {series}\n"
        f"{df['market_date'].min()} to {df['market_date'].max()}  |  "
        f"{df['ticker'].nunique()} markets  |  {len(df):,} trades",
        color=WHITE, fontsize=14, y=0.98,
    )
    gs = fig.add_gridspec(4, 2, hspace=0.42, wspace=0.32,
                          left=0.07, right=0.97, top=0.94, bottom=0.04)

    # ---- 1. Activity: contracts by TTX ----
    ax1 = fig.add_subplot(gs[0, 0])
    style_ax(ax1)
    x, cats = ttx_x(act)
    vals = act.set_index("ttx_bucket").reindex(cats)["contracts_per_market"]
    ax1.bar(x, vals, color=BLUE, alpha=0.8)
    ax1.set_xticks(x); ax1.set_xticklabels(cats, rotation=30, ha="right", fontsize=7)
    ax1.set_title("Contracts Traded per Market by TTX")
    ax1.set_ylabel("Avg contracts / market")

    # ---- 2. Hourly activity ----
    ax2 = fig.add_subplot(gs[0, 1])
    style_ax(ax2)
    ax2.bar(hourly["hour_utc"], hourly["total_contracts"] / 1e3, color=GREEN, alpha=0.8)
    ax2.set_title("Contracts Traded by Hour of Day (UTC)")
    ax2.set_xlabel("Hour (UTC)")
    ax2.set_ylabel("Total contracts (thousands)")
    ax2.set_xticks(range(0, 24, 2))

    # ---- 3. Volatility term structure ----
    ax3 = fig.add_subplot(gs[1, 0])
    style_ax(ax3)
    x, cats = ttx_x(vol_ts)
    v = vol_ts.set_index("ttx_bucket").reindex(cats)
    ax3.bar(x, v["std_dp"], color=RED, alpha=0.8, label="std(Δp)")
    ax3.bar(x, v["mean_abs_dp"], color=GOLD, alpha=0.6, width=0.4, label="mean|Δp|")
    ax3.set_xticks(x); ax3.set_xticklabels(cats, rotation=30, ha="right", fontsize=7)
    ax3.set_title("Realized Volatility Term Structure")
    ax3.set_ylabel("Price change (¢)")
    ax3.legend(fontsize=8, facecolor=PANEL, labelcolor=TEXT)

    # ---- 4. Autocorrelation by lag ----
    ax4 = fig.add_subplot(gs[1, 1])
    style_ax(ax4)
    colors = [GREEN if r < 0 else RED for r in ac["autocorr"]]
    ax4.bar(ac["lag"], ac["autocorr"], color=colors, alpha=0.8)
    ax4.axhline(0, color=TEXT, linewidth=0.8)
    ci = 1.96 / np.sqrt(len(df))
    ax4.axhline(ci,  color=WHITE, linewidth=0.6, linestyle="--", alpha=0.5)
    ax4.axhline(-ci, color=WHITE, linewidth=0.6, linestyle="--", alpha=0.5)
    ax4.set_title("Price Change Autocorrelation (lags 1–10)")
    ax4.set_xlabel("Lag")
    ax4.set_ylabel("Pearson r")
    ax4.set_xticks(ac["lag"])

    # ---- 5. Lag-1 autocorr by TTX ----
    ax5 = fig.add_subplot(gs[2, 0])
    style_ax(ax5)
    x, cats = ttx_x(ac_ttx)
    v = ac_ttx.set_index("ttx_bucket").reindex(cats)
    bar_colors = [GREEN if r < 0 else RED for r in v["lag1_autocorr"].fillna(0)]
    ax5.bar(x, v["lag1_autocorr"], color=bar_colors, alpha=0.8)
    ax5.axhline(0, color=TEXT, linewidth=0.8)
    ax5.set_xticks(x); ax5.set_xticklabels(cats, rotation=30, ha="right", fontsize=7)
    ax5.set_title("Lag-1 Autocorrelation by TTX Bucket")
    ax5.set_ylabel("Pearson r")

    # ---- 6. Kyle's lambda by TTX ----
    ax6 = fig.add_subplot(gs[2, 1])
    style_ax(ax6)
    lam_ttx = lam["by_ttx"]
    x, cats = ttx_x(lam_ttx)
    v = lam_ttx.set_index("ttx_bucket").reindex(cats)
    ax6.bar(x, v["lambda"] * 100, color=GOLD, alpha=0.8)  # scale for readability
    ax6.axhline(0, color=TEXT, linewidth=0.8)
    ax6.set_xticks(x); ax6.set_xticklabels(cats, rotation=30, ha="right", fontsize=7)
    ax6.set_title("Kyle's λ by TTX  (price impact per 100 contracts)")
    ax6.set_ylabel("Δp per 100 contracts (¢)")

    # ---- 7. Spread decomposition by TTX ----
    ax7 = fig.add_subplot(gs[3, 0])
    style_ax(ax7)
    sp = spread["by_ttx"]
    x, cats = ttx_x(sp)
    v = sp.set_index("ttx_bucket").reindex(cats)
    w = 0.25
    ax7.bar(x - w, v["eff_hs_mean"],  width=w, color=BLUE,  alpha=0.85, label="Eff half-spread")
    ax7.bar(x,     v["realized_hs"],  width=w, color=GREEN, alpha=0.85, label="Realized HS")
    ax7.bar(x + w, v["adv_sel_mean"], width=w, color=RED,   alpha=0.85, label="Adverse selection")
    ax7.axhline(0, color=TEXT, linewidth=0.8)
    ax7.set_xticks(x); ax7.set_xticklabels(cats, rotation=30, ha="right", fontsize=7)
    ax7.set_title("Spread Decomposition by TTX")
    ax7.set_ylabel("Cents (¢)")
    ax7.legend(fontsize=8, facecolor=PANEL, labelcolor=TEXT)

    # ---- 8. Convergence profile ----
    ax8 = fig.add_subplot(gs[3, 1])
    style_ax(ax8)
    x, cats = ttx_x(conv)
    v = conv.set_index("ttx_bucket").reindex(cats)
    ax8_twin = ax8.twinx()
    ax8_twin.set_facecolor(PANEL)
    ax8.bar(x, v["std_price"], color=BLUE, alpha=0.7, label="Price std")
    ax8_twin.plot(x, v["pct_committed"] * 100, color=GOLD, marker="o",
                  linewidth=1.5, markersize=4, label="% committed (p>90 or p<10)")
    ax8.set_xticks(x); ax8.set_xticklabels(cats, rotation=30, ha="right", fontsize=7)
    ax8.set_title("Price Convergence Profile")
    ax8.set_ylabel("Price std (¢)", color=BLUE)
    ax8_twin.set_ylabel("% committed", color=GOLD)
    ax8_twin.tick_params(colors=GOLD, labelsize=8)
    ax8_twin.yaxis.label.set_color(GOLD)
    ax8.legend(fontsize=8, facecolor=PANEL, labelcolor=TEXT, loc="upper left")
    ax8_twin.legend(fontsize=8, facecolor=PANEL, labelcolor=TEXT, loc="upper right")

    plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=BG)
    print(f"\nSaved → {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--series", default="KXHIGHNY")
    parser.add_argument("--out",    default=None)
    args = parser.parse_args()
    out = args.out or f"analysis/reports/{args.series.lower()}_microstructure.png"
    run(args.series, out)
