"""End-to-end batch isolation and publication evidence; all data in temporary dirs."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, SCRIPTS  # noqa: F401
from tests.test_validate import cand_payload, BRIEF_TMPL, GOOD_URL
from modules import run_store
import collect_brief
import validate_brief
import finalize_brief
import run_agy
import brief_record


class RunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.candidates = self.root / "_candidates"
        self.output = self.root / "output"
        self.records = self.root / "data/records.json"
        self.patches = [mock.patch.object(validate_brief, "CAND_DIR", str(self.candidates)),
                        mock.patch.object(validate_brief, "OUTPUT_DIR", str(self.output)),
                        mock.patch.object(collect_brief, "OUTPUT_DIR", str(self.output)),
                        mock.patch.object(brief_record, "RECORDS_FILE", str(self.records))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def create(self, rid="first", write_brief=True):
        payload = cand_payload(run_id=rid)
        cand, brief = run_store.create_run(self.candidates, self.output, "2026-10-08", payload)
        if write_brief:
            Path(brief).write_text(BRIEF_TMPL.format(run_id=rid, url=GOOD_URL))
        return Path(cand), Path(brief)

    def check(self, args):
        with mock.patch.object(sys, "argv", ["validate_brief.py"] + list(map(str, args))):
            return validate_brief.main()

    def test_second_collection_cannot_replace_first_run(self):
        cand, brief = self.create()
        old = cand.read_bytes()
        second, _ = self.create("second")
        self.assertNotEqual(cand, second)
        self.assertEqual(cand.read_bytes(), old)
        self.assertEqual(json.loads((cand.parent / "collected.json").read_text())["run_id"], "first")

    def test_default_validation_resolves_brief_run_not_latest_alias(self):
        cand, brief = self.create()
        second, _ = self.create("second", write_brief=False)
        self.candidates.joinpath(cand.name).write_bytes(second.read_bytes())
        self.assertEqual(self.check([brief]), 0)
        self.assertFalse(self.records.exists())  # Validation alone is not delivery.
        receipts = list((cand.parent / "validation").glob("*/receipt.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())["run_id"], "first")

    def test_collected_snapshot_conflict_refused(self):
        cand, _ = self.create()
        payload = cand_payload(run_id="first")
        payload["candidates"][0]["content"] = "changed"
        with self.assertRaises(ValueError):
            run_store.create_run(self.candidates, self.output, "2026-10-08", payload)

    def test_path_traversal_refused(self):
        for rid in ("../bad", "a/b", "", None):
            with self.subTest(rid=rid), self.assertRaises(ValueError):
                run_store.candidate_path(self.candidates, "2026-10-08", rid)

    def test_changes_during_validation_fail_and_save_failed_evidence(self):
        cand, brief = self.create()
        original = validate_brief._validate_main
        def mutate(*args, **kwargs):
            rc = original(*args, **kwargs)
            brief.write_text(brief.read_text() + " changed")
            return rc
        with mock.patch.object(validate_brief, "_validate_main", side_effect=mutate):
            self.assertEqual(self.check([brief, cand]), 1)
        receipt = next((cand.parent / "validation").glob("*/receipt.json"))
        self.assertEqual(json.loads(receipt.read_text())["status"], "FAIL")
        self.assertFalse(self.records.exists())

    def test_finalize_stores_evidence_and_publishes_immutable_attachment(self):
        cand, brief = self.create()
        final = Path(finalize_brief.finalize(cand, brief))
        self.assertIn("final", final.parts)
        self.assertEqual(final.read_bytes(), (self.output / brief.name).read_bytes())
        self.assertEqual(len(list((cand.parent / "validation").glob("*/receipt.json"))), 2)
        record = brief_record.load_records()[0]
        self.assertEqual(record["run_id"], "first")
        self.assertEqual(record["brief_path"], str(final))
        self.assertEqual(self.check([final]), 0)  # Its publication alias is not historical reuse.
        self.assertEqual(finalize_brief.finalize(cand, brief), str(final))
        self.assertEqual(len(brief_record.load_records()), 1)

    def test_new_run_cannot_overwrite_published_day(self):
        cand, brief = self.create()
        final = Path(finalize_brief.finalize(cand, brief))
        before = final.read_bytes()
        cand2, brief2 = self.create("second")
        with self.assertRaises(ValueError):
            finalize_brief.finalize(cand2, brief2)
        self.assertEqual((self.output / brief.name).read_bytes(), before)

    def test_tampered_published_candidate_refused(self):
        cand, brief = self.create()
        finalize_brief.finalize(cand, brief)
        final_cand = cand.parent / "final" / cand.name
        final_cand.write_text("{}")
        with self.assertRaises(ValueError):
            finalize_brief.finalize(cand, brief)

    def test_busy_writer_prevents_finalize(self):
        cand, brief = self.create()
        with finalize_brief.run_lock(cand.parent), self.assertRaises(ValueError):
            finalize_brief.finalize(cand, brief)
        self.assertFalse((self.output / brief.name).exists())

    def test_invalid_brief_never_published(self):
        cand, brief = self.create()
        brief.write_text(BRIEF_TMPL.format(run_id="wrong", url=GOOD_URL))
        with self.assertRaises(ValueError):
            finalize_brief.finalize(cand, brief)
        self.assertFalse((self.output / brief.name).exists())

    def test_agy_duplicate_is_rejected_without_starting_process(self):
        cand, brief = self.create()
        prompt = self.root / "prompt.txt"
        prompt.write_text("first " + str(cand) + " " + str(brief))
        with finalize_brief.run_lock(cand.parent), mock.patch.object(run_agy.subprocess, "Popen") as popen:
            with self.assertRaises(ValueError):
                run_agy.run(cand, prompt)
            popen.assert_not_called()

    def test_agy_nonzero_is_failure_even_when_artifact_exists(self):
        cand, brief = self.create()
        prompt = self.root / "prompt.txt"
        prompt.write_text("first " + str(cand) + " " + str(brief))
        fake = self.root / "fake-agy"
        fake.write_text("#!/bin/sh\nexit 7\n")
        fake.chmod(0o700)
        with mock.patch.dict(os.environ, {"DAILYBRIEF_AGY": str(fake)}):
            self.assertEqual(run_agy.run(cand, prompt), 7)
        receipt = next((cand.parent / "agy").glob("*.json"))
        self.assertEqual(json.loads(receipt.read_text())["exit_code"], 7)

    def test_agy_real_timeout_ends_process_and_releases_run_lock(self):
        cand, brief = self.create()
        prompt = self.root / "prompt.txt"
        prompt.write_text("first " + str(cand) + " " + str(brief))
        fake = self.root / "fake-agy"
        fake.write_text("#!/bin/sh\nsleep 30\n")
        fake.chmod(0o700)
        start = time.monotonic()
        with mock.patch.dict(os.environ, {"DAILYBRIEF_AGY": str(fake)}):
            self.assertEqual(run_agy.run(cand, prompt, timeout=0.1), 124)
        self.assertLess(time.monotonic() - start, 3)
        with finalize_brief.run_lock(cand.parent):
            pass  # A retry can acquire the lock only after the process was ended.

    def test_validation_checks_frozen_inputs_during_temporary_rewrite(self):
        cand, brief = self.create()
        original = validate_brief._validate_main
        before = cand.read_bytes()
        def temporary_rewrite(*args, **kwargs):
            altered = json.loads(before)
            altered["run_id"] = "wrong"
            cand.write_text(json.dumps(altered))
            try:
                return original(*args, **kwargs)
            finally:
                cand.write_bytes(before)
        with mock.patch.object(validate_brief, "_validate_main", side_effect=temporary_rewrite):
            self.assertEqual(self.check([brief, cand]), 0)
        receipt = next((cand.parent / "validation").glob("*/receipt.json"))
        self.assertEqual((receipt.parent / "candidates.json").read_bytes(), before)

    def test_registration_failure_prevents_delivery_and_can_be_retried(self):
        cand, brief = self.create()
        with mock.patch.object(brief_record, "record_brief", return_value=None):
            with self.assertRaises(ValueError):
                finalize_brief.finalize(cand, brief)
        final = finalize_brief.finalize(cand, brief)
        self.assertTrue(Path(final).is_file())
        self.assertEqual(len(brief_record.load_records()), 1)

    def test_timeout_kills_survivors_when_parent_exits_on_term(self):
        cand, brief = self.create()
        prompt = self.root / "prompt.txt"
        prompt.write_text("first " + str(cand) + " " + str(brief))
        proc = mock.Mock(pid=12345)
        proc.wait.side_effect = [run_agy.subprocess.TimeoutExpired("agy", 1), -15]
        with mock.patch.object(run_agy.subprocess, "Popen", return_value=proc), \
                mock.patch.object(run_agy.os, "killpg") as killpg:
            self.assertEqual(run_agy.run(cand, prompt, timeout=1), 124)
        self.assertEqual(killpg.call_args_list,
                         [mock.call(12345, run_agy.signal.SIGTERM), mock.call(12345, run_agy.signal.SIGKILL)])
