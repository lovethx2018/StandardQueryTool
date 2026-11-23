# -*- coding: utf-8 -*-
"""国家标准全文 PDF下载器"""
import random
import time
from pathlib import Path
from app_env import logger
from config import Config
from http_client import _http_request, HAS_REQUESTS, _StdlibSession

try:
    import requests
except ImportError:
    requests = None


class OpenstdPdfDownloader:
    """国家标准全文 PDF下载器"""

    # viewGb 偶发返回 0 字节（Session/Referer 未就绪）；自动重试若干次
    MAX_ATTEMPTS = 3

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback

    def _emit_progress(self, message):
        if self.progress_callback:
            self.progress_callback(message)

    @staticmethod
    def _detail_url(hcno):
        return f'https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno={hcno}&refer=outter'

    @staticmethod
    def _showgb_url(hcno):
        return f'https://openstd.samr.gov.cn/bzgk/std/showGb?type=download&hcno={hcno}&request_locale=zh'

    @staticmethod
    def online_preview_url(hcno):
        """官方在线预览页（type=online）。"""
        return (
            f'https://openstd.samr.gov.cn/bzgk/std/showGb'
            f'?type=online&hcno={hcno}&request_locale=zh'
        )

    @staticmethod
    def _viewgb_url(hcno):
        return f'https://openstd.samr.gov.cn/bzgk/std/viewGb?hcno={hcno}'

    def _new_session(self):
        """优先 requests.Session（Cookie 更稳）；无 requests 时回退标准库。"""
        ua = (
            'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36'
        )
        headers = {
            'User-Agent': ua,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }
        if HAS_REQUESTS and requests is not None:
            sess = requests.Session()
            sess.headers.update(headers)
            return sess
        sess = _StdlibSession()
        sess.headers.update(headers)
        return sess

    def download(self, hcno, filepath, captcha_callback=None):
        """
        从国家标准全文下载PDF。
        流程：首页建 Session → 详情页 → 下载页(showGb) → viewGb 取 PDF。
        空包自动重试；HTML/验证码页返回 NEED_BROWSER:<openstd详情页URL>。
        """
        detail_url = self._detail_url(hcno)
        showgb_url = self._showgb_url(hcno)
        viewgb_url = self._viewgb_url(hcno)
        browser_hint = f"NEED_BROWSER:{detail_url}"

        last_empty = False
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                download_session = self._new_session()
                logger.info(
                    f"[openstd下载] 第 {attempt}/{self.MAX_ATTEMPTS} 次尝试, hcno={hcno}"
                )

                logger.info("[openstd下载] 步骤1: 访问首页建立Session")
                resp1 = _http_request(
                    download_session, 'get',
                    'https://openstd.samr.gov.cn/bzgk/std/',
                    timeout=Config.TIMEOUT,
                )
                logger.info(f"[openstd下载] 首页状态码: {resp1.status_code}")

                logger.info(f"[openstd下载] 步骤2: 访问详情页 {detail_url}")
                resp2 = _http_request(
                    download_session, 'get', detail_url, timeout=Config.TIMEOUT,
                )
                logger.info(f"[openstd下载] 详情页状态码: {resp2.status_code}")

                # 步骤3：下载页（必须带详情页 Referer，否则 viewGb 常返回 0 字节）
                logger.info(f"[openstd下载] 步骤3: 访问下载页 {showgb_url}")
                resp3 = _http_request(
                    download_session, 'get', showgb_url, timeout=Config.TIMEOUT,
                    headers={'Referer': detail_url},
                )
                logger.info(
                    f"[openstd下载] 下载页状态码: {resp3.status_code}, "
                    f"长度: {len(getattr(resp3, 'content', b'') or b'')}"
                )

                # 给服务端一点时间写入下载票据（浏览器场景下常有短延迟）
                time.sleep(0.6 + random.uniform(0.1, 0.4))

                download_session.headers['Referer'] = showgb_url
                download_session.headers['Accept'] = (
                    'application/pdf, application/octet-stream, */*'
                )

                logger.info(f"[openstd下载] 步骤4: 下载PDF {viewgb_url}")
                response = _http_request(
                    download_session, 'get', viewgb_url,
                    timeout=30, allow_redirects=True,
                    headers={'Referer': showgb_url},
                )
                content = getattr(response, 'content', b'') or b''
                logger.info(
                    f"[openstd下载] PDF响应状态码: {response.status_code}, "
                    f"内容长度: {len(content)}"
                )

                if response.status_code == 200 and len(content) > 0:
                    content_type = (response.headers.get('Content-Type') or '').lower()
                    logger.info(f"[openstd下载] Content-Type: {content_type}")
                    if 'text/html' in content_type:
                        # 验证码/拦截页：打开国家标准全文详情页让用户点「下载标准」
                        logger.warning(
                            "[openstd下载] 返回HTML页面，降级到浏览器（openstd详情页）"
                        )
                        return False, browser_hint

                    with open(filepath, 'wb') as f:
                        f.write(content)
                    if filepath.stat().st_size == 0:
                        logger.error(f"[openstd下载] 下载文件为空: {filepath.name}")
                        last_empty = True
                    else:
                        logger.info(
                            f"[openstd下载] 下载成功: {filepath} "
                            f"({filepath.stat().st_size} bytes)"
                        )
                        return True, str(filepath)
                else:
                    last_empty = True
                    logger.warning(
                        f"[openstd下载] 空响应/失败: status={response.status_code}, "
                        f"content_len={len(content)}，将重试"
                    )

            except Exception as e:
                logger.warning(f"[openstd下载] 第 {attempt} 次异常: {e}")
                if attempt >= self.MAX_ATTEMPTS:
                    return False, f"下载异常: {str(e)}"

            if attempt < self.MAX_ATTEMPTS:
                delay = 0.8 + random.uniform(0.2, 0.8)
                logger.info(f"[openstd下载] {delay:.1f}s 后重试…")
                time.sleep(delay)

        # 多次仍空包：软失败（不立刻 NEED_BROWSER），交给后续下载源；
        # 若全部源失败，外层可再用 browser_hint 打开正确的 openstd 页。
        if last_empty:
            logger.warning(
                f"[openstd下载] {self.MAX_ATTEMPTS} 次均为空包，"
                f"软失败继续其它下载源；浏览器降级页={detail_url}"
            )
            return False, f"EMPTY_PDF:{detail_url}"
        return False, browser_hint
