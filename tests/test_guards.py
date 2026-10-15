import unittest
from datetime import date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

from desk import bank
from desk.trading import guards

NY = ZoneInfo("America/New_York")


def tr(symbol, asset, direction, style="mean reversion", status="open"):
    return {"symbol": symbol, "asset": asset, "direction": direction, "style": style, "status": status}


class GuardTests(unittest.TestCase):
    def test_same_currency_limit(self):
        held = [tr("USD_JPY", "forex", "long"), tr("USD_CAD", "forex", "long")]
        self.assertIn("USD", guards.same_bet(held, "forex", "USD_CHF", "long", "macro"))
        self.assertIn("USD", guards.same_bet(held, "forex", "EUR_USD", "short", "macro"))   # short EUR/USD = long USD
        self.assertIsNone(guards.same_bet(held, "forex", "EUR_USD", "long", "macro"))      # the opposite bet is fine
        self.assertIsNone(guards.same_bet(held, "forex", "EUR_GBP", "long", "macro"))

    def test_crypto_and_stock_style_limits(self):
        cryptos = [tr(s, "crypto", "long") for s in ("BTC/USD", "ETH/USD", "SOL/USD")]
        self.assertIsNotNone(guards.same_bet(cryptos, "crypto", "DOGE/USD", "long", "momentum"))
        stocks = [tr(s, "stock", "long") for s in ("FMC", "ONDS", "RKLX")]
        self.assertIsNotNone(guards.same_bet(stocks, "stock", "WULF", "long", "mean reversion"))
        self.assertIsNone(guards.same_bet(stocks, "stock", "WULF", "long", "breakout"))
        closed = [tr(s, "stock", "long", status="closed") for s in ("A", "B", "C")]
        self.assertIsNone(guards.same_bet(closed, "stock", "WULF", "long", "mean reversion"))

    def test_earnings_rules(self):
        today = date(2026, 10, 15)
        self.assertIsNotNone(guards.earnings_conflict("2026-10-19", "1-5 days", "momentum", today))
        self.assertIsNone(guards.earnings_conflict("2026-10-19", "1-5 days", "earnings play", today))
        self.assertIsNone(guards.earnings_conflict("2026-11-30", "1-5 days", "momentum", today))
        now = datetime(2026, 10, 15, 15, 30, tzinfo=NY)   # Thursday afternoon
        t = tr("AAPL", "stock", "long", style="momentum")
        self.assertIsNotNone(guards.must_exit_before_earnings(t, {"AAPL": {"date": "2026-10-16", "hour": "bmo"}}, now))
        self.assertIsNotNone(guards.must_exit_before_earnings(t, {"AAPL": {"date": "2026-10-15", "hour": "amc"}}, now))
        self.assertIsNone(guards.must_exit_before_earnings(t, {"AAPL": {"date": "2026-10-16", "hour": "amc"}}, now))
        fri = datetime(2026, 10, 16, 15, 30, tzinfo=NY)
        self.assertIsNotNone(guards.must_exit_before_earnings(t, {"AAPL": {"date": "2026-10-19", "hour": "bmo"}}, fri))

    def test_risk_off_needs_conviction_6(self):
        self.assertIsNotNone(guards.risk_off_block("risk-off/defensive", "stock", "long", 5))
        self.assertIsNone(guards.risk_off_block("risk-off/defensive", "stock", "long", 6))
        self.assertIsNone(guards.risk_off_block("risk-off/defensive", "forex", "long", 4))
        self.assertIsNone(guards.risk_off_block("strong uptrend", "stock", "long", 4))

    def test_forex_borrows_only_its_margin(self):
        self.assertEqual(bank.loan_needed(25_796 * 0.02, 10_000, 3_000), 0.0)
        self.assertEqual(bank.loan_needed(5_000, 10_000, 8_000), 3_000.0)
        trades = [{"symbol": "USD_CAD", "asset": "forex", "status": "open", "loan_usd": 25_796.0, "notional_usd": 25_796.0}]
        with mock.patch("desk.brokers.oanda.margin_rate", return_value=0.02):
            notes = bank.reprice_forex_loans(trades)
        self.assertAlmostEqual(trades[0]["loan_usd"], 515.92)
        self.assertEqual(len(notes), 1)
        with mock.patch("desk.brokers.oanda.margin_rate", return_value=0.02):
            self.assertEqual(bank.reprice_forex_loans(trades), [])   # only once


if __name__ == "__main__":
    unittest.main()
