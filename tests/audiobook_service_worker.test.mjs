import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../static/sw.js', import.meta.url), 'utf8');

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
}

function response(label) {
  return {label, ok: true, clone: () => response(label)};
}

function harness({entries = new Map(), fetchImpl, refreshTimeoutMs = 4000, offlineRecord = null, offlineBytes = null}) {
  const handlers = new Map();
  const calls = {matches: [], puts: [], fetches: [], fetchOptions: []};
  const cache = {
    addAll: async () => undefined,
    match: async key => {
      calls.matches.push(key);
      return entries.get(typeof key === 'string' ? key : key.url);
    },
    put: async (key, value) => {
      calls.puts.push([key, value.label]);
      entries.set(typeof key === 'string' ? key : key.url, value);
    },
  };
  const clients = {claim: async () => undefined, matchAll: async () => [], openWindow: async () => undefined};
  const self = {
    location: {origin: 'https://david-pi.test'},
    clients,
    registration: {showNotification: async () => undefined},
    skipWaiting: async () => undefined,
    addEventListener: (name, listener) => handlers.set(name, listener),
  };
  if (offlineRecord && offlineBytes) {
    const audio = new Blob([offlineBytes], {type: offlineRecord.book.content_type});
    const recordFile = {text: async () => JSON.stringify(offlineRecord)};
    const directory = {getFileHandle: async name => ({getFile: async () => name.endsWith('.json') ? recordFile : audio})};
    self.navigator = {storage: {getDirectory: async () => ({getDirectoryHandle: async () => directory})}};
  }
  const context = {
    URL, Response, Headers, Blob,
    AbortController,
    setTimeout,
    clearTimeout,
    self,
    clients,
    caches: {
      open: async () => cache,
      keys: async () => [],
      delete: async () => true,
    },
    fetch: async (request, options) => {
      calls.fetches.push(request.url);
      calls.fetchOptions.push(options);
      return fetchImpl(request, options);
    },
  };
  vm.runInNewContext(
    source.replace('const SHELL_REFRESH_TIMEOUT_MS = 4000;', `const SHELL_REFRESH_TIMEOUT_MS = ${refreshTimeoutMs};`),
    context,
  );
  function dispatch(request) {
    const waits = [];
    let responsePromise;
    handlers.get('fetch')({
      request,
      respondWith: value => { responsePromise = Promise.resolve(value); },
      waitUntil: value => waits.push(Promise.resolve(value)),
    });
    return {responsePromise, waits};
  }
  return {calls, dispatch};
}

test('saved audiobook shell responds from cache while a weak network refresh stays bounded in the background', async () => {
  const network = deferred();
  const entries = new Map([['/static/audiobooks-offline.html', response('cached')]]);
  const {calls, dispatch} = harness({entries, fetchImpl: () => network.promise});
  const event = dispatch({
    method: 'GET',
    mode: 'navigate',
    url: 'https://david-pi.test/static/audiobooks-offline.html?source=library',
  });
  const winner = await Promise.race([
    event.responsePromise.then(value => value.label),
    new Promise(resolve => setTimeout(() => resolve('network-delay'), 50)),
  ]);
  assert.equal(winner, 'cached');
  assert.deepEqual(calls.matches, ['/static/audiobooks-offline.html']);
  assert.equal(calls.puts.length, 0);

  network.resolve(response('fresh'));
  await Promise.all(event.waits);
  assert.deepEqual(calls.puts, [['/static/audiobooks-offline.html', 'fresh']]);
});

test('the normal audiobook page is never cached because it contains identity and CSRF state', async () => {
  const entries = new Map();
  const online = harness({entries, fetchImpl: async () => response('online-library')});
  const request = {method: 'GET', mode: 'navigate', url: 'https://david-pi.test/audiobooks'};
  assert.equal((await online.dispatch(request).responsePromise).label, 'online-library');
  assert.deepEqual(online.calls.puts, []);
  assert.equal(entries.has('/audiobooks'), false);
});

test('an old identity page in CacheStorage is ignored during identity rotation and offline fallback', async () => {
  const entries = new Map([
    ['/audiobooks', response('david-personalized-page')],
    ['/static/audiobooks-offline.html', response('content-neutral-offline-shell')],
  ]);
  const offline = harness({entries, fetchImpl: async () => { throw new Error('offline'); }});
  const request = {method: 'GET', mode: 'navigate', url: 'https://david-pi.test/audiobooks'};
  assert.equal((await offline.dispatch(request).responsePromise).label, 'content-neutral-offline-shell');
  assert.deepEqual(offline.calls.matches, ['/static/audiobooks-offline.html']);
});

test('a weak normal-page connection falls back to the content-neutral offline shell at the hard deadline', async () => {
  const entries = new Map([['/static/audiobooks-offline.html', response('offline-shell')]]);
  const {dispatch} = harness({
    entries,
    refreshTimeoutMs: 5,
    fetchImpl: (_request, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new Error('aborted')), {once: true});
    }),
  });
  const started = Date.now();
  const page = await dispatch({method: 'GET', mode: 'navigate', url: 'https://david-pi.test/audiobooks'}).responsePromise;
  assert.equal(page.label, 'offline-shell');
  assert.ok(Date.now() - started < 100, 'offline shell should win promptly after the bounded wait');
});

test('retryable server errors fall back but authentication failures remain explicit', async () => {
  const entries = new Map([['/static/audiobooks-offline.html', response('offline-shell')]]);
  const unavailable = {...response('unavailable'), status: 503};
  const denied = {...response('denied'), status: 403};
  const request = {method: 'GET', mode: 'navigate', url: 'https://david-pi.test/audiobooks'};
  assert.equal((await harness({entries, fetchImpl: async () => unavailable}).dispatch(request).responsePromise).label, 'offline-shell');
  assert.equal((await harness({entries, fetchImpl: async () => denied}).dispatch(request).responsePromise).label, 'denied');
});

test('redirected sign-in content is never stored as the authenticated audiobook shell', async () => {
  const redirected = {...response('sign-in'), redirected: true, url: 'https://david-pi.test/login'};
  const {calls, dispatch} = harness({fetchImpl: async () => redirected});
  const result = await dispatch({method: 'GET', mode: 'navigate', url: 'https://david-pi.test/audiobooks'}).responsePromise;
  assert.equal(result.label, 'sign-in');
  assert.deepEqual(calls.puts, []);
});

test('a first offline normal-page visit falls back to the dedicated saved-book shell', async () => {
  const entries = new Map([['/static/audiobooks-offline.html', response('offline-manager')]]);
  const {calls, dispatch} = harness({entries, fetchImpl: async () => { throw new Error('offline'); }});
  const page = await dispatch({method: 'GET', mode: 'navigate', url: 'https://david-pi.test/audiobooks'}).responsePromise;
  assert.equal(page.label, 'offline-manager');
  assert.deepEqual(calls.matches, ['/static/audiobooks-offline.html']);
});

test('the cached content-neutral player shell includes continuity-independent offline dependencies', () => {
  for (const asset of [
    '/static/theme-bootstrap.js?v=8',
    '/static/audiobook-offline-web.js?v=10',
    '/static/audiobook-ui-safety.js?v=3',
  ]) assert.equal(source.includes(`'${asset}'`), true, asset);
  assert.equal(source.includes("cache.put(AUDIOBOOK_PAGE"), false);
});

test('verified OPFS audiobooks serve HEAD and exact single-byte ranges without network access', async () => {
  const id = 'a'.repeat(32), bytes = new Uint8Array([10, 20, 30, 40, 50]);
  const record = {
    version: 3, state: 'complete', expected_bytes: bytes.length,
    integrity_verified_at: Date.now(), file_name: `${id}.audio.part`,
    content_sha256: 'b'.repeat(64),
    book: {id, visibility: 'shared', content_sha256: 'b'.repeat(64), content_type: 'audio/mp4'},
  };
  const {calls, dispatch} = harness({offlineRecord: record, offlineBytes: bytes, fetchImpl: async () => response('network')});
  const url = `https://david-pi.test/__davidpi_offline/audiobooks/${id}`;
  const request = (method, range) => ({method, mode: 'cors', url, headers: new Headers(range ? {Range: range} : {})});

  const head = await dispatch(request('HEAD')).responsePromise;
  assert.equal(head.status, 200);
  assert.equal(head.headers.get('content-length'), '5');
  assert.equal((await head.arrayBuffer()).byteLength, 0);

  const middle = await dispatch(request('GET', 'bytes=1-3')).responsePromise;
  assert.equal(middle.status, 206);
  assert.equal(middle.headers.get('content-range'), 'bytes 1-3/5');
  assert.deepEqual([...new Uint8Array(await middle.arrayBuffer())], [20, 30, 40]);

  const suffix = await dispatch(request('GET', 'bytes=-2')).responsePromise;
  assert.deepEqual([...new Uint8Array(await suffix.arrayBuffer())], [40, 50]);
  const open = await dispatch(request('GET', 'bytes=3-')).responsePromise;
  assert.deepEqual([...new Uint8Array(await open.arrayBuffer())], [40, 50]);

  const invalid = await dispatch(request('GET', 'bytes=99-')).responsePromise;
  assert.equal(invalid.status, 416);
  assert.equal(invalid.headers.get('content-range'), 'bytes */5');
  assert.deepEqual(calls.fetches, []);
});

test('a cache-first shell refresh is aborted at a hard deadline without delaying the cached player', async () => {
  let aborted = false;
  const entries = new Map([['/static/audiobooks-offline.html', response('cached')]]);
  const {calls, dispatch} = harness({
    entries,
    refreshTimeoutMs: 5,
    fetchImpl: (_request, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => {
        aborted = true;
        reject(new Error('aborted'));
      }, {once: true});
    }),
  });
  const event = dispatch({
    method: 'GET',
    mode: 'navigate',
    url: 'https://david-pi.test/static/audiobooks-offline.html',
  });
  assert.equal((await event.responsePromise).label, 'cached');
  await Promise.all(event.waits);
  assert.equal(aborted, true);
  assert.equal(calls.fetchOptions.length, 1);
  assert.equal(calls.fetchOptions[0].signal.aborted, true);
  assert.deepEqual(calls.puts, []);
});

test('authenticated APIs and audiobook media remain network-only even if a cache entry exists', async () => {
  for (const path of [
    '/api/audiobooks',
    '/api/audiobooks/book/stream',
    '/api/audiobooks/book/hls/index.m4s',
    '/media/private-book.m4b',
  ]) {
    const entries = new Map([[`https://david-pi.test${path}`, response('cached-private')]]);
    const {calls, dispatch} = harness({entries, fetchImpl: async () => response('network')});
    const event = dispatch({method: 'GET', mode: 'cors', url: `https://david-pi.test${path}`});
    assert.equal((await event.responsePromise).label, 'network');
    assert.deepEqual(calls.matches, []);
    assert.deepEqual(calls.puts, []);
  }
});
