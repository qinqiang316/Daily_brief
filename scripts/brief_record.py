#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简报内容记录模块：每次 validate PASS 后自动记录简报元数据。

数据文件：data/brief_records.json
"""
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

BRIEF_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RECORDS_FILE = os.path.join(BRIEF_DIR, "data", "brief_records.json")
OUTPUT_DIR = os.path.join(BRIEF_DIR, "output")
SCRIPTS_DIR = os.path.join(BRIEF_DIR, "scripts")

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
if BRIEF_DIR not in sys.path:
    sys.path.insert(0, BRIEF_DIR)

import collect_brief
from modules.profile import TOPIC_KEYWORDS, extract_topics
from likes import STOPWORDS, infer_direction

TZ = timezone(timedelta(hours=8))

# 扩展英文停用词（likes.py 的 STOPWORDS 只有少量）
_EXTRA_STOPWORDS = {
    "was", "has", "had", "have", "are", "were", "been", "being",
    "does", "did", "will", "would", "could", "should", "may", "might",
    "shall", "can", "need", "dare", "ought", "used",
    "this", "that", "these", "those", "what", "which", "who", "whom",
    "when", "where", "why", "how", "all", "each", "every", "both",
    "few", "more", "most", "other", "some", "such", "only", "own",
    "same", "than", "too", "very", "just", "about", "above", "after",
    "again", "also", "any", "because", "before", "between", "come",
    "even", "first", "from", "into", "just", "keep", "know", "last",
    "like", "long", "look", "made", "make", "many", "may", "much",
    "must", "never", "next", "only", "over", "part", "put", "read",
    "real", "said", "says", "see", "since", "still", "take", "tell",
    "then", "them", "they", "time", "told", "took", "turn", "upon",
    "very", "want", "well", "went", "were", "what", "when", "will",
    "with", "work", "year", "your",
}
MERGED_STOPWORDS = STOPWORDS | _EXTRA_STOPWORDS


def extract_source(url):
    """从 URL 提取域名（去掉 www. 前缀）。"""
    if not url or "://" not in url:
        return ""
    host = url.split("/")[2].lower()
    host = re.sub(r"^www\.", "", host)
    return host


def extract_keywords_single(text, top_n=3):
    """从单篇文本提取关键词：中文词≥2字 + 英文词≥3字母，过滤停用词。"""
    words = []
    words += re.findall(r"[\u4e00-\u9fff]{2,8}", text or "")
    words += [w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text or "")]
    freq = {}
    for w in words:
        if w.lower() in MERGED_STOPWORDS:
            continue
        key = w.lower()
        freq[key] = freq.get(key, 0) + 1
    ranked = sorted(freq.items(), key=lambda x: (-x[1], len(x[0])))
    return [w for w, _ in ranked[:top_n]]


def load_records():
    """读取 brief_records.json，返回列表；无文件返回空列表。"""
    if os.path.exists(RECORDS_FILE):
        try:
            with open(RECORDS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            records = data.get("records", [])
            if isinstance(records, list):
                return records
        except Exception:
            pass
    return []


def save_records(records):
    """写回 brief_records.json。"""
    os.makedirs(os.path.dirname(RECORDS_FILE), exist_ok=True)
    with open(RECORDS_FILE, "w", encoding="utf-8") as f:
        json.dump({"records": records}, f, ensure_ascii=False, indent=2)


def record_brief(brief_path, cand_path):
    """从候选 JSON + 简报 md 提取元数据并追加记录。
    同一日期只记录一次（去重）。
    Returns: 新增记录的 dict，或 None（失败/已存在时）。
    """
    if not brief_path or not os.path.exists(brief_path):
        print("[brief_record] 简报不存在: %s" % brief_path, file=sys.stderr)
        return None
    if not cand_path or not os.path.exists(cand_path):
        print("[brief_record] 候选 JSON 不存在: %s" % cand_path, file=sys.stderr)
        return None

    # 读候选 JSON
    with open(cand_path, encoding="utf-8") as f:
        cand_data = json.load(f)
    generated_at = cand_data.get("generated_at", "")
    pref_info = cand_data.get("preference", {})

    # 读简报 md
    with open(brief_path, encoding="utf-8", errors="ignore") as fh:
        brief_text = fh.read()

    # 日期
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if generated_at:
        try:
            dt = datetime.fromisoformat(generated_at)
            today = dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    # 去重：同一天只记录一次
    records = load_records()
    if any(r.get("date") == today for r in records):
        print("[brief_record] 简报 %s 已有记录，跳过" % today)
        return None

    # 简报中出现的 URL → 属于哪个 section
    url_section = _map_url_to_section(brief_text)

    # 从候选 JSON 构建 url→candidate 映射
    cand_map = {}
    for c in cand_data.get("candidates", []):
        u = collect_brief.norm_url(c.get("url", ""))
        if u:
            cand_map[u] = c

    # 构建文章列表
    articles = []
    seen_urls = set()
    for m in re.finditer(r"https?://[^\s)\]>]+", brief_text):
        url = collect_brief.norm_url(m.group(0))
        if not url or url in seen_urls:
            continue
        # 跳过本地点赞服务链接
        host = url.split("/")[2].lower() if "://" in url else ""
        if host in ("127.0.0.1:8900", "localhost:8900", "127.0.0.1", "localhost"):
            continue
        seen_urls.add(url)

        cand_c = cand_map.get(url, {})
        title = cand_c.get("title", "")
        direction = cand_c.get("direction")
        hn_points = cand_c.get("hn_points")
        word_count = cand_c.get("word_count")
        content_text = cand_c.get("content", "")

        # direction fallback：candidates 里可能是 null，用 infer_direction 补
        if not direction:
            direction = infer_direction(url, title)

        # 话题：复用 profile.extract_topics，传入 content 提高命中率
        topics = extract_topics(url, title, content_text[:1500])

        # 关键词：标题 + 内容摘要
        kw_text = " ".join(filter(None, (title, content_text[:800])))
        keywords = extract_keywords_single(kw_text)

        source = extract_source(url)
        section = url_section.get(url, "")

        articles.append({
            "title": title,
            "url": url,
            "source": source,
            "direction": direction,
            "topics": topics,
            "keywords": keywords,
            "hn_points": hn_points,
            "word_count": word_count,
            "section": section,
        })

    if not articles:
        print("[brief_record] 简报中未提取到文章，跳过记录", file=sys.stderr)
        return None

    # summary
    dir_counter = Counter(a["direction"] for a in articles if a["direction"])
    src_counter = Counter(a["source"] for a in articles if a["source"])
    summary = {
        "total_articles": len(articles),
        "directions": dict(dir_counter),
        "sources": dict(src_counter),
    }

    record = {
        "date": today,
        "generated_at": generated_at,
        "window": _infer_window(brief_text),
        "preference": {
            "pref_dir": pref_info.get("pref_dir"),
            "explore_dir": pref_info.get("explore_dir"),
            "likes_count": pref_info.get("likes_count", 0),
        },
        "articles": articles,
        "summary": summary,
    }

    records.append(record)
    save_records(records)

    print("[brief_record] 已记录简报 %s（%d 篇文章）" % (today, len(articles)))
    return record


def _map_url_to_section(brief_text):
    """把简报 md 中的 URL 映射到所属 section。"""
    result = {}
    current_section = "其他"
    for line in brief_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            header = stripped[3:].strip()
            if "热门文章" in header or "深度总结" in header:
                current_section = "深度总结"
            elif "主题趋势" in header:
                current_section = "今日主题趋势"
            elif "值得深读" in header:
                current_section = "值得深读"
            elif "快速浏览" in header:
                current_section = "快速浏览"
            elif "参考资料" in header:
                current_section = "参考资料"
            else:
                current_section = header
        for m in re.finditer(r"https?://[^\s)\]>]+", stripped):
            url = collect_brief.norm_url(m.group(0))
            if url:
                result[url] = current_section
    return result


def _infer_window(brief_text):
    """从简报正文中推断窗口日期，找不到则返回 None。"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*[~～—\-]+\s*(\d{4}-\d{2}-\d{2})", brief_text)
    if m:
        return {"start": m.group(1), "end": m.group(2)}
    return None


def query_records(start_date, end_date):
    """按日期范围查询记录（含首尾）。"""
    records = load_records()
    return [r for r in records if start_date <= r.get("date", "") <= end_date]


def list_records(n=10):
    """列出最近 n 条记录摘要。"""
    records = load_records()
    return records[-n:]


def stats_records():
    """统计所有记录的来源/方向/话题分布。"""
    records = load_records()
    src_total = Counter()
    dir_total = Counter()
    topic_total = Counter()
    for r in records:
        for a in r.get("articles", []):
            if a.get("source"):
                src_total[a["source"]] += 1
            if a.get("direction"):
                dir_total[a["direction"]] += 1
            for t in a.get("topics", []):
                topic_total[t] += 1
    return {
        "total_records": len(records),
        "total_articles": sum(
            len(r.get("articles", [])) for r in records
        ),
        "sources": dict(src_total.most_common(20)),
        "directions": dict(dir_total.most_common()),
        "topics": dict(topic_total.most_common(20)),
    }


# ── CLI 自测 ──
if __name__ == "__main__":
    print("=== brief_record 自测 ===")
    records = load_records()
    print("已有记录数: %d" % len(records))

    # 尝试自动探测最新简报和候选 JSON 并记录
    brief = None
    if os.path.isdir(OUTPUT_DIR):
        for f in os.listdir(OUTPUT_DIR):
            if f.startswith("Daily-Brief-") and f.endswith(".md"):
                p = os.path.join(OUTPUT_DIR, f)
                if brief is None or os.path.getmtime(p) > os.path.getmtime(brief):
                    brief = p

    cand = None
    cand_dir = collect_brief.CAND_DIR
    if os.path.isdir(cand_dir):
        for f in os.listdir(cand_dir):
            if f.startswith("Daily-Brief-") and f.endswith(".json"):
                p = os.path.join(cand_dir, f)
                if cand is None or os.path.getmtime(p) > os.path.getmtime(cand):
                    cand = p

    if brief and cand:
        print("简报: %s" % brief)
        print("候选: %s" % cand)
        rec = record_brief(brief, cand)
        if rec:
            print("新增记录 date=%s articles=%d" % (rec["date"], len(rec["articles"])))
    else:
        print("未找到简报或候选 JSON，跳过 record_brief 测试")

    # 测试 list
    recent = list_records(5)
    print("\n最近记录（最多5条）:")
    for r in recent:
        s = r.get("summary", {})
        dirs = s.get("directions", {})
        topics = Counter()
        for a in r.get("articles", []):
            for t in a.get("topics", []):
                topics[t] += 1
        print("  %s | %d篇 | 方向:%s | 话题:%s" % (
            r.get("date"), s.get("total_articles", 0),
            dict(dirs), dict(topics.most_common(3)),
        ))

    # 测试 stats
    st = stats_records()
    print("\n统计: %d 条记录, %d 篇文章" % (st["total_records"], st["total_articles"]))
    if st["directions"]:
        print("  方向分布: %s" % st["directions"])
    if st["topics"]:
        print("  话题分布: %s" % dict(list(st["topics"].items())[:8]))
    if st["sources"]:
        print("  来源分布: %s" % dict(list(st["sources"].items())[:8]))
    print("=== 自测完成 ===")
