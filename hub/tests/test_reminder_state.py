"""提醒状态机测试：待确认 → 持续告警 → 恢复中 → 销号 的完整流转。

只测纯函数 advance()，不碰网络、不碰真实状态文件——状态字典在内存里传，
每个用例从空状态开始，断言"第几轮扫描之后应该是什么状态"。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hub import reminder_state as rs  # noqa: E402

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def item(kind="AUTH_FAILED", endpoint="OpenRouter"):
    return {"kind": kind, "endpoint": endpoint,
            "subject": f"{endpoint} 出问题了", "detail": "d"}


def tick(state, items, step=1, **kw):
    """推进一轮，返回 (enriched, transitions, summary)。"""
    _, enriched, transitions, summary = rs.advance(
        items, state=state, now=T0 + timedelta(minutes=step), **kw)
    return enriched, transitions, summary


class ReminderStateTest(unittest.TestCase):
    def test_full_lifecycle(self):
        """信号类：出现 → 待确认 → 告警 → 恢复中 → 销号。"""
        st = {}

        # 第 1 轮：首次出现，进待确认期，不该当成故障
        en, tr, sm = tick(st, [item()])
        self.assertEqual(en[0]["state"], rs.PENDING)
        self.assertEqual(sm, {"pending": 1, "alerting": 0, "recovering": 0})
        self.assertEqual(tr[0]["from"], None)
        self.assertEqual(tr[0]["to"], rs.PENDING)

        # 第 2 轮：连续命中达到 N=2，确认故障
        en, tr, sm = tick(st, [item()])
        self.assertEqual(en[0]["state"], rs.ALERTING)
        self.assertEqual(sm["alerting"], 1)
        self.assertEqual(tr[0]["from"], rs.PENDING)
        self.assertEqual(tr[0]["to"], rs.ALERTING)

        # 第 3 轮：还在，保持告警，不产生新的翻转
        en, _, sm = tick(st, [item()])
        self.assertEqual(en[0]["state"], rs.ALERTING)
        self.assertEqual(sm["alerting"], 1)
        self.assertEqual(len(st), 1)

        # 第 4 轮：恢复正常，进入恢复中（仍在列表里，提示"正在恢复"）
        en, tr, sm = tick(st, [])
        self.assertEqual(len(en), 1)
        self.assertEqual(en[0]["state"], rs.RECOVERING)
        self.assertEqual(tr[0]["to"], rs.RECOVERING)
        self.assertEqual(sm["recovering"], 1)

        # 第 5 轮：连续 M=2 轮正常，销号，列表为空
        en, tr, sm = tick(st, [])
        self.assertEqual(en, [])
        self.assertEqual(st, {})
        self.assertEqual(tr[0]["to"], None)

    def test_pending_blip_is_silent(self):
        """一次抖动：待确认期内就消失，静默销号，不进告警也不进恢复中。"""
        st = {}
        tick(st, [item()])
        self.assertEqual(st["AUTH_FAILED|OpenRouter"]["state"], rs.PENDING)
        en, tr, sm = tick(st, [])
        self.assertEqual(en, [])
        self.assertEqual(st, {})
        self.assertEqual(sm, {"pending": 0, "alerting": 0, "recovering": 0})

    def test_regression_counts_flap(self):
        """恢复期间又坏了：退回告警，并记一次反复。"""
        st = {}
        tick(st, [item()])
        tick(st, [item()])                      # -> ALERTING
        tick(st, [])                            # -> RECOVERING
        self.assertEqual(st["AUTH_FAILED|OpenRouter"]["state"], rs.RECOVERING)

        en, tr, _ = tick(st, [item()])           # 又出现
        self.assertEqual(en[0]["state"], rs.ALERTING)
        self.assertEqual(en[0]["flaps"], 1)
        self.assertEqual(tr[0]["from"], rs.RECOVERING)
        self.assertEqual(tr[0]["reason"], "恢复期间再次出现")

    def test_fact_kind_confirms_immediately(self):
        """确定性事实（额度/到期/未接入）首见即告警，不浪费用户两轮等待。"""
        st = {}
        en, _, _ = tick(st, [item(kind="QUOTA_LOW", endpoint="AMD")])
        self.assertEqual(en[0]["state"], rs.ALERTING)
        en, _, _ = tick(st, [item(kind="NOT_CONFIGURED", endpoint="Packet")])
        self.assertEqual(en[0]["state"], rs.ALERTING)

    def test_thresholds_configurable(self):
        """N / M 可调：N=3 时第 2 轮仍停在待确认，M=3 时第 3 轮才销号。"""
        st = {}
        tick(st, [item()], confirm_n=3)
        en, _, _ = tick(st, [item()], confirm_n=3)
        self.assertEqual(en[0]["state"], rs.PENDING)
        en, _, _ = tick(st, [item()], confirm_n=3)
        self.assertEqual(en[0]["state"], rs.ALERTING)

        tick(st, [], recover_m=3)
        en, _, _ = tick(st, [], recover_m=3)
        self.assertEqual(en[0]["state"], rs.RECOVERING)   # 第 2 轮仍在恢复中
        en, _, _ = tick(st, [], recover_m=3)
        self.assertEqual(en, [])                          # 第 3 轮销号

    def test_multiple_keys_tracked_independently(self):
        """同一轮里不同接口各自走各自的生命周期。"""
        st = {}
        tick(st, [item(endpoint="A"), item(endpoint="B")])   # 两条都进待确认
        tick(st, [item(endpoint="A"), item(endpoint="B")])   # 两条都确认告警
        en, _, _ = tick(st, [item(endpoint="A")])            # B 恢复，A 仍是告警
        by_ep = {e["endpoint"]: e for e in en}
        self.assertEqual(by_ep["A"]["state"], rs.ALERTING)
        self.assertEqual(by_ep["B"]["state"], rs.RECOVERING)
        self.assertEqual(len(st), 2)

    def test_duration_and_age(self):
        """两个时间口径：age=从首次出现算起，state=从进入当前状态算起。

        界面要说的两句话不一样：「这个问题出现 30 分钟了」与「已确认告警 5 分钟」。
        """
        st = {}
        tick(st, [item()], step=0)               # T0：进入待确认
        en, _, _ = tick(st, [item()], step=30)   # T0+30min：确认告警，状态刚翻转
        self.assertEqual(en[0]["age_min"], 30)   # 首次出现在 30 分钟前
        self.assertEqual(en[0]["state_min"], 0)  # 刚进入告警态
        self.assertEqual(en[0]["seen"], 2)

    def test_same_round_does_not_advance(self):
        """同一探测批次被重复读取（页面刷新 / 前端轮询）不该推进计数。

        回归点：曾经按"接口被调用几次"算轮次，用户刷新两次页面就能把一次
        网络抖动确认成故障。
        """
        st = {}
        en, _, _ = tick(st, [item()], step=0, round_id="r1")
        self.assertEqual(en[0]["state"], rs.PENDING)
        self.assertEqual(en[0]["seen"], 1)

        # 同批次再看两眼：状态原样回显，不该被刷成告警
        for _ in range(3):
            en, tr, sm = tick(st, [item()], step=0, round_id="r1")
            self.assertEqual(en[0]["state"], rs.PENDING)
            self.assertEqual(en[0]["seen"], 1)
            self.assertEqual(tr, [])
            self.assertEqual(sm, {"pending": 1, "alerting": 0, "recovering": 0})

        # 换一批才算新一轮，这才该确认告警
        en, tr, _ = tick(st, [item()], step=0, round_id="r2")
        self.assertEqual(en[0]["state"], rs.ALERTING)
        self.assertEqual(en[0]["seen"], 2)
        self.assertEqual(tr[0]["from"], rs.PENDING)

    def test_same_round_does_not_advance_recovery(self):
        """同一批次重复读取也不能把告警"刷"进恢复中。"""
        st = {}
        tick(st, [item()], step=0, round_id="r1")
        tick(st, [item()], step=0, round_id="r2")          # 确认告警
        en, _, _ = tick(st, [item()], step=0, round_id="r2")
        self.assertEqual(en[0]["state"], rs.ALERTING, "同批次不该进入恢复中")

        # 新批次里提醒消失 → 恢复中
        en, tr, sm = tick(st, [], step=0, round_id="r3")
        self.assertEqual(en[0]["state"], rs.RECOVERING)
        self.assertEqual(en[0]["clear"], 1)

        # 同批次再刷两次：不该被刷到销号
        for _ in range(3):
            en, tr, sm = tick(st, [], step=0, round_id="r3")
            self.assertEqual(en[0]["state"], rs.RECOVERING)
            self.assertEqual(en[0]["clear"], 1)
            self.assertEqual(tr, [])

        # 换批次才算第二轮正常，此时销号
        en, tr, sm = tick(st, [], step=0, round_id="r4")
        self.assertEqual(en, [])
        self.assertEqual(sm, {"pending": 0, "alerting": 0, "recovering": 0})
        self.assertEqual(tr[0]["to"], None)

    def test_same_round_still_admits_new_time_driven_item(self):
        """同批次里新冒出来的"事实类"提醒要立刻入账。

        到期、额度不足是跟着时钟走的，不是探测产物；如果因为批次没变就
        视而不见，要等到下一轮探测才被看见，等于凭空延迟一次告警。
        """
        st = {}
        tick(st, [item()], step=0, round_id="r1")

        en, tr, _ = tick(st, [item("EXPIRED", "AMD")], step=0, round_id="r1")
        by_ep = {e["endpoint"]: e for e in en}
        self.assertEqual(by_ep["AMD"]["state"], rs.ALERTING, "到期首见即告警")
        self.assertEqual(by_ep["AMD"]["seen"], 1)
        self.assertTrue(any(t["endpoint"] == "AMD" and t["to"] == rs.ALERTING
                            for t in tr))

    def test_round_key_is_not_counted(self):
        """批次标记不能混进提醒统计。"""
        st = {}
        tick(st, [item()], step=0, round_id="r1")
        self.assertIn(rs._ROUND_KEY, st)
        _, _, sm = tick(st, [item()], step=0, round_id="r1")
        self.assertEqual(sm, {"pending": 1, "alerting": 0, "recovering": 0})


def rec(state=rs.ALERTING, kind="AUTH_FAILED", endpoint="OpenRouter",
        since_hours=1.0, notified_hours=None):
    """拼一条状态记录。since / last_notified 用「距今几小时」表示，读起来直观。"""
    r = {"state": state, "kind": kind, "endpoint": endpoint,
         "subject": f"{endpoint} 出问题了", "detail": "d",
         "since": (T0 - timedelta(hours=since_hours)).isoformat()}
    if notified_hours is not None:
        r["last_notified"] = (T0 - timedelta(hours=notified_hours)).isoformat()
    return r


class DueRepeatsTest(unittest.TestCase):
    """长期不恢复的故障要能再喊一声，且不能变成刷屏。"""

    def test_zero_disables_repeat(self):
        """repeat_hours=0 = 关闭重发：只清计时，不发也不留痕。"""
        st = {"AUTH_FAILED|OpenRouter": rec(notified_hours=99)}
        st, reps = rs.due_repeats(st, [], now=T0, repeat_hours=0)
        self.assertEqual(reps, [])
        self.assertNotIn("last_notified", st["AUTH_FAILED|OpenRouter"])

    def test_just_alerting_starts_the_clock(self):
        """本轮刚翻转成告警的：已经把首条发出去了，只记时点，不重复发。"""
        st = {"AUTH_FAILED|OpenRouter": rec(since_hours=0)}
        tr = [{"key": "AUTH_FAILED|OpenRouter", "to": rs.ALERTING}]
        st, reps = rs.due_repeats(st, tr, now=T0, repeat_hours=6)
        self.assertEqual(reps, [])
        self.assertEqual(st["AUTH_FAILED|OpenRouter"]["last_notified"],
                         T0.isoformat())

    def test_fires_once_then_resets(self):
        """到点发一条，然后把计时重置——下一次得再等一个间隔。"""
        st = {"AUTH_FAILED|OpenRouter": rec(notified_hours=7)}
        st, reps = rs.due_repeats(st, [], now=T0, repeat_hours=6)
        self.assertEqual(len(reps), 1)
        r = reps[0]
        self.assertTrue(r["repeat"])
        self.assertEqual(r["to"], rs.ALERTING)
        self.assertEqual(r["to_label"], "持续告警")
        self.assertIn("未", r["reason"])            # 「已持续 …, 仍未恢复」

        st, reps2 = rs.due_repeats(st, [], now=T0, repeat_hours=6)
        self.assertEqual(reps2, [], "计时已重置，不该连着发")

    def test_not_due_yet_stays_quiet(self):
        st = {"AUTH_FAILED|OpenRouter": rec(notified_hours=3)}
        _, reps = rs.due_repeats(st, [], now=T0, repeat_hours=6)
        self.assertEqual(reps, [])

    def test_seed_from_since_when_never_notified(self):
        """本功能上线前就已经在告警的：从状态开始时间起算，立刻补一条。

        否则这些"老故障"永远等不到重发——它们恰恰是最该被提醒的。
        """
        st = {"AUTH_FAILED|OpenRouter": rec(since_hours=10)}   # 无 last_notified
        _, reps = rs.due_repeats(st, [], now=T0, repeat_hours=6)
        self.assertEqual(len(reps), 1)
        self.assertIn("10", reps[0]["reason"])                 # 已持续约 10 小时

    def test_fresh_alert_without_timer_is_not_spammed(self):
        """刚告警但没记过计时（如进程重启）的：从 since 起算还没到点，静默。"""
        st = {"AUTH_FAILED|OpenRouter": rec(since_hours=0.2)}
        _, reps = rs.due_repeats(st, [], now=T0, repeat_hours=6)
        self.assertEqual(reps, [])

    def test_recovery_clears_timer(self):
        """恢复中 / 待确认：清掉计时，等再次告警时重新开始。"""
        for state in (rs.RECOVERING, rs.PENDING):
            st = {"AUTH_FAILED|OpenRouter": rec(state=state, notified_hours=9)}
            st, reps = rs.due_repeats(st, [], now=T0, repeat_hours=6)
            self.assertEqual(reps, [], state)
            self.assertNotIn("last_notified", st["AUTH_FAILED|OpenRouter"], state)

    def test_round_marker_is_skipped(self):
        """批次标记不该被当成一条提醒来重发。"""
        st = {rs._ROUND_KEY: "r1", "AUTH_FAILED|OpenRouter": rec(notified_hours=9)}
        _, reps = rs.due_repeats(st, [], now=T0, repeat_hours=6)
        self.assertEqual([r["key"] for r in reps], ["AUTH_FAILED|OpenRouter"])

    def test_reenabling_catches_up_on_old_fault(self):
        """关闭期间一直坏着的故障，重新打开重发后应立刻补一条。

        做法：关闭时不维护计时，重新打开时便以 since 为起点重新判定。
        """
        st = {"AUTH_FAILED|OpenRouter": rec(since_hours=20, notified_hours=1)}
        st, reps = rs.due_repeats(st, [], now=T0, repeat_hours=0)
        self.assertEqual(reps, [])
        self.assertNotIn("last_notified", st["AUTH_FAILED|OpenRouter"])

        _, reps = rs.due_repeats(st, [], now=T0, repeat_hours=6)
        self.assertEqual(len(reps), 1, "重新打开后老故障立刻补一条")


class ObserveRepeatTest(unittest.TestCase):
    """observe() 是接口层唯一的入口，得把重发一并带出来。"""

    def test_observe_returns_repeats_after_interval(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "state.json"
            with mock.patch.object(rs, "STATE_FILE", f):
                # 第 1 轮：额度类首见即告警，只有翻转、没有重发
                _, tr, _, reps = rs.observe([item(kind="QUOTA_LOW")],
                                            repeat_hours=6, now=T0)
                self.assertEqual(len(tr), 1)
                self.assertEqual(reps, [], "刚告警不该立刻重发")

                # 第 2 轮：时间推过 6 小时，仍在告警 → 状态没变，但该重发
                _, tr2, _, reps2 = rs.observe([item(kind="QUOTA_LOW")],
                                              repeat_hours=6,
                                              now=T0 + timedelta(hours=7))
                self.assertEqual(tr2, [], "状态未变，不该有翻转")
                self.assertEqual(len(reps2), 1, "到点该重发")
                self.assertTrue(reps2[0]["repeat"])

    def test_observe_repeat_disabled_passes_through(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "state.json"
            with mock.patch.object(rs, "STATE_FILE", f):
                rs.observe([item(kind="QUOTA_LOW")], repeat_hours=0, now=T0)
                _, _, _, reps = rs.observe([item(kind="QUOTA_LOW")], repeat_hours=0,
                                           now=T0 + timedelta(hours=7))
                self.assertEqual(reps, [], "关闭重发时一条都不该有")


if __name__ == "__main__":
    unittest.main()
