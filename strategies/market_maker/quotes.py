"""
Basic inventory-aware market making strategy.

Framework: Avellaneda-Stoikov adapted for binary markets.

Fair value (q) is supplied externally — either a model estimate or
simply the last mid. The strategy quotes symmetrically around q,
then skews based on inventory to manage delta exposure.

Price grid is integer 0-99. All arithmetic in integer cents.
"""

from dataclasses import dataclass

from strategies.base import MarketState, OrderIntent, PositionState, Strategy


@dataclass
class MMParams:
    half_spread: int        # base half-spread in cents (e.g., 2 = quote bid/ask 2 apart from fair)
    max_inventory: int      # max net yes contracts before widening/stopping
    inventory_skew_per_lot: float   # cents to skew per net yes contract held
    quote_size: int         # contracts per side
    min_edge: int           # minimum edge required to place a quote (cents above/below fair)


class MarketMaker(Strategy):
    """
    Quotes bid and ask around a fair value estimate, skewing for inventory.

    Fair value must be set externally via set_fair_value() before updates.
    If no fair value is set, the strategy falls back to the orderbook mid.

    Skew rule (Avellaneda-Stoikov inventory model, simplified):
        bid = fair - half_spread - inventory_skew_per_lot * net_yes
        ask = fair + half_spread - inventory_skew_per_lot * net_yes

    As net_yes grows positive (long), both bid and ask shift down,
    making us less aggressive on buys and more aggressive on sells.
    """

    def __init__(self, tickers: list[str], params: MMParams) -> None:
        super().__init__(tickers)
        self.params = params
        self._fair_values: dict[str, float] = {}

    def set_fair_value(self, ticker: str, fair: float) -> None:
        """Set external fair value estimate (float, 0-100)."""
        self._fair_values[ticker] = fair

    def on_market_update(
        self,
        state: MarketState,
        position: PositionState | None,
    ) -> list[OrderIntent]:
        fair = self._fair_values.get(state.ticker) or state.mid
        if fair is None:
            return []

        net_yes = position.net_yes if position else 0
        p = self.params

        skew = p.inventory_skew_per_lot * net_yes
        bid_price = round(fair - p.half_spread - skew)
        ask_price = round(fair + p.half_spread - skew)

        # Clamp to valid range
        bid_price = max(1, min(99, bid_price))
        ask_price = max(1, min(99, ask_price))

        if ask_price <= bid_price:
            return []  # skew has collapsed the spread

        intents: list[OrderIntent] = []

        # Only quote bid if not at/past max long inventory
        if net_yes < p.max_inventory:
            intents.append(OrderIntent(
                ticker=state.ticker,
                side="yes",
                action="buy",
                price=bid_price,
                count=p.quote_size,
                tag="bid",
            ))

        # Only quote ask if not at/past max short inventory
        if net_yes > -p.max_inventory:
            intents.append(OrderIntent(
                ticker=state.ticker,
                side="yes",
                action="sell",
                price=ask_price,
                count=p.quote_size,
                tag="ask",
            ))

        return intents
