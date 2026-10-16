#!/bin/sh
# Separate finite poll cron; never re-run prepare/collect/pipeline here.
cd '/Users/qqiang/AI project/05-日常工具/DailyBrief' || exit 1
exec python3 scripts/pipeline_job.py poll --cron
