"""脱敏与敏感信息扫描。

fixture 会提交进仓库，数据库也可能被导出，所以两处都要过同一套规则。

原则：**宁可多抹**。响应头/体里常见的账号标识（org/project id、邮箱、
request id 里的账号段）都会被动带出来，不是只有 Key 才敏感。
"""
from __future__ import annotations

import re

# 凭证类
_PATTERNS = [
    (re.compile(r"(sk-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]{6,}"), r"\1***"),
    (re.compile(r"(hai_tf-)[A-Za-z0-9]{6,}"), r"\1***"),
    (re.compile(r"(rc-)[a-f0-9]{8}[a-f0-9]+"), r"\1***"),
    (re.compile(r"([Bb]earer\s+)[A-Za-z0-9_\-\.]{8,}"), r"\1***"),
    (re.compile(r"(x-api-key\"?\s*[:=]\s*\"?)[^\s\",}]{6,}", re.I), r"\1***"),
    # 账号 / 组织 / 项目标识
    (re.compile(r"([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})"), r"***@\2"),
    (re.compile(r"(oneclick-(?:org|project)-)\d+"), r"\1***"),
    (re.compile(r"\b(org|project|account|tenant)[-_]?(?:id)?[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9\-]{6,}",
                re.I), r"\1_id=***"),
]

# 用于"扫描是否还有残留"的探测式正则（不替换）
_SUSPECT = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"hai_tf-[A-Za-z0-9]{20,}"),
    re.compile(r"rc-[a-f0-9]{20,}"),
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    re.compile(r"oneclick-(?:org|project)-\d+"),
]


def scrub_text(text: str | None) -> str:
    if not text:
        return text or ""
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def scrub_headers(headers: dict) -> dict:
    """抹掉敏感头本身，并对保留的头值做脱敏。"""
    drop = {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization"}
    out = {}
    for k, v in (headers or {}).items():
        if k.lower() in drop:
            continue
        out[k] = scrub_text(str(v))
    return out


def find_suspects(text: str) -> list[str]:
    """返回命中的可疑片段，用于测试断言。"""
    hits = []
    for pat in _SUSPECT:
        for m in pat.finditer(text or ""):
            hits.append(m.group(0)[:40])
    return hits
