"""
Kalshi WebSocket client for real-time orderbook, trades, and fills.

Channels: orderbook_delta, trade, ticker, order_fill

Auth: Bearer token (api_key) or RSA-PSS (key_id + private_key_path).
RSA auth uses the same credentials as KalshiClient — no separate token needed.
"""

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from websockets.exceptions import ConnectionClosed

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
_WS_PATH = "/trade-api/ws/v2"

logger = logging.getLogger(__name__)


class KalshiWebSocket:
    """
    Async WebSocket client. Yields parsed messages via async iteration.

    Auth options (pass one):
      - api_key: Bearer token
      - key_id + private_key_path: RSA-PSS (same credentials as KalshiClient)

    Usage:
        async with KalshiWebSocket(key_id=..., private_key_path=...) as ws:
            await ws.subscribe(["trade", "order_fill"], ["TICKER-1"])
            async for msg in ws:
                handle(msg)
    """

    def __init__(
        self,
        api_key: str | None = None,
        key_id: str | None = None,
        private_key_path: str | Path | None = None,
        url: str = WS_URL,
        on_reconnect: Callable | None = None,
    ) -> None:
        if not api_key and not (key_id and private_key_path):
            raise ValueError("Provide either api_key (Bearer) or key_id + private_key_path (RSA)")
        self.api_key = api_key
        self.key_id = key_id
        self._private_key = (
            self._load_key(Path(private_key_path).expanduser())
            if private_key_path else None
        )
        self.url = url
        self.on_reconnect = on_reconnect
        self._ws: Any = None
        self._msg_id = 0
        self._subscriptions: list[dict] = []

    @staticmethod
    def _load_key(path: Path):
        return serialization.load_pem_private_key(path.read_bytes(), password=None)

    def _auth_headers(self) -> dict[str, str]:
        """Return auth headers for the WebSocket upgrade request."""
        if self.key_id and self._private_key:
            ts_ms = str(int(time.time() * 1000))
            message = (ts_ms + "GET" + _WS_PATH).encode("utf-8")
            sig = self._private_key.sign(
                message,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                hashes.SHA256(),
            )
            return {
                "KALSHI-ACCESS-KEY":       self.key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts_ms,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            }
        return {"Authorization": f"Bearer {self.api_key}"}

    async def __aenter__(self) -> "KalshiWebSocket":
        await self._connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._ws:
            await self._ws.close()

    async def _connect(self) -> None:
        self._ws = await websockets.connect(
            self.url,
            additional_headers=self._auth_headers(),
        )

    async def subscribe(self, channels: list[str], tickers: list[str]) -> None:
        self._msg_id += 1
        msg = {
            "id": self._msg_id,
            "cmd": "subscribe",
            "params": {"channels": channels, "market_tickers": tickers},
        }
        self._subscriptions.append(msg)
        await self._ws.send(json.dumps(msg))

    async def unsubscribe(self, channels: list[str], tickers: list[str]) -> None:
        self._msg_id += 1
        msg = {
            "id": self._msg_id,
            "cmd": "unsubscribe",
            "params": {"channels": channels, "market_tickers": tickers},
        }
        await self._ws.send(json.dumps(msg))

    def __aiter__(self) -> "KalshiWebSocket":
        return self

    async def __anext__(self) -> dict:
        while True:
            try:
                raw = await self._ws.recv()
                return json.loads(raw)
            except ConnectionClosed:
                logger.warning("WebSocket disconnected, reconnecting...")
                await self._connect()
                for sub in self._subscriptions:
                    await self._ws.send(json.dumps(sub))
                if self.on_reconnect:
                    self.on_reconnect()
