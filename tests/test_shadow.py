import unittest
from datetime import datetime
from unittest import mock

from desk import edge, shadow


def plan(ideas, session="morning"):
    return {"_file": "p.json", "session": session, "created_at": "2026-10-01T08:00:00-04:00", "ideas": ideas}


class ShadowTests(unittest.TestCase):
    def test_picks(self):
        ideas = [{"symbol": "A", "asset": "stock", "price_at_research": 100, "atr_pct": 2, "status": "approved",
                  "setups": ["breakdown below 20-day low"], "bull": {"direction": "short"},
                  "decision": {"decision": "short", "stop": 104, "target": 92, "conviction": 9}},
                 {"symbol": "B", "asset": "stock", "price_at_research": 50, "status": "pass", "bull": {"direction": "long"},
                  "decision": {"decision": "pass"}}]
        got = {(s, k.split("|")[1]) for s, k, *_ in shadow.picks_for(plan(ideas))}
        self.assertIn(("desk", "A"), got)
        self.assertIn(("high_conviction", "A"), got)
        self.assertIn(("follow_bull", "B"), got)
        self.assertIn(("scanner_top", "A"), got)
        self.assertNotIn(("scanner_top", "B"), got)
        self.assertEqual(sum(s == "coin_flip" for s, _ in got), 2)
        top = next(p for p in shadow.picks_for(plan(ideas)) if p[0] == "scanner_top")
        self.assertEqual(top[3], "short")  # breakdown -> short

    def test_simulate_stop_target_time(self):
        created = datetime.fromisoformat("2026-10-01T08:00:00-04:00")
        bars = [{"t": f"2026-10-{d:02d}T04:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100} for d in range(1, 15)]
        with mock.patch.object(shadow, "now", return_value=datetime.fromisoformat("2026-10-20T10:00:00-04:00")):
            self.assertEqual(shadow.simulate(bars, created, "stock", 100, "long", 95, 110)["how"], "time")
            hit = [dict(b) for b in bars]; hit[2]["h"] = 111
            self.assertEqual(shadow.simulate(hit, created, "stock", 100, "long", 95, 110)["r"], 2.0)
            hit[1]["l"] = 94
            self.assertEqual(shadow.simulate(hit, created, "stock", 100, "long", 95, 110)["r"], -1.0)

    def test_welch(self):
        better = [1.0, 2.0, 1.5, 0.5, 2.5, 1.0, 1.8]
        worse = [-1.0, 0.0, -0.5, 0.5, -1.0, 0.2, -0.3]
        w = edge.welch(better, worse)
        self.assertGreater(w["diff_r"], 1)
        self.assertLess(w["p_value"], 0.01)


if __name__ == "__main__":
    unittest.main()
