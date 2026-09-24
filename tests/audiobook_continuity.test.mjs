import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../static/audiobook-continuity.js', import.meta.url), 'utf8');
const scopeA = 'a'.repeat(32), scopeB = 'b'.repeat(32), bookA = '1'.repeat(32), bookB = '2'.repeat(32);

function fixture() {
  const values = new Map();
  const storage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  };
  const context = {module: {exports: {}}, globalThis: {}, Date, AbortController, setTimeout, clearTimeout};
  context.globalThis.localStorage = storage;
  context.globalThis.AbortController = AbortController;
  context.globalThis.setTimeout = setTimeout;
  context.globalThis.clearTimeout = clearTimeout;
  vm.runInNewContext(source, context);
  return {api: context.module.exports, create: scope => context.module.exports.create(scope, storage), values};
}

test('continuity remembers only content-neutral playback coordinates per verified scope', () => {
  const {create, values} = fixture(), now = 1_000_000;
  create(scopeA).remember(bookA, 42.5, now);
  create(scopeB).remember(bookB, 7, now + 1);
  assert.deepEqual({...create(scopeA).read(now + 2)}, {scope: scopeA, book_id: bookA, position: 42.5, updated_at: now});
  assert.deepEqual({...create(scopeB).read(now + 2)}, {scope: scopeB, book_id: bookB, position: 7, updated_at: now + 1});
  const raw = values.get('david-pi-audiobook-continuity-v1');
  assert.equal(raw.includes('title'), false);
  assert.equal(raw.includes('author'), false);
});

test('closing one scope does not erase another identity continuation', () => {
  const {create} = fixture(), now = 2_000_000;
  create(scopeA).remember(bookA, 12, now);
  create(scopeB).remember(bookB, 18, now);
  create(scopeA).clear(bookA);
  assert.equal(create(scopeA).read(now + 1), null);
  assert.equal(create(scopeB).read(now + 1).book_id, bookB);
});

test('invalid, future, and expired continuity records never restore', () => {
  const {create} = fixture(), now = 800_000_000;
  assert.equal(create('not-a-scope').remember(bookA, 1, now), null);
  const queue = create(scopeA);
  queue.remember(bookA, 4, now);
  assert.equal(queue.read(now - 1), null);
  queue.remember(bookA, 4, now);
  assert.equal(queue.read(now + 8 * 24 * 60 * 60 * 1000), null);
  assert.equal(queue.remember('bad-book', 4, now), null);
  assert.equal(queue.remember(bookA, -1, now), null);
});

test('a never-resolving shelf request is aborted at its deadline', async () => {
  const {api} = fixture();
  let aborted = false;
  const fetcher = (_input, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener('abort', () => { aborted = true; reject(new Error('aborted')); }, {once: true});
  });
  await assert.rejects(api.fetchWithDeadline(fetcher, '/api/audiobooks', {}, 5), /aborted/);
  assert.equal(aborted, true);
});

test('stall watchdog captures the exact current position only when its deadline fires', async () => {
  const {api} = fixture();
  const scheduled = new Map();
  let next = 1, position = 91.25, handoff = null;
  const timers = {
    setTimeout: callback => {
      const id = next++;
      scheduled.set(id, () => { scheduled.delete(id); callback(); });
      return id;
    },
    clearTimeout: id => scheduled.delete(id),
  };
  const watchdog = api.createStallWatchdog(8000, snapshot => { handoff = snapshot; }, timers);
  watchdog.arm(() => ({position, generation: 7}));
  position = 94.75;
  [...scheduled.values()][0]();
  await Promise.resolve();
  assert.deepEqual({...handoff}, {position: 94.75, generation: 7});

  handoff = null;
  watchdog.arm(() => ({position: 120, generation: 8}));
  watchdog.cancel();
  assert.equal(scheduled.size, 0);
  assert.equal(handoff, null);
});
