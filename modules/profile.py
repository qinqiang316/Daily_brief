# -*- coding: utf-8 -*-
"""A2 细粒度偏好画像（独立新增模块）。

把 likes.json 的 6 大方向点赞进一步聚合为子话题频次、关键词权重与方向分布，
并输出可被采集查询使用的定向查询。本模块不改写 likes.py / collect_brief.py
等现有脚本，只为 A3 反方向突破提供更细的偏好信号。
"""

import re
import os
import sys
from collections import Counter
from datetime import datetime

SCRIPTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts")
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
import likes as likes_mod

# 偏好画像的样本门槛（与 likes.py 保持一致）
MIN_QUERY_LIKES = likes_mod.MIN_LIKES_FOR_PREFERENCE
MIN_TOPIC_COUNT = 2
MAX_QUERY_TERMS = 6
MIN_BREAKOUT_LIKES = 5
THIN_DIRECTION_SHARE = 0.05

# 子话题词表：方向 -> 子话题 -> 关键词。
# 使用“主题词优先于泛词”的思路，避免把“AI”等方向级泛词直接当成细粒度信号。
TOPIC_KEYWORDS = {
    "AI": {
        "大模型推理": (
            "推理优化", "推理成本", "推理效率", "推理引擎", "长上下文",
            "token成本", "inference", "vllm",
        ),
        "芯片算力": (
            "芯片", "算力", "gpu", "gpu集群", "hbm", "cuda", "nvidia",
            "asic", "算力中心",
        ),
        "Agent": (
            "agent", "智能体", "多智能体", "mcp", "工具调用",
            "agentic", "ai agent",
        ),
        "开源模型": (
            "开源模型", "open weights", "llama", "deepseek", "qwen",
            "mistral", "开源权重",
        ),
        "AI安全": (
            "ai安全", "模型安全", "对齐", "水印", "越狱", "幻觉",
            "watermark", "hallucination", "jailbreak",
        ),
    },
    "科技": {
        "编程开发": (
            "编程", "代码", "软件开发", "debug", "git", "api", "sqlite",
            "developer",
        ),
        "硬件芯片": (
            "硬件", "芯片", "处理器", "服务器", "晶体管", "电路",
            "hardware", "chip",
        ),
        "工程实践": (
            "工程实践", "架构设计", "系统设计", "可靠性", "运维", "性能优化",
            "sre", "observability",
        ),
        "开源生态": (
            "开源", "open source", "github", "oss", "许可证",
            "open source license",
        ),
        "开发者工具": (
            "开发者工具", "开发工具", "ide", "cli", "命令行", "程序员",
            "devtools",
        ),
    },
    "商业": {
        "公司动态": (
            "公司", "巨头", "裁员", "财报", "管理层", "创业公司",
            "company", "earnings",
        ),
        "资本市场": (
            "资本市场", "股市", "股票", "股价", "上市", "ipo", "债券",
            "央行", "汇率", "银行", "bank", "bond",
        ),
        "投资融资": (
            "投资", "融资", "风投", "估值", "vc", "创业融资", "投资者",
            "startup funding",
        ),
        "产业分析": (
            "产业分析", "行业分析", "赛道", "产业链", "市场分析", "格局",
            "商业模式", "industry analysis",
        ),
    },
    "生活": {
        "职场发展": (
            "职场", "职业发展", "个人成长", "工作方式", "一人公司",
            "自由职业", "效率", "career",
        ),
        "心理情绪": (
            "心理", "情绪", "焦虑", "压力", "抑郁", "心理健康",
            "mental health", "therapy",
        ),
        "生活方式": (
            "生活方式", "家庭", "教育", "育儿", "极简主义", "城市生活",
            "lifestyle",
        ),
        "社会观察": (
            "社会", "文化", "公共政策", "人口", "消费社会", "社会观察",
            "society",
        ),
    },
    "健康": {
        "医学研究": (
            "医学", "临床", "疾病", "治疗", "药物", "疫苗", "循证",
            "医学研究", "clinical",
        ),
        "心理健康": (
            "心理健康", "心理治疗", "情绪管理", "焦虑", "抑郁", "睡眠",
            "精神科", "psychiatry",
        ),
        "科学研究": (
            "科学研究", "研究", "论文", "数据", "随机对照", "meta分析",
            "学术", "同行评审", "rct",
        ),
        "运动营养": (
            "运动", "健身", "营养", "饮食", "代谢", "力量训练",
            "nutrition",
        ),
    },
    "轨道交通": {
        "磁浮技术": (
            "磁浮", "磁悬浮", "超导磁浮", "常导磁浮", "maglev",
            "scmaglev", "リニア",
        ),
        "高速铁路": (
            "高速铁路", "高铁", "动车组", "新干线", "high speed rail",
            "hsr", "时速350公里", "铁路",
        ),
        "城市轨道": (
            "城市轨道", "城轨", "地铁", "轻轨", "市域铁路", "urban rail",
            "metro", "transit",
        ),
        "制动系统": (
            "制动", "刹车", "制动系统", "制动力", "再生制动", "电制动",
            "摩擦制动", "空气制动", "brake",
        ),
        "车辆供应商": (
            "车辆", "转向架", "牵引系统", "信号系统", "供应商", "中车",
            "crrc", "siemens", "alstom", "bombardier",
        ),
    },
}

_TOPIC_DIRECTION = {}
_TOPIC_ORDER = []
for _direction, _topics in TOPIC_KEYWORDS.items():
    for _topic in _topics:
        _TOPIC_DIRECTION[_topic] = _direction
        _TOPIC_ORDER.append(_topic)


def _contains_keyword(text, keyword):
    """关键词匹配：中文按子串；英文/数字按词边界，避免域名内误命中。"""
    keyword = keyword.lower()
    if re.search(r"[a-z0-9]", keyword):
        return bool(re.search(
            r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(keyword),
            text,
        ))
    return keyword in text


def extract_topics(url="", title="", content=""):
    """返回该文命中的细分子话题列表，按 TOPIC_KEYWORDS 做小写子串匹配。"""
    text = " ".join(filter(None, (str(url or ""), str(title or ""), str(content or ""))))
    text = re.sub(r"\s+", " ", text).lower()
    hits = []
    for topics in TOPIC_KEYWORDS.values():
        for topic, keywords in topics.items():
            if any(_contains_keyword(text, kw) for kw in keywords):
                hits.append(topic)
    return hits


def compute_profile(likes=None, path=None, min_top_count=MIN_TOPIC_COUNT):
    """聚合点赞记录为细粒度画像。

    likes 为 None 时调用 likes.load_likes(path) 读取真实数据，避免调用方必须传参。
    """
    if likes is None:
        likes = likes_mod.load_likes(path)
    total = len(likes)

    direction_counts = Counter(
        x.get("direction") for x in likes if isinstance(x, dict)
    )
    all_directions = list(likes_mod.DIRECTIONS)
    for d in direction_counts:
        if d and d not in all_directions:
            all_directions.append(d)
    by_direction = {d: direction_counts.get(d, 0) for d in all_directions}

    topic_counts = Counter()
    keyword_counts = Counter()
    for like in likes:
        if not isinstance(like, dict):
            continue
        text = " ".join(filter(None, (
            str(like.get("url", "")),
            str(like.get("title", "")),
            str(like.get("content", "")),
        )))
        text = re.sub(r"\s+", " ", text).lower()
        for topics in TOPIC_KEYWORDS.values():
            for topic, keywords in topics.items():
                if not any(_contains_keyword(text, kw) for kw in keywords):
                    continue
                topic_counts[topic] += 1
                for kw in keywords:
                    if _contains_keyword(text, kw):
                        keyword_counts[kw] += 1

    top_topics = [
        (topic, count) for topic, count in topic_counts.items()
        if count >= min_top_count
    ]
    topic_rank = {topic: i for i, topic in enumerate(_TOPIC_ORDER)}
    top_topics.sort(key=lambda x: (-x[1], topic_rank.get(x[0], len(_TOPIC_ORDER))))

    keywords = dict(
        sorted(keyword_counts.items(), key=lambda x: (-x[1], x[0]))
    )

    return {
        "total": total,
        "by_direction": by_direction,
        "by_topic": dict(topic_counts),
        "top_direction": likes_mod.preference_direction(likes),
        "top_topics": top_topics,
        "keywords": keywords,
    }


def _month_cn(now):
    """与 likes.py 相同的中文月份表达。"""
    now = now or datetime.now(likes_mod.TZ)
    return "%d月" % now.month


def _query_terms_for_topic(profile, topic):
    """为单个子话题选择高权关键词，优先中文，避免与主题词重复。"""
    topic_keywords = []
    for topics in TOPIC_KEYWORDS.values():
        if topic in topics:
            topic_keywords = topics[topic]
            break
    weighted = []
    for kw in topic_keywords:
        weight = profile.get("keywords", {}).get(kw, 0)
        if weight > 0:
            weighted.append((weight, kw))
    weighted.sort(key=lambda x: (-x[0], 0 if re.search(r"[\u4e00-\u9fff]", x[1]) else 1, x[1]))
    return [kw for _, kw in weighted[:3]]


def pref_queries_from_profile(profile, now=None, max_q=2):
    """依据 top_topics / keywords 生成 ≤2 条偏好定向查询。

    返回结构与 collect_brief/rank 使用的查询列表同构，并带 preferred/explore
    标记，便于直接接入现有采集流程。
    """
    if not profile or profile.get("total", 0) < MIN_QUERY_LIKES:
        return []
    top_topics = profile.get("top_topics") or []
    if not top_topics:
        return []

    queries = []
    for topic, _ in top_topics[:max_q]:
        terms = [topic] + _query_terms_for_topic(profile, topic) + [_month_cn(now)]
        terms = list(dict.fromkeys(terms))[:MAX_QUERY_TERMS]
        queries.append({
            "query": " ".join(terms),
            "max_results": 8,
            "direction": _TOPIC_DIRECTION.get(topic),
            "preferred": True,
            "explore": False,
        })
    return queries


def hole_directions(profile):
    """返回空白/低关注方向：by_direction 中 count == 0 的 6 大方向。"""
    if not profile:
        return list(likes_mod.DIRECTIONS)
    by_direction = profile.get("by_direction", {})
    return [d for d in likes_mod.DIRECTIONS if by_direction.get(d, 0) == 0]


def proto_hole_directions(profile, min_count=0, thin_threshold=None):
    """返回空白/低关注方向。

    count 低于 min_count 的方向必选；thin_threshold 不为 None 时，
    count/total 低于该占比的方向也会被列入低关注探索候选。
    """
    if not profile:
        return list(likes_mod.DIRECTIONS)
    by_direction = profile.get("by_direction", {})
    total = profile.get("total", 0)
    if total <= 0:
        return list(likes_mod.DIRECTIONS)

    candidates = []
    for d in likes_mod.DIRECTIONS:
        count = by_direction.get(d, 0)
        if count < min_count:
            if d not in candidates:
                candidates.append(d)
        elif (thin_threshold is not None
              and count < total * thin_threshold
              and d not in candidates):
            candidates.append(d)
    return candidates


def biggest_bubble_direction(profile):
    """识别当前占比最高、投入最集中的茧房方向。返回 (direction, share)。"""
    if not profile or profile.get("total", 0) <= 0:
        return None, 0.0
    by_direction = profile.get("by_direction", {})
    total = float(profile.get("total", 0))
    counts = [(d, by_direction.get(d, 0)) for d in likes_mod.DIRECTIONS]
    direction, count = max(counts, key=lambda x: (x[1], -counts.index(x)))
    return direction, count / total


# 对立/陌生角度查询词库：反方不是骂，而是批判性审视、成本质疑与可行性挑战。
OPPOSITION_QUERY_TEMPLATES = {
    "AI": [
        "AI 泡沫 反思 局限 批判 {month}",
        "大模型 过度炒作 失败案例 成本 质疑 {month}",
    ],
    "科技": [
        "科技 平台 权力 监管 批判 {month}",
        "程序员 行业 倦怠 开源 可持续 质疑 {month}",
    ],
    "商业": [
        "商业伦理 反消费 批判 {month}",
        "公司 增长 神话 反思 泡沫 质疑 {month}",
    ],
    "生活": [
        "生活方式 焦虑 贩卖 反思 批判 {month}",
        "职场 内卷 倦怠 反思 另类路径 {month}",
    ],
    "健康": [
        "健康 过度医疗 营销 循证 质疑 {month}",
        "养生 伪科学 医学 证据 批判性审视 {month}",
    ],
    "轨道交通": [
        "磁浮 争议 成本 质疑 可行性 {month}",
        "高铁 债务 运营 亏损 反思 {month}",
    ],
}

# 空白/低关注方向的普通探索模板，用于把预算投到用户尚未积累的方向。
HOLE_QUERY_TEMPLATES = {
    "AI": [
        "AI 大模型 新视角 反思 {month}",
        "AI 跨行业 应用 争议 深度 {month}",
    ],
    "科技": [
        "科技 开放硬件 开发者 新趋势 {month}",
        "科技 工程实践 反思 深度 {month}",
    ],
    "商业": [
        "商业 产业 商业模式 新视角 {month}",
        "商业 公司 市场 深度 {month}",
    ],
    "生活": [
        "生活 心理健康 职场 深度 {month}",
        "生活 社会 公共政策 生活方式 新视角 {month}",
    ],
    "健康": [
        "健康 医学 循证 最新研究 {month}",
        "健康 情绪 睡眠 运动 深度 {month}",
    ],
    "轨道交通": [
        "轨道交通 磁浮 城市轨道 深度 {month}",
        "高铁 铁路 技术 成本 争议 {month}",
    ],
}


def _explore_reason(profile, now=None):
    """返回一行探索理由，供 format_status 的增强状态行使用。"""
    if not profile or profile.get("total", 0) < MIN_BREAKOUT_LIKES:
        return "样本不足，回退盲轮换"
    if not any(profile.get("by_direction", {}).values()):
        return "无方向数据，回退盲轮换"
    holes = hole_directions(profile)
    if holes:
        return "空白方向: %s" % "、".join(holes)
    low = proto_hole_directions(
        profile,
        min_count=0,
        thin_threshold=THIN_DIRECTION_SHARE,
    )
    if low:
        return "低关注方向: %s" % "、".join(low)
    bubble, _ = biggest_bubble_direction(profile)
    return "对茧房方向 %s 投放对立/陌生角度查询" % bubble


def opposition_query(direction, now=None):
    """给一个方向生成对立/陌生角度查询（1 条）。"""
    if not direction:
        return []
    now = now or datetime.now(likes_mod.TZ)
    month = _month_cn(now)
    templates = OPPOSITION_QUERY_TEMPLATES.get(direction) or [
        "%s 反方观点 质疑 反思 {month}" % direction
    ]
    query = templates[0].format(month=month)
    return [{
        "query": query,
        "max_results": 8,
        "direction": direction,
        "preferred": False,
        "explore": True,
    }]


def breakout_queries(profile, now=None, max_q=2, thin_threshold=THIN_DIRECTION_SHARE):
    """数据驱动的探索查询引擎：空白方向 → 低关注方向 → 茧房对立查询。

    点赞样本过少时返回空列表，由 likes.py 回退到原有日期轮换，避免冷启动误判。
    """
    if not profile or profile.get("total", 0) < MIN_BREAKOUT_LIKES:
        return []
    if not any(profile.get("by_direction", {}).values()):
        return []
    if max_q is None or max_q <= 0:
        return []

    holes = hole_directions(profile)
    if not holes:
        holes = proto_hole_directions(
            profile,
            min_count=0,
            thin_threshold=thin_threshold,
        )
    if not holes:
        bubble, _ = biggest_bubble_direction(profile)
        return opposition_query(bubble, now)

    now = now or datetime.now(likes_mod.TZ)
    month = _month_cn(now)
    queries = []
    for di, direction in enumerate(holes):
        templates = HOLE_QUERY_TEMPLATES.get(direction) or [
            "%s 深度 新视角 {month}" % direction
        ]
        for template in templates:
            if len(queries) >= max_q:
                break
            queries.append({
                "query": template.format(month=month),
                "max_results": 8,
                "direction": direction,
                "preferred": False,
                "explore": True,
            })
            if len(queries) < max_q and di + 1 < len(holes):
                break
        if len(queries) >= max_q:
            break
    return queries[:max_q]


def format_status(profile, with_explore_reason=False, now=None):
    """返回一行可读的中文画像摘要，供 dailybrief status 使用。"""
    if not profile:
        return "画像: 暂无数据"
    total = profile.get("total", 0)
    pref = profile.get("top_direction") or "未启用"
    topics = "、".join(
        "%s×%d" % (topic, count) for topic, count in profile.get("top_topics", [])[:5]
    ) or "暂无"
    keywords = "、".join(
        list(profile.get("keywords", {}).keys())[:5]
    ) or "暂无"
    holes = "、".join(hole_directions(profile)) or "无"
    status = "画像: 共%d赞 | 偏好方向:%s | 高潜子话题:%s | 关键词:%s | 空白方向:%s" % (
        total, pref, topics, keywords, holes
    )
    if with_explore_reason:
        status += " | 探索理由:%s" % _explore_reason(profile, now)
    return status


if __name__ == "__main__":
    print(format_status(compute_profile()))
