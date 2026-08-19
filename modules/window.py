import os
import re
import sys
from datetime import datetime, timedelta, timezone

BRIEF_DIR = "/Users/qqiang/AI project/05-日常工具/DailyBrief"
TZ = timezone(timedelta(hours=8))

def log(msg):
    sys.stderr.write("[collect] %s\n" % msg)
    sys.stderr.flush()

def get_latest_brief_date():
    latest = None
    if os.path.isdir(BRIEF_DIR):
        for f in os.listdir(BRIEF_DIR):
            m = re.match(r"Daily-Brief-(\d{4}-\d{2}-\d{2})\.md$", f)
            if m and (latest is None or m.group(1) > latest):
                latest = m.group(1)
    return latest

def compute_window(now):
    now_date = now.date()
    latest = get_latest_brief_date()
    if latest:
        latest_date = datetime.strptime(latest, "%Y-%m-%d").date()
        gap = (now_date - latest_date).days
        if gap >= 2:
            start = latest_date + timedelta(days=1)
            log("断档 %d 天，从 %s 补采" % (gap - 1, start))
        else:
            start = now_date - timedelta(days=1)
    else:
        start = now_date - timedelta(days=1)
    hard = now - timedelta(hours=72)
    if start < hard.date():
        start = hard.date()
        log("窗口被 72h 硬上限收缩到 %s" % start)
    return start, now_date
