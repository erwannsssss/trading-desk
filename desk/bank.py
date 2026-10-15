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


def loan_needed(notional: float, equity: float, exposure_now: float) -> float:
    """How much of a new position has to be borrowed: whatever the firm's free cash can't cover."""
    free = max(0.0, equity - exposure_now)
    return round(max(0.0, notional - free), 2)


def interest(loan: float, start_iso: str | None, end: datetime) -> float:
    if not loan or not start_iso:
        return 0.0
    days = max(0.0, (end - datetime.fromisoformat(start_iso)).total_seconds() / 86400)
    return round(loan * LOAN_APR * days / 365, 2)


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
                accruing += interest(loan, t["opened_at"], at)
        elif t.get("status") == "closed":
            paid += t.get("interest_usd") or 0
    return {"credit_line": CREDIT_LINE, "apr_pct": LOAN_APR * 100, "bank": BANK_NAME,
            "borrowed": round(borrowed, 2), "available": round(CREDIT_LINE - borrowed, 2),
            "interest_paid": round(paid, 2), "interest_accruing": round(accruing, 2),
            "financing_cost": round(paid + accruing, 2)}
