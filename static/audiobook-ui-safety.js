(function installAudiobookUiSafety(global) {
  'use strict';

  function reportSafely(report, error) {
    try { report?.(error); } catch (_ignored) {}
  }

  function guard(operation, report) {
    let result;
    try { result = typeof operation === 'function' ? operation() : operation; }
    catch (error) {
      reportSafely(report, error);
      return Promise.resolve(null);
    }
    return Promise.resolve(result).catch(error => {
      reportSafely(report, error);
      return null;
    });
  }

  function createTeardown({persist, cleanup, report}) {
    let generation = 0;
    let completedGeneration = -1;
    let pending = Promise.resolve(null);
    return {
      reset() { generation += 1; },
      run() {
        if (completedGeneration === generation) return pending;
        completedGeneration = generation;
        const runGeneration = generation;
        let saved;
        try { saved = persist?.(); }
        catch (error) { saved = Promise.reject(error); }
        // Teardown is synchronous so Escape/native close cannot leave hidden
        // audio playing while a network progress write is pending.
        try { cleanup?.(); }
        catch (error) { reportSafely(report, error); }
        pending = guard(saved, error => {
          if (generation === runGeneration) reportSafely(report, error);
        });
        return pending;
      },
    };
  }

  function createGenerationGuard() {
    let generation = 0;
    const listeners = new Set();

    function removeListeners() {
      for (const remove of [...listeners]) {
        try { remove(); } catch (_ignored) {}
      }
      listeners.clear();
    }

    function next() {
      generation += 1;
      removeListeners();
      return generation;
    }

    function current(token) { return token === generation; }

    function listen(token, target, type, handler, options = {}) {
      if (!current(token) || !target?.addEventListener || !target?.removeEventListener) {
        return () => {};
      }
      const once = typeof options === 'object' && Boolean(options.once);
      const listenerOptions = typeof options === 'object' ? {...options, once: false} : options;
      let removed = false;
      const wrapped = (...args) => {
        if (once) remove();
        if (current(token)) handler(...args);
      };
      function remove() {
        if (removed) return;
        removed = true;
        target.removeEventListener(type, wrapped, listenerOptions);
        listeners.delete(remove);
      }
      listeners.add(remove);
      target.addEventListener(type, wrapped, listenerOptions);
      return remove;
    }

    return {
      next,
      cancel: next,
      current,
      listen,
    };
  }

  async function startLocalFirst({openLocal, openOnline, offlineOnly = false, online = true, unavailable}) {
    // Source selection deliberately has no inventory dependency. The OPFS
    // lookup performed by openLocal is authoritative and may complete before,
    // after, or despite a presentation-only shelf refresh.
    if (await openLocal()) return 'offline';
    if (offlineOnly || online === false) {
      unavailable?.();
      return 'unavailable';
    }
    await openOnline();
    return 'online';
  }

  const api = {guard, createTeardown, createGenerationGuard, startLocalFirst};
  global.DavidPiAudiobookSafety = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window);
