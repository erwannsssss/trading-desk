import unittest
from datetime import datetime

from desk import bank


class BankTests(unittest.TestCase):
    def test_borrow_only_what_cash_cannot_cover(self):
        self.assertEqual(bank.loan_needed(4_000, 10_000, 0), 0)
        self.assertEqual(bank.loan_needed(4_000, 10_000, 8_000), 2_000)
        self.assertEqual(bank.loan_needed(4_000, 10_000, 15_000), 4_000)

    def test_interest_for_days_held(self):
        start = "2026-01-01T10:00:00-05:00"
        self.assertAlmostEqual(bank.interest(36_500, start, datetime.fromisoformat("2026-01-11T10:00:00-05:00")), 80.0)

    def test_totals(self):
        at = datetime.fromisoformat("2026-01-11T10:00:00-05:00")
        trades = [{"status": "open", "loan_usd": 36_500, "opened_at": "2026-01-01T10:00:00-05:00"},
                  {"status": "pending", "loan_usd": 1_000},
                  {"status": "closed", "loan_usd": 5_000, "interest_usd": 12.5}]
        t = bank.totals(trades, at)
        self.assertEqual(t["borrowed"], 37_500)
        self.assertEqual(t["interest_accruing"], 80.0)
        self.assertEqual(t["financing_cost"], 92.5)
        self.assertEqual(t["available"], 62_500)


if __name__ == "__main__":
    unittest.main()
