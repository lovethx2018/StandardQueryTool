# -*- coding: utf-8 -*-
"""国家标准全文爬虫

检索形态与工标网/全国标准不同：无论用户输入 GB/T 1345 还是 GB/T 1345-2005，
统一按数字主体「1345」检索；列表命中同主体后，再按年号选主标准并挂接替代版。
"""
import re
from urllib.parse import quote
from bs4 import BeautifulSoup
from app_env import logger
from config import Config
from models import StandardInfo
from http_client import _http_request
from .base_spider import BaseSpider, SpiderSearchError


class OpenstdSpider(BaseSpider):
    """国家标准全文爬虫"""

    name = "国家标准全文"
    parser_name = "openstd"

    def __init__(self, engine):
        super().__init__(engine)

    def _query_for_site(self, keyword):
        """国标全文按数字主体检索（如 1345 / 18242）。

        分部分编号（含小数点，如 1.1）单独搜噪声极大且会被同主体过滤成 0 条，
        此时改为「主体-年号」或保留完整关键词（与下载探测 URL 策略对齐）。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return keyword
        core = self.engine._std_no_core(keyword)
        year = self.engine._keyword_year(keyword)
        if core and re.fullmatch(r'[\d.]+', core):
            if '.' in core:
                site_kw = f"{core}-{year}" if year else keyword
                if site_kw != keyword:
                    logger.info(
                        f"[{self.name}] 分部分编号改用: {site_kw!r}（原 {keyword!r}）"
                    )
                return site_kw
            if core != keyword:
                logger.info(f"[{self.name}] 改用数字主体检索: {core!r}（原 {keyword!r}）")
            return core
        return keyword

    def search(self, keyword, site_config):
        """
        搜索标准并返回结果列表
        :param keyword: 搜索关键词
        :param site_config: 站点配置字典
        :return: list of StandardInfo
        """
        results = []
        try:
            site_kw = self._query_for_site(keyword)
            url = site_config['search_url'].format(keyword=quote(site_kw))
            logger.info(f"[{self.name}] 搜索URL: {url}")
            resp = _http_request(self.engine.session, 'get', url, timeout=Config.TIMEOUT)
            if resp.status_code == 200:
                site_results = self._parse(resp.text, keyword, site_config)
                logger.info(f"[{self.name}] 解析结果数量: {len(site_results)}")
                if site_results:
                    results.extend(site_results)
                    logger.info(f"[{self.name}] 命中 {len(site_results)} 条结果")
                else:
                    logger.info(f"[{self.name}] 无结果")
            else:
                raise SpiderSearchError(f"请求失败，状态码: {resp.status_code}")

        except SpiderSearchError:
            raise
        except Exception as e:
            logger.warning(f"[{self.name}] 搜索失败: {e}")
            raise SpiderSearchError(str(e)) from e

        return results

    def _row_to_std(self, std_no, std_name, status, hcno, publish_date="",
                    implement_date="", is_adopted=False):
        std_info = StandardInfo()
        std_info.std_no = std_no
        std_info.std_name = std_name
        std_info.status = status
        std_info.source_site = self.name
        std_info.publish_date = publish_date or ""
        std_info.implement_date = implement_date or ""
        std_info.is_adopted = bool(is_adopted)
        if hcno:
            # 详情页仍可打开；采标不可走官方全文/标信下载
            std_info.detail_url = (
                f"https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno={hcno}&refer=outer"
            )
        if hcno and not is_adopted:
            std_info.download_url = f"https://openstd.samr.gov.cn/bzgk/std/viewGb?hcno={hcno}"
            std_info.download_status = "可下载"
            std_info.download_sources = ['openstd']
        elif is_adopted:
            std_info.download_url = ""
            std_info.download_sources = []
            std_info.download_status = "未检测"
            logger.info(
                f"[{self.name}] {std_no} 有「采」标识，跳过官方全文/标信下载源，改走其它源探测"
            )
        else:
            std_info.download_status = "未检测"
        return std_info

    def _parse(self, html, keyword, site):
        """解析国家标准全文列表：同主体一族一次取出，主标准 + 列表内替代（不二次搜索）。"""
        results = []
        try:
            soup = BeautifulSoup(html, 'html.parser')
            table = soup.find('table', class_='result_list')
            if not table:
                return []
            rows = table.find_all('tr')[1:]

            kw_core = self.engine._std_no_core(keyword)
            kw_year = self.engine._keyword_year(keyword)

            # 列表候选：(std_no, name, status, hcno, pub, act, year, is_adopted)
            candidates = []
            for row in rows:
                cells = row.find_all('td')
                if len(cells) < 7:
                    continue

                std_no = cells[1].get_text(strip=True)
                if not std_no:
                    continue

                # 同数字主体才算一族（1345 不收 13454；18242 收 18242-2008/2025）
                row_core = self.engine._std_no_core(std_no)
                if kw_core and row_core != kw_core:
                    continue
                if not kw_core:
                    # 无核心时退回宽松匹配
                    if not self.engine._match_std_no(std_no, keyword):
                        continue

                std_name = cells[4].get_text(strip=True) if len(cells) > 4 else ''
                status = cells[6].get_text(strip=True) if len(cells) > 6 else ''
                # 发布/实施日期列（表头：… 状态 发布日期 实施日期 操作）
                publish_date = cells[7].get_text(strip=True) if len(cells) > 7 else ''
                implement_date = cells[8].get_text(strip=True) if len(cells) > 8 else ''
                # 日期字段有时在状态前几列，做兜底
                if publish_date and not re.search(r'\d{4}', publish_date):
                    publish_date = ''
                if implement_date and not re.search(r'\d{4}', implement_date):
                    implement_date = ''

                # 第3列「采」= 采用国际/国外标准，官方平台不可下全文
                adopt_text = cells[3].get_text(strip=True) if len(cells) > 3 else ''
                is_adopted = '采' in adopt_text

                hcno = None
                a_tags = cells[1].find_all('a', href=True)
                for a in a_tags:
                    onclick = a.get('onclick', '')
                    if onclick:
                        hcno_match = re.search(r"showInfo\(['\"]([^'\"]+)['\"]\)", onclick)
                        if hcno_match:
                            hcno = hcno_match.group(1)
                            break

                _, year = self.engine._std_no_stem(std_no)
                candidates.append(
                    (std_no, std_name, status, hcno, publish_date, implement_date, year, is_adopted)
                )

            if not candidates:
                return []

            is_pure = self.engine._is_pure_number_query(keyword)
            # 纯数字 / 无年号：同主体全部返回（避免只留一条现行、丢掉同号其它年）
            if is_pure or not kw_year:
                seen = set()
                for c in candidates:
                    key = (c[0] or "").replace(" ", "").upper()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    results.append(
                        self._row_to_std(
                            c[0], c[1], c[2], c[3], c[4], c[5], is_adopted=c[7]
                        )
                    )
                return results

            # 带年号：精确年一条 + 列表内替代挂接
            main = None
            for c in candidates:
                if c[6] == kw_year:
                    main = c
                    break
            if not main:
                main = candidates[0]

            main_std = self._row_to_std(
                main[0], main[1], main[2], main[3], main[4], main[5], is_adopted=main[7]
            )

            # 同列表内找更新的现行/即将实施作为替代（零额外请求）
            main_year = int(main[6]) if (main[6] and str(main[6]).isdigit()) else -1
            best_repl = None
            best_y = -1
            for c in candidates:
                if c[0].replace(' ', '') == main[0].replace(' ', ''):
                    continue
                st = c[2] or ""
                if not any(k in st for k in ("现行", "即将实施")):
                    continue
                if '废止' in st or '作废' in st:
                    continue
                y = int(c[6]) if (c[6] and str(c[6]).isdigit()) else 0
                if main_year > 0 and y <= main_year:
                    continue
                if y > best_y:
                    best_y = y
                    best_repl = c

            if best_repl:
                repl = self._row_to_std(
                    best_repl[0], best_repl[1], best_repl[2],
                    best_repl[3], best_repl[4], best_repl[5],
                    is_adopted=best_repl[7],
                )
                main_std.replacement_no = re.sub(r'\s+', '', best_repl[0])
                main_std.replacement = repl
                logger.info(
                    f"[{self.name}] 同族列表内挂接替代: {main_std.std_no} → {repl.std_no}"
                )

            if main_std.std_no:
                results.append(main_std)

        except Exception as e:
            logger.warning(f"解析openstd异常: {str(e)}")

        return results
