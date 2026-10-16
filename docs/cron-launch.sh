#!/bin/sh
# Combined DailyBrief cron wrapper: one 5-minute task for start and polling.
cd '/Users/qqiang/AI project/05-日常工具/DailyBrief' || exit 1
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR='/Users/qqiang/.hermes/cache/scratch'
exec '/Users/qqiang/.hermes/hermes-agent/venv/bin/python' scripts/dailybrief_cron_tick.py
