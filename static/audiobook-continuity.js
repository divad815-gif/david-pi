(function installAudiobookContinuity(global) {
  'use strict';

  const STORAGE_KEY = 'david-pi-audiobook-continuity-v1';
  const TOKEN = /^[0-9a-f]{32}$/i;
  const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
  const MAX_SCOPES = 8;

  function fetchWithDeadline(fetcher, input, init = {}, timeoutMs = 5000) {
    const wait = Number(timeoutMs);
    if (!Number.isFinite(wait) || wait <= 0 || typeof global.AbortController !== 'function') {
      return fetcher(input, init);
    }
    const controller = new global.AbortController();
    const timer = global.setTimeout(() => controller.abort('audiobook_network_timeout'), wait);
    return Promise.resolve(fetcher(input, {...init, signal: controller.signal}))
      .finally(() => global.clearTimeout(timer));
  }

  function createStallWatchdog(timeoutMs, handler, timers = global) {
    const wait = Number(timeoutMs);
    let timer = null;
    let generation = 0;
    function cancel() {
      generation += 1;
      if (timer !== null) timers.clearTimeout(timer);
      timer = null;
    }
    function arm(snapshot) {
      cancel();
      if (!Number.isFinite(wait) || wait <= 0 || typeof snapshot !== 'function') return;
      const selectedGeneration = generation;
      timer = timers.setTimeout(() => {
        timer = null;
        if (selectedGeneration !== generation) return;
        Promise.resolve(handler(snapshot())).catch(() => {});
      }, wait);
    }
    return {arm, cancel};
  }

  function normalize(value) {
    if (!value || typeof value !== 'object') return null;
    const scope = String(value.scope || '').toLowerCase();
    const bookId = String(value.book_id || '').toLowerCase();
    const position = Number(value.position);
    const updatedAt = Number(value.updated_at);
    if (!TOKEN.test(scope) || !TOKEN.test(bookId) || !Number.isFinite(position) || position < 0 || !Number.isFinite(updatedAt)) return null;
    return {scope, book_id: bookId, position, updated_at: updatedAt};
  }

  function create(scope, storage = global.localStorage) {
    const selectedScope = TOKEN.test(String(scope || '')) ? String(scope).toLowerCase() : '';

    function readAll() {
      try {
        const decoded = JSON.parse(storage.getItem(STORAGE_KEY) || '[]');
        return Array.isArray(decoded) ? decoded.map(normalize).filter(Boolean) : [];
      } catch (_error) { return []; }
    }

    function writeAll(entries) {
      try {
        const bounded = entries.map(normalize).filter(Boolean)
          .sort((left, right) => right.updated_at - left.updated_at).slice(0, MAX_SCOPES);
        if (bounded.length) storage.setItem(STORAGE_KEY, JSON.stringify(bounded));
        else storage.removeItem(STORAGE_KEY);
        return true;
      } catch (_error) { return false; }
    }

    function read(now = Date.now()) {
      if (!selectedScope) return null;
      const current = Number(now);
      const entries = readAll();
      const fresh = entries.filter(entry => Number.isFinite(current) && current >= entry.updated_at && current - entry.updated_at <= MAX_AGE_MS);
      if (fresh.length !== entries.length) writeAll(fresh);
      return fresh.find(entry => entry.scope === selectedScope) || null;
    }

    function remember(bookId, position, now = Date.now()) {
      if (!selectedScope) return null;
      const entry = normalize({scope: selectedScope, book_id: bookId, position, updated_at: now});
      if (!entry) return null;
      writeAll([entry, ...readAll().filter(item => item.scope !== selectedScope)]);
      return entry;
    }

    function clear(bookId = null) {
      if (!selectedScope) return false;
      const selectedBook = bookId === null ? null : String(bookId).toLowerCase();
      return writeAll(readAll().filter(entry => entry.scope !== selectedScope || (selectedBook !== null && entry.book_id !== selectedBook)));
    }

    return {scope: selectedScope, read, remember, clear};
  }

  global.DavidPiAudiobookContinuity = {create, fetchWithDeadline, createStallWatchdog};
  if (typeof module !== 'undefined' && module.exports) module.exports = {create, fetchWithDeadline, createStallWatchdog};
})(typeof globalThis !== 'undefined' ? globalThis : window);
