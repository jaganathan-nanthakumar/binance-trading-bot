"""
hybrid_strategy.py — Hybrid Quantitative Market Regime Predictor & Dynamic Strategy Selector.

Implements the 7-Strategy Taxonomy matching market state to algorithm:

| Algorithm                | Best Market      | Trade Frequency | Main Risk                | Needs Speed? |
|--------------------------|------------------|-----------------|--------------------------|--------------|
| Trend Following          | Strong trends    | Low             | Whipsaws in ranges       | No           |
| Momentum                 | Breakouts        | Medium          | False breakouts          | Somewhat     |
| Mean Reversion           | Sideways ranges  | Medium          | Trends that don't revert | No           |
| Grid                     | Tight ranges     | High            | Price leaves the grid    | Somewhat     |
| Arbitrage                | Any              | Very high       | Fees, transfer delays    | Yes          |
| Market Making / Scalping | Liquid pairs     | Very high       | Costs, sharp moves       | Yes          |
| Machine Learning         | Depends on model | Varies          | Overfitting              | Varies       |

Quantitative foundations:
- Wilder's ADX (Average Directional Movement Index) & Directional Indicators (+DI / -DI).
- Bollinger Bandwidth & Bollinger Squeeze Volatility Expansion.
- Rescaled-Range (R/S) Hurst Exponent (Persistent > 0.55 vs Anti-Persistent < 0.45).
- Kinematic Kalman Velocity (Zero-Lag return slope).
- Relative Volume (RVol) & First Candle Dominance (FCA).
- Order Book Imbalance (OBI) & Liquidity Depth.
- Real-time Triangular Arbitrage Spread Evaluator.
- Multi-Armed Bandit / Online Logistic Meta-Model.
"""

from __future__ import annotations

import math
import time
import threading
from typing import Dict, Any, List, Tuple, Optional
import numpy as np

try:
    import ts_forecast
except ImportError:
    ts_forecast = None

try:
    import ml_selector
except ImportError:
    ml_selector = None


# ---------------------------------------------------------------------------
# STRATEGY CONSTANTS & TAXONOMY TABLE
# ---------------------------------------------------------------------------

STRATEGY_SEMICONDUCTOR_DUAL_ENGINE = "SEMICONDUCTOR_DUAL_ENGINE"
STRATEGY_TREND_FOLLOWING = "TREND_FOLLOWING"
STRATEGY_MOMENTUM = "MOMENTUM"
STRATEGY_MEAN_REVERSION = "MEAN_REVERSION"
STRATEGY_GRID = "GRID"
STRATEGY_ARBITRAGE = "ARBITRAGE"
STRATEGY_MARKET_MAKING = "MARKET_MAKING_SCALPING"
STRATEGY_MACHINE_LEARNING = "MACHINE_LEARNING"
STRATEGY_BLOCKED = "BLOCKED"

ALL_HYBRID_STRATEGIES = [
    STRATEGY_SEMICONDUCTOR_DUAL_ENGINE,
    STRATEGY_TREND_FOLLOWING,
    STRATEGY_MOMENTUM,
    STRATEGY_MEAN_REVERSION,
    STRATEGY_GRID,
    STRATEGY_ARBITRAGE,
    STRATEGY_MARKET_MAKING,
    STRATEGY_MACHINE_LEARNING,
]

STRATEGY_TAXONOMY_TABLE: Dict[str, Dict[str, Any]] = {
    STRATEGY_SEMICONDUCTOR_DUAL_ENGINE: {
        "name": "Semiconductor Dual-Engine (SOXLB / SOXSB)",
        "best_market": "US Semiconductor Sector Asymmetric 3X Counterpart Trends",
        "trade_frequency": "Medium",
        "main_risk": "Volatility Decay in Chop (Mitigated by 100% USDT Safe Haven Cash)",
        "needs_speed": "No",
        "primary_trigger": "Synthetic Price Ratio (SOXLB/SOXSB) EMA9/EMA21 trend breakout with ADX >= 20",
        "risk_mitigation": "Tri-state switching: Long SOXLB on bull surges, Long SOXSB on bear breakdowns, 100% USDT cash in chop.",
        "default_tp_pct": 1.25,
        "default_sl_pct": -0.55,
        "trailing_stop_enabled": True,
        "trailing_drop_pct": 0.25,
        "min_hold_sec": 30,
        "reprice_sec": 120,
        "max_hold_sec": 300,
    },
    STRATEGY_TREND_FOLLOWING: {
        "name": "Trend Following",
        "best_market": "Strong trends",
        "trade_frequency": "Low",
        "main_risk": "Whipsaws in ranges",
        "needs_speed": "No",
        "primary_trigger": "ADX >= 25, Hurst > 0.55, EMA9 > EMA21 > EMA50, Kalman velocity > 0",
        "risk_mitigation": "Wide trailing stop (ATR/Chandelier), patient EMA pullback entry, no premature micro-profit scratch.",
        "default_tp_pct": 1.60,
        "default_sl_pct": -0.85,
        "trailing_stop_enabled": True,
        "trailing_drop_pct": 0.25,
        "min_hold_sec": 60,
        "reprice_sec": 300,
        "max_hold_sec": 300,
    },
    STRATEGY_MOMENTUM: {
        "name": "Momentum",
        "best_market": "Breakouts",
        "trade_frequency": "Medium",
        "main_risk": "False breakouts",
        "needs_speed": "Somewhat",
        "primary_trigger": "RVol >= 1.6, Bollinger Bandwidth expansion, FCA Body Dominance >= 0.60, high taker buy ratio",
        "risk_mitigation": "Tight structural stop loss immediately below breakout candle base; scratch quickly if momentum stalls.",
        "default_tp_pct": 1.00,
        "default_sl_pct": -0.45,
        "trailing_stop_enabled": True,
        "trailing_drop_pct": 0.20,
        "min_hold_sec": 10,
        "reprice_sec": 90,
        "max_hold_sec": 300,
    },
    STRATEGY_MEAN_REVERSION: {
        "name": "Mean Reversion",
        "best_market": "Sideways ranges",
        "trade_frequency": "Medium",
        "main_risk": "Trends that don't revert",
        "needs_speed": "No",
        "primary_trigger": "Hurst < 0.45, ADX < 20, RSI <= 38 or Price <= Lower Bollinger Band, VWAP discount",
        "risk_mitigation": "Enter at support/oversold lower band; exit strictly at Mean (VWAP / SMA20); hard SL 1.2x ATR below support.",
        "default_tp_pct": 0.60,
        "default_sl_pct": -0.65,
        "trailing_stop_enabled": False,
        "trailing_drop_pct": 0.0,
        "min_hold_sec": 20,
        "reprice_sec": 180,
        "max_hold_sec": 300,
    },
    STRATEGY_GRID: {
        "name": "Grid Trading",
        "best_market": "Tight ranges",
        "trade_frequency": "High",
        "main_risk": "Price leaves the grid",
        "needs_speed": "Somewhat",
        "primary_trigger": "ADX < 16, 1m ATR < 0.12%, compressed Bollinger Bandwidth < 0.85%, balanced order book",
        "risk_mitigation": "Staged limit orders across channel; each rung takes +0.25% profit; circuit breaker stop if price dumps out of grid.",
        "default_tp_pct": 0.35,
        "default_sl_pct": -0.75,
        "trailing_stop_enabled": False,
        "trailing_drop_pct": 0.0,
        "min_hold_sec": 10,
        "reprice_sec": 60,
        "max_hold_sec": 300,
    },
    STRATEGY_ARBITRAGE: {
        "name": "Arbitrage",
        "best_market": "Any",
        "trade_frequency": "Very high",
        "main_risk": "Fees, transfer delays",
        "needs_speed": "Yes",
        "primary_trigger": "Triangular loop net spread (Symbol/USDT vs Symbol/BNB * BNB/USDT) > 3x trading fee",
        "risk_mitigation": "Pre-screen 3-leg fees strictly; atomic fill check; instant fallback to best market regime if spread < fee hurdle.",
        "default_tp_pct": 0.20,
        "default_sl_pct": -0.30,
        "trailing_stop_enabled": False,
        "trailing_drop_pct": 0.0,
        "min_hold_sec": 2,
        "reprice_sec": 15,
        "max_hold_sec": 300,
    },
    STRATEGY_MARKET_MAKING: {
        "name": "Market Making / Scalping",
        "best_market": "Liquid pairs",
        "trade_frequency": "Very high",
        "main_risk": "Costs, sharp moves",
        "needs_speed": "Yes",
        "primary_trigger": "Spread <= 0.05%, 24h Quote Volume >= $10M, spread ticks >= 2, deep top-5 order book depth",
        "risk_mitigation": "Pure Best-Bid maker limit entry, resting limit ask for maker discount, instant cancellation on toxic flow.",
        "default_tp_pct": 0.18,
        "default_sl_pct": -0.35,
        "trailing_stop_enabled": False,
        "trailing_drop_pct": 0.0,
        "min_hold_sec": 5,
        "reprice_sec": 30,
        "max_hold_sec": 300,
    },
    STRATEGY_MACHINE_LEARNING: {
        "name": "Machine Learning Meta-Model",
        "best_market": "Depends on the model",
        "trade_frequency": "Varies",
        "main_risk": "Overfitting",
        "needs_speed": "Varies",
        "primary_trigger": "Multi-microstructure feature ensemble prediction, LinUCB exploration bonus, high win probability (> 0.60)",
        "risk_mitigation": "L2 regularization, AdaGrad adaptive rates, Welford running variance normalization to prevent overfitting.",
        "default_tp_pct": 0.85,
        "default_sl_pct": -0.55,
        "trailing_stop_enabled": True,
        "trailing_drop_pct": 0.20,
        "min_hold_sec": 15,
        "reprice_sec": 90,
        "max_hold_sec": 300,
    },
    STRATEGY_BLOCKED: {
        "name": "BLOCKED (Capital Preservation)",
        "best_market": "Downtrend / Exhaustion (High Risk)",
        "trade_frequency": "None",
        "main_risk": "Adverse dump / Falling knife",
        "needs_speed": "No",
        "primary_trigger": "Velocity < -0.015% or -DI > +DI + 6, exhausted spike, or unviable microstructure",
        "risk_mitigation": "100% Capital Protection: Zero buy orders placed. Trade blocked at cycle gate ($0 loss).",
        "default_tp_pct": 0.0,
        "default_sl_pct": 0.0,
        "trailing_stop_enabled": False,
        "trailing_drop_pct": 0.0,
        "min_hold_sec": 0,
        "reprice_sec": 0,
        "max_hold_sec": 0,
    },
}


# ---------------------------------------------------------------------------
# TECHNICAL & QUANTITATIVE INDICATOR CALCULATIONS
# ---------------------------------------------------------------------------

def compute_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> Tuple[float, float, float]:
    """
    Computes Wilder's Average Directional Movement Index (ADX), +DI, and -DI.
    ADX >= 25 indicates a strong trend; ADX < 20 indicates sideways range or chop.
    """
    n = len(closes)
    if n < period + 2:
        return 20.0, 20.0, 20.0

    # True Range
    tr1 = highs[1:] - lows[1:]
    tr2 = np.abs(highs[1:] - closes[:-1])
    tr3 = np.abs(lows[1:] - closes[:-1])
    tr = np.maximum(tr1, np.maximum(tr2, tr3))

    # Directional Movement
    up_move = highs[1:] - highs[:-1]
    down_move = lows[:-1] - lows[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # Wilder's Smoothing
    alpha = 1.0 / period
    tr_smooth = np.zeros(len(tr))
    plus_dm_smooth = np.zeros(len(plus_dm))
    minus_dm_smooth = np.zeros(len(minus_dm))

    tr_smooth[period - 1] = np.sum(tr[:period])
    plus_dm_smooth[period - 1] = np.sum(plus_dm[:period])
    minus_dm_smooth[period - 1] = np.sum(minus_dm[:period])

    for i in range(period, len(tr)):
        tr_smooth[i] = tr_smooth[i - 1] - (tr_smooth[i - 1] / period) + tr[i]
        plus_dm_smooth[i] = plus_dm_smooth[i - 1] - (plus_dm_smooth[i - 1] / period) + plus_dm[i]
        minus_dm_smooth[i] = minus_dm_smooth[i - 1] - (minus_dm_smooth[i - 1] / period) + minus_dm[i]

    # +DI and -DI
    denom = np.where(tr_smooth == 0, 1e-9, tr_smooth)
    plus_di = 100.0 * (plus_dm_smooth / denom)
    minus_di = 100.0 * (minus_dm_smooth / denom)

    # Directional Index (DX)
    di_sum = plus_di + minus_di
    di_diff = np.abs(plus_di - minus_di)
    dx = 100.0 * np.divide(di_diff, di_sum, out=np.zeros_like(di_diff), where=(di_sum > 0))

    # ADX smoothing
    if len(dx) < 2 * period:
        adx_val = float(np.mean(dx[period - 1:]))
    else:
        adx_series = np.zeros(len(dx))
        adx_series[2 * period - 2] = np.mean(dx[period - 1: 2 * period - 1])
        for i in range(2 * period - 1, len(dx)):
            adx_series[i] = (adx_series[i - 1] * (period - 1) + dx[i]) / period
        adx_val = float(adx_series[-1])

    latest_plus_di = float(plus_di[-1])
    latest_minus_di = float(minus_di[-1])
    return round(float(adx_val), 2), round(latest_plus_di, 2), round(latest_minus_di, 2)


def compute_bollinger_bands(
    closes: np.ndarray, period: int = 20, num_std: float = 2.0
) -> Tuple[float, float, float, float, float]:
    """
    Computes Bollinger Bands, Bandwidth %, and %B.
    Returns: (upper, middle, lower, bandwidth_pct, pct_b)
    """
    n = len(closes)
    if n < period:
        p = float(closes[-1]) if n > 0 else 1.0
        return p * 1.01, p, p * 0.99, 2.0, 0.50

    window = closes[-period:]
    mid = float(np.mean(window))
    std = float(np.std(window))
    upper = mid + (num_std * std)
    lower = mid - (num_std * std)

    bandwidth = ((upper - lower) / mid) * 100.0 if mid > 0 else 0.0
    cur_p = float(closes[-1])
    pct_b = (cur_p - lower) / (upper - lower) if (upper - lower) > 0 else 0.50

    return round(upper, 8), round(mid, 8), round(lower, 8), round(bandwidth, 4), round(pct_b, 4)


def compute_rsi(closes: np.ndarray, period: int = 14) -> float:
    """Computes standard Relative Strength Index (RSI)."""
    n = len(closes)
    if n < period + 1:
        return 50.0

    diffs = np.diff(closes)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)

    avg_gain = float(np.mean(gains[-period:]))
    avg_loss = float(np.mean(losses[-period:]))

    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(float(rsi), 2)


def compute_ema(prices: np.ndarray, period: int) -> float:
    """Computes Exponential Moving Average of series."""
    n = len(prices)
    if n == 0:
        return 0.0
    if n < period:
        return float(np.mean(prices))

    k = 2.0 / (period + 1)
    ema = float(prices[0])
    for p in prices[1:]:
        ema = (p * k) + (ema * (1.0 - k))
    return float(ema)


def compute_relative_volume(volumes: np.ndarray, period: int = 20) -> float:
    """Computes current Relative Volume (RVol) vs moving average."""
    n = len(volumes)
    if n < 2:
        return 1.0
    cur_vol = float(volumes[-1])
    avg_vol = float(np.mean(volumes[-min(n, period):-1])) if n > 1 else cur_vol
    return round(cur_vol / avg_vol, 2) if avg_vol > 0 else 1.0


def check_triangular_arbitrage(
    client: Any, symbol: str, bnb_price: Optional[float] = None, fee_rate: float = 0.00075
) -> Dict[str, Any]:
    """
    Evaluates real-time triangular arbitrage spread:
    USDT -> Symbol -> BNB -> USDT.
    Calculates net arbitrage profit after 3 execution fee legs.
    """
    res = {
        "symbol": symbol,
        "is_viable": False,
        "direct_usdt_bid": 0.0,
        "synthetic_bid": 0.0,
        "gross_spread_pct": 0.0,
        "net_spread_pct": 0.0,
        "fee_hurdle_pct": fee_rate * 3.0 * 100.0,
    }
    if not symbol.endswith("USDT") or symbol.startswith("BNB"):
        return res

    base_asset = symbol[:-4]
    bnb_pair = f"{base_asset}BNB"

    try:
        # Check order books
        ob_usdt = client.get_orderbook_ticker(symbol=symbol)
        usdt_bid = float(ob_usdt.get("bidPrice") or 0.0)

        # We need Symbol/BNB price
        ob_bnb = client.get_orderbook_ticker(symbol=bnb_pair)
        bnb_ask = float(ob_bnb.get("askPrice") or 0.0)

        if usdt_bid <= 0 or bnb_ask <= 0:
            return res

        if bnb_price is None or bnb_price <= 0:
            ob_bnbusdt = client.get_orderbook_ticker(symbol="BNBUSDT")
            bnb_price = float(ob_bnbusdt.get("bidPrice") or 0.0)

        synthetic_bid = bnb_ask * bnb_price
        if synthetic_bid <= 0:
            return res

        gross_spread = ((usdt_bid / synthetic_bid) - 1.0) * 100.0
        fee_hurdle = fee_rate * 3.0 * 100.0  # 3 legs @ 0.075% = 0.225%
        net_spread = gross_spread - fee_hurdle

        res["direct_usdt_bid"] = usdt_bid
        res["synthetic_bid"] = synthetic_bid
        res["gross_spread_pct"] = round(gross_spread, 4)
        res["net_spread_pct"] = round(net_spread, 4)
        # Viable only if net profit remains after 3 fees plus buffer
        res["is_viable"] = net_spread > 0.05
        return res
    except Exception:
        return res


# ---------------------------------------------------------------------------
# HYBRID MARKET REGIME PREDICTOR
# ---------------------------------------------------------------------------

class HybridMarketPredictor:
    """
    Intelligent Market Regime Predictor and Algorithm Selector.
    Analyzes microstructure, volatility cones, spectral harmonics, and liquidity
    to match the coin's live state to the optimal trading algorithm.
    """

    def __init__(self):
        self._cache_lock = threading.Lock()
        self._prediction_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self.cache_ttl_sec = 8.0

    def predict_market_and_strategy(
        self,
        client: Any,
        symbol: str,
        investment_usdt: float = 25.0,
        forced_strategy: Optional[str] = None,
        klines_1m: Optional[List[Any]] = None,
        klines_5m: Optional[List[Any]] = None,
        ob_ticker: Optional[Dict[str, Any]] = None,
        depth: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Analyzes live market dynamics of the coin and outputs:
        1. Predicted Market Regime
        2. Selected Algorithm from the Taxonomy Table
        3. Tailored Execution Parameters (Entry, SL, TP, Trailing Stop, Hold Time)
        4. Full Diagnostic Indicator Breakdown
        """
        now = time.time()
        with self._cache_lock:
            if symbol in self._prediction_cache and not forced_strategy:
                cached_ts, cached_pred = self._prediction_cache[symbol]
                if (now - cached_ts) < self.cache_ttl_sec:
                    return dict(cached_pred)

        # 0. Dedicated Semiconductor Dual-Engine Specialist Interceptor
        if forced_strategy == STRATEGY_SEMICONDUCTOR_DUAL_ENGINE or symbol in ("SOXLBUSDT", "SOXSBUSDT"):
            try:
                import semiconductor_dual_engine
                semi = semiconductor_dual_engine.evaluate_semiconductor_regime(client)
                is_selected = (symbol == semi.get("selected_symbol")) and bool(semi.get("viable"))
                strat_name = STRATEGY_SEMICONDUCTOR_DUAL_ENGINE if is_selected else STRATEGY_BLOCKED
                pred_mkt = (
                    f"Semiconductor 5M: {semi.get('bias', 'CHOP')} (Score: {semi.get('total_score', 0):+d}/7)"
                    if is_selected
                    else f"Adverse / Chop Regime (Score: {semi.get('total_score', 0):+d}/7, Capital Protected)"
                )
                tp_pct = float(semi.get("target_profit_pct") or 1.25)
                sl_pct = float(semi.get("stop_loss_pct") or -0.60)
                tp_usdt = max(0.08, investment_usdt * (tp_pct / 100.0))
                sl_usdt = - max(0.05, investment_usdt * (abs(sl_pct) / 100.0))
                return {
                    "symbol": symbol,
                    "timestamp": time.time(),
                    "selected_algorithm": strat_name,
                    "strategy_name": STRATEGY_TAXONOMY_TABLE[strat_name]["name"],
                    "predicted_market": pred_mkt,
                    "trade_frequency": "Medium" if is_selected else "None",
                    "main_risk": "Reversal (Protected)" if is_selected else "Chop Volatility Decay",
                    "needs_speed": "No",
                    "risk_mitigation": "Trailing stop with zero-loss floor" if is_selected else "100% USDT Safe Haven ($0 loss)",
                    "execution_params": {
                        "entry_style": "BEST_BID_MAKER",
                        "target_profit_pct": tp_pct,
                        "target_profit_usdt": tp_usdt,
                        "stop_loss_pct": sl_pct,
                        "stop_loss_usdt": sl_usdt,
                        "trailing_stop_enabled": is_selected,
                        "trailing_drop_pct": 0.25,
                        "is_viable_trade": is_selected,
                        "reprice_sec": 120,
                        "max_hold_sec": 300,
                        "grid_levels": [],
                    },
                    "indicators": {
                        "spread_pct": semi.get("soxlb_spread" if symbol == "SOXLBUSDT" else "soxsb_spread", 0.05),
                        "book_ratio": 1.0,
                        "semiconductor_ratio": semi.get("ratio", 1.0),
                        "semiconductor_regime": semi.get("regime", "CHOP_SKIP"),
                        "total_score": semi.get("total_score", 0),
                        "bias": semi.get("bias", "CHOP"),
                        "scores": semi.get("scores", {}),
                        "reason": semi.get("reason", ""),
                    },
                    "scores": {STRATEGY_SEMICONDUCTOR_DUAL_ENGINE: 10.0 if is_selected else 0.0},
                }
            except Exception as e:
                pass

        # 1. Fetch market data if not passed
        try:
            if klines_1m is None:
                klines_1m = client.get_klines(symbol=symbol, interval="1m", limit=30)
            if klines_5m is None:
                klines_5m = client.get_klines(symbol=symbol, interval="5m", limit=30)
            if ob_ticker is None:
                ob_ticker = client.get_orderbook_ticker(symbol=symbol)
            if depth is None:
                depth = client.get_order_book(symbol=symbol, limit=10)
        except Exception as e:
            return self._fallback_prediction(symbol, investment_usdt, str(e))

        # 2. Extract price arrays
        closes_1m = np.array([float(k[4]) for k in klines_1m]) if klines_1m else np.array([])
        highs_1m = np.array([float(k[2]) for k in klines_1m]) if klines_1m else np.array([])
        lows_1m = np.array([float(k[3]) for k in klines_1m]) if klines_1m else np.array([])
        vols_1m = np.array([float(k[5]) for k in klines_1m]) if klines_1m else np.array([])

        closes_5m = np.array([float(k[4]) for k in klines_5m]) if klines_5m else np.array([])
        highs_5m = np.array([float(k[2]) for k in klines_5m]) if klines_5m else np.array([])
        lows_5m = np.array([float(k[3]) for k in klines_5m]) if klines_5m else np.array([])
        vols_5m = np.array([float(k[5]) for k in klines_5m]) if klines_5m else np.array([])

        cur_price = float(ob_ticker.get("bidPrice") or (closes_1m[-1] if len(closes_1m) > 0 else 1.0))
        ask_price = float(ob_ticker.get("askPrice") or cur_price)
        spread_pct = ((ask_price - cur_price) / cur_price * 100.0) if cur_price > 0 else 0.05

        # Order book liquidity & imbalance
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        bid_vol = sum(float(b[1]) for b in bids[:5])
        ask_vol = sum(float(a[1]) for a in asks[:5])
        tot_depth_usdt = (bid_vol + ask_vol) * cur_price
        book_ratio = (bid_vol / ask_vol) if ask_vol > 0 else 1.0
        obi = (bid_vol - ask_vol) / (bid_vol + ask_vol) if (bid_vol + ask_vol) > 0 else 0.0

        # 3. Compute quantitative indicators
        adx_5m, plus_di_5m, minus_di_5m = compute_adx(highs_5m, lows_5m, closes_5m, period=14)
        upper_bb, mid_bb, lower_bb, bandwidth_pct, pct_b = compute_bollinger_bands(closes_1m, period=20)
        rsi_1m = compute_rsi(closes_1m, period=14)
        rvol_1m = compute_relative_volume(vols_1m, period=20)
        rvol_5m = compute_relative_volume(vols_5m, period=20)

        ema9 = compute_ema(closes_5m, 9)
        ema21 = compute_ema(closes_5m, 21)
        ema50 = compute_ema(closes_5m, 50)
        ema_bull_aligned = (ema9 > ema21 > ema50)

        # Advanced Time Series & Kalman filter
        fc = {}
        hurst = 0.50
        kalman_vel = 0.0
        fca_setup = "UNKNOWN"
        fca_viable = True
        body_dom = 0.50

        if ts_forecast:
            try:
                forecaster = ts_forecast.get_forecaster()
                fc = forecaster.forecast_symbol(symbol, klines_1m=klines_1m, klines_5m=klines_5m)
                hurst = fc.get("hurst_exponent", 0.50)
                kalman_vel = fc.get("kalman_velocity_pct", 0.0)
                fca = fc.get("fca", {})
                fca_setup = fca.get("setup_type", "UNKNOWN")
                fca_viable = fca.get("fca_viable", True)
                body_dom = fca.get("body_dominance", 0.50)
            except Exception:
                pass

        # Check Triangular Arbitrage
        arb_res = check_triangular_arbitrage(client, symbol)

        # 4. Multi-regime Scoring Matrix (0.0 to 10.0 score per algorithm)
        scores: Dict[str, float] = {}

        # GATING CHECKS: Check if market is in an adverse downtrend / dump
        is_downtrend_dump = (minus_di_5m > plus_di_5m + 6.0 and adx_5m >= 22.0) or (kalman_vel < -0.035) or (spread_pct > 0.15)

        # (a) TREND_FOLLOWING (Strong trends - STRICT DIRECTIONAL BULL GATING)
        # Spot can only BUY LONG. If price is dropping, velocity is negative, or bears dominate: score MUST BE ZERO!
        is_bull_trend = (
            (plus_di_5m > minus_di_5m + 4.0)
            and (kalman_vel > 0.015)
            and (closes_5m[-1] >= ema21 if len(closes_5m) > 0 else True)
            and (rvol_5m >= 0.55)
            and not is_downtrend_dump
        )
        if is_bull_trend:
            trend_score = 4.0
            if adx_5m >= 25.0:
                trend_score += min(2.5, (adx_5m - 20.0) * 0.20)
            if plus_di_5m > minus_di_5m + 10.0:
                trend_score += 1.5
            if kalman_vel > 0.03:
                trend_score += min(2.0, kalman_vel * 30.0)
            scores[STRATEGY_TREND_FOLLOWING] = round(trend_score, 2)
        else:
            scores[STRATEGY_TREND_FOLLOWING] = 0.0

        # (b) MOMENTUM (Breakouts - Volume Surge & Bollinger Expansion)
        is_breakout = (
            (rvol_1m >= 1.35 or rvol_5m >= 1.35)
            and (pct_b >= 0.70)
            and (kalman_vel > 0.020)
            and (bandwidth_pct >= 0.85)
            and not is_downtrend_dump
        )
        if is_breakout:
            mom_score = 4.5
            if max(rvol_1m, rvol_5m) >= 1.75:
                mom_score += 2.5
            if bandwidth_pct >= 1.25:
                mom_score += 1.5
            if kalman_vel >= 0.035:
                mom_score += 1.5
            scores[STRATEGY_MOMENTUM] = round(mom_score, 2)
        else:
            scores[STRATEGY_MOMENTUM] = 0.0

        # (c) MEAN_REVERSION (Sideways ranges - Oversold dip to support)
        is_mr_setup = (
            (hurst < 0.52)
            and (adx_5m < 25.0)
            and (rsi_1m <= 38.0 or pct_b <= 0.20)
            and (book_ratio >= 0.85)
            and (kalman_vel >= -0.05)
        )
        if is_mr_setup:
            mr_score = 5.0
            if rsi_1m <= 32.0:
                mr_score += 2.5
            if pct_b <= 0.10:
                mr_score += 1.5
            if book_ratio >= 1.05:
                mr_score += 1.0
            scores[STRATEGY_MEAN_REVERSION] = round(mr_score, 2)
        else:
            scores[STRATEGY_MEAN_REVERSION] = 0.0

        # (d) GRID (Tight ranges - Low volatility, compressed bandwidth, flat velocity)
        is_grid_setup = (
            (adx_5m < 18.0)
            and (bandwidth_pct < 0.85)
            and (38.0 <= rsi_1m <= 62.0)
            and (abs(kalman_vel) < 0.015)
        )
        if is_grid_setup:
            grid_score = 5.0
            if bandwidth_pct < 0.60:
                grid_score += 2.5
            if adx_5m < 14.0:
                grid_score += 1.5
            if 0.85 <= book_ratio <= 1.20:
                grid_score += 1.0
            scores[STRATEGY_GRID] = round(grid_score, 2)
        else:
            scores[STRATEGY_GRID] = 0.0

        # (e) MARKET_MAKING_SCALPING (Liquid pairs - Tight spread, deep depth, non-violent price action)
        is_liquid = (
            (spread_pct <= 0.06)
            and (tot_depth_usdt >= 12000.0)
            and (abs(kalman_vel) < 0.035)
        )
        if is_liquid:
            mm_score = 5.0
            if spread_pct <= 0.03:
                mm_score += 3.0
            elif spread_pct <= 0.05:
                mm_score += 1.5
            if tot_depth_usdt >= 25000.0:
                mm_score += 2.0
            scores[STRATEGY_MARKET_MAKING] = round(mm_score, 2)
        else:
            scores[STRATEGY_MARKET_MAKING] = 0.0

        # (f) ARBITRAGE (Any market with pricing anomaly)
        arb_score = 0.0
        if arb_res.get("is_viable"):
            arb_score = 12.0
        else:
            arb_score = max(0.0, arb_res.get("gross_spread_pct", 0.0) * 5.0)
        scores[STRATEGY_ARBITRAGE] = round(arb_score, 2)

        # (g) MACHINE_LEARNING (Adaptive Model)
        ml_score_val = 0.0
        if ml_selector:
            try:
                selector = ml_selector.get_selector()
                feat_dict = {
                    "book_ratio": book_ratio,
                    "order_book_imbalance": obi,
                    "momentum_5m": float(closes_1m[-1] - closes_1m[-5]) / closes_1m[-5] * 100.0 if len(closes_1m) >= 5 else 0.0,
                    "volatility_1m": bandwidth_pct,
                    "taker_buy_ratio": fc.get("taker_ratio_5m", 0.50),
                    "vwap_score": 1.0 if cur_price > mid_bb else -1.0,
                    "spread_pct": spread_pct,
                    "tick_ratio": 1.0,
                    "cumulative_ask_usdt": tot_depth_usdt * 0.5,
                }
                raw_ml, p_win = selector.score_candidate(feat_dict)
                if p_win >= 0.60:
                    ml_score_val = min(10.0, raw_ml)
            except Exception:
                pass
        scores[STRATEGY_MACHINE_LEARNING] = round(float(ml_score_val), 2)

        # 5. Market Viability & Algorithm Selection
        max_regime_score = max(scores.values()) if scores else 0.0
        is_viable_trade = (max_regime_score >= 3.5) and (not is_downtrend_dump)

        if forced_strategy and forced_strategy in STRATEGY_TAXONOMY_TABLE:
            selected_algorithm = forced_strategy
            spec = STRATEGY_TAXONOMY_TABLE[selected_algorithm]
            predicted_market = spec["best_market"]
        elif arb_res.get("is_viable"):
            selected_algorithm = STRATEGY_ARBITRAGE
            spec = STRATEGY_TAXONOMY_TABLE[selected_algorithm]
            predicted_market = spec["best_market"]
        elif not is_viable_trade:
            # Unfavorable market: either adverse dump, erratic spread, or no clear edge
            selected_algorithm = STRATEGY_BLOCKED
            spec = STRATEGY_TAXONOMY_TABLE[selected_algorithm]
            predicted_market = spec["best_market"]
        else:
            # Pick highest qualifying regime algorithm
            selected_algorithm = max(scores, key=lambda k: scores[k])
            spec = STRATEGY_TAXONOMY_TABLE[selected_algorithm]
            predicted_market = spec["best_market"]

        # 6. Tailor Execution Parameters to the Selected Algorithm
        tp_pct = spec["default_tp_pct"]
        sl_pct = spec["default_sl_pct"]
        trailing_enabled = spec["trailing_stop_enabled"]
        trailing_drop_pct = spec["trailing_drop_pct"]
        reprice_sec = spec["reprice_sec"]
        max_hold_sec = spec["max_hold_sec"]

        # Volatility adjustments from time series forecast
        if fc:
            vol_forecast = fc.get("vol_forecast_pct", 0.15)
            if selected_algorithm == STRATEGY_TREND_FOLLOWING:
                tp_pct = max(1.20, min(3.50, fc.get("forecast_tp_pct", tp_pct) * 1.35))
                sl_pct = min(-0.70, fc.get("forecast_sl_pct", sl_pct))
                trailing_drop_pct = max(0.20, min(0.45, vol_forecast * 1.5))
            elif selected_algorithm == STRATEGY_MOMENTUM:
                tp_pct = max(0.80, min(2.20, fc.get("forecast_tp_pct", tp_pct)))
                sl_pct = max(-0.55, min(-0.35, fc.get("forecast_sl_pct", sl_pct)))  # Tight SL for false breakouts
                trailing_drop_pct = max(0.15, min(0.30, vol_forecast * 1.0))
            elif selected_algorithm == STRATEGY_MEAN_REVERSION:
                # Target the middle band (mean)
                if cur_price < mid_bb:
                    mean_tp_pct = ((mid_bb - cur_price) / cur_price) * 100.0
                    tp_pct = max(0.40, min(1.20, mean_tp_pct))
                sl_pct = - max(0.50, min(1.10, vol_forecast * 2.5))
            elif selected_algorithm == STRATEGY_GRID:
                tp_pct = max(0.25, min(0.50, bandwidth_pct * 0.40))
                sl_pct = - max(0.60, min(1.20, bandwidth_pct * 1.10))

        # Absolute USDT Stop Loss and Profit Target (calibrated to 0.00XXX range)
        if selected_algorithm == STRATEGY_BLOCKED:
            target_profit_usdt = 0.0
            stop_loss_usdt = 0.0
        else:
            target_profit_usdt = 0.0050
            stop_loss_usdt = -0.0050

        result: Dict[str, Any] = {
            "symbol": symbol,
            "timestamp": now,
            "current_price": cur_price,
            "predicted_market": predicted_market,
            "selected_algorithm": selected_algorithm,
            "strategy_name": spec["name"],
            "trade_frequency": spec["trade_frequency"],
            "main_risk": spec["main_risk"],
            "needs_speed": spec["needs_speed"],
            "risk_mitigation": spec["risk_mitigation"],
            "scores": scores,
            "confidence": round(float(scores.get(selected_algorithm, 5.0) / 10.0), 2),
            "execution_params": {
                "entry_style": (
                    "SNIPER_BREAKOUT" if selected_algorithm == STRATEGY_MOMENTUM else
                    ("LIMIT_PULLBACK" if selected_algorithm == STRATEGY_TREND_FOLLOWING else
                    ("LIMIT_OVERSOLD" if selected_algorithm == STRATEGY_MEAN_REVERSION else
                    ("GRID_TRANCHES" if selected_algorithm == STRATEGY_GRID else
                    ("TRIANGULAR_ARB" if selected_algorithm == STRATEGY_ARBITRAGE else "BEST_BID_MAKER"))))
                ),
                "target_profit_pct": round(tp_pct, 4),
                "target_profit_usdt": round(target_profit_usdt, 4),
                "stop_loss_pct": round(sl_pct, 4),
                "stop_loss_usdt": round(stop_loss_usdt, 4),
                "trailing_stop_enabled": trailing_enabled,
                "trailing_drop_pct": round(trailing_drop_pct, 4),
                "is_viable_trade": is_viable_trade,
                "reprice_sec": reprice_sec,
                "max_hold_sec": max_hold_sec,
                "grid_levels": [
                    {"level": 1, "offset_pct": 0.0, "qty_share": 0.50},
                    {"level": 2, "offset_pct": - round(bandwidth_pct * 0.45, 3), "qty_share": 0.50},
                ] if selected_algorithm == STRATEGY_GRID else [],
            },
            "indicators": {
                "adx_5m": adx_5m,
                "plus_di_5m": plus_di_5m,
                "minus_di_5m": minus_di_5m,
                "bollinger_bandwidth_pct": bandwidth_pct,
                "bollinger_pct_b": pct_b,
                "rsi_1m": rsi_1m,
                "rvol_1m": rvol_1m,
                "rvol_5m": rvol_5m,
                "hurst_exponent": hurst,
                "kalman_velocity_pct": kalman_vel,
                "order_book_imbalance": obi,
                "book_ratio": book_ratio,
                "spread_pct": spread_pct,
                "triangular_arb": arb_res,
                "fca_setup": fca_setup,
                "fca_viable": fca_viable,
            },
        }

        with self._cache_lock:
            self._prediction_cache[symbol] = (now, dict(result))

        return result

    def _fallback_prediction(self, symbol: str, investment_usdt: float, error_msg: str) -> Dict[str, Any]:
        """Safe fallback strategy when market data is temporarily unavailable."""
        spec = STRATEGY_TAXONOMY_TABLE[STRATEGY_MARKET_MAKING]
        return {
            "symbol": symbol,
            "timestamp": time.time(),
            "predicted_market": "Liquid pairs (Fallback)",
            "selected_algorithm": STRATEGY_MARKET_MAKING,
            "strategy_name": spec["name"],
            "trade_frequency": spec["trade_frequency"],
            "main_risk": spec["main_risk"],
            "needs_speed": spec["needs_speed"],
            "risk_mitigation": spec["risk_mitigation"],
            "scores": {s: 1.0 for s in ALL_HYBRID_STRATEGIES},
            "confidence": 0.50,
            "execution_params": {
                "entry_style": "BEST_BID_MAKER",
                "target_profit_pct": 0.20,
                "target_profit_usdt": round(investment_usdt * 0.002, 4),
                "stop_loss_pct": -0.40,
                "stop_loss_usdt": - round(investment_usdt * 0.004, 4),
                "trailing_stop_enabled": False,
                "trailing_drop_pct": 0.0,
                "reprice_sec": 30,
                "max_hold_sec": 120,
            },
            "indicators": {"error": error_msg},
        }


# Singleton instance
_PREDICTOR: Optional[HybridMarketPredictor] = None
_PREDICTOR_LOCK = threading.Lock()


def get_market_predictor() -> HybridMarketPredictor:
    """Singleton getter for HybridMarketPredictor."""
    global _PREDICTOR
    with _PREDICTOR_LOCK:
        if _PREDICTOR is None:
            _PREDICTOR = HybridMarketPredictor()
        return _PREDICTOR


def predict_market_and_strategy(
    client: Any, symbol: str, investment_usdt: float = 25.0, forced_strategy: Optional[str] = None
) -> Dict[str, Any]:
    """Helper entry point for coin regime prediction and algorithm selection."""
    return get_market_predictor().predict_market_and_strategy(
        client=client, symbol=symbol, investment_usdt=investment_usdt, forced_strategy=forced_strategy
    )
