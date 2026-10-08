# -*- coding: utf-8 -*-
"""排序：轨交优先、HN 只在同质量排序、探索保底（生活/健康优先）、主题多样性。"""
import unittest

from tests.helpers import ROOT, SCRIPTS  # noqa: F401
from modules import rank


def kept_item(direction, pub, hn_points=None, is_explore=False, title=None):
    return {
        "title": title or ("%s-%s" % (direction, pub)),
        "url": "https://example.com/%s/%s" % (direction, pub),
        "domain": "example.com",
        "direction": direction,
        "is_preferred": False,
        "is_explore": is_explore,
        "publish_date": pub,
        "date_verified": True,
        "date_precision": "day",
        "window_outside_days": 0,
        "hn_points": hn_points,
        "word_count": 800,
        "watermark_suspect": False,
        "watermark_reasons": [],
        "content": "x",
        "source_key": None,
        "source_label": None,
    }


class TestRanking(unittest.TestCase):
    def test_rail_priority_over_hn_points(self):
        """轨交优先：轨交条目即使无 HN 积分也排在高积分 HN 之前。"""
        items = [
            kept_item("科技", "2026-10-08", hn_points=900),
            kept_item("轨道交通", "2026-10-07"),
        ]
        out = rank.rank_candidates(items, "生活")
        self.assertEqual(out[0]["direction"], "轨道交通")

    def test_hn_points_only_order_within_same_date(self):
        """HN 积分只在同质量（同日期层级）内作次序参考。"""
        items = [
            kept_item("科技", "2026-10-08", title="低分"),
            kept_item("科技", "2026-10-08", hn_points=500, title="高分"),
            kept_item("商业", "2026-10-07", title="昨日非HN"),
        ]
        out = rank.rank_candidates(items, "生活")
        self.assertEqual(out[0]["title"], "高分")
        self.assertEqual(out[1]["title"], "低分")
        # 昨日非 HN 条目不得被积分挤到最前，但应排在同日两条之后
        self.assertEqual(out[2]["title"], "昨日非HN")

    def test_explore_seats_reserved_with_life_health_priority(self):
        """有合格探索候选时保底 ≤2 席，生活/健康优先，即使主池全是高分条目。"""
        main = [kept_item("科技", "2026-10-08", hn_points=900 - i,
                          title="主池%d" % i) for i in range(15)]
        explore = [
            kept_item("商业", "2026-10-06", is_explore=True, title="探索商业"),
            kept_item("健康", "2026-10-06", is_explore=True, title="探索健康"),
        ]
        out = rank.rank_candidates(main + explore, "健康")
        titles = [x["title"] for x in out]
        self.assertIn("探索健康", titles)  # 生活/健康探索保底
        self.assertLessEqual(sum(1 for x in out if x["is_explore"]), 2)
        self.assertLessEqual(len(out), rank.MAX_CANDIDATES)

    def test_no_explore_seat_when_no_qualified_explore(self):
        """没有合格探索候选时不凭空保留席位。"""
        main = [kept_item("科技", "2026-10-08", title="m%d" % i) for i in range(15)]
        out = rank.rank_candidates(main, "生活")
        self.assertFalse(any(x["is_explore"] for x in out))

    def test_topic_diversity_cap(self):
        """主题多样性：单方向（轨交除外）超过上限被截断。"""
        items = [kept_item("科技", "2026-10-08", title="t%d" % i) for i in range(10)]
        items += [kept_item("轨道交通", "2026-10-08", title="r%d" % i) for i in range(3)]
        out = rank.rank_candidates(items, "生活")
        tech = [x for x in out if x["direction"] == "科技"]
        rail = [x for x in out if x["direction"] == "轨道交通"]
        self.assertLessEqual(len(tech), rank.MAX_PER_DIRECTION)
        self.assertEqual(len(rail), 3)  # 轨交不受多样性上限约束

    def test_hn_pool_cap(self):
        """HN 不得占满池：超过 MAX_HN 的 HN 条目被截断。"""
        items = [kept_item("科技", "2026-10-08", hn_points=100 + i,
                           title="hn%d" % i) for i in range(rank.MAX_HN + 5)]
        # 方向全相同会先触发多样性上限，这里用不同方向绕开以专测 HN 上限
        dirs = ["科技", "商业", "生活", "健康", "AI"]
        for i, x in enumerate(items):
            x["direction"] = dirs[i % len(dirs)]
        out = rank.rank_candidates(items, "生活")
        self.assertLessEqual(sum(1 for x in out if x.get("hn_points")), rank.MAX_HN)

    def test_life_health_guaranteed_non_explore(self):
        """生活/健康保底不限探索候选：非探索的合格生活/健康条目也各保至少 1 席，
        即使主池全是高分科技条目。"""
        main = [kept_item("科技", "2026-10-08", hn_points=900 - i,
                          title="主池%d" % i) for i in range(15)]
        life = kept_item("生活", "2026-10-06", title="生活非探索")
        health = kept_item("健康", "2026-10-06", title="健康非探索")
        out = rank.rank_candidates(main + [life, health], "生活")
        titles = [x["title"] for x in out]
        self.assertIn("生活非探索", titles)
        self.assertIn("健康非探索", titles)

    def test_none_direction_hn_cannot_crowd_out_life_health(self):
        """方向 None 的 HN 条目再多，也挤不掉合格生活/健康保底席。"""
        hn_none = [kept_item(None, "2026-10-08", hn_points=500 + i,
                             title="无向HN%d" % i) for i in range(10)]
        life = kept_item("生活", "2026-10-07", title="生活文")
        health = kept_item("健康", "2026-10-07", title="健康文")
        out = rank.rank_candidates(hn_none + [life, health], "生活")
        titles = [x["title"] for x in out]
        self.assertIn("生活文", titles)
        self.assertIn("健康文", titles)
        self.assertLessEqual(len(out), rank.MAX_CANDIDATES)
        self.assertLessEqual(sum(1 for x in out if x.get("hn_points")), rank.MAX_HN)

    def test_guaranteed_seats_count_toward_direction_cap(self):
        """保底席计入主题上限：生活保底 1 席后，主池生活最多再进 MAX_PER_DIRECTION-1 条。"""
        main = [kept_item("生活", "2026-10-08", title="生活%d" % i)
                for i in range(rank.MAX_PER_DIRECTION + 2)]
        out = rank.rank_candidates(main, "生活")
        self.assertLessEqual(
            sum(1 for x in out if x["direction"] == "生活"), rank.MAX_PER_DIRECTION)
        self.assertGreaterEqual(
            sum(1 for x in out if x["direction"] == "生活"), 1)  # 保底仍 ≥1


if __name__ == "__main__":
    unittest.main()
