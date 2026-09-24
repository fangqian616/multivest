/*! charts.js —— 本地金融看板的零依赖 SVG 图表库（普通 script，非 ES module）
 * ───────────────────────────────────────────────────────────────────────────
 * 用法：  el.innerHTML = Charts.candles({ bars, prevClose: null })
 *
 * 设计约定
 *   · 每个函数「返回 SVG 字符串」，绝不创建 DOM 节点，也绝不触碰 document。
 *   · 颜色一律走设计系统令牌（var(--xxx)），库内不出现任何十六进制颜色。
 *     本库依赖的令牌：--ink --ink-2 --ink-3 --ink-4 --line --line-2 --surface
 *     --surface-2 --red --green --gold --blue --navy --s1..--s8 --font-num --font-sans
 *   · 所有画布都用 viewBox + width:100%/height:100%，随容器自适应，不写死像素。
 *   · 中国市场惯例：红涨绿跌（与欧美相反），全库统一。
 *   · 参数里的 width/height 是「坐标系尺寸」（viewBox 的宽高），不是渲染尺寸。
 *
 * 坐标映射思路（每个函数都在注释里单独说明）
 *   统一三步：① 把入参规范化成纯数字点集（null/NaN 一律剔除或断开）；
 *   ② 用 linear(domain, range) 建一条线性映射；③ 拼 SVG 字符串。
 *   SVG 的 y 轴向下，所以所有数值轴的 range 都是 [底, 顶]（倒着写）。
 * ─────────────────────────────────────────────────────────────────────────── */
(function () {
  'use strict';

  /* ══════════════════════ 0. 基础设施 ══════════════════════ */

  var UID = 0;
  // 渐变 / clipPath 必须有全局唯一 id，否则同页多个图表会互相串色
  function uid(p) { UID += 1; return (p || 'c') + UID.toString(36); }

  /* 令牌容错 ──────────────────────────────────────────────────────────────
     各函数签名里的颜色默认值一律写作 var(--xxx)（照设计系统令牌来，不硬编码颜色）。
     但若宿主主题还没定义某个令牌，var() 会在「计算值阶段」失效，fill 会退化成黑 ——
     对「红涨绿跌」的金融图是致命的（阴线全黑）。这里只给「容易与旧主题重名」的令牌
     补一层同名回退，调用方无需改动；主题补齐令牌后回退自动失效，取值与设计系统一致：
       --green        → 旧主题叫 --down
       --s1..--s8     → 旧主题只有 --red/--gold/--navy/--blue/--violet/--orange/--cyan 语义色 */
  var FALLBACK = {
    'var(--green)': 'var(--green, var(--down))',
    'var(--s1)': 'var(--s1, var(--red))',
    'var(--s2)': 'var(--s2, var(--gold))',
    'var(--s3)': 'var(--s3, var(--navy))',
    'var(--s4)': 'var(--s4, var(--blue))',
    'var(--s5)': 'var(--s5, var(--green, var(--down)))',
    'var(--s6)': 'var(--s6, var(--violet))',
    'var(--s7)': 'var(--s7, var(--orange))',
    'var(--s8)': 'var(--s8, var(--cyan))'
  };
  function tok(c) { return (typeof c === 'string' && FALLBACK[c]) ? FALLBACK[c] : c; }
  function tokList(a) { return Array.isArray(a) ? a.map(tok) : a; }

  // 设计系统令牌
  var C = {
    ink: 'var(--ink)', ink2: 'var(--ink-2)', ink3: 'var(--ink-3)', ink4: 'var(--ink-4)',
    line: 'var(--line)', line2: 'var(--line-2)',
    surface: 'var(--surface)', surface2: 'var(--surface-2)',
    red: 'var(--red)', green: tok('var(--green)'), gold: 'var(--gold)', blue: 'var(--blue)', navy: 'var(--navy)',
    num: 'var(--font-num)', sans: 'var(--font-sans)'
  };
  // 多序列分类调色（按顺序取用）
  var PAL = ['var(--s1)', 'var(--s2)', 'var(--s3)', 'var(--s4)',
    'var(--s5)', 'var(--s6)', 'var(--s7)', 'var(--s8)'].map(tok);

  // 数值容错：非有限数、空串一律视作「无数据」
  function fin(v) {
    if (typeof v === 'number') return isFinite(v) ? v : null;
    if (typeof v === 'string' && v.trim() !== '') { var n = Number(v); return isFinite(n) ? n : null; }
    return null;
  }
  // 尺寸：必须为正，否则退回默认值
  function pos(v, d) { var n = fin(v); return (n === null || n <= 0) ? d : n; }
  function r2(n) { return Math.round(n * 100) / 100; }          // 坐标留 2 位，压缩体积
  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // 粗略估宽：CJK 记 1em，其余 0.55em。只用于防溢出/截断，不需要精确。
  function textW(s, size) {
    var t = String(s === null || s === undefined ? '' : s), w = 0, i, c;
    for (i = 0; i < t.length; i++) { c = t.charCodeAt(i); w += (c > 0x2e80) ? 1 : 0.55; }
    return w * size;
  }
  function clipText(s, maxW, size) {
    var t = String(s === null || s === undefined ? '' : s);
    if (maxW <= 0) return '';
    if (textW(t, size) <= maxW) return t;
    var out = '';
    for (var i = 0; i < t.length; i++) {
      if (textW(out + t[i] + '…', size) > maxW) break;
      out += t[i];
    }
    return out ? out + '…' : '';
  }

  /* 数字格式 */
  function decFor(step) {                        // 让刻度标签刚好能表达 step（2.5 → 1 位）
    for (var d = 0; d <= 6; d++) {
      var s = step * Math.pow(10, d);
      if (Math.abs(s - Math.round(s)) < 1e-9) return d;
    }
    return 2;
  }
  function fmtTick(v, step) { return Number(v).toFixed(decFor(step)); }
  function fmtSigned(v, d) {                     // 带正号，避免 "-0.00"
    d = (d === null || d === undefined) ? 2 : d;
    if (Math.abs(v) < Math.pow(10, -d) / 2) return (0).toFixed(d);
    return (v > 0 ? '+' : '') + Number(v).toFixed(d);
  }
  function fmtPct(v, d) {                        // v 是小数（0.0123 → +1.23%）
    var n = fin(v); if (n === null) return '—';
    return fmtSigned(n * 100, d === undefined ? 2 : d) + '%';
  }
  function fmtPrice(v) {                         // 价格：常规 2 位，小于 1 给 3 位
    var a = Math.abs(v);
    return Number(v).toFixed(a >= 1 ? 2 : 3);
  }
  function fmtVol(v) {
    if (!isFinite(v)) return '';
    if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(2) + '亿';
    if (Math.abs(v) >= 1e4) return (v / 1e4).toFixed(1) + '万';
    return String(Math.round(v));
  }
  function shortDate(d) {                        // 2026-01-02 / 20260102 → 01-02
    var s = String(d === null || d === undefined ? '' : d);
    var m = s.match(/^(\d{4})[-/]?(\d{2})[-/]?(\d{2})/);
    return m ? (m[2] + '-' + m[3]) : s;
  }

  /* 刻度：先求 1/2/2.5/5/10 的「好看步长」，再从 lo 上方第一个整数倍开始铺 */
  function ticks(lo, hi, count) {
    var out = [];
    if (!isFinite(lo) || !isFinite(hi) || hi < lo) return out;
    if (hi === lo) return [lo];
    var raw = (hi - lo) / Math.max(1, count);
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var nrm = raw / mag;
    var step = (nrm <= 1 ? 1 : nrm <= 2 ? 2 : nrm <= 2.5 ? 2.5 : nrm <= 5 ? 5 : 10) * mag;
    var i = Math.ceil(lo / step - 1e-9);
    for (; i * step <= hi + 1e-9 && out.length < 30; i++) out.push(+(i * step).toPrecision(12));
    return out;
  }
  function tickStep(list, lo, hi) {
    return list.length > 1 ? (list[1] - list[0]) : ((hi - lo) || 1);
  }

  /* 线性映射：domain [d0,d1] → range [r0,r1]。domain 退化（全平）时落在 range 中点。 */
  function linear(d0, d1, r0, r1) {
    var dd = d1 - d0;
    if (!isFinite(dd) || dd === 0) return function () { return (r0 + r1) / 2; };
    return function (v) { return r0 + (v - d0) / dd * (r1 - r0); };
  }

  /* 点集：把原始数组按「原始下标」映射成坐标点，遇到 null/NaN 断成多段。
     —— 断段而不是压缩，保证有缺口的序列不会被画成假的连续线。 */
  function segs(arr, xf, yf) {
    var out = [], cur = [];
    if (!Array.isArray(arr)) return out;
    for (var i = 0; i < arr.length; i++) {
      var v = fin(arr[i]);
      if (v === null) { if (cur.length) { out.push(cur); cur = []; } }
      else cur.push({ x: xf(i), y: yf(v), v: v, i: i });
    }
    if (cur.length) out.push(cur);
    return out;
  }

  /* 取 n 个均匀分布的下标（含首尾、去重） */
  function pickIdx(n, k) {
    var out = [], seen = {}, i, j;
    if (!(n > 0)) return out;
    if (n === 1) return [0];
    var m = Math.min(k, n);
    for (i = 0; i < m; i++) {
      j = Math.round(i * (n - 1) / (m - 1));
      if (!seen[j]) { seen[j] = 1; out.push(j); }
    }
    return out;
  }

  /* SVG 片段构造 */
  function dLine(pts) {
    if (!pts || !pts.length) return '';
    var d = 'M' + r2(pts[0].x) + ' ' + r2(pts[0].y);
    for (var i = 1; i < pts.length; i++) d += 'L' + r2(pts[i].x) + ' ' + r2(pts[i].y);
    return d;
  }
  function dArea(pts, yBase) {                   // 折线下方的面积（用于渐变填充）
    if (!pts || pts.length < 2) return '';
    return dLine(pts) + 'L' + r2(pts[pts.length - 1].x) + ' ' + r2(yBase) +
      'L' + r2(pts[0].x) + ' ' + r2(yBase) + 'Z';
  }
  function lineEl(x1, y1, x2, y2, stroke, sw, dash) {
    return '<line x1="' + r2(x1) + '" y1="' + r2(y1) + '" x2="' + r2(x2) + '" y2="' + r2(y2) +
      '" stroke="' + stroke + '" stroke-width="' + (sw || 1) + '"' +
      (dash ? ' stroke-dasharray="' + dash + '"' : '') + '/>';
  }
  function rectEl(x, y, w, h, fill, extra) {
    return '<rect x="' + r2(x) + '" y="' + r2(y) + '" width="' + r2(Math.max(0, w)) +
      '" height="' + r2(Math.max(0, h)) + '" fill="' + fill + '"' + (extra || '') + '/>';
  }
  function pathEl(d, attrs) { return d ? '<path d="' + d + '" ' + (attrs || '') + '/>' : ''; }
  function circEl(x, y, r, fill, extra) {
    return '<circle cx="' + r2(x) + '" cy="' + r2(y) + '" r="' + r2(r) + '" fill="' + fill + '"' + (extra || '') + '/>';
  }
  /* 文字：数字默认等宽数字栈；中文标签显式传 font: C.sans */
  function T(x, y, s, o) {
    o = o || {};
    var a = 'x="' + r2(x) + '" y="' + r2(y) + '"';
    if (o.anchor) a += ' text-anchor="' + o.anchor + '"';
    if (o.mid) a += ' dominant-baseline="middle"';
    a += ' font-size="' + (o.size || 10.5) + '"';
    a += ' font-family="' + (o.font || C.num) + '"';
    a += ' fill="' + (o.fill || C.ink3) + '"';
    if (o.weight) a += ' font-weight="' + o.weight + '"';
    if (o.opacity !== null && o.opacity !== undefined) a += ' opacity="' + o.opacity + '"';
    if (o.rot) a += ' transform="rotate(' + r2(o.rot) + ' ' + r2(x) + ' ' + r2(y) + ')"';
    // halo：给压在折线/网格上的文字描一圈底色，保证任何数据下都读得清
    if (o.halo) a += ' stroke="' + (o.haloColor || C.surface) + '" stroke-width="' + (o.haloW || 3) +
      '" paint-order="stroke" stroke-linejoin="round"';
    a += ' style="font-variant-numeric:tabular-nums"';
    return '<text ' + a + '>' + esc(s) + '</text>';
  }
  function vGrad(id, y1, y2, col, o1, o2) {      // 竖直渐变（userSpaceOnUse：跟画布坐标走）
    return '<linearGradient id="' + id + '" gradientUnits="userSpaceOnUse" x1="0" y1="' + r2(y1) +
      '" x2="0" y2="' + r2(y2) + '">' +
      '<stop offset="0" stop-color="' + col + '" stop-opacity="' + o1 + '"/>' +
      '<stop offset="1" stop-color="' + col + '" stop-opacity="' + o2 + '"/></linearGradient>';
  }
  function wrap(w, h, inner, extra) {
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + r2(w) + ' ' + r2(h) +
      '" width="100%" height="100%" preserveAspectRatio="xMidYMid meet" style="display:block"' +
      (extra || '') + '>' + inner + '</svg>';
  }
  // 空态：一行灰字，居中
  function empty(w, h, msg) {
    w = pos(w, 240); h = pos(h, 60);
    return wrap(w, h, T(w / 2, h / 2, msg || '暂无数据',
      { anchor: 'middle', mid: true, size: 11, fill: C.ink4, font: C.sans }));
  }
  // 统一兜底：任何意外都不许把异常抛给调用方
  function safe(fn, w, h) {
    try { return fn(); } catch (e) { return empty(w, h, '暂无法绘制'); }
  }

  /* 图例：一行（放不下自动换行）。返回 {svg, h}，坐标从 (0,0) 起，由调用方 translate。 */
  function legendRow(maxW, items, o) {
    o = o || {};
    var size = o.size || 10, sw = o.swatch || 12, gap = o.gap || 14, lh = o.lh || 15;
    var font = o.font || C.sans, x = 0, y = size + 1, rows = 1, out = '';
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var w = sw + 5 + textW(it.label, size) + gap;
      if (x > 0 && x + w > maxW + gap) { x = 0; y += lh; rows += 1; }
      if (it.type === 'square') {
        out += rectEl(x, y - size + 1, size * 0.85, size * 0.85, it.color, ' rx="1.5"');
        x += size * 0.85 + 5;
      } else {
        out += lineEl(x, y - size / 2 + 1, x + sw, y - size / 2 + 1, it.color, 1.8);
        x += sw + 5;
      }
      out += T(x, y, clipText(it.label, Math.max(30, maxW - x - 2), size), { size: size, fill: C.ink2, font: font });
      x += textW(it.label, size) + gap;
    }
    return { svg: out, h: rows * lh };
  }

  /* ══════════════════════ 1. sparkline 迷你走势线 ══════════════════════
     映射：x = 下标均分整幅宽度；y = [min,max] → [底,顶]。
     全平（max===min）时人为撑开 ±1，让线落在中线而不是贴着边。
     单点时复制成两点画成平线——「只有一个数据」也应该看得出是条线。 */
  function sparkline(o) {
    o = o || {};
    var w = pos(o.width, 120), h = pos(o.height, 34);
    var color = tok(o.color) || C.red, sw = pos(o.strokeWidth, 1.5);
    return safe(function () {
      var arr = Array.isArray(o.values) ? o.values.slice() : [];
      var vals = [], i;
      for (i = 0; i < arr.length; i++) { var v = fin(arr[i]); if (v !== null) vals.push(v); }
      if (!vals.length) return empty(w, h, '暂无数据');
      if (vals.length === 1) arr = [vals[0], vals[0]];

      var base = fin(o.baseline);
      var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
      if (base !== null) { lo = Math.min(lo, base); hi = Math.max(hi, base); }
      if (hi === lo) { lo -= 1; hi += 1; }

      var pad = Math.max(sw / 2, 1.8) + 0.5;        // 给线宽与末点圆点留余量，避免贴边被裁
      var X = linear(0, Math.max(1, arr.length - 1), pad, w - pad);
      var Y = linear(lo, hi, h - pad, pad);
      var g = segs(arr, X, Y);
      var inner = '', gid = uid('spf');

      if (o.fill) {
        inner += '<defs>' + vGrad(gid, pad, h - pad, tok(o.fill), 0.30, 0) + '</defs>';
      }
      for (i = 0; i < g.length; i++) {
        var pts = g[i];
        if (pts.length === 1) {
          inner += circEl(pts[0].x, pts[0].y, Math.max(1.2, sw), color);
          continue;
        }
        if (o.fill) inner += pathEl(dArea(pts, h - pad), 'fill="url(#' + gid + ')"');
        inner += pathEl(dLine(pts), 'fill="none" stroke="' + color + '" stroke-width="' + sw +
          '" stroke-linejoin="round" stroke-linecap="round"');
      }
      if (base !== null) inner += lineEl(pad, Y(base), w - pad, Y(base), C.ink4, 1, '2 2');
      for (i = g.length - 1; i >= 0; i--) {          // 末点圆点：点出「当下」
        if (g[i].length) {
          var last = g[i][g[i].length - 1];
          inner += circEl(last.x, last.y, Math.max(1.6, sw), color);
          break;
        }
      }
      return wrap(w, h, inner);
    }, w, h);
  }

  /* ══════════════════════ 2. intraday 分时图 ══════════════════════
     映射：x = 有效点下标均分；y 的 domain 强制包含 prevClose（否则基准线会跑出画布）。
     红绿分区用两个 clipPath 切同一块面积：昨收线以上出红、以下出绿，
     折线本身也画两遍并分别裁切，于是「线上红、线下绿」是连续且精确的。 */
  function points(arr) {
    var out = [];
    if (!Array.isArray(arr)) return out;
    for (var i = 0; i < arr.length; i++) {
      var it = arr[i], p = null, t = '';
      if (it && typeof it === 'object') {
        p = fin(it.p !== undefined ? it.p : (it.value !== undefined ? it.value : it.c));
        var tv = it.t !== undefined ? it.t : it.time;
        t = tv === undefined || tv === null ? '' : String(tv);
      } else {
        p = fin(it);
      }
      if (p === null) continue;
      out.push({ t: t, p: p });
    }
    return out;
  }
  function intraday(o) {
    o = o || {};
    var W = pos(o.width, 760), H = pos(o.height, 200);
    var up = tok(o.upColor) || C.red, dn = tok(o.downColor) || C.green;
    var axis = o.showAxis !== false;
    return safe(function () {
      var pts = points(o.points);
      if (!pts.length) return empty(W, H, '暂无分时数据');
      if (pts.length === 1) pts.push({ t: '', p: pts[0].p });   // 单点也画成一条平线

      var mT = axis ? 12 : 6, mB = axis ? 18 : 6, mL = 6;
      var mR = axis ? 56 : 50;                       // 右侧永远留给最新价标签
      var pw = Math.max(10, W - mL - mR), ph = Math.max(10, H - mT - mB);

      var pc = fin(o.prevClose);
      if (pc === null) pc = pts[0].p;
      var lo = pc, hi = pc, i;
      for (i = 0; i < pts.length; i++) { if (pts[i].p < lo) lo = pts[i].p; if (pts[i].p > hi) hi = pts[i].p; }
      var span = hi - lo;
      if (!(span > 0)) span = Math.max(Math.abs(hi) * 0.01, 1e-6);
      lo -= span * 0.08; hi += span * 0.08;          // 上下各留 8%，线不贴边

      var n = pts.length;
      var X = linear(0, Math.max(1, n - 1), mL, mL + pw);
      var Y = linear(lo, hi, mT + ph, mT);
      var yPc = clamp(Y(pc), 0, H);
      var P = [];
      for (i = 0; i < n; i++) P.push({ x: X(i), y: Y(pts[i].p), v: pts[i].p });

      var idUp = uid('cu'), idDn = uid('cd'), gUp = uid('gu'), gDn = uid('gd');
      var inner = '<defs>' +
        '<clipPath id="' + idUp + '"><rect x="0" y="0" width="' + r2(W) + '" height="' + r2(yPc) + '"/></clipPath>' +
        '<clipPath id="' + idDn + '"><rect x="0" y="' + r2(yPc) + '" width="' + r2(W) + '" height="' + r2(Math.max(0, H - yPc)) + '"/></clipPath>' +
        vGrad(gUp, mT, yPc, up, 0.24, 0.03) + vGrad(gDn, yPc, mT + ph, dn, 0.03, 0.24) +
        '</defs>';

      var areaD = dArea(P, yPc), lineD = dLine(P);
      inner += pathEl(areaD, 'fill="url(#' + gUp + ')" clip-path="url(#' + idUp + ')"');
      inner += pathEl(areaD, 'fill="url(#' + gDn + ')" clip-path="url(#' + idDn + ')"');
      inner += pathEl(lineD, 'fill="none" stroke="' + up + '" stroke-width="1.4" stroke-linejoin="round" clip-path="url(#' + idUp + ')"');
      inner += pathEl(lineD, 'fill="none" stroke="' + dn + '" stroke-width="1.4" stroke-linejoin="round" clip-path="url(#' + idDn + ')"');
      // 昨收基准线（虚线）+ 左侧注记，避免与右侧最新价打架
      inner += lineEl(mL, yPc, mL + pw, yPc, C.ink4, 1, '3 3');
      inner += T(mL + 2, yPc - 4, '昨收 ' + fmtPrice(pc), { size: 10, fill: C.ink3, halo: true });

      // 最新价：末点 + 右侧色块标签
      var last = P[n - 1];
      var lc = last.v >= pc ? up : dn;
      var bw = Math.max(36, textW(fmtPrice(last.v), 10.5) + 12), bh = 15;
      var bx = W - mR + 3, by = clamp(last.y - bh / 2, 0, Math.max(0, H - bh));
      inner += circEl(last.x, last.y, 2.6, lc);
      inner += rectEl(bx, by, bw, bh, lc, ' rx="2"');
      inner += T(bx + bw / 2, by + bh / 2 + 0.5, fmtPrice(last.v),
        { anchor: 'middle', mid: true, size: 10.5, fill: C.surface, weight: 600 });

      if (axis && pts[0].t) {
        var idx = pickIdx(n, 5);
        for (i = 0; i < idx.length; i++) {
          var an = i === 0 ? 'start' : (i === idx.length - 1 ? 'end' : 'middle');
          inner += T(clamp(P[idx[i]].x, 2, W - 2), H - 5, pts[idx[i]].t, { anchor: an, size: 10, fill: C.ink3 });
        }
      }
      return wrap(W, H, inner);
    }, W, H);
  }

  /* ══════════════════════ 3. candles K 线 ══════════════════════
     映射：价格主图占上方 (100% - 22%) 区域，y = [lowMin, highMax] → [底,顶]；
     下方 22% 高度画成交量，y = [0, vMax] → [区底, 区顶]。
     x 用「槽位中心」：cx(i) = 左边距 + 槽宽 × (i + 0.5)，蜡烛实体宽 = 槽宽 × 0.62（1~14px 夹紧）。 */
  function bars(arr) {
    var out = [];
    if (!Array.isArray(arr)) return out;
    for (var i = 0; i < arr.length; i++) {
      var b = arr[i];
      if (!b || typeof b !== 'object') continue;
      var oo = fin(b.o !== undefined ? b.o : b.open), cc = fin(b.c !== undefined ? b.c : b.close);
      var hh = fin(b.h !== undefined ? b.h : b.high), ll = fin(b.l !== undefined ? b.l : b.low);
      var vv = fin(b.v !== undefined ? b.v : b.volume);
      if (oo === null && cc === null) continue;
      if (oo === null) oo = cc;
      if (cc === null) cc = oo;
      if (hh === null) hh = Math.max(oo, cc);
      if (ll === null) ll = Math.min(oo, cc);
      if (hh < ll) { var t = hh; hh = ll; ll = t; }
      if (vv === null || vv < 0) vv = 0;
      var dd = b.d !== undefined ? b.d : b.date;
      out.push({ d: dd === undefined || dd === null ? '' : String(dd), o: oo, h: hh, l: ll, c: cc, v: vv });
    }
    return out;
  }
  function sma(vals, p) {
    var out = new Array(vals.length), sum = 0, i;
    for (i = 0; i < vals.length; i++) {
      sum += vals[i];
      if (i >= p) sum -= vals[i - p];
      out[i] = i >= p - 1 ? sum / p : null;
    }
    return out;
  }
  function candles(o) {
    o = o || {};
    var W = pos(o.width, 760), H = pos(o.height, 280);
    var up = tok(o.upColor) || C.red, dn = tok(o.downColor) || C.green;
    var axis = o.showAxis !== false, showVol = o.showVolume !== false;
    return safe(function () {
      var B = bars(o.bars);
      if (!B.length) return empty(W, H, '暂无K线数据');

      var mL = 4, mR = axis ? 52 : 8, mT = 8, mB = axis ? 18 : 6;
      var pw = Math.max(10, W - mL - mR), ph = Math.max(10, H - mT - mB);
      var volH = showVol ? Math.round(ph * 0.22) : 0;
      var gap = showVol ? 8 : 0;
      var priceH = Math.max(10, ph - volH - gap);

      var lo = Infinity, hi = -Infinity, i, j;
      for (i = 0; i < B.length; i++) { if (B[i].l < lo) lo = B[i].l; if (B[i].h > hi) hi = B[i].h; }
      if (!isFinite(lo) || !isFinite(hi)) return empty(W, H, '暂无K线数据');
      if (hi === lo) { hi = lo + 1; lo -= 1; }
      var padv = (hi - lo) * 0.06; lo -= padv; hi += padv;

      var step = pw / B.length;
      var bw = clamp(step * 0.62, 1, 14);
      var cx = function (k) { return mL + step * (k + 0.5); };
      var Y = linear(lo, hi, mT + priceH, mT);

      var inner = '', k;

      // 网格 + 右侧价格轴（只画必要刻度，不画边框）
      var tk = ticks(lo, hi, 4), tkS = tickStep(tk, lo, hi);
      for (i = 0; i < tk.length; i++) {
        var ty = Y(tk[i]);
        if (ty < mT - 1 || ty > mT + priceH + 1) continue;
        inner += lineEl(mL, ty, mL + pw, ty, C.line, 0.5);
        inner += T(W - mR + 5, ty, fmtTick(tk[i], tkS), { mid: true, size: 10, fill: C.ink3 });
      }

      // 成交量（下方 22%）
      if (showVol && volH > 2) {
        var vTop = mT + priceH + gap;
        var maxV = 0;
        for (i = 0; i < B.length; i++) if (B[i].v > maxV) maxV = B[i].v;
        if (!(maxV > 0)) maxV = 1;
        var VY = linear(0, maxV, vTop + volH, vTop);
        for (i = 0; i < B.length; i++) {
          var col = B[i].c >= B[i].o ? up : dn;
          var vy = VY(B[i].v);
          inner += rectEl(cx(i) - bw / 2, vy, bw, Math.max(0.5, vTop + volH - vy), col, ' fill-opacity="0.42"');
        }
        inner += lineEl(mL, vTop + volH, mL + pw, vTop + volH, C.line, 0.5);
        inner += T(W - mR + 5, vTop + 4, fmtVol(maxV), { size: 9.5, fill: C.ink4 });
      }

      // 蜡烛：影线 + 实体。c >= o 视为阳线（红），实体至少 1px 高（十字星）
      for (i = 0; i < B.length; i++) {
        var b = B[i], x0 = cx(i), col2 = b.c >= b.o ? up : dn;
        inner += lineEl(x0, Y(b.h), x0, Y(b.l), col2, 1);
        var yo = Y(b.o), yc = Y(b.c);
        inner += rectEl(x0 - bw / 2, Math.min(yo, yc), bw, Math.max(1, Math.abs(yc - yo)), col2);
      }

      // 均线（简单移动平均）
      var maArr = Array.isArray(o.ma) ? o.ma : [5, 20, 60];
      var maCol = tokList((Array.isArray(o.maColors) && o.maColors.length) ? o.maColors
        : ['var(--gold)', 'var(--blue)', 'var(--navy)']);
      var closes = [], maPaths = '', legend = '', lx = mL + 2;
      for (i = 0; i < B.length; i++) closes.push(B[i].c);
      for (i = 0; i < maArr.length; i++) {
        var p = Math.floor(fin(maArr[i]) === null ? 0 : fin(maArr[i]));
        if (p < 2 || p > B.length) continue;
        var mc = maCol[i % maCol.length] || PAL[i % PAL.length];
        var m = sma(closes, p), mp = [];
        for (j = 0; j < m.length; j++) if (m[j] !== null) mp.push({ x: cx(j), y: Y(m[j]) });
        if (mp.length > 1) maPaths += pathEl(dLine(mp), 'fill="none" stroke="' + mc + '" stroke-width="1.2" stroke-linejoin="round"');
        var lt = 'MA' + p;
        legend += T(lx, mT + 9, lt, { size: 9.5, fill: mc });
        lx += textW(lt, 9.5) + 8;
      }
      inner += maPaths + legend;

      if (axis) {
        var di = pickIdx(B.length, 6);
        for (i = 0; i < di.length; i++) {
          var an = di[i] === 0 ? 'start' : (di[i] === B.length - 1 ? 'end' : 'middle');
          inner += T(clamp(cx(di[i]), 2, W - 2), H - 4, shortDate(B[di[i]].d), { anchor: an, size: 10, fill: C.ink3 });
        }
      }
      return wrap(W, H, inner);
    }, W, H);
  }

  /* ══════════════════════ 4. lines 多序列折线/面积 ══════════════════════
     映射：所有序列共用一条日期轴（x = 下标）和一条数值轴（y = 全部序列的 [min,max]）。
     有缺口的序列按段断开，不会连出假线。zeroLine 会把 0 强行纳入 domain。 */
  function lines(o) {
    o = o || {};
    var W = pos(o.width, 760), H = pos(o.height, 240);
    var axis = o.showAxis !== false;
    return safe(function () {
      var S = [], i, j;
      if (Array.isArray(o.series)) {
        for (i = 0; i < o.series.length; i++) {
          var s = o.series[i];
          if (!s || typeof s !== 'object') continue;
          var vs = Array.isArray(s.values) ? s.values : [], ok = false;
          for (j = 0; j < vs.length; j++) if (fin(vs[j]) !== null) { ok = true; break; }
          if (!ok) continue;
          S.push({
            name: s.name !== undefined && s.name !== null ? String(s.name) : ('序列' + (i + 1)),
            values: vs, dates: Array.isArray(s.dates) ? s.dates : null
          });
        }
      }
      if (!S.length) return empty(W, H, '暂无数据');
      var colors = tokList((Array.isArray(o.colors) && o.colors.length) ? o.colors : PAL);
      for (i = 0; i < S.length; i++) S[i].color = colors[i % colors.length] || PAL[i % PAL.length];

      var n = 0;
      for (i = 0; i < S.length; i++) n = Math.max(n, S[i].values.length);
      if (n < 1) return empty(W, H, '暂无数据');

      var lo = Infinity, hi = -Infinity;
      for (i = 0; i < S.length; i++) {
        for (j = 0; j < S[i].values.length; j++) {
          var v = fin(S[i].values[j]);
          if (v === null) continue;
          if (v < lo) lo = v;
          if (v > hi) hi = v;
        }
      }
      if (!isFinite(lo) || !isFinite(hi)) return empty(W, H, '暂无数据');
      if (o.zeroLine) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
      if (hi === lo) { hi = lo + 1; lo -= 1; }
      var padv = (hi - lo) * 0.06; lo -= padv; hi += padv;

      // 图例（先量高度，再决定绘图区顶部）
      var legH = 0, legSvg = '';
      if (o.showLegend) {
        var L = legendRow(W - 12, S.map(function (x) { return { label: x.name, color: x.color, type: 'line' }; }), {});
        legSvg = '<g transform="translate(6,4)">' + L.svg + '</g>';
        legH = L.h + 4;
      }
      var mL = axis ? 52 : 6, mR = 6, mT = 8 + legH, mB = axis ? 16 : 6;
      var pw = Math.max(10, W - mL - mR), ph = Math.max(10, H - mT - mB);
      var X = linear(0, Math.max(1, n - 1), mL, mL + pw);
      var Y = linear(lo, hi, mT + ph, mT);

      var inner = legSvg;
      if (axis) {
        var tk = ticks(lo, hi, 4), tkS = tickStep(tk, lo, hi);
        for (i = 0; i < tk.length; i++) {
          var ty = Y(tk[i]);
          if (ty < mT - 1 || ty > mT + ph + 1) continue;
          inner += lineEl(mL, ty, mL + pw, ty, C.line, 0.5, '2 3');
          inner += T(mL - 6, ty, fmtTick(tk[i], tkS), { anchor: 'end', mid: true, size: 10, fill: C.ink3 });
        }
      }
      if (o.zeroLine && lo < 0 && hi > 0) {
        inner += lineEl(mL, Y(0), mL + pw, Y(0), C.line2, 1);
        inner += T(mL - 6, Y(0), '0', { anchor: 'end', mid: true, size: 10, fill: C.ink3 });
      }

      var defs = '';
      for (i = 0; i < S.length; i++) {
        var g = segs(S[i].values, X, Y), k;
        for (k = 0; k < g.length; k++) {
          var pts = g[k];
          if (pts.length === 1) {
            inner += circEl(pts[0].x, pts[0].y, 2, S[i].color);
            continue;
          }
          if (o.area) {
            var gid = uid('la');
            defs += vGrad(gid, mT, mT + ph, S[i].color, 0.20, 0);
            inner += pathEl(dArea(pts, mT + ph), 'fill="url(#' + gid + ')"');
          }
          inner += pathEl(dLine(pts), 'fill="none" stroke="' + S[i].color +
            '" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"');
          if (n <= 30) for (var q = 0; q < pts.length; q++) inner += circEl(pts[q].x, pts[q].y, 1.8, S[i].color);
        }
      }
      if (defs) inner = '<defs>' + defs + '</defs>' + inner;

      if (axis) {
        var dates = null;
        for (i = 0; i < S.length; i++) if (S[i].dates && S[i].dates.length >= n) { dates = S[i].dates; break; }
        var idx = pickIdx(n, 6);
        for (i = 0; i < idx.length; i++) {
          var an = i === 0 ? 'start' : (i === idx.length - 1 ? 'end' : 'middle');
          var lb = dates ? shortDate(dates[idx[i]]) : String(idx[i]);
          inner += T(clamp(X(idx[i]), 2, W - 2), H - 4, lb, { anchor: an, size: 10, fill: C.ink3 });
        }
      }
      return wrap(W, H, inner);
    }, W, H);
  }

  /* ══════════════════════ 5. barRows 横向条形排行 ══════════════════════
     映射：0 落在条形区正中，|value| / maxAbs 映射到半幅宽度 —— 正向右、负向左发散。
     高度：不传就按条数自动算；传了但不够高（每条 < 14px）则以自动高度为准，
     宁可让 viewBox 变高被等比缩小，也不让内容溢出画布。 */
  function barRows(o) {
    o = o || {};
    var W = pos(o.width, 420);
    return safe(function () {
      var items = [], i;
      if (Array.isArray(o.items)) {
        for (i = 0; i < o.items.length; i++) {
          var it = o.items[i];
          if (!it || typeof it !== 'object') continue;
          var v = fin(it.value);
          items.push({
            label: it.label === undefined || it.label === null ? '' : String(it.label),
            sub: it.sub === undefined || it.sub === null ? '' : String(it.sub),
            value: v === null ? 0 : v
          });
        }
      }
      if (!items.length) return empty(W, pos(o.height, 56), '暂无数据');

      var need = items.length * 14 + 12;
      var H = Math.max(pos(o.height, need), need);
      var padT = 6, padB = 6, rowH = (H - padT - padB) / items.length;

      var maxAbs = 0;
      for (i = 0; i < items.length; i++) maxAbs = Math.max(maxAbs, Math.abs(items[i].value));
      if (!(maxAbs > 0)) maxAbs = 1;
      var ud = maxAbs >= 100 ? 0 : (maxAbs >= 10 ? 1 : 2);

      var posC = tok(o.posColor) || C.red, negC = tok(o.negColor) || C.green;
      var unit = o.unit === null || o.unit === undefined ? '%' : String(o.unit);
      var labelW = clamp(W * 0.32, 64, 180);
      var valW = o.showValue === false ? 4 : 66;
      var bx0 = labelW, bx1 = Math.max(bx0 + 20, W - valW);
      var mid = (bx0 + bx1) / 2, half = Math.max(6, (bx1 - bx0) / 2 - 2);

      var inner = lineEl(mid, padT, mid, H - padB, C.line, 1);   // 0 轴
      for (i = 0; i < items.length; i++) {
        var it2 = items[i], cy = padT + rowH * (i + 0.5);
        var bh = clamp(rowH - 9, 7, 15);
        var len = Math.abs(it2.value) / maxAbs * half;
        var col = it2.value >= 0 ? posC : negC;

        inner += T(2, cy, clipText(it2.label, labelW - 8, 11), { mid: true, size: 11, fill: C.ink2, font: C.sans });
        if (it2.sub) {
          var lw = textW(it2.label, 11);
          if (lw + 10 + textW(it2.sub, 9.5) < labelW - 6) {
            inner += T(2 + lw + 8, cy, it2.sub, { mid: true, size: 9.5, fill: C.ink4, font: C.sans });
          }
        }
        if (it2.value === 0) {
          inner += rectEl(mid - 1, cy - bh / 2, 2, bh, C.ink4);
        } else {
          var rx = it2.value > 0 ? mid : mid - Math.max(1, len);
          inner += rectEl(rx, cy - bh / 2, Math.max(1, len), bh, col, ' rx="1.5"');
        }
        if (o.showValue !== false) {
          inner += T(W - 2, cy, fmtSigned(it2.value, ud) + unit,
            { anchor: 'end', mid: true, size: 11, fill: it2.value === 0 ? C.ink3 : col, weight: 600 });
        }
      }
      return wrap(W, H, inner);
    }, W, pos(o.height, 120));
  }

  /* ══════════════════════ 6. heatGrid 涨跌热力网格 ══════════════════════
     映射：没有 width 参数 —— 单元格固定 108×cellHeight，画布宽高由列数/行数反推。
     透明度 = 0.15 + 0.85 × (|value| / maxAbs)，最大涨跌幅给满色，接近 0 的给最浅。 */
  function heatGrid(o) {
    o = o || {};
    var cols = Math.max(1, Math.floor(fin(o.columns) === null ? 6 : fin(o.columns)));
    var gv = fin(o.gap);
    var gap = gv === null ? 6 : Math.max(0, gv);
    var cellH = pos(o.cellHeight, 54);
    var cellW = 126;                                // 单元格基准宽：要同时容下「沪深300」和一个 6 字数值
    var posC = tok(o.posColor) || C.red, negC = tok(o.negColor) || C.green;
    var labelColor = o.labelColor === undefined ? '#fff' : o.labelColor;
    return safe(function () {
      var items = [], i;
      if (Array.isArray(o.items)) {
        for (i = 0; i < o.items.length; i++) {
          var it = o.items[i];
          if (!it || typeof it !== 'object') continue;
          items.push({
            label: it.label === undefined || it.label === null ? '' : String(it.label),
            sub: it.sub === undefined || it.sub === null ? '' : String(it.sub),
            value: fin(it.value)
          });
        }
      }
      var W = cols * cellW + (cols - 1) * gap, H = cellH;
      if (!items.length) return empty(W, H, '暂无数据');
      var rows = Math.ceil(items.length / cols);
      H = rows * cellH + (rows - 1) * gap;

      var maxAbs = 0;
      for (i = 0; i < items.length; i++) if (items[i].value !== null) maxAbs = Math.max(maxAbs, Math.abs(items[i].value));

      var inner = '';
      for (i = 0; i < items.length; i++) {
        var it2 = items[i];
        var r = Math.floor(i / cols), c = i % cols;
        var x = c * (cellW + gap), y = r * (cellH + gap);
        var v = it2.value, fill, alpha = 1, tc = labelColor;

        if (v === null || v === 0 || !(maxAbs > 0)) {
          fill = C.surface2; tc = C.ink3;                    // 平盘/无数据：中性灰底 + 深字
        } else {
          fill = v >= 0 ? posC : negC;
          alpha = r2(0.15 + 0.85 * Math.min(1, Math.abs(v) / maxAbs));
        }
        // 标签可用宽度 = 单元格 - 左右内边距 - 数值实占宽 - 最小间隙（数值估宽对 ASCII 是准的）
        var vTxt = v === null ? '' : fmtPct(v);
        var labW = Math.max(24, cellW - 20 - (vTxt ? textW(vTxt, 12.5) + 2 : 0) - 10);
        inner += '<g><title>' + esc(it2.label + (it2.sub ? ' · ' + it2.sub : '') + ' ' + fmtPct(v)) + '</title>' +
          rectEl(x, y, cellW, cellH, fill, ' fill-opacity="' + alpha + '" rx="3"') +
          T(x + 10, y + 21, clipText(it2.label, labW, 12.5), { size: 12.5, fill: tc, weight: 600, font: C.sans, opacity: 0.95 }) +
          (v === null ? '' : T(x + cellW - 10, y + 22, vTxt, { anchor: 'end', size: 12.5, fill: tc, weight: 600 })) +
          (it2.sub ? T(x + 10, y + cellH - 9, clipText(it2.sub, cellW - 20, 9.5), { size: 9.5, fill: tc, font: C.sans, opacity: 0.68 }) : '') +
          '</g>';
      }
      return wrap(W, H, inner);
    }, 400, 120);
  }

  /* ══════════════════════ 7. heatmap 相关系数方阵 ══════════════════════
     映射：n×n 方阵铺在 size×size 画布内；左侧留 78px 放行标签、顶部留 84px 放 -45° 列标签。
     单元格 = min(可用宽, 可用高) / n，网格整体在可用区内居中，保证任何 n 都不溢出。 */
  function heatmap(o) {
    o = o || {};
    var S = pos(o.size, 560);
    var posC = tok(o.posColor) || C.red, negC = tok(o.negColor) || C.blue;
    var gv = fin(o.cellGap);
    var gap = gv === null ? 1 : Math.max(0, gv);
    return safe(function () {
      var labels = Array.isArray(o.labels) ? o.labels.map(function (x) { return String(x); }) : [];
      var M = Array.isArray(o.matrix) ? o.matrix : [];
      var n = Math.min(labels.length, M.length);
      if (n < 2) return empty(S, S * 0.6, '暂无数据');
      labels = labels.slice(0, n);

      var padL = 78, padT = 64, padR = 44, padB = 8;
      var cw = (S - padL - padR - gap * (n - 1)) / n;
      var ch = (S - padT - padB - gap * (n - 1)) / n;
      var cell = Math.max(5, Math.min(cw, ch));
      var gw = cell * n + gap * (n - 1);
      var gx = padL, gy = padT;                     // 网格左上角贴住留白，余量留到右下（不居中，避免上方空一大块）

      var inner = '', i, j;
      // 顶部列标签：-45° 旋转，起点在列中心正上方
      for (j = 0; j < n; j++) {
        var tx = gx + j * (cell + gap) + cell / 2;
        inner += T(tx, gy - 6, clipText(labels[j], padT - 14, 10),
          { anchor: 'start', size: 10, fill: C.ink3, font: C.sans, rot: -45 });
      }
      // 左侧行标签：右对齐，稍深一档便于纵向扫读
      for (i = 0; i < n; i++) {
        inner += T(gx - 8, gy + i * (cell + gap) + cell / 2, clipText(labels[i], padL - 14, 10.5),
          { anchor: 'end', mid: true, size: 10.5, fill: C.ink2, font: C.sans });
      }
      // 单元格
      var showNum = cell >= 30;
      for (i = 0; i < n; i++) {
        var row = Array.isArray(M[i]) ? M[i] : [];
        for (j = 0; j < n; j++) {
          var x = gx + j * (cell + gap), y = gy + i * (cell + gap);
          var v = fin(row[j]);
          var fill, alpha = 1, tc = C.ink2;
          if (i === j) {
            fill = C.line; tc = C.ink4;                 // 对角线画灰
          } else if (v === null) {
            fill = C.surface2; tc = C.ink4;
          } else {
            v = clamp(v, -1, 1);
            alpha = r2(0.06 + 0.84 * Math.abs(v));
            fill = v >= 0 ? posC : negC;
            tc = alpha > 0.5 ? C.surface : C.ink2;
          }
          inner += '<g><title>' + esc(labels[i] + ' × ' + labels[j] + ' = ' + (v === null ? '—' : Number(v).toFixed(2))) + '</title>' +
            rectEl(x, y, cell, cell, fill, ' fill-opacity="' + alpha + '"') +
            (showNum && i !== j && v !== null
              ? T(x + cell / 2, y + cell / 2 + 0.5, Number(v).toFixed(2), { anchor: 'middle', mid: true, size: Math.min(11, cell * 0.3), fill: tc })
              : '') +
            '</g>';
        }
      }
      return wrap(S, S, inner);
    }, S, S * 0.6);
  }

  /* ══════════════════════ 8. radar 雷达图 ══════════════════════
     映射：第 i 根轴的角度 = -90° + i×360/n（-90° 指正上方，顺时针）。
     半径 = value / max × R；顶点 = 圆心 + 半径 × (cos, sin)（注意 SVG 的 y 轴向下）。
     R 同时受「左右方向轴名宽度」与「上下方向文字高度」约束：R = min(cx-padX, availH/2-padY)，
     所以 3 轴和 12 轴都不会把标签挤出画布；轴名还会按锚点方向二次截断兜底。
     数值标注策略：轴数 × 序列数 ≤ 12 时逐点标注（轴名再外扩一圈让位），否则只留图例，避免糊成一片。 */
  function radar(o) {
    o = o || {};
    var S = pos(o.size, 300);
    return safe(function () {
      var axes = Array.isArray(o.axes) ? o.axes.map(function (x) { return String(x); }) : [];
      var n = axes.length;
      if (n < 3) return empty(S, S * 0.8, '至少需要 3 个维度');
      var raw = [];
      if (Array.isArray(o.series)) {
        for (var i = 0; i < o.series.length; i++) {
          var s = o.series[i];
          if (!s || typeof s !== 'object') continue;
          raw.push({ name: s.name === undefined || s.name === null ? ('系列' + (i + 1)) : String(s.name), values: Array.isArray(s.values) ? s.values : [] });
        }
      }
      if (!raw.length) return empty(S, S * 0.8, '暂无数据');
      var colors = tokList((Array.isArray(o.colors) && o.colors.length) ? o.colors : PAL);
      var mx = fin(o.max);
      if (mx === null || mx <= 0) {
        mx = 0;
        for (var a = 0; a < raw.length; a++) {
          for (var b = 0; b < n; b++) { var vv = fin(raw[a].values[b]); if (vv !== null) mx = Math.max(mx, Math.abs(vv)); }
        }
        if (!(mx > 0)) mx = 100;
      }

      // 数值标注：只在「单序列」时逐点标注 —— 多序列时同一条轴上会叠出两三个数字，
      // 反而读不出来；多序列改由图例交代序列、每个顶点用 <title> 交代数值。
      var labelAll = (o.showValues !== false) && raw.length === 1;
      // 图例先排版，占掉的高度从雷达可用区里扣
      var leg = null, legH = 0;
      if (raw.length > 1) {
        leg = legendRow(S - 7, raw.map(function (s, k) {
          return { label: s.name, color: colors[k % colors.length] || PAL[k % PAL.length], type: 'square' };
        }), { size: 9.5, swatch: 9 });
        legH = leg.h + 2;
      }
      var maxLabW = 0, q;
      for (q = 0; q < n; q++) maxLabW = Math.max(maxLabW, textW(axes[q], 10.5));
      maxLabW = Math.min(maxLabW, S * 0.34);
      var ring = labelAll ? 26 : 13;             // 轴名相对 R 外扩多少（要给数值标注让出一圈）
      var availH = S - legH;
      var cx = S / 2, cy = legH + availH / 2;
      var R = Math.max(S * 0.22, Math.min(cx - ring - maxLabW, availH / 2 - ring - 9));

      var ringN = 4, i2, j2;
      var inner = leg ? '<g transform="translate(2,0)">' + leg.svg + '</g>' : '';
      // 环 + 轴网
      for (i2 = 1; i2 <= ringN; i2++) {
        var rr = R * i2 / ringN, pp = [];
        for (j2 = 0; j2 < n; j2++) {
          var ang = -Math.PI / 2 + j2 * 2 * Math.PI / n;
          pp.push(r2(cx + rr * Math.cos(ang)) + ',' + r2(cy + rr * Math.sin(ang)));
        }
        inner += '<polygon points="' + pp.join(' ') + '" fill="none" stroke="' + C.line + '" stroke-width="0.5"/>';
        // 环刻度压在竖轴上，正上方就是顶点 —— 逐点标注数值时不再画刻度，两者只留其一
        if (!labelAll) inner += T(cx - 5, cy - rr + 8, fmtTick(mx * i2 / ringN, mx / ringN), { anchor: 'end', mid: true, size: 9, fill: C.ink4 });
      }
      for (j2 = 0; j2 < n; j2++) {
        var ang2 = -Math.PI / 2 + j2 * 2 * Math.PI / n;
        inner += lineEl(cx, cy, cx + R * Math.cos(ang2), cy + R * Math.sin(ang2), C.line, 0.5);
      }
      // 轴名：按锚点方向算「还剩多少横向量」，再截断兜底
      for (j2 = 0; j2 < n; j2++) {
        var ang3 = -Math.PI / 2 + j2 * 2 * Math.PI / n;
        var co = Math.cos(ang3), si = Math.sin(ang3);
        var an = co > 0.15 ? 'start' : (co < -0.15 ? 'end' : 'middle');
        var lx = cx + (R + ring) * co, ly = cy + (R + ring) * si;
        var isMid = Math.abs(si) < 0.5;
        var room = an === 'start' ? (S - lx - 2) : (an === 'end' ? (lx - 2) : Math.min(S - lx, lx) * 2 - 4);
        inner += T(clamp(lx, 2, S - 2), clamp(ly + (isMid ? 0 : (si > 0 ? 4 : -2)), 8, S - 4),
          clipText(axes[j2], room, 10.5), { anchor: an, mid: isMid, size: 10.5, fill: C.ink2, font: C.sans });
      }
      // 序列
      for (i2 = 0; i2 < raw.length; i2++) {
        var col = colors[i2 % colors.length] || PAL[i2 % PAL.length];
        var pts = [], vals = [];
        for (j2 = 0; j2 < n; j2++) {
          var v0 = fin(raw[i2].values[j2]);
          if (v0 === null) v0 = 0;
          vals.push(v0);
          var rr2 = clamp(v0 / mx, 0, 1.4) * R, ag = -Math.PI / 2 + j2 * 2 * Math.PI / n;
          pts.push({ x: cx + rr2 * Math.cos(ag), y: cy + rr2 * Math.sin(ag) });
        }
        inner += '<polygon points="' + pts.map(function (p) { return r2(p.x) + ',' + r2(p.y); }).join(' ') +
          '" fill="' + col + '" fill-opacity="0.12" stroke="' + col + '" stroke-width="1.6" stroke-linejoin="round"/>';
        for (j2 = 0; j2 < n; j2++) {
          inner += '<g><title>' + esc(raw[i2].name + ' · ' + axes[j2] + ' ' + (Math.round(vals[j2] * 100) / 100)) + '</title>' +
            circEl(pts[j2].x, pts[j2].y, 2.2, C.surface, ' stroke="' + col + '" stroke-width="1.4"') + '</g>';
          if (labelAll) {
            var ag2 = -Math.PI / 2 + j2 * 2 * Math.PI / n;
            var vr = clamp(vals[j2] / mx, 0, 1.4) * R + 12;
            inner += T(clamp(cx + vr * Math.cos(ag2), 6, S - 6), cy + vr * Math.sin(ag2) + (Math.abs(Math.sin(ag2)) < 0.5 ? 0 : 3),
              String(Math.round(vals[j2] * 100) / 100),
              { anchor: 'middle', mid: Math.abs(Math.sin(ag2)) < 0.5, size: 9.5, fill: col, halo: true });
          }
        }
      }
      return wrap(S, S, inner);
    }, S, S * 0.8);
  }

  /* ══════════════════════ 9. donut 环形图 ══════════════════════
     映射：用「圆环 + stroke-dasharray」而不是弧线路径 —— 每段的长度就是它在周长上的占比。
     value 视作 0..1 的占比；若总和 > 1（调用方传的是权重）则自动按总和归一化。
     段与段之间留 2px 缺口，起点在 12 点方向（rotate(-90)）。 */
  function donut(o) {
    o = o || {};
    var S = pos(o.size, 220);
    return safe(function () {
      var segsIn = [], i;
      if (Array.isArray(o.segments)) {
        for (i = 0; i < o.segments.length; i++) {
          var s = o.segments[i];
          if (!s || typeof s !== 'object') continue;
          var v = fin(s.value);
          if (v === null || v <= 0) continue;
          segsIn.push({
            label: s.label === undefined || s.label === null ? '' : String(s.label),
            value: v, color: tok(s.color) || PAL[segsIn.length % PAL.length]
          });
        }
      }
      if (!segsIn.length) return empty(S, S, '暂无数据');
      var total = 0;
      for (i = 0; i < segsIn.length; i++) total += segsIn[i].value;
      if (!(total > 0)) return empty(S, S, '暂无数据');
      var scale = total > 1.0001 ? total : 1;       // 1 以内视为占比；超过则按权重归一化

      var th = clamp(fin(o.thickness) === null ? 30 : fin(o.thickness), 4, S / 2 - 8);
      var r = S / 2 - 5 - th / 2, circ = 2 * Math.PI * r;

      var inner = circEl(S / 2, S / 2, r, 'none', ' stroke="' + C.surface2 + '" stroke-width="' + r2(th) + '"');
      var acc = 0;
      for (i = 0; i < segsIn.length; i++) {
        var sg = segsIn[i];
        var frac = sg.value / scale;
        var len = frac * circ;
        var dash = Math.max(0.5, len - 2);
        inner += '<g><title>' + esc(sg.label + ' ' + (frac * 100).toFixed(1) + '%') + '</title>' +
          '<circle cx="' + r2(S / 2) + '" cy="' + r2(S / 2) + '" r="' + r2(r) + '" fill="none" stroke="' + sg.color +
          '" stroke-width="' + r2(th) + '" stroke-dasharray="' + r2(dash) + ' ' + r2(Math.max(0.5, circ - dash)) +
          '" stroke-dashoffset="' + r2(-acc * circ) + '" transform="rotate(-90 ' + r2(S / 2) + ' ' + r2(S / 2) + ')"/></g>';
        // 够宽的段直接在弧上标百分比，省掉一张图例
        if (frac * 360 >= 26) {
          var midA = -Math.PI / 2 + (acc + frac / 2) * 2 * Math.PI;
          inner += T(S / 2 + r * Math.cos(midA), S / 2 + r * Math.sin(midA) + 0.5, (frac * 100).toFixed(0) + '%',
            { anchor: 'middle', mid: true, size: 10, fill: C.surface, weight: 600 });
        }
        acc += frac;
      }
      var cl = o.centerLabel, cs = o.centerSub;
      if (cl !== null && cl !== undefined || cs !== null && cs !== undefined) {
        if (cs !== null && cs !== undefined && cs !== '') {
          inner += T(S / 2, S / 2 - 3, cl === null || cl === undefined ? '' : cl, { anchor: 'middle', mid: true, size: 21, fill: C.ink, weight: 600, font: C.num });
          inner += T(S / 2, S / 2 + 16, cs, { anchor: 'middle', mid: true, size: 10.5, fill: C.ink3, font: C.sans });
        } else {
          inner += T(S / 2, S / 2, cl === null || cl === undefined ? '' : cl, { anchor: 'middle', mid: true, size: 23, fill: C.ink, weight: 600, font: C.num });
        }
      }
      return wrap(S, S, inner);
    }, S, S);
  }

  /* ══════════════════════ 10. gauge 仪表盘 ══════════════════════
     映射：半圆。角度 a 从 180°（左）扫到 360°（右），a(v) = π + (v-min)/(max-min) × π。
     点 = 圆心 + R × (cos a, sin a)。zones 是「累计上界」，[上一档 to, 本档 to] 即本档区间。
     已达成区间用满色，未达成区间用 0.16 透明度 —— 一眼看出风险档位与当前位置。 */
  function polar(cx, cy, r, a) { return { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) }; }
  function arcD(cx, cy, r, a0, a1) {
    if (Math.abs(a1 - a0) < 1e-6) return '';
    var p0 = polar(cx, cy, r, a0), p1 = polar(cx, cy, r, a1);
    var large = (a1 - a0) > Math.PI ? 1 : 0;
    return 'M' + r2(p0.x) + ' ' + r2(p0.y) + 'A' + r2(r) + ' ' + r2(r) + ' 0 ' + large + ' 1 ' + r2(p1.x) + ' ' + r2(p1.y);
  }
  function gauge(o) {
    o = o || {};
    var S = pos(o.size, 180);
    return safe(function () {
      var mn = fin(o.min); if (mn === null) mn = 0;
      var mx = fin(o.max); if (mx === null) mx = 100;
      if (!(mx > mn)) return empty(S, S * 0.68, '量程无效');
      var v = fin(o.value); if (v === null) v = mn;
      v = clamp(v, mn, mx);

      var zones = [];
      if (Array.isArray(o.zones)) {
        var prev = mn;
        for (var i = 0; i < o.zones.length; i++) {
          var z = o.zones[i];
          if (!z || typeof z !== 'object') continue;
          var to = fin(z.to);
          if (to === null || to <= prev) continue;
          zones.push({ from: prev, to: Math.min(to, mx), color: tok(z.color) || PAL[i % PAL.length] });
          prev = Math.min(to, mx);
          if (prev >= mx) break;
        }
      }
      if (!zones.length) zones = [{ from: mn, to: mx, color: C.blue }];

      var cx = S / 2, cy = S * 0.58, R = S * 0.40;
      var th = Math.max(6, S * 0.085);
      var A = function (val) { return Math.PI + (val - mn) / (mx - mn) * Math.PI; };
      var av = A(v);

      var inner = '', cur = zones[0].color;
      for (i = 0; i < zones.length; i++) {
        var zz = zones[i];
        var a0 = A(zz.from), a1 = A(zz.to);
        if (av >= zz.to) {                                   // 整档已达成
          inner += pathEl(arcD(cx, cy, R, a0, a1), 'fill="none" stroke="' + zz.color + '" stroke-width="' + r2(th) + '" stroke-linecap="butt"');
        } else if (av <= zz.from) {                          // 整档未达成
          inner += pathEl(arcD(cx, cy, R, a0, a1), 'fill="none" stroke="' + zz.color + '" stroke-width="' + r2(th) + '" stroke-opacity="0.16" stroke-linecap="butt"');
        } else {                                             // 跨档：劈成两半
          inner += pathEl(arcD(cx, cy, R, a0, av), 'fill="none" stroke="' + zz.color + '" stroke-width="' + r2(th) + '" stroke-linecap="butt"');
          inner += pathEl(arcD(cx, cy, R, av, a1), 'fill="none" stroke="' + zz.color + '" stroke-width="' + r2(th) + '" stroke-opacity="0.16" stroke-linecap="butt"');
        }
        if (v >= zz.from && v <= zz.to) cur = zz.color;      // 当前所在档位 → 数字用它的颜色
      }
      // 指针端点
      var pv = polar(cx, cy, R, av);
      inner += circEl(pv.x, pv.y, Math.max(3.5, th * 0.42), C.surface, ' stroke="' + C.ink2 + '" stroke-width="1.6"');
      // 两端量程
      inner += T(cx - R, cy + 15, String(Math.round(mn * 100) / 100), { anchor: 'middle', size: 10, fill: C.ink4 });
      inner += T(cx + R, cy + 15, String(Math.round(mx * 100) / 100), { anchor: 'middle', size: 10, fill: C.ink4 });
      // 中央读数
      inner += T(cx, cy - S * 0.055, String(Math.round(v * 10) / 10),
        { anchor: 'middle', mid: true, size: Math.round(S * 0.21), fill: cur, weight: 600 });
      return wrap(S, Math.round(S * 0.70), inner);
    }, S, S * 0.7);
  }

  /* ══════════════════════ 11. drawdown 回撤（水下）曲线 ══════════════════════
     映射：0 在顶部（y = mT），谷底 m 在底部 —— y = [m, 0] → [底, 顶]，倒着映射。
     面积从 0 轴往下填，越深颜色越实（渐变），符合「水下」直觉。 */
  function drawdown(o) {
    o = o || {};
    var W = pos(o.width, 760), H = pos(o.height, 160);
    var color = tok(o.color) || C.green;
    return safe(function () {
      var vals = Array.isArray(o.values) ? o.values : [];
      var mn = 0, has = false, i;
      for (i = 0; i < vals.length; i++) {
        var v = fin(vals[i]);
        if (v === null) continue;
        has = true;
        mn = Math.min(mn, Math.min(0, v));
      }
      if (!has) return empty(W, H, '暂无回撤数据');
      if (mn === 0) mn = -0.001;
      mn *= 1.08;

      var mL = 46, mR = 10, mT = 10, mB = 16;
      var pw = Math.max(10, W - mL - mR), ph = Math.max(10, H - mT - mB);
      var n = vals.length;
      var X = linear(0, Math.max(1, n - 1), mL, mL + pw);
      var Y = linear(mn, 0, mT + ph, mT);

      var gid = uid('dd');
      var inner = '<defs>' + vGrad(gid, mT, mT + ph, color, 0.06, 0.22) + '</defs>';
      inner += lineEl(mL, Y(0), mL + pw, Y(0), C.line, 0.5);
      inner += T(mL - 6, Y(0), '0%', { anchor: 'end', mid: true, size: 10, fill: C.ink3 });
      inner += T(mL - 6, Y(mn / 2), fmtPct(mn / 2, Math.abs(mn) < 0.02 ? 2 : 1), { anchor: 'end', mid: true, size: 10, fill: C.ink3 });
      inner += T(mL - 6, Y(mn), fmtPct(mn, Math.abs(mn) < 0.02 ? 2 : 1), { anchor: 'end', mid: true, size: 10, fill: C.ink3 });

      var g = segs(vals, X, Y);              // 注意 yf 收到的是「未截断到 ≤0」的原值
      var lowPt = null, lowV = 0;
      for (i = 0; i < g.length; i++) {
        var pts = g[i], k, cl = [];
        for (k = 0; k < pts.length; k++) {
          var yv = Math.min(0, pts[k].v);
          cl.push({ x: pts[k].x, y: Y(yv), v: yv });
          if (yv < lowV) { lowV = yv; lowPt = { x: pts[k].x, y: Y(yv), v: yv }; }
        }
        if (cl.length === 1) { inner += circEl(cl[0].x, cl[0].y, 2, color); continue; }
        inner += pathEl(dArea(cl, Y(0)), 'fill="url(#' + gid + ')"');
        inner += pathEl(dLine(cl), 'fill="none" stroke="' + color + '" stroke-width="1.4" stroke-linejoin="round"');
      }
      // 最大回撤标注
      if (lowPt) {
        var anc = lowPt.x > W - 120 ? 'end' : 'start';
        inner += circEl(lowPt.x, lowPt.y, 2.6, color);
        inner += T(clamp(lowPt.x + (anc === 'end' ? -6 : 6), 4, W - 4), lowPt.y - 6,
          '最大回撤 ' + fmtPct(lowPt.v, 1), { anchor: anc, size: 10, fill: color, weight: 600, halo: true });
      }
      // 时间轴
      if (Array.isArray(o.dates) && o.dates.length) {
        var idx = pickIdx(Math.min(n, o.dates.length), 6);
        for (i = 0; i < idx.length; i++) {
          var an = i === 0 ? 'start' : (i === idx.length - 1 ? 'end' : 'middle');
          inner += T(clamp(X(idx[i]), 2, W - 2), H - 4, shortDate(o.dates[idx[i]]), { anchor: an, size: 10, fill: C.ink3 });
        }
      }
      return wrap(W, H, inner);
    }, W, H);
  }

  /* ══════════════════════ 12. waterfall 瀑布图 ══════════════════════
     映射：先把 items 累加成阶梯（起点/终点），domain 取所有阶梯端点的 [min,max]（含 0）。
     每根柱子的 y 从累计起点画到终点；柱子间用虚线连接，交代「上一根的终点 = 下一根的起点」。
     最后一根是合计柱（从 0 起），用中性色与前面区分。 */
  function waterfall(o) {
    o = o || {};
    var W = pos(o.width, 640), H = pos(o.height, 240);
    var posC = tok(o.posColor) || C.red, negC = tok(o.negColor) || C.green;
    var totalLabel = o.totalLabel === undefined || o.totalLabel === null ? '合计' : String(o.totalLabel);
    return safe(function () {
      var items = [], i;
      if (Array.isArray(o.items)) {
        for (i = 0; i < o.items.length; i++) {
          var it = o.items[i];
          if (!it || typeof it !== 'object') continue;
          var v = fin(it.value);
          items.push({ label: it.label === undefined || it.label === null ? '' : String(it.label), value: v === null ? 0 : v });
        }
      }
      if (!items.length) return empty(W, H, '暂无数据');

      var cols = items.length + 1, total = 0;
      var starts = [], ends = [];
      for (i = 0; i < items.length; i++) { starts.push(total); total += items[i].value; ends.push(total); }
      starts.push(0); ends.push(total);

      var lo = Math.min(0, total), hi = Math.max(0, total);
      for (i = 0; i < items.length; i++) { lo = Math.min(lo, starts[i], ends[i]); hi = Math.max(hi, starts[i], ends[i]); }
      if (hi === lo) { hi = lo + 1; }
      var padv = (hi - lo) * 0.10; lo -= padv; hi += padv;

      var colW = W / cols;
      var rot = colW < 66;
      var mB = rot ? 40 : 22, mT = 18, mL = 4, mR = 4;
      var ph = Math.max(20, H - mT - mB);
      var Y = linear(lo, hi, mT + ph, mT);
      var y0 = Y(0);

      var inner = lineEl(mL, y0, W - mR, y0, C.line, 0.5);
      var bw = Math.min(colW * 0.56, 46);
      var xc = function (k) { return colW * (k + 0.5); };

      for (i = 0; i < cols; i++) {
        var isTotal = i === items.length;
        var s0 = starts[i], e0 = ends[i];
        var col = isTotal ? C.ink2 : (items[i].value >= 0 ? posC : negC);
        var yTop = Y(Math.max(s0, e0)), yBot = Y(Math.min(s0, e0));
        var h = Math.max(2, Math.abs(yBot - yTop));
        inner += rectEl(xc(i) - bw / 2, isTotal ? Math.min(y0, yTop) : yTop, bw,
          isTotal ? Math.max(2, Math.abs(y0 - Y(e0))) : h, col, ' rx="1.5"');
        // 数值标注：涨柱标在柱顶上方，跌柱标在柱底下方
        var val = isTotal ? total : items[i].value;
        var vTop = isTotal ? Math.min(y0, Y(e0)) : yTop;
        var vBot = isTotal ? Math.max(y0, Y(e0)) : yBot;
        var vy = (val >= 0 ? vTop - 5 : vBot + 11);
        inner += T(xc(i), clamp(vy, 10, H - 2), fmtSigned(val, Math.abs(val) >= 100 ? 0 : 1),
          { anchor: 'middle', size: 10, fill: val === 0 ? C.ink3 : col, weight: 600 });
        // 柱间虚线：把上一根的终点牵到下一根的起点
        if (i < cols - 1) inner += lineEl(xc(i) + bw / 2, Y(ends[i]), xc(i + 1) - bw / 2, Y(starts[i + 1]), C.line2, 0.5, '2 2');
        // x 轴标签
        var lb = isTotal ? totalLabel : items[i].label;
        if (rot) inner += T(xc(i) + 5, H - mB + 15, clipText(lb, colW * 1.5, 10), { size: 10, fill: isTotal ? C.ink2 : C.ink3, font: C.sans, rot: -32, anchor: 'end' });
        else inner += T(xc(i), H - mB + 14, clipText(lb, colW - 6, 10.5), { anchor: 'middle', size: 10.5, fill: isTotal ? C.ink2 : C.ink3, font: C.sans });
      }
      return wrap(W, H, inner);
    }, W, H);
  }

  /* ══════════════════════ 13. metricBar 单值大数字条 ══════════════════════
     不是图表，是卡片顶部的指标行 —— 用同一套文字栈与网格节奏，保证和图表摆在一起不违和。
     布局：每列固定 128px；标签(10.5) / 数值(18，按列宽自动缩号，绝不溢出) / 副标(9.5)。 */
  function metricBar(o) {
    o = o || {};
    var colW = 128;
    return safe(function () {
      var items = [], i;
      if (Array.isArray(o.items)) {
        for (i = 0; i < o.items.length; i++) {
          var it = o.items[i];
          if (!it || typeof it !== 'object') continue;
          items.push({
            label: it.label === undefined || it.label === null ? '' : String(it.label),
            value: it.value === undefined || it.value === null ? '—' : it.value,
            sub: it.sub === undefined || it.sub === null ? '' : String(it.sub),
            tone: it.tone || 'muted'
          });
        }
      }
      if (!items.length) return empty(240, 52, '暂无数据');
      var W = items.length * colW, H = 52;
      var tone = { up: C.red, down: C.green, gold: C.gold, muted: C.ink2 };
      var inner = '';
      for (i = 0; i < items.length; i++) {
        var it2 = items[i], x = i * colW;
        if (i > 0) inner += lineEl(x, 9, x, H - 7, C.line, 1);
        var vs = typeof it2.value === 'number' ? String(Math.round(it2.value * 1000) / 1000) : String(it2.value);
        var size = 18, tw = textW(vs, size);
        if (tw > colW - 24) size = Math.max(11, Math.floor(size * (colW - 24) / tw));
        inner += T(x + 12, 16, clipText(it2.label, colW - 22, 10.5), { size: 10.5, fill: C.ink3, font: C.sans });
        inner += T(x + 12, 35, clipText(vs, colW - 22, size), { size: size, fill: tone[it2.tone] || C.ink, weight: 600 });
        if (it2.sub) inner += T(x + 12, 47, clipText(it2.sub, colW - 22, 9.5), { size: 9.5, fill: C.ink4, font: C.sans });
      }
      return wrap(W, H, inner);
    }, 240, 52);
  }

  /* ══════════════════════ 导出 ══════════════════════ */
  window.Charts = {
    version: '1.0.0',
    sparkline: sparkline,
    intraday: intraday,
    candles: candles,
    lines: lines,
    barRows: barRows,
    heatGrid: heatGrid,
    heatmap: heatmap,
    radar: radar,
    donut: donut,
    gauge: gauge,
    drawdown: drawdown,
    waterfall: waterfall,
    metricBar: metricBar
  };
})();
