"""
Backtest performance metrics.

All returns are computed on a dollar basis (contract face value = $1).
"""

import numpy as np
import pandas as pd

from backtest.engine import BacktestResults, Fill


class BacktestMetrics:
    def __init__(self, results: BacktestResults) -> None:
        self.results = results
        self._pnl_df = pd.DataFrame(results.pnl_series, columns=["timestamp", "cum_pnl"])
        self._pnl_df["pnl"] = self._pnl_df["cum_pnl"].diff().fillna(0.0)

    @property
    def total_pnl(self) -> float:
        return self._pnl_df["cum_pnl"].iloc[-1] if len(self._pnl_df) else 0.0

    @property
    def total_fees(self) -> float:
        return self.results.total_fees

    @property
    def gross_pnl(self) -> float:
        return self.total_pnl + self.total_fees

    @property
    def num_fills(self) -> int:
        return len(self.results.fills)

    @property
    def total_volume(self) -> int:
        return sum(f.count for f in self.results.fills)

    @property
    def sharpe(self) -> float:
        """Annualized Sharpe on per-event pnl. Assumes events are ~equally spaced."""
        pnl = self._pnl_df["pnl"].values
        if pnl.std() == 0:
            return 0.0
        return float(np.sqrt(len(pnl)) * pnl.mean() / pnl.std())

    @property
    def max_drawdown(self) -> float:
        cum = self._pnl_df["cum_pnl"].values
        peak = np.maximum.accumulate(cum)
        drawdown = cum - peak
        return float(drawdown.min())

    @property
    def win_rate(self) -> float:
        """Fraction of fills that contributed positive pnl on net."""
        if not self.results.fills:
            return 0.0
        # Approximate: fraction of non-zero pnl periods that are positive
        pnl = self._pnl_df["pnl"].values
        nonzero = pnl[pnl != 0]
        if len(nonzero) == 0:
            return 0.0
        return float((nonzero > 0).mean())

    @property
    def fill_breakdown(self) -> pd.DataFrame:
        return pd.DataFrame([
            {
                "ticker": f.ticker,
                "action": f.action,
                "side": f.side,
                "price": f.price,
                "count": f.count,
                "tag": f.tag,
                "timestamp": f.timestamp,
            }
            for f in self.results.fills
        ])

    def summary(self) -> dict:
        return {
            "total_pnl": round(self.total_pnl, 4),
            "gross_pnl": round(self.gross_pnl, 4),
            "total_fees": round(self.total_fees, 4),
            "num_fills": self.num_fills,
            "total_volume": self.total_volume,
            "sharpe": round(self.sharpe, 3),
            "max_drawdown": round(self.max_drawdown, 4),
            "win_rate": round(self.win_rate, 3),
        }
