import json
import os
import re
import sys
import tempfile

from . import filter as filter_mod
from .retrieve import anysearch_http_batch, chunked, fetch_texts_parallel, parse_search_markdown, run_cli, wc
from .window import log
from . import profile as profile_mod

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path: sys.path.insert(0, SCRIPTS_DIR)
import likes as likes_mod

MAX_CANDIDATES, MAX_HN, HN_FETCH_LIMIT = 15, 8, 12
MAX_PER_DIRECTION = 4          # 主题多样性：单方向（轨交除外）在池内的上限
EXPLORE_FALLBACK_DIRECTIONS = ("生活", "健康")  # 有合格探索候选时的保底方向
RAIL_DIRECTION = "轨道交通"    # 轨交优先
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

def search_batch_with_tags(name, queries, candidates, src_stats, pref_dir, src_map=None, budget=None, tmp_dir=None):
    """src_map: 若传入，为每条结果打 source_key/source_label 并按源归组（未推荐源速览用）。
    查询临时文件走 runtime scratch（tempfile，可指定目录），finally 清理，不落 /tmp 固定名。"""
    total = 0
    for bi, sub in enumerate(chunked(queries, 5)):
        if budget and budget.expired():
            log("来源 %s 搜索提前停止（总预算耗尽）" % name)
            break
        sub_cli = [{"query": q["query"], "max_results": q.get("max_results", 8)} for q in sub]
        fd, qfile = tempfile.mkstemp(prefix="brief_queries_%s_" % name, suffix=".json",
                                     dir=tmp_dir or tempfile.gettempdir())
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(sub_cli, f, ensure_ascii=False)
            md = run_cli(["batch_search", "--queries", "@" + qfile], budget=budget)
        finally:
            try:
                os.unlink(qfile)
            except OSError:
                pass
        batch = parse_search_markdown(md) if md else (anysearch_http_batch(sub_cli, budget=budget) or [])
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

def _cheap_precheck(candidates, dedup):
    """第一阶段：不联网的廉价检查（URL 合法性/去重/黑名单/栏目页/来源特异规则）。"""
    pre, seen_urls = [], set(dedup)
    for c in candidates:
        url = filter_mod.norm_url(c.get("url", ""))
        if not url:
            if c.get("url"):
                log("非法 URL 拒收: %s" % str(c.get("url"))[:80])
            continue
        if url in seen_urls:
            continue
        domain = url.split("/")[2].lower() if "://" in url else ""
        if domain in filter_mod.EXCLUDE_DOMAINS or "/rss" in url or url.rstrip("/").endswith(".rss"): continue
        if domain == "news.ycombinator.com": continue  # HN 讨论页非文章，不入池
        if filter_mod.is_aggregate_url(url):
            log("聚合页（首页/频道/列表/导航）不入池: %s" % url)
            continue
        title = c.get("title", "").strip()
        if domain in ("kk.org", "www.kk.org"):
            p = url.lower().rstrip("/"); is_t = "/thetechnium/" in p and not p.endswith("/thetechnium"); is_c = "/cool-tools/" in p and not p.endswith("/cool-tools")
            if not ("weekly links" in title.lower() or is_t or is_c): continue
        if domain == "www.theguardian.com" and title.endswith("| The Guardian"): continue
        if domain in ("colossus.com", "www.colossus.com"):
            seg = [x for x in url.lower().rstrip("/").split("://", 1)[-1].split("/") if x]
            if len(seg) <= 1 or seg[1] in ("about-us", "shop", "subscribe", "contact") or (seg[1] == "mag" and len(seg) <= 2) or (seg[1] == "series" and len(seg) <= 3): continue
        mf = re.search(r"newsDetail_forward_(\d{6,})", url)
        if mf and int(mf.group(1)) < 33600000: continue
        seen_urls.add(url)
        pre.append({"c": c, "url": url, "domain": domain, "title": title})
    return pre


def filter_candidates(candidates, dedup, window_start, today, pref_dir, budget=None):
    """第二阶段：正文 + 精确日期证据准入（所有来源同一标准，HN 不豁免）。

    入推荐池硬条件（任一不满足即拒收，不静默放行）：
      - 正文 ≥ MIN_WORDS（不足则联网抓正文；抓到仍不足 → 拒收）
      - 发布日期证据精确到日（月精度/未知日期 → 拒收；HN 热议日期不算证据）
      - 日期不得在未来，且必须在采集窗口内（窗口外不进深度/TLDR/快读，直接不入池）
    """
    today_s = today.strftime("%Y-%m-%d")
    window_start_s = window_start.strftime("%Y-%m-%d")
    pre = _cheap_precheck(candidates, dedup)

    # 有界并发统一补抓正文（局部故障隔离 + 总预算）；with_metadata 保留 datePublished/JSON-LD 证据
    need = [x for x in pre if wc(re.sub(r"\s+", " ", x["c"].get("content", "")).strip()) < filter_mod.MIN_WORDS]
    arts = fetch_texts_parallel([x["url"] for x in need], timeout=12, budget=budget,
                                with_metadata=True) if need else {}

    kept = []
    for x in pre:
        c, url, domain, title = x["c"], x["url"], x["domain"], x["title"]
        is_hn = bool(c.get("hn_points"))
        content = re.sub(r"\s+", " ", c.get("content", "")).strip()
        metadata = c.get("metadata") or {}
        if wc(content) < filter_mod.MIN_WORDS:
            art = arts.get(url)
            # 兼容旧 mock/调用：值可以是正文字符串或 {"text","metadata"} 结构
            if isinstance(art, dict):
                text, metadata = art.get("text"), art.get("metadata") or metadata
            else:
                text = art
            if not text:
                log("正文不足且抓取失败，拒收: %s" % title[:40])
                continue
            content = text
            if wc(content) < filter_mod.MIN_WORDS:
                log("正文不足 %d 字，拒收: %s" % (wc(content), title[:40]))
                continue
        # 日期证据：metadata/署名发布时间优先，URL/标题日精度兜底；HN 只提供 hn_discussed_at 供交叉核对
        ev = filter_mod.extract_date_evidence({"url": url, "title": title, "content": content,
                                               "metadata": metadata})
        pub = ev["publish_date"]
        if not pub:
            log("无发布日期证据，拒收: %s" % title[:40])
            continue
        if ev["precision"] != "day":
            log("日期仅精确到月，拒收: %s" % title[:40])
            continue
        if pub > today_s:
            log("未来日期 %s，拒收: %s" % (pub, title[:40]))
            continue
        if pub < window_start_s:
            log("窗口外 %s（窗口起点 %s），拒收: %s" % (pub, window_start_s, title[:40]))
            continue
        if is_hn and c.get("hn_discussed_at") and pub > c["hn_discussed_at"]:
            # 发布日期晚于热议日期 → 日期证据自相矛盾，拒收
            log("发布日期 %s 晚于 HN 热议日 %s，证据矛盾，拒收: %s" % (pub, c["hn_discussed_at"], title[:40]))
            continue
        hard, reasons = filter_mod.ai_watermark_check(content)
        if hard:
            log("水文硬剔除: %s（%s）" % (title[:40], "，".join(reasons))); continue
        kept.append({"title": title[:200], "url": url, "domain": domain, "direction": c.get("direction"), "is_preferred": bool(c.get("is_preferred")) or bool(pref_dir and c.get("direction") == pref_dir), "is_explore": bool(c.get("is_explore")), "publish_date": pub, "date_verified": True, "date_source": ev["date_source"], "date_evidence": ev["evidence"], "date_precision": "day", "window_outside_days": 0, "hn_points": c.get("hn_points"), "hn_discussed_at": c.get("hn_discussed_at"), "word_count": wc(content), "watermark_suspect": bool(reasons), "watermark_reasons": reasons, "content": content[:filter_mod.CONTENT_LIMIT], "source_key": c.get("source_key"), "source_label": c.get("source_label")})
    return kept

def rank_candidates(kept, explore_dir):
    """排序 + 席位分配。

    规则：
      - 轨交优先；其余按发布日期降序；HN 积分只在同质量（全部已过准入）同日内做次序参考，
        不得凭积分越过日期/方向层级，也不得占满池（MAX_HN 上限）。
      - 主题多样性：单方向（轨交除外）≤ MAX_PER_DIRECTION。
      - 生活/健康保底：全池有合格生活/健康条目时各保留至少 1 席（不限探索候选），
        保底席计入主题上限与 HN 上限，防止无方向 HN 条目挤占。
      - 探索保底：有合格探索候选时预留 ≤MAX_EXPLORE_SEATS 席，生活/健康方向优先。
    """
    def sort_key(x):
        return (1 if x["direction"] == RAIL_DIRECTION else 0,
                x["publish_date"] or "",
                x["hn_points"] or 0)
    kept = sorted(kept, key=sort_key, reverse=True)

    seats, seat_urls = [], set()

    def add_seat(x):
        if x["url"] not in seat_urls:
            seats.append(x)
            seat_urls.add(x["url"])

    # 生活/健康保底（非探索也保底）：全池有合格者各保 1 席
    for d in EXPLORE_FALLBACK_DIRECTIONS:
        x = next((x for x in kept if x["direction"] == d), None)
        if x:
            add_seat(x)

    # 探索保底 ≤MAX_EXPLORE_SEATS 席（生活/健康优先，可与上面重叠去重）
    explore_pool = sorted((x for x in kept if x["is_explore"]),
                          key=lambda x: (1 if x["direction"] in EXPLORE_FALLBACK_DIRECTIONS else 0,
                                         x["publish_date"] or "",
                                         x["hn_points"] or 0),
                          reverse=True)
    for x in explore_pool:
        if sum(1 for s in seats if s["is_explore"]) >= likes_mod.MAX_EXPLORE_SEATS:
            break
        add_seat(x)

    # 保底席计入主题与 HN 上限
    dir_count, hn_count = {}, 0
    for s in seats:
        d = s["direction"]
        dir_count[d] = dir_count.get(d, 0) + 1
        if s.get("hn_points"):
            hn_count += 1

    selected = []
    quota = MAX_CANDIDATES - len(seats)
    for x in kept:
        if x["url"] in seat_urls or x["is_explore"]:
            continue
        if len(selected) >= quota:
            break
        d = x["direction"]
        if d != RAIL_DIRECTION and dir_count.get(d, 0) >= MAX_PER_DIRECTION:
            continue
        if x.get("hn_points") and hn_count >= MAX_HN:
            continue
        selected.append(x)
        dir_count[d] = dir_count.get(d, 0) + 1
        if x.get("hn_points"):
            hn_count += 1
    kept = selected + seats
    life_health = [d for d in EXPLORE_FALLBACK_DIRECTIONS
                   if any(s["direction"] == d for s in seats)]
    if life_health:
        log("生活/健康保底 %s（计入主题与 HN 上限）" % "/".join(life_health))
    if any(s["is_explore"] for s in seats):
        log("探索候选保底 %d 席（方向 %s，生活/健康优先）"
            % (sum(1 for s in seats if s["is_explore"]), explore_dir))
    log("过滤后候选 %d 篇" % len(kept))
    return kept
