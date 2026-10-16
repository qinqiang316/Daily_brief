"""One bounded role attempt and transport-only Hermes takeover, never a gate bypass.
The spawn contract returns only after the process group has exited. Test adapters must
be explicitly supplied; no external inference is invoked by legacy injected spawns.
"""
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

HERMES_CLI = os.environ.get('DAILYBRIEF_HERMES', '/Users/qqiang/.local/bin/hermes')


def failure_reason(rc, log, artifact):
    text = Path(log).read_text(errors='replace')[-20000:] if Path(log).exists() else ''
    if 'print timeout after' in text:
        return 'timeout'
    if rc == 127:
        return 'binary_missing'
    if rc == 124:
        return 'timeout'
    if rc:
        if re.search(r'quota|credit|rate.limit|额度|余额|429', text, re.I):
            return 'quota'
        if re.search(r'auth|unauthorized|forbidden|鉴权|401|403', text, re.I):
            return 'authentication'
        return 'nonzero_exit'
    if artifact and not Path(artifact).is_file():
        return 'missing_artifact'
    return None


def hermes_identity(log):
    """Hermes -z's real usage report; deliberately retain only non-secret keys."""
    path = Path(str(log) + '.usage.json')
    try:
        value = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    identity = {k: value.get(k) for k in ('model', 'provider', 'session_id', 'completed', 'failed', 'partial', 'interrupted')}
    receipt = Path(str(log) + '.runtime.json')
    if receipt.exists():
        actual = json.loads(receipt.read_text())
        for key in ('model', 'provider', 'session_id'):
            if not identity.get(key):
                identity[key] = actual.get(key)
    return identity


def capture_runtime_identity(log, prompt, started_at, db_path=None):
    """Older Codex runtime omits identity in usage JSON. Recover only from an
    exact prompt + current invocation timestamp, never from the latest session.
    Ambiguous/missing matches still fail closed. No secrets/system prompts read.
    """
    db_path = Path(db_path or Path(os.environ.get('HERMES_HOME', str(Path.home()/'.hermes'))) / 'state.db')
    if not db_path.exists():
        return
    try:
        with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as conn:
            rows = conn.execute('''SELECT DISTINCT s.id, s.model, s.billing_provider
                FROM sessions s JOIN messages m ON m.session_id=s.id
                WHERE s.started_at>=? AND m.role='user' AND m.content=?''',
                (started_at, prompt)).fetchall()
        if len(rows) == 1 and all(rows[0]):
            sid, model, provider = rows[0]
            Path(str(log)+'.runtime.json').write_text(json.dumps(dict(
                session_id=sid, model=model, provider=provider,
                source='Hermes state.db exact prompt/current invocation')))
    except (sqlite3.Error, OSError):
        return


def configured_fallback(text=None):
    """First fallback_providers entry from Hermes config. Never invent a model."""
    if text is None:
        path = Path(os.environ.get('HERMES_CONFIG', str(Path.home() / '.hermes' / 'config.yaml')))
        try:
            text = path.read_text()
        except OSError:
            return None, None
    provider = model = None
    in_fb = False
    for line in text.splitlines():
        if re.match(r'^fallback_providers:\s*$', line):
            in_fb = True
            continue
        if not in_fb:
            continue
        if line and not line[:1].isspace():
            break
        found = re.match(r"^\s+-\s+provider:\s*['\"]?(.+?)['\"]?\s*$", line)
        if found:
            provider = found.group(1).strip().strip("'\"")
            model = None
            continue
        found = re.match(r"^\s+model:\s*['\"]?(.+?)['\"]?\s*$", line)
        if found and provider and model is None:
            return provider, found.group(1).strip().strip("'\"")
    return None, None


def _api_interrupted(log):
    try:
        text = Path(log).read_text(errors='replace')
    except OSError:
        return False
    return 'retrying API call' in text or 'Operation interrupted' in text


def _score_hermes(rc, hlog, artifact, takeover):
    identity = hermes_identity(hlog)
    takeover.update(rc=rc, model=identity.get('model'), provider=identity.get('provider'),
                    session=identity.get('session_id'), usage=str(hlog) + '.usage.json')
    if rc == 0 and (not all(identity.get(k) for k in ('model', 'provider', 'session_id'))
                    or identity.get('failed') is not False or identity.get('completed') is not True
                    or identity.get('partial') or identity.get('interrupted')):
        rc = takeover['rc'] = 1
        takeover['failure'] = 'missing_or_incomplete_runtime_identity'
    if failure_reason(rc, hlog, artifact):
        takeover['failure'] = takeover.get('failure') or failure_reason(rc, hlog, artifact)
        rc = takeover['rc'] = rc or 1
    return rc, takeover


def execute(kind, command, prompt, cwd, log, artifact, timeout, spawn,
            fallback=True, role=None):
    """timeout is the primary CLI budget. It must match the CLI's own
    --print-timeout. Takeover runs only on time left after an early failure.
    JSON/content/URL/metadata/semantic verdict errors are intentionally not triggers.
    """
    end = time.monotonic() + max(0, timeout)
    attempts = []
    # Caller already sliced the lane into this step and set the CLI's own
    # --print-timeout to the same value. Do not steal half up front: that
    # SIGTERMs a live search and records a false timeout. Takeover uses only
    # time left after an early primary failure.
    unavailable = os.environ.get('DAILYBRIEF_AGY_UNAVAILABLE') if kind == 'agy' else None
    if unavailable and spawn is __import__('modules.agent_lane', fromlist=['run_cli_session']).run_cli_session:
        Path(log).write_text('Primary CLI health probe failed: ' + unavailable + '\n')
        rc = 127
    else:
        rc = spawn(command, cwd, str(log), max(0, timeout))
    reason = failure_reason(rc, log, artifact)
    if unavailable:
        reason = 'health_probe_failed'
    record = {'executor': kind, 'role': role, 'rc': rc, 'log': str(log),
              'model': 'kimi-code/kimi-for-coding' if kind == 'kimi' else os.environ.get('AGY_MODEL') or 'unreported',
              'provider': 'unreported', 'takeover_reason': reason}
    attempts.append(record)
    verdict_failure = False
    if artifact and role and 'reviewer' in role and Path(artifact).is_file():
        try:
            verdict_failure = json.loads(Path(artifact).read_text()).get('verdict') == 'fail'
        except (ValueError, OSError, AttributeError):
            pass
    from modules import agent_lane
    attempts[0]["session"] = agent_lane.extract_session_id(kind, str(log))
    if reason and fallback and not verdict_failure and rc != 130 and not agent_lane.CANCELLED.is_set() and time.monotonic() < end:
        # Partial output from a failed executor is not fresh successful output.
        if artifact and Path(artifact).exists():
            Path(artifact).unlink()
        hlog = str(log) + '.hermes.log'
        binary = os.environ.get('DAILYBRIEF_HERMES', HERMES_CLI)
        takeover = {'executor': 'hermes', 'role': role, 'original_executor': kind,
                    'takeover_reason': reason, 'log': hlog}
        # No resume/-c: independent author and reviewer always have new sessions.
        # Default route resolves the CURRENT available configured runtime; never guessed.
        cmd = [binary, '--ignore-rules', '--toolsets', 'web,terminal',
               '--usage-file', hlog + '.usage.json', '-z', prompt]
        model = os.environ.get('DAILYBRIEF_HERMES_MODEL')
        provider = os.environ.get('DAILYBRIEF_HERMES_PROVIDER')
        if model and provider:
            cmd[1:1] = ['--provider', provider, '-m', model]
        started_at = time.time()
        if not shutil.which(binary):
            Path(hlog).write_text('Hermes binary missing\n')
            rc = 127
        else:
            rc = spawn(cmd, cwd, hlog, max(0, end - time.monotonic()))
        if spawn is agent_lane.run_cli_session:
            capture_runtime_identity(hlog, prompt, started_at)
        rc, takeover = _score_hermes(rc, hlog, artifact, takeover)
        attempts.append(takeover)
        record = takeover
        # Default route can die mid-retry before fallback_providers runs. One extra
        # attempt uses the configured fallback; do not invent a model or relax gates.
        fb_provider, fb_model = configured_fallback() if _api_interrupted(hlog) else (None, None)
        if fb_provider and fb_model and not agent_lane.CANCELLED.is_set() and time.monotonic() < end:
            if artifact and Path(artifact).exists():
                Path(artifact).unlink()
            flog = str(log) + '.hermes-fallback.log'
            fcmd = [binary, '--ignore-rules', '--toolsets', 'web,terminal',
                    '--provider', fb_provider, '-m', fb_model,
                    '--usage-file', flog + '.usage.json', '-z', prompt]
            frc = 127
            started_at = time.time()
            if not shutil.which(binary):
                Path(flog).write_text('Hermes binary missing\n')
            else:
                frc = spawn(fcmd, cwd, flog, max(0, end - time.monotonic()))
            if spawn is agent_lane.run_cli_session:
                capture_runtime_identity(flog, prompt, started_at)
            ftake = {'executor': 'hermes', 'role': role, 'original_executor': kind,
                     'takeover_reason': 'api_interrupted', 'log': flog,
                     'fallback_provider': fb_provider, 'fallback_model': fb_model}
            rc, ftake = _score_hermes(frc, flog, artifact, ftake)
            attempts.append(ftake)
            record = ftake
    record = dict(record, attempts=attempts, semantic_rejection=verdict_failure)
    Path(str(log) + '.attempts.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return rc, record
