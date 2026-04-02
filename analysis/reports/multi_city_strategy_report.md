# Temperature High Markets — Multi-City Directional Strategy

**Last updated:** 2026-03-28
**Cities:** New York (KXHIGHNY), Philadelphia (KXHIGHPHIL), Los Angeles (KXHIGHLAX), Chicago (KXHIGHCHI), Miami (KXHIGHMIA)
**Data window:** 2025-03-21 – 2026-03-29 | ~10,800 markets | ~5.4M trades
**Strategy type:** Directional — buy YES on a specific temperature bracket when it opens cheap at TTX > 24h

---

## 1. Strategy Summary

Each daily temperature-high series has 6 brackets ranked by strike (rank 1 = coldest, rank 6 = hottest). Ranks 3 and 4 straddle the NWS forecast — one resolves YES on most days. The core observation is that **specific rank/season combinations open systematically below fair value at TTX > 24h**, creating a repeatable directional edge.

### 1.1 Universal rules (apply to every city and season)

**From_below filter — always active:**
If the first trade in the entry window (TTX ≥ 24h) is below the band's lower bound, skip the market entirely. A market that opens below band and then rises into it is in an upward momentum state; entering after the move captures less upside while taking the same downside. This filter is negative EV in every city and every season tested, with one minor exception noted in Miami rank 4 Spring (n=9, too small to override the universal rule).

**Entry window:**
- Window opens at the first trade with TTX ≥ 24 hours.
- Watch for 6 hours from window open. If no qualifying trade in that window, skip the market.
- Take the first trade that falls in the entry band.

**NY Spring exception — at_open only:**
For the NY Spring rank 5 setup, only enter if the window opens directly inside the band [10, 15). If the first trade is above 15¢ (from_above), do not chase down. This exception does not apply to any other city or season.

**Fee model:**
Taker fee = `0.07 × P × (1 − P)` per contract (P in dollars). **All cities have no maker fee** — resting limit orders are free across the board.

All EV figures in this document use **taker fees at both entry and exit** — a conservative floor. Since all bracket and stop/target orders are resting limits in live execution, actual round-trip fees will be zero on the exit leg and often zero on the entry leg (from_above entries always rest; at_open entries may take). The backtested EV figures understate live performance by approximately 1–2¢ per trade depending on entry type.

---

## 2. Trade Calendar

The right column is the action. Blank = not listed = not analyzed; "SKIP" = analyzed and negative/marginal.

| Season | NY | Philadelphia | Los Angeles | Chicago | Miami |
|--------|----|----|----|----|-----|
| **Spring** (Mar–May) | Rank 5, at_open only | SKIP | Rank 3 | SKIP | Rank 4 |
| **Summer** (Jun–Aug) | Rank 4 | Rank 4 | Rank 3 | SKIP | SKIP |
| **Fall** (Sep–Nov) | Rank 4 | Rank 4 | Rank 4 | Rank 3 | Rank 5 + Rank 4 (½ size) |
| **Winter** (Dec–Feb) | SKIP | SKIP | Rank 4 | Rank 3 | SKIP |

---

## 3. Parameters by City and Season

### 3.1 New York (KXHIGHNY)

**Series:** KXHIGHNY | **Liquidity:** ~1.44M trades, highest volume city

| Season | Rank | Band | Target | Stop | n | EV/trade | Sharpe |
|--------|------|------|--------|------|---|----------|--------|
| Spring | 5 | [10, 15)¢ | 70¢ | 25% | 39 | +8.44¢ | 1.77 |
| Summer | 4 | [30, 35)¢ | 70¢ | 25% | 21 | +11.13¢ | 1.70 |
| Fall   | 4 | [30, 35)¢ | 70¢ | 25% | 38 | +8.94¢  | 1.82 |
| Winter | — | — | — | — | — | — | SKIP |

**Why skip NY Winter:** Rank 4 Sharpe = 0.22 in winter (n=24, EV=+1.37¢). The warm-bracket mispricing evaporates — the market prices the warm outcome more accurately in winter, eliminating the edge.

**NY Spring note:** Rank 5 is the very cheap bracket (extreme heat outcome). The at_open filter is critical — from_above and from_below are both negative. Only enter if the window opens directly in [10, 15)¢.

**NY Summer note:** n=21 is one summer of data. The EV (+11.13¢) is the highest of any NY season but the sample is thin. Trade at normal size but do not extrapolate magnitude.

### 3.2 Philadelphia (KXHIGHPHIL)

**Series:** KXHIGHPHIL | **Liquidity:** ~558K trades, ~40% of NY volume

Philly rank 4 opens at ~32¢ median (vs NY 26¢) — closer to fair value. This shifts the optimal trade from a large directional call (70¢ target) to a modest "push to toss-up" (50¢ target). The wider stop (60%) reflects the more balanced two-sided risk at higher entry prices.

| Season | Rank | Band | Target | Stop | n | EV/trade | Sharpe |
|--------|------|------|--------|------|---|----------|--------|
| Spring | — | — | — | — | — | — | SKIP |
| Summer | 4 | [30, 35)¢ | 50¢ | 60% | 44 | +6.96¢ | 3.52 |
| Fall   | 4 | [30, 35)¢ | 50¢ | 60% | 42 | +7.46¢  | 3.92 |
| Winter | — | — | — | — | — | — | SKIP |

**Why skip Philly Spring:** All rank/approach combinations are negative. The NY rank 5 spring edge (structural mispricing) does not appear in Philadelphia.

**Why skip Philly Winter:** Sharpe = 1.48 on a single winter (n=36). Positive but insufficient evidence — directional on one data point. Re-evaluate after Winter 2026–27.

**Philly size note:** Use smaller initial size given ~40% of NY liquidity. More patience required for limit order fills.

### 3.3 Los Angeles (KXHIGHLAX)

**Series:** KXHIGHLAX | **Liquidity:** ~1.48M trades, most liquid city

LA requires a **rank inversion by season** — rank 3 (moderate bracket) in Spring/Summer, rank 4 (warm bracket) in Fall/Winter. The physical driver is the marine layer effect (suppresses warm extremes in summer, making the moderate bracket win more often) and the Santa Ana wind pattern (drives warm extremes in fall/winter, making rank 4 win).

| Season | Rank | Band | Target | Stop | n | EV/trade | Sharpe |
|--------|------|------|--------|------|---|----------|--------|
| Spring | 3 | [35, 40)¢ | 55¢ | 25% | 34 | +7.09¢ | 2.37 |
| Summer | 3 | [35, 40)¢ | 55¢ | 25% | 39 | +7.31¢  | 2.77 |
| Fall   | 4 | [30, 35)¢ | 50¢ | 25% | 25 | +6.06¢  | 1.82 |
| Winter | 4 | [25, 30)¢ | 50¢ | 25% | 34 | +10.14¢ | 3.27 |

**LA Fall note:** n=25, one season (Fall 2025 only). Sharpe=1.82 with 80% hit rate — the directional signal is strong but the sample is a single fall. Trade at half-size until Fall 2026 confirms.

**LA Summer note:** n=39, all from Summer 2025 (no 2026 summer data yet). Strong metrics (Sharpe=2.77, HR=84.6%) but Summer 2026 will be the first true out-of-sample test.

### 3.4 Chicago (KXHIGHCHI)

**Series:** KXHIGHCHI | **Liquidity:** ~977K trades, thinnest city

Chicago is structurally different: **rank 4 is universally negative** across all seasons and band configurations. Rank 3 at a low entry band (23–29¢) is the only viable setup. The high targets (75–85¢) and wide stops (50–60%) reflect greater pre-resolution price noise in Chicago temperature markets.

| Season | Rank | Band | Target | Stop | n | EV/trade | Sharpe |
|--------|------|------|--------|------|---|----------|--------|
| Spring | — | — | — | — | — | — | SKIP |
| Summer | — | — | — | — | — | — | SKIP |
| Fall   | 3 | [23, 29)¢ | 85¢ | 50% | 30 | +12.98¢ | 1.99 |
| Winter | 3 | [23, 29)¢ | 75¢ | 60% | 46 | +11.62¢  | 2.67 |

**Why skip Chicago Spring:** at_open HR = 14%, EV = −13¢. Structurally broken — spring pricing appears to correctly reflect the cold-bias in Chicago forecasting.

**Why skip Chicago Summer:** Sharpe = 1.04 on one summer (n=30). The at_open component is negative; only from_above works. Insufficient evidence to commit capital.

**Chicago Winter robustness:** The only Chicago season with two partial years of data — 2025 (n=12, +16.88¢) and 2026 (n=34, +9.76¢). Direction is consistent even if magnitude differs. Most reliable Chicago entry.

---

### 3.5 Miami (KXHIGHMIA)

**Series:** KXHIGHMIA | **Liquidity:** ~922K trades | **Structure:** Variable brackets per day (313 have 6, 39 have 5, 15 have 4) — unique among the five cities

Miami's climate is subtropical: warm year-round with a pronounced wet/hot season (summer) and a drier, still-warm fall. This produces two distinct mispricings:
- **Spring**: rank 4 (the warm-moderate bracket) opens cheaply because the market underweights how often Miami temperatures land in the moderate-warm range during the transition to hot season.
- **Fall**: both rank 4 and rank 5 open cheaply because the market underweights how persistently warm Miami stays through September–November due to the warm Atlantic. Rank 5 (the "extra-hot" bracket) is the primary trade.

Rank 3 is universally negative across all seasons — the cool-moderate bracket almost never wins in a subtropical climate, but the market prices it higher than warranted.

| Season | Rank | Band | Target | Stop | n | EV/trade | Sharpe | Notes |
|--------|------|------|--------|------|---|----------|--------|-------|
| Spring | 4 | [25, 33)¢ | 50¢ | 25% | 34 | +7.45¢ | 2.35 | Two springs of data |
| Summer | — | — | — | — | — | — | SKIP | Rank 4 EV = −7.15¢ |
| Fall   | 5 | [13, 27)¢ | 45¢ | 25% | 55 | +8.45¢ | 3.38 | Strongest signal |
| Fall   | 4 | [25, 33)¢ | 45¢ | 25% | 18 | +7.44¢ | 2.63 | Half size — n=18 only |
| Winter | — | — | — | — | — | — | SKIP | Sharpe=1.61, below threshold |

**Why skip Miami Summer:** Rank 4 EV = −7.15¢ (Sharpe=−1.04). Miami summers are so reliably hot that the warm bracket is correctly or over-priced — no edge.

**Why skip Miami Winter:** Sharpe = 1.61 on one winter (n=30). Consistent threshold with Philly Winter (also skipped). Revisit after Winter 2026–27.

**Miami Spring robustness:** Two springs of data (2025: n=13, +4.49¢; 2026: n=21, +9.28¢). Both positive and the 2026 EV is higher — strongest multi-year confirmation in the dataset. Most reliable Miami entry.

**Miami Fall Rank 5 note:** HR = 65.5% at 45¢ target on n=55 (one fall). The from_below filter still applies (HR drops to 25% for from_below entries). All 55 trades are from Fall 2025; Fall 2026 will be the first true out-of-sample test. Trade at normal size given strong Sharpe but monitor closely.

**Miami Fall Rank 4 note:** n=18, one fall. Trade at half size until confirmed.

---

## 4. Performance Summary

Approximate annual PnL estimates, using seasonal sample sizes × EV/trade × 365/season-length as a rough annualization. Treat these as directional guidance, not projections.

| City | Active seasons | Est. annual trades | Est. annual PnL (¢) |
|------|---------------|-------------------|---------------------|
| NY | Spring (r5) + Summer + Fall | ~98 | ~860¢ |
| Philadelphia | Summer + Fall | ~86 | ~620¢ |
| Los Angeles | All 4 | ~132 | ~1,020¢ |
| Chicago | Fall + Winter | ~76 | ~1,180¢ |
| Miami | Spring + Fall (r5 + r4 ½) | ~107 | ~1,050¢ |
| **Total** | | **~499** | **~4,730¢** |

At 1 contract per market: ~$47.30/year gross on $1 notional per contract. Scale linearly with contract size subject to liquidity constraints (see Section 7).

All figures use taker fees at both legs — a conservative floor. Live execution with resting orders will outperform by ~1–2¢/trade on average.

---

## 5. Implementation — Manual Daily Execution

This section describes how to run the strategy by hand, checking markets and placing orders manually each day. It is the correct way to start before building automation — it forces familiarity with market behavior and validates fill assumptions.

### 5.1 Daily routine

**Step 1 — Morning setup (once per day):**
1. Open the Kalshi markets page or query the API for active markets in each active series.
2. Identify the 6 brackets for today's date in each city you are trading.
3. Assign ranks (1 = lowest strike, 6 = highest strike) and note which rank you are watching (see trade calendar).
4. Note the market's close time. Your entry window opens at close_time − 24h.

**Step 2 — Identify the entry window:**
The window opens at the moment the market's TTX first crosses below 25 hours. In practice, for daily temperature markets this is typically **mid-to-late morning the day before close**, but confirm per series by checking when you first see TTX ≈ 24h in historical data.

Set a reminder for window_open time. You have 6 hours from that moment to act. If you miss the first 6 hours, do not enter.

**Step 3 — At window open, check the opening price:**

Look at the current market price (or the first trade price after window_open):

| Condition | Action |
|-----------|--------|
| Price < band_lo | **Skip** — from_below filter. Do not trade this market today. |
| Price ∈ [band_lo, band_hi) | **Enter immediately** — take the ask (market order or aggressive limit). |
| Price ≥ band_hi | **Post resting bid** at a price within the band (e.g., band_lo + 1¢ to band_hi − 1¢). Wait up to 6h for fill. If not filled by window_close, cancel and skip. |

*NY Spring rank 5 exception:* only enter if price ∈ [10, 15)¢ at window open. If it opens above 15¢, skip regardless.

**Step 4 — On fill, post bracket orders:**
Immediately after your entry fills at price `E`:
- Post a resting **sell limit at target price** (T from the table above).
- Post a resting **sell limit at stop price** = `stop_frac × E` (round to nearest integer cent).

Both orders are resting limit sells. The first to fill exits the position. Cancel the other immediately after one fills.

*Example — Chicago Winter entry at 26¢:*
- Stop = 0.60 × 26 = 15.6¢ → post sell limit at 16¢
- Target = 75¢ → post sell limit at 75¢
- Wait. Market settles at 0 or 100 if neither fires before close.

**Step 5 — Settlement:**
If neither stop nor target fires before close, the market settles binary (0¢ or 100¢). The position is automatically resolved. This counts as a target hit (if YES) or stop hit (if NO) in the P&L — no action needed.

### 5.2 Daily checklist (one card per city)

```
Each active market day:
[ ] Note today's rank to watch per city (from trade calendar)
[ ] Note window_open time for each market (close_time − 24h)
[ ] At window_open: check opening price
    [ ] Below band → skip, note "filtered (from_below)"
    [ ] In band → take ask, post bracket orders
    [ ] Above band → post resting bid in band, set 6h cancel reminder
[ ] After fill: confirm both bracket orders are live
[ ] At window_close (6h later): cancel any unfilled entry bids
[ ] At market close: record outcome (target / stop / settlement)
```

### 5.3 Record keeping

Log every market, whether traded or not:
```
date | city | rank | window_open_price | action | entry | stop | target | outcome | net_pnl
```

The from_below filter rate and hit rates should track the backtest numbers within a few percentage points over 20+ trades. Any significant divergence (>10pp) warrants investigation before scaling.

---

## 6. Implementation — Automated Execution

Automation is the target state. It removes the manual window-monitoring burden, eliminates execution latency, and allows scaling across all four cities simultaneously. The architecture below describes how the live system should be built using the existing `api/` and `strategies/` modules.

### 6.1 Architecture overview

```
WebSocket feed (trade + ticker channels)
        │
        ▼
  MarketMonitor
  (tracks TTX per ticker, detects window_open)
        │
        ▼
  StrategyEngine
  (applies city/season/rank rules → OrderIntent)
        │
        ▼
  RiskManager
  (pre-trade checks: position limits, daily loss limit)
        │
        ▼
  KalshiClient (REST)
  (submits/cancels orders)
        │
        ▼
  PositionTracker
  (tracks open positions, bracket orders, P&L)
```

### 6.2 Core state machine per market

Each active market goes through the following states:

```
PENDING → WINDOW_OPEN → [FILTERED | ENTERED] → [STOPPED | TARGETED | SETTLED]
```

- **PENDING**: market is active but TTX > 25h. No action.
- **WINDOW_OPEN**: first trade with TTX ≥ 24h observed. Classify opening price.
- **FILTERED**: opening price < band_lo (from_below). Record and discard.
- **ENTERED**: qualifying entry filled. Bracket orders live.
- **STOPPED / TARGETED / SETTLED**: exit triggered. Record outcome.

The 6-hour window expiry is handled by scheduling a cancel check at `window_open + 6h` for any markets where a resting entry bid is still pending.

### 6.3 City/season configuration

Strategy parameters are stored as a config dict keyed by `(series, season)`. The engine looks up the active config at window_open time based on the current calendar month.

```python
CONFIGS = {
    ('KXHIGHNY',   'Spring'): {'rank': 5, 'band_lo': 10, 'band_hi': 15, 'target': 70, 'stop_frac': 0.25, 'at_open_only': True},
    ('KXHIGHNY',   'Summer'): {'rank': 4, 'band_lo': 30, 'band_hi': 35, 'target': 70, 'stop_frac': 0.25},
    ('KXHIGHNY',   'Fall'):   {'rank': 4, 'band_lo': 30, 'band_hi': 35, 'target': 70, 'stop_frac': 0.25},
    ('KXHIGHPHIL', 'Summer'): {'rank': 4, 'band_lo': 30, 'band_hi': 35, 'target': 50, 'stop_frac': 0.60},
    ('KXHIGHPHIL', 'Fall'):   {'rank': 4, 'band_lo': 30, 'band_hi': 35, 'target': 50, 'stop_frac': 0.60},
    ('KXHIGHLAX',  'Spring'): {'rank': 3, 'band_lo': 35, 'band_hi': 40, 'target': 55, 'stop_frac': 0.25},
    ('KXHIGHLAX',  'Summer'): {'rank': 3, 'band_lo': 35, 'band_hi': 40, 'target': 55, 'stop_frac': 0.25},
    ('KXHIGHLAX',  'Fall'):   {'rank': 4, 'band_lo': 30, 'band_hi': 35, 'target': 50, 'stop_frac': 0.25},
    ('KXHIGHLAX',  'Winter'): {'rank': 4, 'band_lo': 25, 'band_hi': 30, 'target': 50, 'stop_frac': 0.25},
    ('KXHIGHCHI',  'Fall'):   {'rank': 3, 'band_lo': 23, 'band_hi': 29, 'target': 85, 'stop_frac': 0.50},
    ('KXHIGHCHI',  'Winter'): {'rank': 3, 'band_lo': 23, 'band_hi': 29, 'target': 75, 'stop_frac': 0.60},
    ('KXHIGHMIA',  'Spring'): {'rank': 4, 'band_lo': 25, 'band_hi': 33, 'target': 50, 'stop_frac': 0.25},
    ('KXHIGHMIA',  'Fall'):   {'rank': 5, 'band_lo': 13, 'band_hi': 27, 'target': 45, 'stop_frac': 0.25},
    # Rank 4 Fall also active at half size — handled by position sizing, not config
}
```

Any `(series, season)` pair not in this dict is inactive — the engine skips it.

### 6.4 Entry logic

```python
async def on_window_open(market: MarketState, config: dict, client: KalshiClient):
    open_price = market.first_window_trade_price

    # from_below filter
    if open_price < config['band_lo']:
        market.state = 'FILTERED'
        return

    # NY Spring at_open_only exception
    if config.get('at_open_only') and open_price >= config['band_hi']:
        market.state = 'FILTERED'
        return

    if config['band_lo'] <= open_price < config['band_hi']:
        # Take immediately — taker fill
        await client.create_order(
            ticker=market.ticker, side='yes', action='buy',
            count=SIZE, price=open_price, order_type='limit'
        )
    else:
        # From_above: post resting bid inside the band
        bid_price = config['band_hi'] - 1
        await client.create_order(
            ticker=market.ticker, side='yes', action='buy',
            count=SIZE, price=bid_price, order_type='limit'
        )
        market.schedule_cancel(at=market.window_open + timedelta(hours=6))
```

### 6.5 Bracket order management

On fill confirmation (via `order_fill` WebSocket channel):

```python
async def on_entry_fill(fill: FillEvent, config: dict, client: KalshiClient):
    entry_price = fill.price
    stop_price  = round(config['stop_frac'] * entry_price)
    target_price = config['target']

    # Post both legs as resting limit sells
    stop_order   = await client.create_order(..., price=stop_price,   side='yes', action='sell')
    target_order = await client.create_order(..., price=target_price, side='yes', action='sell')

    market.bracket = BracketState(stop_order.id, target_order.id)
```

On either bracket leg filling, cancel the other:

```python
async def on_exit_fill(fill: FillEvent, market: MarketState, client: KalshiClient):
    other_id = market.bracket.other_order(fill.order_id)
    await client.cancel_order(other_id)
    market.record_outcome(fill)
```

### 6.6 Risk controls

Before any order is submitted, `RiskManager` enforces:

| Limit | Default | Rationale |
|-------|---------|-----------|
| Max open positions | 4 (1 per city) | One trade per city at a time |
| Max contracts per order | 10 | Thin books; avoid moving the market |
| Daily loss limit | 15% of account | Derived from Monte Carlo: p95 annual drawdown at quarter-Kelly is 34.5% spread over ~499 trades. A 15% daily stop prevents a single bad day from breaching the annual worst case in one session. |
| Max position delta | 40 YES contracts | Gross directional exposure cap |

These are hard stops implemented in `risk/manager.py`. Any limit breach raises `InsufficientMarginError` and the order is not submitted.

### 6.7 Monitoring and alerting

The live system should log to a structured file (JSON lines) and emit alerts on:
- Window open events (info)
- Entries and exits (info + P&L)
- Any 429 rate limit or API error (warning)
- Daily loss limit breach (critical — halt)
- Any order that fails to cancel (critical — manual intervention required)

A daily summary email/message: markets observed, filtered, entered, exited, net P&L.

### 6.8 Startup sequence

```bash
# 1. Ensure data is current
python3 scripts/pull_series.py --series KXHIGHNY   --start $(date -d '2 days ago' +%Y-%m-%d)
python3 scripts/pull_series.py --series KXHIGHPHIL --start $(date -d '2 days ago' +%Y-%m-%d)
python3 scripts/pull_series.py --series KXHIGHLAX  --start $(date -d '2 days ago' +%Y-%m-%d)
python3 scripts/pull_series.py --series KXHIGHCHI  --start $(date -d '2 days ago' +%Y-%m-%d)
python3 scripts/build_db.py

# 2. Start the live engine (to be built)
python3 scripts/live_engine.py
```

---

## 7. Risk Analysis — Monte Carlo Simulation

### 7.1 Methodology

We bootstrap the actual backtest P&L records rather than fitting a parametric model. For each simulation: resample each setup's trade pool independently with replacement (same n as in-sample), apply the simultaneous-position cap (total deployed capital ≤ 50% of account on any given day), compute the full equity curve, and record total P&L and maximum drawdown. 10,000 simulations per sizing scheme.

**Why bootstrap:** We have the actual P&L distributions — no need to assume a shape. The bootstrap automatically captures skewness and fat tails, and block-resampling by trade date preserves within-day cross-city correlation (measured pairwise phi < 0.2 for most city pairs; highest is Philly/MIA at +0.43 on n=28 shared trade days — low enough to treat as approximately independent).

All simulations use a **$100 starting account**.

---

### 7.2 P&L Distribution

![Annual P&L Distribution](chart_pnl_distribution.png)

| Sizing scheme | p5 | p25 | p50 | p75 | p95 |
|---|---|---|---|---|---|
| Fixed 1 contract | $34.82 | $39.71 | $43.03 | $46.53 | $51.32 |
| Fixed $10/trade | $1,258 | $1,483 | $1,633 | $1,795 | $2,016 |
| Quarter-Kelly | $1,150 | $1,295 | $1,395 | $1,495 | $1,636 |
| Half-Kelly | $2,175 | $2,455 | $2,645 | $2,838 | $3,108 |

The fixed-1-contract scheme produces a p50 P&L of $43.03 on a $100 account (43% return). This is the calibration baseline — zero sizing risk, used to verify the edge exists before committing real capital. The narrow band [$34.82, $51.32] for p5–p95 confirms that the edge is consistent and not driven by a few outlier trades.

Quarter-Kelly and half-Kelly both produce substantially higher P&L with more dispersion. The P&L distributions are relatively tight (p95/p5 ratio ≈ 1.4) reflecting the large number of independent trades (~499/year).

---

### 7.3 Drawdown Distribution

![Max Drawdown CDF](chart_drawdown_cdf.png)

| Sizing scheme | p50 drawdown | p75 | p95 | P(DD>10%) | P(DD>20%) | P(DD>30%) | P(DD>50%) | P(ruin) |
|---|---|---|---|---|---|---|---|---|
| Fixed 1 contract | 1.1% | 1.4% | 1.9% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| Fixed $10/trade | 32.2% | 47.4% | 85.2% | 98.9% | 80.2% | 54.9% | 22.0% | 0.0% |
| Quarter-Kelly | 15.8% | 21.6% | 34.5% | 86.5% | 30.4% | 8.9% | 0.8% | 0.0% |
| Half-Kelly | 24.6% | 35.0% | 59.2% | 97.4% | 65.4% | 35.9% | 9.4% | 0.0% |

No sizing scheme produces a nonzero probability of ruin (account < $10). The edge is durable enough that even in adverse bootstrap draws the account does not collapse.

Fixed $10/trade has the worst drawdown profile — 22% probability of a 50%+ drawdown on a $100 account. This is effectively a leveraged bet and should not be used.

---

### 7.4 Sizing recommendations

**Phase 1 — Calibration (current):** 1 contract per trade. Maximum risk per trade ≈ entry price (e.g., 26¢ = $0.26 at stake). Validates fill assumptions, entry timing, and from_below filter rate against backtest benchmarks. Run for at least 50 trades across multiple cities and seasons before scaling.

**Phase 2 — Real account (after OOS confirmation):** Quarter-Kelly. Median drawdown of 15.8% is operationally manageable; p95 drawdown of 34.5% is the realistic worst case over a full year. P(ruin) = 0%. The p50 annual return at quarter-Kelly is ~1,395% on a $100 account — or equivalently, at larger account sizes, the annual P&L scales proportionally.

**Phase 3 — Scaled (after two seasons of OOS data):** Half-Kelly. Median drawdown of 24.6%; p95 of 59.2% is the stress case. Only step to half-Kelly after the quarter-Kelly phase confirms out-of-sample Sharpe ratios are consistent with backtest.

**Half-Kelly fractions by setup** (fraction of account per trade, applied to starting account balance):

| Setup | Half-Kelly | Setup | Half-Kelly |
|-------|-----------|-------|-----------|
| NY Spring r5 | 5.3% | LA Winter | 25.4% |
| NY Summer | 16.3% | CHI Fall | 11.6% |
| NY Fall | 13.0% | CHI Winter | 12.5% |
| Philly Summer | 24.1% | MIA Spring | 20.4% |
| Philly Fall | 27.0% | MIA Fall r5 | 19.7% |
| LA Spring | 23.6% | MIA Fall r4 | 16.2% (½ × full) |
| LA Summer | 25.7% | | |
| LA Fall | 21.4% | | |

Fractions are pre-computed from `f = (p×b − (1−p)) / b` using in-sample hit rates and average win/loss magnitudes, then halved. Quarter-Kelly = half of the values above.

---

### 7.5 Daily loss limit

Based on the quarter-Kelly drawdown profile: set the daily hard stop at **15% of account** (e.g., $15 on a $100 account). This corresponds to approximately 3 standard losing trades in a single day and is consistent with the p95 drawdown being 34.5% spread over a full year of ~499 trades.

Update this limit in `risk/manager.py` once the account size is finalized for live trading.

---

## 9. Assumptions and Risk Factors

| Assumption | Detail | Risk if wrong |
|---|---|---|
| Sequential price fill | Backtest exits at exact stop/target price when any trade touches that level. | Price may gap through stop (thin book). Most relevant for Chicago at 23–29¢ entry with wide stops. |
| Taker fill on at_open entries | Assume ask is available at quoted price at window open. | Market may open briefly in band and move before order arrives; latency matters. |
| Resting order fill | Limit orders at target/stop fill when price touches those levels. | Thin books mean the target may be briefly touched with only 1–2 contracts available. Monitor fill rates. |
| No market impact | Backtest uses 1-contract simulation. | Kalshi markets are thin at TTX>24h. 10–30 contracts is realistic max per level. Scale slowly. |
| Fee stability | `0.07 × P × (1−P)` current taker rate. | Fee increases reduce EV linearly. At 32¢ entry / 70¢ target (NY), round-trip taker fee = ~3.0¢. |
| From_below filter | Relies on observing the window-open price before entry. | Automated: requires WebSocket trade feed to be live at window_open. Any feed gap could cause a missed filter. |

### 9.1 Overfitting caveat

All parameters were selected from a single year of data (2025–2026). No true out-of-sample test exists. Every seasonal estimate rests on approximately one occurrence of that season. The mechanistic justifications (marine layer for LA rank inversion, false-stop story for NY stop choice) provide structural grounding, but parameter magnitudes should be treated as provisional until confirmed on a second year.

Re-run the parameter sweeps after accumulating Summer 2026 and Fall 2026 data.

### 9.2 Single-season flags

The following setups have only one observed season and should be traded at reduced size until a second season confirms:

| Setup | n | Sharpe | Status |
|-------|---|--------|--------|
| NY Summer (rank 4) | 21 | 1.70 | One summer |
| LA Fall (rank 4) | 25 | 1.82 | One fall |
| LA Summer (rank 3) | 39 | 2.77 | One summer |
| Chicago Fall (rank 3) | 30 | 1.99 | One fall |
| Miami Fall rank 4 | 18 | 2.63 | One fall — half size |
| Miami Fall rank 5 | 55 | 3.38 | One fall — full size, monitor |

Most reliable entries (multiple seasons or large n): Miami Spring (two springs, n=34), Chicago Winter (two partial winters, n=46), Philly Summer/Fall (n=42–44 each).

---

## 10. Analysis Reproduction

```bash
# Pull and build (all cities)
python3 scripts/pull_series.py --series KXHIGHNY   --start 2025-03-01
python3 scripts/pull_series.py --series KXHIGHPHIL --start 2025-03-01
python3 scripts/pull_series.py --series KXHIGHLAX  --start 2025-03-01
python3 scripts/pull_series.py --series KXHIGHCHI  --start 2025-03-01
python3 scripts/build_db.py
```

```python
# Reproduce any city/season backtest
import duckdb
from analysis.temperature_strategy import load_and_rank, filter_rank, run_backtest, SEASON_MAP

con = duckdb.connect('data/processed/kalshi.duckdb')
df  = load_and_rank(con, 'KXHIGHCHI')
df['season'] = df['market_date'].dt.month.map(SEASON_MAP)

d  = filter_rank(df, rank=3)
ds = d[d['season'] == 'Winter']
bt = run_backtest(ds, band_lo=23, band_hi=29, target=75, stop_frac=0.60)
print(bt['net_pnl'].describe())
```

Key functions in `analysis/temperature_strategy.py`:

| Function | Purpose |
|---|---|
| `load_and_rank(con, series)` | Load trades and assign daily strike ranks |
| `filter_rank(df, rank)` | Filter to a single rank |
| `run_backtest(df, band_lo, band_hi, target, stop_frac)` | Full backtest with from_below filter |
| `entry_decomposition(df, ...)` | at_open / from_above / from_below split |
| `stop_sweep(df, ...)` | Sweep stop fractions at fixed target |
| `target_sweep(df, ...)` | Sweep target prices at fixed stop |
| `seasonal_breakdown(df, ...)` | Per-season hit rate and EV |
