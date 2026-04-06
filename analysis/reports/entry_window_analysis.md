# Entry Window Analysis
**Date:** 2026-04-05
**Series analyzed:** KXHIGHLAX, KXHIGHNY, KXHIGHMIA (city-level); all 5 cities (2D sweep)
**Data window:** 2025-03-21 – 2026-03-28
**Script:** `scripts/window_sweep_analysis.py`

---

## Motivation

Markets open ~36h before settlement. Our entry window is anchored at TTX=24h with a 6h duration (enters between TTX=24h and TTX=18h). Both the start TTX and the 6h duration were set by feel. Two questions:
1. Are we missing entry opportunities by ignoring the 24h–36h window?
2. Is 6h the right duration, or does a wider/narrower window improve EV or trade volume?

---

## Part 1: 36h vs 24h Entry — City-Level Analysis (LAX, NYC, MIA)

### Coverage at 36h TTX
| City | % markets with trade at TTX ≥ 36h |
|------|-----------------------------------|
| LAX | 82.6% |
| NYC | 69.9% |
| MIA | 65.3% |

The 36h window is accessible in the data; coverage is not the limiting factor.

### Price distribution at 36h vs 24h (ranks 3–4)
Mean drift from 36h→24h is ~0¢ across all three cities (std ~10–12¢). Prices at 36h are not systematically cheaper or more uncertain than at 24h — the market is already pricing the outcome similarly 36h out.

### EV comparison (generic directional strategy, ranks 2–5, entry 20–70¢)
| City | Window | Hit rate | Breakeven | Edge | EV/trade |
|------|--------|----------|-----------|------|----------|
| LAX | 36h | 0.279 | 0.289 | −0.010 | −3.61¢ |
| LAX | 24h | 0.316 | 0.321 | −0.005 | −3.96¢ |
| NYC | 36h | 0.312 | 0.284 | +0.027 | −1.45¢ |
| NYC | 24h | 0.353 | 0.331 | +0.022 | −2.41¢ |
| MIA | 36h | 0.357 | 0.320 | +0.037 | −1.42¢ |
| MIA | 24h | 0.359 | 0.367 | −0.008 | −4.53¢ |

LAX: 36h offers no improvement. NYC: both windows have similar positive edge; 36h slightly better EV/trade. MIA: the standout — positive edge at 36h, negative by 24h; 24h EV collapses to −4.53¢.

### The adverse selection warning: "escaped" markets
Markets in-band at 36h but out of band by 24h have result_yes = 10–15% across all cities — these are almost entirely NO outcomes where the market is drifting toward 0. Entering at 36h catches these; the 24h filter naturally avoids them. This is the primary source of "extra volume" from an earlier window, and it's adverse.

### MIA warm-side drift
MIA ranks 4–5 show consistent upward drift from 36h to 24h (+2–3¢), opposite to NYC/LAX (~0¢). Markets priced ≥50¢ at 36h in MIA have result_yes=0.57 (vs 0.30 in LAX, 0.35 in NYC) — a meaningful directional signal. However, the live MIA configs (rank 4/5 bands) are already calibrated around prices below 50¢, and the warm-side drift does not create a profitable early-entry case under actual config parameters (see Part 2).

### Conclusion on 36h window
We are not missing opportunities. The 36h window offers similar prices, similar EV, and marginally worse adverse selection due to escaped markets. **No change to WINDOW_TTX recommended.**

---

## Part 2: 2D Window Sweep — All Cities, Actual Config Parameters

Swept `window_start_ttx` ∈ {21h, 24h, 27h, 30h, 33h, 36h} × `window_duration` ∈ {3h, 6h, 9h, 12h, 18h} across all 13 active (series, season) configs using actual band_lo/band_hi/target/stop_frac from `strategies/temperature/config.py`.

### Aggregate results (total PnL across all configs)
| Start | Duration | N trades | vs baseline | Total PnL |
|-------|----------|----------|-------------|-----------|
| 24h | 3h | 440 | −41 | 4080¢ |
| **24h** | **6h** | **481** | **baseline** | **4187¢** |
| 21h | 6h | 483 | +2 | 4209¢ |
| 24h | 9h | 524 | +43 | 4142¢ |
| 21h | 18h | 595 | +114 | 4201¢ |
| 36h | 6h | 406 | −75 | 3693¢ |
| 36h | 18h | 503 | +22 | 3674¢ |

**The current (24h, 6h) baseline is near-optimal.** No combination materially outperforms it. The best alternative, (21h, 6h), adds only 2 trades and +22¢ — noise-level on this dataset.

### Duration tradeoff
Marginal EV by duration bracket:
- Trades added going from 3h → 6h window: ~41 trades averaging **+2.6¢ each** (worth keeping)
- Trades added going from 6h → 9h window: ~43 trades averaging **−1.1¢ each** (drag)
- Trades added going from 9h → 18h window: ~69 trades averaging **~0¢** (no net benefit)

The 6h window captures the "second tier" of entries (price drifts into band after open) which are modestly profitable. Beyond 6h, marginal entries are neutral-to-negative.

### Start TTX: flat between 21h–30h, degrades at 33h+
Total PnL is essentially flat for start TTX between 21h and 30h — most markets have their first in-band trade in a tight window regardless. Beyond 30h start, coverage drops (especially at 36h) and PnL degrades. Earlier than 21h is not tested but expected to degrade further (adverse selection of very early entries).

### Notable outliers (insufficient sample to act on, but worth monitoring)
- **LAX/Fall at 36h/3h:** EV jumps from 6.1¢ → 10.3¢, hit rate 80% → 91%. N=21. May reflect rank 4 fall prices being better-calibrated early. Monitor as data grows.
- **PHILS/Summer at 36h/3h:** EV jumps from 7.0¢ → 11.1¢, hit rate 75% → 89%. N=28, below the 80% N threshold. Insufficient sample.

### The 3h window note
Across 11 of 13 configs, a 3h window has higher EV/trade than the current 6h. If contract sizing scales up and fill quality / adverse selection in the tail of the window becomes a concern, tightening to 3h (accepting ~8% volume reduction, ~6% EV/trade increase) is a defensible lever.

---

## Summary of Recommendations

| Parameter | Current | Recommendation | Rationale |
|-----------|---------|---------------|-----------|
| WINDOW_TTX | 24h | **Keep** | Earlier windows don't improve EV; adverse selection at 36h |
| WINDOW_DURATION | 6h | **Keep** | Near-optimal; marginal entries beyond 6h are neutral-to-negative |
| Per-city variation | No | **No** | Start TTX flat between 21h–30h; not worth config complexity |
| Future revisit | — | LAX/Fall, PHILS/Summer at 36h | Both show large EV improvement but small N; revisit at 2× data |
