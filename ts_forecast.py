"""
ts_forecast.py — Advanced Multi-Horizon Time Series Forecasting & First Candle Analysis (FCA)
for High-Profit Quantitative Crypto Trading.

Architecture & Quantitative Foundations:
1. Online 3D Kinematic Kalman Filter (Zero-lag denoised price, velocity, acceleration).
2. Rolling Autoregressive Distributed Lag (ARDL) Ridge Regression (5m, 15m, 30m forward returns).
3. Fast Fourier Transform (FFT) Spectral Cycle Decomposition & Hurst Exponent Regime Classifier.
4. Garman-Klass & Parkinson Extreme-Value Volatility Cones.
5. First Candle Analysis (FCA) Engine (Candle Body Dominance, Wick Absorption Ratio, RVol Expansion,
   and Structural Stop/Target Anchoring).
6. Cross-Sectional Information Ratio (FIR) Ranking & In-Trade Structural Thesis Monitor.
"""

from __future__ import annotations

import math
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Tuple, Optional
import numpy as np

from sklearn.linear_model import Ridge

# ---------------------------------------------------------------------------
# 1. ONLINE KINEMATIC KALMAN FILTER (Zero-Lag Trend & Velocity Tracking)
# ---------------------------------------------------------------------------

class KinematicKalmanFilter:
    """
    3D Kinematic State-Space Estimator:
    State: x = [position (price), velocity (return rate/sec), acceleration]^T
    Filters out micro-tick market noise and bid-ask bounce without lag.
    """
    def __init__(self, dt: float = 1.0, process_noise_std: float = 0.005, measurement_noise_std: float = 0.002):
        self.dt = dt
        self.F = np.array([
            [1.0, dt, 0.5 * dt * dt],
            [0.0, 1.0, dt],
            [0.0, 0.0, 1.0]
        ], dtype=float)

        self.H = np.array([[1.0, 0.0, 0.0]], dtype=float)

        q = process_noise_std ** 2
        self.Q = np.array([
            [(dt**5)/20.0, (dt**4)/8.0, (dt**3)/6.0],
            [(dt**4)/8.0,  (dt**3)/3.0, (dt**2)/2.0],
            [(dt**3)/6.0,  (dt**2)/2.0, dt]
        ], dtype=float) * q

        self.R = np.array([[measurement_noise_std ** 2]], dtype=float)
        self.x = np.zeros(3, dtype=float)
        self.P = np.eye(3, dtype=float) * 1.0
        self.initialized = False

    def reset(self, initial_price: float):
        self.x = np.array([initial_price, 0.0, 0.0], dtype=float)
        self.P = np.eye(3, dtype=float) * 0.1
        self.initialized = True

    def update(self, price: float) -> Tuple[float, float, float]:
        if not self.initialized or self.x[0] <= 0:
            self.reset(price)
            return price, 0.0, 0.0

        x_pred = self.F @ self.x
        P_pred = self.F @ self.P @ self.F.T + self.Q

        z = np.array([price], dtype=float)
        y = z - (self.H @ x_pred)
        S = self.H @ P_pred @ self.H.T + self.R
        K = P_pred @ self.H.T @ np.linalg.inv(S)

        self.x = x_pred + (K @ y).flatten()
        self.P = (np.eye(3) - K @ self.H) @ P_pred

        filtered_price = float(self.x[0])
        velocity_pct = (float(self.x[1]) / filtered_price * 100.0) if filtered_price > 0 else 0.0
        acceleration = float(self.x[2])
        return filtered_price, velocity_pct, acceleration


# ---------------------------------------------------------------------------
# 2. SPECTRAL & REGIME ANALYSIS (FFT Dominant Cycle & Hurst Exponent)
# ---------------------------------------------------------------------------

def compute_fft_cycle(prices: np.ndarray) -> Tuple[float, float]:
    """Computes dominant cycle period and phase using Fast Fourier Transform."""
    n = len(prices)
    if n < 16:
        return 0.0, 0.0

    detrended = prices - np.linspace(prices[0], prices[-1], n)
    fft_vals = np.fft.rfft(detrended)
    freqs = np.fft.rfftfreq(n)

    mags = np.abs(fft_vals[1:])
    if len(mags) == 0:
        return 0.0, 0.0

    dom_idx = np.argmax(mags) + 1
    dom_freq = freqs[dom_idx]
    dom_period = (1.0 / dom_freq) if dom_freq > 0 else 0.0
    phase = float(np.angle(fft_vals[dom_idx]))
    return float(dom_period), phase


def compute_hurst_exponent(prices: np.ndarray, max_lags: int = 20) -> float:
    """Estimates the Hurst Exponent (H) via Rescaled Range (R/S) analysis."""
    n = len(prices)
    if n < 30:
        return 0.50

    try:
        returns = np.diff(np.log(prices))
        lags = range(2, min(max_lags, n // 4))
        tau = []
        for lag in lags:
            chunks = len(returns) // lag
            if chunks < 1:
                continue
            rs_list = []
            for i in range(chunks):
                sub = returns[i * lag : (i + 1) * lag]
                mean_sub = np.mean(sub)
                deviations = sub - mean_sub
                cum_dev = np.cumsum(deviations)
                r = np.max(cum_dev) - np.min(cum_dev)
                s = np.std(sub)
                if s > 1e-9:
                    rs_list.append(r / s)
            if rs_list:
                tau.append((math.log(lag), math.log(np.mean(rs_list))))

        if len(tau) >= 3:
            x_vals = [t[0] for t in tau]
            y_vals = [t[1] for t in tau]
            slope, _ = np.polyfit(x_vals, y_vals, 1)
            return float(max(0.0, min(1.0, slope)))
    except Exception:
        pass
    return 0.50


# ---------------------------------------------------------------------------
# 3. EXTREME-VALUE VOLATILITY FORECASTING (Garman-Klass & Parkinson)
# ---------------------------------------------------------------------------

def compute_volatility_metrics(highs: np.ndarray, lows: np.ndarray, opens: np.ndarray, closes: np.ndarray) -> Dict[str, float]:
    """Computes extreme-value volatility estimators: Parkinson, Garman-Klass, and EWMA."""
    n = len(closes)
    if n < 5:
        return {"vol_gk_pct": 0.20, "vol_parkinson_pct": 0.20, "vol_forecast_pct": 0.20}

    h = np.maximum(highs, 1e-8)
    l = np.maximum(lows, 1e-8)
    o = np.maximum(opens, 1e-8)
    c = np.maximum(closes, 1e-8)

    log_hl = np.log(h / l)
    parkinson_var = (log_hl ** 2) / (4.0 * math.log(2.0))
    vol_parkinson = float(np.sqrt(np.mean(parkinson_var)) * 100.0)

    log_co = np.log(c / o)
    gk_var = 0.5 * (log_hl ** 2) - (2.0 * math.log(2.0) - 1.0) * (log_co ** 2)
    vol_gk = float(np.sqrt(np.maximum(0.0, np.mean(gk_var))) * 100.0)

    returns = np.diff(np.log(c)) * 100.0
    weights = np.array([0.94 ** i for i in range(len(returns))])[::-1]
    weights /= np.sum(weights)
    ewma_vol = float(np.sqrt(np.sum(weights * (returns ** 2))))

    vol_forecast = float(0.50 * vol_gk + 0.30 * vol_parkinson + 0.20 * ewma_vol)
    vol_forecast = max(0.05, min(3.0, vol_forecast))

    return {
        "vol_gk_pct": vol_gk,
        "vol_parkinson_pct": vol_parkinson,
        "vol_forecast_pct": vol_forecast,
    }


# ---------------------------------------------------------------------------
# 4. ROLLING AUTOREGRESSIVE DISTRIBUTED LAG (ARDL) RIDGE MODEL
# ---------------------------------------------------------------------------

def fit_rolling_ardl(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    volumes: np.ndarray,
    taker_buys: np.ndarray,
    horizon: int = 5,
    lags: Optional[List[int]] = None,
) -> float:
    """Fits online ARDL Ridge model over historical bars to predict forward return."""
    if lags is None:
        lags = [1, 2, 3, 5, 8]

    n = len(closes)
    if n < (max(lags) + horizon + 10):
        return float((closes[-1] - closes[-horizon]) / closes[-horizon] * 100.0) if n > horizon else 0.0

    log_ret = np.diff(np.log(closes)) * 100.0
    hl_range = (highs[1:] - lows[1:]) / closes[1:] * 100.0
    valid_vol = volumes[1:] > 0
    tbr = np.full_like(taker_buys[1:], 0.5, dtype=float)
    np.divide(taker_buys[1:], volumes[1:], out=tbr, where=valid_vol)

    X = []
    y = []
    max_lag = max(lags)
    for i in range(max_lag, len(log_ret) - horizon):
        feat = [log_ret[i - l] for l in lags]
        feat.append(hl_range[i])
        feat.append(tbr[i] - 0.5)
        fwd_ret = (closes[i + 1 + horizon] - closes[i + 1]) / closes[i + 1] * 100.0
        X.append(feat)
        y.append(fwd_ret)

    if len(X) < 10:
        return 0.0

    X_mat = np.array(X)
    y_vec = np.array(y)

    model = Ridge(alpha=1.5)
    model.fit(X_mat, y_vec)

    latest_feat = np.array([[log_ret[-l] for l in lags] + [hl_range[-1], tbr[-1] - 0.5]])
    pred_return = float(model.predict(latest_feat)[0])
    return pred_return


# ---------------------------------------------------------------------------
# 5. FIRST CANDLE ANALYSIS (FCA) & STRUCTURAL PRICE ACTION CONFIRMATION
# ---------------------------------------------------------------------------

def analyze_first_candle(
    klines_5m: List[list],
    klines_1m: Optional[List[list]] = None,
    cur_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    First Candle Analysis (FCA) for Microstructure Confirmation & Asymmetric Risk Structuring.
    Evaluates the reference 5m candle (Candle -2) and developing candle (Candle -1):
      - Body Dominance: (Close - Low) / (High - Low)
      - Lower Wick (Absorption Ratio): (min(Open, Close) - Low) / Range
      - Upper Wick (Rejection Ratio): (High - max(Open, Close)) / Range
      - Relative Volume: V1 / SMA(V, 10)
    Generates structural entry, stop-loss, and expansion take-profit levels.
    """
    if not klines_5m or len(klines_5m) < 2:
        return {
            "fca_viable": True,
            "setup_type": "NEUTRAL",
            "body_dominance": 0.5,
            "lower_wick_ratio": 0.2,
            "upper_wick_ratio": 0.2,
            "relative_volume": 1.0,
            "fca_score_boost": 0.0,
            "first_candle_high": 0.0,
            "first_candle_low": 0.0,
            "first_candle_mid": 0.0,
            "retest_buy_level": 0.0,
            "structural_sl_pct": -0.75,
            "expansion_tp1_pct": 0.75,
            "expansion_tp2_pct": 1.50,
            "range_pct": 0.50,
        }

    k1 = klines_5m[-2]
    o1 = float(k1[1])
    h1 = float(k1[2])
    l1 = float(k1[3])
    c1 = float(k1[4])
    v1 = float(k1[5])

    p_now = cur_price or c1
    r1 = h1 - l1
    if r1 <= 0:
        r1 = p_now * 0.001

    is_green = c1 >= o1
    upper_wick = (h1 - max(o1, c1)) / r1
    lower_wick = (min(o1, c1) - l1) / r1
    body_dominance = (c1 - l1) / r1
    midpoint = (h1 + l1) / 2.0

    # 10-period volume average
    v_hist = [float(k[5]) for k in klines_5m[:-2]]
    v_ma = float(np.mean(v_hist)) if v_hist else v1
    rvol = (v1 / v_ma) if v_ma > 0 else 1.0

    range_pct = (r1 / l1 * 100.0) if l1 > 0 else 0.50

    # Structural retest buy: entry at midpoint or shallow pullback
    retest_buy = max(l1 + 0.382 * r1, min(h1, midpoint))

    # Structural Stop-Loss anchored 5% below First Candle Low
    sl_price = l1 - (0.05 * r1)
    structural_sl_pct = ((sl_price - p_now) / p_now * 100.0) if p_now > 0 else -0.75
    structural_sl_pct = max(-1.20, min(-0.50, structural_sl_pct))

    # Measured move expansion profit targets
    tp1_pct = max(0.55, min(1.20, max(0.60, 0.85 * range_pct)))
    tp2_pct = max(1.10, min(2.80, 1.65 * range_pct))

    # Candle Action Classification
    if is_green and body_dominance >= 0.65 and upper_wick <= 0.25 and rvol >= 0.70:
        setup_type = "BULLISH_EXPANSION"
        fca_viable = True
        score_boost = 4.5
    elif lower_wick >= 0.35 and c1 >= (l1 + 0.40 * r1):
        setup_type = "BULLISH_ABSORPTION"
        fca_viable = True
        score_boost = 3.5
    elif (not is_green and upper_wick >= 0.35) or (is_green and upper_wick >= 0.45):
        setup_type = "BEARISH_REJECTION"
        fca_viable = False
        score_boost = -7.0
    elif body_dominance <= 0.30:
        setup_type = "BEARISH_DOMINANCE"
        fca_viable = False
        score_boost = -8.0
    elif rvol < 0.35:
        setup_type = "LOW_VOLUME_CHOP"
        fca_viable = False
        score_boost = -4.0
    else:
        setup_type = "NEUTRAL_CHOP"
        fca_viable = (is_green and body_dominance >= 0.50)
        score_boost = 0.5 if fca_viable else -2.0

    return {
        "fca_viable": fca_viable,
        "setup_type": setup_type,
        "body_dominance": round(body_dominance, 3),
        "lower_wick_ratio": round(lower_wick, 3),
        "upper_wick_ratio": round(upper_wick, 3),
        "relative_volume": round(rvol, 2),
        "fca_score_boost": score_boost,
        "first_candle_high": h1,
        "first_candle_low": l1,
        "first_candle_mid": round(midpoint, 4),
        "retest_buy_level": round(retest_buy, 4),
        "structural_sl_price": round(sl_price, 4),
        "structural_sl_pct": round(structural_sl_pct, 4),
        "expansion_tp1_pct": round(tp1_pct, 4),
        "expansion_tp2_pct": round(tp2_pct, 4),
        "range_pct": round(range_pct, 4),
    }


# ---------------------------------------------------------------------------
# 6. ADVANCED TIME SERIES ENSEMBLE FORECASTER (TSF + FCA)
# ---------------------------------------------------------------------------

class AdvancedTimeSeriesForecaster:
    """
    Unified Forecasting Engine:
    Integrates 3D Kinematic Kalman Filter, Rolling ARDL Ridge, Spectral FFT,
    and First Candle Analysis (FCA) for asymmetric profit extraction.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.cache_ttl_sec = 12.0

    def forecast_symbol(
        self,
        symbol: str,
        klines_1m: List[list],
        klines_5m: Optional[List[list]] = None,
        bid_ask_spread_pct: float = 0.02,
        roundtrip_fee_pct: float = 0.15,
        order_book_imbalance: float = 0.0,
    ) -> Dict[str, Any]:
        """Generates full multi-horizon forecast, FCA setup, dynamic TP, and structural SL."""
        now = time.time()
        with self._lock:
            cached = self._cache.get(symbol)
            if cached and (now - cached["timestamp"]) < self.cache_ttl_sec:
                return cached["forecast"]

        if not klines_1m or len(klines_1m) < 20:
            return self._empty_forecast(symbol)

        opens = np.array([float(k[1]) for k in klines_1m])
        highs = np.array([float(k[2]) for k in klines_1m])
        lows = np.array([float(k[3]) for k in klines_1m])
        closes = np.array([float(k[4]) for k in klines_1m])
        volumes = np.array([float(k[5]) for k in klines_1m])
        taker_buys = np.array([float(k[9]) for k in klines_1m])

        cur_price = closes[-1]

        # 1. Kalman dynamic state filter
        kf = KinematicKalmanFilter(dt=1.0)
        filtered_p = cur_price
        velocity_pct = 0.0
        accel = 0.0
        for p in closes:
            filtered_p, velocity_pct, accel = kf.update(p)

        # 2. Volatility metrics
        vol_dict = compute_volatility_metrics(highs, lows, opens, closes)
        vol_1m = vol_dict["vol_forecast_pct"]

        # 3. Spectral FFT & Hurst exponent
        fft_period, fft_phase = compute_fft_cycle(closes)
        hurst = compute_hurst_exponent(closes)

        # 4. Rolling ARDL Ridge forecasts
        ardl_5m = fit_rolling_ardl(closes, highs, lows, volumes, taker_buys, horizon=5)
        ardl_15m = fit_rolling_ardl(closes, highs, lows, volumes, taker_buys, horizon=15)

        # 5. Kinematic Kalman extrapolation
        decay = 0.88
        cum_vel_5m = velocity_pct * sum(decay ** i for i in range(5))
        cum_vel_15m = velocity_pct * sum(decay ** i for i in range(15))

        # 6. Spectral harmonic phase boost
        spectral_boost = math.sin(-fft_phase) * 0.25 if fft_period > 8 else 0.0

        # 7. Multi-timeframe trend
        trend_5m_pct = 0.0
        if klines_5m and len(klines_5m) >= 3:
            c5 = [float(k[4]) for k in klines_5m]
            trend_5m_pct = (c5[-1] - c5[-3]) / c5[-3] * 100.0 if c5[-3] > 0 else 0.0

        regime_mult = 1.25 if hurst > 0.55 else (0.80 if hurst < 0.45 else 1.0)

        # Regime classified by Hurst exponent, Kinematic velocity, and Order Book Imbalance (OBI)
        if hurst > 0.55 and velocity_pct > 0.03 and order_book_imbalance >= -0.15:
            regime = "TRENDING_BULL"
        elif hurst > 0.55 and (velocity_pct < -0.05 or order_book_imbalance < -0.30):
            regime = "TRENDING_BEAR"
        elif hurst < 0.45:
            regime = "MEAN_REVERTING"
        else:
            regime = "CHOP_RANGING"

        tot_vol5 = np.sum(volumes[-5:])
        tot_tb5 = np.sum(taker_buys[-5:])
        taker_ratio = (tot_tb5 / tot_vol5) if tot_vol5 > 0 else 0.50

        # Ensemble Return Forecasts
        pred_return_5m = float(np.clip(
            (0.40 * ardl_5m + 0.35 * cum_vel_5m + 0.15 * spectral_boost + 0.10 * ((taker_ratio - 0.5) * 2.0)) * regime_mult,
            -3.0, 4.0
        ))
        pred_return_15m = float(np.clip(
            (0.45 * ardl_15m + 0.30 * cum_vel_15m + 0.15 * (trend_5m_pct * 0.5) + 0.10 * spectral_boost) * regime_mult,
            -4.0, 6.0
        ))

        # --- First Candle Analysis (FCA) Integration ---
        fca = analyze_first_candle(klines_5m, klines_1m, cur_price)

        # Dynamic Volatility & First Candle Anchored Targets
        sigma_15m = vol_1m * math.sqrt(15)
        raw_tp = max(0.55, min(2.50, max(pred_return_15m * 1.25, 0.55 + 0.60 * sigma_15m)))
        dynamic_tp_pct = max(raw_tp, fca["expansion_tp1_pct"])
        dynamic_sl_pct = fca["structural_sl_pct"]

        total_hurdle_pct = roundtrip_fee_pct + bid_ask_spread_pct
        excess_return = pred_return_15m - total_hurdle_pct
        fir = float(excess_return / sigma_15m) if sigma_15m > 0 else 0.0
        p_win = float(1.0 / (1.0 + math.exp(-max(-6.0, min(6.0, fir * 1.8)))))

        # Composite Forecast Score incorporating First Candle Analysis and Order Book Imbalance
        forecast_score = float(
            (pred_return_15m * 4.0)
            + (fir * 3.0)
            + (p_win * 5.0)
            + (max(0.0, velocity_pct) * 3.0)
            + (1.5 if regime == "TRENDING_BULL" else (-2.0 if regime == "TRENDING_BEAR" else 0.0))
            + ((taker_ratio - 0.50) * 3.0)
            + (order_book_imbalance * 4.5)
            + fca["fca_score_boost"]
            - (total_hurdle_pct * 10.0)
        )

        hurdle_cleared = (pred_return_15m > (total_hurdle_pct + 0.15)) and (fir > 0.25) and fca["fca_viable"]
        expected_hold_sec = int(max(240, min(900, (dynamic_tp_pct / max(0.04, abs(velocity_pct))) * 60.0))) if velocity_pct > 0 else 450

        forecast_result = {
            "symbol": symbol,
            "timestamp": now,
            "current_price": cur_price,
            "filtered_price": filtered_p,
            "pred_return_5m_pct": round(pred_return_5m, 4),
            "pred_return_15m_pct": round(pred_return_15m, 4),
            "forecast_tp_pct": round(dynamic_tp_pct, 4),
            "forecast_sl_pct": round(dynamic_sl_pct, 4),
            "vol_forecast_pct": round(vol_1m, 4),
            "sigma_15m_pct": round(sigma_15m, 4),
            "forecast_info_ratio": round(fir, 4),
            "win_probability": round(p_win, 4),
            "forecast_score": round(forecast_score, 4),
            "hurdle_cleared": hurdle_cleared,
            "kalman_velocity_pct": round(velocity_pct, 4),
            "hurst_exponent": round(hurst, 4),
            "regime": regime,
            "taker_ratio_5m": round(taker_ratio, 4),
            "expected_hold_sec": expected_hold_sec,
            "total_hurdle_pct": round(total_hurdle_pct, 4),
            "fca": fca,
            "fca_setup": fca["setup_type"],
            "fca_viable": fca["fca_viable"],
            "retest_buy_level": fca["retest_buy_level"],
            "structural_sl_price": fca["structural_sl_price"],
            "order_book_imbalance": round(order_book_imbalance, 4),
        }

        with self._lock:
            self._cache[symbol] = {
                "timestamp": now,
                "forecast": forecast_result,
            }

        return forecast_result

    def _empty_forecast(self, symbol: str) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "timestamp": time.time(),
            "current_price": 0.0,
            "filtered_price": 0.0,
            "pred_return_5m_pct": 0.0,
            "pred_return_15m_pct": 0.0,
            "forecast_tp_pct": 0.75,
            "forecast_sl_pct": -0.75,
            "vol_forecast_pct": 0.20,
            "sigma_15m_pct": 0.75,
            "forecast_info_ratio": 0.0,
            "win_probability": 0.50,
            "forecast_score": -999.0,
            "hurdle_cleared": False,
            "kalman_velocity_pct": 0.0,
            "hurst_exponent": 0.50,
            "regime": "CHOP_RANGING",
            "taker_ratio_5m": 0.50,
            "expected_hold_sec": 450,
            "total_hurdle_pct": 0.20,
            "fca": {},
            "fca_setup": "UNKNOWN",
            "fca_viable": False,
            "retest_buy_level": 0.0,
            "structural_sl_price": 0.0,
        }


# Singleton instance
_GLOBAL_FORECASTER = AdvancedTimeSeriesForecaster()


def get_forecaster() -> AdvancedTimeSeriesForecaster:
    global _GLOBAL_FORECASTER
    return _GLOBAL_FORECASTER


# ---------------------------------------------------------------------------
# 7. CONCURRENT CROSS-SECTIONAL FORECAST RANKING
# ---------------------------------------------------------------------------

def rank_pairs_with_forecasting(
    client: Any,
    candidate_symbols: List[str],
    max_workers: int = 6,
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """
    Evaluates candidate USDT pairs concurrently using TSF + First Candle Analysis.
    Ranks pairs by Forecast Score, filtering out structural candle rejections.
    """
    if not candidate_symbols:
        return [], {}

    forecaster = get_forecaster()
    forecast_map: Dict[str, Dict[str, Any]] = {}
    lock = threading.Lock()

    def _eval_symbol(sym: str):
        try:
            # 60 1m klines and 12 5m klines gives 1 hour of multi-horizon depth
            k_1m = client.get_klines(symbol=sym, interval="1m", limit=60)
            k_5m = client.get_klines(symbol=sym, interval="5m", limit=12)

            obi = 0.0
            try:
                ob = client.get_order_book(symbol=sym, limit=10)
                b_usdt = sum(float(b[0]) * float(b[1]) for b in ob.get("bids", [])[:10])
                a_usdt = sum(float(a[0]) * float(a[1]) for a in ob.get("asks", [])[:10])
                tot = b_usdt + a_usdt
                if tot > 0:
                    obi = (b_usdt - a_usdt) / tot
            except Exception:
                pass
            fc = forecaster.forecast_symbol(sym, k_1m, k_5m, order_book_imbalance=obi)
            with lock:
                forecast_map[sym] = fc
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=min(max_workers, len(candidate_symbols))) as executor:
        list(executor.map(_eval_symbol, candidate_symbols))

    if not forecast_map:
        return candidate_symbols, {}

    ranked = sorted(
        candidate_symbols,
        key=lambda s: forecast_map.get(s, {}).get("forecast_score", -999.0),
        reverse=True
    )

    return ranked, forecast_map


# ---------------------------------------------------------------------------
# 8. IN-TRADE POSITION FORECAST & STRUCTURAL THESIS MONITOR
# ---------------------------------------------------------------------------

def evaluate_position_thesis(
    client: Any,
    symbol: str,
    entry_cost: float,
    current_value: float,
    buy_fee_usdt: float,
    elapsed_sec: float,
    forecast: Dict[str, Any],
    peak_pnl: float = 0.0,
) -> Tuple[str, str, float]:
    """
    Evaluates an active trade against the TSF + First Candle structural thesis.
    Actions:
      - 'TAKE_PROFIT': Target profit reached or trailing take-profit triggered.
      - 'PASSIVE_SCRATCH': Stagnant near breakeven for > 180s, reprice to Maker Breakeven.
      - 'STOP_LOSS': Structural or volatility stop loss breached.
      - 'INVALIDATED': First Candle structural low broken or persistent adverse drift.
      - 'TIMEOUT': Safety ceiling exceeded.
      - 'HOLD': Thesis intact, let profit develop.
    """
    est_sell_fee = current_value * 0.00075
    net_pnl = current_value - entry_cost - buy_fee_usdt - est_sell_fee
    pnl_pct = (net_pnl / entry_cost * 100.0) if entry_cost > 0 else 0.0

    target_profit_pct = forecast.get("forecast_tp_pct", 0.75)
    stop_loss_pct = forecast.get("forecast_sl_pct", -0.75)
    expected_hold = forecast.get("expected_hold_sec", 450)

    # 1. Take Profit reached
    if pnl_pct >= target_profit_pct:
        return "TAKE_PROFIT", f"Forecast target profit hit ({pnl_pct:+.2f}% >= {target_profit_pct:+.2f}%)", net_pnl

    # 2. Dynamic Trailing Take-Profit:
    # Once trade clears +0.35% (covers all fees with solid profit), trail by 16% from peak
    peak_pct = (peak_pnl / entry_cost * 100.0) if entry_cost > 0 else pnl_pct
    if peak_pct >= 0.35:
        trail_tolerance = max(0.08, peak_pct * 0.16)
        if pnl_pct <= (peak_pct - trail_tolerance) and pnl_pct >= 0.15:
            return "TAKE_PROFIT", f"Trailing profit locked (+{pnl_pct:.2f}% from peak +{peak_pct:.2f}%)", net_pnl

    # 3. First Candle Structural Low Invalidation
    # If price drops below the First Candle Low (L1), the bullish structure failed
    structural_sl_price = forecast.get("structural_sl_price", 0.0)
    if structural_sl_price > 0 and current_value > 0:
        cur_price = current_value / (entry_cost / (structural_sl_price * 1.01)) if entry_cost > 0 else 0
        if 0 < cur_price < structural_sl_price and pnl_pct <= -0.25:
            return "INVALIDATED", f"First Candle structural low broken ({cur_price:.4f} < {structural_sl_price:.4f})", net_pnl

    # 4. Stop-Loss Floor hit
    if pnl_pct <= stop_loss_pct:
        return "STOP_LOSS", f"Structural stop-loss floor hit ({pnl_pct:.2f}% <= {stop_loss_pct:.2f}%)", net_pnl

    # 5. Passive Maker Scratch Recommendation (Stagnant near breakeven for > 180s)
    # Allows exiting at +0.015 USDT maker without taking market slippage fees
    if elapsed_sec >= 180.0 and -0.06 <= pnl_pct <= 0.04:
        return "PASSIVE_SCRATCH", f"Trade stagnant at breakeven for {elapsed_sec:.0f}s — passive maker exit recommended", net_pnl

    # 6. Safety Ceiling Timeout (12 minutes max)
    max_timeout = max(expected_hold * 1.5, 600.0)
    if elapsed_sec >= max_timeout:
        return "TIMEOUT", f"Forecast horizon timeout ({elapsed_sec:.0f}s >= {max_timeout:.0f}s)", net_pnl

    return "HOLD", "Thesis intact", net_pnl
