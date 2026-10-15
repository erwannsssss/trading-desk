"""Breaking-news huddles.

Every tick (about every 15 minutes):
1. Open positions: if one moves sharply within the hour, or fresh headlines hit it, the portfolio manager,
   risk manager and news analyst huddle on it and decide: hold, close now, or tighten the stop.
2. Today's watchlist: a stock the desk researched today that has since moved 5%+ gets re-researched right away
   (fast one-call team); if approved, it's saved as an "alert" plan and the engine trades it like any other idea.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from desk import activity as floor
from desk import ai, config, marketdata, telegram
from desk.state import now
from desk.telegram import esc
from desk.trading import ledger

MOVE_TRIGGER = {"stock": 3.0, "etf": 2.0, "option": 3.0, "crypto": 5.0, "forex": 0.7}   # % within about an hour
WATCH_TRIGGER = 5.0          # % move since research for re-research
COOLDOWN = timedelta(minutes=90)
MAX_HUDDLES = 3
MAX_RESEARCH = 2


def _symbols_news(symbols: list[str], since: datetime) -> dict[str, list[dict]]:
    if not symbols:
        return {}
    try:
        items = marketdata.news(symbols, limit=50, days=1)
    except Exception:
        return {}
    out: dict[str, list[dict]] = {}
    for n in items:
        t = datetime.fromisoformat(n["time"] + ":00+00:00")
        if t <= since:
            continue
        for s in n.get("symbols", []):
            if s in symbols:
                out.setdefault(s, []).append(n)
    return out


def review_positions(state: dict, engine) -> list[str]:
    """Huddle on open positions that moved hard or got news. `engine` is a trading Engine (to act on decisions)."""
    from desk.trading.engine import current_price
    watch = state.setdefault("watch", {})
    at = now()
    trades = [t for t in engine.trades if t["status"] == "open"]
    stock_syms = [t["symbol"] for t in trades if t["asset"] in ("stock", "etf", "option")]
    oldest = min((datetime.fromisoformat(watch[t["id"]]["t"]) for t in trades if t["id"] in watch), default=at - timedelta(hours=1))
    news = _symbols_news(stock_syms, oldest)
    notes, huddles = [], 0
    for t in trades:
        try:
            px = current_price(t)
        except Exception:
            continue
        ref = watch.get(t["id"])
        if not ref:
            watch[t["id"]] = {"px": px, "t": at.isoformat(timespec="seconds")}
            continue
        ref_t = datetime.fromisoformat(ref["t"])
        move = (px / ref["px"] - 1) * 100
        fresh = [n for n in news.get(t["symbol"], []) if datetime.fromisoformat(n["time"] + ":00+00:00") > ref_t]
        last_huddle = datetime.fromisoformat(t["last_huddle"]) if t.get("last_huddle") else None
        trigger = abs(move) >= MOVE_TRIGGER.get(t["asset"], 3.0) or len(fresh) >= 2 or (fresh and abs(move) >= MOVE_TRIGGER.get(t["asset"], 3.0) / 2)
        if trigger and huddles < MAX_HUDDLES and (not last_huddle or at - last_huddle > COOLDOWN):
            huddles += 1
            notes.append(_huddle(t, px, move, fresh, engine))
            t["last_huddle"] = at.isoformat(timespec="seconds")
        if at - ref_t > timedelta(minutes=60) or trigger:
            watch[t["id"]] = {"px": px, "t": at.isoformat(timespec="seconds")}
    for k in [k for k in watch if k not in {t["id"] for t in trades}]:
        del watch[k]
    return [n for n in notes if n]


def _huddle(t: dict, px: float, move: float, fresh: list[dict], engine) -> str | None:
    sym = t["symbol"]
    for seat in ("pm", "risk", "sentiment"):
        floor.working(seat, f"Emergency huddle on {sym}: {move:+.1f}% in the last hour" + (f", {len(fresh)} new headlines" if fresh else ""), sym)
    long = t["direction"] == "long"
    prompt = f"""EMERGENCY HUDDLE. You are the portfolio manager, risk manager and news analyst of a trading desk, meeting
because an open position just moved sharply or got news. Decide fast and honestly.

POSITION: {json.dumps({k: t.get(k) for k in ("symbol", "asset", "direction", "entry_price", "stop", "target", "initial_stop",
    "horizon", "thesis", "option_symbol", "max_r")}, default=str)}
PRICE NOW: {px} ({move:+.2f}% in about the last hour)
NEW HEADLINES: {json.dumps([{"headline": n["headline"], "summary": n.get("summary")} for n in fresh][:6]) if fresh else "none"}

Choose one:
- "hold": the thesis still stands; this is noise
- "close": the thesis is broken (or a gain should be locked in now); exit at market
- "tighten": keep the trade but move the stop closer to lock in or limit loss (give new_stop; it must be
  {'above the current stop and below the price' if long else 'below the current stop and above the price'})

Return JSON: {{"action": "hold"|"close"|"tighten", "new_stop": number|null, "reason": str (1-2 sentences)}}"""
    try:
        out = ai.ask(prompt, want_json=True, temperature=0.2, models=config.AI_LIGHT)
    except Exception as exc:
        for seat in ("pm", "risk", "sentiment"):
            floor.done(seat, f"Huddle on {sym} failed: {str(exc)[:80]}", sym, kind="error")
        return None
    action, reason = out.get("action"), out.get("reason", "")
    result = "held"
    try:
        if action == "close":
            if t["asset"] in ("stock", "etf", "option") and not engine.stock_market_open():
                result = "wanted to close, but the market is closed; will revisit"
            else:
                engine._exit_now(t, f"emergency huddle: {reason}")
                result = "closing the position"
        elif action == "tighten" and isinstance(out.get("new_stop"), (int, float)):
            ns = float(out["new_stop"])
            ok = (long and t["stop"] < ns < px) or (not long and px < ns < t["stop"])
            if ok:
                if t["asset"] == "option":
                    t["stop"] = ns
                    ledger.stop_moved(t, ns, now().isoformat(timespec="seconds"))
                else:
                    engine._move_stop(t, ns)
                result = f"stop tightened to {ns}"
            else:
                result = "suggested a stop that wasn't valid; holding"
    except Exception as exc:
        result = f"tried to act but hit an error ({str(exc)[:80]})"
    ledger.note(t, f"emergency huddle ({move:+.1f}%): {action}. {reason}", now().isoformat(timespec="seconds"))
    floor.done("pm", f"Huddle on {sym} ({move:+.1f}%): {action.upper() if action else '?'}, {result}. {reason}", sym, kind="decision")
    floor.done("risk", f"Signed off on the {sym} huddle: {result}", sym)
    floor.done("sentiment", f"{sym}: " + (f"{len(fresh)} new headlines; top: {fresh[0]['headline'][:120]}" if fresh else "no news, price-driven move"), sym)
    return (f"<b>Emergency huddle: {esc(sym)}</b> moved {move:+.1f}% in about an hour"
            + (f" with {len(fresh)} new headline{'s' if len(fresh) > 1 else ''}" if fresh else "") + "\n"
            f"Decision: <b>{esc((action or '?').upper())}</b>, {esc(result)}.\n<i>{esc(reason)}</i>")


def research_movers(state: dict) -> list[str]:
    """Re-research today's watchlist names that moved 5%+ since the desk looked at them."""
    from desk.research import dossier as dossiers
    from desk.research import planner, team
    from desk.research.planner import PLANS_DIR, recent_ideas
    from desk import pitch
    from desk.portfolio import snapshot
    at = now()
    if not (9, 45) <= (at.hour, at.minute) <= (15, 20) or at.weekday() >= 5:
        return []
    seen = state.setdefault("alert_researched", {})
    active = ledger.active_symbols(ledger.load())
    cands = {}
    for plan, idea in recent_ideas(8):
        if idea.get("asset") in ("stock", "etf") and idea.get("price_at_research") and idea["symbol"] not in active:
            cands.setdefault(idea["symbol"], (plan, idea))
    if not cands:
        return []
    snaps = marketdata.stock_snapshots(list(cands))
    movers = []
    for sym, (plan, idea) in cands.items():
        px = (snaps.get(sym) or {}).get("price")
        if not px:
            continue
        move = (px / idea["price_at_research"] - 1) * 100
        last = seen.get(sym)
        if abs(move) >= WATCH_TRIGGER and (not last or at - datetime.fromisoformat(last) > timedelta(hours=3)):
            movers.append((abs(move), sym, move, idea))
    notes = []
    macro = planner.latest_macro(14) or {}
    book, _ = snapshot(state)
    for _, sym, move, idea in sorted(movers, reverse=True)[:MAX_RESEARCH]:
        seen[sym] = at.isoformat(timespec="seconds")
        floor.working("pm", f"{sym} moved {move:+.1f}% since this morning's research; pulling the team back in", sym)
        try:
            cand = pitch.candidate(sym, idea["asset"])
            cand["why_scanned"] = [f"breaking move: {move:+.1f}% since research"]
            d = dossiers.build(cand, {}, light=True)
            q = team.quick_desk(d, macro, [], list(active), planner._rulebook(), planner.extra_context("intraday"))
            pm = q["decision"]
            check = team.fact_check(pm, dossiers.facts(d), d)
            new = {"symbol": sym, "venue": "alpaca", "asset": idea["asset"], "price_at_research": d["technicals"]["price"],
                   "atr_pct": d["technicals"].get("atr_pct"), "why_scanned": cand["why_scanned"], "setups": d["technicals"].get("setups"),
                   "specialists": q.get("specialists"), "bull": q.get("bull"), "bear": q.get("bear"), "decision": pm, "fact_check": check}
            if pm.get("decision") in ("long", "short") and check["passed"]:
                new["sizing"] = team.size(pm, book.equity, idea["asset"])
                new["status"] = "approved"
            else:
                new["status"] = "pass" if pm.get("decision") == "pass" else "blocked_by_fact_check"
            plan = {"date": at.date().isoformat(), "session": "alert", "created_at": at.isoformat(timespec="seconds"),
                    "dry_run": state.get("dry_run", True), "equity": round(book.equity, 2), "macro": None, "ideas": [new]}
            PLANS_DIR.mkdir(parents=True, exist_ok=True)
            (PLANS_DIR / f"{plan['date']}-alert-{at.strftime('%H%M%S')}-{sym.replace('/', '')}.json").write_text(json.dumps(plan, indent=1, default=str))
            floor.done("pm", f"{sym} re-research after a {move:+.1f}% move: {pm.get('decision', '?').upper()}. {pm.get('reason') or ''}", sym,
                       kind="decision" if new["status"] == "approved" else "pass")
            if new["status"] == "approved":
                notes.append(f"<b>Breaking move: {esc(sym)}</b> {move:+.1f}% since research. The team re-ran it and wants to "
                             f"<b>{esc(pm['decision'].upper())}</b> (conviction {pm.get('conviction')}/10). <i>{esc(pm.get('thesis') or '')}</i>")
        except Exception as exc:
            floor.done("pm", f"Re-research on {sym} failed: {str(exc)[:100]}", sym, kind="error")
    return notes
