"""Refunds for operational errors: money the firm lost through a mistake outside its trading (for example, a test
order placed by hand) is reimbursed on the firm's books, the way a real firm makes clients whole after an error.

The broker accounts themselves can't be topped up, so each refund is recorded here and added to firm equity.
It shows as its own line in the account breakdown and the income statement, never as trading profit.
"""
from __future__ import annotations

import json

from desk import config

FILE = config.DATA_DIR / "corrections.json"


def load() -> list[dict]:
    try:
        return json.loads(FILE.read_text()) if FILE.exists() else []
    except Exception:
        return []


def total() -> float:
    return round(sum(c.get("amount", 0) for c in load()), 2)
