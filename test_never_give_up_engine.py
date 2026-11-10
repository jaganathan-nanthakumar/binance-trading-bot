import unittest
from BNB import create_instance, update_instance, get_instance, format_price, init_db

class TestNeverGiveUpEngine(unittest.TestCase):
    def setUp(self):
        init_db()
        self.iid = create_instance(name="TEST_ALGO_BOT", investment_usdt=25.0)

    def test_database_strategy_updates(self):
        # Verify update_instance updates strategy column cleanly
        update_instance(self.iid, strategy="MOMENTUM_MAKER")
        inst = get_instance(self.iid)
        self.assertIsNotNone(inst)
        self.assertEqual(inst.get("strategy"), "MOMENTUM_MAKER")

        update_instance(self.iid, strategy="MEAN_REVERSION")
        inst = get_instance(self.iid)
        self.assertEqual(inst.get("strategy"), "MEAN_REVERSION")

        update_instance(self.iid, strategy="MICRO_GRID_REPAIR")
        inst = get_instance(self.iid)
        self.assertEqual(inst.get("strategy"), "MICRO_GRID_REPAIR")

        update_instance(self.iid, strategy="PASSIVE_OSCILLATOR")
        inst = get_instance(self.iid)
        self.assertEqual(inst.get("strategy"), "PASSIVE_OSCILLATOR")

    def test_floor_profit_guarantee(self):
        # Ensure that Phase 3 and Phase 4 floor calculation yields net profit >= +0.0010 USDT
        cost = 8.0
        buy_fee = 0.006
        est_fee_rate = 0.00075
        qty = 0.08
        tick_sz = 0.01

        floor_exit = (cost + buy_fee + 0.0010) / (1.0 - est_fee_rate)
        floor_limit_price = float(format_price(floor_exit / qty, tick_sz, rounding="CEILING"))

        # Actual gross and net
        gross = qty * floor_limit_price
        sell_fee = gross * est_fee_rate
        net = gross - cost - buy_fee - sell_fee

        self.assertGreaterEqual(net, 0.0010)

if __name__ == "__main__":
    unittest.main()
