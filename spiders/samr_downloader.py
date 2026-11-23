# -*- coding: utf-8 -*-
"""全国标准信息公共服务平台 PDF下载器"""
import random
import re
import threading
import time
from pathlib import Path
from app_env import logger
from config import Config
from http_client import _http_request, HAS_REQUESTS, _StdlibSession

if HAS_REQUESTS:
    import requests


class SamrPdfDownloader:
    """全国标准信息公共服务平台 PDF下载器"""
    
    def __init__(self, progress_callback=None):
        """
        初始化下载器
        :param progress_callback: 进度回调函数，签名: callback(message: str)
        """
        self.progress_callback = progress_callback
    
    def _emit_progress(self, message):
        """发送进度信号"""
        if self.progress_callback:
            self.progress_callback(message)
    
    def download(self, url, filepath):
        """
        直接从URL下载PDF文件（用于全国标准信息公共服务平台中DB等标准的filePath直接下载）
        :param url: 下载URL
        :param filepath: 保存路径
        :return: (success, message)
        """
        try:
            logger.info(f"[直接下载] 开始下载: {url[:80]}")
            
            # 行业标准下载特殊处理：需要先访问 portal/online 页面，再设置 Referer 下载
            portal_url = None
            if 'hbba.sacinfo.org.cn/portal/download' in url:
                # 提取 hash 值，构造 portal/online URL
                hash_match = re.search(r'/portal/download/([a-f0-9]+)', url)
                if hash_match:
                    hash_val = hash_match.group(1)
                    portal_url = f'https://hbba.sacinfo.org.cn/portal/online/{hash_val}'
                    logger.info(f"[直接下载] 行业标准下载，先访问 portal 页面: {portal_url[:80]}")
            
            # 使用标准库 session（兼容无 requests 环境）
            dl_session = _StdlibSession()
            # 设置更完整的浏览器请求头
            dl_session.headers = {
                'User-Agent': random.choice(Config.USER_AGENTS),
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
            
            # 先访问 portal/online 页面建立 session
            if portal_url:
                logger.info(f"[直接下载] 访问 portal 页面: {portal_url}")
                try:
                    portal_resp = dl_session.get(portal_url, timeout=Config.TIMEOUT)
                    logger.info(f"[直接下载] portal 页面状态: {portal_resp.status_code}")
                except Exception as e:
                    logger.warning(f"[直接下载] 访问 portal 页面失败: {e}")
            
            # 部分服务器使用 chunked 传输且不发送 Content-Length，
            # 必须使用 stream=True 立即获取响应头，然后分块读取内容
            result = [None, None]
            def _do_get():
                try:
                    # 设置 Referer 为 portal 页面
                    if portal_url:
                        dl_session.headers['Referer'] = portal_url
                    result[0] = dl_session.get(url, timeout=Config.TIMEOUT * 3, stream=True)
                except Exception as e:
                    result[1] = e
            t = threading.Thread(target=_do_get)
            t.daemon = True
            t.start()
            t.join(timeout=Config.TIMEOUT * 4)
            if result[1]:
                raise result[1]
            if result[0] is None:
                return False, "下载超时（服务器未响应）"
            resp = result[0]
            
            if resp.status_code != 200:
                return False, f"下载失败: HTTP {resp.status_code}"
            
            content_type = resp.headers.get('Content-Type', '')
            if 'text/html' in content_type and 'pdf' not in content_type:
                # 返回的是HTML页面而非PDF，可能是需要浏览器交互的预览页
                logger.warning(f"[直接下载] 返回HTML而非PDF，可能需要浏览器交互: {url[:80]}")
                return False, f"NEED_BROWSER:该标准需浏览器打开后手动下载"
            
            # 写入文件（支持 chunked 传输编码）
            total_size = int(resp.headers.get('Content-Length', 0))
            downloaded_size = 0
            last_progress_time = 0
            
            with open(str(filepath), 'wb') as f:
                if hasattr(resp, 'iter_content'):
                    # requests 响应：分块读取
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            
                            # 每 0.5 秒发送一次进度信号，避免 UI 卡顿
                            current_time = time.time()
                            if current_time - last_progress_time > 0.5:
                                if total_size > 0:
                                    percent = (downloaded_size / total_size) * 100
                                    self._emit_progress(f"下载中... {percent:.1f}% ({downloaded_size // 1024 // 1024}MB / {total_size // 1024 // 1024}MB)")
                                else:
                                    # 没有 Content-Length，只显示已下载大小
                                    self._emit_progress(f"下载中... {downloaded_size // 1024 // 1024}MB")
                                last_progress_time = current_time
                else:
                    # 标准库响应：一次性写入
                    f.write(resp.content)
            
            if filepath.exists() and filepath.stat().st_size > 0:
                logger.info(f"[直接下载] 下载成功: {filepath.name} ({filepath.stat().st_size} bytes)")
                return True, str(filepath)
            else:
                return False, "下载文件为空"
        except Exception as e:
            return False, f"直接下载异常: {str(e)}"
