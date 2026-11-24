"""探测预算测试：计数口径、降级决策、配置读写，以及「用户设的额度不被覆盖」的回归。

分四层：
- 纯逻辑（clamp_guard_pct / decision）：不碰库不碰盘，直接断言边界值。
- 口径（used_today / statuses）：临时库里造样本，证明「只算 L2、只算本地今天」。
- 配置（settings）：改到临时文件，验证读写往返与非法值收敛。
- 闸门（run._probe_one）：打桩 L0/L2，证明额度用尽时**连通检查照跑、速度测试停掉**，
  且关掉预算开关后立刻恢复。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hub import budget, db, run  # noqa: E402


class BudgetLogicTest(unittest.TestCase):
    """纯函数：阈值收敛与三态决策。"""

    def test_clamp_guard_pct(self):
        self.assertEqual(budget.clamp_guard_pct(90), 90)
        self.assertEqual(budget.clamp_guard_pct("70"), 70)
        self.assertEqual(budget.clamp_guard_pct(0), 1, "低于下限收敛到 1")
        self.assertEqual(budget.clamp_guard_pct(-5), 1)
        self.assertEqual(budget.clamp_guard_pct(500), 100, "高于上限收敛到 100")
        self.assertEqual(budget.clamp_guard_pct("abc"), budget.DEFAULT_GUARD_PCT)
        self.assertEqual(budget.clamp_guard_pct(None), budget.DEFAULT_GUARD_PCT)

    def test_decision_three_states(self):
        # 预算 200、阈值 90% → 180
        self.assertEqual(budget.decision(200, 0)["state"], budget.STATE_OK)
        self.assertEqual(budget.decision(200, 179)["state"], budget.STATE_OK)
        self.assertEqual(budget.decision(200, 180)["state"], budget.STATE_GUARDED)
        self.assertEqual(budget.decision(200, 199)["state"], budget.STATE_GUARDED)
        self.assertEqual(budget.decision(200, 200)["state"], budget.STATE_EXHAUSTED)
        self.assertEqual(budget.decision(200, 999)["state"], budget.STATE_EXHAUSTED)

    def test_decision_floor_threshold_on_small_budget(self):
        """小预算下阈值向下取整：5 次的 90% 记为 4，降级态一定看得到。"""
        self.assertEqual(budget.decision(5, 3)["state"], budget.STATE_OK)
        self.assertEqual(budget.decision(5, 4)["state"], budget.STATE_GUARDED)
        self.assertEqual(budget.decision(5, 5)["state"], budget.STATE_EXHAUSTED)

    def test_decision_allow_and_max_models(self):
        ok = budget.decision(200, 10)
        self.assertTrue(ok["allow_l2"])
        self.assertIsNone(ok["max_models"], "正常态不限制每轮模型数")

        guarded = budget.decision(200, 190)
        self.assertTrue(guarded["allow_l2"], "接近上限仍做 L2，只是缩减为 1 个模型")
        self.assertEqual(guarded["max_models"], 1)
        self.assertTrue(guarded["reason"], "降级要给出可读原因")

        exhausted = budget.decision(200, 200)
        self.assertFalse(exhausted["allow_l2"], "额度用尽不再做速度测试")
        self.assertEqual(exhausted["max_models"], 0)

    def test_decision_input_robustness(self):
        # 脏数据不该抛异常：预算是监控的护栏，护栏自己不能先倒
        self.assertEqual(budget.decision(None, None)["state"], budget.STATE_OK)
        self.assertEqual(budget.decision(1, 0)["state"], budget.STATE_OK)
        self.assertEqual(budget.decision(1, 1)["state"], budget.STATE_EXHAUSTED)
        self.assertEqual(budget.decision("bad", "bad", "bad")["state"], budget.STATE_OK)

    def test_decision_guard_100_never_guards(self):
        """阈值 100% 时不存在「接近上限」区间，直接由用尽接管。"""
        self.assertEqual(budget.decision(100, 99, 100)["state"], budget.STATE_OK)
        self.assertEqual(budget.decision(100, 100, 100)["state"], budget.STATE_EXHAUSTED)

    def test_decision_quota_gate(self):
        """第二道闸门：账号额度（金额/token）到位即降级、用尽即停，即使次数还很宽裕。"""
        d = budget.decision(200, 0, 90, quota_used_pct=95)
        self.assertEqual(d["state"], budget.STATE_GUARDED)
        self.assertTrue(d["allow_l2"], "额度接近上限仍做 L2，只是缩减为 1 个模型")
        self.assertEqual(d["max_models"], 1)
        self.assertIn("额度", d["reason"], "原因要指明是额度这道闸门")

        d = budget.decision(200, 0, 90, quota_used_pct=100)
        self.assertEqual(d["state"], budget.STATE_EXHAUSTED)
        self.assertFalse(d["allow_l2"])

        self.assertEqual(budget.decision(200, 0, 90, quota_used_pct=10)["state"],
                         budget.STATE_OK, "额度宽裕不干预")

    def test_decision_takes_worse_gate(self):
        """两道闸门取更严：次数已用尽时额度再宽裕也停 L2。"""
        d = budget.decision(200, 200, 90, quota_used_pct=1)
        self.assertEqual(d["state"], budget.STATE_EXHAUSTED)
        self.assertIn("次数", d["reason"])

    def test_decision_quota_unknown_falls_back_to_count(self):
        """额度未知（None）闸门自动让位，只按次数约束。"""
        self.assertEqual(budget.decision(200, 0, 90, quota_used_pct=None)["state"],
                         budget.STATE_OK)
        self.assertEqual(budget.decision(200, 200, 90, quota_used_pct=None)["state"],
                         budget.STATE_EXHAUSTED)

    def test_decision_quota_dirty_values(self):
        """额度脏数据不抛异常、视为未知；超范围钳到 [0,100]。"""
        for bad in ("bad", float("nan"), None, {}):
            self.assertEqual(budget.decision(200, 0, 90, quota_used_pct=bad)["state"],
                             budget.STATE_OK)
        self.assertEqual(budget.decision(200, 0, 90, quota_used_pct=150)["state"],
                         budget.STATE_EXHAUSTED, "超过 100% 钳到用尽")
        self.assertEqual(budget.decision(200, 0, 90, quota_used_pct=-5)["state"],
                         budget.STATE_OK, "负数钳到 0")


class BudgetCountingTest(unittest.TestCase):
    """计数口径：只算 L2、只算本地今天。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmp.name) / "budget.db"
        self.conn = db.connect(self.db_path)
        db.seed_endpoints(self.conn, [{
            "provider": "P", "label": "EP-A", "base_url": "http://x/v1",
            "protocol": "openai_chat", "auth_env": "K", "default_model": "m1",
            "daily_budget": 10,
        }])
        self.eid = self.conn.execute(
            "SELECT id FROM endpoint WHERE label='EP-A'").fetchone()["id"]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _add(self, level: str, day_offset: int = 0):
        self.conn.execute(
            "INSERT INTO probe_result(endpoint_id,level,status,created_at) "
            "VALUES(?,?,?,datetime('now','localtime',?))",
            (self.eid, level, "HEALTHY", f"{day_offset} days"))
        self.conn.commit()

    def test_counts_l2_only(self):
        """L0 不占额度：它通常不消耗配额，被预算挡住就等于「额度紧张时连连通性都不测」。"""
        for _ in range(2):
            self._add("L2")
        for _ in range(3):
            self._add("L0")
        self.assertEqual(budget.used_today(self.conn, self.eid), 2)

    def test_ignores_previous_days(self):
        """跨天清零：库里 created_at 是本地时间，按本地日期比，否则用户会在早上看到昨晚的用量。"""
        self._add("L2")
        self._add("L2", day_offset=-1)
        self._add("L2", day_offset=-2)
        self.assertEqual(budget.used_today(self.conn, self.eid), 1)

    def test_used_today_zero_when_empty(self):
        self.assertEqual(budget.used_today(self.conn, self.eid), 0)

    def test_statuses_accepts_dict_and_row(self):
        for _ in range(9):
            self._add("L2")
        # dict 入参（接口层 _rows 产出）
        as_dict = budget.statuses(self.conn, [
            {"id": self.eid, "label": "EP-A", "daily_budget": 10}])
        st = as_dict["EP-A"]
        self.assertEqual(st["used"], 9)
        self.assertEqual(st["budget"], 10)
        self.assertEqual(st["remaining"], 1)
        self.assertEqual(st["state"], budget.STATE_GUARDED, "9/10 达到 90% 阈值")

        # sqlite3.Row 入参（探针链路直接传 Row 的写法）也不该炸
        row = self.conn.execute(
            "SELECT id, label, daily_budget FROM endpoint WHERE id=?", (self.eid,)).fetchone()
        as_row = budget.statuses(self.conn, [row])
        self.assertEqual(as_row["EP-A"]["used"], 9)

    def test_statuses_remaining_not_negative(self):
        for _ in range(12):
            self._add("L2")
        st = budget.statuses(self.conn, [
            {"id": self.eid, "label": "EP-A", "daily_budget": 10}])["EP-A"]
        self.assertEqual(st["remaining"], 0, "超额时剩余量钳到 0，不出现负数")
        self.assertEqual(st["state"], budget.STATE_EXHAUSTED)

    def _quota(self, total, remaining, used=None, unit="USD", qtype="DAILY"):
        self.conn.execute(
            """INSERT INTO quota_snapshot(endpoint_id,quota_type,unit,total,used,remaining,source)
               VALUES(?,?,?,?,?,?,'API')""",
            (self.eid, qtype, unit, total, used, remaining))
        self.conn.commit()

    def test_quota_used_pct_reads_latest(self):
        self.assertIsNone(budget.quota_used_pct(self.conn, self.eid), "无快照=未知")
        self._quota(10.0, 2.5)  # 已用 7.5/10 = 75%
        self.assertAlmostEqual(budget.quota_used_pct(self.conn, self.eid), 75.0)
        self._quota(10.0, 0.0)  # 取最近一条
        self.assertAlmostEqual(budget.quota_used_pct(self.conn, self.eid), 100.0)

    def test_quota_used_pct_prefers_explicit_used(self):
        self._quota(100.0, 40.0, used=60.0)
        self.assertAlmostEqual(budget.quota_used_pct(self.conn, self.eid), 60.0)

    def test_quota_used_pct_ignores_rate_limit_row(self):
        """RATE_LIMIT/REQUEST 是「每分钟多少次」的**速率**限制，不是可消耗额度。

        实测它**是带 remaining 的**（`x-ratelimit-remaining-user-rpm`），所以光靠
        「remaining 非空」拦不住；而解析顺序让速率行永远写在预算行之后，于是它恒为
        「最新一条」。若按最新一条取，会把「每分钟 20 次用掉 19 次」算成 95% 已用
        而误降级，真正的当日美元预算可能才用了 0.02%。
        """
        self._quota(1.0, 0.9998, unit="USD", qtype="DAILY")          # 真额度
        self._quota(20.0, 1.0, unit="REQUEST", qtype="RATE_LIMIT")    # 速率行，且更新
        self.assertAlmostEqual(budget.quota_used_pct(self.conn, self.eid), 0.02,
                               places=2)

    def test_quota_used_pct_counts_subscription_row(self):
        """`SUBSCRIPTION` 是**用户录的订阅类权益**（月付套餐），是真额度，必须计入。

        这条钉住类型拆分：早先速率行也借 `SUBSCRIPTION`，导致「按类型排除」时
        会连用户手工填的订阅额度一起误伤。拆出 `RATE_LIMIT` 后，`SUBSCRIPTION`
        必须照常算「已用比例」。
        """
        self._quota(1.0, 0.5, unit="USD", qtype="SUBSCRIPTION")
        self.assertAlmostEqual(budget.quota_used_pct(self.conn, self.eid), 50.0,
                               places=2)

    def test_quota_used_pct_ignores_rows_without_remaining(self):
        """只有 total 没有 remaining 的行算不出「已用比例」，不是可用的额度行。"""
        self._quota(60.0, None, unit="REQUEST", qtype="RATE_LIMIT")
        self.assertIsNone(budget.quota_used_pct(self.conn, self.eid))
        self._quota(50.0, None, unit="USD", qtype="DAILY")
        self.assertIsNone(budget.quota_used_pct(self.conn, self.eid))

    def test_quota_used_pct_ignores_bad_total(self):
        self._quota(0, 0)      # total<=0 无意义
        self.assertIsNone(budget.quota_used_pct(self.conn, self.eid))

    def test_statuses_exposes_quota(self):
        self._quota(10.0, 1.0)  # 已用 90%
        st = budget.statuses(self.conn, [
            {"id": self.eid, "label": "EP-A", "daily_budget": 100}])["EP-A"]
        self.assertAlmostEqual(st["quota_used_pct"], 90.0)
        self.assertEqual(st["state"], budget.STATE_GUARDED, "次数宽裕但额度到阈值")
        self.assertTrue(st["reason"])


class BudgetConfigTest(unittest.TestCase):
    """配置读写：默认值、往返、非法值收敛。"""

    def setUp(self):
        from hub import settings
        self.settings = settings
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._saved = settings.SETTINGS_FILE
        settings.SETTINGS_FILE = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.settings.SETTINGS_FILE = self._saved
        self.tmp.cleanup()

    def test_defaults(self):
        self.assertEqual(self.settings.budget_config(), (True, 90))

    def test_roundtrip(self):
        self.settings.save_settings({"budgetEnabled": False, "budgetGuardPct": 55})
        self.assertEqual(self.settings.budget_config(), (False, 55))

    def test_guard_pct_clamped_on_save(self):
        self.settings.save_settings({"budgetGuardPct": 0})
        self.assertEqual(self.settings.budget_config()[1], 1)
        self.settings.save_settings({"budgetGuardPct": 500})
        self.assertEqual(self.settings.budget_config()[1], 100)
        self.settings.save_settings({"budgetGuardPct": "oops"})
        self.assertEqual(self.settings.budget_config()[1], self.settings.DEFAULTS["budgetGuardPct"])

    def test_unknown_keys_ignored(self):
        """配置里不该塞进未声明的字段：load 只认 DEFAULTS 里的键。"""
        self.settings.save_settings({"budgetNope": 1})
        self.assertNotIn("budgetNope", self.settings.load_settings())


class BudgetSeedTest(unittest.TestCase):
    """回归：seed_endpoints 每轮都跑，不能把用户设的每日额度冲掉。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.conn = db.connect(Path(self.tmp.name) / "seed.db")
        self.base = {
            "provider": "P", "label": "EP-A", "base_url": "http://x/v1",
            "protocol": "openai_chat", "auth_env": "K", "default_model": "m1",
        }

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _budget(self) -> int:
        return self.conn.execute(
            "SELECT daily_budget FROM endpoint WHERE label='EP-A'").fetchone()["daily_budget"]

    def test_first_insert_uses_seed_default(self):
        db.seed_endpoints(self.conn, [dict(self.base, daily_budget=50)])
        self.assertEqual(self._budget(), 50)

    def test_reseed_keeps_user_value(self):
        """这里是「假开关」的另一半：页面能改、库里能改，但下一轮 seed 又按常量覆写。"""
        db.seed_endpoints(self.conn, [self.base])
        self.conn.execute(
            "UPDATE endpoint SET daily_budget=7 WHERE label='EP-A'")
        self.conn.commit()
        # 再跑一轮 seed（模拟每次探测前的幂等写入），带上代码里的默认 200
        db.seed_endpoints(self.conn, [dict(self.base, daily_budget=200)])
        self.assertEqual(self._budget(), 7, "用户设的额度必须保留")

    def test_reseed_still_updates_connection_fields(self):
        """只把 daily_budget 从 upsert 里拿掉，连接参数仍要以代码为准。"""
        db.seed_endpoints(self.conn, [self.base])
        db.seed_endpoints(self.conn, [dict(self.base, base_url="http://y/v1")])
        self.assertEqual(
            self.conn.execute(
                "SELECT base_url FROM endpoint WHERE label='EP-A'").fetchone()["base_url"],
            "http://y/v1")


class BudgetGateTest(unittest.TestCase):
    """闸门集成：额度用尽时，L0 照记、L2 停发；关掉开关立刻恢复。"""

    def setUp(self):
        from hub import settings
        self.settings = settings
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self._saved_settings = settings.SETTINGS_FILE
        settings.SETTINGS_FILE = root / "settings.json"
        self.conn = db.connect(root / "gate.db")
        db.seed_endpoints(self.conn, [dict(self.ep_seed(), daily_budget=1)])
        row = self.conn.execute(
            "SELECT id, provider_id FROM endpoint WHERE label='EP-A'").fetchone()
        self.ep = {
            "id": row["id"], "provider_id": row["provider_id"], "label": "EP-A",
            "default_model": "m1", "daily_budget": 1,
            "has_usage": 0, "has_utilization": 0, "include_usage": 1,
        }

    @staticmethod
    def ep_seed() -> dict:
        return {
            "provider": "P", "label": "EP-A", "base_url": "http://x/v1",
            "protocol": "openai_chat", "auth_env": "K", "default_model": "m1",
        }

    def tearDown(self):
        self.conn.close()
        self.settings.SETTINGS_FILE = self._saved_settings
        self.tmp.cleanup()

    def _consume(self):
        """先占掉当日唯一的额度。"""
        self.conn.execute(
            """INSERT INTO probe_result(endpoint_id,level,status,created_at)
               VALUES(?,?,?,datetime('now','localtime'))""", (self.ep["id"], "L2", "HEALTHY"))
        self.conn.commit()

    def _levels(self) -> list[str]:
        return [r["level"] for r in self.conn.execute(
            "SELECT level FROM probe_result WHERE endpoint_id=? ORDER BY id", (self.ep["id"],))]

    def test_exhausted_still_runs_l0_but_skips_l2(self):
        self._consume()
        l0 = {"endpoint_id": self.ep["id"], "level": "L0", "status": "HEALTHY", "models": []}
        with mock.patch.object(run, "probe_l0", return_value=dict(l0)), \
             mock.patch.object(run, "probe_stream") as m_stream:
            run._probe_one(self.conn, self.ep)
        self.assertFalse(m_stream.called, "额度用尽不得再发生成请求")
        self.assertIn("L0", self._levels(), "连通检查仍要记录：接口不能从监控里消失")

    def test_budget_off_probes_normally(self):
        self._consume()
        self.settings.save_settings({"budgetEnabled": False})
        l0 = {"endpoint_id": self.ep["id"], "level": "L0", "status": "HEALTHY", "models": []}
        l2 = {"endpoint_id": self.ep["id"], "level": "L2", "status": "HEALTHY",
              "model_id": "m1", "total_ms": 1200}
        with mock.patch.object(run, "probe_l0", return_value=dict(l0)), \
             mock.patch.object(run, "probe_stream", return_value=dict(l2)) as m_stream:
            run._probe_one(self.conn, self.ep)
        self.assertTrue(m_stream.called, "关掉预算即不做任何限制")
        self.assertIn("L2", self._levels())

    def test_guarded_round_limits_to_one_model(self):
        """接近上限只测 1 个模型：保住主模型、砍掉探索名额。"""
        self.ep["daily_budget"] = 10
        self.conn.execute("UPDATE endpoint SET daily_budget=10 WHERE id=?", (self.ep["id"],))
        self.conn.commit()
        for _ in range(9):  # 9/10 达到 90% 阈值
            self._consume()
        self.ep["free_only"] = True
        # 铺 3 个免费模型，正常态会测 2 个（MAX_MODELS_PER_ROUND）
        for name in ("m1", "m2", "m3"):
            self.conn.execute(
                """INSERT INTO model(endpoint_id,model_id,is_chat,is_free,status,last_seen)
                   VALUES(?,?,1,1,'ACTIVE',datetime('now','localtime'))""", (self.ep["id"], name))
        self.conn.commit()
        l0 = {"endpoint_id": self.ep["id"], "level": "L0", "status": "HEALTHY", "models": []}
        l2 = {"endpoint_id": self.ep["id"], "level": "L2", "status": "FAILED", "model_id": "m1"}
        with mock.patch.object(run, "probe_l0", return_value=dict(l0)), \
             mock.patch.object(run, "probe_stream", return_value=dict(l2)) as m_stream:
            run._probe_one(self.conn, self.ep)
        self.assertEqual(m_stream.call_count, 1, "降级态每轮只测 1 个模型")

    def _quota_exhausted(self):
        self.conn.execute(
            """INSERT INTO quota_snapshot(endpoint_id,quota_type,unit,total,remaining,source)
               VALUES(?, 'DAILY','USD',10.0,0.0,'API')""", (self.ep["id"],))
        self.conn.commit()

    def test_quota_exhausted_skips_l2_but_runs_l0(self):
        """账号额度用尽：即使当日次数还很宽裕，也不得再发生成请求；L0 照记。"""
        self.ep["daily_budget"] = 200
        self.conn.execute("UPDATE endpoint SET daily_budget=200 WHERE id=?", (self.ep["id"],))
        self.conn.commit()
        self._quota_exhausted()
        l0 = {"endpoint_id": self.ep["id"], "level": "L0", "status": "HEALTHY", "models": []}
        with mock.patch.object(run, "probe_l0", return_value=dict(l0)), \
             mock.patch.object(run, "probe_stream") as m_stream:
            run._probe_one(self.conn, self.ep)
        self.assertFalse(m_stream.called, "账号额度用尽不得再发生成请求")
        self.assertIn("L0", self._levels(), "连通检查仍要记录")


if __name__ == "__main__":
    unittest.main()
