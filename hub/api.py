"""FastAPI 服务：把 hub.db 的探测数据以只读 HTTP 接口暴露给前端展示页。

设计原则：
- 只读为主：所有 GET 接口不写库；触发探测 / 下发预览是显式动作。
- Key 永不回显：只暴露 auth_env 变量名，不读取、不返回任何凭证。
- 错误体脱敏：probe_result.error_body 已由 classify.redact 处理，这里再兜底截断。
- 错误分类与限流分开：接口分别给出 status 计数与 429 计数。

运行：
    python -m uvicorn hub.api:app --host 127.0.0.1 --port 8766
或：
    python -m hub.api
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
import json
import os
import statistics
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .scrub import scrub_text

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "hub.db"

# Windows：后端以 DETACHED_PROCESS 常驻，自身没有控制台。此时再 spawn 控制台子进程
# （python.exe / git.exe 等），Windows 会为子进程<新分配>一个控制台窗口——界面上点
# 「立即检测」就会弹出一个黑窗口。统一带上 CREATE_NO_WINDOW，探测即可真正静默后台运行。
# 非 Windows 平台该值为 0，等价于不传 creationflags。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# 探测互斥：同一时刻只允许一轮 hub.run --once 在跑。
# 起因——连点「立即检测」、多标签页、或定时轮撞上手动轮，都会并行起多个探测进程，
# 互抢代理与额度，表现为「某一轮里若干接口网络大面积失败」。这是后端职责，
# 不能只靠前端单页面的防抖（多标签页/连点能绕过它）。
_PROBE_LOCK = threading.Lock()

# 聚合网关（OpenRouter）下，服务商名 → 模型 id 前缀。
# 用于判断「这家免费算力是否真的被探测到」：只有 OpenRouter endpoint 下
# 真的发现了该前缀的模型，才叫「已接入」。仅填 endpoint_label 不算接入。
OR_PREFIX = {
    "NVIDIA NIM": "nvidia/",
    "Particle AI": "particle/",
    "Baidu 千帆": "baidu/",
    "Dots Studio": "dots/",
    "Recursive": "recursive/",
    "LiquidAI": "liquid/",
    "inclusionAI": "inclusion/",
    "Google Gemini": "google/",
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _conn():
    return db.connect(DB_PATH)


def _rows(conn, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _num(v):
    return None if v is None else round(float(v), 3)


def _redact(text: str | None, limit: int = 400) -> str | None:
    if not text:
        return text
    text = text[:limit]
    for env in os.environ:
        if env.endswith(("_KEY", "_TOKEN", "_SECRET")) and os.environ.get(env):
            text = text.replace(os.environ[env], "***")
    return text


def _probe_dict(r: dict) -> dict:
    """探测记录 -> 前端可消费结构（带端点标签、脱敏）。"""
    out = dict(r)
    for k in ("dns_ms", "tcp_ms", "tls_ms", "ttfb_ms", "ttft_ms", "tps", "total_ms"):
        out[k] = _num(out.get(k))
    out["error_body"] = _redact(out.get("error_body"))
    out["resp_body"] = _redact(out.get("resp_body"))
    return out


# ---------------------------------------------------------------------------
# 应用生命周期：可选 APScheduler 定时探测
# ---------------------------------------------------------------------------
class SchedulerState:
    scheduler = None


STATE = SchedulerState()


def _scheduled_probe() -> None:
    """定时任务：跑一轮 L0+L2 探测，带抖动由 APScheduler 触发器提供。"""
    # 手动轮正在跑就别叠加：本轮跳过，下个间隔自动再来，不漏事。
    # 两轮并行会互抢代理与额度（见 _PROBE_LOCK 的说明）。
    if not _PROBE_LOCK.acquire(blocking=False):
        return
    try:
        subprocess.run([sys.executable, "-m", "hub.run", "--once"], cwd=ROOT,
                       capture_output=True, timeout=180, check=False,
                       creationflags=_NO_WINDOW)
    except Exception:
        pass
    finally:
        _PROBE_LOCK.release()
    _round_after_probe()


def _round_after_probe() -> None:
    """新探测落库后，在本进程里推进一轮提醒状态机并推送通知。

    为什么不把这段放进 hub/run.py：run.py 是子进程，状态文件的归属在本进程，
    两边各推进各的会互相覆盖。这里让"探测"和"判断"由同一处串起来，
    于是**没人打开页面时**故障照样能被确认并推出去——提醒页只是查看用的窗口，
    不该兼任"系统的心跳"。

    漏掉某一轮不会丢事：状态机拿存下来的状态跟本轮扫描结果比对，
    少算一轮只是让确认晚一点，翻转本身不会被跳过。
    """
    from . import ledger
    from . import reminder_state as rs
    from .settings import notify_repeat_hours, reminder_tuning

    try:
        conn = _conn()
        try:
            items = ledger.scan_reminders(conn, persist=False)
            round_id = _probe_round_id(conn)
        finally:
            conn.close()
        confirm_n, recover_m = reminder_tuning()
        _enriched, transitions, _tracked, repeats = rs.observe(
            items, confirm_n=confirm_n, recover_m=recover_m, round_id=round_id,
            repeat_hours=notify_repeat_hours())
        _notify_transitions(transitions, repeats, muted=_muted_keys())
    except Exception:
        pass                   # 定时线程里绝不能把异常抛出去


SCHEDULE_FILE = ROOT / "data" / "schedule.json"


def _load_schedule_pref() -> dict:
    """定时检测偏好。持久化到文件，重启后自动恢复。"""
    try:
        return json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_schedule_pref(pref: dict) -> None:
    try:
        SCHEDULE_FILE.write_text(json.dumps(pref, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# 启动补跑的延迟：等服务把 .env、静态资源、数据库都准备好再起探测，
# 避免和启动流程争资源；45 秒对「页面早几十秒看到新数据」没有感知差异。
_CATCHUP_DELAY_SEC = 45


def _next_grid_time(interval_min: int, now: datetime | None = None) -> datetime:
    """下一个「墙上时钟网格」时刻：以本地零点为锚，把间隔向上取整。

    为什么需要它——APScheduler 的 `interval` 触发器首次触发是「注册时刻 + 间隔」，
    **不按墙上时钟对齐**。每次重启都把间隔重新计时，于是重启足够频繁时
    （本机日志见过 24 次启动）定时轮次永不触发，页面数据越等越旧。
    改成显式给 `next_run_time` 钉一个网格点，重启就不再漂移：
    间隔 30 分钟时永远落在 `:00` / `:30`。
    """
    now = now or datetime.now()
    step = max(1, int(interval_min))
    anchor = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_min = int((now - anchor).total_seconds() // 60)
    return anchor + timedelta(minutes=(elapsed_min // step + 1) * step)


def _last_probe_at() -> datetime | None:
    """最近一次探测的落库时刻；库为空或读取失败返回 None。"""
    try:
        conn = _conn()
        try:
            row = conn.execute("SELECT MAX(created_at) AS m FROM probe_result").fetchone()
            last = row["m"] if row else None
        finally:
            conn.close()
    except Exception:
        return None
    if not last:
        return None
    try:
        return datetime.fromisoformat(str(last))
    except Exception:
        return None


def _arm_startup_catchup(interval_min: int) -> None:
    """启动补跑：数据若「在下次定时轮开始前就已超期」，启动后补跑一轮。

    这是「重启清零间隔计时器」的另一半修法——网格对齐解决了「触发时点漂移」，
    但解决不了「重启间隔短于一个间隔时永远跑不到网格点」。补跑的条件刻意收紧：
    只有当「上次探测 + 一个间隔」仍早于**下次定时轮时刻**时才补，也就是数据到那时
    必然已经超过一个间隔没更新。同一间隔内因此最多补一轮，重启几十次也不会变成刷探测。

    库为空时不补（首次部署交给正常调度），避免「刚装好就立刻打外部接口」。
    """
    if STATE.scheduler is None:
        return
    last = _last_probe_at()
    if last is None:
        return
    now = datetime.now()
    nxt = _next_grid_time(interval_min, now)
    if last + timedelta(minutes=max(1, int(interval_min))) >= nxt:
        return
    STATE.scheduler.add_job(_scheduled_probe, "date",
                            run_date=now + timedelta(seconds=_CATCHUP_DELAY_SEC),
                            id="probe_catchup", replace_existing=True)


def _apply_schedule(enabled: bool, interval_min: int) -> None:
    """在运行中的进程里启动/停止/调整定时探测任务。"""
    if enabled:
        from apscheduler.schedulers.background import BackgroundScheduler
        if STATE.scheduler is None:
            STATE.scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
            STATE.scheduler.start()
        STATE.scheduler.add_job(_scheduled_probe, "interval", minutes=interval_min,
                                jitter=60, id="probe", replace_existing=True,
                                next_run_time=_next_grid_time(interval_min))
    else:
        if STATE.scheduler:
            try:
                STATE.scheduler.remove_job("probe")
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 与 hub.run 同口径加载 .env（仅 setdefault，不覆盖已有进程环境变量），
    # 这样 env-check / 测试连接 才能感知服务端配置的密钥。
    env_path = ROOT / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        except Exception:
            pass
    pref = _load_schedule_pref()
    enabled = bool(pref.get("enabled", os.getenv("TOKRIVA_SCHEDULE", "0") == "1"))
    interval = int(pref.get("intervalMin", os.getenv("TOKRIVA_PROBE_INTERVAL_MIN", "30")))
    if enabled:
        try:
            _apply_schedule(True, interval)
            # 重启不计补会让数据越等越旧，这里按需补一轮（见 _arm_startup_catchup）。
            _arm_startup_catchup(interval)
        except ImportError:
            STATE.scheduler = None
    yield
    # 调度器可能只是被创建、从未成功 start()（APScheduler 未安装，或启动即失败）。
    # 对未运行的调度器调 shutdown() 会抛 SchedulerNotRunningError，
    # 让 uvicorn 打出"Application shutdown failed"这种吓人的日志——这里挡住。
    if STATE.scheduler is not None:
        try:
            if getattr(STATE.scheduler, "running", False):
                STATE.scheduler.shutdown(wait=False)
        except Exception:
            pass
        STATE.scheduler = None


app = FastAPI(
    title="Tokriva Hub API",
    description="免费 AI API 资源探测与排名内部控制台接口（只读为主）",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("TOKRIVA_CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 1. 总览统计
# ---------------------------------------------------------------------------
@app.get("/api/overview", tags=["总览"])
def overview(window_hours: int = Query(24, ge=1, le=720)):
    """三层诊断总览：状态矩阵 + 一句话结论。

    判定在服务端做一次（设计文档 D8），前端只负责展示；
    "现在"的判定看最近记录，window_hours 只影响服务可用率的统计窗口。
    """
    from .status_engine import build_overview
    conn = _conn()
    try:
        return build_overview(conn, window_hours=window_hours)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. 探测记录（筛选 + 分页）
# ---------------------------------------------------------------------------
@app.get("/api/probes", tags=["探测记录"])
def probes(
    level: str | None = Query(None, pattern="^(L0|L1|L2)$"),
    status: str | None = None,
    endpoint: str | None = None,
    q: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    conn = _conn()
    where, params = [], []
    if level:
        where.append("p.level=?"); params.append(level)
    if status:
        where.append("p.status=?"); params.append(status)
    if endpoint:
        where.append("e.label=?"); params.append(endpoint)
    if q:
        like = f"%{q}%"
        where.append("(e.label LIKE ? OR p.model_id LIKE ? OR p.status LIKE ?)")
        params.extend([like, like, like])
    sql = (
        "SELECT p.*, e.label endpoint, m.display_name model_display, m.status model_status "
        "FROM probe_result p "
        "JOIN endpoint e ON e.id=p.endpoint_id "
        "LEFT JOIN model m ON m.endpoint_id=p.endpoint_id AND m.model_id=p.model_id "
        + ("WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY p.id DESC LIMIT ? OFFSET ?"
    )
    rows = [_probe_dict(r) for r in _rows(conn, sql, tuple(params) + (limit, offset))]
    count_sql = (
        "SELECT COUNT(*) FROM probe_result p JOIN endpoint e ON e.id=p.endpoint_id "
        + ("WHERE " + " AND ".join(where) if where else "")
    )
    total = conn.execute(count_sql, tuple(params)).fetchone()[0]
    conn.close()
    return {"total": total, "items": rows, "limit": limit, "offset": offset}


@app.get("/api/probes/export", tags=["测试记录"])
def export_probes(level: str | None = None, status: str | None = None,
                  endpoint: str | None = None, q: str | None = None):
    """导出测试记录为 CSV。跟随页面筛选（级别/状态/关键词/接口）。

    面向用户的可读格式：中文表头、状态翻译为中文、时间为北京时间；
    模型列用展示名，原始 ID 单独一列便于核对。
    """
    _LEVEL = {"L0": "连通检测", "L1": "轻量测试", "L2": "速度测试"}
    _STATUS = {"HEALTHY": "健康", "DEGRADED": "降级", "AUTH_FAILED": "鉴权失败",
               "QUOTA_EXHAUSTED": "额度耗尽", "RATE_LIMITED": "限流",
               "MODEL_GONE": "模型下线", "ENDPOINT_ERROR": "接口错误",
               "NETWORK_ERROR": "网络错误", "RESPONSE_INVALID": "响应无效",
               "REASONING_STARVED": "推理占满", "NOT_CONFIGURED": "未接入"}
    _STREAM = {"true_stream": "边生成边返回", "buffered": "整包返回",
               "single": "单次返回", "none": "无内容"}
    _MODEL_STATUS = {"ACTIVE": "在售", "MISSING": "已下线"}

    conn = _conn()
    where, params = [], []
    if level:
        where.append("p.level=?"); params.append(level)
    if status:
        where.append("p.status=?"); params.append(status)
    if endpoint:
        where.append("e.label=?"); params.append(endpoint)
    if q:
        like = f"%{q}%"
        where.append("(e.label LIKE ? OR p.model_id LIKE ? OR p.status LIKE ? OR p.stream_mode LIKE ?)")
        params.extend([like, like, like, like])
    sql = (
        "SELECT e.label endpoint, p.level, p.model_id, m.display_name, "
        "m.status AS model_status, p.status, p.http_status, "
        "p.dns_ms, p.tcp_ms, p.tls_ms, p.ttfb_ms, p.ttft_ms, "
        "p.output_tokens, p.tps, p.total_ms, p.stream_mode, p.created_at "
        "FROM probe_result p "
        "JOIN endpoint e ON e.id=p.endpoint_id "
        "LEFT JOIN model m ON m.endpoint_id=p.endpoint_id AND m.model_id=p.model_id "
        + ("WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY p.id"
    )
    rows = _rows(conn, sql, tuple(params))
    conn.close()

    def _bj(ts: str | None) -> str:
        """库中时间戳已是本机本地时间（即北京时间），导出时原样输出即可。"""
        return ts or ""

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时间（北京时间）", "接口", "测试类型", "状态",
                "HTTP状态码", "域名解析(ms)", "建立连接(ms)", "加密握手(ms)",
                "首字节响应(ms)", "首字延迟(ms)", "输出token数", "出字速度(tokens/s)",
                "总耗时(ms)", "返回方式", "模型", "模型ID", "模型状态"])
    def _num(v, nd):
        return round(v, nd) if v is not None else ""

    for r in rows:
        w.writerow([
            _bj(r["created_at"]),
            r["endpoint"],
            _LEVEL.get(r["level"], r["level"] or ""),
            _STATUS.get(r["status"], r["status"] or ""),
            r["http_status"] if r["http_status"] is not None else "",
            _num(r["dns_ms"], 1), _num(r["tcp_ms"], 1), _num(r["tls_ms"], 1),
            _num(r["ttfb_ms"], 1), _num(r["ttft_ms"], 1),
            r["output_tokens"] if r["output_tokens"] is not None else "",
            _num(r["tps"], 2), _num(r["total_ms"], 1),
            _STREAM.get(r["stream_mode"], r["stream_mode"] or ""),
            r["display_name"] or r["model_id"] or "",
            r["model_id"] or "",
            _MODEL_STATUS.get(r["model_status"], r["model_status"] or ""),
        ])
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=test_records.csv"})


# ---------------------------------------------------------------------------
# 3. 端点台账
# ---------------------------------------------------------------------------
@app.get("/api/endpoints", tags=["端点台账"])
def endpoints():
    conn = _conn()
    rows = _rows(conn, """
        SELECT e.id, e.label, e.base_url, e.protocol, e.default_model, e.use_proxy,
               e.active, e.daily_budget, e.has_usage, e.has_utilization, e.note,
               e.auth_env, p.name provider, p.website, p.kind, p.provider_status
        FROM endpoint e LEFT JOIN provider p ON p.id=e.provider_id
        ORDER BY e.id
    """)
    from . import budget as _budget
    from . import quota_rules as _qr
    from .settings import budget_config
    _budget_on, _guard_pct = budget_config()
    # 当日用量按"本地今天"统计，与用户在页面上看到的日期一致
    _bst = _budget.statuses(conn, rows, guard_pct=_guard_pct)
    out = []
    for r in rows:
        _bstat = _bst.get(r["label"]) or {}
        stats = conn.execute("""
            SELECT COUNT(*) n,
                   SUM(CASE WHEN status='HEALTHY' THEN 1 ELSE 0 END) healthy,
                   MAX(created_at) last_at
            FROM probe_result WHERE endpoint_id=?
        """, (r["id"],)).fetchone()
        l2 = [x["ttft_ms"] for x in conn.execute(
            "SELECT ttft_ms FROM probe_result WHERE endpoint_id=? AND level='L2' "
            "AND status='HEALTHY' AND stream_mode='true_stream' AND ttft_ms IS NOT NULL",
            (r["id"],))]
        tps = [x["tps"] for x in conn.execute(
            "SELECT tps FROM probe_result WHERE endpoint_id=? AND level='L2' "
            "AND status='HEALTHY' AND tps IS NOT NULL", (r["id"],))]
        # 上下文窗口：取该端点 ACTIVE 对话模型里的最大窗口
        ctxrow = conn.execute(
            """SELECT MAX(context_length) ctx, COUNT(*) n FROM model
               WHERE endpoint_id=? AND status='ACTIVE' AND COALESCE(is_chat,1)=1""",
            (r["id"],)).fetchone()
        dm = conn.execute(
            "SELECT display_name FROM model WHERE endpoint_id=? AND model_id=?",
            (r["id"], r["default_model"])).fetchone()
        # 额度快照走**展示口径**（quota_rules.DISPLAY_ROW_SQL）：取最新一条「非速率行」。
        # 不能只按 id DESC 取最新一条——HEADER 抓取先写 DAILY/USD 预算行、再写速率行，
        # 于是最新行恒为速率行，额度列会显示成「19/20 次」且没有重置时间。
        qrow = conn.execute(
            f"""SELECT quota_type, unit, total, remaining, source, reset_at, expires_at, captured_at
               FROM quota_snapshot WHERE endpoint_id=? AND {_qr.DISPLAY_ROW_SQL}
               ORDER BY id DESC LIMIT 1""",
            (r["id"],),
        ).fetchone()
        out.append({
            "quota": (dict(qrow) if qrow else None),
            "id": r["id"], "label": r["label"], "provider": r["provider"],
            "website": r["website"], "kind": r["kind"], "baseUrl": r["base_url"],
            "protocol": r["protocol"], "defaultModel": r["default_model"],
            "defaultModelDisplay": (dm["display_name"] if dm and dm["display_name"] else r["default_model"]),
            "useProxy": bool(r["use_proxy"]), "authEnv": r["auth_env"],
            # 只回显"环境变量是否已配置"的布尔值，绝不回显 Key 本身；
            # 探测可能在网络层就失败（到不了鉴权环节），所以接入状态必须看配置而不是探测结果
            "hasKey": bool(os.environ.get(r["auth_env"] or "")),
            "dailyBudget": r["daily_budget"], "note": _redact(r["note"]),
            # 探测预算的当日状态：预算关掉时 state='off'，页面据此不显示限额压力
            "budgetToday": _bstat.get("used"), "budgetRemaining": _bstat.get("remaining"),
            "budgetState": (_bstat.get("state") if _budget_on else _budget.STATE_OFF),
            "budgetEnabled": _budget_on,
            # 降级原因（哪道闸门先到：次数 or 额度）；未启用时为空
            "budgetReason": (_bstat.get("reason") if _budget_on else ""),
            # 账号额度已用百分比（未知为 None）：第二道闸门，有的免费层按金额/token 限额
            "quotaUsedPct": (_bstat.get("quota_used_pct") if _budget_on else None),
            "status": r["provider_status"],
            "availability": round(stats["healthy"] / stats["n"] * 100, 1) if stats["n"] else 0,
            "probeCount": stats["n"], "lastProbeAt": stats["last_at"],
            "ttftP50": round(statistics.median(l2)) if l2 else None,
            "tpsAvg": round(statistics.mean(tps), 1) if tps else None,
            # 额度来源与上面展示的快照同源：快照里没有一条能上屏（例如只拿到速率行）
            # 就是「需手动查看」，免得出现「列里写着未录入、筛选项却算它可查」的矛盾
            "quotaSource": (qrow["source"] or "MANUAL_REQUIRED") if qrow else "MANUAL_REQUIRED",
            "hasUsage": bool(r["has_usage"]), "hasUtilization": bool(r["has_utilization"]),
            "active": bool(r["active"]),
            "contextMax": ctxrow["ctx"] if ctxrow else None,
            "modelCount": ctxrow["n"] if ctxrow else 0,
        })
    conn.close()
    return {"items": out}


# ---------------------------------------------------------------------------
# 端点写入：用户自行登记新的可调用资源
# ---------------------------------------------------------------------------
import re as _re

_ENV_NAME = _re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class EndpointIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=64, description="端点别名，唯一")
    provider: str = Field(..., min_length=1, max_length=80, description="服务商名称")
    base_url: str = Field(..., description="Base URL，如 https://api.example.com/v1")
    protocol: str = Field("openai_chat", pattern="^(openai_chat|openai_responses)$")
    auth_env: str = Field(..., description="环境变量名（如 XXX_API_KEY），不是 Key 明文")
    default_model: str | None = None
    use_proxy: bool = False
    daily_budget: int = Field(200, ge=1, le=100000)
    kind: str = "model_api"
    website: str | None = None
    note: str | None = None


def _validate_endpoint(e: EndpointIn) -> None:
    """安全校验：SSRF 防护 + 禁止明文 Key（设计文档 8.1/8.2）。"""
    if not _ENV_NAME.match(e.auth_env):
        raise HTTPException(422, "auth_env 必须是环境变量名（大写字母/数字/下划线，如 MY_API_KEY），不能填 Key 明文")
    if not (e.auth_env.endswith("_KEY") or e.auth_env.endswith("_TOKEN") or
            e.auth_env.endswith("_SECRET")):
        raise HTTPException(422, "auth_env 应以 _KEY / _TOKEN / _SECRET 结尾")
    url = e.base_url.strip()
    if not url.startswith("https://") and not (
        url.startswith("http://127.0.0.1") or url.startswith("http://localhost")):
        raise HTTPException(422, "base_url 必须使用 HTTPS；仅本地桥允许 http://127.0.0.1 或 localhost")
    if e.label.strip() != e.label or not e.label.strip():
        raise HTTPException(422, "label 不能为空或含首尾空格")


@app.post("/api/endpoints", tags=["端点台账"], status_code=201)
def endpoint_create(e: EndpointIn):
    """登记新端点。Key 只存环境变量名，明文请在 .env 中配置。"""
    _validate_endpoint(e)
    conn = _conn()
    try:
        exists = conn.execute("SELECT id FROM endpoint WHERE label=?", (e.label,)).fetchone()
        db.seed_endpoints(conn, [{
            "provider": e.provider, "website": e.website, "kind": e.kind,
            "label": e.label, "base_url": e.base_url, "protocol": e.protocol,
            "auth_env": e.auth_env, "default_model": e.default_model,
            "use_proxy": int(e.use_proxy), "daily_budget": e.daily_budget,
            "note": e.note,
        }])
        # 重新登记时确保端点处于启用状态（seed 的 UPSERT 不改 active）；
        # daily_budget 也已从 seed 的 upsert 里拿掉，必须在这里显式写入，
        # 否则在页面上改了「每天最多测几次」保存后会被下一轮探测忽略。
        conn.execute("UPDATE endpoint SET active=1, daily_budget=? WHERE label=?",
                     (e.daily_budget, e.label))
        conn.commit()
        row = conn.execute("SELECT id FROM endpoint WHERE label=?", (e.label,)).fetchone()
    finally:
        conn.close()
    note = f"还请在服务端把 {e.auth_env} 设为真实密钥，测试才能通过鉴权"
    if not e.default_model:
        note += "；未设置默认模型，探测将使用 auto，部分网关不支持会导致对话测试失败"
    return {"ok": True, "id": row["id"], "label": e.label,
            "created": not bool(exists),
            "note": note}


class EndpointActive(BaseModel):
    active: bool


@app.get("/api/env-check", tags=["接口台账"])
def env_check(auth_env: str):
    """检查服务端是否存在某环境变量。只返回存在与否，绝不返回值。"""
    import re as _re
    if not _re.fullmatch(r"[A-Z][A-Z0-9_]*", auth_env or ""):
        raise HTTPException(422, "环境变量名格式不正确（大写字母/数字/下划线）")
    return {"found": bool(os.environ.get(auth_env))}


class EndpointTestIn(BaseModel):
    base_url: str
    protocol: str = "openai_chat"
    auth_env: str


class KeySetIn(BaseModel):
    auth_env: str = Field(..., description="环境变量名（密钥的存放位置名，不是密钥本身）")
    value: str = Field(..., min_length=4, max_length=512)
    label: str | None = Field(None, description="接口名，写入注释便于人读")
    base_url: str | None = Field(None, description="接口地址，写入注释便于人读")


@app.post("/api/env-set", tags=["接口台账"])
def env_set(req: KeySetIn):
    """界面填写/更换密钥：写入服务端 .env 并即时生效。

    只写不读：本接口不接受查询，也永远不会回显已保存的内容（Key 永不回显原则）。
    """
    name = (req.auth_env or "").strip().upper()
    if not _ENV_NAME.match(name):
        raise HTTPException(422, "环境变量名不合法（大写字母/数字/下划线）")
    if not (name.endswith("_KEY") or name.endswith("_TOKEN") or name.endswith("_SECRET")):
        raise HTTPException(422, "环境变量名应以 _KEY / _TOKEN / _SECRET 结尾")
    value = req.value.strip()
    if not value or "\n" in value:
        raise HTTPException(422, "密钥内容不能为空，且不能包含换行")
    env_path = ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    except OSError:
        lines = []
    # 注释头遵循 .env 既有规范：# NNN 接口名 — 接口地址（网页端录入）
    prefix = name
    for _sfx in ("_API_KEY", "_KEY", "_TOKEN", "_SECRET"):
        if name.endswith(_sfx) and len(name) > len(_sfx):
            prefix = name[:-len(_sfx)]
            break
    base_name = f"{prefix}_BASE_URL"
    entry = [f"{name}={value}"]
    if req.base_url:
        entry.append(f"{base_name}={req.base_url.strip()}")
    # 先摘除旧条目（连同紧邻其上的注释行与同名 BASE_URL 行）
    keep, found = [], False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.split("=", 1)[0].strip() == name:
            while keep and keep[-1].startswith("#"):
                keep.pop()
            found = True
            i += 1
            continue
        if line.split("=", 1)[0].strip() == base_name and found:
            i += 1
            continue
        keep.append(line)
        i += 1
    while keep and not keep[0].strip():
        keep.pop(0)  # 摘掉的若是文件首条，别把开头空行留在最前
    # 编号从「摘除后」的内容里取最大值 +1：同一条目重复保存时编号保持稳定
    if req.label:
        try:
            nums = [int(m.group(1)) for m in
                    (_re.match(r"#\s*(\d{3})\s", l) for l in keep) if m]
            num = f"{max(nums) + 1:03d}" if nums else "001"
        except Exception:
            num = None
        head = f" {num} " if num else " "
        head += f"{req.label.strip()}"
        if req.base_url:
            head += f" — {req.base_url.strip()}"
        entry.insert(0, f"#{head}（网页端录入）")
    if keep and keep[-1].strip():
        keep.append("")  # 与上一段之间空一行
    keep.extend(entry)
    env_path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    os.environ[name] = value  # 当前进程即时生效，不用等重启
    return {"ok": True, "auth_env": name}


@app.post("/api/endpoints/test", tags=["接口台账"])
def endpoint_test(req: EndpointTestIn):
    """保存前先测：用服务端环境变量发一次最小请求（L0），不落库、不登记。"""
    from . import probe as probe_mod
    if not (req.base_url.startswith("https://") or req.base_url.startswith("http://127.0.0.1")
            or req.base_url.startswith("http://localhost")):
        raise HTTPException(422, "接口地址必须使用 HTTPS（仅本地桥允许 http://127.0.0.1）")
    ep = {"id": 0, "base_url": req.base_url, "protocol": req.protocol, "auth_env": req.auth_env,
          "label": "connect-test", "kind": "model_api", "timeout_sec": 15}
    result = probe_mod.probe_l0(ep, timeout=15)
    category = {"HEALTHY": "连接成功", "NOT_CONFIGURED": "缺少密钥",
                "AUTH_FAILED": "鉴权失败", "NETWORK_ERROR": "网络失败",
                "ENDPOINT_ERROR": "服务端错误", "RESPONSE_INVALID": "响应异常"}.get(
                    result.get("status"), result.get("status") or "未知")
    return {"ok": result.get("status") == "HEALTHY",
            "category": category, "httpStatus": result.get("http_status"),
            "latencyMs": result.get("total_ms"),
            "detail": (result.get("error_body") or "")[:300],
            "models": result.get("models") or []}


class QuotaManualIn(BaseModel):
    endpoint_id: int
    # 手工只录**真额度**：时间窗额度或订阅类权益（SUBSCRIPTION 回归本义）。
    # 不开放 RATE_LIMIT——那是机器从响应头抓来的速率窗，不是可消耗额度。
    quota_type: str = Field("TOTAL", pattern="^(DAILY|WEEKLY|MONTHLY|TOTAL|SUBSCRIPTION)$")
    unit: str = Field("USD", pattern="^(USD|REQUEST|TOKEN)$")
    total: float | None = None
    remaining: float | None = None
    reset_at: str | None = None
    expires_at: str | None = None


@app.post("/api/quota/manual", tags=["接口台账"])
def quota_manual(q: QuotaManualIn):
    """手工录入额度快照（来源 MANUAL），供额度列与排名额度分项使用。"""
    conn = _conn()
    if not conn.execute("SELECT id FROM endpoint WHERE id=?", (q.endpoint_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "接口不存在")
    conn.execute(
        """INSERT INTO quota_snapshot(endpoint_id, quota_type, unit, total, remaining,
                                      source, reset_at, expires_at)
           VALUES(?,?,?,?,?,'MANUAL',?,?)""",
        (q.endpoint_id, q.quota_type, q.unit, q.total, q.remaining,
         q.reset_at, q.expires_at),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.put("/api/endpoints/{endpoint_id}/active", tags=["端点台账"])
def endpoint_set_active(endpoint_id: int, body: EndpointActive):
    """启用/停用端点。停用后探针不再探测它。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT label FROM endpoint WHERE id=?", (endpoint_id,)).fetchone()
        if not row:
            raise HTTPException(404, "endpoint not found")
        conn.execute("UPDATE endpoint SET active=? WHERE id=?",
                     (int(body.active), endpoint_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "label": row["label"], "active": body.active}


@app.get("/api/endpoints/{endpoint_id}/detail", tags=["端点台账"])
def endpoint_detail(endpoint_id: int):
    conn = _conn()
    ep = conn.execute(
        "SELECT e.*, p.name provider, p.website FROM endpoint e "
        "LEFT JOIN provider p ON p.id=e.provider_id WHERE e.id=?", (endpoint_id,)).fetchone()
    if not ep:
        conn.close(); raise HTTPException(404, "endpoint not found")
    series = _rows(conn, """
        SELECT p.id, p.model_id, p.level, p.status, p.ttft_ms, p.tps,
               p.stream_mode, p.created_at, m.display_name model_display, m.status model_status
        FROM probe_result p
        LEFT JOIN model m ON m.endpoint_id=p.endpoint_id AND m.model_id=p.model_id
        WHERE p.endpoint_id=? ORDER BY p.id DESC LIMIT 40
    """, (endpoint_id,))
    for s in series:
        s["ttft_ms"] = _num(s["ttft_ms"]); s["tps"] = _num(s["tps"])
    models = _rows(conn, """
        SELECT model_id, display_name, served_as, context_length, context_source,
               supports_tools, is_chat, last_seen
        FROM model WHERE endpoint_id=? ORDER BY last_seen DESC LIMIT 30
    """, (endpoint_id,))
    # 抽屉里的「额度情况」：真额度排在前，速率行（RATE_LIMIT）垫底。
    # 只按 id DESC 取会因写入顺序让速率行霸屏（HEADER 每轮先写预算行再写速率行），
    # 把 (quota_type='RATE_LIMIT') 放作第一排序键即可让预算行优先，速率行不丢、只是靠后。
    quotas = _rows(conn, """
        SELECT quota_type, unit, total, used, remaining, source, captured_at
        FROM quota_snapshot WHERE endpoint_id=?
        ORDER BY (quota_type='RATE_LIMIT'), id DESC LIMIT 8
    """, (endpoint_id,))
    util = _rows(conn, """
        SELECT model_id, utilization, state, captured_at FROM model_utilization
        WHERE endpoint_id=? ORDER BY id DESC LIMIT 12
    """, (endpoint_id,))
    conn.close()
    return {
        "endpoint": {
            "id": ep["id"], "label": ep["label"], "provider": ep["provider"],
            "website": ep["website"], "baseUrl": ep["base_url"],
            "protocol": ep["protocol"], "defaultModel": ep["default_model"],
            "useProxy": bool(ep["use_proxy"]), "authEnv": ep["auth_env"],
            "hasKey": bool(os.environ.get(ep["auth_env"] or "")),
            "dailyBudget": ep["daily_budget"], "note": _redact(ep["note"]),
        },
        "series": series, "models": models, "quotas": quotas, "utilization": util,
    }


# ---------------------------------------------------------------------------
# 4. 排名（先过滤再打分，分场景）
# ---------------------------------------------------------------------------
SCENES = {
    "speed": {"label": "速度榜", "w": {"lat": 0.55, "tps": 0.25, "stab": 0.2}},
    "agent": {"label": "Agent / Coding 榜", "w": {"lat": 0.3, "tps": 0.15, "stab": 0.55}},
    "stable": {"label": "稳定榜", "w": {"lat": 0.15, "tps": 0.15, "stab": 0.7}},
}


@app.get("/api/rankings", tags=["排名"])
def rankings(scene: str = Query("speed", pattern="^(speed|agent|stable|longterm)$")):
    """排名统一由 hub.rank 产出，避免三处各算一套口径。

    历史上前端、本接口、hub/rank.py 各有一套打分，权重与过滤条件不一致，
    会出现「页面看到的榜」和「下发用的榜」不同。这里改为单一来源。
    对外结构的组装已抽到 `hub.rank.board`，公开导出（public_snapshot）复用同一函数。
    """
    from . import rank as ranker

    conn = _conn()
    try:
        return ranker.board(conn, scene)
    finally:
        conn.close()


def _probe_round_id(conn) -> str:
    """当前探测批次的标识，喂给提醒状态机当"第几轮"。

    用最后一条探针记录的 id 加总条数：新探测会改变它，而页面刷新、前端轮询、
    手点「重新扫描」不会——于是状态机不会把"多看了几眼"当成"又坏了一轮"。
    没有探针数据时给个固定的 p0-0，退化成"所有调用算同一轮"。
    """
    row = conn.execute("SELECT MAX(id), COUNT(*) FROM probe_result").fetchone()
    if not row or row[0] is None:
        return "p0-0"
    return f"p{row[0]}-{row[1]}"


NOTIFY_LOG_FILE = ROOT / "data" / "notify_log.json"
NOTIFY_LOG_MAX = 50


def _load_notify_log() -> list:
    try:
        data = json.loads(NOTIFY_LOG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _append_notify_log(rec: dict) -> None:
    """留一条投递记录：用户问"怎么没收到通知"时，答案就在这里。"""
    try:
        log = _load_notify_log()
        log.append(rec)
        NOTIFY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        NOTIFY_LOG_FILE.write_text(
            json.dumps(log[-NOTIFY_LOG_MAX:], ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:
        pass


def _muted_keys() -> set[str]:
    """当前处于静音期内的提醒键（snooze / mute 尚未到期）。

    重发要跳过它们：用户已经明说"这段时间别为这条打扰我"，再喊一遍等于
    把静音开关旁路掉。**状态翻转不在此列**——恢复是新信息，用户仍该知道；
    被静音的只是"重复告知一件已知的事"。
    """
    now = datetime.now(timezone.utc)
    out: set[str] = set()
    for key, a in _load_reminder_actions().items():
        try:
            if a.get("until") and datetime.fromisoformat(a["until"]) > now:
                out.add(key)
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def _notify_transitions(transitions: list[dict],
                        repeats: list[dict] | None = None,
                        muted: set[str] | None = None) -> dict:
    """把本轮的翻转（以及到期重发）推到用户配好的渠道。无人值守也照发。

    只在"确认故障"与"已恢复"两个时点打扰用户：状态机已经把抖动挡在待确认期里，
    这里再筛一次是为了滤掉 RECOVERING（恢复观察期）——它只是个中间态，
    发出去等于多一条噪音。没配地址就什么都不做。

    `repeats` 是"仍在告警、到点该再喊一声"的条目（见 reminder_state.due_repeats）。
    它和翻转一起发，且逐条按各自的计时去重（发过就重置 `last_notified`），
    所以页面刷新不会重复发。

    `muted` 是当前处于静音期内的提醒键。重发会跳过它们——静音是用户明确的
    "别打扰"，重发必须服从；翻转不受影响（见 `_muted_keys`）。
    """
    from . import notify
    from .settings import notify_config

    url, notify_recover = notify_config()
    if repeats and muted:
        repeats = [r for r in repeats if r.get("key") not in muted]
    events = list(transitions) + list(repeats or [])
    if not events:
        return {"enabled": bool(url), "sent": False, "count": 0,
                "skipped": 0, "at": None, "items": []}
    try:
        res = notify.dispatch(events, url=url, notify_recover=notify_recover)
    except Exception as e:            # 通知失败不该把探测或页面拖垮
        res = {"sent": False, "count": 0, "skipped": len(events),
               "error": f"{type(e).__name__}: {e}"}
    delivered = [t for t in events
                 if t.get("to") is None or t.get("to") == "ALERTING"]
    if not notify_recover:
        delivered = [t for t in delivered if t.get("to") is not None]
    res.update({
        "enabled": bool(url),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "itemKinds": ["repeat" if t.get("repeat") else "transition"
                      for t in delivered],
        "items": [f"{t.get('endpoint')} → {t.get('to_label')}" for t in delivered],
    })
    _append_notify_log(res)
    return res


def _notify_status() -> dict:
    """给设置页用的通知状态：配了什么、上次发没发出去。"""
    from .settings import mask_webhook, notify_config, notify_repeat_hours

    url, notify_recover = notify_config()
    log = _load_notify_log()
    last = next((r for r in reversed(log) if r.get("count") or r.get("error")), None)
    return {
        "notifyWebhook": mask_webhook(url),
        "notifyWebhookSet": bool(url),
        "notifyRecover": notify_recover,
        "notifyRepeatHours": notify_repeat_hours(),
        "lastNotify": ({"at": last.get("at"), "sent": bool(last.get("sent")),
                        "count": last.get("count"), "error": last.get("error")}
                       if last else None),
    }


@app.get("/api/reminders", tags=["运维"])
def reminders():
    """到期 / 额度低 / 连续鉴权失败 / 模型下线，附带状态机流转。

    状态机看到的是**未过滤**的扫描结果：被确认、延后、静音的提醒仍在持续
    观察，否则静音到期后会以"新问题"的姿态重新报一遍。
    """
    from . import ledger
    from . import reminder_state as rs
    from .settings import notify_repeat_hours, reminder_tuning

    conn = _conn()
    try:
        items = ledger.scan_reminders(conn, persist=False)
        round_id = _probe_round_id(conn)
    finally:
        conn.close()

    confirm_n, recover_m = reminder_tuning()
    enriched, transitions, _tracked, repeats = rs.observe(
        items, confirm_n=confirm_n, recover_m=recover_m, round_id=round_id,
        repeat_hours=notify_repeat_hours())

    actions = _load_reminder_actions()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    visible, muted, muted_keys = [], 0, set()
    for it in enriched:
        a = actions.get(it["key"])
        if a and a.get("until") and datetime.fromisoformat(a["until"]) > now:
            muted += 1
            muted_keys.add(it["key"])
            continue
        it["acked"] = bool(a and a.get("acked") and a.get("action") == "ack")
        visible.append(it)

    # 静音期内不再重复提醒：用户已明确表示"这段时间别打扰我"。
    # 列表里也不展示，免得和"静音生效中"的提示自相矛盾。
    repeats = [r for r in repeats if r.get("key") not in muted_keys]

    # 排序：持续告警 > 待确认 > 恢复中；同组按"这件事存在多久"降序
    _order = {"ALERTING": 0, "PENDING": 1, "RECOVERING": 2}
    visible.sort(key=lambda x: (_order.get(x.get("state"), 3), -(x.get("age_min") or 0)))

    summary = {"pending": 0, "alerting": 0, "recovering": 0}
    for it in visible:
        s = (it.get("state") or "").lower()
        if s in summary:
            summary[s] += 1
    # 角标口径：已确认的故障才计入，"未接入"是待办不是故障
    actionable = sum(1 for it in visible
                     if it.get("state") == "ALERTING" and it.get("kind") != "NOT_CONFIGURED")
    # 有人打开页面时顺手把翻转推出去；无人值守的轮次由定时探测那条路发，
    # 两边共用同一个批次标识，所以同一次翻转只会发一遍
    notify_res = _notify_transitions(transitions, repeats, muted=muted_keys)
    return {"items": visible, "count": len(visible), "muted": muted,
            "summary": summary, "actionable": actionable,
            "transitions": [t for t in transitions if t.get("to") != rs.PENDING],
            "repeats": repeats,
            "notify": {"enabled": notify_res["enabled"], "sent": notify_res["sent"],
                       "count": notify_res["count"], "at": notify_res["at"]},
            "tuning": {"confirmN": confirm_n, "recoverM": recover_m}}


@app.get("/api/config", tags=["下发"])
def config_preview(scene: str = Query("agent", pattern="^(agent|speed|longterm)$")):
    """下发预览：返回将要写入 LiteLLM 的 YAML 与跳过原因，不落盘。"""
    from . import export as ex
    conn = _conn()
    try:
        text, excluded = ex.build_config(conn, scene, "default")
    finally:
        conn.close()
    return {"scene": scene, "yaml": text, "excluded": excluded,
            "count": text.count("- model_name:") if text else 0}


@app.post("/api/config/apply", tags=["下发"])
def config_apply(scene: str = Query("agent", pattern="^(agent|speed|longterm)$"),
                 confirm: bool = Query(False, description="必须显式确认为 true 才落盘")):
    """真实下发：写 LiteLLM 配置并保留上一版以支持回滚。

    安全约束：
    - 必须 confirm=true（前端二次确认），避免误触。
    - 仅 allow_proxy 明确允许的端点进入，UNKNOWN 一律跳过。
    - Key 以 os.environ/<变量名> 引用，绝不写明文。
    """
    if not confirm:
        raise HTTPException(400, "需要 confirm=true 显式确认后才落盘")
    from . import export as ex
    conn = _conn()
    try:
        result = ex.write_config(conn, scene, "default")
    finally:
        conn.close()
    return result


@app.post("/api/config/rollback", tags=["下发"])
def config_rollback(confirm: bool = Query(False)):
    """回滚到上一版 LiteLLM 配置。"""
    if not confirm:
        raise HTTPException(400, "需要 confirm=true 显式确认")
    from . import export as ex
    return ex.rollback()


# ---------------------------------------------------------------------------
# 5. 门槛台账
# ---------------------------------------------------------------------------
@app.get("/api/catalog", tags=["门槛台账"])
def catalog():
    conn = _conn()
    rows = _rows(conn, """
        SELECT p.id provider_id, p.name provider, p.website, p.kind,
               c.signup_method, c.free_form, c.rate_limits, c.allow_proxy,
               c.allow_commercial, c.source_url, c.source_type, c.verified_at,
               c.confidence, c.note, c.endpoint_label,
               c.requires_phone, c.requires_card, c.requires_realname,
               c.region_restricted, c.requires_review
        FROM catalog_entry c JOIN provider p ON p.id=c.provider_id
        ORDER BY p.name
    """)

    # 「已接入」的真实口径：该服务商名下真的有被探测到的模型 / 探测记录。
    # 不能只看 endpoint_label（那只是手填的关联标签，不代表真跑通过）。
    for r in rows:
        r["probed"] = False
        r["probed_models"] = 0

        # 直连：provider_id 直接挂 endpoint
        ep_ids = [x["id"] for x in conn.execute(
            "SELECT id FROM endpoint WHERE provider_id=?", (r["provider_id"],))]
        if ep_ids:
            for eid in ep_ids:
                n = conn.execute(
                    "SELECT COUNT(*) FROM probe_result WHERE endpoint_id=?",
                    (eid,)).fetchone()[0]
                if n:
                    r["probed"] = True
                    r["probed_models"] += conn.execute(
                        "SELECT COUNT(DISTINCT model_id) FROM probe_result "
                        "WHERE endpoint_id=? AND model_id IS NOT NULL",
                        (eid,)).fetchone()[0]

        # 聚合（OpenRouter）：按模型前缀判断该服务商的免费模型是否真被探测到
        prefix = OR_PREFIX.get(r["provider"])
        if prefix:
            row = conn.execute(
                """SELECT COUNT(DISTINCT m.model_id) n FROM model m
                   JOIN endpoint e ON e.id=m.endpoint_id
                   WHERE e.label='OpenRouter' AND m.model_id LIKE ?""",
                (prefix + "%",)).fetchone()
            if row and row["n"]:
                r["probed"] = True
                r["probed_models"] = row["n"]

    conn.close()
    return {"items": rows}


class CatalogEntryIn(BaseModel):
    provider: str = Field(..., description="服务商名称（须与 provider 表一致或新建）")
    signup_method: str | None = None
    free_form: str | None = None
    rate_limits: str | None = None
    allow_proxy: str | None = Field(None, description="YES/NO/UNKNOWN；UNKNOWN 一律按禁止转发")
    allow_commercial: str | None = None
    source_url: str | None = None
    source_type: str = Field("dataset", pattern="^(dataset|official|manual)$")
    verified_at: str | None = None
    confidence: str | None = None
    note: str | None = None
    requires_phone: str | None = Field(None, pattern="^(YES|NO|UNKNOWN)$")
    requires_card: str | None = Field(None, pattern="^(YES|NO|UNKNOWN)$")
    requires_realname: str | None = Field(None, pattern="^(YES|NO|UNKNOWN)$")
    region_restricted: str | None = Field(None, pattern="^(YES|NO|UNKNOWN)$")
    requires_review: str | None = Field(None, pattern="^(YES|NO|UNKNOWN)$")


@app.post("/api/catalog/entry", tags=["门槛台账"])
def catalog_upsert(entry: CatalogEntryIn):
    """写入/更新门槛台账。字段变化由 ledger 写入政策变更历史，不覆盖旧值。"""
    from . import ledger
    conn = _conn()
    try:
        ledger.upsert_catalog(conn, entry.provider, **entry.model_dump(exclude={"provider"}))
        row = conn.execute(
            """SELECT c.*, p.name provider FROM catalog_entry c
               JOIN provider p ON p.id=c.provider_id WHERE p.name=?""",
            (entry.provider,)).fetchone()
        changes = conn.execute(
            "SELECT COUNT(*) FROM provider_policy_history WHERE provider_id=?",
            (row["provider_id"],)).fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "provider": entry.provider,
            "sourceType": entry.source_type, "policyHistoryRows": changes}


# ---------------------------------------------------------------------------
# 6. 触发探测（显式动作，受预算约束）
# ---------------------------------------------------------------------------
class ProbeRequest(BaseModel):
    endpoint: str | None = Field(None, description="只探测指定端点，留空则全部")
    level: str = Field("L2", pattern="^(L0|L2)$")
    async_run: bool = True


class ScheduleIn(BaseModel):
    enabled: bool
    interval_min: int = Field(30, ge=5, le=1440, description="检测间隔（分钟），5~1440")


@app.get("/api/schedule", tags=["运维"])
def schedule_status():
    job = STATE.scheduler.get_job("probe") if STATE.scheduler else None
    interval = None
    if job is not None:
        # interval 触发器以秒为单位
        try:
            interval = max(1, round(job.trigger.interval.total_seconds() / 60))
        except Exception:
            interval = None
    next_run = None
    if job is not None and job.next_run_time is not None:
        next_run = job.next_run_time.isoformat()
    return {"enabled": job is not None, "intervalMin": interval,
            "nextRunAt": next_run,
            "source": "runtime" if STATE.scheduler else "off"}


@app.post("/api/schedule", tags=["运维"])
def schedule_set(req: ScheduleIn):
    """开关/调整自动定时检测；偏好持久化，重启后自动恢复。"""
    try:
        _apply_schedule(req.enabled, req.interval_min)
        # 手动开启 / 改间隔后同样按需补一轮：用户刚打开定时检测，多半正盯着空荡荡的页面。
        if req.enabled:
            _arm_startup_catchup(req.interval_min)
    except ImportError:
        raise HTTPException(503, "APScheduler 未安装，无法开启定时检测")
    _save_schedule_pref({"enabled": req.enabled, "intervalMin": req.interval_min})
    return schedule_status()


REMINDER_ACTIONS_FILE = ROOT / "data" / "reminder_actions.json"


def _load_reminder_actions() -> dict:
    try:
        return json.loads(REMINDER_ACTIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_reminder_actions(actions: dict) -> None:
    try:
        REMINDER_ACTIONS_FILE.write_text(json.dumps(actions, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


class ReminderAckIn(BaseModel):
    key: str = Field(..., description="提醒唯一键：kind|endpoint")
    action: str = Field(..., pattern="^(ack|snooze|mute)$")
    days: int = Field(1, ge=0, le=365, description="snooze 的天数")


@app.post("/api/reminders/ack", tags=["提醒中心"])
def reminder_ack(req: ReminderAckIn):
    """确认 / 延后 / 静音某条提醒。静音与延后有期限，到期自动重新出现。"""
    actions = _load_reminder_actions()
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    entry = {"action": req.action}
    if req.action == "snooze":
        entry["until"] = (now + timedelta(days=req.days)).isoformat()
    elif req.action == "mute":
        entry["until"] = (now + timedelta(days=365)).isoformat()
    else:
        entry["until"] = (now + timedelta(days=1)).isoformat()
        entry["acked"] = True
    actions[req.key] = entry
    _save_reminder_actions(actions)
    return {"ok": True, "key": req.key, "action": req.action}


@app.post("/api/probe/run", tags=["运维"])
def run_probe(req: ProbeRequest):
    cmd = [sys.executable, "-m", "hub.run", "--once"]
    if req.endpoint:
        cmd += ["--only", req.endpoint]
    # 已在跑就直接回绝，不再叠一轮。409 让前端能提示「已有一轮在跑」而非「发起失败」。
    if not _PROBE_LOCK.acquire(blocking=False):
        raise HTTPException(409, "已有一轮检测在进行中，请等它结束再点")

    if req.async_run:
        try:
            proc = subprocess.Popen(cmd, cwd=ROOT,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=_NO_WINDOW)
        except Exception:
            # 没起来就把锁还回去，否则从此再也发不起探测。
            _PROBE_LOCK.release()
            raise

        # 释放权交给看护线程：子进程结束（成功/失败/超时被杀）才放锁，
        # 这样异步返回也不会破坏「同一时刻只有一轮」。
        def _release_when_done(p=proc):
            try:
                p.wait()
            finally:
                _PROBE_LOCK.release()

        threading.Thread(target=_release_when_done, daemon=True).start()
        return {"started": True, "command": " ".join(cmd), "async": True,
                "note": "探测在后台运行，稍后刷新页面查看新数据"}

    # 同步模式：本轮跑完即放锁（含超时 504 的路径）。
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180,
                           creationflags=_NO_WINDOW)
        _round_after_probe()
        return {"started": True, "async": False, "returncode": p.returncode,
                "output": p.stdout[-2000:], "error": _redact(p.stderr[-1000:])}
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "probe timeout")
    finally:
        _PROBE_LOCK.release()


@app.get("/api/settings", tags=["运维"])
def get_settings():
    """运行参数读取（含自定义测试问题、提醒阈值、通知渠道、探测预算）。密钥不在其中。"""
    from .settings import budget_config, load_settings
    s = load_settings()
    _benabled, _bpct = budget_config()
    return {"probePrompt": s.get("probePrompt", ""), "isCustom": bool(s.get("probePrompt")),
            "reminderConfirmN": s.get("reminderConfirmN", 2),
            "reminderRecoverM": s.get("reminderRecoverM", 2),
            "budgetEnabled": _benabled, "budgetGuardPct": _bpct,
            **_notify_status()}


class SettingsIn(BaseModel):
    # None = 本次不修改；显式传 "" 仍表示「恢复默认问题」。
    # 两个提醒阈值可以单独提交，不必连带把 probePrompt 一起写空。
    probePrompt: str | None = Field(None, max_length=500, description="自定义测试问题；空 = 使用默认问题")
    reminderConfirmN: int | None = Field(None, ge=1, le=10,
                                         description="连续命中几次才算确认故障（待确认期长度）")
    reminderRecoverM: int | None = Field(None, ge=1, le=10,
                                         description="连续几轮正常才算恢复（恢复期长度）")
    notifyWebhook: str | None = Field(None, max_length=500,
                                      description="机器人 webhook 地址；空 = 关闭通知")
    notifyRecover: bool | None = Field(None, description="是否也推送「已恢复」")
    notifyRepeatHours: int | None = Field(None, ge=0, le=168,
                                          description="故障未恢复时每隔几小时再提醒；0 = 不重发")
    budgetEnabled: bool | None = Field(None, description="是否启用每日探测预算")
    budgetGuardPct: int | None = Field(None, ge=1, le=100,
                                       description="用到预算的这个百分比就开始降级")


_KEEP = object()


def _webhook_patch(req_value: str):
    """把页面提交的地址转成入库值。

    页面回显的是脱敏地址（`…?***`），用户不改动就原样提交回来——
    那不是真地址，当成"不修改"，否则一保存就把密钥写成了 `***`。
    """
    val = str(req_value or "").strip()
    if val.endswith("?***") or val.endswith("?***/"):
        return _KEEP
    return val


@app.post("/api/settings", tags=["运维"])
def update_settings(req: SettingsIn):
    from .settings import save_settings
    patch = {}
    if req.probePrompt is not None:
        patch["probePrompt"] = req.probePrompt
    if req.reminderConfirmN is not None:
        patch["reminderConfirmN"] = req.reminderConfirmN
    if req.reminderRecoverM is not None:
        patch["reminderRecoverM"] = req.reminderRecoverM
    if req.notifyWebhook is not None:
        val = _webhook_patch(req.notifyWebhook)
        if val is not _KEEP:
            patch["notifyWebhook"] = val
    if req.notifyRecover is not None:
        patch["notifyRecover"] = req.notifyRecover
    if req.notifyRepeatHours is not None:
        patch["notifyRepeatHours"] = req.notifyRepeatHours
    if req.budgetEnabled is not None:
        patch["budgetEnabled"] = req.budgetEnabled
    if req.budgetGuardPct is not None:
        patch["budgetGuardPct"] = req.budgetGuardPct
    try:
        s = save_settings(patch)
    except ValueError as e:
        raise HTTPException(422, str(e))
    from .settings import budget_config
    _benabled, _bpct = budget_config()
    return {"ok": True, "probePrompt": s.get("probePrompt", ""),
            "isCustom": bool(s.get("probePrompt")),
            "reminderConfirmN": s.get("reminderConfirmN", 2),
            "reminderRecoverM": s.get("reminderRecoverM", 2),
            "budgetEnabled": _benabled, "budgetGuardPct": _bpct,
            **_notify_status()}


class NotifyTestIn(BaseModel):
    # 空 = 用已保存的地址试发；也可临时填一个地址先验证再保存
    webhook: str | None = Field(None, max_length=500)


@app.post("/api/settings/notify-test", tags=["运维"])
def notify_test(req: NotifyTestIn):
    """发一条测试消息。配通知最怕"存进去了但一直是坏的"，得能当场验证。"""
    from . import notify
    from .settings import notify_config

    saved, _recover = notify_config()
    url = str(req.webhook or "").strip() or saved
    if not url:
        raise HTTPException(422, "还没有填写通知地址")
    events = [{
        "endpoint": "测试消息", "from": None, "to": "ALERTING", "to_label": "持续告警",
        "reason": "这条来自设置页的「发送测试」，说明通知链路是通的",
    }]
    try:
        ok = notify.send_webhook(url, events)
    except Exception as e:
        raise HTTPException(502, f"发送失败：{type(e).__name__}: {e}")
    if not ok:
        raise HTTPException(502, "发送失败：对方没有接受这条消息，请检查地址是否完整")
    return {"ok": True, "sent": True}


@app.get("/api/health", tags=["运维"])
def health():
    conn = _conn()
    n = conn.execute("SELECT COUNT(*) FROM probe_result").fetchone()[0]
    latest = conn.execute("SELECT MAX(created_at) FROM probe_result").fetchone()[0]
    conn.close()
    # 数据库可能被配置到仓库外（绝对路径），此时 relative_to 会抛异常，
    # 不能让它把健康检查打成 500——健康检查本身必须永远可用。
    try:
        db_repr = str(DB_PATH.relative_to(ROOT))
    except ValueError:
        db_repr = str(DB_PATH)
    return {"ok": True, "db": db_repr, "probeRows": n,
            "latestProbeAt": latest, "scheduler": bool(STATE.scheduler)}


# ---------------------------------------------------------------------------
# 静态前端：同源托管 web/，这样 API_BASE 留空即可，免除跨域配置。
# 必须放在所有 /api 路由之后，避免被 catch-all 抢先匹配。
# ---------------------------------------------------------------------------
WEB_DIR = ROOT / "web"


class NoCacheStatic(StaticFiles):
    """静态资源禁用强缓存：前端改动后刷新即生效，避免用户看到旧 JS/CSS。"""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


if WEB_DIR.exists():
    app.mount("/", NoCacheStatic(directory=str(WEB_DIR), html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("TOKRIVA_PORT", "8766")))
