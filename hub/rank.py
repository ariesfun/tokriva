"""排名引擎：先硬过滤，再多维加权打分。

两条设计原则来自实测与评审：
1. **缺失分项要重分配权重**，不能当 0 分。TTFT 只有真流式端才有，
   缓冲型端点天然缺这一项；若按 0 计入，稳定服务会被系统性压到后面。
2. **429 不计入可用率分母**。限流是额度问题，不是服务故障，
   混进去会让高频小额度服务被判死。
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta

from . import quota_rules
from .db import as_local, now

# 场景权重。分项缺失时在可用项之间按比例重分配。
SCENES = {
    "agent": {"availability": 0.40, "tps": 0.25, "ttft": 0.15,
              "stability": 0.10, "quota": 0.10},
    "speed": {"availability": 0.15, "tps": 0.35, "ttft": 0.40,
              "stability": 0.05, "quota": 0.05},
    "longterm": {"availability": 0.35, "tps": 0.05, "ttft": 0.05,
                 "stability": 0.15, "quota": 0.40},
}

MIN_SAMPLES = 2
FRESH_HOURS = 24


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _collect(conn, endpoint_id: int, model_id: str, hours: int = 168):
    """取某 endpoint+model 的探测样本。"""
    rows = conn.execute(
        """SELECT status, ttft_ms, total_ms, tps, stream_mode, cost_usd
           FROM probe_result
           WHERE endpoint_id=? AND model_id=? AND level='L2'
             AND created_at > datetime('now','localtime', ?)""",
        (endpoint_id, model_id, f"-{hours} hours"),
    ).fetchall()
    return rows


def _availability(rows) -> tuple[float | None, int]:
    """可用率：分母剔除 RATE_LIMITED。

    限流是额度/并发问题，不代表服务不可用；把它算成失败会让
    高额度消耗的服务被永久排在后面。
    """
    total = len(rows)
    if not total:
        return None, 0
    # 未配置密钥的样本也不计入分母：「没接入」不是「服务不可用」
    rate_limited = sum(1 for r in rows if r["status"] == "RATE_LIMITED")
    not_configured = sum(1 for r in rows if r["status"] == "NOT_CONFIGURED")
    good = sum(1 for r in rows if r["status"] == "HEALTHY")
    denom = total - rate_limited - not_configured
    if denom <= 0:
        return None, total
    return good / denom, total


def _quota_factor(conn, endpoint_id: int) -> float:
    """额度健康度 0-1：取最新**预算行**的剩余比例；无数据记 0.5 中性。

    只认额度预算行（`quota_rules.BUDGET_ROW_SQL`）：排除 RATE_LIMIT 速率限制行，
    否则「每分钟 20 次、剩余 19」会被当成 95% 额度充裕，虚高这一分项。
    """
    row = conn.execute(
        f"""SELECT remaining, total FROM quota_snapshot
           WHERE endpoint_id=? AND {quota_rules.BUDGET_ROW_SQL}
           ORDER BY id DESC LIMIT 1""",
        (endpoint_id,),
    ).fetchone()
    if not row or not row["total"]:
        return 0.5
    try:
        return max(0.0, min(1.0, float(row["remaining"]) / float(row["total"])))
    except Exception:
        return 0.5


def build(conn, scene: str = "agent", persist: bool = True) -> list[dict]:
    """生成某场景的榜单。"""
    weights = SCENES.get(scene, SCENES["agent"])
    cutoff = now() - timedelta(hours=FRESH_HOURS)

    # 候选：对话模型（含别名指向的真实模型）。
    # 必须限定 last_seen 新鲜度：端点换过 base_url 或服务商下线模型后，
    # 旧的 model 行仍留在表里，若不筛掉会把已不存在的模型排进榜
    # （实测：AMD 从本地桥切到官方直连后，桥时期的模型名仍在表中）。
    cands = conn.execute(
        """SELECT m.endpoint_id, m.model_id, m.display_name, e.label, e.protocol
           FROM model m JOIN endpoint e ON e.id=m.endpoint_id
           WHERE e.active=1
             AND COALESCE(m.is_chat,1)=1
             AND m.status='ACTIVE'"""
    ).fetchall()

    results = []
    for c in cands:
        rows = _collect(conn, c["endpoint_id"], c["model_id"])
        result = {
            "scene": scene,
            "endpoint_id": c["endpoint_id"],
            "endpoint": c["label"],
            "model_id": c["model_id"],
            "display_name": c["display_name"] or c["model_id"],
            "total_probes": len(rows),
            "eligible": False,
            "reason": "",
            "score": None,
            "detail": {},
        }

        # ---- 硬过滤 1：样本数 ----
        if len(rows) < MIN_SAMPLES:
            result["reason"] = f"样本不足({len(rows)}<{MIN_SAMPLES})"
            results.append(result)
            continue

        # ---- 硬过滤 2：新鲜度 ----
        fresh = conn.execute(
            """SELECT MAX(created_at) t FROM probe_result
               WHERE endpoint_id=? AND model_id=? AND level='L2'""",
            (c["endpoint_id"], c["model_id"]),
        ).fetchone()["t"]
        if not fresh or datetime.fromisoformat(fresh) < cutoff:
            result["reason"] = "数据过期(>24h)"
            results.append(result)
            continue

        # ---- 硬过滤 3：当前状态 ----
        last = conn.execute(
            """SELECT status, model_id, created_at FROM probe_result
               WHERE endpoint_id=? AND model_id=? AND level='L2'
               ORDER BY id DESC LIMIT 1""",
            (c["endpoint_id"], c["model_id"]),
        ).fetchone()
        # 429 是瞬时限流，不代表服务不可用：最近一次撞限流不应把端点踢出榜单，
        # 否则高频小额度服务会被永久排除（它只是刚好在探测时刻用完了窗口）。
        # 真正的失效状态仍然排除：鉴权失败 / 额度耗尽 / 模型下线 / 端点错误。
        if last["status"] in ("AUTH_FAILED", "QUOTA_EXHAUSTED",
                              "MODEL_GONE", "ENDPOINT_ERROR", "RESPONSE_INVALID"):
            result["reason"] = f"当前状态 {last['status']}"
            results.append(result)
            continue

        # ---- 硬过滤 4：额度耗尽 / 到期 ----
        # 两件事各取各的行，且都**排除速率行**（`quota_rules.DISPLAY_ROW_SQL`）：
        # 原先只按 `ORDER BY id DESC LIMIT 1` 取「最新一条」，而 HEADER 抓取每轮先写
        # 美元预算行、后写速率行，于是恒取到速率行——真预算耗尽会被 `remaining=19` 掩盖
        # （耗尽端点照样进榜），`expires_at` 也会被速率行的 NULL 顶掉（到期过滤失效）。
        # 分开取还顺带修掉「额度行与到期行不是同一条」时的相互遮蔽。
        exh = conn.execute(
            f"""SELECT remaining FROM quota_snapshot
                WHERE endpoint_id=? AND remaining IS NOT NULL
                  AND {quota_rules.DISPLAY_ROW_SQL}
                ORDER BY id DESC LIMIT 1""",
            (c["endpoint_id"],),
        ).fetchone()
        if exh and float(exh["remaining"]) <= 0:
            result["reason"] = "额度耗尽"
            results.append(result)
            continue

        exp = conn.execute(
            f"""SELECT expires_at FROM quota_snapshot
                WHERE endpoint_id=? AND expires_at IS NOT NULL
                  AND {quota_rules.DISPLAY_ROW_SQL}
                ORDER BY id DESC LIMIT 1""",
            (c["endpoint_id"],),
        ).fetchone()
        if exp and exp["expires_at"]:
            try:
                if as_local(datetime.fromisoformat(exp["expires_at"])) < now():
                    result["reason"] = "已到期"
                    results.append(result)
                    continue
            except Exception:
                pass

        # ---- 打分 ----
        avail, n = _availability(rows)
        ttfts = [r["ttft_ms"] for r in rows
                 if r["ttft_ms"] and r["stream_mode"] == "true_stream"]
        tpss = [r["tps"] for r in rows if r["tps"]]
        totals = [r["total_ms"] for r in rows if r["total_ms"]]

        p50_ttft = _pct(ttfts, 0.5)
        p50_tps = _pct(tpss, 0.5)
        p50_total = _pct(totals, 0.5)
        p95_total = _pct(totals, 0.95)

        # 稳定性：P95/P50 越接近 1 越稳
        stability = None
        if p50_total and p95_total and p50_total > 0:
            stability = max(0.0, min(1.0, 1 - (p95_total / p50_total - 1)))

        # 归一化成 0-1 分项（用经验上限，避免单点极值主导）
        parts = {}
        if avail is not None:
            parts["availability"] = avail
        if p50_ttft is not None:
            parts["ttft"] = max(0.0, min(1.0, 1 - (p50_ttft - 800) / 12000))
        if p50_tps is not None:
            parts["tps"] = max(0.0, min(1.0, p50_tps / 60))
        if stability is not None:
            parts["stability"] = stability
        parts["quota"] = _quota_factor(conn, c["endpoint_id"])

        # 权重重分配：只在有值的分项之间分配
        active = {k: weights[k] for k in parts if k in weights}
        wsum = sum(active.values())
        if wsum <= 0:
            result["reason"] = "无可评分项"
            results.append(result)
            continue
        score = sum(parts[k] * (w / wsum) for k, w in active.items())

        result.update(
            eligible=True,
            reason="",
            score=round(score * 100, 2),
            detail={
                "parts": {k: round(v, 3) for k, v in parts.items()},
                "weights_used": {k: round(w / wsum, 3) for k, w in active.items()},
                "missing": sorted(set(weights) - set(parts)),
                "n": n,
                "p50_ttft": round(p50_ttft, 1) if p50_ttft else None,
                "p50_tps": round(p50_tps, 2) if p50_tps else None,
                "p50_total": round(p50_total, 1) if p50_total else None,
                "p95_total": round(p95_total, 1) if p95_total else None,
            },
        )
        results.append(result)

    results.sort(key=lambda r: (not r["eligible"], -(r["score"] or 0)))

    if persist:
        conn.execute("DELETE FROM rank_snapshot WHERE scene=?", (scene,))
        for r in results:
            conn.execute(
                """INSERT INTO rank_snapshot(scene,endpoint_id,model_id,score,detail,
                                             eligible,reason)
                   VALUES(?,?,?,?,?,?,?)""",
                (scene, r["endpoint_id"], r["model_id"], r["score"],
                 json.dumps(r["detail"], ensure_ascii=False),
                 1 if r["eligible"] else 0, r["reason"]),
            )
        conn.commit()
    return results


def print_board(results: list[dict], scene: str) -> None:
    print(f"=== 榜单：{scene} ===")
    print(f"{'#':>2s} {'endpoint':13s} {'model':30s} {'得分':>6s} {'nttft':>7s} "
          f"{'ntps':>6s} {'可用':>5s} {'样本':>4s}")
    print("-" * 92)
    i = 0
    for r in results:
        if not r["eligible"]:
            continue
        i += 1
        d = r["detail"]
        parts = d.get("parts", {})
        print(f"{i:>2d} {r['endpoint']:13s} {r['model_id'][:30]:30s} "
              f"{r['score']:>6.2f} {parts.get('ttft',0):>7.2f} "
              f"{parts.get('tps',0):>6.2f} {parts.get('availability',0):>5.2f} "
              f"{d.get('n',0):>4d}")
    miss = [r for r in results if r["eligible"] and r["detail"].get("missing")]
    if miss:
        print(f"\n注：{len(miss)} 个合格项存在缺失分项，已按剩余项重分配权重："
              f"{miss[0]['detail']['missing']}")
    excluded = [r for r in results if not r["eligible"]]
    if excluded:
        print(f"未入榜 {len(excluded)} 项：")
        for r in excluded[:6]:
            print(f"  - {r['endpoint']}/{r['model_id']}: {r['reason']}")
