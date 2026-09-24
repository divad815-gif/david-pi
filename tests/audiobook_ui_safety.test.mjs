import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const helperSource = readFileSync(new URL('../static/audiobook-ui-safety.js', import.meta.url), 'utf8');
const playerSource = readFileSync(new URL('../static/audiobooks.js', import.meta.url), 'utf8');
const offlinePlayerSource = readFileSync(new URL('../static/audiobooks-offline-page.js', import.meta.url), 'utf8');

function loadSafety() {
  const context = {module: {exports: {}}, globalThis: {}};
  context.globalThis.globalThis = context.globalThis;
  vm.runInNewContext(helperSource, context);
  return context.module.exports;
}

test('cancel and close racing together execute teardown exactly once', async () => {
  const safety = loadSafety();
  let persisted = 0;
  let cleaned = 0;
  const teardown = safety.createTeardown({
    persist: async () => { persisted += 1; },
    cleanup: () => { cleaned += 1; },
  });
  teardown.reset();
  await Promise.all([teardown.run(), teardown.run(), teardown.run()]);
  assert.equal(persisted, 1);
  assert.equal(cleaned, 1);
  teardown.reset();
  await teardown.run();
  assert.equal(cleaned, 2);
});

test('rejected persistence and rejected actions are handled without rejection escape', async () => {
  const safety = loadSafety();
  const errors = [];
  let cleaned = 0;
  const teardown = safety.createTeardown({
    persist: () => Promise.reject(new Error('offline')),
    cleanup: () => { cleaned += 1; },
    report: error => errors.push(error.message),
  });
  teardown.reset();
  assert.equal(await teardown.run(), null);
  assert.equal(cleaned, 1);
  assert.deepEqual(errors, ['offline']);
  assert.equal(await safety.guard(() => Promise.reject(new Error('delete failed')), error => errors.push(error.message)), null);
  assert.deepEqual(errors, ['offline', 'delete failed']);
});

test('generation changes remove stale media listeners before a later session', () => {
  const safety = loadSafety();
  const guard = safety.createGenerationGuard();
  const listeners = new Map();
  const target = {
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) {
      if (listeners.get(type) === listener) listeners.delete(type);
    },
  };
  const calls = [];
  const first = guard.next();
  guard.listen(first, target, 'loadedmetadata', () => calls.push('first'), {once: true});
  assert.equal(listeners.has('loadedmetadata'), true);
  const staleListener = listeners.get('loadedmetadata');
  const second = guard.next();
  assert.equal(listeners.has('loadedmetadata'), false);
  staleListener();
  assert.deepEqual(calls, []);
  guard.listen(second, target, 'loadedmetadata', () => calls.push('second'), {once: true});
  listeners.get('loadedmetadata')();
  assert.deepEqual(calls, ['second']);
  assert.equal(listeners.has('loadedmetadata'), false);
});

test('a delayed rejection cannot run recovery after its session is cancelled', async () => {
  const safety = loadSafety();
  const guard = safety.createGenerationGuard();
  let rejectLoad;
  let recoveries = 0;
  const token = guard.next();
  const delayed = safety.guard(
    () => new Promise((_resolve, reject) => { rejectLoad = reject; }),
    () => { if (guard.current(token)) recoveries += 1; },
  );
  guard.cancel();
  rejectLoad(new Error('late HLS failure'));
  await delayed;
  assert.equal(recoveries, 0);
});

test('an earlier teardown rejection cannot overwrite a newly opened session', async () => {
  const safety = loadSafety();
  let rejectPersist;
  const errors = [];
  const teardown = safety.createTeardown({
    persist: () => new Promise((_resolve, reject) => { rejectPersist = reject; }),
    cleanup: () => {},
    report: error => errors.push(error.message),
  });
  teardown.reset();
  const closing = teardown.run();
  teardown.reset();
  rejectPersist(new Error('old session write failed'));
  await closing;
  assert.deepEqual(errors, []);
});

test('player minimizes without destroying playback and retains an explicit stop path', () => {
  assert.match(playerSource, /#closeAudiobookPlayer[^\n]+minimizeAudiobookPlayer/);
  assert.match(playerSource, /#stopAudiobookPlayer[^\n]+audiobookPlayerTeardown\.run/);
  assert.match(playerSource, /addEventListener\('cancel',[^\n]+preventDefault\(\)[^\n]+minimizeAudiobookPlayer/);
  assert.match(playerSource, /addEventListener\('close',[^\n]+audiobookPersistentSession/);
  assert.match(playerSource, /DavidPiPersistentAudio\?\.claim/);
  assert.match(playerSource, /onTakeover:\(\)=>\{[\s\S]{0,220}audio\.pause\(\)[\s\S]{0,220}audiobookPlayerTeardown\.run\(\)/);
  assert.match(playerSource, /persist:\(\)=>saveAudiobookProgress\(true\)/);
  assert.match(playerSource, /audiobookPersistentSession\?\.release\(\)/);
  assert.match(playerSource, /command==='expand'\)restoreAudiobookPlayer/);
  assert.match(playerSource, /event\.metaKey\|\|event\.ctrlKey\|\|event\.shiftKey\|\|event\.altKey/);
  assert.match(playerSource, /link\.hasAttribute\('download'\)/);
  assert.match(playerSource, /link\.target&&link\.target!=='_self'/);
  assert.match(playerSource, /url\.pathname==='\/logout'/);
  assert.match(playerSource, /serviceWorker\.register\('\/sw\.js'\)/);
  assert.match(playerSource, /DavidPiAudiobookSafety\.guard/);
  assert.match(playerSource, /audiobookPlayerGuard\.cancel\(\)/);
  assert.match(playerSource, /audiobookStreamGuard\.listen\(streamGeneration,audio,'loadedmetadata'/);
  assert.match(playerSource, /if\(audiobookStreamIsActive\(book,playerGeneration,streamGeneration\)\)useRangeAudiobookStream/);
  assert.match(playerSource, /audiobookHls===instance/);
  assert.match(playerSource, /seekbackward:whenActive/);
  assert.match(playerSource, /seekforward:whenActive/);
  assert.match(playerSource, /seekto:whenActive/);
  assert.match(playerSource, /stop:whenActive/);
  assert.match(offlinePlayerSource, /offlineGeneration\+=1;clearOfflineMetadataListener\(\)/);
  assert.match(offlinePlayerSource, /generation!==offlineGeneration\|\|offlineActive!==book/);
});

test('native offline save waits for durable Android confirmation and sends integrity', () => {
  assert.match(playerSource, /content_sha256:book\.sha256\|\|''/);
  assert.match(playerSource, /await window\.DavidPiOffline\.saveAudiobook/);
  assert.doesNotMatch(playerSource, /const result=window\.DavidPiOffline\.saveAudiobook/);
});

test('normal player is local-first and releases temporary OPFS playback URLs', () => {
  assert.match(playerSource, /openLocal:\(\)=>useOfflineAudiobookStream/);
  assert.doesNotMatch(playerSource, /startPreferredAudiobookStream[\s\S]{0,800}audiobookOfflineCandidate\(book\)/);
  assert.match(playerSource, /\(!savedScope\|\|savedScope===currentScope\)\?saved:null/);
  assert.doesNotMatch(playerSource, /if\(\(navigator\.onLine===false\|\|book\.offline_only\)&&audiobookOfflineCandidate\(book\)\)/);
  assert.match(playerSource, /offlineApi\.playbackSource\(book\.id,audiobookProgressScope\)/);
  assert.match(playerSource, /releasePlaybackSource\(audiobookOfflinePlaybackSource\)/);
  assert.match(offlinePlayerSource, /offlineApi\.managerPlaybackSource\(book\.id\)/);
  assert.doesNotMatch(offlinePlayerSource, /await navigator\.serviceWorker\?\.ready/);
  assert.doesNotMatch(playerSource, /const data=await audiobookApi\([^\n]+;await offlineRefresh/);
  assert.match(playerSource, /save\.dataset\.offlineBookId=book\.id/);
});

test('cold-start source choice completes from OPFS before inventory settles or rejects', async () => {
  const safety = loadSafety();
  let rejectInventory;
  const inventory = new Promise((_resolve, reject) => { rejectInventory = reject; });
  const observedInventory = inventory.catch(error => error.message);
  const order = [];
  const selected = await safety.startLocalFirst({
    openLocal: async () => { order.push('opfs'); return true; },
    openOnline: async () => { order.push('network'); },
    online: false,
  });
  assert.equal(selected, 'offline');
  assert.deepEqual(order, ['opfs']);
  rejectInventory(new Error('inventory unavailable'));
  assert.equal(await observedInventory, 'inventory unavailable');
});

test('offline cold-start fails closed instead of touching the network without a verified copy', async () => {
  const safety = loadSafety();
  const order = [];
  const selected = await safety.startLocalFirst({
    openLocal: async () => { order.push('opfs'); return false; },
    openOnline: async () => { order.push('network'); },
    unavailable: () => order.push('unavailable'),
    online: false,
  });
  assert.equal(selected, 'unavailable');
  assert.deepEqual(order, ['opfs', 'unavailable']);
});
