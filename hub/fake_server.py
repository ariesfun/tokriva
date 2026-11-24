"""假 OpenAI / Anthropic 兼容服务器：回放真实抓包 + 合成异常场景。

为什么不是手写假响应：
真实事故里最有价值的细节（SSE 不带 [DONE]、403 返回 HTML、reasoning 吃光
token、分片到达节奏）手写很容易"想象错"，回放真实字节流才能复现原现象。

用法：
    python -m hub.fake_server --port 8899
    # 每个场景挂在 /<scenario>/v1/... 下，便于一次跑完全部用例

fixtures/ 下的 JSON 由 capture_fixture 生成，结构：
    {"status":200, "headers":{...}, "chunks":[[delay_ms, "raw text"], ...]}
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _load_fixtures() -> dict[str, dict]:
    out = {}
    if FIXTURE_DIR.exists():
        for f in sorted(FIXTURE_DIR.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                d.setdefault("name", f.stem)
                out[f.stem] = d
            except Exception:
                continue
    return out


# ---- 合成场景：覆盖分类器必须区分的边界 ----
def _synthetic() -> dict[str, dict]:
    sse_done = (
        'data: {"id":"x","choices":[{"index":0,"delta":{"content":"a"},'
        '"finish_reason":null}],"model":"fake-model"}\n\n'
        'data: {"id":"x","choices":[{"index":0,"delta":{"content":"b"},'
        '"finish_reason":null}],"model":"fake-model"}\n\n'
        'data: {"id":"x","choices":[{"index":0,"delta":{"content":"c"},'
        '"finish_reason":"stop"}],"model":"fake-model"}\n\n'
        'data: {"id":"x","choices":[],"usage":{"completion_tokens":3}}\n\n'
        "data: [DONE]\n\n"
    )
    return {
        "ok_sse_with_usage": {
            "status": 200,
            "headers": {"Content-Type": "text/event-stream",
                        "x-ratelimit-limit-user-daily-usd": "5",
                        "x-ratelimit-remaining-user-daily-usd": "4.5"},
            "chunks": [[0, sse_done]],
        },
        "ok_sse_no_usage": {
            "status": 200,
            "headers": {"Content-Type": "text/event-stream"},
            "chunks": [[0, sse_done.replace('"usage"', '"nousage"')]],
        },
        "unauthorized_401": {
            "status": 401,
            "headers": {"Content-Type": "application/json"},
            "chunks": [[0, '{"error":{"message":"invalid api key","type":"auth"}}']],
        },
        "rate_limited_429": {
            "status": 429,
            "headers": {"Content-Type": "application/json", "Retry-After": "1"},
            "chunks": [[0, '{"error":{"message":"model at capacity",'
                           '"code":"model_concurrency_rate_limit_exceeded"}}']],
        },
        "quota_403": {
            "status": 403,
            "headers": {"Content-Type": "application/json"},
            "chunks": [[0, '{"error":{"message":"insufficient_quota: 余额不足"}}']],
        },
        "server_500": {
            "status": 500,
            "headers": {"Content-Type": "application/json"},
            "chunks": [[0, '{"error":"internal"']],
        },
        "cloudflare_403_html": {
            "status": 403,
            "headers": {"Content-Type": "text/html; charset=UTF-8"},
            "chunks": [[0, "<!DOCTYPE html><html><head><title>Attention Required!"
                           "</title></head><body>Cloudflare</body></html>"]],
        },
        # 分片延迟是**累计**毫秒，且客户端按「每次 recv」打时间戳：同一次读取里的
        # 多个分片会共用同一个时刻（见 probe.py 的 now 变量）。若相邻两片间隔 0ms，
        # 快速机器（如 CI）上会被合并进同一次 recv，正文事件只剩 2 个不同时刻，
        # stream_mode 便会误判成 buffered。故三片严格等距 400ms，确保各自落在独立读取里。
        "slow_stream": {
            "status": 200,
            "headers": {"Content-Type": "text/event-stream"},
            "chunks": [
                [0, 'data: {"choices":[{"delta":{"content":"a"},"finish_reason":null}]}\n\n'],
                [400, 'data: {"choices":[{"delta":{"content":"b"},"finish_reason":null}]}\n\n'],
                [800, 'data: {"choices":[{"delta":{"content":"c"},"finish_reason":"stop"}]}\n\n'],
            ],
        },
        "reasoning_starved": {
            "status": 200,
            "headers": {"Content-Type": "text/event-stream"},
            "chunks": [
                [0, 'data: {"choices":[{"delta":{"reasoning_content":"thinking..."},'
                    '"finish_reason":null}]}\n\n'],
                [60, 'data: {"choices":[{"delta":{},"finish_reason":"length"}],'
                     '"usage":{"completion_tokens":8,"reasoning_tokens":8}}\n\n'],
            ],
        },
    }


SCENARIOS: dict[str, dict] = {**_synthetic(), **_load_fixtures()}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静音
        pass

    def _scenario(self):
        # /<scenario>/v1/chat/completions
        parts = [p for p in self.path.split("/") if p]
        if not parts:
            return None, ""
        return parts[0], "/" + "/".join(parts[1:])

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def do_GET(self):
        name, sub = self._scenario()
        if sub.endswith("/models"):
            body = json.dumps({"data": [{"id": "fake-model", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._replay(name, sub)

    def do_POST(self):
        name, sub = self._scenario()
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # 丢弃请求体
        self._replay(name, sub)

    def _replay(self, name: str, sub: str):
        sc = SCENARIOS.get(name)
        if not sc:
            body = json.dumps({"error": f"unknown scenario {name}"}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(sc.get("status", 200))
        for k, v in (sc.get("headers") or {}).items():
            self.send_header(k, v)
        # 用 chunked 让客户端能按块读到，模拟真实流式
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        last = 0.0
        for item in sc.get("chunks", []):
            delay, text = item[0], item[1]
            if delay > last:
                time.sleep((delay - last) / 1000.0)
                last = delay
            payload = text.encode("utf-8")
            if not payload:
                continue
            self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def serve(port: int = 0) -> ThreadingHTTPServer:
    """port=0 交给系统分配，避免 Windows 保留端口段（实测 8931 会被拒）。"""
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    a = ap.parse_args()
    print(f"fake server on http://127.0.0.1:{a.port}  场景 {len(SCENARIOS)} 个：")
    for k in sorted(SCENARIOS):
        print("  /" + k + "/v1/chat/completions")
    serve(a.port).serve_forever()


if __name__ == "__main__":
    main()
