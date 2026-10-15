"""Chart math: trend, momentum, volatility and named setups. Plain code, no AI."""
from __future__ import annotations

from statistics import mean


def _sma(values: list[float], n: int) -> float | None:
    return mean(values[-n:]) if len(values) >= n else None


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        gains.append(max(b - a, 0))
        losses.append(max(a - b, 0))
    avg_g, avg_l = mean(gains[:n]), mean(losses[:n])
    for g, l in zip(gains[n:], losses[n:]):  # Wilder smoothing
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def _atr(bars: list[dict], n: int = 14) -> float | None:
    if len(bars) <= n:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars[:-1], bars[1:])]
    atr = mean(trs[:n])
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr


def _ret(closes: list[float], n: int) -> float | None:
    return round((closes[-1] / closes[-1 - n] - 1) * 100, 2) if len(closes) > n and closes[-1 - n] else None


def analyze(bars: list[dict], live_price: float | None = None, live_replaces_last: bool = True) -> dict | None:
    """Indicators plus setup tags for one symbol. `bars` are daily, oldest first.
    `live_price` (e.g. pre-market) either updates today's forming bar or is added as a new day."""
    if len(bars) < 30:
        return None
    closes = [b["c"] for b in bars]
    if live_price:
        closes = closes[:-1] + [live_price] if live_replaces_last else closes + [live_price]
    price = closes[-1]
    vols = [b["v"] for b in bars]
    hist = bars[:-1]  # everything before the latest bar, for breakout levels

    t = {
        "price": round(price, 6),
        "ret_1d": _ret(closes, 1), "ret_5d": _ret(closes, 5), "ret_20d": _ret(closes, 20), "ret_60d": _ret(closes, 60),
        "sma20": _sma(closes, 20), "sma50": _sma(closes, 50), "sma200": _sma(closes, 200),
        "rsi14": _rsi(closes),
        "atr14": _atr(bars),
        "high_20": max(b["h"] for b in hist[-20:]), "low_20": min(b["l"] for b in hist[-20:]),
        "high_52w": max(b["h"] for b in bars[-252:]), "low_52w": min(b["l"] for b in bars[-252:]),
        "vol_ratio": round(vols[-1] / mean(vols[-21:-1]), 2) if len(vols) > 21 and mean(vols[-21:-1]) else None,
        "avg_dollar_vol_20d": round(mean(c * v for c, v in zip(closes[-21:-1], vols[-21:-1]))) if len(vols) > 21 else None,
    }
    t["atr_pct"] = round(t["atr14"] / price * 100, 2) if t["atr14"] else None
    t["pct_from_52w_high"] = round((price / t["high_52w"] - 1) * 100, 2)
    for k in ("sma20", "sma50", "sma200", "rsi14", "atr14"):
        if t[k] is not None:
            t[k] = round(t[k], 6)

    t["setups"] = _setups(t)
    t["trend"] = _trend(t)
    t["interest"] = _interest(t)
    return t


def _trend(t: dict) -> str:
    p, s50, s200 = t["price"], t["sma50"], t["sma200"]
    if s50 and s200:
        if p > s50 > s200:
            return "uptrend"
        if p < s50 < s200:
            return "downtrend"
    return "sideways/mixed"


def _setups(t: dict) -> list[str]:
    s, p, rsi = [], t["price"], t["rsi14"] or 50
    if p > t["high_20"]:
        s.append("breakout above 20-day high")
    if p < t["low_20"]:
        s.append("breakdown below 20-day low")
    if t["sma200"] and p > t["sma200"] and rsi < 32:
        s.append("oversold pullback in a long-term uptrend")
    if rsi < 25:
        s.append("deeply oversold (bounce candidate)")
    if rsi > 78:
        s.append("very overbought (fade or momentum candidate)")
    if t["sma20"] and t["sma50"] and p > t["sma50"] and abs(p / t["sma20"] - 1) < 0.015 and 40 <= rsi <= 58:
        s.append("trend pullback to 20-day average")
    if (t["vol_ratio"] or 0) >= 2:
        s.append(f"volume surge ({t['vol_ratio']}x normal)")
    if t["ret_20d"] is not None and abs(t["ret_20d"]) >= 15:
        s.append(f"strong 20-day move ({t['ret_20d']:+.1f}%)")
    if t["pct_from_52w_high"] > -2:
        s.append("near 52-week high")
    return s


def _interest(t: dict) -> float:
    """How worth a deeper look this is today. Not a buy/sell signal."""
    score = 0.0
    score += 1.5 * len(t["setups"])
    score += min(abs(t["ret_5d"] or 0) / 3, 3)
    score += min(abs(t["ret_20d"] or 0) / 8, 2)
    score += min(max((t["vol_ratio"] or 1) - 1, 0), 3)
    rsi = t["rsi14"] or 50
    score += max(abs(rsi - 50) - 15, 0) / 10
    return round(score, 2)
