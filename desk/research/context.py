"""What a seasoned trader looks at before deciding, as plain numbers for the AI to reason with (not rules).

  price_behavior   how far this symbol actually travels in an hour-ish, a day, a week and a month, in ATRs
                   (ATR = its average daily range): the odds a target is even reachable in the time allowed
  reach_odds       for one plan: how often, over the last 2 years, price got from the entry to the target
                   within the planned holding time
  base_rates       the desk's real record by style and asset, plus how well its stated confidence has matched
                   reality (an AI saying "60% sure" isn't a probability until it's checked)
  journal          the desk's recent closed trades in the same style or symbol, and what it learned
  setup_history    the same chart signal on this symbol over 2 years (from the quant desk's backtester)
"""
from __future__ import annotations

import json
from collections import defaultdict

from desk import config

HORIZON_BARS = {"intraday": 1, "1-5 days": 5, "1-4 weeks": 20}
LEVELS = (0.5, 1.0, 1.5, 2.0, 3.0)


def _atr_series(bars: list[dict], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    trs = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])))
        if len(trs) >= n:
            out[i] = sum(trs[-n:]) / n
    return out


def price_behavior(bars: list[dict], lookback: int = 500) -> dict | None:
    """% of past windows in which price reached k ATRs above (up) or below (down) the starting close."""
    if not bars or len(bars) < 60:
        return None
    atr = _atr_series(bars)
    start = max(15, len(bars) - lookback)
    out = {}
    for name, h in (("within_1_day", 1), ("within_5_days", 5), ("within_20_days", 20)):
        up = defaultdict(int)
        dn = defaultdict(int)
        n = 0
        for i in range(start, len(bars) - h):
            a = atr[i]
            if not a:
                continue
            c = bars[i]["c"]
            hi = max(b["h"] for b in bars[i + 1:i + 1 + h])
            lo = min(b["l"] for b in bars[i + 1:i + 1 + h])
            n += 1
            for k in LEVELS:
                up[k] += hi >= c + k * a
                dn[k] += lo <= c - k * a
        if n:
            out[name] = {"up": {f"{k}_atr": round(up[k] / n * 100) for k in LEVELS},
                         "down": {f"{k}_atr": round(dn[k] / n * 100) for k in LEVELS}, "windows": n}
    return out or None


def reach_odds(bars: list[dict], long: bool, entry: float, target: float, horizon: str | None) -> dict | None:
    """How often price moved from a close to a target this far away (in ATRs) within the holding time."""
    if not bars or len(bars) < 60 or not entry or not target:
        return None
    atr = _atr_series(bars)
    a_now = atr[-1]
    if not a_now:
        return None
    k = abs(target - entry) / a_now
    h = HORIZON_BARS.get(horizon or "", 5)
    hits = n = 0
    for i in range(max(15, len(bars) - 500), len(bars) - h):
        a = atr[i]
        if not a:
            continue
        c = bars[i]["c"]
        window = bars[i + 1:i + 1 + h]
        if long:
            hits += max(b["h"] for b in window) >= c + k * a
        else:
            hits += min(b["l"] for b in window) <= c - k * a
        n += 1
    if not n:
        return None
    pct = round(hits / n * 100, 1)
    note = "full trading days; an intraday trade gets only part of one, so the real odds are lower" if h == 1 else ""
    return {"target_distance_atr": round(k, 2), "holding_days": h, "reached_pct": pct, "windows": n,
            **({"note": note} if note else {})}


def setup_history(bars: list[dict]) -> dict | None:
    """The quant desk's 2-year replay of the momentum and mean-reversion signals on this symbol."""
    try:
        from desk.research import quant
        f = quant._feat(bars, len(bars) - 1)
        if not f:
            return None
        out = {"price_vs_200d_avg_pct": round((f["c"] / f["sma200"] - 1) * 100, 1),
               "ret_60d_pct": round(f["r60"], 1),
               "z_score_vs_20d_avg": round(f["z20"], 2) if f["z20"] is not None else None,
               "long_term_trend": "up" if f["c"] > f["sma200"] else "down"}
        for kind in ("momentum", "reversion"):
            rec = quant.rule_record(bars, kind)
            if rec:
                out[f"{kind}_signal_record"] = rec
            sig = quant.signal(kind, f)
            if sig:
                out[f"{kind}_signal_now"] = sig
        return out
    except Exception as exc:
        print(f"setup history failed: {exc}")
        return None


# ---- the desk's own record -------------------------------------------------------
def _closed(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t.get("status") == "closed" and isinstance(t.get("r_result"), (int, float))]


def base_rates(trades: list[dict]) -> dict:
    """Real win rates by style and by asset, from closed trades and (once graded) researched ideas."""
    closed = _closed(trades)

    def rate(rows):
        if not rows:
            return None
        return {"trades": len(rows), "win_pct": round(sum(t["r_result"] > 0 for t in rows) / len(rows) * 100),
                "avg_r": round(sum(t["r_result"] for t in rows) / len(rows), 2)}
    by_style, by_asset = defaultdict(list), defaultdict(list)
    for t in closed:
        by_style[(t.get("style") or "?").lower()].append(t)
        by_asset[t.get("asset") or "?"].append(t)
    out = {"all": rate(closed), "by_style": {k: rate(v) for k, v in by_style.items()},
           "by_asset": {k: rate(v) for k, v in by_asset.items()}}
    # How well stated confidence matched outcomes (trades carry the confidence the PM gave)
    buckets = defaultdict(list)
    for t in closed:
        c = t.get("confidence_pct")
        if isinstance(c, (int, float)):
            buckets["under 50%" if c < 50 else "50-59%" if c < 60 else "60%+"].append(t["r_result"] > 0)
    out["confidence_check"] = {k: {"trades": len(v), "actually_won_pct": round(sum(v) / len(v) * 100)} for k, v in buckets.items()}
    try:
        sc = json.loads((config.DATA_DIR / "scorecard.json").read_text())
        g = list(sc.get("graded", {}).values())
        approved = [x for x in g if x.get("status") == "approved" and x.get("pm") in ("long", "short")]
        if approved:
            right = sum((1 if x["pm"] == "long" else -1) * x["move_pct"] > 0 for x in approved)
            out["approved_ideas_3_day_direction"] = {"ideas": len(approved), "right_pct": round(right / len(approved) * 100)}
    except Exception:
        pass
    out["sample_note"] = ("small sample: treat as a hint, not a fact" if (out["all"] or {}).get("trades", 0) < 30
                          else "enough trades to take seriously")
    return out


def style_rate(rates: dict, style: str | None, asset: str | None) -> tuple[float | None, int]:
    """(win rate 0-100, sample size) for this style, falling back to this asset class."""
    for r in ((rates.get("by_style") or {}).get((style or "").lower()), (rates.get("by_asset") or {}).get(asset or "")):
        if r and r.get("trades"):
            return r["win_pct"], r["trades"]
    return None, 0


def journal(trades: list[dict], symbol: str | None = None, style: str | None = None, limit: int = 6) -> list[dict]:
    """Recent closed trades in the same symbol or style: what was planned, what happened, the lesson."""
    rows = [t for t in _closed(trades) if (symbol and t.get("symbol") == symbol)
            or (style and (t.get("style") or "").lower() == style.lower())]
    rows.sort(key=lambda t: t.get("closed_at") or "", reverse=True)
    out = []
    for t in rows[:limit]:
        moved = [n for n in (t.get("notes") or []) if "stop moved" in n or "huddle" in n]
        out.append({"symbol": t["symbol"], "direction": t.get("direction"), "style": t.get("style"), "result_r": t.get("r_result"),
                    "pnl_usd": t.get("pnl_usd"), "best_r_reached": t.get("max_r"), "exit": t.get("exit_reason"),
                    "stop_changes": len(moved),
                    "lesson": (t.get("postmortem") or {}).get("lesson")})
    return out
