# -*- coding: utf-8 -*-
"""M2 新源内容质量判定（可配置规则，不写死在调用处）。

规则常量集中在模块顶部，方便日后调整。核心入口为 evaluate。

"""
import re

from . import filter as filter_mod

# ---- 规则常量（可调） ----
AI_RATIO_MAX = 0.3
MIN_EVIDENCE = 3
MIN_TEXT_LEN = 120
LINK_LIMIT_PER_1000 = 15

# ---- 启发式信号词（全部可调） ----
GENERIC_FILLERS = ["总之", "总的来说", "综上所述", "在当今", "一方面", "另一方面",
                   "值得注意的是", "不得不提", "众所周知"]
FRAMEWORK_MARKERS = ["框架", "心智模型", "思维模型", "方法论", "范式", "概念模型",
                     "第一性原理", "底层逻辑", "mental model", "framework",
                     "first principle"]
REFLECTIVE_MARKERS = ["我认为", "更关键", "更重要的是", "真正重要的是", "本质上是",
                      "值得反思", "重新认识", "反直觉", "应当", "应该", "意味着"]
ACTION_MARKERS = ["可以这样做", "可以这样", "具体步骤", "落地方法", "操作清单",
                  "实用方法", "做法如下", "三步", "四步", "行动指南", "清单",
                  "sop", "how to"]
CROSS_DOMAIN_MARKERS = ["与...相通", "打通", "跨界", "迁移到", "类同于",
                        "不同于主流", "结合起来", "融合了", "借力", "analog"]

_EVIDENCE_PATTERNS = [
    re.compile(r"(?:[0-9]+(?:\.[0-9]+)?(?:%|％|万|亿|倍|个|人|家|元|公里|公里/时)?)"),
    re.compile(r"(?:19[5-9]\d|20\d{2})"),
    re.compile(r"研究(?:显示|表明|发现|指出)|实验|数据(?:显示|表明)|报告(?:指出|显示)"),
    re.compile(r"调查|统计|文献|据\s*\S{1,6}\s*(?:报道|显示)"),
    re.compile(r"[“\"‘\u201c][^”\"’\u201d]{6,}[”\"’\u201d]"),
    re.compile(r"https?://\S+|(?:\[\d+\]|\((?:来源|参考)[^)]*\)|参见\s*[^。；;]{2,10})"),
]

_LINK_RE = re.compile(r"https?://[^\s)\]}>]+")


def link_density_per_1000(content):
    """估算每千字符的链接信号数；href 与裸链接按一种口径计数。"""
    if not content:
        return 0.0
    text = str(content)
    hrefs = len(re.findall(r"href\s*=", text, re.I))
    links = hrefs if hrefs else len(_LINK_RE.findall(text))
    return round(links * 1000.0 / max(1, len(text.strip())), 2)


def ai_ratio_heuristic(content):
    """估算 AI 直接生成内容占比（0.0~1.0）。

    复用 filter.ai_watermark_check 的水文信号（无署名/模板句高频/无数字数据），
    再补一条正文过短与空泛联接词信号，最后钳制到 [0.0, 1.0]。
    """
    if not content or not content.strip():
        return 1.0
    _, reasons = filter_mod.ai_watermark_check(content)
    score = 0.0
    if "无数字/数据" in reasons:
        score += 0.35
    if "模板句高频" in reasons:
        score += 0.25
    if "无署名" in reasons:
        score += 0.20
    text = re.sub(r"\s+", " ", content).strip()
    if len(text) < MIN_TEXT_LEN:
        score += 0.15
    if sum(1 for f in GENERIC_FILLERS if f in text) >= 3:
        score += 0.15
    return round(min(1.0, score), 2)


def _has_any(markers, text):
    low = text.lower()
    return any(m in text or m in low for m in markers)


def _has_viewpoint(text):
    """粗略检测是否存在观点性表达。"""
    if _has_any(REFLECTIVE_MARKERS, text):
        return True
    return bool(re.search(r"[！!]|最要紧的是|给我的启发|我认为|我的判断", text))


def evidence_count(content):
    """统计引证/数据条数。

    检测数字、年份、百分比、研究/实验/报告信号、引用文本、引用链接等。启发式，
    同一文本可能命中多条，用以区分“纯复述”与“有材料支撑”。
    """
    text = content or ""
    if not text.strip():
        return 0
    return sum(len(p.findall(text)) for p in _EVIDENCE_PATTERNS)


def is_inspiring(content):
    """“有启发”操作化判定。

    命中以下四类之一即 True：
      ① 新框架/概念/心智模型；
      ② 有数据支持的反思性判断；
      ③ 可落地的行动/方法；
      ④ 跨领域打通的新连接。
    仅复述已知事实（无观点、无数据、无方法）→ False。
    """
    text = (content or "").strip()
    if len(text) < MIN_TEXT_LEN:
        return False
    has_data = evidence_count(text) >= 1
    has_viewpoint = _has_viewpoint(text)
    if link_density_per_1000(text) > LINK_LIMIT_PER_1000 and not has_viewpoint:
        return False
    if _has_any(FRAMEWORK_MARKERS, text) and (has_viewpoint or has_data):
        return True
    if has_viewpoint and has_data:
        return True
    if _has_any(ACTION_MARKERS, text):
        return True
    if _has_any(CROSS_DOMAIN_MARKERS, text) and has_viewpoint:
        return True
    return False


def evaluate(content):
    """综合评分，返回：
    {"ai_ratio": float, "inspiring": bool, "evidence": int, "pass": bool}

    pass = ai_ratio <= AI_RATIO_MAX AND (inspiring OR evidence >= MIN_EVIDENCE)
    """
    ai_ratio = ai_ratio_heuristic(content)
    inspiring = is_inspiring(content)
    ev = evidence_count(content)
    passed = ai_ratio <= AI_RATIO_MAX and (inspiring or ev >= MIN_EVIDENCE)
    return {"ai_ratio": ai_ratio, "inspiring": inspiring,
            "evidence": ev, "pass": passed}
