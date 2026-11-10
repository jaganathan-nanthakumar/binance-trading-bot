"""
scan_hybrid_regimes.py — Scans top USDT pairs with the Hybrid Market Regime Predictor.
"""

import time
import BNB
import hybrid_strategy

client = BNB.make_client()
tickers = client.get_ticker()
usdt_pairs = [
    t["symbol"]
    for t in sorted(tickers, key=lambda x: float(x.get("quoteVolume") or 0), reverse=True)
    if t["symbol"].endswith("USDT")
    and not any(b in t["symbol"] for b in ["UPUSDT", "DOWNUSDT", "BEARUSDT", "BULLUSDT", "USDCUSDT", "FDUSDUSDT"])
][:20]

print("=" * 120)
print("Scanning top 20 USDT pairs with Hybrid Quantitative Market Regime Predictor...")
print("=" * 120)
start = time.time()

print(f"{'Symbol':<10} | {'Predicted Market':<18} | {'Algorithm':<22} | {'Freq':<9} | {'Speed?':<7} | {'TP Target':<10} | {'SL Floor':<9} | {'Trailing':<8}")
print("-" * 120)

for sym in usdt_pairs:
    try:
        pred = hybrid_strategy.predict_market_and_strategy(client, sym, investment_usdt=25.0)
        ep = pred.get("execution_params", {})
        mkt = pred.get("predicted_market", "N/A")
        algo = pred.get("strategy_name", "N/A")
        freq = pred.get("trade_frequency", "Medium")
        speed = pred.get("needs_speed", "No")
        tp = ep.get("target_profit_pct", 0.0)
        sl = ep.get("stop_loss_pct", 0.0)
        trail = "ACTIVE" if ep.get("trailing_stop_enabled") else "OFF"
        print(f"{sym:<10} | {mkt:<18} | {algo:<22} | {freq:<9} | {speed:<7} | +{tp:.2f}%     | {sl:.2f}%   | {trail:<8}")
    except Exception as e:
        print(f"{sym:<10} | Error: {e}")

print("=" * 120)
print(f"Scan completed in {time.time() - start:.2f}s!")
