"""
Aggregate live trade logs into a cumulative P&L report.

Reads all logs/live_*.jsonl files and produces:
  1. Daily equity curve — cumulative P&L by date
  2. Per-setup OOS vs backtest — hit rate, EV/trade vs backtest benchmark
  3. Max drawdown from peak equity
  4. Daily breakdown table

Usage:
    python3 scripts/trade_report.py
    python3 scripts/trade_report.py --since 2026-04-01
"""

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Backtest benchmarks (EV/trade in cents, from strategy report Section 3)
# Source: analysis/reports/multi_city_strategy_report.md
# ---------------------------------------------------------------------------

BACKTEST_EV: dict[str, dict] = {
    # setup_key (series-season-rank) → {ev_cents, sharpe, n_backtest}
    "KXHIGHNY-Spring-r5":   {"ev": 8.44,  "sharpe": 1.77, "n": 39},
    "KXHIGHNY-Summer-r4":   {"ev": 11.13, "sharpe": 1.70, "n": 21},
    "KXHIGHNY-Fall-r4":     {"ev": 8.94,  "sharpe": 1.82, "n": 38},
    "KXHIGHPHIL-Summer-r4": {"ev": 6.96,  "sharpe": 3.52, "n": 44},
    "KXHIGHPHIL-Fall-r4":   {"ev": 7.46,  "sharpe": 3.92, "n": 42},
    "KXHIGHLAX-Spring-r3":  {"ev": 7.09,  "sharpe": 2.37, "n": 34},
    "KXHIGHLAX-Summer-r3":  {"ev": 7.31,  "sharpe": 2.77, "n": 39},
    "KXHIGHLAX-Fall-r4":    {"ev": 6.06,  "sharpe": 1.82, "n": 25},
    "KXHIGHLAX-Winter-r4":  {"ev": 10.14, "sharpe": 3.27, "n": 34},
    "KXHIGHCHI-Fall-r3":    {"ev": 12.98, "sharpe": 1.99, "n": 30},
    "KXHIGHCHI-Winter-r3":  {"ev": 11.62, "sharpe": 2.67, "n": 46},
    "KXHIGHMIA-Spring-r4":  {"ev": 7.45,  "sharpe": 2.35, "n": 34},
    "KXHIGHMIA-Fall-r5":    {"ev": 8.45,  "sharpe": 3.38, "n": 55},
}

# Map series prefix → series name for benchmark lookup
_SERIES_MAP = {
    "KXHIGHNY":   "KXHIGHNY",
    "KXHIGHPHIL": "KXHIGHPHIL",
    "KXHIGHLAX":  "KXHIGHLAX",
    "KXHIGHCHI":  "KXHIGHCHI",
    "KXHIGHMIA":  "KXHIGHMIA",
}

BOLD  = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[92m"
RED   = "\033[91m"
DIM   = "\033[2m"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _parse_date_from_path(path: Path) -> date | None:
    try:
        return datetime.strptime(path.stem.replace("live_", ""), "%Y%m%d").date()
    except ValueError:
        return None


def load_daily_summaries(log_dir: Path, since: date | None) -> list[dict]:
    """
    Read all logs/live_*.jsonl files, extract daily_summary events.
    Returns list of {date, total_pnl_cents, setups} sorted by date.
    """
    records = []
    for path in sorted(log_dir.glob("live_*.jsonl")):
        file_date = _parse_date_from_path(path)
        if file_date is None:
            continue
        if since and file_date < since:
            continue

        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") == "daily_summary":
                    records.append({
                        "date":            file_date,
                        "total_pnl_cents": event.get("total_pnl_cents", 0.0),
                        "setups":          event.get("setups", []),
                    })

    return sorted(records, key=lambda r: r["date"])


def _series_from_ticker(ticker: str) -> str:
    return ticker.rsplit("-", 2)[0]


def _benchmark_key(series: str, season: str, rank: int) -> str:
    return f"{series}-{season}-r{rank}"


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def report(log_dir: Path, since: date | None) -> None:
    summaries = load_daily_summaries(log_dir, since)

    if not summaries:
        print("No trade logs found" + (f" since {since}" if since else "") + ".")
        print(f"Log directory: {log_dir.resolve()}")
        return

    # -----------------------------------------------------------------------
    # 1. Per-setup aggregation
    # -----------------------------------------------------------------------
    setup_stats: dict[str, dict] = {}

    for day in summaries:
        for s in day["setups"]:
            ticker  = s.get("ticker", "")
            outcome = s.get("outcome", "")
            pnl     = s.get("net_pnl_cents")
            series  = _series_from_ticker(ticker)

            # Build a key: series-season (we don't store rank in summary, use series+season)
            season = s.get("season") or "?"  # engine doesn't include season in setup rows
            key = f"{series}-{season}"

            if key not in setup_stats:
                setup_stats[key] = {
                    "series":    series,
                    "season":    season,
                    "n_entered": 0,
                    "n_filtered": 0,
                    "n_target":  0,
                    "n_stop":    0,
                    "n_settle_yes": 0,
                    "n_settle_no":  0,
                    "total_pnl": 0.0,
                }
            st = setup_stats[key]

            if outcome == "filtered":
                st["n_filtered"] += 1
            elif outcome in ("target", "stop", "settlement_yes", "settlement_no",
                             "settlement_unknown", "no_fill"):
                st["n_entered"] += 1
                if outcome == "target":
                    st["n_target"] += 1
                elif outcome == "stop":
                    st["n_stop"] += 1
                elif outcome == "settlement_yes":
                    st["n_settle_yes"] += 1
                elif outcome == "settlement_no":
                    st["n_settle_no"] += 1
                if pnl is not None:
                    st["total_pnl"] += pnl

    # -----------------------------------------------------------------------
    # 2. Equity curve
    # -----------------------------------------------------------------------
    cum_pnl   = 0.0
    peak_pnl  = 0.0
    max_dd    = 0.0
    eq_rows   = []
    for day in summaries:
        cum_pnl += day["total_pnl_cents"]
        if cum_pnl > peak_pnl:
            peak_pnl = cum_pnl
        dd = peak_pnl - cum_pnl
        if dd > max_dd:
            max_dd = dd
        eq_rows.append((day["date"], day["total_pnl_cents"], cum_pnl))

    # -----------------------------------------------------------------------
    # Print
    # -----------------------------------------------------------------------
    total_days   = len(summaries)
    total_trades = sum(st["n_entered"] for st in setup_stats.values())
    total_pnl    = cum_pnl

    print(f"\n{BOLD}{'='*72}")
    print(f"  TRADE REPORT" + (f"  (since {since})" if since else f"  ({total_days} days)"))
    print(f"{'='*72}{RESET}")
    print(f"  Days traded:  {total_days}")
    print(f"  Total trades: {total_trades}")
    print(f"  Total P&L:    {_pnl_str(total_pnl)}  (${total_pnl/100:+.4f})")
    print(f"  Max drawdown: {max_dd:.1f}¢  (${max_dd/100:.4f})")

    # Daily table
    print(f"\n{BOLD}  DAILY EQUITY CURVE{RESET}")
    print(f"  {'Date':12s}  {'Day P&L':>10s}  {'Cumulative':>12s}  {'Drawdown':>10s}")
    print(f"  {'─'*50}")
    running_peak = 0.0
    running_cum  = 0.0
    for day in summaries:
        running_cum += day["total_pnl_cents"]
        running_peak = max(running_peak, running_cum)
        dd_row = running_peak - running_cum
        dd_str = f"{dd_row:.1f}¢" if dd_row > 0.1 else "—"
        print(f"  {str(day['date']):12s}  {_pnl_str(day['total_pnl_cents']):>10s}  "
              f"{_pnl_str(running_cum):>12s}  {dd_str:>10s}")

    # Per-setup table
    print(f"\n{BOLD}  PER-SETUP  (OOS vs backtest){RESET}")
    hdr = (f"  {'Setup':24s}  {'n':>4s}  {'filt%':>6s}  {'hit%':>6s}  "
           f"{'EV/tr':>7s}  {'bench':>7s}  {'vs':>6s}")
    print(hdr)
    print(f"  {'─'*72}")

    for key, st in sorted(setup_stats.items()):
        n        = st["n_entered"]
        n_fil    = st["n_filtered"]
        n_win    = st["n_target"] + st["n_settle_yes"]
        filt_pct = n_fil / (n + n_fil) * 100 if (n + n_fil) > 0 else 0.0
        hit_pct  = n_win / n * 100 if n > 0 else 0.0
        ev       = st["total_pnl"] / n if n > 0 else 0.0

        # Lookup benchmark — try common rank combinations
        bench_ev = None
        for rank in [3, 4, 5]:
            bkey = _benchmark_key(st["series"], st["season"], rank)
            if bkey in BACKTEST_EV:
                bench_ev = BACKTEST_EV[bkey]["ev"]
                break

        vs_str = ""
        if bench_ev is not None and n > 0:
            diff = ev - bench_ev
            vs_str = f"{diff:+.1f}¢"
            vs_str = (GREEN if diff >= 0 else RED) + vs_str + RESET

        bench_str = f"{bench_ev:.1f}¢" if bench_ev is not None else "  —"
        ev_str    = _pnl_str(ev) if n > 0 else "   —"

        label = f"{st['series']}/{st['season']}"
        print(f"  {label:24s}  {n:>4d}  {filt_pct:>5.0f}%  {hit_pct:>5.0f}%  "
              f"{ev_str:>7s}  {bench_str:>7s}  {vs_str:>6s}")

    if total_trades == 0:
        print(f"\n  {DIM}No completed trades yet.{RESET}")

    print()


def _pnl_str(pnl: float) -> str:
    color = GREEN if pnl > 0 else (RED if pnl < 0 else "")
    return f"{color}{pnl:+.1f}¢{RESET}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kalshi strategy trade report")
    parser.add_argument("--since", default=None,
                        help="Only include logs since this date (YYYY-MM-DD)")
    parser.add_argument("--log-dir", default="logs",
                        help="Directory containing live_*.jsonl files (default: logs/)")
    args = parser.parse_args()

    since_date = None
    if args.since:
        try:
            since_date = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            print(f"Invalid date format: {args.since} (expected YYYY-MM-DD)")
            sys.exit(1)

    report(Path(args.log_dir), since_date)
