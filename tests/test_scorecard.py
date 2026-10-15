import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from desk import scorecard


def graded(move, scores=None, bull="long", pm="pass", status="pass", atr=2.0):
    return {"date": "2099-01-01", "symbol": "X", "asset": "stock", "status": status, "move_pct": move, "atr_pct": atr,
            "scores": scores or {}, "bull": bull, "pm": pm}


class ScorecardTests(unittest.TestCase):
    def run_with(self, graded_items, macro=None, trades=(), curve=None):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.json"
            f.write_text(json.dumps({"graded": {str(i): g for i, g in enumerate(graded_items)}, "macro": macro or {}}))
            with mock.patch.object(scorecard, "FILE", f):
                return scorecard.seat_scores(list(trades), curve)

    def test_specialists_bull_bear(self):
        s = self.run_with([graded(+3, {"technical": 2}), graded(-2, {"technical": 1}), graded(+1, {"technical": 3})])
        self.assertEqual((s["technical"]["right"], s["technical"]["total"]), (2, 3))
        self.assertEqual(s["bull"]["right"], 2)
        self.assertEqual(s["bear"]["right"], 1)

    def test_pm_pass_and_trade(self):
        s = self.run_with([graded(+5, pm="long", status="approved"), graded(+0.5, pm="pass"), graded(+4, pm="pass")])
        self.assertEqual((s["pm"]["right"], s["pm"]["total"]), (2, 3))

    def test_factcheck_saves(self):
        s = self.run_with([graded(-3, pm="long", status="blocked_by_fact_check")] * 3)
        self.assertEqual(s["factcheck"]["pct"], 100)

    def test_small_sample_hidden(self):
        s = self.run_with([graded(+3, {"technical": 2})])
        self.assertIsNone(s["technical"]["pct"])

    def test_trading_desk(self):
        trades = [{"created_at": "2099-01-01", "status": "closed", "pnl_usd": -50, "risk_usd": 50, "entry_order_id": "a", "entry_price": 1},
                  {"created_at": "2099-01-01", "status": "closed", "pnl_usd": 30, "max_r": 1.5, "entry_order_id": "b", "entry_price": 1},
                  {"created_at": "2099-01-01", "status": "cancelled", "entry_order_id": "c"}]
        s = self.run_with([], trades=trades)
        self.assertEqual((s["execution"]["right"], s["execution"]["total"]), (2, 3))
        self.assertEqual(s["sizer"]["right"], 1)
        self.assertEqual(s["positions"]["right"], 1)

    def test_exit_price_waits_for_three_bars(self):
        created = datetime.fromisoformat("2026-10-12T08:00:00-04:00")
        bars = [{"t": f"2026-10-{d}T04:00:00Z", "c": float(d)} for d in (12, 13, 14)]
        with mock.patch.object(scorecard, "now", return_value=datetime.fromisoformat("2026-10-16T10:00:00-04:00")):
            self.assertEqual(scorecard._exit_price(bars, created, "stock"), 14.0)
        with mock.patch.object(scorecard, "now", return_value=datetime.fromisoformat("2026-10-14T10:00:00-04:00")):
            self.assertIsNone(scorecard._exit_price(bars, created, "stock"))


if __name__ == "__main__":
    unittest.main()
