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


DATA_GUIDE = (
    "HOW TO READ THE EXTRA DATA (when present): "
    "short_interest_and_analysts.short_pct_of_float = share of tradable shares sold short (under 5% normal, over 20% a "
    "crowded short: squeeze fuel if good news hits, but also a sign pros expect a drop); days_to_cover = days of normal "
    "volume needed to buy them back; analyst_rating_1_buy_5_sell (1 = strong buy). "
    "short_volume = share of each day's trading that was short sales (40-55% is normal market-making; well above that "
    "means unusual selling pressure). "
    "sec_filings = the latest quarter and year straight from SEC filings (millions of dollars) and insider Form 4 trades: "
    "open-market BUYS by insiders are a strong bullish signal; sales, especially on preset plans, mean little. "
    "futures_positioning = CFTC Commitments of Traders: speculators' net position as % of open interest, its weekly "
    "change, and three_year_percentile (0 = most bearish in 3 years, 100 = most bullish); above 85 or below 15 is a "
    "crowded trade that tends to reverse. "
    "crypto_sentiment = Crypto Fear & Greed (0-100; extreme fear often marks bottoms, extreme greed tops), stablecoin "
    "supply growth (new money ready to buy crypto) and DeFi value locked. "
    "sector = the company's sector and industry. "
    "kronos_forecast = Kronos, an AI model trained only on price charts from 45+ exchanges, drew many possible "
    "futures for the next few days: expected_move_pct and median_move_pct (where it thinks price goes), prob_up_pct "
    "(share of futures that end higher), range_low_10pct/range_high_90pct (the likely range), signal_strength (expected "
    "move vs its own uncertainty; above 1 is a confident call) and trust_weight (0-1, how much its backtest says to "
    "trust it). It is one more opinion, strongest when it agrees with the chart and the story; it knows nothing about "
    "news or earnings. options_market = what options traders are pricing: implied_move_pct by the next weekly expiry "
    "(a target beyond that move needs a reason), implied_vol_pct, and put_call_volume_ratio (above 1 = more bearish bets). "
    "volatility_indexes = VIX (expected 30-day S&P swings; under 15 calm, over 25 fearful), vix_term_ratio (VIX over "
    "3-month VIX; above 1 = near-term panic), VVIX and SKEW (demand for crash protection). market_internals = breadth: "
    "share of stocks above their 50- and 200-day averages, new highs vs new lows, leading and lagging sectors. A rally "
    "with weak breadth is fragile; a sell-off with breadth already washed out often bounces."
)


def _j(obj) -> str:
    return json.dumps(obj, indent=1, default=str)


def _style(*seats: str) -> str:
    """Working approach of anyone recently hired into these seats (replacements for fired analysts)."""
    try:
        from desk import staff
        found = {s: staff.approaches().get(s) for s in seats}
        lines = [f"- {staff.ALL_SEATS[s][1]} ({staff.name(s)}): {a}" for s, a in found.items() if a]
        return ("\nWORKING STYLE OF NEW HIRES (follow it for that role):\n" + "\n".join(lines) + "\n") if lines else ""
    except Exception:
        return ""


# ---- Macro analyst -----------------------------------------------------------
def macro_view(context: dict) -> dict:
    prompt = f"""You are the desk's MACRO STRATEGIST. Read today's market data and set the game plan.
{_style("macro")}
DATA:
{_j(context)}

{DATA_GUIDE}

Decide:
- regime: one of "strong uptrend", "choppy/range-bound", "risk-off/defensive", "panic/high volatility", "recovery"
- risk_appetite: 0-100 (how aggressive the desk should be today). This desk is aggressive: use 50-90 most days and
  rarely go below 40 even in a sell-off, because a falling market is a trading opportunity (shorts, puts, havens)
- bucket_weights: % of the account for "core" (multi-day swing trades on quality setups), "tactical" (short-term momentum, breakouts, news and earnings trades), "high_risk" (big-upside speculative bets, shorts on hype). Must add to 100. Typical: 50/35/15; shift it with the regime.
- playbook: which trading styles should work today (momentum, mean reversion, breakouts, earnings plays, macro/forex, crypto) and which to avoid
- favored and avoided sectors/assets, with reasons
- key_events: scheduled events (Fed, economic data, big earnings) and how to position around them
- overnight_read: what the pre-market/overseas/crypto/forex moves suggest for the open
- summary: 3-4 plain-English sentences a college student could follow

Return JSON: {{"regime": str, "risk_appetite": int, "bucket_weights": {{"core": int, "tactical": int, "high_risk": int}},
"playbook": {{"use": [str], "avoid": [str]}}, "favored": [str], "avoid": [str], "key_events": [str], "overnight_read": str, "summary": str}}"""
    return ai.ask(prompt, system=DESK, want_json=True, temperature=0.3)


# ---- Unlocked desks (firm.UNLOCKS) ---------------------------------------------
# seat: (notes key, which ideas it covers, what it focuses on)
DESK_ROLES = {
    "crypto": ("crypto_desk", "crypto",
               "CRYPTO STRATEGIST: the crypto-specific read: this coin's trend and momentum against Bitcoin and the "
               "other majors in the data, volatility regime, liquidity and weekend effects, hype vs. real buying"),
    "fx": ("fx_desk", "forex",
           "FX STRATEGIST: interest-rate gap and central-bank direction for both currencies (from the macro data), the "
           "US dollar trend, carry, which trading session moves this pair, and key round-number levels"),
    "events": ("event_desk", "stock",
               "EVENT-DRIVEN ANALYST: the upcoming earnings date, the past earnings surprises, how the stock reacted to "
               "recent news, the risk of a big gap, and whether to hold through the event or trade the reaction"),
}


def desk_roles(dossier: dict) -> list[tuple[str, str, str]]:
    """Unlocked desks that should weigh in on this idea: (seat, notes key, focus)."""
    try:
        from desk import firm
        fdata = firm.load()
    except Exception:
        return []
    out = []
    for seat, (key, asset, focus) in DESK_ROLES.items():
        if not firm.seat_unlocked(seat, fdata):
            continue
        if seat == "events":
            if dossier.get("asset_type") == "stock" and (dossier.get("upcoming_earnings") or dossier.get("earnings_history")):
                out.append((seat, key, focus))
        elif dossier.get("asset_type") == asset:
            out.append((seat, key, focus))
    return out


def desk_notes(dossier: dict, macro: dict) -> dict:
    """Extra specialist notes from unlocked desks (one AI call covering all that apply)."""
    roles = desk_roles(dossier)
    if not roles:
        return {}
    from desk import staff
    keys = ", ".join(f'"{k}": {{"score": int, "points": [str]}}' for _, k, _ in roles)
    prompt = f"""You are the firm's specialist desk{'s' if len(roles) > 1 else ''} writing a note on {dossier['symbol']} ({dossier['asset_type']}).
{_style(*(s for s, _, _ in roles))}
""" + "\n".join(f"- {k} ({staff.name(s)}): {f}" for s, k, f in roles) + f"""
Market backdrop: {macro.get('regime')} | {macro.get('summary')}

DOSSIER:
{_j(dossier)}

Each scores -5 (strongly bearish) to +5 (strongly bullish) with 2-4 evidence-based points from the data.
Return JSON: {{{keys}}}"""
    out = ai.ask(prompt, system=DESK, want_json=True, temperature=0.3)
    return {k: out[k] for _, k, _ in roles if isinstance(out, dict) and isinstance(out.get(k), dict)}


def quant_note(dossier: dict) -> dict | None:
    """Quant desk (once unlocked): the backtested record of this idea's chart setups. Plain code, no AI."""
    try:
        from desk import config, firm
        if not firm.seat_unlocked("quantdesk"):
            return None
        path = config.DATA_DIR / "backtest.json"
        if not path.exists():
            return None
        results = json.loads(path.read_text()).get("results") or {}
        setups = dossier.get("technicals", {}).get("setups") or []
        rows = {s: results[s] for s in setups if s in results}
        return {"setups_backtested": rows or "none of this idea's setups have a backtest"} if setups else None
    except Exception:
        return None


def _team_record(extra: dict) -> str:
    team = extra.get("team")
    if not team:
        return ""
    return ("TEAM TRACK RECORDS (levels and how often each person's calls were right over 30 days; give more weight "
            "to senior, proven people and less to anyone under 50%): " + _j(team) + "\n")


# ---- Per-idea debate ---------------------------------------------------------
def specialists(dossier: dict, macro: dict) -> dict:
    prompt = f"""You are four SPECIALIST ANALYSTS writing quick notes on {dossier['symbol']} ({dossier['asset_type']}).
{_style("technical", "fundamental", "sentiment", "smartmoney")}
Market backdrop: {macro.get('regime')} | {macro.get('summary')}

DOSSIER:
{_j(dossier)}

{DATA_GUIDE}

Each specialist scores -5 (strongly bearish) to +5 (strongly bullish) and gives 2-4 evidence-based points:
- technical: trend, momentum, key levels (support/resistance from the data), volatility, the setup, short-sale volume
- fundamental: valuation, growth, margins, earnings record, the SEC-filed numbers, latest press release (skip for crypto/forex/ETFs: say "n/a")
- news_sentiment: what the headlines and Reddit chatter mean, short interest and crowding, crypto fear & greed, is the story already priced in
- smart_money: insider Form 4 buys/sells, which hedge funds hold it, futures positioning (CFTC), analyst targets and rating shifts

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
    "DESK MANDATE: this is an aggressive trading desk that has to make money, not preserve it. Sitting in cash is a "
    "losing strategy. The firm aims for about 30 orders a day across all sessions and markets (the research desk plus "
    "the quant desk), so approve every idea that has a real edge: a typical session should approve at least half of "
    "what it researches, more when the desk is behind its daily pace. Volume is never a reason to take a trade with "
    "negative expected value. "
    "Judge each trade on EXPECTED VALUE, not on whether it is more likely to win than lose: a 40% chance to make 3x "
    "the risk is a GOOD trade. Take calculated risks you believe can actually happen, with a clear stop. "
    "A risk-off or choppy market is NOT a reason to pass: it changes WHICH trades to take (shorts and puts on weak "
    "names, defensive or relative-strength longs, currency and commodity trends, crypto momentum), not whether to "
    "trade. Pass only when the expected value is negative or there is genuinely no edge. "
    "Use conviction honestly: 5-6 for a solid edge, 7-8 for strong setups, 9-10 for the best of the week. "
    "Use options (calls or puts) for strong, fast views where the payoff can be several times the cost. "
    "The firm has a $100,000 bank credit line; trades at conviction 6+ may borrow from it."
)
DESK_RULES = (
    "FIRM RULES (the system enforces these, so don't fight them): "
    "(1) when the market regime is risk-off or panic, buying stocks needs conviction 5+; "
    "(2) no more than 2 positions leaning the same way on one currency, 3 crypto longs, or 3 stock positions with the "
    "same direction and style, so look for a DIFFERENT bet instead of another copy of an existing one; "
    "(3) don't hold a stock through its earnings report unless the style is \"earnings play\"; "
    "(4) bearish stock views are traded with puts, because this account can't short shares; "
    "(5) stocks must trade above $10."
)


def _decision_rules(dossier: dict, extra: dict) -> str:
    t = dossier["technicals"]
    options_ok = dossier["asset_type"] in ("stock", "etf")
    return f"""Current price: {t['price']}. ATR (typical daily range): {t.get('atr14')} ({t.get('atr_pct')}%).
{DESK_TARGET}
{DESK_RULES}
{_team_record(extra)}{("EXIT TUNING FROM REAL RESULTS (stops and targets are adjusted to this automatically): " + " ".join(extra.get("exit_tuning")) + chr(10)) if extra.get("exit_tuning") else ""}{("WHAT THE RESULTS SAY: " + " ".join(extra.get("shadow_lessons") or []) + " Bet sizing: " + str(extra.get("edge_tilt") or "") + chr(10)) if extra.get("shadow_lessons") or extra.get("edge_tilt") else ""}
SESSION: {extra.get('session_note', '')}
STRATEGY TOURNAMENT (how each style has actually performed for this desk; lean toward what works): {_j(extra.get('tournament') or 'no history yet')}
BACKTESTED EDGE OF CHART SETUPS (2 years of history): {_j(extra.get('backtest') or 'not available')}

Steps:
1. PRE-MORTEM: imagine this trade lost money. Write the most likely story of how it failed, and use it to set a
   smart stop. Then set "failure_more_likely" to true ONLY if the trade has NEGATIVE expected value (the chance of
   losing times the loss outweighs the chance of winning times the gain), not merely because losing is possible.
2. DECIDE: "long", "short" or "pass". Crypto can only be traded long. Pass only if the expected value is negative,
   there is no edge, or it duplicates a bet that is already planned or held.
3. If trading, build the plan:
   - bucket: "core", "tactical" or "high_risk"; style: one of momentum, breakout, mean reversion, earnings play,
     trend pullback, short/fade, macro
   - expression: {'"shares", "call_options" or "put_options". Options are a levered high_risk bet where the most you can lose is the premium: use calls for a strong bullish view, puts for a strong bearish one, typically with conviction 6+ and a move expected within 1-4 weeks; the payoff can be several times the cost. Otherwise "shares".' if options_ok else '"shares" (options are only available on stocks and ETFs).'}
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
    return ai.ask(prompt, system=DESK, want_json=True, temperature=0.2, check=_good_decision)


def _good_decision(d) -> bool:
    return isinstance(d, dict) and d.get("decision") in ("long", "short", "pass")


def _good_quick(out) -> bool:
    return isinstance(out, dict) and _good_decision(out.get("decision")) and isinstance(out.get("specialists"), dict)


def quick_desk(dossier: dict, macro: dict, planned_today: list[dict], holdings: list[str],
               rulebook: list[str], extra: dict | None = None) -> dict:
    """The whole team in ONE call, for the fast intraday and overnight sessions (saves the free AI quota)."""
    extra = extra or {}
    roles = desk_roles(dossier)
    role_lines = "".join(f"\nAlso include a specialist note \"{k}\" from the {f}" for _, k, f in roles)
    role_json = "".join(f', "{k}": {{{{"score": int, "points": [str]}}}}' for _, k, _ in roles).replace("{{", "{").replace("}}", "}")
    prompt = f"""Run the whole desk on {dossier['symbol']} ({dossier['asset_type']}) in one pass, keeping each role honest
and separate: four specialists, a bull, a bear who attacks the bull, then the portfolio manager's final call.{role_lines}
{_style(*("technical", "fundamental", "sentiment", "smartmoney", "bull", "bear", "pm"))}
MARKET GAME PLAN: {_j(macro)}
ALREADY PLANNED TODAY: {_j(planned_today)}
ALREADY HOLDING: {holdings or "nothing"}
DESK RULEBOOK: {rulebook or "none yet"}

DOSSIER:
{_j(dossier)}

{DATA_GUIDE}

{_decision_rules(dossier, extra)}

Return JSON: {{"specialists": {{"technical": {{"score": int, "points": [str]}}, "fundamental": {{"score": int, "points": [str]}},
"news_sentiment": {{"score": int, "points": [str]}}, "smart_money": {{"score": int, "points": [str]}}{role_json}, "best_style": str}},
"bull": {{"direction": "long"|"short", "case": str, "catalysts": [str]}},
"bear": {{"case": str, "risks": [str]}},
"decision": <the DECISION JSON>}}"""
    out = ai.ask(prompt, system=DESK, want_json=True, temperature=0.25, check=_good_quick)
    if not isinstance(out, dict) or not isinstance(out.get("decision"), dict):
        raise ValueError("quick desk returned an unexpected shape")
    return out


# ---- Second opinion (a different AI model that never saw the team's debate) -------------
SECOND_OPINION_SIZE = {"agree": 1.2, "disagree": 0.8, "strong_disagree": 0.6}


def second_opinion(dossier: dict, decision: dict, macro: dict) -> dict:
    """Raj Iyer, independent reviewer. Runs on a different AI model family (Groq first) and sees only the data and
    the proposed trade, not the debate, so the two models can catch each other's mistakes."""
    from desk import config
    side = decision.get("decision")
    prompt = f"""{_style("second")}You are an INDEPENDENT REVIEWER at a trading desk. You did not hear the team's debate.
Look only at the data and judge the proposed trade on its own merits. Be willing to disagree.

PROPOSED TRADE: {side} {dossier['symbol']} ({dossier['asset_type']}), entry {decision.get('entry')}, stop {decision.get('stop')},
target {decision.get('target')}, horizon {decision.get('horizon')}, conviction {decision.get('conviction')}/10.
MARKET BACKDROP: {macro.get('regime')} | {macro.get('summary')}

DATA:
{_j(dossier)}

{DATA_GUIDE}

Give your own view of the direction from -5 (strongly bearish) to +5 (strongly bullish), then a verdict on THIS trade:
"agree" (you'd take it), "disagree" (weak or poorly timed; take it smaller if at all), or "strong_disagree"
(you expect it to lose). Return JSON: {{"score": int, "verdict": "agree"|"disagree"|"strong_disagree",
"reason": str (1-2 sentences, citing the data)}}"""
    out = ai.ask(prompt, system=DESK, want_json=True, temperature=0.2,
                 models=config.GROQ_MODELS + config.GEMINI_BACKUP + config.GEMINI_MODELS,
                 check=lambda o: isinstance(o, dict) and o.get("verdict") in ("agree", "disagree", "strong_disagree"))
    score = int(out.get("score") or 0)
    # a "strong_disagree" only counts as a veto when the reviewer's own view points the other way
    if out["verdict"] == "strong_disagree" and not ((side == "long" and score <= -2) or (side == "short" and score >= 2)):
        out["verdict"] = "disagree"
    # a strong disagreement vetoes only a weak trade; a conviction 7+ trade goes ahead smaller
    if out["verdict"] == "strong_disagree" and int(decision.get("conviction") or 0) >= 7:
        out["overruled"] = True
    out["size_multiplier"] = SECOND_OPINION_SIZE.get(out["verdict"], 1.0)
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
                if rr < config.MIN_REWARD_TO_RISK - 0.01:   # small tolerance for rounded prices
                    problems.append(f"reward-to-risk is only {rr:.2f}")
        conf, rr = decision.get("confidence_pct"), decision.get("reward_to_risk")
        if isinstance(conf, (int, float)) and isinstance(rr, (int, float)) and 0 < conf < 100:
            ev = conf / 100 * rr - (1 - conf / 100)       # expected result in units of risk
            decision["expected_r"] = round(ev, 2)
            if ev < -0.05:
                problems.append(f"negative expected value ({ev:+.2f}R: {conf}% to make {rr}x the risk)")
        elif decision.get("premortem", {}).get("failure_more_likely"):
            problems.append("its own pre-mortem says the trade has negative expected value")
        if verified < 2:
            problems.append(f"only {verified} cited numbers checked out (need 2+)")
    return {"passed": not problems, "verified_facts": verified, "problems": problems}


# ---- Position sizing ---------------------------------------------------------
def size(decision: dict, equity: float, asset: str, quote_to_usd: float = 1.0, unit_usd: float | None = None,
         multiplier: float = 1.0) -> dict:
    """Bet more on high conviction. Returns the dollar risk and position size; risk limits re-check it later.
    For forex, prices are in the quote currency: `quote_to_usd` converts them, `unit_usd` is one unit's $ value."""
    c = int(decision.get("conviction") or 0)
    risk_pct = 4.5 if c >= 9 else 3.0 if c >= 7 else 2.0 if c >= 5 else 1.0
    # The strategy tournament and calibration scale bets up for styles that work, down for ones that don't.
    risk_pct = min(config.MAX_RISK_PER_TRADE_PCT, risk_pct * max(0.25, min(multiplier, 1.6)))
    entry, stop = float(decision["entry"]), float(decision["stop"])
    per_unit_risk = abs(entry - stop) * quote_to_usd
    unit_value = unit_usd if unit_usd is not None else entry
    risk_usd = equity * risk_pct / 100
    units = risk_usd / per_unit_risk
    notional = units * unit_value
    cap = equity * (config.MAX_FOREX_POSITION_X if asset == "forex" else config.MAX_POSITION_PCT / 100)  # risk limits re-check totals
    if notional > cap:
        units, notional = cap / unit_value, cap
        risk_usd = units * per_unit_risk
    return {"risk_pct": round(risk_usd / equity * 100, 2), "risk_usd": round(risk_usd, 2),
            "units": round(units, 6 if asset == "crypto" else 2), "notional_usd": round(notional, 2)}
