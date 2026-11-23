# -*- coding: utf-8 -*-
"""水利部国际合作与科技司爬虫。

门户页 gjkj.mwr.gov.cn 已改为 iframe 嵌入检索：
  http://121.36.94.83:9008/jsp/yishenqing/appladd/bzsearch.jsp
页面内含全量表格，客户端过滤；爬虫拉取后按关键词匹配行。
"""
import re
from urllib.parse import quote
from bs4 import BeautifulSoup
from app_env import logger
from config import Config
from models import StandardInfo
from http_client import _http_request
from .base_spider import BaseSpider, SpiderSearchError


class MwrSpider(BaseSpider):
    """水利部国际合作与科技司爬虫"""

    name = "水利部国际合作与科技司"
    parser_name = "mwr"
    IFRAME_URL = "http://121.36.94.83:9008/jsp/yishenqing/appladd/bzsearch.jsp"

    def search(self, keyword, site_config):
        results = []
        try:
            url = (site_config or {}).get("search_url") or self.IFRAME_URL
            # 配置里若仍是旧门户模板，强制改走 iframe
            if "gjkj.mwr.gov.cn" in url or "{keyword}" in url:
                url = self.IFRAME_URL
            resp = _http_request(self.engine.session, "get", url, timeout=max(Config.TIMEOUT, 45))
            if resp is None or getattr(resp, "status_code", 0) != 200:
                raise SpiderSearchError(
                    "请求失败，状态码: %s" % getattr(resp, "status_code", None)
                )
            if resp.encoding is None or str(resp.encoding).lower() in ("iso-8859-1", "ascii"):
                resp.encoding = resp.apparent_encoding or "utf-8"
            site_results = self._parse(resp.text, keyword, site_config or {})
            logger.info("[%s] 解析结果数量: %s", self.name, len(site_results))
            if site_results:
                results.extend(site_results)
                logger.info("[%s] 命中 %s 条结果", self.name, len(site_results))
            else:
                logger.info("[%s] 无结果", self.name)
        except SpiderSearchError:
            raise
        except Exception as e:
            logger.warning("[%s] 搜索失败: %s", self.name, e)
            raise SpiderSearchError(str(e)) from e
        return results

    def _parse(self, html, keyword, site):
        results = []
        try:
            soup = BeautifulSoup(html, "html.parser")
            kw_norm = re.sub(r"\s+", "", (keyword or "")).upper()
            kw_core = (
                self.engine._std_no_core(keyword)
                if self.engine._is_pure_number_query(keyword)
                else None
            )
            is_pure = kw_core is not None

            table = soup.find("table")
            if not table:
                return results
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue
                # 表结构：隐藏列 + 名称链接 + 英文名 + 标准号 + 分类 + 状态 + 发布 + 实施
                std_no = ""
                std_name = ""
                status = ""
                publish_date = ""
                implement_date = ""
                detail_url = ""

                for i, cell in enumerate(cells):
                    text = cell.get_text(strip=True)
                    if re.match(r"^(SL|SL/T)[\s/-]*[\d.]+", text, re.I):
                        std_no = text
                        # 名称通常在标准号前一列的 a
                        if i >= 1:
                            a = cells[i - 2].find("a", href=True) if i >= 2 else None
                            if a is None and i >= 1:
                                a = cells[max(0, i - 1)].find("a", href=True)
                            # 再扫整行
                            if a is None:
                                a = row.find("a", href=True)
                            if a:
                                std_name = a.get_text(strip=True)
                                href = a.get("href") or ""
                                if href.startswith("http"):
                                    detail_url = href
                                elif href:
                                    detail_url = (
                                        "http://121.36.94.83:9008/jsp/yishenqing/appladd/"
                                        + href.lstrip("/")
                                    )
                        break

                if not std_no:
                    continue

                # 状态 / 日期：按文本启发式
                for cell in cells:
                    text = cell.get_text(strip=True)
                    if text in ("现行", "废止", "作废", "即将实施", "通用"):
                        if text != "通用":
                            status = text
                    if re.match(r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}$", text):
                        if not publish_date:
                            publish_date = text.replace("/", "-")
                        elif not implement_date:
                            implement_date = text.replace("/", "-")

                std_no_norm = re.sub(r"\s+", "", std_no).upper()
                if is_pure:
                    std_num = re.sub(r"[^\d.]", "", std_no_norm.split("-")[0])
                    if not re.search(rf"(?<!\d){re.escape(kw_core)}(?![\d.])", std_num):
                        continue
                else:
                    if kw_norm not in std_no_norm and not self.engine._match_std_no(
                        std_no, keyword
                    ):
                        # 也允许名称命中（汉字查询走独立模块，这里宽松一点）
                        if kw_norm and kw_norm not in re.sub(r"\s+", "", std_name).upper():
                            continue

                std_info = StandardInfo(
                    std_no=std_no,
                    std_name=std_name,
                    status=status or "现行",
                    publish_date=publish_date,
                    implement_date=implement_date,
                    source_site=self.name,
                )
                std_info.detail_url = detail_url or (
                    "http://gjkj.mwr.gov.cn/jsjd1/bzcx/?q=%s" % quote(keyword)
                )
                std_info.download_url = std_info.detail_url
                results.append(std_info)
                if len(results) >= 40:
                    break
        except Exception as e:
            logger.warning("[%s] 解析异常: %s", self.name, e)
        return results
