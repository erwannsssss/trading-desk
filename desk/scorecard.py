"""Grades every call the desk makes, so each person gets a hit rate.

Each researched idea is checked 3 trading days later: did the price move the way that person said it would?
  specialists   their score's sign (+ bullish, - bearish) vs the move
  the bull      the direction he argued for vs the move
  the bear      right when the bull's direction turned out wrong
  the PM        long/short: direction vs the move; pass: right if the bull's trade wouldn't have made > half a day's range
  the macro     bullish/bearish/choppy regime call vs the S&P 500's next day
  the scanner   finalists that actually moved more than one day's normal range
  fact-checker  blocked plans that would have lost money
Trading desk numbers come from real trades: fills, losses kept within plan, winners protected, options won, days within limits.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from desk import config, marketdata
from desk.state import now

FILE = config.DATA_DIR / "scorecard.json"
HORIZON_BARS = 3
WINDOW_DAYS = 30
MIN_SAMPLE = 3
NY = ZoneInfo(config.TIMEZONE)
LABELS = {
    "technical": "calls right", "fundamental": "calls right", "sentiment": "calls right", "smartmoney": "calls right",
    "bull": "calls right", "bear": "calls right", "pm": "decisions right", "macro": "market calls right",
    "scanner": "picks that moved", "factcheck": "blocks that saved money",
    "sizer": "losses kept within plan", "execution": "orders filled", "positions": "winners protected",
    "options": "options trades won", "risk": "days within the loss limit",
}
BULLISH = ("strong uptrend", "recovery")
BEARISH = ("risk-off/defensive", "panic/high volatility")


def _load() -> dict:
    return json.loads(FILE.read_text()) if FILE.exists() else {"graded": {}, "macro": {}}


def _ny_date(ts: str) -> str:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(NY).date().isoformat()


def _exit_price(bars: list[dict], created: datetime, asset: str) -> float | None:
    """Close of the 3rd daily bar after the research time, once that bar is finished."""
    if asset in ("stock", "etf"):
        day = created.date().isoformat()
        pre_open = (created.hour, created.minute) < (9, 30)
        after = [b for b in bars if (_ny_date(b["t"]) >= day if pre_open else _ny_date(b["t"]) > day)]
        if len(after) < HORIZON_BARS:
            return None
        bar = after[HORIZON_BARS - 1]
        if _ny_date(bar["t"]) >= now().date().isoformat():
            return None  # that day isn't over yet
        return bar["c"]
    after = [b for b in bars if datetime.fromisoformat(b["t"][:19] + "+00:00") > created]
    if len(after) < HORIZON_BARS + 1:  # +1 so the 3rd bar is complete
        return None
    return after[HORIZON_BARS - 1]["c"]


def grade_ideas(plans: list[dict]) -> dict:
    """Fill in price moves for ideas that are old enough, fetching bars in bulk. Returns the updated cache."""
    data = _load()
    graded = data["graded"]
    pending = []
    cutoff = (now() - timedelta(days=WINDOW_DAYS + 10)).isoformat()
    for plan in plans:
        if plan.get("created_at", "") < cutoff:
            continue
        created = datetime.fromisoformat(plan["created_at"])
        if now() - created < timedelta(days=2):
            continue
        for idea in plan.get("ideas", []):
            key = f"{plan['_file']}|{idea.get('symbol')}"
            if key in graded or not idea.get("price_at_research") or idea.get("status") == "error":
                continue
            pending.append((key, created, plan, idea))
    if pending:
        stocks = sorted({i["symbol"] for _, _, _, i in pending if i.get("asset") in ("stock", "etf")})
        cryptos = sorted({i["symbol"] for _, _, _, i in pending if i.get("asset") == "crypto"})
        fx = sorted({i["symbol"] for _, _, _, i in pending if i.get("asset") == "forex"})
        bars = {}
        try:
            if stocks:
                bars.update(marketdata.stock_daily_bars(stocks, days=WINDOW_DAYS + 20))
            if cryptos:
                bars.update(marketdata.crypto_daily_bars(cryptos, days=WINDOW_DAYS + 20))
            for inst in fx:
                bars[inst] = marketdata.forex_daily_bars(inst, WINDOW_DAYS + 20)
        except Exception as exc:
            print(f"scorecard bars failed: {exc}")
        for key, created, plan, idea in pending:
            exit_px = _exit_price(bars.get(idea["symbol"], []), created, idea.get("asset"))
            if exit_px is None:
                continue
            graded[key] = {"date": plan["created_at"][:10], "symbol": idea["symbol"], "asset": idea.get("asset"),
                           "status": idea.get("status"), "move_pct": round((exit_px / idea["price_at_research"] - 1) * 100, 3),
                           "atr_pct": idea.get("atr_pct") or 2.0,
                           "scores": {k: (v or {}).get("score") for k, v in (idea.get("specialists") or {}).items() if isinstance(v, dict)},
                           "bull": (idea.get("bull") or {}).get("direction"),
                           "pm": (idea.get("decision") or {}).get("decision"),
                           "finalist": True}
    _grade_macro(plans, data)
    data["updated"] = now().isoformat(timespec="minutes")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, indent=1))
    return data


def _grade_macro(plans: list[dict], data: dict) -> None:
    todo = [p for p in plans if p.get("session") in ("morning", "evening") and p.get("macro")
            and p["_file"] not in data["macro"] and now() - datetime.fromisoformat(p["created_at"]) > timedelta(days=1)]
    if not todo:
        return
    try:
        spy = marketdata.stock_daily_bars(["SPY"], days=WINDOW_DAYS + 20).get("SPY", [])
    except Exception:
        return
    for p in todo:
        created = datetime.fromisoformat(p["created_at"])
        day = created.date().isoformat()
        pre_open = (created.hour, created.minute) < (9, 30)
        idx = next((i for i, b in enumerate(spy) if (_ny_date(b["t"]) >= day if pre_open else _ny_date(b["t"]) > day)), None)
        if idx is None or idx == 0 or _ny_date(spy[idx]["t"]) >= now().date().isoformat():
            continue
        ret = (spy[idx]["c"] / spy[idx - 1]["c"] - 1) * 100
        regime = (p["macro"].get("regime") or "").lower()
        if regime in BULLISH:
            right = ret > 0
        elif regime in BEARISH:
            right = ret < 0
        else:
            right = abs(ret) < 0.6
        data["macro"][p["_file"]] = {"date": day, "regime": regime, "spy_next_day_pct": round(ret, 2), "right": right}


def _sign(x) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


def seat_scores(trades: list[dict], curve: list[dict] | None = None) -> dict:
    """{seat: {"right", "total", "pct", "label"}} over the last WINDOW_DAYS."""
    data = _load()
    since = (now() - timedelta(days=WINDOW_DAYS)).date().isoformat()
    tally = defaultdict(lambda: [0, 0])

    def add(seat, ok):
        tally[seat][0] += int(bool(ok))
        tally[seat][1] += 1

    spec_key = {"technical": "technical", "fundamental": "fundamental", "sentiment": "news_sentiment", "smartmoney": "smart_money"}
    for g in data["graded"].values():
        if g["date"] < since:
            continue
        move = g["move_pct"]
        for seat, k in spec_key.items():
            sc = g["scores"].get(k)
            if isinstance(sc, (int, float)) and sc != 0 and move != 0:
                add(seat, _sign(sc) == _sign(move))
        bull_dir = 1 if g.get("bull") == "long" else -1 if g.get("bull") == "short" else 0
        if bull_dir and move != 0:
            add("bull", bull_dir == _sign(move))
            add("bear", bull_dir != _sign(move))
        pm = g.get("pm")
        if pm in ("long", "short") and g["status"] == "approved" and move != 0:
            add("pm", (1 if pm == "long" else -1) == _sign(move))
        elif pm == "pass" and bull_dir:
            add("pm", bull_dir * move < 0.5 * g["atr_pct"])
        if g["status"] == "blocked_by_fact_check" and pm in ("long", "short"):
            add("factcheck", (1 if pm == "long" else -1) * move < 0)
        add("scanner", abs(move) > g["atr_pct"])
    for m in data["macro"].values():
        if m["date"] >= since:
            add("macro", m["right"])

    recent = [t for t in trades if (t.get("created_at") or "")[:10] >= since]
    for t in recent:
        if t.get("entry_order_id") and t.get("status") in ("open", "closed", "closing", "cancelled"):
            add("execution", t.get("entry_price") is not None)
        if t.get("status") == "closed" and t.get("pnl_usd") is not None:
            if t["pnl_usd"] < 0 and t.get("risk_usd"):
                add("sizer", abs(t["pnl_usd"]) <= t["risk_usd"] * 1.1)
            if (t.get("max_r") or 0) >= 1 and t.get("asset") != "option":
                add("positions", t["pnl_usd"] >= 0)
            if t.get("asset") == "option":
                add("options", t["pnl_usd"] > 0)
    if curve:
        days = {}
        for p in curve:
            if p["t"][:10] >= since:
                d = days.setdefault(p["t"][:10], [p["equity"], p["equity"]])
                d[1] = p["equity"]
        prev = None
        for day in sorted(days):
            start = prev if prev is not None else days[day][0]
            add("risk", (days[day][1] / start - 1) * 100 > -config.MAX_DAILY_LOSS_PCT)
            prev = days[day][1]

    return {seat: {"right": r, "total": n, "pct": round(r / n * 100) if n >= MIN_SAMPLE else None,
                   "label": LABELS.get(seat, "right")} for seat, (r, n) in tally.items()}
