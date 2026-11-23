# -*- coding: utf-8 -*-
"""国家标准公告页：最新10条、本地缓存、仅在有新公告号时后台刷新。"""
import webbrowser

from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *

from app_env import logger
from .widgets import GlassButton
from . import theme as _theme


class _AnnouncementCheckWorker(QThread):
    """后台探测：有更新则带回新列表；无更新则带回空。"""
    updated = pyqtSignal(list)          # 有更新
    no_update = pyqtSignal(str)         # 无更新，参数=最新公告号
    failed = pyqtSignal(str)
    force_loaded = pyqtSignal(list)     # 强制全量刷新成功

    def __init__(self, session, cached_latest='', force=False, parent=None):
        super().__init__(parent)
        self.session = session
        self.cached_latest = cached_latest or ''
        self.force = force

    def run(self):
        try:
            from spiders.gb_announcement import (
                fetch_announcement_list,
                check_list_needs_refresh,
                save_list_cache,
            )
            if self.force:
                items, _, site_latest = fetch_announcement_list(self.session)
                save_list_cache(items, latest_code=site_latest)
                self.force_loaded.emit(items)
                return
            need, items, remote_latest = check_list_needs_refresh(
                self.session, self.cached_latest
            )
            if need and items is not None:
                save_list_cache(items, latest_code=remote_latest)
                self.updated.emit(items)
            else:
                self.no_update.emit(remote_latest or self.cached_latest)
        except Exception as e:
            logger.warning(f"[国标公告] 后台检查失败: {e}")
            self.failed.emit(str(e))


class _AnnouncementDetailWorker(QThread):
    finished_ok = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, session, pid, list_item=None, parent=None):
        super().__init__(parent)
        self.session = session
        self.pid = pid
        self.list_item = list_item

    def run(self):
        try:
            from spiders.gb_announcement import get_announcement_detail
            detail = get_announcement_detail(
                self.session, self.pid, list_item=self.list_item
            )
            self.finished_ok.emit(detail)
        except Exception as e:
            logger.warning(f"[国标公告] 详情失败: {e}")
            self.failed.emit(str(e))


class GbAnnouncementPage(QWidget):
    """国标公告：最新 10 条；打开先读缓存；后台仅在有新公告号时刷新。"""

    def __init__(self, get_session, parent=None):
        super().__init__(parent)
        self.get_session = get_session
        self._check_worker = None
        self._detail_worker = None
        self._items = []
        self._cached_latest = ''
        self._init_ui()
        self._apply_style()
        QTimer.singleShot(100, self._bootstrap)

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 8, 0, 0)
        root.setSpacing(8)

        # 方案 B：无独立工具行；列表操作在左顶栏，外链在右顶栏
        pane_header_h = 52
        row_h = 38
        btn_h = 32

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)

        left = QWidget()
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(0, 0, 0, 0)
        left_l.setSpacing(8)
        left_head = QWidget()
        left_head.setFixedHeight(pane_header_h)
        left_head_l = QHBoxLayout(left_head)
        left_head_l.setContentsMargins(0, 0, 0, 0)
        left_head_l.setSpacing(8)
        left_title = QLabel("国家标准公告列表")
        left_title.setObjectName("AnnTitle")
        left_title.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        left_head_l.addWidget(left_title, stretch=1)
        self.refresh_btn = GlassButton("检查更新", _theme.primary_qcolor())
        self.refresh_btn.setFixedHeight(btn_h)
        self.refresh_btn.clicked.connect(lambda: self._start_background_check(force=False))
        left_head_l.addWidget(self.refresh_btn, alignment=Qt.AlignVCenter)
        self.force_btn = GlassButton("强制刷新", _theme.primary_qcolor())
        self.force_btn.setFixedHeight(btn_h)
        self.force_btn.clicked.connect(lambda: self._start_background_check(force=True))
        left_head_l.addWidget(self.force_btn, alignment=Qt.AlignVCenter)
        left_l.addWidget(left_head)

        self.list_table = QTableWidget(0, 3)
        self.list_table.setObjectName("AnnListTable")
        self.list_table.setHorizontalHeaderLabels(["公告号", "公告标题", "发布日期"])
        self.list_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.list_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.list_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.list_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.list_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.list_table.verticalHeader().setVisible(False)
        self.list_table.setAlternatingRowColors(True)
        self.list_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_table.verticalHeader().setDefaultSectionSize(row_h)
        self.list_table.verticalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.list_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.list_table.cellDoubleClicked.connect(self._on_list_activated)
        self.list_table.itemSelectionChanged.connect(self._on_selection_changed)
        left_l.addWidget(self.list_table, stretch=1)
        split.addWidget(left)

        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.setSpacing(8)
        right_head = QWidget()
        right_head.setFixedHeight(pane_header_h)
        right_head_l = QHBoxLayout(right_head)
        right_head_l.setContentsMargins(0, 0, 0, 0)
        right_head_l.setSpacing(8)
        right_text = QVBoxLayout()
        right_text.setContentsMargins(0, 0, 0, 0)
        right_text.setSpacing(2)
        self.detail_title = QLabel("请选择左侧公告")
        self.detail_title.setObjectName("AnnDetailTitle")
        self.detail_title.setWordWrap(False)
        right_text.addWidget(self.detail_title)
        self.detail_meta = QLabel("")
        self.detail_meta.setObjectName("AnnStatus")
        self.detail_meta.setWordWrap(False)
        right_text.addWidget(self.detail_meta)
        right_text.addStretch(1)
        right_head_l.addLayout(right_text, stretch=1)
        self.open_detail_btn = GlassButton("打开原文", _theme.primary_qcolor())
        self.open_detail_btn.setFixedHeight(btn_h)
        self.open_detail_btn.setEnabled(False)
        self.open_detail_btn.clicked.connect(self._open_current_detail)
        right_head_l.addWidget(self.open_detail_btn, alignment=Qt.AlignVCenter)
        self.open_portal_btn = GlassButton("打开官网", _theme.primary_qcolor())
        self.open_portal_btn.setFixedHeight(btn_h)
        self.open_portal_btn.clicked.connect(self._open_portal)
        right_head_l.addWidget(self.open_portal_btn, alignment=Qt.AlignVCenter)
        right_l.addWidget(right_head)

        self.detail_table = QTableWidget(0, 4)
        self.detail_table.setHorizontalHeaderLabels(
            ["标准编号", "标准名称", "代替标准", "实施日期"]
        )
        self.detail_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.detail_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.detail_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.detail_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.detail_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.detail_table.verticalHeader().setVisible(False)
        self.detail_table.setAlternatingRowColors(True)
        self.detail_table.verticalHeader().setDefaultSectionSize(row_h)
        self.detail_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        right_l.addWidget(self.detail_table, stretch=1)
        from .widgets import install_std_row_helpers

        def _ann_detail_info(row):
            no_item = self.detail_table.item(row, 0)
            name_item = self.detail_table.item(row, 1)
            if no_item is None and name_item is None:
                return None
            return (
                no_item.text() if no_item else '',
                name_item.text() if name_item else '',
            )

        install_std_row_helpers(self.detail_table, _ann_detail_info)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        root.addWidget(split, stretch=1)

        self._current_detail_url = ''

    def _apply_style(self):
        self.setStyleSheet(f"""
            QLabel#AnnTitle {{
                color: {_theme.PRIMARY}; font-size: 18px; font-weight: bold;
            }}
            QLabel#AnnStatus {{ color: {_theme.MUTED}; font-size: 12px; }}
            QLabel#AnnDetailTitle {{
                color: {_theme.TEXT}; font-size: 15px; font-weight: bold;
            }}
            QTableWidget {{
                background: rgba(255,255,255,230);
                border: 1px solid {_theme.BORDER};
                border-radius: 8px;
                gridline-color: {_theme.BORDER};
                font-size: 13px;
            }}
            QTableWidget#AnnListTable {{
                font-size: 14px;
            }}
            QTableWidget#AnnListTable QHeaderView::section {{
                font-size: 14px;
                padding: 8px 6px;
            }}
            QLabel#AnnMetaAlert {{
                color: {_theme.DANGER}; font-size: 12px; font-weight: bold;
            }}
            QHeaderView::section {{
                background: {_theme.PRIMARY};
                color: #fff;
                padding: 6px;
                border: none;
                font-weight: bold;
            }}
        """)

    def _bootstrap(self):
        """先展示本地缓存，再后台静默检查是否有更新。"""
        from spiders.gb_announcement import load_list_cache, LIST_DISPLAY_LIMIT
        cache = load_list_cache()
        if cache and cache.get('items'):
            self._cached_latest = cache.get('latest_code') or ''
            self._apply_items(
                cache['items'][:LIST_DISPLAY_LIMIT],
                source=f"本地缓存（{cache.get('saved_at', '')}）",
            )
            self._start_background_check(force=False)
        else:
            self._start_background_check(force=True)

    def _start_background_check(self, force=False):
        if self._check_worker and self._check_worker.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self.force_btn.setEnabled(False)
        session = self.get_session()
        self._check_worker = _AnnouncementCheckWorker(
            session, cached_latest=self._cached_latest, force=force, parent=self
        )
        self._check_worker.updated.connect(self._on_remote_updated)
        self._check_worker.no_update.connect(self._on_no_update)
        self._check_worker.force_loaded.connect(self._on_force_loaded)
        self._check_worker.failed.connect(self._on_check_failed)
        self._check_worker.start()

    def _on_remote_updated(self, items):
        self.refresh_btn.setEnabled(True)
        self.force_btn.setEnabled(True)
        from spiders.gb_announcement import load_list_cache
        cache = load_list_cache()
        self._cached_latest = (cache or {}).get('latest_code') or self._cached_latest
        self._apply_items(items, source="源站更新")
        self._auto_match_ledger_if_latest_civil(items)

    def _on_no_update(self, latest_code):
        self.refresh_btn.setEnabled(True)
        self.force_btn.setEnabled(True)
        if latest_code:
            self._cached_latest = latest_code

    def _on_force_loaded(self, items):
        self.refresh_btn.setEnabled(True)
        self.force_btn.setEnabled(True)
        from spiders.gb_announcement import load_list_cache
        cache = load_list_cache()
        self._cached_latest = (cache or {}).get('latest_code') or self._cached_latest
        self._apply_items(items, source="强制刷新")
        # 首次拉取或强制刷新后：若最新公告含工程相关，同样自动对照台账
        self._auto_match_ledger_if_latest_civil(items)

    def _on_check_failed(self, msg):
        self.refresh_btn.setEnabled(True)
        self.force_btn.setEnabled(True)
        logger.warning(f"[国标公告] 检查失败: {msg}")

    def _auto_match_ledger_if_latest_civil(self, items):
        """源站最新公告号若落在工程相关列表中，则自动对照标准台账。"""
        from spiders.gb_announcement import notice_code_sort_key, load_list_cache
        cache = load_list_cache()
        site_latest = (cache or {}).get('latest_code') or self._cached_latest or ''
        if not site_latest or not items:
            return
        site_key = notice_code_sort_key(site_latest)
        has_civil_latest = any(
            notice_code_sort_key(it.get('code') or '') == site_key
            for it in (items or [])
        )
        if not has_civil_latest:
            logger.info(
                f"[国标公告] 最新 {site_latest} 无工程相关标准，跳过台账对照"
            )
            return
        win = self.window()
        if hasattr(win, 'run_ledger_announcement_match'):
            win.run_ledger_announcement_match(notice_code=site_latest)
    def _apply_items(self, items, source=''):
        from spiders.gb_announcement import LIST_DISPLAY_LIMIT
        self._items = list(items or [])[:LIST_DISPLAY_LIMIT]
        self.list_table.setRowCount(0)
        for it in self._items:
            row = self.list_table.rowCount()
            self.list_table.insertRow(row)
            self.list_table.setItem(row, 0, QTableWidgetItem(it.get('code', '')))
            self.list_table.setItem(row, 1, QTableWidgetItem(it.get('title', '')))
            self.list_table.setItem(row, 2, QTableWidgetItem(it.get('date', '')))
            self.list_table.item(row, 0).setData(Qt.UserRole, it)
        if source:
            logger.info(f"[国标公告] 界面应用 {len(self._items)} 条（{source}）")

    def _on_selection_changed(self):
        rows = self.list_table.selectionModel().selectedRows()
        if not rows:
            return
        item = self.list_table.item(rows[0].row(), 0)
        if not item:
            return
        data = item.data(Qt.UserRole)
        if data:
            self._load_detail(data)

    def _on_list_activated(self, row, _col):
        item = self.list_table.item(row, 0)
        if not item:
            return
        data = item.data(Qt.UserRole)
        if data:
            self._load_detail(data)

    def _load_detail(self, data):
        if self._detail_worker and self._detail_worker.isRunning():
            return
        pid = data.get('pid') or ''
        self.detail_title.setText(data.get('display_title') or data.get('code') or '加载中…')
        self.detail_meta.setObjectName("AnnStatus")
        self.detail_meta.setStyleSheet("")
        self._current_detail_url = data.get('detail_url') or ''
        self.open_detail_btn.setEnabled(bool(self._current_detail_url))

        from spiders.gb_announcement import detail_from_cached_item
        cached = detail_from_cached_item(data)
        if cached:
            self._on_detail_ok(cached)
            return

        self.detail_meta.setText("正在加载…")
        self.detail_table.setRowCount(0)
        session = self.get_session()
        self._detail_worker = _AnnouncementDetailWorker(
            session, pid, list_item=data, parent=self
        )
        self._detail_worker.finished_ok.connect(self._on_detail_ok)
        self._detail_worker.failed.connect(self._on_detail_fail)
        self._detail_worker.start()

    def _on_detail_ok(self, detail):
        self.detail_title.setText(detail.get('display_title') or detail.get('title') or '')
        self._current_detail_url = detail.get('detail_url') or self._current_detail_url
        self.open_detail_btn.setEnabled(bool(self._current_detail_url))
        stds = detail.get('standards') or []
        raw_n = detail.get('raw_count', 0)
        civil_n = detail.get('civil_count', 0)
        if civil_n == 0:
            self.detail_meta.setObjectName("AnnStatus")
            self.detail_meta.setStyleSheet("")
            self.detail_meta.setText(f"共 {raw_n} 项 · 工程相关 0 项")
        else:
            self.detail_meta.setObjectName("AnnMetaAlert")
            self.detail_meta.setStyleSheet("")
            self.detail_meta.style().unpolish(self.detail_meta)
            self.detail_meta.style().polish(self.detail_meta)
            self.detail_meta.setText(f"共 {raw_n} 项 · 工程相关 {civil_n} 项")
        self.detail_table.setRowCount(0)
        from ledger.store import _split_replace_nos
        for s in stds:
            reps = _split_replace_nos(s.get('replace_no'))
            if not reps:
                reps = ['']
            for rep in reps:
                row = self.detail_table.rowCount()
                self.detail_table.insertRow(row)
                self.detail_table.setItem(row, 0, QTableWidgetItem(s.get('std_no', '')))
                self.detail_table.setItem(row, 1, QTableWidgetItem(s.get('std_name', '')))
                self.detail_table.setItem(row, 2, QTableWidgetItem(rep))
                self.detail_table.setItem(row, 3, QTableWidgetItem(s.get('implement_date', '')))

    def _on_detail_fail(self, msg):
        self.detail_meta.setObjectName("AnnStatus")
        self.detail_meta.setStyleSheet("")
        self.detail_meta.setText(f"详情加载失败：{msg}")
        self.open_detail_btn.setEnabled(bool(self._current_detail_url))

    def _open_portal(self):
        from spiders.gb_announcement import NOC_PORTAL_URL
        webbrowser.open(NOC_PORTAL_URL)

    def _open_current_detail(self):
        if self._current_detail_url:
            webbrowser.open(self._current_detail_url)
