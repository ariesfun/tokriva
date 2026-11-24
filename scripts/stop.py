"""一键关闭 Tokriva 控制台。

用法（项目根目录下）：
    python scripts/stop.py            # 按 data/server.pid 关闭，再按端口兜底
    python scripts/stop.py --port 8766

关闭顺序：先读 PID 文件杀进程，再按端口扫描兜底，确保不残留。
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PID_FILE = ROOT / "data" / "server.pid"
DEFAULT_PORT = 8766


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _kill_by_pid(pid: int) -> bool:
    try:
        # 中文 Windows 下 taskkill 输出是 GBK，用 errors="ignore" 避免解码失败
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, check=False, timeout=15,
                       encoding="gbk", errors="ignore")
        return True
    except Exception:
        return False


def _find_pid_by_port(port: int) -> int | None:
    """按端口找监听进程的 PID（Windows 用 netstat）。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, timeout=15,
            check=False, encoding="gbk", errors="ignore").stdout
    except Exception:
        return None
    if not out:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and \
                parts[3] == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="关闭 Tokriva 控制台")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    killed = False

    # 1) 优先按 PID 文件
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if _kill_by_pid(pid):
                print(f"[已关闭] 按 PID {pid} 停止服务")
                killed = True
        except (ValueError, OSError):
            pass
        PID_FILE.unlink(missing_ok=True)

    # 2) 按端口兜底
    pid = _find_pid_by_port(args.port)
    if pid:
        if _kill_by_pid(pid):
            print(f"[已关闭] 按端口 {args.port} 停止进程 PID {pid}")
            killed = True
    elif not killed:
        print(f"[提示] 端口 {args.port} 未发现运行中的服务。")
        return 0

    if _port_in_use(args.port):
        print(f"[警告] 端口 {args.port} 仍被占用，可能未完全退出，请稍候重试。")
        return 1
    print("[完成] 服务已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
