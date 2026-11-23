# -*- coding: utf-8 -*-
"""全源验收：每一个查询源必须能查，每一个下载源必须能探测（可选真下载）。

用法（项目根目录）:
  py -3 scripts/smoke_sources.py
  py -3 scripts/smoke_sources.py --with-download

退出码：
  配置中声明的全部查询源 PASS 且全部下载源探测 PASS → 0
  任一核心/备用查询源或任一下载源失败 → 1

真下载（--with-download）对每个可探测源再拉一份 PDF 头校验；
自然资源网/东盟默认限页（环境变量 NRSIS_MAX_PAGES / GXZL 冒烟路径）。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 自然资源网/东盟冒烟默认只合并前几页，避免拖死联调
os.environ.setdefault("NRSIS_MAX_PAGES", "2")
os.environ.setdefault("GXZL_MAX_PAGES", "2")

import app_env  # noqa: F401  DNS 兜底 + 日志

from config import Config
from models import StandardInfo
from spiders.spider import SpiderEngine
from spiders.base_spider import SpiderSearchError


# 每个 parser 用最可能命中的编号；与 PRIMARY_SITES 对齐
QUERY_KEYWORDS = {
    "std_samr": "GB/T 1.1-2020",
    "openstd": "GB/T 1.1-2020",
    "csres": "GB/T 1.1-2020",
    "aijsti": "GB/T 1.1-2020",
    "jtst": "JTG B01-2014",
    "mwr": "SL 252-2017",
    "nrsis": "TD/T 1110-2025",
}

# 下载探测/真下载默认编号（国标覆盖前几源；nrsis 另用行业标）
DOWNLOAD_STD_BY_PARSER = {
    "std_samr": "GB/T 1.1-2020",
    "openstd": "GB/T 1.1-2020",
    "webfree": "GB/T 1.1-2020",
    "gxzl": "GB/T 1.1-2020",
    # 行业标 mnr_kfs 阅读器当前常 500；选带 openstd 图标的国标走通 nrsis→openstd 链路
    "nrsis": "GB/Z 40524-2021",
}


def _site_by_parser(parser: str):
    return next((s for s in Config.PRIMARY_SITES if s.get("parser") == parser), None)


def check_query_source(engine: SpiderEngine, parser: str, name: str, keyword: str):
    site = _site_by_parser(parser)
    if not site:
        return {"ok": False, "detail": "配置缺失", "elapsed": 0}
    spider = engine.spider_manager.get(parser)
    if spider is None:
        return {"ok": False, "detail": "爬虫未注册", "elapsed": 0}

    t0 = time.time()
    try:
        results = spider.search(keyword, site) or []
        elapsed = time.time() - t0
        if not results:
            return {
                "ok": False,
                "detail": "0 条结果",
                "elapsed": elapsed,
                "count": 0,
            }
        top = results[0]
        return {
            "ok": True,
            "detail": "%s | %s | %s" % (
                top.std_no,
                top.status or "-",
                (top.std_name or "")[:36],
            ),
            "elapsed": elapsed,
            "count": len(results),
            "std": top,
        }
    except SpiderSearchError as e:
        return {"ok": False, "detail": "SpiderSearchError: %s" % e, "elapsed": time.time() - t0}
    except Exception as e:
        return {
            "ok": False,
            "detail": "%s: %s" % (type(e).__name__, e),
            "elapsed": time.time() - t0,
            "trace": traceback.format_exc(limit=3),
        }


def check_download_probe(engine: SpiderEngine, parser: str, std_no: str):
    t0 = time.time()
    try:
        if parser == "std_samr":
            url = engine._fetch_samr_text_url(std_no)
            ok = bool(url)
            detail = (url or "")[:90] if ok else "无查看文本 URL"
            extra = {"url": url}
        elif parser == "openstd":
            available, download_url, detail_url = engine._check_openstd_available(std_no)
            ok = bool(available)
            detail = (download_url or detail_url or "")[:90] if ok else "未命中"
            extra = {"download_url": download_url, "detail_url": detail_url}
        elif parser == "webfree":
            available, detail_url = engine._check_webfree_available(std_no)
            ok = bool(available)
            detail = (detail_url or "")[:90] if ok else "未命中下载页"
            extra = {"detail_url": detail_url}
        elif parser == "gxzl":
            available, detail_url = engine._check_gxzl_available(std_no)
            ok = bool(available)
            detail = (detail_url or "")[:90] if ok else "页数 API 无数据"
            extra = {"detail_url": detail_url}
        elif parser == "nrsis":
            available, detail_url, pdf_url = engine._check_nrsis_available(std_no)
            ok = bool(available)
            detail = (pdf_url or detail_url or "")[:90] if ok else "无 PDF 图标入口"
            extra = {"detail_url": detail_url, "pdf_url": pdf_url}
        else:
            return {"ok": False, "detail": "未知 parser", "elapsed": time.time() - t0}
        row = {
            "ok": ok,
            "detail": detail,
            "elapsed": time.time() - t0,
        }
        row.update(extra)
        return row
    except Exception as e:
        return {
            "ok": False,
            "detail": "%s: %s" % (type(e).__name__, e),
            "elapsed": time.time() - t0,
        }


def _is_pdf_file(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 100:
            return False
        with open(path, "rb") as f:
            return f.read(4) == b"%PDF"
    except OSError:
        return False


def check_download_real(engine: SpiderEngine, parser: str, std_no: str, out_dir: Path):
    """真下载冒烟：写出 PDF 并校验文件头。"""
    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    filepath = out_dir / ("%s_%s.pdf" % (parser, std_no.replace("/", "_").replace(" ", "")))
    try:
        std = StandardInfo(std_no=std_no, std_name="smoke", status="现行", source_site="smoke")
        std.download_status = "可下载"
        std.download_sources = [parser]

        if parser == "nrsis":
            from spiders.nrsis_downloader import NrsisPdfDownloader
            dl = NrsisPdfDownloader(engine.session)
            ok, msg = dl.download(std_no, filepath)
        elif parser == "gxzl":
            from spiders.gxzl_pdf_downloader import GxzlPdfDownloader
            # 东盟全量页多，冒烟只验证能拉到页并写出；用 download_standard
            # 若页数过大可能超时——依赖源站对 1.1 的页数
            dl = GxzlPdfDownloader()
            ok = dl.download_standard(std_no, str(filepath))
            msg = str(filepath) if ok else "gxzl 失败"
        elif parser == "webfree":
            from spiders.webfree_downloader import WebfreePdfDownloader
            src = next(s for s in Config.DOWNLOAD_SOURCES if s["parser"] == "webfree")
            dl = WebfreePdfDownloader(engine.session)
            ok, msg = dl.download(std_no, filepath, src.get("password"))
        elif parser == "openstd":
            available, download_url, detail_url = engine._check_openstd_available(std_no)
            if not available:
                return {"ok": False, "detail": "探测未命中，跳过真下", "elapsed": time.time() - t0}
            import re
            m = re.search(r"hcno=([^&]+)", download_url or detail_url or "")
            if not m:
                return {"ok": False, "detail": "无 hcno", "elapsed": time.time() - t0}
            from spiders.openstd_downloader import OpenstdPdfDownloader
            dl = OpenstdPdfDownloader()
            ok, msg = dl.download(m.group(1), filepath, captcha_callback=None)
        elif parser == "std_samr":
            url = engine._fetch_samr_text_url(std_no)
            if not url:
                return {"ok": False, "detail": "无 samr URL", "elapsed": time.time() - t0}
            if "openstd.samr.gov.cn" in url:
                import re
                m = re.search(r"hcno=([^&]+)", url)
                if not m:
                    return {"ok": False, "detail": "samr→openstd 无 hcno", "elapsed": time.time() - t0}
                from spiders.openstd_downloader import OpenstdPdfDownloader
                dl = OpenstdPdfDownloader()
                ok, msg = dl.download(m.group(1), filepath, captcha_callback=None)
            else:
                from spiders.samr_downloader import SamrPdfDownloader
                dl = SamrPdfDownloader()
                ok, msg = dl.download(url, filepath)
        else:
            return {"ok": False, "detail": "未实现真下载", "elapsed": time.time() - t0}

        if not ok:
            return {"ok": False, "detail": str(msg)[:120], "elapsed": time.time() - t0}
        path = Path(msg) if isinstance(msg, str) and msg.endswith(".pdf") else filepath
        if not _is_pdf_file(path):
            return {"ok": False, "detail": "文件非 PDF 或过小: %s" % path, "elapsed": time.time() - t0}
        return {
            "ok": True,
            "detail": "PDF OK %sB %s" % (path.stat().st_size, path.name),
            "elapsed": time.time() - t0,
        }
    except Exception as e:
        return {
            "ok": False,
            "detail": "%s: %s" % (type(e).__name__, e),
            "elapsed": time.time() - t0,
            "trace": traceback.format_exc(limit=4),
        }


def main():
    ap = argparse.ArgumentParser(description="全源查询/下载验收")
    ap.add_argument(
        "--with-download",
        action="store_true",
        help="探测通过后对每个下载源再做真下载冒烟",
    )
    ap.add_argument(
        "--out-dir",
        default="",
        help="真下载输出目录（默认系统临时目录/smoke_sources_pdf）",
    )
    args = ap.parse_args()

    print("Python:", sys.version.split()[0], "| ROOT:", ROOT)
    print("PRIMARY:", [s["parser"] for s in Config.PRIMARY_SITES])
    print("DOWNLOAD:", [s["parser"] for s in Config.DOWNLOAD_SOURCES])
    print("NRSIS_MAX_PAGES=", os.environ.get("NRSIS_MAX_PAGES"))

    engine = SpiderEngine()

    # —— 查询源：配置里每一个都必须 PASS ——
    query_rows = []
    print("\n" + "=" * 72)
    print("查询源验收（配置中每一项）")
    print("=" * 72)
    for site in Config.PRIMARY_SITES:
        parser = site["parser"]
        name = site["name"]
        keyword = QUERY_KEYWORDS.get(parser, "GB/T 1.1-2020")
        print(">> [%s] %s ..." % (parser, keyword), flush=True)
        row = check_query_source(engine, parser, name, keyword)
        row.update({"parser": parser, "name": name, "keyword": keyword})
        query_rows.append(row)
        print(
            "   [%s] %s (%.1fs)" % ("PASS" if row["ok"] else "FAIL", row["detail"], row["elapsed"]),
            flush=True,
        )

    # —— 下载源探测：配置里每一个都必须 PASS ——
    probe_rows = []
    print("\n" + "=" * 72)
    print("下载源探测验收（配置中每一项）")
    print("=" * 72)
    for source in Config.DOWNLOAD_SOURCES:
        parser = source["parser"]
        name = source["name"]
        std_no = DOWNLOAD_STD_BY_PARSER.get(parser, "GB/T 1.1-2020")
        print(">> probe [%s] %s ..." % (parser, std_no), flush=True)
        row = check_download_probe(engine, parser, std_no)
        row.update({"parser": parser, "name": name, "std_no": std_no})
        probe_rows.append(row)
        print(
            "   [%s] %s (%.1fs)" % ("PASS" if row["ok"] else "FAIL", row["detail"], row["elapsed"]),
            flush=True,
        )

    real_rows = []
    if args.with_download:
        out_dir = Path(args.out_dir) if args.out_dir else Path(tempfile.gettempdir()) / "smoke_sources_pdf"
        print("\n" + "=" * 72)
        print("下载源真下载冒烟 → %s" % out_dir)
        print("=" * 72)
        for source in Config.DOWNLOAD_SOURCES:
            parser = source["parser"]
            name = source["name"]
            std_no = DOWNLOAD_STD_BY_PARSER.get(parser, "GB/T 1.1-2020")
            print(">> download [%s] %s ..." % (parser, std_no), flush=True)
            row = check_download_real(engine, parser, std_no, out_dir)
            row.update({"parser": parser, "name": name, "std_no": std_no})
            real_rows.append(row)
            print(
                "   [%s] %s (%.1fs)" % ("PASS" if row["ok"] else "FAIL", row["detail"], row["elapsed"]),
                flush=True,
            )

    print("\n" + "-" * 72)
    q_fail = [r for r in query_rows if not r["ok"]]
    p_fail = [r for r in probe_rows if not r["ok"]]
    d_fail = [r for r in real_rows if not r["ok"]]

    print(
        "查询源: %s/%s PASS"
        % (len(query_rows) - len(q_fail), len(query_rows))
    )
    print(
        "下载探测: %s/%s PASS"
        % (len(probe_rows) - len(p_fail), len(probe_rows))
    )
    if args.with_download:
        print(
            "真下载: %s/%s PASS"
            % (len(real_rows) - len(d_fail), len(real_rows))
        )

    # 验收红线：配置内全部查询源 + 全部下载探测必须过；真下载若开启也必须过
    if q_fail or p_fail or d_fail:
        print("结论: 不通过（存在失败源）")
        if q_fail:
            print("  查询失败:", ", ".join(r["parser"] for r in q_fail))
        if p_fail:
            print("  探测失败:", ", ".join(r["parser"] for r in p_fail))
        if d_fail:
            print("  真下失败:", ", ".join(r["parser"] for r in d_fail))
        code = 1
    else:
        print("结论: 通过 — 全部查询源与下载源验收合格")
        code = 0
    print("-" * 72)
    return code


if __name__ == "__main__":
    sys.exit(main())
