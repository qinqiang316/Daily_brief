#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给简报追加点赞区：每个参考资料生成一个点赞链接，追加到简报文末（2026-08-13 新增）。

用法:
  python3 like_links.py [简报.md]          # 缺省探测最新简报
  python3 like_links.py --check            # 只检查是否已有点赞区（供流程判断）

追加内容（若文末已存在「## 👍 点赞」区则跳过，不重复追加）：
  ## 👍 点赞
  > 点链接即可为这篇文章点赞（仅本机有效）；已在手机端可用文字指令「点赞 N」。
  - [N] 标题 — [👍 点赞](http://127.0.0.1:8900/like?url=...&title=...)

说明：在 validate_brief.py PASS 之后运行（点赞链接含本机地址，validate 已忽略）。
"""
import argparse
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from add_like import parse_brief_refs, find_brief

BRIEF_DIR = "/Users/qqiang/AI project/05-日常工具/DailyBrief"
OUTPUT_DIR = os.path.join(BRIEF_DIR, "output")
LIKE_BASE = "http://127.0.0.1:8900/like"
SECTION = "## 👍 点赞"


def make_link(url, title):
    q = urllib.parse.urlencode({"url": url, "title": title[:80]})
    return "%s?%s" % (LIKE_BASE, q)


def add_like_section(brief_path, dry=False):
    with open(brief_path, encoding="utf-8") as f:
        text = f.read()
    if SECTION in text:
        return False, "已有点赞区，跳过"
    refs = parse_brief_refs(brief_path)
    if not refs:
        return False, "简报中未解析到参考资料，无法生成点赞区"
    lines = ["", SECTION, ""]
    lines.append("> 点链接即可为这篇文章点赞（本机浏览器有效；手机端可发文字指令「点赞 N」）。")
    lines.append("")
    for n in sorted(refs):
        title, url = refs[n]
        link = make_link(url, title)
        lines.append("- [%d] %s — [👍 点赞](%s)" % (n, title, link))
    lines.append("")
    if dry:
        return True, "将追加 %d 条点赞链接" % len(refs)
    with open(brief_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return True, "已追加 %d 条点赞链接" % len(refs)


def main():
    ap = argparse.ArgumentParser(description="简报追加点赞区")
    ap.add_argument("brief", nargs="?", help="简报文件路径或日期")
    ap.add_argument("--check", action="store_true", help="只检查是否已有点赞区")
    args = ap.parse_args()
    brief_path = args.brief
    if brief_path and not os.path.isfile(brief_path):
        brief_path = find_brief(brief_path)
    if not brief_path:
        brief_path = find_brief(None)
    if not brief_path:
        print("找不到简报")
        return 2
    ok, msg = add_like_section(brief_path, dry=args.check)
    print(msg)
    # 已有点赞区=已完成，不算错误
    if "已有点赞区" in msg:
        return 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
