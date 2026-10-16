"""/close: Erwann takes profit himself. Brokers are faked; nothing real is sold."""
import unittest
from unittest import mock

from desk import commands, manual


def trade(sym, asset="stock", direction="long", **kw):
    t = {"id": f"t-{sym}", "symbol": sym, "asset": asset, "venue": "oanda" if asset == "forex" else "alpaca",
         "direction": direction, "status": "open", "entry_price": 100.0, "entry_plan": 100.0, "initial_stop": 95.0,
         "stop": 95.0, "target": 110.0, "filled_qty": 10.0, "notes": []}
    t.update(kw)
    return t


class CloseTests(unittest.TestCase):
    def setUp(self):
        self.trades = [trade("V", direction="short"), trade("EUR_GBP", "forex", filled_qty=20000.0)]
        self.saved = None
        patches = [mock.patch("desk.trading.ledger.load", side_effect=lambda: self.trades),
                   mock.patch("desk.trading.ledger.save", side_effect=lambda t: setattr(self, "saved", t)),
                   mock.patch("desk.trading.engine.current_price", return_value=104.0),
                   mock.patch("desk.portfolio.snapshot", return_value=(mock.Mock(), {})),
                   mock.patch("desk.activity.event"), mock.patch("desk.manual.log_event")]
        for p in patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        self.eng = mock.Mock(iso="2026-10-16T13:00:00-04:00")
        self.eng.stock_market_open.return_value = True
        e = mock.patch("desk.trading.engine.Engine", return_value=self.eng)
        e.start()

    def test_lists_positions_with_pnl(self):
        out = commands.handle("/close", {})
        self.assertIn("V short: $-40.00 (-0.80R)", out)
        self.assertIn("/close EURGBP", out)

    def test_closes_one_position_by_symbol(self):
        out = commands.handle("/close eurgbp", {})
        self.eng._exit_now.assert_called_once()
        t, reason = self.eng._exit_now.call_args[0]
        self.assertEqual(t["symbol"], "EUR_GBP")
        self.assertIn("closed by Erwann (manual take-profit)", reason)     # it was up when closed
        self.assertIn("Closing EUR_GBP", out)
        self.assertIs(self.saved, self.trades)

    def test_half_only_for_currency_and_crypto(self):
        out = commands.handle("/close V half", {})
        self.eng._take_partial.assert_not_called()
        self.assertIn("currency and crypto positions only", out)
        def halve(t, px):
            t["filled_qty"] /= 2
            t["partial_pnl"] = 40.0
        self.eng._take_partial.side_effect = halve
        out = commands.handle("/close EUR_GBP half", {})
        self.assertIn("Took half of EUR_GBP off", out)
        self.eng._exit_now.assert_not_called()

    def test_waits_for_the_stock_market(self):
        self.eng.stock_market_open.return_value = False
        out = commands.handle("/close V", {})
        self.eng._exit_now.assert_not_called()
        self.assertIn("stock market is closed", out)

    def test_unknown_symbol_shows_the_list(self):
        self.assertIn("No open position in NVDA", commands.handle("/close NVDA", {}))


if __name__ == "__main__":
    unittest.main()
