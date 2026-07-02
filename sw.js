// Elström Service Worker
// CACHE_NAME is injected automatically by scripts/inject-sw-version.mjs before each deploy
const CACHE_NAME = 'elstrom-c51c337';

// Only cache static assets — never HTML. index.html is always fetched
// fresh from the network so stale JS is never served from SW cache.
const PRECACHE = [
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

  // Page navigations — always network, no HTML caching.
  // Serving stale HTML is worse than a brief load — the app needs live data anyway.
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request));
  }
});
