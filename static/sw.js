const CACHE = 'david-pi-static-v9-18-5-ui-metadata';
const SHELL = ['/david-pi-icon-192.png', '/david-pi-icon-512.png'];
self.addEventListener('install', (event) => event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate', (event) => event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))).then(() => self.clients.claim())));
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || event.request.mode === 'navigate' || url.pathname.startsWith('/api/') || url.pathname.startsWith('/media/')) {
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
  let data={title:'David-Pi',body:'New David-Pi message',url:'/chat'};
  try { data={...data,...event.data.json()}; } catch (_error) {}
  event.waitUntil(self.registration.showNotification('David-Pi',{
    body:'New David-Pi message',icon:'/david-pi-icon-192.png',badge:'/david-pi-icon-192.png',
    tag:'david-pi-chat',renotify:true,data:{url:String(data.url||'/chat')}
  }));
});
self.addEventListener('notificationclick',(event)=>{
  event.notification.close();
  const target=new URL(event.notification.data?.url||'/chat',self.location.origin).href;
  event.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(list=>{
    for(const client of list){if('focus'in client){client.navigate(target);return client.focus();}}
    return clients.openWindow(target);
  }));
});
