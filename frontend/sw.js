/* 智能多维投资系统 · Service Worker
   策略：只缓存应用外壳（HTML/CSS/JS/图标），绝不缓存 /api/*。
   审议过程与结果必须实时来自后端，缓存会导致手机上看到过期方案。 */
const CACHE = 'multivest-shell-v1';
const SHELL = [
  '/', '/style.css', '/app.js', '/manifest.webmanifest',
  '/assets/icon-192.png', '/assets/icon-512.png',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL).catch(() => {})).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // API 与二维码一律走网络
  if (url.pathname.startsWith('/api/')) return;
  if (e.request.method !== 'GET') return;

  // 外壳：网络优先，失败回落缓存（保证改动能立刻生效）
  e.respondWith(
    fetch(e.request)
      .then(res => {
        if (res && res.ok && url.origin === location.origin) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(e.request).then(r => r || caches.match('/')))
  );
});
