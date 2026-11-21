/* paratranz-agent WebUI —— 原生 JS，零构建，零依赖 */
'use strict';

const $ = (s, root) => (root || document).querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

// 渲染代次：每次 render() 递增。异步渲染器在 await 之后比对它，
// 不一致说明用户已经切了视图，不能再往 #main 写（否则会把新视图顶掉）。
let renderEpoch = 0;

const state = {
  view: 'overview',
  settings: null,
  project: null,
  files: [],
  dirs: [],
  jobs: [],
  tools: [],
  openJobs: new Set(),
  logCount: 0,
  health: null,
  wbSel: '',        // 「回写」页当前选中的提取记录
  _wbStop: null,    // 回写参数区的停止钩子（换记录 / 切页前先掐掉轮询）
  agentReplyHtml: '',   // 最近一次 Agent 回复的 HTML（含 needs_confirm 提示，切页重绘不丢）
  /* 词条级实时进度：id → {original, translation, status, reason, ts}。
     来自 SSE 的 `item` 事件；只做展示，成败一律以任务结果里的计数为准。 */
  live: new Map(),
  liveJob: '',
  /* 「词条」页的筛选与游标。平台没有服务端搜索（实测 `search`/`q` 参数被忽略），
     所以只有阶段/文件两个服务端条件 + 本地过滤，不假装能全库搜。
     `loadedKey` 记录"当前这批数据是哪个查询条件拉回来的"——用它判断要不要发请求，
     而不是拿 `items.length===0` 当"没加载过"：后者在"查无结果"时会无限重拉（自激）。 */
  str: { file: '', stage: '0', page: 1, size: 50, q: '', items: [], total: 0, pages: 0,
         sel: null, detail: null, loading: false, loadedKey: '',
         // 非空 = 任务下钻模式：items 来自 /api/jobs/{id}/strings 或 /api/ledger/strings，
         // 不再按页向服务端取数（筛查只看这 N 条）
         jobId: '' },
  /* 「文件」页展开的文件与其词条列表。和 state.str 分开：两页各有自己的
     筛选与游标，混用会出现"在文件页翻页，词条页的页码也跟着变"。 */
  fileView: { fid: '', page: 1, size: 50, stage: '', q: '', items: [], total: 0, pages: 0,
              loadedKey: '', loading: false },
  /* 词条详情是**全局**的：词条页与文件页都能打开同一条，且共用同一份
     「原文 / 译文 / 两份记录」。放在某一页的状态里，另一页就看不到它。 */
  detail: null,
  detailId: null,
  /* 批量选择：词条页与文件页共用一份（两页都能选，操作按钮只有一处实现） */
  batch: new Set(),
};

const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* 双语字段取值：后端把 `xxx` 与 `xxx_en` 两份一起给（工具清单、提取器元信息、
 * 任务名），界面按当前语言挑一个。中文优先原字段，英文缺 `_en` 就回落中文——
 * 第三方导入的提取器只有中文描述，回落比显示空白更接近真实。
 * 与 `t()` 的分工：`t()` 管**前端模板里的固定文案**（走字典），
 * `L()` 管**后端数据里的动态文案**（后端本来就两端都给了，不必进字典）。 */
function L(obj, key) {
  if (!obj) return '';
  const base = obj[key] || '';
  if (typeof isEn !== 'function' || !isEn()) return base;
  return obj[key + '_en'] || base;
}

/* ------------------------------------------------------------------ API */
async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) {
    const msg = (data && (data.error || data.detail)) || res.statusText;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

/* ------------------------------------------------------------------ 视图元信息 */
const VIEWS = {
  overview:   { group: '工作台', name: '概览' },
  jobs:       { group: '工作台', name: '任务' },
  strings:    { group: '工作台', name: '词条' },
  files:      { group: '工作台', name: '文件' },
  extract:    { group: '工作台', name: '提取' },
  writeback:  { group: '工作台', name: '回写' },
  tools:      { group: '工作台', name: '工具' },
  models:     { group: '配置',   name: '模型' },
  prompts:    { group: '配置',   name: '提示词' },
  settings:   { group: '配置',   name: '项目' },
  data:       { group: '观测',   name: '数据' },
  logs:       { group: '观测',   name: '日志' },
  usage:      { group: '观测',   name: '开销' },
  extensions: { group: '扩展',   name: '插件与技能' },
  prefs:      { group: '系统',   name: '设置' },
};

function setCrumb(view) {
  const v = VIEWS[view] || { group: '', name: view };
  $('#crumb').innerHTML = `${esc(v.group)} / <b>${esc(v.name)}</b>`;
}

function pageHead(title, sub, acts) {
  return `<div class="pagehead">
    <div><h1>${esc(title)}</h1>${sub ? `<div class="sub">${sub}</div>` : ''}</div>
    <div class="acts">${acts || ''}</div>
  </div>`;
}

function metric(k, v, opts) {
  const o = opts || {};
  return `<div class="metric${o.cls ? ' ' + o.cls : ''}">
    <div class="k">${esc(k)}</div>
    <div class="v">${v}${o.unit ? `<small>${esc(o.unit)}</small>` : ''}</div>
    ${o.ico ? `<div class="ico">${o.ico}</div>` : ''}
  </div>`;
}

/* 后端特意把「读不到」和「0」分成两个字段（tasks_error / terms_error），
   前端以前只读数字，token 过期时显示「术语 0 条」，与真空库一模一样 ——
   用户会去补术语，实际是读失败。这里把失败显示出来。 */
function renderMetricOrError(k, v, err, opts) {
  if (!err) return metric(k, v, opts);
  const o = opts || {};
  return `<div class="metric${o.cls ? ' ' + o.cls : ''}" title="${esc(err)}">
    <div class="k">${esc(k)}</div>
    <div class="v" style="color:var(--err);font-size:15px">读取失败</div>
    ${o.ico ? `<div class="ico">${o.ico}</div>` : ''}
  </div>`;
}

/* ------------------------------------------------------------------ UI 基础 */
let toastTimer = null;
function toast(msg, isErr = false) {
  const el = $('#toast');
  // toast 是渲染后动态生成的，不经过 render()，这里单独过一遍翻译
  el.textContent = typeof trText === 'function' ? trText(String(msg)) : msg;
  el.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 3200);
}

function fmtNum(n) {
  n = Number(n) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

function setConn(ok, text) {
  const el = $('#connState');
  el.className = 'pill ' + (ok ? 'ok' : 'err');
  el.textContent = text;
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}

/* ------------------------------------------------------------------ 主题 */
// 首帧的主题由 index.html 里的内联脚本先定好（避免闪一下深色）。
// 三态：system（跟随系统）/ dark / light —— 存在 localStorage 的 ptz-theme。
const THEME_KEY = 'ptz-theme';

function themePref() {
  let v = null;
  try { v = localStorage.getItem(THEME_KEY); } catch { v = null; }
  return v === 'light' || v === 'dark' || v === 'system' ? v : 'system';
}

function systemTheme() {
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
    ? 'light' : 'dark';
}

// 实际生效的主题（system 时解析成当前系统主题）
function themeCurrent() {
  const p = themePref();
  return p === 'system' ? systemTheme() : p;
}

// persist=false：启动时只同步，不把「跟系统」写死成用户选择
function applyTheme(pref, persist) {
  const next = pref === 'light' || pref === 'dark' ? pref : 'system';
  const root = document.documentElement;
  if (root && root.dataset) {
    root.dataset.theme = next === 'system' ? systemTheme() : next;
    root.dataset.themePref = next;
  }
  if (persist !== false) {
    try { localStorage.setItem(THEME_KEY, next); } catch { /* 无痕模式写不了，不影响观感 */ }
  }
  return next;
}

function toggleTheme() {
  return applyTheme(themeCurrent() === 'light' ? 'dark' : 'light');
}

// 跟随系统时，操作系统切深浅色要立刻反映出来
if (window.matchMedia) {
  const mq = window.matchMedia('(prefers-color-scheme: light)');
  const onChange = () => { if (themePref() === 'system') applyTheme('system', false); };
  if (mq.addEventListener) mq.addEventListener('change', onChange);
  else if (mq.addListener) mq.addListener(onChange);
}

/* ------------------------------------------------------ 语言（i18n 外壳） */
// 静态区（侧栏 / 顶栏 / 日志面板）不经过 render()，单独翻一次；
// 切换语言用整页重载：状态最少、不可能出现半中半英的残留。
function applyChromeLang() {
  if (typeof translateDom !== 'function') return;
  const nav = $('#nav');
  const top = $('.topbar');
  const logp = $('#logPanel');
  if (nav) translateDom(nav);
  if (top) translateDom(top);
  if (logp) translateDom(logp);
  // 启动时就按当前语言标好 <html lang>：只在切换语言时设会让英文界面
  // 顶着 lang="zh-CN"（影响读屏与拼写检查，也误导任何按 lang 判断的逻辑）
  const root = document.documentElement;
  if (root) {
    const en = isEn();
    root.setAttribute('lang', en ? 'en' : 'zh-CN');
    root.dataset.lang = en ? 'en' : 'zh';
  }
  if (isEn()) document.title = 'Paratranz Agent · Console';
}

function setLang(lang) {
  applyLang(lang, () => { window.location.reload(); });
}

/* ------------------------------------------------------------------ 日志 */
function pushLog(entry) {
  const box = $('#logBody');
  const div = document.createElement('div');
  div.className = 'l ' + (entry.level || 'info');
  const t = document.createElement('span');
  t.className = 't';
  t.textContent = fmtTime(entry.ts || Date.now() / 1000);
  div.appendChild(t);
  div.appendChild(document.createTextNode(entry.msg || ''));
  box.appendChild(div);
  state.logCount++;
  $('#logCount').textContent = `(${state.logCount})`;
  while (box.childElementCount > 500) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

function connectSSE() {
  const es = new EventSource('/api/events');
  const dot = $('#sseDot');
  es.onopen = () => { dot.classList.add('on'); };
  es.onerror = () => { dot.classList.remove('on'); };
  es.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    if (data.type === 'log') {
      pushLog(data.entry);
    } else if (data.type === 'job') {
      pushLog({ ts: data.ts, level: data.job.status === 'failed' ? 'error' : 'info',
                msg: `任务 ${data.job.tool} → ${data.job.status}` });
      const st = data.job.status;
      if (st && st !== 'running' && st !== 'queued') {
        settleLive(st);
        scheduleProjectRefresh();     // 写完了就让概览的「待翻译」跟上
      }
      refreshJobs();
    } else if (data.type === 'item') {
      pushItems(data.job_id, data.items, data.ts);
    } else if (data.type === 'settings') {
      state.settings = data.settings;
      $('#dryRun').checked = !!data.settings.dry_run;
    }
  };
}

/* ------------------------------------------------------------------ 导航 */
function switchView(v) {
  // 防抖定时器不清理的话，300ms 后会把插件页画到别的视图上
  clearTimeout(state._extTimer);
  // 回写页的轮询/倒计时要跟着切页一起停（页面被换掉后没人再写这块 DOM）
  if (state._wbStop) { state._wbStop(); state._wbStop = null; }
  // 换页就丢掉批量选择：否则在文件页勾的条目会跟着到词条页，
  // 而那一页根本看不到它们 —— 一次批量操作就打到"看不见的地方"。
  if (state.view !== v && state.batch && state.batch.size) state.batch.clear();
  state.view = v;
  $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.view === v));
  setCrumb(v);
  render();
}

function render() {
  renderEpoch++;
  const main = $('#main');
  const v = state.view;
  if (v === 'overview') renderOverview(main);
  else if (v === 'jobs') renderJobs(main);
  else if (v === 'strings') renderStrings(main);
  else if (v === 'files') renderFiles(main);
  else if (v === 'extract') renderExtract(main);
  else if (v === 'writeback') renderWriteback(main);
  else if (v === 'tools') renderTools(main);
  else if (v === 'models') renderModels(main);
  else if (v === 'prompts') renderPrompts(main);
  else if (v === 'settings') renderSettings(main);
  else if (v === 'data') renderData(main);
  else if (v === 'logs') renderLogs(main);
  else if (v === 'usage') renderUsage(main);
  else if (v === 'extensions') renderExtensions(main);
  else if (v === 'prefs') renderPrefs(main);
  // 统一在 DOM 边界做英文替换：所有渲染器都不必关心 i18n
  if (typeof translateDom === 'function') translateDom(main);
}

/* ------------------------------------------------------------------ 概览 */
function dirTable(dirs) {
  if (!dirs || !dirs.length) return '<div class="empty">还没有目录数据，点「刷新」</div>';
  return `<table>
    <thead><tr><th>目录</th><th class="num">待翻译</th><th class="num">已翻译</th>
      <th class="num">有疑问</th><th>进度</th></tr></thead>
    <tbody>${dirs.map((d) => `<tr>
      <td class="mono">${esc(d.dir)}</td>
      <td class="num" style="color:${d.pending ? 'var(--warn)' : 'inherit'}">${d.pending}</td>
      <td class="num">${d.translated}/${d.total}</td>
      <td class="num" style="color:${d.disputed ? 'var(--err)' : 'inherit'}">${d.disputed || '—'}</td>
      <td><span class="mini-bar"><i style="width:${d.percent}%"></i></span>
          <span class="dim" style="margin-left:6px">${d.percent}%</span></td>
    </tr>`).join('')}</tbody>
  </table>`;
}

function renderOverview(main) {
  const p = state.project;
  if (!p) {
    main.innerHTML = pageHead('概览', '尚未连接项目') + `
      <div class="card">
        <p class="muted">先去「项目」填 ParaTranz API Token 和项目编号，然后点「测试连接」。</p>
        <button class="btn primary" id="goSettings">去配置</button>
      </div>`;
    $('#goSettings').onclick = () => switchView('settings');
    return;
  }

  const g = p.progress;
  const pending = Math.max(0, g.total - g.translated);
  const dirs = state.dirs || [];
  // 任务口径：平台把任务长期挂在 status=0（未结项），其中可能已经没有任何待翻译。
  // 只报"进行中"会让用户以为有这么多任务正在被处理（实测 8 个里 1 个是 0 条待翻译）。
  const ts = p.task_summary || { open: (p.tasks || []).filter((t) => t.status === 0).length,
                                with_pending: 0, pending_strings: 0 };
  // 文案不带「平台任务」前缀：页头自己加，右侧 kv 的标签已经有了
  const taskText = ts.open
    ? `${ts.open} 个未结项` + (ts.with_pending
        ? ` · ${ts.with_pending} 个还有待翻译（${ts.pending_strings} 条）`
        : ' · 均无待翻译词条')
    : '0 个';

  main.innerHTML = `
    ${pageHead(
      p.project.name || ('项目 ' + p.project.id),
      `${esc(p.project.source)} → ${esc(p.project.dest)}
       · 任务功能${p.project.task_enabled ? '已开启' : '<b style="color:var(--warn)">未开启</b>'}
       · 校对等级 ${p.project.review_mode}
       · ${p.tasks_error
          ? '<b style="color:var(--err)" title="' + esc(p.tasks_error) + '">平台任务读取失败</b>'
          : `平台任务 ${esc(taskText)}`}${
          state.projectAt ? ` · 数据更新于 ${fmtTime(state.projectAt / 1000)}` : ''}`,
      '<button class="btn" id="refreshProj">刷新</button>'
    )}

    ${state.llmReady ? '' : `
      <div class="notice warn">
        <b>还不能执行任务</b>：尚未配置模型 API。翻译、校对、术语、规范都要调用模型，
        请先到 <a href="#" data-goto="models">「模型」</a> 页填写 API 地址、模型名与 API Key。
      </div>`}

    <div class="metrics">
      ${metric('已翻译', `${g.translated}<small>/ ${g.total}</small>`, { ico: '▤' })}
      ${metric('进度', g.percent + '%', { cls: 'accent', ico: '◐' })}
      ${metric('待翻译', pending, { cls: pending ? 'warn' : '', ico: '◌' })}
      ${metric('已检查', g.checked, { ico: '✓' })}
      ${metric('已审核', g.reviewed, { ico: '◎' })}
      ${metric('有疑问', g.disputed, { cls: g.disputed ? 'err' : '', ico: '?' })}
      ${metric('文件', g.files, { ico: '▦' })}
      ${metric('词数', fmtNum(g.words), { ico: '#' })}
      ${renderMetricOrError('术语', p.terms, p.terms_error, { ico: '≡' })}
      ${metric('本地 TM', p.local_tm, { ico: '⧉' })}
    </div>

    <div class="split">
      <div>
        <h3>目录进度 <span class="cnt">${dirs.length} 个目录</span></h3>
        <div class="card tight">${dirTable(dirs)}</div>

        <h3>最近任务 <span class="cnt">${state.jobs.length} 个</span></h3>
        <div id="recentJobs"></div>
      </div>

      <div>
        <h3>给 Agent 下指令</h3>
        <div class="card">
          <div class="agent-box">
            <textarea class="field" id="agentText"
              placeholder="例如：把未翻译的翻完并校对 / 翻 100 条 / 整理术语 / 出汉化规范 / 全流程跑一遍"></textarea>
            <button class="btn primary" id="agentSend">执行</button>
          </div>
          <div class="hint">
            <button data-t="翻译 100 条">翻译 100 条</button>
            <button data-t="校对">校对</button>
            <button data-t="整理术语">整理术语</button>
            <button data-t="出汉化规范">出规范</button>
            <button data-t="分派任务">分派任务</button>
            <button data-t="全流程">全流程</button>
          </div>
          <div id="agentReply" class="dim" style="margin-top:8px;font-size:11.5px">${
            state.agentReplyHtml || ''}</div>
        </div>

        <h3>状态</h3>
        <div class="card">
          <div class="kv"><span class="k">演练模式</span>
            <span class="v" style="color:${state.settings && state.settings.dry_run ? 'var(--warn)' : 'var(--ok)'}">
              ${state.settings && state.settings.dry_run ? '开启（只预览不写）' : '关闭（真实写入）'}</span></div>
          <div class="kv"><span class="k">模型 API</span>
            <span class="v" style="color:${state.llmReady ? 'var(--ok)' : 'var(--err)'}">
              ${state.llmReady ? '已配置' : '未配置'}</span></div>
          <div class="kv"><span class="k">平台任务</span><span class="v">${esc(taskText)}</span></div>
          <div class="kv"><span class="k">本地 TM</span><span class="v">${p.local_tm} 条</span></div>
          <div class="kv"><span class="k">术语</span><span class="v">${p.terms} 条</span></div>
          <div class="kv"><span class="k">目录</span><span class="v">${dirs.length} 个</span></div>
        </div>
      </div>
    </div>
  `;

  $('#refreshProj').onclick = () => refreshAll();
  $('#agentSend').onclick = sendAgent;
  main.querySelectorAll('[data-goto]').forEach((a) => {
    a.onclick = (e) => { e.preventDefault(); switchView(a.dataset.goto); };
  });
  $$('.hint button').forEach((b) => {
    b.onclick = () => { $('#agentText').value = b.dataset.t; sendAgent(); };
  });
  $('#agentText').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) sendAgent();
  });
  if (!state.llmReady) {
    const ta = $('#agentText');
    if (ta) { ta.disabled = true; ta.placeholder = '请先在「模型」页配置 API 后再下指令'; }
    const btn = $('#agentSend');
    if (btn) btn.disabled = true;
  }
  renderRecentJobs();
}

function renderRecentJobs() {
  const box = $('#recentJobs');
  if (!box) return;
  const rows = state.jobs.slice(0, 5);
  box.innerHTML = rows.length
    ? rows.map(jobCard).join('')
    : '<div class="empty">还没有任务</div>';
  bindJobCards(box);
}

async function sendAgent() {
  const text = ($('#agentText').value || '').trim();
  if (!text) return;
  const btn = $('#agentSend');
  const ep = renderEpoch;      // 切页后 #agentReply 已经不在文档里了，别再往里写
  if (btn) btn.disabled = true;
  try {
    const res = await api('POST', '/api/agent', { text, execute: true });
    state.agentReplyHtml = agentReplyHtml(res);
    const box = ep === renderEpoch ? $('#agentReply') : null;
    if (box) box.innerHTML = state.agentReplyHtml;

    const jobs = res.job_ids || [];
    const needs = res.needs_confirm || [];
    if (jobs.length) {
      // 任务已经真的提交了：哪怕已经切页也要说清楚，免得用户以为没提交再点一次
      toast(`已提交 ${jobs.length} 个任务` + (box ? '' : '（页面已切换，去「任务」页看结果）'));
      await refreshJobs();
    } else if (needs.length) {
      toast(`${needs.length} 个动作需要你手动确认风险后再执行`, true);
    } else {
      toast('没有可执行的动作', true);
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    // 未配置模型时按钮本来就该是禁用的，别在这里无条件解禁
    if (btn) btn.disabled = !state.llmReady;
  }
}

/* Agent 回复区：把「需要人工确认的危险动作」明写出来，别静默丢弃 */
function agentReplyHtml(res) {
  const steps = res.steps || [];
  const needs = res.needs_confirm || [];
  const stepLine = steps.length
    ? `<div class="mono muted" style="margin-top:6px">${steps
        .map((s) => `${esc(s.tool)}(${esc(JSON.stringify(s.args || {}))})`).join(' → ')}</div>`
    : '';
  const needBox = needs.length ? `
    <div class="notice warn" style="margin-top:8px;font-size:12px">
      <b>以下动作会在本机执行代码，需到对应页面勾选风险确认后再执行：</b>
      <ul style="margin:6px 0 0 18px;padding:0;line-height:1.7">
        ${needs.map((n) => `<li><b class="mono">${esc(n.tool)}</b>
          <span class="mono dim" style="font-size:11px">${esc(JSON.stringify(n.args || {}))}</span>
          ${n.why ? `<div class="dim" style="font-size:11px">${esc(n.why)}</div>` : ''}</li>`).join('')}
      </ul>
      <div class="dim" style="font-size:11px;margin-top:4px">
        这些步骤<b>没有</b>被自动提交；去「提取」页选好提取器，点「开始提取」并阅读风险确认。
      </div>
    </div>` : '';
  return `${esc(res.reply || '')} <span class="tag">${esc(res.source || '')}</span>`
    + stepLine + needBox;
}

/* ------------------------------------------------------------------ 任务 */
function jobCard(j) {
  const dur = j.duration != null ? `${j.duration}s` : '—';
  return `
  <div class="job ${state.openJobs.has(j.id) ? 'open' : ''}" data-job="${esc(j.id)}">
    <div class="hd">
      <span class="status ${esc(j.status)}">${esc(j.status)}</span>
      <span class="name">${esc(j.tool)}</span>
      <span class="meta">${esc(j.origin)} · ${fmtTime(j.created_at)} · ${dur}</span>
      <div class="spacer"></div>
      ${j.result && (j.result.written || j.result.would_write)
        ? `<button class="btn sm" data-jobstrings="${esc(j.id)}" style="margin-right:6px"
             title="查看这个任务写入/判定过的具体词条">本批 ${j.result.written || j.result.would_write} 条</button>`
        : ''}
      ${j.status === 'running' || j.status === 'queued'
        ? `<button class="btn danger" data-cancel="${esc(j.id)}" style="padding:3px 9px;font-size:12px">取消</button>`
        : ''}
    </div>
    <div class="bd">
      ${j.error ? `<div style="color:var(--err);font-size:12.5px">${esc(j.error)}</div>` : ''}
      ${j.result ? `<pre class="result">${esc(JSON.stringify(j.result, null, 2))}</pre>` : ''}
      ${j.logs && j.logs.length
        ? `<pre class="result">${j.logs.map((l) => `[${fmtTime(l.ts)}] ${esc(l.msg)}`).join('\n')}</pre>`
        : '<div class="muted" style="font-size:12.5px">暂无日志</div>'}
    </div>
  </div>`;
}

function bindJobCards(root) {
  root.querySelectorAll('.job').forEach((el) => {
    const id = el.dataset.job;
    const drill = el.querySelector('[data-jobstrings]');
    if (drill) {
      drill.onclick = (ev) => {
        ev.stopPropagation();
        openJobStrings(drill.dataset.jobstrings);
      };
    }
    el.querySelector('.hd').onclick = async (ev) => {
      if (ev.target.dataset.cancel || ev.target.dataset.jobstrings) return;
      if (state.openJobs.has(id)) {
        state.openJobs.delete(id);
        el.classList.remove('open');
      } else {
        state.openJobs.add(id);
        el.classList.add('open');
        try {
          const { job } = await api('GET', `/api/jobs/${id}`);
          const idx = state.jobs.findIndex((x) => x.id === id);
          if (idx >= 0) state.jobs[idx] = { ...state.jobs[idx], ...job };
          el.outerHTML = jobCard(job);
          bindJobCards(root);
        } catch { /* 忽略 */ }
      }
    };
    const btn = el.querySelector('[data-cancel]');
    if (btn) {
      btn.onclick = async (ev) => {
        ev.stopPropagation();
        try {
          await api('POST', `/api/jobs/${id}/cancel`);
          toast('已请求取消');
        } catch (e) { toast(e.message, true); }
      };
    }
  });
}

function renderJobs(main) {
  const running = state.jobs.filter((j) => j.status === 'running' || j.status === 'queued').length;
  main.innerHTML = `
    ${pageHead('任务', `共 ${state.jobs.length} 个 · 运行中 ${running} 个 · 点标题展开日志`,
      '<button class="btn" id="recentWrites" title="按本机账本列出最近 24 小时写入的词条">最近写入</button>'
      + '<button class="btn" id="refreshJobs" style="margin-left:6px">刷新</button>')}
    <div id="liveHost">${livePanel()}</div>
    <div id="jobList">${state.jobs.length ? state.jobs.map(jobCard).join('') : '<div class="empty">还没有任务</div>'}</div>`;
  $('#refreshJobs').onclick = () => refreshJobs(true);
  const recent = $('#recentWrites');
  if (recent) recent.onclick = () => openLedgerStrings(24);
  bindJobCards($('#jobList'));
  bindLiveAndRows($('#liveHost'));
}

/* 刷新页面后实时面板会空：从正在跑的那个任务把最近一批词条捞回来
 * （后端 `GET /api/jobs/{id}` 的 items 就是为这个准备的） */
async function hydrateLive(jobId) {
  if (!jobId || state.live.size) return;
  try {
    const { job } = await api('GET', `/api/jobs/${jobId}`);
    // await 期间 SSE 可能已经推进过：只在仍然为空时补，且只补缺失的 id，
    // 否则会把实时状态回写成更旧的快照
    if (state.live.size || !job || !Array.isArray(job.items)) return;
    state.liveJob = job.id;
    job.items.forEach((it) => {
      const id = Number(it && it.id);
      if (!Number.isFinite(id) || state.live.has(id)) return;
      state.live.set(id, { ...it, id, ts: Number(it.ts) || 0 });
    });
    paintLive();
  } catch { /* 拿不到就不显示，不挡其他功能 */ }
}

async function refreshJobs(rerender = false) {
  try {
    const { jobs } = await api('GET', '/api/jobs?limit=50');
    // 列表接口不带 logs（with_logs=False）；直接替换会把已展开卡片的日志抹掉，
    // 所以按 id 合并回旧的 logs
    const prevById = new Map((state.jobs || []).map((j) => [j.id, j]));
    state.jobs = (jobs || []).map((j) => {
      const old = prevById.get(j.id);
      return old && old.logs && old.logs.length ? { ...j, logs: old.logs } : j;
    });
    const running = jobs.filter((j) => j.status === 'running' || j.status === 'queued').length;
    const badge = $('#badgeJobs');
    if (badge) badge.textContent = running > 0 ? String(running) : '';
    const alive = jobs.find((j) => j.status === 'running');
    if (alive) hydrateLive(alive.id);
    if (rerender || state.view === 'jobs') render();
    else if (state.view === 'overview') renderRecentJobs();
  } catch { /* 静默 */ }
}

/* ------------------------------------------------------------------ 文件 */
function renderFiles(main) {
  const rows = state.files || [];
  const dirs = state.dirs || [];
  const fv = state.fileView;
  main.innerHTML = `
    ${pageHead('文件',
      `${rows.length} 个文件 · ${dirs.length} 个目录 · 点文件行展开该文件的全部词条，可就地改译文`,
      '<button class="btn" id="refreshFiles">刷新</button>')}
    ${state.detail ? strDetailHtml(state.detail) : ''}
    <div id="batchHost">${batchBarHtml()}</div>
    ${dirs.length ? dirs.map((d) => {
      const items = rows.filter((f) => f.dir === d.dir);
      return `
      <div class="dirblock">
        <div class="dirhead">
          <span class="dname mono">${esc(d.dir)}</span>
          <span class="muted">共 ${d.files} 文件 · ${d.total} 词条</span>
          <div class="spacer"></div>
          <span class="muted">待翻译 <b style="color:${d.pending ? 'var(--warn)' : 'inherit'}">${d.pending}</b></span>
          <span class="mini-bar"><i style="width:${d.percent}%"></i></span>
          <span class="muted" style="font-size:12px">${d.percent}%</span>
        </div>
        <table>
          <thead><tr>
            <th>文件</th><th class="num">总条数</th><th class="num">待翻译</th>
            <th class="num">已翻译</th><th class="num">有疑问</th><th>进度</th>
          </tr></thead>
          <tbody>
            ${items.map((f) => {
              const open = String(fv.fid) === String(f.id);
              return `
              <tr class="filerow${open ? ' open' : ''}" data-fid="${esc(f.id)}">
                <td class="mono"><span class="caret">${open ? '▾' : '▸'}</span>${esc(f.name.split('/').pop())}</td>
                <td class="num">${f.total}</td>
                <td class="num" style="color:${f.pending ? 'var(--warn)' : 'inherit'}">${f.pending}</td>
                <td class="num">${f.percent}%</td>
                <td class="num" style="color:${f.disputed ? 'var(--err)' : 'inherit'}">${f.disputed || '—'}</td>
                <td><span class="mini-bar"><i style="width:${f.percent}%"></i></span></td>
              </tr>
              ${open ? `<tr class="filepanel"><td colspan="6">${fileStringsPanelHtml(f)}</td></tr>` : ''}`;
            }).join('')}
          </tbody>
        </table>
      </div>`;
    }).join('') : '<div class="empty">还没有数据，先点刷新或去设置里连项目</div>'}`;

  $('#refreshFiles').onclick = () => loadFiles(true);
  bindFileRows(main);
  bindFilePanel(main);
  bindDetail(main);
  bindBatch(main);
  if (fv.fid && !fv.loading && fv.loadedKey !== fileQueryKey()) loadFileStrings();
}

function bindFileRows(root) {
  root.querySelectorAll('tr.filerow').forEach((tr) => {
    tr.onclick = () => {
      const fid = String(tr.dataset.fid || '');
      const fv = state.fileView;
      if (String(fv.fid) === fid) {          // 再点一次收起
        fv.fid = '';
        render();
        return;
      }
      Object.assign(fv, {
        fid, page: 1, stage: '', q: '', items: [], total: 0, pages: 0, loadedKey: '',
      });
      closeDetail();
      render();                              // 先铺开面板（显示"读取中"），再拉数据
    };
  });
}

function fileQueryKey() {
  const fv = state.fileView;
  return `${fv.fid}|${fv.page}|${fv.size}|${fv.stage}`;
}

/* 展开后的文件面板：该文件全部词条 + 就地编辑 + 实时回填 */
function fileStringsPanelHtml(f) {
  const fv = state.fileView;
  const q = fv.q.trim().toLowerCase();
  const shown = q
    ? fv.items.filter((r) => (r.original + '\u0000' + r.translation).toLowerCase().includes(q))
    : fv.items;
  return `<div class="filepanel-in">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px">
      <b class="mono" style="font-size:12px">${esc(f.name)}</b>
      <span class="muted" style="font-size:11.5px">共 ${fv.total} 条${
        q ? ` · 本页匹配 ${shown.length}` : ''}</span>
      <div class="spacer"></div>
      <select class="field" id="fpStage" style="width:120px">
        ${[['', '全部阶段'], ['0', '未翻译'], ['1', '已翻译'], ['2', '有疑问'],
           ['3', '已检查'], ['5', '已审核'], ['9', '锁定']]
          .map(([v, label]) => `<option value="${v}"${fv.stage === v ? ' selected' : ''}>${label}</option>`)
          .join('')}
      </select>
      <input class="field" id="fpQ" placeholder="在本页内过滤原文/译文" style="width:180px"
        value="${esc(fv.q)}">
      <select class="field" id="fpSize" style="width:100px">
        ${[25, 50, 100, 200].map((n) => `<option value="${n}"${
          fv.size === n ? ' selected' : ''}>${n} 条/页</option>`).join('')}
      </select>
      <button class="btn sm" id="fpPrev"${fv.page <= 1 ? ' disabled' : ''}>上一页</button>
      <span class="dim" style="font-size:11.5px">${fv.page} / ${fv.pages || 1}</span>
      <button class="btn sm" id="fpNext"${fv.page >= (fv.pages || 1) ? ' disabled' : ''}>下一页</button>
    </div>
    ${shown.length ? `<table class="fp-table">
      <thead><tr><th style="width:30px"><input type="checkbox" id="fpPickAll"></th>
        <th style="width:36%">原文</th><th style="width:38%">译文（点一下即可改）</th>
        <th style="width:96px">阶段</th><th style="width:150px;white-space:nowrap">AI / 操作</th></tr></thead>
      <tbody>${shown.map((r) => `<tr data-sid="${esc(r.id)}">
        <td><input type="checkbox" data-pick="${esc(r.id)}"${
          state.batch.has(Number(r.id)) ? ' checked' : ''}></td>
        <td style="font-size:12px" data-editable="${esc(r.id)}">
          <div class="st-tr-text st-orig" data-no-i18n>${esc(r.original)}</div></td>
        <td style="font-size:12px" data-editable="${esc(r.id)}">
          <div class="st-tr-text st-trans" data-no-i18n>${r.translation
            ? esc(r.translation)
            : '<span class="dim">（空）</span>'}</div></td>
        <td style="white-space:nowrap"><span class="st-stage">${stageTag(r.stage)}</span></td>
        <td><span class="tag st-live"></span>
          <button class="btn sm" data-detail="${esc(r.id)}">详情</button></td>
      </tr>`).join('')}</tbody>
    </table>`
    : `<div class="empty">${fv.loading ? '读取中…'
        : (q ? '本页没有匹配的条目' : '这个阶段下没有词条')}</div>`}
    ${fv.total > fv.size ? `<div class="dim" style="font-size:11px;margin-top:6px">
      只显示当前页；改完点这一行右侧的「详情」可看上下文与两份记录。</div>` : ''}
  </div>`;
}

function bindFilePanel(main) {
  const fv = state.fileView;
  const reload = (patch) => {
    Object.assign(fv, patch);
    closeDetail();
    state.batch.clear();
    render();
    return loadFileStrings();
  };
  const st = $('#fpStage');
  if (st) st.onchange = (e) => reload({ stage: e.target.value, page: 1 });
  const sz = $('#fpSize');
  if (sz) sz.onchange = (e) => reload({ size: Number(e.target.value) || 50, page: 1 });
  const prev = $('#fpPrev');
  if (prev) prev.onclick = () => reload({ page: Math.max(1, fv.page - 1) });
  const next = $('#fpNext');
  if (next) next.onclick = () => reload({ page: fv.page + 1 });
  const qb = $('#fpQ');
  if (qb) {
    qb.oninput = () => {                     // 只筛 DOM，不重绘（重绘会失焦）
      fv.q = qb.value;
      const k = fv.q.trim().toLowerCase();
      const panel = qb.closest('.filepanel-in');
      if (!panel) return;
      panel.querySelectorAll('.fp-table tbody tr').forEach((tr) => {
        const cells = tr.querySelectorAll('.st-tr-text');
        const hay = [...cells].map((c) => c.textContent).join('\u0000').toLowerCase();
        tr.style.display = !k || hay.includes(k) ? '' : 'none';
      });
    };
  }
  // 点**译文**格子 → 就地变成编辑框。原文格子不绑：点原文却弹出译文编辑器
  // 会让人以为改的是原文（实测过，容易改错方向）。
  main.querySelectorAll('[data-editable]').forEach((td) => {
    if (!td.querySelector('.st-trans')) return;
    td.onclick = (ev) => {
      if (ev.target.closest('button')) return;
      if (td.querySelector('textarea')) return;
      openInlineEditor(td, Number(td.dataset.editable));
    };
  });
  main.querySelectorAll('[data-detail]').forEach((b) => {
    b.onclick = () => openString(Number(b.dataset.detail));
  });
}

/* 就地编辑：把译文格子换成 textarea + 保存/取消。
 * 不用 contenteditable：译文里本来就可能带 HTML 控制符，改起来风险更大。 */
function openInlineEditor(td, id) {
  const row = (state.fileView.items || []).find((r) => r.id === id)
    || (state.str.items || []).find((r) => r.id === id)
    || { id, translation: '' };
  // 存**整个格子**的 HTML：只存内层会把 `.st-trans` 这层也丢掉，
  // 取消之后实时回填就再也找不到这一格了（实测踩到）。
  const original = td.innerHTML;
  td.innerHTML = `<textarea class="field" style="width:100%;min-height:52px;font-size:12px"
      data-no-i18n>${esc(row.translation || '')}</textarea>
    <div style="display:flex;gap:6px;margin-top:4px">
      <button class="btn sm primary" data-save="1">保存</button>
      <button class="btn sm" data-cancel="1">取消</button>
      <span class="dim" style="font-size:11px">Ctrl+Enter 保存</span>
    </div>`;
  const box = td.querySelector('textarea');
  box.focus();
  const cancel = () => {
    // 取消时用**本地最新值**重建，而不是回滚到打开编辑器前的那份快照：
    // 编辑期间 AI 可能刚产出这条译文，回滚快照会把它抹掉。
    const cur = (state.fileView.items || []).find((r) => r.id === id)
      || (state.str.items || []).find((r) => r.id === id)
      || row;
    if (cur && cur.translation) {
      td.innerHTML = `<div class="st-tr-text st-trans" data-no-i18n>${
        esc(cur.translation)}</div>`;
    } else {
      td.innerHTML = original;
    }
  };
  const save = async () => {
    const ok = await saveInline(id, box.value);
    if (ok) return;          // 成功后由重绘接管
    cancel();
  };
  td.querySelector('[data-save]').onclick = save;
  td.querySelector('[data-cancel]').onclick = cancel;
  box.onkeydown = (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); save(); }
    if (ev.key === 'Escape') { ev.preventDefault(); cancel(); }
  };
}

/* 就地保存一条译文。成功返回 true，并把本地列表与行内文本一起更新。 */
async function saveInline(id, value) {
  try {
    const res = await api('PUT', '/api/strings/' + id, { translation: value });
    if (res.dry_run) {
      toast('演练模式：没有写平台。将提交 ' + JSON.stringify(res.would_write));
      return false;
    }
    const item = res.item;
    if (item) {
      patchLocalItem(id, item);
      applyLiveItems([{ id, translation: item.translation, stage: item.stage, status: 'written' }]);
    }
    // 平台确认已写但回读失败：不能说"没保存"，也不能假装界面上的值就是平台值
    toast(res.note ? res.note : '已写入平台');
    if (String(state.fileView.fid)) loadFileStrings();   // 重拉当前页：阶段/计数才是平台的真相
    return true;
  } catch (e) {
    toast(e.message, true);
    return false;
  }
}

async function loadFileStrings() {
  const fv = state.fileView;
  if (!fv.fid) return;
  const key = fileQueryKey();
  fv.loading = true;
  try {
    const params = new URLSearchParams({
      page: String(fv.page), page_size: String(fv.size), file: String(fv.fid),
    });
    if (fv.stage !== '') params.set('stage', fv.stage);
    const d = await api('GET', '/api/strings?' + params.toString());
    fv.items = d.items || [];
    fv.total = d.total || 0;
    fv.pages = d.pages || 0;
    fv.page = d.page || fv.page;
  } catch (e) {
    fv.items = [];
    fv.total = 0;
    fv.pages = 0;
    toast(e.message, true);
  } finally {
    fv.loading = false;
    fv.loadedKey = key;
  }
  if (state.view === 'files') render();
}

async function loadFiles(rerender = false) {
  try {
    const { files, dirs } = await api('GET', '/api/files');
    state.files = files;
    state.dirs = dirs || [];
    if (rerender || state.view === 'files') render();
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ 词条工作台
 *
 * 为什么需要这一页：翻译与校对都是"模型批量改远端数据"，模型会出错。
 * 没有词条级视图时，人只能在 ParaTranz 网站里逐条翻 —— 出了问题既看不到
 * 是哪一条，也改不动，只能全盘信任报表。
 *
 * 两个入口共用同一份实时数据（`state.live`）：
 * - 「任务」页顶部的实时面板：AI 此刻在处理哪些词条、产出/被拦/失败各多少；
 * - 「词条」页：按阶段/文件浏览、改译文、看平台改动记录。
 */
const STR_STAGE_NAME = {
  0: '未翻译', 1: '已翻译', 2: '有疑问', 3: '已检查', 5: '已审核', 9: '锁定', '-1': '隐藏',
};
const STR_LIVE_NAME = {
  running: '处理中', ok: '已产出', tm: '记忆命中', blocked: '被拦', failed: '失败',
  written: '已写入', ended: '未回结果',
  pass: '校对通过', dispute: '打回', reset: '重置未翻译', skip: '未判定',
};
/* 校对结果 → 阶段徽章（写回事件自带 stage 时以它为准） */
const LIVE_STATUS_STAGE = { pass: 3, dispute: 2, reset: 0 };

/* 截断不能切在代理对中间：emoji/增补平面字符会被切成半个（显示成 ���） */
function clip(text, n) {
  const arr = Array.from(String(text == null ? '' : text));
  return arr.length > n ? arr.slice(0, n).join('') + '…' : arr.join('');
}

function stageTag(stage) {
  const v = Number(stage);
  const cls = v === 1 ? 'ok' : (v === 2 ? 'w' : '');
  return `<span class="tag ${cls}">${esc(STR_STAGE_NAME[v] || String(stage))}</span>`;
}

/* 文件路径只留末两级：真实项目里 `main.csv` 可能有几十个同名，
 * 只显示文件名等于没显示；显示全路径又会把表格挤变形。 */
function strFileTail(name) {
  const parts = String(name || '').split('/').filter(Boolean);
  return parts.length > 2 ? '…/' + parts.slice(-2).join('/') : parts.join('/');
}

/* 实时面板：优先显示"处理中"，再补最近有结果的，两类都只留 10 条 —— 面板要一眼看完 */
function livePanel() {
  // 按 ts 排序再取尾部：Map 的插入序不会因为值更新而移动，
  // 直接 slice(-10) 拿到的是"最晚首次出现的 10 个 id"，不是最近更新的 10 条。
  const all = [...state.live.values()].sort((a, b) => (a.ts || 0) - (b.ts || 0));
  if (!all.length) return '';
  const cnt = {};
  all.forEach((x) => { cnt[x.status] = (cnt[x.status] || 0) + 1; });
  const running = all.filter((x) => x.status === 'running').slice(-10);
  const rest = all.filter((x) => x.status !== 'running').slice(-10).reverse();
  const rows = running.concat(rest);
  return `<div class="card tight" id="liveStrip">
    <div style="display:flex;gap:10px;align-items:center;padding:8px 10px;flex-wrap:wrap">
      <b>AI 正在处理的词条</b>
      <span class="muted" style="font-size:11.5px">${
        ['running', 'ok', 'tm', 'blocked', 'failed', 'written', 'ended']
          .filter((k) => cnt[k])
          .map((k) => `${STR_LIVE_NAME[k]} ${cnt[k]}`).join(' · ') || '等待模型返回…'}</span>
      <div class="spacer"></div>
      <span class="dim mono" style="font-size:11px">${esc(state.liveJob || '')}</span>
    </div>
    <table>
      <thead><tr><th style="width:16%">状态</th><th>原文</th><th>模型产出 / 原因</th></tr></thead>
      <tbody>${rows.map((r) => `<tr data-sid="${esc(r.id)}" style="cursor:pointer">
        <td style="white-space:nowrap"><span class="tag ${r.status === 'failed' || r.status === 'blocked'
          ? 'w' : (r.status === 'written' || r.status === 'ok' ? 'ok' : '')}"
          >${esc(STR_LIVE_NAME[r.status] || r.status)}${r.dry_run ? '（演练）' : ''}</span></td>
        <td style="font-size:12px"><span data-no-i18n>${esc(clip(r.original, 90))}</span></td>
        <td style="font-size:12px">${r.translation
          ? `<span data-no-i18n>${esc(clip(r.translation, 90))}</span>`
          : `<span class="dim">${esc(clip(r.reason || '—', 60))}</span>`}${
          r.translation && r.reason ? ` <span class="dim">（${esc(clip(r.reason, 40))}）</span>` : ''}</td>
      </tr>`).join('')}</tbody>
    </table>
  </div>`;
}

/* 收到逐条事件：只换面板内容，不整页重绘（否则滚动位置、展开状态全丢） */
function pushItems(jobId, items, ts) {
  if (!Array.isArray(items)) return;
  // 换任务了就把上一轮清掉：否则上个任务遗留的 running 会永远占着展示位，
  // 右上角的 job id 却已经是新的，数字和 id 对不上。
  if (jobId && jobId !== state.liveJob) {
    state.live.clear();
    state.liveJob = jobId;
  }
  items.forEach((it) => {
    const id = Number(it && it.id);
    if (!Number.isFinite(id)) return;   // 畸形 id 不进 Map，避免 NaN 行与重复行
    const prev = state.live.get(id) || {};
    state.live.set(id, { ...prev, ...it, id, ts: ts || Date.now() / 1000 });
  });
  // 面板只显示最近一段，超出的老条目清掉，避免长任务把内存与渲染拖垮
  if (state.live.size > 400) {
    const ordered = [...state.live.entries()].sort((a, b) => (a[1].ts || 0) - (b[1].ts || 0));
    ordered.slice(0, state.live.size - 400).forEach(([k]) => state.live.delete(k));
  }
  applyLiveItems(items);   // 词条列表/文件视图就地刷新
  paintLive();
}

/* 把 AI 的产出/判定就地写回已渲染的词条行（不重绘整页）。
 *
 * 为什么必须就地补：翻译/校对是几百秒的长任务，等它跑完再重绘，用户在整个过程里
 * 看不到任何变化；而整页重绘会丢掉滚动位置、展开状态，还打断正在输入的文本框。
 */
function applyLiveItems(items) {
  if (!Array.isArray(items)) return;
  items.forEach((it) => {
    const id = Number(it && it.id);
    if (!Number.isFinite(id)) return;
    // 本地数据也要跟着变，否则翻页/重绘后这些结果又"消失"了
    patchLocalItem(id, it);

    const rows = document.querySelectorAll(`[data-sid="${id}"]`);
    rows.forEach((row) => {
      // 演练模式：模型产出了，但平台没变 —— 只显示状态，绝不改列表里的译文与阶段
      if (it.dry_run) {
        const chipDry = row.querySelector('.st-live');
        if (chipDry) {
          chipDry.textContent = (STR_LIVE_NAME[it.status] || it.status || '') + '（演练）';
          chipDry.className = 'tag st-live';
          if (it.translation) chipDry.title = '预览：' + it.translation;
        }
        return;
      }
      const box = row.querySelector('[data-edit]');
      // 用户正在改这一格时绝不覆盖他的输入
      if (box && document.activeElement !== box && typeof it.translation === 'string'
          && it.translation) {
        box.value = it.translation;
        box.dataset.dirty = '';
      }
      // **只动译文格**：querySelector('.st-tr-text') 取到的是第一个（原文），
      // 实测会把产出写进原文列、把原文冲掉。
      const cell = row.querySelector('.st-trans');
      if (cell && typeof it.translation === 'string' && it.translation) {
        cell.textContent = it.translation;
      }
      const stage = it.stage != null ? Number(it.stage) : LIVE_STATUS_STAGE[it.status];
      const badge = row.querySelector('.st-stage');
      if (badge && stage != null && !Number.isNaN(stage)) {
        badge.outerHTML = `<span class="st-stage">${stageTag(stage)}</span>`;
      }
      const chip = row.querySelector('.st-live');
      if (chip) {
        chip.textContent = STR_LIVE_NAME[it.status] || it.status || '';
        chip.className = 'tag st-live ' + (it.status === 'dispute' || it.status === 'failed'
          || it.status === 'blocked' ? 'w' : 'ok');
        if (it.reason) chip.title = it.reason;
      }
    });
  });
}

/* 同步到本地列表（词条页 / 文件页各有一份） */
function patchLocalItem(id, it) {
  if (it.dry_run) return;        // 演练的预览不进本地数据
  const stage = it.stage != null ? Number(it.stage) : LIVE_STATUS_STAGE[it.status];
  [state.str.items, state.fileView.items].forEach((arr) => {
    const row = (arr || []).find((r) => r.id === id);
    if (!row) return;
    if (typeof it.translation === 'string' && it.translation) row.translation = it.translation;
    if (stage != null && !Number.isNaN(stage)) row.stage = stage;
  });
}

/* 任务进入终态后，仍挂在"处理中"的条目要给个说法（否则永远在飞） */
function settleLive(status) {
  let changed = false;
  state.live.forEach((v, k) => {
    if (v.status !== 'running') return;
    state.live.set(k, {
      ...v, status: 'ended',
      reason: v.reason || `任务已结束（${status}），这一条没有收到结果`,
    });
    changed = true;
  });
  if (changed) paintLive();
}

function paintLive() {
  const host = $('#liveHost');
  if (!host) return;
  host.innerHTML = livePanel();       // 没有数据时就是空，锚点仍在，首条事件不会丢
  bindLiveAndRows(host);
}

async function renderStrings(main) {
  const ep = renderEpoch;
  const s = state.str;
  // 判据用「这批数据是哪个查询条件拉回来的」，不能用 items.length===0。
  // 后者在"查无结果 / 请求失败"时会反复触发拉取（render 里又 render），变成自激循环。
  // 任务下钻模式：数据已经在 items 里（来自 /api/jobs/{id}/strings），不再按页取
  if (!s.jobId && s.loadedKey !== strQueryKey() && !s.loading) {
    await loadStrings();
    if (ep !== renderEpoch) return;
  }
  const files = state.files || [];
  const q = s.q.trim().toLowerCase();
  const shown = q
    ? s.items.filter((r) => (r.original + '\u0000' + r.translation).toLowerCase().includes(q))
    : s.items;

  main.innerHTML = `
    ${pageHead('词条',
      `${s.total} 条 · 第 ${s.page} / ${s.pages || 1} 页 · 点任意行查看原文、上下文与处理记录`,
      '<button class="btn" id="stRefresh">刷新</button>')}
    <div id="liveHost">${livePanel()}</div>
    ${s.jobId ? `<div class="card" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <b>任务 <span class="mono">${esc(s.jobId)}</span> 的 ${s.total} 条</b>
      <span class="dim" style="font-size:11.5px">这些是该任务写入/判定过的词条，可就地改、可批量操作</span>
      <div class="spacer"></div>
      <button class="btn sm" id="stExitJob">返回全部词条</button>
    </div>` : ''}
    <div id="batchHost">${batchBarHtml()}</div>
    <div class="card"${s.jobId ? ' style="display:none"' : ''}>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <select class="field" id="stStage" style="width:132px">
          ${['0', '1', '2', '3', '5', '9', ''].map((v) => `<option value="${v}"${
            s.stage === v ? ' selected' : ''}>${v === '' ? '全部阶段' : STR_STAGE_NAME[v]}</option>`).join('')}
        </select>
        <select class="field" id="stFile" style="width:300px">
          <option value="">全部文件</option>
          ${files.map((f) => `<option value="${f.id}"${
            String(s.file) === String(f.id) ? ' selected' : ''}>${esc(f.name)}（待翻 ${f.pending}）</option>`).join('')}
        </select>
        <select class="field" id="stSize" style="width:104px">
          ${[25, 50, 100, 200].map((n) => `<option value="${n}"${
            s.size === n ? ' selected' : ''}>${n} 条/页</option>`).join('')}
        </select>
        <input class="field" id="stQ" placeholder="在本页内过滤原文/译文" style="width:200px"
          value="${esc(s.q)}">
        <span class="dim" id="stFilterInfo" style="font-size:11.5px"></span>
        <div class="spacer"></div>
        <button class="btn" id="stPrev"${s.page <= 1 ? ' disabled' : ''}>上一页</button>
        <button class="btn" id="stNext"${s.page >= (s.pages || 1) ? ' disabled' : ''}>下一页</button>
      </div>
    </div>
    ${state.detail ? strDetailHtml(state.detail) : ''}
    <div class="card tight" id="stTable">
      ${shown.length ? `<table>
        <thead><tr><th style="width:30px"><input type="checkbox" id="stPickAll"></th>
          <th style="width:40%">原文</th><th style="width:38%">译文</th>
          <th style="width:96px">阶段</th><th>文件</th></tr></thead>
        <tbody>${shown.map((r) => `<tr data-sid="${esc(r.id)}"
          data-q="${esc((r.original + '\u0000' + r.translation).toLowerCase())}"
          style="cursor:pointer${String(state.detailId) === String(r.id) ? ';background:var(--panel-2)' : ''}">
          <td><input type="checkbox" data-pick="${esc(r.id)}"${
            state.batch.has(Number(r.id)) ? ' checked' : ''}></td>
          <td style="font-size:12px"><span class="st-orig" data-no-i18n>${
            esc(clip(r.original, 200))}</span></td>
          <td style="font-size:12px">${r.translation
            ? `<span class="st-trans" data-no-i18n>${esc(clip(r.translation, 200))}</span>`
            : '<span class="dim st-trans">（空）</span>'}</td>
          <td style="white-space:nowrap"><span class="st-stage">${stageTag(r.stage)}</span></td>
          <td class="mono dim" style="font-size:11px" data-no-i18n>${esc(strFileTail(r.file_name))}</td>
        </tr>`).join('')}</tbody>
      </table>`
      : `<div class="empty">${
          s.loading ? '读取中…'
          : (q ? '本页没有匹配的条目（平台不支持全库搜索，只能在这里过滤当前页）'
               : '这个条件下没有词条。换阶段或文件再看')}</div>`}
    </div>`;

  const exitJob = $('#stExitJob');
  if (exitJob) {
    exitJob.onclick = () => {
      Object.assign(state.str, { jobId: '', items: [], loadedKey: '', page: 1, q: '', stage: '' });
      closeDetail();
      render();
      loadStrings();
    };
  }
  $('#stRefresh').onclick = async () => {
    closeDetail();
    s.loadedKey = '';          // 强制重拉（否则 key 没变就不会再请求）
    await loadStrings();
  };
  const reload = (patch) => {
    Object.assign(s, patch);
    closeDetail();
    state.batch.clear();     // 换页/换筛选就清空选择：别让批量操作打到看不见的条目上
    return loadStrings();
  };
  $('#stStage').onchange = (e) => reload({ stage: e.target.value, page: 1 });
  $('#stFile').onchange = (e) => reload({ file: e.target.value, page: 1 });
  $('#stSize').onchange = (e) => reload({ size: Number(e.target.value) || 50, page: 1 });
  // 过滤只动 DOM，不重绘整页：重绘会重建输入框 → 打一个字就失焦，根本没法连续输入
  const qBox = $('#stQ');
  if (qBox) {
    qBox.oninput = () => {
      s.q = qBox.value;
      filterStringsRows(s.q);
    };
    if (s.q) filterStringsRows(s.q);
  }
  $('#stPrev').onclick = () => reload({ page: Math.max(1, s.page - 1) });
  $('#stNext').onclick = () => reload({ page: s.page + 1 });
  bindLiveAndRows(main);
  bindDetail(main);
  bindBatch(main);
}

/* 只筛 DOM，不重绘：重绘会重建输入框（打一个字就失焦）。
 * 范围限定在词条表内 —— 实时面板的行也带 data-sid，不能被一起隐藏。
 * 匹配只认原文/译文（data-q），不拼 tr.textContent：后者会把阶段标签与文件名
 * 也算进去，搜「已翻译」或「main」时结果完全不是用户要的。 */
function filterStringsRows(q) {
  const box = $('#stTable');
  if (!box) return;
  const k = String(q || '').trim().toLowerCase();
  let n = 0;
  box.querySelectorAll('tbody tr').forEach((tr) => {
    const hit = !k || String(tr.dataset.q || '').includes(k);
    tr.style.display = hit ? '' : 'none';
    if (hit) n++;
  });
  const info = $('#stFilterInfo');
  if (info) info.textContent = k ? `本页匹配 ${n} 条` : '';
}

/* ---------- 批量操作 ----------
 *
 * 校对打回、术语统一、误译清理都是成片的活儿，逐条点会把一下午的活干成三天；
 * 平台侧本来就是批量接口。选择集放在 `state.batch`（两页共用一套按钮实现），
 * 换页/换筛选就清空 —— 免得"以为选的是这一页，实际操作了上一页的条目"。
 */
function batchBarHtml() {
  const n = state.batch.size;
  if (!n) return '';
  const stageBtns = [[1, '已翻译'], [2, '有疑问'], [3, '已检查'], [5, '已审核'], [0, '未翻译']]
    .map(([v, label]) => `<button class="btn sm" data-bstage="${v}">标为${label}</button>`).join('');
  return `<div class="card" id="batchBar"
      style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
    <b>已选 ${n} 条</b>
    <span class="dim" style="font-size:11.5px">批量操作会直接改平台数据</span>
    <div class="spacer"></div>
    ${stageBtns}
    <button class="btn sm" data-bclear="1">清空译文</button>
    <button class="btn sm primary" data-bretrans="1">重译选中</button>
    <button class="btn sm" data-bnone="1">取消选择</button>
  </div>`;
}

function paintBatchBar() {
  const host = $('#batchHost');
  if (!host) return;
  host.innerHTML = batchBarHtml();
  bindBatchActions();
}

function bindBatchActions() {
  const host = $('#batchHost');
  if (!host) return;
  host.querySelectorAll('[data-bstage]').forEach((b) => {
    b.onclick = () => batchApply('stage', Number(b.dataset.bstage));
  });
  const clr = host.querySelector('[data-bclear]');
  if (clr) clr.onclick = () => batchApply('clear');
  const rt = host.querySelector('[data-bretrans]');
  if (rt) rt.onclick = batchRetranslate;
  const none = host.querySelector('[data-bnone]');
  if (none) {
    none.onclick = () => {
      state.batch.clear();
      render();
    };
  }
}

function bindBatch(root) {
  if (!root) return;
  root.querySelectorAll('[data-pick]').forEach((box) => {
    // 勾选不能顺带把详情卡打开：这一格在可点击的行里
    box.onclick = (ev) => ev.stopPropagation();
    box.onchange = () => {
      const id = Number(box.dataset.pick);
      if (box.checked) state.batch.add(id);
      else state.batch.delete(id);
      paintBatchBar();
    };
  });
  const all = root.querySelector('#stPickAll') || root.querySelector('#fpPickAll');
  if (all) {
    all.onclick = (ev) => ev.stopPropagation();
    all.onchange = () => {
      // 本地过滤隐藏的行不参与全选：否则"过滤后点全选"会批量改到看不见的条目
      const boxes = [...root.querySelectorAll('[data-pick]')]
        .filter((box) => box.offsetParent !== null);
      boxes.forEach((box) => {
        const id = Number(box.dataset.pick);
        box.checked = all.checked;
        if (all.checked) state.batch.add(id);
        else state.batch.delete(id);
      });
      paintBatchBar();
    };
  }
  bindBatchActions();
}

async function batchApply(action, stage) {
  const ids = [...state.batch];
  if (!ids.length) return;
  const what = action === 'clear'
    ? '清空译文并退回「未翻译」'
    : `标为「${STR_STAGE_NAME[stage] || stage}」`;
  if (!(await askConfirm(`要对选中的 ${ids.length} 条${what}？这会修改平台上的数据。`))) return;
  try {
    const res = await api('POST', '/api/strings/batch', { ids, action, stage });
    if (res.dry_run) {
      toast(`演练模式：未写平台（待提交 ${res.count} 条）`);
      return;
    }
    toast(`已提交 ${res.count} 条，平台确认 ${res.confirmed} 条`);
    state.batch.clear();
    if (state.view === 'files') await loadFileStrings();
    else await loadStrings();
  } catch (e) {
    toast(e.message, true);
  }
}

async function batchRetranslate() {
  const ids = [...state.batch];
  if (!ids.length) return;
  const ok = await askConfirm(
    `把选中的 ${ids.length} 条交给模型重翻？`
    + '这些条目**已有的译文会被覆盖**（点名重翻不走"保护人工译文"）。'
  );
  if (!ok) return;
  try {
    const { job } = await api('POST', '/api/jobs',
      { tool: 'translate', args: { ids, limit: ids.length } });
    toast(`已提交重翻任务 ${job.id}（结果会实时刷到列表里）`);
    state.batch.clear();
    render();
  } catch (e) {
    toast(e.message, true);
  }
}

/* 行点击 / 实时面板行点击统一走这里：data-sid → 打开详情 */
function bindLiveAndRows(root) {
  if (!root) return;
  root.querySelectorAll('[data-sid]').forEach((el) => {
    el.onclick = () => openString(Number(el.dataset.sid));
  });
}

function strDetailHtml(d) {
  const it = d.item || {};
  // history 缺失时不能直接 .length：函数抛错会让整页停在上一帧
  const hist = Array.isArray(d.history) ? d.history : [];
  const local = Array.isArray(d.local_history) ? d.local_history : [];
  const stageBtns = [['标记已翻译', 1], ['标记有疑问', 2], ['标记已检查', 3], ['标记已审核', 5]]
    .map(([label, v]) => `<button class="btn sm" data-stage="${v}">${label}</button>`).join('');
  return `<div class="card" id="stDetail">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <b class="mono">#${esc(it.id)}</b>${stageTag(it.stage)}
      <span class="mono dim" style="font-size:11.5px" data-no-i18n>${esc(it.file_name)}</span>
      <div class="spacer"></div>
      <button class="btn sm" id="stClose">关闭</button>
    </div>
    <div class="muted" style="margin-top:10px;font-size:11.5px">原文</div>
    <div style="white-space:pre-wrap;font-size:13px;line-height:1.55"
      ><span data-no-i18n>${esc(it.original)}</span></div>
    ${it.context ? `<div class="muted" style="margin-top:10px;font-size:11.5px">上下文</div>
      <pre class="result" style="max-height:120px;overflow:auto" data-no-i18n>${esc(it.context)}</pre>` : ''}
    <div class="muted" style="margin-top:10px;font-size:11.5px">译文（改完点保存，直接写回平台）</div>
    <textarea class="field" id="stTr" data-no-i18n
      style="min-height:88px;margin-top:4px;font-size:13px">${esc(it.translation)}</textarea>
    <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
      <button class="btn primary" id="stSave">保存译文</button>
      ${stageBtns}
      <div class="spacer"></div>
      <span class="dim" style="font-size:11px">演练模式开启时只回显、不落平台</span>
    </div>
    <div class="muted" style="margin-top:12px;font-size:11.5px">
      本机处理记录（${local.length} 条）—— 我们的流水线对这条做过什么</div>
    ${local.length ? `<table>
      <thead><tr><th style="width:130px">时间</th><th style="width:90px">环节</th>
        <th style="width:90px">动作</th><th>备注</th></tr></thead>
      <tbody>${local.map((h) => `<tr>
        <td class="dim" style="font-size:11px">${fmtTime(h.ts)}</td>
        <td style="font-size:12px">${esc(h.pipeline || '—')}</td>
        <td style="font-size:12px">${esc(h.action || '—')}</td>
        <td class="dim" style="font-size:11.5px" data-no-i18n>${esc(h.detail || '')}</td>
      </tr>`).join('')}</tbody>
    </table>` : '<div class="empty">本机还没有处理过这条</div>'}
    <div class="muted" style="margin-top:12px;font-size:11.5px">
      平台人工改动记录（${hist.length} 条）</div>
    <div class="dim" style="font-size:11px;margin-top:2px">
      平台只记录网页端的人工操作；通过 API 批量写回（含本软件）不会出现在这个列表里。
    </div>
    ${hist.length ? `<table>
      <thead><tr><th style="width:120px">时间</th><th style="width:110px">操作人</th>
        <th>从</th><th>到</th></tr></thead>
      <tbody>${hist.map((h) => `<tr>
        <td class="dim" style="font-size:11px">${esc(String(h.at || '').replace('T', ' ').slice(0, 19))}</td>
        <td style="font-size:12px" data-no-i18n>${esc(h.user || '—')}</td>
        <td style="font-size:12px" data-no-i18n>${esc(clip(h.before, 80))}</td>
        <td style="font-size:12px" data-no-i18n>${esc(clip(h.after, 80))}</td>
      </tr>`).join('')}</tbody>
    </table>` : '<div class="empty">这条还没有改动记录</div>'}
  </div>`;
}

function bindDetail(main) {
  const close = $('#stClose');
  if (close) {
    close.onclick = closeDetail;
  }
  const save = $('#stSave');
  if (save) save.onclick = () => saveTranslation(main);
  main.querySelectorAll('[data-stage]').forEach((b) => {
    b.onclick = () => saveTranslation(main, Number(b.dataset.stage));
  });
}

/* 当前查询条件的指纹：用于判断"手上这批数据是不是这个条件下的" */
function strQueryKey() {
  const s = state.str;
  return `${s.page}|${s.size}|${s.stage}|${s.file}`;
}

async function loadStrings() {
  const s = state.str;
  const key = strQueryKey();
  s.loading = true;
  try {
    const params = new URLSearchParams({ page: String(s.page), page_size: String(s.size) });
    if (s.stage !== '') params.set('stage', s.stage);
    if (s.file) params.set('file', String(s.file));
    const d = await api('GET', '/api/strings?' + params.toString());
    s.items = d.items || [];
    s.total = d.total || 0;
    s.pages = d.pages || 0;
    s.page = d.page || s.page;
  } catch (e) {
    // 失败也要记 loadedKey：否则 renderStrings 会认为"没加载过"而立刻再拉一次，
    // 401/断网时就是无限请求 + 无限弹窗
    s.items = [];
    s.total = 0;
    s.pages = 0;
    s.err = e.message;
    toast(e.message, true);
  } finally {
    s.loading = false;
    s.loadedKey = key;
  }
  if (state.view === 'strings') render();
}

/* 详情请求序号：连点两行时，慢响应后到不能把详情卡改成上一条 */
let detailSeq = 0;

function closeDetail() {
  state.detail = null;
  state.detailId = null;
}

/* 详情接口失败时降级用：从已加载的列表里找那条 */
function findLoaded(id) {
  return (
    [...(state.str.items || []), ...(state.fileView.items || [])].find((r) => r.id === id) || null
  );
}

/* 从任务下钻：把这个任务写入/判定过的词条装进「词条」视图。
 *
 * 数据来源是本机账本的 job 列（持久、可按任务反查）；老任务没有这一列时
 * 后端会用任务起止时间窗兜底。这样"报表说写了 192 条"就能变成"这 192 条在哪"。
 */
async function openJobStrings(jobId) {
  try {
    toast('正在读取这批词条…（每条约 0.15 秒，受平台速率限制）');
    const d = await api('GET', '/api/jobs/' + encodeURIComponent(jobId) + '/strings');
    const s = state.str;
    Object.assign(s, {
      jobId, items: d.items || [], total: d.count || 0, pages: 1, page: 1,
      loadedKey: 'job:' + jobId, q: '', stage: '', detail: null, loading: false,
    });
    closeDetail();
    state.batch.clear();
    switchView('strings');
    const src = d.source === 'ledger' ? '本机账本'
      : (d.source === 'time-window' ? '任务时间窗' : '任务内存');
    toast(`本批 ${d.count} 条（来源：${src}）${d.missing ? `，${d.missing} 条已不在平台上` : ''}`);
  } catch (e) {
    toast(e.message, true);
  }
}

/* 按时间窗下钻：老任务没有 job 列、进程重启后任务也没了，只能从账本按时间反查。 */
async function openLedgerStrings(hours = 24) {
  try {
    toast('正在读取这批词条…（每条约 0.15 秒，受平台速率限制）');
    const d = await api('GET', `/api/ledger/strings?hours=${hours}&pipeline=translate`);
    if (!d.count) {
      toast(`最近 ${hours} 小时没有本机写入记录`, true);
      return;
    }
    Object.assign(state.str, {
      jobId: 'ledger:' + hours + 'h', items: d.items || [], total: d.count,
      pages: 1, page: 1, loadedKey: 'ledger:' + hours, q: '', stage: '',
      detail: null, loading: false,
    });
    closeDetail();
    state.batch.clear();
    switchView('strings');
    toast(`最近 ${hours} 小时写入 ${d.count} 条${d.missing ? `，${d.missing} 条已不在平台上` : ''}`);
  } catch (e) {
    toast(e.message, true);
  }
}

async function openString(id) {
  const my = ++detailSeq;
  state.detailId = id;
  let d;
  try {
    d = await api('GET', '/api/strings/' + id);
  } catch (e) {
    // 详情拿不到不影响列表：把已加载的那条降级显示，同时明确提示
    d = { item: findLoaded(id) || { id }, history: [], local_history: [] };
    toast(e.message, true);
  }
  if (my !== detailSeq) return;     // 期间用户又点了别的行
  state.detail = d;
  render();
  const box = $('#stDetail');
  if (box) box.scrollIntoView({ block: 'nearest' });
}

/* 保存译文 / 改阶段：同一个 PUT 入口。成功后就地刷新该行（用平台回读的值） */
async function saveTranslation(main, stage) {
  const d = state.detail;
  if (!d || !d.item) return;
  const body = {};
  if (stage == null) {
    const el = $('#stTr');
    // 取不到输入框时**绝不能**提交空串：那等于把平台上的译文清空。
    if (!el) {
      toast('译文输入框已不在页面上，请重新点开这条再改', true);
      return;
    }
    body.translation = el.value;
  } else {
    body.stage = stage;
  }
  try {
    const res = await api('PUT', '/api/strings/' + d.item.id, body);
    if (res.dry_run) {
      toast('演练模式：没有写平台。将提交 ' + JSON.stringify(res.would_write));
      return;
    }
    // 用平台回读的结果覆盖本地，绝不把"我们提交的"当"平台已存的"
    const item = res.item || d.item;
    patchLocalItem(item.id, item);
    applyLiveItems([{ id: item.id, translation: item.translation, stage: item.stage,
                      status: 'written' }]);
    // "还在不在当前页"要按**当前视图**判断：词条页看 state.str，文件页看 state.fileView
    const list = state.view === 'files' ? state.fileView.items : state.str.items;
    if (!(list || []).some((r) => r.id === item.id)) {
      // 保存期间用户翻了页/改了筛选：别把一条不属于当前页的词条重新做成详情卡
      closeDetail();
      render();
      toast('已写入平台（该条已不在当前筛选结果里）');
      return;
    }
    toast('已写入平台');
    // 重新拉一次详情：平台改动记录里必须能看到这一次修改（否则"记录"是假的）
    await openString(item.id);
  } catch (e) {
    toast(e.message, true);
  }
}

/* ------------------------------------------------------------------ 工具 */
function renderTools(main) {
  const tools = state.tools || [];
  main.innerHTML = `
    ${pageHead('工具', '每个工具就是一条 pipeline。带「写」标记的会改平台数据——演练模式开启时只预览。')}
    <div class="toolgrid">
      ${tools.map((t) => `
        <div class="tool">
          <div class="t">${esc(L(t, 'label'))}
            ${t.writes ? '<span class="tag w">写</span>' : '<span class="tag">读</span>'}</div>
          <div class="d">${esc(L(t, 'description'))}</div>
          <div class="e">${esc(L(t, 'endpoint'))}</div>
          <div class="a"><button class="btn" data-run="${esc(t.name)}">运行</button></div>
        </div>`).join('')}
    </div>`;
  main.querySelectorAll('[data-run]').forEach((b) => {
    b.onclick = async () => {
      const name = b.dataset.run;
      b.disabled = true;
      try {
        const { job } = await api('POST', '/api/jobs', { tool: name, args: {} });
        toast(`已提交 ${name}`);
        await refreshJobs();
        state.openJobs.add(job.id);
        switchView('jobs');
      } catch (e) {
        toast(e.message, true);
      } finally {
        b.disabled = false;
      }
    };
  });
}

/* ------------------------------------------------------------------ 设置 */
/* ------------------------------------------------------------------ 设置 */
function segBtn(cur, val, label, kind) {
  const attr = kind === 'theme' ? 'data-theme-opt' : 'data-lang-opt';
  return `<button type="button" class="seg${cur === val ? ' on' : ''}" ${attr}="${val}">${esc(label)}</button>`;
}

function aboutRow(label, desc, val, href, linkText) {
  const right = href
    ? `<a class="btn sm" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(linkText || label)}</a>`
    : `<span class="mono dim">${esc(val || '')}</span>`;
  return `<div class="aboutrow">
    <div class="abouttext">
      <b>${esc(label)}</b>
      ${desc ? `<div class="muted" style="font-size:11.5px;margin-top:2px">${esc(desc)}</div>` : ''}
    </div>
    ${right}
  </div>`;
}

function renderPrefs(main) {
  const h = state.health || {};
  const ver = h.version || '—';
  const tp = themePref();
  const lang = typeof currentLang === 'function' ? currentLang() : 'zh';
  const repo = 'https://github.com/RandyLuaW/Paratranz-Agent';

  main.innerHTML = `
    ${pageHead('设置', '外观、语言与项目信息')}
    <div class="grid c2">
      <div class="card">
        <h3 style="margin-top:0">外观</h3>
        <div class="formrow">
          <label class="lbl">主题</label>
          <div class="segbar" id="prefsTheme">
            ${segBtn(tp, 'system', '跟随系统', 'theme')}
            ${segBtn(tp, 'dark', '深色', 'theme')}
            ${segBtn(tp, 'light', '浅色', 'theme')}
          </div>
          <div class="muted" style="font-size:11.5px;margin-top:6px">
            选择「跟随系统」时，切换操作系统的深色 / 浅色会立即生效。
          </div>
        </div>
      </div>

      <div class="card">
        <h3 style="margin-top:0">语言</h3>
        <div class="formrow">
          <label class="lbl">界面语言</label>
          <div class="segbar" id="prefsLang">
            ${segBtn(lang, 'zh', '简体中文', 'lang')}
            ${segBtn(lang, 'en', 'English', 'lang')}
          </div>
          <div class="muted" style="font-size:11.5px;margin-top:6px">
            切换后页面会重载。项目内容、日志正文与模型返回保持原文，不做翻译。
          </div>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:12px">
      <h3 style="margin-top:0">关于</h3>
      <div class="aboutlist">
        ${aboutRow('版本', '当前运行的构建版本', 'v' + ver)}
        ${aboutRow('作者', '问题反馈与功能建议请走 issue', '', 'https://github.com/RandyLuaW', 'RandyLuaW')}
        ${aboutRow('更新日志', '各版本的改动说明', '', repo + '/releases', 'Releases')}
        ${aboutRow('源码仓库', '源码公开，可自由修改与免费分发，欢迎提交 issue 与 PR', '', repo, 'GitHub')}
        ${aboutRow('免费声明', '本软件完全免费；若通过付费渠道取得请立即退款并到官方仓库获取',
                   '', repo, '官方仓库')}
      </div>
    </div>`;

  const themeBox = $('#prefsTheme');
  if (themeBox) {
    themeBox.onclick = (e) => {
      const b = e.target.closest('[data-theme-opt]');
      if (!b) return;
      applyTheme(b.dataset.themeOpt, true);
      render();               // 重画选中态
    };
  }
  const langBox = $('#prefsLang');
  if (langBox) {
    langBox.onclick = (e) => {
      const b = e.target.closest('[data-lang-opt]');
      if (!b) return;
      setLang(b.dataset.langOpt);
    };
  }
}

function renderSettings(main) {
  const s = state.settings || {};
  main.innerHTML = `
    ${pageHead('项目', 'ParaTranz 连接与翻译/校对策略。模型配置见「模型」页，凭据只保存在本机。')}
    <div class="grid c2">
      <div class="card">
        <h3 style="margin-top:0">ParaTranz</h3>
        <div class="formrow"><label class="lbl">项目编号</label>
          <input class="field" id="s_project" value="${esc(s.project_id || '')}" placeholder="例如 6896"></div>
        <div class="formrow"><label class="lbl">API Token（只写不回显）</label>
          <input class="field" id="s_token" type="password" value="" placeholder="${esc(s.token || '未配置')}"
                 autocomplete="new-password">
          <div class="muted" style="font-size:11.5px;margin-top:4px">
            🔒 仅保存在本机 <code>.ptz-state/settings.json</code>，日志中会脱敏，界面只显示尾号。
          </div></div>
        <div class="formrow"><label class="lbl">API 地址</label>
          <input class="field" id="s_base" value="${esc(s.base_url || '')}"></div>
        <div class="formrow"><label class="lbl">限流（次/分钟，平台硬限 480）</label>
          <input class="field" id="s_rate" type="number" value="${esc(s.rate_limit_per_min || 420)}"></div>
      </div>

      <div class="card">
        <h3 style="margin-top:0">翻译</h3>
        <div class="formrow"><label class="lbl">目标风格（注入 prompt）</label>
          <textarea class="field" id="s_style" rows="3">${esc(s.style || '')}</textarea></div>
        <div class="formrow"><label class="lbl">单批词条数</label>
          <input class="field" id="s_batch" type="number" value="${esc(s.batch_size || 40)}"></div>
        <div class="formrow"><label class="lbl">单次运行条数上限</label>
          <input class="field" id="s_limit" type="number" value="${esc(s.per_run_limit || 200)}"></div>
      </div>

      <div class="card">
        <h3 style="margin-top:0">校对与任务</h3>
        <div class="formrow"><label class="lbl">通过后写入 stage</label>
          <select class="field" id="s_pass">
            <option value="3" ${s.review_pass_stage == 3 ? 'selected' : ''}>3 已检查</option>
            <option value="5" ${s.review_pass_stage == 5 ? 'selected' : ''}>5 已审核</option>
          </select></div>
        <div class="formrow"><label class="lbl">打回阈值</label>
          <select class="field" id="s_block">
            <option value="error" ${s.review_block_on === 'error' ? 'selected' : ''}>error（推荐）</option>
            <option value="warn" ${s.review_block_on === 'warn' ? 'selected' : ''}>warn（更严）</option>
            <option value="info" ${s.review_block_on === 'info' ? 'selected' : ''}>info（最严）</option>
          </select></div>
        <div class="formrow"><label class="lbl">单任务词条上限</label>
          <input class="field" id="s_maxstr" type="number" value="${esc(s.max_strings_per_task || 800)}"></div>
      </div>

      <div class="card">
        <h3 style="margin-top:0">回写</h3>
        <div class="formrow"><label class="lbl">汉化版输出目录</label>
          <input class="field" id="s_wbout" value="${esc(s.writeback_out_dir || '')}"
                 placeholder="留空 = 游戏目录旁的 &lt;游戏目录&gt;_cn">
          <div class="muted" style="font-size:11.5px;margin-top:4px">
            这里只是默认值，单次回写可以在弹窗里改，不影响此项。
          </div></div>
        <div class="formrow"><label class="lbl">回写形式</label>
          <select class="field" id="s_wbmode">${wbModeOptions(s.writeback_mode)}</select>
          <div class="muted" style="font-size:11.5px;margin-top:4px">
            整包镜像 = 未改动的文件原样复制，目标目录可直接替换游戏目录；仅改动文件 = 只放被改的文件。
          </div></div>
        <div class="formrow"><label class="lbl">译文门槛</label>
          <select class="field" id="s_wbstage">${wbStageOptions(s.writeback_min_stage)}</select></div>
      </div>
    </div>

    <div class="row" style="margin-top:16px">
      <button class="btn primary" id="saveSettings">保存</button>
      <button class="btn" id="testSettings">测试连接</button>
      <span class="muted" id="testResult" style="font-size:12.5px"></span>
    </div>`;

  $('#saveSettings').onclick = saveSettings;
  $('#testSettings').onclick = testSettings;
}

async function saveSettings() {
  const body = {
    project_id: Number($('#s_project').value) || 0,
    base_url: $('#s_base').value.trim(),
    rate_limit_per_min: Number($('#s_rate').value) || 420,
    style: $('#s_style').value,
    batch_size: Number($('#s_batch').value) || 40,
    per_run_limit: Number($('#s_limit').value) || 200,
    review_pass_stage: Number($('#s_pass').value),
    review_block_on: $('#s_block').value,
    max_strings_per_task: Number($('#s_maxstr').value) || 800,
    // 回写默认值：弹窗会拿这三项预填，单次回写仍可覆盖
    writeback_out_dir: $('#s_wbout').value.trim(),
    writeback_mode: $('#s_wbmode').value,
    writeback_min_stage: Number($('#s_wbstage').value) || 3,
  };
  const tok = $('#s_token').value.trim();
  if (tok) body.token = tok;

  try {
    const { settings } = await api('PUT', '/api/settings', body);
    state.settings = settings;
    toast('已保存');
    $('#s_token').value = '';
    const h = await api('GET', '/api/health');
    state.health = h;
    state.llmReady = !!h.llm;
    // 换了项目/Token 之后，词条页手上那批数据属于上一个项目 ——
    // 不清掉的话"词条"页会继续显示旧项目的条目（还点得开、还能改）
    resetStringsState();
    await refreshAll();
  } catch (e) {
    toast(e.message, true);
  }
}

/* 词条页的筛选/分页/详情全部回到初始状态（切项目、切语言等场景用） */
function resetStringsState() {
  Object.assign(state.str, {
    page: 1, items: [], total: 0, pages: 0, sel: null, detail: null,
    loading: false, loadedKey: '', q: '',
  });
  Object.assign(state.fileView, {
    page: 1, items: [], total: 0, pages: 0, loadedKey: '', q: '', loading: false,
  });
  closeDetail();
}

async function testSettings() {
  const out = $('#testResult');
  const btn = $('#testSettings');
  out.textContent = '测试中…';
  btn.disabled = true;
  // 测**界面上正在填的值**：以前这个请求不带参数，后端读的是已保存的配置，
  // 于是「改了编号没保存就点测试」测的是上一次的编号 —— 报错与眼前的输入对不上账。
  const body = {
    project_id: Number($('#s_project').value) || 0,
    base_url: $('#s_base').value.trim(),
    token: $('#s_token').value.trim(),
  };
  try {
    const r = await api('POST', '/api/settings/test', body);
    if (!r.ok) {
      const hint = (r.hints || []).map((h) => '· ' + esc(h)).join('<br>');
      out.innerHTML = `<span style="color:var(--err)">✗ ${esc(r.error || '连接失败')}</span>` +
        (r.url ? `<div class="muted" style="font-size:11.5px;margin-top:2px">请求：${esc(r.url)}</div>` : '') +
        (hint ? `<div style="color:var(--warn);margin-top:4px;line-height:1.5">${hint}</div>` : '');
      setConn(false, '连接失败');
      return;
    }
    const p = r.project;
    out.innerHTML = `<span style="color:var(--ok)">✓ ${esc(p.name)}</span>
      · ${esc(p.source)}→${esc(p.dest)} · ${p.members} 名成员`;
    if (r.warnings && r.warnings.length) {
      out.innerHTML += `<div style="color:var(--warn);margin-top:4px">${r.warnings.map(esc).join('<br>')}</div>`;
    }
    setConn(true, '已连接');
    // 只更新数据、不触发 render：重绘会把 #testResult 和用户未保存的表单一起清掉
    state.project = await api('GET', '/api/project');
  } catch (e) {
    out.innerHTML = `<span style="color:var(--err)">✗ ${esc(e.message)}</span>`;
    setConn(false, '连接失败');
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ 数据加载 */
/* 自动刷新前先看用户是不是正在某个表单里打字：整页重绘会把输入框重建，
   正在输入的 Agent 指令会凭空消失（宁可这次不刷，也不能吃用户的东西）。 */
function canAutoRefresh() {
  const el = document.activeElement;
  if (!el || !el.closest || !el.closest('#main')) return true;
  return !/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName);
}

let _projTimers = [];
/* 任务结束后分两次拉项目统计：平台 stats 有 6~8 秒延迟，
   立刻拉会拿到旧数字，用户就会觉得"写完了但待翻译没变"。 */
function scheduleProjectRefresh() {
  _projTimers.forEach(clearTimeout);
  _projTimers = [4000, 13000].map((ms) => setTimeout(() => {
    if (canAutoRefresh()) loadProject(state.view === 'overview');
  }, ms));
}

async function loadProject(rerender = false) {
  try {
    state.project = await api('GET', '/api/project');
    state.projectAt = Date.now();
    setConn(true, '已连接');
    // 项目名由 pageHead 渲染，顶栏只保留面包屑
    if (rerender || state.view === 'overview') render();
  } catch (e) {
    state.project = null;
    setConn(false, e.message.slice(0, 40));
    if (rerender || state.view === 'overview') render();
  }
}

async function refreshAll() {
  await Promise.allSettled([loadProject(false), loadFiles(false), refreshJobs(false)]);
  render();
}

/* ================================================================== 模型 */
const TASK_LABEL = {
  agent: 'Agent 意图解析', translate: '翻译', review: '校对',
  glossary: '术语抽取', styleguide: '规范生成',
};
const CAP_LABEL = { text: '文本', image: '图像', audio: '音频', tool: '工具' };
const modelTestCache = {};   // "pid::mid" → {ok, latency_ms, error}

async function renderModels(main) {
  const ep = renderEpoch;
  let d;
  try {
    d = await api('GET', '/api/providers');
  } catch (e) {
    if (ep !== renderEpoch) return;
    main.innerHTML = pageHead('模型', '读取失败') +
      `<div class="card"><span style="color:var(--err)">${esc(e.message)}</span></div>`;
    return;
  }
  const tab = state.modelTab || 'providers';
  if (!d.providers.some((p) => p.id === state.selProvider)) {
    state.selProvider = d.providers.length ? d.providers[0].id : '';
  }
  const sel = d.providers.find((p) => p.id === state.selProvider) || null;
  const totalModels = d.providers.reduce((n, p) => n + p.models.length, 0);
  const ready = d.providers.filter((p) => p.has_key).length;
  if (ep !== renderEpoch) return;

  main.innerHTML = `
    ${pageHead(
      '模型',
      `${d.providers.length} 个源 · ${totalModels} 个模型（${ready} 个源已配 Key）
       · 按任务分配以平衡质量与成本`,
      '<button class="btn primary" id="addProv">+ 新增源</button>'
    )}
    <div class="subtabs">
      <button data-mtab="providers" class="${tab === 'providers' ? 'on' : ''}">提供商源</button>
      <button data-mtab="routing" class="${tab === 'routing' ? 'on' : ''}">任务路由</button>
    </div>
    ${tab === 'providers' ? provView(d, sel) : routingView(d)}
  `;

  main.querySelectorAll('[data-mtab]').forEach((b) => {
    b.onclick = () => { state.modelTab = b.dataset.mtab; renderModels(main); };
  });
  $('#addProv').onclick = async () => {
    let n = d.providers.length + 1;
    while (d.providers.some((p) => p.id === `p${n}`)) n++;
    try {
      await api('POST', '/api/providers', {
        id: `p${n}`, name: `源 ${n}`, base_url: 'https://api.openai.com/v1',
      });
      state.selProvider = `p${n}`;
      toast(`已新增源 ${n}，填 Key 后可点「获取模型列表」`);
      renderModels(main);
    } catch (e) { toast(e.message, true); }
  };

  if (tab === 'providers' && sel) bindProvForm(main, d, sel);
  if (tab === 'routing') bindRouting(main, d);
}

function provView(d, sel) {
  if (!d.providers.length) {
    return `<div class="card"><div class="empty">还没有提供商源，点右上角「新增源」</div></div>`;
  }
  const items = d.providers.map((p) => {
    const dot = !p.enabled ? '' : (p.has_key ? 'ok' : 'warn');
    const used = Object.entries(d.routing.by_tool || {})
      .filter(([, v]) => String(v).startsWith(p.id + '::')).length;
    return `<button class="mitem ${sel && p.id === sel.id ? 'on' : ''}" data-pid="${esc(p.id)}">
      <span class="mdot ${dot}"></span>
      <span class="mi-main">
        <b>${esc(p.name || p.id)}</b>
        <span class="mono dim">${esc(p.base_url.replace(/^https?:\/\//, ''))}</span>
      </span>
      <span class="tag">${p.models.length}</span>
      ${used ? `<span class="tag ok">${used}</span>` : ''}
    </button>`;
  }).join('');
  return `<div class="msplit">
    <div class="mlist">${items}</div>
    <div class="mdetail">${sel ? provForm(d, sel) : ''}</div>
  </div>`;
}

function provForm(d, p) {
  const isDefault = String(d.routing.default || '').startsWith(p.id + '::');
  return `
  <div class="card">
    <div class="row" style="justify-content:space-between;margin-bottom:10px">
      <div class="row" style="gap:6px">
        <b style="font-size:14px">${esc(p.name || p.id)}</b>
        <span class="mono dim">${esc(p.id)}</span>
        ${isDefault ? '<span class="tag ok">默认源</span>' : ''}
        ${p.enabled ? '' : '<span class="tag w">已停用</span>'}
      </div>
      <div class="row">
        <button class="btn" id="saveProv">保存配置</button>
        <button class="btn danger" id="delProv">删除</button>
      </div>
    </div>

    <div class="grid c2">
      <div>
        <div class="formrow"><label class="lbl">显示名</label>
          <input class="field" id="p_name" value="${esc(p.name || '')}" placeholder="${esc(p.id)}"></div>
        <div class="formrow"><label class="lbl">API Base URL</label>
          <input class="field" id="p_base" value="${esc(p.base_url)}"></div>
        <div class="formrow"><label class="lbl">API Key（只写不回显）</label>
          <input class="field" id="p_key" type="password" autocomplete="new-password"
                 placeholder="${p.has_key ? esc(p.masked_key) : '未配置'}">
          <div class="dim" style="font-size:11px;margin-top:3px">
            🔒 仅存本机 settings.json，日志脱敏
          </div></div>
        <div class="formrow"><label class="lbl">超时（秒）</label>
          <input class="field" id="p_timeout" type="number" value="${p.timeout}"></div>
      </div>
      <div>
        <div class="formrow"><label class="lbl">并发批次数</label>
          <input class="field" id="p_conc" type="number" min="1" max="32" value="${p.concurrency}"></div>
        <div class="formrow"><label class="lbl">最大重试</label>
          <input class="field" id="p_retries" type="number" min="0" max="10" value="${p.max_retries}"></div>
        <div class="formrow"><label class="lbl">代理地址（可选）</label>
          <input class="field" id="p_proxy" value="${esc(p.proxy || '')}"
                 placeholder="http://127.0.0.1:7890"></div>
        <div class="formrow"><label class="lbl">用途备注</label>
          <input class="field" id="p_note" value="${esc(p.note || '')}"
                 placeholder="例如：中转站，聚合多个模型"></div>
      </div>
    </div>

    <div class="row">
      <label class="switch"><input type="checkbox" id="p_enabled" ${p.enabled ? 'checked' : ''}><span>启用</span></label>
      <span class="dim" id="provMsg" style="font-size:11.5px"></span>
    </div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="row" style="justify-content:space-between;margin-bottom:8px">
      <h3 style="margin:0">模型 <span class="cnt">${p.models.length} 个已配置</span></h3>
      <div class="row">
        <input class="field" id="mSearch" placeholder="搜索模型" style="width:150px">
        <button class="btn" id="discoverBtn">获取模型列表</button>
        <button class="btn" id="customModelBtn">+ 自定义模型</button>
        <button class="btn" id="testAllBtn">检查全部</button>
      </div>
    </div>
    <div id="modelRows">${modelRows(p)}</div>
    <div id="mSearchHint" class="empty" style="display:none"></div>
    <div id="discoverBox"></div>
  </div>`;
}

function modelRows(p) {
  if (!p.models.length) {
    return `<div class="empty">还没有模型。点「获取模型列表」自动探测，或「+ 自定义模型」手动加</div>`;
  }
  return `<table>
    <thead><tr>
      <th>模型</th><th style="width:180px">能力</th>
      <th style="width:110px">连接</th><th style="width:150px"></th>
    </tr></thead>
    <tbody>
      ${p.models.map((m) => {
        const ref = `${p.id}::${m.id}`;
        const t = modelTestCache[ref];
        const lat = t
          ? (t.ok
              ? `<span class="lat ok">${t.latency_ms}ms</span>`
              : `<span class="lat err" title="${esc(t.error || '')}">不可用</span>`)
          : '<span class="dim">—</span>';
        const caps = (m.capabilities || []).map(
          (c) => `<span class="tag">${esc(CAP_LABEL[c] || c)}</span>`).join(' ');
        return `<tr data-mid="${esc(m.id)}">
          <td>
            <div><b>${esc(m.name || m.id)}</b>${m.enabled ? '' : ' <span class="tag w">停用</span>'}</div>
            <div class="mono dim" style="font-size:10.5px">${esc(m.id)}</div>
            ${m.context_window ? `<div class="dim" style="font-size:10.5px">上下文 ${fmtNum(m.context_window)}</div>` : ''}
          </td>
          <td>${caps || '<span class="dim">—</span>'}</td>
          <td>${lat}</td>
          <td style="text-align:right;white-space:nowrap">
            <label class="switch" style="display:inline-flex;margin-right:6px">
              <input type="checkbox" class="m_en" ${m.enabled ? 'checked' : ''} title="启用">
            </label>
            <button class="btn sm m_test">测试</button>
            <button class="btn sm m_cfg">设置</button>
            <button class="btn sm danger m_del">删</button>
          </td>
        </tr>`;
      }).join('')}
    </tbody>
  </table>`;
}

function bindProvForm(main, d, p) {
  main.querySelectorAll('.mitem').forEach((b) => {
    b.onclick = () => { state.selProvider = b.dataset.pid; renderModels(main); };
  });

  const collect = () => {
    const body = {
      name: $('#p_name').value.trim(),
      base_url: $('#p_base').value.trim(),
      timeout: Number($('#p_timeout').value) || 120,
      concurrency: Number($('#p_conc').value) || 4,
      max_retries: Number($('#p_retries').value) || 0,
      proxy: $('#p_proxy').value.trim(),
      enabled: $('#p_enabled').checked,
      note: $('#p_note').value.trim(),
    };
    const k = $('#p_key').value.trim();
    if (k) body.api_key = k;
    return body;
  };

  $('#saveProv').onclick = async () => {
    try {
      await api('PUT', `/api/providers/${encodeURIComponent(p.id)}`, collect());
      toast('已保存');
      const h = await api('GET', '/api/health');
      state.health = h; state.llmReady = !!h.llm;
      renderModels(main);
    } catch (e) {
      $('#provMsg').innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`;
    }
  };

  $('#delProv').onclick = async () => {
    const delMsg = isEn()
      ? `Delete provider "${p.name || p.id}" and its ${p.models.length} model(s)?`
      : `删除源「${p.name || p.id}」及其下 ${p.models.length} 个模型？`;
    if (!(await askConfirm(delMsg))) return;
    try {
      await api('DELETE', `/api/providers/${encodeURIComponent(p.id)}`);
      state.selProvider = '';
      // 删掉的可能是最后一个带 Key 的源，概览的「模型 API」状态要跟着变
      const h = await api('GET', '/api/health');
      state.health = h;
      state.llmReady = !!h.llm;
      toast('已删除');
      renderModels(main);
    } catch (e) { toast(e.message, true); }
  };

  // 获取模型列表
  $('#discoverBtn').onclick = async () => {
    const box = $('#discoverBox');
    box.innerHTML = '<div class="dim" style="margin-top:10px">正在向该源请求模型列表…</div>';
    try {
      const r = await api('POST', `/api/providers/${encodeURIComponent(p.id)}/discover`);
      if (!r.models.length) {
        box.innerHTML = '<div class="dim" style="margin-top:10px">该源没有返回任何模型</div>';
        return;
      }
      box.innerHTML = `
        <div class="discover">
          <div class="row" style="justify-content:space-between;margin-bottom:6px">
            <b>探测到 ${r.count} 个模型</b>
            <div class="row">
              <button class="btn sm" id="addAllNew">全部添加未配置的</button>
              <button class="btn sm" id="closeDiscover">关闭</button>
            </div>
          </div>
          <div class="dlist">
            ${r.models.map((m) => `<label class="ditem">
              <input type="checkbox" class="d_chk" value="${esc(m.id)}" ${m.configured ? 'disabled' : ''}>
              <span class="mono">${esc(m.id)}</span>
              ${m.configured ? '<span class="tag ok">已配置</span>' : ''}
            </label>`).join('')}
          </div>
          <div class="row" style="margin-top:8px">
            <button class="btn primary" id="addPicked">添加选中</button>
          </div>
        </div>`;
      const addModels = async (ids) => {
        let done = 0;
        try {
          for (const mid of ids) {
            await api('POST', `/api/providers/${encodeURIComponent(p.id)}/models`,
              { id: mid, enabled: true, capabilities: ['text'] });
            done += 1;
          }
          toast(`已添加 ${done} 个模型`);
        } catch (e) {
          toast(`添加中断：成功 ${done}/${ids.length} —— ${e.message}`, true);
        }
        renderModels(main);
      };
      $('#addPicked').onclick = () => {
        const ids = Array.from(box.querySelectorAll('.d_chk:checked')).map((x) => x.value);
        if (!ids.length) return toast('先勾选模型', true);
        addModels(ids);
      };
      $('#addAllNew').onclick = () => {
        const ids = Array.from(box.querySelectorAll('.d_chk:not(:disabled)')).map((x) => x.value);
        if (!ids.length) return toast('没有可添加的新模型', true);
        addModels(ids);
      };
      $('#closeDiscover').onclick = () => { box.innerHTML = ''; };
    } catch (e) {
      box.innerHTML = `<div style="color:var(--err);margin-top:10px;font-size:12px">${esc(e.message)}</div>`;
    }
  };

  // 自定义模型
  $('#customModelBtn').onclick = () => openModelModal(main, p, null);

  // 检查全部
  $('#testAllBtn').onclick = async () => {
    const btn = $('#testAllBtn');
    btn.disabled = true;
    btn.textContent = '检查中…';
    try {
      const r = await api('POST', `/api/providers/${encodeURIComponent(p.id)}/models/test-all`);
      r.results.forEach((x) => { modelTestCache[`${p.id}::${x.model}`] = x; });
      toast(`${r.ok_count}/${r.total} 个模型可用`);
      renderModels(main);
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
      btn.textContent = '检查全部';
    }
  };

  // 搜索框：按行文本过滤（模型 id / 显示名 / 能力都算命中）。
  // 刻意**不重绘表格**：行内的启用开关、测试、删除都是在上面绑好的，
  // 重绘会把这些处理器一起丢掉，点了没反应。
  const search = $('#mSearch');
  if (search) {
    search.oninput = () => {
      const q = search.value.trim().toLowerCase();
      let shown = 0;
      const rows = main.querySelectorAll('#modelRows tbody tr');
      rows.forEach((tr) => {
        const hit = !q || tr.textContent.toLowerCase().includes(q);
        tr.style.display = hit ? '' : 'none';
        if (hit) shown += 1;
      });
      const hint = $('#mSearchHint');
      if (hint) {
        hint.style.display = q && !shown ? '' : 'none';
        hint.textContent = q && !shown ? t('没有匹配的模型') : '';
      }
    };
  }

  // 行内操作
  main.querySelectorAll('#modelRows tbody tr').forEach((tr) => {
    const mid = tr.dataset.mid;
    const ref = `${p.id}::${mid}`;
    tr.querySelector('.m_en').onchange = async (ev) => {
      try {
        await api('PUT', `/api/providers/${encodeURIComponent(p.id)}/models/${encodeURIComponent(mid)}`,
          { enabled: ev.target.checked });
        toast(ev.target.checked ? '已启用' : '已停用');
      } catch (e) { toast(e.message, true); ev.target.checked = !ev.target.checked; }
    };
    tr.querySelector('.m_test').onclick = async (ev) => {
      const btn = ev.target;
      btn.disabled = true;
      btn.textContent = '…';
      try {
        const r = await api('POST',
          `/api/providers/${encodeURIComponent(p.id)}/models/${encodeURIComponent(mid)}/test`);
        modelTestCache[ref] = r;
        const cell = tr.children[2];
        cell.innerHTML = r.ok
          ? `<span class="lat ok">${r.latency_ms}ms</span>`
          : `<span class="lat err" title="${esc(r.error || '')}">不可用</span>`;
        if (r.ok) toast(`${mid} 可用 · ${r.latency_ms}ms`);
        else toast(`${mid} 不可用：${(r.error || '').slice(0, 60)}`, true);
      } catch (e) {
        modelTestCache[ref] = { ok: false, error: e.message };
        tr.children[2].innerHTML = `<span class="lat err">不可用</span>`;
        toast(e.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = '测试';
      }
    };
    tr.querySelector('.m_cfg').onclick = () => {
      const m = p.models.find((x) => x.id === mid);
      openModelModal(main, p, m);
    };
    tr.querySelector('.m_del').onclick = async () => {
      if (!(await askConfirm(isEn() ? `Delete model "${mid}"?` : `删除模型「${mid}」？`))) return;
      try {
        await api('DELETE',
          `/api/providers/${encodeURIComponent(p.id)}/models/${encodeURIComponent(mid)}`);
        delete modelTestCache[ref];
        toast('已删除');
        renderModels(main);
      } catch (e) { toast(e.message, true); }
    };
  });
}

/* ---- 模型编辑弹窗 ---- */
function openModelModal(main, p, m) {
  const isNew = !m;
  const model = m || { id: '', name: '', enabled: true, capabilities: ['text'],
                       context_window: 0, temperature: null, max_tokens: null,
                       extra_params: {}, note: '' };
  const caps = model.capabilities || [];
  const el = document.createElement('div');
  el.className = 'modal';
  el.innerHTML = `
    <div class="modal-box">
      <div class="modal-head">
        <b>${isNew ? '新增模型' : '编辑模型'}</b>
        <span class="mono dim">${esc(p.name || p.id)}</span>
      </div>
      <div class="modal-body">
        <div class="formrow"><label class="lbl">模型 ID（请求里用的 model 名）</label>
          <input class="field" id="mm_id" value="${esc(model.id)}" ${isNew ? '' : 'readonly'}
                 placeholder="如 deepseek-chat / gpt-4o-mini"></div>
        <div class="formrow"><label class="lbl">显示名（可选）</label>
          <input class="field" id="mm_name" value="${esc(model.name || '')}"></div>

        <div class="formrow"><label class="lbl">模型能力</label>
          <div class="row">
            ${Object.entries(CAP_LABEL).map(([k, v]) => `
              <label class="chk"><input type="checkbox" class="mm_cap" value="${k}"
                ${caps.includes(k) ? 'checked' : ''}><span>${v}</span></label>`).join('')}
          </div>
        </div>

        <div class="grid c2">
          <div class="formrow"><label class="lbl">上下文窗口（0 = 未知）</label>
            <input class="field" id="mm_ctx" type="number" value="${model.context_window || 0}"></div>
          <div class="formrow"><label class="lbl">temperature（留空继承源）</label>
            <input class="field" id="mm_temp" type="number" step="0.1" min="0" max="2"
                   value="${model.temperature === null ? '' : model.temperature}"></div>
          <div class="formrow"><label class="lbl">max_tokens（留空继承源）</label>
            <input class="field" id="mm_maxtok" type="number"
                   value="${model.max_tokens === null ? '' : model.max_tokens}"></div>
          <div class="formrow"><label class="lbl">备注</label>
            <input class="field" id="mm_note" value="${esc(model.note || '')}"></div>
        </div>

        <div class="formrow"><label class="lbl">自定义请求体参数（JSON，如 {"top_p":0.9}）</label>
          <textarea class="field" id="mm_extra" rows="2">${esc(
            Object.keys(model.extra_params || {}).length
              ? JSON.stringify(model.extra_params) : '')}</textarea></div>

        <label class="switch"><input type="checkbox" id="mm_enabled"
          ${model.enabled ? 'checked' : ''}><span>启用</span></label>
      </div>
      <div class="modal-foot">
        <span class="dim" id="mm_msg" style="font-size:11.5px"></span>
        <div class="spacer"></div>
        <button class="btn" id="mm_cancel">取消</button>
        <button class="btn primary" id="mm_save">保存</button>
      </div>
    </div>`;
  document.body.appendChild(el);

  const close = () => el.remove();
  el.onclick = (ev) => { if (ev.target === el) close(); };
  $('#mm_cancel', el).onclick = close;
  $('#mm_save', el).onclick = async () => {
    const mid = $('#mm_id', el).value.trim();
    if (!mid) return toast('模型 ID 必填', true);
    let extra = {};
    const raw = $('#mm_extra', el).value.trim();
    if (raw) {
      try { extra = JSON.parse(raw); }
      catch { $('#mm_msg', el).innerHTML = '<span style="color:var(--err)">自定义参数不是合法 JSON</span>'; return; }
    }
    const t = $('#mm_temp', el).value.trim();
    const mt = $('#mm_maxtok', el).value.trim();
    const body = {
      id: mid,
      name: $('#mm_name', el).value.trim(),
      enabled: $('#mm_enabled', el).checked,
      capabilities: Array.from(el.querySelectorAll('.mm_cap:checked')).map((x) => x.value),
      context_window: Number($('#mm_ctx', el).value) || 0,
      temperature: t === '' ? null : Number(t),
      max_tokens: mt === '' ? null : Number(mt),
      extra_params: extra,
      note: $('#mm_note', el).value.trim(),
    };
    try {
      if (isNew) {
        await api('POST', `/api/providers/${encodeURIComponent(p.id)}/models`, body);
      } else {
        await api('PUT',
          `/api/providers/${encodeURIComponent(p.id)}/models/${encodeURIComponent(mid)}`, body);
      }
      close();
      toast('已保存');
      renderModels(main);
    } catch (e) {
      $('#mm_msg', el).innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`;
    }
  };
}

function routingView(d) {
  const allRefs = [];
  d.providers.forEach((p) => {
    p.models.forEach((m) => {
      allRefs.push({
        ref: `${p.id}::${m.id}`,
        label: `${p.name || p.id} / ${m.name || m.id}`,
        disabled: !p.enabled || !m.enabled || !p.has_key,
      });
    });
  });
  const opts = (cur) => allRefs.map((x) => `
    <option value="${esc(x.ref)}" ${cur === x.ref ? 'selected' : ''}>
      ${esc(x.label)}${x.disabled ? '（不可用）' : ''}
    </option>`).join('');

  return `
  <div class="card">
    <h3 style="margin-top:0">按任务分配模型</h3>
    <p class="muted" style="margin-top:-4px;font-size:12px">
      不同任务对模型要求不同：翻译/校对要语言质量，Agent 意图解析用便宜模型就够。
      留空表示跟随默认。
    </p>
    <table>
      <thead><tr><th style="width:190px">任务</th><th>使用模型</th>
        <th style="width:210px">当前生效</th></tr></thead>
      <tbody>
        ${d.tasks.map((t) => `<tr>
          <td><b>${esc(L(t, 'label'))}</b>
            <div class="dim mono" style="font-size:10.5px">${esc(t.id)}</div></td>
          <td><select class="field r_sel" data-task="${esc(t.id)}">
            <option value="">（跟随默认）</option>
            ${opts(d.routing.by_tool[t.id] || '')}
          </select></td>
          <td class="mono ${d.effective[t.id] ? '' : 'dim'}" style="font-size:11px">
            ${esc(d.effective[t.id] || '未配置')}</td>
        </tr>`).join('')}
      </tbody>
    </table>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="grid c2">
      <div>
        <label class="lbl">默认模型（任务未指定时用）</label>
        <select class="field" id="r_default">
          <option value="">（未设置）</option>${opts(d.routing.default)}
        </select>
      </div>
      <div>
        <label class="lbl">回退链（主模型失败时按序尝试）</label>
        <input class="field" id="r_fallback"
               value="${esc((d.routing.fallback || []).join(', '))}"
               placeholder="pid::mid, 逗号分隔">
      </div>
    </div>
    <div class="row" style="margin-top:10px">
      <span class="dim" id="routeMsg" style="font-size:11.5px"></span>
      <div class="spacer"></div>
      <button class="btn primary" id="saveRouting">保存路由</button>
    </div>
  </div>`;
}

function bindRouting(main, d) {
  $('#saveRouting').onclick = async () => {
    const byTool = {};
    main.querySelectorAll('.r_sel').forEach((sel) => {
      if (sel.value) byTool[sel.dataset.task] = sel.value;
    });
    const fallback = ($('#r_fallback').value || '')
      .split(',').map((x) => x.trim()).filter(Boolean);
    try {
      await api('PUT', '/api/routing', {
        default: $('#r_default').value, fallback, by_tool: byTool,
      });
      toast('路由已保存');
      renderModels(main);
    } catch (e) {
      $('#routeMsg').innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`;
    }
  };
}

/* ================================================================== 数据 */
function barRow(label, value, max, suffix) {
  const pct = max > 0 ? Math.max(2, (value / max) * 100) : 0;
  return `<div class="statrow">
    <span class="sl" title="${esc(label)}">${esc(label)}</span>
    <span class="sb"><i style="width:${pct}%"></i></span>
    <span class="sv">${esc(suffix)}</span>
  </div>`;
}

async function renderData(main) {
  const ep = renderEpoch;
  main.innerHTML = '<div class="empty">加载中…</div>';
  let d;
  try {
    d = await api('GET', '/api/usage/summary?days=14');
  } catch (e) {
    if (ep !== renderEpoch) return;
    main.innerHTML = `<div class="card">读取统计失败：${esc(e.message)}</div>`;
    return;
  }
  if (ep !== renderEpoch) return;   // 切页了就别再往 #main 画
  const t = d.today, mo = d.month, all = d.total;
  // 下限用 1e-9 而不是 1：花费常是小数，用 1 做下限会把所有条压成 2%
  const maxDaily = Math.max(...d.daily.map((x) => x.cost_usd), 1e-9);
  const maxModel = Math.max(...d.by_model.map((x) => x.cost_usd), 1e-9);
  const maxTool = Math.max(...d.by_tool.map((x) => x.cost_usd), 1e-9);

  main.innerHTML = `
    ${pageHead('数据', `近 14 天用量 · 累计 ${fmtNum(all.tokens)} tokens`,
      '<button class="btn" id="refreshData">刷新</button>')}

    <div class="metrics">
      ${metric('今日调用', t.calls, { ico: '⧗' })}
      ${metric('今日 Tokens', fmtNum(t.tokens), { ico: '▤' })}
      ${metric('今日花费', '$' + t.cost_usd.toFixed(4), { cls: 'accent', ico: '$' })}
      ${metric('本月花费', '$' + mo.cost_usd.toFixed(4), { ico: '$' })}
      ${metric('累计 Tokens', fmtNum(all.tokens), { ico: '▥' })}
      ${metric('累计调用', all.calls, { ico: '↻' })}
      ${metric('缓存命中', fmtNum(all.cached_tokens), { ico: '⚡' })}
      ${metric('累计花费', '$' + all.cost_usd.toFixed(4), { ico: '∑' })}
    </div>

    <h3>每日花费（近 14 天）</h3>
    <div class="card">
      ${d.daily.length ? d.daily.slice().reverse().map((x) =>
        barRow(x.day, x.cost_usd, maxDaily, `$${x.cost_usd.toFixed(4)} · ${x.calls} 次 · ${fmtNum(x.tokens)} tok`)
      ).join('') : '<div class="empty">还没有调用记录</div>'}
    </div>

    <div class="grid c2">
      <div>
        <h3>按模型</h3>
        <div class="card">
          ${d.by_model.length ? d.by_model.map((x) =>
            barRow(x.model, x.cost_usd, maxModel, `$${x.cost_usd.toFixed(4)} · ${fmtNum(x.tokens)} tok`)
          ).join('') : '<div class="empty">—</div>'}
        </div>
      </div>
      <div>
        <h3>按工具</h3>
        <div class="card">
          ${d.by_tool.length ? d.by_tool.map((x) =>
            barRow(x.tool, x.cost_usd, maxTool, `$${x.cost_usd.toFixed(4)} · ${fmtNum(x.tokens)} tok`)
          ).join('') : '<div class="empty">—</div>'}
        </div>
      </div>
    </div>

    <h3>按任务（最近 30 个）</h3>
    <div class="card">
      ${d.by_job.length ? `<table>
        <thead><tr><th>任务</th><th>工具</th><th class="num">调用</th><th class="num">Tokens</th><th class="num">花费</th></tr></thead>
        <tbody>${d.by_job.map((x) => `<tr>
          <td class="mono">${esc(x.job_id)}</td><td>${esc(x.tool)}</td>
          <td class="num">${x.calls}</td><td class="num">${fmtNum(x.tokens)}</td>
          <td class="num">$${x.cost_usd.toFixed(4)}</td></tr>`).join('')}</tbody>
      </table>` : '<div class="empty">还没有任务级数据</div>'}
    </div>`;
  $('#refreshData').onclick = () => renderData(main);
}

/* ================================================================== 日志 */
async function renderLogs(main) {
  const f = state.logFilter || (state.logFilter = { level: '', keyword: '', job_id: '' });
  main.innerHTML = `
    ${pageHead('日志', '按级别 / 关键字 / 任务 ID 过滤；落盘文件可下载',
      '<button class="btn" id="refreshLogs">刷新</button>')}
    <div class="card" style="margin-bottom:12px">
      <div class="row">
        <select class="field" id="l_level" style="width:130px">
          <option value="">全部级别</option>
          <option value="debug"${f.level === 'debug' ? ' selected' : ''}>debug</option>
          <option value="info"${f.level === 'info' ? ' selected' : ''}>info</option>
          <option value="warn"${f.level === 'warn' ? ' selected' : ''}>warn</option>
          <option value="error"${f.level === 'error' ? ' selected' : ''}>error</option>
        </select>
        <input class="field" id="l_kw" placeholder="关键字" value="${esc(f.keyword)}" style="flex:1">
        <input class="field" id="l_job" placeholder="任务 ID" value="${esc(f.job_id)}" style="width:150px">
        <button class="btn" id="l_search">筛选</button>
        <button class="btn" id="l_clear">清空缓冲</button>
      </div>
      <div class="muted" id="l_stats" style="font-size:12px;margin-top:8px"></div>
    </div>
    <div class="card" style="padding:0">
      <div class="logview" id="l_body"><div class="empty">加载中…</div></div>
    </div>
    <h3>日志文件</h3>
    <div class="card" id="l_files"><div class="empty">—</div></div>`;

  const load = async () => {
    f.level = $('#l_level').value;
    f.keyword = $('#l_kw').value.trim();
    f.job_id = $('#l_job').value.trim();
    const q = new URLSearchParams();
    if (f.level) q.set('level', f.level);
    if (f.keyword) q.set('keyword', f.keyword);
    if (f.job_id) q.set('job_id', f.job_id);
    q.set('limit', '400');
    try {
      const d = await api('GET', '/api/logs?' + q.toString());
      // 视图可能已经切走，元素没了就别再写
      const statsEl = $('#l_stats');
      const bodyEl = $('#l_body');
      if (!statsEl || !bodyEl) return;
      const st = d.stats;
      statsEl.textContent =
        `缓冲 ${st.buffer} 条 · debug ${st.levels.debug} / info ${st.levels.info} / warn ${st.levels.warn} / error ${st.levels.error} · 磁盘 ${st.files} 个文件 ${(st.disk_bytes / 1024).toFixed(0)} KB`;
      $('#l_body').innerHTML = d.rows.length
        ? d.rows.map((r) => `<div class="l ${esc(r.level)}"><span class="t">${esc(r.date)} ${esc(r.time)}</span>
            <span class="src">${esc(r.source)}${r.job_id ? ' · ' + esc(r.job_id) : ''}</span>
            <span class="msg">${esc(r.msg)}</span></div>`).join('')
        : '<div class="empty">没有匹配的日志</div>';
    } catch (e) {
      $('#l_body').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    }
    try {
      const d2 = await api('GET', '/api/logs/files');
      $('#l_files').innerHTML = d2.files.length
        ? `<table><thead><tr><th>文件</th><th class="num" style="width:90px">大小</th>
             <th style="width:80px"></th></tr></thead><tbody>
           ${d2.files.map((x) => `<tr><td class="mono">${esc(x.name)}</td>
             <td class="num">${(x.size / 1024).toFixed(1)} KB</td>
             <td style="text-align:right;white-space:nowrap">
               <a class="btn sm" style="text-decoration:none"
                  href="/api/logs/download?name=${encodeURIComponent(x.name)}&tail=3000">下载</a></td></tr>`).join('')}
           </tbody></table>`
        : '<div class="empty">还没有落盘日志</div>';
    } catch { /* 忽略 */ }
  };

  $('#refreshLogs').onclick = load;
  $('#l_search').onclick = load;
  $('#l_level').onchange = load;
  $('#l_kw').onkeydown = (e) => { if (e.key === 'Enter') load(); };
  $('#l_job').onkeydown = (e) => { if (e.key === 'Enter') load(); };
  $('#l_clear').onclick = async () => {
    try { await api('DELETE', '/api/logs/buffer'); toast('缓冲已清空（磁盘文件保留）'); load(); }
    catch (e) { toast(e.message, true); }
  };
  await load();
}

/* ================================================================== 开销 */
async function renderUsage(main) {
  const ep = renderEpoch;
  main.innerHTML = '<div class="empty">加载中…</div>';
  let sum, budget;
  try {
    [sum, budget] = await Promise.all([
      api('GET', '/api/usage/summary?days=7'),
      api('GET', '/api/budget'),
    ]);
  } catch (e) {
    if (ep !== renderEpoch) return;
    main.innerHTML = `<div class="card">读取失败：${esc(e.message)}</div>`;
    return;
  }
  if (ep !== renderEpoch) return;   // 切页了就别再往 #main 画
  const b = budget.budget;
  const ruleRow = (key, label, hint) => {
    const r = b[key] || {};
    return `<tr data-rule="${key}">
      <td><b>${esc(label)}</b><div class="muted" style="font-size:11.5px">${esc(hint)}</div></td>
      <td><input type="checkbox" class="b_en" ${r.enabled ? 'checked' : ''}></td>
      <td><input class="field b_tok" type="number" value="${r.max_tokens || 0}" style="width:110px"></td>
      <td><input class="field b_usd" type="number" step="0.01" value="${r.max_usd || 0}" style="width:100px"></td>
      <td><select class="field b_act" style="width:110px">
        ${['warn', 'stop', 'downgrade'].map((a) => `<option value="${a}"${r.action === a ? ' selected' : ''}>${
          a === 'warn' ? '仅告警' : a === 'stop' ? '硬拦截' : '切备用'}</option>`).join('')}
      </select></td>
      <td><input class="field b_fb" value="${esc(r.fallback_model || '')}" placeholder="备用模型" style="width:130px"></td>
    </tr>`;
  };

  const priceRows = Object.entries(sum.pricing).slice(0, 40).map(([k, v]) => `
    <tr><td class="mono">${esc(k)}</td>
      <td class="num">$${v.input}</td><td class="num">$${v.input_cached}</td>
      <td class="num">$${v.output}</td><td class="num">$${v.cache_creation}</td></tr>`).join('');

  main.innerHTML = `
    ${pageHead('开销', `当前模型 ${esc(sum.current_model)} · 今日 $${sum.today.cost_usd.toFixed(4)}`,
      '<button class="btn" id="refreshUsage">刷新</button>')}

    <div class="metrics">
      ${metric('今日花费', '$' + sum.today.cost_usd.toFixed(4), { cls: 'accent', ico: '$' })}
      ${metric('本月花费', '$' + sum.month.cost_usd.toFixed(4), { ico: '∑' })}
      ${metric('今日 Tokens', fmtNum(sum.today.tokens), { ico: '▤' })}
      ${metric('今日调用', sum.today.calls, { ico: '↻' })}
      ${metric('累计花费', '$' + sum.total.cost_usd.toFixed(4), { ico: '▦' })}
      ${metric('累计调用', sum.total.calls, { ico: '⧗' })}
    </div>

    <h3>预算护栏</h3>
    <p class="muted" style="margin-top:-4px;font-size:12.5px">
      ${t('任一上限触发即按「动作」处理。')}
      <b>${t('硬拦截')}</b>${t('：会在任务开始前就拒绝执行。')}
      <b>${t('切备用')}</b>${t('：当前只告警并提示备用模型（自动切换需重建连接，暂未启用）。')}
    </p>
    <div class="card" style="padding:0;overflow:auto">
      <table>
        <thead><tr><th>维度</th><th>启用</th><th>Token 上限</th><th>美元上限</th><th>动作</th><th>备用模型</th></tr></thead>
        <tbody>
          ${ruleRow('per_job', '单次任务', '一次 translate/review 跑飞最烧钱，建议设紧')}
          ${ruleRow('daily', '每日', '当天所有任务累计')}
          ${ruleRow('per_model', '单模型每日', '约束高价模型')}
          ${ruleRow('monthly', '每月', '最终月度上限')}
        </tbody>
      </table>
    </div>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" id="saveBudget">保存预算</button>
      <span class="muted" id="budgetMsg" style="font-size:12.5px"></span>
    </div>

    ${sum.budget_events && sum.budget_events.length ? `
      <h3>最近的预算事件</h3>
      <div class="card" style="padding:0;overflow:auto"><table>
        <thead><tr><th>时间</th><th>维度</th><th>动作</th><th>说明</th></tr></thead>
        <tbody>${sum.budget_events.map((e) => `<tr>
          <td class="mono">${fmtTime(e.ts)}</td><td>${esc(e.scope)}</td>
          <td>${esc(e.action)}</td><td class="muted">${esc(e.detail)}</td></tr>`).join('')}</tbody>
      </table></div>` : ''}

    <h3>定价（USD / 百万 token）</h3>
    <div class="card" style="padding:0;overflow:auto;max-height:320px">
      <table>
        <thead><tr><th>模型</th><th class="num">输入</th><th class="num">缓存命中</th>
          <th class="num">输出</th><th class="num">缓存写入</th></tr></thead>
        <tbody>${priceRows}</tbody>
      </table>
    </div>
    <div class="card" style="margin-top:10px">
      <div class="row">
        <input class="field" id="p_model" placeholder="模型名（覆盖或新增）" style="width:200px">
        <input class="field" id="p_in" type="number" step="0.01" placeholder="输入" style="width:90px">
        <input class="field" id="p_cached" type="number" step="0.01" placeholder="缓存" style="width:90px">
        <input class="field" id="p_out" type="number" step="0.01" placeholder="输出" style="width:90px">
        <button class="btn" id="savePricing">写入定价</button>
      </div>
      <div class="muted" style="font-size:12px;margin-top:6px">
        ${t('模型名匹配：精确 &gt; 前缀 &gt; 子串，最长优先。所以')} <code>gpt-4o-mini-2024-07-18</code> ${t('会自动命中')} <code>gpt-4o-mini</code>。
      </div>
    </div>

    <h3>最近调用</h3>
    <div class="card" style="padding:0;overflow:auto;max-height:320px">
      <table>
        <thead><tr><th>时间</th><th>模型</th><th>工具</th><th>任务</th>
          <th class="num">输入</th><th class="num">输出</th><th class="num">花费</th><th class="num">耗时</th></tr></thead>
        <tbody id="u_recent"><tr><td colspan="8" class="muted">加载中…</td></tr></tbody>
      </table>
    </div>`;

  $('#refreshUsage').onclick = () => renderUsage(main);
  $('#saveBudget').onclick = async () => {
    const out = {};
    main.querySelectorAll('tr[data-rule]').forEach((tr) => {
      const key = tr.dataset.rule;
      out[key] = {
        enabled: tr.querySelector('.b_en').checked,
        max_tokens: Number(tr.querySelector('.b_tok').value) || 0,
        max_usd: Number(tr.querySelector('.b_usd').value) || 0,
        action: tr.querySelector('.b_act').value,
        fallback_model: tr.querySelector('.b_fb').value.trim(),
      };
    });
    try {
      await api('PUT', '/api/budget', out);
      $('#budgetMsg').innerHTML = '<span style="color:var(--ok)">已保存并生效</span>';
      toast('预算已保存');
    } catch (e) {
      $('#budgetMsg').innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`;
    }
  };
  $('#savePricing').onclick = async () => {
    const model = $('#p_model').value.trim();
    if (!model) return toast('填模型名', true);
    const p = {};
    const i = $('#p_in').value, c = $('#p_cached').value, o = $('#p_out').value;
    if (i) p.input = Number(i);
    if (c) p.input_cached = Number(c);
    if (o) p.output = Number(o);
    if (!Object.keys(p).length) return toast('至少填一个价格', true);
    try {
      await api('PUT', '/api/usage/pricing', { [model]: p });
      toast('定价已写入');
      renderUsage(main);
    } catch (e) { toast(e.message, true); }
  };

  try {
    const r = await api('GET', '/api/usage/recent?limit=40');
    $('#u_recent').innerHTML = r.rows.length ? r.rows.map((x) => `<tr>
      <td class="mono">${fmtTime(x.ts)}</td><td class="mono">${esc(x.model)}</td>
      <td>${esc(x.tool || '—')}</td><td class="mono">${esc((x.job_id || '—').slice(0, 8))}</td>
      <td class="num">${x.prompt_tokens}</td><td class="num">${x.completion_tokens}</td>
      <td class="num">$${(x.cost_usd || 0).toFixed(6)}</td><td class="num">${x.latency_ms}ms</td></tr>`).join('')
      : '<tr><td colspan="8" class="muted">还没有调用</td></tr>';
  } catch { /* 忽略 */ }
}


/* 确认弹窗。原生 confirm() 的按钮由系统语言决定、正文也不进 DOM，
 * 英文界面里会中英混排，所以统一用自家弹窗。 */
function askConfirm(message) {
  return new Promise((resolve) => {
    const el = document.createElement('div');
    el.className = 'modal';
    el.innerHTML = `
      <div class="modal-box" style="width:min(460px,92vw)">
        <div class="modal-head"><b>${t('请确认')}</b></div>
        <div class="modal-body" style="font-size:13px;line-height:1.7">${esc(message)}</div>
        <div class="modal-foot">
          <div class="spacer"></div>
          <button class="btn" id="cf_no">${t('取消')}</button>
          <button class="btn danger" id="cf_yes">${t('确定')}</button>
        </div>
      </div>`;
    document.body.appendChild(el);
    const done = (v) => { el.remove(); resolve(v); };
    el.onclick = (ev) => { if (ev.target === el) done(false); };
    el.querySelector('#cf_no').onclick = () => done(false);
    el.querySelector('#cf_yes').onclick = () => done(true);
    document.addEventListener('keydown', function esc(ev) {
      if (ev.key !== 'Escape') return;
      document.removeEventListener('keydown', esc);
      done(false);
    });
  });
}

/* 首次启动的免费声明。文案与仓库地址由后端给（`GET /api/notice`），
 * 中英两段**同时**展示：这是给"从付费渠道拿到软件的人"看的，不该因为
 * 界面语言是英文就漏掉中文原文。 */
function freeNoticeDialog(info) {
  const el = document.createElement('div');
  el.className = 'modal';
  el.innerHTML = `
    <div class="modal-box" style="width:min(600px,94vw)">
      <div class="modal-head"><b>${t('本软件完全免费')} · Free of charge</b></div>
      <div class="modal-body">
        <div class="notice safe" style="margin-bottom:12px;font-size:13px;line-height:1.75">
          <div class="dim mono" style="font-size:10.5px;margin-bottom:4px">中文</div>
          <div>${esc(info.text_zh || '')}</div>
          <div class="dim mono" style="font-size:10.5px;margin:10px 0 4px">English</div>
          <div>${esc(info.text_en || '')}</div>
        </div>
        <div style="font-size:12px;line-height:1.8">
          <div>${t('官方仓库')}${isEn() ? ': ' : '：'}
            <a href="${esc(info.repo || '')}" target="_blank" rel="noopener noreferrer">${esc(info.repo || '')}</a>
          </div>
          <div class="dim">${t('随包还附有说明文件')}：<code>${esc(info.notice_file || 'NOTICE.txt')}</code>
            （${t('本提示只在这台机器首次启动时出现一次')}）</div>
        </div>
      </div>
      <div class="modal-foot">
        <div class="spacer"></div>
        <button class="btn primary" id="fn_ok">${t('我知道了')}</button>
      </div>
    </div>`;
  document.body.appendChild(el);
  const close = () => {
    el.remove();
    // 记到后端（按这台机器算"看过"）；失败也不打扰用户，下次再弹一次而已
    api('POST', '/api/notice/ack').catch(() => {});
  };
  el.querySelector('#fn_ok').onclick = close;
  el.onclick = (ev) => { if (ev.target === el) close(); };
}

/* ================================================================== 提取 */
// 倒计时按钮文案：每秒都在变，字典匹配不了，按语言直接给两种写法
function countdownText(sec) {
  return isEn() ? `Please read (${sec}s)` : `请阅读（${sec}s）`;
}

// 执行提取器 = 在本机跑任意代码，必须显式确认
function confirmRisk(ext) {
  return new Promise((resolve) => {
    const el = document.createElement('div');
    el.className = 'modal';
    el.innerHTML = `
      <div class="modal-box" style="width:min(560px,92vw)">
        <div class="modal-head" style="color:var(--err)">
          <b>${t('⚠ 执行提取器 = 在本机运行任意代码')}</b>
        </div>
        <div class="modal-body">
          <div class="notice warn" style="margin-bottom:12px">
            <div class="dim mono" style="font-size:11.5px;margin-bottom:6px">${esc(L(ext, 'name'))}</div>
            <div>${t('提取器脚本会以当前用户权限在本机运行，并且可以：')}</div>
            <ul style="margin:8px 0 0 18px;padding:0;line-height:1.7">
              <li>${t('读取、修改、删除你电脑上的任何文件')}</li>
              <li>${t('访问网络、上传数据')}</li>
              <li>${t('执行任意系统命令')}</li>
            </ul>
          </div>
          <div style="font-size:12.5px;color:var(--muted);line-height:1.7">
            <div>${t('请只运行你自己写的或来源可信的提取器。')}</div>
            <div>${t('本工具不对提取器的行为做任何限制或担保。')}</div>
            <div style="color:var(--warn);font-weight:600">${t('因执行提取器产生的一切后果由使用者自行承担。')}</div>
          </div>
          ${ext.sends_samples ? `
          <div class="notice" style="margin-top:12px;font-size:12.5px;line-height:1.7">
            <div>${t('本次动作会把该目录下若干文件的内容片段发送给你配置的模型供应商，用于理解数据格式。')}</div>
            <div>${t('凭据与运行数据类文件（.env、settings.json、数据库、日志、私钥等）不会被读取，片段里的密钥形态串也会先被抹除。')}</div>
          </div>` : ''}
          <label class="switch" style="margin-top:14px">
            <input type="checkbox" id="risk_ok">
            <span>${t('我已阅读并了解上述风险，确认执行')}</span>
          </label>
        </div>
        <div class="modal-foot">
          <div class="spacer"></div>
          <button class="btn" id="risk_cancel">${t('取消')}</button>
          <button class="btn danger" id="risk_go" disabled>${countdownText(5)}</button>
        </div>
      </div>`;
    document.body.appendChild(el);

    const go = $('#risk_go', el);
    const chk = $('#risk_ok', el);
    let left = 5;
    const timer = setInterval(() => {
      left -= 1;
      if (left > 0) {
        go.textContent = countdownText(left);
        return;
      }
      clearInterval(timer);
      go.textContent = t('确认执行');
      go.disabled = !chk.checked;
    }, 1000);

    chk.onchange = () => {
      if (left > 0) return;
      go.disabled = !chk.checked;
    };

    const close = (val) => { clearInterval(timer); el.remove(); resolve(val); };
    el.onclick = (ev) => { if (ev.target === el) close(false); };
    $('#risk_cancel', el).onclick = () => close(false);
    go.onclick = () => close(true);
  });
}

/* ---- Agent 生成 / 改进提取器 ---- */
function agentScriptDialog(mode, ext, main) {
  const isGen = mode === 'generate';
  const el = document.createElement('div');
  el.className = 'modal';
  el.innerHTML = `
    <div class="modal-box" style="width:min(680px,94vw)">
      <div class="modal-head">
        <b>${isGen ? t('让 Agent 写一个提取器') : t('让 Agent 改进提取器')}</b>
        ${isGen ? '' : `<span class="mono dim">${esc(ext.id)}</span>`}
      </div>
      <div class="modal-body">
        <div class="formrow">
          <label class="lbl">${t('游戏目录（Agent 要读这里的文件）')}</label>
          <input class="field" id="ag_game" placeholder="例如 D:\\Games\\MyGame"
                 value="${esc((($('#x_game') || {}).value || '').trim())}">
        </div>
        <div class="formrow">
          <label class="lbl">${isGen ? t('额外要求（可选）') : t('想改进什么（可选）')}</label>
          <textarea class="field" id="ag_hint" rows="3"
            placeholder="${isGen
              ? '例如：只抽对话，不要物品名；编码是 GBK'
              : '例如：漏了 Map 里的对话；把纯数字过滤掉'}"></textarea>
        </div>
        <div class="notice" style="font-size:12px">
          <div>${isGen
            ? t('Agent 会先画像目录、采样文件内容，再生成 extractor.yaml 与 run.py，然后自动试跑一次并把校验结果给你看。')
            : t('Agent 会看现有脚本、上次运行日志和你写的反馈，生成改进版（旧脚本会备份到 .backup/ 目录），然后自动试跑。')}</div>
          <div style="margin-top:6px">${t('生成的脚本会被限制在「读文件 + 写 JSONL」范围内（禁止 subprocess / socket / eval 等），但仍会以当前用户权限执行。')}</div>
        </div>
        <div id="ag_out"></div>
      </div>
      <div class="modal-foot">
        <span class="dim" id="ag_msg" style="font-size:11.5px"></span>
        <div class="spacer"></div>
        <button class="btn" id="ag_cancel">${t('关闭')}</button>
        <button class="btn primary" id="ag_go">${isGen ? t('开始生成') : t('开始改进')}</button>
      </div>
    </div>`;
  document.body.appendChild(el);

  let aborted = false;
  const close = () => { aborted = true; el.remove(); };
  el.onclick = (ev) => { if (ev.target === el) close(); };
  $('#ag_cancel', el).onclick = close;

  const out = $('#ag_out', el);
  const msg = $('#ag_msg', el);

  const renderResult = (r) => {
    const t = r.trial || {};
    const v = t.validate || {};
    const st = v.stats || {};
    const sample = (t.sample || []).map((e) => `<tr>
      <td class="mono" style="font-size:11px">${esc(e.key)}</td>
      <td>${esc(e.original)}</td>
      <td class="dim" style="font-size:11px">${esc(e.context || '')}</td>
    </tr>`).join('');
    out.innerHTML = `
      <div style="margin-top:12px;border-top:1px solid var(--border);padding-top:10px">
        <div class="row" style="gap:8px;margin-bottom:8px">
          <b style="font-size:13px">结果</b>
          <span class="tag ok">${esc(r.extractor.id)}</span>
          ${t.ok
            ? `<span class="tag ok">提取 ${t.entries || 0} 条 · ${t.duration_ms || 0}ms</span>`
            : '<span class="tag w">试跑失败</span>'}
          ${st.error_count ? `<span class="tag w">${st.error_count} 错误</span>` : ''}
          ${st.warning_count ? `<span class="tag">${st.warning_count} 警告</span>` : ''}
        </div>
        ${t.ok ? '' : `<pre class="result" style="color:var(--err)">${esc(t.error || '')}</pre>`}
        ${sample ? `<div style="max-height:220px;overflow:auto;margin-top:6px">
          <table><thead><tr><th style="width:36%">key</th><th>原文</th>
            <th style="width:20%">上下文</th></tr></thead><tbody>${sample}</tbody></table>
        </div>` : (t.ok ? '<div class="dim" style="font-size:12px">这次试跑没有提取到词条</div>' : '')}
        ${t.ok && t.job_id
          ? `<div class="row" style="margin-top:8px">
               <button class="btn sm" id="ag_view">查看全部词条</button>
             </div>` : ''}
      </div>`;
    const vb = $('#ag_view', el);
    if (vb) vb.onclick = () => { close(); showExtractionDetail(main, t.job_id); };
  };

  $('#ag_go', el).onclick = async () => {
    const gameDir = ($('#ag_game', el).value || '').trim();
    if (!gameDir) return toast('先填游戏目录', true);
    const hint = ($('#ag_hint', el).value || '').trim();

    // 生成/改进都要把目录样例发给模型，风险确认里必须写明这一条
    const agreed = await confirmRisk(
      isGen ? { name: 'Agent 生成的脚本', sends_samples: true }
            : { ...(ext || {}), sends_samples: true }
    );
    if (!agreed) return;

    const btn = $('#ag_go', el);
    btn.disabled = true;
    msg.innerHTML = '<span class="dim">已提交…</span>';
    try {
      const url = isGen
        ? '/api/extractors/generate'
        : `/api/extractors/${encodeURIComponent(ext.id)}/improve`;
      const payload = isGen
        ? { game_dir: gameDir, hint, accept_risk: true }
        : { game_dir: gameDir, feedback: hint, accept_risk: true };
      const r = await api('POST', url, payload);
      const jid = r.job.id;

      for (let i = 0; i < 600; i++) {
        if (aborted) return;   // 弹窗已关：立刻停，别空跑 10 分钟
        await new Promise((res) => setTimeout(res, 1000));
        if (aborted) return;
        const j = (await api('GET', `/api/jobs/${jid}`)).job;
        if (j.status === 'done') {
          msg.innerHTML = '<span style="color:var(--ok)">✓ 完成</span>';
          renderResult(j.result || {});
          btn.disabled = false;
          btn.textContent = isGen ? '再生成一个' : '再改一版';
          return;
        }
        if (j.status === 'failed') {
          msg.innerHTML = `<span style="color:var(--err)">✗ ${esc((j.error || '').slice(0, 200))}</span>`;
          out.innerHTML = `<pre class="result" style="color:var(--err);margin-top:10px">${esc(j.error || '')}</pre>`;
          btn.disabled = false;
          return;
        }
        const last = (j.logs || []).slice(-1)[0];
        msg.innerHTML = `<span class="dim">${esc(last ? last.msg : '运行中…')}</span>`;
      }
      msg.innerHTML = '<span style="color:var(--warn)">等待超时，去「提取记录」看结果</span>';
      btn.disabled = false;
    } catch (e) {
      msg.innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`;
      btn.disabled = false;
    }
  };
}

async function renderExtract(main) {
  const ep = renderEpoch;
  let d;
  try {
    d = await api('GET', '/api/extractors');
  } catch (e) {
    if (ep !== renderEpoch) return;
    main.innerHTML = pageHead('提取', '读取失败') +
      `<div class="card"><span style="color:var(--err)">${esc(e.message)}</span></div>`;
    return;
  }
  const tab = state.extractTab || 'run';
  const list = d.extractors || [];
  if (!list.some((x) => x.id === state.selExtractor)) {
    state.selExtractor = list.length ? list[0].id : '';
  }
  const sel = list.find((x) => x.id === state.selExtractor) || null;
  const jobs = state.extractJobs || [];
  if (ep !== renderEpoch) return;

  main.innerHTML = `
    ${pageHead(
      '提取',
      `${list.length} 个提取器 · ${t('从游戏文件抽词条 → 校验 → 导入平台')}`,
      '<button class="btn primary" id="addExtractor">+ 导入提取器</button>'
    )}
    <div class="subtabs">
      <button data-xtab="run" class="${tab === 'run' ? 'on' : ''}">提取器</button>
      <button data-xtab="jobs" class="${tab === 'jobs' ? 'on' : ''}">提取记录</button>
    </div>
    ${tab === 'run' ? extractRunView(list, sel) : extractJobsView(jobs)}
    <div id="extractOut"></div>`;

  main.querySelectorAll('[data-xtab]').forEach((b) => {
    b.onclick = async () => {
      state.extractTab = b.dataset.xtab;
      if (b.dataset.xtab === 'jobs') await loadExtractJobs();
      renderExtract(main);
    };
  });
  $('#addExtractor').onclick = () => {
    const p = prompt('提取器目录的绝对路径（需含 extractor.yaml 与入口脚本）');
    if (!p) return;
    api('POST', '/api/extractors', { path: p })
      .then(() => { toast('已导入'); renderExtract(main); })
      .catch((e) => toast(e.message, true));
  };

  if (tab === 'run' && sel) bindExtractRun(main, sel);
  if (tab === 'jobs') bindExtractJobs(main);
}

function extractItem(x, sel) {
  return `<button class="mitem ${sel && x.id === sel.id ? 'on' : ''}" data-eid="${esc(x.id)}">
    <span class="mdot ${x.enabled === false ? '' : 'ok'}"></span>
    <span class="mi-main">
      <b>${esc(L(x, 'name'))}</b>
      <span class="mono dim">${esc(x.engine)} · v${esc(x.version)}</span>
    </span>
    ${x.source === 'agent' ? '<span class="tag ok">Agent</span>' : ''}
    ${x.builtin ? '<span class="tag">内置</span>' : ''}
  </button>`;
}

function extractRunView(list, sel) {
  const agentMade = list.filter((x) => x.source === 'agent');
  const others = list.filter((x) => x.source !== 'agent');

  const group = (title, arr, hint) => `
    <div class="navsep" style="padding:10px 4px 4px">${title}（${arr.length}）</div>
    ${arr.length ? arr.map((x) => extractItem(x, sel)).join('')
      : `<div class="dim" style="font-size:11.5px;padding:2px 6px 6px">${hint}</div>`}`;

  return `<div class="msplit">
    <div class="mlist">
      <button class="btn primary" id="agentNew" style="margin-bottom:8px">
        🤖 让 Agent 写一个
      </button>
      ${group('Agent 生成', agentMade, '还没有 —— 点上面按钮，让 Agent 分析游戏目录后自动写')}
      ${group('导入 / 内置', others, '没有导入的脚本')}
    </div>
    <div class="mdetail">${sel ? extractForm(sel) : '<div class="card"><div class="empty">选一个提取器</div></div>'}</div>
  </div>`;
}

function extractForm(x) {
  const args = (x.args || []).map((a) => `
    <div class="formrow">
      <label class="lbl">${esc(L(a, 'label') || a.name)}${(L(a, 'hint')) ? `<span class="dim" style="font-weight:400"> · ${esc(L(a, 'hint'))}</span>` : ''}</label>
      <input class="field x_arg" data-arg="${esc(a.name)}" value="${esc(a.default || '')}">
    </div>`).join('');

  return `
  <div class="notice warn" style="margin-bottom:12px">
    <b>${t('⚠ 提取器会在本机执行代码')}</b>
    <div style="margin-top:4px">${t('下方脚本以当前用户权限运行，可读写任意文件、访问网络。')}</div>
    <div>${t('只运行你自己写的或来源可信的提取器，执行后果由使用者自行承担。')}</div>
  </div>
  <div class="card">
    <div class="row" style="justify-content:space-between;margin-bottom:10px">
      <div class="row" style="gap:6px">
        <b style="font-size:14px">${esc(L(x, 'name'))}</b>
        <span class="mono dim">${esc(x.id)}</span>
        <span class="tag">${esc(x.engine)}</span>
        ${x.source === 'agent' ? '<span class="tag ok">Agent 生成</span>' : ''}
        ${x.builtin ? '<span class="tag">内置</span>' : ''}
      </div>
      <div class="row">
        <button class="btn sm" id="improveExtractor">🤖 ${t('让 Agent 改进')}</button>
        ${x.builtin ? '' : '<button class="btn danger sm" id="delExtractor">删除</button>'}
      </div>
    </div>
    <div class="muted" style="font-size:12px;margin-bottom:10px">${esc(L(x, 'desc'))}</div>
    ${L(x, 'input_hint') ? `<div class="dim" style="font-size:11.5px;margin-bottom:10px">${esc(isEn() ? 'Input: ' : '输入：')}${esc(L(x, 'input_hint'))}</div>` : ''}

    <div class="formrow">
      <label class="lbl">游戏目录（绝对路径）</label>
      <div class="row">
        <input class="field" id="x_game" style="flex:1" placeholder="例如 D:\\Games\\MyGame" value="${esc(state._extractGameDir || '')}">
        <button class="btn" id="x_analyze">分析目录</button>
      </div>
    </div>

    <div class="grid c2">${args}</div>

    <div class="row" style="margin-top:8px">
      <span class="dim" id="x_msg" style="font-size:11.5px"></span>
      <div class="spacer"></div>
      <button class="btn primary" id="x_run">开始提取</button>
    </div>
  </div>
  <div id="x_analyze_out"></div>`;
}

function bindExtractRun(main, x) {
  main.querySelectorAll('.mitem').forEach((b) => {
    b.onclick = () => {
      // 切提取器时把已填的游戏目录带上，别让用户每次重填
      const g = $('#x_game');
      if (g) state._extractGameDir = g.value;
      state.selExtractor = b.dataset.eid;
      renderExtract(main);
    };
  });

  const gameDir = () => ($('#x_game').value || '').trim();
  const options = () => {
    const o = {};
    main.querySelectorAll('.x_arg').forEach((el) => {
      if (el.value.trim()) o[el.dataset.arg] = el.value.trim();
    });
    return o;
  };

  const newBtn = $('#agentNew');
  if (newBtn) newBtn.onclick = () => agentScriptDialog('generate', null, main);

  const impBtn = $('#improveExtractor');
  if (impBtn) impBtn.onclick = () => agentScriptDialog('improve', x, main);

  $('#x_analyze').onclick = async () => {
    const out = $('#x_analyze_out');
    if (!gameDir()) return toast('先填游戏目录', true);
    out.innerHTML = '<div class="dim" style="margin-top:10px">分析中…</div>';
    try {
      const r = await api('POST', '/api/extract/analyze', { game_dir: gameDir() });
      const ext = Object.entries(r.by_ext || {})
        .map(([k, v]) => `<span class="tag">${esc(k)} × ${v}</span>`).join(' ');
      out.innerHTML = `
        <div class="card" style="margin-top:12px">
          <div class="row" style="gap:8px;margin-bottom:8px">
            <b>目录画像</b>
            <span class="tag ${r.engine_guess === 'unknown' ? 'w' : 'ok'}">
              猜测引擎：${esc(r.engine_guess)}</span>
            <span class="dim" style="font-size:11.5px">${r.files} 个文件${r.truncated ? '（已截断）' : ''}</span>
          </div>
          <div class="row" style="gap:4px;margin-bottom:8px">${ext || '<span class="dim">无扩展名统计</span>'}</div>
          ${(r.markers || []).length ? `<div class="dim" style="font-size:11.5px">命中特征：${r.markers.map(esc).join(' · ')}</div>` : ''}
          <details style="margin-top:8px">
            <summary class="dim" style="font-size:11.5px;cursor:pointer">文件样例（${(r.sample || []).length}）</summary>
            <pre class="result">${esc((r.sample || []).join('\n'))}</pre>
          </details>
        </div>`;
    } catch (e) {
      out.innerHTML = `<div style="color:var(--err);margin-top:10px;font-size:12px">${esc(e.message)}</div>`;
    }
  };

  $('#x_run').onclick = async () => {
    if (!gameDir()) return toast('先填游戏目录', true);
    if (state._extractRunning) return toast('已有提取任务在跑，等它结束再提交', true);
    const agreed = await confirmRisk(x);
    if (!agreed) return;
    state._extractRunning = true;
    const runBtn = $('#x_run');
    if (runBtn) { runBtn.disabled = true; runBtn.textContent = '运行中…'; }
    const msg = $('#x_msg');
    msg.innerHTML = '<span class="dim">已提交，跑完自动加载结果…</span>';
    try {
      const r = await api('POST', `/api/extractors/${encodeURIComponent(x.id)}/run`,
        { game_dir: gameDir(), options: options(), accept_risk: true });
      const jid = r.job.id;
      for (let i = 0; i < 400; i++) {
        await new Promise((res) => setTimeout(res, 500));
        const j = await api('GET', `/api/jobs/${jid}`);
        const job = j.job;
        if (job.status === 'done') {
          const jr = job.result || {};
          msg.innerHTML = `<span style="color:var(--ok)">✓ 提取 ${jr.entries || 0} 条（${jr.duration_ms || 0}ms）</span>`;
          toast(`提取完成：${jr.entries || 0} 条`);
          state.extractLastJob = jr.job_id;
          await showExtractionDetail(main, jr.job_id);
          return;
        }
        if (job.status === 'failed') {
          msg.innerHTML = `<span style="color:var(--err)">✗ ${esc(job.error || '失败')}</span>`;
          return;
        }
        msg.innerHTML = `<span class="dim">运行中… ${job.status}</span>`;
      }
      msg.innerHTML = '<span style="color:var(--warn)">等待超时，去「提取记录」看结果</span>';
    } catch (e) {
      msg.innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`;
    } finally {
      // 成功 / 失败 / 400（目录填错）都要解锁并把按钮放回来，
      // 否则用户按提示改好目录后再点，只会一直撞「已有提取任务在跑」。
      state._extractRunning = false;
      if (runBtn) { runBtn.disabled = false; runBtn.textContent = '开始提取'; }
    }
  };

  const del = $('#delExtractor');
  if (del) {
    del.onclick = async () => {
      if (!(await askConfirm(isEn()
        ? `Delete extractor "${L(x, 'name')}"?`
        : `删除提取器「${x.name}」？`))) return;
      try {
        await api('DELETE', `/api/extractors/${encodeURIComponent(x.id)}`);
        toast('已删除');
        renderExtract(main);
      } catch (e) { toast(e.message, true); }
    };
  }
}

async function loadExtractJobs() {
  try {
    const r = await api('GET', '/api/extractions?limit=50');
    state.extractJobs = r.rows || [];
  } catch { state.extractJobs = []; }
}

function extractJobsView(jobs) {
  if (!jobs.length) {
    return `<div class="card"><div class="empty">还没有提取记录</div></div>`;
  }
  return `<div class="card tight"><table>
    <thead><tr><th>时间</th><th>提取器</th><th class="num">词条</th>
      <th class="num">耗时</th><th>状态</th><th></th></tr></thead>
    <tbody>
      ${jobs.map((j) => `<tr>
        <td class="mono">${esc(String(j.job_id).slice(0, 15))}</td>
        <td>${esc((j.extractor || {}).name || j.extractor_id)}</td>
        <td class="num">${j.entries || 0}</td>
        <td class="num">${j.duration_ms || 0}ms</td>
        <td>${j.status === 'done'
          ? '<span class="status done">完成</span>'
          : `<span class="status failed" title="${esc(j.error || '')}">失败</span>`}</td>
        <td style="text-align:right;white-space:nowrap">
          <button class="btn sm j_view" data-jid="${esc(j.job_id)}">查看</button>
          ${j.status === 'done'
            ? `<button class="btn sm wb_open" data-jid="${esc(j.job_id)}">回写译文</button>`
            : '<button class="btn sm" disabled title="提取未成功，没有可回写的 key">回写译文</button>'}</td>
      </tr>`).join('')}
    </tbody>
  </table></div>`;
}

function bindExtractJobs(main) {
  main.querySelectorAll('.j_view').forEach((b) => {
    b.onclick = () => showExtractionDetail(main, b.dataset.jid);
  });
  // 反向链路：回写是最终产成品，行内按钮直接把人带到「回写」页并选中这条记录
  main.querySelectorAll('.wb_open').forEach((b) => {
    b.onclick = () => {
      state.wbSel = b.dataset.jid;
      switchView('writeback');
    };
  });
}

async function showExtractionDetail(main, jid) {
  const out = $('#extractOut') || main;
  out.innerHTML = '<div class="dim" style="margin-top:12px">加载中…</div>';
  try {
    const [detail, report] = await Promise.all([
      api('GET', `/api/extractions/${jid}?limit=80`),
      api('POST', `/api/extractions/${jid}/validate`),
    ]);
    const files = state.files || [];
    const st = report.stats || {};
    const rows = (detail.entries || []).map((e) => `<tr>
      <td class="mono" style="font-size:11px">${esc(e.key)}</td>
      <td>${esc(e.original)}</td>
      <td class="dim" style="font-size:11px">${esc(e.context || '')}</td>
    </tr>`).join('');

    out.innerHTML = `
      <div class="card" style="margin-top:14px">
        <div class="row" style="justify-content:space-between;margin-bottom:10px">
          <div class="row" style="gap:8px">
            <b>提取结果</b>
            <span class="mono dim">${esc(jid)}</span>
            <span class="tag ok">${st.total || 0} 条</span>
            ${st.error_count ? `<span class="tag w">${st.error_count} 错误</span>` : ''}
            ${st.warning_count ? `<span class="tag">${st.warning_count} 警告</span>` : ''}
          </div>
          <div class="row">
            <button class="btn sm" id="x_copy">复制 JSON</button>
            <button class="btn sm" id="x_import">导入平台</button>
            <button class="btn sm" id="x_upload"
                    title="把这份提取结果打包成 JSON 上传成平台上的一个新文件">上传为新文件</button>
            <button class="btn sm" id="x_writeback"
                    title="把平台上已达门槛的译文按这些 key 写回游戏文件">回写译文</button>
          </div>
        </div>

        ${(report.errors || []).length ? `
          <details open style="margin-bottom:8px">
            <summary style="color:var(--err);font-size:12px;cursor:pointer">
              错误 ${report.errors.length} 条</summary>
            <pre class="result">${esc(report.errors.slice(0, 30)
              .map((e) => `第${e.row}行 ${e.rule}: ${e.msg}${e.key ? ' [' + e.key + ']' : ''}`)
              .join('\n'))}</pre>
          </details>` : ''}
        ${(report.warnings || []).length ? `
          <details style="margin-bottom:8px">
            <summary style="color:var(--warn);font-size:12px;cursor:pointer">
              警告 ${report.warnings.length} 条</summary>
            <pre class="result">${esc(report.warnings.slice(0, 30)
              .map((e) => `第${e.row}行 ${e.rule}: ${e.msg}${e.key ? ' [' + e.key + ']' : ''}`)
              .join('\n'))}</pre>
          </details>` : ''}

        <div style="max-height:420px;overflow:auto">
          <table>
            <thead><tr><th style="width:38%">key</th><th>原文</th>
              <th style="width:20%">上下文</th></tr></thead>
            <tbody>${rows || '<tr><td colspan="3" class="empty">没有词条</td></tr>'}</tbody>
          </table>
        </div>
        <div class="dim" style="font-size:11.5px;margin-top:6px">
          显示前 ${(detail.entries || []).length} / ${detail.total} 条
        </div>
      </div>`;

    $('#x_copy').onclick = async () => {
      try {
        const r = await api('POST', `/api/extractions/${jid}/export`, {});
        await navigator.clipboard.writeText(JSON.stringify(r.items, null, 2));
        toast(`已复制 ${r.count} 条`);
      } catch (e) { toast(e.message, true); }
    };

    const wbBtn = $('#x_writeback');
    if (wbBtn) wbBtn.onclick = () => openWritebackDialog(jid);

    // 上传为新文件：与「导入平台」并列的另一条路。
    // 区别要在界面上讲明白 —— 导入是往已有文件里逐条补 key（慢，但格式无关），
    // 上传是新建文件让平台自己解析（快，但依赖项目格式是 JSON）。
    $('#x_upload').onclick = () => {
      const el = document.createElement('div');
      el.className = 'modal';
      el.innerHTML = `
        <div class="modal-box" style="width:min(620px,94vw)">
          <div class="modal-head"><b>${t('上传为新文件')}</b></div>
          <div class="modal-body">
            <div class="formrow"><label class="lbl">${t('文件名（须 .json 结尾）')}</label>
              <input class="field" id="up_name" value="extract-${esc(jid)}.json"></div>
            <div class="formrow"><label class="lbl">${t('目标目录（可选，如 locale/）')}</label>
              <input class="field" id="up_path" placeholder="${t('留空 = 项目根目录')}"></div>
            <label class="switch"><input type="checkbox" id="up_force">
              <span>${t('强制上传（跳过校验错误）')}</span></label>
            <div class="notice" style="font-size:11.5px;margin-top:8px">
              ${t('上传会真的改平台数据、且不可撤回。实测把 {key: original} 的 JSON 传进 twee 项目也能正确建出词条，但不同项目格式没逐一验证——务必先点「仅预览」看一眼内容。')}
            </div>
          </div>
          <div id="up_preview"></div>
          <div class="modal-foot">
            <span class="dim" id="up_msg" style="font-size:11.5px"></span>
            <div class="spacer"></div>
            <button class="btn" id="up_cancel">${t('取消')}</button>
            <button class="btn" id="up_preview_btn">${t('仅预览')}</button>
            <button class="btn primary" id="up_do">${t('上传')}</button>
          </div>
        </div>`;
      document.body.appendChild(el);
      el.onclick = (ev) => { if (ev.target === el) el.remove(); };
      $('#up_cancel', el).onclick = () => el.remove();
      const payload = () => ({
        filename: $('#up_name', el).value.trim() || 'extract.json',
        path: $('#up_path', el).value.trim(),
        force: $('#up_force', el).checked,
      });

      $('#up_preview_btn', el).onclick = async () => {
        const box = $('#up_preview', el);
        box.innerHTML = `<div class="dim" style="font-size:11.5px;margin-top:8px">${t('生成预览…')}</div>`;
        try {
          const r = await api('POST', `/api/extractions/${jid}/upload`,
            { ...payload(), dry_run: true });
          box.innerHTML = `
            <div style="margin-top:10px;border-top:1px solid var(--border);padding-top:8px">
              <div class="row" style="gap:8px;margin-bottom:6px">
                <b style="font-size:12.5px">${t('预览')}</b>
                <span class="tag ok">${r.count} ${t('条词条')}</span>
                <span class="tag">${r.bytes} ${t('字节')}</span>
                <span class="dim" style="font-size:11px">${t('不会写入平台')}</span>
              </div>
              <pre class="result" style="max-height:200px;overflow:auto">${esc((r.preview || []).join('\n'))}</pre>
              <div class="dim" style="font-size:11px;margin-top:4px">${esc(L(r, 'note'))}</div>
            </div>`;
        } catch (e) {
          box.innerHTML = `<div style="color:var(--err);font-size:11.5px;margin-top:8px">${esc(e.message)}</div>`;
        }
      };

      $('#up_do', el).onclick = async (ev) => {
        // 连点会提交两个 job（第二个把第一个的结果覆盖掉，看着像"上传成功但没生效"）
        const btn = ev.currentTarget;
        if (btn.disabled) return;
        btn.disabled = true;
        $('#up_msg', el).textContent = t('上传中…');
        try {
          const r = await api('POST', `/api/extractions/${jid}/upload`, payload());
          el.remove();
          toast(t('已提交上传任务') + ` ${r.job ? r.job.id : ''}`
            + t('，跑完自动加载结果…')
            + ((state.settings || {}).dry_run ? t('（演练模式：只算不写）') : ''));
        } catch (e) {
          btn.disabled = false;
          $('#up_msg', el).innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`;
        }
      };
    };

    $('#x_import').onclick = async () => {
      if (!files.length) {
        toast('先在「文件」页刷新，拿到平台文件列表', true);
        return;
      }
      const opts = files.map((f) =>
        `<option value="${f.id}">${esc(f.name)}</option>`).join('');
      const el = document.createElement('div');
      el.className = 'modal';
      el.innerHTML = `
        <div class="modal-box" style="width:min(520px,92vw)">
          <div class="modal-head"><b>导入到平台文件</b></div>
          <div class="modal-body">
            <div class="formrow"><label class="lbl">目标文件</label>
              <select class="field" id="imp_file">${opts}</select></div>
            <div class="formrow"><label class="lbl">文件名（传给平台）</label>
              <input class="field" id="imp_name" value="extract.json"></div>
            <label class="switch"><input type="checkbox" id="imp_force">
              <span>强制导入（忽略空译文/已人工翻译的保护）</span></label>
            <div class="dim" style="font-size:11.5px;margin-top:8px">
              平台只会创建缺失的 key、更新空译文；已有译文默认不覆盖。
            </div>
          </div>
          <div id="imp_preview"></div>
          <div class="modal-foot">
            <span class="dim" id="imp_msg" style="font-size:11.5px"></span>
            <div class="spacer"></div>
            <button class="btn" id="imp_cancel">取消</button>
            <button class="btn" id="imp_preview_btn">仅预览</button>
            <button class="btn primary" id="imp_do">导入</button>
          </div>
        </div>`;
      document.body.appendChild(el);
      el.onclick = (ev) => { if (ev.target === el) el.remove(); };
      $('#imp_cancel', el).onclick = () => el.remove();
      const payload = () => ({
        file_id: Number($('#imp_file', el).value),
        filename: $('#imp_name', el).value.trim() || 'extract.json',
        force: $('#imp_force', el).checked,
      });

      $('#imp_preview_btn', el).onclick = async () => {
        const box = $('#imp_preview', el);
        box.innerHTML = '<div class="dim" style="font-size:11.5px;margin-top:8px">生成预览…</div>';
        try {
          const r = await api('POST', `/api/extractions/${jid}/import`,
            { ...payload(), dry_run: true });
          const rows = (r.sample || []).map((it) => `<tr>
            <td class="mono" style="font-size:11px">${esc(it.key)}</td>
            <td>${esc(it.original)}</td>
            <td class="dim" style="font-size:11px">${esc(it.context || '')}</td>
          </tr>`).join('');
          const st = (r.report && r.report.stats) || {};
          box.innerHTML = `
            <div style="margin-top:10px;border-top:1px solid var(--border);padding-top:8px">
              <div class="row" style="gap:8px;margin-bottom:6px">
                <b style="font-size:12.5px">预览</b>
                <span class="tag ok">将发送 ${r.count} 条</span>
                <span class="tag">目标文件 #${r.file_id}</span>
                ${st.error_count ? `<span class="tag w">${st.error_count} 错误</span>` : ''}
                <span class="dim" style="font-size:11px">不会写入平台</span>
              </div>
              <div style="max-height:180px;overflow:auto">
                <table><thead><tr><th style="width:36%">key</th><th>原文</th>
                  <th style="width:22%">上下文</th></tr></thead>
                  <tbody>${rows || '<tr><td colspan="3" class="empty">没有词条</td></tr>'}</tbody>
                </table>
              </div>
              ${r.count > (r.sample || []).length
                ? `<div class="dim" style="font-size:11px;margin-top:4px">
                     仅显示前 ${(r.sample || []).length} 条</div>` : ''}
            </div>`;
        } catch (e) {
          box.innerHTML = `<div style="color:var(--err);font-size:11.5px;margin-top:8px">${esc(e.message)}</div>`;
        }
      };

      $('#imp_do', el).onclick = async (ev) => {
        const btn = ev.currentTarget;
        if (btn.disabled) return;   // 同上传：连点会提交两个 job
        btn.disabled = true;
        $('#imp_msg', el).textContent = '导入中…';
        try {
          const r = await api('POST', `/api/extractions/${jid}/import`, payload());
          el.remove();
          // 正式导入只提交 job，取材/校验/写入都在 job 里做一次；
          // 条数由任务结果给出（原先接口返回的 sent 是路由自己算的重复 prep）
          toast(`已提交导入任务 ${r.job ? r.job.id : ''}，跑完自动加载结果…`
            + ((state.settings || {}).dry_run ? '（演练模式：只算不写）' : ''));
        } catch (e) {
          btn.disabled = false;
          $('#imp_msg', el).innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`;
        }
      };
    };
  } catch (e) {
    out.innerHTML = `<div style="color:var(--err);margin-top:12px">${esc(e.message)}</div>`;
  }
}

/* ---- 回写：把平台上已达门槛的译文写回游戏文件 ---- */
// 反向链路。in_place 会直接覆盖游戏文件（后端会先备份到
// <state-dir>/writeback/<提取记录 id>/backup-<时间戳>/，同秒重复运行会各占一个目录），
// 危险等级和「执行提取器」同级，所以沿用 confirmRisk 那套做法：
// 醒目警示 + 显式勾选确认 + 5 秒倒计时，倒计时结束前提交按钮禁用。
const WB_STAGES = [
  { v: 1, label: '已翻译' },
  { v: 3, label: '已检查' },
  { v: 5, label: '已审核' },
];
// 回写形式：mirror = 目标目录是一份完整可跑的游戏（未改动文件原样复制），
// patch = 只放被改动的文件（原行为，留着先比对用）。
const WB_MODES = [
  { v: 'mirror', label: '整包镜像（完整可跑）' },
  { v: 'patch', label: '仅改动文件' },
];
const WB_DEFAULT_STAGE = 3;
const WB_DEFAULT_MODE = 'mirror';
const WB_COUNTDOWN_SEC = 5;

function wbStageOptions(cur) {
  return WB_STAGES.map(
    (s) => `<option value="${s.v}"${s.v === cur ? ' selected' : ''}>${esc(s.label)}</option>`
  ).join('');
}

function wbModeOptions(cur) {
  return WB_MODES.map((m) => `<option value="${esc(m.v)}"${
    m.v === cur ? ' selected' : ''}>${esc(m.label)}</option>`).join('');
}

function wbModeLabel(mode) {
  const hit = WB_MODES.find((m) => m.v === mode);
  return (hit || WB_MODES[0]).label;
}

/* 设置页的三个回写默认值（拿不到就用兜底） */
function wbDefaults() {
  const s = state.settings || {};
  const mode = WB_MODES.some((m) => m.v === s.writeback_mode)
    ? String(s.writeback_mode) : WB_DEFAULT_MODE;
  const stage = WB_STAGES.some((x) => x.v === Number(s.writeback_min_stage))
    ? Number(s.writeback_min_stage) : WB_DEFAULT_STAGE;
  return { out: String(s.writeback_out_dir || '').trim(), mode, stage };
}

function wbReportHtml(r) {
  // 后端落盘的是 {saved_at, report}；契约里写的是裸 report —— 两种都吃
  const wrap = r || {};
  const d = wrap.report || wrap;
  const savedAt = wrap.saved_at ? fmtTime(wrap.saved_at) : '';
  const num = (k) => Number(d[k] || 0);
  const colored = (k, color) =>
    `<td class="num"${num(k) ? ` style="color:${color}"` : ''}>${num(k)}</td>`;
  const errors = d.errors || [];
  const details = d.details || [];
  const mode = String(d.mode || WB_DEFAULT_MODE);
  // 「没写进去」的条数：这些不是 0 就不能说「无错误」，否则整包镜像时
  // 几百条静默没写，界面却看着全绿。
  const notWritten = num('mismatched') + num('missing') + num('unsupported')
    + num('skipped_no_original') + num('failed_files');
  const errTag = errors.length
    ? `<span class="tag w">${errors.length} 处错误</span>`
    : (notWritten
        ? `<span class="tag w">${notWritten} 条没写进去</span>`
        : '<span class="tag ok">无错误</span>');
  return `
    <div style="margin-top:10px;border-top:1px solid var(--border);padding-top:8px">
      <div class="row" style="gap:8px;margin-bottom:6px">
        <b style="font-size:12.5px">回写报告</b>
        <span class="tag ok">${esc(wbModeLabel(mode))}</span>
        ${d.dry_run
          ? '<span class="tag w">仅预览 · 未落盘</span>'
          : '<span class="tag ok">已落盘</span>'}
        ${errTag}
        ${savedAt ? `<span class="dim" style="font-size:11px">${esc(savedAt)}</span>` : ''}
      </div>
      <table>
        <thead><tr>
          <th class="num">写入</th><th class="num">跳过·空译文</th>
          <th class="num">跳过·门槛</th><th class="num">原文不一致</th>
          <th class="num">缺失</th><th class="num">已复制文件</th><th class="num">写入文件</th>
        </tr></thead>
        <tbody><tr>
          <td class="num">${num('written')}</td>
          <td class="num">${num('skipped_empty')}</td>
          <td class="num">${num('skipped_stage')}</td>
          ${colored('mismatched', 'var(--warn)')}
          ${colored('missing', 'var(--err)')}
          <td class="num">${num('copied')}</td>
          <td class="num">${num('files')}</td>
        </tr></tbody>
      </table>
      <div class="dim" style="font-size:11.5px;margin-top:6px">
        其余没写的：无原文 <b>${num('skipped_no_original')}</b>
        · 平台无此 key <b>${num('skipped_untracked')}</b>
        · 有疑问（校对打回） <b>${num('skipped_disputed')}</b>
        · 格式未支持 <b>${num('unsupported')}</b>
        · 失败文件 <b>${num('failed_files')}</b>
        <span style="opacity:.75">（条目数 / 文件数）</span>
      </div>
      <div class="dim" style="font-size:11.5px;margin-top:2px">
        输出目录 <span class="mono">${esc(d.out_dir || '—')}</span>
      </div>
      ${d.backup_dir ? `<div class="dim" style="font-size:11.5px;margin-top:2px">
        备份 <span class="mono">${esc(d.backup_dir)}</span></div>` : ''}
      ${mode === 'mirror' && !d.dry_run ? `
        <div class="dim" style="font-size:11.5px;margin-top:2px">
          整包镜像：输出目录里是一份完整游戏，可直接替换游戏目录
        </div>` : ''}
      ${details.length ? `
        <details style="margin-top:6px">
          <summary style="color:var(--warn);font-size:11.5px;cursor:pointer">
            未写入明细 ${details.length} 条</summary>
          <table>
            <thead><tr><th style="width:80px">类型</th><th style="width:40%">key</th>
              <th>原因</th></tr></thead>
            <tbody>${details.slice(0, 20).map((x) => `<tr>
              <td class="mono" style="font-size:11px">${esc(String((x || {}).kind || ''))}</td>
              <td class="mono" style="font-size:11px">${esc(String((x || {}).key || ''))}</td>
              <td style="font-size:11.5px">${esc(String((x || {}).reason || ''))}</td>
            </tr>`).join('')}</tbody>
          </table>
          ${details.length > 20
            ? `<div class="dim" style="font-size:11px;margin-top:4px">
                 只列前 20 条，其余见任务日志</div>`
            : ''}
        </details>` : ''}
      ${errors.length ? `
        <details style="margin-top:6px">
          <summary style="color:var(--err);font-size:11.5px;cursor:pointer">
            错误 ${errors.length} 条</summary>
          <pre class="result">${esc(errors.slice(0, 20).map((e) =>
            typeof e === 'string' ? e : JSON.stringify(e)).join('\n'))}</pre>
        </details>` : ''}
    </div>`;
}

/* 回写参数区的 HTML：弹窗与「回写」页共用。
   两处 id 相同（#wb_stage/#wb_mode/#wb_out…），但查找全部限定在各自容器里，不冲突。 */
function wbParamsHtml() {
  const def = wbDefaults();
  return `
    <div class="grid c2">
      <div class="formrow">
        <label class="lbl">译文门槛</label>
        <select class="field" id="wb_stage">${wbStageOptions(def.stage)}</select>
      </div>
      <div class="formrow">
        <label class="lbl">回写形式</label>
        <select class="field" id="wb_mode">${wbModeOptions(def.mode)}</select>
        <div class="dim" style="font-size:11px;margin-top:3px" id="wb_modehint"></div>
      </div>
    </div>
    <div class="formrow">
      <label class="lbl">输出目录（可空，这一次有效）</label>
      <input class="field" id="wb_out" value="${esc(def.out)}"
             placeholder="留空 = 游戏目录旁的 &lt;游戏目录&gt;_cn">
    </div>
    <label class="switch"><input type="checkbox" id="wb_dry" checked>
      <span>仅预览（不落盘，只出报告）</span></label>
    <label class="switch" style="margin-top:8px">
      <input type="checkbox" id="wb_inplace">
      <span>原地修改游戏文件（危险）</span></label>
    <div id="wb_risk" style="display:none">
      <div class="notice warn" style="margin-top:10px">
        <b>⚠ 会直接覆盖游戏文件</b><br>
        回写会按提取记录里的 key 改写<b>游戏目录中的原始文件</b>。
        覆盖前会自动备份到 <code>&lt;state-dir&gt;/writeback/&lt;提取记录 id&gt;/backup-&lt;时间戳&gt;/</code>，但备份仅用于回滚，
        本工具不对回写结果做任何担保，
        <b style="color:var(--warn)">因原地回写造成的一切后果由使用者自行承担</b>。
      </div>
      <label class="switch" style="margin-top:8px">
        <input type="checkbox" id="wb_ack">
        <span>我已阅读并了解上述风险，确认直接覆盖游戏文件</span></label>
    </div>
    <div class="dim" style="font-size:11.5px;margin-top:8px">
      只动提取记录里出现过的 key；空译文、门槛以下的译文、原文已变（mismatch）的条目都会被跳过。
    </div>
    <div id="wb_msg" style="font-size:11.5px;margin-top:8px;min-height:14px"></div>
    <div id="wb_progress" style="display:none;margin-top:6px">
      <div class="dim" style="font-size:11.5px;margin-bottom:4px">任务日志</div>
      <pre class="result" id="wb_log" style="max-height:170px;overflow:auto;margin:0"></pre>
    </div>
    <div id="wb_report"></div>`;
}

/* 把参数区接起来：倒计时 / 危险确认 / 提交 / 轮询 / 报告。
   opts: { jid, ep, last } —— ep 用来发现「用户切页了」，last 是上次报告（页面先显示）。 */
function wbWire(scope, opts) {
  const o = opts || {};
  const jid = o.jid;
  const ep = o.ep != null ? o.ep : renderEpoch;
  const def = wbDefaults();
  const inEl = (s) => $(s, scope);
  const stage = inEl('#wb_stage');
  const mode = inEl('#wb_mode');
  const modeHint = inEl('#wb_modehint');
  const outDir = inEl('#wb_out');
  const dryRun = inEl('#wb_dry');
  const inPlace = inEl('#wb_inplace');
  const riskBox = inEl('#wb_risk');
  const ack = inEl('#wb_ack');
  const msg = inEl('#wb_msg');
  const prog = inEl('#wb_progress');
  const logPre = inEl('#wb_log');
  const report = inEl('#wb_report');
  const hint = inEl('#wb_hint');
  const defNote = inEl('#wb_defnote');
  const go = inEl('#wb_go');

  let left = 0;          // 倒计时剩余秒数，>0 时禁止提交
  let cdTimer = null;
  let busy = false;
  let stopped = false;

  // 值 == 设置页默认值时，小字点明「来自设置页」，免得用户以为改这里是永久的
  function syncDefNote() {
    const same = [];
    if ((outDir.value || '').trim() === def.out) same.push('输出目录');
    if (mode.value === def.mode) same.push('回写形式');
    if (Number(stage.value) === def.stage) same.push('门槛');
    defNote.textContent = same.length
      ? `${same.join(' · ')} 来自设置页默认值（改这里只对本次生效）`
      : '';
  }

  function syncModeHint() {
    modeHint.textContent = mode.value === 'mirror'
      ? '未改动的文件原样复制，目标目录可直接替换游戏目录'
      : '只放被改动的文件，适合先比对再合并';
  }

  stage.onchange = syncDefNote;
  mode.onchange = () => { syncModeHint(); syncDefNote(); };
  outDir.oninput = syncDefNote;
  syncModeHint();
  syncDefNote();

  function syncGo() {
    if (busy) {
      go.disabled = true;
      go.textContent = '回写中…';
      return;
    }
    if (inPlace.checked) {
      go.disabled = left > 0 || !ack.checked;
      go.textContent = left > 0 ? `请阅读（${left}s）` : '开始回写（原地）';
    } else {
      go.disabled = false;
      go.textContent = '开始回写';
    }
  }

  function stopCountdown() {
    if (cdTimer) clearInterval(cdTimer);
    cdTimer = null;
    left = 0;
  }

  function startCountdown() {
    stopCountdown();
    left = WB_COUNTDOWN_SEC;
    syncGo();
    cdTimer = setInterval(() => {
      left -= 1;
      if (left <= 0) {
        stopCountdown();
        syncGo();
        return;
      }
      syncGo();
    }, 1000);
  }

  inPlace.onchange = () => {
    const on = !!inPlace.checked;
    riskBox.style.display = on ? '' : 'none';
    ack.checked = false;                 // 每次勾上都要重新确认
    if (on) startCountdown(); else stopCountdown();
    syncGo();
  };
  ack.onchange = () => syncGo();
  syncGo();

  function paintLog(j) {
    const lines = (j.logs || []).slice(-80).map((l) => `[${fmtTime(l.ts)}] ${l.msg}`);
    logPre.textContent = lines.length ? lines.join('\n') : '（等待日志…）';
  }

  async function loadReport() {
    try {
      const r = await api('GET',
        `/api/extractions/${encodeURIComponent(jid)}/writeback/last`);
      report.innerHTML = wbReportHtml(r);
    } catch (e) {
      report.innerHTML = `<div class="dim" style="font-size:11.5px;margin-top:8px">
        读不到回写报告：${esc(e.message)}</div>`;
    }
  }

  if (o.last) report.innerHTML = wbReportHtml(o.last);   // 页面进来先给上次结果

  go.onclick = async () => {
    if (busy || stopped) return;
    const danger = !!inPlace.checked;
    if (danger && (left > 0 || !ack.checked)) return;   // 双保险，别绕过倒计时
    busy = true;
    syncGo();
    msg.innerHTML = '<span class="dim">已提交，等待任务…</span>';
    report.innerHTML = '';
    logPre.textContent = '';
    prog.style.display = '';
    try {
      const r = await api('POST',
        `/api/extractions/${encodeURIComponent(jid)}/writeback`, {
          min_stage: Number(stage.value) || WB_DEFAULT_STAGE,
          out_dir: (outDir.value || '').trim(),
          mode: mode.value || WB_DEFAULT_MODE,
          in_place: danger,
          accept_risk: danger,          // 危险操作必须显式确认，否则后端 400
          dry_run: !!dryRun.checked,
        });
      // 契约是 {job_id}；顺手兼容 {job:{id}}，免得后端两种风格都要改
      const jobId = r.job_id || (r.job && r.job.id);
      if (!jobId) throw new Error('后端没有返回 job_id');
      msg.innerHTML = `<span class="dim">任务 ${esc(jobId)} 运行中…</span>`;

      for (let i = 0; i < 900; i++) {
        if (stopped || ep !== renderEpoch) {
          hint.textContent = '轮询已停止（离开页面 / 关闭弹窗）';
          break;
        }
        await new Promise((res) => setTimeout(res, 1000));
        if (stopped || ep !== renderEpoch) {
          hint.textContent = '轮询已停止（离开页面 / 关闭弹窗）';
          break;
        }
        const j = (await api('GET', `/api/jobs/${encodeURIComponent(jobId)}`)).job;
        paintLog(j);
        if (j.status === 'done') {
          hint.textContent = `完成${j.duration != null ? ` · ${j.duration}s` : ''}`;
          await loadReport();
          break;
        }
        if (j.status === 'failed' || j.status === 'cancelled') {
          msg.innerHTML = `<span style="color:var(--err)">任务${j.status === 'failed'
            ? '失败' : '已取消'}：${esc(j.error || '（无错误信息）')}</span>`;
          await loadReport();
          break;
        }
        const lastLine = (j.logs || []).slice(-1)[0];
        hint.textContent = `运行中 · ${lastLine ? lastLine.msg : '…'}`;
      }
    } catch (e) {
      // 后端 400 的 detail（app.py 走 {"error": ...}，契约写 {"detail": ...}，都显示）
      msg.innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`;
    } finally {
      busy = false;
      if (!stopped) syncGo();
    }
  };

  return {
    stop() {
      stopped = true;
      stopCountdown();
    },
  };
}

function openWritebackDialog(jid) {
  const el = document.createElement('div');
  el.className = 'modal';
  el.id = 'wb_dlg';
  el.innerHTML = `
    <div class="modal-box" style="width:min(660px,94vw)">
      <div class="modal-head">
        <b>回写译文到本地</b>
        <span class="mono dim" style="margin-left:8px;font-size:11px">${esc(jid)}</span>
      </div>
      <div class="modal-body">${wbParamsHtml()}</div>
      <div class="modal-foot">
        <span class="dim" id="wb_defnote" style="font-size:11px"></span>
        <span class="dim" id="wb_hint" style="font-size:11.5px"></span>
        <div class="spacer"></div>
        <button class="btn" id="wb_close">关闭</button>
        <button class="btn primary" id="wb_go">开始回写</button>
      </div>
    </div>`;
  document.body.appendChild(el);

  const ctrl = wbWire(el, { jid, ep: renderEpoch });
  const close = () => { ctrl.stop(); el.remove(); };
  el.onclick = (ev) => { if (ev.target === el) close(); };
  $('#wb_close', el).onclick = close;
}

/* ---- 「回写」页：一级入口，最终产成品在这里出 ---- */
function wbShortDir(p) {
  const parts = String(p || '').split(/[\\/]+/).filter(Boolean);
  return parts.length > 2 ? '…/' + parts.slice(-2).join('/') : String(p || '');
}

// 提取记录 id 形如 20261023-160329-ab12cd，前两段就是时间戳
function wbJobStamp(jobId) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/.exec(String(jobId || ''));
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : String(jobId || '').slice(0, 15);
}

function wbJobsTable(jobs, sel) {
  const cur = sel ? sel.job_id : '';
  return `<div class="card tight" id="wbTable"><table>
    <thead><tr><th>记录</th><th>提取器</th><th>游戏目录</th>
      <th class="num">词条</th><th>时间</th><th>状态</th><th></th></tr></thead>
    <tbody>${jobs.map((j) => `<tr>
        <td class="mono" style="font-size:11px">${esc(String(j.job_id).slice(0, 15))}</td>
        <td>${esc((j.extractor || {}).name || j.extractor_id || '')}</td>
        <td class="mono dim" style="font-size:11px" title="${esc(j.game_dir)}">${
          esc(wbShortDir(j.game_dir))}</td>
        <td class="num">${j.entries || 0}</td>
        <td class="mono" style="font-size:11.5px">${esc(wbJobStamp(j.job_id))}</td>
        <td>${j.status === 'done'
          ? '<span class="status done">完成</span>'
          : `<span class="status failed" title="${esc(j.error || '')}">失败</span>`}</td>
        <td style="text-align:right"><button class="btn sm${j.job_id === cur ? ' primary' : ''}"
          data-jid="${esc(j.job_id)}"
          title="${j.job_id === cur ? '当前正显示这条记录' : '选中并回写这条记录'}">回写</button></td>
      </tr>`).join('')}</tbody>
  </table></div>`;
}

function wbPanel(sel) {
  return `<div class="card" id="wb_panel" style="margin-top:14px">
    <div class="row" style="justify-content:space-between;margin-bottom:10px">
      <div class="row" style="gap:8px">
        <b>回写参数</b>
        <span class="mono dim" style="font-size:11px">${esc(sel.job_id)}</span>
        <span class="tag" title="${esc(sel.game_dir)}">${esc(wbShortDir(sel.game_dir))}</span>
        <span class="tag">${esc(String(sel.entries || 0))} 条词条</span>
      </div>
      <span class="dim" style="font-size:11.5px">改这里只影响这一次</span>
    </div>
    ${wbParamsHtml()}
    <div class="row" style="margin-top:12px">
      <button class="btn primary" id="wb_go">开始回写</button>
      <span class="dim" id="wb_defnote" style="font-size:11px"></span>
      <div class="spacer"></div>
      <span class="dim" id="wb_hint" style="font-size:11.5px"></span>
    </div>
  </div>`;
}

async function renderWriteback(main) {
  const ep = renderEpoch;
  if (state._wbStop) { state._wbStop(); state._wbStop = null; }   // 换记录前停掉旧轮询
  let rows;
  try {
    rows = (await api('GET', '/api/extractions?limit=50')).rows || [];
  } catch (e) {
    if (ep !== renderEpoch) return;
    main.innerHTML = pageHead('回写', '读取提取记录失败') +
      `<div class="card"><span style="color:var(--err)">${esc(e.message)}</span></div>`;
    return;
  }
  if (ep !== renderEpoch) return;

  // 只有「跑成功 + 有游戏目录」的记录能回写（后端也是这么卡）
  const jobs = rows.filter((x) => x.status === 'done' && x.game_dir);
  if (!state.wbSel || !jobs.some((x) => x.job_id === state.wbSel)) {
    state.wbSel = jobs.length ? jobs[0].job_id : '';
  }
  const sel = jobs.find((x) => x.job_id === state.wbSel) || null;

  let last = null;
  if (sel) {
    try {
      last = await api('GET',
        `/api/extractions/${encodeURIComponent(sel.job_id)}/writeback/last`);
    } catch { last = null; }        // 没报告 = 还没回写过，正常
    if (ep !== renderEpoch) return;
  }
  const def = wbDefaults();

  main.innerHTML = `
    ${pageHead('回写', '把平台上确认过的译文按提取记录的 key 写回游戏文件，产出汉化成品',
      '<button class="btn" id="wbRefresh">刷新</button>')}
    <div class="metrics">
      ${metric('可回写记录', jobs.length, { ico: '↺' })}
      ${metric('上次回写', last && last.saved_at ? fmtTime(last.saved_at) : '—',
        { ico: '◷' })}
      ${metric('默认输出目录',
        `<span class="mono" style="font-size:12.5px;font-weight:400">${
          esc(def.out || '<游戏目录>_cn')}</span>`, { ico: '▤' })}
    </div>
    ${jobs.length ? wbJobsTable(jobs, sel) : `
      <div class="card"><div class="empty">
        ${t('还没有可回写的提取记录：先去「提取」页扫一遍游戏目录、把词条导入平台，等平台上的译文过了门槛，再回来这里回写成品。')}
        <div style="margin-top:10px">
          <button class="btn" id="wbGoExtract">去提取页</button>
        </div>
      </div></div>`}
    ${sel ? wbPanel(sel) : ''}`;

  $('#wbRefresh').onclick = () => renderWriteback(main);
  if (!jobs.length) {
    $('#wbGoExtract').onclick = () => switchView('extract');
    return;
  }

  // 记录表用事件委托：点哪一行就切到哪条记录（省得每次重绘都要重新绑一遍）
  $('#wbTable').onclick = (ev) => {
    const t = ev && ev.target;
    const jid = t && t.dataset ? t.dataset.jid : '';
    if (!jid || jid === state.wbSel) return undefined;
    state.wbSel = jid;
    return renderWriteback(main);
  };

  if (sel) {
    const ctrl = wbWire($('#wb_panel'), { jid: sel.job_id, ep, last });
    state._wbStop = ctrl.stop;
  }
}

/* ================================================================== 提示词 */
async function renderPrompts(main) {
  const ep = renderEpoch;
  let d;
  try {
    d = await api('GET', '/api/prompts');
  } catch (e) {
    if (ep !== renderEpoch) return;
    main.innerHTML = pageHead('提示词', '读取失败') +
      `<div class="card"><span style="color:var(--err)">${esc(e.message)}</span></div>`;
    return;
  }
  if (ep !== renderEpoch) return;   // 切页了就别再往 #main 画
  const tab = state.promptTab || 'translate';
  const p = d.prompts || {};
  const defs = d.defaults || {};
  const customCount = (d.tasks || []).filter((t) => (p[t.id] || '').trim()).length;
  const prefixOn = (p.prefix || '').trim().length > 0;

  main.innerHTML = `
    ${pageHead(
      '提示词',
      `${(d.tasks || []).length} 个环节 · ${customCount} 个已自定义 · 总前置${prefixOn ? '已启用' : '未设置'}`,
      '<button class="btn primary" id="savePrompt">保存</button>'
    )}

    <div class="notice ${prefixOn ? 'safe' : ''}">
      <b>${t('总前置提示词')}</b>
      <div style="margin-top:4px">${t('会注入到所有环节的 system prompt 最前面。')}</div>
      <div>${t('适合放全局约定，例如「本项目是 RPG Maker MV 游戏，控制符必须原样保留」。')}</div>
    </div>
    <div class="card" style="margin-bottom:14px">
      <textarea class="field" id="p_prefix" rows="4"
        placeholder="留空则不注入。例如：本项目为 RPG Maker MV 游戏，\\N[1] 这类控制符必须原样保留。">${esc(p.prefix || '')}</textarea>
      <div class="row" style="margin-top:6px">
        <span class="dim" id="prefixMsg" style="font-size:11.5px"></span>
        <div class="spacer"></div>
        <button class="btn sm" id="resetPrefix">清空</button>
      </div>
    </div>

    <div class="subtabs">
      ${(d.tasks || []).map((t) => `
        <button data-ptab="${esc(t.id)}" class="${tab === t.id ? 'on' : ''}">
          ${esc(L(t, 'label'))}${(p[t.id] || '').trim() ? ' ●' : ''}
        </button>`).join('')}
    </div>

    <div class="card">
      <div class="row" style="justify-content:space-between;margin-bottom:8px">
        <div class="row" style="gap:6px">
          <b>${esc((d.tasks.find((t) => t.id === tab) || {}).label || tab)}</b>
          <span class="mono dim">${esc(tab)}</span>
          ${(p[tab] || '').trim()
            ? '<span class="tag ok">已自定义</span>'
            : '<span class="tag">使用内置默认</span>'}
        </div>
        <div class="row">
          <button class="btn sm" id="fillDefault">填入默认值</button>
          <button class="btn sm" id="resetTask">恢复默认</button>
        </div>
      </div>
      <textarea class="field" id="p_task" rows="14"
        placeholder="${esc((defs[tab] || '').slice(0, 200))}…">${esc(p[tab] || '')}</textarea>
      <div class="dim" style="font-size:11.5px;margin-top:6px">
        留空 = 使用内置默认（下方「填入默认值」可把当前默认内容复制进来再改）。
      </div>
    </div>

    <div class="row" style="margin-top:12px">
      <span class="dim" id="promptMsg" style="font-size:11.5px"></span>
      <div class="spacer"></div>
      <button class="btn" id="resetAll">全部恢复默认</button>
      <button class="btn primary" id="savePrompt2">保存</button>
    </div>`;

  main.querySelectorAll('[data-ptab]').forEach((b) => {
    b.onclick = () => { state.promptTab = b.dataset.ptab; renderPrompts(main); };
  });

  const save = async (extra) => {
    const body = {
      prefix: $('#p_prefix').value,
      [tab]: $('#p_task').value,
      ...(extra || {}),
    };
    try {
      await api('PUT', '/api/prompts', body);
      toast('已保存');
      renderPrompts(main);
    } catch (e) {
      $('#promptMsg').innerHTML = `<span style="color:var(--err)">${esc(e.message)}</span>`;
    }
  };

  $('#savePrompt').onclick = () => save();
  $('#savePrompt2').onclick = () => save();
  $('#resetPrefix').onclick = async () => {
    $('#p_prefix').value = '';
    await save({ prefix: '' });
  };
  $('#fillDefault').onclick = () => {
    $('#p_task').value = defs[tab] || '';
    toast('已填入默认值，改完记得保存');
  };
  $('#resetTask').onclick = async () => {
    try {
      await api('POST', '/api/prompts/reset', { task: tab });
      toast('已恢复默认');
      renderPrompts(main);
    } catch (e) { toast(e.message, true); }
  };
  $('#resetAll').onclick = async () => {
    if (!(await askConfirm(isEn()
      ? `Reset all ${(d.tasks || []).length} stages and the global prefix to defaults?`
      : `把 ${(d.tasks || []).length} 个环节与总前置全部恢复默认？`))) return;
    try {
      await api('POST', '/api/prompts/reset', {});
      toast('已全部恢复默认');
      renderPrompts(main);
    } catch (e) { toast(e.message, true); }
  };
}

/* ============================================================== 扩展 */
async function renderExtensions(main) {
  const ep = renderEpoch;
  main.innerHTML = '<div class="empty">加载中…</div>';
  let d;
  try {
    d = await api('GET', '/api/extensions');
  } catch (e) {
    if (ep !== renderEpoch) return;
    main.innerHTML = pageHead('插件与技能', '读取失败') +
      `<div class="card"><span style="color:var(--err)">${esc(e.message)}</span></div>`;
    return;
  }
  if (ep !== renderEpoch) return;   // 切页了就别再往 #main 画
  const tab = state.extTab || 'plugin';
  const q = (state.extQuery || '').toLowerCase();
  const MAP = {
    plugin: { list: d.extensions.plugins, label: '插件', hint: 'metadata.yaml 描述元信息与挂载页面' },
    skill: { list: d.extensions.skills, label: 'SKILL', hint: 'SKILL.md 带 frontmatter（name/description），供 Agent 提示词装载' },
    mcp: { list: d.extensions.mcp, label: 'MCP', hint: '标准 MCP 配置：command / args / env / enabled' },
  };
  const cur = MAP[tab] || MAP.plugin;
  const list = cur.list.filter(
    (x) => !q || (x.name || '').toLowerCase().includes(q) || x.id.toLowerCase().includes(q));

  main.innerHTML = `
    ${pageHead(
      '插件与技能',
      `插件 ${d.extensions.plugins.length} · SKILL ${d.extensions.skills.length} · MCP ${d.extensions.mcp.length}`,
      '<button class="btn primary" id="extImport">+ 导入</button>'
    )}
    <div class="subtabs">
      ${Object.entries(MAP).map(([k, v]) => `
        <button data-etab="${k}" class="${tab === k ? 'on' : ''}">${v.label} (${v.list.length})</button>
      `).join('')}
    </div>

    <div class="row" style="margin-bottom:10px">
      <span class="dim" style="font-size:12px">${esc(cur.hint)}</span>
      <div class="spacer"></div>
      <input class="field" id="extSearch" placeholder="搜索${cur.label}…" style="width:200px"
             value="${esc(state.extQuery || '')}">
    </div>

    <div id="extImportBox"></div>

    <div class="extgrid">
      ${list.length ? list.map((x) => extCard(x, tab)).join('')
        : `<div class="empty">还没有${cur.label}${q ? '匹配搜索' : ''}</div>`}
    </div>

    ${tab === 'mcp' ? mcpAddForm() : ''}
  `;

  main.querySelectorAll('[data-etab]').forEach((b) => {
    b.onclick = () => { state.extTab = b.dataset.etab; state.extQuery = ''; renderExtensions(main); };
  });
  const search = $('#extSearch');
  search.oninput = () => {
    state.extQuery = search.value;
    clearTimeout(state._extTimer);
    state._extTimer = setTimeout(() => renderExtensions(main), 300);
  };

  $('#extImport').onclick = () => {
    const box = $('#extImportBox');
    box.innerHTML = `
      <div class="card" style="margin-bottom:12px">
        <div class="row">
          <select class="field" id="x_kind" style="width:130px">
            <option value="plugin" ${tab === 'plugin' ? 'selected' : ''}>插件</option>
            <option value="skill" ${tab === 'skill' ? 'selected' : ''}>SKILL</option>
          </select>
          <input class="field" id="x_path" style="flex:1"
                 placeholder="本地目录绝对路径（插件需 metadata.yaml 或 main.py；SKILL 需 SKILL.md）">
          <button class="btn primary" id="x_do">导入</button>
          <button class="btn" id="x_cancel">取消</button>
        </div>
        <div class="dim" style="font-size:11.5px;margin-top:6px">
          目录约定：<code>.ptz-state/plugins/&lt;id&gt;/metadata.yaml</code> ·
          <code>.ptz-state/skills/&lt;id&gt;/SKILL.md</code> · <code>.ptz-state/mcp.json</code>
        </div>
      </div>`;
    $('#x_cancel').onclick = () => { box.innerHTML = ''; };
    $('#x_do').onclick = async () => {
      const kind = $('#x_kind').value;
      const path = $('#x_path').value.trim();
      if (!path) return toast('填目录路径', true);
      try {
        const r = await api('POST', '/api/extensions/install', { kind, path });
        toast(`已导入 ${r.extension.name || r.extension.id}`);
        renderExtensions(main);
      } catch (e) { toast(e.message, true); }
    };
  };

  main.querySelectorAll('[data-toggle]').forEach((b) => {
    b.onclick = async () => {
      const [kind, id, enabled] = b.dataset.toggle.split('|');
      try {
        await api('POST', `/api/extensions/${kind}/${encodeURIComponent(id)}/toggle`,
          { enabled: enabled === '1' });
        renderExtensions(main);
      } catch (e) { toast(e.message, true); }
    };
  });
  main.querySelectorAll('[data-del]').forEach((b) => {
    b.onclick = async () => {
      const [kind, id] = b.dataset.del.split('|');
      if (!(await askConfirm(isEn() ? `Remove ${kind} "${id}"?` : `确定移除 ${kind}「${id}」？`))) return;
      try {
        await api('DELETE', `/api/extensions/${kind}/${encodeURIComponent(id)}`);
        toast('已移除');
        renderExtensions(main);
      } catch (e) { toast(e.message, true); }
    };
  });

  if (tab === 'mcp') {
    $('#mcpAdd').onclick = async () => {
      const name = $('#mcp_name').value.trim();
      const cmd = $('#mcp_cmd').value.trim();
      if (!name || !cmd) return toast('名称和命令必填', true);
      const args = $('#mcp_args').value.trim().split(/\s+/).filter(Boolean);
      try {
        await api('PUT', `/api/extensions/mcp/${encodeURIComponent(name)}`,
          { command: cmd, args, enabled: true });
        toast('已添加');
        renderExtensions(main);
      } catch (e) { toast(e.message, true); }
    };
  }
}

function extCard(x, kind) {
  const initial = (x.name || x.id).slice(0, 1).toUpperCase();
  const kindLabel = { plugin: '插件', skill: 'SKILL', mcp: 'MCP' }[kind] || kind;
  return `
  <div class="extcard2">
    <div class="ec-top">
      <div class="ec-icon">${esc(initial)}</div>
      <div class="ec-head">
        <div class="ec-title">
          <b>${esc(L(x, 'name') || x.id)}</b>
          ${x.version ? `<span class="dim">${esc(x.version)}</span>` : ''}
          ${x.enabled
            ? '<span class="tag ok">已启用</span>'
            : '<span class="tag w">已停用</span>'}
        </div>
        <div class="ec-meta">
          <span class="tag">${esc(kindLabel)}</span>
          ${x.author ? `<span class="dim" style="font-size:11px">${esc(x.author)}</span>` : ''}
          ${x.runtime === 'pending' ? '<span class="tag w">运行引擎待接入</span>' : ''}
        </div>
      </div>
      <label class="switch" title="启用/停用">
        <input type="checkbox" data-toggle="${esc(kind)}|${esc(x.id)}|${x.enabled ? 0 : 1}"
               ${x.enabled ? 'checked' : ''}>
      </label>
    </div>
    <div class="ec-desc">${esc(L(x, 'desc') || t('（无描述）'))}</div>
    <div class="ec-foot">
      <span class="mono dim" style="font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
        ${esc(x.path || x.id)}
      </span>
      <div class="spacer"></div>
      <button class="btn sm danger" data-del="${esc(kind)}|${esc(x.id)}">移除</button>
    </div>
  </div>`;
}

function mcpAddForm() {
  return `
  <div class="card" style="margin-top:12px">
    <h3 style="margin-top:0">新增 MCP 服务器</h3>
    <div class="row">
      <input class="field" id="mcp_name" placeholder="名称" style="width:150px">
      <input class="field" id="mcp_cmd" placeholder="命令，如 npx" style="width:150px">
      <input class="field" id="mcp_args" style="flex:1"
             placeholder="参数，空格分隔，如 -y @modelcontextprotocol/server-filesystem C:\\tmp">
      <button class="btn" id="mcpAdd">添加</button>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------ 启动 */
async function boot() {
  // 主题：首帧已经由 index.html 定好，这里只把 dataset 对齐（含 system 解析）
  applyTheme(themePref(), false);

  // 日志抽屉
  $('#logHead').onclick = () => {
    const p = $('#logPanel');
    p.classList.toggle('collapsed');
    $('#logToggle').textContent = p.classList.contains('collapsed') ? t('展开') : t('收起');
  };

  // 演练模式
  $('#dryRun').onchange = async (e) => {
    const next = e.target.checked;
    try {
      await api('PUT', '/api/settings', { dry_run: next });
      if (state.settings) state.settings.dry_run = next;
      toast(next ? '演练模式：只预览不写平台' : '已关闭演练模式：将真实写入平台');
    } catch (err) {
      e.target.checked = !next;   // 失败要回滚，否则开关与后端不一致
      toast(err.message, true);
    }
  };

  // 导航
  $('#nav').onclick = (e) => {
    const b = e.target.closest('button[data-view]');
    if (b) switchView(b.dataset.view);
  };

  let configured = false;
  let mockMode = false;
  try {
    const [sres, tres, health] = await Promise.all([
      api('GET', '/api/settings'),
      api('GET', '/api/tools'),
      api('GET', '/api/health'),
    ]);
    state.settings = sres.settings;
    state.tools = tres.tools;
    $('#dryRun').checked = !!state.settings.dry_run;
    configured = !!health.configured;
    mockMode = !!health.mock;
    state.health = health;
    state.llmReady = !!health.llm;
    // 侧栏页脚与「关于」都读后端版本，避免和 pyproject 脱节
    const foot = $('#verFoot');
    if (foot) foot.textContent = health.version ? 'v' + health.version : '—';
    if (mockMode) {
      document.querySelector('.brand').title = '演示模式：内置假平台，不碰真实项目';
      document.querySelector('.brand').insertAdjacentHTML(
        'beforeend', ' <span class="tag" style="font-size:10px">DEMO</span>');
    }
  } catch (e) {
    toast('后端不可达：' + e.message, true);
  }

  // 首次启动的免费声明：只在这台机器第一次打开时弹一次
  try {
    const notice = await api('GET', '/api/notice');
    if (notice && notice.pending) freeNoticeDialog(notice);
  } catch { /* 拉不到就不弹，不挡用户 */ }

  // 静态区（侧栏 / 顶栏 / 日志面板）的英文替换
  applyChromeLang();
  // 异步渲染出来的节点靠观察器兜底
  if (typeof startI18nWatcher === 'function') startI18nWatcher();

  connectSSE();
  pushLog({ msg: '控制台已连接，等待操作…', level: 'info' });

  if (configured) {
    await refreshAll();
  } else {
    setConn(false, '未配置');
    render();
  }

  // 定时刷新：任务每 8 秒一次；项目统计另按 15 秒节流（一次要打 4 个平台接口）。
  // 以前只刷任务，于是「已翻译 / 待翻译」从打开页面那一刻起就不再变了。
  let lastProjPull = Date.now();
  setInterval(() => {
    if (state.view === 'overview' || state.view === 'jobs') refreshJobs();
    if (state.view === 'overview' && Date.now() - lastProjPull > 15000 && canAutoRefresh()) {
      lastProjPull = Date.now();
      loadProject(false);
    }
  }, 8000);
}

document.addEventListener('DOMContentLoaded', boot);
