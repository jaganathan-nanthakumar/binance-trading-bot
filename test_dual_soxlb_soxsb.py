"""
test_dual_soxlb_soxsb.py — Unit tests and simulation for DUAL_SOXLB_SOXSB Strategy.
"""

import os
import json
import unittest
from unittest.mock import MagicMock, patch

import dual_soxlb_soxsb


class TestDualSoxlbSoxsb(unittest.TestCase):

    def setUp(self):
        # Use an isolated test state file
        self.original_state_file = dual_soxlb_soxsb.STATE_FILE
        dual_soxlb_soxsb.STATE_FILE = "test_dual_state.json"
        if os.path.exists("test_dual_state.json"):
            os.remove("test_dual_state.json")

    def tearDown(self):
        if os.path.exists("test_dual_state.json"):
            os.remove("test_dual_state.json")
        dual_soxlb_soxsb.STATE_FILE = self.original_state_file

    def test_fee_covered_net_pnl_calculation(self):
        """
        Verify that compute_live_net_pnl accounts for both entry fee and exit fee.
        Cost: 12.50 USDT
        Buy fee: 12.50 * 0.00075 = 0.009375 USDT
        If price is identical (gross = 12.50 USDT):
        Sell fee: 12.50 * 0.00075 = 0.009375 USDT
        Total fees: ~0.01875 USDT
        Net PnL should be negative (-0.01875 USDT) even if gross is 0.
        """
        qty = 0.222
        entry_cost = 12.50
        buy_fee = 12.50 * 0.00075
        cur_bid = 12.50 / qty  # identical price

        net_pnl, gross, est_sell_fee = dual_soxlb_soxsb.compute_live_net_pnl(
            cur_bid=cur_bid,
            qty=qty,
            entry_cost=entry_cost,
            buy_fee_usdt=buy_fee,
            fee_rate=0.00075,
        )
        self.assertAlmostEqual(gross, 12.50, places=4)
        self.assertAlmostEqual(est_sell_fee, 0.009375, places=4)
        self.assertLess(net_pnl, 0.0, "Should be negative due to fees")

        # Now test when bid price rises so net PnL is +0.02 USDT after all fees
        # Required gross = entry_cost + buy_fee + target / (1 - fee_rate)
        # = (12.50 + 0.009375 + 0.02) / 0.99925 = 12.53878
        higher_bid = 12.54 / qty
        net_pnl_high, gross_high, _ = dual_soxlb_soxsb.compute_live_net_pnl(
            cur_bid=higher_bid,
            qty=qty,
            entry_cost=entry_cost,
            buy_fee_usdt=buy_fee,
            fee_rate=0.00075,
        )
        self.assertGreater(net_pnl_high, 0.019, "Net PnL must exceed target after all fees")

    def test_state_persistence(self):
        """Verify state saving, loading, updating, and clearing."""
        instance_id = 999
        leg_data = {
            "symbol": "SOXSBUSDT",
            "cycle_id": 1,
            "qty": 0.22,
            "entry_cost": 12.48,
            "entry_price": 56.72,
            "buy_fee_usdt": 0.0093,
            "buy_fee_bnb": 0.0,
            "started_at": "test_time",
        }
        dual_soxlb_soxsb.update_leg_state(instance_id, "SOXSBUSDT", leg_data)

        loaded = dual_soxlb_soxsb.get_instance_state(instance_id)
        self.assertIn("SOXSBUSDT", loaded["legs"])
        self.assertEqual(loaded["legs"]["SOXSBUSDT"]["qty"], 0.22)

        dual_soxlb_soxsb.record_harvest_win(instance_id, "SOXSBUSDT", 0.025)
        loaded2 = dual_soxlb_soxsb.get_instance_state(instance_id)
        self.assertEqual(loaded2["total_harvests"], 1)
        self.assertAlmostEqual(loaded2["total_profit_usdt"], 0.025, places=4)

        dual_soxlb_soxsb.clear_instance_state(instance_id)
        loaded3 = dual_soxlb_soxsb.get_instance_state(instance_id)
        self.assertEqual(len(loaded3["legs"]), 0)

    def test_simulated_dual_cycle_workflow(self):
        """
        Simulate a full dual cycle:
        1. Both legs bought for $12.50 each ($25 total).
        2. Market moves up: SOXLB reaches profit target, SOXSB goes down.
        3. SOXLB is sold at profit.
        4. SOXLB is re-bought for $12.50.
        5. SOXSB is NOT sold (accumulating).
        """
        mock_client = MagicMock()
        # Mock balances
        mock_client.get_asset_balance.side_effect = lambda asset: {
            "free": "50.0" if asset == "USDT" else ("0.222" if asset == "SOXSB" else "0.226"),
            "locked": "0.0"
        }
        # Mock lot info
        mock_lot = {
            "min_notional": 5.0,
            "tick_size": 0.01,
            "step": 0.001,
            "min_qty": 0.001
        }

        # Mock order creations
        buy_orders = []
        sell_orders = []

        def mock_create_order(**kwargs):
            sym = kwargs["symbol"]
            side = kwargs["side"]
            qty = float(kwargs["quantity"])
            px = 55.0 if "SOXLB" in sym else 56.0
            order_id = len(buy_orders) + len(sell_orders) + 100
            order_resp = {
                "orderId": order_id,
                "executedQty": str(qty),
                "cummulativeQuoteQty": str(qty * px),
                "fills": [
                    {"commission": "0.009", "commissionAsset": "USDT"}
                ]
            }
            if side == "BUY":
                buy_orders.append((sym, qty, order_resp))
            else:
                sell_orders.append((sym, qty, order_resp))
            return order_resp

        mock_client.create_order.side_effect = mock_create_order

        # Test buy_leg for both
        with patch("BNB.get_lot_info", return_value=mock_lot), \
             patch("BNB.insert_cycle", return_value=101), \
             patch("BNB.insert_trade", return_value=201), \
             patch("BNB.update_instance"), \
             patch("BNB.log_event"):

            # Step 1: Buy SOXSB ($12.50)
            mock_client.get_orderbook_ticker.return_value = {"askPrice": "56.00", "bidPrice": "55.95"}
            ok_s, leg_s = dual_soxlb_soxsb.buy_leg(mock_client, 1, "SOXSBUSDT", 12.50, "TestBot")
            self.assertTrue(ok_s)
            self.assertIsNotNone(leg_s)

            # Step 2: Buy SOXLB ($12.50)
            mock_client.get_orderbook_ticker.return_value = {"askPrice": "55.00", "bidPrice": "54.95"}
            ok_l, leg_l = dual_soxlb_soxsb.buy_leg(mock_client, 1, "SOXLBUSDT", 12.50, "TestBot")
            self.assertTrue(ok_l)
            self.assertIsNotNone(leg_l)

            # Both legs bought
            self.assertEqual(len(buy_orders), 2)
            self.assertEqual(buy_orders[0][0], "SOXSBUSDT")
            self.assertEqual(buy_orders[1][0], "SOXLBUSDT")

            # Step 3: Market moves: SOXLB bid surges to 56.50 (+2.7%), SOXSB bid drops to 54.50
            # SOXLB should hit profit target
            pnl_l, _, _ = dual_soxlb_soxsb.compute_live_net_pnl(56.50, leg_l["qty"], leg_l["entry_cost"], leg_l["buy_fee_usdt"])
            pnl_s, _, _ = dual_soxlb_soxsb.compute_live_net_pnl(54.50, leg_s["qty"], leg_s["entry_cost"], leg_s["buy_fee_usdt"])

            self.assertGreater(pnl_l, 0.05, "SOXLB should be strongly profitable")
            self.assertLess(pnl_s, 0.0, "SOXSB should be negative (accumulating)")

            # Step 4: Sell winning leg (SOXLB)
            with patch("BNB.settle_pnl", return_value={"gross_pnl": 0.30, "fees_usdt": 0.018, "net_pnl": 0.282}), \
                 patch("BNB.close_cycle"):
                ok_sell, realized_pnl, _ = dual_soxlb_soxsb.sell_leg(
                    mock_client, 1, "SOXLBUSDT", leg_l, 56.50, "TestBot"
                )
                self.assertTrue(ok_sell)
                self.assertAlmostEqual(realized_pnl, 0.282, places=3)
                self.assertEqual(len(sell_orders), 1)
                self.assertEqual(sell_orders[0][0], "SOXLBUSDT")

            # Step 5: Immediately re-buy SOXLB for $12.50
            mock_client.get_orderbook_ticker.return_value = {"askPrice": "56.55", "bidPrice": "56.50"}
            ok_rebuy, new_leg_l = dual_soxlb_soxsb.buy_leg(mock_client, 1, "SOXLBUSDT", 12.50, "TestBot")
            self.assertTrue(ok_rebuy)
            self.assertEqual(len(buy_orders), 3)
            self.assertEqual(buy_orders[2][0], "SOXLBUSDT")

            # Verify: SOXSB was NEVER sold!
            self.assertEqual(len(sell_orders), 1, "SOXSB must never be sold at a loss")

    def test_multi_bot_concurrency_and_liquidation_isolation(self):
        """
        Verify that multiple bots with DUAL_SOXLB_SOXSB / DUAL_HARVEST_STRATEGY:
        1. When Bot 2 starts while Bot 1 is running, Bot 2 does NOT adopt Bot 1's pairs.
           sync_leg_position returns None for Bot 2, forcing Bot 2 to buy its own pair.
        2. When Bot 1 is liquidated, it sells ONLY Bot 1's tracked quantity.
           Bot 2's tokens in the wallet remain untouched.
        3. Liquidating a bot with 0 open position does not sell other bots' wallet coins.
        """
        mock_client = MagicMock()
        mock_lot = {
            "min_notional": 5.0,
            "tick_size": 0.01,
            "step": 0.001,
            "min_qty": 0.001
        }

        # Wallet has 0.44 SOXSB and 0.44 SOXLB (0.22 for Bot 1 + 0.22 for Bot 2)
        mock_client.get_asset_balance.side_effect = lambda asset: {
            "free": "0.44" if asset in ("SOXSB", "SOXLB") else "100.0",
            "locked": "0.0"
        }
        mock_client.get_orderbook_ticker.return_value = {"askPrice": "55.00", "bidPrice": "55.00"}

        sell_requests = []
        def mock_sell_order(**kwargs):
            sell_requests.append(kwargs)
            return {
                "orderId": 999,
                "executedQty": kwargs["quantity"],
                "cummulativeQuoteQty": str(float(kwargs["quantity"]) * 55.0),
                "fills": [{"commission": "0.01", "commissionAsset": "USDT"}]
            }
        mock_client.create_order.side_effect = mock_sell_order

        # Scenario 1: Bot 2 is fresh (no cycles in DB for inst=2)
        # Mock DB cycles: Bot 1 (id=1) has open cycles for SOXSB and SOXLB with qty=0.22
        def mock_execute(sql, params=()):
            mock_cursor = MagicMock()
            if "WHERE instance_id = ?" in sql:
                inst_id = params[0]
                sym = params[1] if len(params) > 1 else None
                if inst_id == 1:
                    mock_cursor.fetchone.return_value = {
                        "id": 10, "symbol": sym, "entry_cost": 12.10, "qty": 0.22,
                        "fee_bnb": 0.0, "fee_usdt": 0.01, "started_at": "now"
                    }
                else:
                    # Bot 2 has NO open cycles!
                    mock_cursor.fetchone.return_value = None
            elif "WHERE instance_id != ?" in sql:
                # Sum of other bots' open cycles: for Bot 2, other bots (Bot 1) hold 0.22
                inst_id = params[0]
                if inst_id == 2:
                    mock_cursor.fetchone.return_value = (0.22,)
                else:
                    mock_cursor.fetchone.return_value = (0.0,)
            elif "SELECT id, symbol, entry_cost FROM cycles WHERE instance_id = ?" in sql:
                inst_id = params[0]
                if inst_id == 1:
                    mock_cursor.fetchall.return_value = [
                        {"id": 10, "symbol": "SOXSBUSDT", "entry_cost": 12.10},
                        {"id": 11, "symbol": "SOXLBUSDT", "entry_cost": 12.10},
                    ]
                else:
                    mock_cursor.fetchall.return_value = []
            return mock_cursor

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = mock_execute
        mock_conn.__enter__.return_value = mock_conn

        with patch("BNB.get_conn", return_value=mock_conn), \
             patch("BNB.get_lot_info", return_value=mock_lot), \
             patch("BNB.close_cycle"), \
             patch("BNB.insert_trade"), \
             patch("BNB.log_event"):

            # 1. Bot 2 checks sync_leg_position: MUST return None so it buys its own leg!
            leg_b2_s = dual_soxlb_soxsb.sync_leg_position(mock_client, 2, "SOXSBUSDT", "Bot2")
            leg_b2_l = dual_soxlb_soxsb.sync_leg_position(mock_client, 2, "SOXLBUSDT", "Bot2")
            self.assertIsNone(leg_b2_s, "Bot 2 must NOT adopt Bot 1's SOXSB position")
            self.assertIsNone(leg_b2_l, "Bot 2 must NOT adopt Bot 1's SOXLB position")

            # 2. Liquidate Bot 1: MUST sell ONLY 0.22 of each symbol, NOT 0.44!
            res_liq_1 = dual_soxlb_soxsb.liquidate_dual_holdings(mock_client, 1, "Bot1")
            self.assertTrue(res_liq_1["liquidated"])
            self.assertEqual(len(sell_requests), 2)
            for req in sell_requests:
                self.assertEqual(float(req["quantity"]), 0.22, "Must sell strictly Bot 1's allocation of 0.22, not whole wallet (0.44)!")

            # 3. Liquidate Bot 2 when Bot 2 has no open position: MUST NOT place any sell orders!
            sell_requests.clear()
            res_liq_2 = dual_soxlb_soxsb.liquidate_dual_holdings(mock_client, 2, "Bot2")
            self.assertFalse(res_liq_2["liquidated"])
            self.assertEqual(len(sell_requests), 0, "Bot 2 with no holdings must NOT sell coins belonging to other bots!")


if __name__ == "__main__":
    unittest.main()
