/**
 * 研判进度条实拍：打开 #daily → 点「重跑当日」→ 在若干时间点截图。
 *
 * 为什么要脚本点按钮：进度面板只在**本页面发起**的运行中显示
 * （它由 SSE 事件驱动），直接打开页面是看不到的。
 *
 * 用法：node tools/shot/progress.mjs
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
const OUT = argOf('--out', path.join(process.env.TEMP || '.', 'imis_progress'));
const OFFLINE = args.includes('--offline');
// 截图时间点（毫秒）
const AT = (argOf('--at', '4000,12000,26000')).split(',').map(Number);

fs.mkdirSync(OUT, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: EDGE, headless: 'shell',
  args: ['--no-first-run', '--disable-gpu', '--hide-scrollbars',
         '--force-device-scale-factor=1'],
});
const page = await browser.newPage();
await page.setViewport({ width: 1280, height: 900 });

await page.goto(`${BASE.replace(/\/$/, '')}/#daily`, { waitUntil: 'networkidle2', timeout: 45000 });
await new Promise(r => setTimeout(r, 2000));

const btn = OFFLINE ? '#btn-daily-offline' : '#btn-daily-rerun';
console.log(`点击 ${btn}（${OFFLINE ? '离线' : '真实'}模式）…`);
await page.click(btn);

const t0 = Date.now();
for (const ms of AT) {
  const wait = ms - (Date.now() - t0);
  if (wait > 0) await new Promise(r => setTimeout(r, wait));
  const file = path.join(OUT, `prg_${String(ms).padStart(5, '0')}ms.png`);
  // 截到进度面板为止（连同上方标题），不用整页
  const el = await page.$('#daily-progress');
  if (el) {
    const box = await el.boundingBox();
    await page.screenshot({
      path: file,
      clip: { x: 0, y: Math.max(0, (box?.y || 90) - 60),
              width: 1280, height: Math.min(420, 900) },
    });
  } else {
    await page.screenshot({ path: file });
  }
  const info = await page.evaluate(() => ({
    pct: document.querySelector('#prg-pct')?.textContent,
    label: document.querySelector('#prg-label')?.textContent,
    sub: document.querySelector('#prg-sub')?.textContent,
    detail: document.querySelector('#prg-detail')?.textContent,
    elapsed: document.querySelector('#prg-elapsed')?.textContent,
    steps: Array.from(document.querySelectorAll('.prg-step'))
      .map(s => `${s.textContent.trim()}${s.classList.contains('done') ? '[✓]'
        : s.classList.contains('active') ? '[▶]' : ''}`).join(' '),
    hidden: document.querySelector('#daily-progress')?.hidden,
  }));
  console.log(`  [${((Date.now() - t0) / 1000).toFixed(1)}s] ${info.pct}%  ${info.label}${info.sub || ''}`
    + `  ${info.elapsed || ''}\n        ${info.detail || ''}\n        ${info.steps}`);
  console.log(`        → ${file}`);
}

await browser.close();
console.log(`\n截图目录：${OUT}`);
