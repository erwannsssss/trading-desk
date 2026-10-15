"""OANDA practice account: forex."""
from __future__ import annotations

from functools import lru_cache

from desk import config
from desk.http import request


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.oanda_token()}", "Content-Type": "application/json"}


def _call(method: str, path: str, **kw):
    url = f"{config.OANDA_URL}/accounts/{config.oanda_account()}{path}"
    return request("OANDA", method, url, headers=_headers(), **kw)


# ---- Account -----------------------------------------------------------------
def summary() -> dict:
    return _call("GET", "/summary")["account"]


def nav() -> float:
    return float(summary()["NAV"])


def open_trades() -> list[dict]:
    return _call("GET", "/openTrades").get("trades", [])


def open_positions() -> list[dict]:
    return _call("GET", "/openPositions").get("positions", [])


def pending_orders() -> list[dict]:
    return _call("GET", "/pendingOrders").get("orders", [])


@lru_cache(maxsize=1)
def instruments() -> dict:
    data = _call("GET", "/instruments").get("instruments", [])
    return {i["name"]: i for i in data}


def precision(instrument: str) -> int:
    return int(instruments().get(instrument, {}).get("displayPrecision", 5))


# ---- Prices ------------------------------------------------------------------
def price(instrument: str) -> dict:
    d = _call("GET", "/pricing", params={"instruments": instrument})["prices"][0]
    bid, ask = float(d["bids"][0]["price"]), float(d["asks"][0]["price"])
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2, "tradeable": d.get("tradeable", True)}


def candles(instrument: str, granularity: str = "H1", count: int = 100) -> list[dict]:
    url = f"{config.OANDA_URL}/instruments/{instrument}/candles"
    d = request("OANDA", "GET", url, headers=_headers(), params={"granularity": granularity, "count": count, "price": "M"})
    return d.get("candles", [])


def usd_value_per_unit(instrument: str) -> float:
    """Roughly how many US dollars one unit of this pair is worth (for sizing)."""
    base, quote = instrument.split("_")
    if base == "USD":
        return 1.0
    if quote == "USD":
        return price(instrument)["mid"]
    try:
        return price(f"{base}_USD")["mid"]
    except Exception:
        return 1.0 / price(f"USD_{base}")["mid"]


# ---- Orders ------------------------------------------------------------------
def submit_order(
    instrument: str,
    units: int,                   # positive = buy (long), negative = sell (short)
    order_type: str = "LIMIT",    # "MARKET" or "LIMIT"
    limit_price: float | None = None,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    client_tag: str | None = None,
) -> dict:
    p = precision(instrument)
    order: dict = {"type": order_type, "instrument": instrument, "units": str(int(units)), "positionFill": "DEFAULT"}
    if order_type == "LIMIT":
        order["price"] = f"{limit_price:.{p}f}"
        order["timeInForce"] = "GTC"
    else:
        order["timeInForce"] = "FOK"
    if take_profit:
        order["takeProfitOnFill"] = {"price": f"{take_profit:.{p}f}"}
    if stop_loss:
        order["stopLossOnFill"] = {"price": f"{stop_loss:.{p}f}"}
    if client_tag:
        order["clientExtensions"] = {"id": client_tag[:100], "tag": "trading-desk"}
    return _call("POST", "/orders", json={"order": order})


def cancel_order(order_id: str) -> dict:
    return _call("PUT", f"/orders/{order_id}/cancel")


def close_position(instrument: str) -> dict:
    pos = next((x for x in open_positions() if x["instrument"] == instrument), None)
    if not pos:
        return {}
    body = {}
    if float(pos["long"]["units"]) != 0:
        body["longUnits"] = "ALL"
    if float(pos["short"]["units"]) != 0:
        body["shortUnits"] = "ALL"
    return _call("PUT", f"/positions/{instrument}/close", json=body)


def close_all() -> None:
    for o in pending_orders():
        cancel_order(o["id"])
    for p in open_positions():
        close_position(p["instrument"])
