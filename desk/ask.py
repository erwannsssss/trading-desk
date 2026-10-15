"""/ask: message anyone at the firm directly ("/ask Nadia why do you hate ONDS?").

The person answers in first person, in their own voice, from their own work: an analyst from the notes they wrote,
the bull and bear from their cases, Kenji from his orders, Diane from her audits, Ashley from her reviews, an intern
from their account.
"""
from __future__ import annotations

import json
import re

from desk import ai, config, staff

VOICES = {
    "cio": "measured, big-picture, a little old-school",
    "hr": "warm but blunt corporate-HR phrasing with dry humor",
    "client": "friendly and quick, the person who talks to Erwann every day",
    "compliance": "precise, by-the-book, unimpressed by excuses",
    "pm": "decisive, weighs both sides out loud, owns her calls",
    "macro": "talks in regimes, rates and the dollar",
    "bull": "relentlessly optimistic, always sees the upside",
    "bear": "skeptical and sharp, enjoys poking holes",
    "technical": "lives in charts: levels, trends, RSI",
    "fundamental": "valuation and earnings first, distrusts hype",
    "sentiment": "reads headlines and Reddit, knows what's priced in",
    "smartmoney": "follows insiders and hedge funds",
    "factcheck": "pedantic about numbers, proud of every block",
    "sizer": "all about risk per trade and position size",
    "execution": "calm, practical, cares about fills",
    "positions": "patient, trails stops, protects winners",
    "options": "talks premium, deltas and spreads",
    "risk": "protective, thinks about what could go wrong for the whole firm",
    "treasury": "thinks about the credit line and interest costs",
    "performance": "data-driven, cites the scoreboard",
    "reviewer": "reflective, talks about lessons from closed trades",
    "quant": "statistical, talks about samples and luck vs skill",
    "second": "independent and blunt; judges trades on the data alone",
}
NOTE_KEYS = {"second": "second_opinion", "technical": "technical", "fundamental": "fundamental", "sentiment": "news_sentiment",
             "smartmoney": "smart_money", "crypto": "crypto_desk", "fx": "fx_desk", "events": "event_desk"}


def people() -> list[dict]:
    from desk import interns
    out = [p for p in staff.org_chart() if p["id"] != "partner"]
    for i in interns.all_interns():
        if i["status"] in ("intern", "junior"):
            out.append({"id": f"intern:{i['id']}", "name": i["name"], "role": "Junior analyst" if i["status"] == "junior" else "Intern",
                        "desk": "intern", "intern": i})
    return out


def find(text: str) -> tuple[dict | None, str]:
    """Match the start of the message to a person (first name, last name, full name or role). Returns (person, question)."""
    words = text.strip().split()
    if not words:
        return None, ""
    roster = people()
    for n in (2, 1):
        if len(words) < n:
            continue
        key = " ".join(words[:n]).lower().strip(",:?")
        for p in roster:
            first, *rest = p["name"].lower().split()
            if key in (p["name"].lower(), first, rest[-1] if rest else first):
                return p, " ".join(words[n:])
    key = words[0].lower().strip(",:?")
    aliases = {"the bull": "bull", "bull": "bull", "bear": "bear", "hr": "hr", "pm": "pm", "cio": "cio", "risk": "risk",
               "compliance": "compliance", "macro": "macro", "options": "options", "treasurer": "treasury", "quant": "quant"}
    if key in aliases:
        return next((p for p in roster if p["id"] == aliases[key]), None), " ".join(words[1:])
    return None, text


def _symbols(question: str) -> set[str]:
    return {w.upper().replace("/", "") for w in re.findall(r"\b[A-Za-z]{2,6}(?:/USD|_[A-Za-z]{3})?\b", question)}


def context(p: dict, question: str) -> dict:
    from desk import compliance, firm, scorecard
    from desk.dashboard import _curve, _recent_plans
    from desk.trading import ledger
    seat = p["id"]
    trades = ledger.load()
    plans = _recent_plans(3)
    known = {t["symbol"].replace("/", "") for t in trades} | {i.get("symbol", "").replace("/", "") for pl in plans for i in pl.get("ideas", [])}
    syms = {s for s in _symbols(question) if s in known}   # only filter by tickers the desk has actually looked at
    ctx: dict = {"you": {"name": p["name"], "role": p["role"], "level": p.get("level"), "desk": p.get("desk"),
                         "working_style": p.get("approach")}}
    if p.get("intern"):
        i = p["intern"]
        ctx["your_account"] = {k: i.get(k) for k in ("persona", "equity", "cash", "peak", "mood", "positions", "closed", "status")}
        return ctx
    sc = scorecard.seat_scores(trades, _curve()).get(seat)
    if sc:
        ctx["your_hit_rate_30d"] = sc
    try:
        fd = firm.load()
        ctx["your_pay"] = {"level": firm.level(seat, fd), "base_paid_so_far": round(fd["payroll"]["by_seat"].get(seat, 0), 2)}
    except Exception:
        pass
    flags = compliance.findings_by_seat(days=14).get(seat)
    if flags:
        ctx["your_compliance_flags"] = flags
    try:
        reviews = json.loads((config.DATA_DIR / "reviews.json").read_text())
        r = (reviews[-1].get("reviews") or {}).get(seat)
        if r:
            ctx["your_latest_hr_review"] = r
    except Exception:
        pass

    def want(sym):
        return not syms or sym.replace("/", "") in syms

    work = []
    for plan in plans[-8:]:
        for i in plan.get("ideas", []):
            if not want(i.get("symbol", "")):
                continue
            d = i.get("decision") or {}
            row = {"symbol": i.get("symbol"), "when": plan.get("created_at"), "session": plan.get("session"), "outcome": i.get("status")}
            if seat in NOTE_KEYS and isinstance((i.get("specialists") or {}).get(NOTE_KEYS[seat]), dict):
                row["your_note"] = i["specialists"][NOTE_KEYS[seat]]
            elif seat == "bull":
                row["your_case"] = i.get("bull")
            elif seat == "bear":
                row["your_case"], row["bull_wanted"] = i.get("bear"), (i.get("bull") or {}).get("direction")
            elif seat in ("pm", "cio"):
                row["decision"] = {k: d.get(k) for k in ("decision", "conviction", "reason", "thesis", "premortem")}
            elif seat == "scanner":
                row["why_you_flagged_it"] = i.get("why_scanned")
            elif seat == "factcheck":
                row["your_check"] = i.get("fact_check")
            elif seat == "sizer":
                row["your_sizing"] = i.get("sizing")
            else:
                continue
            work.append(row)
    if seat == "macro":
        ctx["your_latest_market_read"] = next((pl.get("macro") for pl in reversed(plans) if pl.get("macro")), None)
    if work:
        ctx["your_recent_work"] = work[-14:]
    if seat in ("execution", "positions", "options", "risk", "treasury", "cio", "pm", "performance", "reviewer", "client"):
        recent = sorted(trades, key=lambda t: t.get("created_at", ""), reverse=True)
        ctx["desk_trades"] = [{k: t.get(k) for k in ("symbol", "asset", "direction", "status", "style", "conviction", "entry_price",
                                                     "stop", "target", "exit_price", "pnl_usd", "r_result", "exit_reason",
                                                     "skip_reason", "loan_usd", "notes", "postmortem", "short_strike", "strike")}
                              for t in recent if want(t["symbol"])][:15]
    if seat == "compliance":
        ctx["your_audit_findings"] = compliance.load()["findings"][-25:]
        ctx["what_you_check_on_every_trade"] = [
            "every position has a stop", "risk at the actual fill stays under the 5% per-trade cap",
            "options premium under 6% per trade", "total open risk under 20% of the firm",
            "positions within equity plus the bank line", "stops are never widened", "losses stay near 1R",
            "fills aren't much worse than planned", "reward at least 1.2x risk at the fill",
            "no stock fills in the first or last 5 minutes", "trades don't overstay their time frame"]
        ctx["audits_so_far"] = compliance.summary()["last_30"]
    if seat in ("performance", "quant", "reviewer"):
        from desk import learning
        ctx["tournament"] = learning._load(learning.TOURNAMENT, None)
    if seat == "hr":
        ctx["reviews"] = json.loads((config.DATA_DIR / "reviews.json").read_text())[-1] if (config.DATA_DIR / "reviews.json").exists() else None
        ctx["level_changes"] = firm.load()["level_log"][-15:]
    return ctx


def answer(text: str) -> str:
    p, question = find(text)
    if not p:
        names = ", ".join(x["name"].split()[0] for x in people()[:12])
        return f"Who should I ask? Start with a name, like /ask Nadia why are you bearish on ONDS? (Try: {names}.)"
    if p.get("locked"):
        return f"{p['name']} hasn't been hired yet: they join when the firm unlocks the {p.get('unlock_title')}."
    if not question.strip():
        return f"What do you want to ask {p['name'].split()[0]}?"
    ctx = json.dumps(context(p, question), default=str)[:24000]
    directory = "; ".join(f"{x['name']} ({x['role']})" for x in people() if not x.get("locked"))
    voice = VOICES.get(p["id"], "a sharp young trader" if p.get("intern") else "professional and plain-spoken")
    prompt = f"""You are {p['name']}, {p['role']} at a small AI trading firm (paper money). Erwann, the managing partner
(a first-year finance student), just messaged you directly on Telegram:

"{question}"

Answer in first person, in your own voice ({voice}). Speak only from YOUR records below: your notes, calls,
numbers and track record. If they don't cover it, say so honestly and point Erwann to the right colleague, using
ONLY a name from this staff list (never invent people): {directory}.
Explain any finance term in plain English. Under 150 words. Plain text only, no markdown.

YOUR RECORDS:
{ctx}"""
    reply = ai.ask(prompt, temperature=0.6, models=config.AI_LIGHT)
    return f"<b>{p['name']}</b> ({p['role']}):\n{_esc(reply)}"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
