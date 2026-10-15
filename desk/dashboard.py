"""Builds docs/data.json, the file the dashboard website reads. Runs after every tick and research session."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from statistics import mean, pstdev

from desk import activity, config, marketdata
from desk.brokers import oanda
from desk.portfolio import snapshot
from desk.research.planner import PLANS_DIR
from desk.risk import daily_loss_pct
from desk.state import now
from desk.trading import ledger

DOCS = config.ROOT / "docs"

# New York times (summer schedule) for the trading floor's "next up" labels
SCHEDULE = [
    {"session": "morning", "time": "07:40", "days": "weekdays", "who": "Research desk, full team"},
    {"session": "intraday", "time": "10:20", "days": "weekdays", "who": "Research desk, quick huddle"},
    {"session": "intraday", "time": "11:50", "days": "weekdays", "who": "Research desk, quick huddle"},
    {"session": "intraday", "time": "13:20", "days": "weekdays", "who": "Research desk, quick huddle"},
    {"session": "intraday", "time": "14:50", "days": "weekdays", "who": "Research desk, quick huddle"},
    {"session": "evening", "time": "18:10", "days": "daily", "who": "Research desk, crypto and forex"},
    {"session": "overnight", "time": "22:10", "days": "daily", "who": "Research desk, crypto and forex"},
    {"session": "overnight", "time": "02:10", "days": "daily", "who": "Research desk, crypto and forex"},
    {"session": "weekly", "time": "19:30", "days": "sunday", "who": "Learning desk, weekly review"},
]


def build(state: dict) -> None:
    book, details = snapshot(state)
    trades = ledger.load()
    curve = _curve()
    plans = _recent_plans(days=10)

    data = {
        "generated_at": now().isoformat(timespec="seconds"),
        "mode": "paused" if state.get("paused") else ("dry_run" if state.get("dry_run") else "live"),
        "halted_for_day": state.get("halted_for_day", False),
        "account": {
            "equity": round(book.equity, 2),
            "start": config.STARTING_CAPITAL,
            "total_return_pct": round((book.equity / config.STARTING_CAPITAL - 1) * 100, 2),
            "today_pct": round(-daily_loss_pct(book, state.get("day_start_equity") or book.equity), 2) + 0.0,
            "stock_exposure": round(book.stock_gross_exposure, 2),
            "forex_exposure": round(book.forex_gross_notional, 2),
            "open_positions": book.open_positions,
            "trades_today": state.get("trades_today", 0),
            "max_trades": config.MAX_TRADES_PER_DAY,
            "since": state.get("created_at"),
        },
        "limits": {"daily_loss_pct": config.MAX_DAILY_LOSS_PCT, "max_position_pct": config.MAX_POSITION_PCT,
                   "max_risk_per_trade_pct": config.MAX_RISK_PER_TRADE_PCT, "max_open_positions": config.MAX_OPEN_POSITIONS},
        "curve": curve,
        "positions": _positions(trades, details),
        "trades": [_slim(t) for t in sorted(trades, key=lambda t: t.get("created_at", ""), reverse=True)],
        "memos": _memos(trades, plans),
        "plan": _latest_plan(plans),
        "stats": _stats(trades, curve),
        "ghost": _ghost(plans, trades),
        "rulebook": _json(config.DATA_DIR / "rulebook.json", []),
        "tournament": _json(config.DATA_DIR / "tournament.json", None),
        "roster": activity.roster(),
        "schedule": SCHEDULE,
        "backtest": _json(config.DATA_DIR / "backtest.json", None),
        "sessions_today": [{"session": p.get("session"), "created_at": p.get("created_at"),
                            "approved": sum(i.get("status") == "approved" for i in p.get("ideas", [])),
                            "researched": len(p.get("ideas", []))} for p in plans if p.get("date") == now().date().isoformat()],
        "letters": _json(config.DATA_DIR / "letters.json", []),
    }
    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.json").write_text(json.dumps(data, default=str, separators=(",", ":")))


# ---- pieces ------------------------------------------------------------------
def _json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _curve(max_points: int = 600) -> list[dict]:
    path = config.DATA_DIR / "equity.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    if not rows:
        return []
    step = max(1, math.ceil(len(rows) / max_points))
    rows = rows[::step] + ([rows[-1]] if (len(rows) - 1) % step else [])
    spy0, eq0 = rows[0]["spy"], config.STARTING_CAPITAL
    return [{"t": r["t"], "equity": r["equity"], "spy": round(eq0 * r["spy"] / spy0, 2)} for r in rows]


def _recent_plans(days: int) -> list[dict]:
    if not PLANS_DIR.exists():
        return []
    cutoff = (now().date() - timedelta(days=days)).isoformat()
    out = []
    for p in sorted(PLANS_DIR.glob("*.json")):
        if p.name[:10] >= cutoff:
            plan = json.loads(p.read_text())
            plan["_file"] = p.name
            out.append(plan)
    return sorted(out, key=lambda p: p.get("created_at", ""))


def _positions(trades: list[dict], details: dict) -> list[dict]:
    out = []
    for t in trades:
        if t["status"] not in ("open", "pending", "closing"):
            continue
        last = t.get("last_price")
        entry = t.get("entry_price") or t["entry_plan"]
        long = t["direction"] == "long"
        unreal = None
        if t.get("asset") == "option":
            entry = t.get("underlying_entry") or t["entry_plan"]
            if t.get("option_mark") and t.get("entry_price"):
                unreal = round((t["option_mark"] - t["entry_price"]) * t["filled_qty"] * 100, 2)
        elif last and t.get("entry_price"):
            unreal = round((last - entry) * t["filled_qty"] * (1 if long else -1) * t.get("quote_to_usd", 1.0) + t.get("partial_pnl", 0), 2)
        progress = None
        if last:
            span = t["target"] - entry
            progress = round(max(-1.0, min(1.0, (last - entry) / span)), 3) if span else None
        out.append({**_slim(t), "last_price": last, "unrealized_usd": unreal, "progress": progress})
    return out


SLIM_KEYS = ("id", "plan_file", "symbol", "asset", "venue", "direction", "status", "bucket", "style", "conviction",
             "confidence_pct", "horizon", "thesis", "entry_plan", "entry_price", "initial_stop", "stop", "target", "qty",
             "filled_qty", "notional_usd", "risk_usd", "risk_pct", "created_at", "opened_at", "closed_at", "exit_price",
             "exit_reason", "pnl_usd", "r_result", "skip_reason", "max_r", "notes", "partial_pnl", "postmortem",
             "option_symbol", "option_type", "strike", "expiry", "premium_paid", "option_mark", "underlying_entry")


def _slim(t: dict) -> dict:
    return {k: t.get(k) for k in SLIM_KEYS if t.get(k) is not None}


def _memo(idea: dict) -> dict:
    return {k: idea.get(k) for k in ("symbol", "asset", "status", "price_at_research", "why_scanned", "specialists",
                                     "bull", "bear", "decision", "fact_check", "sizing")}


def _memos(trades: list[dict], plans: list[dict]) -> dict:
    """Full research memo for every trade, keyed by plan file + symbol."""
    wanted = {(t["plan_file"], t["symbol"]) for t in trades}
    for plan in plans[-12:]:  # every idea in the recent sessions shown on the game plan tab
        wanted |= {(plan["_file"], i.get("symbol")) for i in plan.get("ideas", [])}
    out = {}
    for plan in plans:
        for idea in plan.get("ideas", []):
            if (plan["_file"], idea.get("symbol")) in wanted and idea.get("decision"):
                out[f"{plan['_file']}|{idea['symbol']}"] = _memo(idea)
    for (pf, sym) in wanted:  # trades from plans older than the window
        key = f"{pf}|{sym}"
        if key not in out and (PLANS_DIR / pf).exists():
            plan = json.loads((PLANS_DIR / pf).read_text())
            idea = next((i for i in plan.get("ideas", []) if i.get("symbol") == sym), None)
            if idea and idea.get("decision"):
                out[key] = _memo(idea)
    return out


def _latest_plan(plans: list[dict]) -> dict | None:
    """The latest full research session, plus every idea from the fast sessions that ran after it."""
    if not plans:
        return None
    full = [p for p in plans if p.get("session") in ("morning", "evening")]
    p = full[-1] if full else plans[-1]
    following = [q for q in plans if q.get("created_at", "") > p.get("created_at", "") and q is not p]
    ideas = []
    for plan in [p] + following:
        for i in plan.get("ideas", []):
            d = i.get("decision") or {}
            ideas.append({"symbol": i.get("symbol"), "asset": i.get("asset"), "status": i.get("status"),
                          "session": plan.get("session"), "at": plan.get("created_at"),
                          "decision": d.get("decision"), "expression": d.get("expression"), "conviction": d.get("conviction"),
                          "bucket": d.get("bucket"), "style": d.get("style"), "entry": d.get("entry"), "stop": d.get("stop"),
                          "target": d.get("target"), "reward_to_risk": d.get("reward_to_risk"), "thesis": d.get("thesis"),
                          "reason": d.get("reason"), "sizing": i.get("sizing"),
                          "problems": (i.get("fact_check") or {}).get("problems"),
                          "error": i.get("error"), "memo_key": f"{plan['_file']}|{i.get('symbol')}"})
    return {"file": p["_file"], "date": p.get("date"), "session": p.get("session"), "created_at": p.get("created_at"),
            "macro": p.get("macro"), "calendar": (p.get("macro_inputs") or {}).get("calendar_next_10_days"),
            "economy": (p.get("macro_inputs") or {}).get("economy"), "ideas": ideas,
            "later_sessions": len(following),
            "ai_calls": sum(q.get("ai_calls") or 0 for q in [p] + following),
            "minutes": round(sum(q.get("minutes") or 0 for q in [p] + following), 1)}


def _stats(trades: list[dict], curve: list[dict]) -> dict:
    closed = [t for t in trades if t["status"] == "closed" and t.get("pnl_usd") is not None]
    wins = [t["pnl_usd"] for t in closed if t["pnl_usd"] > 0]
    losses = [t["pnl_usd"] for t in closed if t["pnl_usd"] <= 0]
    s = {
        "closed": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_win": round(mean(wins), 2) if wins else None,
        "avg_loss": round(mean(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if wins and losses and sum(losses) else None,
        "avg_r": round(mean([t["r_result"] for t in closed if t.get("r_result") is not None]), 2) if closed else None,
        "realized_pnl": round(sum(t["pnl_usd"] for t in closed), 2),
        "max_drawdown_pct": _max_dd(curve),
        "sharpe": _sharpe(curve),
        "by_bucket": _group(closed, "bucket"), "by_style": _group(closed, "style"), "by_asset": _group(closed, "asset"),
        "calibration": _calibration(closed),
    }
    return s


def _group(closed: list[dict], key: str) -> list[dict]:
    groups: dict[str, list] = {}
    for t in closed:
        groups.setdefault(t.get(key) or "other", []).append(t["pnl_usd"])
    return sorted([{"name": k, "trades": len(v), "pnl": round(sum(v), 2),
                    "win_rate": round(sum(1 for x in v if x > 0) / len(v) * 100)} for k, v in groups.items()],
                  key=lambda g: -g["pnl"])


def _calibration(closed: list[dict]) -> list[dict]:
    """Do its '70% confident' trades actually win about 70% of the time?"""
    bins = {}
    for t in closed:
        c = t.get("confidence_pct")
        if c is None:
            continue
        b = min(90, max(30, int(c // 10 * 10)))
        bins.setdefault(b, []).append(1 if t["pnl_usd"] > 0 else 0)
    return [{"confidence": b, "trades": len(v), "actual_win_rate": round(mean(v) * 100)} for b, v in sorted(bins.items())]


def _max_dd(curve: list[dict]) -> float | None:
    if not curve:
        return None
    peak, dd = curve[0]["equity"], 0.0
    for p in curve:
        peak = max(peak, p["equity"])
        dd = min(dd, (p["equity"] / peak - 1) * 100)
    return round(dd, 2)


def _sharpe(curve: list[dict]) -> float | None:
    daily: dict[str, float] = {}
    for p in curve:
        daily[p["t"][:10]] = p["equity"]
    vals = list(daily.values())
    if len(vals) < 10:
        return None
    rets = [b / a - 1 for a, b in zip(vals[:-1], vals[1:])]
    sd = pstdev(rets)
    return round(mean(rets) / sd * math.sqrt(252), 2) if sd else None


def _ghost(plans: list[dict], trades: list[dict]) -> list[dict]:
    """Ideas the desk passed on or the fact-checker blocked, and how they moved since."""
    rows, seen = [], set()
    for plan in reversed(plans):
        for i in plan.get("ideas", []):
            if i.get("status") not in ("pass", "blocked_by_fact_check") or not i.get("price_at_research"):
                continue
            key = (plan["_file"], i["symbol"])
            if key in seen:
                continue
            seen.add(key)
            d = i.get("decision") or {}
            lean = (i.get("bull") or {}).get("direction") or "long"
            rows.append({"symbol": i["symbol"], "asset": i["asset"], "venue": i.get("venue"), "date": plan.get("date"),
                         "session": plan.get("session"), "status": i["status"], "lean": lean,
                         "price_then": i["price_at_research"], "reason": d.get("reason") or (i.get("fact_check") or {}).get("problems", [""])[0],
                         "memo_key": f"{plan['_file']}|{i['symbol']}"})
    rows = rows[:80]
    _fill_prices(rows)
    for r in rows:
        if r.get("price_now"):
            move = (r["price_now"] / r["price_then"] - 1) * 100
            r["move_pct"] = round(move, 2)
            r["lean_result_pct"] = round(move if r["lean"] == "long" else -move, 2)
    return rows


def _fill_prices(rows: list[dict]) -> None:
    try:
        stocks = sorted({r["symbol"] for r in rows if r["asset"] in ("stock", "etf")})
        if stocks:
            snaps = marketdata.stock_snapshots(stocks)
            for r in rows:
                if r["symbol"] in snaps:
                    r["price_now"] = snaps[r["symbol"]].get("price")
        cryptos = sorted({r["symbol"] for r in rows if r["asset"] == "crypto"})
        if cryptos:
            px = marketdata.crypto_prices(cryptos)
            for r in rows:
                if r["symbol"] in px:
                    r["price_now"] = px[r["symbol"]]
        cache = {}
        for r in rows:
            if r["asset"] == "forex":
                if r["symbol"] not in cache:
                    cache[r["symbol"]] = oanda.price(r["symbol"])["mid"]
                r["price_now"] = cache[r["symbol"]]
    except Exception as exc:
        print(f"ghost prices failed: {exc}")
