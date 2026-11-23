# -*- coding: utf-8 -*-
"""第 2 批审计修复：原子 JSON、GXZL 缺页、SSL 降级。"""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils import atomic_write_json


class TestAtomicWriteJson(unittest.TestCase):
    def test_write_and_read(self):
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        os.remove(path)
        try:
            atomic_write_json(path, {'a': 1, 'b': '中文'})
            import json
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.assertEqual(data['a'], 1)
            self.assertEqual(data['b'], '中文')
            # 不应残留临时文件
            leftovers = [
                n for n in os.listdir(os.path.dirname(path) or '.')
                if n.startswith('.json_') and n.endswith('.tmp')
            ]
            self.assertEqual(leftovers, [])
        finally:
            if os.path.isfile(path):
                os.remove(path)

    def test_query_history_uses_atomic(self):
        from query_history.store import QueryHistoryStore
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        os.remove(path)
        try:
            store = QueryHistoryStore(path=path, ttl_days=30, max_entries=5)
            store.upsert('GB/T 1', 'std_no', [])
            self.assertTrue(os.path.isfile(path))
            store2 = QueryHistoryStore(path=path, ttl_days=30, max_entries=5)
            self.assertEqual(len(store2.list_entries()), 1)
        finally:
            if os.path.isfile(path):
                os.remove(path)


class TestGxzlMissingPages(unittest.TestCase):
    def test_failed_pages_refuse_merge(self):
        from spiders.gxzl_pdf_downloader import GxzlPdfDownloader
        from PIL import Image

        dl = GxzlPdfDownloader.__new__(GxzlPdfDownloader)
        dl.MAX_RETRIES = 1
        dl.RETRY_BASE_DELAY = 0
        dl.PAGE_DELAY_RANGE = (0, 0)
        dl.WATERMARK_THRESHOLD = 170
        dl.session = MagicMock()

        img = Image.new('RGB', (10, 10), (0, 0, 0))
        # 3 页，中间页失败
        calls = {'n': 0}

        def fake_page(std_no, page_num):
            calls['n'] += 1
            if page_num == 1:
                return None
            from io import BytesIO
            buf = BytesIO()
            img.save(buf, format='PNG')
            return buf.getvalue()

        dl._get_total_pages_with_retry = MagicMock(return_value=3)
        dl._download_page_with_retry = fake_page
        dl._merge_to_pdf = MagicMock(return_value=True)

        fd, out = tempfile.mkstemp(suffix='.pdf')
        os.close(fd)
        os.remove(out)
        try:
            ok = dl.download_standard('GB/T 1.1-2020', out)
            self.assertFalse(ok)
            dl._merge_to_pdf.assert_not_called()
        finally:
            if os.path.isfile(out):
                os.remove(out)


class TestSslNoDegradeOnConnectionError(unittest.TestCase):
    def test_connection_error_does_not_mark_fallback(self):
        import http_client as hc

        if not hc.HAS_REQUESTS:
            self.skipTest('requests 不可用')

        hc._ssl_fallback_hosts.clear()
        session = MagicMock()
        session.get = MagicMock(side_effect=hc.requests.exceptions.ConnectionError('boom'))

        with self.assertRaises(hc.requests.exceptions.ConnectionError):
            hc._http_request(session, 'get', 'https://example.com/x', timeout=1)

        self.assertNotIn('example.com', hc._ssl_fallback_hosts)

    def test_ssl_error_does_mark_fallback(self):
        import http_client as hc

        if not hc.HAS_REQUESTS:
            self.skipTest('requests 不可用')

        hc._ssl_fallback_hosts.clear()
        session = MagicMock()
        # 第一次 SSLError，第二次成功
        session.get = MagicMock(
            side_effect=[
                hc.requests.exceptions.SSLError('bad cert'),
                MagicMock(status_code=200),
            ]
        )
        resp = hc._http_request(session, 'get', 'https://ssl.example.com/x', timeout=1)
        self.assertIsNotNone(resp)
        self.assertIn('ssl.example.com', hc._ssl_fallback_hosts)
        hc._ssl_fallback_hosts.clear()


if __name__ == '__main__':
    unittest.main()
