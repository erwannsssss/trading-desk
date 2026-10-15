"""Runs the whole research team and produces the day's game plan.

    morning session  (before the US open): stocks, ETFs, crypto and forex
    evening session  (after the close):   crypto and forex, which trade overnight
"""
from __future__ import annotations

import json
import time
import traceback

from desk import ai, config, datasources, marketdata, telegram
from desk.brokers import oanda
from desk.portfolio import snapshot
from desk.research import dossier as dossiers
from desk.research import scanner, smartmoney, team
from desk.state import load, now
from desk.telegram import esc

PLANS_DIR = config.DATA_DIR / "plans"
SESSIONS = {
    "morning": {"stocks": 18, "crypto": 3, "forex": 4},
    "evening": {"stocks": 0, "crypto": 4, "forex": 5},
}


def quote_to_usd(instrument: str) -> float:
    quote = instrument.split("_")[1]
    if quote == "USD":
        return 1.0
    try:
        return oanda.price(f"{quote}_USD")["mid"]
    except Exception:
        return 1.0 / oanda.price(f"USD_{quote}")["mid"]


def run(session: str = "morning") -> dict:
    started = time.monotonic()
    plan_cfg = SESSIONS[session]
    state = load()
    book, details = snapshot(state)
    holdings = [p["symbol"] for p in details["alpaca_positions"]] + [t["instrument"] for t in details["oanda_trades"]]

    # 1. Scan everything
    stock_picks, stock_ctx = scanner.scan_stocks(plan_cfg["stocks"])
    crypto_picks, crypto_ctx = scanner.scan_crypto(plan_cfg["crypto"])
    fx_picks, fx_ctx = scanner.scan_forex(plan_cfg["forex"])
    finalists = stock_picks + crypto_picks + fx_picks
    print(f"scanned {stock_ctx['scanned']} stocks/ETFs; {len(finalists)} finalists")

    # 2. Macro strategist sets the game plan
    macro_ctx = {
        "now_new_york": now().strftime("%A %Y-%m-%d %H:%M"),
        "session": session,
        "economy": datasources.macro_snapshot(),
        "calendar_next_10_days": _try(datasources.economic_calendar, 10),
        "big_earnings_next_3_days": stock_ctx.get("earnings_soon"),
        "etfs": stock_ctx["etf_readings"],
        "crypto": crypto_ctx["crypto_readings"],
        "forex": fx_ctx["forex_readings"],
        "headlines": _try(marketdata.news, None, 15, 1),
        "calendar_effects": _calendar_effects(),
    }
    macro = team.macro_view(macro_ctx)
    print("macro:", macro.get("regime"), macro.get("risk_appetite"))

    # 3. Deep research on each finalist
    funds = _try(smartmoney.fund_holdings) or {}
    if not isinstance(funds, dict) or "unavailable" in funds:
        funds = {}
    rulebook = _rulebook()
    ideas, planned = [], []
    for cand in finalists:
        sym = cand["symbol"]
        try:
            d = dossiers.build(cand, funds)
            notes = team.specialists(d, macro)
            bull = team.bull(d, notes)
            bear = team.bear(d, notes, bull)
            pm = team.portfolio_manager(d, notes, bull, bear, macro, planned, holdings, rulebook)
            check = team.fact_check(pm, dossiers.facts(d), d)
            idea = {"symbol": sym, "venue": cand["venue"], "asset": cand["asset"],
                    "price_at_research": d["technicals"]["price"], "why_scanned": cand.get("why_scanned"),
                    "specialists": notes, "bull": bull, "bear": bear, "decision": pm, "fact_check": check}
            if pm.get("decision") in ("long", "short") and check["passed"]:
                kw = {}
                if cand["asset"] == "forex":
                    kw = {"quote_to_usd": quote_to_usd(sym), "unit_usd": oanda.usd_value_per_unit(sym)}
                idea["sizing"] = team.size(pm, book.equity, cand["asset"], **kw)
                idea["status"] = "approved"
                planned.append({"symbol": sym, "direction": pm["decision"], "style": pm.get("style"),
                                "bucket": pm.get("bucket")})
            else:
                idea["status"] = "pass" if pm.get("decision") == "pass" else "blocked_by_fact_check"
            ideas.append(idea)
            print(f"{sym}: {idea['status']} {pm.get('decision')} conv={pm.get('conviction')} {check['problems'][:2]}")
        except Exception as exc:
            traceback.print_exc()
            ideas.append({"symbol": sym, "status": "error", "error": str(exc)[:300]})

    plan = {
        "date": now().date().isoformat(), "session": session, "created_at": now().isoformat(timespec="seconds"),
        "dry_run": state.get("dry_run", True), "equity": round(book.equity, 2),
        "macro": macro, "macro_inputs": {k: macro_ctx[k] for k in ("economy", "calendar_next_10_days")},
        "ideas": ideas, "ai_calls": ai.calls_made, "minutes": round((time.monotonic() - started) / 60, 1),
    }
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    path = PLANS_DIR / f"{plan['date']}-{session}.json"
    path.write_text(json.dumps(plan, indent=1, default=str))
    telegram.send(brief(plan))
    return plan


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
            f"<b>{d['decision'].upper()} {esc(i['symbol'])}</b> | {esc(d.get('bucket'))} | {esc(d.get('style'))} | conviction {d.get('conviction')}/10",
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
