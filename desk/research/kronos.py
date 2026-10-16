"""Kronos: an open-source AI model trained only on price charts (candlesticks from 45+ exchanges).

Given the last ~1-2 years of daily candles it draws many possible futures for the next few days. From those paths
we read off where the price is most likely to go, how wide the range is, and, for a planned trade, how often the
target is reached before the stop. https://github.com/shiyu-coder/Kronos (MIT licence; code vendored in desk/vendor).

The model is optional: if torch or the weights aren't available (e.g. the 15-minute trading job), every function
returns None and the desk works exactly as before.
"""
from __future__ import annotations

import os
import time

MODEL = os.environ.get("KRONOS_MODEL", "NeoQuasar/Kronos-small")
TOKENIZER = "NeoQuasar/Kronos-Tokenizer-base"
LOOKBACK = 64         # daily candles fed in. Backtest (scripts/kronos_backtest.py, Oct 2026, 16 stocks x 14 weeks):
                      # 64 days called the 5-day direction right 56% of the time (+0.17R a trade); 128 days: 51%.
                      # 400 days drifted badly (it treats long trends as stretched and forecasts big reversals).
PATHS = 20            # possible futures drawn per symbol
HORIZON = {"intraday": 1, "1-5 days": 5, "1-4 weeks": 15}
MAX_BATCH = 8         # symbols per batch (x PATHS sample rows)

_pred = None
_failed = None


def available() -> bool:
    return _predictor() is not None


def _predictor():
    global _pred, _failed
    if _pred is not None or _failed:
        return _pred
    try:
        import torch
        from desk.vendor.kronos import Kronos, KronosPredictor, KronosTokenizer
        torch.set_num_threads(max(1, os.cpu_count() or 1))
        t0 = time.time()
        tok = KronosTokenizer.from_pretrained(TOKENIZER)
        model = Kronos.from_pretrained(MODEL)
        tok.eval(); model.eval()
        _pred = KronosPredictor(model, tok, device="cpu", max_context=512)
        print(f"Kronos loaded ({MODEL}) in {time.time() - t0:.1f}s")
    except Exception as exc:
        _failed = str(exc)[:200]
        print(f"Kronos unavailable: {_failed}")
    return _pred


def _future_times(last, steps: int, asset: str):
    import pandas as pd
    if asset == "crypto":
        return pd.Series(pd.date_range(last + pd.Timedelta(days=1), periods=steps, freq="D"))
    return pd.Series(pd.bdate_range(last + pd.Timedelta(days=1), periods=steps))


def _frame(bars: list[dict]):
    import pandas as pd
    df = pd.DataFrame(bars[-LOOKBACK:])
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["amount"] = df["volume"] * df[["open", "high", "low", "close"]].mean(axis=1)
    ts = pd.to_datetime(df["t"], utc=True, format="ISO8601").dt.tz_localize(None).dt.normalize()
    return df[["open", "high", "low", "close", "volume", "amount"]].astype(float), ts


def paths(series: dict[str, tuple[list[dict], str]], steps: int = 5) -> dict[str, list[dict]]:
    """{symbol: (daily bars, asset)} -> {symbol: [path, ...]} where a path is {"high": [...], "low": [...], "close": [...]}."""
    pred = _predictor()
    if pred is None:
        return {}
    import torch
    jobs = []
    for sym, (bars, asset) in series.items():
        if not bars or len(bars) < 120:
            continue
        try:
            df, ts = _frame(bars)
            if df.isnull().values.any() or (df["close"] <= 0).any():
                continue
            jobs.append((sym, df, ts, _future_times(ts.iloc[-1], steps, asset)))
        except Exception as exc:
            print(f"Kronos skip {sym}: {exc}")
    out: dict[str, list[dict]] = {}
    per = max(1, MAX_BATCH)
    for i in range(0, len(jobs), per):
        chunk = jobs[i:i + per]
        dfs, xs, ys, owners = [], [], [], []
        for sym, df, ts, fut in chunk:
            for _ in range(PATHS):     # each copy is sampled independently = one possible future
                dfs.append(df); xs.append(ts); ys.append(fut); owners.append(sym)
        try:
            with torch.no_grad():
                preds = pred.predict_batch(dfs, xs, ys, pred_len=steps, T=1.0, top_p=0.9, sample_count=1, verbose=False)
        except Exception as exc:
            print(f"Kronos batch failed: {exc}")
            continue
        for sym, p in zip(owners, preds):
            out.setdefault(sym, []).append({"high": p["high"].tolist(), "low": p["low"].tolist(), "close": p["close"].tolist()})
    return out


def summarize(sym_paths: list[dict], price: float, atr_pct: float | None = None) -> dict | None:
    """Turn the possible futures into plain numbers the analysts can cite."""
    if not sym_paths or not price:
        return None
    finals = sorted(p["close"][-1] for p in sym_paths)
    n = len(finals)

    def q(x):
        return finals[min(n - 1, int(x * n))]
    rets = [(f / price - 1) * 100 for f in finals]
    exp = sum(rets) / n
    up = sum(r > 0 for r in rets) / n
    spread = (q(0.9) - q(0.1)) / price * 100
    steps = len(sym_paths[0]["close"])
    return {
        "horizon_days": steps,
        "expected_move_pct": round(exp, 2),
        "median_move_pct": round((q(0.5) / price - 1) * 100, 2),
        "prob_up_pct": round(up * 100),
        "range_low_10pct": round(q(0.1), 6 if price < 5 else 2),
        "range_high_90pct": round(q(0.9), 6 if price < 5 else 2),
        "range_width_pct": round(spread, 2),
        "direction": "up" if up >= 0.6 and exp > 0 else "down" if up <= 0.4 and exp < 0 else "unclear",
        "signal_strength": round(abs(exp) / (spread / 2.56) if spread else 0, 2),   # expected move vs its own uncertainty
        "paths": n,
    }


def hit_odds(sym_paths: list[dict], long: bool, stop: float, target: float) -> dict | None:
    """Out of the possible futures, how often is the target touched before the stop? (Same-day touch counts as a loss.)"""
    if not sym_paths or not stop or not target:
        return None
    win = loss = 0
    for p in sym_paths:
        res = None
        for hi, lo in zip(p["high"], p["low"]):
            hit_stop = lo <= stop if long else hi >= stop
            hit_tgt = hi >= target if long else lo <= target
            if hit_stop:
                res = "loss"; break
            if hit_tgt:
                res = "win"; break
        win += res == "win"
        loss += res == "loss"
    n = len(sym_paths)
    return {"target_first_pct": round(win / n * 100), "stop_first_pct": round(loss / n * 100),
            "neither_pct": round((n - win - loss) / n * 100), "paths": n}


def forecast(series: dict[str, tuple[list[dict], str]], steps: int = 5) -> dict[str, dict]:
    """{symbol: summary + the raw paths (key '_paths', dropped before saving)}."""
    t0 = time.time()
    got = paths(series, steps)
    out = {}
    for sym, ps in got.items():
        bars = series[sym][0]
        s = summarize(ps, bars[-1]["c"])
        if s:
            s["_paths"] = ps
            out[sym] = s
    if got:
        print(f"Kronos forecast {len(out)} symbols in {time.time() - t0:.0f}s")
    return out


def public(f: dict | None) -> dict | None:
    return {k: v for k, v in f.items() if not k.startswith("_")} if f else None
