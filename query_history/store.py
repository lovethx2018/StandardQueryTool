# -*- coding: utf-8 -*-
"""查询历史本地存储：去重、30 天 TTL、结果缓存。"""
import json
import os
import re
import time
from datetime import date, datetime, timedelta

from app_env import logger, DATA_DIR
from ledger import normalize_std_key
from models import StandardInfo
from utils import atomic_write_json


_CACHE_FILE = os.path.join(DATA_DIR, 'query_history_cache.json')
_CACHE_VERSION = 1
TTL_DAYS = 30
MAX_ENTRIES = 200

_TIME_FMT = '%Y-%m-%d %H:%M:%S'


def normalize_query_key(text, kind='std_no'):
    """历史去重键。kind: std_no | chinese_name。"""
    raw = (text or '').strip()
    if not raw:
        return ''
    if kind == 'chinese_name':
        return re.sub(r'\s+', ' ', raw)
    return normalize_std_key(raw)


def _parse_dt(text):
    text = (text or '').strip()
    if not text:
        return None
    for fmt in (_TIME_FMT, '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_date(text):
    text = (text or '').strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _series_base(std_no):
    """去掉年号后的系列键，如 GB/T1345-2005 → GB/T1345。"""
    key = normalize_std_key(std_no)
    return re.sub(r'[-—:：]\d{4}$', '', key)


class QueryHistoryStore:
    """查询历史 JSON 存储。"""

    def __init__(self, path=None, ttl_days=TTL_DAYS, max_entries=MAX_ENTRIES):
        self.path = path or _CACHE_FILE
        self.ttl_days = int(ttl_days)
        self.max_entries = int(max_entries)
        self._data = {
            'version': _CACHE_VERSION,
            'ttl_days': self.ttl_days,
            'max_entries': self.max_entries,
            'entries': [],
        }
        self.load()

    def load(self):
        try:
            if not os.path.isfile(self.path):
                return
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            self._data['entries'] = list(data.get('entries') or [])
            if data.get('ttl_days'):
                self.ttl_days = int(data['ttl_days'])
            if data.get('max_entries'):
                self.max_entries = int(data['max_entries'])
        except Exception as e:
            logger.warning(f"[查询历史] 读缓存失败: {e}")

    def save(self):
        try:
            self._data['version'] = _CACHE_VERSION
            self._data['ttl_days'] = self.ttl_days
            self._data['max_entries'] = self.max_entries
            atomic_write_json(self.path, self._data)
        except Exception as e:
            logger.warning(f"[查询历史] 写缓存失败: {e}")

    def list_entries(self):
        entries = list(self._data.get('entries') or [])
        entries.sort(key=lambda e: e.get('queried_at') or '', reverse=True)
        return entries

    def is_expired(self, entry):
        if not entry:
            return True
        dt = _parse_dt(entry.get('queried_at'))
        if not dt:
            return True
        return datetime.now() - dt >= timedelta(days=self.ttl_days)

    def get_entry(self, query_text, kind='std_no'):
        key = normalize_query_key(query_text, kind)
        if not key:
            return None
        for e in self._data.get('entries') or []:
            if e.get('query_key') == key:
                return e
        return None

    def get_fresh(self, query_text, kind='std_no'):
        """未过期才返回 entry，否则 None。"""
        entry = self.get_entry(query_text, kind)
        if not entry or self.is_expired(entry):
            return None
        return entry

    def upsert(self, query_text, kind, results, source='network'):
        """写入/覆盖同 key，置顶；裁剪至 max_entries。返回 entry。"""
        text = (query_text or '').strip()
        key = normalize_query_key(text, kind)
        if not key:
            return None
        result_dicts = []
        if isinstance(results, list) and results:
            if isinstance(results[0], StandardInfo):
                result_dicts = self.results_to_dicts(results)
            elif isinstance(results[0], dict):
                result_dicts = [dict(x) for x in results]
        elif isinstance(results, list):
            result_dicts = []

        now = time.strftime(_TIME_FMT)
        entry = {
            'query_key': key,
            'query_text': text,
            'query_kind': kind if kind in ('std_no', 'chinese_name') else 'std_no',
            'queried_at': now,
            'hit_count': len(result_dicts),
            'results': result_dicts,
            'source': source or 'network',
        }
        others = [
            e for e in (self._data.get('entries') or [])
            if e.get('query_key') != key
        ]
        others.insert(0, entry)
        self._data['entries'] = others[: self.max_entries]
        self.save()
        return entry

    def delete(self, keys):
        if not keys:
            return 0
        keyset = set(keys)
        before = len(self._data.get('entries') or [])
        self._data['entries'] = [
            e for e in (self._data.get('entries') or [])
            if e.get('query_key') not in keyset
        ]
        removed = before - len(self._data['entries'])
        if removed:
            self.save()
        return removed

    def clear(self):
        n = len(self._data.get('entries') or [])
        self._data['entries'] = []
        self.save()
        return n

    # ---------- StandardInfo 序列化 ----------

    def results_to_dicts(self, results):
        out = []
        for r in results or []:
            out.append(self._std_to_dict(r))
        return out

    def dicts_to_results(self, dicts):
        out = []
        for d in dicts or []:
            if isinstance(d, dict):
                out.append(self._dict_to_std(d))
        return out

    def _std_to_dict(self, r, _depth=0):
        d = {
            'std_no': getattr(r, 'std_no', '') or '',
            'std_name': getattr(r, 'std_name', '') or '',
            'status': getattr(r, 'status', '') or '',
            'publish_date': getattr(r, 'publish_date', '') or '',
            'implement_date': getattr(r, 'implement_date', '') or '',
            'download_url': getattr(r, 'download_url', '') or '',
            'source_site': getattr(r, 'source_site', '') or '',
            'detail_url': getattr(r, 'detail_url', '') or '',
            'replacement_no': getattr(r, 'replacement_no', None),
            'replacement_detail_url': getattr(r, 'replacement_detail_url', None) or '',
            'no_replacement': bool(getattr(r, 'no_replacement', False)),
            'download_status': getattr(r, 'download_status', '') or '未检测',
            # 下载路由依赖：丢了这些字段，缓存命中后「采」标会被当成可下全文，
            # gxzl-only 等探测结果也会退化成全源直下
            'samr_text_url': getattr(r, 'samr_text_url', None) or '',
            'is_adopted': bool(getattr(r, 'is_adopted', False)),
            'download_sources': [str(s) for s in (getattr(r, 'download_sources', None) or [])],
        }
        repl = getattr(r, 'replacement', None)
        if repl is not None and _depth < 1:
            d['replacement'] = self._std_to_dict(repl, _depth=1)
        return d

    def _dict_to_std(self, d, _depth=0):
        std = StandardInfo(
            std_no=d.get('std_no') or '',
            std_name=d.get('std_name') or '',
            status=d.get('status') or '',
            publish_date=d.get('publish_date') or '',
            implement_date=d.get('implement_date') or '',
            download_url=d.get('download_url') or '',
            source_site=d.get('source_site') or '',
        )
        std.detail_url = d.get('detail_url') or ''
        std.replacement_no = d.get('replacement_no')
        std.replacement_detail_url = d.get('replacement_detail_url') or None
        std.no_replacement = bool(d.get('no_replacement'))
        std.download_status = d.get('download_status') or '未检测'
        std.samr_text_url = d.get('samr_text_url') or None
        std.is_adopted = bool(d.get('is_adopted'))
        std.download_sources = [str(s) for s in (d.get('download_sources') or [])]
        repl = d.get('replacement')
        if isinstance(repl, dict) and _depth < 1:
            std.replacement = self._dict_to_std(repl, _depth=1)
        return std

    # ---------- 本地状态校正 ----------

    def apply_local_status_fix(self, results):
        """即将实施且实施日已到 → 现行；同系列更旧版本 → 废止。返回新列表（StandardInfo）。"""
        items = list(results or [])
        if not items:
            return items
        # 若传入 dict，先转对象
        if items and isinstance(items[0], dict):
            items = self.dicts_to_results(items)

        today = date.today()
        by_series = {}
        for r in items:
            base = _series_base(r.std_no)
            by_series.setdefault(base, []).append(r)

        for group in by_series.values():
            due = []
            for r in group:
                st = r.status or ''
                if '即将实施' not in st:
                    continue
                impl = _parse_date(r.implement_date)
                if impl and impl <= today:
                    due.append(r)
            if not due:
                continue
            # 取实施日最新的一条升为现行
            due.sort(key=lambda x: _parse_date(x.implement_date) or date.min, reverse=True)
            winner = due[0]
            winner.status = '现行'
            wkey = normalize_std_key(winner.std_no)
            for r in group:
                if normalize_std_key(r.std_no) == wkey:
                    continue
                st = r.status or ''
                if '废止' in st or '作废' in st:
                    continue
                if '现行' in st or '即将实施' in st or not st:
                    r.status = '废止'

        return items

    # ---------- 公告缓存补充 ----------

    def enrich_from_announcement_cache(self, results, announcement_cache=None):
        """用国标公告缓存补实施日 / 代替关系。接受 StandardInfo 列表。"""
        items = list(results or [])
        if not items:
            return items
        if items and isinstance(items[0], dict):
            items = self.dicts_to_results(items)

        if announcement_cache is None:
            try:
                from spiders.gb_announcement import load_list_cache
                announcement_cache = load_list_cache()
            except Exception:
                announcement_cache = None
        if not announcement_cache:
            return items

        # std_no key → {implement_date, replace_no, std_name, notice_code}
        index = {}
        for ann in announcement_cache.get('items') or []:
            for s in ann.get('standards') or []:
                k = normalize_std_key(s.get('std_no'))
                if not k:
                    continue
                index[k] = {
                    'implement_date': (s.get('implement_date') or '').strip(),
                    'replace_no': (s.get('replace_no') or '').strip(),
                    'std_name': (s.get('std_name') or '').strip(),
                    'notice_code': ann.get('code') or '',
                }

        if not index:
            return items

        for r in items:
            meta = index.get(normalize_std_key(r.std_no))
            if not meta:
                continue
            if meta['implement_date'] and not (r.implement_date or '').strip():
                r.implement_date = meta['implement_date']
            if meta['std_name'] and not (r.std_name or '').strip():
                r.std_name = meta['std_name']
            if meta['replace_no'] and not getattr(r, 'replacement_no', None):
                r.replacement_no = meta['replace_no']
        return items
