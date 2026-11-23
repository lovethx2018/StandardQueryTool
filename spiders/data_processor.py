# -*- coding: utf-8 -*-
"""数据处理器。"""
from app_env import logger
from utils import normalize_status_by_date as _normalize_status_by_date_util


class DataProcessor:
    """数据处理器 - 负责查询结果的处理、过滤和状态判定"""
    
    def __init__(self, engine):
        """
        初始化数据处理器
        :param engine: SpiderEngine实例
        """
        self.engine = engine
    
    def normalize_status_by_date(self, results):
        """基于实施日期智能判定标准状态（委托到公共函数）"""
        return _normalize_status_by_date_util(results)
    
    def filter_results(self, results, keyword):
        """
        过滤查询结果
        :param results: list of StandardInfo
        :param keyword: 查询关键词
        :return: list of StandardInfo
        """
        if not results:
            return results
        
        # 过滤脏数据
        results = [r for r in results if self.engine._is_plausible_result(r)]
        
        # 过滤屏蔽状态
        results = [r for r in results if not self.engine._is_status_blocked(r.status)]
        
        # 前缀白名单过滤
        results = [r for r in results if self.engine._is_prefix_allowed(getattr(r, 'std_no', ''))]
        
        # 带四位年号查询：只保留精确匹配该版本的独立结果
        if self.engine._keyword_year(keyword) and results:
            results = [r for r in results if self.engine._std_no_exact_match(r.std_no, keyword)]
        
        # 精确数字匹配（纯数字 / 含数字主体）
        kw_core = self.engine._std_no_core(keyword)
        if kw_core and results:
            results = [r for r in results if self.engine._exact_number_match(r.std_no, keyword)]

        # 带字母前缀时强制前缀一致（避免搜 JTG3460 时 openstd 用数字主体捞回 GB/T 3460）
        if self.engine._keyword_prefix(keyword) and results:
            results = [r for r in results if self.engine._match_std_no(r.std_no, keyword)]
        
        return results
