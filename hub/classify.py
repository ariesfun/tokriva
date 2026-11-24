"""错误分类：把「通 / 不通」拆成可处置的状态。

设计文档 3.4 的 8 个状态，加上实测中发现的 2 个：
  RESPONSE_INVALID   —— HTTP 200/403 但返回 HTML（Cloudflare 风控页、登录页、公告页）
  REASONING_STARVED  —— 响应合法但 content 为空、全部 token 被 reasoning 消耗

后两个都来自本次对真实端点的探测：Packet.ai 直连返回 CF 403 HTML，
LightVela 的 auto 路由到推理模型，8 token 全被 reasoning 吃掉。
"""
from __future__ import annotations

import re

# 服务商错误码 / 文案到状态的映射（按优先级从具体到宽泛）
QUOTA_PATTERNS = [
    r"insufficient[_ ]?(quota|balance|credit)",
    r"quota[_ ]?exceeded",
    r"exceeded your current quota",
    r"billing",
    r"credit balance is too low",
    r"余额不足",
    r"额度不足",
    r"欠费",
]
MODEL_GONE_PATTERNS = [
    r"model[_ ]?not[_ ]?found",
    r"no such model",
    r"does not exist",
    r"model .* (is )?(not available|deprecated|decommissioned)",
    r"模型不存在",
    r"已下线",
]
AUTH_PATTERNS = [
    r"invalid[_ ]?api[_ ]?key",
    r"incorrect api key",
    r"unauthorized",
    r"authentication",
    r"invalid token",
]


def _match(patterns: list[str], text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in patterns)


def classify_http(status: int, body: str, content_type: str,
                 shape: str) -> tuple[str, str]:
    """返回 (status, error_kind)。

    shape: json / html / sse / empty / text
    """
    # 1) 非 JSON 的 2xx / 403 —— 典型是 Cloudflare 或网关风控页
    if shape in ("html",) or ("text/html" in (content_type or "")):
        if status == 403:
            return "RESPONSE_INVALID", "cloudflare_or_waf_html"
        if 200 <= status < 300:
            return "RESPONSE_INVALID", "html_instead_of_json"
        return "ENDPOINT_ERROR", f"html_status_{status}"

    # 2) 额度 / 模型文案必须优先于状态码判断。
    # 实测发现：额度耗尽常以 403 返回，若先按 403 判 AUTH_FAILED 会误判，
    # 导致"额度用完"被当成"Key 失效"，处置完全不同（前者等刷新，后者换 Key）。
    if _match(QUOTA_PATTERNS, body):
        return "QUOTA_EXHAUSTED", "quota_text"

    if _match(MODEL_GONE_PATTERNS, body):
        return "MODEL_GONE", "model_text"

    # 3) 鉴权
    if status in (401, 403):
        if _match(AUTH_PATTERNS, body) or status == 401:
            return "AUTH_FAILED", f"http_{status}"
        return "AUTH_FAILED", f"http_{status}_forbidden"

    # 4) 限流
    if status == 429:
        return "RATE_LIMITED", "http_429"

    # 5) 服务端错误
    if status >= 500:
        return "ENDPOINT_ERROR", f"http_{status}"

    # 6) 其他 4xx
    if 400 <= status < 500:
        return "ENDPOINT_ERROR", f"http_{status}"

    if 200 <= status < 300:
        return "HEALTHY", ""
    return "ENDPOINT_ERROR", f"http_{status}"


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """网络层异常。"""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    # 密钥未配置不是网络问题，单独归类，避免界面显示「网络错误」误导
    if "环境变量" in msg and "为空" in msg:
        return "NOT_CONFIGURED", "missing_key"
    if "timeout" in name or "timed out" in msg:
        return "ENDPOINT_ERROR", "timeout"
    if any(k in msg for k in ("name or service", "getaddrinfo", "nodename")):
        return "NETWORK_ERROR", "dns"
    if any(k in msg for k in ("ssl", "tls", "certificate")):
        return "NETWORK_ERROR", "tls"
    if any(k in msg for k in ("connection refused", "connection reset",
                              "connect", "unreachable", "broken pipe")):
        return "NETWORK_ERROR", "tcp"
    return "NETWORK_ERROR", name


def detect_shape(raw: bytes, content_type: str) -> str:
    if not raw:
        return "empty"
    head = raw[:200].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return "html"
    if head.startswith(b"data:") or head.startswith(b"event:"):
        return "sse"
    if head.startswith(b"{") or head.startswith(b"["):
        return "json"
    if "text/html" in (content_type or "").lower():
        return "html"
    return "text"


def redact(body: str, limit: int = 400) -> str:
    """脱敏：抹掉可能出现的 key / token 片段。"""
    if not body:
        return ""
    body = re.sub(r"(sk-[A-Za-z0-9_\-]{8})[A-Za-z0-9_\-]+", r"\1***", body)
    body = re.sub(r"(hai_tf-)[A-Za-z0-9]+", r"\1***", body)
    body = re.sub(r"(rc-)[a-f0-9]{8}[a-f0-9]+", r"\1***", body)
    body = re.sub(r"([Bb]earer\s+)[A-Za-z0-9_\-\.]+", r"\1***", body)
    return body[:limit]
