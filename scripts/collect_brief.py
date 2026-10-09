#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DailyBrief 采集编排入口。具体职责位于 modules/window|retrieve|filter|rank。"""
import json
import os
import random
import re
import sys
import tempfile
import uuid
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
# 特殊源主页/列表页取最新配置（未进候选时优先从主页/列表页提取最新文章）
HOMEPAGE_LATEST_SOURCES = {
    "jiqizhixin.com": {
        "list_url": "https://www.jiqizhixin.com/industry",
        "url_pattern": r"https://www\.jiqizhixin\.com/articles/\d{4}-\d{2}-\d{2}-\d+",
    },
}


def _leftover_date_evidence(item, window_start_s, today_s):
    """速览日期把关：统一走 extract_date_evidence，只收日精度、有 date_source/evidence、
    且落在本次窗口内的原文日期。月精度/无证据/未来/窗口外一律拒收（返回 None）。
    不信任条目自带的 publish_date/date_verified 布尔位（TG 搬运日不是原文日）。"""
    ev = filter_mod.extract_date_evidence(item)
    pub = ev["publish_date"]
    if not pub or not ev["date_source"]:
        return None
    if ev["precision"] != "day":
        return None
    if pub > today_s or pub < window_start_s:
        return None
    return ev


def pick_homepage_latest_item(cfg, dedup, kept_urls, window_start, today, budget=None):
    """从特殊源主页/列表页通过 extract 通道提取最新文章。
    网络请求受总 Budget 控制：预算耗尽不发请求，直接返回 None。"""
    list_url = cfg.get("list_url")
    url_pattern = cfg.get("url_pattern")
    if not list_url:
        return None
    if budget and budget.expired():
        log("特殊源主页提取跳过（总预算耗尽）: %s" % list_url)
        return None
    try:
        raw_items = retrieve.anysearch_extract(list_url, budget=budget)
    except Exception as e:
        log("提取特殊源主页失败(%s): %s" % (list_url, e))
        return None

    if not raw_items:
        return None

    window_start_s = window_start.strftime("%Y-%m-%d")
    today_s = today.strftime("%Y-%m-%d")

    pool = []
    for it in raw_items:
        raw_url = it.get("url", "")
        if url_pattern and not re.search(url_pattern, raw_url):
            continue
        url = filter_mod.norm_url(raw_url)
        if not url or url in dedup or url in kept_urls or filter_mod.is_aggregate_url(url):
            continue
        domain = url.split("/")[2].lower() if "://" in url else ""
        if domain in filter_mod.EXCLUDE_DOMAINS or domain in LEFTOVER_EXCLUDE_DOMAINS:
            continue
        title = (it.get("title") or "").strip()
        if not title or title.startswith("http"):
            continue
        ev = _leftover_date_evidence({"url": url, "title": title}, window_start_s, today_s)
        if not ev:
            continue  # 无日精度窗口内证据禁止交付（含速览）
        m = re.search(r"(\d{4}-\d{2}-\d{2})(?:-(\d+))?", url)
        seq = int(m.group(2)) if m and m.group(2) else 0
        pool.append({
            "title": title[:200],
            "url": url,
            "domain": domain,
            "publish_date": ev["publish_date"],
            "date_verified": True,
            "date_source": ev["date_source"],
            "date_evidence": ev["evidence"],
            "date_precision": ev["precision"],
            "_sort_key": (ev["publish_date"], seq),
        })

    if not pool:
        return None

    pool.sort(key=lambda x: x["_sort_key"], reverse=True)
    best = pool[0]
    best.pop("_sort_key", None)
    return best


def pick_leftover_item(items, dedup, kept_urls, window_start, today, source_key=None, source_info=None, budget=None):
    """从某来源的全部原始条目中挑 1 条可展示的最新内容。
    把关：去重、候选内不重复、黑名单/RSS/裸域名/t.me 内部链接剔除、无标题/URL式标题剔除、
    栏目导航页剔除。
    日期统一走 extract_date_evidence：只收日精度、有 date_source/evidence、且落在
    本次窗口内的原文日期；月精度/无证据/未来/窗口外一律拒收（无证据即跳过）。
    不信任条目自带 publish_date/date_verified 布尔位（TG 搬运日不是原文日）。
    若命中了 HOMEPAGE_LATEST_SOURCES 特殊源配置，优先走主页 extract 提取最新文章
    （受总 Budget 控制，预算耗尽不发请求）；失败或无匹配则回退到现有随机挑选逻辑。
    返回条目携带 date_source/date_evidence/date_precision。"""
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
        picked = pick_homepage_latest_item(matched_cfg, dedup, kept_urls, window_start, today, budget=budget)
        if picked:
            return picked

    # 回退到现有随机挑选逻辑
    window_start_s = window_start.strftime("%Y-%m-%d")
    today_s = today.strftime("%Y-%m-%d")
    pool = []
    for it in items:
        url = filter_mod.norm_url(it.get("url", ""))
        if not url or url in dedup or url in kept_urls:
            continue
        domain = url.split("/")[2].lower() if "://" in url else ""
        if domain in filter_mod.EXCLUDE_DOMAINS or domain in LEFTOVER_EXCLUDE_DOMAINS or "/rss" in url or url.rstrip("/").endswith(".rss"):
            continue
        if "t.me/" in url and "/s/" not in url:
            continue
        title = (it.get("title") or "").strip()
        if not title or title.startswith("http"):
            continue
        # 聚合页剔除：首页/频道/列表/导航/翻页目录禁止进速览；
        # 独立文章与带独立日期的 digest（如 ACS）不误杀
        if filter_mod.is_aggregate_url(url):
            continue
        # 日期：统一 extract_date_evidence 取证；条目自带 publish_date/date_verified 不作数
        # （TG 搬运日不是原文日）。无日精度窗口内证据即跳过。
        ev = _leftover_date_evidence(it, window_start_s, today_s)
        if not ev:
            continue
        pool.append({
            "title": title[:200],
            "url": url,
            "domain": domain,
            "publish_date": ev["publish_date"],
            "date_verified": True,
            "date_source": ev["date_source"],
            "date_evidence": ev["evidence"],
            "date_precision": ev["precision"],
        })
    if not pool:
        return None
    pool.sort(key=lambda x: x["publish_date"], reverse=True)
    return random.choice(pool[:3])


def build_source_leftovers(src_map, kept, dedup, window_start, today, budget=None):
    """未推荐源 = 抓到了内容但没有任何一条进候选池的源。每源随机挑 1 条最新内容。
    特殊源主页 extract 的网络请求受总 Budget 控制（budget 透传）。"""
    kept_urls = {c["url"] for c in kept}
    kept_keys = {c.get("source_key") for c in kept if c.get("source_key")}
    leftovers = []
    for key, info in src_map.items():
        if key in kept_keys or not info["items"]:
            continue
        picked = pick_leftover_item(info["items"], dedup, kept_urls, window_start, today,
                                    source_key=key, source_info=info, budget=budget)
        if not picked:
            continue
        leftovers.append({
            "source": info["label"],
            "source_key": key,
            "direction": info["direction"],
            **picked,
        })
    return leftovers


LOCK_FILE = os.path.join(BRIEF_DIR, "data", "_collect.lock")
TOTAL_BUDGET_S = 480  # 采集总预算：所有网络阶段共享一个墙钟截止时间，防累积超时
BODY_RESERVE_S = 150  # 给正文抓取/准入阶段预留的预算（搜索不得花光）


def _priority_sorted(queries, pref_dir, explore_dir):
    """重要方向优先：轨交/偏好/探索方向排前，预算紧张时先保它们。"""
    hot = {rank.RAIL_DIRECTION, pref_dir, explore_dir} - {None}
    return sorted(queries, key=lambda q: 0 if q.get("direction") in hot else 1)


def acquire_lock(path=None):
    """非阻塞采集锁。成功返回锁文件句柄（保持即持有），已被占用返回 None。"""
    import fcntl
    path = path or LOCK_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write("pid=%d\n" % os.getpid())
    fh.flush()
    return fh


def release_lock(fh):
    if not fh:
        return
    import fcntl
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def atomic_write_json(path, payload):
    """原子落盘：先写同目录临时文件再 os.replace，避免半截 JSON。"""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _cand_path(today_s):
    return os.path.join(CAND_DIR, "Daily-Brief-%s-candidates.json" % today_s)


def _qualified_cache_candidate(c, win_start, win_end):
    """缓存候选逐条合格校验：与采集准入同一标准（正文实测 wc、日精度窗口内日期证据、合法 URL）。
    word_count 元数据仅参考，真实 content 实测才算数。"""
    if not isinstance(c, dict):
        return False
    url = c.get("url") or ""
    if not filter_mod.is_valid_url(url):
        return False
    if filter_mod.is_aggregate_url(url):
        return False  # 聚合页（首页/频道/列表/导航）不算合格候选
    pub = c.get("publish_date") or ""
    if not filter_mod.is_iso_day(pub) or c.get("date_precision") != "day":
        return False
    if not filter_mod.has_date_provenance(c):
        return False
    if not c.get("date_verified"):
        return False
    if c.get("window_outside_days"):
        return False
    if not (filter_mod.is_iso_day(win_start) and filter_mod.is_iso_day(win_end)
            and win_start <= pub <= win_end):
        return False
    if retrieve.wc(c.get("content") or "") < MIN_WORDS:
        return False
    return True


def _qualified_cache_leftover(lo, win_start, win_end):
    """缓存速览逐条合格校验：日精度、有来源/证据、落在窗口内、URL 合法。"""
    if not isinstance(lo, dict):
        return False
    url = lo.get("url") or ""
    if not filter_mod.is_valid_url(url):
        return False
    if filter_mod.is_aggregate_url(url):
        return False  # 聚合页速览不合格
    pub = lo.get("publish_date") or ""
    if not filter_mod.is_iso_day(pub) or lo.get("date_precision") != "day":
        return False
    if not filter_mod.has_date_provenance(lo):
        return False
    if not lo.get("date_verified"):
        return False
    if not (filter_mod.is_iso_day(win_start) and filter_mod.is_iso_day(win_end)
            and win_start <= pub <= win_end):
        return False
    return True


def load_valid_cache(path, today_s):
    """严格合格缓存：同日 generated_at 只是必要条件；还须 run_id 存在、窗口与当日一致、
    每条候选/速览都过合格校验（正文实测 wc、日精度窗口内日期证据、合法 URL）。
    任一条不合格即 MISS——不得复用同日旧坏池。"""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        log("缓存读取失败(%s): %s" % (path, e))
        return None
    gen = (payload.get("generated_at") or "")[:10]
    if gen != today_s:
        return None
    if not payload.get("run_id"):
        return None
    window = payload.get("window") or {}
    win_start = str(window.get("start") or "")[:10]
    win_end = str(window.get("end") or "")[:10]
    if not win_start or win_end != today_s:
        return None
    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    if not all(_qualified_cache_candidate(c, win_start, win_end) for c in candidates):
        return None
    for lo in payload.get("source_leftovers") or []:
        if not _qualified_cache_leftover(lo, win_start, win_end):
            return None
    return payload


def main(argv=None):
    try:
        return _main(argv)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("[COLLECT_FAILED] 采集脚本异常：%s。本次简报未生成。" % e)
        return 1


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="DailyBrief 采集编排入口")
    ap.add_argument("--reuse-cache", action="store_true",
                    help="显式复用同日有效候选缓存（存在且合格则直接退出 0，不重新采集）")
    args = ap.parse_args(argv)

    now = datetime.now(TZ)
    today_s = now.strftime("%Y-%m-%d")
    os.makedirs(CAND_DIR, exist_ok=True)
    out_file = _cand_path(today_s)

    if args.reuse_cache:
        cached = load_valid_cache(out_file, today_s)
        if cached:
            print("[CACHE_HIT] 复用同日有效候选缓存（run_id=%s，%d 篇）：%s"
                  % (cached.get("run_id", "?"), len(cached["candidates"]), out_file))
            return 0
        print("[CACHE_MISS] 无同日有效缓存，执行全新采集")

    lock = acquire_lock()
    if lock is None:
        print("[COLLECT_FAILED] 已有采集进程在运行（锁占用 %s）。本次简报未生成。" % LOCK_FILE)
        return 1
    try:
        return _collect(now, today_s, out_file)
    finally:
        release_lock(lock)


def _collect(now, today_s, out_file):
    budget = retrieve.Budget(TOTAL_BUDGET_S)
    # 搜索/源采集只用子预算，给正文抓取与准入阶段预留 BODY_RESERVE_S，
    # 防止预算先被搜索花光、正文阶段无预算
    search_budget = budget.derive(TOTAL_BUDGET_S - BODY_RESERVE_S)
    run_id = now.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    window_start, today = compute_window(now)
    dedup = filter_mod.auto_update_dedup()
    extra, pref_dir, explore_dir = rank.build_extra_queries(now)
    src_map = {}
    candidates = retrieve.fetch_hn(now, budget=search_budget)
    for c in candidates:
        c["direction"] = rank.likes_mod.infer_direction(c.get("url", ""), c.get("title", ""))
        c["is_preferred"] = False
        c["is_explore"] = False
        c["source_key"] = "hn"
        c["source_label"] = "Hacker News"
        src_map.setdefault("hn", {"label": "Hacker News", "direction": "科技", "items": []})["items"].append(c)
    src_stats = {"hn": len(candidates)}

    queries_cn, queries_intl = build_default_queries(now)
    # 偏好/探索查询先行（预算紧张时先保重要方向），再轨交优先的默认查询
    if extra:
        rank.search_batch_with_tags("pref", extra, candidates, src_stats, pref_dir, src_map, budget=search_budget)
    for name, queries in (("cn", _priority_sorted(queries_cn, pref_dir, explore_dir)),
                          ("intl", _priority_sorted(queries_intl, pref_dir, explore_dir))):
        rank.search_batch_with_tags(name, queries, candidates, src_stats, pref_dir, src_map, budget=search_budget)

    # Telegram 频道采集
    tg_items = retrieve.fetch_telegram_channels(TELEGRAM_CHANNELS, budget=search_budget)
    for c in tg_items:
        c["direction"] = rank.likes_mod.infer_direction(c.get("url", ""), c.get("title", ""))
        ch = c.get("telegram_channel", "?")
        c["source_key"] = "tg:" + ch
        c["source_label"] = "TG@" + ch
        src_map.setdefault(c["source_key"], {"label": c["source_label"], "direction": c["direction"], "items": []})["items"].append(c)
    candidates.extend(tg_items)
    src_stats["telegram"] = len(tg_items)

    # Agent Case Share AI 新闻日报（AI 领域优先参考源，2026-09-10 加）
    acs_items = retrieve.fetch_agent_case_share(budget=search_budget)
    for c in acs_items:
        c["source_label"] = "Agent Case Share AI日报"
        src_map.setdefault("acs", {"label": "Agent Case Share AI日报", "direction": "AI", "items": []})["items"].append(c)
    candidates.extend(acs_items)
    src_stats["agent_case_share"] = len(acs_items)

    kept = rank.rank_candidates(
        rank.filter_candidates(candidates, dedup, window_start, today, pref_dir, budget=budget),
        explore_dir)

    if not kept:
        # 空池熔断：不覆盖已有候选文件，本次明确失败，由上游决定重试或放弃
        print("[COLLECT_FAILED] 采集脚本失败：候选 0 篇（搜索源全挂或全部被硬过滤）。"
              "熔断：不覆盖已有候选文件 %s，本次简报未生成。" % out_file)
        return 1

    leftovers = build_source_leftovers(src_map, kept, dedup, window_start, today, budget=budget)
    payload = {
        "run_id": run_id,
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
    atomic_write_json(out_file, payload)

    print("时间窗口：%s ~ %s（触发日 %s，run_id %s）" % (window_start, today, today, run_id))
    pref_txt = "偏好方向: %s | " % pref_dir if pref_dir else ""
    print("候选 %d 篇（已去重；统一要求正文 ≥%d 字 + 精确到日的窗口内发布日期，未知/月精度/未来/窗口外日期一律拒收；%s探索方向: %s）"
          % (len(kept), MIN_WORDS, pref_txt, explore_dir))
    for i, it in enumerate(kept, 1):
        flag = it["publish_date"] or "日期未知"
        sus = " [疑似AI水文:%s]" % ",".join(it["watermark_reasons"]) if it["watermark_suspect"] else ""
        tags = ["探索"] if it["is_explore"] else (["偏好"] if it["is_preferred"] else [])
        if it["direction"]:
            tags.append(it["direction"])
        print("[%d]%s %s | %s | %s | %d字%s" % (i, (" [%s]" % ",".join(tags)) if tags else "", it["title"], it["domain"], flag, it["word_count"], sus))
        print("    %s" % it["url"])
    print("未推荐源 %d 个（无文章入选，各取 1 条有日期证据的最新内容）：" % len(leftovers))
    for lo in leftovers:
        print("  - [%s] %s | %s | %s" % (lo["source"], lo["title"], lo["publish_date"], lo["url"]))
    print("候选详情 JSON：%s" % out_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
