# -*- coding: utf-8 -*-
"""Regression tests for controlled HTML link rendering, safe protocols, and browser clicks."""
import json
import importlib.util
import os
import re
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT, SCRIPTS, long_body
from modules import controlled_html, html_report
import add_like
import verify_html_browser

BRIEF_20261016 = Path(ROOT) / "output/Daily-Brief-2026-10-16.md"


class ControlledHtmlLinkCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        refs = add_like.parse_brief_refs(str(BRIEF_20261016))
        brief_text = BRIEF_20261016.read_text()
        run_id = html_report.RUN_ID_RE.search(brief_text).group(1)
        titles = {url: title for title, url in refs.values()}
        urls = set(titles)
        urls.update(url.rstrip(".,;，。") for url in re.findall(r"https?://[^\s)>\]]+", brief_text))
        candidate = {
            "run_id": run_id,
            "window": {"start": "2026-10-15", "end": "2026-10-16"},
            "candidates": [
                {"title": titles.get(url, url), "url": url, "direction": "其他", "region": "other"}
                for url in sorted(urls)
            ],
            "source_leftovers": [],
            "horizon": [],
        }
        self.candidate_path = self.tmp_dir / "candidate.json"
        self.candidate_path.write_text(json.dumps(candidate, ensure_ascii=False))

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_brief_tldr_and_sections_render_proper_anchor_tags(self):
        """Test that the real 2026-10-16 brief renders clickable <a> tags in all sections."""
        rendered = controlled_html.render(str(BRIEF_20261016), str(self.candidate_path))

        # Check TLDR section
        self.assertIn('<section id="tldr"', rendered)
        self.assertIn('<a href="https://www.technologyreview.com/2026/10/15/1146091/pig-kidneys-that-were-frozen-for-a-year-have-been-brought-back-to-life/" class="ref-link" target="_blank" rel="noopener noreferrer">[1]</a>', rendered)
        self.assertIn('<a href="https://spectrum.ieee.org/china-rare-earth-minerals" class="ref-link" target="_blank" rel="noopener noreferrer">[2]</a>', rendered)

        # Check Horizon section
        self.assertIn('<section id="horizon"', rendered)
        self.assertIn('<a href="https://asia.nikkei.com/business/business-trends/china-s-canton-fair-showcases-tech-push-as-trade-frictions-persist" target="_blank" rel="noopener noreferrer">中国广交会展示科技推进步伐与贸易摩擦并存</a>', rendered)

        # Check Trends section
        self.assertIn('<section id="trends"', rendered)
        self.assertIn('<a href="https://research.meta.ai/blog/muse-spark-open" class="ref-link" target="_blank" rel="noopener noreferrer">[4]</a>', rendered)

        # Check Quick links section
        self.assertIn('<section id="quick"', rendered)
        self.assertIn('<a href="https://citizenlab.ca/research/how-discover-tech-turned-targeted-ads-into-a-pipeline-for-spyware/" class="ref-link" target="_blank" rel="noopener noreferrer">[7]</a>', rendered)

        # Check References section
        self.assertIn('<section id="references"', rendered)
        self.assertIn('<a href="https://www.sz.gov.cn/cn/xxgk/zfxxgj/zwdt/content/mpost_13021553.html" target="_blank" rel="noopener noreferrer">深圳市生态环境局与交警局发布通告：11月1日起全市全天全域禁鸣喇叭</a>', rendered)

        # Verify no unparsed markdown brackets remain in rendered HTML
        self.assertNotRegex(rendered, r'\[\[\d+\]\]\(https?:')
        self.assertNotRegex(rendered, r'\[[^\]]+\]\(https?:')

        # Verify no <pre> tags outside details evidence
        scan = html_report.scan_html_text = html_report._HTMLScan()
        scan.feed(rendered)
        pre_nodes = [n for n in scan.nodes if n["tag"] == "pre"]
        self.assertEqual(len(pre_nodes), rendered.count("<details>"))

    def test_dangerous_protocols_and_xss_are_sanitized(self):
        """Verify that malicious javascript/data/vbscript links and raw HTML are blocked."""
        malicious_md = """# Daily Brief 2026-10-14
<!-- run_id: testevil1 -->
**今日速览 (TLDR)**
- XSS attempt: <script>alert("pwned")</script> [[1]](javascript:alert('xss'))
- Image XSS: <img src="x" onerror="evil()"> [Click evil](javascript:steal())
- Data URI: [Data URI](data:text/html,<script>alert(1)</script>)
- VBScript: (vbscript:msgbox(1))
- File URI: [Passwd](file:///etc/passwd)
- Normal cite: [[1]](https://example.com/2026/10/13/good)

## 今日热门文章
### 科技 / 技术
#### 正常标题
正常深度摘要文本内容，保证长度足够用于一致性抽查，没有恶意代码在里面。[[1]](https://example.com/2026/10/13/good)

## 参考资料
[1] [好文章](https://example.com/2026/10/13/good)
"""
        cand_payload = {
            "run_id": "testevil1",
            "generated_at": "2026-10-14T01:00:00+08:00",
            "window": {"start": "2026-10-13", "end": "2026-10-14"},
            "candidates": [{
                "title": "好文章", "url": "https://example.com/2026/10/13/good",
                "direction": "科技", "region": "cn", "publish_date": "2026-10-13",
                "date_source": "url", "date_evidence": "test",
            }],
            "source_leftovers": [], "horizon": []
        }
        brief_p = self.tmp_dir / "brief.md"
        cand_p = self.tmp_dir / "cand.json"
        brief_p.write_text(malicious_md)
        cand_p.write_text(json.dumps(cand_payload))

        rendered = controlled_html.render(str(brief_p), str(cand_p))

        # Check that dangerous protocols NEVER appear inside href="..."
        self.assertNotIn('href="javascript:', rendered.lower())
        self.assertNotIn('href="data:', rendered.lower())
        self.assertNotIn('href="vbscript:', rendered.lower())
        self.assertNotIn('href="file:', rendered.lower())

        # Check that raw script/img tags are escaped in content
        body_content = rendered.split('</main>')[0]
        self.assertIn('&lt;script&gt;alert(&quot;pwned&quot;)&lt;/script&gt;', body_content)
        self.assertIn('&lt;img src=&quot;x&quot; onerror=&quot;evil()&quot;&gt;', body_content)

        # Verify HTML parser sees no executable script or img elements
        scan = html_report._HTMLScan()
        scan.feed(body_content)
        self.assertEqual([n for n in scan.nodes if n["tag"] == "script"], [])
        self.assertEqual([n for n in scan.nodes if n["tag"] == "img"], [])
        self.assertFalse(any("onerror" in n["attrs"] for n in scan.nodes))

        # Check that the safe link is properly rendered
        self.assertIn('<a href="https://example.com/2026/10/13/good" class="ref-link" target="_blank" rel="noopener noreferrer">[1]</a>', rendered)

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "Real-browser test requires the Hermes Python environment")
    def test_browser_verification_clicks_and_offline_receipt(self):
        """Test full browser verification with link click interception on 2026-10-16 readable revision."""
        target_html = self.tmp_dir / "test-readable.html"
        evidence_dir = self.tmp_dir / "browser_evidence"
        controlled_html.write(str(BRIEF_20261016), str(self.candidate_path), str(target_html))

        result = verify_html_browser.verify(str(target_html), str(BRIEF_20261016), str(self.candidate_path), str(evidence_dir))
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("mode"), "REAL_BROWSER_OFFLINE")
        self.assertEqual(result.get("networkRequests"), [])
        self.assertEqual(result.get("pageErrors"), [])

        # Check mobile widths
        self.assertEqual(result.get("mobile"), {"innerWidth": 390, "scrollWidth": 390, "bodyWidth": 390})
        self.assertEqual(result.get("desktop")["innerWidth"], 1280)
        self.assertEqual(result.get("desktop")["scrollWidth"], 1280)

        # Check clicked links evidence
        tested = result.get("testedLinks", [])
        self.assertGreaterEqual(len(tested), 3)
        tldr_click = next((l for l in tested if l["section"] == "tldr"), None)
        self.assertIsNotNone(tldr_click)
        self.assertEqual(tldr_click["url"], "https://www.technologyreview.com/2026/10/15/1146091/pig-kidneys-that-were-frozen-for-a-year-have-been-brought-back-to-life/")

        horizon_click = next((l for l in tested if l["section"] == "horizon"), None)
        self.assertIsNotNone(horizon_click)
        self.assertEqual(horizon_click["url"], "https://asia.nikkei.com/business/business-trends/china-s-canton-fair-showcases-tech-push-as-trade-frictions-persist")

        article_click = next((l for l in tested if l["section"] == "article"), None)
        self.assertIsNotNone(article_click)
        self.assertEqual(article_click["url"], "https://www.technologyreview.com/2026/10/15/1146091/pig-kidneys-that-were-frozen-for-a-year-have-been-brought-back-to-life/")

        # Verify receipt binds properly
        self.assertTrue(html_report.verify_browser_receipt(str(evidence_dir), str(BRIEF_20261016), str(target_html)))


if __name__ == "__main__":
    unittest.main()
