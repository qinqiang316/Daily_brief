#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M2 新源发现：探测现有固定源之外的新高质量信息源。

探测 → 抓候选文章 → quality 判定 → 来源质量 → 方向归类 → 去重记录 → 输出。
搜索时间窗口不限；找不到高质量新源时输出空结果，不硬凑。

"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
_SCRIPTS = os.path.join(ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import collect_brief          # scripts/ 下，靠上面 sys.path 引入
from modules import filter as filter_mod
from modules import quality
from modules import rank as rank_mod
from modules import retrieve
from modules.window import TZ, log

likes_mod = rank_mod.likes_mod

SOURCES_FILE = os.path.join(ROOT, "data", "sources.json")
LIKES_FILE = os.path.join(ROOT, "data", "likes.json")
MAX_TOP = 2
MIN_SOURCE_WORDS = 300

# 明显不适合“长期关注新源”的聚合/社交/视频站 & 内容农场(UGC 低质)
NOISE_DOMAINS = {
    "zhihu.com", "reddit.com", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "baidu.com", "weibo.com", "toutiao.com",
    "news.ycombinator.com", "youtube.com", "bilibili.com", "douban.com",
    "wikipedia.org", "wikiwand.com",
    # 内容农场 / UGC 低质约稿平台（做了再多的文章也不等于高质量固定源）
    "csdn.net", "blog.csdn.net", "juejin.cn", "cnblogs.com", "segmentfault.com",
    "oschina.net", "v2ex.com", "掘金", "简书", "jianshu.com", "sf.gg",
    "qiita.com", "dev.to", "medium.com",
}

# 不用 site: 限定，专用于跳出固定源探测新域名
DISCOVERY_QUERIES = [
    {"query": "高质量 深度 科技 人工智能 独立博客", "max_results": 8, "direction": "AI"},
    {"query": "优秀 深度 商业 分析 方法论 博客", "max_results": 8, "direction": "商业"},
    {"query": "值得关注 深度 思维 框架 反思", "max_results": 8, "direction": "科技"},
    {"query": "longform essay deep analysis technology", "max_results": 8, "direction": "科技"},
    {"query": "best essays business strategy framework", "max_results": 8, "direction": "商业"},
    {"query": "deep dive research health science", "max_results": 8, "direction": "健康"},
    {"query": "城市轨道 磁浮 高速铁路 技术 深度 分析", "max_results": 8, "direction": "轨道交通"},
]


def _site_domains(queries):
    doms = set()
    for q in queries:
        m = re.search(r"site:([A-Za-z0-9.\-]+)", q.get("query", ""))
        if m:
            doms.add(m.group(1).lower().lstrip("www."))
    return doms


KNOWN_DOMAINS = (_site_domains(collect_brief.QUERIES_CN)
                 | _site_domains(collect_brief.QUERIES_INTL)
                 | {d.lower().lstrip("www.") for d in filter_mod.EXCLUDE_DOMAINS})


def _domain(url):
    if "://" not in url:
        return ""
    host = url.split("/")[2].lower()
    return re.sub(r"^www\.", "", host).split(":")[0]


def _in_noise(domain):
    """匹配域名或其父域是否命中 NOISE_DOMAINS（支持 blog.csdn.net → csdn.net）。"""
    if not domain:
        return False
    parts = domain.split(".")
    for i in range(len(parts)):
        candidate = ".".join(parts[i:])
        if candidate in NOISE_DOMAINS:
            return True
    return False


def _load_sources():
    default = {"sources": [], "updated_at": ""}
    if not os.path.exists(SOURCES_FILE):
        return default
    try:
        with open(SOURCES_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("sources"), list):
            return data
    except Exception as e:
        log("读 sources.json 失败: %s" % e)
    return default


def _source_recorded(sources, domain, url):
    for s in sources:
        if not isinstance(s, dict):
            continue
        if s.get("domain", "").lower() == domain:
            return True
        if filter_mod.norm_url(s.get("url", "")) == filter_mod.norm_url(url):
            return True
    return False


def _save_sources(data):
    data["updated_at"] = datetime.now(TZ).isoformat()
    os.makedirs(os.path.dirname(SOURCES_FILE), exist_ok=True)
    with open(SOURCES_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def _search(queries):
    """复用 retrieve.run_cli / anysearch_http_batch / parse_search_markdown。"""
    items = []
    for bi, sub in enumerate(retrieve.chunked(queries, 5)):
        sub_cli = [{"query": q["query"], "max_results": q.get("max_results", 8)}
                   for q in sub]
        md = None
        qfile = "/tmp/discover_queries_%d_%d.json" % (os.getpid(), bi)
        try:
            with open(qfile, "w", encoding="utf-8") as fh:
                json.dump(sub_cli, fh, ensure_ascii=False)
            md = retrieve.run_cli(["batch_search", "--queries", "@" + qfile])
            os.remove(qfile)
        except OSError:
            log("写临时查询文件失败，改用 HTTP 直连")
            if os.path.exists(qfile):
                try:
                    os.remove(qfile)
                except OSError:
                    pass
        batch = retrieve.parse_search_markdown(md) if md else (retrieve.anysearch_http_batch(sub_cli) or [])
        for it in batch:
            qi = it.pop("query_idx", None)
            if qi is not None and 0 <= qi < len(sub):
                it["discovery_query"] = sub[qi]["query"]
                it["discovery_direction"] = sub[qi]["direction"]
            items.append(it)
    return items


def _source_quality(url, content, ev):
    reasons = []
    domain = _domain(url)
    if _in_noise(domain):
        reasons.append("聚合/社交/内容农场站")
    seg = [x for x in url.lower().split("://", 1)[-1].split("/") if x]
    if len(seg) <= 1:
        reasons.append("主页/列表页")
    if retrieve.wc(content) < MIN_SOURCE_WORDS:
        reasons.append("正文过短")
    if ev["ai_ratio"] > quality.AI_RATIO_MAX:
        reasons.append("疑似AI生成")
    return not reasons, reasons


def _accept_direction(direction, pref_dir, explore_dir):
    if direction not in likes_mod.DIRECTIONS:
        return False
    if pref_dir and direction == pref_dir:
        return True
    if explore_dir and direction == explore_dir:
        return True
    return pref_dir is None


def _self_eval(h, pref_dir, explore_dir):
    ev = h["ev"]
    why = "评分达标(ai=%s, evidence=%s, inspiring=%s)" % (
        ev["ai_ratio"], ev["evidence"], ev["inspiring"])
    match = "方向:%s" % h["direction"]
    if pref_dir and h["direction"] == pref_dir:
        match += "，命中偏好方向"
    return "这源值得长期关注吗：是，%s；%s。" % (why, match)


def main(argv=None):
    p = argparse.ArgumentParser(description="M2 新源发现")
    p.add_argument("--limit", type=int, default=30, help="最多探测的新域候选数")
    p.add_argument("--top", type=int, default=MAX_TOP, help="输出达标候选数(1-2)")
    args = p.parse_args(argv)
    args.top = max(1, min(args.top, 2))

    now = datetime.now(TZ)
    data = _load_sources()
    likes = likes_mod.load_likes(LIKES_FILE)
    pref_dir = likes_mod.preference_direction(likes)
    explore_dir = likes_mod.explore_direction(pref_dir, now)

    searched, hits, seen = 0, [], set()
    results = _search(DISCOVERY_QUERIES)
    for item in results:
        url = filter_mod.norm_url(item.get("url", ""))
        domain = _domain(url)
        if not url or not domain or domain in KNOWN_DOMAINS:
            continue
        key = (domain, url)
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > args.limit:
            break
        if _source_recorded(data["sources"], domain, url):
            continue
        searched += 1

        # ① 文章质量 → ② 来源质量 → ③ 方向匹配（严格顺序）
        content = retrieve.fetch_article_text(url, timeout=15)
        if not content:
            continue
        ev = quality.evaluate(content)
        if not ev["pass"]:
            continue
        ok_source, src_reasons = _source_quality(url, content, ev)
        if not ok_source:
            log("来源质量未过: %s %s" % (domain, "，".join(src_reasons)))
            continue
        direction = likes_mod.infer_direction(url, item.get("title", ""))
        if not _accept_direction(direction, pref_dir, explore_dir):
            log("方向未匹配: %s -> %s" % (domain, direction))
            continue

        hits.append({
            "title": item.get("title", ""), "url": url, "domain": domain,
            "direction": direction, "ev": ev,
            "discovery_query": item.get("discovery_query", ""),
        })
        if len(hits) >= args.top:
            break

    if not hits:
        if not results:
            print("[DISCOVER_EMPTY] 搜索未返回结果（CLI/HTTP 均失败或没有新域命中）。本次不硬凑。")
        else:
            print("[DISCOVER_EMPTY] 探测新域 %d 个，均未通过 文章质量/来源质量/方向匹配。本次不硬凑。" % searched)
        return 0

    for i, h in enumerate(hits, 1):
        ev = h["ev"]
        print("[%d] %s | %s | 方向:%s | ai=%.2f evidence=%d inspiring=%s" % (
            i, h["title"][:80], h["domain"], h["direction"],
            ev["ai_ratio"], ev["evidence"], ev["inspiring"]))
        print("    %s" % h["url"])
        print("    %s" % _self_eval(h, pref_dir, explore_dir))

    for h in hits:
        data["sources"].append({
            "name": (h["title"][:60] or h["domain"]),
            "url": h["url"],
            "domain": h["domain"],
            "status": "candidate",
            "direction": h["direction"],
            "discovered_at": now.strftime("%Y-%m-%d"),
            "note": "评分达标：ai=%.2f, evidence=%d, inspiring=%s"
                    % (h["ev"]["ai_ratio"], h["ev"]["evidence"], h["ev"]["inspiring"]),
        })
    _save_sources(data)
    print("已记录新源 %d 个到 %s" % (len(hits), SOURCES_FILE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
