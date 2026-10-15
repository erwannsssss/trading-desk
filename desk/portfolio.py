"""Builds one combined picture of both accounts."""
from __future__ import annotations

from desk.brokers import alpaca, oanda
from desk.risk import Book


def _live_risk(t: dict) -> float:
    """What an open trade can still lose: nothing once its stop is at breakeven or better."""
    if t.get("asset") != "option" and t.get("entry_price") and t.get("stop") is not None:
        long = t.get("direction") == "long"
        if (long and t["stop"] >= t["entry_price"]) or (not long and t["stop"] <= t["entry_price"]):
            return 0.0
    return t.get("risk_usd") or 0.0


def snapshot(state: dict) -> tuple[Book, dict]:
    acct = alpaca.account()
    pos = alpaca.positions()
    orders = alpaca.open_orders()
    fx = oanda.summary()
    fx_trades = oanda.open_trades()

    stock_gross = sum(abs(float(p["market_value"])) for p in pos)
    pending = 0.0
    for o in orders:
        if str(o.get("position_intent", "")).endswith("to_open") and o.get("limit_price"):
            remaining = float(o["qty"] or 0) - float(o.get("filled_qty") or 0)
            pending += remaining * float(o["limit_price"])

    baseline = state.get("baseline") or {}
    book = Book(
        alpaca_equity=float(acct["equity"]),
        oanda_nav=float(fx["NAV"]),
        baseline_alpaca=float(baseline.get("alpaca", acct["equity"])),
        baseline_oanda=float(baseline.get("oanda", fx["NAV"])),
        stock_gross_exposure=stock_gross,
        forex_gross_notional=abs(float(fx.get("positionValue", 0) or 0)),
        open_positions=len(pos) + len(fx_trades),
        pending_exposure=pending,
    )
    # Bank loans, interest and total open risk come from the trade journal.
    try:
        from desk import bank
        from desk.state import now
        from desk.trading import ledger
        trades = ledger.load()
        b = bank.totals(trades, now())
        book.financing_cost, book.borrowed = b["financing_cost"], b["borrowed"]
        book.open_risk = sum(_live_risk(t) for t in trades if t.get("status") in bank.ACTIVE)
    except Exception as exc:
        print(f"bank totals failed: {exc}")
    details = {"alpaca_account": acct, "alpaca_positions": pos, "alpaca_orders": orders,
               "oanda_summary": fx, "oanda_trades": fx_trades}
    return book, details
