"""Alpaca paper account: US stocks, ETFs, options and crypto."""
from __future__ import annotations

import math

from desk import config
from desk.http import request


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID": config.alpaca_key(),
        "APCA-API-SECRET-KEY": config.alpaca_secret(),
    }


def _trade(method: str, path: str, **kw):
    return request("Alpaca", method, f"{config.ALPACA_TRADING_URL}{path}", headers=_headers(), **kw)


def _data(path: str, params: dict | None = None):
    return request("Alpaca data", "GET", f"{config.ALPACA_DATA_URL}{path}", headers=_headers(), params=params)


def is_crypto(symbol: str) -> bool:
    return "/" in symbol  # Alpaca crypto symbols look like "BTC/USD"


def is_option(symbol: str) -> bool:
    return len(symbol) > 15 and any(c.isdigit() for c in symbol)  # OCC format e.g. AAPL261120C00250000


# ---- Account -----------------------------------------------------------------
def account() -> dict:
    return _trade("GET", "/account")


def equity() -> float:
    return float(account()["equity"])


def clock() -> dict:
    return _trade("GET", "/clock")


def positions() -> list[dict]:
    return _trade("GET", "/positions")


def open_orders() -> list[dict]:
    return _trade("GET", "/orders", params={"status": "open", "limit": 500, "nested": "true"})


def todays_orders(after_iso: str) -> list[dict]:
    return _trade("GET", "/orders", params={"status": "all", "after": after_iso, "limit": 500})


# ---- Prices ------------------------------------------------------------------
def last_price(symbol: str) -> float:
    if is_crypto(symbol):
        d = _data("/v1beta3/crypto/us/latest/trades", {"symbols": symbol})
        return float(d["trades"][symbol]["p"])
    d = _data(f"/v2/stocks/{symbol}/trades/latest", {"feed": "iex"})
    return float(d["trade"]["p"])


def bars(symbol: str, timeframe: str = "1Day", limit: int = 100) -> list[dict]:
    if is_crypto(symbol):
        d = _data("/v1beta3/crypto/us/bars", {"symbols": symbol, "timeframe": timeframe, "limit": limit})
        return d.get("bars", {}).get(symbol, [])
    d = _data(f"/v2/stocks/{symbol}/bars", {"timeframe": timeframe, "limit": limit, "feed": "iex", "adjustment": "all"})
    return d.get("bars", [])


# ---- Orders ------------------------------------------------------------------
def submit_order(
    symbol: str,
    side: str,                    # "buy" or "sell" (sell to open = short)
    qty: float,
    order_type: str = "limit",    # "market" or "limit"
    limit_price: float | None = None,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    time_in_force: str | None = None,
    client_order_id: str | None = None,
) -> dict:
    """Place an order. Stocks/ETFs with a target and stop go in as a bracket order,
    so the broker itself takes profit or cuts the loss between the bot's runs."""
    body: dict = {"symbol": symbol, "side": side, "type": order_type}
    if client_order_id:
        body["client_order_id"] = client_order_id[:48]
    if order_type == "limit":
        body["limit_price"] = _px(limit_price)

    if is_crypto(symbol):
        body["qty"] = str(round(qty, 6))
        body["time_in_force"] = time_in_force or "gtc"
        # Alpaca crypto has no bracket orders; the hourly loop manages the exits.
    elif is_option(symbol):
        body["qty"] = str(int(qty))
        body["time_in_force"] = "day"
    else:
        body["time_in_force"] = time_in_force or "day"
        if take_profit and stop_loss:
            body["qty"] = str(int(math.floor(qty)))  # brackets need whole shares
            body["order_class"] = "bracket"
            body["take_profit"] = {"limit_price": _px(take_profit)}
            body["stop_loss"] = {"stop_price": _px(stop_loss)}
        else:
            body["qty"] = str(qty)
    return _trade("POST", "/orders", json=body)


def cancel_order(order_id: str) -> dict:
    return _trade("DELETE", f"/orders/{order_id}")


def cancel_all_orders() -> list:
    return _trade("DELETE", "/orders")


def close_position(symbol: str) -> dict:
    return _trade("DELETE", f"/positions/{symbol.replace('/', '')}")


def close_all_positions() -> list:
    return _trade("DELETE", "/positions", params={"cancel_orders": "true"})


def _px(price: float | None) -> str | None:
    if price is None:
        return None
    # Alpaca accepts 2 decimals for prices >= $1 and 4 below $1.
    return f"{price:.2f}" if price >= 1 else f"{price:.4f}"
