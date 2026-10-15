"""Options: picks a liquid call or put for an idea and values it. Buying options only (the most you can lose is
the premium paid). Spreads and selling options aren't automated yet."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from desk import config, marketdata

DTE_BY_HORIZON = {"intraday": (7, 21), "1-5 days": (10, 28), "1-4 weeks": (21, 50)}
MAX_SPREAD_PCT = 15.0   # skip contracts where bid/ask is wider than this % of the mid price
MIN_PREMIUM = 0.30      # skip contracts cheaper than $0.30 (lottery tickets with awful spreads)
OCC = re.compile(r"^([A-Z.]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def parse(symbol: str) -> dict:
    m = OCC.match(symbol)
    root, yy, mm, dd, cp, strike = m.groups()
    return {"underlying": root, "expiry": date(2000 + int(yy), int(mm), int(dd)),
            "type": "call" if cp == "C" else "put", "strike": int(strike) / 1000}


def _chain(underlying: str, kind: str, dte: tuple[int, int], price: float) -> dict:
    today = date.today()
    d = marketdata._alpaca(f"/v1beta1/options/snapshots/{underlying}", {
        "feed": "indicative", "type": kind, "limit": 1000,
        "expiration_date_gte": str(today + timedelta(days=dte[0])), "expiration_date_lte": str(today + timedelta(days=dte[1])),
        "strike_price_gte": round(price * 0.85, 2), "strike_price_lte": round(price * 1.15, 2)})
    return d.get("snapshots") or {}


def pick_contract(underlying: str, kind: str, price: float, horizon: str | None, max_cost: float | None = None) -> dict | None:
    """The best call/put to express a view: delta near 0.5 (or the at-the-money strike), tight spread,
    and one contract costing no more than `max_cost` dollars (falls back to further out-of-the-money strikes)."""
    dte = DTE_BY_HORIZON.get((horizon or "").strip(), (14, 35))
    best, best_score = None, None
    for sym, s in _chain(underlying, kind, dte, price).items():
        q = s.get("latestQuote") or {}
        bid, ask = q.get("bp") or 0, q.get("ap") or 0
        if bid <= 0 or ask <= 0:
            continue
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        if mid < MIN_PREMIUM or spread_pct > MAX_SPREAD_PCT:
            continue
        if max_cost and mid * 100 > max_cost:
            continue
        info = parse(sym)
        delta = abs((s.get("greeks") or {}).get("delta") or 0)
        if delta and delta < 0.25:
            continue  # too far out of the money
        moneyness = abs(info["strike"] / price - 1)
        # prefer delta ~0.5; without greeks prefer the strike closest to the money
        score = (abs(delta - 0.5) if delta else moneyness * 4) + spread_pct / 100
        if best_score is None or score < best_score:
            best_score = score
            best = {"symbol": sym, **info, "expiry": info["expiry"].isoformat(), "bid": bid, "ask": ask,
                    "mid": round(mid, 2), "spread_pct": round(spread_pct, 1), "delta": delta or None,
                    "iv": s.get("impliedVolatility")}
    return best


def quote(symbol: str) -> dict | None:
    root = parse(symbol)["underlying"]
    d = marketdata._alpaca(f"/v1beta1/options/snapshots", {"symbols": symbol, "feed": "indicative"})
    s = (d.get("snapshots") or {}).get(symbol)
    if not s:
        return None
    q = s.get("latestQuote") or {}
    bid, ask = q.get("bp") or 0, q.get("ap") or 0
    return {"bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 2) if bid and ask else (s.get("latestTrade") or {}).get("p"),
            "underlying": root}


def contracts_for(budget_usd: float, premium: float) -> int:
    return int(budget_usd // (premium * 100)) if premium > 0 else 0


def entry_limit(c: dict) -> float:
    """Pay a little above the middle of the spread so the order actually fills."""
    return round(c["mid"] + (c["ask"] - c["mid"]) * 0.3, 2)


def days_to_expiry(symbol: str, today: date | None = None) -> int:
    return (parse(symbol)["expiry"] - (today or date.today())).days
