"""状态引擎：三层诊断判定（连通 → 服务 → 速度）。

设计文档 docs/tokriva-overview-redesign.md 的 A3/A4 实现：
- 判定在服务端做一次（D8），总览/推荐/提醒消费同一份结果；
- "现在"的判定看最近记录，不受前端时间范围影响（B4）；
- 失败即停（A1）之后，连通失败轮次不再产生 L2 记录，
  下层自动视为"跳过"，不重复计数；
- 可用率口径：排除限流、排除跳过、缺少密钥不计入（A4）。

阈值说明：确认次数（连续失败 >= 2 次判故障）、过期阈值（75 分钟，
约等于 30 分钟探测周期的 2.5 倍）均为初始建议值，待真实数据校准。
"""
from __future__ import annotations

import os
import socket
import statistics
from datetime import datetime
from urllib.parse import urlparse

from .probe import DEFAULT_PROXY

RULES_VERSION = "2026-11-22.1"

# 连通层失败阶段 → 界面文案（设计文档 6.1.1）
STAGE_LABEL = {
    "DNS_FAIL": "域名解析失败",
    "PROXY_UNREACHABLE": "代理不可达",
    "TLS_FAIL": "握手失败",
    "TCP_FAIL": "连接失败",
    "TIMEOUT": "响应超时",
}

# 服务层失败状态 → 界面文案（设计文档 6.2.2）
L2_FAIL_LABEL = {
    "AUTH_FAILED": "鉴权失败",
    "WAF_BLOCKED": "被网关拦截",
    "QUOTA_EXHAUSTED": "额度耗尽",
    "MODEL_GONE": "模型不可用",
    "ENDPOINT_ERROR": "服务端/请求错误",
    "RESPONSE_INVALID": "响应异常",
    "REASONING_STARVED": "推理占满",
}

# 最终判定 → 界面文案 + 颜色档（设计文档 6.5）
FINAL_LABEL = {
    "NO_KEY": ("缺少密钥", "neutral"),
    "DOWN": ("网络不通", "danger"),
    "PENDING": ("连通待确认", "warn"),
    "AUTH_FAILED": ("鉴权失败", "danger"),
    "WAF_BLOCKED": ("被网关拦截", "danger"),
    "QUOTA_EXHAUSTED": ("额度耗尽", "danger"),
    "MODEL_GONE": ("模型不可用", "danger"),
    "ENDPOINT_ERROR": ("服务端错误", "danger"),
    "RESPONSE_INVALID": ("响应异常", "danger"),
    "REASONING_STARVED": ("响应异常", "danger"),
    "LIMITED": ("受限", "warn"),
    "OK": ("可用", "ok"),
}

CONFIRM_FAILS = 2   # 连续失败 N 次才确认故障（待校准）
# 推荐资格门槛（初始建议值，待数据校准）：90% 在限流频繁的场景下几乎永远无达标者
# （实测：近 1 小时 83.7% 但矩阵判定"可用"），校准为 80%——仍能拦住 70% 级别的不稳定接口
REC_AVAIL_MIN = 80
REC_STREAM_MIN = 5  # 推荐资格：流式有效样本下限（待校准）


def _parse_local(s: str) -> datetime | None:
    """解析库中的时间戳（本地朴素串）为 naive datetime。"""
    if not s:
        return None
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _age_sec(rec: dict, now: datetime) -> float:
    t = _parse_local(rec["created_at"])
    return (now - t).total_seconds() if t else 1e9


def _is_l1_fail(rec: dict) -> bool:
    """连通层失败：网络错误，或异常路径的超时（无 HTTP 状态码）。"""
    if rec["status"] == "NETWORK_ERROR":
        return True
    return rec["status"] == "ENDPOINT_ERROR" and rec["error_kind"] == "timeout" \
        and rec["http_status"] is None


def _fail_stage(rec: dict) -> str:
    kind = rec["error_kind"] or ""
    body = (rec["error_body"] or "").lower()
    if kind == "dns":
        return "DNS_FAIL"
    if kind == "tls":
        return "TLS_FAIL"
    if kind == "tcp":
        return "TCP_FAIL"
    if kind == "timeout":
        return "TIMEOUT"
    if "proxy" in body or "over proxy" in body:
        return "PROXY_UNREACHABLE"
    if rec["proxied"]:
        return "PROXY_CONNECT_FAIL"
    return "TLS_FAIL" if "ssl" in body else "TCP_FAIL"


def _p50(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals)) if vals else None


def _judge_l1(l0s: list[dict], now: datetime) -> dict:
    """连通层判定：从最新记录向前数连续失败。"""
    if not l0s:
        return {"state": "unknown", "fail_stage": None, "since": None, "consec": 0}
    newest = l0s[0]
    if newest["status"] == "NOT_CONFIGURED":
        return {"state": "no_key", "fail_stage": None, "since": None, "consec": 0}
    run = 0
    for r in l0s:
        if _is_l1_fail(r):
            run += 1
        else:
            break
    if run == 0:
        return {"state": "ok", "fail_stage": None, "since": None, "consec": 0}
    stage = _fail_stage(l0s[0])
    since = l0s[min(run, len(l0s)) - 1]["created_at"] if run >= 1 else None
    state = "down" if run >= CONFIRM_FAILS else "pending"
    return {"state": state, "fail_stage": stage, "since": since, "consec": run}


def _judge_l2(l2s: list[dict], window_start: str) -> dict:
    """服务层：窗口内可用率（排除限流），加最近一次的服务结论。"""
    win = [r for r in l2s if r["created_at"] >= window_start]
    attempted = [r for r in win if r["status"] != "NOT_CONFIGURED"]
    limited = [r for r in attempted if r["status"] == "RATE_LIMITED"]
    healthy = [r for r in attempted if r["status"] == "HEALTHY"]
    denom = len(attempted) - len(limited)
    avail = round(len(healthy) / denom * 100, 1) if denom else None
    newest = l2s[0] if l2s else None
    if newest is None:
        state = "unknown"
    elif newest["status"] == "HEALTHY":
        state = "ok"
    elif newest["status"] == "RATE_LIMITED":
        state = "limited"
    else:
        state = newest["status"]
    # 近 1 小时的可用率单独给（推荐资格用，窗口更短、更接近"现在"）
    win1 = [r for r in l2s if r["created_at"] >= "-1 hours"]
    att1 = [r for r in win1 if r["status"] not in ("NOT_CONFIGURED",)]
    lim1 = [r for r in att1 if r["status"] == "RATE_LIMITED"]
    hea1 = [r for r in att1 if r["status"] == "HEALTHY"]
    den1 = len(att1) - len(lim1)
    return {
        "state": state,
        "fail_label": L2_FAIL_LABEL.get(newest["status"]) if newest else None,
        "availability": avail,
        "healthy": len(healthy), "limited": len(limited),
        "denom": denom, "window_total": len(win),
        "avail_1h": round(len(hea1) / den1 * 100, 1) if den1 else None,
        "denom_1h": den1, "healthy_1h": len(hea1),
    }


def _judge_l3(l1_state: str, l2s: list[dict]) -> dict:
    """速度层：只在连通与服务可用时给出；整包只报总耗时，不报首字。"""
    healthy = [r for r in l2s if r["status"] == "HEALTHY"]
    if l1_state == "no_key":
        return {"state": "skipped", "reason": "NOT_CONFIGURED"}
    if l1_state != "ok":
        return {"state": "skipped", "reason": "L1_FAILED" if l1_state != "unknown" else "NO_DATA"}
    if not healthy:
        return {"state": "skipped", "reason": "L2_FAILED" if l2s else "NO_DATA"}
    streams = [r for r in healthy if r["stream_mode"] == "true_stream" and r["ttft_ms"]]
    buffered = [r for r in healthy if r["stream_mode"] == "buffered" and r["total_ms"]]
    if streams:
        return {"state": "ok", "mode": "stream",
                "ttft_p50": _p50([r["ttft_ms"] for r in streams]),
                "tps_p50": _p50([r["tps"] for r in streams]),
                "samples": len(streams)}
    if buffered:
        return {"state": "ok", "mode": "whole",
                "total_p50": _p50([r["total_ms"] for r in buffered]),
                "samples": len(buffered)}
    return {"state": "skipped", "reason": "NO_DATA"}


def _final_verdict(has_key: bool, active: bool, l1: dict, l2: dict) -> tuple[str, list]:
    """最终判定：取最靠近根因的一层（设计文档 6.5 优先级）。"""
    overlays = []
    if not active:
        return "DISABLED", overlays
    if not has_key or l1["state"] == "no_key":
        return "NO_KEY", overlays
    if l1["state"] == "down":
        return "DOWN", overlays
    if l1["state"] == "pending":
        return "PENDING", overlays
    if l2["state"] in L2_FAIL_LABEL:
        return l2["state"], overlays
    if l2["state"] == "limited":
        return "LIMITED", overlays
    return "OK", overlays


def _era_cutoff(l2s_desc: list[dict], l0s_desc: list[dict]) -> str | None:
    """接口"首次真正能用"的时刻：第一条健康的 L2 记录；没有则退回第一条健康的 L0。

    未接入期间的失败记录没有统计含义（没配密钥时网络/鉴权本来就不通），
    可用率与心跳只统计这个时刻之后的记录，否则一次接入就会稀释出 19% 这种假可用率。
    """
    for lst in (l2s_desc, l0s_desc):
        chrono = list(reversed(lst))
        for r in chrono:
            if r["status"] == "HEALTHY":
                return r["created_at"]
    return None


def build_overview(conn, window_hours: int = 24, stale_min: int = 75) -> dict:
    """产出总览需要的全部判定结果。只读，不写库。"""
    now = datetime.now()
    win_cut = f"-{int(window_hours)} hours"
    eps = conn.execute(
        "SELECT e.*, p.name AS provider FROM endpoint e "
        "LEFT JOIN provider p ON p.id = e.provider_id "
        "WHERE e.active = 1 ORDER BY e.id"
    ).fetchall()

    matrix, counts = [], {"ok": 0, "pending": 0, "limited": 0,
                          "down": 0, "no_key": 0, "disabled": 0}
    issues: list[str] = []
    fail_events: list[dict] = []  # 共因归因素材：当前连通失败的接口
    last_cycle_at = None

    for ep in eps:
        ep = dict(ep)
        l0s = [dict(r) for r in conn.execute(
            "SELECT * FROM probe_result WHERE endpoint_id=? AND level='L0' "
            "ORDER BY id DESC LIMIT 40", (ep["id"],))]
        l2s = [dict(r) for r in conn.execute(
            "SELECT * FROM probe_result WHERE endpoint_id=? AND level='L2' "
            "ORDER BY id DESC LIMIT 60", (ep["id"],))]

        has_key = bool(os.environ.get(ep["auth_env"] or ""))
        l1 = _judge_l1(l0s, now)
        # 没配密钥的接口谈不上"连通失败"：探测到不了鉴权环节，TLS/网络失败只是
        # 探测顺序的副产物。判定与展示统一按"未探测（缺少密钥）"，不参与共因归因
        if not has_key:
            l1 = {"state": "no_key", "fail_stage": None, "since": None, "consec": 0}
        # 统计口径（用户视角）：未接入期间的失败没有含义，可用率/心跳只统计
        # 首次"真正能用"之后的记录。判定（最终结论）仍看最新记录，不受影响。
        cutoff = _era_cutoff(l2s, l0s)
        l2_eff = [r for r in l2s if not cutoff or r["created_at"] >= cutoff]
        l0_eff = [r for r in l0s if not cutoff or r["created_at"] >= cutoff]
        l2 = _judge_l2(l2_eff, win_cut)
        if not has_key:
            # 未接入时历史记录（如密钥配置前的失败）不代表"现在"，服务层同样按缺少密钥展示
            l2["state"] = "NOT_CONFIGURED"
            l2["fail_label"] = None
        l3 = _judge_l3(l1["state"] if l1["state"] in ("ok", "down", "pending") else "no_key", l2s)
        final, overlays = _final_verdict(has_key, bool(ep["active"]), l1, l2)

        # 连通层被跳过时，服务层结论不可信：连通失败轮次根本没有 L2 记录（A1 失败即停）
        l1_skips_l2 = l1["state"] in ("down", "pending")
        all_recs_age = min([_age_sec(r, now) for r in (l0s[:1] + l2s[:1])] or [1e9])
        if all_recs_age > stale_min * 60 and final != "NO_KEY":
            overlays.append("STALE")

        # 推荐资格（B5，设计文档 8.4.1）：当前连通 + 近 1 小时可用率达标 +
        # 流式样本足够 + 数据新鲜。缺什么如实记录，前端"无达标者"时展示原因。
        rec_missing: list[str] = []
        if not has_key:
            rec_missing.append("缺少密钥")
        elif l1["state"] != "ok":
            rec_missing.append("当前连通未通过")
        elif l2["state"] not in ("ok", "limited"):
            rec_missing.append(f"服务层异常（{L2_FAIL_LABEL.get(l2['state'], l2['state'])}）")
        else:
            if l2["avail_1h"] is None or l2["denom_1h"] < 3:
                rec_missing.append("近 1 小时样本不足")
            elif l2["avail_1h"] < REC_AVAIL_MIN:
                rec_missing.append(f"近 1 小时可用率 {l2['avail_1h']}%（门槛 {REC_AVAIL_MIN}%）")
            streams_n = len([r for r in l2s if r["status"] == "HEALTHY"
                             and r["stream_mode"] == "true_stream" and r["ttft_ms"]])
            if streams_n < REC_STREAM_MIN:
                rec_missing.append(f"流式有效样本 {streams_n}/{REC_STREAM_MIN}")
            if "STALE" in overlays:
                rec_missing.append("数据过期")

        if l1["state"] in ("down", "pending") and has_key:
            fail_events.append({
                "endpoint": ep["label"], "provider": ep["provider"],
                "proxied": bool(ep["use_proxy"]), "at": l0s[0]["created_at"] if l0s else None,
                "stage": l1["fail_stage"],
            })

        if final == "OK":
            counts["ok"] += 1
        elif final.lower() in counts:
            counts[final.lower()] += 1

        heartbeat = [{"status": r["status"], "at": r["created_at"]}
                     for r in list(reversed(l0_eff[:30]))]

        # 模型行：窗口内实测过的模型，各自最新结论与速度
        models = []
        seen: set[str] = set()
        for r in l2s:
            if not r["model_id"] or r["model_id"] in seen:
                continue
            seen.add(r["model_id"])
            mine = [x for x in l2s if x["model_id"] == r["model_id"]]
            m_health = [x for x in mine if x["status"] == "HEALTHY"]
            m_stream = [x for x in m_health if x["stream_mode"] == "true_stream" and x["ttft_ms"]]
            m_buf = [x for x in m_health if x["stream_mode"] == "buffered" and x["total_ms"]]
            models.append({
                "model": r["model_id"],
                "status": r["status"],
                "samples": len(mine),
                "ttft_p50": _p50([x["ttft_ms"] for x in m_stream]),
                "total_p50": _p50([x["total_ms"] for x in m_buf]),
                "stream_mode": r["stream_mode"],
            })

        matrix.append({
            "endpoint": ep["label"],
            "provider": ep["provider"],
            "path": "proxy" if ep["use_proxy"] else "direct",
            "hasKey": has_key,
            "layers": {
                "l1": {**l1, "heartbeat": heartbeat,
                       "age_sec": round(_age_sec(l0s[0], now)) if l0s else None},
                "l2": {**l2, "skipped": l1_skips_l2},
                "l3": l3,
            },
            "final": final,
            "final_label": FINAL_LABEL[final][0],
            "severity": FINAL_LABEL[final][1],
            "overlays": overlays,
            "age_sec": round(all_recs_age),
            "models": models,
            "recommendation": {"eligible": not rec_missing and final == "OK",
                               "missing": rec_missing},
        })

        if l0s and (last_cycle_at is None or l0s[0]["created_at"] > last_cycle_at):
            last_cycle_at = l0s[0]["created_at"]

        # 一句话结论的素材：只挑需要行动的问题
        if final in ("DOWN", "PENDING"):
            stage_txt = STAGE_LABEL.get(l1["fail_stage"], "网络异常")
            dur = ""
            if l1["since"]:
                t = _parse_local(l1["since"])
                if t:
                    dur = f"，已持续 {max(1, round((now - t).total_seconds() / 60))} 分钟"
            issues.append((0 if final == "DOWN" else 1,
                           f"{ep['label']} 网络不通（{stage_txt}{dur}）"
                           if final == "DOWN" else f"{ep['label']} 连通异常待确认（{stage_txt}）"))
        elif final in L2_FAIL_LABEL:
            issues.append((2, f"{ep['label']} {L2_FAIL_LABEL[final]}"))
        elif final == "LIMITED":
            issues.append((3, f"{ep['label']} 受限"))
        elif final == "NO_KEY":
            issues.append((4, f"{ep['label']} 缺少密钥"))

    issues.sort(key=lambda x: x[0])
    n_total = len(matrix)
    n_ok = counts["ok"]
    if not issues:
        text = f"{n_total} 个接口全部可用" if n_total else "暂无接口"
    elif n_ok == 0:
        text = f"{n_total} 个接口中 0 个可用；" + "；".join(t for _, t in issues[:3])
    else:
        text = f"{n_total} 个接口中 {n_ok} 个可用；" + "；".join(t for _, t in issues[:3])
        if len(issues) > 3:
            text += f" 等 {len(issues)} 个问题"

    matrix.sort(key=lambda m: (
        {"DOWN": 0, "PENDING": 1, "AUTH_FAILED": 2, "WAF_BLOCKED": 2,
         "QUOTA_EXHAUSTED": 2, "MODEL_GONE": 2, "ENDPOINT_ERROR": 3,
         "RESPONSE_INVALID": 3, "REASONING_STARVED": 3, "LIMITED": 4,
         "OK": 5, "NO_KEY": 6, "DISABLED": 7}.get(m["final"], 8),
        m["age_sec"] or 0,
    ))

    # ---- 共因归因（D1/D2，设计文档 6.4）----
    # cycle_id 尚未落库，用"失败时间彼此贴近（≤3 分钟）"近似同一轮探测。
    def _proxy_selfcheck() -> bool | None:
        """代理端口可达性自检：能区分'代理未启动'与'代理节点故障'。"""
        try:
            u = urlparse(DEFAULT_PROXY)
            host = u.hostname or "127.0.0.1"
            port = u.port or 8080
            with socket.create_connection((host, port), timeout=1.5):
                return True
        except OSError:
            return False
        except Exception:
            return None  # 自检失败但不是端口问题，不给结论

    common_cause = None
    proxy_fails = [e for e in fail_events if e["proxied"]]
    if proxy_fails:
        # 交叉验证素材：直连接口当前是否正常（同窗口内 l1=ok 即视为直连可用）
        direct_ok = bool([m for m in matrix
                          if m["path"] == "direct" and m["layers"]["l1"]["state"] == "ok"])
        sc = _proxy_selfcheck()
        if sc is False:
            u = urlparse(DEFAULT_PROXY)
            common_cause = {"level": "high",
                            "text": f"代理未启动（{u.hostname}:{u.port} 端口无响应）",
                            "detail": "走代理的接口全部不可用属正常现象，启动代理后恢复"}
        elif len(proxy_fails) >= 2 and direct_ok:
            names = "、".join(e["endpoint"] for e in proxy_fails)
            providers = {e["provider"] for e in proxy_fails if e["provider"]}
            if len(providers) == 1:
                common_cause = {"level": "medium",
                                "text": f"{providers.pop()} 的多个接口经代理同时失败",
                                "detail": f"失败接口：{names}；同窗口直连接口正常。优先怀疑该服务商而非代理"}
            else:
                common_cause = {"level": "high",
                                "text": f"代理链路疑似故障",
                                "detail": f"{names} 同时连通失败，而直连接口正常；代理自检{'通过，疑似代理节点故障' if sc else ''}"}
        elif len(proxy_fails) == 1 and direct_ok:
            e = proxy_fails[0]
            common_cause = {"level": "low",
                            "text": f"{e['endpoint']} 连通失败，无法区分是接口自身还是代理问题",
                            "detail": "该代理下只有 1 个接口，无法交叉验证；可临时改直连验证"}
        elif sc and proxy_fails and not direct_ok:
            common_cause = {"level": "medium",
                            "text": "代理与直连接口均失败，疑似本机网络问题",
                            "detail": "代理端口可达但所有出口探测失败"}

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "rules_version": RULES_VERSION,
        "probe": {"last_cycle_at": last_cycle_at, "runner": "scheduler"},
        "window_hours": window_hours,
        "headline": {"text": text, "counts": counts},
        "common_cause": common_cause,
        "matrix": matrix,
    }
