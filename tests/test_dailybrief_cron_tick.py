import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from modules.window import TZ
from scripts.dailybrief_cron_tick import run_tick


class DailyBriefCronTickTests(TestCase):
    def setUp(self):
        self.jobs = Path(tempfile.mkdtemp())
        self.now = datetime(2026, 10, 16, 8, 30, tzinfo=TZ)
        self.completed = Mock(return_value=None)
        self.poll = Mock(return_value=[])
        self.launch = Mock(return_value=str(self.jobs / "20261016T083000-test" / "ticket.json"))
        self.load = Mock(return_value={"status": "queued"})

    def run_tick(self, now=None):
        return run_tick(
            now=now or self.now,
            jobs=self.jobs,
            completed_fn=self.completed,
            poll_fn=self.poll,
            launch_fn=self.launch,
            load_fn=self.load,
        )

    def test_published_report_short_circuits(self):
        self.completed.return_value = "/published/report.html"
        result = self.run_tick()
        self.assertFalse(result["wakeAgent"])
        self.poll.assert_not_called()
        self.launch.assert_not_called()

    def test_before_start_time_does_not_launch(self):
        result = self.run_tick(datetime(2026, 10, 16, 8, 29, tzinfo=TZ))
        self.assertEqual(result["reason"], "before_daily_start")
        self.launch.assert_not_called()

    def test_launches_once_after_start_and_keeps_agent_asleep(self):
        result = self.run_tick()
        self.launch.assert_called_once_with(self.jobs)
        self.assertFalse(result["wakeAgent"])
        self.assertEqual(result["status"], "queued")

    def test_ready_or_failed_ticket_wakes_agent_without_new_launch(self):
        row = {"run_id": "r1", "status": "ready", "ticket": "/jobs/r1/ticket.json"}
        self.poll.return_value = [row]
        result = self.run_tick()
        self.assertEqual(result["tickets"], [row])
        self.launch.assert_not_called()

    def test_acknowledged_failed_attempt_does_not_relaunch(self):
        ticket = self.jobs / "20261016T083000-test" / "ticket.json"
        ticket.parent.mkdir()
        ticket.write_text(json.dumps({"status": "acknowledged"}))
        self.load = Mock(side_effect=lambda p: json.loads(Path(p).read_text()))
        result = self.run_tick()
        self.assertEqual(result["reason"], "today_attempt_acknowledged_without_publication")
        self.launch.assert_not_called()
