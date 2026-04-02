"""
Risk manager: position limits, exposure, and pre-trade checks.

Delta = net yes exposure in contracts (positive = long, negative = short).
For binary markets, delta is the primary risk dimension.

Correlation across markets (e.g., multiple temperature markets in the same
city on adjacent days) is tracked but not yet hedged automatically.
"""

from dataclasses import dataclass, field

from strategies.base import OrderIntent, PositionState


@dataclass
class RiskLimits:
    max_position_per_market: int    # max |net_yes| per ticker
    max_total_delta: int            # max sum of |net_yes| across all markets
    max_loss_per_market: float      # max realized + unrealized loss per ticker ($)
    max_total_loss: float           # max total portfolio loss ($)


class RiskError(Exception):
    pass


class RiskManager:
    """
    Stateful risk manager. Call check_order() before submitting any order.
    Update state via update_position() after each fill.

    Not thread-safe — use a single-threaded event loop or add locking.
    """

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self._positions: dict[str, PositionState] = {}
        self._unrealized: dict[str, float] = {}

    def update_position(self, position: PositionState) -> None:
        self._positions[position.ticker] = position

    def update_unrealized(self, ticker: str, unrealized_pnl: float) -> None:
        self._unrealized[ticker] = unrealized_pnl

    def check_order(self, intent: OrderIntent) -> None:
        """
        Raises RiskError if the order would breach any limit.
        Call this before submitting to the exchange.
        """
        self._check_position_limit(intent)
        self._check_total_delta(intent)
        self._check_loss_limit(intent)

    def _projected_net_yes(self, intent: OrderIntent) -> int:
        current = self._positions.get(intent.ticker)
        net = current.net_yes if current else 0
        delta = intent.count if intent.action == "buy" else -intent.count
        if intent.side == "no":
            delta = -delta
        return net + delta

    def _check_position_limit(self, intent: OrderIntent) -> None:
        projected = self._projected_net_yes(intent)
        if abs(projected) > self.limits.max_position_per_market:
            raise RiskError(
                f"{intent.ticker}: projected |net_yes|={abs(projected)} "
                f"exceeds per-market limit {self.limits.max_position_per_market}"
            )

    def _check_total_delta(self, intent: OrderIntent) -> None:
        current = self._positions.get(intent.ticker)
        current_delta = abs(current.net_yes) if current else 0
        projected_delta = abs(self._projected_net_yes(intent))
        other_delta = sum(
            abs(p.net_yes)
            for t, p in self._positions.items()
            if t != intent.ticker
        )
        total = other_delta + projected_delta
        if total > self.limits.max_total_delta:
            raise RiskError(
                f"Total delta {total} would exceed limit {self.limits.max_total_delta}"
            )

    def _check_loss_limit(self, intent: OrderIntent) -> None:
        pos = self._positions.get(intent.ticker)
        if pos:
            unrealized = self._unrealized.get(intent.ticker, 0.0)
            market_loss = -(pos.realized_pnl + unrealized)
            if market_loss > self.limits.max_loss_per_market:
                raise RiskError(
                    f"{intent.ticker}: loss ${market_loss:.2f} exceeds "
                    f"per-market limit ${self.limits.max_loss_per_market:.2f}"
                )

        total_realized = sum(p.realized_pnl for p in self._positions.values())
        total_unrealized = sum(self._unrealized.values())
        total_loss = -(total_realized + total_unrealized)
        if total_loss > self.limits.max_total_loss:
            raise RiskError(
                f"Total loss ${total_loss:.2f} exceeds limit ${self.limits.max_total_loss:.2f}"
            )

    @property
    def total_delta(self) -> int:
        return sum(abs(p.net_yes) for p in self._positions.values())

    @property
    def positions_summary(self) -> dict:
        return {
            ticker: {
                "net_yes": p.net_yes,
                "realized_pnl": round(p.realized_pnl, 4),
                "unrealized_pnl": round(self._unrealized.get(ticker, 0.0), 4),
            }
            for ticker, p in self._positions.items()
        }
