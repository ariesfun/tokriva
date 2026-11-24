"""一键启动 Tokriva 控制台（前端 + 后端 + 定时探测）。

用法（项目根目录下）：
    python scripts/start.py            # 默认端口 8766，开启 30 分钟定时探测
    python scripts/start.py --port 8766 --no-schedule
    python scripts/start.py --interval 15

等价于手动执行：
    TOKRIVA_SCHEDULE=1 python -m uvicorn hub.api:app --host 127.0.0.1 --port 8766

启动后写 PID 与日志路径到 data/server.pid / data/server.log，供 scripts/stop.py 关闭。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PID_FILE = ROOT / "data" / "server.pid"
LOG_FILE = ROOT / "data" / "server.log"
DEFAULT_PORT = 8766


def _python() -> str:
    """返回「确实装了 uvicorn/fastapi」的解释器，避免误用缺依赖的环境。

    本机有多套 Python（3.13 装项目依赖、3.11 装 playwright、3.8 供别的工程），
    若从 Python311 调用本脚本，sys.executable 会指向无 uvicorn 的环境导致启动失败。
    因此：当前解释器能 import uvicorn 就直接用；否则回退到项目约定的 3.13 路径。
    """
    try:
        import uvicorn  # noqa: F401
        return sys.executable
    except ImportError:
        pass
    # 回退：WorkBuddy 自带 3.13（requirements.txt 依赖装在这里）
    for cand in (
        Path.home() / ".workbuddy" / "binaries" / "python" / "versions"
        / "3.13.12" / "python.exe",
    ):
        if cand.exists():
            return str(cand)
    return sys.executable


def _port_in_use(port: int) -> bool:
    """返回该端口是否已被占用（用于幂等检查）。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _load_env() -> None:
    """把根目录 .env 的键值读进 os.environ（不覆盖已存在的）。"""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    ap = argparse.ArgumentParser(description="启动 Tokriva 控制台")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--interval", type=int, default=30,
                    help="定时探测间隔（分钟），默认 30")
    ap.add_argument("--no-schedule", action="store_true",
                    help="不开启定时探测（仅手动触发）")
    args = ap.parse_args()

    if _port_in_use(args.port):
        print(f"[跳过] 端口 {args.port} 已被占用，服务可能已在运行。")
        print(f"       若确认没在跑，先执行 python scripts/stop.py 再重试。")
        return 1

    _load_env()

    # 继承当前环境变量（含 .env 已载入的），追加调度开关
    env = os.environ.copy()
    env["TOKRIVA_PORT"] = str(args.port)
    env["TOKRIVA_SCHEDULE"] = "0" if args.no_schedule else "1"
    env["TOKRIVA_PROBE_INTERVAL_MIN"] = str(args.interval)

    log = open(LOG_FILE, "a", encoding="utf-8")
    # DETACHED_PROCESS 让子进程真正脱离当前会话，父进程（本脚本/双击的 cmd）退出后仍常驻。
    # 配合 CREATE_NEW_PROCESS_GROUP 避免 Ctrl+C 连带到服务。
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [_python(), "-m", "uvicorn", "hub.api:app",
         "--host", "127.0.0.1", "--port", str(args.port),
         "--no-access-log"],
        cwd=str(ROOT), env=env,
        stdout=log, stderr=subprocess.STDOUT,
        creationflags=flags,
        close_fds=True,
    )

    # 等一小段确认起来了（失败则马上返回非 0）
    for _ in range(20):
        if _port_in_use(args.port):
            PID_FILE.write_text(str(proc.pid), encoding="utf-8")
            print(f"[已启动] Tokriva 控制台")
            print(f"  PID     : {proc.pid}")
            print(f"  地址    : http://127.0.0.1:{args.port}/")
            print(f"  定时探测: {'关闭' if args.no_schedule else f'每 {args.interval} 分钟'}")
            print(f"  日志    : {LOG_FILE}")
            print(f"  关闭    : python scripts/stop.py")
            return 0
        time.sleep(0.3)

    print(f"[失败] 服务未能在端口 {args.port} 起来，请查看日志 {LOG_FILE}")
    proc.terminate()
    return 1


if __name__ == "__main__":
    sys.exit(main())
