"""回放式测试：用假服务器验证分类与计时，不消耗真实额度。

与真实冒烟测试分开：这里断言的是"分类逻辑对给定字节流的判断"，
不验证服务商当前是否可用。
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hub import fake_server, probe  # noqa: E402


class FakeServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = fake_server.serve(port=0)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def ep(self, scenario: str, protocol: str = "openai_chat") -> dict:
        return {
            "id": 1,
            "label": f"fake-{scenario}",
            "base_url": f"http://127.0.0.1:{self.port}/{scenario}/v1",
            "protocol": protocol,
            "auth_env": "FAKE_KEY",
            "default_model": "fake-model",
            "use_proxy": 0,
            "timeout_sec": 20,
            "include_usage": 1,
            "has_utilization": 0,
            "has_usage": 0,
        }

    def run_l2(self, scenario: str, **kw):
        import os
        os.environ["FAKE_KEY"] = "sk-fake-for-test"
        return probe.probe_stream(self.ep(scenario), "fake-model",
                                  level="L2", max_tokens=32, **kw)

    # --- 分类断言 ---
    def test_ok_sse(self):
        r = self.run_l2("ok_sse_with_usage")
        self.assertEqual(r["status"], "HEALTHY")
        self.assertEqual(r["http_status"], 200)
        self.assertEqual(r["timing_scope"], "direct")

    def test_401(self):
        r = self.run_l2("unauthorized_401")
        self.assertEqual(r["status"], "AUTH_FAILED")
        self.assertEqual(r["http_status"], 401)

    def test_429_carries_retry_after(self):
        r = self.run_l2("rate_limited_429")
        self.assertEqual(r["status"], "RATE_LIMITED")
        self.assertEqual(r["retry_after"], 1.0)

    def test_403_quota_not_auth(self):
        """额度耗尽常以 403 返回，不能判成鉴权失败。"""
        r = self.run_l2("quota_403")
        self.assertEqual(r["status"], "QUOTA_EXHAUSTED")

    def test_500_is_endpoint_error(self):
        r = self.run_l2("server_500")
        self.assertEqual(r["status"], "ENDPOINT_ERROR")

    def test_cloudflare_html_is_response_invalid(self):
        """Packet 事故：403 + HTML 风控页。"""
        r = self.run_l2("cloudflare_403_html")
        self.assertEqual(r["status"], "RESPONSE_INVALID")
        self.assertEqual(r["error_kind"], "cloudflare_or_waf_html")

    def test_reasoning_starved(self):
        """LightVela 事故：正文为空、token 全被 reasoning 吃掉。"""
        r = self.run_l2("reasoning_starved")
        self.assertEqual(r["status"], "REASONING_STARVED")

    # --- 计时与流式判定 ---
    def test_slow_stream_is_true_stream(self):
        """分片间隔 400ms 应判为真流式，且 TTFT 明显小于总耗时。"""
        r = self.run_l2("slow_stream")
        self.assertEqual(r["status"], "HEALTHY")
        self.assertEqual(r["stream_mode"], "true_stream")
        self.assertIsNotNone(r["ttft_ms"])
        self.assertLess(r["ttft_ms"], r["total_ms"])

    def test_buffered_detection(self):
        """所有分片同一时刻到达 → 判 buffered，不给 TPS 假数字。"""
        r = self.run_l2("ok_sse_with_usage")
        self.assertEqual(r["stream_mode"], "buffered")
        self.assertIsNone(r["tps"])

    # --- 入库字段 ---
    def test_captures_headers_and_body(self):
        r = self.run_l2("ok_sse_with_usage")
        self.assertIn("x-ratelimit-limit-user-daily-usd", r["resp_headers"])
        self.assertIn("data:", r["resp_body"])
        self.assertTrue(r["probe_version"])

    def test_header_quota_extraction(self):
        """B 档映射按服务商查表：AMD 的头名与通用兜底不同。"""
        r = self.run_l2("ok_sse_with_usage")
        q = probe.extract_header_quota(r["resp_headers"], "AMD Radeon (官方直连)")
        self.assertTrue(q, "按 AMD 规则应解析出额度")
        self.assertEqual(probe.extract_header_quota(r["resp_headers"], "Unknown Provider"), [])
        daily = [x for x in q if x["quota_type"] == "DAILY"]
        self.assertTrue(daily)
        self.assertEqual(daily[0]["total"], 5.0)
        self.assertEqual(daily[0]["remaining"], 4.5)

    def test_real_amd_replay(self):
        """真实 AMD 抓包：整体缓冲下发，判 buffered 时不得给假 TPS。"""
        if "amd_real_stream" not in fake_server.SCENARIOS:
            self.skipTest("无真实 fixture")
        r = self.run_l2("amd_real_stream")
        self.assertEqual(r["status"], "HEALTHY")
        self.assertIn(r["stream_mode"], ("buffered", "single"))
        self.assertIsNone(r["tps"])

    def test_real_packet_replay(self):
        """真实 Packet 抓包：断言具体分类、分片数与解析出的正文长度。"""
        fx = fake_server.SCENARIOS.get("packet_real_stream")
        if not fx:
            self.skipTest("无真实 fixture")
        self.assertGreaterEqual(len(fx["chunks"]), 10, "抓包分片数异常，可能截断")

        r = self.run_l2("packet_real_stream")
        self.assertEqual(r["status"], "HEALTHY")
        self.assertEqual(r["http_status"], 200)
        self.assertIn(r["stream_mode"], ("single", "buffered", "true_stream"))
        # 关键字段必须存在，否则回归时容易漏报
        self.assertIsNotNone(r["total_ms"])
        self.assertGreater(r["total_ms"], 0)
        self.assertGreater(r["content_chars"], 0, "解析出的正文为空，说明流解析回归")
        self.assertIn("chat.completion.chunk", r["resp_body"])
        if r["stream_mode"] in ("single", "buffered"):
            self.assertIsNone(r["tps"])

    def test_fixtures_have_no_secrets(self):
        """fixture 会进仓库，必须确认没有凭证或账号标识残留。"""
        from hub import scrub
        d = Path(__file__).resolve().parents[2] / "fixtures"
        if not d.exists():
            self.skipTest("无 fixtures 目录")
        bad = {}
        for f in d.glob("*.json"):
            hits = scrub.find_suspects(f.read_text(encoding="utf-8"))
            if hits:
                bad[f.name] = hits[:3]
        self.assertEqual(bad, {}, f"fixture 含敏感残留: {bad}")

    def test_real_lightvela_replay_is_starved(self):
        """真实 LightVela 抓包回放：max_tokens=8 全是 reasoning。"""
        if "lightvela_reasoning_starved" not in fake_server.SCENARIOS:
            self.skipTest("无真实 fixture")
        r = self.run_l2("lightvela_reasoning_starved")
        self.assertEqual(r["status"], "REASONING_STARVED")

    def test_no_secret_leak_in_stored_body(self):
        r = self.run_l2("unauthorized_401")
        self.assertNotIn("sk-fake-for-test", r.get("resp_body") or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
