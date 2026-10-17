/* Service worker do Diário Virtual.
   - Página: rede primeiro; sem rede (ou se o servidor demorar, como no plano gratuito do Render
     ao acordar), abre a última versão salva.
   - APIs de notícias: rede primeiro; sem rede ou com rede lenta, mostra a última resposta salva.
   - Arquivos estáticos e CDNs (fontes, ícones): servidos do cache e atualizados em segundo plano.
   - Tudo o mais (Supabase, imagens de terceiros, POST) passa direto, sem cache. */
const VERSION = 'v1';
const SHELL = 'diario-shell-' + VERSION;
const RUNTIME = 'diario-runtime-' + VERSION;
const API = 'diario-api-' + VERSION;
const KEEP = [SHELL, RUNTIME, API];
const SHELL_URLS = ['/', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png'];
const CDN_HOSTS = ['cdnjs.cloudflare.com', 'cdn.jsdelivr.net', 'fonts.googleapis.com', 'fonts.gstatic.com'];
const API_CACHED = ['/api/feeds', '/api/news', '/api/article', '/api/rss', '/api/providers', '/api/config'];
const API_MAX = 80;
const SLOW_MS = 5000;   /* passou disso, e havendo cópia salva, usa a cópia */

/* Resolve com o resultado, ou com null se falhar/estourar o tempo */
function withTimeout(promise, ms) {
  return new Promise(resolve => {
    const t = setTimeout(() => resolve(null), ms);
    promise.then(v => { clearTimeout(t); resolve(v); }, () => { clearTimeout(t); resolve(null); });
  });
}

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL);
    await Promise.all(SHELL_URLS.map(u => cache.add(u).catch(() => {})));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter(n => !KEEP.includes(n)).map(n => caches.delete(n)));
    await self.clients.claim();
  })());
});

async function trim(cacheName, max) {
  const cache = await caches.open(cacheName);
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - max; i++) await cache.delete(keys[i]);
}

function offlineJson(status, obj) {
  return new Response(JSON.stringify(obj), {
    status, headers: { 'Content-Type': 'application/json; charset=utf-8' }
  });
}

async function networkFirstPage(request) {
  const cache = await caches.open(SHELL);
  const netP = fetch(request).then(res => {
    if (res && res.ok) cache.put('/', res.clone());   /* a rede segue em segundo plano e atualiza a cópia */
    return res;
  });
  const hit = await cache.match('/', { ignoreSearch: true });
  if (!hit) {
    try { return await netP; } catch (e) {
      return new Response(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
        '<title>Sem conexão</title><body style="font-family:sans-serif;padding:2rem"><h1>Sem conexão</h1>' +
        '<p>Conecte-se à internet e abra o Diário Virtual de novo.</p>',
        { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
    }
  }
  const res = await withTimeout(netP, SLOW_MS);
  return (res && res.status < 500) ? res : hit;
}

async function networkFirstApi(request) {
  const cache = await caches.open(API);
  const netP = fetch(request).then(async res => {
    if (res && res.ok) { await cache.put(request, res.clone()); trim(API, API_MAX); }
    return res;
  });
  const hit = await cache.match(request);
  if (!hit) {
    try { return await netP; } catch (e) { return offlineJson(503, { error: 'sem conexão' }); }
  }
  const res = await withTimeout(netP, SLOW_MS);
  /* erros 4xx (chave inválida, cota) passam direto: o app decide o que fazer; só 5xx/lentidão usam a cópia */
  return (res && res.status < 500) ? res : hit;
}

async function staleWhileRevalidate(request, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  const update = fetch(request).then(res => {
    if (res && (res.ok || res.type === 'opaque')) cache.put(request, res.clone());
    return res;
  }).catch(() => null);
  return hit || (await update) || Response.error();
}

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    if (url.pathname === '/api/ping') {
      /* Sem rede, o app continua em "modo servidor" e lê as respostas salvas */
      event.respondWith(withTimeout(fetch(req), SLOW_MS).then(r =>
        (r && r.status < 500) ? r : offlineJson(200, { ok: true, offline: true })));
      return;
    }
    if (API_CACHED.includes(url.pathname)) { event.respondWith(networkFirstApi(req)); return; }
    if (url.pathname.startsWith('/api/')) return;
    if (req.mode === 'navigate') { event.respondWith(networkFirstPage(req)); return; }
    event.respondWith(staleWhileRevalidate(req, SHELL));
    return;
  }
  if (CDN_HOSTS.includes(url.hostname)) event.respondWith(staleWhileRevalidate(req, RUNTIME));
});
