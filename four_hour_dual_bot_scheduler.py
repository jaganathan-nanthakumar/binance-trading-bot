#!/usr/bin/env python3
"""
four_hour_dual_bot_scheduler.py
===============================
Entrypoint for the 4-Hour DUAL_SOXLB_SOXSB Scheduler.
Spawns 1 instance ($10 USDT) every 4 hours up to 6 instances ($60 total), then quits.
"""
from hourly_dual_bot_scheduler import main

if __name__ == "__main__":
    main()
