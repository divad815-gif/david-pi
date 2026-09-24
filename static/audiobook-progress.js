(function installAudiobookProgressQueue(global) {
  'use strict';

  const STORAGE_KEY = 'david-pi-audiobook-progress-v3';
  const LEGACY_KEYS = ['david-pi-audiobook-progress-v1', 'david-pi-audiobook-progress-v2'];
  const MAX_ENTRIES = 100;
  const TOKEN = /^[0-9a-f]{32}$/i;
  const revision = value => Number.isSafeInteger(value) && value >= 0;

  function snapshot(value) {
    if (!value || !revision(value.progress_revision)) return null;
    const position = Number(value.position_seconds);
    if (!Number.isFinite(position) || position < 0) return null;
    return {position_seconds: position, completed: Boolean(value.completed),
      progress_revision: value.progress_revision,
      progress_session: TOKEN.test(value.progress_session || '') ? value.progress_session : '',
      progress_sequence: revision(value.progress_sequence) ? value.progress_sequence : 0};
  }

  function sessionId() {
    if (global.crypto?.randomUUID) return global.crypto.randomUUID().replace(/-/g, '');
    // This is an ordering identifier, not an authentication credential.
    return Array.from({length: 32}, () => Math.floor(Math.random() * 16).toString(16)).join('');
  }

  function normalize(entry) {
    if (!entry || typeof entry !== 'object') return null;
    const scope = String(entry.scope || '');
    const bookId = String(entry.book_id || '');
    const position = Number(entry.position);
    const timestamp = Number(entry.timestamp);
    if (!TOKEN.test(scope) || !TOKEN.test(bookId) || !Number.isFinite(position) || position < 0 || !Number.isFinite(timestamp)) return null;
    const clean = {scope, book_id: bookId, position, completed: Boolean(entry.completed), dirty: Boolean(entry.dirty), timestamp};
    if (TOKEN.test(entry.session_id || '') && revision(entry.base_revision) && revision(entry.sequence) && entry.sequence > 0) {
      Object.assign(clean, {session_id: entry.session_id, base_revision: entry.base_revision, sequence: entry.sequence});
    }
    const current = snapshot(entry.conflict?.current);
    if (current) clean.conflict = {current, reason: entry.conflict.reason === 'legacy_progress' ? 'legacy_progress' : 'stale_progress'};
    if (entry.imported_candidate && current) clean.imported_candidate = true;
    return clean;
  }

  function create(scope, storage = global.localStorage) {
    const selectedScope = TOKEN.test(String(scope || '')) ? String(scope) : '';
    const observed = new Map();
    const session = sessionId();
    // V1 had no identity binding and V2 used a rotating CSRF-derived scope.
    // Neither can be safely attributed to the person currently signed in.
    try { LEGACY_KEYS.forEach(key => storage.removeItem(key)); } catch (_error) {}
    function readAll() {
      try {
        const decoded = JSON.parse(storage.getItem(STORAGE_KEY) || '[]');
        return Array.isArray(decoded) ? decoded.map(normalize).filter(Boolean) : [];
      } catch (_error) { return []; }
    }
    function writeAll(entries) {
      try {
        const bounded = entries.map(normalize).filter(Boolean)
          .sort((left, right) => right.timestamp - left.timestamp).slice(0, MAX_ENTRIES);
        if (bounded.length) storage.setItem(STORAGE_KEY, JSON.stringify(bounded));
        else storage.removeItem(STORAGE_KEY);
        LEGACY_KEYS.forEach(key => storage.removeItem(key));
        return true;
      } catch (_error) { return false; }
    }
    function pending(bookId) {
      if (!selectedScope) return null;
      return readAll().find(entry => entry.scope === selectedScope && entry.book_id === String(bookId)) || null;
    }
    function storeEntry(entry) {
      return writeAll([entry, ...readAll().filter(item => item.scope !== selectedScope || item.book_id !== entry.book_id)]);
    }
    function observe(book) {
      const state = snapshot(book);
      if (state) observed.set(String(book.id), state);
      return state;
    }
    function mark(bookId, position, completed, timestamp = Date.now(), book = null) {
      if (!selectedScope) return null;
      if (book) observe(book);
      const previous = pending(bookId), state = observed.get(String(bookId));
      if (previous?.imported_candidate && previous.conflict) return previous;
      const ordering = previous?.session_id
        ? {session_id: previous.session_id, sequence: previous.sequence + 1, base_revision: previous.base_revision}
        : !previous && state ? {session_id: session, sequence: state.progress_session === session ? state.progress_sequence + 1 : 1, base_revision: state.progress_revision} : {};
      const entry = normalize({scope: selectedScope, book_id: bookId, position, completed, dirty: true,
        timestamp: Math.max(Number(timestamp), (previous?.timestamp ?? -1) + 1), ...ordering,
        ...(previous?.conflict ? {conflict: previous.conflict} : {})});
      if (!entry) return null;
      if (!storeEntry(entry)) throw new Error('This browser could not retain listening progress. Keep this page open.');
      return entry;
    }
    function remove(bookId, timestamp = null) {
      if (!selectedScope) return false;
      const selected = String(bookId);
      return writeAll(readAll().filter(entry => entry.scope !== selectedScope || entry.book_id !== selected ||
        (timestamp !== null && entry.timestamp !== timestamp)));
    }
    function dirtyEntries() {
      if (!selectedScope) return [];
      return readAll().filter(entry => entry.scope === selectedScope && entry.dirty);
    }
    function acknowledge(entry, response) {
      const state = snapshot(response);
      if (!state) throw new Error('The server did not acknowledge an ordered listening update.');
      observed.set(entry.book_id, state);
      const next = pending(entry.book_id);
      if (!next) return;
      if (next.timestamp === entry.timestamp) { remove(entry.book_id, entry.timestamp); return; }
      // Only advance events coalesced behind this exact acknowledged session.
      if (!next.conflict && next.session_id === entry.session_id && next.sequence > entry.sequence && next.base_revision === entry.base_revision) {
        storeEntry({...next, base_revision: state.progress_revision});
      }
    }
    function conflict(entry, response) {
      const current = snapshot(response.current), next = pending(entry.book_id);
      if (!current || !next) return;
      storeEntry({...next, conflict: {current, reason: response.reason}});
    }
    function importLegacy(book, position, completed) {
      const current = snapshot(book);
      if (!current || pending(book.id) || !selectedScope) return;
      const entry = normalize({scope: selectedScope, book_id: book.id, position, completed, dirty: true,
        timestamp: Date.now(), imported_candidate: true, conflict: {current, reason: 'legacy_progress'}});
      if (entry && !storeEntry(entry)) throw new Error('This browser could not retain its earlier listening position.');
    }
    function resolve(bookId, keepCandidate, current) {
      const entry = pending(bookId), state = snapshot(current);
      if (!entry || !state) return null;
      observed.set(String(bookId), state);
      if (!keepCandidate) { remove(bookId); return state; }
      const resolved = normalize({...entry, conflict: null, imported_candidate: false, base_revision: state.progress_revision,
        session_id: sessionId(), sequence: 1, timestamp: Math.max(Date.now(), entry.timestamp + 1)});
      if (!storeEntry(resolved)) throw new Error('This browser could not retain the selected listening position.');
      return resolved;
    }
    return {scope: selectedScope, pending, mark, remove, dirtyEntries, observe, acknowledge, conflict, importLegacy, resolve};
  }

  function createSync(queue, send, {onAcknowledged = () => {}, onConflict = () => {}} = {}) {
    let running = null;
    function flush() {
      if (running) return running;
      running = (async () => {
        const unavailable = new Set();
        while (true) {
          const entry = queue.dirtyEntries().find(item => !item.conflict && !unavailable.has(item.book_id));
          if (!entry) return;
          try {
            const response = await send(entry);
            queue.acknowledge(entry, response);
            await onAcknowledged(entry, response);
          } catch (error) {
            if (error.status === 409 && error.data?.conflict) {
              queue.conflict(entry, error.data);
              onConflict(entry, error.data);
            } else if ([403,404,410].includes(error.status)) {
              // Keep a deleted/revoked book's marker for a possible restore,
              // but it must not block the rest of the listening queue.
              unavailable.add(entry.book_id);
            } else throw error; // Retain the exact event for an idempotent retry.
          }
        }
      })().finally(() => { running = null; });
      return running;
    }
    return {flush};
  }

  global.DavidPiAudiobookProgress = {create, createSync};
})(globalThis);
