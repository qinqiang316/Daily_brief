import json
import os
import re
import sys
from datetime import datetime

from . import filter as filter_mod
from .retrieve import anysearch_http_batch, chunked, fetch_article_text, parse_search_markdown, run_cli, wc
from .window import log
from . import profile as profile_mod

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path: sys.path.insert(0, SCRIPTS_DIR)
import likes as likes_mod

MAX_CANDIDATES, MAX_HN, HN_FETCH_LIMIT = 15, 8, 12
LIKES_FILE = os.path.join("/Users/qqiang/AI project/05-日常工具/DailyBrief", "data", "likes.json")

def build_extra_queries(now):
    likes = likes_mod.load_likes(LIKES_FILE)
    pref_dir = likes_mod.preference_direction(likes)
    profile = profile_mod.compute_profile(likes)
    explore_queries = likes_mod.build_explore_queries(
        pref_dir, likes, profile, now
    )
    explore_dir = (
        explore_queries[0]["direction"]
        if explore_queries else likes_mod.explore_direction(pref_dir, now)
    )
    extra = []
    if pref_dir: extra.extend(likes_mod.build_pref_queries(pref_dir, likes, now))
    extra.extend(explore_queries)
    if pref_dir:
        log("点赞 %d 条 | 偏好方向: %s（样本达标，已启用偏好优先采集）| 探索方向: %s | 增强查询 %d 条" % (len(likes), pref_dir, explore_dir, len(extra)))
    elif extra:
        log("点赞 %d 条 | 偏好方向: 无（样本不足 %d 条或方向不集中，暂不注入偏好查询）| 探索方向: %s | 增强查询 %d 条（仅探索）" % (len(likes), likes_mod.MIN_LIKES_FOR_PREFERENCE, explore_dir, len(extra)))
    else: log("暂无点赞，不注入偏好查询（探索方向: %s）" % explore_dir)
    return extra, pref_dir, explore_dir

def search_batch_with_tags(name, queries, candidates, src_stats, pref_dir, src_map=None):
    """src_map: 若传入，为每条结果打 source_key/source_label 并按源归组（未推荐源速览用）。"""
    total = 0
    for bi, sub in enumerate(chunked(queries, 5)):
        sub_cli = [{"query": q["query"], "max_results": q.get("max_results", 8)} for q in sub]
        qfile = "/tmp/brief_queries_%d_%s_%d.json" % (os.getpid(), name, bi)
        with open(qfile, "w", encoding="utf-8") as f: json.dump(sub_cli, f, ensure_ascii=False)
        md = run_cli(["batch_search", "--queries", "@" + qfile])
        os.remove(qfile)
        batch = parse_search_markdown(md) if md else (anysearch_http_batch(sub_cli) or [])
        for it in batch:
            qidx = it.pop("query_idx", None)
            if qidx is not None and 0 <= qidx < len(sub):
                q = sub[qidx]; it["direction"] = q.get("direction"); it["is_preferred"] = bool(q.get("preferred")); it["is_explore"] = bool(q.get("explore"))
                if src_map is not None:
                    key = "%s:%d" % (name, bi * 5 + qidx)
                    label = "查询 %s#%d: %s" % (name, bi * 5 + qidx + 1, q["query"][:48])
                    it["source_key"] = key; it["source_label"] = label
                    src_map.setdefault(key, {"label": label, "direction": q.get("direction"), "items": []})["items"].append(it)
            else: it.update(direction=None, is_preferred=False, is_explore=False)
        total += len(batch); candidates.extend(batch)
    src_stats[name] = total
    log("来源 %s 返回 %d 条" % (name, total))

def filter_candidates(candidates, dedup, window_start, today, pref_dir):
    kept, seen_urls = [], set(dedup)
    window_start_s, today_s = window_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")
    hn_count = 0
    for c in candidates:
        url = filter_mod.norm_url(c["url"])
        if not url or url in seen_urls: continue
        domain = url.split("/")[2].lower() if "://" in url else ""
        if domain in filter_mod.EXCLUDE_DOMAINS or "/rss" in url or url.rstrip("/").endswith(".rss"): continue
        path = url.split("://", 1)[-1]; path = path.split("/", 1)[1] if "/" in path else ""
        if not path: continue
        title = c.get("title", "").strip()
        if domain in ("kk.org", "www.kk.org"):
            p = url.lower().rstrip("/"); is_t = "/thetechnium/" in p and not p.endswith("/thetechnium"); is_c = "/cool-tools/" in p and not p.endswith("/cool-tools")
            if not ("weekly links" in title.lower() or is_t or is_c): continue
        if domain == "www.theguardian.com" and title.endswith("| The Guardian"): continue
        if domain in ("colossus.com", "www.colossus.com"):
            seg = [x for x in url.lower().rstrip("/").split("://", 1)[-1].split("/") if x]
            if len(seg) <= 1 or seg[1] in ("about-us", "shop", "subscribe", "contact") or (seg[1] == "mag" and len(seg) <= 2) or (seg[1] == "series" and len(seg) <= 3): continue
        is_hn = bool(c.get("hn_points"))
        if is_hn:
            if hn_count >= MAX_HN: continue
            hn_count += 1
        content = re.sub(r"\s+", " ", c.get("content", "")).strip()
        if wc(content) < filter_mod.MIN_WORDS and not is_hn:
            text = fetch_article_text(url, timeout=12)
            if not text:
                continue
            content = text
            log("国内源预抓正文成功: %s (%d字)" % (title[:40], wc(content)))
            if wc(content) < filter_mod.MIN_WORDS:
                continue
        pub, verified, month_only = ((c.get("publish_date"), True, False) if is_hn and c.get("publish_date") else filter_mod.extract_publish_date(c))
        if pub and pub > today_s: continue
        mf = re.search(r"newsDetail_forward_(\d{6,})", url)
        if mf and int(mf.group(1)) < 33600000: continue
        outside = 0
        if pub and not is_hn:
            if month_only:
                if (pub[:4], pub[5:7]) < (window_start_s[:4], window_start_s[5:7]): continue
            elif pub < window_start_s:
                try: outside_days = (window_start - datetime.strptime(pub, "%Y-%m-%d").date()).days
                except Exception: outside_days = 999
                if outside_days > 7: continue
                outside = outside_days
        if not verified and wc(content) < filter_mod.MIN_WORDS_ABSENT: continue
        hard, reasons = filter_mod.ai_watermark_check(content)
        if hard:
            log("水文硬剔除: %s（%s）" % (title[:40], "，".join(reasons))); continue
        kept.append({"title": title[:200], "url": url, "domain": domain, "direction": c.get("direction"), "is_preferred": bool(c.get("is_preferred")) or bool(pref_dir and c.get("direction") == pref_dir), "is_explore": bool(c.get("is_explore")), "publish_date": pub, "date_verified": verified or bool(pub), "window_outside_days": outside, "hn_points": c.get("hn_points"), "word_count": wc(content), "watermark_suspect": bool(reasons), "watermark_reasons": reasons, "content": content[:filter_mod.CONTENT_LIMIT], "source_key": c.get("source_key"), "source_label": c.get("source_label")})
        seen_urls.add(url)
    return kept

def rank_candidates(kept, explore_dir):
    def rank_key(x):
        if x["date_verified"] and x["publish_date"] and not x["window_outside_days"]: return (3, x["hn_points"] or 0)
        if x["date_verified"] and x["publish_date"] and x["window_outside_days"]: return (2, -x["window_outside_days"])
        if x["hn_points"]: return (1, x["hn_points"])
        return (0, 0)
    kept.sort(key=rank_key, reverse=True)
    non_explore, explore = [x for x in kept if not x["is_explore"]], [x for x in kept if x["is_explore"]]
    kept = non_explore[:MAX_CANDIDATES - min(len(explore), likes_mod.MAX_EXPLORE_SEATS)] + explore[:likes_mod.MAX_EXPLORE_SEATS]
    kept = kept[:MAX_CANDIDATES]
    if sum(1 for x in kept if x["is_explore"]): log("探索候选保底 %d 席（方向 %s）" % (sum(1 for x in kept if x["is_explore"]), explore_dir))
    log("过滤后候选 %d 篇" % len(kept))
    return kept
