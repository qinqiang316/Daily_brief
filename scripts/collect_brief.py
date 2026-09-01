#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DailyBrief 采集编排入口。具体职责位于 modules/window|retrieve|filter|rank。"""
import json
import os
import random
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from modules.window import BRIEF_DIR, TZ, compute_window, log
from modules import filter as filter_mod
from modules import rank, retrieve

# 兼容导出：旧脚本(validate_brief 等)仍引用 collect_brief.DEDUP_FILE / norm_url
DEDUP_FILE = filter_mod.DEDUP_FILE
norm_url = filter_mod.norm_url

CAND_DIR = os.path.join(BRIEF_DIR, "_candidates")
LIKES_FILE = os.path.join(BRIEF_DIR, "data", "likes.json")
MAX_CANDIDATES, MAX_HN, MIN_WORDS = rank.MAX_CANDIDATES, rank.MAX_HN, filter_mod.MIN_WORDS

# 查询模板（支持 {month}, {year}, {year_month} 动态格式化）
QUERIES_CN_TEMPLATES = [
    {"query": "site:qbitai.com 大模型 人工智能 {month}", "max_results": 8, "direction": "AI"},
    {"query": "site:36kr.com 深度 商业 科技 {month}", "max_results": 8, "direction": "商业"},
    {"query": "site:jiqizhixin.com 大模型 深度学习 {month}", "max_results": 8, "direction": "AI"},
    {"query": "site:huxiu.com 深度 商业 科技 {month}", "max_results": 8, "direction": "商业"},
    {"query": "site:thepaper.cn 深度报道 科技 商业", "max_results": 8, "direction": "生活"},
    {"query": "site:zhihu.com 深度 分析 商业 经济", "max_results": 8, "direction": "科技"},
    {"query": "site:kk.org thetechnium", "max_results": 6, "direction": "科技"},
    {"query": "site:kk.org weekly links", "max_results": 6, "direction": "科技"},
    {"query": "site:tmtpost.com 深度 科技 商业 AI", "max_results": 8, "direction": "商业"},
    {"query": "site:leiphone.com 人工智能 大模型 深度", "max_results": 8, "direction": "AI"},
    {"query": "site:huxiu.com 互联网 AI 创业 深度", "max_results": 8, "direction": "科技"},
    {"query": "site:woshipm.com 产品 商业 深度 分析", "max_results": 6, "direction": "商业"},
]

QUERIES_INTL_TEMPLATES = [
    {"query": "site:theguardian.com technology {year}", "max_results": 8, "direction": "科技"},
    {"query": "site:techcrunch.com artificial intelligence {year}", "max_results": 8, "direction": "AI"},
    {"query": "site:colossus.com invest like the best", "max_results": 6, "direction": "商业"},
    {"query": "site:colossus.com business breakdowns founders", "max_results": 6, "direction": "商业"},
    {"query": "site:reddit.com technology AI deep dive", "max_results": 8, "direction": "科技"},
    {"query": "site:jiandanxinli.com 心理健康 情绪管理 职场", "max_results": 6, "direction": "健康"},
    {"query": "高速铁路 磁浮 城市轨道 最新进展 {year_month}", "max_results": 8, "direction": "轨道交通"},
    {"query": "新幹線 リニアモーターカー 鉄道 技術 最新 {year}", "max_results": 8, "direction": "轨道交通"},
    {"query": "site:thepaper.cn 城市轨道 磁浮 最新", "max_results": 6, "direction": "轨道交通"},
]


def build_default_queries(now):
    """根据当前时间动态生成国内外搜索查询列表"""
    month = "%d月" % now.month
    year = str(now.year)
    year_month = "%d年%d月" % (now.year, now.month)
    fmt = {"month": month, "year": year, "year_month": year_month}
    cn = [{**q, "query": q["query"].format(**fmt)} for q in QUERIES_CN_TEMPLATES]
    intl = [{**q, "query": q["query"].format(**fmt)} for q in QUERIES_INTL_TEMPLATES]
    return cn, intl


# 兼容默认列表
QUERIES_CN, QUERIES_INTL = build_default_queries(datetime.now(TZ))

# Telegram 频道列表（公开频道，抓 t.me/s/{name}）
TELEGRAM_CHANNELS = [
    "xhqcankao", "wxbyg", "solidot", "zaihuapd", "DNSPODT",
    "idwlch", "CE_Observe", "bigwalnut", "kejiqu", "zhihu_bazaar",
]

# 未推荐源速览的额外黑名单（社交/无信息导航页）
LEFTOVER_EXCLUDE_DOMAINS = {
    "linkedin.com", "cn.linkedin.com", "www.linkedin.com", "x.com", "twitter.com",
    "facebook.com", "www.facebook.com", "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "b23.tv", "weibo.com", "www.weibo.com",
    "youtube.com", "www.youtube.com", "m.youtube.com",
}
# 导航/栏目录页判定（任意段命中即剔；article/column/trends/story/pd/view 等承载段不在表内，天然放行）
LEFTOVER_NAV_SEG = {
    "articles", "category", "categories", "channel", "channels", "video", "videos",
    "people", "company", "companies", "home", "archives", "archive", "posts", "about",
    "about-us", "uk", "us", "news", "technology", "topics", "topic", "section",
    "sections", "list", "lists", "tag", "tags", "noticias", "mag", "magazine", "content", "p", "page",
}
# 单段 path 仍然可能是文章页的站（其余单段一律按栏目/主页剔）
LEFTOVER_SINGLE_SEG_OK = {"telegra.ph", "solidot.org", "linux.solidot.org"}

# 特殊源主页/列表页取最新配置（未进候选时优先从主页/列表页提取最新文章）
HOMEPAGE_LATEST_SOURCES = {
    "jiqizhixin.com": {
        "list_url": "https://www.jiqizhixin.com/industry",
        "url_pattern": r"https://www\.jiqizhixin\.com/articles/\d{4}-\d{2}-\d{2}-\d+",
    },
}


def pick_homepage_latest_item(cfg, dedup, kept_urls, window_start, today):
    """从特殊源主页/列表页通过 extract 通道提取最新文章。"""
    list_url = cfg.get("list_url")
    url_pattern = cfg.get("url_pattern")
    if not list_url:
        return None
    try:
        raw_items = retrieve.anysearch_extract(list_url)
    except Exception as e:
        log("提取特殊源主页失败(%s): %s" % (list_url, e))
        return None

    if not raw_items:
        return None

    window_start_s = window_start.strftime("%Y-%m-%d")
    today_s = today.strftime("%Y-%m-%d")
    start_date = window_start.date() if hasattr(window_start, "date") else window_start

    pool = []
    for it in raw_items:
        raw_url = it.get("url", "")
        if url_pattern and not re.search(url_pattern, raw_url):
            continue
        url = filter_mod.norm_url(raw_url)
        if not url or url in dedup or url in kept_urls:
            continue
        domain = url.split("/")[2].lower() if "://" in url else ""
        if domain in filter_mod.EXCLUDE_DOMAINS or domain in LEFTOVER_EXCLUDE_DOMAINS:
            continue
        title = (it.get("title") or "").strip()
        if not title or title.startswith("http"):
            continue
        pub, verified, month_only = filter_mod.extract_publish_date({"url": url, "title": title})
        if pub and pub > today_s:
            continue
        if pub:
            if month_only:
                if (pub[:4], pub[5:7]) < (window_start_s[:4], window_start_s[5:7]):
                    continue
            elif pub < window_start_s:
                try:
                    outside = (start_date - datetime.strptime(pub, "%Y-%m-%d").date()).days
                except Exception:
                    outside = 999
                if outside > 7:
                    continue
        m = re.search(r"(\d{4}-\d{2}-\d{2})(?:-(\d+))?", url)
        seq = int(m.group(2)) if m and m.group(2) else 0
        pool.append({
            "title": title[:200],
            "url": url,
            "domain": domain,
            "publish_date": pub or "",
            "date_verified": verified or bool(pub),
            "_sort_key": (pub or "0000-00-00", seq),
        })

    if not pool:
        return None

    pool.sort(key=lambda x: x["_sort_key"], reverse=True)
    best = pool[0]
    best.pop("_sort_key", None)
    return best


def pick_leftover_item(items, dedup, kept_urls, window_start, today, source_key=None, source_info=None):
    """从某来源的全部原始条目中挑 1 条可展示的最新内容。
    把关：去重、候选内不重复、黑名单/RSS/裸域名/t.me 内部链接剔除、无标题/URL式标题剔除、
    栏目导航页剔除、未来日期剔除、超窗 >7 天剔除。
    若命中了 HOMEPAGE_LATEST_SOURCES 特殊源配置，优先走主页 extract 提取最新文章；
    失败或无匹配则回退到现有随机挑选逻辑。
    日期优先用条目自带（TG/HN 可信），否则 extract_publish_date 回退。
    优先已验证日期，其次按日期取最近 3 条随机挑 1。"""
    # 优先检查特殊源主页/列表页配置
    matched_cfg = None
    if source_key:
        for dom, cfg in HOMEPAGE_LATEST_SOURCES.items():
            if dom in source_key:
                matched_cfg = cfg
                break
    if not matched_cfg and source_info:
        for dom, cfg in HOMEPAGE_LATEST_SOURCES.items():
            if dom in source_info.get("label", ""):
                matched_cfg = cfg
                break
    if not matched_cfg and items:
        for dom, cfg in HOMEPAGE_LATEST_SOURCES.items():
            if any(dom in (it.get("url") or "") for it in items):
                matched_cfg = cfg
                break

    if matched_cfg:
        picked = pick_homepage_latest_item(matched_cfg, dedup, kept_urls, window_start, today)
        if picked:
            return picked

    # 回退到现有随机挑选逻辑
    window_start_s = window_start.strftime("%Y-%m-%d")
    today_s = today.strftime("%Y-%m-%d")
    start_date = window_start.date() if hasattr(window_start, "date") else window_start
    pool = []
    for it in items:
        url = filter_mod.norm_url(it.get("url", ""))
        if not url or url in dedup or url in kept_urls:
            continue
        domain = url.split("/")[2].lower() if "://" in url else ""
        if domain in filter_mod.EXCLUDE_DOMAINS or domain in LEFTOVER_EXCLUDE_DOMAINS or "/rss" in url or url.rstrip("/").endswith(".rss"):
            continue
        path = url.split("://", 1)[-1]
        path = path.split("/", 1)[1] if "/" in path else ""
        if not path:
            continue
        if "t.me/" in url and "/s/" not in url:
            continue
        title = (it.get("title") or "").strip()
        if not title or title.startswith("http"):
            continue
        # 栏目/导航页剔除：主页/单段非文章站/导航段（除非整体是文章形态）/翻页目录页
        segs = [s for s in path.rstrip("/").split("/") if s]
        query = url.split("?", 1)[1] if "?" in url else ""
        if not segs or (len(segs) == 1 and domain not in LEFTOVER_SINGLE_SEG_OK):
            continue
        # 文章形态：末段是数字/长哈希/以 .html 结尾 → 即使含导航承载段也保留
        last_seg = segs[-1].lower()
        article_like = bool(re.search(r"[0-9a-f]{8,}", last_seg)) or last_seg.isdigit() or last_seg.endswith(".html")
        if article_like and not any(s.lower() in LEFTOVER_NAV_SEG for s in segs[:-1]):
            pass
        elif any(s.lower() in LEFTOVER_NAV_SEG for s in segs):
            continue
        if any(s.lower() in ("page", "p") and i + 1 < len(segs) and segs[i + 1].isdigit() for i, s in enumerate(segs)):
            continue
        if query.startswith("page=") or "&page=" in query:
            continue
        # 日期：优先条目自带（TG/HN 提供），否则 URL/标题/内容回退
        if it.get("date_verified") and it.get("publish_date"):
            pub, verified, month_only = it["publish_date"], True, False
        else:
            pub, verified, month_only = filter_mod.extract_publish_date(it)
        if pub and pub > today_s:
            continue
        if pub:
            if month_only:
                if (pub[:4], pub[5:7]) < (window_start_s[:4], window_start_s[5:7]):
                    continue
            elif pub < window_start_s:
                try:
                    outside = (start_date - datetime.strptime(pub, "%Y-%m-%d").date()).days
                except Exception:
                    outside = 999
                if outside > 7:
                    continue
        pool.append({
            "title": title[:200],
            "url": url,
            "domain": domain,
            "publish_date": pub or "",
            "date_verified": verified or bool(pub),
        })
    if not pool:
        return None
    pool.sort(key=lambda x: (x["date_verified"], x["publish_date"] or "0000-00-00"), reverse=True)
    return random.choice(pool[:3])


def build_source_leftovers(src_map, kept, dedup, window_start, today):
    """未推荐源 = 抓到了内容但没有任何一条进候选池的源。每源随机挑 1 条最新内容。"""
    kept_urls = {c["url"] for c in kept}
    kept_keys = {c.get("source_key") for c in kept if c.get("source_key")}
    leftovers = []
    for key, info in src_map.items():
        if key in kept_keys or not info["items"]:
            continue
        picked = pick_leftover_item(info["items"], dedup, kept_urls, window_start, today, source_key=key, source_info=info)
        if not picked:
            continue
        leftovers.append({
            "source": info["label"],
            "source_key": key,
            "direction": info["direction"],
            **picked,
        })
    return leftovers


def main():
    try:
        return _main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("[COLLECT_FAILED] 采集脚本异常：%s。本次简报未生成。" % e)
        return 1


def _main():
    now = datetime.now(TZ)
    window_start, today = compute_window(now)
    dedup = filter_mod.auto_update_dedup()
    extra, pref_dir, explore_dir = rank.build_extra_queries(now)
    src_map = {}
    candidates = retrieve.fetch_hn(now)
    for c in candidates:
        c["direction"] = rank.likes_mod.infer_direction(c.get("url", ""), c.get("title", ""))
        c["is_preferred"] = False
        c["is_explore"] = False
        c["source_key"] = "hn"
        c["source_label"] = "Hacker News"
        src_map.setdefault("hn", {"label": "Hacker News", "direction": "科技", "items": []})["items"].append(c)
    src_stats = {"hn": len(candidates)}

    queries_cn, queries_intl = build_default_queries(now)
    for name, queries in (("cn", queries_cn), ("intl", queries_intl)):
        rank.search_batch_with_tags(name, queries, candidates, src_stats, pref_dir, src_map)
    if extra:
        rank.search_batch_with_tags("pref", extra, candidates, src_stats, pref_dir, src_map)

    # Telegram 频道采集
    tg_items = retrieve.fetch_telegram_channels(TELEGRAM_CHANNELS)
    for c in tg_items:
        c["direction"] = rank.likes_mod.infer_direction(c.get("url", ""), c.get("title", ""))
        ch = c.get("telegram_channel", "?")
        c["source_key"] = "tg:" + ch
        c["source_label"] = "TG@" + ch
        src_map.setdefault(c["source_key"], {"label": c["source_label"], "direction": c["direction"], "items": []})["items"].append(c)
    candidates.extend(tg_items)
    src_stats["telegram"] = len(tg_items)

    hn_items = [c for c in candidates if c.get("hn_points") and c.get("url") and "news.ycombinator.com" not in c["url"]]
    hn_items.sort(key=lambda x: x.get("hn_points") or 0, reverse=True)
    for c in hn_items[:rank.HN_FETCH_LIMIT]:
        text = retrieve.fetch_article_text(c["url"], timeout=15)
        if text:
            c["content"] = "HN 热议 %d 分。%s\n\n%s" % (c.get("hn_points", 0), c.get("title", ""), text)

    kept = rank.rank_candidates(rank.filter_candidates(candidates, dedup, window_start, today, pref_dir), explore_dir)
    leftovers = build_source_leftovers(src_map, kept, dedup, window_start, today)

    os.makedirs(CAND_DIR, exist_ok=True)
    out_file = os.path.join(CAND_DIR, "Daily-Brief-%s-candidates.json" % today.strftime("%Y-%m-%d"))
    payload = {
        "generated_at": now.isoformat(),
        "window": {"start": str(window_start), "end": str(today)},
        "preference": {
            "pref_dir": pref_dir,
            "explore_dir": explore_dir,
            "likes_count": len(rank.likes_mod.load_likes(LIKES_FILE)),
        },
        "candidates": kept,
        "source_leftovers": leftovers,
    }
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    if not kept:
        print("[COLLECT_FAILED] 采集脚本失败：候选 0 篇（搜索源全挂或全部被硬过滤）。本次简报未生成。")
        return 1

    print("时间窗口：%s ~ %s（触发日 %s）" % (window_start, today, today))
    pref_txt = "偏好方向: %s | " % pref_dir if pref_dir else ""
    print("候选 %d 篇（已去重、已过滤 <500 字；窗口外 ≤7 天已打标放行，>7 天剔除；%s探索方向: %s）" % (len(kept), pref_txt, explore_dir))
    for i, it in enumerate(kept, 1):
        flag = "日期未验证" if not it["date_verified"] else (it["publish_date"] or "日期未知")
        if it["window_outside_days"]:
            flag += " [窗口外%d天]" % it["window_outside_days"]
        sus = " [疑似AI水文:%s]" % ",".join(it["watermark_reasons"]) if it["watermark_suspect"] else ""
        tags = ["探索"] if it["is_explore"] else (["偏好"] if it["is_preferred"] else [])
        if it["direction"]:
            tags.append(it["direction"])
        print("[%d]%s %s | %s | %s | %d字%s" % (i, (" [%s]" % ",".join(tags)) if tags else "", it["title"], it["domain"], flag, it["word_count"], sus))
        print("    %s" % it["url"])
    print("未推荐源 %d 个（无文章入选，各随机取 1 条最新）：" % len(leftovers))
    for lo in leftovers:
        dk = "日期未验证" if not lo["date_verified"] else lo["publish_date"]
        print("  - [%s] %s | %s | %s" % (lo["source"], lo["title"], dk, lo["url"]))
    print("候选详情 JSON：%s" % out_file)


if __name__ == "__main__":
    sys.exit(main())
