import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')
conn = sqlite3.connect('scalper.db')
conn.row_factory = sqlite3.Row

print('=== BOT INSTANCES ===')
for i in conn.execute('SELECT * FROM bot_instances').fetchall():
    print(dict(i))

print('\n=== CYCLES (STATUS != CLOSED) ===')
for c in conn.execute("SELECT * FROM cycles WHERE status != 'CLOSED' ORDER BY id DESC LIMIT 10").fetchall():
    print(dict(c))

print('\n=== ALL RECENT CYCLES ===')
for c in conn.execute('SELECT * FROM cycles ORDER BY id DESC LIMIT 15').fetchall():
    print(dict(c))

print('\n=== RECENT TRADES ===')
for t in conn.execute('SELECT * FROM trades ORDER BY id DESC LIMIT 20').fetchall():
    print(dict(t))

print('\n=== RECENT LOG EVENTS ===')
for ev in conn.execute("SELECT * FROM log_events ORDER BY id DESC LIMIT 25").fetchall():
    print(dict(ev))

conn.close()
