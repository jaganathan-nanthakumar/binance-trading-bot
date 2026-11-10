#!/usr/bin/env python3
"""
six_hour_dual_bot_scheduler.py
==============================
Automated 6-Hour Scheduler for DUAL_SOXLB_SOXSB Bot Instances.

Configuration:
- Investment per instance: $15.00 USDT (50% SOXLB / 50% SOXSB = $7.50 each, safely
  exceeding Binance Spot's MIN_NOTIONAL = 5.0 USDT).
- Interval: Once every 6 hours (21,600 seconds).
- Total instances: 4 instances (deploying $60.00 total across 24 hours).
- Total capital deployed: $60.00 USDT (4 × $15.00).
- Lifecycle: Spawns 1 instance immediately, then once every 6 hours, monitors live balances
  and countdowns in IST, and quits automatically after deploying the full $60.00 budget across 4 instances.

Usage:
  python six_hour_dual_bot_scheduler.py
  python six_hour_dual_bot_scheduler.py --dry-run
  python six_hour_dual_bot_scheduler.py --interval-seconds 60 --max-instances 2   # Fast test
"""

import os
import sys
import time
import argparse
import traceback
from datetime import datetime, timezone, timedelta

# Ensure UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Add current workspace directory to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import BNB

IST = timezone(timedelta(hours=5, minutes=30))


def get_ist_now_str() -> str:
    """Return formatted current timestamp in Indian Standard Time (IST)."""
    return datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST")


def get_account_status(client=None) -> dict:
    """Retrieve current USDT, BNB, and token balances from Binance."""
    try:
        if client is None:
            client = BNB.make_client()
        balances = client.get_account().get("balances", [])
        bal_map = {b["asset"]: float(b["free"]) for b in balances if b["asset"] in ("USDT", "BNB", "SOXLB", "SOXSB")}
        return {
            "usdt": bal_map.get("USDT", 0.0),
            "bnb": bal_map.get("BNB", 0.0),
            "soxlb": bal_map.get("SOXLB", 0.0),
            "soxsb": bal_map.get("SOXSB", 0.0),
            "status": "OK",
        }
    except Exception as e:
        return {"status": "ERROR", "error": str(e), "usdt": 0.0, "bnb": 0.0}


def create_and_spawn_dual_instance(
    index: int,
    total: int,
    investment: float,
    cum_invested: float,
    total_budget: float,
    target_profit: float,
    stop_loss: float,
    name_prefix: str,
    create_only: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Creates a single DUAL_SOXLB_SOXSB bot instance in the SQLite database
    and spawns its background worker process.
    """
    bot_name = f"{name_prefix}{index:02d}"
    strategy = "DUAL_SOXLB_SOXSB"
    now_ist = get_ist_now_str()

    leg_inv = investment / 2.0
    print(f"\n{'='*78}")
    print(f"[{now_ist}] 🚀 Launching Instance {index}/{total}: {bot_name}")
    print(f"{'='*78}")
    print(f"  • Strategy:            {strategy}")
    print(f"  • Instance Capital:    {investment:.2f} USDT (${leg_inv:.2f} SOXLB + ${leg_inv:.2f} SOXSB)")
    print(f"  • Cumulative Deployed: {cum_invested:.2f} / {total_budget:.2f} USDT ({(cum_invested / total_budget) * 100:.1f}%)")
    print(f"  • Profit Target:       +{target_profit:.4f} USDT per cycle (net of fees)")
    print(f"  • Stop Loss:           {stop_loss:.2f} USDT (Zero-loss accumulator)")

    if dry_run:
        print(f"  [DRY RUN] Would create instance '{bot_name}' and spawn background worker.")
        return {"id": 999000 + index, "name": bot_name, "pid": None, "status": "DRY_RUN"}

    try:
        BNB.init_db()

        # Create instance record in scalper.db
        iid = BNB.create_instance(
            name=bot_name,
            investment_usdt=float(investment),
            target_profit=float(target_profit),
            stop_loss=float(stop_loss),
            strategy=strategy,
        )

        # Set symbol claim tag to dual pair
        BNB.claim_symbol_for_instance(iid, "SOXLB+SOXSB")
        print(f"  ✅ Created database record: Instance #{iid} ('{bot_name}')")

        pid = None
        if not create_only:
            # Spawn background worker process
            proc = BNB.spawn_bot_process(iid)
            pid = proc.pid
            log_path = os.path.join(BNB.LOGS_DIR, f"{bot_name}_SOXLB+SOXSB.log")
            print(f"  ✅ Spawned background process (PID: {pid})")
            print(f"  📁 Worker log destination: {log_path}")
            BNB.log_event(
                f"[{bot_name}] 6-Hour Scheduler: Instance #{iid} launched with {investment:.2f} USDT ({cum_invested:.2f}/{total_budget:.2f} USDT total)",
                level="INFO",
                event_type="HOURLY_SCHEDULER_SPAWN",
                instance_id=iid,
                symbol="SOXLB+SOXSB",
            )
        else:
            print(f"  ℹ️ --create-only active: Process not started (registered in DB as CREATED)")

        return {"id": iid, "name": bot_name, "pid": pid, "status": "RUNNING" if pid else "CREATED"}

    except Exception as e:
        print(f"  ❌ Error creating instance #{bot_name}: {e}")
        traceback.print_exc()
        return {"id": None, "name": bot_name, "pid": None, "status": "FAILED", "error": str(e)}


def countdown_sleep(seconds: int, next_index: int, total_instances: int):
    """Sleeps with an in-place countdown display updated every 5-10 seconds."""
    end_time = time.time() + seconds
    next_ist = (datetime.now(IST) + timedelta(seconds=seconds)).strftime("%I:%M:%S %p IST")
    hours_wait = seconds / 3600.0
    print(f"\n⏳ Next instance ({next_index}/{total_instances}) scheduled in {hours_wait:.1f} hours at: {next_ist}")

    try:
        while True:
            remaining = int(end_time - time.time())
            if remaining <= 0:
                break
            hours, rem = divmod(remaining, 3600)
            minutes, secs = divmod(rem, 60)
            timer_str = f"{hours:02d}h {minutes:02d}m {secs:02d}s" if hours > 0 else f"{minutes:02d}m {secs:02d}s"
            sys.stdout.write(f"\r   ⏱️  Countdown to Instance #{next_index}: {timer_str} remaining... ")
            sys.stdout.flush()
            time.sleep(min(5, remaining))
        print("\r   ⏱️  Countdown complete! Spawning next instance now...                 ")
    except KeyboardInterrupt:
        raise


def parse_args():
    parser = argparse.ArgumentParser(
        description="6-Hour DUAL_SOXLB_SOXSB Scheduler ($15 per instance every 6 hours, up to $60 total = 4 instances)."
    )
    parser.add_argument(
        "--investment",
        type=float,
        default=15.0,
        help="Investment in USDT per instance (default: 15.0 USDT = $7.50 SOXLB + $7.50 SOXSB).",
    )
    parser.add_argument(
        "--max-instances",
        type=int,
        default=4,
        help="Total number of instances to create (default: 4 instances).",
    )
    parser.add_argument(
        "--total-budget",
        type=float,
        default=60.0,
        help="Total investment budget in USDT across all instances (default: 60.0 USDT).",
    )
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=6.0,
        help="Interval between instances in hours (default: 6.0 hours).",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=None,
        help="Alternative delay in seconds between instances (overrides --interval-hours).",
    )
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=None,
        help="Alternative shorthand for interval in minutes.",
    )
    parser.add_argument(
        "--target-profit",
        type=float,
        default=0.0050,
        help="Target net profit per cycle in USDT (default: 0.0050 USDT).",
    )
    parser.add_argument(
        "--stop-loss",
        type=float,
        default=-0.50,
        help="Stop loss parameter (default: -0.50).",
    )
    parser.add_argument(
        "--name-prefix",
        type=str,
        default="DUAL_6H_",
        help="Naming prefix for created instances (default: 'DUAL_6H_').",
    )
    parser.add_argument(
        "--create-only",
        action="store_true",
        help="Only insert instances into DB without spawning background worker processes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution without modifying the database or launching processes.",
    )
    parser.add_argument(
        "--wait-first",
        action="store_true",
        help="Wait 1 interval before spawning the first instance (default: spawn #1 immediately).",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Determine interval in seconds (default 6 hours = 21,600s)
    if args.interval_seconds is not None:
        interval_sec = int(args.interval_seconds)
    elif args.interval_minutes is not None:
        interval_sec = int(args.interval_minutes * 60)
    else:
        interval_sec = int(args.interval_hours * 3600)

    # Determine instances count based on budget if needed
    investment = float(args.investment)
    total_budget = float(args.total_budget)
    max_instances = int(args.max_instances)
    if total_budget and investment > 0:
        max_instances = min(max_instances, int(total_budget / investment))

    leg_inv = investment / 2.0

    print("\n" + "#" * 78)
    print("  JitCrypto Autonomous 6-Hour Scheduler: DUAL_SOXLB_SOXSB Strategy")
    print("#" * 78)
    print(f"  • Start Time (IST):      {get_ist_now_str()}")
    print(f"  • Total Instances:       {max_instances}")
    print(f"  • Investment / Bot:      {investment:.2f} USDT (${leg_inv:.2f} SOXLB + ${leg_inv:.2f} SOXSB)")
    print(f"  • Total Budget Cap:      {total_budget:.2f} USDT ({max_instances} × {investment:.2f} USDT)")
    print(f"  • Interval:              {interval_sec} seconds ({interval_sec / 3600:.2f} hours)")
    print(f"  • Target Profit:         +{args.target_profit:.4f} USDT per cycle (net of fees)")
    print(f"  • Stop Loss:             {args.stop_loss:.2f} USDT")
    print(f"  • Name Pattern:          {args.name_prefix}01 ... {args.name_prefix}{max_instances:02d}")
    print(f"  • Spawn Mode:            {'DB INSERT ONLY' if args.create_only else 'BACKGROUND WORKER PROCESS'}")
    print("-" * 78)

    # Check Binance account balance
    acct = get_account_status()
    if acct.get("status") == "OK":
        print(f"  💰 Binance Free Balance: {acct['usdt']:.2f} USDT | {acct['bnb']:.4f} BNB")
        print(f"     Held Tokens:          {acct['soxlb']:.3f} SOXLB | {acct['soxsb']:.2f} SOXSB")
        if acct['usdt'] < total_budget:
            print(f"  ⚠️ Note: Free USDT ({acct['usdt']:.2f}) is currently less than total budget ({total_budget:.2f} USDT).")
            print(f"     Sufficient for initial instances. You can top up USDT as cycles progress.")
    else:
        print(f"  ⚠️ Could not query live balances: {acct.get('error')}")
    print("#" * 78)

    created_instances = []
    cum_invested = 0.0

    try:
        for idx in range(1, max_instances + 1):
            # If wait_first is specified, wait before launching #1
            if idx == 1 and args.wait_first:
                print(f"\n[INFO] --wait-first set: Waiting {interval_sec / 3600:.1f} hours before spawning instance #1...")
                countdown_sleep(interval_sec, 1, max_instances)

            cum_invested += investment

            # Spawn instance
            res = create_and_spawn_dual_instance(
                index=idx,
                total=max_instances,
                investment=investment,
                cum_invested=cum_invested,
                total_budget=total_budget,
                target_profit=args.target_profit,
                stop_loss=args.stop_loss,
                name_prefix=args.name_prefix,
                create_only=args.create_only,
                dry_run=args.dry_run,
            )
            created_instances.append(res)

            # If not the last instance, sleep until next 6-hour interval
            if idx < max_instances:
                countdown_sleep(interval_sec, idx + 1, max_instances)

    except KeyboardInterrupt:
        print("\n\n" + "!" * 78)
        print("🛑 Scheduler stopped by user (Ctrl+C).")
        print("   Running background instances will continue executing independently.")
        print("!" * 78)

    print("\n" + "=" * 78)
    print("🏁 6-HOUR SCHEDULER EXECUTION COMPLETE: QUITTING")
    print("=" * 78)
    print(f"  • Total instances created:      {len(created_instances)} of {max_instances}")
    print(f"  • Total capital deployed:       {cum_invested:.2f} / {total_budget:.2f} USDT")
    print(f"  • Strategy deployed:            DUAL_SOXLB_SOXSB")
    print(f"  • Finish Time (IST):            {get_ist_now_str()}")
    print("-" * 78)
    print(f"{'Instance ID':<14} {'Bot Name':<16} {'PID':<10} {'Status':<12}")
    print("-" * 55)
    for inst in created_instances:
        iid_str = f"#{inst['id']}" if inst.get("id") else "N/A"
        pid_str = str(inst.get("pid") or "-")
        print(f"{iid_str:<14} {inst['name']:<16} {pid_str:<10} {inst['status']:<12}")
    print("=" * 78)
    print(f"👉 All {len(created_instances)} instances are running autonomously in the background.")
    print("👉 Monitor live trades, fills, and P/L anytime via the Streamlit Dashboard:")
    print("   streamlit run BNB.py")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    main()
