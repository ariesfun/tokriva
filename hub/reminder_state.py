"""提醒状态机：待确认 → 持续告警 → 恢复中。

设计依据（快速确认、提醒中心联动）。

为什么需要它：扫描出来的提醒是"当下这一眼"的结果，一次网络抖动和一次
真实故障长得一模一样。如果拿到就报警，用户会被误报淹没；如果一次恢复
就销号，用户又会看到"故障—恢复—故障"反复弹出。于是给每条提醒加一个
生命周期：

    首次出现 ──► 待确认 PENDING ──连续命中 N 次──► 持续告警 ALERTING
                    │                                   │
                    │（未确认就消失：本来就是抖动，销号）    │（消失一次）
                    ▼                                   ▼
                  销号                            恢复中 RECOVERING
                                                        │
                                        连续消失 M 次 ──► 销号

判定输入只有一件事：**本轮扫描里这条提醒还在不在**（在 = 命中，不在 = 归零）。
所以本模块不查库、不重算阈值，只消费 ledger.scan_reminders() 的产出，
与 status_engine 的分层判定不重叠——那边答"接口现在是什么状态"，
这边答"这条提醒闹了多久、要不要惊动用户"。

**"一轮"指一次探测，不是一次接口调用。** 页面刷新、前端轮询、手点"重新扫描"
都会打到本模块；如果拿调用次数当轮次，用户刷新两次页面就能把一次网络抖动
"确认"成故障，恢复中的提醒也会被刷页面刷到销号。所以调用方传入 round_id
（当前轮探测的批次标识）：同一批次重复读取只回显、不推进计数。
唯一例外是这一批里**新出现**的提醒——到期/额度这类是随时间自己冒出来的，
不是探测产物，出现就得立刻入账，否则要等到下一轮探测才被看见。

两类提醒的区别：AUTH_FAILED / UNREACHABLE 来自探测，会抖，需要待确认期；
到期、额度、未接入、模型下线是确定性事实，首见即确认，不走待确认期
（见 _CONFIRM_N_BY_KIND）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "reminder_state.json"

# 状态常量
PENDING = "PENDING"        # 待确认：出现次数还不够，不惊动用户
ALERTING = "ALERTING"      # 持续告警：已确认，需要处置
RECOVERING = "RECOVERING"  # 恢复中：已好转，观察若干轮后再销号

LABELS = {PENDING: "待确认", ALERTING: "持续告警", RECOVERING: "恢复中"}

# 状态字典里额外存一个"当前探测批次"的标记；提醒键是 "kind|endpoint"，
# 不会与它撞名。读的时候要跳过它。
_ROUND_KEY = "__round__"

# 阈值初始值（设计文档 7.2：N、M 都是初始建议值，可在设置里调）
CONFIRM_N_DEFAULT = 2   # 连续命中几次才算确认故障
RECOVER_M_DEFAULT = 2   # 连续几轮正常才算恢复

# 确定性事实首见即确认；只有探测得来的信号才需要待确认期
_CONFIRM_N_BY_KIND = {
    "EXPIRING": 1, "EXPIRED": 1, "QUOTA_LOW": 1,
    "NOT_CONFIGURED": 1, "MODEL_GONE": 1,
}


def load_state() -> dict:
    """读状态文件；损坏或不存在都当作空状态，不抛异常。"""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    """落盘。失败只吞掉——状态机坏了不该让提醒接口跟着 500。"""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8")
    except Exception:
        pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(since: str | None) -> datetime | None:
    """把记录里的 ISO 时间读成带时区的 datetime；读不出来返回 None。"""
    if not since:
        return None
    try:
        dt = datetime.fromisoformat(since)
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _minutes_since(since: str | None, now: datetime) -> int | None:
    dt = _parse(since)
    return None if dt is None else int((now - dt).total_seconds() // 60)


def _human_span(minutes: int | None) -> str:
    """把时长说成人话：90 → 「1.5 小时」，3000 → 「2.1 天」。"""
    if minutes is None:
        return "未知时长"
    if minutes < 60:
        return f"{max(1, minutes)} 分钟"
    if minutes < 60 * 48:
        return f"{minutes / 60:.1f} 小时"
    return f"{minutes / 1440:.1f} 天"


def _public(rec: dict, now: datetime) -> dict:
    """把内部记录压成对外的状态字段，附加到提醒条目上。"""
    return {
        "state": rec.get("state"),
        "state_label": LABELS.get(rec.get("state"), rec.get("state")),
        "state_since": rec.get("since"),
        "state_min": _minutes_since(rec.get("since"), now),
        "first_seen": rec.get("first_seen"),
        "age_min": _minutes_since(rec.get("first_seen"), now),
        "seen": int(rec.get("seen", 0)),
        "clear": int(rec.get("clear", 0)),
        "flaps": int(rec.get("flaps", 0)),
    }


def advance(items: list[dict], *, state: dict, now: datetime | None = None,
            confirm_n: int = CONFIRM_N_DEFAULT,
            recover_m: int = RECOVER_M_DEFAULT,
            round_id: str | None = None):
    """把本轮扫描推进一格，就地更新 state。

    参数
    ----
    items      : ledger.scan_reminders() 的原始产出（未经静音过滤——
                 被静音的提醒仍在持续观察，否则静音到期后会被当成新问题重新报一遍）
    state      : load_state() 读进来的字典，会被就地修改
    confirm_n  : 连续命中几次算确认（信号类提醒用）
    recover_m  : 连续几轮正常算恢复
    round_id   : 当前探测批次标识。与上次相同 = 同一批数据被重复读取
                 （页面刷新 / 前端轮询 / 手点重新扫描），此时只回显状态、
                 不推进任何计数；传 None 则退化为"每次调用算一轮"。

    返回
    ----
    (advance 后的 state, enriched, transitions, summary)
        enriched    : 与 items 等长，每条附加状态字段；另有仍在恢复中的
                      已消失条目（state=RECOVERING），放在列表末尾
        transitions : 本轮发生的状态翻转，形如
                      {"key","kind","endpoint","from","to","to_label","at","reason"}
                      from / to 为 None 表示"新出现" / "销号"
        summary     : {"pending","alerting","recovering"} 计数，供界面与角标使用
    """
    now = now or datetime.now(timezone.utc)

    # 同一批次内重复调用：探测数据没变，计数就不该动
    same_round = bool(round_id) and state.get(_ROUND_KEY) == round_id
    if round_id:
        state[_ROUND_KEY] = round_id

    seen: set[str] = set()
    enriched: list[dict] = []
    transitions: list[dict] = []

    # ---- 第一遍：本轮扫描里还在的提醒，命中次数 +1 ----
    for it in items:
        kind, endpoint = it.get("kind"), it.get("endpoint")
        if not kind or not endpoint:
            continue
        key = f"{kind}|{endpoint}"
        if key in seen:                       # 同键重复（理论上不会）只算一次
            continue
        seen.add(key)

        prev_rec = state.get(key)
        if same_round and prev_rec is not None:
            # 同一批数据再看一眼：原样回显，不推进（刷新页面不算新命中）
            enriched.append({**it, "key": key, **_public(prev_rec, now)})
            continue

        rec = dict(prev_rec or {})
        prev = rec.get("state")
        hits = int(rec.get("seen", 0)) + 1
        need = _CONFIRM_N_BY_KIND.get(kind, max(1, int(confirm_n)))

        if prev == RECOVERING:
            # 恢复期间又坏了：退回告警。flaps 记一次反复，界面可提示"反复"
            new = ALERTING
            rec["flaps"] = int(rec.get("flaps", 0)) + 1
            reason = "恢复期间再次出现"
        elif prev == ALERTING:
            new, reason = ALERTING, "持续出现"
        else:                                  # 首次出现，或还在待确认期
            new = ALERTING if hits >= need else PENDING
            reason = "出现次数达到确认阈值" if new == ALERTING else "首次出现，观察中"

        if new != prev:
            rec["since"] = _iso(now)
            transitions.append({
                "key": key, "kind": kind, "endpoint": endpoint,
                "from": prev, "to": new, "to_label": LABELS.get(new, new),
                "at": rec["since"], "reason": reason,
            })
        rec.setdefault("first_seen", _iso(now))
        rec.update({"state": new, "kind": kind, "endpoint": endpoint,
                    "subject": it.get("subject", ""), "detail": it.get("detail", ""),
                    "seen": hits, "clear": 0, "last_seen": _iso(now)})
        state[key] = rec
        enriched.append({**it, "key": key, **_public(rec, now)})

    # ---- 第二遍：本轮扫描里消失的提醒，正常轮次 +1 ----
    stale = [k for k in state.keys() if k != _ROUND_KEY and k not in seen]
    for key in stale:
        rec = state[key]
        kind, endpoint = rec.get("kind", ""), rec.get("endpoint", "")

        if same_round:
            # 同一批数据再看一眼：不推进任何计数。但"恢复中"的条目本来就
            # 不在扫描结果里，得继续回显，否则刷个页面它就从列表上消失了。
            if rec.get("state") == RECOVERING:
                enriched.append({
                    "kind": kind, "endpoint": endpoint,
                    "subject": rec.get("subject", ""),
                    "detail": f"最近一轮已恢复正常（连续 {rec['clear']} 轮）",
                    "key": key, **_public(rec, now),
                })
            continue

        prev = rec.get("state")
        clears = int(rec.get("clear", 0)) + 1

        if prev == ALERTING:
            rec["state"] = RECOVERING
            rec["since"] = _iso(now)
            rec["clear"], rec["seen"] = clears, 0
            transitions.append({
                "key": key, "kind": kind, "endpoint": endpoint,
                "from": ALERTING, "to": RECOVERING, "to_label": LABELS[RECOVERING],
                "at": rec["since"], "reason": "本轮已恢复正常，观察中",
            })
        elif prev == RECOVERING:
            rec["clear"] = clears
            if clears >= max(1, int(recover_m)):
                transitions.append({
                    "key": key, "kind": kind, "endpoint": endpoint,
                    "from": RECOVERING, "to": None, "to_label": "已恢复",
                    "at": _iso(now), "reason": f"连续 {clears} 轮正常，销号",
                })
                del state[key]
                continue
        elif prev == PENDING:
            # 待确认期里就消失了：本来就是一次抖动，静默销号
            del state[key]
            continue
        else:                                   # 状态缺失的残留记录
            del state[key]
            continue

        # 仅"恢复中"需要继续出现在列表里，让用户看到"正在恢复"
        if rec.get("state") == RECOVERING:
            enriched.append({
                "kind": kind, "endpoint": endpoint,
                "subject": rec.get("subject", ""),
                "detail": f"最近一轮已恢复正常（连续 {rec['clear']} 轮）",
                "key": key, **_public(rec, now),
            })

    summary = {"pending": 0, "alerting": 0, "recovering": 0}
    for key, rec in state.items():
        if key == _ROUND_KEY:
            continue
        s = (rec.get("state") or "").lower()
        if s in summary:
            summary[s] += 1
    return state, enriched, transitions, summary


def due_repeats(state: dict, transitions: list[dict], *,
                now: datetime | None = None, repeat_hours: float = 0.0):
    """仍在告警、且距上次通知已超过间隔的提醒——到期重发。

    为什么需要它：状态机只在**翻转**时通知，所以一个故障一直挂着就只发过最初
    那一条。用户很可能已经翻过去了，而问题还在。重发不是噪音：它说明"这件事
    还没人管"，和"刚刚坏了"是两种不同的信息。

    与 transitions 的区别：那是**发生了变化**，这是**一直没变化**。所以单独
    返回，条目标 `repeat: True`，形状与 transitions 一致以便通知层原样发送。

    计时口径
    --------
    - 本轮刚翻转成 ALERTING 的：马上要发一条，把计时起点记为现在；
    - 其余仍告警的：`last_notified` 距现在超过 `repeat_hours` 才算到期；
    - 从未记过（如本功能上线前就已经在告警）：以**状态开始时间** `since`
      为起点，于是"已经响了很久"的故障会立刻补一条，而不是永远不响；
    - 非 ALERTING 的（恢复中 / 待确认）：清掉计时，再次告警时重新开始。

    `repeat_hours <= 0` 表示关闭重发：返回空列表，且**不维护计时**。于是
    重新打开后，那些"关闭期间一直坏着"的故障会以 `since` 为起点立刻补一条，
    而不是要再等满一个间隔——与"老故障立刻补一条"是同一个口径。

    Returns:
        (就地更新过的 state, repeats)
    """
    now = now or datetime.now(timezone.utc)
    interval = (timedelta(hours=float(repeat_hours))
                if repeat_hours and float(repeat_hours) > 0 else None)

    just_alerting = {t["key"] for t in transitions
                     if t.get("key") and t.get("to") == ALERTING}

    repeats: list[dict] = []
    for key, rec in state.items():
        if key == _ROUND_KEY:
            continue
        if rec.get("state") != ALERTING or interval is None:
            rec.pop("last_notified", None)
            continue
        if key in just_alerting:
            rec["last_notified"] = _iso(now)
            continue

        last = _parse(rec.get("last_notified")) or _parse(rec.get("since"))
        if last is None or now - last < interval:
            # 记不下来时间就先把起点定在现在，免得每轮都当作到期而刷屏
            if last is None:
                rec["last_notified"] = _iso(now)
            continue

        rec["last_notified"] = _iso(now)
        span = _human_span(_minutes_since(rec.get("since"), now))
        repeats.append({
            "key": key, "kind": rec.get("kind"), "endpoint": rec.get("endpoint"),
            "subject": rec.get("subject", ""), "detail": rec.get("detail", ""),
            "from": ALERTING, "to": ALERTING, "to_label": LABELS[ALERTING],
            "at": _iso(now), "repeat": True,
            "reason": f"已持续 {span}，仍未恢复",
        })
    return state, repeats


def observe(items: list[dict], *, confirm_n: int = CONFIRM_N_DEFAULT,
            recover_m: int = RECOVER_M_DEFAULT, round_id: str | None = None,
            repeat_hours: float = 0.0, now: datetime | None = None):
    """load → advance → due_repeats → save 的便捷封装，供接口层一句调用。

    Returns:
        (enriched, transitions, summary, repeats)
    """
    state = load_state()
    state, enriched, transitions, summary = advance(
        items, state=state, confirm_n=confirm_n, recover_m=recover_m,
        round_id=round_id, now=now)
    state, repeats = due_repeats(state, transitions, now=now,
                                 repeat_hours=repeat_hours)
    save_state(state)
    return enriched, transitions, summary, repeats
