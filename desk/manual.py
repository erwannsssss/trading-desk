"""Erwann's own calls from Telegram: take profit (or cut) on a position whenever he wants.

  /close             lists what's open, with the profit or loss on each
  /close V           closes the whole V position at market now
  /close EURUSD half takes half off (currency and crypto positions), keeping the rest and its stop

The desk treats it like any other exit: the trade is closed in the ledger with the reason "closed by Erwann",
P&L is booked, and the reviewer still writes the post-mortem.
"""
from __future__ import annotations

from desk import activity as floor
from desk.state import log_event
from desk.telegram import esc
from desk.trading import ledger


def _norm(s: str) -> str:
    return s.upper().replace("/", "").replace("_", "").replace("-", "")


def _open_trades(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t.get("status") == "open"]


def _pnl_now(t: dict) -> tuple[float | None, float | None]:
    """(unrealized $ P&L, result in R) from the live price."""
    from desk.trading.engine import current_price, r_multiple
    try:
        if t["asset"] == "option":   # the broker's own value of the legs (our mark can be stale)
            from desk.brokers import alpaca
            legs = {t.get("option_symbol"), t.get("short_option_symbol"), t.get("hedge_option_symbol")} - {None}
            usd = sum(float(p["unrealized_pl"]) for p in alpaca.positions() if p["symbol"] in legs)
            return round(usd, 2), None
        px = current_price(t)
        entry = t.get("entry_price") or t["entry_plan"]
        sign = 1 if t["direction"] == "long" else -1
        usd = (px - entry) * sign * float(t.get("filled_qty") or 0) * t.get("quote_to_usd", 1.0) + t.get("partial_pnl", 0)
        return round(usd, 2), round(r_multiple(t["direction"], entry, t["initial_stop"], px), 2)
    except Exception:
        return None, None


def listing(trades: list[dict]) -> str:
    rows = _open_trades(trades)
    if not rows:
        return "Nothing open right now."
    lines = ["<b>Open positions</b> (send /close SYMBOL to take it off, or /close SYMBOL half for currencies and crypto)"]
    for t in rows:
        usd, r = _pnl_now(t)
        money = f"${usd:+,.2f}" if usd is not None else "P&amp;L n/a"
        lines.append(f"{esc(t['symbol'])} {t['direction']}{' (options)' if t['asset'] == 'option' else ''}: {money}"
                     + (f" ({r:+.2f}R)" if r is not None else "") + f"  /close {esc(_norm(t['symbol']))}")
    return "\n".join(lines)


def close(args: list[str], state: dict) -> str:
    from desk.portfolio import snapshot
    from desk.trading.engine import Engine
    trades = ledger.load()
    if not args:
        return listing(trades)
    want = _norm(args[0])
    half = len(args) > 1 and args[1].lower() in ("half", "50", "50%")
    matches = [t for t in _open_trades(trades) if _norm(t["symbol"]) == want]
    if not matches:
        return f"No open position in {esc(args[0].upper())}.\n\n" + listing(trades)
    book, _ = snapshot(state)
    eng = Engine(state, book)
    eng.trades = trades
    out = []
    for t in matches:
        sym = t["symbol"]
        usd, r = _pnl_now(t)
        if t["asset"] in ("stock", "etf", "option") and not eng.stock_market_open():
            out.append(f"The stock market is closed, so {esc(sym)} can't be sold until it opens. Send the command again then.")
            continue
        if half:
            if t["asset"] not in ("forex", "crypto") or (t["asset"] == "crypto" and t["direction"] != "long"):
                out.append(f"Taking half off works for currency and crypto positions only. Send /close {esc(_norm(sym))} to close all of {esc(sym)}.")
                continue
            before = t["filled_qty"]
            from desk.trading.engine import current_price
            eng._take_partial(t, float(current_price(t)))
            if t["filled_qty"] == before:
                out.append(f"{esc(sym)} is too small to split; send /close {esc(_norm(sym))} to close it all.")
                continue
            ledger.note(t, "half closed by Erwann (manual take-profit)", eng.iso)
            out.append(f"Took half of {esc(sym)} off. Booked ${t.get('partial_pnl', 0):+,.2f} so far; the rest keeps its stop at {t['stop']}.")
        else:
            eng._exit_now(t, "closed by Erwann (manual take-profit)" if (usd or 0) >= 0 else "closed by Erwann")
            out.append(f"Closing {esc(sym)} at market" + (f": about ${usd:+,.2f}" if usd is not None else "")
                       + (f" ({r:+.2f}R)" if r is not None else "") + ". The final number lands at the next check.")
        floor.event("partner", f"Erwann {'took half of' if half else 'closed'} {sym} himself" + (f" at about ${usd:+,.2f}" if usd is not None else ""), sym, kind="decision")
        log_event("manual_close", trade=t["id"], symbol=sym, half=half, pnl_est=usd)
    ledger.save(trades)
    return "\n".join(out)
