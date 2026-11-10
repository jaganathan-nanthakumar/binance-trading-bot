import sqlite3
from BNB import make_client

client = make_client()
conn = sqlite3.connect('scalper.db')
c = conn.cursor()

instances = c.execute('SELECT id, name, status, current_symbol, current_pnl, investment_usdt FROM bot_instances').fetchall()
print("=== ACTIVE BOT INSTANCES ===")
for inst in instances:
    print(f"Instance {inst[0]} ({inst[1]}): Status={inst[2]} | Symbol={inst[3]} | Current Unrealized PnL={inst[4]:.4f} USDT | Invested={inst[5]} USDT")

print("\n=== OPEN ORDERS ON BINANCE ===")
orders = client.get_open_orders()
for o in orders:
    sym = o['symbol']
    ticker = client.get_orderbook_ticker(symbol=sym)
    bid = float(ticker['bidPrice'])
    ask = float(ticker['askPrice'])
    ord_px = float(o['price'])
    diff_pct = (ord_px - bid) / bid * 100
    print(f"Symbol: {sym:<10} | Side: {o['side']} | Qty: {o['origQty']:<10} | Limit Sell Target: {ord_px:<10} | Current Bid: {bid:<10} | Distance to Fill: +{diff_pct:.2f}%")

print("\n=== WALLET BALANCES ===")
acct = client.get_account()
for b in acct['balances']:
    free = float(b['free'])
    locked = float(b['locked'])
    if free > 0 or locked > 0:
        print(f"Asset: {b['asset']:<6} | Free: {free:<12} | Locked (in sell orders): {locked:<12}")
