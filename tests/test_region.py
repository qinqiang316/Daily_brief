"""Collection provenance and independent browser expectations; no web or live CLI."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, SCRIPTS
from tests.test_html_report import MD_TMPL, RUN_ID, GOOD_URL, SUMMARY, cand_payload
from modules import controlled_html, region
import run_pipeline
import verify_html_browser


class RegionCase(unittest.TestCase):
    def test_deterministic_sources_use_configuration_only(self):
        context = {'window': {'start': '2026-10-13', 'end': '2026-10-14'}}
        promoted = [{'source_key': 'horizon:latepost', 'source_label': '晚点', 'direction': '商业'},
                    {'source_key': 'horizon:mit_tr', 'source_label': 'MIT', 'direction': '科技'},
                    {'source_key': 'horizon:unknown', 'source_label': '中文来源', 'direction': '科技'}]
        with mock.patch.object(run_pipeline.filter_mod, 'load_dedup', return_value=set()), \
             mock.patch.object(run_pipeline.retrieve, 'fetch_hn', return_value=[{'title': '中文', 'region': 'cn'}]), \
             mock.patch.object(run_pipeline.retrieve, 'fetch_telegram_channels', return_value=[{'telegram_channel': 'unknown', 'region': 'intl'}]), \
             mock.patch.object(run_pipeline.retrieve, 'fetch_agent_case_share', return_value=[{'region': 'cn'}]), \
             mock.patch.object(run_pipeline.horizon, 'fetch_horizon', return_value=(promoted, [], [])):
            items, *_ = run_pipeline._collect_deterministic(context, None)
        self.assertEqual([c['region'] for c in items], ['intl', 'other', 'other', 'cn', 'intl', 'other'])
        self.assertTrue(all(c['region_provenance']['kind'] == 'source_config' for c in items))
        self.assertEqual(items[3]['region_provenance']['configured_region'], '中国')

    def test_unconfigured_region_never_uses_language(self):
        for value in (None, '', 'Chinese', 'English', '中文', 'unknown'):
            item = {'title': '国内 China'}
            region.bind_source(item, 'unknown', value)
            self.assertEqual(item['region'], 'other')

    def test_html_and_browser_expectations_bind_to_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            brief, candidate = Path(d)/'brief.md', Path(d)/'candidate.json'
            brief.write_text(MD_TMPL.format(run_id=RUN_ID, url=GOOD_URL, title='好文章', summary=SUMMARY))
            payload = cand_payload()
            for lane in ('cn', 'intl'):
                region.bind_lane(payload['candidates'][0], lane)
                candidate.write_text(json.dumps(payload))
                output = controlled_html.render(brief, candidate)
                self.assertIn('data-region="'+lane+'"', output)
                articles, required = verify_html_browser.expected_articles(brief, candidate)
                self.assertEqual(articles[0]['region'], lane)
                self.assertEqual(required, [lane])
            del payload['candidates'][0]['region']
            candidate.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, 'provenance lost'):
                verify_html_browser.expected_articles(brief, candidate)
            brief.write_text('# no canonical articles')
            with self.assertRaisesRegex(ValueError, 'nonempty'):
                verify_html_browser.expected_articles(brief, candidate)
