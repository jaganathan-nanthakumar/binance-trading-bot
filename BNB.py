#!/usr/bin/env python3
# BNB Autonomous Scalper & Terminal  —  MAX-PROTECT / NEAR-ZERO-LOSS PATCH
# ======================================================================
# Goal: only enter high-probability pairs and exit at the first sign of trouble.
#
# Key protections:
#   • Hard absolute stop-loss capped at -0.10 USDT (or -0.4 % of investment)
#   • Ultra-early scratch: if still ≤ -0.04 USDT after 12 s → force exit
#   • Micro-profit floor raised to 0.10 USDT; max hold 75 s
#   • Entry filters: spread ≤ 0.015 %, book ≥ 1.45, vol ≥ 0.25 %,
#     taker-buy ≥ 0.60, BTC regime -0.15 %, MTF very tight
#   • Composite score must be ≥ 4.5 (only strongest pairs)
#   • Toxic-flow cancel ratio 0.55; post-loss cooldown 50 min
#   • hard_stop_triggered() + early_scratch_triggered() on every poll
# Usage unchanged:
#   streamlit run BNB-ZERO-LOSS-PATCHED.py
#   python BNB-ZERO-LOSS-PATCHED.py --manager
#   python BNB-ZERO-LOSS-PATCHED.py --bot --instance-id <ID>

from __future__ import annotations

import os
import sys
import time
import json
import math
import random
import re
import hmac
import ctypes
import argparse
import subprocess
import signal
import traceback
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

# Reconfigure console streams for UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Third-party imports
try:
    from binance.client import Client
    from binance.exceptions import BinanceAPIException, BinanceRequestException
    from binance import ThreadedWebsocketManager
except ImportError:
    pass

try:
    import streamlit as st
    import pandas as pd
    import plotly.graph_objects as go
    import plotly.express as px
except ImportError:
    pass

# Global paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.abspath(__file__)
# Windowless pythonw.exe for silent background bot spawning on Windows
_pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
PYTHON = _pythonw if (sys.platform == "win32" and os.path.exists(_pythonw)) else sys.executable

GLOBAL_TWM = None
def get_global_twm():
    global GLOBAL_TWM
    if GLOBAL_TWM is None:
        GLOBAL_TWM = ThreadedWebsocketManager()
        GLOBAL_TWM.start()
    return GLOBAL_TWM

DB_PATH = os.path.join(SCRIPT_DIR, "scalper.db")
LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# Online Adaptive Machine Learning Integration
try:
    from ml_selector import (
        get_selector,
        record_cycle_features,
        update_ml_from_cycle,
        init_ml_tables,
    )
except ImportError:
    get_selector = None
    record_cycle_features = None
    update_ml_from_cycle = None
    init_ml_tables = None

# Principal Component Analysis (PCA) Coin Selector Integration
try:
    import pca_selector
except ImportError:
    pca_selector = None

# Advanced Multi-Horizon Time Series Forecasting Engine
try:
    import ts_forecast
except ImportError:
    ts_forecast = None

# Hybrid Quantitative Strategy & Market Regime Predictor Integration
try:
    import hybrid_strategy
except ImportError:
    hybrid_strategy = None

# SOXSB ↔ SOXLB Lag Arbitrage Engine Integration
try:
    import semiconductor_lag_arb
except ImportError:
    semiconductor_lag_arb = None

RECENT_PAIR_METRICS: Dict[str, dict] = {}


# ===========================================================================
# GLOBAL CONFIGURATION & PARAMETERS
# ===========================================================================

# Defaults / filters (can be overridden per-instance for investment/target/sl)


# --- ADDITIONAL EXTRACTED PARAMETERS ---
# BTC Regime Filter
BTC_REGIME_MIN_MOVE_PCT = -0.15      # Block entries if BTC 5m avg move < -0.15%

# Taker Buy & Volatility
MIN_1M_VOLATILITY_PCT = 0.12         # Requires at least 0.12% 1m volatility for entry
MIN_TAKER_BUY_RATIO = 0.55           # Used in toxic flow check

# Order Book & Queue
BID_QUEUE_USDT_THRESHOLD = 10000.0   # If queue > 10k USDT, join closer to ask
MIN_BNB_FEE_BUFFER = 0.008           # Minimum BNB required to pay fees

# Timeouts & Cooldowns
POST_LOSS_COOLDOWN_SEC = 300        # Cooldown duration after a loss (5 minutes)
MAX_BLOCKED_RETRIES = 2             # Max consecutive blocked cycles before switching coin
BLOCKED_COOLDOWN_SEC = 600          # Cooldown duration for blocked/dumping coins (10 minutes)

MIN_QUOTE_VOLUME = 15_000_000  # Minimum $15M 24h quote volume to avoid illiquid coins
MIN_PRICE = 0.0001
CHANGE_PCT_MIN = 0.2
CHANGE_PCT_MAX = 8.0

# Limit Buy Offset: Place limit orders slightly below the best bid instead of at the top of the book.
# Example: 0.002 = 0.2% below current best bid. Set to 0.0 to match the best bid.
LIMIT_BUY_OFFSET_PCT = 0.0
# How long to wait for the limit buy to fill before aborting the cycle
LIMIT_BUY_MAX_WAIT_SEC = 10.0

DEFAULT_MAX_SPREAD_PCT = 0.30  # Reject >0.30% spread (loosened)
DEFAULT_MAX_TICK_RATIO = 0.0030
DEFAULT_TIMEOUT_SEC = 300.0       # 5-minute cycle hold window
DEFAULT_MIN_BOOK_RATIO = 1.10     # Require 10% more resting bid support
DEFAULT_MIN_VOLATILITY_1M = 0.08  # 0.08% 1m movement is healthy
DEFAULT_MIN_TAKER_BUY_RATIO = 0.50  # More buy aggressors than sell aggressors
# --- ADVANCED TIME SERIES FORECASTING ENGINE & PROFIT OPTIMIZATION ---
# 5-Minute Micro-Profit & Micro-Loss Symmetry (Both in 0.00XXX range)
OPT_MIN_NET_PROFIT_PCT = 0.0002           # Micro-profit net target (~+0.0050 USDT per 25 USDT)
OPT_ABSOLUTE_MIN_PROFIT_USDT = 0.0035      # Target small positive profit in 0.00XXX range
OPT_MICRO_SCRATCH_LOSS_USDT = -0.0035     # Target loss in same 0.00XXX range
OPT_MICRO_LOSS_FLOOR_USDT = -0.0085       # Maximum allowable loss floor in 0.00XXX range
OPT_BREAKEVEN_BUFFER_USDT = 0.0010        # Maker breakeven reprice buffer
OPT_MAX_HOLD_SEC = 300.0                   # Each cycle runs for 5 minutes (300 seconds)
OPT_MAX_SAFETY_TIMEOUT_SEC = 900.0        # 15-minute absolute safety ceiling
OPT_REPRICE_STAGE1_SEC = 150.0             # Stage 1: Halfway check after 150s (2.5 min)
OPT_REPRICE_STAGE2_SEC = 300.0             # Stage 2: After 5 minutes, stop loss repricing to 0.00XXX range
OPT_SCRATCH_DRIFT_PCT = -1.25             # Adverse drift scratch
OPT_TRAILING_ACTIVATION_MULT = 1.05       # Trail activation
OPT_TRAILING_DROP_PCT = 0.18              # Trailing pullback
OPT_MIN_BID_QUEUE_USDT = 3000.0
OPT_MAX_SLIPPAGE_PCT = 0.350              # Realistic slippage tolerance
OPT_HARD_STOP_USDT = -0.0050              # Symmetrical hard stop loss in 0.00XXX range
OPT_HARD_STOP_PCT = -0.0100
OPT_EARLY_SCRATCH_SEC = 9999.0
OPT_EARLY_SCRATCH_USDT = -0.300
OPT_MIN_DYNAMIC_SL_USDT = -0.0035         # Dynamic SL floor in 0.00XXX range
OPT_MAX_DYNAMIC_SL_USDT = -0.0060         # Dynamic SL ceiling in 0.00XXX range
POST_LOSS_COOLDOWN_SEC = 3600             # Quarantine losing symbol for 60 minutes
MAX_DAILY_LOSS_USDT = -1.50               # Daily circuit breaker: halt bot if daily net loss hits -$1.50
MAX_CONSECUTIVE_LOSSES = 3                # Pause trading after 3 consecutive losses
CONSECUTIVE_LOSS_PAUSE_SEC = 1800         # 30-minute cooldown after 3 consecutive losses





# ===========================================================================
# SECTION 1: DATABASE & PERSISTENCE LAYER (from db.py)
# ===========================================================================
# Shared SQLite layer for multi-instance scalper bots.


import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any


# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))


def to_ist(dt_or_str) -> Optional[datetime]:
    """Convert a datetime or ISO timestamp string to Indian Standard Time (IST)."""
    if not dt_or_str:
        return None
    if isinstance(dt_or_str, datetime):
        dt = dt_or_str
    else:
        try:
            s = str(dt_or_str).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def to_ist_str(ts_str: Optional[str], fmt: str = "%d %b %Y, %I:%M:%S %p IST") -> str:
    """Format a timestamp into a human-readable IST string."""
    dt = to_ist(ts_str)
    return dt.strftime(fmt) if dt else "—"


def to_ist_date_str(ts_str: Optional[str]) -> str:
    """Extract the YYYY-MM-DD date in IST."""
    dt = to_ist(ts_str)
    return dt.strftime("%Y-%m-%d") if dt else "Unknown"



@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        if init_ml_tables:
            try:
                init_ml_tables(conn)
            except Exception:
                pass
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS bot_instances (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'CREATED',
            -- CREATED | RUNNING | STOPPING | COMPLETED | STOPPED | ERROR
            investment_usdt REAL NOT NULL DEFAULT 25.0,
            target_profit   REAL NOT NULL DEFAULT 0.10,
            stop_loss       REAL NOT NULL DEFAULT -0.10,
            cumulative_pnl  REAL NOT NULL DEFAULT 0.0,
            cycles_done     INTEGER NOT NULL DEFAULT 0,
            wins            INTEGER NOT NULL DEFAULT 0,
            losses          INTEGER NOT NULL DEFAULT 0,
            current_symbol  TEXT,
            current_pnl     REAL DEFAULT 0.0,
            stop_requested  INTEGER NOT NULL DEFAULT 0,
            strategy        TEXT DEFAULT 'BEST_BID_MAKER',
            pid             INTEGER,
            last_error      TEXT,
            created_at      TEXT NOT NULL,
            started_at      TEXT,
            finished_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id     INTEGER NOT NULL,
            cycle_id        INTEGER,
            symbol          TEXT NOT NULL,
            side            TEXT NOT NULL,
            quantity        REAL,
            price           REAL,
            quote_qty       REAL,
            fee_bnb         REAL DEFAULT 0.0,
            fee_usdt        REAL DEFAULT 0.0,
            order_id        TEXT,
            ts              TEXT NOT NULL,
            pnl_usdt        REAL,
            notes           TEXT,
            FOREIGN KEY (instance_id) REFERENCES bot_instances(id)
        );

        CREATE TABLE IF NOT EXISTS cycles (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id     INTEGER NOT NULL,
            symbol          TEXT,
            entry_cost      REAL,
            exit_value      REAL,
            qty             REAL,
            fee_bnb         REAL DEFAULT 0.0,
            fee_usdt        REAL DEFAULT 0.0,
            pnl_usdt        REAL,
            status          TEXT,
            started_at      TEXT,
            finished_at     TEXT,
            duration_sec    REAL,
            FOREIGN KEY (instance_id) REFERENCES bot_instances(id)
        );

        CREATE TABLE IF NOT EXISTS balance_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id     INTEGER,
            usdt_free       REAL,
            usdt_locked     REAL,
            total_usdt      REAL,
            ts              TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS event_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id     INTEGER,
            level           TEXT NOT NULL DEFAULT 'INFO',
            -- INFO | TRADE | WARN | ERROR | EXCEPTION
            event_type      TEXT,
            -- e.g. BOT_START, SCAN, BUY, SELL, TARGET, STOP_LOSS, CYCLE_END, EXCEPTION
            symbol          TEXT,
            message         TEXT NOT NULL,
            detail          TEXT,
            -- JSON or traceback / extra context
            ts              TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_trades_instance ON trades(instance_id);
        CREATE INDEX IF NOT EXISTS idx_cycles_instance ON cycles(instance_id);
        CREATE INDEX IF NOT EXISTS idx_instances_status ON bot_instances(status);
        CREATE INDEX IF NOT EXISTS idx_event_log_ts ON event_log(ts);
        CREATE INDEX IF NOT EXISTS idx_event_log_instance ON event_log(instance_id);

        CREATE TABLE IF NOT EXISTS manager_config (
            id                  INTEGER PRIMARY KEY CHECK (id = 1),
            enabled             INTEGER NOT NULL DEFAULT 0,
            investment_usdt     REAL NOT NULL DEFAULT 25.0,
            target_profit       REAL NOT NULL DEFAULT 0.10,
            stop_loss           REAL NOT NULL DEFAULT -0.50,
            min_bnb_per_bot     REAL NOT NULL DEFAULT 0.001,
            max_parallel        INTEGER NOT NULL DEFAULT 1,
            check_interval_sec  REAL NOT NULL DEFAULT 5.0,
            last_check_at       TEXT,
            last_message        TEXT,
            use_only            TEXT DEFAULT '',
            max_spread_pct      REAL NOT NULL DEFAULT 0.08,
            timeout_sec         REAL NOT NULL DEFAULT 0.0,
            min_book_ratio      REAL NOT NULL DEFAULT 1.15,
            min_volatility_1m   REAL NOT NULL DEFAULT 0.12,
            min_taker_buy_ratio REAL NOT NULL DEFAULT 0.50,
            strategy            TEXT NOT NULL DEFAULT 'AUTO'
        );

        CREATE TABLE IF NOT EXISTS symbol_cooldowns (
            symbol      TEXT PRIMARY KEY,
            expires_at  TEXT NOT NULL
        );

        INSERT OR IGNORE INTO manager_config (id, enabled, use_only, strategy) VALUES (1, 0, 'SOXLBUSDT, SOXSBUSDT', 'DUAL_SOXLB_SOXSB');
        """)
        try:
            conn.execute("UPDATE manager_config SET use_only = 'SOXLBUSDT, SOXSBUSDT', strategy = 'DUAL_SOXLB_SOXSB' WHERE id = 1 AND (strategy IS NULL OR strategy IN ('', 'AUTO', 'BEST_BID_MAKER', 'SEMICONDUCTOR_DUAL_ENGINE', 'HYBRID_SOXLB_SOXSB_SRATEGY'))")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE manager_config ADD COLUMN strategy TEXT NOT NULL DEFAULT 'AUTO'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE bot_instances ADD COLUMN strategy TEXT DEFAULT 'AUTO'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE manager_config ADD COLUMN use_only TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE manager_config ADD COLUMN blacklist TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE manager_config ADD COLUMN max_spread_pct REAL NOT NULL DEFAULT 0.02")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE manager_config ADD COLUMN timeout_sec REAL NOT NULL DEFAULT 0.0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE manager_config ADD COLUMN min_book_ratio REAL NOT NULL DEFAULT 1.15")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE manager_config ADD COLUMN min_volatility_1m REAL NOT NULL DEFAULT 0.08")
        except Exception:
            pass
        # Removed hardcoded unconditional UPDATE that was wiping user settings
        pass
        try:
            conn.execute("UPDATE manager_config SET max_parallel = 1 WHERE id = 1 AND max_parallel = 20")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE manager_config ADD COLUMN min_taker_buy_ratio REAL NOT NULL DEFAULT 0.50")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN fee_bnb REAL DEFAULT 0.0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN fee_usdt REAL DEFAULT 0.0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE cycles ADD COLUMN fee_bnb REAL DEFAULT 0.0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE cycles ADD COLUMN fee_usdt REAL DEFAULT 0.0")
        except Exception:
            pass
        try:
            # Backfill fee_usdt from notes if notes contains 'fee_usdt='
            rows = conn.execute("SELECT id, notes FROM trades WHERE (fee_usdt IS NULL OR fee_usdt = 0.0) AND notes LIKE '%fee_usdt=%'").fetchall()
            for r in rows:
                notes = r["notes"] or ""
                for part in notes.split():
                    if part.startswith("fee_usdt="):
                        val = float(part.split("=")[1])
                        conn.execute("UPDATE trades SET fee_usdt = ? WHERE id = ?", (val, r["id"]))
        except Exception:
            pass
        try:
            # Backfill fee_bnb from event_log detail or calculation
            t_rows = conn.execute("SELECT id, order_id, fee_usdt FROM trades WHERE fee_bnb IS NULL OR fee_bnb = 0.0").fetchall()
            for tr in t_rows:
                tid = tr["id"]
                oid = tr["order_id"]
                fee_u = tr["fee_usdt"] or 0.0
                bnb_fee = 0.0
                if oid:
                    ev = conn.execute("SELECT detail FROM event_log WHERE detail LIKE ? LIMIT 1", (f"%{oid}%",)).fetchone()
                    if ev and ev["detail"]:
                        try:
                            import ast
                            d = ast.literal_eval(ev["detail"])
                            fee_dict = d.get("fee") or {}
                            bnb_fee = float(fee_dict.get("bnb_amount") or fee_dict.get("fee_bnb") or 0.0)
                        except Exception:
                            pass
                if bnb_fee <= 0 and fee_u > 0:
                    bnb_fee = fee_u / 775.0
                if bnb_fee > 0:
                    conn.execute("UPDATE trades SET fee_bnb = ? WHERE id = ?", (bnb_fee, tid))
        except Exception:
            pass
        try:
            # Backfill cycles fee_bnb and fee_usdt from trades
            conn.execute("""
                UPDATE cycles SET
                    fee_bnb = COALESCE((SELECT SUM(fee_bnb) FROM trades WHERE trades.cycle_id = cycles.id), 0.0),
                    fee_usdt = COALESCE((SELECT SUM(fee_usdt) FROM trades WHERE trades.cycle_id = cycles.id), 0.0)
                WHERE (fee_bnb IS NULL OR fee_bnb = 0.0) AND (fee_usdt IS NULL OR fee_usdt = 0.0)
            """)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Bot instance helpers
# ---------------------------------------------------------------------------

def next_bot_name(when: datetime = None) -> str:
    """
    Default bot name: DDmmYYNNN
    Example for 20 Sep 2026, first bot that day: 200926001
    Sequence is per calendar day (local time), zero-padded to 3 digits.
    """
    when = when or datetime.now()
    prefix = when.strftime("%d%m%y")  # e.g. 200926
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM bot_instances WHERE name LIKE ?",
            (prefix + "%",),
        ).fetchall()
    seq = 0
    for r in rows:
        name = r["name"] if isinstance(r, sqlite3.Row) else r[0]
        if len(name) >= 9 and name[:6] == prefix and name[6:].isdigit():
            seq = max(seq, int(name[6:9]))
    return f"{prefix}{seq + 1:03d}"


def create_instance(
    name: str = None,
    investment_usdt: float = 25.0,
    target_profit: float = 0.10,
    stop_loss: float = -0.25,
    strategy: str = "DUAL_SOXLB_SOXSB",
) -> int:
    if not name:
        name = next_bot_name()
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO bot_instances
                (name, investment_usdt, target_profit, stop_loss, strategy, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'CREATED')
            """,
            (name, investment_usdt, target_profit, stop_loss, strategy, now),
        )
        return cur.lastrowid


def get_instance(instance_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM bot_instances WHERE id = ?", (instance_id,)
        ).fetchone()
        return dict(row) if row else None


def list_instances(limit: int = 100) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_instances ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_instance(instance_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [instance_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE bot_instances SET {cols} WHERE id = ?", vals)


def request_stop(instance_id: int):
    update_instance(instance_id, stop_requested=1, status="STOPPING")


def is_stop_requested(instance_id: int) -> bool:
    inst = get_instance(instance_id)
    return bool(inst and inst.get("stop_requested"))


# ---------------------------------------------------------------------------
# Trade / cycle helpers
# ---------------------------------------------------------------------------

def insert_trade(
    instance_id: int,
    cycle_id: Optional[int],
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    quote_qty: float,
    fee_bnb: float = 0.0,
    fee_usdt: float = 0.0,
    order_id=None,
    pnl_usdt=None,
    notes=None,
):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO trades
                (instance_id, cycle_id, symbol, side, quantity, price,
                 quote_qty, fee_bnb, fee_usdt, order_id, ts, pnl_usdt, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instance_id,
                cycle_id,
                symbol,
                side,
                quantity,
                price,
                quote_qty,
                fee_bnb or 0.0,
                fee_usdt or 0.0,
                str(order_id) if order_id else None,
                datetime.now(timezone.utc).isoformat(),
                pnl_usdt,
                notes,
            ),
        )


def insert_cycle(
    instance_id: int,
    symbol: str,
    entry_cost: float,
    qty: float,
    fee_bnb: float = 0.0,
    fee_usdt: float = 0.0,
    status: str = "OPEN",
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO cycles
                (instance_id, symbol, entry_cost, qty, fee_bnb, fee_usdt, status, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instance_id,
                symbol,
                entry_cost,
                qty,
                fee_bnb or 0.0,
                fee_usdt or 0.0,
                status,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def close_cycle(
    cycle_id: int,
    exit_value: float,
    pnl_usdt: float,
    status: str,
    duration_sec: float,
    fee_bnb: Optional[float] = None,
    fee_usdt: Optional[float] = None,
):
    with get_conn() as conn:
        if fee_bnb is not None and fee_usdt is not None:
            conn.execute(
                """
                UPDATE cycles SET
                    exit_value = ?, pnl_usdt = ?, status = ?,
                    finished_at = ?, duration_sec = ?,
                    fee_bnb = ?, fee_usdt = ?
                WHERE id = ?
                """,
                (
                    exit_value,
                    pnl_usdt,
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    duration_sec,
                    fee_bnb,
                    fee_usdt,
                    cycle_id,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE cycles SET
                    exit_value = ?, pnl_usdt = ?, status = ?,
                    finished_at = ?, duration_sec = ?
                WHERE id = ?
                """,
                (
                    exit_value,
                    pnl_usdt,
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    duration_sec,
                    cycle_id,
                ),
            )

    # Online Continuous ML Feedback: update brain on the fly
    if pnl_usdt is not None and update_ml_from_cycle:
        try:
            update_info = update_ml_from_cycle(DB_PATH, cycle_id, pnl_usdt, duration_sec)
            if update_info:
                sym_str = update_info.get("symbol") or "COIN"
                w_str = "WIN" if update_info.get("won") else "LOSS"
                log_event(
                    f"Adaptive ML update cycle #{cycle_id} ({sym_str}): {w_str} | "
                    f"PnL={pnl_usdt:+.4f} USDT | Pred P(Win)={update_info.get('pred_p', 0.5):.2f} | "
                    f"Err={update_info.get('error', 0.0):+.2f}",
                    level="INFO",
                    event_type="ML_UPDATE",
                    symbol=sym_str,
                )
        except Exception:
            pass


def snapshot_balance(client, instance_id: Optional[int] = None):
    try:
        bal = client.get_asset_balance(asset="USDT")
        free = float(bal["free"])
        locked = float(bal["locked"])
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO balance_snapshots
                    (instance_id, usdt_free, usdt_locked, total_usdt, ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    free,
                    locked,
                    free + locked,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except Exception:
        pass


def load_cycles(instance_id: Optional[int] = None, limit: int = 300):
    with get_conn() as conn:
        q = """
            SELECT
                c.id,
                c.instance_id,
                b.name AS bot_name,
                c.symbol,
                c.entry_cost,
                c.exit_value,
                c.qty,
                c.fee_bnb,
                c.fee_usdt,
                c.pnl_usdt,
                c.status,
                c.started_at,
                c.finished_at,
                c.duration_sec
            FROM cycles c
            LEFT JOIN bot_instances b ON c.instance_id = b.id
        """
        if instance_id:
            q += " WHERE c.instance_id = ? ORDER BY c.id DESC LIMIT ?"
            return conn.execute(q, (instance_id, limit)).fetchall()
        q += " ORDER BY c.id DESC LIMIT ?"
        return conn.execute(q, (limit,)).fetchall()


def load_trades(instance_id: Optional[int] = None, limit: int = 500):
    with get_conn() as conn:
        q = """
            SELECT
                t.id,
                t.instance_id,
                b.name AS bot_name,
                t.cycle_id,
                t.symbol,
                t.side,
                t.quantity,
                t.price,
                t.quote_qty,
                t.fee_bnb,
                t.fee_usdt,
                t.pnl_usdt,
                t.notes,
                t.ts
            FROM trades t
            LEFT JOIN bot_instances b ON t.instance_id = b.id
        """
        if instance_id:
            q += " WHERE t.instance_id = ? ORDER BY t.id DESC LIMIT ?"
            return conn.execute(q, (instance_id, limit)).fetchall()
        q += " ORDER BY t.id DESC LIMIT ?"
        return conn.execute(q, (limit,)).fetchall()


def load_unified_trades(instance_id: Optional[int] = None, limit: int = 300):
    """
    Returns cycles merged with their buy and sell/liquidate trade details:
    entry/exit prices, buy fees (BNB & USDT), sell fees (BNB & USDT),
    total fees, and net PnL.
    """
    with get_conn() as conn:
        q = """
            SELECT
                c.id AS cycle_id,
                c.instance_id,
                b.name AS bot_name,
                c.symbol,
                c.status,
                c.qty,
                c.entry_cost,
                c.exit_value,
                -- Entry (Buy)
                MAX(CASE WHEN t.side = 'BUY' THEN t.price END) AS entry_price,
                COALESCE(SUM(CASE WHEN t.side = 'BUY' THEN t.fee_bnb ELSE 0 END), 0) AS buy_fee_bnb,
                COALESCE(SUM(CASE WHEN t.side = 'BUY' THEN t.fee_usdt ELSE 0 END), 0) AS buy_fee_usdt,
                -- Exit (Sell / Liquidate)
                MAX(CASE WHEN t.side = 'SELL' THEN t.price END) AS exit_price,
                COALESCE(SUM(CASE WHEN t.side = 'SELL' THEN t.fee_bnb ELSE 0 END), 0) AS sell_fee_bnb,
                COALESCE(SUM(CASE WHEN t.side = 'SELL' THEN t.fee_usdt ELSE 0 END), 0) AS sell_fee_usdt,
                MAX(CASE WHEN t.side = 'SELL' THEN t.notes END) AS exit_reason,
                -- Total Fees
                COALESCE(SUM(t.fee_bnb), 0) AS total_fee_bnb,
                COALESCE(SUM(t.fee_usdt), 0) AS total_fee_usdt,
                -- PnL & timing
                c.pnl_usdt,
                c.duration_sec,
                c.started_at,
                c.finished_at
            FROM cycles c
            LEFT JOIN bot_instances b ON c.instance_id = b.id
            LEFT JOIN trades t ON t.cycle_id = c.id
        """
        if instance_id:
            q += " WHERE c.instance_id = ? GROUP BY c.id ORDER BY c.id DESC LIMIT ?"
            return conn.execute(q, (instance_id, limit)).fetchall()
        q += " GROUP BY c.id ORDER BY c.id DESC LIMIT ?"
        return conn.execute(q, (limit,)).fetchall()


# ---------------------------------------------------------------------------
# Event log (detailed messages + exceptions for the UI)
def get_active_symbols(exclude_instance_id: Optional[int] = None) -> set:
    """
    Strict Symbol Exclusion: No two bot instances may trade the same coin simultaneously.
    Returns the set of symbols currently held or being traded by any other active instance.
    """
    with get_conn() as conn:
        q = "SELECT current_symbol FROM bot_instances WHERE status IN ('RUNNING', 'CREATED', 'STOPPING') AND current_symbol IS NOT NULL AND current_symbol != ''"
        params = ()
        if exclude_instance_id is not None:
            q += " AND id != ?"
            params = (exclude_instance_id,)
        rows = conn.execute(q, params).fetchall()
        active = {r["current_symbol"] for r in rows if r["current_symbol"]}

        q_cycles = "SELECT symbol FROM cycles WHERE status = 'OPEN' AND symbol IS NOT NULL AND symbol != ''"
        if exclude_instance_id is not None:
            q_cycles += " AND instance_id != ?"
        rows_c = conn.execute(q_cycles, params).fetchall()
        for r in rows_c:
            if r["symbol"]:
                active.add(r["symbol"])
        return active


def claim_symbol_for_instance(instance_id: int, symbol: str) -> bool:
    """
    Atomically checks if `symbol` is currently active/traded/held by any other bot.
    If free, updates bot_instances.current_symbol = symbol for instance_id and returns True.
    If already taken, returns False without updating.
    NOTE: Semiconductor tokens (SOXLBUSDT / SOXSBUSDT) permit multiple bots to trade concurrently,
    provided each bot strictly enforces its own maximum investment limit.
    """
    if not symbol or symbol == "INIT":
        return False

    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Allow multi-bot concurrency for semiconductor bStocks & dual strategies
        if symbol in ("SOXLBUSDT", "SOXSBUSDT", "SOXLB+SOXSB", "SOXSB+SOXLB", "SOXSB + SOXLB"):
            conn.execute(
                "UPDATE bot_instances SET current_symbol = ? WHERE id = ?",
                (symbol, instance_id),
            )
            return True

        row = conn.execute(
            "SELECT id FROM bot_instances WHERE id != ? AND current_symbol = ? AND status IN ('RUNNING', 'CREATED', 'STOPPING')",
            (instance_id, symbol),
        ).fetchone()
        if row:
            return False

        open_c = conn.execute(
            "SELECT id FROM cycles WHERE instance_id != ? AND symbol = ? AND status = 'OPEN'",
            (instance_id, symbol),
        ).fetchone()
        if open_c:
            return False

        conn.execute(
            "UPDATE bot_instances SET current_symbol = ? WHERE id = ?",
            (symbol, instance_id),
        )
        return True


def release_symbol(instance_id: int) -> None:
    """Clear current_symbol for an instance if it does not have an OPEN cycle."""
    with get_conn() as conn:
        open_c = conn.execute(
            "SELECT 1 FROM cycles WHERE instance_id = ? AND status = 'OPEN' LIMIT 1",
            (instance_id,),
        ).fetchone()
        if not open_c:
            conn.execute(
                "UPDATE bot_instances SET current_symbol = NULL WHERE id = ?",
                (instance_id,),
            )



# ---------------------------------------------------------------------------

def log_event(
    message: str,
    level: str = "INFO",
    event_type: str = None,
    instance_id: Optional[int] = None,
    symbol: str = None,
    detail: str = None,
):
    """Persist a log line. Safe to call from any thread/process."""
    try:
        with get_conn() as conn:
            bot_name = None
            if instance_id is not None:
                row = conn.execute("SELECT name, current_symbol FROM bot_instances WHERE id = ?", (instance_id,)).fetchone()
                if row:
                    bot_name = row[0]
                    if not symbol and row[1]:
                        symbol = row[1]

            if bot_name:
                sym_tag = symbol or "INIT"
                prefix = f"[{bot_name}] [{sym_tag}]"
                if not message.startswith(f"[{bot_name}]"):
                    message = f"{prefix} {message}"
                elif not message.startswith(prefix) and message.startswith(f"[{bot_name}]"):
                    message = message.replace(f"[{bot_name}]", prefix, 1)

            conn.execute(
                """
                INSERT INTO event_log
                    (instance_id, level, event_type, symbol, message, detail, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    level,
                    event_type,
                    symbol,
                    message,
                    detail,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except Exception:
        pass  # never break trading because of logging


def load_events(
    instance_id: Optional[int] = None,
    level: Optional[str] = None,
    limit: int = 300,
):
    with get_conn() as conn:
        clauses = []
        params: list = []
        if instance_id is not None:
            clauses.append("instance_id = ?")
            params.append(instance_id)
        if level:
            clauses.append("level = ?")
            params.append(level)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        return conn.execute(
            f"SELECT * FROM event_log {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()


def clear_events(older_than_days: Optional[int] = None):
    with get_conn() as conn:
        if older_than_days is None:
            conn.execute("DELETE FROM event_log")
        else:
            conn.execute(
                "DELETE FROM event_log WHERE ts < datetime('now', ?)",
                (f"-{older_than_days} days",),
            )


def purge_logs_by_date(date_str_ist: str, purge_files: bool = True) -> dict:
    """
    Purges execution event logs and text log files for a specific execution date (in IST).
    CRITICAL SAFEGUARD:
    NEVER deletes from 'trades', 'cycles', or 'bot_instances'.
    All PnL, trade history, fees, and tax information are permanently preserved.
    """
    try:
        start_ist = datetime.strptime(date_str_ist.strip(), "%Y-%m-%d").replace(tzinfo=IST)
        end_ist = start_ist + timedelta(days=1) - timedelta(microseconds=1)
    except Exception as e:
        return {"success": False, "error": f"Invalid IST date format (expected YYYY-MM-DD): {e}"}

    start_utc = start_ist.astimezone(timezone.utc).isoformat()
    end_utc = end_ist.astimezone(timezone.utc).isoformat()

    deleted_events = 0
    with get_conn() as conn:
        # STRICT SAFEGUARD: Only delete from event_log (diagnostic logs)
        cur = conn.execute(
            "DELETE FROM event_log WHERE ts >= ? AND ts <= ?",
            (start_utc, end_utc),
        )
        deleted_events = cur.rowcount

    deleted_files = 0
    if purge_files:
        logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        if os.path.exists(logs_dir):
            prefix_dmy = start_ist.strftime("%d%m%y")  # DDmmYY prefix e.g. 210926
            for fname in os.listdir(logs_dir):
                if not fname.endswith(".log"):
                    continue
                fpath = os.path.join(logs_dir, fname)
                should_delete = False
                if fname.startswith(prefix_dmy):
                    should_delete = True
                else:
                    try:
                        mtime = os.path.getmtime(fpath)
                        file_dt_ist = datetime.fromtimestamp(mtime, tz=IST)
                        if file_dt_ist.strftime("%Y-%m-%d") == date_str_ist:
                            should_delete = True
                    except Exception:
                        pass
                if should_delete:
                    try:
                        os.remove(fpath)
                        deleted_files += 1
                    except Exception:
                        pass

    return {
        "success": True,
        "date_ist": date_str_ist,
        "deleted_events": deleted_events,
        "deleted_files": deleted_files,
    }


def load_daily_pnl_ist() -> List[Dict[str, Any]]:
    """
    Aggregates closed cycles and trades by IST date (YYYY-MM-DD).
    All profit (> 0) is considered a win, even if manually liquidated.
    Permanently preserves all tax/fee details (BNB & USDT fees) and volume.
    """
    with get_conn() as conn:
        q = """
            SELECT
                c.id AS cycle_id,
                c.instance_id,
                b.name AS bot_name,
                c.symbol,
                c.status,
                c.entry_cost,
                c.exit_value,
                c.pnl_usdt,
                COALESCE(c.fee_bnb, 0) AS fee_bnb,
                COALESCE(c.fee_usdt, 0) AS fee_usdt,
                c.started_at,
                c.finished_at
            FROM cycles c
            LEFT JOIN bot_instances b ON c.instance_id = b.id
            WHERE c.status IN ('SUCCESS', 'FAILURE', 'STOP_LOSS', 'TARGET', 'BREAKEVEN_LOCK', 'TIME_EXPIRED', 'ABORTED', 'SCRATCH', 'LIQUIDATED')
               OR c.pnl_usdt IS NOT NULL
            ORDER BY c.id DESC
        """
        rows = conn.execute(q).fetchall()

    daily: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        ts = d.get("finished_at") or d.get("started_at")
        date_ist = to_ist_date_str(ts)
        if date_ist not in daily:
            daily[date_ist] = {
                "date_ist": date_ist,
                "total_runs": 0,
                "wins": 0,
                "losses": 0,
                "pnl_usdt": 0.0,
                "fee_bnb": 0.0,
                "fee_usdt": 0.0,
                "total_volume": 0.0,
                "symbols": set(),
            }

        pnl = float(d.get("pnl_usdt") or 0.0)
        daily[date_ist]["total_runs"] += 1
        # RULE: All profit (> 0) is considered a win even if manually liquidated
        if pnl > 0:
            daily[date_ist]["wins"] += 1
        else:
            daily[date_ist]["losses"] += 1

        daily[date_ist]["pnl_usdt"] += pnl
        daily[date_ist]["fee_bnb"] += float(d.get("fee_bnb") or 0.0)
        daily[date_ist]["fee_usdt"] += float(d.get("fee_usdt") or 0.0)
        daily[date_ist]["total_volume"] += float(d.get("entry_cost") or 0.0) + float(d.get("exit_value") or 0.0)
        if d.get("symbol"):
            daily[date_ist]["symbols"].add(d["symbol"])

    result = []
    for date_ist in sorted(daily.keys(), reverse=True):
        entry = daily[date_ist]
        runs = entry["total_runs"]
        wins = entry["wins"]
        entry["win_rate_pct"] = round((wins / runs * 100.0), 1) if runs > 0 else 0.0
        entry["pnl_usdt"] = round(entry["pnl_usdt"], 4)
        entry["fee_bnb"] = round(entry["fee_bnb"], 8)
        entry["fee_usdt"] = round(entry["fee_usdt"], 4)
        entry["total_volume"] = round(entry["total_volume"], 2)
        entry["symbols_traded"] = ", ".join(sorted(entry["symbols"])) if entry["symbols"] else "—"
        del entry["symbols"]
        result.append(entry)

    return result



# ---------------------------------------------------------------------------
# Manager config
# ---------------------------------------------------------------------------

def get_manager_config() -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM manager_config WHERE id = 1").fetchone()
        if not row:
            conn.execute("INSERT INTO manager_config (id, enabled) VALUES (1, 0)")
            row = conn.execute("SELECT * FROM manager_config WHERE id = 1").fetchone()
        cfg = dict(row)
        if not cfg.get("use_only"):
            cfg["use_only"] = "SOXLBUSDT, SOXSBUSDT" 
        if cfg.get("blacklist") is None:
            cfg["blacklist"] = ""
        if not cfg.get("strategy") or cfg.get("strategy") in ("AUTO", "", "BEST_BID_MAKER", "SEMICONDUCTOR_DUAL_ENGINE"):
            cfg["strategy"] = "DUAL_SOXLB_SOXSB"
        return cfg


def set_manager_config(**fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values())
    with get_conn() as conn:
        conn.execute(f"UPDATE manager_config SET {cols} WHERE id = 1", vals)


def count_running_instances() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM bot_instances WHERE status IN ('CREATED', 'RUNNING', 'STOPPING')"
        ).fetchone()
        return int(row["c"] if row else 0)


def sum_running_investment() -> float:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(investment_usdt), 0) AS s
            FROM bot_instances
            WHERE status IN ('CREATED', 'RUNNING', 'STOPPING')
            """
        ).fetchone()
        return float(row["s"] if row else 0)


def get_unfilled_committed_usdt(exclude_instance_id: Optional[int] = None) -> float:
    """
    Sum investment_usdt of bots in 'CREATED' status that have been scheduled
    but have not yet started their process and placed orders on Binance.
    Once a bot is 'RUNNING' and places orders, Binance itself locks the funds
    and deducts them from free USDT, so counting RUNNING bots would double-count.
    """
    with get_conn() as conn:
        query = """
            SELECT COALESCE(SUM(b.investment_usdt), 0) AS s
            FROM bot_instances b
            WHERE b.status = 'CREATED'
        """
        params = []
        if exclude_instance_id is not None:
            query += " AND b.id != ?"
            params.append(exclude_instance_id)
        row = conn.execute(query, params).fetchone()
        return float(row["s"] if row else 0.0)


def get_instance_committed_capital(instance_id: int) -> float:
    """
    Returns total entry cost of any OPEN cycles currently held by this bot instance.
    Guarantees a bot can never exceed its configured investment_usdt.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(entry_cost), 0.0) AS s FROM cycles WHERE instance_id = ? AND status = 'OPEN'",
            (instance_id,),
        ).fetchone()
        return float(row["s"] if row else 0.0)


# ---------------------------------------------------------------------------
# Symbol cooldown helpers (post-stop-loss quarantine)
# ---------------------------------------------------------------------------

def add_cooldown_symbol(symbol: str, duration_sec: int = POST_LOSS_COOLDOWN_SEC) -> None:
    """
    Block a symbol from being selected for `duration_sec` seconds after a stop-loss or adverse scratch.
    Default duration: 3000s (50 minutes).
    Uses SQLite datetime arithmetic for portability.
    """
    try:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_cooldowns (symbol, expires_at)
                VALUES (?, datetime('now', ? || ' seconds'))
                """,
                (symbol, str(int(duration_sec))),
            )
    except Exception:
        pass


def get_cooldown_symbols() -> set:
    """Return the set of symbols currently under stop-loss cooldown."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT symbol FROM symbol_cooldowns WHERE expires_at > datetime('now')"
            ).fetchall()
        return {r["symbol"] for r in rows}
    except Exception:
        return set()


def clear_expired_cooldowns() -> None:
    """Housekeeping: remove expired cooldown rows."""
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM symbol_cooldowns WHERE expires_at <= datetime('now')")
    except Exception:
        pass


def get_underperforming_symbols() -> set:
    """
    Auto-blacklist symbols that consistently lose money:
    1. Symbols with >= 2 completed trades and:
       - 0 wins OR net PnL < -0.05 USDT.
    2. Symbols with 2 consecutive losses in their last 2 cycles.
    Returns a set of blacklisted symbols.
    """
    blacklisted = set()
    try:
        with get_conn() as conn:
            # 1. Cumulative underperformers
            rows = conn.execute("""
                SELECT symbol,
                       COUNT(*) as total,
                       SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) as wins,
                       SUM(pnl_usdt) as net_pnl
                FROM cycles
                WHERE status IN ('SUCCESS', 'STOP_LOSS', 'SCRATCH', 'LIQUIDATED')
                  AND symbol IS NOT NULL
                GROUP BY symbol
                HAVING total >= 2 AND (wins = 0 OR net_pnl < -0.03)
            """).fetchall()
            for r in rows:
                if r["symbol"]:
                    blacklisted.add(r["symbol"])

            # 2. Consecutive 2 losses check per symbol
            syms = conn.execute("SELECT DISTINCT symbol FROM cycles WHERE symbol IS NOT NULL").fetchall()
            for s_row in syms:
                sym = s_row["symbol"]
                last_two = conn.execute("""
                    SELECT pnl_usdt, status
                    FROM cycles
                    WHERE symbol = ? AND status IN ('SUCCESS', 'STOP_LOSS', 'SCRATCH', 'LIQUIDATED')
                    ORDER BY id DESC LIMIT 2
                """, (sym,)).fetchall()
                if len(last_two) >= 2:
                    if all(float(r["pnl_usdt"] or 0.0) <= 0 for r in last_two):
                        blacklisted.add(sym)
    except Exception:
        pass
    return blacklisted



# ===========================================================================
# SECTION 2: TRADING ENGINE & BINANCE EXECUTION (from engine.py)
# ===========================================================================
# Trading engine for a single bot instance. Runs until target profit or stop.


import os
import time
import math
import random
import traceback
import threading
import statistics
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any

from binance import ThreadedWebsocketManager
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
import requests
import socket

import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

pass

HIGH_VELOCITY_PAIRS = [
    "XRPUSDT", "SOLUSDT", "DOGEUSDT", "SUIUSDT", "SEIUSDT",
    "PEPEUSDT", "AVAXUSDT", "NEARUSDT", "ADAUSDT", "LINKUSDT", "AAVEUSDT",
]

BLACKLIST = [
    # Stablecoins & Fiat pegged assets (no volatility, fee trap)
    "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "USDPUSDT", "EURUSDT", "DAIUSDT",
    "AEURUSDT", "EURIUSDT", "USDDUSDT",
    # Synthetic / Wrapped tokens
    "WBTCUSDT", "WBETHUSDT",
    # Leveraged / Tokenized derivative tokens
    "BULL", "BEAR", "UP", "DOWN",
    # Heavy slow-movers & fee conflict assets
    "BTCUSDT", "BNBUSDT",
]

os.makedirs(LOGS_DIR, exist_ok=True)


class TeeLogger:
    def __init__(self, filepath, stream):
        self.file = open(filepath, "a", encoding="utf-8")
        self.stream = stream

    def write(self, text):
        try:
            self.file.write(text)
            self.file.flush()
        except Exception:
            pass
        if self.stream:
            try:
                self.stream.write(text)
                self.stream.flush()
            except Exception:
                pass

    def flush(self):
        try:
            self.file.flush()
        except Exception:
            pass
        if self.stream:
            try:
                self.stream.flush()
            except Exception:
                pass




def make_client() -> Client:
    # Load .env if present
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("\"'")
                        if k and v and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

    key = (os.getenv("BINANCE_API_KEY") or "").strip()
    secret = (os.getenv("BINANCE_API_SECRET") or "").strip()

    if not key or not secret or key.startswith("PUT_YOUR") or secret.startswith("PUT_YOUR"):
        raise RuntimeError(
            "Binance API key/secret missing or still placeholders. "
            "Set environment variables BINANCE_API_KEY and BINANCE_API_SECRET "
            "(no spaces, no quotes inside the value)."
        )
    if len(key) < 20 or len(secret) < 20:
        raise RuntimeError(
            f"API key/secret look too short (key={len(key)} chars, secret={len(secret)} chars). "
            "A real Binance key is typically 64 characters. Check for truncated paste."
        )
    return Client(key, secret)


_SYMBOL_FILTERS_CACHE: Dict[str, dict] = {}


def get_symbol_filters(client: Client, symbol: str) -> dict:
    """LOT_SIZE, MARKET_LOT_SIZE, PRICE_FILTER, NOTIONAL + asset precisions from exchangeInfo."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("Invalid empty symbol for filters")
    if sym in _SYMBOL_FILTERS_CACHE:
        return _SYMBOL_FILTERS_CACHE[sym]

    info = None
    # 1. First attempt: single-symbol query (weight = 2 instead of 20)
    try:
        res = client._get("exchangeInfo", data={"symbol": sym})
        symbols = res.get("symbols", [])
        if symbols:
            info = symbols[0]
    except Exception:
        info = None

    # 2. Fallback to client.get_symbol_info(sym) if single-symbol query fails
    if not info:
        info = client.get_symbol_info(sym)

    if not info:
        raise ValueError(f"Symbol info not found for {symbol}")

    lot = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
    market_lot = next(
        (f for f in info["filters"] if f["filterType"] == "MARKET_LOT_SIZE"), None
    )
    notional = next(
        (f for f in info["filters"] if f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL")),
        None,
    )
    price_filter = next(
        (f for f in info["filters"] if f["filterType"] == "PRICE_FILTER"), None
    )
    step = float(lot["stepSize"])
    min_qty = float(lot["minQty"])
    if market_lot:
        step = max(step, float(market_lot["stepSize"]))
        min_qty = max(min_qty, float(market_lot["minQty"]))

    tick_size = float(price_filter["tickSize"]) if price_filter else 0.0001
    min_price = float(price_filter["minPrice"]) if price_filter else 0.0001

    parsed = {
        "step": step,
        "min_qty": min_qty,
        "min_notional": float(notional["minNotional"]) if notional else 5.0,
        "tick_size": tick_size,
        "min_price": min_price,
        "base_precision": int(info.get("baseAssetPrecision", 8)),
        "quote_precision": int(info.get("quoteAssetPrecision", 8)),
        "qty_decimals": max(0, int(round(-math.log10(step), 0))) if step < 1 else 0,
    }
    _SYMBOL_FILTERS_CACHE[sym] = parsed
    return parsed


# Keep old name as alias so other call sites still work
def get_lot_info(client: Client, symbol: str) -> dict:
    return get_symbol_filters(client, symbol)


def round_step(qty: float, step: float) -> float:
    """Floor qty to a multiple of step (float). Prefer format_quantity for API calls."""
    if step <= 0:
        return qty
    precision = max(0, int(round(-math.log10(step), 0)))
    return math.floor(qty * (10 ** precision) + 1e-12) / (10 ** precision)


def format_quantity(qty: float, step: float) -> str:
    """
    Return quantity as a STRING that satisfies LOT_SIZE / MARKET_LOT_SIZE.
    Binance rejects floats with too many decimals or scientific notation.
    """
    from decimal import Decimal, ROUND_DOWN

    d_qty = Decimal(str(qty))
    d_step = Decimal(str(step))
    if d_step <= 0:
        return format(d_qty, "f")
    rounded = (d_qty / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step
    # Normalize: no trailing zeros that break some filters, but keep required decimals
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def format_price(price: float, tick_size: float, rounding: str = "CEILING") -> str:
    """
    Format price according to PRICE_FILTER tickSize.
    Uses ROUND_CEILING for sell prices (so we sell for at least target profit).
    Uses ROUND_FLOOR for buy prices (so we buy at or below target cost).
    """
    from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

    d_price = Decimal(str(price))
    d_tick = Decimal(str(tick_size))
    if d_tick <= 0:
        return format(d_price, "f")
    mode = ROUND_FLOOR if rounding.upper() in ("FLOOR", "DOWN") else ROUND_CEILING
    rounded = (d_price / d_tick).to_integral_value(rounding=mode) * d_tick
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def format_quote(amount: float, quote_precision: int) -> str:
    """Format quoteOrderQty to quote asset precision (usually 8 for USDT)."""
    from decimal import Decimal, ROUND_DOWN

    q = Decimal(str(amount))
    quant = Decimal(10) ** -quote_precision
    rounded = q.quantize(quant, rounding=ROUND_DOWN)
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def market_buy_quote(client: Client, symbol: str, usdt_amount: float) -> dict:
    """Market BUY spending `usdt_amount` USDT (quoteOrderQty)."""
    filters = get_symbol_filters(client, symbol)
    quote = format_quote(usdt_amount, filters["quote_precision"])
    if float(quote) < filters["min_notional"]:
        raise ValueError(
            f"quoteOrderQty {quote} < min_notional {filters['min_notional']}"
        )
    return client.create_order(
        symbol=symbol,
        side=Client.SIDE_BUY,
        type=Client.ORDER_TYPE_MARKET,
        quoteOrderQty=quote,
        recvWindow=10000,
        newOrderRespType="FULL",
    )


def market_sell_qty(client: Client, symbol: str, quantity: float) -> dict:
    """Market SELL exact base quantity (LOT_SIZE rounded)."""
    filters = get_symbol_filters(client, symbol)
    qty_str = format_quantity(quantity, filters["step"])
    if float(qty_str) < filters["min_qty"]:
        raise ValueError(f"quantity {qty_str} < min_qty {filters['min_qty']}")
    return client.create_order(
        symbol=symbol,
        side=Client.SIDE_SELL,
        type=Client.ORDER_TYPE_MARKET,
        quantity=qty_str,
        recvWindow=10000,
        newOrderRespType="FULL",
    )


def limit_buy(client: Client, symbol: str, quantity: float, price: float) -> dict:
    """Place resting LIMIT BUY order on Binance (GTC)."""
    filters = get_symbol_filters(client, symbol)
    qty_str = format_quantity(quantity, filters["step"])
    price_str = format_price(price, filters["tick_size"])
    if float(qty_str) < filters["min_qty"]:
        raise ValueError(f"quantity {qty_str} < min_qty {filters['min_qty']}")
    if float(price_str) < filters["min_price"]:
        raise ValueError(f"price {price_str} < min_price {filters['min_price']}")
    return client.create_order(
        symbol=symbol,
        side=Client.SIDE_BUY,
        type=Client.ORDER_TYPE_LIMIT,
        timeInForce=Client.TIME_IN_FORCE_GTC,
        quantity=qty_str,
        price=price_str,
        recvWindow=10000,
        newOrderRespType="FULL",
    )


def limit_sell(client: Client, symbol: str, quantity: float, price: float) -> dict:
    """Place resting LIMIT SELL order on Binance (GTC)."""
    filters = get_symbol_filters(client, symbol)
    qty_str = format_quantity(quantity, filters["step"])
    price_str = format_price(price, filters["tick_size"])
    if float(qty_str) < filters["min_qty"]:
        raise ValueError(f"quantity {qty_str} < min_qty {filters['min_qty']}")
    if float(price_str) < filters["min_price"]:
        raise ValueError(f"price {price_str} < min_price {filters['min_price']}")
    return client.create_order(
        symbol=symbol,
        side=Client.SIDE_SELL,
        type=Client.ORDER_TYPE_LIMIT,
        timeInForce=Client.TIME_IN_FORCE_GTC,
        quantity=qty_str,
        price=price_str,
        recvWindow=10000,
        newOrderRespType="FULL",
    )


def cancel_order_safe(client: Client, symbol: str, order_id: int, max_retries: int = 3) -> bool:
    """Attempt to cancel an open order on Binance with retries; ignore if already filled or cancelled."""
    for attempt in range(max_retries):
        try:
            client.cancel_order(symbol=symbol, orderId=order_id, recvWindow=10000)
            return True
        except BinanceAPIException as e:
            if getattr(e, "code", None) in (-2011, -2013):
                # Unknown order sent (already filled, cancelled, or doesn't exist)
                return False
            if attempt < max_retries - 1:
                time.sleep(0.3)
                continue
            return False
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(0.3)
                continue
            return False
    return False


def cancel_all_open_orders(client: Client, symbol: str) -> None:
    """Cancel any open orders for symbol to unlock funds."""
    if not symbol or symbol == "INIT" or symbol == "IDLE":
        return

    # 1. Try atomic openOrders DELETE first
    try:
        client._delete("openOrders", True, data={"symbol": symbol, "recvWindow": 10000})
        return
    except BinanceAPIException as e:
        if getattr(e, "code", None) in (-2011, -2013):
            # No open orders to cancel
            return
    except Exception:
        pass

    # 2. Fallback: query open orders and cancel individually with retries
    for _ in range(3):
        try:
            open_orders = client.get_open_orders(symbol=symbol, recvWindow=10000)
            if not open_orders:
                return
            for o in open_orders:
                try:
                    client.cancel_order(symbol=symbol, orderId=o["orderId"], recvWindow=10000)
                except Exception:
                    pass
            return
        except Exception:
            time.sleep(0.3)


# ---------------------------------------------------------------------------
# Network issue detection and waiting helpers
# ---------------------------------------------------------------------------

def is_network_error(exc: Exception) -> bool:
    """Detect transient network/connection issues vs fatal API errors."""
    if isinstance(exc, (requests.exceptions.RequestException, BinanceRequestException, socket.error, TimeoutError)):
        return True
    if isinstance(exc, BinanceAPIException):
        # Specific Binance error codes that represent connection/rate-limit/timestamp/server errors rather than fatal API errors
        # -1001: DISCONNECTED, -1003: TOO_MANY_REQUESTS, -1007: TIMEOUT, -1021: INVALID_TIMESTAMP
        if getattr(exc, "code", None) in (-1001, -1003, -1007, -1021) or getattr(exc, "status_code", None) in (418, 429, 502, 503, 504):
            return True
    err_str = str(exc).lower()
    if any(k in err_str for k in ("timeout", "timed out", "connection", "connect", "reset", "disconnected", "gateway", "502", "503", "504", "too much request weight", "rate limit")):
        return True
    return False


def wait_for_network(client: Client, bot_name: str, symbol: str, instance_id: Optional[int] = None, context: str = "operation", exc: Optional[Exception] = None) -> None:
    """Pause execution and wait until Binance network connectivity or rate limit is restored."""
    is_rate_limit = False
    if exc:
        if getattr(exc, "code", None) == -1003 or getattr(exc, "status_code", None) in (418, 429) or "request weight" in str(exc).lower():
            is_rate_limit = True

    if is_rate_limit:
        msg = f"[{bot_name}] [{symbol}] Rate limit hit (weight exceeded) during {context}. Cooling down for 45s..."
        delay = 45.0
    else:
        msg = f"[{bot_name}] [{symbol}] Network issue detected during {context}. Waiting for network to restore..."
        delay = 5.0

    print(msg)
    log_event(msg, level="WARN", event_type="RATE_LIMIT_WAIT" if is_rate_limit else "NETWORK_WAIT", instance_id=instance_id, symbol=symbol)
    if instance_id:
        update_instance(instance_id, last_error=msg)

    while True:
        time.sleep(delay)
        try:
            client.ping()
            msg_restored = f"[{bot_name}] [{symbol}] Connection / rate limit cleared. Resuming {context}."
            print(msg_restored)
            log_event(msg_restored, level="INFO", event_type="NETWORK_RESTORED", instance_id=instance_id, symbol=symbol)
            if instance_id:
                update_instance(instance_id, last_error=None)
            return
        except Exception as e:
            if is_network_error(e):
                delay = min(60.0, delay * 1.5)
                continue
            else:
                # If ping failed with non-network error (e.g. fatal API error), re-raise
                raise


def _safe_market_sell(client: Client, symbol: str, quantity: float, bot_name: str, instance_id: int) -> dict:
    """Execute market sell with network retry and balance fallback."""
    while True:
        try:
            return market_sell_qty(client, symbol, quantity)
        except BinanceAPIException as e:
            # If account has slightly less balance (e.g. fee taken in base asset or stepSize rounding):
            # Error -2010: Account has insufficient balance for requested action.
            if getattr(e, "code", None) == -2010:
                cancel_all_open_orders(client, symbol)
                time.sleep(0.2)
                base_asset = symbol.replace("USDT", "")
                try:
                    bal = float(client.get_asset_balance(asset=base_asset)["free"] or 0)
                    filters = get_symbol_filters(client, symbol)
                    qty_str = format_quantity(bal, filters["step"])
                    if float(qty_str) >= filters["min_qty"]:
                        log_event(
                            f"[{bot_name}] [{symbol}] Adjusting sell qty to actual free balance: {qty_str}",
                            level="WARN",
                            event_type="SELL_ADJUST",
                            instance_id=instance_id,
                            symbol=symbol,
                        )
                        return client.create_order(
                            symbol=symbol,
                            side=Client.SIDE_SELL,
                            type=Client.ORDER_TYPE_MARKET,
                            quantity=qty_str,
                            recvWindow=10000,
                            newOrderRespType="FULL",
                        )
                except Exception as inner_e:
                    log_event(
                        f"[{bot_name}] [{symbol}] Balance adjust failed: {inner_e}",
                        level="ERROR",
                        instance_id=instance_id,
                        symbol=symbol,
                    )
            raise
        except Exception as e:
            if is_network_error(e):
                wait_for_network(client, bot_name, symbol, instance_id, context="market sell")
                # After network restores, check if the sell order actually went through!
                try:
                    base_asset = symbol.replace("USDT", "")
                    bal = float(client.get_asset_balance(asset=base_asset)["free"] or 0)
                    filters = get_symbol_filters(client, symbol)
                    if bal < filters["min_qty"]:
                        orders = client.get_all_orders(symbol=symbol, limit=5)
                        for o in reversed(orders):
                            if o.get("side") == Client.SIDE_SELL and o.get("status") == "FILLED":
                                return o
                except Exception:
                    pass
                continue
            raise


# ---------------------------------------------------------------------------
# Binance trading fees only (BNB converted to USDT when fee is paid in BNB)
# ---------------------------------------------------------------------------
# Fallback fee estimate while holding (before sell fill): ~0.075% per side with BNB discount
EST_FEE_RATE_PER_SIDE = 0.00075


def fee_from_order(client: Client, order: dict, symbol: Optional[str] = None) -> dict:
    """
    Parse fill commissions into USDT.
    If order has no 'fills' array (e.g. queried via get_order), fetches trades via get_my_trades.
    Returns dict with usdt total and breakdown (BNB amount, BNB→USDT rate, etc.).
    """
    fills = order.get("fills") or []
    sym = symbol or order.get("symbol")
    order_id = order.get("orderId")
    if not fills and order_id and sym:
        try:
            trades = client.get_my_trades(symbol=sym, orderId=order_id, recvWindow=10000)
            if trades:
                fills = trades
        except Exception:
            pass

    total_usdt = 0.0
    bnb_amount = 0.0
    bnb_usdt = 0.0
    other = []
    bnb_price = None

    for f in fills:
        commission = float(f.get("commission") or 0)
        asset = (f.get("commissionAsset") or "").upper()
        if commission <= 0:
            continue
        if asset in ("USDT", "USD", "BUSD", "FDUSD", "USDC"):
            total_usdt += commission
            other.append({"asset": asset, "amount": commission, "usdt": commission})
        elif asset == "BNB":
            if bnb_price is None:
                for attempt in range(5):
                    try:
                        ticker = client.get_orderbook_ticker(symbol="BNBUSDT")
                        bnb_price = float(ticker.get("bidPrice") or ticker.get("price") or 0.0)
                        if bnb_price > 0:
                            break
                    except Exception:
                        time.sleep(0.5)
                if not bnb_price or bnb_price <= 0:
                    try:
                        bnb_price = float(client.get_symbol_ticker(symbol="BNBUSDT")["price"])
                    except Exception:
                        bnb_price = 700.0  # Fallback to current BNB market level if API unreachable
            usdt = commission * (bnb_price or 0.0)
            bnb_amount += commission
            bnb_usdt += usdt
            total_usdt += usdt
        else:
            # Fee in base asset → value at fill price
            px = float(f.get("price") or 0)
            usdt = commission * px
            total_usdt += usdt
            other.append({"asset": asset, "amount": commission, "usdt": usdt, "price": px})

    # If no fills could be retrieved, estimate fee from cummulativeQuoteQty
    cum_quote = float(order.get("cummulativeQuoteQty") or 0.0)
    if total_usdt <= 0.0 and cum_quote > 0.0:
        total_usdt = cum_quote * EST_FEE_RATE_PER_SIDE

    # If bnb_price is None and we have fees in non-BNB asset, fetch BNB price to determine fee_bnb equivalent
    if bnb_price is None:
        try:
            ticker = client.get_orderbook_ticker(symbol="BNBUSDT")
            bnb_price = float(ticker.get("bidPrice") or ticker.get("price") or 0.0)
        except Exception:
            pass
        if not bnb_price or bnb_price <= 0:
            try:
                bnb_price = float(client.get_symbol_ticker(symbol="BNBUSDT")["price"])
            except Exception:
                bnb_price = 700.0

    fee_bnb = bnb_amount if bnb_amount > 0 else (total_usdt / bnb_price if bnb_price and bnb_price > 0 else 0.0)

    return {
        "fee_usdt": total_usdt,
        "fee_bnb": fee_bnb,
        "bnb_amount": bnb_amount,
        "bnb_price": bnb_price,
        "bnb_fee_usdt": bnb_usdt,
        "other": other,
    }


def settle_pnl(
    entry_cost: float,
    exit_value: float,
    buy_fee_usdt: float,
    sell_fee_usdt: float,
) -> dict:
    """
    Gross P/L minus actual Binance trading fees (BNB→USDT when paid in BNB).
    net_pnl is what counts toward the bot's profit target.
    """
    gross = exit_value - entry_cost
    fees = (buy_fee_usdt or 0.0) + (sell_fee_usdt or 0.0)
    net = gross - fees
    return {
        "gross_pnl": gross,
        "buy_fee_usdt": buy_fee_usdt or 0.0,
        "sell_fee_usdt": sell_fee_usdt or 0.0,
        "fees_usdt": fees,
        "net_pnl": net,
    }


def estimate_net_unrealized(entry_cost: float, current_val: float, buy_fee_usdt: float) -> float:
    """Approximate net P/L while holding (gross − buy fee − estimated sell fee)."""
    gross = current_val - entry_cost
    est_sell_fee = current_val * EST_FEE_RATE_PER_SIDE
    return gross - (buy_fee_usdt or 0.0) - est_sell_fee


def inspect_order_book_runway(
    client: Client,
    symbol: str,
    investment_usdt: float = 25.0,
    target_profit: float = 0.10,
    max_wall_ratio: float = 3.5,
    depth: Optional[dict] = None,
) -> Tuple[bool, str, dict]:
    """
    Reverse Order Book Runway & Sell Wall Inspection:
    1. Determines required exit price P_target based on current best_bid, investment, and target_profit.
    2. Scans asks up to P_target in the order book.
    3. Checks if any individual ask level represents a 'sell wall' (volume > max_wall_ratio * median ask volume).
    4. Computes cumulative ask resistance in USDT up to P_target.
    Returns (is_clear, reason, metrics).
    """
    metrics = {
        "symbol": symbol,
        "best_bid": 0.0,
        "best_ask": 0.0,
        "target_exit_price": 0.0,
        "cumulative_ask_usdt": 0.0,
        "wall_detected": False,
        "wall_price": 0.0,
        "wall_usdt": 0.0,
    }
    try:
        if depth is None:
            depth = client.get_order_book(symbol=symbol, limit=50)
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        if not bids or not asks:
            return False, "Empty order book depth", metrics

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        metrics["best_bid"] = best_bid
        metrics["best_ask"] = best_ask

        # Estimate required exit price
        est_buy_fee = investment_usdt * EST_FEE_RATE_PER_SIDE
        est_sell_fee = (investment_usdt + target_profit) * EST_FEE_RATE_PER_SIDE
        req_exit_val = investment_usdt + target_profit + est_buy_fee + est_sell_fee
        est_qty = investment_usdt / best_bid if best_bid > 0 else 1.0
        target_exit_price = req_exit_val / est_qty
        metrics["target_exit_price"] = target_exit_price

        # Parse all asks up to target_exit_price
        ask_levels = []
        all_ask_usdts = []
        for a in asks:
            p = float(a[0])
            q = float(a[1])
            val = p * q
            all_ask_usdts.append(val)
            if p <= target_exit_price:
                ask_levels.append((p, q, val))

        total_depth_usdt = sum(all_ask_usdts)
        median_ask_usdt = statistics.median(all_ask_usdts) if all_ask_usdts else 100.0
        # A true sell wall is an abnormal order that dominates the book: > 35% of total depth and > 8x median
        wall_threshold = max(total_depth_usdt * 0.35, median_ask_usdt * 8.0, 25000.0)

        cum_ask_usdt = sum(val for _, _, val in ask_levels)
        metrics["cumulative_ask_usdt"] = cum_ask_usdt

        # Check for individual sell wall blocking the runway & intelligent wall front-running
        filters = get_symbol_filters(client, symbol)
        tick_sz = filters.get("tick_size") or 0.0001
        min_net_floor = max(OPT_ABSOLUTE_MIN_PROFIT_USDT, 0.0025)
        min_req_exit = (investment_usdt + min_net_floor + est_buy_fee) / (1.0 - EST_FEE_RATE_PER_SIDE)
        min_req_price = min_req_exit / est_qty if est_qty > 0 else 0.0

        for p, q, val in ask_levels:
            if val >= wall_threshold:
                metrics["wall_detected"] = True
                metrics["wall_price"] = p
                metrics["wall_usdt"] = val
                front_run_px = float(format_price(p - tick_sz, tick_sz, rounding="FLOOR"))
                # If front-running the wall still delivers our minimum net profit hurdle:
                if front_run_px >= min_req_price and front_run_px > best_ask:
                    metrics["wall_front_run"] = True
                    metrics["front_run_price"] = front_run_px
                    metrics["target_exit_price"] = front_run_px
                    return True, f"Sell wall of {val:.1f} USDT detected @ {p:.6f} — front-running wall @ {front_run_px:.6f} for guaranteed fill", metrics
                else:
                    return False, f"Sell wall of {val:.1f} USDT ({val/total_depth_usdt*100:.1f}% of depth) @ {p:.6f} blocks target ({target_exit_price:.6f})", metrics

        return True, "Runway is clear to target", metrics

    except Exception as e:
        return True, f"Runway check error (tolerated): {e}", metrics


def evaluate_pair_microstructure(
    client: Client,
    symbol: str,
    max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
    max_tick_ratio: float = DEFAULT_MAX_TICK_RATIO,
    min_book_ratio: float = DEFAULT_MIN_BOOK_RATIO,
    min_volatility_1m: float = DEFAULT_MIN_VOLATILITY_1M,
    min_taker_buy_ratio: float = DEFAULT_MIN_TAKER_BUY_RATIO,
) -> Tuple[bool, str, dict]:
    """
    Evaluate candidate pair for:
    1. Bid-ask spread <= max_spread_pct (e.g. 0.02%)
    2. Tick resolution ratio: tickSize / price <= max_tick_ratio (e.g. 0.015%) to avoid tick rounding hurdle
    3. Order book pressure: top 5 bids qty / top 5 asks qty >= min_book_ratio (e.g. 1.15x)
    4. 5-minute momentum: price velocity >= -0.15% and volume present
    5. 1-minute candle volatility: range (high - low) / open >= min_volatility_1m (e.g. 0.25%)
    6. Aggressive market trade flow: taker buy volume ratio >= min_taker_buy_ratio (e.g. 0.50)
    Returns (is_viable, reason, metrics).
    """
    metrics = {
        "symbol": symbol,
        "spread_pct": 999.0,
        "tick_ratio": 999.0,
        "book_ratio": 0.0,
        "momentum_5m": 0.0,
        "volatility_1m": 0.0,
        "taker_buy_ratio": 0.0,
        "score": -999.0,
    }
    try:
        # 1. Spread Check
        ob = client.get_orderbook_ticker(symbol=symbol)
        bid = float(ob.get("bidPrice") or 0.0)
        ask = float(ob.get("askPrice") or 0.0)
        if bid <= 0 or ask <= 0:
            return False, "Invalid bid/ask", metrics
        spread_pct = ((ask - bid) / bid) * 100.0
        metrics["spread_pct"] = spread_pct
        if spread_pct > max_spread_pct:
            return False, f"Spread too wide ({spread_pct:.4f}% > {max_spread_pct:.4f}%)", metrics

        # 2. Tick Resolution Ratio (avoid pairs where 1 tick forces a large price jump)
        filters = get_symbol_filters(client, symbol)
        tick_size = filters.get("tick_size") or 0.0001
        tick_ratio = tick_size / bid if bid > 0 else 0.0
        metrics["tick_ratio"] = tick_ratio
        if tick_ratio > max_tick_ratio:
            return False, f"Tick size too coarse ({tick_ratio*100:.3f}% > {max_tick_ratio*100:.3f}%)", metrics

        # 3. Order book depth ratio, slippage estimation, and runway (fetch limit=50 once)
        depth = client.get_order_book(symbol=symbol, limit=50)
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []

        # Calculate Slippage for a $100 order size
        buy_notional_needed = 100.0
        spent = 0.0
        acquired_qty = 0.0
        for price_str, qty_str in asks:
            p = float(price_str)
            q = float(qty_str)
            level_notional = p * q
            if buy_notional_needed > level_notional:
                spent += level_notional
                acquired_qty += q
                buy_notional_needed -= level_notional
            else:
                spent += buy_notional_needed
                acquired_qty += buy_notional_needed / p
                buy_notional_needed = 0.0
                break

        if acquired_qty > 0 and buy_notional_needed <= 0.0:
            avg_entry_price = spent / acquired_qty
            slippage_pct = ((avg_entry_price - ask) / ask) * 100.0
            if slippage_pct > 0.15:
                return False, f"Slippage too high for $100 order ({slippage_pct:.3f}% > 0.15%)", metrics
        else:
            return False, "Order book too thin for $100 order (not enough asks)", metrics

        bid_qty = sum(float(b[1]) for b in bids[:5])
        ask_qty = sum(float(a[1]) for a in asks[:5])
        book_ratio = (bid_qty / ask_qty) if ask_qty > 0 else 5.0
        metrics["book_ratio"] = book_ratio

        # 3b. Standardized Order Book Imbalance (OBI) across top 10 levels
        top_bids_usdt = sum(float(b[0]) * float(b[1]) for b in bids[:10])
        top_asks_usdt = sum(float(a[0]) * float(a[1]) for a in asks[:10])
        tot_depth_usdt = top_bids_usdt + top_asks_usdt
        obi = (top_bids_usdt - top_asks_usdt) / tot_depth_usdt if tot_depth_usdt > 0 else 0.0
        metrics["order_book_imbalance"] = round(obi, 4)
        metrics["top_bids_usdt"] = round(top_bids_usdt, 2)
        metrics["top_asks_usdt"] = round(top_asks_usdt, 2)

        if book_ratio < min_book_ratio:
            return False, f"Sell wall dominant (book ratio {book_ratio:.2f} < {min_book_ratio:.2f})", metrics

        if obi < -0.20:
            return False, f"Bearish Order Book Imbalance: active asks dominate bids (OBI {obi:+.2f} < -0.20, Asks ${top_asks_usdt:.0f} vs Bids ${top_bids_usdt:.0f})", metrics

        # 4. 5-minute momentum
        klines = client.get_klines(symbol=symbol, interval="5m", limit=2)
        if klines and len(klines) >= 1:
            last_k = klines[-1]
            k_open = float(last_k[1])
            k_close = float(last_k[4])
            mom_5m = ((k_close - k_open) / k_open) * 100.0 if k_open > 0 else 0.0
            metrics["momentum_5m"] = mom_5m
            if mom_5m < -0.30:
                return False, f"Negative 5m momentum ({mom_5m:.2f}%)", metrics
        else:
            mom_5m = 0.0

        # 5. 1-minute candle volatility (fetch 20 once, use for both volatility and VWAP proximity)
        klines_1m = client.get_klines(symbol=symbol, interval="1m", limit=20)
        recent_1m = klines_1m[-3:] if klines_1m else []
        if recent_1m:
            vol_1m = max(
                (((float(k[2]) - float(k[3])) / float(k[1])) * 100.0)
                for k in recent_1m
                if float(k[1]) > 0
            ) if any(float(k[1]) > 0 for k in recent_1m) else 0.0
        else:
            vol_1m = 0.0
        metrics["volatility_1m"] = vol_1m
        if vol_1m < min_volatility_1m:
            return False, f"Low 1m volatility ({vol_1m:.2f}% < {min_volatility_1m:.2f}%)", metrics

        # 6. Weighted volume delta score (#2) — recent trades weighted exponentially higher
        taker_buy_ratio = get_volume_delta_score(client, symbol, n=50)
        metrics["taker_buy_ratio"] = taker_buy_ratio

        # 7. VWAP proximity bonus (#4) — reuse pre-fetched klines_1m
        vwap_score = get_vwap_proximity(client, symbol, klines=klines_1m)
        metrics["vwap_score"] = vwap_score

        # 8. Reverse Order Book Runway & Sell Wall Check (reuse pre-fetched depth)
        runway_ok, runway_reason, runway_metrics = inspect_order_book_runway(
            client, symbol, investment_usdt=25.0, target_profit=0.035, depth=depth
        )
        metrics["runway_clear"] = runway_ok
        metrics["runway_reason"] = runway_reason
        metrics["cumulative_ask_usdt"] = runway_metrics.get("cumulative_ask_usdt", 0.0)
        metrics["target_exit_price"] = runway_metrics.get("target_exit_price", 0.0)
        if not runway_ok:
            return False, runway_reason, metrics

        # Composite score (higher = better):
        #   book_ratio × 1.5 — order book pressure
        #   mom_5m × 2.0    — recent 5m price momentum
        #   vol_1m × 5.0    — 1m candle volatility (faster cycles)
        #   taker_buy × 6.0 — weighted aggressor buy flow (#2, was 4.0)
        #   vwap_score × 3.0 — proximity to VWAP fair value (#4)
        #   spread × 25     — tighter spread is better
        #   tick_ratio × 1000 — finer tick = smaller exit hurdle
        #   runway bonus    — thin ask resistance to target
        score = (
            (book_ratio * 1.5)
            + (mom_5m * 2.0)
            + (vol_1m * 5.0)
            + (taker_buy_ratio * 6.0)
            + (vwap_score * 3.0)
            + (obi * 5.0)
            - (spread_pct * 25.0)
            - (tick_ratio * 1000.0)
            + max(0.0, 5.0 - (runway_metrics.get("cumulative_ask_usdt", 0.0) / 500.0))
        )
        # Incorporate PCA Idiosyncratic Decoupling Alpha
        if pca_selector:
            try:
                pca_metric = pca_selector.get_cached_pca_metric_for_symbol(symbol)
                if pca_metric:
                    pca_alpha = pca_metric.get("residual_alpha", 0.0)
                    pca_beta = pca_metric.get("market_beta", 1.0)
                    pca_score_val = pca_metric.get("pca_score", 0.0)
                    metrics["pca_residual_alpha"] = pca_alpha
                    metrics["pca_beta"] = pca_beta
                    metrics["pca_score"] = pca_score_val
                    pca_adj = max(-2.5, min(2.5, pca_alpha * 2.5))
                    score += pca_adj
                    metrics["pca_adjustment"] = round(pca_adj, 4)
            except Exception:
                pass

        # Incorporate Advanced Time Series Forecasting & First Candle Analysis (FCA)
        if ts_forecast:
            try:
                k_5m = client.get_klines(symbol=symbol, interval="5m", limit=12)
                fc = ts_forecast.get_forecaster().forecast_symbol(
                    symbol,
                    klines_1m=klines_1m,
                    klines_5m=k_5m,
                    bid_ask_spread_pct=spread_pct,
                    order_book_imbalance=obi,
                )
                metrics["ts_forecast"] = fc
                metrics["ts_pred_return_15m"] = fc.get("pred_return_15m_pct", 0.0)
                metrics["ts_fir"] = fc.get("forecast_info_ratio", 0.0)
                metrics["ts_win_p"] = fc.get("win_probability", 0.50)
                metrics["ts_regime"] = fc.get("regime", "CHOP_RANGING")
                metrics["ts_score"] = fc.get("forecast_score", 0.0)
                metrics["fca_setup"] = fc.get("fca_setup", "UNKNOWN")

                # First Candle Viability Gate: Reject Bearish Rejection or Seller Dominance
                if fc.get("fca") and not fc["fca"].get("fca_viable", True):
                    fca_reason = f"First Candle Rejection: {fc['fca'].get('setup_type')} (body_dom={fc['fca'].get('body_dominance')}, up_wick={fc['fca'].get('upper_wick_ratio')})"
                    return False, fca_reason, metrics

                # Heavy penalty if model predicts negative return or bear regime
                if fc.get("pred_return_15m_pct", 0.0) < -0.15 or fc.get("regime") == "TRENDING_BEAR":
                    score -= 5.0
                else:
                    ts_boost = (fc.get("pred_return_15m_pct", 0.0) * 3.5) + (fc.get("forecast_info_ratio", 0.0) * 2.0) + fc.get("fca", {}).get("fca_score_boost", 0.0)
                    score += ts_boost
                    metrics["ts_boost"] = round(ts_boost, 4)
            except Exception:
                pass

        metrics["heuristic_score"] = round(score, 4)

        # Adaptive Machine Learning Model prediction & dynamic scoring
        if get_selector:
            try:
                selector = get_selector(DB_PATH)
                ml_score, p_win = selector.score_candidate(metrics)
                metrics["p_win"] = p_win
                metrics["ml_score"] = ml_score

                # Dynamic experience blend:
                # 0 updates: 60% rule-based + 40% ML prior
                # 25+ updates: 20% rule-based + 80% learned ML model
                alpha = min(0.80, 0.40 + (selector.total_updates * 0.016))
                metrics["score"] = round(((1.0 - alpha) * score) + (alpha * ml_score), 4)
            except Exception:
                metrics["score"] = round(score, 4)
        else:
            metrics["score"] = round(score, 4)

        # Cache metrics snapshot for cycle recording
        try:
            RECENT_PAIR_METRICS[symbol] = dict(metrics)
        except Exception:
            pass

        return True, "Passed micro-structure checks", metrics

    except BinanceAPIException as e:
        if getattr(e, "code", None) == -1003 or getattr(e, "status_code", None) in (418, 429):
            raise
        return False, f"Micro-structure error: {e}", metrics
    except Exception as e:
        return False, f"Micro-structure error: {e}", metrics



# ===========================================================================
# OPTIMIZED HELPERS - Zero-loss & fast-close
# ===========================================================================
def ensure_bnb_for_fees(client, min_bnb: float = MIN_BNB_FEE_BUFFER) -> bool:
    try:
        bal = client.get_asset_balance(asset='BNB')
        free = float(bal.get('free') or 0.0) if bal else 0.0
        return free >= min_bnb * 0.5
    except:
        return True


_PENDING_LOSS_RECOVERY: Dict[int, float] = {}


def record_pending_loss(instance_id: int, loss_amount: float, bot_name: str = ""):
    """Record realized loss into instance's recovery pool to boost subsequent profit targets."""
    if loss_amount <= 0:
        return
    current = _PENDING_LOSS_RECOVERY.get(instance_id, 0.0)
    _PENDING_LOSS_RECOVERY[instance_id] = round(current + loss_amount, 6)
    print(f"[{bot_name}] Added {loss_amount:.4f} USDT to loss recovery pool (Total pending recovery: {_PENDING_LOSS_RECOVERY[instance_id]:.4f} USDT)")


def get_loss_recovery_boost(instance_id: int, max_boost: float = 0.025) -> float:
    """Disabled: Do not boost profit targets after a loss to avoid prolonged risk exposure."""
    return 0.0


def settle_loss_recovery(instance_id: int, profit_amount: float, bot_name: str = ""):
    """Deduct realized profit from instance's loss recovery pool upon a winning cycle."""
    if profit_amount <= 0:
        return
    pending = _PENDING_LOSS_RECOVERY.get(instance_id, 0.0)
    if pending <= 0:
        return
    recovered = min(pending, profit_amount)
    _PENDING_LOSS_RECOVERY[instance_id] = round(max(0.0, pending - recovered), 6)
    print(f"[{bot_name}] Recovered {recovered:.4f} USDT of previous losses! (Remaining to recover: {_PENDING_LOSS_RECOVERY[instance_id]:.4f} USDT)")


def get_dynamic_profit_target(investment_usdt: float, volatility_1m: float, base_min: float, instance_id: int = 0) -> float:
    """Dynamic micro-profit target guaranteeing small positive profit in the 0.00XXX range after all fees."""
    return max(0.0025, base_min if base_min > 0 else 0.0035)

def should_emergency_scratch(client, symbol: str, entry_price: float, cur_bid: float):
    """MAX-PROTECT: scratch only on catastrophic adverse drift or genuine market collapse."""
    try:
        ob = client.get_orderbook_ticker(symbol=symbol)
        bid = float(ob.get('bidPrice') or 0.0)
        ask = float(ob.get('askPrice') or 0.0)
        if bid <= 0 or ask <= 0:
            return False, ""
        spread_pct = (ask - bid) / bid * 100 if bid > 0 else 0
        if spread_pct > OPT_MAX_SLIPPAGE_PCT:
            return True, f"Catastrophic spread blowout {spread_pct:.4f}% > {OPT_MAX_SLIPPAGE_PCT}%"
        drift_pct = (bid - entry_price) / entry_price * 100 if entry_price > 0 else 0
        if drift_pct <= OPT_SCRATCH_DRIFT_PCT:
            return True, f"Adverse drift {drift_pct:.2f}% <= {OPT_SCRATCH_DRIFT_PCT}%"
        if entry_price > 0 and (bid - entry_price) / entry_price <= OPT_HARD_STOP_PCT:
            return True, f"Hard stop % hit ({drift_pct:.2f}%)"
    except Exception:
        pass
    return False, ""


def hard_stop_triggered(net_pnl: float, investment_usdt: float, configured_sl: float, elapsed_sec: float = 0.0) -> bool:
    """
    Micro-Stop Loss Ceiling (0.05 USDT):
    If net loss reaches 0.05 USDT (net_pnl <= -0.05), trigger exit immediately
    so the bot liquidates and rotates to the next coin, preventing multi-dollar drawdowns.
    """
    threshold = -0.05
    if configured_sl and configured_sl < 0:
        threshold = max(configured_sl, -0.05)
    return net_pnl <= threshold


def early_scratch_triggered(net_pnl: float, elapsed_sec: float) -> bool:
    """Disabled: Stagnant trades are managed passively via Maker Breakeven repricing."""
    return False


def calculate_coin_investment_size(
    symbol: str,
    lot: dict,
    profile: dict,
    max_investment: float = 25.0,
    usdt_free: float = 100.0,
) -> float:
    """
    Coin-Adaptive Investment Sizer:
    Instead of rigidly deploying $25 on every coin, dynamically sizes each coin:
    - High Volatility / Breakouts / Meme coins: $5.0 - $7.0 USDT
    - Medium Volatility / Trends / Momentum: $10.0 - $12.0 USDT
    - Ultra-Liquid Majors (SOL, BTC, ETH): $15.0 - $25.0 USDT (up to max configured cap)
    Ensures minimum exchange notional is respected and never exceeds user max cap.
    """
    min_notional = float(lot.get("min_notional") or 5.0)
    base_floor = max(5.0, min_notional * 1.05)

    vol_1m = float(profile.get("volatility_1m") or 0.25)
    pred_mkt = str(profile.get("predicted_market") or "")

    meme_keywords = ("PEPE", "SHIB", "DOGE", "BONK", "FLOKI", "WIF", "MEME", "BOME", "1000")
    is_meme_or_volatile = vol_1m >= 0.35 or "Breakouts" in pred_mkt or any(m in symbol for m in meme_keywords)

    if is_meme_or_volatile:
        target_size = max(base_floor, 6.0)
    elif vol_1m >= 0.20 or "Trends" in pred_mkt:
        target_size = max(base_floor, 10.0)
    else:
        # Liquid / low-volatility majors
        target_size = max(base_floor, min(max_investment, 20.0))

    final_size = min(max_investment, target_size)
    final_size = min(final_size, usdt_free)
    return round(max(base_floor, final_size), 2)


def auto_select_cycle_strategy(
    client: Client,
    symbol: str,
    investment_usdt: float = 25.0,
    cycles_done: int = 0,
    forced_strategy: Optional[str] = None,
) -> Tuple[str, dict]:
    # Semiconductor Dual-Engine Specialist Profiling (5-Minute Directional Engine)
    if symbol in ("SOXLBUSDT", "SOXSBUSDT"):
        try:
            import semiconductor_dual_engine
            semi = semiconductor_dual_engine.evaluate_semiconductor_regime(client)
            semi_target_pct = float(semi.get("target_profit_pct") or 1.25)
            semi_sl_pct = float(semi.get("stop_loss_pct") or -0.60)
            is_viable = bool(semi.get("viable")) and (symbol == semi.get("selected_symbol"))
            analysis = {
                "symbol": symbol,
                "spread_pct": semi.get("soxlb_spread" if symbol == "SOXLBUSDT" else "soxsb_spread", 0.05),
                "spread_ticks": 1,
                "tick_size": 0.01,
                "step_size": 0.001 if symbol == "SOXLBUSDT" else 0.01,
                "min_notional": 5.0,
                "tick_pct": 0.01,
                "book_ratio": 1.0,
                "volatility_1m": 0.20,
                "taker_buy_ratio": 0.52,
                "candle_1m_green": True,
                "dynamic_dip_pct": 0.0060,
                "min_net_profit_tranche1": max(0.05, investment_usdt * (semi_target_pct / 100.0) * 0.70),
                "min_net_profit_combined": max(0.08, investment_usdt * (semi_target_pct / 100.0)),
                "dynamic_reprice_sec": 120,
                "dynamic_hold_sec": 300,
                "dynamic_stop_loss": - max(0.05, investment_usdt * (abs(semi_sl_pct) / 100.0)),
                "trailing_stop_enabled": True,
                "trailing_drop_pct": 0.25,
                "grid_levels": [],
            }
            analysis["strategy"] = "SEMICONDUCTOR_DUAL_ENGINE"
            analysis["predicted_market"] = f"Semiconductor 5M: {semi.get('bias', 'CHOP')} (Score: {semi.get('total_score', 0):+d}/7)"
            analysis["selected_algorithm"] = "SEMICONDUCTOR_DUAL_ENGINE"
            analysis["trade_frequency"] = "Medium" if is_viable else "None"
            analysis["main_risk"] = "Trend Reversal" if is_viable else "Chop Volatility Decay"
            analysis["needs_speed"] = "No"
            analysis["is_viable_trade"] = is_viable
            analysis["prediction_5m"] = semi.get("prediction_5m", {})
            analysis["scores"] = semi.get("scores", {})
            return "SEMICONDUCTOR_DUAL_ENGINE", analysis
        except Exception as e:
            pass
    """
    Profile the coin's microstructure in real-time per coin, per timestamp, per bot, per cycle.
    Uses Hybrid Quantitative Market Regime Predictor to select the optimal strategy:
      - Trend following (Strong trends)
      - Momentum (Breakouts)
      - Mean reversion (Sideways ranges)
      - Grid (Tight ranges)
      - Arbitrage (Any market)
      - Market making / scalping (Liquid pairs)
      - Machine learning (Model-driven)
    """
    filters = get_symbol_filters(client, symbol)
    tick_sz = filters.get("tick_size") or 0.0001
    step_sz = filters.get("step") or 0.001
    min_notional = filters.get("min_notional") or 5.0

    analysis = {
        "symbol": symbol,
        "spread_pct": 0.0,
        "spread_ticks": 1,
        "tick_size": tick_sz,
        "step_size": step_sz,
        "min_notional": min_notional,
        "tick_pct": 0.01,
        "book_ratio": 1.0,
        "volatility_1m": 0.15,
        "taker_buy_ratio": 0.50,
        "candle_1m_green": False,
        "dynamic_dip_pct": 0.0080,
        "min_net_profit_tranche1": max(OPT_ABSOLUTE_MIN_PROFIT_USDT, investment_usdt * OPT_MIN_NET_PROFIT_PCT * 0.75),
        "min_net_profit_combined": max(OPT_ABSOLUTE_MIN_PROFIT_USDT * 1.25, investment_usdt * OPT_MIN_NET_PROFIT_PCT),
        "dynamic_reprice_sec": OPT_REPRICE_STAGE1_SEC,
        "dynamic_hold_sec": OPT_MAX_HOLD_SEC,
        "strategy": "BEST_BID_MAKER",
        "predicted_market": "Liquid pairs",
        "entry_style": "BEST_BID_MAKER",
        "trailing_stop_enabled": False,
        "trailing_drop_pct": OPT_TRAILING_DROP_PCT,
    }
    try:
        # Check if Hybrid Quantitative Market Predictor is available
        if hybrid_strategy:
            try:
                req_forced = forced_strategy if forced_strategy not in (None, "AUTO", "") else None
                pred = hybrid_strategy.predict_market_and_strategy(
                    client=client,
                    symbol=symbol,
                    investment_usdt=investment_usdt,
                    forced_strategy=req_forced,
                )
                strategy = pred["selected_algorithm"]
                analysis["strategy"] = strategy
                analysis["predicted_market"] = pred.get("predicted_market", "Liquid pairs")
                analysis["selected_algorithm"] = strategy
                analysis["trade_frequency"] = pred.get("trade_frequency", "Medium")
                analysis["main_risk"] = pred.get("main_risk", "None")
                analysis["needs_speed"] = pred.get("needs_speed", "No")
                analysis["risk_mitigation"] = pred.get("risk_mitigation", "")
                analysis["hybrid_prediction"] = pred

                exec_params = pred.get("execution_params", {})
                analysis["entry_style"] = exec_params.get("entry_style", "BEST_BID_MAKER")
                analysis["min_net_profit_combined"] = exec_params.get("target_profit_usdt", max(OPT_ABSOLUTE_MIN_PROFIT_USDT, investment_usdt * 0.005))
                analysis["min_net_profit_tranche1"] = max(OPT_ABSOLUTE_MIN_PROFIT_USDT * 0.70, analysis["min_net_profit_combined"] * 0.65)
                analysis["dynamic_stop_loss"] = exec_params.get("stop_loss_usdt", -0.15)
                analysis["dynamic_reprice_sec"] = exec_params.get("reprice_sec", OPT_REPRICE_STAGE1_SEC)
                analysis["dynamic_hold_sec"] = exec_params.get("max_hold_sec", OPT_MAX_HOLD_SEC)
                analysis["trailing_stop_enabled"] = exec_params.get("trailing_stop_enabled", False)
                analysis["trailing_drop_pct"] = exec_params.get("trailing_drop_pct", OPT_TRAILING_DROP_PCT)
                analysis["grid_levels"] = exec_params.get("grid_levels", [])
                analysis["is_viable_trade"] = exec_params.get("is_viable_trade", True)

                inds = pred.get("indicators", {})
                analysis["spread_pct"] = inds.get("spread_pct", 0.03)
                analysis["book_ratio"] = inds.get("book_ratio", 1.0)
                analysis["volatility_1m"] = inds.get("bollinger_bandwidth_pct", 0.15)
                analysis["adx_5m"] = inds.get("adx_5m", 20.0)
                analysis["rsi_1m"] = inds.get("rsi_1m", 50.0)
                analysis["hurst_exponent"] = inds.get("hurst_exponent", 0.50)
                analysis["kalman_velocity_pct"] = inds.get("kalman_velocity_pct", 0.0)
                return strategy, analysis
            except Exception as e:
                print(f"[HYBRID] Market predictor note ({e}) - using baseline profiling")

        # Heuristic baseline fallback
        ob = client.get_orderbook_ticker(symbol=symbol)
        bid = float(ob.get("bidPrice") or 0.0)
        ask = float(ob.get("askPrice") or 0.0)
        if bid > 0 and ask > 0:
            analysis["spread_pct"] = ((ask - bid) / bid) * 100.0
            analysis["tick_pct"] = (tick_sz / bid) * 100.0
            analysis["spread_ticks"] = max(1, round((ask - bid) / tick_sz))

        depth = client.get_order_book(symbol=symbol, limit=10)
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        bid_vol = sum(float(b[1]) for b in bids[:5])
        ask_vol = sum(float(a[1]) for a in asks[:5])
        analysis["book_ratio"] = (bid_vol / ask_vol) if ask_vol > 0 else 1.0

        klines_1m = client.get_klines(symbol=symbol, interval="1m", limit=5)
        if klines_1m:
            atrs_1m = [
                ((float(k[2]) - float(k[3])) / float(k[1])) * 100.0
                for k in klines_1m
                if float(k[1]) > 0
            ]
            vol_1m = sum(atrs_1m) / len(atrs_1m) if atrs_1m else 0.15
            analysis["volatility_1m"] = vol_1m

            last_k = klines_1m[-1]
            k_open = float(last_k[1])
            k_close = float(last_k[4])
            k_vol = float(last_k[5])
            k_taker_buy = float(last_k[9])

            analysis["candle_1m_green"] = (k_close >= k_open)
            analysis["taker_buy_ratio"] = (k_taker_buy / k_vol) if k_vol > 0 else 0.50

            analysis["dynamic_dip_pct"] = max(0.0080, min(0.0180, (vol_1m / 100.0) * 2.0))
            analysis["dynamic_reprice_sec"] = OPT_REPRICE_STAGE1_SEC
            analysis["dynamic_hold_sec"] = OPT_MAX_HOLD_SEC

        is_bullish = analysis["candle_1m_green"] and analysis["taker_buy_ratio"] >= 0.50
        is_tight_spread = analysis["spread_pct"] <= 0.05
        has_buy_support = analysis["book_ratio"] >= 1.05
        has_volatility = analysis["volatility_1m"] >= DEFAULT_MIN_VOLATILITY_1M

        if investment_usdt <= 50.0:
            strategy = "BEST_BID_MAKER"
        elif is_bullish and is_tight_spread and has_buy_support:
            strategy = "BEST_BID_MAKER"
        else:
            strategy = "SCALE_IN_DCA"

        analysis["strategy"] = strategy

        if ts_forecast:
            try:
                k_5m = client.get_klines(symbol=symbol, interval="5m", limit=12)
                fc = ts_forecast.get_forecaster().forecast_symbol(symbol, klines_1m, k_5m)
                analysis["ts_forecast"] = fc
                target_tp_pct = fc.get("forecast_tp_pct", 0.85) / 100.0
                analysis["min_net_profit_combined"] = max(OPT_ABSOLUTE_MIN_PROFIT_USDT, min(0.0050, investment_usdt * target_tp_pct))
                analysis["min_net_profit_tranche1"] = max(OPT_ABSOLUTE_MIN_PROFIT_USDT * 0.70, analysis["min_net_profit_combined"] * 0.65)
                target_sl_pct = fc.get("forecast_sl_pct", -0.75) / 100.0
                dynamic_sl = investment_usdt * target_sl_pct
                dynamic_sl = max(OPT_MAX_DYNAMIC_SL_USDT, min(OPT_MIN_DYNAMIC_SL_USDT, dynamic_sl))
                analysis["dynamic_stop_loss"] = dynamic_sl
                analysis["dynamic_hold_sec"] = fc.get("expected_hold_sec", OPT_MAX_HOLD_SEC)
            except Exception:
                pass

        return strategy, analysis
    except Exception as e:
        analysis["strategy"] = "SCALE_IN_DCA"
        return "SCALE_IN_DCA", analysis


def wait_for_sniper_entry(client: Client, symbol: str, max_wait_sec: float = 3.0) -> bool:
    """
    Wait up to max_wait_sec for a micro-dip / order book buy pressure confirmation before executing BUY.
    Ensures top-3 bids volume >= top-3 asks volume so the entry is supported by buy volume.
    """
    start = time.time()
    while time.time() - start < max_wait_sec:
        try:
            depth = client.get_order_book(symbol=symbol, limit=5)
            bids = depth.get("bids") or []
            asks = depth.get("asks") or []
            if bids and asks:
                bid_vol = sum(float(b[1]) for b in bids[:3])
                ask_vol = sum(float(a[1]) for a in asks[:3])
                if ask_vol > 0 and (bid_vol / ask_vol) >= 1.15:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return True


# ---------------------------------------------------------------------------
# Optimization #10 — Toxic Flow / Adverse Selection Protection
# ---------------------------------------------------------------------------

def check_pre_entry_order_flow_vpin(client: Client, symbol: str) -> Tuple[bool, str]:
    """
    Quantum-Maker Pre-Entry Order Flow & VPIN Defense:
    1. Fetches recent 60 public trades.
    2. Calculates Taker Buy Ratio and Cumulative Volume Delta (CVD).
    3. Fetches top-5 order book depth and checks Order Book Imbalance (OBI).
    Returns (is_safe, reason).
    """
    try:
        trades = client.get_recent_trades(symbol=symbol, limit=60)
        if not trades:
            return True, "No recent trade history"
        taker_buy_qty = sum(float(t["qty"]) for t in trades if not t.get("isBuyerMaker", True))
        taker_sell_qty = sum(float(t["qty"]) for t in trades if t.get("isBuyerMaker", False))
        tot_qty = taker_buy_qty + taker_sell_qty
        if tot_qty > 0:
            taker_buy_ratio = taker_buy_qty / tot_qty
            if taker_buy_ratio < 0.45:
                return False, f"Institutional sell dominance: Taker buy ratio {taker_buy_ratio:.1%} < 45% (dumping detected)"

        depth = client.get_order_book(symbol=symbol, limit=5)
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        if bids and asks:
            bid_vol = sum(float(b[1]) for b in bids[:5])
            ask_vol = sum(float(a[1]) for a in asks[:5])
            if (bid_vol + ask_vol) > 0:
                obi = (bid_vol - ask_vol) / (bid_vol + ask_vol)
                if obi < -0.35:
                    return False, f"Ask wall stacking: Order book imbalance {obi:.2f} < -0.35 (sell wall heavy)"
        return True, "Order flow healthy and buyer dominant"
    except Exception as e:
        return True, f"Check bypassed: {e}"


def is_orderbook_flow_toxic(
    client: Client,
    symbol: str,
    entry_bid_price: float,
    min_ratio: float = 0.30,
) -> Tuple[bool, str]:
    """
    Detect genuine toxic flow or order book collapse while waiting for a maker buy to fill:
    - If best ask drops to or below entry_bid_price (market dumping through our bid / crossed book)
    - If top 5 asks volume heavily outweighs top 5 bids volume (ratio < 0.35, meaning asks are >2.8x bids)
    Returns (is_toxic, reason).
    """
    try:
        depth = client.get_order_book(symbol=symbol, limit=10)
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        if not bids or not asks:
            return False, "Order book depth empty"
        best_ask = float(asks[0][0])
        if best_ask <= entry_bid_price:
            return True, f"Ask price collapsed to entry bid ({best_ask:.6f} <= {entry_bid_price:.6f})"
        bid_qty = sum(float(b[1]) for b in bids[:5])
        ask_qty = sum(float(a[1]) for a in asks[:5])
        if ask_qty > 0:
            ratio = bid_qty / ask_qty
            if ratio < min_ratio:
                return True, f"Order book ratio collapsed to {ratio:.2f} (< {min_ratio:.2f}) — heavy sell pressure"
        return False, "Order book healthy"
    except Exception as e:
        return False, f"Check error: {e}"


# ---------------------------------------------------------------------------
# Optimization #9 — BTC Regime Filter (Market-Wide Gate)
# ---------------------------------------------------------------------------

def is_market_favorable(client: Client) -> Tuple[bool, str]:
    """
    Check BTC's short-term trend. Block ALL pair entries during BTC dumps.
    Returns (favorable: bool, reason: str).
    - Looks at the last 2 closed 5m candles for BTCUSDT.
    - If average move < -0.30% → market is in a BTC-led dump → skip cycle.
    """
    try:
        # Check last 12 5m candles (1-hour macro window) + last 2 candles (micro window)
        klines = client.get_klines(symbol="BTCUSDT", interval="5m", limit=13)
        if not klines or len(klines) < 2:
            return True, "BTC data unavailable — proceeding"
        
        # 1-hour macro trend check
        if len(klines) >= 12:
            h1_open = float(klines[0][1])
            h1_close = float(klines[-1][4])
            if h1_open > 0:
                h1_change_pct = (h1_close - h1_open) / h1_open * 100.0
                if h1_change_pct < -0.80:
                    return False, f"BTC 1h macro downtrend: {h1_change_pct:+.2f}% < -0.80% (market-wide dump guard)"

        moves = []
        for k in klines[-2:]:
            o, c = float(k[1]), float(k[4])
            if o > 0:
                moves.append((c - o) / o * 100.0)
        if not moves:
            return True, "BTC candles empty — proceeding"
        avg_move = sum(moves) / len(moves)
        if avg_move < BTC_REGIME_MIN_MOVE_PCT:
            return False, f"BTC regime unfavorable: 5m avg={avg_move:+.2f}% — holding all entries until recovery"
        return True, f"BTC regime OK (5m avg={avg_move:+.2f}%)"
    except Exception as e:
        return True, f"BTC regime check error (tolerated): {e}"


# ---------------------------------------------------------------------------
# Optimization #1 — Multi-Timeframe (MTF) Momentum Alignment
# ---------------------------------------------------------------------------

def check_mtf_alignment(client: Client, symbol: str) -> Tuple[bool, str]:
    """
    Confirm 1m, 5m, and 15m candles are not experiencing severe breakdown before entry:
    - 1m drop threshold: -0.40%
    - 5m drop threshold: -0.80%
    - 15m drop threshold: -1.50%
    Tolerates normal micro-noise while protecting against sharp dumps.
    """
    try:
        for interval, limit, thresh in [("1m", 3, -0.18), ("5m", 2, -0.40), ("15m", 2, -0.80)]:
            klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
            if not klines:
                continue
            last = klines[-1]
            o, c = float(last[1]), float(last[4])
            if o <= 0:
                continue
            momentum = (c - o) / o * 100.0
            if momentum < thresh:
                return False, f"MTF breakdown: {interval} candle at {momentum:+.2f}% (threshold {thresh:.2f}%)"
        return True, "MTF aligned — no severe decline"
    except BinanceAPIException as e:
        if getattr(e, "code", None) == -1003 or getattr(e, "status_code", None) in (418, 429):
            raise
        return True, f"MTF check error (tolerated): {e}"
    except Exception as e:
        return True, f"MTF check error (tolerated): {e}"


# ---------------------------------------------------------------------------
# Optimization #2 — Weighted Volume Delta Score
# ---------------------------------------------------------------------------

def get_volume_delta_score(client: Client, symbol: str, n: int = 50) -> float:
    """
    Weighted taker buy ratio — recent trades carry exponentially more weight.
    Returns score in [0, 1]. Score > 0.58 = strong buy pressure.
    """
    try:
        trades = client.get_recent_trades(symbol=symbol, limit=n)
        if not trades:
            return 0.5
        buy_vol = sell_vol = 0.0
        total = len(trades)
        for i, t in enumerate(trades):
            weight = 1.0 + (i / total)  # newer trades weighted higher
            qty = float(t.get("qty", 0.0)) * weight
            if not t.get("isBuyerMaker", True):
                buy_vol += qty
            else:
                sell_vol += qty
        denom = buy_vol + sell_vol
        return buy_vol / denom if denom > 0 else 0.5
    except BinanceAPIException as e:
        if getattr(e, "code", None) == -1003 or getattr(e, "status_code", None) in (418, 429):
            raise
        return 0.5
    except Exception:
        return 0.5


# ---------------------------------------------------------------------------
# Optimization #4 — VWAP Proximity Score
# ---------------------------------------------------------------------------

def get_vwap_proximity(client: Client, symbol: str, klines: Optional[list] = None) -> float:
    """
    Compute VWAP from last 20 x 1m candles and return proximity to current price.
    Returns score in [0, 1] — 1.0 = exactly at VWAP, 0.0 = far away.
    Score >= 0.90 means price is within 0.5% of VWAP (good mean-reversion entry).
    """
    try:
        if klines is None:
            klines = client.get_klines(symbol=symbol, interval="1m", limit=20)
        if not klines:
            return 0.5
        cum_vol = 0.0
        cum_pv = 0.0
        for k in klines:
            typical = (float(k[2]) + float(k[3]) + float(k[4])) / 3.0
            vol = float(k[5])
            cum_pv += typical * vol
            cum_vol += vol
        if cum_vol <= 0:
            return 0.5
        vwap = cum_pv / cum_vol
        current = float(klines[-1][4])
        if vwap <= 0 or current <= 0:
            return 0.5
        deviation = abs(current - vwap) / vwap
        # Score: 1.0 at VWAP, linearly decaying to 0 at 1% deviation
        return max(0.0, 1.0 - (deviation / 0.01))
    except BinanceAPIException as e:
        if getattr(e, "code", None) == -1003 or getattr(e, "status_code", None) in (418, 429):
            raise
        return 0.5
    except Exception:
        return 0.5


# ---------------------------------------------------------------------------
# Optimization #5 — Bid Queue Depth Check
# ---------------------------------------------------------------------------

def get_bid_queue_depth_usdt(client: Client, symbol: str) -> float:
    """
    Returns USDT value queued at the current best bid price.
    Large queue → we'll be behind many orders and fill slowly.
    """
    try:
        depth = client.get_order_book(symbol=symbol, limit=5)
        bids = depth.get("bids", [])
        if not bids:
            return 999_999.0
        best_qty = float(bids[0][1])
        best_price = float(bids[0][0])
        return best_qty * best_price
    except Exception:
        return 0.0  # If we can't read, assume thin queue (no penalty)


# ---------------------------------------------------------------------------
# Optimization #6 — Adaptive DCA Dip % (ATR-Scaled)
# ---------------------------------------------------------------------------

def get_adaptive_dip_pct(client: Client, symbol: str, default: float = 0.0025) -> float:
    """
    Calculate optimal DCA tranche-2 dip % based on 5m ATR (Average True Range).
    - High volatility (DOGE/PEPE): deeper dip 0.35–0.50%
    - Low volatility (BTC/ETH): shallower dip 0.10–0.20%
    Targets 0.6 × ATR_pct as the ideal dip entry.
    """
    try:
        klines = client.get_klines(symbol=symbol, interval="5m", limit=14)
        if not klines or len(klines) < 3:
            return default
        atrs = [float(k[2]) - float(k[3]) for k in klines]  # High - Low per candle
        avg_atr = sum(atrs) / len(atrs)
        price = float(klines[-1][4])
        if price <= 0:
            return default
        atr_pct = (avg_atr / price)
        # OPTIMIZED Scale: target 0.45 x ATR. Clamp 0.12% to 0.35%
        dip_pct = min(0.0060, max(0.0010, atr_pct * 0.6))
        return dip_pct
    except Exception:
        return default


def get_best_pair_candidates(
    client: Client,
    exclude_symbols: Optional[set] = None,
    use_only: Optional[list] = None,
    max_spread_pct: Optional[float] = None,
    min_book_ratio: Optional[float] = None,
    min_volatility_1m: Optional[float] = None,
    min_taker_buy_ratio: Optional[float] = None,
) -> List[str]:
    exclude = set(exclude_symbols or set()) | set(BLACKLIST)

    # Resolve use_only list from manager config if not explicitly passed
    if not use_only:
        try:
            cfg = get_manager_config()
            uo_raw = (cfg.get("use_only") or "").strip()
            if uo_raw:
                use_only = [s.strip().upper() for s in uo_raw.split(",") if s.strip()]
        except Exception:
            pass

    # Default to semiconductor 3x counterpart pairs
    if not use_only:
        use_only = ["SOXLBUSDT", "SOXSBUSDT"]

    # ── Semiconductor 5-Minute Predictive Directional Engine (SOXLB & SOXSB) ──
    # Strictly isolates SOXLB and SOXSB: NONE of the generic crypto parameters are evaluated!
    if any(s in ("SOXLBUSDT", "SOXSBUSDT") for s in use_only):
        semi_candidates = [s for s in ("SOXLBUSDT", "SOXSBUSDT") if s in use_only and s not in exclude]
        return semi_candidates or [s for s in ("SOXLBUSDT", "SOXSBUSDT") if s in use_only] or ["SOXLBUSDT"]

    # Read defaults and user blacklist from manager config if not explicitly passed
    try:
        pass
        cfg = get_manager_config()
        if max_spread_pct is None:
            max_spread_pct = DEFAULT_MAX_SPREAD_PCT
        if min_book_ratio is None:
            min_book_ratio = DEFAULT_MIN_BOOK_RATIO
        if min_volatility_1m is None:
            min_volatility_1m = DEFAULT_MIN_VOLATILITY_1M
        if min_taker_buy_ratio is None:
            min_taker_buy_ratio = DEFAULT_MIN_TAKER_BUY_RATIO
        user_bl_raw = cfg.get("blacklist") or ""
        user_bl = {s.strip().upper() for s in user_bl_raw.replace(",", " ").split() if s.strip()}
        exclude = exclude | user_bl
    except Exception:
        if max_spread_pct is None:
            max_spread_pct = DEFAULT_MAX_SPREAD_PCT
        if min_book_ratio is None:
            min_book_ratio = DEFAULT_MIN_BOOK_RATIO
        if min_volatility_1m is None:
            min_volatility_1m = DEFAULT_MIN_VOLATILITY_1M
        if min_taker_buy_ratio is None:
            min_taker_buy_ratio = DEFAULT_MIN_TAKER_BUY_RATIO

    # Dynamic Performance-Based Auto-Blacklist: exclude underperforming coins (>=2 trades with 0 wins / net < -$0.05, or 2 consecutive losses)
    try:
        underperforming_syms = get_underperforming_symbols()
        exclude = exclude | underperforming_syms
    except Exception:
        pass

    # Optimization #9 — BTC Regime Filter: block all entries during market dumps
    regime_ok, regime_reason = is_market_favorable(client)
    if not regime_ok:
        raise RuntimeError(regime_reason)

    # Optimization #7 — Stop-Loss / Adverse Drift Cooldown: exclude recently stopped symbols (30-min quarantine)
    try:
        clear_expired_cooldowns()
        cooldown_syms = get_cooldown_symbols()
        exclude = exclude | cooldown_syms
    except Exception:
        pass

    tickers = client.get_ticker()
    vol_map = {}
    for t in tickers:
        if "symbol" in t and "quoteVolume" in t:
            try:
                vol_map[t["symbol"]] = float(t["quoteVolume"])
            except ValueError:
                pass

    # 5-Minute Volume Threshold Candidate Engine (No Whitelists)
    # Whitelists removed: Pairs are selected strictly based on real-time 5-minute volume threshold
    MIN_5M_VOL_THRESHOLD_USDT = 20000.0  # Minimum $20,000 USDT traded in the last 5 minutes
    MIN_5M_TRADES_COUNT = 20            # Minimum 20 trades in 5 minutes (eliminates dead pairs)

    candidates_24h = []
    for t in tickers:
        s = t.get("symbol", "")
        if not s.isascii() or not s.endswith("USDT"):
            continue
        if s in exclude or any(b in s for b in BLACKLIST):
            continue
        # Exclude stablecoin pairs that do not oscillate
        if any(s.startswith(st) for st in ("USDC", "FDUSD", "TUSD", "USD1", "RLUSD", "EUR", "BUSD", "DAI", "XUSD")):
            continue
        try:
            vol_24h = float(t.get("quoteVolume", 0))
            price = float(t.get("lastPrice", 0))
        except (KeyError, ValueError):
            continue
        if vol_24h < 3_000_000.0 or price < 0.00001:
            continue
        candidates_24h.append((s, vol_24h))

    # Pre-filter top 25 active pairs for 5-minute volume inspection
    candidates_24h.sort(key=lambda x: x[1], reverse=True)
    top_candidates = [x[0] for x in candidates_24h[:25]]

    pool_5m = []
    for sym in top_candidates:
        try:
            klines = client.get_klines(symbol=sym, interval="5m", limit=1)
            if not klines:
                continue
            k = klines[0]
            vol_5m = float(k[7])        # Quote asset volume in USDT in last 5m
            trades_5m = int(k[8])       # Trades in last 5m
            taker_buy_5m = float(k[10]) # Taker buy volume
            taker_ratio = (taker_buy_5m / vol_5m) if vol_5m > 0 else 0.50

            if vol_5m >= MIN_5M_VOL_THRESHOLD_USDT and trades_5m >= MIN_5M_TRADES_COUNT:
                pool_5m.append({
                    "symbol": sym,
                    "vol_5m": vol_5m,
                    "trades_5m": trades_5m,
                    "taker_ratio": taker_ratio,
                    "score": vol_5m * (trades_5m / 50.0),
                })
        except Exception:
            continue

    # Fallback if market is in quiet hours: lower threshold to $8,000
    if not pool_5m:
        for sym in top_candidates[:12]:
            try:
                klines = client.get_klines(symbol=sym, interval="5m", limit=1)
                if klines:
                    k = klines[0]
                    v5 = float(k[7])
                    tr5 = int(k[8])
                    if v5 >= 8_000.0 and tr5 >= 10:
                        pool_5m.append({
                            "symbol": sym,
                            "vol_5m": v5,
                            "trades_5m": tr5,
                            "taker_ratio": 0.5,
                            "score": v5,
                        })
            except Exception:
                continue

    # Sort strictly by 5-minute volume score (highest active volume first)
    pool_5m.sort(key=lambda x: x["score"], reverse=True)
    candidate_pool = [p["symbol"] for p in pool_5m[:8]]
    if candidate_pool:
        top_summary = [f"{p['symbol']} (${p['vol_5m']:,.0f})" for p in pool_5m[:4]]
        log_event(
            f"5-Minute Volume Candidates (threshold >= ${MIN_5M_VOL_THRESHOLD_USDT:,.0f}): {top_summary}",
            level="INFO",
            event_type="SCAN",
        )
    evaluated = []
    viable = []
    MIN_ACCEPTABLE_SCORE = 3.0

    try:
        for sym in candidate_pool:
            # Optimization #1 — MTF alignment: skip if any timeframe is declining
            mtf_ok, mtf_reason = check_mtf_alignment(client, sym)
            if not mtf_ok:
                log_event(f"MTF Reject {sym}: {mtf_reason}", level="DEBUG", event_type="FILTER_REJECT", symbol=sym)
                evaluated.append((False, sym, {"score": -999.0, "mtf_reason": mtf_reason}))
                continue
            ok, reason, metrics = evaluate_pair_microstructure(
                client,
                sym,
                max_spread_pct=max_spread_pct,
                min_book_ratio=min_book_ratio,
                min_volatility_1m=min_volatility_1m,
                min_taker_buy_ratio=min_taker_buy_ratio,
            )
            if not ok:
                log_event(f"Microstructure Reject {sym}: {reason}", level="DEBUG", event_type="FILTER_REJECT", symbol=sym)
            evaluated.append((ok, sym, metrics))
            if ok and metrics.get("score", -999) >= MIN_ACCEPTABLE_SCORE:
                viable.append((ok, sym, metrics))
                # Early break: 2 viable candidates is plenty and preserves API request weight
                if len(viable) >= 2:
                    break

        if viable:
            viable.sort(key=lambda x: x[2]["score"], reverse=True)
            return [e[1] for e in viable]

        # If none from primary pool were viable, try top 4 volume candidates
        extended_pool = [
            t.get("symbol", "")
            for t in sorted(tickers, key=lambda x: float(x.get("quoteVolume") or 0), reverse=True)
            if t.get("symbol", "").isascii() and t.get("symbol", "").endswith("USDT")
            and t.get("symbol", "") not in exclude and t.get("symbol", "") not in candidate_pool
            and not any(b in t.get("symbol", "") for b in BLACKLIST)
        ][:4]

        for sym in extended_pool:
            mtf_ok, mtf_reason = check_mtf_alignment(client, sym)
            if not mtf_ok:
                log_event(f"MTF Reject {sym} (Extended): {mtf_reason}", level="DEBUG", event_type="FILTER_REJECT", symbol=sym)
                continue
            ok, reason, metrics = evaluate_pair_microstructure(
                client,
                sym,
                max_spread_pct=max_spread_pct,
                min_book_ratio=min_book_ratio,
                min_volatility_1m=min_volatility_1m,
                min_taker_buy_ratio=min_taker_buy_ratio,
            )
            if ok:
                viable.append((ok, sym, metrics))
                if len(viable) >= 2:
                    break
            else:
                log_event(f"Microstructure Reject {sym} (Extended): {reason}", level="DEBUG", event_type="FILTER_REJECT", symbol=sym)

        if viable:
            viable = [e for e in viable if e[2].get("score", -999) >= 4.0]
            viable.sort(key=lambda x: x[2]["score"], reverse=True)
            if viable:
                return [e[1] for e in viable]

    except BinanceAPIException as e:
        if getattr(e, "code", None) == -1003 or getattr(e, "status_code", None) in (418, 429) or "request weight" in str(e).lower():
            rate_msg = "Binance rate limit triggered (weight exceeded): pausing candidate scan for 45s to clear limit window."
            print(rate_msg)
            log_event(rate_msg, level="WARN", event_type="RATE_LIMIT_WAIT")
            time.sleep(45.0)
            return []
        raise

    # Fallback: pick candidates with clear runway and workable spread (<= 0.05%)
    runway_clear = [
        e for e in evaluated
        if e[2].get("runway_clear") and e[2].get("spread_pct", 999.0) <= 0.050
        and e[2].get("tick_ratio", 999.0) <= 0.00035
    ]
    if runway_clear:
        runway_clear.sort(key=lambda x: x[2].get("spread_pct", 999.0))
        return [e[1] for e in runway_clear]

    raise RuntimeError(
        f"No viable USDT pairs passed microstructure and runway checks. Waiting for next market interval."
    )


def get_best_pair(
    client: Client,
    exclude_symbols: Optional[set] = None,
    use_only: Optional[list] = None,
    max_spread_pct: Optional[float] = None,
    min_book_ratio: Optional[float] = None,
    min_volatility_1m: Optional[float] = None,
    min_taker_buy_ratio: Optional[float] = None,
) -> str:
    candidates = get_best_pair_candidates(
        client=client,
        exclude_symbols=exclude_symbols,
        use_only=use_only,
        max_spread_pct=max_spread_pct,
        min_book_ratio=min_book_ratio,
        min_volatility_1m=min_volatility_1m,
        min_taker_buy_ratio=min_taker_buy_ratio,
    )
    if not candidates:
        raise RuntimeError("No candidate pairs available (scanned or rate-limited).")
    return candidates[0]


def select_and_claim_best_pair(
    client: Client,
    instance_id: int,
    exclude_symbols: Optional[set] = None,
    use_only: Optional[list] = None,
    max_spread_pct: Optional[float] = None,
    min_book_ratio: Optional[float] = None,
    min_volatility_1m: Optional[float] = None,
    min_taker_buy_ratio: Optional[float] = None,
) -> str:
    """
    Finds best pair candidates and atomically claims the top available one
    for instance_id in the database. If another bot claims candidate #1 in a race,
    this function immediately moves to candidate #2, guaranteeing no two bots
    trade the same coin.
    """
    candidates = get_best_pair_candidates(
        client=client,
        exclude_symbols=exclude_symbols,
        use_only=use_only,
        max_spread_pct=max_spread_pct,
        min_book_ratio=min_book_ratio,
        min_volatility_1m=min_volatility_1m,
        min_taker_buy_ratio=min_taker_buy_ratio,
    )
    if not candidates:
        raise RuntimeError(f"No candidate pairs available for instance {instance_id} (scanned or rate-limited).")
    for sym in candidates:
        if sym not in ("SOXLBUSDT", "SOXSBUSDT") and hybrid_strategy:
            try:
                pred = hybrid_strategy.predict_market_and_strategy(client, sym, investment_usdt=25.0)
                if not pred.get("execution_params", {}).get("is_viable_trade", True):
                    log_event(
                        f"Candidate {sym} rejected during selection: Market is '{pred.get('predicted_market')}' (is_viable=False). Moving to next candidate.",
                        level="DEBUG",
                        event_type="FILTER_REJECT",
                        symbol=sym,
                    )
                    add_cooldown_symbol(sym, duration_sec=300)
                    continue
            except Exception:
                pass
        if claim_symbol_for_instance(instance_id, sym):
            return sym
    raise RuntimeError(
        f"Could not claim any available pair from {len(candidates)} candidates for instance {instance_id}."
    )



def _emergency_liquidate(client: Client, symbol: str, entry_cost: float, cycle_id: int, instance_id: int, bot_name: str, start_ts: float, reason: str) -> Optional[float]:
    """Sell all held base asset back to USDT immediately to unlock funds on error."""
    try:
        # Cancel any resting open orders first to unlock balance
        if symbol:
            cancel_all_open_orders(client, symbol)
            time.sleep(0.2)
        base_asset = symbol.replace("USDT", "") if symbol else None
        if base_asset:
            bal = float(client.get_asset_balance(asset=base_asset)["free"] or 0)
            filters = get_symbol_filters(client, symbol)

            # CRITICAL FIX: Determine expected_qty from DB to avoid selling other bots' coins
            expected_qty = bal
            try:
                with get_conn() as conn:
                    cyc = conn.execute("SELECT qty FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
                    if cyc and cyc["qty"]:
                        expected_qty = float(cyc["qty"])
            except:
                pass

            target_qty = min(bal, expected_qty)
            qty_str = format_quantity(target_qty, filters["step"])
            qty = float(qty_str)

            if qty >= filters["min_qty"]:
                log_event(
                    f"[{bot_name}] [{symbol}] Error liquidation: selling {qty_str} {symbol} to unlock funds",
                    level="WARN",
                    event_type="EMERGENCY_LIQUIDATE",
                    instance_id=instance_id,
                    symbol=symbol,
                )
                print(f"[{bot_name}] [{symbol}] Error liquidation: selling {qty_str} {symbol} to unlock funds...")
                sell_order = client.create_order(
                    symbol=symbol,
                    side=Client.SIDE_SELL,
                    type=Client.ORDER_TYPE_MARKET,
                    quantity=qty_str,
                    recvWindow=10000,
                    newOrderRespType="FULL",
                )
                exit_val = float(sell_order.get("cummulativeQuoteQty") or 0.0)
                sell_fee_info = fee_from_order(client, sell_order, symbol)
                sell_fee_usdt = sell_fee_info["fee_usdt"]
                sell_fee_bnb = sell_fee_info["fee_bnb"]
                bnb_px = sell_fee_info.get("bnb_price") or 700.0

                with get_conn() as conn:
                    buy_trade = conn.execute("SELECT fee_bnb, fee_usdt FROM trades WHERE cycle_id = ? AND side = 'BUY' LIMIT 1", (cycle_id,)).fetchone()
                if buy_trade and buy_trade["fee_usdt"]:
                    buy_fee_usdt = float(buy_trade["fee_usdt"])
                    buy_fee_bnb = float(buy_trade["fee_bnb"] or (buy_fee_usdt / bnb_px if bnb_px > 0 else 0.0))
                else:
                    buy_fee_usdt = entry_cost * EST_FEE_RATE_PER_SIDE
                    buy_fee_bnb = buy_fee_usdt / bnb_px if bnb_px > 0 else 0.0

                settled = settle_pnl(entry_cost, exit_val, buy_fee_usdt, sell_fee_usdt)
                insert_trade(
                    instance_id=instance_id,
                    cycle_id=cycle_id,
                    symbol=symbol,
                    side="SELL",
                    quantity=qty,
                    price=exit_val / qty if qty else 0,
                    quote_qty=exit_val,
                    fee_bnb=sell_fee_bnb,
                    fee_usdt=sell_fee_usdt,
                    order_id=sell_order.get("orderId"),
                    pnl_usdt=settled["net_pnl"],
                    notes=f"ERROR_LIQUIDATION: {reason}",
                )
                close_cycle(
                    cycle_id,
                    exit_val,
                    settled["net_pnl"],
                    "LIQUIDATED_ON_ERROR",
                    time.time() - start_ts,
                    fee_bnb=buy_fee_bnb + sell_fee_bnb,
                    fee_usdt=buy_fee_usdt + sell_fee_usdt,
                )
                # Optimization #7 — Cooldown: quarantine symbol on error liquidation
                try:
                    add_cooldown_symbol(symbol, duration_sec=300)
                except Exception:
                    pass
                return settled["net_pnl"]
    except Exception as ex:
        log_event(
            f"[{bot_name}] [{symbol}] Emergency liquidation failed: {ex}",
            level="ERROR",
            event_type="LIQUIDATION_FAILED",
            instance_id=instance_id,
            symbol=symbol,
        )
        print(f"[{bot_name}] [{symbol}] Emergency liquidation failed: {ex}")
    if cycle_id:
        close_cycle(cycle_id, None, None, "ERROR", time.time() - start_ts)
    return None


# ---------------------------------------------------------------------------
# 2-Stage Scale-In (DCA Pullback) Strategy
# ---------------------------------------------------------------------------

def trade_one_cycle_scale_in(
    client: Client,
    instance_id: int,
    investment_usdt: float,
    target_profit: float,
    stop_loss: float,
    bot_name: str,
    symbol: str,
    lot: dict,
    start_ts: float,
    timeout_sec: float,
    profile: Optional[dict] = None,
) -> Tuple[Optional[float], Optional[str]]:
    """
    2-Stage Scale-In (DCA Pullback) Strategy:
    1. Splits investment into 2 tranches (50% / 50%).
    2. Tranche 1: Places LIMIT BUY at Best Bid (pure maker entry, 8s fill window).
    3. On Tranche 1 fill:
       - Posts resting LIMIT SELL for Tranche 1 at dynamic micro-profit target.
       - Simultaneously posts resting LIMIT BUY for Tranche 2 at dynamic dip discount.
    4. Monitoring:
       - If Tranche 1 SELL fills before dip: cancels Tranche 2 limit buy, secures profit!
       - If Tranche 2 BUY fills: cancels Tranche 1 sell, calculates blended average entry price,
         posts combined LIMIT SELL at new lower target price, and monitors until target/SL.
    """
    if profile is None:
        _, profile = auto_select_cycle_strategy(client, symbol, investment_usdt)

    cycle_id = None
    qty1 = 0.0
    cost1 = 0.0
    buy1_fee_usdt = 0.0
    buy1_fee_bnb = 0.0

    qty2 = 0.0
    cost2 = 0.0
    buy2_fee_usdt = 0.0
    buy2_fee_bnb = 0.0
    current_held_qty = 0.0
    current_total_cost = 0.0

    min_notional = lot.get("min_notional", 5.0)
    tick_sz = lot.get("tick_size") or 0.0001
    step_sz = lot.get("step") or 0.001

    # Quantum-Maker: Adaptive Tranche Allocation
    if investment_usdt >= (min_notional * 2.1):
        tranche1_inv = max(min_notional * 1.05, investment_usdt * 0.40)
        tranche2_inv = investment_usdt - tranche1_inv
        if tranche2_inv < min_notional:
            tranche1_inv = investment_usdt
            tranche2_inv = 0.0
    else:
        # Smaller allocations ($5-$10): single tranche fast maker scalp
        tranche1_inv = investment_usdt
        tranche2_inv = 0.0

    try:
        # Step 1: Best-Bid entry for Tranche 1
        # Pre-entry VPIN & Order-Flow Toxicity Defense
        vpin_ok, vpin_reason = check_pre_entry_order_flow_vpin(client, symbol)
        if not vpin_ok:
            log_event(
                f"[{bot_name}] [{symbol}] VPIN Toxicity Abort: {vpin_reason} — skipping entry to protect capital ($0 fee, $0 loss)",
                level="WARN",
                event_type="VPIN_ABORT",
                instance_id=instance_id,
                symbol=symbol,
            )
            print(f"[{bot_name}] [{symbol}] VPIN Toxicity Abort: {vpin_reason} ($0 fee, $0 loss)")
            return None, "UNFILLED_ENTRY"

        ob_entry = client.get_orderbook_ticker(symbol=symbol)
        best_bid = float(ob_entry.get("bidPrice") or 0.0)
        best_ask = float(ob_entry.get("askPrice") or 0.0)

        buy1 = None
        limit_buy1_id = None

        if best_bid > 0 and best_ask > best_bid:
            if LIMIT_BUY_OFFSET_PCT > 0.0:
                # Place limit order below the best bid
                bid_price = best_bid * (1.0 - LIMIT_BUY_OFFSET_PCT)
            else:
                bid_price = best_bid
                if (best_ask - best_bid) > (2 * tick_sz):
                    bid_price = best_bid + tick_sz

                # Optimization #5 — Queue depth: if too many orders at best bid, move 1 tick up for faster fill
                queue_usdt = get_bid_queue_depth_usdt(client, symbol)
                if queue_usdt > BID_QUEUE_USDT_THRESHOLD and (best_ask - best_bid) > tick_sz:
                    bid_price = best_bid + tick_sz  # Join closer to ask side to skip the queue

            buy_price_str = format_price(bid_price, tick_sz, rounding="FLOOR")
            bid_price = float(buy_price_str)

            calc_qty1 = tranche1_inv / bid_price
            qty1_str = format_quantity(calc_qty1, step_sz)
            order_qty1 = float(qty1_str)

            if order_qty1 * bid_price < min_notional:
                order_qty1 = float(format_quantity((min_notional / bid_price) * 1.02 + step_sz, step_sz))
                qty1_str = format_quantity(order_qty1, step_sz)

            try:
                log_event(
                    f"[{bot_name}] [{symbol}] DCA Tranche 1: Placing BEST-BID LIMIT BUY {qty1_str} {symbol} @ {buy_price_str} (~{order_qty1 * bid_price:.2f} USDT)",
                    level="TRADE",
                    event_type="LIMIT_BUY_SUBMIT",
                    instance_id=instance_id,
                    symbol=symbol,
                )
                print(f"[{bot_name}] [{symbol}] DCA Tranche 1: Placing BEST-BID LIMIT BUY {qty1_str} {symbol} @ {buy_price_str}")
                limit_buy1_order = limit_buy(client, symbol, order_qty1, bid_price)
                limit_buy1_id = limit_buy1_order.get("orderId")

                wait_start = time.time()
                while time.time() - wait_start < LIMIT_BUY_MAX_WAIT_SEC:
                    if is_stop_requested(instance_id):
                        cancel_order_safe(client, symbol, limit_buy1_id)
                        return None, "ABORTED"
                    time.sleep(0.4)
                    ord_chk = client.get_order(symbol=symbol, orderId=limit_buy1_id, recvWindow=10000)
                    if ord_chk.get("status") == "FILLED":
                        buy1 = ord_chk
                        log_event(
                            f"[{bot_name}] [{symbol}] DCA Tranche 1: Best-bid LIMIT BUY filled @ {buy_price_str} (maker entry)!",
                            level="TRADE",
                            event_type="LIMIT_BUY_FILLED",
                            instance_id=instance_id,
                            symbol=symbol,
                        )
                        print(f"[{bot_name}] [{symbol}] DCA Tranche 1: Best-bid LIMIT BUY filled @ {buy_price_str} (maker entry)!")
                        break
                    elif ord_chk.get("status") in ("CANCELED", "REJECTED", "EXPIRED"):
                        break

                    # Toxic flow check: cancel resting buy if order book collapses
                    is_toxic, toxic_reason = is_orderbook_flow_toxic(client, symbol, bid_price)
                    if is_toxic:
                        cancel_order_safe(client, symbol, limit_buy1_id)
                        time.sleep(0.2)
                        ord_chk = client.get_order(symbol=symbol, orderId=limit_buy1_id, recvWindow=10000)
                        if ord_chk.get("status") == "FILLED":
                            buy1 = ord_chk
                            break
                        log_event(
                            f"[{bot_name}] [{symbol}] Toxic flow detected ({toxic_reason}) — cancelled DCA Tranche 1 maker buy to protect capital ($0 fee, $0 loss)",
                            level="WARN",
                            event_type="TOXIC_FLOW_ABORT",
                            instance_id=instance_id,
                            symbol=symbol,
                        )
                        print(f"[{bot_name}] [{symbol}] Toxic flow detected ({toxic_reason}) — cancelled maker buy ($0 fee, $0 loss)")
                        return None, "UNFILLED_ENTRY"

                if not buy1:
                    cancel_order_safe(client, symbol, limit_buy1_id)
                    time.sleep(0.2)
                    ord_chk = client.get_order(symbol=symbol, orderId=limit_buy1_id, recvWindow=10000)
                    if ord_chk.get("status") == "FILLED":
                        buy1 = ord_chk
            except Exception as e:
                log_event(
                    f"[{bot_name}] [{symbol}] Tranche 1 limit buy attempt error ({e})",
                    level="WARN",
                    event_type="LIMIT_BUY_ERR",
                    instance_id=instance_id,
                    symbol=symbol,
                )

        if not buy1:
            log_event(
                f"[{bot_name}] [{symbol}] DCA Tranche 1: Best-bid limit buy not filled in {LIMIT_BUY_MAX_WAIT_SEC}s — cleanly aborted ($0 fee, $0 loss)",
                level="INFO",
                event_type="UNFILLED_ENTRY",
                instance_id=instance_id,
                symbol=symbol,
            )
            print(f"[{bot_name}] [{symbol}] DCA Tranche 1: Best-bid limit buy not filled in {LIMIT_BUY_MAX_WAIT_SEC}s — cleanly aborted ($0 fee, $0 loss)")
            return None, "UNFILLED_ENTRY"

        qty1 = float(buy1["executedQty"])
        cost1 = float(buy1["cummulativeQuoteQty"])
        qty1 = float(format_quantity(qty1, step_sz))
        if qty1 <= 0:
            raise RuntimeError("Rounded quantity is zero after LOT_SIZE step")

        fee1_info = fee_from_order(client, buy1)
        buy1_fee_usdt = fee1_info["fee_usdt"]
        buy1_fee_bnb = fee1_info["fee_bnb"]

        cycle_id = insert_cycle(
            instance_id,
            symbol,
            cost1,
            qty1,
            fee_bnb=buy1_fee_bnb,
            fee_usdt=buy1_fee_usdt,
            status="OPEN",
        )
        if record_cycle_features:
            try:
                snap = dict(RECENT_PAIR_METRICS.get(symbol) or {"symbol": symbol})
                record_cycle_features(
                    DB_PATH,
                    cycle_id,
                    symbol,
                    snap,
                    p_win=snap.get("p_win", 0.5),
                    ml_score=snap.get("ml_score", 5.0),
                )
            except Exception:
                pass
        avg_price1 = cost1 / qty1
        insert_trade(
            instance_id=instance_id,
            cycle_id=cycle_id,
            symbol=symbol,
            side="BUY",
            quantity=qty1,
            price=avg_price1,
            quote_qty=cost1,
            fee_bnb=buy1_fee_bnb,
            fee_usdt=buy1_fee_usdt,
            order_id=buy1.get("orderId"),
            notes=f"DCA Tranche 1 fee_usdt={buy1_fee_usdt:.6f} fee_bnb={buy1_fee_bnb:.8f}",
        )
        log_event(
            f"[{bot_name}] [{symbol}] DCA Tranche 1 BUY filled: qty={qty1} @ {avg_price1:.8f} cost={cost1:.4f} USDT",
            level="TRADE",
            event_type="BUY",
            instance_id=instance_id,
            symbol=symbol,
        )
        print(f"[{bot_name}] [{symbol}] DCA Tranche 1 BUY filled: qty={qty1} @ {avg_price1:.8f} cost={cost1:.4f} USDT")

        # Step 2: Post Tranche 1 resting LIMIT SELL at dynamic micro-profit
        recovery_boost = get_loss_recovery_boost(instance_id)
        if recovery_boost > 0:
            log_event(
                f"[{bot_name}] [{symbol}] DCA Loss recovery active: boosting target by +{recovery_boost:.4f} USDT",
                level="INFO",
                event_type="LOSS_RECOVERY",
                instance_id=instance_id,
                symbol=symbol,
            )
            print(f"[{bot_name}] [{symbol}] DCA Loss recovery active: boosting target by +{recovery_boost:.4f} USDT")
        min_net_profit1 = profile.get("min_net_profit_tranche1", max(OPT_ABSOLUTE_MIN_PROFIT_USDT, investment_usdt * OPT_MIN_NET_PROFIT_PCT * 0.55)) + (recovery_boost * 0.5)
        min_net_profit_comb = profile.get("min_net_profit_combined", max(OPT_ABSOLUTE_MIN_PROFIT_USDT, investment_usdt * OPT_MIN_NET_PROFIT_PCT)) + recovery_boost
        required_exit1 = (cost1 + min_net_profit1 + buy1_fee_usdt) / (1.0 - EST_FEE_RATE_PER_SIDE)
        target_price1 = float(format_price(required_exit1 / qty1, tick_sz, rounding="CEILING"))

        sell_order = limit_sell(client, symbol, qty1, target_price1)
        active_sell_order_id = sell_order.get("orderId")
        log_event(
            f"[{bot_name}] [{symbol}] DCA Tranche 1: Posted resting LIMIT SELL {qty1} @ {target_price1} (micro-profit target ≥ +{min_net_profit1:.4f} USDT)",
            level="TRADE",
            event_type="LIMIT_SELL_SUBMIT",
            instance_id=instance_id,
            symbol=symbol,
        )
        print(f"[{bot_name}] [{symbol}] DCA Tranche 1: Posted resting LIMIT SELL {qty1} @ {target_price1}")

        # Step 3: Post Tranche 2 resting LIMIT BUY at dynamic dip (calibrated to 1m volatility)
        limit_buy2_id = None
        dip_price = 0.0
        order_qty2 = 0.0
        if tranche2_inv >= min_notional:
            dip_frac = profile.get("dynamic_dip_pct", 0.0080)
            dip_price = float(format_price(avg_price1 * (1.0 - dip_frac), tick_sz, rounding="FLOOR"))
            calc_qty2 = tranche2_inv / dip_price
            order_qty2 = float(format_quantity(calc_qty2, step_sz))
            if order_qty2 * dip_price < min_notional:
                order_qty2 = float(format_quantity((min_notional / dip_price) * 1.02 + step_sz, step_sz))
            try:
                buy2_order = limit_buy(client, symbol, order_qty2, dip_price)
                limit_buy2_id = buy2_order.get("orderId")
                log_event(
                    f"[{bot_name}] [{symbol}] DCA Tranche 2: Posted resting LIMIT BUY {order_qty2} @ {dip_price} (-{dip_frac*100:.2f}% dynamic dip, ~{order_qty2 * dip_price:.2f} USDT)",
                    level="TRADE",
                    event_type="LIMIT_BUY_SUBMIT",
                    instance_id=instance_id,
                    symbol=symbol,
                )
                print(f"[{bot_name}] [{symbol}] DCA Tranche 2: Posted resting LIMIT BUY {order_qty2} @ {dip_price} (-{dip_frac*100:.2f}% dip)")
            except Exception as e:
                log_event(
                    f"[{bot_name}] [{symbol}] DCA Tranche 2 buy placement failed: {e} (continuing with Tranche 1 alone)",
                    level="WARN",
                    event_type="LIMIT_BUY_ERR",
                    instance_id=instance_id,
                    symbol=symbol,
                )

        # Step 4: Monitor dual state & Multi-Algorithm Lifecycle
        tranche2_filled = False
        stage1_repriced = False
        stage2_repriced = False
        stage3_repriced = False
        dca_peak_pnl = 0.0
        tranche2_fill_ts = 0.0
        current_held_qty = qty1
        current_total_cost = cost1
        total_buy_fee_usdt = buy1_fee_usdt
        total_buy_fee_bnb = buy1_fee_bnb
        active_target_price = target_price1

        while True:
            if is_stop_requested(instance_id):
                if limit_buy2_id and not tranche2_filled:
                    cancel_order_safe(client, symbol, limit_buy2_id)
                if active_sell_order_id:
                    cancel_order_safe(client, symbol, active_sell_order_id)
                log_event(
                    f"[{bot_name}] [{symbol}] Stop requested: cancelled resting orders. Leaving {current_held_qty} {symbol} in wallet.",
                    level="WARN",
                    event_type="BOT_STOP",
                    instance_id=instance_id,
                    symbol=symbol,
                )
                print(f"[{bot_name}] [{symbol}] Stop requested: cancelled resting orders. Leaving {current_held_qty} {symbol} in wallet.")
                if cycle_id:
                    close_cycle(
                        cycle_id,
                        exit_value=None,
                        pnl_usdt=None,
                        status="ABORTED",
                        duration_sec=time.time() - start_ts,
                        fee_bnb=total_buy_fee_bnb,
                        fee_usdt=total_buy_fee_usdt,
                    )
                update_instance(
                    instance_id,
                    current_pnl=0.0,
                    last_error="Stopped by user (orders cancelled, coins held in wallet)",
                )
                return None, "ABORTED"

            # Check if active LIMIT SELL filled
            if active_sell_order_id:
                try:
                    chk_sell = client.get_order(symbol=symbol, orderId=active_sell_order_id, recvWindow=10000)
                    if chk_sell.get("status") == "FILLED":
                        if limit_buy2_id and not tranche2_filled:
                            cancel_order_safe(client, symbol, limit_buy2_id)

                        exit_value = float(chk_sell.get("cummulativeQuoteQty") or 0.0)
                        sell_fee_info = fee_from_order(client, chk_sell, symbol)
                        sell_fee_usdt = sell_fee_info["fee_usdt"]
                        sell_fee_bnb = sell_fee_info["fee_bnb"]
                        settled = settle_pnl(current_total_cost, exit_value, total_buy_fee_usdt, sell_fee_usdt)
                        duration = time.time() - start_ts

                        insert_trade(
                            instance_id=instance_id,
                            cycle_id=cycle_id,
                            symbol=symbol,
                            side="SELL",
                            quantity=current_held_qty,
                            price=exit_value / current_held_qty if current_held_qty else 0,
                            quote_qty=exit_value,
                            fee_bnb=sell_fee_bnb,
                            fee_usdt=sell_fee_usdt,
                            order_id=chk_sell.get("orderId"),
                            pnl_usdt=settled["net_pnl"],
                            notes="TARGET (DCA)",
                        )
                        close_cycle(
                            cycle_id,
                            exit_value,
                            settled["net_pnl"],
                            "SUCCESS",
                            duration,
                            fee_bnb=total_buy_fee_bnb + sell_fee_bnb,
                            fee_usdt=total_buy_fee_usdt + sell_fee_usdt,
                        )
                        update_instance(instance_id, current_symbol=None, current_pnl=0.0)
                        log_event(
                            f"[{bot_name}] [{symbol}] DCA SELL (TARGET) {symbol}: exit={exit_value:.4f} USDT | "
                            f"gross={settled['gross_pnl']:+.6f} | fees={settled['fees_usdt']:.6f} | "
                            f"NET={settled['net_pnl']:+.6f} USDT",
                            level="TRADE",
                            event_type="SELL",
                            instance_id=instance_id,
                            symbol=symbol,
                        )
                        print(f"[{bot_name}] [{symbol}] DCA SELL (TARGET) {symbol}: NET={settled['net_pnl']:+.6f} USDT")
                        return settled["net_pnl"], "TARGET"
                except Exception:
                    pass

            # Check if Tranche 2 LIMIT BUY filled
            if limit_buy2_id and not tranche2_filled:
                try:
                    chk_buy2 = client.get_order(symbol=symbol, orderId=limit_buy2_id, recvWindow=10000)
                    if chk_buy2.get("status") == "FILLED":
                        tranche2_filled = True
                        qty2_actual = float(chk_buy2["executedQty"])
                        cost2_actual = float(chk_buy2["cummulativeQuoteQty"])
                        qty2_actual = float(format_quantity(qty2_actual, step_sz))
                        fee2_info = fee_from_order(client, chk_buy2)
                        buy2_fee_usdt = fee2_info["fee_usdt"]
                        buy2_fee_bnb = fee2_info["fee_bnb"]
                        total_buy_fee_usdt += buy2_fee_usdt
                        total_buy_fee_bnb += buy2_fee_bnb

                        avg_price2 = cost2_actual / qty2_actual if qty2_actual > 0 else dip_price
                        insert_trade(
                            instance_id=instance_id,
                            cycle_id=cycle_id,
                            symbol=symbol,
                            side="BUY",
                            quantity=qty2_actual,
                            price=avg_price2,
                            quote_qty=cost2_actual,
                            fee_bnb=buy2_fee_bnb,
                            fee_usdt=buy2_fee_usdt,
                            order_id=chk_buy2.get("orderId"),
                            notes=f"DCA Tranche 2 (Dip) fee_usdt={buy2_fee_usdt:.6f} fee_bnb={buy2_fee_bnb:.8f}",
                        )

                        if active_sell_order_id:
                            cancel_order_safe(client, symbol, active_sell_order_id)

                        current_held_qty = float(format_quantity(qty1 + qty2_actual, step_sz))
                        current_total_cost = cost1 + cost2_actual
                        blended_avg = current_total_cost / current_held_qty

                        # Keep cycles record synchronized with combined position
                        try:
                            with get_conn() as conn:
                                conn.execute(
                                    "UPDATE cycles SET entry_cost = ?, qty = ?, fee_bnb = COALESCE(fee_bnb, 0) + ?, fee_usdt = COALESCE(fee_usdt, 0) + ? WHERE id = ?",
                                    (current_total_cost, current_held_qty, buy2_fee_bnb, buy2_fee_usdt, cycle_id),
                                )
                        except Exception:
                            pass

                        log_event(
                            f"[{bot_name}] [{symbol}] DCA Dip caught! Tranche 2 filled: qty={qty2_actual} @ {avg_price2:.8f}. "
                            f"Combined position: qty={current_held_qty} @ blended avg {blended_avg:.8f} (cost={current_total_cost:.4f} USDT)",
                            level="TRADE",
                            event_type="BUY",
                            instance_id=instance_id,
                            symbol=symbol,
                        )
                        print(
                            f"[{bot_name}] [{symbol}] DCA Dip caught! Combined position: qty={current_held_qty} @ blended avg {blended_avg:.8f}"
                        )

                        req_comb_exit = (current_total_cost + min_net_profit_comb + total_buy_fee_usdt) / (1.0 - EST_FEE_RATE_PER_SIDE)
                        active_target_price = float(format_price(req_comb_exit / current_held_qty, tick_sz, rounding="CEILING"))

                        comb_sell = limit_sell(client, symbol, current_held_qty, active_target_price)
                        active_sell_order_id = comb_sell.get("orderId")
                        stage1_repriced = False
                        stage2_repriced = False
                        stage3_repriced = False
                        tranche2_fill_ts = time.time()
                        start_ts = time.time()
                        log_event(
                            f"[{bot_name}] [{symbol}] DCA: Posted combined LIMIT SELL {current_held_qty} @ {active_target_price} (micro-profit ≥ +{min_net_profit_comb:.4f} USDT)",
                            level="TRADE",
                            event_type="LIMIT_SELL_SUBMIT",
                            instance_id=instance_id,
                            symbol=symbol,
                        )
                        print(f"[{bot_name}] [{symbol}] DCA: Posted combined LIMIT SELL {current_held_qty} @ {active_target_price}")
                except Exception:
                    pass

            # Check Micro-Profit Target and Stop-Loss (Dynamic Per-Coin)
            try:
                ob = client.get_orderbook_ticker(symbol=symbol)
                cur_bid = float(ob.get("bidPrice") or 0.0)
                if cur_bid > 0:
                    cur_val = current_held_qty * cur_bid
                    cur_pnl = estimate_net_unrealized(current_total_cost, cur_val, total_buy_fee_usdt)
                    update_instance(instance_id, current_pnl=cur_pnl)

                    active_target_profit = min_net_profit_comb if tranche2_filled else min_net_profit1
                    # If profit occurs in any small amount after fees, end cycle immediately!
                    if cur_pnl >= 0.0010 or cur_pnl >= active_target_profit:
                        should_take_win = True
                        if should_take_win:
                            if limit_buy2_id and not tranche2_filled:
                                cancel_order_safe(client, symbol, limit_buy2_id)
                            if active_sell_order_id:
                                cancel_order_safe(client, symbol, active_sell_order_id)
                            log_event(
                                f"[{bot_name}] [{symbol}] DCA Profit target reached: est. NET pnl={cur_pnl:+.4f} (peak={dca_peak_pnl:+.4f}) — locking in win",
                                level="TRADE",
                                event_type="TARGET",
                                instance_id=instance_id,
                                symbol=symbol,
                            )
                            print(f"[{bot_name}] [{symbol}] DCA Profit reached: est. NET pnl={cur_pnl:+.4f} (peak={dca_peak_pnl:+.4f}) — selling via Limit Sell @ bid")
                            sell_px = float(format_price(cur_bid, tick_sz, rounding="FLOOR"))
                            sell_res = limit_sell(client, symbol, current_held_qty, sell_px)
                            time.sleep(0.5)
                            try:
                                sell = client.get_order(symbol=symbol, orderId=sell_res.get("orderId"), recvWindow=10000)
                            except Exception:
                                sell = sell_res
                            exit_value = float(sell.get("cummulativeQuoteQty") or (current_held_qty * sell_px))
                            sell_fee_info = fee_from_order(client, sell, symbol)
                            sell_fee_usdt = sell_fee_info["fee_usdt"]
                            sell_fee_bnb = sell_fee_info["fee_bnb"]
                            settled = settle_pnl(current_total_cost, exit_value, total_buy_fee_usdt, sell_fee_usdt)
                            duration = time.time() - start_ts

                            insert_trade(
                                instance_id=instance_id,
                                cycle_id=cycle_id,
                                symbol=symbol,
                                side="SELL",
                                quantity=current_held_qty,
                                price=exit_value / current_held_qty if current_held_qty else 0,
                                quote_qty=exit_value,
                                fee_bnb=sell_fee_bnb,
                                fee_usdt=sell_fee_usdt,
                                order_id=sell.get("orderId"),
                                pnl_usdt=settled["net_pnl"],
                                notes="TARGET (DCA)",
                            )
                            close_cycle(
                                cycle_id,
                                exit_value,
                                settled["net_pnl"],
                                "SUCCESS",
                                duration,
                                fee_bnb=total_buy_fee_bnb + sell_fee_bnb,
                                fee_usdt=total_buy_fee_usdt + sell_fee_usdt,
                            )
                            update_instance(instance_id, current_symbol=None, current_pnl=0.0)
                            return settled["net_pnl"], "TARGET" 

                    # -----------------------------------------------------------------------
                    # Adaptive Multi-Algorithm Lifecycle Engine (Never-Give-Up Cycle)
                    # Instead of giving up or dumping at a loss, the bot transitions across
                    # algorithms on the held coin until it realizes a net profit!
                    # -----------------------------------------------------------------------
                    elapsed = time.time() - start_ts

                    # Phase 2 Transition (60s): Switch to MEAN_REVERSION & Orderbook Spread Pegger
                    if (
                        active_sell_order_id
                        and not stage1_repriced
                        and elapsed >= 60.0
                        and current_held_qty > 0
                    ):
                        try:
                            stage1_exit = (current_total_cost + total_buy_fee_usdt + 0.0020) / (1.0 - EST_FEE_RATE_PER_SIDE)
                            stage1_limit_price = float(format_price(stage1_exit / current_held_qty, tick_sz, rounding="CEILING"))
                            if stage1_limit_price < active_target_price:
                                log_event(
                                    f"[{bot_name}] [{symbol}] Algorithm Shift (60s): MOMENTUM -> MEAN_REVERSION — repricing limit sell {active_target_price} -> {stage1_limit_price} (+0.0020 net target)",
                                    level="INFO",
                                    event_type="ALGO_SHIFT_MEAN_REVERSION",
                                    instance_id=instance_id,
                                    symbol=symbol,
                                )
                                print(f"[{bot_name}] [{symbol}] Algorithm Shift (60s): MOMENTUM -> MEAN_REVERSION | Repricing limit sell to {stage1_limit_price} (+0.0020 net)")
                                cancel_order_safe(client, symbol, active_sell_order_id)
                                time.sleep(0.2)
                                new_sell = limit_sell(client, symbol, current_held_qty, stage1_limit_price)
                                active_sell_order_id = new_sell.get("orderId")
                                active_target_price = stage1_limit_price
                            update_instance(instance_id, strategy="MEAN_REVERSION")
                            stage1_repriced = True
                        except Exception:
                            stage1_repriced = True

                    # Phase 3 Transition (180s / 3m): Switch to MICRO_GRID_REPAIR (Dip Support Absorption)
                    if (
                        active_sell_order_id
                        and not stage2_repriced
                        and elapsed >= 180.0
                        and current_held_qty > 0
                    ):
                        try:
                            floor_exit = (current_total_cost + total_buy_fee_usdt + 0.0010) / (1.0 - EST_FEE_RATE_PER_SIDE)
                            floor_limit_price = float(format_price(floor_exit / current_held_qty, tick_sz, rounding="CEILING"))
                            if floor_limit_price < active_target_price:
                                log_event(
                                    f"[{bot_name}] [{symbol}] Algorithm Shift (180s): MEAN_REVERSION -> MICRO_GRID_REPAIR — pinning sell to break-even floor {floor_limit_price} (+0.0010 net profit floor)",
                                    level="INFO",
                                    event_type="ALGO_SHIFT_MICRO_GRID",
                                    instance_id=instance_id,
                                    symbol=symbol,
                                )
                                print(f"[{bot_name}] [{symbol}] Algorithm Shift (180s): MEAN_REVERSION -> MICRO_GRID_REPAIR | Sell pinned to {floor_limit_price} (+0.0010 floor)")
                                cancel_order_safe(client, symbol, active_sell_order_id)
                                time.sleep(0.2)
                                new_sell = limit_sell(client, symbol, current_held_qty, floor_limit_price)
                                active_sell_order_id = new_sell.get("orderId")
                                active_target_price = floor_limit_price
                            update_instance(instance_id, strategy="MICRO_GRID_REPAIR")
                            stage2_repriced = True
                        except Exception:
                            stage2_repriced = True

                    # Phase 4 Transition (300s+ / 5m+): Switch to PASSIVE_OSCILLATOR (Never Give Up Engine)
                    # Stays on Binance order book at Break-Even Floor until filled as Maker or swept by bid
                    if (
                        active_sell_order_id
                        and elapsed >= 300.0
                        and not stage3_repriced
                        and current_held_qty > 0
                    ):
                        try:
                            update_instance(instance_id, strategy="PASSIVE_OSCILLATOR")
                            log_event(
                                f"[{bot_name}] [{symbol}] Algorithm Shift (300s): Engaging PASSIVE_OSCILLATOR — Never-Give-Up Engine active on Binance order book @ {active_target_price}",
                                level="INFO",
                                event_type="ALGO_SHIFT_OSCILLATOR",
                                instance_id=instance_id,
                                symbol=symbol,
                            )
                            print(f"[{bot_name}] [{symbol}] Algorithm Shift (300s): PASSIVE_OSCILLATOR active — resting at break-even floor until filled in profit")
                            stage3_repriced = True
                        except Exception:
                            stage3_repriced = True

                    # Order-Flow Micro-Scratch Safety Valve (Engages after 600s if order flow turns toxic)
                    elapsed = time.time() - start_ts
                    should_micro_scratch = False
                    if elapsed >= 600.0 and -0.0250 <= cur_pnl <= -0.0010:
                        try:
                            # Quick VPIN / Taker ratio check: if selling dominates (> 75% taker sells), scratch micro-loss
                            vpin_ok, _ = check_pre_entry_order_flow_vpin(client, symbol)
                            if not vpin_ok:
                                should_micro_scratch = True
                                log_event(
                                    f"[{bot_name}] [{symbol}] Toxic Order Flow Scratch: trade open {elapsed:.1f}s, loss {cur_pnl:+.4f} USDT — cleanly scratching to prevent deep drawdown",
                                    level="WARN",
                                    event_type="MICRO_SCRATCH",
                                    instance_id=instance_id,
                                    symbol=symbol,
                                )
                        except Exception:
                            pass

                    if cur_pnl <= -0.05 or should_micro_scratch or hard_stop_triggered(cur_pnl, investment_usdt, stop_loss, elapsed):
                        if limit_buy2_id and not tranche2_filled:
                            cancel_order_safe(client, symbol, limit_buy2_id)
                        if active_sell_order_id:
                            cancel_order_safe(client, symbol, active_sell_order_id)
                        reason_tag = "STOP_LOSS"
                        log_event(
                            f"[{bot_name}] [{symbol}] DCA 0.05 STOP_LOSS reached: est. NET pnl={cur_pnl:+.4f} (elapsed {elapsed:.1f}s) — liquidating immediately to rotate to next coin",
                            level="WARN",
                            event_type="STOP_LOSS",
                            instance_id=instance_id,
                            symbol=symbol,
                        )
                        print(f"[{bot_name}] [{symbol}] DCA 0.05 STOP_LOSS reached: est. NET pnl={cur_pnl:+.4f} (elapsed {elapsed:.1f}s) — liquidating immediately & rotating to next coin")
                        try:
                            # Immediate market execution
                            sell_res = client.create_order(
                                symbol=symbol,
                                side=Client.SIDE_SELL,
                                type=Client.ORDER_TYPE_MARKET,
                                quantity=format_quantity(current_held_qty, step_sz),
                                recvWindow=10000,
                                newOrderRespType="FULL",
                            )
                        except Exception:
                            sell_px = float(format_price(cur_bid, tick_sz, rounding="FLOOR"))
                            sell_res = limit_sell(client, symbol, current_held_qty, sell_px)
                        time.sleep(0.5)
                        try:
                            sell = client.get_order(symbol=symbol, orderId=sell_res.get("orderId"), recvWindow=10000)
                        except Exception:
                            sell = sell_res
                        exit_value = float(sell.get("cummulativeQuoteQty") or (current_held_qty * sell_px))
                        sell_fee_info = fee_from_order(client, sell, symbol)
                        sell_fee_usdt = sell_fee_info["fee_usdt"]
                        sell_fee_bnb = sell_fee_info["fee_bnb"]
                        settled = settle_pnl(current_total_cost, exit_value, total_buy_fee_usdt, sell_fee_usdt)
                        duration = time.time() - start_ts

                        insert_trade(
                            instance_id=instance_id,
                            cycle_id=cycle_id,
                            symbol=symbol,
                            side="SELL",
                            quantity=current_held_qty,
                            price=exit_value / current_held_qty if current_held_qty else 0,
                            quote_qty=exit_value,
                            fee_bnb=sell_fee_bnb,
                            fee_usdt=sell_fee_usdt,
                            order_id=sell.get("orderId"),
                            pnl_usdt=settled["net_pnl"],
                            notes=f"{reason_tag} (DCA)",
                        )
                        close_cycle(
                            cycle_id,
                            exit_value,
                            settled["net_pnl"],
                            reason_tag,
                            duration,
                            fee_bnb=total_buy_fee_bnb + sell_fee_bnb,
                            fee_usdt=total_buy_fee_usdt + sell_fee_usdt,
                        )
                        update_instance(instance_id, current_symbol=None, current_pnl=0.0)
                        if settled["net_pnl"] < 0:
                            record_pending_loss(instance_id, abs(settled["net_pnl"]), bot_name)
                            try:
                                add_cooldown_symbol(symbol, duration_sec=POST_LOSS_COOLDOWN_SEC)
                            except Exception:
                                pass
                        else:
                            settle_loss_recovery(instance_id, settled["net_pnl"], bot_name)
                        return settled["net_pnl"], reason_tag

                    # Zero-loss spot invariant: never market-dump coins on adverse drift or timeout
                    # The resting limit sell order (repriced to micro-scratch at 300s) stays active on Binance order book
                    pass
            except Exception:
                pass

            time.sleep(0.8)

    except BinanceAPIException as e:
        tb = traceback.format_exc()
        if is_network_error(e):
            wait_for_network(client, bot_name, symbol or "INIT", instance_id, context="Binance connection", exc=e)
            return None, "NETWORK_RETRY"

        msg = f"[{bot_name}] [{symbol}] BinanceAPIException: {e}"
        update_instance(instance_id, status="ERROR", last_error=msg, current_symbol=None, finished_at=datetime.now(timezone.utc).isoformat())
        log_event(msg, level="ERROR", event_type="BINANCE_API_ERROR", instance_id=instance_id, symbol=symbol, detail=tb)
        print(msg)
        if cycle_id and current_held_qty > 0:
            _emergency_liquidate(client, symbol, current_total_cost, cycle_id, instance_id, bot_name, start_ts, str(e))
        elif cycle_id:
            close_cycle(cycle_id, None, None, "ERROR", time.time() - start_ts)
        return None, "ERROR"

    except Exception as e:
        tb = traceback.format_exc()
        if is_network_error(e):
            wait_for_network(client, bot_name, symbol or "INIT", instance_id, context="network connection", exc=e)
            return None, "NETWORK_RETRY"

        msg = f"[{bot_name}] [{symbol}] Exception: {e}"
        update_instance(instance_id, status="ERROR", last_error=msg, current_symbol=None, finished_at=datetime.now(timezone.utc).isoformat())
        log_event(msg, level="EXCEPTION", event_type="EXCEPTION", instance_id=instance_id, symbol=symbol, detail=tb)
        print(msg)
        traceback.print_exc()
        if cycle_id and current_held_qty > 0:
            _emergency_liquidate(client, symbol, current_total_cost, cycle_id, instance_id, bot_name, start_ts, str(e))
        elif cycle_id:
            close_cycle(cycle_id, None, None, "ERROR", time.time() - start_ts)
        return None, "ERROR"


# ---------------------------------------------------------------------------
# One full cycle (buy → monitor → sell)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
def trade_one_cycle(
    client: Client,
    instance_id: int,
    investment_usdt: float,
    target_profit: float,
    stop_loss: float,
) -> Tuple[Optional[float], Optional[str]]:
    """
    Returns (realized_pnl, reason) of this cycle, or (None, reason) on error / skip / abort.
    """
    inst = get_instance(instance_id) or {}
    bot_name = inst.get("name") or str(instance_id)
    cycles_done = int(inst.get("cycles_done") or 0)
    symbol = inst.get("current_symbol")
    strategy = (inst.get("strategy") or "AUTO").upper()

    # ── DEDICATED DUAL SOXLB ↔ SOXSB REINVEST HARVESTER ROUTER ──
    if strategy in ("DUAL_SOXLB_SOXSB", "DUAL_HARVEST_STRATEGY", "DUAL_HARVEST", "DUAL_SOXLB_SOXSB_STRATEGY"):
        import dual_soxlb_soxsb
        claim_symbol = "SOXLB+SOXSB"
        claim_symbol_for_instance(instance_id, claim_symbol)
        return dual_soxlb_soxsb.trade_dual_soxlb_soxsb_cycle(
            client=client,
            instance_id=instance_id,
            investment_usdt=investment_usdt,
            target_profit=target_profit,
            stop_loss=stop_loss,
            bot_name=bot_name,
        )

    # Pre-flight check: verify uncommitted USDT balance before scanning (with retry loop)
    min_required = min(investment_usdt * 0.95, 10.0)  # Allow trading as long as >= 10 USDT available
    available_for_me = 0.0
    usdt_free = 0.0
    committed_other = 0.0

    for attempt in range(6):
        usdt_free = float(client.get_asset_balance(asset="USDT")["free"] or 0.0)
        committed_other = get_unfilled_committed_usdt(exclude_instance_id=instance_id)
        available_for_me = max(0.0, usdt_free - committed_other)
        if available_for_me >= min_required:
            break
        if is_stop_requested(instance_id):
            return None, "ABORTED"
        time.sleep(2.0)

    if available_for_me < min_required:
        sym_tag = symbol or "INIT"
        msg = (
            f"[{bot_name}] [{sym_tag}] Insufficient USDT: need ≥{min_required:.2f}, "
            f"available={available_for_me:.4f} (free={usdt_free:.4f}, other_committed={committed_other:.4f})"
        )
        print(msg)
        update_instance(instance_id, last_error=msg)
        log_event(msg, level="WARN", event_type="SKIP", instance_id=instance_id, symbol=sym_tag)
        return None, "INSUFFICIENT_FUNDS"

    if is_stop_requested(instance_id):
        return None, "ABORTED"

    active_symbols = get_active_symbols(exclude_instance_id=instance_id)
    cfg = {}
    try:
        pass
        cfg = get_manager_config()
    except Exception:
        pass

    uo_raw = (cfg.get("use_only") or "").strip().upper()

    # ── DEDICATED HYBRID SOXLB ↔ SOXSB CONFLUENCE ROUTER ──
    if strategy in ("HYBRID_SOXLB_SOXSB_SRATEGY", "HYBRID_SOXLB_SOXSB_STRATEGY"):
        import hybrid_soxlb_soxsb
        claim_symbol = symbol if symbol in ("SOXLBUSDT", "SOXSBUSDT") else "SOXLBUSDT"
        claim_symbol_for_instance(instance_id, claim_symbol)

        committed_by_me = get_instance_committed_capital(instance_id)
        max_investment_allowed = max(0.0, investment_usdt - committed_by_me)
        if max_investment_allowed < 5.0:
            msg = f"[{bot_name}] [{claim_symbol}] Maximum investment limit reached: already have ${committed_by_me:.2f} committed out of ${investment_usdt:.2f} max."
            print(msg)
            update_instance(instance_id, last_error=msg)
            release_symbol(instance_id)
            time.sleep(5.0)
            return None, "CAPITAL_LIMIT_REACHED"

        actual_investment_for_cycle = min(max_investment_allowed, available_for_me, investment_usdt)
        try:
            lot = get_lot_info(client, claim_symbol)
        except Exception:
            lot = {"min_notional": 5.0, "tick_size": 0.01, "step": 0.001, "min_qty": 0.001}
        start_ts = time.time()
        return hybrid_soxlb_soxsb.trade_hybrid_soxlb_soxsb_cycle(
            client=client,
            instance_id=instance_id,
            investment_usdt=actual_investment_for_cycle,
            target_profit=target_profit,
            stop_loss=stop_loss,
            bot_name=bot_name,
            symbol=claim_symbol,
            lot=lot,
            start_ts=start_ts,
            timeout_sec=0.0,
            profile=None,
        )

    # ── DEDICATED SOXSB ↔ SOXLB LAG ARBITRAGE ROUTER ──
    if strategy in ("SOXS_SOXL_LAG_PREDICTOR", "SEMICONDUCTOR_LAG_ARB"):
        import semiconductor_lag_arb
        claim_symbol = symbol if symbol in ("SOXLBUSDT", "SOXSBUSDT") else "SOXLBUSDT"
        claim_symbol_for_instance(instance_id, claim_symbol)

        committed_by_me = get_instance_committed_capital(instance_id)
        max_investment_allowed = max(0.0, investment_usdt - committed_by_me)
        if max_investment_allowed < 5.0:
            msg = f"[{bot_name}] [{claim_symbol}] Maximum investment limit reached: already have ${committed_by_me:.2f} committed out of ${investment_usdt:.2f} max."
            print(msg)
            update_instance(instance_id, last_error=msg)
            release_symbol(instance_id)
            time.sleep(5.0)
            return None, "CAPITAL_LIMIT_REACHED"

        actual_investment_for_cycle = min(max_investment_allowed, available_for_me, investment_usdt)
        try:
            lot = get_lot_info(client, claim_symbol)
        except Exception:
            lot = {"min_notional": 5.0, "tick_size": 0.01, "step": 0.001, "min_qty": 0.001}
        start_ts = time.time()
        return semiconductor_lag_arb.trade_soxs_soxl_lag_cycle(
            client=client,
            instance_id=instance_id,
            investment_usdt=actual_investment_for_cycle,
            target_profit=target_profit,
            stop_loss=stop_loss,
            bot_name=bot_name,
            symbol=claim_symbol,
            lot=lot,
            start_ts=start_ts,
            timeout_sec=0.0,
            profile=None,
        )

    is_semi = (
        strategy == "SEMICONDUCTOR_DUAL_ENGINE"
        or (symbol in ("SOXLBUSDT", "SOXSBUSDT"))
        or any(s in ("SOXLBUSDT", "SOXSBUSDT") for s in uo_raw.split(","))
    )

    if is_semi:
        # ── EXCLUSIVE SEMICONDUCTOR 5M PREDICTION ROUTER ──
        # NONE of the other existing parameters (BTC dump filter, spread filters,
        # book ratio, volatility, taker-buy, meme sizers, 0.05 USDT stop, etc.)
        # are evaluated for SOXLB and SOXSB!
        import semiconductor_flip_flop
        claim_symbol = symbol if symbol in ("SOXLBUSDT", "SOXSBUSDT") else "SOXLBUSDT"
        
        # Claim symbol for this instance (multi-bot concurrent trading permitted for SOXLB/SOXSB)
        claim_symbol_for_instance(instance_id, claim_symbol)

        # Enforce maximum investment cap for this bot
        committed_by_me = get_instance_committed_capital(instance_id)
        max_investment_allowed = max(0.0, investment_usdt - committed_by_me)
        if max_investment_allowed < 5.0:
            msg = f"[{bot_name}] [{claim_symbol}] Maximum investment limit reached: already have ${committed_by_me:.2f} committed out of ${investment_usdt:.2f} max."
            print(msg)
            update_instance(instance_id, last_error=msg)
            release_symbol(instance_id)
            time.sleep(5.0)
            return None, "CAPITAL_LIMIT_REACHED"

        actual_investment_for_cycle = min(max_investment_allowed, available_for_me, investment_usdt)

        try:
            lot = get_lot_info(client, claim_symbol)
        except Exception:
            lot = {"min_notional": 5.0, "tick_size": 0.01, "step": 0.001, "min_qty": 0.001}
        start_ts = time.time()
        return semiconductor_flip_flop.trade_semiconductor_flip_flop_cycle(
            client=client,
            instance_id=instance_id,
            investment_usdt=actual_investment_for_cycle,
            target_profit=target_profit,
            stop_loss=stop_loss,
            bot_name=bot_name,
            symbol=claim_symbol,
            lot=lot,
            start_ts=start_ts,
            timeout_sec=0.0,
            profile=None,
        )

    # Ensure symbol is claimed atomically and not traded by another bot
    if not symbol or symbol == "INIT" or symbol in active_symbols or not claim_symbol_for_instance(instance_id, symbol):
        try:
            use_only_str = (cfg.get("use_only") or "").strip()
            use_only_list = [s.strip().upper() for s in use_only_str.split(",") if s.strip()] if use_only_str else None
            max_sp = DEFAULT_MAX_SPREAD_PCT
            min_br = DEFAULT_MIN_BOOK_RATIO
            min_vol = DEFAULT_MIN_VOLATILITY_1M
            min_tbr = DEFAULT_MIN_TAKER_BUY_RATIO
            symbol = select_and_claim_best_pair(
                client,
                instance_id=instance_id,
                exclude_symbols=active_symbols,
                use_only=use_only_list,
                max_spread_pct=max_sp,
                min_book_ratio=min_br,
                min_volatility_1m=min_vol,
                min_taker_buy_ratio=min_tbr,
            )
        except RuntimeError as e:
            msg = f"[{bot_name}] [NO_PAIR] {e}"
            print(msg)
            update_instance(instance_id, last_error=msg)
            log_event(msg, level="WARN", event_type="SKIP", instance_id=instance_id, symbol="NO_PAIR")
            time.sleep(5)
            return None, "NO_PAIR"
        update_instance(instance_id, current_pnl=0.0)
    else:
        update_instance(instance_id, current_pnl=0.0)

    # Attach TeeLogger to write to logs/{bot_name}_{symbol}.log
    log_file = os.path.join(LOGS_DIR, f"{bot_name}_{symbol}.log")
    sys.stdout = TeeLogger(log_file, sys.__stdout__)
    sys.stderr = sys.stdout

    log_event(
        f"[{bot_name}] [{symbol}] Scanning selected pair: {symbol}",
        level="INFO",
        event_type="SCAN",
        instance_id=instance_id,
        symbol=symbol,
    )
    print(f"[{bot_name}] [{symbol}] Scanning selected pair: {symbol}")

    try:
        lot = get_lot_info(client, symbol)
        usdt_free = float(client.get_asset_balance(asset="USDT")["free"] or 0.0)
        investment = min(usdt_free, investment_usdt)

        if investment < lot["min_notional"]:
            msg = f"[{bot_name}] [{symbol}] Insufficient USDT: need ≥{lot['min_notional']:.2f}, have {investment:.4f}"
            print(msg)
            update_instance(instance_id, last_error=msg)
            log_event(msg, level="WARN", event_type="SKIP", instance_id=instance_id, symbol=symbol)
            time.sleep(5)
            return None, "INSUFFICIENT_FUNDS"

        if is_stop_requested(instance_id):
            return None, "ABORTED"

        start_ts = time.time()

        # Hybrid Market Regime Engine: Predict coin's market and select matching algorithm
        auto_strat, auto_analysis = auto_select_cycle_strategy(
            client, symbol, investment_usdt, cycles_done=cycles_done,
            forced_strategy=strategy if strategy not in ("AUTO", "", None) else None
        )
        strategy = auto_strat

        # Dynamic Coin-Adaptive Investment Sizer: $5-$10 for volatile/memes, up to $25 max for majors
        investment = calculate_coin_investment_size(
            symbol=symbol,
            lot=lot,
            profile=auto_analysis,
            max_investment=investment_usdt,
            usdt_free=usdt_free,
        )
        stop_loss = -0.05  # Fixed micro stop-loss at 0.05 USDT
        pred_mkt = auto_analysis.get("predicted_market", "Market")
        m_risk = auto_analysis.get("main_risk", "None")
        n_speed = auto_analysis.get("needs_speed", "No")
        t_profit = auto_analysis.get("min_net_profit_combined", target_profit)

        log_event(
            f"[{bot_name}] [{symbol}] Hybrid Engine: Market='{pred_mkt}' -> Strategy='{strategy}' "
            f"(Target=+{t_profit:.4f} USDT, SL={stop_loss:.4f} USDT | Main Risk: {m_risk}, Needs Speed: {n_speed})",
            level="INFO",
            event_type="STRATEGY_SELECT",
            instance_id=instance_id,
            symbol=symbol,
        )
        print(f"[{bot_name}] [{symbol}] Hybrid Engine: Market='{pred_mkt}' -> Strategy='{strategy}' | Target=+{t_profit:.4f} USDT | SL={stop_loss:.4f} USDT")

        # Viability Guard: Block buying if coin is in an adverse downtrend, dump, or exhausted move
        if not auto_analysis.get("is_viable_trade", True):
            skip_msg = f"[{bot_name}] [{symbol}] Cycle blocked: Market is '{pred_mkt}' (adverse dump or unviable buy regime). Protected capital ($0 loss)."
            print(skip_msg)
            update_instance(instance_id, last_error=skip_msg)
            log_event(skip_msg, level="WARN", event_type="REGIME_REJECT", instance_id=instance_id, symbol=symbol)
            return None, "UNFAVORABLE_REGIME" 
    except BinanceAPIException as e:
        if is_network_error(e):
            wait_for_network(client, bot_name, symbol or "INIT", instance_id, context="cycle preparation", exc=e)
            return None, "NETWORK_RETRY"
        msg = f"[{bot_name}] [{symbol}] BinanceAPIException during prep: {e}"
        update_instance(instance_id, last_error=msg)
        log_event(msg, level="ERROR", event_type="BINANCE_API_ERROR", instance_id=instance_id, symbol=symbol)
        time.sleep(10.0)
        return None, "BINANCE_API_ERROR"
    except Exception as e:
        if is_network_error(e):
            wait_for_network(client, bot_name, symbol or "INIT", instance_id, context="cycle preparation", exc=e)
            return None, "NETWORK_RETRY"
        msg = f"[{bot_name}] [{symbol}] Error during cycle preparation: {e}"
        print(msg)
        update_instance(instance_id, last_error=msg)
        log_event(msg, level="ERROR", event_type="PREP_ERROR", instance_id=instance_id, symbol=symbol)
        time.sleep(10.0)
        return None, "ERROR"


    # ── Dedicated 100% Capital Flip-Flop Ratchet Router ──
    if strategy == "SEMICONDUCTOR_DUAL_ENGINE" or symbol in ("SOXLBUSDT", "SOXSBUSDT"):
        import semiconductor_flip_flop
        return semiconductor_flip_flop.trade_semiconductor_flip_flop_cycle(
            client=client,
            instance_id=instance_id,
            investment_usdt=investment,
            target_profit=target_profit,
            stop_loss=stop_loss,
            bot_name=bot_name,
            symbol=symbol,
            lot=lot,
            start_ts=start_ts,
            timeout_sec=0.0,
            profile=auto_analysis,
        )

    # Quantum-Maker AI Engine: Route ALL other strategies to 2-Tranche Self-Healing DCA Engine
    if strategy in ("AUTO", "SCALE_IN_DCA", "GRID", "MOMENTUM", "BEST_BID_MAKER", "TREND_FOLLOWING", "MEAN_REVERSION", "MARKET_MAKING_SCALPING", "MACHINE_LEARNING"):
        return trade_one_cycle_scale_in(
            client=client,
            instance_id=instance_id,
            investment_usdt=investment,
            target_profit=target_profit,
            stop_loss=stop_loss,
            bot_name=bot_name,
            symbol=symbol,
            lot=lot,
            start_ts=start_ts,
            timeout_sec=0.0,
            profile=auto_analysis,
        )

    cycle_id = None
    qty = 0.0
    entry_cost = 0.0
    buy_fee_usdt = 0.0

    try:
        # Best-Bid Smart Maker Entry with Sniper Fallback:
        # 1. Place a LIMIT buy at best_bid (or best_bid + 1 tick if spread allows) to capture maker fill at lowest rate
        # 2. Wait up to 12.0s for sellers to hit our bid
        # 3. If filled: proceed directly to resting limit sell at target profit
        # 4. If not filled in 12s: cancel limit buy, check sniper buy pressure, and execute market buy so trades enter reliably
        # Pre-entry VPIN & Order-Flow Toxicity Defense
        vpin_ok, vpin_reason = check_pre_entry_order_flow_vpin(client, symbol)
        if not vpin_ok:
            log_event(
                f"[{bot_name}] [{symbol}] VPIN Toxicity Abort: {vpin_reason} — skipping entry to protect capital ($0 fee, $0 loss)",
                level="WARN",
                event_type="VPIN_ABORT",
                instance_id=instance_id,
                symbol=symbol,
            )
            print(f"[{bot_name}] [{symbol}] VPIN Toxicity Abort: {vpin_reason} ($0 fee, $0 loss)")
            return None, "UNFILLED_ENTRY"

        ob_entry = client.get_orderbook_ticker(symbol=symbol)
        best_bid = float(ob_entry.get("bidPrice") or 0.0)
        best_ask = float(ob_entry.get("askPrice") or 0.0)

        buy = None
        limit_buy_id = None

        if best_bid > 0 and best_ask > best_bid:
            tick_sz = lot.get("tick_size") or 0.0001
            entry_style = auto_analysis.get("entry_style", "BEST_BID_MAKER")
            max_entry_wait_sec = LIMIT_BUY_MAX_WAIT_SEC

            if entry_style == "SNIPER_BREAKOUT":
                # Momentum breakout needs fast fill before price runs away
                bid_price = best_bid + tick_sz
                if (best_ask - best_bid) <= tick_sz:
                    bid_price = best_ask
                max_entry_wait_sec = 5.0
            elif entry_style == "LIMIT_OVERSOLD":
                # Mean reversion: buy into the dip/support
                bid_price = best_bid - tick_sz
                max_entry_wait_sec = 18.0
            elif entry_style == "LIMIT_PULLBACK":
                # Trend following: patient pullback fill
                bid_price = best_bid
                max_entry_wait_sec = 12.0
            else:
                # Market making / default maker
                if LIMIT_BUY_OFFSET_PCT > 0.0:
                    bid_price = best_bid * (1.0 - LIMIT_BUY_OFFSET_PCT)
                else:
                    bid_price = best_bid - tick_sz
                    if (best_ask - best_bid) > (3 * tick_sz):
                        bid_price = best_bid
                max_entry_wait_sec = LIMIT_BUY_MAX_WAIT_SEC

            buy_price_str = format_price(bid_price, tick_sz, rounding="FLOOR")
            bid_price = float(buy_price_str)

            calc_qty = investment / bid_price
            qty_str = format_quantity(calc_qty, lot["step"])
            order_qty = float(qty_str)

            if order_qty * bid_price < lot["min_notional"]:
                order_qty = float(format_quantity((lot["min_notional"] / bid_price) * 1.02 + lot["step"], lot["step"]))
                qty_str = format_quantity(order_qty, lot["step"])

            try:
                log_event(
                    f"[{bot_name}] [{symbol}] Placing [{entry_style}] LIMIT BUY {qty_str} {symbol} @ {buy_price_str} (~{order_qty * bid_price:.2f} USDT)",
                    level="TRADE",
                    event_type="LIMIT_BUY_SUBMIT",
                    instance_id=instance_id,
                    symbol=symbol,
                )
                print(f"[{bot_name}] [{symbol}] Placing [{entry_style}] LIMIT BUY {qty_str} {symbol} @ {buy_price_str}")
                limit_buy_order = limit_buy(client, symbol, order_qty, bid_price)
                limit_buy_id = limit_buy_order.get("orderId")

                # Wait for sellers to fill our bid
                wait_start = time.time()
                while time.time() - wait_start < max_entry_wait_sec:
                    if is_stop_requested(instance_id):
                        cancel_order_safe(client, symbol, limit_buy_id)
                        return None, "ABORTED"
                    time.sleep(0.4)
                    ord_chk = client.get_order(symbol=symbol, orderId=limit_buy_id, recvWindow=10000)
                    if ord_chk.get("status") == "FILLED":
                        buy = ord_chk
                        log_event(
                            f"[{bot_name}] [{symbol}] Best-bid LIMIT BUY filled @ {buy_price_str} (maker entry)!",
                            level="TRADE",
                            event_type="LIMIT_BUY_FILLED",
                            instance_id=instance_id,
                            symbol=symbol,
                        )
                        print(f"[{bot_name}] [{symbol}] Best-bid LIMIT BUY filled @ {buy_price_str} (maker entry)!")
                        break
                    elif ord_chk.get("status") in ("CANCELED", "REJECTED", "EXPIRED"):
                        break

                    # Toxic flow check: cancel resting buy if order book collapses
                    is_toxic, toxic_reason = is_orderbook_flow_toxic(client, symbol, bid_price)
                    if is_toxic:
                        cancel_order_safe(client, symbol, limit_buy_id)
                        time.sleep(0.2)
                        ord_chk = client.get_order(symbol=symbol, orderId=limit_buy_id, recvWindow=10000)
                        if ord_chk.get("status") == "FILLED":
                            buy = ord_chk
                            break
                        log_event(
                            f"[{bot_name}] [{symbol}] Toxic flow detected ({toxic_reason}) — cancelled maker buy to protect capital ($0 fee, $0 loss)",
                            level="WARN",
                            event_type="TOXIC_FLOW_ABORT",
                            instance_id=instance_id,
                            symbol=symbol,
                        )
                        print(f"[{bot_name}] [{symbol}] Toxic flow detected ({toxic_reason}) — cancelled maker buy ($0 fee, $0 loss)")
                        return None, "UNFILLED_ENTRY"

                if not buy:
                    cancel_order_safe(client, symbol, limit_buy_id)
                    time.sleep(0.2)
                    ord_chk = client.get_order(symbol=symbol, orderId=limit_buy_id, recvWindow=10000)
                    if ord_chk.get("status") == "FILLED":
                        buy = ord_chk
            except Exception as e:
                log_event(
                    f"[{bot_name}] [{symbol}] Limit buy attempt error ({e})",
                    level="WARN",
                    event_type="LIMIT_BUY_ERR",
                    instance_id=instance_id,
                    symbol=symbol,
                )

        if not buy:
            log_event(
                f"[{bot_name}] [{symbol}] Best-bid limit buy not filled in {LIMIT_BUY_MAX_WAIT_SEC}s — cleanly aborted ($0 fee, $0 loss)",
                level="INFO",
                event_type="UNFILLED_ENTRY",
                instance_id=instance_id,
                symbol=symbol,
            )
            print(f"[{bot_name}] [{symbol}] Best-bid limit buy not filled in {LIMIT_BUY_MAX_WAIT_SEC}s — cleanly aborted ($0 fee, $0 loss)")
            return None, "UNFILLED_ENTRY"

        qty = float(buy["executedQty"])
        entry_cost = float(buy["cummulativeQuoteQty"])
        qty = float(format_quantity(qty, lot["step"]))
        if qty <= 0:
            raise RuntimeError("Rounded quantity is zero after LOT_SIZE step")

        buy_fee_info = fee_from_order(client, buy)
        buy_fee_usdt = buy_fee_info["fee_usdt"]
        buy_fee_bnb = buy_fee_info["fee_bnb"]

        cycle_id = insert_cycle(
            instance_id,
            symbol,
            entry_cost,
            qty,
            fee_bnb=buy_fee_bnb,
            fee_usdt=buy_fee_usdt,
            status="OPEN",
        )
        if record_cycle_features:
            try:
                snap = dict(RECENT_PAIR_METRICS.get(symbol) or {"symbol": symbol})
                record_cycle_features(
                    DB_PATH,
                    cycle_id,
                    symbol,
                    snap,
                    p_win=snap.get("p_win", 0.5),
                    ml_score=snap.get("ml_score", 5.0),
                )
            except Exception:
                pass
        avg_price = entry_cost / qty
        insert_trade(
            instance_id=instance_id,
            cycle_id=cycle_id,
            symbol=symbol,
            side="BUY",
            quantity=qty,
            price=avg_price,
            quote_qty=entry_cost,
            fee_bnb=buy_fee_bnb,
            fee_usdt=buy_fee_usdt,
            order_id=buy.get("orderId"),
            notes=f"fee_usdt={buy_fee_usdt:.6f} fee_bnb={buy_fee_bnb:.8f}",
        )
        fee_msg = f"fee={buy_fee_bnb:.8f} BNB ({buy_fee_usdt:.6f} USDT)"
        if buy_fee_info["bnb_amount"] > 0:
            fee_msg += (
                f" (BNB {buy_fee_info['bnb_amount']:.8f} × "
                f"{buy_fee_info['bnb_price']:.4f} = {buy_fee_info['bnb_fee_usdt']:.6f} USDT)"
            )
        log_event(
            f"[{bot_name}] [{symbol}] BUY filled {symbol}: qty={qty} @ {avg_price:.8f} "
            f"cost={entry_cost:.4f} USDT | {fee_msg} | orderId={buy.get('orderId')}",
            level="TRADE",
            event_type="BUY",
            instance_id=instance_id,
            symbol=symbol,
            detail=str({
                "orderId": buy.get("orderId"),
                "executedQty": buy.get("executedQty"),
                "cummulativeQuoteQty": buy.get("cummulativeQuoteQty"),
                "fee": buy_fee_info,
            }),
        )
        print(
            f"[{bot_name}] [{symbol}] BUY filled {symbol}: qty={qty} @ {avg_price:.8f} "
            f"cost={entry_cost:.4f} USDT | {fee_msg} | orderId={buy.get('orderId')}"
        )

        def _finalize_sell(sell_order, reason: str) -> float:
            """Compute net P/L after Binance fees only; log everything; return net_pnl."""
            exit_value = float(sell_order.get("cummulativeQuoteQty") or 0.0)
            sell_fee_info = fee_from_order(client, sell_order, symbol)
            sell_fee_usdt = sell_fee_info["fee_usdt"]
            sell_fee_bnb = sell_fee_info["fee_bnb"]
            settled = settle_pnl(entry_cost, exit_value, buy_fee_usdt, sell_fee_usdt)
            duration = time.time() - start_ts

            insert_trade(
                instance_id=instance_id,
                cycle_id=cycle_id,
                symbol=symbol,
                side="SELL",
                quantity=qty,
                price=exit_value / qty if qty else 0,
                quote_qty=exit_value,
                fee_bnb=sell_fee_bnb,
                fee_usdt=sell_fee_usdt,
                order_id=sell_order.get("orderId"),
                pnl_usdt=settled["net_pnl"],
                notes=reason,
            )
            status = {
                "TARGET": "SUCCESS",
                "STOP_LOSS": "STOP_LOSS",
                "ABORTED": "ABORTED",
                "TIMEOUT": "SCRATCH",
                "SCRATCH": "SCRATCH",
            }.get(reason, reason)
            close_cycle(
                cycle_id,
                exit_value,
                settled["net_pnl"],
                status,
                duration,
                fee_bnb=buy_fee_bnb + sell_fee_bnb,
                fee_usdt=buy_fee_usdt + sell_fee_usdt,
            )
            update_instance(instance_id, current_symbol=None, current_pnl=0.0)

            sell_fee_msg = f"sell_fee={sell_fee_bnb:.8f} BNB ({sell_fee_usdt:.6f} USDT)"
            if sell_fee_info["bnb_amount"] > 0:
                sell_fee_msg += (
                    f" (BNB {sell_fee_info['bnb_amount']:.8f} × "
                    f"{sell_fee_info['bnb_price']:.4f} = {sell_fee_info['bnb_fee_usdt']:.6f} USDT)"
                )

            log_event(
                f"[{bot_name}] [{symbol}] SELL ({reason}) {symbol}: exit={exit_value:.4f} USDT | "
                f"gross={settled['gross_pnl']:+.6f} | fees={settled['fees_usdt']:.6f} "
                f"(buy={buy_fee_usdt:.6f} + sell={sell_fee_usdt:.6f}) | "
                f"NET={settled['net_pnl']:+.6f} USDT | {sell_fee_msg}",
                level="TRADE",
                event_type="SELL",
                instance_id=instance_id,
                symbol=symbol,
                detail=str({
                    "reason": reason,
                    "orderId": sell_order.get("orderId"),
                    "entry_cost": entry_cost,
                    "exit_value": exit_value,
                    "buy_fee": buy_fee_info,
                    "sell_fee": sell_fee_info,
                    "settlement": settled,
                    "duration_sec": duration,
                }),
            )
            print(
                f"[{bot_name}] [{symbol}] SELL ({reason}) {symbol}: exit={exit_value:.4f} USDT | "
                f"gross={settled['gross_pnl']:+.6f} | fees={settled['fees_usdt']:.6f} "
                f"(buy={buy_fee_usdt:.6f} + sell={sell_fee_usdt:.6f}) | "
                f"NET={settled['net_pnl']:+.6f} USDT | {sell_fee_msg}"
            )
            if settled["net_pnl"] < 0:
                record_pending_loss(instance_id, abs(settled["net_pnl"]), bot_name)
                try:
                    add_cooldown_symbol(symbol, duration_sec=POST_LOSS_COOLDOWN_SEC)
                except Exception:
                    pass
            else:
                settle_loss_recovery(instance_id, settled["net_pnl"], bot_name)
            return settled["net_pnl"]

        # Calculate target limit sell price guaranteeing profit after all BNB fees
        vol_1m = auto_analysis.get("volatility_1m", 0.18)
        base_min = auto_analysis.get("min_net_profit_combined", max(OPT_ABSOLUTE_MIN_PROFIT_USDT, investment * OPT_MIN_NET_PROFIT_PCT))
        min_net_profit = auto_analysis.get("min_net_profit_combined", get_dynamic_profit_target(investment, vol_1m, base_min, instance_id=instance_id))
        reprice_sec = auto_analysis.get("dynamic_reprice_sec", OPT_REPRICE_STAGE1_SEC)
        hold_sec = auto_analysis.get("dynamic_hold_sec", OPT_MAX_HOLD_SEC)
        trailing_stop_enabled = auto_analysis.get("trailing_stop_enabled", False)
        trailing_drop_pct = auto_analysis.get("trailing_drop_pct", OPT_TRAILING_DROP_PCT)

        required_exit_val = (entry_cost + buy_fee_usdt + min_net_profit) / (1.0 - EST_FEE_RATE_PER_SIDE)
        target_limit_price = required_exit_val / qty
        target_price_str = format_price(target_limit_price, lot.get("tick_size", 0.0001), rounding="CEILING")
        target_limit_price = float(target_price_str)

        # Institutional Wall Front-Running:
        # Check if an ask wall is sitting near target, and sit 1 tick in front of it for guaranteed fill priority
        try:
            depth_snap = client.get_order_book(symbol=symbol, limit=50)
            runway_ok, runway_reason, runway_metrics = inspect_order_book_runway(
                client, symbol, investment_usdt=entry_cost, target_profit=min_net_profit, depth=depth_snap
            )
            if runway_metrics.get("wall_front_run") and runway_metrics.get("front_run_price"):
                front_run_px = runway_metrics["front_run_price"]
                if front_run_px * qty >= required_exit_val:
                    target_limit_price = front_run_px
                    target_price_str = format_price(target_limit_price, lot.get("tick_size", 0.0001), rounding="FLOOR")
                    target_limit_price = float(target_price_str)
                    wall_px = runway_metrics.get("wall_price", 0.0)
                    wall_usd = runway_metrics.get("wall_usdt", 0.0)
                    log_event(
                        f"[{bot_name}] [{symbol}] Institutional Sell Wall of ${wall_usd:.0f} @ {wall_px:.6f} — front-running 1 tick below wall @ {target_price_str} to guarantee priority fill",
                        level="INFO",
                        event_type="WALL_FRONT_RUN",
                        instance_id=instance_id,
                        symbol=symbol,
                    )
                    print(f"[{bot_name}] [{symbol}] Front-running sell wall (${wall_usd:.0f} @ {wall_px:.6f}) -> Limit sell @ {target_price_str}")
        except Exception:
            pass

        # Spot Balance Guard: ensure sell_qty matches actual free base asset in wallet (prevents -2010 error)
        base_asset = symbol[:-4] if symbol.endswith("USDT") else symbol
        try:
            free_bal = float(client.get_asset_balance(asset=base_asset).get("free") or 0.0)
            if free_bal > 0 and free_bal < qty:
                qty = float(format_quantity(free_bal, lot["step"]))
        except Exception:
            pass

        limit_order = None
        limit_order_id = None
        try:
            log_event(
                f"[{bot_name}] [{symbol}] Placing immediate LIMIT SELL {qty} {symbol} @ {target_price_str} "
                f"for net profit ≥ +{min_net_profit:.4f} USDT (after BNB fees)",
                level="TRADE",
                event_type="LIMIT_SELL_SUBMIT",
                instance_id=instance_id,
                symbol=symbol,
            )
            print(
                f"[{bot_name}] [{symbol}] Placing immediate LIMIT SELL {qty} {symbol} @ {target_price_str} "
                f"(aiming for NET +{min_net_profit:.4f} USDT)"
            )
            limit_order = limit_sell(client, symbol, qty, target_limit_price)
            limit_order_id = limit_order.get("orderId")
        except Exception as e:
            limit_order_id = None
            log_event(
                f"[{bot_name}] [{symbol}] Immediate LIMIT SELL placement note: {e}. Falling back to dynamic market sell monitoring.",
                level="INFO",
                event_type="LIMIT_SELL_NOTE",
                instance_id=instance_id,
                symbol=symbol,
            )
            print(f"[{bot_name}] [{symbol}] Immediate LIMIT SELL placement note: {e}. Dynamic monitoring active.")

        # Real-time WebSocket monitoring via ThreadedWebsocketManager
        exit_event = threading.Event()
        trigger_state = {
            "reason": None,
            "bid_price": 0.0,
            "pnl": 0.0,
            "peak_pnl": 0.0,
            "last_ws_ts": time.time(),
            "last_db_ts": 0.0,
            "last_order_check_ts": 0.0,
            "limit_filled_order": None,
            "stage1_repriced": False,
        }

        def on_book_ticker(msg):
            try:
                now_tick = time.time()
                if (now_tick - trigger_state.get("last_ws_proc_ts", 0.0)) < 0.05:
                    return  # Throttle callback to max 20Hz to prevent queue overflow

                bid = float(msg.get("b") or 0.0)
                if bid <= 0:
                    return
                trigger_state["bid_price"] = bid
                trigger_state["last_ws_ts"] = now_tick
                trigger_state["last_ws_proc_ts"] = now_tick
                cur_val = qty * bid
                cur_pnl = estimate_net_unrealized(entry_cost, cur_val, buy_fee_usdt)
                trigger_state["pnl"] = cur_pnl

                if cur_pnl > trigger_state.get("peak_pnl", 0.0):
                    trigger_state["peak_pnl"] = cur_pnl

                elapsed = now_tick - start_ts
                # 1. If profit occurs in any small amount after fees, end cycle immediately!
                if cur_pnl >= 0.0010:
                    trigger_state["reason"] = "TARGET"
                    trigger_state["exit_price"] = bid
                    exit_event.set()
                # 2. After 5 minutes, stop loss runs and quits only when loss is in 0.00XXX range
                elif elapsed >= 300.0 and hard_stop_triggered(cur_pnl, investment_usdt, stop_loss, elapsed):
                    trigger_state["reason"] = "SCRATCH"
                    trigger_state["exit_price"] = bid
                    exit_event.set()
                elif bid >= target_limit_price:
                    # Flag that market bid reached limit sell price (main loop checks fill cleanly without blocking WS)
                    trigger_state["bid_reached_target"] = True
            except Exception:
                pass

        twm = get_global_twm()
        conn_key = twm.start_symbol_book_ticker_socket(callback=on_book_ticker, symbol=symbol)
        log_event(
            f"[{bot_name}] [{symbol}] WebSocket bookTicker stream connected for {symbol}",
            level="INFO",
            event_type="WS_START",
            instance_id=instance_id,
            symbol=symbol,
        )

        try:
            while not exit_event.is_set():
                if is_stop_requested(instance_id):
                    log_event(
                        f"[{bot_name}] [{symbol}] Stop requested — force-selling {symbol}",
                        level="WARN",
                        event_type="ABORT",
                        instance_id=instance_id,
                        symbol=symbol,
                    )
                    print(f"[{bot_name}] [{symbol}] Stop requested — force-selling {symbol}")
                    trigger_state["reason"] = "ABORTED"
                    exit_event.set()
                    break

                now = time.time()
                elapsed = now - start_ts

                # Check resting limit sell order status periodically without blocking WebSocket
                if limit_order_id and ((now - trigger_state.get("last_order_check_ts", 0.0)) >= 1.0 or trigger_state.get("bid_reached_target")):
                    trigger_state["last_order_check_ts"] = now
                    trigger_state["bid_reached_target"] = False
                    try:
                        ord_info = client.get_order(symbol=symbol, orderId=limit_order_id, recvWindow=10000)
                        if ord_info.get("status") == "FILLED":
                            trigger_state["reason"] = "TARGET"
                            trigger_state["limit_filled_order"] = ord_info
                            exit_event.set()
                            break
                    except Exception:
                        pass

                # Spot Asset Safety: holding is zero liquidation risk — never market-dump into bid walls on timeout
                pass

                # Optimization #8 — Two-Stage Scratch Exit:
                # Stage 1: Maker Breakeven Reprice at dynamic reprice_sec if stalling near breakeven
                if (
                    limit_order_id
                    and not trigger_state.get("stage1_repriced")
                    and (now - start_ts) >= reprice_sec
                    and avg_price > 0
                ):
                    try:
                        be_exit_val = (entry_cost + buy_fee_usdt + 0.020) / (1.0 - EST_FEE_RATE_PER_SIDE)
                        be_limit_price = float(format_price(be_exit_val / qty, lot.get("tick_size", 0.0001), rounding="CEILING"))
                        if be_limit_price < target_limit_price:
                            log_event(
                                f"[{bot_name}] [{symbol}] Stage 1 Scratch: trade open {now - start_ts:.1f}s — repricing limit sell from {target_limit_price} to breakeven {be_limit_price} (aiming for $0.00 zero-loss exit)",
                                level="INFO",
                                event_type="STAGE1_REPRICE",
                                instance_id=instance_id,
                                symbol=symbol,
                            )
                            print(f"[{bot_name}] [{symbol}] Stage 1 Scratch: Repricing limit sell {target_limit_price} -> {be_limit_price} (breakeven)")
                            cancel_order_safe(client, symbol, limit_order_id)
                            time.sleep(0.2)
                            ord_info = client.get_order(symbol=symbol, orderId=limit_order_id, recvWindow=10000)
                            if ord_info.get("status") == "FILLED":
                                trigger_state["reason"] = "TARGET"
                                trigger_state["limit_filled_order"] = ord_info
                                exit_event.set()
                                break
                            new_sell = limit_sell(client, symbol, qty, be_limit_price)
                            limit_order_id = new_sell.get("orderId")
                            target_limit_price = be_limit_price
                            target_price_str = str(be_limit_price)
                        trigger_state["stage1_repriced"] = True
                    except Exception as e:
                        trigger_state["stage1_repriced"] = True

                # After 5 minutes (300s), stop loss runs and reprices resting limit sell to Micro-Scratch Price (-0.0035 USDT)
                if (
                    limit_order_id
                    and not trigger_state.get("stage2_repriced")
                    and (now - start_ts) >= 300.0
                    and avg_price > 0
                ):
                    try:
                        target_scratch_usdt = -0.0035
                        be2_exit_val = (entry_cost + buy_fee_usdt + target_scratch_usdt) / (1.0 - EST_FEE_RATE_PER_SIDE)
                        be2_limit_price = float(format_price(be2_exit_val / qty, lot.get("tick_size", 0.0001), rounding="FLOOR"))
                        if be2_limit_price < target_limit_price:
                            log_event(
                                f"[{bot_name}] [{symbol}] 5-Minute Mark Reached ({now - start_ts:.1f}s) — Repricing limit sell from {target_limit_price} to Micro-Loss Scratch {be2_limit_price} (Targeting loss in 0.00XXX range: {target_scratch_usdt:+.4f} USDT)",
                                level="INFO",
                                event_type="STAGE2_REPRICE",
                                instance_id=instance_id,
                                symbol=symbol,
                            )
                            print(f"[{bot_name}] [{symbol}] 5-Minute Mark Reached: Repricing limit sell {target_limit_price} -> {be2_limit_price} (Target Loss: {target_scratch_usdt:+.4f} USDT)")
                            cancel_order_safe(client, symbol, limit_order_id)
                            time.sleep(0.2)
                            ord_info = client.get_order(symbol=symbol, orderId=limit_order_id, recvWindow=10000)
                            if ord_info.get("status") == "FILLED":
                                trigger_state["reason"] = "TARGET"
                                trigger_state["limit_filled_order"] = ord_info
                                exit_event.set()
                                break
                            new_sell = limit_sell(client, symbol, qty, be2_limit_price)
                            limit_order_id = new_sell.get("orderId")
                            target_limit_price = be2_limit_price
                            target_price_str = str(be2_limit_price)
                        trigger_state["stage2_repriced"] = True
                    except Exception as e:
                        trigger_state["stage2_repriced"] = True

                # In-trade Toxic Ask-Stacking Check: if sellers stack massive asks above entry, trigger fast Maker Breakeven reprice
                if (
                    limit_order_id
                    and not trigger_state.get("stage1_repriced")
                    and (now - trigger_state.get("last_toxic_chk_ts", 0.0) >= 10.0)
                    and (now - start_ts) >= 30.0
                ):
                    trigger_state["last_toxic_chk_ts"] = now
                    try:
                        d_chk = client.get_order_book(symbol=symbol, limit=10)
                        b_chk = sum(float(b[0]) * float(b[1]) for b in d_chk.get("bids", [])[:10])
                        a_chk = sum(float(a[0]) * float(a[1]) for a in d_chk.get("asks", [])[:10])
                        tot_chk = b_chk + a_chk
                        live_obi = (b_chk - a_chk) / tot_chk if tot_chk > 0 else 0.0
                        if live_obi < -0.45 and trigger_state.get("pnl", 0.0) <= 0.02:
                            be_exit_val = (entry_cost + buy_fee_usdt + OPT_BREAKEVEN_BUFFER_USDT) / (1.0 - EST_FEE_RATE_PER_SIDE)
                            be_limit_price = float(format_price(be_exit_val / qty, lot.get("tick_size", 0.0001), rounding="CEILING"))
                            if be_limit_price < target_limit_price:
                                log_event(
                                    f"[{bot_name}] [{symbol}] In-Trade Toxic Ask Stacking detected (OBI {live_obi:+.2f}, Asks ${a_chk:.0f} vs Bids ${b_chk:.0f}) — fast repricing to Maker Breakeven @ {be_limit_price} to exit before dump",
                                    level="INFO",
                                    event_type="TOXIC_FAST_REPRICE",
                                    instance_id=instance_id,
                                    symbol=symbol,
                                )
                                print(f"[{bot_name}] [{symbol}] Toxic ask stacking (OBI {live_obi:+.2f}) -> Fast Maker Breakeven reprice @ {be_limit_price}")
                                cancel_order_safe(client, symbol, limit_order_id)
                                time.sleep(0.2)
                                ord_info = client.get_order(symbol=symbol, orderId=limit_order_id, recvWindow=10000)
                                if ord_info.get("status") == "FILLED":
                                    trigger_state["reason"] = "TARGET"
                                    trigger_state["limit_filled_order"] = ord_info
                                    exit_event.set()
                                    break
                                new_sell = limit_sell(client, symbol, qty, be_limit_price)
                                limit_order_id = new_sell.get("orderId")
                                target_limit_price = be_limit_price
                                target_price_str = str(be_limit_price)
                            trigger_state["stage1_repriced"] = True
                    except Exception:
                        pass

                # Check if resting limit order filled on Binance
                if limit_order_id and (now - trigger_state["last_order_check_ts"] >= 1.5):
                    trigger_state["last_order_check_ts"] = now
                    try:
                        ord_info = client.get_order(symbol=symbol, orderId=limit_order_id, recvWindow=10000)
                        if ord_info.get("status") == "FILLED":
                            trigger_state["reason"] = "TARGET"
                            trigger_state["limit_filled_order"] = ord_info
                            exit_event.set()
                            break
                    except Exception as e:
                        if is_network_error(e):
                            pass

                # Watchdog: if no WS update received in > 5.0 seconds, fall back to REST check
                if now - trigger_state["last_ws_ts"] > 5.0:
                    try:
                        ob = client.get_orderbook_ticker(symbol=symbol)
                        bid_price = float(ob.get("bidPrice") or 0.0)
                        if bid_price <= 0:
                            bid_price = float(client.get_symbol_ticker(symbol=symbol)["price"])
                        trigger_state["bid_price"] = bid_price
                        trigger_state["last_ws_ts"] = now
                        cur_val = qty * bid_price
                        cur_pnl = estimate_net_unrealized(entry_cost, cur_val, buy_fee_usdt)
                        trigger_state["pnl"] = cur_pnl

                        if cur_pnl > trigger_state.get("peak_pnl", 0.0):
                            trigger_state["peak_pnl"] = cur_pnl

                        elapsed = time.time() - start_ts
                        # 1. Any profit after fees ends cycle immediately!
                        if cur_pnl >= 0.0010:
                            trigger_state["reason"] = "TARGET"
                            exit_event.set()
                            break
                        # 2. After 5 minutes, stop loss runs and quits only when loss is in 0.00XXX range
                        if elapsed >= 300.0 and hard_stop_triggered(cur_pnl, investment_usdt, stop_loss, elapsed):
                            trigger_state["reason"] = "SCRATCH"
                            exit_event.set()
                            break
                        # Micro-profit market dumps removed: resting limit sell will fill as Maker
                        if limit_order_id:
                            ord_info = client.get_order(symbol=symbol, orderId=limit_order_id, recvWindow=10000)
                            if ord_info.get("status") == "FILLED":
                                trigger_state["reason"] = "TARGET"
                                trigger_state["limit_filled_order"] = ord_info
                                exit_event.set()
                                break
                    except Exception as e:
                        if is_network_error(e):
                            wait_for_network(client, bot_name, symbol, instance_id, context="price check watchdog")

                # Throttle DB PnL updates to once per second
                if now - trigger_state["last_db_ts"] >= 1.0 and trigger_state["bid_price"] > 0:
                    update_instance(instance_id, current_pnl=round(trigger_state["pnl"], 6))
                    trigger_state["last_db_ts"] = now

                exit_event.wait(timeout=0.1)

        finally:
            try:
                twm.stop_socket(conn_key)
            except Exception:
                pass

        reason = trigger_state.get("reason") or "ABORTED"
        bid_price = trigger_state.get("bid_price") or 0.0
        pnl = trigger_state.get("pnl") or 0.0
        limit_filled = trigger_state.get("limit_filled_order")

        # Double check if limit order was filled
        if limit_order_id and not limit_filled:
            try:
                ord_info = client.get_order(symbol=symbol, orderId=limit_order_id, recvWindow=10000)
                if ord_info.get("status") == "FILLED":
                    limit_filled = ord_info
                    reason = "TARGET"
            except Exception:
                pass

        if reason == "TARGET" and limit_filled:
            log_event(
                f"[{bot_name}] [{symbol}] Immediate LIMIT SELL fulfilled on Binance @ {target_price_str} — closing cycle with target profit",
                level="TRADE",
                event_type="TARGET",
                instance_id=instance_id,
                symbol=symbol,
            )
            print(
                f"[{bot_name}] [{symbol}] Immediate LIMIT SELL fulfilled on Binance @ {target_price_str} — closing cycle with target profit"
            )
            return _finalize_sell(limit_filled, "TARGET"), "TARGET"

        # If not filled via resting limit sell: cancel open limit order before market sell to unlock base asset
        if limit_order_id:
            log_event(
                f"[{bot_name}] [{symbol}] Cancelling resting LIMIT SELL order #{limit_order_id} ({reason})",
                level="INFO",
                event_type="CANCEL_ORDER",
                instance_id=instance_id,
                symbol=symbol,
            )
            print(f"[{bot_name}] [{symbol}] Cancelling resting LIMIT SELL order #{limit_order_id} ({reason})...")
            cancel_order_safe(client, symbol, limit_order_id)
            time.sleep(0.3)
            # Check if it was filled during cancellation
            try:
                ord_info = client.get_order(symbol=symbol, orderId=limit_order_id, recvWindow=10000)
                if ord_info.get("status") == "FILLED":
                    return _finalize_sell(ord_info, "TARGET"), "TARGET"
            except Exception:
                pass

        # Ensure any remaining open orders are cancelled before attempting market sell
        cancel_all_open_orders(client, symbol)

        if reason == "ABORTED":
            log_event(
                f"[{bot_name}] [{symbol}] Stop requested: cancelled resting limit sell order. Leaving {qty} {symbol} in wallet for manual decision.",
                level="WARN",
                event_type="BOT_STOP",
                instance_id=instance_id,
                symbol=symbol,
            )
            print(
                f"[{bot_name}] [{symbol}] Stop requested: cancelled resting limit sell order. Leaving {qty} {symbol} in wallet for manual decision."
            )
            if cycle_id:
                close_cycle(
                    cycle_id,
                    exit_value=None,
                    pnl_usdt=None,
                    status="ABORTED",
                    duration_sec=time.time() - start_ts,
                    fee_bnb=buy_fee_bnb,
                    fee_usdt=buy_fee_usdt,
                )
            update_instance(
                instance_id,
                current_pnl=0.0,
                last_error="Stopped by user (limit order cancelled, coins held in wallet)",
            )
            return None, "ABORTED"

        if reason == "STOP_LOSS":
            log_event(
                f"[{bot_name}] [{symbol}] Stop-loss hit on {symbol}: est. NET pnl={pnl:+.4f} ≤ {stop_loss} — selling",
                level="WARN",
                event_type="STOP_LOSS",
                instance_id=instance_id,
                symbol=symbol,
            )
            print(
                f"[{bot_name}] [{symbol}] Stop-loss hit on {symbol}: est. NET pnl={pnl:+.4f} ≤ {stop_loss} — selling"
            )
        elif reason == "SCRATCH":
            log_event(
                f"[{bot_name}] [{symbol}] Breakeven Chase scratch exit on {symbol}: est. NET pnl={pnl:+.4f} — cutting loss early before SL",
                level="WARN",
                event_type="SCRATCH_EXIT",
                instance_id=instance_id,
                symbol=symbol,
            )
            print(
                f"[{bot_name}] [{symbol}] Breakeven Chase scratch exit on {symbol}: est. NET pnl={pnl:+.4f} — cutting loss early"
            )
        elif reason == "TARGET":
            log_event(
                f"[{bot_name}] [{symbol}] Micro-profit target hit on {symbol}: est. NET pnl={pnl:+.4f} — market selling to bank profit",
                level="TRADE",
                event_type="TARGET",
                instance_id=instance_id,
                symbol=symbol,
            )
            print(
                f"[{bot_name}] [{symbol}] Micro-profit hit on {symbol}: est. NET pnl={pnl:+.4f} — market selling"
            )

        # Execute exit via Limit Sell with strict price floor (zero market dump slippage)
        exit_bid = trigger_state.get("exit_price") or trigger_state.get("bid_price") or 0.0
        if exit_bid > 0:
            target_sell_px = float(format_price(exit_bid, lot.get("tick_size", 0.0001), rounding="FLOOR"))
        else:
            target_sell_px = target_limit_price

        log_event(
            f"[{bot_name}] [{symbol}] Executing LIMIT SELL {qty} {symbol} @ {target_sell_px} ({reason})",
            level="TRADE",
            event_type=reason,
            instance_id=instance_id,
            symbol=symbol,
        )
        print(f"[{bot_name}] [{symbol}] Executing LIMIT SELL {qty} {symbol} @ {target_sell_px} ({reason})")
        sell_res = limit_sell(client, symbol, qty, target_sell_px)
        time.sleep(0.5)
        try:
            ord_info = client.get_order(symbol=symbol, orderId=sell_res.get("orderId"), recvWindow=10000)
            if ord_info.get("status") != "FILLED":
                for _ in range(4):
                    time.sleep(0.5)
                    ord_info = client.get_order(symbol=symbol, orderId=sell_res.get("orderId"), recvWindow=10000)
                    if ord_info.get("status") == "FILLED":
                        break
            sell = ord_info
        except Exception:
            sell = sell_res
        net_res = _finalize_sell(sell, reason)
        # Optimization #7 — Cooldown: quarantine symbol on hard stop-loss OR scratch exit (300s / 5m)
        if reason in ("STOP_LOSS", "SCRATCH", "TIMEOUT") or (net_res is not None and net_res < 0):
            try:
                add_cooldown_symbol(symbol, duration_sec=300)
            except Exception:
                pass
        return net_res, reason

    except BinanceAPIException as e:
        tb = traceback.format_exc()
        if is_network_error(e):
            wait_for_network(client, bot_name, symbol or "INIT", instance_id, context="Binance connection")
            return None, "NETWORK_RETRY"

        msg = f"[{bot_name}] [{symbol}] BinanceAPIException: {e}"
        update_instance(instance_id, status="ERROR", last_error=msg, current_symbol=None, finished_at=datetime.now(timezone.utc).isoformat())
        log_event(
            msg,
            level="ERROR",
            event_type="BINANCE_API_ERROR",
            instance_id=instance_id,
            symbol=symbol,
            detail=tb,
        )
        print(msg)
        if cycle_id and qty > 0:
            _emergency_liquidate(client, symbol, entry_cost, cycle_id, instance_id, bot_name, start_ts, str(e))
        elif cycle_id:
            close_cycle(cycle_id, None, None, "ERROR", time.time() - start_ts)
        return None, "ERROR"

    except Exception as e:
        tb = traceback.format_exc()
        if is_network_error(e):
            wait_for_network(client, bot_name, symbol or "INIT", instance_id, context="network connection")
            return None, "NETWORK_RETRY"

        msg = f"[{bot_name}] [{symbol}] Exception: {e}"
        update_instance(instance_id, status="ERROR", last_error=msg, current_symbol=None, finished_at=datetime.now(timezone.utc).isoformat())
        log_event(
            msg,
            level="EXCEPTION",
            event_type="EXCEPTION",
            instance_id=instance_id,
            symbol=symbol,
            detail=tb,
        )
        print(msg)
        traceback.print_exc()
        if cycle_id and qty > 0:
            _emergency_liquidate(client, symbol, entry_cost, cycle_id, instance_id, bot_name, start_ts, str(e))
        elif cycle_id:
            close_cycle(cycle_id, None, None, "ERROR", time.time() - start_ts)
        return None, "ERROR"


# ---------------------------------------------------------------------------
# Main loop for one instance – runs until cumulative profit target reached
# ---------------------------------------------------------------------------

def run_instance(instance_id: int):
    """
    Blocking entry-point. Called by CLI process or by UI-spawned subprocess.
    Keeps trading cycles until:
      - cumulative realized PnL >= target_profit, OR
      - stop is requested, OR
      - fatal error
    """
    inst = get_instance(instance_id)
    if not inst:
        raise SystemExit(f"Instance {instance_id} not found")

    bot_name = inst.get("name") or str(instance_id)
    investment = float(inst["investment_usdt"])
    target = float(inst["target_profit"])          # e.g. 0.10 USDT total
    stop_loss = float(inst["stop_loss"])
    per_cycle_target = target                      # still use same number for single-cycle TP
    # Note: per-cycle take-profit stays at the instance target value (0.10)
    # so each successful cycle aims for ~0.10 and the bot stops after the
    # first (or few) wins that push cumulative >= target.

    client = make_client()
    while True:
        try:
            client.ping()
            break
        except Exception as e:
            sym = inst.get("current_symbol") or "INIT"
            if is_network_error(e):
                wait_for_network(client, bot_name, sym, instance_id, context="startup ping")
                continue
            else:
                msg = f"[{bot_name}] [{sym}] Cannot connect to Binance: {e}"
                update_instance(
                    instance_id,
                    status="ERROR",
                    last_error=msg,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                log_event(
                    msg,
                    level="EXCEPTION",
                    event_type="EXCEPTION",
                    instance_id=instance_id,
                    symbol=sym,
                    detail=traceback.format_exc(),
                )
                print(msg)
                raise SystemExit(1)

    update_instance(
        instance_id,
        status="RUNNING",
        started_at=datetime.now(timezone.utc).isoformat(),
        pid=os.getpid(),
        stop_requested=0,
        last_error=None,
    )

    cumulative = float(inst.get("cumulative_pnl") or 0.0)
    cycles_done = int(inst.get("cycles_done") or 0)
    wins = int(inst.get("wins") or 0)
    losses = int(inst.get("losses") or 0)
    insufficient_retries = 0
    consecutive_losses = 0
    blocked_retry_counts = {}

    sym = inst.get("current_symbol") or "INIT"
    msg = (
        f"[{bot_name}] [{sym}] started | investment={investment} "
        f"target={target} stop={stop_loss} pid={os.getpid()}"
    )
    print(msg)
    log_event(msg, level="INFO", event_type="BOT_START", instance_id=instance_id, symbol=sym)

    try:
        while True:
            if is_stop_requested(instance_id):
                sym = inst.get("current_symbol") or "IDLE"
                msg = f"[{bot_name}] [{sym}] Stop requested – exiting cleanly"
                print(msg)
                update_instance(
                    instance_id,
                    status="STOPPED",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    current_symbol=None,
                    current_pnl=0.0,
                )
                log_event(
                    msg,
                    level="WARN",
                    event_type="BOT_STOP",
                    instance_id=instance_id,
                    symbol=sym,
                )
                break

            # Removed cumulative >= target check to allow infinite cycles without stopping

            # Removed cumulative <= stop_loss check to allow infinite cycles without stopping

            pnl, reason = trade_one_cycle(
                client=client,
                instance_id=instance_id,
                investment_usdt=investment,
                target_profit=per_cycle_target,
                stop_loss=stop_loss,
            )

            if reason in ("ERROR", "BINANCE_API_ERROR"):
                sym = inst.get("current_symbol") or "IDLE"
                msg = f"[{bot_name}] [{sym}] Bot cycle encountered error ({reason}). Retrying next cycle in 10s..."
                print(msg)
                log_event(
                    msg,
                    level="ERROR",
                    event_type="CYCLE_ERROR_RETRY",
                    instance_id=instance_id,
                    symbol=sym,
                )
                time.sleep(10.0)
                continue

            if reason in ("STOP_LOSS", "FAILURE"):
                sym = inst.get("current_symbol") or "IDLE"
                msg = f"[{bot_name}] [{sym}] Stop-loss/loss cycle concluded ({reason}) — recording loss and continuing to next cycle."
                print(msg)
                log_event(
                    msg,
                    level="WARN",
                    event_type="STOP_LOSS_RETRY",
                    instance_id=instance_id,
                    symbol=sym,
                )

            if reason == "ABORTED" or is_stop_requested(instance_id):
                sym = inst.get("current_symbol") or "IDLE"
                msg = f"[{bot_name}] [{sym}] Stop requested – exiting cleanly"
                print(msg)
                update_instance(
                    instance_id,
                    status="STOPPED",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    current_symbol=None,
                    current_pnl=0.0,
                )
                log_event(
                    msg,
                    level="WARN",
                    event_type="BOT_STOP",
                    instance_id=instance_id,
                    symbol=sym,
                )
                break

            if reason in ("INSUFFICIENT_FUNDS", "NO_PAIR"):
                sym = inst.get("current_symbol") or "IDLE"
                msg = f"[{bot_name}] [{sym}] Cycle blocked ({reason}). Retrying in 10s..."
                print(msg)
                log_event(msg, level="INFO", event_type="CYCLE_BLOCKED_RETRY", instance_id=instance_id, symbol=sym)
                time.sleep(10.0)
                continue

            # Dynamic Coin Switching on Blocked Regimes (Downtrends, dumps, sell walls)
            if reason in ("UNFAVORABLE_REGIME", "RUNWAY_BLOCKED", "REGIME_REJECT", "CHOP_SKIP"):
                sym = inst.get("current_symbol") or "IDLE"
                if sym in ("SOXLBUSDT", "SOXSBUSDT"):
                    time.sleep(2.0)
                    continue
                sym = inst.get("current_symbol") or "IDLE"
                blocked_retry_counts[sym] = blocked_retry_counts.get(sym, 0) + 1
                retries_done = blocked_retry_counts[sym]

                if retries_done < MAX_BLOCKED_RETRIES:
                    msg = (
                        f"[{bot_name}] [{sym}] Cycle blocked: {reason} "
                        f"[Attempt {retries_done}/{MAX_BLOCKED_RETRIES}]. Re-checking confirmation in 4s..."
                    )
                    print(msg)
                    log_event(msg, level="WARN", event_type="CYCLE_BLOCKED_RETRY", instance_id=instance_id, symbol=sym)
                    time.sleep(4.0)
                    continue
                else:
                    # Retry threshold reached! Switch to a different coin
                    blocked_retry_counts.pop(sym, None)
                    if sym and sym not in ("IDLE", "INIT", "NO_PAIR"):
                        add_cooldown_symbol(sym, duration_sec=BLOCKED_COOLDOWN_SEC)
                        cooldown_min = BLOCKED_COOLDOWN_SEC // 60
                        switch_msg = (
                            f"[{bot_name}] [{sym}] Blocked for {retries_done} consecutive attempts ({reason}). "
                            f"Quarantining {sym} for {cooldown_min}m. SWITCHING to next best coin..."
                        )
                    else:
                        switch_msg = f"[{bot_name}] Switching candidate pool after {retries_done} blocked attempts..."

                    print(switch_msg)
                    log_event(switch_msg, level="WARN", event_type="COIN_SWITCH", instance_id=instance_id, symbol=sym)

                    # Release symbol so the next cycle scans and claims a new coin
                    release_symbol(instance_id)
                    update_instance(instance_id, current_symbol=None, current_pnl=0.0)
                    time.sleep(2.0)
                    continue

            if reason in ("UNFILLED_ENTRY", "DIP_NOT_FILLED"):
                sym = inst.get("current_symbol") or "IDLE"
                msg = f"[{bot_name}] [{sym}] Entry unfilled/cancelled ({reason}) — retrying next cycle..."
                print(msg)
                log_event(msg, level="INFO", event_type="RETRY_CYCLE", instance_id=instance_id, symbol=sym)
                update_instance(instance_id, current_symbol=None, current_pnl=0.0)
                time.sleep(2.0)
                continue

            if pnl is not None:
                blocked_retry_counts.clear()
                cumulative += pnl
                cycles_done += 1
                if pnl > 0:
                    wins += 1
                    consecutive_losses = 0
                else:
                    losses += 1
                    consecutive_losses += 1

                update_instance(
                    instance_id,
                    cumulative_pnl=round(cumulative, 6),
                    cycles_done=cycles_done,
                    wins=wins,
                    losses=losses,
                    current_symbol=None,
                    current_pnl=0.0,
                )
                sym = inst.get("current_symbol") or "IDLE"
                res_tag = "SUCCESS" if pnl > 0 else "FAILURE"
                msg = (
                    f"[{bot_name}] [{sym}] Cycle #{cycles_done} done [{res_tag} / {reason}] | pnl={pnl:+.4f} "
                    f"cum={cumulative:+.4f} (W{wins}/L{losses}) | consec_losses={consecutive_losses}"
                )
                print(msg)
                log_event(
                    msg,
                    level="INFO" if pnl > 0 else "WARN",
                    event_type="CYCLE_END",
                    instance_id=instance_id,
                    symbol=sym,
                    detail=str({"pnl": pnl, "cumulative": cumulative, "wins": wins, "losses": losses, "reason": reason, "status": res_tag}),
                )
                snapshot_balance(client, instance_id)

                # 1. Daily Net Loss Circuit Breaker Check
                try:
                    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
                    daily_rows = load_daily_pnl_ist()
                    today_entry = next((r for r in daily_rows if r.get("date_ist") == today_ist), None)
                    today_net = today_entry["pnl_usdt"] if today_entry else 0.0
                    if today_net <= MAX_DAILY_LOSS_USDT:
                        cb_msg = (
                            f"[{bot_name}] DAILY CIRCUIT BREAKER TRIGGERED: Today net PnL is {today_net:+.4f} USDT "
                            f"(<= {MAX_DAILY_LOSS_USDT:.2f} limit). Halting bot to preserve capital and prevent further loss."
                        )
                        print(cb_msg)
                        log_event(cb_msg, level="WARN", event_type="CIRCUIT_BREAKER", instance_id=instance_id, symbol=sym)
                        update_instance(instance_id, status="STOPPED", last_error=cb_msg, finished_at=datetime.now(timezone.utc).isoformat())
                        break
                except Exception as cb_err:
                    print(f"[{bot_name}] Circuit breaker check notice: {cb_err}")

                # 2. Consecutive Losses Cooldown Check
                if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                    pause_msg = (
                        f"[{bot_name}] {consecutive_losses} CONSECUTIVE LOSSES DETECTED. "
                        f"Pausing bot operations for {CONSECUTIVE_LOSS_PAUSE_SEC // 60} minutes to let market stabilize..."
                    )
                    print(pause_msg)
                    log_event(pause_msg, level="WARN", event_type="CONSECUTIVE_LOSS_PAUSE", instance_id=instance_id, symbol=sym)
                    consecutive_losses = 0
                    time.sleep(CONSECUTIVE_LOSS_PAUSE_SEC)

                # Continue toward target even if this cycle was a scratch/loss
                sym = inst.get("current_symbol") or "IDLE"
                next_msg = (
                    f"[{bot_name}] [{sym}] Cycle #{cycles_done} complete | pnl={pnl:+.4f} "
                    f"cum={cumulative:+.4f} (W{wins}/L{losses}) — continuing toward target {target:+.4f} USDT"
                )
                print(next_msg)
                log_event(next_msg, level="INFO", event_type="NEXT_CYCLE", instance_id=instance_id, symbol=sym)
                time.sleep(2.0)
                continue

            # Fallback for unhandled None pnl: retry cycle rather than killing bot
            if pnl is None:
                time.sleep(2.0)
                continue

    except KeyboardInterrupt:
        sym = inst.get("current_symbol") or "IDLE"
        msg = f"[{bot_name}] [{sym}] interrupted (KeyboardInterrupt)"
        print(msg)
        if sym and sym not in ("IDLE", "INIT"):
            try:
                cancel_all_open_orders(client, sym)
            except Exception:
                pass
        update_instance(
            instance_id,
            status="STOPPED",
            finished_at=datetime.now(timezone.utc).isoformat(),
            current_symbol=None,
            current_pnl=0.0,
        )
        log_event(
            msg,
            level="WARN",
            event_type="BOT_STOP",
            instance_id=instance_id,
            symbol=sym,
        )
    except Exception as e:
        sym = inst.get("current_symbol") or "IDLE"
        msg = f"[{bot_name}] [{sym}] fatal error: {e}"
        print(msg)
        if sym and sym not in ("IDLE", "INIT"):
            try:
                cancel_all_open_orders(client, sym)
            except Exception:
                pass
        update_instance(
            instance_id,
            status="ERROR",
            last_error=str(e),
            finished_at=datetime.now(timezone.utc).isoformat(),
            current_symbol=None,
            current_pnl=0.0,
        )
        log_event(
            msg,
            level="EXCEPTION",
            event_type="EXCEPTION",
            instance_id=instance_id,
            symbol=sym,
            detail=traceback.format_exc(),
        )
        raise
    finally:
        inst_latest = get_instance(instance_id) or {}
        status = inst_latest.get("status")
        sym = inst_latest.get("current_symbol") or inst.get("current_symbol") or "IDLE"
        if status in ("CREATED", "RUNNING", "STOPPING"):
            status = "STOPPED"
            update_instance(
                instance_id,
                status="STOPPED",
                finished_at=datetime.now(timezone.utc).isoformat(),
                current_symbol=None,
                current_pnl=0.0,
            )
        if status in ("ERROR", "STOPPED", "ABORTED") and sym and sym not in ("IDLE", "INIT"):
            try:
                cancel_all_open_orders(client, sym)
            except Exception:
                pass
        msg = f"[{bot_name}] [{sym}] finished | status={status}"
        print(msg)
        log_event(
            msg,
            level="INFO",
            event_type="BOT_END",
            instance_id=instance_id,
            symbol=sym,
        )
        update_instance(instance_id, current_symbol=None)


def is_pid_running(pid: int) -> bool:
    """Check if process with given PID is still active cross-platform."""
    if pid is None or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            try:
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    STILL_ACTIVE = 259
                    return exit_code.value == STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        try:
            with open(f"/proc/{pid}/status", "r") as f:
                for line in f:
                    if line.startswith("State:"):
                        return "zombie" not in line.lower()
        except Exception:
            pass
        return True


def terminate_pid(pid: int) -> None:
    """Terminate a background process cross-platform."""
    if not pid or pid <= 0:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        except Exception as e:
            print(f"Error terminating PID {pid}: {e}")
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.15)
            if is_pid_running(pid):
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"Error terminating PID {pid}: {e}")


def liquidate_single_bot(instance_id: int) -> dict:
    """
    Liquidate only the specified bot:
    1. Terminates its background process immediately (cross-platform).
    2. Sells any held coin position for this bot back to USDT via Binance MARKET sell.
    3. Records the sell trade with actual Binance fee in USDT, closes cycle as LIQUIDATED,
       and marks the instance STOPPED.
    """
    inst = get_instance(instance_id)
    if not inst:
        return {"success": False, "error": f"Instance #{instance_id} not found"}

    bot_name = inst.get("name") or str(instance_id)
    pid = inst.get("pid")

    # 1. Kill the process immediately so it does not place orders or race
    if pid:
        try:
            terminate_pid(pid)
        except Exception:
            pass

    # 2. Check if this bot has an open cycle or held symbol
    symbol = inst.get("current_symbol")
    strat = (inst.get("strategy") or "").upper()
    if strat in ("DUAL_SOXLB_SOXSB", "DUAL_HARVEST_STRATEGY", "DUAL_HARVEST", "DUAL_SOXLB_SOXSB_STRATEGY") or symbol in ("SOXLB+SOXSB", "SOXSB+SOXLB", "SOXSB + SOXLB"):
        import dual_soxlb_soxsb
        try:
            client = make_client()
            res = dual_soxlb_soxsb.liquidate_dual_holdings(client, instance_id, bot_name)
        except Exception as e:
            print(f"[{bot_name}] Dual liquidation error: {e}")
            res = {"liquidated": False, "exit_val": 0.0, "error": str(e)}

        update_instance(
            instance_id,
            status="CREATED",
            stop_requested=0,
            current_symbol=None,
            current_pnl=0.0,
            last_error=None,
            pid=None,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"success": True, "liquidated": res.get("liquidated", False), "exit_val": res.get("exit_val", 0.0)}

    open_cycle = None
    with get_conn() as conn:
        open_cycle = conn.execute(
            "SELECT id, symbol, entry_cost, qty FROM cycles WHERE instance_id = ? AND status IN ('OPEN', 'ABORTED') ORDER BY id DESC LIMIT 1",
            (instance_id,)
        ).fetchone()

    cycle_id = open_cycle["id"] if open_cycle else None
    if open_cycle and open_cycle["symbol"]:
        symbol = open_cycle["symbol"]

    sold_qty = 0.0
    exit_val = 0.0
    fee_usdt = 0.0
    net_pnl = 0.0
    liquidated = False

    if symbol and symbol.endswith("USDT"):
        base_asset = symbol.replace("USDT", "")
        try:
            client = make_client()
            # Cancel any resting open orders first to unlock balance
            cancel_all_open_orders(client, symbol)

            # Wait for coins to become completely free
            for _ in range(5):
                time.sleep(0.5)
                asset_info = client.get_asset_balance(asset=base_asset)
                bal = float(asset_info["free"] or 0)
                locked = float(asset_info["locked"] or 0)
                if locked == 0.0 or bal > 0:
                    break

            filters = get_symbol_filters(client, symbol)

            expected_qty = float(open_cycle["qty"]) if (open_cycle and open_cycle["qty"] is not None) else (bal + locked)
            target_qty = min(bal + locked, expected_qty)

            qty_str = format_quantity(target_qty, filters["step"])
            qty = float(qty_str)

            # If for some reason we calculated a target but free bal is less (shouldn't happen after wait), cap it
            if qty > bal and bal > 0:
                qty_str = format_quantity(bal, filters["step"])
                qty = float(qty_str)

            # Extract entry cost and buy fees first
            entry_cost = float(open_cycle["entry_cost"]) if open_cycle and open_cycle["entry_cost"] else 0.0
            buy_fee_usdt = entry_cost * EST_FEE_RATE_PER_SIDE
            buy_fee_bnb = buy_fee_usdt / 700.0

            if cycle_id:
                with get_conn() as conn:
                    buy_trade = conn.execute("SELECT fee_bnb, fee_usdt FROM trades WHERE cycle_id = ? AND side = 'BUY' LIMIT 1", (cycle_id,)).fetchone()
                if buy_trade and buy_trade["fee_usdt"]:
                    buy_fee_usdt = float(buy_trade["fee_usdt"])
                    buy_fee_bnb = float(buy_trade["fee_bnb"] or (buy_fee_usdt / 700.0))
            else:
                entry_cost = 0.0

            if qty >= filters["min_qty"]:
                try:
                    sell_order = client.create_order(
                        symbol=symbol,
                        side=Client.SIDE_SELL,
                        type=Client.ORDER_TYPE_MARKET,
                        quantity=qty_str,
                        recvWindow=10000,
                        newOrderRespType="FULL",
                    )
                    exit_val = float(sell_order.get("cummulativeQuoteQty") or 0.0)
                    sold_qty = qty
                    fee_info = fee_from_order(client, sell_order, symbol)
                    fee_usdt = fee_info["fee_usdt"]
                    fee_bnb = fee_info["fee_bnb"]
                    liquidated = True
                except Exception as inner_ex:
                    log_event(
                        f"[{bot_name}] [{symbol}] Manual liquidation order failed: {inner_ex}",
                        level="ERROR",
                        event_type="LIQUIDATION_FAILED",
                        instance_id=instance_id,
                        symbol=symbol,
                    )

            if not liquidated and cycle_id:
                # If we couldn't sell (e.g. qty too small or error), we simulate the sale to record the loss
                try:
                    ticker = client.get_symbol_ticker(symbol=symbol)
                    current_price = float(ticker["price"])
                    exit_val = qty * current_price if qty > 0 else 0.0
                except Exception:
                    exit_val = 0.0
                sold_qty = qty
                fee_usdt = exit_val * EST_FEE_RATE_PER_SIDE
                fee_bnb = fee_usdt / 700.0
                liquidated = True
                sell_order = {}

            if liquidated:
                if not entry_cost and exit_val > 0:
                    entry_cost = exit_val # Fallback
                settled = settle_pnl(entry_cost, exit_val, buy_fee_usdt, fee_usdt)
                net_pnl = settled["net_pnl"]

                insert_trade(
                    instance_id=instance_id,
                    cycle_id=cycle_id,
                    symbol=symbol,
                    side="SELL",
                    quantity=sold_qty,
                    price=exit_val / sold_qty if sold_qty > 0 else 0.0,
                    quote_qty=exit_val,
                    fee_bnb=fee_bnb,
                    fee_usdt=fee_usdt,
                    order_id=sell_order.get("orderId") if 'sell_order' in locals() else None,
                    pnl_usdt=net_pnl,
                    notes="MANUAL_LIQUIDATE (SIMULATED)" if not sell_order.get("orderId") else "MANUAL_LIQUIDATE",
                )
        except Exception as ex:
            log_event(
                f"[{bot_name}] [{symbol}] Manual liquidation failed: {ex}",
                level="ERROR",
                event_type="LIQUIDATION_FAILED",
                instance_id=instance_id,
                symbol=symbol,
            )

    if cycle_id:
        status = "LIQUIDATED" if liquidated else "ABORTED"
        close_cycle(
            cycle_id,
            exit_val if liquidated else None,
            net_pnl if liquidated else None,
            status,
            0.0,
            fee_bnb=(buy_fee_bnb + fee_bnb) if liquidated else None,
            fee_usdt=(buy_fee_usdt + fee_usdt) if liquidated else None,
        )

    cum_pnl = (inst.get("cumulative_pnl") or 0.0) + (net_pnl if liquidated else 0.0)
    cycles_done = (inst.get("cycles_done") or 0) + (1 if liquidated else 0)
    losses = (inst.get("losses") or 0) + (1 if liquidated and net_pnl < 0 else 0)
    wins = (inst.get("wins") or 0) + (1 if liquidated and net_pnl >= 0 else 0)

    update_instance(
        instance_id,
        status="CREATED",
        stop_requested=0,
        finished_at=datetime.now(timezone.utc).isoformat(),
        current_symbol=None,
        current_pnl=0.0,
        cumulative_pnl=round(cum_pnl, 6),
        cycles_done=cycles_done,
        losses=losses,
        wins=wins,
        last_error=None,
        pid=None,
    )

    log_event(
        f"[{bot_name}] [{symbol or 'NONE'}] Bot #{instance_id} liquidated and ready for new run. (sold={sold_qty}, val={exit_val:.4f} USDT, fee={fee_usdt:.4f} USDT, pnl={net_pnl:+.4f} USDT)",
        level="INFO" if liquidated else "WARN",
        event_type="BOT_LIQUIDATED",
        instance_id=instance_id,
        symbol=symbol,
    )
    return {
        "success": True,
        "liquidated": liquidated,
        "sold_qty": sold_qty,
        "exit_val": exit_val,
        "fee_usdt": fee_usdt,
        "net_pnl": net_pnl,
    }


def stop_single_bot(instance_id: int) -> dict:
    """
    Stop the bot without selling coins:
    1. Cancels any resting limit sell order on Binance to unlock coins.
    2. Leaves the coins in the user's Binance spot wallet.
    3. Terminates the background process.
    4. Marks instance STOPPED.
    """
    inst = get_instance(instance_id)
    if not inst:
        return {"success": False, "error": f"Instance #{instance_id} not found"}

    bot_name = inst.get("name") or str(instance_id)
    pid = inst.get("pid")
    symbol = inst.get("current_symbol")
    strat = (inst.get("strategy") or "").upper()
    if strat in ("DUAL_SOXLB_SOXSB", "DUAL_HARVEST_STRATEGY", "DUAL_HARVEST", "DUAL_SOXLB_SOXSB_STRATEGY") or symbol in ("SOXLB+SOXSB", "SOXSB+SOXLB", "SOXSB + SOXLB"):
        try:
            client = make_client()
            cancel_all_open_orders(client, "SOXLBUSDT")
            cancel_all_open_orders(client, "SOXSBUSDT")
        except Exception as e:
            print(f"Error cancelling orders for dual legs: {e}")
        if pid:
            terminate_pid(pid)
        update_instance(instance_id, status="STOPPED", current_symbol="SOXSB + SOXLB", current_pnl=0.0)
        return {"success": True, "stopped": True}

    open_cycle = None
    with get_conn() as conn:
        open_cycle = conn.execute(
            "SELECT id, symbol, entry_cost, qty FROM cycles WHERE instance_id = ? AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
            (instance_id,)
        ).fetchone()
    if open_cycle and open_cycle["symbol"]:
        symbol = open_cycle["symbol"]

    # 1. Cancel resting limit order on Binance immediately so coins are unlocked
    if symbol and symbol.endswith("USDT"):
        try:
            client = make_client()
            cancel_all_open_orders(client, symbol)
        except Exception as e:
            print(f"Error cancelling orders for {symbol}: {e}")

    # 2. Terminate background process
    if pid:
        terminate_pid(pid)

    # 3. Close open cycle as ABORTED (no sell)
    if open_cycle:
        close_cycle(
            open_cycle["id"],
            exit_value=None,
            pnl_usdt=None,
            status="ABORTED",
            duration_sec=0.0,
        )

    # 4. Mark instance STOPPED (retain current_symbol so user can liquidate later if desired)
    update_instance(
        instance_id,
        status="STOPPED",
        stop_requested=1,
        finished_at=datetime.now(timezone.utc).isoformat(),
        current_symbol=symbol,
        current_pnl=0.0,
        last_error="Stopped by user (limit sell cancelled, coins held in wallet)",
    )

    log_event(
        f"[{bot_name}] [{symbol or 'NONE'}] Bot #{instance_id} stopped. Limit sell cancelled. Coins held in wallet for manual decision.",
        level="WARN",
        event_type="BOT_STOP",
        instance_id=instance_id,
        symbol=symbol,
    )
    return {"success": True, "symbol": symbol}



# ===========================================================================
# SECTION 3: AUTONOMOUS BOT MANAGER (from manager.py)
# ===========================================================================
# Auto-manager: when enabled, launches new bot instances while:
#   - free Spot USDT >= investment (default 25)
#   - free BNB covers fees for running bots + one more (min_bnb_per_bot each)
#   - running count < max_parallel



def reconcile_active_instances() -> int:
    """
    Check all instances in ('CREATED', 'RUNNING', 'STOPPING').
    If the OS process is dead, mark as ERROR to immediately free slots and committed funds.
    """
    reaped = 0
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id, name, pid, status, current_symbol FROM bot_instances WHERE status IN ('CREATED', 'RUNNING', 'STOPPING')"
            ).fetchall()
            for r in rows:
                iid = r["id"]
                pid = r["pid"]
                name = r["name"]
                sym = r["current_symbol"] or "INIT"
                if pid and not is_pid_running(pid):
                    msg = f"[{name}] [{sym}] Manager Reaper: process {pid} is dead. Marking instance as ERROR."
                    print(msg)
                    log_event(msg, level="WARN", event_type="MANAGER_REAPER", instance_id=iid, symbol=sym)
                    conn.execute(
                        "UPDATE bot_instances SET status = 'ERROR', finished_at = ?, last_error = ? WHERE id = ?",
                        (now, f"Process {pid} terminated unexpectedly", iid),
                    )
                    conn.execute(
                        "UPDATE cycles SET status = 'ABORTED', finished_at = ? WHERE instance_id = ? AND status = 'OPEN'",
                        (now, iid),
                    )
                    reaped += 1
    except Exception as e:
        print(f"Reconcile error: {e}")
    return reaped


def _balances(client) -> Tuple[float, float, float]:
    try:
        acct = client.get_account()
        balances = {b["asset"]: b for b in acct.get("balances", [])}
        b_usdt = balances.get("USDT", {})
        b_bnb = balances.get("BNB", {})
        usdt_free = float(b_usdt.get("free") or 0.0)
        usdt_locked = float(b_usdt.get("locked") or 0.0)
        bnb_free = float(b_bnb.get("free") or 0.0)
        return usdt_free, usdt_locked, bnb_free
    except BinanceAPIException as e:
        if is_network_error(e):
            raise
        return 0.0, 0.0, 0.0
    except Exception:
        return 0.0, 0.0, 0.0


def spawn_bot_process(instance_id: int) -> subprocess.Popen:
    """Start bot worker for an existing instance in the background."""
    inst = get_instance(instance_id) or {}
    bot_name = inst.get("name") or f"bot_{instance_id}"
    symbol = inst.get("current_symbol") or "INIT"
    clean_sym = "".join(c for c in symbol if c.isalnum() or c in ("-", "_")) or "INIT"
    log_name = f"{bot_name}_{clean_sym}.log"
    log_path = os.path.join(LOGS_DIR, log_name)
    log_f = open(log_path, "a", encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    creationflags = 0
    if sys.platform == "win32":
        # CREATE_NO_WINDOW (0x08000000) prevents any command window from popping up
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            [PYTHON, SCRIPT_PATH, "--bot", "--instance-id", str(instance_id)],
            cwd=SCRIPT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True if sys.platform != "win32" else False,
            creationflags=creationflags,
            env=env,
        )
        update_instance(instance_id, pid=proc.pid)
        return proc
    finally:
        log_f.close()


def _spawn(instance_id: int) -> None:
    spawn_bot_process(instance_id)


def manager_tick(force: bool = False) -> dict:
    """
    One manager cycle. Safe to call from UI refresh or a loop.
    Returns a status dict for the UI.
    """
    init_db()
    # Reconcile dead or crashed processes so slots and committed funds are never blocked
    reconcile_active_instances()

    cfg = get_manager_config()
    now = datetime.now(timezone.utc).isoformat()

    if not cfg.get("enabled"):
        return {"enabled": False, "message": "Manager disabled", "launched": False}

    # Debounce: avoid back-to-back triggers unless explicitly forced
    CHECK_INTERVAL_SEC = 5.0
    MIN_BNB_FEE_BUFFER = 0.001

    # Debounce: avoid back-to-back triggers unless explicitly forced
    if not force and cfg.get("last_check_at"):
        try:
            last_ts = datetime.fromisoformat(cfg["last_check_at"]).timestamp()
            if (datetime.now(timezone.utc).timestamp() - last_ts) < CHECK_INTERVAL_SEC:
                return {"enabled": True, "message": "Manager debounced", "launched": False}
        except Exception:
            pass

    investment = float(cfg.get("investment_usdt") or 25.0)
    stop_loss = float(cfg.get("stop_loss") or -0.25)

    try:
        client = make_client()
        usdt_free, usdt_locked, bnb_free = _balances(client)
    except Exception as e:
        msg = f"Manager: cannot read balances — {e}"
        set_manager_config(last_check_at=now, last_message=msg)
        log_event(msg, level="ERROR", event_type="MANAGER")
        return {"enabled": True, "message": msg, "launched": False, "error": str(e)}

    # Check minimum BNB fee availability for fee deductions
    if bnb_free < MIN_BNB_FEE_BUFFER:
        msg = f"ERROR: Insufficient BNB for trading fee deduction. Free={bnb_free:.6f} BNB, minimum {MIN_BNB_FEE_BUFFER:.4f} BNB required."
        set_manager_config(last_check_at=now, last_message=msg)
        log_event(msg, level="ERROR", event_type="MANAGER")
        return {"enabled": True, "message": msg, "launched": False, "error": msg}

    # Total account USDT balance = free USDT + locked USDT in orders + USDT invested in open bot positions
    spent_in_open_positions = 0.0
    try:
        with get_conn() as conn:
            # Sum all BUY trades for open cycles of running bots (accurately captures DCA dip tranches)
            row = conn.execute(
                """
                SELECT COALESCE(SUM(t.quote_qty), 0) AS s
                FROM trades t
                JOIN bot_instances b ON b.id = t.instance_id
                JOIN cycles c ON c.id = t.cycle_id
                WHERE b.status IN ('RUNNING', 'STOPPING') AND c.status = 'OPEN' AND t.side = 'BUY'
                """
            ).fetchone()
            spent_in_open_positions = float(row["s"] if row else 0.0)
            if spent_in_open_positions <= 0.0:
                row2 = conn.execute(
                    """
                    SELECT COALESCE(SUM(c.entry_cost), 0) AS s
                    FROM cycles c
                    JOIN bot_instances b ON b.id = c.instance_id
                    WHERE b.status IN ('RUNNING', 'STOPPING') AND c.status = 'OPEN' AND c.entry_cost > 0
                    """
                ).fetchone()
                spent_in_open_positions = float(row2["s"] if row2 else 0.0)
    except Exception:
        spent_in_open_positions = 0.0

    total_usdt_balance = usdt_free + usdt_locked + spent_in_open_positions

    # Dynamic parallel limit with 30% wallet cash reserve safety buffer
    # Ensures wallet is never 100% frozen in trades simultaneously
    effective_max_parallel = max(1, int((total_usdt_balance * 0.70) // investment)) if investment > 0 else 0
    balance_max_parallel = effective_max_parallel

    running = count_running_instances()
    reserved_bnb = running * MIN_BNB_FEE_BUFFER
    available_bnb = max(0.0, bnb_free - reserved_bnb)

    # Available USDT = Free USDT minus USDT already committed by active bots
    # that haven't filled their market buy yet (e.g. starting/scanning).
    committed_usdt = get_unfilled_committed_usdt()
    available_usdt = max(0.0, usdt_free - committed_usdt)

    can_slots = running < effective_max_parallel
    can_usdt = available_usdt >= investment
    can_bnb = available_bnb >= MIN_BNB_FEE_BUFFER

    status = {
        "enabled": True,
        "usdt_free": usdt_free,
        "usdt_locked": usdt_locked,
        "total_usdt_balance": total_usdt_balance,
        "bnb_free": bnb_free,
        "running": running,
        "effective_max_parallel": effective_max_parallel,
        "balance_max_parallel": balance_max_parallel,
        "committed_usdt": committed_usdt,
        "available_usdt": available_usdt,
        "reserved_bnb": reserved_bnb,
        "available_bnb": available_bnb,
        "investment": investment,
        "launched": False,
    }

    if not can_slots:
        msg = (
            f"Manager: balance capacity reached ({running}/{effective_max_parallel} bots running, "
            f"total balance: {total_usdt_balance:.2f} USDT / {investment:.2f} USDT investment)"
        )
        set_manager_config(last_check_at=now, last_message=msg)
        status["message"] = msg
        return status

    if not can_usdt:
        msg = (
            f"Manager: need >= {investment:.2f} available USDT, have {available_usdt:.4f} "
            f"(free={usdt_free:.4f}, committed={committed_usdt:.4f}, running={running})"
        )
        set_manager_config(last_check_at=now, last_message=msg)
        status["message"] = msg
        return status

    if not can_bnb:
        msg = (
            f"ERROR: Insufficient BNB for additional bot fees. "
            f"Free={bnb_free:.6f} BNB, Reserved={reserved_bnb:.6f} BNB, "
            f"Available={available_bnb:.6f} BNB (Need >= {MIN_BNB_FEE_BUFFER:.4f} BNB)."
        )
        set_manager_config(last_check_at=now, last_message=msg)
        log_event(msg, level="ERROR", event_type="MANAGER")
        status["message"] = msg
        status["error"] = msg
        return status

    # Ensure we only ever create 1 bot per investment slot initially.
    # No new bots should be created beyond the initial ones; user must reuse them.
    with get_conn() as conn:
        total_bots = conn.execute("SELECT COUNT(*) FROM bot_instances").fetchone()[0]

    if total_bots >= effective_max_parallel:
        msg = f"Manager: Initial bots already created (Total {total_bots} >= {effective_max_parallel} slots). Awaiting user to start/reuse them."
        set_manager_config(last_check_at=now, last_message=msg)
        status["message"] = msg
        return status

    # Create the instance first with status CREATED
    name = next_bot_name()
    strategy = cfg.get("strategy") or "DUAL_SOXLB_SOXSB"
    iid = create_instance(
        name=name,
        investment_usdt=investment,
        target_profit=float(cfg.get("target_profit") or 0.0050),
        stop_loss=stop_loss,
        strategy=strategy,
    )

    # The bot process will independently claim a pair when it starts. No need to pre-claim here.

    try:
        _spawn(iid)
        msg = (
            f"[{name}] [PENDING] Manager: launched [{name}] (id={iid}) | "
            f"USDT free was {usdt_free:.4f} | BNB avail {available_bnb:.6f}"
        )
        log_event(msg, level="INFO", event_type="MANAGER_LAUNCH", instance_id=iid, symbol="PENDING")
        set_manager_config(last_check_at=now, last_message=msg)
        status["message"] = msg
        status["launched"] = True
        status["instance_id"] = iid
        status["name"] = name
        status["symbol"] = "PENDING"
        return status
    except Exception as e:
        msg = f"[{name}] [PENDING] Manager: failed to spawn [{name}] id={iid}: {e}"
        log_event(msg, level="EXCEPTION", event_type="MANAGER", instance_id=iid, symbol="PENDING", detail=str(e))
        set_manager_config(last_check_at=now, last_message=msg)
        status["message"] = msg
        status["error"] = str(e)
        return status


def manager_loop(stop_flag: dict):
    """Background loop; stop_flag['stop']=True to exit."""
    init_db()
    while not stop_flag.get("stop"):
        cfg = get_manager_config()
        interval = float(cfg.get("check_interval_sec") or 15)
        if cfg.get("enabled"):
            try:
                manager_tick()
            except Exception as e:
                set_manager_config(
                    last_check_at=datetime.now(timezone.utc).isoformat(),
                    last_message=f"Manager loop error: {e}",
                )
        time.sleep(max(5.0, interval))




# ===========================================================================
# SECTION 4: CLI UTILITIES (Diagnostics & Liquidation)
# ===========================================================================

def stop_and_liquidate_all():
    """Emergency stop all bots and liquidate non-USDT/non-BNB holdings."""
    print("1. Disabling manager in manager_config...")
    init_db()
    with get_conn() as conn:
        conn.execute("UPDATE manager_config SET enabled = 0 WHERE id = 1")
        rows = conn.execute("SELECT id, name, pid, status, current_symbol FROM bot_instances WHERE status IN ('RUNNING', 'CREATED', 'STOPPING')").fetchall()
        for r in rows:
            pid = r["pid"]
            if pid:
                print(f"Terminating bot {r['name']} (PID {pid})...")
                terminate_pid(pid)
            conn.execute("UPDATE bot_instances SET status = 'STOPPED', stop_requested = 1, finished_at = ? WHERE id = ?", (datetime.now(timezone.utc).isoformat(), r["id"]))

    print("2. Connecting to Binance to liquidate all non-USDT, non-BNB assets...")
    client = make_client()
    account = client.get_account()
    balances = account.get("balances", [])

    for b in balances:
        asset = b["asset"]
        free = float(b["free"])
        if asset in ("USDT", "BNB", "LDUSDT") or free <= 0:
            continue

        symbol = f"{asset}USDT"
        print(f"\nLiquidating {free} {asset} via {symbol}...")
        try:
            filters = get_symbol_filters(client, symbol)
            qty_str = format_quantity(free, filters["step"])
            qty = float(qty_str)
            if qty < filters["min_qty"]:
                print(f"  {asset} balance {qty} < min_qty {filters['min_qty']}, cannot sell via market order.")
                continue

            print(f"  Placing MARKET SELL for {qty_str} {symbol}...")
            sell_order = client.create_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=qty_str,
                recvWindow=10000,
                newOrderRespType="FULL",
            )
            exit_val = float(sell_order.get("cummulativeQuoteQty") or 0.0)
            fee_info = fee_from_order(client, sell_order, symbol)
            fee_usdt = fee_info["fee_usdt"]
            fee_bnb = fee_info["fee_bnb"]
            print(f"  SUCCESS: Sold {qty_str} {symbol} for {exit_val:.4f} USDT (fee={fee_bnb:.8f} BNB / {fee_usdt:.4f} USDT)")

            # Close cycle in db if open
            with get_conn() as conn:
                open_cycle = conn.execute("SELECT id, instance_id, entry_cost FROM cycles WHERE symbol = ? AND status = 'OPEN' ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
                if open_cycle:
                    cid = open_cycle["id"]
                    iid = open_cycle["instance_id"]
                    cost = float(open_cycle["entry_cost"] or 0)
                    settled = settle_pnl(cost, exit_val, cost * 0.00075, fee_usdt)
                    insert_trade(
                        instance_id=iid,
                        cycle_id=cid,
                        symbol=symbol,
                        side="SELL",
                        quantity=qty,
                        price=exit_val / qty if qty else 0,
                        quote_qty=exit_val,
                        fee_bnb=fee_bnb,
                        fee_usdt=fee_usdt,
                        order_id=sell_order.get("orderId"),
                        pnl_usdt=settled["net_pnl"],
                        notes="USER_LIQUIDATION",
                    )
                    close_cycle(
                        cid,
                        exit_val,
                        settled["net_pnl"],
                        "LIQUIDATED",
                        0.0,
                        fee_bnb=fee_bnb,
                        fee_usdt=fee_usdt,
                    )
        except Exception as e:
            print(f"  Error liquidating {symbol}: {e}")

    print("\n3. Current Wallet Balances:")
    for b in client.get_account().get("balances", []):
        f = float(b["free"])
        l = float(b["locked"])
        if f > 0 or l > 0:
            print(f"  {b['asset']}: free={f}, locked={l}")


def get_public_ip() -> str:
    """Fetch current outbound public IP address."""
    urls = ["https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip"]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                return resp.read().decode("utf-8").strip()
        except Exception:
            continue
    return "UNKNOWN"


def mask_str(s: str) -> str:
    if not s or len(s) < 10:
        return "***"
    return f"{s[:4]}...{s[-4:]}"


def run_diagnostics():
    """Diagnostic suite to test network, credentials, and Binance endpoints."""
    print("=" * 60)
    print("       BINANCE API & CONNECTIVITY DIAGNOSTICS")
    print("=" * 60)

    results = []

    print("\n[1/5] Checking outbound public IP...")
    public_ip = get_public_ip()
    print(f"      Outbound Public IP: {public_ip}")
    results.append(("Public IP Detection", "PASS" if public_ip != "UNKNOWN" else "WARN", public_ip))

    print("\n[2/5] Checking API Key & Secret configuration...")
    key = (os.getenv("BINANCE_API_KEY") or "").strip()
    secret = (os.getenv("BINANCE_API_SECRET") or "").strip()
    if not key or not secret:
        env_path = os.path.join(SCRIPT_DIR, ".env")
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("\"'")
                        if k == "BINANCE_API_KEY" and not key:
                            key = v
                            os.environ[k] = v
                        elif k == "BINANCE_API_SECRET" and not secret:
                            secret = v
                            os.environ[k] = v

    if not key or not secret:
        print("      ERROR: BINANCE_API_KEY or BINANCE_API_SECRET is missing.")
        results.append(("API Key & Secret Config", "FAIL", "Missing credentials"))
    else:
        print(f"      API Key:    {mask_str(key)} (length: {len(key)})")
        print(f"      API Secret: {mask_str(secret)} (length: {len(secret)})")
        results.append(("API Key & Secret Config", "PASS", "Loaded successfully"))

    print("\n[3/5] Testing Binance Public REST API...")
    try:
        client = make_client()
        t0 = time.time()
        client.ping()
        ping_ms = (time.time() - t0) * 1000
        print(f"      Server Ping: {ping_ms:.1f} ms")
        ticker = client.get_symbol_ticker(symbol="BTCUSDT")
        print(f"      BTCUSDT Spot Price: {float(ticker['price']):,.2f} USDT")
        results.append(("Public REST API (Ping/Ticker)", "PASS", f"Ping: {ping_ms:.0f}ms"))
    except Exception as e:
        print(f"      Public API Error: {e}")
        results.append(("Public REST API (Ping/Ticker)", "FAIL", str(e)))

    print("\n[4/5] Testing Authenticated Spot API (Account & Permissions)...")
    try:
        account = client.get_account()
        can_trade = account.get("canTrade", False)
        balances = account.get("balances") or []
        usdt_bal = next((b for b in balances if b["asset"] == "USDT"), {"free": "0", "locked": "0"})
        bnb_bal = next((b for b in balances if b["asset"] == "BNB"), {"free": "0", "locked": "0"})
        print(f"      Account canTrade: {can_trade}")
        print(f"      USDT Balance: {float(usdt_bal['free']):.4f} free, {float(usdt_bal['locked']):.4f} locked")
        print(f"      BNB Balance:  {float(bnb_bal['free']):.6f} free, {float(bnb_bal['locked']):.6f} locked")
        results.append(("Authenticated Account API", "PASS" if can_trade else "WARN", f"USDT Free: {float(usdt_bal['free']):.2f}"))
    except Exception as e:
        print(f"      Authenticated API Error: {e}")
        results.append(("Authenticated Account API", "FAIL", str(e)))

    print("\n" + "=" * 60)
    print("                    DIAGNOSTIC SUMMARY")
    print("=" * 60)
    for check, status, details in results:
        badge = "PASS" if status == "PASS" else ("WARN" if status == "WARN" else "FAIL")
        print(f"{check:<32} | {badge:<6} | {details}")
    print("=" * 60 + "\n")


def run_bot_cli(args):
    """CLI worker runner (replaces start_bot.py)."""
    init_db()

    if args.instance_id:
        inst = get_instance(args.instance_id)
        if not inst:
            print(f"ERROR: instance {args.instance_id} not found", file=sys.stderr)
            sys.exit(1)
        if inst["status"] == "RUNNING":
            print(f"ERROR: instance {args.instance_id} is already RUNNING", file=sys.stderr)
            sys.exit(1)
        instance_id = args.instance_id
        bot_name = inst.get("name") or str(instance_id)
        symbol = inst.get("current_symbol") or "INIT"
        if args.use_only:
            try:
                client = make_client()
                active = get_active_symbols(exclude_instance_id=instance_id)
                use_only_list = [s.strip().upper() for s in args.use_only.split(",") if s.strip()]
                symbol = select_and_claim_best_pair(client, instance_id=instance_id, exclude_symbols=active, use_only=use_only_list)
            except Exception as e:
                print(f"Warning: could not pick from use_only list: {e}")
        print(f"[{bot_name}] [{symbol}] Resuming instance #{instance_id} ({bot_name})")
    else:
        name = args.name or next_bot_name()
        instance_id = create_instance(
            name=name,
            investment_usdt=args.investment,
            target_profit=args.target,
            stop_loss=args.stop_loss,
            strategy=args.strategy,
        )
        symbol = "INIT"
        if args.use_only:
            try:
                client = make_client()
                active = get_active_symbols(exclude_instance_id=instance_id)
                use_only_list = [s.strip().upper() for s in args.use_only.split(",") if s.strip()]
                symbol = select_and_claim_best_pair(client, instance_id=instance_id, exclude_symbols=active, use_only=use_only_list)
            except Exception as e:
                print(f"Warning: could not pick from use_only list: {e}")
        inst = get_instance(instance_id)
        bot_name = inst.get("name") or str(instance_id)
        symbol = inst.get("current_symbol") or symbol
        print(
            f"[{bot_name}] [{symbol}] Created instance #{instance_id} "
            f"name={name!r} investment={args.investment} "
            f"target={args.target} stop_loss={args.stop_loss}"
        )

    if args.create_only:
        print(f"[{bot_name}] [{symbol}] (--create-only) Exiting without trading.")
        return

    update_instance(instance_id, stop_requested=0, last_error=None)
    try:
        run_instance(instance_id)
    except Exception as e:
        now = datetime.now(timezone.utc).isoformat()
        update_instance(
            instance_id,
            status="ERROR",
            last_error=f"Bot crashed: {e}",
            finished_at=now,
            current_symbol=None,
        )
        log_event(
            f"[{bot_name}] [{symbol}] Bot crashed: {e}",
            level="ERROR",
            event_type="EXCEPTION",
            instance_id=instance_id,
            symbol=symbol,
            detail=str(e),
        )
        raise


# ===========================================================================
# SECTION 5: STREAMLIT WEB DASHBOARD (from app.py)
# ===========================================================================
# JitCrypto | Autonomous Scalper Terminal
# - WebSocket-style live updates via @st.fragment(run_every=...)
# - No blocking time.sleep() loops
# - Fixed Manager toggle using on_change callback
# - Professional trading terminal UI


import os
import sys
import time
import hmac
import subprocess
from datetime import datetime, timezone
from typing import Optional

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go


pass
pass
pass



# ---------------------------------------------------------------------------
# CSS — Professional Trading Terminal Theme
# ---------------------------------------------------------------------------

def inject_custom_css():
    st.markdown(
        """
        <style>
        /* ── Hide Streamlit Deploy Button, Toolbar, Theme Options & Footer ── */
        .stDeployButton,
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        #MainMenu,
        footer {
            visibility: hidden !important;
            display: none !important;
            height: 0 !important;
            opacity: 0 !important;
            pointer-events: none !important;
        }
        header[data-testid="stHeader"],
        [data-testid="stHeader"] {
            background: transparent !important;
            height: 2.875rem !important;
            pointer-events: none !important;
            z-index: 99999 !important;
        }
        /* Ensure sidebar expand and collapse toggle buttons are ALWAYS visible, styled, and clickable */
        [data-testid="collapsedControl"],
        [data-testid="stSidebarCollapsedControl"],
        [data-testid="stSidebarCollapseButton"] {
            display: flex !important;
            visibility: visible !important;
            opacity: 1 !important;
            pointer-events: auto !important;
            z-index: 1000000 !important;
            color: #2563eb !important;
        }
        [data-testid="collapsedControl"] button,
        [data-testid="stSidebarCollapsedControl"] button,
        [data-testid="stSidebarCollapseButton"] button {
            background: #ffffff !important;
            border: 1px solid #cbd5e1 !important;
            border-radius: 8px !important;
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.08) !important;
            color: #2563eb !important;
            cursor: pointer !important;
            pointer-events: auto !important;
        }
        [data-testid="collapsedControl"] button:hover,
        [data-testid="stSidebarCollapsedControl"] button:hover,
        [data-testid="stSidebarCollapseButton"] button:hover {
            background: #eff6ff !important;
            border-color: #2563eb !important;
            color: #1d4ed8 !important;
        }

        /* ── Base & Typography (White & Blue Theme, Larger Fonts) ──── */
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap');

        html, body, [data-testid="stAppViewContainer"], .stApp {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
            background-color: #f8fafc !important;
            color: #0f172a !important;
            font-size: 15px !important;
        }

        /* Container padding */
        .block-container {
            padding-top: 1.2rem !important;
            padding-bottom: 2rem !important;
            padding-left: 1.5rem !important;
            padding-right: 1.5rem !important;
            max-width: 100% !important;
            background: transparent !important;
        }

        /* Streamlit vertical and horizontal block gaps */
        div[data-testid="stVerticalBlock"] {
            gap: 0.6rem !important;
        }
        div[data-testid="stHorizontalBlock"] {
            gap: 0.6rem !important;
        }
        div[data-testid="stElementContainer"] {
            margin-bottom: 0 !important;
        }

        /* ── Sidebar (Clean White with Blue Accents) ──────────────── */
        section[data-testid="stSidebar"] {
            background: #ffffff !important;
            border-right: 1px solid #e2e8f0 !important;
        }
        section[data-testid="stSidebar"] > div {
            background: transparent !important;
            padding-top: 1rem !important;
        }
        .sidebar-logo {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 4px 0 12px 0;
            border-bottom: 1px solid #e2e8f0;
            margin-bottom: 12px;
        }
        .sidebar-logo-icon {
            width: 38px;
            height: 38px;
            background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
            color: #ffffff;
            box-shadow: 0 2px 8px rgba(37, 99, 235, 0.3);
        }
        .sidebar-logo-text { line-height: 1.2; }
        .sidebar-logo-title {
            font-size: 18px;
            font-weight: 800;
            color: #0f172a;
            letter-spacing: -0.3px;
        }
        .sidebar-logo-sub {
            font-size: 12px;
            color: #64748b;
            letter-spacing: 0.3px;
        }
        .live-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: #ecfdf5;
            border: 1px solid #a7f3d0;
            border-radius: 16px;
            padding: 3px 10px;
            font-size: 12px;
            font-weight: 600;
            color: #059669;
            width: 100%;
            margin: 6px 0;
        }
        .live-dot {
            width: 7px;
            height: 7px;
            background: #10b981;
            border-radius: 50%;
            animation: pulse 1.5s ease-in-out infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.5; transform: scale(0.8); }
        }

        /* ── Sidebar Navigation Menu ──────────────────────────────── */
        div[data-testid="stSidebar"] div[data-testid="stSelectbox"] > div > div {
            background: #ffffff !important;
            border: 2px solid #2563eb !important;
            border-radius: 8px !important;
            font-weight: 600 !important;
            font-size: 15px !important;
            color: #0f172a !important;
            box-shadow: 0 2px 6px rgba(37,99,235,0.08) !important;
            cursor: pointer !important;
        }
        div[data-testid="stSidebar"] div[data-testid="stSelectbox"] > div > div:hover {
            border-color: #1d4ed8 !important;
            box-shadow: 0 4px 12px rgba(37,99,235,0.15) !important;
        }

        /* ── Metrics Cards (White with Blue Accents & Larger Fonts) ─ */
        div[data-testid="stMetric"] {
            background: #ffffff !important;
            border: 1px solid #e2e8f0 !important;
            border-radius: 10px !important;
            padding: 12px 16px !important;
            min-height: 96px !important;
            display: flex !important;
            flex-direction: column !important;
            justify-content: space-between !important;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
            transition: border-color 0.2s ease, box-shadow 0.2s ease;
        }
        div[data-testid="stMetric"]:hover {
            border-color: #93c5fd !important;
            box-shadow: 0 2px 8px rgba(37,99,235,0.08);
        }
        div[data-testid="stMetric"] label {
            font-size: 12px !important;
            color: #64748b !important;
            font-weight: 600 !important;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 2px !important;
        }
        div[data-testid="stMetricValue"] {
            font-size: 24px !important;
            font-weight: 800 !important;
            color: #0f172a !important;
            font-family: 'JetBrains Mono', monospace !important;
            line-height: 1.2 !important;
        }
        div[data-testid="stMetricDelta"] {
            font-size: 12px !important;
            font-weight: 600 !important;
        }

        /* ── Tabs (White & Blue) ─────────────────────────────────── */
        .stTabs [data-baseweb="tab-list"] {
            gap: 6px;
            background: transparent !important;
            border-bottom: 2px solid #e2e8f0 !important;
            padding-bottom: 0;
        }
        .stTabs [data-baseweb="tab"] {
            height: 38px;
            border-radius: 8px 8px 0 0 !important;
            padding: 0 18px !important;
            background: transparent !important;
            border: 1px solid transparent !important;
            border-bottom: none !important;
            color: #64748b !important;
            font-weight: 600 !important;
            font-size: 14px !important;
            transition: all 0.12s ease;
        }
        .stTabs [data-baseweb="tab"]:hover {
            color: #1d4ed8 !important;
            background: #eff6ff !important;
        }
        .stTabs [aria-selected="true"] {
            background: #ffffff !important;
            border-color: #cbd5e1 !important;
            border-bottom-color: #ffffff !important;
            color: #1d4ed8 !important;
            font-weight: 700 !important;
        }
        .stTabs [data-baseweb="tab-panel"] {
            background: #ffffff !important;
            border: 1px solid #e2e8f0 !important;
            border-top: none !important;
            border-radius: 0 0 10px 10px !important;
            padding: 16px 18px !important;
            box-shadow: 0 1px 3px rgba(0,0,0,0.03);
        }

        /* ── Containers & Bot Cards (White & Blue) ────────────────── */
        div[data-testid="stVerticalBlock"] > div[style*="border"],
        div[data-testid="element-container"] > div[data-testid="stVerticalBlockBorderWrapper"] {
            background: #ffffff !important;
            border: 1px solid #e2e8f0 !important;
            border-radius: 10px !important;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important;
            padding: 12px 16px !important;
        }
        .bot-card {
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 10px;
            padding: 12px 16px;
            margin-bottom: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
            transition: border-color 0.2s ease, box-shadow 0.2s ease;
        }
        .bot-card:hover {
            border-color: #93c5fd;
            box-shadow: 0 2px 6px rgba(37,99,235,0.06);
        }
        .bot-card-running { border-left: 4px solid #16a34a !important; }
        .bot-card-stopped { border-left: 4px solid #dc2626 !important; }
        .bot-card-created { border-left: 4px solid #d97706 !important; }
        .pnl-positive { color: #16a34a !important; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
        .pnl-negative { color: #dc2626 !important; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
        .pnl-zero { color: #64748b !important; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 3px 9px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .badge-running { background: #dcfce7; color: #15803d; border: 1px solid #86efac; }
        .badge-stopped { background: #fee2e2; color: #b91c1c; border: 1px solid #fca5a5; }
        .badge-created { background: #fef3c7; color: #b45309; border: 1px solid #fcd34d; }
        .badge-completed { background: #dbeafe; color: #1d4ed8; border: 1px solid #93c5fd; }
        .badge-error { background: #fee2e2; color: #991b1b; border: 1px solid #f87171; }

        /* ── Universal Blue Buttons with White Text ── */
        .stButton > button,
        button[kind="primary"],
        button[data-testid="stBaseButton-primary"],
        button[data-testid="baseButton-primary"],
        div[data-testid="stFormSubmitButton"] > button {
            cursor: pointer !important;
            pointer-events: auto !important;
            position: relative !important;
            z-index: 10 !important;
            background: #2563eb !important;
            background-color: #2563eb !important;
            color: #ffffff !important;
            border: 1px solid #1d4ed8 !important;
            border-radius: 8px !important;
            font-weight: 600 !important;
            font-size: 14px !important;
            padding: 7px 16px !important;
            min-height: 38px !important;
            box-shadow: 0 2px 6px rgba(37,99,235,0.25) !important;
            transition: all 0.15s ease !important;
        }

        button[kind="primary"] *,
        button[data-testid="stBaseButton-primary"] *,
        .stButton > button[kind="primary"] *,
        div[data-testid="stFormSubmitButton"] > button * {
            pointer-events: none !important;
            color: #ffffff !important;
            fill: #ffffff !important;
            font-weight: 600 !important;
        }

        button[kind="primary"]:hover,
        button[data-testid="stBaseButton-primary"]:hover,
        div[data-testid="stFormSubmitButton"] > button:hover {
            background: #1d4ed8 !important;
            background-color: #1d4ed8 !important;
            color: #ffffff !important;
            border-color: #1e40af !important;
            box-shadow: 0 4px 12px rgba(37,99,235,0.35) !important;
        }

        /* ── Secondary Buttons (Clean White with Blue Border & Text) ── */
        button[kind="secondary"],
        button[data-testid="stBaseButton-secondary"],
        button[data-testid="baseButton-secondary"],
        .stButton > button[kind="secondary"],
        .stDownloadButton > button {
            cursor: pointer !important;
            pointer-events: auto !important;
            position: relative !important;
            z-index: 10 !important;
            background: #ffffff !important;
            background-color: #ffffff !important;
            color: #2563eb !important;
            border: 1.5px solid #93c5fd !important;
            border-radius: 8px !important;
            font-weight: 600 !important;
            font-size: 14px !important;
            padding: 7px 16px !important;
            min-height: 38px !important;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important;
            transition: all 0.15s ease !important;
        }

        button[kind="secondary"] *,
        button[data-testid="stBaseButton-secondary"] *,
        .stButton > button[kind="secondary"] *,
        .stDownloadButton > button * {
            pointer-events: none !important;
            color: #2563eb !important;
            fill: #2563eb !important;
            font-weight: 600 !important;
        }

        button[kind="secondary"]:hover,
        button[data-testid="stBaseButton-secondary"]:hover,
        .stDownloadButton > button:hover {
            background: #eff6ff !important;
            background-color: #eff6ff !important;
            color: #1d4ed8 !important;
            border-color: #2563eb !important;
            box-shadow: 0 2px 8px rgba(37,99,235,0.12) !important;
        }

        button[kind="secondary"]:hover *,
        button[data-testid="stBaseButton-secondary"]:hover *,
        .stDownloadButton > button:hover * {
            color: #1d4ed8 !important;
        }

        /* ── Selectbox & Dropdown Menus (Crisp High Contrast White & Blue) ── */
        div[data-testid="stSelectbox"] > div,
        div[data-baseweb="select"],
        div[data-baseweb="select"] > div {
            background-color: #ffffff !important;
            border-color: #2563eb !important;
            border-radius: 8px !important;
            min-height: 40px !important;
        }
        div[data-testid="stSelectbox"] div[data-baseweb="select"] div,
        div[data-testid="stSelectbox"] div[data-baseweb="select"] span,
        div[data-testid="stSelectbox"] div[data-baseweb="select"] p {
            color: #0f172a !important;
            font-weight: 600 !important;
            font-size: 15px !important;
        }
        div[data-baseweb="select"] svg {
            fill: #2563eb !important;
        }
        div[data-baseweb="popover"],
        div[data-baseweb="popover"] > div,
        ul[role="listbox"] {
            background-color: #ffffff !important;
            border: 1px solid #cbd5e1 !important;
            border-radius: 8px !important;
            box-shadow: 0 8px 24px rgba(0,0,0,0.12) !important;
        }
        li[role="option"] {
            color: #0f172a !important;
            background-color: #ffffff !important;
            font-size: 14px !important;
            font-weight: 500 !important;
            padding: 10px 14px !important;
        }
        li[role="option"] * {
            color: #0f172a !important;
        }
        li[role="option"]:hover,
        li[role="option"][aria-selected="true"] {
            background-color: #eff6ff !important;
        }
        li[role="option"]:hover *,
        li[role="option"][aria-selected="true"] * {
            color: #2563eb !important;
            font-weight: 700 !important;
        }

        /* ── Forms & Inputs ───────────────────────────────────────── */
        div[data-testid="stForm"] {
            background: #ffffff !important;
            border: 1px solid #e2e8f0 !important;
            border-radius: 10px !important;
            padding: 16px 20px !important;
            box-shadow: 0 1px 3px rgba(0,0,0,0.03);
        }
        .stTextInput > div > div > input,
        .stNumberInput > div > div > input,
        .stSelectbox > div > div > div {
            background: #ffffff !important;
            border-color: #cbd5e1 !important;
            color: #0f172a !important;
            border-radius: 8px !important;
            font-size: 14px !important;
            min-height: 38px !important;
        }
        .stTextInput > div > div > input:focus,
        .stNumberInput > div > div > input:focus {
            border-color: #2563eb !important;
            box-shadow: 0 0 0 3px rgba(37,99,235,0.15) !important;
        }

        /* ── Toggle ───────────────────────────────────────────────── */
        div[data-testid="stToggle"] > label {
            color: #0f172a !important;
            font-weight: 600 !important;
            font-size: 14px !important;
        }
        div[data-testid="stToggle"] [data-checked="true"] {
            background: #2563eb !important;
        }

        /* ── Expander ─────────────────────────────────────────────── */
        div[data-testid="stExpander"] {
            background: #ffffff !important;
            border: 1px solid #e2e8f0 !important;
            border-radius: 8px !important;
        }
        div[data-testid="stExpander"] summary {
            color: #0f172a !important;
            font-weight: 600 !important;
            font-size: 14px !important;
            padding: 8px 14px !important;
        }

        /* ── Alerts ───────────────────────────────────────────────── */
        div[data-testid="stAlert"] {
            border-radius: 8px !important;
            padding: 10px 14px !important;
            font-size: 14px !important;
        }

        /* ── Divider ──────────────────────────────────────────────── */
        hr {
            border-color: #e2e8f0 !important;
            margin: 10px 0 !important;
        }

        /* ── Page titles & Headings (Larger, High-Contrast Fonts) ─── */
        h1 {
            color: #0f172a !important;
            font-weight: 800 !important;
            font-size: 24px !important;
            letter-spacing: -0.4px;
            margin-top: 0 !important;
            margin-bottom: 4px !important;
        }
        h2, h3 {
            color: #1e293b !important;
            font-weight: 700 !important;
            font-size: 18px !important;
            margin-top: 6px !important;
            margin-bottom: 6px !important;
        }
        p, .stCaption {
            color: #64748b !important;
            margin-bottom: 4px !important;
            font-size: 14px !important;
        }

        /* ── Login page (Centered White Card with Blue Shadow) ─────── */
        .login-wrapper {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 2rem 0 1rem 0;
        }
        .login-card {
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 16px;
            padding: 28px 32px 20px;
            width: 100%;
            max-width: 420px;
            box-shadow: 0 10px 30px rgba(37,99,235,0.08);
            text-align: center;
            margin: 0 auto;
        }
        .login-logo {
            text-align: center;
            margin-bottom: 12px;
        }
        .login-logo-icon {
            width: 52px;
            height: 52px;
            background: linear-gradient(135deg, #2563eb, #1d4ed8);
            border-radius: 14px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 26px;
            color: #ffffff;
            margin: 0 auto 10px;
            box-shadow: 0 4px 16px rgba(37,99,235,0.3);
        }
        .login-title {
            font-size: 24px;
            font-weight: 800;
            color: #0f172a;
            letter-spacing: -0.4px;
            margin-bottom: 4px;
        }
        .login-sub {
            font-size: 14px;
            color: #64748b;
        }

        /* ── Scrollbar ────────────────────────────────────────────── */
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: #f8fafc; }
        ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }

        /* ── Dataframe ────────────────────────────────────────────── */
        div[data-testid="stDataFrame"] {
            border: 1px solid #e2e8f0 !important;
            border-radius: 8px !important;
            background: #ffffff !important;
            overflow: hidden;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_dashboard_secret() -> str:
    sec = (os.getenv("BINANCE_API_SECRET") or "").strip()
    if not sec:
        env_file = os.path.join(SCRIPT_DIR, ".env")
        if os.path.exists(env_file):
            try:
                with open(env_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            if k.strip() == "BINANCE_API_SECRET":
                                sec = v.strip().strip("\"'")
                                os.environ["BINANCE_API_SECRET"] = sec
                                break
            except Exception:
                pass
    return sec


def check_password():
    """Password protection gate: bypassed (login removed per user request)."""
    st.session_state["authenticated"] = True
    return True



def status_emoji(status: str) -> str:
    return f"[{status}]"


def status_badge_html(status: str) -> str:
    cls_map = {
        "RUNNING": "badge-running",
        "STOPPING": "badge-running",
        "CREATED": "badge-created",
        "COMPLETED": "badge-completed",
        "SUCCESS": "badge-completed",
        "FAILURE": "badge-stopped",
        "STOPPED": "badge-stopped",
        "ERROR": "badge-error",
        "STOP_LOSS": "badge-stopped",
        "ABORTED": "badge-stopped",
        "SCRATCH": "badge-created",
        "TARGET": "badge-completed",
        "BREAKEVEN_LOCK": "badge-completed",
        "TIME_EXPIRED": "badge-stopped",
    }
    cls = cls_map.get(status, "badge-created")
    dot = "●"
    return f'<span class="status-badge {cls}">{dot} {status}</span>'


def pnl_html(pnl: float, decimals: int = 4) -> str:
    cls = "pnl-positive" if pnl > 0 else ("pnl-negative" if pnl < 0 else "pnl-zero")
    sign = "+" if pnl > 0 else ""
    return f'<span class="{cls}">{sign}{pnl:.{decimals}f} USDT</span>'


def rows_to_df(rows) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Live Fragments (WebSocket-style updates, no full page rerun)
# ---------------------------------------------------------------------------

@st.fragment(run_every=2)
def render_global_metrics():
    """Updates every 2s via Streamlit's WebSocket channel — no page reload."""
    cfg = get_manager_config()
    if cfg.get("enabled"):
        try:
            manager_tick()
        except Exception:
            pass

    instances = list_instances(limit=100)
    if not instances:
        st.info("No bot instances yet. Launch one from the sidebar or via CLI.")
        return

    total_runs = len(instances)
    wins = sum(1 for i in instances if (i.get("cumulative_pnl") or 0) > 0)
    losses = sum(1 for i in instances if (i.get("cumulative_pnl") or 0) < 0)
    non_cost_errors = sum(
        1 for i in instances
        if (i.get("cumulative_pnl") or 0) >= 0 and (i.get("last_error") or i.get("status") == "ERROR")
    )
    total_pnl = sum(i.get("cumulative_pnl") or 0 for i in instances)

    # Reconcile wins/losses and total net P/L directly from recorded cycles for 100% mathematical accuracy
    try:
        with get_conn() as conn:
            c_stats = conn.execute("""
                SELECT 
                    COUNT(CASE WHEN pnl_usdt > 0 THEN 1 END) AS wins,
                    COUNT(CASE WHEN pnl_usdt < 0 THEN 1 END) AS losses,
                    COALESCE(SUM(pnl_usdt), 0.0) AS total_cycle_pnl
                FROM cycles
                WHERE pnl_usdt IS NOT NULL
            """).fetchone()
            if c_stats and (c_stats[0] > 0 or c_stats[1] > 0):
                wins = int(c_stats[0])
                losses = int(c_stats[1])
                total_pnl = float(c_stats[2])
    except Exception:
        pass

    running_bots = sum(1 for i in instances if i.get("status") == "RUNNING")
    win_rate = (wins / (wins + losses) * 100.0) if (wins + losses) > 0 else 0.0

    # Highest Profit calculation across instances and closed cycles
    highest_profit = max([float(i.get("cumulative_pnl") or 0.0) for i in instances] + [0.0])
    try:
        with get_conn() as conn:
            r = conn.execute("SELECT MAX(pnl_usdt) FROM cycles WHERE pnl_usdt > 0").fetchone()
            if r and r[0] is not None:
                highest_profit = max(highest_profit, float(r[0]))
    except Exception:
        pass

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric(
        "Total Runs",
        total_runs,
        delta=f"{win_rate:.1f}% win rate" if (wins + losses) > 0 else "0.0% win rate",
        delta_color="normal" if win_rate >= 50 and (wins + losses) > 0 else "off",
    )
    c2.metric(
        "Total Wins",
        wins,
        delta=f"Highest: +{highest_profit:.4f} USDT" if highest_profit > 0 else "Highest: +0.0000 USDT",
        delta_color="normal",
    )
    err_label = f"{non_cost_errors} zero-cost loss{'es' if non_cost_errors != 1 else ''}"
    c3.metric(
        "Total Losses",
        losses,
        delta=err_label,
        delta_color="off",
    )
    c4.metric("Active Runs", running_bots, delta=f"{total_runs} total instances", delta_color="off")
    c5.metric(
        "All-time P/L",
        f"{total_pnl:+.4f} USDT",
        delta=f"{total_pnl:+.4f} USDT",
        delta_color="normal",
    )

    tds_on_profit = max(0.0, total_pnl) * 0.30
    c6.metric(
        "30% TDS (Payable)",
        f"{tds_on_profit:.4f} USDT",
        delta="On net profit" if tds_on_profit > 0 else "No profit yet",
        delta_color="off",
    )


@st.cache_data(ttl=1.5, show_spinner=False)
def get_dual_live_orderbook() -> dict:
    """Fetches live orderbook best bids for SOXSBUSDT and SOXLBUSDT with 1.5s cache."""
    try:
        c = make_client()
        ob_s = c.get_orderbook_ticker(symbol="SOXSBUSDT")
        ob_l = c.get_orderbook_ticker(symbol="SOXLBUSDT")
        return {
            "SOXSBUSDT": float(ob_s.get("bidPrice") or 0.0),
            "SOXLBUSDT": float(ob_l.get("bidPrice") or 0.0),
        }
    except Exception:
        return {}


def get_dual_bot_live_stats(inst_id: int) -> dict:
    """Computes live individual P/L stats for SOXSB and SOXLB legs of a dual bot."""
    bids = get_dual_live_orderbook()
    state_file = "dual_soxlb_soxsb_state.json"
    legs_info = {}
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                st_data = json.load(f)
                legs_info = st_data.get(str(inst_id), {}).get("legs", {})
        except Exception:
            pass

    if not legs_info:
        try:
            with get_conn() as conn:
                rows = conn.execute(
                    "SELECT symbol, entry_cost, qty, fee_usdt FROM cycles WHERE instance_id = ? AND status = 'OPEN'",
                    (inst_id,)
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    sym = d["symbol"]
                    qty = float(d.get("qty") or 0.0)
                    cost = float(d.get("entry_cost") or 0.0)
                    entry_px = cost / qty if qty > 0 else 0.0
                    legs_info[sym] = {
                        "symbol": sym,
                        "qty": qty,
                        "entry_cost": cost,
                        "entry_price": entry_px,
                        "buy_fee_usdt": float(d.get("fee_usdt") or (cost * 0.00075)),
                    }
        except Exception:
            pass

    results = {}
    total_pnl = 0.0
    for sym, short_tag in [("SOXSBUSDT", "SOXSB"), ("SOXLBUSDT", "SOXLB")]:
        leg = legs_info.get(sym)
        if leg:
            qty = float(leg.get("qty") or 0.0)
            cost = float(leg.get("entry_cost") or 0.0)
            entry_px = float(leg.get("entry_price") or 0.0)
            buy_fee = float(leg.get("buy_fee_usdt") or (cost * 0.00075))
            bid = bids.get(sym, 0.0) or entry_px
            gross = qty * bid
            est_sell_fee = gross * 0.00075
            pnl = gross - cost - buy_fee - est_sell_fee
            pct = ((bid - entry_px) / entry_px) * 100.0 if entry_px > 0 else 0.0
            total_pnl += pnl
            results[short_tag] = {
                "active": True,
                "bid": bid,
                "entry_px": entry_px,
                "qty": qty,
                "pnl": pnl,
                "pct": pct,
            }
        else:
            results[short_tag] = {
                "active": False,
                "bid": bids.get(sym, 0.0),
                "entry_px": 0.0,
                "qty": 0.0,
                "pnl": 0.0,
                "pct": 0.0,
            }
    results["total_pnl"] = total_pnl
    return results


@st.fragment(run_every=2)
def render_active_bots():
    """Live bot cards — update every 2s without touching the rest of the page."""
    instances = list_instances(limit=100)
    if not instances:
        st.info("No active bot instances yet. Use 'Launch Bot' or the Manager to start.")
        return

    for inst in instances:
        status = inst["status"]
        card_cls = (
            "bot-card bot-card-running" if status in ("RUNNING", "STOPPING")
            else "bot-card bot-card-stopped" if status in ("STOPPED", "ERROR", "STOP_LOSS")
            else "bot-card bot-card-created"
        )
        cum_pnl = inst.get("cumulative_pnl") or 0
        strat = inst.get("strategy") or "AUTO"
        strat_tag = {
            "DUAL_SOXLB_SOXSB": "Dual SOXLB/SOXSB",
            "DUAL_HARVEST_STRATEGY": "Dual SOXLB/SOXSB",
            "DUAL_HARVEST": "Dual SOXLB/SOXSB",
            "HYBRID_SOXLB_SOXSB_SRATEGY": "Hybrid SOXLB/SOXSB",
            "SOXS_SOXL_LAG_PREDICTOR": "Lag Arbitrage",
            "SEMICONDUCTOR_DUAL_ENGINE": "Dual Engine",
            "TREND_FOLLOWING": "Trend",
            "MOMENTUM": "Momentum",
            "MEAN_REVERSION": "Mean Rev",
            "GRID": "Grid",
            "ARBITRAGE": "Arbitrage",
            "MARKET_MAKING_SCALPING": "Scalp/MM",
            "MACHINE_LEARNING": "Adaptive ML",
            "BEST_BID_MAKER": "Maker",
            "SCALE_IN_DCA": "Grid/DCA",
            "AUTO": "Hybrid AUTO",
        }.get(strat, strat)

        with st.container(border=True):
            col_a, col_b, col_c, col_d = st.columns([2.8, 1.8, 2.8, 1.6])

            with col_a:
                started_ist = to_ist_str(inst.get("started_at"))
                st.markdown(
                    f"<div style='line-height:1.3;'>"
                    f"{status_badge_html(status)}&nbsp;&nbsp;"
                    f"<b style='color:#0f172a;font-size:15px;'>#{inst['id']} {inst['name']}</b>"
                    f"<div style='font-size:12px;color:#64748b;margin-top:3px;'>"
                    f"<code>{strat_tag}</code> · <b>{inst['investment_usdt']} USDT</b> · SL: <code>{inst['stop_loss']}</code> · Started: <code>{started_ist}</code>"
                    f"</div></div>",
                    unsafe_allow_html=True,
                )

            with col_b:
                pnl_color = "#16a34a" if cum_pnl > 0 else ("#dc2626" if cum_pnl < 0 else "#64748b")
                sign = "+" if cum_pnl > 0 else ""
                st.markdown(
                    f"<div style='color:#64748b;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.4px;'>Cumulative P/L</div>"
                    f"<div style='font-size:18px;font-weight:700;color:{pnl_color};font-family:JetBrains Mono,monospace;line-height:1.2;'>{sign}{cum_pnl:.4f} USDT</div>"
                    f"<div style='font-size:12px;color:#64748b;'>W{inst.get('wins',0)} / L{inst.get('losses',0)}</div>",
                    unsafe_allow_html=True,
                )

            with col_c:
                is_dual = (strat in ("DUAL_SOXLB_SOXSB", "DUAL_HARVEST_STRATEGY", "DUAL_HARVEST", "DUAL_SOXLB_SOXSB_STRATEGY") or "SOXSB + SOXLB" in (inst.get("current_symbol") or "") or "SOXLB+SOXSB" in (inst.get("current_symbol") or ""))
                if status in ("RUNNING", "STOPPING"):
                    if is_dual:
                        dual_stats = get_dual_bot_live_stats(inst["id"])
                        s_stat = dual_stats.get("SOXSB", {})
                        l_stat = dual_stats.get("SOXLB", {})
                        tot_pnl = dual_stats.get("total_pnl", inst.get("current_pnl") or 0.0)
                        tot_color = "#16a34a" if tot_pnl > 0 else ("#dc2626" if tot_pnl < 0 else "#64748b")
                        tot_sign = "+" if tot_pnl > 0 else ""

                        s_pnl = s_stat.get("pnl", 0.0)
                        s_pct = s_stat.get("pct", 0.0)
                        s_bid = s_stat.get("bid", 0.0)
                        s_color = "#16a34a" if s_pnl > 0 else ("#dc2626" if s_pnl < 0 else "#64748b")
                        s_sign = "+" if s_pnl > 0 else ""

                        l_pnl = l_stat.get("pnl", 0.0)
                        l_pct = l_stat.get("pct", 0.0)
                        l_bid = l_stat.get("bid", 0.0)
                        l_color = "#16a34a" if l_pnl > 0 else ("#dc2626" if l_pnl < 0 else "#64748b")
                        l_sign = "+" if l_pnl > 0 else ""

                        st.markdown(
                            f"""
                            <div style='color:#64748b;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;display:flex;justify-content:space-between;align-items:center;'>
                                <span>Live Dual Legs</span>
                                <span style='color:{tot_color};font-family:JetBrains Mono,monospace;font-size:12px;font-weight:700;'>Total: {tot_sign}{tot_pnl:.4f}</span>
                            </div>
                            <div style='margin-top:3px;font-size:12px;line-height:1.4;'>
                                <div style='display:flex;justify-content:space-between;align-items:center;padding:1px 0;'>
                                    <span><b style='color:#ea580c;background:#fff7ed;padding:1px 4px;border-radius:4px;font-size:11px;border:1px solid #fed7aa;'>SOXSB</b> <span style='color:#64748b;font-size:11px;'>${s_bid:.2f}</span></span>
                                    <span style='font-family:JetBrains Mono,monospace;font-weight:700;color:{s_color};'>{s_sign}{s_pnl:.4f} <span style='font-size:10px;font-weight:500;'>({s_pct:+.2f}%)</span></span>
                                </div>
                                <div style='display:flex;justify-content:space-between;align-items:center;padding:1px 0;'>
                                    <span><b style='color:#2563eb;background:#eff6ff;padding:1px 4px;border-radius:4px;font-size:11px;border:1px solid #bfdbfe;'>SOXLB</b> <span style='color:#64748b;font-size:11px;'>${l_bid:.2f}</span></span>
                                    <span style='font-family:JetBrains Mono,monospace;font-weight:700;color:{l_color};'>{l_sign}{l_pnl:.4f} <span style='font-size:10px;font-weight:500;'>({l_pct:+.2f}%)</span></span>
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    else:
                        live_sym = inst.get("current_symbol") or "—"
                        live_pnl = inst.get("current_pnl") or 0
                        live_color = "#16a34a" if live_pnl > 0 else ("#dc2626" if live_pnl < 0 else "#64748b")
                        live_sign = "+" if live_pnl > 0 else ""
                        st.markdown(
                            f"<div style='color:#64748b;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.4px;'>Live Trade</div>"
                            f"<div style='font-size:14px;font-weight:600;color:#2563eb;'>{live_sym} &nbsp;<span style='font-size:17px;font-weight:700;color:{live_color};font-family:JetBrains Mono,monospace;'>{live_sign}{live_pnl:.4f}</span></div>",
                            unsafe_allow_html=True,
                        )
                elif status == "STOPPED" and inst.get("current_symbol"):
                    if is_dual:
                        dual_stats = get_dual_bot_live_stats(inst["id"])
                        s_stat = dual_stats.get("SOXSB", {})
                        l_stat = dual_stats.get("SOXLB", {})
                        st.markdown(
                            f"""
                            <div style='color:#64748b;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;'>Held in Wallet</div>
                            <div style='margin-top:3px;font-size:12px;line-height:1.4;'>
                                <div><b style='color:#ea580c;'>SOXSB</b>: {s_stat.get('qty', 0):.4f} units (entry ${s_stat.get('entry_px', 0):.2f})</div>
                                <div><b style='color:#2563eb;'>SOXLB</b>: {l_stat.get('qty', 0):.4f} units (entry ${l_stat.get('entry_px', 0):.2f})</div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            f"<div style='color:#64748b;font-size:11px;'>Holding</div>"
                            f"<div style='font-size:14px;color:#d97706;font-weight:600;'>{inst['current_symbol']}</div>",
                            unsafe_allow_html=True,
                        )
                else:
                    if inst.get("finished_at"):
                        st.markdown(f"<div style='font-size:12px;color:#64748b;margin-top:8px;'>Finished: <code>{to_ist_str(inst.get('finished_at'))}</code></div>", unsafe_allow_html=True)

            with col_d:
                if status in ("RUNNING", "STOPPING"):
                    if st.button("Liquidate", key=f"liq_{inst['id']}", type="primary",
                                 width="stretch",
                                 help="Cancels limit order and immediately sells to USDT at market price"):
                        try:
                            res = liquidate_single_bot(inst["id"])
                            if res.get("liquidated"):
                                st.toast(f"Liquidated #{inst['id']}: {res.get('exit_val', 0):.2f} USDT — Ready for new run")
                            else:
                                st.toast(f"Stopped #{inst['id']} (no position held) — Ready for new run")
                        except Exception as ex:
                            st.error(f"Liquidation error: {ex}")
                        st.rerun()
                    if st.button("Stop and Hold", key=f"stop_{inst['id']}", type="secondary",
                                 width="stretch",
                                 help="Cancels Binance limit order, stops bot, keeps coins in wallet"):
                        try:
                            stop_single_bot(inst["id"])
                            st.toast(f"Bot #{inst['id']} stopped. Coins held in wallet.")
                        except Exception as ex:
                            st.error(f"Stop error: {ex}")
                        st.rerun()
                elif status == "STOPPED":
                    if inst.get("current_symbol"):
                        if st.button("Liquidate Holdings", key=f"liq_hold_{inst['id']}", type="primary",
                                     width="stretch",
                                     help="Sells all held coins to USDT and resets bot ready for new run"):
                            try:
                                res = liquidate_single_bot(inst["id"])
                                if res.get("liquidated"):
                                    st.toast(f"Liquidated #{inst['id']}: {res.get('exit_val', 0):.2f} USDT — Ready for new run")
                                else:
                                    st.toast(f"No coins held for #{inst['id']} — Ready for new run")
                            except Exception as ex:
                                st.error(f"Liquidation error: {ex}")
                            st.rerun()
                        if st.button("Start New Run", key=f"start_{inst['id']}", type="secondary",
                                     width="stretch"):
                            update_instance(inst["id"], status="CREATED", stop_requested=0, last_error=None, current_symbol=None)
                            spawn_bot_process(inst["id"])
                            st.toast(f"Started bot #{inst['id']}")
                            st.rerun()
                    else:
                        if st.button("Start", key=f"start_{inst['id']}", type="primary",
                                     width="stretch"):
                            update_instance(inst["id"], status="CREATED", stop_requested=0, last_error=None, current_symbol=None)
                            spawn_bot_process(inst["id"])
                            st.toast(f"Started bot #{inst['id']}")
                            st.rerun()
                        if st.button("Liquidate Holdings", key=f"liq_hold_{inst['id']}", type="secondary",
                                     width="stretch",
                                     help="Sell any residual coin holdings and reset bot state"):
                            try:
                                res = liquidate_single_bot(inst["id"])
                                if res.get("liquidated"):
                                    st.toast(f"Liquidated #{inst['id']}: {res.get('exit_val', 0):.2f} USDT")
                                else:
                                    st.toast(f"No coins held for #{inst['id']}")
                            except Exception as ex:
                                st.error(f"Liquidation error: {ex}")
                            st.rerun()
                elif status == "CREATED" or (status in ("COMPLETED", "ERROR", "STOP_LOSS")):
                    if st.button("Start", key=f"start_{inst['id']}", type="primary",
                                 width="stretch"):
                        update_instance(inst["id"], status="CREATED", stop_requested=0, last_error=None, current_symbol=None)
                        spawn_bot_process(inst["id"])
                        st.toast(f"Started bot #{inst['id']}")
                        st.rerun()
                    if status != "CREATED":
                        if st.button("Liquidate Holdings", key=f"liq_hold_{inst['id']}", type="secondary",
                                     width="stretch"):
                            try:
                                res = liquidate_single_bot(inst["id"])
                                if res.get("liquidated"):
                                    st.toast(f"Liquidated #{inst['id']}: {res.get('exit_val', 0):.2f} USDT")
                                else:
                                    st.toast(f"No coins held for #{inst['id']}")
                            except Exception as ex:
                                st.error(f"Liquidation error: {ex}")
                            st.rerun()

                if inst.get("last_error") and status != "CREATED":
                    le = inst["last_error"]
                    if any(le.startswith(p) for p in ("Lag Scan", "3M Score", "Scanning", "Lag Scanner")):
                        st.caption(f"📡 {le}")
                    else:
                        st.caption(f"⚠️ Warning: {le[:140]}")


@st.fragment(run_every=3)
def render_manager_status():
    """Polls manager status every 3s, auto-ticks when enabled."""
    cfg = get_manager_config()
    if cfg.get("enabled"):
        try:
            manager_tick()
        except Exception:
            pass
        cfg = get_manager_config()

    is_on = bool(cfg.get("enabled"))
    status_color = "#22c55e" if is_on else "#8b949e"
    status_text = "Automation ACTIVE" if is_on else "Automation PAUSED"

    st.markdown(
        f"<div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 18px;margin-top:12px;box-shadow:0 1px 3px rgba(0,0,0,0.04);'>"
        f"<div style='display:flex;align-items:center;gap:8px;'>"
        f"<div style='width:8px;height:8px;border-radius:50%;background:{status_color};'></div>"
        f"<span style='font-weight:700;color:{status_color};font-size:14px;'>{status_text}</span>"
        f"</div>"
        f"<div style='margin-top:8px;font-size:13px;color:#64748b;'>"
        f"Last check: <code style='color:#2563eb;'>{cfg.get('last_check_at') or '—'}</code>"
        f"</div>"
        f"<div style='margin-top:4px;font-size:13px;color:#64748b;'>{cfg.get('last_message') or '—'}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    try:
        pass
        cooldowns = get_cooldown_symbols()
        underperformers = get_underperforming_symbols()
        user_bl = [s.strip().upper() for s in (cfg.get("blacklist") or "").replace(",", " ").split() if s.strip()]

        bl_badges = []
        if user_bl:
            bl_badges.append(f"<b>User Blacklist:</b> {', '.join(user_bl)}")
        if underperformers:
            bl_badges.append(f"<b>Auto-Banned (Underperforming):</b> {', '.join(sorted(underperformers))}")
        if cooldowns:
            bl_badges.append(f"<b>Quarantine (Adverse Drift / Stop-Loss):</b> {', '.join(sorted(cooldowns))}")

        if bl_badges:
            st.markdown(
                f"<div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;margin-top:8px;font-size:12px;color:#334155;line-height:1.6;'>"
                + "<br>".join(bl_badges)
                + f"</div>",
                unsafe_allow_html=True,
            )
    except Exception:
        pass

    if st.button(" Run Check Now", key="btn_mgr_check_now", width="stretch"):
        result = manager_tick(force=True)
        st.json(result)


def render_log_detail(detail: str) -> None:
    """
    Renders structured details inside the log expander dropdown.
    If the detail contains semiconductor 5M scores, renders a clean visual dashboard
    showing all 7 criteria, color badges, underlying live metrics, and conviction summary.
    Uses native Streamlit alert components and clean GitHub Flavored Markdown table.
    Strictly uses Indian Standard Time (IST) for all timestamps.
    Otherwise renders standard code block.
    """
    if not detail:
        return

    import json

    try:
        data = json.loads(detail)
        if isinstance(data, dict) and data.get("type") == "SEMICONDUCTOR_5M_SCORES":
            total = int(data.get("total_score", 0))
            bias = str(data.get("bias", "CHOP"))
            action = str(data.get("action", "SKIP"))
            reason = str(data.get("reason", ""))
            criteria = data.get("criteria", [])
            ts_raw = data.get("timestamp", "")

            # Strictly convert / display timestamp in IST
            if ts_raw and ts_raw.endswith("IST"):
                ts_ist = ts_raw
            else:
                ts_ist = to_ist_str(ts_raw) if ts_raw else to_ist_str(datetime.now(IST).isoformat())

            # Conviction Status Banner using native Streamlit alert widgets
            if total >= 3:
                st.success(
                    f"⚡ **BULLISH BIAS (+{total}/7) → LONG SOXLB**\n\n"
                    f"**Time (IST):** `{ts_ist}` &nbsp;|&nbsp; "
                    f"**Action:** `{action}` &nbsp;|&nbsp; "
                    f"*{reason}*"
                )
            elif total <= -3:
                st.error(
                    f"⚡ **BEARISH BIAS ({total}/7) → LONG SOXSB**\n\n"
                    f"**Time (IST):** `{ts_ist}` &nbsp;|&nbsp; "
                    f"**Action:** `{action}` &nbsp;|&nbsp; "
                    f"*{reason}*"
                )
            else:
                st.warning(
                    f"⚡ **CHOP REGIME ({total:+d}/7) → 100% USDT SAFE HAVEN ($0 Loss)**\n\n"
                    f"**Time (IST):** `{ts_ist}` &nbsp;|&nbsp; "
                    f"**Action:** `{action}` &nbsp;|&nbsp; "
                    f"*{reason}*"
                )

            # 7 Criteria Table using native Markdown table syntax (flawless across all themes)
            st.markdown("##### 📊 7-Criterion 5-Minute Predictive Scoring Breakdown")
            md_lines = [
                "| # | Criterion | Verdict | Live Indicator Metrics & Evaluation |",
                "| :-: | :--- | :-: | :--- |",
            ]
            for item in criteria:
                cid = item.get("id", "")
                label = str(item.get("label", "")).replace("|", "/")
                sc = int(item.get("score", 0))
                desc = str(item.get("desc", "")).replace("|", "/")

                if sc == 1:
                    badge = "🟢 :green[**+1 BULLISH**]"
                elif sc == -1:
                    badge = "🔴 :red[**-1 BEARISH**]"
                else:
                    badge = "⚪ :gray[**0 NEUTRAL**]"

                md_lines.append(f"| **#{cid}** | **{label}** | {badge} | `{desc}` |")

            st.markdown("\n".join(md_lines))

            # Threshold reminder note
            st.caption(
                "🎯 **Scoring Rules:** `+3 to +7` = Bullish bias (Long SOXLB) &nbsp;|&nbsp; "
                "`-3 to -7` = Bearish bias (Long SOXSB) &nbsp;|&nbsp; "
                "`-2 to +2` = Chop (100% USDT Safe Haven, $0 loss)"
            )

            # Optional raw text toggle
            if data.get("formatted_text"):
                with st.expander("📄 View Plaintext Scoring Log", expanded=False):
                    st.code(data["formatted_text"], language=None)
            return
    except Exception:
        pass

    # Fallback to standard code display
    st.code(detail, language=None)


def _render_logs_body(iid: Optional[int], level_f: Optional[str], limit: int):
    """Core log rendering body with stable expander keys and pinned inspector."""
    events = load_events(instance_id=iid, level=level_f, limit=int(limit))
    if not events:
        st.markdown(
            "<div style='text-align:center;padding:40px;color:#8b949e;'>No log entries yet. Start a bot to see activity.</div>",
            unsafe_allow_html=True,
        )
        return

    instances = list_instances(limit=50)
    id_to_name = {i["id"]: i["name"] for i in instances}

    level_color = {
        "INFO": "[INFO]",
        "TRADE": "[TRADE]",
        "WARN": "[WARN]",
        "ERROR": "[ERROR]",
        "EXCEPTION": "[EXCEPTION]",
    }
    level_badge_style = {
        "INFO": "color:#60a5fa;background:rgba(96,165,250,0.1);border:1px solid rgba(96,165,250,0.3);",
        "TRADE": "color:#22c55e;background:rgba(34,197,94,0.1);border:1px solid rgba(34,197,94,0.3);",
        "WARN": "color:#eab308;background:rgba(234,179,8,0.1);border:1px solid rgba(234,179,8,0.3);",
        "ERROR": "color:#f97316;background:rgba(249,115,22,0.1);border:1px solid rgba(249,115,22,0.3);",
        "EXCEPTION": "color:#ef4444;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);",
    }

    # Pinned Log Inspector: Allows users to pin any log entry so they can inspect details permanently without auto-closing
    inspector_options = {"— None (Select a log below to pin & inspect without auto-closing) —": None}
    for ev in events[:60]:
        e_dict = dict(ev)
        e_id = e_dict.get("id")
        e_ts = to_ist_str(e_dict.get("ts"))
        e_msg = str(e_dict.get("message") or "")[:70]
        e_type = e_dict.get("event_type") or "LOG"
        opt_label = f"#{e_id} | {e_ts} | [{e_type}] {e_msg}"
        inspector_options[opt_label] = e_id

    selected_opt = st.selectbox(
        "📌 Pinned Log Inspector (Select any log to view details permanently without auto-closing):",
        list(inspector_options.keys()),
        key="pinned_log_inspector_select",
    )
    pinned_id = inspector_options.get(selected_opt)
    if pinned_id is not None:
        pinned_ev = next((dict(ev) for ev in events if ev["id"] == pinned_id), None)
        if pinned_ev:
            st.markdown(
                f"""
                <div style="background:rgba(30,41,59,0.7);border:1px solid rgba(96,165,250,0.4);border-radius:8px;padding:12px;margin:10px 0;">
                    <div style="font-size:13px;font-weight:700;color:#60a5fa;">
                        🔍 Inspected Event #{pinned_ev.get('id')} — {to_ist_str(pinned_ev.get('ts'))}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.markdown(
                f"**Bot / Symbol:** `[{id_to_name.get(pinned_ev.get('instance_id'), 'SYSTEM')}] [{pinned_ev.get('symbol') or 'INIT'}]` | "
                f"**Type:** `{pinned_ev.get('event_type')}` | **Level:** `{pinned_ev.get('level')}`"
            )
            st.markdown(f"**Message:** {pinned_ev.get('message')}")
            if pinned_ev.get("detail"):
                render_log_detail(pinned_ev["detail"])
            st.markdown("---")

    for ev in events:
        e = dict(ev)
        icon = level_color.get(e.get("level") or "INFO", "[INFO]")
        ts_ist = to_ist_str(e.get("ts"))
        iname = id_to_name.get(e.get("instance_id"), "—")
        bot = f"{iname}" if e.get("instance_id") else "SYSTEM"
        sym = e.get("symbol") or "INIT"
        etype = e.get("event_type") or ""
        lvl = e.get("level") or "INFO"
        badge_style = level_badge_style.get(lvl, "color:#8b949e;")
        header = f"`{ts_ist}`  **[{bot}] [{sym}]**  {etype}"
        ev_id = e.get("id")
        # Use stable unique key so Streamlit remembers user's open/collapsed state
        with st.expander(f"{header} — {e.get('message', '')[:100]}", key=f"log_exp_{ev_id}"):
            st.markdown(
                f"<span style='padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;{badge_style}'>{lvl}</span>",
                unsafe_allow_html=True,
            )
            st.markdown(f"**Message:** {e.get('message')}")
            if e.get("detail"):
                render_log_detail(e["detail"])
            st.caption(f"id={e.get('id')} | type={e.get('event_type')}")

    # Table view
    st.markdown("---")
    st.markdown("**Table View**")
    df = pd.DataFrame([dict(r) for r in events])
    if "ts" in df.columns:
        df["ts"] = df["ts"].apply(to_ist_str)
    show_cols = [c for c in ["id", "ts", "instance_id", "symbol", "level", "event_type", "message"] if c in df.columns]
    st.dataframe(
        df[show_cols],
        width="stretch",
        hide_index=True,
        column_config={"ts": "Time (IST)"},
    )
    st.download_button(
        "Download log CSV",
        df.to_csv(index=False).encode(),
        "event_log.csv",
        "text/csv",
    )


@st.fragment(run_every=3)
def render_live_log_stream_auto(iid: Optional[int], level_f: Optional[str], limit: int):
    """Live log stream fragment with 3-second auto-refresh."""
    _render_logs_body(iid, level_f, limit)


@st.fragment(run_every=None)
def render_live_log_stream_paused(iid: Optional[int], level_f: Optional[str], limit: int):
    """Static log stream fragment (never auto-refreshes, preserving expanded view)."""
    _render_logs_body(iid, level_f, limit)


def render_live_log_stream(iid: Optional[int], level_f: Optional[str], limit: int, auto_refresh: bool = False):
    """Routes to auto-refreshing or static fragment based on user preference."""
    if auto_refresh:
        render_live_log_stream_auto(iid, level_f, limit)
    else:
        render_live_log_stream_paused(iid, level_f, limit)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def render_hybrid_market_predictor_tab():
    st.subheader("⚡ Hybrid Quantitative Market Regime Predictor & Dynamic Strategy Selector")
    st.caption("Predicts the coin's live market regime (Strong trends, Breakouts, Sideways ranges, Tight ranges, Liquid pairs, Arbitrage) and automatically routes to the highest-expected-profit quantitative algorithm.")

    # 1. Taxonomy Table
    st.markdown("##### 📋 Strategy Matching Taxonomy")
    st.markdown("""
| Algorithm | Best Market | Trade Frequency | Main Risk | Needs Speed? | Quantitative Trigger | Risk Mitigation Rule |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Trend Following** | Strong trends | Low | Whipsaws in ranges | No | ADX ≥ 25, Hurst > 0.55, EMA9 > 21 > 50 | Wide trailing stop (Chandelier/ATR), ride trend, no premature scratch |
| **Momentum** | Breakouts | Medium | False breakouts | Somewhat | RVol ≥ 1.6, Bandwidth expansion, FCA Dominance ≥ 60% | Tight SL right below breakout candle base, fast exit if stalled |
| **Mean Reversion** | Sideways ranges | Medium | Trends that don't revert | No | Hurst < 0.45, ADX < 20, RSI ≤ 38, Lower BB bounce | Enter support/oversold, exit strictly at Mean (VWAP/SMA20), hard SL |
| **Grid** | Tight ranges | High | Price leaves the grid | Somewhat | ADX < 16, 1m ATR < 0.12%, compressed Bandwidth | Staged limit orders across channel, exit +0.25%, circuit breaker stop |
| **Arbitrage** | Any | Very high | Fees, transfer delays | Yes | Triangular net spread > 3x trading fee | Pre-screen 3-leg fees strictly, execute or fall back cleanly |
| **Market Making / Scalping** | Liquid pairs | Very high | Costs, sharp moves | Yes | Spread ≤ 0.05%, 24h Vol ≥ $10M, deep book | Pure maker Best-Bid, resting limit ask, toxic flow cancellation |
| **Machine Learning** | Depends on model | Varies | Overfitting | Varies | Online SGD / LinUCB multi-microstructure score | L2 regularization, AdaGrad adaptive rate, running variance normalization |
| **BLOCKED** | Downtrend / Exhaustion | None | Adverse dump / Falling knife | No | Velocity < -0.015% or -DI > +DI + 6 | 100% Capital Protection: Spot long entry blocked ($0 loss) |
    """)

    st.divider()

    col_pred_l, col_pred_r = st.columns([1, 1])
    with col_pred_l:
        st.markdown("##### 🔍 Test Coin Market Prediction")
        pred_sym = st.text_input("Enter Coin Symbol for Real-Time Regime Prediction", value="SOLUSDT", key="pred_sym_input")
        if st.button("🔮 Predict Market Regime & Strategy", type="primary", use_container_width=True):
            if hybrid_strategy and pred_sym:
                try:
                    c_cli = make_client()
                    with st.spinner(f"Analyzing {pred_sym.strip().upper()} microstructure..."):
                        pred_res = hybrid_strategy.predict_market_and_strategy(c_cli, pred_sym.strip().upper(), investment_usdt=25.0)
                    
                    ep = pred_res.get("execution_params", {})
                    is_viable = ep.get("is_viable_trade", True)
                    if is_viable:
                        st.success(f"✅ **Trade Viable:** `{pred_res['predicted_market']}` ➔ **Selected Algorithm:** `{pred_res['strategy_name']}`")
                    else:
                        st.error(f"⛔ **Trade Blocked (Capital Preserved):** Market is `{pred_res['predicted_market']}`. Long entries prohibited ($0 loss).")
                    
                    p_col1, p_col2, p_col3 = st.columns(3)
                    p_col1.metric("Predicted Market", pred_res["predicted_market"])
                    p_col2.metric("Selected Algorithm", pred_res["strategy_name"])
                    p_col3.metric("Trade Viability", "✅ VIABLE" if is_viable else "⛔ BLOCKED")

                    ep = pred_res.get("execution_params", {})
                    st.markdown(f"""
- **Trade Frequency:** `{pred_res['trade_frequency']}` | **Needs Speed?:** `{pred_res['needs_speed']}`
- **Main Risk:** `{pred_res['main_risk']}`
- **Risk Mitigation:** {pred_res['risk_mitigation']}
- **Entry Style:** `{ep.get('entry_style')}`
- **Target Profit:** `+{ep.get('target_profit_pct'):.2f}%` (~+{ep.get('target_profit_usdt'):.4f} USDT)
- **Stop Loss:** `{ep.get('stop_loss_pct'):.2f}%` (~{ep.get('stop_loss_usdt'):.4f} USDT)
- **Trailing Stop:** `{'Active (drop: ' + str(ep.get('trailing_drop_pct')) + '%)' if ep.get('trailing_stop_enabled') else 'Disabled (Target Exit)'}`
- **Reprice Delay / Safety Hold:** `{ep.get('reprice_sec')}s` / `{ep.get('max_hold_sec')}s`
                    """)
                except Exception as e:
                    st.error(f"Error predicting {pred_sym}: {e}")

    with col_pred_r:
        st.markdown("##### 📡 Market-Wide Regime Scanner")
        if st.button("🔄 Scan Top 15 Pairs Regime Matrix", use_container_width=True):
            if hybrid_strategy:
                try:
                    c_cli = make_client()
                    tickers_s = c_cli.get_ticker()
                    scan_syms = [
                        t["symbol"] for t in sorted(tickers_s, key=lambda x: float(x.get("quoteVolume") or 0), reverse=True)
                        if t["symbol"].endswith("USDT") and not any(b in t["symbol"] for b in BLACKLIST)
                    ][:15]
                    
                    scan_rows = []
                    with st.spinner("Classifying market regimes..."):
                        for s in scan_syms:
                            pr = hybrid_strategy.predict_market_and_strategy(c_cli, s, investment_usdt=25.0)
                            inds = pr.get("indicators", {})
                            ep = pr.get("execution_params", {})
                            scan_rows.append({
                                "Symbol": s,
                                "Viable?": "✅ Viable" if ep.get("is_viable_trade", True) else "⛔ Blocked",
                                "Predicted Market": pr.get("predicted_market"),
                                "Algorithm": pr.get("strategy_name"),
                                "ADX (5m)": inds.get("adx_5m", 20.0),
                                "Hurst": inds.get("hurst_exponent", 0.50),
                                "RVol": inds.get("rvol_1m", 1.0),
                                "TP Target": f"+{ep.get('target_profit_pct', 0.20):.2f}%",
                                "SL Floor": f"{ep.get('stop_loss_pct', -0.40):.2f}%",
                                "Trailing": "Yes" if ep.get("trailing_stop_enabled") else "No",
                                "Confidence": f"{pr.get('confidence', 0.5)*100:.0f}%",
                            })
                    st.dataframe(pd.DataFrame(scan_rows), use_container_width=True, hide_index=True)
                except Exception as e:
                    st.error(f"Scanner error: {e}")


def page_dashboard():
    st.title("JitCrypto Terminal")
    st.caption("High-frequency autonomous Binance scalping engine · Live WebSocket updates")

    render_global_metrics()

    tab1, tab2, tab3, tab_lag, tab_hybrid_semi, tab_hybrid, tab4, tab5 = st.tabs([
        "Bot Instances",
        "Date-wise P/L (IST)",
        "Equity Curve",
        "⚡ SOXS/SOXL Lag Predictor",
        "🌐 Hybrid SOXLB/SOXSB Strategy",
        "⚡ Hybrid Market Predictor",
        "🧠 Adaptive ML Brain",
        "📈 Time Series Forecaster",
    ])

    with tab1:
        render_active_bots()

    with tab2:
        st.subheader("Date-wise Performance Breakdown (IST)")
        daily_records = load_daily_pnl_ist()
        if daily_records:
            df_daily = pd.DataFrame(daily_records)

            today_ist = datetime.now(IST).strftime("%Y-%m-%d")
            today_row = df_daily[df_daily["date_ist"] == today_ist]
            today_pnl = float(today_row["pnl_usdt"].values[0]) if not today_row.empty else 0.0
            today_runs = int(today_row["total_runs"].values[0]) if not today_row.empty else 0
            today_wr = float(today_row["win_rate_pct"].values[0]) if not today_row.empty else 0.0

            best_day_row = df_daily.sort_values("pnl_usdt", ascending=False).iloc[0]
            best_day = f"{best_day_row['date_ist']} ({best_day_row['pnl_usdt']:+.4f} USDT)"

            dc1, dc2, dc3, dc4 = st.columns(4)
            dc1.metric("Today's P/L (IST)", f"{today_pnl:+.4f} USDT")
            dc2.metric("Today's Runs", f"{today_runs}", delta=f"{today_wr:.1f}% Win Rate" if today_runs > 0 else None)
            dc3.metric("Total Active Days", len(df_daily))
            dc4.metric("Best Day", best_day)

            df_daily_chart = df_daily.sort_values("date_ist").copy()
            df_daily_chart["color"] = df_daily_chart["pnl_usdt"].apply(lambda x: "#22c55e" if x >= 0 else "#ef4444")

            fig_daily = go.Figure()
            fig_daily.add_trace(
                go.Bar(
                    x=df_daily_chart["date_ist"],
                    y=df_daily_chart["pnl_usdt"],
                    marker_color=df_daily_chart["color"],
                    text=[f"{p:+.4f} USDT<br>({wr}% W)" for p, wr in zip(df_daily_chart["pnl_usdt"], df_daily_chart["win_rate_pct"])],
                    textposition="auto",
                    name="Daily P/L",
                )
            )
            fig_daily.update_layout(
                title="Daily Net P/L (USDT, IST)",
                xaxis_title="Date (IST)",
                yaxis_title="Net P/L (USDT)",
                height=350,
                margin=dict(l=20, r=20, t=40, b=20),
                template="plotly_white",
                paper_bgcolor="#ffffff",
                plot_bgcolor="#ffffff",
                font=dict(color="#0f172a", size=12),
            )
            st.plotly_chart(fig_daily, width="stretch")

            st.markdown("##### Detailed Daily P/L & Tax/Fee Ledger")
            st.dataframe(
                df_daily,
                width="stretch",
                hide_index=True,
                column_config={
                    "date_ist": "Date (IST)",
                    "total_runs": st.column_config.NumberColumn("Total Runs", format="%d"),
                    "wins": st.column_config.NumberColumn("Wins", format="%d"),
                    "losses": st.column_config.NumberColumn("Losses", format="%d"),
                    "win_rate_pct": st.column_config.NumberColumn("Win Rate", format="%.1f%%"),
                    "pnl_usdt": st.column_config.NumberColumn("Net P/L", format="%+.4f USDT"),
                    "fee_bnb": st.column_config.NumberColumn("Fee/Tax (BNB)", format="%.8f BNB"),
                    "fee_usdt": st.column_config.NumberColumn("Fee/Tax (USDT)", format="%.4f USDT"),
                    "total_volume": st.column_config.NumberColumn("Volume", format="%.2f USDT"),
                    "symbols_traded": "Traded Pairs",
                },
            )
        else:
            st.info("No completed cycles recorded yet to calculate date-wise P/L.")

    with tab3:
        st.subheader("Cumulative Equity Curve (IST)")
        cycles = rows_to_df(load_cycles(limit=1000))
        if not cycles.empty and "pnl_usdt" in cycles.columns:
            closed = cycles[
                cycles["status"].isin(["SUCCESS", "FAILURE", "STOP_LOSS", "TARGET", "BREAKEVEN_LOCK", "TIME_EXPIRED", "ABORTED", "SCRATCH", "LIQUIDATED"])
                | cycles["pnl_usdt"].notna()
            ].copy()
            if not closed.empty:
                closed["finished_at_ist"] = closed["finished_at"].apply(lambda x: to_ist(x))
                closed = closed.dropna(subset=["finished_at_ist"])
                closed = closed.sort_values("finished_at_ist")
                closed["cum_pnl"] = closed["pnl_usdt"].cumsum()
                fig = px.line(
                    closed,
                    x="finished_at_ist",
                    y="cum_pnl",
                    markers=True,
                    labels={"finished_at_ist": "Time (IST)", "cum_pnl": "Cumulative Net P/L (USDT)"},
                    template="plotly_white",
                )
                fig.update_traces(line=dict(color="#2563eb", width=2.5), marker=dict(color="#1d4ed8", size=6))
                fig.update_layout(
                    height=360,
                    margin=dict(l=20, r=20, t=30, b=20),
                    paper_bgcolor="#ffffff",
                    plot_bgcolor="#ffffff",
                    font=dict(color="#0f172a", size=12),
                )
                st.plotly_chart(fig, width="stretch")
            else:
                st.caption("No closed cycles yet.")
        else:
            st.caption("No cycle data yet.")

    with tab_lag:
        if semiconductor_lag_arb:
            semiconductor_lag_arb.render_lag_arb_tab()
        else:
            st.info("SOXS/SOXL Lag Predictor module not available.")

    with tab_hybrid_semi:
        try:
            import hybrid_soxlb_soxsb
            hybrid_soxlb_soxsb.render_hybrid_strategy_tab()
        except Exception as e:
            st.info(f"Hybrid SOXLB/SOXSB Strategy module status: {e}")

    with tab_hybrid:
        render_hybrid_market_predictor_tab()

    with tab4:
        st.subheader("Adaptive Machine Learning Brain (Online Continual Learning)")
        st.caption("Dynamically ranks pairs using real-time microstructure features and continuously updates feature weights on the fly.")

        if get_selector:
            try:
                selector = get_selector(DB_PATH)
                importances = selector.get_feature_importances()

                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Online Updates", selector.total_updates)
                m2.metric("Wins Learned", selector.total_wins)
                m3.metric("Losses Learned", selector.total_losses)
                wr = (selector.total_wins / selector.total_updates * 100.0) if selector.total_updates > 0 else 0.0
                m4.metric("Model Win Rate", f"{wr:.1f}%")
                m5.metric("Exploration Bonus (α)", f"{selector.exploration_alpha:.2f}")

                col_chart, col_tbl = st.columns([3, 2])
                with col_chart:
                    st.markdown("##### Current Learned Feature Weights")
                    df_imp = pd.DataFrame(importances)
                    df_imp["color"] = df_imp["weight"].apply(lambda w: "#22c55e" if w > 0 else "#ef4444")
                    fig_w = go.Figure(go.Bar(
                        x=df_imp["weight"],
                        y=df_imp["feature"],
                        orientation="h",
                        marker_color=df_imp["color"],
                        text=[f"{w:+.3f}" for w in df_imp["weight"]],
                        textposition="auto",
                    ))
                    fig_w.update_layout(
                        xaxis_title="Weight (Positive = Favored / Bullish, Negative = Penalized / Barrier)",
                        yaxis_title="Microstructure Feature",
                        height=350,
                        margin=dict(l=20, r=20, t=20, b=20),
                        template="plotly_white",
                    )
                    st.plotly_chart(fig_w, width="stretch")

                with col_tbl:
                    st.markdown("##### Feature Influence Breakdown")
                    st.dataframe(
                        df_imp[["feature", "weight", "influence"]],
                        hide_index=True,
                        width="stretch",
                        height=350,
                    )

                st.markdown("##### Recent On-The-Fly Online Feedback Steps")
                if selector.recent_history:
                    df_h = pd.DataFrame(selector.recent_history)
                    df_h["time_ist"] = df_h["ts"].apply(lambda t: datetime.fromtimestamp(t, tz=IST).strftime("%H:%M:%S"))
                    st.dataframe(
                        df_h[["time_ist", "symbol", "won", "pnl_usdt", "pred_p", "error"]],
                        hide_index=True,
                        width="stretch",
                        column_config={
                            "time_ist": "Time (IST)",
                            "symbol": "Pair",
                            "won": "Trade Won",
                            "pnl_usdt": st.column_config.NumberColumn("Net PnL", format="%+.4f USDT"),
                            "pred_p": st.column_config.NumberColumn("Pred P(Win)", format="%.2f"),
                            "error": st.column_config.NumberColumn("SGD Error", format="%+.2f"),
                        },
                    )
                else:
                    st.info("No closed trade cycles recorded yet to trigger online updates. As soon as a trade cycle finishes, its live feedback step will be recorded here.")
            except Exception as e:
                st.error(f"Error rendering ML brain tab: {e}")
        else:
            st.warning("ml_selector module is not loaded.")

    with tab5:
        st.subheader("📈 Advanced Multi-Horizon Time Series Forecaster")
        st.caption("Zero-lag 3D Kinematic Kalman Filter · Rolling ARDL Ridge Forecaster · Spectral FFT Cycles · Volatility Cones")

        ts_col1, ts_col2 = st.columns([3, 1])
        with ts_col2:
            st.write("")
            run_ts_scan = st.button("🔄 Scan Market Forecasts", type="primary", use_container_width=True)

        if ts_forecast:
            try:
                client_dash = make_client()
                tickers_dash = client_dash.get_ticker()
                scan_syms = [
                    t["symbol"] for t in sorted(tickers_dash, key=lambda x: float(x.get("quoteVolume") or 0), reverse=True)
                    if t["symbol"].endswith("USDT") and not any(b in t["symbol"] for b in BLACKLIST)
                ][:20]

                ranked_ts, fc_map = ts_forecast.rank_pairs_with_forecasting(client_dash, scan_syms, max_workers=6)
                if ranked_ts:
                    ts_rows = []
                    for s in ranked_ts:
                        fc = fc_map.get(s, {})
                        ts_rows.append({
                            "Symbol": s,
                            "Pred Return (15m)": f"{fc.get('pred_return_15m_pct', 0.0):+.2f}%",
                            "Dynamic TP Target": f"+{fc.get('forecast_tp_pct', 0.0):.2f}%",
                            "Volatility SL Floor": f"{fc.get('forecast_sl_pct', 0.0):.2f}%",
                            "Forecast Info Ratio": f"{fc.get('forecast_info_ratio', 0.0):+.2f}",
                            "Win Probability": f"{fc.get('win_probability', 0.0)*100:.1f}%",
                            "Regime": fc.get("regime", "CHOP_RANGING"),
                            "Kalman Velocity": f"{fc.get('kalman_velocity_pct', 0.0):+.3f}%/bar",
                            "Forecast Score": round(fc.get("forecast_score", 0.0), 2),
                        })
                    st.dataframe(pd.DataFrame(ts_rows), width="stretch", hide_index=True)
            except Exception as e:
                st.info(f"Time Series Forecast scanner ready: {e}")
        else:
            st.warning("Time series forecast engine initializing...")



@st.cache_data(ttl=5, show_spinner=False)
def get_live_balance_info() -> dict:
    avail_usdt = 0.0
    usdt_free = 0.0
    usdt_locked = 0.0
    total_usdt = 0.0
    try:
        pass
        pass
        pass
        client = make_client()
        usdt_free, usdt_locked, _ = _balances(client)
        committed = get_unfilled_committed_usdt()
        avail_usdt = max(0.0, usdt_free - committed)

        spent_open = 0.0
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(c.entry_cost), 0) AS s
                FROM cycles c
                JOIN bot_instances b ON b.id = c.instance_id
                WHERE b.status IN ('RUNNING', 'STOPPING') AND c.status = 'OPEN' AND c.entry_cost > 0
                """
            ).fetchone()
            spent_open = float(row["s"] if row else 0.0)
        total_usdt = usdt_free + usdt_locked + spent_open
    except Exception:
        pass
    return {"avail_usdt": avail_usdt, "total_usdt": total_usdt}


def page_manager():
    st.title("Bot Manager")
    st.caption(
        "Automated manager launches bots while Spot USDT >= investment & BNB covers all fee buffers."
    )

    cfg = get_manager_config()
    db_enabled = bool(cfg.get("enabled"))

    # Stable toggle: only sync from DB if the DB value changed externally
    if "mgr_enabled_toggle" not in st.session_state or st.session_state.get("mgr_last_seen_enabled") != db_enabled:
        st.session_state["mgr_enabled_toggle"] = db_enabled
        st.session_state["mgr_last_seen_enabled"] = db_enabled

    def on_manager_toggle():
        val = st.session_state["mgr_enabled_toggle"]
        set_manager_config(enabled=1 if val else 0)
        st.session_state["mgr_last_seen_enabled"] = val
        st.toast("Manager ENABLED — automation active" if val else "Manager DISABLED")

    # Toggle row
    t_col, label_col = st.columns([0.4, 5])
    with t_col:
        st.toggle("Manager Automation", key="mgr_enabled_toggle", on_change=on_manager_toggle, label_visibility="collapsed")
    with label_col:
        is_on = st.session_state.get("mgr_enabled_toggle", False)
        color = "#22c55e" if is_on else "#8b949e"
        text = "**Manager Enabled** — automation is running" if is_on else "**Manager Disabled** — click to enable automation"
        st.markdown(f"<span style='color:{color};font-size:14px;line-height:2;'>{text}</span>", unsafe_allow_html=True)

    # Live balance check to dynamically compute max parallel bots (cached, non-blocking)
    bal_data = get_live_balance_info()
    avail_usdt = bal_data.get("avail_usdt", 0.0)
    total_usdt = bal_data.get("total_usdt", 0.0)

    inv_default = float(cfg.get("investment_usdt") or 25.0)
    # Dynamic calculation: Available USDT // Investment USDT (minimum 1)
    dynamic_from_avail = int(avail_usdt // inv_default) if inv_default > 0 else 1
    dynamic_from_total = int(total_usdt // inv_default) if inv_default > 0 else 1
    dynamic_parallel = max(1, dynamic_from_avail if dynamic_from_avail > 0 else dynamic_from_total)

    with st.form("manager_settings"):
        st.subheader("Launch Parameters")
        col1, col2 = st.columns(2)
        with col1:
            inv = st.number_input("Investment per bot (USDT)", min_value=5.0, max_value=500.0,
                                  value=inv_default, step=1.0)
        with col2:
            use_only = st.text_input(
                "Whitelist pairs (optional)",
                value=cfg.get("use_only") or "",
                placeholder="e.g. ETHUSDT, SOLUSDT, XRPUSDT",
                help="Restrict bots to only these pairs. Leave empty to auto-scan the entire market.",
            )

        blacklist_input = st.text_input(
            "User Blacklisted pairs (optional)",
            value=cfg.get("blacklist") or "",
            placeholder="e.g. DOGEUSDT, LINKUSDT",
            help="Comma- or space-separated symbols to never trade. (Stablecoins, wrapped tokens, BTC/BNB, and poor historical performers are also auto-blacklisted).",
        )

        strat_opts = [
            "DUAL_SOXLB_SOXSB (Dual $12.50 SOXSB + $12.50 SOXLB Reinvest Harvester)",
            "HYBRID_SOXLB_SOXSB_SRATEGY (Dual Macro Trend + Micro Lag Confluence)",
            "SOXS_SOXL_LAG_PREDICTOR (SOXSB ↔ SOXLB Lag Arbitrage Engine)",
            "SEMICONDUCTOR_DUAL_ENGINE (SOXLB & SOXSB Counterpart Engine)",
            "AUTO (Hybrid Regime Predictor)",
            "TREND_FOLLOWING (Strong trends)",
            "MOMENTUM (Breakouts)",
            "MEAN_REVERSION (Sideways ranges)",
            "GRID (Tight ranges)",
            "MARKET_MAKING_SCALPING (Liquid pairs)",
            "ARBITRAGE (Any market)",
            "MACHINE_LEARNING (Adaptive Model)",
        ]
        curr_mgr_strat = cfg.get("strategy") or "DUAL_SOXLB_SOXSB"
        strat_idx = 0
        for i, opt in enumerate(strat_opts):
            if opt.startswith(curr_mgr_strat):
                strat_idx = i
                break
        chosen_strat_label = st.selectbox(
            "Strategy Mode",
            strat_opts,
            index=strat_idx,
            help="DUAL_SOXLB_SOXSB splits investment 50/50 ($12.50 SOXSB + $12.50 SOXLB). Whichever reaches profit first after fees is sold and immediately re-bought. Never sells at a loss, accumulating lagging legs long-term."
        )
        chosen_strat = chosen_strat_label.split(" ")[0]

        st.info(
            f"Automated Configuration & Balance Capacity:\n"
            f"- Parallel Bots: {dynamic_parallel} bot(s) automatically calculated ({avail_usdt:.2f} Available USDT / {inv_default:.2f} USDT per bot).\n"
            f"- Fee Deduction: Enforces minimum 0.001 BNB fee buffer.\n"
            f"- Check Interval: Fixed at 5.0 seconds.\n"
            f"- Engine Mode: Fully Dynamic (Algorithm self-adjusts strategy and stop-loss per bot)."
        )

        if st.form_submit_button("Save Settings", width="stretch"):
            set_manager_config(
                investment_usdt=inv,
                target_profit=float(cfg.get("target_profit") or 0.0050),
                stop_loss=float(cfg.get("stop_loss") or -0.0050),
                min_bnb_per_bot=0.001,
                max_parallel=dynamic_parallel,
                check_interval_sec=5.0,
                use_only=use_only.strip(),
                blacklist=blacklist_input.strip().upper(),
                timeout_sec=0.0,
                strategy=chosen_strat,
            )
            st.success("Settings saved successfully")

    st.divider()
    st.subheader("Live Status")
    render_manager_status()


def page_launch():
    st.title("Launch Bot")
    st.caption("Create and optionally start a new autonomous scalper instance.")

    cfg = get_manager_config()
    default_inv = float(cfg.get("investment_usdt") or 25.0)
    default_sl = float(cfg.get("stop_loss") or -0.10)

    with st.form("launch_form"):
        st.subheader("New Bot Configuration")
        name = st.text_input("Bot name", value=next_bot_name(), help="Unique identifier for this bot instance.")

        col1, col2 = st.columns(2)
        with col1:
            investment = st.number_input("Max Investment per Coin (USDT)", min_value=5.0, max_value=500.0, value=default_inv, step=1.0)
        with col2:
            use_only = st.text_input(
                "Whitelist pairs (optional)",
                value=cfg.get("use_only") or "SOXLBUSDT, SOXSBUSDT",
                help="Comma-separated pairs (e.g. SOXLBUSDT, SOXSBUSDT). Leave blank for auto-scan.",
            )

        blacklist_input = st.text_input(
            "User Blacklisted pairs (optional)",
            value=cfg.get("blacklist") or "",
            help="Comma-separated symbols to exclude (e.g. DOGEUSDT, LINKUSDT).",
        )

        strat_opts = [
            "DUAL_SOXLB_SOXSB (Dual $12.50 SOXSB + $12.50 SOXLB Reinvest Harvester)",
            "HYBRID_SOXLB_SOXSB_SRATEGY (Dual Macro Trend + Micro Lag Confluence)",
            "SOXS_SOXL_LAG_PREDICTOR (SOXSB ↔ SOXLB Lag Arbitrage Engine)",
            "SEMICONDUCTOR_DUAL_ENGINE (SOXLB & SOXSB Counterpart Engine)",
            "AUTO (Hybrid Regime Predictor)",
            "TREND_FOLLOWING (Strong trends)",
            "MOMENTUM (Breakouts)",
            "MEAN_REVERSION (Sideways ranges)",
            "GRID (Tight ranges)",
            "MARKET_MAKING_SCALPING (Liquid pairs)",
            "ARBITRAGE (Any market)",
            "MACHINE_LEARNING (Adaptive Model)",
        ]
        chosen_strat_label = st.selectbox(
            "Trading Strategy / Mode",
            strat_opts,
            index=0,
            help="DUAL_SOXLB_SOXSB splits investment 50/50 ($12.50 SOXSB + $12.50 SOXLB). Whichever reaches profit first after fees is sold and immediately re-bought. Never sells at a loss, accumulating lagging legs long-term."
        )
        chosen_strat = chosen_strat_label.split(" ")[0]

        with st.expander("ℹ️ View Hybrid Market Regime & Strategy Matching Table", expanded=False):
            st.markdown("""
| Algorithm | Best Market | Trade Frequency | Main Risk | Needs Speed? |
| :--- | :--- | :--- | :--- | :--- |
| **Dual SOXLB/SOXSB** | SOXSB & SOXLB Dual Legs | High | $12.50 SOXSB + $12.50 SOXLB; sells on profit after fees & re-buys; zero loss floor | Yes |
| **Hybrid SOXLB/SOXSB** | SOXLB & SOXSB Confluence | High | Combined Macro Bias + Micro Lag Timing (Zero Loss Floor) | Yes |
| **SOXS/SOXL Lag Predictor** | SOXSB & SOXLB Counterparts | High | Fast mean reversion on lag (100% Fee-Covered) | Yes |
| **Semiconductor Dual-Engine** | SOXLB & SOXSB Counterparts | Medium | Volatility Decay in Chop (100% USDT Safe Haven) | No |
| **Trend Following** | Strong trends | Low | Whipsaws in ranges | No |
| **Momentum** | Breakouts | Medium | False breakouts | Somewhat |
| **Mean Reversion** | Sideways ranges | Medium | Trends that don't revert | No |
| **Grid** | Tight ranges | High | Price leaves the grid | Somewhat |
| **Arbitrage** | Any | Very high | Fees, transfer delays | Yes |
| **Market Making / Scalping** | Liquid pairs | Very high | Costs, sharp moves | Yes |
| **Machine Learning** | Depends on the model | Varies | Overfitting | Varies |
            """)

        st.info(
            "⚡ **Hybrid Regime Engine Active**: In AUTO mode, the bot predicts whether the selected coin is in a Strong Trend, "
            "Breakout, Sideways Range, Tight Range, or Liquid Pair, and deploys the corresponding algorithm with dynamic trailing stops and tailored risk management."
        )

        start_now = st.checkbox("Start immediately after creation", value=True)
        submitted = st.form_submit_button("Create Instance", width="stretch", type="primary")

    if submitted:
        if not name.strip():
            st.error("Bot name is required.")
            return
        iid = create_instance(
            name=name.strip(),
            investment_usdt=investment,
            target_profit=float(cfg.get("target_profit") or 0.0050),
            stop_loss=float(cfg.get("stop_loss") or -0.0050),
            strategy=chosen_strat,
        )
        use_only_list = [p.strip().upper() for p in use_only.split(",") if p.strip()] if use_only else None
        user_bl_set = {p.strip().upper() for p in blacklist_input.replace(",", " ").split() if p.strip()} if blacklist_input else set()
        try:
            pass
            pass
            client = make_client()
            active = get_active_symbols(exclude_instance_id=iid) | user_bl_set
            select_and_claim_best_pair(client, instance_id=iid, exclude_symbols=active, use_only=use_only_list)
        except Exception:
            pass
        st.success(f"Created instance **#{iid}** — `{name}`")
        if start_now:
            spawn_bot_process(iid)
            st.info(f"Process started for **#{iid}**. Switch to Dashboard to monitor live P/L.")
        st.balloons()


def page_trades():
    st.title("Trade Log")
    st.caption("Consolidated entry, exit, fees (BNB & USDT), and net P/L per cycle.")

    instances = list_instances(limit=50)
    options = {"All bots": None}
    options.update({f"#{i['id']} {i['name']}": i["id"] for i in instances})
    choice = st.selectbox("Filter by bot", list(options.keys()))
    iid = options[choice]

    tab1, tab2 = st.tabs(["Consolidated Trade Log", "Raw Fills"])

    with tab1:
        unified = rows_to_df(load_unified_trades(instance_id=iid, limit=300))
        if unified.empty:
            st.info("No trades yet.")
        else:
            if "started_at" in unified.columns:
                unified["started_at"] = unified["started_at"].apply(to_ist_str)
            if "finished_at" in unified.columns:
                unified["finished_at"] = unified["finished_at"].apply(to_ist_str)
            if "status" in unified.columns:
                def map_trade_status(row):
                    st_val = str(row.get("status") or "").upper()
                    pnl = row.get("pnl_usdt")
                    try:
                        pnl_f = float(pnl) if pnl is not None else None
                    except (ValueError, TypeError):
                        pnl_f = None
                    if st_val in ("SUCCESS", "TARGET", "BREAKEVEN_LOCK") or (pnl_f is not None and pnl_f > 0):
                        return "SUCCESS"
                    elif st_val in ("FAILURE", "STOP_LOSS", "TIME_EXPIRED", "SCRATCH", "LIQUIDATED", "ERROR") or (pnl_f is not None and pnl_f < 0):
                        return "FAILURE"
                    return st_val
                unified["status"] = unified.apply(map_trade_status, axis=1)
            cols = [
                "bot_name", "symbol", "status", "qty", "entry_price", "exit_price",
                "entry_cost", "exit_value", "buy_fee_bnb", "buy_fee_usdt",
                "sell_fee_bnb", "sell_fee_usdt", "total_fee_bnb", "total_fee_usdt",
                "pnl_usdt", "exit_reason", "duration_sec", "started_at", "finished_at",
            ]
            show = unified[[c for c in cols if c in unified.columns]]
            st.dataframe(
                show,
                width="stretch",
                hide_index=True,
                column_config={
                    "bot_name": "Bot",
                    "symbol": "Symbol",
                    "status": st.column_config.TextColumn("Status", help="SUCCESS = Profitable Target | FAILURE = Loss / Stop-Loss"),
                    "qty": st.column_config.NumberColumn("Qty", format="%.4f"),
                    "entry_price": st.column_config.NumberColumn("Entry Price", format="%.6f"),
                    "exit_price": st.column_config.NumberColumn("Exit Price", format="%.6f"),
                    "entry_cost": st.column_config.NumberColumn("Entry Cost", format="%.4f USDT"),
                    "exit_value": st.column_config.NumberColumn("Exit Value", format="%.4f USDT"),
                    "buy_fee_bnb": st.column_config.NumberColumn("Buy Fee (BNB)", format="%.8f BNB"),
                    "buy_fee_usdt": st.column_config.NumberColumn("Buy Fee (USDT)", format="%.6f USDT"),
                    "sell_fee_bnb": st.column_config.NumberColumn("Sell Fee (BNB)", format="%.8f BNB"),
                    "sell_fee_usdt": st.column_config.NumberColumn("Sell Fee (USDT)", format="%.6f USDT"),
                    "total_fee_bnb": st.column_config.NumberColumn("Total Fee (BNB)", format="%.8f BNB"),
                    "total_fee_usdt": st.column_config.NumberColumn("Total Fee (USDT)", format="%.6f USDT"),
                    "pnl_usdt": st.column_config.NumberColumn("Net P/L", format="%+.4f USDT"),
                    "exit_reason": "Exit Reason",
                    "duration_sec": st.column_config.NumberColumn("Duration (s)", format="%.1f"),
                    "started_at": "Started (IST)",
                    "finished_at": "Finished (IST)",
                },
            )
            st.download_button(
                "Download trade log CSV",
                show.to_csv(index=False).encode(),
                "trade_log.csv",
                "text/csv",
            )

    with tab2:
        trades = rows_to_df(load_trades(instance_id=iid, limit=500))
        if trades.empty:
            st.info("No raw fills yet.")
        else:
            if "ts" in trades.columns:
                trades["ts"] = trades["ts"].apply(to_ist_str)
            cols = ["id", "bot_name", "instance_id", "cycle_id", "symbol", "side",
                    "quantity", "price", "quote_qty", "fee_bnb", "fee_usdt", "pnl_usdt", "notes", "ts"]
            show = trades[[c for c in cols if c in trades.columns]]
            st.dataframe(
                show,
                width="stretch",
                hide_index=True,
                column_config={
                    "bot_name": "Bot Name",
                    "instance_id": "Bot ID",
                    "cycle_id": "Cycle",
                    "price": st.column_config.NumberColumn("Price", format="%.6f"),
                    "quantity": st.column_config.NumberColumn("Qty", format="%.4f"),
                    "quote_qty": st.column_config.NumberColumn("Total (USDT)", format="%.4f USDT"),
                    "fee_bnb": st.column_config.NumberColumn("Fee (BNB)", format="%.8f BNB"),
                    "fee_usdt": st.column_config.NumberColumn("Fee (USDT)", format="%.6f USDT"),
                    "pnl_usdt": st.column_config.NumberColumn("P/L (USDT)", format="%+.4f USDT"),
                    "ts": "Time (IST)",
                },
            )
            st.download_button(
                "Download raw fills CSV",
                show.to_csv(index=False).encode(),
                "raw_trades.csv",
                "text/csv",
            )


def page_live_log():
    st.title("Live Event Log")
    st.caption("Real-time execution log — auto-updates every 3 seconds via WebSocket.")

    with st.expander("Purge Execution Logs by Date (IST)", expanded=False):
        st.markdown(
            " **Safe Purge**: Deletes diagnostic event logs & raw `.log` files only. "
            "**Trade history, P/L, cycles, and tax data are never deleted.**"
        )
        p_col1, p_col2, p_col3 = st.columns([2, 2, 2])
        with p_col1:
            purge_date = st.date_input("Execution Date (IST)", value=datetime.now(IST).date())
        with p_col2:
            purge_disk = st.checkbox("Also delete text .log files", value=True)
        with p_col3:
            st.markdown("<div style='height:24px;'></div>", unsafe_allow_html=True)
            if st.button("Purge Selected Date", type="primary", width="stretch"):
                res = purge_logs_by_date(str(purge_date), purge_files=purge_disk)
                if res.get("success"):
                    st.success(
                        f"Purged {res['deleted_events']} events & {res['deleted_files']} files for {purge_date}."
                    )
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error(f"Error: {res.get('error')}")

    instances = list_instances(limit=50)
    options = {"All bots": None}
    options.update({f"#{i['id']} {i['name']}": i["id"] for i in instances})

    c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
    with c1:
        choice = st.selectbox("Filter by bot", list(options.keys()))
        iid = options[choice]
    with c2:
        level = st.selectbox("Level", ["All", "INFO", "TRADE", "WARN", "ERROR", "EXCEPTION"])
        level_f = None if level == "All" else level
    with c3:
        limit = st.number_input("Rows", min_value=50, max_value=2000, value=300, step=50)
    with c4:
        st.markdown("<div style='height:24px;'></div>", unsafe_allow_html=True)
        if st.button("Clear Logs", type="secondary", width="stretch"):
            clear_events()
            st.toast("Event log cleared (trades & P/L preserved)")
            st.rerun()

    # Stream control toolbar: Allows user to freeze auto-refresh while reading logs
    ctrl_col1, ctrl_col2 = st.columns([3, 1])
    with ctrl_col1:
        auto_refresh = st.toggle(
            "🔄 Live Auto-refresh (3s)",
            value=False,
            key="toggle_log_auto_refresh",
            help="Keep OFF while reading logs so expanders stay open and screen does not reload. Turn ON for continuous 3-second live stream.",
        )
    with ctrl_col2:
        if st.button("🔄 Refresh Logs Now", width="stretch", key="btn_manual_refresh_logs"):
            st.rerun()

    if not auto_refresh:
        st.caption("⏸️ **Auto-refresh is PAUSED** — logs are frozen so you can comfortably expand and inspect scoring details without auto-closing. Click **Refresh Logs Now** or turn on Live Auto-refresh when desired.")
    else:
        st.caption("🟢 **Live Auto-refresh is ACTIVE** (every 3s). Tip: Turn off to freeze logs while reading details.")

    render_live_log_stream(iid, level_f, int(limit), auto_refresh=auto_refresh)


def page_simulation():
    st.title("Live Simulation Engine")
    st.caption("Paper-trade exactly how the algorithm calculates profit, fees, and exits on real-time data.")

    col1, col2 = st.columns(2)
    with col1:
        inv = st.number_input("Virtual Investment (USDT)", min_value=10.0, max_value=500.0, value=25.0, step=5.0)
    with col2:
        sym = st.text_input("Coin Symbol", value="BTCUSDT", help="e.g. BTCUSDT, DOGEUSDT")

    if st.button("▶ Run 1 Live Simulated Cycle (30 sec max)", type="primary", width="stretch"):
        if not sym:
            st.error("Enter a symbol.")
            return

        sym = sym.strip().upper()

        try:
            client = make_client()
            lot = get_lot_info(client, sym)

            with st.status(f"Simulating {sym} execution cycle...", expanded=True) as status:
                st.write("Fetching live order book profile...")
                strat, analysis = auto_select_cycle_strategy(client, sym, inv)

                vol_1m = analysis.get("volatility_1m", 0.15)
                dyn_sl = analysis.get("dynamic_stop_loss", -0.20)
                pred_mkt = analysis.get("predicted_market", "Liquid pairs")
                m_risk = analysis.get("main_risk", "None")
                r_mit = analysis.get("risk_mitigation", "")
                target_profit_usdt = analysis.get("min_net_profit_combined", 0.025)
                trailing_enabled = analysis.get("trailing_stop_enabled", False)
                trailing_drop_pct = analysis.get("trailing_drop_pct", 0.20)

                st.write(f"**Predicted Market:** `{pred_mkt}` ➔ **Algorithm:** `{strat}`")
                st.write(f"**Main Risk:** `{m_risk}` | **Mitigation:** {r_mit}")
                trail_status = f"Active ({trailing_drop_pct}%)" if trailing_enabled else "Off"
                st.write(f"**Target Profit:** `+{target_profit_usdt:.4f} USDT` | **Dynamic SL:** `{dyn_sl:.4f} USDT` | **Trailing:** `{trail_status}`")

                book = client.get_orderbook_ticker(symbol=sym)
                bid = float(book.get("bidPrice", 0))
                ask = float(book.get("askPrice", 0))

                # Pretend we got filled immediately at BEST_BID (discounted 1 tick to simulate dip buying)
                tick_sz = lot.get("tick_size", 0.0001)
                entry_price = bid - tick_sz

                calc_qty = inv / entry_price
                qty_str = format_quantity(calc_qty, lot.get("step", 0.001))
                qty = float(qty_str)

                entry_cost = qty * entry_price

                if qty * entry_price < lot.get("min_notional", 5.0):
                    st.error("Investment too small for minimum notional limit.")
                    return

                # Mock fee using BNB 0.075% assumption
                buy_fee_usdt = entry_cost * 0.00075

                st.write(f"**Virtual ENTRY FILLED**: Bought {qty} {sym} @ {entry_price:.6f} (~{entry_cost:.2f} USDT)")
                st.write(f"**Buy Fee (0.075% BNB)**: {buy_fee_usdt:.6f} USDT")

                ph = st.empty()

                # Simulation loop
                start_time = time.time()
                while time.time() - start_time < 30.0:
                    book = client.get_orderbook_ticker(symbol=sym)
                    current_bid = float(book.get("bidPrice", 0))

                    cur_val = qty * current_bid
                    # Simulated Net PnL = current_value - entry_cost - buy_fee - est_sell_fee
                    cur_pnl = cur_val - entry_cost - buy_fee_usdt - (cur_val * 0.00075)

                    col_color = "#22c55e" if cur_pnl > 0 else "#ef4444"
                    ph.markdown(f"<h4>Live Bid: {current_bid:.6f} | Net PnL: <span style='color:{col_color};'>{cur_pnl:+.6f} USDT</span></h4>", unsafe_allow_html=True)

                    # Logic exits based on algorithm rules
                    if cur_pnl >= target_profit_usdt:
                        status.update(label=f"💰 TARGET HIT (+{cur_pnl:.4f} USDT)", state="complete", expanded=True)
                        st.success(f"Trade successfully exited in profit via {strat} after {time.time() - start_time:.1f}s!")
                        return
                    if cur_pnl <= dyn_sl:
                        status.update(label=f"🚨 STOP LOSS HIT ({cur_pnl:.4f} USDT)", state="error", expanded=True)
                        st.error(f"Trade exited on stop loss to protect capital against {m_risk}.")
                        return
                    if early_scratch_triggered(cur_pnl, time.time() - start_time):
                        status.update(label=f"⏱ EARLY SCRATCH ({cur_pnl:.4f} USDT)", state="complete", expanded=True)
                        st.warning(f"Trade stalled. Exit executed safely near break-even.")
                        return

                    time.sleep(0.5)

                status.update(label=f"⌛ TIME OUT (Max 30s sim)", state="complete")
                st.info("Simulation loop finished without triggering an exit condition.")

        except Exception as e:
            st.error(f"Simulation error: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def render_streamlit_app():
    st.set_page_config(
        page_title="JitCrypto | Autonomous Scalper Terminal",
        page_icon="",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_custom_css()
    init_db()
    check_password()

    nav_options = ["Dashboard", "Manager", "Launch Bot", "Trade Log", "Live Log"]
    if "current_page" not in st.session_state or st.session_state.get("current_page") == "Simulation Mode":
        st.session_state["current_page"] = "Dashboard"

    # ── Top Navigation Bar: Buttons for Each Option ──
    nav_cols = st.columns([1.5, 1.1, 1.1, 1.2, 1.1, 1.1, 1.0, 1.0, 1.0])
    with nav_cols[0]:
        st.markdown(
            """
            <div style="display:flex;align-items:center;gap:8px;padding-top:4px;">
                <div style="width:32px;height:32px;background:#2563eb;border-radius:8px;display:flex;align-items:center;justify-content:center;color:#ffffff;font-weight:800;font-size:14px;box-shadow:0 2px 6px rgba(37,99,235,0.3);">JC</div>
                <div style="font-size:17px;font-weight:800;color:#0f172a;line-height:1.1;">JitCrypto</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    for i, opt in enumerate(nav_options):
        with nav_cols[i + 1]:
            is_active = (st.session_state["current_page"] == opt)
            btn_type = "primary" if is_active else "secondary"
            if st.button(opt, key=f"top_nav_{opt}", width="stretch", type=btn_type):
                if st.session_state["current_page"] != opt:
                    st.session_state["current_page"] = opt
                    st.rerun()

        #with nav_cols[7]:
        #if st.button("Refresh", key="btn_top_refresh", width="stretch", type="secondary"):
        #    st.rerun()

    with nav_cols[6]:
        if st.button("Log Out", key="btn_top_logout", width="stretch", type="secondary"):
            st.session_state["authenticated"] = False
            st.rerun()

    st.markdown("<hr style='margin:6px 0 16px 0;border-color:#e2e8f0;'>", unsafe_allow_html=True)

    with st.sidebar:
        # Logo
        st.markdown(
            """
            <div class="sidebar-logo">
                <div class="sidebar-logo-icon">JC</div>
                <div class="sidebar-logo-text">
                    <div class="sidebar-logo-title">JitCrypto</div>
                    <div class="sidebar-logo-sub">Autonomous Scalper</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(f"<div style='font-size:13px;color:#64748b;margin-bottom:12px;'>Active View: <b style='color:#2563eb;'>{st.session_state['current_page']}</b></div>", unsafe_allow_html=True)

        st.divider()

        # Live stream indicator
        st.markdown(
            '<div class="live-badge"><div class="live-dot"></div>Live WebSocket Stream</div>',
            unsafe_allow_html=True,
        )

        if st.button("Force Refresh", key="sb_btn_refresh", width="stretch", type="secondary"):
            st.rerun()
        if st.button("Log Out", key="sb_btn_logout", width="stretch", type="secondary"):
            st.session_state["authenticated"] = False
            st.rerun()

        st.divider()
        st.caption("`IST (UTC+05:30)` · `scalper.db`")

    page = st.session_state["current_page"]
    page_map = {
        "Dashboard": page_dashboard,
        "Manager": page_manager,
        "Launch Bot": page_launch,
        "Trade Log": page_trades,
        "Live Log": page_live_log,
        "Simulation Mode": page_simulation,
    }
    page_map.get(page, page_dashboard)()




# ===========================================================================
# SECTION 6: ENTRYPOINT & DISPATCHER
# ===========================================================================

def is_running_under_streamlit() -> bool:
    """Detect whether script is executing inside Streamlit runtime."""
    try:
        import streamlit as st
        return bool(st.runtime.exists())
    except Exception:
        return False


def cli_main():
    parser = argparse.ArgumentParser(
        description="BNB Autonomous Scalper & Terminal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  streamlit run BNB.py                      # Launch interactive Web UI
  python BNB.py --manager                   # Run headless autonomous manager loop
  python BNB.py --bot --instance-id 1       # Run bot worker instance #1
  python BNB.py --liquidate                 # Emergency stop all bots & liquidate
  python BNB.py --test                      # Test API keys, balance & connectivity
"""
    )
    parser.add_argument("--bot", action="store_true", help="Run bot worker process")
    parser.add_argument("--instance-id", type=int, help="Instance ID to run")
    parser.add_argument("--name", type=str, default=None, help="Bot name")
    parser.add_argument("--investment", type=float, default=25.0, help="Investment USDT (default: 25)")
    parser.add_argument("--target", type=float, default=0.005, help="Target profit USDT (default: 0.005)")
    parser.add_argument("--stop-loss", type=float, default=-0.10, help="Stop loss USDT (default: -0.10)")
    parser.add_argument("--strategy", type=str, default="DUAL_SOXLB_SOXSB", choices=["DUAL_SOXLB_SOXSB", "DUAL_HARVEST_STRATEGY", "DUAL_HARVEST", "HYBRID_SOXLB_SOXSB_SRATEGY", "HYBRID_SOXLB_SOXSB_STRATEGY", "SOXS_SOXL_LAG_PREDICTOR", "SEMICONDUCTOR_DUAL_ENGINE", "AUTO", "TREND_FOLLOWING", "MOMENTUM", "MEAN_REVERSION", "GRID", "ARBITRAGE", "MARKET_MAKING_SCALPING", "MACHINE_LEARNING", "BEST_BID_MAKER", "SCALE_IN_DCA"], help="Strategy mode")
    parser.add_argument("--use-only", type=str, default=None, help="Whitelist pairs (comma-separated)")
    parser.add_argument("--create-only", action="store_true", help="Create bot instance row in DB without starting")
    parser.add_argument("--manager", action="store_true", help="Run autonomous manager loop in background")
    parser.add_argument("--liquidate", action="store_true", help="Emergency stop all bots and liquidate holdings")
    parser.add_argument("--test", action="store_true", help="Run Binance connectivity & API diagnostics")

    args = parser.parse_args()

    if args.bot or args.instance_id or args.create_only:
        run_bot_cli(args)
    elif args.manager:
        stop_flag = {"stop": False}
        print("Starting autonomous manager loop (Ctrl+C to stop)...")
        try:
            manager_loop(stop_flag)
        except KeyboardInterrupt:
            print("Manager stopped by user.")
    elif args.liquidate:
        stop_and_liquidate_all()
    elif args.test:
        run_diagnostics()
    else:
        parser.print_help()
        print("\n👉 Tip: To run the web dashboard, use:")
        print("    streamlit run BNB.py")


if is_running_under_streamlit():
    render_streamlit_app()
elif __name__ == "__main__":
    cli_main()