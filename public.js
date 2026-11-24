/* ============================================================
   Tokriva · 公开站前端（只读）
   ------------------------------------------------------------
   这是一个**独立、单一用途**的只读页面，只消费 Public Snapshot
   Builder 产出的 `snapshot.json`（正向白名单契约）。

   设计依据：
   - 报告 §9.2  页面信息架构（单页只读看板）
   - 报告 §9.5  必须展示的说明（页脚免责声明）
   - 报告 §10.3 页面视图白名单（Overview/Rankings/Providers/History/Methodology）
   - 报告 §10.4 API 适配层与四个错误码
   - 报告 §10.5 **禁止回退到演示数据** —— 快照缺失/版本不符时显式报「数据暂不可用」
   - 报告 §10.6 Overview 窗口严格匹配（缺 key 即报错，不静默回退）
   - 报告 §15.2 需公开的测量口径 / §16.2 数据新鲜度阈值

   ⚠️ 本文件**不加载** `data.js`（管理端兜底演示数据）、也不加载 `app.js`。
      数据源只有 `snapshot.json` 一处；拿不到就报错，绝不伪造。
   ============================================================ */
(function () {
  'use strict';

  /* ================= 契约与常量 ================= */

  var SUPPORTED_SCHEMA = 1;
  var REPO_URL = 'https://github.com/ariesfun/tokriva';
  var PROJECT = 'Tokriva';

  // 快照位置候选（按序尝试）。默认期望随站点发布为 `data/snapshot.json`（报告 §9.1）。
  var SNAPSHOT_CANDIDATES = [window.TOKRIVA_SNAPSHOT_URL, './data/snapshot.json', './snapshot.json']
    .filter(function (u) { return typeof u === 'string' && u.length; });

  // 窗口选项（UI 标签 ↔ 快照 key）。实际可用窗口以 `meta.windows` 为准，此处只给标签。
  var WINDOW_LABELS = { '1': '近 1 小时', '6': '近 6 小时', '24': '近 24 小时', '168': '近 7 天', '720': '近 30 天', 'allTime': '全部历史' };

  // 公开视图白名单（报告 §10.3）
  var VIEWS = {
    overview: { label: '总览', title: '总览' },
    rankings: { label: '榜单', title: '榜单' },
    providers: { label: '接口', title: '接口与门槛' },
    history: { label: '历史', title: '探测历史' },
    methodology: { label: '方法说明', title: '方法说明' }
  };

  // 明确禁止进入的视图 / 数据路径（报告 §10.3 的「禁止」列）
  var FORBIDDEN = {
    settings: 1, reminders: 1, scheduler: 1, schedule: 1, gateway: 1, config: 1,
    'endpoint-admin': 1, 'quota-admin': 1, debug: 1, health: 1, quota: 1, endpoints_admin: 1
  };

  var LEVEL_LABELS = { L0: 'L0 连通', L2: 'L2 生成' };

  var STATE_MAP = {
    ok: { label: '正常', cls: 'ok' },
    warn: { label: '波动', cls: 'warn' },
    degraded: { label: '降级', cls: 'warn' },
    limited: { label: '限流', cls: 'warn' },
    fail: { label: '失败', cls: 'danger' },
    error: { label: '错误', cls: 'danger' },
    down: { label: '不可用', cls: 'danger' },
    skip: { label: '未测', cls: 'plain' },
    skipped: { label: '未测', cls: 'plain' },
    pending: { label: '待定', cls: 'plain' },
    // 「本机没配凭据」对访客只意味着「该接口本轮不构成有效观测」。
    // 公开契约里后端已把 no_key / NOT_CONFIGURED 统一改称 not_monitored。
    not_monitored: { label: '未纳入观测', cls: 'plain' },
    unknown: { label: '未知', cls: 'plain' }
  };

  //: 最终判定的**访客措辞兜底**。后端已改写过 `final_label`，这里只防老快照。
  var FINAL_LABEL_FALLBACK = {
    NOT_MONITORED: '未纳入观测',
    NO_KEY: '未纳入观测',
    AUTH_FAILED: '未纳入观测'
  };

  var STATUS_MAP = {
    HEALTHY: { label: '正常', cls: 'ok' },
    RATE_LIMITED: { label: '限流', cls: 'warn' },
    REASONING_STARVED: { label: '推理耗尽', cls: 'info' },
    // 公开契约里这一态的键名是 `NOT_MONITORED`（后端把 `NOT_CONFIGURED` 改名了）；
    // 老键名保留作兜底，文案一律是中性词，不出现在访客面前。
    NOT_MONITORED: { label: '未纳入观测', cls: 'plain' },
    NOT_CONFIGURED: { label: '未纳入观测', cls: 'plain' },
    NETWORK_ERROR: { label: '网络错误', cls: 'danger' },
    ENDPOINT_ERROR: { label: '接口错误', cls: 'danger' },
    RESPONSE_INVALID: { label: '响应异常', cls: 'danger' }
  };

  // 少量枚举的中文映射（缺项一律回退原文，不臆造）
  var TR = {
    kind: { model_api: '模型 API', serverless: 'Serverless', chat: '对话接口' },
    signup_method: { EMAIL: '邮箱', PHONE: '手机号', GITHUB: 'GitHub', GOOGLE: 'Google', OAUTH: 'OAuth', NONE: '无需注册' },
    free_form: { STANDING: '长期免费', TRIAL: '限时试用', CREDIT: '赠送额度', RATE_FREE: '限速免费' },
    confidence: { CONFIRMED: '已确认', LIKELY: '较可信', UNVERIFIED: '未核实', UNKNOWN: '未知' },
    quotaSource: { MANUAL_REQUIRED: '待录入', HEADER: '响应头抓取', API: '接口查询', MANUAL: '手工录入', NONE: '未采集' }
  };

  /* ================= 错误码（报告 §10.4） ================= */

  function publicError(code, detail) {
    var e = new Error(detail ? code + ': ' + detail : code);
    e.code = code;
    e.detail = detail || '';
    return e;
  }

  /* ================= 状态 ================= */

  var state = {
    snapshot: null,
    view: 'overview',
    params: {},
    loadState: 'loading', // loading | ready | error
    error: null
  };

  /* ================= DOM 快捷方式 ================= */

  function $(sel) { return document.querySelector(sel); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ================= 格式化 ================= */

  function isNum(v) { return typeof v === 'number' && isFinite(v); }

  function dash(v, fb) { return (v == null || v === '') ? (fb == null ? '—' : fb) : v; }

  function fmtInt(v) {
    if (!isNum(v)) return '—';
    return v.toLocaleString('zh-CN');
  }

  function fmtNum(v, d) {
    if (!isNum(v)) return '—';
    d = d == null ? 0 : d;
    return v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function fmtPct(v, d) {
    if (!isNum(v)) return '—';
    return fmtNum(v, d == null ? 1 : d) + '%';
  }

  function fmtMs(v) {
    if (!isNum(v)) return '—';
    if (v >= 1000) return fmtNum(v / 1000, 2) + ' s';
    if (v >= 100) return fmtNum(v, 0) + ' ms';
    return fmtNum(v, 1) + ' ms';
  }

  function fmtTps(v) {
    if (!isNum(v)) return '—';
    return fmtNum(v, 1) + ' tok/s';
  }

  function fmtTs(ts) {
    if (!ts) return '—';
    var s = String(ts).replace('T', ' ');
    // 只保留到分钟（秒对展示无意义，且噪声大）
    return s.length >= 16 ? s.slice(0, 16) : s;
  }

  // 解析快照里的本地时间串（"YYYY-MM-DD HH:MM:SS"，无时区）。
  // ⚠️ **已废弃，勿再新增调用**：本项目只认**一个时间帧**（帧 B，见 `parseUtc8Ts`）。
  // 这里曾经按**浏览器本地时间**解析，与 UTC+8 口径并列存在——正是「多套时间帧并存」
  // 的温床（时间帧不一致的那次线上事故就是这么来的）。它唯一的调用处只取**差值**
  // （相邻探测间隔），差值本身与帧无关，故直接改用 `parseUtc8Ts` 即可，行为不变。
  // 保留此注释是为了让后来者知道：**别在这里重建第二个解析器**。

  // 数据新鲜度（报告 §16.2：≤90min fresh / 90–180 stale / >180 expired）
  // 项目时间口径**固定 Asia/Shanghai（UTC+8）**：库里的时间戳都是北京时间墙上时间，
  // 前端不做跨时区换算（见 docs/public-data-policy.md §2.8 的 timezone 字段）。
  //
  // 因此新鲜度**必须**按 UTC+8 的当前时刻算，而不是访问者本机时钟——否则海外访客
  // 会看到他所在时区与北京时间相差几小时，于是「才更新过的数据」被显示成「已过期」。
  // 时间帧约定（**整段一起改**，别只动一处）：
  // 库里的时间戳是「北京时间墙上时间」且不带时区。为了能放进 `Date` 做减法，
  // 统一映射到帧 B ——「把北京墙上时间当作 UTC 来写」的毫秒数：
  //     B 满足  new Date(B).toISOString() === 北京墙上时间串
  // 于是两个方向各有明确写法：
  //     字符串 → B ：按 UTC 原样解析（**不加任何偏移**，串本身已是墙上时间）
  //     当前时刻 → B ：Date.now() + 8h（**与运行机器时区无关**）
  // 曾经两处都写反了：`parseUtc8Ts` 多加了 8h，`nowUtc8Ms` 又掺进
  // `getTimezoneOffset()`，结果帧不一致，任何 16:00 之后的当天数据都会被算成「明天」。
  var UTC8_OFFSET_MS = 8 * 3600 * 1000;

  function nowUtc8Ms() {
    return Date.now() + UTC8_OFFSET_MS;
  }

  function parseUtc8Ts(ts) {
    if (!ts) return null;
    var d = new Date(String(ts).replace(' ', 'T') + 'Z');
    return isNaN(d.getTime()) ? null : d.getTime();
  }

  function freshnessOf(dataAsOf) {
    var ms = parseUtc8Ts(dataAsOf);
    if (ms == null) return { state: 'unknown', label: '暂无可用数据', note: '快照未提供数据时间' };
    var mins = (nowUtc8Ms() - ms) / 60000;
    var when = '数据截至 ' + fmtTs(dataAsOf) + '（北京时间）';
    if (mins < 0) return { state: 'fresh', label: '数据更新正常', note: when + '，晚于当前时刻' };
    if (mins < 1) return { state: 'fresh', label: '数据更新正常', note: when + '（刚刚）' };
    if (mins <= 90) return { state: 'fresh', label: '数据更新正常', note: when + '，约 ' + Math.round(mins) + ' 分钟前' };
    if (mins <= 180) return { state: 'stale', label: '数据可能延迟', note: when + '，约 ' + Math.round(mins) + ' 分钟前' };
    return { state: 'expired', label: '数据已过期', note: when + '，约 ' + Math.round(mins / 60) + ' 小时前，请谨慎参考' };
  }

  /* ================= 数据层（报告 §10.4 适配层） ================= */

  function apiGet(path, params) {
    var snap = state.snapshot;
    if (!snap) throw publicError('PUBLIC_DATA_NOT_FOUND', '快照尚未加载');
    params = params || {};
    switch (path) {
      case 'meta': return snap.meta;
      case 'endpoints': return snap.endpoints;
      case 'catalog': return snap.catalog;
      case 'probes': return snap.probes;
      case 'overview': {
        // 严格匹配：缺 key 直接报错，不回退（报告 §10.6）
        var w = String(params.window == null ? '24' : params.window);
        var ov = snap.overview && snap.overview[w];
        if (!ov) throw publicError('PUBLIC_DATA_NOT_FOUND', 'overview window=' + w);
        return ov;
      }
      case 'rankings': {
        var s = params.scene;
        var rk = s ? (snap.rankings && snap.rankings[s]) : null;
        if (!rk) throw publicError('PUBLIC_DATA_NOT_FOUND', 'rankings scene=' + s);
        return rk;
      }
      default:
        if (FORBIDDEN[path]) throw publicError('PUBLIC_VIEW_FORBIDDEN', path);
        throw publicError('PUBLIC_DATA_NOT_FOUND', path);
    }
  }

  function apiPost() {
    throw publicError('PUBLIC_READ_ONLY', '公开站不支持写操作');
  }
  window.apiGet = apiGet;   // 供调试与后续复用（与报告 §10.4 的适配层同名）
  window.apiPost = apiPost;

  async function loadSnapshot() {
    var lastErr = null;
    for (var i = 0; i < SNAPSHOT_CANDIDATES.length; i++) {
      var url = SNAPSHOT_CANDIDATES[i];
      try {
        var res = await fetch(url, { cache: 'no-cache' });
        if (!res.ok) { lastErr = publicError('PUBLIC_DATA_NOT_FOUND', url + ' → HTTP ' + res.status); continue; }
        var json;
        try { json = await res.json(); }
        catch (pe) { lastErr = publicError('PUBLIC_DATA_NOT_FOUND', url + ' → JSON 解析失败'); continue; }

        if (json == null || json.schemaVersion == null) {
          lastErr = publicError('PUBLIC_SCHEMA_UNSUPPORTED', url + ' → 缺少 schemaVersion，非公开契约');
          continue;
        }
        if (Number(json.schemaVersion) !== SUPPORTED_SCHEMA) {
          lastErr = publicError('PUBLIC_SCHEMA_UNSUPPORTED',
            'schemaVersion=' + json.schemaVersion + '，本站支持 ' + SUPPORTED_SCHEMA);
          continue;
        }
        if (!json.meta || !json.overview || !json.rankings) {
          lastErr = publicError('PUBLIC_DATA_NOT_FOUND', url + ' → 契约结构不完整');
          continue;
        }
        return json;
      } catch (e) {
        lastErr = publicError('PUBLIC_DATA_NOT_FOUND', url + ' → ' + (e && e.message ? e.message : '加载失败'));
      }
    }
    throw lastErr || publicError('PUBLIC_DATA_NOT_FOUND', '未找到公开快照');
  }

  /* ================= 片段构造器 ================= */

  function badge(label, cls, dot) {
    return '<span class="badge ' + esc(cls || 'plain') + '">' +
      (dot === false ? '' : '<span class="dot"></span>') + esc(label) + '</span>';
  }

  function stateBadge(s) {
    if (!s) return badge('—', 'plain', false);
    var m = STATE_MAP[String(s).toLowerCase()] || { label: String(s), cls: 'plain' };
    return badge(m.label, m.cls);
  }

  /**
   * 最终判定的展示文案。
   *
   * 后端已经把凭据态改写成「未纳入观测」（`final_label`），但**前端也要守一道**：
   * 老快照或后端回归时，`NO_KEY` / `AUTH_FAILED` 会带着运维措辞漏到访客面前，
   * 所以这里按 `final` 再兜底一次，而不是无脑相信 `final_label`。
   */
  function finalLabel(row) {
    var raw = String((row && row.final) || '').toUpperCase();
    if (FINAL_LABEL_FALLBACK[raw]) return FINAL_LABEL_FALLBACK[raw];
    return dash(row && row.final_label, dash(row && row.final));
  }

  function statusBadge(s) {
    if (!s) return badge('—', 'plain', false);
    var m = STATUS_MAP[s] || { label: String(s), cls: 'plain' };
    return badge(m.label, m.cls);
  }

  function tr(field, value) {
    if (value == null || value === '') return '—';
    var m = TR[field];
    return (m && m[value] != null) ? m[value] : String(value);
  }

  function boolBadge(v) {
    if (v === true) return badge('是', 'info');
    if (v === false) return badge('否', 'plain');
    return badge('未知', 'plain', false);
  }

  /* ================= 加载骨架 ================= */

  function skeleton() {
    var rows = '';
    for (var i = 0; i < 4; i++) rows += '<div class="skeleton"></div>';
    return '<div class="card"><div class="loading-rows">' + rows + '</div></div>';
  }

  /* ================= 错误呈现（禁止回退演示数据） ================= */

  function renderNotice(code, title, lines) {
    var body = (lines || []).map(function (l) { return '<p>' + l + '</p>'; }).join('');
    return '<div class="notice error">' +
      '<svg class="n-ico"><use href="#i-alert"/></svg>' +
      '<div><h3>' + esc(title) + '</h3>' + body +
      '<p class="muted small">错误码 <code>' + esc(code) + '</code>　·　本页为只读镜像，不会回退到演示数据。</p>' +
      '</div></div>';
  }

  function renderLoadError(err) {
    var code = (err && err.code) || 'PUBLIC_DATA_NOT_FOUND';
    var detail = (err && err.detail) ? esc(err.detail) : '';
    return '<section class="section">' +
      renderNotice(code, '数据暂不可用', [
        '公开快照加载失败' + (detail ? '：' + detail : '。'),
        '这可能是因为：快照尚未发布、路径不正确，或快照版本与本站不兼容。请稍后重试。'
      ]) +
      '<div class="card card-pad" style="margin-top:12px"><p class="muted small">尝试过的路径：<code>' +
      esc(SNAPSHOT_CANDIDATES.join('</code>、<code>')) + '</code></p></div>' +
      '</section>';
  }

  function renderForbiddenView(view) {
    var isKnown = FORBIDDEN[view] ? true : false;
    return '<section class="section">' +
      renderNotice('PUBLIC_VIEW_FORBIDDEN', '该页面不属于公开视图', [
        '请求的视图 <code>' + esc(view) + '</code> ' + (isKnown ? '属于管理端功能，' : '不存在，') + '公开站不提供。',
        '公开站仅开放：' + Object.keys(VIEWS).map(function (k) { return VIEWS[k].label; }).join('、') + '。'
      ]) +
      '<div class="card card-pad" style="margin-top:12px"><button class="tab is-active" data-nav="overview">返回总览</button></div>' +
      '</section>';
  }

  /* ================= 视图：总览 ================= */

  function windowOptions() {
    var meta = state.snapshot.meta || {};
    var list = Array.isArray(meta.windows) ? meta.windows.slice() : [];
    if (!list.length) list = [1, 6, 24, 168, 720];
    var keys = list.map(function (w) { return String(w); });
    if (state.snapshot.overview && state.snapshot.overview.allTime) keys.push('allTime');
    return keys;
  }

  function defaultWindow() {
    var keys = windowOptions();
    return keys.indexOf('24') >= 0 ? '24' : keys[0];
  }

  function matrixRowHtml(row) {
    var layers = row.layers || {};
    var l1 = layers.l1 || {};
    var l2 = layers.l2 || {};
    var l3 = layers.l3 || {};
    var models = Array.isArray(row.models) ? row.models : [];
    var samples = models.reduce(function (a, m) { return a + (isNum(m.samples) ? m.samples : 0); }, 0);

    var avail = !l2.skipped && isNum(l2.availability)
      ? fmtPct(l2.availability, 1) + ' <span class="muted small nowrap">(' + fmtInt(l2.healthy) + '/' + fmtInt(l2.denom) + ')</span>'
      : (l2.skipped ? '<span class="muted">未测</span>' : '<span class="muted">—</span>');

    var lat = (!l3.skipped && isNum(l3.ttft_p50))
      ? fmtMs(l3.ttft_p50) + ' <span class="muted small nowrap">TTFT</span>'
      : '<span class="muted">—</span>';

    var modelCell = models.length
      ? esc(models[0].model) + (models.length > 1 ? ' 等 ' + models.length + ' 个' : '')
      : '—';

    return '<tr>' +
      '<td class="ep-cell"><strong>' + esc(row.endpoint) + '</strong><div class="muted small">' + esc(dash(row.provider)) + '</div></td>' +
      '<td>' + badge(dash(row.path), 'plain', false) + '</td>' +
      '<td>' + stateBadge(l1.state) + (isNum(l1.consec) && l1.consec > 0 ? ' <span class="muted small">×' + l1.consec + '</span>' : '') + '</td>' +
      '<td class="num">' + avail + '</td>' +
      '<td class="num">' + lat + '</td>' +
      '<td class="num">' + (isNum(l3.tps_p50) ? fmtTps(l3.tps_p50) : '<span class="muted">—</span>') + '</td>' +
      '<td>' + badge(finalLabel(row), severityCls(row.severity)) + '</td>' +
      '<td class="num">' + (samples ? fmtInt(samples) : '<span class="muted">—</span>') + '</td>' +
      '<td class="muted small" style="max-width:240px">' + modelCell + '</td>' +
      '</tr>';
  }

  function severityCls(sev) {
    switch (String(sev || '').toLowerCase()) {
      case 'ok': return 'ok';
      case 'warn': return 'warn';
      case 'danger': case 'critical': return 'danger';
      case 'info': return 'info';
      default: return 'plain';
    }
  }

  function renderOverview() {
    var win = String(state.params.window || defaultWindow());
    var ov;
    try { ov = apiGet('overview', { window: win }); }
    catch (e) { return renderNotice(e.code, '数据暂不可用', ['所选时间窗口 <code>' + esc(win) + '</code> 在快照中不存在。', '可用窗口：' + windowOptions().join('、')]); }

    var meta = state.snapshot.meta || {};
    var allTime = state.snapshot.overview.allTime || {};
    var counts = (ov.headline && ov.headline.counts) || {};
    var rows = Array.isArray(ov.matrix) ? ov.matrix : [];

    // 本窗口探测次数 = 各接口 L2 窗口总数之和（派生自数据，不臆造）
    var winProbes = rows.reduce(function (a, r) {
      var w = r.layers && r.layers.l2 && r.layers.l2.window_total;
      return a + (isNum(w) ? w : 0);
    }, 0);

    var okCount = isNum(counts.ok) ? counts.ok : 0;
    // 后端把计数键也改名为 `not_monitored`；`no_key` 只作为老快照兜底。
    var noKeyCount = isNum(counts.not_monitored) ? counts.not_monitored
      : (isNum(counts.no_key) ? counts.no_key : 0);
    var epCount = isNum(meta.endpointsCount) ? meta.endpointsCount : (state.snapshot.endpoints && state.snapshot.endpoints.items ? state.snapshot.endpoints.items.length : null);
    // 分母用**本轮真正纳入观测的接口数**，而不是全部接口数：把「未纳入观测」算进分母，
    // 会得出「5 个接口中 1 个可用」这种看似全线告急、实则大部分接口本轮没被测的结论。
    var monitored = rows.length ? rows.length - noKeyCount : null;
    var availSub = (monitored != null && epCount != null && monitored < epCount)
      ? '本窗口 · 另有 ' + (epCount - monitored) + ' 个未纳入观测'
      : '本窗口 · ' + (WINDOW_LABELS[win] || win);

    var kpis =
      kpi('可用接口', okCount + ' / ' + (monitored == null ? '—' : monitored), availSub) +
      kpi('本窗口探测', winProbes ? fmtInt(winProbes) : '—', '各接口合计') +
      kpi('全量可用率', isNum(allTime.availability) ? fmtPct(allTime.availability, 1) : '—', '全部历史 ' + fmtInt(allTime.totalProbes) + ' 次探测') +
      kpi('可观测模型', isNum(meta.modelCount) ? fmtInt(meta.modelCount) : '—', '快照内模型数');

    var seg = '<div class="seg">' + windowOptions().map(function (k) {
      return '<button data-win="' + esc(k) + '"' + (k === win ? ' class="is-active"' : '') + '>' + esc(WINDOW_LABELS[k] || k) + '</button>';
    }).join('') + '</div>';

    // 后端已把运维稿换成访客稿（`headline.summary`，不再含「缺少密钥」这类措辞）。
    // `text` 分支是给老快照的兜底，**新契约不会走到**。
    var summary = (ov.headline && (ov.headline.summary || ov.headline.text)) || '';

    var table =
      '<div class="table-scroll"><table><thead><tr>' +
      '<th>接口</th><th>路径</th><th>连通 L1</th><th class="num">可用率 L2</th><th class="num">首字 L3</th><th class="num">吞吐 L3</th><th>结论</th><th class="num">样本</th><th>代表模型</th>' +
      '</tr></thead><tbody>' +
      (rows.length ? rows.map(matrixRowHtml).join('') : '<tr><td colspan="9"><div class="empty">该窗口暂无数据</div></td></tr>') +
      '</tbody></table></div>';

    return '<section class="section">' +
      '<div class="page-head"><h1>' + esc(PROJECT) + ' · 公开总览</h1>' +
      '<p class="lede">免费 AI 接口的周期性探测结果（只读镜像）。数据来自单一测量出口，窗口以墙上时钟切分。详情见「方法说明」。</p></div>' +
      '<div class="toolbar">' + seg + '<span class="spacer"></span>' +
      '<span class="muted small">最近一轮探测 ' + esc(fmtTs(ov.probe && ov.probe.last_cycle_at)) + ' · 规则版本 ' + esc(dash(ov.rules_version)) + '</span></div>' +
      '<div class="kpis" style="margin-bottom:14px">' + kpis + '</div>' +
      (summary ? '<div class="status-line" style="margin-bottom:12px"><svg class="ico"><use href="#i-info"/></svg><p>' + esc(summary) + '</p></div>' : '') +
      '<div class="section-head"><h2>接口状态矩阵</h2><span class="hint">' + esc(WINDOW_LABELS[win] || win) + ' · ' + rows.length + ' 个接口</span></div>' +
      table +
      '</section>';
  }

  function kpi(label, value, sub) {
    return '<div class="card kpi"><div class="k-label">' + esc(label) + '</div>' +
      '<div class="k-value">' + value + '</div>' +
      '<div class="k-sub">' + esc(sub || '') + '</div></div>';
  }

  /* ================= 视图：榜单 ================= */

  function sceneKeys() {
    var rk = state.snapshot.rankings || {};
    var keys = Object.keys(rk);
    return keys.length ? keys : ['speed', 'agent', 'stable'];
  }

  function renderRankings() {
    var scenes = sceneKeys();
    var scene = state.params.scene && scenes.indexOf(state.params.scene) >= 0 ? state.params.scene : scenes[0];
    var board;
    try { board = apiGet('rankings', { scene: scene }); }
    catch (e) { return renderNotice(e.code, '数据暂不可用', ['榜单 <code>' + esc(scene) + '</code> 在快照中不存在。']); }

    var items = Array.isArray(board.items) ? board.items : [];

    var seg = '<div class="seg">' + scenes.map(function (k) {
      var r = state.snapshot.rankings[k] || {};
      return '<button data-scene="' + esc(k) + '"' + (k === scene ? ' class="is-active"' : '') + '>' + esc(r.sceneLabel || k) + '</button>';
    }).join('') + '</div>';

    var body = items.length ? items.map(function (it) {
      return '<tr>' +
        '<td><span class="rank-no top1">' + esc(it.rank == null ? (items.indexOf(it) + 1) : it.rank) + '</span></td>' +
        '<td><div class="model-cell"><span class="m-name">' + esc(dash(it.model)) + '</span><span class="m-id">' + esc(dash(it.modelId)) + '</span></div></td>' +
        '<td class="ep-cell">' + esc(dash(it.endpoint)) + '</td>' +
        '<td class="num">' + (isNum(it.n) ? fmtInt(it.n) : '—') + '</td>' +
        '<td class="num"><strong>' + (isNum(it.score) ? fmtNum(it.score, 2) : '—') + '</strong></td>' +
        '<td class="num">' + fmtMs(it.ttftP50) + '</td>' +
        '<td class="num">' + fmtTps(it.tpsP50) + '</td>' +
        '<td class="num">' + fmtMs(it.totalP50) + '</td>' +
        '<td class="num muted">' + fmtPct(it.breakdown && it.breakdown.availability, 0) + '</td>' +
        '</tr>';
    }).join('') : '<tr><td colspan="9"><div class="empty">该榜单暂无达到最低样本数的模型</div></td></tr>';

    var table =
      '<div class="table-scroll"><table><thead><tr>' +
      '<th>排名</th><th>模型</th><th>接口</th><th class="num">样本</th><th class="num">综合分</th>' +
      '<th class="num">首字 P50</th><th class="num">吞吐 P50</th><th class="num">总延迟 P50</th><th class="num">可用</th>' +
      '</tr></thead><tbody>' + body + '</tbody></table></div>';

    var weights = board.weights || {};
    var wline = Object.keys(weights).map(function (k) { return k + ' ' + fmtNum(weights[k] * 100, 0) + '%'; }).join('　·　');

    var excluded = Array.isArray(board.excludedSummary) && board.excludedSummary.length
      ? '<div class="section-head" style="margin-top:18px"><h2>未入榜样本</h2><span class="hint">共 ' + fmtInt(board.excludedCount) + ' 条被硬过滤，不参与排名</span></div>' +
        '<div class="table-scroll"><table><thead><tr><th>接口</th><th class="num">条数</th><th>原因</th></tr></thead><tbody>' +
        board.excludedSummary.map(function (g) {
          var reasons = (g.reasons || []).map(function (r) { return esc(r.reason) + ' ×' + fmtInt(r.count); }).join('、');
          return '<tr><td>' + esc(g.endpoint) + '</td><td class="num">' + fmtInt(g.total) + '</td><td class="muted small">' + reasons + '</td></tr>';
        }).join('') + '</tbody></table></div>'
      : '';

    return '<section class="section">' +
      '<div class="page-head"><h1>榜单</h1>' +
      '<p class="lede">' + esc(board.sceneLabel || scene) + '：按加权综合分排序，仅纳入达到最低样本数（' + esc(dash(board.minSamples)) + ' 条）的模型。缺失分项按剩余权重重新分配。</p></div>' +
      '<div class="toolbar">' + seg + '<span class="spacer"></span><span class="muted small nowrap">权重　' + esc(wline) + '</span></div>' +
      table +
      (board.note ? '<p class="muted small" style="margin-top:10px">' + esc(board.note) + '</p>' : '') +
      excluded +
      '</section>';
  }

  /* ================= 视图：接口与门槛 ================= */

  function availabilityBar(v) {
    var level = 'ok';
    if (isNum(v)) { if (v < 60) level = 'danger'; else if (v < 90) level = 'warn'; }
    var pct = isNum(v) ? Math.max(0, Math.min(100, v)) : 0;
    return '<div class="bar" data-level="' + level + '"><i style="width:' + pct + '%"></i></div>';
  }

  function renderProviders() {
    var ep = state.snapshot.endpoints || {};
    var items = Array.isArray(ep.items) ? ep.items : [];
    var cat = state.snapshot.catalog || {};
    var catItems = Array.isArray(cat.items) ? cat.items : [];

    var cards = items.length ? '<div class="ep-grid">' + items.map(function (e) {
      var statusCls = e.publicStatus === 'ACTIVE' ? 'ok' : 'plain';
      var link = e.website
        ? '<a href="' + esc(e.website) + '" target="_blank" rel="noopener noreferrer">官网 <svg style="width:11px;height:11px;vertical-align:-1px"><use href="#i-external"/></svg></a>'
        : '<span class="muted">无官网</span>';
      return '<div class="card ep-card">' +
        '<div class="ep-top"><div><div class="ep-name">' + esc(dash(e.label)) + '</div>' +
        '<div class="ep-sub">' + esc(dash(e.provider)) + ' · ' + esc(tr('kind', e.kind)) + '</div></div>' +
        badge(tr('quotaSource', e.quotaSource), 'plain', false) + badge(e.publicStatus === 'ACTIVE' ? '启用' : '暂停', statusCls) + '</div>' +
        availabilityBar(e.availability) +
        '<div class="ep-stats">' +
        stat('可用率', isNum(e.availability) ? fmtPct(e.availability, 1) : '—') +
        stat('首字 P50', fmtMs(e.ttftP50)) +
        stat('平均吞吐', fmtTps(e.tpsAvg)) +
        stat('探测数', isNum(e.probeCount) ? fmtInt(e.probeCount) : '—') +
        stat('模型数', isNum(e.modelCount) ? fmtInt(e.modelCount) : '—') +
        stat('最近探测', fmtTs(e.lastProbeAt)) +
        '</div>' +
        '<div class="muted small" style="display:flex;justify-content:space-between;gap:8px">' + link +
        '<span>' + esc(dash(e.protocol)) + '</span></div>' +
        '</div>';
    }).join('') + '</div>' : '<div class="card"><div class="empty">暂无接口</div></div>';

    var catTable = catItems.length ? '<div class="table-scroll"><table><thead><tr>' +
      '<th>Provider</th><th>类型</th><th>注册</th><th>免费形态</th><th>需信用卡</th><th>需实名</th><th>地区限制</th><th>可信度</th><th class="num">已测模型</th>' +
      '</tr></thead><tbody>' + catItems.map(function (c) {
        return '<tr>' +
          '<td><strong>' + esc(dash(c.provider)) + '</strong></td>' +
          '<td>' + esc(tr('kind', c.kind)) + '</td>' +
          '<td>' + esc(tr('signup_method', c.signup_method)) + '</td>' +
          '<td>' + esc(tr('free_form', c.free_form)) + '</td>' +
          '<td>' + boolBadge(c.requires_card) + '</td>' +
          '<td>' + boolBadge(c.requires_realname) + '</td>' +
          '<td>' + boolBadge(c.region_restricted) + '</td>' +
          '<td>' + badge(tr('confidence', c.confidence), c.confidence === 'CONFIRMED' ? 'ok' : 'plain', false) + '</td>' +
          '<td class="num">' + (isNum(c.probed_models) ? fmtInt(c.probed_models) : '—') + '</td>' +
          '</tr>';
      }).join('') + '</tbody></table></div>' : '<div class="card"><div class="empty">暂无台账条目</div></div>';

    return '<section class="section">' +
      '<div class="page-head"><h1>接口与接入门槛</h1>' +
      '<p class="lede">已登记并纳入探测的接口；下方为各 Provider 的公开接入门槛台账（注册方式、免费形态、资质要求）。</p></div>' +
      '<div class="section-head"><h2>接口清单</h2><span class="hint">' + items.length + ' 个</span></div>' +
      cards +
      '<div class="section-head" style="margin-top:22px"><h2>接入门槛台账</h2><span class="hint">' + catItems.length + ' 家</span></div>' +
      catTable +
      '</section>';
  }

  function stat(label, value) {
    return '<div class="ep-stat"><span class="s-label">' + esc(label) + '</span><span class="s-value">' + value + '</span></div>';
  }

  /* ================= 视图：探测历史 ================= */

  function renderHistory() {
    var pr = state.snapshot.probes || {};
    var items = Array.isArray(pr.items) ? pr.items : [];
    var total = pr.total;

    var eps = ['__all__'].concat((state.snapshot.endpoints && state.snapshot.endpoints.items || []).map(function (e) { return e.label; }));
    var epFilter = state.params.ep && eps.indexOf(state.params.ep) >= 0 ? state.params.ep : '__all__';
    var lvFilter = state.params.lv || '__all__';

    var rows = items.filter(function (p) {
      if (epFilter !== '__all__' && p.endpoint !== epFilter) return false;
      if (lvFilter !== '__all__' && String(p.level) !== lvFilter) return false;
      return true;
    });

    var epSeg = '<div class="seg">' + eps.map(function (l) {
      var label = l === '__all__' ? '全部接口' : l;
      return '<button data-ep="' + esc(l) + '"' + (l === epFilter ? ' class="is-active"' : '') + '>' + esc(label) + '</button>';
    }).join('') + '</div>';

    var lvSeg = '<div class="seg">' + ['__all__', 'L0', 'L2'].map(function (l) {
      var label = l === '__all__' ? '全部层级' : (LEVEL_LABELS[l] || l);
      return '<button data-lv="' + esc(l) + '"' + (l === lvFilter ? ' class="is-active"' : '') + '>' + esc(label) + '</button>';
    }).join('') + '</div>';

    var body = rows.length ? rows.map(function (p) {
      var note = p.error_kind ? String(p.error_kind) : '';
      if (isNum(p.retry_after)) note += (note ? ' · ' : '') + 'retry ' + Math.round(p.retry_after) + 's';
      if (p.stream_mode) note += (note ? ' · ' : '') + p.stream_mode;
      return '<tr>' +
        '<td class="mono nowrap">' + esc(fmtTs(p.created_at)) + '</td>' +
        '<td>' + esc(dash(p.endpoint)) + '</td>' +
        '<td>' + badge(dash(p.level), 'plain', false) + '</td>' +
        '<td class="muted small" style="max-width:200px">' + esc(dash(p.model_display || p.model_id)) + '</td>' +
        '<td>' + statusBadge(p.status) + '</td>' +
        '<td class="num">' + (isNum(p.http_status) ? fmtInt(p.http_status) : '—') + '</td>' +
        '<td class="num">' + fmtMs(p.ttft_ms) + '</td>' +
        '<td class="num">' + fmtTps(p.tps) + '</td>' +
        '<td class="num">' + fmtMs(p.total_ms) + '</td>' +
        '<td class="muted small">' + esc(note) + '</td>' +
        '</tr>';
    }).join('') : '<tr><td colspan="10"><div class="empty">无匹配的探测记录</div></td></tr>';

    var truncated = isNum(total) && isNum(pr.limit) && total > pr.limit
      ? '仅展示最近 ' + fmtInt(pr.limit) + ' 条（历史共 ' + fmtInt(total) + ' 条，完整历史不回传公开站）'
      : '共 ' + fmtInt(rows.length) + ' 条记录';

    return '<section class="section">' +
      '<div class="page-head"><h1>探测历史</h1>' +
      '<p class="lede">最近的原始探测记录摘要（已剥离请求/响应正文与出网信息）。' + esc(truncated) + '</p></div>' +
      '<div class="toolbar">' + epSeg + lvSeg + '<span class="spacer"></span><span class="muted small">命中 ' + fmtInt(rows.length) + ' / ' + fmtInt(items.length) + ' 条</span></div>' +
      '<div class="table-scroll" style="max-height:620px;overflow-y:auto"><table><thead><tr>' +
      '<th>时间</th><th>接口</th><th>层级</th><th>模型</th><th>状态</th><th class="num">HTTP</th>' +
      '<th class="num">首字</th><th class="num">吞吐</th><th class="num">总延迟</th><th>备注</th>' +
      '</tr></thead><tbody>' + body + '</tbody></table></div>' +
      '</section>';
  }

  /* ================= 视图：方法说明 ================= */

  /* 由探测记录估算**轮次间隔**（分钟）。判不出就返回 null，页面显示 —。

     为什么不能直接取「相邻记录间隔的中位数」：同一轮探测会并发写多条
     （多端点 × 多模型），它们在几秒内落库，于是**相邻间隔绝大多数是 1–3 秒**，
     中位数直接落进簇内 → 页面上显示出「约每 0 分钟一轮」这种废话。
     真实轮间隔在**簇与簇之间**，比簇内大一到两个数量级。

     做法：把间隔排序后找**最大比值跳变**，它天然落在「簇内/簇间」的
     空档里（只要轮间隔明显大于簇内抖动）。此判据**不含任何写死的秒数**——
     不假设探测频率是 30 分钟，换调度也不用改这里。
     幂等：算不出「至少 1 分钟」就返回 null，绝不拿 0 去糊弄访客。 */
  /* 由探测记录估算**轮次间隔**（分钟）。判不出就返回 null，页面显示 —。

     为什么不能直接取「相邻记录间隔的中位数」：同一轮探测会并发写多条
     （多端点 × 多模型），它们在几秒内落库，于是**相邻间隔绝大多数是 1–3 秒**，
     中位数直接落进簇内 → 页面上显示出「约每 0 分钟一轮」这种废话。
     真实轮间隔在**簇与簇之间**，比簇内大一到两个数量级。

     做法：把间隔排序后找**最大比值跳变**，它天然落在「簇内/簇间」的
     空档里（只要轮间隔明显大于簇内抖动）。此判据**不含任何写死的秒数**——
     不假设探测频率是 30 分钟，换调度也不用改这里。
     幂等：算不出「至少 1 分钟」就返回 null，绝不拿 0 去糊弄访客。 */
  function estimateIntervalMin(items) {
    var ts = items.map(function (p) { return parseUtc8Ts(p.created_at); })
      .filter(function (n) { return n != null; })
      .sort(function (a, b) { return a - b; });
    if (ts.length < 3) return null;

    var gaps = [];
    for (var i = 1; i < ts.length; i++) { var g = ts[i] - ts[i - 1]; if (g > 0) gaps.push(g); }
    if (gaps.length < 2) return null;
    gaps.sort(function (a, b) { return a - b; });

    // 最大比值跳变 → 切分阈值（取跳变两侧的几何平均）
    var bestRatio = 0, cut = -1;
    for (var j = 0; j < gaps.length - 1; j++) {
      var r = gaps[j + 1] / gaps[j];
      if (r > bestRatio) { bestRatio = r; cut = j; }
    }
    if (cut < 0 || bestRatio < 2) return null;          // 无双峰 → 判不出
    var thr = Math.sqrt(gaps[cut] * gaps[cut + 1]);

    // 按阈值聚簇：簇首即「一轮」的起始时刻
    var starts = [ts[0]];
    for (var k = 1; k < ts.length; k++) {
      if (ts[k] - ts[k - 1] > thr) starts.push(ts[k]);
    }
    if (starts.length < 3) return null;                 // 轮次太少 → 判不出

    var cycle = [];
    for (var m = 1; m < starts.length; m++) cycle.push((starts[m] - starts[m - 1]) / 60000);
    var med = cycle[Math.floor(cycle.length / 2)];
    var min = Math.round(med);
    return min >= 1 ? min : null;
  }

  function renderMethodology() {
    var meta = state.snapshot.meta || {};
    var probes = state.snapshot.probes || {};
    var interval = estimateIntervalMin(Array.isArray(probes.items) ? probes.items : []);

    var kv = function (dt, dd) { return '<dt>' + esc(dt) + '</dt><dd>' + dd + '</dd>'; };

    var disclaimer =
      '<div class="status-line" style="margin-bottom:16px"><svg class="ico"><use href="#i-info"/></svg><p>' +
      '本页面展示 ' + esc(PROJECT) + ' 在指定测量网络环境下，对所配置 Provider / 模型进行的周期性观测结果。' +
      '实际延迟和可用性会受到测量出口、网络路由、Provider 区域策略、模型版本、请求内容、采样时间和服务负载影响。' +
      '该排名不代表所有地区、所有账户或所有用户的绝对性能，也不构成 Provider 服务等级承诺。' +
      '</p></div>';

    var winList = (meta.windows || []).map(function (w) { return WINDOW_LABELS[String(w)] || (w + 'h'); }).join(' / ');

    var basics =
      '<h3>测量条件</h3><dl class="kv">' +
      kv('测量来源', esc(dash(meta.probeSource)) + '（' + esc(dash(meta.measurementScope)) + '）') +
      kv('时间口径', esc(dash(meta.timezone)) + '（项目固定时区，不做跨时区换算）') +
      kv('探测频率', interval ? '约每 ' + interval + ' 分钟一轮（据最近探测记录估算）' : '—') +
      kv('时间窗口', esc(winList || '—')) +
      kv('请求类型', '流式与非流式混合（逐条记录标注）') +
      kv('首字延迟 TTFT', '自请求发出到收到首个正文 Token（流式）') +
      kv('吞吐 TPS', '正文输出 Token 数 ÷ 生成耗时（不含首字等待）') +
      kv('成功率', '健康探测数 ÷ 有效探测数；429 / 限流样本不计入可用率分母') +
      kv('超时与失败', '超时计为失败样本；分类器对无法确认的流式结果宁可判为缓冲，不虚报吞吐') +
      kv('快照标识', '<code>' + esc(dash(meta.snapshotId)) + '</code>') +
      kv('数据截至', esc(dash(meta.dataAsOf))) +
      kv('快照生成', esc(dash(meta.generatedAt))) +
      kv('契约版本', 'schemaVersion ' + esc(dash(state.snapshot.schemaVersion)) + '　·　policyVersion ' + esc(dash(meta.policyVersion))) +
      kv('样本策略', meta.samplePolicy ? ('随包 ' + esc(dash(meta.samplePolicy.recentProbeLimit)) + ' 条' + (meta.samplePolicy.historyIsTruncated ? '（历史已截断）' : '')) : '—') +
      '</dl>';

    var sceneRules = Object.keys(state.snapshot.rankings || {}).map(function (k) {
      var r = state.snapshot.rankings[k] || {};
      var w = r.weights || {};
      var wline = Object.keys(w).map(function (kk) { return kk + ' ' + fmtNum(w[kk] * 100, 0) + '%'; }).join('、');
      return '<li><strong>' + esc(r.sceneLabel || k) + '</strong>：最低样本 ' + esc(dash(r.minSamples)) + ' 条；权重 ' + esc(wline) + '。<br><span class="muted small">' + esc(dash(r.note)) + '</span></li>';
    }).join('');

    var rankings = '<h3>排名算法</h3><ul>' + (sceneRules || '<li class="muted">—</li>') + '</ul>';

    var states = '<h3>状态词表</h3><div style="display:flex;flex-wrap:wrap;gap:6px">' +
      Object.keys(STATUS_MAP).map(function (k) { return badge(k + ' · ' + STATUS_MAP[k].label, STATUS_MAP[k].cls); }).join('') +
      '</div>';

    var fresh = '<h3>数据新鲜度阈值</h3><ul>' +
      '<li>≤ 90 分钟：数据更新正常</li><li>90 – 180 分钟：数据可能延迟</li>' +
      '<li>&gt; 180 分钟：数据已过期，请谨慎参考</li><li>无有效时间：暂无可用数据</li></ul>' +
      '<p class="muted small">阈值随实际探测周期调整。新鲜度按<strong>北京时间（UTC+8，项目固定口径）</strong>计算，' +
      '与访问者所在时区无关；页头徽标同时给出数据更新到的具体时刻。</p>';

    var scope = '<h3>关于「未纳入观测」</h3>' +
      '<p>部分接口本轮没有产生有效观测数据。原因只与测量端配置有关，' +
      '不代表该服务本身有问题，也不计入可用率分母——本页所有可用率都只在' +
      '<strong>实际观测到的样本</strong>上计算。</p>';

    return '<section class="section"><div class="page-head"><h1>方法说明</h1>' +
      '<p class="lede">数据如何产生、口径如何定义、榜单如何排序。所有说明逐项对应后端计算代码，不做额外推断。</p></div>' +
      disclaimer +
      '<div class="card"><div class="card-pad prose">' + basics + scope + rankings + states + fresh +
      '<h3>源码</h3><ul><li><a href="' + esc(REPO_URL) + '" target="_blank" rel="noopener noreferrer">' + esc(REPO_URL.replace('https://', '')) + '</a>（探测与榜单实现）</li></ul>' +
      '</div></div></section>';
  }

  /* ================= 路由 ================= */

  var RENDERERS = {
    overview: renderOverview,
    rankings: renderRankings,
    providers: renderProviders,
    history: renderHistory,
    methodology: renderMethodology
  };

  function parseHash() {
    var raw = String(location.hash || '').replace(/^#\/?/, '');
    var parts = raw.split('?');
    var view = parts[0] || 'overview';
    var params = {};
    if (parts[1]) {
      parts[1].split('&').forEach(function (kv) {
        if (!kv) return;
        var i = kv.indexOf('=');
        var k = i < 0 ? kv : kv.slice(0, i);
        var v = i < 0 ? '' : kv.slice(i + 1);
        try { params[decodeURIComponent(k)] = decodeURIComponent(v); } catch (e) { params[k] = v; }
      });
    }
    return { view: view, params: params };
  }

  function buildHash() {
    var keys = Object.keys(state.params).filter(function (k) { return state.params[k] != null && state.params[k] !== ''; });
    var qs = keys.map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(state.params[k]); }).join('&');
    return '#/' + state.view + (qs ? '?' + qs : '');
  }

  function navigate(view, params, silent) {
    state.view = view;
    state.params = params || {};
    if (silent) { render(); return; }
    var h = buildHash();
    if (location.hash === h) render();
    else location.hash = h; // hashchange → render
  }

  function renderNav() {
    var nav = $('#nav');
    if (!nav) return;
    nav.innerHTML = Object.keys(VIEWS).map(function (k) {
      return '<button class="tab' + (k === state.view ? ' is-active' : '') + '" data-nav="' + k + '">' + esc(VIEWS[k].label) + '</button>';
    }).join('');
  }

  function renderView() {
    var host = $('#view');
    if (!host) return;

    if (state.loadState === 'loading') { host.innerHTML = skeleton(); return; }
    if (state.loadState === 'error') { host.innerHTML = renderLoadError(state.error); return; }

    var fn = RENDERERS[state.view];
    if (!fn) { host.innerHTML = renderForbiddenView(state.view); return; }

    try {
      host.innerHTML = fn();
    } catch (e) {
      host.innerHTML = renderNotice(e.code || 'PUBLIC_DATA_NOT_FOUND', '渲染失败', [esc(e.message || String(e))]);
    }
    document.title = (VIEWS[state.view] ? VIEWS[state.view].title + ' · ' : '') + PROJECT + ' · 公开榜单';
  }

  function render() {
    var parsed = parseHash();
    state.view = parsed.view;
    state.params = parsed.params;
    renderNav();
    renderView();
  }

  /* ================= 头部：新鲜度 + 主题 ================= */

  function renderFreshness() {
    var badgeEl = $('#freshBadge'), textEl = $('#freshText');
    if (!badgeEl || !textEl) return;
    var meta = (state.snapshot && state.snapshot.meta) || {};
    var f = freshnessOf(meta.dataAsOf);
    badgeEl.setAttribute('data-fresh', f.state);
    badgeEl.setAttribute('title', f.note || '数据新鲜度');
    // 主界面必须能一眼看到「数据更新到什么时候」——只给状态词不够，
    // 访客无从判断该不该信这份数据。时间为北京时间（项目固定口径）。
    var stamp = shortStamp(meta.dataAsOf);
    var prefix = { fresh: '数据更新', stale: '数据延迟', expired: '数据已过期', unknown: '' }[f.state];
    if (f.state === 'unknown' || !stamp) {
      textEl.textContent = '暂无数据时间';
      return;
    }
    textEl.textContent = (prefix ? prefix + ' ' : '') + stamp;
  }

  /** `2026-11-24 15:34:23` → `15:34`；跨天时补 `11-24 15:34`。 */
  function shortStamp(ts) {
    if (!ts) return '';
    var s = String(ts).replace('T', ' ');
    var hhmm = s.length >= 16 ? s.slice(11, 16) : '';
    if (!hhmm) return '';
    // 数据侧日期**直接从串里取**：串本身就是北京墙上时间，前 10 位就是它的日期。
    // 不要再绕 `parseUtc8Ts` + `toISOString()`——那等于把「帧 B」当真实 epoch 解读，
    // 会整体前移 8 小时，凡 16:00 之后的当天数据都被误判成「明天」。
    var s = String(ts).replace('T', ' ');
    var hhmm = s.length >= 16 ? s.slice(11, 16) : '';
    if (!hhmm) return '';
    var day = s.slice(0, 10);
    var today = new Date(nowUtc8Ms()).toISOString().slice(0, 10);   // 北京的「今天」
    return day === today ? hhmm : day.slice(5) + ' ' + hhmm;
  }

  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem('tokriva-pub-theme'); } catch (e) {}
    var theme = saved || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', theme);
    var btn = $('#themeBtn');
    if (btn) btn.addEventListener('click', function () {
      var cur = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', cur);
      try { localStorage.setItem('tokriva-pub-theme', cur); } catch (e) {}
    });
  }

  /* ================= 页脚（报告 §9.5 必须展示的说明） ================= */

  function renderFooter() {
    var d = $('#disclaimer');
    if (d) {
      d.textContent = '本页面展示 ' + PROJECT + ' 在指定测量网络环境下，对所配置 Provider / 模型进行的周期性观测结果。' +
        '实际延迟和可用性会受到测量出口、网络路由、Provider 区域策略、模型版本、请求内容、采样时间和服务负载影响。' +
        '该排名不代表所有地区、所有账户或所有用户的绝对性能，也不构成 Provider 服务等级承诺。';
    }
    var m = $('#footMeta');
    if (!m) return;
    var meta = (state.snapshot && state.snapshot.meta) || {};
    m.innerHTML = [
      '数据截至 ' + esc(dash(meta.dataAsOf)),
      '快照生成 ' + esc(dash(meta.generatedAt)),
      '快照 ' + esc(dash(meta.snapshotId)),
      '时区 ' + esc(dash(meta.timezone)),
      '<a href="' + esc(REPO_URL) + '" target="_blank" rel="noopener noreferrer">源码仓库</a>'
    ].map(function (s) { return '<span>' + s + '</span>'; }).join('');
  }

  /* ================= 事件绑定 ================= */

  function bindEvents() {
    var host = $('#view');
    if (host) {
      host.addEventListener('click', function (ev) {
        var t = ev.target.closest ? ev.target.closest('[data-nav],[data-win],[data-scene],[data-ep],[data-lv]') : null;
        if (!t) return;
        if (t.hasAttribute('data-nav')) { navigate(t.getAttribute('data-nav'), {}); return; }
        if (t.hasAttribute('data-win')) { navigate('overview', merge(state.params, { window: t.getAttribute('data-win') })); return; }
        if (t.hasAttribute('data-scene')) { navigate('rankings', merge(state.params, { scene: t.getAttribute('data-scene') })); return; }
        if (t.hasAttribute('data-ep')) { navigate('history', merge(state.params, { ep: t.getAttribute('data-ep') })); return; }
        if (t.hasAttribute('data-lv')) { navigate('history', merge(state.params, { lv: t.getAttribute('data-lv') })); return; }
      });
    }
    var nav = $('#nav');
    if (nav) {
      nav.addEventListener('click', function (ev) {
        var t = ev.target.closest ? ev.target.closest('[data-nav]') : null;
        if (t) navigate(t.getAttribute('data-nav'), {});
      });
    }
    window.addEventListener('hashchange', function () {
      render();
      var main = $('#view');
      if (main && main.scrollIntoView) { try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch (e) { window.scrollTo(0, 0); } }
    });
  }

  function merge(a, b) {
    var o = {}; var k;
    a = a || {}; b = b || {};
    for (k in a) if (Object.prototype.hasOwnProperty.call(a, k)) o[k] = a[k];
    for (k in b) if (Object.prototype.hasOwnProperty.call(b, k)) o[k] = b[k];
    return o;
  }

  /* ================= 启动 ================= */

  function boot() {
    initTheme();
    bindEvents();
    renderNav();
    render();

    loadSnapshot().then(function (snap) {
      state.snapshot = snap;
      state.loadState = 'ready';
      renderFreshness();
      renderFooter();
      render();
    }).catch(function (err) {
      state.snapshot = null;
      state.loadState = 'error';
      state.error = err;
      renderFreshness();
      renderFooter();
      renderView();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
