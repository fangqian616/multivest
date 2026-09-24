/**
 * 截图工具：用 puppeteer-core 驱动本机已有的 Edge 渲染页面。
 *
 * 为什么不用 `msedge --headless --screenshot`：那条路在本机极不稳定，
 * 同一命令时好时坏，且失败时没有任何错误输出。走 CDP 可控得多。
 *
 * 用法：
 *   node tools/shot/shot.mjs                       # 用最近一次审议记录
 *   node tools/shot/shot.mjs --run <run_id>
 *   node tools/shot/shot.mjs --url http://127.0.0.1:8760/
 */
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer-core';

const EDGE = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  path.join(process.env.LOCALAPPDATA || '', 'Microsoft\\Edge\\Application\\msedge.exe'),
].find(p => { try { return fs.existsSync(p); } catch { return false; } });

const args = process.argv.slice(2);
const argOf = (k, d = '') => {
  const i = args.indexOf(k);
  return i >= 0 && args[i + 1] ? args[i + 1] : d;
};
const BASE = argOf('--url', 'http://127.0.0.1:8760/');
const OUT = argOf('--out', path.join(process.env.TEMP || '.', 'hw_shots'));
const ONLY = argOf('--only', '');

let runId = argOf('--run', '');
if (!runId && !args.includes('--url')) {
  try {
    const r = await fetch(new URL('/api/runs', BASE));
    const j = await r.json();
    runId = j.runs?.[0]?.run_id || '';
  } catch { /* 无记录则只截空页 */ }
}

const SHOTS = [
  { name: 'board',        hash: 'board',   h: 3400, w: 1600 },
  { name: 'daily',        hash: 'daily',   h: 3800, w: 1600 },
  { name: 'family',       hash: 'family',  h: 2600, w: 1600 },
  { name: 'plan',         hash: 'plan',    h: 4200, w: 1600, run: true },
  { name: 'detail',       hash: 'detail',  h: 2600, w: 1600, run: true },
  { name: 'team',         hash: 'team',    h: 3600, w: 1600, run: true },
  { name: 'records',      hash: 'records', h: 1500, w: 1600 },
  { name: 'connect',      hash: 'connect', h: 1300, w: 1600 },
  { name: 'mobile-board', hash: 'board',   h: 2400, w: 430, mobile: true },
  { name: 'mobile-daily', hash: 'daily',   h: 3200, w: 430, mobile: true },
  { name: 'mobile-detail', hash: 'detail', h: 2600, w: 430, mobile: true, run: true },
];

if (!EDGE) { console.error('未找到 Edge'); process.exit(1); }
fs.mkdirSync(OUT, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: EDGE,
  headless: 'shell',
  args: ['--no-first-run', '--no-default-browser-check', '--disable-gpu',
         '--hide-scrollbars', '--disable-extensions', '--force-device-scale-factor=1'],
});

let ok = 0;
for (const s of SHOTS) {
  if (ONLY && s.name !== ONLY) continue;
  const page = await browser.newPage();
  await page.setViewport({
    width: s.w, height: s.h,
    deviceScaleFactor: s.mobile ? 2 : 1,
    isMobile: !!s.mobile,
    hasTouch: !!s.mobile,
  });
  // file:// 是文件路径而不是 URL 路由，拼 "/" 与 "#hash" 会把它当成目录
  // （相对引用的 charts.js 就会 404）。对本地文件只做 hash 定位。
  const isFile = BASE.startsWith('file://');
  // run:true 的视图需要先载入一次审议结果（?run=<id>），否则是空态
  const needRun = s.run && runId && !isFile;
  const q = needRun ? `?run=${encodeURIComponent(runId)}` : '';
  const base = isFile ? BASE : `${BASE.replace(/\/$/, '')}/${q}`;
  const url = `${base}#${s.hash}`;
  try {
    await page.goto(url, { waitUntil: isFile ? 'load' : 'networkidle2', timeout: 45000 });
    // 等动画与异步渲染落定
    await new Promise(r => setTimeout(r, 1600));
    const file = path.join(OUT, `${s.name}.png`);
    await page.screenshot({ path: file, fullPage: false });
    const kb = Math.round(fs.statSync(file).size / 1024);
    console.log(`  ✅ ${s.name.padEnd(12)} ${s.w}×${s.h}  ${kb} KB`);
    ok++;
  } catch (e) {
    console.log(`  ❌ ${s.name.padEnd(12)} ${e.message}`);
  }
  await page.close();
}

await browser.close();
console.log(`\n完成 ${ok}/${ONLY ? 1 : SHOTS.length}  →  ${OUT}`);
