import re
import sqlite3

# 1. Update scalper.db defaults
conn = sqlite3.connect("scalper.db")
c = conn.cursor()
c.execute("UPDATE manager_config SET stop_loss = -0.05 WHERE id = 1")
c.execute("UPDATE bot_instances SET stop_loss = -0.05")
conn.commit()
conn.close()
print("Updated scalper.db stop_loss to -0.05")

# 2. Update BNB.py
with open("BNB.py", "rb") as f:
    raw = f.read()

text = raw.decode("utf-8")
is_crlf = "\r\n" in text
norm_text = text.replace("\r\n", "\n")

# A. Update hard_stop_triggered
old_hard_stop = '''def hard_stop_triggered(net_pnl: float, investment_usdt: float, configured_sl: float, elapsed_sec: float = 0.0) -> bool:
    """
    Zero-Loss Spot Invariant Engine:
    Spot assets have zero liquidation risk and zero maintenance/borrowing cost.
    Selling below break-even creates permanent, self-inflicted losses.
    Therefore, negative market exits and time-based cuts are permanently disabled.
    Every trade must exit at or above Break-Even (+0.0010 USDT minimum net profit).
    """
    return False'''

new_hard_stop = '''def hard_stop_triggered(net_pnl: float, investment_usdt: float, configured_sl: float, elapsed_sec: float = 0.0) -> bool:
    """
    Micro-Stop Loss Ceiling (0.05 USDT):
    If net loss reaches 0.05 USDT (net_pnl <= -0.05), trigger exit immediately
    so the bot liquidates and rotates to the next coin, preventing multi-dollar drawdowns.
    """
    threshold = -0.05
    if configured_sl and configured_sl < 0:
        threshold = max(configured_sl, -0.05)
    return net_pnl <= threshold'''

assert old_hard_stop in norm_text, "old_hard_stop not found!"
norm_text = norm_text.replace(old_hard_stop, new_hard_stop)

# B. Add calculate_coin_investment_size function right above auto_select_cycle_strategy
sizing_func = '''def calculate_coin_investment_size(
    symbol: str,
    lot: dict,
    profile: dict,
    max_investment: float = 25.0,
    usdt_free: float = 100.0,
) -> float:
    """
    Coin-Adaptive Investment Sizer:
    Instead of rigidly deploying $25 on every coin, dynamically sizes each coin:
    - High Volatility / Breakouts / Meme coins: $5.0 - $7.0 USDT
    - Medium Volatility / Trends / Momentum: $10.0 - $12.0 USDT
    - Ultra-Liquid Majors (SOL, BTC, ETH): $15.0 - $25.0 USDT (up to max configured cap)
    Ensures minimum exchange notional is respected and never exceeds user max cap.
    """
    min_notional = float(lot.get("min_notional") or 5.0)
    base_floor = max(5.0, min_notional * 1.05)

    vol_1m = float(profile.get("volatility_1m") or 0.25)
    pred_mkt = str(profile.get("predicted_market") or "")

    meme_keywords = ("PEPE", "SHIB", "DOGE", "BONK", "FLOKI", "WIF", "MEME", "BOME", "1000")
    is_meme_or_volatile = vol_1m >= 0.35 or "Breakouts" in pred_mkt or any(m in symbol for m in meme_keywords)

    if is_meme_or_volatile:
        target_size = max(base_floor, 6.0)
    elif vol_1m >= 0.20 or "Trends" in pred_mkt:
        target_size = max(base_floor, 10.0)
    else:
        # Liquid / low-volatility majors
        target_size = max(base_floor, min(max_investment, 20.0))

    final_size = min(max_investment, target_size)
    final_size = min(final_size, usdt_free)
    return round(max(base_floor, final_size), 2)


'''

target_anchor = "def auto_select_cycle_strategy("
assert target_anchor in norm_text, "target_anchor not found!"
norm_text = norm_text.replace(target_anchor, sizing_func + target_anchor)

# C. Update trade_one_cycle to use calculate_coin_investment_size
old_prep_block = '''        start_ts = time.time()

        # Hybrid Market Regime Engine: Predict coin's market and select matching algorithm
        auto_strat, auto_analysis = auto_select_cycle_strategy(
            client, symbol, investment, cycles_done=cycles_done,
            forced_strategy=strategy if strategy not in ("AUTO", "", None) else None
        )
        strategy = auto_strat
        stop_loss = auto_analysis.get("dynamic_stop_loss", -0.15)'''

new_prep_block = '''        start_ts = time.time()

        # Hybrid Market Regime Engine: Predict coin's market and select matching algorithm
        auto_strat, auto_analysis = auto_select_cycle_strategy(
            client, symbol, investment_usdt, cycles_done=cycles_done,
            forced_strategy=strategy if strategy not in ("AUTO", "", None) else None
        )
        strategy = auto_strat

        # Dynamic Coin-Adaptive Investment Sizer: $5-$10 for volatile/memes, up to $25 max for majors
        investment = calculate_coin_investment_size(
            symbol=symbol,
            lot=lot,
            profile=auto_analysis,
            max_investment=investment_usdt,
            usdt_free=usdt_free,
        )
        stop_loss = -0.05  # Fixed micro stop-loss at 0.05 USDT'''

assert old_prep_block in norm_text, "old_prep_block not found!"
norm_text = norm_text.replace(old_prep_block, new_prep_block)

# D. Update Tranche allocation in trade_one_cycle_scale_in
old_tranches = '''    # Quantum-Maker: 40% Entry ($10 USDT) + 60% Dip Shock Absorber ($15 USDT)
    tranche1_inv = investment_usdt * 0.40
    tranche2_inv = investment_usdt * 0.60

    if tranche1_inv < min_notional:
        tranche1_inv = investment_usdt
        tranche2_inv = 0.0'''

new_tranches = '''    # Quantum-Maker: Adaptive Tranche Allocation
    if investment_usdt >= (min_notional * 2.1):
        tranche1_inv = max(min_notional * 1.05, investment_usdt * 0.40)
        tranche2_inv = investment_usdt - tranche1_inv
        if tranche2_inv < min_notional:
            tranche1_inv = investment_usdt
            tranche2_inv = 0.0
    else:
        # Smaller allocations ($5-$10): single tranche fast maker scalp
        tranche1_inv = investment_usdt
        tranche2_inv = 0.0'''

assert old_tranches in norm_text, "old_tranches not found!"
norm_text = norm_text.replace(old_tranches, new_tranches)

# E. Update Stop-Loss exit in trade_one_cycle_scale_in to liquidate immediately via Market Sell
old_sl_exit = '''                    if should_micro_scratch or hard_stop_triggered(cur_pnl, investment_usdt, stop_loss, elapsed):
                        if limit_buy2_id and not tranche2_filled:
                            cancel_order_safe(client, symbol, limit_buy2_id)
                        if active_sell_order_id:
                            cancel_order_safe(client, symbol, active_sell_order_id)
                        reason_tag = "STOP_LOSS" if cur_pnl <= stop_loss else "SCRATCH"
                        log_event(
                            f"[{bot_name}] [{symbol}] DCA {reason_tag} exit: est. NET pnl={cur_pnl:+.4f} (elapsed {elapsed:.1f}s) — market selling",
                            level="WARN",
                            event_type=reason_tag,
                            instance_id=instance_id,
                            symbol=symbol,
                        )
                        print(f"[{bot_name}] [{symbol}] DCA {reason_tag} exit: est. NET pnl={cur_pnl:+.4f} (elapsed {elapsed:.1f}s) — selling via Limit Sell @ bid")
                        sell_px = float(format_price(cur_bid, tick_sz, rounding="FLOOR"))
                        sell_res = limit_sell(client, symbol, current_held_qty, sell_px)'''

new_sl_exit = '''                    if cur_pnl <= -0.05 or should_micro_scratch or hard_stop_triggered(cur_pnl, investment_usdt, stop_loss, elapsed):
                        if limit_buy2_id and not tranche2_filled:
                            cancel_order_safe(client, symbol, limit_buy2_id)
                        if active_sell_order_id:
                            cancel_order_safe(client, symbol, active_sell_order_id)
                        reason_tag = "STOP_LOSS"
                        log_event(
                            f"[{bot_name}] [{symbol}] DCA 0.05 STOP_LOSS reached: est. NET pnl={cur_pnl:+.4f} (elapsed {elapsed:.1f}s) — liquidating immediately to rotate to next coin",
                            level="WARN",
                            event_type="STOP_LOSS",
                            instance_id=instance_id,
                            symbol=symbol,
                        )
                        print(f"[{bot_name}] [{symbol}] DCA 0.05 STOP_LOSS reached: est. NET pnl={cur_pnl:+.4f} (elapsed {elapsed:.1f}s) — liquidating immediately & rotating to next coin")
                        try:
                            # Immediate market execution
                            sell_res = client.create_order(
                                symbol=symbol,
                                side=Client.SIDE_SELL,
                                type=Client.ORDER_TYPE_MARKET,
                                quantity=format_quantity(current_held_qty, step_sz),
                                recvWindow=10000,
                                newOrderRespType="FULL",
                            )
                        except Exception:
                            sell_px = float(format_price(cur_bid, tick_sz, rounding="FLOOR"))
                            sell_res = limit_sell(client, symbol, current_held_qty, sell_px)'''

assert old_sl_exit in norm_text, "old_sl_exit not found!"
norm_text = norm_text.replace(old_sl_exit, new_sl_exit)

# F. Update UI min_value in page_launch
old_ui_min = 'investment = st.number_input("Investment (USDT)", min_value=10.0, max_value=500.0, value=default_inv, step=5.0)'
new_ui_min = 'investment = st.number_input("Max Investment per Coin (USDT)", min_value=5.0, max_value=500.0, value=default_inv, step=1.0)'
assert old_ui_min in norm_text, "old_ui_min not found!"
norm_text = norm_text.replace(old_ui_min, new_ui_min)

out_text = norm_text.replace("\n", "\r\n") if is_crlf else norm_text

with open("BNB.py", "wb") as f:
    f.write(out_text.encode("utf-8"))

print("Successfully applied 0.05 Stop-Loss & Coin-Adaptive Sizing to BNB.py!")
