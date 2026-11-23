# -*- coding: utf-8 -*-
"""爬虫模块。"""
from .spider import SpiderEngine
from .base_spider import BaseSpider
from .spider_manager import SpiderManager
from .data_processor import DataProcessor
from .samr_spider import SamrSpider
from .csres_spider import CsresSpider
from .openstd_spider import OpenstdSpider
from .jtst_spider import JtstSpider
from .mwr_spider import MwrSpider
from .aijsti_spider import AijstiSpider
from .nrsis_spider import NrsisSpider

__all__ = [
    'SpiderEngine',
    'BaseSpider',
    'SpiderManager',
    'DataProcessor',
    'SamrSpider',
    'CsresSpider',
    'OpenstdSpider',
    'JtstSpider',
    'MwrSpider',
    'AijstiSpider',
    'NrsisSpider',
]
