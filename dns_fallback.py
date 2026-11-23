# -*- coding: utf-8 -*-
"""系统 DNS 失败时的主机解析兜底。

部分政府子域（如 jtst.mot.gov.cn）在部分运营商/本机 DNS 上解析失败，
但公共 DNS（8.8.8.8 / 223.5.5.5）与固定 IP 仍可用。
通过包装 socket.getaddrinfo：先走系统解析，失败再试 DoH，再试内置 IP。
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.request

# 公开记录与实测可用的备用 A 记录（TTL 短，仅作 DNS 全挂时的最后手段）
_STATIC_FALLBACK_IPS = {
    "jtst.mot.gov.cn": ("202.96.42.20",),
}

_DOH_ENDPOINTS = (
    "https://dns.alidns.com/resolve?name={host}&type=A",
    "https://dns.google/resolve?name={host}&type=A",
)

_lock = threading.Lock()
_installed = False
_orig_getaddrinfo = socket.getaddrinfo
_cache = {}  # host -> (expire_ts, [ip, ...])
_CACHE_TTL = 300
_in_fallback = threading.local()  # 标记当前线程正在兜底解析，避免 DoH 请求再次触发兜底


def _doh_lookup(host: str):
    """返回 IPv4 列表；失败返回空列表。"""
    ips = []
    for tmpl in _DOH_ENDPOINTS:
        url = tmpl.format(host=host)
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/dns-json", "User-Agent": "std-query-dns-fallback"},
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            for ans in data.get("Answer") or []:
                if ans.get("type") == 1 and ans.get("data"):
                    ips.append(ans["data"].strip())
            if ips:
                return ips
        except Exception:
            continue
    return ips


def _fallback_ips(host: str):
    import time

    now = time.time()
    with _lock:
        cached = _cache.get(host)
        if cached and cached[0] > now:
            return list(cached[1])

    ips = _doh_lookup(host)
    if not ips:
        ips = list(_STATIC_FALLBACK_IPS.get(host) or ())
    if ips:
        with _lock:
            _cache[host] = (now + _CACHE_TTL, tuple(ips))
    return ips


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        if not isinstance(host, str) or not host:
            raise
        # DoH 请求本身也要解析 dns.alidns.com；系统 DNS 全挂时若不拦截，
        # 会递归回到本函数直至 RecursionError
        if getattr(_in_fallback, 'active', False):
            raise
        _in_fallback.active = True
        try:
            # 仅对已知需要兜底或 DoH 能解的主机介入；避免全局改写所有失败域名
            ips = _fallback_ips(host)
        finally:
            _in_fallback.active = False
        if not ips and host not in _STATIC_FALLBACK_IPS:
            raise
        if not ips:
            raise
        last_err = None
        for ip in ips:
            try:
                return _orig_getaddrinfo(ip, port, family, type, proto, flags)
            except socket.gaierror as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        raise


def install_dns_fallback():
    """幂等安装 getaddrinfo 包装。在应用入口尽早调用。"""
    global _installed
    with _lock:
        if _installed:
            return
        socket.getaddrinfo = _patched_getaddrinfo
        _installed = True
