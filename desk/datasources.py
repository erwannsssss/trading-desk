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
    "T10Y2Y": "Yield curve, 10-year minus 2-year (negative = inverted)",
    "BAMLH0A0HYM2": "Junk bond spread over Treasuries (credit stress gauge, %)",
    "T10YIE": "10-year inflation expectations (%)",
    "DTWEXBGS": "Dollar index (broad, trade-weighted)",
    "NFCI": "Financial conditions index (negative = loose, positive = tight)",
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


# ---- Summaries the analysts read ---------------------------------------------
KEY_METRICS = {
    "marketCapitalization": "market_cap_millions", "peTTM": "pe_ratio", "psTTM": "price_to_sales",
    "pbAnnual": "price_to_book", "epsGrowthTTMYoy": "eps_growth_yoy_pct", "revenueGrowthTTMYoy": "revenue_growth_yoy_pct",
    "revenueGrowthQuarterlyYoy": "revenue_growth_last_quarter_pct", "grossMarginTTM": "gross_margin_pct",
    "netProfitMarginTTM": "net_margin_pct", "roeTTM": "return_on_equity_pct",
    "totalDebt/totalEquityQuarterly": "debt_to_equity", "beta": "beta", "dividendYieldIndicatedAnnual": "dividend_yield_pct",
    "52WeekHigh": "high_52w", "52WeekLow": "low_52w", "10DayAverageTradingVolume": "avg_volume_10d_millions",
}


def key_metrics(symbol: str) -> dict:
    m = basic_financials(symbol)
    return {label: round(m[k], 3) for k, label in KEY_METRICS.items() if isinstance(m.get(k), (int, float))}


def analyst_ratings(symbol: str) -> dict | None:
    rows = recommendation_trends(symbol)
    if not rows:
        return None
    r = rows[0]
    return {"period": r["period"], "strong_buy": r["strongBuy"], "buy": r["buy"], "hold": r["hold"],
            "sell": r["sell"], "strong_sell": r["strongSell"],
            "previous_month_buys": (rows[1]["strongBuy"] + rows[1]["buy"]) if len(rows) > 1 else None}


def eps_surprises(symbol: str) -> list[dict]:
    rows = _fh("/stock/earnings", symbol=symbol, limit=4)
    return [{"quarter": r.get("period"), "actual_eps": r.get("actual"), "expected_eps": r.get("estimate"),
             "surprise_pct": round(r["surprisePercent"], 1) if r.get("surprisePercent") is not None else None}
            for r in rows]


def insider_summary(symbol: str, days: int = 90) -> dict:
    """Open-market buys and sells by executives/directors (Form 4), ignoring option exercises and grants."""
    cutoff = str(date.today() - timedelta(days=days))
    buys = sells = buy_usd = sell_usd = 0
    buyers = set()
    for t in insider_transactions(symbol):
        if (t.get("transactionDate") or "") < cutoff:
            continue
        code, shares, px = t.get("transactionCode"), abs(t.get("change") or 0), t.get("transactionPrice") or 0
        if code == "P":
            buys += 1
            buy_usd += shares * px
            buyers.add(t.get("name"))
        elif code == "S":
            sells += 1
            sell_usd += shares * px
    return {"window_days": days, "open_market_buys": buys, "buy_value_usd": round(buy_usd),
            "open_market_sells": sells, "sell_value_usd": round(sell_usd), "buyers": sorted(b for b in buyers if b)[:5]}


def reddit_trending(top: int = 30) -> dict:
    """Most-mentioned tickers on Reddit stock communities in the last 24h (ApeWisdom, free)."""
    d = request("ApeWisdom", "GET", "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1")
    out = {}
    for r in d.get("results", [])[:top]:
        prev = r.get("mentions_24h_ago") or 0
        out[r["ticker"]] = {"rank": r["rank"], "mentions_24h": r["mentions"], "mentions_prior_24h": prev,
                            "rank_24h_ago": r.get("rank_24h_ago"), "upvotes": r.get("upvotes")}
    return out


WATCHED_RELEASES = {
    10: "CPI inflation", 46: "PPI producer inflation", 50: "Jobs report", 53: "GDP", 54: "PCE inflation / spending",
    9: "Retail sales", 180: "Weekly jobless claims", 192: "JOLTS job openings", 95: "Durable goods",
    13: "Industrial production", 91: "Consumer sentiment (UMich)",
}


def economic_calendar(days_ahead: int = 10) -> list[dict]:
    today = date.today()
    d = request("FRED", "GET", f"{FRED}/releases/dates", params={
        "api_key": config.fred_key(), "file_type": "json", "realtime_start": str(today),
        "realtime_end": str(today + timedelta(days=days_ahead)), "include_release_dates_with_no_data": "true",
        "limit": 1000, "sort_order": "asc"})
    events = [{"date": r["date"], "event": WATCHED_RELEASES[r["release_id"]]}
              for r in d.get("release_dates", []) if r["release_id"] in WATCHED_RELEASES]
    for f in fomc_meetings():
        if str(today) <= f["decision_date"] <= str(today + timedelta(days=days_ahead)):
            events.append({"date": f["decision_date"], "event": "Fed rate decision (FOMC)"})
    return sorted(events, key=lambda e: e["date"])


def fomc_meetings() -> list[dict]:
    import re
    page = request("Fed", "GET", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")["raw"]
    months = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                           "September", "October", "November", "December"], 1)}
    out = []
    parts = re.split(r">(\d{4}) FOMC Meetings<", page)
    for year, body in zip(parts[1::2], parts[2::2]):
        for month, days in re.findall(r'fomc-meeting__month[^>]*><strong>(\w+)(?:/\w+)?</strong>.*?fomc-meeting__date[^>]*>([^<]+)<', body, re.S):
            m = re.match(r"(\d+)(?:-(\d+))?", days.strip())
            if month in months and m and "notation" not in days:
                day = int(m.group(2) or m.group(1))
                out.append({"decision_date": f"{year}-{months[month]:02d}-{day:02d}", "press_conference": "*" in days})
    return out
