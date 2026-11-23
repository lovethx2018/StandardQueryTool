# -*- coding: utf-8 -*-
"""汉字标准名称模糊查询（独立模块）。

仅当输入为纯汉字名称时由 search_standard 分流至此；编号查询原路径不受影响。
规则：名称模糊匹配 → 丢弃废止/作废 → 按编号系列收敛（每系列仅现行/即将实施）→
按相关度排序，最多约 40 条（产品标准、试验方法等可多系列并存）。
相关度偏向建筑工程、公路工程、市政工程、水利工程等领域标准；
若存在该领域命中，则过滤非工程领域噪声。
查询源优先级见 Config.CHINESE_NAME_SITES（与编号查询 PRIMARY_SITES 独立）。
与批量查询一致：不提供 PDF 下载（结果标记为「不启用」）。
"""
import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from app_env import logger
from config import Config
from models import StandardInfo
from http_client import _http_request
from utils import (
    is_obsolete_status,
    normalize_status_by_date,
    _extract_base_key,
)
from .civil_domain import civil_engineering_boost


_CJK_RE = re.compile(r'[\u4e00-\u9fff]')
_LATIN_RE = re.compile(r'[A-Za-z]')


def is_chinese_name_query(keyword):
    """是否走汉字名称查询路径。

    含汉字，且不以数字开头、不含拉丁字母 → True；其余走原编号查询。
    """
    if not keyword:
        return False
    kw = keyword.strip()
    if not kw:
        return False
    if kw[:1].isdigit():
        return False
    if _LATIN_RE.search(kw):
        return False
    return bool(_CJK_RE.search(kw))


def fuzzy_name_match(keyword, std_name):
    """名称模糊匹配：整段包含，或关键词字符在名称中按序出现。"""
    kw = (keyword or '').strip()
    name = (std_name or '').strip()
    if not kw or not name:
        return False
    if len(kw) < 2:
        return False
    if kw in name:
        return True
    return _ordered_subsequence(kw, name)


def series_relevance(keyword, std_name, std_no=''):
    """系列相关度：子串命中优先；名称以关键词结尾（核心品名）再加权。

    例：「沥青防水卷材」在「弹性体改性沥青防水卷材」中为尾部核心词，
    应优于「沥青防水卷材用胎基」（关键词后还有限定成分）。
    同类后缀命中时用年号轻微加权，偏向较新系列。
    另对建筑/公路/市政/水利等领域标准额外加权。
    """
    kw = (keyword or '').strip()
    name = (std_name or '').strip()
    if not kw or not name or not fuzzy_name_match(kw, name):
        return 0.0
    if kw in name:
        # 子串：关键词越长、名称越贴近关键词 → 分越高
        score = 1000.0 + len(kw) * 10.0 - abs(len(name) - len(kw)) * 0.5
        if name.endswith(kw):
            score += 200.0  # 关键词即品名核心（后缀）
        elif name.startswith(kw) and len(name) > len(kw):
            score -= 30.0   # 关键词后还有「用…」等限定，略降
    else:
        # 序匹配：覆盖率 = 关键词长度 / 名称长度
        score = 100.0 + (len(kw) / max(len(name), 1)) * 50.0 + len(kw)

    ym = re.search(r'[-—:：](\d{4})$', (std_no or '').strip())
    if ym:
        score += int(ym.group(1)) * 0.1

    score += civil_engineering_boost(std_no, name)
    return score


# 汉字名称查询结果上限（按相关度排序后截断）
_MAX_DISPLAY_ROWS = 40


def _parse_std_family(std_no):
    """解析编号族与分册号，便于同系列排在一起。

    例：'GB/T 328.11-2007' → ('GB328', 11.0, '2007')
        'GB/T 18242-2025'  → ('GB18242', -1.0, '2025')  # 无分册
    """
    s = (std_no or '').strip()
    m = re.match(
        r'^([A-Za-z]+\d*(?:/[A-Za-z]+)?)\s*(\d+)(?:\.(\d+(?:\.\d+)*))?\s*[-—:：]\s*(\d{4})',
        s,
    )
    if not m:
        key = re.sub(r'\s+', '', s).upper()
        return key or '_', -1.0, ''
    prefix = m.group(1).upper().replace(' ', '')
    # GB/T 与 GB 同族归并
    prefix = re.sub(r'/T$', '', prefix)
    family = f"{prefix}{m.group(2)}"
    part = float(m.group(3)) if m.group(3) else -1.0
    year = m.group(4) or ''
    return family, part, year


def _sort_grouped_by_std_family(results, row_scores):
    """展示排序：无分册产品按相关度在前；同分册族（如 328.x）整组排在一起并按分册号升序。"""
    if not results:
        return results

    fam_scores = {}
    fam_is_multipart = {}
    for r in results:
        fam, part, _year = _parse_std_family(getattr(r, 'std_no', '') or '')
        sc = float(row_scores.get(id(r), 0) or 0)
        fam_scores[fam] = max(fam_scores.get(fam, 0.0), sc)
        if part >= 0:
            fam_is_multipart[fam] = True

    def sort_key(r):
        fam, part, year = _parse_std_family(getattr(r, 'std_no', '') or '')
        # 0=独立标准/产品，1=分册系列（试验方法等）——先产品后分册组
        bucket = 1 if fam_is_multipart.get(fam) else 0
        return (
            bucket,
            -fam_scores.get(fam, 0.0),
            fam,
            part if part >= 0 else -1.0,
            year,
        )

    return sorted(results, key=sort_key)


def _truncate_at_family_boundary(results, limit):
    """截断到 limit，不在同一编号族（如 328.x）中间切开。"""
    if len(results) <= limit:
        return results
    cut = []
    i = 0
    while i < len(results):
        fam, _, _ = _parse_std_family(getattr(results[i], 'std_no', '') or '')
        j = i + 1
        while j < len(results):
            fam2, _, _ = _parse_std_family(getattr(results[j], 'std_no', '') or '')
            if fam2 != fam:
                break
            j += 1
        chunk = results[i:j]
        if cut and len(cut) + len(chunk) > limit:
            break
        cut.extend(chunk)
        i = j
        if len(cut) >= limit:
            break
    return cut


def collapse_best_series(results, keyword):
    """去废止 → 工程领域优先 → 每系列仅现行/即将实施 → 同系列聚拢排序后限量展示。

    最多保留约 _MAX_DISPLAY_ROWS 条；同分册（如 GB/T 328.x）排在一起并按分册号排序。
    """
    if not results:
        return []

    kw = (keyword or '').strip()
    list_order = {id(r): i for i, r in enumerate(results)}
    pos_weight = 5.0 if len(kw) <= 2 else 1.5

    active = [
        r for r in results
        if not is_obsolete_status(getattr(r, 'status', '') or '')
    ]
    if not active:
        logger.info("[汉字名称查询] 过滤后无现行/即将实施，返回空")
        return []

    groups = {}
    for r in active:
        key = _extract_base_key(getattr(r, 'std_no', '') or '')
        if not key:
            key = (getattr(r, 'std_no', '') or '').replace(' ', '').upper() or id(r)
        groups.setdefault(key, []).append(r)

    scored = []
    for key, items in groups.items():
        score = max(
            series_relevance(
                keyword,
                getattr(r, 'std_name', '') or '',
                getattr(r, 'std_no', '') or '',
            )
            - list_order.get(id(r), 999) * pos_weight
            for r in items
        )
        civil = max(
            civil_engineering_boost(
                getattr(r, 'std_no', '') or '',
                getattr(r, 'std_name', '') or '',
            )
            for r in items
        )
        if score > 0:
            scored.append((key, items, score, civil))

    if not scored:
        return []

    # 有建筑工程等领域命中时，丢掉非工程噪声（如油墨/化妆品细度）
    if any(c > 0 for *_, c in scored):
        scored = [x for x in scored if x[3] > 0]

    # 先按相关度取候选，再聚拢排序
    scored.sort(key=lambda x: -x[2])

    kept_all = []
    row_scores = {}
    for key, items, score, civil in scored:
        series_kept = _keep_current_and_pending(items)
        if not series_kept:
            continue
        for r in series_kept:
            row_scores[id(r)] = score
        kept_all.extend(series_kept)

    kept_all = _sort_grouped_by_std_family(kept_all, row_scores)
    kept_all = _truncate_at_family_boundary(kept_all, _MAX_DISPLAY_ROWS)

    logger.info(
        f"[汉字名称查询] 同系列聚拢后保留 {len(kept_all)} 条（上限 {_MAX_DISPLAY_ROWS}）: "
        f"{[getattr(r, 'std_no', '') for r in kept_all]}"
    )
    return kept_all


def _keep_current_and_pending(series):
    """同一编号系列内只留现行与即将实施。"""
    kept = []
    for r in series:
        status = getattr(r, 'status', '') or ''
        if is_obsolete_status(status):
            continue
        if '即将实施' in status or '现行' in status or not status:
            kept.append(r)
    if not kept:
        kept = [
            r for r in series
            if not is_obsolete_status(getattr(r, 'status', '') or '')
        ]

    def _year_key(std):
        m = re.search(r'[-—:：](\d{4})$', (getattr(std, 'std_no', '') or '').strip())
        return m.group(1) if m else ''

    kept.sort(key=_year_key)
    return kept


def search(engine, keyword, check_download=True):
    """汉字名称查询入口（由 SpiderEngine.search_standard 分流调用）。

    与批量查询一致：不探测、不提供下载（结果一律 download_status=不启用）。
    """
    keyword = (keyword or '').strip()
    logger.info(f"[汉字名称查询] 开始: {keyword!r}")
    # 汉字名称查询固定关闭下载探测（与批量一致；忽略调用方 check_download）
    check_download = False
    if hasattr(engine, 'progress_updated'):
        engine.progress_updated.emit(f"正在按名称检索: {keyword}")

    results = []
    seen_nos = set()
    site_variants = _site_query_variants(keyword)
    # 汉字专用优先级（Config.CHINESE_NAME_SITES），与编号查询 PRIMARY_SITES 分开
    fetchers = {
        'csres': _fetch_csres,
        'std_samr': _fetch_samr,
        'openstd': _fetch_openstd,
        'aijsti': _fetch_aijsti,
        'nrsis': _fetch_nrsis,
        'jtst': _fetch_jtst,
        'mwr': _fetch_mwr,
    }
    for site in getattr(Config, 'CHINESE_NAME_SITES', None) or []:
        if getattr(engine, '_cancelled', False):
            break
        parser = site.get('parser') or ''
        # 自然资源网为名称查询备用：前面站点已有命中则跳过
        if parser == 'nrsis' and results:
            logger.info("[汉字名称查询] 已有结果，跳过备用站点: 自然资源网")
            continue
        fetcher = fetchers.get(parser)
        if not fetcher:
            continue
        fetcher_name = site.get('name') or parser
        site_hit = False
        for site_kw in site_variants:
            try:
                if hasattr(engine, '_check_cancelled'):
                    engine._check_cancelled()
                if hasattr(engine, 'progress_updated'):
                    tip = fetcher_name if site_kw == keyword else f"{fetcher_name}({site_kw})"
                    engine.progress_updated.emit(f"正在检索: {tip}")
                site_rows = fetcher(engine, site_kw, site)
                matched = [
                    r for r in site_rows
                    if fuzzy_name_match(keyword, getattr(r, 'std_name', '') or '')
                ]
                matched = [
                    r for r in matched
                    if engine._is_prefix_allowed(getattr(r, 'std_no', '') or '')
                    and _is_plausible_name_result(r)
                    and not engine._is_status_blocked(getattr(r, 'status', '') or '')
                ]
                logger.info(
                    f"[汉字名称查询][{fetcher_name}] 站词={site_kw!r} "
                    f"名称命中 {len(matched)} 条"
                )
                if matched:
                    for r in matched:
                        key = (getattr(r, 'std_no', '') or '').replace(' ', '').upper()
                        if key and key not in seen_nos:
                            seen_nos.add(key)
                            results.append(r)
                    site_hit = True
                    break  # 本站已命中，换下一站补全
            except Exception as e:
                logger.warning(f"[汉字名称查询][{fetcher_name}] 站词={site_kw!r} 失败: {e}")
                continue
        if not site_hit:
            continue

    if not results:
        logger.info("[汉字名称查询] 无命中")
        return []

    results = normalize_status_by_date(results)
    results = collapse_best_series(results, keyword)

    # 工标网列表无发布日期、部分条目仅列表补全：详情页补齐缺省日期
    if results and not getattr(engine, '_cancelled', False):
        if hasattr(engine, 'progress_updated'):
            engine.progress_updated.emit('正在补全发布/实施日期…')
        _enrich_missing_dates(engine, results)

    if check_download and results and not getattr(engine, '_cancelled', False):
        try:
            if hasattr(engine, '_check_download_sources'):
                engine._check_download_sources(results)
        except Exception as e:
            logger.warning(f"[汉字名称查询] 下载源检测异常: {e}")

    # 与批量查询一致：一律不启用下载（覆盖解析阶段可能写入的「可下载」）
    for r in results:
        r.download_status = "不启用"
        r.download_sources = []
        repl = getattr(r, 'replacement', None)
        if repl is not None:
            repl.download_status = "不启用"
            repl.download_sources = []

    return results


def _is_plausible_name_result(std):
    """名称查询用脏数据过滤：试验方法全称常超过 60 字，放宽上限。"""
    name = (getattr(std, 'std_name', '') or '').strip()
    if len(name) > 120:
        return False
    if name.count('标准') >= 3:
        return False
    return True


def _site_query_variants(keyword):
    """站点检索用词：原文优先，再尝试从尾部截取的品名核心（源站对过长短语常无结果）。"""
    kw = (keyword or '').strip()
    if not kw:
        return []
    variants = [kw]
    # 后缀长度从 len-1 降到 4（至少约「防水卷材」量级）
    for n in range(len(kw) - 1, 3, -1):
        suffix = kw[-n:]
        if suffix not in variants:
            variants.append(suffix)
    return variants


def _ordered_subsequence(kw, name):
    """判断 kw 的每个字符是否按顺序出现在 name 中。"""
    i = 0
    for ch in name:
        if i < len(kw) and ch == kw[i]:
            i += 1
            if i >= len(kw):
                return True
    return False


def _resolve_site(parser, site=None):
    """优先用传入的汉字站点配置，否则从 CHINESE_NAME_SITES / PRIMARY_SITES 查找。"""
    if site and site.get('parser') == parser:
        return site
    for s in getattr(Config, 'CHINESE_NAME_SITES', []) or []:
        if s.get('parser') == parser:
            return s
    return next((s for s in Config.PRIMARY_SITES if s.get('parser') == parser), None)


def _fetch_aijsti(engine, keyword, site=None):
    """爱查新 API 列表（名称查询用，不做编号过滤）。"""
    site = _resolve_site('aijsti', site)
    if not site:
        return []
    spider = engine.spider_manager.get('aijsti')
    if spider is None:
        logger.warning("[汉字名称查询][爱查新] 爬虫未注册")
        return []
    try:
        rows = spider.search(keyword, site) or []
        logger.info(f"[汉字名称查询][爱查新] 返回 {len(rows)} 条")
        return rows
    except Exception as e:
        logger.warning(f"[汉字名称查询][爱查新] 失败: {e}")
        return []


def _fetch_nrsis(engine, keyword, site=None):
    """自然资源网列表（名称查询用）。"""
    site = _resolve_site('nrsis', site)
    if not site:
        return []
    spider = engine.spider_manager.get('nrsis')
    if spider is None:
        logger.warning("[汉字名称查询][自然资源网] 爬虫未注册")
        return []
    try:
        rows = spider.search(keyword, site) or []
        logger.info(f"[汉字名称查询][自然资源网] 返回 {len(rows)} 条")
        return rows
    except Exception as e:
        logger.warning(f"[汉字名称查询][自然资源网] 失败: {e}")
        return []


def _fetch_openstd(engine, keyword, site=None):
    """请求国家标准全文列表，解析全部行（不做编号匹配）。"""
    site = _resolve_site('openstd', site)
    if not site:
        return []
    url = site['search_url'].format(keyword=quote(keyword))
    logger.info(f"[汉字名称查询][国家标准全文] URL: {url}")
    resp = _http_request(engine.session, 'get', url, timeout=Config.TIMEOUT)
    if resp.status_code != 200:
        logger.warning(f"[汉字名称查询][国家标准全文] HTTP {resp.status_code}")
        return []
    return _parse_openstd_rows(resp.text)


def _parse_openstd_rows(html):
    results = []
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table', class_='result_list')
    if not table:
        return results
    for row in table.find_all('tr')[1:]:
        cells = row.find_all('td')
        if len(cells) < 7:
            continue
        std_no = cells[1].get_text(strip=True)
        if not std_no:
            continue
        std_name = cells[4].get_text(strip=True) if len(cells) > 4 else ''
        status = cells[6].get_text(strip=True) if len(cells) > 6 else ''
        publish_date = cells[7].get_text(strip=True) if len(cells) > 7 else ''
        implement_date = cells[8].get_text(strip=True) if len(cells) > 8 else ''
        if publish_date and not re.search(r'\d{4}', publish_date):
            publish_date = ''
        if implement_date and not re.search(r'\d{4}', implement_date):
            implement_date = ''

        hcno = None
        for a in cells[1].find_all('a', href=True):
            onclick = a.get('onclick', '') or ''
            m = re.search(r"showInfo\(['\"]([^'\"]+)['\"]\)", onclick)
            if m:
                hcno = m.group(1)
                break

        adopt_text = cells[3].get_text(strip=True) if len(cells) > 3 else ''
        is_adopted = '采' in adopt_text

        std = StandardInfo(
            std_no=std_no,
            std_name=std_name,
            status=status,
            publish_date=publish_date,
            implement_date=implement_date,
            source_site='国家标准全文',
        )
        std.is_adopted = is_adopted
        if hcno:
            std.detail_url = (
                f"https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno={hcno}&refer=outer"
            )
        if hcno and not is_adopted:
            std.download_url = f"https://openstd.samr.gov.cn/bzgk/std/viewGb?hcno={hcno}"
            std.download_status = '可下载'
            std.download_sources = ['openstd']
        elif is_adopted:
            std.download_url = ''
            std.download_sources = []
            std.download_status = '未检测'
        results.append(std)
    return results


def _fetch_csres(engine, keyword, site=None):
    """请求工标网列表（首页即可，名称查询不做全部分页展开）。"""
    site = _resolve_site('csres', site)
    if not site:
        return []
    try:
        encoded = quote(keyword.encode('gb2312'))
    except (UnicodeEncodeError, LookupError):
        encoded = quote(keyword)
    url = site['search_url'].format(keyword=encoded)
    logger.info(f"[汉字名称查询][工标网] URL: {url}")
    resp = _http_request(engine.session, 'get', url, timeout=Config.TIMEOUT)
    if resp.status_code != 200:
        logger.warning(f"[汉字名称查询][工标网] HTTP {resp.status_code}")
        return []
    return _parse_csres_rows(resp.text)


def _parse_csres_rows(html):
    """解析工标网列表。列：标准编号 / 标准名称 / 发布部门 / 实施日期 / 状态。

    列表无「发布日期」列，仅有实施日期；发布日期需详情页补全。
    """
    results = []
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table', class_='heng')
    if not table:
        return results
    for tr in table.find_all('tr'):
        tds = tr.find_all('td')
        if len(tds) < 5:
            continue
        a_tag = tds[0].find('a')
        if not a_tag or not a_tag.get('href'):
            continue
        std_no = a_tag.get_text(strip=True)
        if not std_no:
            continue
        std_name = tds[1].get_text(strip=True) if len(tds) >= 2 else ''
        implement_date = tds[3].get_text(strip=True) if len(tds) >= 4 else ''
        if implement_date and not re.search(r'\d{4}', implement_date):
            implement_date = ''
        status = tds[4].get_text(strip=True) if len(tds) >= 5 else ''
        detail_url = urljoin('http://www.csres.com/', a_tag['href'])
        std = StandardInfo(
            std_no=std_no,
            std_name=std_name,
            status=status,
            implement_date=implement_date,
            source_site='工标网',
        )
        std.detail_url = detail_url
        results.append(std)
    return results


def _fetch_samr(engine, keyword, site=None):
    """全国标准信息公共服务平台：解析全部结果卡片（不做编号匹配）。"""
    site = _resolve_site('std_samr', site)
    iframe_url = f"https://std.samr.gov.cn/search/stdPage?q={quote(keyword)}&tid="
    logger.info(f"[汉字名称查询][全国标准信息公共服务平台] URL: {iframe_url}")
    resp = _http_request(engine.session, 'get', iframe_url, timeout=Config.TIMEOUT)
    if resp.status_code != 200:
        logger.warning(f"[汉字名称查询][全国标准信息公共服务平台] HTTP {resp.status_code}")
        return []
    return _parse_samr_like_rows(
        resp.text,
        source_name=(site or {}).get('name') or '全国标准信息公共服务平台',
        detail_builder=_samr_detail_url,
    )


def _samr_detail_url(pid, tid):
    if not pid:
        return 'https://std.samr.gov.cn/'
    if tid == 'BV_HB':
        return f'https://std.samr.gov.cn/hb/search/stdHBDetailed?id={pid}'
    if tid == 'BV_DB':
        return f'https://std.samr.gov.cn/db/search/stdDBDetailed?id={pid}'
    return f'https://std.samr.gov.cn/gb/search/gbDetailed?id={pid}'


def _fetch_jtst(engine, keyword, site=None):
    """交通运输部标准化信息系统：解析全部结果卡片。"""
    site = _resolve_site('jtst', site)
    iframe_url = f"https://jtst.mot.gov.cn/search/stdPage?q={quote(keyword)}&tid="
    logger.info(f"[汉字名称查询][交通运输部] URL: {iframe_url}")
    resp = _http_request(engine.session, 'get', iframe_url, timeout=Config.TIMEOUT)
    if resp.status_code != 200:
        logger.warning(f"[汉字名称查询][交通运输部] HTTP {resp.status_code}")
        return []

    def _jtst_detail(pid, tid):
        return f"https://jtst.mot.gov.cn/search/std?q={quote(keyword)}"

    return _parse_samr_like_rows(
        resp.text,
        source_name=(site or {}).get('name') or '交通运输部标准化信息系统',
        detail_builder=_jtst_detail,
        normalize_jtg=True,
    )


def _parse_samr_like_rows(html, source_name, detail_builder, normalize_jtg=False):
    """解析标信/交通部同构结果页：返回全部卡片，不按编号过滤。"""
    results = []
    soup = BeautifulSoup(html, 'html.parser')
    posts = soup.find_all('div', class_=['panel', 'post'])
    if not posts:
        posts = soup.find_all('div', class_='post')
    for post in posts:
        en_code_span = post.find('span', class_='en-code')
        if not en_code_span:
            continue
        raw_no = en_code_span.get_text(strip=False).replace('\n', '')
        if normalize_jtg:
            raw_compact = raw_no.replace(' ', '')
            m_norm = re.match(
                r'^([A-Za-z]+(?:/[A-Za-z]+)?)[\s]*([\d.].*)$',
                raw_compact,
            )
            std_no = f"{m_norm.group(1)} {m_norm.group(2)}" if m_norm else raw_no.strip()
        else:
            std_no = raw_no.replace(' ', '').strip()
            # 还原常见空格：GB/T1345-2005 → 尽量保持可读
            m = re.match(r'^([A-Za-z]+(?:/[A-Za-z]+)?)([\d.].*)$', std_no)
            if m:
                std_no = f"{m.group(1)} {m.group(2)}"
        if not std_no:
            continue

        status_tag = post.find('span', class_='s-status')
        status = status_tag.get_text(strip=True) if status_tag else ''
        is_adopted = any(
            (s.get_text(strip=True) or '') == '采'
            for s in post.find_all('span', class_=True)
            if 's-status' in (s.get('class') or [])
        )

        a_tag = post.find('a', attrs={'pid': True}) or post.find('a', href=True)
        pid = a_tag.get('pid', '') if a_tag else ''
        tid = a_tag.get('tid', '') if a_tag else ''

        std_name = ''
        if a_tag:
            name_parts = []
            for child in a_tag.children:
                if hasattr(child, 'get') and child.get('class') and 'en-code' in child.get('class', []):
                    continue
                txt = child if isinstance(child, str) else child.get_text()
                name_parts.append(txt)
            std_name = ''.join(name_parts).strip()
            if normalize_jtg and std_name:
                std_name = re.sub(
                    r'(JTG(?:\s*/\s*T)?|JTJ)[\s\-]*[\d.]+(?:-\d{4})?',
                    '',
                    std_name,
                    count=1,
                    flags=re.IGNORECASE,
                ).strip(' \t-—|·')[:100]

        publish_date = ''
        implement_date = ''
        time_tags = post.find_all('time')
        if len(time_tags) >= 1:
            publish_date = time_tags[0].get_text(strip=True)
        if len(time_tags) >= 2:
            implement_date = time_tags[1].get_text(strip=True)

        std = StandardInfo(
            std_no=std_no,
            std_name=std_name,
            status=status,
            publish_date=publish_date,
            implement_date=implement_date,
            source_site=source_name,
        )
        std.is_adopted = bool(is_adopted)
        std.detail_url = detail_builder(pid, tid)
        results.append(std)
    return results


def _fetch_mwr(engine, keyword, site=None):
    """水利部：解析全部表格行（不做编号匹配）。"""
    site = _resolve_site('mwr', site)
    if not site:
        return []
    url = site['search_url'].format(keyword=quote(keyword))
    logger.info(f"[汉字名称查询][水利部] URL: {url}")
    resp = _http_request(engine.session, 'get', url, timeout=Config.TIMEOUT)
    if resp.status_code != 200:
        logger.warning(f"[汉字名称查询][水利部] HTTP {resp.status_code}")
        return []
    return _parse_mwr_rows(resp.text, (site or {}).get('name') or '水利部国际合作与科技司', keyword)


def _parse_mwr_rows(html, source_name, keyword):
    results = []
    soup = BeautifulSoup(html, 'html.parser')
    for table in soup.find_all('table'):
        for row in table.find_all('tr')[1:]:
            cells = row.find_all('td')
            if len(cells) < 2:
                continue
            cell_text = cells[0].get_text(strip=True)
            m = re.search(r'(SL|SL/T)[\s/-]*([\d.]+(?:-\d{4})?)', cell_text)
            if not m:
                continue
            std_no = f"{m.group(1)} {m.group(2).replace(' ', '')}"
            std_name = cells[1].get_text(strip=True)[:100]
            std = StandardInfo(
                std_no=std_no,
                std_name=std_name,
                source_site=source_name,
            )
            std.download_url = f"http://gjkj.mwr.gov.cn/jsjd1/bzcx/?q={quote(keyword)}"
            std.download_status = '不可下载'
            results.append(std)
    return results


def _enrich_missing_dates(engine, results):
    """对缺发布/实施日期的结果，用工标网详情页补全（仅处理已收敛后的展示列表）。"""
    for r in results:
        if getattr(engine, '_cancelled', False):
            break
        need_pub = not (getattr(r, 'publish_date', '') or '').strip()
        need_impl = not (getattr(r, 'implement_date', '') or '').strip()
        if not need_pub and not need_impl:
            continue
        url = (getattr(r, 'detail_url', '') or '').strip()
        if 'csres.com' not in url:
            continue
        try:
            if hasattr(engine, '_check_cancelled'):
                engine._check_cancelled()
            pub, impl = _fetch_csres_detail_dates(engine, url)
            if need_pub and pub:
                r.publish_date = pub
            if need_impl and impl:
                r.implement_date = impl
        except Exception as e:
            logger.warning(
                f"[汉字名称查询] 补全日期失败 {getattr(r, 'std_no', '')}: {e}"
            )


def _fetch_csres_detail_dates(engine, detail_url):
    """从工标网详情页提取发布日期、实施日期。"""
    resp = _http_request(
        engine.session, 'get', detail_url, timeout=Config.TIMEOUT,
        headers={'Referer': 'http://www.csres.com/'},
    )
    raw = getattr(resp, 'content', None) or b''
    try:
        text = raw.decode('gb2312', errors='replace')
    except Exception:
        text = getattr(resp, 'text', '') or ''
    soup = BeautifulSoup(text, 'html.parser')
    lines = soup.get_text('\n', strip=True).split('\n')

    def _next_field(field_name):
        for i, line in enumerate(lines):
            if field_name in line and i + 1 < len(lines):
                val = lines[i + 1].strip()
                if val and not val.endswith('：') and not val.endswith(':'):
                    if re.search(r'\d{4}', val):
                        return val
        return ''

    return _next_field('发布日期'), _next_field('实施日期')
