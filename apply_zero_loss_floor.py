import re

with open("BNB.py", "rb") as f:
    raw = f.read()

encoding = "utf-8"
text = raw.decode(encoding)

# 1. Update hard_stop_triggered: strictly zero negative exits
old_hard_stop = '''def hard_stop_triggered(net_pnl: float, investment_usdt: float, configured_sl: float, elapsed_sec: float = 0.0) -> bool:
    """
    Quantum-Maker Sub-90-Second Fast-Scratch & Micro-Loss Engine:
    1. During first 45 seconds (elapsed_sec < 45.0s), stop loss is INACTIVE to allow trades to hit profit.
    2. Between 45s and 90s, engages and exits ONLY when loss is in 0.00XXX range (-0.0085 to 0.0).
    3. At 90.0 seconds (Max Hold Ceiling), exits cleanly via Limit Sell to prevent stuck trades.
    """
    if net_pnl >= 0.0:
        return False

    # 1. First 45 seconds: Give trade full room to hit profit target
    if elapsed_sec < 45.0:
        return False

    # 2. Between 45s and 90s: Quits when loss is in 0.00XXX range
    if -0.0085 <= net_pnl < 0.0:
        return True

    # 3. Fast Time-Decay Ceiling: If trade reaches 90s without hitting target, exit to prevent freezing
    if elapsed_sec >= 90.0:
        return True

    return False'''

new_hard_stop = '''def hard_stop_triggered(net_pnl: float, investment_usdt: float, configured_sl: float, elapsed_sec: float = 0.0) -> bool:
    """
    Zero-Loss Spot Invariant Engine:
    Spot assets have zero liquidation risk and zero maintenance/borrowing cost.
    Selling below break-even creates permanent, self-inflicted losses.
    Therefore, negative market exits and time-based cuts are permanently disabled.
    Every trade must exit at or above Break-Even (+0.0010 USDT minimum net profit).
    """
    return False'''

# 2. Update Progressive Repricing in trade_one_cycle_scale_in:
# Stage 1 (45s): Reprice to +0.0020 USDT net profit (soft profit)
# Stage 2 (90s): Reprice to +0.0010 USDT net profit (Strict Break-Even Floor with CEILING rounding)
# Never reprice below Break-Even Floor!
old_repricing_pattern = re.compile(
    r"# 2-Stage Maker Progressive Repricing:.*?(?=# Time-based 0\.00X recovery stop-loss & safety ceiling guard)",
    re.DOTALL
)

new_repricing = '''# 2-Stage Maker Progressive Repricing (Strict Zero-Loss Break-Even Floor):
                    reprice_sec_stage1 = 45.0   # Fast Soft-Target Reprice at 45 seconds
                    reprice_sec_stage2 = 90.0   # Fast Breakeven Reprice at 90 seconds
                    elapsed = time.time() - start_ts

                    # Stage 1: Reprice to soft profit (+0.0020 USDT net) at 45s
                    if (
                        active_sell_order_id
                        and not stage1_repriced
                        and elapsed >= reprice_sec_stage1
                        and current_held_qty > 0
                    ):
                        try:
                            stage1_exit = (current_total_cost + total_buy_fee_usdt + 0.0020) / (1.0 - EST_FEE_RATE_PER_SIDE)
                            stage1_limit_price = float(format_price(stage1_exit / current_held_qty, tick_sz, rounding="CEILING"))
                            if stage1_limit_price < active_target_price:
                                log_event(
                                    f"[{bot_name}] [{symbol}] Fast Reprice (45s): trade open {elapsed:.1f}s — repricing limit sell {active_target_price} -> {stage1_limit_price} (+0.0020 net profit target)",
                                    level="INFO",
                                    event_type="STAGE1_REPRICE",
                                    instance_id=instance_id,
                                    symbol=symbol,
                                )
                                print(f"[{bot_name}] [{symbol}] Fast Reprice (45s): Repricing limit sell {active_target_price} -> {stage1_limit_price} (+0.0020 net)")
                                cancel_order_safe(client, symbol, active_sell_order_id)
                                time.sleep(0.2)
                                new_sell = limit_sell(client, symbol, current_held_qty, stage1_limit_price)
                                active_sell_order_id = new_sell.get("orderId")
                                active_target_price = stage1_limit_price
                            stage1_repriced = True
                        except Exception:
                            stage1_repriced = True

                    # Stage 2: Reprice to Strict Break-Even Floor (+0.0010 USDT net) at 90s
                    if (
                        active_sell_order_id
                        and not stage2_repriced
                        and elapsed >= reprice_sec_stage2
                        and current_held_qty > 0
                    ):
                        try:
                            floor_exit = (current_total_cost + total_buy_fee_usdt + 0.0010) / (1.0 - EST_FEE_RATE_PER_SIDE)
                            floor_limit_price = float(format_price(floor_exit / current_held_qty, tick_sz, rounding="CEILING"))
                            if floor_limit_price < active_target_price:
                                log_event(
                                    f"[{bot_name}] [{symbol}] Strict Break-Even Floor (90s): Repricing limit sell {active_target_price} -> {floor_limit_price} (+0.0010 net profit floor)",
                                    level="INFO",
                                    event_type="STAGE2_REPRICE",
                                    instance_id=instance_id,
                                    symbol=symbol,
                                )
                                print(f"[{bot_name}] [{symbol}] Strict Break-Even Floor (90s): Repricing limit sell {active_target_price} -> {floor_limit_price} (+0.0010 floor)")
                                cancel_order_safe(client, symbol, active_sell_order_id)
                                time.sleep(0.2)
                                new_sell = limit_sell(client, symbol, current_held_qty, floor_limit_price)
                                active_sell_order_id = new_sell.get("orderId")
                                active_target_price = floor_limit_price
                            stage2_repriced = True
                        except Exception:
                            stage2_repriced = True

                    '''

# Normalize newlines for matching
is_crlf = "\r\n" in text
norm_text = text.replace("\r\n", "\n")

assert old_hard_stop in norm_text, "old_hard_stop not found!"
norm_text = norm_text.replace(old_hard_stop, new_hard_stop)

assert old_repricing_pattern.search(norm_text) is not None, "old_repricing_pattern not found!"
norm_text = old_repricing_pattern.sub(new_repricing, norm_text)

if is_crlf:
    out_text = norm_text.replace("\n", "\r\n")
else:
    out_text = norm_text

with open("BNB.py", "wb") as f:
    f.write(out_text.encode(encoding))

print("Successfully applied Strict Zero-Loss Floor & Progressive Break-Even Repricing to BNB.py!")
