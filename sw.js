// Elström Service Worker
// CACHE_NAME is injected automatically by scripts/inject-sw-version.mjs before each deploy
const CACHE_NAME = 'elstrom-c51c337';

const PRECACHE = [
  '/',
  '/index.html',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
];

// Install: precache shell
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

// Activate: clear old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// Fetch: never cache API responses — always network
// Cache static assets (icons, fonts) cache-first
// Page navigations: network-first, offline fallback
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  if (request.method !== 'GET') return;

  // Never intercept API calls — dashboard needs live data
  if (url.pathname.startsWith('/api/')) return;

  // Cross-origin (fonts, Chart.js CDN) — network only
  if (url.origin !== self.location.origin) return;

  // Static assets (icons, images) — cache-first
  if (url.pathname.match(/\.(png|jpg|svg|ico|woff2?)$/)) {
    event.respondWith(
      caches.match(request).then(cached => {
        if (cached) return cached;
        return fetch(request).then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then(c => c.put(request, clone));
          }
          return response;
        });
      })
    );
    return;
  }

  // Page navigations — network-first, offline fallback to cached shell
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match('/index.html') ||
        new Response(
          '<html><body style="font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#0a0c0f;color:#e2e8f0"><div style="text-align:center"><div style="font-size:48px;margin-bottom:16px">⚡</div><h2 style="margin:0 0 8px;color:#f59e0b">Offline</h2><p style="opacity:0.6;margin:0">Kontrollera din anslutning</p></div></body></html>',
          { headers: { 'Content-Type': 'text/html' } }
        )
      )
    );
  }
});
