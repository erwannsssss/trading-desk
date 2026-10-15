import unittest

from desk.research.team import fact_check, size

DOSSIER = {"asset_type": "stock", "technicals": {"price": 100.0}}
FACTS = {"technicals.rsi14": 62.5, "fundamentals.pe_ratio": 24.0, "technicals.ret_20d": 8.1}


def decision(**kw):
    d = {"decision": "long", "entry": 100, "stop": 95, "target": 110, "conviction": 7,
         "premortem": {"failure_more_likely": False},
         "evidence": [{"fact": "technicals.rsi14", "value": 62.5}, {"fact": "fundamentals.pe_ratio", "value": 24}]}
    d.update(kw)
    return d


class FactCheckTests(unittest.TestCase):
    def test_clean_plan_passes(self):
        r = fact_check(decision(), FACTS, DOSSIER)
        self.assertTrue(r["passed"], r["problems"])

    def test_made_up_number_caught(self):
        d = decision(evidence=[{"fact": "technicals.rsi14", "value": 45}, {"fact": "fundamentals.pe_ratio", "value": 24}])
        self.assertFalse(fact_check(d, FACTS, DOSSIER)["passed"])

    def test_made_up_field_caught(self):
        d = decision(evidence=[{"fact": "fundamentals.moat_score", "value": 9}, {"fact": "fundamentals.pe_ratio", "value": 24}])
        self.assertFalse(fact_check(d, FACTS, DOSSIER)["passed"])

    def test_bad_reward_to_risk(self):
        self.assertFalse(fact_check(decision(target=103), FACTS, DOSSIER)["passed"])

    def test_wrong_side_stop(self):
        self.assertFalse(fact_check(decision(stop=105), FACTS, DOSSIER)["passed"])
        short = decision(decision="short", stop=105, target=90)
        self.assertTrue(fact_check(short, FACTS, DOSSIER)["passed"])

    def test_entry_too_far(self):
        self.assertFalse(fact_check(decision(entry=120, stop=115, target=130), FACTS, DOSSIER)["passed"])

    def test_premortem_veto(self):
        self.assertFalse(fact_check(decision(premortem={"failure_more_likely": True}), FACTS, DOSSIER)["passed"])

    def test_pass_needs_no_prices(self):
        self.assertTrue(fact_check({"decision": "pass", "evidence": []}, FACTS, DOSSIER)["passed"])


class SizingTests(unittest.TestCase):
    def test_conviction_scales_risk(self):
        low = size(decision(conviction=4, stop=90), 10_000, "stock")
        high = size(decision(conviction=8, stop=90), 10_000, "stock")
        self.assertEqual(low["risk_usd"], 75)
        self.assertEqual(high["risk_usd"], 250)

    def test_position_cap(self):
        s = size(decision(conviction=10, stop=99), 10_000, "stock")  # tiny stop -> huge size, capped at 25%
        self.assertLessEqual(s["notional_usd"], 2_500)

    def test_forex_sizing_in_dollars(self):
        d = decision(entry=150.0, stop=148.0, target=154.0, conviction=7)  # USD/JPY, 2 yen stop
        s = size(d, 10_000, "forex", quote_to_usd=1 / 150, unit_usd=1.0)
        self.assertAlmostEqual(s["risk_usd"], 250, delta=1)
        self.assertAlmostEqual(s["units"], 18_750, delta=1)

    def test_forex_cap(self):
        d = decision(entry=150.0, stop=149.0, target=152.0, conviction=7)  # tight stop -> capped at 2x equity
        self.assertLessEqual(size(d, 10_000, "forex", quote_to_usd=1 / 150, unit_usd=1.0)["notional_usd"], 20_000)


if __name__ == "__main__":
    unittest.main()
