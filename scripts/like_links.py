#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给简报重建点赞区：按当前「参考资料」生成点赞链接，置于简报文末（2026-08-13 新增，
2026-10-08 加固为幂等重建）。

用法:
  python3 like_links.py [简报.md]          # 缺省探测最新简报
  python3 like_links.py --check            # 只检查点赞区是否与参考资料一致（供流程判断）

加固说明：旧逻辑"点赞区存在即跳过"，会导致参考资料更新后点赞区残留旧链接。
现改为幂等重建：无论是否已有点赞区，都按当前参考资料重新生成并整体替换，
重复执行结果一致（同输入同输出）。写盘为原子替换，不会出现半截文件。

追加内容：
  ## 👍 点赞
  > 点链接即可为这篇文章点赞（仅本机有效）；已在手机端可用文字指令「点赞 N」。
  - [N] 标题 — [👍 点赞](http://127.0.0.1:8900/like?url=...&title=...)

说明：在 validate_brief.py PASS 之后运行（点赞链接含本机地址，validate 已忽略）。
"""
import argparse
import os
import re
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from add_like import parse_brief_refs, find_brief

BRIEF_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUTPUT_DIR = os.path.join(BRIEF_DIR, "output")
LIKE_BASE = "http://127.0.0.1:8900/like"
SECTION = "## 👍 点赞"


def make_link(url, title):
    q = urllib.parse.urlencode({"url": url, "title": title[:80]})
    return "%s?%s" % (LIKE_BASE, q)


def build_section(refs):
    """按参考资料生成点赞区文本。"""
    lines = [SECTION, ""]
    lines.append("> 点链接即可为这篇文章点赞（本机浏览器有效；手机端可发文字指令「点赞 N」）。")
    lines.append("")
    for n in sorted(refs):
        title, url = refs[n]
        lines.append("- [%d] %s — [👍 点赞](%s)" % (n, title, make_link(url, title)))
    lines.append("")
    return "\n".join(lines)


def strip_section(text):
    """移除文末已有的点赞区（点赞区固定在文末；其后若有其它内容也一并保守保留）。"""
    if SECTION not in text:
        return text.rstrip()
    head = text.split(SECTION, 1)[0]
    return head.rstrip()


def _atomic_write(path, content):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)),
                               prefix=".tmp_like_", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add_like_section(brief_path, dry=False):
    """幂等重建点赞区。返回 (changed, msg)：内容有变化（或将要变化）为 True。"""
    with open(brief_path, encoding="utf-8") as f:
        text = f.read()
    refs = parse_brief_refs(brief_path)
    if not refs:
        return False, "简报中未解析到参考资料，无法生成点赞区"
    head = strip_section(text)
    new_text = head + "\n\n" + build_section(refs)
    if new_text.strip() == text.strip():
        return False, "点赞区已是最新（幂等，无需重建）"
    if dry:
        return True, "点赞区需重建（%d 条点赞链接）" % len(refs)
    _atomic_write(brief_path, new_text)
    had = SECTION in text
    return True, ("已重建 %d 条点赞链接（替换旧点赞区）" if had else "已追加 %d 条点赞链接") % len(refs)


def main():
    ap = argparse.ArgumentParser(description="简报点赞区幂等重建")
    ap.add_argument("brief", nargs="?", help="简报文件路径或日期")
    ap.add_argument("--check", action="store_true", help="只检查点赞区是否与参考资料一致")
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
    # 已是最新/已重建都算成功
    return 0 if (ok or "已是最新" in msg) else 1


if __name__ == "__main__":
    sys.exit(main())
