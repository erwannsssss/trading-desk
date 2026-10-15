"""The floor's daily calendar: every meeting and market event for today and tomorrow (New York time), who is in
the room, what's on the agenda, and, once it has happened, what they actually decided.

Built from the fixed schedule (the GitHub Actions timetable), the economic calendar, earnings dates for held
positions, and the research plans each session saves.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from desk import config, staff

NY = ZoneInfo(config.TIMEZONE)
QUICK = ["scanner", "technical", "fundamental", "sentiment", "smartmoney", "bull", "bear", "pm", "factcheck", "sizer"]
FULL = ["scanner", "macro"] + QUICK[1:]
# usual release times (ET) for US economic data; anything else shows as an all-day item
DATA_TIMES = [("fomc", "14:00"), ("fed ", "14:00"), ("interest rate decision", "14:00"), ("industrial production", "09:15"),
              ("consumer sentiment", "10:00"), ("ism", "10:00"), ("jolts", "10:00"), ("new home", "10:00"),
              ("existing home", "10:00"), ("pending home", "10:00"), ("consumer confidence", "10:00"),
              ("cpi", "08:30"), ("inflation", "08:30"), ("ppi", "08:30"), ("retail sales", "08:30"), ("jobless", "08:30"),
              ("payroll", "08:30"), ("employment", "08:30"), ("gdp", "08:30"), ("pce", "08:30"), ("durable", "08:30"),
              ("housing starts", "08:30"), ("trade balance", "08:30"), ("personal income", "08:30")]


def _at(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), NY)


def _names(seats: list[str]) -> str:
    names = [staff.name(s).split()[0] for s in seats]
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else (names[0] if names else "")


def _unlocked_desks() -> list[str]:
    try:
        from desk import firm
        fd = firm.load()
        return [s for s in ("crypto", "fx", "events") if firm.seat_unlocked(s, fd)]
    except Exception:
        return []


def _match(plans: list[dict], day: date, session: str, slot: datetime, used: set) -> dict | None:
    """The plan this slot produced: same session type, created up to 20 min early or 2.5 hours late
    (GitHub sometimes starts scheduled jobs late)."""
    best = None
    for p in plans:
        if p.get("session") != session or p.get("date") != day.isoformat() or p.get("_file") in used:
            continue
        t = datetime.fromisoformat(p["created_at"])
        if slot - timedelta(minutes=20) <= t <= slot + timedelta(minutes=150):
            if best is None or t < datetime.fromisoformat(best["created_at"]):
                best = p
    if best:
        used.add(best["_file"])
    return best


def _result(p: dict) -> tuple[str, list[dict]]:
    ideas = p.get("ideas", [])
    ok = [i for i in ideas if i.get("status") == "approved"]
    txt = (f"Researched {len(ideas)}, approved {len(ok)}" + (f": {', '.join(i['symbol'] for i in ok[:8])}" if ok else "")
           + f". Finished {datetime.fromisoformat(p['created_at']).strftime('%-I:%M %p')}.")
    quotes = []
    best = max(ok, key=lambda i: (i.get("decision") or {}).get("conviction") or 0, default=None)
    if best:
        d = best.get("decision") or {}
        quotes.append({"seat": "pm", "text": f"{best['symbol']}, {d.get('decision')} at conviction {d.get('conviction')}/10: "
                                              f"{(d.get('thesis') or d.get('reason') or '')[:220]}"})
        bear = (best.get("bear") or {}).get("case")
        if bear:
            quotes.append({"seat": "bear", "text": f"On {best['symbol']}: {bear[:220]}"})
    elif ideas:
        i = ideas[0]
        quotes.append({"seat": "pm", "text": f"Passed on {i['symbol']}: {((i.get('decision') or {}).get('reason') or '')[:220]}"})
    if p.get("macro") and p.get("session") in ("morning", "evening"):
        quotes.insert(0, {"seat": "macro", "text": (p["macro"].get("summary") or "")[:260]})
    return txt, quotes


def build(plans: list[dict], trades: list[dict], econ: list[dict] | None, earnings: dict | None,
          now_dt: datetime | None = None) -> dict:
    from desk.state import now
    now_dt = now_dt or now()
    today = now_dt.date()
    active = [t for t in trades if t.get("status") in ("open", "pending", "closing")]
    held = ", ".join(t["symbol"] for t in active) or "nothing yet"
    macro = next((p.get("macro") for p in reversed(plans) if p.get("macro")), None) or {}
    extra_desks = _unlocked_desks()
    used: set = set()
    days = []
    for day in (today, today + timedelta(days=1)):
        wk = day.weekday()
        items: list[dict] = []
        allday: list[dict] = []

        def meeting(hhmm, session, title, who, agenda):
            slot = _at(day, hhmm)
            it = {"t": slot.isoformat(), "kind": "meeting", "session": session, "title": title, "who": who, "agenda": agenda}
            p = _match(plans, day, session, slot, used) if session else None
            if p:
                it["result"], it["quotes"] = _result(p)
            items.append(it)

        # overnight crypto and forex, 2:10am
        meeting("02:10", "overnight", "Overnight huddle: crypto and currencies", ["scanner", "pm", "bull", "bear", "factcheck", "sizer"],
                ["Maya scans the crypto coins and currency pairs that moved overnight",
                 "One-call quick huddle per idea: Jake for, Nadia against, Elena decides", "Asia and Europe are trading; US stocks are closed"])
        if wk < 5:
            interns = "the interns make their own picks with their $150 accounts"
            meeting("07:40", "morning", "Morning meeting: the full research team", FULL + ["hr"] + extra_desks,
                    ["Maya scans about 150 stocks and ETFs plus crypto and currencies and brings the top finalists",
                     f"Darius sets the day's market read (last one: {macro.get('regime', 'none yet')}, aggression {macro.get('risk_appetite', '?')}/100)",
                     "Top 8 ideas get the full team: Priya, Marcus, Sofia and Theo score them, Jake argues for, Nadia argues against",
                     "Elena runs a pre-mortem and decides; Sam checks every number she cites; Iris sizes the bet",
                     f"Ashley's office: {interns}", f"Already holding: {held}"])
            items.append({"t": _at(day, "09:30").isoformat(), "kind": "market", "title": "US stock market opens", "who": ["execution", "positions"],
                          "agenda": ["Kenji places the stock and options orders waiting from the morning plan, after a 5-minute no-trade window",
                                     "Leo starts watching every stop and target"]})
            for hhmm in ("10:20", "11:50", "13:20", "14:50"):
                meeting(hhmm, "intraday", "Intraday huddle", QUICK,
                        ["Maya looks for what's moving right now; each idea gets a one-call huddle (specialists, Jake, Nadia, Elena)",
                         "Reuses the morning market read; favors trades for today or the next few days"])
            soon = []
            if earnings:
                nxt = day + timedelta(days=3 if wk == 4 else 1)
                for t in active:
                    e = earnings.get(t["symbol"])
                    if e and e.get("date") in (day.isoformat(), nxt.isoformat()) and (t.get("style") or "").lower() != "earnings play":
                        soon.append(f"{t['symbol']} reports {e['date']} {(e.get('hour') or '').upper()}")
            items.append({"t": _at(day, "15:15").isoformat(), "kind": "check", "title": "Earnings check", "who": ["positions", "risk"],
                          "agenda": ([f"Leo and Grace close positions reporting earnings before the next open: {', '.join(soon)}"] if soon
                                     else ["Leo and Grace check whether any held stock reports earnings before the next open (none expected right now)"])})
            items.append({"t": _at(day, "16:00").isoformat(), "kind": "market", "title": "US stock market closes", "who": ["execution"],
                          "agenda": ["No new stock orders in the last 5 minutes; crypto and currencies keep trading"]})
            items.append({"t": _at(day, "16:10").isoformat(), "kind": "report", "title": "End-of-day recap to your Telegram", "who": ["client", "performance"],
                          "agenda": ["Riley sends you the day: account value, trades opened and closed, what's still open",
                                     "Omar updates the strategy scoreboard"]})
        meeting("18:10", "evening", "Evening meeting: crypto and currencies", FULL + ["hr"] + [s for s in extra_desks if s != "events"],
                ["Darius reads the US close and sets the overnight game plan",
                 "Full team on the best crypto and currency ideas; the interns make evening picks",
                 f"Already holding: {held}"])
        meeting("22:10", "overnight", "Late huddle: crypto and currencies", ["scanner", "pm", "bull", "bear", "factcheck", "sizer"],
                ["Quick huddle on crypto and currency moves as Asia opens"])
        if wk == 6:
            items.append({"t": _at(day, "19:30").isoformat(), "kind": "meeting", "title": "Sunday review: the learning desk and HR",
                          "who": ["performance", "reviewer", "quant", "hr", "cio"],
                          "agenda": ["Nina re-runs the 2-year backtest; Omar re-scores the strategy tournament",
                                     "Ruth turns the week's hard lessons into rules; Victor writes your weekly fund letter",
                                     "Ashley's performance reviews, promotions and demotions; trade of the week card"]})
        if day.day == 1:
            items.append({"t": _at(day, "08:00").isoformat(), "kind": "report", "title": "Bonus day and season recap",
                          "who": ["hr", "treasury", "cio"],
                          "agenda": ["Ashley pays last month's bonuses (20% of profits above the firm's high-water mark)",
                                     "Victor writes the monthly season recap"]})
        # economic data
        for e in econ or []:
            if e.get("date") != day.isoformat():
                continue
            name = e.get("event") or "Economic release"
            hhmm = next((tm for key, tm in DATA_TIMES if key in name.lower()), None)
            row = {"kind": "data", "title": f"Economic data: {name}", "who": ["macro"],
                   "agenda": ["Darius watches the release; big surprises move stocks, bonds and the dollar"]}
            if hhmm:
                items.append({**row, "t": _at(day, hhmm).isoformat()})
            else:
                allday.append(row)
        # held positions reporting earnings
        for t in active:
            e = (earnings or {}).get(t["symbol"])
            if e and e.get("date") == day.isoformat():
                allday.append({"kind": "data", "title": f"{t['symbol']} earnings ({(e.get('hour') or 'time n/a').upper()})",
                               "who": ["fundamental", "positions"], "agenda": ["A position the desk holds reports today"]})
        allday.insert(0, {"kind": "check", "title": "Every 15 minutes: position check",
                          "who": ["positions", "execution", "risk", "compliance", "treasury"],
                          "agenda": [f"Leo trails stops and takes profits; Kenji works open orders; Grace checks the loss limits; "
                                     f"Diane audits every fill; Hugo tracks the bank loans. Holding: {held}",
                                     "Emergency huddle if a position moves sharply or gets news"]})
        # unscheduled meetings that already happened: breaking-news re-research and your /pitch ideas
        for p in plans:
            if p.get("date") != day.isoformat() or p.get("session") not in ("alert", "pitch"):
                continue
            i = (p.get("ideas") or [{}])[0]
            d = i.get("decision") or {}
            pitch = p["session"] == "pitch"
            items.append({"t": p["created_at"], "kind": "meeting", "unscheduled": True,
                          "title": f"{'Your pitch' if pitch else 'Breaking move'}: {i.get('symbol', '?')}",
                          "who": ["pm", "bull", "bear", "factcheck"] + ([] if pitch else ["sentiment"]),
                          "agenda": [("You pitched it on Telegram; the full team researched it" if pitch
                                      else (i.get("why_scanned") or ["it moved sharply since the morning research"])[0])],
                          "result": f"{(d.get('decision') or '?').upper()}" + (f", conviction {d.get('conviction')}/10" if d.get("decision") in ("long", "short") else "")
                                    + f": {(d.get('reason') or d.get('thesis') or '')[:200]}"})
        items.sort(key=lambda x: x["t"])
        days.append({"date": day.isoformat(), "label": "Today" if day == today else "Tomorrow",
                     "items": items, "allday": allday})
    return {"generated": now_dt.isoformat(timespec="minutes"), "days": days}
