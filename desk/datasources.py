"""Free market data: Finnhub (news, earnings, insiders, quotes) and FRED (economy)."""
from __future__ import annotations

from datetime import date, timedelta

from desk import config
from desk.http import request

FINNHUB = "https://finnhub.io/api/v1"
FRED = "https://api.stlouisfed.org/fred"


def _fh(path: str, **params):
    params["token"] = config.finnhub_key()
    return request("Finnhub", "GET", f"{FINNHUB}{path}", params=params)


# ---- Finnhub -----------------------------------------------------------------
def quote(symbol: str) -> dict:
    return _fh("/quote", symbol=symbol)


def company_news(symbol: str, days: int = 3) -> list[dict]:
    today = date.today()
    return _fh("/company-news", symbol=symbol, **{"from": str(today - timedelta(days=days)), "to": str(today)})


def market_news(category: str = "general") -> list[dict]:
    return _fh("/news", category=category)


def earnings_calendar(days_ahead: int = 7) -> list[dict]:
    today = date.today()
    d = _fh("/calendar/earnings", **{"from": str(today), "to": str(today + timedelta(days=days_ahead))})
    return d.get("earningsCalendar", [])


def insider_transactions(symbol: str) -> list[dict]:
    return _fh("/stock/insider-transactions", symbol=symbol).get("data", [])


def recommendation_trends(symbol: str) -> list[dict]:
    return _fh("/stock/recommendation", symbol=symbol)


def basic_financials(symbol: str) -> dict:
    return _fh("/stock/metric", symbol=symbol, metric="all").get("metric", {})


# ---- FRED (economy) ----------------------------------------------------------
MACRO_SERIES = {
    "FEDFUNDS": "Fed funds rate",
    "DGS10": "10-year Treasury yield",
    "DGS2": "2-year Treasury yield",
    "CPIAUCSL": "CPI (inflation index)",
    "UNRATE": "Unemployment rate",
    "VIXCLS": "VIX (fear index)",
}


def fred_latest(series_id: str) -> dict:
    d = request("FRED", "GET", f"{FRED}/series/observations", params={
        "series_id": series_id, "api_key": config.fred_key(), "file_type": "json",
        "sort_order": "desc", "limit": 5,
    })
    obs = [o for o in d.get("observations", []) if o.get("value") not in (".", None)]
    return obs[0] if obs else {}


def macro_snapshot() -> dict:
    out = {}
    for sid, label in MACRO_SERIES.items():
        try:
            o = fred_latest(sid)
            out[label] = {"value": o.get("value"), "date": o.get("date")}
        except Exception as exc:  # one missing series shouldn't stop the run
            out[label] = {"error": str(exc)[:100]}
    return out
