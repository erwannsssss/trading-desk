"""Backtest of the desk's chart setups on ~2 years of daily data.

For every day and every symbol in the core universe, it checks which setups the scanner would have flagged,
then simulates the trade the desk typically takes: enter at the next day's open, stop 1.5 ATR away,
target 2.5 ATR away, give up after 10 trading days. Results (win rate, average result in R) tell the
analysts and the strategy tournament which setups have actually had an edge.

It tests the chart setups, not the AI's full judgment (replaying the AI on history would burn the free quota).
"""
from __future__ import annotations

import json
import re
from statistics import mean

from desk import marketdata
from desk.research import technicals
from desk.research.scanner import ETFS, STOCKS
from desk.state import now

STOP_ATR, TARGET_ATR, MAX_DAYS, WARMUP = 1.5, 2.5, 10, 210


def _setup_key(tag: str) -> str:
    return re.sub(r"\s*\(.*?\)|\s*[+-]?\d+(\.\d+)?%.*$", "", tag).strip()


def _direction(key: str, t: dict) -> str | None:
    if key in ("breakdown below 20-day low", "very overbought"):
        return "short"
    if key.startswith("strong 20-day move"):
        return "long" if (t.get("ret_20d") or 0) > 0 else "short"
    if key.startswith("volume surge"):
        return "long" if (t.get("ret_1d") or 0) > 0 else "short"
    if key.startswith("pre-market gap"):
        return None
    return "long"


def simulate(bars: list[dict], i: int, direction: str, atr: float) -> float | None:
    """R result of a trade entered at bar i+1's open. Stop is assumed to hit first if both are touched in one bar."""
    if i + 1 >= len(bars) or not atr:
        return None
    entry = bars[i + 1]["o"]
    risk = STOP_ATR * atr
    stop = entry - risk if direction == "long" else entry + risk
    target = entry + TARGET_ATR * atr if direction == "long" else entry - TARGET_ATR * atr
    last = None
    for b in bars[i + 1:i + 1 + MAX_DAYS]:
        if direction == "long":
            if b["l"] <= stop:
                return -1.0
            if b["h"] >= target:
                return TARGET_ATR / STOP_ATR
        else:
            if b["h"] >= stop:
                return -1.0
            if b["l"] <= target:
                return TARGET_ATR / STOP_ATR
        last = b["c"]
    if last is None:
        return None
    move = (last - entry) if direction == "long" else (entry - last)
    return move / risk


def run(symbols: list[str] | None = None, days: int = 760) -> dict:
    symbols = symbols or sorted(set(ETFS) | set(STOCKS))
    data = marketdata.stock_daily_bars(symbols, days=days)
    results: dict[tuple[str, str], list[float]] = {}
    for sym, bars in data.items():
        for i in range(WARMUP, len(bars) - 1):
            t = technicals.analyze(bars[max(0, i - 260):i + 1])
            if not t or not t.get("atr14"):
                continue
            for tag in t["setups"]:
                key = _setup_key(tag)
                direction = _direction(key, t)
                if not direction:
                    continue
                r = simulate(bars, i, direction, t["atr14"])
                if r is not None:
                    results.setdefault((key, direction), []).append(r)
    rows = []
    for (key, direction), rs in results.items():
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        rows.append({"setup": key, "direction": direction, "trades": len(rs),
                     "win_rate": round(len(wins) / len(rs) * 100, 1), "avg_r": round(mean(rs), 3),
                     "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else None})
    rows.sort(key=lambda r: -r["avg_r"])
    out = {"updated": now().isoformat(timespec="minutes"), "symbols": len(data), "days": days,
           "rules": f"enter next open, stop {STOP_ATR} ATR, target {TARGET_ATR} ATR, max {MAX_DAYS} days", "results": rows}
    from desk.learning import BACKTEST, _save
    _save(BACKTEST, out)
    return out
