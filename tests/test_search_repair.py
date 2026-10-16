"""Offline regression for search snapshots and real-runtime identity recovery."""
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, SCRIPTS, long_body
from tests.test_agent_lane import make_task, good_item, ok_result, review, fake_spawn_factory, GOOD_URL
from modules import agent_lane, executor, lane_seeds, run_store, retrieve
from modules.article_text import extract


class SearchRepair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_only_uses_real_page_body_and_window_evidence(self):
        task = make_task(str(self.directory))
        def search(name, queries, candidates, stats, *a, **kw):
            self.assertIn('after:2026-10-13 before:2026-10-15', queries[0]['query'])
            candidates.extend([good_item(content='search snippet')])
        with mock.patch.object(lane_seeds.rank, 'search_batch_with_tags', side_effect=search), \
             mock.patch.object(lane_seeds.fm, 'load_dedup', return_value=set()), \
             mock.patch.object(lane_seeds.retrieve, 'fetch_texts_parallel', return_value={
                 GOOD_URL: {'text':long_body(), 'metadata':{}}}):
            seed, snapshot, digest = lane_seeds.collect(task, run_store.DeadlineBudget(time.monotonic()+2))
        item = json.loads(Path(seed).read_text())['items'][0]
        self.assertEqual(item['content'], long_body())
        self.assertEqual(item['date_evidence'], GOOD_URL)
        self.assertEqual(digest, hashlib.sha256(Path(snapshot).read_bytes()).hexdigest())

    def test_tampered_snapshot_rejected_before_source_admission(self):
        task = make_task(str(self.directory))
        snapshot = self.directory/'snapshot.json'
        snapshot.write_text('{}')
        task.update(source_snapshot=str(snapshot), source_snapshot_sha256='wrong')
        outcome = agent_lane.run_lane(task, str(self.directory), max_rounds=1,
            spawn=fake_spawn_factory([ok_result()], [review('pass')]))
        self.assertEqual(outcome['status'], 'failed')
        self.assertIn('快照被修改', outcome['reason'])

    def test_identity_exact_prompt_time_and_ambiguity(self):
        db = self.directory/'state.db'
        with sqlite3.connect(db) as c:
            c.executescript('CREATE TABLE sessions(id,model,billing_provider,started_at);'
                            'CREATE TABLE messages(session_id,role,content);')
            c.execute('INSERT INTO sessions VALUES(?,?,?,?)', ('real-session','actual-model','actual-provider',20))
            c.execute('INSERT INTO messages VALUES(?,?,?)', ('real-session','user','exact prompt'))
        log = self.directory/'run.log'
        executor.capture_runtime_identity(log, 'other prompt', 10, db)
        self.assertFalse(Path(str(log)+'.runtime.json').exists())
        executor.capture_runtime_identity(log, 'exact prompt', 21, db)
        self.assertFalse(Path(str(log)+'.runtime.json').exists())
        executor.capture_runtime_identity(log, 'exact prompt', 10, db)
        self.assertEqual(json.loads(Path(str(log)+'.runtime.json').read_text())['session_id'], 'real-session')
        Path(str(log)+'.runtime.json').unlink()
        with sqlite3.connect(db) as c:
            c.execute('INSERT INTO sessions VALUES(?,?,?,?)', ('ambiguous','model','provider',20))
            c.execute('INSERT INTO messages VALUES(?,?,?)', ('ambiguous','user','exact prompt'))
        executor.capture_runtime_identity(log, 'exact prompt', 10, db)
        self.assertFalse(Path(str(log)+'.runtime.json').exists())

    def test_no_literal_prompt_placeholders(self):
        task = make_task(str(self.directory))
        for prompt in (agent_lane.build_author_prompt(task), agent_lane.build_review_prompt(task,1)):
            self.assertNotIn('%d', prompt)

    def test_seed_prompt_does_not_repeat_full_search(self):
        task=make_task(str(self.directory))
        task.update(seed_file='seeds.json',source_snapshot='sources.json')
        prompt=agent_lane.build_author_prompt(task,feedback='remove short article',round_no=2)
        self.assertNotIn('逐条执行你的搜索工具',prompt)
        self.assertIn('最多2条定向查询',prompt)
        self.assertIn('修正轮只处理反馈',prompt)

    def test_partial_success_is_timeout_and_identity_is_explicit(self):
        log = self.directory/'agy.log'
        sid = '12345678-0000-4000-8000-123456789abc'
        log.write_text('[agy] print timeout after 1m0s with turn in progress\n' +
            json.dumps({'conversation_id':sid, 'status':'SUCCESS',
                        'response':'99999999-0000-4000-8000-999999999999'}))
        self.assertEqual(executor.failure_reason(0,log,None),'timeout')
        self.assertEqual(agent_lane.extract_session_id('agy',log),sid)

    def test_http_jsonrpc_and_sse_search_are_not_silently_empty(self):
        md = '## Query 1: query\n\n### 1. Actual title\n- **URL**: https://example.com/2026/10/13/story\nArticle snippet'
        envelope = json.dumps({'jsonrpc':'2.0','result':{'content':[{'type':'text','text':md}]}})
        for raw in (md,envelope,'data: '+envelope+'\n\ndata: [DONE]\n'):
            items=retrieve.parse_search_markdown(raw)
            self.assertEqual(len(items),1)
            self.assertEqual(items[0]['query_idx'],0)
            self.assertEqual(items[0]['title'],'Actual title')

    def test_queued_body_fetches_stop_at_shared_deadline(self):
        calls=[]
        def fetch(url, timeout, limit):
            calls.append(url)
            time.sleep(.03)
            return {'text':'test body','metadata':{}}
        start=time.monotonic()
        with mock.patch.object(retrieve,'fetch_article',side_effect=fetch):
            retrieve.fetch_texts_parallel(['https://example.com/'+str(n) for n in range(100)],
                with_metadata=True,budget=run_store.DeadlineBudget(start+.06),max_workers=2)
        self.assertLess(len(calls),10)
        self.assertLess(time.monotonic()-start,.4)

    def test_body_extraction_excludes_navigation_and_uses_inner_article(self):
        raw='<main><div>Recommended stories</div><article><h1>Title</h1><div class="entry-content">Real &amp; body<nav>menu</nav></div><footer>Related stories</footer></article></main>'
        self.assertEqual(extract(raw),'Real & body')
        self.assertEqual(extract('<nav>menu</nav><p>Actual body</p><script>hidden</script>'),'Actual body')
        self.assertEqual(extract('<article class id><p>Actual body</p></article>'),'Actual body')
        self.assertEqual(extract('<div>Site navigation</div><div class="p_mainnew">Article only</div><div>Footer</div>'),'Article only')


if __name__ == '__main__':
    unittest.main()
