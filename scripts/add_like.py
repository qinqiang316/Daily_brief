#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
点赞管理 CLI：把用户对简报文章的点赞写入 data/likes.json。
采集脚本据此在日后检索中优先找偏好方向内容，同时保留探索位（防茧房）。

用法：
  # 按简报参考资料序号点赞（推荐，简报交付后对序号说"点赞"即可）
  python3 add_like.py --brief Daily-Brief-2026-08-13.md --num 3
  python3 add_like.py --brief Daily-Brief-2026-08-13.md --num 3 5 7

  # 直接按 URL 点赞（可指定方向，缺省自动推断）
  python3 add_like.py --url https://xxx [--title "标题"] [--direction 轨道交通]

  # 查看
  python3 add_like.py --list
  python3 add_like.py --stats

说明：
  - 同 URL 重复点赞自动去重（只保留一次）
  - --direction 取值：AI / 科技 / 商业 / 生活 / 健康 / 轨道交通
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import likes as likes_mod

TZ = timezone(timedelta(hours=8))
BRIEF_DIR = likes_mod.BRIEF_DIR

REF_RE = re.compile(r"\[(\d+)\]\s*\[([^\]]*)\]\((https?://[^)\s]+)\)")


def parse_brief_refs(brief_path):
    """从简报 md 提取参考资料 {序号: (标题, URL)}"""
    with open(brief_path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    refs = {}
    for m in REF_RE.finditer(text):
        refs[int(m.group(1))] = (m.group(2).strip(), m.group(3).strip())
    return refs


def find_brief(num):
    """按序号找简报文件（精确日期或最新）"""
    if os.path.isfile(num):
        return num
    candidates = []
    if os.path.isdir(BRIEF_DIR):
        for f in os.listdir(BRIEF_DIR):
            m = re.match(r"Daily-Brief-(\d{4}-\d{2}-\d{2})\.md$", f)
            if m:
                candidates.append((m.group(1), os.path.join(BRIEF_DIR, f)))
    candidates.sort(reverse=True)
    for d, p in candidates:
        if num in d:
            return p
    return candidates[0][1] if candidates else None


def add_like(url, title="", direction=None):
    """写入一条点赞（去重）；返回 (ok, msg)"""
    nu = likes_mod.norm_url(url)
    if not nu:
        return False, "URL 无效", False
    likes = likes_mod.load_likes()
    for x in likes:
        if likes_mod.norm_url(x.get("url", "")) == nu:
            return False, "已点过赞（去重）: %s" % x.get("title", url), True
    d = direction or likes_mod.infer_direction(nu, title)
    if not d:
        d = "科技"  # 兜底默认，避免无方向
    likes.append({
        "url": nu,
        "title": title or nu[:80],
        "direction": d,
        "liked_at": datetime.now(TZ).isoformat(),
    })
    likes_mod.save_likes(likes)
    return True, "已点赞 [%s] %s" % (d, title or nu), False


def main():
    ap = argparse.ArgumentParser(description="简报点赞管理")
    ap.add_argument("--brief", help="简报文件名或日期（如 Daily-Brief-2026-08-13.md 或 2026-08-13）")
    ap.add_argument("--num", nargs="+", type=int, help="参考资料序号（可多个）")
    ap.add_argument("--url", help="文章 URL")
    ap.add_argument("--title", help="文章标题（可选）")
    ap.add_argument("--direction", choices=likes_mod.DIRECTIONS, help="方向（缺省自动推断）")
    ap.add_argument("--list", action="store_true", help="列出全部点赞")
    ap.add_argument("--stats", action="store_true", help="点赞方向统计")
    args = ap.parse_args()

    if args.list:
        likes = likes_mod.load_likes()
        if not likes:
            print("暂无点赞。")
            return 0
        for i, x in enumerate(likes, 1):
            print("%d. [%s] %s\n   %s（%s）" % (i, x.get("direction", "?"),
                                              x.get("title", ""), x.get("url", ""),
                                              x.get("liked_at", "")[:10]))
        return 0

    if args.stats:
        likes = likes_mod.load_likes()
        if not likes:
            print("暂无点赞。")
            return 0
        from collections import Counter
        cnt = Counter(x.get("direction", "?") for x in likes)
        pref, total, min_total, top, top_cnt, share = likes_mod.pref_progress(likes)
        print("点赞总数: %d" % len(likes))
        for d in likes_mod.DIRECTIONS:
            print("  %s: %d" % (d, cnt.get(d, 0)))
        if pref:
            print("当前偏好方向: %s（%d 条中 %s 占 %d%% ≥ %d%% 下限，已启用偏好优先采集）"
                  % (pref, total, top, round(share * 100), round(likes_mod.MIN_PREF_SHARE * 100)))
        elif total < min_total:
            print("偏好方向: 样本积累中（%d/%d 条，还差 %d 条才启用偏好优先采集）"
                  % (total, min_total, min_total - total))
        elif top:
            print("偏好方向: 待定（%d 条中 %s 占 %d%% < %d%% 下限，方向不够集中）"
                  % (total, top, round(share * 100), round(likes_mod.MIN_PREF_SHARE * 100)))
        else:
            print("偏好方向: 无")
        print("今日探索方向: %s（样本不足阶段照常轮换探索）" % likes_mod.explore_direction(pref))
        return 0

    if args.num:
        if not args.brief:
            print("--num 需要 --brief 指定简报（文件名或日期）")
            return 2
        brief_path = find_brief(args.brief)
        if not brief_path:
            print("找不到简报: %s" % args.brief)
            return 2
        refs = parse_brief_refs(brief_path)
        if not refs:
            print("简报中未解析到参考资料: %s" % brief_path)
            return 2
        ok_cnt = 0
        for n in args.num:
            if n not in refs:
                print("序号 %d 不在参考资料中（现有: %s）" % (n, sorted(refs)))
                continue
            title, url = refs[n]
            ok, msg, dup = add_like(url, title, args.direction)
            print(msg)
            if ok or dup:
                ok_cnt += 1
        return 0 if ok_cnt else 1

    if args.url:
        ok, msg, dup = add_like(args.url, args.title or "", args.direction)
        print(msg)
        return 0 if (ok or dup) else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
