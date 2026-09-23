import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../static/gallery-density-gesture.js', import.meta.url), 'utf8');

function loadApi() {
  const context = {module: {exports: {}}, globalThis: null};
  context.globalThis = context;
  vm.runInNewContext(source, context);
  return context.module.exports;
}

function fixture(initialLevel = 2) {
  let level = initialLevel;
  let clock = 1000;
  const updates = [];
  const gesture = loadApi().create({
    getLevel: () => level,
    setLevel: (next) => { level = next; updates.push(next); },
    now: () => clock,
  });
  return {
    gesture,
    updates,
    level: () => level,
    tick: (milliseconds) => { clock += milliseconds; },
  };
}

function fakeCard() {
  const attributes = new Map();
  return {
    attributes,
    dataset: {},
    disabled: false,
    tabIndex: 0,
    setAttribute: (name, value) => attributes.set(name, String(value)),
    removeAttribute: (name) => attributes.delete(name),
    getAttribute: (name) => attributes.get(name) ?? null,
  };
}

test('only 3-across is interactive and dense modes are explicitly browse-only', () => {
  const {modeForLevel} = loadApi();
  assert.deepEqual(
    [0, 1, 2, 3].map((level) => {
      const mode = modeForLevel(level);
      return [mode.columns, mode.interactive, mode.mode, mode.instruction];
    }),
    [
      [3, true, 'interactive', 'Tap to open'],
      [6, false, 'browse', 'Browse only'],
      [9, false, 'browse', 'Browse only'],
      [13, false, 'browse', 'Browse only'],
    ],
  );
});

test('card semantics become unfocusable and disabled in browse mode and recover at 3-across', () => {
  const {applyCardMode} = loadApi();
  const card = fakeCard();
  applyCardMode(card, 'Lake day', 3);
  assert.equal(card.disabled, true);
  assert.equal(card.tabIndex, -1);
  assert.equal(card.dataset.browseOnly, 'true');
  assert.equal(card.getAttribute('aria-disabled'), 'true');
  assert.match(card.getAttribute('aria-label'), /Browse-only thumbnail; switch to 3 across to open or select/);

  applyCardMode(card, 'Lake day', 0);
  assert.equal(card.disabled, false);
  assert.equal(card.tabIndex, 0);
  assert.equal(card.dataset.browseOnly, 'false');
  assert.equal(card.getAttribute('aria-disabled'), null);
  assert.equal(card.getAttribute('aria-label'), 'Open Lake day');
});

test('density anchor is captured once across a change burst and restored after layout', () => {
  const {createAnchorPreserver} = loadApi();
  const frames = [];
  const changes = [];
  const restores = [];
  let captures = 0;
  const anchor = {id: 'photo-42', top: 18, scrollY: 900};
  const preserver = createAnchorPreserver({
    capture: () => { captures += 1; return anchor; },
    restore: (value) => restores.push(value),
    requestFrame: (callback) => { frames.push(callback); return frames.length; },
    cancelFrame: () => {},
  });

  preserver.mutate(() => changes.push(2));
  preserver.mutate(() => changes.push(1));
  preserver.mutate(() => changes.push(0));
  assert.equal(captures, 1);
  assert.deepEqual(changes, [2, 1, 0]);
  assert.equal(frames.length, 1);
  assert.equal(preserver.pending(), true);
  frames.shift()();
  assert.deepEqual(restores, [anchor]);
  assert.equal(preserver.pending(), false);
});

test('cancelled anchor restoration cannot move a refreshed gallery', () => {
  const {createAnchorPreserver} = loadApi();
  const frames = new Map();
  const cancelled = [];
  const restores = [];
  const preserver = createAnchorPreserver({
    capture: () => ({id: 'stale-photo'}),
    restore: (value) => restores.push(value),
    requestFrame: (callback) => { frames.set(7, callback); return 7; },
    cancelFrame: (handle) => { cancelled.push(handle); frames.delete(handle); },
  });
  preserver.mutate(() => {});
  preserver.cancel();
  assert.deepEqual(cancelled, [7]);
  assert.deepEqual(restores, []);
  assert.equal(preserver.pending(), false);
});

test('automatic paging requires and consumes one deliberate scroll intent', () => {
  const {createIntersectionPageGate} = loadApi();
  const gate = createIntersectionPageGate();
  let loads = 0;
  const load = () => { loads += 1; };
  assert.equal(gate.update(true, true, load), false, 'layout alone must not load');
  gate.grantIntent();
  assert.equal(gate.hasIntent(), true);
  assert.equal(gate.update(true, true, load), true);
  assert.equal(gate.hasIntent(), false);
  assert.equal(gate.update(true, true, load), false);
  assert.equal(loads, 1);
});

test('sentinel reflow cannot cascade but a later scroll gesture can load one more page', () => {
  const {createIntersectionPageGate} = loadApi();
  const gate = createIntersectionPageGate();
  let loads = 0;
  const load = () => { loads += 1; };
  gate.grantIntent();
  assert.equal(gate.update(true, true, load), true);
  gate.update(false, true, load);
  assert.equal(gate.update(true, true, load), false, 'exit/re-entry from append has no intent');
  assert.equal(loads, 1);
  gate.grantIntent();
  assert.equal(gate.update(true, true, load), true, 'fresh gesture earns one page');
  assert.equal(loads, 2);
});

test('density changes revoke an unused scroll intent before their layout reflow', () => {
  const {createIntersectionPageGate} = loadApi();
  const gate = createIntersectionPageGate();
  let loads = 0;
  gate.grantIntent();
  gate.revokeIntent();
  assert.equal(gate.update(true, true, () => { loads += 1; }), false);
  assert.equal(loads, 0);
  assert.equal(gate.hasIntent(), false);
  gate.reset();
  assert.equal(gate.isIntersecting(), false);
  assert.equal(gate.hasLoaded(), false);
});

test('one-finger movement remains native scrolling and does not alter density', () => {
  const {gesture, updates} = fixture();
  const down = gesture.pointerDown({id: 1, x: 20, y: 20});
  assert.equal(down.accepted, true);
  assert.equal(down.consume, false);
  assert.deepEqual(Array.from(down.captureIds), []);
  const move = gesture.pointerMove({id: 1, x: 20, y: 160});
  assert.equal(move.consume, false);
  assert.equal(move.changed, false);
  assert.deepEqual(updates, []);
  assert.equal(gesture.shouldSuppressClick(), false);
});

test('second finger starts a claimed pinch and spreading makes tiles larger', () => {
  const {gesture, updates, level} = fixture(2);
  gesture.pointerDown({id: 10, x: 0, y: 0});
  const start = gesture.pointerDown({id: 11, x: 100, y: 0});
  assert.equal(start.consume, true);
  assert.deepEqual(Array.from(start.captureIds), [10, 11]);
  assert.equal(gesture.pointerMove({id: 11, x: 117, y: 0}).changed, true);
  assert.equal(level(), 1);
  assert.deepEqual(updates, [1]);
});

test('contracting makes tiles denser and rebases for multiple intentional steps', () => {
  const {gesture, updates, level} = fixture(0);
  gesture.pointerDown({id: 1, x: 0, y: 0});
  gesture.pointerDown({id: 2, x: 100, y: 0});
  assert.equal(gesture.pointerMove({id: 2, x: 85, y: 0}).changed, true);
  assert.equal(gesture.pointerMove({id: 2, x: 70, y: 0}).changed, true);
  assert.equal(level(), 2);
  assert.deepEqual(updates, [1, 2]);
});

test('reversing after spreading at the largest-tile boundary contracts from the latest extreme', () => {
  const {gesture, updates, level} = fixture(0);
  gesture.pointerDown({id: 1, x: 0, y: 0});
  gesture.pointerDown({id: 2, x: 100, y: 0});
  for (const x of [110, 117, 128, 138]) {
    assert.equal(gesture.pointerMove({id: 2, x, y: 0}).changed, false);
  }
  assert.equal(gesture.pointerMove({id: 2, x: 134, y: 0}).changed, false);
  assert.equal(gesture.pointerMove({id: 2, x: 117, y: 0}).changed, true);
  assert.equal(level(), 1);
  assert.deepEqual(updates, [1]);
});

test('reversing after contracting at the densest boundary expands without lifting', () => {
  const {gesture, updates, level} = fixture(3);
  gesture.pointerDown({id: 1, x: 0, y: 0});
  gesture.pointerDown({id: 2, x: 100, y: 0});
  for (const x of [90, 84, 75, 64]) {
    assert.equal(gesture.pointerMove({id: 2, x, y: 0}).changed, false);
  }
  assert.equal(gesture.pointerMove({id: 2, x: 68, y: 0}).changed, false);
  assert.equal(gesture.pointerMove({id: 2, x: 76, y: 0}).changed, true);
  assert.equal(level(), 2);
  assert.deepEqual(updates, [2]);
});

test('an overshot outward step can reverse into a denser step without crossing its old origin', () => {
  const {gesture, updates, level} = fixture(2);
  gesture.pointerDown({id: 1, x: 0, y: 0});
  gesture.pointerDown({id: 2, x: 100, y: 0});
  assert.equal(gesture.pointerMove({id: 2, x: 118, y: 0}).changed, true);
  assert.equal(level(), 1);
  assert.equal(gesture.pointerMove({id: 2, x: 150, y: 0}).changed, true);
  assert.equal(level(), 0);
  assert.equal(gesture.pointerMove({id: 2, x: 145, y: 0}).changed, false);
  assert.equal(gesture.pointerMove({id: 2, x: 125, y: 0}).changed, true);
  assert.equal(level(), 1);
  assert.deepEqual(updates, [1, 0, 1]);
});

test('jitter and level boundaries consume an active pinch without invalid updates', () => {
  const {gesture, updates, level} = fixture(0);
  gesture.pointerDown({id: 1, x: 0, y: 0});
  gesture.pointerDown({id: 2, x: 100, y: 0});
  const jitter = gesture.pointerMove({id: 2, x: 108, y: 0});
  assert.equal(jitter.consume, true);
  assert.equal(jitter.changed, false);
  assert.equal(jitter.level, 0);
  assert.equal(gesture.pointerMove({id: 2, x: 130, y: 0}).changed, false);
  assert.equal(level(), 0);
  assert.deepEqual(updates, []);
});

test('cancel and pointer end clear state while suppressing the synthetic click', () => {
  const state = fixture();
  state.gesture.pointerDown({id: 1, x: 0, y: 0});
  state.gesture.pointerDown({id: 2, x: 100, y: 0});
  assert.equal(state.gesture.shouldSuppressClick(), true);
  assert.equal(state.gesture.cancelAll().suppressClick, true);
  assert.equal(state.gesture.pointerCount(), 0);
  assert.equal(state.gesture.isPinching(), false);
  state.tick(649);
  assert.equal(state.gesture.shouldSuppressClick(), true);
  state.tick(2);
  assert.equal(state.gesture.shouldSuppressClick(), false);
});

test('a third pointer is ignored and cannot corrupt the active two-pointer gesture', () => {
  const {gesture, updates} = fixture();
  gesture.pointerDown({id: 1, x: 0, y: 0});
  gesture.pointerDown({id: 2, x: 100, y: 0});
  const third = gesture.pointerDown({id: 3, x: 50, y: 50});
  assert.equal(third.accepted, false);
  assert.equal(third.consume, true);
  assert.deepEqual(Array.from(third.captureIds), []);
  assert.equal(gesture.pointerCount(), 2);
  assert.equal(gesture.pointerMove({id: 2, x: 117, y: 0}).changed, true);
  assert.deepEqual(updates, [1]);
});

test('too-close touches do not claim scrolling or suppress a click', () => {
  const {gesture} = fixture();
  gesture.pointerDown({id: 1, x: 5, y: 5});
  const second = gesture.pointerDown({id: 2, x: 8, y: 8});
  assert.equal(second.consume, false);
  assert.equal(gesture.pointerMove({id: 2, x: 9, y: 9}).consume, false);
  assert.equal(gesture.pointerEnd(2).suppressClick, false);
  assert.equal(gesture.shouldSuppressClick(), false);
});

test('a realistic six-pixel movement per finger changes one density step', () => {
  const {gesture, updates, level} = fixture(2);
  gesture.pointerDown({id: 1, x: 20, y: 20});
  gesture.pointerDown({id: 2, x: 80, y: 20});
  assert.equal(gesture.pointerMove({id: 1, x: 17, y: 20}).changed, false);
  assert.equal(gesture.pointerMove({id: 2, x: 83, y: 20}).changed, true);
  assert.equal(level(), 1);
  assert.deepEqual(updates, [1]);
});

test('first pointer movement before the second finger lands is included in the baseline', () => {
  const {gesture, updates, level} = fixture(2);
  gesture.pointerDown({id: 1, x: 0, y: 0});
  gesture.pointerMove({id: 1, x: 20, y: 0});
  gesture.pointerDown({id: 2, x: 100, y: 0});
  assert.equal(gesture.pointerMove({id: 1, x: 13, y: 0}).changed, true);
  assert.equal(level(), 1);
  assert.deepEqual(updates, [1]);
});

test('small pinch reversal returns to the prior density without lifting', () => {
  const {gesture, updates, level} = fixture(2);
  gesture.pointerDown({id: 1, x: 0, y: 0});
  gesture.pointerDown({id: 2, x: 80, y: 0});
  assert.equal(gesture.pointerMove({id: 2, x: 87, y: 0}).changed, true);
  assert.equal(gesture.pointerMove({id: 2, x: 79, y: 0}).changed, true);
  assert.equal(level(), 2);
  assert.deepEqual(updates, [1, 2]);
});

test('cancelled pointer can be replaced and starts from a fresh span', () => {
  const {gesture, updates, level} = fixture(2);
  gesture.pointerDown({id: 1, x: 0, y: 0});
  gesture.pointerDown({id: 2, x: 80, y: 0});
  assert.equal(gesture.pointerMove({id: 2, x: 87, y: 0}).changed, true);
  assert.equal(gesture.pointerEnd(2).suppressClick, true);
  assert.equal(gesture.pointerMove({id: 1, x: 4, y: 0}).changed, false);
  assert.equal(gesture.pointerDown({id: 3, x: 84, y: 0}).consume, true);
  assert.equal(gesture.pointerMove({id: 3, x: 77, y: 0}).changed, true);
  assert.equal(level(), 2);
  assert.deepEqual(updates, [1, 2]);
  assert.equal(gesture.shouldSuppressClick(), true);
});

test('touches that begin too close become an exclusive pinch after separating', () => {
  const {gesture, updates, level} = fixture(2);
  gesture.pointerDown({id: 1, x: 0, y: 0});
  assert.equal(gesture.pointerDown({id: 2, x: 5, y: 0}).consume, false);
  const activated = gesture.pointerMove({id: 2, x: 20, y: 0});
  assert.equal(activated.consume, true);
  assert.deepEqual(Array.from(activated.captureIds), [1, 2]);
  assert.equal(gesture.pointerMove({id: 2, x: 22, y: 0}).changed, true);
  assert.equal(level(), 1);
  assert.deepEqual(updates, [1]);
});

test('one continuous pinch traverses 13 to 3 across and back in deliberate steps', () => {
  const {gesture, updates, level} = fixture(3);
  gesture.pointerDown({id: 1, x: 0, y: 0});
  gesture.pointerDown({id: 2, x: 100, y: 0});
  for (const distance of [110, 122, 135]) {
    assert.equal(gesture.pointerMove({id: 2, x: distance, y: 0}).changed, true);
  }
  assert.equal(level(), 0);
  for (const distance of [120, 105, 90]) {
    assert.equal(gesture.pointerMove({id: 2, x: distance, y: 0}).changed, true);
  }
  assert.equal(level(), 3);
  assert.deepEqual(updates, [2, 1, 0, 1, 2, 3]);
});
