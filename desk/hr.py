"""Ashley Brennan, Head of HR: weekly performance reviews, firing and hiring, the monthly season recap,
and the trade of the week card."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from desk import ai, config, staff, telegram
from desk import activity as floor
from desk.state import now
from desk.telegram import esc

REVIEWS = config.DATA_DIR / "reviews.json"
RECAPS = config.DATA_DIR / "recaps.json"
CARDS = config.DATA_DIR / "cards.json"
CARD_DIR = config.ROOT / "docs" / "cards"
FIRE_MIN_CALLS = 20      # graded calls in the last 30 days before anyone can be fired
FIRE_BELOW_PCT = 42      # hit rate that gets you fired (a coin flip is 50%)
GRACE_DAYS = 30          # new hires can't be fired in their first month


def _load(path, default):
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except Exception:
        return default


def _save(path, data) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, default=str) + "\n")


def _context() -> dict:
    from desk import interns, scorecard
    from desk.dashboard import _curve
    from desk.trading import ledger
    trades = ledger.load()
    scores = scorecard.seat_scores(trades, _curve())
    people = []
    for p in staff.org_chart():
        if p["id"] == "partner":
            continue
        sc = scores.get(p["id"])
        people.append({"seat": p["id"], "name": p["name"], "role": p["role"], "desk": p["desk"],
                       "since": p.get("since"), "score": sc})
    for i in interns.all_interns():
        if i["status"] in ("intern", "junior"):
            people.append({"seat": f"intern:{i['id']}", "name": i["name"], "role": "Junior analyst" if i["status"] == "junior" else "Intern",
                           "persona": i["persona"], "equity": i.get("equity"), "started_with": 150})
    return {"people": people, "trades": trades, "scores": scores}


# ---- Weekly performance reviews, firing and hiring ---------------------------
def weekly_reviews() -> dict:
    ctx = _context()
    floor.working("hr", "Writing everyone's weekly performance review")
    prompt = f"""You are Ashley Brennan, Head of HR at a small AI trading desk. Write this week's performance reviews.
Your voice: warm but blunt, corporate-HR phrasing with dry humor ("let's circle back on your stop placement").
Grade each person A-F from their numbers. "score.pct" is how often their calls were right over the last 30 days
(50% is a coin flip); null means not enough data yet, so grade on effort and keep it short. Interns are judged
on their pretend account (they started with $150; $500 gets them hired).

PEOPLE: {json.dumps(ctx['people'], default=str)[:14000]}

Return JSON: {{"memo": str (2-3 sentence all-staff memo), "reviews": [{{"seat": str, "grade": str,
"review": str (1-2 sentences, specific to their numbers)}}]}}"""
    out = ai.ask(prompt, want_json=True, temperature=0.6)

    fired = []
    for p in ctx["people"]:
        sc, seat = p.get("score") or {}, p["seat"]
        if seat.startswith("intern:") or not staff.SEATS.get(seat, (0, 0, 0, 0, False))[4]:
            continue
        since = p.get("since")
        new_hire = since and datetime.fromisoformat(since) > now().replace(tzinfo=None) - timedelta(days=GRACE_DAYS)
        if sc.get("total", 0) >= FIRE_MIN_CALLS and (sc.get("right", 0) / sc["total"] * 100) < FIRE_BELOW_PCT and not new_hire:
            fired.append(_fire(seat, p, sc))

    record = {"date": now().date().isoformat(), "memo": out.get("memo", ""),
              "reviews": {r["seat"]: {"grade": r.get("grade"), "review": r.get("review")} for r in out.get("reviews", []) if r.get("seat")},
              "fired": fired}
    history = _load(REVIEWS, [])
    history.append(record)
    _save(REVIEWS, history[-26:])

    lines = ["<b>HR memo from Ashley Brennan</b>", "", esc(record["memo"]), ""]
    names = {p["seat"]: p["name"] for p in ctx["people"]}
    for seat, r in record["reviews"].items():
        lines.append(f"<b>{esc(names.get(seat, seat))}</b>: {esc(r['grade'])}. {esc(r['review'])}")
    for f in fired:
        lines += ["", f"<b>Staffing change:</b> {esc(f['old'])} ({esc(f['role'])}) has been let go after a {f['pct']}% hit rate "
                      f"on {f['total']} calls. Welcome {esc(f['new'])}, who brings a new approach: {esc(f['approach'])}"]
    telegram.send("\n".join(lines))
    floor.done("hr", f"Sent weekly reviews for {len(record['reviews'])} people" + (f"; let go {len(fired)}" if fired else ""))
    return record


def _fire(seat: str, person: dict, sc: dict) -> dict:
    pct = round(sc["right"] / sc["total"] * 100)
    role = staff.SEATS[seat][1]
    try:
        out = ai.ask(f"""A trading desk is replacing its {role}, {person['name']}, whose calls were right only {pct}% of the
time over {sc['total']} calls (a coin flip is 50%). Describe, in 1-2 sentences, a clearly different working approach
the replacement should use for this role, aimed at fixing what likely went wrong. Be concrete (what they weigh, what
they ignore, when they stay neutral). Return JSON: {{"approach": str}}""", want_json=True, temperature=0.7)
        approach = out.get("approach") or "More selective: stays neutral unless the evidence clearly points one way."
    except Exception:
        approach = "More selective: stays neutral unless the evidence clearly points one way."
    old, new = staff.fire_and_replace(seat, f"{pct}% hit rate on {sc['total']} calls", approach)
    floor.event("hr", f"Let go {old} ({role}) after a {pct}% hit rate. Hired {new}: {approach}", kind="fire")
    floor.event("cio", f"Signed off on replacing {old} with {new} as {role}", kind="fire")
    return {"seat": seat, "role": role, "old": old, "new": new, "pct": pct, "total": sc["total"], "approach": approach}


# ---- Trade of the week card --------------------------------------------------
def trade_of_the_week() -> dict | None:
    from desk import cards
    from desk.trading import ledger
    week_ago = (now() - timedelta(days=7)).isoformat()
    closed = [t for t in ledger.load() if t.get("status") == "closed" and t.get("pnl_usd") is not None
              and (t.get("closed_at") or "") >= week_ago]
    if not closed:
        return None
    best = max(closed, key=lambda t: t["pnl_usd"])
    start = (now() - timedelta(days=6)).strftime("%b %-d")
    label = f"Week of {start} to {now().strftime('%b %-d, %Y')}"
    called_by = f"{staff.name('options' if best.get('asset') == 'option' else 'pm')}, " + \
        ("options trader" if best.get("asset") == "option" else "portfolio manager")
    argued_by = f"{staff.name('bull' if best.get('direction') == 'long' else 'bear')}, " + \
        ("the bull" if best.get("direction") == "long" else "the bear")
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{now().date().isoformat()}-{best['symbol'].replace('/', '')}.png"
    cards.trade_card(best, label, called_by, argued_by, CARD_DIR / fname)
    entry = {"date": now().date().isoformat(), "symbol": best["symbol"], "pnl_usd": best["pnl_usd"],
             "r_result": best.get("r_result"), "file": f"cards/{fname}", "week": label}
    history = _load(CARDS, [])
    history.append(entry)
    _save(CARDS, history[-52:])
    telegram.send_photo(CARD_DIR / fname, f"Trade of the week: {best['symbol']} {best['pnl_usd']:+,.2f}")
    floor.event("performance", f"Picked the trade of the week: {best['symbol']} ({best['pnl_usd']:+,.2f})", best["symbol"])
    return entry


# ---- Season recap (monthly) --------------------------------------------------
def maybe_season_recap(state: dict) -> None:
    at = now()
    month_key = at.strftime("%Y-%m")
    if at.day > 3 or at.hour < 8 or state.get("recap_month") == month_key:
        return
    state["recap_month"] = month_key
    last_month_end = at.replace(day=1) - timedelta(days=1)
    try:
        season_recap(last_month_end.strftime("%Y-%m"), last_month_end.strftime("%B %Y"))
    except Exception as exc:
        print(f"season recap failed: {exc}")


def season_recap(month: str, label: str) -> dict | None:
    from desk import interns
    from desk.dashboard import _curve
    ctx = _context()
    trades = [t for t in ctx["trades"] if (t.get("closed_at") or "").startswith(month) and t.get("status") == "closed"]
    curve = [p for p in _curve() if p["t"].startswith(month)]
    if not curve and not trades:
        return None
    alumni = [a for a in staff.load()["alumni"] if (a.get("to") or "").startswith(month)]
    floor.working("cio", f"Writing the {label} season recap")
    facts = {
        "month": label,
        "account_start": curve[0]["equity"] if curve else None, "account_end": curve[-1]["equity"] if curve else None,
        "sp500_equivalent_start": curve[0]["spy"] if curve else None, "sp500_equivalent_end": curve[-1]["spy"] if curve else None,
        "closed_trades": len(trades), "wins": sum(t["pnl_usd"] > 0 for t in trades),
        "best_trade": max(trades, key=lambda t: t["pnl_usd"], default=None),
        "worst_trade": min(trades, key=lambda t: t["pnl_usd"], default=None),
        "people": [{k: p.get(k) for k in ("name", "role", "score", "equity")} for p in ctx["people"]],
        "staff_changes": alumni, "intern_graduates": interns._load().get("graduates", []),
    }
    out = ai.ask(f"""You are Victor Ashford, CIO of a small AI trading desk, writing the monthly "season recap" for the
managing partner, Erwann (a first-year finance student). Make it fun, like a sports season recap: the standings
(desk vs the S&P 500), the MVP (best hit rate with real sample size), the trade of the month, the worst trade,
any comeback story, staffing drama (firings, intern hires), and what to watch next month. Under 300 words, plain English.

FACTS: {json.dumps(facts, default=str)[:12000]}

Return JSON: {{"title": str, "recap": str, "mvp": str}}""", want_json=True, temperature=0.6)
    record = {"month": month, "label": label, "title": out.get("title") or f"{label} season recap",
              "body": out.get("recap", ""), "mvp": out.get("mvp")}
    history = _load(RECAPS, [])
    history.append(record)
    _save(RECAPS, history[-24:])
    telegram.send(f"<b>{esc(record['title'])}</b>\n\n{esc(record['body'])}")
    floor.done("cio", f"Sent the {label} season recap. MVP: {record.get('mvp') or 'n/a'}")
    return record
