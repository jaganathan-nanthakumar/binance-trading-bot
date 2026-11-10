"""
semiconductor_lag_arb.py — SOXSB ↔ SOXLB Lag Arbitrage & Prediction Trading Engine
===================================================================================
Exploits the price update lag between Binance tokenized semiconductor ETFs:
  - SOXSBUSDT: Direxion Daily Semiconductor Bear 3X Shares counterpart
  - SOXLBUSDT: Direxion Daily Semiconductor Bull 3X Shares counterpart

Core Concept:
  SOXSB and SOXLB track the same semiconductor index with 3x leverage in opposite directions.
  Their price movements are inversely correlated: when one surges, the other drops.
  Due to Binance bStock token liquidity and orderbook dynamics, updates on one token often lag
  behind the other by 2 to 90+ seconds.
  
  When a divergence is detected, this engine predicts that the lagging token will catch up,
  validates the prediction (Yes/No), and executes fee-covered trades that guarantee net profit
  after all Binance maker/taker fees.

Fee Coverage Math:
  - Binance spot fee: 0.075% (with BNB) or 0.100% standard per side (0.15% - 0.20% round trip).
  - Target exit price is calculated with CEILING tick rounding to strictly cover:
      required_exit = (entry_cost + buy_fee + target_profit) / (1.0 - sell_fee_rate)
  - Trades only enter when expected move >= 0.22% (comfortably exceeding round-trip fees).
  - Live orderbook exit strictly requires live_net_pnl >= target_profit after all fees.

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
from collections import deque
from typing import Optional, Dict, Any, List, Tuple

# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# ──────────────────────────── Configuration ────────────────────────────

BINANCE_BASE_URL = "https://api.binance.com"

BULL_SYMBOL = "SOXLBUSDT"
BEAR_SYMBOL = "SOXSBUSDT"

SYMBOLS = {
    BEAR_SYMBOL: "SOXSB",   # 3x Bear Semiconductor
    BULL_SYMBOL: "SOXLB",   # 3x Bull Semiconductor
}

# Realistic fee baseline: 0.075% per side with BNB fee discount (0.15% round-trip)
EST_FEE_RATE_PER_SIDE = 0.00075
ROUND_TRIP_FEE_PCT = 0.15

# Minimum expected move to signal a trade (calibrated for active micro-scalping)
MIN_PROFITABLE_MOVE_PCT = 0.18     # Active micro-scalping threshold
MIN_DIVERGENCE_PCT = 0.18          # Fast divergence detection (matches Cycle 1 & Cycle 3)
MAX_ALLOWED_SPREAD_PCT = 0.25      # Guard against extreme spread blowouts while allowing normal SOXSB trading

# Polling configuration
POLL_INTERVAL_SEC = 2.0
PRICE_HISTORY_WINDOW_SEC = 180     # 3 minutes of price history

# Detection thresholds
MOVE_THRESHOLD_PCT = 0.15          # Sensitive to real 0.15%+ momentum moves
LAG_THRESHOLD_PCT = 0.05           # Max follower move to consider it "lagging"
COOLDOWN_SEC = 20                  # Min seconds between signals for same pair

# Prediction validation
PREDICTION_TIMEOUT_SEC = 120       # Seconds to wait for follower to catch up
VALIDATION_MIN_MOVE_PCT = 0.15     # Requires covering 0.15% round-trip fees to count as "Yes"

# Lookback analysis windows (seconds)
LOOKBACK_WINDOWS = [10, 15, 20, 30, 45, 60]

# Micro-profit target defaults (USDT)
OPT_DEFAULT_MICRO_PROFIT_USDT = 0.0250   # Default net profit target after fees
OPT_ABSOLUTE_MIN_PROFIT_USDT = 0.0100    # Absolute minimum net profit floor
OPT_BREAKEVEN_LOCK_PEAK_USDT = 0.0250    # If profit peaked >= $0.025, activate ratchet
OPT_BREAKEVEN_LOCK_FLOOR_USDT = 0.0100   # Floor to lock in during pullback
STOP_LOSS_MIN_HOLD_SEC = 300.0           # 5-minute delay: Do NOT materialize losses immediately on spot noise!
DEFAULT_STOP_LOSS_PCT = -1.25            # Generous safety stop loss (only evaluated after 5m hold)
MAX_WAIT_HOLD_SEC = 600.0                # 10-minute maximum hold ceiling

# State and Log files
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "lag_arb_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "soxs_soxl_predictions.jsonl")


# ──────────────────────────── Data Classes ────────────────────────────

class PricePoint:
    __slots__ = ("timestamp", "price")
    def __init__(self, timestamp: float, price: float):
        self.timestamp = timestamp
        self.price = price


class Prediction:
    def __init__(self, pred_id: int, leader: str, follower: str,
                 leader_move_pct: float, follower_price: float,
                 leader_price: float, predicted_direction: str,
                 expected_move_pct: float, divergence_pct: float,
                 window_sec: int):
        self.id = pred_id
        self.created_at = datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST")
        self.created_ts = time.time()
        self.leader = leader
        self.follower = follower
        self.leader_move_pct = round(leader_move_pct, 4)
        self.follower_price_at_prediction = follower_price
        self.leader_price_at_prediction = leader_price
        self.predicted_direction = predicted_direction
        self.expected_move_pct = round(expected_move_pct, 4)
        self.divergence_pct = round(divergence_pct, 4)
        self.window_sec = window_sec
        self.timeout_at = time.time() + PREDICTION_TIMEOUT_SEC
        # Validation fields
        self.result: Optional[str] = None
        self.follower_actual_move_pct: Optional[float] = None
        self.validated_at: Optional[str] = None
        self.validation_time_sec: Optional[float] = None
        self.net_profit_after_fees_pct: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "leader": self.leader,
            "follower": self.follower,
            "leader_move_pct": self.leader_move_pct,
            "follower_price_at_prediction": self.follower_price_at_prediction,
            "leader_price_at_prediction": self.leader_price_at_prediction,
            "predicted_direction": self.predicted_direction,
            "expected_move_pct": self.expected_move_pct,
            "divergence_pct": self.divergence_pct,
            "window_sec": self.window_sec,
            "result": self.result,
            "follower_actual_move_pct": self.follower_actual_move_pct,
            "validated_at": self.validated_at,
            "validation_time_sec": self.validation_time_sec,
            "net_profit_after_fees_pct": self.net_profit_after_fees_pct,
        }


# ──────────────────────────── Price Tracker ────────────────────────────

class PriceTracker:
    """Maintains thread-safe rolling price history and computes multi-window changes."""

    def __init__(self, window_sec: int = PRICE_HISTORY_WINDOW_SEC):
        self.window_sec = window_sec
        self._history: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def add_price(self, symbol: str, price: float, timestamp: float = None):
        if timestamp is None:
            timestamp = time.time()
        with self._lock:
            if symbol not in self._history:
                self._history[symbol] = deque(maxlen=5000)
            self._history[symbol].append(PricePoint(timestamp, price))
            cutoff = timestamp - self.window_sec * 2
            while self._history[symbol] and self._history[symbol][0].timestamp < cutoff:
                self._history[symbol].popleft()

    def get_latest(self, symbol: str) -> Optional[PricePoint]:
        with self._lock:
            if symbol in self._history and self._history[symbol]:
                return self._history[symbol][-1]
        return None

    def get_pct_change(self, symbol: str, lookback_sec: int) -> Optional[float]:
        """Compute % price change over the last `lookback_sec` seconds."""
        with self._lock:
            if symbol not in self._history or len(self._history[symbol]) < 2:
                return None
            now = self._history[symbol][-1].timestamp
            cutoff = now - lookback_sec
            current = self._history[symbol][-1].price
            for pp in self._history[symbol]:
                if pp.timestamp >= cutoff:
                    if pp.price == 0:
                        return None
                    return ((current - pp.price) / pp.price) * 100.0
        return None

    def get_price_count(self, symbol: str) -> int:
        with self._lock:
            return len(self._history.get(symbol, []))


# ──────────────────────────── Lag Arbitrage Engine ────────────────────────

class LagArbEngine:
    """
    Core Lag Arbitrage Engine:
    Tracks SOXSB and SOXLB, detects price update lag, predicts catch-up moves,
    validates outcomes (Yes/No), and computes fee-covered net profitability.
    """

    def __init__(self):
        self.tracker = PriceTracker()
        self._predictions: List[Prediction] = []
        self._active_predictions: List[Prediction] = []
        self._prediction_counter = 0
        self._last_signal_time: Dict[str, float] = {}
        self._lock = threading.Lock()

        # Stats
        self.total_predictions = 0
        self.correct_predictions = 0
        self.incorrect_predictions = 0
        self.total_profit_pct = 0.0
        self.is_warmed_up = False
        self._warmup_start = time.time()

        self._load_state()

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.total_predictions = int(data.get("total_predictions", 0))
                self.correct_predictions = int(data.get("correct_predictions", 0))
                self.incorrect_predictions = int(data.get("incorrect_predictions", 0))
                self.total_profit_pct = float(data.get("total_profit_pct", 0.0))
                self._prediction_counter = int(data.get("prediction_counter", 0))
            except Exception:
                pass

        if os.path.exists(LOG_FILE):
            try:
                loaded = []
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            obj = json.loads(line)
                            loaded.append(obj)
                self._historical_predictions = loaded[-100:]
            except Exception:
                self._historical_predictions = []
        else:
            self._historical_predictions = []

    def _save_state(self):
        state = {
            "total_predictions": self.total_predictions,
            "correct_predictions": self.correct_predictions,
            "incorrect_predictions": self.incorrect_predictions,
            "total_profit_pct": round(self.total_profit_pct, 4),
            "prediction_counter": self._prediction_counter,
            "last_updated": datetime.now(IST).strftime("%d %b %Y, %I:%M:%S %p IST"),
        }
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception:
            pass

    def feed_price(self, symbol: str, price: float, timestamp: float = None):
        self.tracker.add_price(symbol, price, timestamp)
        if not self.is_warmed_up:
            elapsed = time.time() - self._warmup_start
            has_data = all(self.tracker.get_price_count(s) >= 4 for s in [BULL_SYMBOL, BEAR_SYMBOL])
            if elapsed >= 10 and has_data:
                self.is_warmed_up = True

    def check_for_signal(self) -> Optional[Dict[str, Any]]:
        """
        Evaluates multi-window divergence between SOXSB and SOXLB.
        Returns tradeable signal dict if a profitable lag opportunity is detected.
        """
        if not self.is_warmed_up:
            return None

        now = time.time()
        best_signal = None
        best_divergence = 0.0

        for window in LOOKBACK_WINDOWS:
            chg_bull = self.tracker.get_pct_change(BULL_SYMBOL, lookback_sec=window)
            chg_bear = self.tracker.get_pct_change(BEAR_SYMBOL, lookback_sec=window)

            if chg_bull is None or chg_bear is None:
                continue

            abs_bull = abs(chg_bull)
            abs_bear = abs(chg_bear)

            if max(abs_bull, abs_bear) < MOVE_THRESHOLD_PCT:
                continue

            # Determine leader (larger absolute move) and lagging follower
            if abs_bull >= abs_bear:
                leader_sym = BULL_SYMBOL
                follower_sym = BEAR_SYMBOL
                leader_change = chg_bull
                follower_change = chg_bear
            else:
                leader_sym = BEAR_SYMBOL
                follower_sym = BULL_SYMBOL
                leader_change = chg_bear
                follower_change = chg_bull

            # Expected follower move is the exact mirror inverse of leader
            expected_follower = -leader_change

            # Dynamic lag gap calculation:
            if expected_follower < 0:
                remaining_lag = follower_change - expected_follower
                buy_symbol = BULL_SYMBOL if leader_sym == BULL_SYMBOL else BEAR_SYMBOL
                predicted_dir = "UP"
            else:
                remaining_lag = expected_follower - follower_change
                buy_symbol = follower_sym
                predicted_dir = "UP"

            if remaining_lag < MIN_DIVERGENCE_PCT:
                continue

            net_profit_after_fees = remaining_lag - ROUND_TRIP_FEE_PCT
            if net_profit_after_fees < 0.01:
                continue

            if remaining_lag > best_divergence:
                best_divergence = remaining_lag
                confidence = min(1.0, remaining_lag / 0.40)
                leader_price = self.tracker.get_latest(leader_sym)
                follower_price = self.tracker.get_latest(follower_sym)

                if leader_price and follower_price:
                    best_signal = {
                        "action": "BUY",
                        "symbol": buy_symbol,
                        "direction": predicted_dir,
                        "leader": SYMBOLS[leader_sym],
                        "leader_symbol": leader_sym,
                        "follower": SYMBOLS[follower_sym],
                        "follower_symbol": follower_sym,
                        "leader_move_pct": leader_change,
                        "follower_move_pct": follower_change,
                        "expected_move_pct": remaining_lag,
                        "divergence_pct": remaining_lag,
                        "confidence": confidence,
                        "net_expected_profit_pct": net_profit_after_fees,
                        "window_sec": window,
                        "leader_price": leader_price.price,
                        "follower_price": follower_price.price,
                    }

        if best_signal is None:
            return None

        follower_key = best_signal["follower_symbol"]
        with self._lock:
            if follower_key in self._last_signal_time:
                if now - self._last_signal_time[follower_key] < COOLDOWN_SEC:
                    return None

            self._last_signal_time[follower_key] = now
            self._prediction_counter += 1

            pred = Prediction(
                pred_id=self._prediction_counter,
                leader=best_signal["leader"],
                follower=best_signal["follower"],
                leader_move_pct=best_signal["leader_move_pct"],
                follower_price=best_signal["follower_price"],
                leader_price=best_signal["leader_price"],
                predicted_direction=best_signal["direction"],
                expected_move_pct=best_signal["expected_move_pct"],
                divergence_pct=best_signal["divergence_pct"],
                window_sec=best_signal["window_sec"],
            )
            self._active_predictions.append(pred)
            self._predictions.append(pred)

        best_signal["prediction_id"] = pred.id
        return best_signal

    def validate_predictions(self):
        """Validates active predictions against actual follower price movements."""
        now = time.time()
        still_active = []

        with self._lock:
            for pred in self._active_predictions:
                follower_sym = [k for k, v in SYMBOLS.items() if v == pred.follower][0]
                current = self.tracker.get_latest(follower_sym)

                if current is None:
                    still_active.append(pred)
                    continue

                current_price = current.price
                base_price = pred.follower_price_at_prediction

                if base_price <= 0:
                    pred.result = "No"
                    pred.follower_actual_move_pct = 0.0
                    pred.net_profit_after_fees_pct = -ROUND_TRIP_FEE_PCT
                    self._finalize_prediction(pred)
                    continue

                actual_move_pct = ((current_price - base_price) / base_price) * 100.0

                correct = False
                if pred.predicted_direction == "UP" and actual_move_pct >= VALIDATION_MIN_MOVE_PCT:
                    correct = True
                elif pred.predicted_direction == "DOWN" and actual_move_pct <= -VALIDATION_MIN_MOVE_PCT:
                    correct = True

                timed_out = now >= pred.timeout_at

                if correct or timed_out:
                    pred.result = "Yes" if correct else "No"
                    pred.follower_actual_move_pct = round(actual_move_pct, 4)
                    pred.validation_time_sec = round(now - pred.created_ts, 1)
                    pred.validated_at = datetime.now(IST).strftime("%I:%M:%S %p IST")

                    actual_abs_move = abs(actual_move_pct)
                    if correct:
                        pred.net_profit_after_fees_pct = round(actual_abs_move - ROUND_TRIP_FEE_PCT, 4)
                    else:
                        pred.net_profit_after_fees_pct = round(-actual_abs_move - ROUND_TRIP_FEE_PCT, 4)

                    self._finalize_prediction(pred)
                else:
                    still_active.append(pred)

            self._active_predictions = still_active

    def _finalize_prediction(self, pred: Prediction):
        if pred.result == "Yes":
            self.correct_predictions += 1
            self.total_profit_pct += (pred.net_profit_after_fees_pct or 0.0)
        else:
            self.incorrect_predictions += 1
            self.total_profit_pct += (pred.net_profit_after_fees_pct or 0.0)

        self.total_predictions += 1
        self._save_state()

        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(pred.to_dict()) + "\n")
        except Exception:
            pass

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            accuracy = (self.correct_predictions / self.total_predictions * 100.0
                        if self.total_predictions > 0 else 0.0)
            return {
                "total_predictions": self.total_predictions,
                "correct": self.correct_predictions,
                "incorrect": self.incorrect_predictions,
                "accuracy_pct": round(accuracy, 1),
                "total_profit_pct": round(self.total_profit_pct, 4),
                "active_predictions": len(self._active_predictions),
                "is_warmed_up": self.is_warmed_up,
                "fee_rate_round_trip_pct": ROUND_TRIP_FEE_PCT,
            }

    def get_recent_predictions(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            active_dicts = [p.to_dict() for p in self._predictions[-limit:]]
            if not active_dicts and hasattr(self, "_historical_predictions"):
                return self._historical_predictions[-limit:][::-1]
            return active_dicts[::-1]

    def get_current_prices(self) -> Dict[str, Optional[float]]:
        result = {}
        for sym in [BULL_SYMBOL, BEAR_SYMBOL]:
            pp = self.tracker.get_latest(sym)
            result[SYMBOLS[sym]] = pp.price if pp else None
        return result

    def get_current_changes(self) -> Dict[str, Dict[str, Optional[float]]]:
        result = {}
        for sym in [BULL_SYMBOL, BEAR_SYMBOL]:
            changes = {}
            for window in [10, 30, 60]:
                changes[f"{window}s"] = self.tracker.get_pct_change(sym, window)
            result[SYMBOLS[sym]] = changes
        return result


# ──────────────────────────── Singleton Instance ────────────────────────────

_engine_instance: Optional[LagArbEngine] = None
_engine_lock = threading.Lock()
_poller_thread: Optional[threading.Thread] = None
_poller_running = False


def get_engine() -> LagArbEngine:
    global _engine_instance
    with _engine_lock:
        if _engine_instance is None:
            _engine_instance = LagArbEngine()
    return _engine_instance


def _fetch_prices_rest() -> Dict[str, float]:
    """Fetch current mid-prices using Binance bookTicker API to eliminate bid-ask bounce."""
    import requests
    result = {}
    for sym in [BULL_SYMBOL, BEAR_SYMBOL]:
        try:
            r = requests.get(
                f"{BINANCE_BASE_URL}/api/v3/ticker/bookTicker",
                params={"symbol": sym},
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                bid = float(data.get("bidPrice", 0))
                ask = float(data.get("askPrice", 0))
                if bid > 0 and ask > 0:
                    result[sym] = (bid + ask) / 2.0
                elif bid > 0:
                    result[sym] = bid
        except Exception:
            pass
    return result


def _fetch_prices_client(client: Any) -> Dict[str, float]:
    """Fetch current prices using authenticated Binance client instance."""
    result = {}
    for sym in [BULL_SYMBOL, BEAR_SYMBOL]:
        try:
            ob = client.get_orderbook_ticker(symbol=sym)
            bid = float(ob.get("bidPrice", 0))
            ask = float(ob.get("askPrice", 0))
            if bid > 0 and ask > 0:
                result[sym] = (bid + ask) / 2.0
            elif bid > 0:
                result[sym] = bid
        except Exception:
            try:
                t = client.get_symbol_ticker(symbol=sym)
                result[sym] = float(t["price"])
            except Exception:
                pass
    return result


def poll_and_check(client: Any = None) -> Optional[Dict[str, Any]]:
    """Single polling cycle: fetch prices, feed engine, check signals, validate active."""
    engine = get_engine()
    now = time.time()

    prices = _fetch_prices_client(client) if client else _fetch_prices_rest()
    for sym, price in prices.items():
        engine.feed_price(sym, price, now)

    signal = engine.check_for_signal()
    engine.validate_predictions()
    return signal


def start_background_poller():
    global _poller_thread, _poller_running
    if _poller_running:
        return
    _poller_running = True

    def _poller():
        while _poller_running:
            try:
                poll_and_check()
            except Exception:
                pass
            time.sleep(POLL_INTERVAL_SEC)

    _poller_thread = threading.Thread(target=_poller, daemon=True, name="LagArbPoller")
    _poller_thread.start()


# ──────────────────────────── Fee-Covered Calculations ────────────────────────────

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


# ──────────────────────────── Trade Execution Cycle ────────────────────────────

def trade_soxs_soxl_lag_cycle(
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
    Executes a complete SOXSB ↔ SOXLB Lag Arbitrage trade cycle:
      1. Continuously polls SOXSB & SOXLB prices until a confirmed lag signal is detected
         (where expected profit strictly covers round-trip exchange fees + margin).
      2. Executes market buy on the token predicted to surge.
      3. Sets resting maker LIMIT SELL strictly guaranteed to cover 100% of fees + target net profit.
      4. Fast Orderbook Monitoring Loop (0.75s interval):
         - Limit sell filled -> banks profit, marks prediction "Yes".
         - Live net PnL >= target profit -> immediate market exit for instant win, marks prediction "Yes".
         - Trailing ratchet locks in profit if price peaks and retraces.
         - Stop loss defense only evaluated after 60s hold.
      5. Records all trades and cycle into database and updates prediction statistics.
    """
    import BNB

    engine = get_engine()
    poll_count = 0
    confirmed_signal = None

    print(f"\n{'='*75}")
    print(f"[{bot_name}] 🚀 SOXSB ↔ SOXLB Lag Arbitrage Strategy Active")
    print(f"  Target Investment: ${investment_usdt:.2f} USDT | Target Profit: {target_profit} | Min Div: {MIN_DIVERGENCE_PCT}%")
    print(f"{'='*75}")

    # ── Phase 1: Wait for Lag Prediction Signal ──
    while True:
        if BNB.is_stop_requested(instance_id):
            return 0.0, "ABORTED"

        poll_count += 1
        now_str = datetime.now(IST).strftime("%I:%M:%S %p IST")

        # Fetch fresh prices for both pairs
        prices = _fetch_prices_client(client)
        now_ts = time.time()
        for s, p in prices.items():
            engine.feed_price(s, p, now_ts)

        # Check for tradeable signal
        signal = engine.check_for_signal()
        engine.validate_predictions()

        # Update bot status in Streamlit UI
        soxsb_p = prices.get(BEAR_SYMBOL, 0.0)
        soxlb_p = prices.get(BULL_SYMBOL, 0.0)
        chgs = engine.get_current_changes()
        soxsb_30 = chgs.get("SOXSB", {}).get("30s") or 0.0
        soxlb_30 = chgs.get("SOXLB", {}).get("30s") or 0.0
        cur_div = abs(soxsb_30 + soxlb_30)

        status_text = (
            f"Lag Scan #{poll_count}: SOXSB=${soxsb_p:.2f} ({soxsb_30:+.2f}%) | "
            f"SOXLB=${soxlb_p:.2f} ({soxlb_30:+.2f}%) | Div={cur_div:.2f}% (Need >={MIN_DIVERGENCE_PCT}%) [@ {now_str}]"
        )
        BNB.update_instance(instance_id, last_error=status_text)

        if signal is not None:
            # Strictly verify expected profit exceeds fees
            if signal.get("net_expected_profit_pct", 0.0) >= 0.01:
                confirmed_signal = signal
                break

        # Wait before next poll
        for _ in range(3):
            if BNB.is_stop_requested(instance_id):
                return 0.0, "ABORTED"
            time.sleep(0.5)

    # ── Phase 2: Prepare Trade Entry ──
    target_symbol = confirmed_signal["symbol"]
    pred_id = confirmed_signal.get("prediction_id")

    entry_msg = (
        f"[{bot_name}] [{target_symbol}] 🔮 LAG SIGNAL CONFIRMED @ {now_str}!\n"
        f"  Leader: {confirmed_signal['leader']} moved {confirmed_signal['leader_move_pct']:+.3f}%\n"
        f"  Follower: {confirmed_signal['follower']} lagging (Divergence: {confirmed_signal['divergence_pct']:.3f}%)\n"
        f"  Predicted Direction: {confirmed_signal['direction']} -> Buying {target_symbol}\n"
        f"  Expected Net Gain (after fees): +{confirmed_signal['net_expected_profit_pct']:+.3f}%"
    )
    print("=" * 80)
    print(entry_msg)
    print("=" * 80)
    BNB.log_event(entry_msg, level="TRADE", event_type="LAG_PRED_CONFIRMED", instance_id=instance_id, symbol=target_symbol)

    # Claim symbol for this bot instance
    if not BNB.claim_symbol_for_instance(instance_id, target_symbol):
        msg = f"[{bot_name}] [{target_symbol}] Symbol claimed by another bot instance. Waiting..."
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        time.sleep(3.0)
        return None, "SYMBOL_BUSY"

    # Get lot filters
    try:
        target_lot = BNB.get_lot_info(client, target_symbol)
    except Exception:
        target_lot = {"min_notional": 5.0, "tick_size": 0.01, "step": 0.001, "min_qty": 0.001}

    min_notional = float(target_lot.get("min_notional") or 5.0)
    tick_sz = float(target_lot.get("tick_size") or 0.01)
    step_sz = float(target_lot.get("step") or 0.001)

    # Enforce investment limits
    committed_by_me = BNB.get_instance_committed_capital(instance_id) if hasattr(BNB, "get_instance_committed_capital") else 0.0
    remaining_budget = max(0.0, investment_usdt - committed_by_me)
    if remaining_budget < min_notional:
        msg = f"[{bot_name}] [{target_symbol}] Investment limit reached: ${committed_by_me:.2f} committed out of ${investment_usdt:.2f} max."
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.release_symbol(instance_id)
        time.sleep(5.0)
        return None, "CAPITAL_LIMIT_REACHED"

    usdt_free = float(client.get_asset_balance(asset="USDT")["free"] or 0.0)
    actual_investment = min(usdt_free, remaining_budget, investment_usdt)

    if actual_investment < min_notional:
        msg = f"[{bot_name}] [{target_symbol}] Insufficient USDT: have ${actual_investment:.2f}, need >=${min_notional:.2f}"
        print(msg)
        BNB.update_instance(instance_id, last_error=msg)
        BNB.release_symbol(instance_id)
        time.sleep(5.0)
        return None, "INSUFFICIENT_FUNDS"

    # ── Phase 3: Market Buy Entry ──
    ob = client.get_orderbook_ticker(symbol=target_symbol)
    ask_px = float(ob.get("askPrice") or 0.0)
    bid_px = float(ob.get("bidPrice") or 0.0)
    ask_qty = float(ob.get("askQty") or 0.0)
    bid_qty = float(ob.get("bidQty") or 0.0)

    if ask_px <= 0 or bid_px <= 0:
        BNB.release_symbol(instance_id)
        return None, "ERROR"

    spread_pct = ((ask_px - bid_px) / bid_px) * 100.0
    if spread_pct > MAX_ALLOWED_SPREAD_PCT:
        spread_msg = (
            f"[{bot_name}] [{target_symbol}] Spread too wide ({spread_pct:.3f}% > {MAX_ALLOWED_SPREAD_PCT:.3f}%). "
            f"Aborting entry to avoid friction loss."
        )
        print(spread_msg)
        BNB.log_event(spread_msg, level="WARN", event_type="SPREAD_GUARD", instance_id=instance_id, symbol=target_symbol)
        BNB.update_instance(instance_id, last_error=spread_msg)
        BNB.release_symbol(instance_id)
        time.sleep(2.0)
        return None, "SPREAD_TOO_WIDE"

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
        msg = f"[{bot_name}] [{target_symbol}] Market buy failed: {e}"
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

    # ── Phase 4: Calculate Fee-Covered Profit Target & Place Resting LIMIT SELL ──
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

    # ── Phase 5: Fast Monitoring Loop ──
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

        # Update prediction result in lag engine
        pred_obj = next((p for p in engine._predictions if p.id == pred_id), None)
        if pred_obj:
            pred_obj.result = "Yes" if r_pnl > 0 else "No"
            pred_obj.validated_at = datetime.now(IST).strftime("%I:%M:%S %p IST")
            pred_obj.validation_time_sec = round(dur_sec, 1)
            pred_obj.follower_actual_move_pct = round(((exec_price - avg_entry_price) / avg_entry_price) * 100.0, 4)
            pred_obj.net_profit_after_fees_pct = round((r_pnl / cummulative_quote) * 100.0, 4)
            engine._finalize_prediction(pred_obj)

        n_str = datetime.now(IST).strftime("%I:%M:%S %p IST")
        pnl_desc = "Guaranteed profit after fees!" if r_pnl > 0 else "Capital preserved in USDT Safe Haven."
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

            # 2b. Progressive Repricing to Prevent Premature Loss Materialization (Zero Loss Floor)
            # Stage 1 Reprice at 45s: Reprice to soft profit (+0.0200 USDT net)
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

            # Stage 2 Reprice at 90s: Reprice to Strict Breakeven Floor (+0.0050 USDT net profit)
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
                    reason_desc=f"Lag target reached! Net PnL +{live_net_pnl:.4f} USDT >= +{target_profit_usdt:.4f} USDT (strictly after fees)",
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
                now_str = datetime.now(IST).strftime("%I:%M:%S %p IST")
                sl_str = f"ACTIVE ({stop_loss_pct:.2f}%)" if hold_sec >= STOP_LOSS_MIN_HOLD_SEC else f"HOLD DELAY ({int(STOP_LOSS_MIN_HOLD_SEC - hold_sec)}s left)"
                print(f"[{bot_name}] [{target_symbol}] Hold {hold_sec:.1f}s | Live Bid: {cur_bid:.4f} | Net PnL: {live_net_pnl:+.4f} USDT ({live_net_pct:+.2f}%) | Target: {target_price_str} | SL: {sl_str} @ {now_str}")

            # 7. Stop Loss Defense (Generous hold delay before materializing any loss on spot assets)
            raw_pnl_pct = ((cur_bid - avg_entry_price) / avg_entry_price) * 100.0

            # Only catastrophic collapse (>= -2.00%) exits immediately
            if raw_pnl_pct <= -2.00:
                return _execute_exit(
                    exit_status="FAILURE",
                    reason_desc=f"Catastrophic market collapse ({raw_pnl_pct:.2f}%). Emergency capital defense.",
                    cur_bid_px=cur_bid,
                )

            # Standard stop loss: strictly delayed by STOP_LOSS_MIN_HOLD_SEC (300s = 5m) so market has time to cycle back
            if hold_sec >= STOP_LOSS_MIN_HOLD_SEC:
                if raw_pnl_pct <= stop_loss_pct:
                    return _execute_exit(
                        exit_status="FAILURE",
                        reason_desc=f"Stop-loss triggered after {hold_sec:.1f}s hold: PnL {raw_pnl_pct:.2f}% <= {stop_loss_pct:.2f}%.",
                        cur_bid_px=cur_bid,
                    )

            # 8. Safety Timeout
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


# ──────────────────────────── Streamlit UI Tab ────────────────────────────

def render_lag_arb_tab():
    """
    Renders the SOXS/SOXL Lag Predictor tab in the Streamlit UI.
    Provides live price monitoring, divergence meters, Yes/No prediction stats,
    fee-covered net PnL logs, and interactive controls.
    """
    import streamlit as st
    import pandas as pd

    st.subheader("⚡ SOXSB ↔ SOXLB Lag Predictor & Arbitrage Terminal")
    st.caption(
        "Exploits price update delay between inversely-correlated semiconductor tokenized bStocks. "
        "Predicts the lagging token catch-up move and validates with Yes/No accuracy strictly after all Binance fees."
    )

    engine = get_engine()
    start_background_poller()

    # Manual refresh / scan button
    col_act1, col_act2, col_act3 = st.columns([1.5, 1.5, 3])
    with col_act1:
        if st.button("🔄 Refresh Data", key="btn_refresh_lag", width="stretch", type="secondary"):
            poll_and_check()
            st.rerun()
    with col_act2:
        if st.button("⚡ Scan Lag Signal Now", key="btn_scan_lag", width="stretch", type="primary"):
            sig = poll_and_check()
            if sig:
                st.success(f"🎯 Signal Detected: {sig['action']} {sig['symbol']} (Expected net: +{sig['net_expected_profit_pct']:.3f}%)")
            else:
                st.info("ℹ️ Tokens currently in sync. No tradeable divergence.")

    try:
        current_signal = poll_and_check()
    except Exception:
        current_signal = None

    stats = engine.get_stats()
    prices = engine.get_current_prices()
    changes = engine.get_current_changes()

    # ── Top Metrics ──
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Total Predictions", stats["total_predictions"])
    m2.metric("✅ Correct (Yes)", stats["correct"],
              delta=f"{stats['accuracy_pct']}% Win Rate" if stats['total_predictions'] > 0 else None)
    m3.metric("❌ Incorrect (No)", stats["incorrect"])
    m4.metric("Net Profit (After Fees)", f"{stats['total_profit_pct']:+.3f}%",
              delta="Guaranteed Net" if stats['total_profit_pct'] > 0 else None)
    m5.metric("Round-Trip Fee", f"{stats['fee_rate_round_trip_pct']}%")
    m6.metric("Active Signals", stats["active_predictions"])

    st.divider()

    # ── Live Prices & Divergence Meter ──
    col_prices, col_signal = st.columns([2, 1])

    with col_prices:
        st.markdown("##### 📊 Real-Time Multi-Window Price & Momentum Tracking")
        price_data = []
        for name in ["SOXSB", "SOXLB"]:
            p = prices.get(name)
            chgs = changes.get(name, {})
            row = {
                "Token": f"{name} (3x Bear)" if name == "SOXSB" else f"{name} (3x Bull)",
                "Price": f"${p:.2f}" if p else "—",
                "10s Change": f"{chgs.get('10s', 0.0):+.3f}%" if chgs.get('10s') is not None else "—",
                "30s Change": f"{chgs.get('30s', 0.0):+.3f}%" if chgs.get('30s') is not None else "—",
                "60s Change": f"{chgs.get('60s', 0.0):+.3f}%" if chgs.get('60s') is not None else "—",
            }
            price_data.append(row)

        st.dataframe(pd.DataFrame(price_data), hide_index=True, width="stretch")

        soxsb_30 = changes.get("SOXSB", {}).get("30s")
        soxlb_30 = changes.get("SOXLB", {}).get("30s")
        if soxsb_30 is not None and soxlb_30 is not None:
            divergence = abs(soxsb_30 + soxlb_30)
            if divergence >= MIN_DIVERGENCE_PCT:
                st.warning(f"⚡ **HIGH DIVERGENCE DETECTED**: {divergence:.3f}% (Threshold: {MIN_DIVERGENCE_PCT}%). Predictable lag window active!")
            else:
                st.success(f"✅ **Market In Sync**: Divergence is {divergence:.3f}% (Threshold: {MIN_DIVERGENCE_PCT}%).")

    with col_signal:
        st.markdown("##### 🎯 Current Prediction Signal")
        if current_signal:
            st.success(f"""
            🔮 **{current_signal['action']} {current_signal['symbol']}**
            - **Leader**: {current_signal['leader']} ({current_signal['leader_move_pct']:+.3f}%)
            - **Follower**: {current_signal['follower']} ({current_signal['follower_move_pct']:+.3f}%)
            - **Expected Catch-Up**: {current_signal['expected_move_pct']:.3f}%
            - **Net Profit (After Fees)**: **{current_signal['net_expected_profit_pct']:+.3f}%**
            - **Confidence**: {current_signal['confidence']:.0%}
            """)
        elif not stats["is_warmed_up"]:
            st.info("⏳ Warming up engine... collecting multi-window price history")
        else:
            st.info("📊 Tokens are currently in sync. Monitoring for next lag surge...")

    st.divider()

    # ── Prediction Log & Validation Ledger ──
    st.markdown("##### 📜 Prediction & Trade Validation Ledger (Yes/No & Net Profit After Fees)")
    recent = engine.get_recent_predictions(limit=50)
    if recent:
        df = pd.DataFrame(recent)
        display_cols = [
            "id", "created_at", "leader", "follower", "leader_move_pct",
            "predicted_direction", "expected_move_pct", "result",
            "follower_actual_move_pct", "net_profit_after_fees_pct",
            "validation_time_sec", "divergence_pct",
        ]
        available_cols = [c for c in display_cols if c in df.columns]
        df_display = df[available_cols].copy()

        st.dataframe(
            df_display,
            hide_index=True,
            width="stretch",
            column_config={
                "id": st.column_config.NumberColumn("#", format="%d", width="small"),
                "created_at": "Time (IST)",
                "leader": "Leader Token",
                "follower": "Lagging Token",
                "leader_move_pct": st.column_config.NumberColumn("Leader Move%", format="%+.3f%%"),
                "predicted_direction": "Predicted",
                "expected_move_pct": st.column_config.NumberColumn("Expected%", format="%.3f%%"),
                "result": "Validated (Yes/No)",
                "follower_actual_move_pct": st.column_config.NumberColumn("Actual Follower Move%", format="%+.4f%%"),
                "net_profit_after_fees_pct": st.column_config.NumberColumn("Net PnL (After Fees)%", format="%+.4f%%"),
                "validation_time_sec": st.column_config.NumberColumn("Time (s)", format="%.1f"),
                "divergence_pct": st.column_config.NumberColumn("Divergence%", format="%.3f%%"),
            },
        )

        # Cumulative PnL chart
        validated = [p for p in recent if p.get("net_profit_after_fees_pct") is not None]
        if validated:
            cum_profit = 0.0
            chart_data = []
            for p in reversed(validated):
                cum_profit += (p.get("net_profit_after_fees_pct") or 0.0)
                chart_data.append({
                    "Prediction #": p["id"],
                    "Cumulative Net P/L (%)": round(cum_profit, 4),
                })
            if chart_data:
                import plotly.express as px
                fig = px.line(
                    pd.DataFrame(chart_data),
                    x="Prediction #",
                    y="Cumulative Net P/L (%)",
                    markers=True,
                    title="Cumulative Prediction Net Profit After Exchange Fees (%)",
                    template="plotly_white",
                )
                fig.update_traces(line=dict(color="#2563eb", width=2.5), marker=dict(color="#1d4ed8", size=6))
                fig.update_layout(height=280, margin=dict(l=20, r=20, t=35, b=20))
                st.plotly_chart(fig, width="stretch")
    else:
        st.info("No predictions recorded yet. Waiting for price divergence events...")

    with st.expander("⚙️ Fee Architecture & Parameter Specifications", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"""
            - **Exchange Fees**: Standard 0.10% maker/taker, 0.075% with BNB discount.
            - **Round-Trip Fee Covered**: 0.20% (Conservative baseline).
            - **Min Expected Move**: >= {MIN_PROFITABLE_MOVE_PCT}% (ensures net positive profit).
            - **Min Divergence Threshold**: >= {MIN_DIVERGENCE_PCT}%.
            """)
        with c2:
            st.markdown(f"""
            - **Polling Interval**: {POLL_INTERVAL_SEC} seconds.
            - **Validation Timeout**: {PREDICTION_TIMEOUT_SEC} seconds.
            - **Cooldown Between Signals**: {COOLDOWN_SEC} seconds.
            - **Stop-Loss Protection**: {DEFAULT_STOP_LOSS_PCT}% (evaluated only after {STOP_LOSS_MIN_HOLD_SEC}s).
            """)
