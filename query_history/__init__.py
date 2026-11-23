# -*- coding: utf-8 -*-
"""查询历史：本地缓存与去重。"""
from .store import QueryHistoryStore, normalize_query_key, TTL_DAYS, MAX_ENTRIES

__all__ = [
    'QueryHistoryStore',
    'normalize_query_key',
    'TTL_DAYS',
    'MAX_ENTRIES',
]
