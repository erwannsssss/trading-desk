"""How the desk learns from its own results.

- post-mortem on every closed trade: was the thesis right, or was it luck?
- evening recap on Telegram after the US close
- weekly review: a fund letter, new rules for the rulebook, and an updated strategy tournament
- strategy tournament: styles that make money get bigger bets, styles that lose get smaller ones
- confidence calibration: if its "70% confident" trades win far less than 70%, all bets shrink
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from statistics import mean

from desk import ai, config, telegram
from desk.state import now
from desk.telegram import esc

TOURNAMENT = config.DATA_DIR / "tournament.json"
RULEBOOK = config.DATA_DIR / "rulebook.json"
LETTERS = config.DATA_DIR / "letters.json"
BACKTEST = config.DATA_DIR / "backtest.json"
MAX_RULES = 30


def _load(path, default):
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except Exception:
        return default


def _save(path, data) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, default=str) + "\n")


# ---- Strategy tournament and calibration ---------------------------------------
def compute_tournament(trades: list[dict], days: int = 45) -> dict:
    cutoff = (now() - timedelta(days=days)).isoformat()
    closed = [t for t in trades if t.get("status") == "closed" and t.get("pnl_usd") is not None
              and (t.get("closed_at") or "") >= cutoff]
    styles = {}
    for t in closed:
        styles.setdefault((t.get("style") or "other").lower(), []).append(t)
    board = []
    for style, ts in styles.items():
        rs = [t["r_result"] for t in ts if t.get("r_result") is not None]
        avg_r = mean(rs) if rs else 0.0
        weight = 1.0 if len(ts) < 3 else max(0.5, min(1.5, 1 + 0.3 * avg_r))
        board.append({"style": style, "trades": len(ts), "win_rate": round(sum(t["pnl_usd"] > 0 for t in ts) / len(ts) * 100),
                      "avg_r": round(avg_r, 2), "pnl": round(sum(t["pnl_usd"] for t in ts), 2), "weight": round(weight, 2)})
    board.sort(key=lambda b: -b["pnl"])

    calibrated = [t for t in closed if t.get("confidence_pct") is not None]
    calibration = {"trades": len(calibrated), "factor": 1.0}
    if calibrated:
        predicted = mean(t["confidence_pct"] for t in calibrated)
        actual = sum(t["pnl_usd"] > 0 for t in calibrated) / len(calibrated) * 100
        calibration.update(predicted=round(predicted), actual=round(actual))
        if len(calibrated) >= 8:
            if actual < predicted - 12:
                calibration["factor"] = 0.8   # overconfident: shrink every bet
            elif actual > predicted + 12:
                calibration["factor"] = 1.1   # underconfident: lean in a little
    return {"updated": now().isoformat(timespec="minutes"), "window_days": days, "board": board, "calibration": calibration}


EDGE_MIN_TRADES = 30


def edge_tilt(trades: list[dict]) -> dict:
    """Once there are 30+ closed trades, scale every bet by how much real edge the results show.
    Proven edge -> bigger bets (up to 1.2x); losing record -> smaller bets (down to 0.7x)."""
    from desk import edge
    closed = sorted([t for t in trades if t.get("status") == "closed" and t.get("r_result") is not None],
                    key=lambda t: t.get("closed_at") or "")[-60:]
    rs = [t["r_result"] for t in closed]
    if len(rs) < EDGE_MIN_TRADES:
        return {"factor": 1.0, "reason": f"{len(rs)} of {EDGE_MIN_TRADES} closed trades needed before results change bet sizes"}
    s = edge.trade_tests(rs)
    avg, p, tstat = s.get("avg_r", 0), s.get("p_value", 1), s.get("t_stat", 0)
    if avg > 0 and p < 0.10:
        f, why = 1.2, f"proven edge: {avg:+.2f}R a trade over the last {len(rs)} (only a {p:.0%} chance that's luck)"
    elif avg > 0 and p < 0.25:
        f, why = 1.1, f"probably an edge: {avg:+.2f}R a trade over the last {len(rs)}"
    elif avg < 0 and (tstat or 0) < -1:
        f, why = 0.7, f"losing record: {avg:+.2f}R a trade over the last {len(rs)}; betting smaller until it turns"
    elif avg < 0:
        f, why = 0.85, f"slightly negative: {avg:+.2f}R a trade over the last {len(rs)}"
    else:
        f, why = 1.0, f"no clear edge either way yet ({avg:+.2f}R a trade over the last {len(rs)})"
    return {"factor": f, "reason": why, "n": len(rs), "avg_r": avg, "p_value": p}


def shadow_lessons() -> list[str]:
    """Simple rules that are clearly beating the desk's own picks (shown to the PM before every decision)."""
    try:
        from desk import shadow
        sm = shadow.summary()
    except Exception:
        return []
    out = []
    for k, e in (sm.get("strategies") or {}).items():
        vs = e.get("desk_vs")
        if k in ("desk", "coin_flip") or not vs or (e.get("stats") or {}).get("n", 0) < 20:
            continue
        if vs.get("diff_r", 0) < 0 and (vs.get("p_value") or 0) > 0.9:
            out.append(f"The simple rule \"{e['label']}\" has beaten the desk's own picks by {-vs['diff_r']:.2f}R a trade "
                       f"over {e['stats']['n']} ideas. Ask why before overruling what it would do.")
    return out


# ---- Auto-tuning exits ---------------------------------------------------------------
TUNE_MIN_TRADES = 15     # per style; styles with fewer use the all-trades numbers once there are 30


def _pct(vals: list[float], q: float) -> float:
    v = sorted(vals)
    if not v:
        return 0.0
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _tune(ts: list[dict]) -> dict | None:
    rows = []
    for t in ts:
        e, s, g = t.get("entry_price"), t.get("initial_stop"), t.get("target")
        if not (e and s and g) or t.get("max_r") is None or e == s:
            continue
        rows.append({"planned": abs(g - e) / abs(e - s), "max_r": t["max_r"], "min_r": t.get("min_r"),
                     "win": (t.get("pnl_usd") or 0) > 0})
    if len(rows) < TUNE_MIN_TRADES:
        return None
    planned = _pct([r["planned"] for r in rows], 0.5)
    reach = _pct([r["max_r"] for r in rows], 0.75)      # how far the better trades actually ran
    dips = [r["min_r"] for r in rows if r["win"] and r["min_r"] is not None]
    out = {"n": len(rows), "planned_target_r": round(planned, 2), "p75_max_r": round(reach, 2), "advice": []}
    if planned > reach * 1.15 and reach >= 1.2:
        out["target_r"] = round(max(1.2, _pct([r["max_r"] for r in rows], 0.65)), 2)
        out["advice"].append(f"targets are too far: only a quarter of trades ran past {reach:.1f}R, but targets sit at "
                             f"{planned:.1f}R. Aim for about {out['target_r']}R.")
    if len(dips) >= 8:
        deep = abs(_pct(dips, 0.1))                      # 90% of winners never dipped deeper than this
        out["winners_deepest_dip_r"] = round(deep, 2)
        if deep < 0.5:
            out["stop_scale"] = round(max(0.6, deep + 0.25), 2)
            out["advice"].append(f"stops are wider than needed: 90% of winners never dipped past {deep:.2f}R. "
                                 f"Stops about {round((1 - out['stop_scale']) * 100)}% closer would cut losses without losing many winners.")
    return out


def exit_tuning(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("status") == "closed" and t.get("asset") != "option" and t.get("style") != "hedge"]
    out = {}
    by_style: dict[str, list] = {}
    for t in closed:
        by_style.setdefault((t.get("style") or "other").lower(), []).append(t)
    for style, ts in by_style.items():
        r = _tune(ts)
        if r:
            out[style] = r
    if len(closed) >= 2 * TUNE_MIN_TRADES:
        r = _tune(closed)
        if r:
            out["_all"] = r
    return out


def tuned_levels(style: str | None, direction: str, entry: float, stop: float, target: float) -> tuple[float, float, str | None]:
    """Apply the learned stop and target distances to a new trade. Returns (stop, target, note)."""
    t = _load(TOURNAMENT, None) or {}
    tune = (t.get("exit_tuning") or {})
    rule = tune.get((style or "").lower()) or tune.get("_all")
    if not rule or entry == stop:
        return stop, target, None
    sign = 1 if direction == "long" else -1
    risk = abs(entry - stop)
    notes = []
    if rule.get("stop_scale"):
        risk *= rule["stop_scale"]
        stop = entry - sign * risk
        notes.append(f"stop {round((1 - rule['stop_scale']) * 100)}% closer")
    want = rule.get("target_r")
    if want and abs(target - entry) / risk > want * 1.15:
        target = entry + sign * want * risk
        notes.append(f"target at {want}R")
    return round(stop, 6), round(target, 6), ("auto-tuned: " + ", ".join(notes)) if notes else None


# ---- Research budget by session ------------------------------------------------------
SESSION_MIN_TRADES = 10


def session_budget(trades: list[dict]) -> dict:
    """Which research sessions' ideas make money. Once 2+ sessions have 10+ closed trades, the best gets 30% more
    ideas researched and the worst 30% fewer."""
    by: dict[str, list[float]] = {}
    for t in trades:
        if t.get("status") != "closed" or t.get("r_result") is None or not t.get("plan_file"):
            continue
        parts = t["plan_file"].removesuffix(".json").split("-")
        session = parts[3] if len(parts) > 3 else "other"
        by.setdefault(session, []).append(t["r_result"])
    rows = {s: {"trades": len(v), "avg_r": round(mean(v), 3), "win_rate": round(sum(r > 0 for r in v) / len(v) * 100)}
            for s, v in by.items()}
    ready = sorted([s for s, r in rows.items() if r["trades"] >= SESSION_MIN_TRADES and s in ("morning", "intraday", "evening", "overnight")],
                   key=lambda s: rows[s]["avg_r"])
    mult = {s: 1.0 for s in ready}
    if len(ready) >= 2:
        mult[ready[-1]] = 1.3
        if rows[ready[0]]["avg_r"] < rows[ready[-1]]["avg_r"]:
            mult[ready[0]] = 0.7
    for s, m in mult.items():
        rows[s]["budget"] = m
    return {"sessions": rows, "multipliers": mult}


def update_tournament(trades: list[dict]) -> dict:
    t = compute_tournament(trades)
    t["exit_tuning"] = exit_tuning(trades)
    t["session_budget"] = session_budget(trades)
    t["edge_tilt"] = edge_tilt(trades)
    t["shadow_lessons"] = shadow_lessons()
    _save(TOURNAMENT, t)
    return t


def tournament_summary() -> list | None:
    t = _load(TOURNAMENT, None)
    return t["board"] if t and t.get("board") else None


def risk_multiplier(style: str | None) -> float:
    t = _load(TOURNAMENT, None)
    if not t:
        return 1.0
    w = next((b["weight"] for b in t.get("board", []) if b["style"] == (style or "").lower()), 1.0)
    tilt = (t.get("edge_tilt") or {}).get("factor", 1.0)
    return round(max(0.4, min(1.8, w * t.get("calibration", {}).get("factor", 1.0) * tilt)), 3)


def backtest_summary() -> list | None:
    b = _load(BACKTEST, None)
    if not b:
        return None
    return [{k: r[k] for k in ("setup", "direction", "trades", "win_rate", "avg_r")} for r in b.get("results", [])]


def rulebook() -> list[dict]:
    return _load(RULEBOOK, [])


# ---- Post-mortems ------------------------------------------------------------
def postmortem(trade: dict, memo: dict | None) -> dict:
    prompt = f"""A trade just closed. Grade it honestly, like a head trader reviewing a junior's trade.

TRADE: {json.dumps({k: trade.get(k) for k in ("symbol", "asset", "direction", "style", "bucket", "conviction", "confidence_pct",
    "horizon", "entry_price", "initial_stop", "target", "exit_price", "exit_reason", "pnl_usd", "r_result", "max_r", "notes",
    "thesis", "option_symbol")}, default=str)}
ORIGINAL RESEARCH: {json.dumps({k: (memo or {}).get(k) for k in ("bull", "bear", "decision")}, default=str)[:6000]}

Questions: Did the thesis play out? Did it win for the right reason or get lucky? Did it lose because the idea was
wrong, the timing/stop was bad, or just bad luck? max_r is how far it went in our favor at best (in units of risk).

Return JSON: {{"verdict": "right and won"|"right but lost"|"wrong but won (lucky)"|"wrong and lost",
"what_happened": str (2-3 sentences), "lesson": str (one specific, reusable lesson, or "" if none)}}"""
    return ai.ask(prompt, want_json=True, temperature=0.2, models=config.AI_LIGHT)


def run_postmortems(limit: int = 3) -> int:
    """Grade closed trades that haven't been reviewed yet (a few per run to spare the AI quota)."""
    from desk.dashboard import _recent_plans
    from desk.research.planner import PLANS_DIR
    from desk.trading import ledger
    trades = ledger.load()
    todo = [t for t in trades if t.get("status") == "closed" and not t.get("postmortem")][:limit]
    for t in todo:
        memo = None
        path = PLANS_DIR / t.get("plan_file", "")
        if path.exists():
            plan = json.loads(path.read_text())
            memo = next((i for i in plan.get("ideas", []) if i.get("symbol") == t["symbol"]), None)
        try:
            from desk import activity as floor
            floor.working("reviewer", f"Reviewing the closed {t['symbol']} trade", t["symbol"])
            t["postmortem"] = postmortem(t, memo)
            pm = t["postmortem"]
            floor.done("reviewer", f"{t['symbol']} ({t.get('pnl_usd', 0):+,.2f}): {pm.get('verdict')}. {pm.get('lesson') or pm.get('what_happened', '')}", t["symbol"])
        except Exception as exc:
            t["postmortem"] = {"verdict": "not reviewed", "what_happened": f"review failed: {str(exc)[:100]}", "lesson": ""}
    if todo:
        ledger.save(trades)
    return len(todo)


# ---- Evening recap -----------------------------------------------------------
def maybe_send_recap(state: dict, book) -> None:
    at = now()
    if at.weekday() >= 5 or (at.hour, at.minute) < (16, 10) or state.get("recap_sent") == at.date().isoformat():
        return
    from desk.trading import ledger
    today = at.date().isoformat()
    trades = ledger.load()
    closed = [t for t in trades if t.get("status") == "closed" and (t.get("closed_at") or "")[:10] == today]
    opened = [t for t in trades if (t.get("opened_at") or "")[:10] == today]
    open_now = [t for t in trades if t.get("status") == "open"]
    day_pnl = book.equity - (state.get("day_start_equity") or book.equity)
    lines = [f"<b>Evening recap, {today}</b>",
             f"Account: ${book.equity:,.2f} ({day_pnl:+,.2f} today, {(book.equity / config.STARTING_CAPITAL - 1) * 100:+.2f}% overall)",
             f"Opened {len(opened)} trades, closed {len(closed)}, holding {len(open_now)}."]
    if closed:
        best = max(closed, key=lambda t: t["pnl_usd"])
        worst = min(closed, key=lambda t: t["pnl_usd"])
        lines += ["", "<b>Closed today</b>"] + [
            f"{esc(t['symbol'])}: ${t['pnl_usd']:+,.2f} ({esc(t.get('exit_reason'))})" for t in closed[:12]]
        if len(closed) > 1:
            lines.append(f"Best: {esc(best['symbol'])} ${best['pnl_usd']:+,.2f}. Worst: {esc(worst['symbol'])} ${worst['pnl_usd']:+,.2f}.")
    if open_now:
        lines += ["", "<b>Holding overnight</b>"] + [f"{esc(t['symbol'])} {t['direction']}" for t in open_now[:12]]
    telegram.send("\n".join(lines))
    state["recap_sent"] = today
    from desk import activity as floor
    floor.event("client", f"Sent your evening recap: account ${book.equity:,.2f}, {len(closed)} trades closed today")


# ---- Weekly review -----------------------------------------------------------
def weekly_review() -> dict:
    from desk.dashboard import _ghost, _recent_plans, _stats, _curve
    from desk.trading import ledger
    from desk import backtest

    from desk import activity as floor
    floor.working("quant", "Backtesting every chart setup on 2 years of data")
    try:
        bt = backtest.run()
        best = (bt.get("results") or [{}])[0]
        floor.done("quant", f"Backtest done on {bt.get('symbols')} symbols. Best setup: {best.get('setup')} {best.get('direction')} "
                   f"({best.get('win_rate')}% wins, {best.get('avg_r')}R avg)")
    except Exception as exc:
        print(f"backtest failed: {exc}")
        floor.done("quant", f"Backtest failed: {str(exc)[:120]}", kind="error")
    trades = ledger.load()
    floor.working("performance", "Re-scoring the strategy tournament")
    tourney = update_tournament(trades)
    floor.done("performance", f"Tournament updated: {len(tourney.get('board', []))} styles scored")
    floor.working("cio", "Writing the weekly letter and updating the rulebook")
    week_ago = (now() - timedelta(days=7)).isoformat()
    week = [t for t in trades if (t.get("closed_at") or t.get("created_at") or "") >= week_ago]
    closed = [t for t in week if t.get("status") == "closed"]
    stats = _stats(trades, _curve())
    ghost = _ghost(_recent_plans(8), trades)[:40]
    rules = rulebook()
    curve = _curve()
    start_eq = next((p["equity"] for p in curve if p["t"] >= week_ago[:16]), config.STARTING_CAPITAL)
    end_eq = curve[-1]["equity"] if curve else config.STARTING_CAPITAL
    spy_start = next((p["spy"] for p in curve if p["t"] >= week_ago[:16]), None)
    spy_end = curve[-1]["spy"] if curve else None

    prompt = f"""You run a small AI trading desk ($10,000 paper account) and write its weekly letter to the one investor,
Erwann, a first-year finance student. Then you improve the desk's rulebook.

THIS WEEK: account ${start_eq:,.2f} -> ${end_eq:,.2f}; S&P 500 equivalent {spy_start} -> {spy_end}.
CLOSED TRADES THIS WEEK (with post-mortems): {json.dumps([{k: t.get(k) for k in ("symbol", "asset", "direction", "style", "bucket",
    "conviction", "confidence_pct", "pnl_usd", "r_result", "exit_reason", "postmortem")} for t in closed], default=str)[:12000]}
OTHER ACTIVITY: {len(week)} trades planned/placed this week; {sum(t.get('status') == 'skipped' for t in week)} skipped by the engine.
ALL-TIME STATS: {json.dumps({k: v for k, v in stats.items() if k != 'calibration'}, default=str)[:3000]}
STRATEGY TOURNAMENT (last 45 days): {json.dumps(tourney, default=str)[:3000]}
PASSED IDEAS AND HOW THEY MOVED (positive lean_result_pct means the skipped idea would have worked): {json.dumps(ghost, default=str)[:4000]}
BACKTEST OF CHART SETUPS: {json.dumps(backtest_summary(), default=str)[:2500]}
CURRENT RULEBOOK: {json.dumps([r['rule'] for r in rules])}

Write:
1. letter: a hedge-fund-style weekly letter in plain English (250-400 words): returns vs the S&P 500, best and worst
   trades and why, what worked and what didn't, whether the desk was too cautious (see passed ideas) or too loose,
   and the plan for next week. Honest, no hype.
2. new_rules: 0-3 specific, testable rules learned from real results this week (e.g. "Don't buy breakouts in
   small caps on risk-off days"). Only add a rule the evidence supports.
3. retire_rules: exact text of any current rules the evidence says are hurting performance.

Return JSON: {{"title": str, "letter": str, "new_rules": [str], "retire_rules": [str]}}"""
    out = ai.ask(prompt, want_json=True, temperature=0.4)

    today = now().date().isoformat()
    rules = [r for r in rules if r["rule"] not in set(out.get("retire_rules") or [])]
    for rule in (out.get("new_rules") or [])[:3]:
        if rule and rule not in {r["rule"] for r in rules}:
            rules.append({"rule": rule, "date": today, "source": "weekly review"})
    _save(RULEBOOK, rules[-MAX_RULES:])
    letters = _load(LETTERS, [])
    letters.append({"date": today, "title": out.get("title") or f"Week ending {today}", "body": out.get("letter", "")})
    _save(LETTERS, letters[-52:])

    msg = [f"<b>{esc(out.get('title') or 'Weekly letter')}</b>", "", esc(out.get("letter", ""))]
    if out.get("new_rules"):
        msg += ["", "<b>New rules</b>"] + [f"- {esc(r)}" for r in out["new_rules"][:3]]
    if out.get("retire_rules"):
        msg += ["", "<b>Retired rules</b>"] + [f"- {esc(r)}" for r in out["retire_rules"]]
    telegram.send("\n".join(msg))
    from desk import awards, hr
    for step in (hr.trade_of_the_week, hr.weekly_reviews):
        try:
            step()
        except Exception as exc:
            print(f"{step.__name__} failed: {exc}")
    try:
        r = awards.weekly_trash_talk()
        if r:
            who = "Jake" if r["winner"] == "bull" else "Nadia" if r["winner"] == "bear" else "Nobody"
            telegram.send(f"<b>Bull vs. bear this week:</b> Jake {r['bull']}, Nadia {r['bear']}. {who} takes the week.\n<i>{esc(r['line'])}</i>")
            floor.event(r["winner"] or "bull", f"Rivalry: Jake {r['bull']}, Nadia {r['bear']} this week. {r['line']}", kind="award")
    except Exception as exc:
        print(f"rivalry failed: {exc}")
    floor.done("cio", f"Sent the weekly letter \"{out.get('title', '')}\"; {len(out.get('new_rules') or [])} new rules, "
               f"{len(out.get('retire_rules') or [])} retired")
    return out
