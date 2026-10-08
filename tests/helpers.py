# -*- coding: utf-8 -*-
"""测试公共工具：路径与构造辅助。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


def long_body(n=600):
    """构造 ≥500 字的类正文文本（含署名与数字，避免触发水文硬剔除）。"""
    return "作者：张三。" + ("轨道交通制动系统深度分析报道。" * (n // 15 + 2))


def make_candidate(url, title="测试文章", direction="科技", hn_points=None,
                   hn_discussed_at=None, content="", is_explore=False):
    return {
        "title": title,
        "url": url,
        "content": content,
        "direction": direction,
        "is_preferred": False,
        "is_explore": is_explore,
        "hn_points": hn_points,
        "hn_discussed_at": hn_discussed_at,
        "publish_date": "",
        "date_verified": False,
    }
