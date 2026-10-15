import unittest
import unittest.mock
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


class EdgeTiltTests(unittest.TestCase):
    def _trades(self, rs):
        return [{"status": "closed", "r_result": r, "closed_at": f"2026-10-{1 + i % 28:02d}T12:00:00"} for i, r in enumerate(rs)]

    def test_no_tilt_before_30_trades(self):
        from desk import learning
        self.assertEqual(learning.edge_tilt(self._trades([1.0] * 29))["factor"], 1.0)

    def test_tilt_up_with_proven_edge_and_down_when_losing(self):
        from desk import learning
        winning = [1.5, -1, 2, 1.2, -1, 1.8] * 6
        losing = [-1, -1, 0.5, -1, 1, -1] * 6
        self.assertEqual(learning.edge_tilt(self._trades(winning))["factor"], 1.2)
        self.assertLess(learning.edge_tilt(self._trades(losing))["factor"], 1.0)


class TuningTests(unittest.TestCase):
    def _t(self, max_r, min_r, win, style="momentum", plan_file="2026-10-15-morning.json"):
        return {"status": "closed", "asset": "stock", "style": style, "entry_price": 100.0, "initial_stop": 95.0,
                "target": 115.0, "max_r": max_r, "min_r": min_r, "pnl_usd": 50 if win else -50,
                "r_result": 1.0 if win else -1.0, "plan_file": plan_file}

    def test_targets_and_stops_tune_from_real_paths(self):
        from desk import learning
        trades = [self._t(1.6, -0.2, True) for _ in range(10)] + [self._t(0.4, -1.0, False) for _ in range(8)]
        tune = learning.exit_tuning(trades)["momentum"]
        self.assertLess(tune["target_r"], 3.0)                 # planned 3R targets, but trades only ran ~1.6R
        self.assertLess(tune["stop_scale"], 1.0)                # winners barely dipped
        self.assertTrue(tune["advice"])

    def test_tuned_levels_applied_to_a_new_trade(self):
        from desk import learning
        with unittest.mock.patch.object(learning, "_load", return_value={"exit_tuning": {"momentum": {"target_r": 1.6, "stop_scale": 0.6}}}):
            stop, target, note = learning.tuned_levels("momentum", "long", 100.0, 95.0, 115.0)
        self.assertAlmostEqual(stop, 97.0)
        self.assertAlmostEqual(target, 104.8)
        self.assertIn("auto-tuned", note)

    def test_session_budget_favours_the_session_that_pays(self):
        from desk import learning
        trades = [self._t(1, 0, True, plan_file="2026-10-15-intraday-1053.json") for _ in range(12)] + \
                 [self._t(0, -1, False, plan_file="2026-10-15-morning.json") for _ in range(12)]
        b = learning.session_budget(trades)
        self.assertEqual(b["multipliers"], {"morning": 0.7, "intraday": 1.3})
