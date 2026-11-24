"""保留策略测试：让只写不裁的表有界。

覆盖两类无界增长：

1. **时间维度**（`run.prune`）—— probe_result 按天聚合后删除；quota_snapshot、
   model_utilization 直接裁旧行；reminder_log 用更长的审计窗口。
2. **轮次维度**（`ledger.scan_reminders`）—— 常驻循环每轮重复产出同一条提醒，
   同一故障在去重窗口内只落一条日志。

用临时库，不触碰真实数据。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hub import db, ledger, run  # noqa: E402


def _ago(days: float = 0, hours: float = 0) -> str:
    return (datetime.now() - timedelta(days=days, hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S")


class PruneTest(unittest.TestCase):
    """时间维度的保留策略。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._saved = run.DB_PATH
        run.DB_PATH = Path(self.tmp.name) / "retention.db"
        self.conn = db.connect(run.DB_PATH)
        self.eid = self.conn.execute(
            "INSERT INTO endpoint(label,base_url,protocol,auth_env) "
            "VALUES('EP-A','http://127.0.0.1:1/v1','openai_chat','EP_A_KEY')"
        ).lastrowid
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        run.DB_PATH = self._saved
        self.tmp.cleanup()

    # ---- 造数据 ----

    def _add_probe(self, created_at: str, level: str = "L2",
                   status: str = "HEALTHY", ttft: float = 100.0,
                   total: float = 500.0):
        self.conn.execute(
            "INSERT INTO probe_result(endpoint_id,model_id,level,status,ttft_ms,"
            "total_ms,tps,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.eid, "m-1", level, status, ttft, total, 30.0, created_at))

    def _add_quota(self, captured_at: str, remaining: float = 50.0):
        self.conn.execute(
            "INSERT INTO quota_snapshot(endpoint_id,quota_type,unit,total,"
            "remaining,captured_at) VALUES(?,?,?,?,?,?)",
            (self.eid, "DAILY", "REQUEST", 100.0, remaining, captured_at))

    def _add_util(self, captured_at: str):
        self.conn.execute(
            "INSERT INTO model_utilization(endpoint_id,model_id,utilization,"
            "state,captured_at) VALUES(?,?,?,?,?)",
            (self.eid, "m-1", 20.0, "idle", captured_at))

    def _add_reminder(self, created_at: str, subject: str = "EP-A 额度剩余 10%"):
        self.conn.execute(
            "INSERT INTO reminder_log(kind,endpoint_id,subject,created_at) "
            "VALUES('QUOTA_LOW',?,?,?)", (self.eid, subject, created_at))

    def _count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # ---- 旧行为：probe_result 先聚合再删 ----

    def test_probe_old_rows_aggregated_then_deleted(self):
        """超期明细删掉，但按天聚合进 probe_daily，长期趋势不丢。"""
        self._add_probe(_ago(days=40))
        self._add_probe(_ago(days=40))
        self._add_probe(_ago(days=1))
        self.conn.commit()

        r = run.prune(days=30)
        self.assertEqual(r["probe_result"], 2, "只删超期的两条")
        self.assertEqual(self._count("probe_result"), 1, "新鲜明细保留")
        self.assertEqual(self._count("probe_daily"), 1, "超期明细聚合成 1 天 1 行")
        agg = self.conn.execute("SELECT n FROM probe_daily").fetchone()
        self.assertEqual(agg["n"], 2, "聚合行数等于被删明细数")
        self.assertEqual(r["aggregated_days"], 1)

    def test_probe_daily_upsert_not_duplicated(self):
        """重复 prune 不会在 probe_daily 里堆重复行（UNIQUE + upsert）。"""
        self._add_probe(_ago(days=40))
        self.conn.commit()
        run.prune(days=30)
        self._add_probe(_ago(days=40))
        self.conn.commit()
        run.prune(days=30)
        self.assertEqual(self._count("probe_daily"), 1)

    # ---- 新增：无界快照表 ----

    def test_snapshot_tables_trimmed(self):
        self._add_quota(_ago(days=40))
        self._add_quota(_ago(hours=1))
        self._add_util(_ago(days=40))
        self._add_util(_ago(hours=1))
        self.conn.commit()

        r = run.prune(days=30)
        self.assertEqual(r["quota_snapshot"], 1)
        self.assertEqual(r["model_utilization"], 1)
        self.assertEqual(self._count("quota_snapshot"), 1, "新鲜快照保留")
        self.assertEqual(self._count("model_utilization"), 1)

    def test_audit_kept_longer_than_snapshots(self):
        """40 天前的提醒还在（审计窗口 90 天），但同期的快照已被裁。"""
        self._add_reminder(_ago(days=40))
        self._add_quota(_ago(days=40))
        self.conn.commit()

        r = run.prune(days=30)
        self.assertEqual(r["audit_days"], 90, "审计默认 3× 快照")
        self.assertEqual(r["reminder_log"], 0, "40 天在 90 天审计窗口内")
        self.assertEqual(self._count("reminder_log"), 1)
        self.assertEqual(self._count("quota_snapshot"), 0, "同期快照已裁")

    def test_audit_window_override(self):
        """显式指定审计窗口：同一批 40 天前的提醒，窗口放宽就留下、收窄就删。"""
        self._add_reminder(_ago(days=40))
        self.conn.commit()
        r = run.prune(days=30, audit_days=45)
        self.assertEqual(r["audit_days"], 45)
        self.assertEqual(r["reminder_log"], 0, "40 天落在 45 天窗口内，保留")
        r2 = run.prune(days=30, audit_days=35)
        self.assertEqual(r2["audit_days"], 35)
        self.assertEqual(r2["reminder_log"], 1, "窗口收到 35 天后就该删")

    def test_audit_never_shorter_than_snapshot(self):
        """audit_days 传得比 days 还小也没意义，向上钳到 days。"""
        self._add_reminder(_ago(days=20))
        self.conn.commit()
        r = run.prune(days=30, audit_days=1)
        self.assertEqual(r["audit_days"], 30)

    def test_prune_idempotent(self):
        self._add_probe(_ago(days=40))
        self._add_quota(_ago(days=40))
        self._add_reminder(_ago(days=120))
        self.conn.commit()
        first = run.prune(days=30)
        second = run.prune(days=30)
        self.assertEqual(second["probe_result"], 0)
        self.assertEqual(second["quota_snapshot"], 0)
        self.assertEqual(second["reminder_log"], 0)
        self.assertGreater(first["reminder_log"], 0)

    def test_all_counts_present(self):
        """返回值带上每张表的删除行数，便于 CLI 直接打印。"""
        self.conn.commit()
        r = run.prune(days=30)
        for k in ("keep_days", "audit_days", "aggregated_days", "probe_result",
                  "quota_snapshot", "model_utilization", "reminder_log"):
            self.assertIn(k, r)
            self.assertIsNotNone(r[k])

    def test_keep_days_at_least_one(self):
        self.assertEqual(run.prune(days=0)["keep_days"], 1)

    def test_fresh_round_keeps_everything(self):
        """正常一轮的数据一行都不该被删。"""
        self._add_probe(_ago(hours=1))
        self._add_quota(_ago(hours=1))
        self._add_util(_ago(hours=1))
        self._add_reminder(_ago(hours=1))
        self.conn.commit()
        r = run.prune(days=30)
        self.assertEqual(
            (r["probe_result"], r["quota_snapshot"],
             r["model_utilization"], r["reminder_log"]), (0, 0, 0, 0))


class ReminderLogDedupTest(unittest.TestCase):
    """轮次维度：同一故障每轮重复产出，只落一条日志。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.conn = db.connect(Path(self.tmp.name) / "dedup.db")
        # 造一个"未接入"状态：latest 为 NOT_CONFIGURED，每轮都会产出同一条提醒
        self.eid = self.conn.execute(
            "INSERT INTO endpoint(label,base_url,protocol,auth_env) "
            "VALUES('EP-A','http://127.0.0.1:1/v1','openai_chat','MISSING_KEY')"
        ).lastrowid
        self.conn.execute(
            "INSERT INTO probe_result(endpoint_id,model_id,level,status) "
            "VALUES(?,'m-1','L0','NOT_CONFIGURED')", (self.eid,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _log_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM reminder_log").fetchone()[0]

    def test_same_fault_logged_once(self):
        """连跑 5 轮，日志只落 1 条，但每轮返回的提醒都在。"""
        for _ in range(5):
            items = ledger.scan_reminders(self.conn)
            self.assertEqual(len(items), 1, "提醒本身不该被去重掉")
        self.assertEqual(self._log_count(), 1, "5 轮只落 1 条日志")

    def test_dedup_can_be_disabled(self):
        for _ in range(3):
            ledger.scan_reminders(self.conn, dedup_days=0)
        self.assertEqual(self._log_count(), 3, "dedup_days=0 恢复逐轮落库")

    def test_dedup_window_expiry(self):
        """把上一条挪出窗口，同一故障应重新落库（隔天再提醒一次）。"""
        ledger.scan_reminders(self.conn)
        self.conn.execute(
            "UPDATE reminder_log SET created_at=? ", (_ago(days=5),))
        self.conn.commit()
        ledger.scan_reminders(self.conn, dedup_days=1)
        self.assertEqual(self._log_count(), 2, "窗口外的旧记录不拦新一轮")

    def test_different_endpoints_not_deduped(self):
        """两个接口同样未接入：各记一条，不互相吃掉。"""
        self.conn.execute(
            "INSERT INTO endpoint(label,base_url,protocol,auth_env) "
            "VALUES('EP-B','http://127.0.0.1:1/v1','openai_chat','MISSING_KEY')")
        eid2 = self.conn.execute("SELECT id FROM endpoint WHERE label='EP-B'"
                                 ).fetchone()["id"]
        self.conn.execute(
            "INSERT INTO probe_result(endpoint_id,model_id,level,status) "
            "VALUES(?,'m-1','L0','NOT_CONFIGURED')", (eid2,))
        self.conn.commit()
        items = ledger.scan_reminders(self.conn)
        self.assertEqual(len(items), 2)
        self.assertEqual(self._log_count(), 2)

    def test_persist_false_writes_nothing(self):
        ledger.scan_reminders(self.conn, persist=False)
        self.assertEqual(self._log_count(), 0)

    def test_dedup_does_not_silence_webhook_list(self):
        """去重只落在库层：返回列表仍是完整的（通知该发照发）。"""
        first = ledger.scan_reminders(self.conn)
        second = ledger.scan_reminders(self.conn)
        self.assertEqual(len(first), len(second))
        self.assertEqual(first[0]["subject"], second[0]["subject"])


if __name__ == "__main__":
    unittest.main()
