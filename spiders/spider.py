# -*- coding: utf-8 -*-
"""爬虫引擎。"""
import re
import time
import random
from pathlib import Path
import shutil
import webbrowser
from urllib.parse import quote, urljoin
from bs4 import BeautifulSoup
from PyQt5.QtCore import QObject, pyqtSignal
from app_env import logger, TEMP_DIR
from config import Config
from models import StandardInfo
from utils import _safe_filename, _FileNameParser, normalize_status_by_date as _normalize_status_by_date_util, is_obsolete_status
from http_client import HAS_REQUESTS, requests, _StdlibSession, _http_request

# 导入独立的spider模块
from .samr_spider import SamrSpider
from .csres_spider import CsresSpider
from .openstd_spider import OpenstdSpider
from .jtst_spider import JtstSpider
from .mwr_spider import MwrSpider
from .aijsti_spider import AijstiSpider
from .nrsis_spider import NrsisSpider
from .spider_manager import SpiderManager
from .data_processor import DataProcessor
from . import chinese_name_query

# HTTP重试机制
if HAS_REQUESTS:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry


class SpiderEngine(QObject):
    """爬虫引擎 - 负责多站点检索和PDF下载"""
    
    # 定义信号
    status_updated = pyqtSignal(int, str)
    progress_updated = pyqtSignal(str)
    result_found = pyqtSignal(list)
    
    # 屏蔽的标准状态：这些状态的标准不读取、不查找、不显示
    _BLOCKED_STATUSES = ("正在征求意见",)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        if HAS_REQUESTS:
            self.session = self._create_session_with_retry()
            # 禁止读取系统环境变量中的代理设置（HTTPS_PROXY=127.0.0.1:7890 等），
            # 避免本地代理软件未运行时所有请求都失败
            self.session.trust_env = False
        else:
            self.session = _StdlibSession()
        self.session.headers.update(Config.HEADERS)
        self._rotate_user_agent()
        self.download_dir = Path(Config.effective_download_dir())
        try:
            self.download_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            # 配置目录不可写时退回临时目录，保证检索不受影响
            logger.warning("下载目录不可写，改用临时目录: %s (%s)", self.download_dir, e)
            self.download_dir = Path(TEMP_DIR) / "电子规范"
            self.download_dir.mkdir(parents=True, exist_ok=True)
        self._cancelled = False
        self._suppress_incremental = False
        self._csres_candidates = {}  # 搜索结果候选行缓存，供替代标准零请求查找
        self._replacement_cache = {}  # 替代标准结果缓存
        
        # 初始化爬虫管理器
        self.spider_manager = SpiderManager(self)
        self.spider_manager.register(SamrSpider)
        self.spider_manager.register(CsresSpider)
        self.spider_manager.register(OpenstdSpider)
        self.spider_manager.register(JtstSpider)
        self.spider_manager.register(MwrSpider)
        self.spider_manager.register(AijstiSpider)
        self.spider_manager.register(NrsisSpider)
        
        # 初始化数据处理器
        self.data_processor = DataProcessor(self)
    
    def _create_session_with_retry(self):
        """创建带有重试机制的会话
        自动重试 429/500/502/503/504 等状态码，提高网络稳定性
        """
        session = requests.Session()
        # 配置重试策略
        # 不含 500：部分站点（如 nrsis mnr_kfs）长期 500，重试只会拖死并抛 RetryError
        retry = Retry(
            total=3,  # 最大重试次数
            backoff_factor=1,  # 重试间隔因子（1秒、2秒、4秒...）
            status_forcelist=[429, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _get(self, url, **kwargs):
        """封装 requests GET：默认校验证书，仅对确实证书异常的主机自动降级并告警。"""
        return _http_request(self.session, 'get', url, **kwargs)

    def cancel(self):
        """取消当前搜索"""
        self._cancelled = True
    
    def reset_cancel(self):
        """重置取消标志"""
        self._cancelled = False
    
    def _check_cancelled(self):
        """检查是否已取消，已取消则抛出异常"""
        if self._cancelled:
            raise RuntimeError("搜索已被取消")
    
    def _rotate_user_agent(self):
        """随机切换一个 User-Agent 到主 session，并返回该 UA（供下载临时 session 复用）"""
        ua = random.choice(Config.USER_AGENTS)
        self.session.headers['User-Agent'] = ua
        return ua
    
    @staticmethod
    def _is_status_blocked(status):
        """检查标准状态是否被屏蔽（如"正在征求意见"）"""
        if not status:
            return False
        return any(blocked in status for blocked in SpiderEngine._BLOCKED_STATUSES)
    
    @staticmethod
    def _normalize_status_by_date(results):
        """基于实施日期智能判断标准状态（委托到公共函数）"""
        return _normalize_status_by_date_util(results)
    
    # —— 标准查询专用：无斜杠工程标准前缀展开映射（兼容大小写；仅用于标准查询，不作用于单一库查询）——
    _NOSLASH_PREFIX_MAP = {
        'JGJT': ('JGJ/T', 'JGJ'),
        'GBT': ('GB/T', 'GB'),
        'SLT': ('SL/T', 'SL'),
        'JCT': ('JC/T', 'JC'),
        'JTGT': ('JTG/T', 'JTG'),
        'CJJT': ('CJJ/T', 'CJJ'),
        'JGT': ('JG/T', 'JG'),
    }

    # —— 无字母前缀的纯编号(如 317-2014)自动补齐的常见工程标准前缀列表（仅用于标准查询）——
    # 含公路/市政/水利，避免只扫到 GB/JGJ 而漏 JTG/CJJ/SL
    _NOPREFIX_FILL_PREFIXES = (
        'GB/T', 'GB',
        'JGJ/T', 'JGJ', 'JG/T', 'JG',
        'JC/T', 'JC',
        'JTG/T', 'JTG',
        'CJJ/T', 'CJJ',
        'SL/T', 'SL',
    )

    # —— 搜索结果前缀白名单：不在列表中的前缀将被过滤（用户可在此追加新前缀）——
    _ALLOWED_PREFIXES = frozenset({
        'GB', 'GB/T', 'JG', 'JG/T', 'JGJ', 'JGJ/T',
        'CJJ', 'CJJ/T', 'JC', 'JC/T',
        'JTG', 'JTG/T', 'SL', 'SL/T',
        'YD', 'YD/T', 'CECS', 'T/CECS',
        'TD', 'TD/T', 'CH', 'CH/T', 'DZ', 'DZ/T',
    })

    @staticmethod
    def _is_prefix_allowed(std_no):
        """检查标准编号前缀是否在白名单中"""
        if not std_no:
            return False
        m = re.match(r'^([A-Za-z]+(?:/[A-Za-z]+)?)', std_no.strip())
        if not m:
            return False
        prefix = m.group(1).upper().replace(' ', '')
        return prefix in SpiderEngine._ALLOWED_PREFIXES

    @staticmethod
    def _keyword_prefix(keyword):
        """提取查询关键词中的标准前缀（如 'JTG/T 123' → 'JTG/T'），无前缀返回空字符串"""
        kw = keyword.strip()
        m = re.match(r'^([A-Za-z]+(?:/[A-Za-z]+)?)', kw)
        return m.group(1).upper() if m else ''

    @staticmethod
    def _expand_prefix_variants(keyword):
        """将用户输入展开为规范编码（仅用于标准查询）：
        1) 无斜杠字母前缀命中映射(JGJT/GBT/SLT/JCT/JTGT…)时，展开为斜杠规范形
           （如 'JGJT 14-2024' -> ['JGJ/T 14-2024']）；
        2) 代号粘连数字（高频：'JTG3460'）→ 规范空格形 'JTG 3460'；
        3) 无字母前缀且形如 '317-2014'（数字主体+四位年号）时，补齐常见工程前缀并行检索；
        4) 纯数字（无年号，最高频）原样返回单元素，由各主站全量扫描匹配同号不同前缀；
        其余输入原样返回单元素列表。"""
        if not keyword:
            return [keyword]
        kw = keyword.strip()
        m = re.match(r'^([A-Za-z]+)(.*)$', kw)
        if not m:
            # 无字母前缀：形如 '317-2014'(数字主体+四位年号) 或 '50123'(纯数字无年号)
            if kw[:1].isdigit():
                code = SpiderEngine._parse_std_code(kw)
                if code and code.get('core') and code.get('year') and not code.get('prefix'):
                    body = f"{code['core']}-{code['year']}"
                    return [f"{p} {body}" for p in SpiderEngine._NOPREFIX_FILL_PREFIXES]
                # 纯数字（无年号）：直接用纯数字检索每个源，让搜索引擎自己找到匹配标准
                if kw.isdigit():
                    return [kw]
            return [keyword]
        prefix = m.group(1).upper()
        rest = m.group(2).strip()
        mapping = SpiderEngine._NOSLASH_PREFIX_MAP.get(prefix)
        if mapping and rest and rest[0].isdigit():
            slash_form, _ = mapping
            return [f"{slash_form} {rest}"]
        # 已带斜杠输入（如 GB/T1345 或 gb/t1345）：展开为斜杠空格版
        slash_rest = re.match(r'^/([A-Za-z]+)(\d.*)$', rest)
        if slash_rest:
            subtype = slash_rest.group(1).upper()
            number_part = slash_rest.group(2)
            return [f"{prefix}/{subtype} {number_part}"]
        # 代号+数字粘连（JTG3460 / JTG3460-2026）→ 插入空格，站点才能命中
        if rest and rest[0].isdigit():
            spaced = f"{prefix} {rest}"
            return [spaced] if spaced != kw else [kw]
        return [keyword]

    @staticmethod
    def _normalize_slash(keyword):
        """将 U+2215(除号∕)、U+FF0F(全角／) 等 Unicode 斜杠类字符统一归一化为 ASCII /。"""
        if keyword is None:
            return ""
        return keyword.replace('\u2215', '/').replace('\uff0f', '/')

    @staticmethod
    def _encode_csres_keyword(keyword):
        """工标网关键词编码：优先 GB2312，不可编码时回退 UTF-8 quote。"""
        kw = keyword or ""
        try:
            return quote(kw.encode('gb2312'))
        except (UnicodeEncodeError, LookupError):
            return quote(kw)

    @staticmethod
    def _fill_missing_std_fields(primary, secondary):
        """同编号多源合并：用 secondary 补全 primary 的空字段（发布/实施日期等）。"""
        if primary is None or secondary is None:
            return
        for attr in (
            'publish_date', 'implement_date', 'std_name', 'status',
            'detail_url', 'download_url', 'source_site', 'samr_text_url',
        ):
            cur = (getattr(primary, attr, None) or '').strip()
            if cur:
                continue
            nxt = (getattr(secondary, attr, None) or '').strip()
            if nxt:
                setattr(primary, attr, getattr(secondary, attr))
        # 下载路由字段：主源无探测信息时用次源补全，避免 UI 误判来源
        if not (getattr(primary, 'download_sources', None) or []):
            ds = list(getattr(secondary, 'download_sources', None) or [])
            if ds:
                primary.download_sources = ds
        if getattr(secondary, 'is_adopted', False):
            primary.is_adopted = True
        if (getattr(primary, 'download_status', '') or '') in ('', '未检测'):
            nxt_status = getattr(secondary, 'download_status', '') or ''
            if nxt_status and nxt_status != '未检测':
                primary.download_status = nxt_status

    def _prefer_current_or_latest_obsolete(self, results, keyword):
        """无年号查询的统一状态过滤（单路径 / 多变体共用）：
        - 有年号：原样返回（含废止）
        - 无年号：优先保留现行/即将实施；若全无则保留年号最新的一条废止（避免结果被滤空）
        """
        if not results or self._keyword_year(keyword):
            return results

        current = [r for r in results if not is_obsolete_status(getattr(r, 'status', '') or '')]
        if current:
            logger.info(f"无年号过滤：保留 {len(current)} 条现行/即将实施，丢弃废止")
            return current

        obsolete = [r for r in results if is_obsolete_status(getattr(r, 'status', '') or '')]
        if not obsolete:
            return results

        obsolete.sort(key=lambda r: self._keyword_year(r.std_no) or '', reverse=True)
        latest = obsolete[0]
        logger.info(f"无年号过滤：无现行标准，保留最新废止 {latest.std_no}")
        return [latest]

    def search_standard(self, keyword, check_download=True, stop_on_first=None):
        """标准查询入口：无斜杠工程标准前缀（JGJT/GBT/SLT/JCT）自动展开为两组
         规范编码（如 JGJ/T 与 JGJ）并行检索、合并去重后返回；其余输入按原逻辑单编码检索。
         合并去重按完整标准编号 std_no（JGJ/T 与 JGJ 属不同编号，均保留）。
         该展开逻辑仅作用于标准查询，不改变单一库查询链路。

         stop_on_first:
           True  — 任一主站过滤后有结果即停（带前缀代号查询默认如此）
           False — 继续扫全部主站/备用站
           None  — 纯数字强制 False（同号多前缀须全站汇聚）；其余等同 True
        """
        keyword = (self._normalize_slash(keyword) or "").strip()
        if not keyword:
            logger.info("查询关键词为空，跳过检索")
            return []
        # 汉字名称查询：独立模块处理，编号查询原路径不动
        if chinese_name_query.is_chinese_name_query(keyword):
            return chinese_name_query.search(
                self, keyword, check_download=check_download
            )
        # 保存原始关键词用于后续的纯数字精确匹配判断
        original_keyword = keyword
        is_pure_number = self._is_pure_number_query(keyword)
        # 纯数字（如 3460）：必须扫完全部主站+行业备用站，否则先命中的 GB/T 会挡住 JTG
        if is_pure_number:
            stop_on_first = False
            logger.info("纯数字查询：关闭命中即停，全站汇聚同号不同前缀")
        elif stop_on_first is None:
            stop_on_first = True
            logger.info("启用命中即停：任一站精准命中后不再查询后续站点")
        variants = self._expand_prefix_variants(keyword)
        # 去重保留顺序
        seen = set()
        variants = [v for v in variants if not (v in seen or seen.add(v))]
        if len(variants) <= 1:
            search_kw = variants[0] if variants else keyword
            if search_kw != keyword:
                logger.info("关键词规范化: %r → %r", keyword, search_kw)
            self._suppress_incremental = True
            try:
                merged = self._search_standard_core(
                    search_kw, check_download, stop_on_first=stop_on_first
                )
            finally:
                self._suppress_incremental = False
            if merged:
                # 去重：同一编号多源合并，空字段用后到源补全（如工标网无发布日、爱查新有）
                seen_map = {}
                deduped = []
                for r in merged:
                    key = (getattr(r, 'std_no', '') or '').strip().replace(' ', '')
                    if not key:
                        continue
                    if key in seen_map:
                        self._fill_missing_std_fields(seen_map[key], r)
                        continue
                    seen_map[key] = r
                    deduped.append(r)
                merged = deduped
                
                # 基于实施日期智能判定状态（借鉴2026-7-19架构）
                merged = self._normalize_status_by_date(merged)
                # 与多变体路径一致：无年号优先现行，全无则保留最新废止
                merged = self._prefer_current_or_latest_obsolete(merged, search_kw)
            merged = self._sort_by_base_group(merged)
            if is_pure_number:
                merged = self._sort_by_civil_relevance(merged)
            if merged and not self._cancelled:
                self.result_found.emit(merged)
            return merged
        merged = []
        seen = {}  # key -> StandardInfo，重复时补全空字段
        covered_norms = set()  # 已查到现行标准的归一化编号，后续相同变体直接跳过
        sites_hit = set()  # 已命中结果的站点名称，后续变体跳过这些站点
        self._suppress_incremental = True
        try:
            for v in variants:
                if self._cancelled:
                    break
                # 如果该变体的归一化编号已被现行标准覆盖，跳过无需重复查询
                v_norm = self._std_base_norm(v)
                if v_norm in covered_norms:
                    logger.info(f"变体 '{v}' 已被现行标准覆盖（{v_norm}），跳过")
                    continue
                res = self._search_standard_core(v, False, skip_sites=sites_hit, stop_on_first=True)
                if res:  # 如果找到结果
                    # 记录哪些站点命中了结果，后续变体跳过这些站点
                    for r in res:
                        site_name = getattr(r, 'source_site', '')
                        if site_name:
                            sites_hit.add(site_name)
                
                for r in res or []:
                    key = (getattr(r, 'std_no', '') or '').strip().replace(' ', '')
                    if not key:
                        continue
                    if key in seen:
                        self._fill_missing_std_fields(seen[key], r)
                        continue
                    seen[key] = r
                    merged.append(r)
                    # 如果该结果是现行标准，记录其归一化编号供后续跳过重复变体
                    status = getattr(r, 'status', '') or ''
                    if '现行' in status and not is_obsolete_status(status):
                        r_norm = self._std_base_norm(r.std_no)
                        covered_norms.add(r_norm)
        finally:
            self._suppress_incremental = False
        merged = self._disambiguate_by_replacement(merged)
        
        # 纯数字编号精确匹配（如"50123"）：在合并后的结果上应用，确保数字主体严格等于查询值
        if self._is_pure_number_query(original_keyword) and merged:
            logger.info(f"纯数字查询过滤前: {len(merged)} 条结果")
            merged = [r for r in merged if self._exact_number_match(r.std_no, original_keyword)]
            logger.info(f"纯数字查询过滤后: {len(merged)} 条结果")
        
        # 与单路径一致：先按实施日期归一状态，再无年号优先现行
        merged = self._normalize_status_by_date(merged)
        merged = self._prefer_current_or_latest_obsolete(merged, original_keyword)
        
        # 按基础编号分组、组内按年号降序排列，让同一标准的不同年份版本相邻显示
        merged = self._sort_by_base_group(merged)
        if self._is_pure_number_query(original_keyword):
            merged = self._sort_by_civil_relevance(merged)
        # 下载检测（仅一次，不在每个变体内重复跑）
        if check_download and merged and not self._cancelled:
            self._check_download_sources(merged)
        # 消歧后统一推送一次最终结果给UI，规避展开过程中错误编号(如 GB 1345-2005)的短暂闪现
        if merged and not self._cancelled:
            self.result_found.emit(merged)
        return merged

    def _sort_by_base_group(self, results):
        """按基础编号分组、组内按年号降序排列，让同一标准的不同年份版本相邻显示。
        例如查询 GB/T 1345 时，GB/T 1345-2026 和 GB/T 1345-2005 会紧挨在一起，
        且新版本在前、旧版本在后，方便识别替代关系。"""
        if len(results) <= 1:
            return results
        
        def sort_key(r):
            base = self._std_base_norm(getattr(r, 'std_no', '') or '')
            _, year = self._std_no_stem(getattr(r, 'std_no', '') or '')
            # 无年号排最后；有年号按降序（新版本在前）
            year_val = int(year) if year and year.isdigit() else 0
            return (base, -year_val if year_val else 9999)
        
        return sorted(results, key=sort_key)

    def _sort_by_civil_relevance(self, results):
        """纯数字等同号多前缀时：行业前缀/工程名称加权靠前（JTG/JGJ 等优先于无关国标）。"""
        if len(results) <= 1:
            return results
        from .civil_domain import civil_engineering_boost

        def sort_key(r):
            boost = civil_engineering_boost(
                getattr(r, 'std_no', '') or '',
                getattr(r, 'std_name', '') or '',
            )
            base = self._std_base_norm(getattr(r, 'std_no', '') or '')
            _, year = self._std_no_stem(getattr(r, 'std_no', '') or '')
            year_val = int(year) if year and str(year).isdigit() else 0
            return (-boost, base, -year_val if year_val else 9999)

        return sorted(results, key=sort_key)

    def _std_base_norm(self, std_no):
        """取编号的归一化基础键（前缀+数字主体，去年号后统一斜杠/连字符/空格、转大写）。
        如 'GB/T 1345-2005' 与 'GB/T 1345-2026' -> 'GBT1345'；'GB 1345-2005' -> 'GB1345'。"""
        stem, _ = self._std_no_stem(std_no)
        return self._lan_norm(stem)

    def _disambiguate_by_replacement(self, results):
        """前缀展开等导致同一本标准出现多种编号写法时的消歧（仅用于标准查询）。
        对“数字主体+年号相同、但完整编号不同”的一组结果，若它们共享同一替代标准，
        则以替代标准的基础编号(前缀+主体)为准，仅保留基础编号与替代标准一致的那条，
        剔除其余错误编号写法。例如查 GBT 1345-2005 展开得到 GB/T 1345-2005 与 GB 1345-2005，
        二者替代标准同为 GB/T 1345-2026(基础编号 GB/T 1345)，据此保留 GB/T 1345-2005、剔除 GB 1345-2005。
        证据不足时(替代标准缺失/不一致/无一条与替代基础编号相符)一律保守保留，绝不误删。"""
        if len(results) <= 1:
            return results
        groups = {}
        for i, r in enumerate(results):
            core = self._std_no_core(r.std_no)
            _, year = self._std_no_stem(r.std_no)
            if not core or not year:
                continue
            groups.setdefault((core, year), []).append(i)
        drop = set()
        for _key, idxs in groups.items():
            if len(idxs) <= 1:
                continue
            repl_keys = set()
            ok = True
            for i in idxs:
                rep = getattr(results[i], 'replacement', None)
                rno = getattr(rep, 'std_no', '') if rep is not None else ''
                if not rno:
                    ok = False
                    break
                repl_keys.add(self._std_base_norm(rno))
            # 仅当组内所有结果共享同一个替代标准时，才用它反推正确编号
            if not ok or len(repl_keys) != 1:
                continue
            target = next(iter(repl_keys))
            keep = [i for i in idxs if self._std_base_norm(results[i].std_no) == target]
            if keep:
                for i in idxs:
                    if self._std_base_norm(results[i].std_no) != target:
                        drop.add(i)
        if not drop:
            return results
        return [r for i, r in enumerate(results) if i not in drop]

    def _get_sites_for_prefix(self, keyword):
        """所有标准统一走 PRIMARY_SITES 轮询。
        前 N-3 站为主检索；末 3 站为备用（仅前站无结果且前缀匹配时运行）。"""
        return Config.PRIMARY_SITES, True

    def _search_standard_core(self, keyword, check_download=True, enrich_replacements=True, skip_sites=None, stop_on_first=False):
        """检索单一标准编码的核心逻辑（供 search_standard 包装层调用，逻辑与原实现一致）。
        keyword: 标准编号或名称
        check_download: 是否检测下载源
        enrich_replacements: 是否查询替代标准（防止递归）
        skip_sites: 需要跳过的站点名称集合（用于变体查询时避免重复查询同一站点）
        stop_on_first: 过滤后有结果即停止后续站点（默认 False 仅内部兜底调用）
        返回: list of StandardInfo"""
        keyword = (self._normalize_slash(keyword) or "").strip().upper()
        if not keyword:
            return []
        logger.info(f"开始查询: {keyword}")
        results = []
        sites, matched_rule = self._get_sites_for_prefix(keyword)
        if matched_rule:
            site_names = [s['name'] for s in sites]
            logger.info(f"根据前缀规则选择站点: {', '.join(site_names)}")
        
        # 备用站点 = PRIMARY_SITES 末尾 2 站（交通部、水利部）；前 N-2 为主检索
        _BACKUP_PREFIX_MAP = {
            'jtst': ('JTG', 'JTG/T'),
            'mwr': ('SL', 'SL/T'),
        }
        _PRIMARY_SITE_COUNT = max(0, len(Config.PRIMARY_SITES) - 2)
        is_pure_number = self._is_pure_number_query(keyword)
        for idx, site in enumerate(sites):
            # 跳过已命中结果的站点（变体查询优化）
            if skip_sites and site['name'] in skip_sites:
                logger.info(f"跳过已命中的站点: {site['name']}")
                continue
            # 备用站点：纯数字一律查询（同号可能分属 JTG/SL/TD 等行业站）；
            # 带前缀时：仅前缀匹配才跑，且前站已有同前缀结果可跳过
            if idx >= _PRIMARY_SITE_COUNT and not is_pure_number:
                allowed = _BACKUP_PREFIX_MAP.get(site['parser'])
                if allowed:
                    kw_prefix = self._keyword_prefix(keyword)
                    if kw_prefix not in allowed:
                        logger.info(f"关键词前缀 '{kw_prefix}' 不匹配，跳过备用站点: {site['name']}")
                        if idx < len(Config.PRIMARY_SITES):
                            self.status_updated.emit(idx, "no_result")
                        continue
                if results:
                    kw_prefix = self._keyword_prefix(keyword)
                    if kw_prefix:
                        has_matching = any(
                            self._keyword_prefix(getattr(r, 'std_no', '') or '') == kw_prefix
                            for r in results
                        )
                        if has_matching:
                            logger.info(
                                f"前{_PRIMARY_SITE_COUNT}站已有匹配前缀 '{kw_prefix}' 的结果，"
                                f"跳过备用站点: {site['name']}"
                            )
                            continue
                    else:
                        logger.info(f"前{_PRIMARY_SITE_COUNT}站已有结果，跳过备用站点: {site['name']}")
                        continue
            elif idx >= _PRIMARY_SITE_COUNT and is_pure_number:
                logger.info(f"纯数字查询：启用备用站点 {site['name']}")
            self._check_cancelled()
            
            self.progress_updated.emit(f"正在检索: {site['name']}")
            logger.info(f"正在查询站点: {site['name']}")
            
            # 更新站点状态为黄色（检索中）
            self.status_updated.emit(idx, "searching")
            
            try:
                # 使用独立的spider模块（借鉴2026-7-19架构）
                spider_instance = self.spider_manager.get(site['parser'])
                if spider_instance:
                    # 调用独立spider的search方法
                    site_results = spider_instance.search(keyword, site)
                    
                    # 使用数据处理器过滤结果
                    site_results = self.data_processor.filter_results(site_results, keyword)

                    logger.info(f"[{site['name']}] 解析结果数量: {len(site_results)}")
                    
                    if site_results:
                        # 更新站点状态为绿色（成功）
                        if idx < len(Config.PRIMARY_SITES):
                            self.status_updated.emit(idx, "success")
                        
                        results.extend(site_results)
                        # 立即发出结果信号，让UI可以增量显示
                        if not getattr(self, '_suppress_incremental', False):
                            self.result_found.emit(site_results)
                        if stop_on_first:
                            logger.info(f"[{site['name']}] 命中 {len(site_results)} 条结果，停止后续站点")
                            break
                        logger.info(f"[{site['name']}] 命中 {len(site_results)} 条结果，继续检索后续站点")
                    else:
                        # 无结果，继续下一个站点
                        logger.info(f"[{site['name']}] 无结果，继续下一个站点")
                        if idx < len(Config.PRIMARY_SITES):
                            self.status_updated.emit(idx, "no_result")
                else:
                    # 所有主站 parser 均已注册到 SpiderManager；未注册视为配置错误
                    logger.warning(f"[{site['name']}] 未注册解析器: {site.get('parser')}")
                    if idx < len(Config.PRIMARY_SITES):
                        self.status_updated.emit(idx, "failed")

            except RuntimeError as e:
                if "搜索已被取消" in str(e):
                    raise
                logger.warning(f"[{site['name']}] 检索异常: {str(e)}")
                if idx < len(Config.PRIMARY_SITES):
                    self.status_updated.emit(idx, "failed")
                continue
            except Exception as e:
                logger.warning(f"[{site['name']}] 检索异常: {str(e)}")
                if idx < len(Config.PRIMARY_SITES):
                    self.status_updated.emit(idx, "failed")
                continue
        
        # 补充替代规范信息：废止标准始终查；现行标准也查（含批量检索），
        # 以便显示"即将实施/已被替代"的替代版本（如 GB/T 1345-2005 → GB/T 1345-2026）。
        # 优化：先检查是否被取消，被取消时仅对已找到的废止标准查询替代规范
        # 注意：enrich_replacements=False 时跳过，防止递归调用
        if enrich_replacements:
            try:
                if not self._cancelled:
                    self._enrich_replacements(results, include_current=True)
                else:
                    # 已取消：仅对已找到的废止标准查询替代规范（用户要求）
                    obsolete_results = [r for r in results if is_obsolete_status(r.status or "")]
                    if obsolete_results:
                        self._enrich_replacements(obsolete_results, include_current=False)
            except Exception as e:
                logger.warning(f"补充替代规范信息异常: {str(e)}")
        
        # 遍历所有下载源检测是否可下载（仅单本查询时）
        # 优化：被取消时跳过下载源检测
        if check_download and not self._cancelled:
            self._check_download_sources(results)
        
        return results
    
    def _std_no_stem(self, std_no):
        """拆分标准编号为基础编号和年份，如 'GB/T 2812-2006' -> ('GB/T 2812', '2006')
        CECS/CJJ 等系列年号用冒号（如 CECS 293:2011），一并支持。"""
        if not std_no:
            return "", None
        m = re.match(r'^(.*?)[-—:：](\d{4})$', std_no.strip())
        if m:
            return m.group(1).strip(), m.group(2)
        return std_no.strip(), None

    @staticmethod
    def _is_plausible_result(std):
        """过滤解析器从页面导航/热搜文本中误提取的脏数据。
        例如搜 JGJ/T 317-2014 时，jtst 页面没有该标准，通用解析器却把侧边栏
        热搜里的 'ISO 9001' 连同整段导航文字当成结果。真实标准名称不会超长、
        也不会重复出现多次'标准'。与 MainWindow.is_valid_result 判据保持一致。"""
        name = (getattr(std, "std_name", "") or "").strip()
        if len(name) > 60:
            return False
        if name.count("标准") >= 3:
            return False
        return True

    @staticmethod
    def _keyword_year(keyword):
        """提取查询关键词末尾的年号（形如 '-2005' 结尾的 4 位数字）；无年号返回 None。
        例如 'GB/T 1345-2005'、'1345-2005' 均返回 '2005'。"""
        if not keyword:
            return None
        m = re.search(r'[-—–－‐:：]\s*(\d{4})\s*$', str(keyword).strip())
        return m.group(1) if m else None

    def _std_no_exact_match(self, std_no, keyword):
        """带年号查询时，判断某结果编号是否与关键词精确对应（同编号主体且同年份），
        用于只保留用户指定的那一版本，不返回同编号的其他年份版本。"""
        def _norm(t):
            t = str(t or "").strip().replace(" ", "").replace("\u3000", "")
            t = t.replace("／", "/").replace("（", "(").replace("）", ")")
            for d in ("—", "–", "－", "‐", "‑", "‒", "―"):
                t = t.replace(d, "-")
            return t
        kn, kw = _norm(std_no), _norm(keyword)
        if not kn or not kw:
            return False
        if kn == kw:
            return True
        # 关键词不含字母前缀（如 '1345-2005'）：按数字主体 + 年份比较
        if not re.match(r'^[A-Za-z]', kw):
            kw_core = self._std_no_core(kw)
            kw_year = self._keyword_year(kw)
            r_core = self._std_no_core(kn)
            _, r_year = self._std_no_stem(kn)
            if kw_core and kw_year and kw_core == r_core and kw_year == r_year:
                return True
        return False

    def _std_no_core(self, std_no):
        """提取编号数字主体，如 'GB/T 18242-2025' -> '18242'（忽略字母前缀与年份）"""
        stem, _ = self._std_no_stem(std_no)
        m = re.search(r'(\d+(?:\.\d+)*)\s*$', stem)
        return m.group(1) if m else None
    
    @staticmethod
    def _is_pure_number_query(keyword):
        """判断是否为纯数字编号查询（如'50123'），不含字母前缀和年号"""
        if not keyword:
            return False
        kw = keyword.strip()
        # 纯数字查询：仅包含数字，或数字后跟4位年号（如'50123'或'50123-2020'）
        return bool(re.match(r'^\d+(-\d{4})?$', kw))

    def _match_std_no(self, std_no, keyword):
        """统一标准编号匹配：
        - keyword 含年号：严格规整后相等比较（保留现有行为）
        - keyword 无年号含前缀：先比 prefix 再比核心数字（忽略结果年号）
        - keyword 纯数字：由 _exact_number_match 处理（调用方自行调用）
        返回 True=匹配保留，False=跳过
        """
        if not std_no or not keyword:
            return False

        norm = lambda x: x.replace(' ', '').replace('\u3000', '').replace('\xa0', '')

        if self._keyword_year(keyword):
            # 有年号 → 严格相等
            return norm(std_no) == norm(keyword)

        # 无年号 → 提取 keyword 的前缀 + 数字核心
        kw_pre = re.match(r'^([A-Za-z]+(?:/[A-Za-z]+)?)', keyword)
        if not kw_pre:
            return norm(std_no) == norm(keyword)
        kw_prefix = re.sub(r'[\s/]', '', kw_pre.group(1)).upper()

        # std_no 必须含相同前缀
        st_pre = re.match(r'^([A-Za-z]+(?:/[A-Za-z]+)?)', std_no)
        if not st_pre:
            return False
        st_prefix = re.sub(r'[\s/]', '', st_pre.group(1)).upper()
        if kw_prefix != st_prefix:
            # 允许 GB/T↔GB、JGJ/T↔JGJ 等 /T 差异互通
            if not (kw_prefix + 'T' == st_prefix or st_prefix + 'T' == kw_prefix):
                return False

        # 核心数字边界匹配
        kw_core = self._std_no_core(keyword)
        if not kw_core:
            return False
        return bool(re.search(rf'(?<!\d){re.escape(kw_core)}(?![\d.])', std_no))

    def _exact_number_match(self, std_no, keyword):
        """精确数字匹配：确保查询的数字作为完整数值段出现在标准编号中
        例如：'50123' 匹配 'GB 50123-2020'，但不匹配 'GB/T 501230-2020' 或 'DB50/123-2003'
        """
        if not std_no or not keyword:
            return False
        # 提取查询关键词的数字主体
        kw = keyword.strip()
        kw_core = self._std_no_core(kw)
        if not kw_core:
            return False
        # 提取结果标准编号的数字主体
        result_core = self._std_no_core(std_no)
        if not result_core:
            return False
        # 严格相等比较：数字主体必须完全相同
        if kw_core != result_core:
            return False
        # 额外的数字边界检查：确保查询数字在标准编号中作为完整数值段出现，
        # 防止 50123 匹配到 50123.1 或 501230 等变体（_std_no_core 已做核心比较，此处加强）
        try:
            escaped = re.escape(kw_core)
            if not re.search(rf'(?<!\d){escaped}(?![\d.])', std_no):
                return False
        except Exception:
            pass
        return True
    
    def _lan_share_dir(self):
        """返回已配置且当前可访问的局域网标准共享目录 Path；未配置或不可访问时返回 None（判定局域网资源不可用）。"""
        share = (Config.LAN_SHARE_DIR or "").strip()
        if not share:
            return None
        try:
            p = Path(share)
            if p.exists() and p.is_dir():
                return p
        except Exception as e:
            logger.warning(f"[局域网检索] 共享目录不可访问: {e}")
        return None

    @staticmethod
    def _lan_norm(text):
        """标准编号/文件名归一化，用于稳健匹配：
        - 各类连字符（全角/连接号等）统一为半角 '-'（保留 '-' 以支撑裸号无年份匹配时的尾部边界判定）
        - 去除空白（含全角/不换行空格/制表符）、正反斜杠（含全角）、下划线
          （斜杠为 Windows 非法文件名字符，常被替换为下划线或直接删除，如 JGJ/T → JGJ_T / JGJT）
        - 统一转大写
        """
        if not text:
            return ""
        # 各类连字符 + 冒号（半角:/全角：，CECS/CJJ 等系列年号用冒号）统一为半角 '-'
        for ch in ('\uff0d', '\u2010', '\u2011', '\u2012', '\u2013', '\u2014', '\u2015', '\u2212', ':', '\uff1a'):
            text = text.replace(ch, '-')
        for ch in (' ', '\u3000', '\xa0', '\t', '/', '\\', '\uff0f', '\u2215', '\u2044', '\u2571', '\uff3f', '_'):
            text = text.replace(ch, '')
        return text.upper()

    @staticmethod
    def _parse_std_code(text):
        """从任意编号串/文件名中解析标准编号结构，返回 {'prefix','core','year'}；解析不出数字主体时返回 None。
        - prefix：归一化紧凑大写前缀（如 GB/T→GBT、JGJ_T→JGJT、DB11/T→DB11T），可能为空串
        - core：数字主体，整体保留（如 '14'、'50011'、'19000.3'），用于精确相等比较，杜绝 14 命中 214/140
        - year：4 位年号字符串（仅认 19xx/20xx，且其后不接数字），无法确定时为 None
        设计目标：主体与年号整体精确匹配，绝不产生子串式误匹配。"""
        if not text:
            return None
        s = str(text)
        # 统一各类连字符 + 冒号（半角:/全角：，CECS/CJJ 等系列年号用冒号）为半角 '-'
        for ch in ('\uff0d', '\u2010', '\u2011', '\u2012', '\u2013', '\u2014', '\u2015', '\u2212', ':', '\uff1a'):
            s = s.replace(ch, '-')
        # 去除分隔符并紧凑化、转大写；保留中文与括号作为前缀左边界
        for ch in (' ', '\u3000', '\xa0', '\t', '/', '\\', '\uff0f', '\u2215', '\u2044', '\u2571', '\uff3f', '_'):
            s = s.replace(ch, '')
        s = s.upper()
        # 锚定“数字主体-4位年号”，年号限定 19/20 开头且其后不接数字，主体取紧邻年号左侧的完整数字串
        m = re.search(r'(\d+(?:\.\d+)*)-((?:19|20)\d{2})(?!\d)', s)
        if not m:
            return None
        core = m.group(1)
        year = m.group(2)
        # 主体左侧连续的 ASCII 字母数字作为前缀（覆盖 GB/GBT/JGJT 及地方标准 DB11T 等）
        start = m.start(1)
        j = start - 1
        while j >= 0 and s[j].isascii() and s[j].isalnum():
            j -= 1
        prefix = s[j + 1:start]
        return {'prefix': prefix, 'core': core, 'year': year}

    def _lan_index(self, force=False):
        """遍历共享目录构建 (归一化文件名, 文件Path) 索引，带 30 秒缓存以避免批量查询时重复全盘遍历。
        目录不可用返回 None；可用但为空返回 []。"""
        share = self._lan_share_dir()
        if share is None:
            self._lan_cache = None
            return None
        now = time.time()
        cache = getattr(self, '_lan_cache', None)
        if not force and cache and cache['dir'] == str(share) and (now - cache['ts'] < 30):
            return cache['items']
        items = []
        try:
            for f in share.rglob('*'):
                try:
                    if f.is_file() and f.suffix.lower() not in _FileNameParser.EXCLUDED_EXTENSIONS:
                        items.append((self._parse_std_code(f.stem), self._lan_norm(f.stem), f))
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"[局域网检索] 遍历共享目录异常: {e}")
            items = []
        # 遍历期间目录被改掉：不写入缓存，避免旧索引覆盖新路径
        cur = self._lan_share_dir()
        if cur is None or str(cur) != str(share):
            logger.info("[局域网检索] 索引结果已过期（目录已变更），丢弃本次缓存")
            return items
        self._lan_cache = {'dir': str(share), 'ts': now, 'items': items}
        return items

    def _find_in_lan(self, std_no):
        """在局域网共享目录内按标准编号匹配文件，命中返回文件 Path，否则 None。
        约定文件名为“编号在前、名称在后”（编号可含各种连接符/下划线/空格、甚至无前缀）。
        带年号的查询走结构化精确匹配：主体与年号整体相等、前缀分级判定，绝不误配不同类别/年份/主体的标准；
        无法解析出年号的查询回退到归一化子串匹配，保持既有能力。"""
        items = self._lan_index()
        if not items:
            logger.info("[局域网检索] '%s' 未匹配：共享目录未配置或为空（LAN_SHARE_DIR=%r）",
                        std_no, Config.LAN_SHARE_DIR)
            return None
        q = self._parse_std_code(std_no)
        # 主线：查询能解析出“数字主体 + 4 位年号”
        if q and q.get('core') and q.get('year'):
            qp, qc, qy = q.get('prefix', ''), q['core'], q['year']
            # 收集“同主体 + 同年号”的候选（不同前缀 = 不同类别标准）
            same_body = []
            for parsed, _stem, f in items:
                if parsed and parsed.get('core') == qc and parsed.get('year') == qy:
                    same_body.append((parsed.get('prefix', ''), f))
            if not same_body:
                logger.info("[局域网检索] '%s' 解析为 %s，目录中无同主体同年号文件", std_no, q)
                return None
            if qp:
                # L1：前缀精确一致 —— 最可靠，直接命中
                for fp, f in same_body:
                    if fp == qp:
                        return f
                # L2：只剩无前缀文件时，须“无冲突”（无其它前缀的同主体同年标准）才认定，宁缺毋滥
                others = [fp for fp, f in same_body if fp and fp != qp]
                if not others:
                    for fp, f in same_body:
                        if not fp:
                            return f
                logger.info("[局域网检索] '%s' 前缀 %r 与目录候选前缀 %s 冲突，未命中",
                            std_no, qp, [fp for fp, _ in same_body])
                return None
            else:
                # 查询本身未写前缀：目录中该“主体+年号”的前缀种类唯一(≤1)、无歧义时才命中
                distinct = set(fp for fp, f in same_body if fp)
                if len(distinct) <= 1:
                    return same_body[0][1]
                return None
        # 回退：查询无年号（如仅 GB/T 50011）—— 沿用归一化子串匹配 + 尾部数字边界校验
        target = self._lan_norm(std_no)
        if not target:
            return None
        for _parsed, stem, f in items:
            idx = stem.find(target)
            if idx == -1:
                continue
            if stem[idx + len(target):][:1].isdigit():
                continue
            return f
        return None

    def _iter_download_targets(self, results):
        """主标准 + 替代版本一并作为下载探测目标。"""
        targets = []
        for std_info in results:
            targets.append(std_info)
            replacement = getattr(std_info, "replacement", None)
            if replacement is not None:
                targets.append(replacement)
        return targets

    def _skip_official_fulltext_download(self, std_info):
        """查询源为标信/国标全文且带「采」标：官方均不可下，只走其它下载源。"""
        if not getattr(std_info, 'is_adopted', False):
            return False
        site = (getattr(std_info, 'source_site', None) or '').strip()
        return site in ('国家标准全文', '全国标准信息公共服务平台')

    def _iter_download_source_configs(self, std_info=None):
        """按优先级产出下载源配置；「采」标跳过 std_samr / openstd。"""
        skip_official = self._skip_official_fulltext_download(std_info) if std_info else False
        for source in Config.DOWNLOAD_SOURCES:
            parser = source.get('parser', '')
            if skip_official and parser in ('std_samr', 'openstd'):
                continue
            yield source

    def _try_mark_downloadable(self, std_info, log_prefix="下载源检测"):
        """按 DOWNLOAD_SOURCES 优先级探测；命中则标记可下载并返回 True。"""
        if self._skip_official_fulltext_download(std_info):
            logger.info(
                f"[{log_prefix}] {std_info.std_no} 为「采」标"
                f"（来源：{std_info.source_site}），"
                f"跳过全国标准信息公共服务平台与国家标准全文，改走其它源"
            )
        for source in self._iter_download_source_configs(std_info):
            parser = source.get('parser', '')

            if parser == 'std_samr':
                samr_url = getattr(std_info, 'samr_text_url', None)
                if not samr_url and std_info.source_site != '全国标准信息公共服务平台':
                    samr_url = self._fetch_samr_text_url(std_info.std_no)
                if samr_url:
                    std_info.download_status = "可下载"
                    std_info.download_url = samr_url
                    std_info.detail_url = std_info.detail_url or std_info.download_url
                    std_info.download_sources = ['std_samr']
                    logger.info(f"[{log_prefix}] {std_info.std_no} 在全国标准信息公共服务平台可下载/查看")
                    return True

            elif parser == 'openstd':
                available, download_url, detail_url = self._check_openstd_available(std_info.std_no)
                if available:
                    std_info.download_status = "可下载"
                    std_info.download_url = download_url
                    std_info.detail_url = detail_url
                    std_info.download_sources = ['openstd']
                    logger.info(f"[{log_prefix}] {std_info.std_no} 在国家标准全文可下载")
                    return True

            elif parser == 'nrsis':
                available, detail_url, pdf_url = self._check_nrsis_available(std_info.std_no)
                if available:
                    std_info.download_status = "可下载"
                    std_info.download_url = pdf_url or ""
                    std_info.detail_url = detail_url or std_info.detail_url
                    std_info.download_sources = ['nrsis']
                    logger.info(f"[{log_prefix}] {std_info.std_no} 在自然资源网可下载")
                    return True

            elif parser == 'webfree':
                available, detail_url = self._check_webfree_available(std_info.std_no)
                if available:
                    std_info.download_status = "可下载"
                    std_info.download_url = ""  # 清除旧 URL，避免误走其他站点
                    std_info.detail_url = detail_url
                    std_info.download_sources = ['webfree']
                    logger.info(f"[{log_prefix}] {std_info.std_no} 在协筑资源可下载")
                    return True

            elif parser == 'gxzl':
                available, detail_url = self._check_gxzl_available(std_info.std_no)
                if available:
                    std_info.download_status = "可下载"
                    std_info.download_url = ""
                    std_info.detail_url = detail_url
                    std_info.download_sources = ['gxzl']
                    logger.info(f"[{log_prefix}] {std_info.std_no} 在中国东盟标准云平台可下载")
                    return True

        return False

    def _check_download_sources(self, results):
        """
        遍历网络下载源检测是否可下载（仅检测网络源，本地搜索已独立到 LocalSearchWorker）。
        更新结果中 download_status 为"可下载"如果任一网络下载源有该标准
        （同时检测废止标准的替代版本，确保单本查询时替代版本可执行下载轮动查找）
        """
        try:
            targets = self._iter_download_targets(results)
            logger.info(f"[下载源检测] 开始检测 {len(targets)} 条结果（仅网络源）")

            for std_info in targets:
                # 解析阶段已标记可下载（如 openstd 的 hcno 链接），保留网络下载链接
                if std_info.download_status == "可下载":
                    continue
                self._try_mark_downloadable(std_info, "下载源检测")

            logger.info("[下载源检测] 检测完成")

        except Exception as e:
            logger.warning(f"[下载源检测] 异常: {str(e)}")

    def detect_download_for_results(self, results):
        """按需下载源探测：仅处理 download_status 为「检测中」的行（含废止标准的替代版本）。
        逐个探测网络下载源：任一源命中→标记「可下载」并写入下载/详情链接；
        全部未命中→标记「未找到资源」（openstd 标注「采」的采标标准即在此归为未找到）。
        本地命中、解析已确定（可下载/详情页）等行由调用方在标记阶段排除，不会进入本方法。"""
        try:
            pending = [s for s in self._iter_download_targets(results) if s.download_status == "检测中"]
            logger.info(f"[按需下载探测] 开始探测 {len(pending)} 条")

            for std_info in pending:
                if not self._try_mark_downloadable(std_info, "按需下载探测"):
                    std_info.download_status = "未找到资源"
                    logger.info(f"[按需下载探测] {std_info.std_no} 未找到可下载资源")

            logger.info("[按需下载探测] 探测完成")

        except Exception as e:
            logger.warning(f"[按需下载探测] 异常: {str(e)}")

    def _search_openstd(self, keyword):
        """在国家标准全文检索，返回StandardInfo列表（委托 OpenstdSpider，避免引擎内重复解析）。"""
        site = next((s for s in Config.PRIMARY_SITES if s['parser'] == 'openstd'), None)
        if not site:
            logger.warning("_search_openstd: 未找到 openstd 站点配置")
            return []
        spider = self.spider_manager.get('openstd')
        if not spider:
            logger.warning("_search_openstd: openstd 爬虫未注册")
            return []
        # 与主检索路径一致：经 DataProcessor 过滤（spider 内不再重复过滤）
        return self.data_processor.filter_results(spider.search(keyword, site) or [], keyword)

    def _csres_get(self, url):
        """请求工标网页面（GBK编码）"""
        resp = self._get(
            url, timeout=Config.TIMEOUT,
            headers={'Referer': 'http://www.csres.com/'}
        )
        resp.encoding = 'gb2312'
        return resp

    def _find_csres_detail_url(self, std_no):
        """在工标网搜索标准编号，返回精确匹配的详情页URL"""
        kw = (std_no or "").strip()
        if not kw:
            return None
        url = "http://www.csres.com/s.jsp?keyword=" + self._encode_csres_keyword(kw)
        resp = self._csres_get(url)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, 'html.parser')
        norm = lambda x: x.replace(' ', '').replace('\u3000', '')
        for a in soup.find_all('a', href=True):
            if re.match(r'/detail/\d+\.html', a['href']):
                txt = a.get_text(strip=False)
                if norm(txt) == norm(kw):
                    return urljoin("http://www.csres.com/", a['href'])
        return None
    
    def find_replacement_no_csres(self, std_no):
        """从工标网详情页的'替代情况'字段解析'被XXX代替'的替代标准编号"""
        try:
            detail_url = self._find_csres_detail_url(std_no)
            if not detail_url:
                return None
            resp = self._csres_get(detail_url)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, 'html.parser')
            text = re.sub(r'\s+', ' ', soup.get_text(' ', strip=True))
            # 只取"被XXX代替"（表示本标准被新标准替代），忽略"替代XXX"（本标准替代的旧标准）
            m = re.search(r'被\s*([A-Z]+(?:/[A-Z]+)?\s*[\d.]+-\d{4})\s*代替', text)
            if m:
                return re.sub(r'\s+', ' ', m.group(1)).strip()
            return None
        except Exception as e:
            logger.warning(f"工标网查替代异常: {str(e)}")
            return None
    
    def _build_replacement_std(self, replace_no):
        """用替代编号走完整查询流程（PRIMARY_SITES，命中即停），补全为可下载的StandardInfo；
        查不到则返回基础对象。enrich_replacements=False 防止递归调用。
        结果缓存到实例级 _replacement_cache，避免多个变体重复查询同一替代编号。"""
        cache = getattr(self, '_replacement_cache', None)
        if cache is None:
            self._replacement_cache = {}
            cache = self._replacement_cache
        cache_key = replace_no.replace(' ', '').replace('\u3000', '').upper()
        if cache_key in cache:
            return cache[cache_key]
        try:
            results = self._search_standard_core(replace_no, check_download=False, enrich_replacements=False, stop_on_first=True)
            for c in results:
                if c.std_no.replace(' ', '') == replace_no.replace(' ', ''):
                    cache[cache_key] = c
                    return c
        except Exception as e:
            logger.warning("替代标准查询处理失败: %s", e)
        std = StandardInfo(std_no=replace_no, status="现行", source_site="工标网")
        std.download_status = "未检测"
        cache[cache_key] = std
        return std
    
    @staticmethod
    def _prefix_replacement_compatible(orig_no, cand_no):
        """检查两个标准编号的前缀是否可视为替代关系。
        - GB/Z 是独立文档类型（指导性技术文件），不与无/Z 互替
        - GB↔GB/T、SL↔SL/T 等除 /Z 外的 /T 差异允许互替"""
        def _pfx(n):
            m = re.match(r'^([A-Z]+(?:/[A-Z]+)?)', (n or '').strip().upper())
            return m.group(1) if m else ''
        op = _pfx(orig_no)
        cp = _pfx(cand_no)
        if not op or not cp:
            return True  # 无法提取时保守允许
        # /Z 必须严格匹配
        if ('/Z' in op) != ('/Z' in cp):
            return False
        # 去掉 /T 后比较 base prefix
        return op.replace('/T', '') == cp.replace('/T', '')

    def _find_replacement_openstd_family(self, std_info, require_newer=False):
        """回退方案：openstd同编号族检索，取现行/即将实施的最新版本（放宽GB/GB\u002fT前缀）。
        require_newer=True 时（现行标准兜底）仅接受年份严格大于原标准的版本，
        避免把同编号的历史旧版误判为替代。"""
        try:
            core = self._std_no_core(std_info.std_no)
            if not core:
                return None
            _, base_y = self._std_no_stem(std_info.std_no)
            base_year = int(base_y) if (base_y and base_y.isdigit()) else -1
            best = None
            best_year = -1
            for c in self._search_openstd(core):
                if self._std_no_core(c.std_no) != core:
                    continue
                if not any(k in (c.status or "") for k in ("现行", "即将实施")):
                    continue
                if c.std_no.strip() == std_info.std_no.strip():
                    continue
                # 前缀类型兼容性：拒绝 GB/Z 误认为 GB 的替代
                if not self._prefix_replacement_compatible(std_info.std_no, c.std_no):
                    continue
                _, cy = self._std_no_stem(c.std_no)
                year_val = int(cy) if (cy and cy.isdigit()) else 0
                if require_newer and base_year > 0 and year_val <= base_year:
                    continue
                if year_val > best_year:
                    best_year = year_val
                    best = c
            return best
        except Exception as e:
            logger.warning(f"openstd同族查替代异常: {str(e)}")
            return None
    
    def find_replacement(self, std_info):
        """
        查找标准的替代规范。
        废止/作废标准：工标网'被XXX代替'字段优先，无果回退openstd同编号族最新版本。
        现行标准：工标网'被XXX代替'字段优先；工标网超时/无果时回退openstd同编号族，
                  但仅接受年份严格大于原版的现行/即将实施版本，避免历史旧版被误判为替代。
        返回: 替代标准StandardInfo，或None（无替代）
        """
        is_abolished = is_obsolete_status(std_info.status or "")

        # 方案1：工标网替代情况字段（对废止、现行均精确）
        replace_no = self.find_replacement_no_csres(std_info.std_no)
        if replace_no:
            return self._build_replacement_std(replace_no)

        # 方案2：openstd同编号族回退
        #   - 废止标准：取现行/即将实施最新版本
        #   - 现行标准：工标网超时/无果时兜底，仅接受年份更新的版本
        #     （修复工标网超时导致 GB/T 1345-2025 等即将替代规范漏显）
        return self._find_replacement_openstd_family(std_info, require_newer=not is_abolished)
    
    def _enrich_replacements(self, results, include_current=False):
        """遍历检索结果补充替代规范信息。
        默认仅处理废止/作废标准；include_current=True 时（单本精确查询）
        额外为现行标准检测'被XXX代替'字段，以便提示已发布的替代版本。

        优先级：
        1. 站点已挂好 replacement（如国标全文同族列表）→ 跳过
        2. replacement_detail_url → 跟链打开详情（工标网/全国标准），不再搜索
        3. replacement_no → 轻量按编号补全
        4. 否则 find_replacement 网络兜底
        """
        for std in results:
            self._check_cancelled()

            is_abolished = is_obsolete_status(std.status or "")
            if not (is_abolished or include_current):
                continue

            # 站点解析阶段已挂接替代（openstd 同族），无需再查
            if getattr(std, "replacement", None) is not None:
                continue

            self.progress_updated.emit(f"正在查询替代规范: {std.std_no}")
            replacement = None

            detail_url = getattr(std, "replacement_detail_url", None) or ""
            if detail_url:
                replacement = self._fetch_replacement_from_detail(std, detail_url)

            if replacement is None and std.replacement_no:
                replacement = self._build_replacement_light(std.replacement_no)

            if replacement is None:
                # 详情未给出替代线索，或跟链/轻量失败（废止）时再全网兜底
                if not (detail_url or std.replacement_no) or is_abolished:
                    replacement = self.find_replacement(std)

            if replacement:
                std.replacement = replacement
            elif is_abolished:
                std.no_replacement = True

    def _fetch_replacement_from_detail(self, std, detail_url):
        """按主标准来源跟链打开替代详情页，得到完整 StandardInfo。"""
        try:
            source = (std.source_site or "")
            parser = None
            if "工标网" in source or "csres.com" in detail_url:
                parser = "csres"
            elif "全国标准" in source or "std.samr.gov.cn" in detail_url:
                parser = "std_samr"

            spider = self.spider_manager.get(parser) if parser else None
            if spider is None or not hasattr(spider, "fetch_standard_from_detail"):
                return None
            repl = spider.fetch_standard_from_detail(detail_url)
            if repl and repl.std_no:
                logger.info(
                    f"[替代跟链] {std.std_no} → {repl.std_no} via {detail_url}"
                )
                return repl
        except Exception as e:
            logger.warning(f"[替代跟链] 失败 {detail_url}: {e}")
        return None
    

    def _build_replacement_light(self, replace_no):
        """轻量替代标准查询：先查搜索结果缓存，再搜 csres 一页（不要详情页），最后回退全量搜索"""
        cache = self._replacement_cache
        cache_key = replace_no.replace(' ', '').replace('　', '').upper()
        if cache_key in cache:
            return cache[cache_key]

        # 方案A：从 _csres_candidates 缓存查找（零请求）
        csres_cache = getattr(self, '_csres_candidates', None)
        if csres_cache and cache_key in csres_cache:
            std_no, std_name, status, detail_url = csres_cache[cache_key]
            std = StandardInfo(std_no=std_no, std_name=std_name, status=status, source_site="工标网", detail_url=detail_url)
            std.download_status = "未检测"
            cache[cache_key] = std
            return std

        # 方案B：搜索 csres 一页，取表格行（1 请求，不要详情页）
        try:
            url = "http://www.csres.com/s.jsp?keyword=" + self._encode_csres_keyword(replace_no)
            resp = self._csres_get(url)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                table = soup.find('table', class_='heng')
                if table:
                    for tr in table.find_all('tr'):
                        tds = tr.find_all('td')
                        if len(tds) < 5:
                            continue
                        a_tag = tds[0].find('a')
                        if not a_tag:
                            continue
                        std_no = a_tag.get_text(strip=True)
                        if std_no.replace(' ', '').replace('　', '').upper() == cache_key:
                            std_name = tds[1].get_text(strip=True) if len(tds) >= 2 else ''
                            status = tds[4].get_text(strip=True) if len(tds) >= 5 else ''
                            detail_url = urljoin("http://www.csres.com/", a_tag['href'])
                            std = StandardInfo(std_no=std_no, std_name=std_name, status=status, source_site="工标网", detail_url=detail_url)
                            std.download_status = "未检测"
                            cache[cache_key] = std
                            return std
        except Exception as e:
            logger.warning(f"轻量替代标准查询失败: {e}")

        # 方案C：回退全量主站搜索（PRIMARY_SITES）
        return self._build_replacement_std(replace_no)


    def download_pdf(self, standard_info, captcha_callback=None):
        """
        下载PDF文件 - 按 Config.DOWNLOAD_SOURCES 优先级轮动：
        标信平台 → 国家标准全文 → 协筑资源 → 东盟云平台 → 自然资源网（任一源命中即停止）。
        源间加入随机延时与 User-Agent 轮换以降低IP被风控识别的概率。
        版本判断：废止标准不下载其本身；若存在有效替代版本则改下载替代版本。
        返回: (success, message)
        """
        try:
            # 版本判断：废止/作废标准不执行下载轮动查找；若有有效替代版本则改下载替代版本
            if is_obsolete_status(standard_info.status or ""):
                replacement = getattr(standard_info, "replacement", None)
                if replacement is not None and not any(
                    k in (replacement.status or "") for k in ("废止", "作废")
                ):
                    logger.info(f"[版本判断] {standard_info.std_no} 已废止，改为下载替代版本 {replacement.std_no}")
                    standard_info = replacement
                else:
                    return False, "UNAVAILABLE:该标准已废止且无有效替代版本"
        
            # 文件命名：标准编号《标准名称》，例 GB／T 18242-2025《…》
            # 书名号《》保留；半角 / 在 _safe_filename 中转为全角／（Windows 禁止文件名含 /）
            raw_name = f"{standard_info.std_no}《{standard_info.std_name}》"
            safe_name = _safe_filename(raw_name)
            filename = f"{safe_name}.pdf"
            filepath = self.download_dir / filename
        
            counter = 1
            while filepath.exists():
                filename = f"{safe_name}_{counter}.pdf"
                filepath = self.download_dir / filename
                counter += 1

            # 局域网共享优先：命中则本地分发（复制到下载目录）并终止全部网络检索/下载
            lan_file = self._find_in_lan(standard_info.std_no)
            if lan_file is not None:
                try:
                    dest = self.download_dir / f"{safe_name}{lan_file.suffix}"
                    c = 1
                    while dest.exists():
                        dest = self.download_dir / f"{safe_name}_{c}{lan_file.suffix}"
                        c += 1
                    shutil.copy2(str(lan_file), str(dest))
                    logger.info(f"[局域网命中] {standard_info.std_no} → {lan_file}（本地分发完成，终止网络下载）")
                    # LOCAL: 前缀用于区分本地分发与网络下载，外层据此显示"本地下载"
                    return True, "LOCAL:" + str(dest)
                except Exception as e:
                    # 命中但分发失败（如权限/共享中断），放行线上渠道兜底，避免直接失败
                    logger.warning(f"[局域网命中但分发失败] {e}，改走线上渠道")

            # 轮动查找：按优先级依次轮流尝试各下载源，任一源命中即停止
            # 探测仅命中 gxzl 时（东盟预览确认路径）只走东盟，避免再空转其它源
            # 国标全文/标信「采」标：跳过标信/官方全文，只走协筑/东盟等
            gxzl_only = list(getattr(standard_info, 'download_sources', None) or []) == ['gxzl']
            skip_official = self._skip_official_fulltext_download(standard_info)
            if skip_official:
                logger.info(
                    f"[轮动下载] {standard_info.std_no} 为「采」标"
                    f"（来源：{standard_info.source_site}），"
                    f"跳过全国标准信息公共服务平台与国家标准全文"
                )
            for idx, source in enumerate(self._iter_download_source_configs(standard_info)):
                source_name = source['name']
                parser = source.get('parser', '')
                if gxzl_only and parser != 'gxzl':
                    continue
            
                # 源间随机延时 + User-Agent 轮换，降低同一IP被风控识别的概率
                if idx > 0:
                    time.sleep(random.uniform(*Config.DOWNLOAD_DELAY_RANGE))
                self._rotate_user_agent()
            
                logger.debug(f"[轮动下载] 尝试第 {idx + 1} 个下载源: {source_name}")
            
                # 全国标准信息公共服务平台下载流程
                if parser == 'std_samr':
                    samr_url = getattr(standard_info, 'samr_text_url', None)
                    if samr_url:
                        logger.debug(f"[std_samr下载] samr_url={samr_url}")
                        if 'openstd.samr.gov.cn' in samr_url:
                            # 情况B：GB国家标准 → 跳转到"国家标准全文"平台，走openstd下载流程
                            hcno_match = re.search(r'hcno=([^&]+)', samr_url)
                            if hcno_match:
                                hcno = hcno_match.group(1).strip()
                                logger.info(f"[std_samr下载] GB标准跳转到国家标准全文, hcno={hcno}")
                                if not hcno:
                                    logger.warning(f"[std_samr下载] hcno 为空，samr_url={samr_url}")
                                    continue
                                try:
                                    from .openstd_downloader import OpenstdPdfDownloader
                                    logger.info(f"[std_samr下载] 成功导入 OpenstdPdfDownloader")
                                    downloader = OpenstdPdfDownloader(progress_callback=self.progress_updated.emit)
                                    logger.info(f"[std_samr下载] 创建下载器实例成功，开始下载...")
                                    success, message = downloader.download(hcno, filepath, captcha_callback)
                                    logger.info(f"[std_samr下载] 下载完成: success={success}, message={message}")
                                    if success:
                                        return True, message
                                    # HTML/验证码：立刻浏览器降级（URL 已附在 message 中）
                                    if message.startswith("NEED_BROWSER:"):
                                        return False, message
                                    # EMPTY_PDF：继续轮动其它下载源
                                    if message.startswith("EMPTY_PDF:"):
                                        # 记下 openstd 详情页，全部失败时供浏览器降级
                                        standard_info._openstd_browser_url = message.split("EMPTY_PDF:", 1)[1].strip()
                                        logger.info("[std_samr下载] openstd 空包，继续尝试其它下载源")
                                        continue
                                except Exception as e:
                                    logger.error(f"[std_samr下载] openstd下载异常: {e}", exc_info=True)
                                    # 异常不中断整链，继续下一源
                                    continue
                        else:
                            # 情况A：DB地方标准等 → 直接从 filePath URL 下载PDF
                            logger.info(f"[std_samr下载] 直接下载: {samr_url[:80]}")
                            from .samr_downloader import SamrPdfDownloader
                            downloader = SamrPdfDownloader(progress_callback=self.progress_updated.emit)
                            success, message = downloader.download(samr_url, filepath)
                            if success:
                                return True, message
            
                # 国家标准全文专用下载流程（用户要求保留，不修改）
                if parser == 'openstd':
                    if 'openstd.samr.gov.cn' in (standard_info.download_url or '') and 'viewGb' in (standard_info.download_url or ''):
                        hcno_match = re.search(r'hcno=([^&]+)', standard_info.download_url)
                        if hcno_match:
                            hcno = hcno_match.group(1)
                            from .openstd_downloader import OpenstdPdfDownloader
                            downloader = OpenstdPdfDownloader(progress_callback=self.progress_updated.emit)
                            success, message = downloader.download(hcno, filepath, captcha_callback)
                            if success:
                                return True, message
                            if message.startswith("NEED_BROWSER:"):
                                return False, message
                            if message.startswith("EMPTY_PDF:"):
                                standard_info._openstd_browser_url = message.split("EMPTY_PDF:", 1)[1].strip()
                                logger.info("[openstd下载] 空包，继续尝试其它下载源")
                                continue
            
                # 自然资源网：详情页 PDF 图标 readPdf 入口
                elif parser == 'nrsis':
                    from .nrsis_downloader import NrsisPdfDownloader
                    downloader = NrsisPdfDownloader(
                        self.session, progress_callback=self.progress_updated.emit
                    )
                    pdf_url = (standard_info.download_url or "").strip()
                    if pdf_url:
                        if "/mnr_kfs/" in pdf_url and "nrsis.org.cn" not in pdf_url:
                            pdf_url = urljoin("http://www.nrsis.org.cn/", pdf_url)
                        elif "nrsis.org.cn" not in pdf_url and "openstd.samr.gov.cn" not in pdf_url:
                            pdf_url = ""
                    success, message = downloader.download(
                        standard_info.std_no,
                        filepath,
                        pdf_url=pdf_url or None,
                        detail_url=(standard_info.detail_url or None),
                    )
                    if success:
                        return True, message
                    if message.startswith("NEED_BROWSER:"):
                        return False, message

                # 协筑资源专用下载流程
                elif parser == 'webfree':
                    from .webfree_downloader import WebfreePdfDownloader
                    downloader = WebfreePdfDownloader(self.session, progress_callback=self.progress_updated.emit)
                    success, message = downloader.download(standard_info.std_no, filepath, source.get('password'))
                    if success:
                        return True, message
                
                # 中国东盟标准云平台专用下载流程（逐页下载图片合并为PDF）
                elif parser == 'gxzl':
                    success, message = self._download_gxzl_pdf(standard_info.std_no, filepath)
                    if success:
                        return True, message
        
            # 所有下载源都尝试过仍失败
            # 若 openstd 曾空包，优先提示打开国家标准全文详情页（可点「下载标准」），
            # 而不是打开全国标准平台详情页（只有「查看文本」，无法直接下 PDF）
            openstd_url = getattr(standard_info, '_openstd_browser_url', None)
            if openstd_url:
                return False, f"NEED_BROWSER:{openstd_url}"
            return False, "UNAVAILABLE:该标准暂无可下载文件"
            
        except Exception as e:
            return False, f"下载异常: {str(e)}"

    def _download_gxzl_pdf(self, std_no, filepath):
        """
        中国东盟标准云平台PDF下载：逐页下载图片，合并为PDF。
        返回: (success, message)
        """
        try:
            from .gxzl_pdf_downloader import GxzlPdfDownloader
            
            logger.info(f"[gxzl下载] 开始下载: {std_no}")
            
            downloader = GxzlPdfDownloader()
            
            # 进度回调
            def progress_callback(current, total):
                if total > 0:
                    pct = int(current / total * 100)
                    self.progress_updated.emit(f"下载中 {current}/{total} 页 ({pct}%)")
            
            success = downloader.download_standard(std_no, str(filepath), progress_callback)
            
            if success:
                logger.info(f"[gxzl下载] 下载成功: {filepath}")
                return True, str(filepath)
            else:
                logger.warning(f"[gxzl下载] 下载失败: {std_no}")
                return False, "gxzl下载失败"
                
        except ImportError as e:
            logger.error(f"[gxzl下载] 导入GxzlPdfDownloader失败: {e}")
            return False, "gxzl模块导入失败"
        except Exception as e:
            logger.error(f"[gxzl下载] 下载异常: {e}")
            return False, f"下载异常: {str(e)}"

    def open_detail_in_browser(self, standard_info):
        """
        下载失败后的降级方案：在系统浏览器中打开可下载的详情页。
        优先 openstd 国家标准全文页（可点「下载标准」），避免打开
        std.samr.gov.cn 详情页（仅有「查看文本」，无法直接下 PDF）。
        """
        url = (
            getattr(standard_info, '_openstd_browser_url', None)
            or (
                standard_info.download_url
                if standard_info.download_url
                and 'openstd.samr.gov.cn' in standard_info.download_url
                else None
            )
            or standard_info.detail_url
            or standard_info.download_url
        )
        # viewGb 直链不适合给人点，改成详情页
        if url and 'viewGb' in url and 'hcno=' in url:
            m = re.search(r'hcno=([^&]+)', url)
            if m:
                url = f"https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno={m.group(1)}&refer=outter"
        if url and url.startswith(('http://', 'https://')):
            webbrowser.open(url)
            return True
        return False

    def _fetch_samr_text_url(self, std_no):
        """主动访问全国标准信息公共服务平台，搜索标准并提取详情页中的"查看文本"URL。
        返回: samr_text_url 或 None"""
        try:
            from urllib.parse import quote
            # 与 SamrSpider 一致：列表在 iframe stdPage，外层 /search/std 无 pid
            search_url = f'https://std.samr.gov.cn/search/stdPage?q={quote(std_no)}&tid='
            resp = self._get(search_url, timeout=Config.TIMEOUT)
            if resp.status_code != 200:
                return None
        
            soup = BeautifulSoup(resp.text, 'html.parser')
            a_tag = None
            # 优先精确匹配编号行上的详情链接
            target = re.sub(r'\s+', '', (std_no or '')).upper()
            for post in soup.find_all('div', class_=['panel', 'post']):
                en = post.find('span', class_='en-code')
                if not en:
                    continue
                code = en.get_text(strip=False).replace(' ', '').replace('\n', '').upper()
                if code.replace('/', '') != target.replace('/', '') and code != target:
                    # 宽松：去空格后包含
                    if target not in code and code not in target:
                        continue
                a_tag = post.find('a', attrs={'pid': True})
                if a_tag:
                    break
            if not a_tag:
                a_tag = soup.find('a', attrs={'pid': True})
            if not a_tag:
                return None
        
            pid = a_tag.get('pid', '')
            tid = a_tag.get('tid', '')
            if tid == 'BV_HB':
                detail_url = f'https://std.samr.gov.cn/hb/search/stdHBDetailed?id={pid}'
            elif tid == 'BV_DB':
                detail_url = f'https://std.samr.gov.cn/db/search/stdDBDetailed?id={pid}'
            else:
                detail_url = f'https://std.samr.gov.cn/gb/search/gbDetailed?id={pid}'
        
            detail_resp = self._get(detail_url, timeout=Config.TIMEOUT)
            if detail_resp.status_code != 200:
                return None
        
            html = detail_resp.text
        
            # 情况A：提取 filePath（DB地方标准等直接下载URL）
            fp_match = re.search(r"\.openhdbpdf[\s\S]*?var\s+filePath\s*=\s*'([^']+)'", html)
            if fp_match:
                fp_url = fp_match.group(1)
                if fp_url and fp_url.startswith('http'):
                    # 如果是行业标准平台 hbba.sacinfo.org.cn，将 onlineRead 或 portal/online 转换为 portal/download
                    if 'hbba.sacinfo.org.cn' in fp_url:
                        hash_match = re.search(r'/attachment/onlineRead/([a-f0-9]+)', fp_url)
                        if hash_match:
                            hash_val = hash_match.group(1)
                            fp_url = f'https://hbba.sacinfo.org.cn/portal/download/{hash_val}'
                            logger.info(f"[_fetch_samr_text_url] 行业标准下载URL: {fp_url[:80]}")
                        else:
                            hash_match = re.search(r'/portal/online/([a-f0-9]+)', fp_url)
                            if hash_match:
                                hash_val = hash_match.group(1)
                                fp_url = f'https://hbba.sacinfo.org.cn/portal/download/{hash_val}'
                                logger.info(f"[_fetch_samr_text_url] 行业标准下载URL: {fp_url[:80]}")
                    logger.info(f"[_fetch_samr_text_url] 提取到filePath: {fp_url[:80]}")
                    return fp_url
        
            # 情况B：提取 hcno（GB国家标准跳转到openstd）
            hcno_match = re.search(r"\.openpdf[\s\S]*?hcno=([^'\"]+)", html)
            if hcno_match:
                hcno_val = hcno_match.group(1).strip()
                if hcno_val:
                    url = f'http://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno={hcno_val}'
                    logger.info(f"[_fetch_samr_text_url] 提取到openstd hcno: {hcno_val}")
                    return url

            # 情况C：宽松匹配页面中任意 hcno=
            hcno_any = re.search(r'hcno=([A-Fa-f0-9]{16,})', html)
            if hcno_any:
                hcno_val = hcno_any.group(1).strip()
                url = f'http://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno={hcno_val}'
                logger.info(f"[_fetch_samr_text_url] 宽松提取 hcno: {hcno_val}")
                return url

            return None
        except Exception as e:
            logger.warning(f"[_fetch_samr_text_url] 异常: {e}")
            return None

    def _check_webfree_available(self, std_no):
        """
        检测协筑资源是否有该标准可下载
        返回: (bool, detail_url)
        """
        try:
            logger.debug(f"[协筑资源] 检测是否有标准: {std_no}")
        
            # webfree 搜索对空格敏感，去除编号空格以保证精确条目出现在结果窗口内
            search_url = f"https://www.webfree.net/1087-2?search={quote(std_no.replace(' ', ''))}"
            resp = self._get(search_url, timeout=Config.TIMEOUT)
        
            if resp.status_code != 200:
                return False, ""
        
            soup = BeautifulSoup(resp.text, 'html.parser')
        
            std_no_clean = std_no.replace(' ', '').replace('/', '')
        
            for a in soup.find_all('a', href=True):
                href = a['href']
                txt = a.get_text(strip=True)
                txt_clean = txt.replace(' ', '').replace('/', '')
            
                if '/download/' in href and std_no_clean in txt_clean:
                    if not href.startswith('http'):
                        href = urljoin("https://www.webfree.net/", href)
                    return True, href
        
            return False, ""
        
        except Exception as e:
            logger.warning(f"[协筑资源] 检测异常: {str(e)}")
            return False, ""

    def _check_nrsis_available(self, std_no):
        """检测自然资源网详情页 PDF 图标是否可用。
        返回: (bool, detail_url, pdf_url)"""
        try:
            from .nrsis_downloader import NrsisPdfDownloader
            dl = NrsisPdfDownloader(self.session)
            detail_url, pdf_url = dl.find_detail_and_pdf(std_no)
            if detail_url and pdf_url:
                return True, detail_url, pdf_url
            return False, "", ""
        except Exception as e:
            logger.warning(f"[自然资源网] 下载探测异常: {e}")
            return False, "", ""

    def _check_gxzl_available(self, std_no):
        """检测中国东盟标准云平台是否有该标准可下载（单次探测页数 API）。
        返回: (bool, detail_url)"""
        try:
            url = "https://bz.gxzl.org.cn/api/gxist-standard/standardstd/read-pages"
            resp = self._get(url, params={"page": 0, "no": std_no}, timeout=Config.TIMEOUT)
            if resp.status_code != 200:
                return False, ""
            data = resp.json()
            pages = 0
            if isinstance(data, dict):
                pages = data.get('data', 0) or data.get('total', 0) or data.get('pages', 0) or 0
            elif isinstance(data, int):
                pages = data
            if pages and int(pages) > 0:
                detail = f"https://bz.gxzl.org.cn/standard/queryList?stdName={quote(std_no)}"
                return True, detail
            return False, ""
        except Exception as e:
            logger.warning(f"[东盟云平台] 下载探测异常: {e}")
            return False, ""

    def _check_openstd_available(self, std_no):
        """
        检测国家标准全文是否有该标准可下载
        返回: (bool, download_url, detail_url)
        """
        try:
            logger.debug(f"[国家标准全文] 检测是否有标准: {std_no}")
        
            site = next((s for s in Config.PRIMARY_SITES if s['parser'] == 'openstd'), None)
            if not site:
                return False, "", ""
            search_url = site['search_url'].format(keyword=quote(std_no))
            resp = self._get(search_url, timeout=Config.TIMEOUT)
        
            if resp.status_code != 200:
                return False, "", ""
        
            soup = BeautifulSoup(resp.text, 'html.parser')
        
            tables = soup.find_all('table')
            for table in tables:
                rows = table.find_all('tr')[1:]
                for row in rows:
                    cells = row.find_all('td')
                    if len(cells) >= 8:
                        std_no_text = cells[1].get_text(strip=True)
                        if std_no_text and (std_no_text == std_no or std_no.replace(' ', '') in std_no_text.replace(' ', '')):
                            # 检查第3列是否有"采"标识（采用国外标准，不可下载）
                            is_adopted = cells[3].get_text(strip=True) if len(cells) > 3 else ""
                            if '采' in is_adopted:
                                logger.debug(f"[国家标准全文] {std_no} 有采标识，不可下载")
                                return False, "", ""
                        
                            hcno = None
                            std_no_cell = cells[1]
                            a_tags = std_no_cell.find_all('a', href=True)
                            for a in a_tags:
                                onclick = a.get('onclick', '')
                                if onclick:
                                    hcno_match = re.search(r"showInfo\(['\"]([^'\"]+)['\"]\)", onclick)
                                    if hcno_match:
                                        hcno = hcno_match.group(1)
                                        break
                        
                            if hcno:
                                download_url = f"https://openstd.samr.gov.cn/bzgk/std/viewGb?hcno={hcno}"
                                detail_url = f"https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno={hcno}&refer=outter"
                                return True, download_url, detail_url
        
            return False, "", ""
        
        except Exception as e:
            logger.warning(f"[国家标准全文] 检测异常: {str(e)}")
            return False, "", ""

