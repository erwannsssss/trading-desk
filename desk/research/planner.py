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

from desk import activity as floor
from desk import ai, config, datasources, marketdata, telegram
from desk.brokers import oanda
from desk.portfolio import snapshot
from desk.research import dossier as dossiers
from desk.research import extra as extra_data
from desk.research import kronos, quant, scanner, smartmoney, team
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
FULL_TEAM_MAX = 8             # AI budget: the full team (4+ AI calls each) takes the top 8 ideas; the rest get the
                              # one-call quick huddle, so the morning session can't burn most of the day's free quota


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
    t = learning._load(learning.TOURNAMENT, None) or {}
    tune = [f"{s if s != '_all' else 'all trades'}: " + " ".join(r.get("advice") or []) for s, r in (t.get("exit_tuning") or {}).items() if r.get("advice")]
    return {"session_note": SESSION_NOTES[session], "tournament": learning.tournament_summary(), "exit_tuning": tune,
            "backtest": learning.backtest_summary(), "team": team_record(),
            "shadow_lessons": t.get("shadow_lessons") or [], "edge_tilt": (t.get("edge_tilt") or {}).get("reason")}


def team_record() -> list[dict] | None:
    """Each research person's level and 30-day hit rate, so the PM can weigh senior, proven people more."""
    try:
        from desk import scorecard, staff
        from desk.dashboard import _curve
        from desk.trading import ledger
        scores = scorecard.seat_scores(ledger.load(), _curve())
        out = []
        for p in staff.org_chart():
            if p["desk"] != "research" or p.get("locked") or p["id"] in ("scanner", "associate", "factcheck", "sizer"):
                continue
            sc = scores.get(p["id"]) or {}
            out.append({"name": p["name"], "role": p["role"], "level": p.get("level"),
                        "hit_rate": f"{sc['pct']}% of {sc['total']}" if sc.get("pct") is not None else "not enough calls yet"})
        return out
    except Exception as exc:
        print(f"team record failed: {exc}")
        return None


def kronos_size(odds: dict | None, weight: float) -> float:
    """Size up a little when Kronos' simulated futures favor the plan, down when they're against it (scaled by trust)."""
    if not odds:
        return 1.0
    er = odds.get("expected_r") or 0
    return round(1 + max(-0.3, min(0.25, er * 0.5)) * weight, 2)


def _kronos_for(finalists: list[dict]) -> tuple[dict, float]:
    """Kronos forecasts for every finalist in one batch (an empty dict when the model isn't installed)."""
    try:
        if not kronos.available():
            return {}, 0.0
        floor.working("mlquant", f"Running Kronos on the {len(finalists)} finalists")
        stocks = [c["symbol"] for c in finalists if c["asset"] in ("stock", "etf")]
        cryptos = [c["symbol"] for c in finalists if c["asset"] == "crypto"]
        bars = {}
        if stocks:
            bars.update(marketdata.stock_daily_bars(stocks, days=900))
        if cryptos:
            bars.update(marketdata.crypto_daily_bars(cryptos, days=900))
        for c in finalists:
            if c["asset"] == "forex":
                bars[c["symbol"]] = marketdata.forex_daily_bars(c["symbol"], 700)
        series = {c["symbol"]: (bars.get(c["symbol"]) or [], c["asset"]) for c in finalists}
        fc = kronos.forecast(series, steps=5)
        w = quant.kronos_weight()
        quant._log_forecasts(fc, {c["symbol"]: {"asset": c["asset"], "t": c["technicals"]} for c in finalists})
        floor.done("mlquant", f"Kronos forecasts ready for {len(fc)} finalists (trust weight {w})")
        return fc, w
    except Exception as exc:
        traceback.print_exc()
        floor.done("mlquant", f"Kronos skipped this session ({str(exc)[:80]})")
        return {}, 0.0


def run(session: str = "morning") -> dict:
    started = time.monotonic()
    cfg = dict(SESSIONS[session])
    try:   # research budget follows the sessions whose ideas actually make money (learning.session_budget)
        from desk import learning
        m = ((learning._load(learning.TOURNAMENT, None) or {}).get("session_budget") or {}).get("multipliers", {}).get(session, 1.0)
        if m != 1.0:
            for k in ("stocks", "crypto", "forex"):
                if cfg[k]:
                    cfg[k] = max(1, round(cfg[k] * m))
    except Exception as exc:
        print(f"session budget failed: {exc}")
    from desk import firm
    if cfg["crypto"] and firm.unlocked("crypto_desk"):
        cfg["crypto"] += 1
    if cfg["forex"] and firm.unlocked("fx_desk"):
        cfg["forex"] += 1
    if cfg["stocks"] and firm.unlocked("event_desk"):
        cfg["stocks"] += 1
    floor.start_job("research", f"{session} research session")
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
    floor.working("scanner", f"Scanning stocks, ETFs, crypto and forex for the {session} session")
    stock_picks, stock_ctx = scanner.scan_stocks(cfg["stocks"] * 2, core_slots=0 if session == "intraday" else 5) if cfg["stocks"] else ([], {"etf_readings": {}, "scanned": 0})
    crypto_picks, crypto_ctx = scanner.scan_crypto(cfg["crypto"] * 2)
    fx_picks, fx_ctx = scanner.scan_forex(cfg["forex"] * 2)
    pick = lambda lst, n: [c for c in lst if c["symbol"] not in skip][:n]
    finalists = pick(stock_picks, cfg["stocks"]) + pick(crypto_picks, cfg["crypto"]) + pick(fx_picks, cfg["forex"])
    print(f"{session}: scanned {stock_ctx['scanned']} stocks/ETFs; {len(finalists)} finalists; skipped {len(skip)} recent")
    scope = f"{stock_ctx['scanned']} stocks/ETFs plus crypto and forex" if cfg["stocks"] else f"{len(scanner.CRYPTO)} cryptos and {len(scanner.FOREX)} currency pairs"
    floor.done("scanner", f"Scanned {scope}. Finalists: "
               + (", ".join(c["symbol"] for c in finalists) or "none"))

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
        "futures_positioning": _try(extra_data.cot),
        "crypto_sentiment": _try(extra_data.crypto_sentiment),
        "volatility_indexes": _try(extra_data.vol_indexes),
        "market_internals": _try(extra_data.market_internals, stock_ctx.get("analyzed") or [], stock_ctx["etf_readings"]),
        "spy_options": _try(extra_data.implied_move, "SPY") if cfg["stocks"] else None,
        "daily_trade_pace": quant._pace(state),
    }
    macro = latest_macro() if cfg["reuse_macro"] else None
    if macro:
        floor.event("macro", f"Sticking with the latest market read: {macro.get('regime')}, aggression {macro.get('risk_appetite')}/100")
    else:
        floor.working("macro", "Reading rates, inflation, the VIX, every ETF, overnight moves and the calendar")
        macro = team.macro_view(macro_ctx)
        floor.done("macro", f"Market regime: {macro.get('regime')}, aggression {macro.get('risk_appetite')}/100. {macro.get('summary', '')}")
    print("macro:", macro.get("regime"), macro.get("risk_appetite"))

    # 2b. Interns make their own picks (morning and evening); hired juniors pitch their best one to the full team
    if session in ("morning", "evening"):
        try:
            from desk import interns
            menu = pick(stock_picks, cfg["stocks"] * 2) + pick(crypto_picks, cfg["crypto"] * 2) + pick(fx_picks, cfg["forex"] * 2)
            for jp in interns.make_picks(menu, macro, session):
                if jp["symbol"] in {c["symbol"] for c in finalists}:
                    continue
                cand = next((c for c in menu if c["symbol"] == jp["symbol"]), None)
                if cand:
                    cand = {**cand, "why_scanned": (cand.get("why_scanned") or []) + [f"pitched by junior analyst {jp['intern']}: {jp['reason']}"]}
                    finalists.append(cand)
                    floor.event(f"intern:{jp['id']}", f"Pitched {jp['symbol']} ({jp['direction']}) to the full team", jp["symbol"], kind="plan")
        except Exception:
            traceback.print_exc()

    # 3. Research each finalist
    funds = _try(smartmoney.fund_holdings) or {}
    if not isinstance(funds, dict) or "unavailable" in funds:
        funds = {}
    rulebook = _rulebook()
    extra = extra_context(session)
    if stock_ctx.get("earnings_season"):
        extra["session_note"] += (" EARNINGS SEASON: " + ", ".join(stock_ctx["big_reporting"][:15]) + " report in the next few days. "
                                  "Favor earnings plays (positioning before reports, or trading the reaction after) and size "
                                  "them for the gap risk; read the latest press release closely.")
        floor.event("macro", f"Earnings season mode on: {len(stock_ctx['big_reporting'])} big names report in the next 3 days")
    planned = [{"symbol": i["symbol"], "direction": (i.get("decision") or {}).get("decision"),
                "style": (i.get("decision") or {}).get("style")} for _, i in recent_ideas(12) if i.get("status") == "approved"]
    ideas = []
    kz_all, kz_w = _kronos_for(finalists)
    for n, cand in enumerate(finalists):
        sym = cand["symbol"]
        fast = cfg["fast"] or n >= FULL_TEAM_MAX
        try:
            floor.working("associate", f"Pulling the dossier on {sym}: prices, fundamentals, insiders, funds, news", sym)
            d = dossiers.build(cand, funds, light=cfg["fast"])
            kz = kz_all.get(sym)
            if kz:
                d["kronos_forecast"] = {**kronos.public(kz), "trust_weight": kz_w}
            qn = team.quant_note(d)
            if qn:
                d["quant_desk"] = qn
                floor.event("quantdesk", f"{sym}: attached the backtested record of its setups", sym)
            floor.done("associate", f"Dossier ready on {sym}", sym)
            if fast:
                for a in floor.DESK_TEAM:
                    floor.working(a, f"Quick huddle on {sym}", sym)
                q = team.quick_desk(d, macro, planned, holdings, rulebook, extra)
                notes, bull, bear, pm = q.get("specialists"), q.get("bull"), q.get("bear"), q["decision"]
                _report_specialists(sym, notes)
                _report_debate(sym, bull, bear)
            else:
                for a in floor.SPECIALISTS:
                    floor.working(a, f"Analyzing {sym}", sym)
                notes = team.specialists(d, macro)
                roles = team.desk_roles(d)
                if roles:
                    for seat, _, _ in roles:
                        floor.working(seat, f"Writing the desk note on {sym}", sym)
                    try:
                        notes = {**(notes or {}), **team.desk_notes(d, macro)}
                    except Exception as exc:
                        print(f"desk notes failed: {exc}")
                _report_specialists(sym, notes)
                floor.working("bull", f"Building the case for {sym}", sym)
                bull = team.bull(d, notes)
                floor.done("bull", f"{sym}: argues {bull.get('direction', 'long')}. {bull.get('case', '')}", sym)
                floor.working("bear", f"Attacking the {bull.get('direction', 'long')} case on {sym}", sym)
                bear = team.bear(d, notes, bull)
                floor.done("bear", f"{sym}: {bear.get('case', '')}", sym)
                floor.working("pm", f"Pre-mortem and final call on {sym}", sym)
                pm = team.portfolio_manager(d, notes, bull, bear, macro, planned, holdings, rulebook, extra)
            expr = {"call_options": " with calls", "put_options": " with puts"}.get(pm.get("expression"), "")
            verdict = f"{pm.get('decision', '?').upper()}{expr}" + (f", conviction {pm.get('conviction')}/10" if pm.get("decision") != "pass" else "")
            floor.done("pm", f"{sym}: {verdict}. {pm.get('reason') or pm.get('thesis') or ''}", sym,
                       kind="decision" if pm.get("decision") in ("long", "short") else "pass")
            floor.working("factcheck", f"Checking every number Elena cited on {sym}", sym)
            check = team.fact_check(pm, dossiers.facts(d), d)
            if check["passed"]:
                floor.done("factcheck", f"{sym}: all {check['verified_facts']} cited numbers check out", sym)
            else:
                floor.done("factcheck", f"{sym}: blocked. {check['problems'][0]}", sym, kind="blocked")
            idea = {"symbol": sym, "venue": cand["venue"], "asset": cand["asset"],
                    "price_at_research": d["technicals"]["price"], "why_scanned": cand.get("why_scanned"),
                    "setups": d["technicals"].get("setups"), "atr_pct": d["technicals"].get("atr_pct"),
                    "specialists": notes, "bull": bull, "bear": bear, "decision": pm, "fact_check": check}
            if kz:
                idea["specialists"] = {**(notes or {}), "kronos": {"score": quant._kronos_score(kz),
                                       "points": [f"Kronos: {kz['expected_move_pct']:+.1f}% expected over {kz['horizon_days']} days, "
                                                  f"{kz['prob_up_pct']}% of simulated futures end higher"]}}
                notes = idea["specialists"]
                if pm.get("decision") in ("long", "short") and all(isinstance(pm.get(k), (int, float)) for k in ("entry", "stop", "target")):
                    try:
                        idea["kronos_odds"] = quant.kronos_r(kz["_paths"], pm["decision"] == "long", pm["entry"], pm["stop"], pm["target"])
                        ko = idea["kronos_odds"]
                        floor.event("mlquant", f"{sym}: Kronos has the target first in {ko['target_first_pct']}% of futures, "
                                    f"the stop first in {ko['stop_first_pct']}% (expected {ko['expected_r']:+.2f}R)", sym)
                    except Exception as exc:
                        print(f"kronos odds failed: {exc}")
            so = None
            if pm.get("decision") in ("long", "short") and check["passed"]:
                floor.working("second", f"Independent review of {sym} ({pm['decision']})", sym)
                try:
                    so = team.second_opinion(d, pm, macro)
                    idea["second_opinion"] = so
                    notes = {**(notes or {}), "second_opinion": {"score": so.get("score"), "points": [so.get("reason", "")]}}
                    idea["specialists"] = notes
                    word = {"agree": "AGREE, sizing up", "disagree": "DISAGREE, sizing down", "strong_disagree": "STRONG DISAGREE, vetoed"}[so["verdict"]]
                    floor.done("second", f"{sym}: {word}. {so.get('reason', '')}", sym,
                               kind="blocked" if so["verdict"] == "strong_disagree" else "decision")
                except Exception as exc:
                    floor.done("second", f"{sym}: couldn't review ({str(exc)[:80]}); trade goes ahead at normal size", sym)
            if so and so.get("verdict") == "strong_disagree" and not so.get("overruled"):
                idea["status"] = "vetoed_by_second_opinion"
            elif pm.get("decision") in ("long", "short") and check["passed"]:
                kw = {}
                if cand["asset"] == "forex":
                    kw = {"quote_to_usd": quote_to_usd(sym), "unit_usd": oanda.usd_value_per_unit(sym)}
                kw["multiplier"] = (learning.risk_multiplier(pm.get("style")) * ((so or {}).get("size_multiplier") or 1.0)
                                    * kronos_size(idea.get("kronos_odds"), kz_w))
                idea["sizing"] = team.size(pm, book.equity, cand["asset"], **kw)
                idea["status"] = "approved"
                sz = idea["sizing"]
                floor.done("sizer", f"{sym}: ${sz['notional_usd']:,.0f} position risking ${sz['risk_usd']:,.0f} ({sz['risk_pct']}% of the account)", sym)
                planned.append({"symbol": sym, "direction": pm["decision"], "style": pm.get("style")})
            else:
                idea["status"] = "pass" if pm.get("decision") == "pass" else "blocked_by_fact_check"
            ideas.append(idea)
            print(f"{sym}: {idea['status']} {pm.get('decision')} {pm.get('expression')} conv={pm.get('conviction')} {check['problems'][:2]}")
        except Exception as exc:
            traceback.print_exc()
            ideas.append({"symbol": sym, "venue": cand["venue"], "asset": cand["asset"], "status": "error", "error": str(exc)[:300]})
            for a in ("associate",) + floor.DESK_TEAM + ("factcheck",):
                if floor._state.get("agents", {}).get(a, {}).get("status") == "working":
                    floor.done(a, f"{sym}: research hit an error and was dropped", sym, kind="error")

    # 4. Pair trade (morning only): strongest vs weakest stock in one industry
    if session == "morning" and stock_ctx.get("analyzed"):
        try:
            from desk.research import pairs
            for lead, lag, ind, gap in pairs.candidates(stock_ctx["analyzed"], skip=set(active))[:1]:
                floor.working("pm", f"Pair trade: long {lead['symbol']} vs {lag['symbol']} in {ind} ({gap} pt gap over 20 days)")
                res = pairs.research(lead, lag, ind, gap, macro, extra)
                new = pairs.ideas_from(res, lead, lag, ind, gap, now().date().isoformat())
                for i in new:
                    if i["status"] == "approved":
                        i["sizing"] = team.size(i["decision"], book.equity, "stock", multiplier=0.6)
                ideas += new
                verdict = "PAIR" if any(i["status"] == "approved" for i in new) else "PASS"
                floor.done("pm", f"Pair trade {lead['symbol']}/{lag['symbol']} ({ind}): {verdict}. {res['decision'].get('reason') or ''}",
                           lead["symbol"], kind="decision" if verdict == "PAIR" else "pass")
        except Exception:
            traceback.print_exc()

    stamp = now()
    plan = {
        "earnings_season": bool(stock_ctx.get("earnings_season")),
        "date": stamp.date().isoformat(), "session": session, "created_at": stamp.isoformat(timespec="seconds"),
        "dry_run": state.get("dry_run", True), "equity": round(book.equity, 2),
        "macro": macro, "macro_inputs": {**{k: macro_ctx[k] for k in ("economy", "calendar_next_10_days")}, "etfs": macro_ctx["etfs"]},
        "ideas": ideas, "ai_calls": ai.calls_made, "ai_attempts": ai.attempts,
        "minutes": round((time.monotonic() - started) / 60, 1),
    }
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{plan['date']}-{session}" + (stamp.strftime("-%H%M") if session in ("intraday", "overnight") else "")
    path = PLANS_DIR / f"{name}.json"
    path.write_text(json.dumps(plan, indent=1, default=str))
    n_ok = sum(i.get("status") == "approved" for i in ideas)
    floor.event("pm", f"Sent the {session} plan: {n_ok} trade{'s' if n_ok != 1 else ''} out of {len(ideas)} researched", kind="plan")
    floor.finish(f"{session} session: {n_ok} approved of {len(ideas)}")
    if cfg["fast"]:
        text = quick_brief(plan)
        if text:
            telegram.send(text, silent=True)
    else:
        telegram.send(brief(plan))
    return plan


def _report_specialists(sym: str, notes: dict | None) -> None:
    notes = notes or {}
    for agent, key in zip(floor.SPECIALISTS, ("technical", "fundamental", "news_sentiment", "smart_money")):
        n = notes.get(key) or {}
        score = n.get("score")
        point = (n.get("points") or [""])[0]
        floor.done(agent, f"{sym}: {'+' if isinstance(score, int) and score > 0 else ''}{score if score is not None else 'n/a'}. {point}", sym)
    for seat, (key, _, _) in team.DESK_ROLES.items():
        n = notes.get(key)
        if isinstance(n, dict):
            score = n.get("score")
            floor.done(seat, f"{sym}: {'+' if isinstance(score, int) and score > 0 else ''}{score if score is not None else 'n/a'}. "
                       f"{(n.get('points') or [''])[0]}", sym)


def _report_debate(sym: str, bull: dict | None, bear: dict | None) -> None:
    bull, bear = bull or {}, bear or {}
    floor.done("bull", f"{sym}: argues {bull.get('direction', 'long')}. {bull.get('case', '')}", sym)
    floor.done("bear", f"{sym}: {bear.get('case', '')}", sym)


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
