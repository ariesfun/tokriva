"""把一次真实请求的字节流存成 fixture，供假服务器原样回放。

为什么存真实抓包：SSE 是否带 [DONE]、分片节奏、403 返回 HTML 这类细节，
手写假响应极易"想象错"，回放真实字节才能复现原现象。

用法：
    python -m hub.capture_fixture --endpoint AMD-Radeon --model Qwen3.8-27B \
        --max-tokens 64 --name amd_no_done

产物：fixtures/<name>.json，结构 {status, headers, chunks:[[delay_ms, text]]}
凭证不写入（只存响应，不存请求头）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub import db  # noqa: E402
from hub.run import DB_PATH, ENV_PATH, load_env  # noqa: E402
from hub.adapters.base import get_adapter  # noqa: E402
from hub.http_timing import open_timed, request_path  # noqa: E402
from hub import scrub  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"

_SECRET_KEYS = {"authorization", "x-api-key", "cookie", "set-cookie"}


def _clean_headers(h) -> dict:
    """剔除敏感头，并对保留项脱敏后写入 fixture。"""
    kept = {k: v for k, v in h if k.lower() not in _SECRET_KEYS}
    return scrub.scrub_headers(kept)


def capture(label: str, model: str, max_tokens: int, name: str,
            timeout: float = 60) -> Path:
    load_env()
    conn = db.connect(DB_PATH)
    ep = dict(conn.execute("SELECT * FROM endpoint WHERE label=?", (label,)).fetchone())
    adapter = get_adapter(ep["protocol"])
    token = os.environ.get(ep["auth_env"], "")

    url = ep["base_url"].rstrip("/") + adapter.generate_path()
    body = json.dumps(adapter.stream_body(model, "Say ok", max_tokens,
                                          bool(ep.get("include_usage", 1)))).encode()
    proxy = "http://127.0.0.1:7890" if ep.get("use_proxy") else None

    conn2, _ = open_timed(url, proxy, timeout=timeout)
    hdrs = adapter.auth_headers(token)
    hdrs.update({"Content-Type": "application/json",
                 "Accept": "text/event-stream",
                 "User-Agent": "Tokriva-Capture/0.1"})
    conn2.request("POST", request_path(url, proxy), body=body, headers=hdrs)
    resp = conn2.getresponse()

    chunks = []
    t0 = time.perf_counter()
    if conn2.sock:
        conn2.sock.settimeout(min(10.0, timeout))
    while time.perf_counter() - t0 < timeout:
        try:
            raw = resp.read1(8192) if hasattr(resp, "read1") else b""
        except Exception:
            break
        if not raw:
            break
        at = round((time.perf_counter() - t0) * 1000)
        chunks.append([at, scrub.scrub_text(raw.decode("utf-8", "replace"))])
        if len(chunks) > 400:
            break
    conn2.close()

    fx = {
        "name": name,
        "status": resp.status,
        "headers": _clean_headers(resp.getheaders()),
        "chunks": chunks,
        "source": {"endpoint": label, "model": model,
                   "max_tokens": max_tokens,
                   "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "note": "真实抓包回放；凭证与请求头未保存"},
    }
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURE_DIR / f"{name}.json"
    out.write_text(json.dumps(fx, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--name", required=True)
    ap.add_argument("--timeout", type=float, default=60)
    a = ap.parse_args()
    p = capture(a.endpoint, a.model, a.max_tokens, a.name, a.timeout)
    print("已写入", p, "大小", p.stat().st_size, "字节")


if __name__ == "__main__":
    main()
