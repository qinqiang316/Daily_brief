# -*- coding: utf-8 -*-
"""推荐池准入：HN 标题不得入池、旧文新热议、月精度日期、未来日期、窗口外。"""
import unittest
from datetime import date
from unittest import mock

from tests.helpers import long_body, make_candidate  # noqa: F401
from modules import rank


WINDOW_START = date(2026, 10, 6)
TODAY = date(2026, 10, 8)


def run_filter(candidates, texts=None):
    """texts: {url: 抓取的正文}，模拟离线正文抓取结果。"""
    with mock.patch.object(rank, "fetch_texts_parallel",
                           return_value=texts or {}):
        return rank.filter_candidates(candidates, set(), WINDOW_START, TODAY, None)


class TestPoolAdmission(unittest.TestCase):
    def test_hn_title_only_rejected(self):
        """HN 只有标题/积分、抓不到正文 → 不得入池（HN 热议分不代替正文质量）。"""
        c = make_candidate("https://example.com/2026/10/08/hot",
                           hn_points=900, hn_discussed_at="2026-10-08",
                           content="HN 热议 900 分。hot")
        kept = run_filter([c], texts={})
        self.assertEqual(kept, [])

    def test_old_article_new_hn_discussion_rejected(self):
        """旧文新热议：HN 今天热议但文章本身发布日期在窗口外 → 拒收。"""
        url = "https://example.com/2026/09/01/old-news"
        c = make_candidate(url, hn_points=500, hn_discussed_at="2026-10-08")
        kept = run_filter([c], texts={url: long_body()})
        self.assertEqual(kept, [])

    def test_hn_future_discussion_date_contradiction_rejected(self):
        """发布日期晚于 HN 热议日期 → 日期证据自相矛盾 → 拒收。"""
        url = "https://example.com/2026/10/08/a"
        c = make_candidate(url, hn_points=100, hn_discussed_at="2026-10-06")
        kept = run_filter([c], texts={url: long_body()})
        self.assertEqual(kept, [])

    def test_hn_with_real_body_and_date_kept(self):
        """HN 条目有真实正文 + 窗口内精确日期 → 可入池，且日期取文章自身证据。"""
        url = "https://example.com/2026/10/07/real"
        c = make_candidate(url, hn_points=500, hn_discussed_at="2026-10-08")
        kept = run_filter([c], texts={url: long_body()})
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["publish_date"], "2026-10-07")
        self.assertTrue(kept[0]["date_verified"])
        self.assertEqual(kept[0]["date_precision"], "day")
        self.assertEqual(kept[0]["date_source"], "url")
        self.assertTrue(kept[0]["date_evidence"])

    def test_month_only_date_rejected(self):
        """URL 只有 /2026/10/（月精度）→ 拒收。"""
        url = "https://example.com/2026/10/monthly-post"
        c = make_candidate(url)
        kept = run_filter([c], texts={url: long_body()})
        self.assertEqual(kept, [])

    def test_future_date_rejected(self):
        url = "https://example.com/2026/10/09/future"
        c = make_candidate(url)
        kept = run_filter([c], texts={url: long_body()})
        self.assertEqual(kept, [])

    def test_no_date_evidence_rejected(self):
        """正文够长但完全没有日期证据 → 未知日期禁止入池。"""
        url = "https://example.com/articles/no-date-here"
        c = make_candidate(url)
        kept = run_filter([c], texts={url: long_body()})
        self.assertEqual(kept, [])

    def test_short_body_fetch_fail_rejected(self):
        url = "https://example.com/2026/10/08/x"
        c = make_candidate(url, content="太短")
        kept = run_filter([c], texts={url: None})
        self.assertEqual(kept, [])

    def test_window_outside_rejected(self):
        """非 HN 来源窗口外（即使 ≤7 天）也不进推荐池。"""
        url = "https://example.com/2026/10/04/outside"
        c = make_candidate(url)
        kept = run_filter([c], texts={url: long_body()})
        self.assertEqual(kept, [])

    def test_malformed_backslash_url_rejected(self):
        c = make_candidate("https:\\\\evil.com\\2026\\10\\a", content=long_body())
        kept = run_filter([c])
        self.assertEqual(kept, [])

    def test_hn_discussion_page_rejected(self):
        c = make_candidate("https://news.ycombinator.com/item?id=12345",
                           hn_points=300, hn_discussed_at="2026-10-08",
                           content=long_body())
        kept = run_filter([c])
        self.assertEqual(kept, [])


if __name__ == "__main__":
    unittest.main()
