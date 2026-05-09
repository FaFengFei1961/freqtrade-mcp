"""Typed HTTP client for the Freqtrade REST API.

Wraps the endpoints Yangyang actually uses:
  - read state (status, balance, whitelist, profit, locks, trades)
  - mutate state (force_enter, force_exit, blacklist, reload_config)

We deliberately do NOT shell out to the Freqtrade CLI — the bot runs in its
own Docker container and is reached only via HTTP.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from .config import Settings, get_settings

JsonObject = dict[str, Any]
JsonArray = list[JsonObject]
TradeSide = Literal["long", "short"]


class FreqtradeAPIError(RuntimeError):
    """Raised when the Freqtrade API returns a non-2xx response."""

    def __init__(self, status_code: int, body: str, *, endpoint: str) -> None:
        super().__init__(f"[{endpoint}] HTTP {status_code}: {body[:300]}")
        self.status_code = status_code
        self.body = body
        self.endpoint = endpoint


class FreqtradeClient:
    """Thin synchronous wrapper around Freqtrade's REST API.

    Use as a context manager so the underlying connection pool is closed
    cleanly:

        with FreqtradeClient() as ft:
            print(ft.ping())
    """

    def __init__(self, settings: Settings | None = None, *, timeout: float = 10.0) -> None:
        self._settings = settings or get_settings()
        self._client = httpx.Client(
            base_url=str(self._settings.freqtrade_url),
            auth=(
                self._settings.freqtrade_username,
                self._settings.freqtrade_password.get_secret_value(),
            ),
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    # --- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "FreqtradeClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # --- low-level ---------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        json: JsonObject | None = None,
        params: JsonObject | None = None,
    ) -> Any:
        response = self._client.request(method, f"/api/v1{path}", json=json, params=params)
        if response.status_code >= 400:
            raise FreqtradeAPIError(response.status_code, response.text, endpoint=path)
        if not response.content:
            return None
        return response.json()

    # --- read endpoints ----------------------------------------------------
    def ping(self) -> JsonObject:
        return self._request("GET", "/ping")

    def version(self) -> JsonObject:
        return self._request("GET", "/version")

    def show_config(self) -> JsonObject:
        return self._request("GET", "/show_config")

    def status(self) -> JsonArray:
        """Return open trades (empty list if none)."""
        return self._request("GET", "/status")

    def balance(self) -> JsonObject:
        return self._request("GET", "/balance")

    def profit(self) -> JsonObject:
        return self._request("GET", "/profit")

    def whitelist(self) -> JsonObject:
        return self._request("GET", "/whitelist")

    def blacklist(self) -> JsonObject:
        return self._request("GET", "/blacklist")

    def locks(self) -> JsonObject:
        return self._request("GET", "/locks")

    def trades(self, *, limit: int = 50) -> JsonObject:
        return self._request("GET", "/trades", params={"limit": limit})

    def pair_candles(
        self,
        pair: str,
        *,
        timeframe: str = "5m",
        limit: int = 200,
    ) -> JsonObject:
        """OHLCV with strategy-computed indicators for a pair.

        Returns the last `limit` candles of `pair` at `timeframe`. The
        response includes columns from MetaStrategy.populate_indicators
        (ema_fast, ema_slow, rsi, macd, atr, bollinger bands, etc.).
        """
        return self._request(
            "GET",
            "/pair_candles",
            params={"pair": pair, "timeframe": timeframe, "limit": limit},
        )

    def pair_history(
        self,
        pair: str,
        *,
        timeframe: str = "5m",
        timerange: str | None = None,
    ) -> JsonObject:
        """Historical OHLCV with indicators over a timerange (e.g. '20260101-20260108')."""
        params: JsonObject = {"pair": pair, "timeframe": timeframe}
        if timerange is not None:
            params["timerange"] = timerange
        return self._request("GET", "/pair_history", params=params)

    def available_pairs(self, *, timeframe: str | None = None) -> JsonObject:
        """List pairs that currently have downloaded data available."""
        params: JsonObject = {}
        if timeframe is not None:
            params["timeframe"] = timeframe
        return self._request("GET", "/available_pairs", params=params)

    # --- mutate endpoints --------------------------------------------------
    def force_enter(
        self,
        pair: str,
        *,
        side: TradeSide = "long",
        price: float | None = None,
        order_type: Literal["limit", "market"] = "market",
        stake_amount: float | None = None,
        leverage: float | None = None,
        enter_tag: str | None = None,
    ) -> JsonObject:
        """Open a position (Freqtrade `POST /forceenter`).

        Notes:
            - `stake_amount` is in stake currency (USDT).
            - `leverage` only matters in futures mode.
            - When `force_entry_enable` is false in config.json, this 404s.
        """
        payload: JsonObject = {
            "pair": pair,
            "side": side,
            "ordertype": order_type,
        }
        if price is not None:
            payload["price"] = price
        if stake_amount is not None:
            payload["stakeamount"] = stake_amount
        if leverage is not None:
            payload["leverage"] = leverage
        if enter_tag is not None:
            payload["entry_tag"] = enter_tag
        return self._request("POST", "/forceenter", json=payload)

    def force_exit(
        self,
        trade_id: int | str,
        *,
        order_type: Literal["limit", "market"] | None = None,
        amount: float | None = None,
    ) -> JsonObject:
        """Close a position by trade id, or pass `'all'` to flatten."""
        payload: JsonObject = {"tradeid": str(trade_id)}
        if order_type is not None:
            payload["ordertype"] = order_type
        if amount is not None:
            payload["amount"] = amount
        return self._request("POST", "/forceexit", json=payload)

    def add_blacklist(self, pairs: list[str]) -> JsonObject:
        return self._request("POST", "/blacklist", json={"blacklist": pairs})

    def remove_blacklist(self, pairs: list[str]) -> JsonObject:
        return self._request("DELETE", "/blacklist", params={"pairs_to_delete": pairs})

    def reload_config(self) -> JsonObject:
        return self._request("POST", "/reload_config")

    def cancel_open_order(self, trade_id: int | str) -> JsonObject:
        """Cancel a pending entry/exit order on a trade (the trade itself stays)."""
        return self._request("DELETE", f"/trades/{trade_id}/open-order")

    def reload_trade(self, trade_id: int | str) -> JsonObject:
        """Force Freqtrade to reload a trade from the exchange."""
        return self._request("POST", f"/trades/{trade_id}/reload")

    def stop(self) -> JsonObject:
        return self._request("POST", "/stop")

    def start(self) -> JsonObject:
        return self._request("POST", "/start")

    def stopentry(self) -> JsonObject:
        return self._request("POST", "/stopentry")
