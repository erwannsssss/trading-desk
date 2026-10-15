import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desk import interns, staff


class InternTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(interns, "FILE", d / "interns.json"),
                        mock.patch.object(interns, "QUEUE", d / "queue"),
                        mock.patch.object(interns, "_floor", lambda *a, **k: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_open_validates_sides_and_leverage(self):
        i = {"id": "i9", "positions": [], "cash": 150.0}
        self.assertIsNone(interns._open(i, {"symbol": "X", "asset": "stock", "direction": "long", "stop": 105, "target": 120}, 100, 150))
        self.assertIsNone(interns._open(i, {"symbol": "BTC/USD", "asset": "crypto", "direction": "short", "stop": 105, "target": 90}, 100, 150))
        p = interns._open(i, {"symbol": "X", "asset": "stock", "direction": "long", "stop": 95, "target": 115, "size_pct": 500}, 100, 150)
        self.assertAlmostEqual(p["size_usd"], 300)  # capped at 2x leverage

    def test_hire_at_500_and_let_go_at_30(self):
        data = {"next_id": 3, "graduates": [], "interns": [
            {"id": "i1", "name": "A", "persona": "p", "status": "intern", "cash": 150, "closed": [], "peak": 150,
             "positions": [{"symbol": "X", "asset": "stock", "direction": "long", "entry": 100, "units": 3.6,
                            "stop": 90, "target": 300, "opened": "2099-01-01T00:00:00-05:00"}]},
            {"id": "i2", "name": "B", "persona": "p", "status": "intern", "cash": 150, "closed": [], "peak": 150,
             "positions": [{"symbol": "Y", "asset": "stock", "direction": "long", "entry": 100, "units": 2.5,
                            "stop": 40, "target": 150, "opened": "2099-01-01T00:00:00-05:00"}]}]}
        interns.FILE.write_text(json.dumps(data))
        with mock.patch.object(interns, "_prices", lambda positions: {"X": 200.0, "Y": 50.0}):
            news = interns.update()
        by = {i["name"]: i for i in interns._load()["interns"]}
        self.assertEqual(by["A"]["status"], "junior")        # 150 + 360 = 510
        self.assertEqual(by["B"]["status"], "let_go")        # 150 - 125 = 25
        self.assertEqual(sum(i["status"] == "intern" for i in by.values()), 3)  # slots refilled
        self.assertTrue(any("hired" in n for n in news))


class StaffTests(unittest.TestCase):
    def test_fire_and_replace(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(staff, "FILE", Path(d) / "staff.json"):
            old, new = staff.fire_and_replace("technical", "38% hit rate", "Only trades with the trend.")
            self.assertEqual(old, "Priya Natarajan")
            self.assertEqual(staff.name("technical"), new)
            self.assertEqual(staff.approaches()["technical"], "Only trades with the trend.")
            self.assertEqual(staff.load()["alumni"][0]["name"], "Priya Natarajan")
            chart = {p["id"]: p for p in staff.org_chart()}
            self.assertIsNone(chart["partner"]["reports_to"])
            self.assertEqual(chart["pm"]["reports_to"], "cio")


if __name__ == "__main__":
    unittest.main()


class AwardsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from desk import awards
        self.p = mock.patch.object(awards, "FILE", Path(self.tmp.name) / "awards.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_rounds_go_to_whoever_the_price_proved_right(self):
        from desk import awards
        graded = {"graded": {"a": {"date": "2026-10-13", "symbol": "A", "bull": "long", "move_pct": 2.0},
                             "b": {"date": "2026-10-13", "symbol": "B", "bull": "long", "move_pct": -1.0},
                             "c": {"date": "2026-10-14", "symbol": "C", "bull": "short", "move_pct": -3.0},
                             "d": {"date": "2026-10-14", "symbol": "D", "bull": None, "move_pct": 5.0}}, "macro": {}}
        with mock.patch("desk.scorecard._load", return_value=graded):
            rs = awards.rounds()
            t = awards.season_trophy("2026-10")
        self.assertEqual([r["winner"] for r in rs], ["bull", "bear", "bull"])
        self.assertEqual((t["bull"], t["bear"], t["winner"]), (2, 1, "bull"))
        self.assertEqual(awards.load()["trophies"]["bull"], 1)

    def test_employee_of_the_month_needs_10_calls(self):
        from desk import awards
        scores = {"technical": {"right": 9, "total": 9}, "bear": {"right": 8, "total": 12}, "bull": {"right": 5, "total": 12}}
        with mock.patch("desk.ai.ask", return_value={"speech": "Great month.", "thanks": "Thanks!"}):
            rec = awards.employee_of_the_month("2026-10", "October 2026", scores)
        self.assertEqual(rec["seat"], "bear")        # 9/9 is better but under 10 calls
        self.assertEqual(rec["pct"], 67)
