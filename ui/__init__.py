# -*- coding: utf-8 -*-
"""界面模块。

保持空包初始化：冻结后 PDF 渲染子进程会 import ui.pdf_page_renderer，
若此处再导入 MainWindow/PyQt5，会无谓加重子进程且易引发副作用。
请使用 ``from ui.main_window import MainWindow`` 等显式导入。
"""
