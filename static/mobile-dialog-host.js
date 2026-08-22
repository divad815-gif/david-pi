(function () {
  'use strict';

  // Native dialogs work well in ordinary Chrome/Safari and should remain native.
  // The compatibility host exists only for the Android app's embedded WebView,
  // whose user agent includes the `wv` marker and whose dialog top layer has
  // repeatedly produced a backdrop with an invisible or collapsed sheet.
  const isAndroidWebView = /Android/i.test(navigator.userAgent) && /(?:;|\s)wv(?:\)|\s|;)/i.test(navigator.userAgent);
  if (!isAndroidWebView || !window.matchMedia('(max-width: 760px)').matches) return;
  const prototype = Object.getPrototypeOf(document.createElement('dialog'));
  if (!prototype || typeof prototype.showModal !== 'function' || typeof prototype.close !== 'function') return;
  if (prototype.__davidPiOriginalShowModal) return;

  const originalShowModal = prototype.showModal;
  const originalClose = prototype.close;
  // Android WebView's native <dialog> top layer is unreliable in the app shell.
  // Once this compatibility layer is active it must own every dialog on the page;
  // leaving even one native dialog behind produces a backdrop with invisible content.
  const eligible = () => true;
  const openDialogs = [];

  function setImportant(element, property, value) {
    element.style.setProperty(property, value, 'important');
  }

  function viewportSize() {
    const viewport = window.visualViewport;
    return {
      width: Math.max(280, Math.round(viewport ? viewport.width : window.innerWidth)),
      height: Math.max(260, Math.round(viewport ? viewport.height : window.innerHeight)),
      top: Math.max(0, Math.round(viewport ? viewport.offsetTop : 0))
    };
  }

  function sizeHost(dialog, host) {
    if (!host) return;
    const viewport = viewportSize();
    const layer = 2147483000 + Math.max(0, openDialogs.indexOf(dialog));
    setImportant(host, 'position', 'fixed');
    setImportant(host, 'z-index', String(layer));
    setImportant(host, 'box-sizing', 'border-box');
    setImportant(host, 'display', 'block');

    if (dialog.matches('.note-editor, .viewer, .file-viewer, .recipe-view, .place-photo-viewer, .movie-search-sheet')) {
      setImportant(host, 'top', `${viewport.top}px`);
      setImportant(host, 'left', '0px');
      setImportant(host, 'right', 'auto');
      setImportant(host, 'bottom', 'auto');
      setImportant(host, 'width', `${viewport.width}px`);
      setImportant(host, 'max-width', 'none');
      setImportant(host, 'height', `${viewport.height}px`);
      setImportant(host, 'max-height', 'none');
      setImportant(host, 'margin', '0');
      setImportant(host, 'padding', '0');
      const scrollableDocument = dialog.matches('.movie-search-sheet, .recipe-view');
      setImportant(host, 'overflow-x', 'hidden');
      setImportant(host, 'overflow-y', scrollableDocument ? 'auto' : 'hidden');
      if (scrollableDocument) {
        setImportant(host, 'touch-action', 'pan-y');
        setImportant(host, '-webkit-overflow-scrolling', 'touch');
        setImportant(host, 'overscroll-behavior-y', 'contain');
      }
      setImportant(host, 'border-radius', '0');
      setImportant(host, 'transform', 'none');
      return;
    }

    const width = Math.max(280, Math.min(480, viewport.width - 24));
    const left = Math.max(0, Math.round((viewport.width - width) / 2));
    setImportant(host, 'left', `${left}px`);
    setImportant(host, 'right', 'auto');
    setImportant(host, 'width', `${width}px`);
    setImportant(host, 'max-width', 'none');
    setImportant(host, 'margin', '0');
    setImportant(host, 'transform', 'none');
    setImportant(host, 'overflow-x', 'hidden');
    setImportant(host, 'overflow-y', 'auto');

    if (dialog.matches('.small-sheet, .upload-sheet, .assistant-history')) {
      const height = Math.max(260, Math.min(620, viewport.height - 24));
      const top = viewport.top + Math.max(8, Math.round((viewport.height - height) / 2));
      setImportant(host, 'top', `${top}px`);
      setImportant(host, 'bottom', 'auto');
      setImportant(host, 'height', `${height}px`);
      setImportant(host, 'max-height', 'none');
    } else {
      const contentHeight = Math.max(120, Math.min(host.scrollHeight || 220, viewport.height - 24));
      const top = viewport.top + Math.max(8, viewport.height - contentHeight - 12);
      setImportant(host, 'top', `${top}px`);
      setImportant(host, 'bottom', 'auto');
      setImportant(host, 'height', 'auto');
      setImportant(host, 'max-height', `${Math.max(220, viewport.height - 24)}px`);
    }
  }

  function resizeHosts() {
    openDialogs.forEach(dialog => sizeHost(dialog, dialog.__davidPiMobileHost));
  }

  function enableMediaViewerSwipe(dialog, host) {
    if (!dialog.matches('.viewer')) return;
    let startX = null;
    let startY = null;
    let ignored = false;
    host.addEventListener('touchstart', event => {
      const touch = event.changedTouches[0];
      ignored = Boolean(event.target.closest('button, a, video, input, select, textarea'));
      startX = touch?.clientX ?? null;
      startY = touch?.clientY ?? null;
    }, {passive: true});
    host.addEventListener('touchend', event => {
      if (ignored || startX === null || startY === null) return;
      const touch = event.changedTouches[0];
      const dx = (touch?.clientX ?? startX) - startX;
      const dy = (touch?.clientY ?? startY) - startY;
      startX = null;
      startY = null;
      if (Math.abs(dx) < 55 || Math.abs(dx) < Math.abs(dy) * 1.35) return;
      const control = host.querySelector(dx > 0 ? '#previousPhoto' : '#nextPhoto');
      if (control && !control.disabled) control.click();
    }, {passive: true});
  }

  function syncBackdrop() {
    let backdrop = document.querySelector('#david-pi-mobile-dialog-backdrop');
    if (openDialogs.length && !backdrop) {
      backdrop = document.createElement('div');
      backdrop.id = 'david-pi-mobile-dialog-backdrop';
      backdrop.setAttribute('aria-hidden', 'true');
      setImportant(backdrop, 'position', 'fixed');
      setImportant(backdrop, 'inset', '0');
      setImportant(backdrop, 'z-index', '2147482999');
      setImportant(backdrop, 'display', 'block');
      setImportant(backdrop, 'background', 'rgba(31, 28, 24, .62)');
      document.body.appendChild(backdrop);
    } else if (!openDialogs.length && backdrop) {
      backdrop.remove();
    }
    document.documentElement.classList.toggle('davidpi-mobile-dialog-open', Boolean(openDialogs.length));
  }

  function mount(dialog) {
    const host = document.createElement('section');
    host.className = `${dialog.className} davidpi-mobile-dialog-host`;
    host.dataset.dialogId = dialog.id || '';
    host.setAttribute('role', 'dialog');
    host.setAttribute('aria-modal', 'true');
    while (dialog.firstChild) host.appendChild(dialog.firstChild);
    dialog.__davidPiMobileHost = host;
    dialog.__davidPiResizeHost = () => sizeHost(dialog, host);
    dialog.__davidPiReturnFocus = document.activeElement;
    dialog.hidden = true;
    dialog.setAttribute('open', '');
    document.body.appendChild(host);
    openDialogs.push(dialog);
    sizeHost(dialog, host);
    host.addEventListener('submit', event => {
      const form = event.target.closest('form');
      if (!form || String(form.method).toLowerCase() !== 'dialog') return;
      event.preventDefault();
      dialog.close(event.submitter?.value || '');
    });
    enableMediaViewerSwipe(dialog, host);
    syncBackdrop();
    requestAnimationFrame(() => sizeHost(dialog, host));
    requestAnimationFrame(() => {
      const target = host.querySelector('[autofocus], input:not([type="hidden"]), textarea, select, button');
      if (target && typeof target.focus === 'function') target.focus({preventScroll: true});
    });
  }

  function unmount(dialog, value) {
    const host = dialog.__davidPiMobileHost;
    if (!host) return false;
    while (host.firstChild) dialog.appendChild(host.firstChild);
    host.remove();
    dialog.hidden = false;
    dialog.removeAttribute('open');
    if (value !== undefined) dialog.returnValue = String(value);
    const index = openDialogs.indexOf(dialog);
    if (index >= 0) openDialogs.splice(index, 1);
    dialog.__davidPiMobileHost = null;
    dialog.__davidPiResizeHost = null;
    syncBackdrop();
    dialog.dispatchEvent(new Event('close'));
    const returnFocus = dialog.__davidPiReturnFocus;
    dialog.__davidPiReturnFocus = null;
    if (returnFocus && typeof returnFocus.focus === 'function') returnFocus.focus({preventScroll: true});
    return true;
  }

  prototype.__davidPiOriginalShowModal = originalShowModal;
  prototype.__davidPiOriginalClose = originalClose;
  prototype.showModal = function () {
    if (!eligible(this)) return originalShowModal.call(this);
    if (this.__davidPiMobileHost) return;
    mount(this);
  };
  prototype.close = function (value) {
    if (unmount(this, value)) return;
    return originalClose.call(this, value);
  };

  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || !openDialogs.length) return;
    const dialog = openDialogs[openDialogs.length - 1];
    const cancel = new Event('cancel', {cancelable: true});
    if (dialog.dispatchEvent(cancel)) dialog.close();
    event.preventDefault();
  });
  window.addEventListener('resize', resizeHosts, {passive: true});
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', resizeHosts, {passive: true});
    window.visualViewport.addEventListener('scroll', resizeHosts, {passive: true});
  }
})();
