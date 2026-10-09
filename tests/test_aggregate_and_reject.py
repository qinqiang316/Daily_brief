# -*- coding: utf-8 -*-
"""聚合页拦截 + 拒收协议回归：
- 聚合页（首页/频道/列表/导航）不得入有效池与速览，独立文章与带独立日期的 ACS digest 不误杀；
- 拒收探索 + 有界补抓穷尽记录 + 简报「探索内容缺货」→ 可缩减交付（PASS）；
- 合格探索未收录 / 拒收 URL 出现在任意区域 / 缺补抓记录 / 缺缺货说明 → FAIL；
- 日期/正文/去重硬条件不因补抓记录豁免，仍 FAIL；
- reject_candidate CLI：移除命中条目、审计字段齐全、幂等、未命中报错。
全部用例隔离在临时目录，不联网、不写生产元数据。
"""
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

from tests.helpers import long_body, make_candidate  # noqa: F401
from tests.test_validate import (ValidateCase, good_candidate, cand_payload,  # noqa: F401
                                 good_leftover, GOOD_URL)
from modules import filter as filter_mod
from modules import rank
import collect_brief
import reject_candidate
import validate_brief

WINDOW_START = date(2026, 10, 6)
TODAY = date(2026, 10, 8)

FAMILYDOCTOR_NEWS = "https://familydoctor.cn/news"
TECHCRUNCH_CATEGORY = "https://techcrunch.com/category/artificial-intelligence"
ACS_DIGEST = "https://agentcaseshare.cn/news/ai-daily-2026-10-08"
EXP_URL = "https://explore.example.com/2026/10/08/life-piece"


def run_filter(candidates, texts=None):
    with mock.patch.object(rank, "fetch_texts_parallel",
                           return_value=texts or {}):
        return rank.filter_candidates(candidates, set(), WINDOW_START, TODAY, None)


class TestAggregateURL(unittest.TestCase):
    """is_aggregate_url 判定单元测试。"""

    def test_nav_pages_are_aggregate(self):
        for u in (FAMILYDOCTOR_NEWS,
                  TECHCRUNCH_CATEGORY,
                  "https://example.com/",
                  "https://example.com",
                  "https://example.com/news",
                  "https://example.com/channel/tech",
                  "https://example.com/page/2",
                  "https://example.com/list/tech?page=3",
                  "https://example.com/topics/ai"):
            self.assertTrue(filter_mod.is_aggregate_url(u), u)

    def test_independent_articles_not_killed(self):
        for u in ("https://techcrunch.com/2026/10/08/openai-launches-model",
                  "https://www.theguardian.com/technology/2026/oct/08/some-story",
                  "https://36kr.com/p/1234567890123",
                  "https://example.com/articles/98765432",
                  "https://example.com/story/abcd1234ef",
                  "https://example.com/post/some-piece.html",
                  "https://telegra.ph/some-long-article-slug",
                  ACS_DIGEST):
            self.assertFalse(filter_mod.is_aggregate_url(u), u)

    def test_single_segment_slugs_are_not_blanket_rejected(self):
        for path in ("a-real-story", "some-piece.html", "98765432",
                     "ai-daily-2026-10-08"):
            with self.subTest(path=path):
                self.assertFalse(filter_mod.is_aggregate_url(
                    "https://ordinary.example.com/" + path))

    def test_directory_routes_cannot_be_exempted_by_dates_or_ids(self):
        for path in ("category/2026/10/08/story", "category/2026-10-08",
                     "category/123456789.html", "news/page/2",
                     "2026/10/08/page/2", "page/2026-10-08-story",
                     "category/ai-daily-2026-10-08", "p/2"):
            with self.subTest(path=path):
                self.assertTrue(filter_mod.is_aggregate_url(
                    "https://example.com/" + path))

    def test_date_archives_are_not_independent_digest_slugs(self):
        for path in ("2026", "2026/10", "2026/10/08", "2026/oct/08",
                     "news/2026-10-08", "20261008.html"):
            with self.subTest(path=path):
                self.assertTrue(filter_mod.is_aggregate_url(
                    "https://example.com/" + path))

    def test_pagination_query_with_blank_value_is_still_directory(self):
        for query in ("page=", "paged", "q=ai&page=2"):
            with self.subTest(query=query):
                self.assertTrue(filter_mod.is_aggregate_url(
                    "https://example.com/news/story?" + query))

    def test_acs_slug_date_is_url_day_evidence(self):
        """ACS digest slug 的明确独立日期 → URL 日精度证据。"""
        ev = filter_mod.extract_date_evidence(
            {"url": ACS_DIGEST, "title": "AI 日报", "content": "x"})
        self.assertEqual(ev["publish_date"], "2026-10-08")
        self.assertEqual(ev["date_source"], "url")
        self.assertEqual(ev["precision"], "day")


class TestAggregatePoolAdmission(unittest.TestCase):
    """聚合页不得入有效池（即使正文够长、日期证据齐全）。"""

    def test_familydoctor_news_blocked(self):
        body = "发布时间：2026-10-08 " + long_body()
        kept = run_filter([make_candidate(FAMILYDOCTOR_NEWS)],
                          texts={FAMILYDOCTOR_NEWS: body})
        self.assertEqual(kept, [])

    def test_techcrunch_category_blocked(self):
        body = "发布时间：2026-10-08 " + long_body()
        kept = run_filter([make_candidate(TECHCRUNCH_CATEGORY)],
                          texts={TECHCRUNCH_CATEGORY: body})
        self.assertEqual(kept, [])

    def test_real_article_kept(self):
        url = "https://techcrunch.com/2026/10/08/some-ai-article"
        kept = run_filter([make_candidate(url)], texts={url: long_body()})
        self.assertEqual(len(kept), 1)

    def test_single_segment_article_kept_with_real_date_evidence(self):
        url = "https://ordinary.example.com/a-real-story"
        body = "发布时间：2026-10-08 " + long_body()
        kept = run_filter([make_candidate(url)], texts={url: body})
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["url"], url)

    def test_dated_directories_blocked_before_body_fetch(self):
        for path in ("category/2026/10/08/story", "2026/10/08/page/2"):
            url = "https://example.com/" + path
            with self.subTest(url=url), mock.patch.object(rank, "fetch_texts_parallel") as fetch:
                self.assertEqual(rank.filter_candidates(
                    [make_candidate(url)], set(), WINDOW_START, TODAY, None), [])
                fetch.assert_not_called()

    def test_acs_digest_kept(self):
        kept = run_filter([make_candidate(ACS_DIGEST, title="AI 日报 2026-10-08")],
                          texts={ACS_DIGEST: long_body()})
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["publish_date"], "2026-10-08")


class TestAggregateLeftover(unittest.TestCase):
    """速览同样拦截聚合页，不误杀带独立日期的 ACS digest。"""

    def pick(self, items):
        return collect_brief.pick_leftover_item(
            items, set(), set(), datetime(2026, 10, 7), datetime(2026, 10, 8))

    def test_aggregate_leftover_rejected_despite_date_evidence(self):
        """聚合页即使有署名日期证据也不得进速览。"""
        it = {"title": "健康频道", "url": FAMILYDOCTOR_NEWS,
              "content": "发布时间：2026-10-08 健康新闻列表。"}
        self.assertIsNone(self.pick([it]))
        it2 = {"title": "AI 频道", "url": TECHCRUNCH_CATEGORY,
               "content": "发布时间：2026-10-08 AI news."}
        self.assertIsNone(self.pick([it2]))

    def test_single_segment_article_leftover_with_date_evidence(self):
        item = {"title": "独立文章", "url": "https://ordinary.example.com/a-real-story",
                "content": "发布时间：2026-10-08 独立文章正文。"}
        picked = self.pick([item])
        self.assertIsNotNone(picked)
        self.assertEqual(picked["url"], item["url"])

    def test_dated_directory_and_numeric_page_leftovers_blocked(self):
        for path in ("category/2026/10/08/story", "2026/10/08/page/2"):
            with self.subTest(path=path):
                self.assertIsNone(self.pick([{
                    "title": "目录", "url": "https://example.com/" + path,
                    "content": "发布时间：2026-10-08 目录内容。"}]))

    def test_acs_digest_leftover_picked(self):
        it = {"title": "AI 日报", "url": ACS_DIGEST, "content": ""}
        picked = self.pick([it])
        self.assertIsNotNone(picked)
        self.assertEqual(picked["publish_date"], "2026-10-08")
        self.assertEqual(picked["date_source"], "url")


def rejected_entry(url=EXP_URL, is_explore=True, **kw):
    r = {"url": url, "is_explore": is_explore, "reason": "聚合页，非独立文章",
         "reviewed_by": "小g", "reviewed_at": "2026-10-08T12:00:00+08:00",
         "entry": {"url": url, "is_explore": is_explore, "title": "探索条目"}}
    r.update(kw)
    return r


REPLENISHMENT = {"status": "exhausted", "attempts": 2,
                 "reason": "两轮补抓仍未获得合格独立探索文章"}
SHORTAGE_NOTE = "\n> 探索内容缺货：本期探索候选经审核拒收，补抓已穷尽。\n"


class TestRejectProtocol(ValidateCase):
    """拒收协议校验：缩减交付三要件（拒收记录 + 补抓穷尽 + 缺货说明）。"""

    def test_rejected_explore_with_replenishment_and_note_passes(self):
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        cand = self.write_cand(cand_payload(
            rejected_candidates=[rejected_entry()],
            exploration_replenishment=dict(REPLENISHMENT)))
        self.assertEqual(self.run_validate([brief, cand]), 0)

    def test_qualified_explore_missing_fails(self):
        """合格探索候选未被简报收录 → FAIL。"""
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(
            candidates=[good_candidate(),
                        good_candidate(url=EXP_URL, is_explore=True)]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_rejected_url_anywhere_fails(self):
        """拒收 URL 出现在简报（参考资料/速览任何区域）→ FAIL。"""
        extra = "\n## 未推荐来源速览\n- [某源] 被拒条目 %s\n" % EXP_URL
        brief = self.write_brief(extra=extra)
        cand = self.write_cand(cand_payload(
            rejected_candidates=[rejected_entry(is_explore=False)]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_missing_replenishment_fails(self):
        """有探索拒收但无合格探索，缺 exploration_replenishment 记录 → FAIL（有缺货说明也不行）。"""
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        cand = self.write_cand(cand_payload(
            rejected_candidates=[rejected_entry()]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_bad_replenishment_fails(self):
        """补抓记录 status/attempts 不合规 → FAIL。"""
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        cand = self.write_cand(cand_payload(
            rejected_candidates=[rejected_entry()],
            exploration_replenishment={"status": "ok", "attempts": 9, "reason": "x"}))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_missing_shortage_note_fails(self):
        """有补抓穷尽记录但简报未注明「探索内容缺货」→ FAIL。"""
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(
            rejected_candidates=[rejected_entry()],
            exploration_replenishment=dict(REPLENISHMENT)))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_replenishment_does_not_waive_date_evidence(self):
        """补抓记录不豁免日期硬条件：引用候选窗口外仍 FAIL。"""
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        cand = self.write_cand(cand_payload(
            candidates=[good_candidate(publish_date="2026-10-01")],
            rejected_candidates=[rejected_entry()],
            exploration_replenishment=dict(REPLENISHMENT)))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_missing_date_evidence_fails(self):
        """引用候选缺 date_evidence → FAIL（布尔位/date_source 不能冒充证据）。"""
        brief = self.write_brief()
        c = good_candidate()
        del c["date_evidence"]
        cand = self.write_cand(cand_payload(candidates=[c]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_thin_body_still_fails_with_replenishment(self):
        """补抓记录不豁免正文实测：真实 content 不足仍 FAIL。"""
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        cand = self.write_cand(cand_payload(
            candidates=[good_candidate(content="x")],
            rejected_candidates=[rejected_entry()],
            exploration_replenishment=dict(REPLENISHMENT)))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_historical_dedup_still_fails(self):
        """历史去重不豁免：URL 已在历史简报推送过 → FAIL。"""
        hist = os.path.join(self.out_dir, "Daily-Brief-2026-10-07.md")
        with open(hist, "w", encoding="utf-8") as f:
            f.write("# 旧简报\n- 旧文 %s\n" % GOOD_URL)
        brief = self.write_brief()
        cand = self.write_cand(cand_payload())
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_rejected_entry_missing_audit_fields_fails(self):
        """拒收记录缺审计字段（reviewed_at 等）→ FAIL。"""
        r = rejected_entry()
        del r["reviewed_at"]
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        cand = self.write_cand(cand_payload(
            rejected_candidates=[r],
            exploration_replenishment=dict(REPLENISHMENT)))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_rejected_url_still_in_pool_fails(self):
        """拒收项仍留在 candidates（未移除）→ 协议违规 FAIL。"""
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        cand = self.write_cand(cand_payload(
            rejected_candidates=[rejected_entry(url=GOOD_URL, is_explore=False)]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_aggregate_leftover_in_payload_fails(self):
        """候选产物携带聚合页速览 → FAIL。"""
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(source_leftovers=[
            good_leftover(url=TECHCRUNCH_CATEGORY)]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_aggregate_candidate_referenced_fails(self):
        """引用候选为聚合页 → FAIL（即使日期/正文证据齐全）。"""
        brief = self.write_brief(url=FAMILYDOCTOR_NEWS)
        cand = self.write_cand(cand_payload(
            candidates=[good_candidate(url=FAMILYDOCTOR_NEWS)]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_unqualified_explore_left_in_pool_requires_review(self):
        mutations = (
            {"url": FAMILYDOCTOR_NEWS},
            {"url": "https://example.com/category/2026/10/08/story"},
            {"url": "https://example.com/page/2"},
            {"url": "https://bad\\\\host/story"},
            {"url": " https://explore.example.com/story "},
            {"content": "x", "word_count": 9999},
            {"date_verified": False},
            {"date_precision": "month"},
            {"publish_date": "2026-10-01"},
            {"publish_date": "2026-10-08T12:00:00"},
            {"publish_date": "2026-10"},
            {"date_source": "   "},
            {"date_evidence": True},
            {"window_outside_days": 1},
        )
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        for changes in mutations:
            with self.subTest(changes=changes):
                explore = good_candidate(url=EXP_URL, is_explore=True)
                explore.update(changes)
                cand = self.write_cand(cand_payload(
                    candidates=[good_candidate(), explore],
                    rejected_candidates=[rejected_entry(url=EXP_URL + "-old")],
                    exploration_replenishment=dict(REPLENISHMENT)))
                self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_unqualified_explore_without_rejection_cannot_disappear(self):
        brief = self.write_brief()
        cand = self.write_cand(cand_payload(candidates=[
            good_candidate(), good_candidate(url=EXP_URL, is_explore=True, content="x")]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_unqualified_explore_still_fails_with_valid_explore_included(self):
        brief = self.write_brief(extra="\n探索文章 %s\n" % EXP_URL)
        cand = self.write_cand(cand_payload(candidates=[
            good_candidate(), good_candidate(url=EXP_URL, is_explore=True),
            good_candidate(url=FAMILYDOCTOR_NEWS, is_explore=True)]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_unqualified_explore_duplicate_url_cannot_be_overwritten(self):
        brief = self.write_brief(extra="\n探索文章 %s\n" % EXP_URL)
        cand = self.write_cand(cand_payload(candidates=[
            good_candidate(), good_candidate(url=EXP_URL, is_explore=True, content="x"),
            good_candidate(url=EXP_URL, is_explore=True)]))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_historical_explore_left_in_pool_requires_review(self):
        hist = os.path.join(self.out_dir, "Daily-Brief-2026-10-07.md")
        with open(hist, "w", encoding="utf-8") as f:
            f.write("旧探索 %s\n" % EXP_URL)
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        cand = self.write_cand(cand_payload(candidates=[
            good_candidate(), good_candidate(url=EXP_URL, is_explore=True)],
            exploration_replenishment=dict(REPLENISHMENT)))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_valid_explore_cannot_be_waived_by_shortage_protocol(self):
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        cand = self.write_cand(cand_payload(candidates=[
            good_candidate(), good_candidate(url=EXP_URL, is_explore=True)],
            rejected_candidates=[rejected_entry(url=EXP_URL + "-old")],
            exploration_replenishment=dict(REPLENISHMENT)))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_valid_explore_included_passes_without_shortage_protocol(self):
        brief = self.write_brief(extra="\n探索文章 %s\n" % EXP_URL)
        cand = self.write_cand(cand_payload(candidates=[
            good_candidate(), good_candidate(url=EXP_URL, is_explore=True)],
            rejected_candidates=[rejected_entry(url=EXP_URL + "-old")]))
        self.assertEqual(self.run_validate([brief, cand]), 0)

    def test_replenishment_attempts_must_be_integer_one_or_two(self):
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        for attempts in (True, False, 1.0, 2.0, "1", 0, 3, None):
            with self.subTest(attempts=attempts):
                rep = dict(REPLENISHMENT, attempts=attempts)
                cand = self.write_cand(cand_payload(
                    rejected_candidates=[rejected_entry()],
                    exploration_replenishment=rep))
                self.assertEqual(self.run_validate([brief, cand]), 1)
        for attempts in (1, 2):
            with self.subTest(valid_attempts=attempts):
                cand = self.write_cand(cand_payload(
                    rejected_candidates=[rejected_entry()],
                    exploration_replenishment=dict(REPLENISHMENT, attempts=attempts)))
                self.assertEqual(self.run_validate([brief, cand]), 0)

    def test_replenishment_reason_must_be_nonempty_text(self):
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        for reason in ("", "   ", None, True, 12, ["reason"]):
            with self.subTest(reason=reason):
                cand = self.write_cand(cand_payload(
                    rejected_candidates=[rejected_entry()],
                    exploration_replenishment=dict(REPLENISHMENT, reason=reason)))
                self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_shortage_protocol_does_not_waive_run_id(self):
        brief = self.write_brief(run_id="wrong", extra=SHORTAGE_NOTE)
        cand = self.write_cand(cand_payload(
            rejected_candidates=[rejected_entry()],
            exploration_replenishment=dict(REPLENISHMENT)))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_shortage_protocol_does_not_waive_invalid_dates(self):
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        for pub in ("2026-10-08T12:00:00", "2026-10", "2026-02-30", ""):
            with self.subTest(pub=pub):
                cand = self.write_cand(cand_payload(
                    window={"start": "2026-01-01", "end": "2026-12-31"},
                    candidates=[good_candidate(publish_date=pub)],
                    rejected_candidates=[rejected_entry()],
                    exploration_replenishment=dict(REPLENISHMENT)))
                self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_shortage_protocol_does_not_waive_bad_window(self):
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        for window in ({}, {"start": "2026-02-30", "end": "2026-10-08"},
                       {"start": "2026-10-09", "end": "2026-10-08"}):
            with self.subTest(window=window):
                cand = self.write_cand(cand_payload(
                    window=window, rejected_candidates=[rejected_entry()],
                    exploration_replenishment=dict(REPLENISHMENT)))
                self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_shortage_protocol_does_not_waive_bad_leftover_date(self):
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        for pub in ("", "2026-10", "2026-10-08T12:00:00", "2026-02-30"):
            with self.subTest(pub=pub):
                cand = self.write_cand(cand_payload(
                    window={"start": "2026-01-01", "end": "2026-12-31"},
                    source_leftovers=[good_leftover(publish_date=pub)],
                    rejected_candidates=[rejected_entry()],
                    exploration_replenishment=dict(REPLENISHMENT)))
                self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_shortage_protocol_does_not_waive_historical_dedup(self):
        hist = os.path.join(self.out_dir, "Daily-Brief-2026-10-07.md")
        with open(hist, "w", encoding="utf-8") as f:
            f.write("旧文章 %s\n" % GOOD_URL)
        brief = self.write_brief(extra=SHORTAGE_NOTE)
        cand = self.write_cand(cand_payload(
            rejected_candidates=[rejected_entry()],
            exploration_replenishment=dict(REPLENISHMENT)))
        self.assertEqual(self.run_validate([brief, cand]), 1)

    def test_no_explore_no_rejection_no_requirement(self):
        """无探索候选也无探索拒收 → 不要求缺货说明（不误伤正常期）。"""
        brief = self.write_brief()
        cand = self.write_cand(cand_payload())
        self.assertEqual(self.run_validate([brief, cand]), 0)


class TestRejectCLI(unittest.TestCase):
    """reject_candidate CLI：移除 + 审计 + 幂等，全部落在临时文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cand_path = os.path.join(self.tmp.name,
                                      "Daily-Brief-2026-10-08-candidates.json")
        self.payload = {
            "run_id": "r1",
            "generated_at": "2026-10-08T01:00:00+08:00",
            "window": {"start": "2026-10-07", "end": "2026-10-08"},
            "candidates": [good_candidate(),
                           good_candidate(url=EXP_URL, is_explore=True)],
            "source_leftovers": [good_leftover()],
        }
        self._write(self.payload)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, payload):
        with open(self.cand_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    def _read(self):
        with open(self.cand_path, encoding="utf-8") as f:
            return json.load(f)

    def run_cli(self, argv):
        return reject_candidate.main(argv)

    def test_reject_moves_candidate_with_audit_fields(self):
        rc = self.run_cli(["--candidates", self.cand_path, "--url", EXP_URL,
                           "--reason", "聚合页，非独立文章", "--reviewed-by", "小g"])
        self.assertEqual(rc, 0)
        p = self._read()
        self.assertEqual([c["url"] for c in p["candidates"]], [GOOD_URL])
        self.assertEqual(len(p["rejected_candidates"]), 1)
        r = p["rejected_candidates"][0]
        self.assertEqual(r["url"], EXP_URL)
        self.assertTrue(r["is_explore"])
        self.assertEqual(r["reason"], "聚合页，非独立文章")
        self.assertEqual(r["reviewed_by"], "小g")
        self.assertTrue(r["reviewed_at"])
        self.assertEqual(r["entry"], self.payload["candidates"][1])  # 原条目所有字段保留
        self.assertEqual(p["run_id"], self.payload["run_id"])
        self.assertEqual(p["window"], self.payload["window"])
        self.assertEqual(p["source_leftovers"], self.payload["source_leftovers"])

    def test_reject_from_leftovers(self):
        rc = self.run_cli(["--candidates", self.cand_path,
                           "--url", good_leftover()["url"], "--reason", "频道页"])
        self.assertEqual(rc, 0)
        p = self._read()
        self.assertEqual(p["source_leftovers"], [])
        self.assertFalse(p["rejected_candidates"][0]["is_explore"])
        self.assertEqual(p["rejected_candidates"][0]["entry"], self.payload["source_leftovers"][0])

    def test_idempotent_second_reject(self):
        argv = ["--candidates", self.cand_path, "--url", EXP_URL, "--reason", "x"]
        self.assertEqual(self.run_cli(argv), 0)
        once = self._read()
        self.assertEqual(self.run_cli(argv), 0)
        self.assertEqual(self._read(), once)  # 重复拒收不覆盖原条目及首次审核记录
        self.assertEqual(len(once["rejected_candidates"]), 1)

    def test_url_not_found_fails(self):
        rc = self.run_cli(["--candidates", self.cand_path,
                           "--url", "https://example.com/2026/10/08/nope",
                           "--reason", "x"])
        self.assertEqual(rc, 1)

    def test_missing_reason_fails(self):
        rc = self.run_cli(["--candidates", self.cand_path, "--url", EXP_URL])
        self.assertEqual(rc, 1)

    def test_list(self):
        self.run_cli(["--candidates", self.cand_path, "--url", EXP_URL, "--reason", "x"])
        self.assertEqual(self.run_cli(["--candidates", self.cand_path, "--list"]), 0)

    def test_cli_then_validate_end_to_end(self):
        """CLI 拒收探索候选 → 校验要求补抓记录 + 缺货说明；补齐后 PASS。"""
        self.run_cli(["--candidates", self.cand_path, "--url", EXP_URL,
                      "--reason", "聚合页，非独立文章"])
        out_dir = os.path.join(self.tmp.name, "output")
        os.makedirs(out_dir)
        brief = os.path.join(out_dir, "Daily-Brief-2026-10-08.md")
        with open(brief, "w", encoding="utf-8") as f:
            f.write("# Daily Brief 2026-10-08\n<!-- run_id: r1 -->\n"
                    "## 今日热门文章\n正文 %s 好内容。\n\n"
                    "## 参考资料\n- [1] [好文章](%s)\n" % (GOOD_URL, GOOD_URL))
        with mock.patch.object(validate_brief, "OUTPUT_DIR", out_dir), \
                mock.patch.object(validate_brief.brief_record, "record_brief",
                                  return_value=None):
            # 缺补抓记录 + 缺缺货说明 → FAIL
            with mock.patch.object(validate_brief.sys, "argv",
                                   ["validate_brief.py", brief, self.cand_path]):
                self.assertEqual(validate_brief.main(), 1)
            # 补齐补抓穷尽记录 + 缺货说明 → PASS
            p = self._read()
            p["exploration_replenishment"] = dict(REPLENISHMENT)
            self._write(p)
            with open(brief, "a", encoding="utf-8") as f:
                f.write(SHORTAGE_NOTE)
            with mock.patch.object(validate_brief.sys, "argv",
                                   ["validate_brief.py", brief, self.cand_path]):
                self.assertEqual(validate_brief.main(), 0)


if __name__ == "__main__":
    unittest.main()
