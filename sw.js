// Yearly Archive app: works offline with the last paper list and the TOC pictures you have seen.
const VERSION = "archive-app-v3";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icons/apple-touch-icon.png", "icons/icon-192.png"];
const PICS = "archive-pics-v1", MAX_PICS = 500;

self.addEventListener("install", e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== VERSION && k !== PICS).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const res = await fetch(req, {cache: "no-store"});
    if (res.ok) cache.put(req.url.split("?")[0], res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(req.url.split("?")[0]);
    if (hit) return hit;
    throw err;
  }
}
async function cacheFirst(req) {
  const cache = await caches.open(PICS);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok || res.type === "opaque") {
    await cache.put(req, res.clone());
    const keys = await cache.keys();                      // keep the newest MAX_PICS pictures
    for (let i = 0; i < keys.length - MAX_PICS; i++) await cache.delete(keys[i]);
  }
  return res;
}

self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET") return;
  if (url.origin !== location.origin) {                 // publisher TOC pictures (Nature, Elsevier…): keep a copy
    if (req.destination === "image") e.respondWith(cacheFirst(req).catch(() => fetch(req)));
    return;
  }
  if (url.pathname.includes("/data/toc/")) return e.respondWith(cacheFirst(req));
  if (url.pathname.endsWith("/data/papers.json")) return e.respondWith(networkFirst(req, VERSION));
  if (req.mode === "navigate") return e.respondWith(networkFirst(req, VERSION).catch(() => caches.match("index.html")));
  if (url.pathname.match(/\.(webmanifest|png|html|js)$/)) return e.respondWith(networkFirst(req, VERSION));
});
