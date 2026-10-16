"""The quant desk: three quants hunting systematic edges all day, every idea checked against the Kronos price model.

  Arjun Mehta (head of quant)   momentum and trend: the strongest stocks in clean uptrends, the weakest in downtrends
  Mateo Russo (stat arb)        mean reversion: prices stretched 2+ standard deviations from their 20-day average
  Sasha Kim (machine learning)  runs Kronos on every candidate, and pitches the moves Kronos is most confident in

Every candidate gets three independent pieces of evidence, all plain code and real numbers:
  1. the rule's own 2-year track record on THAT symbol (how often the same signal paid, in units of risk)
  2. Kronos' possible futures: how often the target is reached before the stop, and the expected result
  3. the usual technicals (trend, RSI, ATR, volume)
Then one AI call, the quant desk meeting, reads the shortlist and keeps or drops each trade with a written thesis.
Approved ideas go into a normal plan file, so the trading engine, risk limits and compliance treat them like any other.
"""
from __future__ import annotations

import json
import math
import time
import traceback
from datetime import timedelta

from desk import activity as floor
from desk import ai, config, marketdata
from desk.research import kronos, scanner, team, technicals
from desk.state import load, now

STYLE = {"momentum": "quant momentum", "reversion": "quant mean reversion", "kronos": "kronos forecast"}
SEAT = {"momentum": "quantdesk", "reversion": "statarb", "kronos": "mlquant"}
# stop and target in ATRs, and the most trading days to hold
LEVELS = {"momentum": (1.5, 3.0, 5), "reversion": (1.25, 2.0, 5), "kronos": (1.5, 2.5, 5)}
MIN_EDGE_R = 0.05            # combined expected result must beat this (in units of risk)
COOLDOWN_HOURS = 3
QUANT_SIZE = 0.75            # many smaller bets: a quant trade risks 3/4 of what the same conviction gets from the research desk
CALIBRATION = config.DATA_DIR / "kronos_backtest.json"
# leveraged and inverse ETFs decay over time and their charts don't behave like normal prices
LEVERAGED = {"SOXL", "SOXS", "TQQQ", "SQQQ", "UPRO", "SPXU", "SPXL", "SPXS", "TNA", "TZA", "LABU", "LABD", "UVXY", "SVXY",
             "NVDL", "NVDX", "TSLL", "TSLQ", "TSLZ", "MSTU", "MSTX", "CONL", "BITX", "FNGU", "FNGD", "YINN", "YANG", "BOIL",
             "KOLD", "UCO", "SCO", "NUGT", "DUST", "JNUG", "JDST", "TMF", "TMV", "SH", "PSQ", "SDS", "QID", "UVIX", "SVIX"}


# ---- signals -------------------------------------------------------------------
def _z20(closes: list[float]) -> float | None:
    if len(closes) < 21:
        return None
    w = closes[-20:]
    m = sum(w) / 20
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / 20)
    return (closes[-1] - m) / sd if sd else None


def _feat(bars: list[dict], i: int) -> dict | None:
    """Signal inputs at bar i using only data up to i (also used by the backtest)."""
    if i < 200:
        return None
    closes = [b["c"] for b in bars[:i + 1]]
    c = closes[-1]
    sma = lambda n: sum(closes[-n:]) / n
    gains = [max(0.0, closes[k] - closes[k - 1]) for k in range(len(closes) - 14, len(closes))]
    losses = [max(0.0, closes[k - 1] - closes[k]) for k in range(len(closes) - 14, len(closes))]
    rs = (sum(gains) / 14) / (sum(losses) / 14) if sum(losses) else 99
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars[i - 14:i], bars[i - 13:i + 1])]
    return {"c": c, "sma50": sma(50), "sma200": sma(200), "rsi": 100 - 100 / (1 + rs), "atr": sum(trs) / len(trs),
            "r20": (c / closes[-21] - 1) * 100, "r60": (c / closes[-61] - 1) * 100, "z20": _z20(closes)}


def signal(kind: str, f: dict) -> str | None:
    """'long', 'short' or None for one quant's rule."""
    if not f:
        return None
    if kind == "momentum":
        if f["r60"] > 15 and f["r20"] > 3 and f["c"] > f["sma50"] > f["sma200"] and 50 <= f["rsi"] <= 75:
            return "long"
        if f["r60"] < -12 and f["r20"] < -3 and f["c"] < f["sma50"] < f["sma200"] and 25 <= f["rsi"] <= 50:
            return "short"
    if kind == "reversion" and f["z20"] is not None:
        if f["z20"] <= -2 and f["c"] > f["sma200"] and f["rsi"] <= 38:
            return "long"
        if f["z20"] >= 2 and f["c"] < f["sma200"] and f["rsi"] >= 62:
            return "short"
    return None


def _atr_of(t: dict) -> float:
    """ATR in price units, rebuilt from the % figure so tiny-priced coins (PEPE, SHIB) don't round to zero."""
    return t["price"] * t["atr_pct"] / 100 if t.get("atr_pct") else (t.get("atr14") or 0.0)


def levels(kind: str, price: float, atr: float, long: bool) -> tuple[float, float]:
    s, t, _ = LEVELS[kind]
    return (price - s * atr, price + t * atr) if long else (price + s * atr, price - t * atr)


def rule_record(bars: list[dict], kind: str, lookback: int = 500) -> dict | None:
    """Replay this rule on this symbol's own history: every signal, traded with the same stop, target and time limit."""
    s_mult, t_mult, hold = LEVELS[kind]
    rr = t_mult / s_mult
    out, i, start = [], max(200, len(bars) - lookback), None
    i = max(200, len(bars) - lookback)
    while i < len(bars) - 1:
        f = _feat(bars, i)
        side = signal(kind, f)
        if not side:
            i += 1
            continue
        long = side == "long"
        entry, risk = f["c"], s_mult * f["atr"]
        stop, tgt = levels(kind, entry, f["atr"], long)
        res, j = None, i
        for j in range(i + 1, min(len(bars), i + 1 + hold)):
            b = bars[j]
            if (b["l"] <= stop) if long else (b["h"] >= stop):
                res = -1.0; break
            if (b["h"] >= tgt) if long else (b["l"] <= tgt):
                res = rr; break
        if res is None:
            res = ((bars[j]["c"] - entry) if long else (entry - bars[j]["c"])) / risk
        out.append(res)
        i = j + 1                 # one trade at a time, like the desk
    if not out:
        return None
    return {"signals": len(out), "win_pct": round(sum(r > 0 for r in out) / len(out) * 100),
            "avg_r": round(sum(out) / len(out), 2), "best_r": round(max(out), 2), "worst_r": round(min(out), 2)}


# ---- Kronos ---------------------------------------------------------------------
def kronos_weight() -> float:
    """How much to trust Kronos, from its own backtest on our kind of trades (0 = ignore, 1 = full weight)."""
    try:
        cal = json.loads(CALIBRATION.read_text())
        bt = cal.get("combined") or (cal["by_lookback"].get(str(kronos.LOOKBACK)) or next(iter(cal["by_lookback"].values())))["all"]
        best, hit = bt["avg_r_trading_its_call"], bt["direction_hit_pct"]
        cap = 0.8                             # a backtest alone never earns full trust
        live = cal.get("live") or {}
        if live.get("n", 0) >= 30:            # once there's enough live history, that decides
            best, hit, cap = live["avg_r"], live["direction_hit_pct"], 1.0
        return round(max(0.2, min(cap, 0.3 + best * 1.5 + (hit - 52) / 40)), 2)
    except Exception:
        return 0.5


def r_scale() -> float:
    """Kronos' simulated futures overstate how much a trade makes (the backtest found about 3x); this shrinks them."""
    try:
        cal = json.loads(CALIBRATION.read_text())
        return float((cal.get("combined") or {}).get("r_scale") or 0.3)
    except Exception:
        return 0.3


def kronos_r(paths: list[dict], long: bool, entry: float, stop: float, target: float) -> dict | None:
    """Simulate the trade along each possible future: target first = +reward, stop first = -1, else where it ends."""
    if not paths:
        return None
    risk = abs(entry - stop)
    rr = abs(target - entry) / risk
    rs = []
    for p in paths:
        res = None
        for hi, lo in zip(p["high"], p["low"]):
            if (lo <= stop) if long else (hi >= stop):
                res = -1.0; break
            if (hi >= target) if long else (lo <= target):
                res = rr; break
        if res is None:
            last = p["close"][-1]
            res = ((last - entry) if long else (entry - last)) / risk
        rs.append(res)
    n = len(rs)
    raw = sum(rs) / n
    return {"expected_r": round(raw * r_scale(), 2), "model_expected_r": round(raw, 2),
            "target_first_pct": round(sum(r >= rr for r in rs) / n * 100),
            "stop_first_pct": round(sum(r <= -1 for r in rs) / n * 100), "paths": n}


# ---- universe ---------------------------------------------------------------------
def _universe(stocks_open: bool) -> list[dict]:
    """[{symbol, asset, venue, bars, live}] for liquid stocks/ETFs (when the market is open), all crypto and forex."""
    out = []
    if stocks_open:
        syms = set(scanner.STOCKS) | (set(scanner.ETFS) - {"SH", "PSQ", "UUP"})
        try:
            syms |= {a["symbol"] for a in marketdata.most_actives(40) if scanner._clean(a["symbol"])}
            mv = marketdata.movers(30)
            syms |= {g["symbol"] for g in mv["gainers"] + mv["losers"] if (g.get("price") or 0) >= scanner.MIN_PRICE and scanner._clean(g["symbol"])}
        except Exception as exc:
            print(f"quant: screener failed: {exc}")
        bars = marketdata.stock_daily_bars(sorted(syms), days=900)
        snaps = marketdata.stock_snapshots(sorted(bars))
        for s, b in bars.items():
            if len(b) < 260 or s in LEVERAGED:
                continue
            live = (snaps.get(s) or {}).get("price")
            t = technicals.analyze(b, live, live_replaces_last=scanner._ny_date(b[-1]["t"]) == now().date().isoformat())
            if not t or t["price"] < scanner.MIN_PRICE or (t.get("avg_dollar_vol_20d") or 0) < scanner.MIN_DOLLAR_VOLUME:
                continue
            if live and scanner._ny_date(b[-1]["t"]) != now().date().isoformat():
                b = b + [{"t": now().strftime("%Y-%m-%dT04:00:00Z"), "o": live, "h": live, "l": live, "c": live, "v": 0}]   # today so far
            out.append({"symbol": s, "asset": "etf" if s in scanner.ETFS else "stock", "venue": "alpaca", "bars": b, "t": t})
    try:
        cb = marketdata.crypto_daily_bars(scanner.CRYPTO, days=900)
        px = marketdata.crypto_prices(scanner.CRYPTO)
        for s, b in cb.items():
            if len(b) >= 260:
                t = technicals.analyze(b, px.get(s), live_replaces_last=True)
                if t:
                    out.append({"symbol": s, "asset": "crypto", "venue": "alpaca", "bars": b, "t": t})
    except Exception as exc:
        print(f"quant: crypto bars failed: {exc}")
    for inst in scanner.FOREX:
        try:
            b = marketdata.forex_daily_bars(inst, 700)
            t = technicals.analyze(b)
            if t and len(b) >= 260:
                out.append({"symbol": inst, "asset": "forex", "venue": "oanda", "bars": b, "t": t})
        except Exception as exc:
            print(f"quant: {inst} failed: {exc}")
    return out


# ---- the desk ----------------------------------------------------------------------
def _candidates(universe: list[dict], skip: set[str]) -> list[dict]:
    out = []
    for u in universe:
        if u["symbol"] in skip:
            continue
        f = _feat(u["bars"], len(u["bars"]) - 1)
        if not f or not _atr_of(u["t"]):
            continue
        for kind in ("momentum", "reversion"):
            side = signal(kind, f)
            if side == "short" and u["asset"] == "crypto":
                continue        # crypto is long-only here
            if side:
                out.append({**u, "kind": kind, "side": side, "f": f})
    return out


def _pace(state: dict) -> dict:
    """Where the desk is against its ~30 orders a day goal (a target to aim for, not a quota)."""
    goal = config.DAILY_TRADE_GOAL
    placed = state.get("trades_today", 0) if state.get("day") == now().date().isoformat() else 0
    hour = now().hour + now().minute / 60
    # most orders happen in market hours: count the day as 30% overnight/evening, 70% 9:30-16:00
    frac = 0.3 * min(1, hour / 24) + 0.7 * max(0.0, min(1.0, (hour - 9.5) / 6.5))
    expected = round(goal * frac)
    return {"goal": goal, "placed_today": placed, "expected_by_now": expected, "behind_by": max(0, expected - placed)}


def _idea(c: dict, kind: str, side: str, kz: dict | None, kr: dict | None, record: dict | None, w: float) -> dict:
    """Build the idea in the same shape the research desk uses, with the numbers that justify it."""
    t = c["t"]
    price, atr = t["price"], _atr_of(t)
    long = side == "long"
    stop, target = levels(kind, price, atr, long)
    s_mult, t_mult, _ = LEVELS[kind]
    rr = t_mult / s_mult
    hist_r = record["avg_r"] if record and record["signals"] >= 5 else None
    kz_r = kr["expected_r"] if kr else None
    parts = [(hist_r, 1.0 if record and record["signals"] >= 12 else 0.6)] if hist_r is not None else []
    if kz_r is not None:
        parts.append((kz_r, w))
    if parts:
        parts.append((0.0, 0.5))   # shrink toward zero: a small sample or a half-trusted model shouldn't look like a sure thing
    edge = sum(v * wt for v, wt in parts) / sum(wt for _, wt in parts) if parts else None
    conviction = 5
    if edge is not None:
        conviction += (edge >= 0.2) + (edge >= 0.45)
    if hist_r is not None and kz_r is not None and hist_r > 0.15 and kz_r > 0.15:
        conviction += 1                                     # history and Kronos agree
    if kz_r is not None and kz_r < 0 and w >= 0.5:
        conviction -= 1
    conviction = max(3, min(8, conviction))
    conf = round(max(5, min(95, ((edge if edge is not None else 0) + 1) / (rr + 1) * 100)))
    rnd = lambda x: round(x, 8 if price < 1 else 6 if price < 5 else 5 if c["asset"] == "forex" else 2)
    dossier = {"symbol": c["symbol"], "asset_type": c["asset"], "technicals": t,
               "kronos_forecast": kronos.public(kz), "kronos_odds": kr, "quant_backtest": record}
    evidence = [{"fact": "technicals.ret_20d", "value": t.get("ret_20d")}, {"fact": "technicals.rsi14", "value": t.get("rsi14")},
                {"fact": "technicals.atr_pct", "value": t.get("atr_pct")}]
    if record:
        evidence.append({"fact": "quant_backtest.avg_r", "value": record["avg_r"]})
    if kr:
        evidence.append({"fact": "kronos_odds.expected_r", "value": kr["expected_r"]})
    who = {"momentum": "a momentum signal", "reversion": "a stretched price likely to snap back", "kronos": "a strong Kronos forecast"}[kind]
    hist_txt = (f"the same signal on {c['symbol']} over 2 years: {record['signals']} trades, {record['win_pct']}% winners, "
                f"{record['avg_r']:+.2f}R on average" if record else "no history for this signal")
    kz_txt = (f"Kronos: target first in {kr['target_first_pct']}% of {kr['paths']} simulated futures, stop first in "
              f"{kr['stop_first_pct']}%, expected {kr['expected_r']:+.2f}R" if kr else "Kronos unavailable")
    decision = {
        "decision": side, "style": STYLE[kind], "bucket": "tactical", "order_type": "limit",
        "expression": "put_options" if (not long and c["asset"] in ("stock", "etf")) else "shares",
        "entry": rnd(price), "stop": rnd(stop), "target": rnd(target), "conviction": conviction, "confidence_pct": conf,
        "horizon": "1-5 days", "reason": f"Quant desk: {who}. {hist_txt}; {kz_txt}.",
        "thesis": f"{who.capitalize()} on {c['symbol']}. {hist_txt.capitalize()}. {kz_txt}.",
        "premortem": {"failure_story": "the signal fails as it does in the losing share of its history", "failure_more_likely": False},
        "evidence": [e for e in evidence if isinstance(e["value"], (int, float))],
        "quant_edge_r": round(edge, 2) if edge is not None else None,
    }
    return {"symbol": c["symbol"], "venue": c["venue"], "asset": c["asset"], "price_at_research": price,
            "atr_pct": t.get("atr_pct"), "setups": t.get("setups"), "why_scanned": [f"{STYLE[kind]} signal ({SEAT[kind]})"],
            "decision": decision, "_dossier": dossier, "quant": {"kind": kind, "seat": SEAT[kind], "edge_r": decision["quant_edge_r"],
                                                               "history": record, "kronos": kr, "kronos_weight": w},
            "specialists": {**({"kronos": {"score": _kronos_score(kz), "points": [kz_txt]}} if kz else {}),
                            **({f"quant_{kind}": {"score": 3 if long else -3, "points": [hist_txt]}} if kind != "kronos" else {})}}


def _kronos_score(kz: dict | None) -> int | None:
    """Kronos' view as a -5..+5 analyst score (scored on the scorecard like everyone else)."""
    if not kz:
        return None
    s = kz["expected_move_pct"] / max(0.5, kz["range_width_pct"] / 4)
    return int(max(-5, min(5, round(s * 2.5))))


def meeting(shortlist: list[dict], macro: dict | None, pace: dict) -> dict:
    """One AI call: the three quants review the shortlist together and keep or drop each trade."""
    rows = [{"n": n, "symbol": i["symbol"], "asset": i["asset"], "quant": i["quant"]["seat"], "style": i["decision"]["style"],
             "direction": i["decision"]["decision"], "entry": i["decision"]["entry"], "stop": i["decision"]["stop"],
             "target": i["decision"]["target"], "edge_r": i["quant"]["edge_r"], "history": i["quant"]["history"],
             "kronos_odds": i["quant"]["kronos"], "kronos_forecast": i["_dossier"].get("kronos_forecast"),
             "technicals": {k: i["_dossier"]["technicals"].get(k) for k in ("ret_5d", "ret_20d", "ret_60d", "rsi14", "atr_pct", "trend", "vol_ratio")}}
            for n, i in enumerate(shortlist)]
    prompt = f"""You are the QUANT DESK of a small aggressive trading firm: Arjun Mehta (head of quant, momentum and
trend), Mateo Russo (statistical arbitrage, mean reversion) and Sasha Kim (machine learning, runs the Kronos price
model). Every trade below passed a systematic rule and comes with three pieces of evidence: the rule's own 2-year
record on that symbol ("history": avg_r is the average result in units of risk), Kronos' simulated futures
("kronos_odds": how often the target is hit before the stop; expected_r), and the technicals. edge_r blends the two.
Kronos is trusted at weight {shortlist[0]['quant']['kronos_weight'] if shortlist else 0.5} out of 1 based on its own backtest.

MARKET: {(macro or {}).get('regime')} | {(macro or {}).get('summary', '')}
DAILY VOLUME: the firm aims for about {pace['goal']} orders a day; {pace['placed_today']} placed so far
(expected about {pace['expected_by_now']} by now). Keep every trade with a real positive edge; drop only the ones
where the evidence conflicts badly, the move already happened, or the market backdrop makes it a bad bet.

TRADES:
{json.dumps(rows, default=str)}

For each trade: "keep" or "drop", conviction (you may move it by at most 1 from the edge: 3-8), a 2-3 sentence
thesis in the voice of the quant who owns it, and a plain_english line for a beginner (define any term in brackets).
Return JSON: {{"calls": [{{"n": int, "verdict": "keep"|"drop", "conviction": int, "thesis": str, "plain_english": str}}],
"desk_note": str (2 sentences from Arjun on what the quant desk is seeing today)}}"""
    return ai.ask(prompt, system=team.DESK, want_json=True, temperature=0.3, models=config.AI_LIGHT,
                  check=lambda o: isinstance(o, dict) and isinstance(o.get("calls"), list))


def run(markets: str = "all") -> dict:
    """One quant desk pass. markets: 'all' (stocks only when the market is open) or 'crypto_fx'."""
    from desk.portfolio import snapshot
    from desk.research import planner
    from desk.trading import ledger
    from desk import learning
    started = time.monotonic()
    state = load()
    floor.start_job("quant", "Quant desk")
    book, _ = snapshot(state)
    try:
        from desk.brokers import alpaca
        clk = alpaca.clock()
        stocks_open = markets == "all" and clk.get("is_open")
    except Exception:
        stocks_open = False
    pace = _pace(state)
    active = set(ledger.active_symbols(ledger.load()))
    skip = active | {i["symbol"] for _, i in planner.recent_ideas(COOLDOWN_HOURS)}
    skip |= {i["symbol"] for _, i in planner.recent_ideas(12) if i.get("status") == "approved"}

    floor.working("quantdesk", "Pulling 3 years of daily prices for every liquid stock, ETF, crypto and currency pair")
    uni = _universe(bool(stocks_open))
    cands = _candidates(uni, skip)
    floor.done("quantdesk", f"Scanned {len(uni)} markets: {sum(c['kind'] == 'momentum' for c in cands)} momentum and "
               f"{sum(c['kind'] == 'reversion' for c in cands)} mean-reversion signals")

    # Kronos: every rule candidate, plus the most active names for Sasha's own forecasts
    w = kronos_weight()
    rest = [u for u in uni if u["symbol"] not in skip and u["symbol"] not in {c["symbol"] for c in cands}]
    extra = (sorted([u for u in rest if u["asset"] in ("stock", "etf")], key=lambda u: -(u["t"].get("interest") or 0))[:20]
             + [u for u in rest if u["asset"] in ("crypto", "forex")])          # every coin and currency pair, every run
    pool = {c["symbol"]: c for c in cands}
    for u in extra:
        pool.setdefault(u["symbol"], u)
    floor.working("mlquant", f"Running Kronos on {len(pool)} markets: {kronos.PATHS} possible futures each")
    fc = kronos.forecast({s: (u["bars"], u["asset"]) for s, u in pool.items()}, steps=5) if pool else {}
    if fc:
        ups = sorted(((s, f["expected_move_pct"]) for s, f in fc.items()), key=lambda x: -x[1])
        floor.done("mlquant", f"Kronos done on {len(fc)} markets (trust weight {w}). Most bullish: "
                   + ", ".join(f"{s} {m:+.1f}%" for s, m in ups[:3]) + ". Most bearish: "
                   + ", ".join(f"{s} {m:+.1f}%" for s, m in ups[-3:][::-1]))
    else:
        floor.done("mlquant", "Kronos isn't available on this run; the desk trades on the backtested rules alone")
    _log_forecasts(fc, pool)

    ideas = []
    floor.working("statarb", "Backtesting each signal on its own symbol's 2-year history")
    for c in cands:
        kz = fc.get(c["symbol"])
        long = c["side"] == "long"
        stop, target = levels(c["kind"], c["t"]["price"], _atr_of(c["t"]), long)
        kr = kronos_r(kz["_paths"], long, c["t"]["price"], stop, target) if kz else None
        rec = rule_record(c["bars"], c["kind"])
        ideas.append(_idea(c, c["kind"], c["side"], kz, kr, rec, w))
    for s, kz in fc.items():                 # Sasha: Kronos' own high-confidence calls
        u = pool[s]
        # only confident calls: 70%+ of futures on one side (the one group that paid in both backtests, ~+0.16R)
        if s in {c["symbol"] for c in cands} or not (kz["prob_up_pct"] >= 70 or kz["prob_up_pct"] <= 30):
            continue
        long = kz["prob_up_pct"] >= 70
        if not long and u["asset"] == "crypto":
            continue
        if not _atr_of(u["t"]):
            continue
        stop, target = levels("kronos", u["t"]["price"], _atr_of(u["t"]), long)
        kr = kronos_r(kz["_paths"], long, u["t"]["price"], stop, target)
        if kr and kr["expected_r"] >= 0.1:
            ideas.append(_idea(u, "kronos", "long" if long else "short", kz, kr, None, w))
    floor.done("statarb", f"{len(ideas)} trades have a backtest and a Kronos read")

    # rank by edge, keep the ones with a real positive edge, then the desk meeting decides
    ideas = [i for i in ideas if (i["quant"]["edge_r"] or -1) > MIN_EDGE_R]
    ideas.sort(key=lambda i: -(i["quant"]["edge_r"] or 0))
    room = 4 + pace["behind_by"] // 2 + (2 if stocks_open else 0)
    shortlist = ideas[:min(16, room * 2)]
    macro = planner.latest_macro()
    calls, note = {}, ""
    if shortlist:
        for seat in ("quantdesk", "statarb", "mlquant"):
            floor.working(seat, f"Quant desk meeting: {len(shortlist)} trades on the table", None)
        try:
            out = meeting(shortlist, macro, pace)
            calls = {int(c.get("n", -1)): c for c in out.get("calls", []) if isinstance(c, dict)}
            note = out.get("desk_note") or ""
        except Exception as exc:
            print(f"quant meeting failed: {exc}; keeping trades on the numbers alone")
    approved, kronos_side = 0, {}
    for n, i in enumerate(shortlist):
        d = i["decision"]
        c = calls.get(n) or {}
        if c.get("verdict") == "drop":
            i["status"] = "pass"
            d["decision_by_desk"] = "dropped at the quant desk meeting"
            d["reason"] = (c.get("thesis") or "") + " " + d["reason"]
        else:
            if isinstance(c.get("conviction"), int):
                d["conviction"] = max(3, min(8, d["conviction"] + 1, max(d["conviction"] - 1, c["conviction"])))
            if c.get("thesis"):
                d["thesis"] = c["thesis"] + " " + d["thesis"]
            d["plain_english"] = c.get("plain_english") or d["reason"]
            check = team.fact_check(d, _facts(i["_dossier"]), i["_dossier"])
            i["fact_check"] = check
            if not check["passed"]:
                i["status"] = "blocked_by_fact_check"
            elif d["style"] == STYLE["kronos"] and kronos_side.get(d["decision"], 0) >= 2:
                i["status"] = "pass"
                d["reason"] = "already 2 Kronos-only trades this way in this run; " + d["reason"]
            elif approved >= room:
                i["status"] = "pass"
                d["reason"] = "kept by the desk but over this run's trade budget; " + d["reason"]
            else:
                kw = {}
                if i["asset"] == "forex":
                    from desk.brokers import oanda
                    kw = {"quote_to_usd": planner.quote_to_usd(i["symbol"]), "unit_usd": oanda.usd_value_per_unit(i["symbol"])}
                kw["multiplier"] = QUANT_SIZE * learning.risk_multiplier(d["style"])
                i["sizing"] = team.size(d, book.equity, i["asset"], **kw)
                i["status"] = "approved"
                approved += 1
                if d["style"] == STYLE["kronos"]:
                    kronos_side[d["decision"]] = kronos_side.get(d["decision"], 0) + 1
        seat = i["quant"]["seat"]
        verdict = (f"{d['decision'].upper()} {i['symbol']}, conviction {d['conviction']}/10, edge {i['quant']['edge_r']:+.2f}R"
                   if i["status"] == "approved" else f"{i['symbol']}: {i['status'].replace('_', ' ')}")
        floor.event(seat, verdict, i["symbol"], kind="decision" if i["status"] == "approved" else "pass")
    for i in ideas[len(shortlist):]:
        i["status"] = "pass"
        i["decision"]["reason"] = "below the cut for this run; " + i["decision"]["reason"]
    for i in ideas:
        i.pop("_dossier", None)
    for seat in ("quantdesk", "statarb", "mlquant"):
        floor.done(seat, note or f"{approved} trades sent to the trading desk")

    stamp = now()
    plan = {"date": stamp.date().isoformat(), "session": "quant", "created_at": stamp.isoformat(timespec="seconds"),
            "dry_run": state.get("dry_run", True), "equity": round(book.equity, 2), "macro": macro,
            "ideas": shortlist, "quant_summary": {"markets": len(uni), "signals": len(cands), "kronos": len(fc),
                                                  "kronos_weight": w, "candidates_with_edge": len(ideas), "pace": pace,
                                                  "desk_note": note},
            "ai_calls": ai.calls_made, "minutes": round((time.monotonic() - started) / 60, 1)}
    planner.PLANS_DIR.mkdir(parents=True, exist_ok=True)
    (planner.PLANS_DIR / f"{plan['date']}-quant-{stamp.strftime('%H%M')}.json").write_text(json.dumps(plan, indent=1, default=str))
    floor.event("quantdesk", f"Sent {approved} quant trade{'s' if approved != 1 else ''} to the trading desk "
                f"({len(ideas)} had an edge, {len(uni)} markets scanned)", kind="plan")
    floor.finish(f"quant desk: {approved} approved")
    if approved:
        _telegram(plan)
    return plan


def _facts(d: dict) -> dict:
    from desk.research import dossier as dossiers
    return dossiers.facts(d)


def _telegram(plan: dict) -> None:
    from desk import telegram
    from desk.telegram import esc
    ok = [i for i in plan["ideas"] if i.get("status") == "approved"]
    lines = [f"<b>Quant desk: {len(ok)} new trade{'s' if len(ok) != 1 else ''}</b>"]
    for i in ok:
        d, q = i["decision"], i["quant"]
        k = q.get("kronos") or {}
        lines.append(f"{d['decision'].upper()} {esc(i['symbol'])} | {esc(d['style'])} | conv {d['conviction']} | edge {q['edge_r']:+.2f}R"
                     + (f" | Kronos {k.get('target_first_pct')}% target first" if k else ""))
    if plan["quant_summary"].get("desk_note"):
        lines.append(f"<i>{esc(plan['quant_summary']['desk_note'])}</i>")
    telegram.send("\n".join(lines), silent=True)


# ---- Kronos' live track record -------------------------------------------------------
LOG = config.DATA_DIR / "kronos_log.jsonl"


def _log_forecasts(fc: dict, pool: dict) -> None:
    """Every forecast is saved, then graded 5 trading days later (learning.kronos_record)."""
    if not fc:
        return
    with LOG.open("a") as f:
        for s, k in fc.items():
            f.write(json.dumps({"t": now().isoformat(timespec="minutes"), "symbol": s, "asset": pool[s]["asset"],
                                "price": pool[s]["t"]["price"], "atr_pct": pool[s]["t"].get("atr_pct"),
                                "exp": k["expected_move_pct"], "up": k["prob_up_pct"], "days": k["horizon_days"]}) + "\n")


def kronos_record(days: int = 30) -> dict | None:
    """Grade Kronos' logged forecasts against what happened (one per symbol per day), and feed its trust weight."""
    if not LOG.exists():
        return None
    since = (now() - timedelta(days=days)).isoformat()
    rows, seen = [], set()
    for line in LOG.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        key = (r["symbol"], r["t"][:10])
        if r["t"] >= since and key not in seen:
            seen.add(key)
            rows.append(r)
    due = [r for r in rows if now() - __import__("datetime").datetime.fromisoformat(r["t"]) >= timedelta(days=r["days"] * 7 / 5 + 1)]
    if not due:
        return {"n": 0}
    by_asset: dict[str, list] = {}
    for r in due:
        by_asset.setdefault(r["asset"], []).append(r)
    bars = {}
    try:
        st = sorted({r["symbol"] for r in due if r["asset"] in ("stock", "etf")})
        if st:
            bars.update(marketdata.stock_daily_bars(st, days=days + 20))
        cr = sorted({r["symbol"] for r in due if r["asset"] == "crypto"})
        if cr:
            bars.update(marketdata.crypto_daily_bars(cr, days=days + 20))
        for s in {r["symbol"] for r in due if r["asset"] == "forex"}:
            bars[s] = marketdata.forex_daily_bars(s, days + 20)
    except Exception as exc:
        print(f"kronos record bars failed: {exc}")
    hits, rs = 0, []
    for r in due:
        b = [x for x in bars.get(r["symbol"], []) if x["t"][:10] > r["t"][:10]]
        if len(b) < r["days"]:
            continue
        move = (b[r["days"] - 1]["c"] / r["price"] - 1) * 100
        if r["exp"] == 0 or move == 0:
            continue
        hits += (r["exp"] > 0) == (move > 0)
        risk = 1.5 * (r.get("atr_pct") or 2.0)
        rs.append(max(-1.0, min(2.5 / 1.5, (move if r["exp"] > 0 else -move) / risk)))
    if not rs:
        return {"n": 0}
    rec = {"n": len(rs), "direction_hit_pct": round(hits / len(rs) * 100, 1), "avg_r": round(sum(rs) / len(rs), 3)}
    try:
        cal = json.loads(CALIBRATION.read_text()) if CALIBRATION.exists() else {"by_lookback": {}}
        cal["live"] = rec
        CALIBRATION.write_text(json.dumps(cal, indent=1))
    except Exception:
        traceback.print_exc()
    return rec
