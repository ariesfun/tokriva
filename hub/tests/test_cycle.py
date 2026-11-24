"""端到端集成测试：跑完整的 run_cycle 闭环。

与另两个测试文件的分工：
    test_probe_fake  单次探测的分类与计时
    test_core        单模块的排名/台账/下发逻辑
    本文件            把模块串起来跑，验证「探测写入 → 排名出榜 → 下发拦截/放行」

用假服务器 + 临时数据库，不触碰真实库与真实额度。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hub import db, export, fake_server, rank, run  # noqa: E402


class CycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = fake_server.serve(port=0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        os.environ["FAKE_CYCLE_KEY"] = "sk-fake-cycle"
        # Windows 上 SQLite 句柄释放稍有延迟，忽略清理失败
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)

        # 重定向：数据库、下发目录与 .env 都放临时区，不依赖仓库根目录的 .env
        self._saved = (run.DB_PATH, run.ENDPOINTS, export.OUT_DIR, run.ENV_PATH)
        run.DB_PATH = root / "cycle.db"
        export.OUT_DIR = root / "litellm"
        run.ENV_PATH = root / ".env"
        run.ENV_PATH.write_text("FAKE_CYCLE_KEY=sk-fake-cycle\n", encoding="utf-8")
        run.ENDPOINTS = [{
            "provider": "FakeVendor", "website": "http://127.0.0.1",
            "kind": "model_api", "label": "Fake-EP",
            "base_url": f"http://127.0.0.1:{self.port}/slow_stream/v1",
            "protocol": "openai_chat", "auth_env": "FAKE_CYCLE_KEY",
            "default_model": "fake-model", "use_proxy": 0,
            "daily_budget": 50, "utilization": 0, "usage": 0,
            "include_usage": 1, "timeout_sec": 15, "note": "集成测试",
        }]

    def tearDown(self):
        run.DB_PATH, run.ENDPOINTS, export.OUT_DIR, run.ENV_PATH = self._saved
        self.tmp.cleanup()
        os.environ.pop("FAKE_CYCLE_KEY", None)

    def _conn(self):
        return db.connect(run.DB_PATH)

    def test_full_cycle_writes_probes_and_blocks_dispatch(self):
        run.run_cycle()  # 第 1 轮

        conn = self._conn()
        levels = {r["level"] for r in conn.execute(
            "SELECT DISTINCT level FROM probe_result")}
        self.assertEqual(levels, {"L0", "L2"}, "L0 与 L2 都应落库")

        healthy = conn.execute(
            "SELECT COUNT(*) c FROM probe_result WHERE status='HEALTHY'").fetchone()["c"]
        self.assertGreater(healthy, 0)

        # 探测必须带来源与版本，便于事后核对
        row = conn.execute(
            "SELECT timing_scope, probe_version, resp_body FROM probe_result "
            "WHERE level='L2' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["timing_scope"], "direct")
        self.assertTrue(row["probe_version"])
        self.assertIn("data:", row["resp_body"] or "")

        # 下发必须被 allow_proxy 拦住（安全门）
        self.assertFalse((export.OUT_DIR / "latest.yaml").exists(),
                         "allow_proxy 未核实时不应产出配置")
        conn.close()

    def test_cycle_then_allow_then_dispatch(self):
        """两轮攒够样本 → 放行 → 应生成配置且 Key 走环境变量。"""
        run.run_cycle()
        run.run_cycle()

        conn = self._conn()
        snaps = conn.execute("SELECT COUNT(*) c FROM rank_snapshot").fetchone()["c"]
        self.assertGreater(snaps, 0, "排名快照应已落库")

        # 未放行前仍然拦
        self.assertFalse((export.OUT_DIR / "latest.yaml").exists())

        conn.execute(
            "INSERT INTO catalog_entry(provider_id,allow_proxy,source_type,verified_at) "
            "VALUES((SELECT id FROM provider WHERE name='FakeVendor'),'YES','official','2026-11-22')")
        conn.commit()
        conn.close()

        run.run_cycle()

        latest = export.OUT_DIR / "latest.yaml"
        self.assertTrue(latest.exists(), "放行后应产出配置")
        text = latest.read_text(encoding="utf-8")
        self.assertIn("model_list:", text)
        self.assertIn("os.environ/FAKE_CYCLE_KEY", text)
        self.assertNotIn("sk-fake-cycle", text, "配置里不得出现明文 Key")

    def test_rank_produces_eligible_after_two_rounds(self):
        run.run_cycle()
        run.run_cycle()
        conn = self._conn()
        board = rank.build(conn, "speed", persist=False)
        self.assertTrue(any(r["eligible"] for r in board),
                        "两轮后应至少有一项可排名")
        conn.close()

    def test_probe_failure_does_not_kill_cycle(self):
        """单个端点异常不应中断整轮（Packet 类无响应端点的保护）。"""
        bad = dict(run.ENDPOINTS[0])
        bad.update(label="Broken-EP", provider="BrokenVendor",
                   base_url="http://127.0.0.1:1/v1", timeout_sec=2)
        run.ENDPOINTS = run.ENDPOINTS + [bad]
        run.run_cycle()  # 不应抛异常

        conn = self._conn()
        n_bad = conn.execute(
            """SELECT COUNT(*) c FROM probe_result p JOIN endpoint e ON e.id=p.endpoint_id
               WHERE e.label='Broken-EP'""").fetchone()["c"]
        self.assertGreater(n_bad, 0, "失败也应落库，而不是整轮丢弃")
        # 正常端点仍应有数据
        n_ok = conn.execute(
            """SELECT COUNT(*) c FROM probe_result p JOIN endpoint e ON e.id=p.endpoint_id
               WHERE e.label='Fake-EP'""").fetchone()["c"]
        self.assertGreater(n_ok, 0)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
