# -*- coding: utf-8 -*-
"""单本清除不得影响历史结果缓冲。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from query_history.mode_buffers import QueryModeResultBuffers


class TestQueryModeResultBuffers(unittest.TestCase):
    def test_clear_single_keeps_history(self):
        buf = QueryModeResultBuffers()
        buf.save(QueryModeResultBuffers.SINGLE, ["single-a"])
        buf.save(QueryModeResultBuffers.HISTORY, ["hist-a", "hist-b"])
        buf.clear_single()
        self.assertEqual(buf.get(QueryModeResultBuffers.SINGLE), [])
        self.assertEqual(
            buf.get(QueryModeResultBuffers.HISTORY),
            ["hist-a", "hist-b"],
        )

    def test_clear_history_view_keeps_single(self):
        buf = QueryModeResultBuffers()
        buf.save(QueryModeResultBuffers.SINGLE, ["s"])
        buf.save(QueryModeResultBuffers.HISTORY, ["h"])
        buf.clear_history_view()
        self.assertEqual(buf.get(QueryModeResultBuffers.SINGLE), ["s"])
        self.assertEqual(buf.get(QueryModeResultBuffers.HISTORY), [])


if __name__ == "__main__":
    unittest.main()
