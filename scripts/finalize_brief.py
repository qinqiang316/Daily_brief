#!/usr/bin/env python3
"""Validate, publish the dated brief, then remove run working files."""
import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from modules import run_store
from modules import html_report
import collect_brief
import validate_brief
import like_links
import brief_record


DAY_RE = re.compile(r"Daily-Brief-(\d{4}-\d{2}-\d{2})")


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


def _day_of(path):
    match = DAY_RE.search(Path(path).name)
    return match.group(1) if match else None


def _run_id_of(cand):
    path = Path(cand)
    if path.parent.name == "final":
        return path.parent.parent.name
    return path.parent.name


def _record_for(day):
    for rec in brief_record.load_records():
        if rec.get("date") == day:
            return rec
    return None


def _work_dir(path):
    path = Path(path)
    if path.parent.name == "final" and path.parent.parent.parent.name == "runs":
        return path.parent.parent
    if path.parent.parent.name == "runs":
        return path.parent
    return None


def _delivered_alias(cand, brief, output_dir):
    """Return the original dated brief when this run is already confirmed."""
    day = _day_of(brief) or _day_of(cand)
    if not day:
        return None
    alias = Path(output_dir) / ("Daily-Brief-%s.md" % day)
    if not alias.is_file():
        return None
    rec = _record_for(day)
    if not rec or not rec.get("brief_sha256"):
        return None
    if _run_id_of(cand) != rec.get("run_id"):
        raise ValueError("当日已有发布简报，保留旧版；本批次不得覆盖交付路径")
    if hashlib.sha256(alias.read_bytes()).hexdigest() != rec["brief_sha256"]:
        raise ValueError("当日发布入口已变更，禁止覆盖")
    html_hash = rec.get("html_sha256")
    if html_hash:
        html_alias = alias.with_suffix(".html")
        if (not html_alias.is_file()
                or hashlib.sha256(html_alias.read_bytes()).hexdigest() != html_hash):
            raise ValueError("当日 HTML 发布入口已变更，禁止覆盖")
    return alias


def _cleanup_runs(cand, brief, alias):
    """Delete isolation working directories after the dated brief is confirmed."""
    alias = Path(alias).resolve()
    if not alias.is_file():
        raise ValueError("发布入口不存在，禁止清理过程文件")
    seen = []
    for path in (cand, brief):
        work = _work_dir(path)
        if work is None:
            continue
        work = work.resolve()
        if work in seen:
            continue
        seen.append(work)
        if not work.exists():
            continue
        if work.parent.name != "runs" or not run_store.safe_run_id(work.name):
            raise ValueError("拒绝清理非批次目录")
        if alias == work or work in alias.parents:
            raise ValueError("拒绝清理发布入口")
        shutil.rmtree(work)
        parent = work.parent
        if parent.name == "runs" and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()


def _date_cache(directory, cand):
    return Path(directory).parent.parent / Path(cand).name


def _publish_locked(directory, cand, brief, output_dir):
    manifest = json.loads((directory / "manifest.json").read_text())
    if cand != Path(manifest["candidates"]) or brief != Path(manifest["brief"]):
        raise ValueError("输入必须与本次 manifest 的精确路径一致")
    alias = output_dir / brief.name
    if (directory / "published.json").is_file():
        receipt = json.loads((directory / "published.json").read_text())
        final = Path(receipt["brief_path"])
        final_cand = Path(receipt["candidate_path"])
        if (hashlib.sha256(final.read_bytes()).hexdigest() != receipt["brief_sha256"]
                or hashlib.sha256(final_cand.read_bytes()).hexdigest() != receipt["candidate_sha256"]):
            raise ValueError("已发布快照被修改，禁止交付")
        final_html = None
        if receipt.get("html_sha256"):
            final_html = Path(receipt["html_path"])
            if (not final_html.is_file()
                    or hashlib.sha256(final_html.read_bytes()).hexdigest() != receipt["html_sha256"]):
                raise ValueError("已发布 HTML 快照被修改，禁止交付")
        with run_lock(output_dir):
            if not alias.exists():
                run_store.atomic_bytes(alias, final.read_bytes())
            elif alias.read_bytes() != final.read_bytes():
                raise ValueError("当日发布入口已变更，禁止覆盖")
            html_alias = None
            if final_html is not None:
                html_alias = alias.with_suffix(".html")
                if not html_alias.exists():
                    run_store.atomic_bytes(html_alias, final_html.read_bytes())
                elif html_alias.read_bytes() != final_html.read_bytes():
                    raise ValueError("当日 HTML 发布入口已变更，禁止覆盖")
            cache = _date_cache(directory, cand)
            run_store.atomic_bytes(cache, final_cand.read_bytes())
            if brief_record.record_brief(str(alias), str(cache),
                                         html_path=str(html_alias) if html_alias else None) is None:
                raise ValueError("简报登记失败，不交付；保留快照供幂等重试")
        return alias
    with run_lock(output_dir):
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
        # HTML 交付物：存在则必须与 canonical MD/候选严格一致才随本批发布；
        # 批次声明 delivery=html（agent 管线）时缺 HTML 直接拒绝发布。
        payload = json.loads(candidate_bytes)
        html_file = Path(html_report.html_for_brief(brief))
        html_bytes = None
        if html_file.is_file():
            ok_html, html_errors = html_report.verify_html(
                str(html_file), str(brief), str(cand))
            if not ok_html:
                raise ValueError("HTML 与 canonical 不一致，禁止发布: %s"
                                 % "；".join(html_errors[:5]))
            html_bytes = html_file.read_bytes()
        elif payload.get("delivery") == "html":
            raise ValueError("本批次声明 HTML 交付但缺少已验收 HTML: %s" % html_file)
        receipts = list((directory / "validation").glob("*/receipt.json"))
        latest = max(receipts, key=lambda p: p.stat().st_mtime_ns)
        check = json.loads(latest.read_text())
        ch = hashlib.sha256(candidate_bytes).hexdigest()
        bh = hashlib.sha256(brief_bytes).hexdigest()
        if check["status"] != "PASS" or (check["candidate_sha256"], check["brief_sha256"]) != (ch, bh):
            raise ValueError("校验后内容变更，禁止发布")
        final_cand = directory / "final" / cand.name
        final_brief = brief.parent / "final" / brief.name
        run_store.write_once(final_cand, candidate_bytes)
        run_store.write_once(final_brief, brief_bytes)
        receipt = {"run_id": manifest["run_id"], "date": manifest["date"],
                   "brief_path": str(final_brief), "candidate_path": str(final_cand),
                   "brief_sha256": bh, "candidate_sha256": ch, "validation_receipt": str(latest)}
        if html_bytes is not None:
            final_html = brief.parent / "final" / html_file.name
            run_store.write_once(final_html, html_bytes)
            receipt["html_path"] = str(final_html)
            receipt["html_sha256"] = hashlib.sha256(html_bytes).hexdigest()
        run_store.write_once(directory / "published.json", run_store.json_bytes(receipt))
        run_store.atomic_bytes(alias, brief_bytes)
        html_alias = None
        if html_bytes is not None:
            html_alias = alias.with_suffix(".html")
            run_store.atomic_bytes(html_alias, html_bytes)
        cache = _date_cache(directory, cand)
        run_store.atomic_bytes(cache, candidate_bytes)
        if brief_record.record_brief(str(alias), str(cache),
                                     html_path=str(html_alias) if html_alias else None) is None:
            raise ValueError("简报登记失败，不交付；保留快照供幂等重试")
        return alias


def finalize(cand, brief, output_dir=None):
    cand, brief = Path(cand).resolve(), Path(brief).resolve()
    output_dir = Path(output_dir or collect_brief.OUTPUT_DIR)
    delivered = _delivered_alias(cand, brief, output_dir)
    if delivered:
        _cleanup_runs(cand, brief, delivered)
        return str(delivered)
    directory = run_store.managed_run(cand)
    if directory is None:
        raise ValueError("须使用采集 stdout 给出的独立批次候选路径")
    with run_lock(directory):
        alias = _publish_locked(directory, cand, brief, output_dir)
    _cleanup_runs(cand, brief, alias)
    return str(alias)


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
