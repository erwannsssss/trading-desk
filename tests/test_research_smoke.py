"""End-to-end smoke test: a whole research session with every outside service faked.

Catches crashes in the session's wiring (like the name clash that broke the Oct 15 2:50pm huddle) before they reach
the real desk. The AI, brokers and data sources are replaced with simple stand-ins.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desk.risk import Book
from desk.research import planner


def tech(price):
    return {"price": price, "atr14": price * 0.02, "atr_pct": 2.0, "rsi14": 45.0, "ret_1d": 1.0, "ret_5d": 2.0,
            "ret_20d": 5.0, "trend": "up", "setups": ["trend pullback"], "interest": 3}


def cand(sym, asset, venue="alpaca", price=100.0):
    return {"symbol": sym, "asset": asset, "venue": venue, "technicals": tech(price), "why_scanned": ["test"]}


def decision(price, side="long"):
    sign = 1 if side == "long" else -1
    return {"decision": side, "entry": price, "stop": price - sign * 4, "target": price + sign * 8, "conviction": 6,
            "confidence_pct": 55, "horizon": "1-5 days", "style": "trend pullback", "bucket": "core", "expression": "shares",
            "reason": "test", "thesis": "test thesis", "premortem": {"failure_more_likely": False},
            "evidence": [{"fact": "technicals.price", "value": price}, {"fact": "technicals.rsi14", "value": 45.0}]}


UP = {"high": [101, 103, 106, 109, 110], "low": [99.5, 100, 102, 104, 105], "close": [100.5, 102, 105, 108, 109]}
DOWN = {"high": [100.5, 99, 98, 97, 96], "low": [97, 95, 94, 93, 92], "close": [98, 96, 95, 94, 93]}
KRONOS = {"AAA": {"horizon_days": 5, "expected_move_pct": 4.0, "median_move_pct": 5.0, "prob_up_pct": 75, "range_low_10pct": 93,
                  "range_high_90pct": 109, "range_width_pct": 16, "direction": "up", "signal_strength": 0.6, "paths": 4,
                  "_paths": [UP, UP, UP, DOWN]}}
NOTES = {k: {"score": 2, "points": ["x"]} for k in ("technical", "fundamental", "news_sentiment", "smart_money")}


class ResearchSessionSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        book = Book(alpaca_equity=10_000, oanda_nav=10_000, baseline_alpaca=10_000, baseline_oanda=10_000)
        stocks = [cand("AAA", "stock"), cand("BBB", "stock", price=50.0)]
        patches = [
            mock.patch.object(planner, "PLANS_DIR", Path(self.tmp.name) / "plans"),
            mock.patch.object(planner, "load", return_value={"dry_run": True}),
            mock.patch.object(planner, "snapshot", return_value=(book, {"alpaca_positions": [], "oanda_trades": []})),
            mock.patch.object(planner, "floor"), mock.patch.object(planner, "telegram"),
            mock.patch.object(planner.scanner, "scan_stocks", return_value=(stocks, {"etf_readings": {}, "scanned": 2, "analyzed": stocks})),
            mock.patch.object(planner.scanner, "scan_crypto", return_value=([cand("BTC/USD", "crypto")], {"crypto_readings": {}})),
            mock.patch.object(planner.scanner, "scan_forex", return_value=([], {"forex_readings": {}})),
            mock.patch.object(planner.datasources, "macro_snapshot", return_value={}),
            mock.patch.object(planner.datasources, "economic_calendar", return_value=[]),
            mock.patch.object(planner.marketdata, "news", return_value=[]),
            mock.patch.object(planner, "_calendar_effects", return_value={}),
            mock.patch.object(planner.extra_data, "cot", return_value={}),
            mock.patch.object(planner.extra_data, "crypto_sentiment", return_value={}),
            mock.patch.object(planner.smartmoney, "fund_holdings", return_value={}),
            mock.patch.object(planner, "team_record", return_value=None),
            mock.patch.object(planner.dossiers, "build", side_effect=lambda c, f, light=False: {
                "symbol": c["symbol"], "asset_type": c["asset"], "technicals": dict(c["technicals"])}),
            mock.patch.object(planner.team, "macro_view", return_value={"regime": "strong uptrend", "risk_appetite": 60, "summary": "ok"}),
            mock.patch.object(planner.team, "quick_desk", side_effect=lambda d, *a, **k: {
                "specialists": NOTES, "bull": {"direction": "long", "case": "up"}, "bear": {"case": "down"},
                "decision": decision(d["technicals"]["price"])}),
            mock.patch.object(planner.team, "specialists", return_value=NOTES),
            mock.patch.object(planner.team, "desk_notes", return_value={}),
            mock.patch.object(planner.team, "bull", return_value={"direction": "long", "case": "up"}),
            mock.patch.object(planner.team, "bear", return_value={"case": "down"}),
            mock.patch.object(planner.team, "portfolio_manager", side_effect=lambda d, *a, **k: decision(d["technicals"]["price"])),
            mock.patch.object(planner.team, "second_opinion", return_value={"score": 2, "verdict": "agree", "reason": "fine", "size_multiplier": 1.15}),
            mock.patch("desk.interns.make_picks", return_value=[]),
            mock.patch("desk.research.pairs.candidates", return_value=[]),
            mock.patch.object(planner.extra_data, "vol_indexes", return_value={"vix": 16}),
            mock.patch.object(planner.extra_data, "implied_move", return_value=None),
            mock.patch.object(planner, "_kronos_for", return_value=(KRONOS, 0.6)),
        ]
        for p in patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.tmp.cleanup)

    def _run(self, session):
        plan = planner.run(session)
        saved = list((Path(self.tmp.name) / "plans").glob("*.json"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0].read_text())["session"], session)
        return plan

    def test_intraday_session_runs_end_to_end(self):
        plan = self._run("intraday")
        self.assertTrue(plan["ideas"])
        self.assertTrue(all(i["status"] == "approved" for i in plan["ideas"]), [i.get("error") for i in plan["ideas"]])
        self.assertTrue(all(i["second_opinion"]["verdict"] == "agree" for i in plan["ideas"]))
        aaa = next(i for i in plan["ideas"] if i["symbol"] == "AAA")
        self.assertEqual(aaa["kronos_odds"]["target_first_pct"], 75)     # 3 of 4 futures reach 108 before 96
        self.assertIn("kronos", aaa["specialists"])
        self.assertGreater(aaa["specialists"]["kronos"]["score"], 0)

    def test_morning_session_runs_end_to_end(self):
        plan = self._run("morning")
        self.assertTrue(plan["ideas"])
        self.assertFalse([i for i in plan["ideas"] if i["status"] == "error"], [i.get("error") for i in plan["ideas"]])


if __name__ == "__main__":
    unittest.main()
