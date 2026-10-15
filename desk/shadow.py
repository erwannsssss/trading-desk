"""Shadow strategies: simple rule-based traders that see the exact same research ideas as the desk.

If the AI's judgment is worth anything, the desk's picks should beat these dumb rules. Every idea is simulated
the same way for everyone: enter at the research price, exit at the stop or target (stop first if both hit in
one day), or after 10 trading days.

  desk            the portfolio manager's approved ideas, at her levels (apples-to-apples with the shadows)
  follow_bull     take every idea in whatever direction Jake (the bull) argued
  high_conviction only the desk's approved ideas with conviction 8+
  scanner_top     the scanner's #1 pick every session, long unless it flagged a breakdown
  coin_flip       every idea, random direction (pure luck)
Shadow rules use stops 1.5 ATR away and targets 2.5 ATR away.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from desk import config, marketdata
from desk.scorecard import _ny_date
from desk.state import now

FILE = config.DATA_DIR / "shadow.json"
MAX_BARS = 10
STRATEGIES = {
    "desk": "The desk (Elena's approved ideas)",
    "follow_bull": "Always follow the bull",
    "high_conviction": "Only conviction 8+",
    "scanner_top": "Scanner's top pick",
    "coin_flip": "Coin flip",
}


def _atr_levels(price: float, atr_pct: float, direction: str) -> tuple[float, float]:
    atr = price * (atr_pct or 2.0) / 100
    if direction == "long":
        return price - 1.5 * atr, price + 2.5 * atr
    return price + 1.5 * atr, price - 2.5 * atr


def _coin(key: str) -> str:
    return "long" if int(hashlib.sha256(key.encode()).hexdigest(), 16) % 2 == 0 else "short"


def picks_for(plan: dict) -> list[tuple[str, str, dict, str, float, float]]:
    """(strategy, key, idea, direction, stop, target) for every shadow trade this plan generates."""
    out = []
    ideas = [i for i in plan.get("ideas", []) if i.get("price_at_research") and i.get("status") != "error"]
    for n, idea in enumerate(ideas):
        key = f"{plan['_file']}|{idea['symbol']}"
        price, atr = idea["price_at_research"], idea.get("atr_pct") or 2.0
        d = idea.get("decision") or {}
        if idea.get("status") == "approved" and d.get("decision") in ("long", "short"):
            out.append(("desk", key, idea, d["decision"], float(d["stop"]), float(d["target"])))
            if (d.get("conviction") or 0) >= 8:
                out.append(("high_conviction", key, idea, d["decision"], float(d["stop"]), float(d["target"])))
        bull_dir = (idea.get("bull") or {}).get("direction")
        if bull_dir in ("long", "short"):
            out.append(("follow_bull", key, idea, bull_dir, *_atr_levels(price, atr, bull_dir)))
        coin = _coin(key)
        out.append(("coin_flip", key, idea, coin, *_atr_levels(price, atr, coin)))
        if n == 0 and plan.get("session") != "pitch":
            setups = " ".join(idea.get("setups") or [])
            direction = "short" if ("breakdown" in setups or "very overbought" in setups) else "long"
            out.append(("scanner_top", key, idea, direction, *_atr_levels(price, atr, direction)))
    return out


def simulate(bars: list[dict], created: datetime, asset: str, entry: float, direction: str, stop: float, target: float) -> dict | None:
    if asset in ("stock", "etf"):
        day = created.date().isoformat()
        pre_open = (created.hour, created.minute) < (9, 30)
        after = [b for b in bars if (_ny_date(b["t"]) >= day if pre_open else _ny_date(b["t"]) > day)]
        today = now().date().isoformat()
        complete = [b for b in after if _ny_date(b["t"]) < today]
    else:
        after = [b for b in bars if datetime.fromisoformat(b["t"][:19] + "+00:00") > created]
        complete = after[:-1]
    risk = abs(entry - stop)
    if not risk:
        return None
    long = direction == "long"
    for b in complete[:MAX_BARS]:
        if (long and b["l"] <= stop) or (not long and b["h"] >= stop):
            return {"r": -1.0, "status": "closed", "how": "stop"}
        if (long and b["h"] >= target) or (not long and b["l"] <= target):
            return {"r": round(abs(target - entry) / risk, 3), "status": "closed", "how": "target"}
    if len(complete) >= MAX_BARS:
        last = complete[MAX_BARS - 1]["c"]
        return {"r": round(((last - entry) if long else (entry - last)) / risk, 3), "status": "closed", "how": "time"}
    if after:
        last = after[-1]["c"]
        return {"r": round(((last - entry) if long else (entry - last)) / risk, 3), "status": "open", "how": "open"}
    return None


def update(plans: list[dict]) -> dict:
    data = json.loads(FILE.read_text()) if FILE.exists() else {"results": {}}
    done = data["results"]
    todo = []
    for plan in plans:
        created = datetime.fromisoformat(plan["created_at"])
        if now() - created < timedelta(hours=20):
            continue
        for strat, key, idea, direction, stop, target in picks_for(plan):
            rid = f"{strat}|{key}"
            if done.get(rid, {}).get("status") == "closed":
                continue
            todo.append((rid, strat, created, idea, direction, stop, target, plan))
    if todo:
        syms = {i["symbol"]: i.get("asset") for _, _, _, i, _, _, _, _ in todo}
        bars = {}
        try:
            stocks = [s for s, a in syms.items() if a in ("stock", "etf")]
            cryptos = [s for s, a in syms.items() if a == "crypto"]
            if stocks:
                bars.update(marketdata.stock_daily_bars(stocks, days=60))
            if cryptos:
                bars.update(marketdata.crypto_daily_bars(cryptos, days=60))
            for s, a in syms.items():
                if a == "forex":
                    bars[s] = marketdata.forex_daily_bars(s, 60)
        except Exception as exc:
            print(f"shadow bars failed: {exc}")
        for rid, strat, created, idea, direction, stop, target, plan in todo:
            res = simulate(bars.get(idea["symbol"], []), created, idea.get("asset"), idea["price_at_research"], direction, stop, target)
            if res:
                done[rid] = {**res, "strategy": strat, "symbol": idea["symbol"], "direction": direction,
                             "date": plan["created_at"][:10], "created_at": plan["created_at"]}
    data["updated"] = now().isoformat(timespec="minutes")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, indent=1))
    return data


def summary() -> dict:
    """Per-strategy stats and running totals, plus whether the desk beats each simple rule."""
    from desk import edge
    data = json.loads(FILE.read_text()) if FILE.exists() else {"results": {}}
    rows: dict[str, list[dict]] = {k: [] for k in STRATEGIES}
    for r in data["results"].values():
        if r["status"] == "closed" and r["strategy"] in rows:
            rows[r["strategy"]].append(r)
    out = {"labels": STRATEGIES, "strategies": {}, "updated": data.get("updated")}
    desk_rs = [r["r"] for r in sorted(rows["desk"], key=lambda r: r["created_at"])]
    for k, rs in rows.items():
        rs = sorted(rs, key=lambda r: r["created_at"])
        vals = [r["r"] for r in rs]
        t = edge.trade_tests(vals) if len(vals) >= 2 else {"n": len(vals)}
        t.pop("luck_band", None)
        running, cum = [], 0.0
        for r in rs:
            cum += r["r"]
            running.append({"date": r["date"], "cum_r": round(cum, 2)})
        entry = {"label": STRATEGIES[k], "stats": t, "running": running}
        if k != "desk" and len(vals) >= 5 and len(desk_rs) >= 5:
            entry["desk_vs"] = edge.welch(desk_rs, vals)
        out["strategies"][k] = entry
    return out
