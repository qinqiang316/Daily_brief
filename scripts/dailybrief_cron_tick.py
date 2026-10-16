#!/usr/bin/env python3
"""One cron tick: launch today's pipeline once, then surface only completed work."""
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(PROJECT_ROOT), str(PROJECT_ROOT / "scripts")]

import pipeline_job  # noqa: E402
from modules.window import TZ  # noqa: E402

START_HOUR = 8
START_MINUTE = 30


def run_tick(now=None, jobs=None, *, completed_fn=None, poll_fn=None,
             launch_fn=None, load_fn=None):
    """Return one deterministic cron action; waiting ticks never wake the model."""
    jobs = Path(jobs or pipeline_job.DEFAULT_JOBS)
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    else:
        now = now.astimezone(TZ)
    completed_fn = completed_fn or pipeline_job.completed_today
    poll_fn = poll_fn or pipeline_job.poll
    launch_fn = launch_fn or pipeline_job.launch
    load_fn = load_fn or pipeline_job.load

    if completed_fn():
        return {"wakeAgent": False, "reason": "today_already_published"}

    rows = poll_fn(jobs)
    if rows:
        return {"tickets": rows}

    day_prefix = now.strftime("%Y%m%d")
    for ticket in sorted(jobs.glob(day_prefix + "T*/ticket.json")):
        state = load_fn(ticket)
        if state.get("status") == "acknowledged":
            return {
                "wakeAgent": False,
                "reason": "today_attempt_acknowledged_without_publication",
                "ticket": str(ticket),
            }

    if (now.hour, now.minute) < (START_HOUR, START_MINUTE):
        return {"wakeAgent": False, "reason": "before_daily_start"}

    ticket = Path(launch_fn(jobs)).resolve()
    state = load_fn(ticket)
    if state.get("status") in ("ready", "failed"):
        return {"tickets": [dict(state, ticket=str(ticket))]}
    return {
        "wakeAgent": False,
        "ticket": str(ticket),
        "status": state.get("status"),
    }


def main():
    try:
        result = run_tick()
    except Exception as exc:  # surfaced to the scheduled agent as a real failure
        print(json.dumps({"status": "script_error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
