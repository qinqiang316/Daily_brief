#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DailyBrief agent 管线：短时 prepare 入口（cron 预运行，只生成任务上下文）。

职责（全部本地计算，秒级完成，不联网检索、不跑外部 CLI）：
  - 计算时间窗口（断档补采 + 72h 上限，与 collect_brief 同一规则）
  - 读取/维护历史去重集合（auto_update_dedup）
  - 读取点赞画像，生成偏好/探索增强查询（样本门槛与现有实现一致）
  - 生成新批次 run_id 与独立批次目录，落盘 task_context + 两路车道任务文件：
      国内车道、国外车道都走小g（agy CLI），会话互相独立

漫长的 CLI 检索不在此执行；由主席显式启动 scripts/run_pipeline.py 完成
并行检索、合并候选与小g写作草稿。

用法:
  python3 scripts/prepare_brief.py            # 生成批次任务上下文并打印路径
"""
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.window import TZ, compute_window  # noqa: E402
from modules import filter as filter_mod, rank, run_store  # noqa: E402
import collect_brief  # noqa: E402  复用 CAND_DIR/OUTPUT_DIR/查询模板（顶层无副作用）

PREPARE_DIR_NAME = "prepare"


def build_lane_task(payload, lane, queries, lane_dir):
    return {
        "run_id": payload["run_id"],
        "date": payload["date"],
        "window": payload["window"],
        "lane": lane,
        "queries": queries,
        "preference": payload["preference"],
        "dedup_count": payload["dedup"]["count"],
        "lane_dir": lane_dir,
        "result_file": os.path.join(lane_dir, "author_result.json"),
        "review_file_tmpl": os.path.join(lane_dir, "review_r{round}.json"),
    }


def prepare(now=None, cand_dir=None, output_dir=None):
    """生成批次任务上下文。返回 (context, 批次目录)。不写候选/简报产物。"""
    now = now or datetime.now(TZ)
    cand_dir = cand_dir or collect_brief.CAND_DIR
    output_dir = output_dir or collect_brief.OUTPUT_DIR
    today_s = now.strftime("%Y-%m-%d")
    run_id = now.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]

    window_start, today = compute_window(now)
    dedup = filter_mod.auto_update_dedup()
    extra, pref_dir, explore_dir = rank.build_extra_queries(now)
    queries_cn, queries_intl = collect_brief.build_default_queries(now)
    likes_count = len(rank.likes_mod.load_likes(collect_brief.LIKES_FILE))

    with open(filter_mod.DEDUP_FILE, "rb") as f:
        dedup_sha = hashlib.sha256(f.read()).hexdigest()

    run_dir = run_store.candidate_path(cand_dir, today_s, run_id).parent
    run_store.mark_created(run_dir)
    prep_dir = run_dir / PREPARE_DIR_NAME
    lanes = {}
    for lane in ("cn", "intl"):
        lanes[lane] = str(run_dir / "lanes" / lane)
    context = {
        "run_id": run_id,
        "date": today_s,
        "generated_at": now.isoformat(),
        "window": {"start": str(window_start), "end": str(today)},
        "preference": {"pref_dir": pref_dir, "explore_dir": explore_dir,
                       "likes_count": likes_count},
        "dedup": {"count": len(dedup), "sha256": dedup_sha,
                  "file": filter_mod.DEDUP_FILE},
        "batch": {
            "run_dir": str(run_dir),
            "candidates": str(run_store.candidate_path(cand_dir, today_s, run_id)),
            "brief": str(run_store.brief_path(output_dir, today_s, run_id)),
            "html": str(run_store.html_path(output_dir, today_s, run_id)),
        },
        "lanes": {
            "cn": build_lane_task({"run_id": run_id, "date": today_s,
                                   "window": {"start": str(window_start),
                                              "end": str(today)},
                                   "preference": {"pref_dir": pref_dir,
                                                  "explore_dir": explore_dir,
                                                  "likes_count": likes_count},
                                   "dedup": {"count": len(dedup)}},
                                  "cn", queries_cn + extra, lanes["cn"]),
            "intl": build_lane_task({"run_id": run_id, "date": today_s,
                                     "window": {"start": str(window_start),
                                                "end": str(today)},
                                     "preference": {"pref_dir": pref_dir,
                                                    "explore_dir": explore_dir,
                                                    "likes_count": likes_count},
                                     "dedup": {"count": len(dedup)}},
                                    "intl", queries_intl + extra, lanes["intl"]),
        },
        # 确定性采集源仍由脚本函数完成（成熟 HN/RSS/Telegram/ACS 代码复用，
        # 不让模型重复手工做确定性采集），runner 阶段执行
        "deterministic_sources": ["hn", "telegram", "agent_case_share", "horizon"],
    }
    for lane in ("cn", "intl"):
        os.makedirs(lanes[lane], exist_ok=True)
        collect_brief.atomic_write_json(
            os.path.join(lanes[lane], "task.json"), context["lanes"][lane])
    run_store.write_once(prep_dir / "task_context.json",
                         run_store.json_bytes(context))
    os.makedirs(run_store.brief_path(output_dir, today_s, run_id).parent,
                exist_ok=True)
    return context, str(run_dir)


def main(argv=None):
    context, run_dir = prepare()
    win = context["window"]
    pref = context["preference"]
    print("时间窗口：%s ~ %s（run_id %s）" % (win["start"], win["end"], context["run_id"]))
    print("偏好方向: %s | 探索方向: %s | 点赞 %d 条 | 去重集合 %d 条"
          % (pref["pref_dir"] or "无", pref["explore_dir"], pref["likes_count"],
             context["dedup"]["count"]))
    print("任务上下文：%s" % os.path.join(run_dir, PREPARE_DIR_NAME, "task_context.json"))
    for lane in ("cn", "intl"):
        print("车道任务（%s）：%s" % (lane, context["lanes"][lane]["lane_dir"]))
    print("本次简报输出：%s" % context["batch"]["brief"])
    print("本次HTML输出：%s" % context["batch"]["html"])
    print("下一步（主席显式启动，长任务后台运行）：")
    print('  python3 "%s" "%s"'
          % (os.path.join(ROOT, "scripts", "run_pipeline.py"),
             os.path.join(run_dir, PREPARE_DIR_NAME, "task_context.json")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
