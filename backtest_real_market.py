"""
backtest_real_market.py — Historical Evaluation of Shannon's Demon Volatility Harvester
on REAL Binance Market Prices for SOXLB/USDT and SOXSB/USDT (Zero Real Investment).
"""

import math
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple
import BNB


def run_real_market_backtest(
    interval: str = "5m",
    limit: int = 1000,
    initial_usdt: float = 1000.0,
    grid_threshold: float = 0.02,    # 2.0% move threshold
    sell_pct: float = 0.20,          # 20% of winner sold
    vault_pct: float = 0.20,         # 20% vaulted to USDT_locked, 80% to buy loser dip
    fee_pct: float = 0.00075,        # 0.075% Binance trading fee (with BNB discount)
    verbose: bool = True,
) -> Dict[str, Any]:
    client = BNB.make_client()
    klines_l = client.get_klines(symbol="SOXLBUSDT", interval=interval, limit=limit)
    klines_s = client.get_klines(symbol="SOXSBUSDT", interval=interval, limit=limit)

    map_l = {k[0]: float(k[4]) for k in klines_l}
    map_s = {k[0]: float(k[4]) for k in klines_s}

    common_ts = sorted(list(set(map_l.keys()) & set(map_s.keys())))
    if len(common_ts) < 10:
        return {}

    start_dt = datetime.fromtimestamp(common_ts[0] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    end_dt = datetime.fromtimestamp(common_ts[-1] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    # Initial prices
    p_L_prev = map_l[common_ts[0]]
    p_S_prev = map_s[common_ts[0]]

    # Initial allocation: 50/50 split
    half = initial_usdt / 2.0
    q_L = (half * (1.0 - fee_pct)) / p_L_prev
    q_S = (half * (1.0 - fee_pct)) / p_S_prev
    usdt_locked = 0.0
    total_fees_paid = (half * fee_pct * 2.0)

    cycles = 0
    trade_log = []

    for ts in common_ts[1:]:
        p_L = map_l[ts]
        p_S = map_s[ts]

        r_L = (p_L - p_L_prev) / p_L_prev
        r_S = (p_S - p_S_prev) / p_S_prev

        if r_L >= grid_threshold or r_S >= grid_threshold:
            cycles += 1
            v_L = q_L * p_L
            v_S = q_S * p_S

            if r_L > r_S:
                winner_sym = "SOXLB"
                winner_val = v_L
                loser_sym = "SOXSB"
                loser_px = p_S
                q_L_sold = q_L * sell_pct
                gross_sale = q_L_sold * p_L
                q_L -= q_L_sold
            else:
                winner_sym = "SOXSB"
                winner_val = v_S
                loser_sym = "SOXLB"
                loser_px = p_L
                q_S_sold = q_S * sell_pct
                gross_sale = q_S_sold * p_S
                q_S -= q_S_sold

            sell_fee = gross_sale * fee_pct
            net_sale = gross_sale - sell_fee
            total_fees_paid += sell_fee

            # Split proceeds
            vault_add = net_sale * vault_pct
            dip_buy_budget = net_sale * (1.0 - vault_pct)
            usdt_locked += vault_add

            # Execute dip buy on loser
            buy_fee = dip_buy_budget * fee_pct
            net_buy = dip_buy_budget - buy_fee
            total_fees_paid += buy_fee

            new_loser_qty = net_buy / loser_px
            if loser_sym == "SOXSB":
                q_S += new_loser_qty
            else:
                q_L += new_loser_qty

            dt_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime('%m-%d %H:%M')
            tot_val = (q_L * p_L) + (q_S * p_S) + usdt_locked
            trade_log.append({
                "time": dt_str,
                "winner": winner_sym,
                "r_L": r_L,
                "r_S": r_S,
                "vault_add": vault_add,
                "usdt_locked": usdt_locked,
                "total_val": tot_val,
            })

            p_L_prev = p_L
            p_S_prev = p_S

    p_L_final = map_l[common_ts[-1]]
    p_S_final = map_s[common_ts[-1]]
    final_v_L = q_L * p_L_final
    final_v_S = q_S * p_S_final
    final_tokens = final_v_L + final_v_S
    final_total = final_tokens + usdt_locked
    net_pnl = final_total - initial_usdt
    net_pnl_pct = (net_pnl / initial_usdt) * 100.0

    bench_v_L = (half / map_l[common_ts[0]]) * p_L_final
    bench_v_S = (half / map_s[common_ts[0]]) * p_S_final
    bench_total = bench_v_L + bench_v_S
    bench_pnl_pct = ((bench_total - initial_usdt) / initial_usdt) * 100.0

    res = {
        "cycles": cycles,
        "fees": total_fees_paid,
        "usdt_locked": usdt_locked,
        "final_tokens": final_tokens,
        "final_total": final_total,
        "net_pnl": net_pnl,
        "net_pnl_pct": net_pnl_pct,
        "bench_total": bench_total,
        "bench_pnl_pct": bench_pnl_pct,
        "alpha_pct": net_pnl_pct - bench_pnl_pct,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "hours": len(common_ts) * 5 / 60.0,
        "trade_log": trade_log,
    }

    if verbose:
        print("=" * 85)
        print(f" REAL BINANCE MARKET EVALUATION: SOXLB & SOXSB ({interval} candles)")
        print(f" Period: {start_dt} -> {end_dt} (~{res['hours']:.1f} hours)")
        print(f" Parameters: GRID={grid_threshold*100:.1f}% | SELL_PCT={sell_pct*100:.0f}% | VAULT_PCT={vault_pct*100:.0f}% | FEE={fee_pct*100:.3f}%")
        print("=" * 85)
        print(f"Initial Portfolio Capital          : ${initial_usdt:.2f} USDT")
        print(f"Total Rebalancing Cycles Executed  : {cycles}")
        print(f"Total Binance Fees Incurred        : ${total_fees_paid:.2f} USDT")
        print("-" * 85)
        print(f"Vaulted Cash (USDT_locked)         : ${usdt_locked:.2f} USDT (Risk-free cash locked in vault)")
        print(f"Active Tokens Value (V_L + V_S)    : ${final_tokens:.2f} USDT (SOXLB: ${final_v_L:.2f}, SOXSB: ${final_v_S:.2f})")
        print(f"Final Total Portfolio Value        : ${final_total:.2f} USDT")
        print(f"Net Strategy Return                : {net_pnl_pct:+.2f}% (${net_pnl:+.2f} USDT)")
        print("-" * 85)
        print(f"Benchmark Naive 50/50 Buy & Hold   : ${bench_total:.2f} USDT ({bench_pnl_pct:+.2f}%)")
        print(f"Strategy Alpha vs Buy & Hold       : {res['alpha_pct']:+.2f}%")
        print("=" * 85)

    return res


def run_parameter_sweep():
    print("=" * 105)
    print(" PARAMETER OPTIMIZATION SWEEP ON REAL BINANCE MARKET DATA (1000 x 5m Candles)")
    print("=" * 105)
    print(f"{'GRID %':<8} | {'SELL %':<8} | {'VAULT %':<8} | {'Cycles':<7} | {'Fees ($)':<9} | {'USDT Locked':<13} | {'Total Portfolio':<16} | {'Return %':<10} | {'Alpha vs Hold'}")
    print("-" * 105)

    grids = [0.015, 0.020, 0.025, 0.030]
    sells = [0.15, 0.20, 0.25]
    vaults = [0.15, 0.20, 0.30]

    for g in grids:
        for s in sells:
            for v in vaults:
                res = run_real_market_backtest(
                    grid_threshold=g,
                    sell_pct=s,
                    vault_pct=v,
                    verbose=False,
                )
                if res:
                    print(
                        f"{g*100:>5.1f}%  | {s*100:>5.0f}%  | {v*100:>5.0f}%  | "
                        f"{res['cycles']:<7} | ${res['fees']:<8.2f} | ${res['usdt_locked']:<12.2f} | "
                        f"${res['final_total']:<15.2f} | {res['net_pnl_pct']:>+7.2f}%   | {res['alpha_pct']:>+8.2f}%"
                    )
    print("=" * 105)


if __name__ == "__main__":
    run_parameter_sweep()
