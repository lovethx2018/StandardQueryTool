# -*- coding: utf-8 -*-
"""标准台账本地存储与公告对照逻辑。"""
import json
import os
import re
import time
import uuid
from datetime import date, datetime

from app_env import logger, DATA_DIR
from utils import atomic_write_json


_CACHE_FILE = os.path.join(DATA_DIR, 'standard_ledger.json')
_CACHE_VERSION = 1


def normalize_std_key(std_no):
    """编号归一化键：去空格、统一斜杠，便于匹配。"""
    s = (std_no or '').strip().upper().replace('／', '/')
    s = re.sub(r'\s+', '', s)
    # GBT123 → GB/T123
    m = re.match(r'^([A-Z]+\d*)T(\d)', s)
    if m and '/' not in s:
        s = m.group(1) + '/T' + m.group(2)
    return s


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


def _split_replace_nos(text):
    """从「代替标准」字段拆出若干编号。

    只按逗号/顿号/分号拆分，保留编号内部空格（如 GB/T 19889.4-2005）。
    """
    raw = (text or '').strip()
    if not raw:
        return []
    parts = re.split(r'[,，;；、]+', raw)
    out = []
    for p in parts:
        p = re.sub(r'\s+', ' ', p).strip()
        if not p:
            continue
        # 过滤纯中文说明（如「部分代替」）
        if not re.search(r'[A-Za-z0-9]', p):
            continue
        if not re.search(r'[A-Za-z]', p):
            continue
        out.append(p)
    return out


class StandardLedgerStore:
    """台账 JSON 存储。

    items: 台账行
    pending: 即将被替代（未到期）
    history: 已到期/已处置归档
    """

    def __init__(self, path=None):
        self.path = path or _CACHE_FILE
        self._data = {
            'version': _CACHE_VERSION,
            'updated_at': '',
            'items': [],
            'pending': [],
            'history': [],
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
            self._data['items'] = list(data.get('items') or [])
            self._data['pending'] = list(data.get('pending') or [])
            self._data['history'] = list(data.get('history') or [])
            self._data['updated_at'] = data.get('updated_at') or ''
            self.expire_due_replacements(save=False)
        except Exception as e:
            logger.warning(f"[标准台账] 读缓存失败: {e}")

    def save(self):
        try:
            self._data['version'] = _CACHE_VERSION
            self._data['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
            atomic_write_json(self.path, self._data)
        except Exception as e:
            logger.warning(f"[标准台账] 写缓存失败: {e}")

    # ---------- 查询 ----------

    def items(self):
        return list(self._data.get('items') or [])

    def pending(self):
        return list(self._data.get('pending') or [])

    def history(self):
        return list(self._data.get('history') or [])

    def stats(self):
        """总览统计。"""
        items = self.items()
        pending = self.pending()
        current = 0
        for it in items:
            st = it.get('status') or ''
            if '已被替代' in st or '废止' in st or '作废' in st:
                continue
            current += 1
        return {
            'total': len(items),
            'current': current,
            'pending': len(pending),
            'history': len(self.history()),
        }

    def find_item(self, std_no):
        key = normalize_std_key(std_no)
        for it in self._data['items']:
            if normalize_std_key(it.get('std_no')) == key:
                return it
        return None

    # ---------- 写入 ----------

    def upsert_item(self, std_no, std_name='', status='', publish_date='',
                    implement_date='', source='manual', mgmt_no='', save=True):
        """新增或更新台账行。返回 (item, created:bool)。

        save=False 时只改内存，供批量导入末尾一次落盘。
        """
        std_no = (std_no or '').strip()
        if not std_no:
            raise ValueError('缺少标准编号')
        existing = self.find_item(std_no)
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        if existing:
            if std_name:
                existing['std_name'] = std_name
            if status:
                existing['status'] = status
            if publish_date:
                existing['publish_date'] = publish_date
            if implement_date:
                existing['implement_date'] = implement_date
            if mgmt_no:
                existing['mgmt_no'] = mgmt_no
            elif 'mgmt_no' not in existing:
                existing['mgmt_no'] = ''
            existing['updated_at'] = now
            if save:
                self.save()
            return existing, False
        row = {
            'id': uuid.uuid4().hex[:12],
            'std_no': std_no,
            'mgmt_no': (mgmt_no or '').strip(),
            'std_name': std_name or '',
            'status': status or '现行',
            'publish_date': publish_date or '',
            'implement_date': implement_date or '',
            'source': source or 'manual',
            'created_at': now,
            'updated_at': now,
        }
        self._data['items'].append(row)
        if save:
            self.save()
        return row, True

    def place_item_after(self, std_no, after_std_no, save=True):
        """把 std_no 挪到 after_std_no 的下一行（其余相对顺序不变）。"""
        key = normalize_std_key(std_no)
        after_key = normalize_std_key(after_std_no)
        if not key or not after_key or key == after_key:
            return False
        items = self._data['items']
        moving = None
        rest = []
        for it in items:
            if normalize_std_key(it.get('std_no')) == key:
                moving = it
            else:
                rest.append(it)
        if moving is None:
            return False
        out = []
        placed = False
        for it in rest:
            out.append(it)
            if not placed and normalize_std_key(it.get('std_no')) == after_key:
                out.append(moving)
                placed = True
        if not placed:
            out.append(moving)
        self._data['items'] = out
        if save:
            self.save()
        return True

    def remove_item(self, std_no):
        key = normalize_std_key(std_no)
        before = len(self._data['items'])
        self._data['items'] = [
            it for it in self._data['items']
            if normalize_std_key(it.get('std_no')) != key
        ]
        # 同步去掉相关待替代
        self._data['pending'] = [
            p for p in self._data['pending']
            if normalize_std_key(p.get('old_std_no')) != key
        ]
        if len(self._data['items']) != before:
            self.save()
            return True
        return False

    def import_rows(self, rows):
        """批量导入 [{'std_no',...}, ...]。返回 (added, updated, skipped)。

        全程只写盘一次；用编号索引避免逐条线性查找。
        """
        added = updated = skipped = 0
        index = {
            normalize_std_key(it.get('std_no')): it
            for it in self._data['items']
            if it.get('std_no')
        }
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        for r in rows or []:
            no = (r.get('std_no') or '').strip()
            if not no:
                skipped += 1
                continue
            key = normalize_std_key(no)
            existing = index.get(key)
            if existing:
                std_name = r.get('std_name') or ''
                status = r.get('status') or ''
                publish_date = r.get('publish_date') or ''
                implement_date = r.get('implement_date') or ''
                mgmt_no = r.get('mgmt_no') or ''
                if std_name:
                    existing['std_name'] = std_name
                if status:
                    existing['status'] = status
                if publish_date:
                    existing['publish_date'] = publish_date
                if implement_date:
                    existing['implement_date'] = implement_date
                if mgmt_no:
                    existing['mgmt_no'] = mgmt_no
                elif 'mgmt_no' not in existing:
                    existing['mgmt_no'] = ''
                if r.get('source'):
                    existing['source'] = r.get('source')
                existing['updated_at'] = now
                updated += 1
            else:
                row = {
                    'id': uuid.uuid4().hex[:12],
                    'std_no': no,
                    'mgmt_no': (r.get('mgmt_no') or '').strip(),
                    'std_name': r.get('std_name') or '',
                    'status': r.get('status') or '现行',
                    'publish_date': r.get('publish_date') or '',
                    'implement_date': r.get('implement_date') or '',
                    'source': r.get('source') or 'import',
                    'created_at': now,
                    'updated_at': now,
                }
                self._data['items'].append(row)
                index[key] = row
                added += 1
        if added or updated:
            self.save()
        return added, updated, skipped

    # ---------- 待替代 / 到期 ----------

    def add_pending_replacement(self, old_std_no, new_std_no, new_std_name='',
                                implement_date='', notice_code='', source='announcement',
                                save=True):
        """登记即将被替代。已存在同 old+new 则更新。到期则直接走归档。

        返回 (row_or_None, changed:bool)。
        """
        old_std_no = (old_std_no or '').strip()
        new_std_no = (new_std_no or '').strip()
        if not old_std_no or not new_std_no:
            return None, False
        if not self.find_item(old_std_no):
            return None, False

        impl = _parse_date(implement_date)
        today = date.today()
        if impl and impl <= today:
            self._archive_replacement(
                old_std_no, new_std_no, new_std_name, implement_date,
                notice_code, source, reason='已到期', save=save,
            )
            return None, True

        old_key = normalize_std_key(old_std_no)
        new_key = normalize_std_key(new_std_no)
        for p in self._data['pending']:
            if (normalize_std_key(p.get('old_std_no')) == old_key and
                    normalize_std_key(p.get('new_std_no')) == new_key):
                changed = False
                if new_std_name and new_std_name != (p.get('new_std_name') or ''):
                    p['new_std_name'] = new_std_name
                    changed = True
                if implement_date and implement_date != (p.get('implement_date') or ''):
                    p['implement_date'] = implement_date
                    changed = True
                if notice_code and notice_code != (p.get('notice_code') or ''):
                    p['notice_code'] = notice_code
                    changed = True
                item = self.find_item(old_std_no)
                if item and '即将被替代' not in (item.get('status') or ''):
                    item['status'] = '即将被替代'
                    item['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    changed = True
                if changed:
                    p['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    if save:
                        self.save()
                return p, changed

        now = time.strftime('%Y-%m-%d %H:%M:%S')
        row = {
            'id': uuid.uuid4().hex[:12],
            'old_std_no': old_std_no,
            'new_std_no': new_std_no,
            'new_std_name': new_std_name or '',
            'implement_date': implement_date or '',
            'notice_code': notice_code or '',
            'source': source,
            'created_at': now,
            'updated_at': now,
        }
        self._data['pending'].append(row)
        item = self.find_item(old_std_no)
        if item:
            item['status'] = '即将被替代'
            item['updated_at'] = now
        if save:
            self.save()
        return row, True

    def expire_due_replacements(self, save=True):
        """实施日期已到的待替代 → 归档并从待替代移除。返回归档条数。"""
        today = date.today()
        remain = []
        archived = 0
        for p in list(self._data.get('pending') or []):
            impl = _parse_date(p.get('implement_date'))
            if impl and impl <= today:
                self._archive_replacement(
                    p.get('old_std_no'), p.get('new_std_no'),
                    p.get('new_std_name'), p.get('implement_date'),
                    p.get('notice_code'), p.get('source'),
                    reason='已到期', save=False,
                )
                archived += 1
            else:
                remain.append(p)
        self._data['pending'] = remain
        if save and archived:
            self.save()
        return archived

    def _archive_replacement(self, old_std_no, new_std_no, new_std_name,
                             implement_date, notice_code, source, reason='已到期',
                             save=True):
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        hist = {
            'id': uuid.uuid4().hex[:12],
            'old_std_no': old_std_no or '',
            'new_std_no': new_std_no or '',
            'new_std_name': new_std_name or '',
            'implement_date': implement_date or '',
            'notice_code': notice_code or '',
            'source': source or '',
            'reason': reason,
            'archived_at': now,
        }
        # 去重：同 old+new 已在历史则更新字段（补全来源站点等），不重复追加
        ok = normalize_std_key(old_std_no)
        nk = normalize_std_key(new_std_no)
        for h in self._data['history']:
            if (normalize_std_key(h.get('old_std_no')) == ok and
                    normalize_std_key(h.get('new_std_no')) == nk):
                if new_std_name:
                    h['new_std_name'] = new_std_name
                if implement_date:
                    h['implement_date'] = implement_date
                if notice_code:
                    h['notice_code'] = notice_code
                if source:
                    h['source'] = source
                if reason:
                    h['reason'] = reason
                h['archived_at'] = now
                break
        else:
            self._data['history'].insert(0, hist)

        item = self.find_item(old_std_no)
        if item:
            item['status'] = '已被替代'
            item['updated_at'] = now

        # 从 pending 去掉
        self._data['pending'] = [
            p for p in self._data['pending']
            if not (normalize_std_key(p.get('old_std_no')) == ok and
                    normalize_std_key(p.get('new_std_no')) == nk)
        ]
        if save:
            self.save()

    def match_announcement_cache(self, announcement_cache, latest_only=True,
                                 since_date=None):
        """用国标公告缓存中的 standards.replace_no 对照台账。

        announcement_cache: load_list_cache() 的 dict，或含 items 的结构。
        latest_only=True：只对照最新一份（日常「检查更新」用）。
        since_date：若给出 date，则对照该日及之后至最新的全部缓存公告
                   （首次导入用；与 latest_only 同时指定时以 since_date 为准）。
        返回新增/更新的待替代条数。全程只落盘一次。
        """
        if not announcement_cache:
            return 0
        items = list(announcement_cache.get('items') or [])
        if not items:
            return 0
        from spiders.gb_announcement import notice_code_sort_key
        if since_date is not None:
            kept = []
            for it in items:
                d = _parse_date(it.get('date'))
                if d and d >= since_date:
                    kept.append(it)
            items = kept
            items.sort(
                key=lambda it: notice_code_sort_key(it.get('code') or ''),
                reverse=True,
            )
        elif latest_only:
            items = [max(items, key=lambda it: notice_code_sort_key(it.get('code') or ''))]
        if not items:
            logger.info("[标准台账] 公告对照：筛选后无公告可对")
            return 0
        ledger_keys = {
            normalize_std_key(it.get('std_no'))
            for it in self._data.get('items') or []
            if it.get('std_no')
        }
        hit = 0
        any_change = False
        codes = [(it.get('code') or '').strip() for it in items if it.get('code')]
        for ann in items:
            notice_code = ann.get('code') or ''
            for std in ann.get('standards') or []:
                new_no = (std.get('std_no') or '').strip()
                new_name = (std.get('std_name') or '').strip()
                impl = (std.get('implement_date') or '').strip()
                for old_no in _split_replace_nos(std.get('replace_no')):
                    if normalize_std_key(old_no) not in ledger_keys:
                        continue
                    _, changed = self.add_pending_replacement(
                        old_no, new_no, new_name, impl, notice_code,
                        source='announcement',
                        save=False,
                    )
                    if changed:
                        hit += 1
                        any_change = True
        archived = self.expire_due_replacements(save=False)
        if archived:
            any_change = True
        if any_change:
            self.save()
        scope = '、'.join(codes[:5]) + ('…' if len(codes) > 5 else '')
        logger.info(
            f"[标准台账] 公告对照完成（共 {len(items)} 份：{scope or '无编号'}），"
            f"命中处理 {hit} 次，待替代 {len(self.pending())} 条"
        )
        return hit
    def banner_text(self):
        """顶部提醒文案。"""
        pend = self.pending()
        if not pend:
            return ''
        # 按实施日期排序，取最早几条
        def sort_key(p):
            d = _parse_date(p.get('implement_date')) or date.max
            return d

        pend_sorted = sorted(pend, key=sort_key)
        first = pend_sorted[0]
        tip = (
            f"{first.get('old_std_no')} 即将被 {first.get('new_std_no')} 替代"
        )
        if first.get('implement_date'):
            tip += f"（实施 {first.get('implement_date')}）"
        if len(pend) > 1:
            tip += f"；另有 {len(pend) - 1} 项待处置"
        return tip
