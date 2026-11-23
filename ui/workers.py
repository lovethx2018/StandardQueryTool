# -*- coding: utf-8 -*-
"""工作线程：检索、下载、本地匹配。"""
from PyQt5.QtCore import QThread, pyqtSignal
from app_env import logger
from utils import _FileNameParser


class SearchWorker(QThread):
    """搜索工作线程"""
    finished = pyqtSignal(list)
    progress = pyqtSignal(str)
    site_status = pyqtSignal(int, str)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()
    result_found = pyqtSignal(list)
    
    def __init__(self, spider, keyword, check_download=True, mark_disabled=False):
        super().__init__()
        self.spider = spider
        self.keyword = keyword
        self.check_download = check_download
        self.mark_disabled = mark_disabled  # 批量查询专用：结果一律标记为"不启用"
        self._cancelled = False
        
    def cancel(self):
        self._cancelled = True
        if hasattr(self.spider, 'cancel'):
            self.spider.cancel()
        
    def _on_result_found(self, results):
        """接收到 spider 的结果信号，转发给 UI"""
        self.result_found.emit(results)
        
    def run(self):
        results = []
        try:
            self.spider.progress_updated.connect(self.progress.emit)
            self.spider.status_updated.connect(self.site_status.emit)
            self.spider.result_found.connect(self._on_result_found)
            
            # 重置 spider 的取消标志
            if hasattr(self.spider, 'reset_cancel'):
                self.spider.reset_cancel()
            
            # 检查取消标志
            if self._cancelled:
                self.cancelled.emit()
                return
            
            # 执行搜索：批量命中即停，显著减少多站点重复请求
            results = self.spider.search_standard(
                self.keyword,
                self.check_download,
                stop_on_first=True if self.mark_disabled else None,
            )
            
            # 批量查询时，强制关闭下载功能（含废止标准的替代版本行，一律不启用）
            if self.mark_disabled:
                for std in results:
                    std.download_status = "不启用"
                    replacement = getattr(std, "replacement", None)
                    if replacement is not None:
                        replacement.download_status = "不启用"
            
            # 取消时只发 cancelled（附带部分结果走 finished 一次），避免双气泡
            if self._cancelled:
                self.finished.emit(results)
                self.cancelled.emit()
            else:
                self.finished.emit(results)
        except RuntimeError as e:
            if "搜索已被取消" in str(e):
                self.finished.emit(results)
                self.cancelled.emit()
            else:
                error_msg = f"搜索异常: {str(e)}"
                logger.error(error_msg)
                self.error.emit(error_msg)
                self.finished.emit(results)
        except Exception as e:
            error_msg = f"搜索异常: {str(e)}"
            logger.error(error_msg)
            self.error.emit(error_msg)
            self.finished.emit(results)
        finally:
            # 断开与共享 spider 的信号连接，避免批量查询逐条创建 worker 时连接累积
            try:
                self.spider.progress_updated.disconnect(self.progress.emit)
                self.spider.status_updated.disconnect(self.site_status.emit)
                self.spider.result_found.disconnect(self._on_result_found)
            except (TypeError, RuntimeError):
                pass

class DownloadWorker(QThread):
    """下载工作线程"""
    finished = pyqtSignal(bool, str)
    progress = pyqtSignal(str)
    
    def __init__(self, spider, standard_info):
        super().__init__()
        self.spider = spider
        self.standard_info = standard_info
        # 在主线程中连接信号，确保跨线程信号正确传递
        self.spider.progress_updated.connect(self.progress.emit)
        
    def run(self):
        try:
            # openstd 验证码已改为浏览器降级（NEED_BROWSER），不再传进程内 captcha 回调
            success, message = self.spider.download_pdf(self.standard_info)
            self.finished.emit(success, message)
        except Exception as e:
            self.finished.emit(False, f"下载异常: {str(e)}")
        finally:
            try:
                self.spider.progress_updated.disconnect(self.progress.emit)
            except (TypeError, RuntimeError):
                pass


class DirectoryScanWorker(QThread):
    """文档管理目录扫描（后台线程，避免大目录阻塞 UI）。"""
    finished = pyqtSignal(object)

    def __init__(self, target):
        super().__init__()
        self.target = target

    def run(self):
        result = {
            'target': self.target,
            'records': [],
            'failed_files': [],
            'total_files': 0,
            'error': None,
        }
        try:
            import os
            total_files = 0
            for entry in os.scandir(self.target):
                if not entry.is_file():
                    continue
                fname = entry.name
                ext = os.path.splitext(fname)[1].lower()
                if ext in _FileNameParser.EXCLUDED_EXTENSIONS:
                    continue
                total_files += 1
                if ext not in _FileNameParser.DOC_EXTENSIONS:
                    result['failed_files'].append((entry.path, f"不支持的文件类型：{ext}"))
                    continue
                code, name = _FileNameParser.parse(entry.path)
                if code is None and name is None:
                    result['failed_files'].append(
                        (entry.path, "文件名不符合解析规则（缺少《》或可识别格式）")
                    )
                    continue
                result['records'].append({
                    "code": code or "(无编号)",
                    "name": name,
                    "path": entry.path,
                })
            result['total_files'] = total_files
        except PermissionError:
            result['error'] = ('permission', self.target)
        except OSError as e:
            result['error'] = ('os', str(e))
        except Exception as e:
            logger.warning("文档目录扫描异常: %s", e)
            result['error'] = ('os', str(e))
        self.finished.emit(result)


class LanIndexWarmWorker(QThread):
    """后台预热局域网标准索引，避免首次点下载时在 UI/关键路径上触发 rglob。"""
    finished = pyqtSignal(bool)

    def __init__(self, spider):
        super().__init__()
        self.spider = spider

    def run(self):
        ok = False
        try:
            items = self.spider._lan_index(force=True)
            ok = items is not None
            logger.info("[局域网索引] 预热完成 items=%s", 0 if items is None else len(items))
        except Exception as e:
            logger.warning("[局域网索引] 预热失败: %s", e)
        self.finished.emit(ok)


class LocalSearchWorker(QThread):
    """本地搜索工作线程：查询结果返回后自动在文档管理目录中匹配本地文件。
    独立于网络检索线程，不阻塞 UI；本地命中只影响「本地打开」按钮，不受下载开关控制。"""
    finished = pyqtSignal(list)  # 参数：命中的标准编号列表（用于 UI 更新）

    def __init__(self, spider, results):
        super().__init__()
        self.spider = spider
        self.results = results
        self._cancelled = False

    def cancel(self):
        """协作式取消（run 无事件循环，quit() 无效）。"""
        self._cancelled = True

    def run(self):
        hit_std_nos = []
        try:
            # 先建/刷新一次索引，避免首条命中时才冷 rglob
            try:
                self.spider._lan_index()
            except Exception as e:
                logger.debug("[本地搜索] 索引预热跳过: %s", e)
            for std_info in self.results:
                if self._cancelled:
                    break
                # 主标准
                lan_file = self.spider._find_in_lan(std_info.std_no)
                if lan_file is not None:
                    std_info.is_local = True
                    std_info.local_file_path = str(lan_file)
                    std_info.download_status = "可下载"
                    std_info.download_url = ""
                    std_info.detail_url = ""
                    hit_std_nos.append(std_info.std_no)
                    logger.info(f"[本地搜索] {std_info.std_no} 命中文档管理目录")

                if self._cancelled:
                    break
                # 替代标准
                replacement = getattr(std_info, "replacement", None)
                if replacement is not None:
                    lan_file_r = self.spider._find_in_lan(replacement.std_no)
                    if lan_file_r is not None:
                        replacement.is_local = True
                        replacement.local_file_path = str(lan_file_r)
                        replacement.download_status = "可下载"
                        replacement.download_url = ""
                        replacement.detail_url = ""
                        hit_std_nos.append(replacement.std_no)
                        logger.info(f"[本地搜索] 替代标准 {replacement.std_no} 命中文档管理目录")
        except Exception as e:
            logger.warning(f"[本地搜索] 异常: {str(e)}")
        if not self._cancelled:
            self.finished.emit(hit_std_nos)


class DownloadCheckWorker(QThread):
    """按需下载源探测线程：开启下载功能后，对标记为「检测中」的非本地结果联网探测下载源。
    不阻塞 UI；完成后由主窗口重绘当前页，将「检测中」固化为「可下载」或「未找到资源」。"""
    finished = pyqtSignal()

    def __init__(self, spider, results):
        super().__init__()
        self.spider = spider
        self.results = results
        self._cancelled = False
        self.gen = 0

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            if not self._cancelled:
                self.spider.detect_download_for_results(self.results)
        except Exception as e:
            logger.warning(f"[下载源探测] 异常: {str(e)}")
        if not self._cancelled:
            self.finished.emit()


class SilentPreviewDownloadWorker(QThread):
    """预览确认前的静默下载：写入临时 PDF，不进入用户下载目录。"""
    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str)  # ok, path_or_error

    def __init__(self, kind, std_no, dest_path, hcno=None, samr_url=None, parent=None):
        super().__init__(parent)
        self.kind = kind  # gxzl | openstd | std_samr | webfree
        self.std_no = std_no or ''
        self.dest_path = dest_path
        self.hcno = hcno
        self.samr_url = samr_url
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            if self._cancelled:
                self.finished.emit(False, "已取消")
                return
            import os
            from pathlib import Path
            dest = Path(self.dest_path)
            os.makedirs(str(dest.parent), exist_ok=True)

            if self.kind == 'gxzl':
                from spiders.gxzl_pdf_downloader import GxzlPdfDownloader

                def _cb(cur, total):
                    if self._cancelled:
                        return
                    self.progress.emit(f"正在准备 PDF… {cur + 1}/{total}")

                dl = GxzlPdfDownloader()
                ok = dl.download_standard(self.std_no, str(dest), progress_callback=_cb)
                if self._cancelled:
                    self.finished.emit(False, "已取消")
                    return
                if ok:
                    self.finished.emit(True, str(dest))
                else:
                    self.finished.emit(False, "下载失败")
                return

            if self.kind == 'openstd':
                from spiders.openstd_downloader import OpenstdPdfDownloader
                dl = OpenstdPdfDownloader(progress_callback=self.progress.emit)
                ok, msg = dl.download(self.hcno, dest)
                if self._cancelled:
                    self.finished.emit(False, "已取消")
                    return
                if ok:
                    self.finished.emit(True, str(dest))
                else:
                    self.finished.emit(False, "下载失败")
                return

            if self.kind == 'std_samr':
                from spiders.samr_downloader import SamrPdfDownloader
                dl = SamrPdfDownloader(progress_callback=self.progress.emit)
                ok, msg = dl.download(self.samr_url, dest)
                if self._cancelled:
                    self.finished.emit(False, "已取消")
                    return
                if ok:
                    self.finished.emit(True, str(dest))
                else:
                    self.finished.emit(False, "下载失败")
                return

            if self.kind == 'webfree':
                from config import Config
                from spiders.webfree_downloader import WebfreePdfDownloader
                try:
                    import requests
                    sess = requests.Session()
                except ImportError:
                    self.finished.emit(False, "requests 库未安装")
                    return
                password = None
                for src in Config.DOWNLOAD_SOURCES:
                    if src.get('parser') == 'webfree':
                        password = src.get('password')
                        break
                self.progress.emit("正在准备 PDF…")
                dl = WebfreePdfDownloader(sess, progress_callback=self.progress.emit)
                ok, msg = dl.download(self.std_no, dest, password)
                if self._cancelled:
                    self.finished.emit(False, "已取消")
                    return
                if ok:
                    self.finished.emit(True, str(dest))
                else:
                    self.finished.emit(False, "下载失败")
                return

            self.finished.emit(False, f"未知下载类型: {self.kind}")
        except Exception as e:
            logger.warning(f"[静默预览下载] 异常: {e}")
            self.finished.emit(False, str(e))
