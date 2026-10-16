"""The firm's income statement: is the firm actually profitable after the bank and payroll?

For each day (New York date):
  trading gains   = realized P&L on closed trades (by asset class) + change in unrealized P&L on open ones
  interest        = bank interest charged that day (paid on closed loans plus what built up on open ones)
  payroll         = base pay charged that day (2% a year of equity, see firm.py)
  bonuses         = performance bonuses paid that day
  fees            = broker trading fees (crypto fees come out of the coins bought or the dollars received)
  errors          = losses from operational mistakes outside trading (e.g. Claude's test order), each refunded
  refunds         = those refunds (data/corrections.json), so errors and refunds net to zero
  net income      = trading gains - fees - interest - payroll - bonuses - errors + refunds  (= the change in firm equity)
Days are built from the equity log, which records equity, interest and staff costs every 15 minutes.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

from desk import config

NY = ZoneInfo(config.TIMEZONE)
ASSETS = ("stock", "etf", "option", "crypto", "forex")


def _day(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(NY).date().isoformat()


def _rows() -> list[dict]:
    path = config.DATA_DIR / "equity.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def statement(trades: list[dict], firm_data: dict) -> dict:
    rows = _rows()
    ends: "OrderedDict[str, dict]" = OrderedDict()
    for r in rows:
        ends[_day(r["t"])] = r          # last reading of each day
    realized: dict[str, dict[str, float]] = {}
    count: dict[str, int] = {}
    for t in trades:
        if t.get("status") == "closed" and t.get("pnl_usd") is not None and t.get("closed_at"):
            d = _day(t["closed_at"])
            a = t.get("asset") if t.get("asset") in ASSETS else "stock"
            realized.setdefault(d, {})[a] = realized.setdefault(d, {}).get(a, 0.0) + t["pnl_usd"]
            count[d] = count.get(d, 0) + 1
    fees: dict[str, float] = {}
    for t in trades:
        if t.get("fee_usd") and t.get("opened_at"):
            d = _day(t["opened_at"]); fees[d] = fees.get(d, 0.0) + t["fee_usd"]
        if t.get("exit_fee_usd") and t.get("closed_at"):
            d = _day(t["closed_at"]); fees[d] = fees.get(d, 0.0) + t["exit_fee_usd"]
    errors: dict[str, float] = {}
    try:
        from desk import corrections as _c
        for c in _c.load():
            errors[c["date"]] = errors.get(c["date"], 0.0) + c.get("amount", 0.0)
    except Exception:
        pass
    bonus_by_day: dict[str, float] = {}
    for b in firm_data.get("bonuses", {}).get("paid", []):
        bonus_by_day[b["date"]] = bonus_by_day.get(b["date"], 0.0) + b.get("pool", 0.0)

    days = []
    prev = {"equity": config.STARTING_CAPITAL, "fin": 0.0, "opex": 0.0, "corr": 0.0}
    for d, r in ends.items():
        net = r["equity"] - prev["equity"]
        fin = r.get("fin", prev.get("fin", 0.0)) or 0.0
        opex = r.get("opex", prev.get("opex", 0.0)) or 0.0
        interest = max(0.0, fin - (prev.get("fin") or 0.0))
        staff_cost = max(0.0, opex - (prev.get("opex") or 0.0))
        bonus = min(staff_cost, bonus_by_day.get(d, 0.0))
        payroll = staff_cost - bonus
        corr = r.get("corr", prev.get("corr", 0.0)) or 0.0
        correction = corr - (prev.get("corr") or 0.0)      # refunds for operational errors aren't trading profit
        fee = fees.get(d, 0.0)
        error = errors.get(d, 0.0)                         # the loss the refund covers, booked on the day it happened
        gross = net + interest + staff_cost - correction + fee + error
        real = {a: round(v, 2) for a, v in (realized.get(d) or {}).items()}
        real_total = sum(real.values())
        days.append({"date": d, "equity": round(r["equity"], 2), "realized": real, "realized_total": round(real_total, 2),
                     "unrealized_change": round(gross - real_total, 2), "trading": round(gross, 2),
                     "interest": round(interest, 2), "payroll": round(payroll, 4), "bonuses": round(bonus, 2),
                     "fees": round(fee, 2), "errors": round(error, 2),
                     "net": round(net, 2), "corrections": round(correction, 2), "closed_trades": count.get(d, 0)})
        prev = {"equity": r["equity"], "fin": fin, "opex": opex, "corr": corr}

    months: "OrderedDict[str, dict]" = OrderedDict()
    for row in days:
        m = months.setdefault(row["date"][:7], {"month": row["date"][:7], "trading": 0.0, "realized_total": 0.0,
                                                 "unrealized_change": 0.0, "interest": 0.0, "payroll": 0.0, "bonuses": 0.0, "fees": 0.0, "errors": 0.0, "corrections": 0.0,
                                                 "net": 0.0, "closed_trades": 0, "realized": {}, "days": 0})
        for k in ("trading", "realized_total", "unrealized_change", "interest", "payroll", "bonuses", "fees", "errors", "corrections", "net"):
            m[k] += row[k]
        for a, v in row["realized"].items():
            m["realized"][a] = m["realized"].get(a, 0.0) + v
        m["closed_trades"] += row["closed_trades"]
        m["days"] += 1
    for m in months.values():
        for k in ("trading", "realized_total", "unrealized_change", "interest", "payroll", "bonuses", "fees", "errors", "corrections", "net"):
            m[k] = round(m[k], 2)
        m["realized"] = {a: round(v, 2) for a, v in m["realized"].items()}
        m["profitable"] = m["net"] > 0

    tot = {k: round(sum(r[k] for r in days), 2) for k in ("trading", "realized_total", "unrealized_change", "interest", "payroll", "bonuses", "fees", "errors", "corrections", "net")}
    costs = tot["interest"] + tot["payroll"] + tot["bonuses"] + tot["fees"]
    tot["costs"] = round(costs, 2)
    tot["cost_share_pct"] = round(costs / tot["trading"] * 100, 1) if tot["trading"] > 0 else None
    tot["profitable"] = tot["net"] > 0
    tot["profitable_days"] = sum(r["net"] > 0 for r in days)
    tot["days"] = len(days)
    tot["realized"] = {}
    for r in days:
        for a, v in r["realized"].items():
            tot["realized"][a] = round(tot["realized"].get(a, 0.0) + v, 2)
    return {"days": days[-400:], "months": list(months.values()), "total": tot}
