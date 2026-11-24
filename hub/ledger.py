"""台账与提醒：额度三档、门槛台账、到期与异常提醒。

诚实性原则（贯穿设计文档）：
- 额度必须带 source，API / HEADER / MANUAL / ESTIMATED 不可混排。
- 拿不到就是 UNKNOWN，不用默认值假装已知。
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta

from . import quota_rules
from .db import as_local, now

VALID_SCOPES = {"KEY", "PROJECT", "ORG", "ACCOUNT", "IP", "UNKNOWN"}
VALID_VIS = {"PUBLIC", "CONSOLE_ONLY", "UNPUBLISHED", "UNKNOWN"}
VALID_CONF = {"CONFIRMED", "LIKELY", "POSSIBLE", "UNKNOWN"}
VALID_SOURCE = {"API", "HEADER", "MANUAL", "ESTIMATED"}


def write_quota(conn, endpoint_id: int, *, quota_type: str, unit: str,
                total=None, used=None, remaining=None, source: str = "MANUAL",
                scope: str = "UNKNOWN", visibility: str = "UNKNOWN",
                confidence: str = "UNKNOWN", reset_at=None, expires_at=None) -> None:
    """写入一条额度快照，带作用域与来源。

    quota_scope 决定「同一个项目里再建几个 Key 是否叠加额度」，
    这是设计文档第 4 节指出的关键缺口。
    """
    if source not in VALID_SOURCE:
        raise ValueError(f"source 非法: {source}")
    scope = scope if scope in VALID_SCOPES else "UNKNOWN"
    visibility = visibility if visibility in VALID_VIS else "UNKNOWN"
    confidence = confidence if confidence in VALID_CONF else "UNKNOWN"
    conn.execute(
        """INSERT INTO quota_snapshot(endpoint_id,quota_type,unit,total,used,remaining,
                                      source,quota_scope,quota_visibility,confidence,
                                      reset_at,expires_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (endpoint_id, quota_type, unit, total, used, remaining, source,
         scope, visibility, confidence, reset_at, expires_at),
    )
    conn.commit()


def set_manual_quota(conn, endpoint_label: str, *, quota_type: str = "TOTAL",
                     unit: str = "USD", total: float | None = None,
                     remaining: float | None = None,
                     expires_at: str | None = None,
                     note: str | None = None) -> None:
    """手工录入额度（C 档）。

    适用：服务商没有额度查询接口，或额度只在控制台可见。
    """
    row = conn.execute("SELECT id FROM endpoint WHERE label=?",
                       (endpoint_label,)).fetchone()
    if not row:
        raise ValueError(f"未找到端点: {endpoint_label}")
    write_quota(conn, row["id"], quota_type=quota_type, unit=unit,
                total=total, remaining=remaining, source="MANUAL",
                scope="UNKNOWN", visibility="MANUAL",
                confidence="CONFIRMED", expires_at=expires_at)
    if note:
        conn.execute("UPDATE endpoint SET note=? WHERE id=?", (note, row["id"]))
        conn.commit()


def upsert_catalog(conn, provider: str, **kw) -> None:
    """写入门槛台账。

    每条必须带来源与核实日期；dataset 导入的条目只能标 dataset，
    核实官方页面后才允许升级为 official。
    """
    row = conn.execute("SELECT id FROM provider WHERE name=?", (provider,)).fetchone()
    if not row:
        conn.execute("INSERT INTO provider(name,kind) VALUES(?,'model_api')", (provider,))
        row = conn.execute("SELECT id FROM provider WHERE name=?", (provider,)).fetchone()
    pid = row["id"]
    existing = conn.execute(
        "SELECT id FROM catalog_entry WHERE provider_id=?", (pid,)
    ).fetchone()
    fields = ["signup_method", "free_form", "rate_limits", "allow_proxy",
              "allow_commercial", "source_url", "source_type", "verified_at",
              "confidence", "quota_scope", "quota_visibility",
              "training_on_free_data", "note", "endpoint_label",
              "requires_phone", "requires_card", "requires_realname",
              "region_restricted", "requires_review"]
    vals = [kw.get(f) for f in fields]
    if existing:
        # 政策变更留痕：字段变化写历史，不覆盖旧值
        old = conn.execute(
            f"SELECT {','.join(fields)} FROM catalog_entry WHERE id=?",
            (existing["id"],)
        ).fetchone()
        for f, new in zip(fields, vals):
            if new is not None and old[f] is not None and str(old[f]) != str(new):
                conn.execute(
                    """INSERT INTO provider_policy_history
                       (provider_id,catalog_entry_id,field,old_value,new_value,source,confirmed_by)
                       VALUES(?,?,?,?,?,?,?)""",
                    (pid, existing["id"], f, str(old[f]), str(new),
                     kw.get("source_url"), "system"),
                )
        conn.execute(
            f"UPDATE catalog_entry SET {','.join(f+'=?' for f in fields)} WHERE id=?",
            (*vals, existing["id"]),
        )
    else:
        conn.execute(
            f"INSERT INTO catalog_entry(provider_id,{','.join(fields)}) "
            f"VALUES({','.join('?'*(len(fields)+1))})",
            (pid, *vals),
        )
    conn.commit()


def catalog_report(conn) -> None:
    print("=== 门槛台账 ===")
    print(f"{'provider':20s} {'注册':12s} {'免费形态':12s} {'允许转发':>8s} "
          f"{'来源类型':10s} {'核实日期':12s} {'置信度':10s}")
    print("-" * 100)
    rows = conn.execute(
        """SELECT p.name, c.signup_method, c.free_form, c.allow_proxy,
                  c.source_type, c.verified_at, c.confidence
           FROM catalog_entry c JOIN provider p ON p.id=c.provider_id
           ORDER BY p.name"""
    ).fetchall()
    if not rows:
        print("  （空，用 --catalog-import 或 upsert_catalog 录入）")
    for r in rows:
        print(f"{r['name'][:20]:20s} {str(r['signup_method'] or '-')[:12]:12s} "
              f"{str(r['free_form'] or '-')[:12]:12s} {str(r['allow_proxy'] or 'UNKNOWN'):>8s} "
              f"{str(r['source_type'] or '-'):10s} {str(r['verified_at'] or '-')[:10]:12s} "
              f"{str(r['confidence'] or 'UNKNOWN'):10s}")


def scan_reminders(conn, *, expire_days=(30, 7, 1), quota_low=0.2,
                   auth_fail_streak=3, persist: bool = True,
                   dedup_days: int = 1) -> list[dict]:
    """生成提醒。只产出，不发送。

    Args:
        persist: 是否把本轮提醒落进 `reminder_log`。
        dedup_days: 同一故障在多少天内只落一条日志；0 表示不去重。
            只影响落库，不影响返回的 `out`——该发的通知照发，静音由
            `reminder_state` 的生命周期管，两者不重叠。
    """
    out = []

    # 1) 到期提醒
    for r in conn.execute(
        """SELECT id,label,expires_at FROM endpoint
           WHERE active=1 AND expires_at IS NOT NULL"""
    ).fetchall():
        try:
            exp = datetime.fromisoformat(r["expires_at"])
        except Exception:
            continue
        left = (as_local(exp) - now()).days
        for d in expire_days:
            if 0 <= left <= d:
                out.append({"kind": "EXPIRING", "endpoint": r["label"],
                            "subject": f"{r['label']} 将于 {left} 天后到期",
                            "detail": r["expires_at"]})
                break
        if left < 0:
            out.append({"kind": "EXPIRED", "endpoint": r["label"],
                        "subject": f"{r['label']} 已过期 {abs(left)} 天",
                        "detail": r["expires_at"]})

    # 2) 额度低于阈值
    #    口径同 `budget`/`rank`，一律走 `BUDGET_ROW_SQL`（见 quota_rules）。
    #    这里**不能**只按「total 非空的最大 id」取——HEADER 抓取每轮先写美元预算行、
    #    后写速率行，于是「最新一条」恒为速率行（`RATE_LIMIT/REQUEST 19/20`）；
    #    结果真预算再低也轮不到它被评估，「额度不足」提醒会**静默漏发**。
    for r in conn.execute(
        f"""SELECT q.endpoint_id, e.label, q.remaining, q.total, q.unit
            FROM quota_snapshot q JOIN endpoint e ON e.id=q.endpoint_id
            WHERE q.id IN (SELECT MAX(id) FROM quota_snapshot
                           WHERE {quota_rules.BUDGET_ROW_SQL} GROUP BY endpoint_id)"""
    ).fetchall():
        try:
            ratio = float(r["remaining"]) / float(r["total"])
        except Exception:
            continue
        if ratio <= quota_low:
            out.append({"kind": "QUOTA_LOW", "endpoint": r["label"],
                        "subject": f"{r['label']} 额度剩余 {ratio:.0%}",
                        "detail": f"{r['remaining']}/{r['total']} {r['unit']}"})

    # 3) 连续鉴权失败
    for e in conn.execute("SELECT id,label FROM endpoint WHERE active=1").fetchall():
        recent = conn.execute(
            """SELECT status FROM probe_result WHERE endpoint_id=?
               ORDER BY id DESC LIMIT ?""",
            (e["id"], auth_fail_streak),
        ).fetchall()
        if len(recent) == auth_fail_streak and all(
                x["status"] == "AUTH_FAILED" for x in recent):
            out.append({"kind": "AUTH_FAILED", "endpoint": e["label"],
                        "subject": f"{e['label']} 连续 {auth_fail_streak} 次鉴权失败",
                        "detail": "Key 可能失效，请检查"})

    # 3.5) 连续不可用（端点级故障）：ENDPOINT_ERROR / NETWORK_ERROR / RESPONSE_INVALID
    # 与 AUTH_FAILED、限流分开：这类是服务或链路问题，处置方式不同
    _UNREACHABLE = {"ENDPOINT_ERROR", "NETWORK_ERROR", "RESPONSE_INVALID"}
    for e in conn.execute("SELECT id,label FROM endpoint WHERE active=1").fetchall():
        recent = conn.execute(
            """SELECT status FROM probe_result WHERE endpoint_id=?
               ORDER BY id DESC LIMIT ?""",
            (e["id"], auth_fail_streak),
        ).fetchall()
        if len(recent) == auth_fail_streak and all(
                x["status"] in _UNREACHABLE for x in recent):
            kinds = sorted({x["status"] for x in recent})
            out.append({"kind": "UNREACHABLE", "endpoint": e["label"],
                        "subject": f"{e['label']} 连续 {auth_fail_streak} 次不可用",
                        "detail": "；".join(kinds) + "，请检查服务状态或网络/代理"})

    # 3.6) 未接入（密钥未配置）：提醒去配置，而不是当故障
    for e in conn.execute("SELECT id,label,auth_env FROM endpoint WHERE active=1").fetchall():
        latest = conn.execute(
            "SELECT status FROM probe_result WHERE endpoint_id=? ORDER BY id DESC LIMIT 1",
            (e["id"],),
        ).fetchone()
        if latest and latest["status"] == "NOT_CONFIGURED":
            out.append({"kind": "NOT_CONFIGURED", "endpoint": e["label"],
                        "subject": f"{e['label']} 还没接入",
                        "detail": f"密钥未配置（服务端缺少环境变量），配置后即可开始测试"})

    # 4) 本次探测模型下线
    gone = conn.execute(
        """SELECT DISTINCT e.label, p.model_id FROM probe_result p
           JOIN endpoint e ON e.id=p.endpoint_id
           WHERE p.status='MODEL_GONE' AND p.created_at > datetime('now','localtime','-1 day')"""
    ).fetchall()
    for g in gone:
        out.append({"kind": "MODEL_GONE", "endpoint": g["label"],
                    "subject": f"{g['label']} 模型下线: {g['model_id']}", "detail": ""})

    if persist:
        # 同一故障（kind + 端点 + 文案）在 dedup 窗口内只落一条。
        # 常驻循环每 30 分钟一轮，持续故障会反复产出同一条提醒；不去重则
        # reminder_log 按轮数线性膨胀（约 48 行/天/个故障），也看不出"闹了多久"。
        win = f"-{max(0, int(dedup_days))} days"
        for r in out:
            conn.execute(
                "INSERT INTO reminder_log(kind,endpoint_id,subject,detail) "
                "SELECT ?,e.id,?,? FROM endpoint e WHERE e.label=? "
                "AND NOT EXISTS (SELECT 1 FROM reminder_log l "
                "WHERE l.kind=? AND l.endpoint_id=e.id AND l.subject=? "
                "AND l.created_at > datetime('now','localtime', ?))",
                (r["kind"], r["subject"], r["detail"], r["endpoint"],
                 r["kind"], r["subject"], win),
            )
        conn.commit()
    return out


def print_reminders(items: list[dict]) -> None:
    print("=== 提醒 ===")
    if not items:
        print("  无")
        return
    for r in items:
        print(f"  [{r['kind']:12s}] {r['subject']}")


def send_webhook(url: str, items: list[dict]) -> bool:
    """把提醒推到 webhook（企业微信/飞书等均可经此转发）。"""
    if not url or not items:
        return False
    payload = json.dumps({
        "msgtype": "text",
        "text": {"content": "Tokriva 提醒\n" + "\n".join(
            f"- [{i['kind']}] {i['subject']}" for i in items)},
    }).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 台账导出 / 导入（设计文档：配置与台账应可导出导入，能放 git）
# ---------------------------------------------------------------------------
CATALOG_FIELDS = ("signup_method", "free_form", "rate_limits", "allow_proxy",
                  "allow_commercial", "source_url", "source_type", "verified_at",
                  "confidence", "quota_scope", "quota_visibility",
                  "training_on_free_data", "note")


def export_catalog(conn) -> dict:
    """导出门槛台账为可入库的 JSON 结构。"""
    rows = conn.execute(
        f"""SELECT p.name provider, p.website, p.kind,
                   {','.join('c.' + f for f in CATALOG_FIELDS)}
            FROM catalog_entry c JOIN provider p ON p.id=c.provider_id
            ORDER BY p.name"""
    ).fetchall()
    return {
        "schema": "tokriva.catalog/1",
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "entries": [{k: r[k] for k in r.keys()} for r in rows],
    }


def import_catalog(conn, data: dict, *, only_if_missing: bool = False) -> dict:
    """导入门槛台账。字段变化会写政策历史，不静默覆盖。

    only_if_missing=True 时只补空缺，不动已有条目（适合首次从数据集导入）。
    """
    if data.get("schema") not in (None, "tokriva.catalog/1"):
        raise ValueError(f"不认识的结构: {data.get('schema')}")

    added = updated = skipped = 0
    for e in data.get("entries", []):
        name = e.get("provider")
        if not name:
            continue
        row = conn.execute("SELECT id FROM provider WHERE name=?", (name,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO provider(name,website,kind) VALUES(?,?,?)",
                (name, e.get("website"), e.get("kind") or "model_api"))
            row = conn.execute("SELECT id FROM provider WHERE name=?",
                               (name,)).fetchone()
        exists = conn.execute(
            "SELECT id FROM catalog_entry WHERE provider_id=?", (row["id"],)
        ).fetchone()
        if exists and only_if_missing:
            skipped += 1
            continue
        before = "INSERT" if not exists else "UPDATE"
        upsert_catalog(conn, name, **{k: e.get(k) for k in CATALOG_FIELDS})
        if before == "INSERT":
            added += 1
        else:
            updated += 1
    conn.commit()
    return {"added": added, "updated": updated, "skipped": skipped}


# ---------------------------------------------------------------------------
# 别名组（逻辑模型名 -> 场景），供下发复用同一口径
# ---------------------------------------------------------------------------
def set_alias(conn, alias: str, scene: str, members: list[dict] | None = None,
              note: str | None = None) -> None:
    conn.execute(
        """INSERT INTO alias_group(alias,scene,members,note,updated_at)
           VALUES(?,?,?,?,datetime('now','localtime'))
           ON CONFLICT(alias) DO UPDATE SET
             scene=excluded.scene, members=excluded.members,
             note=excluded.note, updated_at=datetime('now','localtime')""",
        (alias, scene, json.dumps(members or [], ensure_ascii=False), note),
    )
    conn.commit()


def resolve_alias_scene(conn, alias: str, default: str = "agent") -> str:
    row = conn.execute("SELECT scene FROM alias_group WHERE alias=?",
                       (alias,)).fetchone()
    return (row["scene"] if row and row["scene"] else default)


def list_aliases(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT alias,scene,note,updated_at FROM alias_group ORDER BY alias")]
