"""
Async Kalshi REST API client.

Authentication: RSA-PSS signing.
  Message  = str(timestamp_ms) + METHOD + path  (no query string)
  Algorithm: RSA-PSS, SHA-256, MGF1(SHA-256), salt_length=32
  Headers:
    KALSHI-ACCESS-KEY        — key ID from dashboard
    KALSHI-ACCESS-TIMESTAMP  — milliseconds since epoch (str)
    KALSHI-ACCESS-SIGNATURE  — base64url-encoded signature

All prices are integers 0-99.
"""

import base64
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiAPIError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


class KalshiClient:
    """
    Async Kalshi REST client with RSA-PSS request signing.

    Parameters
    ----------
    key_id : str
        The API key ID from the Kalshi dashboard.
    private_key_path : str | Path
        Path to the PEM-encoded RSA private key file.
    base_url : str
        API base URL.
    """

    def __init__(
        self,
        key_id: str,
        private_key_path: str | Path,
        base_url: str = BASE_URL,
    ) -> None:
        self.key_id = key_id
        self.base_url = base_url
        self._private_key = self._load_key(Path(private_key_path).expanduser())
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _load_key(path: Path):
        pem = path.read_bytes()
        return serialization.load_pem_private_key(pem, password=None)

    def _sign(self, method: str, path: str) -> dict[str, str]:
        """Return per-request auth headers."""
        ts_ms = str(int(time.time() * 1000))
        # Kalshi signs the full path including the /trade-api/v2 prefix, no query string
        clean_path = urlparse("/trade-api/v2" + path).path
        message = (ts_ms + method.upper() + clean_path).encode("utf-8")
        sig = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=32,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    async def __aenter__(self) -> "KalshiClient":
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        assert self._client is not None, "Use async context manager"
        resp = await self._client.get(path, params=params, headers=self._sign("GET", path))
        if resp.status_code != 200:
            raise KalshiAPIError(resp.status_code, resp.text)
        return resp.json()

    async def _post(self, path: str, body: dict) -> dict:
        assert self._client is not None, "Use async context manager"
        resp = await self._client.post(path, json=body, headers=self._sign("POST", path))
        if resp.status_code not in (200, 201):
            raise KalshiAPIError(resp.status_code, resp.text)
        return resp.json()

    async def _delete(self, path: str) -> dict:
        assert self._client is not None, "Use async context manager"
        resp = await self._client.delete(path, headers=self._sign("DELETE", path))
        if resp.status_code not in (200, 204):
            raise KalshiAPIError(resp.status_code, resp.text)
        return resp.json() if resp.content else {}

    # --- Markets ---

    async def get_markets(
        self,
        status: str | None = None,
        series_ticker: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        return await self._get("/markets", params=params)

    async def get_market(self, ticker: str) -> dict:
        return await self._get(f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str, depth: int | None = None) -> dict:
        params = {"depth": depth} if depth else None
        return await self._get(f"/markets/{ticker}/orderbook", params=params)

    async def get_trades(
        self,
        ticker: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict:
        params: dict[str, Any] = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/markets/trades", params=params)

    # --- Orders ---

    async def create_order(
        self,
        ticker: str,
        side: str,          # "yes" or "no"
        action: str,        # "buy" or "sell"
        count: int,
        price: int,         # 1-99
        order_type: str = "limit",
        client_order_id: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "price": price,
            "type": order_type,
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        return await self._post("/portfolio/orders", body)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._delete(f"/portfolio/orders/{order_id}")

    async def get_orders(
        self,
        ticker: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return await self._get("/portfolio/orders", params=params)

    # --- Portfolio ---

    async def get_positions(self) -> dict:
        return await self._get("/portfolio/positions")

    async def get_balance(self) -> dict:
        """Returns balance in cents (divide by 100 for dollars)."""
        return await self._get("/portfolio/balance")
