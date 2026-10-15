"""The firm's credit line with the (figurative) bank.

The firm can borrow up to CREDIT_LINE to take more positions than its own money covers. Each trade borrows
only what the firm's free cash can't cover when it opens. When the trade closes, the loan is repaid plus
interest at LOAN_APR for the days it was out. Gains and losses stay the firm's: the bank only gets its money
back plus interest, so interest is a cost that comes out of the firm's equity.

Borrowing doesn't change how much any one trade may lose: that's still capped at a % of the firm's OWN
money, and so is the total at risk across all open trades (see risk.py).
"""
from __future__ import annotations

from datetime import datetime

from desk import config

CREDIT_LINE = 100_000.0
LOAN_APR = 0.08          # roughly what real brokers charge on margin loans
BANK_NAME = "First Hoosier Bank"
ACTIVE = ("pending", "open", "closing")


BORROW_MIN_CONVICTION = 7   # stock, crypto and options trades below this must fit in the firm's own cash


def loan_needed(cash_needed: float, equity: float, cash_used: float) -> float:
    """How much of a new position has to be borrowed: whatever the firm's free cash can't cover.
    For forex, cash_needed is only the margin (that's all the position ties up), not the full position value."""
    free = max(0.0, equity - cash_used)
    return round(max(0.0, cash_needed - free), 2)


def free_cash(equity: float, cash_used: float) -> float:
    return max(0.0, equity - cash_used)


def reprice_forex_loans(trades: list[dict]) -> list[str]:
    """One-time fix for forex trades opened when loans covered the full position value: from now on they only
    borrow their margin. Interest is figured on the smaller loan for the whole trade."""
    from desk.brokers import oanda
    notes = []
    for t in trades:
        if t.get("asset") == "forex" and t.get("status") in ACTIVE and t.get("loan_usd") and t.get("loan_basis") != "margin":
            margin = round((t.get("notional_usd") or 0) * oanda.margin_rate(t["symbol"]), 2)
            old = t["loan_usd"]
            t["loan_usd"] = min(old, margin)
            t["loan_basis"] = "margin"
            notes.append(f"{t['symbol']}: loan cut from ${old:,.0f} to ${t['loan_usd']:,.0f} (forex only ties up its margin)")
    return notes


def current_apr() -> float:
    """8% a year, or 6.5% once the firm unlocks better bank terms (firm.UNLOCKS)."""
    try:
        from desk import firm
        return firm.loan_apr()
    except Exception:
        return LOAN_APR


def interest(loan: float, start_iso: str | None, end: datetime, apr: float | None = None) -> float:
    if not loan or not start_iso:
        return 0.0
    days = max(0.0, (end - datetime.fromisoformat(start_iso)).total_seconds() / 86400)
    return round(loan * (apr if apr is not None else LOAN_APR) * days / 365, 2)


def totals(trades: list[dict], at: datetime) -> dict:
    """Outstanding loans, interest already paid on closed trades, and interest building up on open ones."""
    borrowed = paid = accruing = 0.0
    for t in trades:
        loan = t.get("loan_usd") or 0
        if not loan:
            continue
        if t.get("status") in ACTIVE:
            borrowed += loan
            if t.get("opened_at"):  # interest starts when the order fills
                accruing += interest(loan, t["opened_at"], at, t.get("loan_apr"))
        elif t.get("status") == "closed":
            paid += t.get("interest_usd") or 0
    return {"credit_line": CREDIT_LINE, "apr_pct": round(current_apr() * 100, 2), "bank": BANK_NAME,
            "borrowed": round(borrowed, 2), "available": round(CREDIT_LINE - borrowed, 2),
            "interest_paid": round(paid, 2), "interest_accruing": round(accruing, 2),
            "financing_cost": round(paid + accruing, 2)}
