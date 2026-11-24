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
- **纯读取、无副作用**：绝不调用 `/api/reminders`（会触发 `_notify_transitions()`），
  不写库、不触发探测。所有查询在**传入的连接**上完成（便于对一致性备份副本取数）。
- **单一来源**：榜单复用 `hub.rank.board()`，总览复用 `hub.status_engine.build_overview()`，
  与页面/网关口径完全一致。
- **全历史与窗口分开**：`allTime` 是库级全量聚合，**不得**用 720 小时冒充。
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

SCHEMA = 1
POLICY_VERSION = 1
DEFAULT_PROBES = 500
WINDOWS = (1, 6, 24, 168, 720)   # 近 1h / 6h / 24h / 7d / 30d
SCENES = ("speed", "agent", "stable")

# provider 内部名 -> 公开别名（留空即原名公开；需要匿名化时在此登记）
PROVIDER_ALIAS: dict[str, str] = {}


def public_provider(name: str | None) -> str | None:
    """服务商公开名。默认与内部名一致，按需在 PROVIDER_ALIAS 登记别名。"""
    if name is None:
        return None
    return PROVIDER_ALIAS.get(name, name)


def _n(v: Any, nd: int = 3) -> Any:
    return None if v is None else round(float(v), nd)


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
            "label": r["label"],
            "provider": public_provider(r["provider"]),
            "website": r["website"],
            "kind": r["kind"],
            "protocol": r["protocol"],
            "defaultModel": (dm["display_name"] if dm and dm["display_name"] else r["default_model"]),
            "active": bool(r["active"]),
            # provider_status 不公开：实测取值恒为 'UNKNOWN'（零信息量），
            # 不在政策白名单内，前端也不消费。正向白名单下"没登记"即"不公开"。
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
    """状态总览。剥掉矩阵里的 `hasKey`：它暴露"本机是否为某接口配置了凭据"，
    属运维内部信息（页面上仍能从 final=NO_KEY 得到"未接入"结论）。"""
    from .status_engine import build_overview
    ov = build_overview(conn, window_hours=hours)
    for m in ov.get("matrix", []) or []:
        if isinstance(m, dict):
            m.pop("hasKey", None)
    return ov


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


def _health(conn) -> dict:
    """健康投影：**不含**数据库路径与本机调度状态。"""
    n = conn.execute("SELECT COUNT(*) FROM probe_result").fetchone()[0]
    latest = conn.execute("SELECT MAX(created_at) FROM probe_result").fetchone()[0]
    return {"ok": True, "probeRows": n, "latestProbeAt": latest}


def _rankings(conn, scene: str) -> dict:
    from . import rank as ranker
    from .static_export import _summarize_excluded

    payload = ranker.board(conn, scene)
    return _summarize_excluded(payload)   # excluded 明细 -> 聚合摘要


def _meta(conn, probes_limit: int) -> dict:
    """契约元信息。窗口列表只放**顶层** `windows`（§1），此处不再重复。

    `endpointsCount` = 公开端点清单条数（与 `data.endpoints.items` 等长），
    供公开页写"共监控 N 个接口"之类的话术；细化到活跃/在线的口径由总览给出。
    """
    return {
        "probeSource": "tokriva 本机探测（出口固定，历史可比）",
        "timezone": "Asia/Shanghai",
        "policyVersion": POLICY_VERSION,
        "probesLimit": probes_limit,
        "endpointsCount": conn.execute("SELECT COUNT(*) FROM endpoint").fetchone()[0],
        "modelCount": conn.execute(
            "SELECT COUNT(DISTINCT model_id) FROM model "
            "WHERE status='ACTIVE' AND COALESCE(is_chat,1)=1").fetchone()[0],
    }


# ---------------------------------------------------------------------------
# 组装与落盘
# ---------------------------------------------------------------------------
def build_public(conn, probes_limit: int = DEFAULT_PROBES,
                 windows: Iterable[int] = WINDOWS) -> dict[str, Any]:
    """在给定连接上构建公开快照（纯读取，无副作用）。"""
    windows = list(windows)
    data: dict[str, Any] = {
        "meta": _meta(conn, probes_limit),
        "health": _health(conn),
        "endpoints": _endpoints(conn),
        "probes": _probes(conn, probes_limit),
        "catalog": _catalog(conn),
        "rankings": {s: _rankings(conn, s) for s in SCENES},
        "overview": {str(h): _overview(conn, h) for h in windows},
        "allTime": _alltime(conn),
    }
    return {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "dataAsOf": data["health"]["latestProbeAt"],
        "schema": SCHEMA,
        "readOnly": True,
        "mode": "public",
        "windows": windows,
        "data": data,
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

    data = snapshot["data"]
    if args.scan_only:
        print(f"敏感扫描通过（{len(json.dumps(snapshot))} 字符未命中黑名单）。")
        return 0

    info = write_snapshot(snapshot, args.out)
    raw, gz = info["raw_bytes"], info["gzip_bytes"]
    if args.quiet:
        print(f"{info['path']}  {raw:,} B / gzip {gz:,} B")
    else:
        ranks = data["rankings"]
        print(f"已生成公开快照 {info['path']}")
        print(f"  生成时间  {snapshot['generatedAt']}   数据时点  {snapshot['dataAsOf']}")
        print(f"  探测明细  {data['probes']['total']} 条（随包 {len(data['probes']['items'])} 条）")
        print(f"  全历史    {data['allTime']['totalProbes']} 条 / 可用率 {data['allTime']['availability']}%")
        print(f"  榜单      " + "  ".join(
            f"{s}:{len(ranks[s]['items'])} 项" for s in SCENES))
        print(f"  端点      {len(data['endpoints']['items'])} 个"
              f"   门槛 {len(data['catalog']['items'])} 家")
        print(f"  窗口      " + "、".join(f"近 {w}h" if w < 168 else f"近 {w // 24}天"
                                          for w in snapshot["windows"]))
        print(f"  体量      {raw:,} B（未压缩） / {gz:,} B（gzip）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
