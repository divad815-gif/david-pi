import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../installer/web/setup.js', import.meta.url), 'utf8');
const context = {module: {exports: {}}, URL, setTimeout, clearTimeout};
vm.runInNewContext(source, context);
const wizard = context.module.exports;
const normalize = value => JSON.parse(JSON.stringify(value));
const jobId = '0123456789abcdef0123456789abcdef';
const choices = [
  {id: 'system', label: 'Server storage', parent: '/srv', mode: 'folder', device_id: 'os', system_disk: true, free_bytes: 10 * 1024 ** 3, total_bytes: 32 * 1024 ** 3},
  {id: 'photos', label: 'Household drive', parent: '/mnt/home', mode: 'drive', device_id: 'data', free_bytes: 8 * 1024 ** 3, total_bytes: 12 * 1024 ** 3},
  {id: 'same-disk-partition', label: 'Second partition', parent: '/mnt/other', mode: 'drive', device_id: 'data', free_bytes: 1024 ** 3, total_bytes: 2 * 1024 ** 3},
  {id: 'backup', label: 'Backup drive', parent: '/mnt/backup', mode: 'drive', device_id: 'backup', free_bytes: 10 * 1024 ** 3, total_bytes: 12 * 1024 ** 3},
];
const manual = {parent: '/srv/user-chosen/', backup: '/mnt/user-backup/', mode: 'folder'};

test('timezone suggestions use a supported browser timezone, server fallback, then UTC', () => {
  const zones = ['UTC', 'America/Denver', 'Europe/London'];
  assert.equal(wizard.chooseTimezone(zones, 'America/Denver', 'Europe/London'), 'America/Denver');
  assert.equal(wizard.chooseTimezone(zones, 'Unsupported/Zone', 'Europe/London'), 'Europe/London');
  assert.equal(wizard.chooseTimezone(zones, 'Unsupported/Zone', 'Unsupported/Server'), 'UTC');
});

test('timezone search is case-insensitive and never silently changes an existing choice', () => {
  const zones = ['UTC', 'America/Denver', 'America/New_York', 'Europe/London'];
  assert.deepEqual(normalize(wizard.filterTimezones(zones, 'NEW YORK', 'America/Denver')), {
    matches: ['America/New_York'], options: ['America/Denver', 'America/New_York'],
  });
  assert.deepEqual(normalize(wizard.filterTimezones(zones, 'not-a-city', 'America/Denver')), {
    matches: [], options: ['America/Denver'],
  });
});

test('detected storage displays usable capacity and distinguishes the system disk', () => {
  assert.equal(wizard.storageLabel(choices[0]), 'Server storage · system disk · 10 GiB free of 32 GiB');
  assert.equal(wizard.capacity(-1), 'capacity unavailable');
  assert.equal(wizard.capacity(0), '0 B');
});

test('backup list excludes all partitions on the primary physical device', () => {
  assert.deepEqual(normalize(wizard.backupChoices(choices, 'photos').map(choice => choice.id)), ['system', 'backup']);
  assert.deepEqual(normalize(wizard.backupChoices(choices, 'system').map(choice => choice.id)), ['photos', 'same-disk-partition', 'backup']);
});

test('detected selections submit identity handles and dedicated paths, without typed-path leakage', () => {
  assert.deepEqual(normalize(wizard.selectedStorage(choices, 'photos', 'backup', manual)), {
    storage: {mode: 'drive', data_root: '/mnt/home/david-pi-data', backup_root: '/mnt/backup/david-pi-backups'},
    selection: {data: 'photos', backup: 'backup'},
  });
});

test('advanced paths and skipped backups omit only their automatic identity handles', () => {
  assert.deepEqual(normalize(wizard.selectedStorage(choices, 'manual', 'skip', manual)), {
    storage: {mode: 'folder', data_root: '/srv/user-chosen/david-pi-data', backup_root: null}, selection: {},
  });
  assert.deepEqual(normalize(wizard.selectedStorage(choices, 'photos', 'manual', manual)), {
    storage: {mode: 'drive', data_root: '/mnt/home/david-pi-data', backup_root: '/mnt/user-backup/david-pi-backups'}, selection: {data: 'photos'},
  });
});

test('removed drives, empty choices, and same-disk backups cannot become a silent default', () => {
  assert.throws(() => wizard.selectedStorage(choices, '', 'skip', manual), /Choose an available storage/);
  assert.throws(() => wizard.selectedStorage(choices, 'removed-drive', 'skip', manual), /Choose an available storage/);
  assert.throws(() => wizard.selectedStorage(choices, 'photos', 'removed-backup', manual), /Choose an available independent backup/);
  assert.throws(() => wizard.selectedStorage(choices, 'photos', 'same-disk-partition', manual), /different drive/);
  assert.throws(() => wizard.selectedStorage(choices, 'manual', 'manual', {...manual, backup: ''}), /backup parent folder/);
});

const identity = {instance_id: 'f718b396-81b0-443e-9d3c-1935fcbac1c3', origin: 'https://john-pi.example.ts.net'};
const metadata = {instance_id: identity.instance_id, public_url: identity.origin};

test('portal completion requires readiness, installation identity, and exact approved HTTPS origin', () => {
  assert.equal(wizard.readyMatches({ok: true}, metadata, identity, identity.origin), true);
  assert.equal(wizard.readyMatches({ok: false}, metadata, identity, identity.origin), false);
  assert.equal(wizard.readyMatches({}, metadata, identity, identity.origin), false);
  assert.equal(wizard.readyMatches({ok: true}, {...metadata, instance_id: 'other-household'}, identity, identity.origin), false);
  for (const public_url of ['https://evil.example', `${identity.origin}/other`, `${identity.origin}?secret=1`, 'http://john-pi.example.ts.net', 'https://user@john-pi.example.ts.net']) {
    assert.equal(wizard.readyMatches({ok: true}, {...metadata, public_url}, identity, identity.origin), false, public_url);
  }
  assert.equal(wizard.readyMatches({ok: true}, metadata, identity, 'https://other-browser-origin.example'), false);
  assert.equal(wizard.readyMatches({ok: true}, {...metadata, public_url: `${identity.origin}/`}, identity, identity.origin), true);
});

function harness(overrides = {}) {
  const changes = [], remembered = [], pending = [];
  let forgotten = 0;
  const options = {
    fetchJob: async () => ({id: jobId, state: 'running', phase: 'starting selected services', created_at: 100}),
    verifyReady: async () => false,
    remember: value => remembered.push({...value}), forget: () => forgotten++,
    onChange: value => changes.push(value),
    schedule: (callback, delay) => {const timer = {callback, delay}; pending.push(timer); return timer;},
    cancel: timer => {if (timer) timer.cancelled = true;},
    ...overrides,
  };
  const watcher = wizard.watchInstallation(options);
  return {watcher, changes, remembered, pending, forgotten: () => forgotten,
    async next() {let timer; while (pending.length && !timer) {const item = pending.shift(); if (!item.cancelled) timer = item;} assert.ok(timer, 'expected a scheduled progress check'); await timer.callback(); return timer.delay;},
  };
}

test('progress counts finished stages rather than elapsed time or fabricated percentages', async () => {
  let phase = 'preparing selected storage';
  const h = harness({fetchJob: async () => ({state: 'running', phase, created_at: 1})});
  await h.watcher.start({job_id: jobId, created_at: 1, completed: 0});
  assert.equal(h.changes.at(-1).completed, 0);
  await h.next(); assert.equal(h.changes.at(-1).completed, 0);
  phase = 'starting selected services'; await h.next(); assert.equal(h.changes.at(-1).completed, 2);
  phase = 'checking selected services'; await h.next(); assert.equal(h.changes.at(-1).completed, 3);
  phase = 'opening your home server'; await h.next(); assert.equal(h.changes.at(-1).completed, 4);
  assert.equal(h.changes.some(change => change.completed === 5), false);
});

test('job completion keeps verifying instead of declaring a metadata-only success', async () => {
  let ready = false;
  const h = harness({fetchJob: async () => ({state: 'complete', phase: 'complete'}), verifyReady: async () => ready});
  await h.watcher.start({job_id: jobId, completed: 4});
  assert.equal(h.changes.at(-1).kind, 'confirming');
  assert.equal(h.forgotten(), 0);
  ready = true; await h.next();
  assert.equal(h.changes.at(-1).kind, 'complete'); assert.equal(h.changes.at(-1).completed, 5);
  assert.equal(h.forgotten(), 1); assert.equal(h.pending.length, 0);
});

test('a temporary handoff disconnect preserves the saved reference and resumes without re-submission', async () => {
  let polls = 0, ready = false;
  const h = harness({fetchJob: async id => {assert.equal(id, jobId); if (++polls === 1) throw new Error('handoff'); return {state: 'running', phase: 'opening your home server'};}, verifyReady: async () => ready});
  await h.watcher.start({job_id: jobId, created_at: 100, completed: 3});
  assert.equal(h.changes.at(-1).kind, 'reconnecting'); assert.equal(h.changes.at(-1).completed, 3);
  assert.deepEqual(Object.keys(h.remembered.at(-1)).sort(), ['completed', 'created_at', 'job_id']);
  await h.next(); assert.equal(h.changes.at(-1).kind, 'working'); assert.equal(h.changes.at(-1).completed, 4);
  const saved = h.remembered.at(-1); h.watcher.stop();
  ready = true;
  const resumed = harness({fetchJob: async () => {throw new Error('setup endpoint replaced by portal');}, verifyReady: async () => ready});
  await resumed.watcher.start(saved);
  assert.equal(resumed.changes.at(-1).kind, 'complete'); assert.equal(resumed.forgotten(), 1);
});

test('failed or interrupted setup stops polling and keeps its reference for recovery', async () => {
  for (const state of ['failed', 'interrupted']) {
    const h = harness({fetchJob: async () => ({state, phase: 'starting selected services', error: 'Drive is missing'})});
    await h.watcher.start({job_id: jobId});
    assert.equal(h.changes.at(-1).kind, 'failed'); assert.equal(h.changes.at(-1).job.error, 'Drive is missing');
    assert.equal(h.forgotten(), 0); assert.equal(h.pending.length, 0);
  }
});

test('rechecking an old failed job recognizes a successfully repaired same installation', async () => {
  const h = harness({fetchJob: async () => ({state: 'failed', phase: 'starting selected services'}), verifyReady: async () => true});
  await h.watcher.start({job_id: jobId});
  assert.equal(h.changes.at(-1).kind, 'complete'); assert.equal(h.forgotten(), 1);
});

test('repeated network failure is bounded, preserves progress, and supports an explicit check again', async () => {
  let ready = false;
  const h = harness({fetchJob: async () => {throw new Error('offline');}, verifyReady: async () => ready});
  await h.watcher.start({job_id: jobId, completed: 2});
  for (let i = 1; i < 60; i++) await h.next();
  assert.equal(h.changes.at(-1).kind, 'unreachable'); assert.equal(h.changes.at(-1).completed, 2);
  assert.equal(h.forgotten(), 0); assert.equal(h.pending.length, 0);
  ready = true; await h.watcher.start(h.remembered.at(-1));
  assert.equal(h.changes.at(-1).kind, 'complete');
});

test('invalid saved jobs cannot make requests or discard another installation reference', () => {
  let requests = 0;
  const h = harness({fetchJob: async () => {requests++;}});
  assert.throws(() => h.watcher.start({job_id: '../other-job'}), /reference is invalid/);
  assert.equal(requests, 0); assert.equal(h.forgotten(), 0);
});

test('a stopped watcher cannot announce completion after a delayed readiness response', async () => {
  let resolveReady;
  const readiness = new Promise(resolve => {resolveReady = resolve;});
  const h = harness({fetchJob: async () => ({state: 'complete', phase: 'complete'}), verifyReady: () => readiness});
  const active = h.watcher.start({job_id: jobId});
  await new Promise(resolve => setImmediate(resolve));
  h.watcher.stop(); resolveReady(true); await active;
  assert.equal(h.changes.some(change => change.kind === 'complete'), false);
  assert.equal(h.forgotten(), 0); assert.equal(h.pending.length, 0);
});
