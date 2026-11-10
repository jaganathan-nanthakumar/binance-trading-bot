"""
ideal_paired_harvester.py — Shannon's Demon Volatility Harvester for Anti-Correlated Pairs.

Mathematical Formulation:
Idealized anti-correlated counterpart universe:
    P_L(t+1) = P_L(t) * (1 + r)
    P_S(t+1) = P_S(t) * (1 - r)

Initial State:
    V_L(0) = V_S(0) = V_0 / 2
    USDT_locked(0) = 0
    Total Value = V_L + V_S + USDT_locked = V_0

When |r| >= GRID:
1. Identify Winner W and Loser L.
2. Sell SELL_PCT of Winner's quantity:
       Proceeds = SELL_PCT * V_W
3. Split Proceeds:
       VAULT_PCT (20%) -> USDT_locked (Permanently vaulted risk-free cash)
       (1 - VAULT_PCT) (80%) -> Market buy Loser at dip
4. Update quantities and verify portfolio value:
       Total = V_L + V_S + USDT_locked
"""

import math
import random
from typing import Dict, Any, List, Optional


class PairedVolatilityHarvester:
    def __init__(
        self,
        initial_usdt: float = 1000.0,
        grid_threshold: float = 0.01,     # |r| >= 1.0% triggers rebalance
        sell_pct: float = 0.25,           # Sell 25% of the winning asset
        vault_pct: float = 0.20,          # 20% to USDT_locked, 80% to buy dip
        fee_pct: float = 0.0,             # 0.0% for idealized, or e.g. 0.075% with Binance BNB discount
    ):
        self.initial_usdt = initial_usdt
        self.grid_threshold = grid_threshold
        self.sell_pct = sell_pct
        self.vault_pct = vault_pct
        self.fee_pct = fee_pct

        # Initialize with equal values: V_L = V_S = V_0 / 2
        self.p_L = 100.0
        self.p_S = 100.0

        half = initial_usdt / 2.0
        self.q_L = half / self.p_L
        self.q_S = half / self.p_S

        self.usdt_locked = 0.0
        self.cycles = 0
        self.history: List[Dict[str, Any]] = []

    def get_portfolio_state(self) -> Dict[str, float]:
        v_L = self.q_L * self.p_L
        v_S = self.q_S * self.p_S
        total = v_L + v_S + self.usdt_locked
        return {
            "p_L": self.p_L,
            "p_S": self.p_S,
            "v_L": v_L,
            "v_S": v_S,
            "active_tokens": v_L + v_S,
            "usdt_locked": self.usdt_locked,
            "total_value": total,
            "return_pct": ((total - self.initial_usdt) / self.initial_usdt) * 100.0,
        }

    def process_step(self, r: float) -> Optional[Dict[str, Any]]:
        """
        Applies return r to L and -r to S.
        If |r| >= grid_threshold, executes rebalancing.
        """
        # Price evolution under perfect anti-correlation
        self.p_L *= (1.0 + r)
        self.p_S *= (1.0 - r)

        if abs(r) < self.grid_threshold:
            return None

        self.cycles += 1
        v_L = self.q_L * self.p_L
        v_S = self.q_S * self.p_S

        if r > 0:
            winner_sym, loser_sym = "L", "S"
            winner_val = v_L
            loser_price = self.p_S
        else:
            winner_sym, loser_sym = "S", "L"
            winner_val = v_S
            loser_price = self.p_L

        # 1. Sell SELL_PCT of winner
        gross_sale = winner_val * self.sell_pct
        sell_fee = gross_sale * self.fee_pct
        net_sale = gross_sale - sell_fee

        if winner_sym == "L":
            self.q_L *= (1.0 - self.sell_pct)
        else:
            self.q_S *= (1.0 - self.sell_pct)

        # 2. Split sale proceeds: 20% to vaulted USDT, 80% to buy dip on loser
        vault_addition = net_sale * self.vault_pct
        reinvest_dip = net_sale * (1.0 - self.vault_pct)

        self.usdt_locked += vault_addition

        # 3. Buy loser at its dip
        buy_fee = reinvest_dip * self.fee_pct
        net_buy = reinvest_dip - buy_fee
        new_loser_qty = net_buy / loser_price

        if loser_sym == "L":
            self.q_L += new_loser_qty
        else:
            self.q_S += new_loser_qty

        state = self.get_portfolio_state()
        state["step_r"] = r
        state["cycle"] = self.cycles
        state["winner"] = winner_sym
        state["vault_added"] = vault_addition
        self.history.append(state)
        return state


def run_demonstration():
    print("=" * 100)
    print(" SHANNON'S DEMON / PAIRED VOLATILITY HARVESTER: MATHEMATICAL SIMULATION")
    print("=" * 100)
    
    # ── Regime 1: Oscillating Waves (Sine-wave mean reversion) ──
    h_osc = PairedVolatilityHarvester(initial_usdt=1000.0, grid_threshold=0.02, sell_pct=0.25, vault_pct=0.20, fee_pct=0.0)
    print("\n[SCENARIO 1: Symmetrical Oscillating Market (r = +/-3.0%)]")
    print(f"{'Cycle':<6} | {'Return':<8} | {'Winner':<6} | {'V_L (Bull)':<11} | {'V_S (Bear)':<11} | {'USDT_locked':<13} | {'Total Portfolio':<16} | {'Gain':<8}")
    print("-" * 100)
    for step in range(1, 11):
        r = 0.03 if (step % 2 != 0) else -0.03
        res = h_osc.process_step(r)
        if res:
            print(f"{res['cycle']:<6} | {res['step_r']:>+6.1%} | {res['winner']:<6} | ${res['v_L']:<10.2f} | ${res['v_S']:<10.2f} | ${res['usdt_locked']:<12.2f} | ${res['total_value']:<15.2f} | +{res['return_pct']:.3f}%")
    
    st_osc = h_osc.get_portfolio_state()
    print("-" * 100)
    print(f"Scenario 1 Outcome: USDT_locked=${st_osc['usdt_locked']:.2f} | Active Tokens=${st_osc['active_tokens']:.2f} | Total Value=${st_osc['total_value']:.2f} (+{st_osc['return_pct']:.2f}%)")

    # ── Regime 2: Persistent Trend (One-way runaway market) ──
    h_trend = PairedVolatilityHarvester(initial_usdt=1000.0, grid_threshold=0.02, sell_pct=0.25, vault_pct=0.20, fee_pct=0.0)
    print("\n[SCENARIO 2: Persistent One-Way Trend (r = +3.0% consecutive)]")
    print(f"{'Cycle':<6} | {'Return':<8} | {'Winner':<6} | {'V_L (Bull)':<11} | {'V_S (Bear)':<11} | {'USDT_locked':<13} | {'Total Portfolio':<16} | {'Gain':<8}")
    print("-" * 100)
    for step in range(1, 11):
        res = h_trend.process_step(0.03)
        if res:
            print(f"{res['cycle']:<6} | {res['step_r']:>+6.1%} | {res['winner']:<6} | ${res['v_L']:<10.2f} | ${res['v_S']:<10.2f} | ${res['usdt_locked']:<12.2f} | ${res['total_value']:<15.2f} | {res['return_pct']:+.3f}%")
    
    st_trend = h_trend.get_portfolio_state()
    print("-" * 100)
    print(f"Scenario 2 Outcome: USDT_locked=${st_trend['usdt_locked']:.2f} | Active Tokens=${st_trend['active_tokens']:.2f} | Total Value=${st_trend['total_value']:.2f} ({st_trend['return_pct']:+.2f}%)")
    print("=" * 100)


if __name__ == "__main__":
    run_demonstration()
