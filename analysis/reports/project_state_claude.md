# CLAUDE WORKING NOTES — Kalshi KXHIGHNY
**Last updated:** 2026-03-27
**Purpose:** Dense working notes for Claude across sessions. Superset of project_state.md.
Captures implementation details, failed approaches, data quirks, open hypotheses, and
anything that would otherwise be re-derived wastefully in a new session.

---

## 0. QUICK REFERENCE

```
Data:      2025-03-21 to 2026-03-23 | 2,195 markets | 1,444,324 trades
DB:        data/processed/kalshi.duckdb
Raw:       data/raw/KXHIGHNY/  (one JSONL per market, never delete)

── Analysis ──────────────────────────────────────────────────────────
analysis/temperature_strategy.py    reusable v3 strategy module
analysis/hitting_probability.py     general hitting-prob surface builder
analysis/strategy_backtest.py       load_series_trades + original harness
analysis/microstructure.py          Kyle's λ, autocorr, spread decomposition
analysis/market.py                  OrderbookAnalyzer, MarketAnalyzer (live)

── Reports (generated) ───────────────────────────────────────────────
analysis/reports/kxhighny_strategy_report.md   full strategy report
analysis/reports/kxhighny_fig1–4_*.png         report figures
analysis/reports/project_state.md              human-readable summary
analysis/reports/project_state_claude.md       THIS FILE

── Runnable scripts ──────────────────────────────────────────────────
examples/generate_temperature_report.py  regenerate strategy report
examples/microstructure_report.py        regenerate microstructure report
scripts/pull_series.py                   fetch raw data
scripts/build_db.py                      JSONL → parquet → duckdb

── Live trading ──────────────────────────────────────────────────────
api/client.py        REST client (RSA-PSS auth; balance in cents)
api/websocket.py     WS stub (orderbook_delta, trade, fills)
strategies/base.py   Strategy interface, OrderIntent, MarketState
strategies/market_maker/quotes.py  Avellaneda-Stoikov MM
backtest/engine.py   BacktestEngine + fill models
backtest/metrics.py  BacktestMetrics
risk/manager.py      position/delta/loss limits

Pull new data:   python3 scripts/pull_series.py --series KXHIGHNY --start 2025-03-01
Rebuild DB:      python3 scripts/build_db.py
Regen report:    python3 examples/generate_temperature_report.py
```

### Key confirmed numbers (full year, rank 4)
- Core trade: entry in [30,35¢), from_below filter, target 70¢, stop 25% of entry (v3)
- v1 (naive taker, 50% stop): 194 trades, 34% hit, +2.24¢ EV, +433¢ total
- v2 (from_below filter, 50% stop): 117 trades, 41% hit, +2.97¢ EV, +348¢ total
- **v3 (from_below filter, 25% stop): 117 trades, 53% hit, +5.73¢ EV, +671¢ total, Sharpe=2.04**
- Breakeven hit rate at mean entry ~32¢, target 70¢, stop 25%: ~16%
- Mean entry price: 32.6¢ (std=1.3¢, min=30¢, max=34¢)

---

## 1. DATA & INFRASTRUCTURE

### DuckDB schema
```sql
-- trades columns used most:
ticker, market_date, bracket_type, strike, created_time,
yes_price (as price, 0-99), count, time_to_expiry,
result_yes (bool), result (str), series

-- markets columns:
ticker, market_date, bracket_type, strike, result_yes, result, volume,
close_time, open_time
```

### Loading pattern used in every analysis script
```python
from analysis.strategy_backtest import load_series_trades
con = duckdb.connect('data/processed/kalshi.duckdb', read_only=True)
df = load_series_trades(con, 'KXHIGHNY')
con.close()
# df has: ticker, market_date, bracket_type, strike, created_time,
#         price (=yes_price), count, time_to_expiry, result_yes, result
```

### Rank assignment (do this at start of every rank-based analysis)
```python
df['market_date'] = pd.to_datetime(df['market_date'])
daily_strikes = df.drop_duplicates('ticker')[['ticker','market_date','strike']].copy()
daily_strikes['rank'] = (
    daily_strikes.groupby('market_date')['strike']
    .rank(method='first').astype(int)
)
df = df.merge(daily_strikes[['ticker','rank']], on='ticker')
```

### Entry window definition
```python
ENTRY_TTX = 86400  # seconds; 24 hours
# For each ticker:
eligible = tdf[tdf['time_to_expiry'] >= ENTRY_TTX]
window_start = eligible.iloc[0]['_t']        # first trade with TTX >= 24h
window_end   = window_start + pd.Timedelta(hours=6.0)
# Strip tz BEFORE all numpy/time comparisons:
tdf['_t'] = tdf['created_time'].dt.tz_localize(None)
```

### Data quality notes
- `result_yes` is reliable ground truth (98.2¢ avg yes-settle, 1.2¢ avg no-settle)
- `time_to_expiry` can be negative for trades after close (ignore these)
- Some markets have very few trades at TTX>24h (rank 1 and 6 especially)
- Price is always an integer 0–99 in the raw data; stored as float in parquet
- Prices of exactly 0 or 100 appear very rarely (settlement artifacts, ignore)
- 59.4% of rank-4 markets open BELOW 30¢ at TTX>24h — important for filter logic
- 22.3% of rank-4 markets open IN 30–35¢ band at TTX>24h (the core trade)
- 18.3% open above 35¢ (falling-into-band candidates)

---

## 2. MARKET STRUCTURE

### 6 brackets per day, always
Confirmed no exceptions in the dataset. Ranks 1–6 by strike ascending:
- Rank 1: T market (above/below extreme cold). e.g. "high < 30°F". Very cheap.
- Ranks 2–5: B markets (between two temperatures). "high between X and Y".
- Rank 6: T market (above/below extreme heat). e.g. "high > 90°F". Very cheap.

### Why ranks 3 and 4 matter
Rank 3 = cold-side ATM, rank 4 = warm-side ATM. They straddle the NWS forecast.
One of them wins most days. The other goes to 0.

### Entry price distribution at TTX>24h (full year, n=2,137)
```
Rank  mean   median  std
1     13¢    7¢      17¢   (many markets at 1-3¢; occasional spikes to 95¢)
2     24¢    22¢     13¢
3     38¢    38¢     15¢   (tightest distribution; most consistently ATM)
4     25¢    26¢     13¢
5     11¢    9¢      11¢
6      7¢    4¢      11¢
```

### Why the rank 4 mean is lower than rank 3
By TTX=24h, the forecast has committed. Rank 3 (cold) is more frequently
the "underdog" that drifted cheap. Rank 4 (warm) median is 26¢ which
at first seems odd — but in winter/fall, "warm" brackets drift cheap
when cold weather is expected. In summer, rank 3 drifts cheap.

---

## 3. FEE SCHEDULE (confirmed, effective Feb 5 2026)

```
Taker: 0.07 × P × (1-P)   [P in dollars; max 1.75¢ at 50¢]
Maker: 0.0175 × P × (1-P) [only on maker-fee designated markets]
Free:  $0                  [resting orders on NON-designated markets]

KXHIGHNY: NOT on maker-fee list → all resting orders cost $0
```

**Implication**: For a pure limit-order strategy on KXHIGHNY:
- Post bid at E → free
- Post sell at T → free
- Post sell at stop → free
- All-in cost = $0

If ever forced to take: fee = 0.07 × P × (1-P). At 32¢ entry: 0.07 × 0.32 × 0.68 × 100 = 1.52¢.

---

## 4. HITTING PROBABILITY MODULE — implementation details

### `_simulate_multi_target` — the core primitive
**File:** `analysis/hitting_probability.py`
**Critical detail:** Must pass `result_yes` — binary markets always settle to 0 or 100.
Without it, sparse OTM markets get "expiry" outcomes (10–24% false expiry rate for ranks 1,5,6).

```python
# Correct call:
outcomes = _simulate_multi_target(
    after_prices,       # np.ndarray of prices after entry time
    valid_targets,      # list[float] of targets above entry
    stop,               # float: stop_frac * entry_price
    last_price,         # float: last traded price (fallback, rarely used)
    result_yes=ry       # bool: ALWAYS PROVIDE THIS
)
# Returns: dict[target → (outcome_str, exit_price)]
# outcome_str ∈ {"target", "stop"}  (never "expiry" when result_yes provided)
```

**Single-pass algorithm**: O(n_trades + n_targets). Stop check runs before target checks.
When stop is hit, ALL pending targets resolve as stop simultaneously.

### `build_hit_surface`
- Bins entries into 10¢ buckets (BIN_EDGES = [10,20,...,90])
- Finds first trade in bin during 6h window per market
- Returns: entry_bin, entry_mid, target, n, hit_rate, stop_rate, expiry_rate, gross_ev
- The `expiry_rate` column will be ~0 when result_yes is passed correctly

### `compute_ev_surface`
- Uses taker fee formula for both entry and exit
- For KXHIGHNY: call with entry_fee_rate=0.0, exit_fee_rate=0.0
- net_ev formula:
  ```
  EV = hit_rate*(T-E-fee_exit_hit) + stop_rate*(-stop-fee_exit_stop)
       + expiry_rate*(0-fee_exit_expiry) - fee_entry
  ```

### `simulate_patient_backtest`
- Still uses taker-style execution (first trade in window with EV>0 bin)
- Does NOT implement the from_below filter
- Does NOT implement the resting bid logic
- Treat as deprecated for the v2 strategy

---

## 5. STRATEGY EVOLUTION

### v0: Original opening-price strategy (superseded)
- Enter at first trade with TTX>24h (any rank near ATM)
- Flat 3¢/leg fee assumption → too pessimistic by ~2×
- Best net EV with correct fees: −1.40¢ (taker/taker, target 50¢)
- Discovery: B brackets massively outperform T brackets
- Discovery: selection bias in train PnL chart (cliff at train/test = construction artifact)

### v1: Rank-based, entry in [30,35¢), target 70¢ (litmus test)
- 367 rank-4 markets in 1 year
- 194 entries (52.9%), 34.0% hit rate, +2.24¢ EV, +433¢ total
- Naive taker: enter at first print in [30,35¢) regardless of direction

### v2: v1 + from_below filter
- Add rule: skip market if window opens below 30¢
- 117 entries (31.9%), 41.0% hit rate, ~+5.67¢ EV (stop=50%)
- +230¢ improvement by removing 77 negative-EV trades

### v3: v2 + optimized stop fraction (CURRENT BEST)
- Parameters: entry 30–35¢, from_below filter, stop=25%, target=70¢
- 117 trades, 53.0% hit rate, +5.73¢ EV, +671¢ total, Sharpe=2.04
- vs v2 baseline (stop=50%): +323¢ improvement, hit rate +12pp, Sharpe +0.81
- Mechanism: 14 fewer false stops (trades that fell to ~10-16¢ then recovered to 70¢)
- Trade-off: each remaining stop costs 7.7¢ more (exit at 8¢ vs 16¢)

### Why from_below is negative EV
Market opens at ~20¢, rises to 31¢ (enters band). This is a BULLISH move.
But from 31¢, stop is at 15.5¢. The market has just moved +11¢ upward.
For it to stop us out, it needs to fall 15.5¢. For target, another +39¢.
Hit rate: 23.4% vs breakeven 28.9% → negative.
Intuition: a market rising into the band has already "used up" some of its
upside momentum. The conditional distribution from that point is worse.

### Resting bid analysis findings
- True resting bid (post bid at B, fill when market falls from above to B)
  only captures `from_above` entries (n≈35/year)
- `at_open` entries (n≈82/year, EV=+7.95¢) require taking the market at window open
  — a resting bid misses them
- Optimal resting bid: 31–33¢ (hit rate beats breakeven marginally)
- 30¢ bid: adverse selection too strong (hit rate 25.8%, be=27.3%) → negative
- 34¢ bid: hit rate 26.1%, be=32.1% → negative (stop too large)
- EV/day from resting bid alone (all 367 markets) is <+0.6¢ — marginal

### Entry decomposition (the key insight)
```
Approach      n     Hit%    EV/trade    Contribution
at_open      82    45.1%    +7.95¢      +652¢   ← all the edge
from_above   35    31.4%    +0.34¢      +12¢    ← marginal
from_below   77    23.4%    -2.99¢      -230¢   ← drag; FILTER THESE OUT
Total       194    34.0%    +2.24¢      +433¢
```

Execution rule:
1. open_price in [30,35¢) → take immediately
2. open_price > 35¢ → post resting bid at 31–33¢
3. open_price < 30¢ → skip

---

## 6. EV SURFACE BY RANK × ENTRY BIN (full year, n≥15)

### Rank 4 (primary trade)
```
Entry bin   n    Best T   Hit%    BE%    EV
0–5¢       23    50¢     43.5%   2.6%  +19.43¢  (near-zero stop, mechanical)
5–10¢      40    75¢     20.0%   5.3%  +10.24¢
10–15¢     35    60¢     25.7%  11.6%  +7.13¢
15–20¢     41    60¢     22.0%  17.1%  +1.98¢   (marginal)
20–25¢     40    40¢     42.5%  39.1%  +0.46¢   (marginal)
25–30¢     47    60¢     38.3%  29.7%  +3.77¢
30–35¢     85    70¢     43.5%  30.2%  +6.81¢   ← PRIMARY, highest n
35–40¢     25    45¢     64.0%  71.4%  -2.68¢   ← STOP HERE
```
**Edge cliff at 35¢.** Above that, hit rate falls below breakeven.

### Rank 3 (secondary trade)
```
Entry bin   n    Best T   Hit%    BE%    EV
20–25¢     29    85¢     31.0%  15.3%  +11.57¢
25–30¢     46    45¢     56.5%  44.0%  +3.30¢
30–35¢     61    45¢     73.8%  56.5%  +4.42¢
35–40¢     84    75¢     42.9%  33.3%  +4.57¢   ← most obs
40–45¢     40    75¢     47.5%  39.5%  +4.26¢
45–50¢     36    55¢     50.0%  76.0%  -8.85¢   ← STOP HERE
60–65¢     21    80¢     23.8%  64.1%  -19.79¢
```
**Edge cliff at 45¢.** Above that, market is priced correctly.

### Rank 2 (tertiary)
Positive EV below 35¢, most reliable in 5–20¢ range. Thinner market.

### Ranks 1, 6
Do not trade directionally. Marginal or negative across all bins.

### Rank 5
0–5¢ (n=120, +5.48¢) and 10–20¢ (n=114, +5–6¢) show positive EV.
Worth monitoring. Not yet refined with from_below filter.

---

## 7. SEASONAL ANALYSIS (updated 2026-03-27)

### v3 seasonal breakdown (from_below filtered, taker fees included)

**Rank 4 v3 (entry 30-35¢, target 70¢, stop 25%):**
```
Season   n    Hit%    EV       Total   Sharpe
Summer  21   62.0%  +11.13¢   +234¢   ~1.7
Fall    38   57.9%   +8.94¢   +340¢   ~1.8
Spring  34   47.1%   +1.90¢    +65¢   ~0.4
Winter  24   45.8%   +1.37¢    +33¢   ~0.2
```

Summer and Fall are the core seasons. Spring/Winter weak but positive.
Year-by-year: only 1 season each (data spans 2025-mid 2026). Treat all numbers as provisional.

### NEW: Rank 5 Spring strategy (discovered 2026-03-27)

**Setup**: Rank 5, at_open filter only, band 10-15¢, target 70¢, stop 25%
```
n=39, hit=28%, EV=+8.44¢, total=+329¢, Sharpe=1.77
Year: 2025 (n=34, +9.10¢), 2026-partial (n=5, +3.95¢)
```

**Entry decomposition (band 10-15, no filter):**
```
at_open    n=39  hit=28%  EV=+8.44¢   ← keep
from_above n=10  hit=10%  EV=-4.32¢   ← exclude
from_below n=17  hit=12%  EV=-1.11¢   ← exclude
```

**at_open condition**: open_price of rank 5 market falls in [10, 15¢) at window start.
This occurs in ~41% of spring markets (39/95). More restrictive than the rank 4 from_below filter.

**Cross-season performance (band 10-15, at_open only):**
```
Spring  n=39  hit=28%  EV=+8.44¢  Sharpe=1.77  ← USE THIS
Summer  n=22  hit=32%  EV=+10.24¢ Sharpe=1.54  ← good supplement
Fall    n=26  hit=12%  EV=-2.60¢  Sharpe=-0.63 ← DO NOT USE
Winter  n=15  hit=7%   EV=-4.93¢  Sharpe=-1.10 ← DO NOT USE
```

**Physical story**: rank 5 = 2nd hottest bracket (e.g. "high between 80-90°F"). Opens at 10-15¢ in spring because warm days are unusual. When the market genuinely opens at that level (not falling into it), the ~12% chance priced by the market is too cheap — realized hit rate to 70¢ is 28%. Payoff: 0.28×(~55¢) + 0.72×(-9¢) ≈ +8.5¢ net.

**Code pattern:**
```python
from analysis.temperature_strategy import _iter_entries, _simulate_outcome
r5_sp = filter_rank(df_all, 5)
r5_sp = r5_sp[r5_sp['season'] == 'Spring']
# Then _iter_entries with from_below_filter=False and manually check approach == 'at_open'
```

### Winter — no robust alternative found
Rank 4 target=50¢ aggregate looks good (n=24, +5.64¢, Sharpe=1.62) but year-by-year
is −2.08¢ (2025 winter, n=8) and +9.50¢ (2026 winter, n=16). Opposite signs from 2 winters.
Do NOT encode this as a seasonal variant. Run rank 4 v3 at reduced size in winter.

### Rank 3 seasonal analysis (from_below filtered, band 35-40¢)
Full sweep done but weak across all seasons. Sharpe=0.34 full year vs rank 4 Sharpe=2.04.
Spring rank 3 shows EV=-7.52¢ after from_below filter — the prior +8.66¢ (EV surface, no filter) was polluted.
Rank 3 directional strategy is not worth pursuing in isolation.

### Recommended seasonal strategy table
```
Season  Instrument     Band    Target  Stop   n    EV        Sharpe
Spring  Rank5 at_open  10-15¢  70¢     25%   39   +8.44¢    1.77
Summer  Rank4 v3       30-35¢  70¢     25%   21   +11.13¢   ~1.7
Fall    Rank4 v3       30-35¢  70¢     25%   38   +8.94¢    ~1.8
Winter  Rank4 v3 (½ sz)30-35¢  70¢     25%   24   +1.37¢    0.22
```
(Note: rank 5 spring and rank 4 summer may both trigger on the same day — double weather exposure. Risk management consideration.)

### CRITICAL DATA CAVEAT
Dataset spans ~1.5 years. Each season appears ONCE (or partially twice).
All Sharpe ratios and EV estimates above have huge uncertainty intervals.
Seasonal conclusions = directional hypotheses, not confirmed edges.

---

## 8. CODING GOTCHAS AND PATTERNS

### Timezone handling — the most common bug source
```python
# Always strip tz before numpy comparisons
tdf['_t'] = tdf['created_time'].dt.tz_localize(None)
all_times = tdf['created_time'].dt.tz_localize(None).values  # for numpy arrays

# For a single timestamp from a row:
entry_time = row['created_time']
if hasattr(entry_time, 'tz_localize'):
    entry_time = entry_time.tz_localize(None)
else:
    entry_time = entry_time.replace(tzinfo=None)
```

### ISO8601 timestamps with microseconds
```python
# Older data has: "2025-11-03T22:26:40.657397Z" (microseconds)
# Default parser chokes. Always use:
pd.to_datetime(df["created_time"], utc=True, format="ISO8601")
pd.to_datetime(df["close_time"],   utc=True, format="ISO8601")
pd.to_datetime(df["open_time"],    utc=True, format="ISO8601")
# Already fixed in build_db.py
```

### pandas groupby.apply deprecation
```python
# Always pass include_groups=False to silence warning and be future-safe:
df.groupby('ticker').apply(lambda g: ..., include_groups=False)
# Also in aggregation lambdas inside groupby().apply():
.apply(lambda g: pd.Series({...}), include_groups=False)
```

### Binary market "expiry" bug
- NEVER call `_simulate_multi_target` without `result_yes`
- Without it, sparse markets (ranks 1,5,6 at 1–5¢) show 10–24% false expiry rates
- The bug: last traded price before close may be 4¢ but stop is 3.5¢ → no trade at/below stop → "expiry"
- Fix: pending targets resolved by `result_yes` (True→target hit, False→stop hit)

### 0–5¢ entries inflate EV mechanically
- Stop at 0.5 × 2¢ = 1¢ → breakeven hit rate is ~2–3%
- Almost any positive hit rate gives positive EV
- This is structural, not informational edge
- Do not size 0–5¢ entries the same as 30–35¢ entries

### Selection bias in train PnL
- `strategy_report.py` selects the best (entry, target) combo from train data
- Train PnL always looks positive by construction
- The cliff at train/test boundary is this selection, not a regime shift
- For honest backtests: pre-specify the rules, don't select from the same data

### DuckDB quirks
```python
# Use data_type not column_type in information_schema:
con.execute("SELECT column_name, data_type FROM information_schema.columns WHERE ...")
# Nested aggregates illegal:
# AVG(STDDEV(x)) → illegal; use CTE instead
```

### from_below filter implementation
```python
# At top of market loop, after computing open_price and window_start:
open_price = eligible.iloc[0]['price']
if open_price < BAND_LO:   # e.g. BAND_LO = 30
    continue               # skip this market entirely
```

### Pandas cut for entry bins
```python
bin_edges = list(range(0, 101, 5))
bin_labels = [f'{lo}-{lo+5}' for lo in range(0, 96, 5)]
results['entry_bin'] = pd.cut(
    results['entry_price'], bins=bin_edges, labels=bin_labels, include_lowest=True
)
# When splitting label back to lo/hi:
lo, hi = int(row.entry_bin.split('-')[0]), int(row.entry_bin.split('-')[1])
E_mid = (lo + hi) / 2
```

### Breakeven hit rate formula
```python
# EV = hit*(T-E) - stop*(stop_frac*E) = 0
# => hit / stop = stop_frac*E / (T - E)
# => hit_be = (stop_frac*E) / (T - E + stop_frac*E)
be = (STOP_FRAC * E_mid) / (TARGET - E_mid + STOP_FRAC * E_mid)
```

---

## 9. ANALYSIS PATTERNS (reusable code fragments)

### Standard rank-4 analysis setup
```python
import duckdb, pandas as pd, numpy as np, sys
from pathlib import Path
sys.path.insert(0, str(Path('.').resolve()))
from analysis.strategy_backtest import load_series_trades
from analysis.hitting_probability import _simulate_multi_target

con = duckdb.connect('data/processed/kalshi.duckdb', read_only=True)
df = load_series_trades(con, 'KXHIGHNY')
con.close()

df['market_date'] = pd.to_datetime(df['market_date'])
daily_strikes = df.drop_duplicates('ticker')[['ticker','market_date','strike']].copy()
daily_strikes['rank'] = daily_strikes.groupby('market_date')['strike'].rank(method='first').astype(int)
df = df.merge(daily_strikes[['ticker','rank']], on='ticker')
r4 = df[df['rank'] == 4].copy()
```

### Entry window iteration with approach classification
```python
for ticker, tdf in r4.groupby('ticker'):
    tdf = tdf.sort_values('created_time').copy()
    tdf['_t'] = tdf['created_time'].dt.tz_localize(None)
    eligible = tdf[tdf['time_to_expiry'] >= ENTRY_TTX]
    if len(eligible) == 0: continue

    window_start = eligible.iloc[0]['_t']
    window_end   = window_start + pd.Timedelta(hours=WINDOW_HOURS)
    open_price   = eligible.iloc[0]['price']
    result_yes   = bool(tdf['result_yes'].iloc[0])
    window = tdf[(tdf['_t'] >= window_start) & (tdf['_t'] <= window_end)]

    # Approach classification:
    if open_price >= BAND_HI:   approach = 'from_above'
    elif open_price >= BAND_LO: approach = 'at_open'
    else:                       approach = 'from_below'
```

### All prices/times as numpy arrays (fast simulation)
```python
# Pre-compute per-ticker maps outside the loop (much faster):
all_prices_map = df.groupby('ticker').apply(
    lambda g: g.sort_values('created_time')['price'].values, include_groups=False
)
all_times_map = df.groupby('ticker').apply(
    lambda g: g.sort_values('created_time')['created_time'].dt.tz_localize(None).values,
    include_groups=False
)
# Inside the loop:
prices_arr = all_prices_map.get(ticker, np.array([]))
times_arr  = all_times_map.get(ticker, np.array([]))
after_mask   = times_arr > entry_time   # entry_time must be tz-naive
after_prices = prices_arr[after_mask]
last_price   = after_prices[-1] if len(after_prices) > 0 else entry_price
```

### Season mapping
```python
def season(m):
    if m in (3,4,5):   return 'Spring'
    if m in (6,7,8):   return 'Summer'
    if m in (9,10,11): return 'Fall'
    return 'Winter'
df['season'] = df['market_date'].dt.month.map(season)
```

---

## 10. OPEN HYPOTHESES & INVESTIGATION QUEUE

### High priority
1. **from_below filter on rank 3** — does the same filter (skip if open < band_lo)
   improve rank 3 as dramatically as it did rank 4? Rank 3's band is wider (~25–45¢).
   Need to check what fraction of rank-3 entries are from_below and their EV.

2. **Seasonal target parameterization** — encode season-specific targets (rank 4:
   Summer→70¢, Fall→85¢, Winter→50¢, Spring→60¢) and rerun v2 backtest.
   Expected improvement over fixed 70¢ target: meaningful, especially in Fall
   where 85¢ target fits much better.

3. **At_open hit rate driver** — 45.1% hit rate for at_open entries is the core edge.
   WHY is a rank-4 market opening in 30–35¢ followed by 45% target hits?
   Hypotheses: (a) genuine uncertainty at TTX=24h; (b) market underprices the warm
   tail given NWS forecast uncertainty; (c) mean-reversion to 50¢ from near-ATM.
   To test: compare at_open outcomes against actual NWS forecast accuracy.

4. **How thin is the 30–35¢ orderbook at window open?** — The at_open strategy
   requires taking the market immediately. If the orderbook has only 5 contracts
   at the ask, size is limited. Need live orderbook data to estimate available size.

5. **Exit at target vs resting limit** — the simulation exits at exactly 70¢.
   In practice, a resting sell at 70¢ might sit unfilled if price runs through
   quickly or only briefly touches 70¢. Need to check how often 70¢ is actually
   traded vs. price jumping from 65¢ to 75¢ directly.

### Medium priority
6. **Rank 3 from_below decomposition** — analogous to rank 4 analysis. Also:
   does rank 3 have the same "at_open is best" pattern?

7. **Conditional on outcome of the "other" bracket** — in a given day, rank 3
   and rank 4 are anti-correlated (one wins, the other loses). Could hedge by
   holding both simultaneously? PnL correlation analysis needed.

8. **April and October** — both consistently weak months in rank 4.
   April is spring (expected). October is fall (expected to be STRONG per
   seasonal analysis). But October 2025 specifically was 22.2% hit rate.
   Was this a one-year anomaly or is October structurally different from
   Sep/Nov? Only one year of data; can't tell yet.

9. **Time-within-window effect** — does the hit rate differ based on WHEN
   in the 6h window the entry occurs (first hour vs last hour)?
   A market that touches the band in the last 30 min of the window may
   have different dynamics than one that opens there.

10. **Rank 5 deeper investigation** — 10–20¢ entry range shows +5–6¢ EV
    (n=114). Apply the from_below filter and run a similar litmus backtest.

### Low priority / notes to self
- The `build_hit_surface` function uses 10¢ bins; the ad-hoc analysis uses 5¢ bins.
  These are not interchangeable. The hitting_prob_report.py example uses 10¢ bins.
  For strategy analysis, always use 5¢ bins (more granular).

- `simulate_patient_backtest` in hitting_probability.py is effectively
  superseded by the inline analysis patterns. It doesn't implement the
  from_below filter or the at_open/from_above/from_below decomposition.
  Either update it or deprecate it.

- The `examples/hitting_prob_report.py` figure is still based on the 10¢ bin
  surface and uses `entry_fee_rate=0.07` (taker). Should be updated to
  use fee=0.0 (KXHIGHNY is maker-free) and 5¢ bins.

---

## 11. WHAT WE KNOW THAT PROJECT_STATE.MD DOESN'T FULLY CAPTURE

### The "at_open" trade is not about direction
The edge in at_open entries is not because we're forecasting where the
temperature will go. It's because rank-4 markets that open at 30–35¢ at
TTX=24h represent situations where:
- The NWS forecast says ~30–35% chance the warm bracket wins
- But the binary payoff structure (win 38¢, lose 15¢) makes positive EV
  viable at just 30% hit rate
- And empirically the actual resolution is 45%, not 30–35%

This 10–15pp gap between market-implied probability and realized probability
IS the edge. The market is either miscalibrated or the taker-fee-paying
short-sellers are underpaying for the downside option.

### Stop loss optimization COMPLETED — use 25% stop
Full grid search across stop_frac=[10%..75%] × target=[45..90¢]:

```
Stop%  Stop¢    Hit%   EV/trade   Total   Sharpe
 10%    3.3¢   59.0%    +7.51¢    879¢    2.52   ← NOT practical (exec risk at 3¢)
 15%    4.9¢   58.1%    +7.59¢    888¢    2.60   ← NOT practical (same 80 stops as 10%)
 20%    6.5¢   54.7%    +6.08¢    711¢    2.12   ← practical lower bound
 25%    8.2¢   53.0%    +5.73¢    671¢    2.04   ← RECOMMENDED
 30%    9.8¢   50.4%    +4.93¢    577¢    1.80
 50%   16.3¢   41.0%    +2.97¢    348¢    1.23   ← v2 baseline
```

Mechanism: lower stop → fewer false stops (markets that fell to ~10-16¢ and recovered to 70¢).
Moving 50%→25%: converts 14 false-stop trades from −18.7¢ to +34.2¢ each (+52.9¢/trade × 14 = +741¢).
Offset by: remaining 55 stops cost 7.7¢ more each (exit at 8¢ not 16¢) → −423¢. Net: +323¢.

WHY 10-15% stops look best but are NOT practical:
- At 10% stop (3.3¢): stop price < 5¢ for ALL 117 trades. Bid-ask spreads at 3¢ are 1–3¢ wide.
- Critical finding: 10% and 15% stops trigger on IDENTICAL 80 markets (0 markets reach 15% stop
  but not 10%). The improvement from 10%→15% is ZERO in terms of which trades are stopped.
- Both are effectively "wait for the market to nearly die." Execution at 3-5¢ is unreliable.
- At 20%+ stop (≥6.5¢): meaningful trading volume exists, limit orders fill reliably.

RECOMMENDED: v3 parameters = stop=25%, target=70¢
Charts: examples/rank4_optimized_comparison.png, examples/rank4_param_sweep_v2.png

### The 70¢ target is optimal for the full year at 30–35¢ entry
70¢ is confirmed as the best target for rank 4, 30–35¢, across all seasons combined.
Seasonal variation: Winter=50¢ best, Fall=85¢ best — but sub-bins n=8–31 so overfitting
risk is high. Use 70¢ fixed for now; revisit when more data is available.

### The "edge" may partially be structural
Consider: rank-4 market at 32¢ at TTX=24h. This means 32% implied
probability the warm bracket wins. The rank-4 bracket wins when the actual
temperature is in the warm range (e.g., 70–75°F in summer). This occurs
with some real probability P_true. If P_true > 32%, we have edge even
without any informational advantage. The market may be systematically
underpricing the warm bracket in summer because:
(a) Most trading volume is by retail bettors who anchor on forecasts
(b) NWS forecasts systematically underestimate warm extremes in summer
    (known bias in meteorological literature)
Hypothesis: the edge is partly a forecasting bias by NWS + market.
This would make the edge structural and persistent, not noise.

### We have not tested out-of-sample yet
The entire analysis (entry filter, target, stop) was derived from the
same dataset (Mar 2025 – Mar 2026). There is no true out-of-sample test.
The v2 strategy should be considered in-sample until we have
data from a second year. The +664¢ total is likely an overestimate of
live performance.

---

## 12. KXHIGHPHIL — Philadelphia (added 2026-03-27)

### Data
558,466 trades | 2,178 markets | 2025-03-26 to 2026-03-28 | ~40% of NY liquidity

### Structure difference from NY
```
Rank   Open_med   Win%
  3      28¢      23.2%
  4      32¢      30.3%    ← closer to fair value than NY's 26¢
  5      17¢      21.3%
```
Ranks 3 and 4 are nearly symmetric in Philly. In NY rank 4 is the "cheap warm bracket."
In Philly, both are fairly priced relative to each other.

### Philly v1 strategy
Band 30-35¢, target **50¢** (not 70¢), stop **60%** (not 25%), from_below filter.
```
Full year: n=156, hit=67%, EV=+4.30¢, total=+670¢, Sharpe=3.81
2025: n=132, +4.83¢   2026 (partial Jan-Mar): n=24, +1.34¢
```
Year-by-year CONSISTENT direction (unlike NY winter which split +/-).

Seasonal:
```
Season  n    Hit%    EV
Summer  44   75%   +6.96¢
Fall    42   79%   +7.46¢
Winter  36   64%   +3.64¢
Spring  34   44%   -2.37¢  ← skip
```

### Entry decomposition (Philly band 30-35, stop=0.60, target=70)
```
at_open    n=94  EV=+3.66¢  ← positive
from_above n=62  EV=+4.43¢  ← ALSO positive (different from NY where from_above is marginal)
from_below n=75  EV=-4.42¢  ← still negative, filter these
```
Both at_open AND from_above are positive in Philly. run_backtest (from_below filtered) captures both.
No additional filter needed beyond the standard from_below filter.

### Why wider stop and lower target in Philly
- NY rank 4 opens at 26¢ — market underprices warm days → need big target (70¢) to capture the move
- Philly rank 4 opens at 32¢ — market is more accurate → trade is "push to toss-up" (50¢)
- At stop=0.60, stop price ≈ 0.60 × 32 = 19¢ — still liquid territory
- Tight stop (25%) in Philly: EV=+1.74¢, Sharpe=0.72 (terrible vs NY's Sharpe=2.04)
- Wide stop (60%) in Philly: EV=+3.97¢, Sharpe=2.03 (and +4.30¢ Sharpe=3.81 at target=50¢)
- Philly doesn't have the "false stop" problem in the same direction — the price doesn't fall to 8¢ and recover

### Spring: no viable setup in Philly
Rank 5 spring Philly: 8% win rate at 14¢ open → market roughly correctly pricing it.
NY rank 5 spring works because NY rank 5 spring has 17.9% win rate at 10¢ — structural mispricing.
In Philly: no analogous mispricing found across rank 3/4/5 in spring.

### Cross-city trade schedule
```
Season  NY strategy                    Philly strategy
Spring  Rank5 at_open 10-15¢ → 70¢   SKIP
Summer  Rank4 v3 30-35¢ → 70¢  25%   Rank4 v1 30-35¢ → 50¢  60%
Fall    Rank4 v3 30-35¢ → 70¢  25%   Rank4 v1 30-35¢ → 50¢  60%
Winter  Rank4 v3 30-35¢ → 70¢  25%   Rank4 v1 30-35¢ → 50¢  60%
```

### Loading Philly data
```python
con = duckdb.connect('data/processed/kalshi.duckdb', read_only=True)
df_phil = load_and_rank(con, 'KXHIGHPHIL')  # same function as NY
con.close()
```

---

## 13. KXHIGHLAX — Los Angeles (added 2026-03-28)

### Data
1,475,537 trades | 2,174 markets | 2025-03-26 to 2026-03-28 | most liquid of all four cities

### Structure
Ranks 3 and 4 nearly symmetric (32¢/30¢ medians). Win rates 28.3% / 26.2% overall.
Seasonal win rates are INVERTED vs NY:
- Rank 3 Summer wins 40.2% (marine layer keeps temps moderate)
- Rank 4 Fall wins 31.9% (Santa Ana winds bring heat)
- Rank 4 Winter wins 30.0% (occasional offshore warm flow; opens cheapest at 26¢)

### LA v1 strategy
```
Season  Rank  Band    Target  Stop   n    EV       Sharpe
Spring   3    35-40¢   55¢    25%   34   +7.09¢    2.37
Summer   3    35-40¢   55¢    25%   39   +7.31¢    2.77
Fall     4    30-35¢   50¢    25%   25   +6.06¢    1.82
Winter   4    25-30¢   50¢    25%   34   +10.14¢   3.27

Total annual PnL: ~+1022¢
```

### Key differences from NY and Philly
1. **Rank switches by season** — not rank 4 year-round. Spring/Summer use rank 3.
2. **Lower targets** — 55¢ (rank 3) and 50¢ (rank 4), never 70¢. Market is closer to fair value.
3. **from_above is POSITIVE** in all seasons (LA rank 4 Winter from_above: +11.42¢!). No extra filter needed.
4. **Winter entry band is 25-30¢** (not 30-35¢) — rank 4 opens cheapest in winter in LA.

### Entry decompositions (key facts)
```
Rank 3 Spring (35-40, target=55, stop=0.25):
  at_open n=27  EV=+6.70¢  ✓
  from_above n=7  EV=+8.59¢  ✓
  from_below n=24  EV=-5.03¢  ✗  (from_below filter removes these)

Rank 4 Winter (25-30, target=50, stop=0.25):
  at_open n=14  EV=+8.31¢  ✓
  from_above n=20  EV=+11.42¢  ✓ (strong!)
  from_below n=18  EV=-3.52¢  ✗
```

### Year-by-year
Spring/Summer rank3: 2025 n=69, +7.43¢/trade, Sharpe=3.73 (2026 partial: n=4)
Fall rank4: 2025 only n=25, +6.06¢
Winter rank4: 2025 n=8 +4.86¢, 2026 n=26 +11.77¢ — consistent direction, wide magnitude variance

### Loading
```python
df_la = load_and_rank(con, 'KXHIGHLAX')  # same function, note: KXHIGHLAX not KXHIGHLA
```

---

## 14. KXHIGHCHI — Chicago Analysis (2026-03-28)

**Data:** 2025-03-27 – 2026-03-28 | 2,192 markets | 977,363 trades

### Key structural finding
Rank 4 is universally negative (EV < 0 in all band/season combinations). Rank 3 is the play.
Open price distribution at TTX>24h:
- Rank 3: p25=22¢, p50=32¢, p75=39¢, mean=29.7¢
- Rank 4: p25=18¢, p50=28¢, p75=37¢, mean=27.1¢

### Optimal band: 23–29¢
Rationale: captures markets where rank 3 opens cheaply (below fair value), filtering out overpriced entries. Band sweep showed this as top EV with n=142 (all seasons combined).

### Parameters per season (rank 3, band 23–29, from_below filter)

```
Season  Target  Stop   n    EV       Sharpe  HR
Winter    75¢   60%   46  +11.62¢   2.67    41.3%
Fall      85¢   50%   30  +12.98¢   1.99    40.0%
Summer    50¢   30%   30   +3.81¢   1.04    60.0%
Spring    ---   ---   --  -7.00¢   SKIP
```

### Entry decomposition highlights
- Winter at_open: HR=53%, EV=+18.79¢ (strongest single entry type in any city)
- Fall at_open: HR=56%, EV=+24.16¢
- from_below: negative in ALL seasons (−4 to −15¢) — universal filter confirmed
- Spring at_open: HR=14%, EV=−13.20¢ — structural mispricing (spring is opposite direction)

### Why wider stops and higher targets?
Chicago has more temperature volatility pre-resolution. Markets oscillate more before settling, requiring wider stops to avoid being shaken out. The higher targets (75–85¢) reflect that rank 3 at 23–29¢ is substantially mispriced at window open — more edge to capture.

### Summer judgment
Summer Sharpe=1.04, n=30 (one season). The at_open component is slightly negative; from_above is the only positive entry type (+3.98¢). Recommend trading at reduced size or monitoring one more summer before committing.

### Cross-city comparison
```
City    Rank  Band     Target  Stop  Best season EV  Sharpe
NY      4     30–35¢   70¢    25%   Fall +6.85¢     1.90
Philly  4     30–35¢   50¢    60%   Fall +7.46¢     3.81
LA      3/4   season   50–55¢ 25%   Winter +10.14¢  3.27
Chicago 3     23–29¢   75–85¢ 50–60% Winter +11.62¢ 2.67
```

### Loading
```python
con = duckdb.connect('data/processed/kalshi.duckdb')
df_chi = load_and_rank(con, 'KXHIGHCHI')
df_chi['season'] = df_chi['market_date'].dt.month.map(SEASON_MAP)
```

---

## 15. KXHIGHMIA — Miami Analysis (2026-03-28)

**Data:** 2025-03-27 – 2026-03-29 | 2,134 markets | 922,492 trades

### Key structural findings
- Variable brackets per day (313 have 6, 39 have 5, 15 have 4) — unique among all cities
- Rank 3: universally negative (win% 15–26% at median 21–32¢ open — overpriced in subtropical climate)
- Rank 4 Summer: negative EV (−7.15¢) — Miami summer is so reliably hot that rank 4 is fairly/over-priced
- TWO primary setups: rank 4 Spring and rank 5 Fall

### Parameters
```
Season  Rank  Band     Target  Stop   n    EV       Sharpe
Spring  4     25–33¢   50¢    25%    34  +7.45¢   2.35   (2 springs: 2025 +4.49¢, 2026 +9.28¢)
Fall    5     13–27¢   45¢    25%    55  +8.45¢   3.38   (1 fall, Fall 2025 only)
Fall    4     25–33¢   45¢    25%    18  +7.44¢   2.63   (1 fall, half size — n=18)
Summer  ---   SKIP
Winter  ---   SKIP (Sharpe=1.61 — below threshold)
```

### Entry decomposition highlights
- Rank 4 Spring at_open: HR=71.4%, EV=+6.97¢; from_above HR=76.9%, EV=+8.23¢
- Rank 4 Spring from_below: HR=77.8%, EV=+9.80¢ — anomalous positive (n=9, too thin to override universal filter)
- Rank 5 Fall at_open: HR=63.4%, EV=+8.60¢; from_above HR=71.4%, EV=+8.00¢
- Rank 5 Fall from_below: HR=25%, EV=−5.93¢ — filter still applies

### Miami Fall physical story
Miami stays warm through September–November (warm Atlantic). Both rank 4 and rank 5 opening cheaply (13–33¢) are underpriced because the market underweights persistent warm outcomes. Rank 5 (n=55) is the more liquid and better-sampled trade; rank 4 fall (n=18) is secondary.

### Miami Spring robustness
The most multi-year confirmed setup in the dataset: 2025 n=13 +4.49¢, 2026 n=21 +9.28¢. Both positive, improving direction. Highest confidence for a single setup.

### Config dict additions
```python
('KXHIGHMIA', 'Spring'): {'rank': 4, 'band_lo': 25, 'band_hi': 33, 'target': 50, 'stop_frac': 0.25},
('KXHIGHMIA', 'Fall'):   {'rank': 5, 'band_lo': 13, 'band_hi': 27, 'target': 45, 'stop_frac': 0.25},
# Rank 4 Fall also active at half size (handled separately)
```

---

## 16. NEXT SESSION STARTING POINTS

If starting a new session and the context has been compressed:

1. Read this file and `analysis/reports/project_state.md` first.
2. **NY best strategy is v3**: entry 30–35¢ (rank 4), from_below filter, stop=25%, target=70¢.
   - n=117, 53.0% hit rate, +5.73¢ EV, +671¢ total, Sharpe=2.04.
   - Spring supplement: rank 5, at_open filter, band 10-15¢, target=70¢, stop=25%, n=39, +8.44¢.
3. **Philly best strategy is v1**: entry 30–35¢ (rank 4), from_below filter, stop=60%, target=50¢.
   - n=156, 67% hit rate, +4.30¢ EV, +670¢ total, Sharpe=3.81. Skip spring.
4. **LA best strategy is v1**: rank INVERSION by season (rank 3 in Spring/Summer, rank 4 in Fall/Winter).
   - Spring/Summer: rank 3, band 35–40¢, target=55¢, stop=25%. EV ~+7¢, Sharpe ~2.6.
   - Fall/Winter: rank 4, band 25–30¢ (Fall: 30–35¢), target=50¢, stop=25%. EV +6–10¢.
5. **Chicago best strategy is v1**: rank 3 (not rank 4), band 23–29¢, from_below filter.
   - Winter: target=75¢, stop=60%, n=46, EV=+11.62¢, Sharpe=2.67
   - Fall:   target=85¢, stop=50%, n=30, EV=+12.98¢, Sharpe=1.99
   - Summer: target=50¢, stop=30%, n=30, EV=+3.81¢, Sharpe=1.04 (marginal)
   - Spring: SKIP (at_open severely negative, -13¢ EV)
6. **Miami best strategy**: rank 4 Spring + rank 5 Fall (+ rank 4 Fall at half size).
   - Spring: rank 4, band 25–33¢, target=50¢, stop=25%, n=34, EV=+7.45¢, Sharpe=2.35 (two springs confirmed)
   - Fall r5: rank 5, band 13–27¢, target=45¢, stop=25%, n=55, EV=+8.45¢, Sharpe=3.38 (one fall)
   - Fall r4: rank 4, band 25–33¢, target=45¢, stop=25%, n=18, half size (one fall)
   - Summer/Winter: SKIP
7. Parameter optimization for all five cities is COMPLETE. Do NOT re-run sweeps.
7. The next logical tasks are:
   a. **Implementation of the live strategy** — API wired up, $15.37 balance as of 2026-03-23. Wire v3/v1 rules into execution: monitor window open, take at band, post resting limit sells at target and stop.
   b. Live orderbook data collection to validate fill assumptions (thin markets)
   c. NWS forecast integration for fair value estimation
8. Correct LA series ticker is KXHIGHLAX (not KXHIGHLA — that pull returned 0 markets)
5. Do NOT re-derive the rank structure or fee schedule — both are confirmed.
6. Do NOT re-run basic EV surface analysis — results are in sections 6 and 7 above.
7. API is live and tested:
   - Key stored at path in .env (outside project dir), loaded via Path.expanduser()
   - Balance returned in CENTS (1537 = $15.37) — confirmed gotcha
   - get_balance(), get_markets(), get_positions() all verified working
8. When running analysis, always:
   - Use `python3` not `python`
   - Include `result_yes` in `_simulate_multi_target` calls
   - Strip tz before numpy comparisons
   - Pass `include_groups=False` to groupby.apply
9. Standard v3 backtest boilerplate (copy-paste ready):
```python
ENTRY_TTX = 86400; ENTRY_WIN = 6.0
BAND_LO = 30; BAND_HI = 35
STOP_FRAC = 0.25; TARGET = 70.0

for ticker, tdf in r4.groupby('ticker'):
    tdf = tdf.sort_values('_t')
    result_yes = bool(tdf['result_yes'].iloc[0])
    eligible = tdf[tdf['time_to_expiry'] >= ENTRY_TTX]
    if len(eligible) == 0: continue
    ws = eligible.iloc[0]['_t']; we = ws + pd.Timedelta(hours=ENTRY_WIN)
    win = tdf[(tdf['_t'] >= ws) & (tdf['_t'] <= we)]
    if len(win) == 0: continue
    if win.iloc[0]['price'] < BAND_LO: continue  # from_below filter
    for _, row in win.iterrows():
        p = row['price']
        if not (BAND_LO <= p < BAND_HI): continue
        ep = p; sp = STOP_FRAC * ep
        at = row['_t']
        aps = tdf[tdf['_t'] > at]['price'].values
        oc, xp = None, None
        for ap in aps:
            if ap <= sp: oc, xp = 'stop',   sp;    break
            if ap >= TARGET: oc, xp = 'target', TARGET; break
        if oc is None:
            oc, xp = ('target', TARGET) if result_yes else ('stop', sp)
        fe = 0.07*(ep/100)*(1-ep/100)*100; fx = 0.07*(xp/100)*(1-xp/100)*100
        net_pnl = xp - ep - fe - fx
        break  # one trade per market
```
