# -*- mode: python ; coding: utf-8 -*-
# 构建目标：Windows 10 / 11 x64，Python 3.13（PyInstaller 6.x）

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('sqt.ico', '.'),
    ],
    hiddenimports=[
        'PyQt5', 'PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets',
        'requests', 'urllib3', 'certifi', 'chardet', 'idna', 'charset_normalizer',
        'bs4', 'bs4.builder', 'bs4.builder._lxml', 'bs4.builder._htmlparser',
        'bs4.element', 'bs4.dammit',
        'lxml', 'lxml.etree', 'lxml.html',
        'pathlib',
        'openpyxl', 'openpyxl.workbook', 'openpyxl.worksheet',
        'openpyxl.cell', 'openpyxl.styles', 'openpyxl.utils',
        'xlrd',
        'PIL', 'PIL.Image',
        'pymupdf',
        'ui.pdf_page_renderer',
        'http_client', 'utils', 'models', 'config', 'app_env',
        'ui', 'ui.main_window', 'ui.widgets', 'ui.workers', 'ui.theme',
        'ui.settings_dialog', 'ui.document_manager', 'ui.updater', 'ui.preview_dialog',
        'ui.gb_announcement_page', 'ui.standard_ledger_page',
        'spiders', 'spiders.spider', 'spiders.spider_manager', 'spiders.base_spider',
        'spiders.data_processor',
        'spiders.openstd_spider', 'spiders.openstd_downloader',
        'spiders.samr_spider', 'spiders.samr_downloader',
        'spiders.csres_spider', 'spiders.jtst_spider', 'spiders.mwr_spider',
        'spiders.aijsti_spider',
        'spiders.nrsis_spider', 'spiders.nrsis_downloader',
        'spiders.webfree_downloader', 'spiders.gxzl_pdf_downloader',
        'spiders.chinese_name_query', 'spiders.civil_domain', 'spiders.gb_announcement',
        'dns_fallback',
        'ledger', 'ledger.store',
        'query_history', 'query_history.store',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# 瘦身：QtWidgets 应用不需要 QML/Quick/OpenGL 软渲染与 Qt 翻译。
# 保留 qwindows（GUI）、qoffscreen（CLI/无头测试）、imageformats、styles。
_DROP_BINARIES = {
    'PyQt5/Qt5/bin/opengl32sw.dll',
    'PyQt5/Qt5/bin/libEGL.dll',
    'PyQt5/Qt5/bin/libGLESv2.dll',
    'PyQt5/Qt5/bin/d3dcompiler_47.dll',
    'PyQt5/Qt5/bin/Qt5Qml.dll',
    'PyQt5/Qt5/bin/Qt5QmlModels.dll',
    'PyQt5/Qt5/bin/Qt5Quick.dll',
    'PyQt5/Qt5/bin/Qt5DBus.dll',
    'PyQt5/Qt5/bin/Qt5WebSockets.dll',
    'PyQt5/Qt5/bin/Qt5Network.dll',
    'PyQt5/Qt5/plugins/platforms/qminimal.dll',
    'PyQt5/Qt5/plugins/platforms/qwebgl.dll',
}


def _dest(entry):
    return str(entry[0]).replace('\\', '/')


a.binaries = [e for e in a.binaries if _dest(e) not in _DROP_BINARIES]
a.datas = [e for e in a.datas if not _dest(e).startswith('PyQt5/Qt5/translations/')]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='StandardQueryTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='StandardQueryTool_v1.5.9_win10_win11_py313',
)