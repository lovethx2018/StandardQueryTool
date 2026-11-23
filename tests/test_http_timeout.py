# -*- coding: utf-8 -*-
"""HTTP 超时拆分测试。"""
import os
import sys
import unittest
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import http_client


class TestTimeoutSplit(unittest.TestCase):
    def test_requests_timeout_tuple(self):
        if not http_client.HAS_REQUESTS:
            self.skipTest("requests 不可用")
        sess = MagicMock()
        sess.get.return_value = object()
        http_client._http_request(sess, "get", "https://example.com/x", timeout=15)
        _args, kwargs = sess.get.call_args
        self.assertEqual(kwargs.get("timeout"), (5.0, 15.0))


if __name__ == "__main__":
    unittest.main()