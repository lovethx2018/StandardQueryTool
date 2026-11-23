# -*- coding: utf-8 -*-
"""query_history.store 单元测试。"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from models import StandardInfo
from query_history.store import QueryHistoryStore, normalize_query_key


class TestNormalize(unittest.TestCase):
    def test_std_no_spaces(self):
        self.assertEqual(
            normalize_query_key('GB/T 1345', 'std_no'),
            normalize_query_key('GB/T1345', 'std_no'),
        )

    def test_chinese_collapse_space(self):
        self.assertEqual(
            normalize_query_key('水泥  细度', 'chinese_name'),
            '水泥 细度',
        )


class TestStore(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        os.remove(self.path)
        self.store = QueryHistoryStore(path=self.path, ttl_days=30, max_entries=5)

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_upsert_dedupe_and_top(self):
        r1 = [StandardInfo(std_no='GB/T 1345-2005', std_name='a', status='现行')]
        self.store.upsert('GB/T 1345', 'std_no', r1)
        r2 = [
            StandardInfo(std_no='GB/T 1345-2005', status='现行'),
            StandardInfo(std_no='GB/T 1345-2026', status='即将实施'),
        ]
        self.store.upsert('GB/T1345', 'std_no', r2)
        entries = self.store.list_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['hit_count'], 2)

    def test_fresh_and_expired(self):
        self.store.upsert('水泥细度', 'chinese_name', [])
        self.assertIsNotNone(self.store.get_fresh('水泥细度', 'chinese_name'))
        entry = self.store.get_entry('水泥细度', 'chinese_name')
        old = (datetime.now() - timedelta(days=31)).strftime('%Y-%m-%d %H:%M:%S')
        entry['queried_at'] = old
        self.store.save()
        self.assertTrue(self.store.is_expired(entry))
        self.assertIsNone(self.store.get_fresh('水泥细度', 'chinese_name'))

    def test_max_entries(self):
        for i in range(8):
            self.store.upsert('KW%d' % i, 'std_no', [])
        entries = self.store.list_entries()
        self.assertEqual(len(entries), 5)
        keys = [e['query_key'] for e in entries]
        # 最新在前；超出上限删最早（KW0–KW2）
        self.assertEqual(keys[0], normalize_query_key('KW7', 'std_no'))
        self.assertNotIn(normalize_query_key('KW0', 'std_no'), keys)
        self.assertNotIn(normalize_query_key('KW2', 'std_no'), keys)

    def test_status_fix(self):
        today = datetime.now().strftime('%Y-%m-%d')
        results = [
            StandardInfo(std_no='GB/T 1-2000', status='现行', implement_date='2000-01-01'),
            StandardInfo(std_no='GB/T 1-2020', status='即将实施', implement_date=today),
        ]
        fixed = self.store.apply_local_status_fix(results)
        by_no = {r.std_no.replace(' ', ''): r.status for r in fixed}
        self.assertEqual(by_no['GB/T1-2020'], '现行')
        self.assertIn('废止', by_no['GB/T1-2000'])

    def test_download_route_fields_roundtrip(self):
        """缓存往返须保留下载路由字段，否则「采」标会被误判为可下全文。"""
        s = StandardInfo(std_no='GB/T 1.1-2020', std_name='x', status='现行')
        s.is_adopted = True
        s.download_sources = ['gxzl']
        s.samr_text_url = 'https://openstd.samr.gov.cn/bzgk/gb/newGbInfo?hcno=ABC'
        s.replacement_detail_url = 'http://www.csres.com/detail/1.html'
        self.store.upsert('GB/T 1.1-2020', 'std_no', [s])
        entry = self.store.get_fresh('GB/T 1.1-2020', 'std_no')
        back = self.store.dicts_to_results(entry['results'])[0]
        self.assertTrue(back.is_adopted)
        self.assertEqual(back.download_sources, ['gxzl'])
        self.assertEqual(back.samr_text_url, s.samr_text_url)
        self.assertEqual(back.replacement_detail_url, s.replacement_detail_url)


if __name__ == '__main__':
    unittest.main()
