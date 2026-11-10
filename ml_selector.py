"""
ml_selector.py — Online Adaptive Machine Learning for Dynamic Coin Selection.

Integrates continuous online learning (SGD / Logistic Regression with Running
Standardization, L2 Regularization, and Contextual Bandit Exploration)
to dynamically rank and filter crypto pairs based on real microstructure features.

As trades complete, the model updates weights on the fly, automatically adapting
to changing market regimes (trending, chop, volatility spikes).
"""

import json
import math
import os
import sqlite3
import time
from typing import Dict, Any, Tuple, Optional, List
import numpy as np

# Default feature definitions
FEATURE_NAMES = [
    "book_ratio",            # Top 5 bids qty / top 5 asks qty
    "order_book_imbalance", # Standardized OBI: (Bids - Asks) / (Bids + Asks) across top 10 levels
    "momentum_5m",           # 5m candle return %
    "volatility_1m",         # 1m candle range %
    "taker_buy_ratio",       # Aggressive taker buy ratio (0.0 to 1.0)
    "vwap_score",            # Proximity to VWAP
    "spread_pct",            # Bid-Ask spread % (inverted penalty)
    "tick_ratio",            # Tick resolution hurdle (inverted penalty)
    "cumulative_ask_usdt",   # Resistance asks to target (inverted penalty)
]

# Sensible priors so the model is immediately smart on trade #1
DEFAULT_PRIOR_WEIGHTS = {
    "book_ratio": 1.2,
    "order_book_imbalance": 2.5,
    "momentum_5m": 1.0,
    "volatility_1m": 1.5,
    "taker_buy_ratio": 2.2,
    "vwap_score": 1.0,
    "spread_pct": -2.5,
    "tick_ratio": -1.5,
    "cumulative_ask_usdt": -1.0,
}


class AdaptiveCoinSelector:
    """
    Online Logistic Regression model with:
    1. Welford's running feature normalization (tracks mean and variance continuously).
    2. AdaGrad-style adaptive learning rate per feature with L2 weight decay.
    3. Contextual Bandit confidence bonus (LinUCB style) for healthy exploration.
    4. Full state serialization to/from SQLite or JSON.
    """

    def __init__(
        self,
        features: Optional[List[str]] = None,
        base_lr: float = 0.05,
        l2_reg: float = 0.005,
        exploration_alpha: float = 0.15,
    ):
        self.features = features or list(FEATURE_NAMES)
        self.d = len(self.features)
        self.base_lr = base_lr
        self.l2_reg = l2_reg
        self.exploration_alpha = exploration_alpha

        # Weights and bias
        self.weights = np.array([DEFAULT_PRIOR_WEIGHTS.get(f, 0.0) for f in self.features], dtype=float)
        self.bias = 0.0

        # Gradient accumulator for adaptive learning rate (AdaGrad)
        self.grad_accum = np.ones(self.d, dtype=float) * 1.0
        self.bias_accum = 1.0

        # Contextual Bandit diagonal covariance accumulator (for UCB exploration)
        self.diag_cov = np.ones(self.d, dtype=float) * 2.0

        # Running statistics for standardization (Welford's algorithm)
        self.n_samples = 1
        self.running_mean = np.zeros(self.d, dtype=float)
        self.running_m2 = np.ones(self.d, dtype=float)

        # Performance metrics
        self.total_updates = 0
        self.total_wins = 0
        self.total_losses = 0
        self.recent_history: List[Dict[str, Any]] = []

    def _extract_vector(self, metrics: Dict[str, Any]) -> np.ndarray:
        """Extract feature values into raw numpy array."""
        vec = []
        for f in self.features:
            val = metrics.get(f, 0.0)
            if val is None or math.isnan(val) or math.isinf(val):
                val = 0.0
            # Special scaling for large quantities
            if f == "cumulative_ask_usdt":
                val = min(float(val) / 500.0, 10.0)
            elif f == "tick_ratio":
                val = float(val) * 1000.0
            else:
                val = float(val)
            vec.append(val)
        return np.array(vec, dtype=float)

    def _update_running_stats(self, x: np.ndarray):
        """Update running mean and variance without look-ahead bias."""
        self.n_samples += 1
        delta = x - self.running_mean
        self.running_mean += delta / self.n_samples
        delta2 = x - self.running_mean
        self.running_m2 += delta * delta2

    def _standardize(self, x: np.ndarray) -> np.ndarray:
        """Normalize feature vector using running statistics."""
        denom = max(self.n_samples - 1, 1)
        variance = np.maximum(self.running_m2 / denom, 1e-4)
        std_dev = np.sqrt(variance)
        return np.clip((x - self.running_mean) / std_dev, -4.0, 4.0)

    def predict_proba(self, metrics: Dict[str, Any]) -> float:
        """Compute estimated win probability P(Win | metrics) using sigmoid."""
        x = self._extract_vector(metrics)
        x_norm = self._standardize(x)
        z = float(np.dot(self.weights, x_norm) + self.bias)
        z = np.clip(z, -15.0, 15.0)
        return 1.0 / (1.0 + math.exp(-z))

    def score_candidate(self, metrics: Dict[str, Any]) -> Tuple[float, float]:
        """
        Calculates a dynamic score combining:
        1. Model predicted win probability P(Win | features).
        2. Upper Confidence Bound (LinUCB exploration bonus) to prevent bandit blindness.
        Returns: (final_score, p_win).
        """
        x = self._extract_vector(metrics)
        x_norm = self._standardize(x)

        z = float(np.dot(self.weights, x_norm) + self.bias)
        z = np.clip(z, -15.0, 15.0)
        p_win = 1.0 / (1.0 + math.exp(-z))

        # LinUCB-style uncertainty bonus: higher for unexplored feature spaces
        uncertainty = float(np.sqrt(np.sum((x_norm ** 2) / np.maximum(self.diag_cov, 1e-3))))
        exploration_bonus = self.exploration_alpha * uncertainty

        # Final score scaled to 0-10 range for seamless integration
        score = (p_win * 7.0) + min(exploration_bonus * 2.0, 3.0)
        return round(score, 4), round(p_win, 4)

    def update(
        self,
        metrics: Dict[str, Any],
        won: bool,
        pnl_usdt: float = 0.0,
        duration_sec: float = 0.0,
        symbol: str = "",
    ) -> Dict[str, Any]:
        """
        Online gradient descent step with AdaGrad and L2 regularization.
        Executed immediately after trade cycle settles.
        """
        x = self._extract_vector(metrics)
        self._update_running_stats(x)
        x_norm = self._standardize(x)

        # Forward pass
        z = float(np.dot(self.weights, x_norm) + self.bias)
        z = np.clip(z, -15.0, 15.0)
        pred_p = 1.0 / (1.0 + math.exp(-z))

        target = 1.0 if won else 0.0
        error = pred_p - target

        # Gradient with L2 regularization
        grad_w = (error * x_norm) + (self.l2_reg * self.weights)
        grad_b = error

        # AdaGrad accumulator updates
        self.grad_accum += grad_w ** 2
        self.bias_accum += grad_b ** 2

        # Weight updates
        lr_w = self.base_lr / (np.sqrt(self.grad_accum) + 1e-6)
        lr_b = self.base_lr / (math.sqrt(self.bias_accum) + 1e-6)

        self.weights -= lr_w * grad_w
        self.bias -= lr_b * grad_b

        # Update exploration covariance diagonal
        self.diag_cov += x_norm ** 2

        # Update tracking counters
        self.total_updates += 1
        if won:
            self.total_wins += 1
        else:
            self.total_losses += 1

        update_summary = {
            "symbol": symbol,
            "won": won,
            "pnl_usdt": pnl_usdt,
            "duration_sec": duration_sec,
            "pred_p": round(pred_p, 4),
            "target": target,
            "error": round(error, 4),
            "ts": time.time(),
        }
        self.recent_history.append(update_summary)
        if len(self.recent_history) > 50:
            self.recent_history.pop(0)

        return update_summary

    def to_dict(self) -> Dict[str, Any]:
        """Serialize model state to dictionary."""
        return {
            "features": self.features,
            "base_lr": self.base_lr,
            "l2_reg": self.l2_reg,
            "exploration_alpha": self.exploration_alpha,
            "weights": self.weights.tolist(),
            "bias": float(self.bias),
            "grad_accum": self.grad_accum.tolist(),
            "bias_accum": float(self.bias_accum),
            "diag_cov": self.diag_cov.tolist(),
            "n_samples": self.n_samples,
            "running_mean": self.running_mean.tolist(),
            "running_m2": self.running_m2.tolist(),
            "total_updates": self.total_updates,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "recent_history": self.recent_history,
        }

    def from_dict(self, data: Dict[str, Any]):
        """Load model state from dictionary with backward-compatible feature schema migration."""
        if not data:
            return
        saved_features = data.get("features", [])
        if saved_features == self.features:
            self.weights = np.array(data.get("weights", self.weights), dtype=float)
            self.grad_accum = np.array(data.get("grad_accum", self.grad_accum), dtype=float)
            self.diag_cov = np.array(data.get("diag_cov", self.diag_cov), dtype=float)
            self.running_mean = np.array(data.get("running_mean", self.running_mean), dtype=float)
            self.running_m2 = np.array(data.get("running_m2", self.running_m2), dtype=float)
        else:
            new_weights = []
            new_grad = []
            new_cov = []
            new_mean = []
            new_m2 = []
            for f in self.features:
                if f in saved_features:
                    idx = saved_features.index(f)
                    new_weights.append(data.get("weights", [])[idx] if idx < len(data.get("weights", [])) else DEFAULT_PRIOR_WEIGHTS.get(f, 0.0))
                    new_grad.append(data.get("grad_accum", [])[idx] if idx < len(data.get("grad_accum", [])) else 1.0)
                    new_cov.append(data.get("diag_cov", [])[idx] if idx < len(data.get("diag_cov", [])) else 2.0)
                    new_mean.append(data.get("running_mean", [])[idx] if idx < len(data.get("running_mean", [])) else 0.0)
                    new_m2.append(data.get("running_m2", [])[idx] if idx < len(data.get("running_m2", [])) else 1.0)
                else:
                    new_weights.append(DEFAULT_PRIOR_WEIGHTS.get(f, 0.0))
                    new_grad.append(1.0)
                    new_cov.append(2.0)
                    new_mean.append(0.0)
                    new_m2.append(1.0)
            self.weights = np.array(new_weights, dtype=float)
            self.grad_accum = np.array(new_grad, dtype=float)
            self.diag_cov = np.array(new_cov, dtype=float)
            self.running_mean = np.array(new_mean, dtype=float)
            self.running_m2 = np.array(new_m2, dtype=float)

        self.d = len(self.features)
        self.base_lr = float(data.get("base_lr", self.base_lr))
        self.l2_reg = float(data.get("l2_reg", self.l2_reg))
        self.exploration_alpha = float(data.get("exploration_alpha", self.exploration_alpha))
        self.bias = float(data.get("bias", self.bias))
        self.bias_accum = float(data.get("bias_accum", self.bias_accum))
        self.n_samples = int(data.get("n_samples", self.n_samples))
        self.total_updates = int(data.get("total_updates", 0))
        self.total_wins = int(data.get("total_wins", 0))
        self.total_losses = int(data.get("total_losses", 0))
        self.recent_history = data.get("recent_history", [])

    def get_feature_importances(self) -> List[Dict[str, Any]]:
        """Return human-readable feature weights and directions for dashboard UI."""
        items = []
        for name, w in zip(self.features, self.weights):
            items.append({
                "feature": name,
                "weight": round(float(w), 4),
                "influence": "Bullish / Favored" if w > 0 else "Bearish / Penalized",
                "abs_weight": round(abs(float(w)), 4),
            })
        items.sort(key=lambda x: x["abs_weight"], reverse=True)
        return items


# ===========================================================================
# SQLite Persistence & Process-Safe Interface
# ===========================================================================

_GLOBAL_SELECTOR: Optional[AdaptiveCoinSelector] = None
_LAST_LOAD_TS: float = 0.0


def init_ml_tables(conn: sqlite3.Connection):
    """Ensure database tables for ML state and cycle feature logging exist."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS ml_model_state (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        state_json  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS cycle_features (
        cycle_id        INTEGER PRIMARY KEY,
        symbol          TEXT NOT NULL,
        features_json   TEXT NOT NULL,
        p_win           REAL DEFAULT 0.5,
        ml_score        REAL DEFAULT 5.0,
        created_at      TEXT NOT NULL
    );
    """)


def get_selector(db_path: Optional[str] = None, force_reload: bool = False) -> AdaptiveCoinSelector:
    """
    Get or reload the synchronized AdaptiveCoinSelector instance.
    Reloads from SQLite if another process made updates or force_reload is True.
    """
    global _GLOBAL_SELECTOR, _LAST_LOAD_TS
    first_init = (_GLOBAL_SELECTOR is None)
    if first_init:
        _GLOBAL_SELECTOR = AdaptiveCoinSelector()

    if db_path and os.path.exists(db_path):
        now = time.time()
        # Reload if first init, forced, or after 2s interval
        if first_init or force_reload or (now - _LAST_LOAD_TS > 2.0):
            try:
                conn = sqlite3.connect(db_path, timeout=10)
                init_ml_tables(conn)
                row = conn.execute("SELECT state_json FROM ml_model_state WHERE id = 1").fetchone()
                if row and row[0]:
                    state = json.loads(row[0])
                    _GLOBAL_SELECTOR.from_dict(state)
                conn.close()
                _LAST_LOAD_TS = now
            except Exception:
                pass

    return _GLOBAL_SELECTOR


def save_selector_state(selector: AdaptiveCoinSelector, db_path: str):
    """Save model state atomically into SQLite."""
    global _GLOBAL_SELECTOR, _LAST_LOAD_TS
    _GLOBAL_SELECTOR = selector
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        init_ml_tables(conn)
        state_json = json.dumps(selector.to_dict())
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            """
            INSERT INTO ml_model_state (id, state_json, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (state_json, now_iso),
        )
        conn.commit()
        conn.close()
        _LAST_LOAD_TS = time.time()
    except Exception as e:
        print(f"[ML] Error saving model state to DB: {e}")


def record_cycle_features(
    db_path: str,
    cycle_id: int,
    symbol: str,
    features: Dict[str, Any],
    p_win: float = 0.5,
    ml_score: float = 5.0,
):
    """Log the feature vector that caused this cycle to be selected."""
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        init_ml_tables(conn)
        clean_features = {
            f: float(features.get(f, 0.0))
            for f in FEATURE_NAMES
            if f in features and features.get(f) is not None
        }
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            """
            INSERT OR REPLACE INTO cycle_features (cycle_id, symbol, features_json, p_win, ml_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (cycle_id, symbol, json.dumps(clean_features), p_win, ml_score, now_iso),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[ML] Error recording cycle features for cycle #{cycle_id}: {e}")


def update_ml_from_cycle(
    db_path: str,
    cycle_id: int,
    pnl_usdt: float,
    duration_sec: float = 0.0,
) -> Optional[Dict[str, Any]]:
    """
    Retrieve features for completed cycle, execute on-the-fly online learning update,
    and persist updated brain state to SQLite.
    """
    if pnl_usdt is None:
        return None

    try:
        conn = sqlite3.connect(db_path, timeout=30)
        init_ml_tables(conn)
        row = conn.execute(
            "SELECT symbol, features_json FROM cycle_features WHERE cycle_id = ?",
            (cycle_id,),
        ).fetchone()
        conn.close()

        if not row:
            return None

        symbol, features_json = row[0], row[1]
        features = json.loads(features_json) if features_json else {}
        if not features:
            return None

        selector = get_selector(db_path)
        won = (pnl_usdt > 0.0)
        update_info = selector.update(
            metrics=features,
            won=won,
            pnl_usdt=pnl_usdt,
            duration_sec=duration_sec,
            symbol=symbol,
        )
        save_selector_state(selector, db_path)
        return update_info

    except Exception as e:
        print(f"[ML] Error in on-the-fly learning update for cycle #{cycle_id}: {e}")
        return None
