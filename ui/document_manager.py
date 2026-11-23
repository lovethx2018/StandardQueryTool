# -*- coding: utf-8 -*-
"""本地文档（嵌入页）。"""
import os
import json
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from app_env import logger
from config import Config
from utils import _FileNameParser, atomic_write_json
from .widgets import GlassButton, ask_yes_no
from . import theme as _theme


class DocumentManagerPage(QWidget):
    """本地文档页面 —— 嵌入主窗口堆栈，作为本地标准库后台。

    扫描所选目录（本地或局域网共享）解析标准文档，作为标准查询系统的
    本地下载源：扫描完成即同步到 Config.LAN_SHARE_DIR，使下载流程优先
    本地分发并显示"本地下载"。
    """

    COLUMNS = ("序号", "标准编号", "标准名称", "附件")

    def __init__(self, on_directory_changed=None, parent=None):
        super().__init__(parent)
        self.on_directory_changed = on_directory_changed  # 回调：目录变更时同步下载源
        self.current_dir = ""
        self.all_records = []          # [{"code","name","path"}, ...]
        self.filtered_records = []
        self.failed_files = []         # [(filepath, reason), ...]
        self._pending_refresh = False  # 静默扫描时若页面不可见，延迟到 showEvent 再建表
        self._scan_worker = None
        # 搜索防抖：输入停止 250ms 后再执行过滤，避免逐字重建表格造成卡顿
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(250)
        self._search_debounce.timeout.connect(self.do_search)
        self._init_ui()
        self._apply_style()
        # 启动时自动加载上次目录
        QTimer.singleShot(100, self._auto_load_on_startup)

    def showEvent(self, event):
        """文档管理页首次可见时，若有待建表任务则补建，避免启动期建表拖慢主界面。"""
        super().showEvent(event)
        if getattr(self, "_pending_refresh", False):
            self._pending_refresh = False
            self._refresh_table()

    # --------------------------------------------------------
    # 样式：圆角液态玻璃，浅蓝主题，与主应用一致
    # --------------------------------------------------------
    def _apply_style(self):
        self.setStyleSheet(f"""
            QWidget#DocPageRoot {{ background: transparent; }}
            QFrame#GlassCard {{
                background: rgba(255, 255, 255, 200);
                border: 1px solid rgba(176, 200, 216, 180);
                border-radius: 12px;
            }}
            QFrame#HeaderCard {{
                background: {_theme.PRIMARY};
                border: none;
                border-radius: 12px;
            }}
            QLabel#HeaderTitle {{ color: #ffffff; font-size: 18px; font-weight: bold; }}
            QLabel#HeaderSub {{ color: rgba(255,255,255,220); font-size: 12px; }}
            QLabel#HeaderCount {{ color: #ffffff; font-size: 14px; font-weight: bold; }}
            QLabel#SectionLabel {{ color: {_theme.PRIMARY}; font-size: 13px; font-weight: bold; }}
            QLabel#StatusLabel {{ color: {_theme.MUTED}; font-size: 12px; }}
            QLabel#HintLabel {{ color: {_theme.PENDING}; font-size: 12px; }}

            QLineEdit {{
                padding: 8px 12px;
                font-size: 13px;
                border: 1px solid {_theme.BORDER};
                border-radius: 8px;
                background: rgba(255,255,255,240);
                color: {_theme.TEXT};
            }}
            QLineEdit:focus {{ border: 1px solid {_theme.PRIMARY}; }}

            QPushButton {{
                border-radius: 8px;
                font-size: 13px;
                font-weight: bold;
                padding: 7px 16px;
            }}
            QPushButton#PrimaryBtn {{ background: {_theme.PRIMARY}; color: white; border: none; }}
            QPushButton#PrimaryBtn:hover {{ background: {_theme.PRIMARY_HOVER}; }}
            QPushButton#PrimaryBtn:pressed {{ background: {_theme.PRIMARY_PRESSED}; }}
            QPushButton#OutlineBtn {{ background: rgba(255,255,255,220); color: {_theme.PRIMARY};
                border: 1px solid {_theme.BORDER}; }}
            QPushButton#OutlineBtn:hover {{ background: {_theme.IMPORT_BG}; border-color: {_theme.PRIMARY}; }}
            QPushButton#SuccessBtn {{ background: {_theme.SUCCESS}; color: white; border: none; }}
            QPushButton#SuccessBtn:hover {{ background: #347C4E; }}
            QPushButton#WarnBtn {{ background: {_theme.WARNING}; color: white; border: none; }}
            QPushButton#WarnBtn:hover {{ background: #C77A2E; }}

            QTableWidget {{
                background: #ffffff;
                border: 1px solid {_theme.BORDER};
                border-radius: 12px;
                gridline-color: #EEF2F7;
                font-size: 13px;
                color: {_theme.TEXT};
                selection-background-color: {_theme.SOFT};
                selection-color: {_theme.TEXT};
            }}
            QTableWidget::item {{
                padding: 8px 10px;
                border-bottom: 1px solid {_theme.ROW_LINE};
            }}
            QTableWidget::item:alternate {{
                background: {_theme.ROW_ALT};
            }}
            QHeaderView::section {{
                background: {_theme.PRIMARY};
                color: white;
                padding: 12px;
                border: none;
                border-right: 1px solid rgba(255,255,255,100);
                font-weight: bold;
                font-size: 13px;
            }}
            QHeaderView::section:first {{
                border-top-left-radius: 12px;
            }}
            QHeaderView::section:last {{
                border-top-right-radius: 12px;
                border-right: none;
            }}
            QTableCornerButton::section {{
                background: {_theme.PRIMARY};
                border: none;
                border-top-left-radius: 12px;
            }}
        """)

    # --------------------------------------------------------
    # 界面构建
    # --------------------------------------------------------
    def _card_shadow(self, widget):
        """卡片细微投影，区分模块边界、增强层次感（与主查询页一致）。"""
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(14)
        eff.setXOffset(0)
        eff.setYOffset(2)
        eff.setColor(QColor(20, 50, 80, 26))
        widget.setGraphicsEffect(eff)

    def _init_ui(self):
        self.setObjectName("DocPageRoot")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        # 顶部标题卡片
        header_card = self._build_header()
        self._card_shadow(header_card)
        root.addWidget(header_card)

        # 工具与搜索合并卡片（搜索栏 + 重新加载/定位文件）
        toolbar_card = self._build_toolbar()
        self._card_shadow(toolbar_card)
        root.addWidget(toolbar_card)

        # 表格卡片
        table_card = self._build_table()
        self._card_shadow(table_card)
        root.addWidget(table_card, stretch=1)

        # 底部状态栏
        root.addWidget(self._build_status_bar())

    def _build_header(self):
        card = QWidget()
        card.setStyleSheet("""
            QWidget {
                background: #ffffff;
                border-radius: 12px;
            }
        """)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.setSpacing(10)

        left_box = QVBoxLayout()
        left_box.setSpacing(0)
        sub = QLabel("请确保文件为「标准编号《标准名称》.pdf」命名格式；扫描后自动作为本地下载源")
        sub.setObjectName("HeaderSub")
        sub.setStyleSheet(f"""
            font-size: 13px;
            color: {_theme.MUTED};
        """)
        left_box.addWidget(sub)
        lay.addLayout(left_box)
        lay.addStretch()

        self.header_count_var = QLabel("共 0 条本地文档")
        self.header_count_var.setStyleSheet(f"""
            font-size: 14px;
            font-weight: bold;
            color: {_theme.PRIMARY};
        """)
        lay.addWidget(self.header_count_var)
        return card

    def _build_toolbar(self):
        """合并工具栏与搜索栏：搜索标签+输入框+搜索+清除+重新加载+定位文件"""
        card = QWidget()
        card.setStyleSheet("""
            QWidget {
                background: #ffffff;
                border-radius: 12px;
            }
        """)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.setSpacing(10)

        # 文档目录由「设置」统一管理，不在界面显示；dir_input 作为隐藏的内部状态载体
        self.dir_input = QLineEdit()
        self.dir_input.setVisible(False)

        # 搜索区
        lbl = QLabel("本地检索")
        lbl.setStyleSheet(f"""
            font-size: 16px;
            font-weight: bold;
            color: {_theme.PRIMARY};
        """)
        lay.addWidget(lbl)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("输入本地编号或名称关键词，回车搜索")
        self.search_input.textChanged.connect(self._on_search_typing)
        self.search_input.returnPressed.connect(self.do_search)
        lay.addWidget(self.search_input, stretch=1)

        search_btn = GlassButton("搜索", _theme.primary_qcolor())
        search_btn.setMinimumSize(76, 34)
        search_btn.clicked.connect(self.do_search)
        lay.addWidget(search_btn)

        clear_btn = GlassButton("清除", _theme.danger_qcolor())
        clear_btn.setMinimumSize(76, 34)
        clear_btn.clicked.connect(self.clear_search)
        lay.addWidget(clear_btn)

        lay.addSpacing(6)

        # 文件操作区：重新加载、定位文件（删除"打开文件"按钮，打开操作由附件列按钮承担）
        refresh_btn = GlassButton("重新加载", _theme.success_qcolor())
        refresh_btn.setMinimumSize(92, 34)
        # 大目录扫盘必须后台，避免点「重新加载」卡死主界面
        refresh_btn.clicked.connect(lambda: self.scan_directory(background=True))
        lay.addWidget(refresh_btn)

        loc_btn = GlassButton("定位文件", _theme.primary_qcolor())
        loc_btn.setMinimumSize(92, 34)
        loc_btn.clicked.connect(self.open_location)
        lay.addWidget(loc_btn)

        return card

    def _build_table(self):
        card = QWidget()
        card.setStyleSheet("""
            QWidget {
                background: #ffffff;
                border-radius: 12px;
            }
        """)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 12, 18, 12)

        self.tree = QTableWidget(0, len(self.COLUMNS))
        self.tree.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.tree.setSelectionBehavior(QTableWidget.SelectRows)
        self.tree.setSelectionMode(QTableWidget.SingleSelection)
        self.tree.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tree.setAlternatingRowColors(True)
        self.tree.verticalHeader().setVisible(False)
        self.tree.setShowGrid(False)
        self.tree.setWordWrap(True)

        header = self.tree.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        self.tree.setColumnWidth(0, 60)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        self.tree.setColumnWidth(1, 240)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tree.setColumnWidth(3, 130)

        self.tree.doubleClicked.connect(self._on_double_click)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_right_click)

        lay.addWidget(self.tree)
        return card

    def _build_status_bar(self):
        card = QWidget()
        card.setStyleSheet("""
            QWidget {
                background: #ffffff;
                border-radius: 12px;
            }
        """)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.setSpacing(10)
        self.status_var = QLabel("就绪 — 请在「设置」中配置标准共享目录")
        self.status_var.setStyleSheet(f"""
            font-size: 13px;
            color: {_theme.MUTED};
        """)
        lay.addWidget(self.status_var)
        lay.addStretch()
        return card

    # --------------------------------------------------------
    # 自动加载与配置
    # --------------------------------------------------------
    def _auto_load_on_startup(self):
        """启动时加载标准共享目录（单一数据源 Config.LAN_SHARE_DIR）。

        为加快启动、避免每次开软件都全盘扫描：优先读取上次扫描的缓存直接呈现，
        完全不重新扫描目录；仅当无可用缓存时才做一次静默扫描。之后只有用户点
        「重新加载」或在设置里更换目录，才会真正重新扫描并刷新缓存。
        """
        last_dir = getattr(Config, "LAN_SHARE_DIR", "") or ""
        if not last_dir:
            return
        self.dir_input.setText(last_dir)

        cache = self._load_cache()
        if cache and cache.get("dir") == last_dir and isinstance(cache.get("records"), list):
            # 命中缓存：直接使用，不触碰磁盘/网络共享，实现“启动不重新扫描”
            self.current_dir = last_dir
            self.all_records = cache["records"]
            self.filtered_records = list(self.all_records)
            self.failed_files = []  # 缓存仅保存计数，不保留失败明细
            parsed = len(self.all_records)
            failed = int(cache.get("failed_count", 0))
            self.header_count_var.setText(f"共 {parsed} 条标准")
            if failed > 0:
                self.status_var.setText(
                    f"已从缓存加载 {parsed} 条（另有 {failed} 个未识别，点「重新加载」可刷新）"
                )
            else:
                self.status_var.setText(f"已从缓存加载 {parsed} 条标准文档（点「重新加载」可刷新）")
            # 页面可见才立即建表，否则延迟到 showEvent，避免启动期建表拖慢主界面
            if self.isVisible():
                self._refresh_table()
            else:
                self._pending_refresh = True
            # 同步为本地下载源（缓存路径仍指向共享目录，供 _find_in_lan 精确匹配）
            if self.on_directory_changed:
                self.on_directory_changed(last_dir)
            return

        # 无可用缓存：后台静默扫描（绝不在启动主线程上扫大目录）
        if os.path.isdir(last_dir):
            self.scan_directory(silent=True, background=True)

    # --------------------------------------------------------
    # 扫描结果缓存（启动加速：后续启动直接读缓存，不重新扫描）
    # --------------------------------------------------------
    @staticmethod
    def _cache_path():
        return os.path.join(os.path.dirname(Config.SETTINGS_FILE), "doc_scan_cache.json")

    def _load_cache(self):
        """读取上次扫描缓存；不存在或损坏返回 None。"""
        try:
            path = self._cache_path()
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("读取文档扫描缓存失败: %s", e)
            return None

    def _save_cache(self, total_files=0):
        """把当前扫描结果写入缓存文件，供下次启动直接呈现。"""
        try:
            os.makedirs(os.path.dirname(self._cache_path()), exist_ok=True)
            data = {
                "version": 1,
                "dir": self.current_dir,
                "total_files": int(total_files),
                "failed_count": len(self.failed_files),
                "records": self.all_records,
            }
            atomic_write_json(self._cache_path(), data, indent=None)
        except Exception as e:
            logger.warning("写入文档扫描缓存失败: %s", e)

    # --------------------------------------------------------
    # 扫描与解析
    # --------------------------------------------------------
    def scan_directory(self, silent: bool = False, background: bool = False):
        """扫描文件夹，解析文件名，记录失败项；同步为本地下载源。

        silent=True 时不弹错误对话框（用于启动自动加载，避免启动期弹窗干扰）。
        background=True 时在后台线程扫描，避免大目录阻塞设置对话框关闭后的 UI。
        """
        target = self.dir_input.text().strip()
        if not target:
            if not silent:
                QMessageBox.warning(self, "提示", "未配置文档目录，请在「设置」中指定标准共享目录。")
            return
        if not os.path.isdir(target):
            if not silent:
                QMessageBox.critical(self, "错误", f"目录不存在或无法访问：\n{target}")
            return

        if background:
            if self._scan_worker is not None and self._scan_worker.isRunning():
                return
            self.status_var.setText("正在扫描目录…")
            QApplication.setOverrideCursor(Qt.WaitCursor)
            from .workers import DirectoryScanWorker
            self._scan_worker = DirectoryScanWorker(target)
            self._scan_worker.finished.connect(
                lambda r, s=silent: self._apply_scan_result(r, silent=s)
            )
            self._scan_worker.start()
            return

        self.current_dir = target
        self.all_records.clear()
        self.failed_files.clear()

        total_files = 0
        try:
            for entry in os.scandir(target):
                if not entry.is_file():
                    continue
                fname = entry.name
                ext = os.path.splitext(fname)[1].lower()
                if ext in _FileNameParser.EXCLUDED_EXTENSIONS:
                    # 系统级禁止的类型（.db/.bd）：完全不读取、不计数、不报告、不进未识别弹窗
                    continue
                total_files += 1
                if ext not in _FileNameParser.DOC_EXTENSIONS:
                    self.failed_files.append((entry.path, f"不支持的文件类型：{ext}"))
                    continue
                code, name = _FileNameParser.parse(entry.path)
                if code is None and name is None:
                    self.failed_files.append((entry.path, "文件名不符合解析规则（缺少《》或可识别格式）"))
                    continue
                self.all_records.append({
                    "code": code or "(无编号)",
                    "name": name,
                    "path": entry.path,
                })
        except PermissionError:
            QMessageBox.critical(self, "错误", f"没有权限访问：\n{target}")
            return
        except OSError as e:
            QMessageBox.critical(self, "错误", f"扫描出错：\n{e}")
            return

        self._finalize_scan(total_files, target, silent)

    def _apply_scan_result(self, result, silent=False):
        """后台扫描完成：在主线程应用结果并刷新 UI。"""
        QApplication.restoreOverrideCursor()
        self._scan_worker = None
        err = result.get('error')
        if err:
            kind, detail = err
            if not silent:
                if kind == 'permission':
                    QMessageBox.critical(self, "错误", f"没有权限访问：\n{detail}")
                else:
                    QMessageBox.critical(self, "错误", f"扫描出错：\n{detail}")
            else:
                self.status_var.setText("目录扫描失败")
            return

        self.current_dir = result['target']
        self.all_records = result['records']
        self.failed_files = result['failed_files']
        self._finalize_scan(result['total_files'], result['target'], silent)

    def _finalize_scan(self, total_files, target, silent=False):
        """扫描结果落盘、刷新表格与状态栏。"""
        self.all_records.sort(key=lambda r: r["code"])
        self.filtered_records = list(self.all_records)
        self._save_cache(total_files)
        if self.isVisible():
            self._refresh_table()
        else:
            self._pending_refresh = True

        parsed = len(self.all_records)
        failed = len(self.failed_files)
        self.header_count_var.setText(f"共 {parsed} 条标准")
        if failed > 0:
            self.status_var.setText(
                f"总文件 {total_files} 个，已识别 {parsed} 条，未识别 {failed} 个"
            )
            if not silent:
                self._show_failed_files()
        else:
            self.status_var.setText(f"扫描完成，共 {parsed} 条标准文档")

        if self.on_directory_changed:
            self.on_directory_changed(target)

    # --------------------------------------------------------
    # 未识别文件弹窗
    # --------------------------------------------------------
    def _show_failed_files(self):
        if not self.failed_files:
            QMessageBox.information(self, "提示", "没有未识别的文件。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("未识别文件列表")
        dialog.resize(760, 460)
        lay = QVBoxLayout(dialog)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        title = QLabel(f"未识别文件（共 {len(self.failed_files)} 个）")
        title.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {_theme.PRIMARY};")
        lay.addWidget(title)

        tip = QLabel("提示：请确保文件命名为「标准编号《标准名称》.扩展名」格式。\n"
                     "例如：CJ/T 120-2016《给水涂塑复合钢管》.pdf\n"
                     "双击文件名可在资源管理器中定位该文件，或选中后点「打开所在位置」。")
        tip.setStyleSheet(f"font-size: 12px; color: {_theme.PENDING};")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        table = QTableWidget(len(self.failed_files), 2)
        table.setHorizontalHeaderLabels(["文件名", "原因"])
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        table.setColumnWidth(0, 360)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        paths = [p for p, _ in self.failed_files]
        for i, (path, reason) in enumerate(self.failed_files):
            f_item = QTableWidgetItem(os.path.basename(path))
            r_item = QTableWidgetItem(reason)
            f_item.setToolTip("双击可在资源管理器中定位：" + path)
            table.setItem(i, 0, f_item)
            table.setItem(i, 1, r_item)
        lay.addWidget(table, stretch=1)

        def _locate(row):
            if not (0 <= row < len(paths)):
                return
            self._reveal_in_explorer(paths[row], dialog)

        table.cellDoubleClicked.connect(lambda row, _col: _locate(row))

        locate_btn = QPushButton("打开所在位置")
        locate_btn.setObjectName("OutlineBtn")
        locate_btn.setCursor(Qt.PointingHandCursor)
        locate_btn.clicked.connect(
            lambda: _locate(table.currentRow()) if table.currentRow() >= 0
            else QMessageBox.information(dialog, "提示", "请先在列表中选中一个文件。")
        )
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("OutlineBtn")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(dialog.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(locate_btn)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)
        dialog.exec_()

    @staticmethod
    def _reveal_in_explorer(path, parent=None):
        """在资源管理器中定位并选中文件；文件不存在则打开其所在目录。"""
        try:
            import subprocess
            norm = os.path.normpath(path)
            if os.name == "nt":
                if os.path.exists(norm):
                    subprocess.Popen(["explorer", "/select,", norm])
                else:
                    parent_dir = os.path.dirname(norm)
                    if os.path.isdir(parent_dir):
                        subprocess.Popen(["explorer", parent_dir])
                    else:
                        QMessageBox.information(parent, "提示", "文件及其所在目录都已不存在。")
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(norm)])
        except OSError as e:
            QMessageBox.critical(parent, "错误", f"无法打开文件位置：{e}")

    # --------------------------------------------------------
    # 搜索
    # --------------------------------------------------------
    def do_search(self):
        keyword = self.search_input.text().strip()
        if not keyword:
            self.filtered_records = list(self.all_records)
        else:
            kw = keyword.lower()
            self.filtered_records = [
                r for r in self.all_records
                if kw in r["code"].lower() or kw in r["name"].lower()
            ]
        self._refresh_table()
        self.status_var.setText(f"搜索结果：{len(self.filtered_records)} 条")

    def _on_search_typing(self):
        # 防抖：每次输入重置计时器，用户停止输入 250ms 后才真正过滤，避免逐字重建表格卡顿
        self._search_debounce.start()

    def clear_search(self):
        self._search_debounce.stop()
        self.search_input.clear()
        self.filtered_records = list(self.all_records)
        self._refresh_table()
        self.status_var.setText(f"共 {len(self.all_records)} 条标准文档")

    def _refresh_table(self):
        # 批量填充期间关闭刷新与排序，填充完再统一重绘，显著降低大目录建表卡顿
        self.tree.setUpdatesEnabled(False)
        self.tree.setSortingEnabled(False)
        try:
            self.tree.setRowCount(0)
            self.tree.setRowCount(len(self.filtered_records))
            for row, rec in enumerate(self.filtered_records):
                # 序号
                seq = QTableWidgetItem(str(row + 1))
                seq.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                seq.setToolTip(rec["path"])
                self.tree.setItem(row, 0, seq)
                # 标准编号：主色加粗强调
                code_item = QTableWidgetItem(rec["code"])
                code_item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                code_item.setToolTip(rec["path"])
                _theme.apply_std_no_item(code_item, emphasize_color=True)
                self.tree.setItem(row, 1, code_item)
                # 标准名称
                name_item = QTableWidgetItem(rec["name"])
                name_item.setToolTip(rec["path"])
                self.tree.setItem(row, 2, name_item)
                # 附件列：改为轻量文本提示，双击行或右键菜单即可打开。
                # 不再逐行创建 QPushButton 控件，首次切到本页建表不再卡顿。
                hint = QTableWidgetItem("双击打开")
                hint.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                hint.setForeground(_theme.qcolor(_theme.MUTED))
                hint.setToolTip(rec["path"])
                self.tree.setItem(row, 3, hint)
                self.tree.setRowHeight(row, 40)
        finally:
            self.tree.setUpdatesEnabled(True)

    # --------------------------------------------------------
    # 文件操作
    # --------------------------------------------------------
    def _get_selected_record(self):
        rows = self.tree.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "请先在表格中选择一条记录。")
            return None
        idx = rows[0].row()
        if 0 <= idx < len(self.filtered_records):
            return self.filtered_records[idx]
        return None

    def open_selected(self):
        rec = self._get_selected_record()
        if rec:
            self._open_file(rec["path"])

    def open_location(self):
        rec = self._get_selected_record()
        if not rec:
            return
        try:
            import subprocess
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(rec["path"])])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(rec["path"])])
        except OSError as e:
            QMessageBox.critical(self, "错误", f"无法打开文件位置：{e}")

    @staticmethod
    def _open_file(filepath: str):
        try:
            if os.name == "nt":
                # 通过 cmd.exe /c start 间接打开，调用方变为已签名的 cmd.exe，
                # 避免 Adobe Reader 受保护视图因调用方 exe 未签名而拦截 PDF
                import subprocess
                subprocess.Popen(
                    ["cmd.exe", "/c", "start", "", filepath],
                    shell=False,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW 隐藏 cmd 黑窗
                    close_fds=True,
                )
            else:
                import subprocess
                subprocess.Popen(["xdg-open", filepath])
        except OSError as e:
            QMessageBox.critical(None, "错误", f"无法打开文件：\n{e}")

    def delete_selected(self):
        rec = self._get_selected_record()
        if not rec:
            return
        code = rec["code"]
        name = rec["name"]
        if ask_yes_no(
            self, "确认删除",
            f"确定要删除以下标准文档信息吗？\n\n"
            f"标准编号: {code}\n"
            f"标准名称: {name}",
        ):
            self.all_records = [r for r in self.all_records if r["path"] != rec["path"]]
            self.filtered_records = [r for r in self.filtered_records if r["path"] != rec["path"]]
            self._refresh_table()
            self.header_count_var.setText(f"共 {len(self.all_records)} 条标准")
            self.status_var.setText(f"已删除 {code}，当前共 {len(self.all_records)} 条标准文档")

    # --------------------------------------------------------
    # 交互事件
    # --------------------------------------------------------
    def _on_double_click(self):
        rec = self._get_selected_record()
        if not rec:
            return
        # 双击整行即打开文件（附件列不再放按钮，改双击/右键菜单打开）
        self._open_file(rec["path"])

    def _on_right_click(self, pos):
        index = self.tree.indexAt(pos)
        if not index.isValid():
            return
        self.tree.selectRow(index.row())
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { padding: 6px; } QMenu::item { padding: 6px 24px; }")
        act_open = menu.addAction("打开文件")
        menu.addSeparator()
        act_loc = menu.addAction("打开文件所在位置")
        menu.addSeparator()
        act_del = menu.addAction("删除")
        action = menu.exec_(self.tree.viewport().mapToGlobal(pos))
        if action == act_open:
            self.open_selected()
        elif action == act_loc:
            self.open_location()
        elif action == act_del:
            self.delete_selected()

