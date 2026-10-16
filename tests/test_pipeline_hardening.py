"""Offline counterexamples: no search, production files, or listening services."""
import copy
import hashlib
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, SCRIPTS, long_body
from tests.test_agent_lane import LaneCase, good_item, ok_result, review, fake_spawn_factory, GOOD_URL, make_task
from tests.test_html_report import HtmlCase, SUMMARY, BAD_URL
from tests.test_pipeline import RunnerCase, PrepareCase
from modules import agent_lane, filter as fm, run_store
import finalize_brief
import run_pipeline
import prepare_brief


class ReviewCounterexamples(LaneCase):
    def test_exact_boolean_coverage_counterexamples(self):
        good = review('pass')['items'][0]
        cases = [[], [dict(good, **{'pass': 'false'})], [dict(good, **{'pass': 1})],
                 [dict(good, **{'pass': None})], [good, good],
                 [dict(good, url='https://other.example/article')],
                 [good, dict(good, url='https://other.example/article')]]
        for rows in cases:
            with self.subTest(rows=rows):
                oc = self.run_lane(fake_spawn_factory([ok_result()], [review('pass', rows)]), max_rounds=1)
                self.assertEqual(oc['status'], 'failed')
                self.assertEqual(oc['items'], [])

    def test_duplicate_author_rejected(self):
        oc = self.run_lane(fake_spawn_factory([ok_result([good_item(), good_item()])], [review('pass')]), max_rounds=1)
        self.assertEqual(oc['status'], 'failed')

    def test_missing_or_same_session_id_rejected(self):
        base = fake_spawn_factory([ok_result()], [review('pass')])
        for text in ('done', 'kimi -r session_same_123'):
            def spawn(cmd, cwd, log_path, timeout):
                rc = base(cmd, cwd, log_path, timeout)
                Path(log_path).write_text(text)
                return rc
            self.assertEqual(self.run_lane(spawn, max_rounds=1)['status'], 'failed')

    def test_source_hash_or_snapshot_missing_rejected(self):
        bad = review('pass')
        bad['items'][0]['content_sha256'] = 'invented'
        self.assertEqual(self.run_lane(fake_spawn_factory([ok_result()], [bad]), max_rounds=1)['status'], 'failed')
        with mock.patch.object(agent_lane, 'fetch_texts_parallel', return_value={}):
            self.assertEqual(self.run_lane(fake_spawn_factory([ok_result()], [review('pass')]), max_rounds=1)['status'], 'failed')


    def test_true_metadata_reverified_and_survives_ranking(self):
        url = 'https://example.com/articles/no-url-date'
        metadata = {'json_ld': {'datePublished': '2026-10-13T08:00:00Z'}}
        item = good_item(url=url, metadata=metadata, date_source='json_ld',
                         date_evidence='JSON-LD datePublished=2026-10-13T08:00:00Z')
        verdict = copy.deepcopy(review('pass')['items'][0])
        verdict.update(url=url, date_evidence=item['date_evidence'])
        with mock.patch.object(agent_lane, 'fetch_texts_parallel', return_value={url: {'text': long_body(), 'metadata': metadata}}):
            oc = self.run_lane(fake_spawn_factory([ok_result([item])], [review('pass', [verdict])]), max_rounds=1)
        self.assertEqual(oc['status'], 'ok', oc)
        from modules import rank
        from datetime import date
        kept = rank.filter_candidates(oc['items'], set(), date(2026,10,13), date(2026,10,14), None)
        self.assertEqual(kept[0]['date_source'], 'json_ld')
        # Self-reported metadata cannot substitute a fetched page snapshot.
        with mock.patch.object(agent_lane, 'fetch_texts_parallel', return_value={url: {'text': long_body(), 'metadata': {}}}):
            oc = self.run_lane(fake_spawn_factory([ok_result([item])], [review('pass', [verdict])]), max_rounds=1)
        self.assertEqual(oc['status'], 'failed')

    def test_deadline_stops_before_spawn(self):
        spawn = fake_spawn_factory([ok_result()], [review('pass')])
        oc = self.run_lane(spawn, deadline=time.monotonic() - 1)
        self.assertEqual(oc['status'], 'failed')
        self.assertEqual(spawn.calls, [])

    def test_real_subprocess_uses_remaining_pipeline_deadline(self):
        # Local shell only: no CLI credentials or network.
        with mock.patch.object(agent_lane, 'cli_command', return_value=['/bin/sh', '-c', 'sleep 30']):
            start = time.monotonic()
            oc = agent_lane.run_lane(self.task, self.tmp.name, timeout=1500, deadline=start + 0.15)
        self.assertEqual(oc['status'], 'failed')
        self.assertLess(time.monotonic() - start, 1)


class HermesReviewAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.task = make_task(self.tmp.name)
        self.fetch = mock.patch.object(agent_lane, 'fetch_texts_parallel', return_value={
            GOOD_URL: {'text': long_body(), 'metadata': {}}})
        self.fetch.start()

    def tearDown(self):
        self.fetch.stop()
        self.tmp.cleanup()

    def test_finalize_requires_hermes_review_and_checks_author_evidence(self):
        oc = agent_lane.run_lane_for_hermes(
            self.task, self.tmp.name, spawn=fake_spawn_factory([ok_result()], []))
        self.assertEqual(oc['status'], 'ok')
        oc['lane_dir'] = self.tmp.name
        payload = {
            'run_id': self.task['run_id'],
            'pipeline': {'mode': 'agent', 'semantic_review': 'pending_hermes'},
            'agent_review': {'cn': oc},
            'candidates': oc['items'], 'source_leftovers': [],
        }
        with self.assertRaisesRegex(ValueError, '尚未完成'):
            run_store.verify_agent_evidence(payload)
        payload['pipeline']['semantic_review'] = 'complete'
        oc['semantic_review'] = {
            'status': 'complete', 'reviewer': 'Hermes',
            'reviewed_at': '2026-10-16T09:00:00+08:00', 'findings': [],
        }
        run_store.verify_agent_evidence(payload)


class HTMLCounterexamples(HtmlCase):
    def mutate(self, fn):
        self.write()
        self.rebuild()
        path = Path(self.html)
        path.write_text(fn(path.read_text()))
        ok, errors = self.verify()
        self.assertFalse(ok, errors)
        return errors

    def test_full_summary_not_prefix_and_tail_fabrication(self):
        self.mutate(lambda t: t.replace(SUMMARY, SUMMARY[:40] + '模型编造尾部'))
        self.mutate(lambda t: t.replace(SUMMARY, SUMMARY + '额外编造尾部'))

    def test_hidden_missing_visible_body(self):
        for attr in ('hidden', 'aria-hidden="true"', 'style="display:none"'):
            with self.subTest(attr=attr):
                self.mutate(lambda t: t.replace('<p data-role="summary">', '<p data-role="summary" ' + attr + '>'))
        self.mutate(lambda t: t.replace('<style>', '<style>.secret{display:none}').replace('data-role="summary"', 'data-role="summary" class="secret"'))

    def test_arbitrary_hiding_css_and_ancestor_bypasses(self):
        for style in ('opacity:0.0', 'opacity:0.000!important', 'position:absolute;left:-10000px',
                      'color:transparent', 'transform:scale(0)', 'height:0;overflow:hidden',
                      'filter:opacity(0)', 'content-visibility:hidden', 'text-indent:-99999px'):
            with self.subTest(style=style):
                self.mutate(lambda t: t.replace('<p data-role="summary">', '<p data-role="summary" style="'+style+'">'))
                self.mutate(lambda t: t.replace('<body>', '<body style="'+style+'">'))
        self.mutate(lambda t: t.replace('<style>', '<style>.ancestor p{color:transparent}').replace('<body>', '<body class="ancestor">'))
        self.mutate(lambda t: t.replace('<style>', '<style>@media(max-width:400px){article{opacity:0.0}}'))

    def test_cross_article_binding_and_number(self):
        self.mutate(lambda t: t.replace('data-url="', 'data-url="https://other.example/'))
        self.mutate(lambda t: t.replace('data-ref="1"', 'data-ref="999"'))
        self.mutate(lambda t: t.replace('[1]原文链接', '[999]原文链接'))
        self.mutate(lambda t: t.replace('<h3 data-role="title">好文章', '<h3 data-role="title">串篇标题'))

    def test_like_number_duplicate_and_hidden(self):
        self.mutate(lambda t: t.replace('<li>[1]', '<li>[999]'))
        self.mutate(lambda t: t.replace('<li>[1]', '<li hidden>[1]'))
        self.mutate(lambda t: t.replace('</ul></section>', '<li>[1]虚构</li></ul></section>'))

    def test_external_css_protocol_relative_and_srcset(self):
        for resource in ('<style>@import "https://cdn.example/x.css";</style>',
                         '<style>@import "//cdn.example/x.css";</style>',
                         '<script src="//cdn.example/x.js"></script>',
                         '<img srcset="//cdn.example/x.png 1x">',
                         '<style>body{background:url(//cdn.example/x.png)}</style>',
                         '<script src="local.js"></script>'):
            with self.subTest(resource=resource):
                errors = self.mutate(lambda t: t.replace('</head>', resource + '</head>'))
                self.assertTrue(any('外部资源' in e for e in errors))

    def test_rejected_plain_text_and_encoded_comment(self):
        self.write(cand=__import__('tests.test_html_report', fromlist=['cand_payload']).cand_payload(rejected=True))
        self.rebuild()
        path = Path(self.html)
        base = path.read_text()
        for suffix in ('<p>' + BAD_URL + '</p>', '<!-- ' + __import__('urllib.parse', fromlist=['quote']).quote(BAD_URL, safe='') + ' -->'):
            path.write_text(base + suffix)
            ok, errors = self.verify()
            self.assertFalse(ok)
            self.assertTrue(any('被拒收' in e for e in errors))


class RunnerCounterexamples(RunnerCase):
    def test_failed_run_does_not_change_date_alias(self):
        alias = self.cand_dir / 'Daily-Brief-2026-10-14-candidates.json'
        alias.write_text('{"old_published_evidence":true}')
        before = alias.read_bytes()
        with mock.patch.object(run_pipeline.run_agy, 'run', side_effect=self.fake_agy(fail_writer=True)):
            self.assertEqual(run_pipeline.run(str(self.ctx_file)), 1)
        self.assertEqual(alias.read_bytes(), before)

    def test_expired_pipeline_skips_network_and_cli(self):
        with mock.patch.object(run_pipeline, '_collect_deterministic') as collect, mock.patch.object(run_pipeline, '_run_lanes') as lanes:
            self.assertEqual(run_pipeline.run(str(self.ctx_file), pipeline_timeout=0), 1)
            collect.assert_not_called()
            lanes.assert_not_called()

    def test_body_budget_not_expired_deterministic_budget(self):
        observed = []
        original = run_pipeline.build_candidates_payload
        def build(*a, **kw):
            observed.append(kw['budget'])
            self.assertFalse(kw['budget'].expired())
            return original(*a, **kw)
        with mock.patch.object(run_pipeline, 'build_candidates_payload', side_effect=build), mock.patch.object(run_pipeline.run_agy, 'run', side_effect=self.fake_agy()), mock.patch.object(run_pipeline, 'DETERMINISTIC_BUDGET_S', 0):
            self.assertEqual(run_pipeline.run(str(self.ctx_file)), 0)
        self.assertEqual(len(observed), 1)

    def test_archive_and_candidate_hash_tampering_rejected(self):
        with mock.patch.object(run_pipeline.run_agy, 'run', side_effect=self.fake_agy()):
            self.assertEqual(run_pipeline.run(str(self.ctx_file)), 0)
        cand, brief = self.context['batch']['candidates'], self.context['batch']['brief']
        final = finalize_brief.finalize(cand, brief)
        cache = self.cand_dir / Path(cand).name
        payload = json.loads(cache.read_text())
        run_store.verify_agent_evidence(payload)  # Paths remain valid after working dirs deleted.
        archived = self.out_dir / 'final' / self.context['run_id']
        self.assertTrue((archived / 'published.json').is_file())
        receipt = json.loads((archived / 'published.json').read_text())
        self.assertTrue(Path(receipt['validation_receipt']).is_file())
        cache.write_text(cache.read_text() + ' ')
        with self.assertRaises(ValueError):
            finalize_brief.finalize(cand, brief)

    def test_missing_review_evidence_cannot_publish(self):
        with mock.patch.object(run_pipeline.run_agy, 'run', side_effect=self.fake_agy()):
            self.assertEqual(run_pipeline.run(str(self.ctx_file)), 0)
        Path(self.context['lanes']['cn']['lane_dir'], 'source_evidence_r1.json').unlink()
        with self.assertRaises((ValueError, OSError)):
            finalize_brief.finalize(self.context['batch']['candidates'], self.context['batch']['brief'])
        self.assertFalse((self.out_dir / 'Daily-Brief-2026-10-14.md').exists())


class SharedQueries(PrepareCase):
    def test_augmented_queries_on_both_lanes(self):
        from datetime import datetime
        extra = [{'query': '探索健康', 'direction': '健康', 'explore': True},
                 {'query': '偏好科技', 'direction': '科技', 'preferred': True}]
        with mock.patch.object(prepare_brief.rank, 'build_extra_queries', return_value=(extra, '科技', '健康')):
            context, _ = prepare_brief.prepare(datetime(2026,10,14,8,tzinfo=prepare_brief.TZ), self.cand_dir, self.out_dir)
        for lane in ('cn', 'intl'):
            self.assertEqual(context['lanes'][lane]['queries'][-2:], extra)


if __name__ == '__main__':
    unittest.main()

class BrowserReceiptCounterexamples(unittest.TestCase):
    def test_absence_and_stale_hash_refused(self):
        from modules import html_report
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);md=p/'test.md';ht=p/'test.html';md.write_text('fixture');ht.write_text('fixture')
            with self.assertRaises(ValueError): html_report.verify_browser_receipt(p,md,ht)
            (p/'receipt.json').write_text(json.dumps({'ok':True,'mode':'REAL_BROWSER_OFFLINE','html_sha256':'stale'}))
            with self.assertRaises(ValueError): html_report.verify_browser_receipt(p,md,ht)
