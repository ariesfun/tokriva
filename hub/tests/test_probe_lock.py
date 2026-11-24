"""探测并发锁：同一时刻只允许一轮 hub.run --once。

对应提交 7ef4e3e（起因：连点「立即检测」并行跑了两轮，第二轮若干接口
网络大面积失败）。这里**不 spawn 真实探测**——不耗额度、不依赖网络、
不碰真实库——用假的 Popen 验证四条关键行为：

  ① 忙碌时手动触发回 409，且不再拉起子进程；
  ② 异步路径在子进程存活期间一直持锁，进程结束才释放，释放后可再次触发；
  ③ 定时轮撞上手动轮时立刻跳过本轮（不 spawn、不阻塞）；
  ④ 拉起失败(Popen 抛错)必须归还锁，否则此后再也发不起探测（永久锁死）。

为什么要专门测锁：锁这种东西「看起来对」和「真的对」差得很远——把
finally 放在错误的位置会提前放锁（等于没锁），漏掉异常分支会永久锁死。
两种都只在并发下才暴露，靠人工点按钮几乎测不出来。
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import HTTPException  # noqa: E402

from hub import api  # noqa: E402


class FakeProc:
    """假子进程：wait() 阻塞一小段时间后返回，模拟真实探测的存活期。"""

    pid = 424242

    def __init__(self, delay: float = 0.15):
        self._delay = delay

    def wait(self, *a, **k):  # noqa: A003 - 对齐 subprocess.Popen 接口
        time.sleep(self._delay)
        return 0


class ProbeLockTest(unittest.TestCase):
    def setUp(self):
        # 每个用例拿一把干净的锁，避免用例间互相影响
        self._saved_lock = api._PROBE_LOCK
        api._PROBE_LOCK = threading.Lock()

    def tearDown(self):
        # 还回原来的锁；顺手把可能遗留的占用清掉
        try:
            if api._PROBE_LOCK.locked():
                api._PROBE_LOCK.release()
        except Exception:
            pass
        api._PROBE_LOCK = self._saved_lock

    def test_busy_manual_run_rejected_without_spawning(self):
        """已在跑时再点「立即检测」→ 409，且不得再起一个进程。"""
        api._PROBE_LOCK.acquire()
        with mock.patch("subprocess.Popen") as popen:
            with self.assertRaises(HTTPException) as cm:
                api.run_probe(api.ProbeRequest(async_run=True))
        self.assertEqual(cm.exception.status_code, 409)
        popen.assert_not_called()

    def test_async_holds_lock_until_child_exits(self):
        """子进程存活期间必须持锁；结束后自动释放并可再次触发。"""
        with mock.patch("subprocess.Popen", return_value=FakeProc(delay=0.15)):
            api.run_probe(api.ProbeRequest(async_run=True))
            self.assertTrue(api._PROBE_LOCK.locked(),
                            "子进程还在跑，锁不该放开（放开=等于没锁）")
            time.sleep(0.4)
            self.assertFalse(api._PROBE_LOCK.locked(),
                             "子进程已结束，看护线程应已放锁")
            # 释放后应能再发起一轮
            api.run_probe(api.ProbeRequest(async_run=True))
        time.sleep(0.4)

    def test_scheduled_probe_skips_when_busy(self):
        """定时轮撞上手动轮：跳过本轮，不 spawn、不等待。"""
        api._PROBE_LOCK.acquire()
        t0 = time.time()
        with mock.patch("subprocess.run") as run:
            api._scheduled_probe()
        self.assertLess(time.time() - t0, 0.5, "忙碌时应立刻返回，而不是排队等待")
        run.assert_not_called()

    def test_spawn_failure_returns_lock(self):
        """拉起失败必须归还锁，否则该进程此后再也发不起探测。"""
        with mock.patch("subprocess.Popen", side_effect=OSError("spawn failed")):
            with self.assertRaises(OSError):
                api.run_probe(api.ProbeRequest(async_run=True))
        self.assertFalse(api._PROBE_LOCK.locked(), "拉起失败后必须归还锁")


if __name__ == "__main__":
    unittest.main()
