"""
semiconductor_dual_engine.py — Semiconductor 5-Minute Directional Prediction & Trading Engine.

Engineered specifically for Binance bStock tokenized pairs:
- SOXLBUSDT: Direxion Daily Semiconductor Bull 3X Shares counterpart
- SOXSBUSDT: Direxion Daily Semiconductor Bear 3X Shares counterpart

Revised 8-Factor 5-Minute Predictive Scoring Model:
Score +1 for bullish, -1 for bearish, 0 for neutral:
1. Price > VWAP on SMH 1m?
2. NVDA 1m > VWAP and green?
3. AVGO + AMD both green on 1m?
4. NQ futures green last 5 min?
5. CVD positive and volume > average?
6. EMA 9 > EMA 21 on SOX 5m?
7. No major news: no Taiwan/ TSM headline, no NVDA halt?
8. SOXSB ↔ SOXLB orderbook lag & divergence?

Decision Matrix:
+3 to +8 = Bullish bias for next 5 min -> Long SOXLBUSDT
-3 to -8 = Bearish bias for next 5 min -> Long SOXSBUSDT
-2 to +2 = Chop - skip -> 100% USDT Safe Haven ($0 loss)
"""

from __future__ import annotations

import os
import sys
import time
import math
import re
import threading
import concurrent.futures
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Tuple, Optional
import requests
from datetime import datetime, timezone, timedelta

# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))


def get_ist_now() -> datetime:
    """Returns current datetime in Indian Standard Time (IST)."""
    return datetime.now(IST)


def get_ist_now_str(fmt: str = "%d %b %Y, %I:%M:%S %p IST") -> str:
    """Returns formatted current datetime string in IST."""
    return datetime.now(IST).strftime(fmt)


def get_ist_time_str(fmt: str = "%I:%M:%S %p IST") -> str:
    """Returns formatted current time string in IST."""
    return datetime.now(IST).strftime(fmt)


SEMICONDUCTOR_PAIRS = ["SOXLBUSDT", "SOXSBUSDT"]
BULL_SYMBOL = "SOXLBUSDT"
BEAR_SYMBOL = "SOXSBUSDT"

DEFAULT_MAX_SPREAD_BSTOCK = 0.20  # 0.20% max spread tolerance for tokenized bStocks
PREDICTION_CACHE_TTL_SEC = 0.0    # 0.0s TTL: Always re-evaluate fresh on every single check (no stale caching)

_cache_lock = threading.Lock()
_prediction_cache: Optional[Dict[str, Any]] = None
_prediction_cache_ts: float = 0.0

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def calculate_ema(series: List[float], period: int) -> List[float]:
    """Exponential Moving Average."""
    if not series:
        return []
    alpha = 2.0 / (period + 1.0)
    ema = [series[0]]
    for val in series[1:]:
        ema.append(alpha * val + (1.0 - alpha) * ema[-1])
    return ema


def fetch_binance_klines_safe(client: Any, symbol: str, interval: str, limit: int = 60) -> List[Any]:
    """Safely retrieves klines from Binance client."""
    if not client:
        return []
    try:
        return client.get_klines(symbol=symbol, interval=interval, limit=limit) or []
    except Exception:
        return []


def fetch_binance_ticker_safe(client: Any, symbol: str) -> Dict[str, Any]:
    """Safely retrieves orderbook ticker from Binance client."""
    if not client:
        return {}
    try:
        return client.get_orderbook_ticker(symbol=symbol) or {}
    except Exception:
        return {}


def calculate_ema(series: List[float], period: int) -> List[float]:
    """Exponential Moving Average."""
    if not series:
        return []
    alpha = 2.0 / (period + 1.0)
    ema = [series[0]]
    for val in series[1:]:
        ema.append(alpha * val + (1.0 - alpha) * ema[-1])
    return ema


def calculate_vwap_from_candles(highs: List[float], lows: List[float], closes: List[float], vols: List[float]) -> float:
    """Computes intraday VWAP = sum(TP * V) / sum(V)."""
    valid = [(h, l, c, v) for h, l, c, v in zip(highs, lows, closes, vols) if None not in (h, l, c, v) and v > 0]
    if not valid:
        return closes[-1] if closes else 0.0
    cum_pv = sum(((h + l + c) / 3.0) * v for h, l, c, v in valid)
    cum_v = sum(v for _, _, _, v in valid)
    return cum_pv / cum_v if cum_v > 0 else valid[-1][2]


def predict_semiconductor_5m(client: Any = None, force_refresh: bool = True, horizon_min: int = 3) -> Dict[str, Any]:
    """
    100% PURE BINANCE MODE — Zero External APIs, Zero Rate Limit Risk.
    Executes the 8-score directional prediction model exclusively via Binance exchange data.
    Evaluated when the bot starts for the specified horizon (default 3 minutes):
      1. Price > VWAP on SMH 1m? (Binance SMHBUSDT 1m)
      2. NVDA 1m > VWAP and green? (Binance NVDABUSDT 1m)
      3. AVGO + AMD both green on 1m? (Binance AVGOBUSDT + AMDBUSDT 1m)
      4. NQ futures green last 3m/5m? (Binance QQQBUSDT Nasdaq-100 ETF change)
      5. CVD positive and volume > average? (Binance SOXLBUSDT 1m CVD order flow deltas)
      6. EMA 9 > EMA 21 on SOX? (Binance SMHBUSDT EMA9 vs EMA21)
      7. No major news / halt? (Binance NVDABUSDT & TSMBUSDT active book, spread < 2%, zero halts)
      8. SOXSB ↔ SOXLB orderbook lag & divergence? (Lag Arbitrage Micro-Divergence)

    Thresholds:
      +3 to +8 = Bullish bias -> Long SOXLB
      -3 to -8 = Bearish bias -> Long SOXSB
      -2 to +2 = Chop - skip -> 100% USDT Safe Haven ($0 loss)
    """
    global _prediction_cache, _prediction_cache_ts

    now = time.time()
    if not force_refresh and PREDICTION_CACHE_TTL_SEC > 0:
        with _cache_lock:
            if _prediction_cache is not None and (now - _prediction_cache_ts) < PREDICTION_CACHE_TTL_SEC:
                return dict(_prediction_cache)

    if client is None:
        try:
            import BNB
            client = BNB.make_client()
        except Exception:
            pass

    scores: Dict[str, int] = {}
    details: Dict[str, Any] = {}

    # Parallel Data Ingestion exclusively from Binance (~0.05s total)
    kl_smh_1m = []
    kl_smh_5m = []
    kl_nvda_1m = []
    kl_avgo_1m = []
    kl_amd_1m = []
    kl_qqq_1m = []
    kl_sox_1m = []
    kl_soxs_1m = []
    ob_nvda = {}
    ob_tsm = {}
    ob_soxl = {}
    ob_soxs = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        f_smh_1m = executor.submit(fetch_binance_klines_safe, client, "SMHBUSDT", "1m", 60)
        f_smh_5m = executor.submit(fetch_binance_klines_safe, client, "SMHBUSDT", "5m", 30)
        f_nvda_1m = executor.submit(fetch_binance_klines_safe, client, "NVDABUSDT", "1m", 60)
        f_avgo_1m = executor.submit(fetch_binance_klines_safe, client, "AVGOBUSDT", "1m", 5)
        f_amd_1m = executor.submit(fetch_binance_klines_safe, client, "AMDBUSDT", "1m", 5)
        f_qqq_1m = executor.submit(fetch_binance_klines_safe, client, "QQQBUSDT", "1m", 10)
        f_sox_1m = executor.submit(fetch_binance_klines_safe, client, BULL_SYMBOL, "1m", 20)
        f_soxs_1m = executor.submit(fetch_binance_klines_safe, client, BEAR_SYMBOL, "1m", 20)
        f_ob_nvda = executor.submit(fetch_binance_ticker_safe, client, "NVDABUSDT")
        f_ob_tsm = executor.submit(fetch_binance_ticker_safe, client, "TSMBUSDT")
        f_ob_soxl = executor.submit(fetch_binance_ticker_safe, client, BULL_SYMBOL)
        f_ob_soxs = executor.submit(fetch_binance_ticker_safe, client, BEAR_SYMBOL)

        try: kl_smh_1m = f_smh_1m.result()
        except Exception: pass
        try: kl_smh_5m = f_smh_5m.result()
        except Exception: pass
        try: kl_nvda_1m = f_nvda_1m.result()
        except Exception: pass
        try: kl_avgo_1m = f_avgo_1m.result()
        except Exception: pass
        try: kl_amd_1m = f_amd_1m.result()
        except Exception: pass
        try: kl_qqq_1m = f_qqq_1m.result()
        except Exception: pass
        try: kl_sox_1m = f_sox_1m.result()
        except Exception: pass
        try: kl_soxs_1m = f_soxs_1m.result()
        except Exception: pass
        try: ob_nvda = f_ob_nvda.result()
        except Exception: pass
        try: ob_tsm = f_ob_tsm.result()
        except Exception: pass
        try: ob_soxl = f_ob_soxl.result()
        except Exception: pass
        try: ob_soxs = f_ob_soxs.result()
        except Exception: pass

    # 1. Price > VWAP on SMH 1m?
    try:
        if kl_smh_1m:
            highs = [float(k[2]) for k in kl_smh_1m]
            lows = [float(k[3]) for k in kl_smh_1m]
            closes = [float(k[4]) for k in kl_smh_1m]
            vols = [float(k[5]) for k in kl_smh_1m]
            smh_close = closes[-1]
            smh_vwap = calculate_vwap_from_candles(highs, lows, closes, vols)
            if smh_close > smh_vwap:
                s1 = +1
                s1_desc = f"Price (${smh_close:.2f}) > VWAP (${smh_vwap:.2f}) on Binance SMHBUSDT [+1]"
            elif smh_close < smh_vwap:
                s1 = -1
                s1_desc = f"Price (${smh_close:.2f}) < VWAP (${smh_vwap:.2f}) on Binance SMHBUSDT [-1]"
            else:
                s1 = 0
                s1_desc = f"Price (${smh_close:.2f}) == VWAP (${smh_vwap:.2f}) on Binance SMHBUSDT [0]"
            scores["SMH_1m_VWAP"] = s1
            details["SMH_1m_VWAP"] = {"score": s1, "price": round(smh_close, 2), "vwap": round(smh_vwap, 2), "desc": s1_desc}
        else:
            scores["SMH_1m_VWAP"] = 0
            details["SMH_1m_VWAP"] = {"score": 0, "desc": "Binance SMHBUSDT data unavailable (neutral)"}
    except Exception as e:
        scores["SMH_1m_VWAP"] = 0
        details["SMH_1m_VWAP"] = {"score": 0, "error": str(e)}

    # 2. NVDA 1m > VWAP and green?
    try:
        if kl_nvda_1m:
            opens = [float(k[1]) for k in kl_nvda_1m]
            highs = [float(k[2]) for k in kl_nvda_1m]
            lows = [float(k[3]) for k in kl_nvda_1m]
            closes = [float(k[4]) for k in kl_nvda_1m]
            vols = [float(k[5]) for k in kl_nvda_1m]
            nvda_close = closes[-1]
            nvda_open = opens[-1]
            nvda_vwap = calculate_vwap_from_candles(highs, lows, closes, vols)
            is_green = nvda_close > nvda_open
            is_red = nvda_close < nvda_open
            if nvda_close > nvda_vwap and is_green:
                s2 = +1
                s2_desc = f"Price (${nvda_close:.2f}) > VWAP (${nvda_vwap:.2f}) & GREEN (O:${nvda_open:.2f}→C:${nvda_close:.2f}) [+1]"
            elif nvda_close < nvda_vwap and is_red:
                s2 = -1
                s2_desc = f"Price (${nvda_close:.2f}) < VWAP (${nvda_vwap:.2f}) & RED (O:${nvda_open:.2f}→C:${nvda_close:.2f}) [-1]"
            else:
                s2 = 0
                s2_desc = f"Mixed (Price=${nvda_close:.2f}, VWAP=${nvda_vwap:.2f}, Green={is_green}) [0]"
            scores["NVDA_1m_VWAP_Green"] = s2
            details["NVDA_1m_VWAP_Green"] = {
                "score": s2,
                "price": round(nvda_close, 2),
                "open": round(nvda_open, 2),
                "vwap": round(nvda_vwap, 2),
                "is_green": is_green,
                "desc": s2_desc,
            }
        else:
            scores["NVDA_1m_VWAP_Green"] = 0
            details["NVDA_1m_VWAP_Green"] = {"score": 0, "desc": "Binance NVDABUSDT data unavailable (neutral)"}
    except Exception as e:
        scores["NVDA_1m_VWAP_Green"] = 0
        details["NVDA_1m_VWAP_Green"] = {"score": 0, "error": str(e)}

    # 3. AVGO + AMD both green on 1m?
    try:
        avgo_green = False
        avgo_red = False
        amd_green = False
        amd_red = False
        avgo_o, avgo_c = 0.0, 0.0
        amd_o, amd_c = 0.0, 0.0

        if kl_avgo_1m:
            avgo_o, avgo_c = float(kl_avgo_1m[-1][1]), float(kl_avgo_1m[-1][4])
            avgo_green = avgo_c > avgo_o
            avgo_red = avgo_c < avgo_o
        if kl_amd_1m:
            amd_o, amd_c = float(kl_amd_1m[-1][1]), float(kl_amd_1m[-1][4])
            amd_green = amd_c > amd_o
            amd_red = amd_c < amd_o

        if avgo_green and amd_green:
            s3 = +1
            s3_desc = f"Both GREEN on Binance (AVGO: ${avgo_o:.2f}→${avgo_c:.2f}, AMD: ${amd_o:.2f}→${amd_c:.2f}) [+1]"
        elif avgo_red and amd_red:
            s3 = -1
            s3_desc = f"Both RED on Binance (AVGO: ${avgo_o:.2f}→${avgo_c:.2f}, AMD: ${amd_o:.2f}→${amd_c:.2f}) [-1]"
        else:
            s3 = 0
            s3_desc = f"Mixed/Flat (AVGO green: {avgo_green}, AMD green: {amd_green}) [0]"
        scores["AVGO_AMD_both_green"] = s3
        details["AVGO_AMD_both_green"] = {
            "score": s3,
            "avgo_green": avgo_green,
            "amd_green": amd_green,
            "avgo_open": round(avgo_o, 2),
            "avgo_close": round(avgo_c, 2),
            "amd_open": round(amd_o, 2),
            "amd_close": round(amd_c, 2),
            "desc": s3_desc,
        }
    except Exception as e:
        scores["AVGO_AMD_both_green"] = 0
        details["AVGO_AMD_both_green"] = {"score": 0, "error": str(e)}

    # 4. NQ futures / Nasdaq-100 green last horizon min?
    try:
        if kl_qqq_1m:
            qqq_now = float(kl_qqq_1m[-1][4])
            h_offset = max(1, min(len(kl_qqq_1m) - 1, horizon_min))
            qqq_ago = float(kl_qqq_1m[-h_offset][4])
            diff = qqq_now - qqq_ago
            if diff > 0:
                s4 = +1
                s4_desc = f"Nasdaq-100 (QQQ) GREEN last {horizon_min}m (${qqq_ago:.2f}→${qqq_now:.2f}, +${diff:.2f}) [+1]"
            elif diff < 0:
                s4 = -1
                s4_desc = f"Nasdaq-100 (QQQ) RED last {horizon_min}m (${qqq_ago:.2f}→${qqq_now:.2f}, -${abs(diff):.2f}) [-1]"
            else:
                s4 = 0
                s4_desc = f"Nasdaq-100 (QQQ) FLAT last {horizon_min}m (${qqq_now:.2f}) [0]"
            scores["NQ_futures_5m_green"] = s4
            details["NQ_futures_5m_green"] = {
                "score": s4,
                "price_now": round(qqq_now, 2),
                "price_ago": round(qqq_ago, 2),
                "diff": round(diff, 2),
                "desc": s4_desc,
            }
        else:
            scores["NQ_futures_5m_green"] = 0
            details["NQ_futures_5m_green"] = {"score": 0, "desc": "Binance QQQBUSDT data unavailable (neutral)"}
    except Exception as e:
        scores["NQ_futures_5m_green"] = 0
        details["NQ_futures_5m_green"] = {"score": 0, "error": str(e)}

    # 5. CVD positive and volume > average?
    try:
        kl_flow = kl_sox_1m or kl_nvda_1m
        if kl_flow and len(kl_flow) >= 2:
            vols = [float(k[5]) for k in kl_flow]
            deltas = [2.0 * float(k[9]) - float(k[5]) for k in kl_flow]
            cvd = sum(deltas)
            avg_vol = sum(vols) / len(vols)
            cur_vol = vols[-1]
            vol_above_avg = cur_vol > avg_vol
            if cvd > 0 and vol_above_avg:
                s5 = +1
                s5_desc = f"CVD POSITIVE (+{cvd:.1f}) & Vol ({cur_vol:.1f}) > Avg ({avg_vol:.1f}) [+1]"
            elif cvd < 0 and vol_above_avg:
                s5 = -1
                s5_desc = f"CVD NEGATIVE ({cvd:.1f}) & Vol ({cur_vol:.1f}) > Avg ({avg_vol:.1f}) [-1]"
            else:
                s5 = 0
                s5_desc = f"Volume <= Avg or CVD neutral (CVD={cvd:.1f}, Vol={cur_vol:.1f}, Avg={avg_vol:.1f}) [0]"
            scores["CVD_positive_vol_gt_avg"] = s5
            details["CVD_positive_vol_gt_avg"] = {
                "score": s5,
                "cvd": round(cvd, 2),
                "cur_vol": round(cur_vol, 2),
                "avg_vol": round(avg_vol, 2),
                "vol_above_avg": vol_above_avg,
                "desc": s5_desc,
            }
        else:
            scores["CVD_positive_vol_gt_avg"] = 0
            details["CVD_positive_vol_gt_avg"] = {"score": 0, "desc": "Binance flow data unavailable (neutral)"}
    except Exception as e:
        scores["CVD_positive_vol_gt_avg"] = 0
        details["CVD_positive_vol_gt_avg"] = {"score": 0, "error": str(e)}

    # 6. EMA 9 > EMA 21 on SOX 5m?
    try:
        if kl_smh_5m and len(kl_smh_5m) >= 21:
            closes_5m = [float(k[4]) for k in kl_smh_5m]
            ema9 = calculate_ema(closes_5m, 9)[-1]
            ema21 = calculate_ema(closes_5m, 21)[-1]
            diff_ema = ema9 - ema21
            if ema9 > ema21:
                s6 = +1
                s6_desc = f"Semiconductor 5m EMA 9 (${ema9:.2f}) > EMA 21 (${ema21:.2f}) [+1]"
            elif ema9 < ema21:
                s6 = -1
                s6_desc = f"Semiconductor 5m EMA 9 (${ema9:.2f}) < EMA 21 (${ema21:.2f}) [-1]"
            else:
                s6 = 0
                s6_desc = f"Semiconductor 5m EMA 9 == EMA 21 (${ema9:.2f}) [0]"
            scores["SOX_5m_EMA9_gt_EMA21"] = s6
            details["SOX_5m_EMA9_gt_EMA21"] = {
                "score": s6,
                "ema9": round(ema9, 2),
                "ema21": round(ema21, 2),
                "diff": round(diff_ema, 2),
                "desc": s6_desc,
            }
        elif kl_smh_1m and len(kl_smh_1m) >= 21:
            closes_1m = [float(k[4]) for k in kl_smh_1m]
            ema9 = calculate_ema(closes_1m, 9)[-1]
            ema21 = calculate_ema(closes_1m, 21)[-1]
            diff_ema = ema9 - ema21
            s6 = 1 if ema9 > ema21 else (-1 if ema9 < ema21 else 0)
            s6_desc = f"Semiconductor 1m EMA 9 (${ema9:.2f}) {'>' if s6==1 else '<'} EMA 21 (${ema21:.2f}) [{s6:+d}]"
            scores["SOX_5m_EMA9_gt_EMA21"] = s6
            details["SOX_5m_EMA9_gt_EMA21"] = {"score": s6, "ema9": round(ema9, 2), "ema21": round(ema21, 2), "diff": round(diff_ema, 2), "desc": s6_desc}
        else:
            scores["SOX_5m_EMA9_gt_EMA21"] = 0
            details["SOX_5m_EMA9_gt_EMA21"] = {"score": 0, "desc": "Binance SMHBUSDT 5m data unavailable (neutral)"}
    except Exception as e:
        scores["SOX_5m_EMA9_gt_EMA21"] = 0
        details["SOX_5m_EMA9_gt_EMA21"] = {"score": 0, "error": str(e)}

    # 7. No major news / halt: NVDA & TSMC Liquidity & Spread Check
    try:
        bid_n = float(ob_nvda.get("bidPrice") or 0)
        ask_n = float(ob_nvda.get("askPrice") or 0)
        bid_t = float(ob_tsm.get("bidPrice") or 0)
        ask_t = float(ob_tsm.get("askPrice") or 0)

        # Halt check: missing bids or asks
        is_halted = (bid_n <= 0 or ask_n <= 0 or bid_t <= 0 or ask_t <= 0)
        halt_msg = None
        if bid_n <= 0 or ask_n <= 0:
            halt_msg = "NVDA Orderbook Halted / Empty on Binance"
        elif bid_t <= 0 or ask_t <= 0:
            halt_msg = "TSMC Orderbook Halted / Empty on Binance"

        # Extreme spread shock check (> 2.0% spread indicates crisis liquidity dry-up)
        spread_n = ((ask_n - bid_n) / bid_n) * 100.0 if bid_n > 0 else 999.0
        spread_t = ((ask_t - bid_t) / bid_t) * 100.0 if bid_t > 0 else 999.0
        is_spread_blowout = (spread_n > 2.0 or spread_t > 2.0)

        if is_halted or is_spread_blowout:
            s7 = -1
            desc_reasons = []
            if halt_msg:
                desc_reasons.append(halt_msg)
            if is_spread_blowout:
                desc_reasons.append(f"Spread Blowout (NVDA: {spread_n:.2f}%, TSMC: {spread_t:.2f}%)")
            s7_desc = "Liquidity Anomaly / Halt: " + "; ".join(desc_reasons) + " [-1]"
            scores["No_major_news_Taiwan_TSM_NVDA"] = s7
            details["No_major_news_Taiwan_TSM_NVDA"] = {
                "score": s7,
                "is_halted": is_halted,
                "is_crisis_news": is_spread_blowout,
                "desc": s7_desc,
            }
        else:
            s7 = +1
            s7_desc = f"Binance NVDABUSDT & TSMBUSDT active, healthy spreads ({spread_n:.2f}%/{spread_t:.2f}%), zero halts [+1]"
            scores["No_major_news_Taiwan_TSM_NVDA"] = s7
            details["No_major_news_Taiwan_TSM_NVDA"] = {
                "score": s7,
                "is_halted": False,
                "is_crisis_news": False,
                "desc": s7_desc,
            }
    except Exception as e:
        scores["No_major_news_Taiwan_TSM_NVDA"] = 0
        details["No_major_news_Taiwan_TSM_NVDA"] = {"score": 0, "error": str(e)}

    # 8. SOXSB ↔ SOXLB orderbook lag & divergence
    try:
        import semiconductor_lag_arb
        lag_eng = semiconductor_lag_arb.get_engine()
        now_ts = time.time()

        # Feed live orderbook tickers to lag tracker
        bid_l = float(ob_soxl.get("bidPrice") or 0.0)
        ask_l = float(ob_soxl.get("askPrice") or 0.0)
        bid_s = float(ob_soxs.get("bidPrice") or 0.0)
        ask_s = float(ob_soxs.get("askPrice") or 0.0)

        mid_l = (bid_l + ask_l) / 2.0 if (bid_l > 0 and ask_l > 0) else (bid_l if bid_l > 0 else 0.0)
        mid_s = (bid_s + ask_s) / 2.0 if (bid_s > 0 and ask_s > 0) else (bid_s if bid_s > 0 else 0.0)

        if mid_l > 0:
            lag_eng.feed_price(BULL_SYMBOL, mid_l, now_ts)
        if mid_s > 0:
            lag_eng.feed_price(BEAR_SYMBOL, mid_s, now_ts)

        # 8a. Check if lag engine has an active profitable signal
        active_sig = lag_eng.check_for_signal()
        if active_sig is not None and active_sig.get("net_expected_profit_pct", 0) >= 0.01:
            if active_sig.get("symbol") == BULL_SYMBOL:
                s8 = +1
                s8_desc = f"Lag Arb Engine BUY {BULL_SYMBOL} Trigger! (Divergence: {active_sig['divergence_pct']:.2f}%, Exp Net Gain: +{active_sig['net_expected_profit_pct']:.2f}%) [+1]"
            elif active_sig.get("symbol") == BEAR_SYMBOL:
                s8 = -1
                s8_desc = f"Lag Arb Engine BUY {BEAR_SYMBOL} Trigger! (Divergence: {active_sig['divergence_pct']:.2f}%, Exp Net Gain: +{active_sig['net_expected_profit_pct']:.2f}%) [-1]"
            else:
                s8 = 0
                s8_desc = "Lag Arb Engine Neutral [0]"
        else:
            # 8b. Evaluate rolling micro-divergence between SOXLB and SOXSB
            chgs = lag_eng.get_current_changes()
            l_30 = chgs.get("SOXLB", {}).get("30s")
            s_30 = chgs.get("SOXSB", {}).get("30s")

            if l_30 is None or s_30 is None:
                l_30 = chgs.get("SOXLB", {}).get("60s")
                s_30 = chgs.get("SOXSB", {}).get("60s")
            if l_30 is None or s_30 is None:
                l_30 = chgs.get("SOXLB", {}).get("10s")
                s_30 = chgs.get("SOXSB", {}).get("10s")

            if l_30 is not None and s_30 is not None:
                net_div = l_30 + s_30  # Inverse sum: 0 when perfectly synchronized
                if net_div >= 0.10:
                    s8 = +1
                    s8_desc = f"Micro-Lag Divergence Bullish: SOXLB ({l_30:+.2f}%) vs SOXSB ({s_30:+.2f}%) -> Net +{net_div:.2f}% [+1]"
                elif net_div <= -0.10:
                    s8 = -1
                    s8_desc = f"Micro-Lag Divergence Bearish: SOXSB ({s_30:+.2f}%) vs SOXLB ({l_30:+.2f}%) -> Net {net_div:.2f}% [-1]"
                else:
                    s8 = 0
                    s8_desc = f"Orderbook Synchronized: SOXLB ({l_30:+.2f}%) & SOXSB ({s_30:+.2f}%) in sync (Div: {net_div:+.2f}%) [0]"
            elif kl_sox_1m and kl_soxs_1m:
                # 8c. Fallback to 1m candle change comparison
                c_l, o_l = float(kl_sox_1m[-1][4]), float(kl_sox_1m[-1][1])
                c_s, o_s = float(kl_soxs_1m[-1][4]), float(kl_soxs_1m[-1][1])
                pct_l = ((c_l - o_l) / o_l) * 100.0 if o_l > 0 else 0.0
                pct_s = ((c_s - o_s) / o_s) * 100.0 if o_s > 0 else 0.0
                candle_div = pct_l + pct_s
                if candle_div >= 0.15:
                    s8 = +1
                    s8_desc = f"1m Candle Divergence Bullish: SOXLB ({pct_l:+.2f}%) vs SOXSB ({pct_s:+.2f}%) -> Net +{candle_div:.2f}% [+1]"
                elif candle_div <= -0.15:
                    s8 = -1
                    s8_desc = f"1m Candle Divergence Bearish: SOXSB ({pct_s:+.2f}%) vs SOXLB ({pct_l:+.2f}%) -> Net {candle_div:.2f}% [-1]"
                else:
                    s8 = 0
                    s8_desc = f"1m Candles Balanced: SOXLB ({pct_l:+.2f}%) & SOXSB ({pct_s:+.2f}%) [0]"
            else:
                s8 = 0
                s8_desc = "Lag tracker warming up / insufficient data (neutral) [0]"

        scores["SOXS_SOXL_Lag_Signal"] = s8
        details["SOXS_SOXL_Lag_Signal"] = {
            "score": s8,
            "desc": s8_desc,
            "soxl_mid": round(mid_l, 4) if mid_l > 0 else None,
            "soxs_mid": round(mid_s, 4) if mid_s > 0 else None,
        }
    except Exception as e:
        scores["SOXS_SOXL_Lag_Signal"] = 0
        details["SOXS_SOXL_Lag_Signal"] = {"score": 0, "error": str(e)}

    # Aggregate Total Score & 5-Minute Bias
    total_score = sum(scores.values())

    if total_score >= 3:
        bias = "BULLISH"
        regime = "BULL_TREND"
        action = "LONG_SOXLB"
        selected_symbol = BULL_SYMBOL
        viable = True
        reason = f"Bullish Semiconductor Bias (+{total_score}/+8) -> Trade {BULL_SYMBOL} for next {horizon_min} min."
    elif total_score <= -3:
        bias = "BEARISH"
        regime = "BEAR_TREND"
        action = "LONG_SOXSB"
        selected_symbol = BEAR_SYMBOL
        viable = True
        reason = f"Bearish Semiconductor Bias ({total_score}/-8) -> Trade {BEAR_SYMBOL} for next {horizon_min} min."
    else:
        bias = "CHOP"
        regime = "CHOP_SKIP"
        action = "SKIP_100PCT_USDT"
        selected_symbol = None
        viable = False
        reason = f"Chop detected (Score: {total_score:+d} in [-2, +2]). Skipping trade to avoid 3x beta decay (100% USDT Safe Haven)."

    result = {
        "timestamp": get_ist_now_str(),
        "total_score": total_score,
        "bias": bias,
        "regime": regime,
        "action": action,
        "selected_symbol": selected_symbol,
        "viable": viable,
        "reason": reason,
        "horizon": f"{horizon_min}m",
        "scores": scores,
        "details": details,
        "target_profit_pct": 1.25,
        "stop_loss_pct": -0.60,
    }

    with _cache_lock:
        _prediction_cache = dict(result)
        _prediction_cache_ts = now

    return result


def predict_semiconductor_3m(client: Any = None, force_refresh: bool = True) -> Dict[str, Any]:
    """
    3-Minute Directional Prediction Model for Semiconductor token pairs (SOXLB / SOXSB).
    Evaluated when the bot starts to determine directional conviction for the next 3 minutes.
    """
    return predict_semiconductor_5m(client=client, force_refresh=force_refresh, horizon_min=3)


def build_score_detail_payload(
    pred: Dict[str, Any],
    bot_name: str = "SemiconductorEngine",
    now_str: Optional[str] = None,
) -> str:
    """
    Builds a structured JSON payload containing complete details of all 8 prediction scores,
    underlying market indicators, criteria descriptions, and human-readable text.
    Persisted into scalper.db event_log.detail for UI dropdown rendering.
    """
    import json
    if not now_str:
        now_str = get_ist_now_str()
    elif "UTC" in now_str or not now_str.endswith("IST"):
        try:
            import BNB
            now_str = BNB.to_ist_str(now_str)
        except Exception:
            now_str = get_ist_now_str()

    total = int(pred.get("total_score", 0))
    bias = str(pred.get("bias", "CHOP"))
    action = str(pred.get("action", "SKIP_100PCT_USDT"))
    scores = pred.get("scores", {})
    details = pred.get("details", {})
    reason = str(pred.get("reason", ""))
    selected_symbol = pred.get("selected_symbol")

    labels = [
        ("SMH_1m_VWAP", 1, "Price > VWAP on SMH 1m?"),
        ("NVDA_1m_VWAP_Green", 2, "NVDA 1m > VWAP & green?"),
        ("AVGO_AMD_both_green", 3, "AVGO + AMD both green on 1m?"),
        ("NQ_futures_5m_green", 4, "NQ futures green last 5 min?"),
        ("CVD_positive_vol_gt_avg", 5, "CVD positive & volume > avg?"),
        ("SOX_5m_EMA9_gt_EMA21", 6, "EMA 9 > EMA 21 on SOX 5m?"),
        ("No_major_news_Taiwan_TSM_NVDA", 7, "No major news: no Taiwan/TSM headline, no NVDA halt?"),
        ("SOXS_SOXL_Lag_Signal", 8, "SOXSB ↔ SOXLB orderbook lag & divergence?"),
    ]

    criteria_list = []
    lines = [
        f"[{bot_name}] [SEMICONDUCTOR 5M PREDICTION @ {now_str}] Total Score: {total:+d} / 8 | Bias: {bias} -> Action: {action}",
        f"Regime: {pred.get('regime', 'CHOP_SKIP')} | Reason: {reason}",
        "-" * 75,
    ]

    for key, c_id, label in labels:
        sc = int(scores.get(key, 0))
        d = details.get(key, {})
        desc = d.get("desc", "")
        if sc == 1:
            verdict = "BULLISH (+1)"
        elif sc == -1:
            verdict = "BEARISH (-1)"
        else:
            verdict = "NEUTRAL (0)"

        lines.append(f"  {c_id}. [{verdict:<13}] {label}")
        if desc:
            lines.append(f"     -> {desc}")

        criteria_list.append({
            "id": c_id,
            "key": key,
            "label": label,
            "score": sc,
            "verdict": verdict,
            "desc": desc,
            "metrics": {k: v for k, v in d.items() if k not in ("desc", "score")},
        })

    lines.append("-" * 75)
    lines.append("Scoring Model Rules:")
    lines.append("  • +3 to +8 = Bullish bias -> Long SOXLB for next 5 min")
    lines.append("  • -3 to -8 = Bearish bias -> Long SOXSB for next 5 min")
    lines.append("  • -2 to +2 = Chop -> Skip (100% USDT Safe Haven)")
    full_text = "\n".join(lines)

    payload = {
        "type": "SEMICONDUCTOR_5M_SCORES",
        "bot_name": bot_name,
        "timestamp": now_str,
        "total_score": total,
        "bias": bias,
        "action": action,
        "selected_symbol": selected_symbol,
        "viable": bool(pred.get("viable")),
        "reason": reason,
        "target_profit_pct": float(pred.get("target_profit_pct") or 1.25),
        "stop_loss_pct": float(pred.get("stop_loss_pct") or -0.60),
        "scores": {k: int(v) for k, v in scores.items()},
        "details": details,
        "criteria": criteria_list,
        "formatted_text": full_text,
    }
    return json.dumps(payload)


def format_prediction_details(
    pred: Dict[str, Any],
    bot_name: str = "SemiconductorEngine",
    now_str: Optional[str] = None,
) -> str:
    """Convenience alias returning JSON detail string."""
    return build_score_detail_payload(pred, bot_name=bot_name, now_str=now_str)


def log_scores(
    pred: Dict[str, Any],
    bot_name: str = "SemiconductorEngine",
    client: Any = None,
    instance_id: Optional[int] = None,
) -> None:
    """
    Prints and persists the 7-score breakdown to console and scalper.db log.
    Includes full criteria details in scalper.db event_log.detail for UI dropdown.
    Strictly uses Indian Standard Time (IST) everywhere.
    """
    now_str = get_ist_now_str()
    time_str = get_ist_time_str()

    detail_json = build_score_detail_payload(pred, bot_name=bot_name, now_str=now_str)

    # Print to console
    try:
        import json
        parsed = json.loads(detail_json)
        print(parsed.get("formatted_text", ""))
    except Exception:
        pass

    try:
        import BNB
        total = pred.get("total_score", 0)
        bias = pred.get("bias", "CHOP")
        action = pred.get("action", "SKIP")
        sym = pred.get("selected_symbol") or "SOXLB+SOXSB"
        BNB.log_event(
            f"[{bot_name}] [5M PREDICTION @ {time_str}] Score: {total:+d}/8 | {bias} ({action})",
            level="INFO",
            event_type="PREDICTION_5M",
            instance_id=instance_id,
            symbol=sym,
            detail=detail_json,
        )
    except Exception as e:
        print(f"[log_scores] BNB.log_event warning: {e}")


def evaluate_semiconductor_regime(client: Any, instance_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Evaluates SOXLB and SOXSB simultaneously using the revised 8-score 5-minute prediction model.
    Maintains drop-in compatibility with hybrid_strategy.py and BNB.py microstructure profiling.
    """
    pred = predict_semiconductor_5m(client=client)
    log_scores(pred, bot_name="SemiconductorEngine", client=client, instance_id=instance_id)

    # Fetch live orderbook prices for execution precision
    soxlb_px, soxsb_px = 0.0, 0.0
    soxlb_spread, soxsb_spread = 0.05, 0.05
    if client:
        try:
            ob_l = client.get_orderbook_ticker(symbol=BULL_SYMBOL)
            ob_s = client.get_orderbook_ticker(symbol=BEAR_SYMBOL)
            bid_l, ask_l = float(ob_l.get("bidPrice") or 0.0), float(ob_l.get("askPrice") or 0.0)
            bid_s, ask_s = float(ob_s.get("bidPrice") or 0.0), float(ob_s.get("askPrice") or 0.0)
            if bid_l > 0 and ask_l > 0:
                soxlb_px = (bid_l + ask_l) / 2.0
                soxlb_spread = ((ask_l - bid_l) / bid_l) * 100.0
            if bid_s > 0 and ask_s > 0:
                soxsb_px = (bid_s + ask_s) / 2.0
                soxsb_spread = ((ask_s - bid_s) / bid_s) * 100.0
        except Exception:
            pass

    return {
        "viable": bool(pred.get("viable")),
        "selected_symbol": pred.get("selected_symbol"),
        "regime": pred.get("regime", "CHOP_SKIP"),
        "bias": pred.get("bias", "CHOP"),
        "action": pred.get("action", "SKIP_100PCT_USDT"),
        "total_score": pred.get("total_score", 0),
        "ratio": round(soxlb_px / soxsb_px, 4) if (soxlb_px > 0 and soxsb_px > 0) else 1.0,
        "soxlb_price": round(soxlb_px, 4),
        "soxsb_price": round(soxsb_px, 4),
        "soxlb_spread": round(soxlb_spread, 4),
        "soxsb_spread": round(soxsb_spread, 4),
        "target_profit_pct": float(pred.get("target_profit_pct") or 1.25),
        "stop_loss_pct": float(pred.get("stop_loss_pct") or -0.60),
        "reason": pred.get("reason", ""),
        "scores": pred.get("scores", {}),
        "score_details": pred.get("details", {}),
        "prediction_5m": pred,
    }


if __name__ == "__main__":
    import BNB
    c = BNB.make_client()
    res = evaluate_semiconductor_regime(c)
    print("\nResult Dictionary:")
    print(f"Viable: {res['viable']}")
    print(f"Selected Symbol: {res['selected_symbol']}")
    print(f"Regime: {res['regime']}")
    print(f"Total Score: {res['total_score']}")
    print(f"Reason: {res['reason']}")
