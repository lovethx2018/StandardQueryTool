# -*- coding: utf-8 -*-
"""运行环境初始化：编码修复、Qt 日志与 sip 弃用告警屏蔽、模块级 logger、目录与可写兜底。
必须在导入 PyQt5 之前最先导入本模块，使告警过滤器等副作用先行生效。
"""
import sys
import os
import logging
import warnings

# 尽早安装 DNS 兜底（jtst.mot.gov.cn 等子域本机解析失败时走 DoH/静态 IP）
try:
    from dns_fallback import install_dns_fallback
    install_dns_fallback()
except Exception:
    pass

# 配置 certifi 证书路径（兼容 PyInstaller 打包）
if getattr(sys, 'frozen', False):
    # 打包后运行
    base_path = sys._MEIPASS
    # 尝试设置证书路径，但不要让错误阻止程序启动
    try:
        cert_path = os.path.join(base_path, 'certifi', 'cacert.pem')
        if os.path.exists(cert_path):
            os.environ['REQUESTS_CA_BUNDLE'] = cert_path
            os.environ['SSL_CERT_FILE'] = cert_path
            logging.info("证书路径已设置: %s", cert_path)
        else:
            logging.warning("证书文件不存在: %s", cert_path)
    except Exception as e:
        logging.warning("设置证书路径时出错: %s", e)


# ==================== 控制台与旧环境兼容处理 ====================


# 修复 Windows 控制台中文乱码：强制 stdout/stderr 使用 UTF-8
if sys.platform == 'win32':
    try:
        # 兼容不同 Python 版本对 stdout/stderr 包装的差异
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        else:
            # 无 reconfigure 的旧版解释器回退（当前目标 Python 3.13 不会走到这里）
            import io
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    except Exception:
        pass

# 屏蔽 libpng 的 iCCP sRGB profile 警告（来自 C 库的 stderr 输出，无害但刷屏）
os.environ.setdefault('QT_LOGGING_RULES', '*.warning=false;qt.qpa.img=false')

# 在导入 PyQt5 之前先屏蔽 sip 库的 DeprecationWarning，避免 sipPyTypeDict() 等弃用告警刷屏
# 这些警告来自 sip C 扩展模块，用 message 正则匹配最可靠
warnings.filterwarnings('ignore', message='.*sipPyTypeDict.*')
warnings.filterwarnings('ignore', message='.*sip.*deprecated.*', category=DeprecationWarning)


# ==================== Qt 插件路径兜底 ====================
# Windows 下程序所在路径含非 ASCII 字符（如中文目录）时，Qt 5.15 依据 exe 路径推导
# 插件目录会失败，QApplication 初始化时会弹出阻塞式 “could not find the qt platform
# plugin” 错误框（无控制台时表现为启动卡死）。显式写入插件目录可绕过：环境变量由
# Qt 按 Unicode 读取，不受 ANSI 路径转换影响。
def _setup_qt_plugin_path():
    existing = os.environ.get('QT_QPA_PLATFORM_PLUGIN_PATH')
    if existing and os.path.isdir(existing):
        return
    candidates = []
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', '')
        candidates.extend([
            os.path.join(base, 'PyQt5', 'Qt5', 'plugins'),
            os.path.join(base, 'PyQt5', 'plugins'),
        ])
    try:
        import PyQt5
        candidates.append(os.path.join(os.path.dirname(PyQt5.__file__), 'Qt5', 'plugins'))
    except Exception:
        pass
    for path in candidates:
        if path and os.path.isdir(path):
            os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = path
            os.environ.setdefault('QT_PLUGIN_PATH', path)
            break


_setup_qt_plugin_path()


# ==================== 应用目录设置 ====================
# 便携（安装目录可写）：配置/数据跟随程序目录，换盘即迁移；
# 安装版（Program Files 不可写）：配置与数据回落 %APPDATA%，临时文件回落 %TEMP%。
def _get_app_dir():
    """返回应用程序所在的根目录（便携版：exe 所在目录；源码运行：main.py 所在目录）。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def _is_writable_dir(path):
    """实际写入探针，避免只看 os.access（UAC/ACL 场景不可靠）。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe.tmp")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


def _user_data_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "StandardQueryTool")


def _user_temp_dir():
    import tempfile
    return os.path.join(os.path.realpath(tempfile.gettempdir()), "StandardQueryTool_temp")


APP_DIR = _get_app_dir()
APP_DIR_WRITABLE = _is_writable_dir(APP_DIR)

if APP_DIR_WRITABLE:
    TEMP_DIR = os.path.join(APP_DIR, "_temp")
    DATA_DIR = os.path.join(APP_DIR, "_data")      # 历史/台账等持久数据
    CONFIG_DIR = os.path.join(APP_DIR, "config")
    UPDATE_DIR = os.path.join(APP_DIR, "update")
else:
    TEMP_DIR = _user_temp_dir()
    DATA_DIR = _user_data_dir()
    CONFIG_DIR = os.path.join(DATA_DIR, "config")
    UPDATE_DIR = os.path.join(TEMP_DIR, "update")

for _dir in (TEMP_DIR, DATA_DIR, CONFIG_DIR, UPDATE_DIR):
    try:
        os.makedirs(_dir, exist_ok=True)
    except Exception:
        pass


def _migrate_legacy_user_data():
    """把旧版持久数据从安装目录 _temp 迁到 DATA_DIR（目标已存在则不覆盖）。
    便携版升级：_temp/ledger|history → _data/；安装版：→ %APPDATA%。"""
    import shutil
    src_dir = os.path.join(APP_DIR, "_temp")
    if os.path.realpath(src_dir) == os.path.realpath(DATA_DIR):
        return
    for name in ("query_history_cache.json", "standard_ledger.json"):
        src = os.path.join(src_dir, name)
        dst = os.path.join(DATA_DIR, name)
        if not os.path.isfile(src) or os.path.exists(dst):
            continue
        try:
            shutil.copy2(src, dst)
        except Exception:
            pass


# ==================== 模块级日志配置 ====================
logger = logging.getLogger("app")
# PDF 预览每翻一页都会拉起渲染子进程；若子进程也以 mode='w' 打开同一日志，
# 会把主程序正在写的日志截断。子进程只输出到 stderr（由父进程管道捕获）。
_is_renderer_subprocess = (
    (len(sys.argv) >= 2 and sys.argv[1] == '--pdf-page-renderer')
    or os.path.basename(sys.argv[0] or '').lower() == 'pdf_page_renderer.py'
)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    _handler.setFormatter(_formatter)
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)  # 提高到 INFO 级别获取更多调试信息
    logger.propagate = False

    # 同时也添加文件日志输出（方便打包后调试）
    if not _is_renderer_subprocess:
        try:
            log_file = os.path.join(TEMP_DIR, "standard_query_tool.log")
            _file_handler = logging.FileHandler(log_file, encoding='utf-8', mode='w')
            _file_handler.setFormatter(_formatter)
            logger.addHandler(_file_handler)
            logger.info("应用程序启动，日志文件位于: %s", log_file)
        except Exception:
            # 忽略文件日志初始化错误，避免影响应用程序运行
            pass

# 旧版持久数据迁移（便携升级 _temp -> _data；安装版 -> %APPDATA%）
_migrate_legacy_user_data()
