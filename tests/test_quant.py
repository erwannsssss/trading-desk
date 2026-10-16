import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from desk.research import kronos, quant
from desk.research import dossier as dossiers
from desk.research import team

NY = ZoneInfo("America/New_York")


def bars_from(closes, spread=0.01):
    t0 = datetime(2024, 1, 1)
    return [{"t": (t0 + timedelta(days=i)).isoformat() + "Z", "o": c, "h": c * (1 + spread), "l": c * (1 - spread), "c": c, "v": 1e6}
            for i, c in enumerate(closes)]


def trend(n=400, start=100.0, daily=0.004, wobble=0.04):
    return [start * (1 + daily) ** i * (1 + wobble * math.sin(i / 3)) for i in range(n)]


class SignalTests(unittest.TestCase):
    def test_momentum_long_in_a_clean_uptrend(self):
        b = bars_from(trend())
        sigs = [quant.signal("momentum", quant._feat(b, i)) for i in range(len(b) - 30, len(b))]
        self.assertIn("long", sigs)
        self.assertNotIn("short", sigs)
        self.assertGreater(quant._feat(b, len(b) - 1)["r60"], 15)

    def test_reversion_long_on_a_sharp_dip_in_an_uptrend(self):
        closes = trend(daily=0.002, wobble=0.002)
        closes[-3:] = [closes[-4] * 0.97, closes[-4] * 0.94, closes[-4] * 0.91]
        f = quant._feat(bars_from(closes), len(closes) - 1)
        self.assertLessEqual(f["z20"], -2)
        self.assertEqual(quant.signal("reversion", f), "long")

    def test_rule_record_replays_history(self):
        rec = quant.rule_record(bars_from(trend(600)), "momentum")
        self.assertGreater(rec["signals"], 5)
        self.assertGreater(rec["avg_r"], 0)          # a steady uptrend pays the momentum rule
        self.assertLessEqual(rec["worst_r"], 2)


class KronosMathTests(unittest.TestCase):
    UP = {"high": [101, 103, 106, 109, 110], "low": [99.5, 100, 102, 104, 105], "close": [100.5, 102, 105, 108, 109]}
    DOWN = {"high": [100.5, 99, 98, 97, 96], "low": [97, 95, 94, 93, 92], "close": [98, 96, 95, 94, 93]}
    FLAT = {"high": [101] * 5, "low": [99] * 5, "close": [100, 100, 100, 101, 102]}

    def test_target_before_stop(self):
        r = quant.kronos_r([self.UP, self.UP, self.DOWN, self.FLAT], True, 100, 96, 108)
        self.assertEqual((r["target_first_pct"], r["stop_first_pct"]), (50, 25))
        self.assertAlmostEqual(r["model_expected_r"], round((2 + 2 - 1 + 0.5) / 4, 2))
        with mock.patch.object(quant, "r_scale", return_value=0.3):    # the model's own estimate is shrunk to its backtest
            self.assertAlmostEqual(quant.kronos_r([self.UP], True, 100, 96, 108)["expected_r"], 0.6)
        short = quant.kronos_r([self.DOWN], False, 100, 104, 93)
        self.assertEqual(short["target_first_pct"], 100)

    def test_summary(self):
        s = kronos.summarize([self.UP, self.UP, self.UP, self.DOWN], 100.0)
        self.assertEqual(s["prob_up_pct"], 75)
        self.assertEqual(s["direction"], "up")
        self.assertIsNone(kronos.public(None))
        self.assertNotIn("_paths", kronos.public({**s, "_paths": []}))

    def test_kronos_score_and_size(self):
        self.assertGreater(quant._kronos_score({"expected_move_pct": 3, "range_width_pct": 8}), 0)
        self.assertLess(quant._kronos_score({"expected_move_pct": -3, "range_width_pct": 8}), 0)
        from desk.research import planner
        self.assertEqual(planner.kronos_size(None, 1.0), 1.0)
        self.assertGreater(planner.kronos_size({"expected_r": 0.5}, 1.0), 1.0)
        self.assertLess(planner.kronos_size({"expected_r": -1.0}, 1.0), 1.0)
        self.assertEqual(planner.kronos_size({"expected_r": -1.0}, 0.0), 1.0)    # untrusted model changes nothing


class IdeaTests(unittest.TestCase):
    def _cand(self, asset="stock"):
        t = {"price": 100.0, "atr14": 2.0, "atr_pct": 2.0, "rsi14": 60.0, "ret_20d": 8.0, "ret_60d": 25.0, "setups": []}
        return {"symbol": "XYZ", "asset": asset, "venue": "alpaca", "t": t, "bars": []}

    def test_idea_passes_the_fact_checker(self):
        rec = {"signals": 14, "win_pct": 57, "avg_r": 0.42, "best_r": 2.0, "worst_r": -1.0}
        kr = {"expected_r": 0.35, "target_first_pct": 45, "stop_first_pct": 30, "paths": 24}
        kz = {"expected_move_pct": 2.5, "range_width_pct": 9, "_paths": []}
        i = quant._idea(self._cand(), "momentum", "long", kz, kr, rec, 0.7)
        d = i["decision"]
        self.assertEqual((d["stop"], d["target"]), (97.0, 106.0))
        self.assertGreater(d["quant_edge_r"], 0.2)
        self.assertGreaterEqual(d["conviction"], 6)
        dz = {"symbol": "XYZ", "asset_type": "stock", "technicals": self._cand()["t"], "quant_backtest": rec, "kronos_odds": kr}
        check = team.fact_check(d, dossiers.facts(dz), dz)
        self.assertTrue(check["passed"], check["problems"])
        self.assertAlmostEqual(d["expected_r"], d["quant_edge_r"], delta=0.03)   # confidence matches the edge
        self.assertIn("quant_momentum", i["specialists"])

    def test_bearish_stock_uses_puts_and_kronos_disagreement_lowers_conviction(self):
        rec = {"signals": 6, "win_pct": 50, "avg_r": 0.2, "best_r": 2.0, "worst_r": -1.0}
        bad = {"expected_r": -0.4, "target_first_pct": 10, "stop_first_pct": 60, "paths": 24}
        i = quant._idea(self._cand(), "momentum", "short", None, bad, rec, 0.8)
        self.assertEqual(i["decision"]["expression"], "put_options")
        self.assertLess(i["decision"]["quant_edge_r"], 0)
        self.assertLessEqual(i["decision"]["conviction"], 5)

    def test_pace_counts_only_today(self):
        with mock.patch("desk.research.quant.now", lambda: datetime(2026, 10, 16, 12, 45, tzinfo=NY)):
            p = quant._pace({"day": "2026-10-16", "trades_today": 4})
            self.assertEqual(p["placed_today"], 4)
            self.assertGreater(p["expected_by_now"], 10)
            self.assertGreater(p["behind_by"], 5)
            self.assertEqual(quant._pace({"day": "2026-10-15", "trades_today": 12})["placed_today"], 0)

    def test_kronos_weight_follows_its_backtest(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "k.json"
            with mock.patch.object(quant, "CALIBRATION", f):
                self.assertEqual(quant.kronos_weight(), 0.5)            # no backtest yet
                f.write_text(json.dumps({"by_lookback": {"64": {"all": {"direction_hit_pct": 48, "avg_r_trading_its_call": -0.1}}}}))
                self.assertEqual(quant.kronos_weight(), 0.2)
                f.write_text(json.dumps({"by_lookback": {"64": {"all": {"direction_hit_pct": 56, "avg_r_trading_its_call": 0.15}}}}))
                self.assertAlmostEqual(quant.kronos_weight(), 0.62, places=2)
                f.write_text(json.dumps({"by_lookback": {"64": {"all": {"direction_hit_pct": 70, "avg_r_trading_its_call": 0.6}}}}))
                self.assertEqual(quant.kronos_weight(), 0.8)                # capped until there's a live record


if __name__ == "__main__":
    unittest.main()
