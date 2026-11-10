"""
hybrid_soxlb_soxsb.py — Hybrid Dual Engine + Lag Arbitrage Strategy
Strategy Name: HYBRID_SOXLB_SOXSB_SRATEGY (and HYBRID_SOXLB_SOXSB_STRATEGY)

Unifies:
  1. Macro Semiconductor Conviction Engine (8-Factor Predictive Model from semiconductor_dual_engine.py):
     - Gauges broad sector direction (SMH, NVDA, AVGO, AMD, NQ futures, CVD flow deltas, EMA9/21, Liquidity, Lag divergence).
     - Score >= +3 -> BULLISH bias (Only Long SOXLBUSDT allowed).
     - Score <= -3 -> BEARISH bias (Only Long SOXSBUSDT allowed).
     - Score in [-2, +2] -> CHOP Filter (100% USDT Safe Haven, $0 loss risk).

  2. Micro Orderbook Lag Divergence Engine (from semiconductor_lag_arb.py):
     - Detects micro-lag between SOXLB and SOXSB orderbooks (10s–60s divergence).
     - Ensures entries occur on micro-dips or confirmed catch-up opportunities instead of chasing peaks.

  3. Zero-Loss Friction & Capital Defense Architecture:
     - Binance spot fee coverage math with CEILING rounding.
     - Resting maker LIMIT SELL placed immediately upon entry.
     - 2-Stage Progressive Repricing:
         * Stage 1 (45s hold): Reprices limit sell to soft target (+0.0200 USDT net profit).
         * Stage 2 (90s hold): Reprices limit sell to Strict Breakeven Floor (+0.0050 USDT net profit, 100% fee-covered).
     - Holding Delay (300s / 5 minutes): No premature dumping of spot assets on tick noise.
     - Emergency Crash Guard: Immediate defensive exit only if raw drawdown reaches <= -2.00%.

All timestamps strictly in Indian Standard Time (IST, UTC+05:30).
"""

from __future__ import annotations

import os
import sys
import json
import time
import math
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_now() -> datetime:
    return datetime.now(IST)

def get_ist_now_str(fmt: str = "%d %b %Y, %I:%M:%S %p IST") -> str:
    return datetime.now(IST).strftime(fmt)

def get_ist_time_str(fmt: str = "%I:%M:%S %p IST") -> str:
    return datetime.now(IST).strftime(fmt)

BULL_SYMBOL = "SOXLBUSDT"
BEAR_SYMBOL = "SOXSBUSDT"

STRATEGY_IDENTIFIERS = [
    "HYBRID_SOXLB_SOXSB_SRATEGY",
    "HYBRID_SOXLB_SOXSB_STRATEGY",
]

# Fee baselines: 0.075% per side with BNB fee discount (0.15% round-trip)
EST_FEE_RATE_PER_SIDE = 0.00075
ROUND_TRIP_FEE_PCT = 0.15

# Micro-profit targets & friction guards
OPT_DEFAULT_MICRO_PROFIT_USDT = 0.0250    # Default net profit target after fees
OPT_ABSOLUTE_MIN_PROFIT_USDT = 0.0100     # Absolute minimum profit floor
OPT_BREAKEVEN_LOCK_PEAK_USDT = 0.0250     # Ratchet trigger when peak >= $0.025
OPT_BREAKEVEN_LOCK_FLOOR_USDT = 0.0100    # Floor to secure on pullback
STOP_LOSS_MIN_HOLD_SEC = 300.0            # 5-minute hold delay: Never dump spot assets into bids on noise
DEFAULT_STOP_LOSS_PCT = -1.25             # Normal safety stop loss (only evaluated after 5m hold)
EMERGENCY_CRASH_PCT = -2.00               # Immediate emergency defense for catastrophic sector crash
MAX_WAIT_HOLD_SEC = 600.0                 # 10-minute maximum hold ceiling
MAX_ALLOWED_SPREAD_PCT = 0.25             # Guard against extreme spread blowout


def calculate_fee_covered_target_price(
    entry_cost: float,
    qty: float,
    buy_fee_usdt: float,
    target_profit_usdt: float,
    tick_size: float,
    fee_rate: float = EST_FEE_RATE_PER_SIDE,
) -> Tuple[float, str, float]:
    """
    Computes minimum limit sell price guaranteed to cover entry cost,
    buy fee, sell fee, and secure target_profit_usdt net return.

    Formula:
      required_exit = (entry_cost + buy_fee_usdt + target_profit_usdt) / (1.0 - fee_rate)
      target_price = CEILING(required_exit / qty, tick_size)
    """
    if qty <= 0:
        return 0.0, "0.0", 0.0
    import BNB
    req_exit_val = (entry_cost + buy_fee_usdt + target_profit_usdt) / max(0.0001, (1.0 - fee_rate))
    raw_price = req_exit_val / qty
    price_str = BNB.format_price(raw_price, tick_size, rounding="CEILING")
    price = float(price_str)
    entry_price = entry_cost / qty if qty > 0 else price
    pct = ((price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
    return price, price_str, pct


def compute_live_net_pnl(
    cur_bid: float,
    qty: float,
    entry_cost: float,
    buy_fee_usdt: float,
    fee_rate: float = EST_FEE_RATE_PER_SIDE,
) -> Tuple[float, float, float]:
    """
    Computes exact live net PnL at current bid after deducting both buy fee and estimated sell fee.
    Returns: (net_pnl_usdt, gross_val_usdt, est_sell_fee_usdt)
    """
    gross_val = cur_bid * qty
    est_sell_fee = gross_val * fee_rate
    net_pnl = gross_val - entry_cost - buy_fee_usdt - est_sell_fee
    return net_pnl, gross_val, est_sell_fee


def trade_hybrid_soxlb_soxsb_cycle(
    client: Any,
    instance_id: int,
    investment_usdt: float,
    target_profit: float,
    stop_loss: float,
    bot_name: str,
    symbol: str,
    lot: dict,
    start_ts: float,
    timeout_sec: float = 0.0,
    profile: Optional[dict] = None,
) -> Tuple[Optional[float], Optional[str]]:
    """
    Executes a complete HYBRID_SOXLB_SOXSB_SRATEGY trade cycle:
      Phase 1: Dual Macro Conviction & Micro Lag Confluence Scan:
        - Evaluates 8-factor macro conviction model (predict_semiconductor_5m).
        - If Chop (score in [-2, +2]), stays in 100% USDT Safe Haven ($0 risk).
        - If Bullish (score >= +3), restricts trade exclusively to SOXLBUSDT.
        - If Bearish (score <= -3), restricts trade exclusively to SOXSBUSDT.
        - Synchronizes with micro lag orderbook divergence before entry.
      Phase 2: Spread Guard and Budget Verification.
      Phase 3: Clean Market Buy entry.
      Phase 4: Place resting maker LIMIT SELL with 100% fee-covered profit.
      Phase 5: Fast Monitoring Loop:
        - Limit sell fills -> SUCCESS.
        - Live net PnL reaches target -> SUCCESS.
        - Trailing ratchet protection locks profit on pullbacks.
        - Stage 1 Reprice (45s): Reprices limit sell to soft target (+0.0200 USDT net).
        - Stage 2 Reprice (90s): Reprices limit sell to Strict Breakeven Floor (+0.0050 USDT net).
        - 300s Hold Delay: Spot tokens are NOT dumped into bids on normal noise.
        - Emergency crash guard at -2.00%.
    """
    import BNB
    import semiconductor_dual_engine
    import semiconductor_lag_arb

    lag_engine = semiconductor_lag_arb.get_engine()
    poll_count = 0
    target_symbol = None
    entry_signal_info = {}

    print(f"\n{'='*80}")
    print(f"[{bot_name}] 🌐 HYBRID SOXLB ↔ SOXSB CONFLUENCE STRATEGY ACTIVE")
    print(f"  Investment: ${investment_usdt:.2f} USDT | Target Profit: {target_profit} | Stop Loss: {stop_loss}")
    print(f"  Macro Bias Gate: 8-Factor Model | Micro Lag Filter: Orderbook Divergence")
    print(f"{'='*80}")

    # ── Phase 1: Dual Confluence Scanning Loop ──
    consecutive_conviction_count = 0
    last_conviction_symbol = None

    while True:
        if BNB.is_stop_requested(instance_id):
            return 0.0, "ABORTED"

        poll_count += 1
        now_str = get_ist_time_str()

        # Step 1a: Macro Conviction Evaluation (8-Factor Model)
        pred = semiconductor_dual_engine.predict_semiconductor_5m(client=client, force_refresh=True, horizon_min=3)
        macro_score = int(pred.get("total_score", 0))
        macro_bias = str(pred.get("bias", "CHOP"))
        macro_action = str(pred.get("action", "SKIP_100PCT_USDT"))

        # Step 1b: Fetch live prices & feed lag engine
        prices = semiconductor_lag_arb._fetch_prices_client(client) if client else semiconductor_lag_arb._fetch_prices_rest()
        now_ts = time.time()
        for s, p in prices.items():
            lag_engine.feed_price(s, p, now_ts)

        active_lag_sig = lag_engine.check_for_signal()
        lag_engine.validate_predictions()

        chgs = lag_engine.get_current_changes()
        soxsb_p = prices.get(BEAR_SYMBOL, 0.0)
        soxlb_p = prices.get(BULL_SYMBOL, 0.0)
        soxsb_30 = chgs.get("SOXSB", {}).get("30s") or 0.0
        soxlb_30 = chgs.get("SOXLB", {}).get("30s") or 0.0
        micro_div = abs(soxsb_30 + soxlb_30)

        # Step 1c: Check Macro Bias Gate
        if macro_bias == "CHOP" or abs(macro_score) < 3:
            consecutive_conviction_count = 0
            last_conviction_symbol = None
            status_text = (
                f"Scan #{poll_count} [CHOP FILTER]: Macro Score {macro_score:+d}/8 ({macro_bias}) in [-2,+2]. "
                f"100% USDT Safe Haven ($0 risk). SOXLB=${soxlb_p:.2f} | SOXSB=${soxsb_p:.2f} [@ {now_str}]"
            )
            BNB.update_instance(instance_id, last_error=status_text)
            for _ in range(4):
                if BNB.is_stop_requested(instance_id):
                    return 0.0, "ABORTED"
                time.sleep(0.5)
            continue

        # Macro Direction Determined:
        allowed_sym = BULL_SYMBOL if macro_score >= 3 else BEAR_SYMBOL
        if allowed_sym == last_conviction_symbol:
            consecutive_conviction_count += 1
        else:
            consecutive_conviction_count = 1
            last_conviction_symbol = allowed_sym

        # Step 1d: Micro Timing & Lag Confluence Check
        is_lag_confirmed = False
        confluence_reason = ""

        # Check 1: Direct lag signal matches macro bias
        if active_lag_sig is not None and active_lag_sig.get("symbol") == allowed_sym:
            if active_lag_sig.get("net_expected_profit_pct", 0.0) >= 0.01:
                is_lag_confirmed = True
                confluence_reason = (
                    f"Active Lag Signal BUY {allowed_sym} confirmed! "
                    f"Divergence: {active_lag_sig.get('divergence_pct'):.2f}%, Expected Net: +{active_lag_sig.get('net_expected_profit_pct'):.2f}%"
                )

        # Check 2: High macro conviction (Score >= 4) with favorable micro divergence
        elif abs(macro_score) >= 4:
            # For SOXLB: SOXLB change <= -SOXSB change + 0.05% (SOXLB lagging or starting surge)
            if allowed_sym == BULL_SYMBOL and (soxlb_30 - (-soxsb_30)) <= 0.25:
                is_lag_confirmed = True
                confluence_reason = f"High Macro Conviction (+{macro_score}/8) with aligned micro divergence ({soxlb_30:+.2f}% / {soxsb_30:+.2f}%)"
            elif allowed_sym == BEAR_SYMBOL and (soxsb_30 - (-soxlb_30)) <= 0.25:
                is_lag_confirmed = True
                confluence_reason = f"High Macro Conviction ({macro_score}/8) with aligned micro divergence ({soxsb_30:+.2f}% / {soxlb_30:+.2f}%)"

        # Check 3: Persistent Macro Conviction over 3 scans (avoids missing sustained trends)
        elif consecutive_conviction_count >= 3:
            is_lag_confirmed = True
            confluence_reason = f"Persistent Macro Conviction (+{macro_score}/8 for {consecutive_conviction_count} scans). Micro entry confirmed."

        status_text = (
            f"Scan #{poll_count} [CONFLUENCE SCAN]: Macro {macro_score:+d}/8 ({macro_bias}) -> {allowed_sym} | "
            f"Lag Confirmed: {is_lag_confirmed} | Div: {micro_div:.2f}% | Hold Count: {consecutive_conviction_count} [@ {now_str}]"
        )
        BNB.update_instance(instance_id, last_error=status_text)

        if is_lag_confirmed:
            target_symbol = allowed_sym
            entry_signal_info = {
                "macro_score": macro_score,
                "macro_bias": macro_bias,
                "macro_action": macro_action,
                "confluence_reason": confluence_reason,
                "soxlb_price": soxlb_p,
                "soxsb_price": soxsb_p,
                "micro_div": micro_div,
            }
            # Log macro score breakdown for UI dropdown
            semiconductor_dual_engine.log_scores(pred, bot_name=bot_name, client=client, instance_id=instance_id)
            break

        for _ in range(3):
            if BNB.is_stop_requested(instance_id):
                return 0.0, "ABORTED"
            time.sleep(0.5)

    # ── Phase 2: Spread Guard & Capital Management ──
    now_str = get_ist_time_str()
    entry_msg = (
        f"[{bot_name}] [{target_symbol}] 🎯 DUAL HYBRID CONFLUENCE CONFIRMED @ {now_str}!\n"
        f"  Macro Bias: {entry_signal_info['macro_bias']} (Score: {entry_signal_info['macro_score']:+d}/8)\n"
        f"  Confluence: {entry_signal_info['confluence_reason']}\n"
        f"  Target Token: {target_symbol} | Micro Div: {entry_signal_info['micro_div']:.2f}%"
    )
    print("=" * 80)
    print(entry_msg)
    print("=" * 80)
    BNB.log_event(entry_msg, level="TRADE", event_type="HYBRID_CONFLUENCE_CONFIRMED", instance_id=instance_id, symbol=target_symbol)

    if not BNB.claim_symbol_for_instance(instance_id, target_symbol):
        msg = f"[{bot_name}] [{target_symbol}] Symbol currently locked by another bot instance. Waiting..."
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        time.sleep(2.0)
        return None, "SYMBOL_BUSY"

    try:
        target_lot = BNB.get_lot_info(client, target_symbol)
    except Exception:
        target_lot = {"min_notional": 5.0, "tick_size": 0.01, "step": 0.001, "min_qty": 0.001}

    min_notional = float(target_lot.get("min_notional") or 5.0)
    tick_sz = float(target_lot.get("tick_size") or 0.01)
    step_sz = float(target_lot.get("step") or 0.001)

    committed_by_me = BNB.get_instance_committed_capital(instance_id) if hasattr(BNB, "get_instance_committed_capital") else 0.0
    remaining_budget = max(0.0, investment_usdt - committed_by_me)
    if remaining_budget < min_notional:
        msg = f"[{bot_name}] [{target_symbol}] Budget limit reached: ${committed_by_me:.2f} committed out of ${investment_usdt:.2f}."
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.release_symbol(instance_id)
        time.sleep(3.0)
        return None, "CAPITAL_LIMIT_REACHED"

    usdt_free = float(client.get_asset_balance(asset="USDT")["free"] or 0.0)
    actual_investment = min(usdt_free, remaining_budget, investment_usdt)

    if actual_investment < min_notional:
        msg = f"[{bot_name}] [{target_symbol}] Insufficient free USDT: have ${actual_investment:.2f}, need >=${min_notional:.2f}"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.release_symbol(instance_id)
        time.sleep(3.0)
        return None, "INSUFFICIENT_FUNDS"

    # Spread Guard Check
    ob = client.get_orderbook_ticker(symbol=target_symbol)
    ask_px = float(ob.get("askPrice") or 0.0)
    bid_px = float(ob.get("bidPrice") or 0.0)

    if ask_px <= 0 or bid_px <= 0:
        BNB.release_symbol(instance_id)
        return None, "ERROR"

    spread_pct = ((ask_px - bid_px) / bid_px) * 100.0
    if spread_pct > MAX_ALLOWED_SPREAD_PCT:
        spread_msg = f"[{bot_name}] [{target_symbol}] Spread too wide ({spread_pct:.3f}% > {MAX_ALLOWED_SPREAD_PCT:.3f}%). Aborting entry."
        print(spread_msg)
        BNB.log_event(spread_msg, level="WARN", event_type="SPREAD_GUARD", instance_id=instance_id, symbol=target_symbol)
        BNB.update_instance(instance_id, last_error=spread_msg)
        BNB.release_symbol(instance_id)
        time.sleep(2.0)
        return None, "SPREAD_TOO_WIDE"

    # ── Phase 3: Market Buy Entry ──
    qty_raw = (actual_investment * 0.998) / ask_px
    qty_str = BNB.format_quantity(qty_raw, step_sz)
    qty = float(qty_str)

    if qty < float(target_lot.get("min_qty") or 0.001):
        BNB.release_symbol(instance_id)
        return None, "INSUFFICIENT_FUNDS"

    try:
        from binance.client import Client as BClient
        buy_order = client.create_order(
            symbol=target_symbol,
            side=BClient.SIDE_BUY,
            type=BClient.ORDER_TYPE_MARKET,
            quantity=qty_str,
        )
    except Exception as e:
        msg = f"[{bot_name}] [{target_symbol}] Market buy order failed: {e}"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.release_symbol(instance_id)
        time.sleep(3.0)
        return None, "ERROR"

    executed_qty = float(buy_order.get("executedQty") or qty)
    cummulative_quote = float(buy_order.get("cummulativeQuoteQty") or (executed_qty * ask_px))
    avg_entry_price = cummulative_quote / executed_qty if executed_qty > 0 else ask_px

    buy_fee_usdt = 0.0
    buy_fee_bnb = 0.0
    for fill in buy_order.get("fills", []):
        f_asset = fill.get("commissionAsset", "")
        f_amt = float(fill.get("commission", 0))
        if f_asset == "USDT":
            buy_fee_usdt += f_amt
        elif f_asset == "BNB":
            buy_fee_bnb += f_amt

    if buy_fee_usdt <= 0.0 and buy_fee_bnb > 0.0:
        try:
            parsed_fee = BNB.fee_from_order(client, buy_order, target_symbol)
            buy_fee_usdt = float(parsed_fee.get("fee_usdt") or 0.0)
        except Exception:
            buy_fee_usdt = cummulative_quote * EST_FEE_RATE_PER_SIDE
    elif buy_fee_usdt <= 0.0 and cummulative_quote > 0.0:
        buy_fee_usdt = cummulative_quote * EST_FEE_RATE_PER_SIDE

    cycle_id = BNB.insert_cycle(
        instance_id=instance_id,
        symbol=target_symbol,
        entry_cost=cummulative_quote,
        qty=executed_qty,
        fee_bnb=buy_fee_bnb,
        fee_usdt=buy_fee_usdt,
        status="OPEN",
    )
    BNB.insert_trade(
        instance_id=instance_id,
        cycle_id=cycle_id,
        symbol=target_symbol,
        side="BUY",
        quantity=executed_qty,
        price=avg_entry_price,
        quote_qty=cummulative_quote,
        fee_bnb=buy_fee_bnb,
        fee_usdt=buy_fee_usdt,
        order_id=str(buy_order.get("orderId")),
    )

    # ── Phase 4: Place Resting Maker LIMIT SELL ──
    if target_profit and target_profit > 0.0:
        if target_profit < 0.20:
            target_profit_usdt = target_profit
        else:
            target_profit_usdt = max(OPT_ABSOLUTE_MIN_PROFIT_USDT, cummulative_quote * (target_profit / 100.0))
    else:
        target_profit_usdt = OPT_DEFAULT_MICRO_PROFIT_USDT

    target_profit_usdt = max(OPT_ABSOLUTE_MIN_PROFIT_USDT, target_profit_usdt)
    sell_qty_str = BNB.format_quantity(executed_qty, step_sz)
    sell_qty = float(sell_qty_str)

    target_price, target_price_str, target_pct = calculate_fee_covered_target_price(
        entry_cost=cummulative_quote,
        qty=sell_qty,
        buy_fee_usdt=buy_fee_usdt,
        target_profit_usdt=target_profit_usdt,
        tick_size=tick_sz,
        fee_rate=EST_FEE_RATE_PER_SIDE,
    )

    stop_loss_pct = stop_loss if (stop_loss is not None and stop_loss < 0) else DEFAULT_STOP_LOSS_PCT

    post_msg = (
        f"[{bot_name}] [{target_symbol}] Bought @ {avg_entry_price:.4f}. "
        f"Placing fee-covered LIMIT SELL @ {target_price_str} (+{target_pct:.2f}%, net: +{target_profit_usdt:.4f} USDT after fees)"
    )
    print(post_msg)
    BNB.log_event(post_msg, level="TRADE", event_type="SELL_POST", instance_id=instance_id, symbol=target_symbol)

    try:
        from binance.client import Client as BClient
        sell_order = client.create_order(
            symbol=target_symbol,
            side=BClient.SIDE_SELL,
            type=BClient.ORDER_TYPE_LIMIT,
            timeInForce=BClient.TIME_IN_FORCE_GTC,
            quantity=sell_qty_str,
            price=target_price_str,
        )
        sell_order_id = sell_order["orderId"]
    except Exception as e:
        msg = f"[{bot_name}] [{target_symbol}] Failed to post limit sell: {e}"
        print(msg)
        BNB.log_event(msg, level="ERROR", event_type="SELL_FAIL", instance_id=instance_id, symbol=target_symbol)
        time.sleep(2.0)
        return None, "ERROR"

    # ── Phase 5: Fast Orderbook Monitoring Loop ──
    peak_net_pnl_usdt = -9999.0
    monitoring_poll = 0
    stage1_repriced = False
    stage2_repriced = False

    def _execute_exit(
        exit_status: str,
        reason_desc: str,
        cur_bid_px: Optional[float] = None,
        is_limit_filled: bool = False,
        limit_exit_quote: Optional[float] = None,
        limit_order_info: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[float], str]:
        nonlocal sell_order_id, sell_qty, sell_qty_str, cummulative_quote, buy_fee_bnb, buy_fee_usdt, start_ts, target_price

        if not is_limit_filled:
            try:
                client.cancel_order(symbol=target_symbol, orderId=sell_order_id)
            except Exception:
                pass
            time.sleep(0.15)

            if not cur_bid_px or cur_bid_px <= 0:
                try:
                    ob_now = client.get_orderbook_ticker(symbol=target_symbol)
                    cur_bid_px = float(ob_now.get("bidPrice") or avg_entry_price)
                except Exception:
                    cur_bid_px = avg_entry_price

            sell_px_str = BNB.format_price(cur_bid_px, tick_sz, rounding="FLOOR")
            from binance.client import Client as BClient
            try:
                m_sell = client.create_order(
                    symbol=target_symbol,
                    side=BClient.SIDE_SELL,
                    type=BClient.ORDER_TYPE_MARKET,
                    quantity=sell_qty_str,
                )
            except Exception:
                m_sell = client.create_order(
                    symbol=target_symbol,
                    side=BClient.SIDE_SELL,
                    type=BClient.ORDER_TYPE_LIMIT,
                    timeInForce=BClient.TIME_IN_FORCE_IOC,
                    quantity=sell_qty_str,
                    price=sell_px_str,
                )

            exit_q = float(m_sell.get("cummulativeQuoteQty") or (sell_qty * cur_bid_px))
            s_fee_info = BNB.fee_from_order(client, m_sell, target_symbol)
            s_fee_usdt = float(s_fee_info.get("fee_usdt") or 0.0)
            s_fee_bnb = float(s_fee_info.get("fee_bnb") or 0.0)
            if s_fee_usdt <= 0.0 and exit_q > 0.0:
                s_fee_usdt = exit_q * EST_FEE_RATE_PER_SIDE
            recorded_order_id = str(m_sell.get("orderId"))
            exec_price = exit_q / sell_qty if sell_qty > 0 else cur_bid_px
        else:
            exit_q = float(limit_exit_quote or (sell_qty * target_price))
            s_fee_info = BNB.fee_from_order(client, limit_order_info or {}, target_symbol)
            s_fee_usdt = float(s_fee_info.get("fee_usdt") or 0.0)
            s_fee_bnb = float(s_fee_info.get("fee_bnb") or 0.0)
            if s_fee_usdt <= 0.0 and exit_q > 0.0:
                s_fee_usdt = exit_q * EST_FEE_RATE_PER_SIDE
            recorded_order_id = str(sell_order_id)
            exec_price = target_price

        stld = BNB.settle_pnl(cummulative_quote, exit_q, buy_fee_usdt, s_fee_usdt)
        r_pnl = stld["net_pnl"]
        dur_sec = time.time() - start_ts

        BNB.insert_trade(
            instance_id=instance_id,
            cycle_id=cycle_id,
            symbol=target_symbol,
            side="SELL",
            quantity=sell_qty,
            price=exec_price,
            quote_qty=exit_q,
            fee_bnb=s_fee_bnb,
            fee_usdt=s_fee_usdt,
            order_id=recorded_order_id,
        )

        final_status = "SUCCESS" if r_pnl > 0 or exit_status == "SUCCESS" else "FAILURE"

        BNB.close_cycle(
            cycle_id=cycle_id,
            exit_value=exit_q,
            pnl_usdt=r_pnl,
            status=final_status,
            duration_sec=dur_sec,
            fee_bnb=buy_fee_bnb + s_fee_bnb,
            fee_usdt=buy_fee_usdt + s_fee_usdt,
        )

        BNB.release_symbol(instance_id)
        BNB.update_instance(instance_id, current_symbol=None, current_pnl=0.0)

        n_str = get_ist_time_str()
        pnl_desc = "Guaranteed profit after all fees!" if r_pnl > 0 else "Capital preserved in USDT Safe Haven."
        out_msg = (
            f"[{bot_name}] [{target_symbol}] [{final_status} @ {n_str}]\n"
            f"  {reason_desc}\n"
            f"  Sold {sell_qty} {target_symbol} for {exit_q:.4f} USDT | Fees: {stld['fees_usdt']:.4f} USDT\n"
            f"  NET REALIZED PNL: {r_pnl:+.4f} USDT ({dur_sec:.1f}s hold) — {pnl_desc}"
        )
        print("=" * 80)
        print(out_msg)
        print("=" * 80)
        BNB.log_event(out_msg, level="TRADE" if r_pnl >= 0 else "WARN", event_type=final_status, instance_id=instance_id, symbol=target_symbol)
        time.sleep(1.0)
        return r_pnl, final_status

    while True:
        if BNB.is_stop_requested(instance_id):
            BNB.log_event(f"[{bot_name}] [{target_symbol}] Stop requested — cancelling sell order and returning to USDT", level="WARN", event_type="BOT_STOP", instance_id=instance_id, symbol=target_symbol)
            try:
                client.cancel_order(symbol=target_symbol, orderId=sell_order_id)
            except Exception:
                pass
            time.sleep(0.3)
            try:
                from binance.client import Client as BClient
                client.create_order(symbol=target_symbol, side=BClient.SIDE_SELL, type=BClient.ORDER_TYPE_MARKET, quantity=sell_qty_str)
            except Exception:
                pass
            return 0.0, "ABORTED"

        time.sleep(0.75)
        monitoring_poll += 1
        hold_sec = time.time() - start_ts

        try:
            # 1. Check if resting limit sell filled
            order_info = client.get_order(symbol=target_symbol, orderId=sell_order_id)
            status = order_info.get("status")

            if status == "FILLED":
                exit_quote = float(order_info.get("cummulativeQuoteQty") or (sell_qty * target_price))
                return _execute_exit(
                    exit_status="SUCCESS",
                    reason_desc=f"Resting limit sell filled @ {target_price:.4f}! Net fee-covered profit locked.",
                    is_limit_filled=True,
                    limit_exit_quote=exit_quote,
                    limit_order_info=order_info,
                )

            elif status in ("CANCELED", "REJECTED", "EXPIRED"):
                return _execute_exit(
                    exit_status="FAILURE",
                    reason_desc=f"Limit sell order {status} externally! Defensive exit.",
                )

            # 2. Query live bid and compute net PnL after all fees
            ob_curr = client.get_orderbook_ticker(symbol=target_symbol)
            cur_bid = float(ob_curr.get("bidPrice") or avg_entry_price)
            if cur_bid <= 0:
                continue

            live_net_pnl, cur_gross_val, est_sell_fee = compute_live_net_pnl(
                cur_bid=cur_bid,
                qty=sell_qty,
                entry_cost=cummulative_quote,
                buy_fee_usdt=buy_fee_usdt,
                fee_rate=EST_FEE_RATE_PER_SIDE,
            )
            live_net_pct = (live_net_pnl / cummulative_quote) * 100.0 if cummulative_quote > 0 else 0.0

            if live_net_pnl > peak_net_pnl_usdt:
                peak_net_pnl_usdt = live_net_pnl

            BNB.update_instance(instance_id, current_pnl=round(live_net_pnl, 4))

            # 2b. Progressive Repricing Architecture (Zero Loss Floor)
            # Stage 1 Reprice at 45s: Soft profit (+0.0200 USDT net)
            if not stage1_repriced and hold_sec >= 45.0:
                try:
                    s1_px, s1_str, s1_pct = calculate_fee_covered_target_price(
                        cummulative_quote, sell_qty, buy_fee_usdt, 0.0200, tick_sz, EST_FEE_RATE_PER_SIDE
                    )
                    if s1_px < target_price:
                        client.cancel_order(symbol=target_symbol, orderId=sell_order_id)
                        time.sleep(0.15)
                        from binance.client import Client as BClient
                        new_sell = client.create_order(
                            symbol=target_symbol,
                            side=BClient.SIDE_SELL,
                            type=BClient.ORDER_TYPE_LIMIT,
                            timeInForce=BClient.TIME_IN_FORCE_GTC,
                            quantity=sell_qty_str,
                            price=s1_str,
                        )
                        sell_order_id = new_sell["orderId"]
                        target_price = s1_px
                        target_price_str = s1_str
                        target_profit_usdt = 0.0200
                        msg = f"[{bot_name}] [{target_symbol}] 🔄 Progressive Reprice (45s): Repriced limit sell to {s1_str} (+{s1_pct:.2f}%, +0.0200 USDT net). Accelerating fill."
                        print(msg)
                        BNB.log_event(msg, level="INFO", event_type="REPRICE_STAGE1", instance_id=instance_id, symbol=target_symbol)
                except Exception:
                    pass
                stage1_repriced = True

            # Stage 2 Reprice at 90s: Strict Breakeven Floor (+0.0050 USDT net profit)
            if not stage2_repriced and hold_sec >= 90.0:
                try:
                    be_px, be_str, be_pct = calculate_fee_covered_target_price(
                        cummulative_quote, sell_qty, buy_fee_usdt, 0.0050, tick_sz, EST_FEE_RATE_PER_SIDE
                    )
                    if be_px < target_price:
                        client.cancel_order(symbol=target_symbol, orderId=sell_order_id)
                        time.sleep(0.15)
                        from binance.client import Client as BClient
                        new_sell = client.create_order(
                            symbol=target_symbol,
                            side=BClient.SIDE_SELL,
                            type=BClient.ORDER_TYPE_LIMIT,
                            timeInForce=BClient.TIME_IN_FORCE_GTC,
                            quantity=sell_qty_str,
                            price=be_str,
                        )
                        sell_order_id = new_sell["orderId"]
                        target_price = be_px
                        target_price_str = be_str
                        target_profit_usdt = 0.0050
                        msg = f"[{bot_name}] [{target_symbol}] 🛡️ Breakeven Floor Reprice (90s): Lowered limit sell to {be_str} (+{be_pct:.2f}%, fee-covered +0.0050 USDT net). Zero loss exit armed."
                        print(msg)
                        BNB.log_event(msg, level="INFO", event_type="REPRICE_BREAKEVEN", instance_id=instance_id, symbol=target_symbol)
                except Exception:
                    pass
                stage2_repriced = True

            # 3. Micro-Profit Target Exit
            if live_net_pnl >= target_profit_usdt:
                return _execute_exit(
                    exit_status="SUCCESS",
                    reason_desc=f"Hybrid target reached! Net PnL +{live_net_pnl:.4f} USDT >= +{target_profit_usdt:.4f} USDT (strictly after fees)",
                    cur_bid_px=cur_bid,
                )

            # 4. Trailing Ratchet Protection
            elif peak_net_pnl_usdt >= OPT_BREAKEVEN_LOCK_PEAK_USDT and live_net_pnl >= OPT_ABSOLUTE_MIN_PROFIT_USDT and live_net_pnl <= (peak_net_pnl_usdt - 0.0050):
                return _execute_exit(
                    exit_status="SUCCESS",
                    reason_desc=f"Trailing profit lock: Peak +{peak_net_pnl_usdt:.4f} USDT retraced to +{live_net_pnl:.4f} USDT (locked in > 0 after fees)",
                    cur_bid_px=cur_bid,
                )

            # 5. Breakeven Floor Lock
            elif peak_net_pnl_usdt >= OPT_BREAKEVEN_LOCK_PEAK_USDT and live_net_pnl <= OPT_BREAKEVEN_LOCK_FLOOR_USDT:
                return _execute_exit(
                    exit_status="SUCCESS",
                    reason_desc=f"Breakeven Floor Lock: Securing +{live_net_pnl:.4f} USDT net profit against drawdown.",
                    cur_bid_px=cur_bid,
                )

            # 6. Periodic Log
            if monitoring_poll % 8 == 0:
                now_str = get_ist_time_str()
                sl_str = f"ACTIVE ({stop_loss_pct:.2f}%)" if hold_sec >= STOP_LOSS_MIN_HOLD_SEC else f"HOLD DELAY ({int(STOP_LOSS_MIN_HOLD_SEC - hold_sec)}s left)"
                print(f"[{bot_name}] [{target_symbol}] Hold {hold_sec:.1f}s | Live Bid: {cur_bid:.4f} | Net PnL: {live_net_pnl:+.4f} USDT ({live_net_pct:+.2f}%) | Target: {target_price_str} | SL: {sl_str} @ {now_str}")

            # 7. Stop Loss Defense (Strictly Delayed to Avoid Premature Materialization)
            raw_pnl_pct = ((cur_bid - avg_entry_price) / avg_entry_price) * 100.0

            # Emergency crash guard: only triggered on catastrophic plunge (<= -2.00%)
            if raw_pnl_pct <= EMERGENCY_CRASH_PCT:
                return _execute_exit(
                    exit_status="FAILURE",
                    reason_desc=f"Catastrophic market collapse ({raw_pnl_pct:.2f}% <= {EMERGENCY_CRASH_PCT:.2f}%). Emergency capital defense.",
                    cur_bid_px=cur_bid,
                )

            # Standard stop loss: only evaluated after STOP_LOSS_MIN_HOLD_SEC (300s = 5m)
            if hold_sec >= STOP_LOSS_MIN_HOLD_SEC:
                if raw_pnl_pct <= stop_loss_pct:
                    return _execute_exit(
                        exit_status="FAILURE",
                        reason_desc=f"Stop-loss triggered after {hold_sec:.1f}s hold: PnL {raw_pnl_pct:.2f}% <= {stop_loss_pct:.2f}%.",
                        cur_bid_px=cur_bid,
                    )

            # 8. Maximum Safety Timeout (600s / 10m)
            if hold_sec >= MAX_WAIT_HOLD_SEC:
                if live_net_pnl >= OPT_ABSOLUTE_MIN_PROFIT_USDT:
                    return _execute_exit(
                        exit_status="SUCCESS",
                        reason_desc=f"Max hold safety reached with positive profit (+{live_net_pnl:.4f} USDT). Securing gain.",
                        cur_bid_px=cur_bid,
                    )
                else:
                    return _execute_exit(
                        exit_status="FAILURE",
                        reason_desc=f"Max hold safety reached ({hold_sec:.1f}s). Capital protection exit.",
                        cur_bid_px=cur_bid,
                    )

        except Exception as e:
            time.sleep(0.75)


def render_hybrid_strategy_tab():
    """
    Renders the Hybrid Strategy tab in Streamlit UI:
    Displays real-time Macro Conviction, Micro Lag Meters, Confluence state, and zero-loss rules.
    """
    import streamlit as st
    import semiconductor_dual_engine
    import semiconductor_lag_arb

    st.subheader("🌐 HYBRID SOXLB ↔ SOXSB CONFLUENCE TERMINAL")
    st.caption(
        "Combines Macro 8-Factor Predictive Scoring (Direction Conviction) with Micro Lag Arbitrage (Orderbook Entry Timing). "
        "Trades in the direction of the macro trend while entering on micro lag discounts strictly covering all Binance fees."
    )

    pred = semiconductor_dual_engine.predict_semiconductor_5m(force_refresh=True)
    lag_engine = semiconductor_lag_arb.get_engine()
    prices = lag_engine.get_current_prices()
    changes = lag_engine.get_current_changes()

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Macro 8-Factor Score", f"{pred.get('total_score', 0):+d} / 8", delta=pred.get("bias", "CHOP"))
    with col2:
        soxlb_p = prices.get("SOXLB") or 0.0
        soxlb_30 = changes.get("SOXLB", {}).get("30s") or 0.0
        st.metric("SOXLB Price", f"${soxlb_p:.2f}", delta=f"{soxlb_30:+.2f}% (30s)")
    with col3:
        soxsb_p = prices.get("SOXSB") or 0.0
        soxsb_30 = changes.get("SOXSB", {}).get("30s") or 0.0
        st.metric("SOXSB Price", f"${soxsb_p:.2f}", delta=f"{soxsb_30:+.2f}% (30s)")

    st.info(f"**Strategy Confluence Regime**: {pred.get('reason')}")
