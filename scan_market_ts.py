import BNB
import ts_forecast
import time

client = BNB.make_client()
tickers = client.get_ticker()
usdt_pairs = [
    t['symbol'] for t in sorted(tickers, key=lambda x: float(x.get('quoteVolume') or 0), reverse=True)
    if t['symbol'].endswith('USDT') and not any(b in t['symbol'] for b in ['UPUSDT', 'DOWNUSDT', 'BEARUSDT', 'BULLUSDT'])
][:25]

print("Scanning top 25 USDT pairs with Time Series Forecaster & First Candle Analysis (FCA)...")
start = time.time()
ranked, forecasts = ts_forecast.rank_pairs_with_forecasting(client, usdt_pairs, max_workers=8)
print(f"Scanned {len(ranked)} pairs in {time.time() - start:.2f}s!\n")

print(f"{'Symbol':<10} | {'Pred Ret':<9} | {'FCA Setup':<20} | {'BodyDom':<7} | {'RVol':<5} | {'TP Target':<9} | {'SL Floor':<8} | {'Score':<7}")
print("-" * 95)
for sym in ranked[:15]:
    f = forecasts.get(sym, {})
    fca = f.get('fca', {})
    setup = fca.get('setup_type', 'UNKNOWN')
    body_dom = fca.get('body_dominance', 0.0)
    rvol = fca.get('relative_volume', 1.0)
    print(f"{sym:<10} | {f.get('pred_return_15m_pct', 0.0):+8.2f}% | {setup:<20} | {body_dom:6.2f}  | {rvol:4.2f} | +{f.get('forecast_tp_pct', 0.0):.2f}%    | {f.get('forecast_sl_pct', 0.0):.2f}%  | {f.get('forecast_score', 0.0):+6.2f}")
