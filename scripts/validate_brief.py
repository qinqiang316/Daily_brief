#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""今日简报强校验闸门：简报只能使用候选池内容（P0-3）。

用法:
  python3 validate_brief.py [简报.md] [候选.json] [--dedup 去重.json]
缺省自动探测：最新 Daily-Brief-*.md，并严格配对同文件日期的候选 JSON
（Daily-Brief-<日期>.md ↔ _candidates/Daily-Brief-<日期>-candidates.json）。
显式传入的两个文件日期不一致 → FAIL（跨日错配）。

校验项（任一项 FAIL → 退出码 1，cron/LLM 不得投递）:
  1. 简报全部 URL（规范化后）⊆ 候选 JSON URL（简报只能用候选池内容）
  2. 简报 URL 不得命中历史简报 URL（已推送过的内容不得复现）
  3. 候选里 date_verified=false 的 URL 不得出现在简报任何位置
  4. 候选 JSON generated_at 必须早于简报 mtime（候选先生成、简报后写）
  5. 防茧房：候选池存在**合格**探索候选（is_explore=true 且通过实际正文 ≥500 字、
     独立文章（非聚合页）、日精度日期、date_source/date_evidence 齐全、窗口内、
     合法 URL、未命中历史去重）时，简报必须至少收录 1 篇探索内容；合格探索遗漏仍 FAIL
  6. 防茧房：深度总结区"（偏好命中）"条目 ≤ 上限（默认 8）
  7. 未知日期禁止交付（含速览）：date_verified=false 的 leftover 出现在
     简报任何位置 → FAIL；候选产物本身携带未验证 leftover → FAIL；
     leftover 还须日精度、带 date_source/date_evidence、落在候选窗口内、
     非聚合页（导航页/旧日速览不得旁路）
  8. 日期配对：简报与候选文件名日期必须一致；简报中引用候选的 publish_date
     必须落在候选窗口 [window.start, window.end] 内且精确到日；
     正文质量以真实 content 实际 wc 复核（word_count 元数据仅参考，不得冒充证据）；
     候选必须带 date_source 与 date_evidence（旧格式只有 bool 位不能冒充验证）；
     引用候选不得为聚合页（首页/频道/列表/导航）
  9. run_id 配对：候选 JSON 带 run_id 时，简报必须含 <!-- run_id: ... --> 且一致
 10. 拒收协议（可审计）：rejected_candidates 每项须带 url/is_explore/reason/
     reviewed_by/reviewed_at；拒收 URL 无论出现在正文/参考资料/速览均 FAIL；
     拒收项不得仍留在 candidates/source_leftovers；严禁靠取消 is_explore 绕过——
     原池仍有不合格探索时必须先审核移入 rejected_candidates，不得静默忽略；
     有探索拒收但无合格探索时，须候选 JSON 含 exploration_replenishment
     （status=exhausted、attempts 为整数1或2、reason 非空文本）且简报注明「探索内容缺货」，
     才可缩减交付（补抓记录不豁免其余候选的日期/正文/去重硬条件）
"""
import json
import os
import re
import sys
import io
from contextlib import redirect_stdout
from pathlib import Path
from datetime import datetime

BRIEF_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUTPUT_DIR = os.path.join(BRIEF_DIR, "output")
SCRIPTS_DIR = os.path.join(BRIEF_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)
import brief_record
import collect_brief  # 复用 norm_url / BRIEF_DIR / CAND_DIR / DEDUP_FILE（模块顶层无副作用）
import likes as likes_mod
from modules import run_store

CAND_DIR = collect_brief.CAND_DIR
MIN_WORDS = collect_brief.filter_mod.MIN_WORDS
DATE_RE = re.compile(r"Daily-Brief-(\d{4}-\d{2}-\d{2})")
RUN_ID_RE = re.compile(r"<!--\s*run_id:\s*(\S+)\s*-->")


def file_date(path):
    """从文件名提取 Daily-Brief 日期，无日期返回 None。"""
    if not path:
        return None
    m = DATE_RE.search(os.path.basename(path))
    return m.group(1) if m else None


def find_latest(d, prefix, ext):
    best = None
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.startswith(prefix) and f.endswith(ext):
                p = os.path.join(d, f)
                if best is None or os.path.getmtime(p) > os.path.getmtime(best):
                    best = p
    return best


def extract_urls(md_path, text=None):
    if text is None:
        with open(md_path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    urls = set()
    for m in re.finditer(r"https?://[^\s)\]>]+", text):
        u = collect_brief.norm_url(m.group(0))
        if not u:
            continue
        # 忽略本机点赞服务链接（like_links.py 追加的点赞区，不参与候选池校验）
        host = u.split("/")[2].lower() if "://" in u else ""
        if host in ("127.0.0.1:8900", "localhost:8900", "127.0.0.1", "localhost"):
            continue
        urls.add(u)
    return urls


def _validate_main(candidate_bytes=None, brief_bytes=None):
    args = list(sys.argv[1:])
    brief, cand, dedup = None, None, collect_brief.DEDUP_FILE
    while args:
        a = args.pop(0)
        if a == "--dedup":
            dedup = args.pop(0) if args else dedup
        elif not brief and a.endswith(".md"):
            brief = a
        elif not cand and a.endswith(".json"):
            cand = a
    if not brief:
        brief = find_latest(OUTPUT_DIR, "Daily-Brief-", ".md")
    if not brief or not os.path.exists(brief):
        print("FAIL: 找不到简报 %s" % brief)
        return 1

    # 日期配对：简报与候选必须同文件日期，禁止跨日错配
    brief_date = file_date(brief)
    if not brief_date:
        print("FAIL: 简报文件名无日期（要求 Daily-Brief-YYYY-MM-DD.md）: %s" % brief)
        return 1
    if cand:
        cand_date = file_date(cand)
        if cand_date and cand_date != brief_date:
            print("FAIL: 跨日错配：简报日期 %s ≠ 候选日期 %s（%s ↔ %s）"
                  % (brief_date, cand_date, brief, cand))
            return 1
    else:
        cand = os.path.join(CAND_DIR, "Daily-Brief-%s-candidates.json" % brief_date)
    if not cand or not os.path.exists(cand):
        print("FAIL: 找不到与简报同日期的候选 JSON %s" % cand)
        return 1

    errors = []

    # 候选 URL 集合 + 日期未验证集合 + 探索集合 + 逐候选证据
    data = json.loads(candidate_bytes if candidate_bytes is not None else Path(cand).read_bytes())
    brief_text = (brief_bytes if brief_bytes is not None else Path(brief).read_bytes()).decode(
        "utf-8", errors="ignore")
    generated_at = data.get("generated_at", "")
    pref_info = data.get("preference", {})
    window = data.get("window", {}) or {}
    win_start = window.get("start") or ""
    win_end = window.get("end") or ""
    window_valid = (collect_brief.filter_mod.is_iso_day(win_start)
                    and collect_brief.filter_mod.is_iso_day(win_end)
                    and win_start <= win_end)
    if not window_valid:
        errors.append("候选窗口缺失/非法（须为合法日精度日期且 start <= end）")
    cand_urls = set()
    unverified_urls = set()
    pref_count_in_cand = 0
    cand_map = {}
    for c in data.get("candidates", []):
        u = collect_brief.norm_url(c.get("url", ""))
        if u:
            cand_urls.add(u)
            cand_map[u] = c
            if not c.get("date_verified"):
                unverified_urls.add(u)
            if c.get("is_preferred"):
                pref_count_in_cand += 1
    # 拒收协议：rejected_candidates 逐项审计字段校验；拒收 URL 禁止出现在简报任何位置，
    # 也不得仍留在 candidates/source_leftovers（须移除后移入 rejected_candidates）
    rejected_urls = set()
    rejected_explore = []
    for r in data.get("rejected_candidates") or []:
        if not isinstance(r, dict):
            errors.append("拒收记录格式非法（非对象）: %s" % str(r)[:80])
            continue
        ru = collect_brief.norm_url(r.get("url", ""))
        missing = [k for k in ("url", "reason", "reviewed_by", "reviewed_at")
                   if not str(r.get(k) or "").strip()]
        if "is_explore" not in r:
            missing.append("is_explore")
        if not ru and "url" not in missing:
            missing.append("url(非法)")
        if missing:
            errors.append("拒收记录缺审计字段（%s）: %s"
                          % (",".join(missing), str(r.get("url"))[:80]))
        if ru:
            rejected_urls.add(ru)
            if r.get("is_explore"):
                rejected_explore.append(ru)
    # 未推荐源速览（source_leftovers）：允许进简报参考资料区；
    # 未知日期禁止交付（含速览）：date_verified=false 的 leftover 出现即 FAIL；
    # 且须日精度、带 date_source、落在候选窗口内（禁止导航页/旧日速览旁路）
    leftover_urls = set()
    leftover_unv = set()
    leftover_bad = []
    for lo in data.get("source_leftovers", []):
        u = collect_brief.norm_url(lo.get("url", ""))
        if not u:
            leftover_bad.append("速览 URL 非法: %s" % str(lo.get("url", ""))[:80])
            continue
        cand_urls.add(u)
        leftover_urls.add(u)
        if collect_brief.filter_mod.is_aggregate_url(u):
            leftover_bad.append("速览为聚合页（首页/频道/列表/导航），非独立文章: %s" % u)
        if not lo.get("date_verified"):
            leftover_unv.add(u)
        pub = lo.get("publish_date") or ""
        if not collect_brief.filter_mod.is_iso_day(pub):
            leftover_bad.append("速览发布日期缺失/非法（要求合法 YYYY-MM-DD）: %s" % u)
            continue
        if lo.get("date_precision") != "day":
            leftover_bad.append("速览日期非日精度（%s）: %s" % (lo.get("date_precision"), u))
        if not collect_brief.filter_mod.has_date_provenance(lo):
            leftover_bad.append("速览缺少日期来源/证据 date_source/date_evidence（布尔位不能冒充验证）: %s" % u)
        if window_valid and pub < win_start:
            leftover_bad.append("速览日期 %s 早于窗口起点 %s（旧日速览禁止旁路）: %s" % (pub, win_start, u))
        if window_valid and pub > win_end:
            leftover_bad.append("速览日期 %s 晚于窗口终点 %s: %s" % (pub, win_end, u))
    if leftover_unv:
        errors.append("候选产物携带 %d 条日期未验证的未推荐源（采集产物不合格，未知日期禁止交付）: %s"
                      % (len(leftover_unv), " ".join(sorted(leftover_unv)[:5])))
    for msg in leftover_bad:
        errors.append("候选产物速览不合格（采集产物不合格）: %s" % msg)

    # 视野拓展（horizon）：付费墙/反爬源只收标题+原文发布日期。
    # 仅允许出现在简报「## 视野拓展」区，禁止进深度总结；日期须日精度、有证据、
    # 落在本次窗口内，且不得是首页/频道/列表聚合页。
    horizon_urls = set()
    horizon_bad = []
    for hz in data.get("horizon", []):
        u = collect_brief.norm_url(hz.get("url", ""))
        if not u:
            horizon_bad.append("视野拓展 URL 非法: %s" % str(hz.get("url", ""))[:80])
            continue
        cand_urls.add(u)
        horizon_urls.add(u)
        if not str(hz.get("title") or "").strip():
            horizon_bad.append("视野拓展缺标题: %s" % u)
        if collect_brief.filter_mod.is_aggregate_url(u):
            horizon_bad.append("视野拓展为聚合页（首页/频道/列表/导航），非独立文章: %s" % u)
        pub = hz.get("publish_date") or ""
        if not collect_brief.filter_mod.is_iso_day(pub):
            horizon_bad.append("视野拓展发布日期缺失/非法（要求合法 YYYY-MM-DD）: %s" % u)
            continue
        if hz.get("date_precision") != "day":
            horizon_bad.append("视野拓展日期非日精度（%s）: %s" % (hz.get("date_precision"), u))
        if not collect_brief.filter_mod.has_date_provenance(hz):
            horizon_bad.append("视野拓展缺少日期来源/证据 date_source/date_evidence: %s" % u)
        if window_valid and pub < win_start:
            horizon_bad.append("视野拓展日期 %s 早于窗口起点 %s: %s" % (pub, win_start, u))
        if window_valid and pub > win_end:
            horizon_bad.append("视野拓展日期 %s 晚于窗口终点 %s: %s" % (pub, win_end, u))
    for msg in horizon_bad:
        errors.append("候选产物视野拓展不合格（采集产物不合格）: %s" % msg)
    hz_rej = horizon_urls & rejected_urls
    if hz_rej:
        errors.append("拒收 URL 仍保留在 horizon（须移入 rejected_candidates）: %s"
                      % " ".join(sorted(hz_rej)[:5]))
    still_listed = rejected_urls & cand_urls
    if still_listed:
        errors.append("拒收 URL 仍保留在 candidates/source_leftovers（协议要求移除并移入 rejected_candidates）: %s"
                      % " ".join(sorted(still_listed)[:5]))

    # 去重集合：排除当前正在校验的简报自身，确保只和历史/其他简报比对
    historical_dedup_urls = set()
    current_brief_abs = os.path.abspath(brief)
    if os.path.isdir(OUTPUT_DIR):
        for f in os.listdir(OUTPUT_DIR):
            if not re.match(r"Daily-Brief-\d{4}-\d{2}-\d{2}[^.]*\.md$", f):
                continue
            fp = os.path.abspath(os.path.join(OUTPUT_DIR, f))
            if fp == current_brief_abs:
                continue
            # Published alias and its identical run snapshot are the same edition.
            try:
                if Path(fp).read_bytes() == brief_text.encode("utf-8") and RUN_ID_RE.search(brief_text):
                    continue
                with open(fp, encoding="utf-8", errors="ignore") as fh:
                    for m in re.finditer(r"https?://[^\s)\]>]+", fh.read()):
                        nu = collect_brief.norm_url(m.group(0))
                        if nu:
                            host = nu.split("/")[2].lower() if "://" in nu else ""
                            if host not in ("127.0.0.1:8900", "localhost:8900", "127.0.0.1", "localhost"):
                                historical_dedup_urls.add(nu)
            except Exception as e:
                errors.append("扫描历史简报 %s 失败: %s" % (f, e))

    brief_urls = extract_urls(brief, text=brief_text)
    if not brief_urls:
        print("FAIL: 简报中未提取到任何 URL")
        return 1

    # 1) 简报 ⊆ 候选（候选池外的 URL 若命中历史去重 → 追加说明，双重违规）
    outside = brief_urls - cand_urls
    if outside:
        dup2 = outside & historical_dedup_urls
        msg = "简报 %d 条 URL 不在候选池" % len(outside)
        if dup2:
            msg += "，其中 %d 条已在历史简报推送过: %s" % (len(dup2), " ".join(sorted(dup2)[:8]))
        msg += ": %s" % " ".join(sorted(outside)[:8])
        errors.append(msg)

    # 2) 简报 URL 不得命中历史去重集合（已推送过的内容不得复现）
    dup = brief_urls & historical_dedup_urls
    if dup:
        errors.append("简报 %d 条 URL 命中历史去重集合（已推送过不得复现）: %s"
                      % (len(dup), " ".join(sorted(dup)[:8])))

    # 3) 日期未验证不得出现
    unv = brief_urls & unverified_urls
    if unv:
        errors.append("简报 %d 条 URL 日期未验证（不得进任何区）: %s"
                      % (len(unv), " ".join(sorted(unv)[:8])))

    # 3.5) 拒收 URL 无论出现在正文/参考资料/速览均禁止交付
    rej_hit = brief_urls & rejected_urls
    if rej_hit:
        errors.append("简报 %d 条 URL 已被审核拒收（rejected_candidates，任何区域出现均禁止）: %s"
                      % (len(rej_hit), " ".join(sorted(rej_hit)[:8])))

    # 4) 候选先生成
    if generated_at:
        try:
            gen_dt = datetime.fromisoformat(generated_at)
            if gen_dt.tzinfo is not None:
                gen_dt = gen_dt.astimezone().replace(tzinfo=None)  # 统一 naive 比较
            brief_mtime = datetime.fromtimestamp(os.path.getmtime(brief))
            if gen_dt > brief_mtime:
                errors.append("候选 JSON(%s) 晚于简报生成(%s)：简报未走候选池"
                              % (gen_dt.isoformat(), brief_mtime.isoformat()))
        except Exception as e:
            errors.append("候选时间解析失败: %s" % e)

    # 5) 防茧房：合格探索候选必须收录 ≥1 篇；有探索拒收但无合格探索时，
    #    须有界补抓记录 + 简报注明「探索内容缺货」才可缩减交付
    def _explore_qualified(c, u):
        """复用缓存严格准入，并叠加探索标记、历史去重与拒收检查。"""
        return (c.get("is_explore") and u
                and u not in rejected_urls and u not in historical_dedup_urls
                and collect_brief._qualified_cache_candidate(c, win_start, win_end))

    qualified_explore = set()
    for c in data.get("candidates", []):
        if not c.get("is_explore"):
            continue
        u = collect_brief.norm_url(c.get("url", ""))
        if _explore_qualified(c, u):
            qualified_explore.add(u)
        else:
            errors.append("原候选池仍有不合格探索：须先审核并移入 rejected_candidates，不得静默忽略: %s"
                          % str(c.get("url", ""))[:160])
    explore_hit = brief_urls & qualified_explore
    if qualified_explore and not explore_hit:
        errors.append("候选池有 %d 篇合格探索候选（%s）但简报未收录任何探索条目（防信息茧房）"
                      % (len(qualified_explore), " ".join(sorted(qualified_explore)[:3])))
    if not qualified_explore and rejected_explore:
        rep = data.get("exploration_replenishment")
        rep_ok = (isinstance(rep, dict) and rep.get("status") == "exhausted"
                  and type(rep.get("attempts")) is int and rep["attempts"] in (1, 2)
                  and isinstance(rep.get("reason"), str) and rep["reason"].strip())
        if not rep_ok:
            errors.append("探索候选被拒收且无合格探索：候选 JSON 须含 exploration_replenishment"
                          "（status=exhausted、attempts=1或2、reason 非空）才可缩减交付")
        if "探索内容缺货" not in brief_text:
            errors.append("探索候选被拒收且无合格探索：简报须注明「探索内容缺货」才可缩减交付")

    # 6) 防茧房：深度总结区偏好命中 ≤ 上限
    deep_sec = brief_text.split("## 今日热门文章", 1)
    deep_text = deep_sec[1].split("## 今日主题趋势", 1)[0] if len(deep_sec) > 1 else ""
    pref_marks = len(re.findall(r"[（\(]偏好命中[）\)]", deep_text))
    if pref_marks > likes_mod.MAX_PREF_DEEP:
        errors.append("深度总结区偏好命中 %d 条 > 上限 %d（防信息茧房，探索内容 1-2 篇/天）"
                      % (pref_marks, likes_mod.MAX_PREF_DEEP))
    # 视野拓展条目只允许出现在「## 视野拓展」区：出现在深度总结/TLDR 区 → FAIL
    if horizon_urls:
        section = ""
        misplaced = set()
        for line in brief_text.splitlines():
            heading = re.match(r"^##(?!#)\s+(.+)$", line.strip())
            if heading:
                section = heading.group(1).strip()
            line_urls = {collect_brief.norm_url(m.group(0))
                         for m in re.finditer(r"https?://[^\s)\]>]+", line)}
            if section != "视野拓展":
                misplaced.update(line_urls & horizon_urls)
        if misplaced:
            errors.append("视野拓展 URL 出现在其它区域（仅允许「## 视野拓展」）: %s"
                          % " ".join(sorted(misplaced)[:5]))
        deep_urls = {collect_brief.norm_url(m.group(0))
                     for m in re.finditer(r"https?://[^\s)\]\>]+", deep_text)}
        hz_deep = (deep_urls & horizon_urls) - {""}
        if hz_deep:
            errors.append("视野拓展条目出现在深度总结区（只允许写「视野拓展」区）: %s"
                          % " ".join(sorted(hz_deep)[:5]))
        tldr_urls = {collect_brief.norm_url(m.group(0))
                     for m in re.finditer(r"https?://[^\s)\]\>]+", brief_text.split("## 今日热门文章", 1)[0])}
        hz_tldr = (tldr_urls & horizon_urls) - {""}
        if hz_tldr:
            errors.append("视野拓展条目出现在 TLDR 区（只允许写「视野拓展」区）: %s"
                          % " ".join(sorted(hz_tldr)[:5]))
        quick_part = ""
        if "## 快速浏览" in brief_text:
            quick_part = brief_text.split("## 快速浏览", 1)[1]
            nxt = re.search(r"\n## ", quick_part)
            if nxt:
                quick_part = quick_part[:nxt.start()]
        quick_urls = {collect_brief.norm_url(m.group(0))
                      for m in re.finditer(r"https?://[^\s)\]\>]+", quick_part)}
        hz_quick = (quick_urls & horizon_urls) - {""}
        if hz_quick:
            errors.append("视野拓展条目出现在快速浏览区（只允许写「视野拓展」区）: %s"
                          % " ".join(sorted(hz_quick)[:5]))

    # 7) 未知日期禁止交付（含速览）：日期未验证 leftover 出现在简报任何位置 → FAIL
    if leftover_unv:
        unv_lo = brief_urls & leftover_unv
        if unv_lo:
            errors.append("简报 %d 条未推荐源 URL 日期未验证（未知日期含速览禁止交付）: %s"
                          % (len(unv_lo), " ".join(sorted(unv_lo)[:8])))

    # 8) 窗口与正文质量证据：简报引用的候选必须 publish_date 精确到日、落在候选窗口内、
    #    正文 word_count ≥ MIN_WORDS；窗口外不得进深度/TLDR/快读（新采集已不入池，这里兜底校验）
    for u in sorted(brief_urls & set(cand_map)):
        c = cand_map[u]
        pub = c.get("publish_date") or ""
        if not collect_brief.filter_mod.is_iso_day(pub):
            errors.append("候选无发布日期或日期非法（要求合法 YYYY-MM-DD）: %s" % u)
            continue
        if c.get("date_precision") != "day":
            errors.append("候选日期非日精度（%s）: %s" % (c.get("date_precision"), u))
        if not collect_brief.filter_mod.has_date_provenance(c):
            errors.append("候选缺少日期来源/证据 date_source/date_evidence（旧格式布尔位不能冒充验证）: %s" % u)
        if collect_brief.filter_mod.is_aggregate_url(u):
            errors.append("候选为聚合页（首页/频道/列表/导航），非独立文章: %s" % u)
        if window_valid and pub < win_start:
            errors.append("候选日期 %s 早于窗口起点 %s（窗口外不得进深度/TLDR/快读）: %s" % (pub, win_start, u))
        if window_valid and pub > win_end:
            errors.append("候选日期 %s 晚于窗口终点 %s: %s" % (pub, win_end, u))
        if c.get("window_outside_days"):
            errors.append("候选标记窗口外 %d 天，不得进简报: %s" % (c["window_outside_days"], u))
        declared_wc = c.get("word_count") or 0
        actual_wc = collect_brief.retrieve.wc(c.get("content") or "")
        if declared_wc < MIN_WORDS:
            errors.append("候选正文证据不足（%d 字 < %d）: %s" % (declared_wc, MIN_WORDS, u))
        elif actual_wc < MIN_WORDS:
            errors.append("候选正文实测不足（实际 %d 字 < %d，word_count 元数据仅参考不得冒充证据）: %s"
                          % (actual_wc, MIN_WORDS, u))

    # 9) run_id 配对：候选带 run_id 时简报必须标注且一致（不只是错配拒绝）
    run_id = data.get("run_id")
    m_rid = RUN_ID_RE.search(brief_text)
    if run_id and not m_rid:
        errors.append("候选带 run_id(%s) 但简报未标注 <!-- run_id: ... -->（新池必须标注）" % run_id)
    elif m_rid:
        if not run_id:
            errors.append("简报带 run_id(%s) 但候选 JSON 无 run_id（候选产物过旧或不匹配）" % m_rid.group(1))
        elif m_rid.group(1) != run_id:
            errors.append("run_id 错配：简报 %s ≠ 候选 %s（简报未基于本次候选池撰写）"
                          % (m_rid.group(1), run_id))

    if errors:
        print("FAIL: %d 项违规" % len(errors))
        for e in errors:
            print("  ✗ %s" % e)
        print("候选 JSON: %s" % cand)
        print("简报: %s" % brief)
        return 1
    print("PASS: 简报 %d 条 URL 全部来自候选池，无去重复现，无日期未验证条目" % len(brief_urls))
    print("未推荐源速览放行 %d 条（source_leftovers）" % len(data.get("source_leftovers", [])))
    print("视野拓展放行 %d 条（horizon，仅标题+原文发布日期）" % len(data.get("horizon", [])))
    print("简报: %s" % brief)
    print("候选: %s" % cand)
    pref_dir = pref_info.get("pref_dir")
    explore_dir = pref_info.get("explore_dir")
    likes_count = pref_info.get("likes_count", 0)
    print("偏好: 方向=%s | 点赞=%d | 候选内偏好=%d | 深度总结标记=%d | 探索: 方向=%s 合格候选=%d 收录=%d 拒收=%d"
          % (pref_dir or "无", likes_count, pref_count_in_cand, pref_marks,
             explore_dir or "无", len(qualified_explore), len(explore_hit), len(rejected_explore)))
    return 0


def main():
    """Validate stable inputs, persist the actual evidence, then record metadata."""
    args = sys.argv[1:]
    paths = [a for a in args if not a.startswith("--")]
    if "--dedup" in args:
        value = args[args.index("--dedup") + 1:args.index("--dedup") + 2]
        paths = [p for p in paths if p not in value]
    brief = next((p for p in paths if p.endswith(".md")), None)
    cand = next((p for p in paths if p.endswith(".json")), None)
    brief = brief or find_latest(OUTPUT_DIR, "Daily-Brief-", ".md")
    if not brief or not os.path.isfile(brief):
        print("FAIL: 找不到简报 %s" % brief)
        return 1
    brief_bytes = Path(brief).read_bytes()
    day = file_date(brief)
    rid_match = RUN_ID_RE.search(brief_bytes.decode("utf-8", errors="ignore"))
    if not cand and day:
        # Never select a newer daily alias when an exact snapshot is available.
        cand = (run_store.resolve_candidate(CAND_DIR, day, rid_match.group(1))
                if rid_match else None)
        cand = cand or os.path.join(CAND_DIR, "Daily-Brief-%s-candidates.json" % day)
    if not cand or not os.path.isfile(cand):
        print("FAIL: 找不到与简报同日期的候选 JSON %s" % cand)
        return 1
    candidate_bytes = Path(cand).read_bytes()
    saved_argv = sys.argv
    capture = io.StringIO()
    try:
        sys.argv = [saved_argv[0], brief, cand] + args
        with redirect_stdout(capture):
            rc = _validate_main(candidate_bytes=candidate_bytes, brief_bytes=brief_bytes)
    except (ValueError, OSError, TypeError) as exc:
        rc = 1
        capture.write("FAIL: 校验异常 %s\n" % exc)
    finally:
        sys.argv = saved_argv
    if Path(cand).read_bytes() != candidate_bytes or Path(brief).read_bytes() != brief_bytes:
        rc = 1
        capture = io.StringIO("FAIL: 校验过程中候选或简报被修改，禁止交付\n")
    output = capture.getvalue()
    try:
        receipt = run_store.save_validation(cand, brief, candidate_bytes, brief_bytes,
                                            "PASS" if rc == 0 else "FAIL", output)
    except (ValueError, OSError) as exc:
        print("FAIL: 无法保存校验凭据 %s" % exc)
        return 1
    print(output, end="")
    if receipt:
        print("校验凭据：%s" % receipt)
    if rc == 0 and "--no-record" not in args and run_store.managed_run(cand) is None:
        brief_record.record_brief(brief, cand)
    return rc


if __name__ == "__main__":
    sys.exit(main())
