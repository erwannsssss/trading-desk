"""How good is Kronos on our kind of trades? Forecast past dates and compare with what actually happened.

Usage: python scripts/kronos_backtest.py [symbols...]   -> writes data/kronos_backtest.json
"""
import json, sys, time
from datetime import datetime, timezone

sys.path.insert(0, ".")
from desk import config, marketdata
from desk.research import kronos

SYMS = sys.argv[1:] or ["SPY", "QQQ", "IWM", "AAPL", "NVDA", "AMD", "TSLA", "META", "AMZN", "JPM", "XOM", "PLTR", "IONQ", "COIN", "MU", "SOFI"]
DATES = 14          # forecast points per symbol, every 7 trading days going back
STEP, H = 7, 5
LOOKBACKS = [int(x) for x in (__import__("os").environ.get("KB_LOOKBACKS") or str(kronos.LOOKBACK)).split(",")]
kronos.PATHS = int(__import__("os").environ.get("KB_PATHS") or 10)

bars = marketdata.stock_daily_bars(SYMS, days=900)
rows = []
t0 = time.time()
for lb in LOOKBACKS:
    kronos.LOOKBACK = lb
    for d in range(DATES):
        cut = -(H + 1 + d * STEP)
        series = {s: (b[:cut + 1] if cut + 1 else b, "stock") for s, b in bars.items() if len(b) > lb + 200}
        f = kronos.forecast(series, H)
        for s, v in f.items():
            past = series[s][0]
            fut = bars[s][len(past):len(past) + H]
            if len(fut) < H:
                continue
            p0 = past[-1]["c"]
            actual = (fut[-1]["c"] / p0 - 1) * 100
            atr = sum(x["h"] - x["l"] for x in past[-14:]) / 14 / p0 * 100
            # a typical desk trade: stop 1.5 ATR, target 2.5 ATR, in Kronos' direction
            long = v["expected_move_pct"] > 0
            stop = p0 * (1 - 1.5 * atr / 100) if long else p0 * (1 + 1.5 * atr / 100)
            tgt = p0 * (1 + 2.5 * atr / 100) if long else p0 * (1 - 2.5 * atr / 100)
            res = 0.0
            for x in fut:
                if (x["l"] <= stop) if long else (x["h"] >= stop):
                    res = -1.0; break
                if (x["h"] >= tgt) if long else (x["l"] <= tgt):
                    res = 2.5 / 1.5; break
            else:
                res = ((fut[-1]["c"] - p0) if long else (p0 - fut[-1]["c"])) / (1.5 * atr / 100 * p0)
            odds = kronos.hit_odds(v["_paths"], long, stop, tgt)
            from desk.research import quant
            raw = quant.kronos_r(v["_paths"], long, p0, stop, tgt)["expected_r"]
            rows.append({"lb": lb, "sym": s, "d": d, "exp": v["expected_move_pct"], "up": v["prob_up_pct"],
                         "strength": v["signal_strength"], "actual": round(actual, 2), "r": round(res, 2),
                         "tgt_first": odds["target_first_pct"], "raw_er": raw})
        print(f"lb {lb} date {d}: {len(rows)} rows, {time.time() - t0:.0f}s", flush=True)


def stats(rs):
    if not rs:
        return None
    hit = sum((r["exp"] > 0) == (r["actual"] > 0) for r in rs) / len(rs)
    return {"n": len(rs), "direction_hit_pct": round(hit * 100, 1), "avg_r_trading_its_call": round(sum(r["r"] for r in rs) / len(rs), 3)}


out = {"at": datetime.now(timezone.utc).isoformat(), "horizon_days": H, "symbols": SYMS, "by_lookback": {}}
for lb in LOOKBACKS:
    rs = [r for r in rows if r["lb"] == lb]
    strong = [r for r in rs if r["strength"] >= 1.0]
    conf = [r for r in rs if r["up"] >= 70 or r["up"] <= 30]
    out["by_lookback"][lb] = {"all": stats(rs), "strong_signal": stats(strong), "confident_70pct": stats(conf)}
    # Kronos' own estimate of a trade's expected R vs what those trades really made: the scale that calibrates it
    raw = sum(r["raw_er"] for r in rs) / max(1, len(rs))
    real = sum(r["r"] for r in rs) / max(1, len(rs))
    out["by_lookback"][lb]["model_expected_r"] = round(raw, 3)
    out["by_lookback"][lb]["r_scale"] = round(max(0.0, min(1.0, real / raw)) if raw > 0 else 0.0, 3)
    hi = [r for r in rs if r["raw_er"] >= 0.5]
    out["by_lookback"][lb]["model_says_over_0_5R"] = stats(hi)
# always-long baseline (the market mostly went up)
base = [r for r in rows if r["lb"] == LOOKBACKS[0]]
out["baseline_always_up_pct"] = round(sum(r["actual"] > 0 for r in base) / max(1, len(base)) * 100, 1)
out["note"] = "5-day direction calls; avg_r trades each call with a 1.5 ATR stop and 2.5 ATR target"
(config.DATA_DIR / "kronos_backtest.json").write_text(json.dumps(out, indent=1))
print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=1))
