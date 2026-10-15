"""Trade replay: the price path of each trade, with its entry, stop moves, target and exit, for the dashboard.

Saved as docs/replays/<trade id>.json. Closed trades are drawn once; open trades are redrawn about every hour.
Bars are 15-minute for trades under two days, hourly up to ten days, daily beyond that. Options are drawn on the
underlying stock, since their stop and target are stock prices.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from desk import config, marketdata
from desk.brokers import oanda
from desk.state import now

DIR = config.ROOT / "docs" / "replays"
MAX_PER_RUN = 6
OPEN_REFRESH = timedelta(minutes=55)


def _frame(start: datetime, end: datetime) -> tuple[str, str, timedelta]:
    span = end - start
    if span < timedelta(days=2):
        return "15Min", "M15", timedelta(minutes=15)
    if span < timedelta(days=10):
        return "1Hour", "H1", timedelta(hours=1)
    return "1Day", "D", timedelta(days=1)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bars_for(t: dict) -> tuple[list[dict], str]:
    opened = datetime.fromisoformat(t["opened_at"])
    closed = datetime.fromisoformat(t["closed_at"]) if t.get("closed_at") else now()
    tf, gran, step = _frame(opened, closed)
    pad = max(step * 12, (closed - opened) * 0.25)
    start, end = opened - pad, min(now() - timedelta(minutes=16), closed + pad / 2)
    sym = t["symbol"]
    if t.get("venue") == "oanda" or t.get("asset") == "forex":
        count = min(500, int((now() - start) / step) + 2)
        rows = []
        for c in oanda.candles(sym, gran, count):
            ts = datetime.fromisoformat(c["time"][:19] + "+00:00")
            if start <= ts <= closed + pad / 2:
                m = c["mid"]
                rows.append({"t": _iso(ts), "o": float(m["o"]), "h": float(m["h"]), "l": float(m["l"]), "c": float(m["c"])})
        return rows, tf
    if t.get("asset") == "crypto":
        d = marketdata._alpaca("/v1beta3/crypto/us/bars", {"symbols": sym, "timeframe": tf, "start": _iso(start),
                                                           "end": _iso(min(now(), closed + pad / 2)), "limit": 2000})
        return [marketdata._norm(b) for b in (d.get("bars") or {}).get(sym, [])], tf
    d = marketdata._alpaca("/v2/stocks/bars", {"symbols": sym, "timeframe": tf, "start": _iso(start), "end": _iso(end),
                                               "feed": "sip", "adjustment": "raw", "limit": 2000})
    return [marketdata._norm(b) for b in (d.get("bars") or {}).get(sym, [])], tf


def build(t: dict) -> dict:
    bars, tf = bars_for(t)
    option = t.get("asset") == "option"
    entry = t.get("underlying_entry") if option else t.get("entry_price")
    exit_px = t.get("exit_price") if not option else (t.get("last_price") if t.get("status") == "closed" else None)
    stops = t.get("stop_history") or [{"t": t["opened_at"], "stop": t.get("initial_stop")}]
    return {"id": t["id"], "symbol": t["symbol"], "asset": t.get("asset"), "direction": t.get("direction"),
            "timeframe": tf, "bars": [{"t": b["t"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in bars],
            "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at"), "entry": entry, "exit": exit_px,
            "target": t.get("target"), "initial_stop": t.get("initial_stop"), "stops": stops,
            "exit_reason": t.get("exit_reason"), "pnl_usd": t.get("pnl_usd"), "r_result": t.get("r_result"),
            "notes": t.get("notes") or [], "final": t.get("status") == "closed", "drawn_at": now().isoformat(timespec="minutes"),
            "on_underlying": option}


def update(trades: list[dict]) -> int:
    """Draw replays that are missing or stale. Returns how many were drawn."""
    DIR.mkdir(parents=True, exist_ok=True)
    cutoff = (now() - timedelta(days=60)).isoformat()
    todo = []
    for t in trades:
        if not t.get("opened_at") or t.get("status") not in ("open", "closing", "closed"):
            continue
        if t.get("status") == "closed" and (t.get("closed_at") or "") < cutoff:
            continue
        path = DIR / f"{t['id']}.json"
        if path.exists():
            try:
                old = json.loads(path.read_text())
            except Exception:
                old = {}
            if old.get("final"):
                continue
            if t["status"] != "closed" and old.get("drawn_at") and now() - datetime.fromisoformat(old["drawn_at"]) < OPEN_REFRESH:
                continue
        todo.append(t)
    done = 0
    for t in todo[:MAX_PER_RUN]:
        try:
            r = build(t)
            if r["bars"]:
                (DIR / f"{t['id']}.json").write_text(json.dumps(r, separators=(",", ":"), default=str))
                done += 1
        except Exception as exc:
            print(f"replay for {t['id']} failed: {exc}")
        time.sleep(0.3)
    return done
