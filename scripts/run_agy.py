#!/usr/bin/env python3
"""Run agy with one writer per batch; timeout kills the actual subprocess group."""
import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from modules import run_store
from finalize_brief import run_lock


def run(candidates, prompt, timeout=1200):
    cand = Path(candidates).resolve()
    directory = run_store.managed_run(cand)
    if directory is None:
        raise ValueError("agy 必须绑定独立批次候选")
    manifest = json.loads((directory / "manifest.json").read_text())
    payload = json.loads(cand.read_text())
    if str(cand) != manifest["candidates"] or payload.get("run_id") != manifest["run_id"]:
        raise ValueError("manifest/候选 run_id 不匹配")
    content = Path(prompt).read_text()
    if not all(str(manifest[k]) in content for k in ("run_id", "candidates", "brief")):
        raise ValueError("agy 任务必须包含精确 run_id、候选路径和简报路径")
    if timeout <= 0:
        raise ValueError("超时必须大于零")
    with run_lock(directory):
        if (directory / "published.json").exists():
            raise ValueError("批次已完成发布，不再重写")
        attempt = directory / "agy"
        attempt.mkdir(exist_ok=True)
        import uuid
        token = uuid.uuid4().hex
        prompt_copy, log = attempt / (token + ".txt"), attempt / (token + ".log")
        run_store.write_once(prompt_copy, content.encode())
        cmd = [os.environ.get("DAILYBRIEF_AGY", "/Users/qqiang/.local/bin/agy"),
               "--dangerously-skip-permissions", "--print-timeout", "%ss" % timeout]
        if os.environ.get("AGY_MODEL"):
            cmd += ["--model", os.environ["AGY_MODEL"]]
        cmd += ["-p", content]
        with log.open("wb") as fh:
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            try:
                rc = proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                # Parent may exit after TERM while a descendant ignores it.
                # Kill any survivors before releasing the batch writer lock.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                rc = 124
        run_store.write_once(attempt / (token + ".json"), run_store.json_bytes(
            {"run_id": manifest["run_id"], "exit_code": rc,
             "prompt": str(prompt_copy), "log": str(log)}))
        print("[AGY_EXIT] rc=%s log=%s" % (rc, log))
        return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("candidates")
    ap.add_argument("prompt")
    ap.add_argument("--timeout", type=int, default=1200)
    args = ap.parse_args()
    try:
        return run(args.candidates, args.prompt, args.timeout)
    except (OSError, ValueError) as exc:
        print("[AGY_FAILED] %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
