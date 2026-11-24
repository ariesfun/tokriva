"""HTTP 探测底层：手工控制连接，拿到 DNS / TCP / TLS 分段耗时。

要点：
- HTTPS 走 HTTP 代理必须先用 CONNECT 建隧道再 TLS，否则请求会挂住
  （实测 Packet 走代理时不发 CONNECT 会一直阻塞）。
- 分段计时：DNS / TCP / TLS 分别测量，用于区分「本地网络慢」与「服务商慢」。
"""
from __future__ import annotations

import http.client
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass
class Timing:
    dns_ms: float | None = None
    tcp_ms: float | None = None
    connect_ms: float | None = None    # 代理 CONNECT 隧道耗时（含代理自己建连到远端）
    tls_ms: float | None = None        # 仅 TLS 握手
    connect_total_ms: float | None = None
    egress: str | None = None          # direct / proxy:<host:port>


def _resolve(host: str, port: int):
    t0 = time.perf_counter()
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return infos, (time.perf_counter() - t0) * 1000


def _proxy_tunnel(sock: socket.socket, target_host: str,
                  target_port: int) -> None:
    """向代理发 CONNECT，建立到目标主机的隧道。"""
    req = (
        f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        f"Host: {target_host}:{target_port}\r\n"
        "Proxy-Connection: keep-alive\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(1024)
        if not chunk:
            raise ConnectionError("代理在 CONNECT 阶段关闭连接")
        buf += chunk
        if len(buf) > 65536:
            raise ConnectionError("CONNECT 响应过长")
    status_line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if " 200" not in status_line:
        raise ConnectionError(f"CONNECT 失败: {status_line}")


def open_timed(url: str, proxy: str | None, timeout: float,
               egress_label: str | None = None):
    """建立连接，返回 (conn, timing)。

    conn 的 sock 已就绪，调用方直接用 conn.request 即可；
    HTTPS 时 conn 为 HTTPSConnection 且 sock 已是 TLS 套接字。
    """
    u = urlsplit(url)
    host = u.hostname
    port = u.port or (443 if u.scheme == "https" else 80)
    is_tls = u.scheme == "https"
    timing = Timing()
    timing.egress = f"proxy:{egress_label}" if (proxy and egress_label) else \
                    (f"proxy:{urlsplit(proxy).netloc}" if proxy else "direct")
    ctx = ssl.create_default_context()

    if proxy:
        p = urlsplit(proxy)
        phost, pport = p.hostname, p.port or 8080
        infos, dns_ms = _resolve(phost, pport)
        timing.dns_ms = dns_ms
        t0 = time.perf_counter()
        sock = socket.create_connection((infos[0][4][0], pport), timeout=timeout)
        timing.tcp_ms = (time.perf_counter() - t0) * 1000

        if is_tls:
            # CONNECT 与 TLS 必须分开：CONNECT 里含代理自己去连远端的时间，
            # 混进 tls_ms 会让"网络慢"的归因错位。
            tc = time.perf_counter()
            _proxy_tunnel(sock, host, port)
            timing.connect_ms = (time.perf_counter() - tc) * 1000
            t1 = time.perf_counter()
            sock = ctx.wrap_socket(sock, server_hostname=host)
            timing.tls_ms = (time.perf_counter() - t1) * 1000
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
            conn.sock = sock
        else:
            conn = http.client.HTTPConnection(phost, pport, timeout=timeout)
            conn.sock = sock
        return conn, timing

    infos, dns_ms = _resolve(host, port)
    timing.dns_ms = dns_ms
    t0 = time.perf_counter()
    sock = socket.create_connection((infos[0][4][0], port), timeout=timeout)
    timing.tcp_ms = (time.perf_counter() - t0) * 1000

    if is_tls:
        t1 = time.perf_counter()
        sock = ctx.wrap_socket(sock, server_hostname=host)
        timing.tls_ms = (time.perf_counter() - t1) * 1000
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn.sock = sock
    return conn, timing


def request_path(url: str, proxy: str | None) -> str:
    """走代理且是 http（无隧道）时用绝对 URI，其余用 path?query。"""
    u = urlsplit(url)
    p = u.path or "/"
    if u.query:
        p += "?" + u.query
    if proxy and u.scheme == "http":
        return url
    return p
