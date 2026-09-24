(function installBrowserAudiobooks(global) {
  'use strict';

  const ROOT = 'david-pi-audiobooks-v1';
  const LEGACY_LIBRARY = 'library.json';
  const MAX_BYTES = 4 * 1024 * 1024 * 1024;
  const MAX_BOOKS = 100;
  const RESERVE_BYTES = 128 * 1024 * 1024;
  const ID = /^[0-9a-f]{32}$/;
  const SCOPE = /^[0-9a-f]{32}$/;
  const SHA256 = /^[0-9a-f]{64}$/;
  const LOCAL_ROUTE = '/__davidpi_offline/audiobooks/';
  const CHECKPOINT_BYTES = 4 * 1024 * 1024;
  const IO_TIMEOUT_MS = 30000;
  const PLAYBACK_PROBE_TIMEOUT_MS = 3000;
  const LOCK_PREFIX = 'david-pi-audiobook-storage-v3:';
  const RESERVATIONS = 'reservations.json';
  const fallbackLocks = new Map(), downloads = new Map();
  const cancelChannel = global.BroadcastChannel ? new global.BroadcastChannel('david-pi-audiobook-downloads-v1') : null;
  if (cancelChannel) cancelChannel.onmessage = event => {
    if (event.data?.type === 'cancel') cancel(event.data.id, false);
  };

  function cancelled() { return new Error('Download paused. Its durable checkpoint can be resumed.'); }
  function checkSignal(signal) { if (signal?.aborted) throw signal.reason || cancelled(); }

  async function bounded(operation, signal, milliseconds = IO_TIMEOUT_MS, label = 'The operation timed out. Try again.') {
    checkSignal(signal);
    let timer, abort;
    try {
      return await Promise.race([operation(), new Promise((resolve, reject) => {
        timer = setTimeout(() => reject(new Error(label)), milliseconds);
        abort = () => reject(signal.reason || cancelled());
        signal?.addEventListener('abort', abort, {once: true});
      })]);
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
  }

  async function fetchBounded(url, options, signal, timeout = IO_TIMEOUT_MS) {
    const controller = new AbortController();
    const abort = () => controller.abort(signal?.reason);
    signal?.addEventListener('abort', abort, {once: true});
    try { return await bounded(() => fetch(url, {...options, signal: controller.signal}), signal, timeout); }
    catch (error) { controller.abort(); throw error; }
    // The download's controller stays linked after headers, through body
    // consumption. save() aborts it on every terminal outcome.
  }

  function cancel(id = null, broadcast = true) {
    for (const [bookId, controller] of downloads) if (!id || id === bookId) controller.abort(cancelled());
    if (broadcast) cancelChannel?.postMessage({type: 'cancel', id});
  }

  function storageInteger(value, {positive = false} = {}) {
    if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
    const number = Number(value);
    if (!Number.isSafeInteger(number) || number < (positive ? 1 : 0)) return null;
    return number;
  }

  function storageSum(...values) {
    let total = 0;
    for (const value of values) {
      const number = storageInteger(value);
      if (number === null || number > Number.MAX_SAFE_INTEGER - total) return null;
      total += number;
    }
    return total;
  }

  function supported() {
    return Boolean(
      global.isSecureContext
      && global.navigator?.storage?.getDirectory
      && global.navigator?.locks?.request
    );
  }

  function hasCapacity(bookBytes, estimate) {
    const size = storageInteger(bookBytes, {positive: true});
    const quota = storageInteger(estimate?.quota);
    const usage = storageInteger(estimate?.usage);
    const projected = storageSum(usage, size, RESERVE_BYTES);
    return size !== null && size <= MAX_BYTES
      && quota !== null && usage !== null && usage <= quota
      && projected !== null && projected <= quota;
  }

  function canSaveBook(book) { return book?.visibility === 'shared'; }

  function cleanBook(book) {
    const id = String(book?.id || '');
    const byteSize = Number(book?.byte_size || 0);
    if (!canSaveBook(book)) throw new Error('Private audiobooks can only be saved in the Android app.');
    if (!ID.test(id) || storageInteger(byteSize, {positive: true}) === null || byteSize > MAX_BYTES) {
      throw new Error('This audiobook cannot be saved by this browser.');
    }
    const downloadUrl = new URL(String(book.download_url || ''), global.location?.origin);
    if (downloadUrl.origin !== global.location?.origin || downloadUrl.pathname !== `/api/audiobooks/${id}/download`) {
      throw new Error('The audiobook source is invalid.');
    }
    const suppliedScope = String(book.progress_scope || '').toLowerCase();
    const contentSha256 = String(book.sha256 || book.content_sha256 || '').toLowerCase();
    if (!SHA256.test(contentSha256)) throw new Error('This audiobook is missing a trusted content fingerprint.');
    return {
      id,
      title: String(book.title || 'Audiobook').slice(0, 240),
      author: String(book.author || '').slice(0, 240),
      series: String(book.series || '').slice(0, 240),
      duration_seconds: Number.isFinite(Number(book.duration_seconds))
        ? Math.max(0, Number(book.duration_seconds)) : 0,
      byte_size: byteSize,
      content_type: String(book.content_type || 'application/octet-stream').slice(0, 120),
      download_url: downloadUrl.pathname,
      position_seconds: Number.isFinite(Number(book.position_seconds))
        ? Math.max(0, Number(book.position_seconds)) : 0,
      completed: Boolean(book.completed),
      progress_scope: SCOPE.test(suppliedScope) ? suppliedScope : '',
      ...(Number.isSafeInteger(book.progress_revision) && book.progress_revision >= 0 ? {progress_revision: book.progress_revision} : {}),
      content_sha256: contentSha256,
      saved_at: Date.now(),
      visibility: 'shared',
    };
  }

  async function locked(operation, name = 'catalog', options = {}) {
    if (global.navigator?.locks?.request) {
      return global.navigator.locks.request(
        `${LOCK_PREFIX}${name}`, {mode: 'exclusive', ...options}, operation,
      );
    }
    const run = (fallbackLocks.get(name) || Promise.resolve()).then(operation);
    fallbackLocks.set(name, run.catch(() => {}));
    return run;
  }

  function bookLocked(id, operation) { return locked(operation, `book:${id}`); }
  function downloadRecordName(id) { return `${id}.download.json`; }

  async function reserve(directory, id, total) {
    await locked(async () => {
      let reservations = await readJson(directory, RESERVATIONS, {});
      if (!reservations || typeof reservations !== 'object' || Array.isArray(reservations)) reservations = {};
      // A crashed document automatically loses its Web Lock. Do not expire a
      // live reservation merely because a browser froze a background tab.
      if (global.navigator.locks.query) {
        const held = new Set((await global.navigator.locks.query()).held.map(lock => lock.name));
        reservations = Object.fromEntries(Object.entries(reservations).filter(([key]) => held.has(`${LOCK_PREFIX}download:${key}`)));
      }
      const otherBytes = Object.entries(reservations).reduce((sum, [key, bytes]) => key === id ? sum : storageSum(sum, bytes), 0);
      const estimate = await global.navigator.storage.estimate();
      if (otherBytes === null || !hasCapacity(total, {...estimate, usage: storageSum(estimate.usage, otherBytes)})) {
        throw new Error('This device does not report enough browser storage to finish this book plus a safety reserve.');
      }
      const all = await records(directory, false);
      const ids = new Set([...all.filter(record => record.state === 'complete').map(record => record.book.id), ...Object.keys(reservations), id]);
      if (ids.size > MAX_BOOKS) throw new Error('This browser already has 100 saved books. Remove one before saving another.');
      reservations[id] = total;
      await writeJson(directory, RESERVATIONS, reservations);
    });
  }

  async function releaseReservation(id) {
    await locked(async () => {
      const directory = await root(false), reservations = await readJson(directory, RESERVATIONS, {});
      if (!Object.hasOwn(reservations || {}, id)) return;
      delete reservations[id];
      if (Object.keys(reservations).length) await writeJson(directory, RESERVATIONS, reservations);
      else await removeIfPresent(directory, RESERVATIONS);
    });
  }

  function workerClient(signal) {
    if (!global.Worker) throw new Error('This browser cannot checkpoint downloads safely. Existing saved books still play.');
    const worker = new global.Worker('/static/audiobook-offline-worker.js?v=1');
    let serial = 0, current = null;
    worker.onmessage = event => {
      if (!current || event.data?.request_id !== current.id) return;
      current.touch();
      if (event.data.busy) return;
      if (event.data.error) current.reject(new Error(event.data.error));
      else current.resolve(event.data.result);
    };
    worker.onerror = () => current?.reject(new Error('Offline storage worker failed. The last checkpoint is retained.'));
    return {
      request(command, data = {}, transfer = []) {
        checkSignal(signal);
        return new Promise((resolve, reject) => {
          const id = ++serial;
          let timer;
          const cleanup = () => { clearTimeout(timer); signal?.removeEventListener('abort', abort); current = null; };
          const fail = error => { cleanup(); worker.terminate(); reject(error); };
          const abort = () => fail(signal.reason || cancelled());
          const touch = () => { clearTimeout(timer); timer = setTimeout(() => fail(new Error('Offline storage stopped responding. Resume from the last checkpoint.')), IO_TIMEOUT_MS); };
          current = {id, touch, resolve: result => { cleanup(); resolve(result); }, reject: fail};
          signal?.addEventListener('abort', abort, {once: true});
          touch();
          try { worker.postMessage({request_id: id, command, ...data}, transfer); } catch (error) { fail(error); }
        });
      },
      close() { worker.terminate(); },
    };
  }

  async function root(create = true) {
    const storageRoot = await global.navigator.storage.getDirectory();
    return storageRoot.getDirectoryHandle(ROOT, {create});
  }

  async function readJson(directory, name, fallback = null) {
    try {
      const handle = await directory.getFileHandle(name);
      return JSON.parse(await (await handle.getFile()).text());
    } catch (_error) { return fallback; }
  }

  async function writeJson(directory, name, value) {
    const handle = await directory.getFileHandle(name, {create: true});
    const writable = await handle.createWritable();
    try {
      await writable.write(JSON.stringify(value));
      await writable.close();
    } catch (error) {
      try { await writable.abort(); } catch (_ignored) {}
      throw error;
    }
  }

  function recordName(id) { return `${id}.json`; }
  function mediaName(id) { return `${id}.audio.part`; }

  function validRecord(value) {
    const id = String(value?.book?.id || '');
    return Boolean(
      value && [2, 3].includes(value.version) && ID.test(id)
      && value.book.visibility === 'shared'
      && ['partial', 'complete'].includes(value.state)
      && storageInteger(value.expected_bytes, {positive: true}) !== null
      && value.expected_bytes <= MAX_BYTES
      && [`${id}.audio.part`, `${id}.audio`].includes(value.file_name)
      && ['etag', 'last-modified', 'legacy'].includes(value.validator?.type)
      && Boolean(value.validator?.value)
    );
  }

  function integrityVerified(record) {
    return Boolean(
      record?.version === 3
      && SHA256.test(String(record.content_sha256 || ''))
      && record.content_sha256 === record.book?.content_sha256
      && Number.isSafeInteger(record.integrity_verified_at)
      && record.integrity_verified_at > 0
    );
  }

  async function verifyHandle(handle, expectedSha256, signal) {
    if (!SHA256.test(String(expectedSha256 || ''))) {
      throw new Error('Cryptographic offline verification is unavailable.');
    }
    const file = await handle.getFile();
    const worker = workerClient(signal);
    let actual;
    try { actual = await worker.request('verify', {file}); } finally { worker.close(); }
    if (actual !== expectedSha256) throw new Error('The saved audiobook failed its integrity check.');
    return actual;
  }

  async function names(directory) {
    const result = [];
    if (typeof directory.entries === 'function') {
      for await (const [name] of directory.entries()) result.push(name);
    } else if (typeof directory.values === 'function') {
      for await (const handle of directory.values()) result.push(handle.name);
    }
    return result;
  }

  async function fileSize(directory, name) {
    try { return Number((await (await directory.getFileHandle(name)).getFile()).size); }
    catch (error) {
      if (String(error?.name || error).match(/NotFound|not found/i)) return -1;
      throw error;
    }
  }

  async function migrateLegacy(directory) {
    const legacy = await readJson(directory, LEGACY_LIBRARY, null);
    if (!legacy || !Array.isArray(legacy.books)) return;
    const remaining = [];
    for (const book of legacy.books) {
      const id = String(book?.id || '');
      if (book?.visibility !== 'shared' || !ID.test(id)) {
        // Never discard a legacy manifest entry that cannot be safely bound.
        remaining.push(book);
        continue;
      }
      const existing = await readJson(directory, recordName(id), null);
      if (
        validRecord(existing)
        && await fileSize(directory, existing.file_name) === existing.expected_bytes
      ) continue;
      try {
        const old = await directory.getFileHandle(`${id}.audio`);
        const file = await old.getFile();
        if (!file.size) throw new Error('empty legacy copy');
        const migrated = cleanBook({...book, byte_size: file.size, progress_scope: ''});
        await verifyHandle(old, migrated.content_sha256);
        await writeJson(directory, recordName(id), {
          version: 3,
          state: 'complete',
          expected_bytes: file.size,
          file_name: `${id}.audio`,
          validator: {type: 'legacy', value: `size-${file.size}`},
          content_sha256: migrated.content_sha256,
          integrity_verified_at: Date.now(),
          book: migrated,
          updated_at: Date.now(),
        });
      } catch (_error) {
        remaining.push(book);
      }
    }
    if (remaining.length) {
      try {
        await writeJson(directory, LEGACY_LIBRARY, {
          ...legacy,
          books: remaining,
          migration_pending: true,
        });
      } catch (_error) {
        // The original manifest remains the recovery source if rewriting fails.
      }
      return;
    }
    try { await directory.removeEntry(LEGACY_LIBRARY); } catch (_error) {}
  }

  async function records(directory, migrate = true) {
    if (migrate) await migrateLegacy(directory);
    const output = [];
    for (const name of await names(directory)) {
      if (!/^[0-9a-f]{32}(?:\.download)?\.json$/.test(name)) continue;
      const record = await readJson(directory, name, null);
      if (validRecord(record)) output.push(record);
    }
    const published = new Set(output.filter(record => record.state === 'complete' && integrityVerified(record))
      .map(record => `${record.book.id}:${record.file_name}`));
    // A committed copy remains complete even if removing its obsolete staging
    // manifest failed. Such a sidecar must not masquerade as a second download.
    return output.filter(record => record.state === 'complete' || !published.has(`${record.book.id}:${record.file_name}`));
  }

  function emptyInventory() {
    return {
      supported: supported(),
      books: [],
      partials: [],
      complete_count: 0,
      partial_count: 0,
      complete_bytes: 0,
      partial_bytes: 0,
      quota_bytes: null,
      usage_bytes: null,
      available_bytes: null,
      persistent: null,
      has_data: false,
      has_unresolved_data: false,
    };
  }

  async function readEstimate(summary) {
    try {
      const estimate = await global.navigator.storage.estimate();
      const quota = storageInteger(estimate?.quota);
      const usage = storageInteger(estimate?.usage);
      summary.quota_bytes = quota;
      summary.usage_bytes = usage;
      summary.available_bytes = quota !== null && usage !== null && usage <= quota ? quota - usage : null;
    } catch (_error) {}
    try {
      summary.persistent = global.navigator.storage.persisted
        ? Boolean(await global.navigator.storage.persisted()) : null;
    } catch (_error) {}
  }

  async function inventoryUnlocked() {
    const summary = emptyInventory();
    if (!summary.supported) return summary;
    await readEstimate(summary);
    try {
      const directory = await root(false);
      const all = await records(directory);
      const directoryNames = await names(directory);
      summary.has_data = directoryNames.length > 0;
      const recognized = new Set([RESERVATIONS]);
      for (const record of all) {
        recognized.add(recordName(record.book.id));
        recognized.add(downloadRecordName(record.book.id));
        recognized.add(record.file_name);
        const rawSize = await fileSize(directory, record.file_name);
        const actualBytes = Number.isSafeInteger(record.checkpoint_bytes)
          ? Math.min(storageInteger(rawSize) ?? 0, record.checkpoint_bytes) : storageInteger(rawSize) ?? 0;
        if (record.state === 'complete' && actualBytes === record.expected_bytes && integrityVerified(record)) {
          summary.books.push(record.book);
          summary.complete_bytes = storageSum(summary.complete_bytes, actualBytes) ?? summary.complete_bytes;
        } else {
          summary.partials.push({
            ...record.book,
            downloaded_bytes: Math.min(actualBytes, record.expected_bytes),
            expected_bytes: record.expected_bytes,
          });
          summary.partial_bytes = storageSum(summary.partial_bytes, actualBytes) ?? summary.partial_bytes;
        }
      }
      summary.books.sort((left, right) => Number(right.saved_at || 0) - Number(left.saved_at || 0));
      summary.partials.sort((left, right) => Number(right.saved_at || 0) - Number(left.saved_at || 0));
      summary.complete_count = summary.books.length;
      summary.partial_count = summary.partials.length;
      summary.has_unresolved_data = directoryNames.some(name => !recognized.has(name));
      return summary;
    } catch (error) {
      if (String(error?.name || error).match(/NotFound|not found/i)) return summary;
      throw error;
    }
  }

  function inventory() { return locked(inventoryUnlocked, 'lifecycle', {mode: 'shared'}); }
  async function listUnlocked() { return (await inventoryUnlocked()).books; }
  function list() { return locked(listUnlocked, 'lifecycle', {mode: 'shared'}); }

  function validatorFrom(response) {
    const etag = response.headers?.get?.('etag');
    if (etag) return {type: 'etag', value: etag};
    const modified = response.headers?.get?.('last-modified');
    return modified ? {type: 'last-modified', value: modified} : null;
  }

  function sameValidator(left, right) {
    return Boolean(left && right && left.type === right.type && left.value === right.value);
  }

  function contentRange(response, start, total) {
    const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers?.get?.('content-range') || '');
    if (!match) return false;
    const first = storageInteger(match[1]);
    const last = storageInteger(match[2]);
    const size = storageInteger(match[3], {positive: true});
    return first === start && last !== null && last >= start && last < total && size === total;
  }

  async function persistenceStatus() {
    try { return global.navigator.storage.persist ? await global.navigator.storage.persist() : false; }
    catch (_error) { return false; }
  }

  async function removeIfPresent(directory, name) {
    try { await directory.removeEntry(name); }
    catch (error) {
      if (!String(error?.name || error).match(/NotFound|not found/i)) throw error;
    }
  }

  async function saveUnlocked(book, onProgress, signal) {
    if (!supported()) throw new Error('Offline browser storage is unavailable here.');
    const clean = cleanBook(book);
    const directory = await root(true);
    const original = await bookLocked(clean.id, () => readJson(directory, recordName(clean.id), null));
    const intact = validRecord(original) && original.state === 'complete' && integrityVerified(original)
      && await fileSize(directory, original.file_name) === original.expected_bytes;
    const pending = await readJson(directory, downloadRecordName(clean.id), null);
    const current = validRecord(pending) ? pending : original;

    const head = await fetchBounded(clean.download_url, {method: 'HEAD', credentials: 'same-origin', cache: 'no-store'}, signal);
    if (!head.ok) throw new Error(`Could not verify the audiobook (${head.status}).`);
    const total = storageInteger(head.headers?.get?.('content-length'), {positive: true});
    const validator = validatorFrom(head);
    if (total !== clean.byte_size || !validator || String(head.headers?.get?.('accept-ranges') || '').toLowerCase() !== 'bytes') {
      throw new Error('The server did not provide a stable resumable audiobook source.');
    }

    const currentValid = validRecord(current);
    if (
      intact && original.expected_bytes === total && original.content_sha256 === clean.content_sha256
    ) {
      if (clean.progress_scope && original.book.progress_scope !== clean.progress_scope) {
        // Rebinding is explicit (the signed-in person pressed Save again) and
        // starts from that person's server position, never unattributed legacy
        // progress from a shared browser profile.
        original.book = {...clean, saved_at: original.book.saved_at || Date.now()};
        original.updated_at = Date.now();
        await bookLocked(clean.id, () => writeJson(directory, recordName(clean.id), original));
        return {persistent: await persistenceStatus(), resumed: false, rebound: true, alreadySaved: true};
      }
      throw new Error('This book is already saved in this browser.');
    }

    const targetName = intact ? (original.file_name === mediaName(clean.id) ? `${clean.id}.audio` : mediaName(clean.id))
      : currentValid ? current.file_name : mediaName(clean.id);
    const manifestName = intact ? downloadRecordName(clean.id) : recordName(clean.id);
    const handle = await directory.getFileHandle(targetName, {create: true});
    let written = storageInteger((await handle.getFile()).size);
    if (written === null) throw new Error('The partial browser copy has an invalid size.');
    const resumable = currentValid
      && current.file_name === targetName
      && sameValidator(current.validator, validator)
      && current.expected_bytes === total
      && (!current.content_sha256 || current.content_sha256 === clean.content_sha256)
      && written <= total;
    if (resumable && Number.isSafeInteger(current.checkpoint_bytes)) written = Math.min(written, current.checkpoint_bytes);
    if (!resumable) {
      written = 0;
    }

    const record = {
      version: 3,
      state: written === total ? 'complete' : 'partial',
      expected_bytes: total,
      file_name: targetName,
      validator,
      content_sha256: clean.content_sha256,
      checkpoint_bytes: written,
      book: {...clean, ...(currentValid && current.book.progress_scope === clean.progress_scope ? {
        position_seconds: current.book.position_seconds, completed: current.book.completed,
        ...(Number.isSafeInteger(current.book.progress_revision) ? {progress_revision: current.book.progress_revision} : {}),
      } : {})},
      updated_at: Date.now(),
    };
    const obsoleteName = intact ? original.file_name : currentValid && current.file_name !== targetName ? current.file_name : '';
    async function publish() {
      await bookLocked(clean.id, async () => {
        const latest = await readJson(directory, recordName(clean.id), null);
        if (validRecord(latest) && latest.book.progress_scope === clean.progress_scope) {
          for (const key of ['position_seconds', 'completed', 'progress_revision']) {
            if (Object.hasOwn(latest.book, key)) record.book[key] = latest.book[key];
          }
        }
        await writeJson(directory, recordName(clean.id), record);
        if (manifestName !== recordName(clean.id)) {
          try { await removeIfPresent(directory, manifestName); } catch (_error) {}
        }
      });
      if (obsoleteName) { try { await removeIfPresent(directory, obsoleteName); } catch (_error) {} }
    }
    if (written === total) {
      try {
        await verifyHandle(handle, clean.content_sha256, signal);
        record.integrity_verified_at = Date.now();
        record.book.saved_at = Date.now();
        await publish();
        return {persistent: await persistenceStatus(), resumed: true, recovered: true, verified: true};
      } catch (error) {
        record.state = 'partial';
        record.checkpoint_bytes = 0;
        delete record.integrity_verified_at;
        await bookLocked(clean.id, () => writeJson(directory, manifestName, record));
        throw error;
      }
    }

    // Cross-tab reservations remain conservative even on an in-place writer.
    await reserve(directory, clean.id, total);
    const persistent = await persistenceStatus();
    await bookLocked(clean.id, () => writeJson(directory, manifestName, record));
    const resumedAt = written;
    const headers = {};
    if (written) {
      headers.Range = `bytes=${written}-`;
      headers['If-Range'] = validator.value;
    }
    const response = await fetchBounded(clean.download_url, {credentials: 'same-origin', cache: 'no-store', headers}, signal);
    if (!response.ok || !response.body) throw new Error(`Download failed (${response.status}).`);
    if (written && response.status !== 206) {
      throw new Error('The audiobook changed or the server could not safely resume it. Remove the partial copy and try again.');
    }
    if (written && (!contentRange(response, written, total) || !sameValidator(validatorFrom(response), validator))) {
      throw new Error('The audiobook changed, so this partial copy was not continued. Remove it and start again.');
    }
    if (!written && (response.status !== 200 || storageInteger(response.headers?.get?.('content-length'), {positive: true}) !== total || !sameValidator(validatorFrom(response), validator))) {
      throw new Error('The download response did not match the verified audiobook.');
    }

    let writer, reader;
    try {
      writer = workerClient(signal);
      await writer.request('open', {root: ROOT, name: targetName, offset: written, total});
      reader = response.body.getReader();
      while (true) {
        const {done, value} = await bounded(() => reader.read(), signal, IO_TIMEOUT_MS, 'Download stalled. Resume from the last saved checkpoint.');
        if (done) break;
        const nextWritten = storageSum(written, value?.byteLength);
        if (nextWritten === null || nextWritten > total) throw new Error('The server returned more data than expected.');
        // Limit each transfer as well as each durable checkpoint. Streaming
        // input is untrusted and need not respect the browser's usual size.
        for (let offset = 0; offset < value.byteLength;) {
          const count = Math.min(value.byteLength - offset, CHECKPOINT_BYTES - (written - record.checkpoint_bytes));
          const chunk = value.slice(offset, offset + count);
          const result = await writer.request('write', {bytes: chunk}, [chunk.buffer]);
          offset += count;
          written = result.written;
          if (result.checkpoint > record.checkpoint_bytes) {
            record.checkpoint_bytes = result.checkpoint;
            record.updated_at = Date.now();
            await bookLocked(clean.id, () => writeJson(directory, manifestName, record));
          }
        }
        onProgress?.(Math.min(100, Math.round(written * 100 / total)));
      }
      const finished = await writer.request('finish', {sha256: clean.content_sha256});
      record.checkpoint_bytes = finished.written;
      writer.close();writer=null;
      if ((await handle.getFile()).size !== total) throw new Error('The saved copy was incomplete.');
      record.state = 'complete';
      record.integrity_verified_at = Date.now();
      record.book.saved_at = Date.now();
      record.updated_at = Date.now();
      await publish();
      return {persistent, resumed: resumedAt > 0, verified: true};
    } catch (error) {
      writer?.close();
      if (reader?.cancel) { try { await bounded(() => reader.cancel(), null, 1000); } catch (_ignored) {} }
      record.state = 'partial';
      record.updated_at = Date.now();
      if (/integrity check/.test(error.message)) record.checkpoint_bytes = 0;
      try { await bookLocked(clean.id, () => writeJson(directory, manifestName, record)); } catch (_ignored) {}
      throw error;
    }
  }

  async function save(book, onProgress, {signal} = {}) {
    const id = cleanBook(book).id;
    if (downloads.has(id)) throw new Error('This book is already downloading. Pause it before resuming.');
    const controller = new AbortController();
    const abort = () => controller.abort(signal.reason || cancelled());
    if (signal?.aborted) abort();
    signal?.addEventListener('abort', abort, {once: true});
    downloads.set(id, controller);
    try {
      return await locked(() => locked(async () => {
        try { return await saveUnlocked(book, onProgress, controller.signal); }
        finally { await releaseReservation(id).catch(() => {}); }
      }, `download:${id}`, {signal: controller.signal}), 'lifecycle', {mode: 'shared', signal: controller.signal});
    } finally { controller.abort();downloads.delete(id);signal?.removeEventListener('abort', abort); }
  }

  async function file(id) {
    if (!ID.test(String(id || ''))) throw new Error('Invalid audiobook id.');
    return bookLocked(id, async () => {
      const directory = await root(false);
      const record = await readJson(directory, recordName(id), null);
      if (!validRecord(record) || record.state !== 'complete' || !integrityVerified(record)) throw new Error('This offline copy is incomplete or unverified.');
      const value = await (await directory.getFileHandle(record.file_name)).getFile();
      if (value.size !== record.expected_bytes) throw new Error('This offline copy is incomplete.');
      return value;
    });
  }

  function localUrl(id) {
    if (!ID.test(String(id || ''))) throw new Error('Invalid audiobook id.');
    return `${LOCAL_ROUTE}${id}`;
  }

  function playable(id, progressScope) {
    const selectedId = String(id || '').toLowerCase();
    const selectedScope = String(progressScope || '').toLowerCase();
    if (!ID.test(selectedId) || !SCOPE.test(selectedScope)) return Promise.resolve(null);
    return bookLocked(selectedId, async () => {
      try {
        const directory = await root(false);
        const record = await readJson(directory, recordName(selectedId), null);
        if (
          !validRecord(record) || record.state !== 'complete' || !integrityVerified(record)
          || await fileSize(directory, record.file_name) !== record.expected_bytes
        ) return null;
        const recordScope = String(record.book.progress_scope || '').toLowerCase();
        if (recordScope && recordScope !== selectedScope) return null;
        if (!recordScope) {
          // Early David-Pi browser downloads predate identity-scoped progress.
          // The dedicated offline shelf already exposes those local copies, so
          // bind one atomically to the current signed-in identity the first
          // time it is opened from the normal player. This preserves the
          // downloaded bytes while making all later progress updates scoped.
          record.book.progress_scope = selectedScope;
          record.updated_at = Date.now();
          await writeJson(directory, recordName(selectedId), record);
        }
        return {...record.book, local_url: localUrl(selectedId)};
      } catch (error) {
        if (String(error?.name || error).match(/NotFound|not found/i)) return null;
        throw error;
      }
    });
  }

  async function testLocalPlayback(id) {
    const url = localUrl(id);
    const controller = new AbortController(), signal = controller.signal;
    try {
      const head = await fetchBounded(url, {method: 'HEAD', cache: 'no-store'}, signal, PLAYBACK_PROBE_TIMEOUT_MS);
      if (!head.ok || head.headers.get('accept-ranges') !== 'bytes') throw new Error('The local player is not ready. Reload this page and try again.');
      const total = storageInteger(head.headers.get('content-length'), {positive: true});
      if (total === null) throw new Error('The local copy has an invalid size.');
      const range = await fetchBounded(url, {headers: {Range: 'bytes=0-0'}, cache: 'no-store'}, signal, PLAYBACK_PROBE_TIMEOUT_MS);
      if (range.status !== 206 || range.headers.get('content-range') !== `bytes 0-0/${total}` || (await bounded(() => range.arrayBuffer(), signal, PLAYBACK_PROBE_TIMEOUT_MS)).byteLength !== 1) {
        throw new Error('The local player failed its byte-range check.');
      }
      return {bytes: total};
    } finally { controller.abort(); }
  }

  async function buildPlaybackSource(playableBook) {
    // A controlled page gets efficient byte-range playback through the
    // service worker. A newly-installed, stale, or unavailable worker must
    // not make an otherwise verified OPFS copy unplayable, so fall back to a
    // browser-owned Blob URL after the range probe fails (or cannot run).
    if (global.navigator?.serviceWorker?.controller) {
      try {
        await testLocalPlayback(playableBook.id);
        return {
          ...playableBook,
          playback_url: playableBook.local_url,
          transport: 'service-worker',
          revoke_url: false,
        };
      } catch (_error) {
        // The OPFS record is independently verified below before fallback.
      }
    }

    const value = await file(playableBook.id);
    const contentType = /^audio\/[a-z0-9.+-]+$/i.test(String(playableBook.content_type || ''))
      ? playableBook.content_type : 'application/octet-stream';
    // File.slice creates a view with corrected MIME metadata and does not
    // eagerly duplicate a multi-gigabyte OPFS-backed audiobook.
    const media = value.type === contentType || contentType === 'application/octet-stream'
      ? value : value.slice(0, value.size, contentType);
    const playbackUrl = global.URL?.createObjectURL?.(media);
    if (!playbackUrl) throw new Error('This browser could not open the verified offline copy.');
    return {
      ...playableBook,
      playback_url: playbackUrl,
      transport: 'object-url',
      revoke_url: true,
    };
  }

  async function playbackSource(id, progressScope) {
    const playableBook = await playable(id, progressScope);
    return playableBook ? buildPlaybackSource(playableBook) : null;
  }

  async function managerPlaybackSource(id) {
    const selectedId = String(id || '').toLowerCase();
    if (!ID.test(selectedId)) return null;
    const playableBook = await bookLocked(selectedId, async () => {
      try {
        const directory = await root(false);
        const record = await readJson(directory, recordName(selectedId), null);
        if (
          !validRecord(record) || record.state !== 'complete' || !integrityVerified(record)
          || await fileSize(directory, record.file_name) !== record.expected_bytes
        ) return null;
        return {...record.book, local_url: localUrl(selectedId)};
      } catch (error) {
        if (String(error?.name || error).match(/NotFound|not found/i)) return null;
        throw error;
      }
    });
    return playableBook ? buildPlaybackSource(playableBook) : null;
  }

  function releasePlaybackSource(source) {
    if (!source?.revoke_url || !source.playback_url) return;
    try { global.URL?.revokeObjectURL?.(source.playback_url); } catch (_error) {}
  }

  function updatePosition(id, position, completed, progressScope = '') {
    if (!ID.test(String(id || ''))) return Promise.resolve();
    return bookLocked(id, async () => {
      const directory = await root(false);
      const record = await readJson(directory, recordName(id), null);
      if (!validRecord(record) || record.state !== 'complete') return;
      const selectedScope = String(progressScope || '').toLowerCase();
      if (selectedScope && (!SCOPE.test(selectedScope) || String(record.book.progress_scope || '').toLowerCase() !== selectedScope)) return;
      const nextPosition = Number(position);
      record.book.position_seconds = Number.isFinite(nextPosition) ? Math.max(0, nextPosition) : 0;
      record.book.completed = Boolean(completed);
      record.updated_at = Date.now();
      await writeJson(directory, recordName(id), record);
    });
  }

  function updateProgressRevision(id, state, progressScope) {
    if (!ID.test(String(id || '')) || !Number.isSafeInteger(state?.progress_revision) || state.progress_revision < 0) return Promise.resolve();
    return bookLocked(id, async () => {
      let directory;
      try { directory = await root(false); } catch (_error) { return; }
      const record = await readJson(directory, recordName(id), null);
      if (!validRecord(record) || record.book.progress_scope !== progressScope) return;
      record.book.progress_revision = state.progress_revision;
      await writeJson(directory, recordName(id), record);
    });
  }

  function remove(id) {
    if (!ID.test(String(id || ''))) return Promise.resolve();
    cancel(id);
    return waitForDownloadLock(id, () => bookLocked(id, async () => {
      const directory = await root(false);
      const record = await readJson(directory, recordName(id), null);
      if (validRecord(record)) {
        // A failed deletion must not leave metadata claiming a missing media
        // file is complete. This partial marker is repairable by Save.
        record.state = 'partial';
        record.updated_at = Date.now();
        await writeJson(directory, recordName(id), record);
      }
      const targets = new Set([mediaName(id), `${id}.audio`]);
      if (record?.file_name) targets.add(record.file_name);
      for (const target of targets) await removeIfPresent(directory, target);
      await removeIfPresent(directory, recordName(id));
      await removeIfPresent(directory, downloadRecordName(id));
    }));
  }

  async function waitForDownloadLock(id, operation) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error('Another tab is still closing this download. Try again.')), IO_TIMEOUT_MS);
    try { return await locked(operation, id ? `download:${id}` : 'lifecycle', {signal: controller.signal}); }
    finally { clearTimeout(timer); }
  }

  async function hasStoredData() {
    if (!supported()) return false;
    try { return (await names(await root(false))).length > 0; }
    catch (_error) { return false; }
  }

  function removeAll() {
    if (!supported()) return Promise.resolve();
    cancel();
    return waitForDownloadLock(null, async () => {
      const storageRoot = await global.navigator.storage.getDirectory();
      try { await storageRoot.removeEntry(ROOT, {recursive: true}); }
      catch (error) {
        if (!String(error?.name || error).match(/NotFound|not found/i)) throw error;
      }
      try {
        await storageRoot.getDirectoryHandle(ROOT, {create: false});
        throw new Error('Browser copies could not be fully removed.');
      } catch (error) {
        if (String(error?.name || error).match(/NotFound|not found/i)) return;
        throw error;
      }
    });
  }

  const api = {supported, hasCapacity, canSaveBook, inventory, list, save, cancel, file, localUrl, playable, testLocalPlayback, playbackSource, managerPlaybackSource, releasePlaybackSource, updatePosition, updateProgressRevision, remove, removeAll, hasStoredData};
  global.DavidPiBrowserAudiobooks = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window);
