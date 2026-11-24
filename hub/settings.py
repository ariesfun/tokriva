"""用户运行参数：测试问题、提醒阈值、通知渠道。存 data/settings.json，改动即时生效。

与 schedule.json（定时检测）分离：一个是"测什么"，一个是"什么时候测"。
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from .budget import clamp_guard_pct

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "data" / "settings.json"

DEFAULTS = {
    # 自定义测试问题：空 = 使用内置默认问题（Count from 1 to 20.）
    "probePrompt": "",
    # 提醒状态机阈值（设计文档 7.2）：连续命中 N 次才算确认故障，
    # 连续 M 轮正常才算恢复。只影响 AUTH_FAILED / UNREACHABLE 这类
    # 探测得来的信号；到期/额度/未接入是确定性事实，首见即确认。
    "reminderConfirmN": 2,
    "reminderRecoverM": 2,
    # 通知渠道：企业微信机器人 webhook 地址（空 = 只站内提醒，不往外发）。
    # 别的平台（飞书/钉钉/Slack）用一条转发规则接同一个 text 结构即可。
    "notifyWebhook": "",
    # 是否也推送"已恢复"。关掉就只在故障确认时打扰用户。
    "notifyRecover": True,
    # 故障长期不恢复时，每隔多少小时再提醒一次（0 = 不重发，只在确认时发一条）。
    # 状态机只在"翻转"时通知，所以故障一直挂着就只发过最初那一条——用户
    # 很可能已经翻过去了。重发不是噪音：它说明"这件事还没人管"。
    "notifyRepeatHours": 6,
    # 探测预算：限制单端点每天「消耗额度」的速度测试次数（L0 连通检查不受限）。
    # 免费层额度很低，探针不能把它吃光；关掉则不做任何限制。
    # 每个接口的具体额度在接口详情里单独设（endpoint.daily_budget）。
    "budgetEnabled": True,
    # 用到预算的这个百分比就开始降级（每轮只测 1 个模型），给突发留余量。
    "budgetGuardPct": 90,
}

# 自定义问题长度上限：探测请求体不能无限大，也避免把"写文章"当成速度测试
PROMPT_MAX_LEN = 500

# 状态机阈值取值范围：下限 1（等于关掉待确认期），上限 10（再多就没人看了）
CONFIRM_N_RANGE = (1, 10)
RECOVER_M_RANGE = (1, 10)
# 重发间隔：0 = 关闭；上限 7 天（再长就没意义了——那时该去看状态页而不是等提醒）
REPEAT_HOURS_RANGE = (0, 168)

# webhook 只允许 http/https：避免 file:// 之类被当成推送目标
_WEBHOOK_SCHEMES = ("http", "https")


def load_settings() -> dict:
    out = dict(DEFAULTS)
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            out.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return out


def _clamp(v, rng, default):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(rng[0], min(rng[1], n))


def save_settings(patch: dict) -> dict:
    cur = load_settings()
    cur.update({k: v for k, v in patch.items() if k in DEFAULTS})
    prompt = str(cur.get("probePrompt") or "").strip()
    if len(prompt) > PROMPT_MAX_LEN:
        raise ValueError(f"测试问题最长 {PROMPT_MAX_LEN} 字")
    cur["probePrompt"] = prompt
    cur["reminderConfirmN"] = _clamp(cur.get("reminderConfirmN"),
                                     CONFIRM_N_RANGE, DEFAULTS["reminderConfirmN"])
    cur["reminderRecoverM"] = _clamp(cur.get("reminderRecoverM"),
                                     RECOVER_M_RANGE, DEFAULTS["reminderRecoverM"])
    cur["notifyWebhook"] = _clean_webhook(cur.get("notifyWebhook"))
    cur["notifyRecover"] = bool(cur.get("notifyRecover", DEFAULTS["notifyRecover"]))
    cur["notifyRepeatHours"] = _clamp(cur.get("notifyRepeatHours"),
                                      REPEAT_HOURS_RANGE,
                                      DEFAULTS["notifyRepeatHours"])
    cur["budgetEnabled"] = bool(cur.get("budgetEnabled", DEFAULTS["budgetEnabled"]))
    cur["budgetGuardPct"] = clamp_guard_pct(cur.get("budgetGuardPct"))
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return cur


def _clean_webhook(v) -> str:
    """规范化 webhook 地址；空串表示关闭通知，写了非法地址直接报错而不是静默丢弃。"""
    url = str(v or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in _WEBHOOK_SCHEMES or not parsed.netloc:
        raise ValueError("通知地址需为 http(s):// 开头的完整地址")
    return url


def notify_config() -> tuple[str, bool]:
    """返回 (webhook 地址, 是否推送恢复)；地址为空即通知关闭。"""
    s = load_settings()
    try:
        url = _clean_webhook(s.get("notifyWebhook"))
    except ValueError:
        url = ""
    return url, bool(s.get("notifyRecover", DEFAULTS["notifyRecover"]))


def mask_webhook(url: str) -> str:
    """回显用的脱敏地址：只留主机与路径，密钥段替换成 ***。

    机器人 webhook 的 key 就是凭证，和模型 API key 一样不该回显到页面上——
    页面只需让用户认出"配的是哪个群"，不需要能把它抄走。
    """
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.netloc:
        return "***"
    tail = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{tail}?***"


def notify_repeat_hours() -> int:
    """故障长期不恢复时每隔多少小时重发一次；0 = 不重发。"""
    s = load_settings()
    return _clamp(s.get("notifyRepeatHours"), REPEAT_HOURS_RANGE,
                  DEFAULTS["notifyRepeatHours"])


def reminder_tuning() -> tuple[int, int]:
    """返回 (连续命中几次确认, 连续几轮正常算恢复)，已做范围收敛。"""
    s = load_settings()
    return (_clamp(s.get("reminderConfirmN"), CONFIRM_N_RANGE, DEFAULTS["reminderConfirmN"]),
            _clamp(s.get("reminderRecoverM"), RECOVER_M_RANGE, DEFAULTS["reminderRecoverM"]))


def budget_config() -> tuple[bool, int]:
    """返回 (是否启用每日探测预算, 降级阈值百分比)。"""
    s = load_settings()
    return (bool(s.get("budgetEnabled", DEFAULTS["budgetEnabled"])),
            clamp_guard_pct(s.get("budgetGuardPct")))
