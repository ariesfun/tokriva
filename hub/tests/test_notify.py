"""通知模块测试：消息成型、该不该发、发不出去怎么办。

不真的联外网：send_webhook 的 POST 用假的 urlopen 顶掉，断言发出去的
payload 长什么样；dispatch 的取舍逻辑是纯函数，直接喂翻转列表。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hub import notify  # noqa: E402


def flip(to="ALERTING", endpoint="OpenRouter", **kw):
    base = {"key": f"X|{endpoint}", "kind": "AUTH_FAILED", "endpoint": endpoint,
            "from": "PENDING", "to": to,
            "to_label": {"ALERTING": "持续告警", "RECOVERING": "恢复中",
                         "PENDING": "待确认", None: "已恢复"}[to],
            "at": "2026-11-23T01:00:00+00:00", "reason": "出现次数达到确认阈值"}
    base.update(kw)
    return base


class CapturingResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class BuildMessageTest(unittest.TestCase):
    def test_content_carries_endpoint_and_reason(self):
        text = notify.build_content([flip()])
        self.assertIn("OpenRouter", text)
        self.assertIn("持续告警", text)
        self.assertIn("出现次数达到确认阈值", text)

    def test_recovery_line_reads_as_recovered(self):
        text = notify.build_content([flip(to=None, reason="连续 2 轮正常，销号")])
        self.assertIn("已恢复", text)
        self.assertIn("不再报警", text)

    def test_long_batch_is_truncated(self):
        many = [flip(endpoint=f"模型{i}") for i in range(200)]
        text = notify.build_content(many)
        self.assertLess(len(text), 1700, "正文要有上限，别把平台接口撑爆")
        self.assertIn("其余已省略", text)

    def test_payload_is_wecom_text_shape(self):
        p = notify.build_payload([flip()])
        self.assertEqual(p["msgtype"], "text")
        self.assertIn("content", p["text"])

    def test_repeat_line_reads_as_still_broken(self):
        """到期重发要和「刚刚坏了」区分开：用户扫一眼就知道这不是新故障。"""
        text = notify.build_content(
            [flip(repeat=True, reason="已持续 7.0 小时，仍未恢复")])
        self.assertIn("仍未恢复", text)
        self.assertNotIn("【持续告警】", text)
        self.assertIn("7.0 小时", text)


class DispatchTest(unittest.TestCase):
    def test_no_url_means_disabled(self):
        with mock.patch.object(notify, "send_webhook") as post:
            res = notify.dispatch([flip()], url="")
        self.assertEqual(res, {"sent": False, "count": 0, "skipped": 1})
        post.assert_not_called()

    def test_pending_and_recovering_are_not_pushed(self):
        """只有确认故障和销号值得打扰用户；中间态不发。"""
        events = [flip(), flip(to="RECOVERING"), flip(to="PENDING"), flip(to=None)]
        with mock.patch.object(notify, "send_webhook", return_value=True) as post:
            res = notify.dispatch(events, url="https://x/y")
        sent = post.call_args[0][1]
        self.assertEqual({e["to"] for e in sent}, {"ALERTING", None})
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["skipped"], 2)

    def test_recover_can_be_switched_off(self):
        events = [flip(), flip(to=None)]
        with mock.patch.object(notify, "send_webhook", return_value=True) as post:
            res = notify.dispatch(events, url="https://x/y", notify_recover=False)
        sent = post.call_args[0][1]
        self.assertEqual([e["to"] for e in sent], ["ALERTING"])
        self.assertEqual(res["skipped"], 1)

    def test_nothing_to_say_stays_quiet(self):
        events = [flip(to="PENDING")]
        with mock.patch.object(notify, "send_webhook") as post:
            res = notify.dispatch(events, url="https://x/y")
        self.assertEqual(res["count"], 0)
        post.assert_not_called()

    def test_repeat_events_are_delivered(self):
        """重发条目 to=ALERTING，应当和翻转一起被放行——别被筛掉。"""
        events = [flip(repeat=True)]
        with mock.patch.object(notify, "send_webhook", return_value=True) as post:
            res = notify.dispatch(events, url="https://x/y")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["skipped"], 0)
        self.assertTrue(post.call_args[0][1][0]["repeat"])


class SendWebhookTest(unittest.TestCase):
    def test_posts_json_utf8_and_reports_success(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode("utf-8"))
            seen["ctype"] = req.headers.get("Content-type") or req.headers.get("Content-Type")
            return CapturingResponse()

        with mock.patch.object(notify.urllib.request, "urlopen", fake_urlopen):
            ok = notify.send_webhook("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k",
                                     [flip(endpoint="GPT-5 预览")])
        self.assertTrue(ok)
        self.assertIn("application/json", seen["ctype"])
        self.assertIn("GPT-5 预览", seen["body"]["text"]["content"])

    def test_network_error_is_swallowed(self):
        """通知失败绝不该往上抛——探测和页面不能因为推送坏了而坏。"""
        with mock.patch.object(notify.urllib.request, "urlopen",
                               side_effect=OSError("connection refused")):
            self.assertFalse(notify.send_webhook("https://x/y", [flip()]))

    def test_non_2xx_is_failure(self):
        class Bad(CapturingResponse):
            status = 500

        with mock.patch.object(notify.urllib.request, "urlopen",
                               return_value=Bad()):
            self.assertFalse(notify.send_webhook("https://x/y", [flip()]))

    def test_empty_url_or_events_short_circuit(self):
        with mock.patch.object(notify.urllib.request, "urlopen") as post:
            self.assertFalse(notify.send_webhook("", [flip()]))
            self.assertFalse(notify.send_webhook("https://x/y", []))
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
