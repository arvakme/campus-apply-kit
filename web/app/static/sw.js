/* 看板 PWA service worker。
 * 数据要实时：一律网络优先。只缓存静态资源和离线提示页；
 * 不缓存 /api/*、/raw/* 与任何含个人数据的 HTML 页面。
 * 发布新静态资源时把 VERSION 加一，旧缓存会在 activate 时清掉。
 */
const VERSION = 'v1';
const CACHE = 'campus-web-' + VERSION;
const OFFLINE = '/static/offline.html';
const PRECACHE = [
  OFFLINE,
  '/manifest.webmanifest',
  '/static/icon.svg',
];

self.addEventListener('install', (e) => {
  // 逐个缓存：某一项 404 不该让整个 service worker 装不上
  e.waitUntil(caches.open(CACHE).then((c) => Promise.all(PRECACHE.map((u) => c.add(u).catch(() => null)))).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // 页面导航：网络优先拿实时数据；连不上时给离线提示页，HTML 永不落缓存
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
    return;
  }

  // API、实例文件、SW 自身：直连
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/raw/') || url.pathname === '/sw.js') return;

  // 静态资源：网络优先，成功后回写缓存做离线兜底
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req).then((hit) => hit || Response.error()))
  );
});
