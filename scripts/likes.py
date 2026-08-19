#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
点赞偏好与探索模块（简报防信息茧房）
======================================
职责：
  1. 读写 data/likes.json（用户对简报文章的点赞记录）
  2. 方向推断：URL 域名 + 标题关键词 → 6 大方向之一
  3. 偏好方向：点赞计数最多者（并列取最近点赞）
  4. 探索方向：空白/低关注方向优先，样本不足时回退日期轮换
  5. 生成偏好查询（≤2 条）与探索查询（≤2 条），注入 collect_brief.py

数据文件：
  <DailyBrief>/data/likes.json
    {"likes": [{"url","title","direction","liked_at"}], "updated_at": "..."}

防茧房设计（强哥 2026-08-13 定）：
  - 偏好方向最多 2 条定向查询（个性化但不垄断候选池）
  - 探索优先投空白/低关注方向；样本不足时每日轮换回退
  - 候选池为探索内容保底 ≤2 席；validate 硬校验探索条目必须进简报
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

BRIEF_DIR = "/Users/qqiang/AI project/05-日常工具/DailyBrief"
LIKES_FILE = os.path.join(BRIEF_DIR, "data", "likes.json")
OUTPUT_DIR = os.path.join(BRIEF_DIR, "output")
TZ = timezone(timedelta(hours=8))

# 6 大方向（与 today-brief skill 一致）
DIRECTIONS = ["AI", "科技", "商业", "生活", "健康", "轨道交通"]

# 防茧房硬约束
MAX_PREF_QUERIES = 2    # 偏好方向最多 2 条定向查询
MAX_EXPLORE_SEATS = 2   # 候选池为探索内容保底 ≤2 席
MAX_PREF_DEEP = 8       # 深度总结区偏好命中条目上限（10 篇的 80%，留探索位）

# 偏好样本门槛（2026-08-16 加）：样本足够多才启用偏好优先采集
MIN_LIKES_FOR_PREFERENCE = 10  # 点赞总数下限（不足不认定偏好方向）
MIN_PREF_SHARE = 0.40          # top 方向占比下限（方向不集中不认定）

# 域名 → 方向（优先于关键词）
DOMAIN_DIR = {
    "qbitai.com": "AI", "jiqizhixin.com": "AI", "techcrunch.com": "AI",
    "openrouter.ai": "AI", "theinformation.com": "AI",
    "36kr.com": "商业", "huxiu.com": "商业",
    "zhihu.com": "科技", "kk.org": "科技", "theguardian.com": "科技",
    "reddit.com": "科技", "news.ycombinator.com": "科技",
    "tailscale.com": "科技", "ngrok.com": "科技", "blog.florianherrengt.com": "科技",
    "thepaper.cn": "生活", "lemonde.fr": "生活", "effort.news": "生活",
    "thewalrus.ca": "生活", "ft.com": "商业",
    "jiandanxinli.com": "健康",
    "chinametrorail.com": "轨道交通", "rail-transit.com": "轨道交通",
}

# 关键词 → 方向（打分取最高；小写匹配）
DIR_KEYWORDS = {
    "AI": ["人工智能", "大模型", "深度学习", "机器学习", "神经网络", "智能体",
           "生成式", "agent", "llm", "gpt", "transformer", "ai"],
    "科技": ["编程", "硬件", "芯片", "开源", "软件", "工程", "开发者", "程序员",
             "数据库", "云计算", "代码", "sqlite", "科技"],
    "商业": ["商业", "经济", "市场", "融资", "投资", "财报", "股价", "营销",
             "品牌", "巨头", "产业", "消费", "startup"],
    "生活": ["职场", "心理", "社会", "生活方式", "城市", "家庭", "教育",
             "文化", "婚姻", "育儿", "生活"],
    "健康": ["健康", "医学", "医疗", "情绪", "睡眠", "健身", "疾病", "营养",
             "焦虑", "抑郁", "therapy"],
    "轨道交通": ["轨道交通", "磁浮", "高铁", "高速铁路", "铁路", "城轨", "地铁",
                 "动车", "制动", "maglev", "railway", "train", "transit", "新幹線", "リニア"],
}

# 关键词提取停用词
STOPWORDS = {
    "深度", "分析", "最新", "今日", "一个", "我们", "他们", "这个", "那个",
    "什么", "如何", "为什么", "报道", "文章", "重磅", "突发", "发布", "上线",
    "来了", "时代", "背后", "未来", "and", "the", "for", "with", "from",
}

# 偏好方向查询模板（单域 site: 或纯关键词，遵守"禁止多域 OR"规则）
PREF_QUERY_TEMPLATES = {
    "AI": [
        {"query": "site:qbitai.com 大模型 发布 {month}", "max_results": 8},
        {"query": "site:jiqizhixin.com 大模型 深度 {month}", "max_results": 8},
    ],
    "科技": [
        {"query": "site:36kr.com 硬件 编程 开源 {month}", "max_results": 8},
        {"query": "site:zhihu.com 工程 技术 深度 {month}", "max_results": 8},
    ],
    "商业": [
        {"query": "site:huxiu.com 商业 经济 市场 {month}", "max_results": 8},
        {"query": "site:36kr.com 融资 公司 深度 {month}", "max_results": 8},
    ],
    "生活": [
        {"query": "site:thepaper.cn 深度 职场 社会 {month}", "max_results": 8},
        {"query": "site:zhihu.com 职场 心理 生活方式 深度", "max_results": 8},
    ],
    "健康": [
        {"query": "site:jiandanxinli.com 心理健康 情绪管理 {month}", "max_results": 6},
        {"query": "健康 医学 睡眠 最新研究 {month}", "max_results": 8},
    ],
    "轨道交通": [
        {"query": "磁浮 高速铁路 最新进展 {month} 深度", "max_results": 8},
        {"query": "site:thepaper.cn 轨道交通 磁浮 高铁", "max_results": 6},
    ],
}

# 探索方向查询模板（数据驱动查询不可用时的回退路径）
EXPLORE_QUERY_TEMPLATES = {
    "AI": {"query": "site:jiqizhixin.com 大模型 新进展 {month}", "max_results": 8},
    "科技": {"query": "site:36kr.com 硬件 开源 开发者 {month}", "max_results": 8},
    "商业": {"query": "site:huxiu.com 商业 公司 市场 {month}", "max_results": 8},
    "生活": {"query": "site:thepaper.cn 深度报道 社会 {month}", "max_results": 8},
    "健康": {"query": "site:jiandanxinli.com 心理健康 科普 {month}", "max_results": 6},
    "轨道交通": {"query": "高速铁路 磁浮 城市轨道 最新进展 {month}", "max_results": 8},
}


def norm_url(u):
    """URL 简化规范化（去 fragment/www/追踪参数），与 collect_brief.norm_url 一致语义"""
    if not u:
        return ""
    u = u.strip().strip(".,;:!?)]}\"'")
    if not u or not u.startswith("http"):
        return ""
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    u = re.sub(r"^https://www\.", "https://", u)
    u = u.split("#", 1)[0]
    if "?" in u:
        base, _, query = u.partition("?")
        keep = [p for p in query.split("&")
                if p and not any(p.startswith(t) for t in
                                 ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid",
                                  "ref_src", "ref_url", "cmpid", "spm", "from",
                                  "share_token", "share_source", "source"))]
        u = base + ("?" + "&".join(keep) if keep else "")
    return u.rstrip("/")


def load_likes(path=None):
    """读取点赞记录，返回 [{"url","title","direction","liked_at"}, ...]；无文件返回 []"""
    path = path or LIKES_FILE
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            likes = data.get("likes", [])
            if isinstance(likes, list):
                return [x for x in likes if isinstance(x, dict)]
        except Exception as e:
            print("[likes] 读点赞文件失败: %s" % e, file=__import__("sys").stderr)
    return []


def save_likes(likes, path=None):
    """写回点赞记录"""
    path = path or LIKES_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"likes": likes, "updated_at": datetime.now(TZ).isoformat()},
                  f, ensure_ascii=False, indent=2)


def infer_direction(url, title=""):
    """URL 域名优先，其次标题关键词打分；无法判断返回 None"""
    if url:
        host = url.split("/")[2].lower() if "://" in url else ""
        host = re.sub(r"^www\.", "", host)
        for dom, d in DOMAIN_DIR.items():
            if host == dom or host.endswith("." + dom):
                return d
    text = (title or "").lower()
    best, best_score = None, 0
    for d, kws in DIR_KEYWORDS.items():
        score = sum(1 for kw in kws if kw.lower() in text)
        if score > best_score:
            best, best_score = d, score
    return best


def preference_direction(likes):
    """点赞最多的方向；样本不足或方向不集中返回 None（不启用偏好优先采集）。

    门槛（2026-08-16 起）：点赞总数 ≥ MIN_LIKES_FOR_PREFERENCE(10)
    且 top 方向占比 ≥ MIN_PREF_SHARE(40%)。并列取最近点赞者。
    """
    counts, latest = _count_directions(likes)
    if not counts:
        return None
    total = sum(counts.values())
    if total < MIN_LIKES_FOR_PREFERENCE:
        return None
    best = max(counts, key=lambda d: counts[d])
    if counts[best] / total < MIN_PREF_SHARE:
        return None
    ties = [d for d, c in counts.items() if c == counts[best]]
    if len(ties) > 1:
        return max(ties, key=lambda d: latest.get(d, ""))
    return best


def _count_directions(likes):
    """统计各方向点赞数与最近点赞时间"""
    counts, latest = {}, {}
    for x in likes:
        d = x.get("direction")
        if not d:
            continue
        counts[d] = counts.get(d, 0) + 1
        latest[d] = x.get("liked_at", "")
    return counts, latest


def pref_progress(likes):
    """偏好进度（供 stats/确认页显示）：
    返回 (pref_dir, total, min_total, top_dir, top_count, top_share)
    样本不足或方向不集中时 pref_dir=None，调用方据此提示还差多少。
    """
    counts, _ = _count_directions(likes)
    total = sum(counts.values())
    if not counts:
        return None, total, MIN_LIKES_FOR_PREFERENCE, None, 0, 0.0
    top = max(counts, key=lambda d: counts[d])
    share = counts[top] / total
    pref = preference_direction(likes)
    return pref, total, MIN_LIKES_FOR_PREFERENCE, top, counts[top], share


def explore_direction(pref_dir, now=None):
    """探索方向：从非偏好方向按日期轮换（排除偏好后 5 方向循环）"""
    now = now or datetime.now(TZ)
    others = [d for d in DIRECTIONS if d != pref_dir]
    if not others:
        return DIRECTIONS[0]
    return others[now.toordinal() % len(others)]


def extract_keywords(titles, top_n=3):
    """从标题列表提取 top 关键词（连续中文词 ≥2 字 + 英文词 ≥3 字母，过滤停用词）"""
    words = []
    for t in titles:
        t = t or ""
        words += re.findall(r"[\u4e00-\u9fff]{2,8}", t)
        words += [w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", t)]
    freq = {}
    for w in words:
        if w.lower() in STOPWORDS:
            continue
        key = w.lower()
        freq[key] = freq.get(key, 0) + 1
    ranked = sorted(freq.items(), key=lambda x: (-x[1], len(x[0])))
    return [w for w, _ in ranked[:top_n]]


def _month_cn(now):
    return "%d月" % now.month


def build_pref_queries(pref_dir, likes, now=None):
    """偏好方向定向查询（≤2 条）。命中关键词的查询加权排前。"""
    now = now or datetime.now(TZ)
    month = _month_cn(now)
    templates = PREF_QUERY_TEMPLATES.get(pref_dir, [])
    if not templates:
        return []
    kws = extract_keywords([x.get("title", "") for x in likes], top_n=2)
    queries = []
    for tpl in templates[:MAX_PREF_QUERIES]:
        q = dict(tpl)
        q["query"] = q["query"].format(month=month)
        if kws:
            # 第一条查询附加点赞关键词（个性化），第二条保持广度
            if queries == []:
                q["query"] = q["query"] + " " + " ".join(kws)
        q["direction"] = pref_dir
        q["preferred"] = True
        q["explore"] = False
        queries.append(q)
    return queries


def build_explore_query(explore_dir, now=None):
    """探索方向查询（1 条）"""
    now = now or datetime.now(TZ)
    tpl = EXPLORE_QUERY_TEMPLATES.get(explore_dir)
    if not tpl:
        return None
    q = dict(tpl)
    q["query"] = q["query"].format(month=_month_cn(now))
    q["direction"] = explore_dir
    q["preferred"] = False
    q["explore"] = True
    return q


def _load_profile_module():
    """延迟加载 profile 模块，避免脚本入口与 modules 之间形成顶层循环导入。"""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from modules import profile as profile_mod
        return profile_mod
    except Exception:
        return None


def build_explore_queries(pref_dir, likes, profile=None, now=None):
    """数据驱动探索查询（≤2 条）；画像不足时回退原有日期轮换。"""
    now = now or datetime.now(TZ)
    profile_mod = _load_profile_module()
    if profile_mod is not None:
        if profile is None:
            try:
                profile = profile_mod.compute_profile(likes)
            except Exception:
                profile = None
        if profile is not None:
            try:
                queries = profile_mod.breakout_queries(
                    profile,
                    now,
                    max_q=MAX_EXPLORE_SEATS,
                    thin_threshold=profile_mod.THIN_DIRECTION_SHARE,
                )
                if queries:
                    return queries[:MAX_EXPLORE_SEATS]
            except Exception:
                pass

    fallback_dir = explore_direction(pref_dir, now)
    eq = build_explore_query(fallback_dir, now)
    return [eq] if eq else []


def build_extra_queries(now=None):
    """组合偏好查询 + 探索查询。返回 (extra_queries, pref_dir, explore_dir)"""
    now = now or datetime.now(TZ)
    likes = load_likes()
    pref_dir = preference_direction(likes)
    extra = []
    if pref_dir:
        extra.extend(build_pref_queries(pref_dir, likes, now))
    explore_queries = build_explore_queries(pref_dir, likes, None, now)
    extra.extend(explore_queries)
    explore_dir = (
        explore_queries[0]["direction"]
        if explore_queries else explore_direction(pref_dir, now)
    )
    return extra, pref_dir, explore_dir


if __name__ == "__main__":
    # 自测
    import sys
    print("DIRECTIONS:", DIRECTIONS)
    print("pref(空):", preference_direction([]))
    print("infer(磁浮):", infer_direction("https://www.chinametrorail.com/abc", "磁浮列车最新进展"))
    print("infer(qbitai):", infer_direction("https://www.qbitai.com/2026/08/123.html", "大模型Agent发布"))
    extra, pref, expl = build_extra_queries()
    print("extra:", json.dumps(extra, ensure_ascii=False, indent=1))
    print("pref_dir:", pref, "| explore_dir:", expl)
