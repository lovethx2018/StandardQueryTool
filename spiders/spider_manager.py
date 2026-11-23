# -*- coding: utf-8 -*-
"""爬虫管理器。"""
from app_env import logger


class SpiderManager:
    """爬虫管理器 - 负责爬虫的注册、查找和调度"""
    
    def __init__(self, engine):
        """
        初始化爬虫管理器
        :param engine: SpiderEngine实例
        """
        self.engine = engine
        self._spiders = {}  # parser_name -> spider实例
    
    def register(self, spider_class):
        """
        注册爬虫类
        :param spider_class: 爬虫类（必须继承BaseSpider）
        """
        instance = spider_class(self.engine)
        self._spiders[instance.parser_name] = instance
        logger.debug(f"[SpiderManager] 注册爬虫: {instance.name} ({instance.parser_name})")
    
    def get(self, parser_name):
        """
        获取爬虫实例
        :param parser_name: 解析器名称
        :return: 爬虫实例，未找到返回None
        """
        return self._spiders.get(parser_name)
