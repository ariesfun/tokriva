"""探测预算：限制单端点每天「消耗额度」的探测次数，避免探针把免费额度吃光。

设计依据（探测分级、预算与熔断）：

- L0（`GET /models`）通常不消耗额度 → 不计入预算；预算耗尽后仍继续跑，
  这正好实现设计里那句「额度低于阈值时**自动降级到 L0**」。
- L2（流式生成，`max_tokens≈256`）吃额度 → 计入预算。

于是「预算即分级频率」：便宜的连通检查每轮照跑，昂贵的速度测试被每日次数
封顶。不需要另加一套调度，就得到了设计里要求的「分级频率」——高频的是
不耗额度的 L0，低频的是耗额度的 L2。

降级是两级的：用到 `guard_pct` 先把每轮模型数降到 1（保住主模型、砍掉探索），
真正到顶才完全停 L2。这样额度紧张时是先降级、再停测，接口不会突然从监控里消失。
"""
from __future__ import annotations

from . import quota_rules

DEFAULT_BUDGET = 200
DEFAULT_GUARD_PCT = 90
GUARD_PCT_RANGE = (1, 100)

STATE_OK = "ok"
STATE_GUARDED = "guarded"
STATE_EXHAUSTED = "exhausted"
STATE_OFF = "off"

STATE_LABEL = {
    STATE_OK: "正常",
    STATE_GUARDED: "接近上限",
    STATE_EXHAUSTED: "已用尽",
    STATE_OFF: "未启用",
}

# 三态严重度，用于「两道闸门取更严的那个」（见 decision）。
_SEVERITY = {STATE_OK: 0, STATE_GUARDED: 1, STATE_EXHAUSTED: 2}

# 当日口径：库中 created_at 已是本机本地时间，取它的日期即可；右端同样取
# 本机「今天」（`date('now','localtime')`）。注意右端不能写 `date('now')`——
# 那是 UTC 日期，在本地凌晨 0–8 点会跟本地日期差一天。
_DAY_MATCH = "date(created_at)=date('now','localtime')"


def _as_pct(v):
    """把「已用百分比」收敛到 0–100；无法解析返回 None（= 未知，让位给别的闸门）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return max(0.0, min(100.0, f))


def _pick(obj, key, default=None):
    """从 dict 或 sqlite3.Row 取值。

    Row 没有 .get()，只有下标访问；接口层已经 dict 化，但探针链路直接传
    Row 的写法很容易再出现，这里统一兜底，省得调用方各自判断类型。
    """
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return obj[key]
    except (KeyError, IndexError, TypeError):
        return default


def clamp_guard_pct(v) -> int:
    """降级阈值收敛到 1–100；非法输入退回默认。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_GUARD_PCT
    return max(GUARD_PCT_RANGE[0], min(GUARD_PCT_RANGE[1], n))


def used_today(conn, endpoint_id: int) -> int:
    """该端点当日已消耗额度的探测次数。

    只算 L2：L0 通常不消耗额度，不该被预算挡住（否则就成了「预算用尽连
    连通性都不测」，与设计里「降级到 L0」相反）。
    """
    row = conn.execute(
        "SELECT COUNT(*) n FROM probe_result WHERE endpoint_id=? AND level='L2' AND "
        + _DAY_MATCH, (endpoint_id,)).fetchone()
    if row is None:
        return 0
    # sqlite3.Row 支持按键取值；裸 cursor（未设 row_factory）返回元组，兜底取第 0 列
    try:
        n = row["n"]
    except (TypeError, KeyError, IndexError):
        n = row[0]
    try:
        return max(0, int(n or 0))
    except (TypeError, ValueError):
        return 0


def quota_used_pct(conn, endpoint_id: int):
    """该端点最近一次已知「账号额度」的已用百分比（0–100）；未知返回 None。

    这是第二道闸门：次数预算管「今天还能测几次」，这里管「账号的钱还剩多少」。
    免费额度有的按次数、有的按金额/token，只按次数封顶会漏掉后者。

    只认**额度预算行**（判定见 `quota_rules.BUDGET_ROW_SQL`）：有 total、有 remaining，
    且**不是 RATE_LIMIT 速率限制行**。速率行（`x-ratelimit-limit-user-rpm` →
    RATE_LIMIT/REQUEST，如「每分钟 20 次」）**也可能带 remaining**，若不排除，
    会把「每分钟 20 次用掉 19 次」误算成 95% 已用而触发误降级——它衡量的是速率，
    不是可消耗预算。取最近一条，保证用的是新鲜数据。
    """
    row = conn.execute(
        f"""SELECT total, used, remaining FROM quota_snapshot
           WHERE endpoint_id=? AND {quota_rules.BUDGET_ROW_SQL}
           ORDER BY id DESC LIMIT 1""",
        (endpoint_id,)).fetchone()
    if row is None:
        return None
    try:
        total = float(_pick(row, "total"))
        remaining = float(_pick(row, "remaining"))
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    used = _pick(row, "used")
    try:
        used = float(used) if used is not None else total - remaining
    except (TypeError, ValueError):
        used = total - remaining
    return _as_pct(used / total * 100.0)


def decision(budget, used, guard_pct: int = DEFAULT_GUARD_PCT,
             quota_used_pct=None) -> dict:
    """给出本轮的探测许可：两道闸门取更严的那个。

    闸门一（次数）：当日 L2 次数达到 daily_budget 即停。
    闸门二（额度）：账号额度「已用百分比」达到阈值即降级、用尽即停；额度未知
    时自动让位，只按次数约束。（免费额度有的按次数、有的按金额/token，只封一个
    会漏掉另一种。）

    返回：
      state       ok / guarded / exhausted
      allow_l2    本轮是否还能做速度测试
      max_models  本轮最多测几个模型（None = 沿用默认上限；0 = 不测）
      reason      给日志/界面的一句话
    """
    try:
        b = max(1, int(budget))
    except (TypeError, ValueError):
        b = DEFAULT_BUDGET
    try:
        u = max(0, int(used))
    except (TypeError, ValueError):
        u = 0
    pct = clamp_guard_pct(guard_pct)
    # 阈值向下取整：宁可早一点开始降级，给突发留余量。
    # 不用 round()——它按银行家舍入（round(4.5)==4）会让阈值随预算大小前后不一致；
    # int() 对正数是截断，行为单一，且 pct<100 时阈值必然 < 预算，降级态一定可见。
    threshold = max(1, int(b * pct / 100))

    # 闸门一：次数
    if u >= b:
        c_state, c_reason = STATE_EXHAUSTED, f"今日探测次数已用尽（{u}/{b} 次）"
    elif u >= threshold:
        c_state, c_reason = STATE_GUARDED, f"今日探测次数接近上限（{u}/{b} 次）"
    else:
        c_state, c_reason = STATE_OK, ""

    # 闸门二：账号额度余额（未知则该闸门不参与）
    q_state, q_reason = STATE_OK, ""
    qp = _as_pct(quota_used_pct)
    if qp is not None:
        if qp >= 100:
            q_state, q_reason = STATE_EXHAUSTED, f"账号额度已用尽（已用 {qp:.0f}%）"
        elif qp >= pct:
            q_state, q_reason = STATE_GUARDED, f"账号额度接近上限（已用 {qp:.0f}%）"

    if _SEVERITY[c_state] >= _SEVERITY[q_state]:
        state, reason = c_state, c_reason
    else:
        state, reason = q_state, q_reason

    if state == STATE_EXHAUSTED:
        return {"state": state, "allow_l2": False, "max_models": 0, "reason": reason}
    if state == STATE_GUARDED:
        return {"state": state, "allow_l2": True, "max_models": 1, "reason": reason}
    return {"state": STATE_OK, "allow_l2": True, "max_models": None, "reason": ""}


def statuses(conn, endpoints, guard_pct: int = DEFAULT_GUARD_PCT) -> dict:
    """批量算「当日用量 / 状态」，供接口与界面展示。

    endpoints: 可迭代的映射序列，至少要有 id、label、daily_budget。
    返回 {label: {budget, used, remaining, state, quota_used_pct, reason}}。
    """
    out: dict = {}
    pct = clamp_guard_pct(guard_pct)
    for ep in endpoints:
        label = _pick(ep, "label")
        if not label:
            continue
        b = _pick(ep, "daily_budget") or DEFAULT_BUDGET
        eid = _pick(ep, "id")
        used = used_today(conn, eid)
        qp = quota_used_pct(conn, eid)
        dec = decision(b, used, pct, qp)
        out[label] = {
            "budget": int(b), "used": used,
            "remaining": max(0, int(b) - used),
            "state": dec["state"], "quota_used_pct": qp, "reason": dec["reason"],
        }
    return out
