# -*- coding: utf-8 -*-
"""离线回放：2026-10-07 旧池（HN stub 池）必须被明确拒绝，不编造。"""
import os
import unittest

from tests.helpers import ROOT  # noqa: F401
import replay_candidates


class TestReplay(unittest.TestCase):
    def test_2026_10_07_old_pool_refused(self):
        path = os.path.join(ROOT, "_candidates",
                            "Daily-Brief-2026-10-07-candidates.json")
        if not os.path.isfile(path):
            self.skipTest("无 2026-10-07 候选文件")
        before = os.path.getmtime(path)
        rc = replay_candidates.replay(path)
        self.assertEqual(rc, 1)  # 旧池正文/日期证据不足 → 明确拒绝
        self.assertEqual(os.path.getmtime(path), before)  # 只读，不改历史

    def test_qualified_pool_passes(self):
        import json
        import tempfile
        payload = {
            "run_id": "r1",
            "generated_at": "2026-10-08T01:00:00+08:00",
            "window": {"start": "2026-10-07", "end": "2026-10-08"},
            "preference": {},
            "candidates": [{
                "title": "合格", "url": "https://example.com/2026/10/08/a",
                "publish_date": "2026-10-08", "date_verified": True,
                "date_precision": "day", "window_outside_days": 0,
                "word_count": 800,
            }],
            "source_leftovers": [],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump(payload, f)
            path = f.name
        try:
            self.assertEqual(replay_candidates.replay(path), 0)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
