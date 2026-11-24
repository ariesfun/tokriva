#!/usr/bin/env node
/* ============================================================
   Tokriva 公开前端契约消费检查（web/public/check_public_ui.js）
   ------------------------------------------------------------
   为什么需要它：`web/app.js` 的浏览器侧分支长期零测试覆盖
   （见 docs/static-pages-assessment.md §七）。公开前端是**独立**的只读页面，
   恰好最容易做成可执行断言——它只依赖一份 snapshot.json 和一段 DOM。

   做法：用一段极简 DOM 桩把 `public.js` 真实跑起来，逐个视图驱动路由，
   断言报告 §10 的四条硬要求：
     · 视图白名单（Overview/Rankings/Providers/History/Methodology 可进；Settings/Reminders/… 拒绝）
     · 四个错误码（PUBLIC_DATA_NOT_FOUND / PUBLIC_SCHEMA_UNSUPPORTED /
                    PUBLIC_VIEW_FORBIDDEN / PUBLIC_READ_ONLY）
     · Overview 窗口严格匹配（缺 key 不得静默回退到别的窗口）
     · 快照缺失 / 版本不符 → 显式「数据暂不可用」，**不回退演示数据**

   用法：
     node web/public/check_public_ui.js [--site <站点目录>]
   站点目录须含 `public.js` 与 `data/snapshot.json`（默认取脚本所在目录）。
   退出码：0 全通过；1 有失败；2 用法/环境错误。
   ============================================================ */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

/* ---------- 参数 ---------- */
function parseArgs(argv) {
  const out = { site: null };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === '--site') out.site = argv[++i];
  }
  return out;
}

const args = parseArgs(process.argv);
const here = __dirname;
const site = path.resolve(args.site || here);

const jsPath = path.join(site, 'public.js');
let snapPath = path.join(site, 'data', 'snapshot.json');
if (!fs.existsSync(snapPath)) snapPath = path.join(site, 'snapshot.json');

if (!fs.existsSync(jsPath)) { console.error('[check_public_ui] 找不到 ' + jsPath); process.exit(2); }
if (!fs.existsSync(snapPath)) { console.error('[check_public_ui] 找不到快照（试过 data/snapshot.json 与 snapshot.json）'); process.exit(2); }

const src = fs.readFileSync(jsPath, 'utf8');
const snapshot = JSON.parse(fs.readFileSync(snapPath, 'utf8'));

/* ---------- 结果收集 ---------- */
const results = [];
function check(name, cond, extra) { results.push({ name: name, ok: !!cond, extra: extra == null ? '' : String(extra) }); }

/* ---------- 极简 DOM 桩 ---------- */
function mkEl(id) {
  return {
    id: id, _html: '', _text: '', _attrs: {}, _handlers: {},
    set innerHTML(v) { this._html = String(v); }, get innerHTML() { return this._html; },
    set textContent(v) { this._text = String(v); }, get textContent() { return this._text; },
    setAttribute(k, v) { this._attrs[k] = String(v); }, getAttribute(k) { return this._attrs[k] == null ? null : this._attrs[k]; },
    addEventListener(t, h) { (this._handlers[t] = this._handlers[t] || []).push(h); },
    closest() { return null; },
  };
}

/* 把「当前时刻」钉死的 Date 替身：`Date.now()` 与 `new Date()` 都返回 fixedMs。
   时间帧类缺陷（如把北京墙上时间当真实 epoch 解读）只在特定钟点显形，
   用真实 now 会随执行时刻漂移——冻结时钟才能稳定复现与回归。 */
function fakeDate(fixedMs) {
  const Real = Date;
  function FD() {
    var a = Array.prototype.slice.call(arguments);
    if (!(this instanceof FD)) return new Real(fixedMs).toString();
    if (a.length === 0) return new Real(fixedMs);
    return new (Function.prototype.bind.apply(Real, [null].concat(a)))();
  }
  FD.now = function () { return fixedMs; };
  FD.parse = Real.parse;
  FD.UTC = Real.UTC;
  FD.prototype = Real.prototype;
  return FD;
}

/* 在独立 VM 沙箱里加载 public.js。fetchMode 决定快照获取行为；
   nowMs 非空时冻结时钟（用于时间帧断言）。 */
function boot(snapshotObj, fetchMode, nowMs) {
  const els = {};
  const winHandlers = {};
  const ctx = {
    console: console,
    setTimeout: setTimeout, clearTimeout: clearTimeout,
    Date: nowMs == null ? Date : fakeDate(nowMs), Math: Math, JSON: JSON, isFinite: isFinite, isNaN: isNaN,
    encodeURIComponent: encodeURIComponent, decodeURIComponent: decodeURIComponent,
    localStorage: { getItem: function () { return null; }, setItem: function () {} },
    fetch: async function () {
      if (fetchMode === 'fail') throw new Error('network down');
      if (fetchMode === 'notfound') return { ok: false, status: 404, json: async function () { return {}; } };
      if (fetchMode === 'oldschema') return { ok: true, json: async function () { return { schemaVersion: 99, meta: {}, overview: {}, rankings: {} }; } };
      return { ok: true, json: async function () { return JSON.parse(JSON.stringify(snapshotObj)); } };
    },
  };
  ctx.window = {
    TOKRIVA_PUBLIC_BUILD: true,
    addEventListener: function (t, h) { (winHandlers[t] = winHandlers[t] || []).push(h); },
    matchMedia: function () { return { matches: false }; },
    scrollTo: function () {},
    location: { hash: '' },
  };
  ctx.document = {
    readyState: 'complete', title: '',
    documentElement: { _a: {}, setAttribute: function (k, v) { this._a[k] = v; }, getAttribute: function (k) { return this._a[k] == null ? null : this._a[k]; } },
    querySelector: function (s) { return (els[s] = els[s] || mkEl(s)); },
    addEventListener: function () {},
  };
  ctx.location = ctx.window.location;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  return {
    ctx: ctx, els: els,
    view: function () { return els['#view']._html; },
    fire: function (hash) {
      ctx.location.hash = hash;
      (winHandlers['hashchange'] || []).forEach(function (h) { h(); });
    },
    click: function (sel, attrs) {
      const el = els[sel];
      const target = { closest: function () { return { hasAttribute: function (n) { return n in attrs; }, getAttribute: function (n) { return attrs[n]; } }; } };
      (el._handlers['click'] || []).forEach(function (h) { h({ target: target }); });
    },
  };
}

function has(s) { return Array.prototype.slice.call(arguments, 1).every(function (n) { return s.indexOf(n) >= 0; }); }
const wait = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

/* ---------- 主流程 ---------- */
(async function () {
  check('快照 schemaVersion = 1（检查前置）', Number(snapshot.schemaVersion) === 1, snapshot.schemaVersion);

  /* ===== 正常加载 ===== */
  const A = boot(snapshot, 'ok');
  await wait(60);

  check('加载完成：#view 有渲染内容', A.view().length > 50, A.view().length);
  check('新鲜度徽标已落定', ['fresh', 'stale', 'expired', 'unknown'].indexOf(A.els['#freshBadge']._attrs['data-fresh']) >= 0, A.els['#freshBadge']._attrs['data-fresh']);

  /* ===== 页头必须给出「数据更新到什么时候」 =====
     只给「正常/过期」这种状态词不够：访客无从判断该不该信这份数据。
     时间戳还必须按**北京时间**解读，因为库里的时间戳就是北京时间墙上时间，
     海外访客的本机时钟与北京相差数小时——按本机算会把「刚更新过」显示成「已过期」。 */
  const freshText = A.els['#freshText']._text;
  check('页头显示数据更新时间（HH:MM）', /\d{2}:\d{2}/.test(freshText), freshText);
  check('页头时间戳取自 meta.dataAsOf',
    freshText.indexOf(String(snapshot.meta.dataAsOf).slice(11, 16)) >= 0,
    freshText + ' ← ' + snapshot.meta.dataAsOf);
  check('新鲜度提示标注北京时间口径',
    A.els['#freshBadge']._attrs['title'].indexOf('北京时间') >= 0,
    A.els['#freshBadge']._attrs['title']);

  /* 跨天快照要补出月日，否则「03:04」会被误读成今天凌晨。 */
  const XD = JSON.parse(JSON.stringify(snapshot));
  XD.meta.dataAsOf = '2026-01-02 03:04:05';
  const SNAP_XD = boot(XD, 'ok');
  await wait(60);
  check('跨天快照的时间戳补月日',
    /01-02 03:04/.test(SNAP_XD.els['#freshText']._text), SNAP_XD.els['#freshText']._text);
  check('跨天快照判为已过期',
    SNAP_XD.els['#freshBadge']._attrs['data-fresh'] === 'expired', SNAP_XD.els['#freshBadge']._attrs['data-fresh']);

  /* ===== 时间帧自检（冻结时钟） =====
     历史缺陷：`shortStamp` 曾用 `parseUtc8Ts` 的结果再 `toISOString()`，
     等于把「北京墙上时间」当成真实 epoch 解读，整体前移 8 小时——
     凡 16:00 之后的**当天**数据都被标成「明天」（如 11-24 17:06 显示成 11-25 17:06）。
     用真实 now 会随执行时刻漂移，故冻结到 2026-11-24 17:30（北京时间）。 */
  const FIXED_NOW = Date.UTC(2026, 10, 24, 9, 30, 0);   // = 2026-11-24 17:30 北京时间
  const NEAR = JSON.parse(JSON.stringify(snapshot));
  NEAR.meta.dataAsOf = '2026-11-24 17:06:22';           // 当天，约 24 分钟前
  const SNAP_NEAR = boot(NEAR, 'ok', FIXED_NOW);
  await wait(60);
  check('当天 17:06 数据 → 页头只显示 HH:MM，不串到次日',
    /数据更新 17:06$/.test(SNAP_NEAR.els['#freshText']._text), SNAP_NEAR.els['#freshText']._text);
  check('当天 17:06 数据 → 判为 fresh（既非过期也非「晚于当前时刻」）',
    SNAP_NEAR.els['#freshBadge']._attrs['data-fresh'] === 'fresh', SNAP_NEAR.els['#freshBadge']._attrs['data-fresh']);
  check('当天 17:06 数据 → 新鲜度标注「约 24 分钟前」',
    SNAP_NEAR.els['#freshBadge']._attrs['title'].indexOf('约 24 分钟前') >= 0, SNAP_NEAR.els['#freshBadge']._attrs['title']);
  check('新鲜度提示不得出现「晚于当前时刻」',
    SNAP_NEAR.els['#freshBadge']._attrs['title'].indexOf('晚于当前时刻') < 0, SNAP_NEAR.els['#freshBadge']._attrs['title']);

  /* 无数据时间时不得编造一个时间戳。 */
  const NO_TS = JSON.parse(JSON.stringify(snapshot));
  NO_TS.meta.dataAsOf = null;
  const SNAP_NO_TS = boot(NO_TS, 'ok');
  await wait(60);
  check('缺数据时间 → 只显示「暂无数据时间」',
    SNAP_NO_TS.els['#freshText']._text === '暂无数据时间', SNAP_NO_TS.els['#freshText']._text);

  check('页脚免责声明非空', A.els['#disclaimer']._text.length > 30);
  check('页脚含快照标识', A.els['#footMeta']._html.indexOf(snapshot.meta.snapshotId) >= 0);
  check('导航只含 5 个公开视图', (A.els['#nav']._html.match(/data-nav=/g) || []).length === 5, (A.els['#nav']._html.match(/data-nav=/g) || []).length);

  /* ===== 点击导航（真实交互胶水） ===== */
  A.click('#nav', { 'data-nav': 'rankings' });
  check('点击导航 → hash 切到 rankings', A.ctx.location.hash.indexOf('#/rankings') === 0, A.ctx.location.hash);

  /* ===== 各公开视图 ===== */
  A.fire('#/overview?window=24');
  check('Overview 24h 渲染矩阵与端点', has(A.view(), '接口状态矩阵', '可用接口'));

  A.fire('#/overview?window=1');
  check('Overview 1h 可切换', A.view().length > 200);

  A.fire('#/rankings?scene=speed');
  check('Rankings 渲染综合分与模型', has(A.view(), '综合分', '最低样本'));
  A.click('#view', { 'data-scene': 'stable' });
  check('点击场景分段 → hash 带 scene=stable', A.ctx.location.hash.indexOf('scene=stable') >= 0, A.ctx.location.hash);

  A.fire('#/providers');
  check('Providers 渲染接口清单与门槛台账', has(A.view(), '接口清单', '接入门槛台账'));

  A.fire('#/history');
  check('History 渲染探测历史表', has(A.view(), '探测历史'));
  A.fire('#/history?ep=__nope__&lv=L2');
  check('History 非法过滤值被忽略而非报错', A.view().indexOf('探测历史') >= 0);

  A.fire('#/methodology');
  check('Methodology 渲染方法说明四段', has(A.view(), '测量条件', '排名算法', '状态词表', '数据新鲜度阈值', '源码'));

  /* ===== 窗口严格匹配（报告 §10.6） ===== */
  A.fire('#/overview?window=999');
  check('缺失窗口 → PUBLIC_DATA_NOT_FOUND（不回退）', has(A.view(), 'PUBLIC_DATA_NOT_FOUND', '数据暂不可用'));

  /* ===== 视图白名单（报告 §10.3） ===== */
  ['settings', 'reminders', 'scheduler', 'gateway', 'config', 'endpoint-admin', 'quota-admin', 'debug', 'health'].forEach(function (v) {
    A.fire('#/' + v);
    check('禁止视图 ' + v + ' → PUBLIC_VIEW_FORBIDDEN', A.view().indexOf('PUBLIC_VIEW_FORBIDDEN') >= 0);
  });
  A.fire('#/totally-unknown');
  check('未知视图 → PUBLIC_VIEW_FORBIDDEN', A.view().indexOf('PUBLIC_VIEW_FORBIDDEN') >= 0);

  /* ===== 适配层错误码（报告 §10.4） ===== */
  let code = null;
  try { A.ctx.window.apiPost('anything'); } catch (e) { code = e.code; }
  check('apiPost → PUBLIC_READ_ONLY', code === 'PUBLIC_READ_ONLY', code);

  code = null;
  try { A.ctx.window.apiGet('config'); } catch (e) { code = e.code; }
  check('apiGet(禁止路径) → PUBLIC_VIEW_FORBIDDEN', code === 'PUBLIC_VIEW_FORBIDDEN', code);

  code = null;
  try { A.ctx.window.apiGet('overview', { window: '999' }); } catch (e) { code = e.code; }
  check('apiGet(缺失窗口) → PUBLIC_DATA_NOT_FOUND', code === 'PUBLIC_DATA_NOT_FOUND', code);

  const ep = A.ctx.window.apiGet('endpoints');
  check('apiGet(endpoints) 返回 items 数组', ep && Array.isArray(ep.items), ep && ep.items && ep.items.length);

  /* ===== 不回退演示数据（报告 §10.5） ===== */
  const B = boot(snapshot, 'fail');
  await wait(60);
  check('快照加载失败 → 数据暂不可用', B.view().indexOf('数据暂不可用') >= 0);
  check('快照加载失败 → 明示不回退演示数据', B.view().indexOf('不会回退到演示数据') >= 0);
  check('快照加载失败 → 新鲜度为 unknown', B.els['#freshBadge']._attrs['data-fresh'] === 'unknown', B.els['#freshBadge']._attrs['data-fresh']);

  const C = boot(snapshot, 'oldschema');
  await wait(60);
  check('schema 不兼容 → PUBLIC_SCHEMA_UNSUPPORTED', C.view().indexOf('PUBLIC_SCHEMA_UNSUPPORTED') >= 0);

  const D = boot(snapshot, 'notfound');
  await wait(60);
  check('快照 404 → PUBLIC_DATA_NOT_FOUND', D.view().indexOf('PUBLIC_DATA_NOT_FOUND') >= 0);

  /* ---------- 报告 ---------- */
  let fail = 0;
  results.forEach(function (r) {
    if (!r.ok) fail++;
    console.log((r.ok ? '  PASS  ' : '  FAIL  ') + r.name + (!r.ok && r.extra ? '   [' + r.extra + ']' : ''));
  });
  console.log('\n  ' + (results.length - fail) + '/' + results.length + ' 项通过' + (fail ? '　← 有 ' + fail + ' 项失败' : ''));
  process.exit(fail ? 1 : 0);
})().catch(function (e) {
  console.error('[check_public_ui] 未捕获异常：', e && e.stack ? e.stack : e);
  process.exit(2);
});
