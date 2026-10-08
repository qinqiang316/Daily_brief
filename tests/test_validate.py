# -*- coding: utf-8 -*-
"""校验闸门：跨日错配、未知日期速览、窗口/正文证据、run_id 配对。"""
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.helpers import ROOT, SCRIPTS, long_body  # noqa: F401
import validate_brief

GOOD_URL = "https://example.com/2026/10/08/good"
LO_URL = "https://leftover.example.com/2026/10/08/lo"


def good_candidate(**kw):
    c = {
        "title": "好文章", "url": GOOD_URL, "domain": "example.com",
        "direction": "科技", "is_preferred": False, "is_explore": False,
        "publish_date": "2026-10-08", "date_verified": True,
        "date_source": "url", "date_evidence": GOOD_URL,
        "date_precision": "day", "window_outside_days": 0,
        "hn_points": None, "word_count": 800,
        "watermark_suspect": False, "watermark_reasons": [],
        "content": long_body(), "source_key": "cn:0", "source_label": "查询",
    }
    c.update(kw)
    return c


def cand_payload(**kw):
    p = {
        "run_id": "20261008T010000-abcd1234",
        "generated_at": "2026-10-08T01:00:00+08:00",
        "window": {"start": "2026-10-07", "end": "2026-10-08"},
        "preference": {"pref_dir": None, "explore_dir": "生活", "likes_count": 0},
        "candidates": [good_candidate()],
        "source_leftovers": [],
    }
    p.update(kw)
    return p


BRIEF_TMPL = """# Daily Brief 2026-10-08
<!-- run_id: {run_id} -->
## 今日热门文章
正文 {url} 好内容。

## 参考资料
- [1] [好文章]({url})
"""


class ValidateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = os.path.join(self.tmp.name, "output")
        self.cand_dir = os.path.join(self.tmp.name, "_candidates")
        os.makedirs(self.out_dir)
        os.makedirs(self.cand_dir)
        self.patches = [
            mock.patch.object(validate_brief, "OUTPUT_DIR", self.out_dir),
            mock.patch.object(validate_brief, "CAND_DIR", self.cand_dir),
            mock.patch.object(validate_brief.brief_record, "record_brief",
                              return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write_brief(self, date_s="2026-10-08", run_id="20261008T010000-abcd1234",
                    url=GOOD_URL, extra=""):
        path = os.path.join(self.out_dir, "Daily-Brief-%s.md" % date_s)
        with open(path, "w", encoding="utf-8") as f:
            f.write(BRIEF_TMPL.format(run_id=run_id, url=url) + extra)
        return path

    def write_cand(self, payload, date_s="2026-10-08"):
        path = os.path.join(self.cand_dir,
                            "Daily-Brief-%s-candidates.json" % date_s)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def run_validate(self, argv):
        with mock.patch.object(validate_brief.sys, "argv", ["validate_brief.py"] + argv):
            return validate_brief.main()


class TestValidatePairing(ValidateCase):
    def test_pass_when_paired(self):
        brief = self.write_brief()
        cand = self.write_cand(cand_payload())
        self.assertEqual(self.run_validate([brief, cand]), 0)

    def test_cross_day_mismatch_explicit_args(self):
        """跨日错配：显式传入的简报与候选日期不同 → FAIL。"""
        brief = self.write_brief("2026-10-08")
        cand = self.write_cand(cand_payload(), "2026-10-07")
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_missing_same_day_candidate(self):
        """缺省配对：没有同日期候选 JSON → FAIL（不得退而取最新候选）。"""
        brief = self.write_brief("2026-10-08")
        self.write_cand(cand_payload(), "2026-10-07")  # 只有别的日期
        self.assertEqual(self.run_validate([brief]), 1)

    def test_run_id_mismatch(self):
        brief = self.write_brief(run_id="别的run")
        cand = self.write_cand(cand_payload())
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_run_id_absent_in_brief_rejected(self):
        """候选带 run_id 时简报必须标注（新池强制，不只是错配拒绝）。"""
        brief = self.write_brief()
        with open(brief, encoding="utf-8") as f:
            text = f.read()
        with open(brief, "w", encoding="utf-8") as f:
            f.write("\n".join(l for l in text.splitlines() if "run_id" not in l))
        cand = self.write_cand(cand_payload())
        self.assertEqual(self.run_validate([brief, cand]), 1)


class TestValidateEvidence(ValidateCase):
    def test_window_outside_candidate_rejected(self):
        """候选日期在窗口外/带窗口外标记 → 不得进简报。"""
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(
            candidates=[good_candidate(publish_date="2026-10-05")]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_window_outside_flag_rejected(self):
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(
            candidates=[good_candidate(window_outside_days=2)]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_thin_body_candidate_rejected(self):
        """候选正文证据不足（word_count < 500）→ FAIL。"""
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(
            candidates=[good_candidate(word_count=16)]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_month_precision_candidate_rejected(self):
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(
            candidates=[good_candidate(date_precision="month")]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_word_count_metadata_lies_rejected(self):
        """word_count 元数据虚标（800）但真实 content 实测不足 → FAIL。"""
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(
            candidates=[good_candidate(word_count=800, content="x")]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_missing_date_source_rejected(self):
        """旧格式只有 date_verified 布尔位、没有 date_source → 不能冒充验证 → FAIL。"""
        brief = self.write_brief()
        c = good_candidate()
        del c["date_source"]
        cand = self.write_cand(cand_payload(candidates=[c]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_missing_date_precision_rejected(self):
        """旧格式没有 date_precision 字段 → 不能默认当日精度放行 → FAIL。"""
        brief = self.write_brief()
        c = good_candidate()
        del c["date_precision"]
        cand = self.write_cand(cand_payload(candidates=[c]))
        self.assertEqual(self.run_validate([brief, cand]), 1)


def good_leftover(**kw):
    lo = {
        "source": "某源", "source_key": "cn:1", "direction": "生活",
        "title": "标题", "url": LO_URL, "domain": "leftover.example.com",
        "publish_date": "2026-10-08", "date_verified": True,
        "date_source": "url", "date_evidence": LO_URL, "date_precision": "day",
    }
    lo.update(kw)
    return lo


class TestValidateLeftover(ValidateCase):
    def test_unverified_leftover_in_brief_rejected(self):
        """未知日期速览禁止交付：未验证 leftover 出现在简报 → FAIL。"""
        extra = "\n## 未推荐来源速览\n- [某源] 标题（日期未验证） https://%s\n" % LO_URL.split("://", 1)[1]
        brief = self.write_brief(extra=extra)
        cand = self.write_cand(cand_payload(source_leftovers=[good_leftover(
            publish_date="", date_verified=False, date_source=None,
            date_evidence=None, date_precision=None)]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_unverified_leftover_in_payload_rejected(self):
        """候选产物本身携带未验证 leftover（即使简报未引用）→ FAIL。"""
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(source_leftovers=[good_leftover(
            publish_date="", date_verified=False, date_source=None,
            date_evidence=None, date_precision=None)]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_verified_leftover_ok(self):
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(source_leftovers=[good_leftover()]))
        self.assertEqual(self.run_validate([brief, cand]), 0)

    def test_month_precision_leftover_rejected(self):
        """月精度速览（即使有日期）→ FAIL。"""
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(source_leftovers=[good_leftover(
            publish_date="2026-10-01", date_precision="month")]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_leftover_missing_date_source_rejected(self):
        """速览只有 date_verified 布尔位、缺 date_source/date_evidence → FAIL。"""
        brief = self.write_brief()
        lo = good_leftover()
        del lo["date_source"]
        cand = self.write_cand(cand_payload(source_leftovers=[lo]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_old_day_leftover_rejected(self):
        """旧日速览旁路：日期早于窗口起点 → FAIL。"""
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(source_leftovers=[good_leftover(
            publish_date="2026-09-20")]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_future_leftover_rejected(self):
        """速览日期晚于窗口终点 → FAIL。"""
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(source_leftovers=[good_leftover(
            publish_date="2026-10-09")]))
        self.assertEqual(self.run_validate([brief, cand]), 1)


if __name__ == "__main__":
    unittest.main()
