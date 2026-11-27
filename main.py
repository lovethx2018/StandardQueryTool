# -*- coding: utf-8 -*-
"""本地查询工具 —— 唯一可执行入口。
作者：黄加林
运行：python main.py

冻结（PyInstaller）后预览 PDF 会再启本 exe 作为子进程，必须用
--pdf-page-renderer 分发，不能依赖 python -m / -c（会被忽略并再次打开主界面）。
"""
import app_env  # noqa: F401  最先导入以完成环境初始化（编码/Qt日志/sip告警屏蔽）
import sys
import os

# 须在导入 PyQt5 / MainWindow 之前处理，避免渲染子进程拉起整套 GUI
if len(sys.argv) >= 2 and sys.argv[1] == "--pdf-page-renderer":
    from ui.pdf_page_renderer import main as _pdf_renderer_main
    sys.exit(_pdf_renderer_main(sys.argv[2:]))

from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import QFont, QIcon, QPixmap
from ui.main_window import MainWindow


class ContextMenuLocalizer(QObject):
    _MAP = (
        ("select all", "全选"),
        ("undo", "撤销"),
        ("redo", "重做"),
        ("cut", "剪切"),
        ("copy", "复制"),
        ("paste", "粘贴"),
        ("delete", "删除"),
    )

    def eventFilter(self, obj, event):
        try:
            if event.type() == QEvent.ContextMenu and isinstance(
                obj, (QLineEdit, QTextEdit, QPlainTextEdit)
            ):
                menu = obj.createStandardContextMenu()
                if menu is not None:
                    for act in menu.actions():
                        clean = act.text().replace("&", "").strip().lower()
                        if not clean:
                            continue
                        for en, zh in self._MAP:
                            if en in clean:
                                act.setText(zh)
                                break
                    menu.setAttribute(Qt.WA_DeleteOnClose)
                    menu.exec_(event.globalPos())
                    return True
        except Exception:
            return False
        return super().eventFilter(obj, event)


def get_icon_path():
    # 优先 ico（体积小、多尺寸）；png 仅作源码目录兼容回退
    icon_files = ['sqt.ico', 'sqt.png']
    base_dir = sys._MEIPASS if hasattr(sys, '_MEIPASS') else os.path.dirname(os.path.abspath(__file__))
    
    for icon_file in icon_files:
        path = os.path.join(base_dir, icon_file)
        if os.path.exists(path):
            return path
    return ""


def load_app_icon():
    """加载应用程序图标，确保在所有平台上都能正常工作"""
    icon_path = get_icon_path()
    if not icon_path or not os.path.exists(icon_path):
        return QIcon()
    
    # 使用QPixmap加载图标，然后创建QIcon，这样更兼容
    pixmap = QPixmap(icon_path)
    if not pixmap.isNull():
        icon = QIcon()
        icon.addPixmap(pixmap)
        return icon
    return QIcon()


def main():
    """主函数"""
    # 设置环境变量，解决一些显示问题
    os.environ['QT_AUTO_SCREEN_SCALE_FACTOR'] = '1'
    
    # 设置 Windows AppUserModelID，使任务栏图标正确显示
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('standard.query.tool.v1.6.1')
    except Exception:
        pass
    
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    # 设置应用程序图标 - 使用改进的加载方法
    app_icon = load_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    # 右键菜单中文化（PyQt5 无内置中文 .qm，改用事件过滤器重建中文标准菜单）
    ctx_localizer = ContextMenuLocalizer(app)
    app.installEventFilter(ctx_localizer)
    
    # 设置默认字体：主字体 Microsoft YaHei，保留系统符号字体作为回退
    font = QFont()
    font.setFamilies(["Microsoft YaHei", "Segoe UI Symbol"])
    font.setPointSize(9)
    app.setFont(font)
    
    window = MainWindow()
    # 主窗口也设置图标，确保窗口左上角图标显示
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)
    window.show()

    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
