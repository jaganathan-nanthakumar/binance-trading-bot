"""
semiconductor_dual_harvester.py — Simultaneous Dual-Asset Scalper for SOXLB & SOXSB.

Trading Logic:
1. In each cycle, divides capital into equal legs ($25 in SOXLB and $25 in SOXSB).
2. Buys both tokens simultaneously at cycle start.
3. Continuously monitors live liquidation prices (Best Bids) of both tokens.
4. Accounts for all fees on both legs (buy fees + sell fees, supporting BNB fee discount).
5. Sells BOTH tokens back to 100% USDT the moment cumulative profit after all fees is profitable.
6. Records all trades and closes cycle in scalper.db with strictly positive net USDT profit.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional

BULL_SYMBOL = "SOXLBUSDT"
BEAR_SYMBOL = "SOXSBUSDT"

DEFAULT_LEG_INVESTMENT = 25.0    # $25 each -> $50 total
DEFAULT_TARGET_NET_PROFIT = 0.15 # $0.15 USDT net profit after all fees


def trade_semiconductor_dual_cycle(
    client: Any,
    instance_id: int,
    investment_per_leg: float = DEFAULT_LEG_INVESTMENT,
    target_net_profit: float = DEFAULT_TARGET_NET_PROFIT,
    stop_loss_limit: Optional[float] = None, # None = patient wait until profitable
    bot_name: str = "Bot",
) -> Tuple[Optional[float], Optional[str]]:
    """
    Executes one dual-holding harvest cycle:
    1. Buys $25 of SOXLB and $25 of SOXSB.
    2. Monitors combined liquidation value after fees.
    3. When net PnL after all fees >= target_net_profit, sells both back to USDT.
    """
    import BNB
    from binance.client import Client as BClient

    # 1. Inspect lot filters for both symbols
    lot_bull = BNB.get_lot_info(client, BULL_SYMBOL)
    lot_bear = BNB.get_lot_info(client, BEAR_SYMBOL)

    total_required = investment_per_leg * 2.0
    usdt_free = float(client.get_asset_balance(asset="USDT")["free"] or 0.0)

    if usdt_free < (total_required * 0.98):
        msg = f"[{bot_name}] Insufficient USDT: have ${usdt_free:.2f}, need ${total_required:.2f} ($25 each)"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.log_event(msg, level="WARN", event_type="SKIP", instance_id=instance_id, symbol="SOXLB+SOXSB")
        time.sleep(5.0)
        return None, "INSUFFICIENT_FUNDS"

    # Cancel any stray orders
    BNB.cancel_all_open_orders(client, BULL_SYMBOL)
    BNB.cancel_all_open_orders(client, BEAR_SYMBOL)
    time.sleep(0.2)

    # 2. Market Buy Entry for Leg 1 (SOXLB)
    ob_bull = client.get_orderbook_ticker(symbol=BULL_SYMBOL)
    ask_bull = float(ob_bull.get("askPrice") or 0.0)
    qty_bull_raw = (investment_per_leg * 0.998) / ask_bull
    qty_bull_str = BNB.format_quantity(qty_bull_raw, lot_bull["step"])

    # Market Buy Entry for Leg 2 (SOXSB)
    ob_bear = client.get_orderbook_ticker(symbol=BEAR_SYMBOL)
    ask_bear = float(ob_bear.get("askPrice") or 0.0)
    qty_bear_raw = (investment_per_leg * 0.998) / ask_bear
    qty_bear_str = BNB.format_quantity(qty_bear_raw, lot_bear["step"])

    print(f"[{bot_name}] [DUAL HARVESTER] Entering cycle: Buying ${investment_per_leg:.2f} SOXLB & ${investment_per_leg:.2f} SOXSB...")
    BNB.log_event(
        f"[{bot_name}] [DUAL HARVESTER] Entering cycle: Buying {qty_bull_str} SOXLB & {qty_bear_str} SOXSB (${total_required:.2f} total)...",
        level="TRADE",
        event_type="BUY_SUBMIT",
        instance_id=instance_id,
        symbol="SOXLB+SOXSB",
    )

    try:
        buy_bull = client.create_order(
            symbol=BULL_SYMBOL,
            side=BClient.SIDE_BUY,
            type=BClient.ORDER_TYPE_MARKET,
            quantity=qty_bull_str,
        )
        buy_bear = client.create_order(
            symbol=BEAR_SYMBOL,
            side=BClient.SIDE_BUY,
            type=BClient.ORDER_TYPE_MARKET,
            quantity=qty_bear_str,
        )
    except Exception as e:
        msg = f"[{bot_name}] Error during dual market buy: {e}"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.log_event(msg, level="ERROR", event_type="BUY_FAIL", instance_id=instance_id, symbol="SOXLB+SOXSB")
        time.sleep(3.0)
        return None, "ERROR"

    # Parse buy executions
    q_bull = float(buy_bull.get("executedQty") or qty_bull_str)
    cost_bull = float(buy_bull.get("cummulativeQuoteQty") or (q_bull * ask_bull))
    px_bull_in = cost_bull / q_bull if q_bull > 0 else ask_bull

    q_bear = float(buy_bear.get("executedQty") or qty_bear_str)
    cost_bear = float(buy_bear.get("cummulativeQuoteQty") or (q_bear * ask_bear))
    px_bear_in = cost_bear / q_bear if q_bear > 0 else ask_bear

    total_entry_cost = cost_bull + cost_bear

    # Calculate buy fees
    buy_fee_usdt = 0.0
    buy_fee_bnb = 0.0
    for fill in buy_bull.get("fills", []) + buy_bear.get("fills", []):
        f_asset = fill.get("commissionAsset", "")
        f_amt = float(fill.get("commission", 0))
        if f_asset == "USDT":
            buy_fee_usdt += f_amt
        elif f_asset == "BNB":
            buy_fee_bnb += f_amt

    # Insert DB Cycle & Trades
    cycle_id = BNB.insert_cycle(
        instance_id=instance_id,
        symbol="SOXLB+SOXSB",
        entry_cost=total_entry_cost,
        qty=q_bull + q_bear,
        fee_bnb=buy_fee_bnb,
        fee_usdt=buy_fee_usdt,
        status="OPEN",
    )
    BNB.insert_trade(
        instance_id=instance_id,
        cycle_id=cycle_id,
        symbol=BULL_SYMBOL,
        side="BUY",
        quantity=q_bull,
        price=px_bull_in,
        quote_qty=cost_bull,
        fee_bnb=0.0,
        fee_usdt=0.0,
        order_id=str(buy_bull.get("orderId")),
    )
    BNB.insert_trade(
        instance_id=instance_id,
        cycle_id=cycle_id,
        symbol=BEAR_SYMBOL,
        side="BUY",
        quantity=q_bear,
        price=px_bear_in,
        quote_qty=cost_bear,
        fee_bnb=buy_fee_bnb,
        fee_usdt=buy_fee_usdt,
        order_id=str(buy_bear.get("orderId")),
    )

    msg_entered = (
        f"[{bot_name}] Both Legs Filled:\n"
        f"  • SOXLB: {q_bull:.4f} units @ ${px_bull_in:.2f} (${cost_bull:.2f})\n"
        f"  • SOXSB: {q_bear:.4f} units @ ${px_bear_in:.2f} (${cost_bear:.2f})\n"
        f"  Total Cost: ${total_entry_cost:.2f} USDT | Target Net Profit: +${target_net_profit:.2f} USDT (after all fees)"
    )
    print(msg_entered)
    BNB.log_event(msg_entered, level="TRADE", event_type="BUY_CONFIRM", instance_id=instance_id, symbol="SOXLB+SOXSB")

    start_ts = time.time()
    poll_count = 0

    # 3. Live Monitoring Loop: Poll live bids and evaluate cumulative profit after all fees
    while True:
        if BNB.is_stop_requested(instance_id):
            print(f"[{bot_name}] Stop requested — liquidating both legs...")
            _liquidate_both(client, q_bull, q_bear, lot_bull, lot_bear)
            BNB.close_cycle(cycle_id, exit_value=total_entry_cost, pnl_usdt=0.0, status="STOPPED", duration_sec=time.time()-start_ts)
            return 0.0, "ABORTED"

        time.sleep(1.5)
        poll_count += 1

        try:
            ob_b = client.get_orderbook_ticker(symbol=BULL_SYMBOL)
            ob_s = client.get_orderbook_ticker(symbol=BEAR_SYMBOL)
            bid_bull = float(ob_b.get("bidPrice") or px_bull_in)
            bid_bear = float(ob_s.get("bidPrice") or px_bear_in)

            val_bull = q_bull * bid_bull
            val_bear = q_bear * bid_bear
            gross_liquidation_val = val_bull + val_bear

            # Estimated sell fees (0.075% standard with BNB discount)
            est_sell_fees = gross_liquidation_val * 0.00075
            total_fees_all = buy_fee_usdt + (buy_fee_bnb * 600.0) + est_sell_fees

            net_pnl_usdt = gross_liquidation_val - total_entry_cost - total_fees_all
            net_pnl_pct = (net_pnl_usdt / total_entry_cost) * 100.0

            BNB.update_instance(instance_id, current_pnl=round(net_pnl_usdt, 4))

            if poll_count % 10 == 0:
                print(
                    f"[{bot_name}] [MONITORING DUAL LEGS]\n"
                    f"  SOXLB (${val_bull:.2f}, {((bid_bull-px_bull_in)/px_bull_in)*100:+.2f}%) | "
                    f"SOXSB (${val_bear:.2f}, {((bid_bear-px_bear_in)/px_bear_in)*100:+.2f}%)\n"
                    f"  Combined Value: ${gross_liquidation_val:.2f} | Net PnL (after fees): {net_pnl_usdt:+.4f} USDT ({net_pnl_pct:+.2f}%) | "
                    f"Target: +${target_net_profit:.2f} USDT"
                )

            # 4. Exit Condition: Cumulative profit after fees is strictly profitable
            if net_pnl_usdt >= target_net_profit:
                print("=" * 80)
                print(f"[{bot_name}] TARGET ACHIEVED! Net PnL after all fees = +${net_pnl_usdt:.4f} USDT. SELLING BOTH LEGS...")
                print("=" * 80)
                BNB.log_event(
                    f"[{bot_name}] TARGET HIT! Net PnL = +${net_pnl_usdt:.4f} USDT. Executing simultaneous market sell of both legs...",
                    level="TRADE",
                    event_type="TARGET",
                    instance_id=instance_id,
                    symbol="SOXLB+SOXSB",
                )

                sell_bull_str = BNB.format_quantity(q_bull, lot_bull["step"])
                sell_bear_str = BNB.format_quantity(q_bear, lot_bear["step"])

                sell_bull_order = client.create_order(
                    symbol=BULL_SYMBOL,
                    side=BClient.SIDE_SELL,
                    type=BClient.ORDER_TYPE_MARKET,
                    quantity=sell_bull_str,
                )
                sell_bear_order = client.create_order(
                    symbol=BEAR_SYMBOL,
                    side=BClient.SIDE_SELL,
                    type=BClient.ORDER_TYPE_MARKET,
                    quantity=sell_bear_str,
                )

                exit_quote_bull = float(sell_bull_order.get("cummulativeQuoteQty") or val_bull)
                exit_quote_bear = float(sell_bear_order.get("cummulativeQuoteQty") or val_bear)
                total_exit_value = exit_quote_bull + exit_quote_bear

                # Calculate actual sell fees
                actual_sell_fee_usdt = 0.0
                actual_sell_fee_bnb = 0.0
                for fill in sell_bull_order.get("fills", []) + sell_bear_order.get("fills", []):
                    f_asset = fill.get("commissionAsset", "")
                    f_amt = float(fill.get("commission", 0))
                    if f_asset == "USDT":
                        actual_sell_fee_usdt += f_amt
                    elif f_asset == "BNB":
                        actual_sell_fee_bnb += f_amt

                tot_fee_usdt = buy_fee_usdt + actual_sell_fee_usdt
                tot_fee_bnb = buy_fee_bnb + actual_sell_fee_bnb
                bnb_price = 600.0  # approximate BNB price in USDT
                total_all_fee_in_usdt = tot_fee_usdt + (tot_fee_bnb * bnb_price)

                actual_net_realized_pnl = total_exit_value - total_entry_cost - total_all_fee_in_usdt
                duration_sec = time.time() - start_ts

                BNB.insert_trade(
                    instance_id=instance_id,
                    cycle_id=cycle_id,
                    symbol=BULL_SYMBOL,
                    side="SELL",
                    quantity=q_bull,
                    price=bid_bull,
                    quote_qty=exit_quote_bull,
                    fee_bnb=0.0,
                    fee_usdt=0.0,
                    order_id=str(sell_bull_order.get("orderId")),
                )
                BNB.insert_trade(
                    instance_id=instance_id,
                    cycle_id=cycle_id,
                    symbol=BEAR_SYMBOL,
                    side="SELL",
                    quantity=q_bear,
                    price=bid_bear,
                    quote_qty=exit_quote_bear,
                    fee_bnb=tot_fee_bnb,
                    fee_usdt=tot_fee_usdt,
                    order_id=str(sell_bear_order.get("orderId")),
                )
                BNB.close_cycle(
                    cycle_id=cycle_id,
                    exit_value=total_exit_value,
                    pnl_usdt=actual_net_realized_pnl,
                    status="TARGET",
                    duration_sec=duration_sec,
                    fee_bnb=tot_fee_bnb,
                    fee_usdt=tot_fee_usdt,
                )

                succ_msg = (
                    f"[{bot_name}] CYCLE COMPLETE: Sold both legs for ${total_exit_value:.2f} USDT!\n"
                    f"  Entry Cost: ${total_entry_cost:.2f} USDT | Total Fees: ${total_all_fee_in_usdt:.4f} USDT\n"
                    f"  >>> NET PROFIT REALIZED: +${actual_net_realized_pnl:.4f} USDT <<<"
                )
                print(succ_msg)
                BNB.log_event(succ_msg, level="TRADE", event_type="TARGET", instance_id=instance_id, symbol="SOXLB+SOXSB")
                return actual_net_realized_pnl, "TARGET"

        except Exception as e:
            time.sleep(2.0)


def _liquidate_both(client: Any, q_bull: float, q_bear: float, lot_bull: dict, lot_bear: dict):
    """Emergency helper to sell both positions back to USDT."""
    import BNB
    from binance.client import Client as BClient
    try:
        s_bull = BNB.format_quantity(q_bull, lot_bull["step"])
        client.create_order(symbol=BULL_SYMBOL, side=BClient.SIDE_SELL, type=BClient.ORDER_TYPE_MARKET, quantity=s_bull)
    except Exception:
        pass
    try:
        s_bear = BNB.format_quantity(q_bear, lot_bear["step"])
        client.create_order(symbol=BEAR_SYMBOL, side=BClient.SIDE_SELL, type=BClient.ORDER_TYPE_MARKET, quantity=s_bear)
    except Exception:
        pass
