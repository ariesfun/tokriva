"""一键关闭 Tokriva 控制台。

用法（项目根目录下）：
    python scripts/stop.py                  # 按 data/server.pid 关闭
    python scripts/stop.py --port 8766      # 额外允许对 8766 做一次端口兜底
    python scripts/stop.py --force          # 兜底时跳过「是否本项目服务」校验

关闭顺序：**先按 PID 文件关**（最可靠）；只有当这一步一无所获时，才对端口做
一次兜底扫描。

安全设计（2026-11-24 重写）——早先的实现实测在本机误杀了 8766 上的无关服务：
  1) 端口兜底**无条件执行**：按 PID 文件关掉 8788 之后，又接着把 8766 上另一个
     服务杀了；
  2) 兜底端口**硬编码默认 8766**，且**不校验进程归属**：新克隆里没有 PID 文件，
     于是 `stop.py` 必定去杀 8766 上的「别人」。

现在三条硬约束：
  · 兜底仅在 PID 文件这条主路径一无所获时才启用；
  · 兜底端口只认显式 `--port`，或 PID 文件里记录过的端口；**推不出来就什么都
    不做**，绝不猜默认端口；
  · 兜底动手前必须能证明「这是本项目的服务」（命令行含 hub.api，或 PID 与本仓
    库记录一致）；证明不了就拒绝执行，要强杀须显式 `--force`。

PID 文件（data/server.pid）是本仓库自己的记录，视为主路径的**授权凭据**：里面有
PID 就直接关，不再额外取证——否则用户用自定义启动器（命令行里没有 hub.api）
拉起的服务反而关不掉。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PID_FILE = ROOT / "data" / "server.pid"

# 判定「是本项目的服务」的命令行特征——只认本项目对外提供的模块名。
# 刻意保持严格：宁可拒绝关闭，也不误杀别人的进程。
OWN_MARKERS = ("hub.api", "hub/api")


def _run(cmd: list[str], timeout: int = 15, encoding: str = "gbk") -> str:
    """跑一条外部命令并返回 stdout。

    中文 Windows 下 netstat/tasklist/taskkill 的输出是 GBK，用 errors="ignore"
    兜住解码，避免一个奇怪字节就把整个关闭流程炸掉。
    """
    try:
        p = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout,
                           encoding=encoding, errors="ignore")
        return p.stdout or ""
    except Exception:
        return ""


def _pid_alive(pid: int) -> bool:
    """PID 是否指向一个仍在运行的进程。

    注意：Windows 上**不能**用 `os.kill(pid, 0)` 探活——CPython 在 Windows 上没有
    「信号 0」语义，会直接调 TerminateProcess 把目标进程干掉。这里走 Win32 API。
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    k32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _kill_by_pid(pid: int) -> bool:
    """强杀 PID（Windows 上连子进程一起），返回是否**确实**结束了它。"""
    if not _pid_alive(pid):
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 15)
            return True
        except OSError:
            return False
    out = _run(["taskkill", "/PID", str(pid), "/F", "/T"])
    # 成功时 taskkill 打印 "SUCCESS: ..." / "成功: ..."；PID 不存在时返回码非 0 且
    # 输出里没有这两个词——不能再像旧版那样无条件 return True。
    up = out.upper()
    return "SUCCESS" in up or "成功" in out


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _find_pid_by_port(port: int) -> int | None:
    """按端口找 LISTENING 进程的 PID。"""
    if os.name != "nt":
        out = _run(["lsof", "-ti", f"tcp:{port}", "-s", "TCP:LISTEN"], encoding="utf-8")
        for tok in out.split():
            if tok.isdigit():
                return int(tok)
        return None
    for line in _run(["netstat", "-ano"]).splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") \
                and parts[3].upper() == "LISTENING" and parts[4].isdigit():
            return int(parts[4])
    return None


def _cmdline_of(pid: int) -> str:
    """尽量取出进程命令行；拿不到返回空串（调用方须按「无法取证」处理）。"""
    if os.name != "nt":
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return ""
        return raw.replace(b"\x00", b" ").decode("utf-8", "ignore")
    # Windows 11 已移除 wmic，tasklist 又只给映像名，只能走 PowerShell 的 CIM 查询。
    return _run([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine",
    ])


def _is_own_process(pid: int, recorded_pid: int | None) -> tuple[bool, str]:
    """证明 pid 是本项目的服务。返回 (是否确认, 依据说明)。"""
    if recorded_pid is not None and pid == recorded_pid:
        return True, "与 data/server.pid 记录一致"
    cmd = _cmdline_of(pid).lower()
    if not cmd.strip():
        return False, "读不到该进程的命令行，无法确认归属"
    for marker in OWN_MARKERS:
        if marker in cmd:
            return True, f"命令行含 {marker!r}"
    return False, "命令行不含 " + "/".join(OWN_MARKERS) + " 任一特征"


def _read_pid_file() -> tuple[int | None, int | None]:
    """读 data/server.pid → (pid, port)。

    兼容两种写法：
      · "44256"        —— 只记 PID（外部启动器、本脚本旧版）
      · "8766 44256"   —— 记端口 + PID（scripts/start.py 新版，便于 stop 兜底）
    """
    if not PID_FILE.exists():
        return None, None
    try:
        text = PID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    nums = re.findall(r"\d+", text)
    if not nums:
        return None, None
    if len(nums) == 1:
        return int(nums[0]), None
    return int(nums[-1]), int(nums[0])


def main() -> int:
    ap = argparse.ArgumentParser(description="关闭 Tokriva 控制台")
    # 刻意不给默认值：只有用户显式传了 --port，才允许对端口做兜底扫描。
    ap.add_argument("--port", type=int, default=None,
                    help="端口兜底扫描的目标端口；不给则只按 PID 文件关闭")
    ap.add_argument("--force", action="store_true",
                    help="兜底时跳过「是否本项目服务」校验（慎用）")
    args = ap.parse_args()

    killed = False

    # ── 1) 按 PID 文件关（主路径）────────────────────────────────────────
    rec_pid, rec_port = _read_pid_file()
    if rec_pid is not None:
        if _pid_alive(rec_pid):
            if _kill_by_pid(rec_pid):
                print(f"[已关闭] 按 PID 文件停止服务：PID {rec_pid}")
                killed = True
            else:
                print(f"[警告] PID {rec_pid} 仍在运行，未能结束（可能权限不足）。")
        else:
            print(f"[提示] PID 文件记录的 {rec_pid} 已不存在（陈旧记录），跳过。")
        PID_FILE.unlink(missing_ok=True)

    # ── 2) 端口兜底：仅当主路径一无所获，且端口可追溯 ──────────────────
    if not killed:
        port = args.port if args.port is not None else rec_port
        if port is None:
            print("[结束] PID 文件没关掉任何服务，且未指定 --port —— 不做端口兜底"
                  "（避免误杀其他服务）。")
            print("[提示] 若确认服务仍在本机运行：python scripts/stop.py --port <端口>")
            return 0
        pid = _find_pid_by_port(port)
        if pid is None:
            print(f"[提示] 端口 {port} 上没有正在监听的服务。")
            return 0
        ok, why = _is_own_process(pid, rec_pid)
        if not ok and not args.force:
            print(f"[拒绝] 端口 {port} 被 PID {pid} 占用，但无法确认它是本项目的服务"
                  f"（{why}）。")
            print("       已中止，未对任何进程动手；确认无误后可用 --force 强制关闭。")
            return 1
        if _kill_by_pid(pid):
            print(f"[已关闭] 端口 {port} 上的 PID {pid}（{why}）")
            killed = True
        else:
            print(f"[警告] 端口 {port} 上的 PID {pid} 未能结束，请手工确认。")

    # ── 3) 收尾校验 ─────────────────────────────────────────────────────
    port = args.port if args.port is not None else rec_port
    if port is not None:
        for _ in range(10):
            if not _port_in_use(port):
                break
            time.sleep(0.3)
        if _port_in_use(port):
            print(f"[警告] 端口 {port} 仍被占用，可能未完全退出，请稍候重试。")
            return 1
    print("[完成] 服务已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
