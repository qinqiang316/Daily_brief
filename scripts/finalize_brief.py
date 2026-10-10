#!/usr/bin/env python3
"""Validate, add likes, validate again and publish immutable per-run artifacts."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from modules import run_store
import collect_brief
import validate_brief
import like_links
import brief_record


@contextmanager
def run_lock(directory):
    lock = collect_brief.acquire_lock(str(Path(directory) / "writer.lock"))
    if lock is None:
        raise ValueError("此批次仍有 agy/发布进程运行，禁止重试或交付")
    try:
        yield
    finally:
        collect_brief.release_lock(lock)


def validate(brief, cand):
    saved = sys.argv
    try:
        sys.argv = ["validate_brief.py", str(brief), str(cand), "--no-record"]
        return validate_brief.main()
    finally:
        sys.argv = saved


def finalize(cand, brief, output_dir=None):
    cand, brief = Path(cand).resolve(), Path(brief).resolve()
    directory = run_store.managed_run(cand)
    if directory is None:
        raise ValueError("须使用采集 stdout 给出的独立批次候选路径")
    manifest = json.loads((directory / "manifest.json").read_text())
    if cand != Path(manifest["candidates"]) or brief != Path(manifest["brief"]):
        raise ValueError("输入必须与本次 manifest 的精确路径一致")
    output_dir = Path(output_dir or collect_brief.OUTPUT_DIR)
    with run_lock(directory):
        if (directory / "published.json").is_file():
            receipt = json.loads((directory / "published.json").read_text())
            final = Path(receipt["brief_path"])
            final_cand = Path(receipt["candidate_path"])
            if (hashlib.sha256(final.read_bytes()).hexdigest() != receipt["brief_sha256"]
                    or hashlib.sha256(final_cand.read_bytes()).hexdigest() != receipt["candidate_sha256"]):
                raise ValueError("已发布快照被修改，禁止交付")
            with run_lock(output_dir):
                alias = output_dir / final.name
                if not alias.exists():
                    run_store.atomic_bytes(alias, final.read_bytes())
                elif alias.read_bytes() != final.read_bytes():
                    raise ValueError("当日发布入口已变更，禁止覆盖")
                if brief_record.record_brief(str(final), str(final_cand)) is None:
                    raise ValueError("简报登记失败，不交付；保留快照供幂等重试")
            return str(final)
        # Global publication fence: different runs cannot replace an existing delivered day.
        with run_lock(output_dir):
            alias = output_dir / brief.name
            if alias.exists():
                raise ValueError("当日已有发布简报，保留旧版；本批次不得覆盖交付路径")
            if validate(brief, cand):
                raise ValueError("第一次校验失败")
            ok, msg = like_links.add_like_section(str(brief))
            print(msg)
            if not ok and "已是最新" not in msg:
                raise ValueError("点赞区重建失败")
            if validate(brief, cand):
                raise ValueError("最终校验失败")
            candidate_bytes, brief_bytes = cand.read_bytes(), brief.read_bytes()
            # Verify the last PASS describes precisely the bytes being published.
            receipts = list((directory / "validation").glob("*/receipt.json"))
            latest = max(receipts, key=lambda p: p.stat().st_mtime_ns)
            check = json.loads(latest.read_text())
            ch, bh = hashlib.sha256(candidate_bytes).hexdigest(), hashlib.sha256(brief_bytes).hexdigest()
            if check["status"] != "PASS" or (check["candidate_sha256"], check["brief_sha256"]) != (ch, bh):
                raise ValueError("校验后内容变更，禁止发布")
            final_cand = directory / "final" / cand.name
            final_brief = brief.parent / "final" / brief.name
            run_store.write_once(final_cand, candidate_bytes)
            run_store.write_once(final_brief, brief_bytes)
            receipt = {"run_id": manifest["run_id"], "date": manifest["date"],
                       "brief_path": str(final_brief), "candidate_path": str(final_cand),
                       "brief_sha256": bh, "candidate_sha256": ch, "validation_receipt": str(latest)}
            run_store.write_once(directory / "published.json", run_store.json_bytes(receipt))
            run_store.atomic_bytes(alias, brief_bytes)
            if brief_record.record_brief(str(final_brief), str(final_cand)) is None:
                raise ValueError("简报登记失败，不交付；保留快照供幂等重试")
            return str(final_brief)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("brief")
    ap.add_argument("candidates")
    args = ap.parse_args()
    try:
        final = finalize(args.candidates, args.brief)
        print('MEDIA:"%s"' % final)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print("[FINALIZE_FAILED] %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
