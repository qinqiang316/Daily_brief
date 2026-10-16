"""Per-run artifacts, immutable evidence and publication receipts (stdlib only)."""
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class DeadlineBudget:
    """Monotonic pipeline deadline; derived phases can never extend it."""
    def __init__(self, deadline):
        self.deadline = float(deadline)
    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())
    def expired(self):
        return self.remaining() <= 0
    def timeout(self, cap):
        return min(float(cap), self.remaining())
    def derive(self, seconds):
        return DeadlineBudget(min(self.deadline, time.monotonic() + max(0, seconds)))
    def require(self, cap=None):
        left = self.remaining()
        if left <= 0:
            raise ValueError("pipeline deadline exceeded")
        return min(left, cap) if cap is not None else left


def safe_run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
        raise ValueError("非法 run_id")
    return value


def atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_once(path, content):
    """Never replace an existing snapshot; a conflicting rerun is an error."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".snapshot_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)  # Atomic publish, without ever replacing an existing inode.
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError("快照已存在且内容不同: %s" % path)
    finally:
        os.unlink(tmp)


def archive_tree(source, target):
    """Copy immutable bytes, refusing symlinks and conflicting existing evidence."""
    source, target = Path(source), Path(target)
    if not source.exists():
        return
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("证据不允许符号链接")
        if path.is_file() and path.name not in ("writer.lock", "published.json"):
            write_once(target / path.relative_to(source), path.read_bytes())


def remap_paths(value, mappings):
    if isinstance(value, dict):
        return {k: remap_paths(v, mappings) for k, v in value.items()}
    if isinstance(value, list):
        return [remap_paths(v, mappings) for v in value]
    if isinstance(value, str):
        for old, new in mappings:
            if value == old or value.startswith(old + "/"):
                return new + value[len(old):]
            # macOS /var and /private/var are the same file tree. Evidence
            # paths supplied before resolve() must survive working-dir cleanup.
            if value.startswith('/'):
                try:
                    relative = Path(value).resolve().relative_to(Path(old).resolve())
                except ValueError:
                    continue
                return str(Path(new) / relative)
    return value


def verify_agent_evidence(payload):
    """Fail closed on missing source snapshots or independently identified CLI sessions."""
    if payload.get("pipeline", {}).get("mode") != "agent":
        return
    if payload.get("pipeline", {}).get("semantic_review") is not None:
        _verify_hermes_review_evidence(payload)
        return
    from modules import agent_lane, filter as fm
    for lane, oc in (payload.get("agent_review") or {}).items():
        if oc.get("status") != "ok":
            continue
        directory = Path(oc.get("lane_dir") or "")
        sessions = oc.get("sessions") or {}
        ids = []
        for role in ("author", "reviewer"):
            for session in sessions.get(role) or []:
                kind = session.get("executor") or agent_lane.LANES[lane]["cli"]
                sid = agent_lane.extract_session_id(kind, session["log"])
                if kind == "hermes":
                    from modules.executor import hermes_identity
                    identity = hermes_identity(session["log"])
                    if (identity.get("model") != session.get("model") or identity.get("provider") != session.get("provider")
                            or identity.get("completed") is not True or identity.get("failed") is not False
                            or identity.get("partial") or identity.get("interrupted")):
                        raise ValueError("Hermes runtime模型/provider凭据不符")
                if session.get("rc") != 0 or not sid or sid != session.get("session"):
                    raise ValueError("真实会话记录缺失/不符")
                ids.append(sid)
        if len(ids) < 2 or len(ids) != len(set(ids)):
            raise ValueError("作者复核会话非独立")
        r = oc.get("rounds")
        author = json.loads((directory / ("author_result_r%d.json" % r)).read_text())
        review = json.loads((directory / ("review_r%d.json" % r)).read_text())
        originals = json.loads((directory / ("source_evidence_r%d.json" % r)).read_text())
        au = [x["url"] for x in author["items"]]
        ru = [x["url"] for x in review["items"]]
        if (author.get("run_id") != payload["run_id"] or author.get("lane") != lane
                or review.get("run_id") != payload["run_id"] or review.get("lane") != lane
                or review.get("round") != r or review.get("verdict") != "pass"
                or len(au) != len(set(au)) or len(ru) != len(set(ru)) or set(au) != set(ru)):
            raise ValueError("归档复核覆盖/批次不符")
        for item, verdict in ((it, next(v for v in review["items"] if v["url"] == it["url"])) for it in author["items"]):
            original = originals.get(item["url"]) or {}
            text = original.get("text") or ""
            ev = fm.extract_date_evidence(dict(item, content=text, metadata=original.get("metadata") or {}))
            digest = hashlib.sha256(re.sub(r"\s+", "", text).encode()).hexdigest()
            if (verdict.get("pass") is not True or verdict.get("content_sha256") != digest
                    or verdict.get("date_evidence") != ev["evidence"]
                    or verdict.get("publish_date") != ev["publish_date"]
                    or item["publish_date"] != ev["publish_date"]
                    or agent_lane.wc(text) < agent_lane.MIN_BODY_WORDS
                    or re.sub(r"\s+", "", item["content"]) != re.sub(r"\s+", "", text)):
                raise ValueError("归档原文复核证据不符")
        candidate_map = {c["url"]: c for key in ("candidates", "source_leftovers") for c in payload.get(key, [])}
        for it in author["items"]:
            c = candidate_map.get(fm.norm_url(it["url"]))
            if c:
                original = originals[it["url"]]
                normalized = re.sub(r"\s+", " ", original["text"]).strip()[:fm.CONTENT_LIMIT]
                if (re.sub(r"\s+", "", c["content"]) != re.sub(r"\s+", "", normalized)
                        or c["publish_date"] != it["publish_date"]
                        or c.get("metadata", {}) != original.get("metadata", {})):
                    raise ValueError("候选正文/metadata与原文复核证据不一致")
        for role in ("author", "review"):
            if not (directory / ("%s_prompt_r%d.txt" % (role, r))).is_file():
                raise ValueError("归档prompt缺失")


def _verify_hermes_review_evidence(payload):
    """Validate one author session + immutable script evidence + recorded Hermes review."""
    from modules import agent_lane, filter as fm
    if payload.get("pipeline", {}).get("semantic_review") != "complete":
        raise ValueError("Hermes语义复核尚未完成")
    for lane, oc in (payload.get("agent_review") or {}).items():
        if oc.get("status") != "ok":
            continue
        review = oc.get("semantic_review") or {}
        if (review.get("status") != "complete" or review.get("reviewer") != "Hermes"
                or not review.get("reviewed_at") or not isinstance(review.get("findings"), list)):
            raise ValueError("车道 %s 缺Hermes语义复核记录" % lane)
        directory = Path(oc.get("lane_dir") or "")
        sessions = oc.get("sessions") or {}
        authors = sessions.get("author") or []
        if len(authors) != 1 or sessions.get("reviewer"):
            raise ValueError("车道 %s 必须恰有一次作者会话且无模型复核会话" % lane)
        session = authors[0]
        kind = session.get("executor") or agent_lane.LANES[lane]["cli"]
        sid = agent_lane.extract_session_id(kind, session["log"])
        if kind == "hermes":
            from modules.executor import hermes_identity
            identity = hermes_identity(session["log"])
            if (identity.get("model") != session.get("model")
                    or identity.get("provider") != session.get("provider")
                    or identity.get("completed") is not True or identity.get("failed") is not False
                    or identity.get("partial") or identity.get("interrupted")):
                raise ValueError("Hermes作者接管runtime凭据不符")
        if session.get("rc") != 0 or not sid or sid != session.get("session"):
            raise ValueError("小g作者会话记录缺失/不符")
        author = json.loads((directory / "author_result_r1.json").read_text())
        accepted = json.loads((directory / "accepted_items.json").read_text())
        originals = json.loads((directory / "source_evidence.json").read_text())
        mechanical = json.loads((directory / "mechanical_review.json").read_text())
        if (author.get("run_id") != payload["run_id"] or author.get("lane") != lane
                or accepted.get("run_id") != payload["run_id"] or accepted.get("lane") != lane
                or mechanical.get("run_id") != payload["run_id"]
                or mechanical.get("lane") != lane or mechanical.get("status") != "complete"):
            raise ValueError("车道 %s 机械证据批次/状态错配" % lane)
        source_by_url = {item.get("url"): item for item in author.get("items", [])
                         if isinstance(item, dict)}
        if any(item.get("url") not in source_by_url for item in accepted.get("items", [])):
            raise ValueError("脚本接受了作者未提交的URL")
        candidate_map = {c.get("url"): c for key in ("candidates", "source_leftovers")
                         for c in payload.get(key, [])}
        for item in accepted.get("items", []):
            url = item["url"]
            original = originals.get(url) or {}
            text = original.get("text") or ""
            ev = fm.extract_date_evidence(dict(item, content=text,
                                               metadata=original.get("metadata") or {}))
            digest = hashlib.sha256(re.sub(r"\s+", "", text).encode()).hexdigest()
            if (not text or agent_lane.wc(text) < agent_lane.MIN_BODY_WORDS
                    or ev["precision"] != "day" or ev["publish_date"] != item.get("publish_date")
                    or re.sub(r"\s+", "", item.get("content", "")) != re.sub(r"\s+", "", text)
                    or item.get("source_verification", {}).get("content_sha256") != digest):
                raise ValueError("车道 %s 原文/日期/hash证据不符" % lane)
            candidate = candidate_map.get(fm.norm_url(url)) or candidate_map.get(url)
            if candidate and (candidate.get("publish_date") != item.get("publish_date")
                    or re.sub(r"\s+", "", candidate.get("content", ""))
                       != re.sub(r"\s+", "", re.sub(r"\s+", " ", text).strip()[:fm.CONTENT_LIMIT])):
                raise ValueError("候选正文与已核验原文不一致")
        if not (directory / "author_prompt.txt").is_file():
            raise ValueError("小g作者prompt缺失")


def candidate_path(cand_dir, day, run_id):
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", day):
        raise ValueError("非法批次日期")
    datetime.strptime(day, "%Y-%m-%d")
    return Path(cand_dir) / "runs" / safe_run_id(run_id) / ("Daily-Brief-%s-candidates.json" % day)


def brief_path(output_dir, day, run_id):
    candidate_path(output_dir, day, run_id)  # Validate both path components.
    return Path(output_dir) / "runs" / safe_run_id(run_id) / ("Daily-Brief-%s.md" % day)


def html_path(output_dir, day, run_id):
    candidate_path(output_dir, day, run_id)  # Validate both path components.
    return Path(output_dir) / "runs" / safe_run_id(run_id) / ("Daily-Brief-%s.html" % day)


def create_run(cand_dir, output_dir, day, payload):
    run_id = safe_run_id(payload.get("run_id"))
    path = candidate_path(cand_dir, day, run_id)
    content = json_bytes(payload)
    write_once(path.parent / "collected.json", content)
    write_once(path, content)  # Working copy: reject/replenish only within this run.
    out = brief_path(output_dir, day, run_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"run_id": run_id, "date": day, "candidates": str(path.resolve()),
                "brief": str(out.resolve()),
                "html": str(html_path(output_dir, day, run_id).resolve()),
                "collected_sha256": hashlib.sha256(content).hexdigest()}
    write_once(path.parent / "manifest.json", json_bytes(manifest))
    return str(path.resolve()), str(out.resolve())


def resolve_candidate(cand_dir, day, run_id):
    path = candidate_path(cand_dir, day, run_id)
    final = path.parent / "final" / path.name
    if (path.parent / "published.json").is_file() and final.is_file():
        return str(final)
    return str(path) if path.is_file() else None


def managed_run(cand):
    path = Path(cand).resolve()
    if path.parent.name == "final" and path.parent.parent.parent.name == "runs":
        return path.parent.parent
    return path.parent if path.parent.parent.name == "runs" else None


def save_validation(cand, brief, candidate_bytes, brief_bytes, status, output):
    directory = managed_run(cand)
    if directory is None:
        return None  # Legacy files remain compatible and tests stay isolated.
    payload = json.loads(candidate_bytes)
    if directory.name != safe_run_id(payload.get("run_id")):
        raise ValueError("候选目录与 run_id 不一致")
    attempt = directory / "validation" / uuid.uuid4().hex
    write_once(attempt / "candidates.json", candidate_bytes)
    write_once(attempt / "brief.md", brief_bytes)
    receipt = {"run_id": payload["run_id"], "status": status,
               "checked_at": datetime.now(timezone.utc).isoformat(),
               "candidate_path": str(Path(cand).resolve()), "brief_path": str(Path(brief).resolve()),
               "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
               "brief_sha256": hashlib.sha256(brief_bytes).hexdigest(), "output": output}
    write_once(attempt / "receipt.json", json_bytes(receipt))
    return str(attempt / "receipt.json")
