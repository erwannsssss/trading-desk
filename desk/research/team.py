"""The AI analyst team. Each role is a separate AI call so they can genuinely disagree.

    macro analyst  -> what kind of market is this, how aggressive should we be
    specialists    -> fundamental, technical, news/sentiment and smart-money reads
    bull           -> the strongest case FOR the trade
    bear           -> the strongest case AGAINST, attacking the bull
    portfolio mgr  -> weighs it all, runs a pre-mortem, makes the call and the plan
"""
from __future__ import annotations

import json

from desk import ai, config

DESK = (
    "You work at a small, aggressive but disciplined trading desk managing a $10,000 paper account. "
    "The goal is to make money: take real risks when the setup earns it, size bigger on high conviction, "
    "and cut losers fast. You can go long or short stocks, ETFs, crypto and forex. "
    "Be specific and quantitative. Only use numbers that appear in the data you are given; never invent figures. "
    "If data is missing, say so instead of guessing."
)


def _j(obj) -> str:
    return json.dumps(obj, indent=1, default=str)


def _style(*seats: str) -> str:
    """Working approach of anyone recently hired into these seats (replacements for fired analysts)."""
    try:
        from desk import staff
        found = {s: staff.approaches().get(s) for s in seats}
        lines = [f"- {staff.SEATS[s][1]} ({staff.name(s)}): {a}" for s, a in found.items() if a]
        return ("\nWORKING STYLE OF NEW HIRES (follow it for that role):\n" + "\n".join(lines) + "\n") if lines else ""
    except Exception:
        return ""


# ---- Macro analyst -----------------------------------------------------------
def macro_view(context: dict) -> dict:
    prompt = f"""You are the desk's MACRO STRATEGIST. Read today's market data and set the game plan.
{_style("macro")}
DATA:
{_j(context)}

Decide:
- regime: one of "strong uptrend", "choppy/range-bound", "risk-off/defensive", "panic/high volatility", "recovery"
- risk_appetite: 0-100 (how aggressive the desk should be today)
- bucket_weights: % of the account for "core" (multi-day swing trades on quality setups), "tactical" (short-term momentum, breakouts, news and earnings trades), "high_risk" (big-upside speculative bets, shorts on hype). Must add to 100. Typical: 50/35/15; shift it with the regime.
- playbook: which trading styles should work today (momentum, mean reversion, breakouts, earnings plays, macro/forex, crypto) and which to avoid
- favored and avoided sectors/assets, with reasons
- key_events: scheduled events (Fed, economic data, big earnings) and how to position around them
- overnight_read: what the pre-market/overseas/crypto/forex moves suggest for the open
- summary: 3-4 plain-English sentences a college student could follow

Return JSON: {{"regime": str, "risk_appetite": int, "bucket_weights": {{"core": int, "tactical": int, "high_risk": int}},
"playbook": {{"use": [str], "avoid": [str]}}, "favored": [str], "avoid": [str], "key_events": [str], "overnight_read": str, "summary": str}}"""
    return ai.ask(prompt, system=DESK, want_json=True, temperature=0.3)


# ---- Per-idea debate ---------------------------------------------------------
def specialists(dossier: dict, macro: dict) -> dict:
    prompt = f"""You are four SPECIALIST ANALYSTS writing quick notes on {dossier['symbol']} ({dossier['asset_type']}).
{_style("technical", "fundamental", "sentiment", "smartmoney")}
Market backdrop: {macro.get('regime')} | {macro.get('summary')}

DOSSIER:
{_j(dossier)}

Each specialist scores -5 (strongly bearish) to +5 (strongly bullish) and gives 2-4 evidence-based points:
- technical: trend, momentum, key levels (support/resistance from the data), volatility, the setup
- fundamental: valuation, growth, margins, earnings record, latest press release (skip for crypto/forex/ETFs: say "n/a")
- news_sentiment: what the headlines and Reddit chatter mean, is the story already priced in
- smart_money: insider buying/selling, which hedge funds hold it and how big, analyst rating shifts

Also name the best trading style for it right now (momentum, breakout, mean reversion, earnings play, trend pullback, short/fade, macro) or "no clear setup".

Return JSON: {{"technical": {{"score": int, "points": [str]}}, "fundamental": {{"score": int, "points": [str]}},
"news_sentiment": {{"score": int, "points": [str]}}, "smart_money": {{"score": int, "points": [str]}}, "best_style": str}}"""
    return ai.ask(prompt, system=DESK, want_json=True, temperature=0.3)


def bull(dossier: dict, notes: dict) -> dict:
    prompt = f"""{_style("bull")}You are the BULL on {dossier['symbol']}. Make the strongest honest case to trade it in the direction you think pays best
(usually long, but if the best trade is a short, argue for the short). Use the data.

DOSSIER:
{_j(dossier)}

SPECIALIST NOTES:
{_j(notes)}

Return JSON: {{"direction": "long" or "short", "case": str (4-6 sentences), "catalysts": [str], "price_target_logic": str}}"""
    return ai.ask(prompt, system=DESK, want_json=True, temperature=0.5)


def bear(dossier: dict, notes: dict, bull_case: dict) -> dict:
    prompt = f"""{_style("bear")}You are the BEAR (devil's advocate) on {dossier['symbol']}. The bull wants to go {bull_case.get('direction')}.
Attack that trade as hard as the data allows: what's priced in, what could go wrong, bad timing, crowded positioning, upcoming events.

DOSSIER:
{_j(dossier)}

SPECIALIST NOTES:
{_j(notes)}

BULL CASE:
{_j(bull_case)}

Return JSON: {{"case": str (4-6 sentences), "risks": [str], "would_change_my_mind": str}}"""
    return ai.ask(prompt, system=DESK, want_json=True, temperature=0.5)


DESK_TARGET = (
    "DESK TARGET: the desk aims for roughly 20-30 trades a day across all sessions and markets, many of them small "
    "tactical or intraday trades. Pass only when you see no edge at all. A modest edge with a clear stop deserves a "
    "small, low-conviction position (conviction 4-5) rather than a pass; save conviction 8+ for the best setups."
)


def _decision_rules(dossier: dict, extra: dict) -> str:
    t = dossier["technicals"]
    options_ok = dossier["asset_type"] in ("stock", "etf")
    return f"""Current price: {t['price']}. ATR (typical daily range): {t.get('atr14')} ({t.get('atr_pct')}%).
{DESK_TARGET}
SESSION: {extra.get('session_note', '')}
STRATEGY TOURNAMENT (how each style has actually performed for this desk; lean toward what works): {_j(extra.get('tournament') or 'no history yet')}
BACKTESTED EDGE OF CHART SETUPS (2 years of history): {_j(extra.get('backtest') or 'not available')}

Steps:
1. PRE-MORTEM: imagine this trade lost money. Write the most likely story of how it failed.
   Then judge honestly: is that failure story more likely than the success story?
2. DECIDE: "long", "short" or "pass". Crypto can only be traded long. Pass if there is no edge, the failure
   story is more likely, or it duplicates a bet that is already planned or held.
3. If trading, build the plan:
   - bucket: "core", "tactical" or "high_risk"; style: one of momentum, breakout, mean reversion, earnings play,
     trend pullback, short/fade, macro
   - expression: {'"shares", "call_options" or "put_options". Options are a levered high_risk bet where the most you can lose is the premium: use calls for a strong bullish view, puts for a strong bearish one, typically with conviction 7+ and a move expected within 1-4 weeks. Otherwise "shares".' if options_ok else '"shares" (options are only available on stocks and ETFs).'}
   - entry: a limit price near the current price (within about 1 ATR), or the current price for a market order
   - stop: where the thesis is wrong (use real levels from the data; typically 1-2.5 ATR away). For options this is
     the UNDERLYING price where you exit.
   - target: a realistic take-profit level (underlying price for options); reward must be at least {config.MIN_REWARD_TO_RISK}x the risk
   - conviction 1-10; confidence_pct = your honest probability the target hits before the stop
   - horizon: "intraday", "1-5 days" or "1-4 weeks"
4. EVIDENCE: list the 3-6 numbers from the dossier your decision relies on. "fact" must be the exact path
   "section.field" (e.g. "technicals.rsi14", "fundamentals.pe_ratio", "insiders_last_90d.open_market_buys") and
   "value" the number shown.

DECISION JSON: {{"premortem": {{"failure_story": str, "failure_more_likely": bool}},
"decision": "long"|"short"|"pass", "reason": str, "bucket": str, "style": str, "expression": "shares"|"call_options"|"put_options",
"order_type": "limit"|"market", "entry": number|null, "stop": number|null, "target": number|null, "conviction": int,
"confidence_pct": int, "horizon": str, "thesis": str (3-5 plain-English sentences), "invalidation": str,
"plain_english": str (explain the decision to a complete beginner in 3-5 short sentences, defining any finance term
you use in brackets, e.g. "RSI [a 0-100 gauge of how stretched a price is]"),
"evidence": [{{"fact": str, "value": number}}]}}"""


def portfolio_manager(dossier: dict, notes: dict, bull_case: dict, bear_case: dict, macro: dict,
                      planned_today: list[dict], holdings: list[str], rulebook: list[str], extra: dict | None = None) -> dict:
    extra = extra or {}
    prompt = f"""You are the PORTFOLIO MANAGER and make the final call on {dossier['symbol']} ({dossier['asset_type']}).
{_style("pm")}
MARKET GAME PLAN: {_j(macro)}
ALREADY PLANNED TODAY (avoid piling into the same bet): {_j(planned_today)}
ALREADY HOLDING: {holdings or "nothing"}
DESK RULEBOOK (lessons learned; follow them): {rulebook or "none yet"}

DOSSIER:
{_j(dossier)}

SPECIALISTS: {_j(notes)}
BULL: {_j(bull_case)}
BEAR: {_j(bear_case)}

{_decision_rules(dossier, extra)}

Return only the DECISION JSON."""
    return ai.ask(prompt, system=DESK, want_json=True, temperature=0.2)


def quick_desk(dossier: dict, macro: dict, planned_today: list[dict], holdings: list[str],
               rulebook: list[str], extra: dict | None = None) -> dict:
    """The whole team in ONE call, for the fast intraday and overnight sessions (saves the free AI quota)."""
    extra = extra or {}
    prompt = f"""Run the whole desk on {dossier['symbol']} ({dossier['asset_type']}) in one pass, keeping each role honest
and separate: four specialists, a bull, a bear who attacks the bull, then the portfolio manager's final call.
{_style(*("technical", "fundamental", "sentiment", "smartmoney", "bull", "bear", "pm"))}
MARKET GAME PLAN: {_j(macro)}
ALREADY PLANNED TODAY: {_j(planned_today)}
ALREADY HOLDING: {holdings or "nothing"}
DESK RULEBOOK: {rulebook or "none yet"}

DOSSIER:
{_j(dossier)}

{_decision_rules(dossier, extra)}

Return JSON: {{"specialists": {{"technical": {{"score": int, "points": [str]}}, "fundamental": {{"score": int, "points": [str]}},
"news_sentiment": {{"score": int, "points": [str]}}, "smart_money": {{"score": int, "points": [str]}}, "best_style": str}},
"bull": {{"direction": "long"|"short", "case": str, "catalysts": [str]}},
"bear": {{"case": str, "risks": [str]}},
"decision": <the DECISION JSON>}}"""
    out = ai.ask(prompt, system=DESK, want_json=True, temperature=0.25)
    if not isinstance(out, dict) or not isinstance(out.get("decision"), dict):
        raise ValueError("quick desk returned an unexpected shape")
    return out


# ---- Fact-checker (plain code, no AI) ----------------------------------------
MAX_ENTRY_DISTANCE = {"stock": 0.06, "etf": 0.04, "crypto": 0.10, "forex": 0.015}


def fact_check(decision: dict, facts: dict, dossier: dict) -> dict:
    """Verifies the AI didn't make up numbers and that its prices make sense."""
    problems, verified = [], 0
    for ev in decision.get("evidence") or []:
        key, val = ev.get("fact"), ev.get("value")
        if key not in facts:
            problems.append(f"cited '{key}', which isn't in the data")
            continue
        real = facts[key]
        try:
            val = float(val)
        except (TypeError, ValueError):
            problems.append(f"'{key}' cited without a number")
            continue
        if abs(val - real) > max(abs(real) * 0.02, 0.02):
            problems.append(f"'{key}' cited as {val} but the data says {real}")
        else:
            verified += 1

    if decision.get("decision") in ("long", "short"):
        price = dossier["technicals"]["price"]
        entry, stop, target = decision.get("entry"), decision.get("stop"), decision.get("target")
        if not all(isinstance(x, (int, float)) and x > 0 for x in (entry, stop, target)):
            problems.append("entry, stop and target must all be prices")
        else:
            long = decision["decision"] == "long"
            if abs(entry / price - 1) > MAX_ENTRY_DISTANCE.get(dossier["asset_type"], 0.06):
                problems.append(f"entry {entry} is too far from the current price {price}")
            if long and not (stop < entry < target) or (not long and not (target < entry < stop)):
                problems.append("stop and target are on the wrong sides of the entry")
            else:
                rr = abs(target - entry) / abs(entry - stop)
                decision["reward_to_risk"] = round(rr, 2)
                if rr < config.MIN_REWARD_TO_RISK:
                    problems.append(f"reward-to-risk is only {rr:.2f}")
        if decision.get("premortem", {}).get("failure_more_likely"):
            problems.append("its own pre-mortem says failure is more likely")
        if verified < 2:
            problems.append(f"only {verified} cited numbers checked out (need 2+)")
    return {"passed": not problems, "verified_facts": verified, "problems": problems}


# ---- Position sizing ---------------------------------------------------------
def size(decision: dict, equity: float, asset: str, quote_to_usd: float = 1.0, unit_usd: float | None = None,
         multiplier: float = 1.0) -> dict:
    """Bet more on high conviction. Returns the dollar risk and position size; risk limits re-check it later.
    For forex, prices are in the quote currency: `quote_to_usd` converts them, `unit_usd` is one unit's $ value."""
    c = int(decision.get("conviction") or 0)
    risk_pct = 4.0 if c >= 9 else 2.5 if c >= 7 else 1.5 if c >= 5 else 0.75
    # The strategy tournament and calibration scale bets up for styles that work, down for ones that don't.
    risk_pct = min(config.MAX_RISK_PER_TRADE_PCT, risk_pct * max(0.25, min(multiplier, 1.6)))
    entry, stop = float(decision["entry"]), float(decision["stop"])
    per_unit_risk = abs(entry - stop) * quote_to_usd
    unit_value = unit_usd if unit_usd is not None else entry
    risk_usd = equity * risk_pct / 100
    units = risk_usd / per_unit_risk
    notional = units * unit_value
    cap = equity * (config.MAX_FOREX_POSITION_X if asset == "forex" else config.MAX_POSITION_PCT / 100)
    if notional > cap:
        units, notional = cap / unit_value, cap
        risk_usd = units * per_unit_risk
    return {"risk_pct": round(risk_usd / equity * 100, 2), "risk_usd": round(risk_usd, 2),
            "units": round(units, 6 if asset == "crypto" else 2), "notional_usd": round(notional, 2)}
