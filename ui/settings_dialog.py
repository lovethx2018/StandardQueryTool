# -*- coding: utf-8 -*-
"""设置对话框。"""
import os
import tempfile
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from .updater import APP_VERSION
from . import theme as _theme


def _checkbox_check_icon_path():
    """生成勾选图标路径（stylesheet image:url 需要文件；写临时目录避免打包只读）。"""
    path = os.path.join(tempfile.gettempdir(), "standard_query_cb_check.png")
    if not os.path.isfile(path):
        pm = QPixmap(18, 18)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#ffffff"))
        pen.setWidth(2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(4, 9, 8, 13)
        painter.drawLine(8, 13, 14, 5)
        painter.end()
        pm.save(path, "PNG")
    return path.replace("\\", "/")


class SettingsDialog(QDialog):
    """设置对话框 - 用于修改下载保存目录"""

    def __init__(self, current_dir, lan_share_dir="", ledger_enabled=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setFixedSize(540, 540)
        self.setStyleSheet(f"QDialog {{ background: {_theme.DIALOG_BG}; }}")
        self.init_ui(current_dir, lan_share_dir, ledger_enabled)

    def init_ui(self, current_dir, lan_share_dir="", ledger_enabled=False):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        # 与主窗口查询行对齐：控件统一固定行高。
        # 勿在 stylesheet 再写 min-height（Qt 会把 padding 叠加上去，行高被撑到 ~48）。
        # 原先 padding:9px 且无固定高度时，Win 下底边框/占位字易被裁 1px。
        _row_h = 36
        _input = (
            f"QLineEdit {{ padding: 6px 12px; font-size: 13px; "
            f"border: 1px solid {_theme.BORDER}; border-radius: 8px; "
            f"background: white; color: {_theme.TEXT}; }}"
            f"QLineEdit::placeholder {{ color: {_theme.MUTED}; }}"
        )
        _outline = (
            f"QPushButton {{ padding: 6px 18px; background: #ffffff; color: {_theme.PRIMARY}; "
            f"border: 1px solid {_theme.BORDER}; border-radius: 8px; "
            f"font-size: 13px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {_theme.IMPORT_BG}; border-color: {_theme.PRIMARY}; }}"
            f"QPushButton:pressed {{ background: {_theme.SOFT}; }}"
        )
        _danger_outline = (
            f"QPushButton {{ padding: 6px 18px; background: #ffffff; color: {_theme.DANGER}; "
            f"border: 1px solid #e8b5ba; border-radius: 8px; "
            f"font-size: 13px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: #fef1f2; border-color: {_theme.DANGER}; }}"
            f"QPushButton:pressed {{ background: #fce5e7; }}"
        )

        title = QLabel("设置")
        title.setStyleSheet(
            f"font-size: 18px; font-weight: bold; color: {_theme.PRIMARY};"
        )
        layout.addWidget(title)

        section_label = QLabel("下载保存目录")
        section_label.setStyleSheet(f"font-size: 13px; color: {_theme.MUTED};")
        layout.addWidget(section_label)

        dir_row = QHBoxLayout()
        dir_row.setSpacing(10)

        self.dir_input = QLineEdit(current_dir)
        self.dir_input.setReadOnly(True)
        self.dir_input.setFixedHeight(_row_h)
        self.dir_input.setStyleSheet(_input)
        dir_row.addWidget(self.dir_input, stretch=1)

        browse_btn = QPushButton("浏览...")
        browse_btn.setCursor(Qt.PointingHandCursor)
        browse_btn.setFixedHeight(_row_h)
        browse_btn.setStyleSheet(_outline)
        browse_btn.clicked.connect(self.browse_dir)
        dir_row.addWidget(browse_btn)

        clear_btn = QPushButton("清除")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setFixedHeight(_row_h)
        clear_btn.setStyleSheet(_danger_outline)
        clear_btn.clicked.connect(self.clear_dir)
        dir_row.addWidget(clear_btn)

        layout.addLayout(dir_row)

        hint = QLabel("PDF 文件将保存到此目录，下载完成后会自动打开该文件夹。")
        hint.setStyleSheet(f"font-size: 12px; color: {_theme.MUTED};")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        lan_label = QLabel("标准共享目录")
        lan_label.setStyleSheet(f"font-size: 13px; color: {_theme.MUTED};")
        layout.addWidget(lan_label)

        lan_row = QHBoxLayout()
        lan_row.setSpacing(10)

        self.lan_input = QLineEdit(lan_share_dir or "")
        self.lan_input.setReadOnly(True)
        self.lan_input.setPlaceholderText("未配置（不使用局域网，直接走线上渠道）")
        self.lan_input.setFixedHeight(_row_h)
        self.lan_input.setStyleSheet(_input)
        lan_row.addWidget(self.lan_input, stretch=1)

        lan_browse_btn = QPushButton("浏览...")
        lan_browse_btn.setCursor(Qt.PointingHandCursor)
        lan_browse_btn.setFixedHeight(_row_h)
        lan_browse_btn.setStyleSheet(_outline)
        lan_browse_btn.clicked.connect(self.browse_lan_dir)
        lan_row.addWidget(lan_browse_btn)

        lan_clear_btn = QPushButton("清除")
        lan_clear_btn.setCursor(Qt.PointingHandCursor)
        lan_clear_btn.setFixedHeight(_row_h)
        lan_clear_btn.setStyleSheet(_danger_outline)
        lan_clear_btn.clicked.connect(self.clear_lan_dir)
        lan_row.addWidget(lan_clear_btn)

        layout.addLayout(lan_row)

        lan_hint = QLabel("标准文档管理与目录设置：标准文档管理页据此目录扫描并解析。留空则不启用局域网。")
        lan_hint.setStyleSheet(f"font-size: 12px; color: {_theme.MUTED};")
        lan_hint.setWordWrap(True)
        layout.addWidget(lan_hint)

        feature_label = QLabel("功能开关")
        feature_label.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {_theme.TEXT};"
        )
        layout.addWidget(feature_label)

        # 与上方输入框同款四边描边；用浅底区分区块。
        # 注意：一旦写了 QCheckBox::indicator，就必须自绘边框/勾选态，
        # 否则父级 stylesheet 接管后原生指示器会被掏空（只剩文字）。
        feature_box = QWidget()
        feature_box.setObjectName("FeatureSwitchBox")
        feature_box.setAttribute(Qt.WA_StyledBackground, True)
        feature_box.setStyleSheet(
            f"QWidget#FeatureSwitchBox {{"
            f" background: {_theme.IMPORT_BG};"
            f" border: 1px solid {_theme.BORDER};"
            f" border-radius: 8px; }}"
        )
        feature_body_layout = QVBoxLayout(feature_box)
        feature_body_layout.setContentsMargins(14, 12, 14, 12)
        feature_body_layout.setSpacing(6)

        self.ledger_checkbox = QCheckBox("启用标准台账")
        self.ledger_checkbox.setChecked(bool(ledger_enabled))
        self.ledger_checkbox.setCursor(Qt.PointingHandCursor)
        _cb_font = QFont(self.ledger_checkbox.font())
        _cb_font.setPointSize(10)
        _cb_font.setBold(True)
        self.ledger_checkbox.setFont(_cb_font)
        _check_icon = _checkbox_check_icon_path()
        self.ledger_checkbox.setStyleSheet(
            f"QCheckBox {{ color: {_theme.TEXT}; spacing: 10px; background: transparent; }}"
            f"QCheckBox::indicator {{"
            f" width: 18px; height: 18px;"
            f" border: 1px solid {_theme.BORDER};"
            f" border-radius: 4px;"
            f" background: {_theme.WHITE}; }}"
            f"QCheckBox::indicator:hover {{ border-color: {_theme.PRIMARY}; }}"
            f"QCheckBox::indicator:checked {{"
            f" border-color: {_theme.PRIMARY};"
            f" background: {_theme.PRIMARY};"
            f" image: url('{_check_icon}'); }}"
        )
        feature_body_layout.addWidget(self.ledger_checkbox)

        ledger_hint = QLabel(
            "勾选后，顶部导航会出现「标准台账」入口。\n"
            "默认关闭；关闭后不影响已保存的台账数据。"
        )
        ledger_hint.setWordWrap(True)
        _hint_font = QFont("Microsoft YaHei", 9)
        ledger_hint.setFont(_hint_font)
        ledger_hint.setStyleSheet(f"color: {_theme.MUTED}; background: transparent;")
        ledger_hint.setMinimumHeight(QFontMetrics(_hint_font).lineSpacing() * 2 + 2)
        feature_body_layout.addWidget(ledger_hint)
        layout.addWidget(feature_box)

        update_label = QLabel("软件更新")
        update_label.setStyleSheet(f"font-size: 13px; color: {_theme.MUTED};")
        layout.addWidget(update_label)

        update_row = QHBoxLayout()
        update_row.setSpacing(10)

        self.version_label = QLabel(f"当前版本：v{APP_VERSION}")
        self.version_label.setStyleSheet(f"font-size: 13px; color: {_theme.TEXT};")
        update_row.addWidget(self.version_label)
        update_row.addSpacing(18)
        author_label = QLabel("作者：黄加林")
        author_label.setStyleSheet(f"font-size: 13px; color: {_theme.MUTED};")
        update_row.addWidget(author_label)
        update_row.addStretch()

        layout.addLayout(update_row)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        check_update_btn = QPushButton("检查更新")
        check_update_btn.setCursor(Qt.PointingHandCursor)
        check_update_btn.setStyleSheet(_outline)
        check_update_btn.clicked.connect(self._on_check_update)
        btn_row.addWidget(check_update_btn)
        btn_row.addSpacing(16)

        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(
            f"QPushButton {{ padding: 9px 24px; background: #ffffff; color: {_theme.MUTED}; "
            f"border: 1px solid #c0c8d0; border-radius: 8px; font-size: 13px; }}"
            f"QPushButton:hover {{ background: #f0f0f0; }}"
        )
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        ok_btn = QPushButton("确定")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setStyleSheet(
            f"QPushButton {{ padding: 9px 28px; background: {_theme.PRIMARY}; color: white; "
            f"border: none; border-radius: 8px; font-size: 13px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {_theme.PRIMARY_HOVER}; }}"
            f"QPushButton:pressed {{ background: {_theme.PRIMARY_PRESSED}; }}"
        )
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)

        layout.addLayout(btn_row)

    def browse_dir(self):
        """选择新的下载目录"""
        directory = QFileDialog.getExistingDirectory(
            self, "选择下载保存目录", self.dir_input.text()
        )
        if directory:
            self.dir_input.setText(directory)

    def clear_dir(self):
        """清除下载保存目录"""
        self.dir_input.setText("")

    def clear_lan_dir(self):
        """清除局域网标准共享目录"""
        self.lan_input.setText("")

    def browse_lan_dir(self):
        """选择局域网标准共享目录"""
        directory = QFileDialog.getExistingDirectory(
            self, "选择局域网标准共享目录", self.lan_input.text()
        )
        if directory:
            self.lan_input.setText(directory)

    def get_download_dir(self):
        """返回用户选择的下载目录"""
        return self.dir_input.text().strip()

    def get_lan_share_dir(self):
        """返回用户配置的局域网标准共享目录（可为空表示不启用）"""
        return self.lan_input.text().strip()

    def get_ledger_enabled(self):
        """是否启用标准台账导航。"""
        return bool(self.ledger_checkbox.isChecked())

    def _on_check_update(self):
        """手动检查更新：委托给主窗口执行，弹窗以本设置对话框为父窗口。"""
        mw = self.parent()
        if mw is not None and hasattr(mw, "check_for_updates"):
            mw.check_for_updates(silent=False, parent_widget=self)
        else:
            QMessageBox.information(self, "检查更新", "无法启动更新检查。")
