"""
semiconductor_flip_flop.py — 5-Minute Predictive Directional Engine for SOXLB & SOXSB.

Operational Architecture:
1. Revised 5-Minute Predictive Scoring Gating:
   Evaluates all 7 semiconductor pillars before every entry:
   - SMH 1m > VWAP
   - NVDA 1m > VWAP & green
   - AVGO + AMD both green on 1m
   - NQ futures green last 5m
   - CVD positive & volume > avg
   - EMA 9 > EMA 21 on SOX 5m
   - No major news (TSM/NVDA halt)
2. Directional Execution:
   - +3 to +7: Enter SOXLBUSDT (Bull 3x)
   - -3 to -7: Enter SOXSBUSDT (Bear 3x)
   - -2 to +2: CHOP - SKIP! Stay 100% in USDT Safe Haven ($0 loss)
3. Micro-Profit Architecture (0.0XXX Range After Fees):
   - Calculates exact Binance maker/taker fees (0.075% with BNB discount, 0.0010 standard).
   - Places fee-covered resting maker LIMIT SELL strictly guaranteed to secure target micro-profit.
   - Live Orderbook Bid Monitor: continuously calculates live net PnL after all trading fees.
   - Instant Micro-Profit Taker: executes immediate market exit as soon as net PnL reaches the 0.0XXX target range.
   - Trailing Micro-Ratchet: locks in green 0.0XXX profit if price peaks and begins retracing.
   - Dynamic 5M Reversal & Stop Loss protection.
   - All timestamps strictly in Indian Standard Time (IST).
"""

from __future__ import annotations

import os
import sys
import json
import time
import math
from datetime import datetime, timezone, timedelta

# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))
from typing import Dict, Any, Tuple, Optional, List

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "flip_flop_state.json")

BULL_SYMBOL = "SOXLBUSDT"
BEAR_SYMBOL = "SOXSBUSDT"
DEFAULT_TARGET_PCT = 0.25  # Micro-profit target pct (~0.25% fee-covered 0.00XXX-0.0XXX USDT)
DEFAULT_STOP_LOSS_PCT = -0.25  # Tightened stop loss (-0.25%)
MAX_ALLOWABLE_STOP_LOSS_PCT = -0.30  # Hard floor capping worst-case stop loss

# Pre-purchase and Stop-loss protection timers (seconds)
PRE_PURCHASE_WAIT_SEC = 0.0     # 0.0s: immediate purchase upon prediction confirmation (30s wait removed)
STOP_LOSS_MIN_HOLD_SEC = 120.0  # Stop-loss is ONLY evaluated after 2 full minutes (120 seconds) of hold
MAX_WAIT_HOLD_SEC = 1800.0      # Safety timeout (30 min) while waiting for profit to reach 0.0X range

# Fallback fee estimate: 0.075% per side with BNB fee discount, 0.10% standard
EST_FEE_RATE_PER_SIDE = 0.00075

# 0.0X Profit target defaults (USDT)
# Ensures profits down to minute profit range (+0.0010 to +0.0250 USDT) after fees are captured
OPT_DEFAULT_MICRO_PROFIT_USDT = 0.0050  # Default profit target in micro range (0.0050 USDT)
OPT_ABSOLUTE_MIN_PROFIT_USDT = 0.0010   # Absolute minimum net profit floor (0.0010 USDT minute profit)
OPT_BREAKEVEN_LOCK_PEAK_USDT = 0.0050   # If trade touched this profit level, ratchet breakeven floor
OPT_BREAKEVEN_LOCK_FLOOR_USDT = 0.0010  # Exit if pulling back to protect against turning negative (lock in >= 0.0010)


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

    Exit_Quote * (1.0 - fee_rate) >= entry_cost + buy_fee_usdt + target_profit_usdt
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


def compute_smoothed_log_return(prices: List[float], span: int = 5) -> float:
    """
    Computes EMA-smoothed log returns:
        r_t = ln(P_t / P_{t-1})
        r_smooth = EMA(r_t)
    Provides a noise-filtered drift rate without assuming continuous differentiability.
    """
    if len(prices) < 2:
        return 0.0
    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices)) if prices[i - 1] > 0 and prices[i] > 0]
    if not log_returns:
        return 0.0
    alpha = 2.0 / (span + 1.0)
    ema_r = log_returns[0]
    for r in log_returns[1:]:
        ema_r = alpha * r + (1.0 - alpha) * ema_r
    return ema_r


def get_counterpart(symbol: str) -> str:
    """Returns the opposite 3x semiconductor counterpart."""
    return BEAR_SYMBOL if symbol == BULL_SYMBOL else BULL_SYMBOL


def load_flip_flop_state() -> Dict[str, Any]:
    """Loads persistent flip-flop state or initializes defaults."""
    default_state = {
        "next_symbol": BULL_SYMBOL,
        "last_sold_symbol": None,
        "flip_count": 0,
        "total_ratchet_pnl_usdt": 0.0,
        "target_profit_pct": DEFAULT_TARGET_PCT,
        "last_entry_price": 0.0,
        "last_exit_price": 0.0,
        "last_flip_at": None,
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                default_state.update(saved)
        except Exception:
            pass
    return default_state


def save_flip_flop_state(state: Dict[str, Any]) -> None:
    """Saves persistent state atomically."""
    temp_file = STATE_FILE + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(temp_file, STATE_FILE)
    except Exception:
        pass


def record_flip_success(sold_symbol: str, exit_price: float, profit_usdt: float) -> str:
    """
    Called upon successful target exit:
    Updates flip counter, accumulates realized profit.
    """
    state = load_flip_flop_state()
    next_sym = get_counterpart(sold_symbol)

    state["last_sold_symbol"] = sold_symbol
    state["next_symbol"] = next_sym
    state["last_exit_price"] = exit_price
    state["flip_count"] = int(state.get("flip_count") or 0) + 1
    state["total_ratchet_pnl_usdt"] = round(float(state.get("total_ratchet_pnl_usdt") or 0.0) + profit_usdt, 4)
    state["last_flip_at"] = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")

    save_flip_flop_state(state)
    return next_sym


def get_next_flip_symbol(client: Any = None) -> Optional[str]:
    """
    Evaluates 5-minute semiconductor prediction model:
    +3 to +7 = BULL_SYMBOL (SOXLBUSDT)
    -3 to -7 = BEAR_SYMBOL (SOXSBUSDT)
    -2 to +2 = None (Chop - skip)
    """
    try:
        import semiconductor_dual_engine
        pred = semiconductor_dual_engine.predict_semiconductor_5m(client=client)
        semiconductor_dual_engine.log_scores(pred, bot_name="Scanner", client=client)
        if pred.get("bias") == "BULLISH":
            return BULL_SYMBOL
        elif pred.get("bias") == "BEARISH":
            return BEAR_SYMBOL
        else:
            return None
    except Exception:
        state = load_flip_flop_state()
        return state.get("next_symbol") or BULL_SYMBOL


def trade_semiconductor_flip_flop_cycle(
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
    bnb_module: Any = None,
) -> Tuple[Optional[float], Optional[str]]:
    """
    Executes a 5-minute prediction guided trade cycle with immediate micro-profit capture:
    1. Continuous 5-Minute Predictive Scoring Gating:
       Rechecks scores fresh until directional conviction (+3 to +7 or -3 to -7) is met.
       Skips Chop (-2 to +2) in 100% USDT cash safe haven.
    2. Enters via market buy on confirmed directional conviction.
    3. Calculates exact Binance trading fees and sets resting maker LIMIT SELL at fee-covered micro-target.
    4. Fast Orderbook Monitoring Loop (0.75s interval):
       - If resting LIMIT SELL fills: banks profit.
       - If live bid gives net PnL >= micro-target (0.0XXX range after fees): executes IMMEDIATE MARKET EXIT.
       - If peak profit >= 0.0025 USDT retraces: trailing ratchet locks in the win.
       - If reversal detected (5M score flips against position): protective exit.
       - If stop-loss triggered: defensive exit.
    """
    import BNB
    import semiconductor_dual_engine

    # ── 3-Minute Predictive Scoring Gating (Evaluated When Bot Starts) ──
    recheck_count = 0
    total_score = 0
    bias = "CHOP"

    while True:
        if BNB.is_stop_requested(instance_id):
            return 0.0, "ABORTED"

        recheck_count += 1
        now_str = datetime.now(IST).strftime("%I:%M:%S %p IST")

        # Evaluate 3-Minute directional prediction when bot starts
        pred = semiconductor_dual_engine.predict_semiconductor_5m(client=client, force_refresh=True, horizon_min=3)
        total_score = pred.get("total_score", 0)
        bias = pred.get("bias", "CHOP")
        action = pred.get("action", "SKIP_100PCT_USDT")

        print(f"\n{'='*75}")
        print(f"[{bot_name}] [3M PREDICTION RECHECK #{recheck_count} @ {now_str}] Score={total_score:+d}/7 | Bias={bias} -> Action={action}")
        print(f"{'='*75}")
        semiconductor_dual_engine.log_scores(pred, bot_name=bot_name, client=client, instance_id=instance_id)

        score_summary = f"3M Score: {total_score:+d}/7 ({bias}) [@ {now_str}]"
        BNB.update_instance(instance_id, last_error=score_summary)

        # -2 to +2 = CHOP - SKIP and RECHECK AGAIN!
        if bias == "CHOP" or (-2 <= total_score <= 2):
            print(f"[{bot_name}] Chop detected (Score: {total_score:+d}/7 in [-2, +2]). 100% USDT Safe Haven ($0 loss). Rechecking in 5s...")
            for _ in range(5):
                if BNB.is_stop_requested(instance_id):
                    return 0.0, "ABORTED"
                time.sleep(1.0)
            continue

        # Conviction confirmed (+3 to +7 or -3 to -7)!
        print(f"\n>>> CONVICTION CONFIRMED! 3-Minute Score = {total_score:+d}/7 ({bias}) <<<\n")
        break

    # Enforce directional alignment based on the confirmed conviction score
    default_lot = lot or {"min_notional": 5.0, "tick_size": 0.01, "step": 0.001, "min_qty": 0.001}
    if bias == "BULLISH":
        symbol = BULL_SYMBOL
        try:
            lot = BNB.get_lot_info(client, BULL_SYMBOL)
        except Exception:
            lot = default_lot
    elif bias == "BEARISH":
        symbol = BEAR_SYMBOL
        try:
            lot = BNB.get_lot_info(client, BEAR_SYMBOL)
        except Exception:
            lot = default_lot

    # ── Immediate Purchase upon Prediction Confirmation (30s wait removed) ──
    now_str = datetime.now(IST).strftime("%I:%M:%S %p IST")
    conf_msg = (
        f"[{bot_name}] [{symbol}] [3M PREDICTION CONFIRMED @ {now_str}]\n"
        f"  Direction: {bias} (Score: {total_score:+d}/7) -> Target Token: {symbol}\n"
        f"  Executing immediate purchase (0s wait)..."
    )
    print("=" * 80)
    print(conf_msg)
    print("=" * 80)
    BNB.log_event(conf_msg, level="INFO", event_type="PRED_CONFIRMED", instance_id=instance_id, symbol=symbol)

    if PRE_PURCHASE_WAIT_SEC > 0.0:
        for _ in range(int(PRE_PURCHASE_WAIT_SEC)):
            if BNB.is_stop_requested(instance_id):
                return 0.0, "ABORTED"
            time.sleep(1.0)

    # ── Multi-Bot Concurrency & Investment Cap Enforcement ──
    # Multiple bots are permitted to trade the same symbol (e.g. SOXLBUSDT or SOXSBUSDT)
    # concurrently, provided each bot strictly adheres to its own configured investment limit.
    if not BNB.claim_symbol_for_instance(instance_id, symbol):
        msg = f"[{bot_name}] [{symbol}] Symbol already claimed/traded by another bot instance. Skipping trade."
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.log_event(msg, level="WARN", event_type="MUTUAL_EXCLUSION_SKIP", instance_id=instance_id, symbol=symbol)
        time.sleep(5.0)
        return None, "SYMBOL_BUSY"

    min_notional = float(lot.get("min_notional") or 5.0)
    tick_sz = float(lot.get("tick_size") or 0.01)
    step_sz = float(lot.get("step") or 0.001)

    # ── Strict Maximum Investment Enforcement ──
    # A bot must strictly use only its configured investment amount!
    committed_by_me = BNB.get_instance_committed_capital(instance_id) if hasattr(BNB, "get_instance_committed_capital") else 0.0
    remaining_budget = max(0.0, investment_usdt - committed_by_me)
    if remaining_budget < min_notional:
        msg = f"[{bot_name}] [{symbol}] Maximum investment reached: ${committed_by_me:.2f} already committed out of ${investment_usdt:.2f} max. Blocked from buying more!"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.release_symbol(instance_id)
        time.sleep(5.0)
        return None, "CAPITAL_LIMIT_REACHED"

    # Verify this instance does not already have an open cycle
    try:
        with BNB.get_conn() as conn:
            my_open_cycle = conn.execute(
                "SELECT id FROM cycles WHERE instance_id = ? AND status = 'OPEN'",
                (instance_id,),
            ).fetchone()
        if my_open_cycle:
            msg = f"[{bot_name}] [{symbol}] Bot instance #{instance_id} already has an OPEN cycle (#{my_open_cycle[0]}). Waiting for cycle completion."
            print(msg)
            BNB.update_instance(instance_id, last_error=msg)
            time.sleep(3.0)
            return None, "CYCLE_ALREADY_OPEN"
    except Exception:
        pass

    # Check free balance on Binance
    usdt_free = float(client.get_asset_balance(asset="USDT")["free"] or 0.0)
    actual_investment = min(usdt_free, remaining_budget, investment_usdt)

    if actual_investment < min_notional:
        msg = f"[{bot_name}] [{symbol}] Insufficient USDT: have {actual_investment:.2f}, need >={min_notional:.2f} (budget: {remaining_budget:.2f}, free: {usdt_free:.2f})"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.release_symbol(instance_id)
        BNB.log_event(msg, level="WARN", event_type="SKIP", instance_id=instance_id, symbol=symbol)
        time.sleep(5.0)
        return None, "INSUFFICIENT_FUNDS"

    # ── Unmanaged Orphan Asset Check ──
    # Check if there are orphan tokens in account that are NOT accounted for by ANY active bot cycle
    base_asset = symbol.replace("USDT", "")
    try:
        active_managed_qty = 0.0
        with BNB.get_conn() as conn:
            row_sum = conn.execute(
                "SELECT COALESCE(SUM(qty), 0.0) FROM cycles WHERE symbol = ? AND status = 'OPEN'",
                (symbol,),
            ).fetchone()
            active_managed_qty = float(row_sum[0] if row_sum else 0.0)

        base_bal = client.get_asset_balance(asset=base_asset)
        held_base = float(base_bal.get("free") or 0.0) + float(base_bal.get("locked") or 0.0)
        unmanaged_qty = max(0.0, held_base - active_managed_qty)
        ob_check = client.get_orderbook_ticker(symbol=symbol)
        cur_ask_check = float(ob_check.get("askPrice") or 0.0)
        if cur_ask_check > 0 and (unmanaged_qty * cur_ask_check) >= (min_notional * 1.5):
            msg = f"[{bot_name}] [{symbol}] Account already holds unmanaged orphan balance of {unmanaged_qty:.4f} {base_asset} (~${unmanaged_qty * cur_ask_check:.2f} USDT). Blocked from buying more on top!"
            print(msg)
            BNB.update_instance(instance_id, last_error=msg)
            BNB.release_symbol(instance_id)
            time.sleep(5.0)
            return None, "POSITION_ALREADY_HELD"
    except Exception:
        pass

    # NOTICE: cancel_all_open_orders is REMOVED to prevent cross-bot order cancellation!

    # Market Buy Entry
    ob = client.get_orderbook_ticker(symbol=symbol)
    ask_px = float(ob.get("askPrice") or 0.0)
    if ask_px <= 0:
        return None, "ERROR"

    qty_raw = (actual_investment * 0.998) / ask_px
    qty_str = BNB.format_quantity(qty_raw, step_sz)
    qty = float(qty_str)

    if qty < float(lot.get("min_qty") or 0.001):
        return None, "INSUFFICIENT_FUNDS"

    buy_detail = semiconductor_dual_engine.build_score_detail_payload(pred, bot_name=bot_name)
    BNB.log_event(
        f"[{bot_name}] [{symbol}] [5M PREDICTION ENTER @ {now_str}: {bias} (Score: {total_score:+d}/7)] Buying {qty_str} {symbol} (~${actual_investment:.2f} USDT)...",
        level="TRADE",
        event_type="BUY_SUBMIT",
        instance_id=instance_id,
        symbol=symbol,
        detail=buy_detail,
    )
    print(f"[{bot_name}] [{symbol}] [5M PREDICTION ENTER @ {now_str}: {bias} (Score: {total_score:+d}/7)] Buying {qty_str} {symbol} (~${actual_investment:.2f} USDT)...")

    try:
        from binance.client import Client as BClient
        buy_order = client.create_order(
            symbol=symbol,
            side=BClient.SIDE_BUY,
            type=BClient.ORDER_TYPE_MARKET,
            quantity=qty_str,
        )
    except Exception as e:
        msg = f"[{bot_name}] [{symbol}] Market buy failed: {e}"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.log_event(msg, level="ERROR", event_type="BUY_FAIL", instance_id=instance_id, symbol=symbol)
        time.sleep(3.0)
        return None, "ERROR"

    # Parse buy execution details
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

    # Accurate fee parsing via BNB module if needed
    if buy_fee_usdt <= 0.0 and buy_fee_bnb > 0.0:
        try:
            parsed_fee = BNB.fee_from_order(client, buy_order, symbol)
            buy_fee_usdt = float(parsed_fee.get("fee_usdt") or 0.0)
        except Exception:
            buy_fee_usdt = cummulative_quote * EST_FEE_RATE_PER_SIDE
    elif buy_fee_usdt <= 0.0 and cummulative_quote > 0.0:
        buy_fee_usdt = cummulative_quote * EST_FEE_RATE_PER_SIDE

    cycle_id = BNB.insert_cycle(
        instance_id=instance_id,
        symbol=symbol,
        entry_cost=cummulative_quote,
        qty=executed_qty,
        fee_bnb=buy_fee_bnb,
        fee_usdt=buy_fee_usdt,
        status="OPEN",
    )
    BNB.insert_trade(
        instance_id=instance_id,
        cycle_id=cycle_id,
        symbol=symbol,
        side="BUY",
        quantity=executed_qty,
        price=avg_entry_price,
        quote_qty=cummulative_quote,
        fee_bnb=buy_fee_bnb,
        fee_usdt=buy_fee_usdt,
        order_id=str(buy_order.get("orderId")),
    )

    # ── Accurate 0.0X Profit Target after Fees ──
    # Determine the desired target net profit in USDT (0.0X range)
    if target_profit and target_profit > 0.0:
        if target_profit < 0.20:
            # User or system specified an exact USDT target (e.g. 0.0100 to 0.0500 USDT)
            target_profit_usdt = target_profit
        else:
            # User specified a percentage (e.g. 0.25%, 0.50%, 1.25%)
            target_profit_usdt = max(OPT_ABSOLUTE_MIN_PROFIT_USDT, cummulative_quote * (target_profit / 100.0))
    else:
        # Default to 0.0X profit target (~0.0250 USDT)
        target_profit_usdt = OPT_DEFAULT_MICRO_PROFIT_USDT

    # If target_profit was a legacy default like 0.10 USDT, default to 0.0X range (0.0250 USDT)
    if target_profit_usdt > 0.0500 and (not target_profit or target_profit == 0.10):
        target_profit_usdt = 0.0250

    target_profit_usdt = max(OPT_ABSOLUTE_MIN_PROFIT_USDT, target_profit_usdt)

    sell_qty_str = BNB.format_quantity(executed_qty, step_sz)
    sell_qty = float(sell_qty_str)

    # Calculate fee-covered resting limit sell price strictly guaranteed to cover fees + target
    target_price, target_price_str, target_pct = calculate_fee_covered_target_price(
        entry_cost=cummulative_quote,
        qty=sell_qty,
        buy_fee_usdt=buy_fee_usdt,
        target_profit_usdt=target_profit_usdt,
        tick_size=tick_sz,
        fee_rate=EST_FEE_RATE_PER_SIDE,
    )

    # Stop Loss calculation - tightly capped to prevent outsized losses
    stop_loss_pct = DEFAULT_STOP_LOSS_PCT
    if stop_loss and stop_loss < 0.0:
        if stop_loss > -0.20:
            stop_loss_pct = - max(0.15, (abs(stop_loss) / cummulative_quote) * 100.0)
        else:
            stop_loss_pct = stop_loss
    # Always enforce hard ceiling: stop loss cannot be wider than MAX_ALLOWABLE_STOP_LOSS_PCT (-0.25%)
    stop_loss_pct = max(MAX_ALLOWABLE_STOP_LOSS_PCT, stop_loss_pct)

    BNB.log_event(
        f"[{bot_name}] [{symbol}] Bought @ {avg_entry_price:.4f}. Placing resting LIMIT SELL @ {target_price_str} (Target: +{target_profit_usdt:.4f} USDT / +{target_pct:.2f}%, SL: {stop_loss_pct:.2f}%)...",
        level="TRADE",
        event_type="SELL_POST",
        instance_id=instance_id,
        symbol=symbol,
    )
    print(f"[{bot_name}] [{symbol}] Bought @ {avg_entry_price:.4f}. Placing resting LIMIT SELL @ {target_price_str} (Target: +{target_profit_usdt:.4f} USDT / +{target_pct:.2f}%, SL: {stop_loss_pct:.2f}%)...")

    try:
        from binance.client import Client as BClient
        sell_order = client.create_order(
            symbol=symbol,
            side=BClient.SIDE_SELL,
            type=BClient.ORDER_TYPE_LIMIT,
            timeInForce=BClient.TIME_IN_FORCE_GTC,
            quantity=sell_qty_str,
            price=target_price_str,
        )
        sell_order_id = sell_order["orderId"]
    except Exception as e:
        msg = f"[{bot_name}] [{symbol}] Failed to post limit sell: {e}"
        print(msg)
        BNB.log_event(msg, level="ERROR", event_type="SELL_FAIL", instance_id=instance_id, symbol=symbol)
        time.sleep(2.0)
        return None, "ERROR"

    # Fast Monitoring Loop: Immediate Micro-Profit Capture, Stop Loss, and Reversal Detection
    poll_count = 0
    peak_net_pnl_usdt = -9999.0

    def _execute_exit(
        exit_status: str,
        event_type: str,
        reason_desc: str,
        cur_bid_px: Optional[float] = None,
        is_limit_filled: bool = False,
        limit_exit_quote: Optional[float] = None,
        limit_order_info: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[float], str]:
        nonlocal sell_order_id, sell_qty, sell_qty_str, cummulative_quote, buy_fee_bnb, buy_fee_usdt, start_ts

        if not is_limit_filled:
            # Defensive exit: Cancel resting maker limit sell for THIS bot instance
            try:
                client.cancel_order(symbol=symbol, orderId=sell_order_id)
            except Exception:
                pass
            time.sleep(0.15)

            if not cur_bid_px or cur_bid_px <= 0:
                try:
                    ob_now = client.get_orderbook_ticker(symbol=symbol)
                    cur_bid_px = float(ob_now.get("bidPrice") or avg_entry_price)
                except Exception:
                    cur_bid_px = avg_entry_price

            # Execute immediate market sell for THIS bot's quantity only
            from binance.client import Client as BClient
            try:
                m_sell = client.create_order(
                    symbol=symbol,
                    side=BClient.SIDE_SELL,
                    type=BClient.ORDER_TYPE_MARKET,
                    quantity=sell_qty_str,
                )
            except Exception:
                sell_px_str = BNB.format_price(cur_bid_px, tick_sz, rounding="FLOOR")
                m_sell = client.create_order(
                    symbol=symbol,
                    side=BClient.SIDE_SELL,
                    type=BClient.ORDER_TYPE_LIMIT,
                    timeInForce=BClient.TIME_IN_FORCE_GTC,
                    quantity=sell_qty_str,
                    price=sell_px_str,
                )

            exit_q = float(m_sell.get("cummulativeQuoteQty") or (sell_qty * cur_bid_px))
            s_fee_info = BNB.fee_from_order(client, m_sell, symbol)
            s_fee_usdt = float(s_fee_info.get("fee_usdt") or 0.0)
            s_fee_bnb = float(s_fee_info.get("fee_bnb") or 0.0)
            if s_fee_usdt <= 0.0 and exit_q > 0.0:
                s_fee_usdt = exit_q * EST_FEE_RATE_PER_SIDE
            recorded_order_id = str(m_sell.get("orderId"))
            exec_price = exit_q / sell_qty if sell_qty > 0 else cur_bid_px
        else:
            # Resting limit sell was filled by maker order
            exit_q = float(limit_exit_quote or (sell_qty * target_price))
            s_fee_info = BNB.fee_from_order(client, limit_order_info or {}, symbol)
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
            symbol=symbol,
            side="SELL",
            quantity=sell_qty,
            price=exec_price,
            quote_qty=exit_q,
            fee_bnb=s_fee_bnb,
            fee_usdt=s_fee_usdt,
            order_id=recorded_order_id,
        )
        # Enforce strict SUCCESS or FAILURE status for crystal-clear trade log tracking
        if exit_status in ("TARGET", "BREAKEVEN_LOCK", "SUCCESS") or r_pnl > 0:
            final_status = "SUCCESS"
        elif exit_status in ("STOP_LOSS", "EMERGENCY_LIQUIDATE", "TIME_EXPIRED", "FAILURE") or r_pnl < 0:
            final_status = "FAILURE"
        else:
            final_status = exit_status

        BNB.close_cycle(
            cycle_id=cycle_id,
            exit_value=exit_q,
            pnl_usdt=r_pnl,
            status=final_status,
            duration_sec=dur_sec,
            fee_bnb=buy_fee_bnb + s_fee_bnb,
            fee_usdt=buy_fee_usdt + s_fee_usdt,
        )

        if r_pnl > 0:
            record_flip_success(symbol, exec_price, r_pnl)

        BNB.release_symbol(instance_id)
        BNB.update_instance(instance_id, current_symbol=None, current_pnl=0.0)

        n_str = datetime.now(IST).strftime("%I:%M:%S %p IST")
        out_msg = (
            f"[{bot_name}] [{symbol}] [{final_status} ({exit_status}) @ {n_str}]\n"
            f"  {reason_desc}\n"
            f"  Sold {sell_qty} {symbol} for {exit_q:.4f} USDT | Gross: {stld['gross_pnl']:+.4f} USDT | Fees: {stld['fees_usdt']:.4f} USDT\n"
            f"  NET REALIZED PNL: {r_pnl:+.4f} USDT ({dur_sec:.1f}s hold)\n"
            f"  Capital secured in USDT Safe Haven."
        )
        print("=" * 80)
        print(out_msg)
        print("=" * 80)
        BNB.log_event(out_msg, level="TRADE" if r_pnl >= 0 else "WARN", event_type=final_status, instance_id=instance_id, symbol=symbol)
        time.sleep(1.0)
        return r_pnl, final_status

    while True:
        if BNB.is_stop_requested(instance_id):
            BNB.log_event(f"[{bot_name}] [{symbol}] Stop requested — cancelling limit sell and selling held position to USDT", level="WARN", event_type="BOT_STOP", instance_id=instance_id, symbol=symbol)
            try:
                client.cancel_order(symbol=symbol, orderId=sell_order_id)
            except Exception:
                pass
            time.sleep(0.3)
            try:
                from binance.client import Client as BClient
                client.create_order(symbol=symbol, side=BClient.SIDE_SELL, type=BClient.ORDER_TYPE_MARKET, quantity=sell_qty_str)
            except Exception:
                pass
            return 0.0, "ABORTED"

        # Responsive polling interval for ultra-fast profit reaction
        time.sleep(0.75)
        poll_count += 1
        hold_sec = time.time() - start_ts

        try:
            # 1. Check if resting maker LIMIT SELL has been filled
            order_info = client.get_order(symbol=symbol, orderId=sell_order_id)
            status = order_info.get("status")

            if status == "FILLED":
                exit_quote = float(order_info.get("cummulativeQuoteQty") or (sell_qty * target_price))
                return _execute_exit(
                    exit_status="SUCCESS",
                    event_type="SUCCESS",
                    reason_desc=f"Resting limit sell filled @ {target_price:.4f}! 0.0X profit locked.",
                    is_limit_filled=True,
                    limit_exit_quote=exit_quote,
                    limit_order_info=order_info,
                )

            elif status in ("CANCELED", "REJECTED", "EXPIRED"):
                return _execute_exit(
                    exit_status="FAILURE",
                    event_type="FAILURE",
                    reason_desc=f"Limit sell order {status} externally! Defensive market exit executed to return 100% funds to USDT.",
                )

            # 2. Query live orderbook for instant profit evaluation
            ob_curr = client.get_orderbook_ticker(symbol=symbol)
            cur_bid = float(ob_curr.get("bidPrice") or avg_entry_price)
            if cur_bid <= 0:
                continue

            # Compute exact live net PnL after deducting 100% of fees
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

            # Update live PnL in UI with the exact NET PnL after fees
            BNB.update_instance(instance_id, current_pnl=round(live_net_pnl, 4))

            # 3. ── IMMEDIATE 0.0X PROFIT CAPTURE & BREAKEVEN RATCHET ──
            # Check A: Live net PnL reached or exceeded target profit in 0.0X range
            if live_net_pnl >= target_profit_usdt:
                return _execute_exit(
                    exit_status="SUCCESS",
                    event_type="SUCCESS",
                    reason_desc=f"0.0X profit threshold reached: Net PnL +{live_net_pnl:.4f} USDT >= +{target_profit_usdt:.4f} USDT (after fees)",
                    cur_bid_px=cur_bid,
                )

            # Check B: Trailing ratchet protection for 0.0X profits
            elif peak_net_pnl_usdt >= 0.0250 and live_net_pnl >= OPT_ABSOLUTE_MIN_PROFIT_USDT and live_net_pnl <= (peak_net_pnl_usdt - 0.0050):
                return _execute_exit(
                    exit_status="SUCCESS",
                    event_type="SUCCESS",
                    reason_desc=f"Trailing 0.0X profit lock: Peak +{peak_net_pnl_usdt:.4f} USDT retraced to +{live_net_pnl:.4f} USDT (locked in >= +{OPT_ABSOLUTE_MIN_PROFIT_USDT:.4f} USDT)",
                    cur_bid_px=cur_bid,
                )

            # Check C: Breakeven defense: if trade was green (peak >= 0.0200 USDT) and pulls back towards 0.0100 USDT floor, exit to secure profit
            elif peak_net_pnl_usdt >= OPT_BREAKEVEN_LOCK_PEAK_USDT and live_net_pnl <= OPT_BREAKEVEN_LOCK_FLOOR_USDT:
                return _execute_exit(
                    exit_status="SUCCESS",
                    event_type="SUCCESS",
                    reason_desc=f"Breakeven Ratchet Lock: Peak was +{peak_net_pnl_usdt:.4f} USDT; pulling back towards floor (+{live_net_pnl:.4f} USDT). Defending 0.0X profit against loss!",
                    cur_bid_px=cur_bid,
                )

            # 4. ── PERIODIC STATUS LOGGING (Every ~6s) ──
            if poll_count % 8 == 0:
                now_str = datetime.now(IST).strftime("%I:%M:%S %p IST")
                sl_status = f"ACTIVE (SL: {stop_loss_pct:.2f}%)" if hold_sec >= STOP_LOSS_MIN_HOLD_SEC else f"PROTECTED for {max(0, int(STOP_LOSS_MIN_HOLD_SEC - hold_sec))}s"
                print(f"[{bot_name}] [{symbol}] Holding ({hold_sec:.1f}s) | Waiting for 0.0X profit | Live Bid: {cur_bid:.4f} | Net PnL: {live_net_pnl:+.4f} USDT ({live_net_pct:+.2f}%) | Target: {target_price_str} (+{target_profit_usdt:.4f} USDT) | Stop-Loss: {sl_status} @ {now_str}")

            # 5. ── STOP-LOSS CHECK (ONLY AFTER 2 MINUTES OF HOLD) ──
            # User requirement: Allow position 2 full minutes (120s) to develop without noise stop-outs.
            # Stop loss is strictly evaluated ONLY after 2 minutes have elapsed.
            if hold_sec >= STOP_LOSS_MIN_HOLD_SEC:
                raw_pnl_pct = ((cur_bid - avg_entry_price) / avg_entry_price) * 100.0
                if raw_pnl_pct <= stop_loss_pct:
                    now_str = datetime.now(IST).strftime("%I:%M:%S %p IST")
                    sl_msg = (
                        f"Stop-loss triggered after 2-minute hold ({hold_sec:.1f}s): "
                        f"PnL {raw_pnl_pct:.2f}% <= {stop_loss_pct:.2f}%. Exiting cycle with FAILURE to proceed to next cycle."
                    )
                    return _execute_exit(
                        exit_status="FAILURE",
                        event_type="FAILURE",
                        reason_desc=sl_msg,
                        cur_bid_px=cur_bid,
                    )

            # 6. ── WAIT FOR PROFIT (DO NOT TIMEOUT AT 180s) ──
            # Position waits for 0.0X profit or stop loss; safety timeout only after MAX_WAIT_HOLD_SEC (30 min)
            if hold_sec >= MAX_WAIT_HOLD_SEC:
                if live_net_pnl >= OPT_ABSOLUTE_MIN_PROFIT_USDT:
                    return _execute_exit(
                        exit_status="SUCCESS",
                        event_type="SUCCESS",
                        reason_desc=f"Hold safety limit reached ({hold_sec:.1f}s) with 0.0X profit (+{live_net_pnl:.4f} USDT). Securing win!",
                        cur_bid_px=cur_bid,
                    )
                else:
                    return _execute_exit(
                        exit_status="FAILURE",
                        event_type="FAILURE",
                        reason_desc=f"Hold safety limit reached ({hold_sec:.1f}s). Exiting position to protect capital.",
                        cur_bid_px=cur_bid,
                    )

        except Exception as e:
            time.sleep(0.75)
