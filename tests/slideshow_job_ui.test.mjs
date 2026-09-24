import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const helperSource = readFileSync(new URL('../static/slideshow-job-ui.js', import.meta.url), 'utf8');
const gallerySource = readFileSync(new URL('../static/gallery.js', import.meta.url), 'utf8');
const photoTemplate = readFileSync(new URL('../templates/photos.html', import.meta.url), 'utf8');
const JOB_ID = '0123456789abcdef0123456789abcdef';

function loadApi() {
  const context = {module: {exports: {}}, globalThis: null};
  context.globalThis = context;
  vm.runInNewContext(helperSource, context);
  return context.module.exports;
}

function settle() {
  return new Promise(resolve => setImmediate(resolve));
}

class MemoryStorage {
  constructor(entries = {}) { this.values = new Map(Object.entries(entries)); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

class Scheduler {
  constructor() { this.tasks = []; }
  add(callback, delay) {
    const task = {callback, delay, cancelled: false};
    this.tasks.push(task);
    return task;
  }
  cancel(task) { task.cancelled = true; }
  pending() { return this.tasks.filter(task => !task.cancelled); }
  async runNext() {
    let task;
    while (this.tasks.length && !task) {
      const candidate = this.tasks.shift();
      if (!candidate.cancelled) task = candidate;
    }
    assert.ok(task, 'expected a scheduled poll');
    await task.callback();
    await settle();
    return task.delay;
  }
}

class FakeDialog {
  constructor() {
    this.listeners = new Map();
    this.open = false;
    this.__davidPiMobileHost = null;
  }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }
  removeEventListener(type, listener) { this.listeners.get(type)?.delete(listener); }
  emit(type, event = {}) {
    for (const listener of this.listeners.get(type) || []) listener(event);
  }
  showModal() { this.open = true; }
  close() {
    if (!this.open && !this.__davidPiMobileHost) return;
    this.open = false;
    this.__davidPiMobileHost = null;
    this.emit('close', {type: 'close'});
  }
  cancel() {
    const event = {
      type: 'cancel', defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
    };
    this.emit('cancel', event);
    if (!event.defaultPrevented) this.close();
    return event;
  }
}

function controllerOptions(overrides = {}) {
  const scheduler = overrides.scheduler || new Scheduler();
  return {
    scheduler,
    options: {
      storage: overrides.storage || new MemoryStorage(),
      submit: overrides.submit || (async () => ({id: JOB_ID, status: 'queued'})),
      poll: overrides.poll || (async () => ({id: JOB_ID, status: 'working', progress: 25, message: 'Rendering…'})),
      pollInterval: 250,
      retryDelays: [250, 500, 750],
      schedule: (callback, delay) => scheduler.add(callback, delay),
      cancelSchedule: task => scheduler.cancel(task),
      onState: overrides.onState,
      onComplete: overrides.onComplete,
      onFailure: overrides.onFailure,
    },
  };
}

test('an accepted POST survives polling failures with bounded backoff and no duplicate submission', async () => {
  const api = loadApi();
  const storage = new MemoryStorage();
  const states = [];
  let submissions = 0;
  let polls = 0;
  const {scheduler, options} = controllerOptions({
    storage,
    submit: async () => { submissions += 1; return {id: JOB_ID, status: 'queued'}; },
    poll: async () => { polls += 1; throw new Error('temporary disconnect'); },
    onState: state => states.push(state),
  });
  const controller = api.create(options);

  const accepted = await controller.submit({collection_id: 'collection'});
  await settle();
  assert.equal(accepted.accepted, true);
  assert.equal(submissions, 1);
  assert.equal(polls, 1);
  assert.equal(controller.activeJobId(), JOB_ID);
  assert.equal(storage.getItem('davidPiActiveSlideshowJobV1'), JOB_ID);
  assert.equal(states.at(-1).phase, 'observation-error');
  assert.match(states.at(-1).message, /accepted/i);
  assert.match(states.at(-1).message, /retry automatically/i);
  assert.equal(states.some(state => /Nothing was changed/i.test(state.message)), false);

  const resumed = await controller.submit({collection_id: 'collection'});
  assert.equal(resumed.resumed, true);
  assert.equal(submissions, 1, 'retry UI must observe the accepted job instead of POSTing again');
  assert.deepEqual(scheduler.pending().map(task => task.delay), [250]);
  assert.equal(await scheduler.runNext(), 250);
  assert.equal(await scheduler.runNext(), 500);
  assert.equal(await scheduler.runNext(), 750);
  assert.equal(await scheduler.runNext(), 750, 'backoff must remain capped');
});

test('Escape and native close pause preview while reopen resumes one watcher and completes once', async () => {
  const api = loadApi();
  const storage = new MemoryStorage({davidPiActiveSlideshowJobV1: JOB_ID});
  const scheduler = new Scheduler();
  let polls = 0;
  let completions = 0;
  const {options} = controllerOptions({
    scheduler,
    storage,
    poll: async () => {
      polls += 1;
      return polls === 1
        ? {id: JOB_ID, status: 'working', progress: 40, message: 'Rendering…'}
        : {id: JOB_ID, status: 'completed', progress: 100, result_photo_id: 'result'};
    },
    onComplete: () => { completions += 1; },
  });
  const controller = api.create(options);
  assert.equal(controller.resume(), true);
  assert.equal(controller.resume(), true);
  await settle();
  assert.equal(polls, 1, 'repeat resume while open must not overlap watchers');
  assert.equal(scheduler.pending().length, 1);

  const dialog = new FakeDialog();
  let pauses = 0;
  const lifecycle = api.bindDialogLifecycle({dialog, pause: () => { pauses += 1; }, isSubmitting: () => false});
  dialog.showModal();
  assert.equal(dialog.cancel().defaultPrevented, false, 'Escape should offer background close after acceptance');
  assert.equal(dialog.open, false);
  assert.equal(pauses, 1);
  dialog.showModal();
  dialog.close();
  assert.equal(pauses, 2, 'a native close event must also stop preview audio');

  dialog.showModal();
  assert.equal(controller.resume(), true);
  assert.equal(scheduler.pending().length, 1, 'reopening must reuse the existing watcher');
  await scheduler.runNext();
  assert.equal(completions, 1);
  assert.equal(controller.activeJobId(), '');
  assert.equal(storage.getItem('davidPiActiveSlideshowJobV1'), null);
  assert.equal(controller.resume(), false);
  assert.equal(scheduler.pending().length, 0);
  lifecycle.destroy();
});

test('Escape is blocked only while acceptance is unknown and still pauses preview', () => {
  const api = loadApi();
  const dialog = new FakeDialog();
  let submitting = true;
  let pauses = 0;
  let blocked = 0;
  api.bindDialogLifecycle({
    dialog,
    pause: () => { pauses += 1; },
    isSubmitting: () => submitting,
    onBlockedClose: () => { blocked += 1; },
  });
  dialog.showModal();
  assert.equal(dialog.cancel().defaultPrevented, true);
  assert.equal(dialog.open, true);
  assert.equal(pauses, 1);
  assert.equal(blocked, 1);
  submitting = false;
  assert.equal(dialog.cancel().defaultPrevented, false);
  assert.equal(dialog.open, false);
  assert.equal(pauses, 2);
});

test('destroyed watcher generations ignore late completion and a new page resumes safely', async () => {
  const api = loadApi();
  const storage = new MemoryStorage({davidPiActiveSlideshowJobV1: JOB_ID});
  let resolvePoll;
  let staleCompletions = 0;
  const firstFixture = controllerOptions({
    storage,
    poll: () => new Promise(resolve => { resolvePoll = resolve; }),
    onComplete: () => { staleCompletions += 1; },
  });
  const first = api.create(firstFixture.options);
  first.resume();
  await settle();
  first.destroy();
  resolvePoll({id: JOB_ID, status: 'completed', progress: 100});
  await settle();
  assert.equal(staleCompletions, 0);
  assert.equal(storage.getItem('davidPiActiveSlideshowJobV1'), JOB_ID);

  let resumedCompletions = 0;
  const secondFixture = controllerOptions({
    storage,
    poll: async () => ({id: JOB_ID, status: 'completed', progress: 100}),
    onComplete: () => { resumedCompletions += 1; },
  });
  const second = api.create(secondFixture.options);
  second.resume();
  await settle();
  assert.equal(resumedCompletions, 1);
  assert.equal(storage.getItem('davidPiActiveSlideshowJobV1'), null);
});

test('pre-acceptance failure is truthful and permits a later submission', async () => {
  const api = loadApi();
  const states = [];
  let calls = 0;
  const {options} = controllerOptions({
    submit: async () => { calls += 1; throw new Error('Queue unavailable.'); },
    onState: state => states.push(state),
  });
  const controller = api.create(options);
  const first = await controller.submit({});
  const second = await controller.submit({});
  assert.equal(first.accepted, false);
  assert.equal(second.accepted, false);
  assert.equal(calls, 2);
  assert.equal(controller.activeJobId(), '');
  assert.equal(states.at(-1).phase, 'submission-error');
  assert.match(states.at(-1).message, /No video job was accepted/);
  assert.doesNotMatch(states.at(-1).message, /Nothing was changed/);
});

test('an invalid remembered identifier is discarded instead of adopted', () => {
  const api = loadApi();
  const storage = new MemoryStorage({davidPiActiveSlideshowJobV1: '../another-job'});
  let polls = 0;
  const {options} = controllerOptions({
    storage,
    poll: async () => { polls += 1; return {}; },
  });
  const controller = api.create(options);
  assert.equal(controller.activeJobId(), '');
  assert.equal(controller.resume(), false);
  assert.equal(polls, 0);
  assert.equal(storage.getItem('davidPiActiveSlideshowJobV1'), null);
});

test('malformed or mismatched terminal observations cannot clear or complete the accepted job', async () => {
  const api = loadApi();
  const wrongId = 'fedcba9876543210fedcba9876543210';
  for (const response of [
    {status: 'completed', progress: 100},
    {id: '../invalid', status: 'completed', progress: 100},
    {id: wrongId, status: 'completed', progress: 100},
  ]) {
    const storage = new MemoryStorage({davidPiActiveSlideshowJobV1: JOB_ID});
    const scheduler = new Scheduler();
    const states = [];
    let completions = 0;
    const {options} = controllerOptions({
      scheduler,
      storage,
      poll: async () => response,
      onState: state => states.push(state),
      onComplete: () => { completions += 1; },
    });
    const controller = api.create(options);

    assert.equal(controller.resume(), true);
    await settle();
    assert.equal(controller.activeJobId(), JOB_ID);
    assert.equal(storage.getItem('davidPiActiveSlideshowJobV1'), JOB_ID);
    assert.equal(completions, 0);
    assert.equal(states.at(-1).phase, 'observation-error');
    assert.equal(scheduler.pending().length, 1, 'the accepted job must remain under observation');
    controller.destroy();
  }
});

test('gallery integration loads the controller first and exposes truthful accessible progress', () => {
  const helperIndex = photoTemplate.indexOf('/static/slideshow-job-ui.js?v=1');
  const galleryIndex = photoTemplate.indexOf('/static/gallery.js?v=62');
  assert.ok(helperIndex >= 0 && helperIndex < galleryIndex);
  assert.match(photoTemplate, /id="slideshowProgress" role="progressbar"[^>]+aria-valuenow="0"/);
  const start = gallerySource.indexOf('let slideshowSubmitting = false;');
  const end = gallerySource.indexOf("document.querySelector('#createFromOrganize')", start);
  const integration = gallerySource.slice(start, end);
  assert.match(integration, /DavidPiSlideshowJobs\?\.create/);
  assert.match(integration, /bindDialogLifecycle/);
  assert.match(integration, /activeJobId\(\)/);
  assert.match(integration, /slideshowJobController\.resume\(\)/);
  assert.doesNotMatch(integration, /Nothing was changed/);
  assert.doesNotMatch(integration, /watchSlideshow/);
});
