/* ═══════════════════════════════════════════════════════════════════════════
   智能多维投资系统 · 前端应用
   五个一级视图：行情总览 / 智能研判 / 家庭投资 / 决策记录 / 连接
   图表全部走 Charts.*（内联 SVG，零依赖）
   ═══════════════════════════════════════════════════════════════════════════ */
'use strict';

/* ───────── 常量 ───────── */
const ASSET_KEYS = [
  ['cash_deposit', '活期与定期存款'], ['money_fund', '货币基金 / 现金管理'],
  ['bank_wealth', '银行理财'], ['bond_fund', '债券基金'],
  ['equity_fund', '股票与权益基金'], ['pension_account', '个人养老金账户'],
  ['insurance_cash', '储蓄型保险现金价值'], ['property_self_use', '自住房产（市值）'],
  ['property_invest', '投资性房产（市值）'], ['other', '其他资产'],
];
const LIABILITY_KEYS = [
  ['mortgage_balance', '房贷余额'], ['consumer_debt', '消费贷 / 信用卡分期'],
  ['other_debt', '其他负债'],
];

/* 大类配色：现代金融看板的分类色，红金为主轴，其余为可区分的辅助色 */
const CLASS_COLORS = {
  equity_cn: '#D92B2B', equity_global: '#EA580C', bond: '#1B2A4A',
  gold: '#C8A02E', commodity: '#7C3AED', cash: '#0891B2',
  deposit: '#64748B', insurance: '#BE185D',
};
const CLASS_SHORT = {
  equity_cn: 'A股权益', equity_global: '海外权益', bond: '债券', gold: '黄金',
  commodity: '商品', cash: '现金', deposit: '存款理财', insurance: '保险养老',
};
const CLASS_LABEL = {
  equity_cn: 'A股权益', equity_global: '海外与港股权益', bond: '债券',
  gold: '黄金', commodity: '商品与另类', cash: '现金管理',
  deposit: '存款与理财', insurance: '保险与养老金',
};

/* 智能体头像（单字 + 品牌色圆角块） */
const AGENT_COLORS = {
  macro: '#2563EB', valuation: '#D92B2B', lifecycle: '#0B8A5C',
  taxfee: '#C8A02E', behavioral: '#7C3AED',
  technical: '#D92B2B', flow: '#C8A02E',
  risk: '#AE1F1F', officer: '#1B2A4A', committee: '#1B2A4A',
};
const AGENT_CHAR = {
  macro: '宏', valuation: '价', lifecycle: '期', taxfee: '税', behavioral: '行',
  technical: '技', flow: '资', risk: '风', officer: '判', committee: '决',
};

const FAMILY_VIEWS = ['profile', 'live', 'plan', 'detail', 'team', 'stress', 'debate'];
const TOP_VIEWS = ['board', 'daily', 'family', 'records', 'connect'];

const state = {
  view: 'board',
  famTab: 'profile',
  board: null,
  minuteCode: 'sh000300',
  candleCode: 'sh000300',
  autoTimer: null,
  daily: null,
  dailyList: [],
  household: null,
  preview: null,
  runId: null,
  es: null,
  result: null,
  events: [],
  rounds: {},
  arguments: [],
  convergence: [],
  convSummary: null,
  lastStances: null,
  lastWhy: {},
  cards: {},
  // 研判进度
  prgT0: null,
  prgLastAt: null,
  prgTimer: null,
  prgPoll: null,
  lastPrg: null,
  lastSteps: [],
};

/* ═══════════════ 工具 ═══════════════ */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
/* ── Markdown 渲染 ────────────────────────────────────────────────────────
   大语言模型输出的是 Markdown（`## 标题`、`**加粗**`、`| 表格 |`）。
   早期直接 esc() 转义后显示，用户在界面上看到的是 Markdown 源码而不是排版内容。
   这里统一走 md.js；它内部先转义 HTML 再做语法转换，模型输出被当作不可信内容。
   若 md.js 未加载则退回 esc()，保证不会因此白屏。 */
function md(text) {
  if (window.MD && typeof MD.render === 'function') return MD.render(text || '');
  return esc(text || '');
}
function mdi(text) {   // 仅行内（表格单元格、列表项）
  if (window.MD && typeof MD.inline === 'function') return MD.inline(text || '');
  return esc(text || '');
}
function mdp(text) {   // 纯文本（title、摘要截断）
  // 注意：MD.plain() 按设计返回**未转义**的纯文本（便于按字数截断与搜索匹配）。
  // 拼进 title="" 这类属性时必须自己转义，否则引号逃逸就是 XSS。
  const s = (window.MD && typeof MD.plain === 'function')
    ? MD.plain(text || '') : String(text == null ? '' : text);
  return esc(s);
}
function money(v, digits = 1) {
  const n = Number(v) || 0;
  const a = Math.abs(n);
  if (a >= 1e8) return (n / 1e8).toFixed(2) + ' 亿';
  if (a >= 1e4) return (n / 1e4).toFixed(digits) + ' 万';
  return n.toFixed(0) + ' 元';
}
function pct(v, d = 1) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  return (Number(v) * 100).toFixed(d) + '%';
}
function signed(v, d = 2) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  const n = Number(v) * 100;
  return (n >= 0 ? '+' : '') + n.toFixed(d) + '%';
}
function num(v, d = 2) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  return Number(v).toFixed(d);
}
/* 涨跌方向 → CSS 类（红涨绿跌） */
function tone(v) {
  const n = Number(v) || 0;
  return n > 0 ? 'up' : n < 0 ? 'down' : 'flat';
}
function toast(msg, kind = '') {
  const t = el('div', 'toast ' + kind, esc(msg));
  $('#toast-wrap').appendChild(t);
  setTimeout(() => { t.style.transition = 'opacity .3s'; t.style.opacity = '0'; }, 3600);
  setTimeout(() => t.remove(), 4100);
}
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const txt = await r.text();
  let data;
  try { data = JSON.parse(txt); } catch { data = { detail: txt }; }
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}
function avatar(agent, extra = '') {
  const ch = AGENT_CHAR[agent] || '·';
  const bg = AGENT_COLORS[agent] || '#64748B';
  return `<span class="avatar" style="background:${bg}${extra}">${esc(ch)}</span>`;
}
function kpi(k, v, s = '', cls = '') {
  return `<div class="kpi"><div class="k">${esc(k)}</div>
    <div class="v ${cls}">${v}</div>${s ? `<div class="s">${esc(s)}</div>` : ''}</div>`;
}

/* ═══════════════ 导航 ═══════════════ */
function goto(view) {
  if (FAMILY_VIEWS.includes(view)) { showTop('family'); famGoto(view); return; }
  showTop(TOP_VIEWS.includes(view) ? view : 'board');
}
function showTop(view) {
  state.view = view;
  TOP_VIEWS.forEach(v => { const n = $('#view-' + v); if (n) n.hidden = v !== view; });
  $$('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $$('#bottom-nav button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  if (location.hash.slice(1) !== view) history.replaceState(null, '', '#' + view);
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (view === 'board') loadBoard();
  if (view === 'daily') loadDaily();
  if (view === 'records') loadRecords();
  if (view === 'connect') loadConnect();
}
function famGoto(tab) {
  state.famTab = tab;
  FAMILY_VIEWS.forEach(v => { const n = $('#view-' + v); if (n) n.hidden = v !== tab; });
  $$('#family-seg button').forEach(b => b.classList.toggle('active', b.dataset.fam === tab));
}
$$('#tabs button').forEach(b => b.addEventListener('click', () => goto(b.dataset.view)));
$$('#bottom-nav button').forEach(b => b.addEventListener('click', () => goto(b.dataset.view)));
$$('#family-seg button').forEach(b => b.addEventListener('click', () => famGoto(b.dataset.fam)));
document.addEventListener('click', e => {
  const a = e.target.closest('[data-goto]');
  if (a) { e.preventDefault(); goto(a.dataset.goto); }
});

/* ═══════════════════════════════════════════════════════════════════════════
   ① 行情总览
   ═══════════════════════════════════════════════════════════════════════════ */
async function loadBoard(silent = false) {
  try {
    const b = await api('/api/board' + (silent ? '' : '?force=1'));
    state.board = b;
    renderBoard(b);
  } catch (e) {
    if (!silent) toast('行情拉取失败：' + e.message, 'err');
    $('#board-sub').textContent = '行情拉取失败：' + e.message;
  }
}

function renderMarketPill(st) {
  const m = (st && st.market) || {};
  const cls = { open: 'open', auction: 'auction', break: 'closed', closed: 'closed' }[m.state] || 'closed';
  $('#mkt-dot').className = 'dot ' + cls;
  $('#mkt-text').innerHTML = `<b>${esc(m.label || '—')}</b>`;
}

function renderBoard(b) {
  renderMarketPill(b.status);
  const br = b.breadth || {};
  const sub = $('#board-sub');
  sub.innerHTML = `数据时间 ${esc((b.server_time || '').replace('T', ' '))}　`
    + `缓存 ${b.status?.cache_age_sec ?? '—'}s`
    + (b.stale ? '　<b style="color:var(--gold)">⚠ 上游不可用，显示内置数据集收盘价</b>' : '');

  /* KPI 条 */
  $('#board-kpis').innerHTML =
    kpi('上涨 / 下跌', `<span class="up">${br.up ?? 0}</span> / <span class="down">${br.down ?? 0}</span>`,
        `共 ${br.total ?? 0} 个标的`)
    + kpi('上涨占比', pct(br.up_ratio, 0),
        '市场宽度', (br.up_ratio || 0) > 0.6 ? 'up' : (br.up_ratio || 0) < 0.4 ? 'down' : '')
    + kpi('平均涨跌', `<span class="${tone(br.avg_change)}">${signed(br.avg_change)}</span>`, '全标的等权')
    + kpi('最强', `<span class="up">${signed((b.top_gainers?.[0] || {}).change_pct)}</span>`,
        (b.top_gainers?.[0] || {}).label || '')
    + kpi('最弱', `<span class="down">${signed((b.top_losers?.[0] || {}).change_pct)}</span>`,
        (b.top_losers?.[0] || {}).label || '')
    + kpi('债券 10Y 代理', `<span class="num">${num((b.universe || []).find(x => x.code === 'sh511260')?.price, 2)}</span>`,
        '十年国债ETF');

  /* 指数瓦片：分两行 */
  const benchGroups = b.groups || [];
  const flat = benchGroups.flatMap(g => g.items.map(it => ({ ...it, group: g.group })));
  const A = flat.filter(x => (x.group || '').startsWith('A股')).slice(0, 8);
  const B = flat.filter(x => !(x.group || '').startsWith('A股')).slice(0, 8);
  $('#board-idx-a').innerHTML = A.map(tileHtml).join('');
  $('#board-idx-b').innerHTML = B.map(tileHtml).join('');
  // 迷你走势：用快照里带回来的 60 日收盘序列
  flat.forEach(it => {
    const box = document.querySelector(`.spark[data-spark="${it.code}"]`);
    if (!box || !it.spark || it.spark.length < 5) return;
    box.innerHTML = Charts.sparkline({
      values: it.spark, width: 200, height: 34,
      color: tone(it.change_pct) === 'down' ? 'var(--down)' : 'var(--up)',
      baseline: it.spark[0],
    });
  });

  renderMinuteSeg(b.benchmarks || []);
  renderClassBars(b);
  renderHeat(b);
  renderRank(b);
  renderCandleSeg(b.benchmarks || []);
  renderTrend();
  loadMinute(state.minuteCode);
  loadCandles(state.candleCode);
  renderCorr();
}

function tileHtml(it) {
  const t = tone(it.change_pct);
  const cls = t === 'up' ? 'tint-up' : t === 'down' ? 'tint-down' : '';
  return `<div class="tile ${cls}">
    <div class="k">${esc(it.label || it.name)}</div>
    <div class="v ${t}">${num(it.price, it.price > 1000 ? 2 : 3)}</div>
    <div class="d"><span class="${t}">${signed(it.change_pct)}</span>
      <span class="muted">${it.change >= 0 ? '+' : ''}${num(it.change, 2)}</span></div>
    <div class="d muted" style="font-size:10.5px;margin-top:4px">
      高 ${num(it.high, 2)}　低 ${num(it.low, 2)}　额 ${(it.amount / 1e8).toFixed(1)}亿</div>
    <div class="spark" data-spark="${esc(it.code)}"></div>
  </div>`;
}

function renderMinuteSeg(list) {
  const seg = $('#board-minute-seg');
  const picks = list.filter(x => ['sh000001', 'sh000300', 'sz399006', 'sh000688', 'hkHSI',
                                  'sh513100', 'sh518880', 'sh511010'].includes(x.code));
  seg.innerHTML = picks.map(p =>
    `<button data-code="${esc(p.code)}" class="${p.code === state.minuteCode ? 'active' : ''}">
       ${esc(p.label)}</button>`).join('');
  seg.onclick = e => {
    const b = e.target.closest('button');
    if (!b) return;
    state.minuteCode = b.dataset.code;
    $$('#board-minute-seg button').forEach(x => x.classList.toggle('active', x === b));
    loadMinute(state.minuteCode);
  };
}

async function loadMinute(code) {
  try {
    const d = await api('/api/minute/' + encodeURIComponent(code));
    const box = $('#board-minute');
    if (!d.points || d.points.length < 2) {
      box.innerHTML = `<div class="empty" style="padding:36px 10px">
        <div class="small">${d.stale ? '分时数据暂不可用' : '暂无分时数据（休市或刚开盘）'}</div></div>`;
      $('#minute-meta').textContent = '';
      return;
    }
    box.innerHTML = Charts.intraday({ points: d.points, prevClose: d.prev_close,
                                     width: 760, height: 200 });
    const last = d.points[d.points.length - 1];
    const chg = d.prev_close ? (last.p / d.prev_close - 1) : 0;
    $('#minute-meta').innerHTML = `<span class="${tone(chg)}">${signed(chg)}</span>
      <span class="muted">　${esc(last.t)}</span>`;
  } catch (e) {
    $('#board-minute').innerHTML =
      `<div class="empty" style="padding:36px 10px"><div class="small">分时加载失败</div></div>`;
  }
}

function renderClassBars(b) {
  const items = (b.by_class || []).map(c => ({
    label: CLASS_LABEL[c.asset_class] || c.asset_class,
    value: c.change_pct,
    sub: `${c.n} 只 · 涨 ${c.up} 跌 ${c.down}`,
  }));
  $('#board-class').innerHTML = items.length
    ? Charts.barRows({ items, width: 420, unit: '%' })
    : '<div class="muted small">无数据</div>';
}

function renderHeat(b) {
  const items = (b.universe || []).map(r => ({
    label: r.label || r.name, sub: r.asset_class, value: r.change_pct,
  }));
  $('#heat-meta').textContent = `${items.length} 个标的 · 颜色深浅＝涨跌幅`;
  $('#board-heat').innerHTML = items.length
    ? Charts.heatGrid({ items, columns: 6, cellHeight: 56 })
    : '<div class="muted small">无数据</div>';
}

function renderRank(b) {
  const row = (r, i, cls) => `<div style="display:flex;align-items:center;gap:10px;
      padding:7px 0;border-bottom:1px solid var(--line)">
      <span class="num" style="width:16px;color:var(--ink-4);font-size:11px">${i + 1}</span>
      <span style="flex:1;font-size:12.5px">${esc(r.label || r.name)}</span>
      <span class="num ${cls}" style="font-weight:600">${signed(r.change_pct)}</span>
    </div>`;
  $('#board-rank').innerHTML =
    `<div class="small muted" style="margin-bottom:4px">涨幅前五</div>`
    + (b.top_gainers || []).map((r, i) => row(r, i, 'up')).join('')
    + `<div class="small muted" style="margin:12px 0 4px">跌幅前五</div>`
    + (b.top_losers || []).map((r, i) => row(r, i, 'down')).join('');
}

function renderCandleSeg(list) {
  const seg = $('#board-candle-seg');
  const picks = list.filter(x => ['sh000300', 'sh000905', 'sz399006', 'sh000922',
                                  'sh518880', 'sh511010', 'sh513100', 'sh000012'].includes(x.code));
  seg.innerHTML = picks.map(p =>
    `<button data-code="${esc(p.code)}" class="${p.code === state.candleCode ? 'active' : ''}">
       ${esc(p.label)}</button>`).join('');
  seg.onclick = e => {
    const btn = e.target.closest('button');
    if (!btn) return;
    state.candleCode = btn.dataset.code;
    $$('#board-candle-seg button').forEach(x => x.classList.toggle('active', x === btn));
    loadCandles(state.candleCode);
  };
}

async function loadCandles(code) {
  try {
    const d = await api(`/api/candles/${encodeURIComponent(code)}?limit=160`);
    const bars = d.bars || [];
    $('#board-candles').innerHTML = bars.length > 5
      ? Charts.candles({ bars, width: 760, height: 280, ma: [5, 20, 60], showVolume: true })
      : '<div class="muted small" style="padding:20px">数据不足</div>';
  } catch (e) {
    $('#board-candles').innerHTML = '<div class="muted small" style="padding:20px">K 线加载失败</div>';
  }
}

function renderTrend() {
  const b = state.board;
  if (!b) return;
  const picks = ['sh000300', 'sh000905', 'sz399006', 'sh000922', 'sh518880', 'sh000012'];
  const names = {};
  (b.benchmarks || []).forEach(x => { names[x.code] = x.label; });
  (b.universe || []).forEach(x => { names[x.code] = names[x.code] || x.label || x.name; });
  api(`/api/trend?codes=${picks.join(',')}&days=120`).then(d => {
    if (!d.series || !d.series.length) throw new Error('no data');
    const cols = ['var(--red)', 'var(--orange)', 'var(--violet)', 'var(--gold)',
                  'var(--cyan)', 'var(--navy)'];
    $('#board-trend').innerHTML = Charts.lines({
      series: d.series.map(s => ({ name: names[s.code] || s.name, values: s.values,
                                   dates: s.dates })),
      colors: cols, width: 760, height: 280, showAxis: true, showLegend: true,
    });
  }).catch(() => {
    $('#board-trend').innerHTML =
      '<div class="empty" style="padding:28px"><div class="small muted">走势数据不可用</div></div>';
  });
}

async function renderCorr() {
  const box = $('#board-corr');
  if (!state.board) return;
  const names = {};
  (state.board.universe || []).forEach(u => { names[u.code] = u.label || u.name; });
  try {
    const d = await api('/api/correlation');
    if (!d || !d.codes) throw new Error('no data');
    const labels = d.codes.map(c => names[c] || c);
    box.innerHTML = Charts.heatmap({
      labels, matrix: d.matrix, size: Math.min(620, 24 * d.codes.length),
    });
  } catch (e) {
    box.innerHTML = '<div class="empty" style="padding:28px"><div class="small muted">相关性数据不可用</div></div>';
  }
}

/* 自动刷新 */
function toggleAuto() {
  if (state.autoTimer) {
    clearInterval(state.autoTimer); state.autoTimer = null;
    $('#btn-autoref').textContent = '自动刷新：关';
    $('#btn-autoref').classList.remove('primary');
  } else {
    state.autoTimer = setInterval(() => loadBoard(true), 15000);
    $('#btn-autoref').textContent = '自动刷新：开（15s）';
    $('#btn-autoref').classList.add('primary');
    loadBoard(true);
  }
}
$('#btn-refresh').addEventListener('click', () => loadBoard());
$('#btn-autoref').addEventListener('click', toggleAuto);

/* ═══════════════════════════════════════════════════════════════════════════
   ② 智能研判
   ═══════════════════════════════════════════════════════════════════════════ */
async function loadDaily() {
  loadVol();            // 波动率与仓位乘数
  try {
    const st = await api('/api/daily/status').catch(() => null);
    if (st) $('#nightly-at').textContent = st.at || '20:30';
    const list = await api('/api/daily/list').catch(() => ({ reports: [] }));
    state.dailyList = list.reports || [];
    renderDailyHistory();

    if (!state.dailyList.length) {
      $('#daily-empty-wrap').hidden = false;
      $('#daily-body').hidden = true;
      $('#daily-kpi').innerHTML = kpi('研判报告', '0', '尚未生成')
        + kpi('自动运行时间', st?.at || '20:30', '每交易日一次')
        + kpi('调度状态', st?.running ? '运行中' : '未启动', '', st?.running ? 'up' : '');
      return;
    }
    const rec = await api('/api/daily/latest');
    state.daily = rec;
    renderDaily(rec);
  } catch (e) {
    toast('研判记录读取失败：' + e.message, 'err');
  }
}

function renderDaily(rec) {
  $('#daily-empty-wrap').hidden = true;
  $('#daily-body').hidden = false;
  const d = rec.dashboard || {};
  const sc = d.scores || {};
  const pos = d.position || {};
  const br = (rec.board || {}).breadth || {};
  const degraded = !!rec.degraded;

  $('#daily-sub').innerHTML = `${esc(rec.date)} 研判　生成于 ${esc((rec.generated_at || '').replace('T', ' '))}　`
    + `耗时 ${num(rec.elapsed_sec, 0)}s　模型调用 ${rec.usage?.calls || 0} 次`
    + (degraded ? '　<b style="color:var(--gold-d)">降级模式</b>' : '');

  // 降级横幅：说明哪些是系统算的、哪些因为模型不可用而缺失
  let banner = $('#daily-degraded');
  if (!banner) {
    banner = el('div', ''); banner.id = 'daily-degraded';
    $('#daily-body').insertBefore(banner, $('#daily-body').firstChild);
  }
  banner.innerHTML = degraded
    ? `<div class="note warn" style="margin-bottom:14px">
         <b>降级模式：</b>${esc(d.degraded_note || '模型后端当前不可用。')}
         本页的六维分数、动量表、市场宽度等<b>客观指标仍然完整</b>——
         它们本来就完全由行情数据推导，与模型无关。
         但<b>大类观点、建议仓位、行动建议与关键分歧需要模型研判，本次为空</b>。
         系统不会用机械规则去填满这些字段，那会让人误以为那是研判结论。</div>`
    : '';

  const objOnly = Object.values(sc).every(x => !x.base || x.adjust === 0);

  $('#daily-kpi').innerHTML =
    kpi('研判日期', esc(rec.date), degraded ? '降级模式（仅客观指标）' : '交易日收盘后', degraded ? 'gold' : '')
    + kpi('建议总仓位', degraded ? '—' : pct(pos.suggested, 0),
        degraded ? '模型不可用' : `区间 ${pct(pos.range?.[0], 0)}–${pct(pos.range?.[1], 0)}`,
        degraded ? '' : 'gold')
    + kpi('当日平均涨跌', `<span class="${tone(br.avg_change)}">${signed(br.avg_change)}</span>`,
        `涨 ${br.up ?? 0} 跌 ${br.down ?? 0}`)
    + kpi('综合评分', num(Object.values(sc).reduce((a, x) => a + (x.score || 0), 0) / (Object.keys(sc).length || 1), 1),
        '六维均值')
    + kpi('行动建议', degraded ? '—' : `${(d.actions || []).length} 条`,
        degraded ? '需模型研判' : `${(d.risks || []).length} 条风险提示`)
    + kpi('辩论轮数', `${(rec.convergence || {}).rounds ?? '—'}`,
        (rec.convergence || {}).termination_track || (degraded ? '未辩论' : ''));

  /* 雷达 + 评分条 */
  const axes = ['估值', '动量', '资金', '情绪', '宏观', '风险'];
  const vals = axes.map(a => (sc[a] || {}).score ?? 50);
  $('#daily-radar').innerHTML = Charts.radar({
    axes, series: [{ name: '今日研判', values: vals }], size: 260, max: 100,
  });
  const colorOf = v => v >= 65 ? 'var(--up)' : v >= 45 ? 'var(--gold)' : 'var(--down)';
  $('#daily-scores').innerHTML = axes.map(a => {
    const it = sc[a] || {};
    return `<div class="score-row wrap">
      <span class="lb">${esc(a)}</span>
      <span class="track"><span class="fill" style="width:${it.score ?? 0}%;
        background:${colorOf(it.score ?? 0)}"></span></span>
      <span class="val">${num(it.score, 0)}</span>
      <span class="rs">${mdi(it.reason || '')}
        ${it.adjust ? `<b style="color:var(--ink-3)">（系统 ${it.base} ${it.adjust > 0 ? '+' : ''}${it.adjust}）</b>` : ''}
      </span></div>`;
  }).join('');

  /* 研判官结论 */
  $('#daily-position').innerHTML = degraded
    ? `<div style="display:flex;align-items:baseline;gap:12px">
         <div><div class="small muted">建议总仓位</div>
           <div class="num" style="font-size:34px;font-weight:600;color:var(--ink-4);line-height:1.1">—</div>
           <div class="small muted">需模型研判</div></div></div>
       <div class="small" style="margin-top:10px;color:var(--ink-2);line-height:1.7">
         仓位建议需要综合宏观判断与风险偏好，属于主观研判，不能由行情数据直接推导。
         离线模式下系统不会用机械规则编造一个数字。</div>`
    : `<div style="display:flex;align-items:baseline;gap:12px">
         <div><div class="small muted">建议总仓位</div>
           <div class="num" style="font-size:34px;font-weight:600;color:var(--red);line-height:1.1">
             ${pct(pos.suggested, 0)}</div>
           <div class="small muted">区间 ${pct(pos.range?.[0], 0)} – ${pct(pos.range?.[1], 0)}</div></div>
       </div>
       <div class="small md-body" style="margin-top:10px;color:var(--ink-2)">${md(pos.reason || '')}</div>`;
  const sum = $('#daily-summary');
  sum.className = degraded ? 'note warn' : 'note';
  sum.innerHTML = md(d.summary || '（研判官未给出总结）');

  /* 大类观点 */
  $('#daily-stance').innerHTML = `<table class="tbl"><thead><tr>
      <th>大类</th><th>观点</th><th class="r">信心度</th><th>依据</th></tr></thead><tbody>`
    + Object.entries(d.stance || {}).map(([k, v]) => `<tr>
        <td><b>${esc(v.label || CLASS_LABEL[k] || k)}</b></td>
        <td><span class="view-badge view-${esc(v.view)}">${esc(v.view)}</span></td>
        <td class="r">${pct(v.conviction, 0)}</td>
        <td style="white-space:normal;color:var(--ink-2);font-size:12px">${mdi(v.reason || '')}</td>
      </tr>`).join('') + '</tbody></table>';

  /* 关键分歧 */
  const dis = d.disagreements || [];
  $('#daily-disagreements').innerHTML = dis.length ? dis.map(x => `
    <div class="arg">
      <div class="arg-hd"><span class="arg-id">争点</span>
        <b style="font-size:12.8px">${esc(x.topic || '')}</b></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:12px;line-height:1.65">
        <div><span class="pill chip-up">看多</span><div class="md-body" style="margin-top:5px;color:var(--ink-2)">${md(x.bull || '')}</div></div>
        <div><span class="pill chip-down">看空</span><div class="md-body" style="margin-top:5px;color:var(--ink-2)">${md(x.bear || '')}</div></div>
      </div>
      <div class="ruling md-body"><b>裁决：</b>${md(x.resolution || '')}</div>
    </div>`).join('') : '<div class="muted small">无</div>';

  /* 行动建议 */
  const acts = d.actions || [];
  $('#daily-actions').innerHTML = acts.length ? acts.map(a => `
    <div class="arg" style="border-left:3px solid ${a.priority === '高' ? 'var(--red)' : a.priority === '中' ? 'var(--gold)' : 'var(--line-2)'}">
      <div class="arg-hd"><span class="pill ${a.priority === '高' ? 'solid' : ''}">${esc(a.priority || '')}</span>
        <b style="font-size:12.8px">${esc(a.action || '')}</b></div>
      ${a.trigger ? `<div class="arg-mt">触发条件：${mdi(a.trigger)}</div>` : ''}
      ${a.reason ? `<div class="md-body" style="font-size:12px;color:var(--ink-2);margin-top:4px">${md(a.reason)}</div>` : ''}
    </div>`).join('')
    : `<div class="empty" style="padding:28px 10px"><div class="small">
         ${degraded ? '离线模式未调用模型，无行动建议' : '本次研判未给出行动建议'}</div></div>`;

  /* 风险 + 观察 */
  $('#daily-risks').innerHTML =
    (d.risks || []).map(r => `<div class="note bad md-body" style="margin-bottom:8px">${md(r)}</div>`).join('')
    + ((d.watchlist || []).length
      ? `<div class="small muted" style="margin:12px 0 6px">明日观察清单</div>`
        + d.watchlist.map(w => `<div class="note warn md-body" style="margin-bottom:6px">${md(w)}</div>`).join('')
      : '');

  /* 动量表 */
  const ps = rec.per_symbol || [];
  $('#daily-momentum').innerHTML = `<table class="tbl"><thead><tr>
      <th>标的</th><th class="r">1 日</th><th class="r">5 日</th><th class="r">20 日</th>
      <th class="r">60 日</th><th class="r">距一年高</th><th class="r">波动环境</th>
    </tr></thead><tbody>` + ps.map(s => `<tr>
      <td>${esc(s.name)}</td>
      <td class="r ${tone(s.d1)}">${signed(s.d1)}</td>
      <td class="r ${tone(s.d5)}">${signed(s.d5)}</td>
      <td class="r ${tone(s.d20)}">${signed(s.d20)}</td>
      <td class="r ${tone(s.d60)}">${signed(s.d60)}</td>
      <td class="r down">${signed(s.from_high)}</td>
      <td class="r">${num(s.vol_regime, 2)}</td>
    </tr>`).join('') + '</tbody></table>';

  /* 收敛曲线 + 时间线 */
  const conv = rec.convergence || {};
  const track = conv.cv_track || [];
  $('#daily-conv-meta').innerHTML = `${conv.rounds ?? 0} 轮　CV ${num(conv.final_cv, 4)}　W ${num(conv.final_w, 3)}`;
  $('#daily-cv-chart').innerHTML = track.length
    ? Charts.lines({
        series: [
          { name: '分歧度 CV', values: track.map(t => t.cv ?? 0) },
          { name: '排序一致性 W', values: track.map(t => t.w ?? 0) },
        ],
        colors: ['var(--red)', 'var(--gold)'], width: 560, height: 180, showLegend: true,
      })
    : `<div class="empty" style="padding:24px"><div class="small">${
        degraded ? '离线模式未进行辩论，无收敛数据' : '本次研判未做收敛量化'}</div></div>`;

  const speeches = rec.speeches || {};
  const lastRound = Object.keys(speeches).sort((a, b) => Number(b) - Number(a))[0];
  const tl = $('#daily-timeline');
  tl.innerHTML = Object.values(speeches[lastRound] || {}).map(sp => `
    <div class="tl-item done">
      <div class="tl-hd">${avatar(sp.agent)}<span class="who">${esc(sp.name || sp.agent)}</span></div>
      <div class="tl-bd clamp md-body">${md((sp.text || '').slice(0, 900))}</div>
    </div>`).join('')
    || `<div class="empty" style="padding:24px"><div class="small">${
        degraded ? '离线模式未调用模型，无讨论记录' : '无发言记录'}</div></div>`;

  $('#daily-badge').hidden = false;
}

/* 波动位置图：把「现在在哪、未来去哪」画成一条轴。
   只讲一件事 —— 当前波动相对历史区间与目标水平的位置。
   （这张图原本叫 volBandSvg，随诊断面板一起被删掉了，此处重写。） */
function volBandSvg(v) {
  const W = 620, H = 150, L = 24, R = 24, T = 34, B = 40;
  const iw = W - L - R;
  const p33 = v.hist_p33 || v.forecast_vol * 0.7;
  const p67 = v.hist_p67 || v.forecast_vol * 1.3;
  const hi = Math.max(p67 * 1.7, v.current_vol * 1.25, v.forecast_vol * 1.3, 0.05);
  const X = x => L + Math.min(1, Math.max(0, x / hi)) * iw;
  const y = T + 42;

  const col = v.state === '高' ? 'var(--red)'
    : v.state === '低' ? 'var(--down)' : 'var(--gold)';
  const label = v.state === '高' ? '偏高' : v.state === '低' ? '偏低' : '正常';

  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
      aria-label="当前波动位置与未来预测">
    <defs><linearGradient id="vbg" x1="0" x2="1">
      <stop offset="0" stop-color="var(--down-l)"/>
      <stop offset="0.5" stop-color="var(--gold-l)"/>
      <stop offset="1" stop-color="var(--red-l)"/>
    </linearGradient></defs>
    <rect x="${L}" y="${y - 9}" width="${iw}" height="18" rx="9"
      fill="url(#vbg)" opacity="0.55"/>
    <rect x="${X(p33)}" y="${y - 13}" width="${Math.max(2, X(p67) - X(p33))}"
      height="26" rx="4" fill="none" stroke="var(--ink-4)" stroke-dasharray="3 3"/>
    <text x="${L}" y="${T - 8}" font-size="12" fill="var(--ink-4)">低波动</text>
    <text x="${L + iw}" y="${T - 8}" font-size="12" fill="var(--ink-4)"
      text-anchor="end">高波动</text>

    <line x1="${X(v.target_vol)}" y1="${y - 26}" x2="${X(v.target_vol)}"
      y2="${y + 26}" stroke="var(--ink-3)" stroke-width="1.5" stroke-dasharray="4 3"/>
    <text x="${X(v.target_vol)}" y="${y + 40}" font-size="11"
      fill="var(--ink-3)" text-anchor="middle">目标 ${pct(v.target_vol, 0)}</text>

    <circle cx="${X(v.current_vol)}" cy="${y}" r="7" fill="${col}"
      stroke="var(--surface)" stroke-width="2.5"/>
    <text x="${X(v.current_vol)}" y="${y - 22}" font-size="12.5" font-weight="600"
      fill="${col}" text-anchor="middle">现在 ${pct(v.current_vol, 1)}</text>

    <circle cx="${X(v.forecast_vol)}" cy="${y}" r="6" fill="var(--ink-2)"
      stroke="var(--surface)" stroke-width="2.5" opacity="0.85"/>
    <text x="${X(v.forecast_vol)}" y="${y + 20}" font-size="12"
      fill="var(--ink-2)" text-anchor="middle">未来 20 日 ${pct(v.forecast_vol, 1)}</text>

    <text x="${L}" y="${T + 4}" font-size="12" fill="var(--ink-2)">当前状态：${label}</text>
  </svg>`;
}

async function loadVol() {
  try {
    const v = await api('/api/vol');
    renderVol(v);
  } catch (e) {
    $('#vol-meta').textContent = '';
    const empty = $('#vol-empty');
    if (empty) {
      empty.hidden = false;
      empty.innerHTML = '<div class="small muted">风险模型暂时取不到数据</div>';
    }
    const body = $('#vol-body');
    if (body) body.hidden = true;
  }
}

function renderVol(v) {
  const meta = $('#vol-meta');
  const empty = $('#vol-empty');
  const body = $('#vol-body');
  if (!v || !v.available) {
    meta.textContent = '';
    if (empty) {
      empty.hidden = false;
      empty.innerHTML = '<div class="small muted">风险模型暂不可用</div>';
    }
    if (body) body.hidden = true;
    return;
  }
  if (empty) empty.hidden = true;
  if (body) body.hidden = false;
  meta.textContent = '更新于 ' + (v.as_of || '—');

  const mult = v.multiplier;
  const up = mult >= 1;
  const stateWord = v.state === '高' ? '偏高' : v.state === '低' ? '偏低' : '正常';
  // 目标波动下的等风险仓位：乘数就是「同样风险能拿多少」
  const example = Math.round(60 * mult);

  // ① 结论 —— 一句话说清该做什么，给到具体数字
  $('#vol-verdict').innerHTML =
    `<div class="vrd-main">建议把权益仓位乘 <b>${num(mult, 2)}</b></div>
     <div class="vrd-sub">例如现在持有 60% 权益，可调整到约 <b>${example}%</b></div>`;

  // ② 解读 —— 为什么，以及对「它不预测涨跌」说清楚
  $('#vol-read').innerHTML =
    `<b>为什么：</b>当前市场波动${stateWord}（历史 ${pct(v.percentile, 0)} 分位）。
     ${up
       ? '同样的仓位，现在承担的风险比目标水平更低，所以可以适度放宽。'
       : '同样的仓位，现在承担的风险比目标水平更高，所以建议收缩。'}<br>
     <b>注意：</b>这只调整<b>仓位大小</b>，不预测涨跌方向。目的是让你承担的风险
     保持稳定，而不是让你赚得更多。`;

  // ③ 一张图
  $('#vol-band').innerHTML = volBandSvg(v);
  $('#vol-band-n').innerHTML =
    `当前波动 <b>${pct(v.current_vol, 1)}</b>，历史 33/67 分位是
     ${pct(v.hist_p33, 1)} / ${pct(v.hist_p67, 1)}；
     未来 20 日预测 <b>${pct(v.forecast_vol, 1)}</b>，目标 ${pct(v.target_vol, 0)}。`;

  // ④ 可执行：仓位对照表
  const bases = [0.3, 0.4, 0.5, 0.6, 0.8];
  $('#vol-scale').innerHTML = bases.map(b => {
    const adj = b * mult;
    const over = adj > 0.9;
    const col = over ? 'var(--red)' : mult < 1 ? 'var(--gold)' : 'var(--down)';
    const hit = Math.abs(b - 0.6) < 1e-6;
    return `<div class="vol-scale-row"${hit ? ' style="font-weight:600"' : ''}>
      <span class="base">${pct(b, 0)}</span>
      <span class="arrow">→</span>
      <span class="adj" style="color:${col}">${pct(adj, 0)}</span>
      <span class="bar"><i style="width:${Math.min(100, adj * 100)}%;background:${col}"></i></span>
      <span class="tag" style="color:${col}">${over ? '偏高' : mult < 1 ? '收缩' : '放开'}</span>
    </div>`;
  }).join('')
    + `<div class="dg-n" style="margin-top:10px">
        左列是当前权益仓位，右列是调整后的仓位。<br>
        <span class="muted">本项目量化预测部分仅参考，请务必谨慎用于投资决策。</span>
      </div>`;
}

/* ── 设置弹窗：密钥与模型 ────────────────────────────────────────────────
   为什么做这个：让用户去项目目录手写 .env 是开发者的习惯，不是桌面应用该有的
   体验 —— 找不到路径、键名写错一个字母没有任何反馈、改完还得重启。
   现在首次启动就会问，之后点右上角「设置」随时可改。

   安全约定：**接口不回传明文密钥**。输入框永远不预填，占位符只显示掩码
   （sk-ff9••••••••e504）；留空表示"保持当前密钥不变"。
   ──────────────────────────────────────────────────────────────────────── */
let SETTINGS = null;

function openSettings(firstRun) {
  $('#settings-mask').hidden = false;
  const s = SETTINGS || {};
  $('#settings-title').textContent = firstRun ? '首次使用：填写 API Key' : '模型与密钥';
  $('#settings-lead').innerHTML = firstRun
    ? `本系统需要一个大语言模型后端才能运行研判与家庭配置审议。<br>
       填入 API Key 即可开始，<b>配置只保存在本机</b>。<br>
       暂时不填也能用：行情总览与全部确定性指标不依赖模型。`
    : `当前密钥来源：<b>${esc(s.key_source || '未配置')}</b>。输入框留空则保持不变。`;
  $('#settings-later').hidden = !firstRun;

  $('#set-key').value = '';
  $('#set-key').placeholder = s.key_masked || 'sk-...';
  $('#set-key-hint').textContent = s.configured
    ? `当前：${s.key_masked} —— 留空表示不改`
    : '尚未配置，研判与审议功能不可用';
  $('#set-base').value = s.base_url || 'https://api.deepseek.com';
  $('#set-model').value = s.model || 'deepseek-chat';
  $('#set-model-hint').textContent = `当前来源：${s.model_source || '默认值'}`;

  $('#set-model-list').innerHTML = (s.model_choices || [])
    .map(c => `<option value="${esc(c.value)}">${esc(c.label)}</option>`).join('');

  $('#settings-path').textContent = s.settings_file ? `配置保存在：${s.settings_file}` : '';
  const t = $('#set-test');
  t.hidden = true; t.className = 'test-box';
  setTimeout(() => $('#set-key').focus(), 80);
}

function closeSettings() { $('#settings-mask').hidden = true; }

async function loadSettings() {
  try {
    SETTINGS = await api('/api/settings');
  } catch (e) { SETTINGS = null; return; }
  const dot = $('#llm-dot'), txt = $('#llm-text');
  if (SETTINGS.configured) {
    dot.className = 'dot ok';
    txt.textContent = SETTINGS.model || '已配置';
  } else {
    dot.className = 'dot bad';
    txt.textContent = '未配置';
  }
  // 首次启动且未配置 → 直接把弹窗推到面前，而不是让用户自己去找设置入口
  if (!SETTINGS.configured) setTimeout(() => openSettings(true), 600);
}

function settingsBody() {
  const k = $('#set-key').value.trim();
  return {
    // 留空要表达"不改"，而空串在后端含义是"清除" —— 所以留空时发 null
    api_key: k || null,
    base_url: $('#set-base').value.trim() || null,
    model: $('#set-model').value.trim() || null,
  };
}

async function testSettings() {
  const t = $('#set-test');
  t.hidden = false; t.className = 'test-box wait';
  t.textContent = '正在连接…';
  try {
    const r = await api('/api/settings/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settingsBody()),
    });
    if (r.ok) {
      t.className = 'test-box ok';
      t.innerHTML = `✅ 连接成功　模型 <b>${esc(r.model)}</b>　`
        + `延迟 ${r.latency_ms} ms　回复「${esc(r.reply || '')}」`;
    } else {
      t.className = 'test-box err';
      t.innerHTML = `❌ 失败：${esc(r.error || '未知错误')}`;
    }
  } catch (e) {
    t.className = 'test-box err';
    t.textContent = `❌ 请求失败：${e.message}`;
  }
}

async function saveSettings() {
  try {
    SETTINGS = await api('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settingsBody()),
    });
    closeSettings();
    toast(SETTINGS.configured ? '已保存，下一次研判即生效（无需重启）'
                              : '已保存，但密钥仍为空', SETTINGS.configured ? 'ok' : 'warn');
    loadSettings();
  } catch (e) {
    toast('保存失败：' + e.message, 'err');
  }
}

$('#btn-settings').addEventListener('click', () => openSettings(false));
$('#llm-pill').addEventListener('click', () => openSettings(false));
$('#settings-x').addEventListener('click', closeSettings);
$('#settings-later').addEventListener('click', closeSettings);
$('#settings-test-btn').addEventListener('click', testSettings);
$('#settings-save').addEventListener('click', saveSettings);
$('#settings-mask').addEventListener('click', e => {
  if (e.target.id === 'settings-mask') closeSettings();   // 点遮罩关闭
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !$('#settings-mask').hidden) closeSettings();
});

/* ── 研判进度条 ────────────────────────────────────────────────────────── */
// 超过这个秒数没有任何进度事件，就不再当成"模型慢"，而是"可能失联"。
// 依据：服务端单次研判有 15 分钟硬上限，正常一次 3~5 分钟；
// 而单次 LLM 调用最坏 3×120s 超时 + 退避 ≈ 6.3 分钟。
// 150 秒还没动静就已经不正常了，不该让用户对着一个递增的数字干等。
const STALE_SEC = 150;
const STATUS_POLL_MS = 15000;

function resetProgress() {
  const box = $('#daily-progress');
  box.hidden = false;
  $('#prg-label').textContent = '准备中…';
  $('#prg-sub').textContent = '';
  $('#prg-pct').textContent = '0';
  $('#prg-detail').textContent = '正在启动研判…';
  $('#prg-elapsed').textContent = '';
  const f = $('#prg-fill');
  f.style.width = '0%'; f.classList.remove('done');
  $('#prg-dot').classList.remove('done');
  $('#prg-steps').innerHTML = '';
  state.prgT0 = Date.now();
  state.prgLastAt = Date.now();
  if (state.prgTimer) clearInterval(state.prgTimer);
  state.prgTimer = setInterval(tickProgress, 1000);
  // 运行期间定期向服务端确认任务是否真的还在跑（见 pollRunStatus 的注释）
  if (state.prgPoll) clearInterval(state.prgPoll);
  state.prgPoll = setInterval(() => {
    if (state.prgT0 && (Date.now() - (state.prgLastAt || Date.now())) / 1000 >= 60) {
      pollRunStatus(false);
    }
  }, STATUS_POLL_MS);
}
function tickProgress() {
  if (!state.prgT0) return;
  const sec = Math.round((Date.now() - state.prgT0) / 1000);
  $('#prg-elapsed').textContent = `已用 ${sec}s`;
  const idle = Math.round((Date.now() - (state.prgLastAt || Date.now())) / 1000);

  // 阶段一（30s~）：模型慢，但连接是好的
  if (idle >= 30 && idle < STALE_SEC) {
    $('#prg-detail').innerHTML =
      `<span style="color:var(--gold-d)">已等待模型响应 ${idle} 秒…`
      + `（上游变慢时系统会自动重试；若五路调用全部失败，将自动降级为仅客观指标）</span>`;
    return;
  }
  // 阶段二（≥150s）：不再无限报数 —— 那个数字没有信息量，只会让人干等。
  // 改为明确告知可能失联，并给出可操作的出口。
  if (idle >= STALE_SEC) {
    const mins = Math.floor(sec / 60);
    $('#prg-detail').innerHTML =
      `<span style="color:var(--red)"><b>连接可能已失联</b> —— 已 ${mins} 分钟没有收到任何进度。
       服务端每次研判有 15 分钟硬上限，正常一次 3~5 分钟。</span>`
      + `<span style="margin-left:10px">`
      + `<button class="btn sm" id="prg-reconnect">重新连接</button> `
      + `<button class="btn sm ghost" id="prg-giveup">放弃本次</button></span>`;
    const rc = $('#prg-reconnect'), gu = $('#prg-giveup');
    if (rc && !rc._bound) {
      rc._bound = true;
      rc.addEventListener('click', () => {
        // 直接问服务端：现在到底有没有在跑。这个判断现在可信了 ——
        // /api/daily/status 的 running 之前报的是调度器守护线程（永远为真），已修。
        pollRunStatus(true);
      });
    }
    if (gu && !gu._bound) {
      gu._bound = true;
      gu.addEventListener('click', () => {
        if (state.es) state.es.close();
        stopProgress();
        $('#daily-progress').hidden = true;
        toast('已放弃本次等待（服务端任务可能仍在运行）', 'warn');
      });
    }
  }
}

/* 向服务端确认：此刻到底有没有一次研判在跑。
   这是区分「模型慢」与「连接死」的唯一可靠依据 ——
   SSE 的心跳是注释行（`: keep-alive`），不会触发 onmessage，
   所以前端单靠流本身无法判断对端是死是活。 */
async function pollRunStatus(manual) {
  try {
    const st = await api('/api/daily/status');
    if (st.running) {
      if (manual) {
        toast(`服务端仍在运行（已 ${Math.round((st.active_run?.elapsed || 0) / 60)} 分钟）`, 'ok');
        // 真正的重连：重新订阅同一个 run 的事件流（服务端会补发历史）
        const rid = st.active_run?.run_id;
        if (rid && state.es) {
          state.es.close();
          state.prgLastAt = Date.now();
          connectDailyStream(rid, () => {});
        }
      }
      return true;
    }
    // 服务端已无运行中的任务，而我们还在等 —— 确定是失联
    if (state.es) state.es.close();
    stopProgress();
    $('#prg-label').textContent = '连接已结束';
    $('#prg-dot').classList.remove('done');
    $('#prg-detail').innerHTML =
      `<span style="color:var(--red)"><b>服务端已无运行中的研判</b> —— `
      + `本次流已失效（常见于服务重启或浏览器休眠）。</span>`
      + `<span style="margin-left:10px"><button class="btn sm" id="prg-rerun">重新发起研判</button></span>`;
    const rb = $('#prg-rerun');
    if (rb && !rb._bound) {
      rb._bound = true;
      rb.addEventListener('click', () => { $('#daily-progress').hidden = true; runDaily(true); });
    }
    toast('研判流已失效，服务端没有正在运行的任务', 'err');
    return false;
  } catch (e) {
    return null;   // 查询本身失败，不改判定
  }
}
function stopProgress() {
  if (state.prgTimer) { clearInterval(state.prgTimer); state.prgTimer = null; }
  if (state.prgPoll) { clearInterval(state.prgPoll); state.prgPoll = null; }
  state.prgT0 = null;
}
function renderProgress(d) {
  state.prgLastAt = Date.now();
  state.lastPrg = d;
  state.lastSteps = d.steps || state.lastSteps || [];
  $('#daily-progress').hidden = false;
  const pct = Number(d.pct ?? 0);
  $('#prg-label').textContent = d.label || '';
  $('#prg-sub').textContent = d.sub ? `（${d.sub}）` : '';
  $('#prg-pct').textContent = String(Math.floor(pct));
  $('#prg-detail').textContent = d.detail || '';
  const f = $('#prg-fill');
  f.style.width = Math.min(100, pct) + '%';
  const fin = pct >= 100;
  f.classList.toggle('done', fin);
  $('#prg-dot').classList.toggle('done', fin);
  renderProgressSteps(d.steps || [], d.phase, fin);
}
function renderProgressSteps(steps, active, finished) {
  const box = $('#prg-steps');
  if (!steps.length) { box.innerHTML = ''; return; }
  const order = steps.map(s => s[0]);
  const ai = order.indexOf(active);
  box.innerHTML = steps.map(([k, label], i) => {
    const cls = finished || (ai >= 0 && i < ai) ? 'done' : (i === ai ? 'active' : '');
    return `<span class="prg-step ${cls}"><span class="tick">✓</span>${esc(label)}</span>`;
  }).join('');
}

/* 手动跑一次研判（带实时进度） */
async function runDaily(force, offline) {
  const btn = offline ? $('#btn-daily-offline') : (force ? $('#btn-daily-rerun') : $('#btn-daily-run'));
  if (!btn) return;
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '研判中…';
  const restore = () => { btn.disabled = false; btn.textContent = old; stopProgress(); };
  try {
    const r = await api(`/api/daily/run?force=${force ? 1 : 0}&offline=${offline ? 1 : 0}`,
                        { method: 'POST' });
    if (r.status === 'exists') { toast(r.message || '当日研判已存在'); restore(); return; }
    resetProgress();
    toast(offline ? '正在生成仅客观指标的看板…' : '研判已启动，进度实时显示在下方', 'ok');
    connectDailyStream(r.run_id, restore);
  } catch (e) {
    toast('启动失败：' + e.message, 'err');
    restore();
  }
}
$('#btn-daily-run').addEventListener('click', () => runDaily(false, false));
$('#btn-daily-rerun').addEventListener('click', () => runDaily(true, false));
$('#btn-daily-offline').addEventListener('click', () => runDaily(true, true));

function connectDailyStream(runId, done) {
  const box = $('#daily-timeline');
  box.innerHTML = '';
  const add = (agent, name, text) => {
    box.insertAdjacentHTML('beforeend', `<div class="tl-item ${text ? 'done' : 'active'}">
      <div class="tl-hd">${avatar(agent)}<span class="who">${esc(name)}</span>
        <span class="tm">${new Date().toLocaleTimeString('zh-CN', { hour12: false })}</span></div>
      <div class="tl-bd md-body">${text ? md(text) : '正在研判…'}</div></div>`);
    box.lastElementChild.scrollIntoView({ block: 'nearest' });
  };
  if (state.es) state.es.close();
  const es = new EventSource('/api/stream/' + runId);
  state.es = es;
  es.onmessage = ev => {
    let d; try { d = JSON.parse(ev.data); } catch { return; }
    if (d.type === 'progress') {
      renderProgress(d);
      if (d.phase === 'analysts' && d.sub) $('#prg-detail').textContent = d.detail || '';
    } else if (d.type === 'phase') add('officer', d.label, '');
    else if (d.type === 'agent_done') add(d.agent, d.name, (d.text || '').slice(0, 700));
    else if (d.type === 'error') toast(d.message, 'err');
    else if (d.type === 'done') {
      renderProgress({ ...(state.lastPrg || {}), pct: 100, label: '研判完成',
                       detail: '报告已生成', steps: state.lastSteps || [], phase: 'finalize' });
      toast('研判完成', 'ok');
      es.close(); if (done) done(); loadDaily();
    } else if (d.type === '__end__') { es.close(); if (done) done(); }
  };
  es.onerror = () => { es.close(); if (done) done(); };
}

$('#btn-daily-export').addEventListener('click', () => {
  if (!state.daily) return toast('还没有研判报告', 'err');
  download(`智能多维投资系统_研判_${state.daily.date}.json`,
    JSON.stringify(state.daily, null, 2), 'application/json');
});

/* ═══════════════════════════════════════════════════════════════════════════
   ③ 家庭投资（沿用原审议逻辑，改配色与容器）
   ═══════════════════════════════════════════════════════════════════════════ */
function buildRows() {
  const ar = $('#asset-rows'); ar.innerHTML = '';
  ASSET_KEYS.forEach(([k, label]) => {
    ar.appendChild(el('div', 'frow',
      `<span>${esc(label)}</span><input type="number" data-a="${k}" value="0" step="10000" min="0">`));
  });
  const lr = $('#liability-rows'); lr.innerHTML = '';
  LIABILITY_KEYS.forEach(([k, label]) => {
    lr.appendChild(el('div', 'frow',
      `<span>${esc(label)}</span><input type="number" data-l="${k}" value="0" step="10000" min="0">`));
  });
}
function addGoalRow(g = {}) {
  const box = $('#goal-rows');
  const row = el('div', '');
  row.style.cssText = 'display:grid;grid-template-columns:1.4fr 1fr .7fr 1fr auto;gap:7px;margin-bottom:7px;align-items:center';
  row.innerHTML = `
    <input placeholder="目标名称" data-g="name" value="${esc(g.name || '')}">
    <input type="number" placeholder="金额" data-g="amount" value="${g.amount || 0}" step="50000">
    <input type="number" placeholder="年数" data-g="years" value="${g.years || 5}" step="1">
    <select data-g="priority">
      <option value="critical">关键</option><option value="important">重要</option>
      <option value="optional">可选</option></select>
    <button class="btn sm" title="删除">✕</button>`;
  if (g.priority) row.querySelector('[data-g=priority]').value = g.priority;
  row.querySelector('button').onclick = () => row.remove();
  box.appendChild(row);
}
function collectHousehold() {
  const g = id => $(id).value, gn = id => Number($(id).value) || 0;
  const assets = {}, liabilities = {};
  $$('#asset-rows input').forEach(i => { assets[i.dataset.a] = Number(i.value) || 0; });
  $$('#liability-rows input').forEach(i => { liabilities[i.dataset.l] = Number(i.value) || 0; });
  const goals = $$('#goal-rows > div').map(r => ({
    name: r.querySelector('[data-g=name]').value || '未命名目标',
    amount: Number(r.querySelector('[data-g=amount]').value) || 0,
    years: Number(r.querySelector('[data-g=years]').value) || 0,
    priority: r.querySelector('[data-g=priority]').value, note: '',
  })).filter(x => x.amount > 0 && x.years > 0);
  return {
    name: g('#f-name') || '我的家庭',
    head_age: gn('#f-age'), retirement_age: gn('#f-retire'),
    dependents: gn('#f-dependents'), city_tier: g('#f-city'),
    assets, liabilities,
    mortgage_rate: gn('#f-mortgage-rate') / 100, consumer_rate: gn('#f-consumer-rate') / 100,
    monthly_income: gn('#f-income'), income_stability: gn('#f-stability'),
    monthly_expense: gn('#f-expense'), monthly_mortgage: gn('#f-mortgage-monthly'),
    goals,
    has_medical_insurance: $('#f-medical').checked,
    has_critical_illness: $('#f-critical').checked,
    life_sum_assured: gn('#f-life'),
    risk_score: gn('#f-riskscore'), loss_tolerance_pct: gn('#f-losstol'),
    experience_years: gn('#f-exp'),
  };
}
function fillHousehold(h) {
  const set = (id, v) => { if (v !== undefined && v !== null) $(id).value = v; };
  set('#f-name', h.name); set('#f-age', h.head_age); set('#f-retire', h.retirement_age);
  set('#f-dependents', h.dependents);
  if (h.city_tier) $('#f-city').value = h.city_tier;
  set('#f-income', h.monthly_income); set('#f-expense', h.monthly_expense);
  set('#f-mortgage-monthly', h.monthly_mortgage); set('#f-stability', h.income_stability);
  $$('#asset-rows input').forEach(i => { i.value = (h.assets && h.assets[i.dataset.a]) || 0; });
  $$('#liability-rows input').forEach(i => { i.value = (h.liabilities && h.liabilities[i.dataset.l]) || 0; });
  set('#f-mortgage-rate', ((h.mortgage_rate || 0) * 100).toFixed(2));
  set('#f-consumer-rate', ((h.consumer_rate || 0) * 100).toFixed(2));
  $('#goal-rows').innerHTML = '';
  (h.goals || []).forEach(addGoalRow);
  if (!(h.goals || []).length) addGoalRow();
  $('#f-medical').checked = !!h.has_medical_insurance;
  $('#f-critical').checked = !!h.has_critical_illness;
  set('#f-life', h.life_sum_assured); set('#f-riskscore', h.risk_score);
  set('#f-losstol', h.loss_tolerance_pct); set('#f-exp', h.experience_years);
  schedulePreview();
}

let previewTimer = null;
function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(runPreview, 320); }
async function runPreview() {
  try {
    state.preview = await api('/api/preview', { method: 'POST', body: JSON.stringify(collectHousehold()) });
    renderPreview(state.preview);
  } catch (e) { /* 输入中出错不打扰 */ }
}

function renderPreview(p) {
  const sav = p.monthly_savings;
  const h = $('#cashflow-hint');
  h.className = 'note ' + (sav <= 0 ? 'bad' : sav < 5000 ? 'warn' : 'good');
  h.innerHTML = sav <= 0
    ? `<b>月结余为负（${money(sav)}）。</b>当前收入无法覆盖支出与月供，任何投资方案都无从谈起——应先处理现金流缺口。`
    : `月结余 <b>${money(sav)}</b>（储蓄率 ${pct(p.savings_rate, 0)}），年结余 <b>${money(p.annual_savings)}</b>。`
      + `应急储备覆盖 <b>${num(p.emergency_months, 1)} 个月</b>（目标 ${num(p.emergency_target_months, 1)} 个月）`
      + (p.emergency_gap > 0 ? `，<b style="color:var(--gold-d)">缺口 ${money(p.emergency_gap)}</b>` : '（已达标）') + '。';

  $('#asset-metrics').innerHTML =
    kpi('总资产', money(p.total_assets)) + kpi('总负债', money(p.total_liabilities))
    + kpi('净资产', money(p.net_worth))
    + kpi('负债率', pct(p.leverage, 1), '', p.leverage > 0.5 ? 'down' : p.leverage > 0.3 ? 'gold' : 'up')
    + kpi('可投资资产', money(p.investable_assets), '剔除自住房')
    + kpi('可即时变现', money(p.liquid_assets));

  $('#f-kpis').innerHTML =
    kpi('净资产', money(p.net_worth), p.lifecycle_label)
    + kpi('可投资资产', money(p.investable_assets), '配置的作用对象')
    + kpi('权益上限', pct(p.equity_ceiling, 0), `约束来自${p.constraint_binding}`, 'gold')
    + kpi('风险等级', p.risk_grade_label, `综合 ${p.risk_score.toFixed(0)}/100`)
    + kpi('应急储备', `${num(p.emergency_months, 1)} 月`, `目标 ${num(p.emergency_target_months, 1)} 月`,
        p.emergency_gap > 0 ? 'down' : 'up');

  /* 承受力雷达 */
  const bd = p.capacity_breakdown || [];
  $('#risk-radar').innerHTML = bd.length ? Charts.radar({
    axes: bd.map(x => x.dimension),
    series: [
      { name: '客观承受能力', values: bd.map(x => x.score) },
      { name: '主观风险偏好', values: bd.map(() => p.preference_score) },
    ],
    colors: ['var(--red)', 'var(--gold)'], size: 240, max: 100,
  }) : '';

  const rh = $('#risk-hint');
  const diverge = Math.abs(p.capacity_score - p.preference_score) >= 15;
  rh.className = 'note ' + (diverge ? 'warn' : '');
  rh.innerHTML = `综合风险等级 <b>${esc(p.risk_grade_label)}</b>（综合 ${p.risk_score.toFixed(0)}/100，
    取客观承受能力与主观偏好的较小者，约束来自<b>${esc(p.constraint_binding)}</b>）。`
    + (diverge ? '<br><b>注意：客观承受能力与主观偏好背离较大。</b>预测偏差时以客观承受能力为准。' : '')
    + `<br>据此推出的<b>权益类上限为 ${pct(p.equity_ceiling, 0)}</b>。`
    + `<br><span class="tiny muted">${esc(p.equity_ceiling_reason)}</span>`
    + (p.emergency_gap > 0 ? `<br><b>应急金缺口 ${money(p.emergency_gap)}，应在配置权益之前优先补足。</b>` : '')
    + (p.insurance_gap > 0 ? `<br>测算保障缺口 <b>${money(p.insurance_gap)}</b>，建议用定期寿险/重疾解决。` : '');
  updateCostEst();
}
function updateCostEst() {
  const rounds = Number($('#f-maxrounds').value) || 5;
  const calls = 5 + 2 + (rounds - 1) * 10 + 1 + 2 + 2;
  $('#cost-est').innerHTML = `预计调用约 <b>${calls}</b> 次模型请求（最多 ${rounds} 轮辩论）`;
}
$('#f-maxrounds').addEventListener('change', updateCostEst);
$('#btn-example').addEventListener('click', async () => {
  fillHousehold(await api('/api/example'));
  toast('已载入样例家庭', 'ok');
});
$('#btn-save').addEventListener('click', async () => {
  try {
    const r = await api('/api/household', { method: 'POST', body: JSON.stringify(collectHousehold()) });
    toast('已保存档案：' + r.name, 'ok');
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
});
$('#btn-load').addEventListener('click', async () => {
  const r = await api('/api/household');
  const list = r.profiles || [];
  if (!list.length) return toast('还没有保存过档案', 'err');
  fillHousehold(list[list.length - 1]);
  toast('已载入：' + list[list.length - 1].name, 'ok');
});
$('#btn-add-goal').addEventListener('click', () => addGoalRow());

/* ── 启动审议 ── */
$('#btn-run').addEventListener('click', startRun);
async function startRun() {
  const btn = $('#btn-run');
  btn.disabled = true;
  btn.querySelector('.spin').hidden = false;
  btn.querySelector('.btn-label').textContent = '审议进行中…';
  resetLive();
  try {
    const r = await api('/api/analyze', {
      method: 'POST',
      body: JSON.stringify({
        household: collectHousehold(),
        max_rounds: Number($('#f-maxrounds').value) || 5,
      }),
    });
    state.runId = r.run_id;
    $('#run-note').textContent = `运行编号 ${r.run_id}`;
    setRunStatus('active', '审议中');
    goto('live');
    connectStream(r.run_id);
  } catch (e) {
    toast('启动失败：' + e.message, 'err');
    btn.disabled = false;
    btn.querySelector('.spin').hidden = true;
    btn.querySelector('.btn-label').textContent = '启动审议';
  }
}
function setRunStatus(kind, text) {
  $('#run-status').innerHTML = `<span class="dot ${kind}"></span>${esc(text)}`;
}
function resetLive() {
  state.events = []; state.rounds = {}; state.arguments = []; state.convergence = [];
  state.convSummary = null; state.cards = {}; state.result = null;
  $('#speech-stream').innerHTML = '<div class="muted small">审议开始后，各智能体的发言会陆续出现。</div>';
  $('#console').innerHTML = '';
  $('#stance-matrix').innerHTML = '<div class="muted small">议题抽取完成后显示。</div>';
  $('#conv-hint').textContent = '尚未开始';
  $('#conv-track').textContent = '';
  $('#cv-chart').innerHTML = '';
  buildStepper();
}
const PHASES = [['profile', '画像'], ['round1', '首轮'], ['arguments', '立题'],
                ['round2', '辩论'], ['draft', '草案'], ['grounding', '接地'],
                ['risk', '风控'], ['final', '裁决'], ['qc', '门禁']];
function buildStepper() {
  $('#stepper').innerHTML = PHASES.map(([k, label], i) =>
    `<div class="tl-item" data-phase="${k}"><div class="tl-hd">
      <span class="num" style="color:var(--ink-4);font-size:11px">${String(i + 1).padStart(2, '0')}</span>
      <span class="who" style="font-size:12.5px">${esc(label)}</span></div></div>`).join('');
}
function markPhase(key, label) {
  let hit = false;
  $$('#stepper .tl-item').forEach(st => {
    st.classList.remove('active', 'done');
    if (st.dataset.phase === key) { st.classList.add('active'); hit = true; }
    else if (!hit) st.classList.add('done');
  });
  if (label) $('#live-sub').textContent = label;
}

/* ── SSE ── */
function connectStream(runId) {
  if (state.es) state.es.close();
  const es = new EventSource('/api/stream/' + runId);
  state.es = es;
  es.onmessage = ev => { let d; try { d = JSON.parse(ev.data); } catch { return; } handleEvent(d); };
  es.onerror = () => { es.close(); setRunStatus('closed', '连接结束'); finishRun(); };
}
function handleEvent(d) {
  state.events.push(d);
  switch (d.type) {
    case 'phase': markPhase(d.phase, d.detail || d.label);
      log(`▶ ${d.label}${d.detail ? ' — ' + d.detail : ''}`); break;
    case 'profile': state.preview = d.data; renderPreview(d.data); break;
    case 'market': log(`数据池载入：${(d.catalog || []).reduce((a, g) => a + g.symbols.length, 0)} 个标的`); break;
    case 'agent_start': addSpeechPlaceholder(d.agent, d.name); log(`⋯ ${d.name} 正在思考`); break;
    case 'agent_done': updateSpeech(d); log(`✓ ${d.name} 发言完成（${(d.text || '').length} 字）`, 'ok'); break;
    case 'arguments': state.arguments = d.arguments || []; renderStanceMatrix();
      log(`议题抽取完成：${state.arguments.length} 条配置主张`, 'ok'); break;
    case 'round_start': log(`◆ 第 ${d.round} 轮交叉辩论开始`); break;
    case 'convergence':
      state.convergence.push(d.data);
      state.lastStances = d.data.stance_by_agent || {};
      state.lastWhy = d.data.why || {};
      renderConvergence(d.data); renderStanceMatrix();
      log(`第 ${d.round} 轮：CV=${num(d.data.cv_overall, 4)}，W=${num(d.data.kendall_w, 3)}，`
        + `解析失败率 ${pct(d.data.parse_fail_rate, 0)}`, 'ok');
      if (d.data.termination_reason) log(`终止判定：${d.data.termination_reason}`, 'wn');
      break;
    case 'convergence_summary': state.convSummary = d.data; renderConvSummary(); break;
    case 'draft': log(`委员会草案：${Object.entries(d.weights || {}).map(([k, v]) =>
      (CLASS_SHORT[k] || k) + ' ' + pct(v, 0)).join('、')}`); break;
    case 'proposal_draft': case 'proposal_final':
      state.result = state.result || {};
      state.result.proposal = d.data;
      if (d.committee) state.result.committee_final = d.committee;
      renderPlan(); renderStress();
      log(d.revised ? '配置委员会已按门禁要求修订方案' : '方案已接地', 'ok'); break;
    case 'risk_verdict': log(`风控委员会结论：${d.verdict}`, d.verdict === '否决' ? 'er' : 'wn'); break;
    case 'qc': {
      const r = d.data;
      log(`质量门禁：${r.n_passed}/${r.n_checks} 通过` + (r.passed ? ' ✅'
        : ` ❌ 未通过项 ${r.blockers.map(b => b.id).join(', ')}`), r.passed ? 'ok' : 'er');
      if (state.result) { state.result.qc = r; renderQC(r); } break;
    }
    case 'warning': log('⚠ ' + d.message, 'wn'); toast(d.message, 'err'); break;
    case 'error': log('✗ ' + d.message, 'er'); toast('运行出错：' + d.message, 'err'); break;
    case 'llm_call': if (!d.ok) log(`模型调用失败：${d.label} — ${d.error}`, 'er'); break;
    case 'done':
      state.result = d.data; renderEverything(d.data);
      log(`✔ 审议完成，用时 ${num(d.data.elapsed_sec, 0)} 秒，模型调用 ${d.data.usage.calls} 次`, 'ok'); break;
    case '__end__': finishRun(); break;
  }
}
function finishRun() {
  const btn = $('#btn-run');
  btn.disabled = false;
  btn.querySelector('.spin').hidden = true;
  btn.querySelector('.btn-label').textContent = '启动审议';
  setRunStatus(state.result ? 'open' : 'closed', state.result ? '已完成' : '已结束');
  if (state.result) goto('plan');
}
function log(msg, cls = '') {
  const c = $('#console');
  const t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  c.appendChild(el('div', cls, `<span class="t">${t}</span>  ${esc(msg)}`));
  c.scrollTop = c.scrollHeight;
}
function addSpeechPlaceholder(agent, name) {
  const box = $('#speech-stream');
  const first = box.querySelector('.muted');
  if (first) first.remove();
  let card = state.cards[agent];
  if (!card) {
    card = el('div', 'speech new');
    card.innerHTML = `<div class="speech-hd">${avatar(agent)}<span class="who">${esc(name)}</span>
      <span class="rnd"></span></div>
      <div class="speech-bd"><span class="typing"></span></div>`;
    box.appendChild(card);
    state.cards[agent] = card;
  }
  box.scrollTop = box.scrollHeight;
}
function updateSpeech(d) {
  const box = $('#speech-stream');
  if (!state.cards[d.agent]) addSpeechPlaceholder(d.agent, d.name);
  const card = state.cards[d.agent];
  if (!card) return;
  card.classList.remove('new');
  card.querySelector('.rnd').textContent = `第 ${state.convergence.length + 1} 轮`;
  card.querySelector('.speech-bd').innerHTML = md(d.text || '');
  box.scrollTop = box.scrollHeight;
}

/* ── 收敛图 ── */
function renderConvergence(r) {
  const trackLabel = { converged: '已收敛', composite_deadlock: '高位僵持',
                       soft_deadlock: '分歧冻结', blocked: '信号不可信', continue: '继续' }[r.track] || '';
  $('#conv-track').textContent = trackLabel;
  const hint = $('#conv-hint');
  hint.className = 'note ' + (r.track === 'converged' ? 'good' : r.track === 'continue' ? '' : 'warn');
  hint.innerHTML = `第 <b>${r.round}</b> 轮：分歧度 CV = <b>${num(r.cv_overall, 4)}</b>（收敛线 0.07），`
    + `排序一致性 W = <b>${num(r.kendall_w, 3)}</b>（阈值 0.5），`
    + `非中立占比 ${pct(r.substantive_ratio, 0)}，解析失败率 ${pct(r.parse_fail_rate, 0)}。`
    + (r.termination_reason ? `<br><b>终止判定：</b>${esc(r.termination_reason)}` : '<br>尚未触发终止条件。');
  drawCVChart();
}
function drawCVChart() {
  const data = state.convergence;
  if (!data.length) { $('#cv-chart').innerHTML = ''; return; }
  $('#cv-chart').innerHTML = Charts.lines({
    series: [
      { name: '分歧度 CV', values: data.map(d => d.cv_overall ?? 0) },
      { name: '排序一致性 W', values: data.map(d => d.kendall_w ?? 0) },
    ],
    colors: ['var(--red)', 'var(--gold)'], width: 560, height: 220, showLegend: true,
  });
}

/* ── 立场矩阵 ── */
const STANCE_SCALE = {
  1: { bg: '#0B8A5C', fg: '#fff' }, 2: { bg: '#7FBFA4', fg: '#0F1319' },
  3: { bg: '#EEF0F4', fg: '#4A5261' }, 4: { bg: '#F0A6A6', fg: '#0F1319' },
  5: { bg: '#D92B2B', fg: '#fff' },
};
function renderStanceMatrix() {
  const box = $('#stance-matrix');
  if (!state.arguments.length) return;
  const agents = Object.keys(state.lastStances || {});
  const NAMES = { macro: '宏观', valuation: '定价', lifecycle: '期限', taxfee: '税费',
                  behavioral: '行为', technical: '技术', flow: '资金', risk: '风控' };
  let html = '<table class="tbl"><thead><tr><th>配置主张</th>';
  agents.forEach(a => { html += `<th class="c">${esc(NAMES[a] || a)}</th>`; });
  html += '<th class="c">均分</th></tr></thead><tbody>';
  state.arguments.forEach(a => {
    let sum = 0, cnt = 0, cells = '';
    agents.forEach(ag => {
      const v = (state.lastStances[ag] || {})[a.id];
      const why = ((state.lastWhy[ag] || {})[a.id]) || '';
      if (v != null) { sum += v; cnt++; }
      const sc = STANCE_SCALE[v] || { bg: 'transparent', fg: 'var(--ink-4)' };
      cells += `<td class="c"><span title="${esc(why || a.text)}"
        style="display:inline-grid;place-items:center;width:26px;height:22px;border-radius:4px;
        font-family:var(--font-num);font-weight:600;font-size:12px;
        background:${sc.bg};color:${sc.fg}">${v ?? '·'}</span></td>`;
    });
    html += `<tr><th style="font-weight:400;white-space:normal;max-width:230px"
      title="${esc(a.text)}">${esc(a.text.slice(0, 30))}${a.text.length > 30 ? '…' : ''}</th>`
      + cells + `<td class="c num">${cnt ? (sum / cnt).toFixed(1) : '—'}</td></tr>`;
  });
  html += '</tbody></table><div class="tiny muted" style="margin-top:8px">'
    + '绿＝反对，红＝支持，越分散分歧越大。悬停单元格看该分析师给出的理由。</div>';

  const last = state.convergence[state.convergence.length - 1];
  if (last && ((last.style_collapse || []).length || (last.duplicate_vectors || []).length)) {
    const c = last.style_collapse || [], d = last.duplicate_vectors || [];
    html += `<div class="note warn" style="margin-top:10px"><b>⚠ 量表效度提示（第 ${last.round} 轮）</b><br>`
      + (c.length ? `${c.length} 位分析师对本轮全部主张给出完全相同的分数：`
          + c.map(x => `${esc(NAMES[x.agent] || x.agent)} 全 ${x.constant_score} 分`).join('、') + '。<br>' : '')
      + (d.length ? `打分向量完全相同：${d.map(g => g.map(a => esc(NAMES[a] || a)).join('=')).join('；')}。<br>` : '')
      + `真实判断很难在内容迥异的主张上产生同分，这通常是<b>响应风格坍缩</b>而非真实分歧，收敛指标仅供参考。</div>`;
  }
  box.innerHTML = html;
}

/* ── 方案 ── */
function renderEverything(res) {
  state.result = res;
  renderPlanSummary(res);
  renderPlan(); renderStress(); renderConvSummary();
  renderDisagreement(); renderRiskReview(); renderTranscript(); renderQC(res.qc);
  renderPlanDetail(res.proposal || {});
  renderTeam(res);
  ['plan', 'detail', 'team', 'stress', 'debate'].forEach(v => {
    const e = $('#view-' + v); if (e) e.hidden = v !== state.famTab;
  });
  ['plan', 'stress', 'debate', 'detail', 'team'].forEach(v => {
    const e = $('#' + v + '-empty'); if (e) e.hidden = true;
    const b = $('#' + v + '-body'); if (b) b.hidden = false;
  });
}
function renderPlan() {
  const p = state.result && state.result.proposal;
  if (!p) return;
  $('#plan-empty').hidden = true; $('#plan-body').hidden = false;

  const bar = $('#weight-bar'); bar.innerHTML = '';
  Object.entries(p.weights).forEach(([k, v]) => {
    if (v <= 0.0005) return;
    const s = el('div', 'seg-i');
    s.style.width = (v * 100) + '%';
    s.style.background = CLASS_COLORS[k] || '#64748B';
    s.title = `${CLASS_SHORT[k]} ${pct(v, 1)}`;
    if (v > 0.055) s.innerHTML = `<span>${pct(v, 0)}</span>`;
    bar.appendChild(s);
  });
  const lg = el('div', 'legend');
  Object.entries(p.weights).forEach(([k, v]) => {
    if (v <= 0.0005) return;
    lg.appendChild(el('span', '', `<i style="background:${CLASS_COLORS[k]}"></i>${CLASS_SHORT[k]} ${pct(v, 1)}`));
  });
  bar.parentElement.appendChild(lg);

  const f = p.forward || {}, h = p.historical || {};
  $('#plan-metrics').innerHTML =
    kpi('可投资资产', money(p.investable_assets))
    + kpi('风险资产占比', pct(p.risk_assets_weight, 1), '', p.risk_assets_weight > 0.6 ? 'gold' : '')
    + kpi('前瞻预期年化', pct(f.expected_return, 2), '', 'up')
    + kpi('前瞻预期波动', pct(f.expected_vol, 2))
    + kpi('历史回放年化', h.ok ? pct(h.annual_return, 2) : '—', '', h.ok && h.annual_return > 0 ? 'up' : 'down')
    + kpi('历史最大回撤', h.ok ? pct(h.max_drawdown, 1) : '—', '', 'down');

  /* 图表组：环形、风险收益散点、期限阶梯、情景瀑布、风险预算 */
  renderPlanCharts(p);

  const hz = $('#horizon-layers'); hz.innerHTML = '';
  Object.values(p.horizon_layers || {}).forEach(L => {
    hz.appendChild(el('div', 'frow', `
      <div><div style="font-size:12.8px;font-weight:600">${esc(L.label)}</div>
        <div class="tiny muted">${esc(L.target)}</div></div>
      <div style="text-align:right"><div class="num" style="font-size:15px;font-weight:600;
        color:var(--red)">${pct(L.weight, 1)}</div>
        <div class="tiny muted">${money(L.amount)}</div></div>`));
  });

  const cc = $('#class-cards'); cc.innerHTML = '';
  (p.classes || []).forEach(c => {
    if (c.weight <= 0.0005) return;
    const inst = (c.instruments || []).slice(0, 4).map(i =>
      `· ${esc(i.name)}${i.sub_weight ? ' ' + pct(i.sub_weight, 1) : ''}`).join('<br>');
    cc.appendChild(el('div', 'cls-card', `
      <div class="top"><div>
        <div class="nm">${esc(c.label)}</div>
        <div class="amt">${money(c.amount)}</div></div>
        <div class="w">${pct(c.weight, 1)}</div></div>
      <div class="bars">
        <span class="tag">风险 ${esc(c.risk)}</span>
        <span class="tag">${esc(c.liquidity)}</span>
        <span class="tag">区间 ${pct(c.range[0], 0)}–${pct(c.range[1], 0)}</span>
        ${c.in_range ? '' : '<span class="tag" style="background:var(--gold-l);color:var(--gold-d)">越界</span>'}
      </div>
      ${c.rationale ? `<div class="why md-body">${md(c.rationale)}</div>` : ''}
      ${inst ? `<div class="inst">${inst}</div>` : ''}`));
  });

  const rb = (state.result.committee_final || {}).rebalance || {};
  $('#rebalance-box').innerHTML = rb.rule
    ? `<div class="md-body" style="font-size:12.8px"><b>规则：</b>${md(rb.rule)}</div>
       ${rb.threshold ? `<div class="small muted" style="margin-top:7px">偏离阈值：${pct(rb.threshold, 0)}</div>` : ''}
       ${rb.calendar ? `<div class="small muted">检查频率：${esc(rb.calendar)}</div>` : ''}
       ${rb.note ? `<div class="note md-body" style="margin-top:9px">${md(rb.note)}</div>` : ''}`
    : '<div class="muted small">委员会未给出再平衡规则。</div>';

  const acts = (state.result.committee_final || {}).actions || [];
  $('#actions-box').innerHTML = acts.length ? acts.map(a => `
    <div class="arg"><div class="arg-hd"><span class="pill solid">${esc(a.when || '待办')}</span>
      <b style="font-size:12.8px">${esc(a.what || '')}</b></div>
      ${a.why ? `<div class="md-body" style="font-size:12px;color:var(--ink-2)">${md(a.why)}</div>` : ''}
      ${a.amount ? `<div class="arg-mt">金额：${money(a.amount)}</div>` : ''}</div>`).join('')
    : '<div class="muted small">委员会未给出行动清单。</div>';
}
/* ── 执行总结 ──────────────────────────────────────────────────────────── */
function renderPlanSummary(res) {
  const p = res.proposal || {};
  const c = res.committee_final || {};
  const h = p.historical || {}, f = p.forward || {}, qcd = res.qc || {};
  const nActs = (c.actions || []).length;
  const eqW = (p.weights?.equity_cn || 0) + (p.weights?.equity_global || 0);

  $('#plan-summary-kpis').innerHTML =
    kpi('可投资资产', money(p.investable_assets), `${res.household?.name || ''}`)
    + kpi('权益合计', pct(eqW, 0), '含 A股与海外',
        eqW > (res.household?.equity_ceiling || 1) ? 'gold' : 'up')
    + kpi('预期年化', pct(f.expected_return, 2), '前瞻测算', 'up')
    + kpi('历史最大回撤', h.ok ? pct(h.max_drawdown, 1) : '—', '真实数据回放', 'down')
    + kpi('行动清单', `${nActs} 条`, qcd.passed ? '门禁全过' : '门禁有未通过项',
        qcd.passed ? 'up' : 'gold')
    + kpi('风险等级', res.household?.risk_grade_label || '—',
        `权益上限 ${pct(res.household?.equity_ceiling || 0, 0)}`);

  $('#plan-summary-meta').innerHTML =
    `${(res.generated_at || '').replace('T', ' ').slice(0, 19)}　`
    + `${res.convergence?.rounds || '?'} 轮辩论　耗时 ${num(res.elapsed_sec, 0)}s`;

  // 总结正文：委员会的 summary 优先；没有就用规则拼一段
  let body = c.summary || '';
  if (!body) {
    const top = Object.entries(p.weights || {}).filter(([, v]) => v > 0.0005)
      .sort((a, b) => b[1] - a[1]).slice(0, 4)
      .map(([k, v]) => `${CLASS_SHORT[k] || k} ${pct(v, 0)}`).join('、');
    body = `本方案把 ${money(p.investable_assets)} 的可投资资产按 **${top}** 等大类分散配置。`
      + `在 ${hist_window(p)} 的真实历史回放中，组合年化 ${pct(h.annual_return, 2)}、`
      + `最大回撤 ${pct(h.max_drawdown, 1)}，权益合计 ${pct(eqW, 0)}`
      + `（家庭承受力上限 ${pct(res.household?.equity_ceiling || 0, 0)}）。`
      + `风险资产占比 ${pct(p.risk_assets_weight, 0)}，防御资产 ${pct(p.defensive_assets_weight, 0)}。`;
  }
  const div = (res.convergence || {}).termination_track;
  const note = div === 'converged'
    ? '<div class="note good md-body" style="margin-top:12px">本轮辩论在 CV 与排序一致性双达标后收敛，'
      + '方案采纳的是共识值。</div>'
    : `<div class="note warn md-body" style="margin-top:12px">本轮辩论${div === 'composite_deadlock' ? '出现高位僵持'
        : div === 'soft_deadlock' ? '分歧冻结' : '未能在轮数上限内收敛'} —— `
      + `无法收敛的分歧已由配置委员会逐条裁决，理由见「分歧地图」。`
      + `<b>分歧不是失败，回避分歧才是。</b></div>`;
  $('#plan-summary-text').innerHTML = `<div class="md-body">${md(body)}</div>` + note;
}
function hist_window(p) {
  const h = p.historical || {};
  return h.ok ? `${h.window.start} ~ ${h.window.end}` : '内置数据集';
}

/* ── 方案图表组 ────────────────────────────────────────────────────────── */
function scatterSvg(points, opts = {}) {
  const W = opts.width || 380, H = opts.height || 240;
  const pad = { l: 46, r: 14, t: 14, b: 34 };
  const NS = 'http://www.w3.org/2000/svg';
  const xs = points.map(p => p.x), ys = points.map(p => p.y);
  const xlo = Math.min(0, ...xs), xhi = Math.max(...xs, 0.01);
  const ylo = Math.min(0, ...ys), yhi = Math.max(...ys, 0.01);
  const X = v => pad.l + (W - pad.l - pad.r) * ((v - xlo) / (xhi - xlo || 1));
  const Y = v => pad.t + (H - pad.t - pad.b) * (1 - (v - ylo) / (yhi - ylo || 1));
  const L = [];
  for (let g = 0; g <= 4; g++) {
    const y = pad.t + (H - pad.t - pad.b) * g / 4;
    const v = yhi - (yhi - ylo) * g / 4;
    L.push(`<line x1="${pad.l}" y1="${y}" x2="${W - pad.r}" y2="${y}" stroke="var(--line)"/>`);
    L.push(`<text x="${pad.l - 6}" y="${y + 3.5}" fill="var(--ink-3)" font-size="10"
      text-anchor="end" font-family="var(--font-num)">${(v * 100).toFixed(0)}%</text>`);
  }
  for (let g = 0; g <= 4; g++) {
    const x = pad.l + (W - pad.l - pad.r) * g / 4;
    const v = xlo + (xhi - xlo) * g / 4;
    L.push(`<text x="${x}" y="${H - 12}" fill="var(--ink-3)" font-size="10"
      text-anchor="middle" font-family="var(--font-num)">${(v * 100).toFixed(0)}%</text>`);
  }
  L.push(`<text x="${W / 2}" y="${H - 1}" fill="var(--ink-4)" font-size="10"
    text-anchor="middle">历史年化波动 →</text>`);
  L.push(`<text x="11" y="${H / 2}" fill="var(--ink-4)" font-size="10"
    text-anchor="middle" transform="rotate(-90 11 ${H / 2})">历史年化收益 →</text>`);
  points.forEach(p => {
    const r = p.big ? 7 : 5;
    L.push(`<circle cx="${X(p.x)}" cy="${Y(p.y)}" r="${r}"
      fill="${p.color || 'var(--red)'}" fill-opacity="${p.big ? 1 : 0.75}"
      stroke="${p.big ? 'var(--ink)' : 'none'}" stroke-width="1.5">
      <title>${esc(p.label)}：波动 ${(p.x * 100).toFixed(1)}%　年化 ${(p.y * 100).toFixed(1)}%</title>
    </circle>`);
    if (p.big || p.showLabel) {
      L.push(`<text x="${X(p.x)}" y="${Y(p.y) - r - 4}" fill="var(--ink-2)" font-size="10"
        text-anchor="middle">${esc(p.label)}</text>`);
    }
  });
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
    style="width:100%;height:100%">${L.join('')}</svg>`;
}

function renderPlanCharts(p) {
  const segs = Object.entries(p.weights).filter(([, v]) => v > 0.0005)
    .map(([k, v]) => ({ label: CLASS_SHORT[k], value: v, color: CLASS_COLORS[k] }));
  const el1 = $('#plan-donut');
  if (el1) el1.innerHTML = Charts.donut({
    segments: segs, size: 200, thickness: 28,
    centerLabel: money(p.investable_assets, 0), centerSub: '可投资资产',
  });

  // 风险收益散点：各大类 + 本方案（星号）
  const pts = [];
  (p.classes || []).forEach(c => {
    if (c.weight <= 0.0005) return;
    const inst = (c.instruments || []).filter(i => i.annual_vol_hist != null);
    if (!inst.length) return;
    const vol = inst.reduce((a, i) => a + i.annual_vol_hist, 0) / inst.length;
    const ret = inst.reduce((a, i) => a + (i.annual_return_hist || 0), 0) / inst.length;
    pts.push({ x: vol, y: ret, label: c.label, color: CLASS_COLORS[c.key],
               showLabel: true });
  });
  const h = p.historical || {}, f = p.forward || {};
  if (h.ok) {
    pts.push({ x: h.annual_vol || 0, y: h.annual_return || 0,
               label: '本方案(历史)', color: 'var(--ink)', big: true });
  }
  if (f.expected_vol) {
    pts.push({ x: f.expected_vol, y: f.expected_return,
               label: '本方案(前瞻)', color: 'var(--gold)', big: true });
  }
  const el2 = $('#plan-scatter');
  if (el2) el2.innerHTML = pts.length > 1
    ? scatterSvg(pts, { width: 400, height: 240 })
    : '<div class="empty" style="padding:30px"><div class="small muted">数据不足</div></div>';

  // 期限分层：一条堆叠条
  const hz = Object.values(p.horizon_layers || {});
  const el3 = $('#plan-horizon-chart');
  if (el3) {
    const cols = ['var(--cyan)', 'var(--gold)', 'var(--red)'];
    el3.innerHTML = `<div style="display:flex;height:100%;flex-direction:column;
      justify-content:center;gap:10px">
      <div class="wbar" style="height:30px">${hz.map((L, i) =>
        `<div class="seg-i" style="width:${L.weight * 100}%;background:${cols[i % 3]}">
          <span>${pct(L.weight, 0)}</span></div>`).join('')}</div>
      <div style="display:flex;gap:14px;font-size:11.5px;color:var(--ink-2)">
        ${hz.map((L, i) => `<span><i style="display:inline-block;width:9px;height:9px;
          border-radius:2px;background:${cols[i % 3]};margin-right:5px"></i>${esc(L.label)}</span>`).join('')}
      </div></div>`;
  }

  // 情景损益瀑布：用最差情景下各大类的贡献拆解
  const el4 = $('#plan-waterfall');
  if (el4) {
    const inv = p.investable_assets || 0;
    const worst = (p.stress || []).filter(s => s.covered)
      .sort((a, b) => a.return - b.return)[0];
    const items = (p.classes || []).filter(c => c.weight > 0.0005 && c.weight).map(c => {
      const inst = (c.instruments || []).filter(i => i.max_drawdown_hist != null);
      const dd = inst.length
        ? inst.reduce((a, i) => a + i.max_drawdown_hist, 0) / inst.length : 0;
      return { label: c.label, value: dd * c.weight };
    }).filter(x => Math.abs(x.value) > 1e-6);
    el4.innerHTML = items.length
      ? Charts.waterfall({ items, width: 620, height: 240, totalLabel: '合计' })
      : '<div class="empty" style="padding:30px"><div class="small muted">无数据</div></div>';
  }

  // 风险预算（方案页也放一份）
  const rb = $('#risk-budget-plan');
  if (rb) {
    rb.innerHTML = '';
    (p.risk_budget || []).slice(0, 12).forEach(r => {
      const share = r.risk_share || 0;
      const over = share > r.weight * 1.5;
      rb.appendChild(el('div', '', `
        <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">
          <span>${esc(r.name)}</span>
          <span class="muted num">权重 ${pct(r.weight, 1)} → 风险
            <b style="color:${over ? 'var(--gold-d)' : 'var(--ink)'}">${pct(share, 1)}</b></span></div>
        <div class="tk" style="height:6px"><div class="fl"
          style="width:${Math.min(100, share * 100)}%;background:${
            over ? 'var(--gold)' : (CLASS_COLORS[r.asset_class] || '#64748B')}"></div></div>`));
    });
    rb.appendChild(el('div', 'tiny muted', '权重小但风险贡献大 = 隐藏敞口。'));
  }
}

/* ── 组合明细 ──────────────────────────────────────────────────────────── */
function renderPlanDetail(p) {
  $('#detail-empty').hidden = true; $('#detail-body').hidden = false;
  const risk = {};
  (p.risk_budget || []).forEach(r => { risk[r.code] = r; });
  const inv = p.investable_assets || 0;

  let rows = '', totalAmt = 0, totalWeight = 0;
  (p.classes || []).forEach(c => {
    if (c.weight <= 0.0005) return;
    totalAmt += c.amount; totalWeight += c.weight;
    const insts = c.instruments || [];
    if (!insts.length) {
      rows += `<tr><td><b>${esc(c.label)}</b></td><td class="muted">—</td>
        <td class="r">${pct(c.weight, 1)}</td><td class="r">${money(c.amount)}</td>
        <td class="c">${pct(c.range[0], 0)}–${pct(c.range[1], 0)}</td>
        <td class="r">—</td><td class="r">—</td><td class="r">—</td><td class="r">—</td></tr>`;
      return;
    }
    insts.forEach((it, i) => {
      const r = risk[it.code] || {};
      rows += `<tr>
        <td>${i === 0 ? `<b>${esc(c.label)}</b>` : ''}</td>
        <td>${esc(it.name)}${it.code ? `<span class="tiny muted"> ${esc(it.code)}</span>` : ''}</td>
        <td class="r">${it.sub_weight != null ? pct(it.sub_weight, 1) : '—'}</td>
        <td class="r">${it.amount != null ? money(it.amount) : '—'}</td>
        <td class="c">${i === 0 ? pct(c.range[0], 0) + '–' + pct(c.range[1], 0) : ''}</td>
        <td class="r ${(it.annual_return_hist || 0) > 0 ? 'up' : 'down'}">${
          it.annual_return_hist != null ? pct(it.annual_return_hist, 2) : '—'}</td>
        <td class="r">${it.annual_vol_hist != null ? pct(it.annual_vol_hist, 2) : '—'}</td>
        <td class="r down">${it.max_drawdown_hist != null ? pct(it.max_drawdown_hist, 1) : '—'}</td>
        <td class="r">${r.risk_share != null ? pct(r.risk_share, 1) : '—'}</td></tr>`;
    });
  });

  $('#detail-table').innerHTML = `<table class="tbl"><thead><tr>
      <th>大类</th><th>工具</th><th class="r">权重</th><th class="r">金额</th>
      <th class="c">区间</th><th class="r">历史年化</th><th class="r">历史波动</th>
      <th class="r">最大回撤</th><th class="r">风险贡献</th></tr></thead>
    <tbody>${rows}
      <tr style="background:var(--surface-2);font-weight:600">
        <td colspan="2">合计</td><td class="r">${pct(totalWeight, 1)}</td>
        <td class="r">${money(totalAmt)}</td><td colspan="5"></td></tr>
    </tbody></table>`;
  $('#detail-meta').textContent =
    `${(p.classes || []).filter(c => c.weight > 0.0005).length} 个大类`;
  $('#detail-notes').innerHTML = `
    <div class="note md-body">${md(p.backtest_basis || '')}</div>
    <div class="note warn" style="margin-top:10px">
      历史上限与波动均来自内置数据集的真实回放（${hist_window(p)}）；
      类内等权是**确定性**结果，不是最优化组合。
      风险贡献为边际风险贡献（MCTR），权重小但贡献大的是隐藏敞口。
    </div>`;

  // 各工具金额条形
  const items = [];
  (p.classes || []).forEach(c => {
    (c.instruments || []).forEach(it => {
      if (it.amount) items.push({ label: `${CLASS_SHORT[c.key] || c.label}·${it.name}`,
                                  value: it.amount / (inv || 1) });
    });
  });
  items.sort((a, b) => b.value - a.value);
  $('#detail-bars').innerHTML = items.length
    ? Charts.barRows({ items: items.slice(0, 16), width: 520, unit: '' })
    : '<div class="empty" style="padding:30px"><div class="small muted">无数据</div></div>';
}

/* ── 团队分工 ──────────────────────────────────────────────────────────── */
const TEAM_ROLES = {
  macro: '判断宏观与利率环境，给出大类方向性倾斜',
  valuation: '用历史统计给出前瞻收益与分散化评估',
  lifecycle: '从现金流与期限结构判断配置的期限匹配',
  taxfee: '关注账户层级、税费与费率，算净收益',
  behavioral: '校准配置与家庭真实承受力，有权否决比例',
};
function renderTeam(res) {
  $('#team-empty').hidden = true; $('#team-body').hidden = false;
  const conv = res.convergence || {};
  const speeches = res.speeches || {};
  const rounds = Object.keys(speeches).sort((a, b) => Number(a) - Number(b));
  const last = speeches[rounds[rounds.length - 1]] || {};
  const args = conv.arguments || [];
  const dmap = {};
  (conv.disagreement_map || []).forEach(r => { dmap[r.id] = r; });

  // ① 组织结构
  const ORDER = ['macro', 'valuation', 'lifecycle', 'taxfee', 'behavioral'];
  $('#team-flow').innerHTML = `<div class="flow">
      <div class="flow-node on">
        <div class="fn-t">分析师团队<span class="fn-n">5</span></div>
        <div class="fn-d">五路独立发言，各自基于同一份家庭事实基础</div></div>
      <div class="flow-arrow">→</div>
      <div class="flow-node on">
        <div class="fn-t">研究员辩论<span class="fn-n">${conv.rounds || 0} 轮</span></div>
        <div class="fn-d">看到他人意见后修正判断，逐条表态；CV 与 W 量化收敛</div></div>
      <div class="flow-arrow">→</div>
      <div class="flow-node gold">
        <div class="fn-t">风控委员会<span class="fn-n">1</span></div>
        <div class="fn-d">对抗性审查：压力测试金额化、集中度、流动性、杠杆</div></div>
      <div class="flow-arrow">→</div>
      <div class="flow-node">
        <div class="fn-t">配置委员会<span class="fn-n">1</span></div>
        <div class="fn-d">逐条裁决未收敛分歧，产出最终权重</div></div>
      <div class="flow-arrow">→</div>
      <div class="flow-node">
        <div class="fn-t">质量门禁<span class="fn-n">10</span></div>
        <div class="fn-d">确定性硬校验；不过则退回修订或走兜底</div></div>
    </div>`;
  $('#team-analyst-meta').textContent =
    `共 ${ORDER.length} 位　${conv.rounds || 0} 轮　最终 CV ${num(conv.final_cv, 4)}`;

  // ② 分析师卡片：角色 + 发言摘录 + 立场条
  $('#team-analysts').innerHTML = ORDER.map(a => {
    const sp = last[a] || {};
    const txt = (sp.text || '').replace(/^#+\s*/gm, '').trim();
    const stances = args.map(g => {
      const v = (dmap[g.id]?.stance_by_agent || {})[a];
      return { id: g.id, v };
    });
    const bars = stances.map(s => {
      const c = STANCE_SCALE[s.v] || { bg: 'var(--surface-3)' };
      return `<i style="background:${c.bg}" title="${esc(s.id)}：${s.v ?? '—'}"></i>`;
    }).join('');
    return `<div class="agent-card">
      <div class="ac-h">${avatar(a)}<span class="ac-n">${esc(sp.name || a)}</span>
        <span class="ac-num">${(sp.text || '').length} 字</span></div>
      <div class="ac-b">${mdp(txt.slice(0, 220))}…</div>
      <div class="mini-stance" title="对该轮每条议题的最终立场">${bars}</div>
      <div class="ac-f">${esc(TEAM_ROLES[a] || '')}</div></div>`;
  }).join('');

  // ③ 分组讨论矩阵：行=议题，列=分析师
  const NAMES = { macro: '宏观', valuation: '定价', lifecycle: '期限',
                  taxfee: '税费', behavioral: '行为', risk: '风控', committee: '裁决' };
  let m = '<table class="tbl"><thead><tr><th>配置主张</th>'
    + ORDER.map(a => `<th class="c">${esc(NAMES[a] || a)}</th>`).join('') + '<th class="c">均分</th></tr></thead><tbody>';
  args.forEach(g => {
    const row = dmap[g.id] || {};
    let sum = 0, n = 0;
    const cells = ORDER.map(a => {
      const v = (row.stance_by_agent || {})[a];
      if (v != null) { sum += v; n++; }
      const c = STANCE_SCALE[v] || { bg: 'transparent', fg: 'var(--ink-4)' };
      return `<td class="c"><span style="display:inline-grid;place-items:center;width:26px;
        height:22px;border-radius:4px;font-family:var(--font-num);font-weight:600;font-size:12px;
        background:${c.bg};color:${c.fg}">${v ?? '·'}</span></td>`;
    }).join('');
    m += `<tr><th style="font-weight:400;white-space:normal;max-width:260px">${esc(g.text.slice(0, 42))}${
      g.text.length > 42 ? '…' : ''}</th>${cells}<td class="c num">${n ? (sum / n).toFixed(1) : '—'}</td></tr>`;
  });
  m += '</tbody></table><div class="tiny muted" style="margin-top:8px">'
    + '绿＝反对，红＝支持，灰＝中立。同一行颜色越分散，该议题分歧越大。</div>';
  $('#team-matrix').innerHTML = m;

  // ④ 立场稳定性：跨轮打分的变化幅度
  // 注意 round_results 在**结果对象的顶层**，不在 convergence 里 —— 早先写成
  // res.convergence.round_results，于是这个面板永远是空的（且不报错）。
  const perRound = {};
  (res.round_results || []).forEach(r => { perRound[r.round] = r.stance_by_agent || {}; });
  const roundNums = Object.keys(perRound).map(Number).sort((a, b) => a - b);
  if (!roundNums.length) {
    $('#team-stability').innerHTML =
      '<div class="empty" style="padding:24px"><div class="small">'
      + '本次审议只跑了 1 轮（或结果由旧版本生成），没有跨轮比较数据</div></div>';
  } else {
  $('#team-stability').innerHTML = ORDER.map(a => {
    const series = roundNums.map(r => {
      const s = perRound[r][a] || {};
      const vals = args.map(g => s[g.id]).filter(v => v != null);
      return vals.length ? vals.reduce((x, y) => x + y, 0) / vals.length : null;
    });
    const first = series.find(v => v != null);
    const lastV = [...series].reverse().find(v => v != null);
    const drift = (first != null && lastV != null) ? lastV - first : 0;
    const swings = series.filter(v => v != null).reduce((acc, v, i, arr) =>
      acc + (i ? Math.abs(v - arr[i - 1]) : 0), 0);
    // 条宽按 |漂移| 映射：0.5 分漂移填满半条（放大 2 倍，否则微小变化看不出来）
    const w = Math.min(96, Math.abs(drift) * 96);
    const col = Math.abs(drift) < 0.15 ? 'var(--down)'
      : Math.abs(drift) < 0.5 ? 'var(--gold)' : 'var(--red)';
    const kind = Math.abs(drift) < 0.15 ? '稳定'
      : Math.abs(drift) < 0.5 ? '小幅调整' : '明显转向';
    return `<div class="stab-row">
      <span class="nm">${avatar(a)}${esc(NAMES[a] || a)}</span>
      <span class="stab-track">
        <span style="position:absolute;top:0;bottom:0;left:50%;width:1px;
          background:var(--line-2)"></span>
        <span class="stab-seg" style="width:${w}%;background:${col};
          ${drift < 0 ? `right:50%;` : `left:50%;`}"></span></span>
      <span class="vv">${first != null ? first.toFixed(1) : '—'} → ${
        lastV != null ? lastV.toFixed(1) : '—'}　<span style="color:${col}">${kind}</span></span></div>`;
  }).join('') + `<div class="tiny muted" style="margin-top:9px">
      均分从首轮到末轮的变化。变化小＝立场稳定；反复摇摆＝该角色的判断不稳定，
      它的打分对收敛指标贡献的是噪声而不是信号。</div>`;
  }

  // ⑤ 风控与委员会
  const rv = res.risk_review || {};
  const rcls = rv.verdict === '否决' ? 'bad' : rv.verdict === '有条件通过' ? 'warn' : 'good';
  $('#team-risk').innerHTML =
    `<div class="note ${rcls}" style="margin-bottom:12px"><b>结论：${esc(rv.verdict || '—')}</b></div>
     <div class="md-body" style="max-height:360px;overflow-y:auto">${md(rv.text || '（无风控意见）')}</div>`;

  const c = res.committee_final || {};
  const rulings = c.deadlock_rulings || [];
  $('#team-committee').innerHTML =
    (c.summary ? `<div class="md-body" style="margin-bottom:12px">${md(c.summary)}</div>` : '')
    + (rulings.length
      ? `<div class="small muted" style="margin-bottom:7px">逐条裁决（${rulings.length} 条）</div>`
        + rulings.map(r => `<div class="ruling md-body" style="margin-bottom:7px">
            <b>${esc(r.argument_id || '')}　${esc(r.ruling || '')}</b><br>${md(r.reason || '')}</div>`).join('')
      : '<div class="note">本次没有需要裁决的未收敛主张。</div>');
}

function renderQC(r) {
  if (!r) return;
  const box = $('#qc-box'); box.innerHTML = '';
  box.appendChild(el('div', 'note ' + (r.passed ? 'good' : 'bad'), r.passed
    ? `<b>全部硬门禁通过</b>（${r.n_passed}/${r.n_checks} 项通过，${r.warnings.length} 项软提醒）。方案可交付。`
    : `<b>存在未通过的硬门禁</b>：${r.blockers.map(b => b.id + ' ' + b.name).join('、')}`));
  const list = el('div', '', ''); list.style.marginTop = '10px';
  (r.checks || []).forEach(c => {
    const cls = c.passed ? '' : (c.severity === 'hard' ? 'fail' : 'warn');
    const ic = c.passed ? ['ok', '✓'] : (c.severity === 'hard' ? ['no', '✕'] : ['wn', '!']);
    list.appendChild(el('div', 'qc ' + cls, `
      <span class="ic ${ic[0]}">${ic[1]}</span>
      <div><div class="nm">${esc(c.id)} · ${esc(c.name)}</div>
      <div class="dt">${esc(c.detail)}</div></div>`));
  });
  box.appendChild(list);
}
function renderStress() {
  const p = state.result && state.result.proposal;
  if (!p) return;
  $('#stress-empty').hidden = true; $('#stress-body').hidden = false;

  const curve = p.historical_curve || [], dates = p.historical_curve_dates || [];
  $('#equity-chart').innerHTML = curve.length > 2
    ? Charts.lines({ series: [{ name: '组合净值', values: curve, dates }], colors: ['var(--red)'],
                     width: 760, height: 240, area: true, showAxis: true })
    : '<div class="muted small" style="padding:20px">暂无</div>';
  $('#curve-sub').textContent = p.historical?.ok
    ? `${p.historical.window.start} ~ ${p.historical.window.end}` : '';

  const dd = [];
  let peak = -Infinity;
  curve.forEach(v => { peak = Math.max(peak, v); dd.push(v / peak - 1); });
  $('#dd-chart').innerHTML = dd.length > 2
    ? Charts.drawdown({ values: dd, dates, width: 760, height: 240 })
    : '<div class="muted small" style="padding:20px">暂无</div>';

  const sl = $('#scenario-list'); sl.innerHTML = '';
  const inv = p.investable_assets || 0;
  const maxAbs = Math.max(0.01, ...(p.stress || []).map(s => Math.abs(s.return || 0)));
  (p.stress || []).forEach(s => {
    if (!s.covered) {
      sl.appendChild(el('div', 'scn', `<div><div class="nm">${esc(s.name)}</div>
        <div class="dt">数据窗口未覆盖</div></div><div></div><div class="vl">—</div>`));
      return;
    }
    const neg = s.return < 0;
    sl.appendChild(el('div', 'scn', `
      <div><div class="nm">${esc(s.name)}</div>
        <div class="dt">${esc(s.start)} ~ ${esc(s.end)}（${s.days} 日）</div>
        <div class="tiny muted" style="margin-top:3px">${esc(s.note || '')}</div></div>
      <div class="tk"><div class="fl" style="width:${Math.min(100, Math.abs(s.return) / maxAbs * 100)}%;
        background:${neg ? 'var(--down)' : 'var(--up)'}"></div></div>
      <div class="vl ${neg ? 'down' : 'up'}">${signed(s.return)}
        <small>≈ ${money(s.return * inv)}｜回撤 ${pct(s.max_drawdown, 1)}</small></div>`));
  });

  const rb = $('#risk-budget'); rb.innerHTML = '';
  (p.risk_budget || []).slice(0, 14).forEach(r => {
    const share = r.risk_share || 0;
    const over = share > r.weight * 1.5;
    rb.appendChild(el('div', '', `
      <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">
        <span>${esc(r.name)}</span>
        <span class="muted num">权重 ${pct(r.weight, 1)} → 风险
          <b style="color:${over ? 'var(--gold-d)' : 'var(--ink)'}">${pct(share, 1)}</b></span></div>
      <div class="tk" style="height:6px"><div class="fl" style="width:${Math.min(100, share * 100)}%;
        background:${over ? 'var(--gold)' : (CLASS_COLORS[r.asset_class] || '#64748B')}"></div></div>`));
  });
  rb.appendChild(el('div', 'tiny muted', '权重小但风险贡献大 = 隐藏敞口。'));

  const yr = (p.historical || {}).yearly_returns || {};
  const ys = Object.entries(yr).sort((a, b) => a[0].localeCompare(b[0]));
  $('#yearly-chart').innerHTML = ys.length
    ? Charts.barRows({ items: ys.map(([y, v]) => ({ label: y + ' 年', value: v })), width: 420 })
    : '<div class="muted small">无数据</div>';
}
function renderConvSummary() {
  const s = state.convSummary || (state.result && state.result.convergence);
  if (!s) return;
  const trackLabels = {
    converged: '已收敛（CV 与排序一致性双达标）',
    composite_deadlock: '高位僵持（多数主张无收敛动作且分歧仍高）',
    soft_deadlock: '连续平台（分歧冻结）',
    unresolved: '达到轮数上限仍未收敛',
    blocked: '信号不可信（表态解析失败率过高）',
  };
  $('#conv-summary').innerHTML = `<div class="kpis">
      ${kpi('辩论轮数', s.rounds)}
      ${kpi('最终分歧度 CV', num(s.final_cv, 4), '收敛线 0.07', s.final_cv < 0.07 ? 'up' : 'gold')}
      ${kpi('排序一致性 W', num(s.final_w, 3), '阈值 0.5', s.final_w > 0.5 ? 'up' : '')}
      ${kpi('可辩论主张', (s.arguments || []).length)}
    </div>
    <div class="note ${s.termination_track === 'converged' ? 'good' : 'warn'}" style="margin-top:12px">
      <b>终止轨道：</b>${esc(trackLabels[s.termination_track] || s.termination_track || '—')}<br>
      <b>终止原因：</b>${esc(s.termination_reason || '—')}<br>
      <span class="tiny">阈值取自 consensus-pipeline v2：ε1=${s.thresholds.eps1_converged}、
      ε2=${s.thresholds.eps2_flat}、W ${s.thresholds.w_threshold}、解析失败率门
      ${pct(s.thresholds.max_parse_fail_rate, 0)}。分歧不是失败——无法收敛的分歧会被显式上交裁决。</span>
    </div>`;
}
function renderDisagreement() {
  const s = state.convSummary || (state.result && state.result.convergence);
  if (!s) return;
  const box = $('#disagreement-list'); box.innerHTML = '';
  const rulings = {};
  ((state.result.committee_final || {}).deadlock_rulings || []).forEach(r => {
    if (r && r.argument_id) rulings[String(r.argument_id).trim()] = r;
  });
  const label = { converged: '已收敛', partial: '部分收敛', deadlocked: '高位僵持', unresolved: '未决' };
  const cls = { converged: 'chip-down', partial: 'chip-flat', deadlocked: 'chip-up', unresolved: 'chip-flat' };
  (s.disagreement_map || []).forEach(row => {
    const r = rulings[row.id];
    const stances = Object.entries(row.stance_by_agent || {}).filter(([, v]) => v !== null)
      .map(([k, v]) => `${k}=${v}`).join('  ');
    box.appendChild(el('div', 'arg', `
      <div class="arg-hd"><span class="id">${esc(row.id)}</span>
        <span class="pill ${cls[row.state] || ''}">${label[row.state] || row.state}</span>
        <span class="tiny muted num">CV ${num(row.final_cv, 4)}　均分 ${num(row.mean, 2)}</span></div>
      <div class="arg-tx">${esc(row.text)}</div>
      <div class="arg-mt">各分析师打分：${esc(stances || '—')}</div>
      ${r ? `<div class="ruling md-body"><b>委员会裁决（${esc(r.ruling || '')}）：</b>${md(r.reason || '')}</div>`
          : (row.state !== 'converged'
             ? '<div class="ruling pending"><b>缺少委员会的显式裁决</b> —— 这是 QC-9 的失败项。</div>' : '')}`));
  });
}
function renderRiskReview() {
  const rv = (state.result || {}).risk_review || {};
  const v = rv.verdict || '';
  const cls = v === '否决' ? 'bad' : v === '有条件通过' ? 'warn' : 'good';
  const outstanding = (state.result || {}).risk_veto_outstanding;
  let extra = '';
  if (outstanding && rv.applies_to_current_plan === false) {
    extra = `<br><b>该否决针对的是上一版方案。</b>当前方案已按否决意见重新生成，旧否决在形式上不再适用，
      但风控的核心关切仍然有效——请以原文为准。`;
  } else if (outstanding) { extra = `<br><b>风控委员会否决了当前方案。</b>`; }
  $('#risk-review').innerHTML =
    `<div class="note ${cls}" style="margin-bottom:12px"><b>风控结论：${esc(v)}</b>${extra}</div>`
    + md(rv.text || '（无风控意见）');
}
function renderTranscript() {
  const sp = (state.result || {}).speeches || {};
  const rounds = Object.keys(sp).sort((a, b) => Number(a) - Number(b));
  const tabs = $('#round-tabs'); tabs.innerHTML = '';
  rounds.forEach((r, i) => {
    const b = el('button', i === rounds.length - 1 ? 'active' : '', '第 ' + r + ' 轮');
    b.onclick = () => {
      $$('#round-tabs button').forEach(x => x.classList.remove('active'));
      b.classList.add('active'); showRound(sp[r]);
    };
    tabs.appendChild(b);
  });
  if (rounds.length) showRound(sp[rounds[rounds.length - 1]]);
}
function showRound(o) {
  const box = $('#transcript'); box.innerHTML = '';
  Object.entries(o || {}).forEach(([k, v]) => {
    box.appendChild(el('div', 'speech', `
      <div class="speech-hd">${avatar(k)}<span class="who">${esc(v.name || k)}</span>
        <span class="tiny muted">${esc(v.role || '')}</span></div>
      <div class="speech-bd md-body">${md(v.text || '')}</div>`));
  });
}

/* ═══════════════════════════════════════════════════════════════════════════
   ④ 决策记录 / ⑤ 连接
   ═══════════════════════════════════════════════════════════════════════════ */
async function loadRecords() {
  await loadDaily();
  try {
    const r = await api('/api/runs');
    const box = $('#runs-list'); box.innerHTML = '';
    $('#rec-family-meta').textContent = `${r.runs.length} 次`;
    if (!r.runs.length) {
      box.innerHTML = '<div class="empty" style="padding:32px"><div class="small">暂无记录</div></div>';
      return;
    }
    r.runs.forEach(run => {
      const item = el('div', 'run-row');
      item.innerHTML = `
        <div><div class="nm">${esc(run.household_name || '未命名')}</div>
          <div class="mt">${esc((run.generated_at || '').replace('T', ' ').slice(0, 19))}
            ｜${run.rounds || '?'} 轮｜CV ${num(run.final_cv, 4)}｜
            ${run.qc_passed ? '门禁通过' : '门禁有未通过项'}</div></div>
        <div class="num ${(run.expected_return || 0) > 0 ? 'up' : 'down'}" style="font-size:14px">
          ${pct(run.expected_return, 2)}<div class="tiny muted" style="font-weight:400">预期年化</div></div>`;
      item.onclick = async () => {
        const res = await api('/api/runs/' + encodeURIComponent(run.run_id));
        renderEverything(res); goto('plan');
        toast('已载入 ' + run.run_id, 'ok');
      };
      box.appendChild(item);
    });
  } catch (e) { toast('记录读取失败：' + e.message, 'err'); }
}
$('#btn-reload-records').addEventListener('click', loadRecords);

async function loadConnect() {
  try {
    const h = await api('/api/health');
    $('#lan-url').textContent = h.lan_url;
    $('#qr-img').src = '/api/qrcode?ts=' + Date.now();
    $('#llm-dot').className = 'dot ' + (h.llm.available ? 'open' : 'bad');
    $('#llm-text').textContent = h.llm.available ? h.llm.model : '未配置 Key';
    const hint = $('#connect-hint');
    hint.className = 'note';
    hint.innerHTML = `局域网地址：<b>${esc(h.lan_url)}</b>。若手机打不开，请检查
      ① 手机与电脑是否同一 WiFi；② Windows 防火墙是否允许 Python 访问专用网络。`;
    const st = await api('/api/daily/status').catch(() => null);
    const mk = await api('/api/market').catch(() => null);
    $('#sys-status').innerHTML =
      kpi('应用版本', h.version) + kpi('模型后端', h.llm.available ? '可用' : '未配置', h.llm.model)
      + kpi('内置数据集', `${h.dataset.n_symbols} 个标的`, (h.dataset.fetched_at || '').slice(0, 10))
      + kpi('市场状态', mk?.market?.label || '—')
      + kpi('研判调度', st?.running ? '运行中' : '未启动', `每晚 ${st?.at || '20:30'}`)
      + kpi('研判报告', `${st?.n_reports ?? 0} 份`);
  } catch (e) { toast('连接信息读取失败：' + e.message, 'err'); }
}

function download(name, text, mime) {
  const b = new Blob([text], { type: mime + ';charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(b); a.download = name;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 800);
  toast('已导出 ' + name, 'ok');
}

/* ═══════════════════════════════════════════════════════════════════════════
   启动
   ═══════════════════════════════════════════════════════════════════════════ */
async function boot() {
  buildRows(); addGoalRow(); buildStepper(); updateCostEst();
  document.addEventListener('input', e => {
    if (e.target.closest('#view-family')) schedulePreview();
  });

  try {
    const h = await api('/api/health');
    runPreview();
  } catch (e) {
    $('#llm-dot').className = 'dot bad';
    $('#llm-text').textContent = '后端未连接';
  }
  // 配置状态与首次引导交给 loadSettings —— 它会负责状态灯与首启弹窗，
  // 避免这里和它各写一份状态灯逻辑（两份迟早不一致）。
  loadSettings();

  // ?run= 载入某次家庭审议；?date= 载入某天研判
  const q = new URLSearchParams(location.search);
  if (q.get('run')) {
    try { renderEverything(await api('/api/runs/' + encodeURIComponent(q.get('run')))); }
    catch (e) { toast('载入失败：' + e.message, 'err'); }
  }

  const hash = location.hash.slice(1);
  goto(TOP_VIEWS.includes(hash) || FAMILY_VIEWS.includes(hash) ? hash : 'board');

  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
}
boot();
