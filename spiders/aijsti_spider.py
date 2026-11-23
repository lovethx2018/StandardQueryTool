# -*- coding: utf-8 -*-
"""爱查新（biaozhun.aijsti.com）查询爬虫。

公开检索 API（无需登录）:
  GET https://api.aijsti.com/business/standards/list?keyword=&pageNum=&pageSize=
"""
import re

from app_env import logger
from config import Config
from models import StandardInfo
from utils import _FileNameParser
from .base_spider import BaseSpider, SpiderSearchError


class AijstiSpider(BaseSpider):
    """爱查新标准检索（免登录）"""

    name = "爱查新"
    parser_name = "aijsti"

    API_BASE = "https://api.aijsti.com"
    SITE_ORIGIN = "https://biaozhun.aijsti.com"

    @staticmethod
    def _keyword_variants(keyword):
        """爱查新对「DL/T5151-2014」无结果，需「DL/T 5151-2014」带空格；
        「JTG3460」需展开为「JTG 3460」。"""
        kw = (keyword or "").strip()
        if not kw:
            return []
        out = []
        glued = re.sub(
            r"^([A-Za-z]+(?:/[A-Za-z]+)?)(\d)",
            r"\1 \2",
            kw,
            count=1,
        )
        for v in (
            kw,
            glued,
            _FileNameParser._normalize_code(kw),
            re.sub(r"(/[Tt])(\d)", r"\1 \2", kw),
        ):
            v = (v or "").strip()
            if v and v not in out:
                out.append(v)
        return out

    def _api_base(self, site_config=None):
        site_config = site_config or {}
        return (site_config.get("api_base") or self.API_BASE).rstrip("/")

    def _list_request(self, api_base, keyword):
        params = {
            "keyword": keyword,
            "pageNum": 1,
            "pageSize": 20,
        }
        headers = {
            "Accept": "application/json",
            "Origin": self.SITE_ORIGIN,
            "Referer": self.SITE_ORIGIN + "/",
        }
        url = api_base + "/business/standards/list"
        resp = self._get(url, params=params, headers=headers, timeout=Config.TIMEOUT)
        if resp is None or getattr(resp, "status_code", 0) != 200:
            raise SpiderSearchError(
                "检索失败: HTTP %s" % getattr(resp, "status_code", None)
            )
        try:
            body = resp.json()
        except Exception as e:
            raise SpiderSearchError("检索响应非 JSON: %s" % e) from e
        if body.get("code") != 200:
            raise SpiderSearchError(body.get("message") or "检索失败")
        return (body.get("data") or {}).get("list") or []

    def search(self, keyword, site_config):
        results = []
        try:
            api_base = self._api_base(site_config)
            variants = self._keyword_variants(keyword)
            logger.info("[%s] 搜索: %s → %s", self.name, keyword, variants)
            rows = []
            seen_ids = set()
            for kw in variants:
                for row in self._list_request(api_base, kw):
                    if not isinstance(row, dict):
                        continue
                    rid = row.get("id")
                    key = rid if rid is not None else row.get("standardNo")
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                    rows.append(row)
                if rows:
                    break
            for row in rows:
                std = self._row_to_standard(row)
                if not std:
                    continue
                if not self._is_plausible_result(std):
                    continue
                if self._is_status_blocked(std.status):
                    continue
                # 纯数字/带数字主体：拒 13460 等超集；带前缀时再卡前缀
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

    def _row_to_standard(self, row):
        if not isinstance(row, dict):
            return None
        std_no = (row.get("standardNo") or "").strip()
        if not std_no:
            return None
        status = (row.get("statusDesc") or "").strip()
        if not status and row.get("status") is not None:
            status = {
                1: "即将实施",
                2: "现行",
                3: "作废",
                4: "废止",
            }.get(row.get("status"), "")
        std = StandardInfo(
            std_no=std_no,
            std_name=(row.get("standardName") or "").strip(),
            status=status,
            publish_date=(row.get("publishDate") or "").strip(),
            implement_date=(row.get("implementDate") or "").strip(),
            source_site=self.name,
        )
        sid = row.get("id")
        if sid is not None:
            std.detail_url = "%s/standard/%s" % (self.SITE_ORIGIN, sid)
        return std
