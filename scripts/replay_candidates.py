#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史候选池离线回放入口（2026-10-08 流程加固新增）。

用途：对既有候选 JSON 按加固后的准入标准做**只读、不联网**回放体检，
回答"这池候选按新标准还能不能交付"。

用法:
  python3 scripts/replay_candidates.py 2026-10-07
  python3 scripts/replay_candidates.py _candidates/Daily-Brief-2026-10-07-candidates.json

回放检查（全部离线，不重新抓取）:
  1. URL 合法性（畸形/反斜杠 URL 拒收）
  2. 发布日期证据：date_verified + 精确到日 + 不未来 + 落在候选窗口内
  3. 正文质量证据：word_count ≥ MIN_WORDS（旧池只有标题/HN 一句话 stub 即不合格）

判定:
  - 合格候选 ≥1 → 打印可交付清单，退出码 0
  - 合格候选 0 → [REPLAY_REFUSED] 明确拒绝交付，退出码 1（不编造、不补写正文）

保证：不联网、不写任何文件、不修改历史候选/简报/点赞数据。
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from modules import filter as filter_mod

CAND_DIR = os.path.join(ROOT, "_candidates")
MIN_WORDS = filter_mod.MIN_WORDS


def find_candidate_file(target):
    if os.path.isfile(target):
        return target
    path = os.path.join(CAND_DIR, "Daily-Brief-%s-candidates.json" % target)
    return path if os.path.isfile(path) else None


def check_item(c, win_start, win_end, today_s):
    """返回 (ok, reasons)。全部基于池内已有证据，离线判定。"""
    reasons = []
    url = filter_mod.norm_url(c.get("url", ""))
    if not url:
        reasons.append("非法 URL")
        return False, reasons
    pub = c.get("publish_date") or ""
    if not c.get("date_verified") or not pub:
        reasons.append("无发布日期证据")
    else:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", pub):
            reasons.append("日期非日精度: %s" % pub)
        else:
            if pub > today_s:
                reasons.append("未来日期 %s" % pub)
            if win_start and pub < win_start:
                reasons.append("窗口外（%s < %s）" % (pub, win_start))
            if win_end and pub > win_end:
                reasons.append("晚于窗口终点（%s > %s）" % (pub, win_end))
    if c.get("window_outside_days"):
        reasons.append("标记窗口外 %d 天" % c["window_outside_days"])
    wc = c.get("word_count") or 0
    if wc < MIN_WORDS:
        reasons.append("正文证据不足（%d 字 < %d）" % (wc, MIN_WORDS))
    return not reasons, reasons


def replay(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    window = data.get("window", {}) or {}
    win_start = str(window.get("start") or "")[:10]
    win_end = str(window.get("end") or "")[:10]
    today_s = win_end or "9999-99-99"  # 以窗口终点为"当时今天"判定未来日期

    print("回放文件: %s" % path)
    print("run_id: %s | generated_at: %s | 窗口: %s ~ %s"
          % (data.get("run_id", "无（旧格式）"), data.get("generated_at", "?"),
             win_start or "?", win_end or "?"))

    passed, failed = [], []
    for c in data.get("candidates", []):
        ok, reasons = check_item(c, win_start, win_end, today_s)
        (passed if ok else failed).append((c, reasons))

    for c, _ in passed:
        print("  ✓ %s | %s | %d字" % (c.get("title", "")[:60], c.get("publish_date"), c.get("word_count") or 0))
    for c, reasons in failed:
        print("  ✗ %s | %s" % (c.get("title", "")[:60], "；".join(reasons)))

    unv_lo = [lo for lo in data.get("source_leftovers", []) if not lo.get("date_verified")]
    if unv_lo:
        print("速览 leftover 含 %d 条日期未验证（新标准禁止交付）" % len(unv_lo))

    print("回放结论: %d/%d 篇候选符合加固后准入标准" % (len(passed), len(passed) + len(failed)))
    if not passed:
        print("[REPLAY_REFUSED] 旧池正文/日期证据不足，明确拒绝交付；"
              "不编造正文、不补写日期。请用新流程重新采集。")
        return 1
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = find_candidate_file(sys.argv[1])
    if not path:
        print("找不到候选文件: %s" % sys.argv[1])
        return 2
    return replay(path)


if __name__ == "__main__":
    sys.exit(main())
