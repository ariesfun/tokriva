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

公开快照是**独立数据契约**，不复用管理 API 的响应形状。**扁平结构**（不套 `data.*`）：

| 键 | 含义 |
|---|---|
| `schemaVersion` | 数据契约版本号（当前 `1`）。公开结构发生**不兼容**变化时递增 |
| `meta` | 自述块（快照标识 / 时间 / 口径 / 截断说明，见 §2.8） |
| `overview` | 状态总览，键为窗口小时数（`"1"`…`"720"`）**外加 `"allTime"`**（见 §2.4 / §2.5） |
| `rankings` | 榜单，键为场景（`speed` / `agent` / `stable`，见 §2.3） |
| `endpoints` | 被监控的接口清单（见 §2.1） |
| `probes` | 最近探测明细（上限 500 条，见 §2.2） |
| `catalog` | 服务商门槛台账（见 §2.6） |

**为什么是扁平的：** 公开站走**独立前端**（`web/public/`），不与管理端 `app.js` 共用取数
路径，所以「嵌套成 `window.SNAPSHOT` 的形状以便复用」这个前提不成立；`readOnly` / `mode`
是**管理端只读快照**（`hub/static_export.py`）的概念，公开契约天然只读，不该带。

**两项与《Tokriva 公开静态展示站：架构设计与代码审查报告》的有意偏离：**

1. 报告建议顶层键叫 `providers`。但那按「一个 provider 一个 base_url」设想；本库的
   `provider` 表是**厂商元数据**（13 家，其中 8 家只有 catalog 条目、无被探测端点），
   真正被探测的对象是 `endpoint`（5 个）。改名会与按厂商聚合的 `catalog` 重叠、
   且与库内语义相悖 → **保留 `endpoints`**。报告 `PublicProvider` DTO 里真正需要公开的
   字段已并入各项：`providerId`（公开 slug）、`publicStatus`（由 `active` 派生）。
2. 报告把 `allTime` 单列为顶层键，并单列 `generatedAt` / `dataAsOf`。本政策把
   `allTime` **并入 `overview`**（它与各窗口同属"总览"维度），把时间戳放进 `meta`。

**时间口径：** 全栈使用运行机器的本地时间（本机 = 北京时间）。对外时间戳保持与
`hub/db.py` 的本地时间口径一致，不做二次时区换算；非 UTC+8 机器需显式设 `TZ=Asia/Shanghai`。
页面上**不得**把 `git push` 时刻当作"线上已生效时间"。

---

## 2. 字段白名单（逐接口）

### 2.0 命名约定（先读这条）

公开契约有两类字段，**命名风格刻意不同**：

| 类别 | 风格 | 理由 |
|---|---|---|
| **原始行投影**（`probes.items[]`） | **snake_case**，与库列同名 | 让「库列 / CSV 兜底快照 `data.js` / 前端 `normalizeRows`」三者同名，少一层适配 |
| **派生 / 聚合字段**（`endpoints`、`rankings`、`meta` 等） | **camelCase** | 这些不是原始行，是构建期算出来的展示字段 |

因此探针行是 `model_id` / `http_status` / `error_kind` / `ttft_ms`，而端点清单是
`defaultModel` / `probeCount` / `lastProbeAt` / `ttftP50`。**新增字段时照此归类，
不要在两处混用同一种风格。**

检查手法：`hub/tests/test_public_snapshot.py` 的用例名即归类说明
（`test_probe_projection_*` 走 snake_case，`test_endpoint_projection_*` 走 camelCase）。

### 2.1 `endpoints.items[]` — 端点清单

**公开（白名单）：**

`providerId`（公开 slug，由 `label` 派生，**不是**库内主键）、`label`、`provider`、
`website`、`kind`、`protocol`、`defaultModel`、`active`、`publicStatus`（由 `active`
派生 `ACTIVE` / `PAUSED`）、`availability`（%）、`probeCount`、`lastProbeAt`、
`ttftP50`、`tpsAvg`、`contextMax`、`modelCount`、`quotaSource`。

**明确排除（绝不进入公开快照）：**

- `baseUrl` —— 可能内嵌查询串或私有网段地址；
- `authEnv` —— **环境变量名本身即敏感**（泄漏命名即可推断凭据体系）；
- `hasKey` —— 暴露"本机是否配置了某凭据"；
- `note` —— 运维备注，可能夹带内网信息；
- `dailyBudget` / `budgetToday` / `budgetRemaining` / `budgetState` / `budgetReason`
  / `budgetEnabled` / `quotaUsedPct` —— 本机探测预算与闸门细节，属运维内部量；
- `id` —— 内部主键，公开站用 `label` / `providerId` 标识；
- `provider_status` / `status` —— 实测取值恒为 `UNKNOWN`，**零信息量**，又不属任何公开
  语义；正向白名单下「没登记」即「不公开」。真实可用性由 `availability`（本机实测
  百分比）与总览给出。

> `providerId` 由**名字**派生（slug），不用库内主键：主键是内部信息，且重建库时会变。
> slug 只要名字不变就稳定——名字变了本就该变。

### 2.2 `probes.items[]` — 最近探测明细（上限 500 条）

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

### 2.3 `rankings.<scene>` — 榜单

复用 `hub/rank.py` 的 `board()`（**单一来源**，与管理端同算法）。白名单：
`scene`、`sceneLabel`、`weights`、`minSamples`、`items[]`、`excludedSummary[]`、`note`。

- `items[]` 保留：`endpoint`、`model`、`modelId`、`contextLength`、`n`、`totalProbes`、
  `score`、`ttftP50`、`tpsP50`、`totalP50`、`totalP95`、`breakdown`、`weightsUsed`、`missing`。
- `excluded` 的**逐条明细**压成 `excludedSummary`（按「接口 × 原因」聚合），
  明细仅用于诊断，公开站不背负（体量实测：明细约占三份榜单总和的 85%）。
- 场景集合由 `public_snapshot.SCENES` 与 `rank.SCENES` 驱动，**前端不另立枚举**。

### 2.4 `overview.<hours>` — 状态总览

复用 `hub.status_engine.build_overview(conn, window_hours=...)`。该结构本就是面向
"现在状态"的展示层，不含绝对路径与凭据明文；保留其 `matrix` / `headline` /
`common_cause` 等字段，但**做两处裁剪**：

1. 剥掉 `matrix[].hasKey` —— 暴露"本机是否为某接口配置了凭据"，属运维内部信息
   （页面上仍能从 `final=NO_KEY` 得到"未接入"结论）；
2. 剥掉所有**「现在」派生的易变字段**（`generated_at` / `age_sec`，**递归**）——理由见 §2.9。

公开页要"最后探测多久以前"，用 `meta.dataAsOf` 与 `endpoints.items[].lastProbeAt`
在前端自行推算，不必由后端固化一个会过期的秒数。

### 2.5 `overview.allTime` — 全历史聚合（**与各窗口分开算**）

**不得**用 `window_hours=720`（近 30 天）冒充"全部"。全历史是独立的库级聚合：

`totalProbes`、`healthy`、`availability`、`firstAt`、`lastAt`、`endpointCount`、
`modelCount`、`byLevel{}`、`byStatus{}`、`daily[]`（按自然日聚合的序列）。

放在 `overview` 之下（键名 `allTime`）而非顶层：它与 `"1"`…`"720"` 是同一种东西的
不同取值，分家会让人误以为是并列的另一个集合。

### 2.6 `catalog.items[]` — 门槛台账

公开：`provider`、`website`、`kind`、`signup_method`、`free_form`、`rate_limits`、
`allow_proxy`、`allow_commercial`、`source_url`、`source_type`、`verified_at`、
`confidence`、`requires_phone`、`requires_card`、`requires_realname`、
`region_restricted`、`requires_review`、`probed`、`probed_models`。
排除 `provider_id`（内部主键）。

### 2.7 原始 `health` **整体不导出**

报告 §5.3.7 把原始 `health` 列入"明确禁止导出集合"，本政策照办——**公开快照不含
`health` 键**。原先想保留的 `{ok, probeRows, latestProbeAt}` 三个字段已被
`meta.dataAsOf`（数据时点）与 `probes.total` / `overview.allTime.totalProbes`
（条数）覆盖，再单列一个集合纯属重复。数据库路径与调度状态自然不再有机会出场。

### 2.8 `meta` — 自述块

| 字段 | 含义 |
|---|---|
| `snapshotId` | `YYYYMMDDTHHMMSS+0800-<12位指纹>`。**变更令牌**，见 §2.9 |
| `generatedAt` | 快照**生成**时刻（本机时间）；仅用于诊断"这是哪一次运行" |
| `dataAsOf` | **数据时点** = 库中最新一条探测记录的 `created_at`。刻意不取生成时刻 |
| `timezone` | 恒为 `Asia/Shanghai` |
| `measurementScope` | 恒为 `single-origin-network`：数据来自**一条固定出口**，不代表全球网络 |
| `freshness` | 由 `dataAsOf` 与当下推算：`fresh` / `stale` / `expired` / `unknown` |
| `samplePolicy` | `{recentProbeLimit, historyIsTruncated}`：明确探测列表是**截断后的样本** |
| `policyVersion` | 本政策版本（当前 `1`），与 `schemaVersion` 分开演进 |
| `probeSource` | 公开来源说明（"tokriva 本机探测（出口固定，历史可比）"） |
| `windows` | overview 采用的小时窗口列表（见 §4） |
| `endpointsCount` | 公开端点清单条数（与 `endpoints.items` 等长） |
| `modelCount` | 活跃对话模型数 |

**新鲜度阈值：** `age = now - dataAsOf`；`age ≤ 90min` → `fresh`；`≤ 180min` →
`stale`；否则 `expired`。时间戳缺失/不可解析 → `unknown`（**不猜**）；未来时间戳
（`age < 0`，可能因时钟回拨）→ `fresh`。

### 2.9 幂等与快照指纹（"无变化不提交"的依据）

报告 §7.3 第 8 条要求构建**幂等**：输入库内容不变时，公开数据内容应保持一致。
本实现给出可执行的定义：

- **数据体**（`overview` / `rankings` / `endpoints` / `probes` / `catalog`）在库不变时
  **逐字节一致**——因此所有"现在"派生字段必须剥掉（§2.4 第 2 条）。
- **`snapshotId` = 数据时点戳 + 本机时区偏移 + 数据体 sha256 前 12 位**。
  它只覆盖**数据体**，不含 `generatedAt` / `freshness` 这类每次运行都变的元信息。
- **发布器必须以 `snapshotId` 为变更令牌**（不是文件字节、更不是 `generatedAt`）：
  相同 → 跳过提交；不同 → 提交。这样"无变化不提交"才真正成立。
- `meta.generatedAt` 与 `meta.freshness` 仍会随运行变化，但它们**不参与指纹**，
  也**不构成"数据变了"**。

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
