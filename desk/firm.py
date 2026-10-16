"""The business side of the firm: job levels, payroll, bonuses and unlockable desks.

Payroll works like a real hedge fund's: staff are paid out of fees on the money managed, not out of trading cash.
  - Base pay pool: 2% a year of firm equity (the "management fee"), charged a little every 15 minutes and split
    across the staff by level (Junior 0.6x, Associate 1x, Senior 1.5x, Principal 2.2x).
  - Bonus pool: 20% of new profits above the firm's previous high (the "performance fee", with a high-water mark),
    paid once a month and split by level and hit rate. No new high, no bonuses.
  - Erwann (managing partner) takes no salary: whatever is left after the bank and payroll is his.
  - Interns are unpaid; their $150 account is the job.
Both come out of firm equity, so the P&L tab shows whether the firm is profitable after paying everyone.

Levels change in Ashley's weekly review (at most once every 4 weeks per person):
  promoted at 60%+ right over 15+ graded calls, demoted under 45%; a Junior who stays under 45% is let go.

Desks unlock when the firm's equity first reaches a milestone, and stay unlocked.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from desk import config
from desk.state import now

FILE = config.DATA_DIR / "firm.json"
MGMT_FEE = 0.02
PERF_FEE = 0.20
LEVELS = ["Junior", "Associate", "Senior", "Principal"]
LEVEL_PAY = {"Junior": 0.6, "Associate": 1.0, "Senior": 1.5, "Principal": 2.2}
PRINCIPALS = {"cio", "hr", "pm", "execution", "performance", "risk", "compliance"}
UNPAID = {"partner"}
PROMOTE_PCT, DEMOTE_PCT, LEVEL_MIN_CALLS, LEVEL_COOLDOWN_DAYS = 60, 45, 15, 28

# Unlockable desks: (key, equity milestone, title, what it does, seat that joins or None)
UNLOCKS = [
    ("crypto_desk", 10_500, "Crypto desk",
     "Lena Hoffmann joins as crypto strategist: an extra expert read on every crypto idea, plus one more crypto idea per session.", "crypto"),
    ("event_desk", 11_000, "Event desk",
     "Marisol Vega joins to trade earnings and news: an extra read on stocks reporting soon, plus one more stock idea per session.", "events"),
    ("fx_desk", 11_750, "FX desk",
     "Callum Reid joins as FX strategist: an extra read on every currency idea, plus one more forex idea per session.", "fx"),
    ("bank_terms", 12_500, "Better bank terms",
     "First Hoosier Bank cuts the credit line rate from 8% to 6.5% a year on new loans.", None),
    ("quant_desk", 13_500, "Quant desk",
     "Arjun Mehta joins: every idea comes with the 2-year backtested record of its chart setup.", "quantdesk"),
    ("intern_class", 15_000, "Bigger intern class",
     "The intern program grows from 3 to 5 seats.", None),
]


# ---- storage -----------------------------------------------------------------
def load() -> dict:
    try:
        data = json.loads(FILE.read_text()) if FILE.exists() else {}
    except Exception:
        data = {}
    data.setdefault("levels", {})
    data.setdefault("level_log", [])
    data.setdefault("payroll", {"accrued_through": None, "total": 0.0, "by_day": {}, "by_seat": {}, "by_month_seat": {}})
    data.setdefault("bonuses", {"hwm": config.STARTING_CAPITAL, "total": 0.0, "paid": []})
    data.setdefault("unlocked", {})
    return data


def save(data: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, indent=1, default=str) + "\n")


# ---- levels ------------------------------------------------------------------
def default_level(seat: str) -> str:
    return "Principal" if seat in PRINCIPALS else "Associate"


def level(seat: str, data: dict | None = None) -> str | None:
    if seat in UNPAID or seat.startswith("intern:"):
        return None
    data = data or load()
    return (data["levels"].get(seat) or {}).get("level") or default_level(seat)


def set_level(seat: str, new: str, reason: str, data: dict | None = None) -> None:
    own = data is None
    data = data or load()
    old = level(seat, data)
    data["levels"][seat] = {"level": new, "since": now().date().isoformat()}
    if old == new:          # not a promotion or demotion (e.g. a new hire starting at the default level)
        if own:
            save(data)
        return
    data["level_log"].append({"date": now().date().isoformat(), "seat": seat, "from": old, "to": new, "reason": reason})
    data["level_log"] = data["level_log"][-200:]
    if own:
        save(data)


def review_levels(scores: dict) -> list[dict]:
    """Promote or demote people from their 30-day hit rates. Returns changes; kind "fire" means a Junior stayed bad."""
    from desk import staff
    data = load()
    changes = []
    today = now().date()
    for p in staff.org_chart():
        seat = p["id"]
        if seat in UNPAID or p.get("locked"):
            continue
        sc = scores.get(seat) or {}
        if sc.get("total", 0) < LEVEL_MIN_CALLS:
            continue
        pct = sc["right"] / sc["total"] * 100
        cur = level(seat, data)
        since = (data["levels"].get(seat) or {}).get("since") or p.get("since")
        if since and (today - datetime.fromisoformat(since).date()).days < LEVEL_COOLDOWN_DAYS:
            continue
        i = LEVELS.index(cur)
        why = f"{pct:.0f}% right on {sc['total']} calls"
        if pct >= PROMOTE_PCT and i < len(LEVELS) - 1:
            set_level(seat, LEVELS[i + 1], why, data)
            changes.append({"seat": seat, "name": p["name"], "kind": "promote", "from": cur, "to": LEVELS[i + 1], "why": why})
        elif pct < DEMOTE_PCT:
            if i > 0:
                set_level(seat, LEVELS[i - 1], why, data)
                changes.append({"seat": seat, "name": p["name"], "kind": "demote", "from": cur, "to": LEVELS[i - 1], "why": why})
            elif p.get("fireable"):
                changes.append({"seat": seat, "name": p["name"], "kind": "fire", "from": cur, "to": None, "why": why})
    save(data)
    return changes


# ---- payroll -----------------------------------------------------------------
def payroll_weights(data: dict) -> dict[str, float]:
    from desk import staff
    return {p["id"]: LEVEL_PAY[level(p["id"], data)] for p in staff.org_chart()
            if p["id"] not in UNPAID and not p.get("locked")}


def accrue_payroll(equity: float, at: datetime | None = None) -> float:
    """Charge base pay since the last run. Returns the amount charged."""
    at = at or now()
    data = load()
    pr = data["payroll"]
    last = datetime.fromisoformat(pr["accrued_through"]) if pr.get("accrued_through") else None
    pr["accrued_through"] = at.isoformat(timespec="seconds")
    if last is None or equity <= 0:
        save(data)
        return 0.0
    days = min(2.0, max(0.0, (at - last).total_seconds() / 86400))   # never back-charge more than 2 days at once
    amount = equity * MGMT_FEE * days / 365
    if amount <= 0:
        save(data)
        return 0.0
    weights = payroll_weights(data)
    total_w = sum(weights.values()) or 1.0
    day, month = at.date().isoformat(), at.strftime("%Y-%m")
    pr["total"] = round(pr["total"] + amount, 6)
    pr["by_day"][day] = round(pr["by_day"].get(day, 0.0) + amount, 6)
    ms = pr["by_month_seat"].setdefault(month, {})
    for seat, w in weights.items():
        share = amount * w / total_w
        pr["by_seat"][seat] = round(pr["by_seat"].get(seat, 0.0) + share, 6)
        ms[seat] = round(ms.get(seat, 0.0) + share, 6)
    save(data)
    return amount


def opex_total(data: dict | None = None) -> float:
    data = data or load()
    return round(data["payroll"]["total"] + data["bonuses"]["total"], 4)


def annual_salary(seat: str, equity: float, data: dict | None = None) -> float:
    data = data or load()
    weights = payroll_weights(data)
    return equity * MGMT_FEE * weights.get(seat, 0) / (sum(weights.values()) or 1)


# ---- bonuses -----------------------------------------------------------------
def _perf_factor(sc: dict | None) -> float:
    if not sc or sc.get("total", 0) < 3:
        return 0.5                      # not enough graded calls: half share
    pct = sc["right"] / sc["total"] * 100
    if pct < DEMOTE_PCT:
        return 0.0
    return min(2.0, 0.5 + (pct - DEMOTE_PCT) / 10)


def maybe_pay_bonuses(state: dict, equity: float, scores: dict) -> dict | None:
    """Once a month (days 1-3, after 8am): pay 20% of profits above the high-water mark."""
    at = now()
    month = (at.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    if at.day > 3 or at.hour < 8 or state.get("bonus_month") == month:
        return None
    state["bonus_month"] = month
    return pay_bonuses(month, equity, scores)


def pay_bonuses(month: str, equity: float, scores: dict) -> dict:
    from desk import staff
    data = load()
    b = data["bonuses"]
    profit = equity - b["hwm"]
    pool = round(PERF_FEE * profit, 2) if profit > 0 else 0.0
    weights = {seat: LEVEL_PAY[level(seat, data)] * _perf_factor(scores.get(seat)) for seat in payroll_weights(data)}
    total_w = sum(weights.values())
    by_seat = {}
    if pool > 0 and total_w > 0:
        for seat, w in weights.items():
            if w > 0:
                by_seat[seat] = {"name": staff.name(seat), "level": level(seat, data), "amount": round(pool * w / total_w, 2),
                                 "pct": (scores.get(seat) or {}).get("pct")}
    paid = round(sum(v["amount"] for v in by_seat.values()), 2)
    record = {"month": month, "date": now().date().isoformat(), "equity": round(equity, 2), "hwm_before": round(b["hwm"], 2),
              "profit_above_hwm": round(profit, 2), "pool": paid, "by_seat": by_seat}
    b["paid"].append(record)
    b["paid"] = b["paid"][-36:]
    b["total"] = round(b["total"] + paid, 4)
    b["hwm"] = round(max(b["hwm"], equity - paid), 2)
    save(data)
    return record


# ---- unlockable desks ----------------------------------------------------------
def unlocked(key: str, data: dict | None = None) -> bool:
    return key in (data or load())["unlocked"]


def unlock_for_seat(seat: str) -> tuple | None:
    return next((u for u in UNLOCKS if u[4] == seat), None)


def seat_unlocked(seat: str, data: dict | None = None) -> bool:
    u = unlock_for_seat(seat)
    return u is None or unlocked(u[0], data)


def check_unlocks(equity: float) -> list[tuple]:
    """Unlock every milestone the firm has reached. Returns the newly unlocked ones."""
    data = load()
    new = []
    for u in UNLOCKS:
        if u[0] not in data["unlocked"] and equity >= u[1]:
            data["unlocked"][u[0]] = now().date().isoformat()
            new.append(u)
    if new:
        save(data)
        from desk import staff
        sdata = staff.load()
        for u in new:
            if u[4]:
                sdata["seats"].setdefault(u[4], {"name": staff.ALL_SEATS[u[4]][0], "since": None, "approach": None})
                sdata["seats"][u[4]]["since"] = now().date().isoformat()
        staff.save(sdata)
    return new


def loan_apr() -> float:
    return 0.065 if unlocked("bank_terms") else 0.08


def intern_slots() -> int:
    return 5 if unlocked("intern_class") else 3


def summary(equity: float) -> dict:
    """Everything the dashboard needs about levels, pay and unlocks."""
    data = load()
    weights = payroll_weights(data)
    total_w = sum(weights.values()) or 1
    return {
        "levels": {seat: level(seat, data) for seat in weights},
        "level_log": [x for x in data["level_log"] if x.get("from") != x.get("to")][-60:],
        "salaries": {seat: round(equity * MGMT_FEE * w / total_w, 2) for seat, w in weights.items()},
        "payroll_total": round(data["payroll"]["total"], 2),
        "payroll_by_seat": {k: round(v, 2) for k, v in data["payroll"]["by_seat"].items()},
        "payroll_by_day": {k: round(v, 4) for k, v in data["payroll"]["by_day"].items()},
        "payroll_by_month_seat": {m: {k: round(v, 4) for k, v in seats.items()} for m, seats in data["payroll"]["by_month_seat"].items()},
        "bonus_total": round(data["bonuses"]["total"], 2),
        "bonuses": data["bonuses"]["paid"],
        "hwm": data["bonuses"]["hwm"],
        "unlocks": [{"key": k, "at": at, "title": t, "effect": e, "seat": s, "unlocked": data["unlocked"].get(k)}
                    for k, at, t, e, s in UNLOCKS],
        "fees": {"management_pct": MGMT_FEE * 100, "performance_pct": PERF_FEE * 100},
    }
