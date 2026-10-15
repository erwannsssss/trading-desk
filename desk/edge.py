"""Luck or skill? The statistics that tell real edge apart from a lucky streak.

Everything here is plain Python (no numpy) and seeded, so the numbers are stable between runs.

Tests on closed trades (results measured in R = units of risk):
  - expectancy with a t-test and a bootstrap confidence interval
  - coin-flip permutation test: same trades, random direction. How often does luck do as well?
  - win rate with a Wilson confidence interval
  - how many more trades are needed before the current edge would be statistically significant
Tests on the account vs the S&P 500 (daily returns from the equity curve):
  - alpha and beta (does it beat the market after adjusting for market exposure?), alpha t-stat
  - probabilistic Sharpe ratio (chance the true Sharpe is above zero) and the track record still needed
Tests on each analyst's calls: binomial test against a coin flip.
Plus: does conviction predict results? Monte Carlo of the next 100 trades.
"""
from __future__ import annotations

import math
import random
from statistics import mean, pstdev, stdev

SEED = 7
BOOT = 4000


# ---- distributions -----------------------------------------------------------
def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _betacf(a, b, x):
    tiny, qab, qap, qam = 1e-300, a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + aa / c if abs(1 + aa / c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + aa / c if abs(1 + aa / c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1) < 3e-12:
            break
    return h


def betainc(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1 - bt * _betacf(b, a, 1 - x) / b


def t_sf(t: float, df: int) -> float:
    """P(T > t) for Student's t."""
    x = df / (df + t * t)
    tail = 0.5 * betainc(df / 2, 0.5, x)
    return tail if t > 0 else 1 - tail


def t_ppf_95(df: int) -> float:
    """One-sided 95% (two-sided 90%) critical value, by bisection."""
    lo, hi = 0.0, 50.0
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if t_sf(mid, df) > 0.05 else (lo, mid)
    return (lo + hi) / 2


def binom_sf(k: int, n: int, p: float = 0.5) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    ph = k / n
    denom = 1 + z * z / n
    centre = (ph + z * z / (2 * n)) / denom
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


# ---- trade-level tests -------------------------------------------------------
def trade_tests(rs: list[float]) -> dict:
    n = len(rs)
    out = {"n": n}
    if n < 2:
        return out
    m, sd = mean(rs), stdev(rs)
    out.update(avg_r=round(m, 3), sd_r=round(sd, 3), total_r=round(sum(rs), 2))
    se = sd / math.sqrt(n) if sd else 0
    if se:
        t = m / se
        out.update(t_stat=round(t, 2), p_value=round(t_sf(t, n - 1), 4))
        crit = t_ppf_95(n - 1)
        out["ci90"] = [round(m - crit * se, 3), round(m + crit * se, 3)]
    if m > 0 and sd:
        out["trades_needed"] = math.ceil((1.645 * sd / m) ** 2)  # n where a one-sided 95% test would pass
    rng = random.Random(SEED)
    boots = sorted(mean(rng.choices(rs, k=n)) for _ in range(BOOT))
    out["boot_ci95"] = [round(_pct(boots, 0.025), 3), round(_pct(boots, 0.975), 3)]
    out["prob_positive"] = round(sum(b > 0 for b in boots) / BOOT, 3)
    # coin-flip trader: same trade sizes, random direction
    obs = sum(rs)
    abs_rs = [abs(r) for r in rs]
    beats, sims = 0, []
    for _ in range(BOOT):
        path, tot = [], 0.0
        for a in abs_rs:
            tot += a if rng.random() < 0.5 else -a
            path.append(tot)
        sims.append(path)
        beats += tot >= obs
    out["luck_p_value"] = round(beats / BOOT, 4)
    out["luck_band"] = [[round(_pct(sorted(s[i] for s in sims), 0.05), 2), round(_pct(sorted(s[i] for s in sims), 0.95), 2)]
                        for i in range(n)]
    wins = sum(r > 0 for r in rs)
    lo, hi = wilson(wins, n)
    out.update(win_rate=round(wins / n * 100, 1), win_rate_ci95=[round(lo * 100, 1), round(hi * 100, 1)])
    avg_win = mean([r for r in rs if r > 0]) if wins else 0
    avg_loss = abs(mean([r for r in rs if r <= 0])) if wins < n else 0
    if avg_win and avg_loss:
        out["breakeven_win_rate"] = round(avg_loss / (avg_win + avg_loss) * 100, 1)
    return out


def welch(a: list[float], b: list[float]) -> dict:
    """Is the average of `a` higher than `b`? One-sided Welch t-test (unequal variances)."""
    na, nb = len(a), len(b)
    va, vb = stdev(a) ** 2 / na, stdev(b) ** 2 / nb
    diff = mean(a) - mean(b)
    if va + vb == 0:
        return {"diff_r": round(diff, 3), "t_stat": None, "p_value": None}
    t = diff / math.sqrt(va + vb)
    df = (va + vb) ** 2 / ((va ** 2 / (na - 1) if na > 1 else 0) + (vb ** 2 / (nb - 1) if nb > 1 else 0) or 1)
    return {"diff_r": round(diff, 3), "t_stat": round(t, 2), "p_value": round(t_sf(t, max(1, int(df))), 4)}


def group_edges(trades: list[dict], key: str) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for t in trades:
        groups.setdefault(t.get(key) or "other", []).append(t["r_result"])
    out = []
    for name, rs in groups.items():
        row = {"name": name, "n": len(rs), "avg_r": round(mean(rs), 3)}
        if len(rs) >= 3:
            sd = stdev(rs)
            se = sd / math.sqrt(len(rs)) if sd else 0
            crit = t_ppf_95(len(rs) - 1)
            row["ci90"] = [round(row["avg_r"] - crit * se, 3), round(row["avg_r"] + crit * se, 3)]
            row["p_value"] = round(t_sf(row["avg_r"] / se, len(rs) - 1), 4) if se else None
        out.append(row)
    return sorted(out, key=lambda r: -r["avg_r"])


def correlation(xs: list[float], ys: list[float]) -> dict | None:
    n = len(xs)
    if n < 5 or pstdev(xs) == 0 or pstdev(ys) == 0:
        return None
    mx, my = mean(xs), mean(ys)
    r = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n * pstdev(xs) * pstdev(ys))
    r = max(-0.9999, min(0.9999, r))
    t = r * math.sqrt((n - 2) / (1 - r * r))
    return {"r": round(r, 3), "n": n, "p_value": round(t_sf(t, n - 2), 4)}


# ---- account-level tests -----------------------------------------------------
def daily_returns(curve: list[dict]) -> tuple[list[str], list[float], list[float]]:
    days: dict[str, dict] = {}
    for p in curve:
        days[p["t"][:10]] = p  # last point of each day
    keys = sorted(days)
    dates, desk, spy = [], [], []
    for a, b in zip(keys[:-1], keys[1:]):
        pa, pb = days[a], days[b]
        if pa["equity"] and pa["spy"]:
            dates.append(b)
            desk.append(pb["equity"] / pa["equity"] - 1)
            spy.append(pb["spy"] / pa["spy"] - 1)
    return dates, desk, spy


def market_tests(curve: list[dict]) -> dict:
    dates, r, m = daily_returns(curve)
    n = len(r)
    out = {"days": n}
    if n < 5:
        return out
    mr, mm = mean(r), mean(m)
    sxx = sum((x - mm) ** 2 for x in m)
    if sxx > 0:
        beta = sum((x - mm) * (y - mr) for x, y in zip(m, r)) / sxx
        alpha = mr - beta * mm
        resid = [y - alpha - beta * x for x, y in zip(m, r)]
        s = math.sqrt(sum(e * e for e in resid) / max(1, n - 2))
        se_a = s * math.sqrt(1 / n + mm * mm / sxx)
        out.update(beta=round(beta, 3), alpha_annual_pct=round(alpha * 252 * 100, 2),
                   alpha_t=round(alpha / se_a, 2) if se_a else None,
                   alpha_p=round(t_sf(alpha / se_a, n - 2), 4) if se_a else None,
                   correlation=round(beta * math.sqrt(sxx / n) / pstdev(r), 3) if pstdev(r) else None)
    sd = pstdev(r)
    if sd:
        sr = mr / sd
        g3 = sum((x - mr) ** 3 for x in r) / n / sd ** 3
        g4 = sum((x - mr) ** 4 for x in r) / n / sd ** 4
        var_term = max(1e-9, 1 - g3 * sr + (g4 - 1) / 4 * sr * sr)
        psr = norm_cdf(sr * math.sqrt(n - 1) / math.sqrt(var_term))
        out.update(sharpe_annual=round(sr * math.sqrt(252), 2), psr=round(psr, 3), skew=round(g3, 2), kurtosis=round(g4, 2))
        if sr > 0:
            out["min_track_record_days"] = math.ceil(1 + var_term * (1.645 / sr) ** 2)
    excess = [a - b for a, b in zip(r, m)]
    if pstdev(excess):
        out["information_ratio"] = round(mean(excess) / pstdev(excess) * math.sqrt(252), 2)
    out["beat_market_days_pct"] = round(sum(e > 0 for e in excess) / n * 100, 1)
    return out


# ---- analysts ----------------------------------------------------------------
def analyst_tests(scores: dict) -> list[dict]:
    out = []
    for seat, s in scores.items():
        if seat in ("execution", "sizer", "risk", "positions", "options") or not s.get("total"):
            continue
        k, n = s["right"], s["total"]
        lo, hi = wilson(k, n)
        out.append({"seat": seat, "right": k, "total": n, "pct": round(k / n * 100, 1),
                    "ci95": [round(lo * 100, 1), round(hi * 100, 1)], "p_value": round(binom_sf(k, n), 4)})
    return sorted(out, key=lambda a: -a["pct"])


# ---- projection --------------------------------------------------------------
def monte_carlo(rs: list[float], risk_usd: float, start: float, n_trades: int = 100, paths: int = 2000) -> dict | None:
    if len(rs) < 5:
        return None
    rng = random.Random(SEED)
    steps = list(range(0, n_trades + 1, 5))
    at_step = {s: [] for s in steps}
    finals, dd20 = [], 0
    for _ in range(paths):
        eq, peak, hit = start, start, False
        at_step[0].append(eq)
        for i in range(1, n_trades + 1):
            eq += rng.choice(rs) * risk_usd
            peak = max(peak, eq)
            hit = hit or (eq <= peak * 0.8)
            if i in at_step:
                at_step[i].append(eq)
        finals.append(eq)
        dd20 += hit
    bands = []
    for s in steps:
        v = sorted(at_step[s])
        bands.append({"trade": s, **{f"p{q}": round(_pct(v, q / 100), 2) for q in (5, 25, 50, 75, 95)}})
    return {"bands": bands, "prob_profit": round(sum(f > start for f in finals) / paths, 3),
            "prob_20pct_drawdown": round(dd20 / paths, 3), "risk_per_trade_usd": round(risk_usd, 2)}


# ---- everything --------------------------------------------------------------
def verdict(t: dict) -> dict:
    n = t.get("n", 0)
    if n < 20:
        return {"level": "too_early", "headline": "Too early to tell",
                "detail": f"{n} closed trade{'s' if n != 1 else ''} so far. Even a coin flip has hot and cold streaks; "
                          "results start to mean something around 30 trades and are solid around 100."}
    p_luck, prob_pos = t.get("luck_p_value", 1), t.get("prob_positive", 0)
    if t.get("avg_r", 0) <= 0:
        return {"level": "no_edge", "headline": "No edge so far",
                "detail": f"Across {n} trades the average result is {t['avg_r']}R. A random-direction trader would match "
                          f"this {round((1 - p_luck) * 100)}% of the time."}
    if p_luck < 0.05 and prob_pos >= 0.95:
        return {"level": "edge", "headline": "Evidence of a real edge",
                "detail": f"Only {round(p_luck * 100, 1)}% of coin-flip traders with the same trade sizes did this well, and "
                          f"the bootstrap says there's a {round(prob_pos * 100)}% chance the true average is above zero."}
    return {"level": "promising", "headline": "Promising, not proven",
            "detail": f"Positive so far ({t['avg_r']}R per trade), but {round(p_luck * 100)}% of coin-flip traders did as well. "
                      + (f"About {t['trades_needed']} trades at this edge would make it significant." if t.get("trades_needed") else "")}


def analyze(trades: list[dict], curve: list[dict], scores: dict, start: float) -> dict:
    closed = sorted([t for t in trades if t.get("status") == "closed" and t.get("r_result") is not None],
                    key=lambda t: t.get("closed_at") or "")
    rs = [t["r_result"] for t in closed]
    tests = trade_tests(rs)
    risk = mean([t["risk_usd"] for t in closed if t.get("risk_usd")]) if any(t.get("risk_usd") for t in closed) else start * 0.015
    conv = [(t["conviction"], t["r_result"]) for t in closed if isinstance(t.get("conviction"), (int, float))]
    conf = [(t["confidence_pct"], t["r_result"]) for t in closed if isinstance(t.get("confidence_pct"), (int, float))]
    hist = {}
    for r in rs:
        b = max(-3.0, min(4.0, math.floor(r * 2) / 2))
        hist[b] = hist.get(b, 0) + 1
    dates, dret, sret = daily_returns(curve)
    eq_by_day: dict[str, float] = {}
    for p in curve:
        eq_by_day[p["t"][:10]] = p["equity"]
    peak, dd = 0.0, []
    for p in curve:
        peak = max(peak, p["equity"])
        dd.append({"t": p["t"], "dd_pct": round((p["equity"] / peak - 1) * 100, 2)})
    rolling = []
    for i in range(len(rs)):
        w = rs[max(0, i - 19):i + 1]
        rolling.append(round(sum(r > 0 for r in w) / len(w) * 100, 1))
    return {
        "verdict": verdict(tests), "trades": tests,
        "cum_r": [round(sum(rs[:i + 1]), 2) for i in range(len(rs))],
        "trade_labels": [f"{t['symbol']} {t.get('closed_at', '')[:10]}" for t in closed],
        "rolling_win_rate": rolling,
        "r_histogram": [{"bin": b, "count": hist[b]} for b in sorted(hist)],
        "by_style": group_edges(closed, "style"), "by_asset": group_edges(closed, "asset"), "by_bucket": group_edges(closed, "bucket"),
        "conviction_vs_r": {"points": [{"x": x, "y": y} for x, y in conv], "stats": correlation([x for x, _ in conv], [y for _, y in conv])},
        "confidence_vs_r": correlation([x for x, _ in conf], [y for _, y in conf]),
        "market": market_tests(curve),
        "daily": [{"date": d, "desk_pct": round(a * 100, 3), "spy_pct": round(b * 100, 3)} for d, a, b in zip(dates, dret, sret)],
        "drawdown": dd[-600:],
        "analysts": analyst_tests(scores),
        "monte_carlo": monte_carlo(rs, risk, curve[-1]["equity"] if curve else start),
    }
