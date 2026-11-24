"""响应头到额度字段的映射规则（按服务商声明）。

为什么单独抽出来：B 档（响应头可推）每家的头名都不一样，
AMD 是 `x-ratelimit-limit-user-daily-usd`，换一家可能完全不同。
把映射写成数据而不是 if/else，接第 4、5 家时只加一条配置。

`quota_type` 词表（全项目统一，勿再造新值）：
  DAILY / WEEKLY / MONTHLY / TOTAL  可消耗的时间窗额度
  SUBSCRIPTION                      用户录入的**订阅类权益**（月付套餐等），是真额度
  RATE_LIMIT                        机器抓来的**速率窗**（rpm/rps），**不是额度**，展示与闸门都排除
"""

from __future__ import annotations

import datetime as _dt
import json

# 按服务商名匹配；匹配不到的走 _GENERIC
RULES: dict[str, list[dict]] = {
    "AMD Radeon (官方直连)": [
        {
            "quota_type": "DAILY", "unit": "USD",
            "limit": "x-ratelimit-limit-user-daily-usd",
            "used": "x-ratelimit-used-user-daily-usd",
            "remaining": "x-ratelimit-remaining-user-daily-usd",
            "reset": "x-ratelimit-reset-user-daily-usd",
            "reset_unit": "epoch", "scope": "ACCOUNT",
        },
        {
            "quota_type": "RATE_LIMIT", "unit": "REQUEST",
            "limit": "x-ratelimit-limit-user-rpm",
            "used": None,
            "remaining": "x-ratelimit-remaining-user-rpm",
            "reset": None, "reset_unit": None, "scope": "ACCOUNT",
        },
    ],
}

# 通用兜底：只认最常见的 x-ratelimit-* 形态，避免为每家硬编码
_GENERIC = [
    {
        "quota_type": "RATE_LIMIT", "unit": "REQUEST",
        "limit": "x-ratelimit-limit-requests",
        "used": None,
        "remaining": "x-ratelimit-remaining-requests",
        "reset": "x-ratelimit-reset-requests",
        "reset_unit": "epoch", "scope": "ACCOUNT",
    },
    {
        "quota_type": "DAILY", "unit": "TOKEN",
        "limit": "x-ratelimit-limit-tokens",
        "used": None,
        "remaining": "x-ratelimit-remaining-tokens",
        "reset": "x-ratelimit-reset-tokens",
        "reset_unit": "epoch", "scope": "ACCOUNT",
    },
]


def rules_for(provider: str | None) -> list[dict]:
    if provider and provider in RULES:
        return RULES[provider]
    return _GENERIC


# 「额度行」判定（SQL 片段，集中在此，避免各消费点口径漂移）：
#
# 首先要排除 `quota_type='RATE_LIMIT'` 的**速率限制行**——它由
# `x-ratelimit-limit-user-rpm` / `x-ratelimit-limit-requests` 写入，单位是
# REQUEST（每分钟请求数），衡量的是**速率**、不是可消耗额度，而且通常没有
# `reset_at`。它**也可能带 remaining**（`x-ratelimit-remaining-user-rpm`），
# 所以光靠 `remaining IS NOT NULL` 拦不住它。不排除会出两个错：
#   1. 接口清单额度列显示「19/20 次」而不是当天的美元预算，且没有重置时间——
#      实测 AMD 端点每次抓取都先写 DAILY/USD 预算行、再写速率行，于是「最新一条」
#      恒为速率行，真正的预算行被顶掉（用户投诉的「重置时间不对」即源于此）；
#   2. 额度闸门按速率触发——「每分钟 20 次用掉 19 次」会被算成 95% 已用而误降级，
#      真正的当日预算可能才用了 0.1%；排名 `_quota_factor` 同理被虚低。
#
# 为什么单独立 RATE_LIMIT、而不是继续借 `SUBSCRIPTION`：`SUBSCRIPTION` 是
# **用户语义**的「订阅类权益」（手工录入的月付套餐：1000 次/月、20 美元/月…），
# 是**真额度**、必须参与额度列与闸门；机器抓来的 rpm 窗则**不是额度**。两者
# 之前挤在同一个 `SUBSCRIPTION` 下，导致「凡是 SUBSCRIPTION 就排除」会误伤用户
# 手工填的订阅额度。分开后口径才自洽：`RATE_LIMIT`=机器抓的速率窗（排除），
#  `SUBSCRIPTION`=用户录的订阅权益（保留）。
#
# 分两档口径，用途不同：
#   BUDGET_ROW_SQL  预算口径——额度闸门 `budget.quota_used_pct` 与排名
#                   `rank._quota_factor` 用。要算「已用比例」，所以必须有 total>0
#                   且有 remaining；顺带保证一条只录了总额的行不会遮蔽更早的有效行。
#   DISPLAY_ROW_SQL 展示口径——接口清单 `api.endpoints()` 的 quota 字段用。只要不是
#                   速率行就能上屏，哪怕只录了总额（手工录入的 remaining 允许留空），
#                   免得用户填过的数字从界面上消失。
#
# 两者都**不看 unit**：手工录入的「1000 次/月」这类**请求数预算**（unit=REQUEST、
# quota_type=SUBSCRIPTION/TOTAL/DAILY…）是合法额度，必须保留——速率的判别特征
# 在 quota_type，不在 unit。
BUDGET_ROW_SQL = ("total IS NOT NULL AND total > 0 AND remaining IS NOT NULL "
                  "AND (quota_type IS NULL OR quota_type <> 'RATE_LIMIT')")
DISPLAY_ROW_SQL = "(quota_type IS NULL OR quota_type <> 'RATE_LIMIT')"


def local_naive_from_epoch(raw) -> str | None:
    """epoch 秒 -> **本地**朴素串（`YYYY-MM-DD HH:MM:SS`），与库中
    `datetime('now','localtime')` 同口径。

    为什么强调本地：`quota_snapshot.reset_at` 会被前端**原样**展示（不再做时区
    换算）。若这里用 UTC 落库，用户看到的「重置时间」就会晚 8 小时。
    换算不出来（非数字、越界）返回 None，宁缺勿错。
    """
    try:
        return (_dt.datetime.fromtimestamp(float(raw))
                .strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def reset_at_from_timezone(tz_name: str | None,
                           now_local: _dt.datetime | None = None) -> str | None:
    """`daily_reset_timezone`（如 `Asia/Shanghai`）-> 下一个当地零点的**本地**时刻。

    服务商在 `/usage` 里给的常是**时区名**而不是时刻，语义是「每天在此时区的
    00:00 重置」。库里统一存本机本地时间，故取该时区「下一个 00:00」后换算成
    本机本地时刻落库。时区名空/无法识别时返回 None（不猜、不回落成本地时区）。
    """
    name = str(tz_name).strip() if tz_name else ""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(name)
    except Exception:
        return None
    # now_local 是 naive 本地时间；先当作本地挂上时区，再换算到目标时区。
    base = now_local or _dt.datetime.now()
    in_zone = base.astimezone().astimezone(zone)
    nxt = (in_zone + _dt.timedelta(days=1)).replace(hour=0, minute=0,
                                                    second=0, microsecond=0)
    return nxt.astimezone().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def parse(headers_json: str | None, provider: str | None = None) -> list[dict]:
    """把响应头解析成额度条目。没有匹配的返回空列表。"""
    if not headers_json:
        return []
    try:
        h = {k.lower(): v for k, v in json.loads(headers_json).items()}
    except Exception:
        return []

    def num(key):
        if not key:
            return None
        try:
            return float(h[key])
        except Exception:
            return None

    out = []
    for r in rules_for(provider):
        limit = num(r.get("limit"))
        if limit is None:
            continue
        reset_at = None
        raw_reset = h.get(r.get("reset") or "")
        if raw_reset and r.get("reset_unit") == "epoch":
            reset_at = local_naive_from_epoch(raw_reset)
        out.append({
            "quota_type": r["quota_type"], "unit": r["unit"],
            "total": limit,
            "used": num(r.get("used")),
            "remaining": num(r.get("remaining")),
            "reset_at": reset_at,
            "scope": r.get("scope", "UNKNOWN"),
        })
    return out
