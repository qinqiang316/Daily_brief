#!/usr/bin/env python3
"""Safely expire DailyBrief temporary batch material after 48 hours."""
import json
import os
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from modules import run_store
import pipeline_job

RETENTION_SECONDS = 48 * 60 * 60


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _has_symlink_component(path):
    path = Path(path).expanduser()
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current == Path("/var") and current.is_symlink() and current.resolve() == Path("/private/var"):
            continue  # macOS's standard /var alias; temporary test roots commonly use it.
        if current.is_symlink():
            return True
    return False


def _active_run_ids(jobs_root):
    active = set()
    jobs_root = Path(jobs_root)
    if not jobs_root.is_dir() or jobs_root.is_symlink():
        return active
    for ticket in jobs_root.glob("*/ticket.json"):
        try:
            state = json.loads(ticket.read_text())
        except (OSError, ValueError):
            active.add(ticket.parent.name)
            continue
        if state.get("status") in {"queued", "running", "ready", "failed", "claimed"}:
            active.add(ticket.parent.name)
    return active


def _created_at(directory):
    marker = directory / ".created_at"
    try:
        return float(marker.read_text().strip())
    except (OSError, ValueError):
        return None


def _remove_batch(directory, root, now, active):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir() or not _inside(directory, root):
        return False
    if directory.name in active:
        return False
    created = _created_at(directory)
    if created is None or now - created < RETENTION_SECONDS:
        return False
    # Refuse unusual trees rather than risk following an unexpected link.
    if any(path.is_symlink() for path in directory.rglob("*")):
        return False
    shutil.rmtree(directory)
    return True


def cleanup(artifact_root=None, jobs_root=None, now=None):
    """Delete only expired material inside the dedicated artifact and job roots."""
    now = time.time() if now is None else float(now)
    artifact_root = Path(artifact_root or run_store.ARTIFACT_ROOT).expanduser()
    jobs_root = Path(jobs_root or pipeline_job.DEFAULT_JOBS).expanduser()
    if _has_symlink_component(artifact_root) or not artifact_root.is_dir():
        return {"batches": 0, "cache_files": 0, "jobs": 0}
    if _has_symlink_component(jobs_root):
        return {"batches": 0, "cache_files": 0, "jobs": 0}
    root = artifact_root.resolve()
    active = _active_run_ids(jobs_root)
    removed_batches = 0
    for relative in ("_candidates/runs", "briefs/runs", "final"):
        parent = root / relative
        if (_has_symlink_component(parent) or not parent.is_dir()
                or not _inside(parent, root)):
            continue
        for child in list(parent.iterdir()):
            if child.name == "runs" and relative == "final":
                continue
            if _remove_batch(child, root, now, active):
                removed_batches += 1
    cache_files = 0
    cache_root = root / "_candidates"
    if cache_root.is_dir() and not _has_symlink_component(cache_root):
        for path in cache_root.iterdir():
            if path.is_file() and not path.is_symlink() and path.name.startswith("Daily-Brief-"):
                try:
                    if now - path.stat().st_mtime >= RETENTION_SECONDS:
                        path.unlink()
                        cache_files += 1
                except OSError:
                    continue
    removed_jobs = 0
    if jobs_root.is_dir() and not _has_symlink_component(jobs_root):
        job_root_resolved = jobs_root.resolve()
        for directory in list(jobs_root.iterdir()):
            ticket = directory / "ticket.json"
            if directory.is_symlink() or not directory.is_dir() or not _inside(directory, job_root_resolved):
                continue
            try:
                state = json.loads(ticket.read_text())
                terminal_at = float(state.get("acknowledged_at", 0))
            except (OSError, ValueError, TypeError):
                continue
            if (state.get("status") == "acknowledged" and terminal_at
                    and now - terminal_at >= RETENTION_SECONDS
                    and not any(path.is_symlink() for path in directory.rglob("*"))):
                shutil.rmtree(directory)
                removed_jobs += 1
    return {"batches": removed_batches, "cache_files": cache_files, "jobs": removed_jobs}


if __name__ == "__main__":
    print(json.dumps(cleanup(), ensure_ascii=False))
