/**
 * 图表渲染审计：在真实页面上检查有没有「因 CSS 变量未定义而退化成黑色」的图元。
 *
 * 为什么要这个：SVG 的 fill/stroke 写成 var(--x) 时，若 --x 未定义，
 * var() 会在**计算值阶段**失效，颜色回退到初始值黑色。黑色折线在浅色看板上
 * 很扎眼，但在深色或灰度截图里不一定能一眼看出来 —— 用程序判定才可靠。
 *
 * 用法：
 *   node tools/shot/audit.mjs                    # 审计全部视图
 *   node tools/shot/audit.mjs --url http://...   # 指定地址
 */
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer-core';

const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(p => { try { return fs.existsSync(p); } catch { return false; } });

const args = process.argv.slice(2);
const argOf = (k, d = '') => {
  const i = args.indexOf(k);
  return i >= 0 && args[i + 1] ? args[i + 1] : d;
};
const BASE = argOf('--url', 'http://127.0.0.1:8760/');

const VIEWS = ['board', 'daily', 'family', 'records', 'connect'];
// 家庭投资的子页签：需要先载入一次审议结果（?run=），否则是空态、没有图可审。
// 早期只审一级视图，于是新加的「配置方案 / 组合明细 / 团队分工」三页里的
// 环形图、散点、瀑布图从未被这条检查覆盖过。
const SUBVIEWS = ['plan', 'detail', 'team', 'stress', 'debate'];
const LIST = [...VIEWS.map(v => ({ hash: v, run: false })),
              ...SUBVIEWS.map(v => ({ hash: v, run: true }))];

let runId = '';
try {
  const r = await fetch(new URL('/api/runs', BASE));
  const j = await r.json();
  runId = j.runs?.[0]?.run_id || '';
} catch { /* 没有历史记录时子视图会显示空态 */ }

const browser = await puppeteer.launch({
  executablePath: EDGE, headless: 'shell',
  args: ['--no-first-run', '--disable-gpu', '--force-device-scale-factor=1'],
});

let totalBad = 0;
let totalEl = 0;

for (const s of LIST) {
  const v = s.hash;
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1400 });
  try {
    const q = (s.run && runId) ? `?run=${encodeURIComponent(runId)}` : '';
    await page.goto(`${BASE.replace(/\/$/, '')}/${q}#${v}`,
                    { waitUntil: 'networkidle2', timeout: 45000 });
    await new Promise(r => setTimeout(r, 3000));

    const report = await page.evaluate(() => {
      const isBlack = (c) => /^rgba?\(\s*0\s*,\s*0\s*,\s*0\s*(,\s*1\s*)?\)$/.test((c || '').trim());
      // 关键：只有「真的会被 fill / stroke 绘制」的元素才算。
      // <line> 没有面积（fill 无意义）、<g>/<title>/<defs> 是容器与元数据，
      // 它们的 computed fill 一律是默认黑，但屏幕上不会画出任何黑色 ——
      // 不加白名单会得到上千个假阳性。
      const PAINTS_FILL = new Set(['path', 'rect', 'circle', 'ellipse', 'polygon',
                                   'polyline', 'text', 'tspan', 'use', 'textpath']);
      const PAINTS_STROKE = new Set(['path', 'line', 'rect', 'circle', 'ellipse',
                                     'polygon', 'polyline', 'text', 'tspan']);
      const bad = [];
      let n = 0, hidden = 0;
      document.querySelectorAll('svg *').forEach(el => {
        const tag = el.tagName.toLowerCase();
        const painted = PAINTS_FILL.has(tag) || PAINTS_STROKE.has(tag);
        if (!painted) return;
        // 可见性过滤必须在计数**之前**：早期 n++ 写在前面，于是隐藏面板里的
        // 图元也被计入，导致每个子视图的统计一模一样、数字失去意义。
        const box = el.getBoundingClientRect();
        if (box.width === 0 && box.height === 0) { hidden++; return; }
        if (parseFloat(getComputedStyle(el).opacity) === 0) { hidden++; return; }
        n++;
        const cs = getComputedStyle(el);

        if (PAINTS_FILL.has(tag) && cs.fill && cs.fill !== 'none' && isBlack(cs.fill)) {
          bad.push({ tag, attr: 'fill', cls: (el.getAttribute('class') || '').slice(0, 30) });
        }
        if (PAINTS_STROKE.has(tag) && cs.stroke && cs.stroke !== 'none' && isBlack(cs.stroke)) {
          bad.push({ tag, attr: 'stroke', cls: (el.getAttribute('class') || '').slice(0, 30) });
        }
      });
      return { n, hidden, bad: bad.slice(0, 12), nBad: bad.length,
               svgCount: document.querySelectorAll('svg').length };
    });

    totalBad += report.nBad;
    totalEl += report.n;
    const flag = report.nBad ? '❌' : '✅';
    console.log(`  ${flag} ${v.padEnd(9)} 可见图元 ${String(report.n).padStart(4)} 个`
      + `（另有 ${report.hidden} 个在隐藏面板中，未计），黑色 ${report.nBad} 个`);
    if (report.nBad) report.bad.forEach(b => console.log(`       · <${b.tag}> ${b.attr} 黑色 ${b.cls}`));
  } catch (e) {
    console.log(`  ❌ ${v.padEnd(9)} 审计失败：${e.message}`);
  }
  await page.close();
}

await browser.close();
console.log(`\n合计：${totalEl} 个图元，${totalBad} 个渲染成黑色`
  + (totalBad ? '  ← 有 CSS 变量未定义' : '  ← 全部变量均已定义'));
process.exit(totalBad ? 1 : 0);
