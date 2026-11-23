# -*- coding: utf-8 -*-
"""全国标准信息公共服务平台爬虫"""
import re
from urllib.parse import quote
from bs4 import BeautifulSoup
from app_env import logger
from config import Config
from models import StandardInfo
from http_client import _http_request
from .base_spider import BaseSpider, SpiderSearchError


class SamrSpider(BaseSpider):
    """全国标准信息公共服务平台爬虫"""
    
    name = "全国标准信息公共服务平台"
    parser_name = "std_samr"
    
    def __init__(self, engine):
        super().__init__(engine)
    
    def search(self, keyword, site_config):
        """
        搜索标准并返回结果列表
        :param keyword: 搜索关键词
        :param site_config: 站点配置字典
        :return: list of StandardInfo
        """
        results = []
        try:
            # 直接从iframe源获取搜索结果
            iframe_url = f"https://std.samr.gov.cn/search/stdPage?q={quote(keyword)}&tid="
            resp = _http_request(self.engine.session, 'get', iframe_url, timeout=Config.TIMEOUT)
            if resp.status_code != 200:
                raise SpiderSearchError(f"iframe请求失败: {resp.status_code}")

            soup = BeautifulSoup(resp.text, 'html.parser')
            posts = soup.find_all('div', class_=['panel', 'post'])
            if not posts:
                posts = soup.find_all('div', class_='post')

            kw_core = self.engine._std_no_core(keyword) if self.engine._is_pure_number_query(keyword) else None
            is_pure = kw_core is not None

            candidates = []
            for post in posts:
                # 标准编号：span.en-code 内的完整文本
                en_code_span = post.find('span', class_='en-code')
                if not en_code_span:
                    continue
                std_no = en_code_span.get_text(strip=False).replace(' ', '').replace('\n', '')
                if not std_no:
                    continue

                # 状态：span.s-status（现行/废止）；「采」为同 class 的 label-default
                status_tag = post.find('span', class_='s-status')
                status = status_tag.get_text(strip=True) if status_tag else ''
                # 列表「采」角标：span.s-status.label-default 文本为「采」
                is_adopted = any(
                    (s.get_text(strip=True) or '') == '采'
                    for s in post.find_all('span', class_=True)
                    if 's-status' in (s.get('class') or [])
                )

                # 详情链接参数
                a_tag = post.find('a', attrs={'pid': True})
                pid = a_tag.get('pid', '') if a_tag else ''
                tid = a_tag.get('tid', '') if a_tag else ''

                # 标准名称
                std_name = ''
                if a_tag:
                    name_parts = []
                    for child in a_tag.children:
                        if hasattr(child, 'get') and child.get('class') and 'en-code' in child.get('class', []):
                            continue
                        txt = child if isinstance(child, str) else child.get_text()
                        name_parts.append(txt)
                    std_name = ''.join(name_parts).strip()

                # 发布日期、实施日期
                publish_date = ''
                implement_date = ''
                time_tags = post.find_all('time')
                if len(time_tags) >= 1:
                    publish_date = time_tags[0].get_text(strip=True)
                if len(time_tags) >= 2:
                    implement_date = time_tags[1].get_text(strip=True)

                # 纯数字查询：数字边界检查
                if is_pure:
                    if not re.search(rf'(?<!\d){re.escape(kw_core)}(?![\d.])', std_no):
                        continue
                else:
                    if not self.engine._match_std_no(std_no, keyword):
                        continue

                candidates.append(
                    (std_no, std_name, status, pid, tid, publish_date, implement_date, is_adopted)
                )

            if not candidates:
                return []

            def _detail_url(pid, tid):
                if tid == 'BV_HB':
                    return f'https://std.samr.gov.cn/hb/search/stdHBDetailed?id={pid}' if pid else site_config['url']
                if tid == 'BV_DB':
                    return f'https://std.samr.gov.cn/db/search/stdDBDetailed?id={pid}' if pid else site_config['url']
                return f'https://std.samr.gov.cn/gb/search/gbDetailed?id={pid}' if pid else site_config['url']

            def _norm_std_no(raw):
                """LY/T3460-2025 → LY/T 3460-2025"""
                raw = (raw or '').strip()
                m = re.match(
                    r'^([A-Za-z]+(?:/[A-Za-z]+)?)\s*([0-9].*)$',
                    raw.replace(' ', ''),
                )
                return f"{m.group(1)} {m.group(2)}" if m else raw

            def _cand_to_info(cand, fetch_detail=False):
                std_no, std_name, status, pid, tid, publish_date, implement_date, is_adopted = cand
                std_info = StandardInfo()
                std_info.std_no = _norm_std_no(std_no)
                std_info.std_name = std_name
                std_info.status = status
                std_info.source_site = self.name
                std_info.detail_url = _detail_url(pid, tid)
                std_info.download_status = "未检测"
                std_info.publish_date = publish_date
                std_info.implement_date = implement_date
                std_info.is_adopted = bool(is_adopted)
                if is_adopted:
                    logger.info(
                        f"[{self.name}] {std_info.std_no} 列表有「采」标识，"
                        f"下载将跳过标信/国标全文官方源"
                    )
                if fetch_detail and pid:
                    try:
                        detail_resp = _http_request(
                            self.engine.session, 'get', std_info.detail_url, timeout=Config.TIMEOUT
                        )
                        if detail_resp.status_code == 200:
                            self._parse_detail(detail_resp.text, std_info)
                    except Exception as e:
                        logger.warning(f"[{self.name}] 详情页访问异常: {e}")
                return std_info

            # 纯数字：同号多系列全部返回（禁止再「只取一条现行」）
            if is_pure:
                seen = set()
                for cand in candidates:
                    info = _cand_to_info(cand, fetch_detail=False)
                    key = (info.std_no or '').replace(' ', '').upper()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    results.append(info)
                return results

            # 带前缀：优先选与查询年号精确匹配；否则再选「现行/即将实施」
            kw_year = self.engine._keyword_year(keyword)
            best = None
            if kw_year:
                for cand in candidates:
                    _, y = self.engine._std_no_stem(_norm_std_no(cand[0]))
                    if y == kw_year:
                        best = cand
                        break
            if not best:
                for cand in candidates:
                    status = cand[2] or ''
                    if status and (
                        '现行' in status or '即将实施' in status
                    ) and '废止' not in status and '作废' not in status:
                        best = cand
                        break
            if not best:
                best = candidates[0]

            std_info = _cand_to_info(best, fetch_detail=True)
            if std_info.std_no:
                results.append(std_info)

        except SpiderSearchError:
            raise
        except Exception as e:
            logger.error(f"[{self.name}] 搜索失败: {e}")
            raise SpiderSearchError(str(e)) from e
        return results

    def fetch_standard_from_detail(self, detail_url):
        """直接打开详情页解析为 StandardInfo（替代标准跟链，不再搜索）。"""
        try:
            resp = _http_request(self.engine.session, 'get', detail_url, timeout=Config.TIMEOUT)
            if resp.status_code != 200:
                return None
            std_info = StandardInfo()
            std_info.source_site = self.name
            std_info.detail_url = detail_url
            std_info.download_status = "未检测"
            self._parse_detail(resp.text, std_info, as_primary=True)
            if not std_info.std_no:
                return None
            return std_info
        except Exception as e:
            logger.warning(f"[{self.name}] 跟链详情失败: {e}")
            return None
    
    def _parse_detail(self, html, std_info, as_primary=False):
        """解析详情页。
        as_primary=True：把本页当作目标标准（跟链打开的替代详情），提取编号/名称/状态/日期。
        as_primary=False：在主标准详情上提取「即将被替代」链接与查看文本。
        """
        try:
            soup = BeautifulSoup(html, 'html.parser')

            if as_primary:
                # 标题、标准号、状态（即将实施等）
                h4 = soup.find(['h4', 'h3', 'h2'])
                # 页面常见：标准号在「基础信息」；状态在徽章「即将实施」
                page_text = soup.get_text('\n', strip=True)
                no_m = re.search(
                    r'标准号\s*\n?\s*([A-Z]+(?:/[A-Z]+)?\s*[\d.]+-\d{4})',
                    page_text,
                )
                if no_m:
                    std_info.std_no = re.sub(r'\s+', ' ', no_m.group(1)).strip()
                if not std_info.std_no:
                    no_m = re.search(
                        r'([A-Z]+(?:/[A-Z]+)?\s*[\d.]+-\d{4})',
                        page_text,
                    )
                    if no_m:
                        std_info.std_no = no_m.group(1).strip()

                # 名称：基础信息「中文标准名称」或页面主标题
                for cell in soup.find_all(['td', 'th', 'dt', 'div', 'span', 'label']):
                    cell_text = cell.get_text(strip=True)
                    if cell_text in ('中文标准名称', '标准名称', '中文名称'):
                        nxt = cell.find_next(['td', 'dd', 'div', 'span'])
                        if nxt:
                            name = nxt.get_text(strip=True)
                            if name and len(name) < 120:
                                std_info.std_name = name
                                break
                if not std_info.std_name:
                    title = soup.find('h4') or soup.find('h3')
                    if title:
                        t = title.get_text(strip=True)
                        if t and not re.match(r'^[A-Z]+(?:/[A-Z]+)?', t) and len(t) < 120:
                            std_info.std_name = t

                for badge in ('即将实施', '现行', '废止', '被代替'):
                    if badge in page_text[:800]:
                        # 取靠前出现的状态词
                        std_info.status = badge
                        break

                for cell in soup.find_all(['td', 'th', 'dt', 'div', 'span', 'label']):
                    cell_text = cell.get_text(strip=True)
                    if cell_text == '发布日期':
                        nxt = cell.find_next(['td', 'dd', 'div', 'span'])
                        if nxt:
                            std_info.publish_date = nxt.get_text(strip=True)
                    elif cell_text == '实施日期':
                        nxt = cell.find_next(['td', 'dd', 'div', 'span'])
                        if nxt:
                            std_info.implement_date = nxt.get_text(strip=True)
                return

            # 1. 定位"被以下标准替代"卡片，同时保存详情直链
            std_no_pattern = re.compile(r'[A-Z]+(?:/[A-Z]+)?[\s]*[\d.]+[-—]\d{4}')
            for tag in soup.find_all(['div', 'span', 'h3', 'h4', 'p', 'th', 'td']):
                text = tag.get_text(strip=True)
                if text in ('被以下标准替代', '即将被以下标准替代'):
                    container = tag
                    for _ in range(5):
                        container = container.parent
                        if container is None:
                            break
                        for a in container.find_all('a', href=True):
                            a_text = a.get_text(strip=True)
                            m = std_no_pattern.search(a_text)
                            if m:
                                std_info.replacement_no = re.sub(r'\s+', '', m.group(0))
                                href = (a.get('href') or '').strip()
                                if href.startswith('http'):
                                    std_info.replacement_detail_url = href
                                elif href.startswith('/'):
                                    std_info.replacement_detail_url = (
                                        'https://std.samr.gov.cn' + href
                                    )
                                elif 'gbDetailed' in href or 'id=' in href:
                                    std_info.replacement_detail_url = (
                                        'https://std.samr.gov.cn/gb/search/' + href.lstrip('./')
                                    )
                                break
                        if std_info.replacement_no:
                            break
                    break

            # 2. 从"基础信息"区域提取发布日期、实施日期
            for cell in soup.find_all(['td', 'th', 'dt', 'div', 'span', 'label']):
                cell_text = cell.get_text(strip=True)
                if cell_text == '发布日期':
                    nxt = cell.find_next(['td', 'dd', 'div', 'span'])
                    if nxt:
                        std_info.publish_date = nxt.get_text(strip=True)
                elif cell_text == '实施日期':
                    nxt = cell.find_next(['td', 'dd', 'div', 'span'])
                    if nxt:
                        std_info.implement_date = nxt.get_text(strip=True)

            # 3. 从JS中提取"查看文本"按钮的URL
            if not getattr(std_info, 'samr_text_url', None):
                # 情况A：提取 .openhdbpdf handler 中的 filePath
                fp_match = re.search(r"\.openhdbpdf[\s\S]*?var\s+filePath\s*=\s*'([^']+)'", html)
                if fp_match:
                    fp_url = fp_match.group(1)
                    if fp_url and fp_url.startswith('http'):
                        if 'hbba.sacinfo.org.cn' in fp_url:
                            hash_match = re.search(r'/attachment/onlineRead/([a-f0-9]+)', fp_url)
                            if hash_match:
                                hash_val = hash_match.group(1)
                                fp_url = f'https://hbba.sacinfo.org.cn/portal/download/{hash_val}'
                            else:
                                hash_match = re.search(r'/portal/online/([a-f0-9]+)', fp_url)
                                if hash_match:
                                    hash_val = hash_match.group(1)
                                    fp_url = f'https://hbba.sacinfo.org.cn/portal/download/{hash_val}'
                        std_info.samr_text_url = fp_url

                # 情况B：提取 .openpdf handler 中的 hcno
                if not std_info.samr_text_url:
                    hcno_match = re.search(r"\.openpdf[\s\S]*?hcno=([^'\"]+)", html)
                    if hcno_match:
                        hcno_val = hcno_match.group(1).strip()
                        if hcno_val:
                            std_info.samr_text_url = f'http://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno={hcno_val}'

        except Exception as e:
            logger.warning(f"[{self.name}] 详情页解析异常: {e}")
