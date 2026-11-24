"""核心模块测试：排名、台账、下发、配置解析、数据库。

与 test_probe_fake.py 分工：
    探针文件测「给定字节流 → 分类与计时是否正确」
    本文件测「给定数据 → 排名/额度/下发是否正确」

全部用临时 SQLite，不触碰真实库，不消耗额度。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hub import capability, db, export, ledger, quota_rules, rank  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        self.ep_id = self._make_endpoint()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _make_endpoint(self, label="EP-A", provider="P-A", allow_proxy=None) -> int:
        db.seed_endpoints(self.conn, [{
            "provider": provider, "label": label,
            "base_url": "http://x/v1", "protocol": "openai_chat",
            "auth_env": "K", "default_model": "m1",
        }])
        ep = self.conn.execute("SELECT id, provider_id FROM endpoint WHERE label=?",
                               (label,)).fetchone()
        if allow_proxy:
            self.conn.execute(
                "INSERT INTO catalog_entry(provider_id,allow_proxy,source_type) "
                "VALUES(?,?,'manual')", (ep["provider_id"], allow_proxy))
            self.conn.commit()
        return ep["id"]

    def add_model(self, model_id="m1", is_chat=1, status="ACTIVE", ep_id=None):
        self.conn.execute(
            """INSERT INTO model(endpoint_id,model_id,is_chat,status,last_seen)
               VALUES(?,?,?,?,datetime('now','localtime'))""",
            (ep_id or self.ep_id, model_id, is_chat, status))
        self.conn.commit()

    def add_probe(self, status="HEALTHY", ttft=None, tps=None, total=1000,
                  stream_mode="true_stream", model_id="m1", level="L2",
                  age_hours=0, ep_id=None):
        ts = (datetime.now() - timedelta(hours=age_hours)).strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            """INSERT INTO probe_result(endpoint_id,model_id,level,status,ttft_ms,tps,
                                        total_ms,stream_mode,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (ep_id or self.ep_id, model_id, level, status, ttft, tps, total,
             stream_mode, ts))
        self.conn.commit()


class TestRank(Base):
    def test_insufficient_samples_excluded(self):
        self.add_model()
        self.add_probe()  # 只有 1 条，低于 MIN_SAMPLES=2
        r = rank.build(self.conn, "agent", persist=False)
        self.assertFalse(r[0]["eligible"])
        self.assertIn("样本不足", r[0]["reason"])

    def test_429_not_counted_in_availability(self):
        """限流是额度问题，不能算作服务不可用；且不应因此被踢出榜单。"""
        self.add_model()
        self.add_probe("HEALTHY")
        self.add_probe("RATE_LIMITED")
        self.add_probe("RATE_LIMITED")
        r = rank.build(self.conn, "agent", persist=False)
        self.assertTrue(r[0]["eligible"])
        self.assertAlmostEqual(r[0]["detail"]["parts"]["availability"], 1.0, places=3)

    def test_server_error_lowers_availability(self):
        """5xx 要计入可用率分母；最后一条保持 HEALTHY 才不会被状态过滤挡掉。"""
        self.add_model()
        self.add_probe("ENDPOINT_ERROR")
        self.add_probe("HEALTHY")
        r = rank.build(self.conn, "agent", persist=False)
        self.assertAlmostEqual(r[0]["detail"]["parts"]["availability"], 0.5, places=3)

    def test_missing_factors_reweight(self):
        """缺 TTFT 时不能当 0 分，应在剩余分项间重新分配权重。"""
        self.add_model()
        # 两条都成功但无 ttft/tps（缓冲型端点）
        self.add_probe(total=1000)
        self.add_probe(total=1000)
        r = rank.build(self.conn, "speed", persist=False)
        self.assertTrue(r[0]["eligible"])
        d = r[0]["detail"]
        self.assertIn("ttft", d["missing"])
        self.assertIn("tps", d["missing"])
        self.assertAlmostEqual(sum(d["weights_used"].values()), 1.0, places=3)
        self.assertNotIn("ttft", d["weights_used"])

    def test_quota_exhausted_excluded(self):
        self.add_model()
        self.add_probe()
        self.add_probe()
        ledger.write_quota(self.conn, self.ep_id, quota_type="TOTAL", unit="USD",
                           total=5, remaining=0, source="API")
        r = rank.build(self.conn, "agent", persist=False)
        self.assertFalse(r[0]["eligible"])
        self.assertIn("额度耗尽", r[0]["reason"])

    def test_quota_exhausted_not_masked_by_rate_limit_row(self):
        """真预算耗尽时，后写的速率行不能把它掩盖掉。

        HEADER 抓取每轮**先写美元预算行、后写速率行**，所以「最新一条」恒是速率行。
        若「额度耗尽」硬过滤按最新一条判，就会读到 `RATE_LIMIT/REQUEST 19/20`，
        真预算 `0/5 USD` 被掩盖，已耗尽的端点照样留在榜上。
        """
        self.add_model()
        self.add_probe()
        self.add_probe()
        ledger.write_quota(self.conn, self.ep_id, quota_type="DAILY", unit="USD",
                           total=5, remaining=0, source="HEADER")
        ledger.write_quota(self.conn, self.ep_id, quota_type="RATE_LIMIT",
                           unit="REQUEST", total=20, remaining=19, source="HEADER")
        r = rank.build(self.conn, "agent", persist=False)
        self.assertFalse(r[0]["eligible"])
        self.assertIn("额度耗尽", r[0]["reason"])

    def test_quota_factor_ignores_rate_limit_row(self):
        """额度分项算的是「能花的额度」，不能被「每分钟 20 次」的**速率**行顶掉。

        速率行也带 remaining，且在实测的写入顺序里恒为最新一条；若不排除，这一项
        就变成拿速率打分，方向还不固定——「20 次剩 1 次」记成 0.05（虚低），
        「20 次剩 19 次」记成 0.95（虚高），而真正的当日预算（1.0/0.9998）根本没被看到。
        """
        self.add_model()
        self.add_probe()
        self.add_probe()
        ledger.write_quota(self.conn, self.ep_id, quota_type="DAILY", unit="USD",
                           total=1.0, remaining=0.9998, source="HEADER")
        ledger.write_quota(self.conn, self.ep_id, quota_type="RATE_LIMIT",
                           unit="REQUEST", total=20, remaining=1.0, source="HEADER")
        self.assertAlmostEqual(rank._quota_factor(self.conn, self.ep_id), 0.9998,
                               places=3, msg="应按当日美元预算算剩余比例")

    def test_quota_factor_neutral_when_only_rate_limit(self):
        """只有速率行时算不出额度，记中性 0.5——既不把「剩 1/20」当额度告急，
        也不把「剩 19/20」当额度充裕。"""
        self.add_model()
        self.add_probe()
        self.add_probe()
        ledger.write_quota(self.conn, self.ep_id, quota_type="RATE_LIMIT",
                           unit="REQUEST", total=20, remaining=1.0, source="HEADER")
        self.assertEqual(rank._quota_factor(self.conn, self.ep_id), 0.5)

    def test_stale_data_excluded(self):
        self.add_model()
        self.add_probe(age_hours=48)
        self.add_probe(age_hours=48)
        r = rank.build(self.conn, "agent", persist=False)
        self.assertFalse(r[0]["eligible"])
        self.assertIn("过期", r[0]["reason"])

    def test_inactive_model_ignored(self):
        self.add_model(status="MISSING")
        self.add_probe()
        self.add_probe()
        r = rank.build(self.conn, "agent", persist=False)
        self.assertEqual(r, [])

    def test_non_chat_model_ignored(self):
        self.add_model(is_chat=0)
        self.add_probe()
        self.add_probe()
        r = rank.build(self.conn, "agent", persist=False)
        self.assertEqual(r, [])


class TestQuotaRules(unittest.TestCase):
    HDR = ('{"x-ratelimit-limit-user-daily-usd":"5",'
           '"x-ratelimit-used-user-daily-usd":"0.5",'
           '"x-ratelimit-remaining-user-daily-usd":"4.5",'
           '"x-ratelimit-reset-user-daily-usd":"1795363200",'
           '"x-ratelimit-limit-user-rpm":"60",'
           '"x-ratelimit-remaining-user-rpm":"57"}')

    def test_amd_rules(self):
        got = quota_rules.parse(self.HDR, "AMD Radeon (官方直连)")
        self.assertEqual(len(got), 2)
        daily = got[0]
        self.assertEqual(daily["unit"], "USD")
        self.assertEqual(daily["total"], 5.0)
        self.assertEqual(daily["remaining"], 4.5)
        # epoch 应换算成具体时间，且必须是 **本机本地**朴素串（空格分隔）。
        # 1795363200 = 2026-11-22T16:00:00Z（= 北京时间 11-23 00:00）。
        # 库内统一存本地时间，前端原样展示，故这里直接是北京时间的零点。
        self.assertEqual(daily["reset_at"], "2026-11-23 00:00:00")

    def test_reset_at_epoch_bad_input(self):
        self.assertIsNone(quota_rules.local_naive_from_epoch("abc"))
        self.assertIsNone(quota_rules.local_naive_from_epoch(None))
        self.assertIsNone(quota_rules.local_naive_from_epoch(""))

    def test_reset_at_from_timezone(self):
        # /usage 给的是时区名而非时刻，需换算成「下一个当地零点」的**本机本地**时刻。
        # 本机为北京时间（见 db.py 口径说明），故 Asia/Shanghai 的零点即本机零点。
        now_local = datetime(2026, 11, 23, 14, 28)      # 本机 14:28
        self.assertEqual(
            quota_rules.reset_at_from_timezone("Asia/Shanghai", now_local=now_local),
            "2026-11-24 00:00:00")
        # 当地恰好是零点时取「下一个」零点，而不是当场
        midnight = datetime(2026, 11, 23, 0, 0)
        self.assertEqual(
            quota_rules.reset_at_from_timezone("Asia/Shanghai", now_local=midnight),
            "2026-11-24 00:00:00")

    def test_reset_at_from_timezone_bad_input(self):
        for bad in (None, "", "   ", "Not/AZone"):
            self.assertIsNone(quota_rules.reset_at_from_timezone(bad))

    def test_two_reset_sources_agree(self):
        """两条来源（响应头 epoch / /usage 时区名）必须落在同一时刻。

        这正是原先的缺陷：一条走本地时区、一条走时区名，前端看到两个不同的
        「重置时间」。现在两条都归一为本机本地朴素串，应当严格相等。
        """
        hdr = ('{"x-ratelimit-limit-user-daily-usd":"5",'
               '"x-ratelimit-reset-user-daily-usd":"1795449600"}')
        from_header = quota_rules.parse(hdr, "AMD Radeon (官方直连)")[0]["reset_at"]
        from_usage = quota_rules.reset_at_from_timezone(
            "Asia/Shanghai", now_local=datetime(2026, 11, 23, 14, 28))
        self.assertEqual(from_header, from_usage)

    def test_unknown_provider_uses_generic_only(self):
        got = quota_rules.parse(self.HDR, "Some Other Vendor")
        self.assertEqual(got, [])

    def test_garbage_headers_safe(self):
        self.assertEqual(quota_rules.parse("not json", "AMD Radeon (官方直连)"), [])
        self.assertEqual(quota_rules.parse(None, "AMD Radeon (官方直连)"), [])


class TestLedger(Base):
    def test_invalid_source_rejected(self):
        with self.assertRaises(ValueError):
            ledger.write_quota(self.conn, self.ep_id, quota_type="DAILY",
                               unit="USD", total=1, source="GUESS")

    def test_quota_low_reminder(self):
        ledger.write_quota(self.conn, self.ep_id, quota_type="DAILY", unit="USD",
                           total=5, remaining=0.5, source="API")
        items = ledger.scan_reminders(self.conn, persist=False)
        self.assertTrue(any(i["kind"] == "QUOTA_LOW" for i in items))

    def test_quota_low_not_masked_by_later_rate_limit_row(self):
        """真预算低于阈值、且其后又写了速率行时，提醒不能漏发。

        与 rank 的「额度耗尽」同族：速率行恒为最新一条，若只按最新一条评估，
        真预算（5 USD 剩 0.5，10%）就永远轮不到被检查，「额度不足」会静默消失。
        """
        ledger.write_quota(self.conn, self.ep_id, quota_type="DAILY", unit="USD",
                           total=5, remaining=0.5, source="HEADER")
        ledger.write_quota(self.conn, self.ep_id, quota_type="RATE_LIMIT",
                           unit="REQUEST", total=20, remaining=19, source="HEADER")
        items = ledger.scan_reminders(self.conn, persist=False)
        self.assertTrue(any(i["kind"] == "QUOTA_LOW" for i in items))

    def test_expiring_reminder(self):
        soon = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        self.conn.execute("UPDATE endpoint SET expires_at=? WHERE id=?",
                          (soon, self.ep_id))
        self.conn.commit()
        items = ledger.scan_reminders(self.conn, persist=False)
        self.assertTrue(any(i["kind"] == "EXPIRING" for i in items))

    def test_policy_change_recorded(self):
        """政策变更必须留痕，不能覆盖旧值。"""
        ledger.upsert_catalog(self.conn, "P-A", free_form="STANDING",
                              allow_proxy="UNKNOWN", source_type="dataset")
        ledger.upsert_catalog(self.conn, "P-A", allow_proxy="NO",
                              source_url="https://example.com/tos",
                              verified_at="2026-11-22")
        rows = self.conn.execute(
            "SELECT field,old_value,new_value FROM provider_policy_history").fetchall()
        self.assertTrue(any(r["field"] == "allow_proxy" and r["new_value"] == "NO"
                            for r in rows))

    def test_reminders_not_persisted_when_asked(self):
        ledger.write_quota(self.conn, self.ep_id, quota_type="DAILY", unit="USD",
                           total=5, remaining=0.1, source="API")
        n0 = self.conn.execute("SELECT COUNT(*) c FROM reminder_log").fetchone()["c"]
        ledger.scan_reminders(self.conn, persist=False)
        n1 = self.conn.execute("SELECT COUNT(*) c FROM reminder_log").fetchone()["c"]
        self.assertEqual(n0, n1)


class TestCatalogIO(Base):
    def test_export_import_roundtrip(self):
        ledger.upsert_catalog(self.conn, "P-A", free_form="STANDING",
                              allow_proxy="NO", source_type="official",
                              verified_at="2026-11-22")
        data = ledger.export_catalog(self.conn)
        self.assertEqual(data["schema"], "tokriva.catalog/1")
        self.assertTrue(any(e["provider"] == "P-A" for e in data["entries"]))

        # 导入到空库，应新增
        with tempfile.TemporaryDirectory() as d:
            c2 = db.connect(Path(d) / "z.db")
            r = ledger.import_catalog(c2, data)
            self.assertEqual(r["added"], 1)
            row = c2.execute(
                "SELECT allow_proxy FROM catalog_entry").fetchone()
            self.assertEqual(row["allow_proxy"], "NO")
            c2.close()

    def test_import_only_missing(self):
        ledger.upsert_catalog(self.conn, "P-A", free_form="STANDING")
        data = {"schema": "tokriva.catalog/1", "entries": [
            {"provider": "P-A", "free_form": "ONE_TIME"},
            {"provider": "P-B", "free_form": "TRIAL"}]}
        r = ledger.import_catalog(self.conn, data, only_if_missing=True)
        self.assertEqual(r["skipped"], 1)
        self.assertEqual(r["added"], 1)
        # 已有条目不应被覆盖
        v = self.conn.execute(
            "SELECT free_form FROM catalog_entry WHERE provider_id="
            "(SELECT id FROM provider WHERE name='P-A')").fetchone()
        self.assertEqual(v["free_form"], "STANDING")

    def test_import_rejects_unknown_schema(self):
        with self.assertRaises(ValueError):
            ledger.import_catalog(self.conn, {"schema": "other/9", "entries": []})

    def test_alias_scene_resolution(self):
        ledger.set_alias(self.conn, "coding-main", "speed")
        self.assertEqual(ledger.resolve_alias_scene(self.conn, "coding-main"), "speed")
        self.assertEqual(ledger.resolve_alias_scene(self.conn, "unknown"), "agent")


class TestExport(Base):
    def _good_data(self):
        self.add_model()
        self.add_probe(ttft=1000, tps=20)
        self.add_probe(ttft=1100, tps=21)

    def test_blocked_when_allow_proxy_unknown(self):
        self._good_data()
        text, excluded = export.build_config(self.conn, "agent", "coding-main")
        self.assertEqual(text, "")
        self.assertTrue(excluded)
        self.assertIn("allow_proxy", excluded[0])

    def test_generates_yaml_when_allowed(self):
        self.conn.execute("INSERT INTO catalog_entry(provider_id,allow_proxy,source_type) "
                          "VALUES((SELECT id FROM provider WHERE name='P-A'),'YES','official')")
        self.conn.commit()
        self._good_data()
        text, excluded = export.build_config(self.conn, "agent", "coding-main")
        self.assertIn("model_list:", text)
        self.assertIn("coding-main", text)
        # Key 必须是环境变量引用，不能是明文
        self.assertIn("os.environ/K", text)
        self.assertEqual(excluded, [])

    def test_write_and_rollback(self):
        self.conn.execute("INSERT INTO catalog_entry(provider_id,allow_proxy,source_type) "
                          "VALUES((SELECT id FROM provider WHERE name='P-A'),'YES','official')")
        self.conn.commit()
        self._good_data()
        with tempfile.TemporaryDirectory() as d:
            export.OUT_DIR = Path(d)
            try:
                r1 = export.write_config(self.conn, "agent", "a1")
                self.assertTrue(r1["ok"])
                first = (Path(d) / "latest.yaml").read_text(encoding="utf-8")
                # 第二次写入会留存 prev
                self.conn.execute("UPDATE endpoint SET note='v2'")
                self.conn.commit()
                r2 = export.write_config(self.conn, "agent", "a2")
                self.assertTrue(r2["ok"])
                self.assertTrue(list(Path(d).glob("prev-*.yaml")))
                rb = export.rollback()
                self.assertTrue(rb["ok"])
                self.assertEqual((Path(d) / "latest.yaml").read_text(encoding="utf-8"), first)
            finally:
                export.OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "litellm"


class TestCapability(unittest.TestCase):
    def test_zero_context_treated_unknown(self):
        """AMD 的 Clef/d3 把 context_length 写成 0，不能当成真的 0 上下文。"""
        r = capability.parse_model_entry({"id": "Clef", "context_length": 0})
        self.assertIsNone(r["context_length"])
        self.assertEqual(r["context_source"], "UNKNOWN")

    def test_non_chat_detected(self):
        r = capability.parse_model_entry({
            "id": "d3-flash",
            "architecture": {"output_modalities": ["decision"]}})
        self.assertEqual(r["is_chat"], 0)

    def test_chat_and_context(self):
        r = capability.parse_model_entry({
            "id": "Qwen3.8-27B", "context_length": 262144,
            "architecture": {"output_modalities": ["text"]},
            "supported_parameters": ["tools", "stream"]})
        self.assertEqual(r["is_chat"], 1)
        self.assertEqual(r["context_length"], 262144)
        self.assertEqual(r["supports_tools"], 1)

    def test_usable_input(self):
        self.assertEqual(capability.usable_input_tokens(128000, 8000), 120000)
        self.assertIsNone(capability.usable_input_tokens(None, 8000))


class TestDb(unittest.TestCase):
    def test_connect_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.db"
            for _ in range(3):
                c = db.connect(p)
                c.close()
            c = db.connect(p)
            names = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for t in ("provider", "endpoint", "model", "probe_result",
                      "quota_snapshot", "rank_snapshot", "alias_group",
                      "probe_daily", "reminder_log", "catalog_entry"):
                self.assertIn(t, names)
            c.close()

    def test_seed_is_upsert(self):
        with tempfile.TemporaryDirectory() as d:
            c = db.connect(Path(d) / "y.db")
            ep = [{"provider": "P", "label": "L", "base_url": "http://a/v1",
                   "protocol": "openai_chat", "auth_env": "K"}]
            db.seed_endpoints(c, ep)
            db.seed_endpoints(c, [{**ep[0], "base_url": "http://b/v1"}])
            rows = c.execute("SELECT base_url FROM endpoint").fetchall()
            self.assertEqual(len(rows), 1, "重复 seed 不应产生多行")
            self.assertEqual(rows[0]["base_url"], "http://b/v1")
            c.close()


class TestConsistentCopy(unittest.TestCase):
    """`db.consistent_copy`：SQLite Online Backup 一致性副本（发布安全 P0）。

    为什么要有它：库开着 WAL，已提交但尚未 checkpoint 的页只在 `-wal` 里，
    **单拷主文件会漏数据**（实测真实库少 19 行）。公开导出要在一份数据上跑多次
    查询（总览/榜单/接口/明细），必须先把这一刻冻结成自足单文件，整包才自洽。
    """

    def _seed_live(self, path: Path) -> sqlite3.Connection:
        """建库并写入若干行，**保持连接打开**（WAL 才不会顺手被 checkpoint 掉）。"""
        conn = db.connect(path)
        db.seed_endpoints(conn, [{"provider": "P", "label": "L",
                                  "base_url": "http://a/v1",
                                  "protocol": "openai_chat", "auth_env": "K"}])
        ep = conn.execute("SELECT id FROM endpoint").fetchone()["id"]
        for i in range(40):
            conn.execute(
                "INSERT INTO probe_result(endpoint_id,model_id,level,status,total_ms,"
                "response_shape,created_at) "
                "VALUES(?,?,?,?,?,?,datetime('now','localtime'))",
                (ep, None, "L0", "HEALTHY", 100 + i, "json"))
        conn.commit()
        return conn

    @staticmethod
    def _count_ro(path: Path, table: str = "probe_result"):
        """只读统计行数；打不开或没这张表都返回 None（裸拷主文件常常如此）。"""
        try:
            c = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            return None
        finally:
            c.close()

    def test_copy_matches_source_and_includes_wal(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "live.db"
            conn = self._seed_live(src)
            try:
                live = conn.execute("SELECT COUNT(*) FROM probe_result").fetchone()[0]
                self.assertTrue(src.with_name(src.name + "-wal").is_file(),
                                "本用例前提：数据攒在 -wal 里（连接未关）")

                plain = d / "plain.db"          # 模拟 `cp`：只拷主文件
                shutil.copy2(src, plain)
                copy = db.consistent_copy(src, d / "sub" / "copy.db")

                self.assertEqual(self._count_ro(copy), live,
                                 "副本必须含只存在于 -wal 的已提交数据")
                self.assertNotEqual(self._count_ro(plain), live,
                                    "前提校验：裸拷主文件本就该读不全")
            finally:
                conn.close()

    def test_copy_is_self_contained_single_file(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "live.db"
            self._seed_live(src).close()        # 关闭即 checkpoint
            copy = db.consistent_copy(src, d / "copy.db")

            self.assertTrue(copy.is_file())
            self.assertFalse(copy.with_name(copy.name + "-wal").exists(),
                             "副本不该依赖 -wal")
            self.assertFalse(copy.with_name(copy.name + "-shm").exists(),
                             "副本不该依赖 -shm")
            c = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
            try:
                self.assertEqual(
                    c.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete",
                    "副本应落成单文件 rollback journal")
                self.assertGreater(
                    c.execute("SELECT COUNT(*) FROM probe_result").fetchone()[0], 0)
            finally:
                c.close()

    def test_copy_replaces_stale_target(self):
        """同名旧副本必须被覆盖，不能把上次的残留读成「本次结果」。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "live.db"
            self._seed_live(src).close()
            dst = d / "copy.db"
            dst.write_bytes(b"not a database at all")
            copy = db.consistent_copy(src, dst)
            self.assertEqual(self._count_ro(copy, "endpoint"), 1)


class RateLimitRetagMigration(unittest.TestCase):
    """`db._retag_rate_limit_rows`：把机器抓的速率行从 `SUBSCRIPTION` 改判为 `RATE_LIMIT`。

    边界是「只动机器写的」——手工录入（`MANUAL`）的订阅额度是用户明确声明的真额度，
    一个都不能改。用例同时钉住幂等：重复跑一次不应再动任何行。
    """

    def _legacy_db(self):
        """造一个「旧口径」库：user_version=1，速率行与订阅权益共用 SUBSCRIPTION。"""
        d = tempfile.TemporaryDirectory()
        conn = db.connect(Path(d.name) / "legacy.db")   # 会跑到最新版本
        conn.execute("PRAGMA user_version=1")           # 退回到「重打前」状态
        db.seed_endpoints(conn, [{
            "provider": "P", "label": "L", "base_url": "http://a/v1",
            "protocol": "openai_chat", "auth_env": "K",
        }])
        eid = conn.execute("SELECT id FROM endpoint WHERE label='L'").fetchone()["id"]
        # 机器写入的三行：HEADER 速率行（带 remaining）、HEADER 速率行（无 remaining）、
        # /usage 的 API 速率行——都该被重打。
        ledger.write_quota(conn, eid, quota_type="SUBSCRIPTION", unit="REQUEST",
                           total=20, remaining=19, source="HEADER")
        ledger.write_quota(conn, eid, quota_type="SUBSCRIPTION", unit="REQUEST",
                           total=60, source="API")
        # 真额度：HEADER 的当日美元预算——unit=USD，即便 quota_type 是 SUBSCRIPTION
        # 也不该被这条迁移碰到（迁移条件含 unit='REQUEST'）。
        ledger.write_quota(conn, eid, quota_type="SUBSCRIPTION", unit="USD",
                           total=1.0, remaining=0.5, source="HEADER")
        # 用户手工录的订阅权益——最不能被误伤的一行。
        ledger.write_quota(conn, eid, quota_type="SUBSCRIPTION", unit="REQUEST",
                           total=1000, remaining=250, source="MANUAL")
        conn.commit()
        return d, conn, eid

    def test_retag_only_machine_rows(self):
        d, conn, eid = self._legacy_db()
        try:
            self.assertTrue(db._retag_rate_limit_rows(conn), "应重打到行")
            got = {(r["unit"], r["source"]): r["quota_type"] for r in conn.execute(
                "SELECT unit, source, quota_type FROM quota_snapshot")}
            self.assertEqual(got[("REQUEST", "HEADER")], "RATE_LIMIT")
            self.assertEqual(got[("REQUEST", "API")], "RATE_LIMIT")
            self.assertEqual(got[("USD", "HEADER")], "SUBSCRIPTION",
                             "美元预算行不是速率行，不该动")
            self.assertEqual(got[("REQUEST", "MANUAL")], "SUBSCRIPTION",
                             "用户手工录的订阅额度是真额度，不许动")
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        finally:
            conn.close()
            d.cleanup()

    def test_retag_is_idempotent(self):
        d, conn, eid = self._legacy_db()
        try:
            db._retag_rate_limit_rows(conn)
            self.assertFalse(db._retag_rate_limit_rows(conn),
                             "版本号已到位，第二次不应再动")
            n = conn.execute("SELECT COUNT(*) c FROM quota_snapshot "
                             "WHERE quota_type='RATE_LIMIT'").fetchone()["c"]
            self.assertEqual(n, 2)
        finally:
            conn.close()
            d.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
