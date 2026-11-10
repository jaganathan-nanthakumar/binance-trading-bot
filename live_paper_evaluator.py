"""
live_paper_evaluator.py — Real-Time Live Paper Evaluator for Shannon's Demon / Paired Volatility Harvester.
Zero Real Investment: Tracks live Binance Spot ticker prices for SOXLBUSDT & SOXSBUSDT.
"""

import time
import math
from datetime import datetime, timezone
import BNB


def run_live_paper_harvester(
    initial_usdt: float = 1000.0,
    grid_pct: float = 1.5,          # Rebalance when live move >= 1.5%
    sell_pct: float = 20.0,         # Sell 20% of the winning asset
    vault_pct: float = 25.0,        # 25% of sale to USDT_locked, 75% to buy loser dip
    fee_pct: float = 0.075,         # 0.075% Binance fee with BNB
    poll_sec: float = 3.0,
):
    client = BNB.make_client()
    grid_frac = grid_pct / 100.0
    sell_frac = sell_pct / 100.0
    vault_frac = vault_pct / 100.0
    fee_frac = fee_pct / 100.0

    print("=" * 95)
    print(" LIVE REAL-TIME PAPER TRADING EVALUATOR: SOXLB & SOXSB (ZERO REAL MONEY)")
    print(f" Initial Virtual USDT : ${initial_usdt:.2f} USDT")
    print(f" Grid Rebalance Target: {grid_pct:.2f}% | Sell Winner: {sell_pct:.0f}% | Vault Cash: {vault_pct:.0f}% | Fee: {fee_pct:.3f}%")
    print("=" * 95)

    # Initial prices
    ob_l = client.get_orderbook_ticker(symbol="SOXLBUSDT")
    ob_s = client.get_orderbook_ticker(symbol="SOXSBUSDT")
    p_L_ref = float(ob_l["bidPrice"])
    p_S_ref = float(ob_s["bidPrice"])

    half = initial_usdt / 2.0
    q_L = (half * (1.0 - fee_frac)) / p_L_ref
    q_S = (half * (1.0 - fee_frac)) / p_S_ref
    usdt_locked = 0.0
    total_fees = half * fee_frac * 2.0
    cycles = 0

    print(f"Initialized Paper Portfolio @ {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}:")
    print(f"  • SOXLB: {q_L:.4f} units @ ${p_L_ref:.2f} = ${q_L * p_L_ref:.2f}")
    print(f"  • SOXSB: {q_S:.4f} units @ ${p_S_ref:.2f} = ${q_S * p_S_ref:.2f}")
    print(f"  • USDT_locked Vault: ${usdt_locked:.2f}")
    print("-" * 95)
    print(f"{'Time UTC':<10} | {'SOXLB Bid':<10} | {'SOXSB Bid':<10} | {'dL %':<8} | {'dS %':<8} | {'USDT Locked':<13} | {'Total Portfolio':<16} | {'Status'}")
    print("-" * 95)

    try:
        while True:
            time.sleep(poll_sec)
            ob_l = client.get_orderbook_ticker(symbol="SOXLBUSDT")
            ob_s = client.get_orderbook_ticker(symbol="SOXSBUSDT")
            p_L = float(ob_l["bidPrice"])
            p_S = float(ob_s["bidPrice"])

            r_L = (p_L - p_L_ref) / p_L_ref
            r_S = (p_S - p_S_ref) / p_S_ref

            cur_v_L = q_L * p_L
            cur_v_S = q_S * p_S
            total_val = cur_v_L + cur_v_S + usdt_locked
            t_now = datetime.now(timezone.utc).strftime('%H:%M:%S')

            # Rebalance trigger
            if r_L >= grid_frac or r_S >= grid_frac:
                cycles += 1
                if r_L > r_S:
                    win_sym = "SOXLB"
                    win_val = cur_v_L
                    lose_sym = "SOXSB"
                    lose_px = p_S
                    q_sold = q_L * sell_frac
                    gross_sale = q_sold * p_L
                    q_L -= q_sold
                else:
                    win_sym = "SOXSB"
                    win_val = cur_v_S
                    lose_sym = "SOXLB"
                    lose_px = p_L
                    q_sold = q_S * sell_frac
                    gross_sale = q_sold * p_S
                    q_S -= q_sold

                s_fee = gross_sale * fee_frac
                net_sale = gross_sale - s_fee
                total_fees += s_fee

                vault_add = net_sale * vault_frac
                dip_budget = net_sale * (1.0 - vault_frac)
                usdt_locked += vault_add

                b_fee = dip_budget * fee_frac
                net_buy = dip_budget - b_fee
                total_fees += b_fee

                new_q = net_buy / lose_px
                if lose_sym == "SOXSB":
                    q_S += new_q
                else:
                    q_L += new_q

                tot_after = (q_L * p_L) + (q_S * p_S) + usdt_locked
                print(f"{t_now:<10} | ${p_L:<9.2f} | ${p_S:<9.2f} | {r_L:>+6.2%} | {r_S:>+6.2%} | ${usdt_locked:<12.2f} | ${tot_after:<15.2f} | [REBALANCE #{cycles}: {win_sym} -> {lose_sym} (+${vault_add:.2f} to Vault)]")

                # Reset reference price
                p_L_ref = p_L
                p_S_ref = p_S
            else:
                # Heartbeat every 10 ticks
                print(f"{t_now:<10} | ${p_L:<9.2f} | ${p_S:<9.2f} | {r_L:>+6.2%} | {r_S:>+6.2%} | ${usdt_locked:<12.2f} | ${total_val:<15.2f} | Tracking live...")

    except KeyboardInterrupt:
        print("\n" + "=" * 80)
        print("Live Paper Harvester Stopped cleanly.")
        print(f"Final Total Cycles: {cycles}")
        print(f"Final USDT_locked Vault: ${usdt_locked:.2f}")
        print(f"Final Total Portfolio Value: ${total_val:.2f} ({((total_val - initial_usdt)/initial_usdt)*100:+.2f}%)")
        print("=" * 80)


if __name__ == "__main__":
    run_live_paper_harvester(initial_usdt=1000.0, grid_pct=1.0, sell_pct=20.0, vault_pct=25.0, poll_sec=2.0)
