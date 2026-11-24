# 公开数据政策（Public Data Policy）

> **适用范围：** `hub/public_snapshot.py` 产出的公开快照 `snapshot.json`，以及由此生成的
> GitHub Pages 公开站。
> **适用对象：** 对外发布的静态站点（`gh-pages`）。**不约束**本机管理控制台（8766）。
> **状态：** 生效。这是发布前的**阶段门禁**——白名单未落地，不得启用正式发布。
> **基线：** 2026-11-24，对应 `docs/static-pages-assessment.md` 与
> 《Tokriva 公开静态展示站：架构设计与代码审查报告》。

本政策回答一个问题：**哪些字段可以离开本机、进入公开静态站？**

原则只有一条：**公开站只承载"已经可以直接给陌生人看"的字段，而不是"把管理 API 的响应
脱一层敏"。** 因此采用**正向白名单**——构建器逐字段挑选，而不是对管理接口结果做减法。
减法（黑名单）会在上游新增字段时静默泄漏；白名单则默认安全（新字段默认不公开）。

---

## 1. 顶层数据契约

公开快照是**独立数据契约**，不复用管理 API 的响应形状：

| 键 | 含义 |
|---|---|
| `generatedAt` | 快照生成时刻（本机时间，北京时间） |
| `dataAsOf` | 数据时点 = 库中最新一条探测记录的 `created_at`，与 `generatedAt` **分开** |
| `schema` | 数据契约版本号（当前 `1`） |
| `readOnly` | 恒为 `true` |
| `mode` | 恒为 `"public"`（与只读快照 `"readonly"` 区分） |
| `windows` | 采用的 overview 窗口列表（见 §4） |
| `data` | 白名单数据体（见 §2） |

**时间口径：** 全栈使用运行机器的本地时间（本机 = 北京时间）。对外时间戳保持与
`hub/db.py` 的本地时间口径一致，不做二次时区换算；非 UTC+8 机器需显式设 `TZ=Asia/Shanghai`。
页面上**不得**把 `git push` 时刻当作"线上已生效时间"。

---

## 2. 字段白名单（逐接口）

### 2.1 `data.endpoints.items[]` — 端点清单

**公开（白名单）：**

`label`、`provider`、`website`、`kind`、`protocol`、`defaultModelDisplay`、
`active`、`availability`（%）、`probeCount`、`lastProbeAt`、`ttftP50`、`tpsAvg`、
`contextMax`、`modelCount`、`quotaSource`。

**明确排除（绝不进入公开快照）：**

- `baseUrl` —— 可能内嵌查询串或私有网段地址；
- `authEnv` —— **环境变量名本身即敏感**（泄漏命名即可推断凭据体系）；
- `hasKey` —— 暴露"本机是否配置了某凭据"；
- `note` —— 运维备注，可能夹带内网信息；
- `dailyBudget` / `budgetToday` / `budgetRemaining` / `budgetState` / `budgetReason`
  / `budgetEnabled` / `quotaUsedPct` —— 本机探测预算与闸门细节，属运维内部量；
- `id` —— 内部主键，公开站用 `label` 标识。

### 2.2 `data.probes.items[]` — 最近探测明细（上限 500 条）

**公开（白名单）：** `created_at`、`endpoint`（标签）、`level`、`model_id`、
`model_display`、`status`、`http_status`、`dns_ms`、`tcp_ms`、`tls_ms`、`ttfb_ms`、
`ttft_ms`、`output_tokens`、`tps`、`total_ms`、`stream_mode`、`served_model`、
`token_source`、`error_kind`（**标准化分类**，非原文）、`response_shape`、
`reasoning_tokens`、`retry_after`、`cost_usd`。

**明确排除：**

- `error_body` / `resp_body` —— 上游响应正文，可能含回显凭据或内部信息；
- `request_json` —— 请求体；
- `resp_headers` —— 响应头（可能含服务端内部头）；
- `egress` —— 出口路径标识（`direct` / `proxy:<节点>`），暴露本机网络拓扑；
- `timing_scope`、`probe_version` —— 探针实现细节。

**错误展示口径：** 只允许 `error_kind` 这类**标准化分类**上屏，禁止发布任何原始响应文本。

### 2.3 `data.rankings.<scene>` — 榜单

复用 `hub/rank.py` 的 `board()`（**单一来源**，与管理端同算法）。白名单：
`scene`、`sceneLabel`、`weights`、`minSamples`、`items[]`、`excludedCount`、
`excludedSummary[]`、`note`。

- `items[]` 保留：`endpoint`、`model`、`modelId`、`contextLength`、`n`、`totalProbes`、
  `score`、`ttftP50`、`tpsP50`、`totalP50`、`totalP95`、`breakdown`、`weightsUsed`、`missing`。
- `excluded` 的**逐条明细**压成 `excludedSummary`（按「接口 × 原因」聚合），
  明细仅用于诊断，公开站不背负（体量实测：明细约占三份榜单总和的 85%）。

### 2.4 `data.overview.<hours>` — 状态总览

复用 `hub.status_engine.build_overview(conn, window_hours=...)`。该结构本就是面向
"现在状态"的展示层，不含绝对路径与凭据明文；保留其 `matrix` / `headline` /
`common_cause` 等字段。

### 2.5 `data.allTime` — 全历史聚合（**与 overview 分开**）

**不得**用 `window_hours=720`（近 30 天）冒充"全部"。全历史是独立的库级聚合：

`totalProbes`、`healthy`、`availability`、`firstAt`、`lastAt`、`endpointCount`、
`modelCount`、`byLevel{}`、`byStatus{}`、`daily[]`（按自然日聚合的序列）。

### 2.6 `data.catalog.items[]` — 门槛台账

公开：`provider`、`website`、`kind`、`signup_method`、`free_form`、`rate_limits`、
`allow_proxy`、`allow_commercial`、`source_url`、`source_type`、`verified_at`、
`confidence`、`requires_phone`、`requires_card`、`requires_realname`、
`region_restricted`、`requires_review`、`probed`、`probed_models`。
排除 `provider_id`（内部主键）。

### 2.7 `data.health` — 健康投影

仅保留：`ok`、`probeRows`、`latestProbeAt`。
**排除 `db`（数据库路径，仓库外时可能是绝对路径）与 `scheduler`（本机调度状态）。**

### 2.8 `data.meta`

`probeSource`（公开来源说明）、`timezone`（`Asia/Shanghai`）、`probesLimit`、
`endpointsCount`、`modelCount`、`policyVersion`。

---

## 3. 明确不请求 / 不导出的接口

| 接口 | 原因 |
|---|---|
| `/api/reminders` | **有副作用**：会触发 `_notify_transitions()`，公共导出绝不能调用 |
| `/api/settings` | 含 `probePrompt`、提醒阈值、预算与通知状态 |
| `/api/schedule` | 本机调度状态 |
| `/api/config` | 网关下发 YAML 预览 |
| 原始 `/api/endpoints`、`/api/health`、`/api/probes` | 字段过宽（见 §2 排除项） |

任何写入类接口（`POST` / `PUT` / `DELETE`）一律不调用。

---

## 4. 时间窗口定义

| 键 | 窗口 |
|---|---|
| `1` | 近 1 小时 |
| `6` | 近 6 小时 |
| `24` | 近 24 小时 |
| `168` | 近 7 天 |
| `720` | 近 30 天 |
| `allTime` | **全库聚合**（独立计算，非 720 小时） |

产品强调时效性：默认窗口应短，长周期仅作备选。

---

## 5. Canary 与敏感字段扫描（发布前置）

`hub/public_snapshot.py` 在产物写盘前执行**双向扫描**，任一命中即判定构建失败：

1. **结构扫描（黑名单键）**：产物 `data` 中不得出现 `authEnv`、`baseUrl`、
   `error_body`、`resp_body`、`request_json`、`resp_headers`、`egress`、`settings`、
   `schedule`、`config`、`reminders`、`db`（health 下）、`dailyBudget`、`hasKey`、
   `probePrompt`。

   > `note` **不进**全局键黑名单：榜单里的 `note` 是"排名的公开方法说明"，属要公开的内容；
   > 端点台账与门槛台账的 `note` 由各自的正向投影排除，并由专门用例守卫。用 `note`
   > 做全局拦截会误伤。
2. **内容扫描（canary 正则）**：对整个 JSON 文本匹配并统计：
   - 凭据环境变量名形态：`\b[A-Z][A-Z0-9_]*_(KEY|TOKEN|SECRET)\b`
     （显式豁免公开状态常量 `NO_KEY` / `HAS_KEY`）；
   - 常见 Key 前缀：`sk-`、`Bearer `、`Authorization`；
   - 本机绝对路径：盘符路径（`C:\...`）、`/Users/`、`/home/`；
   - 回环与内网：`127.0.0.1`、`localhost`、`0.0.0.0`、`10.0.0.0/8`、`192.168.0.0/16`、`172.16.0.0/12`；
   - 未脱敏长串（长度 ≥ 44 的连续 Base64/十六进制）。

扫描实现与断言见 `hub/tests/test_public_snapshot.py`。**发布脚本在扫描未通过时
必须保留上一次成功产物，不得覆盖线上旧站。**

---

## 6. 发布安全（不属数据政策，但同属门禁）

- 导出源使用 **SQLite Online Backup API** 生成一致性副本（WAL 安全），只读打开；
- 构建在**独立 staging 目录**完成，失败不覆盖旧产物；
- 跨进程锁防止重复发布；
- 凭据只具备目标仓库所需最小写权限，Token 不出现在命令行 / 日志 / 源码中；
- 内容无变化时不生成无意义提交；
- `gh-pages` 分支**只含静态文件**；
- `main` 与已有 `docs/` 不被覆盖；
- 本机活动 `web/` 目录**不得**出现 `snapshot.js` / `snapshot.json`。

---

## 7. 变更流程

任何对公开字段的增删，必须：

1. 修改本文件的白名单表；
2. 同步修改 `hub/public_snapshot.py` 的 DTO 与 `hub/tests/test_public_snapshot.py` 的守卫；
3. 运行全量回归（internal 与 public 两仓）；
4. 重新生成 `snapshot.json` 并复核体量。

**白名单变更 = 产品决策**，不是纯技术改动，需明确确认后再合入。
