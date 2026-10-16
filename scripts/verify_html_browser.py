#!/usr/bin/env python3
"""Real isolated Chrome/Chromium offline 390px + desktop verification; no user browser/profile."""
import argparse
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from modules import html_report, run_store, region as region_mod
from modules.agent_lane import run_cli_session


def browser_path():
    for bundle in sorted(Path('/Applications').glob('*.app')):
        if bundle.stem not in ('Google Chrome','Microsoft Edge','Chromium'): continue
        with (bundle/'Contents/Info.plist').open('rb') as f: meta=plistlib.load(f)
        executable=bundle/'Contents/MacOS'/meta['CFBundleExecutable']
        if executable.is_file(): return str(executable)
    raise ValueError('No discovered Chrome/Edge/Chromium application; browser verification blocked')


def expected_articles(brief, candidate):
    articles = html_report.canonical_articles(Path(brief).read_text())
    if not articles:
        raise ValueError('Browser validation requires nonempty canonical articles')
    payload = json.loads(Path(candidate).read_text())
    pool = {c['url']: c for key in ('candidates', 'source_leftovers', 'horizon') for c in payload.get(key, [])}
    required_regions = set()
    for article in articles:
        c = pool[article['cites'][0][1]]
        region = region_mod.normalized(c.get('region'))
        provenance = c.get('region_provenance') or {}
        if provenance.get('kind') == 'lane':
            lane = provenance.get('lane')
            if lane not in ('cn', 'intl') or region != lane:
                raise ValueError('Candidate lane region provenance lost or mismatched')
        if region in ('cn', 'intl'):
            required_regions.add(region)
        article.update(region=region, direction=c.get('direction') or '其他')
    return articles, sorted(required_regions)

def verify(html,brief,candidate,evidence,timeout=110):
    ok,errors=html_report.verify_html(html,brief,candidate)
    if not ok: raise ValueError('HTML static gate failed: '+str(errors))
    directory=Path(evidence);directory.mkdir(parents=True,exist_ok=True)
    articles, required_regions = expected_articles(brief, candidate)
    expected={'articles':articles, 'requiredRegions':required_regions,
              'html':str(Path(html).resolve()),'browser':browser_path(),'evidence':str(directory.resolve())}
    # Hermes already ships Playwright's Node driver inside the Python package.
    # Reuse it when npm's module search path cannot resolve 'playwright'.
    spec=importlib.util.find_spec('playwright')
    if spec and spec.origin:
        package=Path(spec.origin).parent/'driver/package/index.js'
        if package.is_file(): expected['playwrightPackage']=str(package)
    (directory/'expected.json').write_text(json.dumps(expected,ensure_ascii=False))
    node=shutil.which('node')
    if not node: raise ValueError('Node required for real browser validation')
    rc=run_cli_session([node,str(ROOT/'scripts/verify_html_browser.cjs'),str(directory/'expected.json')],ROOT,str(directory/'browser.log'),timeout)
    if rc: raise ValueError('Real browser check failed rc=%s; see %s'%(rc,directory/'browser.log'))
    result=json.loads((directory/'browser-result.json').read_text())
    if result.get('ok') is not True or result.get('mode')!='REAL_BROWSER_OFFLINE':
        raise ValueError('Missing real browser verdict')
    result.update(html_sha256=hashlib.sha256(Path(html).read_bytes()).hexdigest(),md_sha256=hashlib.sha256(Path(brief).read_bytes()).hexdigest())
    run_store.write_once(directory/'receipt.json',run_store.json_bytes(result))
    return result


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('html');ap.add_argument('brief');ap.add_argument('candidate');ap.add_argument('--evidence',required=True)
    a=ap.parse_args()
    try: print(json.dumps(verify(a.html,a.brief,a.candidate,a.evidence),ensure_ascii=False,indent=2))
    except (ValueError,OSError) as e: print('[BROWSER_FAILED]',e);sys.exit(1)
