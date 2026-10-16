# -*- coding: utf-8 -*-
"""HTML 交付物校验：条目/URL/摘要/点赞映射一致性、拒收污染、离线资源、移动端静态要求。"""
import json
import os
import tempfile
import unittest

from tests.helpers import ROOT, SCRIPTS, long_body  # noqa: F401
from modules import html_report

RUN_ID = "testhtml1"
GOOD_URL = "https://example.com/2026/10/13/good"
SUMMARY = "这是一段关于轨道交通制动系统的深度摘要，包含具体数据和作者观点，长度足够用于一致性抽查。"
BAD_URL = "https://bad.example.com/2026/10/13/rejected"

MD_TMPL = """# Daily Brief 2026-10-14
<!-- run_id: {run_id} -->
**今日速览 (TLDR)**
- 一句话速览 [[1]]({url})

## 今日热门文章
### 科技 / 技术
#### {title}
{summary}[[1]]({url})

## 参考资料
[1] [{title}]({url})
"""

HTML_TMPL = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
{viewport}
<style>body{{font-size:19px;line-height:1.8}} a{{overflow-wrap:anywhere;word-break:break-word}}</style>
</head><body>
<!-- run_id: {run_id} -->
<p>手机端无法点击点赞链接，请在 Hermes 会话发文字指令「点赞 N」。</p>
<nav><a href="#deep">目录</a></nav>
<h1>Daily Brief 2026-10-14</h1>
<section id="deep"><h2>今日热门文章</h2>
<article data-direction="科技" data-ref="1" data-url="{url}"><h3 data-role="title">{title}</h3>
<p data-role="summary">{summary}</p>
<a data-role="citation" href="{url}">[1]原文链接</a>
<details><summary>日期证据</summary><p>url: {url}</p></details>
</article></section>
<section><h2>参考资料</h2><p>[1] <a href="{url}">{title}</a></p></section>
{like_block}
</body></html>"""

VIEWPORT = '<meta name="viewport" content="width=device-width,initial-scale=1">'


def cand_payload(rejected=False):
    p = {
        "run_id": RUN_ID,
        "generated_at": "2026-10-14T01:00:00+08:00",
        "window": {"start": "2026-10-13", "end": "2026-10-14"},
        "candidates": [{
            "title": "好文章", "url": GOOD_URL, "domain": "example.com",
            "direction": "科技", "is_preferred": False, "is_explore": False,
            "publish_date": "2026-10-13", "date_verified": True,
            "date_source": "url", "date_evidence": GOOD_URL,
            "date_precision": "day", "window_outside_days": 0,
            "word_count": 800, "content": long_body(),
        }],
        "source_leftovers": [], "horizon": [],
    }
    if rejected:
        p["rejected_candidates"] = [{
            "url": BAD_URL, "is_explore": False, "reason": "聚合页",
            "reviewed_by": "小g", "reviewed_at": "2026-10-14T02:00:00+08:00",
            "entry": {"url": BAD_URL}}]
    return p


class HtmlCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.brief = os.path.join(d, "Daily-Brief-2026-10-14.md")
        self.cand = os.path.join(d, "Daily-Brief-2026-10-14-candidates.json")
        self.html = os.path.join(d, "Daily-Brief-2026-10-14.html")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, md=None, cand=None, html=None, title="好文章"):
        with open(self.brief, "w", encoding="utf-8") as f:
            f.write(md if md is not None else MD_TMPL.format(
                run_id=RUN_ID, url=GOOD_URL, title=title, summary=SUMMARY))
        with open(self.cand, "w", encoding="utf-8") as f:
            json.dump(cand if cand is not None else cand_payload(), f,
                      ensure_ascii=False)
        import html as html_lib
        etitle = html_lib.escape(title)
        with open(self.html, "w", encoding="utf-8") as f:
            f.write(html if html is not None else HTML_TMPL.format(
                run_id=RUN_ID, url=GOOD_URL, title=etitle, summary=SUMMARY,
                viewport=VIEWPORT,
                like_block="%s\n%s" % (html_report.LIKE_START, html_report.LIKE_END)))

    def verify(self):
        return html_report.verify_html(self.html, self.brief, self.cand)

    def rebuild(self):
        import add_like
        return html_report.rebuild_like_section(
            self.html, add_like.parse_brief_refs(self.brief))

    def test_controlled_html_mixed_null_direction_matches_browser_contract(self):
        from pathlib import Path
        from modules import controlled_html
        from verify_html_browser import expected_articles
        payload = cand_payload()
        payload['candidates'][0]['direction'] = None
        second = dict(payload['candidates'][0], url=GOOD_URL+'-second',
                      title='另一篇', direction='科技')
        payload['candidates'].append(second)
        md = MD_TMPL.format(run_id=RUN_ID, url=GOOD_URL,
                            title='好文章', summary=SUMMARY)
        md = md.replace('## 参考资料',
                        '#### 另一篇\n'+SUMMARY+'[[2]]('+second['url']+')\n\n## 参考资料')
        md += '[2] [另一篇]('+second['url']+')\n'
        self.write(md=md, cand=payload)
        rendered = controlled_html.render(self.brief, self.cand)
        self.assertIn('data-direction="其他"', rendered)
        self.assertIn('<option>科技</option>', rendered)
        self.assertNotIn('data-direction="None"', rendered)
        articles, _ = expected_articles(self.brief, self.cand)
        self.assertEqual([a['direction'] for a in articles], ['其他', '科技'])

    def test_consistent_passes_and_rebuild_idempotent(self):
        self.write()
        changed, msg = self.rebuild()
        self.assertTrue(changed)
        ok, errors = self.verify()
        self.assertTrue(ok, errors)
        changed2, msg2 = self.rebuild()
        self.assertFalse(changed2)  # 幂等：同输入同输出
        ok, errors = self.verify()
        self.assertTrue(ok, errors)

    def test_missing_like_markers_rebuild_fails(self):
        self.write(html=HTML_TMPL.format(run_id=RUN_ID, url=GOOD_URL, title="好文章",
                                         summary=SUMMARY, viewport=VIEWPORT,
                                         like_block="<p>无标记位</p>"))
        changed, msg = self.rebuild()
        self.assertFalse(changed)
        self.assertIn("标记位", msg)

    def test_unrebuilt_like_section_fails_mapping(self):
        """小g自己写/留空点赞区 → 点赞映射与参考资料不一致 → FAIL。"""
        self.write()
        ok, errors = self.verify()
        self.assertFalse(ok)
        self.assertTrue(any("点赞映射" in e for e in errors))

    def test_missing_ref_url_fails(self):
        html = HTML_TMPL.format(run_id=RUN_ID, url=GOOD_URL, title="好文章",
                                summary=SUMMARY, viewport=VIEWPORT,
                                like_block="%s\n%s" % (html_report.LIKE_START,
                                                       html_report.LIKE_END))
        html = html.replace('<a data-role="citation" href="%s">[1]原文链接</a>' % GOOD_URL, "")
        html = html.replace('<p>[1] <a href="%s">好文章</a></p>' % GOOD_URL, "")
        self.write(html=html)
        self.rebuild()
        ok, errors = self.verify()
        self.assertFalse(ok)
        self.assertTrue(any("缺参考资料 [1] 的原文链接" in e for e in errors))

    def test_url_outside_pool_fails(self):
        html = HTML_TMPL.format(
            run_id=RUN_ID, url=GOOD_URL, title="好文章", summary=SUMMARY,
            viewport=VIEWPORT,
            like_block="%s\n%s" % (html_report.LIKE_START, html_report.LIKE_END))
        html += '<p><a href="https://sneaky.example.com/2026/10/13/x">未验收链接</a></p>'
        self.write(html=html)
        self.rebuild()
        ok, errors = self.verify()
        self.assertFalse(ok)
        self.assertTrue(any("不在候选池" in e for e in errors))

    def test_rejected_url_pollution_fails(self):
        """拒收 URL 出现在 HTML（含点赞区外任何位置）即 FAIL。"""
        html = HTML_TMPL.format(
            run_id=RUN_ID, url=GOOD_URL, title="好文章", summary=SUMMARY,
            viewport=VIEWPORT,
            like_block="%s\n%s" % (html_report.LIKE_START, html_report.LIKE_END))
        html += '<p><a href="%s">被拒收的</a></p>' % BAD_URL
        self.write(html=html, cand=cand_payload(rejected=True))
        self.rebuild()
        ok, errors = self.verify()
        self.assertFalse(ok)
        self.assertTrue(any("被拒收" in e for e in errors))

    def test_rejected_like_link_fails(self):
        """拒收 URL 的点赞链接：手工伪造点赞区（绕过脚本重建）→ FAIL。"""
        import urllib.parse
        q = urllib.parse.urlencode({"url": BAD_URL, "title": "坏"})
        like_block = ('<a href="%s?%s">👍</a>' % (html_report.LIKE_BASE, q))
        self.write(html=HTML_TMPL.format(run_id=RUN_ID, url=GOOD_URL, title="好文章",
                                         summary=SUMMARY, viewport=VIEWPORT,
                                         like_block=like_block),
                   cand=cand_payload(rejected=True))
        ok, errors = self.verify()
        self.assertFalse(ok)
        self.assertTrue(any("点赞" in e for e in errors))

    def test_external_resource_fails(self):
        html = HTML_TMPL.format(run_id=RUN_ID, url=GOOD_URL, title="好文章",
                                summary=SUMMARY, viewport=VIEWPORT,
                                like_block="%s\n%s" % (html_report.LIKE_START,
                                                       html_report.LIKE_END))
        html = html.replace("</head>", '<script src="https://cdn.example.com/x.js"></script></head>')
        self.write(html=html)
        self.rebuild()
        ok, errors = self.verify()
        self.assertFalse(ok)
        self.assertTrue(any("外部资源" in e for e in errors))

    def test_summary_mismatch_fails(self):
        self.write(html=HTML_TMPL.format(run_id=RUN_ID, url=GOOD_URL, title="好文章",
                                         summary="这段摘要与 MD 完全不一致，是小g自己编造的内容，必须被抓出来。",
                                         viewport=VIEWPORT,
                                         like_block="%s\n%s" % (html_report.LIKE_START,
                                                                html_report.LIKE_END)))
        self.rebuild()
        ok, errors = self.verify()
        self.assertFalse(ok)
        self.assertTrue(any("摘要与 canonical" in e for e in errors))

    def test_run_id_mismatch_fails(self):
        self.write(html=HTML_TMPL.format(run_id="otherrun", url=GOOD_URL, title="好文章",
                                         summary=SUMMARY, viewport=VIEWPORT,
                                         like_block="%s\n%s" % (html_report.LIKE_START,
                                                                html_report.LIKE_END)))
        self.rebuild()
        ok, errors = self.verify()
        self.assertFalse(ok)
        self.assertTrue(any("run_id" in e for e in errors))

    def test_mobile_static_requirements(self):
        base = HTML_TMPL.format(run_id=RUN_ID, url=GOOD_URL, title="好文章",
                                summary=SUMMARY, viewport=VIEWPORT,
                                like_block="%s\n%s" % (html_report.LIKE_START,
                                                       html_report.LIKE_END))
        # 缺 viewport
        self.write(html=base.replace(VIEWPORT, ""))
        self.rebuild()
        ok, errors = self.verify()
        self.assertFalse(ok)
        self.assertTrue(any("viewport" in e for e in errors))
        # 缺断行样式
        self.write(html=base.replace("overflow-wrap:anywhere;word-break:break-word", ""))
        self.rebuild()
        ok, errors = self.verify()
        self.assertFalse(ok)
        self.assertTrue(any("断行" in e for e in errors))
        # 缺手机文字点赞提示（不重建点赞区时检查原文提示）
        self.write(html=base.replace("手机端无法点击点赞链接，请在 Hermes 会话发文字指令「点赞 N」。", ""))
        ok, errors = self.verify()
        self.assertFalse(ok)
        self.assertTrue(any("手机端文字点赞" in e for e in errors))

    def test_escaped_title_passes(self):
        """标题含 & < >：HTML 转义后解析比对仍一致。"""
        self.write(title="A&B <测试>")
        self.rebuild()
        ok, errors = self.verify()
        self.assertTrue(ok, errors)


if __name__ == "__main__":
    unittest.main()
