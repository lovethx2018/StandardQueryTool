# -*- coding: utf-8 -*-
"""自然资源网 PDF 下载器。

详情页标题旁 PDF 图标: readPdf('<url>')
  - 国标常跳转 openstd（hcno=…）→ 复用 OpenstdPdfDownloader
  - 行业标走 /mnr_kfs/file/read/{code} 在线阅读器
    → POST /mnr_kfs/file/readPage {code, page} 返回单页 PDF 的 base64
    → 合并为完整 PDF
"""
import base64
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from app_env import logger
from config import Config
from http_client import _http_request
from .nrsis_spider import NrsisSpider


class NrsisPdfDownloader:
    """自然资源网 PDF 下载器"""

    SITE_ORIGIN = "http://www.nrsis.org.cn"
    LIST_PATH = "/portal/xxcx/std"
    MAX_RETRIES = 3
    PAGE_DELAY = 0.25
    # 冒烟/联调可设环境变量 NRSIS_MAX_PAGES=2 只下前 N 页
    MAX_PAGES_ENV = "NRSIS_MAX_PAGES"

    def __init__(self, session, progress_callback=None):
        self.session = session
        self.progress_callback = progress_callback

    def _emit(self, message):
        if self.progress_callback:
            self.progress_callback(message)

    def _get(self, url, **kwargs):
        # mnr_kfs 常返回 500：Session 全局 Retry 会耗尽后抛 RetryError，
        # 这里吞掉异常，交给上层按 status 走盲下/失败分支。
        try:
            return _http_request(self.session, "get", url, **kwargs)
        except Exception as e:
            logger.warning("[自然资源网] GET 异常 %s: %s", url[:80], e)
            return None

    def _post(self, url, **kwargs):
        try:
            return _http_request(self.session, "post", url, **kwargs)
        except Exception as e:
            logger.warning("[自然资源网] POST 异常 %s: %s", url[:80], e)
            return None

    @staticmethod
    def _norm_no(std_no):
        return re.sub(r"\s+", "", (std_no or "")).upper()

    @classmethod
    def _keyword_variants(cls, std_no):
        return NrsisSpider._keyword_variants(std_no)

    def find_detail_and_pdf(self, std_no):
        """搜索标准 → 详情页 → 解析 PDF 图标 readPdf 链接。

        返回: (detail_url, pdf_target_url) 或 (None, None)
        """
        target = self._norm_no(std_no)
        detail_url = None
        for kw in self._keyword_variants(std_no):
            url = "%s%s?pageNo=1&pageSize=20&key=%s" % (
                self.SITE_ORIGIN,
                self.LIST_PATH,
                quote(kw, safe=""),
            )
            resp = self._get(
                url,
                headers={"Referer": self.SITE_ORIGIN + self.LIST_PATH},
                timeout=Config.TIMEOUT,
            )
            if resp is None or getattr(resp, "status_code", 0) != 200:
                continue
            if resp.encoding is None or str(resp.encoding).lower() in ("iso-8859-1", "ascii"):
                resp.encoding = resp.apparent_encoding or "utf-8"
            soup = BeautifulSoup(resp.text, "html.parser")
            for tr in soup.select("table tr"):
                tds = tr.find_all("td")
                if len(tds) < 3:
                    continue
                row_no = self._norm_no(tds[1].get_text(strip=True))
                if row_no != target:
                    continue
                a = tr.find("a", href=re.compile(r"/portal/stdDetail/\d+"))
                if not a:
                    continue
                href = a.get("href") or ""
                detail_url = urljoin(self.SITE_ORIGIN + "/", href)
                break
            if detail_url:
                break
        if not detail_url:
            return None, None
        pdf_url = self._extract_pdf_from_detail(detail_url)
        return detail_url, pdf_url

    def _extract_pdf_from_detail(self, detail_url):
        resp = self._get(
            detail_url,
            headers={"Referer": self.SITE_ORIGIN + self.LIST_PATH},
            timeout=Config.TIMEOUT,
        )
        if resp is None or getattr(resp, "status_code", 0) != 200:
            return None
        if resp.encoding is None or str(resp.encoding).lower() in ("iso-8859-1", "ascii"):
            resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text or ""
        # 站点常把 URL 拆成多行：readPdf('http://...\n...code')
        def _clean_pdf_url(raw):
            return re.sub(r"\s+", "", (raw or "").strip())

        # 优先：带 pdf.png 图标的 readPdf（与页面「查看标准全文」入口一致）
        for m in re.finditer(
            r"readPdf\(\s*['\"]([\s\S]*?)['\"]\s*\)[\s\S]{0,240}?pdf\.png",
            html,
            re.I,
        ):
            url = _clean_pdf_url(m.group(1))
            if url:
                return url
        m = re.search(r"readPdf\(\s*['\"]([\s\S]*?)['\"]\s*\)", html)
        return _clean_pdf_url(m.group(1)) if m else None

    def download(self, std_no, filepath, pdf_url=None, detail_url=None):
        """
        下载 PDF 到 filepath。
        :return: (success, message)
        """
        try:
            self._emit("自然资源网：定位标准…")
            if not pdf_url:
                detail_url, pdf_url = self.find_detail_and_pdf(std_no)
            if not pdf_url:
                return False, "自然资源网未找到 PDF 入口"

            logger.info("[自然资源网] PDF入口: %s", pdf_url[:120])

            # 国标：图标跳转国家标准全文
            if "openstd.samr.gov.cn" in pdf_url and "hcno=" in pdf_url:
                hcno_m = re.search(r"hcno=([^&]+)", pdf_url)
                if not hcno_m:
                    return False, "自然资源网 PDF 链接缺少 hcno"
                hcno = hcno_m.group(1).strip()
                self._emit("自然资源网：经由国家标准全文下载…")
                from .openstd_downloader import OpenstdPdfDownloader

                dl = OpenstdPdfDownloader(progress_callback=self.progress_callback)
                return dl.download(hcno, filepath, captcha_callback=None)

            # 行业标：mnr_kfs 分页阅读器
            code_m = re.search(r"/mnr_kfs/file/read/([0-9a-fA-F]+)", pdf_url)
            if not code_m:
                return False, "自然资源网暂不支持该 PDF 链接格式"
            code = code_m.group(1)
            return self._download_mnr_pages(code, filepath, referer=detail_url or pdf_url)

        except Exception as e:
            logger.error("[自然资源网] 下载异常: %s", e, exc_info=True)
            return False, "下载异常: %s" % e

    def _download_mnr_pages(self, code, filepath, referer=None):
        viewer_url = "%s/mnr_kfs/file/read/%s" % (self.SITE_ORIGIN, code)
        self.session.cookies.set("readed", "yes", domain="www.nrsis.org.cn")
        self._emit("自然资源网：打开全文阅读器…")
        # 阅读器偶发/持续 500：自行退避重试；仍失败则盲下前 N 页
        viewer = None
        for attempt in range(1, 6):
            viewer = self._get(
                viewer_url,
                headers={"Referer": referer or self.SITE_ORIGIN},
                timeout=Config.TIMEOUT,
            )
            if viewer is not None and getattr(viewer, "status_code", 0) == 200:
                break
            status = getattr(viewer, "status_code", None)
            logger.warning(
                "[自然资源网] 阅读器打开失败 status=%s attempt=%s", status, attempt
            )
            time.sleep(min(2.0 * attempt, 8.0))
            viewer = None

        if viewer is None or getattr(viewer, "status_code", 0) != 200:
            try:
                cap = int(os.environ.get(self.MAX_PAGES_ENV, "2") or "2")
            except ValueError:
                cap = 2
            total = max(cap, 1)
            logger.warning("[自然资源网] 阅读器不可用，盲下前 %s 页", total)
            page_blobs = []
            for page in range(1, total + 1):
                self._emit("自然资源网：盲下 %s/%s 页…" % (page, total))
                raw = self._fetch_page_bytes(code, page, referer or viewer_url)
                if not raw:
                    if page == 1:
                        return False, "自然资源网阅读器/首页下载失败"
                    break
                page_blobs.append(raw)
                time.sleep(self.PAGE_DELAY)
            if not page_blobs:
                return False, "自然资源网阅读器打开失败"
            ok = self._merge_pdf_pages(page_blobs, filepath)
            return (True, str(filepath)) if ok else (False, "自然资源网 PDF 合并失败")

        if viewer.encoding is None or str(viewer.encoding).lower() in ("iso-8859-1", "ascii"):
            viewer.encoding = viewer.apparent_encoding or "utf-8"
        size_m = re.search(r"size\s*:\s*parseInt\(\s*['\"](\d+)['\"]\s*\)", viewer.text or "")
        if not size_m:
            size_m = re.search(r"Page\.size\s*=\s*(\d+)", viewer.text or "")
        total = int(size_m.group(1)) if size_m else 0
        if total <= 0:
            return False, "自然资源网无法获取页数"
        try:
            cap = int(os.environ.get(self.MAX_PAGES_ENV, "0") or "0")
        except ValueError:
            cap = 0
        if cap > 0:
            total = min(total, cap)
            logger.info("[自然资源网] 冒烟限页: %s (env %s)", total, self.MAX_PAGES_ENV)
        logger.info("[自然资源网] 总页数: %s code=%s", total, code)

        page_blobs = []
        for page in range(1, total + 1):
            self._emit("自然资源网：下载 %s/%s 页…" % (page, total))
            raw = self._fetch_page_bytes(code, page, viewer_url)
            if not raw:
                return False, "自然资源网第 %s 页下载失败" % page
            page_blobs.append(raw)
            if page < total:
                time.sleep(self.PAGE_DELAY)

        self._emit("自然资源网：合并 PDF…")
        ok = self._merge_pdf_pages(page_blobs, filepath)
        if not ok:
            return False, "自然资源网 PDF 合并失败"
        return True, str(filepath)

    def _fetch_page_bytes(self, code, page, referer):
        url = "%s/mnr_kfs/file/readPage" % self.SITE_ORIGIN
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                resp = self._post(
                    url,
                    data={"code": code, "page": page},
                    headers={
                        "Referer": referer,
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "*/*",
                    },
                    timeout=max(Config.TIMEOUT, 60),
                )
                if resp is None or getattr(resp, "status_code", 0) != 200:
                    raise RuntimeError("HTTP %s" % getattr(resp, "status_code", None))
                text = (resp.text or "").strip()
                raw = base64.b64decode(text)
                if raw[:4] != b"%PDF":
                    raise RuntimeError("非 PDF 内容")
                return raw
            except Exception as e:
                logger.warning(
                    "[自然资源网] 第 %s 页失败(%s/%s): %s",
                    page,
                    attempt,
                    self.MAX_RETRIES,
                    e,
                )
                time.sleep(1.0 * attempt)
        return None

    @staticmethod
    def _import_fitz():
        """PyMuPDF：包名 pymupdf，模块名多为 fitz（与预览渲染一致）。"""
        try:
            import fitz
            return fitz
        except ImportError:
            import pymupdf as fitz  # noqa: WPS433
            return fitz

    @classmethod
    def _merge_pdf_pages(cls, page_blobs, filepath):
        """将多份单页 PDF 合并为一个文件（优先 PyMuPDF，回退纯拼装）。"""
        dest = Path(filepath)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        try:
            try:
                fitz = cls._import_fitz()
            except ImportError:
                fitz = None
            if fitz is not None:
                out = fitz.open()
                try:
                    for raw in page_blobs:
                        src = fitz.open(stream=raw, filetype="pdf")
                        try:
                            out.insert_pdf(src)
                        finally:
                            src.close()
                    out.save(str(part), deflate=True)
                finally:
                    out.close()
            else:
                if not cls._merge_pdf_pages_raw(page_blobs, part):
                    return False
            if not part.is_file() or part.stat().st_size <= 0:
                try:
                    part.unlink()
                except OSError:
                    pass
                return False
            os.replace(str(part), str(dest))
            return True
        except Exception as e:
            logger.error("[自然资源网] 合并失败: %s", e, exc_info=True)
            try:
                if part.is_file():
                    part.unlink()
            except OSError:
                pass
            return False

    @staticmethod
    def _merge_pdf_pages_raw(page_blobs, part_path):
        """无 PyMuPDF 时的兜底：按对象号重编号拼接多份单页 PDF。"""
        # 仅处理较规整的单页 PDF（自然资源网 readPage 输出）；失败则返回 False
        try:
            bodies = []
            for raw in page_blobs:
                if not raw.startswith(b"%PDF"):
                    return False
                bodies.append(raw)
            if not bodies:
                return False
            if len(bodies) == 1:
                part_path.write_bytes(bodies[0])
                return True

            out_objects = {}  # objnum -> bytes of object body (without "N N obj" wrapper)
            next_id = 1
            page_ids = []

            def _parse_objects(data):
                # 找 xref 或线性扫描 obj
                objs = {}
                for m in re.finditer(
                    br"(\d+)\s+(\d+)\s+obj\b", data
                ):
                    num = int(m.group(1))
                    start = m.end()
                    end_m = re.search(br"\bendobj\b", data[start:])
                    if not end_m:
                        continue
                    end = start + end_m.start()
                    objs[num] = data[start:end]
                return objs

            def _remap(content, mapping):
                def repl(m):
                    n = int(m.group(1))
                    if n in mapping:
                        return b"%d %s R" % (mapping[n], m.group(2))
                    return m.group(0)

                return re.sub(br"(\d+)\s+(\d+)\s+R\b", repl, content)

            for raw in bodies:
                objs = _parse_objects(raw)
                if not objs:
                    return False
                # 找 Page 对象
                page_num = None
                for num, body in objs.items():
                    if re.search(br"/Type\s*/Page\b", body) and not re.search(
                        br"/Type\s*/Pages\b", body
                    ):
                        page_num = num
                        break
                if page_num is None:
                    return False
                local_map = {}
                for old in sorted(objs.keys()):
                    local_map[old] = next_id
                    next_id += 1
                for old, body in objs.items():
                    new_id = local_map[old]
                    # Pages 树节点跳过，稍后重建
                    if re.search(br"/Type\s*/Pages\b", body) and not re.search(
                        br"/Type\s*/Page\b", body
                    ):
                        continue
                    if re.search(br"/Type\s*/Catalog\b", body):
                        continue
                    remapped = _remap(body, local_map)
                    # Page 去掉 Parent，稍后挂到新 Pages
                    if old == page_num:
                        remapped = re.sub(
                            br"/Parent\s+\d+\s+\d+\s+R", b"", remapped
                        )
                        page_ids.append(new_id)
                    out_objects[new_id] = remapped

            pages_id = next_id
            next_id += 1
            catalog_id = next_id
            next_id += 1
            kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
            out_objects[pages_id] = (
                b"<< /Type /Pages /Count %d /Kids [ %s ] >>"
                % (len(page_ids), kids)
            )
            # 给每个 Page 补 Parent
            for pid in page_ids:
                body = out_objects[pid]
                if b"/Parent" not in body:
                    body = body.replace(
                        b"/Type /Page",
                        b"/Type /Page /Parent %d 0 R" % pages_id,
                        1,
                    )
                    out_objects[pid] = body
            out_objects[catalog_id] = (
                b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
            )

            # 写文件
            buf = bytearray()
            buf.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
            offsets = {0: 0}
            for oid in sorted(out_objects.keys()):
                offsets[oid] = len(buf)
                buf.extend(b"%d 0 obj\n" % oid)
                buf.extend(out_objects[oid])
                if not out_objects[oid].endswith(b"\n"):
                    buf.extend(b"\n")
                buf.extend(b"endobj\n")
            xref_pos = len(buf)
            max_id = max(out_objects.keys())
            buf.extend(b"xref\n0 %d\n" % (max_id + 1))
            buf.extend(b"0000000000 65535 f \n")
            for i in range(1, max_id + 1):
                if i in offsets:
                    buf.extend(b"%010d 00000 n \n" % offsets[i])
                else:
                    buf.extend(b"0000000000 65535 f \n")
            buf.extend(
                b"trailer\n<< /Size %d /Root %d 0 R >>\n"
                b"startxref\n%d\n%%%%EOF\n" % (max_id + 1, catalog_id, xref_pos)
            )
            part_path.write_bytes(bytes(buf))
            return part_path.stat().st_size > 0
        except Exception as e:
            logger.warning("[自然资源网] 纯拼装合并失败: %s", e)
            return False
