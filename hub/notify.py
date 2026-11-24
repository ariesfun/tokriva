"""提醒通知：把状态机的状态翻转推到用户自己配的渠道。

为什么需要它：提醒页只在打开时有用，而故障不会挑你盯着屏幕的时候发生。
状态机已经把"这次翻转值不值得惊动用户"判完了（待确认期的抖动根本不会
走到这里），这一层只负责在**确认故障**和**已恢复**两个时点上发出消息。

消息格式用企业微信机器人的 text 结构：飞书、钉钉、Slack 都能用一条中间
转发规则接上，自己写的接收端也只是读 `text.content`。不引入任何 SDK，
就是一次带超时的 POST——发不出去就记一笔，绝不往上抛，通知失败不该
把探测或页面拖垮。

发送时机由 hub/reminder_state.py 的批次机制保证"每轮翻转只发一次"：
同一批探测数据被重复读取（页面刷新、前端轮询）不会产生新的 transitions，
自然也不会重复发。
"""
from __future__ import annotations

import json
import urllib.request

# 目标端超时：宁可发不出去，也不让定时探测的线程卡在这里
TIMEOUT = 10

# 单条消息正文上限。企业微信 text 上限 2048 字节，留出富余给标题和
# 中文多字节，超了就截断并注明还剩几条
_TEXT_LIMIT = 1500

_SEP = "\n"


def _line(event: dict) -> str:
    """把一条状态翻转（或一次到期重发）写成一行给人看的话。"""
    endpoint = event.get("endpoint") or "未知接口"
    subject = (event.get("subject") or "").strip()
    to_label = event.get("to_label") or event.get("to") or ""
    reason = (event.get("reason") or "").strip()

    if event.get("to") is None:                    # 销号
        head = f"【已恢复】{endpoint} 不再报警"
    elif event.get("repeat"):                      # 一直没恢复，再喊一声
        # 用「仍未恢复」开头，和「刚刚坏了」区分开：用户扫一眼就知道
        # 这条不是新故障，而是同一件事还没人管
        head = f"【仍未恢复】{subject or endpoint}"
    else:                                          # 新出现 / 状态变化
        head = f"【{to_label}】{subject or endpoint}"

    tail = f"（{reason}）" if reason else ""
    return f"- {head}{tail}"


def build_content(events: list[dict]) -> str:
    """把若干条翻转拼成一条消息正文。"""
    body = _SEP.join(_line(e) for e in events)
    if len(body) > _TEXT_LIMIT:
        body = body[:_TEXT_LIMIT].rstrip() + f"{_SEP}…（其余已省略）"
    return f"Tokriva 提醒{_SEP}{body}" if body else "Tokriva 提醒"


def build_payload(events: list[dict]) -> dict:
    """企业微信机器人 text 消息体；别的平台用转发规则接。"""
    return {"msgtype": "text", "text": {"content": build_content(events)}}


def send_webhook(url: str, events: list[dict], *, timeout: int = TIMEOUT) -> bool:
    """POST 到 webhook。空地址或空事件直接算"没发"；任何异常都吞掉。"""
    if not url or not events:
        return False
    data = json.dumps(build_payload(events), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def dispatch(transitions: list[dict], *, url: str = "",
             notify_recover: bool = True, timeout: int = TIMEOUT) -> dict:
    """按设置挑出该发的翻转并发送。

    参数
    ----
    transitions    : reminder_state 产出的本轮翻转
    url            : webhook 地址；空 = 通知关闭
    notify_recover : 是否也推"已恢复"

    返回 {"sent": bool, "count": int, "skipped": int}
    """
    if not url:
        return {"sent": False, "count": 0, "skipped": len(transitions)}

    events = [t for t in transitions
              if t.get("to") is None or t.get("to") == "ALERTING"]
    if not notify_recover:
        events = [e for e in events if e.get("to") is not None]
    skipped = len(transitions) - len(events)
    if not events:
        return {"sent": False, "count": 0, "skipped": skipped}
    return {"sent": send_webhook(url, events, timeout=timeout),
            "count": len(events), "skipped": skipped}
