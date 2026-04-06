# Kalshi Multi-City Temperature Strategy — Project State
**Last updated:** 2026-04-04  
**Changes (2026-04-04):** RAM upgraded to 8 GB. `build_db.py` streaming fix (OOM on LAX). `status.py` now shows TTX + live bid/ask/last for today's setups and a tomorrow's markets section (24–48h TTX window). `scripts/bot.py` added — Telegram command bot responding to `status`. `assign_rank()` now includes B brackets (previously T-only). VPS push access enabled via `~/.ssh/id_ed25519`.
**Status:** Live infrastructure deployed. Engine running 1-contract sizing on VPS. 5 cities active.
**Data window:** 2025-03-21 – 2026-04-03 | ~10,800 markets | ~5.44M trades across all cities
**See also:** `analysis/reports/project_state_claude.md` — dense working notes, code fragments, open hypotheses

---

## 1. Infrastructure

### Stack
- **Storage**: DuckDB + Parquet (columnar, zstd). One `trades.parquet` and `markets.parquet` per series under `data/processed/{SERIES}/`.
- **DuckDB views** (`data/processed/kalshi.duckdb`): `trades`, `markets`, `trades_with_result`, `series_summary`. Views use glob so adding a new series requires zero schema changes.
- **Raw source**: one JSONL file per market under `data/raw/{SERIES}/`. Never delete — rebuild DB from these.

### Data pipeline
```
python3 scripts/pull_series.py --series KXHIGHNY --start 2025-03-01   # fetch raw
python3 scripts/build_db.py                                             # rebuild parquet + duckdb
```
`pull_series.py` skips existing JSONL files (resume-safe). Concurrency=2, exponential backoff on 429.

### API auth
- RSA-PSS, SHA-256, MGF1, salt_length=32.
- Sign the **full path** including the `/trade-api/v2` prefix: `ts_ms + METHOD + /trade-api/v2/path`.
- Trades endpoint: `GET /markets/trades?ticker=TICKER` (not `/markets/{ticker}/trades`).
- Base URL: `https://api.elections.kalshi.com/trade-api/v2`

### Known build issues
- `pd.to_datetime()` must use `format='ISO8601'` — older data has microsecond timestamps (`2025-11-03T22:26:40.657397Z`) that break the default parser. Fixed in `build_db.py`.
- Large series (e.g. KXHIGHLAX, 344 MB raw) previously OOM'd during `build_db.py` on low-RAM VPS. Fixed: `build_trades()` now streams one JSONL file at a time via `pyarrow.parquet.ParquetWriter` instead of accumulating all chunks in RAM before concat.

---

## 2. Market Structure (KXHIGHNY)

### Daily bracket structure
Exactly **6 brackets per day** (confirmed across the full dataset):
- **Rank 1**: Always a `T` market — extreme cold (e.g., "high < 35°F"). Median open at TTX>24h ≈ 7¢. Rare event.
- **Ranks 2–5**: Always `B` (between) markets, ordered by strike ascending (cold → hot).
- **Rank 6**: Always a `T` market — extreme heat (e.g., "high > 85°F"). Median open ≈ 3¢. Very rare.

Rank 3 and rank 4 together bracket the NWS forecast — one will be the winning bracket most days. Entry price distribution at TTX>24h (full year, n=2,137 markets with TTX>24h entry):

| Rank | Mean entry | Median | Std | Note |
|---|---|---|---|---|
| 1 | 13¢ | 7¢ | 17¢ | T_low: extreme cold |
| 2 | 24¢ | 22¢ | 13¢ | B_cold |
| 3 | 38¢ | 38¢ | 15¢ | Cold-side ATM |
| 4 | 25¢ | 26¢ | 13¢ | Warm-side ATM |
| 5 | 11¢ | 9¢ | 11¢ | B_hot |
| 6 | 7¢ | 4¢ | 11¢ | T_high: extreme heat |

**Important**: by TTX=24h, most B markets have drifted away from 50¢ as temperature outcomes become more certain. Rank 3 and 4 median entries are 38¢ and 26¢ respectively — well off the 50¢ ATM level they'd have at market creation.

### Volume and settlement
- `T` markets trade 2.1× the contract volume of `B` markets despite half the count.
- Settlement accuracy: yes-markets average 98.2¢, no-markets 1.2¢. `result` field is reliable ground truth.

---

## 3. Microstructure Findings

Full report: `analysis/reports/microstructure_findings_kxhighny.md`

**Entry window analysis (2026-04-05):** `analysis/reports/entry_window_analysis.md` — 36h vs 24h entry window study (LAX, NYC, MIA) + 2D sweep (start TTX × duration) across all 5 cities. Conclusion: current 24h/6h window is near-optimal; no change recommended.

### Key numbers
| Metric | Value | Implication |
|---|---|---|
| % volume in 1–24h TTX | 81% | Focus quoting activity here |
| % volume in >24h TTX | 17% | Sparse; where directional entry lives |
| Lag-1 autocorr (pooled) | −0.341 | Strong mean reversion / bid-ask bounce |
| Kyle's λ (global) | ~0.0001 | ~10k contracts to move price 1¢ |
| Yes-taker share of volume | 74% of contracts | Yes-side is more aggressive |
| Peak trading hour (UTC) | ~18–21 | NYC high being recorded at Central Park |

### Volatility term structure
- Highest vol in 1–24h window (forecast updates + observed conditions)
- Drops sharply near expiry (outcome known)
- Low vol far from expiry (>24h): favorable for directional entry, adverse selection low

### Spread decomposition (proxy only — no live orderbook)
- Numbers computed from consecutive trade prices, not true quoted spread.
- Negative global adverse selection is bid-ask bounce artifact, not signal.
- **Need live orderbook snapshots for true decomposition.**

### Price convergence
- Markets "commit" (p > 90 or p < 10) predominantly in the 1–4h TTX window.
- Before commitment: genuine two-sided uncertainty → MM opportunity.
- After commitment: only mechanical/directional flow.

---

## 4. Fee Schedule (Kalshi, effective Feb 5, 2026)

### Formulas (per contract, P = price in dollars)
```
Taker fee : 0.07  × P × (1-P)   [max 1.75¢ at 50¢]
Maker fee : 0.0175 × P × (1-P)  [max 0.4375¢ at 50¢; only on designated markets]
Free maker: 0¢                   [resting orders on markets NOT on maker-fee list]
```

### KXHIGHNY fee status (confirmed)
**KXHIGHNY is NOT on the maker-fee list.** Resting limit orders (maker fills) cost **zero** on both entry and exit. Taker orders (immediately crossing the spread) cost `0.07 × P × (1-P)`.

### Practical implication for the directional strategy
- Post a limit bid at the target entry price → fill is free.
- Post a limit sell at the target and a limit sell at the stop → both exits are free.
- **All-in cost = $0 if executed entirely as resting orders.**
- The strategy is viable wherever gross EV > 0, with no fee hurdle to clear.

### Round-trip cost reference (if forced to take)
| Entry | Exit | Round-trip (taker/taker) |
|---|---|---|
| 15¢ | 35¢ | 0.89¢ + 1.59¢ = 2.48¢ |
| 13¢ | 50¢ | 0.79¢ + 1.75¢ = 2.54¢ |
| 35¢ | 55¢ | 1.59¢ + 1.73¢ = 3.32¢ |

---

## 5. Directional Strategy: Rank-Based Bracket Trade

### Conceptual framework
Rather than entering at the opening print, we:
1. Identify markets by their **rank** (1–6, strike-ordered within each day).
2. Condition on the **entry price bin** at the TTX>24h window.
3. Estimate **conditional hitting probability** P(price reaches T before stop=0.5×E | rank, entry_bin).
4. Compute **gross EV = P_hit × (T − E) − P_stop × (0.5 × E)**. Zero fees (KXHIGHNY maker-free).
5. Enter only where EV is positive and exceeds a minimum threshold.

### Simulation methodology
- **Stop**: 50% of entry price.
- **Settlement resolution**: binary markets always resolve to 0 or 100. Any target not hit during trading is resolved using `result_yes`: YES → target hit, NO → stop hit. "Expiry" outcomes are structurally impossible for a strategy held to settlement. Early versions of the simulation incorrectly produced ~10–24% expiry rates for ranks 1, 5, 6 because sparse trade data at low prices sometimes skipped over the stop price. Fixed by passing `result_yes` to `_simulate_multi_target`.
- **Entry**: first trade with TTX > 24h per market, binned in 5¢ buckets.

### EV by rank × entry bin (full year, n=2,137 markets, fees=0)

Best target and gross EV for each rank's core entry range (n≥15):

**Rank 4 (warm-side ATM) — PRIMARY TRADE:**
| Entry bin | n | Best target | Hit rate | Breakeven hit | Gross EV |
|---|---|---|---|---|---|
| 0–5¢ | 23 | 50¢ | 43.5% | 2.6% | +19.43¢ |
| 5–10¢ | 40 | 75¢ | 20.0% | 5.3% | +10.24¢ |
| 10–15¢ | 35 | 60¢ | 25.7% | 11.6% | +7.13¢ |
| 15–20¢ | 41 | 60¢ | 22.0% | 17.1% | +1.98¢ |
| 20–25¢ | 40 | 40¢ | 42.5% | 39.1% | +0.46¢ |
| 25–30¢ | 47 | 60¢ | 38.3% | 29.7% | +3.77¢ |
| **30–35¢** | **85** | **70¢** | **43.5%** | **30.2%** | **+6.81¢** |
| 35–40¢ | 25 | 45¢ | 64.0% | 71.4% | −2.68¢ |

Edge cliff at 35¢: observed hit rate drops below breakeven. Do not trade rank 4 above 35¢ entry.

**Rank 3 (cold-side ATM) — SECONDARY TRADE:**
| Entry bin | n | Best target | Hit rate | Breakeven hit | Gross EV |
|---|---|---|---|---|---|
| 20–25¢ | 29 | 85¢ | 31.0% | 15.3% | +11.57¢ |
| 25–30¢ | 46 | 45¢ | 56.5% | 44.0% | +3.30¢ |
| 30–35¢ | 61 | 45¢ | 73.8% | 56.5% | +4.42¢ |
| **35–40¢** | **84** | **75¢** | **42.9%** | **33.3%** | **+4.57¢** |
| 40–45¢ | 40 | 75¢ | 47.5% | 39.5% | +4.26¢ |
| 45–50¢ | 36 | 55¢ | 50.0% | 76.0% | −8.85¢ |
| 60–65¢ | 21 | 80¢ | 23.8% | 64.1% | −19.79¢ |

Edge cliff at 45¢: hit rate falls sharply below breakeven. Do not trade rank 3 above 45¢ entry.

**Rank 2 (B_cold):** Positive EV across 0–35¢ entry range. Best buckets: 5–10¢ (+6.22¢, n=38) and 15–20¢ (+5.11¢, n=44). Lower liquidity than ranks 3–4.

**Ranks 1, 6:** Marginal or negative across all bins. Do not trade directionally.

**Rank 5:** Positive EV in 0–5¢ (n=120, +5.48¢) and 10–20¢ (n=114, +5–6¢) bins. Worth monitoring but secondary to ranks 3–4.

### The 0–5¢ "lottery ticket" entries
Entries below 5¢ show very high nominal EV across all ranks due to mechanical payoff asymmetry: the stop loss is essentially zero (0.5 × 2¢ = 1¢), so almost any positive hit rate gives positive EV. These are structurally different from the main trades — the risk per contract is negligible but **liquidity at 1–4¢ is extremely thin**. Fill rates will be poor and market impact significant. Treat as a separate, small-size allocation rather than part of the core strategy.

### Seasonal analysis — v3 strategy (with from_below filter, fees included)

Using the v3 methodology (from_below filtered, `taker_fee = 0.07×P×(1-P)`, result_yes resolution):

**Rank 4 v3 (band 30-35¢, target 70¢, stop 25%):**
| Season | n | Hit rate | Net EV | Total PnL | Sharpe |
|---|---|---|---|---|---|
| Summer | 21 | 62% | +11.13¢ | +234¢ | ~1.7 |
| Fall | 38 | 58% | +8.94¢ | +340¢ | ~1.8 |
| Spring | 34 | 47% | +1.90¢ | +65¢ | ~0.4 |
| Winter | 24 | 46% | +1.37¢ | +33¢ | ~0.2 |

Summer and Fall are the core seasons. Spring and Winter are weak but positive.

**Seasonal strategy — proposed setups (2026-03-27 analysis):**

| Season | Strategy | Band | Target | Stop | n | Net EV | Sharpe |
|---|---|---|---|---|---|---|---|
| Summer | Rank 4 v3 | 30-35¢ | 70¢ | 25% | 21 | +11.13¢ | ~1.7 |
| Fall | Rank 4 v3 | 30-35¢ | 70¢ | 25% | 38 | +8.94¢ | ~1.8 |
| Spring | **Rank 5 at_open** | 10-15¢ | 70¢ | 25% | 39 | **+8.44¢** | **1.77** |
| Winter | Rank 4 v3 (reduced size) | 30-35¢ | 70¢ | 25% | 24 | +1.37¢ | 0.22 |

**Rank 5 Spring setup — key details:**

Rank 5 is the 2nd hottest bracket (median opening 10¢ in spring). Strategy: when rank 5 opens at 10-15¢ during the entry window (at_open condition), buy and target 70¢ with a 25% stop at ~3¢.

- Entry decomposition (band 10-15): at_open n=39 EV=+8.44¢; from_above n=10 EV=−4.32¢; from_below n=17 EV=−1.11¢
- **Filter: at_open only**. Exclude from_above and from_below.
- 41% of spring rank-5 markets trigger an entry (39/95 markets).
- Cross-season: works Spring (+8.44¢) and Summer (+10.24¢), fails Fall (−2.60¢) and Winter (−4.93¢). Use only in Spring (and Summer as supplement).
- Physical story: rank 5 opens at 10-15¢ because spring NYC is usually mild. On warm-spell days the price runs from 10-15¢ toward settlement — we catch the first leg (10-15¢ → 70¢). 28% hit rate vs 6% breakeven = large margin.

**Winter — no robust alternative found:**

The rank 4 target=50¢ variant showed EV=+5.64¢ (n=24) in aggregate, but split 2025 winter (n=8, −2.08¢) vs 2026 winter (n=16, +9.50¢). Two winters with opposite signs — not reliable. Running rank 4 v3 at reduced size is the honest recommendation until more winter data accumulates.

**Sample size warning (critical):** The dataset spans ~1.5 years. Each season appears once. Every seasonal number has massive uncertainty — treat all seasonal conclusions as directional hypotheses, not confirmed edges. Need 2-3 more years of data for seasonal parameterization confidence.

### Earlier opening-price strategy results (superseded)
The original strategy (always enter at opening print, flat 3¢/leg fee) showed:
- All-taker fees (corrected to `0.07×P×(1-P)`): best net EV = −1.40¢ (target 50¢, train)
- The strategy is fee-limited on a taker basis; viable only with maker entry (now confirmed free)
- B brackets far outperform T brackets; T brackets should be excluded

---

## 6. Analysis Modules

### `analysis/hitting_probability.py`
- `_simulate_multi_target(after_prices, targets, stop, last_price, result_yes)`: single-pass O(n_trades + n_targets) multi-target simulation. Pending targets at end of trade stream are resolved via `result_yes` (True→target hit, False→stop hit). **`result_yes` must always be passed** — the None fallback exists only for testing.
- `build_hit_surface(all_trades, ...)`: for each (entry_bin, target), finds first price in bin during 6h entry window, simulates multi-target outcome. Passes `result_yes` per ticker.
- `compute_ev_surface(hit_surface, entry_fee_rate, exit_fee_rate)`: adds net_ev using `fee = rate × P × (1-P)`.
- `simulate_patient_backtest(all_trades, ev_surface, train_cutoff, ...)`: on each test market, enters at first price in entry window where EV > 0; picks best target for that price bin.

### `analysis/strategy_backtest.py`
- `run_backtest(series, n_atm, entry_fee_rate, exit_fee_rate, ...)`: original opening-price strategy with correct price-dependent fee model.
- `taker_fee(price_cents, rate)`: fee formula helper.
- `simulate_trade(...)`: single-target outcome simulation with price-dependent fees.

### `analysis/microstructure.py`
- Full microstructure suite: `activity_profile`, `volatility_term_structure`, `price_autocorrelation`, `autocorrelation_by_ttx`, `kyle_lambda`, `spread_decomposition`, `convergence_profile`.

---

## 7. Backtests

### Simple litmus test: Rank 4, entry 30–35¢, target 70¢, stop 50%

**Strategy rules (v1 — naive taker):**
- For each rank-4 market, find the first trade in [30, 35¢) during the 6h entry window.
- Enter at that price. Target 70¢. Stop at 50% of entry.
- One trade per market.

**Result (full year, 367 rank-4 markets):**
- 194 entries (52.9% of days had a signal)
- Hit rate: 34.0%, Stop rate: 66.0%
- Mean gross EV: +2.24¢, Total PnL: +433¢

**Entry decomposition — critical finding:**

The 194 taker entries are three structurally different scenarios:

| Approach | n | Hit rate | Breakeven | EV/trade | Total contribution |
|---|---|---|---|---|---|
| Opens in band (30–35¢) | 82 | 45.1% | 30.2% | **+7.95¢** | +652¢ |
| Falls into band from above | 35 | 31.4% | 30.7% | +0.34¢ | +12¢ |
| Rises into band from below | 77 | 23.4% | 28.9% | **−2.99¢** | −230¢ |

The from_below entries are structurally negative and drag down the entire strategy. A market that opens at 20¢ and rises into the band is experiencing upward momentum that doesn't translate to further gains to 70¢ — stop at 15¢ is hit instead.

**Refined strategy (v2 — filter from_below):**

Add rule: if the window opens below 30¢, skip the market entirely.

| Metric | v1 (naive) | v2 (filtered) |
|---|---|---|
| Trades | 194 | **117** |
| Hit rate | 34.0% | **41.0%** |
| Mean EV | +2.24¢ | **+5.67¢** |
| Total PnL | +433¢ | **+664¢** |

Removing 77 trades increased total PnL by +230¢ — those 77 trades lost exactly that amount. The margin above breakeven expanded from 4pp to 11pp.

**Output charts:** `examples/rank4_backtest_pnl.png` (v1), `examples/rank4_backtest_filtered.png` (v2)

### Stop fraction optimization (v3)

**Core finding:** The 50% stop is too wide. Binary markets that fall from 30¢ to 16¢ often recover to 70¢+ — the 50% stop creates "false stops." Tighter stops dramatically improve hit rate by avoiding these reversals.

**Full sweep (n=117 trades, entry 30–35¢, target 70¢, from_below filtered):**

| Stop | Stop price | Hit rate | N wins | N stops | Avg win | Avg loss | EV | Total PnL | Sharpe |
|---|---|---|---|---|---|---|---|---|---|
| 10% | 3.3¢ | 59.0% | 69 | 48 | +34.2¢ | −30.9¢ | +7.51¢ | 879¢ | 2.52 |
| 15% | 4.9¢ | 58.1% | 68 | 49 | +34.2¢ | −29.4¢ | +7.59¢ | 888¢ | 2.60 |
| **20%** | **6.5¢** | **54.7%** | **64** | **53** | **+34.2¢** | **−27.8¢** | **+6.08¢** | **711¢** | **2.12** |
| **25%** | **8.2¢** | **53.0%** | **62** | **55** | **+34.2¢** | **−26.4¢** | **+5.73¢** | **671¢** | **2.04** |
| 30% | 9.8¢ | 50.4% | 59 | 58 | +34.2¢ | −24.8¢ | +4.93¢ | 577¢ | 1.80 |
| 40% | 13.0¢ | 44.4% | 52 | 65 | +34.2¢ | −21.8¢ | +3.08¢ | 361¢ | 1.19 |
| 50% | 16.3¢ | 41.0% | 48 | 69 | +34.1¢ | −18.7¢ | +2.97¢ | 348¢ | 1.23 |

**Mechanism:** Moving from 50% to 25% stop converts 14 false-stop trades from losses (−18.7¢) to wins (+34.2¢), worth +52.9¢ each → +741¢. Offset by: 55 remaining stops now cost 7.7¢ more each (8¢ exit vs 16¢) → −423¢. Net: +318¢.

**Why 10-15% stops are NOT the practical optimum despite best backtest numbers:**
- Stop price at 3–5¢: this is essentially "let the market die before stopping out."
- At 3¢, spreads are very wide (bid–ask 1–3¢ wide). Execution at the limit price is unreliable.
- Critical check: 10% and 15% stops trigger on *exactly the same 80 markets* (checked via min-price analysis). The marginal improvement from 10% to 15% is zero. Both are exploiting the fact that markets that fall to 3–5¢ almost always resolve NO — not a controllable stop.

**Recommended parameters (v3): stop=25%, target=70¢**
- Stop at 8¢: meaningful liquidity (thousands of trades/year at this level), realistic limit-sell execution.
- Avoids the core false-stop problem (14 trades saved vs 50% stop).
- Conservative enough that the stop is a real stop, not an "almost-dead" exit.
- EV improvement: +2.76¢/trade vs 50% stop (+92% lift). Total PnL: 671¢ vs 348¢. Sharpe: 2.04 vs 1.23.

**Overfitting caveat:** n=117 one-year sample. The false-stop story is mechanically motivated and the improvement is large (2×), but parameter selection should be treated as provisional until confirmed on live data.

**Output chart:** `examples/rank4_optimized_comparison.png` (baseline vs optimized PnL overlay), `examples/rank4_param_sweep_v2.png` (full grid heatmap)

### Resting bid fill-rate analysis

A resting bid at price B fills when the market (opening above B) trades down to B. Only the `from_above` scenario (n=35) is captured by a resting bid. The `at_open` cases (where all the EV lives) open directly in the band and should be taken immediately, not waited for.

Resting bid results (markets opening above bid, target 70¢):

| Bid | Opens above bid | Fill rate | Hit rate | Breakeven | EV/trade |
|---|---|---|---|---|---|
| 30¢ | 142 | 43.7% | 25.8% | 27.3% | −0.81¢ |
| 31¢ | 128 | 47.7% | 34.4% | 28.4% | **+3.26¢** |
| 32¢ | 109 | 53.2% | 36.2% | 29.6% | **+3.55¢** |
| 33¢ | 93 | 58.1% | 35.2% | 30.8% | +2.32¢ |
| 34¢ | 67 | 68.7% | 26.1% | 32.1% | −3.17¢ |

31–33¢ are the optimal resting bid levels. However, the trade count is small (n≈35 fills/year) and the EV/trade is lower than the at_open scenario. The resting bid adds marginal value on top of the core at_open strategy; it is not the primary execution mechanism.

**Execution summary:**
1. Window opens in 30–35¢ → **take immediately** (core trade, +7.95¢ EV)
2. Window opens above 35¢ → **post resting bid at 31–33¢** (secondary, +3¢ EV if filled)
3. Window opens below 30¢ → **do nothing** (from_below = −2.99¢ EV)

## 8. Next Steps

### Immediate (post-deployment)
1. **Monitor first live trades** — verify entry/exit fills, confirm SMS alerts arrive via Telegram, check `logs/live_YYYYMMDD.jsonl` for correct event structure.
2. **Run `python3 scripts/trade_report.py`** after first trading day — validate OOS P&L matches backtest EV directionally.
3. **Live orderbook data collection** — validate 30–35¢ ask-side liquidity (rank 4) and 10–15¢ ask-side liquidity (rank 5 Spring) exist at window open; confirm fill assumptions before scaling size.

### Medium term
4. **Size up to quarter-Kelly** — once ~20 live trades per setup with positive OOS EV, move from 1 contract to quarter-Kelly sizing (see multi_city_strategy_report.md Section 7 for sizing schedule).
5. **NWS forecast integration** — NOAA API for P(high > X) at various lead times. Enables entry conditioned on fair value vs. market price rather than just price level.
6. **Second year of data** — seasonal sub-bins need 2–3× current sample sizes for high-confidence parameterization. Continue pulling data as it accumulates.
7. **MM strategy parameterization** — framework scaffolded in `strategies/market_maker/quotes.py`. Requires: fair value model + live spread + Kyle's λ by TTX.
8. **Miami analysis** — KXHIGHMIA data exists in DB. Spring rank 4 and Fall rank 5 setups identified in multi-city report; encode in engine config.

---

## 9. KXHIGHPHIL — Philadelphia Analysis (2026-03-27)

**Data:** 2025-03-26 – 2026-03-28 | 2,178 markets | 558,466 trades (~40% of NY liquidity)

### Market structure
Same 6-bracket structure as NY. Key difference: rank 4 opens at **32¢ median** (vs NY 26¢) — Philly's warm-side bracket is priced closer to fair value at TTX>24h, reflecting more accurate near-term temperature forecasting in Philly markets.

| Rank | N | Open_med | Win% |
|---|---|---|---|
| 3 | 366 | 28¢ | 23.2% |
| 4 | 366 | 32¢ | 30.3% |
| 5 | 357 | 17¢ | 21.3% |

### Strategy: Philly v1

**Different from NY v3.** Because rank 4 opens near fair value, the optimal trade is a modest "push to toss-up" (target 50¢) rather than a big directional call (target 70¢). Wider stop reflects the more balanced two-sided risk.

| Parameter | NY v3 | Philly v1 |
|---|---|---|
| Band | 30–35¢ | 30–35¢ |
| Target | 70¢ | **50¢** |
| Stop | **25%** (~8¢) | **60%** (~19¢) |
| From_below filter | Yes | Yes |

**Full-year results:** n=156, hit=67%, EV=+4.30¢, total=+670¢, **Sharpe=3.81**

| Season | n | Hit | EV | Notes |
|---|---|---|---|---|
| Summer | 44 | 75% | +6.96¢ | Strong |
| Fall | 42 | 79% | +7.46¢ | Strongest |
| Winter | 36 | 64% | +3.64¢ | Solid |
| Spring | 34 | 44% | −2.37¢ | Skip |

Year-by-year: 2025 (n=132, +4.83¢), 2026 partial Jan–Mar (n=24, +1.34¢ — includes negative spring).

### Spring: no viable setup
Rank 5 spring in Philly has 8% actual win rate at 14¢ open — market is fairly pricing it. The NY rank 5 spring edge (structural mispricing) does not appear in Philly. All rank 3/4/5 sweeps in Philly spring are negative.

### Cross-city trade schedule
- **Summer/Fall/Winter**: trade both NY and Philly rank 4 (different params)
- **Spring**: NY only (rank 5 at_open 10–15¢)
- **Philly liquidity**: ~40% of NY — use smaller initial size, more limit-order patience

---

## 10. KXHIGHLAX — Los Angeles Analysis (2026-03-28)

**Data:** 2025-03-26 – 2026-03-28 | 2,174 markets | 1,475,537 trades (most liquid of four cities)

### Market structure
6 brackets per day. Ranks 3 and 4 are nearly symmetric (32¢/30¢ medians). Ranks 1 and 6 are rare-event extremes as in other cities.

| Rank | N | Open_med | Win% |
|---|---|---|---|
| 3 | 367 | 32¢ | 28.3% |
| 4 | 367 | 30¢ | 26.2% |

### Strategy: LA v1

LA requires a **rank inversion** by season — rank 3 (moderate bracket) dominates Spring/Summer due to the marine layer, while rank 4 (warm bracket) wins in Fall/Winter via Santa Ana winds and offshore warm flow.

| Season | Rank | Band | Target | Stop | n | EV | Sharpe |
|---|---|---|---|---|---|---|---|
| Spring | 3 | 35–40¢ | 55¢ | 25% | 34 | +7.09¢ | 2.37 |
| Summer | 3 | 35–40¢ | 55¢ | 25% | 39 | +7.31¢ | 2.77 |
| Fall | 4 | 30–35¢ | 50¢ | 25% | 25 | +6.06¢ | 1.82 |
| Winter | 4 | 25–30¢ | 50¢ | 25% | 34 | +10.14¢ | 3.27 |

**Total annual PnL: +1022¢** (vs NY +671¢, Philly +670¢). From_below filter applies in all seasons.

### Entry patterns
- Rank 3 Spring (35-40): at_open +6.70¢, from_above +8.59¢, from_below -5.03¢
- Rank 4 Winter (25-30): at_open +8.31¢, from_above +11.42¢, from_below -3.52¢
- From_above is consistently positive in LA (unlike NY where it was marginal)

### Sample size warnings
Fall n=25 = one season. Winter has n=8 (2025) and n=26 (2026) with +4.86¢ / +11.77¢ — consistent direction but unreliable magnitude. Standard 1.5-year data caveat applies.

---

## 11. KXHIGHCHI — Chicago Analysis (2026-03-28)

**Data:** 2025-03-27 – 2026-03-28 | 2,192 markets | 977,363 trades (thinnest city, ~67% of NY)

### Market structure
6 brackets per day (357/367 days have 6; 10 have 5). Ranks 3 and 4 are nearly symmetric (31¢/30¢ medians), but **rank 4 is universally negative** — rank 3 is the only viable directional play.

| Rank | N | Open_med | Win% |
|---|---|---|---|
| 3 | 365 | 32¢ | 27.3% |
| 4 | 362 | 30¢ | 27.6% |

### Strategy: Chicago v1

Rank 3 with a low-entry band (23–29¢). Strong in Winter and Fall; Summer marginal; Spring skip.

| Season | Rank | Band | Target | Stop | n | EV | Sharpe |
|---|---|---|---|---|---|---|---|
| Winter | 3 | 23–29¢ | 75¢ | 60% | 46 | +11.62¢ | 2.67 |
| Fall   | 3 | 23–29¢ | 85¢ | 50% | 30 | +12.98¢ | 1.99 |
| Summer | 3 | 23–29¢ | 50¢ | 30% | 30 | +3.81¢  | 1.04 |
| Spring | — | —      | —   | —   | —  | −7.00¢ | skip |

From_below filter applies in all seasons (same universal finding as other cities).

### Entry patterns
- Winter (target=75, stop=60%): at_open +18.79¢ (HR=53%), from_above +7.42¢ (HR=34%), from_below −4.04¢
- Fall (target=85, stop=50%): at_open +24.16¢ (HR=56%), from_above +8.19¢ (HR=33%), from_below −14.69¢
- Summer: from_above only (+3.98¢); at_open negative — do not force Summer entries

### Key differences from other cities
- **Rank 3 not rank 4**: opposite of NY/Philly (which use rank 4). Chicago rank 4 is universally negative.
- **Wider stops needed**: 50–60% vs NY's 25%, suggesting more price noise pre-resolution.
- **High targets**: 75–85¢ vs 50–70¢ elsewhere — markets are more underpriced at window open.
- **Spring excluded entirely**: at_open and from_below both severely negative in spring.

### Sample size warnings
Each season = ~one occurrence. Fall and Summer n=30 each. Treat as directional hypothesis.

---

## 11b. KXHIGHMIA — Miami Analysis (2026-03-28)

**Data:** 2025-03-26 – 2026-03-28 | 2,174 markets | ~400k trades

### Market structure
Miami is a high-heat market. Ranks 4 and 5 dominate volume. Rank 3 is rarely the winning bracket.

### Strategy: Miami v1

| Season | Rank | Band | Target | Stop | n | EV | Sharpe |
|--------|------|------|--------|------|---|----|--------|
| Spring | 4 | 25–33¢ | 50¢ | 25% | 34 | +7.45¢ | 2.35 |
| Fall   | 5 | 10–18¢ | 70¢ | 25% | 55 | +8.45¢ | 3.38 |

Summer and Winter setups were not robust (negative or near-zero EV across all rank/band combinations tested). From_below filter applies in all seasons.

**Fall rank 5 note:** High n=55 makes this the most statistically reliable Miami setup. The at_open approach dominates; from_above is marginal.

### Implementation status
Config is in `strategies/temperature/config.py` as `("KXHIGHMIA", "Spring")` and `("KXHIGHMIA", "Fall")`. Both active in the live engine via `ACTIVE_SERIES`.

---

## 12. File Map

```
kalshi/
├── api/
│   ├── client.py             # REST client; RSA-PSS auth; balance in cents
│   ├── websocket.py          # Async WS; RSA auth; additional_headers (websockets v14)
│   └── alerts.py             # Telegram Bot API alerts via httpx; send_alert()
├── analysis/
│   ├── temperature_strategy.py  # Reusable v3 strategy module (load_and_rank,
│   │                            #   run_backtest, stop_sweep, entry_decomposition,
│   │                            #   seasonal_breakdown, ev_surface)
│   ├── hitting_probability.py   # General hitting-prob surface (build_hit_surface,
│   │                            #   compute_ev_surface, simulate_patient_backtest)
│   ├── strategy_backtest.py     # load_series_trades; original opening-price harness
│   ├── microstructure.py        # kyle_lambda, autocorr, spread decomposition
│   ├── market.py                # OrderbookAnalyzer, MarketAnalyzer (live use)
│   └── reports/
│       ├── project_state.md              # THIS FILE
│       ├── project_state_claude.md       # Dense working notes for Claude
│       └── multi_city_strategy_report.md # Full 5-city strategy report (canonical)
├── backtest/
│   ├── engine.py             # BacktestEngine; conservative/passive fill models
│   └── metrics.py            # BacktestMetrics (Sharpe, drawdown, win rate)
├── strategies/
│   ├── base.py               # Strategy interface; OrderIntent; MarketState; PositionState
│   ├── market_maker/
│   │   └── quotes.py         # Avellaneda-Stoikov MM; inventory skew
│   └── temperature/
│       ├── config.py         # CONFIGS dict; active_config(); assign_rank(); current_season()
│       └── engine.py         # TemperatureEngine; WebSocket state machine per market
├── risk/
│   ├── manager.py            # Pre-trade checks; position/delta/loss limits
│   └── monte_carlo.py        # Bootstrap MC simulation for risk profiling
├── scripts/
│   ├── pull_series.py        # Bulk trade pull for any series; resume-safe
│   ├── build_db.py           # JSONL → Parquet + DuckDB views
│   ├── daily_refresh.py      # Pull last N days settled data + rebuild DuckDB
│   ├── live_engine.py        # Entry point: loads env, reconciles state, runs engine
│   ├── trade_report.py       # Aggregate logs → cumulative P&L vs backtest benchmarks
│   ├── status.py             # Check credentials, API, setups, positions, WS
│   └── run_daily.sh          # Cron wrapper: refresh → engine (3 retries) → SMS on fail
├── docs/
│   └── kalshi-fee-schedule.pdf  # Fee schedule reference (effective Feb 5 2026)
├── examples/
│   ├── generate_temperature_report.py  # Full strategy report generator (per-city)
│   └── microstructure_report.py        # 8-panel microstructure figure
├── data/
│   ├── raw/KXHIGHNY/         # New York (source of truth, never delete)
│   ├── raw/KXHIGHPHIL/       # Philadelphia
│   ├── raw/KXHIGHLAX/        # Los Angeles (note: NOT KXHIGHLA)
│   ├── raw/KXHIGHCHI/        # Chicago
│   ├── raw/KXHIGHMIA/        # Miami
│   └── processed/            # markets.parquet + trades.parquet per city; kalshi.duckdb
├── logs/                     # gitignored; live_YYYYMMDD.jsonl, run_YYYYMMDD.log
├── CLAUDE.md                 # Project instructions, API reference, coding conventions
├── DEPLOYMENT.md             # VPS setup, cron config, log locations, status checks
└── .env.example              # Credential template (KALSHI_*, TELEGRAM_*)
```

---

## 9. Known Issues / Gotchas

| Issue | Resolution |
|---|---|
| API URL moved | `api.elections.kalshi.com` (not `trading-api.kalshi.com`) |
| RSA signing path | Must sign `/trade-api/v2/path`, not just `/path` |
| Trades endpoint | `GET /markets/trades?ticker=TICKER` (not `/markets/{ticker}/trades`) |
| Rate limiting | Concurrency=2, exponential backoff up to 2^5=32s in `pull_series.py` |
| DuckDB `column_type` | Use `data_type` in `information_schema.columns` |
| Nested aggregate | `AVG(STDDEV(x))` illegal in DuckDB; use CTE |
| Python command | `python3` (not `python`) |
| `load_dotenv` path | Pass explicit path: `load_dotenv('.env')` |
| Fee model | Flat 3¢/leg was ~2× too high; use `0.07 × P × (1-P)` formula |
| ISO8601 timestamps | Older data has microseconds; use `format='ISO8601'` in `pd.to_datetime()` |
| tz-naive vs tz-aware | Strip tz with `.dt.tz_localize(None)` before numpy comparisons in hitting_probability.py |
| Binary market expiry | "Expiry" outcome is impossible for a hold-to-settlement strategy. Early sim produced 10–24% expiry rates for OTM ranks because sparse trades skipped the stop price. Fixed: pass `result_yes` to `_simulate_multi_target` to resolve pending outcomes by settlement. |
| pandas groupby.apply deprecation | `DataFrameGroupBy.apply` operated on grouping columns by default; add `include_groups=False` to all `.apply()` calls on groupby objects to suppress warning and future-proof. |
| Selection bias in train PnL | Best-combo chart always looks positive on train by construction. The cliff at train/test boundary is selection bias, not a regime shift. |
| 0–5¢ entries inflate EV | Near-zero stop loss makes breakeven hit rate trivially low (~2–3%). High nominal EV in this bin is partly mechanical, not informational. Treat separately; do not size as a normal trade. |
| Optimal target varies by season | A fixed target (e.g., always 70¢ for rank 4) is suboptimal. The EV-maximizing target shifts meaningfully across seasons — use a seasonal lookup, not a fixed value. |
| from_below entries destroy EV | Markets that open below the entry band and rise into it have negative EV (23.4% hit rate vs 30% breakeven). Filter: if window_open_price < band_lo, skip the market. |
| Resting bid ≠ taker for at_open | A resting bid only catches markets falling into the band from above (n≈35/yr, marginal EV). The core at_open trades (n≈82/yr, +7.95¢) require taking the market at open — a resting bid misses them entirely. |
| 10-15% stops look best but aren't practical | At stop=3-5¢, the stop price is so low that execution is unreliable (wide spreads, thin one-sided market). More importantly, 10% and 15% stops trigger on *identical* markets — they're both just "wait for the market to nearly die." Use 20-25% stops which are at 6-8¢ with genuine liquidity. |
| False stop mechanism: lower stops → more wins | Moving from 50% to 25% stop converts 14 trades from stop→win. These are markets that fell to 10-16¢ but recovered to 70¢. Each conversion is worth ~53¢. At 10% stop (3¢), essentially zero false stops exist — but stop execution at 3¢ is unreliable. |
| API balance is in cents | `get_balance()` returns `{"balance": 1537}` meaning $15.37. All portfolio dollar values from the API are in cents. Always divide by 100. |
| Private key path needs expanduser() | `Path("~/...").expanduser()` required; bare `Path("~/...")` raises FileNotFoundError. Fixed in `api/client.py`. Store key outside the project directory. |
| `get_markets(status="active")` returns 400 | The markets endpoint does not accept a `status` filter. Filter client-side by TTX (0 < ttx ≤ 36h) to find today's open markets. |
| websockets v14 breaking change | `extra_headers` renamed to `additional_headers` in websockets 14.x. Fixed in `api/websocket.py`. |
| KXHIGHNY rank 5 Spring routinely not found | With only 4 warm-side brackets on cold days, rank 5 doesn't exist. The engine logs a warning and skips — expected behavior. Not a bug. |
| `asyncio.get_event_loop()` deprecated | Use `asyncio.get_running_loop()` when creating tasks from a sync method called within an async context. Fixed in `engine._daily_summary()`. |
