"""
pca_selector.py — Principal Component Analysis (PCA) for Crypto Coin Selection.

Decomposes cross-sectional return dynamics across candidate USDT pairs into:
1. Common Systematic Market Mode (PC1) — Broad market / BTC beta tide.
2. Idiosyncratic Residual Return (Alpha) — Decoupled asset-specific buying/selling pressure.

Coins demonstrating strong positive idiosyncratic alpha (decoupling upwards from market chop)
are prioritized for scalping entries, while coins merely dragged along by passive beta or
experiencing negative residual drift are penalized and filtered out.

Operates with sub-millisecond pure-NumPy SVD and concurrent kline retrieval with memory caching.
"""

from __future__ import annotations

import math
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Tuple, Optional
import numpy as np

# Cache state for candidate PCA calculations
_PCA_CACHE_LOCK = threading.Lock()
_PCA_CACHE: Dict[str, Any] = {
    "timestamp": 0.0,
    "symbols": [],
    "metrics": {},      # symbol -> {"residual_alpha": float, "market_beta": float, "pca_score": float, ...}
    "ranked": [],       # list of symbols sorted by pca preference
}

PCA_CACHE_TTL = 25.0  # seconds to reuse PCA decomposition across concurrent bots


def compute_pca_market_decomposition(
    returns_matrix: np.ndarray,
    symbols: List[str],
    n_components: int = 1,
) -> Dict[str, Dict[str, float]]:
    """
    Perform SVD on the standardized cross-sectional returns matrix.
    
    Parameters:
        returns_matrix: Shape (T, N) where T = time periods (e.g. 30 1m bars), N = coins.
        symbols: List of N symbol strings corresponding to columns.
        n_components: Number of systematic market modes to remove (default 1).
        
    Returns:
        Dict mapping symbol -> {
            "residual_alpha": float, # recent idiosyncratic outperformance
            "market_beta": float,    # sensitivity to systematic market mode
            "pca_score": float,      # composite score to rank/prefer coin
            "explained_var": float,  # % of cross-sectional variance driven by PC1
        }
    """
    T, N = returns_matrix.shape
    default_res = {
        s: {
            "residual_alpha": 0.0,
            "market_beta": 1.0,
            "pca_score": 0.0,
            "explained_var": 0.0,
        }
        for s in symbols
    }

    if N < 2 or T < 5:
        return default_res

    # Clean any non-finite entries
    clean_mat = np.nan_to_num(returns_matrix, nan=0.0, posinf=0.0, neginf=0.0)

    # Standardize returns across time (zero mean, unit variance)
    col_means = np.mean(clean_mat, axis=0)
    col_stds = np.std(clean_mat, axis=0)
    col_stds[col_stds < 1e-8] = 1.0  # avoid division by zero on flat candles
    Z = (clean_mat - col_means) / col_stds

    try:
        # Economy SVD: Z = U * S * Vt
        # U has shape (T, min(T, N)), S has shape (min(T, N)), Vt has shape (min(T, N), N)
        U, S, Vt = np.linalg.svd(Z, full_matrices=False)
        V = Vt.T  # columns of V are eigenvectors of sample covariance

        # Ensure PC1 (Market Mode) points in the direction of the market average
        # (Positive correlation with equal-weighted market basket)
        if np.sum(V[:, 0]) < 0:
            V[:, 0] *= -1.0
            U[:, 0] *= -1.0

        # Systematic component reconstruction from top n_components
        k = max(1, min(n_components, N, T))
        Z_sys = np.dot(U[:, :k] * S[:k], V[:, :k].T)

        # Idiosyncratic residual return matrix
        Z_res = Z - Z_sys

        # Explained variance ratio of PC1
        var_total = float(np.sum(S ** 2))
        explained_var = float((S[0] ** 2) / var_total) if var_total > 1e-12 else 0.0

        # Exponential decay weights over lookback (prioritize recent candles)
        weights = np.exp(np.linspace(-1.5, 0.0, T))
        weights /= np.sum(weights)

        # Scale factor loadings so average beta = 1.0
        raw_betas = V[:, 0]
        mean_beta = float(np.mean(raw_betas))
        norm_factor = (1.0 / mean_beta) if abs(mean_beta) > 1e-5 else 1.0

        results = {}
        for i, sym in enumerate(symbols):
            res_i = Z_res[:, i]

            # Weighted residual alpha over entire window
            w_alpha = float(np.sum(weights * res_i))

            # Ultra-recent 5-bar residual momentum
            recent_alpha = float(np.mean(res_i[-5:])) if T >= 5 else w_alpha

            # Combined alpha metric: weighted blend
            alpha_metric = (0.65 * recent_alpha) + (0.35 * w_alpha)
            beta = float(raw_betas[i] * norm_factor)

            # Signal-to-noise ratio of residual in recent window
            tail_res = res_i[-10:] if T >= 10 else res_i
            res_snr = float(np.mean(tail_res) / (np.std(tail_res) + 1e-4))

            # PCA score:
            # - Strongly rewards positive idiosyncratic decoupling
            # - Penalizes negative decoupling (dragging or distribution)
            # - Modulated by residual stability (SNR)
            score = (alpha_metric * 2.2) + (math.tanh(res_snr) * 0.8)
            score = max(-3.0, min(3.0, score))

            results[sym] = {
                "residual_alpha": round(alpha_metric, 4),
                "market_beta": round(beta, 4),
                "pca_score": round(score, 4),
                "explained_var": round(explained_var, 4),
            }

        return results

    except Exception as e:
        # Fallback gracefully on any SVD failure
        return default_res


def _fetch_single_symbol_returns(client: Any, symbol: str, limit: int = 30) -> Tuple[str, Optional[np.ndarray]]:
    """Fetch recent 1m closes and return log returns vector."""
    try:
        klines = client.get_klines(symbol=symbol, interval="1m", limit=limit)
        if not klines or len(klines) < 5:
            return symbol, None
        closes = [float(k[4]) for k in klines]
        prices = np.array(closes, dtype=float)
        # Compute log returns
        ret = np.diff(np.log(np.maximum(prices, 1e-8)))
        return symbol, ret
    except Exception:
        return symbol, None


def fetch_candidate_returns(
    client: Any,
    symbols: List[str],
    limit: int = 30,
    max_workers: int = 6,
) -> Tuple[np.ndarray, List[str]]:
    """
    Concurrently fetch recent 1m returns for candidates.
    Returns: (returns_matrix, valid_symbols)
    """
    if not symbols:
        return np.empty((0, 0)), []

    valid_symbols = []
    returns_list = []

    with ThreadPoolExecutor(max_workers=min(max_workers, len(symbols))) as executor:
        results = list(executor.map(lambda s: _fetch_single_symbol_returns(client, s, limit), symbols))

    # Determine minimum valid length to align arrays
    valid_pairs = [(sym, ret) for sym, ret in results if ret is not None and len(ret) >= 5]
    if not valid_pairs:
        return np.empty((0, 0)), []

    min_len = min(len(ret) for _, ret in valid_pairs)
    for sym, ret in valid_pairs:
        valid_symbols.append(sym)
        returns_list.append(ret[-min_len:])

    returns_matrix = np.column_stack(returns_list)
    return returns_matrix, valid_symbols


def rank_candidates_with_pca(
    client: Any,
    candidate_symbols: List[str],
    max_candidates: int = 14,
) -> Tuple[List[str], Dict[str, Dict[str, float]]]:
    """
    Ranks candidate symbols by PCA idiosyncratic outperformance.
    Employs an in-memory TTL cache to minimize API calls across fast concurrent loops.
    
    Returns:
        (ranked_symbols, metrics_dict)
    """
    global _PCA_CACHE

    if not candidate_symbols:
        return [], {}

    now = time.time()
    clean_symbols = [s for s in candidate_symbols if isinstance(s, str) and s.endswith("USDT")]

    # Check cache validity
    with _PCA_CACHE_LOCK:
        cached_ts = _PCA_CACHE.get("timestamp", 0.0)
        cached_metrics = _PCA_CACHE.get("metrics", {})
        if (now - cached_ts) < PCA_CACHE_TTL and cached_metrics:
            # Check if majority of candidates are present in cache
            known_syms = [s for s in clean_symbols if s in cached_metrics]
            if len(known_syms) >= max(2, int(len(clean_symbols) * 0.6)):
                # Return cached re-ranking
                ranked = sorted(
                    clean_symbols,
                    key=lambda s: cached_metrics.get(s, {}).get("pca_score", 0.0),
                    reverse=True,
                )
                return ranked, cached_metrics

    # Pool candidates for PCA (limit to max_candidates for speed and API weight)
    pool = clean_symbols[:max_candidates]
    if len(pool) < 2:
        return clean_symbols, {}

    try:
        ret_matrix, valid_symbols = fetch_candidate_returns(client, pool, limit=30)
        if len(valid_symbols) < 2:
            return clean_symbols, {}

        metrics = compute_pca_market_decomposition(ret_matrix, valid_symbols, n_components=1)

        # Re-sort full candidate list: known valid symbols by pca_score desc, un-fetched at tail
        ranked = sorted(
            clean_symbols,
            key=lambda s: metrics.get(s, {}).get("pca_score", -999.0) if s in metrics else -1000.0,
            reverse=True,
        )

        with _PCA_CACHE_LOCK:
            _PCA_CACHE["timestamp"] = now
            _PCA_CACHE["symbols"] = clean_symbols
            _PCA_CACHE["metrics"] = metrics
            _PCA_CACHE["ranked"] = ranked

        return ranked, metrics

    except Exception:
        # On any network or calculation error, preserve original order
        return clean_symbols, {}


def get_cached_pca_metric_for_symbol(symbol: str) -> Optional[Dict[str, float]]:
    """Retrieve the latest cached PCA metric for a single symbol if fresh."""
    global _PCA_CACHE
    now = time.time()
    with _PCA_CACHE_LOCK:
        if (now - _PCA_CACHE.get("timestamp", 0.0)) < (PCA_CACHE_TTL * 2.0):
            return _PCA_CACHE.get("metrics", {}).get(symbol)
    return None
