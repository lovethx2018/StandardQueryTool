# -*- coding: utf-8 -*-
"""主窗口。"""
import os
import re
import sys
import shutil
import glob
import tempfile
import logging
from pathlib import Path
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from app_env import logger, TEMP_DIR, UPDATE_DIR
from config import Config
from models import StandardInfo
from .workers import SearchWorker, BatchSearchWorker, DownloadWorker, LocalSearchWorker, DownloadCheckWorker, SilentPreviewDownloadWorker, LanIndexWarmWorker
from .widgets import StatusDot, DownloadButton, BubbleNotification, GlassButton, ask_yes_no
from .settings_dialog import SettingsDialog
from .document_manager import DocumentManagerPage
from .updater import (APP_VERSION, STABLE_EXE_NAME, is_newer_version, build_download_urls,
                     UpdateCheckWorker, UpdateDownloadWorker, ManualUpdateDialog,
                     is_setup_package_path, is_installed_app)
from . import theme as _theme
from utils import _safe_filename


class _SearchFocusFilter(QObject):
    """搜索框聚焦：选中全文便于覆盖输入，不清空，避免误触丢内容。"""
    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn:
            QTimer.singleShot(0, obj.selectAll)
        return super().eventFilter(obj, event)


class _CurrentPageStack(QStackedWidget):
    """按当前页尺寸布局，避免历史列表把单本/批量查询区一起撑高。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.currentChanged.connect(self._on_page_changed)

    def _on_page_changed(self, _index):
        self.updateGeometry()
        parent = self.parentWidget()
        if parent is not None:
            parent.updateGeometry()

    def sizeHint(self):
        w = self.currentWidget()
        return w.sizeHint() if w is not None else super().sizeHint()

    def minimumSizeHint(self):
        w = self.currentWidget()
        return w.minimumSizeHint() if w is not None else super().minimumSizeHint()


# 界面配色：统一取自 theme（主蓝 + 语义色）
_COLOR_PRIMARY = _theme.PRIMARY
_COLOR_ACCENT = _theme.ACCENT
_COLOR_MUTED = _theme.MUTED
_COLOR_BG = _theme.BG
_COLOR_SOFT = _theme.SOFT
_COLOR_IMPORT_BG = _theme.IMPORT_BG
_COLOR_BORDER = _theme.BORDER


class MainWindow(QMainWindow):
    """主窗口"""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"标准查询工具 {APP_VERSION}")
        self.setMinimumSize(1024, 680)
        
        # 初始化组件
        self._spider = None  # spider 引擎将在首次访问时懒初始化
        self.search_results = []
        from query_history.mode_buffers import QueryModeResultBuffers
        self._mode_result_bufs = QueryModeResultBuffers()  # 单本/历史结果互不覆盖
        self.batch_data = []
        self.current_search_index = 0
        self.search_worker = None
        self.total_search_count = 0
        self.download_enabled = False  # 单本查询"开启下载"开关，默认关闭
        self._download_workers = []  # 保留在跑的下载线程引用，防止被GC导致崩溃
        self._retired_workers = []  # 已取消但仍可能在跑的旧线程，防 GC / 防信号串台
        self._local_search_running = False  # 本地搜索进行中标志，探测须待其完成后再触发
        self._batch_search_running = False  # 批量检索进行中（勿用按钮禁用态推断：未导入时按钮本就禁用）
        self._detect_worker = None  # 按需下载源探测线程引用，防GC并用于防重复启动
        self._local_spider = None  # 本地匹配专用 engine（与检索/下载隔离）
        self._detect_spider = None  # 下载探测专用 engine（与检索/下载隔离）
        self._ui_spider = None  # UI 短操作专用 engine（浏览器打开详情等）
        self._batch_table_timer = None  # 批量检索结果表防抖刷新
        self._detect_gen = 0  # 探测代数：新查询时递增，忽略过期探测回调
        self._search_gen = 0  # 搜索代数：连点/退役后忽略过期 SearchWorker 回调
        self._silent_preview_worker = None
        self._preview_dialog = None
        self._preview_temp_path = None

        # 创建下载目录；清理上次残留的预览缓存
        try:
            os.makedirs(Config.effective_download_dir(), exist_ok=True)
        except Exception as e:
            logger.warning("创建默认下载目录失败（稍后可在设置中修改）: %s", e)
        self._cleanup_preview_cache()
        
        # 初始化UI
        self.init_ui()
        self.apply_theme()
        
        # 气泡通知
        self.bubble = BubbleNotification(self)

        # 自动更新相关状态；启动后延迟30分钟静默检查，仅在有新版本时提示用户
        # 条件：30分钟内未执行过批量检索或单本查询才触发
        self._update_check_worker = None
        self._update_dl_worker = None
        self._update_progress_dlg = None
        self._update_silent = True
        self._update_parent = self
        self._search_executed = False  # 标记是否已执行过查询（单本或批量）
        QTimer.singleShot(30 * 60 * 1000, lambda: self._delayed_update_check())
        # 启动后后台预热局域网索引，避免首次点下载/本地打开触发冷 rglob
        QTimer.singleShot(800, self._warm_lan_index_async)
        # 新电脑首次运行：启动约 5 秒后提示查看使用说明
        if not Config.FIRST_RUN_TIP_SHOWN:
            QTimer.singleShot(5000, self._show_first_run_tip)

    def _show_first_run_tip(self):
        """首次启动提示：建议观看使用说明（仅弹一次）。"""
        if Config.FIRST_RUN_TIP_SHOWN:
            return
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle("欢迎使用")
            box.setText("第一次使用，建议观看使用说明")
            open_btn = box.addButton("查看使用说明", QMessageBox.AcceptRole)
            box.addButton("知道了", QMessageBox.RejectRole)
            box.setDefaultButton(open_btn)
            box.exec_()
            opened = box.clickedButton() == open_btn
            Config.FIRST_RUN_TIP_SHOWN = True
            Config.save_settings()
            if opened:
                self.on_standard_help()
        except Exception as e:
            logger.warning(f"首次使用提示失败: {e}")
            Config.FIRST_RUN_TIP_SHOWN = True
            try:
                Config.save_settings()
            except Exception:
                pass

    def _get_spider(self):
        """懒初始化 spider 引擎（检索 + PDF 下载共用）。"""
        if self._spider is None:
            from spiders.spider import SpiderEngine
            self._spider = SpiderEngine()
        return self._spider

    def _get_local_spider(self):
        """本地目录匹配专用引擎，避免与检索/下载共享 session 与取消标志。"""
        if self._local_spider is None:
            from spiders.spider import SpiderEngine
            self._local_spider = SpiderEngine()
        return self._local_spider

    def _get_detect_spider(self):
        """下载源探测专用引擎，避免与检索/下载并发抢同一 session。"""
        if self._detect_spider is None:
            from spiders.spider import SpiderEngine
            self._detect_spider = SpiderEngine()
        return self._detect_spider

    def _get_ui_spider(self):
        """UI 短操作专用引擎（浏览器打开详情等），与检索线程隔离。"""
        if self._ui_spider is None:
            from spiders.spider import SpiderEngine
            self._ui_spider = SpiderEngine()
        return self._ui_spider

    def _invalidate_lan_cache(self):
        """失效所有引擎上的局域网索引缓存。"""
        for attr in ('_spider', '_local_spider', '_detect_spider', '_ui_spider'):
            eng = getattr(self, attr, None)
            if eng is not None:
                eng._lan_cache = None
        # 目录变更时若旧预热仍在跑，结束后再刷一次，避免写回旧目录索引
        self._lan_warm_pending = True

    def _warm_lan_index_async(self):
        """后台预热局域网索引，避免首次本地下载/打开触发冷 rglob。"""
        share = (Config.LAN_SHARE_DIR or "").strip()
        if not share:
            self._lan_warm_pending = False
            return
        old = getattr(self, '_lan_warm_worker', None)
        if old is not None and old.isRunning():
            self._lan_warm_pending = True
            return
        self._lan_warm_pending = False
        self._lan_warm_gen = getattr(self, '_lan_warm_gen', 0) + 1
        gen = self._lan_warm_gen
        worker = LanIndexWarmWorker(self._get_local_spider())
        worker.gen = gen
        worker.finished.connect(lambda _ok, g=gen: self._on_lan_warm_finished(g))
        self._lan_warm_worker = worker
        worker.start()

    def _on_lan_warm_finished(self, gen):
        if gen != getattr(self, '_lan_warm_gen', None):
            return
        if getattr(self, '_lan_warm_pending', False):
            self._warm_lan_index_async()

    def _find_in_lan_cached_only(self, std_no):
        """仅在缓存已 warm 时查找，不在 UI 线程触发 rglob 建索引。"""
        import time
        spider = self._get_local_spider()
        share = spider._lan_share_dir()
        if share is None:
            return None
        cache = getattr(spider, '_lan_cache', None)
        if not cache or cache.get('dir') != str(share):
            # 冷缓存：踢一脚后台预热，本次不阻塞 UI
            self._warm_lan_index_async()
            return None
        if time.time() - cache.get('ts', 0) >= 30:
            self._warm_lan_index_async()
            return None
        return spider._find_in_lan(std_no)

    def _schedule_batch_table_refresh(self):
        """批量检索期间防抖刷新结果表，避免每条都全量重建。"""
        if self._batch_table_timer is None:
            self._batch_table_timer = QTimer(self)
            self._batch_table_timer.setSingleShot(True)
            self._batch_table_timer.setInterval(400)
            self._batch_table_timer.timeout.connect(self._on_batch_table_refresh)
        self._batch_table_timer.start(400)

    def _on_batch_table_refresh(self):
        self.result_table.setUpdatesEnabled(False)
        try:
            self.show_all_results()
        finally:
            self.result_table.setUpdatesEnabled(True)

    def _new_download_spider(self):
        """每次下载使用独立引擎，避免多线程共用 Session 导致卡死/串台。"""
        from spiders.spider import SpiderEngine
        eng = SpiderEngine()
        eng.download_dir = Path(Config.effective_download_dir())
        return eng

    def _invalidate_detect(self):
        """作废进行中的下载探测（新查询或关闭时调用）。"""
        self._detect_gen = getattr(self, '_detect_gen', 0) + 1
        w = getattr(self, '_detect_worker', None)
        if w is not None:
            try:
                w.finished.disconnect(self.on_download_detection_finished)
            except (TypeError, RuntimeError):
                pass
            if hasattr(w, 'cancel'):
                w.cancel()

    def init_ui(self):
        """初始化UI"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(20, 20, 20, 20)
        
        # 顶部导航栏（固定，跨页面共享）
        header_layout = self.create_header()
        main_layout.addLayout(header_layout)
        
        # 功能页面堆栈（文档管理页在首次切换到该标签时懒创建）
        self.stack = QStackedWidget()
        self.stack.addWidget(self.create_query_page())       # 索引0：标准查询系统
        main_layout.addWidget(self.stack, stretch=1)
        
        # 默认展示标准查询系统
        self.switch_page(0)
        QTimer.singleShot(200, self._refresh_ledger_nav_badge)
    
    def create_header(self):
        """创建顶部导航栏（页面切换 + 使用说明 + 设置）"""
        header = QHBoxLayout()
        header.setSpacing(0)
        
        # 菜单导航按钮：0查询 1公告 2台账 3文档
        self.nav_buttons = []
        nav_query = self._make_nav_button("在线查询", 0)
        nav_ann = self._make_nav_button("国标公告", 1)
        nav_ledger = self._make_nav_button("标准台账", 2)
        nav_doc = self._make_nav_button("本地文档", 3)
        self.nav_buttons.extend([nav_query, nav_ann, nav_ledger, nav_doc])
        self.nav_ledger_btn = nav_ledger
        header.addWidget(nav_query)
        header.addSpacing(8)
        header.addWidget(nav_ann)
        header.addSpacing(8)
        header.addWidget(nav_ledger)
        header.addSpacing(8)
        header.addWidget(nav_doc)
        
        header.addStretch()
        
        help_btn = GlassButton("使用说明", _theme.primary_qcolor())
        help_btn.setMinimumSize(96, 38)
        help_btn.clicked.connect(self.on_standard_help)
        header.addWidget(help_btn)
        header.addSpacing(8)
        
        settings_btn = GlassButton("设置", _theme.primary_qcolor())
        settings_btn.setMinimumSize(84, 38)
        settings_btn.clicked.connect(self.open_settings)
        header.addWidget(settings_btn)

        self._apply_ledger_nav_visibility()
        
        return header
    
    def _make_nav_button(self, text, index):
        """创建单个导航菜单按钮"""
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setCheckable(True)
        btn.clicked.connect(lambda _, i=index: self.switch_page(i))
        return btn
    
    def switch_page(self, index):
        """切换功能页面并刷新导航高亮。
        0=在线查询；1=国标公告；2=标准台账；3=本地文档（后三者懒创建）。
        """
        if index == 2 and not Config.LEDGER_ENABLED:
            self.show_bubble("请先在「设置」中启用标准台账", "warning")
            return
        if index == 1:
            if not getattr(self, '_announcement_page', None):
                from .gb_announcement_page import GbAnnouncementPage
                page = GbAnnouncementPage(get_session=lambda: self._get_ui_spider().session)
                self.stack.addWidget(page)
                self._announcement_page = page
                self._announcement_stack_index = self.stack.count() - 1
            self.stack.setCurrentIndex(self._announcement_stack_index)
        elif index == 2:
            if not getattr(self, '_ledger_page', None):
                from .standard_ledger_page import StandardLedgerPage
                page = StandardLedgerPage()
                self.stack.addWidget(page)
                self._ledger_page = page
                self._ledger_stack_index = self.stack.count() - 1
            # 已创建时不再强制 refresh_all；由台账页 showEvent 按脏标记刷新
            self.stack.setCurrentIndex(self._ledger_stack_index)
            self._refresh_ledger_nav_badge()
        elif index == 3:
            if not getattr(self, 'doc_manager_page', None):
                doc_page = self.create_doc_manager_page()
                self.stack.addWidget(doc_page)
                self.doc_manager_page = doc_page
                self._doc_stack_index = self.stack.count() - 1
            elif not hasattr(self, '_doc_stack_index'):
                self._doc_stack_index = self.stack.indexOf(self.doc_manager_page)
            self.stack.setCurrentIndex(self._doc_stack_index)
        else:
            self.stack.setCurrentIndex(0)
        self._update_nav_style(index)

    def _refresh_ledger_nav_badge(self):
        """标准台账导航文案（不再附带待替代数量，避免影响美观）。"""
        if len(self.nav_buttons) < 3:
            return
        self.nav_buttons[2].setText("标准台账")

    def run_ledger_announcement_match(self, notice_code=''):
        """国标公告更新后：对照最新工程相关公告写入台账待替代。"""
        if not Config.LEDGER_ENABLED:
            return 0
        try:
            page = getattr(self, '_ledger_page', None)
            if page is None:
                from .standard_ledger_page import StandardLedgerPage
                page = StandardLedgerPage()
                self.stack.addWidget(page)
                self._ledger_page = page
                self._ledger_stack_index = self.stack.count() - 1
            n, code = page.match_latest_announcement(
                refresh_ui=page.isVisible()
            )
            if not page.isVisible():
                page.mark_dirty()
            self._refresh_ledger_nav_badge()
            label = notice_code or code or '最新公告'
            if n > 0:
                self.show_bubble(
                    f"已对照「{label}」：命中 {n} 条，请查看标准台账「待替代」",
                    "success",
                )
            else:
                self.show_bubble(
                    f"已对照「{label}」，台账暂无新增待替代",
                    "info",
                )
            logger.info(f"[标准台账] 公告自动对照 {label} 命中 {n}")
            return n
        except Exception as e:
            logger.warning(f"[标准台账] 公告自动对照失败: {e}")
            return 0

    def _apply_ledger_nav_visibility(self):
        """按设置显示/隐藏「标准台账」导航；关闭时若正在台账页则回到在线查询。"""
        btn = getattr(self, 'nav_ledger_btn', None)
        if btn is None and len(getattr(self, 'nav_buttons', []) or []) > 2:
            btn = self.nav_buttons[2]
        if btn is None:
            return
        enabled = bool(Config.LEDGER_ENABLED)
        btn.setVisible(enabled)
        if not enabled:
            # 若当前正停在台账页，切回在线查询
            stack_idx = getattr(self, '_ledger_stack_index', None)
            if stack_idx is not None and self.stack.currentIndex() == stack_idx:
                self.switch_page(0)
            else:
                # 仅刷新角标文案，避免隐藏后仍显示「标准台账（n）」
                self.nav_buttons[2].setText("标准台账")
        else:
            self._refresh_ledger_nav_badge()

    
    def _update_nav_style(self, active_index):
        """根据当前选中项刷新导航按钮视觉反馈"""
        for i, btn in enumerate(self.nav_buttons):
            active = (i == active_index)
            btn.setChecked(active)
            if active:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        padding: 8px 20px;
                        background: {_COLOR_ACCENT};
                        color: #ffffff;
                        border: none;
                        border-radius: 8px;
                        font-size: 15px;
                        font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        padding: 8px 20px;
                        background: transparent;
                        color: {_COLOR_MUTED};
                        border: none;
                        border-radius: 8px;
                        font-size: 15px;
                        font-weight: bold;
                    }}
                    QPushButton:hover {{
                        background: {_COLOR_SOFT};
                        color: {_COLOR_ACCENT};
                    }}
                """)
    
    def create_query_page(self):
        """标准查询系统页面（原主界面内容）"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # 顶部搜索区域
        top_layout = self.create_top_section()
        layout.addLayout(top_layout)
        
        # 进度条显示
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumHeight(20)
        layout.addWidget(self.progress_bar)

        self.progress_label = QLabel("就绪")
        self.progress_label.setStyleSheet(f"""
            font-size: 13px;
            color: {_COLOR_MUTED};
            padding: 2px 4px;
        """)
        layout.addWidget(self.progress_label)
        
        # 结果表格：外层白底圆角卡片 + 内层带边框圆角表（与本地文档双层框一致）
        table_card = QWidget()
        table_card.setStyleSheet("""
            QWidget {
                background: #ffffff;
                border-radius: 12px;
            }
        """)
        table_card_layout = QVBoxLayout(table_card)
        table_card_layout.setContentsMargins(18, 12, 18, 12)
        table_card_layout.setSpacing(0)

        self.create_result_table()
        table_card_layout.addWidget(self.result_table)
        self._apply_card_shadow(table_card)

        layout.addWidget(table_card, stretch=1)
        
        # 底部站点状态
        bottom_layout = self.create_bottom_section()
        layout.addLayout(bottom_layout)
        
        return page
    
    def create_doc_manager_page(self):
        """标准文档管理页面 - 作为本地标准库后台，扫描目录后同步为本地下载源。"""
        self.doc_manager_page = DocumentManagerPage(
            on_directory_changed=self.on_doc_directory_changed
        )
        return self.doc_manager_page

    def on_doc_directory_changed(self, directory):
        """文档管理目录变更回调：同步为局域网/本地下载源并立即生效。

        安全与一致性：扫描目录即同步 Config.LAN_SHARE_DIR，使下载流程的
        _find_in_lan 优先在此目录精确匹配标准编号；同时失效缓存并持久化设置。
        """
        Config.LAN_SHARE_DIR = directory or ""
        # 失效局域网索引缓存，使新目录立即参与精确匹配
        try:
            self._invalidate_lan_cache()
            if Config.LAN_SHARE_DIR:
                self._warm_lan_index_async()
        except Exception:
            pass
        try:
            Config.save_settings()
        except Exception as e:
            logger.warning("保存设置失败: %s", e)
    
    def _apply_card_shadow(self, widget):
        """为卡片添加细微投影，区分模块边界、增强层次感（工程类工具偏稳重）。"""
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(14)
        shadow.setXOffset(0)
        shadow.setYOffset(2)
        shadow.setColor(QColor(20, 50, 80, 26))
        widget.setGraphicsEffect(shadow)

    def create_top_section(self):
        """顶部查询区：单本 / 批量 / 历史 标题可点击切换。"""
        layout = QVBoxLayout()
        layout.setSpacing(10)

        query_card = QWidget()
        query_card.setStyleSheet("""
            QWidget#queryModeCard {
                background: #ffffff;
                border-radius: 12px;
            }
        """)
        query_card.setObjectName("queryModeCard")
        card_layout = QVBoxLayout(query_card)
        card_layout.setContentsMargins(18, 12, 18, 12)
        card_layout.setSpacing(10)

        # 模式切换：单本查询 | 批量查询 | 历史查询
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.query_mode_buttons = []
        btn_single = self._make_query_mode_button("单本查询", 0)
        btn_batch = self._make_query_mode_button("批量查询", 1)
        btn_history = self._make_query_mode_button("历史查询", 2)
        self.query_mode_buttons.extend([btn_single, btn_batch, btn_history])
        mode_row.addWidget(btn_single)
        mode_row.addWidget(btn_batch)
        mode_row.addWidget(btn_history)
        mode_row.addStretch()
        card_layout.addLayout(mode_row)

        self.query_mode_stack = _CurrentPageStack()
        self.query_mode_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

        # 统一控件高度，保证同一行对齐
        _row_h = 34
        _input_style = f"""
            QLineEdit {{
                padding: 6px 12px;
                font-size: 13px;
                border: 1px solid {_COLOR_BORDER};
                border-radius: 8px;
                background: white;
                color: #333333;
                selection-background-color: {_COLOR_PRIMARY};
                selection-color: white;
                min-height: 34px;
            }}
            QLineEdit:focus {{
                border-color: {_COLOR_PRIMARY};
                outline: none;
            }}
            QLineEdit::placeholder {{
                color: {_COLOR_MUTED};
            }}
        """
        _import_style = f"""
            QPushButton {{
                padding: 6px 14px;
                background: {_COLOR_IMPORT_BG};
                color: {_COLOR_PRIMARY};
                border: 1px solid {_COLOR_BORDER};
                border-radius: 8px;
                font-size: 13px;
                font-weight: bold;
                text-align: left;
                min-height: 34px;
            }}
            QPushButton:hover {{
                background: {_COLOR_SOFT};
                border-color: {_COLOR_PRIMARY};
            }}
        """

        # —— 单本：输入框 + 立即查询 + 清除查询 + 下载开关——
        single_page = QWidget()
        single_layout = QVBoxLayout(single_page)
        single_layout.setContentsMargins(0, 4, 0, 0)
        single_layout.setSpacing(8)

        single_row = QHBoxLayout()
        single_row.setSpacing(10)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "输入编号后回车查询；已有缓存时点「强制联网」可重新拉取"
        )
        self.search_input.setStyleSheet(_input_style)
        self.search_input.setMinimumHeight(_row_h)
        self.search_input.returnPressed.connect(self.on_search)
        self.search_input.installEventFilter(_SearchFocusFilter(self.search_input))
        single_row.addWidget(self.search_input, stretch=1)

        search_btn = GlassButton("立即查询", _theme.primary_qcolor())
        search_btn.setFixedHeight(_row_h)
        search_btn.setMinimumWidth(108)
        search_btn.clicked.connect(self.on_search)
        single_row.addWidget(search_btn)

        force_btn = GlassButton("强制联网", _theme.accent_qcolor())
        force_btn.setFixedHeight(_row_h)
        force_btn.setMinimumWidth(96)
        force_btn.setToolTip("忽略 30 天内本地缓存，重新联网查询并覆盖历史")
        force_btn.clicked.connect(self.on_force_search)
        single_row.addWidget(force_btn)

        clear_btn = GlassButton("清除查询", _theme.primary_qcolor())
        clear_btn.setFixedHeight(_row_h)
        clear_btn.setMinimumWidth(96)
        clear_btn.clicked.connect(lambda: self.clear_results(clear_input=True))
        single_row.addWidget(clear_btn)

        self.download_checkbox = GlassButton("", QColor(_COLOR_PRIMARY))
        self.download_checkbox.setCheckable(True)
        self.download_checkbox.setFixedHeight(_row_h)
        self.download_checkbox.setMinimumWidth(100)
        self.download_checkbox.setChecked(self.download_enabled)
        self.download_checkbox.toggled.connect(self.on_download_toggle)
        self._update_download_checkbox_style()
        single_row.addWidget(self.download_checkbox)

        single_layout.addLayout(single_row)
        self.query_mode_stack.addWidget(single_page)

        # —— 批量：导入框 + 批量检索， ——
        batch_page = QWidget()
        batch_layout = QVBoxLayout(batch_page)
        batch_layout.setContentsMargins(0, 4, 0, 0)
        batch_layout.setSpacing(8)

        batch_row = QHBoxLayout()
        batch_row.setSpacing(10)

        self.excel_status_btn = QPushButton(
            "如需批量查询，单击我导入文件 ｜ 支持 Excel、txt，Excel 仅第一列，表头为标准编号"
        )
        self.excel_status_btn.setCursor(Qt.PointingHandCursor)
        self.excel_status_btn.setStyleSheet(_import_style)
        self.excel_status_btn.setMinimumHeight(_row_h)
        self.excel_status_btn.clicked.connect(self.on_batch_import)
        self._batch_import_style = _import_style  # 导入成功后复用同风格
        batch_row.addWidget(self.excel_status_btn, stretch=1)

        self.batch_search_btn = GlassButton("批量检索", _theme.primary_qcolor())
        self.batch_search_btn.setFixedHeight(_row_h)
        self.batch_search_btn.setMinimumWidth(108)
        self.batch_search_btn.clicked.connect(self.on_batch_search)
        self.batch_search_btn.setEnabled(False)
        batch_row.addWidget(self.batch_search_btn)

        batch_layout.addLayout(batch_row)
        self.query_mode_stack.addWidget(batch_page)

        # —— 历史：汇总展示全部查询结果（不再按单条切换）——
        history_page = QWidget()
        history_layout = QVBoxLayout(history_page)
        history_layout.setContentsMargins(0, 4, 0, 0)
        history_layout.setSpacing(0)

        hist_tools = QHBoxLayout()
        hist_tools.setSpacing(8)
        self.history_summary_label = QLabel("暂无历史记录")
        self.history_summary_label.setStyleSheet(
            f"color: {_COLOR_MUTED}; font-size: 13px; padding-left: 4px;"
        )
        hist_tools.addWidget(self.history_summary_label, 1)
        self.history_delete_btn = GlassButton("删除本条", _theme.danger_qcolor())
        self.history_delete_btn.setFixedHeight(_row_h)
        self.history_delete_btn.clicked.connect(self._on_history_delete_selected)
        hist_tools.addWidget(self.history_delete_btn)
        self.history_clear_btn = GlassButton("清空历史", _theme.primary_qcolor())
        self.history_clear_btn.setFixedHeight(_row_h)
        self.history_clear_btn.clicked.connect(self._on_history_clear_all)
        hist_tools.addWidget(self.history_clear_btn)
        history_layout.addLayout(hist_tools)
        # 兼容旧引用
        self.history_list = None
        self.history_combo = None
        self.query_mode_stack.addWidget(history_page)

        card_layout.addWidget(self.query_mode_stack)
        self._apply_card_shadow(query_card)
        layout.addWidget(query_card)

        # 兼容旧引用名（若别处仍用 batch_panel）
        self.batch_panel = batch_page
        self.switch_query_mode(0)
        return layout

    def _make_query_mode_button(self, text, index):
        """单本/批量/历史模式切换按钮。"""
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setCheckable(True)
        btn.clicked.connect(lambda _, i=index: self.switch_query_mode(i))
        return btn

    def switch_query_mode(self, index):
        """切换单本 / 批量 / 历史查询面板。"""
        if not hasattr(self, "query_mode_stack"):
            return
        bufs = getattr(self, "_mode_result_bufs", None)
        if bufs is not None:
            old = bufs.active
            # 离开单本/历史时各自存快照；批量有独立占位逻辑，不写入这两个缓冲
            if old in (bufs.SINGLE, bufs.HISTORY):
                bufs.save(old, self.search_results)
            elif old == 1:
                # 离开批量：保留批量结果，避免切到历史后再回来仍显示历史表
                self._batch_result_snapshot = list(self.search_results or [])
        self.query_mode_stack.setCurrentIndex(index)
        self.query_mode_stack.updateGeometry()
        if bufs is not None:
            bufs.active = index
        if index == 2:
            # 历史模式：始终从缓存汇总全部结果（不按单条切换）
            self._show_all_history_results()
        elif index == 0:
            self._restore_mode_results(0)
        elif index == 1:
            self._restore_batch_view()
        for i, btn in enumerate(self.query_mode_buttons):
            active = (i == index)
            btn.setChecked(active)
            if active:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        padding: 6px 16px;
                        background: transparent;
                        color: {_COLOR_PRIMARY};
                        border: none;
                        border-bottom: 2px solid {_COLOR_ACCENT};
                        border-radius: 0;
                        font-size: 16px;
                        font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        padding: 6px 16px;
                        background: transparent;
                        color: {_COLOR_MUTED};
                        border: none;
                        border-bottom: 2px solid transparent;
                        border-radius: 0;
                        font-size: 16px;
                        font-weight: bold;
                    }}
                    QPushButton:hover {{
                        color: {_COLOR_PRIMARY};
                    }}
                """)
    
    def create_result_table(self):
        """创建结果表格"""
        self.result_table = QTableWidget()
        self.result_table.setColumnCount(7)
        self.result_table.setHorizontalHeaderLabels([
            "序号", "标准编号", "标准名称", "标准状态", "发布日期",
            "实施日期", "PDF操作"
        ])
        
        # 表格样式（与本地文档统一：圆角边框 + 首末列表头圆角）
        self.result_table.setStyleSheet(f"""
            QTableWidget {{
                background: #ffffff;
                border: 1px solid {_COLOR_BORDER};
                border-radius: 12px;
                gridline-color: #EEF2F7;
                font-size: 13px;
                font-family: "Microsoft YaHei";
                color: #333333;
                selection-background-color: {_COLOR_SOFT};
                selection-color: #333333;
            }}
            QTableWidget::item {{
                padding: 8px 10px;
                border-bottom: 1px solid #e8eef3;
            }}
            QTableWidget::item:selected {{
                background: {_COLOR_SOFT};
                color: #333333;
            }}
            QTableWidget::item:alternate {{
                background: #f7fafc;
            }}
            QHeaderView::section {{
                background: {_COLOR_PRIMARY};
                color: white;
                padding: 12px;
                border: none;
                border-right: 1px solid rgba(255,255,255,100);
                font-weight: bold;
                font-size: 13px;
                font-family: "Microsoft YaHei";
            }}
            QHeaderView::section:first {{
                border-top-left-radius: 12px;
            }}
            QHeaderView::section:last {{
                border-top-right-radius: 12px;
                border-right: none;
            }}
            QTableCornerButton::section {{
                background: {_COLOR_PRIMARY};
                border: none;
                border-top-left-radius: 12px;
            }}
        """)
        
        header = self.result_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        self.result_table.setColumnWidth(0, 52)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        self.result_table.setColumnWidth(1, 200)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.Fixed)
        self.result_table.setColumnWidth(6, 120)
        
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setShowGrid(False)
        self.result_table.setWordWrap(True)
        self.result_table.setMouseTracking(True)
        self.result_table.cellEntered.connect(self._on_table_cell_entered)
        self._hovered_row = -1
        self._pdf_col = 6  # PDF 操作列索引（悬停同步背景用）
        
        self.result_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.result_table.customContextMenuRequested.connect(self._on_table_context_menu)
        self.result_table.cellDoubleClicked.connect(self._on_table_cell_double_clicked)
    
    def create_bottom_section(self):
        """创建底部站点状态区域"""
        layout = QHBoxLayout()
        layout.setSpacing(6)
        
        self.site_status_caption = QLabel("站点状态")
        self.site_status_caption.setStyleSheet(
            f"font-size: 12px; color: {_COLOR_MUTED}; padding-right: 4px;"
        )
        layout.addWidget(self.site_status_caption)
        
        self.status_dots = []
        for site in Config.PRIMARY_SITES:
            full = site.get('name', '')
            # 仅圆点，悬停显示完整站名
            dot = StatusDot("")
            if full:
                dot.setToolTip(full)
            self.status_dots.append(dot)
            layout.addWidget(dot)
        
        layout.addStretch()
        
        self.stop_search_btn = GlassButton("停止查询", _theme.danger_qcolor())
        self.stop_search_btn.setMinimumSize(96, 36)
        self.stop_search_btn.setVisible(False)
        self.stop_search_btn.clicked.connect(self.on_stop_search)
        layout.addWidget(self.stop_search_btn)
        
        export_excel_btn = GlassButton("导出结果", _theme.primary_qcolor())
        export_excel_btn.setMinimumSize(96, 36)
        export_excel_btn.clicked.connect(self.export_results)
        layout.addWidget(export_excel_btn)
        
        return layout
    
    def deduplicate_results(self, results):
        """去重：过滤脏数据后，按完整标准编号去重；重复时用后到结果补全空字段。
        同时检查替代关系：如果某标准的替代标准已经在结果列表中（作为独立查询结果），
        则移除该替代关系，避免重复显示。"""
        seen = {}
        unique_results = []
        for result in results:
            if not self.is_valid_result(result):
                continue
            std_no = (result.std_no or "").strip()
            if not std_no:
                continue

            if std_no in seen:
                from spiders.spider import SpiderEngine
                SpiderEngine._fill_missing_std_fields(seen[std_no], result)
                continue
            seen[std_no] = result
            unique_results.append(result)
        
        # 检查替代关系：如果某标准的替代标准已经在结果列表中，则移除替代关系
        for r in unique_results:
            replacement = getattr(r, "replacement", None)
            if replacement is not None:
                repl_no = (replacement.std_no or "").strip()
                if repl_no in seen:
                    r.replacement = None
                    r.no_replacement = False
        
        return unique_results
    
    def remove_redundant_replacements(self, results):
        """批量查询后处理：如果替代标准本身也在导入数据中，则移除替代行，
        只保留原标准显示为废止，替代标准由自己的查询结果展示。
        如果导入数据中只有原标准没有替代标准，则保留替代行。"""
        # 收集所有独立查询的标准编号（非替代行）
        direct_std_nos = set()
        for r in results:
            direct_std_nos.add((r.std_no or "").strip())
        
        # 遍历结果，如果某标准有替代行，且替代标准也在直接查询结果中，则移除替代行
        for r in results:
            replacement = getattr(r, "replacement", None)
            if replacement is not None:
                repl_no = (replacement.std_no or "").strip()
                if repl_no in direct_std_nos:
                    r.replacement = None
                    r.no_replacement = False
        
        return results
    
    def is_valid_result(self, result):
        """校验是否为有效标准，过滤解析器误报的脏数据（如把页面导航文本当成ISO9001）"""
        from spiders.spider import SpiderEngine
        if not SpiderEngine._is_plausible_result(result):
            return False
        # 屏蔽特定状态的标准（如"正在征求意见"）
        status = (result.status or "").strip()
        if any(blocked in status for blocked in ("正在征求意见",)):
            return False
        return True
    
    def on_search_finished(self, results):
        """搜索完成（包括正常完成和取消后返回部分结果）"""
        if not self._is_current_search_worker():
            return
        try:
            self._flush_pending_table_timers()
            self._stop_single_progress_anim()
            self.progress_bar.setValue(100)
            self.search_input.setEnabled(True)
            self.progress_bar.setVisible(False)
            self.stop_search_btn.setVisible(False)
            was_cancelled = bool(getattr(self, '_search_cancelled', False))
            
            if results:
                self.search_results = self.deduplicate_results(results)
                bufs = getattr(self, "_mode_result_bufs", None)
                if bufs is not None:
                    bufs.save(bufs.SINGLE, self.search_results)
                self.show_all_results()

                # 检查是否是取消后返回的结果
                if was_cancelled:
                    self.progress_label.setText(
                        f'<span style="color:{_theme.WARNING}; font-weight:bold;">!</span>  检索已停止，已找到 {len(self.search_results)} 条结果'
                    )
                    self._search_cancelled = False
                else:
                    self.progress_label.setText(
                        f'<span style="color:{_theme.SUCCESS}; font-weight:bold;">✓</span>  检索完成，找到 {len(self.search_results)} 条结果'
                    )

                # ★ 结果返回后自动执行本地搜索（独立线程，不阻塞 UI）
                #   本地命中 → 显示「本地打开」按钮（不受下载开关控制）
                #   本地未命中 → 保留网络下载按钮（受下载开关控制）
                self._start_local_search()
            else:
                self.search_results = []
                bufs = getattr(self, "_mode_result_bufs", None)
                if bufs is not None:
                    bufs.save(bufs.SINGLE, [])
                self.result_table.setRowCount(0)
                
                if was_cancelled:
                    self.progress_label.setText("检索已停止，未找到相关标准")
                    self._search_cancelled = False
                else:
                    self.progress_label.setText("未找到相关标准")
                    self.show_bubble("未找到相关标准", "warning")

            # 单本/汉字：写入历史（取消不写，避免半成品污染缓存）
            pending = getattr(self, '_pending_history', None)
            if pending and not was_cancelled:
                try:
                    store = self._get_history_store()
                    store.upsert(
                        pending.get('text') or '',
                        pending.get('kind') or 'std_no',
                        list(self.search_results or []),
                        source='network',
                    )
                    self._reload_history_list()
                except Exception as he:
                    logger.warning(f"[查询历史] 写入失败: {he}")
        except Exception as e:
            error_msg = f"搜索结果处理异常: {str(e)}"
            logger.error(error_msg)
            self.show_bubble(error_msg, "error")

    def _start_local_search(self):
        """查询结果返回后自动启动本地搜索线程：在文档管理目录中匹配本地文件。
        本地命中只影响「本地打开」按钮，不受下载开关控制。"""
        if not self.search_results:
            return
        # 取消上一轮本地搜索（quit() 对无事件循环的 run 无效，改用协作式 cancel）
        old = getattr(self, '_local_search_worker', None)
        if old is not None:
            try:
                old.finished.disconnect(self.on_local_search_finished)
            except (TypeError, RuntimeError):
                pass
            if old.isRunning():
                old.cancel()
                old.wait(500)
        self._local_search_worker = LocalSearchWorker(self._get_local_spider(), self.search_results)
        self._local_search_worker.finished.connect(self.on_local_search_finished)
        self._local_search_running = True
        self._local_search_worker.start()

    def on_local_search_finished(self, hit_std_nos):
        """本地搜索完成：记录 is_local / local_file_path 数据。
        下载功能未开启时不刷新按钮、不弹气泡（保持「未开启」外观）；
        但若用户在本地搜索完成前就已开启下载功能，则此刻立即重渲染当前页，
        使本地命中的行从「下载」切换为「本地打开」，并对未命中行启动按需下载源探测，
        修复异步时序缺口（探测须待本地搜索完成，避免误标本地命中行）。"""
        self._local_search_running = False
        if hit_std_nos:
            logger.info(f"[本地搜索] 完成，命中 {len(hit_std_nos)} 条本地标准")
        # 时序补偿：下载已开启时立即刷新，避免命中先于开关或晚于开关都能正确显示「本地打开」
        if getattr(self, "download_enabled", False):
            self.show_all_results()
            self._start_download_detection()

    def show_all_results(self):
        """显示所有查询结果"""
        if not self.search_results:
            self.result_table.setRowCount(0)
            return
        self.update_table(self.search_results, 0)
    
    def on_download_toggle(self, state):
        """切换单本查询下载功能开关，并即时刷新当前结果的下载按钮状态与勾选样式。
        开启开关时对当前所有非本地、非终态行整批联网探测下载源（懒加载）；
        若本地搜索尚未结束，则推迟到 on_local_search_finished 再触发，避免误标本地命中行。"""
        self.download_enabled = self.download_checkbox.isChecked()
        self._update_download_checkbox_style()
        self.show_all_results()
        if self.download_enabled and not self._local_search_running:
            self._start_download_detection()

    def _needs_detection(self, std):
        """判断某标准是否需要联网探测下载源：跳过本地命中、废止/作废、以及已处于
        终态或检测中的行；仅对「未检测」的现行非本地标准探测，避免重复联网。"""
        if getattr(std, "is_local", False):
            return False
        status = std.status or ""
        if ("废止" in status) or ("作废" in status):
            return False
        if std.download_status in ("可下载", "详情页", "不启用", "未找到资源", "检测中"):
            return False
        return True

    def _start_download_detection(self):
        """开启下载功能后，对当前所有需要探测的行整批联网探测下载源（懒加载）。
        先把目标行标记为「检测中」并即时重绘（蓝色流光占位），再启动后台线程；
        已探测过的行状态固化，反复开关不重复联网；用 isRunning 防重复启动。
        检索进行中则短延迟重试，避免与主 spider 检索抢资源。"""
        if not self.search_results:
            return
        # 检索仍在跑：推迟探测（本地搜已由 on_local_search_finished 串行）
        sw = getattr(self, 'search_worker', None)
        if sw is not None and sw.isRunning():
            QTimer.singleShot(400, self._start_download_detection)
            return
        targets = []
        for std in self.search_results:
            cands = [std]
            r = getattr(std, "replacement", None)
            if r is not None:
                cands.append(r)
            for c in cands:
                if self._needs_detection(c):
                    c.download_status = "检测中"
                    targets.append(c)
        if not targets:
            return
        self.show_all_results()
        if getattr(self, "_detect_worker", None) and self._detect_worker.isRunning():
            return
        self._detect_gen = getattr(self, '_detect_gen', 0) + 1
        gen = self._detect_gen
        self._detect_worker = DownloadCheckWorker(self._get_detect_spider(), self.search_results)
        self._detect_worker.gen = gen
        self._detect_worker.finished.connect(self.on_download_detection_finished)
        self._detect_worker.start()

    def on_download_detection_finished(self):
        """按需探测完成：重绘当前页，将「检测中」固化为「可下载」或「未找到资源」。"""
        w = self.sender()
        if w is not None and getattr(w, 'gen', None) != getattr(self, '_detect_gen', None):
            return
        self.show_all_results()

    def _update_download_checkbox_style(self):
        """下载开关短文案：关=主色，开=高亮。"""
        if self.download_checkbox.isChecked():
            self.download_checkbox.setText("下载：开")
            self.download_checkbox.setBaseColor(QColor(_COLOR_ACCENT))
        else:
            self.download_checkbox.setText("下载：关")
            self.download_checkbox.setBaseColor(QColor(_COLOR_PRIMARY))
    
    def apply_theme(self):
        """应用主题：主色/高亮/中性三档蓝 + 纯色底。"""
        self.setStyleSheet(f"""
            QMainWindow {{
                background: {_COLOR_BG};
            }}
            QLabel {{
                color: #333333;
            }}
            QScrollBar:vertical {{
                width: 10px;
                background: #f0f0f0;
                border-radius: 5px;
            }}
            QScrollBar::handle:vertical {{
                background: {_COLOR_BORDER};
                border-radius: 5px;
                min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {_COLOR_PRIMARY};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar:horizontal {{
                height: 10px;
                background: #f0f0f0;
                border-radius: 5px;
            }}
            QScrollBar::handle:horizontal {{
                background: {_COLOR_BORDER};
                border-radius: 5px;
                min-width: 30px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: {_COLOR_PRIMARY};
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0;
            }}
        """)
    
    def on_standard_help(self):
        """标准查询系统使用说明"""
        help_text = """
<h3>一、在线查询</h3>
<ul>
<li><b>单本查询</b>：输入标准编号（如 GB/T 1345-2005）或名称关键词，点「立即查询」或回车。</li>
<li><b>立即查询与强制联网</b>：
  <ul>
    <li>「立即查询」：若该词在 <b>30 天内</b>已查过，会直接显示本地历史缓存（不联网），速度更快。</li>
    <li>「强制联网」：<b>忽略本地缓存</b>，重新上网查询，并用最新结果覆盖历史。适合：上次结果不全（如缺发布日期）、怀疑有新版本、或刚改过检索逻辑后要重查。</li>
  </ul>
</li>
<li><b>批量查询</b>：导入 Excel/文本（第一列为标准编号）→「批量检索」→ 可「导出结果」。（批量不写入历史缓存）</li>
<li><b>历史查询</b>：自动汇总近期单本/汉字查询结果；可选中行「删除本条」或「清空历史」。删除某条后，再对该词「立即查询」也会重新联网。</li>
<li>结果中废止标准为红色；若有替代版本，会紧挨显示在下一行。</li>
<li>下载：先点「点击开启下载功能」，再点行内「下载」。目录可在「设置」中修改。</li>
<li>右键结果行可将标准「加入标准台账」（需在设置中启用台账）。</li>
</ul>

<h3>二、国标公告</h3>
<ul>
<li>展示含工程相关标准的最新国标公告列表。</li>
<li>点「检查更新」：有新公告且含工程相关内容时，会<strong>自动对照标准台账</strong>，命中项写入「待替代」。</li>
<li>点选左侧公告可查看代替关系明细；「打开原文 / 打开官网」可看网页。</li>
</ul>

<h3>三、标准台账</h3>
<p>（需在「设置」中启用「标准台账」后显示）</p>
<ul>
<li><b>导入台账</b>：支持 .xlsx / .xls / .csv；首次导入后会自动对照上一年6月之后至最新的全部国标公告一次。</li>
<li><b>全部标准</b>：勾选后点「标准查新」联网更新状态；若发现替代版，新标准插在旧标准下一行（旧行标红）。</li>
<li><b>待替代</b>：尚未到实施日的代替关系（如 GB/T 1345-2005 → GB/T 1345-2026）</li>
<li><b>已归档变更</b>：已作废/已实施的代替记录（如 GB/T 6096-2009 → GB/T 6096-2020）</li>
<li>表头可拖动调整列宽。</li>
</ul>

<h3>四、本地文档</h3>
<ul>
<li>管理本机/局域网目录中的标准 PDF，可按编号检索并打开。</li>
<li>在线查询命中本地文件时，可直接「本地打开」。</li>
</ul>

<h3>五、设置</h3>
<ul>
<li>下载保存目录、局域网文档目录、是否启用标准台账等。</li>
</ul>

<h3>六、注意</h3>
<ul>
<li>查询依赖网络；批量查询时请保持网络畅通，可随时「停止」。</li>
<li>编号尽量写完整（如 GB/T 18242-2025），少用纯数字，命中更准。</li>
<li>部分标准网站可能无 PDF，下载失败属正常情况。</li>
<li>看到日志「命中本地缓存」却觉得结果过时或不完整时：在单本查询点「强制联网」即可。</li>
</ul>
"""
        
        dialog = QDialog(self)
        dialog.setWindowTitle("标准查询系统 - 使用说明")
        dialog.setMinimumWidth(750)
        dialog.setMinimumHeight(550)
        dialog.setStyleSheet("QDialog { background-color: white; }")
        
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # 标题
        title = QLabel("使用说明")
        title.setStyleSheet(f"""
            QLabel {{
                font-size: 18px;
                font-weight: bold;
                color: {_COLOR_PRIMARY};
                padding: 10px;
                border-bottom: 2px solid #e0e0e0;
            }}
        """)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        # 内容区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea {
                border: 1px solid #ddd;
                border-radius: 8px;
                background: white;
            }
            QScrollBar:vertical {
                border: none;
                background: #f0f0f0;
                width: 10px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: #b0b0b0;
                border-radius: 5px;
                min-height: 30px;
            }
        """)
        
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(15, 15, 15, 15)
        
        text_label = QLabel(help_text)
        text_label.setWordWrap(True)
        text_label.setTextFormat(Qt.RichText)
        text_label.setStyleSheet(f"""
            QLabel {{
                font-size: 13px;
                color: #333;
                line-height: 1.6;
            }}
            QLabel h2 {{
                font-size: 16px;
                color: {_COLOR_PRIMARY};
                margin-top: 15px;
                margin-bottom: 10px;
            }}
            QLabel h3 {{
                font-size: 14px;
                color: {_COLOR_PRIMARY};
                margin-top: 12px;
                margin-bottom: 8px;
            }}
            QLabel h4 {{
                font-size: 13px;
                color: #333;
                font-weight: bold;
                margin-top: 10px;
                margin-bottom: 5px;
            }}
            QLabel ul {{
                margin-left: 20px;
            }}
            QLabel li {{
                margin-bottom: 5px;
            }}
            QLabel p {{
                margin-bottom: 8px;
            }}
        """)
        content_layout.addWidget(text_label)
        
        scroll.setWidget(content)
        layout.addWidget(scroll)
        
        # 关闭按钮
        close_btn = QPushButton("关闭")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                padding: 8px 30px;
                background: {_COLOR_PRIMARY};
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: {_theme.PRIMARY_HOVER};
            }}
            QPushButton:pressed {{
                background: {_theme.PRIMARY_PRESSED};
            }}
        """)
        close_btn.clicked.connect(dialog.accept)
        
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        dialog.exec_()
    
    def on_force_search(self):
        """忽略本地历史缓存，强制联网查询并覆盖该词条。"""
        self._force_history_refresh = True
        self.on_search()

    def on_search(self):
        """执行搜索（优先读 30 天内历史缓存）。"""
        if self._is_batch_search_running():
            self.show_bubble("批量检索进行中，请先停止后再查询", "warning")
            return
        keyword = self.search_input.text().strip()
        if not keyword:
            self.show_bubble("请输入检索关键词", "warning")
            return
        
        # 标记已执行查询（阻止延迟自动更新检测）
        self._search_executed = True
        
        # 重置取消标志
        self._search_cancelled = False
        self._invalidate_detect()

        from spiders.chinese_name_query import is_chinese_name_query
        kind = 'chinese_name' if is_chinese_name_query(keyword) else 'std_no'
        self._pending_history = {'text': keyword, 'kind': kind}
        force = bool(getattr(self, '_force_history_refresh', False))
        self._force_history_refresh = False

        if not force:
            entry = self._get_history_store().get_fresh(keyword, kind)
            if entry:
                logger.info(f"[查询历史] 命中本地缓存: {keyword}")
                self.clear_results()
                self._apply_history_entry(entry, bump=True, label_prefix='缓存命中')
                return
        else:
            logger.info(f"[查询历史] 强制联网刷新: {keyword}")
            self.show_bubble("正在强制联网查询（忽略本地缓存）…", "info")
        
        # 清空之前的结果
        self.clear_results()
        bufs = getattr(self, "_mode_result_bufs", None)
        if bufs is not None:
            bufs.clear_single()
        
        # 禁用搜索按钮
        self.search_input.setEnabled(False)
        
        # 显示进度条（信号驱动：蜘蛛实时消息→百分比）
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        
        # 显示停止按钮
        self.stop_search_btn.setVisible(True)
        
        # 重置站点状态
        self._reset_site_dots()
        self.progress_label.setText(f"正在查询 1/1：{keyword}")
        
        # 断开旧线程信号并取消（不阻塞 UI）；旧线程仍在跑则换新 spider 避免数据竞争
        if self._retire_search_worker():
            logger.warning("上一搜索线程尚未退出，创建新的 spider 实例")
            from spiders.spider import SpiderEngine
            self._spider = SpiderEngine()
        
        # 创建工作线程（单本查询不再在查询时联网探测下载源，改为开启下载开关后按需触发）
        self._search_gen = getattr(self, '_search_gen', 0) + 1
        self.search_worker = SearchWorker(self._get_spider(), keyword, check_download=False)
        self.search_worker.gen = self._search_gen
        self.search_worker.progress.connect(self.update_progress)
        self.search_worker.site_status.connect(self.update_site_status)
        self.search_worker.finished.connect(self.on_search_finished)
        self.search_worker.error.connect(self.on_search_error)
        self.search_worker.cancelled.connect(self.on_search_cancelled)
        self.search_worker.result_found.connect(self.on_result_found)
        self.search_worker.start()
    
    def _stop_single_progress_anim(self):
        """停止单本查询走动进度条。"""
        if getattr(self, '_single_progress_timer', None) is not None:
            self._single_progress_timer.stop()
    
    def on_result_found(self, results):
        """接收到增量搜索结果，立即显示"""
        if not self._is_current_search_worker():
            return
        if not results:
            return
        
        # 将新结果添加到搜索结果列表
        for result in results:
            if self.is_valid_result(result):
                self.search_results.append(result)
        
        # 去重并更新显示（防抖：多站增量/与 finished 叠刷时避免反复全表重建）
        self.search_results = self.deduplicate_results(self.search_results)
        self._schedule_single_table_refresh()
        
        # 更新进度标签显示已找到的数量
        self.progress_label.setText(
            f'正在检索... 已找到 {len(self.search_results)} 条结果'
        )

    def _schedule_single_table_refresh(self):
        """单本检索增量结果防抖刷新。"""
        if getattr(self, '_single_table_timer', None) is None:
            self._single_table_timer = QTimer(self)
            self._single_table_timer.setSingleShot(True)
            self._single_table_timer.setInterval(200)
            self._single_table_timer.timeout.connect(self._on_single_table_refresh)
        self._single_table_timer.start(200)

    def _flush_pending_table_timers(self):
        """取消挂起的表格防抖，避免 finished 后再刷一次。"""
        for attr in ('_batch_table_timer', '_single_table_timer'):
            t = getattr(self, attr, None)
            if t is not None and t.isActive():
                t.stop()

    def _on_single_table_refresh(self):
        if not self.search_results:
            return
        self.result_table.setUpdatesEnabled(False)
        try:
            self.show_all_results()
        finally:
            self.result_table.setUpdatesEnabled(True)
    
    def on_search_cancelled(self):
        """搜索被用户取消 - 立即更新 UI 状态"""
        if not self._is_current_search_worker():
            return
        self._search_cancelled = True
        self._stop_single_progress_anim()
        self.search_input.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.stop_search_btn.setVisible(False)
        
        # 如果已有部分结果，立即显示
        if self.search_results:
            self.show_all_results()
            self.progress_label.setText(
                f'<span style="color:{_theme.WARNING}; font-weight:bold;">!</span>  检索已停止，已找到 {len(self.search_results)} 条结果'
            )
        else:
            self.progress_label.setText("正在停止...")
        
        self.show_bubble("查新已停止", "warning")
    
    def on_search_error(self, error_msg):
        """搜索错误处理；若有旧历史则回退展示。"""
        if not self._is_current_search_worker():
            return
        self._stop_single_progress_anim()
        self.search_input.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.stop_search_btn.setVisible(False)
        pending = getattr(self, '_pending_history', None)
        if pending:
            stale = self._get_history_store().get_entry(pending['text'], pending['kind'])
            if stale:
                self._apply_history_entry(stale, bump=False, label_prefix='刷新失败，沿用缓存')
                self.show_bubble("缓存已过期，刷新失败，已显示旧结果", "warning")
                self.progress_label.setText("搜索出错，已显示历史缓存")
                return
        self.show_bubble(error_msg, "error")
        self.progress_label.setText("搜索出错")

    def _get_history_store(self):
        if not getattr(self, '_history_store', None):
            from query_history import QueryHistoryStore
            self._history_store = QueryHistoryStore()
        return self._history_store

    def _get_batch_cache_store(self):
        """批量检索独立缓存，避免污染单本查询历史。"""
        store = getattr(self, '_batch_cache_store', None)
        if store is None:
            from app_env import DATA_DIR
            from query_history.store import QueryHistoryStore
            store = QueryHistoryStore(
                path=os.path.join(DATA_DIR, 'batch_query_cache.json'),
                ttl_days=30,
                max_entries=500,
            )
            self._batch_cache_store = store
        return store

    def _load_batch_cached_results(self, keyword):
        """命中 30 天批量缓存时返回 StandardInfo 列表（统一标记不启用下载）；否则 None。"""
        try:
            store = self._get_batch_cache_store()
            entry = store.get_fresh(keyword, 'std_no')
            if not entry:
                return None
            results = store.dicts_to_results(entry.get('results') or [])
            if not results:
                return None
            results = store.enrich_from_announcement_cache(results)
            results = store.apply_local_status_fix(results)
            for std in results:
                std.download_status = "不启用"
                replacement = getattr(std, "replacement", None)
                if replacement is not None:
                    replacement.download_status = "不启用"
            return results
        except Exception as e:
            logger.warning("[批量缓存] 读取失败: %s", e)
            return None

    def _apply_history_entry(self, entry, bump=False, label_prefix=''):
        """将历史条目结果填入结果表（公告补充 + 本地状态校正）。"""
        store = self._get_history_store()
        results = store.dicts_to_results(entry.get('results') or [])
        results = store.enrich_from_announcement_cache(results)
        results = store.apply_local_status_fix(results)
        if bump:
            store.upsert(
                entry.get('query_text') or '',
                entry.get('query_kind') or 'std_no',
                results,
                source='cache',
            )
            self._reload_history_list()
        self._history_view_key = entry.get('query_key')
        self.search_input.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.stop_search_btn.setVisible(False)
        self._stop_single_progress_anim()
        if results:
            self.search_results = self.deduplicate_results(results)
            bufs = getattr(self, "_mode_result_bufs", None)
            if bufs is not None:
                bufs.save(bufs.HISTORY, self.search_results)
                # 单本模式缓存命中时，同步写入单本缓冲
                if bufs.active == bufs.SINGLE:
                    bufs.save(bufs.SINGLE, self.search_results)
            self.show_all_results()
            prefix = label_prefix or '历史'
            self.progress_label.setText(
                f'<span style="color:{_theme.SUCCESS}; font-weight:bold;">✓</span>  '
                f'{prefix}，找到 {len(self.search_results)} 条结果'
            )
            self._start_local_search()
        else:
            self.search_results = []
            bufs = getattr(self, "_mode_result_bufs", None)
            if bufs is not None:
                bufs.save(bufs.HISTORY, [])
                if bufs.active == bufs.SINGLE:
                    bufs.save(bufs.SINGLE, [])
            self.result_table.setRowCount(0)
            self.progress_label.setText(f"{label_prefix or '历史'}：未找到相关标准")

    def _reload_history_list(self):
        """刷新历史汇总文案；若当前在历史模式则重绘全部结果。"""
        store = self._get_history_store()
        entries = store.list_entries()
        label = getattr(self, 'history_summary_label', None)
        if label is not None:
            if not entries:
                label.setText("暂无历史记录")
            else:
                label.setText(f"历史汇总 · {len(entries)} 次查询")
        bufs = getattr(self, "_mode_result_bufs", None)
        if bufs is not None and bufs.active == bufs.HISTORY:
            self._show_all_history_results()

    def _std_no_key(self, std_no):
        return ((std_no or "").strip().replace(" ", "").replace("\u3000", "").upper())

    def _show_all_history_results(self):
        """汇总全部历史条目的结果到同一张表（按编号去重，新查询优先）。"""
        if not getattr(self, "result_table", None):
            return
        store = self._get_history_store()
        entries = store.list_entries()
        label = getattr(self, 'history_summary_label', None)

        if not entries:
            self.search_results = []
            bufs = getattr(self, "_mode_result_bufs", None)
            if bufs is not None:
                bufs.save(bufs.HISTORY, [])
            self.result_table.setRowCount(0)
            if label is not None:
                label.setText("暂无历史记录")
            self.progress_label.setText("暂无历史记录")
            return

        merged = []
        seen = set()
        for entry in entries:
            results = store.dicts_to_results(entry.get('results') or [])
            results = store.enrich_from_announcement_cache(results)
            results = store.apply_local_status_fix(results)
            for r in results:
                key = self._std_no_key(getattr(r, 'std_no', None))
                if not key or key in seen:
                    continue
                seen.add(key)
                # 记录来源历史条目，供「删除本条」精确定位
                qkey = entry.get('query_key') or ''
                setattr(r, '_history_query_key', qkey)
                # 预占替代编号，避免后续历史条目再把替代标准当独立主行插入
                repl = getattr(r, 'replacement', None)
                if repl is not None:
                    rk = self._std_no_key(getattr(repl, 'std_no', None))
                    if rk:
                        seen.add(rk)
                    setattr(repl, '_history_query_key', qkey)
                merged.append(r)

        merged = self.deduplicate_results(merged)
        merged = self.remove_redundant_replacements(merged)
        self.search_results = merged
        bufs = getattr(self, "_mode_result_bufs", None)
        if bufs is not None:
            bufs.save(bufs.HISTORY, self.search_results)

        if label is not None:
            label.setText(f"历史汇总 · {len(entries)} 次查询")

        if merged:
            self.show_all_results()
            self.progress_label.setText(
                f'<span style="color:{_theme.SUCCESS}; font-weight:bold;">✓</span>  '
                f'历史汇总，{len(entries)} 次查询，共 {len(merged)} 条标准'
            )
            self._start_local_search()
        else:
            self.result_table.setRowCount(0)
            self.progress_label.setText(
                f'历史汇总，{len(entries)} 次查询，暂无标准结果'
            )

    def _show_latest_history_results(self):
        """兼容旧调用：改为汇总全部历史。"""
        self._show_all_history_results()

    def _selected_history_entry(self):
        return None

    def _on_history_combo_changed(self, index):
        return

    def _on_history_item_clicked(self, item):
        return

    def _on_history_refresh_selected(self):
        self.show_bubble("请到「单本查询」输入编号后点「强制联网」", "warning")

    def _on_history_delete_selected(self):
        """删除表格当前选中行对应的那一条历史查询记录。"""
        bufs = getattr(self, "_mode_result_bufs", None)
        if bufs is None or bufs.active != bufs.HISTORY:
            self.show_bubble("请在「历史查询」中操作", "warning")
            return
        table = getattr(self, "result_table", None)
        if table is None:
            return
        row = table.currentRow()
        if row < 0:
            self.show_bubble("请先在表格中选中要删除的一行", "warning")
            return
        item = table.item(row, 1)
        if item is None:
            self.show_bubble("无法识别选中行", "warning")
            return
        data = item.data(Qt.UserRole) or {}
        qkey = (data.get("history_query_key") or "").strip()
        std_no = (data.get("std_no") or item.text() or "").strip()
        store = self._get_history_store()

        if not qkey and std_no:
            # 兜底：按编号在历史结果中反查 query_key
            target = self._std_no_key(std_no)
            for e in store.list_entries():
                if self._std_no_key(e.get("query_text") or e.get("query_key")) == target:
                    qkey = e.get("query_key") or ""
                    break
                for d in e.get("results") or []:
                    if isinstance(d, dict) and self._std_no_key(d.get("std_no")) == target:
                        qkey = e.get("query_key") or ""
                        break
                    repl = d.get("replacement") if isinstance(d, dict) else None
                    if isinstance(repl, dict) and self._std_no_key(repl.get("std_no")) == target:
                        qkey = e.get("query_key") or ""
                        break
                if qkey:
                    break

        if not qkey:
            self.show_bubble("未找到对应历史记录", "warning")
            return

        label = std_no or qkey
        if not ask_yes_no(self, "删除本条", f"确定删除历史记录「{label}」？"):
            return
        n = store.delete([qkey])
        self._show_all_history_results()
        if n:
            self.show_bubble(f"已删除：{label}", "success")
        else:
            self.show_bubble("未删除任何记录（可能已不存在）", "warning")

    def _on_history_clear_all(self):
        if not ask_yes_no(self, "清空历史", "确定清空全部查询历史？"):
            return
        n = self._get_history_store().clear()
        self._history_view_key = None
        bufs = getattr(self, "_mode_result_bufs", None)
        if bufs is not None:
            bufs.clear_history_view()
        self._show_all_history_results()
        self.show_bubble(f"已清空 {n} 条历史", "success")
    
    def on_batch_search(self):
        """批量检索"""
        if not self.batch_data:
            self.show_bubble("请先导入文件", "warning")
            return

        # 标记已执行查询（阻止延迟自动更新检测）
        self._search_executed = True

        # 不清空 search_results：保留导入预览的占位行(标准编号在位)，
        # 检索完成后逐条原地替换占位行为真实结果，避免表格重建导致编号消失
        if not getattr(self, "_batch_stub_refs", None) or len(self._batch_stub_refs) != len(self.batch_data):
            # 兜底：未预览或数量不匹配时重建占位骨架
            self._render_batch_preview()
        # 清掉占位行的"待检索"标记前先重置结果集为占位骨架（保留编号）
        self.search_results = list(self._batch_stub_refs)
        self.current_search_index = 0
        self.total_search_count = len(self.batch_data)
        self.batch_not_found = []  # 未找到的标准编号列表
        self._batch_cancelled = False  # 批量取消标志
        self._batch_search_running = True
        self._invalidate_detect()
        
        # 禁用按钮
        self.batch_search_btn.setEnabled(False)
        self.search_input.setEnabled(False)
        
        # 显示进度条（批量查询用确定模式）
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(self.total_search_count)
        self.progress_bar.setValue(0)
        
        # 显示停止按钮
        self.stop_search_btn.setVisible(True)
        
        # 批量缓存命中直接复用；其余条目交给 BatchSearchWorker 并行检索
        self._batch_ready = {}
        pending = []
        for k, keyword in enumerate(self.batch_data):
            keyword = keyword.strip()
            cached = self._load_batch_cached_results(keyword)
            if cached is not None:
                self._batch_ready[k] = (cached, False)
            else:
                pending.append((k, keyword))
        self._retire_search_worker()  # 批量开始前收掉单本残留线程
        self._reset_site_dots()
        self._batch_worker = None
        if pending:
            self._batch_worker = BatchSearchWorker(pending)
            self._batch_worker.item_done.connect(self.on_batch_item_done)
            self._batch_worker.finished.connect(self.on_batch_worker_finished)
            self._batch_worker.start()
        self.process_next_batch()
    
    def on_stop_search(self):
        """停止查新"""
        # 无论单本还是批量，都置位批量取消标志，阻止 process_next_batch 继续调度后续条目
        self._batch_cancelled = True
        # 取消批量并行检索（未完成条目不再落地）
        batch_worker = getattr(self, '_batch_worker', None)
        if batch_worker is not None and batch_worker.isRunning():
            batch_worker.cancel()
        # 取消当前正在运行的搜索线程（单本）
        if hasattr(self, 'search_worker') and self.search_worker and self.search_worker.isRunning():
            self.search_worker.cancel()
        self.stop_search_btn.setVisible(False)
        self.progress_label.setText("正在停止...")
    
    def process_next_batch(self):
        """按原始顺序把已完成的批量结果落地到表格（检索由 BatchSearchWorker 并行完成）"""
        if not getattr(self, '_batch_search_running', False):
            return
        # 检查是否被取消
        if getattr(self, '_batch_cancelled', False):
            self._restore_batch_ui()
            self.progress_label.setText("已停止查新")
            self.show_bubble("批量查新已停止", "warning")
            return

        if self.current_search_index >= self.total_search_count:
            # 全部完成
            self._restore_batch_ui()
            # 去重
            self.search_results = self.deduplicate_results(self.search_results)
            # 移除冗余替代行：如果替代标准本身也在导入数据中，则移除替代行
            self.search_results = self.remove_redundant_replacements(self.search_results)

            # 显示结果（批量完成：取消防抖并强制刷新）
            if self._batch_table_timer is not None:
                self._batch_table_timer.stop()
            self.show_all_results()

            found_count = len(self.search_results)
            not_found_count = len(self.batch_not_found)

            progress_msg = (
                f'<span style="color:{_theme.SUCCESS}; font-weight:bold;">✓</span>  '
                f'批量检索完成，共找到 {found_count} 条结果'
            )
            if not_found_count > 0:
                progress_msg += (
                    f'，<span style="color:{_theme.DANGER}; font-weight:bold;">'
                    f'未找到 {not_found_count} 条</span>'
                )
            self.progress_label.setText(progress_msg)

            # 弹出未找到标准对话框
            if not_found_count > 0:
                self._show_not_found_dialog()
            self._batch_result_snapshot = list(self.search_results or [])
            return

        k = self.current_search_index
        keyword = self.batch_data[k].strip()
        self.progress_label.setText(f"正在查询 {k + 1}/{self.total_search_count}：{keyword}")
        self.progress_bar.setValue(k)
        ready = self._batch_ready.pop(k, None)
        if ready is None:
            return  # 该条尚未完成，等待 on_batch_item_done
        results, remember = ready
        if self._apply_batch_item_results(k, results, remember=remember):
            QTimer.singleShot(0, self.process_next_batch)


    def _restore_batch_ui(self):
        """恢复批量检索 UI 状态（按钮、进度条、输入框）"""
        self._batch_search_running = False
        self.batch_search_btn.setEnabled(bool(self.batch_data))
        self.search_input.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.stop_search_btn.setVisible(False)

    def on_batch_item_done(self, index, results):
        """BatchSearchWorker 单条完成：登记结果，按原始顺序落地到表格。"""
        if not self._batch_search_running or self._batch_cancelled:
            return
        self._batch_ready[index] = (results, True)
        self.process_next_batch()

    def on_batch_worker_finished(self):
        """并行工作线程退出：仅在取消时由此恢复 UI（正常完成已在最后一条落地时处理）。"""
        if self._batch_search_running and self._batch_cancelled:
            self.process_next_batch()


    def _apply_batch_item_results(self, k, results, remember=False):
        """应用第 k 条批量结果（网络或缓存），返回是否继续下一条。"""
        keyword = self.batch_data[k].strip() if 0 <= k < len(self.batch_data) else ''
        self.current_search_index = k + 1
        self.progress_bar.setValue(self.current_search_index)

        # 检查是否被取消（停止后不再处理后续，但仍需恢复 UI）
        if getattr(self, '_batch_cancelled', False):
            self._restore_batch_ui()
            self.progress_label.setText("已停止查新")
            self.show_bubble("批量查新已停止", "warning")
            return False

        stub_refs = getattr(self, "_batch_stub_refs", None)
        if stub_refs and 0 <= k < len(stub_refs):
            stub = stub_refs[k]
            # 按对象身份在 search_results 中定位占位行
            idx = None
            for i, s in enumerate(self.search_results):
                if s is stub:
                    idx = i
                    break
            if idx is not None:
                if results:
                    # 原地替换：占位行 → 真实结果（保留编号，填入名称/状态/日期）
                    # results[0] 可能带 .replacement 属性，update_table 会自动追加替代行
                    self.search_results[idx:idx + 1] = results
                else:
                    # 未找到：保留占位行，标记为"未找到"，并退出「待检索」预览态
                    stub.status = "未找到"
                    stub.std_name = ""
                    stub.download_status = "不启用"
                    stub.is_pending = False

        if not results:
            # 未找到，记录到未找到列表
            if keyword and keyword not in self.batch_not_found:
                self.batch_not_found.append(keyword)
        elif remember:
            # 批量缓存独立存储，不污染单本查询历史；只缓存有结果的条目
            try:
                self._get_batch_cache_store().upsert(
                    keyword, 'std_no', results, source='batch'
                )
            except Exception as e:
                logger.warning("[批量缓存] 写入失败: %s", e)

        # 刷新当前页（防抖，避免 200 条批量检索重建表 200 次）
        self._schedule_batch_table_refresh()
        return True
    
    def _show_not_found_dialog(self):
        """弹出未找到标准列表对话框"""
        dlg = QDialog(self)
        dlg.setWindowTitle("批量检索 - 未找到标准")
        dlg.setMinimumWidth(480)
        dlg.setStyleSheet("QDialog { background: #ffffff; }")
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        
        header = QLabel(
            f"共 {len(self.batch_not_found)} 条标准未找到，请手动核实："
        )
        header.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {_theme.DANGER};")
        layout.addWidget(header)
        
        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet(
            f"QTextEdit {{ border: 1px solid {_COLOR_BORDER}; border-radius: 6px; padding: 8px; "
            f"font-size: 13px; background: #fff8f8; color: {_theme.DANGER}; }}"
        )
        text_edit.setPlainText("\n".join(self.batch_not_found))
        layout.addWidget(text_edit)
        
        btn_row = QHBoxLayout()
        copy_btn = QPushButton("复制列表")
        copy_btn.setStyleSheet(
            f"QPushButton {{ padding: 7px 20px; background: {_COLOR_PRIMARY}; color: white; "
            f"border: none; border-radius: 6px; font-size: 13px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {_theme.PRIMARY_HOVER}; }}"
        )
        def _copy():
            QApplication.clipboard().setText("\n".join(self.batch_not_found))
            self.show_bubble("已复制未找到标准列表", "success")
        copy_btn.clicked.connect(_copy)
        btn_row.addWidget(copy_btn)
        
        close_btn = QPushButton("关闭")
        close_btn.setStyleSheet(
            "QPushButton { padding: 7px 20px; background: #888; color: white; "
            "border: none; border-radius: 6px; font-size: 13px; font-weight: bold; }"
            "QPushButton:hover { background: #666; }"
        )
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)
        
        dlg.exec_()
    
    def _render_batch_preview(self):
        """批量导入后、检索前：以占位行(Stub)预览序号+标准编号，其余列"待检索"，
        并启用分页。点击「批量检索」后不重建表格，仅逐条把占位行替换为真实结果，
        标准编号始终在位。"""
        # 构造占位 StandardInfo，作为 search_results 的骨架（保留标准编号）
        stubs = []
        for code in self.batch_data:
            s = StandardInfo(std_no=code)
            s.is_pending = True
            s.download_status = "不启用"  # 预览阶段不提供下载
            stubs.append(s)
        self.search_results = stubs
        # 保留占位行对象引用，便于检索完成后按身份定位并原地替换
        self._batch_stub_refs = list(stubs)
        self._batch_result_snapshot = list(stubs)
        self.show_all_results()
        self.progress_label.setText(
            f"已导入 {len(self.batch_data)} 条，点击「批量检索」开始查询"
        )

    def on_batch_import(self):
        """批量导入：弹出文件选择对话框，支持Excel和txt格式"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择文件", "",
            "批量导入文件 (*.xlsx *.txt);;Excel文件 (*.xlsx);;文本文件 (*.txt);;所有文件 (*)"
        )
        if not file_path:
            return
        
        try:
            if file_path.lower().endswith('.xls') and not file_path.lower().endswith('.xlsx'):
                self.show_bubble("暂不支持旧版 .xls，请另存为 .xlsx 后再导入", "warning")
                return
            if file_path.lower().endswith('.xlsx'):
                # Excel文件 - 使用 openpyxl 读取（轻量，避免引入 pandas/numpy）
                from openpyxl import load_workbook
                wb = load_workbook(file_path, read_only=True, data_only=True)
                ws = wb.active
                rows = list(ws.iter_rows(values_only=True))
                wb.close()
                
                if rows:
                    # 检查第一行是否为表头（包含"编号"、"标准"等关键词）
                    first_cell = str(rows[0][0]).strip() if rows[0] and rows[0][0] else ""
                    has_header = any(keyword in first_cell for keyword in ['编号', '标准', '序号', 'No', 'Code'])
                    
                    # 提取第一列数据（跳过表头）
                    start_idx = 1 if has_header else 0
                    self.batch_data = [
                        str(rows[i][0]).strip() 
                        for i in range(start_idx, len(rows)) 
                        if rows[i] and rows[i][0] is not None and str(rows[i][0]).strip()
                    ]
                else:
                    self.show_bubble("Excel文件无有效数据", "error")
                    return
            elif file_path.lower().endswith('.txt'):
                # txt文件 - 自动检测编码（优先UTF-8，回退GBK）
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        self.batch_data = [line.strip() for line in f if line.strip()]
                except UnicodeDecodeError:
                    with open(file_path, 'r', encoding='gbk', errors='replace') as f:
                        self.batch_data = [line.strip() for line in f if line.strip()]
            else:
                self.show_bubble("不支持的文件格式，请选择Excel或txt文件", "error")
                return
            
            if not self.batch_data:
                self.show_bubble("文件中未找到有效数据", "warning")
                return
            
            # 去重（保持顺序，忽略大小写和空格差异）
            seen = set()
            unique_data = []
            for item in self.batch_data:
                key = item.strip().upper()
                if key not in seen:
                    seen.add(key)
                    unique_data.append(item.strip())
            total_before = len(self.batch_data)
            self.batch_data = unique_data
            dup_count = total_before - len(self.batch_data)
            
            # 更新UI状态
            if dup_count > 0:
                self.excel_status_btn.setText(f"已导入 {len(self.batch_data)} 条（已去除重复 {dup_count} 条）")
            else:
                self.excel_status_btn.setText(f"已导入 {len(self.batch_data)} 条")
            self.excel_status_btn.setStyleSheet("""
                QPushButton {
                    padding: 8px 16px;
                    background: rgba(76, 175, 80, 0.15);
                    color: #2e7d32;
                    border: 1px solid rgba(76, 175, 80, 0.45);
                    border-radius: 8px;
                    font-size: 13px;
                    font-weight: bold;
                    text-align: left;
                    min-height: 40px;
                }
                QPushButton:hover {
                    background: rgba(76, 175, 80, 0.25);
                }
            """)
            self.batch_search_btn.setEnabled(True)
            # 导入后立即在结果表中预览序号与标准编号，检索后再填入名称/日期等
            self._render_batch_preview()
            if dup_count > 0:
                self.show_bubble(f"成功导入 {len(self.batch_data)} 条数据（已去除重复 {dup_count} 条）", "success")
            else:
                self.show_bubble(f"成功导入 {len(self.batch_data)} 条数据", "success")
        except Exception as e:
            self.show_bubble(f"导入失败: {str(e)}", "error")
    
    def _restore_mode_results(self, mode):
        """按模式恢复结果表（单本与历史相互独立）。"""
        # 初始化阶段 create_top_section 会先 switch_query_mode，此时结果表尚未创建
        if not getattr(self, "result_table", None):
            return
        bufs = getattr(self, "_mode_result_bufs", None)
        if bufs is None or mode not in (bufs.SINGLE, bufs.HISTORY):
            return
        results = bufs.get(mode)
        self.search_results = list(results)
        if results:
            self.show_all_results()
            label = "历史" if mode == bufs.HISTORY else "单本"
            self.progress_label.setText(
                f'<span style="color:{_theme.SUCCESS}; font-weight:bold;">✓</span>  '
                f'{label}结果 {len(results)} 条'
            )
        else:
            self.result_table.setRowCount(0)
            if getattr(self, "progress_label", None):
                self.progress_label.setText("就绪")

    def _restore_batch_view(self):
        """切回批量查询：恢复批量快照/导入预览，勿残留历史汇总表。"""
        if not getattr(self, "result_table", None):
            return
        snap = getattr(self, "_batch_result_snapshot", None)
        if snap is not None:
            self.search_results = list(snap)
            if self.search_results:
                self.show_all_results()
                pending = sum(
                    1 for s in self.search_results
                    if getattr(s, "is_pending", False)
                )
                if pending and pending == len(self.search_results):
                    self.progress_label.setText(
                        f"已导入 {len(self.search_results)} 条，点击「批量检索」开始查询"
                    )
                else:
                    self.progress_label.setText(
                        f'<span style="color:{_theme.SUCCESS}; font-weight:bold;">✓</span>  '
                        f'批量结果 {len(self.search_results)} 条'
                    )
            else:
                self.result_table.setRowCount(0)
                self.progress_label.setText("就绪")
            return
        if getattr(self, "batch_data", None):
            self._render_batch_preview()
            return
        self.search_results = []
        self.result_table.setRowCount(0)
        self.progress_label.setText("就绪")

    def clear_results(self, clear_input=False):
        """清空搜索结果（保留批量导入数据）。
        clear_input=True 时同时清空单本搜索框（仅「清除查询」按钮使用）；
        只影响单本查询视图，不会清除历史查询列表或历史结果快照。
        """
        self.search_results = []
        self._batch_stub_refs = []  # 清空占位行引用，避免下次检索误定位
        self.result_table.setRowCount(0)
        self.progress_label.setText("就绪")
        if clear_input:
            self.search_input.clear()
            self._pending_history = None
            bufs = getattr(self, "_mode_result_bufs", None)
            if bufs is not None:
                bufs.clear_single()
            # 停止进行中的单本检索，避免晚到的回调改写历史缓存
            if self._retire_search_worker():
                self._search_cancelled = True
            self._stop_single_progress_anim()
            self.progress_bar.setVisible(False)
            self.stop_search_btn.setVisible(False)
        self.search_input.setEnabled(True)
        # 注意：不清空 batch_data，保留批量导入的数据
        # 注意：绝不触碰 QueryHistoryStore / 历史结果缓冲

        self._reset_site_dots()
    
    def _reset_site_dots(self):
        """重置底部站点圆点与「站点 n/m」文案。"""
        for dot in self.status_dots:
            dot.status = "idle"
            dot.update()
        self._refresh_site_status_caption()

    def _refresh_site_status_caption(self):
        """根据圆点状态刷新「站点状态 / 站点 n/m」。"""
        if not hasattr(self, "site_status_caption"):
            return
        total = len(self.status_dots)
        if total == 0:
            self.site_status_caption.setText("站点状态")
            return
        done = sum(
            1 for d in self.status_dots
            if d.status in ("success", "failed", "no_result")
        )
        searching = any(d.status == "searching" for d in self.status_dots)
        if searching or done:
            self.site_status_caption.setText(f"站点 {done}/{total}")
        else:
            self.site_status_caption.setText("站点状态")
    
    def export_results(self):
        """导出结果（含替代标准：序号/编号/名称/状态/日期）"""
        if not self.search_results:
            self.show_bubble("没有可导出的结果", "warning")
            return
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "保存Excel文件", "标准查新结果.xlsx",
            "Excel文件 (*.xlsx);;所有文件 (*)"
        )
        
        if file_path:
            try:
                rows = []
                seq = 1
                for std in self.search_results:
                    if getattr(std, "is_pending", False):
                        continue
                    is_abolished = any(k in (std.status or "") for k in ("废止", "作废"))
                    replacement = getattr(std, "replacement", None)
                    
                    # 主标准行
                    status_text = std.status or ""
                    if is_abolished and not replacement and getattr(std, "no_replacement", False):
                        status_text = "废止（已作废，无替代）"
                    elif replacement and not is_abolished:
                        repl_pending = "即将实施" in (replacement.status or "")
                        status_text = f"{std.status or '现行'}（{'即将被替代' if repl_pending else '已被替代'}）"
                    
                    rows.append({
                        "序号": seq,
                        "标准编号": std.std_no,
                        "标准名称": std.std_name,
                        "标准状态": status_text,
                        "发布日期": std.publish_date,
                        "实施日期": std.implement_date,
                    })
                    seq += 1
                    
                    # 替代标准行（紧跟在已作废标准之后）
                    if replacement:
                        rows.append({
                            "序号": "替代",
                            "标准编号": replacement.std_no,
                            "标准名称": replacement.std_name,
                            "标准状态": replacement.status or "现行",
                            "发布日期": replacement.publish_date,
                            "实施日期": replacement.implement_date,
                        })
                
                # 使用 openpyxl 写入 Excel（轻量，避免引入 pandas/numpy）
                from openpyxl import Workbook
                wb = Workbook()
                ws = wb.active
                headers = ["序号", "标准编号", "标准名称", "标准状态", "发布日期", "实施日期"]
                ws.append(headers)
                for row in rows:
                    ws.append([
                        row.get("序号", ""),
                        row.get("标准编号", ""),
                        row.get("标准名称", ""),
                        row.get("标准状态", ""),
                        row.get("发布日期", ""),
                        row.get("实施日期", ""),
                    ])
                wb.save(file_path)
                wb.close()
                self.show_bubble(f"成功导出 {len(rows)} 条到: {os.path.basename(file_path)}", "success")
            except Exception as e:
                self.show_bubble(f"导出失败: {str(e)}", "error")
    
    def update_table(self, results, start_index=0):
        """更新表格显示：先算总行数一次 setRowCount，避免 insertRow 逐行触发重排卡顿。"""
        plan = []
        for i, std in enumerate(results):
            is_abolished = any(k in (std.status or "") for k in ("废止", "作废"))
            replacement = getattr(std, "replacement", None)
            if is_abolished and not replacement and getattr(std, "no_replacement", False):
                plan.append((std, str(start_index + i + 1), False, "废止（已作废，无替代）"))
            elif replacement and not is_abolished:
                repl_pending = "即将实施" in (replacement.status or "")
                suffix = "即将被替代" if repl_pending else "已被替代"
                plan.append(
                    (std, str(start_index + i + 1), False,
                     f"{std.status or '现行'}（{suffix}）")
                )
            else:
                plan.append((std, str(start_index + i + 1), False, None))
            if replacement:
                plan.append((replacement, "替代", True, None))

        sorting = self.result_table.isSortingEnabled()
        self.result_table.setUpdatesEnabled(False)
        self.result_table.setSortingEnabled(False)
        try:
            self.result_table.setRowCount(len(plan))
            for row, (std, seq, is_repl, status_text) in enumerate(plan):
                self._fill_std_row(row, std, seq, is_replacement=is_repl, status_text=status_text)
        finally:
            self.result_table.setSortingEnabled(sorting)
            self.result_table.setUpdatesEnabled(True)

    def _insert_std_row(self, row, std, seq_text, is_replacement=False, status_text=None):
        """兼容旧调用：不足时扩展行数再填充。"""
        if self.result_table.rowCount() <= row:
            self.result_table.setRowCount(row + 1)
        self._fill_std_row(row, std, seq_text, is_replacement=is_replacement, status_text=status_text)

    def _fill_std_row(self, row, std, seq_text, is_replacement=False, status_text=None):
        """填充一行标准数据（主行或替代行）；调用方须已 setRowCount。"""
        self.result_table.setRowHeight(row, 56)
        highlight = QColor(*_theme.REPLACEMENT_BG_RGB) if is_replacement else None

        # 批量导入预览占位行：仅序号+标准编号有效，其余列统一显示"待检索"
        is_pending = getattr(std, "is_pending", False)

        # 整行文字配色：即将实施→警告色，已作废→危险色，其它→默认
        status_show = status_text if status_text else (std.status or "")
        is_abolished = "废止" in status_show or "作废" in status_show
        if "即将实施" in status_show:
            row_color = _theme.warning_qcolor()
        elif is_abolished:
            row_color = _theme.danger_qcolor()
        else:
            row_color = None

        def apply_bg(item, colorize=True):
            if highlight:
                item.setBackground(highlight)
            if colorize and row_color:
                item.setForeground(row_color)
            return item

        # 待检索占位样式
        ph_color = _theme.qcolor(_theme.PENDING)
        def ph_item(text="待检索"):
            it = QTableWidgetItem(text)
            it.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
            it.setForeground(ph_color)
            return it

        # 序号
        item = QTableWidgetItem(seq_text)
        item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        if is_replacement:
            item.setForeground(_theme.success_qcolor())
            self.result_table.setItem(row, 0, apply_bg(item, colorize=False))
        else:
            self.result_table.setItem(row, 0, apply_bg(item))

        # 标准编号：字号/字重/主色强调（有状态行色时仍跟状态色）
        item = QTableWidgetItem(std.std_no)
        item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        _theme.apply_std_no_item(item, emphasize_color=(row_color is None))
        item.setData(Qt.UserRole, {
            "std_no": std.std_no or "",
            "std_name": std.std_name or "",
            "history_query_key": getattr(std, "_history_query_key", None) or "",
        })
        self.result_table.setItem(row, 1, apply_bg(item))

        # 预览占位：名称/状态/日期/PDF 一律"待检索"，不渲染下载按钮
        if is_pending:
            self.result_table.setItem(row, 2, apply_bg(ph_item("待检索"), colorize=False))
            self.result_table.setItem(row, 3, apply_bg(ph_item("待检索"), colorize=False))
            self.result_table.setItem(row, 4, apply_bg(ph_item("待检索"), colorize=False))
            self.result_table.setItem(row, 5, apply_bg(ph_item("待检索"), colorize=False))
            container = QWidget()
            cl = QHBoxLayout(container)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.addWidget(QLabel("待检索"), 0, Qt.AlignCenter)
            container.setStyleSheet(f"color: {_theme.PENDING}; font-size: 12px;")
            self.result_table.setCellWidget(row, 6, container)
            return
        
        # 标准名称
        item = QTableWidgetItem(std.std_name)
        item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.result_table.setItem(row, 2, apply_bg(item))
        
        # 标准状态（带颜色）
        item = QTableWidgetItem(status_show)
        item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        if "即将实施" in status_show:
            item.setForeground(_theme.warning_qcolor())
        elif "现行" in status_show:
            item.setForeground(_theme.success_qcolor())
        elif is_abolished:
            item.setForeground(_theme.danger_qcolor())
        self.result_table.setItem(row, 3, apply_bg(item, colorize=False))
        
        # 发布日期
        item = QTableWidgetItem(std.publish_date)
        item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        self.result_table.setItem(row, 4, apply_bg(item))
        
        # 实施日期
        item = QTableWidgetItem(std.implement_date)
        item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        self.result_table.setItem(row, 5, apply_bg(item))
        
        # PDF操作按钮 - 居中容器
        download_btn = DownloadButton()
        is_batch = (std.download_status == "不启用")  # 批量/汉字名称查询：一律「不启用」
        if is_batch:
            # 批量查询、汉字名称查询：一律不启用下载
            download_btn.set_status("disabled_batch")
        elif is_abolished:
            # 单本查询废止标准：不提供下载
            download_btn.set_status("not_provided")
        elif not self.download_enabled:
            # 单本查询且未勾选"开启下载"：禁用下载（显示"未开启"）
            download_btn.set_status("download_off")
        elif std.download_status == "可下载":
            # 已开启下载功能：本地命中→「本地打开」，网络命中→「下载」
            if getattr(std, "is_local", False):
                download_btn.set_status("local_idle")
                download_btn.clicked.connect(
                    lambda checked, s=std, b=download_btn: self.on_open_local_pdf(s, b)
                )
            else:
                download_btn.set_status("idle")
                download_btn.clicked.connect(
                    lambda checked, s=std, b=download_btn: self.on_download_pdf(s, b)
                )
        elif std.download_status == "详情页":
            download_btn.setText("查看详情")
            download_btn.clicked.connect(
                lambda checked, s=std: self.open_detail_page(s)
            )
        elif std.download_status == "检测中":
            # 已开启下载功能，正在联网探测下载源：蓝色流光「检测中」占位
            download_btn.set_status("detecting")
        elif std.download_status == "未找到资源":
            # 已探测但各下载源均无可用资源（含 openstd 标"采"标准）：不可下载
            download_btn.set_status("not_found")
        else:
            download_btn.set_status("disabled")
        
        # 用容器包裹按钮实现水平居中（透明背景，行悬停时由 _on_table_cell_entered 更新）
        container = QWidget()
        container.setProperty("is_replacement", bool(is_replacement))
        if is_replacement:
            container.setStyleSheet(f"background: {_theme.REPLACEMENT_BG};")
        else:
            container.setStyleSheet("background: transparent;")
        cell_layout = QHBoxLayout(container)
        cell_layout.setContentsMargins(0, 0, 0, 0)
        cell_layout.setAlignment(Qt.AlignCenter)
        cell_layout.addWidget(download_btn)
        self.result_table.setCellWidget(row, 6, container)

    def _on_table_cell_entered(self, row, col):
        """行悬停时同步更新 PDF 列容器背景，保持与表项 hover 一致的外观"""
        pdf_col = getattr(self, "_pdf_col", 6)
        if row == self._hovered_row:
            return
        # 恢复上一行（替代行恢复浅黄底，普通行透明）
        if self._hovered_row >= 0:
            w = self.result_table.cellWidget(self._hovered_row, pdf_col)
            if w:
                if w.property("is_replacement"):
                    w.setStyleSheet(f"background: {_theme.REPLACEMENT_BG};")
                else:
                    w.setStyleSheet("background: transparent;")
        self._hovered_row = row
        # 高亮当前行
        w = self.result_table.cellWidget(row, pdf_col)
        if w:
            w.setStyleSheet(f"background: {_COLOR_SOFT};")

    def _row_payload(self, row):
        """从表格行取出 UserRole 载荷（编号/名称）。"""
        item = self.result_table.item(row, 1)
        if item is None:
            return None
        data = item.data(Qt.UserRole)
        if isinstance(data, dict):
            return data
        return {
            "std_no": item.text() or "",
            "std_name": (self.result_table.item(row, 2).text()
                         if self.result_table.item(row, 2) else ""),
        }

    def _on_table_cell_double_clicked(self, row, col):
        """双击编号/名称：复制对应文本。"""
        payload = self._row_payload(row)
        if not payload:
            return
        if col == 1:
            text = payload.get("std_no", "")
        elif col == 2:
            text = payload.get("std_name", "")
        else:
            text = payload.get("std_no", "")
        if text and text != "待检索":
            QApplication.clipboard().setText(text)
            self.show_bubble(f"已复制：{text}", "success")

    def _on_table_context_menu(self, pos):
        """右键：复制编号/名称、加入台账、打开下载目录。"""
        index = self.result_table.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()
        payload = self._row_payload(row)
        if not payload:
            return
        std_no = payload.get("std_no", "")
        std_name = payload.get("std_name", "")

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { padding: 6px; } QMenu::item { padding: 6px 20px; }"
        )
        act_no = menu.addAction("复制标准编号")
        act_name = menu.addAction("复制标准名称")
        act_ledger = None
        if Config.LEDGER_ENABLED:
            menu.addSeparator()
            act_ledger = menu.addAction("加入标准台账")
        menu.addSeparator()
        act_dir = menu.addAction("打开下载目录")
        chosen = menu.exec_(self.result_table.viewport().mapToGlobal(pos))
        if chosen == act_no and std_no:
            QApplication.clipboard().setText(std_no)
            self.show_bubble(f"已复制：{std_no}", "success")
        elif chosen == act_name and std_name and std_name != "待检索":
            QApplication.clipboard().setText(std_name)
            self.show_bubble("已复制标准名称", "success")
        elif act_ledger is not None and chosen == act_ledger and std_no:
            self._add_row_to_ledger(row, payload)
        elif chosen == act_dir:
            self._open_download_dir()

    def _add_row_to_ledger(self, row, payload):
        """将查询结果行写入标准台账。"""
        if not Config.LEDGER_ENABLED:
            self.show_bubble("请先在「设置」中启用标准台账", "warning")
            return
        std_no = payload.get("std_no", "")
        if not std_no:
            return
        status = ""
        publish_date = ""
        implement_date = ""
        try:
            st_item = self.result_table.item(row, 3)
            pub_item = self.result_table.item(row, 4)
            impl_item = self.result_table.item(row, 5)
            status = st_item.text() if st_item else ""
            publish_date = pub_item.text() if pub_item else ""
            implement_date = impl_item.text() if impl_item else ""
            if status == "待检索":
                status = "现行"
            if publish_date == "待检索":
                publish_date = ""
            if implement_date == "待检索":
                implement_date = ""
        except Exception:
            pass
        # 确保台账页已创建，便于写入后刷新
        if not getattr(self, '_ledger_page', None):
            from .standard_ledger_page import StandardLedgerPage
            page = StandardLedgerPage()
            self.stack.addWidget(page)
            self._ledger_page = page
            self._ledger_stack_index = self.stack.count() - 1
        created = self._ledger_page.add_from_query(
            std_no,
            std_name=payload.get("std_name") or "",
            status=status,
            publish_date=publish_date,
            implement_date=implement_date,
        )
        self._refresh_ledger_nav_badge()
        self.show_bubble(
            f"{'已加入' if created else '已更新'}台账：{std_no}", "success"
        )
    def _open_download_dir(self):
        """在资源管理器中打开下载目录。"""
        path = Config.effective_download_dir()
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(path)
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            self.show_bubble(f"已打开：{path}", "success")
        except Exception as e:
            self.show_bubble(f"打开下载目录失败：{e}", "error")

    def on_open_local_pdf(self, standard_info, button):
        """本地打开PDF：直接用系统默认程序打开文档管理目录中命中的原文件，不复制到下载目录。"""
        file_path = getattr(standard_info, "local_file_path", None)
        if not file_path:
            # 兜底：路径丢失时重新在文档管理目录查找一次
            lan_file = self._find_in_lan_cached_only(standard_info.std_no)
            if lan_file is None:
                button.set_status("failed")
                self.show_bubble(f"未在文档管理目录找到 {standard_info.std_no} 的本地文件", "error")
                return
            file_path = str(lan_file)
            standard_info.local_file_path = file_path

        try:
            import os
            if not os.path.exists(file_path):
                button.set_status("failed")
                self.show_bubble(f"本地文件不存在：{file_path}", "error")
                return
            # 通过 cmd.exe /c start 间接打开，调用方变为已签名的 cmd.exe，
            # 避免 Adobe Reader 受保护视图因调用方 exe 未签名而拦截 PDF
            import subprocess
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "", file_path],
                shell=False,
                creationflags=0x08000000,  # CREATE_NO_WINDOW 隐藏 cmd 黑窗
                close_fds=True,
            )
            button.set_status("local_success")
            self.show_bubble(f"已打开本地文件：{os.path.basename(file_path)}", "success")
        except OSError as e:
            button.set_status("failed")
            self.show_bubble(f"打开本地文件失败：{str(e)}", "error")

    def on_download_pdf(self, standard_info, button):
        """下载PDF。

        下载前兜底：即便本地搜索线程尚未标记 is_local（异步时序问题），
        也先同步在文档管理目录精确匹配一次；命中则直接本地打开，绝不误走网络下载。
        未命中本地时：先预览（静默下到临时目录），确认后再复制到用户下载目录。
        """
        if not getattr(standard_info, "is_local", False):
            try:
                lan_file = self._find_in_lan_cached_only(standard_info.std_no)
            except Exception as e:
                logger.warning(f"[下载兜底] 本地匹配异常: {e}")
                lan_file = None
            if lan_file is not None:
                standard_info.is_local = True
                standard_info.local_file_path = str(lan_file)
                standard_info.download_status = "可下载"
                standard_info.download_url = ""
                standard_info.detail_url = ""
                self.on_open_local_pdf(standard_info, button)
                QTimer.singleShot(0, self.show_all_results)
                return

        plan = self._resolve_preview_plan(standard_info)
        if plan is not None:
            self._start_preview_then_save(standard_info, button, plan)
            return

        # 其它未纳入预览计划的源：保持原直接下载
        button.set_status("downloading")
        self._download_workers = [w for w in self._download_workers if w.isRunning()]
        worker = DownloadWorker(self._new_download_spider(), standard_info)
        worker.finished.connect(
            lambda success, msg, b=button, s=standard_info: self.on_download_finished(success, msg, b, s)
        )
        worker.progress.connect(self.update_download_progress)
        self._download_workers.append(worker)
        worker.start()

    @staticmethod
    def _extract_hcno_from_std(standard_info):
        """从 download_url / samr_text_url / detail_url 提取 hcno。"""
        for attr in ('download_url', 'samr_text_url', 'detail_url'):
            url = getattr(standard_info, attr, None) or ''
            m = re.search(r'hcno=([^&]+)', url)
            if m:
                return m.group(1).strip()
        return ''

    def _resolve_preview_plan(self, standard_info):
        """按 DOWNLOAD_SOURCES 优先级决定预览模式。

        返回 dict: mode, kind, hcno, online_url, samr_url, source_label
        或 None（走旧直接下载）。
        """
        from spiders.openstd_downloader import OpenstdPdfDownloader

        sources_raw = list(getattr(standard_info, 'download_sources', None) or [])
        sources_l = [str(s).lower() for s in sources_raw]
        samr_url = (getattr(standard_info, 'samr_text_url', None) or '').strip()
        hcno = self._extract_hcno_from_std(standard_info)
        # 「采」标（标信/国标全文查询）：不走标信/官方全文预览下载
        site = (getattr(standard_info, 'source_site', None) or '').strip()
        skip_official = (
            getattr(standard_info, 'is_adopted', False)
            and site in ('国家标准全文', '全国标准信息公共服务平台')
        )

        def _has(parser_key, *aliases):
            keys = (parser_key,) + aliases
            for s in sources_l:
                for k in keys:
                    if k in s:
                        return True
            return False

        for src in Config.DOWNLOAD_SOURCES:
            parser = src.get('parser', '')
            name = src.get('name', '')
            if skip_official and parser in ('std_samr', 'openstd'):
                continue

            if parser == 'std_samr':
                if not (_has('std_samr', 'samr') or samr_url):
                    continue
                if samr_url and 'openstd.samr.gov.cn' in samr_url:
                    h = hcno
                    if not h:
                        m = re.search(r'hcno=([^&]+)', samr_url)
                        h = m.group(1).strip() if m else ''
                    if h:
                        return {
                            'mode': 'online_url',
                            'kind': 'openstd',
                            'hcno': h,
                            'online_url': OpenstdPdfDownloader.online_preview_url(h),
                            'detail_url': OpenstdPdfDownloader._detail_url(h),
                            'samr_url': None,
                            'source_label': name,
                        }
                elif samr_url:
                    return {
                        'mode': 'temp_pdf',
                        'kind': 'std_samr',
                        'hcno': None,
                        'online_url': None,
                        'detail_url': None,
                        'samr_url': samr_url,
                        'source_label': name,
                    }

            elif parser == 'openstd':
                if not (_has('openstd') or hcno):
                    continue
                h = hcno
                if not h:
                    continue
                return {
                    'mode': 'online_url',
                    'kind': 'openstd',
                    'hcno': h,
                    'online_url': OpenstdPdfDownloader.online_preview_url(h),
                    'detail_url': OpenstdPdfDownloader._detail_url(h),
                    'samr_url': None,
                    'source_label': name,
                }

            elif parser == 'webfree':
                if not _has('webfree', '协筑'):
                    continue
                return {
                    'mode': 'temp_pdf',
                    'kind': 'webfree',
                    'hcno': None,
                    'online_url': None,
                    'detail_url': None,
                    'samr_url': None,
                    'source_label': name,
                }

            elif parser == 'gxzl':
                if not _has('gxzl', '东盟'):
                    continue
                return {
                    'mode': 'gxzl_pages',
                    'kind': 'gxzl',
                    'hcno': None,
                    'online_url': None,
                    'detail_url': None,
                    'samr_url': None,
                    'source_label': name,
                }

        # 探测结果仅有 gxzl 的兼容：sources 空但 download_url 像东盟
        if _has('gxzl', '东盟'):
            return {
                'mode': 'gxzl_pages',
                'kind': 'gxzl',
                'hcno': None,
                'online_url': None,
                'detail_url': None,
                'samr_url': None,
                'source_label': '中国东盟标准云平台',
            }
        return None

    def _preview_cache_dir(self):
        d = os.path.join(TEMP_DIR, 'preview_cache')
        os.makedirs(d, exist_ok=True)
        return d

    def _make_preview_temp_path(self, standard_info):
        import time
        raw = f"{standard_info.std_no}《{standard_info.std_name or ''}》"
        safe = _safe_filename(raw)
        # 每次预览独立文件名，避免上次句柄未释放时覆盖失败
        return os.path.join(
            self._preview_cache_dir(),
            f"{safe}_{int(time.time() * 1000)}.pdf",
        )

    def _start_preview_then_save(self, standard_info, button, plan):
        """打开预览框并启动静默下载到临时目录。"""
        from ui.preview_dialog import (
            StandardPreviewDialog, MODE_GXZL_PAGES, MODE_ONLINE_URL, MODE_TEMP_PDF,
        )

        mode_map = {
            'gxzl_pages': MODE_GXZL_PAGES,
            'online_url': MODE_ONLINE_URL,
            'temp_pdf': MODE_TEMP_PDF,
        }
        mode = mode_map.get(plan['mode'], MODE_TEMP_PDF)
        temp_path = self._make_preview_temp_path(standard_info)

        # 取消上一轮静默任务
        self._cancel_silent_preview_worker()

        dialog = StandardPreviewDialog(
            getattr(standard_info, 'std_no', ''),
            getattr(standard_info, 'std_name', ''),
            self,
            mode=mode,
            online_url=plan.get('online_url'),
            detail_url=plan.get('detail_url'),
        )
        self._preview_dialog = dialog
        self._preview_temp_path = temp_path
        self._preview_std = standard_info
        self._preview_button = button

        dialog.download_requested.connect(
            lambda si=standard_info, btn=button, tp=temp_path: QTimer.singleShot(
                0, lambda: self._on_preview_save_confirmed(si, btn, tp)
            )
        )
        dialog.discard_requested.connect(
            lambda tp=temp_path: QTimer.singleShot(
                0, lambda: self._on_preview_discarded(tp)
            )
        )

        # 限制 download_sources，避免确认后误走其它源（协筑-only 路径）
        kind = plan.get('kind')
        if kind == 'gxzl':
            standard_info.download_sources = ['gxzl']
        elif kind == 'openstd':
            standard_info.download_sources = ['openstd']
        elif kind == 'std_samr':
            standard_info.download_sources = ['std_samr']
        elif kind == 'webfree':
            standard_info.download_sources = ['webfree']

        worker = SilentPreviewDownloadWorker(
            kind=kind,
            std_no=getattr(standard_info, 'std_no', ''),
            dest_path=temp_path,
            hcno=plan.get('hcno'),
            samr_url=plan.get('samr_url'),
        )
        worker.progress.connect(dialog.set_silent_progress)
        worker.finished.connect(
            lambda ok, msg, d=dialog: self._on_silent_preview_finished(d, ok, msg)
        )
        self._silent_preview_worker = worker
        worker.start()

        dialog.exec_()

    def _on_silent_preview_finished(self, dialog, ok, path_or_error):
        try:
            if dialog is not None and dialog.isVisible():
                dialog.set_silent_ready(ok, path_or_error)
        except RuntimeError:
            pass
        if not ok:
            logger.warning(f"[预览静默下载] 失败: {path_or_error}")

    def _on_preview_save_confirmed(self, standard_info, button, temp_path):
        """用户确认：复制临时 PDF 到下载目录并清理临时文件。"""
        # 若已开启新一轮预览，忽略过期的保存回调，避免清错引用/删错文件
        cur = getattr(self, '_preview_temp_path', None)
        if cur is not None and temp_path and cur != temp_path:
            logger.debug("[预览保存] 忽略过期回调: %s", temp_path)
            return
        # 预览就绪时静默任务通常已结束；切勿 wait 阻塞 UI
        self._cancel_silent_preview_worker(wait=False)
        dlg = getattr(self, '_preview_dialog', None)
        if dlg is not None and cur == temp_path:
            try:
                dlg._close_pdf_doc(wait_ms=0)
            except Exception:
                pass
        if not temp_path or not os.path.isfile(temp_path):
            self.show_bubble("临时文件不存在，请重试下载", "error")
            if button is not None:
                button.set_status("failed")
            if cur == temp_path:
                self._preview_dialog = None
                self._preview_temp_path = None
            return
        try:
            dest_dir = Config.effective_download_dir()
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(temp_path))
            base, ext = os.path.splitext(dest)
            n = 1
            while os.path.exists(dest):
                dest = f"{base}_{n}{ext}"
                n += 1
            # 文件可能仍被刚杀掉的渲染进程短暂占用，短暂重试
            last_err = None
            for _ in range(5):
                try:
                    shutil.copy2(temp_path, dest)
                    last_err = None
                    break
                except OSError as e:
                    last_err = e
                    try:
                        import time
                        time.sleep(0.08)
                    except Exception:
                        pass
            if last_err is not None:
                raise last_err
            self._delete_preview_temp(temp_path)
            if button is not None:
                button.set_status("success")
            self.show_bubble(f"已保存：{os.path.basename(dest)}", "success")
            self.open_download_location(dest)
        except Exception as e:
            logger.warning(f"[预览保存] 复制失败: {e}")
            self.show_bubble(f"保存失败：{e}", "error")
            if button is not None:
                button.set_status("failed")
        finally:
            if getattr(self, '_preview_temp_path', None) == temp_path:
                self._preview_dialog = None
                self._preview_temp_path = None

    def _on_preview_discarded(self, temp_path):
        """关闭预览：取消静默任务并删除临时文件。"""
        cur = getattr(self, '_preview_temp_path', None)
        # 新预览已启动：只清理本轮临时文件，不动当前弹窗引用
        if cur is not None and temp_path and cur != temp_path:
            self._delete_preview_temp(temp_path)
            return
        dlg = getattr(self, '_preview_dialog', None)
        if dlg is not None:
            try:
                dlg._close_pdf_doc(wait_ms=0)
            except Exception:
                pass
        self._cancel_silent_preview_worker(wait=False)
        self._delete_preview_temp(temp_path)
        if getattr(self, '_preview_temp_path', None) == temp_path:
            self._preview_dialog = None
            self._preview_temp_path = None

    def _cancel_silent_preview_worker(self, wait=False):
        w = getattr(self, '_silent_preview_worker', None)
        if w is None:
            return
        try:
            w.cancel()
            if wait and w.isRunning():
                w.wait(3000)
            elif w.isRunning():
                # 不阻塞 UI：稍后随 GC；文件删除由 discard 负责
                pass
        except RuntimeError:
            pass
        self._silent_preview_worker = None

    def _delete_preview_temp(self, path):
        if not path:
            return
        for _ in range(3):
            try:
                if os.path.isfile(path):
                    os.remove(path)
                return
            except OSError as e:
                logger.debug(f"[预览清理] 删除失败 {path}: {e}")
                try:
                    import time
                    time.sleep(0.05)
                except Exception:
                    pass
        # 残留交给下次启动 _cleanup_preview_cache
        logger.warning(f"[预览清理] 仍无法删除（可能被占用）: {path}")

    def _cleanup_preview_cache(self):
        """清理 preview_cache 目录残留。"""
        cache = os.path.join(TEMP_DIR, 'preview_cache')
        if not os.path.isdir(cache):
            return
        try:
            for name in os.listdir(cache):
                p = os.path.join(cache, name)
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                    elif os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                except OSError:
                    pass
        except OSError:
            pass

    def _check_gxzl_preview_required(self, standard_info, button):
        """兼容旧调用：改走统一预览流。"""
        plan = self._resolve_preview_plan(standard_info)
        if plan and plan.get('kind') == 'gxzl':
            self._start_preview_then_save(standard_info, button, plan)
            return True
        return False

    def _start_gxzl_download(self, standard_info, button):
        """兼容旧调用：预览确认后已由复制逻辑处理，此处不再二次下载。"""
        logger.info("[预览] 跳过旧版二次协筑下载（已由临时文件复制完成）")
    
    def on_download_finished(self, success, message, button, standard_info):
        """下载完成"""
        def _safe_set_status(status):
            try:
                if button is not None:
                    button.set_status(status)
            except RuntimeError:
                logger.debug("下载完成回调：按钮已销毁，跳过状态更新")

        if success:
            # LOCAL: 前缀 = 命中文档管理目录的本地分发（兜底路径，正常流程走 on_open_local_pdf 直接打开）
            if message.startswith("LOCAL:"):
                _safe_set_status("local_success")
                file_path = Path(message[len("LOCAL:"):])
                self.show_bubble(f"本地文件已复制到：{file_path.parent}", "success")
                self.open_download_location(file_path)
                return
            _safe_set_status("success")
            file_path = Path(message)
            self.show_bubble(f"下载成功，已保存到：{file_path.parent}", "success")
            self.open_download_location(file_path)
        elif message.startswith("UNAVAILABLE:"):
            # 轮动查找所有下载源仍未找到可下载的有效版本：置为不可下载并居中提示"未找到"
            _safe_set_status("disabled")
            detail = message.split("UNAVAILABLE:", 1)[1].strip()
            self.show_bubble(detail or "未找到可下载的标准文件", "warning")
        elif message.startswith("NEED_BROWSER:"):
            # openstd 等需网页端操作：优先打开 message 中的 openstd 详情/下载页
            # （勿打开 std.samr.gov.cn 详情页——那里只有「查看文本」，不是直接下载页）
            browser_url = message.split("NEED_BROWSER:", 1)[1].strip()
            opened = False
            if browser_url.startswith(("http://", "https://")):
                import webbrowser
                webbrowser.open(browser_url)
                opened = True
            else:
                opened = self._get_ui_spider().open_detail_in_browser(standard_info)
            if opened:
                _safe_set_status("idle")
                self.show_bubble("已在浏览器打开国家标准全文页，请点击「下载标准」", "info")
            else:
                _safe_set_status("disabled")
        else:
            _safe_set_status("failed")
            self.show_bubble(message, "error")
            # 降级方案：询问用户是否在浏览器中打开详情页下载
            if ask_yes_no(
                self, "下载失败",
                f"{message}\n\n是否在系统浏览器中打开详情页手动下载？",
                default_yes=True,
            ):
                self._get_ui_spider().open_detail_in_browser(standard_info)
                self.show_bubble(f"已在浏览器中打开: {standard_info.std_no}", "success")
    
    def open_download_location(self, file_path):
        """下载成功后，在资源管理器中定位该文件；失败则打开下载目录"""
        try:
            import subprocess
            file_path = Path(file_path)
            if file_path.exists():
                subprocess.Popen(['explorer', '/select,', str(file_path)])
            else:
                os.startfile(str(file_path.parent))
        except Exception:
            try:
                os.startfile(Config.effective_download_dir())
            except Exception as e:
                logger.warning("回退打开下载目录失败: %s", e)
    
    def open_settings(self):
        """打开设置对话框，允许用户修改下载保存目录与局域网标准共享目录"""
        dialog = SettingsDialog(
            Config.DOWNLOAD_DIR,
            Config.LAN_SHARE_DIR,
            ledger_enabled=Config.LEDGER_ENABLED,
            parent=self,
        )
        if dialog.exec_() != QDialog.Accepted:
            return

        changed = False
        messages = []

        new_dir = dialog.get_download_dir()
        if new_dir != Config.DOWNLOAD_DIR:
            if new_dir:
                try:
                    os.makedirs(new_dir, exist_ok=True)
                except Exception as e:
                    self.show_bubble(f"目录创建失败：{e}", "error")
                    return
            Config.DOWNLOAD_DIR = new_dir
            if self._spider is not None:
                self._spider.download_dir = Path(Config.effective_download_dir())
            changed = True
            eff = Config.effective_download_dir()
            messages.append(
                f"下载目录已更新：{eff}" if new_dir else f"下载目录已重置为默认：{eff}"
            )

        new_lan = dialog.get_lan_share_dir()
        if new_lan != Config.LAN_SHARE_DIR:
            Config.LAN_SHARE_DIR = new_lan
            # 变更后失效局域网索引缓存，使新目录立即生效
            self._invalidate_lan_cache()
            # 同步到文档管理页：更新只读目录显示，并在目录非空时自动重新扫描
            doc_page = getattr(self, "doc_manager_page", None)
            if doc_page is not None:
                doc_page.dir_input.setText(new_lan)
                if new_lan:
                    doc_page.scan_directory(background=True)
                    self._warm_lan_index_async()
                else:
                    # 清空目录：重置文档管理页状态
                    doc_page.current_dir = ""
                    doc_page.all_records.clear()
                    doc_page.filtered_records.clear()
                    doc_page._refresh_table()
                    doc_page.header_count_var.setText("共 0 条标准")
                    doc_page.status_var.setText("未配置标准共享目录，请在「设置」中指定")
            changed = True
            messages.append(f"局域网共享目录已更新：{new_lan}" if new_lan else "已关闭局域网共享目录")

        new_ledger = dialog.get_ledger_enabled()
        if new_ledger != bool(Config.LEDGER_ENABLED):
            Config.LEDGER_ENABLED = new_ledger
            self._apply_ledger_nav_visibility()
            changed = True
            messages.append("已启用标准台账" if new_ledger else "已关闭标准台账")

        if changed:
            if Config.save_settings():
                self.show_bubble("；".join(messages), "success")
            else:
                self.show_bubble(
                    "设置已在本次运行生效，但写入配置文件失败"
                    "（可能无写权限），重启后可能丢失。",
                    "error",
                )

    # ==================== 软件自动更新 ====================
    def _delayed_update_check(self):
        """延迟自动更新检测：仅在启动后30分钟内未执行过查询时才触发。"""
        if self._search_executed:
            logger.info("[更新] 已执行过查询，跳过延迟自动更新检测")
            return
        logger.info("[更新] 启动30分钟且未执行查询，开始静默自动更新检测")
        self.check_for_updates(silent=True)

    def check_for_updates(self, silent=True, parent_widget=None):
        """检查是否有新版本。

        silent=True（启动时自动检查）：仅在发现新版本时弹窗提示，出错静默忽略。
        silent=False（设置里手动检查）：无论结果如何都给出反馈。
        """
        if self._update_check_worker is not None and self._update_check_worker.isRunning():
            if not silent:
                self.show_bubble("正在检查更新，请稍候…", "info")
            return
        self._update_silent = silent
        self._update_parent = parent_widget or self
        if not silent:
            self.show_bubble("正在检查更新…", "info")
        worker = UpdateCheckWorker()
        worker.success.connect(self._on_update_check_success)
        worker.failed.connect(self._on_update_check_failed)
        self._update_check_worker = worker
        worker.start()

    def _on_update_check_success(self, info):
        remote = info.get("version", "")
        if not remote:
            if not self._update_silent:
                QMessageBox.information(self._update_parent, "检查更新", "未获取到有效的版本信息，请稍后再试。")
            return
        if is_newer_version(remote, APP_VERSION):
            self._prompt_update(info)
        elif not self._update_silent:
            QMessageBox.information(self._update_parent, "检查更新", f"当前已是最新版本（v{APP_VERSION}）。")

    def _on_update_check_failed(self, err):
        logger.warning("软件更新: 检查更新失败: %s", err)
        if not self._update_silent:
            # 区分不同的错误情况给用户更友好的提示
            if "JSON" in str(err) or "格式" in str(err):
                QMessageBox.warning(
                    self._update_parent, "检查更新",
                    f"版本信息格式有误：\n{err}\n\n这通常是因为服务器上的版本文件有问题，您当前版本仍然可以正常使用。"
                )
            else:
                # 网络问题或其他错误，也提示当前版本是最新版，让用户放心
                QMessageBox.information(
                    self._update_parent, "检查更新",
                    f"检查更新时遇到网络问题：\n{err}\n\n您当前已是最新版本（v{APP_VERSION}），请稍后再试。"
                )

    def _prompt_update(self, info):
        """发现新版本时询问用户是否立即更新。"""
        self._last_update_info = info  # 保存更新信息用于后续使用
        remote = info.get("version", "")
        notes = (info.get("notes") or "").strip() or "（本次更新暂无说明）"
        if len(notes) > 800:
            notes = notes[:800] + "…"
        box = QMessageBox(self._update_parent)
        box.setWindowTitle("发现新版本")
        box.setIcon(QMessageBox.Information)
        box.setText(f"检测到新版本 v{remote}（当前 v{APP_VERSION}），是否立即更新？")
        box.setInformativeText(f"更新内容：\n{notes}")
        update_btn = box.addButton("立即更新", QMessageBox.AcceptRole)
        box.addButton("以后再说", QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is not update_btn:
            return

        # 源码运行环境无法自替换 exe：直接弹出手动更新链接
        if not getattr(sys, "frozen", False):
            self._show_manual_update("当前为源码运行环境，无法自动替换程序。")
            return

        prefer_setup = is_installed_app()
        if not build_download_urls(info, prefer_setup=prefer_setup):
            self._show_manual_update("该版本未提供可自动下载的安装包。")
            return

        self._start_update_download(info)

    def _escape_cmd_path(self, p: str) -> str:
        """对写入 bat 的路径做 cmd 元字符转义。"""
        p = p.replace('%', '%%')
        p = p.replace('^', '^^')
        return p

    def _run_hidden_bat_and_quit(self, bat_body: str, done_tip: str):
        """写入隐藏批处理、启动后退出本程序。"""
        import subprocess
        bat_path = os.path.join(TEMP_DIR, "_update_标准查询工具.bat")
        try:
            with open(bat_path, "w", encoding="gbk", errors="replace") as f:
                f.write(bat_body)
        except Exception as e:
            logger.warning("准备更新脚本失败: %s", e)
            self._show_manual_update(f"准备更新脚本失败：{e}")
            return False

        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        try:
            subprocess.Popen(
                ["cmd", "/c", bat_path],
                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                startupinfo=startupinfo,
                close_fds=True,
            )
        except Exception as e:
            logger.warning("启动更新程序失败: %s", e)
            self._show_manual_update(f"启动更新程序失败：{e}")
            return False

        QMessageBox.information(self._update_parent, "软件更新", done_tip)
        QApplication.quit()
        return True

    def _start_update_download(self, info):
        """按安装类型下载：安装版 → setup.exe；绿色版 → zip。"""
        prefer_setup = is_installed_app()
        if prefer_setup:
            raw_asset = (
                info.get("setup_asset_name")
                or info.get("asset_name")
                or "StandardQueryTool_setup.exe"
            )
        else:
            raw_asset = info.get("asset_name") or "StandardQueryTool_update.zip"

        asset_name = os.path.basename(raw_asset)
        if not re.match(r'^[\w\-. ]+$', asset_name):
            asset_name = (
                "StandardQueryTool_setup.exe" if prefer_setup else "StandardQueryTool_update.zip"
            )
        dest = os.path.join(TEMP_DIR, asset_name)

        urls = build_download_urls(info, prefer_setup=prefer_setup)
        if not urls:
            self._show_manual_update("未获取到可用的下载地址。")
            return

        tip = "正在下载安装包，请稍候…" if prefer_setup else "正在下载更新包，请稍候…"
        dlg = QProgressDialog(tip, "取消", 0, 100, self._update_parent)
        dlg.setWindowTitle("软件更新")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumWidth(380)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        self._update_progress_dlg = dlg

        sha_map = info.get("sha256") or {}
        expected_sha = sha_map.get(os.path.basename(dest).lower(), "")
        worker = UpdateDownloadWorker(urls, dest, expected_sha256=expected_sha)
        worker.progress.connect(dlg.setValue)
        worker.success.connect(self._on_update_downloaded)
        worker.failed.connect(self._on_update_download_failed)
        dlg.canceled.connect(worker.stop)
        self._update_dl_worker = worker
        worker.start()
        dlg.show()

    def _show_manual_update(self, reason=""):
        """弹出可复制链接的手动更新对话框（网盘兜底），并居中显示。"""
        lanzou_url = None
        lanzou_pwd = None
        if hasattr(self, '_last_update_info') and self._last_update_info:
            lanzou_url = self._last_update_info.get("lanzou_url")
            lanzou_pwd = self._last_update_info.get("lanzou_pwd")

        dlg = ManualUpdateDialog(reason, lanzou_url, lanzou_pwd, self._update_parent or self)
        dlg.adjustSize()
        try:
            parent = self._update_parent or self
            center = parent.frameGeometry().center()
            geo = dlg.frameGeometry()
            geo.moveCenter(center)
            dlg.move(geo.topLeft())
        except Exception:
            pass
        dlg.exec_()

    def _on_update_downloaded(self, path):
        if self._update_progress_dlg is not None:
            self._update_progress_dlg.close()
            self._update_progress_dlg = None

        # 安装包：静默运行 setup 升级
        if is_setup_package_path(path) or (
            path.lower().endswith(".exe") and is_installed_app()
        ):
            self._launch_setup_updater_and_exit(path)
            return

        # 绿色包 zip：解压覆盖后重启稳定名 exe
        if path.lower().endswith(".zip"):
            self._launch_portable_updater_and_exit(path)
            return

        # 兼容旧单文件 exe 直替
        self._launch_updater_and_exit(path)

    def _launch_portable_updater_and_exit(self, zip_path):
        """绿色包更新：解压到临时目录 → 剥掉外层文件夹 → 覆盖到程序目录 → 启动稳定名 exe。"""
        temp_dir = os.path.realpath(TEMP_DIR)
        real_zip = os.path.realpath(zip_path)
        if not real_zip.startswith(temp_dir + os.sep) and real_zip != temp_dir:
            self._show_manual_update("更新包路径异常，请手动更新。")
            return

        exe_dir = os.path.dirname(sys.executable)
        exe_name_only = os.path.basename(sys.executable)
        safe_zip = self._escape_cmd_path(zip_path)
        safe_exe_dir = self._escape_cmd_path(exe_dir)
        safe_fallback = self._escape_cmd_path(sys.executable)
        stable_name = STABLE_EXE_NAME

        ps_zip = safe_zip.replace("'", "''")
        ps_dir = safe_exe_dir.replace("'", "''")
        ps_stable = stable_name.replace("'", "''")
        ps_fallback = safe_fallback.replace("'", "''")

        ps_cmd = (
            "powershell -NoProfile -ExecutionPolicy Bypass -Command "
            "\"$ErrorActionPreference='Stop'; "
            "$z='%s'; $app='%s'; $stable='%s'; $fallback='%s'; "
            "$stage=Join-Path $env:TEMP ('sqt_upd_'+[guid]::NewGuid().ToString('N')); "
            "New-Item -ItemType Directory -Path $stage | Out-Null; "
            "try { "
            # 清理 PyInstaller 5/Win7 时代遗留在根目录的运行时，避免新版 _internal 之外出现 DLL 影子
            "$pats=@('api-ms-win-*.dll','ucrtbase.dll','python3*.dll','*.pyd','base_library.zip','VCRUNTIME140*.dll','MSVCP140*.dll','libcrypto-1_1*.dll','libssl-1_1*.dll','libffi-7.dll','libopenblas*.dll','sqt.png'); "
            "foreach($p in $pats){ Get-ChildItem -LiteralPath $app -Filter $p -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue }; "
            "$oldDirs=@('PyQt5','PIL','certifi','lxml','pymupdf','fontTools','numpy'); "
            "foreach($d in $oldDirs){ $t=Join-Path $app $d; if(Test-Path -LiteralPath $t){ Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue } }; "
            "Expand-Archive -LiteralPath $z -DestinationPath $stage -Force; "
            "$root=$stage; $kids=@(Get-ChildItem -LiteralPath $stage -Force); "
            "if ($kids.Count -eq 1 -and $kids[0].PSIsContainer) { $root=$kids[0].FullName }; "
            "$rc = Start-Process -FilePath robocopy.exe -ArgumentList @($root,$app,'/E','/IS','/IT','/NFL','/NDL','/NJH','/NJS','/NC','/NS','/NP') -Wait -PassThru; "
            "if ($rc.ExitCode -ge 8) { throw ('robocopy failed: '+$rc.ExitCode) }; "
            "$launch=Join-Path $app $stable; "
            "if (-not (Test-Path -LiteralPath $launch)) { "
            "  $cands=@(Get-ChildItem -LiteralPath $app -Filter 'StandardQueryTool*.exe' -File | "
            "    Where-Object { $_.Name -notmatch 'setup|installer' } | Sort-Object Name -Descending); "
            "  if ($cands.Count -gt 0) { $launch=$cands[0].FullName } else { $launch=$fallback } "
            "}; "
            "Start-Process -FilePath $launch; "
            "Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue "
            "} catch { exit 1 } "
            "finally { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }\""
        ) % (ps_zip, ps_dir, ps_stable, ps_fallback)

        bat = "".join([
            "@echo off\r\n",
            "set /a n=0\r\n",
            ":wait\r\n",
            "ping -n 2 127.0.0.1 >nul 2>&1\r\n",
            'tasklist /FI "IMAGENAME eq %s" 2>nul | find /I "%s" >nul\r\n' % (
                exe_name_only, exe_name_only
            ),
            "if not errorlevel 1 goto wait\r\n",
            ":retry\r\n",
            "set /a n+=1\r\n",
            "if %n% gtr 60 goto fail\r\n",
            "%s\r\n" % ps_cmd,
            "if errorlevel 1 goto retry\r\n",
            'del /f /q "%s" >nul 2>&1\r\n' % safe_zip,
            "goto end\r\n",
            ":fail\r\n",
            'echo 更新失败，请手动更新。\r\n',
            "pause\r\n",
            ":end\r\n",
            'del "%~f0"\r\n',
        ])
        self._run_hidden_bat_and_quit(
            bat,
            "更新包已下载完成，程序将关闭并自动完成升级，稍后会自动重新打开。",
        )

    def _launch_setup_updater_and_exit(self, setup_path):
        """安装版更新：静默运行 Inno setup，完成后启动稳定名主程序。"""
        temp_dir = os.path.realpath(TEMP_DIR)
        real_setup = os.path.realpath(setup_path)
        if not real_setup.startswith(temp_dir + os.sep) and real_setup != temp_dir:
            self._show_manual_update("更新包路径异常，请手动更新。")
            return

        exe_dir = os.path.dirname(sys.executable)
        exe_name_only = os.path.basename(sys.executable)
        safe_setup = self._escape_cmd_path(setup_path)
        launch_candidates = [
            os.path.join(exe_dir, STABLE_EXE_NAME),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "标准查询工具", STABLE_EXE_NAME),
            os.path.join(exe_dir, exe_name_only),
        ]
        seen = set()
        launch_list = []
        for p in launch_candidates:
            rp = os.path.normpath(p)
            key = rp.lower()
            if key not in seen:
                seen.add(key)
                launch_list.append(self._escape_cmd_path(rp))

        bat_lines = [
            "@echo off\r\n",
            "set /a n=0\r\n",
            ":wait\r\n",
            "ping -n 2 127.0.0.1 >nul 2>&1\r\n",
            'tasklist /FI "IMAGENAME eq %s" 2>nul | find /I "%s" >nul\r\n' % (
                exe_name_only, exe_name_only
            ),
            "if not errorlevel 1 goto wait\r\n",
            ":retry\r\n",
            "set /a n+=1\r\n",
            "if %n% gtr 30 goto fail\r\n",
            '"%s" /VERYSILENT /NORESTART /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS\r\n' % safe_setup,
            "if errorlevel 1 (\r\n",
            "  ping -n 3 127.0.0.1 >nul 2>&1\r\n",
            "  goto retry\r\n",
            ")\r\n",
        ]
        for p in launch_list:
            bat_lines.append('if exist "%s" (\r\n' % p)
            bat_lines.append('  start "" "%s"\r\n' % p)
            bat_lines.append("  goto cleanup\r\n")
            bat_lines.append(")\r\n")
        bat_lines.extend([
            "goto cleanup\r\n",
            ":fail\r\n",
            'echo 安装更新失败，请手动运行安装包。\r\n',
            "pause\r\n",
            ":cleanup\r\n",
            'del /f /q "%s" >nul 2>&1\r\n' % safe_setup,
            'del "%~f0"\r\n',
        ])
        self._run_hidden_bat_and_quit(
            "".join(bat_lines),
            "安装包已下载完成，程序将关闭并由安装程序静默升级，完成后会自动重新打开。",
        )

    def _on_update_download_failed(self, err):
        if self._update_progress_dlg is not None:
            self._update_progress_dlg.close()
            self._update_progress_dlg = None
        logger.warning("下载更新失败: %s", err)
        self._show_manual_update(f"自动下载更新失败：{err}")

    def _launch_updater_and_exit(self, new_exe_path):
        """单文件 exe：等旧进程退出后 move 覆盖并重启。"""
        temp_dir = os.path.realpath(TEMP_DIR)
        real_new = os.path.realpath(new_exe_path)
        if not real_new.startswith(temp_dir + os.sep) and real_new != temp_dir:
            self._show_manual_update("更新包路径异常，请手动更新。")
            return

        target = sys.executable
        safe_new = self._escape_cmd_path(new_exe_path)
        safe_target = self._escape_cmd_path(target)
        bat = (
            "@echo off\r\n"
            "set /a n=0\r\n"
            ":retry\r\n"
            "ping -n 2 127.0.0.1 >nul 2>&1\r\n"
            + ('move /y "%s" "%s" >nul 2>&1\r\n' % (safe_new, safe_target))
            + "if not errorlevel 1 goto done\r\n"
            "set /a n+=1\r\n"
            "if %n% lss 60 goto retry\r\n"
            ":done\r\n"
            + ('start "" "%s"\r\n' % safe_target)
            + 'del "%~f0"\r\n'
        )
        self._run_hidden_bat_and_quit(
            bat,
            "更新包已下载完成，程序将关闭并自动完成升级，稍后会自动重新打开。",
        )

    def open_detail_page(self, standard_info):
        """在浏览器中打开详情页"""
        url = standard_info.detail_url or standard_info.download_url
        if url and url.startswith(('http://', 'https://')):
            import webbrowser
            webbrowser.open(url)
            self.show_bubble(f"已打开详情页: {standard_info.std_no}", "success")
    
    def update_progress(self, message):
        """信号驱动进度条：根据 spider 发来的实时消息映射为百分比。
        单本查询：消息 → 百分比（不依赖计时器，与真实检索步骤同步）。"""
        logger.debug("搜索进度: %s", message)
        # 进度百分比映射
        if not hasattr(self, '_progress_map'):
            # 与 Config.PRIMARY_SITES 同步，映射到约 15%～80%
            n = max(len(Config.PRIMARY_SITES), 1)
            self._progress_map = {
                site['name']: int(15 + i * (65 / max(n - 1, 1)))
                for i, site in enumerate(Config.PRIMARY_SITES)
            }
        # 替代查询：在最后阶段推进
        if "正在查询替代规范" in message:
            self.progress_bar.setValue(90)
            self.progress_label.setText(message)
            return
        # 下载进度不覆盖搜索进度
        if "下载中" in message:
            return
        # 从 "正在检索: X" 提取站点名
        if message.startswith("正在检索: "):
            site = message[6:]
            pct = self._progress_map.get(site)
            if pct is not None:
                # 存量进度（已搜过的站点比当前进度高则保留）
                cur = self.progress_bar.value()
                if pct > cur:
                    self.progress_bar.setValue(pct)
            self.progress_label.setText(message)
    
    def update_download_progress(self, message):
        """更新下载进度显示"""
        # 在进度标签中显示下载进度
        self.progress_label.setText(message)
    
    def update_site_status(self, index, status):
        """更新站点状态"""
        if 0 <= index < len(self.status_dots):
            self.status_dots[index].status = status
            self.status_dots[index].update()
            self._refresh_site_status_caption()
    
    def show_bubble(self, message, msg_type="info"):
        """显示气泡提示"""
        self.bubble.show_message(message, msg_type)

    def _cleanup_temp_files(self):
        """关闭时清理 update 目录、_MEI* 残留，并将当前日志归档。"""
        try:
            # 关闭日志文件句柄释放 Windows 文件锁
            for h in logger.handlers[:]:
                if isinstance(h, logging.FileHandler):
                    h.close()
                    logger.removeHandler(h)

            # 将当前日志归档为 standard_query_tool1.log（保留上一次的日志供排查）
            log_path = os.path.join(TEMP_DIR, "standard_query_tool.log")
            log1_path = os.path.join(TEMP_DIR, "standard_query_tool1.log")
            if os.path.exists(log_path):
                try:
                    if os.path.exists(log1_path):
                        os.remove(log1_path)
                    os.rename(log_path, log1_path)
                except Exception:
                    pass

            # 清理 update 目录全部内容（下载的安装包、bat 脚本等）
            if os.path.isdir(UPDATE_DIR):
                for entry in os.listdir(UPDATE_DIR):
                    try:
                        p = os.path.join(UPDATE_DIR, entry)
                        if os.path.isfile(p):
                            os.remove(p)
                        elif os.path.isdir(p):
                            shutil.rmtree(p, ignore_errors=True)
                    except Exception:
                        pass

            # 清理预览静默下载缓存
            self._cleanup_preview_cache()

            # 清理 onefile 残留的 _MEIxxxx 临时目录（在 TEMP_DIR 和系统临时目录中都搜）
            # 跳过当前进程正在使用的 _MEIPASS，否则会删除运行中程序自己的解包文件
            current_mei = os.path.realpath(getattr(sys, '_MEIPASS', '') or '')
            count = 0
            for base_dir in (TEMP_DIR, os.path.realpath(tempfile.gettempdir())):
                for d in glob.glob(os.path.join(base_dir, "_MEI*")):
                    if current_mei and os.path.realpath(d) == current_mei:
                        continue
                    try:
                        shutil.rmtree(d, ignore_errors=True)
                        count += 1
                    except Exception:
                        pass
            if count:
                logger.info(f"关闭清理：删除 {count} 个 _MEI* 临时目录")
        except Exception as e:
            logger.warning(f"清理临时文件异常: {e}")

    def _retire_search_worker(self):
        """断开当前 SearchWorker 全部 UI 信号并 cancel；保留引用防 GC。
        返回 True 表示线程仍在跑（调用方应换新 spider）。"""
        worker = getattr(self, 'search_worker', None)
        if worker is None:
            return False
        for signal, slot in (
            (worker.progress, self.update_progress),
            (worker.site_status, self.update_site_status),
            (worker.finished, self.on_search_finished),
            (worker.error, self.on_search_error),
            (worker.cancelled, self.on_search_cancelled),
            (worker.result_found, self.on_result_found),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        still_running = worker.isRunning()
        if still_running:
            worker.cancel()
        retired = getattr(self, '_retired_workers', None)
        if retired is None:
            self._retired_workers = []
            retired = self._retired_workers
        retired.append(worker)
        self._retired_workers = [w for w in retired if w.isRunning() or w is worker]
        self.search_worker = None
        return still_running

    def _is_batch_search_running(self):
        """批量检索进行中（由 on_batch_search / _restore_batch_ui 显式维护）。"""
        return bool(getattr(self, '_batch_search_running', False))

    def _is_current_search_worker(self, w=None):
        """是否为当前代数的 SearchWorker（忽略退役后仍投递的 Queued 信号）。"""
        if w is None:
            w = self.sender()
        if w is None:
            return True
        return getattr(w, 'gen', None) == getattr(self, '_search_gen', None)

    def _stop_background_workers(self, wait_ms=5000):
        """关闭窗口前尽量停止搜索/下载/本地/探测线程。"""
        self._batch_cancelled = True
        self._batch_search_running = False
        self._search_cancelled = True
        self._cancel_silent_preview_worker(wait=True)
        self._retire_search_worker()
        self._invalidate_detect()
        all_workers = []
        for w in list(getattr(self, '_retired_workers', [])):
            if w.isRunning():
                all_workers.append(('search', w))
                w.wait(wait_ms)
        bw = getattr(self, '_batch_worker', None)
        if bw is not None and bw.isRunning():
            bw.cancel()
            bw.wait(wait_ms)
        for w in list(getattr(self, '_download_workers', [])):
            if w.isRunning():
                all_workers.append(('download', w))
                try:
                    w.spider.progress_updated.disconnect(w.progress.emit)
                except (TypeError, RuntimeError):
                    pass
                w.wait(wait_ms)
        for attr in ('_local_search_worker', '_detect_worker', '_lan_warm_worker'):
            w = getattr(self, attr, None)
            if w is not None and w.isRunning():
                all_workers.append((attr, w))
                if hasattr(w, 'cancel'):
                    w.cancel()
                w.wait(wait_ms)
        for name, w in all_workers:
            if w.isRunning():
                logger.warning("关闭时仍有线程未退出: %s（网络阻塞中），由进程退出强制结束", name)

    def closeEvent(self, event):
        """窗口关闭时停止后台线程、清理临时文件并退出。"""
        try:
            self._stop_background_workers()
        except Exception as e:
            logger.warning(f"关闭时停止线程异常: {e}")
        self._cleanup_temp_files()
        super().closeEvent(event)
