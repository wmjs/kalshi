"""
Abstract base class for all strategies.

A Strategy consumes market state and produces order intents.
Execution is handled separately (live executor or backtest engine).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OrderIntent:
    """
    A desired order action. Not yet submitted — execution layer decides
    whether and how to act on it.
    """
    ticker: str
    side: str           # "yes" or "no"
    action: str         # "buy" or "sell"
    price: int          # 1-99
    count: int
    tag: str = ""       # strategy-internal label (e.g., "bid", "ask", "hedge")


@dataclass
class MarketState:
    """
    Snapshot of a single market passed to the strategy on each update.
    """
    ticker: str
    best_bid: int | None        # highest yes bid (0-99)
    best_ask: int | None        # lowest yes ask (0-99)
    bid_size: int | None
    ask_size: int | None
    last_price: int | None
    last_trade_size: int | None
    timestamp: float            # unix epoch seconds
    extra: dict[str, Any] = field(default_factory=dict)

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


@dataclass
class PositionState:
    """Current inventory for a single market."""
    ticker: str
    net_yes: int    # positive = long yes, negative = short yes (= long no)
    realized_pnl: float
    avg_cost: float


class Strategy(ABC):
    """
    Base class for all strategies.

    Subclasses implement on_market_update() and return a (possibly empty)
    list of OrderIntents. They should not submit orders directly.
    """

    def __init__(self, tickers: list[str]) -> None:
        self.tickers = tickers

    @abstractmethod
    def on_market_update(
        self,
        state: MarketState,
        position: PositionState | None,
    ) -> list[OrderIntent]:
        """
        Called on each market data event. Return desired order actions.
        Returning an empty list means no action.
        """
        ...

    def on_fill(self, intent: OrderIntent, filled_count: int, fill_price: int) -> None:
        """Optional hook called when one of our orders fills."""
        pass

    def on_cancel(self, intent: OrderIntent) -> None:
        """Optional hook called when one of our orders is cancelled."""
        pass
