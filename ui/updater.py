# -*- coding: utf-8 -*-
"""软件版本与自动更新。"""
import os
import sys
import json
import re
import ssl
import hashlib
import urllib.request
from app_env import logger
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from . import theme as _theme

APP_VERSION = "1.6.0"

# 主程序稳定文件名（不含版本号），便于绿色包在线覆盖更新
STABLE_EXE_NAME = "StandardQueryTool.exe"

# 版本检测源：按顺序读取多个清单，取其中最高的版本（兼容国内镜像滞后）
VERSION_MANIFEST_URLS = (
    "https://raw.githubusercontent.com/lovethx2018/StandardQueryTool/main/version.json",
    "https://gitee.com/lovethx2024/app-update-store/raw/main/version.json",
)

# 手动更新（网盘）兜底地址：当自动下载/替换失败时，弹窗提示用户手动下载。
LANZOU_URL = "https://wwawy.lanzoub.com/b0j1kkfcd"
LANZOU_PWD = "1234"


def _parse_version(v: str):
    """将版本字符串解析为可比较的整数元组，如 'v1.2.3' -> (1, 2, 3)。"""
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts) if parts else (0,)


def is_newer_version(remote: str, local: str) -> bool:
    """判断 remote 版本是否比 local 更新（元组比较，自动处理不同段数）。"""
    return _parse_version(remote) > _parse_version(local)


def _updater_urlopen(url: str, timeout: int = 15):
    """打开一个 URL（禁用系统代理；始终强制 SSL 证书验证，不降级）。

    专用于访问 Gitee 托管的 version.json 与 Gitee Releases 安装包直链。
    自动更新下载的是可执行文件，MITM 攻击可替换整个程序，
    因此必须始终验证证书，不允许降级。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": "biaozhun-chaxin-updater",
        "Accept": "*/*",
    })
    # 兼容Windows 7的SSL上下文设置
    try:
        context = ssl.create_default_context()
    except Exception:
        # 如果create_default_context在旧系统上失败，使用兼容的设置
        context = ssl.SSLContext(ssl.PROTOCOL_TLS)
        context.options |= ssl.OP_NO_SSLv2
        context.options |= ssl.OP_NO_SSLv3
        context.set_default_verify_paths()
        context.verify_mode = ssl.CERT_REQUIRED
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
    )
    return opener.open(req, timeout=timeout)


def _sha256_file(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_sha256(path, expected):
    """校验下载文件；expected 为空时跳过（兼容尚未提供哈希的旧清单）。"""
    if not expected:
        return True
    actual = _sha256_file(path)
    if actual.lower() != str(expected).strip().lower():
        raise Exception(
            "更新包 SHA-256 校验失败：期望 %s，实际 %s" % (expected, actual)
        )
    return True


def _normalize_manifest(data: dict) -> dict:
    """把 version.json 归一化为统一的版本信息 dict。

    建议字段：
        version / tag / notes
        download_url / download_urls / asset_name   → 绿色包 zip
        setup_url / setup_urls / setup_asset_name   → 安装包 setup.exe
    """
    ver = str(data.get("version") or data.get("tag") or "").strip().lstrip("vV").strip()
    tag = str(data.get("tag") or (f"v{ver}" if ver else "")).strip()

    def _listify(val):
        if not val:
            return []
        if isinstance(val, str):
            return [val]
        return list(val)

    dls = _listify(data.get("download_urls"))
    setups = _listify(data.get("setup_urls"))
    # sha256: {"文件名": "64位小写hex"}；也兼容 {"url": "..."}（按 basename 归一）
    sha_map = {}
    raw_sha = data.get("sha256")
    if isinstance(raw_sha, dict):
        for name, digest in raw_sha.items():
            key = os.path.basename(str(name or "").strip()).lower()
            val = str(digest or "").strip().lower()
            if key and re.fullmatch(r"[0-9a-f]{64}", val):
                sha_map[key] = val
    return {
        "version": ver,
        "tag": tag,
        "notes": data.get("notes") or data.get("body") or "",
        "html_url": data.get("html_url") or "",
        "download_url": (data.get("download_url") or "").strip(),
        "download_urls": [str(u).strip() for u in dls if str(u).strip()],
        "asset_name": (data.get("asset_name") or "").strip(),
        "setup_url": (data.get("setup_url") or "").strip(),
        "setup_urls": [str(u).strip() for u in setups if str(u).strip()],
        "setup_asset_name": (data.get("setup_asset_name") or "").strip(),
        "sha256": sha_map,
        "lanzou_url": data.get("lanzou_url") or LANZOU_URL,
        "lanzou_pwd": data.get("lanzou_pwd") or LANZOU_PWD,
    }


def _gitee_urls(*groups) -> list:
    """收集并去重 Gitee 下载地址，保持先后顺序。"""
    seen = set()
    unique = []
    for group in groups:
        for u in group:
            s = str(u or "").strip()
            if s and "gitee.com" in s.lower() and s not in seen:
                seen.add(s)
                unique.append(s)
    return unique


def build_download_urls(info: dict, prefer_setup: bool = False) -> list:
    """构造自动下载候选地址。

    prefer_setup=True 时优先安装包（setup.exe），否则优先绿色包 zip。
    """
    zip_urls = _gitee_urls(
        info.get("download_urls") or [],
        [info.get("download_url") or ""],
    )
    setup_urls = _gitee_urls(
        info.get("setup_urls") or [],
        [info.get("setup_url") or ""],
    )
    if prefer_setup:
        return setup_urls or zip_urls
    return zip_urls or setup_urls


def is_setup_package_path(path: str) -> bool:
    """根据文件名判断是否为 Inno 安装包。"""
    name = os.path.basename(path or "").lower()
    return name.endswith(".exe") and ("setup" in name or "installer" in name)


def is_installed_app(exe_path: str = None) -> bool:
    """判断当前是否为 Inno 安装版（相对绿色免安装目录）。"""
    exe = os.path.realpath(exe_path or sys.executable)
    exe_dir = os.path.dirname(exe)
    # Inno 会在安装目录写入卸载程序
    for name in ("unins000.exe", "unins001.exe"):
        if os.path.isfile(os.path.join(exe_dir, name)):
            return True
    lower = exe.lower()
    for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        root = os.environ.get(key) or ""
        if root and lower.startswith(os.path.realpath(root).lower() + os.sep):
            return True
    return False


def fetch_latest_release() -> dict:
    """从多个版本清单中取最高版本，避免镜像滞后挡住新版本。"""
    best = None
    errors = []
    for url in VERSION_MANIFEST_URLS:
        try:
            resp = _updater_urlopen(url, timeout=12)
            raw_data = resp.read()
            try:
                data = json.loads(raw_data.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as e:
                raise Exception(f"JSON 解析失败: {e}")
            info = _normalize_manifest(data)
            if not info.get("version"):
                raise Exception("version.json 缺少 version 字段")
            logger.info("[更新] 版本清单来源: %s -> v%s", url, info["version"])
            if best is None or is_newer_version(info["version"], best.get("version", "")):
                best = info
        except Exception as e:
            errors.append(f"{url}: {e}")
    if best is not None:
        return best
    raise Exception("；".join(errors) if errors else "无可用版本清单")


class UpdateCheckWorker(QThread):
    """后台查询 Gitee 托管的最新版本，避免阻塞界面。"""
    success = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def run(self):
        try:
            self.success.emit(fetch_latest_release())
        except Exception as e:
            self.failed.emit(str(e))


class UpdateDownloadWorker(QThread):
    """后台下载新版本文件（zip/setup），按 Content-Length 上报进度。

    校验：文件头（MZ/PK）、完整性、最小体积，以及清单提供的 SHA-256。
    """
    progress = pyqtSignal(int)
    success = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, urls, dest, expected_sha256=None, parent=None):
        super().__init__(parent)
        if isinstance(urls, str):
            urls = [urls]
        self.urls = [u for u in urls if u]
        self.dest = dest
        self.expected_sha256 = (expected_sha256 or "").strip().lower()
        self._stopped = False
        self._file_type = "exe"  # 默认 exe，下载时根据文件头自动识别

    def stop(self):
        self._stopped = True

    def run(self):
        errors = []
        for idx, url in enumerate(self.urls):
            if self._stopped:
                return
            try:
                self.progress.emit(0)
                self._download_one(url)
                self.success.emit(self.dest)
                return
            except Exception as e:
                if self._stopped:
                    return
                logger.warning("下载源失败[%d] %s : %s", idx + 1, url, e)
                errors.append(f"源{idx + 1}: {e}")
                continue
        self.failed.emit("；".join(errors) if errors else "无可用下载源")

    def _download_one(self, url):
        resp = _updater_urlopen(url, timeout=30)
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        checked_header = False
        with open(self.dest, "wb") as f:
            while True:
                if self._stopped:
                    raise Exception("已取消")
                chunk = resp.read(65536)
                if not chunk:
                    break
                if not checked_header:
                    # 支持 exe（MZ头）和 zip（PK头）两种格式
                    if chunk.startswith(b"MZ"):
                        self._file_type = "exe"
                    elif chunk.startswith(b"PK"):
                        self._file_type = "zip"
                    else:
                        raise Exception("返回内容不是有效的可执行文件或压缩包")
                    checked_header = True
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    self.progress.emit(int(downloaded * 100 / total))
        if not checked_header:
            raise Exception("未收到任何数据")
        if total > 0 and downloaded < total:
            raise Exception(f"下载不完整（{downloaded}/{total} 字节）")
        if downloaded < 1024:
            raise Exception(f"文件过小（{downloaded} 字节），可能不是有效更新包")
        if self.expected_sha256:
            try:
                _verify_sha256(self.dest, self.expected_sha256)
            except Exception:
                try:
                    os.remove(self.dest)
                except OSError:
                    pass
                raise
        else:
            logger.warning("[更新] 清单未提供 SHA-256，已跳过完整性校验: %s", os.path.basename(self.dest))
        self.progress.emit(100)


class ManualUpdateDialog(QDialog):
    """自动更新失败时的手动更新提示弹窗：展示可复制的网盘下载链接与提取密码。"""

    def __init__(self, reason="", lanzou_url=None, lanzou_pwd=None, parent=None):
        super().__init__(parent)
        self.lanzou_url = lanzou_url or LANZOU_URL
        self.lanzou_pwd = lanzou_pwd or LANZOU_PWD
        self.setWindowTitle("手动更新")
        self.setMinimumWidth(480)
        self.setStyleSheet(f"QDialog {{ background: {_theme.DIALOG_BG}; }}")
        self._build_ui(reason)

    def _build_ui(self, reason):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(12)

        title = QLabel("自动更新未能完成")
        title.setStyleSheet(
            f"font-size: 16px; font-weight: bold; color: {_theme.PRIMARY};"
        )
        layout.addWidget(title)

        base_tip = "您可以通过下面的网盘链接手动下载并安装最新版本："
        tip_text = f"{reason}\n\n{base_tip}" if reason else base_tip
        tip = QLabel(tip_text)
        tip.setWordWrap(True)
        tip.setStyleSheet(f"font-size: 13px; color: {_theme.TEXT};")
        layout.addWidget(tip)

        field_style = (
            f"QLineEdit {{ background: #ffffff; border: 1px solid {_theme.BORDER};"
            f" border-radius: 6px; padding: 6px 8px; font-size: 13px; color: #222; }}"
        )

        # 下载链接行
        link_row = QHBoxLayout()
        link_row.setSpacing(8)
        link_lbl = QLabel("下载链接：")
        link_lbl.setFixedWidth(64)
        link_lbl.setStyleSheet(f"font-size: 13px; color: {_theme.MUTED};")
        self.link_edit = QLineEdit(self.lanzou_url)
        self.link_edit.setReadOnly(True)
        self.link_edit.setCursorPosition(0)
        self.link_edit.setStyleSheet(field_style)
        link_row.addWidget(link_lbl)
        link_row.addWidget(self.link_edit, 1)
        layout.addLayout(link_row)

        # 提取密码行
        pwd_row = QHBoxLayout()
        pwd_row.setSpacing(8)
        pwd_lbl = QLabel("提取密码：")
        pwd_lbl.setFixedWidth(64)
        pwd_lbl.setStyleSheet(f"font-size: 13px; color: {_theme.MUTED};")
        self.pwd_edit = QLineEdit(self.lanzou_pwd)
        self.pwd_edit.setReadOnly(True)
        self.pwd_edit.setFixedWidth(110)
        self.pwd_edit.setStyleSheet(field_style)
        pwd_row.addWidget(pwd_lbl)
        pwd_row.addWidget(self.pwd_edit)
        pwd_row.addStretch(1)
        layout.addLayout(pwd_row)

        # 反馈提示
        self._feedback = QLabel("")
        self._feedback.setStyleSheet(f"font-size: 12px; color: {_theme.SUCCESS};")
        layout.addWidget(self._feedback)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        primary_style = (
            f"QPushButton {{ background: {_theme.PRIMARY}; color: #ffffff; border: none;"
            f" border-radius: 6px; padding: 8px 14px; font-size: 13px; }}"
            f" QPushButton:hover {{ background: {_theme.PRIMARY_HOVER}; }}"
            f" QPushButton:pressed {{ background: {_theme.PRIMARY_PRESSED}; }}"
        )
        close_style = (
            f"QPushButton {{ background: #ffffff; color: {_theme.MUTED}; border: 1px solid #c0c8d0;"
            f" border-radius: 6px; padding: 8px 18px; font-size: 13px; }}"
            f" QPushButton:hover {{ background: {_theme.IMPORT_BG}; border-color: {_theme.PRIMARY}; }}"
        )

        copy_link_btn = QPushButton("复制链接")
        copy_link_btn.setCursor(Qt.PointingHandCursor)
        copy_link_btn.setStyleSheet(primary_style)
        copy_link_btn.clicked.connect(self._copy_link)

        copy_all_btn = QPushButton("复制链接和密码")
        copy_all_btn.setCursor(Qt.PointingHandCursor)
        copy_all_btn.setStyleSheet(primary_style)
        copy_all_btn.clicked.connect(self._copy_all)

        open_btn = QPushButton("打开链接")
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.setStyleSheet(primary_style)
        open_btn.clicked.connect(self._open_link)

        close_btn = QPushButton("关闭")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(close_style)
        close_btn.clicked.connect(self.accept)

        btn_row.addWidget(copy_link_btn)
        btn_row.addWidget(copy_all_btn)
        btn_row.addWidget(open_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _copy_link(self):
        QApplication.clipboard().setText(self.lanzou_url)
        self._feedback.setText("✓ 已复制下载链接到剪贴板")

    def _copy_all(self):
        QApplication.clipboard().setText(f"链接：{self.lanzou_url}\n密码：{self.lanzou_pwd}")
        self._feedback.setText("✓ 已复制链接和密码到剪贴板")

    def _open_link(self):
        import webbrowser
        webbrowser.open(self.lanzou_url)
        self._feedback.setText(f"✓ 已在浏览器打开链接，提取密码：{self.lanzou_pwd}")
