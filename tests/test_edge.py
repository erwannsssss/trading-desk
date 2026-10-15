import random
import unittest

from desk import edge


class DistributionTests(unittest.TestCase):
    def test_t_distribution(self):
        self.assertAlmostEqual(edge.t_sf(2.0, 10), 0.0367, places=3)
        self.assertAlmostEqual(edge.t_sf(-2.0, 10), 1 - 0.0367, places=3)
        self.assertAlmostEqual(edge.t_ppf_95(10), 1.812, places=2)
        self.assertAlmostEqual(edge.t_ppf_95(1000), 1.646, places=2)

    def test_binomial_and_wilson(self):
        self.assertAlmostEqual(edge.binom_sf(8, 10), 0.0547, places=3)
        lo, hi = edge.wilson(50, 100)
        self.assertAlmostEqual(lo, 0.404, places=2)
        self.assertAlmostEqual(hi, 0.596, places=2)


class EdgeTests(unittest.TestCase):
    def test_real_edge_is_detected(self):
        rng = random.Random(1)
        rs = [2.0 if rng.random() < 0.5 else -1.0 for _ in range(150)]  # +0.5R expectancy
        t = edge.trade_tests(rs)
        self.assertLess(t["p_value"], 0.01)
        self.assertGreater(t["prob_positive"], 0.97)
        self.assertEqual(edge.verdict(t)["level"], "edge")

    def test_coin_flip_is_not_called_skill(self):
        rng = random.Random(2)
        rs = [1.0 if rng.random() < 0.5 else -1.0 for _ in range(60)]
        v = edge.verdict(edge.trade_tests(rs))["level"]
        self.assertIn(v, ("no_edge", "promising"))
        self.assertNotEqual(v, "edge")

    def test_too_early(self):
        self.assertEqual(edge.verdict(edge.trade_tests([3.0, 2.0, 1.5]))["level"], "too_early")

    def test_market_alpha(self):
        rng = random.Random(3)
        curve, eq, spy = [], 10000.0, 500.0
        for d in range(120):
            m = rng.gauss(0.0004, 0.01)
            eq *= 1 + 0.5 * m + 0.002 + rng.gauss(0, 0.002)  # beta 0.5 plus 0.2%/day alpha
            spy *= 1 + m
            curve.append({"t": f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}T16:00", "equity": eq, "spy": spy})
        mt = edge.market_tests(curve)
        self.assertAlmostEqual(mt["beta"], 0.5, delta=0.1)
        self.assertGreater(mt["alpha_t"], 3)
        self.assertGreater(mt["psr"], 0.95)


if __name__ == "__main__":
    unittest.main()
