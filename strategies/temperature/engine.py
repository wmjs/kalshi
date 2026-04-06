"""
Event-driven execution engine for the multi-city temperature directional strategy.

State machine per market:
    PENDING → WINDOW_OPEN → ENTRY_PENDING → ENTERED → DONE
    PENDING → WINDOW_OPEN → FILTERED → DONE

Window opens when TTX first drops to ~24h. Entry logic classifies the
opening price and posts limit orders. Fill events arrive via WebSocket
order_fill channel for instant detection. Bracket orders (stop + target)
are posted immediately on entry fill.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from api.alerts import send_alert
from api.client import KalshiAPIError, KalshiClient
from api.websocket import KalshiWebSocket
from risk.manager import RiskError, RiskManager
from strategies.base import OrderIntent, PositionState
from strategies.temperature.config import (
    ACTIVE_SERIES,
    WINDOW_BUFFER,
    WINDOW_DURATION,
    WINDOW_TTX,
    active_config,
    assign_rank,
    current_season,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Market state
# ---------------------------------------------------------------------------

@dataclass
class MarketSetup:
    ticker: str
    series: str
    season: str
    config: dict
    close_time: datetime          # UTC-aware

    state: str = "PENDING"
    # States:
    #   PENDING       — waiting for window to open (TTX > 24h)
    #   WINDOW_OPEN   — transient; entry logic runs immediately, transitions away
    #   FILTERED      — from_below or at_open_only violation; no trade
    #   ENTRY_PENDING — entry order posted, waiting for fill or 6h expiry
    #   ENTERED       — in position; bracket orders live
    #   DONE          — terminal; outcome recorded

    window_open_time: datetime | None = None
    open_price: int | None = None     # first trade price at window open
    approach: str | None = None       # "at_open" | "from_above" | "filtered"

    entry_order_id: str | None = None
    entry_price: int | None = None
    stop_order_id: str | None = None
    target_order_id: str | None = None

    # outcome values: "target" | "stop" | "settlement_yes" | "settlement_no" | "filtered" | "no_fill"
    outcome: str | None = None
    exit_price: int | None = None
    net_pnl_cents: float | None = None

    # asyncio tasks for scheduled cancels / settlement checks
    _cancel_task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    _settle_task: asyncio.Task | None = field(default=None, repr=False, compare=False)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class TemperatureEngine:
    """
    Consumes WebSocket events (trade + order_fill) to advance per-market state
    machines. Uses REST only for market discovery and order management.
    """

    def __init__(
        self,
        client: KalshiClient,
        ws: KalshiWebSocket | None,
        risk: RiskManager,
        contracts: int = 1,
        log_path: Path | None = None,
    ) -> None:
        self.client    = client
        self.ws        = ws
        self.risk      = risk
        self.contracts = contracts
        self.log_path  = log_path
        self._setups: dict[str, MarketSetup] = {}   # ticker → MarketSetup

    # -----------------------------------------------------------------------
    # Market discovery
    # -----------------------------------------------------------------------

    async def discover_todays_markets(self) -> list[MarketSetup]:
        """
        For each active series, find today's target-rank market via REST.
        Returns list of MarketSetup objects (one per active series/season pair).
        """
        now    = datetime.now(timezone.utc)
        today  = now.date()
        setups = []

        for series in ACTIVE_SERIES:
            cfg = active_config(series, now)
            if cfg is None:
                logger.debug("%s: no active config for %s", series, current_season(now))
                continue

            try:
                resp    = await self.client.get_markets(series_ticker=series, limit=100)
                markets = resp.get("markets", [])
            except KalshiAPIError as e:
                logger.error("%s: failed to fetch markets: %s", series, e)
                continue

            # Filter to markets that close today or tomorrow and are still open.
            # We enter ~24h before close, so a market closing tomorrow is already
            # in (or approaching) its entry window today.
            todays = []
            for m in markets:
                ct_raw = m.get("close_time", "")
                try:
                    ct = datetime.fromisoformat(ct_raw.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue
                ttx = (ct - now).total_seconds()
                # Accept markets closing between now and ~36h from now
                if 0 < ttx <= 36 * 3600:
                    m["_close_time_parsed"] = ct
                    todays.append(m)

            if not todays:
                logger.debug("%s: no markets found for %s", series, today)
                continue

            ranked = assign_rank(todays)
            target_rank = cfg["rank"]
            market = ranked.get(target_rank)
            if market is None:
                logger.warning("%s: rank %d not found (found ranks %s)",
                               series, target_rank, sorted(ranked.keys()))
                continue

            season = current_season(now)
            setup = MarketSetup(
                ticker     = market["ticker"],
                series     = series,
                season     = season,
                config     = cfg,
                close_time = market["_close_time_parsed"],
            )
            setups.append(setup)
            logger.info("Discovered %s  rank=%d  season=%s  band=[%d,%d)  target=%d  stop=%.0f%%",
                        setup.ticker, target_rank, season,
                        cfg["band_lo"], cfg["band_hi"], cfg["target"],
                        cfg["stop_frac"] * 100)

        for s in setups:
            self._setups[s.ticker] = s

        return setups

    # -----------------------------------------------------------------------
    # WebSocket event handlers
    # -----------------------------------------------------------------------

    async def on_trade(self, msg: dict) -> None:
        """
        Handle a 'trade' WebSocket message. Detects window open and records
        the opening price for PENDING setups.

        Expected message structure (Kalshi trade channel):
            {"type": "trade", "msg": {"market_ticker": "...", "yes_price": ..., ...}}
        """
        inner  = msg.get("msg", msg)
        ticker = inner.get("market_ticker") or inner.get("ticker")
        if ticker not in self._setups:
            return

        setup = self._setups[ticker]
        if setup.state != "PENDING":
            return

        # Extract price — field name may vary; try common names
        price = inner.get("yes_price") or inner.get("price")
        if price is None:
            return
        price = int(price)

        # Check if window has opened
        now = datetime.now(timezone.utc)
        ttx = (setup.close_time - now).total_seconds()
        if ttx > WINDOW_TTX + WINDOW_BUFFER:
            return

        # Window just opened — record opening price and apply entry logic
        setup.window_open_time = now
        setup.open_price       = price
        logger.info("%s: window open  price=%d  ttx=%.1fh", ticker, price, ttx / 3600)
        self._log("window_open", setup, price=price, ttx_hours=round(ttx / 3600, 2))

        setup.state = "WINDOW_OPEN"
        await self._apply_entry_logic(setup)

    async def on_order_fill(self, msg: dict) -> None:
        """
        Handle an 'order_fill' WebSocket message.

        Expected message structure:
            {"type": "fill", "msg": {"order_id": "...", "market_ticker": "...", "yes_price": ..., ...}}
        """
        inner    = msg.get("msg", msg)
        order_id = inner.get("order_id")
        ticker   = inner.get("market_ticker") or inner.get("ticker")
        price    = inner.get("yes_price") or inner.get("price")

        if not order_id or ticker not in self._setups:
            return

        setup = self._setups[ticker]
        if price is not None:
            price = int(price)

        if order_id == setup.entry_order_id and setup.state == "ENTRY_PENDING":
            await self._on_entry_filled(setup, price)

        elif order_id == setup.stop_order_id and setup.state == "ENTERED":
            await self._on_exit_filled(setup, price, outcome="stop")

        elif order_id == setup.target_order_id and setup.state == "ENTERED":
            await self._on_exit_filled(setup, price, outcome="target")

    # -----------------------------------------------------------------------
    # Entry logic
    # -----------------------------------------------------------------------

    async def _apply_entry_logic(self, setup: MarketSetup) -> None:
        """
        Classify opening price and post entry order (or filter).
        Called immediately after window_open_time is detected.
        """
        price  = setup.open_price
        cfg    = setup.config
        ticker = setup.ticker

        # from_below filter
        if price < cfg["band_lo"]:
            setup.approach = "filtered"
            setup.state    = "FILTERED"
            setup.outcome  = "filtered"
            logger.info("%s: FILTERED (from_below)  price=%d < band_lo=%d", ticker, price, cfg["band_lo"])
            self._log("filtered", setup, reason="from_below", price=price)
            return

        # at_open_only exception (NY Spring rank 5)
        if cfg.get("at_open_only") and price >= cfg["band_hi"]:
            setup.approach = "filtered"
            setup.state    = "FILTERED"
            setup.outcome  = "filtered"
            logger.info("%s: FILTERED (at_open_only, price above band)  price=%d", ticker, price)
            self._log("filtered", setup, reason="at_open_only_above_band", price=price)
            return

        # Determine entry price and approach
        if cfg["band_lo"] <= price < cfg["band_hi"]:
            setup.approach  = "at_open"
            entry_price_int = price
        else:
            # from_above: price >= band_hi; post resting bid inside band
            setup.approach  = "from_above"
            entry_price_int = cfg["band_hi"] - 1

        # Risk check before posting
        intent = OrderIntent(
            ticker=ticker, side="yes", action="buy",
            price=entry_price_int, count=self.contracts,
        )
        try:
            self.risk.check_order(intent)
        except RiskError as e:
            logger.warning("%s: risk check failed: %s", ticker, e)
            self._log("risk_blocked", setup, reason=str(e))
            setup.state   = "DONE"
            setup.outcome = "risk_blocked"
            return

        # Post entry order
        try:
            resp = await self.client.create_order(
                ticker=ticker, side="yes", action="buy",
                count=self.contracts, price=entry_price_int,
            )
            order = resp.get("order", resp)
            setup.entry_order_id = order.get("order_id") or order.get("id")
        except KalshiAPIError as e:
            logger.error("%s: failed to post entry order: %s", ticker, e)
            setup.state   = "DONE"
            setup.outcome = "order_error"
            return

        setup.state = "ENTRY_PENDING"
        logger.info("%s: ENTRY_PENDING  approach=%s  price=%d  order_id=%s",
                    ticker, setup.approach, entry_price_int, setup.entry_order_id)
        self._log("entry_posted", setup, approach=setup.approach,
                  entry_price=entry_price_int, order_id=setup.entry_order_id)

        # Schedule 6h window expiry cancel
        setup._cancel_task = asyncio.create_task(self._cancel_after_window(setup))

        # Schedule settlement check at close_time + 5min
        setup._settle_task = asyncio.create_task(self._handle_settlement(setup))

    async def _cancel_after_window(self, setup: MarketSetup) -> None:
        """Cancel unfilled entry order after the 6h window expires."""
        await asyncio.sleep(WINDOW_DURATION)
        if setup.state != "ENTRY_PENDING":
            return
        logger.info("%s: window expired, cancelling entry order %s", setup.ticker, setup.entry_order_id)
        try:
            await self.client.cancel_order(setup.entry_order_id)
        except KalshiAPIError as e:
            logger.warning("%s: cancel failed (may already be filled): %s", setup.ticker, e)
        if setup.state == "ENTRY_PENDING":   # could have filled between sleep and cancel
            setup.state   = "DONE"
            setup.outcome = "no_fill"
            self._log("no_fill", setup)

    # -----------------------------------------------------------------------
    # Fill handling
    # -----------------------------------------------------------------------

    async def _on_entry_filled(self, setup: MarketSetup, fill_price: int | None) -> None:
        """Entry order confirmed. Post stop + target bracket orders."""
        if setup._cancel_task:
            setup._cancel_task.cancel()

        # Use fill_price from WS if available; fall back to entry order price
        cfg = setup.config
        entry = fill_price if fill_price is not None else (cfg["band_lo"] + cfg["band_hi"]) // 2
        setup.entry_price = entry

        stop_price   = max(1, round(cfg["stop_frac"] * entry))
        target_price = cfg["target"]

        # Post stop (resting sell)
        try:
            stop_resp = await self.client.create_order(
                ticker=setup.ticker, side="yes", action="sell",
                count=self.contracts, price=stop_price,
            )
            stop_order = stop_resp.get("order", stop_resp)
            setup.stop_order_id = stop_order.get("order_id") or stop_order.get("id")
        except KalshiAPIError as e:
            logger.error("%s: failed to post stop order: %s", setup.ticker, e)

        # Post target (resting sell)
        try:
            tgt_resp = await self.client.create_order(
                ticker=setup.ticker, side="yes", action="sell",
                count=self.contracts, price=target_price,
            )
            tgt_order = tgt_resp.get("order", tgt_resp)
            setup.target_order_id = tgt_order.get("order_id") or tgt_order.get("id")
        except KalshiAPIError as e:
            logger.error("%s: failed to post target order: %s", setup.ticker, e)

        # Update risk manager with new position
        pos = PositionState(ticker=setup.ticker, net_yes=self.contracts,
                            realized_pnl=0.0, avg_cost=entry / 100.0)
        self.risk.update_position(pos)

        setup.state = "ENTERED"
        logger.info("%s: ENTERED  entry=%d  stop=%d  target=%d", setup.ticker, entry, stop_price, target_price)
        self._log("entered", setup, entry_price=entry, stop_price=stop_price,
                  target_price=target_price, stop_id=setup.stop_order_id, target_id=setup.target_order_id)
        asyncio.create_task(send_alert(
            f"ENTERED {setup.ticker} @{entry}c  stop={stop_price}c  target={target_price}c"
        ))

    async def _on_exit_filled(self, setup: MarketSetup, fill_price: int | None, outcome: str) -> None:
        """Stop or target filled. Cancel the other bracket leg."""
        other_id = setup.target_order_id if outcome == "stop" else setup.stop_order_id
        if other_id:
            try:
                await self.client.cancel_order(other_id)
            except KalshiAPIError as e:
                logger.warning("%s: cancel bracket failed: %s", setup.ticker, e)

        exit_price = fill_price if fill_price is not None else (
            setup.config["target"] if outcome == "target"
            else round(setup.config["stop_frac"] * (setup.entry_price or 0))
        )
        setup.exit_price = exit_price
        setup.outcome    = outcome
        setup.net_pnl_cents = self._compute_pnl(setup)

        # Clear position in risk manager
        flat = PositionState(ticker=setup.ticker, net_yes=0,
                             realized_pnl=setup.net_pnl_cents / 100.0, avg_cost=0.0)
        self.risk.update_position(flat)

        if setup._settle_task:
            setup._settle_task.cancel()

        setup.state = "DONE"
        logger.info("%s: DONE  outcome=%s  exit=%d  pnl=%.2f¢",
                    setup.ticker, outcome, exit_price, setup.net_pnl_cents)
        self._log("exited", setup, outcome=outcome, exit_price=exit_price,
                  net_pnl_cents=round(setup.net_pnl_cents, 2))
        icon = "TARGET" if outcome == "target" else "STOP"
        asyncio.create_task(send_alert(
            f"{icon} {setup.ticker} @{exit_price}c  P&L: {setup.net_pnl_cents:+.1f}c"
        ))

    # -----------------------------------------------------------------------
    # Settlement handler (fallback for markets that settle without bracket fill)
    # -----------------------------------------------------------------------

    async def _handle_settlement(self, setup: MarketSetup) -> None:
        """
        Scheduled at close_time + 5 minutes. If still ENTERED, check REST
        position to determine settlement outcome (binary 0 or 100).
        """
        now    = datetime.now(timezone.utc)
        delay  = (setup.close_time - now).total_seconds() + 300   # 5 min after close
        if delay > 0:
            await asyncio.sleep(delay)

        if setup.state != "ENTERED":
            return

        logger.info("%s: checking settlement via REST", setup.ticker)
        try:
            resp      = await self.client.get_positions()
            positions = resp.get("market_positions", [])
            ticker_pos = next((p for p in positions if p.get("ticker") == setup.ticker), None)
        except KalshiAPIError as e:
            logger.error("%s: get_positions failed at settlement: %s", setup.ticker, e)
            return

        if ticker_pos is None or ticker_pos.get("position", 0) == 0:
            # Position is flat — settlement happened
            # Infer outcome from the market's final result if available
            try:
                mkt = await self.client.get_market(setup.ticker)
                result = mkt.get("market", mkt).get("result", "")
                outcome = "settlement_yes" if result == "yes" else "settlement_no"
                exit_price = 100 if result == "yes" else 0
            except KalshiAPIError:
                outcome    = "settlement_unknown"
                exit_price = None

            setup.exit_price = exit_price
            setup.outcome    = outcome
            setup.net_pnl_cents = self._compute_pnl(setup) if exit_price is not None else None

            flat = PositionState(ticker=setup.ticker, net_yes=0,
                                 realized_pnl=(setup.net_pnl_cents or 0) / 100.0, avg_cost=0.0)
            self.risk.update_position(flat)

            setup.state = "DONE"
            logger.info("%s: settled  outcome=%s  pnl=%.2f¢",
                        setup.ticker, outcome, setup.net_pnl_cents or 0)
            self._log("settled", setup, outcome=outcome, exit_price=exit_price,
                      net_pnl_cents=round(setup.net_pnl_cents, 2) if setup.net_pnl_cents is not None else None)
        else:
            # Still holding — settlement may not have processed yet
            logger.warning("%s: position still open 5min after close — check manually", setup.ticker)

    # -----------------------------------------------------------------------
    # P&L computation
    # -----------------------------------------------------------------------

    def _taker_fee_cents(self, price: int) -> float:
        p = price / 100.0
        return 0.07 * p * (1.0 - p) * 100.0  # in cents, taker rate

    def _compute_pnl(self, setup: MarketSetup) -> float:
        """
        Net P&L in cents per contract. Uses taker fees at both legs (conservative,
        matches backtest assumptions). In live execution with resting exits, exit
        fee is 0 — so actual P&L will be slightly higher.
        """
        if setup.entry_price is None or setup.exit_price is None:
            return 0.0
        gross = (setup.exit_price - setup.entry_price) * self.contracts
        fees  = (self._taker_fee_cents(setup.entry_price) +
                 self._taker_fee_cents(setup.exit_price)) * self.contracts
        return gross - fees

    # -----------------------------------------------------------------------
    # TTX poll — fallback for quiet markets with no WS trade events
    # -----------------------------------------------------------------------

    _POLL_INTERVAL = 5 * 60   # seconds between REST checks

    async def _ttx_poll_loop(self) -> None:
        """
        Periodically check PENDING setups whose TTX has crossed the window
        threshold, in case no WS trade event arrived to trigger on_trade().

        Fetches the current market price via REST and synthesises the same
        window-open logic that on_trade() would have applied.
        """
        while True:
            await asyncio.sleep(self._POLL_INTERVAL)

            now     = datetime.now(timezone.utc)
            pending = [s for s in self._setups.values() if s.state == "PENDING"]
            if not pending:
                return

            for setup in pending:
                ttx = (setup.close_time - now).total_seconds()
                if ttx > WINDOW_TTX + WINDOW_BUFFER:
                    continue

                # TTX has crossed the threshold with no WS trade. Fetch current
                # price via REST to use as the synthetic opening price.
                logger.warning(
                    "%s: window should be open (ttx=%.1fh) but no WS trade received — "
                    "polling REST for current price",
                    setup.ticker, ttx / 3600,
                )
                try:
                    resp  = await self.client.get_market(setup.ticker)
                    mkt   = resp.get("market", resp)
                    # last_price_dollars is a string like "0.3900"; convert to cents int
                    last  = mkt.get("last_price_dollars") or mkt.get("yes_bid_dollars", "0")
                    price = round(float(last) * 100)
                except (KalshiAPIError, ValueError, TypeError) as e:
                    logger.error("%s: poll failed to fetch price: %s", setup.ticker, e)
                    continue

                if price == 0:
                    logger.warning("%s: poll got price=0, skipping", setup.ticker)
                    continue

                # Synthesise the same window-open path as on_trade()
                setup.window_open_time = now
                setup.open_price       = price
                logger.info("%s: window open (via poll)  price=%d  ttx=%.1fh",
                            setup.ticker, price, ttx / 3600)
                self._log("window_open", setup, price=price,
                          ttx_hours=round(ttx / 3600, 2), via="poll")

                setup.state = "WINDOW_OPEN"
                await self._apply_entry_logic(setup)

    # -----------------------------------------------------------------------
    # Main run loop
    # -----------------------------------------------------------------------

    async def run(self) -> None:
        """
        Subscribe to WebSocket and consume events until all setups reach DONE.
        Calls discover_todays_markets() only if no setups are loaded yet.
        """
        if not self._setups:
            await self.discover_todays_markets()
        setups = list(self._setups.values())
        if not setups:
            logger.info("No active setups for today. Exiting.")
            return

        tickers = [s.ticker for s in setups]
        logger.info("Subscribing to %d tickers: %s", len(tickers), tickers)
        self._log("startup", tickers=tickers, n_setups=len(setups))

        await self.ws.subscribe(["trade", "order_fill"], tickers)

        # Background TTX poll — catches quiet markets where no WS trade arrives
        poll_task = asyncio.create_task(self._ttx_poll_loop())

        try:
            async for msg in self.ws:
                msg_type = msg.get("type", "")

                if msg_type == "trade":
                    await self.on_trade(msg)
                elif msg_type in ("fill", "order_fill"):
                    await self.on_order_fill(msg)

                # Exit when all setups are terminal
                if all(s.state == "DONE" for s in self._setups.values()):
                    logger.info("All setups complete. Exiting event loop.")
                    break
        finally:
            poll_task.cancel()

        self._daily_summary()

    def _daily_summary(self) -> None:
        """Log and print a daily P&L summary."""
        total_pnl = sum(
            s.net_pnl_cents for s in self._setups.values()
            if s.net_pnl_cents is not None
        )
        rows = []
        for s in self._setups.values():
            rows.append({
                "ticker":        s.ticker,
                "state":         s.state,
                "outcome":       s.outcome,
                "approach":      s.approach,
                "entry_price":   s.entry_price,
                "exit_price":    s.exit_price,
                "net_pnl_cents": round(s.net_pnl_cents, 2) if s.net_pnl_cents is not None else None,
            })

        self._log("daily_summary", total_pnl_cents=round(total_pnl, 2), setups=rows)
        print(f"\n{'':=<60}")
        print(f"  DAILY SUMMARY  (total P&L: {total_pnl:+.2f}¢)")
        print(f"{'':=<60}")
        for r in rows:
            pnl_str = f"{r['net_pnl_cents']:+.2f}¢" if r["net_pnl_cents"] is not None else "—"
            print(f"  {r['ticker']:30s}  {r['outcome'] or r['state']:20s}  {pnl_str}")
        print()

        n_trades   = sum(1 for s in self._setups.values() if s.outcome not in (None, "filtered", "risk_blocked"))
        n_filtered = sum(1 for s in self._setups.values() if s.outcome == "filtered")
        asyncio.get_running_loop().create_task(send_alert(
            f"Daily P&L: {total_pnl:+.1f}c  ({n_trades} trades, {n_filtered} filtered)"
        ))

    # -----------------------------------------------------------------------
    # Logging
    # -----------------------------------------------------------------------

    def _log(self, event: str, setup: MarketSetup | None = None, **kwargs) -> None:
        record: dict[str, Any] = {
            "ts":    datetime.now(timezone.utc).isoformat(),
            "event": event,
        }
        if setup:
            record["ticker"] = setup.ticker
            record["series"] = setup.series
            record["season"] = setup.season
            record["state"]  = setup.state
        record.update(kwargs)

        line = json.dumps(record)
        logger.debug(line)

        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(line + "\n")
