# -*- coding: utf-8 -*-
"""数据模型。"""


class StandardInfo:
    """标准信息数据模型"""
    def __init__(self, std_no="", std_name="", status="", publish_date="", 
                 implement_date="", download_url="", source_site=""):
        self.std_no = std_no
        self.std_name = std_name
        self.status = status
        self.publish_date = publish_date
        self.implement_date = implement_date
        self.download_url = download_url
        self.source_site = source_site
        self.download_status = "未检测"
        self.detail_url = ""  # 详情页URL（备用下载入口）
        self.replacement = None  # 替代标准（StandardInfo），废止标准专用
        self.no_replacement = False  # 是否已确认无替代规范
        self.is_local = False  # 命中文档管理（本地/局域网）目录，PDF操作栏显示"本地打开"直接打开文件
        self.local_file_path = None  # 命中文档管理目录时对应的本地文件路径，供"本地打开"直接使用
        self.is_pending = False  # 批量导入预览占位：仅有序号+标准编号，其余列显示"待检索"
        self.replacement_no = None  # 替代标准编号（从详情页直接提取，避免重复查询）
        # 替代标准详情页直链（工标网 /detail/xxx、全国标准 gbDetailed?id=）；有则跟链取详情，不再搜索
        self.replacement_detail_url = None
        self.samr_text_url = None  # 全国标准信息公共服务平台"查看文本"URL（filePath或openstd链接）
        # 探测命中的下载源 parser 列表（如 ['gxzl']）；仅含 gxzl 时 UI 先弹东盟预览
        self.download_sources = []
        # 国家标准全文/标信列表「采」标识：官方平台不可下全文，探测/下载应跳过 std_samr/openstd
        self.is_adopted = False
    
    def to_dict(self):
        return {
            'std_no': self.std_no,
            'std_name': self.std_name,
            'status': self.status,
            'publish_date': self.publish_date,
            'implement_date': self.implement_date,
            'download_url': self.download_url,
            'source_site': self.source_site,
            'download_status': self.download_status
        }
