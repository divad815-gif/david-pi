import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../static/gallery-window.js', import.meta.url), 'utf8');

function loadApi() {
  const context = {
    module: {exports: {}},
    globalThis: null,
    setTimeout,
    clearTimeout,
  };
  context.globalThis = context;
  vm.runInNewContext(source, context);
  return context.module.exports;
}

function photos(count, options = {}) {
  const months = options.months || 24;
  return Array.from({length: count}, (_, index) => {
    const monthIndex = Math.floor(index / Math.max(1, Math.ceil(count / months)));
    const year = 2026 - Math.floor(monthIndex / 12);
    const month = 12 - (monthIndex % 12);
    return {
      id: `photo-${String(index).padStart(5, '0')}`,
      captured_at: `${year}-${String(month).padStart(2, '0')}-15T12:00:00Z`,
      thumb: `/media/${index}/thumb`,
    };
  });
}

test('month headings are fixed standalone rows and photo rows never cross months', () => {
  const {buildRowModel} = loadApi();
  const items = [
    {id: 'a', captured_at: '2026-09-03T00:00:00Z'},
    {id: 'b', captured_at: '2026-09-02T00:00:00Z'},
    {id: 'c', captured_at: '2026-08-31T00:00:00Z'},
    {id: 'd', captured_at: '2026-08-30T00:00:00Z'},
  ];
  const model = buildRowModel(items, {columns: 3, width: 412, monthHeight: 62});
  assert.deepEqual(Array.from(model.rows, (row) => row.type), ['month', 'photos', 'month', 'photos']);
  assert.deepEqual(Array.from(model.rows[1].itemIndexes), [0, 1]);
  assert.deepEqual(Array.from(model.rows[3].itemIndexes), [2, 3]);
  assert.equal(model.rows[0].height, 62);
  assert.equal(model.rows[2].height, 62);
});

test('thirty-thousand-item windows stay under density caps across long forward and reverse scrolls', () => {
  const {buildRowModel, rangeForViewport, DEFAULT_CARD_CAPS, gapForColumns} = loadApi();
  const items = photos(30000, {months: 120});
  for (const columns of [3, 6, 9, 13]) {
    const model = buildRowModel(items, {
      columns,
      width: 412,
      viewportHeight: 915,
      gap: gapForColumns(columns),
    });
    const positions = [];
    const step = Math.max(1, model.totalHeight / 127);
    for (let offset = 0; offset < model.totalHeight; offset += step) positions.push(offset);
    positions.push(Math.max(0, model.totalHeight - 915));
    positions.push(...positions.slice().reverse());
    for (const offset of positions) {
      const range = rangeForViewport(model, offset, 915);
      assert.ok(range.start <= range.visibleStart, `${columns}: visible start retained`);
      assert.ok(range.end >= range.visibleEnd, `${columns}: visible end retained`);
      assert.ok(range.mountedPhotos <= DEFAULT_CARD_CAPS[columns], `${columns}: ${range.mountedPhotos} mounted`);
      assert.equal(range.top, model.offsets[range.start]);
      assert.equal(range.bottom, model.totalHeight - model.offsets[range.end]);
    }
  }
});

test('narrow tall viewports keep exact caps with contiguous coverage in both directions', () => {
  const {
    buildRowModel,
    rangeForViewport,
    createWindowManager,
    DEFAULT_CARD_CAPS,
    gapForColumns,
    minimumPhotoRowHeight,
  } = loadApi();
  assert.deepEqual({...DEFAULT_CARD_CAPS}, {3: 72, 6: 180, 9: 324, 13: 598});
  const items = photos(30000, {months: 120});
  for (const {width, height} of [{width: 384, height: 1366}, {width: 252, height: 915}]) {
    for (const columns of [3, 6, 9, 13]) {
      const cap = DEFAULT_CARD_CAPS[columns];
      const model = buildRowModel(items, {
        columns,
        width,
        viewportHeight: height,
        cardCap: cap,
        gap: gapForColumns(columns),
      });
      assert.ok(model.photoRowHeight >= minimumPhotoRowHeight(height, columns, cap));
      const maxScroll = Math.max(0, model.totalHeight - height);
      const forward = [0, maxScroll / 2, maxScroll];
      const positions = [...forward, ...forward.slice().reverse()];
      const manager = createWindowManager({
        cardCap: cap,
        mount: (_row, index) => ({index}),
        unmount: () => {},
        setSpacers: () => {},
      });
      manager.setModel(model);
      for (const offset of positions) {
        const range = rangeForViewport(model, offset, height, {cardCap: cap});
        const mountedHeight = model.offsets[range.end] - model.offsets[range.start];
        const viewportEnd = Math.min(model.totalHeight, offset + height);
        assert.ok(range.mountedPhotos <= cap, `${width}x${height}/${columns}: ${range.mountedPhotos}`);
        assert.ok(range.top <= offset + 1e-7, 'mounted interval starts before the viewport');
        assert.ok(model.offsets[range.end] + 1e-7 >= viewportEnd, 'mounted interval covers the viewport end');
        assert.ok(Math.abs(range.top + mountedHeight + range.bottom - model.totalHeight) < 1e-6);
        manager.update(offset, height);
        assert.deepEqual(
          Array.from(manager.mountedIndexes()),
          Array.from({length: range.end - range.start}, (_, index) => range.start + index),
        );
      }
    }
  }
});

test('the manager keeps shared boundary rows mounted while scrolling both directions', () => {
  const {buildRowModel, createWindowManager} = loadApi();
  const model = buildRowModel(photos(30000, {months: 1}), {columns: 3, width: 412});
  let mountCount = 0;
  let unmountCount = 0;
  const manager = createWindowManager({
    cardCap: 72,
    mount: (_row, index) => ({index}),
    unmount: () => { unmountCount += 1; },
    setSpacers: () => {},
  });
  const originalMount = manager.mountedIndexes;
  manager.setModel(model);
  const first = manager.update(3000, 915);
  mountCount += manager.mountedIndexes().length;
  const firstIndexes = new Set(manager.mountedIndexes());
  const second = manager.update(model.offsets[first.visibleStart + 1], 915);
  const retainedForward = manager.mountedIndexes().filter((index) => firstIndexes.has(index));
  assert.ok(retainedForward.length > 0);
  assert.ok(unmountCount <= 2, `small forward move evicted ${unmountCount} rows`);
  const beforeReverse = new Set(manager.mountedIndexes());
  manager.update(model.offsets[Math.max(0, second.visibleStart - 1)], 915);
  assert.ok(manager.mountedIndexes().some((index) => beforeReverse.has(index)));
  assert.ok(mountCount > 0);
  assert.equal(originalMount, manager.mountedIndexes, 'manager API remains stable');
});

test('metadata append preserves the mounted top spacer and visible photo anchor', () => {
  const {buildRowModel, createWindowManager} = loadApi();
  const viewportHeight = 844;
  const firstModel = buildRowModel(photos(90, {months: 1}), {
    columns: 3,
    width: 375,
    viewportHeight,
  });
  const writes = [];
  const manager = createWindowManager({
    cardCap: 72,
    mount: (row, index) => ({key: row.key, index}),
    unmount: () => {},
    setSpacers: (top, bottom, range) => writes.push({top, bottom, range}),
  });
  manager.setModel(firstModel);
  const anchorId = 'photo-00072';
  const anchorOffset = firstModel.photoLocations.get(anchorId).offset;
  const scrollOffset = anchorOffset + 3;
  const before = manager.update(scrollOffset, viewportHeight);
  assert.ok(before.top > 0, 'fixture must exercise a non-zero virtual top spacer');

  const appendedModel = buildRowModel(photos(180, {months: 1}), {
    columns: 3,
    width: 375,
    viewportHeight,
  });
  manager.setModel(appendedModel, {preserveRows: true});
  const provisional = writes.at(-1);
  assert.equal(provisional.top, before.top, 'append must not transiently collapse the top spacer');
  assert.equal(
    provisional.top
      + (appendedModel.offsets[provisional.range.end] - appendedModel.offsets[provisional.range.start])
      + provisional.bottom,
    appendedModel.totalHeight,
  );
  assert.equal(appendedModel.photoLocations.get(anchorId).offset, anchorOffset);

  const after = manager.update(scrollOffset, viewportHeight);
  const anchorRow = appendedModel.photoLocations.get(anchorId).rowIndex;
  assert.ok(anchorRow >= after.visibleStart && anchorRow < after.visibleEnd);
  assert.ok(manager.mountedIndexes().includes(anchorRow));
});

test('density changes preserve a photo-id anchor and its viewport offset', () => {
  const {buildRowModel, gapForColumns, rowAtOffset} = loadApi();
  const items = photos(30000);
  const anchorId = 'photo-18342';
  const viewportTop = 37;
  for (const {width, height} of [{width: 384, height: 1366}, {width: 252, height: 915}]) {
    for (const fromColumns of [3, 6, 9, 13]) {
      const from = buildRowModel(items, {
        columns: fromColumns,
        width,
        viewportHeight: height,
        gap: gapForColumns(fromColumns),
      });
      const fromLocation = from.photoLocations.get(anchorId);
      const capturedTop = fromLocation.offset - (fromLocation.offset - viewportTop);
      for (const toColumns of [3, 6, 9, 13]) {
        const to = buildRowModel(items, {
          columns: toColumns,
          width,
          viewportHeight: height,
          gap: gapForColumns(toColumns),
        });
        const toLocation = to.photoLocations.get(anchorId);
        const restoredScroll = toLocation.offset - capturedTop;
        assert.equal(to.rows[rowAtOffset(to, restoredScroll + viewportTop)].index, toLocation.rowIndex);
        assert.equal(toLocation.offset - restoredScroll, viewportTop);
      }
    }
  }
});

test('visible rows are eager, overscan is lazy, and only three visible thumbnails are high priority', () => {
  const {buildRowModel, rangeForViewport, withVisiblePhotoRow, thumbnailHintsForRow} = loadApi();
  const model = buildRowModel(photos(300), {columns: 13, width: 412});
  const range = withVisiblePhotoRow(model, rangeForViewport(model, 500, 915));
  const before = range.start;
  assert.ok(before < range.visibleStart);
  assert.deepEqual(
    {...thumbnailHintsForRow(before, range, 0)},
    {loading: 'lazy', fetchPriority: 'low', visible: false},
  );
  const firstRow = range.firstVisiblePhotoRow;
  assert.equal(thumbnailHintsForRow(firstRow, range, 0).fetchPriority, 'high');
  assert.equal(thumbnailHintsForRow(firstRow, range, 2).fetchPriority, 'high');
  assert.equal(thumbnailHintsForRow(firstRow, range, 3).fetchPriority, 'auto');
  assert.equal(thumbnailHintsForRow(firstRow + 1, range, 0).loading, 'eager');
  assert.equal(thumbnailHintsForRow(firstRow + 1, range, 0).fetchPriority, 'auto');
});

test('remounted selection state restores both chosen styling and aria-pressed', () => {
  const {applySelectionState} = loadApi();
  const classes = new Set();
  const attributes = new Map();
  const card = {
    classList: {
      toggle(name, force) {
        if (force) classes.add(name); else classes.delete(name);
      },
    },
    setAttribute(name, value) { attributes.set(name, value); },
    removeAttribute(name) { attributes.delete(name); },
  };
  assert.equal(applySelectionState(card, true, true), true);
  assert.equal(classes.has('chosen'), true);
  assert.equal(attributes.get('aria-pressed'), 'true');
  assert.equal(applySelectionState(card, false, true), false);
  assert.equal(classes.has('chosen'), false);
  assert.equal(attributes.get('aria-pressed'), 'false');
  applySelectionState(card, true, false);
  assert.equal(classes.has('chosen'), false);
  assert.equal(attributes.has('aria-pressed'), false);
});

test('intra-row frames do no presentation work and a row boundary updates only changed hints', () => {
  const {buildRowModel, createWindowManager} = loadApi();
  const model = buildRowModel(photos(300, {months: 1}), {columns: 3, width: 312});
  const mounted = [];
  const unmounted = [];
  const updated = [];
  let spacerWrites = 0;
  const manager = createWindowManager({
    cardCap: 72,
    mount: (_row, index) => { mounted.push(index); return {index}; },
    unmount: (_node, _row, index) => unmounted.push(index),
    update: (_node, _row, index) => updated.push(index),
    setSpacers: () => { spacerWrites += 1; },
  });
  manager.setModel(model);
  const firstRow = 20;
  const firstOffset = model.offsets[firstRow] + 2;
  manager.update(firstOffset, 20);
  const baseline = {
    mounts: mounted.length,
    unmounts: unmounted.length,
    updates: updated.length,
    spacers: spacerWrites,
  };
  manager.update(firstOffset + 3, 20);
  assert.deepEqual(
    {mounts: mounted.length, unmounts: unmounted.length, updates: updated.length, spacers: spacerWrites},
    baseline,
  );
  manager.update(model.offsets[firstRow + 1] + 2, 20);
  assert.equal(mounted.length - baseline.mounts, 1);
  assert.equal(unmounted.length - baseline.unmounts, 1);
  assert.deepEqual(updated.slice(baseline.updates).sort((left, right) => left - right), [firstRow, firstRow + 1]);
});

test('metadata batches are density-aware, bounded, and never exceed the API limit', () => {
  const {metadataBatchSize, MAX_METADATA_BATCH} = loadApi();
  const sizes = [3, 6, 9, 13].map((columns) => metadataBatchSize({
    columns,
    width: 412,
    viewportHeight: 915,
  }));
  assert.ok(sizes[0] >= 30);
  assert.ok(sizes.every((size) => size <= MAX_METADATA_BATCH));
  assert.deepEqual(sizes.slice().sort((left, right) => left - right), sizes);
  assert.equal(sizes.at(-1), 200);
});

test('metadata ids are deduplicated and missing or repeated cursors are rejected', () => {
  const {uniqueMetadataItems, isSafeNextCursor} = loadApi();
  const ids = new Set(['existing']);
  const unique = uniqueMetadataItems([
    {id: 'existing'}, {id: 'new-a'}, {id: 'new-a'}, {id: ''}, {id: 'new-b'},
  ], ids);
  assert.deepEqual(Array.from(unique, (item) => item.id), ['new-a', 'new-b']);
  assert.deepEqual([...ids].sort(), ['existing', 'new-a', 'new-b']);
  const cursors = new Set(['seen']);
  assert.equal(isSafeNextCursor('request', 'next', true, cursors), true);
  assert.equal(isSafeNextCursor('request', 'request', true, cursors), false);
  assert.equal(isSafeNextCursor('request', 'seen', true, cursors), false);
  assert.equal(isSafeNextCursor('request', '', true, cursors), false);
  assert.equal(isSafeNextCursor('request', '', false, cursors), true);
});

test('generation gate deduplicates an in-flight request and rejects stale generations', async () => {
  const {createGenerationRequestGate} = loadApi();
  const gate = createGenerationRequestGate(7);
  let resolve;
  let calls = 0;
  const task = () => {
    calls += 1;
    return new Promise((done) => { resolve = done; });
  };
  const first = gate.run(7, task);
  const duplicate = gate.run(7, task);
  await Promise.resolve();
  assert.equal(first, duplicate);
  assert.equal(calls, 1);
  gate.setGeneration(8);
  assert.equal(await gate.run(7, task), false);
  resolve('old-result');
  assert.equal(await first, 'old-result');
  assert.equal(gate.isCurrent(7), false, 'caller can reject the stale async result');
  assert.equal(await gate.run(8, async () => 'fresh-result'), 'fresh-result');
});

test('rapid density and filter model replacements release stale mounted rows', () => {
  const {buildRowModel, createWindowManager, DEFAULT_CARD_CAPS} = loadApi();
  const released = [];
  const manager = createWindowManager({
    cardCap: (columns) => DEFAULT_CARD_CAPS[columns],
    mount: (row, index) => ({key: row.key, index}),
    unmount: (node) => released.push(node.key),
    setSpacers: () => {},
  });
  for (let cycle = 0; cycle < 24; cycle += 1) {
    const columns = [3, 6, 9, 13][cycle % 4];
    const filtered = photos(30000 - cycle * 211, {months: 36});
    const model = buildRowModel(filtered, {columns, width: 412});
    manager.setModel(model);
    const range = manager.update(Math.min(model.totalHeight - 1, cycle * 997), 915);
    assert.ok(range.mountedPhotos <= DEFAULT_CARD_CAPS[columns]);
    assert.ok(manager.mountedIndexes().every((index) => index >= range.start && index < range.end));
  }
  assert.ok(released.length > 0);
});

test('animation-frame scheduling coalesces rapid scroll values into one arithmetic update', () => {
  const {createFrameScheduler} = loadApi();
  const frames = [];
  const values = [];
  const scheduler = createFrameScheduler((value) => values.push(value), {
    requestFrame: (callback) => { frames.push(callback); return frames.length; },
    cancelFrame: () => {},
  });
  assert.equal(scheduler.request(10), true);
  assert.equal(scheduler.request(20), false);
  assert.equal(scheduler.request(30), false);
  assert.equal(frames.length, 1);
  frames.shift()();
  assert.deepEqual(values, [30]);
  assert.equal(scheduler.pending(), false);
});
