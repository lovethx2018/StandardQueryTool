# -*- coding: utf-8 -*-
"""协筑资源 PDF下载器"""
import os
import re
from pathlib import Path
from urllib.parse import quote, urljoin
from bs4 import BeautifulSoup
from app_env import logger
from config import Config
from http_client import _http_request


class WebfreePdfDownloader:
    """协筑资源 PDF下载器"""
    
    def __init__(self, session, progress_callback=None):
        """
        初始化下载器
        :param session: HTTP会话
        :param progress_callback: 进度回调函数，签名: callback(message: str)
        """
        self.session = session
        self.progress_callback = progress_callback
    
    def _emit_progress(self, message):
        """发送进度信号"""
        if self.progress_callback:
            self.progress_callback(message)
    
    def _get(self, url, **kwargs):
        """HTTP GET请求"""
        return _http_request(self.session, 'get', url, **kwargs)

    @staticmethod
    def _write_pdf_file(filepath, response):
        """流式写入 PDF：先写 .part 再替换，避免覆盖被锁文件时直接失败。"""
        dest = Path(filepath)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        try:
            with open(part, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            if part.stat().st_size <= 0:
                try:
                    part.unlink()
                except OSError:
                    pass
                return None
            try:
                os.replace(str(part), str(dest))
                return dest
            except PermissionError:
                # 目标被占用：改写为独立文件名
                alt = dest.with_name(f"{dest.stem}_w{os.getpid()}{dest.suffix}")
                os.replace(str(part), str(alt))
                logger.warning(f"[协筑资源] 目标文件被占用，改存为: {alt.name}")
                return alt
        except Exception:
            try:
                if part.is_file():
                    part.unlink()
            except OSError:
                pass
            raise

    def download(self, std_no, filepath, password=None):
        """
        从协筑资源下载PDF
        下载流程：搜索标准号 → 点击详情 → 访问下载弹窗 → 提交同意和密码 → 下载PDF
        搜索URL格式: https://www.webfree.net/1087-2?search=GB%2FT+8077-2023
        详情页格式: https://www.webfree.net/download/17966
        下载密码: Webfree.net
        
        :param std_no: 标准编号
        :param filepath: 保存路径
        :param password: 下载密码（默认：Webfree.net） 密码必须有，下载的时候需要自动提交。
        :return: (success, message)
        """
        try:
            logger.debug(f"[协筑资源] 尝试下载: {std_no}, 密码: {'***' if password else '(无)'}")
            
            if not password:
                password = "Webfree.net"
            
            # 步骤1：先访问首页建立Session
            logger.debug("[协筑资源] 访问首页")
            home_resp = self._get("https://www.webfree.net/", timeout=Config.TIMEOUT)
            if home_resp.status_code != 200:
                return False, f"协筑资源首页访问失败: HTTP {home_resp.status_code}"
            
            # 步骤2：搜索标准编号（正确的搜索URL格式）
            # 注意：webfree 搜索对空格敏感，带空格会被当作分词返回大量无关结果，
            # 导致精确条目被挤出结果窗口而匹配失败，故查询前去除编号中的空格。
            search_url = f"https://www.webfree.net/1087-2?search={quote(std_no.replace(' ', ''))}"
            logger.debug(f"[协筑资源] 搜索URL: {search_url}")
            
            resp = self._get(search_url, timeout=Config.TIMEOUT)
            if resp.status_code != 200:
                return False, f"协筑资源搜索失败: HTTP {resp.status_code}"
            
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # 步骤3：找到标准详情页链接
            detail_link = None
            download_id = None
            std_no_clean = std_no.replace(' ', '').replace('/', '')
            
            for a in soup.find_all('a', href=True):
                href = a['href']
                txt = a.get_text(strip=True)
                txt_clean = txt.replace(' ', '').replace('/', '')
                
                if '/download/' in href and std_no_clean in txt_clean:
                    detail_link = href
                    # 提取download ID
                    id_match = re.search(r'/download/(\d+)', href)
                    if id_match:
                        download_id = id_match.group(1)
                    break
            
            if not detail_link:
                return False, "协筑资源未找到该标准"
            
            if not detail_link.startswith('http'):
                detail_link = urljoin("https://www.webfree.net/", detail_link)
            
            logger.debug(f"[协筑资源] 详情页URL: {detail_link}")
            logger.debug(f"[协筑资源] 下载ID: {download_id}")
            
            # 步骤4：访问详情页
            detail_resp = self._get(detail_link, timeout=Config.TIMEOUT)
            if detail_resp.status_code != 200:
                return False, f"协筑资源详情页访问失败: HTTP {detail_resp.status_code}"
            
            detail_soup = BeautifulSoup(detail_resp.text, 'html.parser')
            
            # 步骤5：访问下载弹窗页面（__wpdmlo参数）
            if download_id:
                wpdmlo_url = f"https://www.webfree.net/?__wpdmlo={download_id}&REFERRER={quote(detail_link)}"
                logger.debug(f"[协筑资源] 访问下载弹窗页面: {wpdmlo_url}")
                
                wpdmlo_resp = self._get(wpdmlo_url, timeout=Config.TIMEOUT)
                logger.debug(f"[协筑资源] 下载弹窗页面HTTP状态: {wpdmlo_resp.status_code}")
                
                if wpdmlo_resp.status_code != 200:
                    return False, f"协筑资源下载弹窗访问失败: HTTP {wpdmlo_resp.status_code}"
                
                # 步骤6：提交密码验证请求（wp-json API）
                ajax_url = "https://www.webfree.net/wp-json/wpdm/validate-password"
                
                form_data = {
                    '__wpdm_ID': download_id,
                    'dataType': 'json',
                    'execute': 'wpdm_getlink',
                    'action': 'wpdm_ajax_call',
                    'password': password
                }
                
                # 日志中脱敏密码字段
                safe_form_data = {k: ('***' if k == 'password' else v) for k, v in form_data.items()}
                logger.debug(f"[协筑资源] AJAX请求数据: {safe_form_data}")
                
                ajax_resp = _http_request(
                    self.session, 'post',
                    ajax_url,
                    data=form_data,
                    timeout=Config.TIMEOUT,
                    headers={
                        'Referer': wpdmlo_url,
                        'X-Requested-With': 'XMLHttpRequest',
                        'Content-Type': 'application/x-www-form-urlencoded'
                    }
                )
                
                logger.debug(f"[协筑资源] AJAX请求HTTP状态: {ajax_resp.status_code}")
                
                if ajax_resp.status_code == 200:
                    logger.debug(f"[协筑资源] AJAX响应内容: {ajax_resp.text[:500]}")
                    try:
                        result = ajax_resp.json()
                        logger.debug(f"[协筑资源] AJAX JSON结果: {result}")
                        if result.get('success') == True and 'downloadurl' in result:
                            pdf_url = result['downloadurl']
                            logger.debug(f"[协筑资源] 获取到PDF下载链接: {pdf_url}")
                            
                            if not pdf_url.startswith('http'):
                                pdf_url = urljoin("https://www.webfree.net/", pdf_url)
                            
                            download_resp = self._get(
                                pdf_url,
                                timeout=Config.TIMEOUT * 2,
                                stream=True,
                                headers={'Referer': wpdmlo_url}
                            )
                            
                            if download_resp.status_code == 200 and len(download_resp.content) > 0:
                                content_type = download_resp.headers.get('content-type', '').lower()
                                if 'text/html' not in content_type or 'application/pdf' in content_type:
                                    written = self._write_pdf_file(filepath, download_resp)
                                    if written is not None:
                                        return True, str(written)
                    except PermissionError as e:
                        logger.warning(f"[协筑资源] 写入 PDF 权限失败: {e}")
                        return False, f"无法写入临时文件（文件被占用）：{e}"
                    except Exception as e:
                        logger.warning(f"[协筑资源] AJAX响应解析失败: {str(e)}")
            
            # 尝试查找直接的PDF下载链接
            pdf_link = None
            for a in detail_soup.find_all('a', href=True):
                href = a['href']
                if '.pdf' in href.lower():
                    pdf_link = href
                    break
            
            if pdf_link:
                if not pdf_link.startswith('http'):
                    pdf_link = urljoin("https://www.webfree.net/", pdf_link)
                
                download_resp = self._get(pdf_link, timeout=Config.TIMEOUT * 2, stream=True)
                if download_resp.status_code == 200 and len(download_resp.content) > 0:
                    content_type = download_resp.headers.get('content-type', '').lower()
                    if 'text/html' not in content_type:
                        try:
                            written = self._write_pdf_file(filepath, download_resp)
                            if written is not None:
                                return True, str(written)
                        except PermissionError as e:
                            return False, f"无法写入临时文件（文件被占用）：{e}"
            
            return False, "协筑资源暂无可下载文件"
            
        except Exception as e:
            logger.warning(f"[协筑资源] 下载异常: {str(e)}")
            return False, f"协筑资源下载异常: {str(e)}"
