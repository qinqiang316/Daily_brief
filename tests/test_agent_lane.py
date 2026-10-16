# -*- coding: utf-8 -*-
"""外部 CLI 车道（agent_lane）全分支测试：spawn 全部注入假实现，不触网不起真实 CLI。"""
import hashlib
import re
import json
from unittest import mock
import os
import tempfile
import time
import unittest

from tests.helpers import ROOT, SCRIPTS, long_body  # noqa: F401
from modules import agent_lane

RUN_ID = "20261014T010000-testlane"
GOOD_URL = "https://example.com/2026/10/13/good-article"


def make_task(lane_dir):
    return {
        "run_id": RUN_ID,
        "date": "2026-10-14",
        "window": {"start": "2026-10-13", "end": "2026-10-14"},
        "lane": "cn",
        "queries": [{"query": "site:example.com 深度", "max_results": 3,
                     "direction": "科技"}],
        "preference": {"pref_dir": None, "explore_dir": "生活", "likes_count": 0},
        "lane_dir": lane_dir,
        "result_file": os.path.join(lane_dir, "author_result.json"),
        "review_file_tmpl": os.path.join(lane_dir, "review_r{round}.json"),
    }


def good_item(**kw):
    it = {
        "title": "好文章", "url": GOOD_URL, "content": long_body(),
        "publish_date": "2026-10-13", "date_source": "url",
        "date_evidence": GOOD_URL, "date_precision": "day",
        "direction": "科技", "is_explore": False, "is_preferred": False,
        "source_key": "agent_cn:example", "source_label": "示例站",
    }
    it.update(kw)
    return it


def fake_spawn_factory(result_payloads, review_payloads, rc_plan=None):
    """按调用顺序回放行为：作者会话写 result，复核会话写 review。
    rc_plan: {调用序号(从1起): 退出码}，缺省 0；退出码非 0 时不写产物。"""
    calls = []

    def spawn(cmd, cwd, log_path, timeout, env=None):
        calls.append(list(cmd))
        with open(log_path, "ab") as f:
            f.write(("done\n00000000-0000-4000-8000-%012d\n" % len(calls)).encode())
        rc = (rc_plan or {}).get(len(calls), 0)
        if rc != 0:
            return rc
        prompt = cmd[-1]
        if "独立复核者" in prompt:
            path = prompt.split("把复核结论写成 JSON 文件：", 1)[1].splitlines()[0].strip()
            idx = sum(1 for c in calls if "独立复核者" in c[-1]) - 1
            payload = review_payloads[min(idx, len(review_payloads) - 1)]
        else:
            path = prompt.split("把结果写成 JSON 文件：", 1)[1].splitlines()[0].strip()
            idx = sum(1 for c in calls if "独立复核者" not in c[-1]) - 1
            payload = result_payloads[min(idx, len(result_payloads) - 1)]
        if isinstance(payload, dict) and "独立复核者" in prompt:
            payload = dict(payload, round=idx + 1)
        if payload is not None:
            with open(path, "w", encoding="utf-8") as f:
                if isinstance(payload, str):
                    f.write(payload)  # 非法 JSON 分支
                else:
                    json.dump(payload, f, ensure_ascii=False)
        return 0

    spawn.calls = calls
    return spawn


def ok_result(items=None):
    return {"run_id": RUN_ID, "lane": "cn", "items": items if items is not None else [good_item()]}


def review(verdict, items=None):
    return {"run_id": RUN_ID, "lane": "cn", "round": 1, "verdict": verdict,
            "items": items if items is not None else [{"url": GOOD_URL, "pass": True, "reasons": [],
                "publish_date": "2026-10-13", "date_evidence": GOOD_URL,
                "content_sha256": hashlib.sha256(re.sub(r"\s+", "", long_body()).encode()).hexdigest()}]}


class LaneCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.task = make_task(self.tmp.name)
        self.fetch = mock.patch.object(agent_lane, "fetch_texts_parallel", return_value={
            GOOD_URL: {"text": long_body(), "metadata": {}}})
        self.fetch.start()

    def tearDown(self):
        self.fetch.stop()
        self.tmp.cleanup()

    def run_lane(self, spawn, **kw):
        return agent_lane.run_lane(self.task, self.tmp.name, spawn=spawn, **kw)

    def test_hermes_review_flow_uses_one_author_and_marks_review_pending(self):
        spawn = fake_spawn_factory([ok_result()], [])
        oc = agent_lane.run_lane_for_hermes(self.task, self.tmp.name, spawn=spawn)
        self.assertEqual(oc["status"], "ok")
        self.assertEqual(len(oc["items"]), 1)
        self.assertEqual(len(getattr(spawn, "calls")), 1)
        self.assertEqual(oc["sessions"]["reviewer"], [])
        self.assertEqual(oc["review"]["verdict"], "pending_hermes")
        self.assertEqual(oc["semantic_review"]["status"], "pending")

    def test_hermes_review_flow_rejects_mechanical_problem_without_retry(self):
        spawn = fake_spawn_factory([ok_result([good_item(content="太短")])], [])
        oc = agent_lane.run_lane_for_hermes(self.task, self.tmp.name, spawn=spawn)
        self.assertEqual(oc["status"], "ok")
        self.assertEqual(oc["items"], [])
        self.assertEqual(len(getattr(spawn, "calls")), 1)
        self.assertFalse(oc["sessions"]["reviewer"])
        self.assertTrue(any("机械预检" in x["reason"]
                            for x in oc["mechanical_rejections"]))

    def test_pass_first_round(self):
        spawn = fake_spawn_factory([ok_result()], [review("pass")])
        oc = self.run_lane(spawn)
        self.assertEqual(oc["status"], "ok")
        self.assertEqual(len(oc["items"]), 1)
        self.assertEqual(oc["items"][0]["url"], GOOD_URL)
        self.assertEqual(oc["rounds"], 1)
        # 作者与复核是两个独立会话，各有真实会话标识与日志
        self.assertEqual(len(oc["sessions"]["author"]), 1)
        self.assertEqual(len(oc["sessions"]["reviewer"]), 1)
        self.assertEqual(oc["sessions"]["author"][0]["session"], "00000000-0000-4000-8000-000000000001")
        self.assertEqual(oc["sessions"]["reviewer"][0]["session"], "00000000-0000-4000-8000-000000000002")
        self.assertNotEqual(oc["sessions"]["author"][0]["log"],
                            oc["sessions"]["reviewer"][0]["log"])
        self.assertEqual(oc["review"]["verdict"], "pass")

    def test_fix_round_after_review_fail(self):
        spawn = fake_spawn_factory(
            [ok_result(), ok_result()],
            [review("fail", [{"url": GOOD_URL, "pass": False, "reasons": ["日期证据不符"]}]),
             review("pass")])
        oc = self.run_lane(spawn)
        self.assertEqual(oc["status"], "ok")
        self.assertEqual(oc["rounds"], 2)
        # 第二轮作者 prompt 必须带复核反馈（修正轮）
        second_author_prompt = spawn.calls[2][-1]
        self.assertIn("修正轮", second_author_prompt)
        self.assertIn("日期证据不符", second_author_prompt)

    def test_review_never_passes_is_real_failure(self):
        spawn = fake_spawn_factory(
            [ok_result(), ok_result()],
            [review("fail", [{"url": GOOD_URL, "pass": False, "reasons": ["正文不符"]}]),
             review("fail", [{"url": GOOD_URL, "pass": False, "reasons": ["仍不符"]}])])
        oc = self.run_lane(spawn)
        self.assertEqual(oc["status"], "failed")
        self.assertIn("复核仍未通过", oc["reason"])
        self.assertEqual(oc["items"], [])  # 未通过复核的产物不得返回
        self.assertEqual(len(oc["review"]["rounds"]), 2)

    def test_author_nonzero_exit(self):
        spawn = fake_spawn_factory([ok_result()], [review("pass")], rc_plan={1: 7})
        oc = self.run_lane(spawn)
        self.assertEqual(oc["status"], "failed")
        self.assertIn("rc=7", oc["reason"])
        self.assertEqual(oc["sessions"]["reviewer"], [])  # 作者失败不启动复核

    def test_timeout_is_failure(self):
        spawn = fake_spawn_factory([ok_result()], [review("pass")], rc_plan={1: 124})
        oc = self.run_lane(spawn)
        self.assertEqual(oc["status"], "failed")
        self.assertIn("rc=124", oc["reason"])

    def test_missing_result_file(self):
        spawn = fake_spawn_factory([None], [review("pass")])
        oc = self.run_lane(spawn)
        self.assertEqual(oc["status"], "failed")
        self.assertIn("未产出结果文件", oc["reason"])

    def test_malformed_result_json(self):
        spawn = fake_spawn_factory(["{not json"], [review("pass")])
        oc = self.run_lane(spawn)
        self.assertEqual(oc["status"], "failed")
        self.assertIn("无法解析", oc["reason"])

    def test_run_id_mismatch_rejected(self):
        bad = ok_result()
        bad["run_id"] = "otherrun"
        spawn = fake_spawn_factory([bad], [review("pass")])
        oc = self.run_lane(spawn)
        self.assertEqual(oc["status"], "failed")
        self.assertIn("run_id 错配", oc["reason"])

    def test_missing_review_is_failure(self):
        """复核会话没产出复核文件 = 未复核 = 真实故障，不得放行。"""
        spawn = fake_spawn_factory([ok_result()], [None])
        oc = self.run_lane(spawn)
        self.assertEqual(oc["status"], "failed")
        self.assertIn("未产出复核文件", oc["reason"])

    def test_mechanical_precheck_forces_fix_round(self):
        """复核 pass 但脚本预检发现正文不足 → 仍进修正轮，反馈含具体问题。"""
        bad = ok_result([good_item(content="太短")])
        spawn = fake_spawn_factory([bad, ok_result()], [review("pass"), review("pass")])
        oc = self.run_lane(spawn)
        self.assertEqual(oc["status"], "ok")
        self.assertEqual(oc["rounds"], 2)
        self.assertIn("正文不足", spawn.calls[2][-1])

    def test_precheck_rejects_window_outside_and_aggregate(self):
        items = [good_item(url="https://example.com/2026/09/01/old",
                           publish_date="2026-09-01"),
                 good_item(url="https://example.com/category/tech",
                           publish_date="2026-10-13")]
        ok, problems = agent_lane.check_items(items, self.task)
        self.assertEqual(ok, [])
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("窗口" in p for p in problems))
        self.assertTrue(any("聚合页" in p for p in problems))

    def test_empty_items_pass_review(self):
        """如实空池：作者给空 items、复核 pass → ok 且 items 为空（不凑数）。"""
        spawn = fake_spawn_factory([ok_result([])], [review("pass", [])])
        oc = self.run_lane(spawn)
        self.assertEqual(oc["status"], "ok")
        self.assertEqual(oc["items"], [])


class TestCliHarness(unittest.TestCase):
    def test_agy_flags_before_p(self):
        cmd = agent_lane.cli_command("agy", "任务正文", 60)
        self.assertEqual(cmd[-2], "-p")
        self.assertEqual(cmd[-1], "任务正文")
        self.assertIn("--dangerously-skip-permissions", cmd[:2])

    def test_kimi_command(self):
        cmd = agent_lane.cli_command("kimi", "任务正文", 60)
        self.assertEqual(cmd[-2], "-p")
        self.assertIn("--output-format", cmd)

    def test_unknown_cli_rejected(self):
        with self.assertRaises(ValueError):
            agent_lane.cli_command("别的", "x", 60)

    def test_run_cli_session_timeout_kills_group(self):
        """真实子进程超时：杀进程组并返回 124（本机 /bin/sh，不触网）。"""
        with tempfile.TemporaryDirectory() as d:
            log_path = os.path.join(d, "x.log")
            start = time.monotonic()
            rc = agent_lane.run_cli_session(["/bin/sh", "-c", "sleep 30"], d,
                                            log_path, timeout=0.2)
            self.assertEqual(rc, 124)
            self.assertLess(time.monotonic() - start, 10)

    def test_run_cli_session_success_rc(self):
        with tempfile.TemporaryDirectory() as d:
            rc = agent_lane.run_cli_session(["/bin/sh", "-c", "exit 3"], d,
                                            os.path.join(d, "x.log"), timeout=5)
            self.assertEqual(rc, 3)

    def test_extract_session_id(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "k.log")
            with open(p, "w") as f:
                f.write("输出若干\n续接：kimi -r session_abc123def\n")
            self.assertEqual(agent_lane.extract_session_id("kimi", p),
                             "session_abc123def")
            with open(p, "w") as f:
                f.write("conversation 123e4567-e89b-12d3-a456-426614174000 done")
            self.assertEqual(agent_lane.extract_session_id("agy", p),
                             "123e4567-e89b-12d3-a456-426614174000")
            with open(p, "w") as f:
                f.write("没有任何标识")
            self.assertIsNone(agent_lane.extract_session_id("kimi", p))


if __name__ == "__main__":
    unittest.main()
