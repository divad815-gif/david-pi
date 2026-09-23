const CACHE = 'david-pi-static-v44-portable-households';
const OFFLINE_AUDIOBOOK_PAGE = '/static/audiobooks-offline.html';
const AUDIOBOOK_PAGE = '/audiobooks';
const OFFLINE_AUDIOBOOK_ROUTE = '/__davidpi_offline/audiobooks/';
const OFFLINE_AUDIOBOOK_ROOT = 'david-pi-audiobooks-v1';
const OFFLINE_AUDIOBOOK_ID = /^[0-9a-f]{32}$/;
const OFFLINE_AUDIOBOOK_SHA256 = /^[0-9a-f]{64}$/;
const SHELL_REFRESH_TIMEOUT_MS = 4000;
const SHELL = [
  '/david-pi-icon-192.png', '/david-pi-icon-512.png',
  OFFLINE_AUDIOBOOK_PAGE, '/static/installation.js?v=2',
  '/static/app.css?v=40', '/static/platform.css?v=25', '/static/audiobooks-offline.css?v=7',
  '/static/david-pi-ui.css?v=16', '/static/david-pi-ui.js?v=15', '/static/theme-bootstrap.js?v=8',
  '/static/audiobook-progress.js?v=4', '/static/audiobook-continuity.js?v=2', '/static/sha256-stream.js?v=1', '/static/audiobook-offline-web.js?v=10',
  '/static/audiobook-ui-safety.js?v=3', '/static/mobile-dialog-host.js?v=10', '/static/audiobook-shelf.css?v=6',
  '/static/audiobooks.js?v=31', '/static/audiobooks-offline-page.js?v=9', '/static/audiobook-offline-worker.js?v=1'
];
self.addEventListener('install', (event) => event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate', (event) => event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key.startsWith('david-pi-static-') && key !== CACHE).map((key) => caches.delete(key)))).then(() => self.clients.claim())));

async function boundedShellFetch(request) {
  const controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort('shell_refresh_timeout'),SHELL_REFRESH_TIMEOUT_MS);
  try{return await fetch(request,{signal:controller.signal});}
  finally{clearTimeout(timeout);}
}

function cacheFirstShell(event, cacheKey) {
  const cachePromise=caches.open(CACHE);
  const refresh=Promise.all([cachePromise,boundedShellFetch(event.request)]).then(async([cache,response])=>{
    if(response.ok)await cache.put(cacheKey,response.clone());
    return response;
  });
  // Refresh in the background, but never make an already-saved local player
  // wait for a weak network connection.
  event.waitUntil(refresh.catch(()=>undefined));
  return cachePromise.then(cache=>cache.match(cacheKey)).then(cached=>cached||refresh);
}

async function networkFirstAudiobookPage(request) {
  const cache=await caches.open(CACHE);
  try{
    // A weak cellular/Tailscale path must not leave the library navigation
    // spinning indefinitely when a complete local copy is already available.
    const response=await boundedShellFetch(request);
    // Never persist the personalized page: it contains a CSRF token and an
    // identity-scoped progress namespace. Retryable server failures use the
    // content-neutral offline shell, while auth failures remain explicit.
    if(response.status>=500){
      const fallback=await cache.match(OFFLINE_AUDIOBOOK_PAGE);
      return fallback||response;
    }
    return response;
  }catch(error){
    const fallback=await cache.match(OFFLINE_AUDIOBOOK_PAGE);
    if(fallback)return fallback;
    throw error;
  }
}

function offlineRange(value, total) {
  if (!value) return {start: 0, end: total - 1, partial: false};
  if (!value.startsWith('bytes=') || value.includes(',')) return null;
  const match = /^bytes=(\d*)-(\d*)$/.exec(value);
  if (!match || (!match[1] && !match[2])) return null;
  let start;
  let end;
  if (!match[1]) {
    const suffix = Number(match[2]);
    if (!Number.isSafeInteger(suffix) || suffix <= 0) return null;
    start = Math.max(0, total - suffix);
    end = total - 1;
  } else {
    start = Number(match[1]);
    end = match[2] ? Number(match[2]) : total - 1;
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end < start) return null;
    end = Math.min(end, total - 1);
  }
  if (start >= total) return null;
  return {start, end, partial: true};
}

async function offlineAudiobookResponse(request, url) {
  const id = url.pathname.slice(OFFLINE_AUDIOBOOK_ROUTE.length);
  if (!OFFLINE_AUDIOBOOK_ID.test(id) || id.includes('/')) return new Response('Not found', {status: 404});
  try {
    const storageRoot = await self.navigator.storage.getDirectory();
    const directory = await storageRoot.getDirectoryHandle(OFFLINE_AUDIOBOOK_ROOT, {create: false});
    const recordHandle = await directory.getFileHandle(`${id}.json`, {create: false});
    const record = JSON.parse(await (await recordHandle.getFile()).text());
    const sha256 = String(record?.content_sha256 || '');
    if (
      record?.version !== 3 || record?.state !== 'complete'
      || record?.book?.id !== id || record?.book?.visibility !== 'shared'
      || record?.book?.content_sha256 !== sha256 || !OFFLINE_AUDIOBOOK_SHA256.test(sha256)
      || !Number.isSafeInteger(record?.integrity_verified_at) || record.integrity_verified_at <= 0
      || ![`${id}.audio.part`, `${id}.audio`].includes(record?.file_name)
    ) return new Response('Offline copy is incomplete', {status: 404});
    const file = await (await directory.getFileHandle(record.file_name, {create: false})).getFile();
    if (!Number.isSafeInteger(record.expected_bytes) || record.expected_bytes <= 0 || file.size !== record.expected_bytes) {
      return new Response('Offline copy is incomplete', {status: 404});
    }
    const range = offlineRange(request.headers.get('range'), file.size);
    const common = {
      'Accept-Ranges': 'bytes',
      'Cache-Control': 'no-store',
      'Content-Type': String(record.book.content_type || 'application/octet-stream').replace(/[\r\n]/g, '').slice(0, 120),
      'ETag': `"sha256-${sha256}"`,
      'X-Content-Type-Options': 'nosniff',
    };
    if (!range) return new Response(null, {status: 416, headers: {...common, 'Content-Range': `bytes */${file.size}`}});
    const length = range.end - range.start + 1;
    const headers = {...common, 'Content-Length': String(length)};
    if (range.partial) headers['Content-Range'] = `bytes ${range.start}-${range.end}/${file.size}`;
    const body = request.method === 'HEAD' ? null : file.slice(range.start, range.end + 1);
    return new Response(body, {status: range.partial ? 206 : 200, headers});
  } catch (_error) {
    return new Response('Offline copy unavailable', {status: 404, headers: {'Cache-Control': 'no-store'}});
  }
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.origin === self.location.origin && url.pathname.startsWith(OFFLINE_AUDIOBOOK_ROUTE)) {
    if (event.request.method === 'GET' || event.request.method === 'HEAD') event.respondWith(offlineAudiobookResponse(event.request, url));
    else event.respondWith(Promise.resolve(new Response('Method not allowed', {status: 405, headers: {Allow: 'GET, HEAD'}})));
    return;
  }
  if (event.request.method !== 'GET') return;
  // Authentication and row visibility are evaluated by the origin. Keep this
  // guard ahead of every CacheStorage lookup so authenticated media can never
  // become part of the offline shell, even if a future shell list is wrong.
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/') || url.pathname.startsWith('/media/')) {
    event.respondWith(fetch(event.request));
    return;
  }
  if(url.pathname===AUDIOBOOK_PAGE&&event.request.mode==='navigate'){
    event.respondWith(networkFirstAudiobookPage(event.request));
    return;
  }
  const requestedShell=`${url.pathname}${url.search}`;
  const cacheKey=url.pathname === OFFLINE_AUDIOBOOK_PAGE && event.request.mode === 'navigate'
    ? OFFLINE_AUDIOBOOK_PAGE
    : (SHELL.includes(requestedShell) ? requestedShell : '');
  if(cacheKey){
    event.respondWith(cacheFirstShell(event,cacheKey));
    return;
  }
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request));
    return;
  }
  if (url.pathname.startsWith('/static/') || url.pathname.startsWith('/david-pi-icon-')) {
    event.respondWith(caches.open(CACHE).then(async(cache)=>{
      const cached=await cache.match(event.request);
      try {
        const response=await fetch(event.request);
        if(response.ok)await cache.put(event.request,response.clone());
        return response;
      } catch (_error) {
        if(cached)return cached;
        throw _error;
      }
    }));
  }
});
self.addEventListener('push', (event) => {
  let data={title:'Home server',url:'/chat'};
  try { data={...data,...event.data.json()}; } catch (_error) {}
  const title=typeof data.title==='string' && data.title.trim() ? data.title.slice(0,80) : 'Home server';
  event.waitUntil(self.registration.showNotification(title,{
    body:'New household message',icon:'/david-pi-icon-192.png',badge:'/david-pi-icon-192.png',
    tag:'david-pi-chat',renotify:true,data:{url:String(data.url||'/chat')}
  }));
});
self.addEventListener('notificationclick',(event)=>{
  event.notification.close();
  let target=new URL('/chat',self.location.origin).href;
  try {const requested=new URL(event.notification.data?.url||'/chat',self.location.origin);if(requested.origin===self.location.origin && /^\/chat(?:\/[^/?#]+)?$/.test(requested.pathname))target=requested.href;}catch(_){}
  event.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(list=>{
    for(const client of list){if('focus'in client){client.navigate(target);return client.focus();}}
    return clients.openWindow(target);
  }));
});
