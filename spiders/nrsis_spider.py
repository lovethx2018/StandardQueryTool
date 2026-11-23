# -*- coding: utf-8 -*-
"""自然资源标准化信息服务平台（nrsis.org.cn）查询爬虫。

列表检索:
  GET /portal/xxcx/std?pageNo=1&pageSize=20&key={keyword}
详情页:
  /portal/stdDetail/{id}
"""
import re
from urllib.parse import quote

from bs4 import BeautifulSoup

from app_env import logger
from config import Config
from models import StandardInfo
from .base_spider import BaseSpider, SpiderSearchError


class NrsisSpider(BaseSpider):
    """自然资源网标准检索"""

    name = "自然资源网"
    parser_name = "nrsis"

    SITE_ORIGIN = "http://www.nrsis.org.cn"
    LIST_PATH = "/portal/xxcx/std"

    @staticmethod
    def _keyword_variants(keyword):
        """站点对「GB/T14352」无结果，需「GB/T 14352」带空格；同时保留原样。"""
        kw = (keyword or "").strip()
        if not kw:
            return []
        out = []
        spaced = re.sub(r"(/[Tt])(\d)", r"\1 \2", kw)
        spaced = re.sub(r"([A-Za-z])(\d)", r"\1 \2", spaced, count=1) if "/" not in kw else spaced
        # CH/T1061 → CH/T 1061
        spaced2 = re.sub(r"(/(?:T|t))(\d)", r"\1 \2", kw)
        for v in (kw, spaced, spaced2, kw.replace(" ", "")):
            v = (v or "").strip()
            if v and v not in out:
                out.append(v)
        return out

    @staticmethod
    def _norm_no(std_no):
        return re.sub(r"\s+", "", (std_no or "")).upper()

    def search(self, keyword, site_config):
        results = []
        try:
            variants = self._keyword_variants(keyword)
            logger.info("[%s] 搜索: %s → %s", self.name, keyword, variants)
            rows = []
            seen = set()
            for kw in variants:
                for std in self._list_request(kw, site_config):
                    key = self._norm_no(std.std_no)
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    rows.append(std)
                if rows:
                    break
            for std in rows:
                if not self._is_plausible_result(std):
                    continue
                if self._is_status_blocked(std.status):
                    continue
                if not self.engine._is_prefix_allowed(std.std_no):
                    continue
                if self.engine._std_no_core(keyword):
                    if not self.engine._exact_number_match(std.std_no, keyword):
                        continue
                if self.engine._keyword_prefix(keyword):
                    if not self.engine._match_std_no(std.std_no, keyword):
                        continue
                results.append(std)
            logger.info("[%s] 命中 %s 条", self.name, len(results))
        except SpiderSearchError:
            raise
        except Exception as e:
            logger.warning("[%s] 搜索失败: %s", self.name, e)
            raise SpiderSearchError(str(e)) from e
        return results

    def _list_request(self, keyword, site_config=None):
        site_config = site_config or {}
        base = (site_config.get("url") or self.SITE_ORIGIN).rstrip("/")
        # 与站点 JS encodeURI(page.url+'?…&key='+key) 一致
        url = "%s%s?pageNo=1&pageSize=20&key=%s" % (
            base,
            self.LIST_PATH,
            quote(keyword, safe=""),
        )
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": base + self.LIST_PATH,
        }
        resp = self._get(url, headers=headers, timeout=Config.TIMEOUT)
        if resp is None or getattr(resp, "status_code", 0) != 200:
            raise SpiderSearchError(
                "检索失败: HTTP %s" % getattr(resp, "status_code", None)
            )
        text = resp.text or ""
        if resp.encoding is None or resp.encoding.lower() in ("iso-8859-1", "ascii"):
            resp.encoding = resp.apparent_encoding or "utf-8"
            text = resp.text
        return self._parse_list_html(text, base)

    def _parse_list_html(self, html, base):
        soup = BeautifulSoup(html, "html.parser")
        out = []
        for tr in soup.select("table tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            a = tr.find("a", href=re.compile(r"/portal/stdDetail/\d+"))
            if not a:
                continue
            std_no = tds[1].get_text(strip=True)
            if not std_no:
                continue
            href = a.get("href") or ""
            if href.startswith("/"):
                detail = base + href
            else:
                detail = href
            publish = tds[3].get_text(strip=True) if len(tds) > 3 else ""
            implement = tds[4].get_text(strip=True) if len(tds) > 4 else ""
            status = tds[5].get_text(strip=True) if len(tds) > 5 else ""
            if publish and not re.search(r"\d{4}", publish):
                publish = ""
            if implement and not re.search(r"\d{4}", implement):
                implement = ""
            std = StandardInfo(
                std_no=std_no,
                std_name=a.get_text(strip=True),
                status=status,
                publish_date=publish,
                implement_date=implement,
                source_site=self.name,
            )
            std.detail_url = detail
            out.append(std)
        return out
