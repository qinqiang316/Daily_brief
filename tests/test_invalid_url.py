# -*- coding: utf-8 -*-
"""非法 URL 拒收（不静默修复）。"""
import unittest

from tests.helpers import ROOT, SCRIPTS  # noqa: F401  (sys.path 副作用)
from modules import filter as filter_mod


class TestInvalidUrl(unittest.TestCase):
    def test_backslash_url_rejected(self):
        self.assertEqual(filter_mod.norm_url("https:\\\\evil.com\\2026\\10\\a"), "")
        self.assertEqual(filter_mod.norm_url("https://example.com/2026\\10\\a"), "")

    def test_whitespace_and_control_rejected(self):
        self.assertEqual(filter_mod.norm_url("https://example.com/a b"), "")
        self.assertEqual(filter_mod.norm_url("https://example.com/a\tb"), "")
        self.assertEqual(filter_mod.norm_url("https://example.com/a\nb"), "")

    def test_non_http_scheme_rejected(self):
        self.assertEqual(filter_mod.norm_url("javascript:alert(1)"), "")
        self.assertEqual(filter_mod.norm_url("ftp://example.com/x"), "")
        self.assertEqual(filter_mod.norm_url("example.com/2026/10/08/a"), "")

    def test_missing_host_rejected(self):
        self.assertEqual(filter_mod.norm_url("https://"), "")
        self.assertEqual(filter_mod.norm_url(""), "")
        self.assertEqual(filter_mod.norm_url(None), "")

    def test_valid_url_normalized_not_dropped(self):
        self.assertEqual(
            filter_mod.norm_url("http://www.example.com/2026/10/08/a?utm_source=x&b=1#frag"),
            "https://example.com/2026/10/08/a?b=1")


if __name__ == "__main__":
    unittest.main()
