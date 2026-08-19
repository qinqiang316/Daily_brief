#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DailyBrief 采集编排入口。具体职责位于 modules/window|retrieve|filter|rank。"""
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)
from modules.window import BRIEF_DIR, TZ, compute_window
from modules import filter as filter_mod
from modules import rank, retrieve

CAND_DIR = os.path.join(BRIEF_DIR, "_candidates")
LIKES_FILE = os.path.join(BRIEF_DIR, "data", "likes.json")
MAX_CANDIDATES, MAX_HN, MIN_WORDS = rank.MAX_CANDIDATES, rank.MAX_HN, filter_mod.MIN_WORDS
QUERIES_CN = [{"query": 'site:qbitai.com 大模型 人工智能 8月', "max_results": 8, "direction": "AI"}, {"query": 'site:36kr.com 深度 商业 科技 8月', "max_results": 8, "direction": "商业"}, {"query": 'site:jiqizhixin.com 大模型 深度学习 8月', "max_results": 8, "direction": "AI"}, {"query": 'site:huxiu.com 深度 商业 科技 8月', "max_results": 8, "direction": "商业"}, {"query": 'site:thepaper.cn 深度报道 科技 商业', "max_results": 8, "direction": "生活"}, {"query": 'site:zhihu.com 深度 分析 商业 经济', "max_results": 8, "direction": "科技"}, {"query": 'site:kk.org thetechnium', "max_results": 6, "direction": "科技"}, {"query": 'site:kk.org weekly links', "max_results": 6, "direction": "科技"}, {"query": 'site:tmtpost.com 深度 科技 商业 AI', "max_results": 8, "direction": "商业"}, {"query": 'site:leiphone.com 人工智能 大模型 深度', "max_results": 8, "direction": "AI"}, {"query": 'site:huxiu.com 互联网 AI 创业 深度', "max_results": 8, "direction": "科技"}, {"query": 'site:woshipm.com 产品 商业 深度 分析', "max_results": 6, "direction": "商业"}]
QUERIES_INTL = [{"query": 'site:theguardian.com technology 2026', "max_results": 8, "direction": "科技"}, {"query": 'site:techcrunch.com artificial intelligence 2026', "max_results": 8, "direction": "AI"}, {"query": 'site:colossus.com invest like the best', "max_results": 6, "direction": "商业"}, {"query": 'site:colossus.com business breakdowns founders', "max_results": 6, "direction": "商业"}, {"query": 'site:reddit.com technology AI deep dive', "max_results": 8, "direction": "科技"}, {"query": 'site:jiandanxinli.com 心理健康 情绪管理 职场', "max_results": 6, "direction": "健康"}, {"query": '高速铁路 磁浮 城市轨道 最新进展 2026年8月', "max_results": 8, "direction": "轨道交通"}, {"query": '新幹線 リニアモーターカー 鉄道 技術 最新 2026', "max_results": 8, "direction": "轨道交通"}, {"query": 'site:thepaper.cn 城市轨道 磁浮 最新', "max_results": 6, "direction": "轨道交通"}]

def main():
    try: return _main()
    except Exception as e:
        import traceback; traceback.print_exc(); print("[COLLECT_FAILED] 采集脚本异常：%s。本次简报未生成。" % e); return 1

def _main():
    now = datetime.now(TZ); window_start, today = compute_window(now)
    dedup = filter_mod.auto_update_dedup(); extra, pref_dir, explore_dir = rank.build_extra_queries(now)
    candidates = retrieve.fetch_hn(now)
    for c in candidates:
        c["direction"] = rank.likes_mod.infer_direction(c.get("url", ""), c.get("title", "")); c["is_preferred"] = False; c["is_explore"] = False
    src_stats = {"hn": len(candidates)}
    for name, queries in (("cn", QUERIES_CN), ("intl", QUERIES_INTL)):
        rank.search_batch_with_tags(name, queries, candidates, src_stats, pref_dir)
    if extra: rank.search_batch_with_tags("pref", extra, candidates, src_stats, pref_dir)
    hn_items = [c for c in candidates if c.get("hn_points") and c.get("url") and "news.ycombinator.com" not in c["url"]]; hn_items.sort(key=lambda x: x.get("hn_points") or 0, reverse=True)
    for c in hn_items[:rank.HN_FETCH_LIMIT]:
        text = retrieve.fetch_article_text(c["url"], timeout=15)
        if text: c["content"] = "HN 热议 %d 分。%s\n\n%s" % (c.get("hn_points", 0), c.get("title", ""), text)
    kept = rank.rank_candidates(rank.filter_candidates(candidates, dedup, window_start, today, pref_dir), explore_dir)
    os.makedirs(CAND_DIR, exist_ok=True); out_file = os.path.join(CAND_DIR, "Daily-Brief-%s-candidates.json" % today.strftime("%Y-%m-%d"))
    payload = {"generated_at": now.isoformat(), "window": {"start": str(window_start), "end": str(today)}, "preference": {"pref_dir": pref_dir, "explore_dir": explore_dir, "likes_count": len(rank.likes_mod.load_likes(LIKES_FILE))}, "candidates": kept}
    with open(out_file, "w", encoding="utf-8") as f: json.dump(payload, f, ensure_ascii=False, indent=2)
    if not kept: print("[COLLECT_FAILED] 采集脚本失败：候选 0 篇（搜索源全挂或全部被硬过滤）。本次简报未生成。"); return 1
    print("时间窗口：%s ~ %s（触发日 %s）" % (window_start, today, today)); pref_txt = "偏好方向: %s | " % pref_dir if pref_dir else ""
    print("候选 %d 篇（已去重、已过滤 <500 字；窗口外 ≤7 天已打标放行，>7 天剔除；%s探索方向: %s）" % (len(kept), pref_txt, explore_dir))
    for i, it in enumerate(kept, 1):
        flag = "日期未验证" if not it["date_verified"] else (it["publish_date"] or "日期未知")
        if it["window_outside_days"]: flag += " [窗口外%d天]" % it["window_outside_days"]
        sus = " [疑似AI水文:%s]" % ",".join(it["watermark_reasons"]) if it["watermark_suspect"] else ""; tags = ["探索"] if it["is_explore"] else (["偏好"] if it["is_preferred"] else [])
        if it["direction"]: tags.append(it["direction"])
        print("[%d]%s %s | %s | %s | %d字%s" % (i, (" [%s]" % ",".join(tags)) if tags else "", it["title"], it["domain"], flag, it["word_count"], sus)); print("    %s" % it["url"])
    print("候选详情 JSON：%s" % out_file)

if __name__ == "__main__": sys.exit(main())
