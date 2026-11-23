# -*- coding: utf-8 -*-
"""共享工具函数：标准编号规范化、安全文件名、文件名解析。"""
import json
import os
import re
import tempfile


def atomic_write_json(path, data, **json_kwargs):
    """原子写入 JSON：先写同目录临时文件，再 os.replace 覆盖目标。

    避免进程崩溃/断电时留下半截 JSON。json_kwargs 传给 json.dump
   （默认 ensure_ascii=False, indent=2）。
    """
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    kwargs = {'ensure_ascii': False, 'indent': 2}
    kwargs.update(json_kwargs)
    fd, tmp_path = tempfile.mkstemp(prefix='.json_', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, **kwargs)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.isfile(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _safe_filename(raw: str, max_length: int = 200) -> str:
    """将字符串转换为安全的 Windows 文件名（不含扩展名）。

    处理步骤：
    1. 半角斜杠 / 改为全角／（Windows 禁止文件名含 /，全角／外观接近 GB/T）
    2. 其余 Windows 非法字符 [\\*?:"<>|] 及 cmd.exe 元字符替换为下划线
    3. 剔除 ASCII 控制字符 (\\x00-\\x1f, \\x7f)
    4. 去除首尾空白与尾部点号（Windows 不允许文件名以点号或空格结尾）
    5. 规避 Windows 保留名（CON/PRN/AUX/NUL/COM1-9/LPT1-9，不区分大小写）
    6. 截断到 max_length 字符，避免超长路径

    保留书名号《》等合法字符。
    """
    # 步骤1: / → 全角／，使下载名呈现为 GB／T 1345-2026《…》而非 GB_T …
    name = raw.replace('/', '\uff0f')
    # 步骤2: 替换其余 Windows 非法字符 + cmd.exe 元字符（防止被 cmd.exe /c start 解释）
    name = re.sub(r'[\\*?:"<>|&()%^]', '_', name)
    # 步骤3: 剔除 ASCII 控制字符
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)
    # 步骤4: 去除首尾空白与尾部点号
    name = name.strip().rstrip('.')
    # 步骤5: 规避 Windows 保留名
    _WINDOWS_RESERVED = {
        'CON', 'PRN', 'AUX', 'NUL',
        'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
        'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
    }
    stem = name
    if '.' in name:
        stem = name.rsplit('.', 1)[0]
    if stem.upper() in _WINDOWS_RESERVED:
        name = name + '_'
    # 步骤6: 截断长度（按字符数，避免半截多字节字符；Python 字符串本身按 unicode 字符处理）
    if len(name) > max_length:
        name = name[:max_length]
    return name


class _FileNameParser:
    """解析标准文档文件名，提取标准编号与标准名称。

    支持格式：
        书名号：   GBT 46605-2025《硫化橡胶或热塑性橡胶 动态耐切割性能的测定》.pdf
        空格分隔： GBT 46605-2025 硫化橡胶或热塑性橡胶 动态耐切割性能的测定.pdf
        纯名称：   硫化橡胶或热塑性橡胶 动态耐切割性能的测定.pdf（无编号）

    名称统一规则：两种带编号格式均去除《》后生成相同的标准名称。
    编号补齐规则：无斜杠的推荐标准前缀末尾为 T 时自动补斜杠，
                  如 GBT→GB/T、CJT→CJ/T、DB11T→DB11/T。
    """

    # 书名号格式：编号《名称》
    PATTERN_BOOKMARK = re.compile(r"(.+?)《(.+?)》")

    # 从开头提取标准编号：前缀(字母开头，含字母/数字/斜杠) + 数字主体 + 分隔 + 4位年号
    # 分隔符支持 连字符-、半角冒号:、全角冒号：（CECS/CJJ 等系列年号用冒号，如 CECS 293:2011）
    # 前缀用惰性匹配，避免数字主体被吞入前缀；适配单空格/无空格分隔
    PATTERN_STD_CODE = re.compile(
        r"^([A-Za-z][A-Za-z\d/／]*?)\s*(\d+(?:\.\d+)*)\s*[-:：]\s*((?:19|20)\d{2})(?!\d)"
    )

    DOC_EXTENSIONS = {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".dwg", ".dxf", ".txt", ".rtf", ".csv",
        ".rar", ".zip", ".7z",
        ".jpg", ".jpeg", ".png", ".bmp",
        ".chm", ".html", ".mht",
    }

    # 系统级禁止读取的文件类型：完全跳过，不计数、不识别、不进未识别弹窗。
    # （.db 数据库文件、.bd 等非文档类型）
    EXCLUDED_EXTENSIONS = {".db", ".bd"}

    @classmethod
    def is_document(cls, filepath: str) -> bool:
        ext = os.path.splitext(filepath)[1].lower()
        return ext not in cls.EXCLUDED_EXTENSIONS and ext in cls.DOC_EXTENSIONS

    @classmethod
    def parse(cls, filepath: str):
        """返回 (标准编号, 标准名称)；无法解析返回 (None, None)。"""
        # 仅按系统主分隔符取末段文件名；不使用 os.path.basename，
        # 因其在 Windows 会同时按 \ 和 / 切分，把标准编号中的 "GB/T ..." 误当路径截断
        name = filepath.rsplit(os.sep, 1)[-1]
        basename = os.path.splitext(name)[0].strip()
        if not basename:
            return None, None

        # 策略 1：书名号格式「编号《名称》」—— 名称已天然去除《》
        match = cls.PATTERN_BOOKMARK.search(basename)
        if match:
            code = cls._normalize_code(match.group(1).strip())
            name = match.group(2).strip()
            return code, name

        # 策略 2：从开头提取标准编号（前缀+数字主体-年号），剩余部分为名称
        # 适配单空格分隔（"GBT 46605-2025 硫化橡胶..."），并自动补齐 GBT→GB/T
        m = cls.PATTERN_STD_CODE.match(basename)
        if m:
            code = cls._normalize_code(m.group(1), m.group(2), m.group(3))
            name = basename[m.end():].strip()
            # 去除名称两侧可能残留的《》，使两种格式生成统一名称
            name = name.strip('《》').strip()
            return code, name

        # 策略 3：无编号，整个文件名作为名称（至少含 2 个中文字符）
        if cls._looks_like_chinese_name(basename):
            return "", basename

        return None, None

    @classmethod
    def _normalize_code(cls, prefix: str, core: str = None, year: str = None) -> str:
        """归一化标准编号。
        两种调用方式：
          1) 传入完整编号串（如 "GBT 46605-2025"）：自动拆分前缀/主体/年号
          2) 传入已拆分的三段 prefix/core/year（来自 PATTERN_STD_CODE 分组）
        规则：
          - 全角斜杠／统一为半角/，前缀内部去除空格
          - 无斜杠且前缀末尾为 T → 补斜杠：GBT→GB/T、CJT→CJ/T、DB11T→DB11/T
          - 前缀与数字主体间保留单个空格，主体-年号间无空格
        """
        # 调用方式 1：仅传入完整编号串，先用 PATTERN_STD_CODE 拆分
        if core is None or year is None:
            raw = (prefix or "").strip().replace('／', '/')
            m = cls.PATTERN_STD_CODE.match(raw)
            if not m:
                return raw
            prefix, core, year = m.group(1), m.group(2), m.group(3)

        prefix = re.sub(r'\s+', '', prefix.replace('／', '/'))
        # 无斜杠且前缀末尾为 T → 补斜杠（GBT→GB/T、DB11T→DB11/T）
        if '/' not in prefix:
            m2 = re.match(r'^([A-Za-z]+\d*)T$', prefix)
            if m2:
                prefix = m2.group(1) + '/T'
        return f"{prefix} {core}-{year}"

    @staticmethod
    def _looks_like_chinese_name(text: str) -> bool:
        chinese_count = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return chinese_count >= 2


# --- 标准状态判定工具函数 ---

import datetime
from typing import Optional


def is_obsolete_status(status_str: str) -> bool:
    """判断标准状态是否为废止/作废"""
    if not status_str:
        return False
    return any(k in status_str for k in ("废止", "作废", "被代替", "废止代替"))


def _parse_date(date_str: str) -> Optional[datetime.date]:
    """解析标准日期字符串，支持 '%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d' 三种格式"""
    if not date_str:
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'):
        try:
            return datetime.datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _extract_base_key(std_no: str) -> Optional[str]:
    """从标准编号中提取基础编号（去掉年份）。

    例：'GB/T 1345-2005' → 'GB/T1345'，'GB 1345-1988' → 'GB1345'
        'DB11/T 1234-2020' → 'DB11/T1234'，'CECS 293:2011' → 'CECS293'
    GB 与 GB/T 保持为独立系列键，不剥离 /T。
    """
    if not std_no:
        return None
    base_match = re.match(
        r'([A-Za-z]+\d*(?:/[A-Za-z]+)?\s*\d+(?:\.\d+)?)\s*[-—:：]\s*\d{4}', std_no
    )
    if base_match:
        key = base_match.group(1).replace(' ', '').upper()
        return key
    return None


def _keyword_year(std_no: str) -> Optional[str]:
    """从标准编号中提取年份"""
    if not std_no:
        return None
    m = re.search(r'[-—:：](\d{4})$', std_no.strip())
    return m.group(1) if m else None


def normalize_status_by_date(results: list) -> list:
    """基于实施日期智能判定标准状态
    
    对于同系列标准，根据实施日期自动判定：
    - 实施日期 <= 今天：现行
    - 实施日期 > 今天：即将实施
    - 同系列中有现行版本时，其他旧版本标记为作废
    
    :param results: list of StandardInfo（需有 std_no, implement_date, status 属性）
    :return: 状态更新后的列表
    """
    if not results:
        return results
    
    today = datetime.date.today()
    
    # 1. 按基础编号分组
    groups = {}
    for std in results:
        std_no = getattr(std, 'std_no', '') or ''
        base_key = _extract_base_key(std_no)
        if base_key:
            groups.setdefault(base_key, []).append(std)
    
    # 2. 对每个系列进行状态判定
    for base_key, std_list in groups.items():
        if len(std_list) <= 1:
            std = std_list[0]
            # 源站已标明废止/作废：勿因实施日期已过而误改成现行
            if is_obsolete_status(getattr(std, 'status', '') or ''):
                continue
            impl_date_str = getattr(std, 'implement_date', '') or ''
            if impl_date_str:
                impl_date = _parse_date(impl_date_str)
                if impl_date:
                    std.status = '现行' if impl_date <= today else '即将实施'
            continue

        # 多个同系列标准，按年份降序排列
        std_list.sort(
            key=lambda s: _keyword_year(getattr(s, 'std_no', '')) or '',
            reverse=True
        )

        # 先解析所有日期（一次解析，避免循环内重复解析）
        parsed_dates = {}
        for std in std_list:
            impl_date_str = getattr(std, 'implement_date', '') or ''
            parsed_dates[id(std)] = _parse_date(impl_date_str)

        # 优先：实施日期已到的最新非废止版本 → 现行
        # 勿因「无实施日期」抢先 break，否则会压过同系列的即将实施新版
        current_std = None
        for std in std_list:
            if is_obsolete_status(getattr(std, 'status', '') or ''):
                continue
            impl_date = parsed_dates.get(id(std))
            if impl_date and impl_date <= today:
                current_std = std
                current_std.status = '现行'
                break

        # 标记其他版本的状态
        if current_std:
            for std in std_list:
                if std is current_std:
                    continue
                if is_obsolete_status(getattr(std, 'status', '') or ''):
                    # 保留源站废止/作废语义（统一成作废便于展示）
                    if '废止' in (std.status or '') and '作废' not in (std.status or ''):
                        std.status = '作废'
                    continue
                impl_date = parsed_dates.get(id(std))
                if impl_date:
                    std.status = '即将实施' if impl_date > today else '作废'
                else:
                    std.status = '作废'
        else:
            # 无「已实施」版本：最新非废止若实施日在未来 → 即将实施；否则（无日期）→ 现行
            non_obsolete = [
                s for s in std_list
                if not is_obsolete_status(getattr(s, 'status', '') or '')
            ]
            if non_obsolete:
                newest = non_obsolete[0]
                newest_date = parsed_dates.get(id(newest))
                if newest_date and newest_date > today:
                    newest.status = '即将实施'
                else:
                    newest.status = '现行'
                for std in std_list:
                    if std is newest:
                        continue
                    if is_obsolete_status(getattr(std, 'status', '') or ''):
                        continue
                    impl_date = parsed_dates.get(id(std))
                    if impl_date and impl_date > today:
                        std.status = '即将实施'
                    else:
                        std.status = '作废'

    return results
