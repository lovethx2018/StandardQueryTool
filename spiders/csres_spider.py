# -*- coding: utf-8 -*-
"""工标网爬虫"""
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from app_env import logger
from config import Config
from models import StandardInfo
from http_client import _http_request
from .base_spider import BaseSpider, SpiderSearchError


class CsresSpider(BaseSpider):
    """工标网爬虫"""
    
    name = "工标网"
    parser_name = "csres"
    
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
            # 工标网使用GB2312编码（与分页共用同一编码逻辑）
            encoded_kw = self.engine._encode_csres_keyword(keyword)
            # 与站内高级筛选一致：现行/未实施/作废/废止全开，避免漏「即将实施」
            search_url = (
                "http://www.csres.com/s.jsp?keyword=%s"
                "&xx=on&wss=on&zf=on&fz=on&pageSize=25&pageNum=1"
                "&SortIndex=1&WayIndex=0&nowUrl="
            ) % encoded_kw
            logger.info(f"[{self.name}] 搜索URL: {search_url}")
            
            # 发送请求
            response = _http_request(
                self.engine.session, 'get', search_url, 
                timeout=Config.TIMEOUT
            )
            response.encoding = 'gb2312'
            
            logger.info(f"[{self.name}] 响应状态码: {response.status_code}, 响应长度: {len(response.text)}")
            
            if response.status_code == 200:
                # 统一过滤由 SpiderEngine → DataProcessor 负责，此处只解析
                site_results = self._parse(response.text, keyword, site_config)
                logger.info(f"[{self.name}] 解析结果数量: {len(site_results)}")
                if site_results:
                    results.extend(site_results)
                    logger.info(f"[{self.name}] 命中 {len(site_results)} 条结果")
                else:
                    logger.info(f"[{self.name}] 无结果")
            else:
                raise SpiderSearchError(f"请求失败，状态码: {response.status_code}")
        
        except SpiderSearchError:
            raise
        except Exception as e:
            logger.warning(f"[{self.name}] 搜索失败: {e}")
            raise SpiderSearchError(str(e)) from e
        
        return results
    
    def _parse(self, html, keyword, site):
        """解析工标网搜索结果。

        纯数字查询（如 3460）：列表里同号不同前缀全部返回（JTG/GB/T/YD…），
        禁止再「只取一条现行」——否则会把页面上的 JTG 3460-2026 直接丢掉。
        带前缀查询：仍走单条优选 + 详情页补全（替代关系）。
        """
        results = []
        try:
            soup = BeautifulSoup(html, 'html.parser')

            kw_core = self.engine._std_no_core(keyword) if self.engine._is_pure_number_query(keyword) else None
            is_pure = kw_core is not None

            # 从第一页提取候选
            candidates = self._parse_page(soup, kw_core, is_pure, keyword)

            # 检测总页数并遍历后续分页
            total_pages = self._parse_total_pages(soup)
            if total_pages > 1:
                base_url = "http://www.csres.com/s.jsp"
                for page in range(2, total_pages + 1):
                    page_url = (
                        f"{base_url}?keyword={self.engine._encode_csres_keyword(keyword)}"
                        f"&xx=on&wss=on&zf=on&fz=on&pageSize=25&pageNum={page}"
                        f"&SortIndex=1&WayIndex=0&nowUrl="
                    )
                    try:
                        resp = self._csres_get(page_url)
                        if resp.status_code != 200:
                            continue
                        page_soup = BeautifulSoup(resp.text, 'html.parser')
                        page_candidates = self._parse_page(page_soup, kw_core, is_pure, keyword)
                        candidates.extend(page_candidates)
                    except Exception as e:
                        logger.warning(f"[工标网] 第{page}页请求异常: {e}")
                        continue

            if not candidates:
                return []

            # 前缀白名单过滤
            candidates = [(s, name, st, u, impl) for s, name, st, u, impl in candidates
                          if self.engine._is_prefix_allowed(s)]
            
            # 缓存全量候选行供替代标准零请求查找
            for s, name, st, u, _impl in candidates:
                cache_key = s.replace(' ', '').replace('\u3000', '').upper()
                self.engine._csres_candidates[cache_key] = (s, name, st, u)

            if not candidates:
                return []

            # 纯数字：同号多系列全部保留；列表无发布日期，详情页补全
            if is_pure:
                infos = self._infos_from_list_candidates(candidates)
                return self._enrich_missing_dates(infos)

            # 带前缀：优先选与查询年号精确匹配的条目；否则再选「现行/即将实施」
            kw_year = self.engine._keyword_year(keyword)
            best = None
            if kw_year:
                for std_no, std_name, status, detail_url, _impl in candidates:
                    _, y = self.engine._std_no_stem(std_no)
                    if y == kw_year:
                        best = (std_no, std_name, status, detail_url)
                        break
            if not best:
                for std_no, std_name, status, detail_url, _impl in candidates:
                    if status and (
                        '现行' in status or '即将实施' in status or '即将实行' in status
                    ) and '废止' not in status and '作废' not in status:
                        best = (std_no, std_name, status, detail_url)
                        break
            if not best:
                std_no, std_name, status, detail_url, _impl = candidates[0]
                best = (std_no, std_name, status, detail_url)

            std_no, std_name, status, detail_url = best

            # 访问详情页获取完整信息
            resp = self._csres_get(detail_url)
            if resp.status_code != 200:
                return self._infos_from_list_candidates([
                    (std_no, std_name, status, detail_url, "")
                ])

            detail_soup = BeautifulSoup(resp.text, 'html.parser')
            std_info = self._parse_detail_soup(detail_soup, detail_url, list_status=status, list_std_no=std_no)
            if std_info and std_info.std_no:
                results.append(std_info)

        except Exception as e:
            logger.warning(f"解析csres异常: {str(e)}")

        return results

    def _infos_from_list_candidates(self, candidates):
        """列表行 → StandardInfo（去重保留首次出现顺序）。"""
        results = []
        seen = set()
        for std_no, std_name, status, detail_url, impl in candidates:
            key = (std_no or "").replace(" ", "").replace("\u3000", "").upper()
            if not key or key in seen:
                continue
            seen.add(key)
            info = StandardInfo()
            info.std_no = std_no
            info.std_name = std_name
            info.status = status or ""
            info.detail_url = detail_url
            info.implement_date = impl or ""
            info.source_site = self.name
            info.download_status = "未检测"
            results.append(info)
        return results

    def _enrich_missing_dates(self, results):
        """工标网列表无「发布日期」列：仅对仍缺发布日的行业标准拉详情补全。

        国标 GB/GB/T/GB/Z 通常由 openstd 提供发布日，跳过以免纯数字查询多打 N 次详情。
        """
        if not results:
            return results
        max_fetch = 8
        fetched = 0
        for info in results:
            if fetched >= max_fetch:
                break
            if getattr(self.engine, '_cancelled', False):
                break
            if (info.publish_date or "").strip():
                continue
            std_no = (info.std_no or "").strip()
            if re.match(r'^(GB(?:/[TZ])?)\b', std_no, re.IGNORECASE):
                continue
            url = (info.detail_url or "").strip()
            if "csres.com" not in url:
                continue
            try:
                pub, impl = self._fetch_detail_dates(url)
                fetched += 1
                if pub:
                    info.publish_date = pub
                if not (info.implement_date or "").strip() and impl:
                    info.implement_date = impl
            except Exception as e:
                logger.warning(f"[{self.name}] 补全日期失败 {info.std_no}: {e}")
        return results

    def _fetch_detail_dates(self, detail_url):
        """仅提取发布/实施日期（比整页解析轻）。"""
        resp = self._csres_get(detail_url)
        if resp.status_code != 200:
            return "", ""
        lines = BeautifulSoup(resp.text, 'html.parser').get_text('\n', strip=True).split('\n')

        def _next_field(field_name):
            for i, line in enumerate(lines):
                if field_name in line and i + 1 < len(lines):
                    val = lines[i + 1].strip()
                    if val and not val.endswith('：') and not val.endswith(':'):
                        if re.search(r'\d{4}', val):
                            return val
            return ""

        return _next_field("发布日期"), _next_field("实施日期")

    def fetch_standard_from_detail(self, detail_url):
        """直接打开详情页解析为 StandardInfo（用于替代标准跟链，不再搜索）。"""
        try:
            resp = self._csres_get(detail_url)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, 'html.parser')
            return self._parse_detail_soup(soup, detail_url)
        except Exception as e:
            logger.warning(f"[{self.name}] 跟链详情失败: {e}")
            return None

    def _parse_detail_soup(self, detail_soup, detail_url, list_status="", list_std_no=""):
        """从工标网详情页 DOM 提取主信息与「被…代替」链接。"""
        text_block = detail_soup.get_text('\n', strip=True)
        lines = text_block.split('\n')

        def _extract_next_line(field_name, all_lines):
            for i, line in enumerate(all_lines):
                if field_name in line and i + 1 < len(all_lines):
                    val = all_lines[i + 1].strip()
                    if val and not val.endswith('：') and not val.endswith(':'):
                        return val
            return ""

        std_info = StandardInfo()
        std_info.source_site = self.name
        std_info.detail_url = detail_url
        std_info.download_status = "未检测"

        h3 = detail_soup.find('h3')
        if h3:
            h3_text = h3.get_text(strip=True)
            m = re.match(r'^([A-Z]+(?:/[A-Z]+)?\s*[\d.]+(?:-\d{4})?)', h3_text, re.IGNORECASE)
            if m:
                std_info.std_no = m.group(1)
        if not std_info.std_no:
            actual_std_no = _extract_next_line("标准编号", lines)
            if actual_std_no:
                std_info.std_no = actual_std_no
        if not std_info.std_no:
            std_info.std_no = list_std_no

        if h3:
            h3_text = h3.get_text(strip=True)
            m = re.match(r'^[A-Z]+(?:/[A-Z]+)?\s*[\d.]+(?:-\d{4})?\s*(.+)$', h3_text, re.IGNORECASE)
            if m:
                std_info.std_name = m.group(1).strip()
            else:
                std_info.std_name = h3_text
        if not std_info.std_name:
            m = re.search(r'标准名称[：:]\s*(.+?)(?:\n|$)', text_block)
            if m:
                std_info.std_name = m.group(1).strip()

        # 详情页状态文案（如「即将作废」）优先于列表状态
        status_from_page = ""
        for i, line in enumerate(lines):
            if line.startswith("标准状态") or line == "标准状态":
                if i + 1 < len(lines):
                    status_from_page = lines[i + 1].strip()
                break
        # 列表状态仍可用；若正文出现即将作废提示则保留列表状态但 enrichment 用详情日期
        std_info.status = list_status or status_from_page or ""
        # 详情里常见「现行」+「即将作废」：若有作废日期提示，状态仍用列表/页面主状态
        if not std_info.status:
            m = re.search(r'标准状态[：:\s]*([^\n]+)', text_block)
            if m:
                std_info.status = m.group(1).strip()

        std_info.publish_date = _extract_next_line("发布日期", lines)
        std_info.implement_date = _extract_next_line("实施日期", lines)

        # 「被 <a href="/detail/xxx">GB/T …-2026</a> 代替」——跟链，不再二次搜索
        html_str = str(detail_soup)
        repl_link = re.search(
            r'被\s*<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*'
            r'([A-Z]+(?:/[A-Z]+)?\s*[\d.]+-\d{4})\s*</a>\s*代替',
            html_str,
            re.IGNORECASE,
        )
        if repl_link:
            std_info.replacement_detail_url = urljoin("http://www.csres.com/", repl_link.group(1))
            std_info.replacement_no = re.sub(r'\s+', '', repl_link.group(2))
        else:
            replacement_text = _extract_next_line("替代情况", lines)
            if replacement_text:
                repl_match = re.search(
                    r'被\s*\(*([A-Z]+(?:/[A-Z]+)?\s*[\d.]+-\d{4})\)*\s*代替',
                    replacement_text,
                )
                if repl_match:
                    std_info.replacement_no = re.sub(r'\s+', '', repl_match.group(1))

        return std_info
    
    def _parse_page(self, soup, kw_core, is_pure, keyword):
        """从工标网搜索结果页的表格中提取候选行。
        返回 (std_no, std_name, status, detail_url, implement_date)
        """
        candidates = []
        table = soup.find('table', class_='heng')
        if not table:
            return candidates
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
            implement = tds[3].get_text(strip=True) if len(tds) >= 4 else ''
            status = tds[4].get_text(strip=True) if len(tds) >= 5 else ''
            detail_url = urljoin("http://www.csres.com/", a_tag['href'])

            # 纯数字查询：数字边界检查（拒 13460 等超集）
            if is_pure:
                if not re.search(rf'(?<!\d){re.escape(kw_core)}(?![\d.])', std_no):
                    continue
            else:
                if not self.engine._match_std_no(std_no, keyword):
                    continue

            candidates.append((std_no, std_name, status, detail_url, implement))
        return candidates
    
    def _parse_total_pages(self, soup):
        """从工标网搜索结果页HTML中提取总页数"""
        text = soup.get_text()
        m = re.search(r'共\s*(\d+)\s*页', text)
        if m:
            return int(m.group(1))
        # 查找分页链接中的最大页码
        page_nums = []
        for a in soup.find_all('a', href=True):
            pm = re.search(r'pageNum[=/](\d+)', a['href'])
            if pm:
                page_nums.append(int(pm.group(1)))
        if page_nums:
            return max(page_nums)
        return 1
    
    def _csres_get(self, url):
        """请求工标网页面（GBK编码）"""
        resp = _http_request(
            self.engine.session, 'get', url, timeout=Config.TIMEOUT,
            headers={'Referer': 'http://www.csres.com/'}
        )
        resp.encoding = 'gb2312'
        return resp
