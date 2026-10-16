#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DailyBrief agent 管线 runner（主席显式启动的长任务）。

prepare_brief.py 已生成任务上下文；本脚本负责：
  1. 采集锁（与 collect_brief 同一把锁，防并发重复）
  2. 确定性来源采集（HN Algolia / Telegram / Agent Case Share / 视野拓展 RSS，
     复用成熟函数，不让模型重复手工做确定性采集）
  3. 并行双路检索车道：国内=小k（真实 kimi CLI）、国外=小g（真实 agy CLI），
     各路独立进程/独立工作目录，作者+独立复核会话，修正后再复核
  4. 合并候选 → 统一准入（rank.filter_candidates/rank_candidates 原闸门不削弱：
     真实正文 ≥500 字、日精度窗口内日期证据、非法 URL/聚合页拒收、历史去重、
     方向多样性/轨交优先/生活健康保底/探索保底）
  5. 空池熔断（不回退历史池）；单路失败降级并记录 lane_status，如实写进产物
  6. create_run 落盘批次（含 agent_review 复核证据）+ 同日日期别名缓存
  7. 小g写作 canonical MD（run_agy 批次锁）→ 原 validate 校验 → 重建点赞区 → 再校验
  8. 小g基于同批 JSON+MD 写 HTML → 脚本重建 HTML 点赞区 → HTML↔canonical 一致性校验

本脚本不 finalize、不发布：最终交付仍由主席验收后运行 finalize_brief.py。

用法:
  python3 scripts/run_pipeline.py <task_context.json> [--lane-timeout 1500]
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules import agent_lane, filter as filter_mod, horizon, html_report, rank, retrieve, run_store  # noqa: E402
from modules.window import TZ, log  # noqa: E402
import collect_brief  # noqa: E402
import finalize_brief  # noqa: E402
import like_links  # noqa: E402
import add_like  # noqa: E402
import run_agy  # noqa: E402

DETERMINISTIC_BUDGET_S = 480   # 确定性来源采集总预算（与 collect_brief 一致）
LANE_TIMEOUT_S = 1500          # 单车道单个 CLI 会话超时
WRITER_TIMEOUT_S = 1200        # 小g写作超时
HTML_TIMEOUT_S = 1200          # 小g HTML 超时


def _collect_deterministic(context, budget):
    """确定性来源采集（复用 collect_brief 同源函数）。返回 (candidates, src_map, stats, hz_items, hz_unavailable)。"""
    now = datetime.now(TZ)
    ws = datetime.strptime(context["window"]["start"], "%Y-%m-%d").date()
    we = datetime.strptime(context["window"]["end"], "%Y-%m-%d").date()
    dedup = filter_mod.load_dedup()
    candidates, src_map, stats = [], {}, {}

    hn_items = retrieve.fetch_hn(now, budget=budget)
    for c in hn_items:
        c["direction"] = rank.likes_mod.infer_direction(c.get("url", ""), c.get("title", ""))
        c["is_preferred"] = False
        c["is_explore"] = False
        c["source_key"] = "hn"
        c["source_label"] = "Hacker News"
        src_map.setdefault("hn", {"label": "Hacker News", "direction": "科技", "items": []})["items"].append(c)
    candidates.extend(hn_items)
    stats["hn"] = len(hn_items)

    tg_items = retrieve.fetch_telegram_channels(collect_brief.TELEGRAM_CHANNELS, budget=budget)
    for c in tg_items:
        c["direction"] = rank.likes_mod.infer_direction(c.get("url", ""), c.get("title", ""))
        ch = c.get("telegram_channel", "?")
        c["source_key"] = "tg:" + ch
        c["source_label"] = "TG@" + ch
        src_map.setdefault(c["source_key"], {"label": c["source_label"], "direction": c["direction"], "items": []})["items"].append(c)
    candidates.extend(tg_items)
    stats["telegram"] = len(tg_items)

    acs_items = retrieve.fetch_agent_case_share(budget=budget)
    for c in acs_items:
        c["source_label"] = "Agent Case Share AI日报"
        src_map.setdefault("acs", {"label": "Agent Case Share AI日报", "direction": "AI", "items": []})["items"].append(c)
    candidates.extend(acs_items)
    stats["agent_case_share"] = len(acs_items)

    hz_promoted, hz_items, hz_unavailable = horizon.fetch_horizon(dedup, ws, we, budget=budget)
    for c in hz_promoted:
        c["is_preferred"] = False
        c["is_explore"] = False
        src_map.setdefault(c["source_key"], {"label": c["source_label"],
                                             "direction": c["direction"], "items": []})["items"].append(c)
    candidates.extend(hz_promoted)
    stats["horizon_fulltext"] = len(hz_promoted)
    stats["horizon"] = len(hz_items)
    return candidates, src_map, stats, hz_items, hz_unavailable


def _run_lanes(context, lane_timeout):
    """并行执行 cn/intl 两条车道。返回 {lane: outcome}（outcome 永不抛出 CLI 故障）。"""
    outcomes = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {}
        for lane in ("cn", "intl"):
            task = context["lanes"].get(lane)
            if not task:
                outcomes[lane] = {"lane": lane, "status": "failed",
                                  "reason": "任务上下文缺车道定义", "items": [],
                                  "rounds": 0, "sessions": {"author": [], "reviewer": []},
                                  "logs": [], "review": {"verdict": None, "rounds": []}}
                continue
            futs[ex.submit(agent_lane.run_lane, task, task["lane_dir"],
                           timeout=lane_timeout)] = lane
        for fut, lane in futs.items():
            try:
                outcomes[lane] = fut.result()
            except Exception as e:  # 车道内部异常不得拖垮另一路
                outcomes[lane] = {"lane": lane, "status": "failed",
                                  "reason": "车道内部异常: %s" % e, "items": [],
                                  "rounds": 0, "sessions": {"author": [], "reviewer": []},
                                  "logs": [], "review": {"verdict": None, "rounds": []}}
    return outcomes


def _merge_lane_items(outcomes, candidates, src_map):
    """只合并复核通过车道的条目；复核未通过/失败车道的产物一律不进池。"""
    for lane, oc in outcomes.items():
        if oc.get("status") != "ok":
            continue
        label = agent_lane.LANES[lane]["label"]
        for it in oc.get("items") or []:
            c = dict(it)
            c["source_key"] = c.get("source_key") or "agent:%s" % lane
            c["source_label"] = c.get("source_label") or label
            c["is_preferred"] = bool(c.get("is_preferred"))
            c["is_explore"] = bool(c.get("is_explore"))
            candidates.append(c)
            src_map.setdefault("agent:%s" % lane,
                               {"label": label, "direction": c.get("direction"),
                                "items": []})["items"].append(c)


def build_candidates_payload(context, outcomes, det_candidates, src_map, stats,
                             hz_items, hz_unavailable, budget=None):
    """合并 + 统一准入 + 席位分配 + 速览。空池返回 None（熔断由调用方决定）。"""
    ws = datetime.strptime(context["window"]["start"], "%Y-%m-%d").date()
    we = datetime.strptime(context["window"]["end"], "%Y-%m-%d").date()
    dedup = filter_mod.load_dedup()
    pref_dir = (context.get("preference") or {}).get("pref_dir")
    explore_dir = (context.get("preference") or {}).get("explore_dir")

    candidates = list(det_candidates)
    _merge_lane_items(outcomes, candidates, src_map)

    kept = rank.rank_candidates(
        rank.filter_candidates(candidates, dedup, ws, we, pref_dir, budget=budget),
        explore_dir)
    if not kept:
        return None
    leftovers = collect_brief.build_source_leftovers(src_map, kept, dedup, ws, we,
                                                     budget=budget)
    return {
        "run_id": context["run_id"],
        "generated_at": datetime.now(TZ).isoformat(),
        "window": context["window"],
        "preference": context["preference"],
        "delivery": "html",
        "pipeline": {
            "mode": "agent",
            "lane_status": {lane: {"status": oc.get("status"),
                                   "reason": oc.get("reason")}
                            for lane, oc in outcomes.items()},
            "deterministic_stats": stats,
        },
        "agent_review": {
            lane: {"agent": oc.get("agent"), "status": oc.get("status"),
                   "reason": oc.get("reason"), "rounds": oc.get("rounds"),
                   "sessions": oc.get("sessions"), "logs": oc.get("logs"),
                   "review": oc.get("review"),
                   "admitted": len(oc.get("items") or [])}
            for lane, oc in outcomes.items()
        },
        "candidates": kept,
        "source_leftovers": leftovers,
        "horizon": hz_items,
        "horizon_unavailable": hz_unavailable,
    }


def _writer_prompt(context, cand_path):
    """小g canonical MD 写作 prompt（含精确 run_id/候选/简报路径，run_agy 强制要求）。"""
    brief_path = context["batch"]["brief"]
    win = context["window"]
    pref = context["preference"]
    return """你是小g，负责 DailyBrief 批次 %s 的简报写作（canonical Markdown）。

精确配对（不得改用其他文件）：
- run_id: %s
- 候选 JSON: %s
- 简报输出（写到这里）: %s

时间窗口：%s ~ %s。偏好方向: %s，探索方向: %s。

流程：
1. 读候选 JSON。只用 candidates / source_leftovers / horizon 里的条目；
   rejected_candidates 里的 URL 禁止出现在任何位置。
2. 需要拒收候选时只能运行：
   python3 "%s" --candidates "%s" --url '原URL' --reason '真实原因' --reviewed-by '小g'
   禁止只删正文、取消 is_explore 或伪造日期。
3. 按模板写简报到上面的简报路径：
   - 文首第一行标题后写 <!-- run_id: %s -->
   - 结构：# Daily Brief-%s / **今日速览 (TLDR)** / ## 今日热门文章（按 ### 方向分组，
     #### 标题 + 200-400字中文深度总结 + [[序号]](url)）/ ## 今日主题趋势 /
     ## 值得深读的观点 / ## 快速浏览 / ## 更多资源链接 / ## 未推荐来源速览 /
     ## 视野拓展 / ## 参考资料（[序号] [标题](url)）
   - 偏好命中条目（is_preferred=true）标题行尾加 （偏好命中），探索条目（is_explore=true）加 （今日探索），
     深度总结区偏好命中 ≤8 条；有合格探索候选必须收录 ≥1 篇
   - horizon 条目只写「## 视野拓展」区（标题+链接+来源+地区+日期），不进其他区；
     horizon_unavailable 如实写缺货原因
   - source_leftovers 写「## 未推荐来源速览」，每源 1 条，标注来源简称与日期
   - 全中文；只写候选里有的 URL；摘要以候选 content 为依据，禁止凭记忆补数字/人物/结论；
     无合格内容的方向注明缺货，不凑篇数
   - 车道状态见候选 JSON 的 pipeline.lane_status；有车道失败时在文末如实注明该方向缺货
4. 不要写 ## 👍 点赞 区（脚本统一重建），不要运行 validate/finalize，不要改其他文件。
5. 写完读回简报文件确认结构完整。
""" % (context["run_id"], context["run_id"], cand_path, brief_path,
       win["start"], win["end"], pref.get("pref_dir") or "无", pref.get("explore_dir") or "无",
       os.path.join(ROOT, "scripts", "reject_candidate.py"), cand_path,
       context["run_id"], context["date"])


def _html_prompt(context, cand_path):
    """小g HTML 报告 prompt：基于同批 JSON 与已校验 canonical MD 制作。"""
    brief_path = context["batch"]["brief"]
    html_path = context["batch"]["html"]
    return """你是小g，负责 DailyBrief 批次 %s 的 HTML 报告制作。

输入（只读）：
- run_id: %s
- 候选 JSON: %s
- canonical 简报 MD（已通过强校验）: %s
输出（写到这里）: %s

要求：
1. 内容严格来自上述 MD 与候选 JSON：标题/URL/摘要逐条对应，禁止新增来源事实，
   禁止引用 rejected_candidates 里的 URL。
2. 单文件离线 HTML：不引用任何外部资源（无 CDN/外链 CSS/JS/字体/图片）；
   <meta charset="utf-8">、<meta name="viewport" content="width=device-width,initial-scale=1">。
3. 中文、大字号（正文 ≥18px，行高 ≥1.7）；含目录导航；主题方向与地区筛选按钮
   （原生 JS 实现，不依赖框架）；日期证据用 <details> 折叠展示
   （date_source/date_evidence 来自候选 JSON）。
4. 移动端 390px 宽度无横向溢出：长 URL/代码必须可断行
   （CSS 加 overflow-wrap:anywhere / word-break:break-word）。
5. 页面注明 run_id（注释 <!-- run_id: %s --> 即可）；
   页首保留手机端提示：手机无法点击点赞链接，需发文字指令「点赞 N」。
6. 点赞区位置固定放标记位，中间留空（脚本统一重建，不要自己写点赞链接）：
   %s
   %s
7. 不要运行 validate/finalize，不要改 MD、候选 JSON 或其他文件；写完读回确认。
""" % (context["run_id"], context["run_id"], cand_path, brief_path, html_path,
       context["run_id"], html_report.LIKE_START, html_report.LIKE_END)


def _validate_and_likes(cand_path, brief_path):
    """canonical MD 闸门：validate → 重建点赞区 → 再 validate。任一步失败返回 False。"""
    if finalize_brief.validate(brief_path, cand_path):
        print("[VALIDATE_FAILED] 简报预检未过（明细见上方输出）")
        return False
    ok, msg = like_links.add_like_section(str(brief_path))
    print(msg)
    if not ok and "已是最新" not in msg:
        print("[LIKE_FAILED] 点赞区重建失败")
        return False
    if finalize_brief.validate(brief_path, cand_path):
        print("[VALIDATE_FAILED] 点赞区重建后复检未过")
        return False
    return True


def run(context_path, lane_timeout=LANE_TIMEOUT_S, writer_timeout=WRITER_TIMEOUT_S,
        html_timeout=HTML_TIMEOUT_S):
    with open(context_path, encoding="utf-8") as f:
        context = json.load(f)
    run_id = run_store.safe_run_id(context.get("run_id"))
    for key in ("cn", "intl"):
        lane_task = context.get("lanes", {}).get(key)
        if lane_task and lane_task.get("run_id") != run_id:
            print("[PIPELINE_FAILED] 车道任务 run_id 与上下文不一致（%s）" % key)
            return 1
    date_s = context["date"]

    lock = collect_brief.acquire_lock()
    if lock is None:
        print("[PIPELINE_FAILED] 已有采集/管线进程在运行（锁占用 %s）" % collect_brief.LOCK_FILE)
        return 1
    try:
        det_budget = retrieve.Budget(DETERMINISTIC_BUDGET_S)
        log("确定性来源采集开始（预算 %ds）" % DETERMINISTIC_BUDGET_S)
        det_candidates, src_map, stats, hz_items, hz_unavailable = \
            _collect_deterministic(context, det_budget)

        log("双路检索车道并行启动（单会话超时 %ds）" % lane_timeout)
        outcomes = _run_lanes(context, lane_timeout)
        for lane in ("cn", "intl"):
            oc = outcomes[lane]
            log("车道 %s: %s（%s）" % (lane, oc.get("status"), oc.get("reason") or "ok"))

        payload = build_candidates_payload(context, outcomes, det_candidates, src_map,
                                           stats, hz_items, hz_unavailable,
                                           budget=det_budget)
        lane_note = "; ".join("%s=%s" % (l, o.get("status")) for l, o in outcomes.items())
        if payload is None:
            print("[COLLECT_FAILED] 合并后候选 0 篇（车道: %s）。熔断：不写候选文件，"
                  "本次简报未生成。" % lane_note)
            return 1

        cand_path, brief_path = run_store.create_run(
            collect_brief.CAND_DIR, collect_brief.OUTPUT_DIR, date_s, payload)
        collect_brief.atomic_write_json(collect_brief._cand_path(date_s), payload)
        print("候选 %d 篇（run_id %s；车道: %s）" % (len(payload["candidates"]), run_id, lane_note))
        print("候选详情 JSON：%s" % cand_path)
        print("本次简报输出：%s" % brief_path)

        # ── 小g写作 canonical MD（批次锁）──
        writer_prompt = os.path.join(os.path.dirname(cand_path), "writer_prompt.txt")
        with open(writer_prompt, "w", encoding="utf-8") as f:
            f.write(_writer_prompt(context, cand_path))
        rc = run_agy.run(cand_path, writer_prompt, timeout=writer_timeout)
        if rc != 0:
            print("[WRITER_FAILED] 小g写作异常退出 rc=%s" % rc)
            return 1
        if not os.path.isfile(brief_path):
            print("[WRITER_FAILED] 小g未产出简报文件 %s" % brief_path)
            return 1
        if not _validate_and_likes(cand_path, brief_path):
            return 1

        # ── 小g HTML 制作（批次锁）→ 脚本重建点赞区 → 一致性校验 ──
        html_prompt = os.path.join(os.path.dirname(cand_path), "html_prompt.txt")
        with open(html_prompt, "w", encoding="utf-8") as f:
            f.write(_html_prompt(context, cand_path))
        rc = run_agy.run(cand_path, html_prompt, timeout=html_timeout)
        if rc != 0:
            print("[HTML_FAILED] 小g HTML 制作异常退出 rc=%s" % rc)
            return 1
        html_path = context["batch"]["html"]
        if not os.path.isfile(html_path):
            print("[HTML_FAILED] 小g未产出 HTML 文件 %s" % html_path)
            return 1
        refs = add_like.parse_brief_refs(brief_path)
        ok, msg = html_report.rebuild_like_section(html_path, refs)
        print(msg)
        if not ok and "已是最新" not in msg:
            print("[HTML_FAILED] HTML 点赞区重建失败（缺标记位？）")
            return 1
        ok_html, errors = html_report.verify_html(html_path, brief_path, cand_path)
        if not ok_html:
            print("[HTML_INVALID] HTML 与 canonical 不一致，未通过验收：")
            for e in errors:
                print("  ✗ %s" % e)
            return 1
        print("HTML 一致性校验 PASS：%s" % html_path)
        print("[PIPELINE_OK] 候选+canonical MD+HTML 已就绪，待主席验收后 finalize：")
        print('  python3 "%s" "%s" "%s"'
              % (os.path.join(ROOT, "scripts", "finalize_brief.py"), brief_path, cand_path))
        return 0
    finally:
        collect_brief.release_lock(lock)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("context", help="prepare_brief.py 生成的 task_context.json")
    ap.add_argument("--lane-timeout", type=int, default=LANE_TIMEOUT_S)
    ap.add_argument("--writer-timeout", type=int, default=WRITER_TIMEOUT_S)
    ap.add_argument("--html-timeout", type=int, default=HTML_TIMEOUT_S)
    args = ap.parse_args(argv)
    try:
        return run(args.context, args.lane_timeout, args.writer_timeout,
                   args.html_timeout)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("[PIPELINE_FAILED] 管线异常：%s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
