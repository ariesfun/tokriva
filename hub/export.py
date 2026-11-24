"""向 LiteLLM 下发：把排名变成模型清单，保留历史版本支持回滚。

安全约束（设计文档 7.3）：
- 仅 allow_proxy 明确允许的端点才进入网关；UNKNOWN 按「禁止转发」处理。
- 下发前留存上一版，支持一键回滚。
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "litellm"


def _eligible_endpoints(conn) -> dict[int, dict]:
    """可按条款转发的端点。默认 UNKNOWN = 不允许。"""
    rows = conn.execute(
        """SELECT e.id, e.label, e.base_url, e.protocol, e.auth_env,
                  c.allow_proxy
           FROM endpoint e
           LEFT JOIN catalog_entry c ON c.provider_id = e.provider_id
           WHERE e.active=1"""
    ).fetchall()
    out = {}
    for r in rows:
        allowed = str(r["allow_proxy"] or "UNKNOWN").upper() in ("YES", "TRUE", "ALLOWED")
        out[r["id"]] = {**dict(r), "allowed": allowed}
    return out


def build_config(conn, scene: str = "agent", alias: str = "default") -> tuple[str, list[str]]:
    """生成 LiteLLM 配置文本。返回 (yaml, 被排除的条目说明)。"""
    from . import rank

    results = rank.build(conn, scene, persist=False)
    eps = _eligible_endpoints(conn)
    excluded = []
    model_list = []
    priority = 0

    for r in results:
        if not r["eligible"]:
            continue
        ep = eps.get(r["endpoint_id"])
        if not ep:
            continue
        if not ep["allowed"]:
            excluded.append(f"{r['endpoint']}/{r['model_id']}: allow_proxy 未核实")
            continue
        priority += 1
        model_list.append({
            "model_name": alias,
            "litellm_params": {
                "model": f"openai/{r['model_id']}",
                "api_base": ep["base_url"],
                "api_key": f"os.environ/{ep['auth_env']}",
            },
            "model_info": {
                "tokriva_priority": priority,
                "tokriva_score": r["score"],
                "tokriva_endpoint": r["endpoint"],
            },
        })

    if not model_list:
        return "", excluded

    lines = [
        "# 由 Tokriva 生成，请勿手改。",
        f"# 场景: {scene}  生成时间: {datetime.now().isoformat(timespec='seconds')}",
        "# 注意：model_info.priority 的具体键名需与实际 LiteLLM 版本核对（设计文档第 10 节）。",
        "model_list:",
    ]
    for m in model_list:
        lines.append(f"  - model_name: {m['model_name']}")
        lines.append("    litellm_params:")
        for k, v in m["litellm_params"].items():
            lines.append(f"      {k}: {v}")
        lines.append("    model_info:")
        for k, v in m["model_info"].items():
            lines.append(f"      {k}: {v}")
    return "\n".join(lines) + "\n", excluded


def write_config(conn, scene: str = "agent", alias: str = "default") -> dict:
    """写入并保留历史版本。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    text, excluded = build_config(conn, scene, alias)
    if not text:
        return {"ok": False, "reason": "没有可下发的条目（allow_proxy 未核实或无合格项）",
                "excluded": excluded}

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    latest = OUT_DIR / "latest.yaml"
    if latest.exists():
        shutil.copy2(latest, OUT_DIR / f"prev-{ts}.yaml")
    (OUT_DIR / f"dynamic-{ts}.yaml").write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return {"ok": True, "path": str(latest), "excluded": excluded,
            "count": text.count("- model_name:")}


def rollback() -> dict:
    """回滚到上一版。"""
    latest = OUT_DIR / "latest.yaml"
    prevs = sorted(OUT_DIR.glob("prev-*.yaml"))
    if not prevs:
        return {"ok": False, "reason": "没有可回滚的历史版本"}
    prev = prevs[-1]
    shutil.copy2(prev, latest)
    return {"ok": True, "restored_from": prev.name}
