# -*- coding: utf-8 -*-
"""PDF 页渲染（独立进程调用，避免 PyMuPDF 与 Qt 同进程崩溃）。

用法:
  python -m ui.pdf_page_renderer count <pdf_path>
  python -m ui.pdf_page_renderer page <pdf_path> <page_index> <out_png> [scale]
  python -m ui.pdf_page_renderer probe

冻结 exe:
  StandardQueryTool.exe --pdf-page-renderer count|page|probe ...
"""
from __future__ import print_function

import json
import os
import sys


def _load_fitz():
    """必须使用 PyMuPDF 1.24+ 的正式模块名；旧 fitz 别名已废弃。"""
    import pymupdf as fitz
    return fitz


def _probe():
    fitz = _load_fitz()
    print(json.dumps({"ok": True, "probe": getattr(fitz, "__version__", "fitz")}, ensure_ascii=False))
    return 0


def _count(pdf_path):
    fitz = _load_fitz()
    doc = fitz.open(pdf_path)
    try:
        n = int(doc.page_count or 0)
    finally:
        doc.close()
    print(json.dumps({"ok": True, "pages": n}, ensure_ascii=False))
    return 0


def _page(pdf_path, page_index, out_png, scale):
    fitz = _load_fitz()
    doc = fitz.open(pdf_path)
    try:
        n = int(doc.page_count or 0)
        idx = int(page_index)
        if idx < 0 or idx >= n:
            print(json.dumps({"ok": False, "error": "page out of range", "pages": n}))
            return 2
        page = doc.load_page(idx)
        pix = page.get_pixmap(matrix=fitz.Matrix(float(scale), float(scale)), alpha=False)
        parent = os.path.dirname(out_png)
        if parent:
            os.makedirs(parent, exist_ok=True)
        pix.save(out_png)
        print(json.dumps({"ok": True, "pages": n, "out": out_png}, ensure_ascii=False))
    finally:
        doc.close()
    return 0


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print(json.dumps({"ok": False, "error": "no args"}))
        return 1
    cmd = argv[0]
    try:
        if cmd == "probe":
            return _probe()
        if cmd == "count" and len(argv) >= 2:
            return _count(argv[1])
        if cmd == "page" and len(argv) >= 4:
            scale = argv[4] if len(argv) >= 5 else "3.0"
            return _page(argv[1], argv[2], argv[3], scale)
        print(json.dumps({"ok": False, "error": "bad args"}))
        return 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
