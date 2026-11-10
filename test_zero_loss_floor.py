import unittest
from BNB import hard_stop_triggered, format_price

class TestZeroLossFloor(unittest.TestCase):
    def test_hard_stop_always_false_for_negative(self):
        # Even with -0.0050, -0.0085, -0.0500, elapsed 45s, 90s, 300s, it must NEVER trigger
        for elapsed in [0.0, 30.0, 45.0, 68.7, 90.0, 120.0, 300.0, 600.0]:
            for pnl in [-0.0001, -0.0040, -0.0069, -0.0085, -0.0237, -0.10, -1.0]:
                self.assertFalse(
                    hard_stop_triggered(pnl, 25.0, -0.05, elapsed),
                    f"hard_stop_triggered returned True for pnl={pnl}, elapsed={elapsed}"
                )

    def test_break_even_ceiling_calculation(self):
        # Verify that CEILING rounding guarantees net profit >= +0.0010 USDT
        cost = 10.0
        buy_fee = 0.0075
        est_fee_rate = 0.00075 # BNB discounted maker/taker fee
        target_net = 0.0010
        qty = 0.1
        tick_sz = 0.01

        req_exit = (cost + buy_fee + target_net) / (1.0 - est_fee_rate)
        limit_px = float(format_price(req_exit / qty, tick_sz, rounding="CEILING"))

        # Actual proceeds at limit_px
        gross_exit = qty * limit_px
        sell_fee = gross_exit * est_fee_rate
        actual_net = gross_exit - cost - buy_fee - sell_fee

        self.assertGreaterEqual(actual_net, target_net, f"Actual net {actual_net} < target {target_net}")

if __name__ == "__main__":
    unittest.main()
