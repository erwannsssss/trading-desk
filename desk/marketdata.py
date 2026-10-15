"""Price data for everything the desk trades, fetched in bulk."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from desk import config
from desk.brokers import alpaca, oanda
from desk.http import request


def _alpaca(path: str, params: dict):
    return request("Alpaca data", "GET", f"{config.ALPACA_DATA_URL}{path}", headers=alpaca._headers(), params=params)


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _norm(b: dict) -> dict:
    return {"t": b["t"], "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"]), "v": float(b.get("v", 0))}


def stock_daily_bars(symbols: list[str], days: int = 400) -> dict[str, list[dict]]:
    """Daily bars with full consolidated volume (SIP). Free plan allows SIP data older than 15 minutes."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    end = (datetime.now(timezone.utc) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: dict[str, list[dict]] = {}
    for chunk in _chunks(sorted(set(symbols)), 50):
        token = None
        while True:
            params = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": start, "end": end,
                      "feed": "sip", "adjustment": "all", "limit": 10000}
            if token:
                params["page_token"] = token
            d = _alpaca("/v2/stocks/bars", params)
            for sym, bars in (d.get("bars") or {}).items():
                out.setdefault(sym, []).extend(_norm(b) for b in bars)
            token = d.get("next_page_token")
            if not token:
                break
    return out


def stock_snapshots(symbols: list[str]) -> dict[str, dict]:
    """Latest trade price and time (IEX feed, includes pre-market trading)."""
    out = {}
    for chunk in _chunks(sorted(set(symbols)), 100):
        d = _alpaca("/v2/stocks/snapshots", {"symbols": ",".join(chunk), "feed": "iex"})
        for sym, s in d.items():
            if not s:
                continue
            trade = s.get("latestTrade") or {}
            out[sym] = {"price": trade.get("p"), "trade_time": trade.get("t")}
    return out


def crypto_daily_bars(symbols: list[str], days: int = 400) -> dict[str, list[dict]]:
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    out: dict[str, list[dict]] = {}
    token = None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "limit": 10000}
        if token:
            params["page_token"] = token
        d = _alpaca("/v1beta3/crypto/us/bars", params)
        for sym, bars in (d.get("bars") or {}).items():
            out.setdefault(sym, []).extend(_norm(b) for b in bars)
        token = d.get("next_page_token")
        if not token:
            break
    return out


def crypto_prices(symbols: list[str]) -> dict[str, float]:
    d = _alpaca("/v1beta3/crypto/us/latest/trades", {"symbols": ",".join(symbols)})
    return {s: float(t["p"]) for s, t in (d.get("trades") or {}).items()}


def forex_daily_bars(instrument: str, count: int = 300) -> list[dict]:
    rows = []  # includes the current (still forming) daily candle as the last bar
    for c in oanda.candles(instrument, "D", count):
        m = c["mid"]
        rows.append({"t": c["time"], "o": float(m["o"]), "h": float(m["h"]), "l": float(m["l"]),
                     "c": float(m["c"]), "v": float(c.get("volume", 0))})
    return rows


def news(symbols: list[str] | None = None, limit: int = 10, days: int = 3) -> list[dict]:
    """Recent headlines (Benzinga via Alpaca). Crypto symbols use the form BTCUSD."""
    params = {"limit": limit, "sort": "desc",
              "start": (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if symbols:
        params["symbols"] = ",".join(s.replace("/", "") for s in symbols)
    d = _alpaca("/v1beta1/news", params)
    return [{"time": n["created_at"][:16], "headline": n["headline"], "summary": (n.get("summary") or "")[:300],
             "symbols": n.get("symbols", [])} for n in d.get("news", [])]


def most_actives(top: int = 40) -> list[dict]:
    return _alpaca("/v1beta1/screener/stocks/most-actives", {"by": "volume", "top": top}).get("most_actives", [])


def movers(top: int = 30) -> dict:
    d = _alpaca("/v1beta1/screener/stocks/movers", {"top": top})
    return {"gainers": d.get("gainers", []), "losers": d.get("losers", [])}


def tradable_stock(symbol: str) -> dict | None:
    try:
        a = request("Alpaca", "GET", f"{config.ALPACA_TRADING_URL}/assets/{symbol}", headers=alpaca._headers())
    except Exception:
        return None
    if a.get("status") != "active" or not a.get("tradable"):
        return None
    return a
