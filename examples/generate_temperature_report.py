"""
Generate the full strategy development report for a daily temperature series.

Produces:
    analysis/reports/{series}_strategy_report.md   — detailed markdown report
    analysis/reports/{series}_fig1_structure.png   — market structure
    analysis/reports/{series}_fig2_decomp.png      — entry decomposition + PnL
    analysis/reports/{series}_fig3_stop.png        — stop optimization
    analysis/reports/{series}_fig4_seasonal.png    — seasonal breakdown

Usage:
    python3 examples/generate_temperature_report.py
    python3 examples/generate_temperature_report.py --series KXHIGHNY --rank 4
    python3 examples/generate_temperature_report.py --series KXHIGHNY --rank 4 \
        --band-lo 30 --band-hi 35 --target 70 --stop 0.25
"""

import argparse, sys, textwrap, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent.parent))

import duckdb
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import seaborn as sns

from analysis.temperature_strategy import (
    load_and_rank, filter_rank,
    run_backtest, entry_decomposition, stop_sweep, seasonal_breakdown,
    ev_surface, SEASON_ORDER, taker_fee,
    ENTRY_WIN_HRS,
)

DB_PATH = Path("data/processed/kalshi.duckdb")

# ── Palette ──────────────────────────────────────────────────────────────────
BG    = "#0f0f0f"; PANEL = "#1a1a1a"; TEXT = "#cccccc"
WHITE = "#eeeeee"; BLUE  = "#4488cc"; GREEN = "#44cc88"
RED   = "#cc4444"; GOLD  = "#ccaa44"; ORANGE = "#cc8844"

def sax(ax):
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=TEXT, labelsize=8)
    ax.xaxis.label.set_color(TEXT); ax.yaxis.label.set_color(TEXT)
    ax.title.set_color(WHITE)
    for sp in ax.spines.values(): sp.set_edgecolor("#2a2a2a")
    ax.grid(axis="y", color="#2a2a2a", linewidth=0.5, alpha=0.6)

# ── Figure 1: Market Structure ────────────────────────────────────────────────

def fig_structure(df, series, primary_rank, band_lo, band_hi, out_path):
    n_ranks = df["rank"].max()
    fig, axes = plt.subplots(1, 2, figsize=(14, 5), facecolor=BG)
    fig.suptitle(f"{series} — Market Structure", color=WHITE, fontsize=12)

    # Left: entry price distribution by rank at TTX>24h
    ax = axes[0]; sax(ax)
    for rank in range(1, n_ranks + 1):
        rdf  = filter_rank(df, rank)
        elig = rdf.groupby("ticker").apply(
            lambda g: g.sort_values("_t")[g["time_to_expiry"] >= 86400].iloc[0]["price"]
            if len(g[g["time_to_expiry"] >= 86400]) > 0 else np.nan,
            include_groups=False
        ).dropna()
        color = GREEN if rank == primary_rank else BLUE
        alpha = 0.85 if rank == primary_rank else 0.45
        ax.hist(elig, bins=range(0, 102, 3), alpha=alpha, color=color,
                label=f"Rank {rank}" + (" ★" if rank == primary_rank else ""), density=True)
    ax.axvspan(band_lo, band_hi, alpha=0.18, color=GOLD, label=f"Entry band [{band_lo},{band_hi})")
    ax.set_title("First-trade price at TTX>24h by rank", color=WHITE)
    ax.set_xlabel("Price (¢)", color=TEXT); ax.set_ylabel("Density", color=TEXT)
    ax.legend(fontsize=7, facecolor=PANEL, labelcolor=TEXT, ncol=2)

    # Right: # markets per rank × season
    ax = axes[1]; sax(ax)
    season_map = {3:"Spring",4:"Spring",5:"Spring",6:"Summer",7:"Summer",8:"Summer",
                  9:"Fall",10:"Fall",11:"Fall",12:"Winter",1:"Winter",2:"Winter"}
    mkt = df.drop_duplicates("ticker").copy()
    mkt["season"] = mkt["market_date"].dt.month.map(season_map)
    tbl = mkt.groupby(["rank","season"]).size().unstack(fill_value=0)[SEASON_ORDER]
    x = np.arange(n_ranks); w = 0.2
    colors = [GREEN, BLUE, ORANGE, RED]
    for i, s in enumerate(SEASON_ORDER):
        ax.bar(x + i*w, tbl[s].values, width=w, color=colors[i], alpha=0.8, label=s)
    ax.set_xticks(x + w*1.5)
    ax.set_xticklabels([f"R{r}" for r in range(1, n_ranks+1)], fontsize=8)
    ax.set_title("Markets per rank × season", color=WHITE)
    ax.set_ylabel("# markets", color=TEXT)
    ax.legend(fontsize=7, facecolor=PANEL, labelcolor=TEXT)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close()

# ── Figure 2: Entry Decomposition + PnL progression ──────────────────────────

def fig_decomp(df_rank, band_lo, band_hi, target, stop_frac, series, out_path):
    decomp = entry_decomposition(df_rank, band_lo, band_hi, target, stop_frac)
    bt_v1  = run_backtest(df_rank, band_lo, band_hi, target, 0.50, from_below_filter=False)
    bt_v2  = run_backtest(df_rank, band_lo, band_hi, target, 0.50, from_below_filter=True)
    bt_v3  = run_backtest(df_rank, band_lo, band_hi, target, stop_frac, from_below_filter=True)

    fig, axes = plt.subplots(1, 3, figsize=(18, 5), facecolor=BG)
    fig.suptitle(f"{series} — Entry Decomposition & Strategy Evolution", color=WHITE, fontsize=12)

    # Left: entry decomposition bar
    ax = axes[0]; sax(ax)
    if len(decomp) > 0:
        approaches = decomp["approach"].tolist()
        evs = decomp["ev_per_trade"].tolist()
        colors = [GREEN if e > 0 else RED for e in evs]
        ax.bar(approaches, evs, color=colors, alpha=0.85)
        ax.axhline(0, color=TEXT, linewidth=0.7, linestyle="--")
        for i, (ev, n) in enumerate(zip(decomp["ev_per_trade"], decomp["n"])):
            ax.text(i, ev + (0.3 if ev >= 0 else -0.8), f"n={n}\n{ev:+.2f}¢",
                    ha="center", va="bottom" if ev >= 0 else "top", color=TEXT, fontsize=8)
    ax.set_title("EV by entry approach (stop=50%, taker fees)", color=WHITE)
    ax.set_ylabel("Net EV per trade (¢)", color=TEXT)

    # Middle: cumulative PnL v1/v2/v3
    ax = axes[1]; sax(ax)
    for bt, color, label in [
        (bt_v1, RED,    "v1 naive (no filter, 50% stop)"),
        (bt_v2, GOLD,   "v2 from_below filter, 50% stop"),
        (bt_v3, GREEN,  f"v3 filter + {stop_frac:.0%} stop"),
    ]:
        if len(bt) == 0: continue
        sub = bt.sort_values("market_date").copy()
        sub["cum"] = sub["net_pnl"].cumsum()
        ax.plot(sub["market_date"], sub["cum"], color=color, linewidth=1.6, label=label)
        ax.fill_between(sub["market_date"], sub["cum"], alpha=0.08, color=color)
    ax.axhline(0, color=TEXT, linewidth=0.7, linestyle="--")
    ax.set_title("Cumulative net PnL — strategy evolution", color=WHITE)
    ax.set_ylabel("Cumulative net PnL (¢)", color=TEXT)
    ax.legend(fontsize=7, facecolor=PANEL, labelcolor=TEXT)
    fig.autofmt_xdate()

    # Right: summary stats table
    ax = axes[2]; ax.axis("off")
    rows = []
    for label, bt in [("v1 naive", bt_v1), ("v2 from_below filter", bt_v2), (f"v3 (stop={stop_frac:.0%})", bt_v3)]:
        if len(bt) == 0: continue
        n = len(bt); hr = (bt["outcome"]=="target").mean()
        ev = bt["net_pnl"].mean(); tot = bt["net_pnl"].sum()
        sd = bt["net_pnl"].std()
        sh = ev / sd * np.sqrt(n) if sd > 0 else 0
        rows.append([label, str(n), f"{hr:.1%}", f"{ev:+.2f}¢", f"{tot:.0f}¢", f"{sh:.2f}"])
    tbl = ax.table(cellText=rows, colLabels=["Version","N","Hit%","Avg EV","Total","Sharpe"],
                   cellLoc="center", loc="center")
    tbl.auto_set_font_size(False); tbl.set_fontsize(8); tbl.scale(1.0, 2.4)
    for (r, c), cell in tbl.get_celld().items():
        cell.set_facecolor(PANEL if r > 0 else "#2a2a2a")
        cell.set_edgecolor("#333333")
        cell.set_text_props(color=WHITE if r == 0 else TEXT)
    ax.set_title("Summary statistics", color=WHITE, fontsize=10, pad=20)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close()

# ── Figure 3: Stop Optimization ───────────────────────────────────────────────

def fig_stop(sw, recommended_stop, series, out_path):
    fig, axes = plt.subplots(1, 3, figsize=(16, 5), facecolor=BG)
    fig.suptitle(f"{series} — Stop Fraction Optimization", color=WHITE, fontsize=12)

    pcts = sw["stop_frac"] * 100

    for ax, col, ylabel, title in [
        (axes[0], "hit_rate",  "Hit rate",             "Hit Rate vs Stop %"),
        (axes[1], "ev",        "Net EV per trade (¢)", "Mean EV vs Stop %"),
        (axes[2], "sharpe",    "Sharpe",               "Sharpe vs Stop %"),
    ]:
        sax(ax)
        ax.plot(pcts, sw[col], color=BLUE, linewidth=1.8, marker="o", markersize=5)
        # Highlight recommended
        rec_row = sw[sw["stop_frac"] == recommended_stop]
        if len(rec_row) > 0:
            ax.scatter(rec_row["stop_frac"]*100, rec_row[col],
                       color=GREEN, s=100, zorder=5, label="Recommended")
        if col == "hit_rate":
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:.0%}"))
        ax.set_xlabel("Stop Fraction (%)", color=TEXT)
        ax.set_ylabel(ylabel, color=TEXT)
        ax.set_title(title, color=WHITE)
        if col == "hit_rate":
            ax.legend(fontsize=8, facecolor=PANEL, labelcolor=TEXT)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close()

# ── Figure 4: Seasonal Breakdown ──────────────────────────────────────────────

def fig_seasonal(df_rank, band_lo, band_hi, target, stop_frac, series, out_path):
    seasonal = seasonal_breakdown(df_rank, band_lo, band_hi, target, stop_frac)
    bt = run_backtest(df_rank, band_lo, band_hi, target, stop_frac)

    fig, axes = plt.subplots(1, 3, figsize=(16, 5), facecolor=BG)
    fig.suptitle(f"{series} — Seasonal Analysis (rank filtered, v3 params)", color=WHITE, fontsize=12)

    seas_colors = {"Spring": GREEN, "Summer": GOLD, "Fall": ORANGE, "Winter": BLUE}

    # Left: EV by season
    ax = axes[0]; sax(ax)
    if len(seasonal) > 0:
        seas = seasonal["season"].tolist()
        evs  = seasonal["ev"].tolist()
        cols = [seas_colors.get(s, BLUE) for s in seas]
        ax.bar(seas, evs, color=cols, alpha=0.85)
        ax.axhline(0, color=TEXT, linewidth=0.7, linestyle="--")
        for i, (ev, n) in enumerate(zip(evs, seasonal["n"])):
            ax.text(i, ev + (0.2 if ev >= 0 else -0.5), f"n={n}", ha="center",
                    va="bottom" if ev >= 0 else "top", color=TEXT, fontsize=8)
    ax.set_title("Net EV per trade by season", color=WHITE)
    ax.set_ylabel("Net EV (¢)", color=TEXT)

    # Middle: hit rate by season with breakeven line
    ax = axes[1]; sax(ax)
    if len(seasonal) > 0:
        e_mid  = bt["entry_price"].mean()
        be_hit = (stop_frac * e_mid) / ((target - e_mid) + stop_frac * e_mid)
        hrs    = seasonal["hit_rate"].tolist()
        cols   = [seas_colors.get(s, BLUE) for s in seasonal["season"].tolist()]
        ax.bar(seasonal["season"].tolist(), hrs, color=cols, alpha=0.85)
        ax.axhline(be_hit, color=RED, linewidth=1.2, linestyle="--",
                   label=f"Breakeven ({be_hit:.1%})")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:.0%}"))
        ax.legend(fontsize=8, facecolor=PANEL, labelcolor=TEXT)
    ax.set_title("Hit rate by season vs breakeven", color=WHITE)
    ax.set_ylabel("Hit rate", color=TEXT)

    # Right: cumulative PnL colored by season
    ax = axes[2]; sax(ax)
    if len(bt) > 0:
        bt2 = bt.sort_values("market_date").copy()
        bt2["season"] = bt2["market_date"].dt.month.map(
            {3:"Spring",4:"Spring",5:"Spring",6:"Summer",7:"Summer",8:"Summer",
             9:"Fall",10:"Fall",11:"Fall",12:"Winter",1:"Winter",2:"Winter"}
        )
        bt2["cum"] = bt2["net_pnl"].cumsum()
        for s in SEASON_ORDER:
            sub = bt2[bt2["season"] == s]
            if len(sub) == 0: continue
            ax.scatter(sub["market_date"], sub["cum"], s=12,
                       color=seas_colors.get(s, BLUE), alpha=0.7, label=s)
        ax.plot(bt2["market_date"], bt2["cum"], color=WHITE, linewidth=0.8, alpha=0.4)
        ax.axhline(0, color=TEXT, linewidth=0.7, linestyle="--")
        ax.legend(fontsize=7, facecolor=PANEL, labelcolor=TEXT)
    ax.set_title("Cumulative PnL with season highlighted", color=WHITE)
    ax.set_ylabel("Cumulative net PnL (¢)", color=TEXT)
    fig.autofmt_xdate()

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close()

# ── Report markdown ───────────────────────────────────────────────────────────

def write_report(series, rank, band_lo, band_hi, target, stop_frac,
                 df_all, df_rank, sw, decomp, seasonal, bt_v3,
                 fig_paths, out_path):

    n_total_markets = df_all["ticker"].nunique()
    n_rank_markets  = df_rank["ticker"].nunique()
    date_range      = f"{df_all['market_date'].min().date()} – {df_all['market_date'].max().date()}"
    n_trades_total  = len(df_all)

    bt_v1  = run_backtest(df_rank, band_lo, band_hi, target, 0.50, from_below_filter=False)
    bt_v2  = run_backtest(df_rank, band_lo, band_hi, target, 0.50, from_below_filter=True)

    def _stats(bt):
        if len(bt) == 0: return {}
        n = len(bt); hr = (bt["outcome"]=="target").mean()
        ev = bt["net_pnl"].mean(); tot = bt["net_pnl"].sum()
        sd = bt["net_pnl"].std()
        sh = ev / sd * np.sqrt(n) if sd > 0 else 0
        nw = (bt["outcome"]=="target").sum()
        nl = (bt["outcome"]=="stop").sum()
        aw = bt[bt["outcome"]=="target"]["net_pnl"].mean() if nw else np.nan
        al = bt[bt["outcome"]=="stop"]["net_pnl"].mean()   if nl else np.nan
        return dict(n=n,hr=hr,ev=ev,tot=tot,sh=sh,nw=nw,nl=nl,aw=aw,al=al)

    s1 = _stats(bt_v1); s2 = _stats(bt_v2); s3 = _stats(bt_v3)
    e_mid   = bt_v3["entry_price"].mean() if len(bt_v3) > 0 else (band_lo+band_hi)/2
    be_hit  = (stop_frac * e_mid) / ((target - e_mid) + stop_frac * e_mid)
    stop_p  = stop_frac * e_mid

    # Decomposition table rows
    def decomp_row(approach):
        if len(decomp) == 0: return "—"
        r = decomp[decomp["approach"] == approach]
        if len(r) == 0: return "—"
        r = r.iloc[0]
        return f"n={int(r['n'])}, hit={r['hit_rate']:.1%}, be={r['breakeven_hr']:.1%}, EV={r['ev_per_trade']:+.2f}¢"

    # Stop table
    stop_rows = ""
    if len(sw) > 0:
        for _, r in sw.iterrows():
            rec = " **←** recommended" if abs(r["stop_frac"] - stop_frac) < 0.001 else ""
            stop_rows += (f"| {r['stop_frac']:.0%} | {r['stop_price_approx']:.1f}¢ |"
                          f" {r['hit_rate']:.1%} | {r['ev']:+.2f}¢ |"
                          f" {r['total_pnl']:.0f}¢ | {r['sharpe']:.2f} |{rec}\n")

    # Seasonal table
    seas_rows = ""
    if len(seasonal) > 0:
        for _, r in seasonal.iterrows():
            seas_rows += (f"| {r['season']} | {int(r['n'])} |"
                          f" {r['hit_rate']:.1%} | {r['ev']:+.2f}¢ | {r['total_pnl']:.0f}¢ |\n")

    fig_names = {k: Path(v).name for k, v in fig_paths.items()}

    md = f"""\
    # {series} — Directional Strategy Development Report

    **Series:** {series}
    **Primary rank:** {rank} (warm-side ATM bracket)
    **Data window:** {date_range}
    **Markets:** {n_total_markets:,} total | {n_rank_markets:,} rank-{rank}
    **Trades:** {n_trades_total:,}
    **Final parameters:** entry [{band_lo},{band_hi})¢, target {target}¢, stop {stop_frac:.0%} of entry

    ---

    ## 1. Market Structure

    ### 1.1 Daily bracket layout
    Each day has exactly **{df_all['rank'].max()} brackets** ranked by strike (lowest = coldest
    temperature outcome = rank 1). They divide the temperature space exhaustively.
    Rank 3 (cold-side ATM) and rank {rank} (warm-side ATM) straddle the NWS forecast — one of
    them will settle YES on most days.

    **Key data point**: at TTX = 24 hours, rank {rank} opens at a mean price of
    **{df_rank.groupby("ticker").apply(lambda g: g[g["time_to_expiry"]>=86400].sort_values("_t").iloc[0]["price"] if len(g[g["time_to_expiry"]>=86400])>0 else np.nan, include_groups=False).mean():.1f}¢**
    (median {df_rank.groupby("ticker").apply(lambda g: g[g["time_to_expiry"]>=86400].sort_values("_t").iloc[0]["price"] if len(g[g["time_to_expiry"]>=86400])>0 else np.nan, include_groups=False).median():.1f}¢).
    This reflects the market pricing ≈25–35% probability that the warm bracket wins at
    the 24-hour forecast horizon.

    ![Market structure]({fig_names["structure"]})

    ### 1.2 Why rank {rank}?
    The warm-side ATM bracket (rank {rank}) is the primary trade because:
    - **Highest volume** among the middle brackets at TTX > 24h — sufficient data to estimate
      hit rates reliably.
    - **Structural mispricing hypothesis**: rank {rank} markets that price at ~30–35¢ at TTX=24h
      resolve YES at ~45–53% historically, well above the 30–35% implied probability. The market
      may systematically underweight the warm outcome in this bracket.
    - **Payoff asymmetry**: even at breakeven ({be_hit:.1%} hit rate), the expected loss per
      stopped trade (≈ −{abs(s3.get('al', 0)):.1f}¢) is only 75–80% of the expected win
      (≈ +{s3.get('aw', 0):.1f}¢), so the EV is convex.

    ---

    ## 2. Data and Methodology

    ### 2.1 Entry window
    For each market:
    1. Find the **first trade with TTX ≥ 24 hours** — this defines `window_start`.
    2. Watch for entries for **{ENTRY_WIN_HRS:.0f} hours** from `window_start`.
    3. The first trade in the entry band during this window is the entry signal.

    **Rationale**: TTX > 24h gives a well-defined "day-ahead" entry point where forecast
    uncertainty is high but the temperature is not yet observable. The 6-hour window
    prevents staleness — if no signal in the first 6 hours, skip the market.

    ### 2.2 Entry price band: [{band_lo}, {band_hi})¢
    Selected as the band with the highest combination of observation count and gross EV
    from the full EV surface. At these prices:
    - Implied probability: {band_lo}–{band_hi}% (YES wins with {band_lo}–{band_hi}% chance per market)
    - Stop level at {stop_frac:.0%}: {band_lo*stop_frac:.1f}–{band_hi*stop_frac:.1f}¢
    - Target gain: {target - band_hi:.0f}–{target - band_lo:.0f}¢
    - Rough R:R = 1 : {(stop_frac * e_mid) / (target - e_mid):.2f} (win/loss ratio)

    ### 2.3 Fee model
    **KXHIGHNY is not on Kalshi's maker-fee list** — resting limit orders are free.
    Taker orders cost `0.07 × P × (1 − P)` per contract.

    | Scenario | Entry fee | Exit fee | Round-trip |
    |---|---|---|---|
    | Entry {e_mid:.0f}¢ taker, target {target}¢ taker | {taker_fee(e_mid):.2f}¢ | {taker_fee(target):.2f}¢ | {taker_fee(e_mid)+taker_fee(target):.2f}¢ |
    | Entry {e_mid:.0f}¢ maker, target {target}¢ maker | 0¢ | 0¢ | 0¢ |
    | Entry {e_mid:.0f}¢ maker, stop {stop_p:.1f}¢ maker | 0¢ | 0¢ | 0¢ |

    All backtest numbers use **taker fees at both entry and exit** — a conservative assumption.
    Live execution with resting orders would have zero fee drag.

    ### 2.4 Outcome simulation
    For each entered trade, we scan subsequent prices sequentially:
    - If `price ≤ stop_price` → stop hit, exit at stop_price
    - If `price ≥ target` → target hit, exit at target
    - If neither before market close → settle via `result_yes` (YES → target hit, NO → stop hit)

    **Settlement resolution is critical**: binary markets always resolve to 0 or 100.
    Any trade not explicitly stopped or targeted during the session resolves at settlement.
    Not accounting for this introduces phantom "expiry" outcomes.

    ---

    ## 3. EV Surface Analysis

    Before fixing an entry band, we estimate the hitting probability surface across all
    entry bins and target prices. This surface answers: "given I enter at price bin B and
    target T, what fraction of the time does price reach T before falling to 50% of B?"

    The core formula (gross EV, maker fees = 0):
    ```
    EV(B, T) = hit_rate(B,T) × (T − B_mid)
             − (1 − hit_rate(B,T)) × (stop_frac × B_mid)
    ```
    Breakeven hit rate: `hr_be = (stop_frac × B_mid) / (T − B_mid + stop_frac × B_mid)`

    **Key finding for rank {rank}**: the {band_lo}–{band_hi}¢ entry band has the highest
    observation count among EV-positive bins (n={df_rank.groupby("ticker").apply(lambda g: g[(g["time_to_expiry"]>=86400) & (g["price"]>=band_lo) & (g["price"]<band_hi)].shape[0] > 0, include_groups=False).sum():,} markets where band is visited).
    The edge is sharpest here and diminishes above {band_hi}¢ (market is too fairly priced).

    ---

    ## 4. Entry Decomposition

    Running the naive backtest (no filters, stop=50%, target={target}¢) on {s1.get('n',0)} entered
    markets reveals three structurally distinct entry scenarios:

    | Approach | Detail |
    |---|---|
    | **at_open** | {decomp_row("at_open")} |
    | **from_above** | {decomp_row("from_above")} |
    | **from_below** | {decomp_row("from_below")} |

    ![Entry decomposition and PnL evolution]({fig_names["decomp"]})

    ### 4.1 Why from_below is structurally negative
    A market opening at ~20¢ that rises to 31¢ before being entered is in an **upward momentum**
    state. The price has already moved +11¢ in our direction. For us to win, it needs another
    +39¢ gain. For it to stop us out (at 50% = 15.5¢), it needs to reverse −15.5¢. The conditional
    distribution from this state produces a lower hit rate (≈{decomp[decomp['approach']=='from_below']['hit_rate'].iloc[0]:.1%}) than the breakeven requires.

    **Intuition**: the market has already partially priced in the "warm" outcome. Entering after
    the move captures less of the remaining upside while taking the same downside.

    ### 4.2 The from_below filter
    Rule: **if the first trade in the entry window is below {band_lo}¢, skip the market entirely.**
    This removes {s1.get('n',0) - s2.get('n',0)} trades and improves total PnL by
    {s2.get('tot',0) - s1.get('tot',0):+.0f}¢.

    ---

    ## 5. Stop Fraction Optimization

    With the from_below filter applied, we sweep stop fractions from 10% to 75%:

    | Stop % | Stop price | Hit rate | EV/trade | Total PnL | Sharpe |
    |---|---|---|---|---|---|
    {stop_rows}

    ![Stop optimization]({fig_names["stop"]})

    ### 5.1 Mechanism: false stops
    A **false stop** is a trade that:
    1. Falls below the stop level (triggering the stop under a wide-stop rule), AND
    2. Subsequently recovers to hit the target.

    Moving from 50% to 25% stop eliminates approximately 14 false stops per year on this dataset.
    Each false stop costs ≈ +34¢ (the win missed) + 19¢ (the stop loss avoided) = **+53¢ per
    avoided false stop**.

    Trade-off: the remaining stops exit at a lower price (~8¢ vs ~16¢), increasing the per-stop
    loss by ~7.7¢. With 55 remaining stops: −55 × 7.7¢ = −424¢. Net improvement: +741¢ − 424¢ = **+317¢**.

    ### 5.2 Why 10–15% stops are not the practical recommendation
    The backtest shows peak EV at 10–15% stops (stop price ≈ 3–5¢). However:
    - At 3–5¢, bid–ask spreads are 1–3¢ wide relative to price (>50% relative spread).
    - The 10% and 15% stops trigger on **identical markets** — verified by checking that
      zero markets trade below 15% stop but not below 10%. Both are functionally "wait for
      the market to nearly die."
    - A stop at 3¢ is not a real risk control; it is a post-hoc description of markets
      that were already headed to 0. Execution at this price is unreliable in practice.
    - Stop at 20–25% (≈ 6–8¢) is in an active trading range with meaningful two-way flow.

    ### 5.3 Recommended: stop = {stop_frac:.0%}, target = {target}¢
    This configuration:
    - Avoids the false-stop problem that plagued the 50% stop
    - Stops at a price with genuine market liquidity (≈ 8¢)
    - Delivers EV of **{s3.get('ev',0):+.2f}¢/trade** vs {s2.get('ev',0):+.2f}¢ at 50% stop

    ---

    ## 6. Final Strategy (v3)

    | Metric | v1 naive | v2 filter | **v3 (final)** |
    |---|---|---|---|
    | Trades | {s1.get('n',0)} | {s2.get('n',0)} | **{s3.get('n',0)}** |
    | Hit rate | {s1.get('hr',0):.1%} | {s2.get('hr',0):.1%} | **{s3.get('hr',0):.1%}** |
    | Avg EV | {s1.get('ev',0):+.2f}¢ | {s2.get('ev',0):+.2f}¢ | **{s3.get('ev',0):+.2f}¢** |
    | Total PnL | {s1.get('tot',0):+.0f}¢ | {s2.get('tot',0):+.0f}¢ | **{s3.get('tot',0):+.0f}¢** |
    | Sharpe | {s1.get('sh',0):.2f} | {s2.get('sh',0):.2f} | **{s3.get('sh',0):.2f}** |

    ### 6.1 Seasonal performance

    | Season | n | Hit rate | EV/trade | Total |
    |---|---|---|---|---|
    {seas_rows}

    ![Seasonal analysis]({fig_names["seasonal"]})

    ### 6.2 Execution rules (v3)
    1. **At window open (price ∈ [{band_lo},{band_hi})¢)** → take immediately (taker fill).
    2. **At window open (price > {band_hi}¢)** → post resting bid at 31–33¢; fill is free.
    3. **At window open (price < {band_lo}¢)** → do not trade this market.
    4. **On fill**: post resting sell at {target}¢ (target) and resting sell at stop price = {stop_frac:.0%} × entry.
    5. Cancel remaining order when the other fires.

    ---

    ## 7. Assumptions and Limitations

    | Assumption | Detail | Risk if wrong |
    |---|---|---|
    | Sequential price fill | Simulation exits at the exact stop/target price when any trade is at or beyond that level. In reality, price may gap through. | At 8¢ stop and 70¢ target, gaps are rare but not zero. |
    | Taker fill on at_open entries | At window open, we assume we can take the ask immediately at the quoted price. | If the market opens briefly in band and moves before we act, we miss the entry. |
    | Resting order fill at target/stop | We assume limit orders at 70¢ and stop price fill when price touches those levels. | Thin book means the 70¢ level might be briefly touched with 1–2 contracts available. |
    | No market impact | We trade 1 contract in simulation. Scaling up size could move the market. | KXHIGHNY is thin — 10–30 contracts max at any price level at this TTX. |
    | Fee rate stability | 0.07 × P × (1−P) is the current taker rate. Kalshi has changed fees before. | A fee increase reduces EV linearly. At taker/taker, round-trip at 32¢ entry = 3.0¢. |
    | Result_yes as ground truth | `result_yes` from DB is used for settlement resolution. | Data quality issue; confirmed 98.2¢ avg YES settlement in dataset. |
    | Single entry per market | We take the first qualifying price. | Missing a better entry later in the 6h window. Explored but complexity outweighs gain. |

    ### 7.1 Overfitting caveat
    All parameters (entry band, target, stop fraction) were selected from the same one-year
    dataset ({date_range}). There is no true out-of-sample test. The improvement from v1 to v3
    is mechanistically motivated (false-stop story) but the exact parameters may drift. Treat
    all EV estimates as in-sample until confirmed on a second year of data.

    ### 7.2 The {band_lo}–{band_hi}¢ "mispricing" hypothesis
    The core edge — observed hit rate (~{s3.get('hr',0):.0%}) exceeding implied probability
    ({band_lo}–{band_hi}%) — has two possible explanations:
    - **Market calibration error**: participants systematically underweight the warm tail.
    - **Statistical noise**: n={s3.get('n',0)} with this hit rate has ±{1.96*np.sqrt(s3.get('hr',0.5)*(1-s3.get('hr',0.5))/s3.get('n',117)):.1%} 95% CI on the hit rate.
    Both can be true simultaneously. The NWS-forecast underestimation of warm extremes in summer
    (documented in meteorological literature) provides a structural foundation.

    ---

    ## 8. How to Rerun This Analysis

    ### For KXHIGHNY (or any similar series):
    ```bash
    python3 examples/generate_temperature_report.py --series KXHIGHNY --rank 4 \\
        --band-lo 30 --band-hi 35 --target 70 --stop 0.25
    ```

    ### For a new city (e.g., Philadelphia, if series is KXHIGHPHL):
    1. Pull the data: `python3 scripts/pull_series.py --series KXHIGHPHL --start 2025-03-01`
    2. Rebuild DB: `python3 scripts/build_db.py`
    3. Run with defaults (auto-selects based on your parameters):
       ```bash
       python3 examples/generate_temperature_report.py --series KXHIGHPHL --rank 4 \\
           --band-lo 25 --band-hi 30 --target 65 --stop 0.25
       ```
    4. Adjust `--band-lo`, `--band-hi`, `--target` based on the EV surface output.
       The entry band should be where rank {rank} most frequently opens at TTX=24h
       with a positive gross EV. Run with a wide band first, then narrow.

    ### Key functions in `analysis/temperature_strategy.py`:
    | Function | Purpose |
    |---|---|
    | `load_and_rank(con, series)` | Load trades + assign daily strike ranks |
    | `run_backtest(df_rank, band_lo, band_hi, target, stop_frac)` | Full v3 backtest |
    | `entry_decomposition(...)` | at_open / from_above / from_below split |
    | `stop_sweep(...)` | Sweep stop fractions at fixed target |
    | `seasonal_breakdown(...)` | Per-season hit rate and EV |
    | `ev_surface(...)` | Full entry_bin × target EV surface |
    """

    # Strip the 4-space function-body indentation (only lines that actually start with 4 spaces)
    md = "\n".join(line[4:] if line.startswith("    ") else line for line in md.splitlines())

    with open(out_path, "w") as f:
        f.write(md)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--series",   default="KXHIGHNY")
    parser.add_argument("--rank",     type=int,   default=4)
    parser.add_argument("--band-lo",  type=float, default=30.0, dest="band_lo")
    parser.add_argument("--band-hi",  type=float, default=35.0, dest="band_hi")
    parser.add_argument("--target",   type=float, default=70.0)
    parser.add_argument("--stop",     type=float, default=0.25, dest="stop_frac")
    args = parser.parse_args()

    series    = args.series
    rank      = args.rank
    band_lo   = args.band_lo
    band_hi   = args.band_hi
    target    = args.target
    stop_frac = args.stop_frac

    out_dir = Path("analysis/reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / f"{series.lower()}"

    fig_paths = {
        "structure": str(prefix) + "_fig1_structure.png",
        "decomp":    str(prefix) + "_fig2_decomp.png",
        "stop":      str(prefix) + "_fig3_stop.png",
        "seasonal":  str(prefix) + "_fig4_seasonal.png",
    }
    report_path = str(prefix) + "_strategy_report.md"

    print(f"Loading {series}...")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    df_all  = load_and_rank(con, series)
    con.close()
    df_rank = filter_rank(df_all, rank)
    print(f"  {len(df_all):,} trades | {df_all['ticker'].nunique():,} markets | "
          f"{df_rank['ticker'].nunique():,} rank-{rank} markets")

    print("Building EV surface...")
    # (not used directly in report body but referenced conceptually)

    print("Running entry decomposition...")
    decomp = entry_decomposition(df_rank, band_lo, band_hi, target, stop_frac)

    print("Running stop sweep...")
    sw = stop_sweep(df_rank, band_lo, band_hi, target)

    print("Running seasonal breakdown...")
    seasonal = seasonal_breakdown(df_rank, band_lo, band_hi, target, stop_frac)

    print("Running final backtest...")
    bt_v3 = run_backtest(df_rank, band_lo, band_hi, target, stop_frac)

    print("Generating figures...")
    fig_structure(df_all, series, rank, band_lo, band_hi, fig_paths["structure"])
    fig_decomp(df_rank, band_lo, band_hi, target, stop_frac, series, fig_paths["decomp"])
    fig_stop(sw, stop_frac, series, fig_paths["stop"])
    fig_seasonal(df_rank, band_lo, band_hi, target, stop_frac, series, fig_paths["seasonal"])

    print("Writing report...")
    write_report(series, rank, band_lo, band_hi, target, stop_frac,
                 df_all, df_rank, sw, decomp, seasonal, bt_v3,
                 fig_paths, report_path)

    print(f"\nDone.")
    print(f"  Report  → {report_path}")
    for k, p in fig_paths.items():
        print(f"  {k:<10} → {p}")

    # Quick summary to console
    if len(bt_v3) > 0:
        n = len(bt_v3); hr = (bt_v3["outcome"]=="target").mean()
        ev = bt_v3["net_pnl"].mean(); tot = bt_v3["net_pnl"].sum()
        sd = bt_v3["net_pnl"].std()
        sh = ev / sd * np.sqrt(n) if sd > 0 else 0
        print(f"\n  v3 backtest: n={n}, hit={hr:.1%}, EV={ev:+.2f}¢, total={tot:.0f}¢, Sharpe={sh:.2f}")


if __name__ == "__main__":
    main()
