import unittest
from BNB import calculate_coin_investment_size, hard_stop_triggered

class TestMicroStopAndAdaptiveSizing(unittest.TestCase):
    def test_hard_stop_at_0_05(self):
        # Losses below 0.05 must NOT trigger
        self.assertFalse(hard_stop_triggered(-0.005, 25.0, -0.05))
        self.assertFalse(hard_stop_triggered(-0.010, 25.0, -0.05))
        self.assertFalse(hard_stop_triggered(-0.049, 25.0, -0.05))
        self.assertFalse(hard_stop_triggered(0.001, 25.0, -0.05))

        # Losses reaching or exceeding 0.05 MUST trigger
        self.assertTrue(hard_stop_triggered(-0.050, 25.0, -0.05))
        self.assertTrue(hard_stop_triggered(-0.051, 25.0, -0.05))
        self.assertTrue(hard_stop_triggered(-0.100, 25.0, -0.05))

    def test_coin_adaptive_sizing(self):
        lot_5 = {"min_notional": 5.0}
        lot_10 = {"min_notional": 10.0}

        # 1. Meme / High Volatility coin (e.g. PEPE, DOGE) -> small size ($5-$7)
        pepe_size = calculate_coin_investment_size(
            symbol="PEPEUSDT",
            lot=lot_5,
            profile={"volatility_1m": 0.45, "predicted_market": "Breakouts"},
            max_investment=25.0,
            usdt_free=97.0
        )
        self.assertGreaterEqual(pepe_size, 5.0)
        self.assertLessEqual(pepe_size, 7.0)

        # 2. Medium volatility trending coin (e.g. BCH, LINK) -> moderate size (~$10)
        trend_size = calculate_coin_investment_size(
            symbol="BCHUSDT",
            lot=lot_5,
            profile={"volatility_1m": 0.25, "predicted_market": "Strong trends"},
            max_investment=25.0,
            usdt_free=97.0
        )
        self.assertEqual(trend_size, 10.0)

        # 3. Liquid large cap (e.g. SOL) -> larger size (up to max 25.0)
        sol_size = calculate_coin_investment_size(
            symbol="SOLUSDT",
            lot=lot_5,
            profile={"volatility_1m": 0.12, "predicted_market": "Liquid pairs"},
            max_investment=25.0,
            usdt_free=97.0
        )
        self.assertGreaterEqual(sol_size, 15.0)
        self.assertLessEqual(sol_size, 25.0)

        # 4. Respects max_investment cap
        capped_size = calculate_coin_investment_size(
            symbol="SOLUSDT",
            lot=lot_5,
            profile={"volatility_1m": 0.12, "predicted_market": "Liquid pairs"},
            max_investment=12.0,
            usdt_free=97.0
        )
        self.assertEqual(capped_size, 12.0)

        # 5. Respects high min_notional coin (e.g. pairs where min_notional is 10.0)
        min_notional_size = calculate_coin_investment_size(
            symbol="DOGEUSDT",
            lot=lot_10,
            profile={"volatility_1m": 0.50, "predicted_market": "Breakouts"},
            max_investment=25.0,
            usdt_free=97.0
        )
        self.assertGreaterEqual(min_notional_size, 10.0)

if __name__ == "__main__":
    unittest.main()
