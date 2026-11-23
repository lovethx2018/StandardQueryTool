# -*- coding: utf-8 -*-
"""国家标准公告：列表+ 详情（仅土建相关标准）+ 本地缓存。"""
import json
import os
import re
import time
from urllib.parse import quote

from bs4 import BeautifulSoup

from app_env import logger, TEMP_DIR
from config import Config
from http_client import _http_request
from .civil_domain import is_civil_std_name, is_excluded_announcement_title


NOC_LIST_URL = 'https://std.samr.gov.cn/noc/search/nocGBPage'
NOC_DETAIL_URL = 'https://std.sacinfo.org.cn/gnoc/queryInfo?id={pid}'
NOC_PORTAL_URL = 'https://std.samr.gov.cn/noc/nocGB'

# 界面固定展示最新 N 条（排除外文版/样品，且工程相关>0 后）
LIST_DISPLAY_LIMIT = 10
# 拉取时多取一些，以便过滤后仍能凑满 10 条
LIST_FETCH_PAGE_SIZE = 50
LIST_CANDIDATE_LIMIT = 40
CACHE_VERSION = 3

_CACHE_FILE = os.path.join(TEMP_DIR, 'gb_announcement_cache.json')


def notice_code_sort_key(code):
    """公告号排序键：2026年第31号 > 2026年第30号。无法解析时返回 (0, 0)。"""
    m = re.search(r'(\d{4})\s*年\s*第\s*(\d+)\s*号', code or '')
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


def latest_notice_code(items):
    """列表中最新公告号（按年号+期号）。"""
    if not items:
        return ''
    best = max(items, key=lambda it: notice_code_sort_key(it.get('code') or ''))
    return (best.get('code') or '').strip()


def load_list_cache():
    """读取本地公告列表缓存。返回 dict 或 None。"""
    try:
        if not os.path.isfile(_CACHE_FILE):
            return None
        with open(_CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            return None
        if int(data.get('version') or 0) != CACHE_VERSION:
            logger.info("[国标公告] 缓存版本过旧，忽略")
            return None
        return data
    except Exception as e:
        logger.warning(f"[国标公告] 读缓存失败: {e}")
        return None


def save_list_cache(items, latest_code=''):
    """写入本地公告列表缓存（仅保留展示用最新 N 条）。

    latest_code 应为源站最新公告号（含被剔除的无工程相关公告），用于后台增量探测。
    """
    try:
        os.makedirs(TEMP_DIR, exist_ok=True)
        payload = {
            'version': CACHE_VERSION,
            'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'latest_code': latest_code or latest_notice_code(items),
            'items': list(items or [])[:LIST_DISPLAY_LIMIT],
        }
        with open(_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(
            f"[国标公告] 已缓存 {len(payload['items'])} 条，源站最新={payload['latest_code']}"
        )
    except Exception as e:
        logger.warning(f"[国标公告] 写缓存失败: {e}")


def fetch_list_candidates(session, page=1, page_size=None, search_text=''):
    """拉取公告候选列表（排除外文版/样品），按公告号新→旧，最多 LIST_CANDIDATE_LIMIT 条。

    返回: (candidates, total_raw)
    """
    page_size = page_size or LIST_FETCH_PAGE_SIZE
    url = (
        f"{NOC_LIST_URL}?searchText={quote(search_text or '')}"
        f"&pageNumber={int(page)}&pageSize={int(page_size)}"
    )
    logger.info(f"[国标公告] 列表: {url}")
    last_err = None
    resp = None
    for attempt in range(3):
        try:
            resp = _http_request(session, 'get', url, timeout=Config.TIMEOUT)
            if resp.status_code == 200:
                break
            last_err = RuntimeError(f"公告列表请求失败 HTTP {resp.status_code}")
            logger.warning(f"[国标公告] HTTP {resp.status_code}，重试 {attempt + 1}/3")
            time.sleep(0.8 * (attempt + 1))
        except Exception as e:
            last_err = e
            logger.warning(f"[国标公告] 请求异常，重试 {attempt + 1}/3: {e}")
            time.sleep(0.8 * (attempt + 1))
    if resp is None or resp.status_code != 200:
        raise RuntimeError(str(last_err) if last_err else '公告列表请求失败')

    data = json.loads(resp.content.decode('utf-8', 'replace'))
    rows = data.get('rows') or []
    total = int(data.get('total') or 0)
    items = []
    for row in rows:
        title = (row.get('TITLE') or '').strip()
        if is_excluded_announcement_title(title):
            continue
        code = (row.get('CODE') or '').strip()
        pid = (row.get('PID') or '').strip()
        if not pid:
            continue
        items.append({
            'code': code,
            'title': title,
            'date': (row.get('NOTICE_DATE') or '').strip(),
            'pid': pid,
            'detail_url': NOC_DETAIL_URL.format(pid=pid),
            'display_title': f"{code}中国国家标准公告" if code else title,
        })
    items.sort(key=lambda it: notice_code_sort_key(it.get('code') or ''), reverse=True)
    return items[:LIST_CANDIDATE_LIMIT], total


def filter_civil_announcements(session, candidates, limit=None):
    """逐条解析详情，仅保留工程相关标准数 > 0 的公告，取最新 limit 条。

    同时把工程相关标准明细写入条目，供本地缓存，避免再次点开时重算。
    """
    limit = LIST_DISPLAY_LIMIT if limit is None else int(limit)
    kept = []
    for it in candidates or []:
        if len(kept) >= limit:
            break
        code = it.get('code') or ''
        pid = it.get('pid') or ''
        try:
            detail = fetch_announcement_detail(session, pid)
            civil_n = int(detail.get('civil_count') or 0)
            if civil_n <= 0:
                logger.info(f"[国标公告] 剔除无工程相关：{code}")
                continue
            row = dict(it)
            row['civil_count'] = civil_n
            row['raw_count'] = int(detail.get('raw_count') or 0)
            row['standards'] = list(detail.get('standards') or [])
            row['detail_heading'] = detail.get('title') or ''
            if detail.get('display_title'):
                row['display_title'] = detail.get('display_title')
            kept.append(row)
            logger.info(f"[国标公告] 保留 {code}（工程相关 {civil_n}，已写入缓存明细）")
        except Exception as e:
            logger.warning(f"[国标公告] 解析 {code} 失败，跳过: {e}")
    return kept


def detail_from_cached_item(item):
    """若列表项已含工程相关明细，组装详情 dict；否则返回 None。"""
    if not item or not isinstance(item, dict):
        return None
    standards = item.get('standards')
    if not isinstance(standards, list) or not standards:
        return None
    return {
        'code': item.get('code') or '',
        'title': item.get('detail_heading') or item.get('title') or '',
        'display_title': item.get('display_title') or '',
        'detail_url': item.get('detail_url') or '',
        'standards': standards,
        'raw_count': int(item.get('raw_count') or 0),
        'civil_count': int(item.get('civil_count') or len(standards)),
        'from_cache': True,
    }


def load_cached_detail_by_pid(pid):
    """从本地列表缓存中按 PID 取详情。"""
    pid = (pid or '').strip()
    if not pid:
        return None
    cache = load_list_cache()
    if not cache:
        return None
    for it in cache.get('items') or []:
        if (it.get('pid') or '').strip() == pid:
            return detail_from_cached_item(it)
    return None


def get_announcement_detail(session, pid, list_item=None):
    """优先本地缓存明细，没有再联网解析。"""
    cached = detail_from_cached_item(list_item) if list_item else None
    if cached:
        logger.info(f"[国标公告] 详情命中内存/列表缓存: {pid}")
        return cached
    cached = load_cached_detail_by_pid(pid)
    if cached:
        logger.info(f"[国标公告] 详情命中本地文件缓存: {pid}")
        return cached
    return fetch_announcement_detail(session, pid)


def fetch_announcement_list(session, page=1, page_size=None, search_text=''):
    """拉取并过滤：排除外文版/样品，再排除工程相关为 0，截取最新 LIST_DISPLAY_LIMIT 条。

    返回: (items, total_raw, site_latest_code)
    """
    candidates, total = fetch_list_candidates(
        session, page=page, page_size=page_size, search_text=search_text
    )
    site_latest = latest_notice_code(candidates)
    items = filter_civil_announcements(session, candidates)
    return items, total, site_latest


def check_list_needs_refresh(session, cached_latest_code):
    """后台探测：源站最新公告号新于缓存时才拉详情过滤。

    返回: (need_refresh, fresh_items_or_None, site_latest_code)
    """
    candidates, _total = fetch_list_candidates(session)
    remote_latest = latest_notice_code(candidates)
    cached = (cached_latest_code or '').strip()
    if not cached:
        items = filter_civil_announcements(session, candidates)
        return True, items, remote_latest
    if notice_code_sort_key(remote_latest) > notice_code_sort_key(cached):
        logger.info(f"[国标公告] 发现更新：缓存={cached} → 源站={remote_latest}")
        items = filter_civil_announcements(session, candidates)
        return True, items, remote_latest
    logger.info(f"[国标公告] 无更新（源站最新仍为 {remote_latest or cached}）")
    return False, None, remote_latest


def fetch_announcement_detail(session, pid):
    """拉取单条公告详情，仅保留土建/建材相关标准行。

    返回 dict: code, title, date, detail_url, standards(list), raw_count, civil_count
    """
    pid = (pid or '').strip()
    if not pid:
        raise ValueError('缺少公告 PID')
    url = NOC_DETAIL_URL.format(pid=pid)
    logger.info(f"[国标公告] 详情: {url}")
    resp = _http_request(session, 'get', url, timeout=Config.TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"公告详情请求失败 HTTP {resp.status_code}")

    html = resp.content.decode('utf-8', 'replace')
    soup = BeautifulSoup(html, 'html.parser')

    # 标题：优先 h 标签或含「年第」文案
    page_title = ''
    for tag in soup.find_all(['h1', 'h2', 'h3', 'h4']):
        t = tag.get_text(strip=True)
        if '公告' in t or '年第' in t:
            page_title = t
            break
    if not page_title and soup.title:
        page_title = soup.title.get_text(strip=True)

    notice_code = ''
    m = re.search(r'(\d{4}年第\d+号)', html)
    if m:
        notice_code = m.group(1)

    # 公告正文标题（关于批准发布…）
    notice_heading = ''
    for p in soup.find_all(['p', 'div', 'h3', 'h4']):
        t = p.get_text(strip=True)
        if t.startswith('关于') and ('标准' in t or '公告' in t):
            notice_heading = t
            break

    standards_all = _parse_detail_standards(soup)
    standards = [s for s in standards_all if is_civil_std_name(s.get('std_name', ''))]

    display = f"{notice_code}中国国家标准公告" if notice_code else (page_title or '国家标准公告')
    return {
        'code': notice_code,
        'title': notice_heading or page_title,
        'display_title': display,
        'detail_url': url,
        'standards': standards,
        'raw_count': len(standards_all),
        'civil_count': len(standards),
    }


def _parse_detail_standards(soup):
    """从详情页表格解析全部标准行。列：序号 标准编号 标准名称 代替标准号 实施日期。"""
    results = []
    table = soup.find('table', class_=re.compile(r'table'))
    if not table:
        tables = soup.find_all('table')
        table = tables[0] if tables else None
    if not table:
        return results

    for tr in table.find_all('tr'):
        tds = tr.find_all('td')
        if len(tds) < 3:
            continue
        cells = [td.get_text(' ', strip=True) for td in tds]
        # 跳过表头
        if cells[0] in ('序号', 'No', '编号') or '标准编号' in ''.join(cells[:3]):
            continue
        # 常见：序号, 标准编号, 标准名称, 代替标准号, 实施日期
        # 有的名称列重复英文/中文，取第一个非空名称列
        std_no = ''
        std_name = ''
        replace_no = ''
        implement_date = ''
        for c in cells:
            if not std_no and re.match(r'^[A-Z]{1,10}(?:/[A-Z]+)?\s*[\d.]+(?:-\d{4})?$', c, re.I):
                std_no = c
                continue
            if std_no and not std_name and re.search(r'[\u4e00-\u9fff]', c) and '代替' not in c:
                # 避免把「代替标准号」列当名称：若整格像标准号列表则跳过
                if re.match(r'^[A-Z0-9/.,\s\-]+$', c, re.I) and re.search(r'\d{4}', c):
                    replace_no = c
                    continue
                # 页面常重复中英文/中文名称，去重
                if ' ' in c:
                    left, right = c.split(' ', 1)
                    if left == right:
                        c = left
                elif len(c) >= 4 and len(c) % 2 == 0:
                    half = len(c) // 2
                    if c[:half] == c[half:]:
                        c = c[:half]
                std_name = c
                continue
            if std_name and not replace_no and re.search(r'GB|JGJ|CJJ|JTG|SL|JC', c, re.I):
                replace_no = c
                continue
            if re.match(r'^\d{4}-\d{2}-\d{2}$', c):
                implement_date = c

        if not std_no:
            # 兜底按固定列位
            if len(cells) >= 3 and re.search(r'[A-Za-z]', cells[1]):
                std_no = cells[1]
                std_name = cells[2] if len(cells) > 2 else ''
                replace_no = cells[3] if len(cells) > 3 else ''
                implement_date = cells[4] if len(cells) > 4 else ''
            else:
                continue
        if not std_name:
            continue
        results.append({
            'std_no': std_no,
            'std_name': std_name,
            'replace_no': replace_no,
            'implement_date': implement_date,
        })
    return results
