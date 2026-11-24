"""阶段 0 探针入口。

用法：
    python -m hub.run --once          # 跑一轮
    python -m hub.run --report        # 只看汇总
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from . import db
from . import quota_rules
from .probe import (extract_header_quota, pick_idle_model, probe_l0,
                    probe_stream, probe_usage, probe_utilization)

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "hub.db"
# 每轮每端点最多测几个模型。免费额度有限，不宜贪多。
MAX_MODELS_PER_ROUND = 2
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

ENDPOINTS = [
    {
        "provider": "LightVela",
        "website": "https://token.lightvela.ai",
        "kind": "model_api",
        "label": "LightVela",
        "base_url": "https://token.lightvela.ai/v1",
        "protocol": "openai_chat",
        "auth_env": "LIGHTVELA_API_KEY",
        "default_model": "auto",
        "use_proxy": 0,
        "daily_budget": 200,
        "timeout_sec": 45,
        "note": "auto 会选到推理型模型，前 256 个字是思考过程，正文出现较晚，所以首字延迟偏高",
    },
    {
        "provider": "AMD Radeon (官方直连)",
        "website": "https://developer.amd.com.cn",
        "kind": "serverless",
        "label": "AMD-Radeon",
        "base_url": "https://developer.amd.com.cn/radeon/api/v1",
        "protocol": "openai_chat",
        "auth_env": "AMD_BEARER_TOKEN",
        "default_model": "Qwen3.8-27B",
        "use_proxy": 0,
        "daily_budget": 200,
        "utilization": 1,
        "usage": 1,
        "include_usage": 0,
        "timeout_sec": 60,
        "note": "官方 OpenAI 兼容；另有 /messages(Anthropic)，无 /responses。有 /usage 与 /models/utilization",
    },
    {
        "provider": "Packet.ai",
        "website": "https://api.packet.ai",
        "kind": "model_api",
        "label": "Packet",
        "base_url": "https://api.packet.ai/v1",
        "protocol": "openai_chat",
        "auth_env": "PACKET_API_KEY",
        "default_model": "nvidia-deepseek-v4-1-flash-nvfp4",
        "use_proxy": 1,
        "daily_budget": 200,
        "timeout_sec": 35,
        "note": "直连被 Cloudflare 403 拦，需走 7890 代理；响应常 >20s 且可能完全无响应",
    },
    {
        # OpenRouter 聚合网关：一个 Key 覆盖多家服务商的免费模型。
        # free_only=1 表示 L0 只保留 :free 模型，避免把几百个付费模型塞进榜单。
        "provider": "OpenRouter",
        "website": "https://openrouter.ai",
        "kind": "proxy_station",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "protocol": "openai_chat",
        "auth_env": "OPENROUTER_API_KEY",
        "default_model": "nvidia/nemotron-3.5-super-vl:free",
        "free_only": 1,
        "use_proxy": 1,
        "daily_budget": 200,
        "timeout_sec": 60,
        "note": "聚合网关，免费模型用 :free 后缀；L0 只留免费模型，覆盖 NVIDIA/Particle/Baidu/Dots/Recursive/LiquidAI/inclusionAI/Google 等",
    },
]


def load_env() -> None:
    if not ENV_PATH.exists():
        sys.exit(f"缺少 {ENV_PATH}")
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def capture_usage(conn, ep: dict) -> None:
    """把 /usage 的账号级限额写入 quota_snapshot（A 档：接口可查）。"""
    data = probe_usage(ep)
    if not data:
        return
    limit = data.get("daily_cost_limit_usd")
    used = data.get("daily_cost_used_usd")
    remaining = data.get("daily_cost_remaining_usd")
    # /usage 给的是**时区名**（如 Asia/Shanghai），语义为「每天此时区 00:00 重置」。
    # 落库前换算成**本机本地时刻**，与全库时间戳口径一致，前端原样展示即可
    # （直接把时区名写进 reset_at 会让前端显示成「重置 Asia/Shanghai」这种乱码）。
    reset_at = quota_rules.reset_at_from_timezone(data.get("daily_reset_timezone"))
    if limit is not None:
        conn.execute(
            """INSERT INTO quota_snapshot(endpoint_id,quota_type,unit,total,used,
                                          remaining,source,reset_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (ep["id"], "DAILY", "USD", limit, used, remaining, "API", reset_at),
        )
    rpm = data.get("rpm_limit")
    if rpm is not None:
        conn.execute(
            """INSERT INTO quota_snapshot(endpoint_id,quota_type,unit,total,source,reset_at)
               VALUES(?,?,?,?,?,?)""",
            (ep["id"], "RATE_LIMIT", "REQUEST", rpm, "API", reset_at),
        )
    conn.commit()


def capture_utilization(conn, ep: dict) -> None:
    """把 /models/utilization 写入快照表。"""
    for u in probe_utilization(ep):
        conn.execute(
            """INSERT INTO model_utilization(endpoint_id,model_id,utilization,state,sampled_at)
               VALUES(?,?,?,?,?)""",
            (ep["id"], u["model_id"], u.get("utilization"), u.get("state"),
             u.get("sampled_at")),
        )
    conn.commit()


def run_once(only: str | None = None) -> None:
    load_env()
    conn = db.connect(DB_PATH)
    db.seed_endpoints(conn, ENDPOINTS)
    seed_catalog(conn)

    # 测试问题：用户可在设置里自定义；空 = 内置默认问题。
    # 自定义问题的记录带 prompt_tag='custom'，界面上与默认问题的速度分开展示。
    from .settings import load_settings
    _cfg = load_settings()
    prompt_cfg = {"text": (_cfg.get("probePrompt") or "").strip() or "Count from 1 to 20.",
                  "tag": "custom" if (_cfg.get("probePrompt") or "").strip() else "default"}

    sql = "SELECT * FROM endpoint WHERE active=1"
    params: tuple = ()
    if only:
        # 单接口重测：只探测指定 label 的端点，省时省额度
        sql += " AND label=?"
        params = (only,)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()

    print(f"{'endpoint':14s} {'L0':10s} {'L2':12s} {'流式':12s} {'TTFT':>9s} {'TPS':>7s} {'总耗时':>9s}")
    print("-" * 88)

    for ep in rows:
        ep = dict(ep)
        try:
            _probe_one(conn, ep, prompt_cfg)
        except Exception as e:
            print(f"{ep['label']:14s} 探测异常: {type(e).__name__}: {e}")

    print()
    report(conn)
    conn.close()


def _probe_one(conn, ep: dict, prompt_cfg: dict | None = None) -> None:
    """单个端点的完整探测。异常在调用方捕获，避免一个端点拖垮整轮。"""
    if True:
        prompt_cfg = prompt_cfg or {"text": "Count from 1 to 20.", "tag": "default"}
        l0 = probe_l0(ep)
        db.record_probe(conn, l0)
        conn.commit()

        # 失败即停（三层诊断 A1）：连通层不通过时跳过本轮速度探测。
        # 网络不通时继续发 L2 ①会产生同一故障的两条重复记录（连通失败+速度失败），
        # ②白耗请求数额度，③加重对方/代理压力。限流同理推迟到下一轮。
        if l0.get("status") != "HEALTHY":
            print(f"{ep['label']:14s} 连通层未通过（{l0.get('status')}），本轮跳过速度探测")
            return

        # L0 成功后同步模型目录（含上下文窗口）。
        # 先整体标 MISSING，再把本次返回的置 ACTIVE —— 这是设计文档 3.4
        # 的模型目录 diff。不做这一步，端点换 base_url 后旧模型会一直留在
        # 榜单里（实测：AMD 从本地桥切直连后，桥时期模型名仍在表中且被排名）。
        live_models = l0.get("models", [])
        if l0.get("status") == "HEALTHY" and live_models:
            conn.execute(
                "UPDATE model SET status='MISSING' WHERE endpoint_id=?", (ep["id"],)
            )
        for m in live_models:
            conn.execute(
                """INSERT INTO model(endpoint_id,model_id,display_name,context_length,
                                     context_source,max_output_tokens,supports_tools,
                                     supports_reasoning,modalities,is_chat,is_free)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(endpoint_id,model_id) DO UPDATE SET
                     last_seen=datetime('now','localtime'),
                     display_name=COALESCE(excluded.display_name, model.display_name),
                     context_length=COALESCE(excluded.context_length, model.context_length),
                     context_source=CASE WHEN excluded.context_length IS NOT NULL
                                         THEN excluded.context_source
                                         ELSE model.context_source END,
                     max_output_tokens=COALESCE(excluded.max_output_tokens, model.max_output_tokens),
                     supports_tools=COALESCE(excluded.supports_tools, model.supports_tools),
                     supports_reasoning=COALESCE(excluded.supports_reasoning, model.supports_reasoning),
                     modalities=COALESCE(excluded.modalities, model.modalities),
                     is_chat=COALESCE(excluded.is_chat, model.is_chat),
                     is_free=COALESCE(excluded.is_free, model.is_free),
                     status='ACTIVE'""",
                (ep["id"], m["model_id"], m.get("display_name"),
                 m.get("context_length"), m.get("context_source", "UNKNOWN"),
                 m.get("max_output_tokens"), m.get("supports_tools"),
                 m.get("supports_reasoning"), m.get("modalities"),
                 m.get("is_chat"), m.get("is_free")),
            )
        conn.commit()

        # 额度与利用率（仅支持该能力的端点）
        if ep.get("has_usage"):
            capture_usage(conn, ep)
        if ep.get("has_utilization"):
            capture_utilization(conn, ep)

        # 探测预算（设计文档第 1 章 / 6.3）：L0 已跑完且不占额度，这里只约束 L2。
        # 放到 L0 之后、选模型之前，是因为「降级到 L0」的含义正是——连通层照测，
        # 只把吃额度的速度测试停掉；接口不会因为额度紧张就从监控里消失。
        from .budget import (decision as _budget_decision,
                             used_today as _budget_used,
                             quota_used_pct as _budget_quota)
        from .settings import budget_config
        _budget_on, _guard_pct = budget_config()
        if _budget_on:
            # 两道闸门取更严：次数（今天还能测几次）+ 额度（账号的钱还剩多少）
            _dec = _budget_decision(ep.get("daily_budget"), _budget_used(conn, ep["id"]),
                                    _guard_pct, _budget_quota(conn, ep["id"]))
        else:
            _dec = {"state": "ok", "allow_l2": True, "max_models": None, "reason": ""}
        if not _dec["allow_l2"]:
            print(f"{ep['label']:14s} {l0['status']:10s} {'SKIP':12s} "
                  f"{'-':12s} {'-':>9s} {'-':>7s} {'-':>9s}  {_dec['reason']}")
            return

        # 选模型：每轮最多测 N 个对话模型，兼顾覆盖与额度消耗。
        # 排序依据 = 已实测耗时（快的优先），满载(full)的排除，
        # 未验证过的排后面但保留名额，否则新模型永远拿不到样本。
        model = ep["default_model"] or "auto"
        targets = [model]
        if ep.get("has_utilization"):
            utils = {u["model_id"]: u for u in probe_utilization(ep)}
            speed = {r["model_id"]: r["avg_ms"] for r in conn.execute(
                """SELECT model_id, AVG(total_ms) avg_ms FROM probe_result
                   WHERE endpoint_id=? AND status='HEALTHY' AND model_id IS NOT NULL
                     AND created_at > datetime('now','localtime','-24 hours')
                   GROUP BY model_id""",
                (ep["id"],)
            )}
            chat_models = {r["model_id"] for r in conn.execute(
                "SELECT model_id FROM model WHERE endpoint_id=? AND is_chat=1 "
                "AND status='ACTIVE'",
                (ep["id"],)
            )}
            verified = [(speed[m], m) for m in chat_models
                        if m in speed and (utils.get(m) or {}).get("state") != "full"]
            verified.sort()
            unverified = [m for m in chat_models
                          if m not in speed and (utils.get(m) or {}).get("state") != "full"]
            unverified.sort(key=lambda m: (utils.get(m) or {}).get("utilization") or 0)
            targets = [m for _, m in verified][:MAX_MODELS_PER_ROUND]
            for m in unverified:
                if len(targets) >= MAX_MODELS_PER_ROUND:
                    break
                targets.append(m)
            if not targets:
                targets = [model]
            # 探测顺序即优先级：先快的，再未验证的
            model = targets[0]
        elif ep.get("free_only"):
            # 聚合网关（OpenRouter）：从已发现的免费模型里轮换测几个，
            # 而不是永远只测 default_model 一个，才能覆盖到多家服务商。
            free_models = [r["model_id"] for r in conn.execute(
                "SELECT model_id FROM model WHERE endpoint_id=? AND is_free=1 "
                "AND is_chat=1 AND status='ACTIVE' ORDER BY model_id",
                (ep["id"],)
            )]
            if free_models:
                targets = free_models[:MAX_MODELS_PER_ROUND]
                model = targets[0]

        # 接近预算上限时的降级：只保底测 1 个模型（通常是已实测最快或默认模型），
        # 砍掉探索性名额。额度到顶时上面已直接 return，不会走到这里。
        if _dec.get("max_models"):
            targets = targets[:max(1, int(_dec["max_models"]))]
            model = targets[0]

        # 推理模型需要更大 token 预算才产出正文，实测阈值 256
        max_tokens = 256 if ep["label"] == "LightVela" else 128
        l2 = None
        for mi, mname in enumerate(targets):
            l2 = probe_stream(ep, mname, level="L2", max_tokens=max_tokens,
                              include_usage=bool(ep.get("include_usage", 1)),
                              prompt=prompt_cfg["text"], prompt_tag=prompt_cfg["tag"])
            db.record_probe(conn, l2)
            # 只要首个模型成功，就没必要再消耗额度测更多
            if mi == 0 and l2.get("status") == "HEALTHY":
                model = mname
                break
        model = model or targets[0]

        # B 档：响应头里的额度（仅部分服务商提供）
        prov = conn.execute("SELECT name FROM provider WHERE id=?",
                            (ep["provider_id"],)).fetchone()
        hq = extract_header_quota((l2 or {}).get("resp_headers"),
                                  prov["name"] if prov else None)
        if hq:
            from . import ledger
            for q in hq:
                ledger.write_quota(
                    conn, ep["id"], quota_type=q["quota_type"], unit=q["unit"],
                    total=q.get("total"), used=q.get("used"),
                    remaining=q.get("remaining"), source="HEADER",
                    scope=q.get("scope", "UNKNOWN"), visibility="PUBLIC",
                    confidence="CONFIRMED", reset_at=q.get("reset_at"),
                )

        # 别名背后实际服务的模型：单独登记，窗口挂在它身上
        served = (l2 or {}).get("served_model")
        last_model = (l2 or {}).get("model_id") or model
        if served and served != last_model:
            conn.execute(
                """INSERT INTO model(endpoint_id,model_id,served_as,context_source)
                   VALUES(?,?,?,'UNKNOWN')
                   ON CONFLICT(endpoint_id,model_id) DO UPDATE SET
                     last_seen=datetime('now','localtime'),
                     served_as=excluded.served_as""",
                (ep["id"], served, last_model),
            )
            conn.commit()

        l2 = l2 or {}
        ttft = f"{l2['ttft_ms']:.0f}ms" if l2.get("ttft_ms") else "-"
        if l2.get("tps"):
            tps = f"{l2['tps']:.1f}"
        elif l2.get("tps_unreliable"):
            tps = "缓冲"
        else:
            tps = "-"
        total = f"{l2['total_ms']:.0f}ms" if l2.get("total_ms") else "-"
        mode = l2.get("stream_mode") or "-"
        print(f"{ep['label']:14s} {l0['status']:10s} {l2['status']:12s} {mode:12s} "
              f"{ttft:>9s} {tps:>7s} {total:>9s}")


def model_report(conn) -> None:
    """模型能力视图：上下文窗口、可用输入预算、来源。"""
    from .capability import fmt_tokens, usable_input_tokens

    print("=== 模型能力（上下文窗口）===")
    print(f"{'endpoint':13s} {'model':38s} {'上下文':>8s} {'输出上限':>9s} "
          f"{'可用输入':>9s} {'来源':>12s} {'工具':>4s}")
    print("-" * 108)
    rows = conn.execute(
        """SELECT e.label, m.model_id, m.served_as, m.context_length,
                  m.max_output_tokens, m.context_source, m.supports_tools,
                  m.supports_reasoning
           FROM model m JOIN endpoint e ON e.id=m.endpoint_id
           ORDER BY e.id, m.model_id"""
    ).fetchall()
    if not rows:
        print("  （暂无模型记录，先跑 --once）")
        return
    for r in rows:
        ctx = fmt_tokens(r["context_length"])
        out = fmt_tokens(r["max_output_tokens"])
        usable = fmt_tokens(usable_input_tokens(r["context_length"],
                                                r["max_output_tokens"]))
        src = r["context_source"] or "UNKNOWN"
        tools = "-" if r["supports_tools"] is None else ("是" if r["supports_tools"] else "否")
        name = r["model_id"]
        if r["served_as"]:
            name = f"{name}（经 {r['served_as']}）"
        print(f"{r['label']:13s} {name:38s} {ctx:>8s} {out:>9s} "
              f"{usable:>9s} {src:>12s} {tools:>4s}")

    print("\n说明：上下文=服务商声明的窗口；输出上限=单次生成上限；")
    print("      可用输入=窗口−输出上限（多服务商把输入输出算在同一窗口内）。")
    print("      UNKNOWN 表示该端点 /models 未返回该字段，需手动核实或实测，不做推测。")


def report(conn) -> None:
    print("=== 最近探测汇总 ===")
    rows = conn.execute(
        """SELECT e.label, p.level, p.status, p.http_status, p.error_kind,
                  p.ttft_ms, p.tps, p.total_ms, p.response_shape, p.stream_mode,
                  p.created_at
           FROM probe_result p JOIN endpoint e ON e.id=p.endpoint_id
           ORDER BY p.id DESC LIMIT 20"""
    ).fetchall()
    for r in rows:
        print(f"  {r['label']:14s} {r['level']:3s} {r['status']:18s} "
              f"http={r['http_status']} shape={r['response_shape'] or '-':5s} "
              f"mode={r['stream_mode'] or '-':11s} "
              f"ttft={r['ttft_ms'] or '-':>8} tps={r['tps'] or '-':>6}")

    print("\n=== 延迟分位数（L2，仅 HEALTHY 且真流式）===")
    stat = conn.execute(
        """SELECT e.label,
                  COUNT(*) n,
                  AVG(p.ttft_ms) avg_ttft,
                  AVG(p.tps) avg_tps
           FROM probe_result p JOIN endpoint e ON e.id=p.endpoint_id
           WHERE p.level='L2' AND p.status='HEALTHY' AND p.ttft_ms IS NOT NULL
           GROUP BY e.label"""
    ).fetchall()
    if not stat:
        print("  （暂无合格样本）")
    for s in stat:
        print(f"  {s['label']:14s} n={s['n']:2d}  "
              f"平均TTFT={s['avg_ttft']:.0f}ms  "
              f"平均TPS={s['avg_tps']:.1f}" if s['avg_tps'] else
              f"  {s['label']:14s} n={s['n']:2d}  平均TTFT={s['avg_ttft']:.0f}ms")

    print("\n=== 状态分布 ===")
    for s in conn.execute(
        "SELECT status, COUNT(*) c FROM probe_result GROUP BY status ORDER BY c DESC"
    ):
        print(f"  {s['status']:20s} {s['c']}")


def export_csv(conn) -> Path:
    import csv
    out = DB_PATH.parent / "probe_results.csv"
    rows = conn.execute(
        """SELECT e.label endpoint, p.level, p.model_id, p.status, p.http_status,
                  p.error_kind, p.dns_ms, p.tcp_ms, p.tls_ms, p.ttfb_ms, p.ttft_ms,
                  p.output_tokens, p.tps, p.total_ms, p.token_source,
                  p.response_shape, p.stream_mode, p.created_at
           FROM probe_result p JOIN endpoint e ON e.id=p.endpoint_id
           ORDER BY p.id"""
    ).fetchall()
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(rows[0].keys() if rows else [])
        for r in rows:
            w.writerow([r[k] for k in r.keys()])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Tokriva 免费算力探针与台账")
    ap.add_argument("--once", action="store_true", help="跑一轮探测")
    ap.add_argument("--only", metavar="LABEL", help="只探测指定 label 的端点（配合 --once）")
    ap.add_argument("--report", action="store_true", help="探测汇总")
    ap.add_argument("--models", action="store_true", help="模型能力与上下文窗口")
    ap.add_argument("--csv", action="store_true", help="导出探测明细 CSV")
    ap.add_argument("--rank", choices=["agent", "speed", "longterm", "all"],
                    help="生成榜单")
    ap.add_argument("--catalog", action="store_true", help="门槛台账")
    ap.add_argument("--remind", action="store_true", help="生成提醒")
    ap.add_argument("--webhook", metavar="URL", help="提醒推送到 webhook")
    ap.add_argument("--export", metavar="ALIAS", nargs="?", const="default",
                    help="下发 LiteLLM 配置")
    ap.add_argument("--rollback", action="store_true", help="回滚下发配置")
    ap.add_argument("--allow", nargs=2, metavar=("LABEL", "YES|NO"),
                    help="确认条款后允许/禁止该端点进入网关")
    ap.add_argument("--catalog-export", metavar="FILE", help="导出门槛台账 JSON")
    ap.add_argument("--catalog-import", metavar="FILE",
                    help="导入门槛台账 JSON（--only-missing 只补空缺）")
    ap.add_argument("--only-missing", action="store_true")
    ap.add_argument("--quota-set", metavar="LABEL", help="手工录入额度（C 档）")
    ap.add_argument("--quota-type", default="TOTAL")
    ap.add_argument("--quota-unit", default="USD")
    ap.add_argument("--quota-total", type=float)
    ap.add_argument("--quota-remaining", type=float)
    ap.add_argument("--expires", metavar="YYYY-MM-DD")
    ap.add_argument("--alias-set", nargs=2, metavar=("ALIAS", "SCENE"),
                    help="登记别名组，如 coding-main agent")
    ap.add_argument("--aliases", action="store_true", help="列出别名组")
    ap.add_argument("--cycle", action="store_true",
                    help="完整闭环：探测→榜单→提醒→下发")
    ap.add_argument("--loop", action="store_true", help="常驻按间隔循环")
    ap.add_argument("--interval-min", type=int, default=30, help="循环间隔（分钟）")
    ap.add_argument("--prune", type=int, nargs="?", const=30, metavar="DAYS",
                    help="保留策略：明细聚合、快照裁剪（默认 30；审计表 ×3）")
    a = ap.parse_args()

    if a.loop:
        run_loop(a.interval_min, a.webhook, a.prune or 30)
        return
    if a.prune:
        print(prune(a.prune))
        return
    if a.cycle:
        run_cycle(a.webhook)
        return
    if a.catalog_export:
        import json as _json
        from . import ledger as _lg
        conn = db.connect(DB_PATH)
        data = _lg.export_catalog(conn)
        conn.close()
        Path(a.catalog_export).write_text(
            _json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已导出 {len(data['entries'])} 条 → {a.catalog_export}")
        return
    if a.catalog_import:
        import json as _json
        from . import ledger as _lg
        conn = db.connect(DB_PATH)
        data = _json.loads(Path(a.catalog_import).read_text(encoding="utf-8"))
        print(_lg.import_catalog(conn, data, only_if_missing=a.only_missing))
        conn.close()
        return
    if a.quota_set:
        from . import ledger as _lg
        conn = db.connect(DB_PATH)
        _lg.set_manual_quota(conn, a.quota_set, quota_type=a.quota_type,
                             unit=a.quota_unit, total=a.quota_total,
                             remaining=a.quota_remaining, expires_at=a.expires)
        if a.expires:
            conn.execute("UPDATE endpoint SET expires_at=? WHERE label=?",
                         (a.expires, a.quota_set))
            conn.commit()
        conn.close()
        print(f"{a.quota_set} 额度已录入：{a.quota_type}/{a.quota_unit} "
              f"total={a.quota_total} remaining={a.quota_remaining} expires={a.expires}")
        return
    if a.alias_set:
        from . import ledger as _lg
        conn = db.connect(DB_PATH)
        _lg.set_alias(conn, a.alias_set[0], a.alias_set[1])
        conn.close()
        print(f"别名 {a.alias_set[0]} → 场景 {a.alias_set[1]}")
        return
    if a.aliases:
        from . import ledger as _lg
        conn = db.connect(DB_PATH)
        rows = _lg.list_aliases(conn)
        conn.close()
        print("=== 别名组 ===")
        for r in rows or []:
            print(f"  {r['alias']:16s} → {r['scene']}")
        if not rows:
            print("  （空）")
        return
    if a.allow:
        print(set_allow(db.connect(DB_PATH), a.allow[0], a.allow[1]))
        return
    if a.rollback:
        from . import export as ex
        print(ex.rollback())
        return
    if a.export:
        from . import export as ex, ledger as _lg
        conn = db.connect(DB_PATH)
        scene = _lg.resolve_alias_scene(conn, a.export)
        r = ex.write_config(conn, scene, a.export)
        conn.close()
        print(r)
        return
    if a.rank:
        from . import rank
        conn = db.connect(DB_PATH)
        scenes = ["agent", "speed", "longterm"] if a.rank == "all" else [a.rank]
        for s in scenes:
            rank.print_board(rank.build(conn, s, persist=True), s)
            print()
        return
    if a.catalog:
        from . import ledger
        ledger.catalog_report(db.connect(DB_PATH))
        return
    if a.remind:
        from . import ledger
        conn = db.connect(DB_PATH)
        items = ledger.scan_reminders(conn)
        ledger.print_reminders(items)
        if a.webhook:
            print("webhook:", "成功" if ledger.send_webhook(a.webhook, items) else "失败")
        return
    if a.models:
        conn = db.connect(DB_PATH)
        model_report(conn)
    elif a.csv:
        conn = db.connect(DB_PATH)
        print("已导出:", export_csv(conn))
    elif a.report:
        conn = db.connect(DB_PATH)
        report(conn)
    elif a.once:
        run_once(only=a.only)
    else:
        ap.print_help()


CATALOG_SEED = [
    {"provider": "LightVela", "signup_method": "EMAIL", "free_form": "STANDING",
     "allow_proxy": "UNKNOWN", "source_type": "manual",
     "quota_visibility": "CONSOLE_ONLY", "confidence": "POSSIBLE",
     "note": "auto 会选到推理型模型，前 256 个字是思考过程"},
    {"provider": "AMD Radeon (官方直连)", "signup_method": "EMAIL",
     "free_form": "STANDING", "allow_proxy": "UNKNOWN", "source_type": "official",
     "quota_scope": "ACCOUNT", "quota_visibility": "PUBLIC",
     "confidence": "CONFIRMED",
     "note": "官方接口，兼容 OpenAI 与 Anthropic 两种格式；支持查询用量和模型利用率"},
    {"provider": "Packet.ai", "signup_method": "EMAIL", "free_form": "STANDING",
     "allow_proxy": "UNKNOWN", "source_type": "manual",
     "quota_visibility": "CONSOLE_ONLY", "confidence": "POSSIBLE",
     "note": "直连会被 Cloudflare 拦截，必须走代理；偶尔会完全没有响应"},
]


def seed_catalog(conn) -> None:
    """只补缺失条目，不覆盖已确认的 allow_proxy。"""
    from . import ledger
    for c in CATALOG_SEED:
        row = conn.execute(
            "SELECT id FROM provider WHERE name=?", (c["provider"],)
        ).fetchone()
        if not row:
            conn.execute("INSERT INTO provider(name,kind) VALUES(?,'model_api')",
                         (c["provider"],))
            row = conn.execute("SELECT id FROM provider WHERE name=?",
                               (c["provider"],)).fetchone()
        has = conn.execute("SELECT id FROM catalog_entry WHERE provider_id=?",
                           (row["id"],)).fetchone()
        if not has:
            ledger.upsert_catalog(conn, c["provider"],
                                  **{k: v for k, v in c.items() if k != "provider"})


def set_allow(conn, label: str, value: str) -> str:
    """人工确认条款后，才允许该端点进入网关。"""
    ep = conn.execute("SELECT provider_id FROM endpoint WHERE label=?",
                      (label,)).fetchone()
    if not ep:
        return f"未找到端点: {label}"
    conn.execute(
        "UPDATE catalog_entry SET allow_proxy=?, source_type='official', "
        "verified_at=date('now','localtime') WHERE provider_id=?",
        (value.upper(), ep["provider_id"]),
    )
    conn.commit()
    return f"{label} allow_proxy={value.upper()}"


# 无界快照表：每轮探测各插一行，只被 `ORDER BY id DESC LIMIT n` 读最新若干条，
# 因此裁掉旧行不影响任何功能。表名写死（不是用户输入），f-string 拼列名安全。
SNAPSHOT_TABLES = (
    # (表名, 时间列, 说明)
    ("quota_snapshot", "captured_at", "每轮每个有 /usage 的接口各 1~2 行"),
    ("model_utilization", "captured_at", "每轮每个模型各 1 行，实测 12 行/轮"),
)
# 审计类：出问题要能回头查，留得比快照久。
AUDIT_TABLES = (
    ("reminder_log", "created_at", "提醒流水"),
)
_AUDIT_DAYS_FACTOR = 3   # 审计保留天数 = 快照天数 × 3


def prune(days: int = 30, audit_days: int | None = None) -> dict:
    """保留策略：明细按天聚合后删除，无界快照表直接裁剪，审计表留更久。

    设计文档要求 probe_result 增长最快，明细 30 天后聚合再删。初版设想 L0/L1 与
    L2 分别保留、并做小时级摘要，实测**统一 30 天、按天聚合**够用，未再细分。
    但排查发现另有三张表同样只写不裁：

    - `quota_snapshot`  2 行/轮（有 /usage 的接口）→ 约 96 行/天
    - `model_utilization` 12 行/轮（每模型一行）→ 约 576 行/天
    - `reminder_log`    每条提醒每轮各插一行，且**全仓无读取方**

    它们都只被读最新若干条（`ORDER BY id DESC LIMIT n`），没有按时间范围的查询，
    因此裁剪旧行是安全的。不裁则随时间线性增长，迟早撑爆库。

    Args:
        days: 明细与快照的保留天数。
        audit_days: 审计表保留天数，默认 3×days——故障追溯需要更长的视野。

    Returns:
        各项删除行数与有效保留天数。
    """
    conn = db.connect(DB_PATH)
    keep = max(1, int(days))
    audit = max(keep, int(audit_days if audit_days is not None
                         else keep * _AUDIT_DAYS_FACTOR))
    result: dict = {"keep_days": keep, "audit_days": audit}
    cutoff = f"-{keep} days"
    rows = conn.execute(
        """SELECT endpoint_id, model_id, level, date(created_at) day,
                  COUNT(*) n,
                  SUM(CASE WHEN status='HEALTHY' THEN 1 ELSE 0 END) n_healthy,
                  AVG(ttft_ms) avg_ttft, AVG(tps) avg_tps,
                  AVG(total_ms) avg_total, MIN(total_ms) min_total,
                  MAX(total_ms) max_total,
                  GROUP_CONCAT(DISTINCT error_kind) error_kinds
           FROM probe_result
           WHERE created_at < datetime('now','localtime', ?)
           GROUP BY endpoint_id, model_id, level, day""",
        (cutoff,)).fetchall()
    for r in rows:
        conn.execute(
            """INSERT INTO probe_daily(endpoint_id,model_id,level,day,n,n_healthy,
                                       avg_ttft,avg_tps,avg_total,min_total,max_total,error_kinds)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(endpoint_id,model_id,level,day) DO UPDATE SET
                 n=excluded.n, n_healthy=excluded.n_healthy,
                 avg_ttft=excluded.avg_ttft, avg_tps=excluded.avg_tps,
                 avg_total=excluded.avg_total, min_total=excluded.min_total,
                 max_total=excluded.max_total, error_kinds=excluded.error_kinds""",
            (r["endpoint_id"], r["model_id"], r["level"], r["day"], r["n"],
             r["n_healthy"], r["avg_ttft"], r["avg_tps"], r["avg_total"],
             r["min_total"], r["max_total"], r["error_kinds"]))
    cur = conn.execute("DELETE FROM probe_result WHERE created_at < datetime('now','localtime', ?)",
                       (cutoff,))
    result["probe_result"] = cur.rowcount
    result["aggregated_days"] = len(rows)

    # 快照表：直接裁掉旧行，不需要聚合——它们是点位读数，不是需要累计的明细。
    for table, col, _ in SNAPSHOT_TABLES:
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {col} < datetime('now','localtime', ?)", (cutoff,))
        result[table] = cur.rowcount

    # 审计表：留更久，但同样有界。
    audit_cutoff = f"-{audit} days"
    for table, col, _ in AUDIT_TABLES:
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {col} < datetime('now','localtime', ?)", (audit_cutoff,))
        result[table] = cur.rowcount

    conn.commit()
    conn.close()
    return result


def run_loop(interval_min: int, webhook: str | None = None,
             prune_days: int = 30) -> None:
    """常驻：按间隔跑闭环。用最简单的 sleep 循环，不引入调度依赖。

    崩了重启即可；每轮结束才睡，避免上一轮超时导致堆积。
    """
    import time as _t
    n = 0
    while True:
        n += 1
        print(f"\n===== 第 {n} 轮  {_t.strftime('%Y-%m-%d %H:%M:%S')} =====", flush=True)
        try:
            run_cycle(webhook)
        except Exception as e:
            print(f"本轮失败：{type(e).__name__}: {e}")
        try:
            if prune_days:
                print("保留策略：", prune(prune_days))
        except Exception as e:
            print("prune 失败:", e)
        _t.sleep(max(60, interval_min * 60))


def run_cycle(webhook: str | None = None) -> None:
    """完整闭环：探测 → 排名 → 提醒 → 下发。"""
    from . import export as ex, ledger, rank

    print("【1/4】探测")
    run_once()

    conn = db.connect(DB_PATH)
    print("\n【2/4】排名")
    for scene in ("agent", "speed", "longterm"):
        rank.print_board(rank.build(conn, scene, persist=True), scene)
        print()

    print("【3/4】提醒")
    items = ledger.scan_reminders(conn)
    ledger.print_reminders(items)
    if webhook and items:
        print("webhook:", "成功" if ledger.send_webhook(webhook, items) else "失败")

    print("\n【4/4】下发")
    r = ex.write_config(conn, "agent", "default")
    conn.close()
    if r.get("ok"):
        print(f"  已写入 {r['path']}（{r['count']} 条）")
        for e in r.get("excluded", []):
            print(f"  跳过 {e}")
    else:
        print(f"  未下发：{r['reason']}")
        for e in r.get("excluded", [])[:5]:
            print(f"  跳过 {e}")


if __name__ == "__main__":
    main()
