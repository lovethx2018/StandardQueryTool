# -*- coding: utf-8 -*-
"""第 1 批审计修复回归：关键词守卫、状态校正、工标网编码。"""
import datetime
import os
import sys
import unittest
from unittest.mock import MagicMock
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from models import StandardInfo
from utils import _extract_base_key, normalize_status_by_date


class TestKeywordGuards(unittest.TestCase):
    def test_normalize_slash_none(self):
        from spiders.spider import SpiderEngine
        self.assertEqual(SpiderEngine._normalize_slash(None), "")

    def test_normalize_slash_unicode(self):
        from spiders.spider import SpiderEngine
        self.assertEqual(SpiderEngine._normalize_slash("GB\u2215T 1"), "GB/T 1")

    def test_search_standard_empty_returns_list(self):
        from spiders.spider import SpiderEngine
        eng = SpiderEngine.__new__(SpiderEngine)
        eng._cancelled = False
        for kw in (None, "", "   "):
            self.assertEqual(eng.search_standard(kw), [])

    def test_encode_csres_fallback(self):
        from spiders.spider import SpiderEngine
        # 欧元符号超出 GB2312 → 回退 quote(keyword)
        special = "标准€"
        encoded = SpiderEngine._encode_csres_keyword(special)
        self.assertEqual(encoded, quote(special))
        # 普通中文可用 GB2312
        cn = "水泥"
        self.assertEqual(
            SpiderEngine._encode_csres_keyword(cn),
            quote(cn.encode("gb2312")),
        )


class TestFindCsresNoFallback(unittest.TestCase):
    def test_no_exact_match_returns_none(self):
        from spiders.spider import SpiderEngine
        from bs4 import BeautifulSoup

        eng = SpiderEngine.__new__(SpiderEngine)
        html = """
        <html><body>
          <a href="/detail/111.html">GB/T 9999-2020 其它标准</a>
        </body></html>
        """
        resp = MagicMock()
        resp.status_code = 200
        resp.text = html
        eng._csres_get = MagicMock(return_value=resp)
        # BeautifulSoup 在模块内已导入
        self.assertIsNone(eng._find_csres_detail_url("GB/T 1234-2018"))


class TestNormalizeStatus(unittest.TestCase):
    def test_future_new_beats_empty_old(self):
        today = datetime.date.today()
        future = (today + datetime.timedelta(days=60)).strftime("%Y-%m-%d")
        new = StandardInfo(std_no="GB/T 9999-2024", status="", implement_date=future)
        old = StandardInfo(std_no="GB/T 9999-2015", status="", implement_date="")
        normalize_status_by_date([new, old])
        self.assertEqual(new.status, "即将实施")
        self.assertEqual(old.status, "作废")

    def test_implemented_newest_is_current(self):
        today = datetime.date.today()
        past = (today - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
        older = (today - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
        a = StandardInfo(std_no="GB/T 8888-2023", status="", implement_date=past)
        b = StandardInfo(std_no="GB/T 8888-2010", status="", implement_date=older)
        normalize_status_by_date([a, b])
        self.assertEqual(a.status, "现行")
        self.assertEqual(b.status, "作废")

    def test_gb_and_gbt_separate_series_keys(self):
        today = datetime.date.today()
        past = (today - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
        future = (today + datetime.timedelta(days=60)).strftime("%Y-%m-%d")
        gb = StandardInfo(std_no="GB 9999-2010", status="", implement_date=past)
        gbt = StandardInfo(std_no="GB/T 9999-2024", status="", implement_date=future)
        self.assertNotEqual(
            _extract_base_key(gb.std_no),
            _extract_base_key(gbt.std_no),
        )
        normalize_status_by_date([gb, gbt])
        self.assertEqual(gbt.status, "即将实施")
        self.assertNotEqual(gb.status, "作废")

    def test_local_and_colon_year_series(self):
        today = datetime.date.today()
        past = (today - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
        future = (today + datetime.timedelta(days=60)).strftime("%Y-%m-%d")
        self.assertEqual(_extract_base_key("DB11/T 1234-2020"), "DB11/T1234")
        self.assertEqual(_extract_base_key("CECS 293:2011"), "CECS293")
        old = StandardInfo(std_no="DB11/T 1234-2018", status="", implement_date=past)
        new = StandardInfo(std_no="DB11/T 1234-2025", status="", implement_date=future)
        normalize_status_by_date([old, new])
        # 新版尚未实施：旧版仍是现行，新版为即将实施
        self.assertEqual(new.status, "即将实施")
        self.assertEqual(old.status, "现行")
        c1 = StandardInfo(std_no="CECS 293:2011", status="", implement_date=past)
        c2 = StandardInfo(std_no="CECS 293:2008", status="", implement_date=past)
        normalize_status_by_date([c1, c2])
        self.assertEqual(c1.status, "现行")
        self.assertEqual(c2.status, "作废")


class TestMergeMissingFields(unittest.TestCase):
    def test_download_route_fields_filled_from_secondary(self):
        from spiders.spider import SpiderEngine
        primary = StandardInfo(std_no="GB/T 1.1-2020", status="现行")
        secondary = StandardInfo(
            std_no="GB/T 1.1-2020", std_name="名称", status="现行",
            publish_date="2020-03-31",
        )
        secondary.is_adopted = True
        secondary.download_sources = ["openstd"]
        secondary.samr_text_url = "https://openstd.samr.gov.cn/bzgk/gb/newGbInfo?hcno=ABC"
        secondary.download_status = "可下载"
        SpiderEngine._fill_missing_std_fields(primary, secondary)
        self.assertTrue(primary.is_adopted)
        self.assertEqual(primary.download_sources, ["openstd"])
        self.assertEqual(primary.samr_text_url, secondary.samr_text_url)
        self.assertEqual(primary.download_status, "可下载")
        self.assertEqual(primary.publish_date, "2020-03-31")


class TestNumberQueryExpand(unittest.TestCase):
    def test_glued_prefix_expands_space(self):
        from spiders.spider import SpiderEngine
        self.assertEqual(
            SpiderEngine._expand_prefix_variants("JTG3460"),
            ["JTG 3460"],
        )
        self.assertEqual(
            SpiderEngine._expand_prefix_variants("JTG3460-2026"),
            ["JTG 3460-2026"],
        )

    def test_pure_digit_stays_single(self):
        from spiders.spider import SpiderEngine
        self.assertEqual(SpiderEngine._expand_prefix_variants("3460"), ["3460"])

    def test_year_body_fills_jtg(self):
        from spiders.spider import SpiderEngine
        variants = SpiderEngine._expand_prefix_variants("3460-2026")
        self.assertIn("JTG 3460-2026", variants)
        self.assertIn("GB/T 3460-2026", variants)

    def test_civil_sort_puts_jtg_before_gbt(self):
        from spiders.spider import SpiderEngine
        eng = SpiderEngine.__new__(SpiderEngine)
        rows = [
            StandardInfo(std_no="GB/T 3460-2017", std_name="钼酸铵", status="现行"),
            StandardInfo(
                std_no="JTG 3460-2026",
                std_name="公路工程土工合成材料试验规程",
                status="即将实施",
            ),
        ]
        sorted_rows = eng._sort_by_civil_relevance(rows)
        self.assertEqual(sorted_rows[0].std_no, "JTG 3460-2026")


class TestFilterPrefixEnforced(unittest.TestCase):
    def test_prefixed_query_drops_other_series(self):
        from spiders.spider import SpiderEngine
        from spiders.data_processor import DataProcessor

        eng = SpiderEngine.__new__(SpiderEngine)
        dp = DataProcessor(eng)
        rows = [
            StandardInfo(std_no="GB/T 3460-2017", std_name="钼酸铵", status="现行"),
            StandardInfo(
                std_no="JTG 3460-2026",
                std_name="公路工程土工合成材料试验规程",
                status="即将实施",
            ),
        ]
        out = dp.filter_results(rows, "JTG 3460")
        self.assertEqual([r.std_no for r in out], ["JTG 3460-2026"])


if __name__ == "__main__":
    unittest.main()
