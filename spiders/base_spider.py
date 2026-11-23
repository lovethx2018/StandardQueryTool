# -*- coding: utf-8 -*-
"""爬虫基类。"""
from abc import ABC, abstractmethod
from http_client import _http_request


class SpiderSearchError(Exception):
    """站点检索失败（网络/HTTP/解析异常），区别于「正常无结果」。
    由 SpiderEngine 捕获后标记 UI 圆点为 failed。"""


class BaseSpider(ABC):
    """爬虫基类 - 定义统一的爬虫接口和通用方法"""
    
    # 子类必须定义
    name = ""  # 爬虫名称
    parser_name = ""  # 解析器标识
    
    def __init__(self, engine):
        """
        初始化爬虫
        :param engine: SpiderEngine实例，提供session和通用方法
        """
        self.engine = engine
    
    @abstractmethod
    def search(self, keyword, site_config):
        """
        搜索标准 - 子类必须实现
        :param keyword: 搜索关键词
        :param site_config: 站点配置字典
        :return: list of StandardInfo
        :raises SpiderSearchError: 网络/HTTP 等失败时抛出（勿吞掉后返回 []）
        """
        pass
    
    def _get(self, url, **kwargs):
        """封装HTTP GET请求"""
        return _http_request(self.engine.session, 'get', url, **kwargs)
    
    def _post(self, url, **kwargs):
        """封装HTTP POST请求"""
        return _http_request(self.engine.session, 'post', url, **kwargs)
    
    def _is_plausible_result(self, std):
        """过滤解析器误提取的脏数据"""
        return self.engine._is_plausible_result(std)
    
    def _is_status_blocked(self, status):
        """检查标准状态是否被屏蔽"""
        return self.engine._is_status_blocked(status)
    
    def _is_prefix_allowed(self, std_no):
        """检查标准前缀是否在白名单中"""
        return self.engine._is_prefix_allowed(std_no)
    
    def _match_std_no(self, std_no, keyword):
        """标准编号匹配"""
        return self.engine._match_std_no(std_no, keyword)
    
    def _std_no_core(self, std_no):
        """提取编号数字主体"""
        return self.engine._std_no_core(std_no)
    
    def _keyword_year(self, keyword):
        """提取查询关键词末尾的年号"""
        return self.engine._keyword_year(keyword)
    
    def _std_no_exact_match(self, std_no, keyword):
        """带年号查询时，判断某结果编号是否与关键词精确对应"""
        return self.engine._std_no_exact_match(std_no, keyword)
    
    def _exact_number_match(self, std_no, keyword):
        """精确数字匹配"""
        return self.engine._exact_number_match(std_no, keyword)
    
    def _is_pure_number_query(self, keyword):
        """判断是否为纯数字编号查询"""
        return self.engine._is_pure_number_query(keyword)
    
    def _normalize_status_by_date(self, results):
        """基于实施日期智能判定状态"""
        return self.engine._normalize_status_by_date(results)
