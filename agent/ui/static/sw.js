// Service worker: keeps the UI shell and the (immutable, versioned) vendor bundle available offline.
// API calls are always network-only; model files are cached by Transformers.js itself (Cache API).
const VERSION = "workbench-ui-v9";
const SHELL = ["/", "/static/manifest.json", "/static/stt-worker.js", "/static/lib/marked.umd.js", "/favicon.ico", "/static/icons/icon-32.png", "/static/icons/icon-48.png", "/static/icons/icon-192.png", "/static/icons/icon-512.png"];

self.addEventListener("install", e => { e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/static/models/") || url.pathname === "/ca.crt") return;   // network only
  if (url.pathname.startsWith("/static/vendor/")) {                 // stale-while-revalidate: instant offline, refreshed after `support-ctl update`
    e.respondWith(caches.open(VERSION).then(async c => {
      const cached = await c.match(e.request);
      const fresh = fetch(e.request).then(r => { if (r.ok) c.put(e.request, r.clone()); return r; }).catch(() => cached);
      return cached || fresh;
    }));
    return;
  }
  // shell: network first, cache fallback (so an update is picked up when online)
  e.respondWith(fetch(e.request).then(r => { if (r.ok) caches.open(VERSION).then(c => c.put(e.request, r.clone())); return r; })
                .catch(() => caches.match(e.request).then(r => r || caches.match("/"))));
});
