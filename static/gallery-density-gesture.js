(function exposeGalleryDensityGesture(global) {
  'use strict';

  const COLUMN_COUNTS = Object.freeze([3, 6, 9, 13]);

  function normalizeLevel(value) {
    const number = Number(value);
    return Math.max(0, Math.min(COLUMN_COUNTS.length - 1, Number.isFinite(number) ? Math.trunc(number) : 0));
  }

  function modeForLevel(value) {
    const level = normalizeLevel(value);
    const interactive = level === 0;
    return {
      level,
      columns: COLUMN_COUNTS[level],
      interactive,
      mode: interactive ? 'interactive' : 'browse',
      instruction: interactive ? 'Tap to open' : 'Browse only',
    };
  }

  function applyCardMode(card, name, value) {
    const mode = modeForLevel(value);
    const safeName = String(name || 'Media item').slice(0, 240);
    card.disabled = !mode.interactive;
    card.tabIndex = mode.interactive ? 0 : -1;
    card.dataset.browseOnly = mode.interactive ? 'false' : 'true';
    card.setAttribute(
      'aria-label',
      mode.interactive
        ? `Open ${safeName}`
        : `${safeName}. Browse-only thumbnail; switch to 3 across to open or select.`,
    );
    if (mode.interactive) card.removeAttribute('aria-disabled');
    else card.setAttribute('aria-disabled', 'true');
    return mode;
  }

  function createAnchorPreserver(options = {}) {
    if (typeof options.capture !== 'function' || typeof options.restore !== 'function') {
      throw new TypeError('capture and restore are required');
    }
    const requestFrame = typeof options.requestFrame === 'function'
      ? options.requestFrame
      : typeof global.requestAnimationFrame === 'function'
        ? global.requestAnimationFrame.bind(global)
        : (callback) => global.setTimeout(callback, 0);
    const cancelFrame = typeof options.cancelFrame === 'function'
      ? options.cancelFrame
      : typeof global.cancelAnimationFrame === 'function'
        ? global.cancelAnimationFrame.bind(global)
        : (handle) => global.clearTimeout(handle);
    let pendingAnchor = null;
    let scheduledFrame = null;

    function mutate(change) {
      if (typeof change !== 'function') throw new TypeError('change is required');
      if (pendingAnchor === null) pendingAnchor = options.capture() || false;
      change();
      if (pendingAnchor === false) {
        pendingAnchor = null;
        return;
      }
      if (scheduledFrame !== null) return;
      scheduledFrame = requestFrame(() => {
        scheduledFrame = null;
        const anchor = pendingAnchor;
        pendingAnchor = null;
        if (anchor) options.restore(anchor);
      });
    }

    function cancel() {
      if (scheduledFrame !== null) cancelFrame(scheduledFrame);
      scheduledFrame = null;
      pendingAnchor = null;
    }

    return {
      mutate,
      cancel,
      pending: () => pendingAnchor !== null,
    };
  }

  function createIntersectionPageGate() {
    let intersecting = false;
    let loadedForIntersection = false;
    let intentAvailable = false;

    function grantIntent() {
      intentAvailable = true;
      // A fresh user gesture may request another page even when the sentinel
      // never left the short dense viewport after the preceding append.
      loadedForIntersection = false;
    }

    function revokeIntent() {
      intentAvailable = false;
    }

    function update(nextIntersecting, eligible, load) {
      if (!nextIntersecting) {
        intersecting = false;
        loadedForIntersection = false;
        return false;
      }
      if (!intersecting) {
        intersecting = true;
        loadedForIntersection = false;
      }
      if (
        !eligible || !intentAvailable || loadedForIntersection
        || typeof load !== 'function'
      ) return false;
      loadedForIntersection = true;
      intentAvailable = false;
      load();
      return true;
    }

    function reset() {
      intersecting = false;
      loadedForIntersection = false;
      intentAvailable = false;
    }

    return {
      grantIntent,
      revokeIntent,
      update,
      reset,
      isIntersecting: () => intersecting,
      hasLoaded: () => loadedForIntersection,
      hasIntent: () => intentAvailable,
    };
  }

  function finiteCoordinate(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  function distance(points) {
    return Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
  }

  function create(options = {}) {
    const getLevel = options.getLevel;
    const setLevel = options.setLevel;
    if (typeof getLevel !== 'function' || typeof setLevel !== 'function') {
      throw new TypeError('getLevel and setLevel are required');
    }

    const minimumLevel = Number.isInteger(options.minimumLevel) ? options.minimumLevel : 0;
    const maximumLevel = Number.isInteger(options.maximumLevel) ? options.maximumLevel : 3;
    const expandRatio = Number(options.expandRatio) > 1 ? Number(options.expandRatio) : 1.08;
    const contractRatio = Number(options.contractRatio) > 0 && Number(options.contractRatio) < 1
      ? Number(options.contractRatio)
      : 0.92;
    const minimumDistance = Number(options.minimumDistance) > 0 ? Number(options.minimumDistance) : 12;
    const directionSlop = Number(options.directionSlop) >= 0 ? Number(options.directionSlop) : 0.75;
    const suppressMilliseconds = Number(options.suppressMilliseconds) >= 0
      ? Number(options.suppressMilliseconds)
      : 650;
    const now = typeof options.now === 'function'
      ? options.now
      : () => global.performance.now();

    const pointers = new Map();
    let baselineDistance = 0;
    let previousDistance = 0;
    let movementDirection = 0;
    let pinchActive = false;
    let suppressClickUntil = 0;

    function markPinchActivity() {
      suppressClickUntil = Math.max(suppressClickUntil, now() + suppressMilliseconds);
    }

    function pointerDown(pointer) {
      const id = pointer.id;
      if (!pointers.has(id) && pointers.size >= 2) {
        return {accepted: false, consume: pinchActive, captureIds: []};
      }
      pointers.set(id, {x: finiteCoordinate(pointer.x), y: finiteCoordinate(pointer.y)});
      if (pointers.size !== 2) {
        return {accepted: true, consume: false, captureIds: []};
      }

      baselineDistance = distance([...pointers.values()]);
      previousDistance = baselineDistance;
      movementDirection = 0;
      pinchActive = baselineDistance >= minimumDistance;
      if (pinchActive) markPinchActivity();
      return {
        accepted: true,
        consume: pinchActive,
        captureIds: pinchActive ? [...pointers.keys()] : [],
      };
    }

    function pointerMove(pointer) {
      if (!pointers.has(pointer.id)) return {consume: false, changed: false, captureIds: []};
      pointers.set(pointer.id, {x: finiteCoordinate(pointer.x), y: finiteCoordinate(pointer.y)});
      if (pointers.size !== 2) return {consume: false, changed: false, captureIds: []};

      const nextDistance = distance([...pointers.values()]);
      if (!pinchActive || baselineDistance <= 0) {
        // Two fingers can initially land almost together. Claim the gesture as
        // soon as their span is measurable instead of leaving it permanently
        // stranded in one-finger-scroll mode.
        if (nextDistance < minimumDistance) return {consume: false, changed: false, captureIds: []};
        baselineDistance = nextDistance;
        previousDistance = nextDistance;
        movementDirection = 0;
        pinchActive = true;
        markPinchActivity();
        return {consume: true, changed: false, captureIds: [...pointers.keys()]};
      }

      const distanceDelta = nextDistance - previousDistance;
      const nextDirection = Math.abs(distanceDelta) > directionSlop ? Math.sign(distanceDelta) : 0;
      if (nextDirection && movementDirection && nextDirection !== movementDirection) {
        // Anchor a reversal at the latest extreme. Without this, an outward
        // pinch at the largest-tile boundary must contract past its original
        // starting span before a denser level can ever register.
        baselineDistance = previousDistance;
      }
      if (nextDirection) movementDirection = nextDirection;
      const ratio = nextDistance / baselineDistance;
      let nextLevel = Math.max(minimumLevel, Math.min(maximumLevel, Number(getLevel()) || 0));
      let changed = false;
      if (ratio >= expandRatio && nextLevel > minimumLevel) {
        nextLevel -= 1;
        changed = true;
      } else if (ratio <= contractRatio && nextLevel < maximumLevel) {
        nextLevel += 1;
        changed = true;
      }
      if (changed) setLevel(nextLevel);
      if (changed || ratio >= expandRatio || ratio <= contractRatio) {
        // Re-anchor even when the requested step is clamped at a boundary.
        // That makes the opposite gesture relative to the user's current hand
        // position rather than a stale distance from earlier in the pinch.
        baselineDistance = nextDistance;
        movementDirection = 0;
      }
      previousDistance = nextDistance;
      markPinchActivity();
      return {consume: true, changed, level: nextLevel, captureIds: []};
    }

    function pointerEnd(id) {
      const participatedInPinch = pinchActive;
      pointers.delete(id);
      if (pointers.size < 2) {
        baselineDistance = 0;
        previousDistance = 0;
        movementDirection = 0;
        pinchActive = false;
      }
      if (participatedInPinch) markPinchActivity();
      return {suppressClick: participatedInPinch};
    }

    function cancelAll() {
      const participatedInPinch = pinchActive;
      pointers.clear();
      baselineDistance = 0;
      previousDistance = 0;
      movementDirection = 0;
      pinchActive = false;
      if (participatedInPinch) markPinchActivity();
      return {suppressClick: participatedInPinch};
    }

    return {
      pointerDown,
      pointerMove,
      pointerEnd,
      cancelAll,
      shouldSuppressClick: () => now() < suppressClickUntil,
      isPinching: () => pinchActive,
      pointerCount: () => pointers.size,
    };
  }

  const api = {
    create,
    modeForLevel,
    applyCardMode,
    createAnchorPreserver,
    createIntersectionPageGate,
  };
  global.DavidPiGalleryDensityGesture = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
