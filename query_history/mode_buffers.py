# -*- coding: utf-8 -*-
"""单本查询与历史查询的结果缓冲隔离。"""


class QueryModeResultBuffers:
    """单本(0)与历史(2)各自保留结果快照；清除单本不影响历史。"""

    SINGLE = 0
    HISTORY = 2

    def __init__(self):
        self._bufs = {self.SINGLE: [], self.HISTORY: []}
        self.active = self.SINGLE

    def save(self, mode, results):
        if mode in self._bufs:
            self._bufs[mode] = list(results or [])

    def get(self, mode):
        return list(self._bufs.get(mode) or [])

    def clear_single(self):
        self._bufs[self.SINGLE] = []

    def clear_history_view(self):
        self._bufs[self.HISTORY] = []
