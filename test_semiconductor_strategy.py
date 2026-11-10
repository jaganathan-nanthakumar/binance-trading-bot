"""
Unit tests for Revised Semiconductor 5-Minute Predictive Scoring Strategy.
"""

import sys
import os
import time
import unittest
from unittest.mock import MagicMock, patch

# Ensure workspace is in path
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

import semiconductor_dual_engine
import semiconductor_flip_flop
import hybrid_strategy
import BNB


class TestSemiconductor5MPrediction(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        # Mock orderbook tickers
        self.mock_client.get_orderbook_ticker.side_effect = lambda symbol: {
            "symbol": symbol,
            "bidPrice": "68.50" if "SOXL" in symbol else "57.20",
            "askPrice": "68.60" if "SOXL" in symbol else "57.30",
        }
        # Mock 1m klines: [open_time, open, high, low, close, volume, close_time, quote_vol, trades, taker_buy_base, taker_buy_quote, ignore]
        mock_kline = [
            1793687400000, "68.00", "69.00", "67.80", "68.50", "200.0",
            1793687459999, "13700.0", 50, "120.0", "8220.0", "0"
        ]
        self.mock_client.get_klines.return_value = [mock_kline] * 30
        self.mock_client.get_asset_balance.return_value = {"asset": "USDT", "free": "500.0", "locked": "0.0"}

    def test_predict_semiconductor_5m_structure(self):
        """Test that predict_semiconductor_5m returns all expected keys and valid ranges."""
        pred = semiconductor_dual_engine.predict_semiconductor_5m(client=self.mock_client, force_refresh=True)

        self.assertIn("total_score", pred)
        self.assertIn("bias", pred)
        self.assertIn("regime", pred)
        self.assertIn("action", pred)
        self.assertIn("viable", pred)
        self.assertIn("scores", pred)
        self.assertIn("details", pred)

        # Verify all 7 scoring components exist
        expected_scores = [
            "SMH_1m_VWAP",
            "NVDA_1m_VWAP_Green",
            "AVGO_AMD_both_green",
            "NQ_futures_5m_green",
            "CVD_positive_vol_gt_avg",
            "SOX_5m_EMA9_gt_EMA21",
            "No_major_news_Taiwan_TSM_NVDA",
        ]
        for s_name in expected_scores:
            self.assertIn(s_name, pred["scores"], f"Missing score component: {s_name}")
            val = pred["scores"][s_name]
            self.assertIn(val, [-1, 0, 1], f"Score {s_name}={val} not in [-1, 0, 1]")

        # Verify total score is sum of individual scores
        self.assertEqual(pred["total_score"], sum(pred["scores"].values()))
        self.assertTrue(-7 <= pred["total_score"] <= 7)

    def test_scoring_thresholds(self):
        """Test that total scores map correctly to Bullish (+3 to +7), Bearish (-3 to -7), Chop (-2 to +2)."""
        with patch.object(semiconductor_dual_engine, "predict_semiconductor_5m") as mock_pred:
            # Case 1: +5 Bullish -> Long SOXLB
            mock_pred.return_value = {
                "total_score": 5,
                "bias": "BULLISH",
                "regime": "BULL_TREND",
                "action": "LONG_SOXLB",
                "selected_symbol": "SOXLBUSDT",
                "viable": True,
                "reason": "Bullish Semiconductor Bias (+5/+7) -> Trade SOXLBUSDT",
                "scores": {"SMH_1m_VWAP": 1, "NVDA_1m_VWAP_Green": 1, "AVGO_AMD_both_green": 1, "NQ_futures_5m_green": 1, "CVD_positive_vol_gt_avg": 0, "SOX_5m_EMA9_gt_EMA21": 0, "No_major_news_Taiwan_TSM_NVDA": 1},
                "details": {},
                "target_profit_pct": 1.25,
                "stop_loss_pct": -0.60,
            }
            res = semiconductor_dual_engine.evaluate_semiconductor_regime(self.mock_client)
            self.assertTrue(res["viable"])
            self.assertEqual(res["selected_symbol"], "SOXLBUSDT")
            self.assertEqual(res["bias"], "BULLISH")

            # Case 2: -4 Bearish -> Long SOXSB
            mock_pred.return_value = {
                "total_score": -4,
                "bias": "BEARISH",
                "regime": "BEAR_TREND",
                "action": "LONG_SOXSB",
                "selected_symbol": "SOXSBUSDT",
                "viable": True,
                "reason": "Bearish Semiconductor Bias (-4/-7) -> Trade SOXSBUSDT",
                "scores": {},
                "details": {},
                "target_profit_pct": 1.25,
                "stop_loss_pct": -0.60,
            }
            res = semiconductor_dual_engine.evaluate_semiconductor_regime(self.mock_client)
            self.assertTrue(res["viable"])
            self.assertEqual(res["selected_symbol"], "SOXSBUSDT")
            self.assertEqual(res["bias"], "BEARISH")

            # Case 3: 0 Chop -> Skip (100% USDT)
            mock_pred.return_value = {
                "total_score": 0,
                "bias": "CHOP",
                "regime": "CHOP_SKIP",
                "action": "SKIP_100PCT_USDT",
                "selected_symbol": None,
                "viable": False,
                "reason": "Chop detected (Score: 0 in [-2, +2]). Skipping trade.",
                "scores": {},
                "details": {},
                "target_profit_pct": 1.25,
                "stop_loss_pct": -0.60,
            }
            res = semiconductor_dual_engine.evaluate_semiconductor_regime(self.mock_client)
            self.assertFalse(res["viable"])
            self.assertIsNone(res["selected_symbol"])
            self.assertEqual(res["regime"], "CHOP_SKIP")

    def test_log_scores_formatting(self):
        """Test that log_scores prints and logs all 7 scores without crashing."""
        pred = semiconductor_dual_engine.predict_semiconductor_5m(client=self.mock_client)
        semiconductor_dual_engine.log_scores(pred, bot_name="TestBot", client=self.mock_client)

    def test_score_detail_payload_and_ui_rendering(self):
        """Test that build_score_detail_payload contains all 7 criteria and BNB.render_log_detail executes cleanly."""
        import json
        from unittest.mock import MagicMock
        pred = semiconductor_dual_engine.predict_semiconductor_5m(client=self.mock_client)
        payload_str = semiconductor_dual_engine.build_score_detail_payload(pred, bot_name="TestBot")
        data = json.loads(payload_str)
        self.assertEqual(data["type"], "SEMICONDUCTOR_5M_SCORES")
        self.assertEqual(len(data["criteria"]), 7)
        for c in data["criteria"]:
            self.assertIn("id", c)
            self.assertIn("label", c)
            self.assertIn("score", c)
            self.assertIn("verdict", c)
            self.assertIn("desc", c)

        # Test UI rendering function inside BNB
        with patch.object(BNB.st, "markdown") as mock_md, \
             patch.object(BNB.st, "code") as mock_code, \
             patch.object(BNB.st, "expander") as mock_exp:
            BNB.render_log_detail(payload_str)
            self.assertTrue(mock_md.called)

    def test_bnb_auto_select_cycle_strategy_integration(self):
        """Test that BNB.py auto_select_cycle_strategy returns 5m prediction and correct viability flag."""
        strat, analysis = BNB.auto_select_cycle_strategy(
            client=self.mock_client,
            symbol="SOXLBUSDT",
            investment_usdt=50.0,
        )
        self.assertEqual(strat, "SEMICONDUCTOR_DUAL_ENGINE")
        self.assertIn("Semiconductor 5M", analysis["predicted_market"])
        self.assertIn("scores", analysis)
        self.assertIn("prediction_5m", analysis)

    def test_hybrid_strategy_interceptor(self):
        """Test that hybrid_strategy intercepts SOXLBUSDT and SOXSBUSDT properly."""
        hmp = hybrid_strategy.HybridMarketPredictor()
        regime = hmp.predict_market_and_strategy(
            client=self.mock_client,
            symbol="SOXLBUSDT",
            investment_usdt=50.0,
        )
        self.assertIn(regime["selected_algorithm"], ["SEMICONDUCTOR_DUAL_ENGINE", "BLOCKED"])
        self.assertIn("total_score", regime["indicators"])
        self.assertIn("scores", regime["indicators"])




class TestMicroProfitCapture(unittest.TestCase):
    def setUp(self):
        BNB.init_db()
        semiconductor_flip_flop.PRE_PURCHASE_WAIT_SEC = 0.0

    def test_fee_covered_target_price_math(self):
        """Test that calculate_fee_covered_target_price guarantees net profit >= target after maker and taker fees."""
        entry_cost = 25.0
        qty = 0.5
        buy_fee_usdt = 0.01875  # 0.075% on $25
        target_profit_usdt = 0.0035  # Micro-profit in 0.00XXX range
        tick_size = 0.01
        fee_rate = 0.00075  # BNB discount rate

        price, price_str, pct = semiconductor_flip_flop.calculate_fee_covered_target_price(
            entry_cost=entry_cost,
            qty=qty,
            buy_fee_usdt=buy_fee_usdt,
            target_profit_usdt=target_profit_usdt,
            tick_size=tick_size,
            fee_rate=fee_rate,
        )

        exit_val = price * qty
        sell_fee = exit_val * fee_rate
        net_profit = exit_val - entry_cost - buy_fee_usdt - sell_fee

        self.assertGreaterEqual(net_profit, target_profit_usdt)
        self.assertAlmostEqual(price, float(price_str))

    def test_live_net_pnl_calculation(self):
        """Test that compute_live_net_pnl accurately subtracts both buy fee and estimated sell fee."""
        cur_bid = 50.10
        qty = 0.5
        entry_cost = 25.00
        buy_fee_usdt = 0.01875
        fee_rate = 0.00075

        net_pnl, gross_val, est_sell_fee = semiconductor_flip_flop.compute_live_net_pnl(
            cur_bid=cur_bid,
            qty=qty,
            entry_cost=entry_cost,
            buy_fee_usdt=buy_fee_usdt,
            fee_rate=fee_rate,
        )

        expected_gross = 25.05
        expected_sell_fee = 25.05 * 0.00075
        expected_net = 25.05 - 25.00 - 0.01875 - expected_sell_fee

        self.assertAlmostEqual(gross_val, expected_gross, places=4)
        self.assertAlmostEqual(est_sell_fee, expected_sell_fee, places=5)
        self.assertAlmostEqual(net_pnl, expected_net, places=5)
        self.assertGreater(net_pnl, 0.0020)  # Micro-profit in 0.00XXX range

    def test_immediate_micro_profit_exit_simulation(self):
        """Test that trade_semiconductor_flip_flop_cycle immediately captures micro-profit when live bid reaches threshold."""
        import time
        mock_client = MagicMock()
        def mock_bal(asset):
            if asset == 'USDT':
                return {'asset': 'USDT', 'free': '100.0', 'locked': '0.0'}
            return {'asset': asset, 'free': '0.0', 'locked': '0.0'}
        mock_client.get_asset_balance.side_effect = mock_bal
        mock_client.cancel_all_open_orders.return_value = {}

        # Mock orderbook: entry ask 50.00, subsequent live bid 50.15 (yields 0.0X profit)
        ticker_stream = [
            {'symbol': 'SOXLBUSDT', 'askPrice': '50.00', 'bidPrice': '49.98'},
            {'symbol': 'SOXLBUSDT', 'askPrice': '50.15', 'bidPrice': '50.15'},
        ]
        def mock_ob(symbol):
            if ticker_stream:
                return ticker_stream.pop(0)
            return {'symbol': 'SOXLBUSDT', 'askPrice': '50.15', 'bidPrice': '50.15'}
        mock_client.get_orderbook_ticker.side_effect = mock_ob

        # Mock order executions: buy -> limit sell -> market sell
        mock_client.create_order.side_effect = [
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 11111,
                'executedQty': '0.500',
                'cummulativeQuoteQty': '25.00',
                'fills': [{'commission': '0.01875', 'commissionAsset': 'USDT'}],
            },
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 22222,
                'status': 'NEW',
            },
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 33333,
                'status': 'FILLED',
                'executedQty': '0.500',
                'cummulativeQuoteQty': '25.06',
                'fills': [{'commission': '0.01879', 'commissionAsset': 'USDT'}],
            },
        ]

        mock_client.get_order.return_value = {
            'symbol': 'SOXLBUSDT',
            'orderId': 22222,
            'status': 'NEW',
            'executedQty': '0.0',
        }

        with patch('semiconductor_dual_engine.predict_semiconductor_5m') as mock_pred, \
             patch('BNB.is_stop_requested', return_value=False), \
             patch('BNB.claim_symbol_for_instance', return_value=True), \
             patch('BNB.get_instance_committed_capital', return_value=0.0), \
             patch('BNB.insert_cycle', return_value=1), \
             patch('BNB.insert_trade', return_value=1), \
             patch('BNB.close_cycle', return_value=True), \
             patch('BNB.update_instance', return_value=True), \
             patch('BNB.release_symbol', return_value=True):

            mock_pred.return_value = {
                'total_score': 4,
                'bias': 'BULLISH',
                'action': 'LONG_SOXLB',
                'scores': {},
                'details': {},
            }

            pnl, status = semiconductor_flip_flop.trade_semiconductor_flip_flop_cycle(
                client=mock_client,
                instance_id=1,
                investment_usdt=25.0,
                target_profit=0.0035,
                stop_loss=-0.10,
                bot_name='UnitTestBot',
                symbol='SOXLBUSDT',
                lot={'min_notional': 5.0, 'tick_size': 0.01, 'step': 0.001, 'min_qty': 0.001},
                start_ts=time.time(),
            )

            self.assertEqual(status, 'SUCCESS')
            self.assertGreater(pnl, 0.0)
            mock_client.cancel_order.assert_called_with(symbol='SOXLBUSDT', orderId=22222)



    def test_position_already_held_safeguard(self):
        """Test that trade_semiconductor_flip_flop_cycle blocks buying if account already holds base tokens."""
        mock_client = MagicMock()
        def mock_bal(asset):
            if asset == 'SOXLB':
                return {'asset': 'SOXLB', 'free': '1.5', 'locked': '0.0'}
            return {'asset': 'USDT', 'free': '100.0', 'locked': '0.0'}
        mock_client.get_asset_balance.side_effect = mock_bal
        mock_client.get_orderbook_ticker.return_value = {'symbol': 'SOXLBUSDT', 'askPrice': '70.00', 'bidPrice': '69.95'}

        with patch('semiconductor_dual_engine.predict_semiconductor_5m') as mock_pred, \
             patch('BNB.is_stop_requested', return_value=False), \
             patch('BNB.claim_symbol_for_instance', return_value=True), \
             patch('BNB.get_instance_committed_capital', return_value=0.0), \
             patch('BNB.release_symbol', return_value=True), \
             patch('BNB.update_instance', return_value=True):

            mock_pred.return_value = {
                'total_score': 5,
                'bias': 'BULLISH',
                'action': 'LONG_SOXLB',
                'scores': {},
                'details': {},
            }

            pnl, status = semiconductor_flip_flop.trade_semiconductor_flip_flop_cycle(
                client=mock_client,
                instance_id=1,
                investment_usdt=25.0,
                target_profit=0.0035,
                stop_loss=-0.10,
                bot_name='UnitTestBot',
                symbol='SOXLBUSDT',
                lot={'min_notional': 5.0, 'tick_size': 0.01, 'step': 0.001, 'min_qty': 0.001},
                start_ts=time.time(),
            )
            self.assertEqual(status, 'POSITION_ALREADY_HELD')

    def test_mutual_exclusion_safeguard(self):
        """Test that trade_semiconductor_flip_flop_cycle enforces mutual exclusion when symbol is already claimed."""
        mock_client = MagicMock()
        mock_client.get_asset_balance.return_value = {'asset': 'USDT', 'free': '100.0', 'locked': '0.0'}
        mock_client.get_orderbook_ticker.return_value = {'symbol': 'SOXLBUSDT', 'askPrice': '70.00', 'bidPrice': '69.95'}

        with patch('semiconductor_dual_engine.predict_semiconductor_5m') as mock_pred, \
             patch('BNB.is_stop_requested', return_value=False), \
             patch('BNB.claim_symbol_for_instance', return_value=False), \
             patch('BNB.update_instance', return_value=True), \
             patch('BNB.log_event', return_value=True):

            mock_pred.return_value = {
                'total_score': 5,
                'bias': 'BULLISH',
                'action': 'LONG_SOXLB',
                'scores': {},
                'details': {},
            }

            pnl, status = semiconductor_flip_flop.trade_semiconductor_flip_flop_cycle(
                client=mock_client,
                instance_id=2,
                investment_usdt=25.0,
                target_profit=0.0035,
                stop_loss=-0.10,
                bot_name='UnitTestBot2',
                symbol='SOXLBUSDT',
                lot={'min_notional': 5.0, 'tick_size': 0.01, 'step': 0.001, 'min_qty': 0.001},
                start_ts=time.time(),
            )
            self.assertEqual(status, 'SYMBOL_BUSY')

    def test_capital_limit_safeguard(self):
        """Test that trade_semiconductor_flip_flop_cycle blocks buying when maximum investment is already reached."""
        mock_client = MagicMock()
        def mock_bal(asset):
            if asset == 'USDT':
                return {'asset': 'USDT', 'free': '100.0', 'locked': '0.0'}
            return {'asset': asset, 'free': '0.0', 'locked': '0.0'}
        mock_client.get_asset_balance.side_effect = mock_bal
        mock_client.get_orderbook_ticker.return_value = {'symbol': 'SOXLBUSDT', 'askPrice': '70.00', 'bidPrice': '69.95'}

        with patch('semiconductor_dual_engine.predict_semiconductor_5m') as mock_pred, \
             patch('BNB.is_stop_requested', return_value=False), \
             patch('BNB.claim_symbol_for_instance', return_value=True), \
             patch('BNB.get_instance_committed_capital', return_value=25.0), \
             patch('BNB.release_symbol', return_value=True), \
             patch('BNB.update_instance', return_value=True):

            mock_pred.return_value = {
                'total_score': 5,
                'bias': 'BULLISH',
                'action': 'LONG_SOXLB',
                'scores': {},
                'details': {},
            }

            pnl, status = semiconductor_flip_flop.trade_semiconductor_flip_flop_cycle(
                client=mock_client,
                instance_id=1,
                investment_usdt=25.0,
                target_profit=0.0035,
                stop_loss=-0.10,
                bot_name='UnitTestBot',
                symbol='SOXLBUSDT',
                lot={'min_notional': 5.0, 'tick_size': 0.01, 'step': 0.001, 'min_qty': 0.001},
                start_ts=time.time(),
            )
            self.assertEqual(status, 'CAPITAL_LIMIT_REACHED')

    def test_multi_bot_same_symbol_concurrency(self):
        """Test that multiple bots can trade the same symbol concurrently without exceeding investment limit."""
        mock_client = MagicMock()
        def mock_bal(asset):
            if asset == 'USDT':
                return {'asset': 'USDT', 'free': '75.0', 'locked': '0.0'}
            if asset == 'SOXLB':
                # Bot 1 is already holding 0.50 SOXLB in its active cycle
                return {'asset': 'SOXLB', 'free': '0.0', 'locked': '0.50'}
            return {'asset': asset, 'free': '0.0', 'locked': '0.0'}
        mock_client.get_asset_balance.side_effect = mock_bal

        # Mock orderbook: ask 50.00, bid 50.00
        mock_client.get_orderbook_ticker.return_value = {
            'symbol': 'SOXLBUSDT', 'askPrice': '50.00', 'bidPrice': '50.00'
        }

        # Mock order execution for Bot 2
        mock_client.create_order.side_effect = [
            # Buy order
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 20001,
                'executedQty': '0.499',
                'cummulativeQuoteQty': '24.95',
                'fills': [{'commission': '0.0187', 'commissionAsset': 'USDT'}],
            },
            # Limit sell order
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 20002,
                'status': 'NEW',
            },
        ]
        # Simulate limit sell filled
        mock_client.get_order.return_value = {
            'symbol': 'SOXLBUSDT',
            'orderId': 20002,
            'status': 'FILLED',
            'executedQty': '0.499',
            'cummulativeQuoteQty': '25.04',
        }

        # Mock database connection showing Bot 1's cycle of 0.50 SOXLB is active
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = None
        # SUM(qty) query returns 0.50 (Bot 1's tokens), and open cycle check for Bot 2 returns None
        mock_conn.execute.side_effect = [
            MagicMock(fetchone=lambda: (0.50,)),  # active_managed_qty for SOXLBUSDT
            MagicMock(fetchone=lambda: None),    # my_open_cycle for Bot 2 (none)
        ]

        with patch('semiconductor_dual_engine.predict_semiconductor_5m') as mock_pred, \
             patch('BNB.is_stop_requested', return_value=False), \
             patch('BNB.claim_symbol_for_instance', return_value=True), \
             patch('BNB.get_instance_committed_capital', return_value=0.0), \
             patch('BNB.get_conn', return_value=mock_conn), \
             patch('BNB.insert_cycle', return_value=2), \
             patch('BNB.insert_trade', return_value=2), \
             patch('BNB.close_cycle', return_value=True), \
             patch('BNB.update_instance', return_value=True), \
             patch('BNB.release_symbol', return_value=True):

            mock_pred.return_value = {
                'total_score': 5,
                'bias': 'BULLISH',
                'action': 'LONG_SOXLB',
                'scores': {},
                'details': {},
            }

            pnl, status = semiconductor_flip_flop.trade_semiconductor_flip_flop_cycle(
                client=mock_client,
                instance_id=2,  # Bot 2
                investment_usdt=25.0,
                target_profit=0.0035,
                stop_loss=-0.10,
                bot_name='UnitTestBot2',
                symbol='SOXLBUSDT',
                lot={'min_notional': 5.0, 'tick_size': 0.01, 'step': 0.001, 'min_qty': 0.001},
                start_ts=time.time(),
            )

            # Bot 2 successfully traded SOXLBUSDT concurrently while Bot 1 held position!
            self.assertEqual(status, 'SUCCESS')
            self.assertGreater(pnl, 0.0)

    def test_stop_loss_only_after_2_minutes(self):
        """Test that stop loss is protected during first 2 minutes, and only triggers after 120s."""
        mock_client = MagicMock()
        def mock_bal(asset):
            if asset == 'USDT':
                return {'asset': 'USDT', 'free': '100.0', 'locked': '0.0'}
            return {'asset': asset, 'free': '0.0', 'locked': '0.0'}
        mock_client.get_asset_balance.side_effect = mock_bal

        # Mock orderbook: ask 50.00, bid 49.80 (-0.40% loss, worse than -0.25% SL)
        mock_client.get_orderbook_ticker.return_value = {
            'symbol': 'SOXLBUSDT', 'askPrice': '50.00', 'bidPrice': '49.80'
        }

        mock_client.create_order.side_effect = [
            # Buy order
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 30001,
                'executedQty': '0.500',
                'cummulativeQuoteQty': '25.00',
                'fills': [{'commission': '0.0187', 'commissionAsset': 'USDT'}],
            },
            # Limit sell order
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 30002,
                'status': 'NEW',
            },
            # Market sell on stop loss
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 30003,
                'status': 'FILLED',
                'executedQty': '0.500',
                'cummulativeQuoteQty': '24.90',
                'fills': [{'commission': '0.0187', 'commissionAsset': 'USDT'}],
            },
        ]
        mock_client.get_order.return_value = {
            'symbol': 'SOXLBUSDT',
            'orderId': 30002,
            'status': 'NEW',
        }

        with patch('semiconductor_dual_engine.predict_semiconductor_5m') as mock_pred, \
             patch('semiconductor_flip_flop.PRE_PURCHASE_WAIT_SEC', 0.0), \
             patch('BNB.is_stop_requested', return_value=False), \
             patch('BNB.claim_symbol_for_instance', return_value=True), \
             patch('BNB.get_instance_committed_capital', return_value=0.0), \
             patch('BNB.insert_cycle', return_value=3), \
             patch('BNB.insert_trade', return_value=3), \
             patch('BNB.close_cycle', return_value=True), \
             patch('BNB.update_instance', return_value=True), \
             patch('BNB.release_symbol', return_value=True):

            mock_pred.return_value = {
                'total_score': 5,
                'bias': 'BULLISH',
                'action': 'LONG_SOXLB',
                'scores': {},
                'details': {},
            }

            # Simulate trade held for 125 seconds (exceeding 2-minute threshold)
            simulated_start = time.time() - 125.0

            pnl, status = semiconductor_flip_flop.trade_semiconductor_flip_flop_cycle(
                client=mock_client,
                instance_id=3,
                investment_usdt=25.0,
                target_profit=0.0035,
                stop_loss=-0.25,
                bot_name='UnitTestBot3',
                symbol='SOXLBUSDT',
                lot={'min_notional': 5.0, 'tick_size': 0.01, 'step': 0.001, 'min_qty': 0.001},
                start_ts=simulated_start,
            )

            self.assertEqual(status, 'FAILURE')
            mock_client.cancel_order.assert_called_with(symbol='SOXLBUSDT', orderId=30002)

    def test_stop_loss_protected_during_first_2_minutes(self):
        """Test that trade ignores stop-loss dip during first 2 minutes, allowing trade to reach target."""
        mock_client = MagicMock()
        def mock_bal(asset):
            if asset == 'USDT':
                return {'asset': 'USDT', 'free': '100.0', 'locked': '0.0'}
            return {'asset': asset, 'free': '0.0', 'locked': '0.0'}
        mock_client.get_asset_balance.side_effect = mock_bal

        # Mock orderbook:
        # Tick 1: ask 50.00 (entry)
        # Tick 2: bid 49.80 (dips below -0.25% stop loss at 45s - should be IGNORED because < 120s)
        # Tick 3: bid 50.15 (rebounds to target profit)
        ticker_stream = [
            {'symbol': 'SOXLBUSDT', 'askPrice': '50.00', 'bidPrice': '49.98'}, # orphan check
            {'symbol': 'SOXLBUSDT', 'askPrice': '50.00', 'bidPrice': '49.98'}, # buy entry
            {'symbol': 'SOXLBUSDT', 'askPrice': '49.85', 'bidPrice': '49.80'}, # poll 1 (dip)
            {'symbol': 'SOXLBUSDT', 'askPrice': '50.18', 'bidPrice': '50.15'}, # poll 2 (rebound to target)
        ]
        def mock_ob(symbol):
            if ticker_stream:
                return ticker_stream.pop(0)
            return {'symbol': 'SOXLBUSDT', 'askPrice': '50.18', 'bidPrice': '50.15'}
        mock_client.get_orderbook_ticker.side_effect = mock_ob

        mock_client.create_order.side_effect = [
            # Buy order
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 35001,
                'executedQty': '0.500',
                'cummulativeQuoteQty': '25.00',
                'fills': [{'commission': '0.0187', 'commissionAsset': 'USDT'}],
            },
            # Limit sell order
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 35002,
                'status': 'NEW',
            },
            # Market sell on target hit
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 35003,
                'status': 'FILLED',
                'executedQty': '0.500',
                'cummulativeQuoteQty': '25.07',
                'fills': [{'commission': '0.0187', 'commissionAsset': 'USDT'}],
            },
        ]
        mock_client.get_order.return_value = {
            'symbol': 'SOXLBUSDT',
            'orderId': 35002,
            'status': 'NEW',
        }

        with patch('semiconductor_dual_engine.predict_semiconductor_5m') as mock_pred, \
             patch('semiconductor_flip_flop.PRE_PURCHASE_WAIT_SEC', 0.0), \
             patch('BNB.is_stop_requested', return_value=False), \
             patch('BNB.claim_symbol_for_instance', return_value=True), \
             patch('BNB.get_instance_committed_capital', return_value=0.0), \
             patch('BNB.insert_cycle', return_value=35), \
             patch('BNB.insert_trade', return_value=35), \
             patch('BNB.close_cycle', return_value=True), \
             patch('BNB.update_instance', return_value=True), \
             patch('BNB.release_symbol', return_value=True):

            mock_pred.return_value = {
                'total_score': 5,
                'bias': 'BULLISH',
                'action': 'LONG_SOXLB',
                'scores': {},
                'details': {},
            }

            # Simulate hold_sec starts at 45 seconds (< 120s buffer)
            simulated_start = time.time() - 45.0

            pnl, status = semiconductor_flip_flop.trade_semiconductor_flip_flop_cycle(
                client=mock_client,
                instance_id=35,
                investment_usdt=25.0,
                target_profit=0.0035,
                stop_loss=-0.25,
                bot_name='UnitTestBot35',
                symbol='SOXLBUSDT',
                lot={'min_notional': 5.0, 'tick_size': 0.01, 'step': 0.001, 'min_qty': 0.001},
                start_ts=simulated_start,
            )

            # Dip was ignored during the 2-minute buffer, allowing the trade to hit SUCCESS!
            self.assertEqual(status, 'SUCCESS')
            self.assertGreater(pnl, 0.0)

    def test_breakeven_lock_defense(self):
        """Test that trade locks at breakeven when position was green (+0.0025) and pulls back towards zero."""
        mock_client = MagicMock()
        def mock_bal(asset):
            if asset == 'USDT':
                return {'asset': 'USDT', 'free': '100.0', 'locked': '0.0'}
            return {'asset': asset, 'free': '0.0', 'locked': '0.0'}
        mock_client.get_asset_balance.side_effect = mock_bal

        ticker_stream = [
            {'symbol': 'SOXLBUSDT', 'askPrice': '50.00', 'bidPrice': '49.98'}, # orphan check
            {'symbol': 'SOXLBUSDT', 'askPrice': '50.00', 'bidPrice': '49.98'}, # buy entry
            {'symbol': 'SOXLBUSDT', 'askPrice': '50.15', 'bidPrice': '50.12'}, # poll 1: peak green (net pnl >= 0.0200 USDT)
            {'symbol': 'SOXLBUSDT', 'askPrice': '50.08', 'bidPrice': '50.08'}, # poll 2: pullback to breakeven floor (<= 0.0100 USDT)
        ]
        def mock_ob(symbol):
            if ticker_stream:
                return ticker_stream.pop(0)
            return {'symbol': 'SOXLBUSDT', 'askPrice': '50.08', 'bidPrice': '50.08'}
        mock_client.get_orderbook_ticker.side_effect = mock_ob

        mock_client.create_order.side_effect = [
            # Buy order
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 40001,
                'executedQty': '0.500',
                'cummulativeQuoteQty': '25.00',
                'fills': [{'commission': '0.0187', 'commissionAsset': 'USDT'}],
            },
            # Limit sell order
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 40002,
                'status': 'NEW',
            },
            # Market sell on breakeven lock
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 40003,
                'status': 'FILLED',
                'executedQty': '0.500',
                'cummulativeQuoteQty': '25.02',
                'fills': [{'commission': '0.0187', 'commissionAsset': 'USDT'}],
            },
        ]
        mock_client.get_order.return_value = {
            'symbol': 'SOXLBUSDT',
            'orderId': 40002,
            'status': 'NEW',
        }

        with patch('semiconductor_dual_engine.predict_semiconductor_5m') as mock_pred, \
             patch('semiconductor_flip_flop.PRE_PURCHASE_WAIT_SEC', 0.0), \
             patch('BNB.is_stop_requested', return_value=False), \
             patch('BNB.claim_symbol_for_instance', return_value=True), \
             patch('BNB.get_instance_committed_capital', return_value=0.0), \
             patch('BNB.insert_cycle', return_value=4), \
             patch('BNB.insert_trade', return_value=4), \
             patch('BNB.close_cycle', return_value=True), \
             patch('BNB.update_instance', return_value=True), \
             patch('BNB.release_symbol', return_value=True):

            mock_pred.return_value = {
                'total_score': 5,
                'bias': 'BULLISH',
                'action': 'LONG_SOXLB',
                'scores': {},
                'details': {},
            }

            pnl, status = semiconductor_flip_flop.trade_semiconductor_flip_flop_cycle(
                client=mock_client,
                instance_id=4,
                investment_usdt=25.0,
                target_profit=0.0350,  # higher target (0.0X range) so it doesn't immediately hit target on first tick
                stop_loss=-0.25,
                bot_name='UnitTestBot4',
                symbol='SOXLBUSDT',
                lot={'min_notional': 5.0, 'tick_size': 0.01, 'step': 0.001, 'min_qty': 0.001},
                start_ts=time.time(),
            )

            self.assertEqual(status, 'SUCCESS')
            mock_client.cancel_order.assert_called_with(symbol='SOXLBUSDT', orderId=40002)

    def test_wait_for_profit_until_0_0x_range(self):
        """Test that trade does NOT prematurely exit at 180s, but waits for profit to reach 0.0X range."""
        mock_client = MagicMock()
        def mock_bal(asset):
            if asset == 'USDT':
                return {'asset': 'USDT', 'free': '100.0', 'locked': '0.0'}
            return {'asset': asset, 'free': '0.0', 'locked': '0.0'}
        mock_client.get_asset_balance.side_effect = mock_bal

        # Mock orderbook:
        # Tick 1 & 2: flat (at 185s, hold continues without premature 180s exit!)
        # Tick 3: bid rises to 50.18 (reaches 0.0X profit target)
        ticker_stream = [
            {'symbol': 'SOXLBUSDT', 'askPrice': '50.00', 'bidPrice': '49.98'}, # orphan check
            {'symbol': 'SOXLBUSDT', 'askPrice': '50.00', 'bidPrice': '49.98'}, # buy entry
            {'symbol': 'SOXLBUSDT', 'askPrice': '50.01', 'bidPrice': '50.00'}, # poll 1 (185s hold, waits for profit)
            {'symbol': 'SOXLBUSDT', 'askPrice': '50.20', 'bidPrice': '50.18'}, # poll 2 (reaches 0.0X profit target)
        ]
        def mock_ob(symbol):
            if ticker_stream:
                return ticker_stream.pop(0)
            return {'symbol': 'SOXLBUSDT', 'askPrice': '50.20', 'bidPrice': '50.18'}
        mock_client.get_orderbook_ticker.side_effect = mock_ob

        mock_client.create_order.side_effect = [
            # Buy order
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 50001,
                'executedQty': '0.500',
                'cummulativeQuoteQty': '25.00',
                'fills': [{'commission': '0.0187', 'commissionAsset': 'USDT'}],
            },
            # Limit sell order
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 50002,
                'status': 'NEW',
            },
            # Market sell on 0.0X target hit
            {
                'symbol': 'SOXLBUSDT',
                'orderId': 50003,
                'status': 'FILLED',
                'executedQty': '0.500',
                'cummulativeQuoteQty': '25.09',
                'fills': [{'commission': '0.0187', 'commissionAsset': 'USDT'}],
            },
        ]
        mock_client.get_order.return_value = {
            'symbol': 'SOXLBUSDT',
            'orderId': 50002,
            'status': 'NEW',
        }

        with patch('semiconductor_dual_engine.predict_semiconductor_5m') as mock_pred, \
             patch('semiconductor_flip_flop.PRE_PURCHASE_WAIT_SEC', 0.0), \
             patch('BNB.is_stop_requested', return_value=False), \
             patch('BNB.claim_symbol_for_instance', return_value=True), \
             patch('BNB.get_instance_committed_capital', return_value=0.0), \
             patch('BNB.insert_cycle', return_value=5), \
             patch('BNB.insert_trade', return_value=5), \
             patch('BNB.close_cycle', return_value=True), \
             patch('BNB.update_instance', return_value=True), \
             patch('BNB.release_symbol', return_value=True):

            mock_pred.return_value = {
                'total_score': 5,
                'bias': 'BULLISH',
                'action': 'LONG_SOXLB',
                'scores': {},
                'details': {},
            }

            # Simulate start_ts was 185 seconds ago (past 180s)
            simulated_start = time.time() - 185.0

            pnl, status = semiconductor_flip_flop.trade_semiconductor_flip_flop_cycle(
                client=mock_client,
                instance_id=5,
                investment_usdt=25.0,
                target_profit=0.0250,
                stop_loss=-0.25,
                bot_name='UnitTestBot5',
                symbol='SOXLBUSDT',
                lot={'min_notional': 5.0, 'tick_size': 0.01, 'step': 0.001, 'min_qty': 0.001},
                start_ts=simulated_start,
            )

            # Did NOT exit on 180s timeout; successfully waited for 0.0X profit and completed with SUCCESS!
            self.assertEqual(status, 'SUCCESS')
            self.assertGreater(pnl, 0.0)
            mock_client.cancel_order.assert_called_with(symbol='SOXLBUSDT', orderId=50002)


if __name__ == "__main__":
    unittest.main()
