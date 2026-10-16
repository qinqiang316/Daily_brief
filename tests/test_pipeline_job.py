"""Durable scheduler contract, all context in TMPDIR; real subprocesses are local fixtures."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from tests.helpers import ROOT,SCRIPTS
import pipeline_job
from modules import agent_lane, pipeline_budget, run_store

class JobCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.jobs=self.root/'jobs'
        self.cand=self.root/'_candidates/runs/fixture/Daily-Brief-2099-01-01-candidates.json'
        self.brief=self.root/'output/runs/fixture/Daily-Brief-2099-01-01.md'
        self.context=self.cand.parent/'prepare/task_context.json';self.context.parent.mkdir(parents=True)
        self.context.write_text(json.dumps({'run_id':'fixture','batch':{'candidates':str(self.cand),'brief':str(self.brief),'html':str(self.brief.with_suffix('.html'))},'fixture':'TEST NO SEARCH'}))
    def tearDown(self): self.tmp.cleanup()
    def launch(self):
        def spawn(cmd,**kw):
            self.assertEqual(cmd[2],'work');self.assertTrue(kw['start_new_session']);return mock.Mock(pid=os.getpid())
        return Path(pipeline_job.launch(self.jobs,self.context,spawn=spawn))
    def test_durable_ready_claim_ack_and_no_duplicate_launch(self):
        ticket=self.launch()
        self.assertEqual(pipeline_job.launch(self.jobs,self.context,spawn=mock.Mock(side_effect=AssertionError('duplicate spawn'))),str(ticket))
        # ACTUAL local fixture worker, not a pipeline/live inference claim.
        self.assertEqual(pipeline_job.work(ticket,command=[sys.executable,'-c','print("SIMULATED PIPELINE WORKER FIXTURE")'],timeout=2),0)
        self.assertEqual(pipeline_job.poll(self.jobs)[0]['status'],'ready')
        self.assertEqual(pipeline_job.claim(ticket)['previous_status'],'ready')
        with self.assertRaises(ValueError): pipeline_job.claim(ticket)
        pipeline_job.ack(ticket);self.assertEqual(pipeline_job.poll(self.jobs),[])
    def test_failed_worker_and_context_tamper(self):
        ticket=self.launch();self.context.write_text(self.context.read_text()+' ')
        self.assertEqual(pipeline_job.work(ticket),1)
        self.assertEqual(pipeline_job.poll(self.jobs)[0]['reason'],'context hash changed')
    def test_timeout_local_process_reaped(self):
        ticket=self.launch()
        self.assertEqual(pipeline_job.work(ticket,command=[sys.executable,'-c','import time;time.sleep(30)'],timeout=.1),124)
        value=json.loads(ticket.read_text());self.assertEqual(value['status'],'failed')
        with self.assertRaises(ProcessLookupError): os.kill(value['pipeline_pid'],0)
    def test_published_day_refused_before_launch(self):
        alias=self.root/'output'/self.brief.name;alias.parent.mkdir();alias.write_text('TEST already published')
        with self.assertRaises(ValueError): pipeline_job.launch(self.jobs,self.context,spawn=mock.Mock())
    def test_early_publication_skips_cron_only_after_exact_hash_check(self):
        import hashlib
        import collect_brief,finalize_brief
        day=pipeline_job.datetime.now(pipeline_job.TZ).strftime('%Y-%m-%d')
        output=self.root/'output';output.mkdir()
        alias=output/('Daily-Brief-'+day+'.md');alias.write_text('TEST published brief')
        cache=self.root/'published-candidates.json';cache.write_text('TEST published evidence')
        record={'run_id':'fixture','brief_sha256':hashlib.sha256(alias.read_bytes()).hexdigest(),
                'candidate_path':str(cache),'candidate_sha256':hashlib.sha256(cache.read_bytes()).hexdigest()}
        with mock.patch.object(collect_brief,'CAND_DIR',str(self.root/'_candidates')), \
             mock.patch.object(collect_brief,'OUTPUT_DIR',str(output)), \
             mock.patch.object(finalize_brief,'_record_for',return_value=record):
            self.assertEqual(pipeline_job.completed_today(),alias)
            alias.write_text('tampered')
            with self.assertRaises(ValueError):pipeline_job.completed_today()
    def test_stage_saturation_preserves_body_writer_html_final(self):
        clock=[0.0]
        with mock.patch('time.monotonic',side_effect=lambda:clock[0]):
            b=run_store.DeadlineBudget(pipeline_budget.RUNNER)
            for duration in (pipeline_budget.DETERMINISTIC,pipeline_budget.LANES,pipeline_budget.BODY,pipeline_budget.WRITER,pipeline_budget.HTML,pipeline_budget.VALIDATION):
                stage=b.derive(duration)
                self.assertEqual(stage.remaining(),duration)
                clock[0]+=duration
            self.assertTrue(b.expired())
        self.assertEqual(pipeline_budget.TOTAL-pipeline_budget.RUNNER,pipeline_budget.FINAL_REVIEW)
    def test_cancel_registry_kills_only_owned_process_group(self):
        import subprocess,time
        p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
        try:
            with agent_lane._ACTIVE_LOCK: agent_lane._ACTIVE.add(p.pid)
            agent_lane.cancel_sessions();p.wait(timeout=2)
            with self.assertRaises(ProcessLookupError): os.kill(p.pid,0)
        finally:
            with agent_lane._ACTIVE_LOCK: agent_lane._ACTIVE.discard(p.pid)
            agent_lane.CANCELLED.clear()
