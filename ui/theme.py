# -*- coding: utf-8 -*-
"""共享界面色板与排版令牌（仅视觉，不含业务逻辑）。"""
from PyQt5.QtGui import QColor, QFont


# —— 主色系（查询页既有三档蓝 + 中性）——
PRIMARY = "#2a6aa8"
PRIMARY_RGB = (42, 106, 168)
PRIMARY_HOVER = "#1a5a98"
PRIMARY_PRESSED = "#0d4a88"
ACCENT = "#2f88ff"
MUTED = "#6B7A89"
BG = "#eef4f9"
SOFT = "#d6ebf9"
IMPORT_BG = "#f0f7fc"
BORDER = "#a8c4d8"
TEXT = "#333333"
WHITE = "#ffffff"
ROW_ALT = "#f7fafc"
ROW_LINE = "#e8eef3"
DIALOG_BG = "#f4f9fd"

# —— 语义色（收敛到与主蓝协调的色相，去掉 Material 残留）——
SUCCESS = "#3d8b65"
SUCCESS_RGB = (61, 139, 101)
WARNING = "#c4892a"
WARNING_RGB = (196, 137, 42)
DANGER = "#c45c4e"
DANGER_RGB = (196, 92, 78)
LOCAL = "#2a8f85"
LOCAL_RGB = (42, 143, 133)
PENDING = "#8a9aaa"
IDLE = "#c8c8c8"
DETECT_RGB = (42, 106, 168)

# 替代行浅底
REPLACEMENT_BG = "#fff8e8"
REPLACEMENT_BG_RGB = (255, 248, 232)


def qcolor(hex_color, alpha=255):
    """#RRGGBB → QColor。"""
    c = QColor(hex_color)
    if alpha < 255:
        c.setAlpha(alpha)
    return c


def primary_qcolor():
    return QColor(*PRIMARY_RGB)


def accent_qcolor():
    return qcolor(ACCENT)


def success_qcolor():
    return QColor(*SUCCESS_RGB)


def danger_qcolor():
    return QColor(*DANGER_RGB)


def warning_qcolor():
    return QColor(*WARNING_RGB)


def local_qcolor():
    return QColor(*LOCAL_RGB)


def std_no_font(point_size=11):
    """标准编号强调字体：略大、加粗，作为结果表视觉锚点。"""
    font = QFont()
    font.setFamilies(["Microsoft YaHei", "Segoe UI Symbol"])
    font.setPointSize(point_size)
    font.setBold(True)
    return font


def apply_std_no_item(item, emphasize_color=True):
    """统一设置标准编号单元格的字号/字重/主色（状态色由调用方覆盖）。"""
    item.setFont(std_no_font(11))
    if emphasize_color:
        item.setForeground(qcolor(PRIMARY))
