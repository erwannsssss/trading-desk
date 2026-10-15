import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from desk import compliance, config, firm, pnl, staff
from desk.risk import Book

NY = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=NY)


class FirmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(firm, "FILE", d / "firm.json"),
                        mock.patch.object(staff, "FILE", d / "staff.json"),
                        mock.patch.object(compliance, "FILE", d / "compliance.json"),
                        mock.patch.object(config, "DATA_DIR", d),
                        mock.patch("desk.firm.now", lambda: T0),
                        mock.patch("desk.compliance.now", lambda: T0)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    # ---- payroll and bonuses
    def test_payroll_is_two_percent_a_year_split_by_level(self):
        firm.accrue_payroll(10_000, T0)                       # first call only sets the clock
        charged = firm.accrue_payroll(10_000, T0 + timedelta(days=1))
        self.assertAlmostEqual(charged, 10_000 * 0.02 / 365, places=6)
        data = firm.load()
        by = data["payroll"]["by_seat"]
        self.assertNotIn("partner", by)                       # Erwann takes no salary
        self.assertNotIn("crypto", by)                        # locked desks aren't on payroll
        self.assertAlmostEqual(by["pm"] / by["technical"], 2.2, places=3)   # Principal vs Associate
        self.assertAlmostEqual(sum(by.values()), charged, places=4)

    def test_payroll_never_back_charges_more_than_two_days(self):
        firm.accrue_payroll(10_000, T0)
        charged = firm.accrue_payroll(10_000, T0 + timedelta(days=30))
        self.assertAlmostEqual(charged, 10_000 * 0.02 * 2 / 365, places=6)

    def test_bonus_pool_is_20pct_above_high_water_mark(self):
        scores = {"technical": {"right": 14, "total": 20, "pct": 70}, "bull": {"right": 6, "total": 20, "pct": 30}}
        rec = firm.pay_bonuses("2026-10", 11_000, scores)
        self.assertAlmostEqual(rec["pool"], 200.0, delta=0.5)  # 20% of $1,000
        self.assertNotIn("bull", rec["by_seat"])              # under 45% gets nothing
        self.assertGreater(rec["by_seat"]["technical"]["amount"], rec["by_seat"]["fundamental"]["amount"])
        self.assertAlmostEqual(firm.load()["bonuses"]["hwm"], 10_800, places=0)
        again = firm.pay_bonuses("2026-11", 10_700, scores)   # below the high-water mark: no bonus
        self.assertEqual(again["pool"], 0)

    def test_opex_reduces_equity(self):
        b = Book(alpaca_equity=10_000, oanda_nav=10_000, baseline_alpaca=10_000, baseline_oanda=10_000, opex=12.5)
        self.assertAlmostEqual(b.equity, 9_987.5)

    # ---- levels
    def test_promote_demote_and_fire_ladder(self):
        scores = {"technical": {"right": 13, "total": 20}, "bull": {"right": 8, "total": 20}, "bear": {"right": 10, "total": 20}}
        changes = {c["seat"]: c for c in firm.review_levels(scores)}
        self.assertEqual(changes["technical"]["to"], "Senior")
        self.assertEqual(changes["bull"]["to"], "Junior")
        self.assertNotIn("bear", changes)
        self.assertEqual(firm.review_levels(scores), [])      # 4-week cooldown
        with mock.patch("desk.firm.now", lambda: T0 + timedelta(days=30)):
            later = {c["seat"]: c for c in firm.review_levels(scores)}
        self.assertEqual(later["bull"]["kind"], "fire")       # a Junior still under 45%
        self.assertEqual(later["technical"]["to"], "Principal")

    # ---- unlocks
    def test_unlocks_at_milestones_and_stay_unlocked(self):
        self.assertEqual(firm.check_unlocks(10_400), [])
        self.assertFalse(firm.seat_unlocked("crypto"))
        new = firm.check_unlocks(11_100)
        self.assertEqual([u[0] for u in new], ["crypto_desk", "event_desk"])
        self.assertTrue(firm.seat_unlocked("crypto"))
        self.assertEqual(firm.check_unlocks(9_000), [])       # falling back doesn't re-lock
        self.assertTrue(firm.unlocked("crypto_desk"))
        chart = {p["id"]: p for p in staff.org_chart()}
        self.assertFalse(chart["crypto"]["locked"])
        self.assertTrue(chart["fx"]["locked"])
        self.assertEqual(firm.loan_apr(), 0.08)
        firm.check_unlocks(15_000)
        self.assertEqual(firm.loan_apr(), 0.065)
        self.assertEqual(firm.intern_slots(), 5)

    # ---- compliance
    def _trade(self, **kw):
        t = {"id": "t1", "symbol": "AAPL", "asset": "stock", "direction": "long", "status": "open", "entry_plan": 100.0,
             "entry_price": 100.2, "initial_stop": 95.0, "stop": 95.0, "target": 110.0, "filled_qty": 50, "horizon": "1-5 days",
             "opened_at": "2026-10-15T11:00:00-04:00"}
        t.update(kw)
        return t

    def _book(self, **kw):
        base = dict(alpaca_equity=10_000, oanda_nav=10_000, baseline_alpaca=10_000, baseline_oanda=10_000)
        base.update(kw)
        return Book(**base)

    def test_clean_trade_has_no_findings(self):
        with mock.patch("desk.activity.event", lambda *a, **k: None):
            self.assertEqual(compliance.audit([self._trade()], self._book()), [])

    def test_compliance_catches_breaches_once(self):
        bad = self._trade(entry_price=101.5, filled_qty=200, stop=94.0, opened_at="2026-10-15T09:31:00-04:00")
        with mock.patch("desk.activity.event", lambda *a, **k: None):
            found = compliance.audit([bad], self._book())
            again = compliance.audit([bad], self._book())
        rules = {f["rule"]: f for f in found}
        self.assertIn("Fill too far from plan", rules)
        self.assertIn("Risk per trade over cap", rules)       # 6.5 x 200 = $1,300 > $500
        self.assertIn("Stop moved further away", rules)
        self.assertIn("Traded in the blackout window", rules)
        self.assertEqual(rules["Risk per trade over cap"]["seat"], "sizer")
        self.assertEqual(again, [])                           # each finding is reported once

    def test_big_loss_flagged_on_close(self):
        t = self._trade(status="closed", r_result=-2.4, exit_reason="stop", closed_at="2026-10-15T12:00:00-04:00")
        with mock.patch("desk.activity.event", lambda *a, **k: None):
            found = compliance.audit([t], self._book())
        self.assertEqual([f["severity"] for f in found if f["rule"] == "Loss bigger than planned"], ["major"])

    # ---- income statement
    def test_statement_splits_trading_interest_and_staff_costs(self):
        rows = [{"t": "2026-10-14T16:00-04:00", "equity": 10_050.0, "spy": 1, "fin": 0.0, "opex": 0.5},
                {"t": "2026-10-15T16:00-04:00", "equity": 10_020.0, "spy": 1, "fin": 2.0, "opex": 1.0}]
        (config.DATA_DIR / "equity.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        trades = [{"status": "closed", "pnl_usd": 40.0, "asset": "crypto", "closed_at": "2026-10-15T10:00:00-04:00"}]
        s = pnl.statement(trades, {"payroll": {}, "bonuses": {"paid": []}})
        d2 = s["days"][1]
        self.assertAlmostEqual(d2["net"], -30.0)
        self.assertAlmostEqual(d2["interest"], 2.0)
        self.assertAlmostEqual(d2["payroll"], 0.5)
        self.assertAlmostEqual(d2["trading"], -27.5)           # -30 + 2 + 0.5
        self.assertAlmostEqual(d2["unrealized_change"], -67.5)  # -27.5 - 40 realized
        self.assertAlmostEqual(s["total"]["net"], 20.0)
        self.assertTrue(s["total"]["profitable"])


if __name__ == "__main__":
    unittest.main()
