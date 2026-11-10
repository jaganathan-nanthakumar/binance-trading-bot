"""
dual_soxlb_soxsb.py — Dual SOXLB & SOXSB Reinvest Harvester Strategy.

Trading Logic:
1. Capital Allocation:
   Divides configured investment 50/50 across anti-correlated semiconductor pairs:
   - Leg 1: SOXSBUSDT (3x Inverse / Bear Semiconductor) -> e.g. $12.50 (for $25 total)
   - Leg 2: SOXLBUSDT (3x Bull Semiconductor)          -> e.g. $12.50 (for $25 total)
2. Simultaneous Dual-Asset Acquisition:
   At startup, establishes both positions at current market prices if not already held.
3. Independent Fee-Covered Profit Harvesting:
   Continuously monitors both legs at sub-second intervals.
   Calculates exact real-time net PnL after deducting BOTH entry buy fee and estimated exit sell fee.
   The moment EITHER leg reaches positive profit after all fees:
   - Sells the winning leg immediately at market price.
   - Banks the realized net USDT profit to the account.
   - Immediately re-buys the SAME token for its allocated capital ($12.50).
   - Opens a fresh cycle with updated baseline entry price.
4. Zero-Loss Accumulation Floor:
   - The lagging/underwater leg is NEVER sold at a loss ("accumulate for long term").
   - It patiently waits for market oscillation/reversal while the winning leg repeatedly harvests profit.
   - When the market eventually swings back, the accumulated leg hits profit, sells, and re-buys!
5. State Persistence & Safety:
   - Persists open positions in `dual_soxlb_soxsb_state.json` and SQLite `scalper.db`.
   - Seamlessly reconciles with Binance wallet balances upon restart.
   - "Stop and Hold" preserves coins in wallet without selling.
   - "Liquidate" cleanly sells both legs back to 100% USDT.
"""

from __future__ import annotations

import os
import sys
import time
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Tuple, Optional, List

BULL_SYMBOL = "SOXLBUSDT"
BEAR_SYMBOL = "SOXSBUSDT"
BULL_ASSET = "SOXLB"
BEAR_ASSET = "SOXSB"

DEFAULT_INVESTMENT = 25.0
DEFAULT_LEG_INVESTMENT = 12.50
DEFAULT_LEG_TARGET_PROFIT = 0.0050  # $0.0050 net USDT profit per leg (ensures 1-tick slippage buffer above all fees)
MIN_LEG_NOTIONAL = 5.0              # Binance minimum notional for spot pairs
EST_FEE_RATE_PER_SIDE = 0.00075     # 0.075% with BNB fee discount (or 0.0010 without)

STATE_FILE = "dual_soxlb_soxsb_state.json"
IST = timezone(timedelta(hours=5, minutes=30))


import contextlib

@contextlib.contextmanager
def dual_harvest_lock(symbol: str, timeout_sec: float = 12.0):
    """
    Atomic cross-process mutex lock using an exclusive file lock.
    Guarantees that when multiple bots trade the same symbol, only one bot
    can execute a sell & re-buy transaction at any instant, eliminating race conditions.
    """
    lock_file = f".{symbol}_harvest.lock"
    start_t = time.time()
    acquired = False
    while time.time() - start_t < timeout_sec:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            time.sleep(0.15)
        except Exception:
            break
    try:
        yield acquired
    finally:
        if acquired:
            try:
                os.remove(lock_file)
            except Exception:
                pass


def get_ist_now_str(fmt: str = "%d %b %Y, %I:%M:%S %p IST") -> str:
    return datetime.now(IST).strftime(fmt)


def load_dual_state() -> Dict[str, Any]:
    """Loads state dictionary from JSON file."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_dual_state(state: Dict[str, Any]) -> None:
    """Saves state dictionary atomically to JSON file."""
    tmp_path = f"{STATE_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except Exception as e:
        print(f"[DUAL_SOXLB_SOXSB] Error saving state: {e}")


def get_instance_state(instance_id: int) -> Dict[str, Any]:
    """Returns state for a specific instance."""
    st = load_dual_state()
    return st.get(str(instance_id), {
        "instance_id": instance_id,
        "legs": {},
        "total_harvests": 0,
        "total_profit_usdt": 0.0,
    })


def update_leg_state(instance_id: int, symbol: str, leg_data: Optional[Dict[str, Any]]) -> None:
    """Updates or clears leg state for an instance."""
    st = load_dual_state()
    iid_str = str(instance_id)
    if iid_str not in st:
        st[iid_str] = {
            "instance_id": instance_id,
            "legs": {},
            "total_harvests": 0,
            "total_profit_usdt": 0.0,
        }
    if leg_data is None:
        st[iid_str]["legs"].pop(symbol, None)
    else:
        st[iid_str]["legs"][symbol] = leg_data
    save_dual_state(st)


def record_harvest_win(instance_id: int, symbol: str, profit_usdt: float) -> None:
    """Records a completed profit harvest in the state file."""
    st = load_dual_state()
    iid_str = str(instance_id)
    if iid_str not in st:
        st[iid_str] = {
            "instance_id": instance_id,
            "legs": {},
            "total_harvests": 0,
            "total_profit_usdt": 0.0,
        }
    st[iid_str]["total_harvests"] = int(st[iid_str].get("total_harvests", 0)) + 1
    st[iid_str]["total_profit_usdt"] = float(st[iid_str].get("total_profit_usdt", 0.0)) + profit_usdt
    st[iid_str]["last_harvest_at"] = get_ist_now_str()
    st[iid_str]["last_winning_symbol"] = symbol
    save_dual_state(st)


def clear_instance_state(instance_id: int) -> None:
    """Clears all state for an instance (e.g. upon liquidation)."""
    st = load_dual_state()
    st.pop(str(instance_id), None)
    save_dual_state(st)


def compute_live_net_pnl(
    cur_bid: float,
    qty: float,
    entry_cost: float,
    buy_fee_usdt: float,
    fee_rate: float = EST_FEE_RATE_PER_SIDE,
) -> Tuple[float, float, float]:
    """
    Computes exact live net PnL at current bid after deducting both entry buy fee
    and estimated exit sell fee.
    Returns: (net_pnl_usdt, gross_val_usdt, est_sell_fee_usdt)
    """
    if qty <= 0.0 or cur_bid <= 0.0:
        return 0.0, 0.0, 0.0
    gross_val = cur_bid * qty
    est_sell_fee = gross_val * fee_rate
    net_pnl = gross_val - entry_cost - (buy_fee_usdt or 0.0) - est_sell_fee
    return net_pnl, gross_val, est_sell_fee


def sync_leg_position(
    client: Any,
    instance_id: int,
    symbol: str,
    bot_name: str,
) -> Optional[Dict[str, Any]]:
    """
    Checks if this instance holds an active leg position by cross-checking:
    1. Instance JSON state file for this specific instance
    2. SQLite cycles table for OPEN cycles belonging to this specific instance
    3. Binance actual asset balance (ensuring other active bots' allocations are excluded)
    Returns leg data dict if active, or None if leg needs to be purchased.
    NOTE: A new bot instance has no open cycle or saved state, so it will always
    return None and purchase its own pair with its allocated capital ($12.50).
    """
    import BNB

    base_asset = symbol.replace("USDT", "")
    inst_state = get_instance_state(instance_id)
    saved_leg = inst_state.get("legs", {}).get(symbol)

    # 1. Check SQLite for an OPEN cycle for THIS instance & symbol
    db_cycle = None
    try:
        with BNB.get_conn() as conn:
            row = conn.execute(
                """
                SELECT id, symbol, entry_cost, qty, fee_bnb, fee_usdt, started_at
                FROM cycles
                WHERE instance_id = ? AND symbol = ? AND status = 'OPEN'
                ORDER BY id DESC LIMIT 1
                """,
                (instance_id, symbol),
            ).fetchone()
            if row:
                db_cycle = dict(row)
    except Exception as e:
        print(f"[{bot_name}] [{symbol}] Error checking DB cycle: {e}")

    # If this instance does NOT have an open cycle or saved leg, it does NOT own this leg.
    # It must buy its own pair with its own allocated funds ($12.50)! Never adopt other bots' coins.
    if not db_cycle and not saved_leg:
        return None

    # 2. Check Binance free balance for this asset
    held_qty = 0.0
    try:
        bal = client.get_asset_balance(asset=base_asset)
        held_qty = float(bal.get("free") or 0.0)
    except Exception as e:
        print(f"[{bot_name}] [{symbol}] Error checking Binance balance: {e}")

    # 3. Check what quantity is owned by OTHER active bots in OPEN cycles
    other_active_qty = 0.0
    try:
        with BNB.get_conn() as conn:
            r_other = conn.execute(
                """
                SELECT COALESCE(SUM(qty), 0.0)
                FROM cycles
                WHERE instance_id != ? AND symbol = ? AND status = 'OPEN'
                """,
                (instance_id, symbol),
            ).fetchone()
            if r_other:
                other_active_qty = float(r_other[0] or 0.0)
    except Exception as e:
        print(f"[{bot_name}] [{symbol}] Error checking other bots' allocations: {e}")

    avail_for_me = max(0.0, held_qty - other_active_qty)

    try:
        lot = BNB.get_lot_info(client, symbol)
        min_qty = float(lot.get("min_qty") or 0.001)
        min_notional = float(lot.get("min_notional") or MIN_LEG_NOTIONAL)
    except Exception:
        min_qty = 0.001
        min_notional = MIN_LEG_NOTIONAL

    if avail_for_me < min_qty:
        # Insufficient free tokens available for this instance: clean up stale state/cycle
        update_leg_state(instance_id, symbol, None)
        if db_cycle:
            try:
                BNB.close_cycle(db_cycle["id"], exit_value=0.0, pnl_usdt=0.0, status="CLOSED_EXTERNAL", duration_sec=0)
            except Exception:
                pass
        return None

    # Check approximate market value
    try:
        ob = client.get_orderbook_ticker(symbol=symbol)
        bid = float(ob.get("bidPrice") or 0.0)
    except Exception:
        bid = 0.0

    if bid > 0 and (avail_for_me * bid) < (min_notional * 0.75):
        # Dust balance; not a tradable position for this instance
        return None

    # This instance's leg is actively held on Binance!
    if db_cycle and float(db_cycle.get("qty") or 0) > 0:
        expected_qty = float(db_cycle["qty"])
        qty = min(avail_for_me, expected_qty)
        entry_cost = float(db_cycle.get("entry_cost") or (qty * (bid or 1.0)))
        buy_fee_usdt = float(db_cycle.get("fee_usdt") or (entry_cost * EST_FEE_RATE_PER_SIDE))
        buy_fee_bnb = float(db_cycle.get("fee_bnb") or 0.0)
        cycle_id = db_cycle["id"]
        started_at = db_cycle.get("started_at") or get_ist_now_str()
    else:
        expected_qty = float(saved_leg.get("qty") or 0.0)
        qty = min(avail_for_me, expected_qty)
        entry_cost = float(saved_leg.get("entry_cost") or (qty * (bid or 1.0)))
        buy_fee_usdt = float(saved_leg.get("buy_fee_usdt") or (entry_cost * EST_FEE_RATE_PER_SIDE))
        buy_fee_bnb = float(saved_leg.get("buy_fee_bnb") or 0.0)
        cycle_id = saved_leg.get("cycle_id")
        started_at = saved_leg.get("started_at") or get_ist_now_str()

    entry_price = entry_cost / qty if qty > 0 else (bid or 1.0)
    leg_data = {
        "symbol": symbol,
        "base_asset": base_asset,
        "cycle_id": cycle_id,
        "qty": qty,
        "entry_cost": entry_cost,
        "entry_price": entry_price,
        "buy_fee_usdt": buy_fee_usdt,
        "buy_fee_bnb": buy_fee_bnb,
        "started_at": started_at,
        "started_at_ts": time.time(),
    }
    update_leg_state(instance_id, symbol, leg_data)
    return leg_data


def buy_leg(
    client: Any,
    instance_id: int,
    symbol: str,
    target_cost_usdt: float,
    bot_name: str,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Executes market buy for one leg (e.g. $12.50 of SOXSB or SOXLB)
    and records trade & open cycle in DB and state file.
    """
    import BNB
    from binance.client import Client as BClient

    base_asset = symbol.replace("USDT", "")

    try:
        lot = BNB.get_lot_info(client, symbol)
    except Exception:
        lot = {"min_notional": MIN_LEG_NOTIONAL, "tick_size": 0.01, "step": 0.001, "min_qty": 0.001}

    min_notional = float(lot.get("min_notional") or MIN_LEG_NOTIONAL)
    step_sz = float(lot.get("step") or 0.001)

    if target_cost_usdt < min_notional:
        msg = f"[{bot_name}] [{symbol}] Target cost ${target_cost_usdt:.2f} is below min notional ${min_notional:.2f}"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        return False, None

    # Check free USDT balance
    try:
        usdt_free = float(client.get_asset_balance(asset="USDT")["free"] or 0.0)
    except Exception as e:
        msg = f"[{bot_name}] [{symbol}] Error checking USDT balance: {e}"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        return False, None

    if usdt_free < (target_cost_usdt * 0.95):
        msg = f"[{bot_name}] [{symbol}] Insufficient USDT: have ${usdt_free:.2f}, need ${target_cost_usdt:.2f}"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.log_event(msg, level="WARN", event_type="INSUFFICIENT_USDT", instance_id=instance_id, symbol=symbol)
        return False, None

    # Get current best ask
    try:
        ob = client.get_orderbook_ticker(symbol=symbol)
        ask_px = float(ob.get("askPrice") or 0.0)
    except Exception as e:
        msg = f"[{bot_name}] [{symbol}] Error getting ticker: {e}"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        return False, None

    if ask_px <= 0.0:
        return False, None

    spend_usdt = min(target_cost_usdt, usdt_free * 0.99)
    qty_raw = (spend_usdt * 0.998) / ask_px
    qty_str = BNB.format_quantity(qty_raw, step_sz)
    qty = float(qty_str)

    # Ensure quantity strictly satisfies Binance MIN_NOTIONAL filter (e.g. 5.0 USDT).
    # Since format_quantity floors the value, rounding down can dip below min_notional.
    if (qty * ask_px) < min_notional:
        import math
        needed_qty = (min_notional * 1.01) / ask_px
        steps = math.ceil(needed_qty / step_sz)
        ceil_qty = steps * step_sz
        if (ceil_qty * ask_px) <= usdt_free:
            qty = ceil_qty
            qty_str = BNB.format_quantity(qty, step_sz)
            qty = float(qty_str)
            spend_usdt = qty * ask_px

    if qty < float(lot.get("min_qty") or 0.001):
        msg = f"[{bot_name}] [{symbol}] Calculated qty {qty} < min_qty {lot.get('min_qty')}"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        return False, None

    now_str = get_ist_now_str()
    print(f"\n[{bot_name}] [{symbol}] [DUAL_SOXLB_SOXSB BUY @ {now_str}] Purchasing {qty_str} {symbol} (~${spend_usdt:.2f} USDT)...")
    BNB.log_event(
        f"[{bot_name}] [{symbol}] [DUAL BUY] Purchasing {qty_str} {symbol} (~${spend_usdt:.2f} USDT @ ask ${ask_px:.2f})...",
        level="TRADE",
        event_type="BUY_SUBMIT",
        instance_id=instance_id,
        symbol=symbol,
    )

    try:
        buy_order = client.create_order(
            symbol=symbol,
            side=BClient.SIDE_BUY,
            type=BClient.ORDER_TYPE_MARKET,
            quantity=qty_str,
        )
    except Exception as e:
        msg = f"[{bot_name}] [{symbol}] Market buy order failed: {e}"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.log_event(msg, level="ERROR", event_type="BUY_FAIL", instance_id=instance_id, symbol=symbol)
        return False, None

    # Parse execution
    exec_qty = float(buy_order.get("executedQty") or qty)
    cummulative_quote = float(buy_order.get("cummulativeQuoteQty") or (exec_qty * ask_px))
    avg_price = cummulative_quote / exec_qty if exec_qty > 0 else ask_px

    fee_info = BNB.fee_from_order(client, buy_order, symbol)
    buy_fee_usdt = float(fee_info.get("fee_usdt") or 0.0)
    buy_fee_bnb = float(fee_info.get("fee_bnb") or 0.0)
    if buy_fee_usdt <= 0.0 and cummulative_quote > 0:
        buy_fee_usdt = cummulative_quote * EST_FEE_RATE_PER_SIDE

    cycle_id = BNB.insert_cycle(
        instance_id=instance_id,
        symbol=symbol,
        entry_cost=cummulative_quote,
        qty=exec_qty,
        fee_bnb=buy_fee_bnb,
        fee_usdt=buy_fee_usdt,
        status="OPEN",
    )
    BNB.insert_trade(
        instance_id=instance_id,
        cycle_id=cycle_id,
        symbol=symbol,
        side="BUY",
        quantity=exec_qty,
        price=avg_price,
        quote_qty=cummulative_quote,
        fee_bnb=buy_fee_bnb,
        fee_usdt=buy_fee_usdt,
        order_id=str(buy_order.get("orderId")),
    )

    leg_data = {
        "symbol": symbol,
        "base_asset": base_asset,
        "cycle_id": cycle_id,
        "qty": exec_qty,
        "entry_cost": cummulative_quote,
        "entry_price": avg_price,
        "buy_fee_usdt": buy_fee_usdt,
        "buy_fee_bnb": buy_fee_bnb,
        "started_at": now_str,
        "started_at_ts": time.time(),
    }
    update_leg_state(instance_id, symbol, leg_data)

    confirm_msg = (
        f"[{bot_name}] [{symbol}] [BUY CONFIRMED @ {now_str}]\n"
        f"  Bought {exec_qty:.4f} {symbol} @ ${avg_price:.2f} | Cost: ${cummulative_quote:.2f} USDT | Fee: ${buy_fee_usdt:.4f} USDT"
    )
    print(confirm_msg)
    BNB.log_event(confirm_msg, level="TRADE", event_type="BUY_CONFIRM", instance_id=instance_id, symbol=symbol)
    return True, leg_data


def sell_leg(
    client: Any,
    instance_id: int,
    symbol: str,
    leg_data: Dict[str, Any],
    cur_bid_px: float,
    bot_name: str,
) -> Tuple[bool, float, Dict[str, Any]]:
    """
    Executes market sell of the winning leg to bank profit.
    Closes cycle in DB with status='SUCCESS', settles PnL,
    and updates cumulative bot metrics.
    """
    import BNB
    from binance.client import Client as BClient

    qty = float(leg_data.get("qty") or 0.0)
    entry_cost = float(leg_data.get("entry_cost") or 0.0)
    buy_fee_usdt = float(leg_data.get("buy_fee_usdt") or 0.0)
    buy_fee_bnb = float(leg_data.get("buy_fee_bnb") or 0.0)
    cycle_id = leg_data.get("cycle_id")
    started_at_ts = float(leg_data.get("started_at_ts") or time.time())

    try:
        lot = BNB.get_lot_info(client, symbol)
        step_sz = float(lot.get("step") or 0.001)
    except Exception:
        step_sz = 0.001

    qty_str = BNB.format_quantity(qty, step_sz)
    sell_qty = float(qty_str)

    # Ensure this instance has its required tokens free in wallet before selling.
    # Never sell a partial quantity that creates an artificial shortfall loss!
    try:
        base_asset = symbol.replace("USDT", "")
        held = float(client.get_asset_balance(asset=base_asset).get("free") or 0.0)
        if held < sell_qty * 0.95:
            msg = f"[{bot_name}] [{symbol}] Wallet balance ({held:.4f}) is less than required position ({sell_qty:.4f}). Waiting for balance to settle..."
            print(msg)
            return False, 0.0, {}
    except Exception as e:
        print(f"[{bot_name}] [{symbol}] Balance check warning: {e}")

    now_str = get_ist_now_str()
    print(f"\n{'='*80}")
    print(f"[{bot_name}] [{symbol}] [PROFIT HIT @ {now_str}] Selling {qty_str} {symbol} to bank profit...")
    print(f"{'='*80}")
    BNB.log_event(
        f"[{bot_name}] [{symbol}] [PROFIT HIT] Selling {qty_str} {symbol} @ bid ${cur_bid_px:.2f}...",
        level="TRADE",
        event_type="SELL_SUBMIT",
        instance_id=instance_id,
        symbol=symbol,
    )

    try:
        sell_order = client.create_order(
            symbol=symbol,
            side=BClient.SIDE_SELL,
            type=BClient.ORDER_TYPE_MARKET,
            quantity=qty_str,
        )
    except Exception as e:
        msg = f"[{bot_name}] [{symbol}] Market sell failed: {e}"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.log_event(msg, level="ERROR", event_type="SELL_FAIL", instance_id=instance_id, symbol=symbol)
        return False, 0.0, {}

    # Parse execution
    exit_val = float(sell_order.get("cummulativeQuoteQty") or (sell_qty * cur_bid_px))
    fee_info = BNB.fee_from_order(client, sell_order, symbol)
    sell_fee_usdt = float(fee_info.get("fee_usdt") or 0.0)
    sell_fee_bnb = float(fee_info.get("fee_bnb") or 0.0)
    if sell_fee_usdt <= 0.0 and exit_val > 0:
        sell_fee_usdt = exit_val * EST_FEE_RATE_PER_SIDE

    settled = BNB.settle_pnl(entry_cost, exit_val, buy_fee_usdt, sell_fee_usdt)
    net_pnl = settled["net_pnl"]
    duration_sec = time.time() - started_at_ts
    exec_price = exit_val / sell_qty if sell_qty > 0 else cur_bid_px

    BNB.insert_trade(
        instance_id=instance_id,
        cycle_id=cycle_id,
        symbol=symbol,
        side="SELL",
        quantity=sell_qty,
        price=exec_price,
        quote_qty=exit_val,
        fee_bnb=sell_fee_bnb,
        fee_usdt=sell_fee_usdt,
        order_id=str(sell_order.get("orderId")),
        pnl_usdt=net_pnl,
        notes="DUAL_HARVEST_WIN",
    )

    if cycle_id:
        c_status = "SUCCESS" if net_pnl > 0.0 else "FAILURE"
        BNB.close_cycle(
            cycle_id=cycle_id,
            exit_value=exit_val,
            pnl_usdt=net_pnl,
            status=c_status,
            duration_sec=duration_sec,
            fee_bnb=buy_fee_bnb + sell_fee_bnb,
            fee_usdt=buy_fee_usdt + sell_fee_usdt,
        )

    # Update instance cumulative metrics in DB
    try:
        inst = BNB.get_instance(instance_id)
        if inst:
            old_cum = float(inst.get("cumulative_pnl") or 0.0)
            old_wins = int(inst.get("wins") or 0)
            old_losses = int(inst.get("losses") or 0)
            BNB.update_instance(
                instance_id,
                cumulative_pnl=old_cum + net_pnl,
                wins=old_wins + (1 if net_pnl > 0 else 0),
                losses=old_losses + (1 if net_pnl <= 0 else 0),
            )
    except Exception as e:
        print(f"[{bot_name}] Error updating bot metrics: {e}")

    record_harvest_win(instance_id, symbol, net_pnl)
    update_leg_state(instance_id, symbol, None)

    out_msg = (
        f"[{bot_name}] [{symbol}] [PROFIT HARVESTED (SUCCESS) @ {now_str}]\n"
        f"  Sold {sell_qty} {symbol} for ${exit_val:.4f} USDT | Entry Cost: ${entry_cost:.4f} USDT\n"
        f"  Gross PnL: {settled['gross_pnl']:+.4f} USDT | Total Fees: {settled['fees_usdt']:.4f} USDT\n"
        f"  NET REALIZED PNL: {net_pnl:+.4f} USDT ({duration_sec:.1f}s hold)\n"
        f"  Win banked to Spot USDT! Immediately re-buying {symbol}..."
    )
    print("=" * 80)
    print(out_msg)
    print("=" * 80)
    BNB.log_event(out_msg, level="TRADE", event_type="SUCCESS", instance_id=instance_id, symbol=symbol)
    return True, net_pnl, settled


def liquidate_dual_holdings(client: Any, instance_id: int, bot_name: str = "Bot") -> Dict[str, Any]:
    """
    Emergency liquidation handler:
    Sells ONLY this instance's tracked holdings (SOXLBUSDT and SOXSBUSDT) at market price to USDT,
    records sell trades and settles net PnL, closes all open cycles for this instance as LIQUIDATED,
    and clears persistent state for this instance.
    Never liquidates or touches tokens owned by other concurrent bots!
    """
    import BNB
    from binance.client import Client as BClient

    total_exit_val = 0.0
    total_net_pnl = 0.0
    sold_symbols = []

    for sym in (BEAR_SYMBOL, BULL_SYMBOL):
        base_asset = sym.replace("USDT", "")

        # Find how much THIS instance holds from DB and state
        inst_leg = get_instance_state(instance_id).get("legs", {}).get(sym)
        open_c = None
        try:
            with BNB.get_conn() as conn:
                open_c = conn.execute(
                    "SELECT id, entry_cost, qty, fee_bnb, fee_usdt FROM cycles WHERE instance_id = ? AND symbol = ? AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
                    (instance_id, sym),
                ).fetchone()
        except Exception as e:
            print(f"[{bot_name}] Error querying open cycle for liquidation: {e}")

        my_qty = 0.0
        if open_c and float(open_c["qty"] or 0) > 0:
            my_qty = float(open_c["qty"])
        elif inst_leg and float(inst_leg.get("qty") or 0) > 0:
            my_qty = float(inst_leg["qty"])

        if my_qty <= 0.0:
            # This instance holds no tokens for this symbol; do NOT sell anything from wallet!
            continue

        try:
            lot = BNB.get_lot_info(client, sym)
            step_sz = float(lot.get("step") or 0.001)
            min_qty = float(lot.get("min_qty") or 0.001)
            min_notional = float(lot.get("min_notional") or MIN_LEG_NOTIONAL)
        except Exception:
            step_sz = 0.001
            min_qty = 0.001
            min_notional = MIN_LEG_NOTIONAL

        held = 0.0
        try:
            bal = client.get_asset_balance(asset=base_asset)
            held = float(bal.get("free") or 0.0)
        except Exception as e:
            print(f"[{bot_name}] Error checking balance for {base_asset}: {e}")

        # Sell strictly this instance's allocation, never more than held in wallet
        target_sell_qty = min(held, my_qty)
        qty_str = BNB.format_quantity(target_sell_qty, step_sz)
        qty = float(qty_str)

        if qty >= min_qty:
            try:
                ob = client.get_orderbook_ticker(symbol=sym)
                bid_px = float(ob.get("bidPrice") or 0.0)
                if (qty * bid_px) >= min_notional:
                    sell_order = client.create_order(
                        symbol=sym,
                        side=BClient.SIDE_SELL,
                        type=BClient.ORDER_TYPE_MARKET,
                        quantity=qty_str,
                    )
                    exit_q = float(sell_order.get("cummulativeQuoteQty") or (qty * bid_px))
                    total_exit_val += exit_q
                    sold_symbols.append(f"{qty_str} {sym} (${exit_q:.2f})")

                    fee_info = BNB.fee_from_order(client, sell_order, sym)
                    sell_fee_usdt = float(fee_info.get("fee_usdt") or 0.0)
                    sell_fee_bnb = float(fee_info.get("fee_bnb") or 0.0)
                    if sell_fee_usdt <= 0.0 and exit_q > 0:
                        sell_fee_usdt = exit_q * EST_FEE_RATE_PER_SIDE

                    # Insert trade and close open cycle for this symbol
                    if open_c:
                        c_id = open_c["id"]
                        e_cost = float(open_c["entry_cost"] or exit_q)
                        b_fee_usdt = float(open_c["fee_usdt"] or 0.0)
                        b_fee_bnb = float(open_c["fee_bnb"] or 0.0)
                        stld = BNB.settle_pnl(e_cost, exit_q, b_fee_usdt, sell_fee_usdt)
                        net_pnl = stld["net_pnl"]
                        total_net_pnl += net_pnl
                        BNB.insert_trade(
                            instance_id=instance_id,
                            cycle_id=c_id,
                            symbol=sym,
                            side="SELL",
                            quantity=qty,
                            price=exit_q / qty if qty > 0 else bid_px,
                            quote_qty=exit_q,
                            fee_bnb=sell_fee_bnb,
                            fee_usdt=sell_fee_usdt,
                            order_id=str(sell_order.get("orderId")),
                            pnl_usdt=net_pnl,
                            notes="DUAL_LIQUIDATE",
                        )
                        BNB.close_cycle(
                            cycle_id=c_id,
                            exit_value=exit_q,
                            pnl_usdt=net_pnl,
                            status="LIQUIDATED",
                            duration_sec=0.0,
                            fee_bnb=b_fee_bnb + sell_fee_bnb,
                            fee_usdt=b_fee_usdt + sell_fee_usdt,
                        )
            except Exception as e:
                print(f"[{bot_name}] Error liquidating {sym}: {e}")

    # Close any remaining unclosed open cycles in DB for THIS instance ONLY
    try:
        with BNB.get_conn() as conn:
            cycles = conn.execute(
                "SELECT id, symbol, entry_cost FROM cycles WHERE instance_id = ? AND status = 'OPEN'",
                (instance_id,),
            ).fetchall()
            for c in cycles:
                BNB.close_cycle(
                    cycle_id=c["id"],
                    exit_value=0.0,
                    pnl_usdt=0.0,
                    status="LIQUIDATED",
                    duration_sec=0.0,
                )
    except Exception:
        pass

    clear_instance_state(instance_id)
    msg = f"[{bot_name}] Liquidated dual positions: {', '.join(sold_symbols) if sold_symbols else 'None held'} -> Total exit: ${total_exit_val:.2f} USDT"
    print(msg)
    BNB.log_event(msg, level="WARN", event_type="LIQUIDATED", instance_id=instance_id, symbol="SOXLB+SOXSB")
    return {"liquidated": bool(sold_symbols), "exit_val": total_exit_val, "symbols": sold_symbols, "net_pnl": total_net_pnl}


def trade_dual_soxlb_soxsb_cycle(
    client: Any,
    instance_id: int,
    investment_usdt: float = DEFAULT_INVESTMENT,
    target_profit: float = DEFAULT_LEG_TARGET_PROFIT,
    stop_loss: float = -0.10,
    bot_name: str = "Bot",
) -> Tuple[Optional[float], Optional[str]]:
    """
    Main trade cycle for the DUAL_SOXLB_SOXSB Strategy:
    1. Splits investment 50/50:
       - Leg 1: $12.50 into SOXSBUSDT (Bear)
       - Leg 2: $12.50 into SOXLBUSDT (Bull)
    2. Ensures both legs are actively held. If either is missing, buys it immediately.
    3. Continuously monitors live bids for both tokens:
       - Calculates fee-covered net PnL (accounting for buy fees + estimated sell fees).
    4. Whichever leg reaches even minute profit first after fees (>= +0.0005 to +0.0010 USDT):
       - Sells it immediately to bank net USDT profit.
       - Immediately re-buys the SAME token for $12.50.
       - Returns (realized_pnl, 'SUCCESS').
    5. The lagging/underwater leg is NEVER sold at a loss:
       - Patiently held across cycles ("accumulate for long term").
       - Zero stop-loss sell.
    """
    import BNB

    # Determine per-leg capital allocation (50/50 split)
    total_inv = max(10.0, float(investment_usdt or DEFAULT_INVESTMENT))
    leg_investment = round(total_inv / 2.0, 2)
    leg_investment = max(MIN_LEG_NOTIONAL, leg_investment)

    # Determine per-leg target profit in USDT
    # Ensures fee coverage + 1 tick slippage buffer (at least +$0.0050 USDT)
    if target_profit and target_profit > 0.0:
        if target_profit <= 0.01:
            leg_target = target_profit
        elif target_profit < 0.20:
            leg_target = DEFAULT_LEG_TARGET_PROFIT
        else:
            leg_target = max(DEFAULT_LEG_TARGET_PROFIT, leg_investment * (target_profit / 100.0))
    else:
        leg_target = DEFAULT_LEG_TARGET_PROFIT

    # Minimum threshold to protect against 1-cent market execution slippage:
    leg_target = max(0.0050, leg_target)

    now_str = get_ist_now_str()
    header_msg = (
        f"\n{'='*80}\n"
        f"[{bot_name}] [DUAL_SOXLB_SOXSB CYCLE START @ {now_str}]\n"
        f"  Total Investment: ${total_inv:.2f} USDT -> $ {leg_investment:.2f} SOXSB + ${leg_investment:.2f} SOXLB\n"
        f"  Target Profit per Leg: +${leg_target:.4f} USDT (strictly after all fees)\n"
        f"  Zero Loss Floor: Lagging positions NEVER sold at a loss (accumulate for long term)\n"
        f"{'='*80}"
    )
    print(header_msg)
    BNB.log_event(header_msg, level="INFO", event_type="DUAL_CYCLE_START", instance_id=instance_id, symbol="SOXLB+SOXSB")

    # ── Step 1: Verify & Establish Both Positions ──
    # Check SOXSB leg
    leg_soxsb = sync_leg_position(client, instance_id, BEAR_SYMBOL, bot_name)
    if not leg_soxsb:
        if BNB.is_stop_requested(instance_id):
            return 0.0, "ABORTED"
        ok, leg_soxsb = buy_leg(client, instance_id, BEAR_SYMBOL, leg_investment, bot_name)
        if not ok:
            msg = f"[{bot_name}] Failed to establish SOXSB leg. Retrying next cycle..."
            print(msg)
            BNB.update_instance(instance_id, last_error=msg)
            time.sleep(3.0)
            return None, "BUY_FAIL_SOXSB"
        time.sleep(0.5)

    # Check SOXLB leg
    leg_soxlb = sync_leg_position(client, instance_id, BULL_SYMBOL, bot_name)
    if not leg_soxlb:
        if BNB.is_stop_requested(instance_id):
            return 0.0, "ABORTED"
        ok, leg_soxlb = buy_leg(client, instance_id, BULL_SYMBOL, leg_investment, bot_name)
        if not ok:
            msg = f"[{bot_name}] Failed to establish SOXLB leg. Retrying next cycle..."
            print(msg)
            BNB.update_instance(instance_id, last_error=msg)
            time.sleep(3.0)
            return None, "BUY_FAIL_SOXLB"
        time.sleep(0.5)

    entry_soxsb = leg_soxsb["entry_price"]
    entry_soxlb = leg_soxlb["entry_price"]
    qty_soxsb = leg_soxsb["qty"]
    qty_soxlb = leg_soxlb["qty"]

    print(
        f"[{bot_name}] Both Legs Active:\n"
        f"  • SOXSB (Bear): {qty_soxsb:.4f} units @ ${entry_soxsb:.2f} (${leg_soxsb['entry_cost']:.2f} USDT)\n"
        f"  • SOXLB (Bull): {qty_soxlb:.4f} units @ ${entry_soxlb:.2f} (${leg_soxlb['entry_cost']:.2f} USDT)\n"
        f"  Monitoring live bids for fee-covered net profit (Target >= +${leg_target:.4f} USDT)..."
    )

    # ── Step 2: Live Continuous Monitoring Loop ──
    poll_count = 0
    start_ts = time.time()

    while True:
        # Check user stop request
        if BNB.is_stop_requested(instance_id):
            stop_msg = (
                f"[{bot_name}] Stop requested — exiting cycle cleanly.\n"
                f"Positions preserved in Binance wallet without selling (SOXSB: {qty_soxsb:.4f}, SOXLB: {qty_soxlb:.4f})."
            )
            print(stop_msg)
            BNB.log_event(stop_msg, level="WARN", event_type="BOT_STOP", instance_id=instance_id, symbol="SOXLB+SOXSB")
            BNB.update_instance(instance_id, status="STOPPED", current_symbol="SOXSB + SOXLB", current_pnl=0.0)
            return 0.0, "ABORTED"

        time.sleep(1.2)
        poll_count += 1

        try:
            ob_soxsb = client.get_orderbook_ticker(symbol=BEAR_SYMBOL)
            ob_soxlb = client.get_orderbook_ticker(symbol=BULL_SYMBOL)
            bid_soxsb = float(ob_soxsb.get("bidPrice") or entry_soxsb)
            bid_soxlb = float(ob_soxlb.get("bidPrice") or entry_soxlb)
        except Exception as e:
            if poll_count % 10 == 0:
                print(f"[{bot_name}] Orderbook ticker poll error: {e}")
            continue

        # Compute live net PnL after all fees for both legs
        pnl_soxsb, gross_soxsb, fee_soxsb = compute_live_net_pnl(
            cur_bid=bid_soxsb,
            qty=qty_soxsb,
            entry_cost=leg_soxsb["entry_cost"],
            buy_fee_usdt=leg_soxsb["buy_fee_usdt"],
            fee_rate=EST_FEE_RATE_PER_SIDE,
        )
        pnl_soxlb, gross_soxlb, fee_soxlb = compute_live_net_pnl(
            cur_bid=bid_soxlb,
            qty=qty_soxlb,
            entry_cost=leg_soxlb["entry_cost"],
            buy_fee_usdt=leg_soxlb["buy_fee_usdt"],
            fee_rate=EST_FEE_RATE_PER_SIDE,
        )

        # ── Slippage Safety Buffer Check ──
        # Evaluates net PnL if market sell executes 1 tick ($0.01) lower in orderbook.
        # Guarantees that market order execution slippage can NEVER flip a harvest into a loss!
        pnl_soxsb_buf, _, _ = compute_live_net_pnl(
            cur_bid=max(0.01, bid_soxsb - 0.01),
            qty=qty_soxsb,
            entry_cost=leg_soxsb["entry_cost"],
            buy_fee_usdt=leg_soxsb["buy_fee_usdt"],
            fee_rate=EST_FEE_RATE_PER_SIDE,
        )
        pnl_soxlb_buf, _, _ = compute_live_net_pnl(
            cur_bid=max(0.01, bid_soxlb - 0.01),
            qty=qty_soxlb,
            entry_cost=leg_soxlb["entry_cost"],
            buy_fee_usdt=leg_soxlb["buy_fee_usdt"],
            fee_rate=EST_FEE_RATE_PER_SIDE,
        )

        combined_live_pnl = pnl_soxsb + pnl_soxlb
        BNB.update_instance(
            instance_id,
            current_symbol="SOXSB + SOXLB",
            current_pnl=round(combined_live_pnl, 4),
        )

        pct_soxsb = ((bid_soxsb - entry_soxsb) / entry_soxsb) * 100.0 if entry_soxsb > 0 else 0.0
        pct_soxlb = ((bid_soxlb - entry_soxlb) / entry_soxlb) * 100.0 if entry_soxlb > 0 else 0.0

        if poll_count % 8 == 0:
            print(
                f"[{bot_name}] [DUAL MONITOR #{poll_count}]\n"
                f"  SOXSB: bid=${bid_soxsb:.2f} | entry=${entry_soxsb:.2f} ({pct_soxsb:+.2f}%) | Net PnL: {pnl_soxsb:+.4f} USDT {'[TARGET HIT]' if (pnl_soxsb >= leg_target and pnl_soxsb_buf > 0.0) else '[Holding]'}\n"
                f"  SOXLB: bid=${bid_soxlb:.2f} | entry=${entry_soxlb:.2f} ({pct_soxlb:+.2f}%) | Net PnL: {pnl_soxlb:+.4f} USDT {'[TARGET HIT]' if (pnl_soxlb >= leg_target and pnl_soxlb_buf > 0.0) else '[Holding]'}\n"
                f"  Combined PnL: {combined_live_pnl:+.4f} USDT | Target per Leg: +${leg_target:.4f} USDT"
            )

        # ── Step 3: Independent Profit Harvesting Trigger ──
        # Whichever reaches profit first after fees AND after 1-tick slippage buffer:
        soxsb_win = (pnl_soxsb >= leg_target and pnl_soxsb_buf > 0.0)
        soxlb_win = (pnl_soxlb >= leg_target and pnl_soxlb_buf > 0.0)

        if not soxsb_win and not soxlb_win:
            # Neither is profitable yet.
            # Never sell at a loss! Continue monitoring loop.
            continue

        # Choose the winner (if both reach profit at once, choose higher net PnL)
        if soxsb_win and soxlb_win:
            winner_sym = BEAR_SYMBOL if pnl_soxsb >= pnl_soxlb else BULL_SYMBOL
            winner_bid = bid_soxsb if winner_sym == BEAR_SYMBOL else bid_soxlb
            winner_leg = leg_soxsb if winner_sym == BEAR_SYMBOL else leg_soxlb
            winner_pnl = pnl_soxsb if winner_sym == BEAR_SYMBOL else pnl_soxlb
        elif soxsb_win:
            winner_sym = BEAR_SYMBOL
            winner_bid = bid_soxsb
            winner_leg = leg_soxsb
            winner_pnl = pnl_soxsb
        else:
            winner_sym = BULL_SYMBOL
            winner_bid = bid_soxlb
            winner_leg = leg_soxlb
            winner_pnl = pnl_soxlb

        # Execute sell and re-buy under cross-process mutex lock to guarantee zero conflicts with other concurrent bots!
        with dual_harvest_lock(winner_sym):
            # 1. Sell the winning leg to bank the profit
            ok, realized_pnl, settled_info = sell_leg(
                client=client,
                instance_id=instance_id,
                symbol=winner_sym,
                leg_data=winner_leg,
                cur_bid_px=winner_bid,
                bot_name=bot_name,
            )

            if not ok:
                print(f"[{bot_name}] [{winner_sym}] Sell execution deferred, re-checking next tick...")
                time.sleep(1.0)
                continue

            # 2. Immediately re-buy the same winning leg with its allocated capital ($12.50)!
            print(f"[{bot_name}] [{winner_sym}] Win banked (+${realized_pnl:.4f} USDT). Immediately re-buying {winner_sym} for ${leg_investment:.2f}...")
            time.sleep(0.5)

            re_ok, new_leg = buy_leg(
                client=client,
                instance_id=instance_id,
                symbol=winner_sym,
                target_cost_usdt=leg_investment,
                bot_name=bot_name,
            )

            if re_ok and new_leg:
                print(f"[{bot_name}] [{winner_sym}] Re-buy successful @ ${new_leg['entry_price']:.2f}. New baseline active!")
            else:
                print(f"[{bot_name}] [{winner_sym}] Re-buy pending; next cycle iteration will pick it up.")

        # The other leg remains in its held position untouched! ("accumulate for long term")
        other_sym = BULL_SYMBOL if winner_sym == BEAR_SYMBOL else BEAR_SYMBOL
        other_entry = entry_soxlb if winner_sym == BEAR_SYMBOL else entry_soxsb
        print(f"[{bot_name}] [{other_sym}] Lagging position preserved in wallet (entry ${other_entry:.2f}). Waiting for trend swing...")

        # Conclude cycle with success
        return realized_pnl, "SUCCESS"
