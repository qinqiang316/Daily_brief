#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""今日简报强校验闸门：简报只能使用候选池内容（P0-3）。

用法:
  python3 validate_brief.py [简报.md] [候选.json] [--dedup 去重.json]
缺省自动探测：最新 Daily-Brief-*.md 与 _candidates/ 下最新候选 JSON。

校验项（任一项 FAIL → 退出码 1，cron/LLM 不得投递）:
  1. 简报全部 URL（规范化后）⊆ 候选 JSON URL（简报只能用候选池内容）
  2. 简报 URL 不得命中 _dedup_urls.json（已推送过的内容不得复现）
  3. 候选里 date_verified=false 的 URL 不得出现在简报任何位置
     （skill 规则：日期未验证不得进深度总结/TLDR/快读，也不得列链接）
  4. 候选 JSON generated_at 必须早于简报 mtime（候选先生成、简报后写）
  5. 防茧房：候选池存在探索候选（is_explore=true 且日期已验证）时，
     简报必须至少收录 1 篇探索内容（防 LLM 写作时偏好挤掉探索位）
  6. 防茧房：深度总结区"（偏好命中）"条目 ≤ 上限（默认 8），
     防止偏好方向垄断（上限与 likes.MAX_PREF_DEEP 一致）
"""
import json
import os
import re
import sys
from datetime import datetime

BRIEF_DIR = "/Users/qqiang/AI project/05-日常工具/DailyBrief"
SCRIPTS_DIR = os.path.join(BRIEF_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)
import collect_brief  # 复用 norm_url / BRIEF_DIR / CAND_DIR / DEDUP_FILE（模块顶层无副作用）
import likes as likes_mod

CAND_DIR = collect_brief.CAND_DIR


def find_latest(d, prefix, ext):
    best = None
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.startswith(prefix) and f.endswith(ext):
                p = os.path.join(d, f)
                if best is None or os.path.getmtime(p) > os.path.getmtime(best):
                    best = p
    return best


def extract_urls(md_path):
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


def main():
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
        brief = find_latest(BRIEF_DIR, "Daily-Brief-", ".md")
    if not cand:
        cand = find_latest(CAND_DIR, "Daily-Brief-", ".json")
    if not brief or not os.path.exists(brief):
        print("FAIL: 找不到简报 %s" % brief)
        return 1
    if not cand or not os.path.exists(cand):
        print("FAIL: 找不到候选 JSON %s" % cand)
        return 1

    errors = []

    # 候选 URL 集合 + 日期未验证集合 + 探索集合
    with open(cand, encoding="utf-8") as f:
        data = json.load(f)
    cand_urls = set()
    unverified_urls = set()
    explore_urls = set()
    pref_count_in_cand = 0
    for c in data.get("candidates", []):
        u = collect_brief.norm_url(c.get("url", ""))
        if u:
            cand_urls.add(u)
            if not c.get("date_verified"):
                unverified_urls.add(u)
            if c.get("is_explore") and c.get("date_verified"):
                explore_urls.add(u)
            if c.get("is_preferred"):
                pref_count_in_cand += 1
    generated_at = data.get("generated_at", "")
    pref_info = data.get("preference", {})

    # 去重集合
    dedup_urls = set()
    if os.path.exists(dedup):
        try:
            with open(dedup, encoding="utf-8") as f:
                for u in json.load(f):
                    nu = collect_brief.norm_url(u)
                    if nu:
                        dedup_urls.add(nu)
        except Exception as e:
            errors.append("读去重集合失败: %s" % e)

    brief_urls = extract_urls(brief)
    if not brief_urls:
        print("FAIL: 简报中未提取到任何 URL")
        return 1

    # 1) 简报 ⊆ 候选（候选池外的 URL 若命中历史去重 → 追加说明，双重违规）
    outside = brief_urls - cand_urls
    if outside:
        dup2 = outside & dedup_urls
        msg = "简报 %d 条 URL 不在候选池" % len(outside)
        if dup2:
            msg += "，其中 %d 条已在历史简报推送过: %s" % (len(dup2), " ".join(sorted(dup2)[:8]))
        msg += ": %s" % " ".join(sorted(outside)[:8])
        errors.append(msg)

    # 3) 日期未验证不得出现
    unv = brief_urls & unverified_urls
    if unv:
        errors.append("简报 %d 条 URL 日期未验证（不得进任何区）: %s"
                      % (len(unv), " ".join(sorted(unv)[:8])))

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

    # 5) 防茧房：候选池有探索候选 → 简报必须收录 ≥1 篇探索
    explore_hit = brief_urls & explore_urls
    if explore_urls and not explore_hit:
        errors.append("候选池有 %d 篇探索候选（%s）但简报未收录任何探索条目（防信息茧房）"
                      % (len(explore_urls), " ".join(sorted(explore_urls)[:3])))

    # 6) 防茧房：深度总结区偏好命中 ≤ 上限
    with open(brief, encoding="utf-8", errors="ignore") as fh:
        brief_text = fh.read()
    deep_sec = brief_text.split("## 今日热门文章", 1)
    deep_text = deep_sec[1].split("## 今日主题趋势", 1)[0] if len(deep_sec) > 1 else ""
    pref_marks = len(re.findall(r"（偏好命中）", deep_text))
    if pref_marks > likes_mod.MAX_PREF_DEEP:
        errors.append("深度总结区偏好命中 %d 条 > 上限 %d（防信息茧房，探索内容 1-2 篇/天）"
                      % (pref_marks, likes_mod.MAX_PREF_DEEP))

    if errors:
        print("FAIL: %d 项违规" % len(errors))
        for e in errors:
            print("  ✗ %s" % e)
        print("候选 JSON: %s" % cand)
        print("简报: %s" % brief)
        return 1
    print("PASS: 简报 %d 条 URL 全部来自候选池，无去重复现，无日期未验证条目" % len(brief_urls))
    print("简报: %s" % brief)
    print("候选: %s" % cand)
    pref_dir = pref_info.get("pref_dir")
    explore_dir = pref_info.get("explore_dir")
    likes_count = pref_info.get("likes_count", 0)
    print("偏好: 方向=%s | 点赞=%d | 候选内偏好=%d | 深度总结标记=%d | 探索: 方向=%s 候选=%d 收录=%d"
          % (pref_dir or "无", likes_count, pref_count_in_cand, pref_marks,
             explore_dir or "无", len(explore_urls), len(explore_hit)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
