# -*- coding: utf-8 -*-
"""标准预览对话框：协筑翻页 / 官方在线预览 / 临时 PDF 确认后再保存。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from io import BytesIO
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QMessageBox,
                             QWidget, QScrollArea)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QEvent
from PyQt5.QtGui import QPixmap, QImage
from app_env import logger
from . import theme as _theme

# 准备中省略号轮播色
_DOT_COLORS = ("#2563EB", "#059669", "#D97706")


try:
    import requests
except ImportError:
    requests = None

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    Image = None

# 预览模式
MODE_GXZL_PAGES = "gxzl_pages"
MODE_ONLINE_URL = "online_url"
MODE_TEMP_PDF = "temp_pdf"

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0


def _pdf_renderer_argv(*args):
    """PDF 渲染子进程命令行。

    开发态：python -m ui.pdf_page_renderer ...
    冻结 exe：不能使用 -m/-c（会被忽略并再次打开主界面），改走
    StandardQueryTool.exe --pdf-page-renderer ...
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--pdf-page-renderer"] + list(args)
    return [sys.executable, "-m", "ui.pdf_page_renderer"] + list(args)


def _pdf_renderer_available():
    """子进程能否 import PyMuPDF（不在 GUI 进程加载 fitz，避免与 Qt 冲突崩溃）。"""
    try:
        r = subprocess.run(
            _pdf_renderer_argv("probe"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=_CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            return False
        out = (r.stdout or "").strip()
        if "ok" in out.lower():
            return True
        # probe 输出 JSON
        for line in out.splitlines()[::-1]:
            try:
                if json.loads(line).get("ok"):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


class PdfMetaWorker(QThread):
    """子进程读取 PDF 页数。"""
    finished = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(self, pdf_path):
        super().__init__()
        self.pdf_path = pdf_path
        self._cancelled = False
        self._proc = None

    def cancel(self):
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def run(self):
        try:
            if self._cancelled:
                return
            self._proc = subprocess.Popen(
                _pdf_renderer_argv("count", self.pdf_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_CREATE_NO_WINDOW,
            )
            try:
                stdout, stderr = self._proc.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                # 注意：不可先调 cancel()，否则 _cancelled 会吞掉超时错误提示
                user_cancelled = self._cancelled
                try:
                    self._proc.kill()
                    self._proc.communicate(timeout=2)
                except Exception:
                    pass
                if not user_cancelled:
                    self.failed.emit("读取页数超时")
                return
            if self._cancelled:
                return
            line = (stdout or "").strip().splitlines()
            line = line[-1] if line else ""
            data = json.loads(line) if line else {}
            if data.get("ok") and int(data.get("pages") or 0) > 0:
                self.finished.emit(int(data["pages"]))
            else:
                err = data.get("error") or (stderr or "").strip() or f"exit={self._proc.returncode}"
                self.failed.emit(err)
        except Exception as e:
            if not self._cancelled:
                self.failed.emit(str(e))


class PdfPageRenderWorker(QThread):
    """子进程渲染单页 PDF → PNG 文件。"""
    page_loaded = pyqtSignal(int, str)  # page_num, png_path
    load_failed = pyqtSignal(int, str)

    def __init__(self, pdf_path, page_num, out_png, scale=3.0):
        super().__init__()
        self.pdf_path = pdf_path
        self.page_num = page_num
        self.out_png = out_png
        self.scale = scale
        self._cancelled = False
        self._proc = None

    def cancel(self):
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def run(self):
        try:
            if self._cancelled:
                return
            self._proc = subprocess.Popen(
                _pdf_renderer_argv(
                    "page",
                    self.pdf_path,
                    str(self.page_num),
                    self.out_png,
                    str(self.scale),
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_CREATE_NO_WINDOW,
            )
            try:
                stdout, stderr = self._proc.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                user_cancelled = self._cancelled
                try:
                    self._proc.kill()
                    self._proc.communicate(timeout=2)
                except Exception:
                    pass
                if not user_cancelled:
                    self.load_failed.emit(self.page_num, "渲染超时")
                return
            if self._cancelled:
                return
            if self._proc.returncode == 0 and os.path.isfile(self.out_png) and os.path.getsize(self.out_png) > 0:
                self.page_loaded.emit(self.page_num, self.out_png)
            else:
                line = (stdout or "").strip().splitlines()
                err = ""
                if line:
                    try:
                        err = json.loads(line[-1]).get("error") or ""
                    except Exception:
                        err = line[-1]
                err = err or (stderr or "").strip() or f"exit={self._proc.returncode}"
                self.load_failed.emit(self.page_num, err)
        except Exception as e:
            if not self._cancelled:
                self.load_failed.emit(self.page_num, str(e))


class PageCountWorker(QThread):
    """异步获取标准总页数，避免阻塞 UI。"""
    finished = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(self, std_no, base_url):
        super().__init__()
        self.std_no = std_no
        self.base_url = base_url
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            if self._cancelled:
                return
            if requests is None:
                self.failed.emit("requests 库未安装，无法加载页面数据")
                return
            url = f"{self.base_url}/api/gxist-standard/standardstd/read-pages"
            params = {"page": 0, "no": self.std_no}
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36",
                "Referer": f"{self.base_url}/standard/details",
                "Cookie": "i18n_redirected=zh",
            }
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if self._cancelled:
                return
            if resp.status_code != 200:
                self.failed.emit(f"HTTP {resp.status_code}")
                return
            data = resp.json()
            pages = 0
            if isinstance(data, dict):
                pages = data.get('data', 0) or data.get('total', 0) or data.get('pages', 0) or 0
            elif isinstance(data, int):
                pages = data
            pages = int(pages) if pages else 0
            if self._cancelled:
                return
            if pages > 0:
                self.finished.emit(pages)
            else:
                self.failed.emit("无法获取标准页数")
        except Exception as e:
            if not self._cancelled:
                self.failed.emit(str(e))


class PageLoadWorker(QThread):
    """单页预览图加载工作线程"""
    page_loaded = pyqtSignal(int, object)  # (页码, 图片数据)
    load_failed = pyqtSignal(int, str)     # (页码, 错误信息)

    def __init__(self, std_no, page_num, base_url):
        super().__init__()
        self.std_no = std_no
        self.page_num = page_num
        self.base_url = base_url
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            if self._cancelled:
                return
            if requests is None:
                self.load_failed.emit(self.page_num, "requests 库未安装，无法加载页面")
                return
            url = f"{self.base_url}/api/gxist-standard/standardstd/read-image"
            params = {"no": self.std_no, "page": self.page_num}

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36",
                "Referer": f"{self.base_url}/standard/details",
                "Cookie": "i18n_redirected=zh",
            }

            resp = requests.get(url, params=params, headers=headers, timeout=30)
            if self._cancelled:
                return

            if resp.status_code == 200 and len(resp.content) > 1000:
                self.page_loaded.emit(self.page_num, resp.content)
            else:
                self.load_failed.emit(self.page_num, f"HTTP {resp.status_code}")
        except Exception as e:
            if not self._cancelled:
                self.load_failed.emit(self.page_num, str(e))


class StandardPreviewDialog(QDialog):
    """标准预览对话框（三种模式）。

    mode:
      - gxzl_pages: 协筑逐页图片预览 + 后台静默拼 PDF
      - online_url: 打开官方在线预览 + 后台静默下 PDF
      - temp_pdf:   静默下 PDF，仅在本弹窗内翻页预览
    """
    download_requested = pyqtSignal()   # 用户确认保存到下载目录
    discard_requested = pyqtSignal()    # 关闭/取消，需清理临时文件

    # 窗内 PDF 渲染倍率（相对 72dpi）；越大越清晰，文件/耗时也更大
    PDF_RENDER_SCALE = 3.0

    def __init__(self, std_no, std_name, parent=None, mode=MODE_GXZL_PAGES,
                 online_url=None, detail_url=None, source_label=""):
        super().__init__(parent)
        self.std_no = std_no or ""
        self.std_name = std_name or ""
        self.mode = mode or MODE_GXZL_PAGES
        self.online_url = (online_url or "").strip()
        self.detail_url = (detail_url or "").strip()
        self.source_label = source_label or ""
        self.base_url = "https://bz.gxzl.org.cn"

        self.current_page = 0
        self.total_pages = 0
        self.page_cache = {}
        self.loading_workers = {}
        self._page_count_worker = None
        self._closing = False
        self._accepted_download = False
        self._discard_emitted = False
        self._silent_ready = False
        self._silent_path = ""
        self._silent_error = ""
        self._status_label = None
        self._pdf_path = ""
        self._pdf_png_dir = ""
        self._pdf_meta_worker = None
        self._pdf_page_workers = {}
        self._base_pixmap = QPixmap()
        self._zoom = 1.0       # 相对「适应宽度」的倍率
        self._fit_zoom = 1.0
        self._prep_anim_phase = 0
        self._prep_anim_timer = QTimer(self)
        self._prep_anim_timer.setInterval(380)
        self._prep_anim_timer.timeout.connect(self._tick_preparing_animation)

        self.init_ui()
        if self.mode == MODE_GXZL_PAGES:
            self.load_page_count()
        else:
            self._setup_non_gxzl_view()

    def _emit_discard(self):
        if self._accepted_download or self._discard_emitted:
            return
        self._discard_emitted = True
        try:
            self.discard_requested.emit()
        except RuntimeError:
            pass

    def init_ui(self):
        """初始化UI"""
        self.setWindowTitle(f"标准预览 - {self.std_no}")
        # 去掉「?」帮助按钮，增加最小化 / 最大化
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
        )
        self.setMinimumSize(900, 700)
        self.resize(1000, 800)

        layout = QVBoxLayout(self)

        title_text = self.std_no or ""
        if self.std_name:
            title_text = f"{title_text}  {self.std_name}" if title_text else self.std_name
        title_label = QLabel(title_text)
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setWordWrap(True)
        title_label.setStyleSheet(
            f"QLabel {{ color: {_theme.PRIMARY}; font-size: 18px; font-weight: bold; }}"
        )
        layout.addWidget(title_label)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(False)
        self.scroll_area.setAlignment(Qt.AlignCenter)
        self.scroll_area.setStyleSheet("QScrollArea { background-color: #f0f0f0; border: 1px solid #ccc; }")
        self.scroll_area.viewport().installEventFilter(self)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(200, 200)
        self.image_label.setStyleSheet("background-color: #f0f0f0;")
        self.image_label.setTextFormat(Qt.RichText)
        self.image_label.setText("正在获取页数...")
        self.image_label.setWordWrap(True)
        self.scroll_area.setWidget(self.image_label)
        layout.addWidget(self.scroll_area, stretch=1)

        # 底栏状态/假进度条已去掉：中间区域已有「正在准备预览文件」提示
        self._status_label = None
        self.progress_bar = None

        self.nav_widget = QWidget()
        nav_layout = QHBoxLayout(self.nav_widget)
        nav_layout.setContentsMargins(0, 0, 0, 0)

        self.prev_btn = QPushButton("上一页")
        self.prev_btn.setMinimumHeight(40)
        self.prev_btn.clicked.connect(self.prev_page)
        self.prev_btn.setEnabled(False)
        nav_layout.addWidget(self.prev_btn)

        self.page_label = QLabel("第 - 页 / 共 - 页")
        self.page_label.setAlignment(Qt.AlignCenter)
        self.page_label.setMinimumWidth(150)
        nav_layout.addWidget(self.page_label)

        self.next_btn = QPushButton("下一页")
        self.next_btn.setMinimumHeight(40)
        self.next_btn.clicked.connect(self.next_page)
        self.next_btn.setEnabled(False)
        nav_layout.addWidget(self.next_btn)

        layout.addWidget(self.nav_widget)

        btn_layout = QHBoxLayout()

        self.online_btn = QPushButton("在线预览")
        self.online_btn.setMinimumHeight(40)
        self.online_btn.setVisible(False)
        self.online_btn.clicked.connect(self.open_online_preview)
        self.online_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {_theme.PRIMARY};
                color: white;
                font-weight: bold;
                border-radius: 5px;
                padding: 0 16px;
            }}
            QPushButton:hover {{ background-color: {_theme.PRIMARY_HOVER}; }}
        """)
        btn_layout.addWidget(self.online_btn)

        self.download_btn = QPushButton("下载到本地")
        self.download_btn.setMinimumHeight(40)
        self.download_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {_theme.SUCCESS};
                color: white;
                font-weight: bold;
                border-radius: 5px;
            }}
            QPushButton:hover {{
                background-color: #347C4E;
            }}
            QPushButton:disabled {{
                background-color: #cccccc;
            }}
        """)
        self.download_btn.clicked.connect(self.accept_download)
        self.download_btn.setEnabled(False)
        btn_layout.addWidget(self.download_btn)

        self.close_btn = QPushButton("关闭")
        self.close_btn.setMinimumHeight(40)
        self.close_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

    def _preparing_html(self, phase=0):
        """「正在准备预览文件」+ 三色轮播省略号。"""
        dots = []
        for i in range(3):
            c = _DOT_COLORS[(i + phase) % len(_DOT_COLORS)]
            dots.append(
                f'<span style="color:{c};font-size:22px;font-weight:bold;">·</span>'
            )
        return (
            '<div style="text-align:center;color:#333;font-size:16px;line-height:1.6;">'
            f'正在准备预览文件{"".join(dots)}'
            '<br><span style="font-size:13px;color:#888;">'
            '就绪后将在本窗口内翻页查看；也可「下载到本地」。'
            '</span></div>'
        )

    def _start_preparing_animation(self):
        self._prep_anim_phase = 0
        self.image_label.setTextFormat(Qt.RichText)
        self.image_label.setText(self._preparing_html(0))
        if not self._prep_anim_timer.isActive():
            self._prep_anim_timer.start()

    def _stop_preparing_animation(self):
        if self._prep_anim_timer.isActive():
            self._prep_anim_timer.stop()

    def _tick_preparing_animation(self):
        if self._closing or not self._prep_anim_timer.isActive():
            return
        if not self._base_pixmap.isNull():
            self._stop_preparing_animation()
            return
        self._prep_anim_phase = (self._prep_anim_phase + 1) % 3
        self.image_label.setText(self._preparing_html(self._prep_anim_phase))

    def _setup_non_gxzl_view(self):
        """online_url / temp_pdf：隐藏翻页，展示说明。"""
        self.nav_widget.setVisible(False)
        if self.mode == MODE_ONLINE_URL:
            self.online_btn.setVisible(True)
            self.image_label.setTextFormat(Qt.PlainText)
            self.image_label.setText(
                "可点击「在线预览」用系统浏览器查看标准全文。\n"
                "后台正在准备文件，就绪后可「下载到本地」。"
            )
            if self.online_url:
                QTimer.singleShot(200, self.open_online_preview)
        else:
            self._start_preparing_animation()

    @staticmethod
    def _sanitize_user_message(message):
        """去掉可能暴露下载站点的措辞，仅保留通用提示。"""
        if not message:
            return ""
        text = str(message)
        leak_tokens = (
            "协筑", "东盟", "国家标准全文", "标信", "工标网",
            "openstd", "samr", "webfree", "gxzl", "webfree.org",
            "bz.gxzl", "std.samr",
        )
        lower = text.lower()
        if any(t.lower() in lower for t in leak_tokens):
            if "失败" in text or "错误" in text or "异常" in text:
                return "准备失败，请稍后重试"
            return "正在准备预览文件…"
        return text

    def set_silent_progress(self, message):
        """主窗口转发静默下载进度（底栏已去掉，保留接口兼容）。"""
        return

    def set_silent_ready(self, ok, path_or_error):
        """静默下载结束。"""
        if self._closing:
            return
        if ok:
            self._silent_ready = True
            self._silent_path = path_or_error
            self._silent_error = ""
            self.download_btn.setEnabled(True)
            if self.mode == MODE_GXZL_PAGES:
                pass
            elif self.mode == MODE_TEMP_PDF:
                if self._start_embedded_pdf_preview(path_or_error):
                    self._stop_preparing_animation()
                    self.image_label.setTextFormat(Qt.PlainText)
                    self.image_label.setText("正在解析预览文件…")
                else:
                    self._stop_preparing_animation()
                    self.image_label.setTextFormat(Qt.PlainText)
                    self.image_label.setText(
                        "预览文件已准备完成（窗内预览不可用）。\n"
                        "可「下载到本地」保存到下载目录。"
                    )
            elif self.mode == MODE_ONLINE_URL:
                self.image_label.setTextFormat(Qt.PlainText)
                self.image_label.setText(
                    "在线预览可随时打开。\n"
                    "文件已就绪，点击「下载到本地」保存。"
                )
        else:
            self._silent_ready = False
            self._silent_path = ""
            self._silent_error = path_or_error or "准备失败"
            self.download_btn.setEnabled(False)
            self._stop_preparing_animation()
            safe_err = self._sanitize_user_message(self._silent_error)
            self.image_label.setTextFormat(Qt.PlainText)
            if self.mode == MODE_ONLINE_URL and self.online_url:
                self.image_label.setText(
                    "静默下载失败，仍可「在线预览」。\n"
                    f"原因：{safe_err}"
                )
            else:
                self.image_label.setText(f"准备失败：{safe_err}")

    def _start_embedded_pdf_preview(self, pdf_path):
        """启动子进程渲染 PDF 页（GUI 进程不加载 fitz，避免与 Qt 同进程崩溃）。"""
        self._stop_pdf_workers()
        self._cleanup_pdf_png_dir()
        if not pdf_path or not os.path.isfile(pdf_path):
            return False
        # 不在 UI 线程同步 probe PyMuPDF（可卡到数秒）；由 PdfMetaWorker 失败信号兜底
        try:
            parent = os.path.dirname(pdf_path) or tempfile.gettempdir()
            self._pdf_png_dir = tempfile.mkdtemp(prefix="sqt_pdf_prev_", dir=parent)
            self._pdf_path = pdf_path
            self.page_cache = {}
            self.total_pages = 0
            self.current_page = 0
            self._stop_preparing_animation()
            self.image_label.setTextFormat(Qt.PlainText)
            self.image_label.setText("正在解析预览文件…")
            worker = PdfMetaWorker(pdf_path)
            worker.finished.connect(self._on_pdf_meta_ready)
            worker.failed.connect(self._on_pdf_meta_failed)
            worker.finished.connect(worker.deleteLater)
            worker.failed.connect(worker.deleteLater)
            self._pdf_meta_worker = worker
            worker.start()
            return True
        except Exception as e:
            logger.warning(f"启动 PDF 预览失败: {e}")
            self._cleanup_pdf_png_dir()
            return False

    def _on_pdf_meta_ready(self, pages):
        if self._closing:
            return
        self._pdf_meta_worker = None
        self.total_pages = int(pages or 0)
        if self.total_pages <= 0:
            self._on_pdf_meta_failed("页数为 0")
            return
        self.nav_widget.setVisible(True)
        self.update_page_label()
        self.prev_btn.setEnabled(False)
        self.next_btn.setEnabled(self.total_pages > 1)
        self.load_page(0)

    def _on_pdf_meta_failed(self, message):
        if self._closing:
            return
        self._pdf_meta_worker = None
        logger.warning(f"PDF 页数解析失败: {message}")
        self.image_label.setText(
            "临时 PDF 已准备完成（窗内预览失败）。\n"
            f"原因：{message}\n"
            "可「下载到本地」保存。"
        )

    def _stop_pdf_workers(self, wait_ms=0):
        """取消 PDF 渲染线程；默认不等待，避免卡住 UI。"""
        w = self._pdf_meta_worker
        self._pdf_meta_worker = None
        if w is not None:
            try:
                try:
                    w.finished.disconnect(self._on_pdf_meta_ready)
                except (TypeError, RuntimeError):
                    pass
                try:
                    w.failed.disconnect(self._on_pdf_meta_failed)
                except (TypeError, RuntimeError):
                    pass
                w.cancel()
                if wait_ms > 0 and w.isRunning():
                    w.wait(wait_ms)
            except Exception:
                pass
        for page_num, w in list(self._pdf_page_workers.items()):
            try:
                try:
                    w.page_loaded.disconnect(self._on_pdf_page_rendered)
                except (TypeError, RuntimeError):
                    pass
                try:
                    w.load_failed.disconnect(self._on_pdf_page_failed)
                except (TypeError, RuntimeError):
                    pass
                w.cancel()
                if wait_ms > 0 and w.isRunning():
                    w.wait(wait_ms)
            except Exception:
                pass
        self._pdf_page_workers.clear()

    def _cleanup_pdf_png_dir(self):
        d = self._pdf_png_dir
        self._pdf_png_dir = ""
        if d and os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)

    def _close_pdf_doc(self, wait_ms=0):
        """停止子进程渲染并清理 PNG 缓存目录。"""
        self._stop_pdf_workers(wait_ms=wait_ms)
        self._cleanup_pdf_png_dir()
        self._pdf_path = ""

    def open_online_preview(self):
        """系统浏览器打开（详情预热 → type=online）。"""
        if not self.online_url:
            QMessageBox.warning(self, "提示", "无在线预览地址")
            return
        try:
            detail = self.detail_url or ""
            online = self.online_url
            if detail and "openstd.samr.gov.cn" in detail:
                webbrowser.open(detail)
                if self._status_label is not None:
                    self._status_label.setText("已打开详情页预热，即将进入在线预览…")
                QTimer.singleShot(1400, lambda u=online: self._open_url_safe(u))
            else:
                self._open_url_safe(online)
                if self._status_label is not None and not self._silent_ready:
                    self._status_label.setText("已在系统浏览器打开在线预览")
        except Exception as e:
            QMessageBox.warning(self, "提示", f"无法打开浏览器：{e}")

    @staticmethod
    def _open_url_safe(url):
        try:
            if url:
                webbrowser.open(url)
        except Exception as e:
            logger.warning(f"打开浏览器失败: {e}")

    def open_temp_pdf(self):
        """已移除系统阅读器入口；保留空实现以免外部误连。"""
        return

    def load_page_count(self):
        """异步获取总页数（不阻塞 UI）"""
        if requests is None:
            self.show_error("requests 库未安装，无法加载页面数据")
            return
        self.image_label.setText("正在获取页数...")
        self.download_btn.setEnabled(False)
        worker = PageCountWorker(self.std_no, self.base_url)
        worker.finished.connect(self.on_page_count_loaded)
        worker.failed.connect(self.on_page_count_failed)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        self._page_count_worker = worker
        worker.start()

    def on_page_count_loaded(self, total_pages):
        if self._closing or not self.isVisible():
            return
        self.total_pages = total_pages
        self.update_page_label()
        self.next_btn.setEnabled(self.total_pages > 1)
        # 下载按钮等静默 PDF 就绪后再开（若已就绪则立刻开）
        if self._silent_ready:
            self.download_btn.setEnabled(True)
        self.load_page(0)

    def on_page_count_failed(self, message):
        if self._closing or not self.isVisible():
            return
        logger.error(f"获取页数失败: {message}")
        self.show_error(f"获取页数失败: {message}")
        self.image_label.setText(f"获取页数失败: {message}")

    def load_page(self, page_num):
        """加载指定页面（东盟图 / 临时 PDF 子进程渲染）。"""
        if page_num < 0 or (self.total_pages > 0 and page_num >= self.total_pages):
            return

        # 临时 PDF：缓存为 PNG 路径或 bytes
        if self.mode == MODE_TEMP_PDF and self._pdf_path:
            if page_num in self.page_cache:
                self.display_image(self.page_cache[page_num])
                return
            if page_num in self._pdf_page_workers:
                self.image_label.setText(f"正在渲染第 {page_num + 1} 页...")
                return
            if not self._pdf_png_dir:
                self.image_label.setText("预览缓存目录无效")
                return
            self.image_label.setText(f"正在渲染第 {page_num + 1} 页...")
            out_png = os.path.join(self._pdf_png_dir, f"p{page_num:04d}.png")
            worker = PdfPageRenderWorker(
                self._pdf_path, page_num, out_png, scale=self.PDF_RENDER_SCALE
            )
            worker.page_loaded.connect(self._on_pdf_page_rendered)
            worker.load_failed.connect(self._on_pdf_page_failed)
            worker.page_loaded.connect(worker.deleteLater)
            worker.load_failed.connect(worker.deleteLater)
            self._pdf_page_workers[page_num] = worker
            worker.start()
            return

        if page_num in self.page_cache:
            self.display_image(self.page_cache[page_num])
            return

        if page_num in self.loading_workers:
            return

        self.image_label.setText(f"正在加载第 {page_num + 1} 页...")

        worker = PageLoadWorker(self.std_no, page_num, self.base_url)
        worker.page_loaded.connect(self.on_page_loaded)
        worker.load_failed.connect(self.on_load_failed)
        worker.page_loaded.connect(worker.deleteLater)
        worker.load_failed.connect(worker.deleteLater)
        worker.start()

        self.loading_workers[page_num] = worker

        for offset in [-2, -1, 1, 2]:
            preload_page = page_num + offset
            if 0 <= preload_page < self.total_pages and preload_page not in self.page_cache:
                if preload_page not in self.loading_workers:
                    preload_worker = PageLoadWorker(self.std_no, preload_page, self.base_url)
                    preload_worker.page_loaded.connect(self.on_page_loaded)
                    preload_worker.load_failed.connect(self.on_load_failed)
                    preload_worker.page_loaded.connect(preload_worker.deleteLater)
                    preload_worker.load_failed.connect(preload_worker.deleteLater)
                    preload_worker.start()
                    self.loading_workers[preload_page] = preload_worker

    def _on_pdf_page_rendered(self, page_num, png_path):
        self._pdf_page_workers.pop(page_num, None)
        if self._closing:
            return
        try:
            with open(png_path, "rb") as f:
                data = f.read()
        except OSError as e:
            self._on_pdf_page_failed(page_num, str(e))
            return
        self.page_cache[page_num] = data
        if page_num == self.current_page:
            self.display_image(data)

    def _on_pdf_page_failed(self, page_num, error_msg):
        self._pdf_page_workers.pop(page_num, None)
        if self._closing:
            return
        logger.warning(f"PDF 第 {page_num + 1} 页渲染失败: {error_msg}")
        if page_num == self.current_page:
            self.image_label.setText(f"渲染失败: {error_msg}")

    def on_page_loaded(self, page_num, image_data):
        """页面加载完成"""
        if self._closing or not self.isVisible():
            self.loading_workers.pop(page_num, None)
            return
        self.page_cache[page_num] = image_data

        if page_num in self.loading_workers:
            del self.loading_workers[page_num]

        if page_num == self.current_page:
            self.display_image(image_data)

    def on_load_failed(self, page_num, error_msg):
        """页面加载失败"""
        if self._closing or not self.isVisible():
            self.loading_workers.pop(page_num, None)
            return
        logger.warning(f"第 {page_num + 1} 页加载失败: {error_msg}")

        if page_num in self.loading_workers:
            del self.loading_workers[page_num]

        if page_num == self.current_page:
            self.image_label.setText(f"加载失败: {error_msg}")

    def display_image(self, image_data):
        """显示图片；支持滚轮缩放（相对适应宽度）。"""
        self._stop_preparing_animation()
        try:
            raw = image_data
            if isinstance(image_data, str) and os.path.isfile(image_data):
                with open(image_data, "rb") as f:
                    raw = f.read()
            if not isinstance(raw, (bytes, bytearray)):
                self.image_label.setText("预览数据无效")
                return

            pixmap = QPixmap()
            if not pixmap.loadFromData(bytes(raw)):
                if not HAS_PIL:
                    self.image_label.setText("无法解码预览图")
                    return
                pil_image = Image.open(BytesIO(raw))
                if pil_image.mode == "RGBA":
                    pil_image = pil_image.convert("RGB")
                elif pil_image.mode != "RGB":
                    pil_image = pil_image.convert("RGB")
                buf = pil_image.tobytes("raw", "RGB")
                qimage = QImage(
                    buf, pil_image.width, pil_image.height, QImage.Format_RGB888
                ).copy()
                pixmap = QPixmap.fromImage(qimage)

            self._base_pixmap = pixmap
            self._update_fit_zoom()
            self._apply_zoom()
        except Exception as e:
            logger.error(f"显示图片失败: {e}")
            self.image_label.setText(f"显示图片失败: {e}")

    def _update_fit_zoom(self):
        """按预览区宽度计算「适应宽度」基准倍率。"""
        if self._base_pixmap.isNull() or self._base_pixmap.width() <= 0:
            self._fit_zoom = 1.0
            return
        vw = 800
        try:
            vw = max(100, self.scroll_area.viewport().width() - 8)
        except Exception:
            pass
        self._fit_zoom = float(vw) / float(self._base_pixmap.width())

    def _apply_zoom(self):
        if self._base_pixmap.isNull():
            return
        z = max(0.05, self._fit_zoom * self._zoom)
        w = max(1, int(self._base_pixmap.width() * z))
        h = max(1, int(self._base_pixmap.height() * z))
        scaled = self._base_pixmap.scaled(
            w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.image_label.setWordWrap(False)
        self.image_label.setText("")
        self.image_label.setPixmap(scaled)
        self.image_label.resize(scaled.size())

    def eventFilter(self, obj, event):
        """预览区滚轮：放大 / 缩小。"""
        try:
            if (
                hasattr(self, "scroll_area")
                and obj is self.scroll_area.viewport()
                and event.type() == QEvent.Wheel
                and not self._base_pixmap.isNull()
            ):
                delta = event.angleDelta().y()
                if delta > 0:
                    self._zoom = min(8.0, self._zoom * 1.15)
                elif delta < 0:
                    self._zoom = max(0.2, self._zoom / 1.15)
                else:
                    return True
                self._apply_zoom()
                return True
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def prev_page(self):
        """上一页"""
        if self.current_page > 0:
            self.current_page -= 1
            self.update_page_label()
            self.load_page(self.current_page)
            self.next_btn.setEnabled(True)
            if self.current_page == 0:
                self.prev_btn.setEnabled(False)

    def next_page(self):
        """下一页"""
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.update_page_label()
            self.load_page(self.current_page)
            self.prev_btn.setEnabled(True)
            if self.current_page == self.total_pages - 1:
                self.next_btn.setEnabled(False)

    def update_page_label(self):
        """更新页码显示"""
        self.page_label.setText(f"第 {self.current_page + 1} 页 / 共 {self.total_pages} 页")

    def accept_download(self):
        """确认下载到本地（先关弹窗，保存放到下一事件循环，避免 UI 卡死）。"""
        if not self._silent_ready:
            QMessageBox.information(self, "请稍候", "预览文件尚未准备完成，请稍后再试。")
            return
        self._accepted_download = True
        self._closing = True
        self._stop_preparing_animation()
        self.download_btn.setEnabled(False)
        # 仅发取消信号并杀掉渲染子进程，不在此长时间 wait / 复制文件
        self._stop_pdf_workers(wait_ms=0)
        self._pdf_path = ""
        self.download_requested.emit()
        self.accept()

    def reject(self):
        self._closing = True
        self._stop_preparing_animation()
        self._close_pdf_doc(wait_ms=100)
        self._emit_discard()
        super().reject()

    def show_error(self, message):
        """显示错误消息"""
        QMessageBox.critical(self, "错误", message)

    def closeEvent(self, event):
        """关闭时先停渲染子进程，再 discard（若未确认下载）。"""
        self._closing = True
        self._stop_preparing_animation()
        # 确认下载时尽快关闭；取消时稍等释放文件句柄
        wait_ms = 0 if self._accepted_download else 200
        self._close_pdf_doc(wait_ms=wait_ms)
        self._emit_discard()
        workers = list(self.loading_workers.values())
        if self._page_count_worker is not None:
            workers.append(self._page_count_worker)
        join_ms = 50 if self._accepted_download else 400
        for w in workers:
            try:
                if hasattr(w, 'cancel'):
                    w.cancel()
                for signal, slot in (
                    (getattr(w, 'page_loaded', None), self.on_page_loaded),
                    (getattr(w, 'load_failed', None), self.on_load_failed),
                    (getattr(w, 'finished', None), self.on_page_count_loaded),
                    (getattr(w, 'failed', None), self.on_page_count_failed),
                ):
                    if signal is None:
                        continue
                    try:
                        signal.disconnect(slot)
                    except (TypeError, RuntimeError):
                        pass
                if w.isRunning() and join_ms > 0:
                    w.wait(join_ms)
                w.deleteLater()
            except RuntimeError:
                pass
        self.loading_workers.clear()
        self._page_count_worker = None
        super().closeEvent(event)

    def resizeEvent(self, event):
        """窗口大小改变时按当前缩放倍率重绘。"""
        super().resizeEvent(event)
        if not self._base_pixmap.isNull() and self.mode in (
            MODE_GXZL_PAGES, MODE_TEMP_PDF
        ):
            old_fit = self._fit_zoom
            self._update_fit_zoom()
            # 保持视觉大小大致稳定：用户倍率按 fit 变化微调
            if old_fit > 0 and self._fit_zoom > 0:
                pass  # _zoom 相对 fit，窗口变宽后同样 _zoom 会更大图——符合「适应宽度」语义
            self._apply_zoom()
