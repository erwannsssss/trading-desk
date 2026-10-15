import unittest
from datetime import date
from unittest import mock

from desk import learning
from desk.backtest import _setup_key, simulate
from desk.trading import options


def closed(style, pnl, r, conf=60):
    return {"status": "closed", "style": style, "pnl_usd": pnl, "r_result": r, "confidence_pct": conf,
            "closed_at": "2099-01-01T10:00:00-05:00"}


class TournamentTests(unittest.TestCase):
    def test_winning_style_gets_bigger_bets(self):
        trades = [closed("Momentum", 100, 2.0)] * 3 + [closed("breakout", -50, -1.0)] * 3
        t = learning.compute_tournament(trades)
        w = {b["style"]: b["weight"] for b in t["board"]}
        self.assertEqual(w["momentum"], 1.5)
        self.assertEqual(w["breakout"], 0.7)

    def test_needs_three_trades(self):
        t = learning.compute_tournament([closed("macro", 100, 3.0)] * 2)
        self.assertEqual(t["board"][0]["weight"], 1.0)

    def test_overconfidence_shrinks_bets(self):
        trades = [closed("x", -10, -1, conf=75)] * 6 + [closed("x", 10, 1, conf=75)] * 2  # 25% wins vs 75% predicted
        self.assertEqual(learning.compute_tournament(trades)["calibration"]["factor"], 0.8)

    def test_risk_multiplier_reads_file(self):
        board = {"board": [{"style": "momentum", "weight": 1.5}], "calibration": {"factor": 0.8}}
        with mock.patch.object(learning, "_load", return_value=board):
            self.assertAlmostEqual(learning.risk_multiplier("Momentum"), 1.2)
            self.assertAlmostEqual(learning.risk_multiplier("unknown"), 0.8)


class OptionsTests(unittest.TestCase):
    def test_parse_occ(self):
        o = options.parse("AAPL261120C00345000")
        self.assertEqual((o["underlying"], o["type"], o["strike"], o["expiry"]), ("AAPL", "call", 345.0, date(2026, 11, 20)))
        self.assertEqual(options.parse("SPY261030P00771500")["strike"], 771.5)

    def test_contract_count(self):
        self.assertEqual(options.contracts_for(600, 2.50), 2)
        self.assertEqual(options.contracts_for(200, 4.25), 0)

    def test_days_to_expiry(self):
        self.assertEqual(options.days_to_expiry("AAPL261120C00345000", date(2026, 11, 10)), 10)


class BacktestTests(unittest.TestCase):
    bars = [{"o": 100, "h": 101, "l": 99, "c": 100}] + [{"o": 100, "h": 106, "l": 99.5, "c": 105}]

    def test_target_hit(self):
        self.assertAlmostEqual(simulate(self.bars, 0, "long", 2.0), 2.5 / 1.5)

    def test_stop_hit(self):
        bars = [{"o": 100, "h": 101, "l": 99, "c": 100}, {"o": 100, "h": 100.5, "l": 96, "c": 97}]
        self.assertEqual(simulate(bars, 0, "long", 2.0), -1.0)

    def test_setup_keys(self):
        self.assertEqual(_setup_key("volume surge (3.6x normal)"), "volume surge")
        self.assertEqual(_setup_key("strong 20-day move (-17.7%)"), "strong 20-day move")


if __name__ == "__main__":
    unittest.main()
