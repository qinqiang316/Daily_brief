#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DailyBrief 统一命令行入口（A4）。

把 scripts/ 下各独立脚本收敛到 dailybrief 子命令，跨过 cron 也能手动运行。
本文件只做导入与分发，逻辑仍复用各脚本的 main()，不改写现有内部逻辑。
"""
import argparse
import json
import os
import sys
from collections import Counter

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

import add_like
import collect_brief
import discover_sources
import like_ctl
import like_links
import likes as likes_mod
import validate_brief

SOURCES_FILE = os.path.join(ROOT, "data", "sources.json")


def _invoke(main_func, argv):
    """把子命令参数换成底层脚本形式并复用其 main()。"""
    sys.argv = [os.path.abspath(__file__)] + list(argv)
    return main_func()


def _reconstruct_like(args):
    argv = []
    if args.brief:
        argv += ["--brief", args.brief]
    if args.num:
        argv += ["--num"] + [str(n) for n in args.num]
    if args.url:
        argv += ["--url", args.url]
    if args.title:
        argv += ["--title", args.title]
    if args.direction:
        argv += ["--direction", args.direction]
    if args.list:
        argv.append("--list")
    if args.stats:
        argv.append("--stats")
    return argv


def _reconstruct_validate(args):
    argv = list(args.paths or [])
    if args.dedup:
        argv += ["--dedup", args.dedup]
    return argv


def _reconstruct_discover(args):
    argv = []
    if args.limit is not None:
        argv += ["--limit", str(args.limit)]
    if args.top is not None:
        argv += ["--top", str(args.top)]
    return argv


def _reconstruct_serve(args):
    argv = [args.action]
    if args.idle_timeout is not None:
        argv += ["--idle-timeout", str(args.idle_timeout)]
    return argv


def _reconstruct_links(args):
    argv = []
    if args.brief:
        argv.append(args.brief)
    if args.check:
        argv.append("--check")
    return argv


def _latest_candidate():
    cand_dir = collect_brief.CAND_DIR
    best = None
    if os.path.isdir(cand_dir):
        for name in os.listdir(cand_dir):
            if name.startswith("Daily-Brief-") and name.endswith("-candidates.json"):
                path = os.path.join(cand_dir, name)
                if best is None or os.path.getmtime(path) > os.path.getmtime(best):
                    best = path
    if not best:
        return None
    try:
        with open(best, encoding="utf-8") as fh:
            data = json.load(fh)
        return {
            "path": best,
            "generated_at": data.get("generated_at", "未知"),
            "count": len(data.get("candidates", [])),
        }
    except (OSError, ValueError):
        return {"path": best, "generated_at": "读取失败", "count": "未知"}


def cmd_status(_args):
    print("=== DailyBrief 系统状态 ===")

    likes = likes_mod.load_likes()
    print("点赞总数: %d" % len(likes))
    cnt = Counter(x.get("direction", "?") for x in likes)
    for d in likes_mod.DIRECTIONS:
        print("  %s: %d" % (d, cnt.get(d, 0)))
    if cnt.get("?"):
        print("  其他/未知: %d" % cnt["?"])
    pref = likes_mod.preference_direction(likes)
    print("偏好方向: %s" % (pref or "未启用"))
    print("今日探索方向: %s" % likes_mod.explore_direction(pref))

    stat = {"candidate": 0, "confirmed": 0, "other": 0}
    try:
        with open(SOURCES_FILE, encoding="utf-8") as fh:
            sdata = json.load(fh)
        for s in sdata.get("sources", []):
            st = s.get("status", "other")
            stat[st] = stat.get(st, 0) + 1
    except (OSError, ValueError):
        sdata = None
    if sdata is None:
        print("sources.json: 读取失败")
    else:
        print("新源发现总数: %d" % stat["candidate"])
        print("  candidate: %d | confirmed: %d | 其他: %d"
              % (stat["candidate"], stat["confirmed"], stat["other"]))

    latest = _latest_candidate()
    if latest is None:
        print("候选池: 未找到候选 JSON")
    else:
        print("最近候选 JSON: %s" % latest["path"])
        print("  生成时间: %s | 候选数: %s"
              % (latest["generated_at"], latest["count"]))
    return 0


def _add_collect(sp):
    p = sp.add_parser("collect", help="采集简报候选（调 collect_brief）")
    p.add_argument("--window", help="窗口日期（当前透传但不参与计算）")
    p.set_defaults(func=lambda a: _invoke(collect_brief.main, []))


def _add_discover(sp):
    p = sp.add_parser("discover", help="新源发现（调 discover_sources）")
    p.add_argument("--limit", type=int, help="最多探测的新域候选数")
    p.add_argument("--top", type=int, help="输出达标候选数(1-2)")
    p.set_defaults(func=lambda a: _invoke(discover_sources.main, _reconstruct_discover(a)))


def _add_validate(sp):
    p = sp.add_parser("validate", help="简报强校验（调 validate_brief）")
    p.add_argument("paths", nargs="*", help="简报.md 与 候选.json（缺省自动探测）")
    p.add_argument("--dedup", help="去重 JSON 路径")
    p.set_defaults(func=lambda a: _invoke(
        validate_brief.main, _reconstruct_validate(a)))


def _add_like(sp):
    p = sp.add_parser("like", help="点赞管理（调 add_like）")
    p.add_argument("--brief", help="简报文件名或日期")
    p.add_argument("--num", nargs="+", type=int, help="参考资料序号（可多个）")
    p.add_argument("--url", help="文章 URL")
    p.add_argument("--title", help="文章标题（可选）")
    p.add_argument("--direction", choices=likes_mod.DIRECTIONS, help="方向")
    p.add_argument("--list", action="store_true", help="列出全部点赞")
    p.add_argument("--stats", action="store_true", help="点赞方向统计")
    p.set_defaults(func=lambda a: _invoke(add_like.main, _reconstruct_like(a)))


def _add_serve(sp):
    p = sp.add_parser("serve", help="点赞服务控制（调 like_ctl）")
    p.add_argument("action", choices=["start", "stop", "status"])
    p.add_argument("--idle-timeout", type=int, help="空闲自动退出秒数")
    p.set_defaults(func=lambda a: _invoke(like_ctl.main, _reconstruct_serve(a)))


def _add_links(sp):
    p = sp.add_parser("links", help="简报追加点赞区（调 like_links）")
    p.add_argument("brief", nargs="?", help="简报文件路径或日期")
    p.add_argument("--check", action="store_true", help="只检查是否已有点赞区")
    p.set_defaults(func=lambda a: _invoke(like_links.main, _reconstruct_links(a)))


def _add_status(sp):
    p = sp.add_parser("status", help="查看系统状态概览")
    p.set_defaults(func=cmd_status)


def main():
    ap = argparse.ArgumentParser(
        prog="dailybrief",
        description="DailyBrief 统一命令行入口；各子命令复用 scripts/ 现有脚本 main()。",
    )
    sp = ap.add_subparsers(dest="command", title="子命令")
    _add_collect(sp)
    _add_discover(sp)
    _add_validate(sp)
    _add_like(sp)
    _add_serve(sp)
    _add_links(sp)
    _add_status(sp)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
