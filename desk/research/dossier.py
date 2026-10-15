"""Gathers every piece of data on one finalist into a dossier the analysts read."""
from __future__ import annotations

from desk import datasources, marketdata
from desk.research import extra, smartmoney


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as exc:
        return {"unavailable": str(exc)[:120]}


def build(cand: dict, fund_holdings: dict, light: bool = False) -> dict:
    """`light` skips the slow SEC press-release fetch (used by the fast sessions)."""
    sym, asset = cand["symbol"], cand["asset"]
    d = {
        "symbol": sym,
        "asset_type": asset,
        "why_it_was_scanned": cand.get("why_scanned") or [],
        "technicals": {k: v for k, v in cand["technicals"].items() if k != "interest"},
    }
    if asset in ("stock", "etf"):
        d["news"] = _safe(marketdata.news, [sym], 8, 3)
        if cand.get("reddit"):
            d["reddit"] = cand["reddit"]
        if asset == "stock":
            d["fundamentals"] = _safe(datasources.key_metrics, sym)
            d["analyst_ratings"] = _safe(datasources.analyst_ratings, sym)
            d["earnings_history"] = _safe(datasources.eps_surprises, sym)
            d["insiders_last_90d"] = _safe(datasources.insider_summary, sym)
            d["hedge_funds_holding"] = fund_holdings.get(sym, [])
            if cand.get("earnings"):
                e = cand["earnings"]
                d["upcoming_earnings"] = {"date": e.get("date"), "timing": e.get("hour"),
                                          "expected_eps": e.get("epsEstimate"), "expected_revenue": e.get("revenueEstimate")}
            rel = None if light else _safe(smartmoney.latest_earnings_release, sym)
            if rel and "text" in rel:
                d["latest_earnings_press_release"] = {"filed": rel["filed"], "excerpt": rel["text"][:3500]}
    elif asset == "crypto":
        d["news"] = _safe(marketdata.news, [sym], 8, 3)
    elif asset == "forex":
        d["news"] = _safe(lambda: [n for n in datasources.market_news("forex")][:8])
        if isinstance(d["news"], list):
            d["news"] = [{"headline": n.get("headline"), "summary": (n.get("summary") or "")[:250]} for n in d["news"]]
    _add_extra(d, sym, asset, light)
    return d


def _add_extra(d: dict, sym: str, asset: str, light: bool) -> None:
    """Free extra data (desk/research/extra.py). Each piece is skipped quietly if its source is down."""
    def put(key, fn, *a):
        try:
            v = fn(*a)
            if v:
                d[key] = v
        except Exception as exc:
            print(f"extra data {key} for {sym} failed: {str(exc)[:100]}")
    if asset in ("stock", "etf"):
        put("sector", extra.sector, sym)
        put("short_interest_and_analysts", extra.yahoo_stats, sym, d["technicals"].get("price"))
        put("short_volume", extra.short_volume, sym)
        if asset == "stock" and not light:
            put("sec_filings", extra.sec_snapshot, sym)
    elif asset == "forex":
        put("futures_positioning", extra.cot_for_pair, sym)
    elif asset == "crypto":
        put("crypto_sentiment", extra.crypto_sentiment)
        coin = {"BTC/USD": "BITCOIN", "ETH/USD": "ETHER"}.get(sym)
        if coin:
            put("futures_positioning", lambda: {coin: extra.cot().get(coin)} if extra.cot().get(coin) else None)


def facts(dossier: dict) -> dict:
    """Flat {"section.field": number} map of every number in the dossier, for fact-checking."""
    out = {}
    for section in ("technicals", "fundamentals", "analyst_ratings", "insiders_last_90d", "reddit",
                    "short_interest_and_analysts", "short_volume", "sec_filings", "crypto_sentiment", "futures_positioning"):
        block = dossier.get(section)
        if isinstance(block, dict):
            for k, v in block.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[f"{section}.{k}"] = v
                elif isinstance(v, dict):   # one level down, e.g. futures_positioning.EUR.three_year_percentile
                    for k2, v2 in v.items():
                        if isinstance(v2, (int, float)) and not isinstance(v2, bool):
                            out[f"{section}.{k}.{k2}"] = v2
    hist = dossier.get("earnings_history")
    if isinstance(hist, list) and hist:
        for k, v in hist[0].items():
            if isinstance(v, (int, float)):
                out[f"earnings_history.latest.{k}"] = v
    return out
