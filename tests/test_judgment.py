"""The trader's context the agents reason with, and the huddle's new guardrails."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from desk import alerts
from desk.research import context, team


def bars(n=300, step=0.0, wiggle=1.0, start=100.0):
    out, c = [], start
    for i in range(n):
        c = c + step
        out.append({"t": f"day{i}", "o": c, "h": c + wiggle, "l": c - wiggle, "c": c, "v": 1e6})
    return out


class ContextTests(unittest.TestCase):
    def test_price_behavior_and_reach_odds(self):
        quiet = bars()                          # ATR ~2, never moves more than 1 ATR from a close
        pb = context.price_behavior(quiet)
        self.assertEqual(pb["within_5_days"]["up"]["1.0_atr"], 0)
        self.assertGreater(pb["within_5_days"]["up"]["0.5_atr"], 90)
        far = context.reach_odds(quiet, True, 100, 104, "1-5 days")
        self.assertEqual(far["target_distance_atr"], 2.0)
        self.assertEqual(far["reached_pct"], 0)
        trend = bars(step=1.0)                  # rises 1 a day: 2 ATRs (~4) takes about 3 days
        self.assertEqual(context.reach_odds(trend, True, 399, 403, "1-5 days")["reached_pct"], 100)
        self.assertEqual(context.reach_odds(trend, True, 399, 403, "intraday")["reached_pct"], 0)
        self.assertIn("note", context.reach_odds(trend, True, 399, 403, "intraday"))

    def test_base_rates_and_confidence_check(self):
        trades = [{"status": "closed", "r_result": r, "style": "mean reversion", "asset": "stock", "confidence_pct": c}
                  for r, c in ((1.2, 58), (-1.0, 58), (-1.0, 62), (0.3, 45))]
        rates = context.base_rates(trades)
        self.assertEqual(rates["by_style"]["mean reversion"]["trades"], 4)
        self.assertEqual(rates["by_style"]["mean reversion"]["win_pct"], 50)
        self.assertEqual(rates["confidence_check"]["60%+"]["actually_won_pct"], 0)
        self.assertEqual(context.style_rate(rates, "Mean Reversion", "stock"), (50, 4))
        self.assertEqual(context.style_rate(rates, "breakout", "stock"), (50, 4))     # falls back to the asset class
        self.assertIn("small sample", rates["sample_note"])

    def test_confidence_is_pulled_toward_the_real_win_rate(self):
        d = {"decision": "long", "entry": 100, "stop": 96, "target": 106, "confidence_pct": 70,
             "base_win_pct": 25, "base_trades": 40, "evidence": [{"fact": "technicals.price", "value": 100}, {"fact": "technicals.rsi14", "value": 50}]}
        dz = {"technicals": {"price": 100, "rsi14": 50}, "asset_type": "stock"}
        check = team.fact_check(d, {"technicals.price": 100, "technicals.rsi14": 50}, dz)
        self.assertAlmostEqual(d["confidence_used_pct"], 34.0)     # (70*10 + 25*40) / 50
        self.assertLess(d["expected_r"], 0)
        self.assertFalse(check["passed"])
        d2 = {**d, "base_trades": 3}
        d2.pop("confidence_used_pct", None)
        team.fact_check(d2, {"technicals.price": 100, "technicals.rsi14": 50}, dz)
        self.assertNotIn("confidence_used_pct", d2)                # too few trades to overrule the analyst


class HuddleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(alerts, "HUDDLE_LOG", Path(self.tmp.name) / "h.jsonl")
        p.start()
        self.addCleanup(p.stop)
        for target in ("desk.activity.working", "desk.activity.done", "desk.trading.ledger.note"):
            q = mock.patch(target)
            q.start()
            self.addCleanup(q.stop)

    def _trade(self):
        return {"id": "t1", "symbol": "FMC", "asset": "stock", "direction": "long", "entry_price": 100.0, "initial_stop": 94.0,
                "stop": 94.0, "target": 112.0, "atr_pct": 6.0, "opened_at": (datetime.now() - timedelta(hours=3)).astimezone().isoformat(),
                "notes": []}

    def test_trigger_scales_with_normal_range(self):
        self.assertAlmostEqual(alerts._trigger_pct({"asset": "stock", "atr_pct": 12.0}), 7.2)
        self.assertEqual(alerts._trigger_pct({"asset": "stock", "atr_pct": 2.0}), 3.0)
        self.assertEqual(alerts._trigger_pct({"asset": "forex"}), 0.7)

    def test_grace_refuses_a_stop_inside_noise(self):
        t, eng = self._trade(), mock.Mock(trades=[])
        with mock.patch("desk.ai.ask", return_value={"action": "tighten", "new_stop": 101.0, "reason": "protect gains"}):
            msg = alerts._huddle(t, 102.0, 2.0, [], eng)
        eng._move_stop.assert_not_called()
        self.assertEqual(t["stop"], 94.0)
        self.assertIn("Grace refused", msg)
        logged = json.loads((Path(self.tmp.name) / "h.jsonl").read_text().splitlines()[0])
        self.assertEqual(logged["action"], "hold (stop refused)")

    def test_a_reasoned_stop_outside_noise_goes_through(self):
        t, eng = self._trade(), mock.Mock(trades=[])
        with mock.patch("desk.ai.ask", return_value={"action": "tighten", "new_stop": 97.0, "reason": "earnings guide cut"}):
            alerts._huddle(t, 102.0, 2.0, [], eng)
        eng._move_stop.assert_called_once_with(t, 97.0)

    def test_past_huddles_show_how_trades_ended(self):
        t, eng = self._trade(), mock.Mock(trades=[])
        with mock.patch("desk.ai.ask", return_value={"action": "hold", "reason": "noise"}):
            alerts._huddle(t, 102.0, 2.0, [], eng)
        closed = {**t, "status": "closed", "exit_reason": "target", "r_result": 2.0, "max_r": 2.1}
        past = alerts._past_huddles([closed])
        self.assertEqual(past[0]["decision"], "hold")
        self.assertEqual(past[0]["final_r"], 2.0)


if __name__ == "__main__":
    unittest.main()
