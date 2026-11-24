"""公开快照构建器测试：守卫"可以公开什么"这条边界。

与相邻测试的分工：
    test_static_export  只读站（本机脱网可看控制台）——烤的是管理接口整包
    本文件              公开站（GitHub Pages）——只允许正向白名单字段出门

为什么值得单测：公开站一旦上线，字段就是**对陌生人可见**的。白名单是正向的
（上游新增字段默认不公开），所以真正需要守卫的是三件事：
    ① 排除项真的被排除（凭据名、base_url、响应正文、出口路径、库路径…）；
    ② canary 扫描真的会拦（注入密钥/本机路径要被抓到）；
    ③ 全历史聚合独立于 overview 窗口，不能被 720 小时冒充。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hub import api, db, public_snapshot  # noqa: E402
from hub.public_snapshot import scan_payload  # noqa: E402

# 公开快照 `data` 下必须齐备的键
EXPECTED_KEYS = {
    "meta", "health", "endpoints", "probes", "catalog",
    "rankings", "overview", "allTime",
}
SCENES = ("speed", "agent", "stable")
# 绝不能出现在公开产物里的字段名（正向白名单的"反向清单"）。
# 不含 `note`：榜单的 note 是"排名方法说明"，属要公开的内容；端点/台账的 note
# 由各自的正向投影排除，并有专门用例（test_endpoint_projection_* / test_catalog_*）。
NEVER_PUBLIC = (
    "authEnv", "baseUrl", "hasKey", "dailyBudget", "budgetReason",
    "error_body", "resp_body", "request_json", "resp_headers", "egress",
    "probePrompt", "scheduler",
)


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

    # ---------- 覆盖面 ----------
    def test_build_covers_all_keys(self):
        snap = self._build(probes_limit=50)
        self.assertTrue(snap["readOnly"])
        self.assertEqual(snap["mode"], "public")
        self.assertEqual(snap["schema"], 1)
        self.assertIn("generatedAt", snap)
        self.assertIn("dataAsOf", snap)
        self.assertEqual(set(snap["data"]), EXPECTED_KEYS)

    def test_default_windows_and_alltime_present(self):
        snap = self._build(probes_limit=50)
        self.assertEqual(set(snap["data"]["overview"]),
                         {str(w) for w in public_snapshot.WINDOWS})
        self.assertIn("allTime", snap["data"])

    def test_rankings_are_populated(self):
        snap = self._build(probes_limit=50)
        for scene in SCENES:
            self.assertGreater(len(snap["data"]["rankings"][scene]["items"]), 0, scene)

    # ---------- 白名单守卫：该排除的一项都不能漏 ----------
    def test_structural_scan_finds_nothing(self):
        """整个产物必须通过结构+内容扫描（最强的单条守卫）。"""
        snap = self._build(probes_limit=50)
        self.assertEqual(scan_payload(snap), [])

    def test_probe_projection_drops_bodies_and_egress(self):
        snap = self._build(probes_limit=50)
        items = snap["data"]["probes"]["items"]
        self.assertTrue(items)
        for it in items:
            for bad in ("error_body", "resp_body", "request_json", "resp_headers", "egress"):
                self.assertNotIn(bad, it)
        # 标准化的错误分类要保留（供公开页展示"错误原因"）
        self.assertIn("errorKind", items[0])

    def test_endpoint_projection_drops_credentials(self):
        snap = self._build(probes_limit=50)
        items = snap["data"]["endpoints"]["items"]
        self.assertEqual(len(items), 1)
        for bad in ("authEnv", "baseUrl", "hasKey", "note", "dailyBudget"):
            self.assertNotIn(bad, items[0])
        # 该公开的保留
        for good in ("label", "provider", "availability", "probeCount", "quotaSource"):
            self.assertIn(good, items[0])

    def test_health_drops_db_path_and_scheduler(self):
        h = self._build(probes_limit=5)["data"]["health"]
        self.assertNotIn("db", h)
        self.assertNotIn("scheduler", h)
        self.assertEqual(set(h), {"ok", "probeRows", "latestProbeAt"})

    def test_overview_matrix_drops_haskey(self):
        snap = self._build(probes_limit=5)
        for ov in snap["data"]["overview"].values():
            for m in ov.get("matrix", []):
                self.assertNotIn("hasKey", m)

    def test_catalog_drops_internal_id_and_note(self):
        snap = self._build(probes_limit=5)
        items = snap["data"]["catalog"]["items"]
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

    # ---------- 全历史 vs 窗口 ----------
    def test_alltime_counts_full_history_not_window(self):
        """远古记录（2020 年）必须计入 allTime，证明它不是 720 小时窗口。"""
        snap = self._build(probes_limit=50)
        at = snap["data"]["allTime"]
        # 种子共 5 条探测（3 新鲜 L2 + 1 L0 + 1 远古 L2）
        self.assertEqual(at["totalProbes"], 5)
        self.assertEqual(at["daily"][0]["date"], "2020-01-01")
        self.assertEqual(snap["data"]["probes"]["total"], 5)

    def test_probes_limit_respected_but_total_is_real(self):
        snap = self._build(probes_limit=2)
        self.assertEqual(len(snap["data"]["probes"]["items"]), 2)
        self.assertEqual(snap["data"]["probes"]["total"], 5)

    # ---------- 无副作用 ----------
    def test_never_calls_reminders(self):
        """reminders 会触发 _notify_transitions()，公开导出绝不能碰它。"""
        with mock.patch.object(api, "reminders",
                               side_effect=AssertionError("public export must not call reminders")):
            snap = self._build(probes_limit=5)
            self.assertNotIn("reminders", snap["data"])
            self.assertEqual(scan_payload(snap), [])

    def test_never_calls_notify_transitions(self):
        with mock.patch.object(api, "_notify_transitions",
                               side_effect=AssertionError("no notifications in export")):
            self._build(probes_limit=5)

    # ---------- canary 本身会拦 ----------
    def test_scan_flags_secret_like_content(self):
        findings = scan_payload({"data": {"x": "sk-abcdefghijklmnop",
                                          "p": r"C:\Users\ariesfun\hub.db"}})
        rules = {f["rule"] for f in findings}
        self.assertIn("key_prefix", rules)
        self.assertIn("win_path", rules)

    def test_scan_flags_forbidden_key(self):
        findings = scan_payload({"data": {"endpoints": {"items": [{"authEnv": "X"}]}}})
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
        self.assertTrue(loaded["readOnly"])
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
