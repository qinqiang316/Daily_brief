"""Per-run artifacts, immutable evidence and publication receipts (stdlib only)."""
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


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


def candidate_path(cand_dir, day, run_id):
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", day):
        raise ValueError("非法批次日期")
    datetime.strptime(day, "%Y-%m-%d")
    return Path(cand_dir) / "runs" / safe_run_id(run_id) / ("Daily-Brief-%s-candidates.json" % day)


def brief_path(output_dir, day, run_id):
    candidate_path(output_dir, day, run_id)  # Validate both path components.
    return Path(output_dir) / "runs" / safe_run_id(run_id) / ("Daily-Brief-%s.md" % day)


def create_run(cand_dir, output_dir, day, payload):
    run_id = safe_run_id(payload.get("run_id"))
    path = candidate_path(cand_dir, day, run_id)
    content = json_bytes(payload)
    write_once(path.parent / "collected.json", content)
    write_once(path, content)  # Working copy: reject/replenish only within this run.
    out = brief_path(output_dir, day, run_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"run_id": run_id, "date": day, "candidates": str(path.resolve()),
                "brief": str(out.resolve()), "collected_sha256": hashlib.sha256(content).hexdigest()}
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
