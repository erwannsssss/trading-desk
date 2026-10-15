"""Employee of the month, and the season-long rivalry between Jake (the bull) and Nadia (the bear).

Employee of the month: on the 1st, the person with the best hit rate (10+ graded calls) over the last 30 days wins.
Victor gives a short speech; the winner says thanks. Until the first award, the dashboard shows the live race.

The rivalry: every idea where Jake argued a direction is graded 3 trading days later. If the price went his way,
Jake wins the round; otherwise Nadia does. Weekly standings with a trash-talk line from the week's winner (Sunday),
and a season trophy each month.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from desk import ai, config, staff
from desk.state import now

FILE = config.DATA_DIR / "awards.json"
MIN_CALLS = 10


def load() -> dict:
    try:
        d = json.loads(FILE.read_text()) if FILE.exists() else {}
    except Exception:
        d = {}
    d.setdefault("eotm", [])
    d.setdefault("trophies", {"bull": 0, "bear": 0})
    d.setdefault("seasons", [])
    d.setdefault("weekly", [])
    return d


def save(d: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(d, indent=1, default=str) + "\n")


# ---- Employee of the month -----------------------------------------------------------
def race(scores: dict, top: int = 5) -> list[dict]:
    rows = []
    for p in staff.org_chart():
        if p["id"] == "partner" or p.get("locked"):
            continue
        sc = scores.get(p["id"]) or {}
        if sc.get("total", 0) >= 3:
            rows.append({"seat": p["id"], "name": p["name"], "role": p["role"], "right": sc["right"], "total": sc["total"],
                         "pct": round(sc["right"] / sc["total"] * 100), "qualified": sc["total"] >= MIN_CALLS})
    return sorted(rows, key=lambda r: (not r["qualified"], -r["pct"], -r["total"]))[:top]


def employee_of_the_month(month: str, label: str, scores: dict) -> dict | None:
    board = [r for r in race(scores, 20) if r["qualified"]]
    if not board:
        return None
    w = board[0]
    try:
        out = ai.ask(f"""Victor Ashford, CIO of a small AI trading firm, presents EMPLOYEE OF THE MONTH for {label}:
{w['name']} ({w['role']}), right on {w['right']} of {w['total']} graded calls ({w['pct']}%; a coin flip is 50%).
Runner-up: {json.dumps(board[1:3])}.
Write Victor's speech (2-3 warm, specific sentences, a little old-school) and the winner's one-line thank-you in
their own voice. Return JSON: {{"speech": str, "thanks": str}}""", want_json=True, temperature=0.7, models=config.AI_LIGHT)
    except Exception:
        out = {"speech": f"{w['name']} called it right {w['pct']}% of the time this month. Outstanding work.", "thanks": "Thank you, team."}
    rec = {**w, "month": month, "label": label, "speech": out.get("speech", ""), "thanks": out.get("thanks", ""),
           "date": now().date().isoformat()}
    d = load()
    d["eotm"] = [e for e in d["eotm"] if e["month"] != month] + [rec]
    save(d)
    return rec


# ---- Jake vs Nadia ---------------------------------------------------------------------
def rounds() -> list[dict]:
    """Every graded idea where Jake argued a direction: who won the round."""
    from desk import scorecard
    out = []
    for g in scorecard._load()["graded"].values():
        if g.get("bull") not in ("long", "short") or not g.get("move_pct"):
            continue
        jake = (g["bull"] == "long") == (g["move_pct"] > 0)
        out.append({"date": g["date"], "symbol": g["symbol"], "winner": "bull" if jake else "bear", "move_pct": g["move_pct"]})
    return sorted(out, key=lambda r: r["date"])


def _week(d: str) -> str:
    y, w, _ = date.fromisoformat(d).isocalendar()
    return f"{y}-W{w:02d}"


def standings() -> dict:
    rs = rounds()
    today = now().date()
    month, week = today.strftime("%Y-%m"), _week(today.isoformat())
    tally = lambda rows: {"bull": sum(r["winner"] == "bull" for r in rows), "bear": sum(r["winner"] == "bear" for r in rows)}
    d = load()
    return {"season": month, "this_season": tally([r for r in rs if r["date"].startswith(month)]),
            "this_week": tally([r for r in rs if _week(r["date"]) == week]), "all_time": tally(rs),
            "trophies": d["trophies"], "weekly": d["weekly"][-8:], "seasons": d["seasons"][-12:],
            "last_rounds": rs[-6:]}


def weekly_trash_talk() -> dict | None:
    """Sunday: the week's winner gets a trash-talk line."""
    rs = rounds()
    since = (now().date() - timedelta(days=7)).isoformat()
    week = [r for r in rs if r["date"] >= since]
    if not week:
        return None
    b, n = sum(r["winner"] == "bull" for r in week), sum(r["winner"] == "bear" for r in week)
    if b == n:
        winner = None
    else:
        winner = "bull" if b > n else "bear"
    w_name, l_name = (staff.name(winner), staff.name("bear" if winner == "bull" else "bull")) if winner else (None, None)
    try:
        line = ai.ask(f"""Weekly rivalry at a small trading desk: Jake the BULL vs Nadia the BEAR. This week Jake won {b}
rounds and Nadia won {n} (a round = an idea where the price went the way Jake argued, or didn't).
{f"Write ONE short, funny, workplace-appropriate trash-talk line from the winner, {w_name}, aimed at {l_name}." if winner
 else "It was a tie: write ONE short, funny line from each of them, in one sentence."}
Return JSON: {{"line": str}}""", want_json=True, temperature=0.9, models=config.AI_LIGHT).get("line", "")
    except Exception:
        line = ""
    rec = {"week": _week(now().date().isoformat()), "bull": b, "bear": n, "winner": winner, "line": line,
           "date": now().date().isoformat()}
    d = load()
    d["weekly"] = [x for x in d["weekly"] if x["week"] != rec["week"]] + [rec]
    save(d)
    return rec


def season_trophy(month: str) -> dict | None:
    rs = [r for r in rounds() if r["date"].startswith(month)]
    if not rs:
        return None
    b, n = sum(r["winner"] == "bull" for r in rs), sum(r["winner"] == "bear" for r in rs)
    winner = None if b == n else ("bull" if b > n else "bear")
    d = load()
    if any(s["month"] == month for s in d["seasons"]):
        return None
    if winner:
        d["trophies"][winner] = d["trophies"].get(winner, 0) + 1
    rec = {"month": month, "bull": b, "bear": n, "winner": winner}
    d["seasons"].append(rec)
    save(d)
    return rec


# ---- Hooks -----------------------------------------------------------------------------
def maybe_monthly(state: dict) -> list[str]:
    """Days 1-3 of a month, after 8am: Employee of the month and the rivalry season trophy. Returns Telegram lines."""
    from desk import scorecard
    from desk.dashboard import _curve
    from desk.telegram import esc
    from desk.trading import ledger
    at = now()
    last = at.replace(day=1) - timedelta(days=1)
    month, label = last.strftime("%Y-%m"), last.strftime("%B %Y")
    if at.day > 3 or at.hour < 8 or state.get("awards_month") == month:
        return []
    state["awards_month"] = month
    msgs = []
    rec = employee_of_the_month(month, label, scorecard.seat_scores(ledger.load(), _curve()))
    if rec:
        msgs.append(f"<b>Employee of the month, {esc(label)}: {esc(rec['name'])}</b> ({esc(rec['role'])})\n"
                    f"{rec['pct']}% right on {rec['total']} calls.\n<i>Victor: {esc(rec['speech'])}</i>\n{esc(rec['name'].split()[0])}: “{esc(rec['thanks'])}”")
    t = season_trophy(month)
    if t:
        who = staff.name(t["winner"]) if t["winner"] else "Nobody (a tie)"
        msgs.append(f"<b>Bull vs. bear, {esc(label)} season:</b> Jake {t['bull']}, Nadia {t['bear']}. Trophy: {esc(who)}.")
    return msgs
