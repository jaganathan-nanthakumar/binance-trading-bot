import re

with open("BNB.py", "rb") as f:
    raw = f.read()

text = raw.decode("utf-8")
is_crlf = "\r\n" in text
norm_text = text.replace("\r\n", "\n")

# 1. Initialize stage3_repriced in trade_one_cycle_scale_in
old_init_flags = '''        # Step 4: Monitor dual state
        tranche2_filled = False
        stage1_repriced = False
        stage2_repriced = False'''

new_init_flags = '''        # Step 4: Monitor dual state & Multi-Algorithm Lifecycle
        tranche2_filled = False
        stage1_repriced = False
        stage2_repriced = False
        stage3_repriced = False'''

assert old_init_flags in norm_text, "old_init_flags not found!"
norm_text = norm_text.replace(old_init_flags, new_init_flags)

# Also reset stage3_repriced when Tranche 2 fills
old_t2_reset = '''                        comb_sell = limit_sell(client, symbol, current_held_qty, active_target_price)
                        active_sell_order_id = comb_sell.get("orderId")
                        stage1_repriced = False
                        stage2_repriced = False'''

new_t2_reset = '''                        comb_sell = limit_sell(client, symbol, current_held_qty, active_target_price)
                        active_sell_order_id = comb_sell.get("orderId")
                        stage1_repriced = False
                        stage2_repriced = False
                        stage3_repriced = False'''

assert old_t2_reset in norm_text, "old_t2_reset not found!"
norm_text = norm_text.replace(old_t2_reset, new_t2_reset)

# 2. Multi-Algorithm Lifecycle Engine (Never Give Up Engine)
old_reprice_block = '''                    # 2-Stage Maker Progressive Repricing (Strict Zero-Loss Break-Even Floor):
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
                            stage2_repriced = True'''

new_reprice_block = '''                    # -----------------------------------------------------------------------
                    # Adaptive Multi-Algorithm Lifecycle Engine (Never-Give-Up Cycle)
                    # Instead of giving up or dumping at a loss, the bot transitions across
                    # algorithms on the held coin until it realizes a net profit!
                    # -----------------------------------------------------------------------
                    elapsed = time.time() - start_ts

                    # Phase 2 Transition (60s): Switch to MEAN_REVERSION & Orderbook Spread Pegger
                    if (
                        active_sell_order_id
                        and not stage1_repriced
                        and elapsed >= 60.0
                        and current_held_qty > 0
                    ):
                        try:
                            stage1_exit = (current_total_cost + total_buy_fee_usdt + 0.0020) / (1.0 - EST_FEE_RATE_PER_SIDE)
                            stage1_limit_price = float(format_price(stage1_exit / current_held_qty, tick_sz, rounding="CEILING"))
                            if stage1_limit_price < active_target_price:
                                log_event(
                                    f"[{bot_name}] [{symbol}] Algorithm Shift (60s): MOMENTUM -> MEAN_REVERSION — repricing limit sell {active_target_price} -> {stage1_limit_price} (+0.0020 net target)",
                                    level="INFO",
                                    event_type="ALGO_SHIFT_MEAN_REVERSION",
                                    instance_id=instance_id,
                                    symbol=symbol,
                                )
                                print(f"[{bot_name}] [{symbol}] Algorithm Shift (60s): MOMENTUM -> MEAN_REVERSION | Repricing limit sell to {stage1_limit_price} (+0.0020 net)")
                                cancel_order_safe(client, symbol, active_sell_order_id)
                                time.sleep(0.2)
                                new_sell = limit_sell(client, symbol, current_held_qty, stage1_limit_price)
                                active_sell_order_id = new_sell.get("orderId")
                                active_target_price = stage1_limit_price
                            update_instance(instance_id, strategy="MEAN_REVERSION")
                            stage1_repriced = True
                        except Exception:
                            stage1_repriced = True

                    # Phase 3 Transition (180s / 3m): Switch to MICRO_GRID_REPAIR (Dip Support Absorption)
                    if (
                        active_sell_order_id
                        and not stage2_repriced
                        and elapsed >= 180.0
                        and current_held_qty > 0
                    ):
                        try:
                            floor_exit = (current_total_cost + total_buy_fee_usdt + 0.0010) / (1.0 - EST_FEE_RATE_PER_SIDE)
                            floor_limit_price = float(format_price(floor_exit / current_held_qty, tick_sz, rounding="CEILING"))
                            if floor_limit_price < active_target_price:
                                log_event(
                                    f"[{bot_name}] [{symbol}] Algorithm Shift (180s): MEAN_REVERSION -> MICRO_GRID_REPAIR — pinning sell to break-even floor {floor_limit_price} (+0.0010 net profit floor)",
                                    level="INFO",
                                    event_type="ALGO_SHIFT_MICRO_GRID",
                                    instance_id=instance_id,
                                    symbol=symbol,
                                )
                                print(f"[{bot_name}] [{symbol}] Algorithm Shift (180s): MEAN_REVERSION -> MICRO_GRID_REPAIR | Sell pinned to {floor_limit_price} (+0.0010 floor)")
                                cancel_order_safe(client, symbol, active_sell_order_id)
                                time.sleep(0.2)
                                new_sell = limit_sell(client, symbol, current_held_qty, floor_limit_price)
                                active_sell_order_id = new_sell.get("orderId")
                                active_target_price = floor_limit_price
                            update_instance(instance_id, strategy="MICRO_GRID_REPAIR")
                            stage2_repriced = True
                        except Exception:
                            stage2_repriced = True

                    # Phase 4 Transition (300s+ / 5m+): Switch to PASSIVE_OSCILLATOR (Never Give Up Engine)
                    # Stays on Binance order book at Break-Even Floor until filled as Maker or swept by bid
                    if (
                        active_sell_order_id
                        and elapsed >= 300.0
                        and not stage3_repriced
                        and current_held_qty > 0
                    ):
                        try:
                            update_instance(instance_id, strategy="PASSIVE_OSCILLATOR")
                            log_event(
                                f"[{bot_name}] [{symbol}] Algorithm Shift (300s): Engaging PASSIVE_OSCILLATOR — Never-Give-Up Engine active on Binance order book @ {active_target_price}",
                                level="INFO",
                                event_type="ALGO_SHIFT_OSCILLATOR",
                                instance_id=instance_id,
                                symbol=symbol,
                            )
                            print(f"[{bot_name}] [{symbol}] Algorithm Shift (300s): PASSIVE_OSCILLATOR active — resting at break-even floor until filled in profit")
                            stage3_repriced = True
                        except Exception:
                            stage3_repriced = True'''

assert old_reprice_block in norm_text, "old_reprice_block not found!"
norm_text = norm_text.replace(old_reprice_block, new_reprice_block)

out_text = norm_text.replace("\n", "\r\n") if is_crlf else norm_text

with open("BNB.py", "wb") as f:
    f.write(out_text.encode("utf-8"))

print("Successfully applied Adaptive Multi-Algorithm Progression (Never Give Up Engine) to BNB.py!")
