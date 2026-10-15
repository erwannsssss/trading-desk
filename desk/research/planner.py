"""Runs the research team and produces a game plan.

    morning    before the US open: full team on stocks, ETFs, crypto and forex
    intraday   every ~90 minutes while the market is open: fast one-call analyst on fresh movers
    evening    after the close: full team on crypto and forex, which trade overnight
    overnight  a few times a night: fast analyst on crypto and forex
"""
from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timedelta

from desk import ai, config, datasources, marketdata, telegram
from desk.brokers import oanda
from desk.portfolio import snapshot
from desk.research import dossier as dossiers
from desk.research import scanner, smartmoney, team
from desk.state import load, now
from desk.telegram import esc

PLANS_DIR = config.DATA_DIR / "plans"
SESSIONS = {
    "morning": {"stocks": 18, "crypto": 3, "forex": 4, "fast": False, "reuse_macro": False},
    "intraday": {"stocks": 10, "crypto": 2, "forex": 2, "fast": True, "reuse_macro": True},
    "evening": {"stocks": 0, "crypto": 4, "forex": 5, "fast": False, "reuse_macro": False},
    "overnight": {"stocks": 0, "crypto": 3, "forex": 3, "fast": True, "reuse_macro": True},
}
SESSION_NOTES = {
    "morning": "Pre-market. Plans can be intraday, swing (1-5 days) or position (1-4 weeks).",
    "intraday": "The market is open now. Favor intraday and 1-5 day trades on what is moving right now.",
    "evening": "After the US close. Crypto and forex trade overnight; plan for the next 12 hours to a few days.",
    "overnight": "Overnight. Crypto and forex only; favor trades that can work over the next several hours.",
}
RESEARCH_COOLDOWN_HOURS = 3   # don't re-research a symbol that was looked at this recently


def quote_to_usd(instrument: str) -> float:
    quote = instrument.split("_")[1]
    if quote == "USD":
        return 1.0
    try:
        return oanda.price(f"{quote}_USD")["mid"]
    except Exception:
        return 1.0 / oanda.price(f"USD_{quote}")["mid"]


def recent_ideas(hours: float) -> list[tuple[dict, dict]]:
    """(plan, idea) from plans created in the last `hours`."""
    cutoff = now() - timedelta(hours=hours)
    out = []
    if not PLANS_DIR.exists():
        return out
    for path in PLANS_DIR.glob("*.json"):
        if path.name[:10] < (now().date() - timedelta(days=2)).isoformat():
            continue
        plan = json.loads(path.read_text())
        if datetime.fromisoformat(plan["created_at"]) >= cutoff:
            out += [(plan, i) for i in plan.get("ideas", [])]
    return out


def latest_macro(max_age_hours: float = 14) -> dict | None:
    best = None
    for plan, _ in recent_ideas(max_age_hours) or []:
        if plan.get("macro") and (best is None or plan["created_at"] > best["created_at"]):
            best = plan
    if best is None and PLANS_DIR.exists():  # plans with no ideas still carry a macro view
        for path in PLANS_DIR.glob("*.json"):
            plan = json.loads(path.read_text())
            if plan.get("macro") and datetime.fromisoformat(plan["created_at"]) >= now() - timedelta(hours=max_age_hours):
                if best is None or plan["created_at"] > best["created_at"]:
                    best = plan
    return best["macro"] if best else None


def extra_context(session: str) -> dict:
    from desk import learning
    return {"session_note": SESSION_NOTES[session], "tournament": learning.tournament_summary(),
            "backtest": learning.backtest_summary()}


def run(session: str = "morning") -> dict:
    started = time.monotonic()
    cfg = SESSIONS[session]
    state = load()
    book, details = snapshot(state)
    holdings = [p["symbol"] for p in details["alpaca_positions"]] + [t["instrument"] for t in details["oanda_trades"]]
    from desk.trading import ledger
    from desk import learning
    active = ledger.active_symbols(ledger.load())

    # Skip what's already traded, planned, or researched in the last few hours.
    skip = set(active) | {i["symbol"] for _, i in recent_ideas(RESEARCH_COOLDOWN_HOURS)}
    skip |= {i["symbol"] for _, i in recent_ideas(12) if i.get("status") == "approved"}

    # 1. Scan everything (ask for extra so the skip list doesn't leave us short)
    stock_picks, stock_ctx = scanner.scan_stocks(cfg["stocks"] * 2, core_slots=0 if session == "intraday" else 5) if cfg["stocks"] else ([], {"etf_readings": {}, "scanned": 0})
    crypto_picks, crypto_ctx = scanner.scan_crypto(cfg["crypto"] * 2)
    fx_picks, fx_ctx = scanner.scan_forex(cfg["forex"] * 2)
    pick = lambda lst, n: [c for c in lst if c["symbol"] not in skip][:n]
    finalists = pick(stock_picks, cfg["stocks"]) + pick(crypto_picks, cfg["crypto"]) + pick(fx_picks, cfg["forex"])
    print(f"{session}: scanned {stock_ctx['scanned']} stocks/ETFs; {len(finalists)} finalists; skipped {len(skip)} recent")

    # 2. Macro strategist (fast sessions reuse the latest view to save AI calls)
    macro_ctx = {
        "now_new_york": now().strftime("%A %Y-%m-%d %H:%M"),
        "session": session,
        "economy": _try(datasources.macro_snapshot),
        "calendar_next_10_days": _try(datasources.economic_calendar, 10),
        "big_earnings_next_3_days": stock_ctx.get("earnings_soon"),
        "etfs": stock_ctx["etf_readings"],
        "crypto": crypto_ctx["crypto_readings"],
        "forex": fx_ctx["forex_readings"],
        "headlines": _try(marketdata.news, None, 15, 1),
        "calendar_effects": _calendar_effects(),
    }
    macro = latest_macro() if cfg["reuse_macro"] else None
    if not macro:
        macro = team.macro_view(macro_ctx)
    print("macro:", macro.get("regime"), macro.get("risk_appetite"))

    # 3. Research each finalist
    funds = _try(smartmoney.fund_holdings) or {}
    if not isinstance(funds, dict) or "unavailable" in funds:
        funds = {}
    rulebook = _rulebook()
    extra = extra_context(session)
    planned = [{"symbol": i["symbol"], "direction": (i.get("decision") or {}).get("decision"),
                "style": (i.get("decision") or {}).get("style")} for _, i in recent_ideas(12) if i.get("status") == "approved"]
    ideas = []
    for cand in finalists:
        sym = cand["symbol"]
        try:
            d = dossiers.build(cand, funds, light=cfg["fast"])
            if cfg["fast"]:
                q = team.quick_desk(d, macro, planned, holdings, rulebook, extra)
                notes, bull, bear, pm = q.get("specialists"), q.get("bull"), q.get("bear"), q["decision"]
            else:
                notes = team.specialists(d, macro)
                bull = team.bull(d, notes)
                bear = team.bear(d, notes, bull)
                pm = team.portfolio_manager(d, notes, bull, bear, macro, planned, holdings, rulebook, extra)
            check = team.fact_check(pm, dossiers.facts(d), d)
            idea = {"symbol": sym, "venue": cand["venue"], "asset": cand["asset"],
                    "price_at_research": d["technicals"]["price"], "why_scanned": cand.get("why_scanned"),
                    "setups": d["technicals"].get("setups"),
                    "specialists": notes, "bull": bull, "bear": bear, "decision": pm, "fact_check": check}
            if pm.get("decision") in ("long", "short") and check["passed"]:
                kw = {}
                if cand["asset"] == "forex":
                    kw = {"quote_to_usd": quote_to_usd(sym), "unit_usd": oanda.usd_value_per_unit(sym)}
                kw["multiplier"] = learning.risk_multiplier(pm.get("style"))
                idea["sizing"] = team.size(pm, book.equity, cand["asset"], **kw)
                idea["status"] = "approved"
                planned.append({"symbol": sym, "direction": pm["decision"], "style": pm.get("style")})
            else:
                idea["status"] = "pass" if pm.get("decision") == "pass" else "blocked_by_fact_check"
            ideas.append(idea)
            print(f"{sym}: {idea['status']} {pm.get('decision')} {pm.get('expression')} conv={pm.get('conviction')} {check['problems'][:2]}")
        except Exception as exc:
            traceback.print_exc()
            ideas.append({"symbol": sym, "venue": cand["venue"], "asset": cand["asset"], "status": "error", "error": str(exc)[:300]})

    stamp = now()
    plan = {
        "date": stamp.date().isoformat(), "session": session, "created_at": stamp.isoformat(timespec="seconds"),
        "dry_run": state.get("dry_run", True), "equity": round(book.equity, 2),
        "macro": macro, "macro_inputs": {k: macro_ctx[k] for k in ("economy", "calendar_next_10_days")},
        "ideas": ideas, "ai_calls": ai.calls_made, "ai_attempts": ai.attempts,
        "minutes": round((time.monotonic() - started) / 60, 1),
    }
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{plan['date']}-{session}" + (stamp.strftime("-%H%M") if session in ("intraday", "overnight") else "")
    path = PLANS_DIR / f"{name}.json"
    path.write_text(json.dumps(plan, indent=1, default=str))
    if cfg["fast"]:
        text = quick_brief(plan)
        if text:
            telegram.send(text, silent=True)
    else:
        telegram.send(brief(plan))
    return plan


def quick_brief(plan: dict) -> str | None:
    approved = [i for i in plan["ideas"] if i.get("status") == "approved"]
    if not approved:
        return None
    label = "Intraday scan" if plan["session"] == "intraday" else "Overnight scan"
    lines = [f"<b>{label}: {len(approved)} new idea{'s' if len(approved) > 1 else ''}</b>"]
    for i in approved:
        d = i["decision"]
        expr = {"call_options": " via calls", "put_options": " via puts"}.get(d.get("expression"), "")
        lines.append(f"{d['decision'].upper()} {esc(i['symbol'])}{expr} | {esc(d.get('style'))} | conv {d.get('conviction')}/10 | "
                     f"entry {_p(d['entry'])}, stop {_p(d['stop'])}, target {_p(d['target'])}")
    return "\n".join(lines)


def brief(plan: dict) -> str:
    m = plan["macro"]
    w = m.get("bucket_weights", {})
    title = "Morning brief" if plan["session"] == "morning" else "Evening brief (crypto + forex)"
    lines = [
        f"<b>{title}, {plan['date']}</b>",
        f"Market: <b>{esc(m.get('regime'))}</b> | aggression {m.get('risk_appetite')}/100",
        f"Buckets: core {w.get('core')}% / tactical {w.get('tactical')}% / high-risk {w.get('high_risk')}%",
        "",
        esc(m.get("summary", "")),
    ]
    if m.get("overnight_read"):
        lines += ["", f"<b>Overnight:</b> {esc(m['overnight_read'])}"]
    if m.get("key_events"):
        lines += ["", "<b>Watch:</b>"] + [f"- {esc(e)}" for e in m["key_events"][:5]]

    approved = [i for i in plan["ideas"] if i.get("status") == "approved"]
    approved.sort(key=lambda i: -(i["decision"].get("conviction") or 0))
    lines += ["", f"<b>Trade plan ({len(approved)} ideas)</b>"]
    for i in approved:
        d, s = i["decision"], i["sizing"]
        lines += [
            "",
            f"<b>{d['decision'].upper()} {esc(i['symbol'])}{ {'call_options': ' via calls', 'put_options': ' via puts'}.get(d.get('expression'), '')}</b> | {esc(d.get('bucket'))} | {esc(d.get('style'))} | conviction {d.get('conviction')}/10",
            f"Entry {_p(d['entry'])} | stop {_p(d['stop'])} | target {_p(d['target'])} | reward/risk {d.get('reward_to_risk')}",
            f"Size ${s['notional_usd']:,.0f}, risking ${s['risk_usd']:,.0f} ({s['risk_pct']}%) | {esc(d.get('horizon'))}",
            f"<i>{esc(d.get('thesis'))}</i>",
        ]
    passed = [i["symbol"] for i in plan["ideas"] if i.get("status") == "pass"]
    blocked = [i for i in plan["ideas"] if i.get("status") == "blocked_by_fact_check"]
    if passed:
        lines += ["", f"<b>Passed on:</b> {esc(', '.join(passed))}"]
    if blocked:
        lines += ["", "<b>Blocked by the fact-checker:</b>"] + [
            f"- {esc(b['symbol'])}: {esc(b['fact_check']['problems'][0])}" for b in blocked]
    lines += ["", f"Researched {len(plan['ideas'])} finalists with {plan['ai_calls']} AI calls in {plan['minutes']} min."]
    if plan["dry_run"]:
        lines.append("Dry run: no orders placed. Send /golive when you're ready.")
    return "\n".join(lines)


def _p(x) -> str:
    x = float(x)
    return f"{x:,.2f}" if x >= 10 else f"{x:.4f}" if x >= 0.01 else f"{x:.8f}"


def _try(fn, *a):
    try:
        return fn(*a)
    except Exception as exc:
        return {"unavailable": str(exc)[:120]}


def _calendar_effects() -> dict:
    import calendar
    d = now().date()
    last = calendar.monthrange(d.year, d.month)[1]
    return {"weekday": d.strftime("%A"), "trading_days_left_in_month_approx": max(0, int((last - d.day) * 5 / 7)),
            "month": d.strftime("%B"), "notes": "Month-end fund flows often lift stocks in the last 2-3 days and first days of the month; "
                                                 "October has a history of volatility; Fridays see position squaring before weekends."}


def _rulebook() -> list[str]:
    path = config.DATA_DIR / "rulebook.json"
    if path.exists():
        return [r["rule"] for r in json.loads(path.read_text())][-30:]
    return []
