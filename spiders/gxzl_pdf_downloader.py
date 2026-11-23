# -*- coding: utf-8 -*-
"""中国东盟标准云平台PDF下载器"""
import os
import time
import random
from io import BytesIO
from PIL import Image
from app_env import logger
from http_client import HAS_REQUESTS

if HAS_REQUESTS:
    import requests


class GxzlPdfDownloader:
    """中国东盟标准云平台PDF下载器"""
    
    # 稳定性配置
    MAX_RETRIES = 3          # 单页最大重试次数
    RETRY_BASE_DELAY = 2     # 重试基础延迟（秒）
    PAGE_TIMEOUT = 60        # 单页下载超时（秒）
    PAGES_TIMEOUT = 30       # 获取页数超时（秒）
    PAGE_DELAY_RANGE = (0.5, 1.5)  # 页间延迟范围（秒）
    MIN_IMAGE_SIZE = 1000    # 最小有效图片大小（字节）
    COMPLETION_THRESHOLD = 1.0  # 必须全页成功；缺页不得合并为「成功」PDF
    # 浅灰斜向水印「中国-东盟国家标准云平台」约 RGB 206～238；硬阈值抹白
    WATERMARK_THRESHOLD = 170
    
    def __init__(self):
        self.base_url = "https://bz.gxzl.org.cn"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": "i18n_redirected=zh"
        })
    
    def download_standard(self, std_no, output_path, progress_callback=None):
        """
        下载标准为PDF
        
        :param std_no: 标准编号，如 "GB/T 50326-2017"
        :param output_path: 输出PDF文件路径
        :param progress_callback: 进度回调函数 callback(current, total)
        :return: True表示成功，False表示失败
        """
        try:
            logger.info(f"[GXZL下载器] 开始下载: {std_no}")
            
            # 步骤1: 获取总页数（带重试）
            total_pages = self._get_total_pages_with_retry(std_no)
            if total_pages <= 0:
                logger.error(f"[GXZL下载器] 无法获取总页数")
                return False
            
            logger.info(f"[GXZL下载器] 总页数: {total_pages}")
            # 冒烟/联调可设 GXZL_MAX_PAGES=N 只下前 N 页
            try:
                cap = int(os.environ.get("GXZL_MAX_PAGES", "0") or "0")
            except ValueError:
                cap = 0
            if cap > 0 and total_pages > cap:
                logger.info(f"[GXZL下载器] 冒烟限页: {cap} (env GXZL_MAX_PAGES)")
                total_pages = cap
            
            # 步骤2: 逐页下载图片（带重试机制）
            images = [None] * total_pages  # 预分配列表，保持页码对应
            failed_pages = []  # 记录失败的页码
            
            for page_num in range(total_pages):
                if progress_callback:
                    progress_callback(page_num, total_pages)
                
                img_data = self._download_page_with_retry(std_no, page_num)
                
                if img_data:
                    try:
                        img = Image.open(BytesIO(img_data))
                        images[page_num] = img
                        logger.debug(f"[GXZL下载器] 第 {page_num + 1}/{total_pages} 页下载成功")
                    except Exception as e:
                        logger.warning(f"[GXZL下载器] 第 {page_num + 1} 页图片解析失败: {e}")
                        failed_pages.append(page_num)
                else:
                    logger.warning(f"[GXZL下载器] 第 {page_num + 1} 页下载失败（已重试{self.MAX_RETRIES}次）")
                    failed_pages.append(page_num)
                
                # 随机延迟，避免请求过快
                delay = random.uniform(*self.PAGE_DELAY_RANGE)
                time.sleep(delay)
            
            if progress_callback:
                progress_callback(total_pages, total_pages)

            if failed_pages:
                logger.error(
                    f"[GXZL下载器] 有缺页，拒绝合并: 失败页={[p+1 for p in failed_pages]} "
                    f"({len(failed_pages)}/{total_pages})"
                )
                return False

            # 过滤掉 None（理论上 failed_pages 为空时不应再有）
            valid_images = [img for img in images if img is not None]
            if len(valid_images) != total_pages:
                logger.error(
                    f"[GXZL下载器] 页数不一致: 有效={len(valid_images)} 期望={total_pages}"
                )
                return False

            # 步骤3: 按原页序去水印后合并为 PDF（保留页码对应，不跳页）
            logger.info(
                f"[GXZL下载器] 去水印并合并 {len(valid_images)} 页"
                f"（阈值={self.WATERMARK_THRESHOLD}）"
            )
            cleaned = [self._remove_watermark(img) for img in images]
            success = self._merge_to_pdf(cleaned, output_path)
            
            if success:
                file_size = os.path.getsize(output_path)
                logger.info(f"[GXZL下载器] PDF已保存: {output_path} ({file_size / 1024 / 1024:.2f} MB)")
            else:
                logger.error(f"[GXZL下载器] 合并PDF失败")
            
            return success
            
        except Exception as e:
            logger.error(f"[GXZL下载器] 下载失败: {e}")
            return False
    
    def _get_total_pages_with_retry(self, std_no):
        """获取标准总页数（带重试）"""
        for attempt in range(self.MAX_RETRIES):
            try:
                url = f"{self.base_url}/api/gxist-standard/standardstd/read-pages"
                params = {"page": 0, "no": std_no}
                
                resp = self.session.get(url, params=params, timeout=self.PAGES_TIMEOUT)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        pages = data.get('data', 0) or data.get('total', 0) or data.get('pages', 0)
                        if pages > 0:
                            return pages
                    elif isinstance(data, int) and data > 0:
                        return data
                
                logger.warning(f"[GXZL下载器] 获取页数失败（第{attempt+1}次）: {resp.status_code}")
                
            except requests.exceptions.Timeout:
                logger.warning(f"[GXZL下载器] 获取页数超时（第{attempt+1}次）")
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"[GXZL下载器] 获取页数连接错误（第{attempt+1}次）: {e}")
            except Exception as e:
                logger.warning(f"[GXZL下载器] 获取页数异常（第{attempt+1}次）: {e}")
            
            # 重试前等待
            if attempt < self.MAX_RETRIES - 1:
                delay = self.RETRY_BASE_DELAY * (2 ** attempt)
                logger.info(f"[GXZL下载器] {delay}秒后重试...")
                time.sleep(delay)
        
        return 0
    
    def _download_page_with_retry(self, std_no, page_num):
        """下载单页图片（带重试机制）"""
        for attempt in range(self.MAX_RETRIES):
            try:
                img_data = self._download_page_image(std_no, page_num)
                if img_data:
                    return img_data
                
                # 返回None但不抛异常，继续重试
                logger.debug(f"[GXZL下载器] 第 {page_num + 1} 页返回无效数据（第{attempt+1}次）")
                
            except requests.exceptions.Timeout:
                logger.warning(f"[GXZL下载器] 第 {page_num + 1} 页下载超时（第{attempt+1}次）")
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"[GXZL下载器] 第 {page_num + 1} 页连接错误（第{attempt+1}次）: {str(e)[:100]}")
            except Exception as e:
                logger.warning(f"[GXZL下载器] 第 {page_num + 1} 页下载异常（第{attempt+1}次）: {e}")
            
            # 重试前等待（指数退避）
            if attempt < self.MAX_RETRIES - 1:
                delay = self.RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
                time.sleep(delay)
        
        return None
    
    def _download_page_image(self, std_no, page_num):
        """下载单页图片（单次尝试）"""
        url = f"{self.base_url}/api/gxist-standard/standardstd/read-image"
        params = {"no": std_no, "page": page_num}
        
        # 设置Referer
        headers = {
            "Referer": f"{self.base_url}/standard/details"
        }
        
        resp = self.session.get(url, params=params, headers=headers, timeout=self.PAGE_TIMEOUT)
        
        if resp.status_code == 200:
            content_type = resp.headers.get('Content-Type', '')
            content_len = len(resp.content)
            
            # 检查是否为有效图片
            if content_len >= self.MIN_IMAGE_SIZE:
                # 检查PNG或JPEG文件头
                is_png = resp.content[:4] == b'\x89PNG'
                is_jpeg = resp.content[:3] == b'\xff\xd8\xff'
                is_image_type = 'image' in content_type.lower()
                
                if is_png or is_jpeg or is_image_type:
                    return resp.content
                else:
                    logger.debug(f"[GXZL下载器] 第 {page_num + 1} 页不是图片: {content_type}")
            else:
                logger.debug(f"[GXZL下载器] 第 {page_num + 1} 页内容太小: {content_len} bytes")
        else:
            logger.debug(f"[GXZL下载器] 第 {page_num + 1} 页HTTP错误: {resp.status_code}")
        
        return None
    
    def _remove_watermark(self, img):
        """硬阈值去水印：浅灰斜字/徽标 → 白；深色正文保留为黑（二值化）。"""
        try:
            gray = img.convert("L")
            bw = gray.point(
                lambda x, t=self.WATERMARK_THRESHOLD: 255 if x > t else 0
            )
            return bw.convert("RGB")
        except Exception as e:
            logger.warning(f"[GXZL下载器] 去水印失败，使用原图: {e}")
            return img.convert("RGB") if img.mode != "RGB" else img

    def _merge_to_pdf(self, images, output_path):
        """合并图片为PDF"""
        try:
            # 转换所有图片为RGB模式（PDF不支持RGBA）
            rgb_images = []
            for img in images:
                if img.mode == 'RGBA':
                    # 创建白色背景
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[3])
                    rgb_images.append(background)
                elif img.mode != 'RGB':
                    rgb_images.append(img.convert('RGB'))
                else:
                    rgb_images.append(img)
            
            # 保存为PDF
            if rgb_images:
                rgb_images[0].save(
                    output_path,
                    save_all=True,
                    append_images=rgb_images[1:],
                    resolution=150.0
                )
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"[GXZL下载器] 合并PDF异常: {e}")
            return False


# 测试函数
def test_download():
    """测试下载功能"""
    downloader = GxzlPdfDownloader()
    
    std_no = "GB/T 50326-2017"
    output_path = f"test_{std_no.replace('/', '_')}.pdf"
    
    def progress_callback(current, total):
        print(f"\r下载进度: {current}/{total} ({current/total*100:.1f}%)", end="", flush=True)
    
    success = downloader.download_standard(std_no, output_path, progress_callback)
    
    if success:
        print(f"\n\n下载成功: {output_path}")
    else:
        print(f"\n\n下载失败")


if __name__ == '__main__':
    test_download()
