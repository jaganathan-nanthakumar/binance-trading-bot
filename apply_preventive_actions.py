import re

with open("BNB.py", "rb") as f:
    raw = f.read()

text = raw.decode("utf-8")
is_crlf = "\r\n" in text
norm_text = text.replace("\r\n", "\n")

# 1. Update get_active_symbols and claim_symbol_for_instance to enforce symbol mutual exclusion
old_claim_block = '''def get_active_symbols(exclude_instance_id: Optional[int] = None) -> set:
    """
    Returns an empty set. User requested allowing multiple bots to trade the same pair
    simultaneously since the whitelist is limited to highly volatile pairs.
    """
    return set()


def claim_symbol_for_instance(instance_id: int, symbol: str) -> bool:
    """
    Atomically checks if `symbol` is currently active/traded/held by any other bot
    (status in 'CREATED', 'RUNNING', 'STOPPING' or open cycle in 'cycles').
    If free, updates bot_instances.current_symbol = symbol for instance_id and returns True.
    If already taken, returns False without updating.
    """
    if not symbol or symbol == "INIT":
        return False
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Conflict check removed: User requested allowing multiple bots to trade the same pair

        conn.execute(
            "UPDATE bot_instances SET current_symbol = ? WHERE id = ?",
            (symbol, instance_id),
        )
        return True'''

new_claim_block = '''def get_active_symbols(exclude_instance_id: Optional[int] = None) -> set:
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
    """
    if not symbol or symbol == "INIT":
        return False
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
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
        return True'''

assert old_claim_block in norm_text, "old_claim_block not found!"
norm_text = norm_text.replace(old_claim_block, new_claim_block)

# 2. Update DCA dip spacing in auto_select_cycle_strategy and defaults
old_dip_calc = 'analysis["dynamic_dip_pct"] = max(0.0010, min(0.0035, (vol_1m / 100.0) * 0.45))'
new_dip_calc = 'analysis["dynamic_dip_pct"] = max(0.0080, min(0.0180, (vol_1m / 100.0) * 2.0))'
assert old_dip_calc in norm_text, "old_dip_calc not found!"
norm_text = norm_text.replace(old_dip_calc, new_dip_calc)

old_dip_default = '"dynamic_dip_pct": 0.0015,'
new_dip_default = '"dynamic_dip_pct": 0.0080,'
assert old_dip_default in norm_text, "old_dip_default not found!"
norm_text = norm_text.replace(old_dip_default, new_dip_default, 1)

old_dip_step3 = 'dip_frac = profile.get("dynamic_dip_pct", 0.0015)'
new_dip_step3 = 'dip_frac = profile.get("dynamic_dip_pct", 0.0080)'
assert old_dip_step3 in norm_text, "old_dip_step3 not found!"
norm_text = norm_text.replace(old_dip_step3, new_dip_step3)

# 3. Update effective_max_parallel in manager_tick to reserve 30% wallet USDT buffer
old_parallel = '''    # Dynamic parallel limit calculated automatically based on available account balance
    effective_max_parallel = int(total_usdt_balance // investment) if investment > 0 else 0'''

new_parallel = '''    # Dynamic parallel limit with 30% wallet cash reserve safety buffer
    # Ensures wallet is never 100% frozen in trades simultaneously
    effective_max_parallel = max(1, int((total_usdt_balance * 0.70) // investment)) if investment > 0 else 0'''

assert old_parallel in norm_text, "old_parallel not found!"
norm_text = norm_text.replace(old_parallel, new_parallel)

# 4. Enhance is_market_favorable with 1-hour BTC trend check
old_regime = '''    try:
        klines = client.get_klines(symbol="BTCUSDT", interval="5m", limit=3)
        if not klines or len(klines) < 2:
            return True, "BTC data unavailable — proceeding"
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
        return True, f"BTC regime OK (5m avg={avg_move:+.2f}%)"'''

new_regime = '''    try:
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
        return True, f"BTC regime OK (5m avg={avg_move:+.2f}%)"'''

assert old_regime in norm_text, "old_regime not found!"
norm_text = norm_text.replace(old_regime, new_regime)

# 5. Add Order-Flow Micro-Scratch Safety Valve after 10 minutes in trade_one_cycle_scale_in
old_guard = '''                    # Time-based 0.00X recovery stop-loss & safety ceiling guard
                    elapsed = time.time() - start_ts
                    if hard_stop_triggered(cur_pnl, investment_usdt, stop_loss, elapsed):'''

new_guard = '''                    # Order-Flow Micro-Scratch Safety Valve (Engages after 600s if order flow turns toxic)
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

                    if should_micro_scratch or hard_stop_triggered(cur_pnl, investment_usdt, stop_loss, elapsed):'''

assert old_guard in norm_text, "old_guard not found!"
norm_text = norm_text.replace(old_guard, new_guard)

out_text = norm_text.replace("\n", "\r\n") if is_crlf else norm_text

with open("BNB.py", "wb") as f:
    f.write(out_text.encode("utf-8"))

print("Successfully applied all 5 Preventive Actions to BNB.py!")
