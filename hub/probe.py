"""探测引擎：L0（列模型）/ L1（小生成，非流式）/ L2（流式基准）。

每次探测都记录：
  - DNS / TCP / TLS 分段耗时（区分本地网络还是服务商慢）
  - TTFB / TTFT / 输出 TPS / 总耗时
  - 错误分类 + 脱敏错误体 + 响应形状
"""
from __future__ import annotations

import http.client
import json
import os
import time

from . import classify
from .adapters.base import StreamReading, get_adapter
from .http_timing import open_timed, request_path

DEFAULT_PROXY = "http://127.0.0.1:7890"

# 探针代码版本：优先 git 短 sha，取不到则回落包版本。目的是让历史数据能对应到代码。
def _probe_version() -> str:
    try:
        import subprocess
        from pathlib import Path
        # 探测进程本身是无控制台的（api.py 用 CREATE_NO_WINDOW 拉起），
        # 它再 spawn git 时同样会被分配新控制台窗口，所以这里也要静默。
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        root = Path(__file__).resolve().parent.parent
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=3,
                             creationflags=flags)
        if out.returncode == 0 and out.stdout.strip():
            sha = out.stdout.strip()
            # 有未提交改动时必须标脏，否则不同代码的结果会被记成同一个版本
            st = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                capture_output=True, text=True, timeout=3,
                                creationflags=flags)
            if st.returncode == 0 and st.stdout.strip():
                sha += "-dirty"
            return sha
    except Exception:
        pass
    return "0.1.0"


PROBE_VERSION = _probe_version()

# 响应头里值得留存的：限流与协商相关。凭证类一律不存。
_HEADER_KEEP_PREFIX = ("x-ratelimit", "ratelimit", "retry-after", "content-type",
                       "content-length", "transfer-encoding", "date", "server",
                       "x-request-id", "cf-ray", "anthropic-ratelimit")


def _slim_headers(resp) -> str:
    """只保留限流/协商类响应头，剔除 set-cookie / authorization 等敏感项。"""
    keep = {}
    try:
        for k, v in resp.getheaders():
            lk = k.lower()
            if any(lk.startswith(p) or p in lk for p in _HEADER_KEEP_PREFIX):
                keep[k] = v
    except Exception:
        return ""
    from . import scrub
    keep = scrub.scrub_headers(keep)
    return json.dumps(keep, ensure_ascii=False)[:2000]


def _truncate(text: str, limit: int = 4000) -> str:
    if text is None:
        return ""
    return text if len(text) <= limit else text[:limit] + f"...<truncated {len(text)-limit}>"


def _token(auth_env: str) -> str:
    tok = os.environ.get(auth_env, "")
    if not tok:
        raise RuntimeError(f"环境变量 {auth_env} 为空")
    return tok


def _proxy_for(ep: dict) -> str | None:
    return DEFAULT_PROXY if ep.get("use_proxy") else None


def _egress_label() -> str | None:
    """出口节点标识。代理节点质量会直接污染排名，必须可追溯。

    用 TOKRIVA_EGRESS_LABEL 标注当前节点（如 'hk-01'），不填则只记代理地址。
    """
    return os.environ.get("TOKRIVA_EGRESS_LABEL") or None


def probe_l0(ep: dict, timeout: float | None = None) -> dict:
    """L0：GET /models。通常不消耗额度。"""
    url = ep["base_url"].rstrip("/") + "/models"
    proxy = _proxy_for(ep)
    base = {
        "endpoint_id": ep["id"],
        "level": "L0",
        "timing_scope": "proxy_hop" if proxy else "direct",
        "connect_ms": None,
        "egress": None,
        "probe_version": PROBE_VERSION,
        "request_json": json.dumps({"method": "GET", "url": url}, ensure_ascii=False),
        "model_id": None,
        "proxied": 1 if proxy else 0,
        "token_source": None,
        "output_tokens": None,
        "tps": None,
        "ttft_ms": None,
        "ttfb_ms": None,
    }
    t0 = time.perf_counter()
    try:
        _to = timeout or ep.get("timeout_sec") or 25
        conn, timing = open_timed(url, proxy, timeout=_to, egress_label=_egress_label())
        base.update(dns_ms=timing.dns_ms, tcp_ms=timing.tcp_ms, tls_ms=timing.tls_ms,
                    connect_ms=timing.connect_ms, egress=timing.egress)
        hdrs = get_adapter(ep["protocol"]).auth_headers(_token(ep["auth_env"]))
        hdrs.update({"Accept": "application/json", "User-Agent": "Tokriva-Probe/0.1"})
        conn.request("GET", request_path(url, proxy), headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        base["resp_headers"] = _slim_headers(resp)
        total_ms = (time.perf_counter() - t0) * 1000
        ctype = resp.getheader("Content-Type", "")
        shape = classify.detect_shape(raw, ctype)
        body = raw.decode("utf-8", "replace")

        status, kind = classify.classify_http(resp.status, body, ctype, shape)
        base.update(
            status=status,
            http_status=resp.status,
            error_kind=kind,
            error_body=classify.redact(body) if status != "HEALTHY" else None,
            resp_body=_truncate(classify.redact(body)) if status != "HEALTHY" else None,
            response_shape=shape,
            total_ms=round(total_ms, 1),
        )
        if status == "HEALTHY" and shape == "json":
            try:
                models = get_adapter(ep["protocol"]).extract_models(json.loads(body))
                # free_only 端点（如 OpenRouter 聚合网关）只保留免费模型，
                # 否则 /models 会返回几百个付费模型，既污染榜单又可能误测烧额度。
                if ep.get("free_only") and models:
                    models = [m for m in models if m.get("is_free")]
                base["models"] = models
            except Exception:
                pass
        conn.close()
    except Exception as e:
        base.update(
            status=classify.classify_exception(e)[0],
            error_kind=classify.classify_exception(e)[1],
            error_body=classify.redact(str(e)),
            total_ms=round((time.perf_counter() - t0) * 1000, 1),
            response_shape="none",
        )
    return base


def probe_stream(ep: dict, model: str, level: str = "L2",
                 max_tokens: int = 128, prompt: str = "Count from 1 to 20.",
                 include_usage: bool = True,
                 timeout: float | None = None,
                 prompt_tag: str = "default") -> dict:
    """L1/L2：生成并流式读取，测 TTFT / TPS。

    L1 用 max_tokens 小、非流式也能测总耗时，但 TTFT 只能流式得到，
    所以这里统一走流式；L1 只是把 max_tokens 调小。
    """
    adapter = get_adapter(ep["protocol"])
    timeout = timeout or ep.get("timeout_sec") or 60
    url = ep["base_url"].rstrip("/") + adapter.generate_path()
    proxy = _proxy_for(ep)
    body = json.dumps(adapter.stream_body(model, prompt, max_tokens,
                                          include_usage)).encode()

    result = {
        "endpoint_id": ep["id"],
        "model_id": model,
        "level": level,
        "prompt_tag": prompt_tag,
        "proxied": 1 if proxy else 0,
        "reasoning_starved": False,
        "served_model": None,
        "cost_usd": None,
        "retry_after": None,
        "reasoning_tokens": None,
        "timing_scope": "proxy_hop" if proxy else "direct",
        "connect_ms": None,
        "egress": None,
        "probe_version": PROBE_VERSION,
        "request_json": _truncate(json.dumps(
            {"model": model, "max_tokens": max_tokens, "stream": True,
             "prompt": prompt, "include_usage": include_usage},
            ensure_ascii=False)),
        "resp_headers": None,
        "resp_body": None,
        "usage_json": None,
    }
    reading = StreamReading()
    chunk_times: list[float] = []
    t0 = time.perf_counter()

    try:
        conn, timing = open_timed(url, proxy, timeout=timeout, egress_label=_egress_label())
        result.update(dns_ms=timing.dns_ms, tcp_ms=timing.tcp_ms, tls_ms=timing.tls_ms,
                      connect_ms=timing.connect_ms, egress=timing.egress)
        hdrs = adapter.auth_headers(_token(ep["auth_env"]))
        hdrs.update({"Content-Type": "application/json",
                     "Accept": "text/event-stream",
                     "User-Agent": "Tokriva-Probe/0.1"})
        conn.request("POST", request_path(url, proxy), body=body, headers=hdrs)
        resp = conn.getresponse()
        ctype = resp.getheader("Content-Type", "")
        first_byte_at = None

        if resp.status != 200:
            raw = resp.read()
            total_ms = (time.perf_counter() - t0) * 1000
            shape = classify.detect_shape(raw, ctype)
            text = raw.decode("utf-8", "replace")
            status, kind = classify.classify_http(resp.status, text, ctype, shape)
            ra = resp.getheader("Retry-After")
            result.update(
                status=status, http_status=resp.status, error_kind=kind,
                retry_after=float(ra) if ra and ra.isdigit() else None,
                resp_headers=_slim_headers(resp),
                resp_body=_truncate(classify.redact(text)),
                error_body=classify.redact(text), response_shape=shape,
                total_ms=round(total_ms, 1),
                output_tokens=None, tps=None, ttft_ms=None, ttfb_ms=None,
                token_source=None,
            )
            conn.close()
            return result

        # 逐块读 socket，按到达时间切行。
        # 不能用 resp.readline()：它从缓冲区分行，多行会一次性返回，
        # 导致所有行拿到同一个时间戳，TTFT 与 TPS 直接被压平
        # （实测：三个端点 TPS 全部变成「不可靠」）。
        # 关键：AMD 的 SSE 不发 [DONE] 且保持连接不关，若阻塞在 recv
        # 会一直等到整体超时（实测 60s）。这里用「空闲超时 + finish 后宽限」：
        # 收到 finish_reason 后最多再等 800ms 收尾（拿 usage），之后立即结束。
        # 用 read1：只在一次底层读之后返回，时间戳贴近真实到达时间。
        # 不用 readline（会从缓冲区凑整行，时间戳被压平），
        # 也不用直接 recv 裸 socket（HTTPS/代理下拿不到正确的流对象）。
        if conn.sock is not None:
            conn.sock.settimeout(min(10.0, timeout))
        deadline = time.perf_counter() + timeout
        buf = b""
        raw_head = b""          # 原始字节流前 4KB，供 fixture 回放与事后核对
        while time.perf_counter() < deadline:
            if reading.finish_seen and reading.finish_ms is not None:
                if (time.perf_counter() - t0) * 1000 - reading.finish_ms > 800:
                    break
            try:
                chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
            except Exception:
                break
            if not chunk:
                break
            if len(raw_head) < 4096:
                raw_head += chunk[:4096 - len(raw_head)]
            chunk_times.append(round((time.perf_counter() - t0) * 1000, 1))
            now = time.perf_counter()
            if first_byte_at is None:
                first_byte_at = now
                result["ttfb_ms"] = round((now - t0) * 1000, 1)
            buf += chunk
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                line = line_bytes.decode("utf-8", "replace").rstrip("\r")
                adapter.parse_stream_line(line, reading, now, t0)
                if reading.done:
                    break
            if reading.done:
                break

        total_ms = (time.perf_counter() - t0) * 1000
        conn.close()

        # 计算 TPS：优先 usage 的 completion_tokens
        out_tokens = reading.output_tokens
        if out_tokens is None and reading.content:
            # 粗略估计：约 4 字符 / token
            out_tokens = max(1, len(reading.content) // 4)
            reading.token_source = "ESTIMATED"

        # TPS 只用「正文 token」，且分母必须是正文生成窗口。
        # 推理模型的 reasoning token 全在首个正文 token 之前产生，若用
        # usage.completion_tokens 作分子、首末正文 token 间隔作分母，
        # 会得到 40 万 TPS 这种荒谬值（实测 LightVela）。因此：
        #   分子 = 正文 token 估算（chars/4）
        #   分母 = 正文窗口
        #
        # 另外：实测 Packet / AMD 把响应整体缓冲后一次性下发，
        # 所有分片落在同一毫秒，窗口接近 0。此时 TPS 不可测，
        # 必须标为不可靠而不是给个假数字。
        content_tokens = max(1, len(reading.content) // 4) if reading.content else 0
        mode = reading.stream_mode()
        win_ms = None
        if reading.first_token_ms is not None and reading.last_token_ms is not None:
            win_ms = reading.last_token_ms - reading.first_token_ms
        elif reading.first_token_ms is not None:
            win_ms = total_ms - reading.first_token_ms

        tps = None
        tps_unreliable = False
        if content_tokens and win_ms is not None:
            if mode == "buffered" or win_ms < 200:
                tps_unreliable = True
            else:
                tps = round(content_tokens / (win_ms / 1000), 2)

        # 空正文但有 reasoning => 推理模型被 max_tokens 截断
        starved = (not reading.content) and bool(reading.reasoning)
        status = "HEALTHY"
        kind = ""
        if starved:
            status = "REASONING_STARVED"
            kind = "no_content_before_limit"
        elif not reading.content and not reading.reasoning:
            status = "RESPONSE_INVALID"
            kind = "empty_stream"

        result.update(
            status=status,
            http_status=resp.status,
            error_kind=kind or None,
            error_body=None,
            response_shape="sse",
            ttft_ms=round(reading.first_token_ms, 1) if reading.first_token_ms else None,
            output_tokens=out_tokens,
            token_source=reading.token_source if out_tokens else None,
            tps=tps,
            tps_unreliable=tps_unreliable,
            stream_mode=mode,
            served_model=reading.served_model,
            resp_headers=_slim_headers(resp),
            resp_body=_truncate(classify.redact(
                raw_head.decode("utf-8", "replace") if raw_head else "")),
            usage_json=json.dumps(reading.usage_raw, ensure_ascii=False)[:4000]
                       if reading.usage_raw else None,
            cost_usd=reading.cost_usd,
            reasoning_tokens=reading.reasoning_tokens,
            retry_after=None,
            total_ms=round(total_ms, 1),
            reasoning_starved=starved,
            reasoning_chars=len(reading.reasoning),
            content_chars=len(reading.content),
        )
    except Exception as e:
        st, kd = classify.classify_exception(e)
        result.update(
            status=st, error_kind=kd,
            error_body=classify.redact(str(e)),
            total_ms=round((time.perf_counter() - t0) * 1000, 1),
            response_shape="none",
            ttfb_ms=None, ttft_ms=None, output_tokens=None, tps=None,
            token_source=None,
        )
    return result


def _get_json(ep: dict, path: str, timeout: float = 20) -> tuple[int, dict | None, str]:
    """带鉴权的 GET，返回 (status, json, raw_text)。"""
    url = ep["base_url"].rstrip("/") + path
    proxy = _proxy_for(ep)
    adapter = get_adapter(ep["protocol"])
    hdrs = adapter.auth_headers(_token(ep["auth_env"]))
    hdrs.update({"Accept": "application/json", "User-Agent": "Tokriva-Probe/0.1"})
    try:
        conn, _ = open_timed(url, proxy, timeout=timeout, egress_label=_egress_label())
        conn.request("GET", request_path(url, proxy), headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        base["resp_headers"] = _slim_headers(resp)
        conn.close()
        text = raw.decode("utf-8", "replace")
        shape = classify.detect_shape(raw, resp.getheader("Content-Type", ""))
        if shape != "json":
            return resp.status, None, text
        return resp.status, json.loads(text), text
    except Exception as e:
        return 0, None, str(e)


def probe_utilization(ep: dict) -> list[dict]:
    """GET /models/utilization：各模型实时负载。

    AMD 独有。用它挑空闲模型，比在满载模型上硬等 429 划算得多
    （实测 DeepSeek-V4.1-Flash 96.9% 时直接返回
      model_concurrency_rate_limit_exceeded + Retry-After）。
    """
    st, data, raw = _get_json(ep, "/models/utilization")
    if st != 200 or not isinstance(data, dict):
        return []
    out = []
    for m in data.get("data", []) or []:
        if isinstance(m, dict) and m.get("id"):
            out.append({
                "model_id": m["id"],
                "utilization": m.get("utilization"),
                "state": m.get("state"),
                "sampled_at": m.get("sampled_at"),
            })
    return out


def probe_usage(ep: dict) -> dict | None:
    """GET /usage：账号级额度与限额。

    AMD 返回 rpm_limit / daily_cost_limit_usd / daily_cost_used_usd /
    daily_cost_remaining_usd / daily_reset_timezone。属设计文档里的 A 档
    「接口可查」，不需要手工录入。
    """
    st, data, raw = _get_json(ep, "/usage")
    if st != 200 or not isinstance(data, dict):
        return None
    return data


def pick_idle_model(ep: dict, candidates: list[str] | None = None) -> str | None:
    """按利用率挑最空闲的候选模型。"""
    utils = probe_utilization(ep)
    if not utils:
        return None
    rank = {"idle": 0, "busy": 1, "full": 2}
    pool = [u for u in utils if not candidates or u["model_id"] in candidates]
    if not pool:
        return None
    pool.sort(key=lambda u: (rank.get(u.get("state"), 3),
                             u.get("utilization") or 0))
    return pool[0]["model_id"]


def extract_header_quota(headers_json: str | None,
                         provider: str | None = None) -> list[dict]:
    """响应头额度（B 档）。映射规则见 hub/quota_rules.py，按服务商声明。

    实测 AMD 返回 x-ratelimit-*-user-daily-usd / -rpm，含 remaining 与 reset(epoch)。
    LightVela 与 Packet 不返回任何限流头，仍归 MANUAL。
    """
    from . import quota_rules
    return quota_rules.parse(headers_json, provider)
