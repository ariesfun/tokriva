/* ============================================================
   Tokriva Hub · 前端展示逻辑
   数据：window.PROBE_ROWS（源自 data/probe_results.csv 的只读镜像）
   接口元数据：源自 hub/run.py 的 ENDPOINTS（Key 一律不出现在前端）
   ============================================================ */
(function () {
  'use strict';

  // API_BASE：留空表示与页面同源（生产）；本地开发时后端在 8766 端口。
  const API_BASE = window.TOKRIVA_API_BASE || '';

  // ---------- 只读快照模式（纯静态 / GitHub Pages 部署） ----------
  // 页面同目录存在 snapshot.js（由 hub/static_export.py 生成）时启用：
  // 只读数据一律从 window.SNAPSHOT 取，不需要服务端、不需要数据库；
  // 写操作（触发探测 / 改配置 / 下发网关…）在静态站点上无意义，一律禁用并隐藏入口。
  const SNAPSHOT = (window.SNAPSHOT && window.SNAPSHOT.data) ? window.SNAPSHOT : null;
  const READ_ONLY = !!SNAPSHOT;
  const READ_ONLY_MSG = '只读快照模式：该操作需要服务端，静态页面不可用';

  /** 把 `apiGet` 的路径映射到快照里的对应数据；无对应项返回 undefined。 */
  function snapshotFor(path) {
    if (!SNAPSHOT) return undefined;
    const parts = String(path).split('?');
    const key = parts[0].replace(/^\/api\//, '');
    const q = new URLSearchParams(parts[1] || '');
    const d = SNAPSHOT.data;
    switch (key) {
      case 'health': return d.health;
      case 'endpoints': return d.endpoints;
      case 'probes': return d.probes;
      case 'reminders': return d.reminders;
      case 'catalog': return d.catalog;
      case 'settings': return d.settings;
      case 'schedule': return d.schedule;
      case 'config': return d.config;
      case 'rankings': return (d.rankings || {})[q.get('scene') || 'speed'];
      case 'overview': {
        const ov = d.overview || {};
        return ov[q.get('window_hours')] || ov[Object.keys(ov)[0]];
      }
      default: return undefined;
    }
  }

  let DATA_SOURCE = 'snapshot';
  let ROWS = normalizeRows(window.PROBE_ROWS || []);
  function normalizeRows(list) {
    return list.map(r => ({
      ...r,
      dns_ms: num(r.dns_ms), tcp_ms: num(r.tcp_ms), tls_ms: num(r.tls_ms),
      ttfb_ms: num(r.ttfb_ms), ttft_ms: num(r.ttft_ms), tps: num(r.tps),
      total_ms: num(r.total_ms), output_tokens: num(r.output_tokens),
      http_status: num(r.http_status)
    }));
  }
  function num(v) { return (v === '' || v == null) ? null : Number(v); }

  // 接口元数据（来自 hub/run.py；不含任何 Key 明文）
  const ENDPOINT_THEME = {
    'LightVela': { color: '#5e6ad2', short: 'LV' },
    'AMD-Radeon': { color: '#1a9c5b', short: 'AMD' },
    'Packet': { color: '#c9821a', short: 'PK' }
  };
  let ENDPOINTS = {
    'LightVela': {
      provider: 'LightVela', kind: 'model_api', base: 'token.lightvela.ai/v1',
      protocol: 'openai_chat', model: 'auto', proxy: false,
      budget: 200, quota: 'MANUAL_REQUIRED', quotaSnap: null, tools: 'UNKNOWN',
      note: 'auto 路由到推理模型，实测需 256 token 才出正文；出现 44.7s 离群值',
      ...ENDPOINT_THEME['LightVela']
    },
    'AMD-Radeon': {
      provider: 'AMD Radeon (官方直连)', kind: 'serverless', base: 'developer.amd.com.cn/radeon/api/v1',
      protocol: 'openai_chat', model: 'Qwen3.8-27B', proxy: false,
      budget: 200, quota: 'API', tools: 'SUPPORTED',
      note: 'OpenAI 兼容；有 /usage 与 /models/utilization（额度 A 档可查）',
      ...ENDPOINT_THEME['AMD-Radeon']
    },
    'Packet': {
      provider: 'Packet.ai', kind: 'model_api', base: 'api.packet.ai/v1',
      protocol: 'openai_chat', model: 'nvidia-deepseek-v4-1-flash-nvfp4', proxy: true,
      budget: 200, quota: 'MANUAL_REQUIRED', quotaSnap: null, tools: 'UNKNOWN',
      note: '直连会被 Cloudflare 拦截，需走代理；三个接口中响应最稳定',
      ...ENDPOINT_THEME['Packet']
    }
  };

  const STATUS_MAP = {
    HEALTHY: { cls: 'ok', label: '健康' },
    DEGRADED: { cls: 'warn', label: '降级' },
    AUTH_FAILED: { cls: 'danger', label: '鉴权失败' },
    QUOTA_EXHAUSTED: { cls: 'warn', label: '额度耗尽' },
    RATE_LIMITED: { cls: 'warn', label: '限流' },
    MODEL_GONE: { cls: 'danger', label: '模型下线' },
    ENDPOINT_ERROR: { cls: 'danger', label: '服务端错误' },
    NOT_CONFIGURED: { cls: 'neutral', label: '缺少密钥' },
    NETWORK_ERROR: { cls: 'danger', label: '网络失败' },
    RESPONSE_INVALID: { cls: 'danger', label: '响应异常' },
    REASONING_STARVED: { cls: 'warn', label: '推理占满' }
  };
  function statusMeta(s) { return STATUS_MAP[s] || { cls: 'neutral', label: s || '未知' }; }
  function badge(status) {
    const m = statusMeta(status);
    return `<span class="badge ${m.cls}" title="${status || 'UNKNOWN'}"><span class="dot"></span>${m.label}</span>`;
  }

  // ---------- 派生指标 ----------
  let L2 = [], L0 = [], EP_STATS = {}, OVERVIEW = null, PROBE_PROMPT = 'Count from 1 to 20.';

  // 时间范围选择器：放在受它控制的模块里（总览页/测试记录页），不再放顶栏
  const RANGE_OPTIONS = [['1', '近 1 小时'], ['6', '近 6 小时'], ['24', '近 24 小时'], ['168', '近 7 天'], ['0', '全部']];
  function rangeSelectHtml(id, title) {
    return `<select id="${id}" title="${title}" style="height:30px;padding:0 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);font-size:12px;color:var(--ink)">
      ${RANGE_OPTIONS.map(([v, label]) => `<option value="${v}">${label}</option>`).join('')}
    </select>`;
  }
  function bindRangeSelect(id, onChange) {
    const sel = $(`#${id}`);
    if (!sel) return;
    sel.value = String(TIME_RANGE);
    sel.addEventListener('change', () => {
      TIME_RANGE = parseInt(sel.value, 10) || 0;
      localStorage.setItem('tokriva-range', sel.value);
      onChange();
    });
  }
  function fmtWindow(hours) {
    return hours >= 24 && hours % 24 === 0 ? `近 ${hours / 24} 天` : `近 ${hours} 小时`;
  }
  function recomputeDerived() {
    L2 = ROWS.filter(r => r.level === 'L2');
    L0 = ROWS.filter(r => r.level === 'L0');
    EP_STATS = {};
    Object.keys(ENDPOINTS).forEach(name => {
      const all = ROWS.filter(r => r.endpoint === name);
      const l2 = all.filter(r => r.level === 'L2');
      const s = statsOf(l2);
      // 可用率只统计"首次真正能用"之后的记录：未接入期间的失败（没配密钥时
      // 网络/鉴权本来就不通）没有统计含义，混进来会把接入后的 100% 稀释成 19%
      const chrono = all.slice().sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
      const firstOk = chrono.find(r => r.status === 'HEALTHY');
      const firstSuccessAt = firstOk ? firstOk.created_at : null;
      const effRows = firstSuccessAt ? all.filter(r => String(r.created_at) >= firstSuccessAt) : [];
      const counted = effRows.filter(r => r.status !== 'NOT_CONFIGURED');
      const healthy = counted.filter(r => r.status === 'HEALTHY').length;
      const availDenom = counted.length;
      // 按时间取最新一条，不能依赖数组顺序（API 为 id DESC，快照为 id ASC）
      const latest = all.slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
      const tsL2 = l2.filter(r => r.stream_mode === 'true_stream' && r.ttft_ms != null);
      // 当前返回方式：以最近一次速度测试为准（探测始终请求流式，整包是服务端行为）；
      // 注意 ROWS 可能是倒序，必须按 created_at 排序取最新
      // 取最近一次「有返回方式」的记录判定口径（失败记录没有该字段，不能因此抹掉历史指标）
      const l2Latest = l2.filter(r => r.stream_mode)
        .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
      const curMode = l2Latest ? l2Latest.stream_mode : null;
      const curRows = curMode ? l2.filter(r => r.stream_mode === curMode) : [];
      const totals = curRows.map(r => r.total_ms).filter(v => v != null);
      const curTps = curRows.map(r => r.tps).filter(v => v != null);
      EP_STATS[name] = {
        ...s, total: all.length,
        availability: firstSuccessAt ? pct(healthy, availDenom) : null,
        firstSuccessAt,
        lastAt: latest ? latest.created_at : '—',
        lastStatus: latest ? latest.status : 'UNKNOWN',
        currentMode: curMode,
        trueStream: tsL2.length,
        // 仅流式的 P50/TPS，避免整包的 TTFT≈总耗时污染横向比较
        ttftP50True: tsL2.length ? median(tsL2.map(r => r.ttft_ms)) : null,
        tpsP50True: (() => { const t = tsL2.map(r => r.tps).filter(v => v != null); return t.length ? median(t) : null; })(),
        buffered: l2.filter(r => r.stream_mode === 'buffered').length,
        // 展示口径（U-01 修正版）：只要存在真流式样本，就用真流式的稳定中位数——
        // 最近一轮偶发的整包/单次响应（网络抖动导致分片合并）不会把整个展示翻转成 n/a；
        // 完全没有真流式样本时才退回整包口径（首字≈总耗时）
        dispMode: tsL2.length ? 'true_stream' : (curMode || null),
        dispTTFT: tsL2.length ? median(tsL2.map(r => r.ttft_ms))
          : (curMode && curMode !== 'true_stream' && totals.length ? median(totals) : null),
        dispTps: tsL2.length ? median(tsL2.map(r => r.tps).filter(v => v != null)) : null,
        dispTotal: !tsL2.length && curMode && curMode !== 'true_stream' && totals.length ? median(totals) : null,
        // 按测试问题分组统计输出速度（自定义问题 vs 默认问题分开展示）
        tpsP50Default: (() => { const t = l2.filter(r => (r.prompt_tag || 'default') === 'default' && r.tps != null).map(r => r.tps); return t.length ? median(t) : null; })(),
        tpsP50Custom: (() => { const t = l2.filter(r => r.prompt_tag === 'custom' && r.tps != null).map(r => r.tps); return t.length ? median(t) : null; })(),
        nDefault: l2.filter(r => (r.prompt_tag || 'default') === 'default').length,
        nCustom: l2.filter(r => r.prompt_tag === 'custom').length
      };
    });
  }
  function statsOf(list) {
    // TTFT 优先取真流式：buffered 接口的 TTFT≈总耗时，混入会严重高估
    const tsList = list.filter(r => r.stream_mode === 'true_stream' && r.ttft_ms != null);
    const ttfts = (tsList.length ? tsList : list).map(r => r.ttft_ms).filter(v => v != null);
    const tpss = list.map(r => r.tps).filter(v => v != null);
    const avg = a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
    return {
      n: list.length,
      ttftAvg: avg(ttfts), ttftMedian: ttfts.length ? median(ttfts) : null,
      tpsAvg: avg(tpss), strengths: [...new Set(list.map(r => r.stream_mode).filter(Boolean))]
    };
  }
  // 最近 N 次心跳条：颜色=状态，悬停=时间/状态/耗时，点击=跳到该接口的记录
  function heartbeatHTML(name, n = 40) {
    const recs = ROWS.filter(r => r.endpoint === name)
      .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
      .slice(0, n);
    if (!recs.length) return '<span class="mono" style="font-size:10px;color:var(--ink-4)">—</span>';
    const bars = recs.map(r => {
      const cls = r.status === 'HEALTHY' ? 'ok' : (r.status === 'RATE_LIMITED' || r.status === 'DEGRADED') ? 'warn'
        : r.status === 'NOT_CONFIGURED' ? 'neutral' : 'danger';
      const tip = `${fmtTs(r.created_at, true)} · ${statusMeta(r.status).label}${r.ttft_ms != null ? ' · ' + fmtMs(r.ttft_ms) : ''}`;
      return `<span class="hb-cell ${cls}" title="${esc(tip)}"></span>`;
    }).join('');
    return `<span class="hb-strip" data-hb="${esc(name)}">${bars}</span>`;
  }
  function median(a) { const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }
  function pct(n, d) { return d ? Math.round(n / d * 100) : 0; }
  // 测试级别 -> 用户可读名称（L0 只查连通，L2 是流式测速）
  const LEVEL_LABEL = { L0: '连通检测', L1: '轻量测试', L2: '速度测试' };
  // 内部枚举 -> 用户可读中文（用户产品不直接暴露英文代号）
  const PROTOCOL_LABEL = { openai_chat: 'OpenAI 对话格式', openai_responses: 'OpenAI Responses 格式', openai_models: '模型列表格式', anthropic: 'Anthropic 对话格式' };
  const KIND_LABEL = { model_api: '模型服务', serverless: 'GPU 云服务' };
  const STREAM_LABEL = { true_stream: '流式', buffered: '整包', single: '单次', none: '无内容' };
  const QUOTA_LABEL = { API: '接口可查', HEADER: '响应头返回', MANUAL: '需手动查看', MANUAL_REQUIRED: '需手动查看', CONSOLE_ONLY: '需到官网查看', PUBLIC: '官网公开', NONE: '无', UNKNOWN: '未核实' };
  const QUOTA_TYPE_LABEL = { DAILY: '每日额度', WEEKLY: '每周额度', MONTHLY: '每月额度', TOTAL: '总额度', SUBSCRIPTION: '订阅额度', RATE_LIMIT: '速率限制', UNKNOWN: '未知' };
  const UNIT_LABEL = { USD: '美元', REQUEST: '次', TOKEN: '字', UNKNOWN: '' };
  const TOOLS_LABEL = { SUPPORTED: '支持', UNSUPPORTED: '不支持', UNKNOWN: '未核实' };
  const SOURCE_TYPE_LABEL = { official: '官方页面核实', manual: '人工录入', dataset: '数据集导入' };
  const ALLOW_LABEL = { YES: '允许', TRUE: '允许', ALLOWED: '允许', NO: '不允许', FALSE: '不允许', DENIED: '不允许', UNKNOWN: '未核实' };
  const SIGNUP_LABEL = { EMAIL: '邮箱注册', PHONE: '手机号注册', CARD: '需绑定银行卡', OAUTH: '第三方账号登录', UNKNOWN: '未核实' };
  const FREE_LABEL = { STANDING: '长期免费额度', TRIAL: '限时试用额度', CREDIT: '赠送额度', UNKNOWN: '未核实' };
  const CONFIDENCE_LABEL = { CONFIRMED: '已确认', LIKELY: '较可能', POSSIBLE: '待确认', LOW: '低', MEDIUM: '中', HIGH: '高', UNKNOWN: '未核实' };
  const CTX_SOURCE_LABEL = { declared_api: '接口声明', API: '接口声明', UNKNOWN: '未知', MANUAL: '人工填写' };
  const WEIGHT_LABEL = { availability: '可用率', ttft: '首字延迟', latency: '首字延迟', tps: '输出速度', stability: '稳定性', quota: '剩余额度' };
  // 通用：查表翻译，查不到就原样显示
  function labelOf(map, v, fallback) {
    if (v == null || v === '') return fallback || '—';
    const s = String(v);
    return map[s] || map[s.toUpperCase()] || map[s.toLowerCase()] || fallback || s;
  }
  // 把后端返回原因里的状态代号也翻译掉，如“当前状态 NETWORK_ERROR”
  function humanize(text) {
    if (!text) return '';
    return String(text).replace(/\b(HEALTHY|DEGRADED|AUTH_FAILED|QUOTA_EXHAUSTED|RATE_LIMITED|MODEL_GONE|ENDPOINT_ERROR|NETWORK_ERROR|RESPONSE_INVALID|REASONING_STARVED)\b/g,
      m => statusMeta(m).label);
  }
  function levelCell(lv) {
    const label = LEVEL_LABEL[lv] || lv || '—';
    return `<span class="tag-mono" title="${esc(lv || '')}">${esc(label)}</span>`;
  }
  // 探测预算：把「设了限额」变成「看得见还剩多少」。
  // 后端算了预算却不在界面回显，用户只会以为限额没生效。
  const BUDGET_STATE = {
    ok: { cls: 'ok', label: '正常' },
    guarded: { cls: 'warn', label: '接近上限' },
    exhausted: { cls: 'danger', label: '今日已用尽' },
  };
  function budgetRows(e) {
    const head = `<div class="kv"><span class="k">每天最多测</span><span class="v">${e.budget} 次</span></div>`;
    if (e.budgetState == null || e.budgetToday == null) return head;
    if (!e.budgetOn || e.budgetState === 'off') {
      return head + `<div class="kv"><span class="k">今日预算</span>`
        + `<span class="v" style="color:var(--ink-3)">未启用，速度测试不限次数</span></div>`;
    }
    const meta = BUDGET_STATE[e.budgetState];
    const tag = meta
      ? `<span class="badge ${meta.cls}" style="margin-left:6px"${e.budgetReason ? ` title="${esc(e.budgetReason)}"` : ''}>${esc(meta.label)}</span>`
      : '';
    const used = `今日已用 ${e.budgetToday} 次`;
    const left = e.budgetRemaining > 0
      ? `，还能测 ${e.budgetRemaining} 次`
      : '，本轮只做连通检查';
    // 账号额度（第二道闸门）：拿到具体余额才展示；未知则不占位。
    // 有的免费层按金额/token 限额，光看「还剩几次」看不出真实压力。
    const q = (e.quotaUsedPct == null) ? ''
      : `<div class="kv"><span class="k">账号额度</span><span class="v">`
        + (e.quotaUsedPct >= 100 ? '已用尽' : `已用 ${Math.round(e.quotaUsedPct)}%`)
        + `</span></div>`;
    return head + `<div class="kv"><span class="k">今日预算</span>`
      + `<span class="v">${esc(used + left)}${tag}</span></div>` + q;
  }
  // 自定义测试问题的记录打标（速度单独统计，不和默认问题混在一起）
  function promptBadge(r) {
    if ((r.prompt_tag || 'default') !== 'custom') return '';
    return `<span class="tag-mono" style="color:var(--accent)" title="使用自定义测试问题测得">自定义</span>`;
  }
  // 模型单元格：展示名为主，原始 ID 作小字；已下线(MISSING)标灰并注明，避免历史记录看起来像当前模型
  function modelCell(r) {
    if (!r.model_id) return '<span class="mono" style="font-size:10px;color:var(--ink-4)">—</span>';
    const name = r.model_display || r.model_id;
    const stale = r.model_status === 'MISSING';
    const main = `<span class="mono" title="${esc(name)}" style="font-size:10px;display:inline-block;max-width:150px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;vertical-align:bottom${stale ? ';color:var(--ink-4)' : ''}">${esc(name)}</span>`;
    const sub = name !== r.model_id
      ? `<span class="cell-sub mono" style="font-size:9px">${esc(r.model_id)}</span>` : '';
    const tag = stale
      ? ' <span class="badge neutral" style="height:15px;font-size:9px;padding:0 4px" title="该模型已不在当前模型目录中，属历史记录">已下线</span>' : '';
    return main + tag + sub;
  }

  recomputeDerived();

  // ---------- API 数据加载 ----------
  let apiAvailable = false;
  // 后端统一排名（hub/rank.py）。前端不再自行打分，避免与下发口径不一致。
  let SERVER_RANK = {};
  let REMINDERS = [], CATALOG = [];
  // 提醒状态机的附带信息：各态计数、待处理数、本轮状态翻转、当前阈值
  let REMINDER_META = { summary: { pending: 0, alerting: 0, recovering: 0 },
                        actionable: 0, muted: 0, transitions: [],
                        tuning: { confirmN: 2, recoverM: 2 } };
  let REMINDER_FILTER = 'all';
  async function apiGet(path) {
    // 静态模式：全部只读数据来自本地快照，不发任何网络请求
    if (READ_ONLY) {
      const got = snapshotFor(path);
      if (got === undefined) throw new Error('快照中没有 ' + path + ' 的数据');
      return got;
    }
    const res = await fetch(API_BASE + path, { headers: { 'Accept': 'application/json' } });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }
  async function apiPost(path, body) {
    // 静态模式下没有服务端可写：明确拒绝，避免静默失败让用户以为已生效
    if (READ_ONLY) {
      const err = new Error(READ_ONLY_MSG);
      err.readOnly = true;
      throw err;
    }
    const headers = { 'Accept': 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const res = await fetch(API_BASE + path, { method: 'POST', headers, body: body !== undefined ? JSON.stringify(body) : undefined });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { msg = (await res.json()).detail || msg; } catch (_) {}
      // 把状态码带到异常上，调用方才能区分「稍等」类语义（如 409 探测已在进行中），
      // 而不是只能对着文案字符串做判断。
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }
  // 提醒数据落地：列表 + 状态机元信息 + 侧栏角标，一处收口，
  // 避免「列表刷新了但角标还是旧数」这类不一致
  function applyReminderPayload(d) {
    REMINDERS = (d && d.items) || [];
    REMINDER_META = {
      summary: (d && d.summary) || { pending: 0, alerting: 0, recovering: 0 },
      actionable: (d && d.actionable) || 0,
      muted: (d && d.muted) || 0,
      transitions: (d && d.transitions) || [],
      tuning: (d && d.tuning) || { confirmN: 2, recoverM: 2 },
      // 通知渠道状态：配了没有、这一轮发出去了什么
      notify: (d && d.notify) || { enabled: false, sent: false, count: 0, at: null }
    };
    const rc = $('#navCountReminders'); if (rc) rc.textContent = REMINDER_META.actionable;
    const nb = $('#notifyBtn'); if (nb) nb.classList.toggle('has-dot', REMINDER_META.actionable > 0);
  }
  async function loadFromApi(silent) {
    try {
      const [probes, eps, health, rankSpeed, rankAgent, rankStable, reminders, catalog, overview, settings] = await Promise.all([
        apiGet('/api/probes?limit=2000'),
        apiGet('/api/endpoints'),
        apiGet('/api/health'),
        apiGet('/api/rankings?scene=speed').catch(() => null),
        apiGet('/api/rankings?scene=agent').catch(() => null),
        apiGet('/api/rankings?scene=stable').catch(() => null),
        apiGet('/api/reminders').catch(() => ({ items: [] })),
        apiGet('/api/catalog').catch(() => ({ items: [] })),
        apiGet('/api/overview?window_hours=' + (TIME_RANGE ? TIME_RANGE : 720)).catch(() => null),
        apiGet('/api/settings').catch(() => null)
      ]);
      if (settings && typeof settings.probePrompt === 'string' && settings.probePrompt.trim()) {
        PROBE_PROMPT = settings.probePrompt.trim();
      } else if (settings) {
        PROBE_PROMPT = 'Count from 1 to 20.';
      }
      OVERVIEW = overview && Array.isArray(overview.matrix) ? overview : null;
      // 标签页标题带异常数（C5）：故障/待确认的接口数
      if (OVERVIEW) {
        const bad = (OVERVIEW.headline.counts.down || 0) + (OVERVIEW.headline.counts.pending || 0);
        document.title = bad > 0 ? `（${bad}）Tokriva · AI 接口监控台` : 'Tokriva · AI 接口监控台';
      }
      applyReminderPayload(reminders);
      CATALOG = catalog.items || [];
      SERVER_RANK = {};
      [['speed', rankSpeed], ['agent', rankAgent], ['stable', rankStable]].forEach(([k, v]) => {
        if (v && Array.isArray(v.items)) SERVER_RANK[k] = v;
      });
      if (!Array.isArray(probes.items) || !Array.isArray(eps.items)) throw new Error('bad payload');
      ROWS = normalizeRows(probes.items.map(r => ({ ...r, endpoint: r.endpoint })));
      // 用后端清单覆盖元数据，同时保留前端的主题色/缩写
      const merged = {};
      eps.items.forEach(e => {
        merged[e.label] = {
          id: e.id, provider: e.provider || e.label, kind: e.kind || 'model_api',
          base: (e.baseUrl || '').replace(/^https?:\/\//, ''),
          baseUrl: e.baseUrl || '',
          protocol: e.protocol || 'openai_chat',
          model: e.defaultModelDisplay || e.defaultModel || '—',
          modelId: e.defaultModel || null,
          proxy: !!e.useProxy, budget: e.dailyBudget || 200,
          // 探测预算的当日状态：关掉开关时 state='off'，界面不显示额度压力
          budgetToday: e.budgetToday == null ? null : e.budgetToday,
          budgetRemaining: e.budgetRemaining == null ? null : e.budgetRemaining,
          budgetState: e.budgetState || null,
          budgetReason: e.budgetReason || '',
          quotaUsedPct: e.quotaUsedPct == null ? null : e.quotaUsedPct,
          budgetOn: e.budgetEnabled !== false,
          quota: e.quotaSource || 'MANUAL_REQUIRED', quotaSnap: e.quota || null, tools: 'UNKNOWN',
          authEnv: e.authEnv || null, hasKey: e.hasKey === undefined ? null : !!e.hasKey, active: e.active !== false,
          ctxMax: e.contextMax || null, modelCount: e.modelCount || 0,
          note: e.note || '（无备注）',
          ...(ENDPOINT_THEME[e.label] || { color: '#5e6ad2', short: (e.label || '?').slice(0, 3) })
        };
      });
      ENDPOINTS = merged;
      recomputeDerived();
      apiAvailable = true;
      DATA_SOURCE = READ_ONLY ? 'static' : 'api';
      const stamp = health.latestProbeAt || health.probeRows;
      $('#syncTime').textContent = health.latestProbeAt ? fmtTs(health.latestProbeAt, true) : (health.probeRows + ' 条');
      const footer = document.querySelector('.sync-top span:last-child');
      if (footer) footer.textContent = READ_ONLY
        ? '只读快照 · ' + (SNAPSHOT.generatedAt || '').replace('T', ' ').slice(0, 16)
        : '已连接服务端';
      $('#navCountProbes').textContent = probes.total;
      $('#navCountEndpoints').textContent = eps.items.length;
      // 侧栏角标只数「已确认的故障」：待确认还在观察，恢复中已在好转，都不催用户
      // （由 applyReminderPayload 统一更新）
      renderCurrent(silent);
      if (!silent) toast(READ_ONLY
        ? '已载入快照，共 ' + probes.total + ' 条测试数据'
        : '已刷新，共 ' + probes.total + ' 条实时测试数据', 'i-refresh');
      return true;
    } catch (err) {
      apiAvailable = false;
      DATA_SOURCE = 'snapshot';
      console.warn('[tokriva] 后端加载失败，已回退本地快照：', err);
      if (!silent) toast('暂时连不上服务端，先显示本地示例数据', 'i-info');
      return false;
    }
  }
  function renderCurrent(silent) {
    RENDERERS[currentView]();
  }

  // ---------- DOM 帮助 ----------
  const $ = s => document.querySelector(s);
  const content = $('#content');
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  // 库中时间戳统一存**本机本地时间**（后端 datetime('now','localtime')），
  // 显示按本地时间原样格式化即可，不再做任何时区换算。
  function fmtTs(ts, withDate) {
    if (!ts) return '—';
    const raw = String(ts).trim();
    // 纯日期（YYYY-MM-DD）表示「哪一天」而非「哪一刻」，原样显示。
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw;
    // 带时区的值（末尾 Z 或 ±HH:MM）交给 Date 换算到本地；朴素串当作本地直接解析。
    const d = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(raw) ? new Date(raw) : new Date(raw.replace(' ', 'T'));
    if (isNaN(d)) return ts;
    const p = n => String(n).padStart(2, '0');
    const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
    const isToday = new Date().toDateString() === d.toDateString();
    if (withDate === 'smart') return isToday ? hm : `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}`;
    if (withDate) return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}`;
    return hm;
  }
  // 表单里的时间框填的就是**本机墙上时间**，而库里也存本地时间，
  // 所以直接落库即可，无需换算。纯日期原样返回，其余规整成库内文本形态。
  function dbTime(s) {
    const raw = String(s || '').trim();
    if (!raw) return null;
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw;
    if (/[zZ]$|[+-]\d{2}:?\d{2}$/.test(raw)) return raw;
    return raw.replace('T', ' ');
  }
  const fmtMs = v => v == null ? '—' : (v >= 1000 ? (v / 1000).toFixed(2) + 's' : Math.round(v) + 'ms');
  const fmtNum = v => v == null ? '—' : (Number.isInteger(v) ? v : v.toFixed(1));

  let currentView = 'overview';
  let TIME_RANGE = parseInt(localStorage.getItem('tokriva-range') || '24', 10); // 小时；0=全部
  function withinRange(r) {
    if (!TIME_RANGE) return true;
    const t = new Date(String(r.created_at).replace(' ', 'T')).getTime();
    return Date.now() - t <= TIME_RANGE * 3600 * 1000;
  }
  const viewState = { probes: { q: '', level: 'ALL', status: 'ALL', scope: 'ALL', sort: 'created_at', dir: -1 }, endpoints: { q: '' }, rankView: 'card', matrixFilter: null };

  // ============================================================
  // 视图：总览
  // ============================================================
  function fmtAge(sec) {
    if (sec == null || !isFinite(sec)) return '—';
    if (sec < 90) return '刚刚';
    if (sec < 3600) return Math.max(1, Math.round(sec / 60)) + ' 分钟前';
    if (sec < 86400) return Math.round(sec / 3600) + ' 小时前';
    return Math.round(sec / 86400) + ' 天前';
  }

  // ---------- 状态矩阵（三层诊断：判定在服务端做一次，展示"现在"） ----------
  function matrixPanelHtml() {
    const ov = OVERVIEW;
    if (!ov) return '';
    const sevCls = { ok: 'ok', warn: 'warn', danger: 'danger', neutral: 'neutral' };
    const stageLabel = { DNS_FAIL: '域名解析失败', PROXY_UNREACHABLE: '代理不可达', TLS_FAIL: '握手失败', TCP_FAIL: '连接失败', TIMEOUT: '响应超时' };
    const skipReason = { L1_FAILED: '连通失败', L2_FAILED: '服务不可用', NOT_CONFIGURED: '缺少密钥', NO_DATA: '无数据' };
    const badgeMini = (cls, txt) => `<span class="badge ${cls}" style="height:18px"><span class="dot"></span>${txt}</span>`;
    const hbMini = hb => hb.map(h => {
      const cls = h.status === 'HEALTHY' ? 'ok' : h.status === 'NOT_CONFIGURED' ? 'skip' : 'fail';
      return `<span class="hb-mini ${cls}" title="${esc(fmtTs(h.at, true))} · ${esc(statusMeta(h.status).label || h.status)}"></span>`;
    }).join('');
    const l1Cell = l1 => {
      if (l1.state === 'unknown') return '<span class="cell-sub">无数据</span>';
      if (l1.state === 'no_key') return '<span class="cell-sub">未探测（缺少密钥）</span>';
      const st = l1.state === 'ok' ? badgeMini('ok', '通')
        : l1.state === 'down' ? badgeMini('danger', '不通')
        : badgeMini('warn', '待确认');
      const stage = l1.fail_stage ? `<span class="cell-sub">${stageLabel[l1.fail_stage] || l1.fail_stage}</span>` : '';
      const hb = l1.heartbeat && l1.heartbeat.length ? `<div class="hb-strip">${hbMini(l1.heartbeat)}</div>` : '';
      return `${st}${stage}${hb}`;
    };
    const l2Cell = l2 => {
      if (l2.skipped) return '<span class="cell-sub">跳过（连通失败）</span>';
      if (l2.state === 'unknown') return '<span class="cell-sub">无数据</span>';
      if (l2.state === 'NOT_CONFIGURED') return `${badgeMini('neutral', '缺少密钥')}<span class="cell-sub">未接入，未参与探测与评比</span>`;
      if (l2.state === 'limited') return `${badgeMini('warn', '受限')}<span class="cell-sub">速度测试推迟，避免加重限流</span>`;
      if (l2.state !== 'ok') return `${badgeMini('danger', esc(l2.fail_label || l2.state))}<span class="cell-sub">最近一次服务调用失败</span>`;
      const avail = l2.availability != null ? `可用率 <b class="mono">${l2.availability}%</b>` : '暂无样本';
      const lim = l2.limited ? ` · 受限 ${l2.limited} 次（不计入）` : '';
      return `${avail}<span class="cell-sub">${fmtWindow(ov.window_hours)} · 健康 ${l2.healthy}/${l2.denom}${lim}</span>`;
    };
    const l3Cell = l3 => {
      if (l3.state === 'skipped') return `<span class="cell-sub">跳过（${skipReason[l3.reason] || l3.reason}）</span>`;
      if (l3.mode === 'stream') return `首字 <b class="mono">${fmtMs(l3.ttft_p50)}</b> · ${l3.tps_p50} tok/s<span class="cell-sub">流式中位 · ${l3.samples} 样本</span>`;
      return `整包 <b class="mono">${fmtMs(l3.total_p50)}</b><span class="cell-sub">总耗时中位 · ${l3.samples} 样本</span>`;
    };
    const rowsSrc = (ov.matrix || []).filter(m => {
      if (!viewState.matrixFilter) return true;
      return m.final.toLowerCase() === viewState.matrixFilter;
    });
    const rows = rowsSrc.map(m => {
      const l1 = m.layers.l1, l2 = m.layers.l2, l3 = m.layers.l3;
      const stale = (m.overlays || []).includes('STALE');
      const finalBadge = `<span class="badge ${sevCls[m.severity] || 'neutral'}" style="height:20px"><span class="dot"></span>${esc(m.final_label)}${stale ? ' · 数据过期' : ''}</span>`;
      const mainRow = `<tr>
        <td><span class="cell-primary" data-open-ep="${esc(m.endpoint)}" style="cursor:pointer" title="查看详情 / 管理密钥">${esc(m.endpoint)}</span><span class="cell-sub">${esc(m.provider || '')}</span></td>
        <td><span class="path-badge ${m.path}">${m.path === 'proxy' ? '代理' : '直连'}</span></td>
        <td>${l1Cell(l1)}</td>
        <td>${l2Cell(l2)}</td>
        <td>${l3Cell(l3)}</td>
        <td>${finalBadge}</td>
        <td class="num${stale ? ' stale' : ''}">${fmtAge(m.age_sec)}</td>
      </tr>`;
      const subRows = (m.models || []).map(md => `<tr class="matrix-sub">
        <td><span class="cell-sub" style="padding-left:14px;color:var(--ink-2);font-size:11px">└ ${esc(md.model)}</span></td>
        <td><span class="cell-sub">—</span></td><td><span class="cell-sub">—</span></td>
        <td>${md.status === 'HEALTHY' ? '<span class="cell-sub">可用</span>' : badge(md.status)}</td>
        <td>${md.ttft_p50 != null ? `<span class="cell-sub">首字 ${fmtMs(md.ttft_p50)}</span>` : md.total_p50 != null ? `<span class="cell-sub">整包 ${fmtMs(md.total_p50)}</span>` : '<span class="cell-sub">—</span>'}</td>
        <td><span class="cell-sub">—</span></td><td><span class="cell-sub">—</span></td>
      </tr>`).join('');
      return mainRow + subRows;
    }).join('');
    let lastAge = null;
    if (ov.probe && ov.probe.last_cycle_at && ov.generated_at) {
      const gen = new Date(String(ov.generated_at).replace(' ', 'T'));
      const last = new Date(String(ov.probe.last_cycle_at).replace(' ', 'T'));
      if (!isNaN(gen) && !isNaN(last)) lastAge = (gen - last) / 1000;
    }
    // 计数筛选 chips（B1）：点击按判定过滤矩阵行
    const flt = viewState.matrixFilter;
    const chipDefs = [['down', '不通'], ['pending', '待确认'], ['limited', '受限'], ['ok', '可用'], ['no_key', '缺密钥']];
    const chips = chipDefs.map(([k, label]) =>
      `<button class="count-chip${flt === k ? ' is-active' : ''}" data-f="${k}">${ov.headline.counts[k] || 0} ${label}</button>`).join('');
    // 共因横幅（D2）：代理链路问题不让用户误判成单个接口的故障
    const cc = ov.common_cause ? `<div class="common-cause cc-${ov.common_cause.level}">
        <span class="cc-icon">⚠</span>
        <div><strong>${esc(ov.common_cause.text)}</strong><span>${esc(ov.common_cause.detail)}</span></div>
      </div>` : '';
    return `
      <section class="panel matrix-panel">
        <div class="panel-head">
          <div class="panel-title"><svg><use href="#i-server"/></svg>状态矩阵 · 现在谁能用</div>
          <span class="panel-sub">判定为当前状态，不随时间范围变化 · 可用率统计${fmtWindow(ov.window_hours)}</span>
        </div>
        ${cc}
        <div class="matrix-headline"><span class="status-dot ok" style="margin-right:6px"></span>${esc(ov.headline.text)}${lastAge != null ? ` · 最近探测 ${fmtAge(lastAge)}` : ''}
          <span class="count-chips">${chips}</span></div>
        <div class="table-scroll"><table class="data-table matrix-table">
          <colgroup><col style="width:170px" /><col style="width:64px" /><col style="width:220px" /><col style="width:200px" /><col style="width:200px" /><col style="width:130px" /><col /></colgroup>
          <thead><tr><th>接口</th><th>通路</th><th>① 连通</th><th>② 服务</th><th>③ 速度</th><th>判定</th><th class="num">更新</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="7" style="text-align:center;color:var(--ink-3);padding:18px">该状态下暂无接口</td></tr>'}</tbody>
        </table></div>
      </section>`;
  }

  function viewOverview() {
    const rowsInRange = ROWS.filter(withinRange);
    // 最快响应：流式接口比首字、整包接口比总耗时，标注口径
    const fast = Object.keys(EP_STATS)
      .filter(k => EP_STATS[k].dispTTFT != null || EP_STATS[k].dispTotal != null)
      .map(k => ({ k, v: EP_STATS[k].dispTTFT != null ? EP_STATS[k].dispTTFT : EP_STATS[k].dispTotal,
                   kind: EP_STATS[k].dispTTFT != null ? '流式首字' : '整包总耗时' }))
      .sort((a, b) => a.v - b.v);
    const fastest = fast[0] || null;
    const dist = {};
    let notConfigured = 0;
    rowsInRange.forEach(r => {
      if (r.status === 'NOT_CONFIGURED') { notConfigured++; return; }
      dist[r.status] = (dist[r.status] || 0) + 1;
    });
    const distDenom = rowsInRange.length - notConfigured;
    const distArr = Object.entries(dist).sort((a, b) => b[1] - a[1]);

    const kpis = [
      (() => {
        // 缺少密钥以"配置"为准（后端 hasKey），不能以探测结果为准——
        // 探测若在网络层就失败（如 TLS 握手不通），到不了鉴权环节，没配密钥会被掩盖成网络故障
        const nMissing = Object.keys(ENDPOINTS).filter(k => {
          const e = ENDPOINTS[k];
          if (e.hasKey === true) return false;
          if (e.hasKey === false) return true;
          return EP_STATS[k] && EP_STATS[k].lastStatus === 'NOT_CONFIGURED'; // 兜底快照无 hasKey 时退回旧口径
        }).length;
        const vals = Object.values(ENDPOINTS);
        return { label: '接口总数', value: vals.length, unit: '个', icon: 'i-server',
                 foot: `已接入 ${vals.length - nMissing} · 缺少密钥 ${nMissing}（不计入可用率）`, delta: '' };
      })(),
      (() => {
        // 额度告急：剩余比例 ≤ 20% 的已录入接口；顺带提示未录入数量
        const urgent = [], noData = [];
        Object.entries(ENDPOINTS).forEach(([k, e]) => {
          const q = e.quotaSnap;
          if (!q || q.remaining == null || !q.total) { noData.push(k); return; }
          if (q.remaining / q.total <= 0.2) urgent.push(k);
        });
        // 没数据不能说"充足"（P8）：全部未录入时明说无法判定
        const foot = urgent.length
          ? `<span class="delta down">●</span> ${esc(urgent.join('、'))} 剩余不足 20%`
          : noData.length && urgent.length === 0
            ? `<span class="delta" style="color:var(--ink-4)">●</span> ${noData.length} 个未录入额度 · 暂无法判定`
            : `<span class="delta up">●</span> 额度充足`;
        return { label: '额度告急', value: urgent.length, unit: '个', icon: 'i-shield', foot, delta: '' };
      })(),
      (() => {
        // 与状态引擎同口径：每接口只统计首次成功之后的记录
        const counted = rowsInRange.filter(r => {
          const fs = EP_STATS[r.endpoint] && EP_STATS[r.endpoint].firstSuccessAt;
          return (!fs || String(r.created_at) >= fs) && r.status !== 'NOT_CONFIGURED';
        });
        const healthy = counted.filter(r => r.status === 'HEALTHY').length;
        return { label: '测试可用率', value: pct(healthy, counted.length), unit: '%', icon: 'i-shield', foot: `<span class="delta up">●</span> 健康 ${healthy}/${counted.length} 次${TIME_RANGE ? ' · 近 ' + (TIME_RANGE >= 24 ? TIME_RANGE / 24 + ' 天' : TIME_RANGE + ' 小时') : ' · 全部时间'}`, delta: '' };
      })(),
      { label: '最快响应', value: '—', unit: 'ms', icon: 'i-zap', foot: fastest ? `${fastest.k} · ${fastest.kind}中位数${TIME_RANGE ? ' · 近 ' + (TIME_RANGE >= 24 ? TIME_RANGE / 24 + ' 天' : TIME_RANGE + ' 小时') : ' · 全部时间'}` : '暂无合格样本', delta: '' }
    ];
    if (fastest) {
      const v = fastest.v;
      kpis[3].value = v >= 1000 ? (v / 1000).toFixed(2) : Math.round(v);
      kpis[3].unit = v >= 1000 ? 's' : 'ms';
    }

    const kpiHtml = kpis.map(k => `
      <div class="kpi">
        <div class="kpi-top"><span class="kpi-label">${k.label}</span><span class="kpi-icon"><svg><use href="#${k.icon}"/></svg></span></div>
        <div class="kpi-value">${k.value}<span class="unit">${k.unit}</span></div>
        <div class="kpi-foot">${k.foot}</div>
      </div>`).join('');

    const distColors = { HEALTHY: 'var(--ok)', DEGRADED: 'var(--warn)', RESPONSE_INVALID: 'var(--danger)' };
    const distHtml = distArr.map(([s, c]) => `
      <div class="dist-row">
        <span class="dist-name"><span class="status-dot ${statusMeta(s).cls}"></span>${statusMeta(s).label}</span>
        <span class="dist-count">${c} · ${pct(c, distDenom)}%</span>
        <span class="dist-bar"><span style="width:${pct(c, distDenom)}%;background:${distColors[s] || 'var(--neutral)'}"></span></span>
      </div>`).join('') + (notConfigured ? `
      <div class="dist-row" style="opacity:.75">
        <span class="dist-name"><span class="status-dot neutral"></span>缺少密钥（不计入）</span>
        <span class="dist-count">${notConfigured}</span>
        <span class="dist-bar"><span style="width:0%;background:var(--neutral)"></span></span>
      </div>` : '');

    // 真正的"最近"：按时间倒序取前 6 条（API 与快照的数组顺序不同，不能依赖数组位置）
    const recent = ROWS.filter(withinRange)
      .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
      .slice(0, 6).map(r => `
      <tr>
        <td class="mono">${esc(fmtTs(r.created_at, "smart"))}</td>
        <td><span class="cell-primary">${esc(r.endpoint)}</span><span class="cell-sub" title="${r.timing_scope === 'proxy_hop' ? '计时包含本地代理段' : '计时为到服务商的直连耗时'}">${r.timing_scope === 'proxy_hop' ? '代理' : '直连'}</span></td>
        <td>${fmtNum(r.tps)}</td>
        <td>${levelCell(r.level)}${promptBadge(r)}</td>
        <td>${badge(r.status)}</td>
        <td class="num">${fmtMs(r.ttft_ms)}</td>
      </tr>`).join('');

    // 仅在有值得注意的信息时才显示一行简短注释
    const viewNote = (() => {
      const buffered = [...new Set(L2.filter(r => r.stream_mode === 'buffered').map(r => r.endpoint))];
      const outliers = [...new Set(L2.filter(r => r.ttft_ms != null && r.ttft_ms > 10000).map(r => r.endpoint))];
      const parts = [];
      if (buffered.length) parts.push(`<strong>${esc(buffered.join('、'))}</strong> 为整包返回，“首字延迟”≈总耗时，不参与比快`);
      if (outliers.length) parts.push(`<strong>${esc(outliers.join('、'))}</strong> 出现过超 10 秒的慢响应`);
      return parts.length ? `<p class="view-note">${parts.join('；')}</p>` : '';
    })();

    // 趋势图面板（矩阵可用时独占整行；否则与状态分布并排）
    const trendPanelHtml = `
          <section class="panel">
            <div class="panel-head">
              <div class="panel-title"><svg><use href="#i-activity"/></svg>首字延迟趋势 · 速度测试<span class="panel-sub">近 3 小时</span></div>
              <div class="segment" id="chartRange">
                <button class="is-active" data-range="all">全部</button>
                <button data-range="true">仅流式</button>
              </div>
            </div>
            <div class="chart-wrap" id="chartWrap">
              <svg class="chart-svg" id="ttftChart" viewBox="0 0 720 232" preserveAspectRatio="none"></svg>
              <div class="chart-tooltip" id="chartTip" style="opacity:0"></div>
            </div>
            <div class="chart-legend">
              ${(() => {
                // "最优"评比：优先用真流式首字中位数（整包接口的"首字"≈总耗时，混入不公平）；
                // 没有任何接口具备真流式数据时，退回普通中位数。至少 2 条有效记录才参与。
                const ks = Object.keys(ENDPOINTS);
                let bestName = null, bestVal = Infinity;
                ks.forEach(k => {
                  const s = EP_STATS[k];
                  const v = s.trueStream >= 2 ? s.ttftP50True : null;
                  if (v != null && v < bestVal) { bestVal = v; bestName = k; }
                });
                if (bestName === null) {
                  ks.forEach(k => {
                    const s = EP_STATS[k];
                    if (s.n >= 2 && s.ttftMedian != null && s.ttftMedian < bestVal) { bestVal = s.ttftMedian; bestName = k; }
                  });
                }
                return ks.map(k => `<span class="legend-item${k === bestName ? ' is-best' : ''}"><span class="legend-swatch" style="background:${ENDPOINTS[k].color}"></span>${k === bestName ? '<span class="legend-best">最优</span>' : ''}${k} 中位 <b>${EP_STATS[k].ttftMedian != null ? fmtMs(EP_STATS[k].ttftMedian) : '—'}</b></span>`).join('');
              })()}
              <span class="legend-item" style="color:var(--ink-3)">顶部色带 = 全部接口的连通汇总 · 绿=全通 红=有接口不通（悬停看是谁）灰=未接入</span>
            </div>
          </section>`;

    // 状态分布面板：仅在状态矩阵不可用（后端 /api/overview 不可达）时兜底显示
    const distPanelHtml = `
          <section class="panel">
            <div class="panel-head"><div class="panel-title"><svg><use href="#i-shield"/></svg>状态分布</div><span class="panel-sub">${TIME_RANGE ? `近 ${TIME_RANGE >= 24 ? TIME_RANGE / 24 + ' 天' : TIME_RANGE + ' 小时'}` : '全部时间'} · 健康统计 ${distDenom} 条${notConfigured ? ` · 另有缺少密钥 ${notConfigured} 条` : ''}</span></div>
            <div class="panel-body"><div class="dist-list">${distHtml}</div>
              <div style="margin-top:18px;padding-top:14px;border-top:1px solid var(--line-soft);font-size:11px;color:var(--ink-3);line-height:1.6">
                <span class="status-dot ok" style="margin-right:6px"></span>失败会按原因归类：鉴权失败、网络不通、响应异常、被限流等，便于快速定位问题。
              </div>
            </div>
          </section>`;

    content.innerHTML = `
      <div class="content-inner">
        <div class="view-head">
          <div class="view-head-row">
            <div>
              
              <h1 class="view-title">测试总览</h1>
              <p class="view-desc">${DATA_SOURCE === 'api'
                ? `实时数据 · 共 ${Object.keys(ENDPOINTS).length} 个接口、${ROWS.length} 条测试记录，每条都来自真实调用。`
                : DATA_SOURCE === 'static'
                ? `只读快照 · 共 ${Object.keys(ENDPOINTS).length} 个接口、${ROWS.length} 条测试记录，快照生成于 ${esc((SNAPSHOT.generatedAt || '').replace('T', ' '))}。`
                : `本地示例数据 · 共 ${Object.keys(ENDPOINTS).length} 个接口、${ROWS.length} 条测试记录。连上服务端后会自动换成实时数据。`}</p>
              ${viewNote}
            </div>
            <div class="head-actions">
              ${rangeSelectHtml('kpiRange', '测试可用率/最快响应的统计窗口，同时决定状态矩阵的可用率窗口')}
              <label class="auto-probe" id="autoProbeBar" style="display:${apiAvailable ? 'inline-flex' : 'none'}" title="自动定时检测">
                <select id="autoProbeSel">
                  <option value="0">手动</option>
                  <option value="15">每 15 分钟</option>
                  <option value="30">每 30 分钟</option>
                  <option value="60">每小时</option>
                  <option value="120">每 2 小时</option>
                </select>
                <span id="autoProbeStatus"></span>
              </label>
              <button class="btn btn-primary" data-action="run-probe"><svg><use href="#i-zap"/></svg>立即检测</button>
              <button class="btn btn-primary" data-action="go-probes"><svg><use href="#i-activity"/></svg>查看测试记录</button>
            </div>
          </div>
        </div>

        <section class="panel reco-hero" id="recoPanel" style="display:none"><div class="panel-body" id="recoBody"></div></section>

        <div class="kpi-grid">${kpiHtml}</div>

        ${OVERVIEW ? matrixPanelHtml() : ''}

        ${OVERVIEW ? trendPanelHtml : `<div class="grid-2">${trendPanelHtml}${distPanelHtml}</div>`}

        <section class="panel">
          <div class="panel-head"><div class="panel-title"><svg><use href="#i-clock"/></svg>最近测试</div><a class="panel-sub" href="#probes" data-nav="probes" style="color:var(--accent);font-weight:600">全部记录 →</a></div>
          <div class="table-scroll"><table class="data-table recent-table">
            <colgroup><col style="width:90px" /><col style="width:240px" /><col style="width:175px" /><col style="width:110px" /><col style="width:96px" /><col /></colgroup>
            <thead><tr><th>时间</th><th>接口</th><th title="每秒输出多少字">输出速度（tokens/s）</th><th>测试类型</th><th>状态</th><th class="num" title="发出请求到收到第一个字的时间">首字延迟</th></tr></thead>
            <tbody>${recent}</tbody>
          </table></div>
        </section>
      </div>`;

    renderRecoCard();
    renderTTFTChart('all');
    bindChartRange();
    bindAutoProbe();
    bindRangeSelect('kpiRange', () => loadFromApi(false));
    // 计数筛选 chips（B1）：点击切换矩阵过滤并重渲染
    document.querySelectorAll('.count-chip').forEach(c => c.addEventListener('click', () => {
      viewState.matrixFilter = viewState.matrixFilter === c.dataset.f ? null : c.dataset.f;
      RENDERERS[currentView]();
    }));
    // 矩阵接口名点击 → 详情抽屉（含密钥管理入口）
    document.querySelectorAll('[data-open-ep]').forEach(el =>
      el.addEventListener('click', () => openDrawer(el.dataset.openEp)));
  }

  // ---------- 当前最佳可用卡（与排名同源，只推荐数据完整的候选；模型名突出展示）----------
  function renderRecoCard() {
    const panel = $('#recoPanel'), host = $('#recoBody');
    if (!panel || !host) return;
    if (!apiAvailable) { panel.style.display = 'none'; return; }
    const scenes = [
      { key: 'agent', label: 'Agent / 编程' },
      { key: 'speed', label: '速度优先' }
    ];
    // 推荐资格（B5）：只推荐"当前连通 + 近1小时可用率达标"的接口（由状态引擎判定）。
    // 矩阵数据不可达时不加限制，退回原有的 missing/n 过滤。
    const recMap = {};
    if (OVERVIEW && OVERVIEW.matrix) OVERVIEW.matrix.forEach(m => { recMap[m.endpoint] = m; });
    const blocks = scenes.map(sc => {
      const srv = SERVER_RANK[sc.key];
      const all = (srv && srv.items) || [];
      const items = all.filter(it => {
        const m = recMap[it.endpoint];
        return !m || (m.recommendation && m.recommendation.eligible);
      });
      const best = items.find(it => !(it.missing || []).length && it.n > 0);
      if (!best) {
        // 无达标者：如实说明，并给出最接近的候选缺什么（设计文档 8.4.2）
        const near = all.find(it => it.n > 0);
        let msg = items.length ? '候选样本不足' : '暂无流式测试数据';
        if (near) {
          const m = recMap[near.endpoint];
          const miss = m && m.recommendation && m.recommendation.missing.length
            ? m.recommendation.missing
            : ['候选样本不足'];
          msg = `最接近：${esc(near.endpoint)} · ${esc(near.model || '')}，缺：${esc(miss.join('；'))}`;
        }
        return `<div class="reco-item">
          <span class="reco-scene">${sc.label}</span>
          <div class="reco-empty">${msg}</div></div>`;
      }
      const b = best.breakdown || {};
      const quota = ENDPOINTS[best.endpoint] && ENDPOINTS[best.endpoint].quotaSnap;
      const quotaTxt = quota && quota.remaining != null
        ? `额度 ${fmtNum(quota.remaining)}/${fmtNum(quota.total)} ${labelOf(UNIT_LABEL, quota.unit)}` : '额度未录入';
      const sub = best.modelId && best.modelId !== best.model ? `<small class="mono">${esc(best.modelId)}</small>` : '';
      const recoId = best.modelId || best.model || '';
      return `<div class="reco-item">
        <span class="reco-scene">${sc.label}</span>
        <div class="reco-model">${esc(best.model)}${sub}</div>
        <div class="reco-stats">
          <span>可用率<b>${b.availability || 0}%</b></span>
          <span>首字<b>${best.ttftP50 ? fmtMs(best.ttftP50) : '—'}</b></span>
          <span>合格样本<b>${best.n} 次</b></span>
        </div>
        <div class="reco-via">${esc(best.endpoint)} · ${quotaTxt}</div>
        <div class="reco-actions">
          <button class="icon-btn js-reco-id" data-id="${esc(recoId)}" title="复制模型 ID" style="width:28px;height:28px"><svg><use href="#i-copy"/></svg></button>
          <button class="icon-btn js-reco-curl" data-ep="${esc(best.endpoint)}" data-id="${esc(recoId)}" title="查看可直接测试的 curl 命令（密钥用占位符）" style="width:28px;height:28px"><svg><use href="#i-external"/></svg></button>
        </div>
      </div>`;
    }).join('');
    host.innerHTML = `
      <div class="reco-head">
        <svg><use href="#i-zap"/></svg><strong>当前最佳可用</strong>
        <span class="reco-sub">仅推荐当前连通、近 1 小时可用率达标的接口</span>
        <a href="#rankings" data-nav="rankings">查看依据 →</a>
      </div>
      <div class="reco-grid">${blocks}</div>`;
    // 推荐区复制入口：复制 ID / curl 浮窗（与详情抽屉同款体验）
    host.querySelectorAll('.js-reco-id').forEach(b => b.addEventListener('click', () => copyText(b.dataset.id, '模型 ID 已复制')));
    host.querySelectorAll('.js-reco-curl').forEach(b => b.addEventListener('click', () => {
      const epName = b.dataset.ep;
      const base = (ENDPOINTS[epName] && ENDPOINTS[epName].base) || '';
      showCurlModal(buildCurl(base, b.dataset.id));
    }));
    panel.style.display = '';
  }

  // ---------- 自动定时检测 ----------
  function bindAutoProbe() {
    const sel = $('#autoProbeSel'); if (!sel) return;
    const status = $('#autoProbeStatus');
    const render = (st) => {
      sel.value = st.enabled ? String(st.intervalMin || 30) : '0';
      status.textContent = st.enabled
        ? (st.nextRunAt ? `下次 ${fmtTs(st.nextRunAt)}` : '运行中')
        : '手动模式';
    };
    if (apiAvailable) {
      apiGet('/api/schedule').then(render).catch(() => { status.textContent = '读取失败'; });
    }
    sel.addEventListener('change', () => {
      const minutes = parseInt(sel.value, 10);
      if (!minutes) {
        apiPost('/api/schedule', { enabled: false, interval_min: 30 })
          .then(st => { render(st); toast('已关闭自动检测', 'i-clock'); })
          .catch(e => toast('设置失败：' + e.message, 'i-info'));
        return;
      }
      apiPost('/api/schedule', { enabled: true, interval_min: minutes })
        .then(st => { render(st); toast(`已开启自动检测，每 ${minutes} 分钟一轮`, 'i-clock'); })
        .catch(e => toast('设置失败：' + e.message, 'i-info'));
    });
  }

  // ---------- TTFT 折线图（原生 SVG） ----------
  function renderTTFTChart(range) {
    const svg = $('#ttftChart'); if (!svg) return;
    const toTs = r => new Date(String(r.created_at).replace(' ', 'T')).getTime();
    // 时间窗固定为最近 3 小时；range 只控制是否仅看流式
    const H3 = 3 * 3600 * 1000;
    const list = L2.filter(r => {
      if (r.ttft_ms == null) return false;
      if (range === 'true' && r.stream_mode !== 'true_stream') return false;
      return Date.now() - toTs(r) <= H3 && withinRange(r);
    }).slice().sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
    // SVG 宽度取容器实际像素宽，viewBox 与渲染尺寸 1:1：
    // 之前固定 720 + preserveAspectRatio="none"，容器更宽时文字被非等比拉伸变形
    const W = Math.max(320, Math.round(svg.getBoundingClientRect().width) || 720);
    // 聚合连通色带（B6 精简版）：一条，不按接口分行——
    // 每个接口的连通历史在状态矩阵的心跳条里已有，图上只回答"这段时间有没有接口不通"，
    // 悬停看是谁不通。按接口分行在 4 个接口时占掉 ~70px 高度，太拥挤。
    const l0s = ROWS.filter(r => r.level === 'L0' && Date.now() - toTs(r) <= H3)
      .slice().sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
    const rounds = [];
    l0s.forEach(r => {
      const t = toTs(r);
      const last = rounds[rounds.length - 1];
      if (last && t - last.t <= 120 * 1000) last.recs.push(r);  // 同一轮探测聚成一个时间点
      else rounds.push({ t, recs: [r] });
    });
    const lanesH = rounds.length ? 22 : 0;  // 色带 8px + 上下呼吸空间
    const H = 232 + lanesH;
    const pad = { l: 44, r: 12, t: 14 + lanesH, b: 26 };
    const innerW = W - pad.l - pad.r, innerH = H - pad.t - pad.b;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('width', W);
    svg.style.height = H + 'px';
    svg.style.width = '100%';
    // 纵轴按 P95×1.5 截断：个别离群的慢请求压到顶部，不撑爆画面（悬停仍显示真实值）
    const sorted = list.map(r => r.ttft_ms).sort((a, b) => a - b);
    const p95 = sorted.length ? sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * 0.95) - 1)] : 0;
    const maxVal = Math.max(1000, p95 * 1.5) * 1.05;
    const Y = v => pad.t + innerH - (Math.min(v, maxVal) / maxVal) * innerH;

    // 真实时间轴：折线与连通色带共用时间域（折线无数据时退用 L0 的时间域）
    const domain = (list.length ? list : l0s).map(toTs);
    const t0 = domain.length ? Math.min(...domain) : 0;
    const span = domain.length > 1 ? Math.max(1, Math.max(...domain) - t0) : 1;
    // 断档阈值放宽到 45 分钟：自动检测默认 30 分钟一轮，20 分钟阈值会把每轮间隔都判成断档
    const GAP = 45 * 60 * 1000;
    const Xts = ts => pad.l + (ts - t0) / span * innerW;
    const X = r => Xts(toTs(r));
    const xs = list.map(X);

    let g = '';
    // 聚合连通色带：全部接口连通状态的汇总（设计文档 B6）。
    // 标签与纵轴刻度列右对齐、与色带垂直居中，避免看起来错位
    if (rounds.length) {
      const bandY = 8, bandH = 8;
      g += `<text class="chart-axis" x="${pad.l - 8}" y="${bandY + bandH / 2 + 3}" text-anchor="end" style="font-size:9px">连通</text>`;
      rounds.forEach((rd, k) => {
        const x1 = Xts(rd.t);
        const tNext = k + 1 < rounds.length ? rounds[k + 1].t : null;
        const w = (tNext !== null && tNext - rd.t <= GAP) ? Xts(tNext) - x1 : 8;
        const fails = rd.recs.filter(r => r.status !== 'HEALTHY' && r.status !== 'NOT_CONFIGURED');
        const allSkip = rd.recs.every(r => r.status === 'NOT_CONFIGURED');
        const fill = fails.length ? 'var(--danger)' : allSkip ? 'var(--neutral)' : 'var(--ok)';
        const tip = fails.length
          ? `此时刻有接口不通：${fails.map(r => r.endpoint).join('、')}（其余正常）`
          : allSkip ? '全部接口未接入' : '全部接口连通正常';
        g += `<rect x="${x1.toFixed(1)}" y="${bandY}" width="${Math.max(2, w).toFixed(1)}" height="${bandH}" fill="${fill}" fill-opacity="${fails.length ? 0.9 : 0.45}" rx="2"><title>${esc(fmtTs(rd.recs[0].created_at, true))} · ${esc(tip)}</title></rect>`;
      });
    }
    // y 网格 + 轴标签
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + innerH - (i / 4) * innerH;
      const val = maxVal * i / 4;
      const label = val >= 1000 ? ((val / 1000).toFixed(1).replace(/\.0$/, '')) + 's' : Math.round(val);
      g += `<line class="chart-grid" x1="${pad.l}" y1="${y}" x2="${W - pad.r}" y2="${y}"/>`;
      g += `<text class="chart-axis" x="${pad.l - 8}" y="${y + 3}" text-anchor="end">${label}</text>`;
    }
    // x 标签：只取 4 个刻度，正常只显示时分，跨天时首尾带日期
    {
      const p = k => String(k).padStart(2, '0');
      const crossesDay = new Date(t0).toDateString() !== new Date(t0 + span).toDateString();
      const n = 4;
      for (let i = 0; i <= n; i++) {
        const d = new Date(t0 + span * i / n);
        const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
        const showDate = crossesDay && (i === 0 || i === n);
        const label = showDate ? `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}` : hm;
        g += `<text class="chart-axis" x="${(pad.l + innerW * i / n).toFixed(1)}" y="${H - 8}" text-anchor="middle">${esc(label)}</text>`;
      }
    }
    // 各接口折线（断档处分段，但不做任何标注；每组一个 <g>，hover 时聚焦高亮）
    const names = Object.keys(ENDPOINTS);
    names.forEach(name => {
      const pts = list.filter(r => r.endpoint === name);
      if (!pts.length) return;
      let d = '';
      let prevTs = null;
      pts.forEach(r => {
        const ts = toTs(r);
        const x = X(r).toFixed(1), y = Y(r.ttft_ms).toFixed(1);
        if (prevTs !== null && ts - prevTs > GAP) d += ` M${x},${y}`;
        else d += (d ? ` L${x},${y}` : `M${x},${y}`);
        prevTs = ts;
      });
      let grp = `<g data-ep="${esc(name)}"><path class="chart-line" d="${d}" stroke="${ENDPOINTS[name].color}"/>`;
      pts.forEach(r => {
        grp += `<circle class="chart-dot" cx="${X(r).toFixed(1)}" cy="${Y(r.ttft_ms).toFixed(1)}" r="2.5" fill="${ENDPOINTS[name].color}"/>`;
      });
      grp += '</g>';
      g += grp;
    });
    // hover 命中区（按时间相邻点分带）：悬停时聚焦当前接口折线
    list.forEach((r, i) => {
      const xL = i === 0 ? pad.l : (xs[i] + xs[i - 1]) / 2;
      const xR = i === list.length - 1 ? W - pad.r : (xs[i] + xs[i + 1]) / 2;
      g += `<rect class="chart-hit" data-i="${i}" data-ep="${esc(r.endpoint)}" x="${Math.min(xL, xR).toFixed(1)}" y="${pad.t}" width="${Math.max(4, Math.abs(xR - xL)).toFixed(1)}" height="${innerH}"/>`;
    });
    g += `<line class="chart-cross" id="chartCross" x1="0" y1="${pad.t}" x2="0" y2="${pad.t + innerH}" style="opacity:0"/>`;
    svg.innerHTML = g;

    svg.querySelectorAll('.chart-hit').forEach(hit => {
      hit.addEventListener('mousemove', e => {
        const i = +hit.dataset.i, r = list[i];
        const rect = svg.getBoundingClientRect();
        const px = xs[i] / W * rect.width, py = (pad.t + innerH - (Math.min(r.ttft_ms, maxVal) / maxVal) * innerH) / H * rect.height;
        const tip = $('#chartTip'), wrap = $('#chartWrap'), cross = $('#chartCross');
        cross.style.opacity = 1; cross.setAttribute('x1', xs[i]); cross.setAttribute('x2', xs[i]);
        svg.querySelectorAll('g[data-ep]').forEach(gEl => { gEl.style.opacity = gEl.dataset.ep === r.endpoint ? 1 : 0.12; });
        tip.innerHTML = `<div class="tt-time">${esc(fmtTs(r.created_at, true))}</div>
          <div class="tt-row"><span class="tt-swatch" style="background:${ENDPOINTS[r.endpoint].color}"></span><span class="tt-name">${esc(r.endpoint)}</span><span class="tt-val">${fmtMs(r.ttft_ms)}</span></div>
          ${r.tps != null ? `<div class="tt-row"><span class="tt-swatch" style="background:transparent"></span><span class="tt-name">出字速度</span><span class="tt-val">${r.tps} tokens/s</span></div>` : ''}`;
        tip.style.opacity = 1;
        tip.style.left = Math.max(80, Math.min(wrap.clientWidth - 80, px)) + 'px';
        tip.style.top = Math.max(60, py - 8) + 'px';
      });
      hit.addEventListener('mouseleave', () => {
        const tip = $('#chartTip'), cross = $('#chartCross');
        if (tip) tip.style.opacity = 0; if (cross) cross.style.opacity = 0;
        svg.querySelectorAll('g[data-ep]').forEach(gEl => { gEl.style.opacity = ''; });
      });
    });
  }
  function bindChartRange() {
    const seg = $('#chartRange'); if (!seg) return;
    seg.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
      seg.querySelectorAll('button').forEach(x => x.classList.remove('is-active'));
      b.classList.add('is-active'); viewState.chartRange = b.dataset.range; renderTTFTChart(b.dataset.range);
    }));
  }
  // 窗口尺寸变化时按新宽度重绘（viewBox 与像素 1:1，宽度变了必须重算）
  let _chartRszT = null;
  window.addEventListener('resize', () => {
    clearTimeout(_chartRszT);
    _chartRszT = setTimeout(() => {
      if (currentView === 'overview' && $('#ttftChart')) renderTTFTChart(viewState.chartRange || 'all');
    }, 200);
  });

  // ============================================================
  // 视图：接口清单
  // ============================================================
  function viewEndpoints() {
    const q = viewState.endpoints.q.toLowerCase();
    const flt = viewState.endpoints.filter || 'all';
    const names = Object.keys(ENDPOINTS).filter(n => {
      const e = ENDPOINTS[n];
      if (q && !(n.toLowerCase().includes(q) || e.provider.toLowerCase().includes(q))) return false;
      if (flt === 'proxy' && !e.proxy) return false;
      if (flt === 'quota' && !(e.quota === 'API' || e.quota === 'HEADER')) return false;
      return true;
    });
    const rows = names.map(name => {
      const e = ENDPOINTS[name], s = EP_STATS[name];
      return `<tr class="clickable" data-endpoint="${esc(name)}">
        <td><span style="color:${e.color};font-size:12px">●</span> <span class="cell-primary" style="color:var(--ink)">${esc(name)}</span><span class="cell-sub">${esc(e.provider)} · ${e.proxy ? '代理' : '直连'}</span></td>
        <td><span class="tag-mono" title="${esc(e.protocol || '')}">${esc(labelOf(PROTOCOL_LABEL, e.protocol))}</span></td>
        <td><span class="mono" style="font-size:11px">${esc(e.model)}</span>${e.modelId && e.modelId !== e.model ? `<span class="cell-sub mono" style="font-size:9px">${esc(e.modelId)}</span>` : ''}</td>
        <td>${e.hasKey === false
          ? badge('NOT_CONFIGURED')
          : badge(s.lastStatus || '')}</td>
        <td class="num">${s.dispMode === 'true_stream'
          ? (s.dispTTFT != null ? fmtMs(s.dispTTFT) : '—')
          : (s.dispTotal != null ? `<span title="整包返回：首字=总耗时">${fmtMs(s.dispTotal)}<span class="cell-sub">总耗时 · 整包</span></span>` : '—')}</td>
        <td class="num">${s.dispTps != null ? fmtNum(s.dispTps)
          : (s.dispMode === 'buffered' ? '<span class="mono" style="font-size:10px;color:var(--ink-4)" title="整包返回不适用输出速度">n/a · 整包</span>'
          : s.dispMode === 'single' ? '<span class="mono" style="font-size:10px;color:var(--ink-4)" title="单次返回不适用输出速度">n/a · 单次</span>'
          : '—')}</td>
        <td class="num">${e.ctxMax ? fmtCtx(e.ctxMax) : '—'}</td>
        <td>${(() => {
          const q = e.quotaSnap;
          if (!q) return '<span class="mono" style="font-size:10px;color:var(--ink-4)" title="还没有额度快照">未录入</span>';
          const remain = q.remaining != null ? fmtNum(q.remaining) : '—';
          const total = q.total != null ? '/' + fmtNum(q.total) : '';
          const reset = q.reset_at ? ` · 重置 ${fmtTs(q.reset_at, 'smart')}` : '';
          const exp = q.expires_at ? ` · 到期 ${fmtTs(q.expires_at, 'smart')}` : '';
          return `<span title="来源：${esc(labelOf(QUOTA_LABEL, q.source))}${q.captured_at ? ' · 采集于 ' + esc(fmtTs(q.captured_at, true)) : ''}">${remain}${total} ${labelOf(UNIT_LABEL, q.unit)}${reset}${exp}</span><span class="cell-sub">来源：${labelOf(QUOTA_LABEL, q.source)}</span>`;
        })()}</td>
        <td>${heartbeatHTML(name)}</td>
        <td>${(() => {
          if (s.lastStatus === 'NOT_CONFIGURED') return '<span class="mono" style="font-size:10px;color:var(--ink-4)">—</span>';
          const low = s.availability < 80;
          return `<span class="mini-bar ${low ? 'warn' : 'ok'}"><span style="width:${s.availability}%"></span></span><span class="mono" style="font-size:10px;color:var(--ink-3)"> ${s.availability}% · ${s.total} 次</span>`;
        })()}</td>
        <td class="mono" style="font-size:10px">${esc(fmtTs(s.lastAt, 'smart'))}</td>
        <td><svg style="width:14px;height:14px;color:var(--ink-4)"><use href="#i-chevron-right"/></svg></td>
      </tr>`;
    }).join('');

    content.innerHTML = `
      <div class="content-inner">
        <div class="view-head"><div class="view-head-row">
          <div><h1 class="view-title">接口清单</h1>
            <p class="view-desc">每个接口对应一个服务商账号和一把密钥。密钥只保存在服务端，页面上始终脱敏显示，不会回传到浏览器。</p></div>
          <div class="head-actions"><button class="btn btn-secondary" data-action="run-probe"><svg><use href="#i-zap"/></svg>立即检测</button><button class="btn btn-primary" data-action="add-endpoint"><svg><use href="#i-server"/></svg>新增接口</button></div>
        </div></div>

        <section class="panel">
          <div class="table-tools">
            <label class="search-input"><svg><use href="#i-search"/></svg><input id="epSearch" placeholder="搜索接口或服务商…" value="${esc(viewState.endpoints.q)}"/></label>
            <div class="filter-chips">
              ${[['all', '全部'], ['proxy', '走代理'], ['quota', '额度可查']].map(([k, label]) =>
                `<button class="chip ${flt === k ? 'is-active' : ''}" data-epfilter="${k}">${label}</button>`).join('')}
            </div>
          </div>
          <div class="table-scroll"><table class="data-table">
            <thead><tr><th>接口 / 服务商</th><th>对接方式</th><th>默认模型</th><th>当前状态</th><th class="num" title="流式接口=首字延迟中位数；整包接口=总耗时中位数（首字=总耗时）">响应耗时</th><th class="num" title="每秒输出多少字；仅流式测试有效，整包不适用">输出速度（tokens/s）</th><th class="num" title="模型一次能记住多少内容，单位是 token（一个汉字约 1～2 个 token）">上下文长度（token）</th><th title="额度剩余与重置时间，来自最新快照">额度 / 到期</th><th title="最近 40 次测试结果，悬停看详情，点击看记录">最近 40 次</th><th>可用率</th><th>最近测试</th><th></th></tr></thead>
            <tbody id="epBody">${rows || `<tr><td colspan="12"><div class="empty-state">未找到匹配的接口</div></td></tr>`}</tbody>
          </table></div>
          <div class="table-foot"><span>显示 ${names.length} / ${Object.keys(ENDPOINTS).length} 个接口</span><span>${DATA_SOURCE === 'api' ? '数据实时来自服务端' : DATA_SOURCE === 'static' ? '数据来自只读快照' : '当前为本地示例数据'}</span></div>
        </section>
      </div>`;

    function applyEpFilter() {
      const v = viewState.endpoints.q.toLowerCase(), f = viewState.endpoints.filter || 'all';
      let shown = 0;
      content.querySelectorAll('#epBody tr[data-endpoint]').forEach(tr => {
        const name = tr.dataset.endpoint, e = ENDPOINTS[name];
        const matchQ = !v || name.toLowerCase().includes(v) || e.provider.toLowerCase().includes(v);
        const matchF = f === 'all' || (f === 'proxy' && e.proxy) || (f === 'quota' && (e.quota === 'API' || e.quota === 'HEADER'));
        const show = matchQ && matchF;
        tr.style.display = show ? '' : 'none';
        if (show) shown++;
      });
      const foot = content.querySelector('.table-foot span');
      if (foot) foot.textContent = `显示 ${shown} / ${Object.keys(ENDPOINTS).length} 个接口`;
    }
    $('#epSearch').addEventListener('input', e => { viewState.endpoints.q = e.target.value; applyEpFilter(); });
    content.querySelectorAll('[data-epfilter]').forEach(b => b.addEventListener('click', () => {
      viewState.endpoints.filter = b.dataset.epfilter;
      content.querySelectorAll('[data-epfilter]').forEach(x => x.classList.toggle('is-active', x === b));
      applyEpFilter();
    }));
    content.querySelectorAll('tr[data-endpoint]').forEach(tr =>
      tr.addEventListener('click', () => openDrawer(tr.dataset.endpoint)));
    content.querySelectorAll('[data-hb]').forEach(el => {
      el.addEventListener('click', e => {
        e.stopPropagation();
        viewState.probes.q = el.dataset.hb;
        navigate('probes');
      });
    });
  }

  // ============================================================
  // 视图：测试记录
  // ============================================================
  function viewProbes() {
    const st = viewState.probes;
    const list = filteredProbes();
    const head = [
      ['created_at', '时间', '这条测试发生的时刻'],
      ['endpoint', '接口', ''],
      ['level', '测试类型', '连通检测只查能不能用、有哪些模型；速度测试会真的发一段对话'],
      ['status', '状态', ''],
      ['tps', '输出速度（tokens/s）', '每秒输出多少 token（字）——速度测试的核心指标'],
      ['ttft_ms', '首字延迟（毫秒）', '发出请求到看到第一个字花的时间'],
      ['model_id', '模型', ''],
      ['stream_mode', '返回方式', '流式＝逐字显示；整包＝一次性给全部内容'],
      ['http_status', 'HTTP 状态', '服务器返回的状态码，200 表示正常'],
      ['dns_ms', '域名解析（毫秒）', '把域名换成服务器地址花的时间'],
      ['tcp_ms', '建立连接（毫秒）', '和服务器建立连接花的时间；走代理的接口，此段是到本地代理的耗时'],
      ['tls_ms', '加密握手（毫秒）', 'HTTPS 加密协商花的时间；走代理的接口，此段是到本地代理的耗时'],
      ['ttfb_ms', '首字节响应（毫秒）', '服务器开始吐数据花的时间']
    ];
    const numCols = new Set(['http_status', 'dns_ms', 'tcp_ms', 'tls_ms', 'ttfb_ms', 'ttft_ms', 'tps']);
    const th = head.map(([key, label, tip]) => {
      const sorted = st.sort === key;
      const hideMd = ['http_status', 'dns_ms', 'tcp_ms', 'tls_ms', 'ttfb_ms'].includes(key) ? ' hide-md' : '';
      return `<th class="sortable ${sorted ? 'is-sorted' : ''} ${numCols.has(key) ? 'num' : ''}${hideMd}" data-sort="${key}"${tip ? ` title="${tip}"` : ''}>${label}<span class="sort-ind">${sorted ? (st.dir === 1 ? '▲' : '▼') : '↕'}</span></th>`;
    }).join('');
    const body = list.map((r, ri) => `
      <tr class="clickable" data-ri="${ri}" title="点击查看详情与诊断">
        <td class="mono" style="font-size:10px;white-space:nowrap">${esc(fmtTs(r.created_at, 'smart'))}</td>
        <td style="white-space:nowrap"><span class="cell-primary">${esc(r.endpoint)}</span><span class="cell-sub" title="${r.timing_scope === 'proxy_hop' ? '计时包含本地代理段' : '计时为到服务商的直连耗时'}">${r.timing_scope === 'proxy_hop' ? '代理' : '直连'}</span></td>
        <td>${levelCell(r.level)}${promptBadge(r)}</td>
        <td>${badge(r.status)}</td>
        <td class="num">${r.tps != null ? `<b>${r.tps}</b>` : (r.stream_mode === 'buffered' ? '整包' : '—')}</td>
        <td class="num" style="color:${r.ttft_ms > 10000 ? 'var(--danger)' : 'var(--ink)'}">${r.ttft_ms != null ? r.ttft_ms.toFixed(0) : '—'}</td>
        <td>${modelCell(r)}</td>
        <td><span class="badge ${r.stream_mode === 'true_stream' ? 'ok' : r.stream_mode === 'buffered' ? 'warn' : 'neutral'}"${r.stream_mode ? ` title="${esc(r.stream_mode)}"` : ''}><span class="dot"></span>${esc(labelOf(STREAM_LABEL, r.stream_mode, '—'))}</span></td>
        <td class="num hide-md">${r.http_status || '—'}</td>
        <td class="num hide-md">${r.dns_ms != null ? r.dns_ms.toFixed(1) : '—'}</td>
        <td class="num hide-md">${r.tcp_ms != null ? r.tcp_ms.toFixed(1) : '—'}</td>
        <td class="num hide-md">${r.tls_ms != null ? r.tls_ms.toFixed(0) : '—'}</td>
        <td class="num hide-md">${r.ttfb_ms != null ? r.ttfb_ms.toFixed(0) : '—'}</td>
      </tr>`).join('');

    content.innerHTML = `
      <div class="content-inner">
        <div class="view-head"><div class="view-head-row">
          <div><h1 class="view-title">测试记录</h1>
            <p class="view-desc">每次测试的原始记录。可按类型、状态筛选，点列头排序；首字延迟超过 10 秒的会用红色标出。</p></div>
          <div class="head-actions"><button class="btn btn-primary" data-action="run-probe"><svg><use href="#i-zap"/></svg>立即检测</button><button class="btn btn-secondary" data-action="export-csv"><svg><use href="#i-download"/></svg>导出表格</button></div>
        </div></div>

        <section class="panel">
          <div class="table-tools">
            <label class="search-input"><svg><use href="#i-search"/></svg><input id="probeSearch" placeholder="搜索接口、模型、状态…" value="${esc(st.q)}"/></label>
            <div class="filter-chips">
              <button class="chip" id="densityToggle" title="切换表格行高">紧凑密度</button>
              <div class="segment" id="levelFilter">
                ${[['ALL', '全部'], ['L0', '连通检测'], ['L2', '速度测试']].map(([lv, label]) => `<button class="${st.level === lv ? 'is-active' : ''}" data-level="${lv}" title="${lv === 'ALL' ? '全部测试记录' : lv === 'L0' ? '只检查能不能连通、能用哪些模型，几乎不消耗额度' : '真发一段对话，测首字延迟和输出速度'}">${label}</button>`).join('')}
              </div>
              <div class="segment" id="scopeFilter">
                ${['ALL', 'direct', 'proxy_hop'].map(sc => `<button class="${(st.scope || 'ALL') === sc ? 'is-active' : ''}" data-scope="${sc}">${sc === 'ALL' ? '全部口径' : sc === 'direct' ? '直连' : '代理'}</button>`).join('')}
              </div>
              <div class="segment" id="statusFilter">
                ${['ALL', 'HEALTHY'].map(sv => `<button class="${st.status === sv ? 'is-active' : ''}" data-status="${sv}">${sv === 'ALL' ? '全部状态' : '仅健康'}</button>`).join('')}
              </div>
              ${rangeSelectHtml('probeRange', '只显示这个时间范围内的测试记录')}
            </div>
          </div>
          <div class="table-scroll"><table class="data-table">
            <thead><tr>${th}</tr></thead><tbody id="probeBody">${body || `<tr><td colspan="13"><div class="empty-state">无匹配记录</div></td></tr>`}</tbody>
          </table></div>
          <div class="table-foot"><span id="probeCount">显示 ${list.length} / ${ROWS.length} 条</span><span>耗时单位毫秒；输出速度＝每秒输出多少 token（tokens/s）。域名解析、建立连接、加密握手、首字节响应属于网络分段耗时，只在连通检测阶段采集，不消耗额度</span></div>
        </section>
      </div>`;

    $('#densityToggle').addEventListener('click', () => {
      const table = content.querySelector('.data-table');
      table.classList.toggle('compact');
      $('#densityToggle').classList.toggle('is-active', table.classList.contains('compact'));
    });
    $('#probeSearch').addEventListener('input', e => { st.q = e.target.value; refreshProbeTable(); });
    $('#levelFilter').querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
      st.level = b.dataset.level;
      $('#levelFilter').querySelectorAll('button').forEach(x => x.classList.toggle('is-active', x === b));
      refreshProbeTable();
    }));
    $('#statusFilter').querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
      st.status = b.dataset.status;
      $('#statusFilter').querySelectorAll('button').forEach(x => x.classList.toggle('is-active', x === b));
      refreshProbeTable();
    }));
    $('#scopeFilter').querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
      st.scope = b.dataset.scope;
      $('#scopeFilter').querySelectorAll('button').forEach(x => x.classList.toggle('is-active', x === b));
      refreshProbeTable();
    }));
    bindRangeSelect('probeRange', () => RENDERERS[currentView]());
    bindRecordRows(list);
    content.querySelectorAll('th.sortable').forEach(th => th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (st.sort === key) st.dir *= -1; else { st.sort = key; st.dir = 1; }
      refreshProbeTable(true);
    }));
  }

  function filteredProbes() {
    const st = viewState.probes, q = st.q.toLowerCase();
    let list = ROWS.filter(withinRange).filter(r =>
      (st.level === 'ALL' || r.level === st.level) &&
      (st.status === 'ALL' || r.status === st.status) &&
      (st.scope === 'ALL' || (st.scope === 'proxy_hop' ? r.timing_scope === 'proxy_hop' : (r.timing_scope || 'direct') === 'direct')) &&
      (!q || [r.endpoint, r.model_id, r.model_display, r.status, r.level, r.stream_mode].some(v => String(v || '').toLowerCase().includes(q)))
    );
    list = [...list].sort((a, b) => {
      const av = a[st.sort], bv = b[st.sort];
      if (av == null && bv == null) return 0;
      if (av == null) return 1; if (bv == null) return -1;
      return (typeof av === 'number' ? av - bv : String(av).localeCompare(String(bv))) * st.dir;
    });
    return list;
  }
  function refreshProbeTable(rebuildHead) {
    if (rebuildHead) { viewProbes(); return; }
    const list = filteredProbes();
    $('#probeBody').innerHTML = list.map((r, ri) => `
      <tr class="clickable" data-ri="${ri}" title="点击查看详情与诊断">
        <td class="mono" style="font-size:10px;white-space:nowrap">${esc(fmtTs(r.created_at, 'smart'))}</td>
        <td><span class="cell-primary">${esc(r.endpoint)}</span><span class="cell-sub" title="${r.timing_scope === 'proxy_hop' ? '计时包含本地代理段' : '计时为到服务商的直连耗时'}">${r.timing_scope === 'proxy_hop' ? '代理' : '直连'}</span></td>
        <td>${levelCell(r.level)}${promptBadge(r)}</td>
        <td>${modelCell(r)}</td>
        <td>${badge(r.status)}</td>
        <td class="num">${r.http_status || '—'}</td>
        <td class="num">${r.dns_ms != null ? r.dns_ms.toFixed(1) : '—'}</td>
        <td class="num hide-md">${r.tcp_ms != null ? r.tcp_ms.toFixed(1) : '—'}</td>
        <td class="num hide-md">${r.tls_ms != null ? r.tls_ms.toFixed(0) : '—'}</td>
        <td class="num hide-md">${r.ttfb_ms != null ? r.ttfb_ms.toFixed(0) : '—'}</td>
        <td class="num" style="color:${r.ttft_ms > 10000 ? 'var(--danger)' : 'var(--ink)'}">${r.ttft_ms != null ? r.ttft_ms.toFixed(0) : '—'}</td>
        <td class="num">${r.tps != null ? r.tps : (r.stream_mode === 'buffered' ? '整包' : '—')}</td>
        <td><span class="badge ${r.stream_mode === 'true_stream' ? 'ok' : r.stream_mode === 'buffered' ? 'warn' : 'neutral'}"${r.stream_mode ? ` title="${esc(r.stream_mode)}"` : ''}><span class="dot"></span>${esc(labelOf(STREAM_LABEL, r.stream_mode, '—'))}</span></td>
      </tr>`).join('') || `<tr><td colspan="13"><div class="empty-state">无匹配记录</div></td></tr>`;
    $('#probeCount').textContent = `显示 ${list.length} / ${ROWS.length} 条`;
    bindRecordRows(list);
  }
  function bindRecordRows(list) {
    content.querySelectorAll('#probeBody tr[data-ri]').forEach(tr =>
      tr.addEventListener('click', () => openRecordDrawer(list[+tr.dataset.ri])));
  }

  // ============================================================
  // 视图：排名榜单
  // ============================================================
  function viewRankings() {
    // 只纳入真流式 HEALTHY L2 样本做本地兜底排名（后端可用时以后端为准）
    const eligible = L2.filter(r => r.status === 'HEALTHY' && r.stream_mode === 'true_stream' && r.ttft_ms != null);
    const scenes = {
      speed: { label: '速度榜', desc: '首字延迟与输出速度权重更高，优先响应快的服务。', w: { lat: 0.55, tps: 0.25, stab: 0.2 } },
      agent: { label: 'Agent / 编程', desc: '可用率与稳定性优先，适合长时间连续调用。', w: { lat: 0.3, tps: 0.15, stab: 0.55 } },
      stable: { label: '长期榜', desc: '额度与有效期权重更高，适合长期稳定使用。', w: { lat: 0.15, tps: 0.15, stab: 0.7 } }
    };
    const scene = viewState.rankScene || 'speed';
    const srv = SERVER_RANK[scene];
    let rows = '', excluded = [], w = scenes[scene].w, serverNote = '', fullWeights = null;

    if (srv && srv.items && srv.items.length) {
      // 后端统一口径（hub/rank.py）：硬过滤 + 缺失分项重分配 + 429 排除分母
      const bk = srv.weights || {};
      w = { lat: bk.ttft || 0, tps: bk.tps || 0, stab: bk.stability || 0,
            avail: bk.availability || 0, quota: bk.quota || 0 };
      fullWeights = bk;
      rows = srv.items.map((it, i) => {
        const b = it.breakdown || {};
        const miss = (it.missing || []).length
          ? `<span class="badge neutral" style="height:18px;margin-left:6px" title="这些指标暂时没有足够数据，评分按剩余指标重新分配权重">少 ${it.missing.map(m => labelOf(WEIGHT_LABEL, m)).join('、')}</span>` : '';
        return `<div class="rank-row ${i === 0 ? 'is-top' : ''}">
        <div class="rank-num">${i + 1}</div>
        <div>
          <div class="rank-name">${esc(it.endpoint)} <span class="badge ${(i === 0 && !(it.missing || []).length) ? 'accent' : 'neutral'}" style="height:18px">${(i === 0 && !(it.missing || []).length) ? '当前最优' : (i === 0 ? '暂列第一 · 数据不完整' : '候选')}</span>${miss}</div>
          <div class="rank-meta">${esc(it.model)}${it.modelId && it.modelId !== it.model ? `<span style="color:var(--ink-3)"> · ${esc(it.modelId)}</span>` : ''} · 合格样本 ${it.n} 次（门槛 ${srv.minSamples || 2}）${it.totalProbes != null ? ` · 窗口总探测 ${it.totalProbes}` : ''} · 首字 ${it.ttftP50 ? fmtMs(it.ttftP50) : '—'} · 输出 ${it.tpsP50 != null ? fmtNum(it.tpsP50) + ' tokens/s' : '—'}</div>
          <div class="rank-bars">
            <span class="rank-bar-item">首字 <span class="mini-bar"><span style="width:${b.latency || 0}%"></span></span></span>
            <span class="rank-bar-item">出字 <span class="mini-bar"><span style="width:${b.throughput || 0}%"></span></span></span>
            <span class="rank-bar-item">稳定 <span class="mini-bar"><span style="width:${b.stability || 0}%"></span></span></span>
            <span class="rank-bar-item">可用 <span class="mini-bar"><span style="width:${b.availability || 0}%"></span></span></span>
          </div>
        </div>
        <div class="rank-score"><b>${Math.round(it.score)}</b><small>得分</small></div>
      </div>`;
      }).join('');
      // 按「接口 × 原因」聚合，避免按模型重复刷屏
      const byEp = {};
      (srv.excluded || []).forEach(x => {
        const ep = x.endpoint, reason = (humanize(x.reason) || '数据不足').replace(/\(\d+\s*<\s*\d+\)/, '');
        const m = byEp[ep] = byEp[ep] || {};
        m[reason] = (m[reason] || 0) + 1;
      });
      excluded = Object.entries(byEp).map(([ep, reasons]) =>
        `${ep}（${Object.entries(reasons).map(([r, n]) => n > 1 ? `${n} 个模型${r}` : r).join('、')}）`);
      serverNote = '排名由服务端统一计算，和最终应用到你服务里的配置保持一致。';
    } else {
    const byEp = {};
    Object.keys(ENDPOINTS).forEach(n => byEp[n] = eligible.filter(r => r.endpoint === n));
    const metrics = {};
    Object.keys(ENDPOINTS).forEach(n => {
      const arr = byEp[n], ttfts = arr.map(r => r.ttft_ms).sort((a, b) => a - b);
      const tpss = arr.map(r => r.tps).filter(v => v != null);
      metrics[n] = {
        n: arr.length,
        ttft: ttfts.length ? ttfts[Math.floor(ttfts.length / 2)] : null,
        p95: ttfts.length ? ttfts[Math.min(ttfts.length - 1, Math.ceil(ttfts.length * 0.95) - 1)] : null,
        tps: tpss.length ? tpss.reduce((a, b) => a + b, 0) / tpss.length : null
      };
    });
    const maxTtft = Math.max(...Object.values(metrics).map(m => m.ttft || 0));
    const maxTps = Math.max(...Object.values(metrics).map(m => m.tps || 0));
    const maxDisp = Math.max(...Object.values(metrics).map(m => m.ttft && m.p95 ? m.p95 / m.ttft : 1));
    Object.values(metrics).forEach(m => {
      const lat = m.ttft ? 1 - m.ttft / (maxTtft * 1.1) : 0;
      const tps = m.tps ? m.tps / (maxTps * 1.1) : 0;
      const stab = (m.ttft && m.p95) ? 1 - (m.p95 / m.ttft) / (maxDisp * 1.1) : 0;
      m.score = Math.round((lat * w.lat + tps * w.tps + stab * w.stab) * 100);
      m.lat = Math.round(lat * 100); m.tpsN = Math.round(tps * 100); m.stabN = Math.round(stab * 100);
    });
    const ranked = Object.keys(ENDPOINTS).filter(n => metrics[n].n > 0).sort((a, b) => metrics[b].score - metrics[a].score);
    rows = ranked.map((n, i) => {
      const m = metrics[n], e = ENDPOINTS[n];
      return `<div class="rank-row ${i === 0 ? 'is-top' : ''}">
        <div class="rank-num">${i + 1}</div>
        <div>
          <div class="rank-name">${esc(n)} <span class="badge ${i === 0 ? 'accent' : 'neutral'}" style="height:18px">${i === 0 ? '当前最优' : '候选'}</span></div>
          <div class="rank-meta">${esc(e.model)} · 测过 ${m.n} 次 · 首字 ${m.ttft ? fmtMs(m.ttft) : '—'} · 速度 ${m.tps != null ? fmtNum(m.tps) + ' tokens/s' : '—'}</div>
          <div class="rank-bars">
            <span class="rank-bar-item">首字 <span class="mini-bar"><span style="width:${m.lat}%"></span></span></span>
            <span class="rank-bar-item">出字 <span class="mini-bar"><span style="width:${m.tpsN}%"></span></span></span>
            <span class="rank-bar-item">稳定 <span class="mini-bar"><span style="width:${m.stabN}%"></span></span></span>
          </div>
        </div>
        <div class="rank-score"><b>${m.score}</b><small>得分</small></div>
      </div>`;
    }).join('');
    excluded = Object.keys(ENDPOINTS).filter(n => metrics[n].n === 0).map(n => `${n}（没有合格的流式结果）`);
    serverNote = '暂时连不上服务端，这里是本地估算结果，仅供参考。';
    }

    // 紧凑表格视图（参考 free-model-pulse）：一行一个模型，一眼对比延迟/速度/成功率/状态
    function rankTableHtml() {
      const list = (srv && srv.items) ? srv.items : [];
      if (!list.length) {
        return `<div class="empty-state"><svg><use href="#i-rank"/></svg><div>还没有可排名的数据。${serverNote}</div></div>`;
      }
      const medal = ['🥇', '🥈', '🥉'];
      const body = list.map((it, i) => {
        const b = it.breakdown || {};
        const avail = b.availability != null ? b.availability : null;
        // 状态点：成功率越高越绿；<60% 红、60–90% 黄、>=90% 绿
        let dot = 'ok';
        if (avail == null) dot = 'neutral';
        else if (avail < 60) dot = 'danger';
        else if (avail < 90) dot = 'warn';
        const rankLabel = i < 3 ? `<span style="font-size:16px">${medal[i]}</span>` : `<span class="rank-num" style="font-size:13px">${i + 1}</span>`;
        return `<tr class="${i === 0 ? 'is-top' : ''}">
          <td class="num">${rankLabel}</td>
          <td>
            <span class="cell-primary">${esc(it.model)}</span>
            <span class="cell-sub">${esc(it.endpoint)}${it.modelId && it.modelId !== it.model ? ' · ' + esc(it.modelId) : ''}</span>
          </td>
          <td class="num">${it.contextLength != null ? fmtCtx(it.contextLength) : '—'}</td>
          <td class="num">${it.ttftP50 != null ? fmtMs(it.ttftP50) : '—'}</td>
          <td class="num">${it.tpsP50 != null ? fmtNum(it.tpsP50) : '—'}</td>
          <td class="num">${avail != null ? avail + '%' : '—'}</td>
          <td><span class="status-dot ${dot}"></span></td>
          <td class="num"><b>${Math.round(it.score)}</b></td>
        </tr>`;
      }).join('');
      return `<div class="table-scroll"><table class="data-table rank-table">
        <thead><tr>
          <th class="num">#</th>
          <th>模型</th>
          <th class="num" title="一次能记住多少内容，单位是 token">上下文（token）</th>
          <th class="num" title="发出请求到收到第一个字的时间（中位数）">首字延迟</th>
          <th class="num" title="每秒输出多少字（中位数）">输出速度（tokens/s）</th>
          <th class="num" title="状态正常的测试占比（429 限流不计入）">成功率</th>
          <th>状态</th>
          <th class="num">得分</th>
        </tr></thead>
        <tbody>${body}</tbody>
      </table></div>`;
    }

    content.innerHTML = `
      <div class="content-inner">
        <div class="view-head"><div class="view-head-row">
          <div><h1 class="view-title">排名榜单</h1>
            <p class="view-desc">按响应速度、稳定性和额度综合打分，帮你挑出现在最值得用的接口。目前有 ${Object.keys(ENDPOINTS).length} 个接口、${ROWS.length} 条测试记录，次数还不多，分数仅供参考。</p></div>
          <div class="head-actions"><button class="btn btn-primary" data-action="toast-dispatch" ${(() => {
            const items = (srv && srv.items) || [];
            const anyFull = items.some(it => !(it.missing || []).length && it.n > 0);
            return anyFull ? '' : 'disabled title="当前没有数据完整的候选，先积累样本或补充密钥"';
          })()}><svg><use href="#i-zap"/></svg>应用推荐配置</button></div>
        </div></div>

        <div class="notice"><svg><use href="#i-info"/></svg><div><strong>上榜条件：</strong>只看<b>状态正常</b>、<b>24 小时内测过</b>、并且是<b>流式</b>的速度测试结果；测得次数太少、额度用完或已到期的不会上榜。${serverNote}${excluded.length ? `未入榜 ${excluded.length} 项：<strong>${excluded.slice(0,4).join('、')}</strong>${excluded.length>4?' 等':''}。` : ''}权重目前是初始值，数据积累后会逐步校准。</div></div>

        <div class="grid-2-even" style="grid-template-columns:1fr 1fr;margin-bottom:16px;align-items:center">
          <div class="segment" id="sceneFilter">
            ${Object.entries(scenes).map(([k, v]) => `<button class="${scene === k ? 'is-active' : ''}" data-scene="${k}">${v.label}</button>`).join('')}
          </div>
          <div class="segment" id="rankViewFilter" style="justify-self:end">
            <button class="${viewState.rankView === 'card' ? 'is-active' : ''}" data-rview="card">卡片</button>
            <button class="${viewState.rankView === 'table' ? 'is-active' : ''}" data-rview="table" title="紧凑表格：一行一个模型，一眼对比延迟、速度、成功率">表格</button>
          </div>
        </div>
        <p style="font-size:12px;color:var(--ink-3);margin:0 0 16px">${scenes[scene].desc}</p>

        ${viewState.rankView === 'table' ? rankTableHtml() : `<div class="rank-list">${rows}</div>`}

        <section class="panel" style="margin-top:18px">
          <div class="panel-head"><div class="panel-title"><svg><use href="#i-info"/></svg>评分方法（当前权重）</div></div>
          <div class="panel-body" style="display:grid;grid-template-columns:repeat(5,1fr);gap:16px">
            ${(fullWeights
              ? [['availability', '可用率'], ['ttft', '首字延迟'], ['tps', '输出速度'], ['stability', '稳定性'], ['quota', '剩余额度']]
              : [['lat', '首字延迟'], ['tps', '输出速度'], ['stab', '稳定性']]
            ).map(([k, label]) => `<div><div class="micro" style="color:var(--ink-3)">${label}</div><div style="font-size:20px;font-weight:650;margin-top:4px">${Math.round((fullWeights ? (fullWeights[k] || 0) : (w[k] || 0)) * 100)}%</div></div>`).join('')}
          </div>
        </section>
      </div>`;

    $('#sceneFilter').querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
      viewState.rankScene = b.dataset.scene; viewRankings();
    }));
    $('#rankViewFilter').querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
      viewState.rankView = b.dataset.rview; viewRankings();
    }));
  }

  // ============================================================
  // 视图：提醒中心
  // ============================================================
  function viewReminders() {
    // 来源类型只作说明，用中性色；颜色留给状态机，避免两套配色互相打架
    const kindLabel = {
      EXPIRING: '即将到期', EXPIRED: '已过期', QUOTA_LOW: '额度偏低',
      AUTH_FAILED: '鉴权失败', UNREACHABLE: '连续不可用',
      MODEL_GONE: '模型下线', NOT_CONFIGURED: '未接入'
    };
    const stateMeta = {
      ALERTING: { cls: 'danger', label: '持续告警' },
      PENDING: { cls: 'info', label: '待确认' },
      RECOVERING: { cls: 'ok', label: '恢复中' }
    };
    const S = REMINDER_META.summary || { pending: 0, alerting: 0, recovering: 0 };
    const T = REMINDER_META.tuning || { confirmN: 2, recoverM: 2 };

    const fmtMin = (m) => {
      if (m === null || m === undefined || m < 0) return '—';
      if (m < 1) return '刚刚';
      if (m < 60) return m + ' 分钟';
      const h = Math.floor(m / 60);
      if (h < 24) return h + ' 小时' + (m % 60 ? (m % 60) + ' 分' : '');
      return Math.floor(h / 24) + ' 天';
    };

    const FILTERS = [
      { k: 'all', label: '全部', n: REMINDERS.length },
      { k: 'ALERTING', label: '持续告警', n: S.alerting },
      { k: 'PENDING', label: '待确认', n: S.pending },
      { k: 'RECOVERING', label: '恢复中', n: S.recovering }
    ];
    if (!FILTERS.some(f => f.k === REMINDER_FILTER)) REMINDER_FILTER = 'all';
    const shown = REMINDER_FILTER === 'all'
      ? REMINDERS : REMINDERS.filter(r => r.state === REMINDER_FILTER);

    const rows = shown.length ? shown.map(r => {
      const sm = stateMeta[r.state] || { cls: 'neutral', label: r.state || '未知' };
      const isMissing = r.kind === 'NOT_CONFIGURED';
      const canAct = r.state === 'ALERTING';
      const actions = isMissing
        ? `<button class="btn btn-secondary" style="height:26px;font-size:11px" data-action="go-configure" data-ep="${esc(r.endpoint)}">去配置</button>`
        : (canAct
          ? `<button class="btn btn-secondary" style="height:26px;font-size:11px" data-rack="ack" data-key="${esc(r.key || '')}">知道了</button>
             <button class="btn btn-secondary" style="height:26px;font-size:11px" data-rack="snooze" data-key="${esc(r.key || '')}">延后 1 天</button>
             <button class="btn btn-secondary" style="height:26px;font-size:11px" data-rack="mute" data-key="${esc(r.key || '')}">静音</button>`
          : `<button class="btn btn-secondary" style="height:26px;font-size:11px" data-rack="mute" data-key="${esc(r.key || '')}">不再提示</button>`);
      const bits = [esc(r.endpoint), kindLabel[r.kind] || r.kind];
      if (r.age_min !== null && r.age_min !== undefined) bits.push('已出现 ' + fmtMin(r.age_min));
      if (r.seen > 1) bits.push('连续命中 ' + r.seen + ' 次');
      if (r.flaps > 0) bits.push('反复 ' + r.flaps + ' 次');
      if (r.state === 'PENDING') bits.push('再命中 ' + Math.max(1, T.confirmN - (r.seen || 1)) + ' 次即确认告警');
      if (r.state === 'RECOVERING') bits.push('再正常 ' + Math.max(1, T.recoverM - (r.clear || 0)) + ' 轮即销号');
      if (r.detail) bits.push(esc(r.detail));
      if (r.acked) bits.push('已确认');
      return `<div class="rank-row" style="grid-template-columns:104px 1fr auto">
        <span class="badge ${sm.cls}" style="justify-self:start"><span class="dot"></span>${sm.label}</span>
        <div><div class="rank-name">${esc(r.subject)}</div>
        <div class="rank-meta">${bits.join(' · ')}</div></div>
        <div style="display:flex;gap:6px;align-items:center">${actions}</div>
      </div>`;
    }).join('') : `<div class="empty-state"><svg><use href="#i-shield"/></svg><div>${
      REMINDER_FILTER === 'all' ? '暂无提醒 · 所有接口状态正常' : '这一组暂时没有提醒'
    }</div></div>`;

    const chips = FILTERS.filter(f => f.k === 'all' || f.n > 0).map(f =>
      `<button class="btn ${REMINDER_FILTER === f.k ? 'btn-primary' : 'btn-secondary'}"
         style="height:26px;font-size:11px" data-rfilter="${f.k}">${f.label} ${f.n}</button>`).join('');

    content.innerHTML = `
      <div class="content-inner">
        <div class="view-head"><div class="view-head-row">
          <div><h1 class="view-title">提醒中心</h1>
            <p class="view-desc">提示来自两处：到期与额度这类确定事实，出现即告警；鉴权失败与调不通来自探测，会先观察 ${T.confirmN} 轮再确认，恢复正常 ${T.recoverM} 轮后才销号。</p></div>
          <div class="head-actions"><button class="btn btn-secondary" data-action="refresh"><svg><use href="#i-refresh"/></svg>重新扫描</button></div>
        </div></div>
        <div class="kpi-grid" style="grid-template-columns:repeat(4,1fr)">
          <div class="kpi"><div class="kpi-top"><span class="kpi-label">持续告警</span><span class="kpi-icon"><svg><use href="#i-bell"/></svg></span></div><div class="kpi-value">${S.alerting}</div><div class="kpi-foot">已确认，需要处置</div></div>
          <div class="kpi"><div class="kpi-top"><span class="kpi-label">待确认</span><span class="kpi-icon"><svg><use href="#i-activity"/></svg></span></div><div class="kpi-value">${S.pending}</div><div class="kpi-foot">观察中，先不打扰</div></div>
          <div class="kpi"><div class="kpi-top"><span class="kpi-label">恢复中</span><span class="kpi-icon"><svg><use href="#i-shield"/></svg></span></div><div class="kpi-value">${S.recovering}</div><div class="kpi-foot">正在好转，继续观察</div></div>
          <div class="kpi"><div class="kpi-top"><span class="kpi-label">确认 / 恢复阈值</span><span class="kpi-icon"><svg><use href="#i-clock"/></svg></span></div><div class="kpi-value">${T.confirmN}<span class="unit">/ ${T.recoverM} 轮</span></div><div class="kpi-foot">可在设置里调整</div></div>
        </div>
        <section class="panel"><div class="panel-head"><div class="panel-title"><svg><use href="#i-bell"/></svg>提醒列表</div>
          <div style="display:flex;gap:6px;align-items:center">${chips}${REMINDER_META.muted ? `<span class="panel-sub">${REMINDER_META.muted} 条已静音</span>` : ''}</div></div>
          <div class="panel-body"><div class="rank-list">${rows}</div></div></section>
          ${(() => {
            const tr = (REMINDER_META.transitions || []).slice(0, 6);
            if (!tr.length) return '';
            const fromLabel = { ALERTING: '持续告警', PENDING: '待确认', RECOVERING: '恢复中' };
            const txt = tr.map(t => {
              if (!t.from) return `${t.endpoint} 新出现（${t.to_label}）`;
              if (!t.to) return `${t.endpoint} 恢复正常，已销号`;
              return `${t.endpoint} ${fromLabel[t.from] || t.from} → ${t.to_label}`;
            });
            return `<div class="notice" style="margin-top:14px"><svg><use href="#i-activity"/></svg><div><strong>最近一次扫描的变化：</strong>${txt.join('；')}。</div></div>`;
          })()}
          ${(() => {
            // 以密钥配置为准（hasKey），探测结果可能被网络故障掩盖（同 KPI 口径）
            const missing = ENDPOINTS && Object.keys(ENDPOINTS).filter(k => {
              const e = ENDPOINTS[k];
              if (e.hasKey === false) return true;
              if (e.hasKey === true) return false;
              return EP_STATS[k] && EP_STATS[k].lastStatus === 'NOT_CONFIGURED';
            });
            return missing && missing.length ? `<div class="notice"><svg><use href="#i-info"/></svg><div><strong>设置清单：</strong>${missing.map(k => esc(k)).join('、')} 缺少密钥——可在接口详情里直接填写，或配置好环境变量后下一轮检测自动开始。</div></div>` : '';
          })()}
        <div class="notice" style="margin-top:18px"><svg><use href="#i-info"/></svg><div>${
          (REMINDER_META.notify && REMINDER_META.notify.enabled)
            ? `已接入通知渠道：确认告警和恢复会推到那里，无人看页面时也照发。`
            : `目前只在页面里提示——关掉页面就收不到。可在「设置 → 通知渠道」填一个群机器人地址。`
        }被服务商限流不算故障，不计入可用率，也不会报警。提醒被静音后仍会继续观察，到期重新出现时带着原有状态，不会被当成新问题。</div></div>
      </div>`;
  }

  // ============================================================
  // 视图：设置
  // ============================================================
  function viewSettings() {
    content.innerHTML = `
      <div class="content-inner">
        <div class="view-head"><div class="view-head-row">
          <div><h1 class="view-title">设置</h1>
            <p class="view-desc">定时检测与提醒相关的运行参数。改动即时生效。</p></div>
        </div></div>

        <section class="panel">
          <div class="panel-head"><div class="panel-title"><svg><use href="#i-clock"/></svg>自动定时检测</div></div>
          <div class="panel-body">
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
              <select id="autoProbeSel" style="padding:6px 10px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);font-size:12px;color:var(--ink)">
                <option value="0">关闭（手动点「立即检测」）</option>
                <option value="15">每 15 分钟</option>
                <option value="30">每 30 分钟</option>
                <option value="60">每 1 小时</option>
                <option value="120">每 2 小时</option>
              </select>
              <span id="autoProbeStatus" style="font-size:12px;color:var(--ink-3)">读取中…</span>
            </div>
            <div class="cell-sub" style="margin-top:8px">定时检测在服务端运行；服务重启后会按保存的配置自动恢复。每轮检测消耗各接口的探测预算。</div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head"><div class="panel-title"><svg><use href="#i-activity"/></svg>探测预算</div>
            <span class="panel-sub" id="budgetState">读取中…</span></div>
          <div class="panel-body">
            <div class="cell-sub" style="margin-top:0">免费接口的每日额度往往很低，探针不该把它吃光。开启后有两道闸门，取更严的那个：一是每轮生成测试（速度测试）的当日次数；二是接口账号额度（如 <span class="mono">/usage</span> 给出的余额，按金额/token 限额的接口尤其需要），余额未知时不参与。连通检查不消耗额度，额度紧张时仍然照常进行。</div>
            <div style="display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap;margin-top:12px">
              <label style="display:flex;gap:6px;align-items:center;font-size:12px;color:var(--ink-2);cursor:pointer;padding-bottom:8px">
                <input type="checkbox" id="budgetEnabledToggle" style="width:16px;height:16px"> 启用每日限额
              </label>
              <label style="display:flex;flex-direction:column;gap:6px">
                <span style="font-size:11px;color:var(--ink-3)">用到百分之多少开始降级</span>
                <input type="number" id="budgetGuardPct" min="1" max="100" step="1"
                  style="width:96px;padding:6px 10px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);font-size:12px;color:var(--ink)">
              </label>
              <button class="btn btn-primary" id="budgetSave">保存</button>
            </div>
            <div class="cell-sub" style="margin-top:8px">每个接口的次数上限在「接口详情」里单独设置。到达阈值后每轮只测 1 个模型；额度用尽时该接口本轮只做连通检查——先降级、再停测，接口不会突然从监控里消失。</div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head"><div class="panel-title"><svg><use href="#i-bell"/></svg>提醒规则</div>
            <span class="panel-sub" id="reminderRulesState">读取中…</span></div>
          <div class="panel-body"><div class="kv-list">
            <div class="kv"><span class="k">到期提醒</span><span class="v">提前 30 / 7 / 1 天</span></div>
            <div class="kv"><span class="k">额度提醒</span><span class="v">剩余低于 20%</span></div>
            <div class="kv"><span class="k">限流（429）</span><span class="v">不算故障，不报警</span></div>
          </div>
          <div class="cell-sub" style="margin:14px 0 8px"><strong>连续命中阈值</strong>——只看鉴权失败、连不通这类「探测得出」的信号。</div>
          <div style="display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap">
            <label style="display:flex;flex-direction:column;gap:6px">
              <span style="font-size:11px;color:var(--ink-3)">连续失败几次转为「持续告警」</span>
              <input type="number" id="reminderConfirmN" min="1" max="10" step="1"
                style="width:96px;padding:6px 10px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);font-size:12px;color:var(--ink)">
            </label>
            <label style="display:flex;flex-direction:column;gap:6px">
              <span style="font-size:11px;color:var(--ink-3)">连续正常几轮后销号</span>
              <input type="number" id="reminderRecoverM" min="1" max="10" step="1"
                style="width:96px;padding:6px 10px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);font-size:12px;color:var(--ink)">
            </label>
            <label style="display:flex;flex-direction:column;gap:6px">
              <span style="font-size:11px;color:var(--ink-3)">故障未恢复时每隔几小时再提醒</span>
              <input type="number" id="notifyRepeatHours" min="0" max="168" step="1"
                style="width:96px;padding:6px 10px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);font-size:12px;color:var(--ink)">
            </label>
            <button class="btn btn-primary" id="reminderRulesSave">保存</button>
          </div>
          <div class="cell-sub" style="margin-top:8px">到期、额度不足这类确定事实一次即可确认，不受上面阈值影响。取值范围 1–10，调大更安静但发现更晚。</div>
          <div class="cell-sub" style="margin-top:6px">重发间隔填 <strong>0</strong> 表示关闭——只在故障刚确认时提醒一次。填正数则故障一直不恢复时按该间隔再提醒，提醒里会标「仍未恢复」，方便和刚发生的新故障区分。此设置需要先配好通知渠道。</div>
        </div></section>

        <section class="panel">
          <div class="panel-head"><div class="panel-title"><svg><use href="#i-bell"/></svg>通知渠道</div>
            <span class="panel-sub" id="notifyState">读取中…</span></div>
          <div class="panel-body">
            <div class="cell-sub" style="margin-top:0">提醒页只在打开时有用，而故障不挑你盯着屏幕的时候发生。填一个群机器人地址，确认告警与恢复都会推过去；留空则只在页面里提示。</div>
            <input id="notifyWebhookInput" type="text" spellcheck="false"
              placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…（留空 = 关闭通知）"
              style="width:100%;margin-top:10px;padding:9px 11px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);font-size:12px;color:var(--ink);font-family:var(--mono)">
            <div style="display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap">
              <button class="btn btn-primary" id="notifySave">保存</button>
              <button class="btn" id="notifyTest">发送测试</button>
              <label style="display:flex;gap:6px;align-items:center;font-size:12px;color:var(--ink-2);cursor:pointer">
                <input type="checkbox" id="notifyRecoverToggle"> 恢复时也通知
              </label>
              <span id="notifyLast" style="font-size:11px;color:var(--ink-3)"></span>
            </div>
            <div class="cell-sub" style="margin-top:8px">地址只存在服务端，页面回显为脱敏形式；企业微信机器人可直接使用，钉钉 / 飞书 / Slack 用一条转发规则接同一个 text 结构即可。发送失败会记在服务端日志里，不影响探测。</div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head"><div class="panel-title"><svg><use href="#i-activity"/></svg>测试问题</div>
            <span class="panel-sub" id="promptState">读取中…</span></div>
          <div class="panel-body">
            <textarea id="probePromptInput" rows="3" maxlength="500"
              placeholder="留空使用默认问题（Count from 1 to 20.）。自定义后每轮速度测试都用这个问题，记录会标为「自定义」，速度单独统计。"
              style="width:100%;padding:9px 11px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);font-size:12px;color:var(--ink);font-family:var(--sans);resize:vertical"></textarea>
            <div style="display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap">
              <button class="btn btn-primary" id="promptSave">保存</button>
              <button class="btn" id="promptReset">恢复默认</button>
              <span style="font-size:11px;color:var(--ink-3)">建议用贴近你真实用法的短问题（≤500 字）。问题越长越慢，会抬高所有接口的首字延迟。</span>
            </div>
          </div>
        </section>

        <div class="notice"><svg><use href="#i-info"/></svg><div>密钥只存在服务端环境变量里；页面与数据库都不会保存明文。通知地址同样只存服务端、回显时脱敏。</div></div>
      </div>`;
    bindAutoProbe();
    bindBudgetSettings();
    bindPromptSettings();
    bindReminderRules();
    bindNotifySettings();
  }

  // ---------- 探测预算设置 ----------
  function bindBudgetSettings() {
    const toggle = $('#budgetEnabledToggle'), pct = $('#budgetGuardPct'), state = $('#budgetState');
    if (!toggle || !pct || !state) return;
    const summary = () => toggle.checked
      ? `已启用 · 用到 ${pct.value}% 开始降级`
      : '未启用（速度测试不限次数）';
    const render = (s) => {
      if (s.budgetEnabled != null) toggle.checked = !!s.budgetEnabled;
      if (s.budgetGuardPct != null) pct.value = s.budgetGuardPct;
      pct.disabled = !toggle.checked;
      state.textContent = summary();
    };
    apiGet('/api/settings').then(render)
      .catch(() => { state.textContent = '读取失败（后端不可达）'; });
    toggle.addEventListener('change', () => {
      pct.disabled = !toggle.checked;
      state.textContent = summary();
    });
    $('#budgetSave').addEventListener('click', () => {
      apiPost('/api/settings', {
        budgetEnabled: toggle.checked,
        budgetGuardPct: parseInt(pct.value, 10) || 90,
      }).then(s => {
        render(s);
        toast(toggle.checked ? `已启用探测预算：用到 ${pct.value}% 开始降级` : '已关闭探测预算限制', 'i-activity');
      }).catch(e => toast('保存失败：' + e.message, 'i-info'));
    });
  }

  // ---------- 测试问题设置 ----------
  const DEFAULT_PROMPT = 'Count from 1 to 20.';  // 与 hub/probe.py 的默认值保持一致
  function bindPromptSettings() {
    const input = $('#probePromptInput'), state = $('#promptState');
    if (!input || !state) return;
    const renderState = (s) => {
      state.textContent = s.isCustom
        ? '当前使用自定义问题 ↓'
        : `当前使用默认问题：${DEFAULT_PROMPT}`;
    };
    apiGet('/api/settings').then(s => {
      input.value = s.probePrompt || '';
      renderState(s);
    }).catch(() => { state.textContent = '读取失败（后端不可达）'; });
    const save = (val) => {
      apiPost('/api/settings', { probePrompt: val }).then(s => {
        input.value = s.probePrompt || '';
        renderState(s);
        toast(s.isCustom ? '已保存自定义测试问题，下一轮检测生效' : '已恢复默认测试问题', 'i-activity');
      }).catch(e => toast('保存失败：' + e.message, 'i-info'));
    };
    $('#promptSave').addEventListener('click', () => save(input.value.trim()));
    $('#promptReset').addEventListener('click', () => save(''));
  }

  // ---------- 通知渠道设置 ----------
  function bindNotifySettings() {
    const input = $('#notifyWebhookInput'), state = $('#notifyState');
    const lastEl = $('#notifyLast'), toggle = $('#notifyRecoverToggle');
    const testBtn = $('#notifyTest');
    if (!input || !state) return;
    // 页面拿到的是脱敏地址（…?***）。原样提交回来表示"没改"，服务端据此保留原地址；
    // 所以这里不能清空、也不能当成新地址看待。
    let masked = '';

    const fmtAgo = (iso) => {
      if (!iso) return '';
      const t = new Date(iso).getTime();
      if (!Number.isFinite(t)) return '';
      const min = Math.round((Date.now() - t) / 60000);
      if (min < 1) return '刚刚';
      if (min < 60) return min + ' 分钟前';
      const h = Math.round(min / 60);
      return h < 24 ? h + ' 小时前' : Math.round(h / 24) + ' 天前';
    };
    const renderState = (s) => {
      if (!s.notifyWebhookSet) {
        state.textContent = '未开启 · 只在页面里提示';
        lastEl.textContent = '还没有可推送的地址';
        return;
      }
      state.textContent = s.notifyRecover
        ? '已开启 · 故障与恢复都推送'
        : '已开启 · 只在故障时推送';
      const ln = s.lastNotify;
      lastEl.textContent = ln
        ? `上次推送 ${fmtAgo(ln.at)}：${ln.sent ? `成功（${ln.count} 条）` : '失败' + (ln.error ? '，' + ln.error : '')}`
        : '还没推送过（等下一次状态翻转）';
    };
    const apply = (s) => {
      masked = s.notifyWebhook || '';
      input.value = masked;
      toggle.checked = !!s.notifyRecover;
      if (testBtn) testBtn.disabled = !s.notifyWebhookSet && !input.value.trim();
      renderState(s);
    };
    apiGet('/api/settings').then(apply)
      .catch(() => { state.textContent = '读取失败（后端不可达）'; });

    $('#notifySave').addEventListener('click', () => {
      const val = input.value.trim();
      if (val && !/^https?:\/\//i.test(val)) {
        toast('地址需以 http:// 或 https:// 开头', 'i-info'); return;
      }
      apiPost('/api/settings',
        { notifyWebhook: val, notifyRecover: toggle.checked }).then(s => {
        apply(s);
        toast(!s.notifyWebhookSet ? '已关闭通知'
          : (s.notifyRecover ? '已保存：故障与恢复都会推送' : '已保存：只在故障时推送'), 'i-bell');
      }).catch(e => toast('保存失败：' + e.message, 'i-info'));
    });

    $('#notifyTest').addEventListener('click', (ev) => {
      const btn = ev.currentTarget;
      const val = input.value.trim();
      if (val && val !== masked && !/^https?:\/\//i.test(val)) {
        toast('地址需以 http:// 或 https:// 开头', 'i-info'); return;
      }
      btn.disabled = true; btn.textContent = '发送中…';
      // 没改地址就发到已保存的那一条（input 里是脱敏值，发出去也认不出）
      const payload = (val && val !== masked) ? { webhook: val } : {};
      apiPost('/api/settings/notify-test', payload).then(() => {
        toast('测试消息已发出，去群里看一眼', 'i-bell');
        return apiGet('/api/settings').then(apply);
      }).catch(e => toast('发送失败：' + e.message, 'i-info'))
        .finally(() => { btn.disabled = false; btn.textContent = '发送测试'; });
    });

    toggle.addEventListener('change', () => {
      apiPost('/api/settings', { notifyRecover: toggle.checked })
        .then(apply)
        .catch(e => {
          toggle.checked = !toggle.checked;
          toast('保存失败：' + e.message, 'i-info');
        });
    });

    // 刚粘进来一个地址还没保存时，也允许先试发一条
    input.addEventListener('input', () => {
      if (testBtn) testBtn.disabled = !input.value.trim();
    });
  }

  // ---------- 提醒阈值设置 ----------
  function bindReminderRules() {
    const nInput = $('#reminderConfirmN'), mInput = $('#reminderRecoverM'),
      rInput = $('#notifyRepeatHours'), state = $('#reminderRulesState');
    if (!nInput || !mInput || !state) return;
    const repeatText = (h) => (h > 0 ? `每 ${h} 小时重发` : '不重发');
    const renderState = (s) => {
      const n = s.reminderConfirmN, m = s.reminderRecoverM, h = s.notifyRepeatHours;
      state.textContent = (n == null || m == null)
        ? '默认 连续 2 次失败告警 / 2 轮正常销号'
        : `当前 连续 ${n} 次失败告警 / 连续 ${m} 轮正常销号 / ${repeatText(h)}`;
    };
    apiGet('/api/settings').then(s => {
      if (s.reminderConfirmN != null) nInput.value = s.reminderConfirmN;
      if (s.reminderRecoverM != null) mInput.value = s.reminderRecoverM;
      if (rInput && s.notifyRepeatHours != null) rInput.value = s.notifyRepeatHours;
      renderState(s);
    }).catch(() => { state.textContent = '读取失败（后端不可达）'; });
    $('#reminderRulesSave').addEventListener('click', () => {
      const n = parseInt(nInput.value, 10), m = parseInt(mInput.value, 10);
      if (!Number.isFinite(n) || !Number.isFinite(m) || n < 1 || n > 10 || m < 1 || m > 10) {
        toast('阈值需为 1–10 的整数', 'i-info'); return;
      }
      const h = rInput ? parseInt(rInput.value, 10) : null;
      if (rInput && (!Number.isFinite(h) || h < 0 || h > 168)) {
        toast('重发间隔需为 0–168 的整数（小时）', 'i-info'); return;
      }
      const patch = { reminderConfirmN: n, reminderRecoverM: m };
      if (rInput) patch.notifyRepeatHours = h;
      apiPost('/api/settings', patch).then(s => {
        if (s.reminderConfirmN != null) nInput.value = s.reminderConfirmN;
        if (s.reminderRecoverM != null) mInput.value = s.reminderRecoverM;
        if (rInput && s.notifyRepeatHours != null) rInput.value = s.notifyRepeatHours;
        renderState(s);
        REMINDER_META = REMINDER_META || {};
        REMINDER_META.tuning = { confirmN: s.reminderConfirmN, recoverM: s.reminderRecoverM };
        const rep = (s.notifyRepeatHours > 0)
          ? `，未恢复每 ${s.notifyRepeatHours} 小时重发` : '，不重发';
        toast(`已保存：连续 ${s.reminderConfirmN} 次失败告警，连续 ${s.reminderRecoverM} 轮正常销号${rep}`, 'i-bell');
      }).catch(e => toast('保存失败：' + e.message, 'i-info'));
    });
  }

  // ============================================================
  // 视图：接入条件
  // ============================================================
  function viewCatalog() {
    const isApi = CATALOG.length > 0;
    const themeFor = name => Object.values(ENDPOINT_THEME).find(t => name && name.indexOf(Object.keys(ENDPOINT_THEME).find(k => ENDPOINT_THEME[k] === t) || '@@') >= 0) || { color: '#5e6ad2', short: (name || '?').slice(0, 2).toUpperCase() };
    // 排序：真接入(probed) > 已配置未探测 > 未接入；组内按名称
    const rankOf = c => c.probed ? 0 : (c.endpoint_label ? 1 : 2);
    const sorted = [...CATALOG].sort((a, b) => {
      const ra = rankOf(a), rb = rankOf(b);
      if (ra !== rb) return ra - rb;
      return (a.provider || '').localeCompare(b.provider || '', 'zh');
    });
    const cards = isApi ? sorted.map(c => {
      const t = themeFor(c.provider);
      const flag = v => { const s = String(v || 'UNKNOWN').toUpperCase(); return ['YES','TRUE','ALLOWED'].includes(s); };
      // 接入状态：以真实探测结果为准（probed），不是看手填的 endpoint_label
      let linkBadge;
      if (c.probed) {
        const detail = c.probed_models ? ` · ${c.probed_models} 个模型` : '';
        linkBadge = `<span class="badge ok" style="height:18px"><span class="dot"></span>已接入${esc(c.endpoint_label ? ' · ' + c.endpoint_label : '')}${detail}</span>`;
      } else if (c.endpoint_label) {
        linkBadge = `<span class="badge warn" style="height:18px"><span class="dot"></span>未接入</span>`;
      } else {
        linkBadge = `<span class="badge neutral" style="height:18px">未接入</span>`;
      }
      return `<article class="cat-card">
        <div class="cat-card-head">
          <div class="cat-logo" style="background:${t.color}">${esc(t.short)}</div>
          <div style="flex:1"><div class="cat-name">${esc(c.provider)}</div><div class="cat-type">${esc(labelOf(KIND_LABEL, c.kind))}${c.website ? ' · ' + esc(String(c.website).replace(/^https?:\/\//,'')) : ''}</div></div>
          <div>${linkBadge}</div>
        </div>
        <div class="cat-rows">
          <div class="cat-row"><span class="k">怎么注册</span><span class="v">${esc(labelOf(SIGNUP_LABEL, c.signup_method))}</span></div>
          <div class="cat-row"><span class="k">免费方式</span><span class="v">${esc(labelOf(FREE_LABEL, c.free_form))}</span></div>
          <div class="cat-row"><span class="k">限额</span><span class="v mono">${esc(c.rate_limits || '—')}</span></div>
          <div class="cat-row"><span class="k">能否转发</span><span class="v"><span class="badge ${flag(c.allow_proxy) ? 'ok' : 'neutral'}" style="height:18px"><span class="dot"></span>${labelOf(ALLOW_LABEL, c.allow_proxy)}</span></span></div>
          <div class="cat-row"><span class="k">能否商用</span><span class="v"><span class="tag-mono">${labelOf(ALLOW_LABEL, c.allow_commercial)}</span></span></div>
          <div class="cat-row"><span class="k">信息从哪来</span><span class="v"><span class="tag-mono">${labelOf(SOURCE_TYPE_LABEL, c.source_type)}</span> ${esc(c.verified_at || '—')}</span></div>
          ${c.confidence ? `<div class="cat-row"><span class="k">把握程度</span><span class="v"><span class="tag-mono">${labelOf(CONFIDENCE_LABEL, c.confidence)}</span></span></div>` : ''}
        </div>
        <div class="cat-foot"><span class="tag-mono">${labelOf(SOURCE_TYPE_LABEL, c.source_type)}</span>${c.source_url ? `<a href="${esc(c.source_url)}" target="_blank" rel="noopener">来源 <svg><use href="#i-external"/></svg></a>` : ''}${!c.source_url && c.source_type === 'official' ? '<span class="badge warn" style="height:18px" title="官方核实必须有来源链接与核实日期">需补来源链接</span>' : ''}${!c.verified_at && c.source_type === 'official' ? '<span class="badge warn" style="height:18px" title="官方核实必须有核实日期">需补核实日期</span>' : ''}${!c.source_url && c.source_type !== 'official' ? '<span class="tag-mono">无来源链接</span>' : ''}</div>
      </article>`;
    }).join('') : Object.keys(ENDPOINTS).map(n => {
      const e = ENDPOINTS[n];
      return `<article class="cat-card">
        <div class="cat-card-head"><div class="cat-logo" style="background:${e.color}">${esc(e.short)}</div>
          <div><div class="cat-name">${esc(e.provider)}</div><div class="cat-type">${esc(labelOf(KIND_LABEL, e.kind))} · ${esc(e.base)}</div></div></div>
        <div class="cat-rows">
          <div class="cat-row"><span class="k">每天最多测</span><span class="v mono">${e.budget} 次</span></div>
          <div class="cat-row"><span class="k">代理转发</span><span class="v">${e.proxy ? '必须走代理' : '直连'}</span></div>
          <div class="cat-row"><span class="k">额度怎么查</span><span class="v"><span class="tag-mono">${esc(labelOf(QUOTA_LABEL, e.quota))}</span></span></div>
        </div>
        <div class="cat-foot"><span class="tag-mono">本地示例数据</span></div>
      </article>`;
    }).join('');

    content.innerHTML = `
      <div class="content-inner">
        <div class="view-head"><div class="view-head-row">
          <div><h1 class="view-title">接入条件</h1>
            <p class="view-desc">这个服务怎么注册、免费怎么给、能不能转发和商用。每条都注明信息来源和核实日期；不清楚的就写「未核实」，不会擅自当成「不允许」。</p></div>
          <div class="head-actions"><button class="btn btn-secondary" data-action="catalog-add"><svg><use href="#i-catalog"/></svg>录入条目</button><button class="btn btn-secondary" data-action="refresh"><svg><use href="#i-refresh"/></svg>刷新</button></div>
        </div></div>
        <div class="notice"><svg><use href="#i-info"/></svg><div><strong>${isApi ? '服务端数据：' : '本地示例数据：'}</strong>${isApi
          ? `共 ${CATALOG.length} 条。数据集导入的条目，必须去官网核实后才算「官方页面核实」。`
          : '暂时连不上服务端，这里显示的是本地示例数据。'}</div></div>
        <div class="catalog-grid">${cards}</div>
      </div>`;
  }

  function openEndpointForm() {
    const drawer = $('#detailDrawer');
    const field = (id, label, ph, hint) => `<label class="kv" style="display:flex;flex-direction:column;gap:4px;padding:8px 0">
      <span class="k">${label}${hint ? ` <span style="color:var(--ink-4);font-weight:400;text-transform:none;letter-spacing:0">${hint}</span>` : ''}</span>
      <input id="${id}" class="mono" placeholder="${ph}" style="width:100%;padding:7px 9px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);font-size:12px"/>
    </label>`;
    drawer.innerHTML = `
      <div class="drawer-head"><span class="drawer-title">新增接口</span><button class="icon-btn" id="drawerClose" aria-label="关闭"><svg><use href="#i-close"/></svg></button></div>
      <div class="drawer-body">
        <div class="notice" style="margin-bottom:14px"><svg><use href="#i-shield"/></svg><div><strong>密钥不会保存到网页，也不会写进数据库。</strong>这里只填「密钥放在哪个配置项里」（例如 <span class="mono">MY_API_KEY</span>），真正的密钥请在服务端配置文件中设置。保存后，下一轮自动测试就会包含这个接口。</div></div>
        <div class="drawer-section">
          ${field('ef_label', '名称', '例如 MyProvider', '不能和已有接口重名')}
          ${field('ef_provider', '服务商', '例如 MyProvider Inc.')}
          ${field('ef_base', '接口地址', 'https://api.example.com/v1', '必须以 https:// 开头')}
          <label class="kv" style="display:flex;flex-direction:column;gap:4px;padding:8px 0">
            <span class="k">对接格式 <span style="color:var(--ink-4);font-weight:400;text-transform:none;letter-spacing:0">服务商文档里写的那种格式</span></span>
            <select id="ef_protocol" style="width:100%;padding:7px 9px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);font-size:12px"><option value="openai_chat">OpenAI 对话格式（最常见）</option><option value="openai_responses">OpenAI Responses 格式</option></select>
          </label>
          ${field('ef_env', '密钥存放的配置项名', 'MY_API_KEY', '建议以 _KEY 或 _TOKEN 结尾')}
          <label class="kv" style="display:flex;flex-direction:column;gap:4px;padding:8px 0">
            <span class="k">默认模型 <span id="ef_modelHint" style="color:var(--ink-4);font-weight:400;text-transform:none;letter-spacing:0">留空将用 auto 探测，部分网关不支持会失败；建议先点下方「测试连接」从候选里选</span></span>
            <input id="ef_model" class="mono" placeholder="测试连接后从下拉候选中选择" style="width:100%;padding:7px 9px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);font-size:12px" list="ef_modelList"/>
          </label>
          <datalist id="ef_modelList"></datalist>
          ${field('ef_budget', '每天最多测几次', '200', '防止浪费额度')}
          ${field('ef_note', '备注', '可选')}
          <label class="kv" style="flex-direction:row;align-items:center;padding:10px 0">
            <span class="k">必须走代理才能访问</span><span class="v"><input type="checkbox" id="ef_proxy" style="width:16px;height:16px"/></span>
          </label>
        </div>
        <div class="drawer-section" style="border-top:1px solid var(--line-soft);padding-top:10px">
          <div style="display:flex;gap:8px">
            <button class="btn btn-secondary" id="efTest" style="flex:1"><svg><use href="#i-zap"/></svg>测试连接并发现模型</button>
            <button class="btn btn-secondary" id="efCancel">取消</button>
          </div>
          <div id="efTestResult" style="margin-top:8px"></div>
        </div>
        <details class="drawer-section" style="margin-top:6px">
          <summary style="cursor:pointer;font-size:12px;font-weight:650;color:var(--ink-2)">额度与期限（可选，录入后显示在清单和排名里）</summary>
          <div class="kv-list" style="gap:6px;margin-top:8px">
            <label class="kv"><span class="k">额度类型</span><span class="v"><select id="ef_qtype" style="padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"><option value="TOTAL">总额度</option><option value="DAILY">每日额度</option><option value="SUBSCRIPTION">订阅额度</option></select></span></label>
            <label class="kv"><span class="k">单位</span><span class="v"><select id="ef_qunit" style="padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"><option value="USD">美元</option><option value="REQUEST">次</option><option value="TOKEN">字</option></select></span></label>
            <label class="kv"><span class="k">总量 / 剩余</span><span class="v" style="display:flex;gap:6px"><input id="ef_qtotal" placeholder="如 10" style="width:80px;padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"/><input id="ef_qremain" placeholder="如 7.5" style="width:80px;padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"/></span></label>
            <label class="kv"><span class="k">重置时间</span><span class="v"><input id="ef_qreset" placeholder="可选，2026-11-23 00:00" title="按本机本地时间填写，库内同样按本地时间保存，无需换算" style="width:170px;padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"/></span></label>
            <label class="kv"><span class="k">到期日</span><span class="v"><input id="ef_qexpire" placeholder="可选，2026-12-31" style="width:170px;padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"/></span></label>
          </div>
        </details>
        <button class="btn btn-primary" id="efSubmit" style="width:100%;margin-top:10px"><svg><use href="#i-server"/></svg>保存接口</button>
        <button class="btn btn-secondary" id="efSubmitTest" style="width:100%;margin-top:8px"><svg><use href="#i-zap"/></svg>保存并立即测试</button>
      </div>`;
    drawer.hidden = false; $('#drawerOverlay').hidden = false;
    $('#drawerClose').addEventListener('click', closeDrawer);
    $('#efCancel').addEventListener('click', closeDrawer);
    let pendingTestAfterSave = false;
    // 环境变量是否存在（服务端校验，不回传值）
    $('#ef_env').addEventListener('blur', () => {
      const name = $('#ef_env').value.trim();
      const hint = $('#ef_envHint');
      if (!name || !apiAvailable) { if (hint) hint.remove(); return; }
      apiGet('/api/env-check?auth_env=' + encodeURIComponent(name)).then(r => {
        if (hint) hint.remove();
        $('#ef_env').insertAdjacentHTML('afterend',
          `<span id="ef_envHint" class="cell-sub" style="color:${r.found ? 'var(--ok)' : 'var(--warn)'}">${r.found ? '✓ 服务端已找到该变量' : '⚠ 服务端还没找到该变量，先在 .env 里配置'}</span>`);
      }).catch(() => {});
    });
    // 测试连接并发现模型
    $('#efTest').addEventListener('click', () => {
      const base_url = $('#ef_base').value.trim(), auth_env = $('#ef_env').value.trim(), protocol = $('#ef_protocol').value;
      const host = $('#efTestResult');
      if (!base_url || !auth_env) { toast('先填接口地址和密钥配置项名', 'i-info'); return; }
      host.innerHTML = '<span class="cell-sub">正在测试（最多 15 秒）…</span>';
      fetch(API_BASE + '/api/endpoints/test', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ base_url, protocol, auth_env }) })
        .then(r => r.json())
        .then(d => {
          const models = d.models || [];
          // 全部发现的模型灌入 datalist：输入框下拉可搜索选择（模型多时比 chip 好用）
          const dl = $('#ef_modelList');
          if (dl && models.length) {
            dl.innerHTML = models.map(m => {
              const mid = m.model_id || m.id || String(m);
              const dn = (m.display_name || '').trim();
              return `<option value="${esc(mid)}">${esc(dn && dn !== mid ? dn : '')}</option>`;
            }).join('');
            $('#ef_modelHint').innerHTML = `已发现 <strong>${models.length}</strong> 个模型，点击输入框下拉选择（可输入关键字过滤）`;
            $('#ef_modelHint').style.color = 'var(--ok)';
          }
          host.innerHTML = `<div style="font-size:12px;font-weight:650;color:${d.ok ? 'var(--ok)' : 'var(--danger)'}">${d.ok ? '✓ ' : '✗ '}${esc(d.category || '未知')}${d.httpStatus ? ' · HTTP ' + d.httpStatus : ''}${d.latencyMs ? ' · ' + fmtMs(d.latencyMs) : ''}</div>
            ${!d.ok && d.detail ? `<div class="cell-sub mono" style="max-height:60px;overflow:auto">${esc(d.detail)}</div>` : ''}
            ${models.length ? `<div style="margin-top:6px"><span class="cell-sub">连接成功，请从上方「默认模型」下拉中选择一个作为探测模型。常用候选：</span><div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">${models.slice(0, 12).map(m => {
              const mid = m.model_id || m.id || String(m);
              const dn = (m.display_name || '').trim();
              return `<button class="chip" data-pick-model="${esc(mid)}">${esc(dn && dn !== mid ? dn : mid)}</button>`;
            }).join('')}</div>${models.length > 12 ? '<span class="cell-sub">其余 ' + (models.length - 12) + ' 个在输入框下拉中搜索</span>' : ''}</div>`
            : d.ok ? '<div class="cell-sub" style="margin-top:6px">网关没有返回模型列表，请查服务商文档手动填写「默认模型」</div>' : ''}`;
          host.querySelectorAll('[data-pick-model]').forEach(b => b.addEventListener('click', () => {
            $('#ef_model').value = b.dataset.pickModel;
          }));
        })
        .catch(e => { host.innerHTML = `<span style="color:var(--danger);font-size:12px">测试失败：${esc(e.message)}</span>`; });
    });
    $('#efSubmit').addEventListener('click', () => {
      const payload = {
        label: $('#ef_label').value.trim(), provider: $('#ef_provider').value.trim(),
        base_url: $('#ef_base').value.trim(), protocol: $('#ef_protocol').value,
        auth_env: $('#ef_env').value.trim(), default_model: $('#ef_model').value.trim() || null,
        daily_budget: parseInt($('#ef_budget').value, 10) || 200,
        use_proxy: $('#ef_proxy').checked, note: $('#ef_note').value.trim() || null
      };
      if (!payload.label || !payload.provider || !payload.base_url || !payload.auth_env) {
        toast('名称、服务商、接口地址、密钥配置项名都要填', 'i-info'); return;
      }
      // 未选默认模型时明确拦截确认（此前静默回退 auto，导致部分网关探测必然失败）
      if (!payload.default_model) {
        const n = ($('#ef_modelList') && $('#ef_modelList').children.length) || 0;
        const msg = n
          ? `还没有选择默认模型（测试发现了 ${n} 个候选）。探测将用 "auto"，部分网关（如 OpenCode）不支持会失败。\n\n确定仍要保存吗？`
          : '还没有设置默认模型，探测将用 "auto"，部分网关不支持会失败。建议先点「测试连接并发现模型」再从候选里选。\n\n确定仍要保存吗？';
        if (!window.confirm(msg)) return;
      }
      const saveThen = (testAfter) => {
        fetch(API_BASE + '/api/endpoints', { method: 'POST',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
          .then(async r => { if (!r.ok) throw new Error((await r.json()).detail || 'HTTP ' + r.status); return r.json(); })
          .then(d => {
            // 额度可选录入
            const qtotal = parseFloat($('#ef_qtotal') && $('#ef_qtotal').value);
            if (d.id && !isNaN(qtotal)) {
              const qbody = {
                endpoint_id: d.id,
                quota_type: $('#ef_qtype').value,
                unit: $('#ef_qunit').value,
                total: qtotal,
                remaining: parseFloat($('#ef_qremain').value) || null,
                reset_at: dbTime($('#ef_qreset') && $('#ef_qreset').value),
                expires_at: dbTime($('#ef_qexpire') && $('#ef_qexpire').value)
              };
              fetch(API_BASE + '/api/quota/manual', { method: 'POST',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(qbody) }).catch(() => {});
            }
            closeDrawer();
            toast(`已登记 ${d.label}${d.created ? '' : '（已更新）'}：${d.note}`, 'i-server');
            loadFromApi(true).then(() => {
              if (currentView === 'endpoints') viewEndpoints();
              if (testAfter) runProbeNow(d.label);
            });
          })
          .catch(e => toast('保存失败：' + e.message, 'i-info'));
      };
      saveThen(pendingTestAfterSave);
      pendingTestAfterSave = false;
    });
  }

  function openCatalogForm() {
    const drawer = $('#detailDrawer');
    drawer.innerHTML = `
      <div class="drawer-head"><span class="drawer-title">录入接入条件</span><button class="icon-btn" id="drawerClose" aria-label="关闭"><svg><use href="#i-close"/></svg></button></div>
      <div class="drawer-body">
        <div class="notice" style="margin-bottom:16px"><svg><use href="#i-info"/></svg><div>拿不准的字段留空即可，页面会显示「未核实」，不会当成「不允许」。改动会留下历史记录，方便追溯。</div></div>
        <div class="drawer-section"><h4>基本信息</h4><div class="kv-list" style="gap:8px">
          <label class="kv"><span class="k">服务商</span><span class="v"><input id="cf_provider" placeholder="例如 LightVela" style="width:200px;padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"/></span></label>
          <label class="kv"><span class="k">怎么注册</span><span class="v"><select id="cf_signup" style="width:200px;padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"><option value="EMAIL">邮箱注册</option><option value="PHONE">手机号注册</option><option value="CARD">需要绑银行卡</option><option value="UNKNOWN">未核实</option></select></span></label>
          <label class="kv"><span class="k">免费方式</span><span class="v"><select id="cf_free" style="width:200px;padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"><option value="STANDING">长期免费额度</option><option value="TRIAL">限时试用额度</option><option value="CREDIT">赠送额度</option><option value="UNKNOWN">未核实</option></select></span></label>
          <label class="kv"><span class="k">能否转发</span><span class="v"><select id="cf_proxy" style="width:200px;padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"><option value="UNKNOWN">未核实</option><option value="YES">允许</option><option value="NO">不允许</option></select></span></label>
          <label class="kv"><span class="k">信息从哪来</span><span class="v"><select id="cf_source" style="width:200px;padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"><option value="dataset">数据集导入</option><option value="manual">人工录入</option><option value="official">官方页面核实</option></select></span></label>
          <label class="kv"><span class="k">核实日期</span><span class="v"><input id="cf_verified" placeholder="2026-11-22" style="width:200px;padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"/></span></label>
          <label class="kv"><span class="k">依据链接</span><span class="v"><input id="cf_url" placeholder="https://…（官网说明页）" style="width:200px;padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"/></span></label>
          <label class="kv"><span class="k">备注</span><span class="v"><input id="cf_note" style="width:200px;padding:5px 8px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px"/></span></label>
        </div></div>
        <button class="btn btn-primary" id="cfSubmit" style="width:100%"><svg><use href="#i-shield"/></svg>保存</button>
      </div>`;
    drawer.hidden = false; $('#drawerOverlay').hidden = false;
    $('#drawerClose').addEventListener('click', closeDrawer);
    $('#cfSubmit').addEventListener('click', () => {
      const provider = $('#cf_provider').value.trim();
      if (!provider) { toast('服务商名称必填', 'i-info'); return; }
      if ($('#cf_source').value === 'official' && (!$('#cf_url').value.trim() || !$('#cf_verified').value.trim())) {
        toast('标记「官方页面核实」必须填来源链接和核实日期', 'i-info'); return;
      }
      const payload = {
        provider, signup_method: $('#cf_signup').value.trim() || null,
        free_form: $('#cf_free').value.trim() || null,
        allow_proxy: $('#cf_proxy').value, source_type: $('#cf_source').value,
        verified_at: $('#cf_verified').value.trim() || null,
        source_url: $('#cf_url').value.trim() || null,
        note: $('#cf_note').value.trim() || null
      };
      fetch(API_BASE + '/api/catalog/entry', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
        .then(async r => { if (!r.ok) throw new Error((await r.json()).detail || 'HTTP ' + r.status); return r.json(); })
        .then(d => { closeDrawer(); toast(`已保存 ${d.provider}，同时记录了 ${d.policyHistoryRows} 条变更历史`, 'i-shield'); loadFromApi(true).then(() => viewCatalog()); })
        .catch(e => toast('保存失败：' + e.message, 'i-info'));
    });
  }

  function openDispatch() {
    if (!apiAvailable) { toast('暂时连不上服务端，无法应用配置', 'i-info'); return; }
    const drawer = $('#detailDrawer');
    drawer.innerHTML = `
      <div class="drawer-head"><span class="drawer-title">应用配置 · 预览</span><button class="icon-btn" id="drawerClose" aria-label="关闭"><svg><use href="#i-close"/></svg></button></div>
      <div class="drawer-body">
        <div class="notice" style="margin-bottom:14px"><svg><use href="#i-shield"/></svg><div>只有「允许转发」明确为「允许」的接口才会写进配置；不确定的一律跳过，避免违约。应用时会自动保留上一版，随时可以还原；密钥只写配置项名字，不会写入明文。</div></div>
        <div class="head-actions" style="margin-bottom:12px">
          <div class="segment" id="dispatchScene"><button class="is-active" data-scene="agent">Agent 榜</button><button data-scene="speed">速度榜</button><button data-scene="longterm">长期榜</button></div>
        </div>
        <div id="dispatchBody"><div class="mono" style="font-size:11px;color:var(--ink-3)">加载配置预览…</div></div>
        <div id="dispatchActions" style="margin-top:14px;display:flex;gap:8px">
          <button class="btn btn-primary" id="btnApply" style="flex:1"><svg><use href="#i-zap"/></svg>确认应用</button>
          <button class="btn btn-secondary" id="btnRollback"><svg><use href="#i-refresh"/></svg>还原到上一版</button>
        </div>
      </div>`;
    drawer.hidden = false; $('#drawerOverlay').hidden = false;
    $('#drawerClose').addEventListener('click', closeDrawer);
    let scene = 'agent';
    const load = () => {
      $('#dispatchBody').innerHTML = '<div class="mono" style="font-size:11px;color:var(--ink-3)">加载配置预览…</div>';
      apiGet('/api/config?scene=' + scene).then(c => {
        const title = `<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px"><span class="badge ${c.count ? 'ok' : 'warn'}"><span class="dot"></span>可应用 ${c.count} 个接口</span><span class="panel-sub">跳过 ${(c.excluded || []).length} 个</span></div>`;
        const excl = (c.excluded || []).length ? `<div class="notice" style="margin:0 0 10px;background:var(--warn-soft);border-color:color-mix(in srgb,var(--warn) 25%,transparent)"><svg><use href="#i-info"/></svg><div>${(c.excluded || []).slice(0, 6).map(esc).join('<br>')}${(c.excluded || []).length > 6 ? '<br>…' : ''}</div></div>` : '';
        const yaml = c.yaml ? `<pre class="mono" style="font-size:10px;line-height:1.5;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);padding:12px;overflow:auto;max-height:300px;white-space:pre-wrap">${esc(c.yaml)}</pre>` : '<div class="mono" style="font-size:11px;color:var(--ink-3)">暂时没有可应用的接口（「允许转发」还没核实，或没有符合条件的）</div>';
        $('#dispatchBody').innerHTML = title + excl + yaml;
      }).catch(e => { $('#dispatchBody').innerHTML = '<div class="mono" style="font-size:11px;color:var(--danger)">预览失败：' + esc(e.message) + '</div>'; });
    };
    $('#dispatchScene').querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
      $('#dispatchScene').querySelectorAll('button').forEach(x => x.classList.toggle('is-active', x === b));
      scene = b.dataset.scene; load();
    }));
    $('#btnApply').addEventListener('click', () => applyConfig(scene));
    $('#btnRollback').addEventListener('click', rollbackConfig);
    load();
  }
  function applyConfig(scene) {
    if (!window.confirm('确认把当前排名结果应用到你的服务？上一版会自动保留，可以还原。')) return;
    toast('正在应用…', 'i-zap');
    apiPost('/api/config/apply?scene=' + (scene || 'agent') + '&confirm=true').then(r => {
      if (r.ok) toast(`已应用 ${r.count} 个接口，上一版已保留`, 'i-zap');
      else toast('应用失败：' + (r.reason || '请稍后重试'), 'i-shield');
    }).catch(e => toast('应用失败：' + e.message, 'i-info'));
  }
  function rollbackConfig() {
    if (!window.confirm('确认还原到上一次的配置？')) return;
    apiPost('/api/config/rollback?confirm=true').then(r => {
      toast(r.ok ? '已还原到上一次的配置' : '还原失败：' + (r.reason || '没有可还原的版本'), r.ok ? 'i-zap' : 'i-info');
    }).catch(e => toast('还原失败：' + e.message, 'i-info'));
  }

  // ============================================================
  // 详情抽屉
  // ============================================================
  function openDrawer(name) {
    const e = ENDPOINTS[name], s = EP_STATS[name];
    const epRows = ROWS.filter(r => r.endpoint === name);
    const l2 = epRows.filter(r => r.level === 'L2' && r.ttft_ms != null);
    const spark = l2.map((r, i) => {
      const x = l2.length <= 1 ? 50 : i / (l2.length - 1) * 100;
      const max = Math.max(...l2.map(a => a.ttft_ms));
      const y = 82 - (r.ttft_ms / max) * 70;
      return `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    // 未接入（缺少密钥）的接口：所有"探测类"内容都是噪声，只保留配置与接入指引
    const noKey = e.hasKey === false;
    const availColor = noKey ? 'var(--ink-4)'
      : s.availability >= 80 ? 'var(--ok)' : s.availability >= 50 ? 'var(--warn)' : 'var(--danger)';
    const availVal = noKey ? '未接入' : (s.availability != null ? s.availability + '%' : '—');
    // 空状态用 HTML 而不是 SVG <text>：mini-chart 是拉伸绘制的，文字会被横向放大变形
    const chartBlock = noKey ? '' : (l2.length ? `
          <svg class="mini-chart" viewBox="0 0 100 90" preserveAspectRatio="none">
            <line x1="0" y1="82" x2="100" y2="82" stroke="var(--line)"/>
            <path d="${spark}" fill="none" stroke="${e.color}" stroke-width="1.6" vector-effect="non-scaling-stroke"/>
          </svg>` : `<div class="chart-empty">还没有合格的速度测试结果 · 点「立即检测」积累样本</div>`);

    const drawer = $('#detailDrawer');
    drawer.innerHTML = `
      <div class="drawer-head"><span class="drawer-title">接口详情</span><button class="icon-btn" id="drawerClose" aria-label="关闭"><svg><use href="#i-close"/></svg></button></div>
      <div class="drawer-body">
        <div class="drawer-hero">
          <div class="cat-logo" style="background:${e.color}">${esc(e.short)}</div>
          <div><div style="font-size:16px;font-weight:650">${esc(name)}</div><div class="mono" style="font-size:11px;color:var(--ink-3)">${esc(e.base)}</div></div>
        </div>
        ${noKey ? `<div class="notice" style="margin-bottom:16px"><svg><use href="#i-info"/></svg><div><strong>尚未接入。</strong>在下方「密钥管理」粘贴密钥即可，保存后写入服务端配置并即时生效，下一轮检测自动开始。<button class="btn btn-primary js-set-key" style="margin-left:8px;padding:4px 12px;font-size:11px">立即填写</button></div></div>` : ''}
        <div class="drawer-stats">
          <div class="drawer-stat"><div class="k">首字延迟（中位）</div><div class="v"${s.ttftMedian == null ? ' style="color:var(--ink-4)"' : ''}>${s.ttftMedian != null ? fmtMs(s.ttftMedian) : '—'}</div></div>
          <div class="drawer-stat"><div class="k">输出速度（均值）</div><div class="v"${s.tpsAvg == null ? ' style="color:var(--ink-4)"' : ''}>${s.tpsAvg != null ? fmtNum(s.tpsAvg) + ' <span style="font-size:11px;font-weight:400;color:var(--ink-3)">tokens/s</span>' : '—'}</div></div>
          <div class="drawer-stat"><div class="k">可用率</div><div class="v" style="color:${availColor}">${availVal}</div></div>
          <div class="drawer-stat"><div class="k">测试次数</div><div class="v">${s.total} 次</div></div>
        </div>
        ${noKey ? '' : `
        <div class="drawer-section">
          <h4>首字延迟变化（速度测试）</h4>
          ${chartBlock}
        </div>`}
        <div class="drawer-section"><h4>配置</h4><div class="kv-list">
          <div class="kv"><span class="k">服务商</span><span class="v">${esc(e.provider)}</span></div>
          <div class="kv"><span class="k">对接格式</span><span class="v">${esc(labelOf(PROTOCOL_LABEL, e.protocol))}</span></div>
          <div class="kv"><span class="k">默认模型</span><span class="v">${esc(e.model)}</span></div>
          ${budgetRows(e)}
          <div class="kv"><span class="k">网络</span><span class="v">${e.proxy ? '需要走代理' : '可以直连'}</span></div>
          <div class="kv"><span class="k">额度怎么查</span><span class="v">${esc(labelOf(QUOTA_LABEL, e.quota))}</span></div>
          <div class="kv"><span class="k">支持工具调用</span><span class="v">${esc(labelOf(TOOLS_LABEL, e.tools))}</span></div>
          <div class="kv"><span class="k">密钥</span><span class="v" style="color:var(--ink-3)">••••••••（不显示）</span></div>
          <div class="kv" style="border-bottom:0"><span class="k">密钥管理</span><span class="v"><button class="btn js-set-key" style="padding:4px 12px;font-size:11px">${noKey ? '填写密钥' : '更换密钥'}</button></span></div>
          <div id="keyFormSlot" hidden style="padding:10px 2px 12px;border-bottom:1px solid var(--line-soft)">
            <div style="display:flex;gap:8px">
              <input id="keyInput" type="password" autocomplete="new-password" class="mono" placeholder="粘贴密钥明文（只发送到本机服务，不会回显）" style="flex:1;min-width:0;padding:7px 9px;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);font-size:12px"/>
              <button id="keySave" class="btn btn-primary" style="padding:6px 14px;font-size:12px">保存</button>
              <button id="keyCancel" class="btn" style="padding:6px 14px;font-size:12px">取消</button>
            </div>
            <div style="font-size:11px;color:var(--ink-3);margin-top:6px">写入服务端 .env 并即时生效；页面与数据库都不会保存明文，已有密钥也不会回显。</div>
          </div>
        </div></div>
        <div class="drawer-section"><h4>实测备注</h4><div class="notice" style="margin:0"><svg><use href="#i-info"/></svg><div>${esc(e.note)}</div></div></div>
        ${noKey ? '' : `
        <div class="drawer-section"><h4>返回方式构成</h4><div class="kv-list">
          <div class="kv"><span class="k">流式</span><span class="v">${s.trueStream} 次</span></div>
          <div class="kv"><span class="k">整包</span><span class="v">${s.buffered} 次</span></div>
          <div class="kv"><span class="k">最近测试</span><span class="v">${esc(fmtTs(s.lastAt))}</span></div>
        </div></div>
        ${s.nCustom ? `
        <div class="drawer-section"><h4>输出速度 · 按测试问题分组</h4><div class="kv-list">
          <div class="kv"><span class="k">默认问题（Count from 1 to 20.）</span><span class="v">${s.tpsP50Default != null ? fmtNum(s.tpsP50Default) + ' tokens/s 中位 · ' + s.nDefault + ' 次' : '—'}</span></div>
          <div class="kv"><span class="k">自定义问题</span><span class="v" style="color:var(--accent)">${s.tpsP50Custom != null ? fmtNum(s.tpsP50Custom) + ' tokens/s 中位 · ' + s.nCustom + ' 次' : '—'}</span></div>
          <div class="cell-sub">两类问题的速度不能混着比：问题越长首字越慢、速度越低，分开看才有意义。</div>
        </div></div>` : ''}`}
        <div class="drawer-section" id="drawerModels"><h4>可用模型</h4><div class="mono" style="font-size:11px;color:var(--ink-3)">加载中…</div></div>
        <div class="drawer-section" id="drawerQuota"></div>
      </div>`;
    drawer.hidden = false; $('#drawerOverlay').hidden = false;
    $('#drawerClose').addEventListener('click', closeDrawer);
    // 密钥填写/更换（只写不读：POST /api/env-set，永不回显）
    drawer.querySelectorAll('.js-set-key').forEach(b => b.addEventListener('click', () => {
      $('#keyFormSlot').hidden = false;
      $('#keyInput').focus();
    }));
    if ($('#keyFormSlot')) {
      $('#keyCancel').addEventListener('click', () => {
        $('#keyFormSlot').hidden = true; $('#keyInput').value = '';
      });
      $('#keySave').addEventListener('click', () => {
        const v = $('#keyInput').value.trim();
        if (!v) { toast('先粘贴密钥内容', 'i-info'); return; }
        apiPost('/api/env-set', { auth_env: e.authEnv, value: v, label: name, base_url: e.baseUrl }).then(r => {
          toast(`已保存 ${r.auth_env} 并即时生效，下一轮检测自动开始`, 'i-shield');
          closeDrawer();
          loadFromApi(true).then(() => RENDERERS[currentView]());
        }).catch(err => toast('保存失败：' + err.message, 'i-info'));
      });
    }
    if (apiAvailable) loadDrawerDetail(ENDPOINTS[name].id, name);
  }

  function fmtCtx(n) {
    if (n == null) return '—';
    if (n >= 1000000) return (n / 1000000).toFixed(n % 1000000 ? 1 : 0) + 'M';
    if (n >= 1000) return Math.round(n / 1000) + 'K';
    return String(n);
  }
  // ---------- 复制工具（剪贴板 API 不可用时回退 execCommand） ----------
  function copyText(text, msg) {
    const done = () => toast(msg || '已复制', 'i-info');
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
    } else fallbackCopy(text, done);
  }
  function fallbackCopy(text, done) {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } catch (_) { toast('复制失败，请手动选择文本', 'i-info'); }
    document.body.removeChild(ta);
  }
  // 生成可直接测试的 curl 命令（密钥用占位符，绝不写入真实 Key）
  function buildCurl(baseUrl, model) {
    let url = String(baseUrl || '');
    if (!/^https?:\/\//.test(url)) url = 'https://' + url;
    url = url.replace(/\/+$/, '') + '/chat/completions';
    const q = (PROBE_PROMPT || 'Count from 1 to 20.').replace(/'/g, "'\\''");
    return `curl -X POST "${url}" \\
  -H "Authorization: Bearer <你的密钥>" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${model}",
    "messages": [{ "role": "user", "content": "${q}" }],
    "max_tokens": 128,
    "stream": true
  }'`;
  }
  // curl 浮窗（全局共享一个实例，不在卡片内展开挤布局）
  let _curlModal = null;
  function ensureCurlModal() {
    if (_curlModal && document.body.contains(_curlModal)) return _curlModal;
    _curlModal = document.createElement('div');
    _curlModal.className = 'modal-overlay';
    _curlModal.hidden = true;
    _curlModal.innerHTML = `<div class="modal-box curl-box">
      <div class="curl-head"><span>Terminal · OpenAI 兼容格式，密钥处替换为你自己的</span>
        <span style="display:flex;gap:8px;align-items:center">
          <button class="btn" id="curlModalCopy" style="padding:2px 10px;font-size:11px">Copy</button>
          <button class="icon-btn" id="curlModalClose" aria-label="关闭"><svg><use href="#i-close"/></svg></button>
        </span></div>
      <pre class="mono" id="curlModalCode"></pre></div>`;
    document.body.appendChild(_curlModal);
    _curlModal.addEventListener('click', e => { if (e.target === _curlModal) _curlModal.hidden = true; });
    $('#curlModalClose').addEventListener('click', () => { _curlModal.hidden = true; });
    $('#curlModalCopy').addEventListener('click', () => copyText($('#curlModalCode').textContent, 'curl 命令已复制'));
    return _curlModal;
  }
  function showCurlModal(text) {
    const m = ensureCurlModal();
    $('#curlModalCode').textContent = text;
    m.hidden = false;
  }

  async function loadDrawerDetail(id, label) {
    if (!id) return;
    try {
      const d = await apiGet('/api/endpoints/' + id + '/detail');
      const models = d.models || [], quotas = d.quotas || [], util = d.utilization || [];
      // 卡片行布局而非表格：模型名是不可断的长 ID，表格列宽会把它们挤成竖排字符
      const modelHtml = models.length ? `
        <div class="model-list">
        ${models.map(m => `
          <div class="model-row">
            <div style="min-width:0;flex:1">
              <div class="model-name">${esc(m.display_name || m.model_id)}</div>
              ${m.display_name && m.display_name !== m.model_id ? `<div class="model-id mono">${esc(m.model_id)}</div>` : ''}
              ${m.served_as ? `<div class="model-meta">对外显示为 ${esc(m.served_as)}</div>` : ''}
              <div class="model-meta">上下文 ${m.context_length != null ? fmtCtx(m.context_length) : '—'} · ${esc(labelOf(CTX_SOURCE_LABEL, m.context_source))} · ${m.is_chat == null ? '是否支持对话未知' : (m.is_chat ? '支持多轮对话' : '不支持多轮对话')}</div>
            </div>
            <div style="display:flex;gap:6px;flex-shrink:0;align-items:center">
              <button class="icon-btn js-copy-id" data-id="${esc(m.model_id || '')}" title="复制模型 ID" style="width:28px;height:28px"><svg><use href="#i-copy"/></svg></button>
              <button class="icon-btn js-copy-curl" data-id="${esc(m.model_id || '')}" title="查看可直接测试的 curl 命令（密钥用占位符）" style="width:28px;height:28px"><svg><use href="#i-external"/></svg></button>
            </div>
          </div>`).join('')}
        </div>
        </div>` : `<div class="mono" style="font-size:11px;color:var(--ink-3)">还没有模型清单，跑一次连通检测就会自动生成</div>`;
      const host = $('#drawerModels');
      if (host) {
        host.innerHTML = `<h4>可用模型 <span class="panel-sub">${models.length} 个</span></h4>${modelHtml}`;
        host.querySelectorAll('.js-copy-id').forEach(b => b.addEventListener('click', () => copyText(b.dataset.id, '模型 ID 已复制')));
        host.querySelectorAll('.js-copy-curl').forEach(b => b.addEventListener('click', () => {
          showCurlModal(buildCurl((d.endpoint && d.endpoint.baseUrl) || '', b.dataset.id));
        }));
      }
      const qHost = $('#drawerQuota');
      if (qHost && quotas.length) {
        qHost.innerHTML = `<h4>额度情况 <span class="panel-sub">来源如实标注；「速率限制」非可消耗额度</span></h4><div class="kv-list">
          ${quotas.slice(0, 5).map(q => `<div class="kv"><span class="k">${esc(labelOf(QUOTA_TYPE_LABEL, q.quota_type))}<span class="cell-sub">${esc(labelOf(QUOTA_LABEL, q.source))}</span></span><span class="v">${q.remaining != null ? fmtNum(q.remaining) : '—'} / ${q.total != null ? fmtNum(q.total) : '—'}${q.unit ? ' ' + esc(labelOf(UNIT_LABEL, q.unit)) : ''}</span></div>`).join('')}
        </div>`;
      }
    } catch (_) {
      const host = $('#drawerModels');
      if (host) host.innerHTML = `<h4>可用模型</h4><div class="mono" style="font-size:11px;color:var(--ink-3)">暂时读不到详情</div>`;
    }
  }
  function openRecordDrawer(r) {
    if (!r) return;
    const drawer = $('#detailDrawer');
    const isProxy = r.timing_scope === 'proxy_hop';
    const errSnippet = v => v ? `<pre class="mono" style="font-size:10px;line-height:1.5;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);padding:10px;overflow:auto;max-height:220px;white-space:pre-wrap;margin:6px 0 0">${esc(String(v).slice(0, 1200))}</pre>` : '';
    const ms = v => v != null ? fmtMs(v) : '—';
    drawer.innerHTML = `
      <div class="drawer-head"><span class="drawer-title">${esc(r.endpoint)} · ${esc(LEVEL_LABEL[r.level] || r.level || '')} · ${esc(fmtTs(r.created_at, true))}</span><button class="icon-btn" id="drawerClose" aria-label="关闭"><svg><use href="#i-close"/></svg></button></div>
      <div class="drawer-body">
        <div class="drawer-section"><h4>结果</h4><div class="kv-list">
          <div class="kv"><span class="k">状态</span><span class="v">${badge(r.status)}</span></div>
          <div class="kv"><span class="k">口径</span><span class="v">${isProxy ? '<span class="tag-mono" title="计时包含本地代理段">代理</span>' : '<span class="tag-mono" title="计时为到服务商的直连耗时">直连</span>'}</span></div>
          <div class="kv"><span class="k">返回方式</span><span class="v">${esc(labelOf(STREAM_LABEL, r.stream_mode, '—'))}</span></div>
          <div class="kv"><span class="k">模型</span><span class="v">${esc(r.model_display || r.model_id || '—')}${r.model_display && r.model_id && r.model_display !== r.model_id ? `<span class="cell-sub mono">${esc(r.model_id)}</span>` : ''}</span></div>
          <div class="kv"><span class="k">HTTP 状态</span><span class="v">${r.http_status || '—'}</span></div>
          ${r.probe_version ? `<div class="kv"><span class="k">测试代码版本</span><span class="v mono" style="font-size:11px">${esc(String(r.probe_version).slice(0, 12))}</span></div>` : ''}
        </div></div>
        <div class="drawer-section"><h4>计时分段 ${isProxy ? '<span class="panel-sub">代理口径：此段为到本地代理</span>' : ''}</h4><div class="kv-list">
          <div class="kv"><span class="k">域名解析</span><span class="v">${ms(r.dns_ms)}</span></div>
          <div class="kv"><span class="k">建立连接</span><span class="v">${ms(r.tcp_ms)}${isProxy ? ' <span class="cell-sub">本地代理</span>' : ''}</span></div>
          <div class="kv"><span class="k">加密握手</span><span class="v">${ms(r.tls_ms)}${isProxy ? ' <span class="cell-sub">本地代理</span>' : ''}</span></div>
          <div class="kv"><span class="k">首字节响应</span><span class="v">${ms(r.ttfb_ms)}</span></div>
          <div class="kv"><span class="k">首字延迟</span><span class="v">${r.stream_mode === 'true_stream' ? ms(r.ttft_ms) : 'n/a · 整包'}</span></div>
          <div class="kv"><span class="k">总耗时</span><span class="v">${ms(r.total_ms)}</span></div>
        </div></div>
        ${(r.error_kind || r.error_body) ? `<div class="drawer-section"><h4>失败诊断</h4><div class="kv-list">
          <div class="kv"><span class="k">错误分类</span><span class="v mono" style="font-size:11px">${esc(r.error_kind || '—')}</span></div>
        </div>${errSnippet(r.error_body)}</div>` : ''}
        ${r.resp_body ? `<div class="drawer-section"><h4>响应片段（脱敏）</h4>${errSnippet(r.resp_body)}</div>` : ''}
        <div class="drawer-actions" style="display:flex;gap:8px;margin-top:14px">
          <button class="btn btn-primary" style="flex:1" data-action="rerun-probe" data-ep="${esc(r.endpoint)}"><svg><use href="#i-zap"/></svg>重测此接口</button>
          <button class="btn btn-secondary" data-action="go-probes"><svg><use href="#i-activity"/></svg>查看全部记录</button>
        </div>
      </div>`;
    drawer.hidden = false; $('#drawerOverlay').hidden = false;
    $('#drawerClose').addEventListener('click', closeDrawer);
  }
  function closeDrawer() { $('#detailDrawer').hidden = true; $('#drawerOverlay').hidden = true; }

  // ============================================================
  // 命令面板
  // ============================================================
  const CMDS = [
    { group: '导航', icon: 'i-grid', label: '总览', hint: 'G O', view: 'overview' },
    { group: '导航', icon: 'i-server', label: '接口清单', hint: 'G E', view: 'endpoints' },
    { group: '导航', icon: 'i-rank', label: '排名榜单', hint: 'G R', view: 'rankings' },
    { group: '导航', icon: 'i-catalog', label: '接入条件', hint: 'G C', view: 'catalog' },
    { group: '导航', icon: 'i-activity', label: '测试记录', hint: 'G P', view: 'probes' },
    { group: '导航', icon: 'i-bell', label: '提醒中心', hint: 'G M', view: 'reminders' },
    { group: '导航', icon: 'i-sun', label: '设置', hint: 'G S', view: 'settings' },
    { group: '命令', icon: 'i-download', label: '导出测试记录（表格）', action: 'export-csv' },
    { group: '命令', icon: 'i-sun', label: '切换明暗主题', action: 'toggle-theme' },
    { group: '命令', icon: 'i-zap', label: '立即检测所有接口', action: 'run-probe' },
    { group: '命令', icon: 'i-refresh', label: '刷新数据', action: 'refresh' }
  ];
  let cmdkSel = 0, cmdkFiltered = CMDS;
  function openCmdk() {
    $('#cmdkOverlay').hidden = false; $('#cmdkInput').value = ''; cmdkSel = 0;
    renderCmdk(''); $('#cmdkList').scrollTop = 0; setTimeout(() => $('#cmdkInput').focus(), 0);
  }
  function closeCmdk() { $('#cmdkOverlay').hidden = true; }
  function renderCmdk(q) {
    const epItems = Object.keys(ENDPOINTS).map(n => ({
      group: '接口', icon: 'i-server', label: n, action: 'open-endpoint', ep: n
    }));
    const all = CMDS.concat(epItems);
    cmdkFiltered = all.filter(c => !q || c.label.toLowerCase().includes(q.toLowerCase()) || c.group.includes(q));
    let html = '', lastGroup = '';
    cmdkFiltered.forEach((c, i) => {
      if (c.group !== lastGroup) { html += `<div class="cmdk-group-label">${c.group}</div>`; lastGroup = c.group; }
      html += `<div class="cmdk-item ${i === cmdkSel ? 'is-sel' : ''}" data-i="${i}"><svg><use href="#${c.icon}"/></svg><span>${c.label}</span><span class="ci-hint">${c.hint || ''}</span></div>`;
    });
    $('#cmdkList').innerHTML = html || `<div class="empty-state" style="padding:28px">无匹配命令</div>`;
    $('#cmdkList').querySelectorAll('.cmdk-item').forEach(el => {
      el.addEventListener('mouseenter', () => { cmdkSel = +el.dataset.i; markCmdk(); });
      el.addEventListener('click', () => runCmdk(+el.dataset.i));
    });
  }
  function markCmdk() { $('#cmdkList').querySelectorAll('.cmdk-item').forEach((el, i) => el.classList.toggle('is-sel', i === cmdkSel)); }
  function runCmdk(i) {
    const c = cmdkFiltered[i]; if (!c) return;
    closeCmdk();
    if (c.view) navigate(c.view);
    else if (c.action === 'open-endpoint') { navigate('endpoints'); setTimeout(() => openDrawer(c.ep), 60); }
    else if (c.action === 'toggle-theme') toggleTheme();
    else handleAction(c.action);
  }

  // ============================================================
  // 导航 / 主题 / Toast / 动作
  // ============================================================
  const TITLES = { overview: '总览', endpoints: '接口清单', probes: '测试记录', rankings: '排名榜单', reminders: '提醒中心', catalog: '接入条件', settings: '设置' };
  const RENDERERS = { overview: viewOverview, endpoints: viewEndpoints, probes: viewProbes, rankings: viewRankings, reminders: viewReminders, catalog: viewCatalog, settings: viewSettings };
  function navigate(view) {
    if (!RENDERERS[view]) view = 'overview';
    currentView = view;
    location.hash = view;
    document.querySelectorAll('.nav-item[data-view]').forEach(a => a.classList.toggle('is-active', a.dataset.view === view));
    $('#crumbCurrent').textContent = TITLES[view];
    RENDERERS[view]();
    closeNav(); content.scrollTop = 0; window.scrollTo(0, 0);
  }
  function toggleTheme() {
    const root = document.documentElement;
    const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    localStorage.setItem('tokriva-theme', next);
    $('#themeToggle').querySelector('use').setAttribute('href', next === 'dark' ? '#i-moon' : '#i-sun');
  }
  function toast(msg, icon) {
    const el = document.createElement('div');
    el.className = 'toast';
    el.innerHTML = `<svg><use href="#${icon || 'i-info'}"/></svg><span>${esc(msg)}</span>`;
    $('#toastStack').appendChild(el);
    setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 220); }, 2800);
  }
  function handleAction(action, el) {
    if (action === 'export-csv') exportCSV();
    else if (action === 'go-probes') navigate('probes');
    else if (action === 'add-endpoint') {
      if (!apiAvailable) { toast('暂时连不上服务端，无法保存', 'i-info'); return; }
      openEndpointForm();
    }
    else if (action === 'toast-import') openCatalogForm();
    else if (action === 'toast-dispatch') openDispatch();
    else if (action === 'config-apply') applyConfig();
    else if (action === 'config-rollback') rollbackConfig();
    else if (action === 'catalog-add') openCatalogForm();
    else if (action === 'go-reminders') navigate('reminders');
    else if (action === 'run-probe') runProbeNow();
    else if (action === 'rerun-probe') runProbeNow(el && el.dataset.ep);
    else if (action === 'refresh') loadFromApi(false);
  }
  // 手动触发一轮检测：异步启动 + 轮询数据库最新时间，完成后自动刷新
  let probeRunning = false;
  async function runProbeNow(endpoint) {
    if (!apiAvailable) { toast('暂时连不上服务端，无法发起检测', 'i-info'); return; }
    if (probeRunning) { toast('已有一轮检测在进行中，请稍等', 'i-info'); return; }
    probeRunning = true;
    const startedAt = Date.now();
    const baseline = (await apiGet('/api/health').catch(() => ({}))).latestProbeAt;
    const total = endpoint ? 1 : Object.keys(ENDPOINTS).length;
    setProbeButtons(true, '检测中… 0s');
    // 进度：每秒更新用时；每 5 秒轮询新记录，按已产生新记录的接口数显示 N/M
    const tick = setInterval(() => {
      const s = Math.round((Date.now() - startedAt) / 1000);
      setProbeButtons(true, `检测中… ${s}s`);
    }, 1000);
    try {
      await apiPost('/api/probe/run', { async_run: true, endpoint: endpoint || null });
      const probeStartLabel = endpoint ? `重测 ${endpoint}` : '检测全部接口';
      const progress = async () => {
        const fresh = await apiGet('/api/probes?limit=60').catch(() => ({ items: [] }));
        const done = new Set((fresh.items || [])
          .filter(r => r.created_at > baseline).map(r => r.endpoint)).size;
        return Math.min(done, total);
      };
      const timer = setInterval(async () => {
        try {
          const h = await apiGet('/api/health');
          const elapsed = Math.round((Date.now() - startedAt) / 1000);
          if (h.latestProbeAt && h.latestProbeAt !== baseline) {
            const doneN = await progress();
            clearInterval(timer); clearInterval(tick);
            probeRunning = false;
            setProbeButtons(false);
            await loadFromApi(true);
            toast(endpoint
              ? `重测 ${endpoint} 完成 · 用时 ${elapsed}s`
              : `检测完成 · ${doneN}/${total} 个接口 · 用时 ${elapsed}s`, 'i-shield');
          } else if (Date.now() - startedAt > 240000) {
            clearInterval(timer); clearInterval(tick);
            probeRunning = false;
            setProbeButtons(false);
            toast('检测超过 4 分钟仍未结束（可能在等慢接口超时），稍后点「刷新数据」查看', 'i-info');
          } else {
            const doneN = await progress();
            setProbeButtons(true, `检测中… ${doneN}/${total} · ${elapsed}s`);
          }
        } catch (_) { /* 轮询失败忽略，等下一轮 */ }
      }, 5000);
    } catch (e) {
      clearInterval(tick);
      probeRunning = false;
      setProbeButtons(false);
      // 409 = 后端已有新一轮在跑（并发锁挡住叠跑）：这是「稍等」不是「出错」。
      if (e && e.status === 409) toast(e.message || '已有一轮检测在进行中，请稍等', 'i-info');
      else toast('发起检测失败：' + e.message, 'i-info');
    }
  }
  function setProbeButtons(running, label) {
    document.querySelectorAll('[data-action="run-probe"], [data-action="rerun-probe"]').forEach(b => {
      b.disabled = running;
      if (running) {
        b.innerHTML = `<svg><use href="#i-refresh"/></svg>${esc(label || '检测中…')}`;
      } else {
        b.innerHTML = b.dataset.action === 'rerun-probe'
          ? '<svg><use href="#i-zap"/></svg>重测此接口'
          : '<svg><use href="#i-zap"/></svg>立即检测';
      }
    });
  }
  function exportCSV() {
    const _L = { L0: '连通检测', L1: '轻量测试', L2: '速度测试' };
    const _S = { HEALTHY: '健康', DEGRADED: '降级', AUTH_FAILED: '鉴权失败', QUOTA_EXHAUSTED: '额度耗尽', RATE_LIMITED: '限流', MODEL_GONE: '模型下线', ENDPOINT_ERROR: '服务端错误', NETWORK_ERROR: '网络错误', RESPONSE_INVALID: '响应无效', REASONING_STARVED: '推理占满', NOT_CONFIGURED: '未接入' };
    const _M = { true_stream: '流式', buffered: '整包', single: '单次返回' };
    const row = r => [
      fmtTs(r.created_at, true), r.endpoint,
      _L[r.level] || r.level || '', _S[r.status] || r.status || '',
      r.http_status ?? '', r.dns_ms ?? '', r.tcp_ms ?? '', r.tls_ms ?? '', r.ttfb_ms ?? '',
      r.ttft_ms ?? '', r.output_tokens ?? '', r.tps ?? '', r.total_ms ?? '',
      _M[r.stream_mode] || r.stream_mode || '',
      r.model_display || r.model_id || '', r.model_id || '',
      r.model_status === 'MISSING' ? '已下线' : (r.model_status ? '在售' : '')
    ];
    const write = list => {
      const head = ['时间（北京时间）', '接口', '测试类型', '状态', 'HTTP状态码', '域名解析(ms)', '建立连接(ms)', '加密握手(ms)', '首字节响应(ms)', '首字延迟(ms)', '输出token数', '输出速度(tokens/s)', '总耗时(ms)', '返回方式', '模型', '模型ID', '模型状态'];
      const lines = [head.join(',')].concat(list.map(r => row(r).map(v => `"${String(v == null ? '' : v).replace(/"/g, '""')}"`).join(',')));
      const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
      const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'test_records.csv'; a.click();
      URL.revokeObjectURL(a.href);
    };
    if (apiAvailable) {
      // 跟随页面当前筛选（级别/状态/关键词）
      const st = viewState.probes, params = new URLSearchParams();
      if (st.level !== 'ALL') params.set('level', st.level);
      if (st.status !== 'ALL') params.set('status', st.status);
      if (st.q) params.set('q', st.q);
      window.location.href = API_BASE + '/api/probes/export' + (params.toString() ? '?' + params.toString() : '');
      toast('正在导出表格（含当前筛选）', 'i-download');
      return;
    }
    write(ROWS);
    toast('已导出 ' + ROWS.length + ' 条测试记录（本地示例数据）', 'i-download');
  }
  function openNav() { document.querySelector('.app-shell').classList.add('nav-open'); $('#scrim').hidden = false; }
  function closeNav() { document.querySelector('.app-shell').classList.remove('nav-open'); $('#scrim').hidden = true; }
  function setSidebarCollapsed(collapsed, persist = true) {
    document.querySelector('.app-shell').classList.toggle('sidebar-collapsed', collapsed);
    if (persist) localStorage.setItem('tokriva-sidebar-collapsed', collapsed ? '1' : '0');
  }

  // ============================================================
  // 事件绑定
  // ============================================================
  function bindGlobal() {
    document.querySelectorAll('.nav-item[data-view]').forEach(a => a.addEventListener('click', e => { e.preventDefault(); navigate(a.dataset.view); }));
    document.querySelectorAll('[data-nav]').forEach(a => a.addEventListener('click', e => { e.preventDefault(); navigate(a.dataset.nav); }));
    document.addEventListener('click', e => {
      const el = e.target.closest('[data-action]'); if (el) handleAction(el.dataset.action, el);
    });
    $('#hamburger').addEventListener('click', openNav);
    $('#sidebarClose').addEventListener('click', closeNav);
    $('#scrim').addEventListener('click', closeNav);
    $('#sidebarCollapse').addEventListener('click', () => {
      const shell = document.querySelector('.app-shell');
      setSidebarCollapsed(!shell.classList.contains('sidebar-collapsed'));
    });
    // 折叠态 tooltip 文案：从导航文字与按钮标题取
    document.querySelectorAll('.nav-item[data-view], .nav-item[data-external]').forEach(a => {
      const text = a.querySelector('span:not(.nav-count)');
      if (text) a.dataset.tip = text.textContent.trim();
    });
    $('#themeToggle').addEventListener('click', toggleTheme);
    $('#refreshBtn').addEventListener('click', () => handleAction('refresh'));
  document.addEventListener('click', e => {
      const rf = e.target.closest('[data-rfilter]');
      if (rf) { REMINDER_FILTER = rf.dataset.rfilter; viewReminders(); return; }
      const rack = e.target.closest('[data-rack]');
      if (rack) {
        const key = rack.dataset.key, action = rack.dataset.rack;
        apiPost('/api/reminders/ack', { key, action, days: 1 }).then(() => {
          toast(action === 'mute' ? '已静音，一年内不再提示'
              : action === 'snooze' ? '已延后 1 天' : '已确认', 'i-bell');
          apiGet('/api/reminders').then(d => { applyReminderPayload(d); viewReminders(); });
        }).catch(err => toast('操作失败：' + err.message, 'i-info'));
      }
      const cfg = e.target.closest('[data-action="go-configure"]');
      if (cfg) {
        closeDrawer();
        if (cfg.dataset.ep) toast(`请在服务端 .env 中配置 ${cfg.dataset.ep} 对应的密钥环境变量`, 'i-info');
        openEndpointForm();
      }
    });
    $('#notifyBtn').addEventListener('click', () => {
      const actionableN = REMINDER_META.actionable;
      if (actionableN) {
        toast(`有 ${actionableN} 条已确认的提醒待处理`, 'i-bell');
        navigate('reminders');
      } else if (REMINDER_META.summary.pending) {
        toast(`有 ${REMINDER_META.summary.pending} 条提醒正在观察确认`, 'i-bell');
        navigate('reminders');
      } else {
        toast('暂无新的政策变更或额度告警', 'i-bell');
      }
    });
    $('#drawerOverlay').addEventListener('click', closeDrawer);
    $('#searchTrigger').addEventListener('click', openCmdk);
    $('#cmdkOverlay').addEventListener('click', e => { if (e.target === $('#cmdkOverlay')) closeCmdk(); });
    $('#cmdkInput').addEventListener('input', e => { cmdkSel = 0; renderCmdk(e.target.value); });
    $('#cmdkInput').addEventListener('keydown', e => {
      if (e.key === 'ArrowDown') { e.preventDefault(); cmdkSel = Math.min(cmdkSel + 1, cmdkFiltered.length - 1); markCmdk(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); cmdkSel = Math.max(cmdkSel - 1, 0); markCmdk(); }
      else if (e.key === 'Enter') { e.preventDefault(); runCmdk(cmdkSel); }
    });
    document.addEventListener('keydown', e => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openCmdk(); }
      if (e.key === 'Escape') { closeCmdk(); closeDrawer(); closeNav(); }
    });
    window.addEventListener('hashchange', () => { const v = location.hash.slice(1); if (v && v !== currentView) navigate(v); });
  }

  // ---------- 平台适配：快捷键提示 ----------
  {
    const kbd = $('#kbdShortcut');
    if (kbd && !/Mac|iPhone|iPad/.test(navigator.platform || '')) kbd.textContent = 'Ctrl K';
  }

  // ---------- 启动 ----------
  // 只读快照模式：给 body 打标记（样式表据此隐藏写操作入口），并兜底提示
  if (READ_ONLY) {
    document.body.dataset.readonly = '1';
    document.querySelector('.sync-top > span:last-child').textContent = '只读快照';
    window.addEventListener('unhandledrejection', (e) => {
      if (e.reason && e.reason.readOnly) {
        e.preventDefault();
        toast(READ_ONLY_MSG, 'i-info');
      }
    });
  }
  if (localStorage.getItem('tokriva-theme') === 'dark') {
    document.documentElement.dataset.theme = 'dark';
    $('#themeToggle').querySelector('use').setAttribute('href', '#i-moon');
  }
  if (localStorage.getItem('tokriva-sidebar-collapsed') === '1') setSidebarCollapsed(true, false);
  $('#navCountProbes').textContent = ROWS.length;
  $('#navCountEndpoints').textContent = Object.keys(ENDPOINTS).length;
  bindGlobal();
  const initial = location.hash.slice(1);
  navigate(RENDERERS[initial] ? initial : 'overview');
  // 快照先渲染，随后异步拉取后端真实数据覆盖（失败则静默回退）
  loadFromApi(true);
})();
