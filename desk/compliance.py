"""Diane Foster, chief compliance officer. Audits every trade against the firm's own rules.

The risk manager blocks bad orders before they go in. Compliance checks what actually happened afterwards,
like a real compliance desk: fills, stops, holding periods, losses, and firm-wide limits.

Each finding names the rule, how serious it is (minor or major), who was responsible, and what happened.
Major findings go to Telegram; all of them feed Ashley's weekly reviews.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from desk import config
from desk.state import now

FILE = config.DATA_DIR / "compliance.json"
NY = ZoneInfo(config.TIMEZONE)
SLIPPAGE_LIMIT = {"stock": 0.5, "etf": 0.4, "crypto": 1.0, "forex": 0.1, "option": 5.0}   # % from the planned entry
HOLD_LIMIT_DAYS = {"intraday": 1, "1-5 days": 8, "1-4 weeks": 35}
RISK_TOLERANCE = 1.10          # 10% over the per-trade cap counts as a breach (prices move between plan and fill)


def load() -> dict:
    try:
        data = json.loads(FILE.read_text()) if FILE.exists() else {}
    except Exception:
        data = {}
    data.setdefault("findings", [])
    data.setdefault("checked", {})      # trade id -> list of checks already done
    data.setdefault("days", {})         # date -> {"audits": n, "findings": n}
    return data


def save(data: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    data["findings"] = data["findings"][-2000:]
    FILE.write_text(json.dumps(data, indent=1, default=str) + "\n")


def _ny(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(NY)


def _r(t: dict) -> float | None:
    if t.get("asset") == "option":
        return None
    entry, stop = t.get("entry_price"), t.get("initial_stop") or t.get("stop")
    return abs(entry - stop) if entry and stop else None


def check_open(t: dict, equity: float) -> list[dict]:
    """Checks on a trade once it has filled."""
    out = []
    asset = t.get("asset", "stock")
    entry, plan = t.get("entry_price"), t.get("entry_plan")
    if t.get("stop") is None and asset != "option":
        out.append(("No stop loss", "major", "risk", "Position is open without a stop loss; every trade must have one."))
    if entry and plan and asset != "option":
        long = t.get("direction") == "long"
        slip = (entry / plan - 1) * 100 * (1 if long else -1)   # positive = worse than planned
        if slip > SLIPPAGE_LIMIT.get(asset, 0.5):
            out.append(("Fill too far from plan", "minor", "execution",
                        f"Filled at {entry:g}, {slip:.2f}% worse than the planned {plan:g} (limit {SLIPPAGE_LIMIT.get(asset, 0.5)}%)."))
    risk = None
    if asset == "option" and t.get("premium_paid") and t.get("filled_qty"):
        risk = t["premium_paid"] * t["filled_qty"] * 100
        cap = equity * config.MAX_OPTIONS_PREMIUM_PCT / 100
        if risk > cap * RISK_TOLERANCE:
            out.append(("Options premium over cap", "major", "options",
                        f"Paid ${risk:,.0f} in premium, {risk / equity * 100:.1f}% of the firm (cap {config.MAX_OPTIONS_PREMIUM_PCT}%)."))
    elif _r(t) and t.get("filled_qty"):
        risk = _r(t) * t["filled_qty"] * t.get("quote_to_usd", 1.0)
        cap = equity * config.MAX_RISK_PER_TRADE_PCT / 100
        if risk > cap * RISK_TOLERANCE:
            out.append(("Risk per trade over cap", "major", "sizer",
                        f"At the actual fill, a stop-out loses ${risk:,.0f}, {risk / equity * 100:.1f}% of the firm "
                        f"(cap {config.MAX_RISK_PER_TRADE_PCT}%)."))
        if t.get("target") and entry:
            rr = abs(t["target"] - entry) / _r(t)
            if rr < config.MIN_REWARD_TO_RISK - 0.05:
                out.append(("Reward too small for the risk", "minor", "pm",
                            f"At the fill price the target pays only {rr:.2f}x the risk (minimum {config.MIN_REWARD_TO_RISK}x)."))
    if asset in ("stock", "etf", "option") and t.get("opened_at"):
        o = _ny(t["opened_at"])
        minutes = o.hour * 60 + o.minute
        if o.weekday() < 5 and (570 <= minutes < 570 + config.NO_TRADE_OPEN_MINUTES or 960 - config.NO_TRADE_CLOSE_MINUTES <= minutes < 960):
            out.append(("Traded in the blackout window", "minor", "execution",
                        f"Filled at {o.strftime('%-I:%M %p')}, inside the first or last {config.NO_TRADE_OPEN_MINUTES} minutes of the session."))
    return out


def check_holding(t: dict, at: datetime) -> list[dict]:
    """Checks on an open trade, every run."""
    out = []
    long = t.get("direction") == "long"
    init, stop = t.get("initial_stop"), t.get("stop")
    if init is not None and stop is not None and t.get("asset") != "option":
        if (long and stop < init - 1e-9) or (not long and stop > init + 1e-9):
            out.append(("Stop moved further away", "major", "positions",
                        f"Stop moved from {init:g} to {stop:g}. Stops may only be tightened, never widened."))
    limit = HOLD_LIMIT_DAYS.get(t.get("horizon"))
    if limit and t.get("opened_at"):
        held = (at - datetime.fromisoformat(t["opened_at"])).total_seconds() / 86400
        if held > limit + 0.5:
            out.append(("Held past its time limit", "minor", "positions",
                        f"Planned as {t['horizon']} but held {held:.1f} days."))
    return out


def check_close(t: dict) -> list[dict]:
    out = []
    r = t.get("r_result")
    if r is not None and t.get("asset") != "option" and r < -1.25:
        sev = "major" if r < -2 else "minor"
        out.append(("Loss bigger than planned", sev, "risk",
                    f"Lost {abs(r):.2f}R (planned maximum 1R) on {t.get('exit_reason') or 'exit'}; the stop didn't hold the line."))
    return out


def check_firm(book, at: datetime) -> list[dict]:
    from desk.bank import CREDIT_LINE
    out = []
    eq = book.equity
    if book.open_risk > eq * config.MAX_TOTAL_OPEN_RISK_PCT / 100 * RISK_TOLERANCE:
        out.append(("Total open risk over cap", "major", "risk",
                    f"If every stop hit, the firm would lose ${book.open_risk:,.0f}, {book.open_risk / eq * 100:.1f}% "
                    f"(cap {config.MAX_TOTAL_OPEN_RISK_PCT}%)."))
    if book.gross_exposure > max(0.0, eq) + CREDIT_LINE + 1:
        out.append(("Exposure over the credit line", "major", "treasury",
                    f"Positions total ${book.gross_exposure:,.0f}, more than equity plus the ${CREDIT_LINE:,.0f} line."))
    return out


def audit(trades: list[dict], book, at: datetime | None = None) -> list[dict]:
    """Run every check that hasn't run yet. Returns the new findings."""
    from desk import activity as floor
    from desk import staff
    at = at or now()
    data = load()
    day = at.date().isoformat()
    tally = data["days"].setdefault(day, {"audits": 0, "findings": 0})
    new = []

    def record(t, check, items):
        done = data["checked"].setdefault(t["id"] if t else f"firm-{day}", [])
        for rule, sev, seat, detail in items:
            key = f"{check}:{rule}"
            if key in done:
                continue
            done.append(key)
            f = {"id": f"c{len(data['findings']) + 1}-{at.strftime('%H%M%S')}", "date": day, "at": at.isoformat(timespec="seconds"),
                 "trade_id": t["id"] if t else None, "symbol": t["symbol"] if t else None, "rule": rule, "severity": sev,
                 "seat": seat, "who": staff.name(seat), "detail": detail}
            data["findings"].append(f)
            new.append(f)

    for t in trades:
        done = data["checked"].setdefault(t["id"], [])
        if t.get("status") in ("open", "closing", "closed") and t.get("entry_price") and "open" not in done:
            record(t, "open", check_open(t, book.equity))
            done.append("open")
            tally["audits"] += 1
        if t.get("status") == "open":
            record(t, "hold", check_holding(t, at))
        if t.get("status") == "closed" and "close" not in done:
            record(t, "close", check_close(t))
            done.append("close")
            tally["audits"] += 1
    record(None, "firm", check_firm(book, at))
    tally["findings"] += len(new)
    # keep the checked map small: drop closed trades older than 60 days
    old = (at - timedelta(days=60)).isoformat()
    live_ids = {t["id"] for t in trades if (t.get("closed_at") or "9") >= old}
    data["checked"] = {k: v for k, v in data["checked"].items() if k in live_ids or k.startswith("firm-") and k >= f"firm-{old[:10]}"}
    save(data)

    for f in new:
        floor.event("compliance", f"{f['severity'].upper()}: {f['rule']}" + (f" on {f['symbol']}" if f["symbol"] else "")
                    + f". Responsible: {f['who']}. {f['detail']}", f.get("symbol"), kind="compliance")
    return new


def findings_by_seat(days: int = 7) -> dict[str, list[str]]:
    since = (now() - timedelta(days=days)).date().isoformat()
    out: dict[str, list[str]] = {}
    for f in load()["findings"]:
        if f["date"] >= since:
            out.setdefault(f["seat"], []).append(f"{f['severity']}: {f['rule']}" + (f" ({f['symbol']})" if f.get("symbol") else ""))
    return out


def summary() -> dict:
    data = load()
    since30 = (now() - timedelta(days=30)).date().isoformat()
    recent = [f for f in data["findings"] if f["date"] >= since30]
    audits30 = sum(v["audits"] for d, v in data["days"].items() if d >= since30)
    by_seat: dict[str, int] = {}
    for f in recent:
        by_seat[f["seat"]] = by_seat.get(f["seat"], 0) + 1
    return {"findings": data["findings"][-400:], "days": data["days"],
            "last_30": {"audits": audits30, "findings": len(recent), "major": sum(f["severity"] == "major" for f in recent),
                        "clean_pct": round(max(0.0, 1 - len({f["trade_id"] for f in recent if f.get("trade_id")}) / audits30) * 100) if audits30 else None,
                        "by_seat": by_seat}}
