import unittest
from datetime import date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

from desk import bank
from desk.trading import guards

NY = ZoneInfo("America/New_York")


def tr(symbol, asset, direction, style="mean reversion", status="open"):
    return {"symbol": symbol, "asset": asset, "direction": direction, "style": style, "status": status}


class GuardTests(unittest.TestCase):
    def test_same_currency_limit(self):
        held = [tr("USD_JPY", "forex", "long"), tr("USD_CAD", "forex", "long")]
        self.assertIn("USD", guards.same_bet(held, "forex", "USD_CHF", "long", "macro"))
        self.assertIn("USD", guards.same_bet(held, "forex", "EUR_USD", "short", "macro"))   # short EUR/USD = long USD
        self.assertIsNone(guards.same_bet(held, "forex", "EUR_USD", "long", "macro"))      # the opposite bet is fine
        self.assertIsNone(guards.same_bet(held, "forex", "EUR_GBP", "long", "macro"))

    def test_crypto_and_stock_style_limits(self):
        cryptos = [tr(s, "crypto", "long") for s in ("BTC/USD", "ETH/USD", "SOL/USD")]
        self.assertIsNotNone(guards.same_bet(cryptos, "crypto", "DOGE/USD", "long", "momentum"))
        stocks = [tr(s, "stock", "long") for s in ("FMC", "ONDS", "RKLX")]
        self.assertIsNotNone(guards.same_bet(stocks, "stock", "WULF", "long", "mean reversion"))
        self.assertIsNone(guards.same_bet(stocks, "stock", "WULF", "long", "breakout"))
        closed = [tr(s, "stock", "long", status="closed") for s in ("A", "B", "C")]
        self.assertIsNone(guards.same_bet(closed, "stock", "WULF", "long", "mean reversion"))

    def test_same_industry_limit(self):
        held = [dict(tr(s, "stock", "long", style=st), industry="Semiconductors & Chip Equipment")
                for s, st in (("NVDA", "momentum"), ("AMD", "breakout"))]
        self.assertIn("Semiconductors", guards.same_bet(held, "stock", "AVGO", "long", "trend pullback", "Semiconductors & Chip Equipment"))
        self.assertIsNone(guards.same_bet(held, "stock", "AVGO", "short", "trend pullback", "Semiconductors & Chip Equipment"))
        self.assertIsNone(guards.same_bet(held, "stock", "JPM", "long", "trend pullback", "Banks"))

    def test_earnings_rules(self):
        today = date(2026, 10, 15)
        self.assertIsNotNone(guards.earnings_conflict("2026-10-19", "1-5 days", "momentum", today))
        self.assertIsNone(guards.earnings_conflict("2026-10-19", "1-5 days", "earnings play", today))
        self.assertIsNone(guards.earnings_conflict("2026-11-30", "1-5 days", "momentum", today))
        now = datetime(2026, 10, 15, 15, 30, tzinfo=NY)   # Thursday afternoon
        t = tr("AAPL", "stock", "long", style="momentum")
        self.assertIsNotNone(guards.must_exit_before_earnings(t, {"AAPL": {"date": "2026-10-16", "hour": "bmo"}}, now))
        self.assertIsNotNone(guards.must_exit_before_earnings(t, {"AAPL": {"date": "2026-10-15", "hour": "amc"}}, now))
        self.assertIsNone(guards.must_exit_before_earnings(t, {"AAPL": {"date": "2026-10-16", "hour": "amc"}}, now))
        fri = datetime(2026, 10, 16, 15, 30, tzinfo=NY)
        self.assertIsNotNone(guards.must_exit_before_earnings(t, {"AAPL": {"date": "2026-10-19", "hour": "bmo"}}, fri))

    def test_risk_off_needs_conviction_5(self):
        self.assertIsNotNone(guards.risk_off_block("risk-off/defensive", "stock", "long", 4))
        self.assertIsNone(guards.risk_off_block("risk-off/defensive", "stock", "long", 5))
        self.assertIsNone(guards.risk_off_block("risk-off/defensive", "forex", "long", 4))
        self.assertIsNone(guards.risk_off_block("strong uptrend", "stock", "long", 4))

    def test_forex_borrows_only_its_margin(self):
        self.assertEqual(bank.loan_needed(25_796 * 0.02, 10_000, 3_000), 0.0)
        self.assertEqual(bank.loan_needed(5_000, 10_000, 8_000), 3_000.0)
        trades = [{"symbol": "USD_CAD", "asset": "forex", "status": "open", "loan_usd": 25_796.0, "notional_usd": 25_796.0}]
        with mock.patch("desk.brokers.oanda.margin_rate", return_value=0.02):
            notes = bank.reprice_forex_loans(trades)
        self.assertAlmostEqual(trades[0]["loan_usd"], 515.92)
        self.assertEqual(len(notes), 1)
        with mock.patch("desk.brokers.oanda.margin_rate", return_value=0.02):
            self.assertEqual(bank.reprice_forex_loans(trades), [])   # only once


class PairTests(unittest.TestCase):
    def test_pairs_pick_the_widest_split_inside_one_industry(self):
        from desk.research import pairs
        def a(sym, r):
            return {"symbol": sym, "asset": "stock", "technicals": {"ret_20d": r}}
        stocks = [a("NVDA", 25), a("AMD", -4), a("AVGO", 8), a("JPM", 6), a("BAC", -1)]
        ind = {"NVDA": "Semis", "AMD": "Semis", "AVGO": "Semis", "JPM": "Banks", "BAC": "Banks"}
        with mock.patch("desk.research.extra.sector", side_effect=lambda s: {"industry": ind[s]}):
            got = pairs.candidates(stocks, skip=set())
        self.assertEqual([(l["symbol"], g["symbol"], i, gap) for l, g, i, gap in got], [("NVDA", "AMD", "Semis", 29)])

    def test_both_legs_or_neither(self):
        from desk.research import pairs
        tech = {"price": 100.0, "atr_pct": 2.0, "ret_20d": 10.0, "rsi14": 60.0}
        dz = {"symbol": "X", "asset_type": "stock", "technicals": tech}
        good = {"entry": 100, "stop": 96, "target": 108, "conviction": 6, "evidence": [{"fact": "technicals.ret_20d", "value": 10}, {"fact": "technicals.rsi14", "value": 60}]}
        bad = {**good, "evidence": [{"fact": "technicals.made_up", "value": 1}]}
        lead, lag = {"symbol": "A", "technicals": tech}, {"symbol": "B", "technicals": tech}
        ok = pairs.ideas_from({"decision": {"decision": "pair", "long": good, "short": {**good, "stop": 104, "target": 92}},
                               "lead_dossier": dz, "lag_dossier": dz}, lead, lag, "Semis", 20, "2026-10-16")
        self.assertEqual([i["status"] for i in ok], ["approved", "approved"])
        self.assertEqual(ok[0]["pair_id"], ok[1]["pair_id"])
        self.assertEqual(ok[1]["decision"]["expression"], "put_options")
        half = pairs.ideas_from({"decision": {"decision": "pair", "long": good, "short": {**bad, "stop": 104, "target": 92}},
                                 "lead_dossier": dz, "lag_dossier": dz}, lead, lag, "Semis", 20, "2026-10-16")
        self.assertTrue(all(i["status"] == "blocked_by_fact_check" for i in half))


class SpreadTests(unittest.TestCase):
    def _chain(self):
        def snap(bid, ask, delta):
            return {"latestQuote": {"bp": bid, "ap": ask}, "greeks": {"delta": delta}}
        return {"AAPL261030C00345000": snap(6.2, 6.4, 0.5), "AAPL261030C00350000": snap(4.0, 4.2, 0.4),
                "AAPL261030C00357500": snap(1.7, 1.9, 0.25), "AAPL261030C00340000": snap(9.0, 9.3, 0.62)}

    def test_pick_spread_sells_the_strike_at_the_target(self):
        from desk.trading import options
        with mock.patch.object(options, "_chain", return_value=self._chain()):
            s = options.pick_spread("AAPL", "call", 344.0, 358.0, "1-5 days", max_cost=600)
        self.assertEqual((s["strike"], s["short_strike"]), (345.0, 357.5))
        self.assertAlmostEqual(s["mid"], 4.5)
        self.assertLess(s["mid"], s["single_cost"])
        self.assertGreaterEqual(s["width"] - s["mid"], s["mid"])

    def test_pick_spread_respects_budget(self):
        from desk.trading import options
        with mock.patch.object(options, "_chain", return_value=self._chain()):
            cheaper = options.pick_spread("AAPL", "call", 344.0, 358.0, "1-5 days", max_cost=300)
            none = options.pick_spread("AAPL", "call", 344.0, 358.0, "1-5 days", max_cost=100)
        self.assertLessEqual(cheaper["limit"] * 100, 300)     # falls back to a tighter spread that fits
        self.assertIsNone(none)

    def test_credit_spread_only_when_options_are_expensive(self):
        from desk.trading import options
        def snap(bid, ask, delta, iv):
            return {"latestQuote": {"bp": bid, "ap": ask}, "greeks": {"delta": delta}, "impliedVolatility": iv}
        chain = {"XYZ261113P00095000": snap(2.4, 2.6, 0.3, 0.6), "XYZ261113P00090000": snap(0.9, 1.0, 0.18, 0.62),
                 "XYZ261113P00100000": snap(4.0, 4.3, 0.5, 0.58)}
        with mock.patch.object(options, "_chain", return_value=chain):
            rich = options.pick_credit_spread("XYZ", True, 101.0, 95.0, atr_pct=2.0, max_loss=600)   # realized ~25%
            fair = options.pick_credit_spread("XYZ", True, 101.0, 95.0, atr_pct=6.0, max_loss=600)   # realized ~76%
        self.assertEqual((rich["strike"], rich["hedge_strike"]), (95.0, 90.0))
        self.assertGreaterEqual(rich["limit"] / rich["width"], 0.2)
        self.assertAlmostEqual(rich["max_loss"], rich["width"] - rich["limit"])
        self.assertIsNone(fair)

    def test_net_price_of_two_leg_orders(self):
        from desk.trading import options
        opened = {"legs": [{"side": "buy", "filled_avg_price": "6.30"}, {"side": "sell", "filled_avg_price": "1.80"}]}
        closed = {"legs": [{"side": "sell", "filled_avg_price": "9.10"}, {"side": "buy", "filled_avg_price": "3.00"}]}
        self.assertAlmostEqual(options.mleg_net_price(opened), 4.5)
        self.assertAlmostEqual(options.mleg_net_price(closed), 6.1)


if __name__ == "__main__":
    unittest.main()
