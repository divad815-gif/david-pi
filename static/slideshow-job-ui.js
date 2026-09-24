(function installSlideshowJobUi(global) {
  'use strict';

  const JOB_ID = /^[0-9a-f]{32}$/;
  const ACTIVE_STATES = new Set(['queued', 'working']);
  const DEFAULT_STORAGE_KEY = 'davidPiActiveSlideshowJobV1';
  const DEFAULT_RETRY_DELAYS = [1500, 3000, 6000, 12000, 15000];
  const MAX_POLL_DELAY = 15000;

  function validJobId(value) {
    const jobId = String(value || '').trim().toLowerCase();
    return JOB_ID.test(jobId) ? jobId : '';
  }

  function safeCall(callback, value, report) {
    try {
      return Promise.resolve(callback?.(value)).catch(error => {
        try { report?.(error); } catch (_ignored) {}
      });
    } catch (error) {
      try { report?.(error); } catch (_ignored) {}
      return Promise.resolve();
    }
  }

  function create(options = {}) {
    if (typeof options.submit !== 'function' || typeof options.poll !== 'function') {
      throw new TypeError('Slideshow submission and polling functions are required.');
    }
    let storage = options.storage;
    if (!storage) {
      try { storage = global.sessionStorage; } catch (_ignored) { storage = null; }
    }
    const storageKey = options.storageKey || DEFAULT_STORAGE_KEY;
    const schedule = options.schedule || ((callback, delay) => global.setTimeout(callback, delay));
    const cancelSchedule = options.cancelSchedule || (handle => global.clearTimeout(handle));
    const pollInterval = Math.min(MAX_POLL_DELAY, Math.max(250, Number(options.pollInterval) || 1500));
    const retryDelays = (Array.isArray(options.retryDelays) && options.retryDelays.length
      ? options.retryDelays : DEFAULT_RETRY_DELAYS)
      .map(delay => Math.min(MAX_POLL_DELAY, Math.max(pollInterval, Number(delay) || pollInterval)));

    function loadStoredJob() {
      let jobId = '';
      try { jobId = validJobId(storage?.getItem(storageKey)); } catch (_ignored) {}
      if (!jobId) {
        try { storage?.removeItem(storageKey); } catch (_ignored) {}
      }
      return jobId;
    }

    let activeId = loadStoredJob();
    let current = activeId ? {
      phase: 'accepted', id: activeId, accepted: true, active: true, terminal: false,
      progress: 5,
      message: 'Video creation was accepted. Checking its progress now; you can close this window.',
    } : {
      phase: 'idle', id: '', accepted: false, active: false, terminal: false,
      progress: 0, message: '',
    };
    let generation = 0;
    let watchingId = '';
    let timer = null;
    let inFlight = null;
    let retryIndex = 0;
    let submission = null;
    let destroyed = false;
    const handledTerminalIds = new Set();

    function emit(next) {
      current = {...next};
      try { options.onState?.({...current}); } catch (error) {
        try { options.onCallbackError?.(error); } catch (_ignored) {}
      }
      return current;
    }

    function storeActive(jobId) {
      try { storage?.setItem(storageKey, jobId); } catch (_ignored) {}
    }

    function clearStored(jobId) {
      try {
        if (validJobId(storage?.getItem(storageKey)) === jobId) storage.removeItem(storageKey);
      } catch (_ignored) {}
    }

    function cancelTimer() {
      if (timer === null) return;
      try { cancelSchedule(timer); } catch (_ignored) {}
      timer = null;
    }

    function stopWatching() {
      generation += 1;
      cancelTimer();
      watchingId = '';
      inFlight = null;
      retryIndex = 0;
    }

    function observationState(jobId) {
      return {
        phase: 'observation-error', id: jobId, accepted: true, active: true,
        terminal: false, progress: Number(current.progress) || 5,
        message: 'Video creation was accepted. David-Pi will keep working; status will retry automatically.',
      };
    }

    function schedulePoll(token, jobId, delay) {
      if (destroyed || token !== generation || activeId !== jobId) return;
      cancelTimer();
      timer = schedule(() => {
        timer = null;
        return runPoll(token, jobId);
      }, delay);
    }

    async function finish(jobId, job, phase) {
      if (handledTerminalIds.has(jobId)) return;
      handledTerminalIds.add(jobId);
      stopWatching();
      if (activeId === jobId) activeId = '';
      clearStored(jobId);
      const completed = phase === 'completed';
      const unavailable = phase === 'unavailable';
      const terminal = {
        phase, id: jobId, accepted: true, active: false, terminal: true,
        progress: completed ? 100 : Math.max(0, Math.min(100, Number(job?.progress) || 0)),
        result_photo_id: job?.result_photo_id || null,
        message: completed
          ? 'Your slideshow is ready.'
          : unavailable
            ? 'The accepted video job is no longer available to this account.'
            : `${job?.error || 'The slideshow could not be created.'} Nothing was removed.`,
      };
      emit(terminal);
      await safeCall(completed ? options.onComplete : options.onFailure, {...job, ...terminal}, options.onCallbackError);
    }

    async function observe(token, jobId) {
      let job;
      try {
        job = await options.poll(jobId);
      } catch (error) {
        if (destroyed || token !== generation || activeId !== jobId) return;
        if (error?.status === 403 || error?.status === 404) {
          await finish(jobId, {}, 'unavailable');
          return;
        }
        emit(observationState(jobId));
        const delay = retryDelays[Math.min(retryIndex, retryDelays.length - 1)];
        retryIndex = Math.min(retryIndex + 1, retryDelays.length - 1);
        schedulePoll(token, jobId, delay);
        return;
      }
      if (destroyed || token !== generation || activeId !== jobId) return;
      if (validJobId(job?.id) !== jobId) {
        emit(observationState(jobId));
        const delay = retryDelays[Math.min(retryIndex, retryDelays.length - 1)];
        retryIndex = Math.min(retryIndex + 1, retryDelays.length - 1);
        schedulePoll(token, jobId, delay);
        return;
      }
      const status = String(job?.status || '').toLowerCase();
      if (status === 'completed' || status === 'failed') {
        await finish(jobId, job, status);
        return;
      }
      if (!ACTIVE_STATES.has(status)) {
        emit(observationState(jobId));
        const delay = retryDelays[Math.min(retryIndex, retryDelays.length - 1)];
        retryIndex = Math.min(retryIndex + 1, retryDelays.length - 1);
        schedulePoll(token, jobId, delay);
        return;
      }
      retryIndex = 0;
      const progress = Math.max(5, Math.min(100, Number(job.progress) || 0));
      const serverMessage = typeof job.message === 'string' && job.message.trim()
        ? job.message.trim() : 'Creating your video…';
      emit({
        phase: status, id: jobId, accepted: true, active: true, terminal: false,
        progress,
        message: `${serverMessage} You can close this window; David-Pi will keep working.`,
      });
      schedulePoll(token, jobId, pollInterval);
    }

    function runPoll(token, jobId) {
      if (destroyed || token !== generation || activeId !== jobId) return Promise.resolve();
      let tracked;
      tracked = Promise.resolve().then(() => observe(token, jobId)).finally(() => {
        if (inFlight === tracked) inFlight = null;
      });
      inFlight = tracked;
      return tracked;
    }

    function ensureWatching() {
      if (destroyed || !activeId) return false;
      if (watchingId === activeId && (inFlight || timer !== null)) return false;
      stopWatching();
      watchingId = activeId;
      const token = generation;
      void runPoll(token, activeId);
      return true;
    }

    async function submit(payload) {
      if (activeId) {
        ensureWatching();
        return {accepted: true, resumed: true, id: activeId};
      }
      if (submission) return submission;
      emit({
        phase: 'submitting', id: '', accepted: false, active: false, terminal: false,
        progress: 5, message: 'Submitting your video job to David-Pi…',
      });
      submission = (async () => {
        try {
          const result = await options.submit(payload);
          const jobId = validJobId(result?.id);
          if (!jobId) throw new Error('David-Pi did not return a valid video job.');
          activeId = jobId;
          storeActive(jobId);
          emit({
            phase: 'accepted', id: jobId, accepted: true, active: true, terminal: false,
            progress: 5,
            message: 'Video creation was accepted. You can close this window; David-Pi will keep working.',
          });
          if (!destroyed) ensureWatching();
          return {accepted: true, resumed: Boolean(result.duplicate), id: jobId};
        } catch (error) {
          const detail = typeof error?.message === 'string' && error.message.trim()
            ? error.message.trim() : 'David-Pi could not accept the video job.';
          emit({
            phase: 'submission-error', id: '', accepted: false, active: false, terminal: true,
            progress: 0, message: `${detail} No video job was accepted.`,
          });
          return {accepted: false, error};
        } finally {
          submission = null;
        }
      })();
      return submission;
    }

    function resume() {
      if (destroyed || !activeId) return false;
      if (!current.accepted || current.id !== activeId) {
        emit({
          phase: 'accepted', id: activeId, accepted: true, active: true, terminal: false,
          progress: 5,
          message: 'Video creation was accepted. Checking its progress now; you can close this window.',
        });
      }
      ensureWatching();
      return true;
    }

    return Object.freeze({
      submit,
      resume,
      activeJobId: () => activeId,
      snapshot: () => ({...current}),
      destroy() { destroyed = true; stopWatching(); },
    });
  }

  function bindDialogLifecycle({dialog, pause, isSubmitting, onBlockedClose} = {}) {
    if (!dialog?.addEventListener || !dialog?.removeEventListener) {
      throw new TypeError('A dialog event target is required.');
    }
    const pauseSafely = () => { try { pause?.(); } catch (_ignored) {} };
    const onCancel = event => {
      if (!isSubmitting?.()) return;
      event.preventDefault();
      pauseSafely();
      try { onBlockedClose?.(); } catch (_ignored) {}
    };
    const onClose = () => pauseSafely();
    dialog.addEventListener('cancel', onCancel);
    dialog.addEventListener('close', onClose);
    return Object.freeze({
      close() {
        pauseSafely();
        if (dialog.open || dialog.__davidPiMobileHost) dialog.close();
      },
      destroy() {
        dialog.removeEventListener('cancel', onCancel);
        dialog.removeEventListener('close', onClose);
      },
    });
  }

  const api = Object.freeze({create, bindDialogLifecycle, validJobId});
  global.DavidPiSlideshowJobs = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window);
