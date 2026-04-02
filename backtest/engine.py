"""
Event-driven backtest engine for Kalshi strategies.

Processes a sequence of MarketState snapshots in chronological order,
passes each to the strategy, and simulates order execution.

Fill model: configurable, defaults to conservative (fills only if
price crosses through, at the quoted price — never at mid).
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from strategies.base import MarketState, OrderIntent, PositionState, Strategy

logger = logging.getLogger(__name__)


@dataclass
class Fill:
    ticker: str
    side: str
    action: str
    price: int
    count: int
    timestamp: float
    tag: str


@dataclass
class SimPosition:
    ticker: str
    net_yes: int = 0
    realized_pnl: float = 0.0
    cost_basis: float = 0.0     # total cost of current open position

    @property
    def avg_cost(self) -> float:
        if self.net_yes == 0:
            return 0.0
        return self.cost_basis / self.net_yes

    def to_position_state(self) -> PositionState:
        return PositionState(
            ticker=self.ticker,
            net_yes=self.net_yes,
            realized_pnl=self.realized_pnl,
            avg_cost=self.avg_cost,
        )


def conservative_fill_model(intent: OrderIntent, state: MarketState) -> int:
    """
    Returns number of contracts filled.

    Buy yes: fills if intent.price >= best_ask (we lift the ask).
    Sell yes: fills if intent.price <= best_bid (we hit the bid).
    Never partial fills — either full size or nothing.

    For market making (passive quotes), this model is too aggressive.
    Use passive_fill_model for MM strategies.
    """
    if intent.action == "buy":
        if state.best_ask is not None and intent.price >= state.best_ask:
            return intent.count
    else:
        if state.best_bid is not None and intent.price <= state.best_bid:
            return intent.count
    return 0


def passive_fill_model(intent: OrderIntent, state: MarketState) -> int:
    """
    Passive (maker) fill model for MM strategies.

    Assumes our quotes sit in the book and fill when the market moves through them.
    Buy yes fills if state.best_ask <= intent.price (someone sells through our bid).
    Sell yes fills if state.best_bid >= intent.price (someone buys through our ask).

    This is still an approximation — real fills depend on queue position.
    """
    if intent.action == "buy":
        if state.best_ask is not None and state.best_ask <= intent.price:
            return intent.count
    else:
        if state.best_bid is not None and state.best_bid >= intent.price:
            return intent.count
    return 0


class BacktestEngine:
    """
    Runs a strategy against a stream of MarketState events.

    Parameters
    ----------
    strategy : Strategy
    fill_model : callable(OrderIntent, MarketState) -> int
        Returns number of contracts to fill. Default: passive_fill_model.
    maker_fee_rate : float
        Fee per contract as fraction of $1 face value. Kalshi charges per side.
    taker_fee_rate : float
    """

    def __init__(
        self,
        strategy: Strategy,
        fill_model: Callable[[OrderIntent, MarketState], int] = passive_fill_model,
        maker_fee_rate: float = 0.03,
        taker_fee_rate: float = 0.07,
    ) -> None:
        self.strategy = strategy
        self.fill_model = fill_model
        self.maker_fee_rate = maker_fee_rate
        self.taker_fee_rate = taker_fee_rate
        self._positions: dict[str, SimPosition] = defaultdict(
            lambda: SimPosition(ticker="")
        )
        self.fills: list[Fill] = []
        self.pnl_series: list[tuple[float, float]] = []  # (timestamp, cumulative_pnl)
        self._total_fees: float = 0.0

    def run(self, events: list[MarketState]) -> "BacktestResults":
        for state in events:
            pos = self._positions.get(state.ticker)
            if pos is None:
                pos = SimPosition(ticker=state.ticker)
                self._positions[state.ticker] = pos

            intents = self.strategy.on_market_update(state, pos.to_position_state())

            for intent in intents:
                filled = self.fill_model(intent, state)
                if filled > 0:
                    self._apply_fill(intent, filled, state.timestamp)

            cum_pnl = self._compute_unrealized_pnl(state) + sum(
                p.realized_pnl for p in self._positions.values()
            )
            self.pnl_series.append((state.timestamp, cum_pnl))

        return BacktestResults(
            fills=self.fills,
            pnl_series=self.pnl_series,
            final_positions={t: p.to_position_state() for t, p in self._positions.items()},
            total_fees=self._total_fees,
        )

    def _apply_fill(self, intent: OrderIntent, count: int, timestamp: float) -> None:
        pos = self._positions[intent.ticker]
        price_frac = intent.price / 100.0
        fee = self.maker_fee_rate * count  # per-contract fee in dollars

        if intent.action == "buy":
            cost = price_frac * count
            if pos.net_yes < 0:
                # Closing a short
                closed = min(count, -pos.net_yes)
                pnl = closed * (pos.avg_cost - price_frac)
                pos.realized_pnl += pnl
                pos.cost_basis += pos.avg_cost * closed
                pos.net_yes += closed
                remaining = count - closed
                if remaining > 0:
                    pos.net_yes += remaining
                    pos.cost_basis += price_frac * remaining
            else:
                pos.net_yes += count
                pos.cost_basis += cost
        else:
            proceed = price_frac * count
            if pos.net_yes > 0:
                closed = min(count, pos.net_yes)
                pnl = closed * (price_frac - pos.avg_cost)
                pos.realized_pnl += pnl
                pos.cost_basis -= pos.avg_cost * closed
                pos.net_yes -= closed
                remaining = count - closed
                if remaining > 0:
                    pos.net_yes -= remaining
                    pos.cost_basis += price_frac * remaining
            else:
                pos.net_yes -= count
                pos.cost_basis += proceed

        pos.realized_pnl -= fee
        self._total_fees += fee

        self.fills.append(Fill(
            ticker=intent.ticker,
            side=intent.side,
            action=intent.action,
            price=intent.price,
            count=count,
            timestamp=timestamp,
            tag=intent.tag,
        ))

        self.strategy.on_fill(intent, count, intent.price)

    def _compute_unrealized_pnl(self, state: MarketState) -> float:
        pos = self._positions.get(state.ticker)
        if not pos or pos.net_yes == 0 or state.mid is None:
            return 0.0
        return pos.net_yes * (state.mid / 100.0 - pos.avg_cost)


@dataclass
class BacktestResults:
    fills: list[Fill]
    pnl_series: list[tuple[float, float]]
    final_positions: dict[str, PositionState]
    total_fees: float
