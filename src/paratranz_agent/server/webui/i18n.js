/* Paratranz Agent —— 国际化（i18n）
 *
 * 设计取舍：源码里**保留中文原文**，英文通过「中文 → 英文」字典在 DOM 边界统一替换。
 * 理由：WebUI 是零构建的原生 JS，渲染代码有三千多行、上百个模板字符串；
 * 若改成 t('key') 全量改写，改动面覆盖每个渲染函数，回归风险远高于收益。
 * DOM 级替换只依赖「文本节点 + 少量属性」，字典漏译也只是回落成中文，不会白屏。
 *
 * 覆盖范围：界面外壳（导航、页面标题、按钮、表头、标签、空状态、提示语）。
 * 不覆盖：服务端返回的数据（项目名、词条原文、日志正文）与用户输入 —— 这些本来就该保持原样。
 */
'use strict';

window.I18N = window.I18N || { zh: {}, en: {} };

const LANG_KEY = 'ptz-lang';
const LANGS = ['zh', 'en'];

/* 语言选择：显式选择 > 浏览器语言 > 中文 */
function currentLang() {
  let saved = null;
  try { saved = localStorage.getItem(LANG_KEY); } catch { saved = null; }
  if (LANGS.indexOf(saved) >= 0) return saved;
  const nav = String((navigator && navigator.language) || 'zh').toLowerCase();
  return nav.indexOf('zh') === 0 ? 'zh' : 'en';
}

function isEn() {
  return currentLang() === 'en';
}

/* 新代码用 t('中文')：英文环境查字典，查不到回落中文 */
function t(zh) {
  if (!isEn()) return zh;
  const dict = window.I18N.en || {};
  return Object.prototype.hasOwnProperty.call(dict, zh) ? dict[zh] : zh;
}

/* 动态文案规则：模板里带数字/变量，无法整串命中字典，用正则兜底。
 * 每条是 [正则, 替换串 | 函数]，只在英文环境下生效。
 * 函数式替换用于「同一个模板里按值变词」的情况（如 已开启/未开启）。 */
const LANG_RULES = [
  // 概览页副标题：`en → zh-CN · 任务功能已开启 · 校对等级 1 · 平台任务 0 个进行中`
  [/^(.+?)\s*·\s*任务功能(已开启|未开启)\s*·\s*校对等级\s*(\d+)\s*·\s*平台任务\s*(\d+)\s*个进行中$/,
    (m) => `${m[1]} · Tasks ${m[2] === '已开启' ? 'enabled' : 'disabled'}`
      + ` · Review level ${m[3]} · ${m[4]} platform tasks in progress`],
  // 概览页「还不能执行任务」提示被 <b>/<a> 拆成三段文本节点
  [/^：尚未配置模型 API。翻译、校对、术语、规范都要调用模型，\s*请先到$/,
    ': no model API is configured. Translation, review, glossary and style guide all call a model. Go to'],
  [/^页填写 API 地址、模型名与 API Key。$/,
    'and fill in the API base URL, model name and API key.'],
  [/^(\d+) 个文件$/, '$1 files'],
  [/^(\d+) 个目录$/, '$1 directories'],
  [/^(\d+) 个进行中$/, '$1 in progress'],
  [/^(\d+) 名成员$/, '$1 members'],
  [/^(\d+) 个模型$/, '$1 models'],
  [/^(\d+) 个源$/, '$1 providers'],
  [/^(\d+) 个已配置$/, '$1 configured'],
  [/^(\d+) 个环节$/, '$1 stages'],
  [/^(\d+) 个提取器$/, '$1 extractors'],
  [/^(\d+) 个已启用$/, '$1 enabled'],
  [/^(\d+) 条$/, '$1 strings'],
  [/^(\d+) 条词条$/, '$1 strings'],
  [/^(\d+) 个$/, '$1'],
  [/^共 (\d+) 条$/, '$1 strings in total'],
  [/^共 (\d+) 项$/, '$1 items in total'],
  [/^已提交 (\d+) 条$/, 'Submitted $1 strings'],
  [/^已复制 (\d+) 条$/, 'Copied $1 strings'],
  [/^已提交 (\d+) 个任务$/, 'Submitted $1 jobs'],
  [/^已添加 (\d+) 个模型$/, 'Added $1 models'],
  [/^已导入 (\d+) 条$/, 'Imported $1 strings'],
  [/^将发送 (\d+) 条$/, '$1 strings will be sent'],
  [/^显示前 (\d+) \/ (\d+) 条$/, 'Showing $1 of $2'],
  [/^(\d+) 处错误$/, '$1 errors'],
  [/^(\d+) 条没写进去$/, '$1 not written'],
  [/^写入 (\d+) 条 \/ (\d+) 个文件$/, '$1 strings written to $2 files'],
  [/^缓冲 (\d+) 条$/, 'Buffer: $1 entries'],
  [/^提取完成：(\d+) 条$/, 'Extraction finished: $1 strings'],
  [/^(\d+) 个动作需要你手动确认风险后再执行$/, '$1 actions need your confirmation before running'],
  [/^(.+) 条词条$/, '$1 strings'],
  [/^(.+) 个文件$/, '$1 files'],
  // —— 页头副标题里的分段（配合下面的「按 · 拆段」逻辑） ——
  [/^共 (\d+) 个$/, '$1 in total'],
  [/^运行中 (\d+) 个$/, '$1 running'],
  [/^共 (\d+) 文件$/, '$1 files'],
  [/^(\d+) 词条$/, '$1 strings'],
  [/^（(\d+) 个源已配 Key）$/, '($1 providers with a key)'],
  [/^(\d+) 个已自定义$/, '$1 customised'],
  [/^插件 (\d+)$/, 'Plugins $1'],
  [/^累计 (.+) tokens$/, '$1 tokens total'],
  [/^当前模型 ([^·]+)$/, 'Current model $1'],
  [/^今日 (\$[\d.]+)$/, 'Today $1'],
  [/^磁盘 (\d+) 个文件 (.+)$/, '$1 files on disk, $2'],
  [/^(\d+) 个文件 · (.+)$/, '$1 files · $2'],
  [/^(\d+) 个模型（(\d+) 个源已配 Key）$/, '$1 models ($2 providers with a key)'],
];

/* 不参与翻译的容器：代码、脚本、输入框、以及显式标记的区域 */
const SKIP_TAGS = { SCRIPT: 1, STYLE: 1, CODE: 1, PRE: 1, TEXTAREA: 1, KBD: 1, SAMP: 1 };

function trText(raw) {
  const dict = window.I18N.en || {};
  const key = raw.trim();
  if (!key) return raw;
  if (Object.prototype.hasOwnProperty.call(dict, key)) {
    return raw.replace(key, dict[key]);
  }
  for (let i = 0; i < LANG_RULES.length; i++) {
    const m = key.match(LANG_RULES[i][0]);
    if (!m) continue;
    const rep = LANG_RULES[i][1];
    const out = typeof rep === 'function' ? rep(m) : key.replace(LANG_RULES[i][0], rep);
    return raw.replace(key, out);
  }
  // 页头副标题常是「A · B · C」拼出来的，整串进不了字典：
  // 按 · 拆段逐段翻，只要有一段被翻过就采用新串（没翻的段原样保留）。
  if (key.indexOf(' · ') > 0) {
    const parts = key.split(' · ');
    let touched = false;
    const out = parts.map((seg) => {
      const s = seg.trim();
      if (!s) return seg;
      if (Object.prototype.hasOwnProperty.call(dict, s)) { touched = true; return dict[s]; }
      for (let i = 0; i < LANG_RULES.length; i++) {
        if (LANG_RULES[i][0].test(s)) {
          const rep = LANG_RULES[i][1];
          touched = true;
          return typeof rep === 'function' ? s.replace(LANG_RULES[i][0], rep)
            : s.replace(LANG_RULES[i][0], rep);
        }
      }
      return seg;
    });
    if (touched) return raw.replace(key, out.join(' · '));
  }
  return raw;
}

/* 把一棵子树里的文本节点与 title/placeholder/aria-label 就地翻译。
 * 中文环境直接返回（零开销）。 */
function translateDom(root) {
  if (!root || !isEn()) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
  const texts = [];
  while (walker.nextNode()) texts.push(walker.currentNode);
  for (let i = 0; i < texts.length; i++) {
    const node = texts[i];
    const parent = node.parentNode;
    if (!parent || SKIP_TAGS[parent.nodeName]) continue;
    if (parent.closest && parent.closest('[data-no-i18n]')) continue;
    const next = trText(node.nodeValue);
    if (next !== node.nodeValue) node.nodeValue = next;
  }
  const attrs = ['title', 'placeholder', 'aria-label'];
  const els = root.querySelectorAll ? root.querySelectorAll('[title],[placeholder],[aria-label]') : [];
  for (let i = 0; i < els.length; i++) {
    for (let a = 0; a < attrs.length; a++) {
      const v = els[i].getAttribute(attrs[a]);
      if (v) {
        const next = trText(v);
        if (next !== v) els[i].setAttribute(attrs[a], next);
      }
    }
  }
}

/* 切换语言：写 localStorage + <html lang> + 重绘界面。
 * rerender 由 app.js 提供（它才知道怎么重画），i18n 不反向依赖它。 */
function applyLang(lang, rerender) {
  const next = lang === 'en' ? 'en' : 'zh';
  try { localStorage.setItem(LANG_KEY, next); } catch { /* 无痕模式忽略 */ }
  const root = document.documentElement;
  if (root) {
    root.setAttribute('lang', next === 'en' ? 'en' : 'zh-CN');
    root.dataset.lang = next;
  }
  if (typeof rerender === 'function') rerender();
}

/* 异步渲染兜底：多数渲染器在 await 之后才写 DOM，render() 末尾那一次翻译
 * 抓不到晚到的节点（概览的「最近任务」、日志流、toast 都是这样）。
 * 用一个 MutationObserver 观察新增节点，攒到下一帧统一翻一遍。
 * 只监听 childList：我们改的是文本节点的值，不会再次触发，天然不会自激。 */
let _i18nBusy = false;
let _i18nObserver = null;

function startI18nWatcher() {
  if (!isEn() || !window.MutationObserver || _i18nObserver) return;
  const targets = ['#main', '#nav', '.topbar', '#logPanel', '#toast'];
  const translateAll = () => {
    for (let i = 0; i < targets.length; i++) {
      const el = document.querySelector(targets[i]);
      if (el) translateDom(el);
    }
  };
  _i18nObserver = new MutationObserver(() => {
    if (_i18nBusy) return;
    _i18nBusy = true;
    // 只跑一次：rAF 与定时器谁先到用谁
    const run = () => {
      if (!_i18nBusy) return;
      _i18nBusy = false;
      translateAll();
    };
    if (window.requestAnimationFrame) requestAnimationFrame(run);
    // rAF 在后台标签页会被节流，在部分自动化/嵌入环境里**完全不触发**——
    // 那时 `_i18nBusy` 会一直为 true，后续节点再也不翻译（实测踩过：
    // 无头浏览器里切页后整页停在中文）。所以始终挂一个定时器兜底。
    setTimeout(run, 50);
  });
  for (let i = 0; i < targets.length; i++) {
    const el = document.querySelector(targets[i]);
    if (el) _i18nObserver.observe(el, { childList: true, subtree: true });
  }
}
