"""公开快照构建器（Public Snapshot Builder）。

与 `hub/static_export.py` 的区别
--------------------------------
`static_export.py` 把**本机管理控制台**的只读接口整包烤成 `snapshot.js`，
目标是"本机脱网也能看控制台"——**不等于"允许公开全部响应"**。

本模块服务的是 **GitHub Pages 公开站**：只产出经过**正向白名单**裁剪的独立数据契约
`snapshot.json`（见 `docs/public-data-policy.md`），并自带敏感字段扫描。
二者产物不同、用途不同，互不替代。

设计要点
--------
- **正向白名单**：逐字段挑选，而不是对管理接口结果做减法。上游新增字段默认不公开。
- **扁平契约**：顶层直接是 `schemaVersion` / `meta` / `overview` / `rankings` /
  `endpoints` / `probes` / `catalog`，**不套 `data.*`**。理由：公开站走独立前端
  （`web/public/`），不与管理端 `app.js` 共用取数路径，所以「嵌套成 `window.SNAPSHOT`
  的形状以便复用」这个前提不成立；`readOnly` / `mode` 那些是**管理端只读快照**的概念，
  公开契约天然只读，不该带。

  ⚠️ **有意偏离架构报告的 `providers` 命名**：报告建议顶层键叫 `providers`，但那是
  它按「一个 provider 一个 base_url」设想的名字。本库的 `provider` 表是**厂商元数据**
  （13 家，其中 8 家只有 catalog 条目、没有任何被探测端点），真正被探测的对象是
  `endpoint`（5 个）。若把 `endpoints` 改名 `providers`，会与按厂商聚合的 `catalog`
  重叠、且与库内语义相悖。故**保留 `endpoints`**；报告 `PublicProvider` DTO 里
  真正需要的公开状态，已并入 `endpoints` 各项（`providerId` slug / `publicStatus`）。
- **自述字段**：`meta` 里带 `snapshotId` / `measurementScope` / `freshness` /
  `samplePolicy`，让访客一眼知道「这是哪一次快照、测的是一条出口、
  数据新不新、探测列表是被截断的样本」。
- **纯读取、无副作用**：绝不调用 `/api/reminders`（会触发 `_notify_transitions()`），
  不写库、不触发探测。所有查询在**传入的连接**上完成（便于对一致性备份副本取数）。
- **单一来源**：榜单复用 `hub.rank.board()`，总览复用 `hub.status_engine.build_overview()`，
  与页面/网关口径完全一致。
- **全历史与窗口分开**：`overview.allTime` 是库级全量聚合，**不得**用 720 小时冒充。
- **原子落盘**：先写临时文件再 `replace`，避免半个 JSON。

用法
----
    python -m hub.public_snapshot                    # → dist/public/snapshot.json
    python -m hub.public_snapshot --out dist/public
    python -m hub.public_snapshot --probes 500
    python -m hub.public_snapshot --window 1,6,24,168,720
    python -m hub.public_snapshot --scan-only        # 只跑敏感扫描，不落盘
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import statistics
import sys
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import quota_rules as _qr

ROOT = Path(__file__).resolve().parent.parent
# 与只读站产物（static_export 的 dist/ 根）分开，避免两者在 dist/ 里混住
DEFAULT_OUT = ROOT / "dist" / "public"
DEFAULT_DB = ROOT / "data" / "hub.db"

SCHEMA_VERSION = 1
POLICY_VERSION = 1
DEFAULT_PROBES = 500
WINDOWS = (1, 6, 24, 168, 720)   # 近 1h / 6h / 24h / 7d / 30d
SCENES = ("speed", "agent", "stable")

# 数据新鲜度阈值（分钟），见政策 §2.9 / 报告 §16.2
FRESH_MIN, STALE_MIN = 90, 180
# 公开的测量范围声明：只代表**一条固定出口**的体验，不代表全球网络
MEASUREMENT_SCOPE = "single-origin-network"

# provider 内部名 -> 公开别名（留空即原名公开；需要匿名化时在此登记）
PROVIDER_ALIAS: dict[str, str] = {}


def public_provider(name: str | None) -> str | None:
    """服务商公开名。默认与内部名一致，按需在 PROVIDER_ALIAS 登记别名。"""
    if name is None:
        return None
    return PROVIDER_ALIAS.get(name, name)


def public_id(name: str | None) -> str | None:
    """稳定公开 ID（slug）：由公开名派生，前端可作锚点 / DOM id / 路由片段。

    **不用库内主键**——主键是内部信息，且重建库时会变。slug 由名字派生，
    只要名字不变就稳定；名字变了本就该变。
    """
    if not name:
        return None
    slug = re.sub(r"[^0-9a-z]+", "-", name.strip().lower()).strip("-")
    return slug or None


def _n(v: Any, nd: int = 3) -> Any:
    return None if v is None else round(float(v), nd)


# ---------------------------------------------------------------------------
# 自述字段：新鲜度 / 快照指纹 / 本地时区偏移
# ---------------------------------------------------------------------------
def freshness_of(data_as_of: str | None, now: datetime | None = None) -> str:
    """按「数据时点」与「当前时间」的差给一个公开状态：fresh / stale / expired。

    阈值见模块常量 `FRESH_MIN` / `STALE_MIN`（90 / 180 分钟）。时区口径与全栈一致：
    两者都是本机本地时间（库内默认值即 `datetime('now','localtime')`），故直接相减。
    时间戳缺失或解析失败返回 `unknown`——**不猜**，让访客自己看 `dataAsOf`。
    """
    if not data_as_of:
        return "unknown"
    try:
        t = datetime.fromisoformat(str(data_as_of))
    except ValueError:
        return "unknown"
    if t.tzinfo is None:
        base = now or datetime.now()          # 库内时间戳无时区，按本地时间直减
    else:
        base = now or datetime.now().astimezone()
    age_min = (base - t).total_seconds() / 60
    if age_min <= FRESH_MIN:      # 含未来时间戳（负数）——宁可说 fresh 也不谎报过期
        return "fresh"
    if age_min <= STALE_MIN:
        return "stale"
    return "expired"


def snapshot_digest(payload: Any) -> str:
    """数据体指纹（sha256 前 12 位十六进制）。

    只覆盖**数据**（overview / rankings / endpoints / probes / catalog），
    不含 `generatedAt` / `freshness` 这类每次都变的元信息。
    因此「库没变 → 指纹不变」，发布器可据此实现「无变化不提交」。
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _local_offset(now: datetime | None = None) -> str:
    """本机时区偏移，形如 `+0800`。本栈固定用运行机器的本地时区。"""
    return (now or datetime.now()).astimezone().strftime("%z")


def _stamp(text: str | None) -> str | None:
    """`2026-11-24 12:00:00` → `20261124T120000`（snapshotId 的时间段）。"""
    if not text:
        return None
    digits = re.sub(r"[^0-9]", "", str(text))
    if len(digits) < 14:
        return None
    return f"{digits[:8]}T{digits[8:14]}"


# ---------------------------------------------------------------------------
# 敏感字段扫描（双保险：结构黑名单 + 内容 canary）
# ---------------------------------------------------------------------------
# 结构黑名单：产物里出现这些键名即判定构建失败（白名单之外的兜底）。
# 注意**不含 `note`**：榜单里的 `note` 是"排名的公开方法说明"，属要公开的内容；
# 端点台账的 `note` 则由 `_endpoints` 的正向投影排除（并有对应用例守卫），
# 用 `note` 做全局键名拦截会误伤。同理不含 `model`/`source` 等语义中性的键名。
FORBIDDEN_KEYS = frozenset({
    "authEnv", "baseUrl", "error_body", "resp_body", "request_json",
    "resp_headers", "egress", "settings", "schedule", "config", "reminders",
    "db", "dailyBudget", "hasKey", "probePrompt",
})

# 内容 canary：对整个 JSON 文本匹配。命中即失败。
CANARY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("env_key_name", re.compile(r"\b[A-Z][A-Z0-9_]*_(?:KEY|TOKEN|SECRET)\b")),
    ("key_prefix", re.compile(r"sk-[A-Za-z0-9]{8,}")),
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._-]{8,}")),
    ("authorization", re.compile(r"Authorization", re.IGNORECASE)),
    ("win_path", re.compile(r"[A-Za-z]:\\")),
    ("posix_home", re.compile(r"/(?:Users|home)/")),
    ("loopback", re.compile(r"\b(?:127\.0\.0\.1|localhost|0\.0\.0\.0)\b")),
    ("private_ip", re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")),
    ("long_secret", re.compile(r"\b[A-Za-z0-9+/]{44,}={0,2}\b")),
)

# canary 显式豁免：这些是大写状态常量，形如环境变量名但**是要公开的状态结论**。
# 例：overview 的 final="NO_KEY"（缺少密钥）；不豁免会被 env_key_name 规则误杀。
CANARY_ALLOW = frozenset({"NO_KEY", "HAS_KEY"})


def _walk_keys(obj: Any, path: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path + "." + k, k
            yield from _walk_keys(v, path + "." + k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_keys(v, f"{path}[{i}]")


# 「现在」派生的易变键名：`build_overview` 会在多层塞入 `generated_at`，
# 并在端点行/模型行塞入 `age_sec`。它们每次构建都不同，会破坏产物幂等
# （报告 §7.3 第 8 条），进而让「无变化不提交」永远判定为「有变化」。
# 公开页要「多久以前」，用 `meta.dataAsOf` / `endpoints[].lastProbeAt` 在前端推算即可。
VOLATILE_KEYS = frozenset({"generated_at", "age_sec"})


def drop_volatile(obj: Any) -> Any:
    """递归剥掉「现在」派生字段（见 `VOLATILE_KEYS`）。原地改并返回。"""
    if isinstance(obj, dict):
        for k in list(obj):
            if k in VOLATILE_KEYS:
                del obj[k]
            else:
                drop_volatile(obj[k])
    elif isinstance(obj, list):
        for v in obj:
            drop_volatile(v)
    return obj


def scan_payload(payload: Any) -> list[dict[str, str]]:
    """扫描公开产物，返回发现清单（空 = 通过）。

    两层：① 结构——递归找禁用键名；② 内容——对 JSON 文本跑 canary 正则。
    """
    findings: list[dict[str, str]] = []
    for path, key in _walk_keys(payload):
        if key in FORBIDDEN_KEYS:
            findings.append({"rule": "forbidden_key", "where": path, "match": key})

    text = json.dumps(payload, ensure_ascii=False)
    for name, pat in CANARY_PATTERNS:
        seen: set[str] = set()
        for m in pat.finditer(text):
            frag = m.group(0)[:48]
            if frag in seen or frag.strip() in CANARY_ALLOW:
                continue
            seen.add(frag)
            findings.append({"rule": name, "where": "content", "match": frag})
    return findings


# ---------------------------------------------------------------------------
# 各接口的白名单投影
# ---------------------------------------------------------------------------
def _probe_row(r: dict) -> dict:
    """探测明细的正向白名单投影（排除正文 / 请求体 / 响应头 / 出口路径）。

    命名约定（见 `docs/public-data-policy.md` §2.0）：**照抄库中列名**（snake_case）。
    探测行是最贴近原始数据的部分，保留 snake_case 可让三者同名——
    库列、CSV 兜底快照 `data.js`、前端 `normalizeRows`——从而少一层适配。
    派生 / 聚合字段（`endpoints`、`rankings`）才用 camelCase。
    """
    return {
        "created_at": r["created_at"],
        "endpoint": r["endpoint"],
        "level": r["level"],
        "model_id": r["model_id"],
        "model_display": r.get("model_display") or r["model_id"],
        "status": r["status"],
        "http_status": r["http_status"],
        "dns_ms": _n(r["dns_ms"]),
        "tcp_ms": _n(r["tcp_ms"]),
        "tls_ms": _n(r["tls_ms"]),
        "ttfb_ms": _n(r["ttfb_ms"]),
        "ttft_ms": _n(r["ttft_ms"]),
        "output_tokens": r["output_tokens"],
        "tps": _n(r["tps"], 2),
        "total_ms": _n(r["total_ms"]),
        "stream_mode": r["stream_mode"],
        "served_model": r["served_model"],
        "token_source": r["token_source"],
        "error_kind": r["error_kind"],         # 标准化分类，非响应原文
        "response_shape": r["response_shape"],
        "reasoning_tokens": r["reasoning_tokens"],
        "retry_after": r["retry_after"],
        "cost_usd": r["cost_usd"],
    }


def _probes(conn, limit: int) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT p.created_at, p.level, p.model_id, p.status, p.http_status, "
        "       p.dns_ms, p.tcp_ms, p.tls_ms, p.ttfb_ms, p.ttft_ms, p.output_tokens, "
        "       p.tps, p.total_ms, p.stream_mode, p.served_model, p.token_source, "
        "       p.error_kind, p.response_shape, p.reasoning_tokens, p.retry_after, p.cost_usd, "
        "       e.label endpoint, m.display_name model_display "
        "FROM probe_result p "
        "JOIN endpoint e ON e.id=p.endpoint_id "
        "LEFT JOIN model m ON m.endpoint_id=p.endpoint_id AND m.model_id=p.model_id "
        "ORDER BY p.id DESC LIMIT ?", (limit,))]
    total = conn.execute("SELECT COUNT(*) FROM probe_result").fetchone()[0]
    return {"total": total, "items": [_probe_row(r) for r in rows], "limit": limit}


def _endpoints(conn) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT e.id, e.label, e.protocol, e.default_model, e.active, "
        "       p.name provider, p.website, p.kind "
        "FROM endpoint e LEFT JOIN provider p ON p.id=e.provider_id "
        "ORDER BY e.id")]
    out: list[dict] = []
    for r in rows:
        stats = conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN status='HEALTHY' THEN 1 ELSE 0 END) healthy, "
            "       MAX(created_at) last_at FROM probe_result WHERE endpoint_id=?",
            (r["id"],)).fetchone()
        l2 = [x["ttft_ms"] for x in conn.execute(
            "SELECT ttft_ms FROM probe_result WHERE endpoint_id=? AND level='L2' "
            "AND status='HEALTHY' AND stream_mode='true_stream' AND ttft_ms IS NOT NULL",
            (r["id"],))]
        tps = [x["tps"] for x in conn.execute(
            "SELECT tps FROM probe_result WHERE endpoint_id=? AND level='L2' "
            "AND status='HEALTHY' AND tps IS NOT NULL", (r["id"],))]
        ctx = conn.execute(
            "SELECT MAX(context_length) ctx, COUNT(*) n FROM model "
            "WHERE endpoint_id=? AND status='ACTIVE' AND COALESCE(is_chat,1)=1",
            (r["id"],)).fetchone()
        dm = conn.execute("SELECT display_name FROM model WHERE endpoint_id=? AND model_id=?",
                          (r["id"], r["default_model"])).fetchone()
        qrow = conn.execute(
            f"SELECT source FROM quota_snapshot WHERE endpoint_id=? "
            f"AND {_qr.DISPLAY_ROW_SQL} ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        out.append({
            # 公开稳定 ID：由 label 派生的 slug，供前端做 DOM id / 锚点 / 路由。
            # **不用库内主键**（内部信息，重建库时会变）。见 `public_id()`。
            "providerId": public_id(r["label"]),
            "label": r["label"],
            "provider": public_provider(r["provider"]),
            "website": r["website"],
            "kind": r["kind"],
            "protocol": r["protocol"],
            "defaultModel": (dm["display_name"] if dm and dm["display_name"] else r["default_model"]),
            "active": bool(r["active"]),
            # publicStatus：只由 `active` 派生（ACTIVE / PAUSED），是**可核实的内部事实**。
            # 不用 provider.provider_status —— 实测取值恒为 'UNKNOWN'（零信息量）。
            "publicStatus": "ACTIVE" if r["active"] else "PAUSED",
            "availability": round(stats["healthy"] / stats["n"] * 100, 1) if stats["n"] else 0.0,
            "probeCount": stats["n"],
            "lastProbeAt": stats["last_at"],
            "ttftP50": round(statistics.median(l2)) if l2 else None,
            "tpsAvg": round(statistics.mean(tps), 1) if tps else None,
            "contextMax": ctx["ctx"] if ctx else None,
            "modelCount": ctx["n"] if ctx else 0,
            "quotaSource": (qrow["source"] or "MANUAL_REQUIRED") if qrow else "MANUAL_REQUIRED",
        })
    return {"items": out}


def _catalog(conn) -> dict:
    from .api import OR_PREFIX   # 服务商聚合前缀映射（OpenRouter 等）

    rows = [dict(r) for r in conn.execute(
        "SELECT p.id provider_id, p.name provider, p.website, p.kind, "
        "       c.signup_method, c.free_form, c.rate_limits, c.allow_proxy, "
        "       c.allow_commercial, c.source_url, c.source_type, c.verified_at, "
        "       c.confidence, c.requires_phone, c.requires_card, c.requires_realname, "
        "       c.region_restricted, c.requires_review "
        "FROM catalog_entry c JOIN provider p ON p.id=c.provider_id "
        "ORDER BY p.name")]
    for r in rows:
        probed, n_models = False, 0
        for eid in [x["id"] for x in conn.execute(
                "SELECT id FROM endpoint WHERE provider_id=?", (r["provider_id"],))]:
            n = conn.execute("SELECT COUNT(*) FROM probe_result WHERE endpoint_id=?",
                             (eid,)).fetchone()[0]
            if n:
                probed = True
                n_models += conn.execute(
                    "SELECT COUNT(DISTINCT model_id) FROM probe_result "
                    "WHERE endpoint_id=? AND model_id IS NOT NULL", (eid,)).fetchone()[0]
        prefix = OR_PREFIX.get(r["provider"])
        if prefix:
            row = conn.execute(
                "SELECT COUNT(DISTINCT m.model_id) n FROM model m "
                "JOIN endpoint e ON e.id=m.endpoint_id "
                "WHERE e.label='OpenRouter' AND m.model_id LIKE ?", (prefix + "%",)).fetchone()
            if row and row["n"]:
                probed, n_models = True, row["n"]
        r["probed"], r["probed_models"] = probed, n_models
        r["provider"] = public_provider(r["provider"])

    # 只保留白名单字段，并丢掉内部主键 provider_id
    keep = ("provider", "website", "kind", "signup_method", "free_form", "rate_limits",
            "allow_proxy", "allow_commercial", "source_url", "source_type", "verified_at",
            "confidence", "requires_phone", "requires_card", "requires_realname",
            "region_restricted", "requires_review", "probed", "probed_models")
    return {"items": [{k: r.get(k) for k in keep} for r in rows]}


def _overview(conn, hours: int) -> dict:
    """状态总览。做两件事：剥掉 `hasKey`，以及剥掉所有「现在」派生的易变字段。

    ① `hasKey` 暴露「本机是否为某接口配置了凭据」，属运维内部信息
       （页面上仍能从 `final=NO_KEY` 得到「未接入」结论）。
    ② 所有「现在」派生的易变字段（`generated_at` / `age_sec`，见 `drop_volatile`）
       会让「库没变 → 产物不变」失效（报告 §7.3 第 8 条要求构建幂等）。
       公开页要「最后探测多久以前」，可用 `meta.dataAsOf` 与
       `endpoints.items[].lastProbeAt` 在前端自行推算，不必由后端固化一个会过期的秒数。
    """
    from .status_engine import build_overview
    ov = build_overview(conn, window_hours=hours)
    for m in ov.get("matrix", []) or []:
        if isinstance(m, dict):
            m.pop("hasKey", None)
    return drop_volatile(ov)


def _alltime(conn) -> dict:
    """全库聚合（**不是** 720 小时窗口）：总量、分层、分状态、按自然日序列。"""
    row = conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN status='HEALTHY' THEN 1 ELSE 0 END) healthy, "
        "       MIN(created_at) first_at, MAX(created_at) last_at, "
        "       COUNT(DISTINCT endpoint_id) endpoints, COUNT(DISTINCT model_id) models "
        "FROM probe_result").fetchone()
    total = row["n"] or 0
    healthy = row["healthy"] or 0
    by_level = {r["level"] or "—": r["n"] for r in conn.execute(
        "SELECT level, COUNT(*) n FROM probe_result GROUP BY level")}
    by_status = {r["status"] or "—": r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM probe_result GROUP BY status")}
    daily = []
    for r in conn.execute(
            "SELECT date(created_at) d, COUNT(*) n, "
            "       SUM(CASE WHEN status='HEALTHY' THEN 1 ELSE 0 END) h "
            "FROM probe_result GROUP BY date(created_at) ORDER BY d"):
        n, h = r["n"] or 0, r["h"] or 0
        daily.append({"date": r["d"], "total": n, "healthy": h,
                      "availability": round(h / n * 100, 1) if n else None})
    return {
        "totalProbes": total,
        "healthy": healthy,
        "availability": round(healthy / total * 100, 1) if total else None,
        "firstAt": row["first_at"],
        "lastAt": row["last_at"],
        "endpointCount": row["endpoints"] or 0,
        "modelCount": row["models"] or 0,
        "byLevel": by_level,
        "byStatus": by_status,
        "daily": daily,
    }


def probes_as_of(conn) -> str | None:
    """**数据时点**：有效探测记录的最大采集时间。

    刻意**不**取导出时刻——导出可能在探测之间发生（甚至手工补跑），
    用「机器什么时候打包」冒充「数据到哪儿了」会高估新鲜度。
    """
    return conn.execute("SELECT MAX(created_at) FROM probe_result").fetchone()[0]


# 注：原始 `health` **整体不导出**（政策 §2.7 / 报告 §5.3.7）。原先设想的
# `{ok, probeRows, latestProbeAt}` 三字段已被 `meta.dataAsOf`（数据时点）与
# `probes.total` / `overview.allTime.totalProbes`（条数）覆盖，故不再保留该函数——
# 留着空壳只会让人以为「health 是要导出的，只是这次没调」。


def _rankings(conn, scene: str) -> dict:
    from . import rank as ranker
    from .static_export import _summarize_excluded

    payload = ranker.board(conn, scene)
    return _summarize_excluded(payload)   # excluded 明细 -> 聚合摘要


def _meta(conn, probes: dict, windows: list[int], *, generated_at: str,
          snapshot_id: str, data_as_of: str | None) -> dict:
    """契约元信息（自述块）。字段清单见 `docs/public-data-policy.md` §2.8。

    `endpointsCount` = 公开端点清单条数（与 `endpoints.items` 等长），
    供公开页写「共监控 N 个接口」之类的话术；活跃/在线的细化口径由总览给出。
    `windows` 放在这里（而非顶层）是为了让「契约长什么样」集中一处；
    它与 `overview` 的数值键同源，因此不会漂移。
    """
    return {
        "snapshotId": snapshot_id,
        "generatedAt": generated_at,
        "dataAsOf": data_as_of,
        "timezone": "Asia/Shanghai",
        "measurementScope": MEASUREMENT_SCOPE,
        "freshness": freshness_of(data_as_of),
        "samplePolicy": {
            "recentProbeLimit": probes["limit"],
            "historyIsTruncated": probes["total"] > probes["limit"],
        },
        "policyVersion": POLICY_VERSION,
        "probeSource": "tokriva 本机探测（出口固定，历史可比）",
        "windows": list(windows),
        "endpointsCount": conn.execute("SELECT COUNT(*) FROM endpoint").fetchone()[0],
        "modelCount": conn.execute(
            "SELECT COUNT(DISTINCT model_id) FROM model "
            "WHERE status='ACTIVE' AND COALESCE(is_chat,1)=1").fetchone()[0],
    }


# ---------------------------------------------------------------------------
# 组装与落盘
# ---------------------------------------------------------------------------
def build_public(conn, probes_limit: int = DEFAULT_PROBES,
                 windows: Iterable[int] = WINDOWS,
                 now: datetime | None = None) -> dict[str, Any]:
    """在给定连接上构建公开快照（纯读取，无副作用）。

    返回**扁平**契约：`schemaVersion` + `meta` / `overview` / `rankings` /
    `endpoints` / `probes` / `catalog`。`allTime` 并入 `overview.allTime`——
    它与各窗口同为一个「总览」维度的取值，分家会让人以为它是另一个集合。

    顺序很关键：先算出**数据**（不含任何时间戳），再据此算指纹 `snapshotId`，
    最后才拼 `meta`。这样「库没变 → 指纹不变」，发布器可据此跳过无变化的提交。
    """
    windows = list(windows)
    probes = _probes(conn, probes_limit)
    data: dict[str, Any] = {
        "overview": {str(h): _overview(conn, h) for h in windows},
        "rankings": {s: _rankings(conn, s) for s in SCENES},
        "endpoints": _endpoints(conn),
        "probes": probes,
        "catalog": _catalog(conn),
    }
    data["overview"]["allTime"] = _alltime(conn)

    now = now or datetime.now()
    generated_at = now.isoformat(timespec="seconds")
    data_as_of = probes_as_of(conn)
    digest = snapshot_digest(data)
    stamp = _stamp(data_as_of) or _stamp(generated_at)
    snapshot_id = f"{stamp}{_local_offset(now)}-{digest}"

    return {
        "schemaVersion": SCHEMA_VERSION,
        "meta": _meta(conn, probes, windows, generated_at=generated_at,
                      snapshot_id=snapshot_id, data_as_of=data_as_of),
        **data,
    }


def write_snapshot(snapshot: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """原子写出 `snapshot.json`（先写临时文件再 replace，避免半个 JSON）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    path = out_dir / "snapshot.json"
    tmp = out_dir / "snapshot.json.tmp"
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
    return {
        "path": str(path),
        "raw_bytes": len(body.encode("utf-8")),
        "gzip_bytes": len(gzip.compress(body.encode("utf-8"))),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m hub.public_snapshot",
        description="生成 GitHub Pages 公开站的数据契约 snapshot.json（正向白名单）。")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"输出目录（默认 {DEFAULT_OUT.relative_to(ROOT)}）")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB,
                    help="SQLite 库路径（默认 data/hub.db）")
    ap.add_argument("--probes", type=int, default=DEFAULT_PROBES,
                    help=f"随包探测明细条数（默认 {DEFAULT_PROBES}，上限 2000）")
    ap.add_argument("--window", default=",".join(str(w) for w in WINDOWS),
                    help="overview 统计窗口，小时，逗号分隔")
    ap.add_argument("--scan-only", action="store_true",
                    help="只构建并跑敏感扫描，不落盘")
    ap.add_argument("--quiet", action="store_true", help="只输出一行结果")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from . import db

    args = _parse_args(argv)
    windows = [int(w) for w in str(args.window).split(",") if w.strip()]
    probes_limit = max(1, min(2000, args.probes))

    conn = db.connect_ro(args.db)
    try:
        snapshot = build_public(conn, probes_limit=probes_limit, windows=windows)
    finally:
        conn.close()

    findings = scan_payload(snapshot)
    if findings:
        print("❌ 敏感字段扫描未通过，已中止（未落盘）：", file=sys.stderr)
        for f in findings[:25]:
            print(f"  - [{f['rule']}] {f['where']}: {f['match']}", file=sys.stderr)
        if len(findings) > 25:
            print(f"  … 另有 {len(findings) - 25} 项", file=sys.stderr)
        return 2

    if args.scan_only:
        print(f"敏感扫描通过（{len(json.dumps(snapshot))} 字符未命中黑名单）。")
        return 0

    info = write_snapshot(snapshot, args.out)
    raw, gz = info["raw_bytes"], info["gzip_bytes"]
    if args.quiet:
        print(f"{info['path']}  {raw:,} B / gzip {gz:,} B  {snapshot['meta']['snapshotId']}")
    else:
        meta, ranks = snapshot["meta"], snapshot["rankings"]
        print(f"已生成公开快照 {info['path']}")
        print(f"  快照 ID   {meta['snapshotId']}")
        print(f"  生成时间  {meta['generatedAt']}   数据时点  {meta['dataAsOf']}"
              f"   新鲜度 {meta['freshness']}")
        print(f"  探测明细  {snapshot['probes']['total']} 条"
              f"（随包 {len(snapshot['probes']['items'])} 条"
              f" / 截断 {meta['samplePolicy']['historyIsTruncated']}）")
        print(f"  全历史    {snapshot['overview']['allTime']['totalProbes']} 条"
              f" / 可用率 {snapshot['overview']['allTime']['availability']}%")
        print(f"  榜单      " + "  ".join(
            f"{s}:{len(ranks[s]['items'])} 项" for s in SCENES))
        print(f"  端点      {len(snapshot['endpoints']['items'])} 个"
              f"   门槛 {len(snapshot['catalog']['items'])} 家")
        print(f"  窗口      " + "、".join(f"近 {w}h" if w < 168 else f"近 {w // 24}天"
                                          for w in meta["windows"]))
        print(f"  体量      {raw:,} B（未压缩） / {gz:,} B（gzip）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
