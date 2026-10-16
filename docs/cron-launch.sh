#!/bin/sh
# Chairman copies this exact wrapper into ~/.hermes/scripts/ after independent acceptance.
cd '/Users/qqiang/AI project/05-日常工具/DailyBrief' || exit 1
exec python3 scripts/pipeline_job.py launch --cron
