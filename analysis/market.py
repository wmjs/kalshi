"""
Market analysis utilities.

Covers: orderbook microstructure, spread/depth analysis, fair value
estimation, and time-series stats on price/volume.

Designed for exploratory use and for feeding fair value into strategies.
"""

import numpy as np
import pandas as pd
from scipy import stats


class OrderbookAnalyzer:
    """
    Analyzes a single orderbook snapshot.

    Kalshi orderbook format:
        {"yes": [[price, size], ...], "no": [[price, size], ...]}
    Yes bids are sorted descending, no bids sorted ascending (by price).
    A no bid at price P is equivalent to a yes ask at (100 - P).
    """

    def __init__(self, orderbook: dict) -> None:
        yes_levels = orderbook.get("yes", [])
        no_levels = orderbook.get("no", [])

        # Normalize to yes-side: bids (buy yes) and asks (sell yes)
        self.bids = sorted(yes_levels, key=lambda x: -x[0])
        self.asks = sorted(
            [[100 - p, s] for p, s in no_levels],
            key=lambda x: x[0],
        )

    @property
    def best_bid(self) -> int | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> int | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> int | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def depth(self, levels: int = 5) -> dict:
        """Cumulative depth on each side up to N levels."""
        bid_depth = sum(s for _, s in self.bids[:levels])
        ask_depth = sum(s for _, s in self.asks[:levels])
        return {"bid_depth": bid_depth, "ask_depth": ask_depth, "imbalance": bid_depth - ask_depth}

    def volume_weighted_mid(self, levels: int = 3) -> float | None:
        """VWAP-style mid weighting the top N levels on each side."""
        bids = self.bids[:levels]
        asks = self.asks[:levels]
        if not bids or not asks:
            return None
        bid_vwap = sum(p * s for p, s in bids) / sum(s for _, s in bids)
        ask_vwap = sum(p * s for p, s in asks) / sum(s for _, s in asks)
        return (bid_vwap + ask_vwap) / 2.0


class MarketAnalyzer:
    """
    Time-series analysis on a sequence of market snapshots.

    Expects a DataFrame with columns: timestamp, best_bid, best_ask,
    last_price, volume (optional).
    """

    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df.copy().sort_values("timestamp").reset_index(drop=True)
        if "mid" not in self.df.columns:
            self.df["mid"] = (self.df["best_bid"] + self.df["best_ask"]) / 2.0
        if "spread" not in self.df.columns:
            self.df["spread"] = self.df["best_ask"] - self.df["best_bid"]

    def spread_stats(self) -> dict:
        s = self.df["spread"].dropna()
        return {
            "mean": float(s.mean()),
            "median": float(s.median()),
            "std": float(s.std()),
            "p5": float(s.quantile(0.05)),
            "p95": float(s.quantile(0.95)),
        }

    def price_autocorrelation(self, lags: int = 10) -> pd.Series:
        """Autocorrelation of mid-price changes at lags 1..N."""
        returns = self.df["mid"].diff().dropna()
        return pd.Series(
            [returns.autocorr(lag=i) for i in range(1, lags + 1)],
            index=range(1, lags + 1),
            name="autocorr",
        )

    def kyle_lambda(self) -> float | None:
        """
        Kyle's lambda (price impact coefficient) estimated via OLS:
            delta_mid ~ lambda * signed_volume

        Requires 'volume' and 'last_price' columns. Returns None if unavailable.
        Positive lambda = buying pressure moves price up.
        """
        needed = {"volume", "last_price", "mid"}
        if not needed.issubset(self.df.columns):
            return None
        df = self.df.dropna(subset=list(needed)).copy()
        df["delta_mid"] = df["mid"].diff()
        df["signed_vol"] = df["volume"] * np.sign(df["last_price"] - df["mid"].shift(1))
        df = df.dropna()
        if len(df) < 10:
            return None
        slope, _, _, _, _ = stats.linregress(df["signed_vol"], df["delta_mid"])
        return float(slope)

    def realized_volatility(self, window: int | None = None) -> float:
        """
        Realized vol of mid-price changes (in probability points).
        If window given, uses the last N observations.
        """
        mid = self.df["mid"] if window is None else self.df["mid"].iloc[-window:]
        returns = mid.diff().dropna()
        return float(returns.std())

    def mean_reversion_halflife(self) -> float | None:
        """
        Ornstein-Uhlenbeck half-life from AR(1) fit on mid-price.
        Half-life = -ln(2) / ln(|beta|) where beta is the AR(1) coefficient.
        Returns None if the series is explosive (|beta| >= 1).
        """
        mid = self.df["mid"].dropna().values
        if len(mid) < 20:
            return None
        lag = mid[:-1]
        current = mid[1:]
        beta, _, _, _, _ = stats.linregress(lag, current)
        if abs(beta) >= 1.0:
            return None
        return float(-np.log(2) / np.log(abs(beta)))
