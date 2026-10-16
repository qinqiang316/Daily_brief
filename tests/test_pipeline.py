# -*- coding: utf-8 -*-
"""agent 管线：prepare 任务上下文、车道合并准入、runner 端到端（车道/写作/HTML 全 mock）、
finalize 的 HTML 发布与幂等。全程不触网、不起真实 CLI。"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT, SCRIPTS, long_body  # noqa: F401
from tests.test_html_report import HTML_TMPL, MD_TMPL, VIEWPORT, RUN_ID, GOOD_URL, SUMMARY
from modules import agent_lane, filter as filter_mod, html_report, run_store
from modules import window as window_mod
import brief_record
import collect_brief
import finalize_brief
import prepare_brief
import run_pipeline
import validate_brief

DATE = "2026-10-14"


def lane_item(**kw):
    it = {
        "title": "好文章", "url": GOOD_URL, "content": long_body(),
        "publish_date": "2026-10-13", "date_source": "url",
        "date_evidence": GOOD_URL, "date_precision": "day",
        "direction": "科技", "is_explore": False, "is_preferred": False,
        "source_key": "agent_cn:example", "source_label": "示例站",
    }
    it.update(kw)
    return it


def lane_outcome(status, items=None, reason=None):
    return {"lane": "cn", "agent": "kimi", "status": status, "reason": reason,
            "items": items or [], "rounds": 1 if status == "ok" else 0,
            "sessions": {"author": [{"round": 1, "rc": 0, "session": "session_x",
                                     "log": "/tmp/a.log"}],
                         "reviewer": [{"round": 1, "rc": 0, "session": "session_y",
                                       "log": "/tmp/r.log"}]},
            "logs": ["/tmp/a.log", "/tmp/r.log"],
            "review": {"verdict": "pass" if status == "ok" else None,
                       "rounds": []}}


class PrepareCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cand_dir = os.path.join(self.tmp.name, "_candidates")
        self.out_dir = os.path.join(self.tmp.name, "output")
        self.dedup_file = os.path.join(self.tmp.name, "dedup.json")
        with open(self.dedup_file, "w") as f:
            json.dump([], f)
        self.patches = [
            mock.patch.object(filter_mod, "auto_update_dedup", return_value=set()),
            mock.patch.object(filter_mod, "DEDUP_FILE", self.dedup_file),
            mock.patch.object(window_mod, "get_latest_brief_date", return_value=None),
            mock.patch.object(prepare_brief.rank, "build_extra_queries",
                              return_value=([], None, "生活")),
            mock.patch.object(prepare_brief.rank.likes_mod, "load_likes",
                              return_value=[]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_prepare_writes_context_only(self):
        now = datetime(2026, 10, 14, 8, 0, tzinfo=prepare_brief.TZ)
        context, run_dir = prepare_brief.prepare(
            now=now, cand_dir=self.cand_dir, output_dir=self.out_dir)
        # 窗口/偏好/run_id 绑定
        self.assertEqual(context["window"], {"start": "2026-10-13", "end": "2026-10-14"})
        self.assertEqual(context["date"], DATE)
        self.assertTrue(context["run_id"])
        for lane in ("cn", "intl"):
            task = context["lanes"][lane]
            self.assertEqual(task["run_id"], context["run_id"])
            self.assertEqual(task["window"], context["window"])
            self.assertTrue(task["queries"])
            self.assertTrue(os.path.isfile(os.path.join(task["lane_dir"], "task.json")))
        # 上下文落盘且两路共享同一 window/run_id/去重信息
        ctx_file = os.path.join(run_dir, "prepare", "task_context.json")
        with open(ctx_file, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["run_id"], context["run_id"])
        self.assertEqual(saved["dedup"]["count"], 0)
        # 只生成任务上下文：不落候选/简报产物
        self.assertFalse(os.path.exists(context["batch"]["candidates"]))
        self.assertFalse(os.path.exists(context["batch"]["brief"]))
        self.assertIn(DATE, context["batch"]["html"])

    def test_prepare_twice_distinct_runs(self):
        now = datetime(2026, 10, 14, 8, 0, tzinfo=prepare_brief.TZ)
        c1, d1 = prepare_brief.prepare(now=now, cand_dir=self.cand_dir,
                                       output_dir=self.out_dir)
        c2, d2 = prepare_brief.prepare(now=now, cand_dir=self.cand_dir,
                                       output_dir=self.out_dir)
        self.assertNotEqual(c1["run_id"], c2["run_id"])
        self.assertNotEqual(d1, d2)  # 批次隔离：互不改写


class MergeCase(unittest.TestCase):
    """合并与统一准入：run_id 绑定、复核失败车道排除、空池熔断数据、探索标记保留。"""

    def setUp(self):
        self.patches = [
            mock.patch.object(filter_mod, "load_dedup", return_value=set()),
        ]
        for p in self.patches:
            p.start()
        self.context = {
            "run_id": RUN_ID, "date": DATE,
            "window": {"start": "2026-10-13", "end": "2026-10-14"},
            "preference": {"pref_dir": None, "explore_dir": "科技", "likes_count": 0},
            "lanes": {},
        }

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def build(self, outcomes, det=None):
        return run_pipeline.build_candidates_payload(
            self.context, outcomes, det or [], {}, {"hn": 0}, [], [])

    def test_merged_payload_binds_run_id_and_review(self):
        outcomes = {"cn": lane_outcome("ok", [lane_item(is_explore=True)]),
                    "intl": lane_outcome("failed", reason="作者会话异常退出 rc=1")}
        payload = self.build(outcomes)
        self.assertEqual(payload["run_id"], RUN_ID)
        self.assertEqual(payload["delivery"], "html")
        self.assertEqual(len(payload["candidates"]), 1)
        self.assertTrue(payload["candidates"][0]["is_explore"])  # 探索标识不丢失
        self.assertEqual(payload["pipeline"]["lane_status"]["intl"]["status"], "failed")
        self.assertEqual(payload["agent_review"]["cn"]["sessions"]["reviewer"][0]["session"],
                         "session_y")  # 复核记录随批次留存

    def test_failed_lane_items_excluded(self):
        """复核未通过/失败车道的产物不得进池；另一路失败不拖垮整池（单路失败降级）。"""
        outcomes = {"cn": lane_outcome("failed", reason="复核未通过"),
                    "intl": lane_outcome("ok", [lane_item()])}
        payload = self.build(outcomes)
        self.assertEqual(len(payload["candidates"]), 1)
        self.assertEqual(payload["candidates"][0]["url"], GOOD_URL)

    def test_lane_regions_survive_merge_and_rank_without_input_region(self):
        cn = lane_item(title="English language domestic source")
        intl = lane_item(url="https://example.com/2026/10/13/intl", title="中文国外来源")
        self.assertNotIn("region", cn)
        self.assertNotIn("region", intl)
        payload = self.build({"cn": lane_outcome("ok", [cn]), "intl": lane_outcome("ok", [intl])})
        by_url = {c["url"]: c for c in payload["candidates"]}
        for lane, original in (("cn", cn), ("intl", intl)):
            self.assertEqual(by_url[original["url"]]["region"], lane)
            self.assertEqual(by_url[original["url"]]["region_provenance"], {"kind": "lane", "lane": lane})

    def test_lane_overrides_untrusted_article_region(self):
        item = lane_item(region="intl", region_provenance={"kind": "lane", "lane": "intl"})
        payload = self.build({"cn": lane_outcome("ok", [item])})
        self.assertEqual(payload["candidates"][0]["region"], "cn")
        self.assertEqual(payload["candidates"][0]["region_provenance"], {"kind": "lane", "lane": "cn"})

    def test_rank_preserves_explicit_region_and_unknown_defaults_other(self):
        from modules import region
        configured = lane_item(url="https://example.com/2026/10/13/configured")
        region.bind_source(configured, "horizon:latepost", "中国")
        unknown = lane_item(title="中文标题不能证明地区")
        payload = self.build({}, [configured, unknown])
        by_url = {c["url"]: c for c in payload["candidates"]}
        self.assertEqual(by_url[configured["url"]]["region"], "cn")
        self.assertEqual(by_url[configured["url"]]["region_provenance"], configured["region_provenance"])
        self.assertEqual(by_url[unknown["url"]]["region"], "other")

    def test_empty_pool_returns_none(self):
        outcomes = {"cn": lane_outcome("failed", reason="超时"),
                    "intl": lane_outcome("failed", reason="超时")}
        self.assertIsNone(self.build(outcomes))  # 空池熔断，由调用方退出 1

    def test_lane_item_must_pass_unified_admission(self):
        """车道条目仍过统一准入：窗口外日期被 filter 拒收（闸门未削弱）。"""
        outcomes = {"cn": lane_outcome("ok", [lane_item(publish_date="2026-09-01",
                                                        url="https://example.com/2026/09/01/old")]),
                    "intl": lane_outcome("failed")}
        self.assertIsNone(self.build(outcomes))


class RunnerCase(unittest.TestCase):
    """runner 端到端：车道/确定性采集/agy 全 mock，validate/点赞/HTML 校验走真实代码。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cand_dir = Path(self.tmp.name) / "_candidates"
        self.out_dir = Path(self.tmp.name) / "output"
        self.records = Path(self.tmp.name) / "data/records.json"
        self.context = {
            "run_id": RUN_ID, "date": DATE,
            "window": {"start": "2026-10-13", "end": "2026-10-14"},
            "preference": {"pref_dir": None, "explore_dir": "科技", "likes_count": 0},
            "lanes": {
                "cn": {"run_id": RUN_ID, "lane": "cn"},
                "intl": {"run_id": RUN_ID, "lane": "intl"},
            },
            "batch": {
                "candidates": str(run_store.candidate_path(self.cand_dir, DATE, RUN_ID)),
                "brief": str(run_store.brief_path(self.out_dir, DATE, RUN_ID)),
                "html": str(run_store.html_path(self.out_dir, DATE, RUN_ID)),
            },
        }
        # Real lane state machine with injected offline fixture adapter; not a real CLI success.
        from tests.test_agent_lane import fake_spawn_factory, review, good_item, ok_result
        lane_dir = self.cand_dir / "runs" / RUN_ID / "lanes" / "cn"
        lane_dir.mkdir(parents=True)
        task = prepare_brief.build_lane_task(dict(self.context, dedup={"count": 0}), "cn", [], str(lane_dir))
        self.context["lanes"]["cn"] = task
        result = {"run_id": RUN_ID, "lane": "cn", "items": [lane_item()]}
        verdict = {"run_id": RUN_ID, "lane": "cn", "round": 1, "verdict": "pass",
                   "items": [{"url": GOOD_URL, "pass": True, "publish_date": "2026-10-13",
                              "date_evidence": GOOD_URL,
                              "content_sha256": __import__("hashlib").sha256(__import__("re").sub(r"\s+", "", long_body()).encode()).hexdigest()}]}
        spawn = fake_spawn_factory([result], [verdict])
        with mock.patch.object(agent_lane, "fetch_texts_parallel", return_value={GOOD_URL: {"text": long_body(), "metadata": {}}}):
            self.cn_outcome = agent_lane.run_lane(task, str(lane_dir), spawn=spawn)
        self.assertEqual(self.cn_outcome["status"], "ok")
        self.ctx_file = Path(self.tmp.name) / "task_context.json"
        self.ctx_file.write_text(json.dumps(self.context, ensure_ascii=False))
        self.patches = [
            # Unit fixtures explicitly mock browser proof; replay performs real browser validation.
            mock.patch.object(run_pipeline.verify_html_browser, "verify", return_value={"ok": True, "mode": "MOCK_BROWSER_UNIT_TEST"}),
            mock.patch.object(html_report, "verify_browser_receipt", return_value=True),
            mock.patch.object(collect_brief, "CAND_DIR", str(self.cand_dir)),
            mock.patch.object(collect_brief, "OUTPUT_DIR", str(self.out_dir)),
            mock.patch.object(collect_brief, "LOCK_FILE",
                              str(Path(self.tmp.name) / "lock")),
            mock.patch.object(validate_brief, "CAND_DIR", str(self.cand_dir)),
            mock.patch.object(validate_brief, "OUTPUT_DIR", str(self.out_dir)),
            mock.patch.object(brief_record, "RECORDS_FILE", str(self.records)),
            mock.patch.object(filter_mod, "load_dedup", return_value=set()),
            mock.patch.object(run_pipeline, "_collect_deterministic",
                              return_value=([], {}, {"hn": 0}, [], [])),
            mock.patch.object(run_pipeline, "_run_lanes", return_value={
                "cn": self.cn_outcome,
                "intl": lane_outcome("failed", reason="作者会话异常退出 rc=1"),
            }),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def fake_agy(self, fail_writer=False, fail_html=False):
        def run(cand, prompt, timeout=1200):
            content = Path(prompt).read_text(encoding="utf-8")
            if "HTML 报告制作" in content:
                if fail_html:
                    return 1
                html = HTML_TMPL.format(
                    run_id=RUN_ID, url=GOOD_URL, title="好文章", summary=SUMMARY,
                    viewport=VIEWPORT,
                    like_block="%s\n%s" % (html_report.LIKE_START, html_report.LIKE_END))
                Path(self.context["batch"]["html"]).write_text(html, encoding="utf-8")
            else:
                if fail_writer:
                    return 1
                Path(self.context["batch"]["brief"]).write_text(
                    MD_TMPL.format(run_id=RUN_ID, url=GOOD_URL, title="好文章",
                                   summary=SUMMARY), encoding="utf-8")
            return 0
        return run

    def test_pipeline_end_to_end_and_finalize_html(self):
        with mock.patch.object(run_pipeline.run_agy, "run", side_effect=self.fake_agy()):
            rc = run_pipeline.run(str(self.ctx_file))
        self.assertEqual(rc, 0)
        cand = self.context["batch"]["candidates"]
        brief = self.context["batch"]["brief"]
        html_path = self.context["batch"]["html"]
        # canonical MD 点赞区已由脚本重建（本机 127.0.0.1:8900 链接）
        brief_text = Path(brief).read_text(encoding="utf-8")
        self.assertIn("## 👍 点赞", brief_text)
        self.assertIn("http://127.0.0.1:8900/like", brief_text)
        # HTML 点赞区由脚本重建且通过一致性校验
        html_text = Path(html_path).read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:8900/like", html_text)
        self.assertIn("手机端", html_text)
        # 校验凭据留存于批次目录
        self.assertTrue(list((Path(cand).parent / "validation").glob("*/receipt.json")))
        # 日期别名缓存
        self.assertFalse((self.cand_dir / ("Daily-Brief-%s-candidates.json" % DATE)).is_file())

        # finalize：MD+HTML 双发布
        final = Path(finalize_brief.finalize(cand, brief))
        self.assertEqual(final, self.out_dir / ("Daily-Brief-%s.md" % DATE))
        html_alias = self.out_dir / ("Daily-Brief-%s.html" % DATE)
        self.assertTrue(html_alias.is_file())
        rec = brief_record.load_records()[0]
        self.assertEqual(rec["run_id"], RUN_ID)
        import hashlib
        self.assertEqual(rec["html_sha256"],
                         hashlib.sha256(html_alias.read_bytes()).hexdigest())
        self.assertFalse(Path(cand).parent.exists())  # 过程目录已清理

        # 幂等：同批重复 finalize 返回同一路径，不重复记录
        self.assertEqual(finalize_brief.finalize(cand, brief), str(final))
        self.assertEqual(len(brief_record.load_records()), 1)

        # HTML 发布入口被篡改 → 拒绝
        html_alias.write_text(html_alias.read_text(encoding="utf-8") + " ",
                              encoding="utf-8")
        with self.assertRaises(ValueError):
            finalize_brief.finalize(cand, brief)

    def test_writer_failure_rolls_back_no_publish(self):
        with mock.patch.object(run_pipeline.run_agy, "run",
                               side_effect=self.fake_agy(fail_writer=True)):
            rc = run_pipeline.run(str(self.ctx_file))
        self.assertEqual(rc, 1)
        self.assertFalse(Path(self.context["batch"]["brief"]).exists())
        self.assertFalse((self.out_dir / ("Daily-Brief-%s.md" % DATE)).exists())

    def test_html_failure_rolls_back_no_publish(self):
        with mock.patch.object(run_pipeline.run_agy, "run",
                               side_effect=self.fake_agy()), \
             mock.patch.object(run_pipeline.controlled_html, "write", side_effect=ValueError('template rendering failed')):
            rc = run_pipeline.run(str(self.ctx_file))
        self.assertEqual(rc, 1)
        self.assertFalse((self.out_dir / ("Daily-Brief-%s.html" % DATE)).exists())

    def test_rerun_same_context_refused_no_overwrite(self):
        with mock.patch.object(run_pipeline.run_agy, "run", side_effect=self.fake_agy()):
            self.assertEqual(run_pipeline.run(str(self.ctx_file)), 0)
            cand_file = Path(self.context["batch"]["candidates"])
            before = cand_file.read_bytes()
            self.assertEqual(run_pipeline.run(str(self.ctx_file)), 1)  # write_once 冲突
            self.assertEqual(cand_file.read_bytes(), before)

    def test_collect_lock_blocks_concurrent_pipeline(self):
        held = collect_brief.acquire_lock()
        self.assertIsNotNone(held)
        try:
            self.assertEqual(run_pipeline.run(str(self.ctx_file)), 1)
        finally:
            collect_brief.release_lock(held)

    def test_actual_lane_without_region_inherits_collection_lane(self):
        self.assertEqual(self.cn_outcome["status"], "ok")
        self.assertEqual(self.cn_outcome["items"][0]["region"], "cn")
        self.assertEqual(self.cn_outcome["items"][0]["region_provenance"], {"kind": "lane", "lane": "cn"})

    def test_lane_run_id_mismatch_refused(self):
        bad = dict(self.context)
        bad["lanes"] = {"cn": {"run_id": "别的批次", "lane": "cn"},
                        "intl": {"run_id": RUN_ID, "lane": "intl"}}
        f = Path(self.tmp.name) / "bad_context.json"
        f.write_text(json.dumps(bad, ensure_ascii=False))
        self.assertEqual(run_pipeline.run(str(f)), 1)


class FinalizeHtmlCase(unittest.TestCase):
    """finalize 的 HTML 规则：声明 delivery=html 缺 HTML 拒绝；旧批次无 HTML 仍兼容。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cand_dir = Path(self.tmp.name) / "_candidates"
        self.out_dir = Path(self.tmp.name) / "output"
        self.records = Path(self.tmp.name) / "data/records.json"
        self.patches = [
            mock.patch.object(validate_brief, "CAND_DIR", str(self.cand_dir)),
            mock.patch.object(validate_brief, "OUTPUT_DIR", str(self.out_dir)),
            mock.patch.object(collect_brief, "OUTPUT_DIR", str(self.out_dir)),
            mock.patch.object(brief_record, "RECORDS_FILE", str(self.records)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def create(self, payload=None):
        payload = payload or {
            "run_id": RUN_ID, "generated_at": "2026-10-14T01:00:00+08:00",
            "window": {"start": "2026-10-13", "end": "2026-10-14"},
            "preference": {"pref_dir": None, "explore_dir": "科技", "likes_count": 0},
            "candidates": [{
                "title": "好文章", "url": GOOD_URL, "domain": "example.com",
                "direction": "科技", "is_preferred": False, "is_explore": False,
                "publish_date": "2026-10-13", "date_verified": True,
                "date_source": "url", "date_evidence": GOOD_URL,
                "date_precision": "day", "window_outside_days": 0,
                "hn_points": None, "word_count": 800,
                "watermark_suspect": False, "watermark_reasons": [],
                "content": long_body(), "source_key": "cn:0", "source_label": "查询",
            }],
            "source_leftovers": [], "horizon": [],
        }
        cand, brief = run_store.create_run(self.cand_dir, self.out_dir, DATE, payload)
        Path(brief).write_text(MD_TMPL.format(run_id=RUN_ID, url=GOOD_URL,
                                              title="好文章", summary=SUMMARY),
                               encoding="utf-8")
        return Path(cand), Path(brief)

    def test_delivery_html_requires_html(self):
        payload = None
        cand, brief = self.create()
        p = json.loads(cand.read_text())
        p["delivery"] = "html"
        payload = p
        # 直接改写工作副本（测试内构造声明 HTML 交付的批次）
        cand.write_text(json.dumps(payload, ensure_ascii=False))
        with self.assertRaises(ValueError):
            finalize_brief.finalize(cand, brief)
        self.assertFalse((self.out_dir / brief.name).exists())

    def test_legacy_md_only_still_works(self):
        cand, brief = self.create()  # 无 delivery 标记、无 HTML
        final = Path(finalize_brief.finalize(cand, brief))
        self.assertTrue(final.is_file())
        self.assertFalse((self.out_dir / ("Daily-Brief-%s.html" % DATE)).exists())
        rec = brief_record.load_records()[0]
        self.assertNotIn("html_sha256", rec)


if __name__ == "__main__":
    unittest.main()
