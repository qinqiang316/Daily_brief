# -*- coding: utf-8 -*-
"""日期证据：metadata/署名发布时间优先、正文首日期不得冒充、合法日历校验、
HTML 清洗前保留 datePublished/JSON-LD、正文抓取上限兼容。"""
import inspect
import unittest
from unittest import mock

from tests.helpers import ROOT  # noqa: F401
from modules import filter as filter_mod
from modules import retrieve


class TestDateEvidencePriority(unittest.TestCase):
    def test_jsonld_beats_url_date(self):
        """JSON-LD datePublished 是最强证据，压过 URL 日期。"""
        ev = filter_mod.extract_date_evidence({
            "url": "https://example.com/2026/10/01/a",
            "title": "t",
            "content": "x",
            "metadata": {"json_ld": {"datePublished": "2026-10-07T09:00:00+08:00"},
                         "meta": {}},
        })
        self.assertEqual(ev["publish_date"], "2026-10-07")
        self.assertEqual(ev["date_source"], "json_ld")
        self.assertEqual(ev["precision"], "day")
        self.assertIn("datePublished", ev["evidence"])

    def test_meta_beats_url_date(self):
        ev = filter_mod.extract_date_evidence({
            "url": "https://example.com/2026/10/01/a", "title": "t", "content": "x",
            "metadata": {"json_ld": {},
                         "meta": {"article:published_time": "2026-10-06T00:00:00Z"}},
        })
        self.assertEqual(ev["publish_date"], "2026-10-06")
        self.assertEqual(ev["date_source"], "html_meta")

    def test_byline_marker_date_accepted(self):
        """明确署名发布时间（标记词附近）算证据。"""
        ev = filter_mod.extract_date_evidence({
            "url": "https://example.com/articles/slug", "title": "t",
            "content": "本报讯 发布时间：2026-10-05 作者：李四 正文……",
        })
        self.assertEqual(ev["publish_date"], "2026-10-05")
        self.assertEqual(ev["date_source"], "byline")

    def test_byline_english_published_on(self):
        ev = filter_mod.extract_date_evidence({
            "url": "https://example.com/articles/slug", "title": "t",
            "content": "Published on October 6, 2026 by Jane. Body text here.",
        })
        self.assertEqual(ev["publish_date"], "2026-10-06")
        self.assertEqual(ev["date_source"], "byline")

    def test_plain_body_first_date_not_publish_evidence(self):
        """正文第一处日期（事件时间）不得冒充发布时间。"""
        ev = filter_mod.extract_date_evidence({
            "url": "https://example.com/articles/slug", "title": "t",
            "content": "事故发生于2025年3月1日，调查持续至今。" + "正文。" * 50,
        })
        self.assertIsNone(ev["publish_date"])
        self.assertIsNone(ev["date_source"])

    def test_url_month_loses_to_title_day(self):
        """URL 月精度不得抢在标题精确日前。"""
        ev = filter_mod.extract_date_evidence({
            "url": "https://example.com/2026/10/monthly",
            "title": "10/05/2026 某深度报道",
            "content": "x",
        })
        self.assertEqual(ev["publish_date"], "2026-10-05")
        self.assertEqual(ev["date_source"], "title")
        self.assertEqual(ev["precision"], "day")

    def test_url_month_is_month_precision(self):
        ev = filter_mod.extract_date_evidence({
            "url": "https://example.com/2026/10/monthly", "title": "t", "content": "x",
        })
        self.assertEqual(ev["precision"], "month")

    def test_invalid_calendar_date_rejected(self):
        """2026-02-30 不是合法日历日期 → 日精度证据拒收（最多回落到月精度线索）。"""
        ev = filter_mod.extract_date_evidence({
            "url": "https://example.com/2026/02/30/x", "title": "t", "content": "x",
        })
        self.assertNotEqual(ev["precision"], "day")
        self.assertNotEqual(ev["publish_date"], "2026-02-30")

    def test_time_datetime_tag(self):
        """<time datetime> 结构化标签算证据（实测 MIT News 只有这一种）。"""
        ev = filter_mod.extract_date_evidence({
            "url": "https://example.com/articles/slug", "title": "t", "content": "x",
            "metadata": {"json_ld": {}, "meta": {},
                         "time": ["2026-10-07T20:55:00Z"]},
        })
        self.assertEqual(ev["publish_date"], "2026-10-07")
        self.assertEqual(ev["date_source"], "html_time")

    def test_compat_wrapper(self):
        pub, verified, month_only = filter_mod.extract_publish_date({
            "url": "https://example.com/2026/10/07/a", "title": "t", "content": "x"})
        self.assertEqual((pub, verified, month_only), ("2026-10-07", True, False))
        pub, verified, month_only = filter_mod.extract_publish_date({
            "url": "https://example.com/2026/10/a", "title": "t", "content": "x"})
        self.assertEqual((pub, verified, month_only), ("2026-10-01", True, True))


HTML_SAMPLE = """<html><head>
<meta property="article:published_time" content="2026-10-06T12:00:00Z">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"NewsArticle","datePublished":"2026-10-07T08:30:00+08:00"}
</script>
</head><body><article>正文内容 发布时间：2026-10-01 </article></body></html>"""


class TestHtmlMetadataPreserved(unittest.TestCase):
    def test_metadata_extracted_before_cleaning(self):
        """HTML 清洗丢掉 meta/JSON-LD 之前先提取 metadata。"""
        md = retrieve.extract_html_metadata(HTML_SAMPLE)
        self.assertEqual(md["json_ld"].get("datePublished"), "2026-10-07T08:30:00+08:00")
        self.assertEqual(md["meta"].get("article:published_time"), "2026-10-06T12:00:00Z")

    def test_fetch_article_structured(self):
        """fetch_article 返回正文+metadata；fetch_article_text 兼容返回字符串。"""
        body = "正文内容。" * 60
        html = HTML_SAMPLE.replace("<article>正文内容 发布时间：2026-10-01 </article>",
                                   "<article>%s</article>" % body)

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return html.encode("utf-8")

        with mock.patch("modules.retrieve.urllib.request.urlopen", return_value=FakeResp()):
            art = retrieve.fetch_article("https://example.com/x")
            text = retrieve.fetch_article_text("https://example.com/x")
        self.assertIsInstance(text, str)
        self.assertEqual(art["text"], text)
        ev = filter_mod.extract_date_evidence(
            {"url": "https://example.com/x", "title": "t",
             "content": art["text"], "metadata": art["metadata"]})
        self.assertEqual(ev["publish_date"], "2026-10-07")  # JSON-LD 最强
        self.assertEqual(ev["date_source"], "json_ld")

    def test_script_release_time_is_day_evidence(self):
        """晚点文章页的 var release_time='YYYY/MM/DD' 是日精度原文日期，不是列表上的「今天」。"""
        html = "<html><head><script>var release_time='2026/10/09';</script></head><body>正文</body></html>"
        md = retrieve.extract_html_metadata(html)
        self.assertEqual(md["meta"].get("release_time"), "2026/10/09")
        ev = filter_mod.extract_date_evidence({
            "url": "https://www.latepost.com/news/dj_detail?id=3753",
            "title": "踏板", "metadata": md})
        self.assertEqual(ev["publish_date"], "2026-10-09")
        self.assertEqual(ev["date_source"], "html_meta")
        self.assertEqual(ev["precision"], "day")

    def test_default_limit_covers_500_english_words(self):
        """默认抓取上限须容下英文 500 词（≈3000+ 字符）。"""
        sig = inspect.signature(retrieve.fetch_article_text)
        self.assertGreaterEqual(sig.parameters["limit"].default, 6000)
        self.assertGreaterEqual(filter_mod.CONTENT_LIMIT, 6000)


if __name__ == "__main__":
    unittest.main()
