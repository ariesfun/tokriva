"""数据库层：SQLite schema 与写入。

沿用设计文档第 5 节的 9 张表，阶段 0 只落地支撑探测与排名必需的 6 张，
其余（alias_group / rank_snapshot）留到阶段 2。

时间口径（**全栈统一为运行机器的本地时间**，本机即北京时间）：
- 库内时间戳默认值统一写成 `datetime('now','localtime')`，落库形态是
  `YYYY-MM-DD HH:MM:SS` 的**本地朴素串**。
- Python 侧取「现在」一律用 `now()`（= `datetime.now()`）；凡与库中时间戳
  比较都用同一口径，不再做任何 UTC/本地换算。
- 前端不再换算时区，直接把库里的串按本地时间展示。

历史背景：早期默认值写成 `datetime('now')`（**UTC** 朴素串），而 Python 侧
`datetime.now()` 是本地时间——两者差 8 小时，会把「新鲜度 24h」压成 16h、
让「已到期」提前 8 小时触发。`_rebuild_to_local_time()` 会把旧库一次性重建为
本地口径并把存量时间戳整体平移（只跑一次，见该函数）。

若部署到非 UTC+8 机器，请显式设置进程时区（如 `TZ=Asia/Shanghai`），
让 SQLite 的 `localtime` 与 Python 的 `datetime.now()` 都落在北京时间。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


def now() -> datetime:
    """当前**本地**时间（naive），与库中 `datetime('now','localtime')` 同口径。

    凡是要跟 created_at / expires_at / captured_at 比较的地方，一律用它取
    「现在」；不要再引入 `datetime.now(timezone.utc)`——那会跟库值差 8 小时。
    """
    return datetime.now()


def as_local(dt: datetime) -> datetime:
    """把可能带时区的 datetime 归一到 naive 本地时间，便于与库中时间戳比较。

    库里的值是 naive 本地时间；外部来源（如服务商返回的 ISO8601）可能带时区，
    直接跟 naive 值比较会抛 TypeError，故先统一到同一口径。
    """
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def now_str() -> str:
    """当前本地时间的库内文本形态（`YYYY-MM-DD HH:MM:SS`）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS provider (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    website       TEXT,
    kind          TEXT,                 -- model_api / gpu_cloud / serverless / proxy_station
    capabilities  TEXT,                 -- JSON: 能力声明
    provider_status TEXT DEFAULT 'UNKNOWN',
    created_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS catalog_entry (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id   INTEGER REFERENCES provider(id),
    signup_method TEXT,                 -- email / phone / card / kyc / region_locked
    free_form     TEXT,                 -- permanent / one_time / periodic / activity / apply
    rate_limits   TEXT,
    allow_proxy   TEXT DEFAULT 'UNKNOWN',
    allow_commercial TEXT DEFAULT 'UNKNOWN',
    source_url    TEXT,
    source_type   TEXT,                 -- dataset / official / manual
    verified_at   TEXT,
    confidence    TEXT DEFAULT 'UNKNOWN',
    quota_scope   TEXT DEFAULT 'UNKNOWN',
    quota_visibility TEXT DEFAULT 'UNKNOWN',
    training_on_free_data TEXT DEFAULT 'UNKNOWN',
    note          TEXT,
    endpoint_label TEXT
);

CREATE TABLE IF NOT EXISTS endpoint (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id   INTEGER REFERENCES provider(id),
    label         TEXT NOT NULL UNIQUE,  -- 别名，如 LightVela
    base_url      TEXT NOT NULL,
    protocol      TEXT NOT NULL,         -- openai_chat / openai_responses
    auth_env      TEXT NOT NULL,         -- 环境变量名，不存明文
    default_model TEXT,
    use_proxy     INTEGER DEFAULT 0,
    active        INTEGER DEFAULT 1,
    daily_budget  INTEGER DEFAULT 200,   -- 每日探测预算
    has_utilization INTEGER DEFAULT 0,   -- 是否提供 /models/utilization
    has_usage     INTEGER DEFAULT 0,     -- 是否提供 /usage
    include_usage INTEGER DEFAULT 1,     -- 请求带 stream_options.include_usage
    timeout_sec   INTEGER DEFAULT 60,     -- 单次探测超时（秒）
    expires_at    TEXT,
    note          TEXT,
    created_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS model (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id   INTEGER REFERENCES endpoint(id),
    model_id      TEXT NOT NULL,
    display_name  TEXT,
    supports_tools INTEGER,              -- NULL = 未知
    supports_reasoning INTEGER,          -- NULL = 未知
    context_length INTEGER,              -- 上下文窗口；NULL = 未知
    context_source TEXT DEFAULT 'UNKNOWN', -- declared_api / manual / observed / UNKNOWN
    max_output_tokens INTEGER,
    modalities    TEXT,
    served_as     TEXT,                  -- 通过哪个别名请求到的（如 auto）
    is_chat       INTEGER,               -- 是否对话模型（d3 这类为 0）
    is_free       INTEGER,               -- 是否免费
    status        TEXT DEFAULT 'ACTIVE',
    first_seen    TEXT DEFAULT (datetime('now','localtime')),
    last_seen     TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(endpoint_id, model_id)
);

CREATE TABLE IF NOT EXISTS probe_result (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id   INTEGER REFERENCES endpoint(id),
    model_id      TEXT,
    level         TEXT,                  -- L0 / L1 / L2
    status        TEXT NOT NULL,
    dns_ms        REAL,
    tcp_ms        REAL,
    tls_ms        REAL,
    ttfb_ms       REAL,
    ttft_ms       REAL,
    output_tokens INTEGER,
    tps           REAL,
    total_ms      REAL,
    token_source  TEXT,                  -- API / ESTIMATED
    http_status   INTEGER,
    error_kind    TEXT,
    error_body    TEXT,                  -- 脱敏截断
    response_shape TEXT,                 -- json / html / sse / empty
    stream_mode   TEXT,                 -- true_stream / buffered / single
    served_model  TEXT,                 -- 别名背后实际服务的模型
    cost_usd      REAL,                 -- 单次成本（部分服务商 usage 带 cost）
    reasoning_tokens INTEGER,
    retry_after   REAL,                 -- 429 时的 Retry-After 秒数
    timing_scope  TEXT,                 -- direct / proxy_hop（三段耗时描述哪条连接）
    connect_ms    REAL,                 -- 代理 CONNECT 隧道耗时（与 TLS 分开）
    egress        TEXT,                 -- direct / proxy:<节点>（出口路径标识）
    probe_version TEXT,                 -- 探针代码版本
    request_json  TEXT,                 -- 请求体（已剔除凭证，截断）
    resp_headers  TEXT,                 -- 响应头（保留限流字段，剔除敏感项）
    resp_body     TEXT,                 -- 响应体截断（成功与失败都存）
    usage_json    TEXT,                 -- 完整 usage 原始 JSON
    proxied       INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_probe_endpoint_time ON probe_result(endpoint_id, created_at);
CREATE INDEX IF NOT EXISTS idx_probe_status ON probe_result(status, created_at);

CREATE TABLE IF NOT EXISTS quota_snapshot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id   INTEGER REFERENCES endpoint(id),
    quota_type    TEXT,                  -- DAILY/WEEKLY/MONTHLY/TOTAL/ONE_TIME/TRIAL/SUBSCRIPTION
                                         -- 另有 RATE_LIMIT：机器抓来的速率窗，**不是额度**
    unit          TEXT,                  -- TOKEN/REQUEST/USD/GPU_HOUR
    total         REAL,
    used          REAL,
    remaining     REAL,
    source        TEXT,                  -- API/HEADER/MANUAL/ESTIMATED
    quota_scope   TEXT DEFAULT 'UNKNOWN',   -- KEY/PROJECT/ORG/ACCOUNT/IP
    quota_visibility TEXT DEFAULT 'UNKNOWN',-- PUBLIC/CONSOLE_ONLY/UNPUBLISHED
    confidence    TEXT DEFAULT 'UNKNOWN',   -- CONFIRMED/LIKELY/POSSIBLE/UNKNOWN
    reset_at      TEXT,
    expires_at    TEXT,
    captured_at   TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS model_utilization (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id   INTEGER REFERENCES endpoint(id),
    model_id      TEXT NOT NULL,
    utilization   REAL,                  -- 0-100
    state         TEXT,                  -- idle / busy / full
    sampled_at    TEXT,                  -- 服务商给出的采样时刻
    captured_at   TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_util_endpoint ON model_utilization(endpoint_id, captured_at);

CREATE TABLE IF NOT EXISTS provider_policy_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id   INTEGER REFERENCES provider(id),
    catalog_entry_id INTEGER REFERENCES catalog_entry(id),
    field         TEXT,
    old_value     TEXT,
    new_value     TEXT,
    changed_at    TEXT DEFAULT (datetime('now','localtime')),
    source        TEXT,
    confirmed_by  TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.executescript(SCHEMA_EXTRA)
    # 旧库（默认值仍是 UTC）在此一次性重建为本地口径。重建会 DROP 掉表，
    # 连带删掉其索引，故之后要再跑一遍建表/建索引脚本把索引补回来。
    if _rebuild_to_local_time(conn):
        conn.executescript(SCHEMA)
        conn.executescript(SCHEMA_EXTRA)
    # 速率行重打标：纯 UPDATE，不动表结构，不需要补索引。
    _retag_rate_limit_rows(conn)
    return conn


def connect_ro(db_path: str | Path) -> sqlite3.Connection:
    """只读打开：不建表、不迁移、不写。用于公开导出等纯读取场景。

    优先走 `mode=ro` URI；WAL 库在个别平台只读打不开时（需要 -shm 写权限）
    退回普通连接——此时也只是读，且库已是最新 schema，迁移为空操作。
    调用方负责 `close()`。
    """
    p = Path(db_path)
    try:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        return conn
    except sqlite3.Error:
        return connect(p)


def seed_endpoints(conn: sqlite3.Connection, endpoints: list[dict]) -> None:
    """幂等写入 provider + endpoint。"""
    for ep in endpoints:
        conn.execute(
            "INSERT INTO provider(name, website, kind) VALUES(?,?,?) "
            "ON CONFLICT(name) DO NOTHING",
            (ep["provider"], ep.get("website"), ep.get("kind", "model_api")),
        )
        pid = conn.execute(
            "SELECT id FROM provider WHERE name=?", (ep["provider"],)
        ).fetchone()["id"]
        # daily_budget 只在首次插入时取默认值，**不进 upsert 分支**：
        # 它由用户在接口详情里自行设定，若每轮探测都按代码里的常量覆写，
        # 页面上的「每天最多测几次」就成了改不动的假开关。其余字段仍是
        # 代码侧为准（连接参数变了要能跟着更新）。
        conn.execute(
            """INSERT INTO endpoint(provider_id,label,base_url,protocol,auth_env,
                                    default_model,use_proxy,daily_budget,
                                    has_utilization,has_usage,include_usage,timeout_sec,note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(label) DO UPDATE SET
                 base_url=excluded.base_url,
                 protocol=excluded.protocol,
                 auth_env=excluded.auth_env,
                 default_model=excluded.default_model,
                 use_proxy=excluded.use_proxy,
                 has_utilization=excluded.has_utilization,
                 has_usage=excluded.has_usage,
                 include_usage=excluded.include_usage,
                 timeout_sec=excluded.timeout_sec""",
            (
                pid,
                ep["label"],
                ep["base_url"],
                ep["protocol"],
                ep["auth_env"],
                ep.get("default_model"),
                int(ep.get("use_proxy", 0)),
                ep.get("daily_budget", 200),
                int(ep.get("utilization", 0)),
                int(ep.get("usage", 0)),
                int(ep.get("include_usage", 1)),
                ep.get("timeout_sec", 60),
                ep.get("note"),
            ),
        )
    conn.commit()


def record_probe(conn: sqlite3.Connection, r: dict) -> int:
    cols = (
        "endpoint_id model_id level status dns_ms tcp_ms tls_ms ttfb_ms ttft_ms "
        "output_tokens tps total_ms token_source http_status error_kind error_body "
        "response_shape stream_mode served_model cost_usd reasoning_tokens retry_after "
        "timing_scope connect_ms egress probe_version request_json resp_headers resp_body "
        "usage_json proxied prompt_tag"
    ).split()
    cur = conn.execute(
        f"INSERT INTO probe_result({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        tuple(r.get(c) for c in cols),
    )
    conn.commit()
    return cur.lastrowid


# 需要向后兼容补齐的列：(表, 列, 类型定义)
_MIGRATIONS = [
    ("model", "display_name", "TEXT"),
    ("model", "supports_reasoning", "INTEGER"),
    ("model", "context_source", "TEXT DEFAULT 'UNKNOWN'"),
    ("model", "max_output_tokens", "INTEGER"),
    ("model", "modalities", "TEXT"),
    ("model", "served_as", "TEXT"),
    ("probe_result", "stream_mode", "TEXT"),
    ("probe_result", "served_model", "TEXT"),
    ("probe_result", "cost_usd", "REAL"),
    ("probe_result", "reasoning_tokens", "INTEGER"),
    ("probe_result", "retry_after", "REAL"),
    ("probe_result", "timing_scope", "TEXT"),
    ("probe_result", "connect_ms", "REAL"),
    ("probe_result", "egress", "TEXT"),
    ("probe_result", "probe_version", "TEXT"),
    ("probe_result", "request_json", "TEXT"),
    ("probe_result", "prompt_tag", "TEXT DEFAULT 'default'"),
    ("probe_result", "resp_headers", "TEXT"),
    ("probe_result", "resp_body", "TEXT"),
    ("probe_result", "usage_json", "TEXT"),
    ("model", "is_chat", "INTEGER"),
    ("model", "is_free", "INTEGER"),
    ("endpoint", "has_utilization", "INTEGER DEFAULT 0"),
    ("endpoint", "has_usage", "INTEGER DEFAULT 0"),
    ("endpoint", "include_usage", "INTEGER DEFAULT 1"),
    ("endpoint", "timeout_sec", "INTEGER DEFAULT 60"),
    ("quota_snapshot", "quota_scope", "TEXT DEFAULT 'UNKNOWN'"),
    ("quota_snapshot", "quota_visibility", "TEXT DEFAULT 'UNKNOWN'"),
    ("quota_snapshot", "confidence", "TEXT DEFAULT 'UNKNOWN'"),
    ("catalog_entry", "requires_phone", "TEXT DEFAULT 'UNKNOWN'"),
    ("catalog_entry", "requires_card", "TEXT DEFAULT 'UNKNOWN'"),
    ("catalog_entry", "requires_realname", "TEXT DEFAULT 'UNKNOWN'"),
    ("catalog_entry", "region_restricted", "TEXT DEFAULT 'UNKNOWN'"),
    ("catalog_entry", "requires_review", "TEXT DEFAULT 'UNKNOWN'"),
    ("catalog_entry", "confidence", "TEXT DEFAULT 'UNKNOWN'"),
    ("catalog_entry", "quota_scope", "TEXT DEFAULT 'UNKNOWN'"),
    ("catalog_entry", "quota_visibility", "TEXT DEFAULT 'UNKNOWN'"),
    ("catalog_entry", "training_on_free_data", "TEXT DEFAULT 'UNKNOWN'"),
    ("catalog_entry", "endpoint_label", "TEXT"),
]


def migrate(conn: sqlite3.Connection) -> None:
    """给旧库补列，避免删库重来。"""
    for table, col, decl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


# 时间口径迁移版本号：0 = 旧库（默认值 UTC）；1 = 本地时间口径。
_LOCAL_TIME_VERSION = 1

# 额度类型口径版本号：≤1 = 速率限制行与「订阅类权益」共用 `SUBSCRIPTION`；
# 2 = 速率行改用专用类型 `RATE_LIMIT`，`SUBSCRIPTION` 回归本义（见 quota_rules 顶部注释）。
_RATE_LIMIT_VERSION = 2

# 额外平移列表：**只平移 schema 默认值列**（那一定是 SQLite 的 UTC 记录时刻），
# 业务写入的列一律不动——它们的历史口径不可辨，平移反而会破坏数据。
#
# 为什么 reset_at / expires_at 不能平移：这些列由不同版本代码写过两种形态——
# 早期 `fromtimestamp` 用**本地**时区（落库已是本地时间）、后期改用 **UTC**
# （落库是 UTC 尾随 `Z`）。实测存量 93 行里 90 行是前者（本地形态），只有 3 行
# 是后者；若统一 +8，会把本来正确的 90 行平移到 08:00。
# 这些列今后由新代码按本地口径写入即可，存量值不再回改。
_EXTRA_SHIFT_COLS: list[tuple[str, str, bool]] = []


def _shift_expr(col: str, date_only: bool) -> str:
    """把某列的存量 UTC 文本平移到本地时间（SQL 片段）。"""
    if date_only:
        return (f'CASE WHEN length("{col}")<=10 THEN "{col}" '
                f'ELSE datetime("{col}", \'localtime\') END')
    return f'datetime("{col}", \'localtime\')'


def _rebuild_to_local_time(conn: sqlite3.Connection) -> bool:
    """把旧库的时间默认值与存量时间戳迁到「本地时间」口径；已迁过则不动。

    SQLite 不支持 `ALTER TABLE ... ALTER COLUMN`，要改默认值只能整表重建：
    按 `sqlite_master` 里的原建表语句重建一张 `*__new`（把 `datetime('now')`
    换成 `datetime('now','localtime')`），把旧行按本地时区平移后灌进去，再改名
    顶替。用 `PRAGMA user_version` 守卫，整个重放只做一次。

    只有含 `datetime('now')` 默认值的表会被重建——这些正是「记录时刻」的列，
    语义确定是 SQLite 的 UTC 绝对时刻，平移一定正确。业务写入的列（如
    `endpoint.expires_at`、`quota_snapshot.reset_at`）历史口径不可辨，**不迁移**
    （见 `_EXTRA_SHIFT_COLS` 的说明）。返回是否真的改动了库内容。
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= _LOCAL_TIME_VERSION:
        return False

    import re
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]
    changed = False
    conn.commit()  # PRAGMA 需在无活动事务时执行才生效
    conn.execute("PRAGMA foreign_keys=OFF")
    for t in tables:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (t,)).fetchone()
        sql = row[0] if row else None
        if not sql or "datetime('now')" not in sql:
            continue
        ts_cols = set(re.findall(
            r"([A-Za-z_]\w*)\s+TEXT\s+DEFAULT\s*\(datetime\('now'\)\)", sql))
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        if not cols:
            continue

        # 原语句里第一次出现的表名就是表名本身（在 "CREATE TABLE [IF NOT EXISTS]"
        # 之后）；据此拼出 `<t>__new`，并把默认值换成 localtime。
        head, _, tail = sql.partition(t)
        new_sql = (head + t + "__new" + tail).replace(
            "datetime('now')", "datetime('now','localtime')")
        collist = ", ".join(f'"{c}"' for c in cols)
        select = ", ".join(
            f'datetime("{c}", \'localtime\') AS "{c}"' if c in ts_cols
            else f'"{c}"' for c in cols)

        conn.execute(new_sql)
        conn.execute(f'INSERT INTO "{t}__new" ({collist}) '
                     f'SELECT {select} FROM "{t}"')
        conn.execute(f'DROP TABLE "{t}"')
        conn.execute(f'ALTER TABLE "{t}__new" RENAME TO "{t}"')
        changed = True

    # 业务写入的时间列（非默认值列）单独平移。
    for t, col, date_only in _EXTRA_SHIFT_COLS:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')}
        if t in names and col in cols:
            conn.execute(f'UPDATE "{t}" SET "{col}"={_shift_expr(col, date_only)} '
                         f'WHERE "{col}" IS NOT NULL')
            changed = True

    conn.execute(f"PRAGMA user_version={_LOCAL_TIME_VERSION}")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    return changed


def _retag_rate_limit_rows(conn: sqlite3.Connection) -> bool:
    """把历史上误标为 `SUBSCRIPTION` 的**速率限制行**重打为 `RATE_LIMIT`；只做一次。

    为什么需要：`x-ratelimit-limit-user-rpm` / `x-ratelimit-limit-requests` 这两条
    响应头规则、以及 `/usage` 的 `rpm_limit` 字段，写的都是「每分钟多少次」的**速率**，
    却一直落在 `quota_type='SUBSCRIPTION'` 上。于是「订阅类权益」和「速率限制」共用了
    一个取值，任何按类型做的判定都会误伤另一边：
      - 接口清单的额度列会显示成「19/20 次」且没有重置时间（速率行没有 `reset_at`）；
      - 用户手工录入的「订阅额度」又会被当成速率行滤掉。
    重打之后 `SUBSCRIPTION` 回归设计文档的本义，速率行用专用类型，两边都能按类型判定。

    范围**只限机器写入的**（`source IN ('API','HEADER')` 且 `unit='REQUEST'`）：手工录入
    （`MANUAL`）的订阅额度是用户明确声明的额度，一个都不能动。条件幂等，重复执行无副作用。
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= _RATE_LIMIT_VERSION:
        return False
    cur = conn.execute(
        "UPDATE quota_snapshot SET quota_type='RATE_LIMIT' "
        "WHERE quota_type='SUBSCRIPTION' AND unit='REQUEST' "
        "AND source IN ('API','HEADER')")
    conn.execute(f"PRAGMA user_version={_RATE_LIMIT_VERSION}")
    conn.commit()
    return cur.rowcount > 0


SCHEMA_EXTRA = """
CREATE TABLE IF NOT EXISTS rank_snapshot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scene         TEXT NOT NULL,          -- agent / speed / longterm
    endpoint_id   INTEGER REFERENCES endpoint(id),
    model_id      TEXT,
    score         REAL,
    detail        TEXT,                   -- JSON: 各分项与权重
    eligible      INTEGER DEFAULT 1,
    reason        TEXT,                   -- 不合格原因
    generated_at  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_rank_scene ON rank_snapshot(scene, generated_at);

CREATE TABLE IF NOT EXISTS alias_group (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    alias         TEXT NOT NULL UNIQUE,   -- coding-main / fast-chat
    scene         TEXT,                   -- agent / speed / longterm
    members       TEXT,                   -- JSON: [{endpoint_label, model_id}]
    note          TEXT,
    updated_at    TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS probe_daily (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id   INTEGER,
    model_id      TEXT,
    level         TEXT,
    day           TEXT,
    n             INTEGER,
    n_healthy     INTEGER,
    avg_ttft      REAL,
    avg_tps       REAL,
    avg_total     REAL,
    min_total     REAL,
    max_total     REAL,
    error_kinds   TEXT,
    created_at    TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(endpoint_id, model_id, level, day)
);

CREATE TABLE IF NOT EXISTS reminder_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT,                   -- EXPIRING / QUOTA_LOW / AUTH_FAILED / MODEL_GONE
    endpoint_id   INTEGER,
    subject       TEXT,
    detail        TEXT,
    created_at    TEXT DEFAULT (datetime('now','localtime'))
);
-- 去重查询按 (kind, endpoint_id, subject) 命中窗口内的旧行，走索引避免全表扫
CREATE INDEX IF NOT EXISTS idx_reminder_kind_time
    ON reminder_log(kind, endpoint_id, created_at);
"""
