#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拒收候选 CLI（审核者小g 的拒收同步入口）。

协议（简单、明确、可审计）：
  - 按规范化 URL 在 candidates / source_leftovers 中找到命中条目并**移除**；
  - 原条目整体保留在顶层 rejected_candidates[].entry，并记录审计字段：
    url / is_explore / reason / reviewed_by / reviewed_at；
  - 幂等：同一 URL 已拒收则跳过不重复记录；URL 不在本次候选产物中 → 退出 1；
  - 原子落盘（mkstemp + os.replace），不写其他任何文件。
  - 严禁靠取消 is_explore 绕过探索收录要求：探索候选只能走本拒收通道移除。
    有探索拒收但无合格探索时，校验闸门要求候选 JSON 含 exploration_replenishment
    （status=exhausted、attempts=1或2、reason 非空）且简报注明「探索内容缺货」。

用法：
  python3 scripts/reject_candidate.py --date 2026-10-09 \
      --url https://familydoctor.cn/news --reason "聚合页，非独立文章"
  python3 scripts/reject_candidate.py --candidates _candidates/Daily-Brief-2026-10-09-candidates.json \
      --url https://... --reason "..." --reviewed-by 小g
  python3 scripts/reject_candidate.py --date 2026-10-09 --list
"""
import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modules.window import TZ  # noqa: E402
from modules import filter as filter_mod, run_store  # noqa: E402
import collect_brief  # noqa: E402  复用 CAND_DIR / atomic_write_json（模块顶层无副作用）


def _cand_path(date_s):
    return os.path.join(collect_brief.CAND_DIR, "Daily-Brief-%s-candidates.json" % date_s)


def list_rejected(cand_path):
    if not os.path.isfile(cand_path):
        print("[REJECT] 找不到候选 JSON: %s" % cand_path)
        return 1
    with open(cand_path, encoding="utf-8") as f:
        payload = json.load(f)
    rejected = payload.get("rejected_candidates") or []
    if not rejected:
        print("（无拒收记录）%s" % cand_path)
        return 0
    print("拒收 %d 项（%s）：" % (len(rejected), cand_path))
    for r in rejected:
        print("  - %s | explore=%s | %s | by %s @ %s"
              % (r.get("url"), r.get("is_explore"), r.get("reason"),
                 r.get("reviewed_by"), r.get("reviewed_at")))
    return 0


def reject(cand_path, url, reason, reviewed_by):
    """把命中条目从 candidates/source_leftovers 移入顶层 rejected_candidates。"""
    directory = run_store.managed_run(cand_path)
    if directory and (directory / "published.json").exists():
        print("[REJECT_FAILED] 批次已发布，禁止修改审核证据")
        return 1
    nu = filter_mod.norm_url(url)
    if not nu:
        print("[REJECT_FAILED] URL 非法: %s" % str(url)[:80])
        return 1
    if not os.path.isfile(cand_path):
        print("[REJECT_FAILED] 找不到候选 JSON: %s" % cand_path)
        return 1
    with open(cand_path, encoding="utf-8") as f:
        payload = json.load(f)
    rejected = payload.setdefault("rejected_candidates", [])
    if any(filter_mod.norm_url(r.get("url", "")) == nu for r in rejected if isinstance(r, dict)):
        print("[REJECTED] 已在拒收列表（幂等跳过）: %s" % nu)
        return 0
    hit, hit_list, hit_idx = None, None, -1
    for name in ("candidates", "source_leftovers"):
        for i, c in enumerate(payload.get(name) or []):
            if filter_mod.norm_url(c.get("url", "")) == nu:
                hit, hit_list, hit_idx = c, name, i
                break
        if hit is not None:
            break
    if hit is None:
        print("[REJECT_FAILED] URL 不在本次 candidates/source_leftovers 中: %s" % nu)
        return 1
    del payload[hit_list][hit_idx]
    rejected.append({
        "url": nu,
        "is_explore": bool(hit.get("is_explore")),
        "reason": reason.strip(),
        "reviewed_by": reviewed_by.strip(),
        "reviewed_at": datetime.now(TZ).isoformat(),
        "entry": hit,  # 原条目整体保留，可审计可恢复
    })
    collect_brief.atomic_write_json(cand_path, payload)
    print("[REJECTED] 已从 %s 移除并记入 rejected_candidates: %s（explore=%s，by %s）"
          % (hit_list, nu, bool(hit.get("is_explore")), reviewed_by))
    if hit.get("is_explore"):
        print("注意：这是探索候选。若移除后无合格探索，需补抓；补抓穷尽须在候选 JSON 写入"
              " exploration_replenishment（status=exhausted、attempts=1或2、reason）"
              " 并在简报注明「探索内容缺货」，否则校验 FAIL。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="拒收候选 CLI：把拒收项从候选池/速览移入 rejected_candidates")
    ap.add_argument("--date", help="候选日期 YYYY-MM-DD（定位 _candidates/Daily-Brief-<date>-candidates.json）")
    ap.add_argument("--candidates", help="候选 JSON 路径（与 --date 二选一）")
    ap.add_argument("--url", help="要拒收的 URL")
    ap.add_argument("--reason", help="拒收原因（必填，非空）")
    ap.add_argument("--reviewed-by", default="小g", help="审核者标识（默认 小g）")
    ap.add_argument("--list", action="store_true", help="只列出当前拒收记录")
    args = ap.parse_args(argv)

    cand_path = args.candidates or (_cand_path(args.date) if args.date else None)
    if not cand_path:
        print("[REJECT_FAILED] 须用 --date 或 --candidates 指定候选 JSON")
        return 1
    if args.list:
        return list_rejected(cand_path)
    if not args.url or not (args.reason or "").strip():
        print("[REJECT_FAILED] 拒收必须提供 --url 与非空 --reason（可审计协议）")
        return 1
    return reject(cand_path, args.url, args.reason, args.reviewed_by)


if __name__ == "__main__":
    sys.exit(main())
