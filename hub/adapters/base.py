"""适配器：三种协议 + 鉴权差异 + 能力声明。

  openai_chat        POST /chat/completions   Authorization: Bearer
  openai_responses   POST /responses          Authorization: Bearer
  anthropic_messages POST /messages           x-api-key + anthropic-version

实测要点（2026-11-21）：
- AMD 官方端点同时提供 chat/completions 与 /messages，但**没有 /responses**。
  之前接的 127.0.0.1:8791 是本地翻译桥（把 responses 翻成 chat），不是官方端点。
- AMD 的 usage 里直接带 USD 成本，可用于日限额追踪。
- d3 / d3-flash / Clef 不是对话模型（400 提示走 /v1/systemone），探测时要能识别。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..capability import parse_model_entry

SUPPORTED = "SUPPORTED"
PARTIAL = "PARTIALLY_SUPPORTED"
MANUAL = "MANUAL_REQUIRED"
UNSUPPORTED = "UNSUPPORTED"
UNKNOWN = "UNKNOWN"


@dataclass
class StreamReading:
    """一次流式读取的结果。"""
    first_byte_ms: float | None = None   # TTFB
    first_token_ms: float | None = None  # TTFT：首个正文 token（不含 reasoning）
    last_token_ms: float | None = None
    output_tokens: int | None = None
    token_source: str = "ESTIMATED"
    content: str = ""
    reasoning: str = ""
    saw_any_bytes: bool = False
    done: bool = False
    content_events: list = field(default_factory=list)
    recv_count: int = 0
    served_model: str | None = None
    cost_usd: float | None = None
    usage_raw: dict | None = None
    reasoning_tokens: int | None = None
    finish_seen: bool = False            # 收到 finish_reason / message_stop
    finish_ms: float | None = None

    def mark_content(self, t_rel_ms: float) -> None:
        self.content_events.append(round(t_rel_ms, 1))

    def stream_mode(self) -> str:
        """真流式判定。

        Packet / AMD 常把响应缓冲后一次性下发，所有分片落在同一毫秒。
        用分片时间戳的离散度判定，避免把缓冲当流式、算出假 TPS。
        """
        ev = self.content_events
        if len(ev) <= 1:
            return "single"
        span = ev[-1] - ev[0]
        if span < 50 or len(set(ev)) <= 2:
            return "buffered"
        return "true_stream"


class Adapter:
    name = "base"
    models_path = "/models"
    supports = {
        "list_models": SUPPORTED,
        "check_auth": SUPPORTED,
        "get_quota": UNKNOWN,
        "get_usage": UNKNOWN,
        "get_account_status": UNKNOWN,
        "stream_benchmark": SUPPORTED,
    }

    def auth_headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def generate_path(self) -> str:
        raise NotImplementedError

    def stream_body(self, model: str, prompt: str, max_tokens: int,
                    include_usage: bool = True) -> dict:
        raise NotImplementedError

    def parse_stream_line(self, line: str, reading: StreamReading,
                          t_now: float, t0: float) -> None:
        raise NotImplementedError

    def extract_models(self, payload: dict) -> list[dict]:
        out = []
        for m in payload.get("data", []) or []:
            if isinstance(m, dict):
                parsed = parse_model_entry(m)
                if parsed.get("model_id"):
                    out.append(parsed)
        return out


class OpenAIChatAdapter(Adapter):
    name = "openai_chat"

    def generate_path(self) -> str:
        return "/chat/completions"

    def stream_body(self, model, prompt, max_tokens, include_usage=True):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "stream": True,
        }
        # 实测 AMD 网关开启 include_usage 会把单次耗时从 39s 拉到 73s，
        # 因此做成按端点开关，默认开、慢网关闭。
        if include_usage:
            body["stream_options"] = {"include_usage": True}
        return body

    def parse_stream_line(self, line, reading, t_now, t0):
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if data == "[DONE]":
            reading.done = True
            return
        try:
            obj = json.loads(data)
        except Exception:
            return

        if obj.get("model") and not reading.served_model:
            reading.served_model = obj["model"]

        usage = obj.get("usage")
        if usage:
            reading.usage_raw = usage
            if usage.get("completion_tokens"):
                reading.output_tokens = usage["completion_tokens"]
                reading.token_source = "API"
            if usage.get("cost") is not None:
                reading.cost_usd = usage["cost"]
            rd = usage.get("reasoning_tokens")
            if rd is None and isinstance(usage.get("completion_tokens_details"), dict):
                rd = usage["completion_tokens_details"].get("reasoning_tokens")
            if rd:
                reading.reasoning_tokens = rd

        for ch in obj.get("choices", []) or []:
            if ch.get("finish_reason") and not reading.finish_seen:
                reading.finish_seen = True
                reading.finish_ms = (t_now - t0) * 1000
            delta = ch.get("delta") or {}
            reasoning = delta.get("reasoning_content")
            content = delta.get("content")
            if reasoning:
                reading.reasoning += reasoning
                reading.saw_any_bytes = True
                if reading.first_byte_ms is None:
                    reading.first_byte_ms = (t_now - t0) * 1000
            if content:
                reading.content += content
                reading.saw_any_bytes = True
                rel = (t_now - t0) * 1000
                if reading.first_byte_ms is None:
                    reading.first_byte_ms = rel
                if reading.first_token_ms is None:
                    reading.first_token_ms = rel
                reading.last_token_ms = rel
                reading.mark_content(rel)


class OpenAIResponsesAdapter(Adapter):
    name = "openai_responses"

    def generate_path(self) -> str:
        return "/responses"

    def stream_body(self, model, prompt, max_tokens, include_usage=True):
        return {"model": model, "input": prompt,
                "max_output_tokens": max_tokens, "stream": True}

    def parse_stream_line(self, line, reading, t_now, t0):
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if not data:
            return
        try:
            obj = json.loads(data)
        except Exception:
            return
        etype = obj.get("type", "")
        if not reading.served_model:
            r0 = obj.get("response")
            if isinstance(r0, dict) and r0.get("model"):
                reading.served_model = r0["model"]

        if etype == "response.output_text.delta":
            delta = obj.get("delta") or ""
            if delta:
                reading.content += delta
                reading.saw_any_bytes = True
                rel = (t_now - t0) * 1000
                if reading.first_byte_ms is None:
                    reading.first_byte_ms = rel
                if reading.first_token_ms is None:
                    reading.first_token_ms = rel
                reading.last_token_ms = rel
                reading.mark_content(rel)
        elif etype == "response.reasoning_text.delta":
            d = obj.get("delta") or ""
            if d:
                reading.reasoning += d
                reading.saw_any_bytes = True
                if reading.first_byte_ms is None:
                    reading.first_byte_ms = (t_now - t0) * 1000
        elif etype in ("response.completed", "response.incomplete"):
            resp = obj.get("response") or {}
            usage = resp.get("usage") or {}
            reading.usage_raw = usage
            if usage.get("output_tokens"):
                reading.output_tokens = usage["output_tokens"]
                reading.token_source = "API"
            reading.done = True


class AnthropicMessagesAdapter(Adapter):
    """Anthropic Messages API。

    鉴权是 x-api-key（不是 Bearer），且必须带 anthropic-version。
    流式事件：message_start / content_block_delta(text_delta|thinking_delta)
             / message_delta(usage) / message_stop
    """
    name = "anthropic_messages"
    anthropic_version = "2023-06-01"

    def auth_headers(self, token: str) -> dict:
        return {"x-api-key": token, "anthropic-version": self.anthropic_version}

    def generate_path(self) -> str:
        return "/messages"

    def stream_body(self, model, prompt, max_tokens, include_usage=True):
        return {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }

    def parse_stream_line(self, line, reading, t_now, t0):
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if not data:
            return
        if data == "[DONE]":
            reading.done = True
            return
        try:
            obj = json.loads(data)
        except Exception:
            return
        etype = obj.get("type", "")

        if etype == "message_start":
            msg = obj.get("message") or {}
            if msg.get("model"):
                reading.served_model = msg["model"]
            u = msg.get("usage") or {}
            if u:
                reading.usage_raw = u
        elif etype == "content_block_delta":
            d = obj.get("delta") or {}
            dt = d.get("type")
            if dt == "thinking_delta" and d.get("thinking"):
                reading.reasoning += d["thinking"]
                reading.saw_any_bytes = True
                if reading.first_byte_ms is None:
                    reading.first_byte_ms = (t_now - t0) * 1000
            elif dt == "text_delta" and d.get("text"):
                reading.content += d["text"]
                reading.saw_any_bytes = True
                rel = (t_now - t0) * 1000
                if reading.first_byte_ms is None:
                    reading.first_byte_ms = rel
                if reading.first_token_ms is None:
                    reading.first_token_ms = rel
                reading.last_token_ms = rel
                reading.mark_content(rel)
        elif etype == "message_delta":
            u = obj.get("usage") or {}
            if u:
                reading.usage_raw = {**(reading.usage_raw or {}), **u}
                if u.get("output_tokens"):
                    reading.output_tokens = u["output_tokens"]
                    reading.token_source = "API"
        elif etype == "message_stop":
            reading.finish_seen = True
            reading.finish_ms = (t_now - t0) * 1000
            reading.done = True


ADAPTERS = {
    "openai_chat": OpenAIChatAdapter,
    "openai_responses": OpenAIResponsesAdapter,
    "anthropic_messages": AnthropicMessagesAdapter,
}


def get_adapter(protocol: str) -> Adapter:
    cls = ADAPTERS.get(protocol)
    if not cls:
        raise ValueError(f"未知协议: {protocol}（可选 {list(ADAPTERS)}）")
    return cls()
