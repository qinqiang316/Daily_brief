# -*- coding: utf-8 -*-
"""点赞区幂等重建 + brief_record 幂等 upsert/跨日守卫。"""
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.helpers import ROOT, SCRIPTS  # noqa: F401
import brief_record
import like_links

BRIEF_V1 = """# Daily Brief 2026-10-08

## 参考资料
- [1] [文章甲](https://example.com/2026/10/08/a)
- [2] [文章乙](https://example.com/2026/10/08/b)
"""

BRIEF_V2 = """# Daily Brief 2026-10-08

## 参考资料
- [1] [文章甲](https://example.com/2026/10/08/a)
- [2] [文章丙](https://example.com/2026/10/08/c)
"""


class TestLikeLinksRebuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.brief = os.path.join(self.tmp.name, "Daily-Brief-2026-10-08.md")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text):
        with open(self.brief, "w", encoding="utf-8") as f:
            f.write(text)

    def _read(self):
        with open(self.brief, encoding="utf-8") as f:
            return f.read()

    def test_append_then_idempotent(self):
        self._write(BRIEF_V1)
        ok1, _ = like_links.add_like_section(self.brief)
        self.assertTrue(ok1)
        text1 = self._read()
        ok2, msg2 = like_links.add_like_section(self.brief)
        self.assertFalse(ok2)
        self.assertIn("已是最新", msg2)
        self.assertEqual(self._read(), text1)  # 幂等：重复执行内容不变
        self.assertEqual(text1.count(like_links.SECTION), 1)

    def test_rebuild_replaces_stale_links(self):
        """旧逻辑'存在即跳过'残留旧链接；现按当前参考资料重建。"""
        self._write(BRIEF_V1)
        like_links.add_like_section(self.brief)
        self.assertIn("example.com/2026/10/08/b", self._read())
        # 只改参考资料（乙 → 丙），旧点赞区原样留在文末 → 必须整体重建
        text = self._read().replace(
            "文章乙](https://example.com/2026/10/08/b)", "文章丙](https://example.com/2026/10/08/c)")
        self._write(text)
        ok, msg = like_links.add_like_section(self.brief)
        self.assertTrue(ok)
        text = self._read()
        self.assertEqual(text.count(like_links.SECTION), 1)
        # 点赞区内旧链接不残留（参考资料区的 URL 已是丙，乙只应出现在旧点赞区 → 被清掉）
        like_sec = text.split(like_links.SECTION, 1)[1]
        self.assertIn("08%2Fc", like_sec)      # 新链接（点赞 URL 为 percent-encoded）
        self.assertNotIn("08%2Fb", like_sec)   # 旧链接不残留
        self.assertIn("重建", msg)


class TestBriefRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.records = os.path.join(self.tmp.name, "brief_records.json")
        self.patch = mock.patch.object(brief_record, "RECORDS_FILE", self.records)
        self.patch.start()
        self.brief = os.path.join(self.tmp.name, "Daily-Brief-2026-10-08.md")
        self.cand = os.path.join(self.tmp.name, "Daily-Brief-2026-10-08-candidates.json")
        with open(self.brief, "w", encoding="utf-8") as f:
            f.write("<!-- run_id: r1 -->\n" + BRIEF_V1)
        with open(self.cand, "w", encoding="utf-8") as f:
            json.dump({
                "run_id": "r1", "generated_at": "2026-10-08T01:00:00+08:00",
                "window": {"start": "2026-10-07", "end": "2026-10-08"},
                "preference": {},
                "candidates": [
                    {"title": "文章甲", "url": "https://example.com/2026/10/08/a",
                     "direction": "科技", "word_count": 800, "content": "正文"},
                    {"title": "文章乙", "url": "https://example.com/2026/10/08/b",
                     "direction": "商业", "word_count": 700, "content": "正文"},
                ],
                "source_leftovers": [],
            }, f, ensure_ascii=False)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_record_idempotent_upsert(self):
        """重复记录同日简报 → 替换而非追加，总数恒为 1。"""
        r1 = brief_record.record_brief(self.brief, self.cand)
        self.assertIsNotNone(r1)
        r2 = brief_record.record_brief(self.brief, self.cand)
        self.assertIsNotNone(r2)
        records = brief_record.load_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["date"], "2026-10-08")
        self.assertEqual(len(records[0]["articles"]), 2)

    def test_cross_day_mismatch_refused(self):
        other = os.path.join(self.tmp.name, "Daily-Brief-2026-10-07-candidates.json")
        os.rename(self.cand, other)
        self.assertIsNone(brief_record.record_brief(self.brief, other))
        self.assertEqual(brief_record.load_records(), [])

    def test_run_mismatch_refused(self):
        with open(self.brief, "w", encoding="utf-8") as f:
            f.write("<!-- run_id: wrong -->\n" + BRIEF_V1)
        self.assertIsNone(brief_record.record_brief(self.brief, self.cand))
        self.assertEqual(brief_record.load_records(), [])


if __name__ == "__main__":
    unittest.main()
