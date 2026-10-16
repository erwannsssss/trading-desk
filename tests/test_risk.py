import unittest
from datetime import datetime, timedelta

from desk import config
from desk.risk import Book, OrderIdea, check_order, daily_limit_hit, in_blackout


def book(**kw):
    base = dict(alpaca_equity=10_000, oanda_nav=10_000, baseline_alpaca=10_000, baseline_oanda=10_000)
    base.update(kw)
    return Book(**base)


def idea(**kw):
    base = dict(symbol="AAPL", venue="alpaca", side="buy", notional=1_000, entry=100, stop=95, target=115)
    base.update(kw)
    return OrderIdea(**base)


STATE = {"paused": False, "halted_for_day": False, "trades_today": 0}


class RiskTests(unittest.TestCase):
    def test_equity_combines_both_accounts(self):
        self.assertEqual(book(alpaca_equity=10_500, oanda_nav=9_800).equity, 10_300)

    def test_normal_trade_approved(self):
        self.assertTrue(check_order(idea(), book(), STATE).ok)

    def test_kill_switch_blocks(self):
        self.assertFalse(check_order(idea(), book(), {**STATE, "paused": True}).ok)

    def test_closing_always_allowed_even_when_paused(self):
        self.assertTrue(check_order(idea(opens_position=False), book(), {**STATE, "paused": True}).ok)

    def test_trade_cap(self):
        v = check_order(idea(), book(), {**STATE, "trades_today": config.MAX_TRADES_PER_DAY})
        self.assertFalse(v.ok)

    def test_position_too_big(self):
        self.assertTrue(check_order(idea(notional=9_000, stop=99), book(), STATE).ok)
        self.assertFalse(check_order(idea(notional=12_000, stop=99), book(), STATE).ok)  # over 100% of equity in one name

    def test_needs_stop(self):
        self.assertFalse(check_order(idea(stop=None), book(), STATE).ok)

    def test_stop_wrong_side(self):
        self.assertFalse(check_order(idea(stop=105), book(), STATE).ok)
        self.assertTrue(check_order(idea(side="sell", stop=105, target=90), book(), STATE).ok)

    def test_risk_per_trade(self):
        # $2,500 position with a 30% stop = $750 at risk > $500 limit
        self.assertFalse(check_order(idea(notional=2_500, stop=70), book(), STATE).ok)

    def test_credit_line_cap(self):
        self.assertTrue(check_order(idea(), book(stock_gross_exposure=60_000, forex_gross_notional=40_000), STATE).ok)
        self.assertFalse(check_order(idea(), book(stock_gross_exposure=70_000, forex_gross_notional=40_000), STATE).ok)

    def test_total_open_risk_cap(self):
        self.assertTrue(check_order(idea(), book(open_risk=3_900), STATE).ok)    # +$50 at risk -> $3,950
        self.assertFalse(check_order(idea(notional=3_000, stop=90), book(open_risk=3_900), STATE).ok)  # +$300 -> over $4,000 (40%)

    def test_interest_reduces_equity(self):
        self.assertEqual(book(financing_cost=25).equity, 9_975)

    def test_forex_cap(self):
        fx = idea(symbol="EUR_USD", venue="oanda", entry=1.10, stop=1.09, target=1.12, notional=2_000)
        self.assertTrue(check_order(fx, book(), STATE).ok)
        self.assertTrue(check_order(OrderIdea(**{**fx.__dict__, "notional": 25_000}), book(), STATE).ok)
        tight = OrderIdea(**{**fx.__dict__, "stop": 1.099})                     # tiny stop, so only the size cap binds
        self.assertTrue(check_order(OrderIdea(**{**tight.__dict__, "notional": 55_000}), book(), STATE).ok)
        self.assertFalse(check_order(OrderIdea(**{**tight.__dict__, "notional": 65_000}), book(), STATE).ok)  # over 6x equity

    def test_daily_loss_limit(self):
        self.assertTrue(daily_limit_hit(book(alpaca_equity=9_400), 10_000))
        self.assertFalse(daily_limit_hit(book(alpaca_equity=9_500), 10_000))

    def test_blackout(self):
        open_ = datetime(2026, 10, 15, 9, 30)
        close = datetime(2026, 10, 15, 16, 0)
        self.assertTrue(in_blackout(open_, close, open_ + timedelta(minutes=2)))
        self.assertFalse(in_blackout(open_, close, open_ + timedelta(hours=2)))
        self.assertTrue(in_blackout(open_, close, close - timedelta(minutes=1)))


if __name__ == "__main__":
    unittest.main()
