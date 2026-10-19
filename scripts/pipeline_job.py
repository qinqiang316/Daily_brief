#!/usr/bin/env python3
"""Durable asynchronous job. Launch/poll/claim/ack are finite cron turns; no session continuation.
Does not finalize or deliver. Chairman must review a ready ticket and explicitly finalize.
"""
import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from modules import run_store, pipeline_budget
from modules.window import TZ
import prepare_brief
DEFAULT_JOBS=Path('/Users/qqiang/.hermes/cache/scratch/dailybrief-jobs')

@contextmanager
def guard(directory):
    directory.mkdir(parents=True,exist_ok=True)
    with (directory/'control.lock').open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX)
        try: yield
        finally: fcntl.flock(f,fcntl.LOCK_UN)


def load(path): return json.loads(Path(path).read_text())

def save(path,value): run_store.atomic_bytes(path,run_store.json_bytes(value))

def launch(jobs, context=None, spawn=None):
    jobs=Path(jobs).resolve()
    with guard(jobs):
        # Active unfinished ticket survives cron/session expiration. Do not repeat prepare/search.
        for ticket in jobs.glob('*/ticket.json'):
            value=load(ticket)
            if value['status'] in ('queued','running','ready','claimed'):
                return str(ticket)
        if context is None:
            value,directory=prepare_brief.prepare()
            context=Path(directory)/'prepare/task_context.json'
        context=Path(context).resolve();value=load(context)
        rid=run_store.safe_run_id(value['run_id'])
        expected=(Path(value['batch']['candidates']).parent/'prepare/task_context.json').resolve()
        if context!=expected: raise ValueError('精确prepare上下文路径不符')
        import collect_brief
        published_alias = Path(collect_brief.OUTPUT_DIR) / Path(value['batch']['brief']).name
        if published_alias.is_file():
            raise ValueError('当日已有发布版，拒绝启动新采集')
        directory=jobs/rid;directory.mkdir()
        ticket=directory/'ticket.json'
        save(ticket,{'run_id':rid,'context':str(context),'context_sha256':hashlib.sha256(context.read_bytes()).hexdigest(),
                     'status':'queued','created_at':time.time(),'candidates':value['batch']['candidates'],
                     'brief':value['batch']['brief'],'html':value['batch']['html'],'log':str(directory/'pipeline.log')})
        with (directory/'launcher.log').open('wb') as f:
            proc=(spawn or subprocess.Popen)([sys.executable,str(Path(__file__).resolve()),'work',str(ticket)],
                cwd=ROOT,stdin=subprocess.DEVNULL,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
        state=load(ticket);state['worker_pid']=proc.pid;save(ticket,state)
        return str(ticket)


def work(ticket,command=None,timeout=pipeline_budget.RUNNER+30):
    ticket=Path(ticket).resolve();jobs=ticket.parent.parent
    with guard(jobs):
        value=load(ticket)
        if value['status']!='queued': return 1
        if hashlib.sha256(Path(value['context']).read_bytes()).hexdigest()!=value['context_sha256']:
            value.update(status='failed',reason='context hash changed');save(ticket,value);return 1
        value.update(status='running',started_at=time.time());save(ticket,value)
    cmd=command or [sys.executable,str(ROOT/'scripts/run_pipeline.py'),value['context']]
    proc=None;previous={}
    def cancel(signum,frame): raise KeyboardInterrupt('job cancellation')
    for sig in (signal.SIGTERM,signal.SIGINT):
        previous[sig]=signal.signal(sig,cancel)
    try:
        with open(value['log'],'wb') as f:
            proc=subprocess.Popen(cmd,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
            value['pipeline_pid']=proc.pid;save(ticket,value)
            try: rc=proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired,KeyboardInterrupt):
                # Pipeline descendants have their own groups; pipeline handles TERM first.
                try: os.killpg(proc.pid,signal.SIGTERM)
                except ProcessLookupError: pass
                try: proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try: os.killpg(proc.pid,signal.SIGKILL)
                    except ProcessLookupError: pass
                    proc.wait()
                rc=124
        with guard(jobs):
            value=load(ticket);value.update(exit_code=rc,finished_at=time.time(),status='ready' if rc==0 else 'failed');save(ticket,value)
        return rc
    finally:
        for sig,handler in previous.items(): signal.signal(sig,handler)


def poll(jobs):
    jobs=Path(jobs)
    with guard(jobs):
        rows=[]
        for p in sorted(jobs.glob('*/ticket.json')):
            v=load(p)
            if v['status'] in ('ready','failed'): rows.append(dict(v,ticket=str(p)))
            elif v['status']=='claimed' and time.time()-v.get('claimed_at',v['created_at'])>900:
                rows.append(dict(v,ticket=str(p),review_overdue=True,
                    reason='验收领取超过15分钟，保留票据并报警，不静默丢失或重复发布'))
            elif v['status'] in ('running','queued'):
                pid=v.get('worker_pid')
                try:
                    if not pid: raise ProcessLookupError()
                    os.kill(pid,0)
                except ProcessLookupError:
                    v.update(status='failed',reason='worker exited without result');save(p,v);rows.append(dict(v,ticket=str(p)))
                else:
                    if time.time()-v['created_at']>pipeline_budget.TOTAL+60:
                        rows.append(dict(v,ticket=str(p),reason='worker overdue: chairman must cancel exact PID, never restart blindly'))
        return rows


def claim(ticket):
    ticket=Path(ticket).resolve()
    with guard(ticket.parent.parent):
        v=load(ticket)
        if v['status'] not in ('ready','failed'): raise ValueError('ticket unavailable/already claimed')
        v['previous_status']=v['status'];v['status']='claimed';v['claimed_at']=time.time();save(ticket,v)
        return v


def ack(ticket):
    ticket=Path(ticket).resolve()
    with guard(ticket.parent.parent):
        v=load(ticket)
        if v['status']!='claimed': raise ValueError('claim first')
        v.update(status='acknowledged',acknowledged_at=time.time());save(ticket,v)


def completed_today():
    """An early manual publication makes the regular morning launch a
    verified no-op. Existing hash/registration guards remain authoritative.
    """
    import collect_brief
    import finalize_brief
    day=datetime.now(TZ).strftime('%Y-%m-%d')
    record=finalize_brief._record_for(day)
    if not record:
        return None
    candidate=run_store.candidate_path(collect_brief.CAND_DIR,day,record['run_id'])
    brief=Path(collect_brief.OUTPUT_DIR)/('Daily-Brief-'+day+'.md')
    return finalize_brief._delivered_alias(candidate,brief,collect_brief.OUTPUT_DIR)


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('action',choices=['launch','work','poll','claim','ack']);ap.add_argument('ticket',nargs='?');ap.add_argument('--jobs',default=str(DEFAULT_JOBS));ap.add_argument('--context');ap.add_argument('--cron',action='store_true')
    a=ap.parse_args()
    try:
        if a.action=='launch':
            if a.cron and completed_today():
                print(json.dumps({'wakeAgent':False,'reason':'today already published and verified'}))
                return 0
            print('JOB_TICKET='+launch(a.jobs,a.context))
            if a.cron: print(json.dumps({'wakeAgent':False}))
        elif a.action=='work': return work(a.ticket)
        elif a.action=='poll':
            rows=poll(a.jobs)
            print(json.dumps(rows,ensure_ascii=False))
            if a.cron and not rows: print(json.dumps({'wakeAgent':False}))
        elif a.action=='claim': print(json.dumps(claim(a.ticket),ensure_ascii=False))
        else: ack(a.ticket)
        return 0
    except (ValueError,OSError,KeyError) as e: print('[JOB_FAILED]',e);return 1

if __name__=='__main__': sys.exit(main())
