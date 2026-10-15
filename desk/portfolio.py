"""Builds one combined picture of both accounts."""
from __future__ import annotations

from desk.brokers import alpaca, oanda
from desk.risk import Book


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
    details = {"alpaca_account": acct, "alpaca_positions": pos, "alpaca_orders": orders,
               "oanda_summary": fx, "oanda_trades": fx_trades}
    return book, details
