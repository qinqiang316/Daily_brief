# -*- coding: utf-8 -*-
"""视野拓展源：订阅解析、日期证据、全文升级窗口把关。"""
import os
import unittest
from datetime import datetime
from unittest import mock

from tests.helpers import ROOT, SCRIPTS, long_body  # noqa: F401
from modules import horizon

FEED_HEAD = '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'


def rss(items):
    body = []
    for it in items:
        body.append(
            "<item><title>%s</title><link>%s</link><pubDate>%s</pubDate>"
            "<description>%s</description></item>"
            % (it[0], it[1], it[2], it[3] if len(it) > 3 else "摘要"))
    return FEED_HEAD + "".join(body) + "</channel></rss>"


WIN_START = datetime(2026, 10, 9)
WIN_END = datetime(2026, 10, 10)


def src(**kw):
    s = {"key": "t", "label": "测试源", "region": "美国", "direction": "科技",
         "feed": "https://feed.example.com/rss", "prefer_fulltext": False}
    s.update(kw)
    return s


class HorizonCase(unittest.TestCase):
    def setUp(self):
        # 单测一律走 mock 订阅，关闭全局开关的影响
        self._off = os.environ.pop("DAILYBRIEF_HORIZON_OFF", None)

    def tearDown(self):
        if self._off is not None:
            os.environ["DAILYBRIEF_HORIZON_OFF"] = self._off


class TestParseFeed(HorizonCase):
    def test_parse_rss_items(self):
        text = rss([("标题A", "https://a.example.com/2026/10/09/x", "Thu, 09 Oct 2026 08:00:00 +0800")])
        items = horizon.parse_feed(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "标题A")
        self.assertEqual(items[0]["url"], "https://a.example.com/2026/10/09/x")
        self.assertEqual(items[0]["publish_date"], "2026-10-09")

    def test_atom_href_link(self):
        text = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title>'
                '<link href="https://b.example.com/2026/10/10/y"/>'
                '<published>2026-10-10T01:00:00+08:00</published></entry></feed>')
        items = horizon.parse_feed(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://b.example.com/2026/10/10/y")
        self.assertEqual(items[0]["publish_date"], "2026-10-10")

    def test_entry_without_url_skipped(self):
        text = rss([("无链接", "", "Thu, 09 Oct 2026 08:00:00 +0800")])
        self.assertEqual(horizon.parse_feed(text), [])

    def test_rdf_item(self):
        """RDF (RSS 1.0) 的 <item rdf:about> 也要解析出来。"""
        text = ('<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                '<item rdf:about="https://c.example.com/atcl/nxt/news/24/03421/">'
                '<title>日文标题</title><link>https://c.example.com/atcl/nxt/news/24/03421/</link>'
                '</item></rdf:RDF>')
        items = horizon.parse_feed(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "日文标题")


class TestCollectSource(HorizonCase):
    def test_window_outside_entry_dropped(self):
        """订阅条目日期早于窗口起点 → 不进视野拓展（无日精度窗口内证据即丢弃）。"""
        text = rss([("旧闻", "https://a.example.com/2026/10/01/old", "Wed, 01 Oct 2026 08:00:00 +0800")])
        with mock.patch.object(horizon, "fetch_feed", return_value=text):
            promoted, items, status = horizon.collect_source(src(), set(), WIN_START, WIN_END)
        self.assertEqual(promoted, [])
        self.assertEqual(items, [])
        self.assertTrue(status)  # 缺货原因如实返回

    def test_no_date_entry_dropped(self):
        """订阅无日期、URL/标题无日期、文章页也取不到 → 丢弃，不猜测发布日期。"""
        text = rss([("无日期", "https://a.example.com/some-post", "")])
        with mock.patch.object(horizon, "fetch_feed", return_value=text), \
             mock.patch.object(horizon.retrieve, "fetch_article", return_value=None):
            promoted, items, _ = horizon.collect_source(src(), set(), WIN_START, WIN_END)
        self.assertEqual(promoted, [])
        self.assertEqual(items, [])

    def test_dedup_entry_dropped(self):
        text = rss([("已推", "https://a.example.com/2026/10/09/x", "Thu, 09 Oct 2026 08:00:00 +0800")])
        with mock.patch.object(horizon, "fetch_feed", return_value=text):
            promoted, items, _ = horizon.collect_source(
                src(), {"https://a.example.com/2026/10/09/x"}, WIN_START, WIN_END)
        self.assertEqual(items, [])

    def test_aggregate_homepage_dropped(self):
        text = rss([("首页", "https://a.example.com/", "Thu, 09 Oct 2026 08:00:00 +0800")])
        with mock.patch.object(horizon, "fetch_feed", return_value=text):
            _, items, _ = horizon.collect_source(src(), set(), WIN_START, WIN_END)
        self.assertEqual(items, [])

    def test_feed_unavailable_returns_status(self):
        """订阅源抓不到 → 返回缺货原因，不静默消失。"""
        with mock.patch.object(horizon, "fetch_feed", return_value=None):
            promoted, items, status = horizon.collect_source(src(), set(), WIN_START, WIN_END)
        self.assertEqual((promoted, items), ([], []))
        self.assertTrue(status)

    def test_nofulltext_source_keeps_title_with_date_evidence(self):
        """付费墙源：只留标题+原文发布日期，条目须带 date_source/date_evidence。"""
        text = rss([("付费墙文", "https://a.example.com/2026/10/09/pay", "Thu, 09 Oct 2026 08:00:00 +0800")])
        with mock.patch.object(horizon, "fetch_feed", return_value=text):
            promoted, items, _ = horizon.collect_source(src(), set(), WIN_START, WIN_END)
        self.assertEqual(promoted, [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["publish_date"], "2026-10-09")
        self.assertTrue(items[0]["date_source"])
        self.assertTrue(items[0]["date_evidence"])

    def test_date_probe_from_article_metadata(self):
        """订阅无日期时，抓文章页取 JSON-LD 发布日期（窗口内才收）。"""
        text = rss([("无日期", "https://a.example.com/post-a", "")])
        art = {"text": "x", "metadata": {"json_ld": {"datePublished": "2026-10-09T10:00:00+08:00"}}}
        with mock.patch.object(horizon, "fetch_feed", return_value=text), \
             mock.patch.object(horizon.retrieve, "fetch_article", return_value=art):
            promoted, items, _ = horizon.collect_source(src(), set(), WIN_START, WIN_END)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["publish_date"], "2026-10-09")
        self.assertEqual(items[0]["date_source"], "json_ld")

    def test_fulltext_promotion_requires_min_words_and_window(self):
        """可抓全文源：正文 ≥ MIN_WORDS 且日期窗口内才升级进候选池。"""
        text = rss([("长文", "https://a.example.com/2026/10/09/deep", "Thu, 09 Oct 2026 08:00:00 +0800")])
        good = {u: {"text": long_body(900), "metadata": {}}
                for u in ["https://a.example.com/2026/10/09/deep"]}
        with mock.patch.object(horizon, "fetch_feed", return_value=text), \
             mock.patch.object(horizon.retrieve, "fetch_texts_parallel", return_value=good):
            promoted, items, _ = horizon.collect_source(
                src(prefer_fulltext=True), set(), WIN_START, WIN_END)
        self.assertEqual(len(promoted), 1)
        self.assertEqual(items, [])
        self.assertGreaterEqual(horizon.retrieve.wc(promoted[0]["content"]), horizon.filter_mod.MIN_WORDS)
        self.assertTrue(promoted[0]["date_source"])

    def test_fulltext_short_body_downgrades_to_title(self):
        """抓到的正文不足 MIN_WORDS → 降级为标题/短讯，不进候选池。"""
        text = rss([("短文", "https://a.example.com/2026/10/09/short", "Thu, 09 Oct 2026 08:00:00 +0800")])
        short = {u: {"text": "太短", "metadata": {}}
                 for u in ["https://a.example.com/2026/10/09/short"]}
        with mock.patch.object(horizon, "fetch_feed", return_value=text), \
             mock.patch.object(horizon.retrieve, "fetch_texts_parallel", return_value=short):
            promoted, items, _ = horizon.collect_source(
                src(prefer_fulltext=True), set(), WIN_START, WIN_END)
        self.assertEqual(promoted, [])
        self.assertEqual(len(items), 1)


class TestFetchHorizon(HorizonCase):
    def test_per_source_and_global_caps(self):
        items = [("T%d" % i, "https://a.example.com/2026/10/09/p%d" % i,
                  "Thu, 09 Oct 2026 08:00:00 +0800") for i in range(8)]
        text = rss(items)
        with mock.patch.object(horizon, "fetch_feed", return_value=text):
            promoted, horizon_items, _ = horizon.fetch_horizon(
                set(), WIN_START, WIN_END, sources=[src()])
        self.assertEqual(promoted, [])
        self.assertLessEqual(len(horizon_items), horizon.MAX_PER_SOURCE)

    def test_unavailable_sources_recorded(self):
        with mock.patch.object(horizon, "fetch_feed", return_value=None):
            promoted, items, unavailable = horizon.fetch_horizon(
                set(), WIN_START, WIN_END, sources=[src()])
        self.assertEqual((promoted, items), ([], []))
        self.assertEqual(len(unavailable), 1)
        self.assertIn("订阅源不可用", unavailable[0]["reason"])

    def test_budget_exhausted_skips(self):
        class B:
            def expired(self): return True
            def timeout(self, cap): return cap
        with mock.patch.object(horizon, "fetch_feed") as f:
            promoted, items, _ = horizon.fetch_horizon(set(), WIN_START, WIN_END,
                                                        budget=B(), sources=[src()])
        self.assertEqual((promoted, items), ([], []))
        f.assert_not_called()

    def test_page_probe_respects_cap(self):
        """无日期条目的文章页补抓不得超过 MAX_DATE_PROBE，不能用 budget=None 绕开。"""
        items = [("无日期%d" % i, "https://a.example.com/post-%d" % i, "") for i in range(6)]
        with mock.patch.object(horizon, "fetch_feed", return_value=rss(items)), \
             mock.patch.object(horizon.retrieve, "fetch_article", return_value=None) as fa:
            promoted, kept, _ = horizon.collect_source(src(), set(), WIN_START, WIN_END)
        self.assertEqual((promoted, kept), ([], []))
        self.assertEqual(fa.call_count, horizon.MAX_DATE_PROBE)

    def test_url_date_does_not_spend_probe(self):
        """URL 里已有日精度日期时不抓文章页，也不占用补抓名额。"""
        text = rss([("有网址日期", "https://a.example.com/2026/10/09/x", "")])
        with mock.patch.object(horizon, "fetch_feed", return_value=text), \
             mock.patch.object(horizon.retrieve, "fetch_article") as fa:
            _, items, _ = horizon.collect_source(src(), set(), WIN_START, WIN_END)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["publish_date"], "2026-10-09")
        self.assertEqual(items[0]["date_source"], "url")
        fa.assert_not_called()

    def test_iso_datetime_converts_to_shanghai_day(self):
        """带时区的 ISO 日期按上海日历日，不按字符串前 10 位猜。"""
        text = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>跨日</title>'
                '<link href="https://c.example.com/atcl/nxt/news/24/1/"/>'
                '<published>2026-10-10T00:30:00+09:00</published></entry></feed>')
        items = horizon.parse_feed(text)
        self.assertEqual(items[0]["publish_date"], "2026-10-09")

    def test_duplicate_across_sources_kept_once(self):
        text = rss([("同文", "https://a.example.com/2026/10/09/x", "Thu, 09 Oct 2026 08:00:00 +0800")])
        with mock.patch.object(horizon, "fetch_feed", return_value=text):
            _, items, _ = horizon.fetch_horizon(
                set(), WIN_START, WIN_END, sources=[src(key="a"), src(key="b")])
        self.assertEqual(len(items), 1)

    def test_all_deduped_is_not_unavailable(self):
        text = rss([("已推", "https://a.example.com/2026/10/09/x", "Thu, 09 Oct 2026 08:00:00 +0800")])
        with mock.patch.object(horizon, "fetch_feed", return_value=text):
            _, items, unavailable = horizon.fetch_horizon(
                {"https://a.example.com/2026/10/09/x"}, WIN_START, WIN_END, sources=[src()])
        self.assertEqual(items, [])
        self.assertEqual(unavailable, [])


class _Resp:
    def __init__(self, body):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]


class TestLatePost(HorizonCase):
    def test_list_month_day_is_not_a_publish_date(self):
        """列表 release_time 没有年份，文章页也拿不到日期 → 丢弃，不把「10月10日」当成原文日期。"""
        payload = ('{"code":1,"data":[{"title":"踏板","abstract":"摘要",'
                   '"release_time":"10月10日","detail_url":"/news/dj_detail?id=3753"}]}')
        with mock.patch.object(horizon.urllib.request, "urlopen", return_value=_Resp(payload)), \
             mock.patch.object(horizon.retrieve, "fetch_article", return_value=None):
            promoted, items, status = horizon.collect_source(
                src(key="latepost", kind="latepost", prefer_fulltext=True),
                set(), WIN_START, WIN_END)
        self.assertEqual(promoted, [])
        self.assertEqual(items, [])
        self.assertTrue(status)

    def test_page_release_time_promotes_fulltext(self):
        """文章页 var release_time 是日精度原文日期；正文够长才升级进候选池。"""
        payload = ('{"code":1,"data":[{"title":"踏板断裂","abstract":"刹车踏板",'
                   '"release_time":"10月10日","detail_url":"/news/dj_detail?id=3753"}]}')
        norm = "https://latepost.com/news/dj_detail?id=3753"
        live = "https://www.latepost.com/news/dj_detail?id=3753"
        art = {"text": long_body(900), "metadata": {
            "json_ld": {}, "meta": {"release_time": "2026/10/09"}, "time": []}}
        with mock.patch.object(horizon.urllib.request, "urlopen", return_value=_Resp(payload)), \
             mock.patch.object(horizon.retrieve, "fetch_article", return_value=art), \
             mock.patch.object(horizon.retrieve, "fetch_texts_parallel", return_value={live: art}):
            promoted, items, status = horizon.collect_source(
                src(key="latepost", kind="latepost", prefer_fulltext=True),
                set(), WIN_START, WIN_END)
        self.assertEqual(status, None)
        self.assertEqual(items, [])
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0]["url"], norm)
        self.assertEqual(promoted[0]["publish_date"], "2026-10-09")
        self.assertEqual(promoted[0]["date_source"], "html_meta")
        self.assertGreaterEqual(horizon.retrieve.wc(promoted[0]["content"]), horizon.filter_mod.MIN_WORDS)


if __name__ == "__main__":
    unittest.main()
