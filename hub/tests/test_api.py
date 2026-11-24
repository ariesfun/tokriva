"""API 层测试：起真实 uvicorn 服务，验证 10 个路由的返回结构与安全行为。

为什么不用 TestClient：环境未安装 httpx（TestClient 的依赖）。
直接起 uvicorn 子线程 + 标准库 urllib，反而更接近真实调用路径。

所有数据放临时库，api.DB_PATH 在测试内被替换，不触碰真实数据。
"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn  # noqa: E402

from hub import api, db  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from hub import reminder_state, settings
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.tmp.name)
        cls.db_path = cls.root / "api.db"
        cls._seed()

        # 四处落盘路径都要改到临时区。少改一个，跑测试就会动到 data/ 下的
        # 真实文件——提醒状态机会把"临时库里没有这些接口"当成"故障已消失"，
        # 顺手把真实状态清空。
        cls._saved = (api.DB_PATH, api.REMINDER_ACTIONS_FILE, api.NOTIFY_LOG_FILE,
                      reminder_state.STATE_FILE, settings.SETTINGS_FILE)
        api.DB_PATH = cls.db_path
        api.REMINDER_ACTIONS_FILE = cls.root / "actions.json"
        api.NOTIFY_LOG_FILE = cls.root / "notify_log.json"
        reminder_state.STATE_FILE = cls.root / "state.json"
        settings.SETTINGS_FILE = cls.root / "settings.json"

        cls.port = _free_port()
        cfg = uvicorn.Config(api.app, host="127.0.0.1", port=cls.port,
                             log_level="error", lifespan="on")
        cls.server = uvicorn.Server(cfg)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()

        for _ in range(100):
            if getattr(cls.server, "started", False):
                break
            time.sleep(0.05)
        if not cls.server.started:
            raise RuntimeError("API 未能在预期时间内启动")

    @classmethod
    def tearDownClass(cls):
        from hub import reminder_state, settings
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        (api.DB_PATH, api.REMINDER_ACTIONS_FILE, api.NOTIFY_LOG_FILE,
         reminder_state.STATE_FILE, settings.SETTINGS_FILE) = cls._saved
        cls.tmp.cleanup()

    @classmethod
    def _seed(cls):
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

    # ---------- 请求工具 ----------
    def get(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def get_json(self, path: str):
        code, body, _ = self.get(path)
        return code, json.loads(body.decode("utf-8"))

    def post_json(self, path: str, payload: dict):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    # ---------- 用例 ----------
    def test_health(self):
        code, d = self.get_json("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        self.assertGreater(d["probeRows"], 0)

    def test_overview(self):
        """总览已改为「三层诊断」结构：顶部结论 + 探测元信息 + 状态矩阵。"""
        code, d = self.get_json("/api/overview")
        self.assertEqual(code, 200)
        for k in ("headline", "probe", "matrix", "rules_version", "window_hours"):
            self.assertIn(k, d)
        self.assertEqual(len(d["matrix"]), 1, "一个接口对应矩阵一行")
        row = d["matrix"][0]
        for k in ("endpoint", "final", "final_label", "severity", "layers", "recommendation"):
            self.assertIn(k, row)
        for layer in ("l1", "l2", "l3"):
            self.assertIn(layer, row["layers"], "矩阵行必须给出三层判定")

    def test_probes_list(self):
        code, d = self.get_json("/api/probes?limit=5")
        self.assertEqual(code, 200)
        self.assertTrue(d["items"])
        self.assertGreater(d["total"], 0)
        # 下划线字段应已转驼峰或保留，且不含明文凭证
        self.assertNotIn("sk-", json.dumps(d))

    def test_endpoints_exposes_context_window(self):
        code, d = self.get_json("/api/endpoints")
        self.assertEqual(code, 200)
        it = d["items"][0]
        self.assertEqual(it["contextMax"], 262144)
        self.assertEqual(it["modelCount"], 1)
        self.assertEqual(it["quotaSource"], "API")

    def test_endpoint_detail_structure(self):
        code, d = self.get_json("/api/endpoints/1/detail")
        self.assertEqual(code, 200)
        for k in ("endpoint", "series", "models", "quotas", "utilization"):
            self.assertIn(k, d)
        self.assertEqual(d["models"][0]["context_length"], 262144)

    def test_rankings_agent(self):
        code, d = self.get_json("/api/rankings?scene=agent")
        self.assertEqual(code, 200)
        self.assertEqual(d["scene"], "agent")
        self.assertTrue(d["items"], "有 3 条 HEALTHY 样本应能出榜")
        it = d["items"][0]
        for k in ("endpoint", "model", "score", "breakdown", "n"):
            self.assertIn(k, it)
        self.assertIn("availability", it["breakdown"])

    def test_rankings_stable_maps_to_longterm(self):
        code, d = self.get_json("/api/rankings?scene=stable")
        self.assertEqual(code, 200)
        self.assertEqual(d["scene"], "longterm")
        self.assertIn("quota", d["weights"])

    def test_rankings_rejects_bad_scene(self):
        code, _ = self.get_json("/api/rankings?scene=nope")
        self.assertEqual(code, 422)

    def test_catalog(self):
        code, d = self.get_json("/api/catalog")
        self.assertEqual(code, 200)
        self.assertTrue(any(i["provider"] == "P-A" for i in d["items"]))
        self.assertEqual(d["items"][0]["allow_proxy"], "UNKNOWN")

    def test_reminders_readonly(self):
        code, d = self.get_json("/api/reminders")
        self.assertEqual(code, 200)
        self.assertIn("items", d)
        # 只读接口不应写提醒日志
        conn = db.connect(self.db_path)
        n = conn.execute("SELECT COUNT(*) c FROM reminder_log").fetchone()["c"]
        conn.close()
        self.assertEqual(n, 0)

    def test_config_blocked_without_allow(self):
        code, d = self.get_json("/api/config?scene=agent")
        self.assertEqual(code, 200)
        self.assertEqual(d["count"], 0, "allow_proxy 未核实不得产出配置")
        self.assertTrue(d["excluded"])

    def test_export_csv(self):
        """导出面向最终用户，表头用中文并带 BOM（Excel 双击不乱码）。"""
        code, body, headers = self.get("/api/probes/export")
        self.assertEqual(code, 200)
        # http.client 的 headers 是大小写不敏感的
        self.assertIn("csv", (headers.get("Content-Type") or "").lower())
        head = body[:400].decode("utf-8-sig")
        self.assertIn("接口", head)
        self.assertIn("状态", head)


class ReminderStateApiTest(unittest.TestCase):
    """提醒状态机在 HTTP 层的端到端行为：告警 → 恢复中 → 销号。

    单独一个类、单独的临时库，因为要反复改写 endpoint.expires_at 与
    probe_result 来"制造事故"和"制造恢复"，不能污染 ApiTest 的固定数据集。
    状态文件与确认记录同样重定向到临时目录，绝不碰 data/。
    """

    @classmethod
    def setUpClass(cls):
        from hub import reminder_state, settings
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.tmp.name)
        cls.db_path = cls.root / "rs.db"
        cls._seed()

        # 四处落盘路径全部改到临时区
        cls._saved = (api.DB_PATH, api.REMINDER_ACTIONS_FILE, api.NOTIFY_LOG_FILE,
                      reminder_state.STATE_FILE, settings.SETTINGS_FILE)
        api.DB_PATH = cls.db_path
        api.REMINDER_ACTIONS_FILE = cls.root / "actions.json"
        api.NOTIFY_LOG_FILE = cls.root / "notify_log.json"
        reminder_state.STATE_FILE = cls.root / "state.json"
        settings.SETTINGS_FILE = cls.root / "settings.json"

        cls.port = _free_port()
        cfg = uvicorn.Config(api.app, host="127.0.0.1", port=cls.port,
                             log_level="error", lifespan="on")
        cls.server = uvicorn.Server(cfg)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if getattr(cls.server, "started", False):
                break
            time.sleep(0.05)
        if not cls.server.started:
            raise RuntimeError("API 未能在预期时间内启动")

        # 探测批次由测试自己掌控：不真的探测，也能精确控制"这是第几轮"
        cls._round_n = 0
        cls._saved_round_id = api._probe_round_id
        api._probe_round_id = lambda conn: f"test-r{cls._round_n}"

    @classmethod
    def tearDownClass(cls):
        from hub import reminder_state, settings
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        api._probe_round_id = cls._saved_round_id
        (api.DB_PATH, api.REMINDER_ACTIONS_FILE, api.NOTIFY_LOG_FILE,
         reminder_state.STATE_FILE, settings.SETTINGS_FILE) = cls._saved
        cls.tmp.cleanup()

    @classmethod
    def _seed(cls):
        """一个干净接口：无到期、无低额度、最近探针 HEALTHY（不会被误报）。"""
        conn = db.connect(cls.db_path)
        db.seed_endpoints(conn, [{
            "provider": "P-RS", "label": "EP-RS", "base_url": "http://x/v1",
            "protocol": "openai_chat", "auth_env": "K", "default_model": "m1",
        }])
        ep = conn.execute("SELECT id FROM endpoint WHERE label='EP-RS'").fetchone()
        conn.execute(
            """INSERT INTO probe_result(endpoint_id,model_id,level,status,total_ms,
                                        response_shape,created_at)
               VALUES(?,?,?,?,?,?,datetime('now','localtime'))""",
            (ep["id"], None, "L0", "HEALTHY", 120, "json"))
        conn.commit()
        cls.ep_id = ep["id"]
        conn.close()

    # ---------- 制造 / 解除事故 ----------
    def _set_expired(self, on: bool):
        conn = db.connect(self.db_path)
        conn.execute("UPDATE endpoint SET expires_at=? WHERE id=?",
                     ("2020-01-01T00:00:00" if on else None, self.ep_id))
        conn.commit()
        conn.close()

    def _set_auth_fail(self, n: int):
        """插入 n 条 AUTH_FAILED 探针记录；n=0 表示全部删掉（模拟恢复）。"""
        conn = db.connect(self.db_path)
        conn.execute("DELETE FROM probe_result WHERE endpoint_id=? AND status='AUTH_FAILED'",
                     (self.ep_id,))
        for _ in range(n):
            conn.execute(
                """INSERT INTO probe_result(endpoint_id,model_id,level,status,total_ms,
                                            created_at)
                   VALUES(?,?,?,?,?,datetime('now','localtime'))""",
                (self.ep_id, "m1", "L2", "AUTH_FAILED", 80))
        conn.commit()
        conn.close()

    def get_reminders(self) -> dict:
        path = f"/api/reminders?t={time.time()}"   # 绕开任何缓存
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def _by_kind(self, d, kind):
        return next((i for i in d["items"] if i["kind"] == kind), None)

    @classmethod
    def _new_round(cls):
        """模拟"又跑了一轮探测"。

        真实世界里每探测一次，probe_result 就多几条记录，批次标识随之改变；
        状态机靠这个区分"真的又坏了一轮"和"用户只是又刷了一次页面"。
        测试里不发真请求，直接把这个标识往前推一格。
        """
        cls._round_n += 1

    def post_settings(self, payload: dict) -> dict:
        url = f"http://127.0.0.1:{self.port}/api/settings"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---------- 用例 ----------
    def test_01_baseline_is_quiet(self):
        d = self.get_reminders()
        self.assertEqual(d["count"], 0, "干净接口不该产出提醒")
        self.assertEqual(d["summary"], {"pending": 0, "alerting": 0, "recovering": 0})
        self.assertEqual(d["actionable"], 0)
        self.assertEqual(d["tuning"], {"confirmN": 2, "recoverM": 2})

    def test_02_fact_kind_alerts_immediately_and_recovers(self):
        """到期是确定性事实：首见即告警，不需要待确认期。"""
        self._set_expired(True)
        self._new_round()
        d = self.get_reminders()
        it = self._by_kind(d, "EXPIRED")
        self.assertIsNotNone(it, "已过期接口应产出 EXPIRED 提醒")
        self.assertEqual(it["state"], "ALERTING")
        self.assertEqual(it["state_label"], "持续告警")
        self.assertEqual(it["seen"], 1, "事实类首见即确认，不经过待确认")
        self.assertEqual(d["summary"]["alerting"], 1)
        self.assertEqual(d["actionable"], 1)

        # 刷新页面（同一批次）不该推进任何东西
        d = self.get_reminders()
        self.assertEqual(self._by_kind(d, "EXPIRED")["seen"], 1)

        # 恢复正常 → 进入恢复中，仍在列表里（要告诉用户"正在恢复"）
        self._set_expired(False)
        self._new_round()
        d = self.get_reminders()
        it = self._by_kind(d, "EXPIRED")
        self.assertIsNotNone(it)
        self.assertEqual(it["state"], "RECOVERING")
        self.assertEqual(d["summary"]["recovering"], 1)
        self.assertEqual(d["actionable"], 0, "恢复中不算待处理")

        # 连续 M=2 轮正常 → 销号，列表清空
        self._new_round()
        d = self.get_reminders()
        self.assertEqual(d["count"], 0)
        self.assertTrue(any(t["to"] is None for t in d["transitions"]),
                        "销号应出现在 transitions 里")
        self.assertEqual(d["summary"], {"pending": 0, "alerting": 0, "recovering": 0})

    def test_03_signal_kind_needs_confirmation(self):
        """鉴权失败来自探测，会抖：先待确认，第二轮才升级为告警。"""
        self._set_auth_fail(3)

        self._new_round()
        d = self.get_reminders()
        it = self._by_kind(d, "AUTH_FAILED")
        self.assertIsNotNone(it)
        self.assertEqual(it["state"], "PENDING", "第一次出现不该立刻报警")
        self.assertEqual(it["seen"], 1)
        self.assertEqual(d["summary"]["pending"], 1)
        self.assertEqual(d["actionable"], 0)

        # 用户刷新了三次页面：同一批次，不该被刷成告警
        for _ in range(3):
            d = self.get_reminders()
            self.assertEqual(self._by_kind(d, "AUTH_FAILED")["state"], "PENDING")

        # 真的又探测了一轮，这才确认
        self._new_round()
        d = self.get_reminders()
        it = self._by_kind(d, "AUTH_FAILED")
        self.assertEqual(it["state"], "ALERTING", "连续 N=2 次后确认故障")
        self.assertEqual(it["seen"], 2)
        self.assertEqual(d["actionable"], 1)

        # 恢复：告警 → 恢复中 → 销号
        self._set_auth_fail(0)
        self._new_round()
        d = self.get_reminders()
        it = self._by_kind(d, "AUTH_FAILED")
        self.assertEqual(it["state"], "RECOVERING")
        self._new_round()
        d = self.get_reminders()
        self.assertIsNone(self._by_kind(d, "AUTH_FAILED"))

    def test_04_thresholds_from_settings(self):
        """阈值可调：N=1 时首见即告警（待确认期关闭）。"""
        from hub.settings import save_settings
        save_settings({"reminderConfirmN": 1, "reminderRecoverM": 5})
        try:
            self._set_auth_fail(3)
            self._new_round()
            d = self.get_reminders()
            self.assertEqual(d["tuning"], {"confirmN": 1, "recoverM": 5})
            it = self._by_kind(d, "AUTH_FAILED")
            self.assertEqual(it["state"], "ALERTING", "N=1 关闭待确认期")
        finally:
            save_settings({"reminderConfirmN": 2, "reminderRecoverM": 2})
            self._set_auth_fail(0)

    def test_05_settings_patch_does_not_clobber_prompt(self):
        """只改阈值不该把自定义测试问题写空。

        回归点：update_settings 曾把 probePrompt 无条件拼进 patch，
        于是"保存提醒阈值"会连带清空测试问题。
        """
        r = self.post_settings({"probePrompt": "say hi"})
        self.assertTrue(r["isCustom"])

        r = self.post_settings({"reminderConfirmN": 5, "reminderRecoverM": 3})
        self.assertEqual(r["probePrompt"], "say hi", "改阈值不应清空测试问题")
        self.assertEqual((r["reminderConfirmN"], r["reminderRecoverM"]), (5, 3))

        # 显式传空字符串仍然表示"恢复默认问题"
        r = self.post_settings({"probePrompt": ""})
        self.assertFalse(r["isCustom"])
        self.assertEqual((r["reminderConfirmN"], r["reminderRecoverM"]), (5, 3),
                         "恢复默认问题不应顺带把阈值改回去")

        self.post_settings({"probePrompt": "", "reminderConfirmN": 2, "reminderRecoverM": 2})


class NotifyApiTest(unittest.TestCase):
    """通知渠道在 HTTP 层的端到端行为：存地址、脱敏回显、只发确认过的翻转。

    出网的部分全程被替换掉——断言的是"我们发了什么、发了几次"，
    不是"对方收没收到"。真实投递由 test_notify.py 的 payload 断言兜住。
    """

    @classmethod
    def setUpClass(cls):
        from hub import notify, reminder_state, settings
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.tmp.name)
        cls.db_path = cls.root / "notify.db"
        cls._seed()

        cls._saved = (api.DB_PATH, api.REMINDER_ACTIONS_FILE, api.NOTIFY_LOG_FILE,
                      reminder_state.STATE_FILE, settings.SETTINGS_FILE)
        api.DB_PATH = cls.db_path
        api.REMINDER_ACTIONS_FILE = cls.root / "actions.json"
        api.NOTIFY_LOG_FILE = cls.root / "notify_log.json"
        reminder_state.STATE_FILE = cls.root / "state.json"
        settings.SETTINGS_FILE = cls.root / "settings.json"

        # 拦下所有真实投递；测试里自己决定"对方接受还是拒绝"
        cls.posts = []
        cls.reply = {"ok": True}

        def fake_send(url, events, *, timeout=notify.TIMEOUT):
            cls.posts.append({"url": url, "events": events})
            return cls.reply["ok"]

        cls._patch = mock.patch.object(notify, "send_webhook", fake_send)
        cls._patch.start()

        cls.port = _free_port()
        cfg = uvicorn.Config(api.app, host="127.0.0.1", port=cls.port,
                             log_level="error", lifespan="on")
        cls.server = uvicorn.Server(cfg)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if getattr(cls.server, "started", False):
                break
            time.sleep(0.05)
        if not cls.server.started:
            raise RuntimeError("API 未能在预期时间内启动")

        cls._round_n = 0
        cls._saved_round_id = api._probe_round_id
        api._probe_round_id = lambda conn: f"nt-r{cls._round_n}"

    @classmethod
    def tearDownClass(cls):
        from hub import reminder_state, settings
        cls._patch.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        api._probe_round_id = cls._saved_round_id
        (api.DB_PATH, api.REMINDER_ACTIONS_FILE, api.NOTIFY_LOG_FILE,
         reminder_state.STATE_FILE, settings.SETTINGS_FILE) = cls._saved
        cls.tmp.cleanup()

    @classmethod
    def _seed(cls):
        conn = db.connect(cls.db_path)
        db.seed_endpoints(conn, [{
            "provider": "P-NT", "label": "EP-NT", "base_url": "http://x/v1",
            "protocol": "openai_chat", "auth_env": "K", "default_model": "m1",
        }])
        ep = conn.execute("SELECT id FROM endpoint WHERE label='EP-NT'").fetchone()
        conn.execute(
            """INSERT INTO probe_result(endpoint_id,model_id,level,status,total_ms,
                                        created_at)
               VALUES(?,?,?,?,?,datetime('now','localtime'))""",
            (ep["id"], "m1", "L2", "HEALTHY", 60))
        conn.commit()
        conn.close()
        cls.ep_id = ep["id"]

    # ---------- 工具 ----------
    def _open(self, path: str, method="GET", payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def get_settings(self):
        return self._open("/api/settings")[1]

    def post_settings(self, payload):
        return self._open("/api/settings", "POST", payload)

    def get_reminders(self):
        return self._open(f"/api/reminders?t={time.time()}")[1]

    @classmethod
    def _new_round(cls):
        cls._round_n += 1

    def _set_auth_fail(self, n: int):
        conn = db.connect(self.db_path)
        conn.execute("DELETE FROM probe_result WHERE endpoint_id=? AND status='AUTH_FAILED'",
                     (self.ep_id,))
        for _ in range(n):
            conn.execute(
                """INSERT INTO probe_result(endpoint_id,model_id,level,status,total_ms,
                                            created_at)
                   VALUES(?,?,?,?,?,datetime('now','localtime'))""",
                (self.ep_id, "m1", "L2", "AUTH_FAILED", 80))
        conn.commit()
        conn.close()

    @classmethod
    def setUp(cls):
        cls.posts.clear()
        cls.reply["ok"] = True

    @classmethod
    def _rewind_notified(cls, key: str, hours: int = 100):
        """把某条告警的"上次通知时点"拨回很久以前——模拟故障一直没恢复。

        直接改状态文件，比等 6 小时现实得多；状态文件的路径在 setUpClass
        里已被重定向到临时目录。
        """
        from hub import reminder_state as rs
        st = json.loads(rs.STATE_FILE.read_text(encoding="utf-8"))
        st[key]["last_notified"] = (datetime.now(timezone.utc)
                                    - timedelta(hours=hours)).isoformat()
        rs.STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

    # ---------- 用例 ----------
    def test_01_default_is_page_only(self):
        """没配地址时提醒只在页面亮着，接口要如实说明"通知未开启"。"""
        s = self.get_settings()
        self.assertEqual(s["notifyWebhook"], "")
        self.assertFalse(s["notifyWebhookSet"])
        self.assertTrue(s["notifyRecover"], "默认也把恢复推出去")
        self.assertIsNone(s["lastNotify"], "一次都没发过")

        d = self.get_reminders()
        self.assertFalse(d["notify"]["enabled"])
        self.assertEqual(self.posts, [], "没配地址不该尝试投递")

    def test_02_invalid_address_is_rejected(self):
        code, body = self.post_settings({"notifyWebhook": "ftp://x/y"})
        self.assertEqual(code, 422)
        self.assertIn("http(s)", body["detail"])
        code, body = self.post_settings({"notifyWebhook": "qyapi.weixin.qq.com/x"})
        self.assertEqual(code, 422, "少了协议头的地址要当场拦下，别等发不出去才发现")

    def test_03_saved_address_is_masked_and_survives_roundtrip(self):
        """webhook 的 key 就是凭证：回显脱敏，页面原样提交回来不算改动。"""
        real = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRET-1234"
        r = self.post_settings({"notifyWebhook": real})[1]
        self.assertTrue(r["notifyWebhookSet"])
        self.assertNotIn("SECRET-1234", r["notifyWebhook"], "密钥不该回显")
        self.assertTrue(r["notifyWebhook"].endswith("?***"))

        # 页面把脱敏值原样提交回来 → 不能把 *** 存进去
        r = self.post_settings({"notifyWebhook": r["notifyWebhook"]})[1]
        self.assertTrue(r["notifyWebhookSet"])
        self.assertIn("SECRET-1234",
                      (self.root / "settings.json").read_text(encoding="utf-8"),
                      "密钥应原样保留在本地配置里")

        # 显式清空 = 关闭通知
        r = self.post_settings({"notifyWebhook": ""})[1]
        self.assertFalse(r["notifyWebhookSet"])

    def test_04_confirmed_failure_is_pushed_once(self):
        real = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=S"
        self.post_settings({"notifyWebhook": real})
        self._set_auth_fail(3)

        # 第一轮：鉴权失败刚出现，还在待确认期 → 不该打扰用户
        self.get_reminders()
        self.assertEqual(self.posts, [], "待确认期的抖动不该发通知")

        # 第二轮：连续命中，升级为持续告警 → 发一条
        self._new_round()
        d = self.get_reminders()
        self.assertEqual(len(self.posts), 1)
        events = self.posts[0]["events"]
        self.assertEqual([e["endpoint"] for e in events], ["EP-NT"])
        self.assertEqual(events[0]["to"], "ALERTING")
        self.assertTrue(d["notify"]["sent"])
        self.assertEqual(d["notify"]["count"], 1)

        # 同一批次里再刷几次页面：状态没变，不发第二条
        for _ in range(3):
            self.get_reminders()
        self.assertEqual(len(self.posts), 1, "重复读取不该重复推送")

        self._set_auth_fail(0)
        self._new_round()
        self.get_reminders()          # 告警 → 恢复中：中间态，不发
        self.assertEqual(len(self.posts), 1, "恢复观察期不该发")
        self._new_round()
        self.get_reminders()          # 恢复中 → 销号：发"已恢复"
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(self.posts[1]["events"][0]["to"], None)

        s = self.get_settings()
        self.assertIsNotNone(s["lastNotify"], "投递记录要能回查")
        self.assertTrue(s["lastNotify"]["sent"])

    def test_05_recover_push_can_be_switched_off(self):
        """只想在坏的时候被打扰，那就别推"已恢复"。"""
        self.post_settings({"notifyWebhook": "https://x/wh?key=1",
                            "notifyRecover": False})
        self._set_auth_fail(3)
        self.get_reminders()
        self._new_round()
        self.get_reminders()                      # 确认告警 → 发
        self.assertEqual(len(self.posts), 1)

        self._set_auth_fail(0)
        self._new_round()
        self.get_reminders()
        self._new_round()
        self.get_reminders()                      # 销号 → 不发
        self.assertEqual(len(self.posts), 1, "关掉恢复推送后只发故障")

        self.post_settings({"notifyWebhook": "", "notifyRecover": True})
        self._set_auth_fail(0)
        self._new_round()
        self.get_reminders()

    def test_06_test_button_reports_failure_honestly(self):
        code, body = self._open("/api/settings/notify-test", "POST", {})
        self.assertEqual(code, 422, "没填地址就点测试，要直接说清楚")
        self.assertIn("通知地址", body["detail"])

        self.post_settings({"notifyWebhook": "https://x/wh?key=2"})
        code, body = self._open("/api/settings/notify-test", "POST", {})
        self.assertEqual(code, 200)
        self.assertTrue(body["sent"])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["events"][0]["endpoint"], "测试消息")

        # 对方没接受 → 如实报错，而不是假装成功
        self.reply["ok"] = False
        code, body = self._open("/api/settings/notify-test", "POST", {})
        self.assertEqual(code, 502)
        self.assertIn("发送失败", body["detail"])

        self.post_settings({"notifyWebhook": ""})

    def test_07_long_fault_repeats_and_respects_mute(self):
        """故障久不恢复要能再喊一声；静音期内则不喊。

        完整链路：界面 → observe() → due_repeats() → 投递 → 静音过滤。
        这是"通知随时间失效"的正解——只在翻转时发一条，用户翻过去就没了。
        """
        self.post_settings({"notifyWebhook": "https://x/wh?key=rep"})
        self._set_auth_fail(3)

        self.get_reminders()                       # 第 1 轮：待确认期，不发
        self._new_round()
        d = self.get_reminders()                   # 第 2 轮：确认告警 → 发第 1 条
        self.assertEqual(len(self.posts), 1)
        key = next(i for i in d["items"] if i["kind"] == "AUTH_FAILED")["key"]
        self.assertEqual(d["repeats"], [], "刚确认的故障不该立刻重发")

        # 模拟"100 小时都没恢复"——到点该再喊一声
        self._rewind_notified(key)
        d = self.get_reminders()
        self.assertEqual([r["kind"] for r in d["repeats"]], ["AUTH_FAILED"])
        self.assertTrue(d["repeats"][0]["repeat"])
        self.assertEqual(len(self.posts), 2, "到点应真的推一条")
        ev = self.posts[1]["events"][0]
        self.assertTrue(ev.get("repeat"), "重发条目要带 repeat 标记")
        self.assertEqual(ev["to"], "ALERTING")

        # 计时已重置：紧接着再来一次不该重复
        self.get_reminders()
        self.assertEqual(len(self.posts), 2, "发过就该重置，别连着刷")

        # 静音：用户明说了"这段时间别打扰我"，重发必须服从
        self._rewind_notified(key)
        self._open("/api/reminders/ack", "POST",
                   {"key": key, "action": "snooze", "days": 3})
        d = self.get_reminders()
        self.assertEqual(d["repeats"], [], "静音期内不该再喊")
        self.assertEqual(len(self.posts), 2, "静音时也不该真的投递")

        # 关掉重发：即便时点很老，也一条都没有
        self._open("/api/reminders/ack", "POST",
                   {"key": key, "action": "snooze", "days": 0})
        self.post_settings({"notifyRepeatHours": 0})
        self._rewind_notified(key)
        d = self.get_reminders()
        self.assertEqual(d["repeats"], [], "关闭重发后不该再有")
        self.assertEqual(len(self.posts), 2)

        # 收尾：恢复默认设置、解除事故
        self.post_settings({"notifyRepeatHours": 6, "notifyWebhook": ""})
        self._set_auth_fail(0)
        self._new_round()
        self.get_reminders()


class BudgetApiTest(unittest.TestCase):
    """探测预算在 HTTP 层的暴露：用量回显、开关即时效、额度不被 seed 冲掉。

    这一层是"假开关"最容易被放过的地方——后端算了半天，页面拿不到；
    或者页面改了保存了，下一轮探测又被常量覆写。两个方向都要断言。
    """

    @classmethod
    def setUpClass(cls):
        from hub import reminder_state, settings
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.tmp.name)
        cls.db_path = cls.root / "budget_api.db"
        cls._seed()

        cls._saved = (api.DB_PATH, api.REMINDER_ACTIONS_FILE, api.NOTIFY_LOG_FILE,
                      reminder_state.STATE_FILE, settings.SETTINGS_FILE)
        api.DB_PATH = cls.db_path
        api.REMINDER_ACTIONS_FILE = cls.root / "actions.json"
        api.NOTIFY_LOG_FILE = cls.root / "notify_log.json"
        reminder_state.STATE_FILE = cls.root / "state.json"
        settings.SETTINGS_FILE = cls.root / "settings.json"

        cls.port = _free_port()
        cfg = uvicorn.Config(api.app, host="127.0.0.1", port=cls.port,
                             log_level="error", lifespan="on")
        cls.server = uvicorn.Server(cfg)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if getattr(cls.server, "started", False):
                break
            time.sleep(0.05)
        if not cls.server.started:
            raise RuntimeError("API 未能在预期时间内启动")

    @classmethod
    def tearDownClass(cls):
        from hub import reminder_state, settings
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        (api.DB_PATH, api.REMINDER_ACTIONS_FILE, api.NOTIFY_LOG_FILE,
         reminder_state.STATE_FILE, settings.SETTINGS_FILE) = cls._saved
        cls.tmp.cleanup()

    @classmethod
    def _seed(cls):
        conn = db.connect(cls.db_path)
        db.seed_endpoints(conn, [{
            "provider": "P-B", "label": "EP-B", "base_url": "http://x/v1",
            "protocol": "openai_chat", "auth_env": "K", "default_model": "m1",
            "daily_budget": 5,
        }])
        ep = conn.execute("SELECT id FROM endpoint WHERE label='EP-B'").fetchone()
        cls.ep_id = ep["id"]
        conn.commit()
        conn.close()

    # ---------- 工具 ----------
    def _open(self, path: str, method="GET", payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _endpoint_row(self) -> dict:
        code, d = self._open("/api/endpoints")
        self.assertEqual(code, 200)
        return next(i for i in d["items"] if i["label"] == "EP-B")

    def _consume(self, n: int):
        """造 n 条当日 L2 记录，模拟已经用掉的额度。"""
        conn = db.connect(self.db_path)
        conn.execute("DELETE FROM probe_result WHERE endpoint_id=?", (self.ep_id,))
        for _ in range(n):
            conn.execute(
                """INSERT INTO probe_result(endpoint_id,model_id,level,status,total_ms,
                                            created_at)
                   VALUES(?,?,?,?,?,datetime('now','localtime'))""",
                (self.ep_id, "m1", "L2", "HEALTHY", 90))
        conn.commit()
        conn.close()

    def setUp(self):
        # 每个用例从「预算开启、额度 5」的干净态开始
        self._open("/api/settings", "POST",
                   {"budgetEnabled": True, "budgetGuardPct": 90})
        self._consume(0)

    def tearDown(self):
        self._consume(0)

    # ---------- 用例 ----------
    def test_01_endpoints_expose_budget_state(self):
        it = self._endpoint_row()
        self.assertEqual(it["dailyBudget"], 5)
        self.assertTrue(it["budgetEnabled"])
        self.assertEqual(it["budgetToday"], 0)
        self.assertEqual(it["budgetRemaining"], 5)
        self.assertEqual(it["budgetState"], "ok")

    def test_02_usage_reflected_in_state(self):
        self._consume(3)  # 3/5，未到 90%（阈值 4）
        self.assertEqual(self._endpoint_row()["budgetState"], "ok")

        self._consume(4)  # 4/5 命中阈值，进入降级
        self.assertEqual(self._endpoint_row()["budgetState"], "guarded")

        self._consume(5)  # 5/5 用尽
        it = self._endpoint_row()
        self.assertEqual(it["budgetToday"], 5)
        self.assertEqual(it["budgetRemaining"], 0)
        self.assertEqual(it["budgetState"], "exhausted")

    def test_03_guard_threshold_is_configurable(self):
        """阈值必须真跟着设置走，否则又是个改不动的假开关。"""
        self._open("/api/settings", "POST", {"budgetGuardPct": 50})
        self._consume(1)  # 1/5，未到 50%（阈值 2）
        self.assertEqual(self._endpoint_row()["budgetState"], "ok")
        self._consume(2)  # 2/5 命中 50%
        self.assertEqual(self._endpoint_row()["budgetState"], "guarded")

    def test_04_switch_off_reports_off(self):
        self._consume(5)
        code, d = self._open("/api/settings", "POST", {"budgetEnabled": False})
        self.assertEqual(code, 200)
        self.assertFalse(d["budgetEnabled"])
        it = self._endpoint_row()
        self.assertFalse(it["budgetEnabled"])
        self.assertEqual(it["budgetState"], "off", "关掉开关后页面不该再显示限额压力")

    def test_05_settings_roundtrip_over_http(self):
        self._open("/api/settings", "POST",
                   {"budgetEnabled": False, "budgetGuardPct": 33})
        code, d = self._open("/api/settings")
        self.assertEqual(code, 200)
        self.assertFalse(d["budgetEnabled"])
        self.assertEqual(d["budgetGuardPct"], 33)

    def test_06_guard_pct_out_of_range_rejected(self):
        code, _ = self._open("/api/settings", "POST", {"budgetGuardPct": 0})
        self.assertEqual(code, 422)
        code, _ = self._open("/api/settings", "POST", {"budgetGuardPct": 101})
        self.assertEqual(code, 422)

    def test_07_daily_budget_survives_reseed(self):
        """页面改的额度要顶得住下一轮探测：seed 每轮都跑，不能按常量覆写。"""
        code, _ = self._open("/api/endpoints", "POST", {
            "label": "EP-B", "provider": "P-B", "base_url": "https://x/v1",
            "protocol": "openai_chat", "auth_env": "K_KEY", "default_model": "m1",
            "daily_budget": 7,
        })
        self.assertEqual(code, 201)
        self.assertEqual(self._endpoint_row()["dailyBudget"], 7)

        # 模拟新一轮探测前的幂等 seed（代码里的默认是 200）
        conn = db.connect(self.db_path)
        db.seed_endpoints(conn, [{
            "provider": "P-B", "label": "EP-B", "base_url": "https://x/v1",
            "protocol": "openai_chat", "auth_env": "K_KEY", "default_model": "m1",
            "daily_budget": 200,
        }])
        conn.close()
        self.assertEqual(self._endpoint_row()["dailyBudget"], 7, "用户设的额度被覆写了")

    def test_08_create_rejects_bad_budget(self):
        code, body = self._open("/api/endpoints", "POST", {
            "label": "EP-BAD", "provider": "P-B", "base_url": "https://x/v1",
            "protocol": "openai_chat", "auth_env": "K_KEY", "daily_budget": 0,
        })
        self.assertEqual(code, 422)
        self.assertIn("daily_budget", json.dumps(body), "报错要指向额度字段，而不是别的校验")

    def _quota(self, **cols):
        """往 EP-B 追加一条额度快照。"""
        keys = list(cols)
        conn = db.connect(self.db_path)
        conn.execute(
            f"""INSERT INTO quota_snapshot(endpoint_id,{','.join(keys)})
                VALUES({','.join('?' * (len(keys) + 1))})""",
            (self.ep_id, *[cols[k] for k in keys]))
        conn.commit()
        conn.close()

    def _clear_quota(self):
        conn = db.connect(self.db_path)
        conn.execute("DELETE FROM quota_snapshot WHERE endpoint_id=?", (self.ep_id,))
        conn.commit()
        conn.close()

    def test_09_rate_limit_row_is_not_the_quota(self):
        """速率行既不能上屏当额度，也不能触发额度闸门。

        HEADER 抓取先写 DAILY/USD 预算行、再写 RATE_LIMIT/REQUEST 速率行，于是
        「最新一条」恒为速率行。旧实现照最新一条取，结果是：额度列显示「19/20 次」
        且**没有重置时间**（速率行不带 reset_at），额度闸门按 95% 误判降级。
        """
        self._clear_quota()
        self._quota(quota_type="DAILY", unit="USD", total=1.0, remaining=0.9998,
                    source="HEADER", quota_scope="ACCOUNT",
                    reset_at="2026-11-24 00:00:00")
        self._quota(quota_type="RATE_LIMIT", unit="REQUEST", total=20.0,
                    remaining=1.0, source="HEADER", quota_scope="ACCOUNT")

        it = self._endpoint_row()
        self.assertEqual(it["quota"]["quota_type"], "DAILY", "额度列该显示真预算")
        self.assertEqual(it["quota"]["reset_at"], "2026-11-24 00:00:00",
                         "必须带上重置时间——用户看不到它就会以为坏了")
        self.assertEqual(it["quotaSource"], "HEADER")
        self.assertLess(it["quotaUsedPct"], 1, "按真实预算算：用了 0.02%")
        self.assertEqual(it["budgetState"], "ok", "速率行不得触发降级")

    def test_10_display_keeps_total_only_row(self):
        """展示口径比预算口径宽：只录了总额（remaining 留空）的行仍要显示出来，
        否则用户填过的数字会从界面上消失；但算不出比例，闸门照旧让位。"""
        self._clear_quota()
        self._quota(quota_type="TOTAL", unit="USD", total=100.0,
                    source="MANUAL", quota_scope="ACCOUNT")

        it = self._endpoint_row()
        self.assertEqual(it["quota"]["total"], 100.0)
        self.assertIsNone(it["quota"]["remaining"])
        self.assertEqual(it["quotaSource"], "MANUAL")
        self.assertIsNone(it["quotaUsedPct"], "算不出已用比例 → 闸门让位")
        self.assertEqual(it["budgetState"], "ok")

    def test_11_manual_subscription_still_displays(self):
        """手工录的订阅额度（`SUBSCRIPTION`）不能被当成速率行滤掉。

        这是类型拆分的回归用例：`SUBSCRIPTION` 曾被速率行借用，任何「按类型排除
        SUBSCRIPTION」的写法都会连用户填的订阅权益一起误伤。拆出 `RATE_LIMIT` 后，
        `SUBSCRIPTION` 是真额度——要上屏、要带重置时间、要参与额度闸门。
        """
        self._clear_quota()
        self._quota(quota_type="RATE_LIMIT", unit="REQUEST", total=20.0,
                    remaining=1.0, source="HEADER", quota_scope="ACCOUNT")
        self._quota(quota_type="SUBSCRIPTION", unit="REQUEST", total=1000.0,
                    remaining=250.0, source="MANUAL", quota_scope="ACCOUNT",
                    reset_at="2026-12-01 00:00:00")

        it = self._endpoint_row()
        self.assertEqual(it["quota"]["quota_type"], "SUBSCRIPTION",
                         "订阅额度要盖过速率行上屏")
        self.assertEqual(it["quota"]["total"], 1000.0)
        self.assertEqual(it["quota"]["reset_at"], "2026-12-01 00:00:00")
        self.assertEqual(it["quotaSource"], "MANUAL")
        self.assertAlmostEqual(it["quotaUsedPct"], 75.0, places=1,
                               msg="订阅额度要照常算已用比例（用了 750/1000）")


class _FakeScheduler:
    """只记录 add_job 调用，不真的跑调度——测试装配参数用。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def add_job(self, fn, trigger, **kw):
        self.calls.append((trigger, kw))


class ScheduleGridTest(unittest.TestCase):
    """定时探测的「墙上时钟对齐」与「启动补跑」。

    背景（2026-11-23 实查）：APScheduler 的 `interval` 触发器首次触发是
    「注册时刻 + 间隔」，不按墙上时钟对齐，于是每次重启都把间隔重新计时；
    本机日志见过 24 次启动，重启间隔短于一个间隔时定时轮次**永不触发**，
    页面数据越等越旧（实测出现过 3 小时空洞）。修法两条：
      ① 装配时显式给 `next_run_time` 钉到墙上时钟网格——重启不再漂移；
      ② 启动时若数据「到下次定时轮就已超期」则补跑一轮——重启频繁也能出数。
    """

    def test_grid_is_aligned_and_always_in_the_future(self):
        cases = [
            (30, datetime(2026, 11, 23, 10, 0, 0), datetime(2026, 11, 23, 10, 30)),
            (30, datetime(2026, 11, 23, 10, 7, 0), datetime(2026, 11, 23, 10, 30)),
            (30, datetime(2026, 11, 23, 10, 29, 59), datetime(2026, 11, 23, 10, 30)),
            # 正好落在网格点上时，要取**下一个**网格点——否则重启即立刻探测
            (30, datetime(2026, 11, 23, 10, 30, 0), datetime(2026, 11, 23, 11, 0)),
            (5, datetime(2026, 11, 23, 23, 58, 0), datetime(2026, 11, 24, 0, 0)),
            (60, datetime(2026, 11, 23, 10, 59, 59), datetime(2026, 11, 23, 11, 0)),
        ]
        for step, now, want in cases:
            got = api._next_grid_time(step, now)
            self.assertEqual(got, want, f"间隔 {step} 分钟、从 {now} 出发应得 {want}")
            self.assertGreater(got, now, "必须是未来时刻")
            self.assertEqual(got.minute % step, 0, "必须落在墙上时钟网格上")
            self.assertEqual(got.second, 0)

    def test_schedule_arm_is_wall_clock_aligned(self):
        """装配必须显式传 next_run_time，否则重启又会把间隔重新计时。"""
        sched = _FakeScheduler()
        with mock.patch.object(api, "STATE") as st:
            st.scheduler = sched
            api._apply_schedule(True, 30)
        self.assertEqual(len(sched.calls), 1)
        trigger, kw = sched.calls[0]
        self.assertEqual(trigger, "interval")
        self.assertEqual(kw["id"], "probe")
        nrt = kw.get("next_run_time")
        self.assertIsInstance(nrt, datetime, "必须显式对齐，不能交给 interval 自己算")
        self.assertEqual(nrt.minute % 30, 0)
        self.assertGreater(nrt, datetime.now())

    def test_catchup_armed_only_when_data_would_be_stale(self):
        """补跑条件：到下次定时轮开始时，数据必然已超过一个间隔没更新。"""
        now = datetime.now()

        sched = _FakeScheduler()
        with mock.patch.object(api, "STATE") as st, \
                mock.patch.object(api, "_last_probe_at",
                                  return_value=now - timedelta(minutes=1)):
            st.scheduler = sched
            api._arm_startup_catchup(30)
        self.assertEqual(sched.calls, [], "数据还新，不该补跑（否则重启变成刷探测）")

        sched2 = _FakeScheduler()
        with mock.patch.object(api, "STATE") as st, \
                mock.patch.object(api, "_last_probe_at",
                                  return_value=now - timedelta(hours=5)):
            st.scheduler = sched2
            api._arm_startup_catchup(30)
        self.assertEqual(len(sched2.calls), 1, "数据陈旧，应补一轮")
        trigger, kw = sched2.calls[0]
        self.assertEqual(trigger, "date")
        self.assertEqual(kw["id"], "probe_catchup")
        self.assertGreater(kw["run_date"], now, "延后起跑，别和启动流程争资源")

    def test_no_catchup_on_empty_db_or_no_scheduler(self):
        """库为空（首次部署）或调度器不在时都不补，交给正常调度。"""
        sched = _FakeScheduler()
        with mock.patch.object(api, "STATE") as st, \
                mock.patch.object(api, "_last_probe_at", return_value=None):
            st.scheduler = sched
            api._arm_startup_catchup(30)
        self.assertEqual(sched.calls, [], "空库不补")

        sched2 = _FakeScheduler()
        with mock.patch.object(api, "STATE") as st, \
                mock.patch.object(api, "_last_probe_at",
                                  return_value=datetime.now() - timedelta(days=1)):
            st.scheduler = None
            api._arm_startup_catchup(30)  # 不应抛异常
        self.assertEqual(sched2.calls, [], "无调度器时不补（也不该炸）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
