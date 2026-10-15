"""Options: picks a liquid call or put, or (preferred) a debit spread, for an idea and values it.

A debit spread buys one option near the money and sells a further-out one with the same expiry, at about the
trade's target. Selling the second option pays for part of the first, so the trade costs less and loses less to
time decay; in exchange, the profit is capped at the gap between the two strikes. The most it can lose is still
what was paid."""
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


def _quotes(chain: dict) -> list[dict]:
    rows = []
    for sym, s in chain.items():
        q = s.get("latestQuote") or {}
        bid, ask = q.get("bp") or 0, q.get("ap") or 0
        if bid <= 0 or ask <= 0:
            continue
        mid = (bid + ask) / 2
        rows.append({"symbol": sym, **parse(sym), "bid": bid, "ask": ask, "mid": mid, "spread_pct": (ask - bid) / mid * 100,
                     "delta": abs((s.get("greeks") or {}).get("delta") or 0) or None, "iv": s.get("impliedVolatility")})
    return rows


def pick_spread(underlying: str, kind: str, price: float, target: float, horizon: str | None,
                max_cost: float | None = None) -> dict | None:
    """A debit spread: buy the near-the-money option, sell the same-expiry option whose strike is closest to the
    target. Needs a real discount, a payoff at least equal to the cost, and both legs liquid."""
    dte = DTE_BY_HORIZON.get((horizon or "").strip(), (14, 35))
    rows = _quotes(_chain(underlying, kind, dte, price))
    longs = [r for r in rows if r["mid"] >= MIN_PREMIUM and r["spread_pct"] <= MAX_SPREAD_PCT and not (r["delta"] and r["delta"] < 0.3)]
    longs.sort(key=lambda r: (abs(r["delta"] - 0.5) if r["delta"] else abs(r["strike"] / price - 1) * 4) + r["spread_pct"] / 100)
    for lg in longs[:12]:
        shorts = [r for r in rows if r["expiry"] == lg["expiry"] and r["spread_pct"] <= MAX_SPREAD_PCT * 2
                  and ((kind == "call" and r["strike"] > lg["strike"]) or (kind == "put" and r["strike"] < lg["strike"]))]
        if not shorts:
            continue
        sh = min(shorts, key=lambda r: abs(r["strike"] - target))
        debit = lg["mid"] - sh["mid"]
        width = abs(sh["strike"] - lg["strike"])
        if debit < 0.10 or width - debit < debit:          # must be able to make at least what it costs
            continue
        natural = lg["ask"] - sh["bid"]
        limit = round(debit + (natural - debit) * 0.3, 2)
        if max_cost and limit * 100 > max_cost:
            continue
        return {"symbol": lg["symbol"], "type": kind, "strike": lg["strike"], "expiry": lg["expiry"].isoformat(),
                "short_symbol": sh["symbol"], "short_strike": sh["strike"], "mid": round(debit, 2), "limit": limit,
                "width": width, "max_value": width, "single_cost": round(lg["mid"], 2), "delta": lg["delta"]}
    return None


def spread_mark(long_symbol: str, short_symbol: str) -> dict | None:
    """Current value of a debit spread (long leg minus short leg), from mid prices; bid = what closing would get."""
    d = marketdata._alpaca("/v1beta1/options/snapshots", {"symbols": f"{long_symbol},{short_symbol}", "feed": "indicative"})
    snaps = d.get("snapshots") or {}
    a, b = snaps.get(long_symbol), snaps.get(short_symbol)
    if not a or not b:
        return None
    qa, qb = a.get("latestQuote") or {}, b.get("latestQuote") or {}
    mid = ((qa.get("bp", 0) + qa.get("ap", 0)) - (qb.get("bp", 0) + qb.get("ap", 0))) / 2
    return {"mid": round(mid, 2), "bid": round((qa.get("bp") or 0) - (qb.get("ap") or 0), 2)}


def mleg_net_price(order: dict) -> float | None:
    """Net price of a filled two-leg order: what the bought leg cost minus what the sold leg brought in."""
    if order.get("filled_avg_price"):
        return abs(float(order["filled_avg_price"]))
    net = 0.0
    for leg in order.get("legs") or []:
        if not leg.get("filled_avg_price"):
            return None
        p = float(leg["filled_avg_price"])
        net += p if leg.get("side") == "buy" else -p
    return abs(net)


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
