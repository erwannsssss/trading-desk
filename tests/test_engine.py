import unittest
from unittest import mock
from datetime import datetime

from desk.trading.engine import r_multiple, time_stop_due, trailed_stop


class EngineTests(unittest.TestCase):
    def test_r_multiple(self):
        self.assertEqual(r_multiple("long", 100, 95, 110), 2)
        self.assertEqual(r_multiple("short", 100, 105, 95), 1)

    def test_no_trail_before_1r(self):
        self.assertIsNone(trailed_stop("long", 100, 95, 95, 104))

    def test_breakeven_at_1r(self):
        self.assertEqual(trailed_stop("long", 100, 95, 95, 105.5), 100)
        self.assertEqual(trailed_stop("short", 100, 105, 105, 94), 100)

    def test_trail_at_2r(self):
        self.assertEqual(trailed_stop("long", 100, 95, 100, 112), 107)
        self.assertEqual(trailed_stop("short", 100, 105, 100, 88), 93)

    def test_stop_never_loosens(self):
        self.assertIsNone(trailed_stop("long", 100, 95, 108, 112))   # trail would be 107 < 108
        self.assertIsNone(trailed_stop("long", 100, 95, 100, 105.2))  # already at breakeven

    def test_time_stops(self):
        opened = "2026-10-15T10:00:00-04:00"
        self.assertFalse(time_stop_due("intraday", opened, datetime.fromisoformat("2026-10-15T15:00:00-04:00")))
        self.assertTrue(time_stop_due("intraday", opened, datetime.fromisoformat("2026-10-15T15:51:00-04:00")))
        self.assertFalse(time_stop_due("1-5 days", opened, datetime.fromisoformat("2026-10-20T10:00:00-04:00")))
        self.assertTrue(time_stop_due("1-5 days", opened, datetime.fromisoformat("2026-10-22T10:00:00-04:00")))


if __name__ == "__main__":
    unittest.main()


class OptionPnlTests(unittest.TestCase):
    def _close(self, t, price):
        from desk.trading import engine
        e = engine.Engine.__new__(engine.Engine)
        e.iso, e.at, e.alerts = "2026-10-15T15:00:00-04:00", None, []
        with mock.patch("desk.trading.engine.floor"), mock.patch("desk.trading.engine.log_event"):
            e._closed(t, price, "test")
        return t

    def _opt(self, **kw):
        t = {"id": "x", "symbol": "IONQ", "asset": "option", "status": "open", "entry_price": 1.43, "filled_qty": 2,
             "qty": 2, "initial_stop": 35, "quote_to_usd": 100.0}
        t.update(kw)
        return t

    def test_bought_puts_profit_when_their_price_rises(self):
        t = self._close(self._opt(direction="short"), 2.43)          # bearish put spread went up $1
        self.assertAlmostEqual(t["pnl_usd"], 200.0)
        self.assertAlmostEqual(t["r_result"], 0.7, places=1)

    def test_bought_calls_lose_when_their_price_falls(self):
        self.assertAlmostEqual(self._close(self._opt(direction="long"), 0.43)["pnl_usd"], -200.0)

    def test_credit_spread_profits_when_it_gets_cheaper(self):
        t = self._close(self._opt(direction="long", credit=True, entry_price=1.20, spread_width=5.0), 0.40)
        self.assertAlmostEqual(t["pnl_usd"], 160.0)                  # kept $0.80 of the $1.20 credit, x2 x100
        self.assertAlmostEqual(t["r_result"], round(160 / (3.8 * 200), 2))


class HedgeTests(unittest.TestCase):
    def _engine(self, trades, equity=10_000):
        from desk.trading import engine
        e = engine.Engine.__new__(engine.Engine)
        e.trades, e.iso, e.alerts = trades, "2026-10-15T11:00:00-04:00", []
        from datetime import datetime
        from zoneinfo import ZoneInfo
        e.at = datetime(2026, 10, 15, 11, 0, tzinfo=ZoneInfo("America/New_York"))
        e.book = mock.Mock(equity=equity)
        e.stock_market_open = lambda: True
        e.blackout = lambda: False
        return e

    def test_exposure_counts_shares_and_option_deltas(self):
        trades = [{"status": "open", "asset": "stock", "direction": "long", "notional_usd": 3000},
                  {"status": "open", "asset": "option", "direction": "short", "underlying_entry": 30, "filled_qty": 2},
                  {"status": "open", "asset": "forex", "direction": "long", "notional_usd": 25000}]
        self.assertAlmostEqual(self._engine(trades).stock_exposure(), 3000 - 0.45 * 30 * 200)

    def test_hedges_when_long_heavy_in_risk_off_and_removes_later(self):
        trades = [{"status": "open", "asset": "stock", "direction": "long", "notional_usd": 4000}]
        e = self._engine(trades)
        with mock.patch("desk.research.planner.latest_macro", return_value={"regime": "risk-off/defensive"}), \
             mock.patch("desk.trading.engine.current_price", return_value=600.0), \
             mock.patch("desk.trading.engine.floor"), mock.patch.object(e, "_enter_option", return_value="entered") as enter:
            e._hedge_check()
        self.assertTrue(enter.called)
        self.assertLessEqual(enter.call_args.kwargs["budget_override"], 150.0)
        trades.append({"status": "open", "style": "hedge", "asset": "option", "direction": "short", "symbol": "SPY"})
        with mock.patch("desk.research.planner.latest_macro", return_value={"regime": "strong uptrend"}), \
             mock.patch("desk.trading.engine.floor"), mock.patch.object(e, "_exit_now") as ex:
            e._hedge_check()
        self.assertTrue(ex.called)

    def test_no_hedge_when_market_read_is_fine(self):
        e = self._engine([{"status": "open", "asset": "stock", "direction": "long", "notional_usd": 9000}])
        with mock.patch("desk.research.planner.latest_macro", return_value={"regime": "strong uptrend"}), \
             mock.patch.object(e, "_enter_option") as enter:
            e._hedge_check()
        self.assertFalse(enter.called)
