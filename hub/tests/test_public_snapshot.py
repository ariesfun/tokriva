"""公开快照构建器测试：守卫"可以公开什么"这条边界。

与相邻测试的分工：
    test_static_export  只读站（本机脱网可看控制台）——烤的是管理接口整包
    本文件              公开站（GitHub Pages）——只允许正向白名单字段出门

为什么值得单测：公开站一旦上线，字段就是**对陌生人可见**的。白名单是正向的
（上游新增字段默认不公开），所以真正需要守卫的是四件事：
    ① 排除项真的被排除（凭据名、base_url、响应正文、出口路径、库路径…）；
    ② canary 扫描真的会拦（注入密钥/本机路径要被抓到）；
    ③ 全历史聚合独立于 overview 窗口，不能被 720 小时冒充；
    ④ 构建**幂等**：库不变则数据体与 `snapshotId` 都不变（"无变化不提交"的依据）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hub import api, db, public_snapshot  # noqa: E402
from hub.public_snapshot import scan_payload  # noqa: E402

# 公开契约**顶层**必须齐备的键（扁平结构，不套 `data.*`）。
EXPECTED_TOP_KEYS = {
    "schemaVersion", "meta", "overview", "rankings", "endpoints", "probes", "catalog",
}
# `meta` 自责块必须齐备的键（架构报告 §5.3.1 的自述字段 + 我们自加的溯源字段）。
EXPECTED_META_KEYS = {
    "snapshotId", "generatedAt", "dataAsOf", "timezone", "measurementScope",
    "freshness", "samplePolicy", "policyVersion", "probeSource",
    "windows", "endpointsCount", "modelCount",
}
SCENES = ("speed", "agent", "stable")
# 绝不能出现在公开产物里的字段名（正向白名单的"反向清单"）。
# 不含 `note`：榜单的 note 是"排名方法说明"，属要公开的内容；端点/台账的 note
# 由各自的正向投影排除，并有专门用例（test_endpoint_projection_* / test_catalog_*）。
NEVER_PUBLIC = (
    "authEnv", "baseUrl", "hasKey", "dailyBudget", "budgetReason",
    "error_body", "resp_body", "request_json", "resp_headers", "egress",
    "probePrompt", "scheduler", "provider_status",
)
# 「现在」派生字段：会让产物每次构建都不一样，必须被 drop_volatile 剥掉。
VOLATILE = ("generated_at", "age_sec")


class PublicSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.tmp.name)
        cls.db_path = cls.root / "public.db"
        cls._seed()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _seed(cls):
        """1 端点/1 模型 + 3 条新鲜 L2 + 1 条 L0 + 1 条**远古** L2（带敏感正文）。"""
        conn = db.connect(cls.db_path)
        db.seed_endpoints(conn, [{
            "provider": "P-A", "label": "EP-A", "base_url": "http://intranet.local/v1",
            "protocol": "openai_chat", "auth_env": "SECRET_API_KEY",
            "default_model": "m1", "note": "内网备注 C:\\Users\\ariesfun\\hub.db",
        }])
        ep = conn.execute("SELECT id, provider_id FROM endpoint").fetchone()
        conn.execute(
            """INSERT INTO model(endpoint_id,model_id,context_length,context_source,
                                 is_chat,supports_tools,status,last_seen)
               VALUES(?,?,?,'declared_api',1,1,'ACTIVE',datetime('now','localtime'))""",
            (ep["id"], "m1", 262144))
        # 远古记录**先插**（低 id）：真实库中 id 与时间同序，最新一条必是新鲜记录。
        # 若把它插在新鲜记录之后，rank 的"取最新一条看当前状态"会取到它，
        # 于是一个健康端点被误判成 ENDPOINT_ERROR 踢出榜——那是种子造假，不是产品问题。
        # 同时带上各类敏感正文：用于验证「全历史含它」且「正文被剥掉」。
        conn.execute(
            """INSERT INTO probe_result(endpoint_id,model_id,level,status,total_ms,
                                        error_body,resp_body,request_json,resp_headers,
                                        egress,error_kind,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ep["id"], "m1", "L2", "ENDPOINT_ERROR", 900,
             "sk-livedeadbeef1234567890", "{\"echo\":\"secret\"}", "{\"prompt\":\"x\"}",
             "authorization: Bearer abc", "proxy:tokyo-01", "HTTP_ERROR",
             "2020-01-01 00:00:00"))
        for i in range(3):
            conn.execute(
                """INSERT INTO probe_result(endpoint_id,model_id,level,status,ttft_ms,
                                            tps,total_ms,stream_mode,created_at)
                   VALUES(?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
                (ep["id"], "m1", "L2", "HEALTHY", 1000 + i * 10, 20 + i,
                 1500, "true_stream"))
        conn.execute(
            """INSERT INTO probe_result(endpoint_id,model_id,level,status,total_ms,
                                        response_shape,created_at)
               VALUES(?,?,?,?,?,?,datetime('now','localtime'))""",
            (ep["id"], None, "L0", "HEALTHY", 200, "json"))
        conn.execute(
            """INSERT INTO quota_snapshot(endpoint_id,quota_type,unit,total,remaining,
                                          source,quota_scope)
               VALUES(?,?,?,?,?,?,?)""",
            (ep["id"], "DAILY", "USD", 5, 4, "API", "ACCOUNT"))
        conn.execute(
            """INSERT INTO catalog_entry(provider_id,allow_proxy,source_type,confidence,note)
               VALUES(?,'UNKNOWN','manual','POSSIBLE','内部备注')""",
            (ep["provider_id"],))
        conn.commit()
        conn.close()

    def _conn(self):
        return db.connect_ro(self.db_path)

    def _build(self, **kw):
        conn = self._conn()
        try:
            return public_snapshot.build_public(conn, **kw)
        finally:
            conn.close()

    # ---------- 覆盖面 / 顶层形状 ----------
    def test_top_level_is_flat(self):
        """扁平契约：顶层不再有 data 嵌套、readOnly、mode。"""
        snap = self._build(probes_limit=50)
        self.assertEqual(set(snap), EXPECTED_TOP_KEYS)
        for gone in ("data", "readOnly", "mode", "health", "allTime", "windows"):
            self.assertNotIn(gone, snap, f"顶层不应再有 {gone}")

    def test_meta_is_self_describing(self):
        snap = self._build(probes_limit=50)
        self.assertEqual(set(snap["meta"]), EXPECTED_META_KEYS)
        self.assertEqual(snap["schemaVersion"], 1)
        self.assertEqual(snap["meta"]["timezone"], "Asia/Shanghai")
        self.assertEqual(snap["meta"]["measurementScope"], "single-origin-network")
        self.assertIn(snap["meta"]["freshness"], {"fresh", "stale", "expired", "unknown"})

    def test_default_windows_and_alltime_merged_into_overview(self):
        snap = self._build(probes_limit=50)
        self.assertEqual(set(snap["overview"]),
                         {str(w) for w in public_snapshot.WINDOWS} | {"allTime"})
        # allTime 与窗口同构于 overview，不再单列顶层键
        self.assertIn("totalProbes", snap["overview"]["allTime"])

    def test_rankings_are_populated(self):
        snap = self._build(probes_limit=50)
        for scene in SCENES:
            self.assertGreater(len(snap["rankings"][scene]["items"]), 0, scene)

    def test_endpoints_expose_public_id_and_status(self):
        """公开 ID 是名字派生的 slug，不是库内主键；状态由 active 派生。"""
        it = self._build(probes_limit=50)["endpoints"]["items"][0]
        self.assertEqual(it["providerId"], "ep-a")
        self.assertEqual(it["publicStatus"], "ACTIVE")
        self.assertNotIn("provider_status", it)

    # ---------- 白名单守卫：该排除的一项都不能漏 ----------
    def test_structural_scan_finds_nothing(self):
        """整个产物必须通过结构+内容扫描（最强的单条守卫）。"""
        snap = self._build(probes_limit=50)
        self.assertEqual(scan_payload(snap), [])

    def test_matrix_layers_carry_only_visitor_fields(self):
        """矩阵各层必须被收窄到访客白名单。

        为什么单独立一条：`scan_payload` 只拦**已知**的坏词，而层的**多出来的字段**
        本身就该被删掉——首版上线时 `l3.reason` 里躺着 `NOT_CONFIGURED`（凭据态的内部名），
        结构化字段 `hasKey` 明明剥掉了，同一信息却从这个散文口子漏给了访客。
        白名单是正向的，所以这里断言的是「集合相等」，不是「不含某几个坏字段」。
        """
        snap = self._build(probes_limit=50)
        seen = 0
        for win, blk in (snap.get("overview") or {}).items():
            for row in (blk or {}).get("matrix") or []:
                layers = row.get("layers") or {}
                for key, keep in public_snapshot._LAYER_KEEP.items():
                    layer = layers.get(key)
                    if not isinstance(layer, dict):
                        continue
                    seen += 1
                    extra = set(layer) - set(keep)
                    self.assertFalse(
                        extra, f"窗口 {win} 的 {key} 多出访客不该看到的字段：{sorted(extra)}")
        self.assertGreater(seen, 0, "没走到断言路径——layers 结构变了，本用例需同步更新")

    def test_probe_projection_drops_bodies_and_egress(self):
        snap = self._build(probes_limit=50)
        items = snap["probes"]["items"]
        self.assertTrue(items)
        for it in items:
            for bad in ("error_body", "resp_body", "request_json", "resp_headers", "egress"):
                self.assertNotIn(bad, it)
        # 标准化的错误分类要保留（供公开页展示"错误原因"）。
        # 命名照抄库列名（snake_case），见 docs/public-data-policy.md §2.0。
        self.assertIn("error_kind", items[0])

    def test_endpoint_projection_drops_credentials(self):
        snap = self._build(probes_limit=50)
        items = snap["endpoints"]["items"]
        self.assertEqual(len(items), 1)
        for bad in ("authEnv", "baseUrl", "hasKey", "note", "dailyBudget"):
            self.assertNotIn(bad, items[0])
        # 该公开的保留
        for good in ("providerId", "label", "provider", "availability",
                     "probeCount", "quotaSource", "publicStatus"):
            self.assertIn(good, items[0])

    def test_raw_health_not_exported(self):
        """原始 health 不进公开快照（报告 §5.3.7）；只保留 meta 里的公开状态。"""
        snap = self._build(probes_limit=5)
        self.assertNotIn("health", snap)
        self.assertNotIn("db", snap["meta"])
        self.assertNotIn("scheduler", snap["meta"])

    def test_overview_matrix_drops_haskey(self):
        snap = self._build(probes_limit=5)
        for ov in snap["overview"].values():
            for m in ov.get("matrix", []) if isinstance(ov, dict) else []:
                self.assertNotIn("hasKey", m)

    def test_catalog_drops_internal_id_and_note(self):
        snap = self._build(probes_limit=5)
        items = snap["catalog"]["items"]
        self.assertTrue(items)
        self.assertNotIn("provider_id", items[0])
        self.assertNotIn("note", items[0])

    def test_no_forbidden_key_anywhere(self):
        """兜底：逐键递归，确认 NEVER_PUBLIC 一个都没出现（除白名单例外）。"""
        snap = self._build(probes_limit=50)

        def walk(o, path="$"):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in NEVER_PUBLIC:
                        self.fail(f"敏感字段泄漏：{path}.{k}")
                    walk(v, f"{path}.{k}")
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, f"{path}[{i}]")

        walk(snap)

    # ---------- 幂等：库不变 → 产物不变 ----------
    def test_build_is_idempotent_apart_from_meta(self):
        """同一份库连构两次，除 meta 外必须逐字节一致（报告 §7.3 第 8 条）。"""
        a, b = self._build(probes_limit=50), self._build(probes_limit=50)
        strip = lambda s: {k: v for k, v in s.items() if k != "meta"}  # noqa: E731
        self.assertEqual(json.dumps(strip(a), sort_keys=True, ensure_ascii=False),
                         json.dumps(strip(b), sort_keys=True, ensure_ascii=False))

    def test_no_volatile_now_derived_fields(self):
        """`generated_at` / `age_sec` 这类「现在」派生字段必须被剥干净。"""
        snap = self._build(probes_limit=50)

        def walk(o, path="$"):
            if isinstance(o, dict):
                for k, v in o.items():
                    self.assertNotIn(k, VOLATILE, f"{path}.{k} 是易变字段")
                    walk(v, f"{path}.{k}")
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, f"{path}[{i}]")

        walk(snap["overview"])

    def test_snapshot_id_is_stable_and_well_formed(self):
        """`snapshotId` = 数据时点 + 本机时区偏移 + 数据体指纹；库不变则不变。"""
        a, b = self._build(probes_limit=50), self._build(probes_limit=50)
        sid = a["meta"]["snapshotId"]
        self.assertEqual(sid, b["meta"]["snapshotId"])
        self.assertRegex(sid, r"^\d{8}T\d{6}[+-]\d{4}-[0-9a-f]{12}$")

    def test_sample_policy_reflects_truncation(self):
        """截断标志必须如实反映「随包条数 < 全库条数」。"""
        cut = self._build(probes_limit=2)["meta"]["samplePolicy"]
        self.assertEqual(cut, {"recentProbeLimit": 2, "historyIsTruncated": True})
        full = self._build(probes_limit=50)["meta"]["samplePolicy"]
        self.assertEqual(full, {"recentProbeLimit": 50, "historyIsTruncated": False})

    # ---------- 新鲜度判定 ----------
    def test_freshness_thresholds(self):
        """90 / 180 分钟两档；未来时间戳与坏值都不许炸。"""
        f = public_snapshot.freshness_of
        base = datetime(2026, 11, 24, 14, 0, 0)
        stamp = "2026-11-24 14:00:00"
        self.assertEqual(f(stamp, now=base), "fresh")
        self.assertEqual(f(stamp, now=base + timedelta(minutes=89)), "fresh")
        self.assertEqual(f(stamp, now=base + timedelta(minutes=91)), "stale")
        self.assertEqual(f(stamp, now=base + timedelta(minutes=181)), "expired")
        self.assertEqual(f(stamp, now=base - timedelta(minutes=10)), "fresh")
        self.assertEqual(f(None), "unknown")
        self.assertEqual(f("not-a-timestamp"), "unknown")

    # ---------- 全历史 vs 窗口 ----------
    def test_alltime_counts_full_history_not_window(self):
        """远古记录（2020 年）必须计入 allTime，证明它不是 720 小时窗口。"""
        snap = self._build(probes_limit=50)
        at = snap["overview"]["allTime"]
        # 种子共 5 条探测（3 新鲜 L2 + 1 L0 + 1 远古 L2）
        self.assertEqual(at["totalProbes"], 5)
        self.assertEqual(at["daily"][0]["date"], "2020-01-01")
        self.assertEqual(snap["probes"]["total"], 5)

    def test_probes_limit_respected_but_total_is_real(self):
        snap = self._build(probes_limit=2)
        self.assertEqual(len(snap["probes"]["items"]), 2)
        self.assertEqual(snap["probes"]["total"], 5)

    def test_data_as_of_is_max_probe_time_not_now(self):
        """数据时点必须来自探测记录，而非导出时刻（否则会高估新鲜度）。"""
        snap = self._build(probes_limit=50)
        conn = self._conn()
        try:
            expected = conn.execute("SELECT MAX(created_at) FROM probe_result").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(snap["meta"]["dataAsOf"], expected)

    # ---------- 无副作用 ----------
    def test_never_calls_reminders(self):
        """reminders 会触发 _notify_transitions()，公开导出绝不能碰它。"""
        with mock.patch.object(api, "reminders",
                               side_effect=AssertionError("public export must not call reminders")):
            snap = self._build(probes_limit=5)
            self.assertNotIn("reminders", snap)
            self.assertEqual(scan_payload(snap), [])

    def test_never_calls_notify_transitions(self):
        with mock.patch.object(api, "_notify_transitions",
                               side_effect=AssertionError("no notifications in export")):
            self._build(probes_limit=5)

    # ---------- canary 本身会拦 ----------
    def test_scan_flags_secret_like_content(self):
        findings = scan_payload({"x": "sk-abcdefghijklmnop",
                                 "p": r"C:\Users\ariesfun\hub.db"})
        rules = {f["rule"] for f in findings}
        self.assertIn("key_prefix", rules)
        self.assertIn("win_path", rules)

    def test_scan_flags_forbidden_key(self):
        findings = scan_payload({"endpoints": {"items": [{"authEnv": "X"}]}})
        self.assertEqual(findings[0]["rule"], "forbidden_key")
        self.assertEqual(findings[0]["match"], "authEnv")

    def test_scan_allows_public_status_tokens(self):
        """NO_KEY 是公开状态结论，形似环境变量名但必须放行。"""
        self.assertEqual(scan_payload({"final": "NO_KEY"}), [])

    # ---------- 落盘与 CLI ----------
    def test_write_snapshot_is_valid_json(self):
        snap = self._build(probes_limit=5)
        out = self.root / "pub-write"
        info = public_snapshot.write_snapshot(snap, out)
        self.assertGreater(info["raw_bytes"], 0)
        self.assertLess(info["gzip_bytes"], info["raw_bytes"])
        loaded = json.loads((out / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(loaded["schemaVersion"], 1)
        self.assertIn("snapshotId", loaded["meta"])
        # 临时文件不应残留（原子替换）
        self.assertFalse((out / "snapshot.json.tmp").exists())

    def test_main_writes_and_returns_zero(self):
        out = self.root / "pub-cli-ok"
        rc = public_snapshot.main(
            ["--db", str(self.db_path), "--out", str(out), "--probes", "5", "--quiet"])
        self.assertEqual(rc, 0)
        self.assertTrue((out / "snapshot.json").exists())

    def test_main_aborts_without_writing_when_scan_fails(self):
        out = self.root / "pub-cli-fail"
        with mock.patch.object(public_snapshot, "scan_payload",
                               return_value=[{"rule": "x", "where": "y", "match": "z"}]):
            rc = public_snapshot.main(
                ["--db", str(self.db_path), "--out", str(out), "--probes", "5", "--quiet"])
        self.assertEqual(rc, 2)
        self.assertFalse((out / "snapshot.json").exists())


if __name__ == "__main__":
    unittest.main()
