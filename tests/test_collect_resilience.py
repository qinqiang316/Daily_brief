# -*- coding: utf-8 -*-
"""采集韧性：锁、原子落盘、同日缓存复用、空池熔断不覆盖、总预算与超时隔离、
速览日期证据（月精度/TG 搬运日拒收，合格证据放行）。"""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from tests.helpers import ROOT, SCRIPTS, long_body  # noqa: F401
import collect_brief
from modules import retrieve, window


def qualified_payload(today_s):
    """构造严格合格的同日候选缓存（过 load_valid_cache 全部校验）。"""
    day_before = (datetime.strptime(today_s, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "run_id": "20261008T010000-abcd1234",
        "generated_at": today_s + "T01:00:00+08:00",
        "window": {"start": day_before, "end": today_s},
        "candidates": [{
            "title": "好文章", "url": "https://example.com/2026/10/08/good",
            "domain": "example.com",
            "publish_date": today_s, "date_verified": True,
            "date_source": "url", "date_evidence": "https://example.com/2026/10/08/good",
            "date_precision": "day", "window_outside_days": 0,
            "word_count": 800, "content": long_body(),
        }],
        "source_leftovers": [],
    }


class TestLock(unittest.TestCase):
    def test_second_instance_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            lock_path = os.path.join(d, "x.lock")
            h1 = collect_brief.acquire_lock(lock_path)
            self.assertIsNotNone(h1)
            h2 = collect_brief.acquire_lock(lock_path)
            self.assertIsNone(h2)  # 已被占用 → 拒绝并发采集
            collect_brief.release_lock(h1)
            h3 = collect_brief.acquire_lock(lock_path)
            self.assertIsNotNone(h3)  # 释放后可再获取
            collect_brief.release_lock(h3)


class TestAtomicWriteAndCache(unittest.TestCase):
    def test_atomic_write_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.json")
            collect_brief.atomic_write_json(path, {"a": 1})
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"a": 1})
            self.assertFalse([f for f in os.listdir(d) if f.startswith(".tmp_")])

    def test_load_valid_cache(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            today_s = "2026-10-08"
            collect_brief.atomic_write_json(path, qualified_payload(today_s))
            self.assertIsNotNone(collect_brief.load_valid_cache(path, today_s))
            self.assertIsNone(collect_brief.load_valid_cache(path, "2026-10-09"))
            self.assertIsNone(collect_brief.load_valid_cache(path + ".missing", today_s))
            bad = os.path.join(d, "b.json")
            collect_brief.atomic_write_json(bad, {"generated_at": "2026-10-08T01:00:00+08:00",
                                                  "candidates": []})
            self.assertIsNone(collect_brief.load_valid_cache(bad, today_s))

    def test_same_day_bad_cache_rejected(self):
        """同日坏缓存一律 MISS：缺 run_id / 窗口非当日 / 正文实测不足 / 月精度 /
        窗口外日期 / 缺 date_source / 坏速览，都不得复用。"""
        today_s = "2026-10-08"

        def cached(mutate):
            payload = qualified_payload(today_s)
            mutate(payload)
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "c.json")
                collect_brief.atomic_write_json(path, payload)
                return collect_brief.load_valid_cache(path, today_s)

        self.assertIsNone(cached(lambda p: p.pop("run_id")))  # 无 run_id
        self.assertIsNone(cached(lambda p: p["window"].update(end="2026-10-07")))  # 窗口非当日
        self.assertIsNone(cached(lambda p: p["candidates"][0].update(content="x")))  # 正文实测不足
        self.assertIsNone(cached(lambda p: p["candidates"][0].update(date_precision="month")))  # 月精度
        self.assertIsNone(cached(lambda p: p["candidates"][0].update(publish_date="2026-09-01")))  # 窗口外
        self.assertIsNone(cached(lambda p: p["candidates"][0].pop("date_source")))  # 缺日期来源
        self.assertIsNone(cached(lambda p: p["candidates"][0].update(url="https://bad\\x")))  # 非法 URL

        def bad_leftover(p):
            p["source_leftovers"] = [{
                "title": "旧速览", "url": "https://leftover.example.com/2026/09/01/old",
                "publish_date": "2026-09-01", "date_verified": True,
                "date_source": "url", "date_evidence": "u", "date_precision": "day"}]
        self.assertIsNone(cached(bad_leftover))  # 速览日期窗口外

        def good_leftover(p):
            p["source_leftovers"] = [{
                "title": "合格速览", "url": "https://leftover.example.com/2026/10/08/lo",
                "publish_date": today_s, "date_verified": True,
                "date_source": "url", "date_evidence": "u", "date_precision": "day"}]
        self.assertIsNotNone(cached(good_leftover))  # 合格速览不影响命中


class CollectMainCase(unittest.TestCase):
    """把采集入口的所有外部依赖 mock 掉，验证锁/缓存/熔断行为（不联网）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cand_dir = os.path.join(self.tmp.name, "_candidates")
        os.makedirs(self.cand_dir)
        self.patches = [
            mock.patch.object(collect_brief, "CAND_DIR", self.cand_dir),
            mock.patch.object(collect_brief, "LOCK_FILE",
                              os.path.join(self.tmp.name, "lock")),
            mock.patch.object(collect_brief.retrieve, "fetch_hn", return_value=[]),
            mock.patch.object(collect_brief.retrieve, "fetch_telegram_channels",
                              return_value=[]),
            mock.patch.object(collect_brief.retrieve, "fetch_agent_case_share",
                              return_value=[]),
            mock.patch.object(collect_brief.rank, "search_batch_with_tags",
                              lambda *a, **kw: None),
            mock.patch.object(collect_brief.rank, "build_extra_queries",
                              return_value=([], None, "生活")),
            mock.patch.object(collect_brief.filter_mod, "auto_update_dedup",
                              return_value=set()),
            mock.patch.object(window, "get_latest_brief_date", return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_empty_pool_circuit_breaker_keeps_old_cache(self):
        """全源失败 → 空池熔断：退出 1 且不覆盖已有候选文件。"""
        today_s = collect_brief.datetime.now(collect_brief.TZ).strftime("%Y-%m-%d")
        out_file = collect_brief._cand_path(today_s)
        sentinel = {"run_id": "old", "generated_at": today_s + "T00:00:00+08:00",
                    "candidates": [{"url": "https://example.com/keep"}]}
        collect_brief.atomic_write_json(out_file, sentinel)
        rc = collect_brief.main([])
        self.assertEqual(rc, 1)
        with open(out_file, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["run_id"], "old")  # 未被覆盖

    def test_lock_contention_fails_fast(self):
        held = collect_brief.acquire_lock()
        self.assertIsNotNone(held)
        try:
            self.assertEqual(collect_brief.main([]), 1)
        finally:
            collect_brief.release_lock(held)

    def test_reuse_cache_explicit(self):
        """--reuse-cache：存在同日合格缓存时直接复用退出 0（不采集）。"""
        today_s = collect_brief.datetime.now(collect_brief.TZ).strftime("%Y-%m-%d")
        out_file = collect_brief._cand_path(today_s)
        payload = qualified_payload(today_s)
        payload["run_id"] = "cached"
        collect_brief.atomic_write_json(out_file, payload)
        self.assertEqual(collect_brief.main(["--reuse-cache"]), 0)

    def test_reuse_cache_miss_falls_through_to_collect(self):
        """无有效缓存时 --reuse-cache 继续采集；全源失败则熔断退出 1。"""
        self.assertEqual(collect_brief.main(["--reuse-cache"]), 1)


class TestBudgetAndTimeout(unittest.TestCase):
    def test_budget_basics(self):
        b = retrieve.Budget(0)
        self.assertTrue(b.expired())
        b2 = retrieve.Budget(30)
        self.assertFalse(b2.expired())
        self.assertLessEqual(b2.timeout(60), 30)
        self.assertGreaterEqual(b2.timeout(60), 1)

    def test_fetch_hn_timeout_isolated(self):
        """HN API 超时 → 局部故障隔离：返回 [] 而不抛出。"""
        import socket
        with mock.patch("modules.retrieve.urllib.request.urlopen",
                        side_effect=socket.timeout("timed out")):
            self.assertEqual(retrieve.fetch_hn(collect_brief.datetime.now(collect_brief.TZ)), [])

    def test_fetch_texts_parallel_bounded_by_budget(self):
        """并发抓取受总预算约束，单 URL 故障隔离为 None。"""
        def slow(url, timeout=15, limit=2500):
            time.sleep(0.2)
            return None
        with mock.patch.object(retrieve, "fetch_article_text", side_effect=slow):
            b = retrieve.Budget(0.3)
            start = time.time()
            res = retrieve.fetch_texts_parallel(
                ["https://example.com/%d" % i for i in range(8)], budget=b)
            elapsed = time.time() - start
        self.assertTrue(all(v is None for v in res.values()))
        self.assertLess(elapsed, 10)  # 不随 URL 数线性累积

    def test_run_cli_skipped_when_budget_exhausted(self):
        b = retrieve.Budget(0)
        self.assertIsNone(retrieve.run_cli(["batch_search"], budget=b))

    def test_budget_derive_reserves_time(self):
        """派生子预算给后续阶段预留：子预算 deadline 不晚于 now+seconds 且不超过母预算。"""
        b = retrieve.Budget(480)
        sub = b.derive(330)
        self.assertLessEqual(sub.remaining(), 331)
        self.assertGreater(sub.remaining(), 0)
        self.assertLessEqual(retrieve.Budget(5).derive(330).remaining(), 5)
        self.assertTrue(retrieve.Budget(0).derive(330).expired())

    def test_fetch_hn_budget_caps_timeout(self):
        """HN 接预算：单次 socket 超时 ≤ 剩余预算（socket timeout ≠ 总墙钟）。"""
        calls = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"hits": []}'

        def fake_urlopen(url, timeout=None):
            calls["timeout"] = timeout
            return Resp()

        from datetime import datetime as _dt
        with mock.patch("modules.retrieve.urllib.request.urlopen",
                        side_effect=fake_urlopen):
            retrieve.fetch_hn(_dt.now(retrieve.TZ), budget=retrieve.Budget(10))
        self.assertLessEqual(calls["timeout"], 10)

    def test_fetch_texts_parallel_with_metadata(self):
        """with_metadata=True 时经 fetch_article 返回正文+metadata 结构。"""
        art = {"text": "正文" * 200, "metadata": {"json_ld": {}, "meta": {}}}
        with mock.patch.object(retrieve, "fetch_article", return_value=art):
            res = retrieve.fetch_texts_parallel(["https://example.com/a"],
                                                with_metadata=True)
        self.assertEqual(res["https://example.com/a"], art)


class TestHttpFallbackBudgetAndTmpfile(unittest.TestCase):
    def test_http_fallback_receives_budget_and_tmpfile_cleaned(self):
        """HTTP 兜底必须接同一预算；查询临时文件走可指定目录且 finally 清理。"""
        from modules import rank
        candidates, stats = [], {}
        budget = retrieve.Budget(30)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(rank, "run_cli", return_value=None), \
                 mock.patch.object(rank, "anysearch_http_batch",
                                   return_value=[]) as http:
                rank.search_batch_with_tags(
                    "cn", [{"query": "q", "max_results": 1, "direction": "科技"}],
                    candidates, stats, None, tmp_dir=d, budget=budget)
            _, kw = http.call_args
            self.assertIs(kw.get("budget"), budget)  # 预算传给 HTTP 兜底
            # finally 清理：目录里不得残留查询临时文件
            self.assertEqual([f for f in os.listdir(d) if "brief_queries" in f], [])

    def test_http_fallback_skipped_when_budget_exhausted(self):
        with mock.patch("modules.retrieve.urllib.request.urlopen") as uo, \
                mock.patch.object(retrieve, "_get_anysearch_api_key", return_value="test-only-key"):
            self.assertIsNone(retrieve.anysearch_http_batch(
                [{"query": "q"}], budget=retrieve.Budget(0)))
            uo.assert_not_called()


class TestLeftoverDateEvidence(unittest.TestCase):
    """速览日期把关：统一 extract_date_evidence，只收日精度窗口内原文日期。
    月精度 / TG 搬运日 / 无证据一律拒收；合格条目携带 date_source/evidence/precision。"""

    def setUp(self):
        self.window_start = datetime(2026, 10, 7)
        self.today = datetime(2026, 10, 8)

    def pick(self, items):
        return collect_brief.pick_leftover_item(items, set(), set(),
                                                self.window_start, self.today)

    def test_month_precision_leftover_rejected(self):
        """URL 只有月精度日期（/2026/10/）→ 速览不得放行。"""
        it = {"title": "月度汇总", "url": "https://example.com/2026/10/monthly-report",
              "content": ""}
        self.assertIsNone(self.pick([it]))

    def test_tg_repost_date_not_original_date(self):
        """TG 搬运日（date_verified=true, 2026-10-08）不得冒充原文日：
        原文 URL 日期 2026-09-01 在窗口外 → 拒收。"""
        it = {"title": "旧文搬运", "url": "https://example.com/2026/09/01/old-article",
              "content": "Telegram @ch: 搬运一篇旧文",
              "publish_date": "2026-10-08", "date_verified": True}
        self.assertIsNone(self.pick([it]))

    def test_tg_repost_no_evidence_skipped(self):
        """TG 布尔位 + 窗口内搬运日，但 URL/标题/正文均无原文日期证据 → 跳过。"""
        it = {"title": "无日期文章", "url": "https://example.com/post/some-slug",
              "content": "Telegram @ch: 快讯一则",
              "publish_date": "2026-10-08", "date_verified": True}
        self.assertIsNone(self.pick([it]))

    def test_future_date_leftover_rejected(self):
        it = {"title": "未来文章", "url": "https://example.com/2026/10/09/future-piece",
              "content": ""}
        self.assertIsNone(self.pick([it]))

    def test_qualified_leftover_carries_evidence(self):
        """日精度窗口内 URL 日期 → 放行并携带 date_source/date_evidence/date_precision。"""
        it = {"title": "合格速览", "url": "https://example.com/2026/10/08/great-piece",
              "content": ""}
        picked = self.pick([it])
        self.assertIsNotNone(picked)
        self.assertEqual(picked["publish_date"], "2026-10-08")
        self.assertEqual(picked["date_precision"], "day")
        self.assertEqual(picked["date_source"], "url")
        self.assertTrue(picked["date_evidence"])
        self.assertTrue(picked["date_verified"])

    def test_homepage_extract_respects_budget(self):
        """特殊源主页 extract 受总 Budget 控制：预算耗尽不发任何网络请求。"""
        cfg = {"list_url": "https://www.jiqizhixin.com/industry",
               "url_pattern": r"https://www\.jiqizhixin\.com/articles/\d{4}-\d{2}-\d{2}-\d+"}
        with mock.patch.object(collect_brief.retrieve, "anysearch_extract") as ex:
            picked = collect_brief.pick_homepage_latest_item(
                cfg, set(), set(), self.window_start, self.today,
                budget=retrieve.Budget(0))
        self.assertIsNone(picked)
        ex.assert_not_called()

    def test_homepage_extract_receives_budget(self):
        """预算未耗尽时 extract 调用透传同一 Budget。"""
        cfg = {"list_url": "https://www.jiqizhixin.com/industry",
               "url_pattern": r"https://www\.jiqizhixin\.com/articles/\d{4}-\d{2}-\d{2}-\d+"}
        budget = retrieve.Budget(60)
        with mock.patch.object(collect_brief.retrieve, "anysearch_extract",
                               return_value=[]) as ex:
            self.assertIsNone(collect_brief.pick_homepage_latest_item(
                cfg, set(), set(), self.window_start, self.today, budget=budget))
        _, kw = ex.call_args
        self.assertIs(kw.get("budget"), budget)

    def test_homepage_latest_requires_day_precision(self):
        """主页 extract 条目同样只收日精度窗口内日期。"""
        cfg = {"list_url": "https://www.jiqizhixin.com/industry",
               "url_pattern": r"https://www\.jiqizhixin\.com/articles/\d{4}-\d{2}-\d{2}-\d+"}
        items = [
            {"title": "窗口外旧文", "url": "https://www.jiqizhixin.com/articles/2026-09-01-1"},
            {"title": "窗口内新文", "url": "https://www.jiqizhixin.com/articles/2026-10-08-2"},
        ]
        with mock.patch.object(collect_brief.retrieve, "anysearch_extract",
                               return_value=items):
            picked = collect_brief.pick_homepage_latest_item(
                cfg, set(), set(), self.window_start, self.today,
                budget=retrieve.Budget(60))
        self.assertIsNotNone(picked)
        self.assertEqual(picked["publish_date"], "2026-10-08")
        self.assertEqual(picked["date_precision"], "day")
        self.assertEqual(picked["date_source"], "url")


if __name__ == "__main__":
    unittest.main()
