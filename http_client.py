# -*- coding: utf-8 -*-
"""HTTP 客户端：requests 可选依赖检测与标准库兜底、SSL 优雅降级。"""
import ssl
import time
import warnings
import urllib.request
import urllib.parse
import urllib.error
from http.cookiejar import CookieJar
from app_env import logger

try:
    import requests
    HAS_REQUESTS = True
    logger.info("requests 库导入成功")
except ImportError as e:
    requests = None
    HAS_REQUESTS = False
    logger.error(f"requests 库导入失败: {e}")
    logger.warning("将使用 urllib 标准库进行网络请求")

if HAS_REQUESTS:
    import urllib3
    warnings.filterwarnings('ignore', category=urllib3.exceptions.InsecureRequestWarning)
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# 连接超时上限：总超时 15s 时，连接阶段最多 5s，避免坏站把线程长期卡住
_CONNECT_TIMEOUT_CAP = 5


class _StdlibResponse:
    def __init__(self, resp, content):
        self.status_code = resp.getcode()
        self.content = content
        self._encoding = 'utf-8'
        self._text = None
        self.headers = resp.info()
    
    @property
    def text(self):
        if self._text is None:
            self._text = self.content.decode(self._encoding, errors='replace')
        return self._text
    
    @text.setter
    def text(self, value):
        self._text = value
    
    @property
    def encoding(self):
        return self._encoding
    
    @encoding.setter
    def encoding(self, value):
        if value and value != self._encoding:
            self._encoding = value
            self._text = None  # 强制下次访问 text 时重新解码
    
    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i+chunk_size]


class _StdlibSession:
    def __init__(self):
        self.headers = {}
        self.cookie_jar = CookieJar()
        # 默认启用 SSL 证书验证（与 requests 行为一致）
        self._ssl_context = ssl.create_default_context()
        # 降级用的宽松 SSL 上下文（不校验证书）
        self._ssl_context_fallback = ssl.create_default_context()
        self._ssl_context_fallback.check_hostname = False
        self._ssl_context_fallback.verify_mode = ssl.CERT_NONE
        # 标准库兜底路径：Win10/11 与 Python 3.13 均支持 TLS 1.2+，不再放开 TLS 1.0
        for ctx in (self._ssl_context, self._ssl_context_fallback):
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    
    def _build_request(self, method, url, headers=None, data=None, timeout=30, stream=False, use_fallback_ssl=False):
        req_headers = dict(self.headers)
        if headers:
            req_headers.update(headers)
        
        if data and isinstance(data, dict):
            data = urllib.parse.urlencode(data).encode('utf-8')
            req_headers.setdefault('Content-Type', 'application/x-www-form-urlencoded')
        
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())
        # 显式传入 ProxyHandler({}) 禁用环境变量代理，避免 127.0.0.1:7890 等本地代理失效时全部请求失败
        ssl_ctx = self._ssl_context_fallback if use_fallback_ssl else self._ssl_context
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
            urllib.request.HTTPSHandler(context=ssl_ctx),
        )
        return req, opener
    
    def _do_request(self, method, url, headers=None, data=None, timeout=30, stream=False):
        """执行请求，带 SSL 降级重试逻辑"""
        if isinstance(timeout, (tuple, list)):
            timeout = timeout[-1]  # urllib 只接受单个超时值
        host = _host_of(url)
        # 检查该主机是否已在降级列表
        use_fallback = host in _ssl_fallback_hosts and not _is_ssl_fallback_expired(host)
        
        req, opener = self._build_request(method, url, headers=headers, data=data, 
                                          timeout=timeout, use_fallback_ssl=use_fallback)
        try:
            resp = opener.open(req, timeout=timeout)
            content = resp.read()
            return _StdlibResponse(resp, content)
        except urllib.error.HTTPError as e:
            content = e.read() if hasattr(e, 'read') else b''
            return _StdlibResponse(e, content)
        except ssl.SSLCertVerificationError as e:
            # SSL 证书校验失败，降级并重试
            if not use_fallback:
                _ssl_fallback_hosts[host] = time.time()
                _log_ssl_degradation(url)
                # 使用降级 SSL 上下文重试
                req, opener = self._build_request(method, url, headers=headers, data=data,
                                                  timeout=timeout, use_fallback_ssl=True)
                try:
                    resp = opener.open(req, timeout=timeout)
                    content = resp.read()
                    return _StdlibResponse(resp, content)
                except urllib.error.HTTPError as e:
                    content = e.read() if hasattr(e, 'read') else b''
                    return _StdlibResponse(e, content)
                except Exception as e2:
                    raise RuntimeError(f"HTTP request failed: {str(e2)}")
            raise RuntimeError(f"HTTP request failed: {str(e)}")
        except Exception as e:
            # 检查是否是 SSL 相关错误（不同 Python 版本异常类型可能不同）
            err_str = str(e).lower()
            if 'ssl' in err_str or 'certificate' in err_str:
                if not use_fallback:
                    _ssl_fallback_hosts[host] = time.time()
                    _log_ssl_degradation(url)
                    req, opener = self._build_request(method, url, headers=headers, data=data,
                                                      timeout=timeout, use_fallback_ssl=True)
                    try:
                        resp = opener.open(req, timeout=timeout)
                        content = resp.read()
                        return _StdlibResponse(resp, content)
                    except urllib.error.HTTPError as e2:
                        content = e2.read() if hasattr(e2, 'read') else b''
                        return _StdlibResponse(e2, content)
                    except Exception as e3:
                        raise RuntimeError(f"HTTP request failed: {str(e3)}")
            raise RuntimeError(f"HTTP request failed: {str(e)}")
    
    def get(self, url, headers=None, timeout=30, stream=False, **kwargs):
        return self._do_request('GET', url, headers=headers, timeout=timeout)
    
    def post(self, url, headers=None, data=None, timeout=30, stream=False, **kwargs):
        return self._do_request('POST', url, headers=headers, data=data, timeout=timeout)


# SSL 降级状态：{host: timestamp}，5 分钟后自动清除，避免永久降级
_ssl_fallback_hosts: dict = {}
_SSL_FALLBACK_TTL = 300  # 5 分钟


def _log_ssl_degradation(url: str) -> None:
    """记录 SSL 证书验证降级（verify=False），按主机去重，每主机仅记录一次 WARNING。"""
    try:
        host = urllib.parse.urlparse(url).hostname or url
    except Exception:
        host = url
    logger.warning("SSL 证书验证已降级（verify=False），目标主机: %s（5分钟后自动恢复验证）", host)


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).hostname or url
    except Exception:
        return url


def _is_ssl_fallback_expired(host: str) -> bool:
    """检查 SSL 降级是否已过期（超过 TTL 则清除）"""
    if host not in _ssl_fallback_hosts:
        return True
    ts = _ssl_fallback_hosts[host]
    if time.time() - ts > _SSL_FALLBACK_TTL:
        del _ssl_fallback_hosts[host]
        return True
    return False


def _http_request(session, method: str, url: str, **kwargs):
    """带 SSL 优雅降级的 HTTP 请求封装。

    默认开启证书校验（verify=True）；仅当某主机抛出 SSLError（证书相关）时，
    才把该主机加入降级集合并对该主机后续请求关闭校验。降级状态 5 分钟后自动清除。
    ConnectionError / Timeout 不触发 SSL 降级（避免断网被误记为证书问题）。
    """
    kwargs.pop('verify', None)
    # requests 支持 (connect, read) 元组；给单值超时补上更短的连接超时，
    # 让关闭/取消时最多等连接阶段 5s，而不是整段 15s
    _t = kwargs.get('timeout')
    if HAS_REQUESTS and isinstance(_t, (int, float)) and not isinstance(_t, bool):
        kwargs['timeout'] = (min(_CONNECT_TIMEOUT_CAP, float(_t)), float(_t))
    host = _host_of(url)
    func = getattr(session, method)

    if HAS_REQUESTS:
        # 检查降级是否过期
        if host in _ssl_fallback_hosts and not _is_ssl_fallback_expired(host):
            return func(url, verify=False, **kwargs)
        for attempt in range(2):
            try:
                return func(url, verify=(attempt == 0), **kwargs)
            except requests.exceptions.SSLError as e:
                _ssl_fallback_hosts[host] = time.time()
                _log_ssl_degradation(url)
                logger.debug(f"[{host}] SSLError, 降级重试: {e}")
                if attempt == 1:
                    raise
            except requests.exceptions.ConnectionError as e:
                # 断网/DNS/拒连：不写入 SSL 降级表
                logger.debug(f"[{host}] ConnectionError（不降级 SSL）: {e}")
                raise
            except requests.exceptions.Timeout as e:
                logger.debug(f"[{host}] Timeout: {e}")
                raise
    else:
        return func(url, **kwargs)
