#!/usr/bin/env python3
"""Reproducible isolated E2E. ALL source/CLI outputs are labelled TEST FIXTURES.
No network, CLI invocation, or HTTP listener. Artifacts persist under runtime scratch.
Run: python3 tests/offline_pipeline_replay.py
"""
import hashlib
import html
import json
import re
import socket
import shutil
import sys
import tempfile
import urllib.parse
import uuid
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from tests.helpers import long_body
from modules import agent_lane, filter as fm, html_report, lane_seeds, rank, run_store, window
import add_like
import brief_record
import collect_brief
import finalize_brief
import likes
import prepare_brief
import run_pipeline
import validate_brief
import verify_html_browser

SCRATCH = Path('/Users/qqiang/.hermes/cache/scratch')


def replay(takeover=False):
    base = Path(tempfile.mkdtemp(prefix='dailybrief-offline-e2e-', dir=SCRATCH)).resolve()
    cand_dir, output = base / '_candidates', base / 'output'
    data = base / 'data'
    data.mkdir()
    dedup, liked, records = data / 'dedup.json', data / 'likes.json', data / 'records.json'
    dedup.write_text('[]')
    urls = {'cn': 'https://cn.example.com/articles/fixture-domestic',
            'intl': 'https://intl.example.com/articles/fixture-international'}
    sources = {url: {'text': long_body(), 'metadata': {'json_ld': {'datePublished': '2026-10-13'}}} for url in urls.values()}
    (base / 'TEST_FIXTURE_NOTICE.md').write_text('本回放所有文章、会话、摘要均为明确测试夹具，不是新闻，不代表联网CLI成功。禁止作为生产简报交付。\n')
    (base / 'source-fixtures.json').write_text(json.dumps(sources, ensure_ascii=False, indent=2))
    real_spawn = agent_lane.run_cli_session
    state = {'context': None, 'writer_failure': False}

    def lane_adapter(cmd, cwd, log_path, timeout, env=None):
        """Injected offline CLI adapter; distinct fixture IDs explicitly marked as non-real."""
        task = json.loads((Path(cwd) / 'task.json').read_text())
        lane = task['lane']
        is_hermes = '--usage-file' in cmd
        sid = str(uuid.uuid4())
        if is_hermes:
            from tests.test_executor_takeover import usage
            usage(log_path, session=sid)
        Path(log_path).write_text('OFFLINE TEST FIXTURE - NOT A REAL CLI SESSION\nconversation ' + sid)
        url = urls[lane]
        source = sources[url]
        evidence = fm.extract_date_evidence({'url': url, 'content': source['text'], 'metadata': source['metadata']})
        direction = task['preference']['pref_dir'] if lane == 'cn' else task['preference']['explore_dir']
        result = {'run_id': task['run_id'], 'lane': lane, 'fixture': True, 'items': [{
            'title': '国内科技测试夹具' if lane == 'cn' else '国外探索测试夹具',
            'url': url, 'content': source['text'], 'metadata': source['metadata'],
            'publish_date': evidence['publish_date'], 'date_source': evidence['date_source'],
            'date_evidence': evidence['evidence'], 'date_precision': 'day',
            'direction': direction, 'is_preferred': lane == 'cn', 'is_explore': lane == 'intl',
            'source_key': 'fixture:' + lane, 'source_label': 'OFFLINE TEST FIXTURE ' + lane}]}
        path = task['result_file']
        Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    def writer_adapter(candidates, prompt, timeout=1200):
        context = state['context']
        if state['writer_failure']:
            return 7
        payload = json.loads(Path(candidates).read_text())
        entries = payload['candidates']
        prompt_text = Path(prompt).read_text()
        summary = '这是一段明确标注的离线测试摘要，只验证编号、标题和完整文本的一致性，不代表任何真实新闻事实。'
        canonical = '# Daily Brief-2026-10-14\n<!-- run_id: ' + context['run_id'] + ' -->\n\n**今日速览 (TLDR)**\n'
        for n, c in enumerate(entries, 1):
            canonical += '- 离线测试夹具 [[%d]](%s)\n' % (n, c['url'])
        canonical += '\n## 今日热门文章\n'
        for n, c in enumerate(entries, 1):
            title = c['title'] + ('（今日探索）' if c['is_explore'] else '（偏好命中）')
            canonical += '\n### %s\n#### %s\n%s [[%d]](%s)\n' % (c['direction'], title, summary, n, c['url'])
        canonical += '\n## 参考资料\n'
        for n, c in enumerate(entries, 1):
            canonical += '[%d] [%s](%s)\n' % (n, c['title'], c['url'])
        canonical += '\n## 测试声明\n全部为离线测试夹具，不是新闻。\n'
        Path(context['batch']['brief']).write_text(canonical)
        # Preserve the fact that the Markdown writer was an adapter, not a real CLI call.
        d = Path(candidates).parent / 'agy'
        d.mkdir(exist_ok=True)
        token = uuid.uuid4().hex
        (d / (token + '.txt')).write_text(prompt_text)
        (d / (token + '.log')).write_text('OFFLINE TEST FIXTURE WRITER ADAPTER\n')
        return 0

    def cli_adapter(cmd, cwd, log_path, timeout, env=None):
        hermes = '--usage-file' in cmd
        if takeover and not hermes:
            # Real executable-not-found failure; inference itself remains fully mocked.
            return real_spawn(['/DAILYBRIEF-TEST-NONEXISTENT-CLI'], cwd, log_path, timeout)
        if '健康检查' in cmd[-1]:
            Path(log_path).write_text('OFFLINE TEST FIXTURE\nDAILYBRIEF_OK\n')
            return 0
        if Path(cwd).name in ('cn','intl'):
            return lane_adapter(cmd, cwd, log_path, timeout, env)
        Path(log_path).write_text('OFFLINE MOCK INFERENCE WRITER, NOT LIVE CLI\n' + str(uuid.uuid4()))
        if hermes:
            from tests.test_executor_takeover import usage
            usage(log_path, session='session_fixture_'+uuid.uuid4().hex)
        prompt = Path(state['context']['batch']['candidates']).parent / ('html_prompt.txt' if 'HTML 报告制作' in cmd[-1] else 'writer_prompt.txt')
        return writer_adapter(state['context']['batch']['candidates'], str(prompt), timeout)

    def seed_adapter(task, budget):
        lane = task['lane']
        url = urls[lane]
        source = sources[url]
        evidence = fm.extract_date_evidence({'url': url, 'content': source['text'], 'metadata': source['metadata']})
        item = {
            'title': '国内科技测试夹具' if lane == 'cn' else '国外探索测试夹具',
            'url': url, 'content': source['text'], 'metadata': source['metadata'],
            'publish_date': evidence['publish_date'], 'date_source': evidence['date_source'],
            'date_evidence': evidence['evidence'], 'date_precision': 'day',
            'direction': task['preference']['pref_dir'] if lane == 'cn' else task['preference']['explore_dir'],
            'is_preferred': lane == 'cn', 'is_explore': lane == 'intl',
            'source_key': 'fixture:' + lane, 'source_label': 'OFFLINE TEST FIXTURE ' + lane,
        }
        directory = Path(task['lane_dir'])
        snapshot = directory / 'seed_sources.json'
        snapshot.write_text(json.dumps({url: source}, ensure_ascii=False))
        seed = directory / 'seed_items.json'
        seed.write_text(json.dumps({'run_id': task['run_id'], 'lane': lane,
                                    'items': [item], 'stats': {'fixture': 1}}, ensure_ascii=False))
        return str(seed), str(snapshot), hashlib.sha256(snapshot.read_bytes()).hexdigest()

    with ExitStack() as stack:
        patches = [(collect_brief, 'CAND_DIR', str(cand_dir)), (collect_brief, 'OUTPUT_DIR', str(output)),
                   (collect_brief, 'LOCK_FILE', str(data / 'pipeline.lock')),
                   (collect_brief, 'LIKES_FILE', str(liked)), (rank, 'LIKES_FILE', str(liked)),
                   (likes, 'LIKES_FILE', str(liked)), (likes, 'OUTPUT_DIR', str(output)),
                   (fm, 'DEDUP_FILE', str(dedup)), (fm, 'OUTPUT_DIR', str(output)),
                   (window, 'OUTPUT_DIR', str(output)), (brief_record, 'RECORDS_FILE', str(records)),
                   (validate_brief, 'CAND_DIR', str(cand_dir)), (validate_brief, 'OUTPUT_DIR', str(output)),
                   (agent_lane, 'run_cli_session', cli_adapter), (agent_lane, 'fetch_texts_parallel', lambda urls, **kw: {u: sources[u] for u in urls}),
                   (lane_seeds, 'collect', seed_adapter),
                   (run_pipeline, '_collect_deterministic', lambda *args: ([], {}, {'fixture': 0}, [], []))]
        for obj, name, value in patches:
            stack.enter_context(mock.patch.object(obj, name, value))
        stack.enter_context(mock.patch.dict(__import__('os').environ, DAILYBRIEF_HERMES=sys.executable))
        stack.enter_context(mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('OFFLINE REPLAY forbids network')))
        # First use actual add_like CLI entry for URL likes; no socket/listener needed.
        for n in range(10):
            with mock.patch.object(sys, 'argv', ['add_like.py', '--url', 'https://seed.example/fixture-%d' % n, '--title', '科技开源测试夹具', '--direction', '科技']):
                assert add_like.main() == 0
        assert likes.preference_direction(likes.load_likes()) == '科技'
        now = datetime(2026,10,14,8,tzinfo=prepare_brief.TZ)
        context, directory = prepare_brief.prepare(now, str(cand_dir), str(output))
        state['context'] = context
        for lane in ('cn', 'intl'):
            queries = context['lanes'][lane]['queries']
            assert any(q.get('preferred') for q in queries)
            assert any(q.get('explore') for q in queries)
        context_file = Path(directory) / 'prepare/task_context.json'
        assert run_pipeline.run(str(context_file)) == 0
        candidate, brief = context['batch']['candidates'], context['batch']['brief']
        assert not (cand_dir / Path(candidate).name).exists()
        review_payload = json.loads(Path(candidate).read_text())
        review_payload['pipeline']['semantic_review'] = 'complete'
        for lane, lane_record in review_payload['agent_review'].items():
            if lane_record.get('status') == 'ok':
                lane_record['semantic_review'] = {
                    'status': 'complete', 'reviewer': 'Hermes',
                    'reviewed_at': datetime.now(prepare_brief.TZ).isoformat(), 'findings': [],
                }
        Path(candidate).write_bytes(run_store.json_bytes(review_payload))
        final = finalize_brief.finalize(candidate, brief, str(output))
        cache = cand_dir / Path(candidate).name
        assert finalize_brief.finalize(candidate, brief, str(output)) == final
        payload = json.loads(cache.read_text())
        run_store.verify_agent_evidence(payload)
        assert {c['source_key'] for c in payload['candidates']} == {'fixture:cn', 'fixture:intl'}
        assert {c['region'] for c in payload['candidates']} == {'cn', 'intl'}
        for c in payload['candidates']:
            assert c['region'] == c['source_key'].split(':')[1]
            assert c['region_provenance'] == {'kind': 'lane', 'lane': c['region']}
        region_browser = verify_html_browser.verify(str(Path(final).with_suffix('.html')), final,
                                                    str(cache), str(base / 'region-browser-recheck'))
        region_counts = {f['region']: f['count'] for f in region_browser['filters'] if f['topic'] == ''}
        assert region_counts['cn'] == 1 and region_counts['intl'] == 1
        assert region_counts['other'] == 0
        # Exercise the real JS gate directly as well: static template checks already
        # reject tampering, so this isolates the browser's independent region oracle.
        browser_negative = []
        expected = json.loads((base / 'region-browser-recheck/expected.json').read_text())
        all_other = base / 'all-other-negative.html'
        all_other.write_text(Path(expected['html']).read_text().replace('data-region="cn"', 'data-region="other"').replace('data-region="intl"', 'data-region="other"'))
        for name, message in (('mismatched-region', 'candidate region mismatch'),
                              ('self-consistent-empty-regions', 'required region filter empty'),
                              ('empty-articles', 'empty canonical articles')):
            probe = json.loads(json.dumps(expected))
            probe['html'] = str(all_other)
            if name == 'self-consistent-empty-regions':
                for a in probe['articles']:
                    a['region'] = 'other'
                probe['requiredRegions'] = ['cn', 'intl']  # known collection lanes, independent of DOM
            if name == 'empty-articles':
                probe['articles'] = []
            directory = base / name
            directory.mkdir()
            probe['evidence'] = str(directory)
            input_file, log = directory / 'expected.json', directory / 'browser.log'
            input_file.write_text(json.dumps(probe))
            rc = verify_html_browser.run_cli_session([shutil.which('node'), str(ROOT / 'scripts/verify_html_browser.cjs'), str(input_file)], ROOT, str(log), 110)
            assert rc != 0 and message in log.read_text(), (name, rc, log.read_text())
            assert not (directory / 'browser-result.json').exists()
            browser_negative.append({'probe': name, 'rc': rc, 'rejected': True, 'log': str(log)})

        assert any(c['is_explore'] for c in payload['candidates'])
        assert any(c['is_preferred'] for c in payload['candidates'])
        assert html_report.verify_html(str(Path(final).with_suffix('.html')), final, str(cache))[0]
        refs = add_like.parse_brief_refs(final)
        links = html_report.scan_html(str(Path(final).with_suffix('.html'))).hrefs
        like_link = next(h for h in links if h.startswith(html_report.LIKE_BASE))
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(like_link).query)
        assert add_like.add_like(qs['url'][0], qs['title'][0])[0]  # URL target from actual generated link.
        for number in refs:
            with mock.patch.object(sys, 'argv', ['add_like.py', '--brief', final, '--num', str(number)]):
                assert add_like.main() == 0
        assert len(likes.load_likes()) == 12  # Duplicate URL like + text number counted once.
        next_context, _ = prepare_brief.prepare(now, str(cand_dir), str(output))
        for lane in ('cn', 'intl'):
            assert any(q.get('preferred') for q in next_context['lanes'][lane]['queries'])
            assert any(q.get('explore') for q in next_context['lanes'][lane]['queries'])
        # Same-day failed second run must not corrupt any published artifacts or records.
        protected = [Path(final), Path(final).with_suffix('.html'), cache, records]
        hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        state['context'], state['writer_failure'] = next_context, True
        assert run_pipeline.run(str(Path(next_context['batch']['candidates']).parent / 'prepare/task_context.json')) == 1
        assert hashes == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        # Reach the actual locked writer path in a NEW isolated same-day run.
        dedup.write_text('[]')
        for lane in urls:
            old=urls[lane]; urls[lane]=old+'/new-run'
            sources[urls[lane]]=sources[old]
        failed_context, failed_dir = prepare_brief.prepare(now, str(cand_dir), str(output))
        state['context'], state['writer_failure'] = failed_context, True
        assert run_pipeline.run(str(Path(failed_dir) / 'prepare/task_context.json')) == 1
        assert hashes == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        # Candidate tamper after cleanup is not a successful idempotent delivery.
        before = cache.read_bytes()
        cache.write_bytes(before + b' ')
        try:
            finalize_brief.finalize(candidate, brief, str(output))
            raise AssertionError('tampered candidate accepted')
        except ValueError:
            pass
        cache.write_bytes(before)
        result = {'mode': 'OFFLINE TEST FIXTURE REPLAY, NOT LIVE CLI', 'result': 'PASS', 'takeover': takeover,
                  'base': str(base), 'final_md': final, 'final_html': str(Path(final).with_suffix('.html')),
                  'candidate': str(cache), 'archive': str(output / 'final' / context['run_id']),
                  'likes_count': len(likes.load_likes()), 'articles': len(payload['candidates']),
                  'region_counts_390': region_counts, 'negative_browser_probes': browser_negative, 'region_browser_receipt': str(base / 'region-browser-recheck/receipt.json'),
                  "checks": ['prepare preferences/exploration shared', 'single author plus Hermes review gate',
                             'metadata/hash snapshot evidence', 'merge/rank admission', 'MD/HTML original validation',
                             'publication/immutable archive/readback', 'idempotence/candidate tamper rejection',
                             'URL likes/text numbered likes/dedup', 'subsequent preference/exploration tasks',
                             'same-day failure does not change published evidence', 'network forbidden/no 8900 listener']}
        (base / 'replay-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result


if __name__ == '__main__':
    replay(takeover='--takeover' in sys.argv)
