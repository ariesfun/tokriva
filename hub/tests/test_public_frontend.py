"""公开前端（`web/public/public.js`）的契约消费测试。

与相邻测试的分工：
    test_public_snapshot  守**后端**出口：哪些字段允许出门（白名单 + canary + 幂等）
    本文件                守**前端**消费：拿到的快照是否被正确地、安全地渲染出来

为什么值得单测：前端是唯一会被陌生人直接看到的产物，而它过去是**零覆盖**的
（见 docs/static-pages-assessment.md §七）。公开前端是独立只读页，最容易做成
可执行断言——只依赖一份 `snapshot.json` 与一段 DOM，无框架、无构建步骤。

做法：用真实构建器产出一份快照 → 铺一个最小站点目录（`public.js` + `data/snapshot.json`）
→ 跑 `web/public/check_public_ui.js`（自带极简 DOM 桩）→ 断言全部子检查通过。
`node` 缺失时跳过（CI 的 ubuntu-latest 自带 node）。

守的是报告 §10 的四条硬要求：
    · 视图白名单：Overview/Rankings/Providers/History/Methodology 可进，其余拒绝；
    · 四个错误码：PUBLIC_DATA_NOT_FOUND / PUBLIC_SCHEMA_UNSUPPORTED /
      PUBLIC_VIEW_FORBIDDEN / PUBLIC_READ_ONLY；
    · Overview 窗口严格匹配：缺 key 必须报错，不得静默回退到别的窗口；
    · 快照缺失 / 版本不符 → 显式「数据暂不可用」，**不得回退到演示数据**。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hub import db, public_snapshot  # noqa: E402

WEB_PUBLIC = Path(__file__).resolve().parents[2] / "web" / "public"
CHECKER = WEB_PUBLIC / "check_public_ui.js"
NODE = shutil.which("node")


@unittest.skipIf(NODE is None, "未安装 node，跳过公开前端检查")
class PublicFrontendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(cls.tmp.name)

        # --- 最小库：1 端点 / 1 模型 / 3 条新鲜 L2（满足 minSamples=2）+ 1 条 L0 ---
        db_path = root / "public.db"
        conn = db.connect(db_path)
        db.seed_endpoints(conn, [{
            "provider": "P-A", "label": "EP-A", "base_url": "http://intranet.local/v1",
            "protocol": "openai_chat", "auth_env": "SECRET_API_KEY",
            "default_model": "m1", "note": "internal",
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
                (ep["id"], "m1", "L2", "HEALTHY", 1000 + i * 10, 20 + i, 1500, "true_stream"))
        conn.execute(
            """INSERT INTO probe_result(endpoint_id,model_id,level,status,total_ms,
                                        response_shape,created_at)
               VALUES(?,?,?,?,?,?,datetime('now','localtime'))""",
            (ep["id"], None, "L0", "HEALTHY", 200, "json"))
        conn.commit()

        ro = db.connect_ro(db_path)
        try:
            snapshot = public_snapshot.build_public(ro, probes_limit=50)
        finally:
            ro.close()
        conn.close()

        # --- 铺最小站点目录：public.js（被测文件）+ data/snapshot.json ---
        cls.site = root / "site"
        (cls.site / "data").mkdir(parents=True, exist_ok=True)
        shutil.copy2(WEB_PUBLIC / "public.js", cls.site / "public.js")
        (cls.site / "data" / "snapshot.json").write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_frontend_passes_all_contract_checks(self):
        proc = subprocess.run(
            [NODE, str(CHECKER), "--site", str(self.site)],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        # 断言脚本自身跑通（否则把 node 的输出原样贴出来，便于定位）
        self.assertNotEqual(proc.returncode, 2,
                            f"检查脚本异常退出：\n{proc.stdout}\n{proc.stderr}")
        self.assertEqual(proc.returncode, 0,
                         f"公开前端契约检查存在失败项：\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("项通过", proc.stdout)

    def test_checker_rejects_missing_snapshot(self):
        """站点里没有快照时必须非零退出（守卫「不回退演示数据」的下游）。"""
        empty = Path(self.tmp.name) / "empty-site"
        empty.mkdir(exist_ok=True)
        shutil.copy2(WEB_PUBLIC / "public.js", empty / "public.js")
        proc = subprocess.run(
            [NODE, str(CHECKER), "--site", str(empty)],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
