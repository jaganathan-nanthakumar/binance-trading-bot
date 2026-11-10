"""
SOXSB ↔ SOXLB Lag Prediction Engine
=====================================
Exploits the delay between SOXSB and SOXLB price updates on Binance.

Since SOXSB (3x Bear semiconductor) and SOXLB (3x Bull semiconductor) track
the same underlying index inversely, their price changes should mirror each other.
However, Binance tokenized stock prices can lag, creating predictable moments.

Strategy:
  1. Continuously poll both SOXSBUSDT and SOXLBUSDT prices
  2. Compute rolling % changes over short windows
  3. When one moves significantly and the other hasn't yet → PREDICTION
  4. Track whether the lagging token catches up → VALIDATION (Yes/No)

Uses ONLY Binance public REST API (no external data sources).
"""

import os
import sys
import json
import time
import math
import signal
import requests
import logging
from datetime import datetime, timezone, timedelta
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional

# ──────────────────────────── Configuration ────────────────────────────

BINANCE_BASE_URL = "https://api.binance.com"

# The two inversely-correlated tokenized stocks on Binance
LEADER_SYMBOLS = {
    "SOXSBUSDT": "SOXSB",   # 3x Bear Semiconductor
    "SOXLBUSDT": "SOXLB",   # 3x Bull Semiconductor
}

# Polling
POLL_INTERVAL_SEC = 2          # How often to fetch prices (seconds)
PRICE_HISTORY_WINDOW = 90     # Seconds of price history to retain

# Detection thresholds
MOVE_THRESHOLD_PCT = 0.25      # Minimum % move in "leader" to trigger prediction (raised from 0.15% to avoid spread noise)
LAG_THRESHOLD_PCT = 0.05       # Max % move in "follower" to consider it lagging
COOLDOWN_SEC = 30              # Min seconds between predictions for same pair

# Prediction validation
PREDICTION_TIMEOUT_SEC = 120   # How long to wait for follower to respond
VALIDATION_MIN_MOVE_PCT = 0.20 # Min follower % move (must cover 0.15% round-trip fees + margin)

# Logging
LOG_FILE = "soxs_soxl_predictions.jsonl"
STATS_DISPLAY_INTERVAL = 10    # Show stats every N predictions


# ──────────────────────────── Data Classes ────────────────────────────

@dataclass
class PricePoint:
    timestamp: float
    price: float

@dataclass
class Prediction:
    id: int
    created_at: str
    leader: str
    follower: str
    leader_move_pct: float
    follower_price_at_prediction: float
    leader_price_at_prediction: float
    predicted_direction: str   # "UP" or "DOWN" for the follower
    timeout_at: float
    # Filled after validation
    result: Optional[str] = None          # "Yes" or "No"
    follower_actual_move_pct: Optional[float] = None
    validated_at: Optional[str] = None
    validation_time_sec: Optional[float] = None

    def to_dict(self):
        return asdict(self)


# ──────────────────────────── Binance API ────────────────────────────

class BinanceClient:
    """Lightweight Binance REST API client (public endpoints only)."""

    def __init__(self, base_url: str = BINANCE_BASE_URL):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "SOX-Lag-Predictor/1.0"})

    def get_price(self, symbol: str) -> float:
        """Fetch latest mid-price for a symbol using bookTicker to avoid bid-ask bounce."""
        try:
            resp = self.session.get(
                f"{self.base_url}/api/v3/ticker/bookTicker",
                params={"symbol": symbol},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            bid = float(data.get("bidPrice", 0))
            ask = float(data.get("askPrice", 0))
            if bid > 0 and ask > 0:
                return (bid + ask) / 2.0
            elif bid > 0:
                return bid
        except Exception:
            pass
        # Fallback
        resp = self.session.get(
            f"{self.base_url}/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["price"])

    def get_prices_bulk(self, symbols: list) -> dict:
        """Fetch prices for multiple symbols."""
        # Fetch individually (reliable across all Binance API versions)
        result = {}
        for sym in symbols:
            result[sym] = self.get_price(sym)
        return result

    def get_klines(self, symbol: str, interval: str = "1m", limit: int = 60) -> list:
        """Fetch kline/candlestick data."""
        resp = self.session.get(
            f"{self.base_url}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_ticker_24h(self, symbol: str) -> dict:
        """Fetch 24hr ticker stats."""
        resp = self.session.get(
            f"{self.base_url}/api/v3/ticker/24hr",
            params={"symbol": symbol},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


# ──────────────────────────── Price Tracker ────────────────────────────

class PriceTracker:
    """Maintains rolling price history and computes short-term changes."""

    def __init__(self, window_sec: int = PRICE_HISTORY_WINDOW):
        self.window_sec = window_sec
        self.history: dict[str, deque[PricePoint]] = {}

    def add_price(self, symbol: str, price: float, timestamp: float = None):
        if timestamp is None:
            timestamp = time.time()
        if symbol not in self.history:
            self.history[symbol] = deque(maxlen=5000)
        self.history[symbol].append(PricePoint(timestamp, price))
        self._prune(symbol, timestamp)

    def _prune(self, symbol: str, now: float):
        """Remove old price points outside the window."""
        cutoff = now - self.window_sec * 2  # keep extra buffer
        while self.history[symbol] and self.history[symbol][0].timestamp < cutoff:
            self.history[symbol].popleft()

    def get_latest(self, symbol: str) -> Optional[PricePoint]:
        if symbol in self.history and self.history[symbol]:
            return self.history[symbol][-1]
        return None

    def get_pct_change(self, symbol: str, lookback_sec: int) -> Optional[float]:
        """Compute % change over the last `lookback_sec` seconds."""
        if symbol not in self.history or len(self.history[symbol]) < 2:
            return None

        now = self.history[symbol][-1].timestamp
        cutoff = now - lookback_sec
        current = self.history[symbol][-1].price

        # Find the oldest price within the lookback window
        for pp in self.history[symbol]:
            if pp.timestamp >= cutoff:
                if pp.price == 0:
                    return None
                return ((current - pp.price) / pp.price) * 100.0
        return None

    def get_volatility(self, symbol: str, lookback_sec: int = 60) -> Optional[float]:
        """Compute recent price volatility (std dev of returns)."""
        if symbol not in self.history or len(self.history[symbol]) < 5:
            return None

        now = self.history[symbol][-1].timestamp
        cutoff = now - lookback_sec
        prices = [pp.price for pp in self.history[symbol] if pp.timestamp >= cutoff]

        if len(prices) < 3:
            return None

        returns = [(prices[i] - prices[i-1]) / prices[i-1] * 100
                   for i in range(1, len(prices)) if prices[i-1] != 0]
        if not returns:
            return None

        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        return math.sqrt(variance)


# ──────────────────────────── Prediction Engine ────────────────────────

class LagPredictionEngine:
    """
    Core prediction logic:
    - SOXSB and SOXLB are inversely correlated (one is 3x bull, other 3x bear)
    - When SOXSB moves up X%, SOXLB should move down ~X% (and vice versa)
    - If one moves and the other hasn't yet → predict the laggard will catch up
    """

    def __init__(self):
        self.client = BinanceClient()
        self.tracker = PriceTracker()
        self.predictions: list[Prediction] = []
        self.active_predictions: list[Prediction] = []
        self.prediction_counter = 0
        self.last_prediction_time: dict[str, float] = {}

        # Stats
        self.total_predictions = 0
        self.correct_predictions = 0
        self.incorrect_predictions = 0

        # Lookback windows to analyze (seconds)
        self.lookback_windows = [10, 20, 30, 45, 60]

        self._setup_logging()

    def _setup_logging(self):
        self.logger = logging.getLogger("LagPredictor")
        self.logger.setLevel(logging.INFO)

        # Console handler
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        fmt = logging.Formatter(
            "%(asctime)s │ %(message)s",
            datefmt="%H:%M:%S"
        )
        ch.setFormatter(fmt)
        self.logger.addHandler(ch)

    def _get_inverse_pair(self, symbol: str) -> str:
        """Get the inversely correlated pair."""
        symbols = list(LEADER_SYMBOLS.keys())
        if symbol == symbols[0]:
            return symbols[1]
        return symbols[0]

    def _check_for_lag_signal(self, now: float) -> Optional[Prediction]:
        """
        Check if one token has moved significantly while the other lags.

        Multi-window analysis: check multiple lookback periods to find
        the strongest lag signal.
        """
        symbols = list(LEADER_SYMBOLS.keys())
        best_signal = None
        best_divergence = 0

        for window in self.lookback_windows:
            changes = {}
            for sym in symbols:
                pct = self.tracker.get_pct_change(sym, lookback_sec=window)
                if pct is not None:
                    changes[sym] = pct

            if len(changes) < 2:
                continue

            sym_a, sym_b = symbols
            change_a = changes.get(sym_a, 0)
            change_b = changes.get(sym_b, 0)

            # Since they're inversely correlated, their moves should be opposite
            # If A goes up by X%, B should go down by ~X%
            # Divergence = how much the relationship is "broken" right now
            # Expected: change_a ≈ -change_b
            # Divergence: change_a + change_b (should be ~0 if in sync)

            # Check if A moved significantly and B hasn't responded
            for leader_sym, follower_sym in [(sym_a, sym_b), (sym_b, sym_a)]:
                leader_change = changes.get(leader_sym, 0)
                follower_change = changes.get(follower_sym, 0)

                # Leader must have moved significantly
                if abs(leader_change) < MOVE_THRESHOLD_PCT:
                    continue

                # The follower should have moved in the OPPOSITE direction
                # Expected follower change ≈ -leader_change
                expected_follower = -leader_change
                actual_follower = follower_change

                # Lag = difference between expected and actual follower move
                lag = abs(expected_follower - actual_follower)

                # Remaining lag gap:
                if expected_follower < 0:
                    remaining_lag = follower_change - expected_follower
                else:
                    remaining_lag = expected_follower - follower_change

                if remaining_lag >= MOVE_THRESHOLD_PCT and remaining_lag > best_divergence:
                    best_divergence = remaining_lag
                    predicted_dir = "DOWN" if expected_follower < 0 else "UP"

                    leader_price = self.tracker.get_latest(leader_sym)
                    follower_price = self.tracker.get_latest(follower_sym)

                    if leader_price and follower_price:
                        best_signal = {
                            "leader": leader_sym,
                            "follower": follower_sym,
                            "leader_change": leader_change,
                            "follower_change": follower_change,
                            "predicted_dir": predicted_dir,
                            "leader_price": leader_price.price,
                            "follower_price": follower_price.price,
                            "window": window,
                            "divergence": lag,
                        }

        if best_signal is None:
            return None

        # Check cooldown
        follower_key = best_signal["follower"]
        if follower_key in self.last_prediction_time:
            if now - self.last_prediction_time[follower_key] < COOLDOWN_SEC:
                return None

        # Create prediction
        self.prediction_counter += 1
        pred = Prediction(
            id=self.prediction_counter,
            created_at=datetime.now(timezone.utc).isoformat(),
            leader=LEADER_SYMBOLS[best_signal["leader"]],
            follower=LEADER_SYMBOLS[best_signal["follower"]],
            leader_move_pct=round(best_signal["leader_change"], 4),
            follower_price_at_prediction=best_signal["follower_price"],
            leader_price_at_prediction=best_signal["leader_price"],
            predicted_direction=best_signal["predicted_dir"],
            timeout_at=now + PREDICTION_TIMEOUT_SEC,
        )

        self.last_prediction_time[follower_key] = now
        return pred

    def _validate_predictions(self, now: float):
        """Check active predictions and validate them."""
        still_active = []

        for pred in self.active_predictions:
            follower_sym = [k for k, v in LEADER_SYMBOLS.items() if v == pred.follower][0]
            current = self.tracker.get_latest(follower_sym)

            if current is None:
                still_active.append(pred)
                continue

            current_price = current.price
            base_price = pred.follower_price_at_prediction

            if base_price == 0:
                pred.result = "No"
                pred.follower_actual_move_pct = 0
                pred.validated_at = datetime.now(timezone.utc).isoformat()
                pred.validation_time_sec = 0
                self._log_result(pred)
                continue

            actual_move_pct = ((current_price - base_price) / base_price) * 100

            # Check if prediction was correct
            correct = False
            if pred.predicted_direction == "UP" and actual_move_pct >= VALIDATION_MIN_MOVE_PCT:
                correct = True
            elif pred.predicted_direction == "DOWN" and actual_move_pct <= -VALIDATION_MIN_MOVE_PCT:
                correct = True

            # Either timed out or validated
            timed_out = now >= pred.timeout_at

            if correct or timed_out:
                pred.result = "Yes" if correct else "No"
                pred.follower_actual_move_pct = round(actual_move_pct, 4)
                pred.validated_at = datetime.now(timezone.utc).isoformat()
                pred.validation_time_sec = round(now - (pred.timeout_at - PREDICTION_TIMEOUT_SEC), 1)
                self._log_result(pred)
            else:
                still_active.append(pred)

        self.active_predictions = still_active

    def _log_result(self, pred: Prediction):
        """Log prediction result to console and file."""
        if pred.result == "Yes":
            self.correct_predictions += 1
            icon = "✅"
        else:
            self.incorrect_predictions += 1
            icon = "❌"

        self.total_predictions += 1
        accuracy = (self.correct_predictions / self.total_predictions * 100
                    if self.total_predictions > 0 else 0)

        self.logger.info(
            f"{icon} Prediction #{pred.id}: {pred.follower} {pred.predicted_direction} → "
            f"{pred.result} │ Move: {pred.follower_actual_move_pct:+.4f}% │ "
            f"Time: {pred.validation_time_sec}s │ "
            f"Running: {self.correct_predictions}/{self.total_predictions} ({accuracy:.1f}%)"
        )

        # Append to JSONL log
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(pred.to_dict()) + "\n")
        except Exception as e:
            self.logger.warning(f"Failed to write log: {e}")

        # Periodic detailed stats
        if self.total_predictions % STATS_DISPLAY_INTERVAL == 0:
            self._print_stats()

    def _print_stats(self):
        """Print detailed statistics."""
        total = self.total_predictions
        if total == 0:
            return

        accuracy = self.correct_predictions / total * 100

        self.logger.info("=" * 70)
        self.logger.info(f"📊 STATS after {total} predictions")
        self.logger.info(f"   ✅ Correct: {self.correct_predictions}  │  ❌ Incorrect: {self.incorrect_predictions}")
        self.logger.info(f"   🎯 Accuracy: {accuracy:.1f}%")
        self.logger.info(f"   ⏳ Active predictions: {len(self.active_predictions)}")
        self.logger.info("=" * 70)

    def run(self):
        """Main loop — runs continuously."""
        symbols = list(LEADER_SYMBOLS.keys())

        self.logger.info("=" * 70)
        self.logger.info("🚀 SOXSB ↔ SOXLB Lag Prediction Engine Started")
        self.logger.info(f"   Monitoring: {', '.join(LEADER_SYMBOLS.values())}")
        self.logger.info(f"   Move threshold: {MOVE_THRESHOLD_PCT}%")
        self.logger.info(f"   Prediction timeout: {PREDICTION_TIMEOUT_SEC}s")
        self.logger.info(f"   Cooldown: {COOLDOWN_SEC}s between predictions")
        self.logger.info(f"   Poll interval: {POLL_INTERVAL_SEC}s")
        self.logger.info(f"   Log file: {LOG_FILE}")
        self.logger.info("=" * 70)

        # Warmup phase — collect initial price history
        self.logger.info("⏳ Warming up... collecting initial price data")
        warmup_start = time.time()
        warmup_duration = max(15, min(self.lookback_windows))  # at least 15s

        error_streak = 0
        max_errors = 10

        while True:
            try:
                now = time.time()

                # Fetch prices
                prices = self.client.get_prices_bulk(symbols)
                for sym, price in prices.items():
                    self.tracker.add_price(sym, price, now)
                error_streak = 0

                # During warmup, just collect data
                if now - warmup_start < warmup_duration:
                    remaining = int(warmup_duration - (now - warmup_start))
                    if int(now) % 5 == 0:  # print every ~5 seconds
                        prices_str = " │ ".join(
                            f"{LEADER_SYMBOLS[s]}: ${p:.2f}" for s, p in prices.items()
                        )
                        self.logger.info(f"   Warmup ({remaining}s left) │ {prices_str}")
                    time.sleep(POLL_INTERVAL_SEC)
                    continue

                # ── Live Phase ──

                # Display current prices with changes
                price_parts = []
                for sym in symbols:
                    name = LEADER_SYMBOLS[sym]
                    price = prices[sym]
                    chg_30 = self.tracker.get_pct_change(sym, 30)
                    chg_60 = self.tracker.get_pct_change(sym, 60)
                    chg_str_30 = f"{chg_30:+.3f}%" if chg_30 is not None else "n/a"
                    chg_str_60 = f"{chg_60:+.3f}%" if chg_60 is not None else "n/a"
                    price_parts.append(f"{name}: ${price:.2f} (30s:{chg_str_30} 60s:{chg_str_60})")

                # Only log prices every 10 seconds to reduce noise
                if int(now) % 10 == 0:
                    self.logger.info(f"💹 {' │ '.join(price_parts)}")

                # Check for new prediction signals
                pred = self._check_for_lag_signal(now)
                if pred:
                    self.active_predictions.append(pred)
                    self.predictions.append(pred)

                    self.logger.info(
                        f"🔮 PREDICTION #{pred.id}: {pred.leader} moved {pred.leader_move_pct:+.3f}% → "
                        f"Predict {pred.follower} will go {pred.predicted_direction} "
                        f"(from ${pred.follower_price_at_prediction:.2f})"
                    )

                # Validate active predictions
                self._validate_predictions(now)

                time.sleep(POLL_INTERVAL_SEC)

            except requests.exceptions.RequestException as e:
                error_streak += 1
                if error_streak >= max_errors:
                    self.logger.error(f"💀 {max_errors} consecutive API errors. Stopping.")
                    break
                self.logger.warning(f"⚠️  API error ({error_streak}/{max_errors}): {e}")
                time.sleep(5)  # Back off on errors

            except KeyboardInterrupt:
                self.logger.info("\n⏹️  Stopped by user.")
                self._print_stats()
                break


# ──────────────────────────── Entrypoint ────────────────────────────

def main():
    # Allow graceful shutdown
    engine = LagPredictionEngine()

    def handle_signal(sig, frame):
        print("\n⏹️  Shutting down...")
        engine._print_stats()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)

    engine.run()


if __name__ == "__main__":
    main()
