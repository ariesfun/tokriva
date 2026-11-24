"""给 README 生成产品截图。

用 Playwright(chromium) 打开本地控制台各视图截图，输出到 web/preview/。
服务需先在 8766 端口运行（scripts/start.py）。
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "preview"
OUT.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:8766/"

# 要截的视图：hash -> 输出文件名
VIEWS = [
    ("", "overview.png"),           # 总览
    ("#rankings", "rankings.png"),  # 排名榜单
    ("#catalog", "catalog.png"),    # 接入条件
    ("#endpoints", "endpoints.png"),# 接口清单
    ("#probes", "probes.png"),      # 探测记录
]

WIDTH, HEIGHT = 1440, 900


def main() -> int:
    with sync_playwright() as p:
        # 优先用系统 Chrome（无需额外下载 playwright 浏览器）
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        page.goto(BASE, wait_until="networkidle", timeout=30000)
        # 等前端从 API 拉取数据完成
        page.wait_for_timeout(2500)

        for hash_, name in VIEWS:
            if hash_:
                page.goto(BASE + hash_, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(1500)
            path = OUT / name
            page.screenshot(path=str(path), full_page=False)
            print(f"[已截图] {name}")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
