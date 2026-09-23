(function exposeDavidPiGalleryWindow(global) {
  'use strict';

  const COLUMN_COUNTS = Object.freeze([3, 6, 9, 13]);
  const DEFAULT_CARD_CAPS = Object.freeze({3: 72, 6: 180, 9: 324, 13: 598});
  const DEFAULT_GAPS = Object.freeze({3: 7, 6: 4, 9: 2, 13: 1});
  const DEFAULT_OVERSCAN_BEFORE = 4;
  const DEFAULT_OVERSCAN_AFTER = 8;
  const DEFAULT_MONTH_HEIGHT = 62;
  const MAX_METADATA_BATCH = 200;

  function finiteNumber(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function normalizedColumns(value) {
    const number = Math.trunc(finiteNumber(value, 3));
    return COLUMN_COUNTS.includes(number) ? number : 3;
  }

  function gapForColumns(value) {
    return DEFAULT_GAPS[normalizedColumns(value)];
  }

  function tileSizeForWidth(width, columns, gap = gapForColumns(columns)) {
    const safeColumns = normalizedColumns(columns);
    const safeWidth = Math.max(1, finiteNumber(width, 1));
    const safeGap = Math.max(0, finiteNumber(gap, 0));
    return Math.max(1, (safeWidth - safeGap * (safeColumns - 1)) / safeColumns);
  }

  function minimumPhotoRowHeight(viewportHeight, columns, cardCap = DEFAULT_CARD_CAPS[normalizedColumns(columns)]) {
    const safeColumns = normalizedColumns(columns);
    const safeViewportHeight = Math.max(0, finiteNumber(viewportHeight, 0));
    const safeCap = Math.max(safeColumns, Math.trunc(finiteNumber(
      cardCap,
      DEFAULT_CARD_CAPS[safeColumns],
    )));
    const maximumPhotoRows = Math.max(2, Math.floor(safeCap / safeColumns));
    if (!safeViewportHeight) return 1;
    // A viewport can partially intersect one row at each edge. Reserving its
    // height over (maximumRows - 1), plus one device-independent pixel of
    // aggregate safety, guarantees no interval intersects a row beyond cap.
    return Math.max(1, (safeViewportHeight + 1) / (maximumPhotoRows - 1));
  }

  function photoMonth(photo) {
    const value = String(photo?.captured_at || '');
    return /^\d{4}-\d{2}/.test(value) ? value.slice(0, 7) : '';
  }

  function buildRowModel(items, options = {}) {
    const photos = Array.isArray(items) ? items : [];
    const columns = normalizedColumns(options.columns);
    const gap = Math.max(0, finiteNumber(options.gap, gapForColumns(columns)));
    const tileSize = Math.max(
      1,
      finiteNumber(options.tileSize, tileSizeForWidth(options.width, columns, gap)),
    );
    const cardCap = Math.max(columns, Math.trunc(finiteNumber(
      options.cardCap,
      DEFAULT_CARD_CAPS[columns],
    )));
    const viewportHeight = Math.max(0, finiteNumber(options.viewportHeight, 0));
    const naturalPhotoRowHeight = tileSize + gap;
    const photoRowHeight = Math.max(
      naturalPhotoRowHeight,
      minimumPhotoRowHeight(viewportHeight, columns, cardCap),
    );
    const photoCardHeight = Math.max(1, photoRowHeight - gap);
    const monthHeight = Math.max(1, finiteNumber(options.monthHeight, DEFAULT_MONTH_HEIGHT));
    const rows = [];
    const offsets = [0];
    const photoLocations = new Map();
    let pendingIndexes = [];
    let previousMonth = '';

    function pushRow(row) {
      row.index = rows.length;
      row.offset = offsets[offsets.length - 1];
      rows.push(row);
      offsets.push(row.offset + row.height);
      if (row.type === 'photos') {
        row.itemIndexes.forEach((itemIndex, columnIndex) => {
          const id = String(photos[itemIndex]?.id || '');
          if (id && !photoLocations.has(id)) {
            photoLocations.set(id, {
              itemIndex,
              rowIndex: row.index,
              columnIndex,
              offset: row.offset,
            });
          }
        });
      }
    }

    function flushPhotos() {
      if (!pendingIndexes.length) return;
      const first = pendingIndexes[0];
      const last = pendingIndexes[pendingIndexes.length - 1];
      pushRow({
        type: 'photos',
        key: `photos:${columns}:${String(photos[first]?.id || first)}:${String(photos[last]?.id || last)}`,
        itemIndexes: pendingIndexes,
        photoCount: pendingIndexes.length,
        height: photoRowHeight,
        cardHeight: photoCardHeight,
      });
      pendingIndexes = [];
    }

    photos.forEach((photo, itemIndex) => {
      const month = photoMonth(photo);
      if (month && month !== previousMonth) {
        flushPhotos();
        pushRow({
          type: 'month',
          key: `month:${month}:${itemIndex}`,
          period: month,
          photoCount: 0,
          height: monthHeight,
        });
      }
      pendingIndexes.push(itemIndex);
      if (pendingIndexes.length === columns) flushPhotos();
      previousMonth = month;
    });
    flushPhotos();

    return {
      columns,
      gap,
      tileSize,
      photoRowHeight,
      photoCardHeight,
      naturalPhotoRowHeight,
      monthHeight,
      viewportHeight,
      cardCap,
      items: photos,
      itemCount: photos.length,
      rows,
      offsets,
      photoLocations,
      totalHeight: offsets[offsets.length - 1] || 0,
    };
  }

  // Extend only the unfinished tail. The caller guarantees an unchanged prefix
  // and unchanged geometry; sort/filter/density changes still use buildRowModel.
  function appendRowModel(model, items) {
    const photos = Array.isArray(items) ? items : [];
    if (photos.length < model.itemCount) throw new RangeError('gallery prefix shrank');
    let start = model.itemCount;
    let changedFrom = model.rows.length;
    const tail = model.rows[model.rows.length - 1];
    if (tail?.type === 'photos' && tail.photoCount < model.columns && photos.length > start) {
      changedFrom -= 1;
      start = tail.itemIndexes[0];
      tail.itemIndexes.forEach(index => model.photoLocations.delete(String(model.items[index]?.id || '')));
      model.rows.pop();
      model.offsets.pop();
    }
    const last = model.rows[model.rows.length - 1];
    let previousMonth = last?.type === 'month' ? last.period : photoMonth(photos[start - 1]);
    let pending = [];
    const push = row => {
      row.index = model.rows.length;
      row.offset = model.offsets[model.offsets.length - 1];
      model.rows.push(row);
      model.offsets.push(row.offset + row.height);
      if (row.type === 'photos') row.itemIndexes.forEach((itemIndex, columnIndex) => {
        const id = String(photos[itemIndex]?.id || '');
        if (id && !model.photoLocations.has(id)) model.photoLocations.set(id, {
          itemIndex, rowIndex: row.index, columnIndex, offset: row.offset,
        });
      });
    };
    const flush = () => {
      if (!pending.length) return;
      const first = pending[0], last = pending[pending.length - 1];
      push({type: 'photos', key: `photos:${model.columns}:${String(photos[first]?.id || first)}:${String(photos[last]?.id || last)}`,
        itemIndexes: pending, photoCount: pending.length, height: model.photoRowHeight, cardHeight: model.photoCardHeight});
      pending = [];
    };
    for (let index = start; index < photos.length; index += 1) {
      const month = photoMonth(photos[index]);
      if (month && month !== previousMonth) {
        flush();
        push({type: 'month', key: `month:${month}:${index}`, period: month, photoCount: 0, height: model.monthHeight});
      }
      pending.push(index);
      if (pending.length === model.columns) flush();
      previousMonth = month;
    }
    flush();
    model.items = photos;
    model.itemCount = photos.length;
    model.totalHeight = model.offsets[model.offsets.length - 1] || 0;
    return {model, changedFrom};
  }

  function rowAtOffset(model, offset) {
    const rowCount = model?.rows?.length || 0;
    if (!rowCount) return 0;
    const target = Math.max(0, finiteNumber(offset, 0));
    let low = 0;
    let high = rowCount;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (model.offsets[middle + 1] <= target) low = middle + 1;
      else high = middle;
    }
    return Math.min(low, rowCount - 1);
  }

  function photoCount(model, start, end) {
    let count = 0;
    for (let index = start; index < end; index += 1) {
      count += model.rows[index]?.photoCount || 0;
    }
    return count;
  }

  function rangeForViewport(model, scrollOffset, viewportHeight, options = {}) {
    const rowCount = model?.rows?.length || 0;
    if (!rowCount) {
      return {
        start: 0, end: 0, visibleStart: 0, visibleEnd: 0,
        top: 0, bottom: 0, mountedPhotos: 0,
      };
    }
    const viewportStart = Math.max(0, finiteNumber(scrollOffset, 0));
    const viewportEnd = viewportStart + Math.max(1, finiteNumber(viewportHeight, 1));
    const visibleStart = rowAtOffset(model, viewportStart);
    let visibleEnd = visibleStart + 1;
    while (visibleEnd < rowCount && model.offsets[visibleEnd] < viewportEnd) {
      visibleEnd += 1;
    }
    const before = Math.max(0, Math.trunc(finiteNumber(
      options.overscanBefore,
      DEFAULT_OVERSCAN_BEFORE,
    )));
    const after = Math.max(0, Math.trunc(finiteNumber(
      options.overscanAfter,
      DEFAULT_OVERSCAN_AFTER,
    )));
    let start = Math.max(0, visibleStart - before);
    let end = Math.min(rowCount, visibleEnd + after);
    const configuredCap = options.cardCap ?? model.cardCap ?? DEFAULT_CARD_CAPS[model.columns];
    const cardCap = Math.max(
      model.columns,
      Math.trunc(finiteNumber(configuredCap, DEFAULT_CARD_CAPS[model.columns])),
    );
    const visiblePhotos = photoCount(model, visibleStart, visibleEnd);
    if (visiblePhotos > cardCap) {
      throw new RangeError('viewport exceeds the row model card cap; rebuild for this viewport height');
    }

    // Preserve every visible row. Trim only overscan, taking from the larger
    // side first so the default four-behind/eight-ahead bias survives normal
    // windows while hard caps protect unusually tall or narrow viewports.
    while (photoCount(model, start, end) > cardCap) {
      const beforeRows = visibleStart - start;
      const afterRows = end - visibleEnd;
      if (afterRows > 0 && afterRows >= beforeRows) end -= 1;
      else if (beforeRows > 0) start += 1;
      else if (afterRows > 0) end -= 1;
      else break;
    }

    return {
      start,
      end,
      visibleStart,
      visibleEnd,
      top: model.offsets[start],
      bottom: Math.max(0, model.totalHeight - model.offsets[end]),
      mountedPhotos: photoCount(model, start, end),
    };
  }

  function thumbnailHintsForRow(rowIndex, range, columnIndex) {
    const visible = rowIndex >= range.visibleStart && rowIndex < range.visibleEnd;
    if (!visible) return {loading: 'lazy', fetchPriority: 'low', visible: false};
    const firstVisiblePhotoRow = rowIndex === range.firstVisiblePhotoRow;
    return {
      loading: 'eager',
      fetchPriority: firstVisiblePhotoRow && columnIndex < 3 ? 'high' : 'auto',
      visible: true,
    };
  }

  function applySelectionState(card, selected, enabled = true) {
    if (!card?.classList || typeof card.setAttribute !== 'function') {
      throw new TypeError('card is required');
    }
    const active = Boolean(enabled && selected);
    card.classList.toggle('chosen', active);
    if (enabled) card.setAttribute('aria-pressed', active ? 'true' : 'false');
    else card.removeAttribute('aria-pressed');
    return active;
  }

  function withVisiblePhotoRow(model, range) {
    let firstVisiblePhotoRow = -1;
    for (let index = range.visibleStart; index < range.visibleEnd; index += 1) {
      if (model.rows[index]?.type === 'photos') {
        firstVisiblePhotoRow = index;
        break;
      }
    }
    return {...range, firstVisiblePhotoRow};
  }

  function metadataBatchSize(options = {}) {
    const columns = normalizedColumns(options.columns);
    const gap = Math.max(0, finiteNumber(options.gap, gapForColumns(columns)));
    const tileSize = tileSizeForWidth(options.width, columns, gap);
    const rowHeight = tileSize + gap;
    const viewportHeight = Math.max(1, finiteNumber(options.viewportHeight, 1));
    const visibleRows = Math.max(1, Math.ceil(viewportHeight / rowHeight));
    const targetRows = visibleRows + DEFAULT_OVERSCAN_BEFORE + DEFAULT_OVERSCAN_AFTER + 4;
    const target = Math.ceil((targetRows * columns) / columns) * columns;
    const minimum = Math.max(30, columns * 2);
    return Math.min(MAX_METADATA_BATCH, Math.max(minimum, target));
  }

  function uniqueMetadataItems(items, seenIds) {
    const seen = seenIds && typeof seenIds.has === 'function' && typeof seenIds.add === 'function'
      ? seenIds
      : new Set();
    const unique = [];
    (Array.isArray(items) ? items : []).forEach((item) => {
      const id = String(item?.id || '');
      if (!id || seen.has(id)) return;
      seen.add(id);
      unique.push(item);
    });
    return unique;
  }

  function isSafeNextCursor(requestCursor, responseCursor, hasMore, seenCursors) {
    if (!hasMore) return true;
    const cursor = String(responseCursor || '');
    if (!cursor || cursor === String(requestCursor || '')) return false;
    return !(seenCursors && typeof seenCursors.has === 'function' && seenCursors.has(cursor));
  }

  function createFrameScheduler(callback, options = {}) {
    if (typeof callback !== 'function') throw new TypeError('callback is required');
    const requestFrame = options.requestFrame
      || global.requestAnimationFrame?.bind(global)
      || ((run) => global.setTimeout(run, 0));
    const cancelFrame = options.cancelFrame
      || global.cancelAnimationFrame?.bind(global)
      || ((handle) => global.clearTimeout(handle));
    let handle = null;
    let latestValue;

    function request(value) {
      latestValue = value;
      if (handle !== null) return false;
      handle = requestFrame(() => {
        handle = null;
        const nextValue = latestValue;
        latestValue = undefined;
        callback(nextValue);
      });
      return true;
    }

    function cancel() {
      if (handle !== null) cancelFrame(handle);
      handle = null;
      latestValue = undefined;
    }

    return {request, cancel, pending: () => handle !== null};
  }

  function createGenerationRequestGate(initialGeneration = 0) {
    let generation = Math.trunc(finiteNumber(initialGeneration, 0));
    let active = null;

    function setGeneration(value) {
      generation = Math.trunc(finiteNumber(value, generation + 1));
      if (active?.generation !== generation) active = null;
      return generation;
    }

    function run(requestGeneration, task) {
      if (typeof task !== 'function') throw new TypeError('task is required');
      if (requestGeneration !== generation) return Promise.resolve(false);
      if (active?.generation === requestGeneration) return active.promise;
      const request = {generation: requestGeneration, promise: null};
      request.promise = Promise.resolve().then(task).finally(() => {
        if (active === request) active = null;
      });
      active = request;
      return request.promise;
    }

    return {
      run,
      setGeneration,
      isCurrent: (value) => value === generation,
      hasActive: () => active !== null,
      generation: () => generation,
    };
  }

  function createWindowManager(options = {}) {
    if (
      typeof options.mount !== 'function'
      || typeof options.unmount !== 'function'
      || typeof options.setSpacers !== 'function'
    ) throw new TypeError('mount, unmount, and setSpacers are required');
    let model = buildRowModel([], {columns: 3, width: 1});
    let mounted = new Map();
    let presentationSignatures = new Map();
    let lastRange = rangeForViewport(model, 0, 1);
    let reconcileRequired = true;

    function presentationSignature(row, index, range) {
      if (row?.type !== 'photos') return 'static';
      if (index === range.firstVisiblePhotoRow) return 'first-visible';
      if (index >= range.visibleStart && index < range.visibleEnd) return 'visible';
      return 'overscan';
    }

    function sameRange(left, right) {
      return Boolean(
        left && right
        && left.start === right.start
        && left.end === right.end
        && left.visibleStart === right.visibleStart
        && left.visibleEnd === right.visibleEnd
        && left.firstVisiblePhotoRow === right.firstVisiblePhotoRow
      );
    }

    function releaseAll() {
      [...mounted.entries()]
        .sort((left, right) => left[0] - right[0])
        .forEach(([index, node]) => options.unmount(node, model.rows[index], index));
      mounted.clear();
      presentationSignatures.clear();
      reconcileRequired = true;
    }

    function setModel(nextModel, setOptions = {}) {
      if (setOptions.preserveRows && Number.isInteger(setOptions.appendFrom)) {
        // Stable prefix indexes: do not rebuild an O(N) key map for every page.
        [...mounted.entries()].forEach(([index, node]) => {
          if (index < setOptions.appendFrom) return;
          options.unmount(node, model.rows[index], index);
          mounted.delete(index);
          presentationSignatures.delete(index);
        });
      } else if (setOptions.preserveRows) {
        const nextIndexesByKey = new Map(
          nextModel.rows.map((row, index) => [row.key, index]),
        );
        const preserved = new Map();
        const preservedSignatures = new Map();
        [...mounted.entries()].forEach(([index, node]) => {
          const nextIndex = nextIndexesByKey.get(model.rows[index]?.key);
          if (nextIndex === undefined || preserved.has(nextIndex)) {
            options.unmount(node, model.rows[index], index);
          } else {
            preserved.set(nextIndex, node);
            if (presentationSignatures.has(index)) {
              preservedSignatures.set(nextIndex, presentationSignatures.get(index));
            }
          }
        });
        mounted = preserved;
        presentationSignatures = preservedSignatures;
      } else {
        releaseAll();
      }
      model = nextModel;
      reconcileRequired = true;
      if (setOptions.preserveRows && mounted.size) {
        const indexes = [...mounted.keys()].sort((left, right) => left - right);
        const start = indexes[0];
        const end = indexes[indexes.length - 1] + 1;
        let firstVisiblePhotoRow = start;
        while (firstVisiblePhotoRow < end && model.rows[firstVisiblePhotoRow]?.type !== 'photos') {
          firstVisiblePhotoRow += 1;
        }
        if (firstVisiblePhotoRow === end) firstVisiblePhotoRow = -1;
        lastRange = {
          start,
          end,
          visibleStart: start,
          visibleEnd: end,
          firstVisiblePhotoRow,
          top: model.offsets[start],
          bottom: Math.max(0, model.totalHeight - model.offsets[end]),
          mountedPhotos: photoCount(model, start, end),
        };
        // Keep the mounted interval at its existing document position while
        // appended metadata extends only the bottom spacer. Resetting top to
        // zero here lets browser scroll anchoring jump by thousands of pixels.
        options.setSpacers(lastRange.top, lastRange.bottom, lastRange);
      } else {
        lastRange = rangeForViewport(model, 0, 1);
        options.setSpacers(0, model.totalHeight, lastRange);
      }
    }

    function update(scrollOffset, viewportHeight) {
      const updateOptions = {
        ...options,
        cardCap: typeof options.cardCap === 'function'
          ? options.cardCap(model.columns)
          : options.cardCap,
      };
      const range = withVisiblePhotoRow(
        model,
        rangeForViewport(model, scrollOffset, viewportHeight, updateOptions),
      );
      // Most scroll frames remain inside the same arithmetic row window. Avoid
      // any mounted-row iteration or image-property writes in that hot path.
      if (!reconcileRequired && sameRange(range, lastRange)) return lastRange;
      [...mounted.entries()].forEach(([index, node]) => {
        if (index >= range.start && index < range.end) return;
        options.unmount(node, model.rows[index], index);
        mounted.delete(index);
        presentationSignatures.delete(index);
      });
      for (let index = range.start; index < range.end; index += 1) {
        if (mounted.has(index)) continue;
        const successorIndex = [...mounted.keys()]
          .filter((candidate) => candidate > index)
          .sort((left, right) => left - right)[0];
        const successor = successorIndex === undefined ? null : mounted.get(successorIndex);
        mounted.set(index, options.mount(model.rows[index], index, successor, range));
        presentationSignatures.set(index, presentationSignature(model.rows[index], index, range));
      }
      if (typeof options.update === 'function') {
        [...mounted.entries()].forEach(([index, node]) => {
          const signature = presentationSignature(model.rows[index], index, range);
          if (presentationSignatures.get(index) === signature) return;
          options.update(node, model.rows[index], index, range);
          presentationSignatures.set(index, signature);
        });
      }
      options.setSpacers(range.top, range.bottom, range);
      lastRange = range;
      reconcileRequired = false;
      return range;
    }

    return {
      setModel,
      update,
      releaseAll,
      model: () => model,
      range: () => lastRange,
      mountedIndexes: () => [...mounted.keys()].sort((left, right) => left - right),
      mountedNodes: () => [...mounted.values()],
    };
  }

  const api = {
    COLUMN_COUNTS,
    DEFAULT_CARD_CAPS,
    DEFAULT_GAPS,
    DEFAULT_OVERSCAN_BEFORE,
    DEFAULT_OVERSCAN_AFTER,
    DEFAULT_MONTH_HEIGHT,
    MAX_METADATA_BATCH,
    normalizedColumns,
    gapForColumns,
    tileSizeForWidth,
    minimumPhotoRowHeight,
    buildRowModel,
    appendRowModel,
    rowAtOffset,
    rangeForViewport,
    withVisiblePhotoRow,
    thumbnailHintsForRow,
    applySelectionState,
    metadataBatchSize,
    uniqueMetadataItems,
    isSafeNextCursor,
    createFrameScheduler,
    createGenerationRequestGate,
    createWindowManager,
  };
  global.DavidPiGalleryWindow = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
