# -*- coding: utf-8 -*-
"""交通运输部标准化信息系统爬虫"""
import re
from urllib.parse import quote
from bs4 import BeautifulSoup
from app_env import logger
from config import Config
from models import StandardInfo
from http_client import _http_request
from .base_spider import BaseSpider, SpiderSearchError


class JtstSpider(BaseSpider):
    """交通运输部标准化信息系统爬虫"""
    
    name = "交通运输部标准化信息系统"
    parser_name = "jtst"
    
    def __init__(self, engine):
        super().__init__(engine)
    
    def search(self, keyword, site_config):
        """
        搜索标准并返回结果列表。
        结果在 iframe stdPage 中（与标信平台同构），含 en-code / s-status。
        """
        results = []
        try:
            iframe_url = f"https://jtst.mot.gov.cn/search/stdPage?q={quote(keyword)}&tid="
            resp = _http_request(self.engine.session, 'get', iframe_url, timeout=Config.TIMEOUT)
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
    
    def _parse(self, html, keyword, site):
        """解析交通部 stdPage：按结果卡片提取编号/名称/状态。"""
        results = []
        try:
            soup = BeautifulSoup(html, 'html.parser')

            kw_core = self.engine._std_no_core(keyword) if self.engine._is_pure_number_query(keyword) else None
            is_pure = kw_core is not None

            posts = soup.find_all('div', class_=['panel', 'post'])
            if not posts:
                posts = soup.find_all('div', class_='post')

            candidates = []
            for post in posts:
                en_code_span = post.find('span', class_='en-code')
                if not en_code_span:
                    continue
                # sacinfo 可能把编号拆开；保留已有空格，避免 JTG B01 被压成 JTGB
                raw_no = re.sub(r"\s+", " ", en_code_span.get_text(strip=True).replace("\n", "")).strip()
                if " " in raw_no:
                    std_no = raw_no
                else:
                    compact = raw_no
                    m_norm = None
                    for pref in ("JTG/T", "JTG", "JT/T", "JTJ", "JT"):
                        if compact.upper().startswith(pref):
                            rest = compact[len(pref) :]
                            if rest:
                                m_norm = (compact[: len(pref)], rest)
                            break
                    if m_norm is None:
                        m = re.match(
                            r"^([A-Za-z]+(?:/[A-Za-z]+)?)([0-9A-Za-z].*)$",
                            compact,
                        )
                        m_norm = (m.group(1), m.group(2)) if m else None
                    std_no = f"{m_norm[0]} {m_norm[1]}" if m_norm else raw_no
                if not std_no:
                    continue

                if is_pure:
                    if not re.search(rf'(?<!\d){re.escape(kw_core)}(?![\d.])', std_no):
                        continue
                else:
                    if not self.engine._match_std_no(std_no, keyword):
                        continue

                status_tag = post.find('span', class_='s-status')
                status = status_tag.get_text(strip=True) if status_tag else ''

                std_name = ''
                a_tag = post.find('a', href=True)
                if a_tag:
                    name_text = a_tag.get_text(' ', strip=True)
                    std_name = re.sub(
                        r'(JTG(?:\s*/\s*T)?|JTJ)[\s\-]*[\d.]+(?:-\d{4})?',
                        '',
                        name_text,
                        count=1,
                        flags=re.IGNORECASE,
                    ).strip(' \t-—|·')[:100]

                if not status:
                    post_text = post.get_text(' ', strip=True)
                    for key in ('现行', '废止', '作废', '即将实施'):
                        if key in post_text:
                            status = key
                            break

                candidates.append((std_no, std_name, status))

            if not candidates:
                return []

            # 纯数字：命中卡片全部返回（JTG/JT 等同号不同系列）
            is_pure = kw_core is not None
            if is_pure:
                seen = set()
                for std_no, std_name, status in candidates:
                    key = (std_no or "").replace(" ", "").upper()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    std_info = StandardInfo()
                    std_info.std_no = std_no
                    std_info.std_name = std_name
                    std_info.status = status
                    std_info.source_site = self.name
                    std_info.detail_url = f"https://jtst.mot.gov.cn/search/std?q={quote(keyword)}"
                    std_info.download_status = "未检测"
                    results.append(std_info)
                return results

            # 带前缀：优先选「现行/即将实施」
            best = candidates[0]
            for cand in candidates:
                st = cand[2] or ''
                if st and (
                    '现行' in st or '即将实施' in st
                ) and '废止' not in st and '作废' not in st:
                    best = cand
                    break

            std_no, std_name, status = best

            std_info = StandardInfo()
            std_info.std_no = std_no
            std_info.std_name = std_name
            std_info.status = status
            std_info.source_site = self.name
            std_info.detail_url = f"https://jtst.mot.gov.cn/search/std?q={quote(keyword)}"
            std_info.download_status = "未检测"

            if std_info.std_no:
                results.append(std_info)

        except Exception as e:
            logger.warning(f"解析jtst异常: {str(e)}")

        return results
