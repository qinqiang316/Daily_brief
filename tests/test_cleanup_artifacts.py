import json
import tempfile
import time
import unittest
from pathlib import Path

from scripts.cleanup_artifacts import cleanup


class CleanupArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "artifacts"
        self.jobs = Path(self.tmp.name) / "jobs"
        self.root.mkdir()
        self.jobs.mkdir()
        self.now = 2_000_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def batch(self, relative, run_id, created):
        path = self.root / relative / run_id
        path.mkdir(parents=True)
        (path / ".created_at").write_text(str(created))
        (path / "artifact.json").write_text("temporary")
        return path

    def ticket(self, run_id, status, created):
        directory = self.jobs / run_id
        directory.mkdir()
        (directory / "ticket.json").write_text(json.dumps({
            "run_id": run_id, "status": status, "created_at": created,
            "acknowledged_at": created,
        }))
        return directory

    def test_age_threshold_cleans_only_expired_nonactive_batches(self):
        expired = self.batch("_candidates/runs", "expired", self.now - 48 * 3600)
        young = self.batch("briefs/runs", "young", self.now - 48 * 3600 + 1)
        active = self.batch("final", "active", self.now - 72 * 3600)
        self.ticket("active", "ready", self.now - 72 * 3600)
        result = cleanup(self.root, self.jobs, now=self.now)
        self.assertEqual(result["batches"], 1)
        self.assertFalse(expired.exists())
        self.assertTrue(young.exists())
        self.assertTrue(active.exists())

    def test_cache_and_acknowledged_job_expire_but_output_and_unacked_job_do_not(self):
        cache_root = self.root / "_candidates"
        cache_root.mkdir()
        cache = cache_root / "Daily-Brief-2026-01-01-candidates.json"
        cache.write_text("cache")
        old = self.now - 48 * 3600
        import os
        os.utime(cache, (old, old))
        completed = self.ticket("done", "acknowledged", old)
        pending = self.ticket("pending", "failed", old)
        result = cleanup(self.root, self.jobs, now=self.now)
        self.assertEqual(result["cache_files"], 1)
        self.assertEqual(result["jobs"], 1)
        self.assertFalse(cache.exists())
        self.assertFalse(completed.exists())
        self.assertTrue(pending.exists())

    def test_symlink_inside_batch_prevents_batch_deletion(self):
        target = Path(self.tmp.name) / "outside.txt"
        target.write_text("untouched")
        batch = self.batch("final", "linked", self.now - 72 * 3600)
        (batch / "link").symlink_to(target)
        result = cleanup(self.root, self.jobs, now=self.now)
        self.assertEqual(result["batches"], 0)
        self.assertTrue(target.exists())
        self.assertTrue(batch.exists())

    def test_cleanup_never_touches_output_tree(self):
        output = Path(self.tmp.name) / "output"
        output.mkdir()
        brief = output / "Daily-Brief-2026-10-17.md"
        brief.write_text("deliverable")
        (output / "writer.lock").write_text("unrelated existing lock")
        cleanup(self.root, self.jobs, now=self.now)
        self.assertTrue(brief.exists())
        self.assertTrue((output / "writer.lock").exists())

    def test_symlink_artifact_root_is_not_followed(self):
        expired = self.batch("final", "expired", self.now - 72 * 3600)
        alias = Path(self.tmp.name) / "artifact-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        result = cleanup(alias, self.jobs, now=self.now)
        self.assertEqual(result, {"batches": 0, "cache_files": 0, "jobs": 0})
        self.assertTrue(expired.exists())


if __name__ == "__main__":
    unittest.main()
