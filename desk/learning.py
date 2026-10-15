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


def update_tournament(trades: list[dict]) -> dict:
    t = compute_tournament(trades)
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
    return round(w * t.get("calibration", {}).get("factor", 1.0), 3)


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
    return ai.ask(prompt, want_json=True, temperature=0.2)


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
            t["postmortem"] = postmortem(t, memo)
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


# ---- Weekly review -----------------------------------------------------------
def weekly_review() -> dict:
    from desk.dashboard import _ghost, _recent_plans, _stats, _curve
    from desk.trading import ledger
    from desk import backtest

    try:
        backtest.run()
    except Exception as exc:
        print(f"backtest failed: {exc}")
    trades = ledger.load()
    tourney = update_tournament(trades)
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
    return out
