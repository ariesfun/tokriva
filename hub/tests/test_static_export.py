"""静态导出器测试：验证只读快照的采集覆盖、excluded 压缩与产物落盘契约。

与 test_api.py 的分工：
    test_api     起真实 uvicorn 子线程，验证 HTTP 路由的返回结构与安全行为
    本文件       不起服务，直调 `hub.static_export`，验证「导出产物」这一层：
                 快照键是否齐、errors 是否为空、excluded 是否按模式压缩、
                 build_site 的落点与注入顺序是否满足「静态页脱网可跑」的前提。

为什么值得单测：静态页一旦发布，前端就**没有服务端可回退**——快照少一个键，
页面上就少一块数据且无人报错。导出器又是「尽力而为、单点失败不致命」的设计
（失败项只记进 errors），所以「errors 必须为空」这条契约只能靠用例守住。

用临时 SQLite + 临时输出目录；同时把四处 JSON 落盘路径改到临时区——否则
提醒状态机会把「临时库里没有这些接口」当成「故障已消失」，顺手清空真实状态。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hub import api, db, static_export  # noqa: E402

# 快照 `data` 下必须齐备的只读接口键（与 _collect 的 take(...) 一一对应）
EXPECTED_KEYS = {
    "health", "endpoints", "probes", "reminders", "catalog",
    "settings", "schedule", "config", "rankings", "overview",
}
# 三份榜单场景；注意静态页用的是 speed/agent/stable 三个键名
SCENES = ("speed", "agent", "stable")


class StaticExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from hub import reminder_state, settings

        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.tmp.name)
        cls.db_path = cls.root / "snap.db"
        cls._seed()

        # 五处落盘路径重定向到临时区（理由同 test_api）：少改一个，
        # 跑测试就会动到 data/ 下的真实文件。
        cls._saved = (api.DB_PATH, api.REMINDER_ACTIONS_FILE, api.NOTIFY_LOG_FILE,
                      reminder_state.STATE_FILE, settings.SETTINGS_FILE)
        api.DB_PATH = cls.db_path
        api.REMINDER_ACTIONS_FILE = cls.root / "actions.json"
        api.NOTIFY_LOG_FILE = cls.root / "notify.json"
        reminder_state.STATE_FILE = cls.root / "state.json"
        settings.SETTINGS_FILE = cls.root / "settings.json"

    @classmethod
    def tearDownClass(cls):
        from hub import reminder_state, settings

        (api.DB_PATH, api.REMINDER_ACTIONS_FILE, api.NOTIFY_LOG_FILE,
         reminder_state.STATE_FILE, settings.SETTINGS_FILE) = cls._saved
        cls.tmp.cleanup()

    @classmethod
    def _seed(cls):
        """建最小可用数据集：1 端点 + 1 模型 + 3 条 L2 探测 + 额度 + 台账。

        L2 探测给 3 条，是为了让 rank 的样本量门槛（MIN_SAMPLES）恰好满足，
        榜单能真的产出条目——空榜会让「导出的榜单是空的」这种缺陷漏过用例。
        """
        conn = db.connect(cls.db_path)
        db.seed_endpoints(conn, [{
            "provider": "P-A", "label": "EP-A", "base_url": "http://x/v1",
            "protocol": "openai_chat", "auth_env": "K", "default_model": "m1",
        }])
        ep = conn.execute("SELECT id, provider_id FROM endpoint").fetchone()
        conn.execute(
            """INSERT INTO model(endpoint_id,model_id,context_length,context_source,
                                 is_chat,supports_tools,status,last_seen)
               VALUES(?,?,?,'declared_api',1,1,'ACTIVE',datetime('now','localtime'))""",
            (ep["id"], "m1", 262144))
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
            """INSERT INTO catalog_entry(provider_id,allow_proxy,source_type,confidence)
               VALUES(?,'UNKNOWN','manual','POSSIBLE')""",
            (ep["provider_id"],))
        conn.commit()
        conn.close()

    # ---------- 采集：覆盖面与错误契约 ----------
    def test_collect_covers_all_readonly_endpoints_without_error(self):
        """十个只读接口必须全部取到且一个都不报错——静态页没有服务端可回退。"""
        data, errors = static_export._collect(
            probes_limit=50, windows=[24], excluded_mode="summary")

        self.assertEqual(errors, [])
        self.assertEqual(set(data), EXPECTED_KEYS)
        self.assertEqual(set(data["rankings"]), set(SCENES))
        self.assertEqual(set(data["overview"]), {"24"})

    def test_collect_returns_ranked_items_and_endpoints(self):
        """榜单与端点清单要真有内容，防止「接口通了但导出成空壳」。"""
        data, errors = static_export._collect(
            probes_limit=50, windows=[24], excluded_mode="summary")

        self.assertEqual(errors, [])
        for scene in SCENES:
            self.assertGreater(len(data["rankings"][scene]["items"]), 0, scene)
        self.assertEqual(len(data["endpoints"]["items"]), 1)

    def test_collect_respects_probes_limit(self):
        """随包条数受 --probes 限制，但 total 仍是库里的真实总数。"""
        data, errors = static_export._collect(
            probes_limit=2, windows=[24], excluded_mode="summary")

        self.assertEqual(errors, [])
        self.assertEqual(len(data["probes"]["items"]), 2)
        # 库里共 4 条（3 条 L2 + 1 条 L0）；这条断言正是「不要拿 items 当 total」的提醒
        self.assertEqual(data["probes"]["total"], 4)

    def test_collect_accepts_multiple_overview_windows(self):
        """--window 支持逗号分隔的多窗口，快照按字符串键分片。"""
        data, errors = static_export._collect(
            probes_limit=10, windows=[24, 72], excluded_mode="summary")

        self.assertEqual(errors, [])
        self.assertEqual(set(data["overview"]), {"24", "72"})

    def test_collect_records_failure_without_aborting(self):
        """单接口失败不致命：失败记进 errors，其余接口照常导出。"""
        with mock.patch.object(api, "catalog", side_effect=RuntimeError("boom")):
            data, errors = static_export._collect(
                probes_limit=5, windows=[24], excluded_mode="summary")

        self.assertEqual([e["key"] for e in errors], ["catalog"])
        self.assertNotIn("catalog", data)
        # 关键：一个接口挂了，别的九个不能跟着陪葬
        self.assertEqual(set(data), EXPECTED_KEYS - {"catalog"})

    # ---------- excluded：压缩模式 ----------
    def test_summarize_excluded_aggregates_and_drops_detail(self):
        """压缩后只留「接口 × 原因」的计数，明细整块消失。"""
        payload = {"items": [], "excluded": [
            {"endpoint": "EP-A", "reason": "数据不足"},
            {"endpoint": "EP-A", "reason": "数据不足"},
            {"endpoint": "EP-B", "reason": "额度耗尽"},
        ]}
        out = static_export._summarize_excluded(payload)

        self.assertNotIn("excluded", out)
        self.assertEqual(out["excludedCount"], 3)
        # 按条数降序：EP-A(2) 在前，EP-B(1) 在后
        self.assertEqual([row["endpoint"] for row in out["excludedSummary"]], ["EP-A", "EP-B"])
        self.assertEqual(out["excludedSummary"][0]["total"], 2)
        self.assertEqual(out["excludedSummary"][0]["reasons"],
                         [{"reason": "数据不足", "count": 2}])

    def test_summarize_excluded_tolerates_missing_field(self):
        """没有 excluded 字段（或不是 list）时原样返回，不凭空造字段。"""
        payload = {"items": []}
        self.assertEqual(static_export._summarize_excluded(payload), payload)

    def test_summary_mode_compresses_every_scene(self):
        data, _ = static_export._collect(
            probes_limit=5, windows=[24], excluded_mode="summary")
        for scene in SCENES:
            payload = data["rankings"][scene]
            self.assertNotIn("excluded", payload, scene)
            self.assertIn("excludedCount", payload, scene)
            self.assertIn("excludedSummary", payload, scene)

    def test_full_mode_keeps_excluded_detail(self):
        """--excluded full 时保留原样明细，不注入摘要字段。"""
        data, _ = static_export._collect(
            probes_limit=5, windows=[24], excluded_mode="full")
        for scene in SCENES:
            payload = data["rankings"][scene]
            self.assertIn("excluded", payload, scene)
            self.assertNotIn("excludedSummary", payload, scene)

    # ---------- 快照结构 ----------
    def test_build_snapshot_is_marked_readonly(self):
        snap = static_export.build_snapshot(probes_limit=5, windows=[24])

        self.assertTrue(snap["readOnly"])
        self.assertEqual(snap["schema"], 1)
        self.assertEqual(snap["errors"], [])
        self.assertIn("generatedAt", snap)

    # ---------- 落盘：产物完整性与注入顺序 ----------
    def test_build_site_writes_assets_and_injects_snapshot_before_appjs(self):
        """快照脚本必须排在 app.js 之前，否则只读模式来不及生效。"""
        out = self.root / "site"
        site = static_export.build_site(
            static_export.build_snapshot(probes_limit=5, windows=[24]), out)

        for name in ("index.html", "app.js", "styles.css", "snapshot.js"):
            self.assertTrue((out / name).exists(), name)
        self.assertGreater(site["raw_bytes"], 0)
        self.assertLess(site["gzip_bytes"], site["raw_bytes"])

        html = (out / "index.html").read_text(encoding="utf-8")
        # 用 `src="..."` 精确匹配：页面正文注释里也有裸 "app.js"（「视图由 app.js 渲染」），
        # 拿裸串比位置会误判。
        self.assertLess(html.index('src="snapshot.js"'), html.index('src="app.js"'))
        # 只注入一次：重复导出不该把同一行挂两遍
        self.assertEqual(html.count('src="snapshot.js"'), 1)

    def test_build_site_never_pollutes_web_dir(self):
        """产物绝不落进 web/——那是同源服务正在托管的活动资源。

        一旦 snapshot.js 出现在 web/，本机跑 8766 时前端也会进只读模式，
        正常控制台直接废掉。这条用例守的就是这个边界。
        """
        before = sorted(p.name for p in static_export.WEB.iterdir())
        out = self.root / "site-no-pollute"
        static_export.build_site(
            static_export.build_snapshot(probes_limit=1, windows=[24]), out)
        after = sorted(p.name for p in static_export.WEB.iterdir())

        self.assertEqual(before, after)
        self.assertFalse((static_export.WEB / "snapshot.js").exists())

    def test_snapshot_js_is_parseable_and_readonly(self):
        """snapshot.js 是「注释 + 一行赋值」，主体必须是合法 JSON。"""
        out = self.root / "site-parse"
        static_export.build_site(
            static_export.build_snapshot(probes_limit=5, windows=[24]), out)
        text = (out / "snapshot.js").read_text(encoding="utf-8")

        self.assertTrue(text.startswith("//"))
        self.assertIn("window.SNAPSHOT = ", text)
        body = text.split("window.SNAPSHOT = ", 1)[1].rstrip()
        self.assertTrue(body.endswith(";"))
        payload = json.loads(body[:-1])
        self.assertTrue(payload["readOnly"])
        self.assertIn("probes", payload["data"])

    # ---------- CLI 退出码 ----------
    def test_main_returns_zero_and_writes_site(self):
        out = self.root / "site-cli-ok"
        rc = static_export.main(["--out", str(out), "--probes", "5", "--quiet"])

        self.assertEqual(rc, 0)
        self.assertTrue((out / "snapshot.js").exists())

    def test_main_returns_one_when_an_endpoint_fails(self):
        """有接口导出失败时退出码 1，便于 CI 发现「静默缺数」。"""
        with mock.patch.object(api, "catalog", side_effect=RuntimeError("boom")):
            out = self.root / "site-cli-fail"
            rc = static_export.main(["--out", str(out), "--probes", "5", "--quiet"])

        self.assertEqual(rc, 1)
        # 失败也不该阻断落盘：其余九个接口的数据仍要写出来
        self.assertTrue((out / "snapshot.js").exists())


if __name__ == "__main__":
    unittest.main()
