# -*- coding: utf-8 -*-
"""标准台账页：总览 / 全部标准 / 待替代。"""
import json
import os

from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *

from app_env import logger, TEMP_DIR
from ledger import StandardLedgerStore
from .widgets import GlassButton, install_std_row_helpers, ask_yes_no
from . import theme as _theme

# 全部标准表列：勾选 | 管理编号 | 标准编号 | 名称 | 状态 | 发布 | 实施
_COL_CHECK = 0
_COL_MGMT = 1
_COL_STD_NO = 2
_COL_NAME = 3
_COL_STATUS = 4
_COL_PUB = 5
_COL_IMPL = 6

_WIDTHS_FILE = os.path.join(TEMP_DIR, 'ledger_table_widths.json')

# 按常见窗口默认列宽（末列 Stretch 不写入）
_DEFAULT_LEDGER_WIDTHS = [36, 150, 140, 300, 88, 100]
_DEFAULT_PENDING_WIDTHS = [130, 130, 260, 100, 110]
_DEFAULT_HISTORY_WIDTHS = [130, 130, 240, 100, 110, 110]

_REASON_LABEL = {
    '已到期': '实施到期，已作废',
    '查新确认': '查新确认已作废',
}

_SOURCE_FALLBACK = {
    'announcement': '国标公告',
    'query': '标准查新',
    'import': '台账导入',
    'manual': '手动添加',
}


def _origin_display(row):
    """待替代/已归档「来源公告」：公告号，或查新命中的站点名。"""
    if not isinstance(row, dict):
        return ''
    code = (row.get('notice_code') or '').strip()
    if code:
        return code
    return _SOURCE_FALLBACK.get((row.get('source') or '').strip(), '')


def _date_only(text):
    """时间戳只保留日期部分。"""
    s = (text or '').strip()
    if not s:
        return ''
    if ' ' in s:
        return s.split(' ', 1)[0]
    if 'T' in s:
        return s.split('T', 1)[0]
    return s[:10] if len(s) >= 10 and s[4] == '-' else s


class StandardLedgerPage(QWidget):
    """标准台账：本地清单 + 公告对照待替代提醒。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = StandardLedgerStore()
        self._spider = None
        self._refresh_worker = None
        self._refresh_queue = []
        self._refresh_total = 0
        self._refresh_index = 0
        self._refresh_updated = 0
        self._refresh_missing = 0
        self._refresh_failed = 0
        self._refresh_replacements = 0
        self._refresh_cancelled = False
        self._refresh_item_errored = False
        self._header_all_checked = False
        self._ui_dirty = True
        self._widths = self._load_widths()
        self._width_save_timer = QTimer(self)
        self._width_save_timer.setSingleShot(True)
        self._width_save_timer.setInterval(400)
        self._width_save_timer.timeout.connect(self._persist_widths)
        self._init_ui()
        self._apply_style()
        # 首屏由 showEvent 按需刷新，避免与切页路径重复整表重建

    def mark_dirty(self):
        """外部改动台账后标记，下次进入本页再刷新。"""
        self._ui_dirty = True

    def showEvent(self, event):
        super().showEvent(event)
        # 切页不做公告对照（由国标公告「检查更新」或首次导入触发）
        archived = self.store.expire_due_replacements()
        if archived:
            self._ui_dirty = True
        if self._ui_dirty:
            self._ui_dirty = False
            QTimer.singleShot(0, self.refresh_all)

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 4, 0, 0)
        root.setSpacing(8)

        self.banner = QLabel("")
        self.banner.setObjectName("LedgerBanner")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.banner.mousePressEvent = lambda _e: self.tabs.setCurrentIndex(2)
        root.addWidget(self.banner)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("LedgerTabs")

        corner = QWidget()
        tools = QHBoxLayout(corner)
        tools.setContentsMargins(0, 0, 4, 0)
        tools.setSpacing(6)
        self.import_btn = GlassButton("导入台账", _theme.primary_qcolor())
        self.import_btn.setFixedHeight(30)
        self.import_btn.clicked.connect(self._on_import)
        tools.addWidget(self.import_btn)
        self.add_btn = GlassButton("手动添加", _theme.primary_qcolor())
        self.add_btn.setFixedHeight(30)
        self.add_btn.clicked.connect(self._on_add_manual)
        tools.addWidget(self.add_btn)
        self.refresh_btn = GlassButton("刷新", _theme.primary_qcolor())
        self.refresh_btn.setFixedHeight(30)
        self.refresh_btn.clicked.connect(self.refresh_all)
        tools.addWidget(self.refresh_btn)
        self.query_btn = GlassButton("标准查新", _theme.accent_qcolor())
        self.query_btn.setFixedHeight(30)
        self.query_btn.clicked.connect(self._on_standard_refresh)
        tools.addWidget(self.query_btn)
        self.tabs.setCornerWidget(corner, Qt.TopRightCorner)

        overview = QWidget()
        ov = QVBoxLayout(overview)
        ov.setContentsMargins(4, 8, 4, 4)
        ov.setSpacing(12)
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.card_total = self._stat_card("台账总数", "0")
        self.card_current = self._stat_card("现行有效", "0")
        self.card_pending = self._stat_card("即将被替代", "0")
        self.card_history = self._stat_card("已归档变更", "0")
        for c in (self.card_total, self.card_current, self.card_pending, self.card_history):
            cards.addWidget(c)
        ov.addLayout(cards)
        tip = QLabel(
            "从「在线查询」右键可将标准加入台账；在「全部标准」勾选后点「标准查新」联网更新。"
            "若检出替代版本，会写入台账新行；未到实施日进「待替代」，已作废/已实施进「已归档变更」。"
            "国标公告「检查更新」发现含工程相关的最新公告时，会自动对照台账；"
            "首次导入后会对照上一年6月之后至最新的全部缓存公告一次。"
        )
        tip.setObjectName("LedgerTip")
        tip.setWordWrap(True)
        ov.addWidget(tip)
        ov.addStretch(1)
        self.tabs.addTab(overview, "总览")

        all_page = QWidget()
        all_l = QVBoxLayout(all_page)
        all_l.setContentsMargins(4, 8, 4, 4)
        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索编号、名称或管理编号…")
        self.search_edit.textChanged.connect(self._filter_ledger_table)
        search_row.addWidget(self.search_edit, stretch=1)
        self.refresh_progress = QLabel("")
        self.refresh_progress.setObjectName("LedgerTip")
        search_row.addWidget(self.refresh_progress)
        del_btn = GlassButton("删除选中", _theme.danger_qcolor())
        del_btn.setFixedHeight(32)
        del_btn.clicked.connect(self._on_delete_selected)
        search_row.addWidget(del_btn)
        all_l.addLayout(search_row)
        self.ledger_table = self._make_ledger_table()
        all_l.addWidget(self.ledger_table, stretch=1)
        self.tabs.addTab(all_page, "全部标准")

        pend_page = QWidget()
        pend_l = QVBoxLayout(pend_page)
        pend_l.setContentsMargins(4, 8, 4, 4)
        self.pending_meta = QLabel("")
        self.pending_meta.setObjectName("LedgerTip")
        pend_l.addWidget(self.pending_meta)
        self.pending_table = self._make_table(
            ["原标准", "将被替代为", "新标准名称", "实施日期", "来源公告", "登记日期"]
        )
        self._apply_saved_widths(
            self.pending_table, 'pending', _DEFAULT_PENDING_WIDTHS
        )
        pend_l.addWidget(self.pending_table, stretch=1)
        self.tabs.addTab(pend_page, "待替代")

        hist_page = QWidget()
        hist_l = QVBoxLayout(hist_page)
        hist_l.setContentsMargins(4, 8, 4, 4)
        self.history_meta = QLabel("")
        self.history_meta.setObjectName("LedgerTip")
        hist_l.addWidget(self.history_meta)
        self.history_table = self._make_table(
            ["原标准", "替代标准", "新标准名称", "实施日期", "归档说明", "来源公告", "归档日期"]
        )
        self._apply_saved_widths(
            self.history_table, 'history', _DEFAULT_HISTORY_WIDTHS
        )
        hist_l.addWidget(self.history_table, stretch=1)
        self.tabs.addTab(hist_page, "已归档变更")

        root.addWidget(self.tabs, stretch=1)

        # 表都建好后再监听拖宽，避免初始化时误存
        self.ledger_table.horizontalHeader().sectionResized.connect(
            lambda *_: self._schedule_width_save()
        )
        self.pending_table.horizontalHeader().sectionResized.connect(
            lambda *_: self._schedule_width_save()
        )
        self.history_table.horizontalHeader().sectionResized.connect(
            lambda *_: self._schedule_width_save()
        )

        def _ledger_info(row):
            no_item = self.ledger_table.item(row, _COL_STD_NO)
            name_item = self.ledger_table.item(row, _COL_NAME)
            if no_item is None and name_item is None:
                return None
            return (
                no_item.text() if no_item else '',
                name_item.text() if name_item else '',
            )

        def _pending_info(row):
            old_item = self.pending_table.item(row, 0)
            new_item = self.pending_table.item(row, 1)
            name_item = self.pending_table.item(row, 2)
            old_no = old_item.text() if old_item else ''
            new_no = new_item.text() if new_item else ''
            name = name_item.text() if name_item else ''
            no = f"{old_no} → {new_no}" if (old_no or new_no) else ''
            return (no, name) if (no or name) else None

        def _history_info(row):
            old_item = self.history_table.item(row, 0)
            new_item = self.history_table.item(row, 1)
            name_item = self.history_table.item(row, 2)
            old_no = old_item.text() if old_item else ''
            new_no = new_item.text() if new_item else ''
            name = name_item.text() if name_item else ''
            no = f"{old_no} → {new_no}" if (old_no or new_no) else ''
            return (no, name) if (no or name) else None

        install_std_row_helpers(self.ledger_table, _ledger_info)
        install_std_row_helpers(self.pending_table, _pending_info)
        install_std_row_helpers(self.history_table, _history_info)

        self.card_total.mousePressEvent = lambda _e: self.tabs.setCurrentIndex(1)
        self.card_current.mousePressEvent = lambda _e: self.tabs.setCurrentIndex(1)
        self.card_pending.mousePressEvent = lambda _e: self.tabs.setCurrentIndex(2)
        self.card_history.mousePressEvent = lambda _e: self.tabs.setCurrentIndex(3)
    def _stat_card(self, label, value):
        w = QFrame()
        w.setObjectName("StatCard")
        w.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 14, 16, 14)
        v = QLabel(value)
        v.setObjectName("StatValue")
        v.setAlignment(Qt.AlignCenter)
        lay.addWidget(v)
        t = QLabel(label)
        t.setObjectName("StatLabel")
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(t)
        w._value_label = v
        return w

    def _make_table(self, headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        # 禁止横向滚动，否则末列会跟着内容区移动，右边线看起来不固定
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        hh = table.horizontalHeader()
        hh.setSectionsMovable(False)
        hh.setCascadingSectionResizes(False)
        # 末列 Stretch + stretchLastSection：右边线贴死表格右缘，拖其它列时右边不动
        last = len(headers) - 1
        for i in range(len(headers)):
            if i == last:
                hh.setSectionResizeMode(i, QHeaderView.Stretch)
            else:
                hh.setSectionResizeMode(i, QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        return table

    def _make_ledger_table(self):
        headers = [
            "", "管理编号", "标准编号", "标准名称", "状态", "发布日期", "实施日期"
        ]
        table = self._make_table(headers)
        hh = table.horizontalHeader()
        hh.setSectionResizeMode(_COL_CHECK, QHeaderView.Fixed)
        for col in (
            _COL_MGMT, _COL_STD_NO, _COL_NAME, _COL_STATUS, _COL_PUB,
        ):
            hh.setSectionResizeMode(col, QHeaderView.Interactive)
        # 「实施日期」末列 Stretch，右边线贴死表格右缘
        hh.setSectionResizeMode(_COL_IMPL, QHeaderView.Stretch)
        hh.setStretchLastSection(True)
        self._apply_saved_widths(table, 'ledger', _DEFAULT_LEDGER_WIDTHS)
        hh.setMinimumSectionSize(40)
        hh.setSectionsClickable(True)
        hh.sectionClicked.connect(self._on_ledger_header_clicked)
        head = QTableWidgetItem("☑")
        head.setToolTip("单击切换全选/全不选")
        head.setTextAlignment(Qt.AlignCenter)
        table.setHorizontalHeaderItem(_COL_CHECK, head)
        return table

    def _load_widths(self):
        try:
            if os.path.isfile(_WIDTHS_FILE):
                with open(_WIDTHS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.warning(f"[标准台账] 读列宽失败: {e}")
        return {}

    def _apply_saved_widths(self, table, key, defaults):
        """恢复交互列宽；末列保持 Stretch 不设固定宽。"""
        saved = self._widths.get(key) if isinstance(self._widths, dict) else None
        widths = list(defaults)
        if isinstance(saved, list) and len(saved) == len(defaults):
            try:
                widths = [int(x) for x in saved]
            except (TypeError, ValueError):
                widths = list(defaults)
        last = table.columnCount() - 1
        for i, w in enumerate(widths):
            if i >= last:
                break
            if w > 0:
                table.setColumnWidth(i, w)

    def _schedule_width_save(self):
        self._width_save_timer.start()

    def _persist_widths(self):
        if not hasattr(self, 'ledger_table') or not hasattr(self, 'pending_table'):
            return
        if not hasattr(self, 'history_table'):
            return
        try:
            def grab(table, n_interactive):
                return [table.columnWidth(i) for i in range(n_interactive)]

            payload = {
                'ledger': grab(self.ledger_table, len(_DEFAULT_LEDGER_WIDTHS)),
                'pending': grab(self.pending_table, len(_DEFAULT_PENDING_WIDTHS)),
                'history': grab(self.history_table, len(_DEFAULT_HISTORY_WIDTHS)),
            }
            self._widths = payload
            os.makedirs(TEMP_DIR, exist_ok=True)
            with open(_WIDTHS_FILE, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[标准台账] 保存列宽失败: {e}")

    def _apply_style(self):
        self.setStyleSheet(f"""
            QLabel#LedgerTip {{ color: {_theme.MUTED}; font-size: 12px; }}
            QLabel#LedgerBanner {{
                background: {_theme.DANGER};
                color: #ffffff;
                font-size: 13px;
                font-weight: bold;
                padding: 10px 14px;
                border-radius: 8px;
            }}
            QFrame#StatCard {{
                background: #ffffff;
                border: 1px solid {_theme.BORDER};
                border-radius: 12px;
            }}
            QLabel#StatValue {{
                color: {_theme.PRIMARY}; font-size: 28px; font-weight: bold;
            }}
            QLabel#StatLabel {{ color: {_theme.MUTED}; font-size: 13px; }}
            QTabWidget#LedgerTabs::pane {{
                border: 1px solid {_theme.BORDER};
                border-radius: 8px;
                background: rgba(255,255,255,200);
                top: -1px;
            }}
            QTabBar::tab {{
                padding: 8px 18px;
                margin-right: 4px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                background: rgba(255,255,255,160);
                color: {_theme.TEXT};
            }}
            QTabBar::tab:selected {{
                background: {_theme.PRIMARY};
                color: #fff;
                font-weight: bold;
            }}
            QLineEdit {{
                padding: 8px 12px; border: 1px solid {_theme.BORDER};
                border-radius: 8px; background: #fff; font-size: 13px;
            }}
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
                padding: 6px 8px;
                border-bottom: 1px solid {_theme.ROW_LINE};
            }}
            QTableWidget::item:selected {{
                background: {_theme.SOFT};
                color: {_theme.TEXT};
            }}
            QTableWidget::item:alternate {{ background: {_theme.ROW_ALT}; }}
            QTableWidget::item:alternate:selected {{
                background: {_theme.SOFT};
                color: {_theme.TEXT};
            }}
            QHeaderView::section {{
                background: {_theme.PRIMARY};
                color: white;
                padding: 10px 8px;
                border: none;
                border-right: 1px solid rgba(255,255,255,100);
                font-weight: bold;
                font-size: 13px;
            }}
            QHeaderView::section:first {{ border-top-left-radius: 12px; }}
            QHeaderView::section:last {{
                border-top-right-radius: 12px; border-right: none;
            }}
        """)

    def refresh_all(self):
        # 重新读盘，避免进程内缓存与已更新的归档来源不一致
        try:
            self.store.load()
        except Exception as e:
            logger.warning(f"[标准台账] 刷新读盘失败: {e}")
        self.store.expire_due_replacements()
        st = self.store.stats()
        self.card_total._value_label.setText(str(st['total']))
        self.card_current._value_label.setText(str(st['current']))
        self.card_pending._value_label.setText(str(st['pending']))
        self.card_history._value_label.setText(str(st['history']))

        tip = self.store.banner_text()
        if tip:
            self.banner.setText(tip)
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)

        self._reload_ledger_table()
        self._reload_pending_table()
        self._reload_history_table()
        n = st['pending']
        self.tabs.setTabText(2, f"待替代（{n}）" if n else "待替代")
        h = st['history']
        self.tabs.setTabText(3, f"已归档变更（{h}）" if h else "已归档变更")

    def _reload_ledger_table(self):
        from ledger.store import normalize_std_key

        q = (self.search_edit.text() or '').strip().lower()
        all_rows = self.store.items()
        if q:
            matched_keys = set()
            for it in all_rows:
                if (q in (it.get('std_no') or '').lower()
                        or q in (it.get('std_name') or '').lower()
                        or q in (it.get('mgmt_no') or '').lower()):
                    matched_keys.add(normalize_std_key(it.get('std_no')))
            # 命中旧/新任一方时成对保留，便于紧挨显示
            for it in all_rows:
                k = normalize_std_key(it.get('std_no'))
                rep = normalize_std_key(it.get('replaces_std_no'))
                if k in matched_keys and rep:
                    matched_keys.add(rep)
                if rep and rep in matched_keys:
                    matched_keys.add(k)
            rows = [
                it for it in all_rows
                if normalize_std_key(it.get('std_no')) in matched_keys
            ]
        else:
            rows = all_rows

        rows = self._order_rows_with_replacements(rows)

        table = self.ledger_table
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(len(rows))
            self._header_all_checked = False
            head = table.horizontalHeaderItem(_COL_CHECK)
            if head:
                head.setText("☐")
            highlight = QColor(*_theme.REPLACEMENT_BG_RGB)
            obsolete_fg = QColor(_theme.DANGER)
            for r, it in enumerate(rows):
                status_text = it.get('status', '') or ''
                is_obsolete = (
                    '已被替代' in status_text
                    or '已作废' in status_text
                    or '废止' in status_text
                    or '作废' in status_text
                )
                is_repl = bool((it.get('replaces_std_no') or '').strip())
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                chk.setCheckState(Qt.Unchecked)
                chk.setTextAlignment(Qt.AlignCenter)
                if is_repl:
                    chk.setBackground(highlight)
                if is_obsolete:
                    chk.setForeground(obsolete_fg)
                table.setItem(r, _COL_CHECK, chk)
                vals = [
                    it.get('mgmt_no', '') or '',
                    it.get('std_no', ''),
                    it.get('std_name', ''),
                    status_text,
                    it.get('publish_date', ''),
                    it.get('implement_date', ''),
                ]
                for offset, text in enumerate(vals):
                    c = offset + 1
                    item = QTableWidgetItem(text)
                    if is_repl:
                        item.setBackground(highlight)
                    if is_obsolete:
                        item.setForeground(obsolete_fg)
                    elif c == _COL_STATUS and '即将被替代' in text:
                        item.setForeground(obsolete_fg)
                    table.setItem(r, c, item)
                table.item(r, _COL_STD_NO).setData(Qt.UserRole, it)
        finally:
            table.setUpdatesEnabled(True)

    @staticmethod
    def _order_rows_with_replacements(rows):
        """旧标准后紧跟其替代版；替代版不再单独占原位置。"""
        from ledger.store import normalize_std_key

        by_key = {}
        children = {}
        for it in rows:
            k = normalize_std_key(it.get('std_no'))
            by_key[k] = it
            parent = normalize_std_key(it.get('replaces_std_no'))
            if parent:
                children.setdefault(parent, []).append(it)

        child_keys = {
            normalize_std_key(it.get('std_no'))
            for lst in children.values() for it in lst
        }

        out = []
        seen = set()
        for it in rows:
            k = normalize_std_key(it.get('std_no'))
            if k in seen:
                continue
            if k in child_keys and normalize_std_key(it.get('replaces_std_no')) in by_key:
                continue
            out.append(it)
            seen.add(k)
            for child in children.get(k, []):
                ck = normalize_std_key(child.get('std_no'))
                if ck not in seen:
                    out.append(child)
                    seen.add(ck)
        for it in rows:
            k = normalize_std_key(it.get('std_no'))
            if k not in seen:
                out.append(it)
                seen.add(k)
        return out

    def _filter_ledger_table(self):
        self._reload_ledger_table()

    def _reload_pending_table(self):
        pend = self.store.pending()
        self.pending_meta.setText(
            f"共 {len(pend)} 项即将被替代（实施日期到期后自动清除本列表并归档）"
            if pend else "暂无待替代项。国标公告检查到含工程相关的最新公告后会自动写入。"
        )
        self.pending_table.setRowCount(0)
        for p in pend:
            r = self.pending_table.rowCount()
            self.pending_table.insertRow(r)
            vals = [
                p.get('old_std_no', ''),
                p.get('new_std_no', ''),
                p.get('new_std_name', ''),
                p.get('implement_date', ''),
                _origin_display(p),
                _date_only(p.get('created_at', '')),
            ]
            for c, text in enumerate(vals):
                self.pending_table.setItem(r, c, QTableWidgetItem(text))

    def _reload_history_table(self):
        hist = self.store.history()
        self.history_meta.setText(
            f"共 {len(hist)} 条已归档变更（原标准已作废/已被替代，新标准已写入台账）"
            if hist else "暂无已归档变更。查新确认作废或待替代实施到期后会出现在此。"
        )
        self.history_table.setRowCount(0)
        for h in hist:
            r = self.history_table.rowCount()
            self.history_table.insertRow(r)
            reason = (h.get('reason') or '').strip()
            reason_show = _REASON_LABEL.get(reason, reason or '已作废')
            vals = [
                h.get('old_std_no', ''),
                h.get('new_std_no', ''),
                h.get('new_std_name', ''),
                h.get('implement_date', ''),
                reason_show,
                _origin_display(h),
                _date_only(h.get('archived_at', '')),
            ]
            for c, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setForeground(QColor(_theme.DANGER))
                self.history_table.setItem(r, c, item)

    def _on_ledger_header_clicked(self, section):
        if section != _COL_CHECK:
            return
        self._header_all_checked = not self._header_all_checked
        state = Qt.Checked if self._header_all_checked else Qt.Unchecked
        head = self.ledger_table.horizontalHeaderItem(_COL_CHECK)
        if head:
            head.setText("☑" if self._header_all_checked else "☐")
        for r in range(self.ledger_table.rowCount()):
            item = self.ledger_table.item(r, _COL_CHECK)
            if item:
                item.setCheckState(state)

    def _checked_ledger_items(self):
        out = []
        for r in range(self.ledger_table.rowCount()):
            chk = self.ledger_table.item(r, _COL_CHECK)
            if chk is None or chk.checkState() != Qt.Checked:
                continue
            data_item = self.ledger_table.item(r, _COL_STD_NO)
            if data_item is None:
                continue
            it = data_item.data(Qt.UserRole)
            if isinstance(it, dict) and it.get('std_no'):
                out.append(it)
            else:
                no = (data_item.text() or '').strip()
                if no:
                    found = self.store.find_item(no)
                    if found:
                        out.append(found)
        return out

    def _get_spider(self):
        if self._spider is None:
            from spiders.spider import SpiderEngine
            self._spider = SpiderEngine()
        return self._spider

    def _on_standard_refresh(self):
        if self._refresh_worker is not None and self._refresh_worker.isRunning():
            self._refresh_cancelled = True
            try:
                self._refresh_worker.cancel()
            except Exception:
                pass
            self.refresh_progress.setText("正在停止查新…")
            return

        items = self._checked_ledger_items()
        if not items:
            QMessageBox.information(self, "标准查新", "请先在「全部标准」中勾选要查新的标准。")
            self.tabs.setCurrentIndex(1)
            return

        self.tabs.setCurrentIndex(1)
        self._refresh_queue = [it.get('std_no') for it in items if it.get('std_no')]
        self._refresh_total = len(self._refresh_queue)
        self._refresh_index = 0
        self._refresh_updated = 0
        self._refresh_missing = 0
        self._refresh_failed = 0
        self._refresh_replacements = 0
        self._refresh_cancelled = False
        self._refresh_item_errored = False
        self.query_btn.setText("停止查新")
        self._set_refresh_controls_enabled(False)
        self._start_next_refresh()

    def _set_refresh_controls_enabled(self, enabled):
        self.import_btn.setEnabled(enabled)
        self.add_btn.setEnabled(enabled)
        self.refresh_btn.setEnabled(enabled)

    def _start_next_refresh(self):
        if self._refresh_cancelled or self._refresh_index >= len(self._refresh_queue):
            self._finish_refresh()
            return
        keyword = self._refresh_queue[self._refresh_index]
        self.refresh_progress.setText(
            f"正在查新 {self._refresh_index + 1}/{self._refresh_total}：{keyword}"
        )
        from .workers import SearchWorker
        spider = self._get_spider()
        if hasattr(spider, 'reset_cancel'):
            spider.reset_cancel()
        worker = SearchWorker(spider, keyword, check_download=False)
        self._refresh_item_errored = False
        worker.finished.connect(self._on_refresh_item_finished)
        worker.error.connect(self._on_refresh_item_error)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        self._refresh_worker = worker
        worker.start()

    def _on_refresh_item_error(self, _msg):
        # SearchWorker 异常时还会再发 finished；此处只做标记，计数在 finished 里统一处理
        self._refresh_item_errored = True

    def _on_refresh_item_finished(self, results):
        if self._refresh_cancelled:
            self._finish_refresh()
            return
        keyword = self._refresh_queue[self._refresh_index]
        existing = self.store.find_item(keyword) or {}
        try:
            if self._refresh_item_errored:
                self._refresh_failed += 1
            elif results:
                self._apply_refresh_result(keyword, results[0], existing)
                self._refresh_updated += 1
            else:
                self._refresh_missing += 1
        except Exception as e:
            logger.warning(f"[标准台账] 查新写回失败 {keyword}: {e}")
            self._refresh_failed += 1
        self._refresh_item_errored = False
        self._refresh_index += 1
        self._refresh_worker = None
        QTimer.singleShot(0, self._start_next_refresh)

    def _apply_refresh_result(self, keyword, std, existing):
        """写回被查编号；若有替代版则入库，并登记待替代/已替代。"""
        from ledger.store import normalize_std_key, _parse_date
        from datetime import date

        old_no = (existing.get('std_no') or keyword or '').strip()
        status = (getattr(std, 'status', '') or '').strip()
        repl = getattr(std, 'replacement', None)
        repl_no = ''
        if repl is not None:
            repl_no = (getattr(repl, 'std_no', '') or '').strip()

        # 与在线查询一致：有替代时标注即将被替代 / 已被替代
        status_write = status or '现行'
        if repl_no:
            is_abolished = any(k in status for k in ('废止', '作废'))
            repl_pending = '即将实施' in (getattr(repl, 'status', '') or '')
            if not is_abolished:
                suffix = '即将被替代' if repl_pending else '已被替代'
                base = status or '现行'
                if suffix not in base:
                    status_write = f'{base}（{suffix}）'
            elif '被替代' not in status and '废止' in status:
                status_write = status

        self.store.upsert_item(
            old_no,
            std_name=getattr(std, 'std_name', '') or '',
            status=status_write,
            publish_date=getattr(std, 'publish_date', '') or '',
            implement_date=getattr(std, 'implement_date', '') or '',
            source='query',
            mgmt_no=existing.get('mgmt_no') or '',
        )

        if not repl_no:
            return
        if normalize_std_key(repl_no) == normalize_std_key(old_no):
            return

        # 新标准写入「全部标准」，紧挨旧标准下一行
        self.store.upsert_item(
            repl_no,
            std_name=getattr(repl, 'std_name', '') or '',
            status=getattr(repl, 'status', '') or '现行',
            publish_date=getattr(repl, 'publish_date', '') or '',
            implement_date=getattr(repl, 'implement_date', '') or '',
            source='query',
            mgmt_no='',
        )
        new_item = self.store.find_item(repl_no)
        if new_item is not None:
            new_item['replaces_std_no'] = old_no
        self.store.place_item_after(repl_no, old_no)
        self._refresh_replacements = getattr(self, '_refresh_replacements', 0) + 1

        impl = getattr(repl, 'implement_date', '') or ''
        impl_d = _parse_date(impl)
        # 查新命中站点写入「来源公告」（与国标公告号并列展示）
        site = (
            (getattr(std, 'source_site', None) or '').strip()
            or (getattr(repl, 'source_site', None) or '').strip()
        )
        if not site:
            logger.warning(f"[标准台账] 查新未带回站点名: {old_no}")
        else:
            logger.info(f"[标准台账] 查新来源站点: {site}（{old_no}）")
        # 未到期 → 待替代 Tab；已到期/已废止 → 归档为已被替代
        if impl_d and impl_d > date.today() and '废止' not in status and '作废' not in status:
            self.store.add_pending_replacement(
                old_no, repl_no,
                new_std_name=getattr(repl, 'std_name', '') or '',
                implement_date=impl,
                notice_code=site,
                source='query',
            )
        else:
            self.store._archive_replacement(
                old_no, repl_no,
                getattr(repl, 'std_name', '') or '',
                impl, site, 'query',
                reason='查新确认',
            )

    def _finish_refresh(self):
        self._refresh_worker = None
        self._refresh_queue = []
        self.query_btn.setText("标准查新")
        self._set_refresh_controls_enabled(True)
        self.refresh_all()
        repl_n = getattr(self, '_refresh_replacements', 0)
        repl_part = f"，写入替代 {repl_n}" if repl_n else ""
        if self._refresh_cancelled:
            msg = (
                f"查新已停止：已更新 {self._refresh_updated}{repl_part}，"
                f"未找到 {self._refresh_missing}，失败 {self._refresh_failed}"
            )
        else:
            msg = (
                f"查新完成：更新 {self._refresh_updated}{repl_part}，"
                f"未找到 {self._refresh_missing}，失败 {self._refresh_failed}"
            )
        self.refresh_progress.setText(msg)
        QMessageBox.information(self, "标准查新", msg)

    def match_latest_announcement(self, refresh_ui=True):
        """对照最新一份国标公告（日常检查更新用）。返回 (命中次数, 公告号)。"""
        from spiders.gb_announcement import load_list_cache, notice_code_sort_key
        cache = load_list_cache()
        if not cache or not (cache.get('items') or []):
            return 0, ''
        latest = max(
            cache['items'],
            key=lambda it: notice_code_sort_key(it.get('code') or ''),
        )
        code = (latest.get('code') or '').strip()
        n = self.store.match_announcement_cache(cache, latest_only=True)
        if refresh_ui:
            self.refresh_all()
        else:
            self.mark_dirty()
        return n, code

    def match_announcements_since_last_june(self, refresh_ui=True):
        """首次导入：对照上一年6月之后至最新的全部缓存国标公告。

        返回 (命中次数, 说明文案)。
        """
        from datetime import date as _date
        from spiders.gb_announcement import load_list_cache, notice_code_sort_key
        cache = load_list_cache()
        if not cache or not (cache.get('items') or []):
            return 0, ''
        today = _date.today()
        # 上一年6月之后 → 自上年7月1日起
        since = _date(today.year - 1, 7, 1)
        n = self.store.match_announcement_cache(
            cache, latest_only=False, since_date=since
        )
        items = cache.get('items') or []
        matched = []
        from ledger.store import _parse_date
        for it in items:
            d = _parse_date(it.get('date'))
            if d and d >= since and it.get('code'):
                matched.append(it.get('code'))
        matched.sort(key=lambda c: notice_code_sort_key(c), reverse=True)
        if matched:
            label = f"{matched[-1]}～{matched[0]}" if len(matched) > 1 else matched[0]
            label = f"{label}（共{len(matched)}份，自{since.isoformat()}）"
        else:
            label = f'自{since.isoformat()}起（缓存中无符合公告）'
        if refresh_ui:
            self.refresh_all()
        else:
            self.mark_dirty()
        return n, label

    def _on_add_manual(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("手动添加标准")
        dlg.setMinimumWidth(420)
        form = QFormLayout(dlg)
        mgmt = QLineEdit()
        no = QLineEdit()
        name = QLineEdit()
        status = QLineEdit("现行")
        pub = QLineEdit()
        impl = QLineEdit()
        form.addRow("管理编号", mgmt)
        form.addRow("标准编号 *", no)
        form.addRow("标准名称", name)
        form.addRow("状态", status)
        form.addRow("发布日期", pub)
        form.addRow("实施日期", impl)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec_() != QDialog.Accepted:
            return
        if not (no.text() or '').strip():
            QMessageBox.warning(self, "手动添加", "请填写标准编号。")
            return
        try:
            _, created = self.store.upsert_item(
                no.text().strip(),
                std_name=name.text().strip(),
                status=status.text().strip() or '现行',
                publish_date=pub.text().strip(),
                implement_date=impl.text().strip(),
                source='manual',
                mgmt_no=mgmt.text().strip(),
            )
            self.refresh_all()
            self.tabs.setCurrentIndex(1)
            QMessageBox.information(
                self, "手动添加", "已加入台账。" if created else "已更新台账中的同编号记录。"
            )
        except Exception as e:
            QMessageBox.warning(self, "手动添加", str(e))

    def _on_import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入台账", "",
            "Excel/CSV (*.xlsx *.xls *.csv);;所有文件 (*.*)"
        )
        if not path:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            rows = self._read_import_file(path)
            was_empty = len(self.store.items()) == 0
            added, updated, skipped = self.store.import_rows(rows)
            # 首次导入（导入前台账为空）：对照上一年6月之后至最新的全部缓存公告
            match_n, match_label = 0, ''
            try:
                if was_empty and (added or updated):
                    match_n, match_label = self.match_announcements_since_last_june(
                        refresh_ui=False
                    )
                elif added or updated:
                    # 非首次：只对照最新一份
                    match_n, match_label = self.match_latest_announcement(
                        refresh_ui=False
                    )
            except Exception as e:
                logger.warning(f"[标准台账] 导入后自动对照失败: {e}")
            self.refresh_all()
            self.tabs.setCurrentIndex(1)
            extra = ''
            if match_label:
                kind = '首次导入对照' if was_empty else '对照最新公告'
                extra = (
                    f"\n已自动{kind}「{match_label}」："
                    f"命中 {match_n}，待替代 {len(self.store.pending())} 条。"
                )
            elif was_empty:
                extra = "\n（暂无国标公告缓存，未自动对照；请先在「国标公告」检查更新。）"
            QMessageBox.information(
                self, "导入台账",
                f"导入完成：新增 {added}，更新 {updated}，跳过 {skipped}。{extra}"
            )
        except Exception as e:
            logger.warning(f"[标准台账] 导入失败: {e}")
            QMessageBox.warning(self, "导入台账", f"导入失败：{e}")
        finally:
            QApplication.restoreOverrideCursor()

    def _read_import_file(self, path):
        lower = path.lower()
        if lower.endswith('.csv'):
            import csv
            with open(path, 'r', encoding='utf-8-sig', newline='') as f:
                all_rows = list(csv.reader(f))
            return self._rows_from_table(all_rows)

        if lower.endswith('.xls') and not lower.endswith('.xlsx'):
            try:
                import xlrd
            except ImportError as e:
                raise RuntimeError(
                    "缺少 xlrd，无法读取旧版 .xls。请执行：pip install xlrd==2.0.1"
                ) from e
            wb = xlrd.open_workbook(path)
            sh = wb.sheet_by_index(0)
            table = []
            for r in range(sh.nrows):
                row = []
                for c in range(sh.ncols):
                    cell = sh.cell(r, c)
                    if cell.ctype == 3:
                        try:
                            dt = xlrd.xldate_as_datetime(cell.value, wb.datemode)
                            row.append(dt.strftime('%Y-%m-%d'))
                        except Exception:
                            row.append(self._import_cell_str(cell.value))
                    else:
                        row.append(self._import_cell_str(cell.value, datemode=wb.datemode))
                table.append(row)
            return self._rows_from_table(table)

        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        data = list(ws.iter_rows(values_only=True))
        wb.close()
        table = []
        for cells in data:
            table.append([self._import_cell_str(c) for c in (cells or [])])
        return self._rows_from_table(table)

    @staticmethod
    def _import_cell_str(val, datemode=None):
        if val is None or val == '':
            return ''
        from datetime import datetime, date
        if isinstance(val, datetime):
            return val.strftime('%Y-%m-%d')
        if isinstance(val, date):
            return val.isoformat()
        if isinstance(val, float):
            if datemode is not None and 20000 < val < 80000:
                try:
                    import xlrd
                    return xlrd.xldate_as_datetime(val, datemode).strftime('%Y-%m-%d')
                except Exception:
                    pass
            if val == int(val):
                return str(int(val))
            return str(val).strip()
        if isinstance(val, int):
            return str(val)
        return str(val).strip()

    def _rows_from_table(self, table):
        if not table:
            return []
        mapping = None
        start = 0
        for i, cells in enumerate(table[:30]):
            header = [self._import_cell_str(c) for c in (cells or [])]
            m = self._map_headers(header)
            if m is not None:
                mapping = m
                start = i + 1
                break
        if mapping is None:
            mapping = {
                0: 'std_no', 1: 'std_name', 2: 'status',
                3: 'publish_date', 4: 'implement_date',
            }
            start = 0

        rows = []
        for cells in table[start:]:
            cells = [self._import_cell_str(c) for c in (cells or [])]
            row = self._row_from_mapped(cells, mapping)
            if row.get('std_no'):
                rows.append(row)
        return rows

    def _map_headers(self, header):
        if not header:
            return None
        aliases = {
            'std_no': ('标准编号', '标准号', 'std_no', 'code'),
            'mgmt_no': ('管理编号', '规范受控管理编号', '受控编号', '受控管理编号', 'mgmt_no'),
            'std_name': ('标准名称', '名称', 'std_name', 'name'),
            'status': ('状态', '标准状态', 'status'),
            'publish_date': ('发布日期', '发布', 'publish_date'),
            'implement_date': ('实施日期', '实施', 'implement_date'),
        }
        mapping = {}
        for i, h in enumerate(header):
            h_raw = (h or '').strip()
            h_norm = h_raw.lower()
            for field, names in aliases.items():
                if h_norm in [n.lower() for n in names] or h_raw in names:
                    mapping[i] = field
                    break
        if 'std_no' not in mapping.values():
            return None
        return mapping

    def _row_from_mapped(self, cells, mapping):
        row = {
            'std_no': '', 'mgmt_no': '', 'std_name': '', 'status': '现行',
            'publish_date': '', 'implement_date': '', 'source': 'import',
        }
        for i, field in mapping.items():
            if i < len(cells):
                val = cells[i]
                row[field] = (val or '').strip() if isinstance(val, str) else str(val or '').strip()
        if not row.get('status'):
            row['status'] = '现行'
        return row

    def _on_delete_selected(self):
        checked = self._checked_ledger_items()
        nos = []
        if checked:
            nos = [it.get('std_no') for it in checked if it.get('std_no')]
        else:
            rows = self.ledger_table.selectionModel().selectedRows()
            for idx in rows:
                item = self.ledger_table.item(idx.row(), _COL_STD_NO)
                if item:
                    nos.append(item.text())
        nos = [n for n in nos if n]
        if not nos:
            QMessageBox.information(self, "删除", "请先勾选或选中要删除的台账行。")
            return
        if not ask_yes_no(self, "删除", f"确认从台账删除 {len(nos)} 条？"):
            return
        for no in nos:
            self.store.remove_item(no)
        self.refresh_all()

    def add_from_query(self, std_no, std_name='', status='', publish_date='',
                       implement_date='', mgmt_no=''):
        try:
            _, created = self.store.upsert_item(
                std_no, std_name=std_name, status=status or '现行',
                publish_date=publish_date, implement_date=implement_date,
                source='query', mgmt_no=mgmt_no or '',
            )
            self.refresh_all()
            return created
        except Exception as e:
            logger.warning(f"[标准台账] 加入失败: {e}")
            return False

    def pending_count(self):
        return len(self.store.pending())
