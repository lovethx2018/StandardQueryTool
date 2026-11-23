# -*- coding: utf-8 -*-
"""全局配置。"""
import os
import json
import shutil
from app_env import logger, APP_DIR, APP_DIR_WRITABLE, CONFIG_DIR
from utils import atomic_write_json


class Config:
    """全局配置"""
    # 主检索站点（优先级从高到低）——编号/代号查询用
    # 前 N-2 站主查；末 2 站为行业备用（仅前站全空且前缀匹配时运行）：
    # 交通部(JTG) / 水利部(SL)
    PRIMARY_SITES = [
        {
            "name": "全国标准信息公共服务平台",
            "url": "https://std.samr.gov.cn/",
            "search_url": "https://std.samr.gov.cn/search/std?q={keyword}",
            "parser": "std_samr",
        },
        {
            "name": "国家标准全文",
            "url": "http://openstd.samr.gov.cn/bzgk/gb/index",
            "search_url": "http://openstd.samr.gov.cn/bzgk/gb/std_list?p.p1=0&p.p90=circulation_date&p.p91=desc&p.p2={keyword}",
            "parser": "openstd",
        },
        {
            "name": "工标网",
            "url": "http://www.csres.com/",
            "search_url": "http://www.csres.com/s.jsp?keyword={keyword}",
            "parser": "csres",
        },
        {
            "name": "爱查新",
            "url": "https://biaozhun.aijsti.com/",
            "search_url": "https://api.aijsti.com/business/standards/list",
            "parser": "aijsti",
            "api_base": "https://api.aijsti.com",
        },
        {
            "name": "交通运输部标准化信息系统",
            "url": "https://jtst.mot.gov.cn/search/std?q=",
            "search_url": "https://jtst.mot.gov.cn/search/std?q={keyword}",
            "parser": "jtst",
        },
        {
            "name": "水利部国际合作与科技司",
            "url": "http://gjkj.mwr.gov.cn/jsjd1/bzcx/",
            "search_url": "http://121.36.94.83:9008/jsp/yishenqing/appladd/bzsearch.jsp",
            "parser": "mwr",
        },
    ]

    # 汉字名称查询专用站点优先级（与 PRIMARY_SITES 独立）
    CHINESE_NAME_SITES = [
        {
            "name": "工标网",
            "url": "http://www.csres.com/",
            "search_url": "http://www.csres.com/s.jsp?keyword={keyword}",
            "parser": "csres",
        },
        {
            "name": "爱查新",
            "url": "https://biaozhun.aijsti.com/",
            "search_url": "https://api.aijsti.com/business/standards/list",
            "parser": "aijsti",
            "api_base": "https://api.aijsti.com",
        },
        {
            "name": "全国标准信息公共服务平台",
            "url": "https://std.samr.gov.cn/",
            "search_url": "https://std.samr.gov.cn/search/std?q={keyword}",
            "parser": "std_samr",
        },
        {
            "name": "国家标准全文",
            "url": "http://openstd.samr.gov.cn/bzgk/gb/index",
            "search_url": "http://openstd.samr.gov.cn/bzgk/gb/std_list?p.p1=0&p.p90=circulation_date&p.p91=desc&p.p2={keyword}",
            "parser": "openstd",
        },
        {
            "name": "交通运输部标准化信息系统",
            "url": "https://jtst.mot.gov.cn/search/std?q=",
            "search_url": "https://jtst.mot.gov.cn/search/std?q={keyword}",
            "parser": "jtst",
        },
        {
            "name": "水利部国际合作与科技司",
            "url": "http://gjkj.mwr.gov.cn/jsjd1/bzcx/",
            "search_url": "http://121.36.94.83:9008/jsp/yishenqing/appladd/bzsearch.jsp",
            "parser": "mwr",
        },
    ]
    
    # 下载源（独立于搜索源，用于PDF下载）- 优先级从高到低
    # 下载源优先级（高→低）
    DOWNLOAD_SOURCES = [
        {
            "name": "全国标准信息公共服务平台",
            "url": "https://std.samr.gov.cn/",
            "parser": "std_samr",
            "password": None
        },
        {
            "name": "国家标准全文",
            "url": "http://openstd.samr.gov.cn/",
            "parser": "openstd",
            "password": None
        },
        {
            "name": "协筑资源",
            "url": "https://www.webfree.net/home",
            "parser": "webfree",
            # 协筑站点公开共享口令，非用户凭据
            "password": "Webfree.net"
        },
        {
            "name": "中国东盟标准云平台",
            "url": "https://bz.gxzl.org.cn/",
            "parser": "gxzl",
            "password": None,
            "download_method": "image_to_pdf"  # 特殊下载方式：逐页下载图片合并为PDF
        },
    ]
    
    # 请求头（User-Agent 会在运行时随机选择）
    HEADERS = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive'
    }

    # User-Agent 轮换池：单本下载轮动查找时随机切换，降低同一IP被风控识别的概率
    # 客户端目标平台为 Win10/Win11，UA 使用对应 Windows NT 10.0 x64 的现代 Chrome
    USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
    ]

    # 单本下载轮动查找时，各下载源之间的随机延时区间（秒）
    DOWNLOAD_DELAY_RANGE = (1.0, 3.0)
    
    # 下载目录：默认留空（设置界面可为空）；未设置时回退到「电子规范」。
    # 便携版跟随程序目录；安装到 Program Files 等不可写位置时回落用户文档。
    if APP_DIR_WRITABLE:
        DEFAULT_DOWNLOAD_DIR = os.path.join(APP_DIR, "电子规范")
    else:
        DEFAULT_DOWNLOAD_DIR = os.path.join(
            os.path.expanduser("~"), "Documents", "电子规范"
        )
    DOWNLOAD_DIR = ""

    # 局域网标准共享目录（默认留空，使用者在“设置”中自定义；留空则不使用局域网、直接走线上渠道）
    LAN_SHARE_DIR = ""

    # 标准台账功能开关（默认关闭；设置中勾选后才显示导航入口）
    LEDGER_ENABLED = False

    # 是否已提示过「首次使用建议看使用说明」（新电脑第一次运行为 False）
    FIRST_RUN_TIP_SHOWN = False
    
    # 超时设置（部分政府站点响应慢，杀软 SSL 拦截也会额外耗时，设为 15 秒）
    TIMEOUT = 15
    
    # 用户设置：便携版放安装目录 config；安装版放 %APPDATA%\StandardQueryTool\config
    SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
    # 旧版曾写在安装目录 config / 用户主目录，启动时若新路径不存在则自动迁移
    _LEGACY_SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".标准查新工具")
    _LEGACY_SETTINGS_FILE = os.path.join(_LEGACY_SETTINGS_DIR, "settings.json")
    _APP_LEGACY_SETTINGS_FILE = os.path.join(APP_DIR, "config", "settings.json")

    @classmethod
    def effective_download_dir(cls):
        """实际用于落盘的下载目录：用户已在设置中指定则用之，否则回退默认目录。
        这样设置界面可保持“默认为空”，同时下载功能始终有一个有效目录。"""
        return (cls.DOWNLOAD_DIR or "").strip() or cls.DEFAULT_DOWNLOAD_DIR

    @classmethod
    def _migrate_legacy_settings(cls):
        """把旧版配置迁到当前配置目录（目标已存在则不覆盖）。
        兼容两类旧路径：安装目录 config\\、用户主目录 .标准查新工具\\。"""
        if os.path.exists(cls.SETTINGS_FILE):
            return
        candidates = (cls._APP_LEGACY_SETTINGS_FILE, cls._LEGACY_SETTINGS_FILE)
        src = next((p for p in candidates if os.path.isfile(p)), None)
        if not src:
            return
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            shutil.copy2(src, cls.SETTINGS_FILE)
            new_doc = os.path.join(CONFIG_DIR, "doc_scan_cache.json")
            for src_dir in (os.path.dirname(src), cls._LEGACY_SETTINGS_DIR):
                legacy_doc = os.path.join(src_dir, "doc_scan_cache.json")
                if os.path.exists(legacy_doc) and not os.path.exists(new_doc):
                    shutil.copy2(legacy_doc, new_doc)
            logger.info("已迁移旧版设置: %s -> %s", src, cls.SETTINGS_FILE)
        except Exception as e:
            logger.warning("迁移旧版设置失败: %s", e)

    @classmethod
    def load_settings(cls):
        """从安装目录配置文件加载用户设置（如自定义下载目录），覆盖默认值"""
        try:
            cls._migrate_legacy_settings()
            if os.path.exists(cls.SETTINGS_FILE):
                with open(cls.SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                download_dir = data.get('download_dir')
                if download_dir:
                    cls.DOWNLOAD_DIR = download_dir
                lan_share_dir = data.get('lan_share_dir')
                if lan_share_dir is not None:
                    cls.LAN_SHARE_DIR = lan_share_dir
                if 'ledger_enabled' in data:
                    cls.LEDGER_ENABLED = bool(data.get('ledger_enabled'))
                # 已有配置文件：有字段则读；无字段视为非首次安装（升级用户不弹）
                if 'first_run_tip_shown' in data:
                    cls.FIRST_RUN_TIP_SHOWN = bool(data.get('first_run_tip_shown'))
                else:
                    cls.FIRST_RUN_TIP_SHOWN = True
            else:
                # 无配置文件：新电脑首次运行
                cls.FIRST_RUN_TIP_SHOWN = False
        except Exception as e:
            logger.warning("加载设置失败，使用默认配置: %s", e)
    
    @classmethod
    def save_settings(cls):
        """原子写入用户设置；返回是否成功（调用方需据此提示用户）。"""
        data = {
            'download_dir': cls.DOWNLOAD_DIR,
            'lan_share_dir': cls.LAN_SHARE_DIR,
            'ledger_enabled': bool(cls.LEDGER_ENABLED),
            'first_run_tip_shown': bool(cls.FIRST_RUN_TIP_SHOWN),
        }
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            atomic_write_json(cls.SETTINGS_FILE, data)
            return True
        except Exception as e:
            logger.warning("保存设置失败: %s", e)
            return False

# 启动时加载用户设置，确保后续 DOWNLOAD_DIR 使用用户自定义目录
Config.load_settings()
