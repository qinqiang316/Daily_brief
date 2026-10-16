#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DailyBrief agent 管线 runner（主席显式启动的长任务）。

prepare_brief.py 已生成任务上下文；本脚本负责：
  1. 采集锁（与 collect_brief 同一把锁，防并发重复）
  2. 确定性来源采集（HN Algolia / Telegram / Agent Case Share / 视野拓展 RSS，
     复用成熟函数，不让模型重复手工做确定性采集）
  3. 并行双路检索车道：脚本批量检索打底，小g单次选文并按需补漏；
     脚本核验原文证据，候选与报告进入 ready 后由 Hermes 语义复核
  4. 合并候选 → 统一准入（rank.filter_candidates/rank_candidates 原闸门不削弱：
     真实正文 ≥500 字、日精度窗口内日期证据、非法 URL/聚合页拒收、历史去重、
     方向多样性/轨交优先/生活健康保底/探索保底）
  5. 空池熔断（不回退历史池）；单路失败降级并记录 lane_status，如实写进产物
  6. create_run 落盘批次（含 agent_review 复核证据）+ 同日日期别名缓存
  7. 小g写作 canonical MD（run_agy 批次锁）→ 原 validate 校验 → 重建点赞区 → 再校验
  8. 小g基于同批 JSON+MD 写 HTML → 脚本重建 HTML 点赞区 → HTML↔canonical 一致性校验

本脚本不 finalize、不发布：最终交付仍由主席验收后运行 finalize_brief.py。

用法:
  python3 scripts/run_pipeline.py <task_context.json> [--lane-timeout 1200]
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules import controlled_html, pipeline_budget, agent_lane, filter as filter_mod, horizon, html_report, rank, retrieve, run_store  # noqa: E402
from modules import region as region_mod
from modules.window import TZ, log  # noqa: E402
import collect_brief  # noqa: E402
import finalize_brief  # noqa: E402
import like_links  # noqa: E402
import add_like  # noqa: E402
import verify_html_browser
import run_agy  # noqa: E402

DETERMINISTIC_BUDGET_S = pipeline_budget.DETERMINISTIC
LANE_TIMEOUT_S = pipeline_budget.LANES  # TOTAL per lane, including all rounds/takeovers
WRITER_TIMEOUT_S = pipeline_budget.WRITER
HTML_TIMEOUT_S = pipeline_budget.HTML
PIPELINE_TIMEOUT_S = pipeline_budget.TOTAL
BODY_BUDGET_S = pipeline_budget.BODY


def preflight(context):
    """Probe once per run, not once per author/reviewer/writer. A hung
    primary must not consume every role's entire slot before takeover.
    """
    runtime = Path(ROOT) / 'docs/pipeline-runtime.json'
    if runtime.exists():
        config = json.loads(runtime.read_text())
        for key in ('model', 'provider'):
            if config.get('fallback_' + key):
                os.environ['DAILYBRIEF_HERMES_' + key.upper()] = config['fallback_' + key]
    path = Path(context['batch']['run_dir']) / 'preflight-agy.log'
    prompt = '健康检查。只回复 DAILYBRIEF_OK，不调用工具、不委派任务。'
    command = agent_lane.cli_command('agy', prompt, 45)
    rc = agent_lane.run_cli_session(command, ROOT, str(path), 50)
    text = path.read_text(errors='replace')
    if rc or 'DAILYBRIEF_OK' not in text or 'print timeout' in text:
        os.environ['DAILYBRIEF_AGY_UNAVAILABLE'] = str(path)
        log('小g健康探针失败，本批按既有规则由Hermes新会话接管；证据 ' + str(path))
    else:
        os.environ.pop('DAILYBRIEF_AGY_UNAVAILABLE', None)


def _collect_deterministic(context, budget):
    """确定性来源采集（复用 collect_brief 同源函数）。返回 (candidates, src_map, stats, hz_items, hz_unavailable)。"""
    now = datetime.now(TZ)
    ws = datetime.strptime(context["window"]["start"], "%Y-%m-%d").date()
    we = datetime.strptime(context["window"]["end"], "%Y-%m-%d").date()
    dedup = filter_mod.load_dedup()
    candidates, src_map, stats = [], {}, {}
    # These sources have no dependencies. A slow ACS/foreign feed must not
    # consume the time that otherwise could have served every other source.
    with ThreadPoolExecutor(max_workers=4) as pool:
        hn_future = pool.submit(retrieve.fetch_hn, now, budget=budget)
        tg_future = pool.submit(retrieve.fetch_telegram_channels,
                                collect_brief.TELEGRAM_CHANNELS, budget=budget)
        acs_future = pool.submit(retrieve.fetch_agent_case_share, budget=budget)
        hz_future = pool.submit(horizon.fetch_horizon, dedup, ws, we, budget=budget)
        hn_items = hn_future.result()
        tg_items = tg_future.result()
        acs_items = acs_future.result()
        hz_promoted, hz_items, hz_unavailable = hz_future.result()
    for c in hn_items:
        c["direction"] = rank.likes_mod.infer_direction(c.get("url", ""), c.get("title", ""))
        c["is_preferred"] = False
        c["is_explore"] = False
        region_mod.bind_source(c, "hn", 'intl')
        c["source_key"] = "hn"
        c["source_label"] = "Hacker News"
        src_map.setdefault("hn", {"label": "Hacker News", "direction": "科技", "items": []})["items"].append(c)
    candidates.extend(hn_items)
    stats["hn"] = len(hn_items)

    for c in tg_items:
        c["direction"] = rank.likes_mod.infer_direction(c.get("url", ""), c.get("title", ""))
        ch = c.get("telegram_channel", "?")
        region_mod.bind_source(c, "tg:" + ch, 'cn' if ch in collect_brief.TELEGRAM_CHANNELS else None)
        c["source_key"] = "tg:" + ch
        c["source_label"] = "TG@" + ch
        src_map.setdefault(c["source_key"], {"label": c["source_label"], "direction": c["direction"], "items": []})["items"].append(c)
    candidates.extend(tg_items)
    stats["telegram"] = len(tg_items)

    for c in acs_items:
        region_mod.bind_source(c, "acs")
        c["source_label"] = "Agent Case Share AI日报"
        src_map.setdefault("acs", {"label": "Agent Case Share AI日报", "direction": "AI", "items": []})["items"].append(c)
    candidates.extend(acs_items)
    stats["agent_case_share"] = len(acs_items)

    hz_config = {"horizon:" + src["key"]: src.get("region") for src in horizon.HORIZON_SOURCES}
    for c in hz_promoted:
        region_mod.bind_source(c, c["source_key"], hz_config.get(c["source_key"]))
        c["is_preferred"] = False
        c["is_explore"] = False
        src_map.setdefault(c["source_key"], {"label": c["source_label"],
                                             "direction": c["direction"], "items": []})["items"].append(c)
    candidates.extend(hz_promoted)
    stats["horizon_fulltext"] = len(hz_promoted)
    stats["horizon"] = len(hz_items)
    return candidates, src_map, stats, hz_items, hz_unavailable


def _run_lanes(context, lane_timeout, deadline=None, initial_items=None):
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
            task = dict(task, initial_items=[c for c in initial_items or []
                                            if c.get('region') == lane])
            futs[ex.submit(agent_lane.run_lane_for_hermes, task, task["lane_dir"],
                           timeout=lane_timeout, deadline=deadline)] = lane
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
            region_mod.bind_lane(c, lane)
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
    evidence_by_url = {filter_mod.norm_url(c.get("url", "")): c for c in candidates}
    for c in kept:
        original = evidence_by_url.get(c["url"], {})
        if "metadata" in original:
            c["metadata"] = original["metadata"]
        if "source_verification" in original:
            c["source_verification"] = original["source_verification"]
        c['url'] = c.get('source_url') or c['url']
    leftovers = collect_brief.build_source_leftovers(src_map, kept, dedup, ws, we,
                                                     budget=budget)
    # The legacy selector returns headline/date only. Formal agent delivery
    # requires an actual page body even for leftovers; no silent snippet bypass.
    qualified_leftovers = []
    for item in leftovers:
        original = evidence_by_url.get(filter_mod.norm_url(item['url']), {})
        body = original.get('content') or ''
        if retrieve.wc(body) < filter_mod.MIN_WORDS:
            continue
        item.update(content=body, metadata=original.get('metadata') or {},
                    word_count=retrieve.wc(body))
        qualified_leftovers.append(item)
    leftovers = qualified_leftovers
    return {
        "run_id": context["run_id"],
        "generated_at": datetime.now(TZ).isoformat(),
        "window": context["window"],
        "preference": context["preference"],
        "delivery": "html",
        "pipeline": {
            "mode": "agent",
            "semantic_review": ("pending_hermes" if any(oc.get("semantic_review")
                                                         for oc in outcomes.values()) else None),
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
                   "semantic_review": oc.get("semantic_review"),
                   "mechanical_rejections": oc.get("mechanical_rejections", []),
                   "lane_dir": context.get("lanes", {}).get(lane, {}).get("lane_dir"),
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
   候选已通过脚本机械证据核验；不要重新搜索、重新选文或运行 reject_candidate.py。
   后续语义复核由Hermes完成。摘要只写本批正文明确支持的事实，不能补入外部记忆。
2. 按模板写简报到上面的简报路径：
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
3. 不要写 ## 👍 点赞 区（脚本统一重建），不要运行 validate/finalize，不要改其他文件。
4. 写完读回简报文件确认结构完整；随后由Hermes对照原文快照做语义复核。
""" % (context["run_id"], context["run_id"], cand_path, brief_path,
       win["start"], win["end"], pref.get("pref_dir") or "无", pref.get("explore_dir") or "无",
       context["run_id"], context["date"])


def _html_prompt(context, cand_path):
    """旧版小g HTML提示词，仅保留兼容；生产交付由controlled_html.write生成。"""
    brief_path = context["batch"]["brief"]
    html_path = context["batch"]["html"]
    return """你是小g，负责 DailyBrief 批次 %s 的 HTML 报告制作。

输入（只读）：
- run_id: %s
- 候选 JSON: %s
- canonical 简报 MD（已通过强校验）: %s
输出（写到这里）: %s

要求：
1. 每篇深度文章使用严格语义结构：<article data-ref="N" data-url="精确原URL">
   <h3 data-role="title">完整canonical标题含偏好/探索标记</h3>
   <p data-role="summary">完整canonical摘要，不能增删改尾部</p>
   <a data-role="citation" href="精确原URL">[N]原文链接</a>
   <details><summary>日期证据</summary>证据</details></article>。
   article除上述标题/摘要/引用及details证据外不得有额外正文；禁止用hidden/CSS隐藏正文。
   内容严格来自上述 MD 与候选 JSON：标题/URL/摘要逐条对应，禁止新增来源事实，
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
7. 必须使用项目 modules.controlled_html.write(上述MD路径,上述候选路径,上述HTML路径)
   生成固定模板，不自行编写CSS/JS，不修改模板模块。只写HTML，禁止validate/finalize或发布。
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
        html_timeout=HTML_TIMEOUT_S, pipeline_timeout=PIPELINE_TIMEOUT_S):
    if pipeline_timeout < pipeline_budget.TOTAL:
        print("[PIPELINE_FAILED] 总预算不足以保留全部阶段")
        return 1
    pipeline_budget.allocation(pipeline_timeout)
    if lane_timeout > LANE_TIMEOUT_S or writer_timeout > WRITER_TIMEOUT_S or html_timeout > HTML_TIMEOUT_S:
        raise ValueError("阶段预算不得吃掉后续保留时间")
    agent_lane.CANCELLED.clear()
    budget = run_store.DeadlineBudget(time.monotonic() + pipeline_budget.RUNNER)
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
        return _run_locked(context, date_s, run_id,
                           lane_timeout, writer_timeout, html_timeout, budget)
    except (ValueError, OSError, KeyError) as e:
        print("[PIPELINE_FAILED] 管线失败：%s" % e)
        return 1
    finally:
        collect_brief.release_lock(lock)


def _run_locked(context, date_s, run_id, lane_timeout, writer_timeout, html_timeout, budget):
    budget.require()
    if context.get('batch', {}).get('run_dir'):
        preflight(context)
    det_budget = budget.derive(DETERMINISTIC_BUDGET_S)
    log("确定性来源采集开始（预算 %ds）" % DETERMINISTIC_BUDGET_S)
    det_candidates, src_map, stats, hz_items, hz_unavailable = \
        _collect_deterministic(context, det_budget)

    log("双路检索车道并行启动（车道总预算 %ds）" % lane_timeout)
    budget.require()
    outcomes = _run_lanes(context, lane_timeout, deadline=budget.derive(lane_timeout).deadline,
                          initial_items=det_candidates)
    if any(o.get("status") != "ok" and any(e.get("takeover_reason") and not e.get("semantic_rejection") for e in o.get("attempts", [])) for o in outcomes.values()) or all(o.get("status") != "ok" for o in outcomes.values()):
        print("[COLLECT_FAILED] 必需车道/接管失败，真实熔断，不用确定性池或历史池替代")
        return 1
    for lane in ("cn", "intl"):
        oc = outcomes[lane]
        log("车道 %s: %s（%s）" % (lane, oc.get("status"), oc.get("reason") or "ok"))

    payload = build_candidates_payload(context, outcomes, det_candidates, src_map,
                                       stats, hz_items, hz_unavailable,
                                       budget=budget.derive(BODY_BUDGET_S))
    budget.require()
    lane_note = "; ".join("%s=%s" % (l, o.get("status")) for l, o in outcomes.items())
    if payload is None:
        print("[COLLECT_FAILED] 合并后候选 0 篇（车道: %s）。熔断：不写候选文件，"
              "本次简报未生成。" % lane_note)
        return 1

    cand_path, brief_path = run_store.create_run(
        collect_brief.CAND_DIR, collect_brief.OUTPUT_DIR, date_s, payload)
    print("候选 %d 篇（run_id %s；车道: %s）" % (len(payload["candidates"]), run_id, lane_note))
    print("候选详情 JSON：%s" % cand_path)
    print("本次简报输出：%s" % brief_path)

    # ── 小g写作 canonical MD（批次锁）──
    writer_prompt = os.path.join(os.path.dirname(cand_path), "writer_prompt.txt")
    with open(writer_prompt, "w", encoding="utf-8") as f:
        f.write(_writer_prompt(context, cand_path))
    rc = run_agy.run(cand_path, writer_prompt, timeout=budget.derive(writer_timeout).require())
    budget.require()
    if rc != 0:
        print("[WRITER_FAILED] 小g写作异常退出 rc=%s" % rc)
        return 1
    if not os.path.isfile(brief_path):
        print("[WRITER_FAILED] 小g未产出简报文件 %s" % brief_path)
        return 1
    if not _validate_and_likes(cand_path, brief_path):
        return 1

    # Fixed rendering is deterministic: no extra model/network session merely
    # to call controlled_html.write. Preserve all consistency/browser gates.
    html_path = context["batch"]["html"]
    controlled_html.write(brief_path, cand_path, html_path)
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
    verify_html_browser.verify(html_path, brief_path, cand_path,
        os.path.join(os.path.dirname(cand_path), "browser"), timeout=budget.require(pipeline_budget.VALIDATION - 10))
    budget.require()
    print("[PIPELINE_OK] 候选+canonical MD+HTML 已就绪，待主席验收后 finalize：")
    print('  python3 "%s" "%s" "%s"'
          % (os.path.join(ROOT, "scripts", "finalize_brief.py"), brief_path, cand_path))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("context", help="prepare_brief.py 生成的 task_context.json")
    ap.add_argument("--lane-timeout", type=int, default=LANE_TIMEOUT_S)
    ap.add_argument("--writer-timeout", type=int, default=WRITER_TIMEOUT_S)
    ap.add_argument("--html-timeout", type=int, default=HTML_TIMEOUT_S)
    ap.add_argument("--pipeline-timeout", type=int, default=PIPELINE_TIMEOUT_S)
    args = ap.parse_args(argv)
    agent_lane.install_cancel_handlers()
    try:
        return run(args.context, args.lane_timeout, args.writer_timeout,
                   args.html_timeout, args.pipeline_timeout)
    except KeyboardInterrupt:
        agent_lane.cancel_sessions()
        print("[PIPELINE_FAILED] cancelled; owned subprocess groups terminated")
        return 130
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("[PIPELINE_FAILED] 管线异常：%s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
