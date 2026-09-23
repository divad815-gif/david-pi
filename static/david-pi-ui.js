(function installDavidPiUi(global) {
  'use strict';

  const document = global.document;
  let generatedId = 0;

  function resolve(value, root = document) {
    if (!value || !root) return null;
    return typeof value === 'string' ? root.querySelector(value) : value;
  }

  function nextId(prefix) {
    generatedId += 1;
    return `${prefix}-${generatedId}`;
  }

  function setVisible(element, visible) {
    if (element) element.hidden = !visible;
  }

  function focusableElements(root) {
    if (!root?.querySelectorAll) return [];
    const selector = [
      'a[href]', 'area[href]', 'button:not([disabled])',
      'input:not([disabled]):not([type="hidden"])', 'select:not([disabled])',
      'textarea:not([disabled])', 'iframe', 'object', 'embed',
      '[contenteditable="true"]', '[tabindex]:not([tabindex="-1"])'
    ].join(',');
    return [...root.querySelectorAll(selector)].filter(element => {
      if (element.hidden || element.getAttribute?.('aria-hidden') === 'true') return false;
      if (element.closest?.('[inert]')) return false;
      const style = global.getComputedStyle ? global.getComputedStyle(element) : null;
      return !style || (style.display !== 'none' && style.visibility !== 'hidden');
    });
  }

  const modalStates = new WeakMap();
  const modalAttributes = ['aria-label', 'aria-labelledby', 'aria-describedby'];

  function copyModalSemantics(source, target) {
    modalAttributes.forEach(attribute => {
      if (source?.hasAttribute?.(attribute)) target.setAttribute(attribute, source.getAttribute(attribute));
      else target.removeAttribute?.(attribute);
    });
    target.setAttribute('role', 'dialog');
    target.setAttribute('aria-modal', 'true');
    return target;
  }

  function trapModalFocus(root, event) {
    if (event.key !== 'Tab') return false;
    const items = focusableElements(root);
    if (!items.length) {
      event.preventDefault();
      root.focus?.({preventScroll: true});
      return true;
    }
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && (document?.activeElement === first || !root.contains?.(document?.activeElement))) {
      event.preventDefault();
      last.focus({preventScroll: true});
      return true;
    }
    if (!event.shiftKey && (document?.activeElement === last || !root.contains?.(document?.activeElement))) {
      event.preventDefault();
      first.focus({preventScroll: true});
      return true;
    }
    return false;
  }

  function activateModal(root, options = {}) {
    if (!root || modalStates.has(root)) return modalStates.get(root) || null;
    const ownerDocument = root.ownerDocument || document;
    const returnFocus = options.returnFocus || ownerDocument?.activeElement || null;
    const exclusions = new Set([root, ...(options.exclusions || [])].filter(Boolean));
    const isolation = [];
    [...(ownerDocument?.body?.children || [])].forEach(element => {
      if (exclusions.has(element)) return;
      const prior = {
        element,
        hadInert: element.hasAttribute('inert'),
        inert: Boolean(element.inert),
        ariaHidden: element.getAttribute('aria-hidden')
      };
      isolation.push(prior);
      element.inert = true;
      element.setAttribute('inert', '');
      element.setAttribute('aria-hidden', 'true');
    });
    if (!root.hasAttribute?.('tabindex')) root.setAttribute?.('tabindex', '-1');
    const keydown = event => trapModalFocus(root, event);
    root.addEventListener?.('keydown', keydown);
    const state = {returnFocus, isolation, keydown};
    modalStates.set(root, state);
    const focus = () => {
      if (modalStates.get(root) !== state) return;
      // Do not steal focus from a field the user reached before this frame,
      // or from a newer nested modal that has made this sheet inert.
      if (root.inert || (root.contains?.(ownerDocument?.activeElement) && ownerDocument.activeElement !== root)) return;
      const requested = resolve(options.initialFocus, root);
      const target = requested || focusableElements(root)[0] || root;
      target?.focus?.({preventScroll: true});
    };
    if (global.requestAnimationFrame) global.requestAnimationFrame(focus);
    else global.setTimeout?.(focus, 0);
    return state;
  }

  function deactivateModal(root, options = {}) {
    const state = modalStates.get(root);
    if (!state) return false;
    root.removeEventListener?.('keydown', state.keydown);
    state.isolation.reverse().forEach(prior => {
      prior.element.inert = prior.inert;
      if (prior.hadInert) prior.element.setAttribute('inert', '');
      else prior.element.removeAttribute('inert');
      if (prior.ariaHidden === null) prior.element.removeAttribute('aria-hidden');
      else prior.element.setAttribute('aria-hidden', prior.ariaHidden);
    });
    modalStates.delete(root);
    if (options.restoreFocus !== false && state.returnFocus?.focus) {
      state.returnFocus.focus({preventScroll: true});
    }
    return true;
  }

  function createAnnouncer(options = {}) {
    const ownerDocument = options.document || document;
    const container = options.container || ownerDocument?.body;
    function make(role, live) {
      const node = ownerDocument.createElement('div');
      node.className = 'davidpi-announcer';
      node.setAttribute('role', role);
      node.setAttribute('aria-live', live);
      node.setAttribute('aria-atomic', 'true');
      container?.appendChild(node);
      return node;
    }
    const politeNode = options.polite || make('status', 'polite');
    const alertNode = options.alert || make('alert', 'assertive');
    function announce(node, message) {
      node.textContent = '';
      const write = () => { node.textContent = String(message || ''); };
      if (global.requestAnimationFrame) global.requestAnimationFrame(write);
      else global.setTimeout?.(write, 0);
    }
    return {
      polite: message => announce(politeNode, message),
      alert: message => announce(alertNode, message),
      clear: () => { politeNode.textContent = ''; alertNode.textContent = ''; },
      nodes: {polite: politeNode, alert: alertNode}
    };
  }

  function createAsyncPanel(options = {}) {
    const root = resolve(options.root);
    const regions = {
      loading: resolve(options.loading), content: resolve(options.content),
      empty: resolve(options.empty), noResults: resolve(options.noResults),
      error: resolve(options.error)
    };
    const retry = resolve(options.retry);
    const announcer = options.announcer || null;
    let activeRequest = null;
    let state = 'idle';

    function transition(next, message = '') {
      state = next;
      Object.entries(regions).forEach(([name, element]) => setVisible(element, name === next));
      root?.setAttribute?.('data-state', next);
      root?.setAttribute?.('aria-busy', next === 'loading' ? 'true' : 'false');
      if (message && regions.error && next === 'error') regions.error.textContent = message;
      if (message && announcer) (next === 'error' ? announcer.alert : announcer.polite)(message);
      return state;
    }

    function current(requestId) {
      return requestId === undefined || requestId === activeRequest;
    }

    retry?.addEventListener?.('click', () => options.onRetry?.());
    return {
      begin(requestId = Symbol('request')) { activeRequest = requestId; transition('loading', options.loadingMessage || 'Loading…'); return requestId; },
      success(requestId, result = {}) {
        if (!current(requestId)) return false;
        const next = result.noResults ? 'noResults' : result.empty ? 'empty' : 'content';
        transition(next, result.message || '');
        return true;
      },
      fail(requestId, error) {
        if (!current(requestId)) return false;
        transition('error', error?.message || String(error || 'Something went wrong.'));
        return true;
      },
      transition,
      get state() { return state; },
      get activeRequest() { return activeRequest; }
    };
  }

  function createFilterGroup(rootValue, options = {}) {
    const root = resolve(rootValue);
    if (!root) throw new Error('DavidPiFilterGroup requires a root element.');
    const mode = options.mode === 'tabs' ? 'tabs' : 'toggle';
    const selector = options.selector || 'button';
    const buttons = () => [...root.querySelectorAll(selector)].filter(button => !button.disabled);

    function select(button, notify = true) {
      if (!button || !root.contains(button)) return false;
      buttons().forEach(item => {
        const selected = item === button;
        item.classList.toggle('selected', selected);
        if (mode === 'tabs') {
          item.setAttribute('role', 'tab');
          item.setAttribute('aria-selected', selected ? 'true' : 'false');
          item.tabIndex = selected ? 0 : -1;
          const panelId = item.getAttribute('aria-controls');
          const panel = panelId && (root.ownerDocument || document).getElementById(panelId);
          if (panel) panel.hidden = !selected;
        } else {
          item.setAttribute('aria-pressed', selected ? 'true' : 'false');
        }
      });
      if (notify) options.onChange?.(button);
      return true;
    }

    if (mode === 'tabs') root.setAttribute('role', 'tablist');
    const initial = buttons().find(button => button.classList.contains('selected')) || buttons()[0];
    if (initial) select(initial, false);
    root.addEventListener('click', event => {
      const button = event.target.closest?.(selector);
      if (button) select(button);
    });
    root.addEventListener('keydown', event => {
      if (mode !== 'tabs' || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      const items = buttons();
      const current = Math.max(0, items.indexOf(event.target));
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
        : (current + (event.key === 'ArrowRight' ? 1 : -1) + items.length) % items.length;
      event.preventDefault();
      select(items[index]);
      items[index].focus();
    });
    return {select, buttons, get selected() { return buttons().find(button => button.classList.contains('selected')) || null; }};
  }

  function enhanceForm(formValue, options = {}) {
    const form = resolve(formValue);
    if (!form) throw new Error('DavidPiForm requires a form element.');

    function clearError(control) {
      control.removeAttribute('aria-invalid');
      const errorId = control.dataset.davidpiErrorId;
      if (!errorId) return;
      const error = (form.ownerDocument || document).getElementById(errorId);
      error?.remove();
      const descriptions = (control.getAttribute('aria-describedby') || '').split(/\s+/).filter(id => id && id !== errorId);
      if (descriptions.length) control.setAttribute('aria-describedby', descriptions.join(' '));
      else control.removeAttribute('aria-describedby');
      delete control.dataset.davidpiErrorId;
    }

    function setError(control, message) {
      clearError(control);
      const ownerDocument = form.ownerDocument || document;
      const error = ownerDocument.createElement('p');
      error.id = nextId('davidpi-field-error');
      error.className = 'davidpi-field-error';
      error.textContent = String(message || control.validationMessage || 'Check this field.');
      control.dataset.davidpiErrorId = error.id;
      control.setAttribute('aria-invalid', 'true');
      const descriptions = (control.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
      control.setAttribute('aria-describedby', [...descriptions, error.id].join(' '));
      const anchor = control.closest?.('label') || control;
      if (anchor.insertAdjacentElement) anchor.insertAdjacentElement('afterend', error);
      else (anchor.parentElement || form).appendChild(error);
      return error;
    }

    function validate() {
      const invalid = [...form.querySelectorAll('input, select, textarea')].filter(control => !control.disabled && !control.checkValidity());
      [...form.querySelectorAll('[aria-invalid="true"]')].forEach(clearError);
      invalid.forEach(control => setError(control, control.validationMessage));
      invalid[0]?.focus?.();
      options.onInvalid?.(invalid);
      return invalid.length === 0;
    }

    form.addEventListener('input', event => { if (event.target?.checkValidity?.()) clearError(event.target); });
    return {form, validate, setError, clearError};
  }

  const serverName = global.davidPiServerName || 'David-Pi';
  const installation = global.DavidPiInstallation || {};
  const moduleByPath = {'/photos': 'media', '/mytube': 'mytube', '/audiobooks': 'audiobooks', '/files': 'files', '/notes': 'notes', '/recipes': 'recipes', '/chat': 'chat', '/movies': 'movies', '/places': 'places', '/games': 'games', '/assistant': 'assistant', '/device-backup': 'device_backup', '/api/pihole': 'pihole'};
  const ROUTES = Object.freeze([
    Object.freeze({path: '/', label: 'Home', group: 'Home'}),
    Object.freeze({path: '/photos', label: 'Media', group: 'Library'}),
    Object.freeze({path: '/mytube', label: 'MyTube', group: 'Library'}),
    Object.freeze({path: '/audiobooks', label: 'Audiobooks', group: 'Library'}),
    Object.freeze({path: '/files', label: 'Files', group: 'Library'}),
    Object.freeze({path: '/notes', label: 'Notes', group: 'Library'}),
    Object.freeze({path: '/recipes', label: 'Recipes', group: 'Library'}),
    Object.freeze({path: '/chat', label: 'Chat', group: 'Together'}),
    Object.freeze({path: '/movies', label: 'Movie Night', group: 'Together'}),
    Object.freeze({path: '/places', label: 'Date Night', group: 'Together'}),
    Object.freeze({path: '/games', label: 'Games', group: 'Together'}),
    Object.freeze({path: '/assistant', label: 'Assistant', group: 'System'}),
    Object.freeze({path: '/status', label: 'Server status', group: 'System'}),
    Object.freeze({path: '/device-backup', label: 'Phone backup', group: 'System'}),
    ...(installation.is_admin ? [Object.freeze({path: '/settings', label: 'Settings', group: 'System'})] : [])
  ].filter(route => !installation.modules || !moduleByPath[route.path] || installation.modules[moduleByPath[route.path]] !== 'disabled'));

  function createAppSwitcher(options = {}) {
    const ownerDocument = options.document || document;
    const currentPath = (options.currentPath || global.location?.pathname || '/').replace(/\/$/, '') || '/';
    const nav = ownerDocument.createElement('nav');
    nav.className = options.className || 'davidpi-app-switcher';
    nav.setAttribute('aria-label', options.label || `${serverName} apps`);
    const groups = new Map();
    ROUTES.forEach(route => {
      if (!groups.has(route.group)) groups.set(route.group, []);
      groups.get(route.group).push(route);
    });
    groups.forEach((routes, group) => {
      const section = ownerDocument.createElement('section');
      const heading = ownerDocument.createElement('h2');
      heading.id = nextId('davidpi-route-group');
      heading.textContent = group;
      section.setAttribute('aria-labelledby', heading.id);
      section.appendChild(heading);
      routes.forEach(route => {
        const link = ownerDocument.createElement('a');
        link.href = route.path;
        link.textContent = route.label;
        if (route.path !== '/' && (route.path === currentPath || currentPath.startsWith(`${route.path}/`))) link.setAttribute('aria-current', 'page');
        else if (route.path === currentPath) link.setAttribute('aria-current', 'page');
        section.appendChild(link);
      });
      nav.appendChild(section);
    });
    options.container?.appendChild(nav);
    return nav;
  }

  function syncThemeControls(theme) {
    const value = theme === 'dark' ? 'dark' : 'light';
    document?.querySelectorAll?.('.davidpi-theme-choice')?.forEach(button => {
      const selected = button.dataset.themeChoice === value;
      button.setAttribute('aria-checked', selected ? 'true' : 'false');
      button.tabIndex = selected ? 0 : -1;
    });
    return value;
  }

  function applyTheme(theme, {persist = false} = {}) {
    let value = theme === 'dark' ? 'dark' : 'light';
    const controller = global.__davidPiTheme;
    if (typeof controller?.apply === 'function') {
      value = controller.apply(value, {persist});
      syncThemeControls(value);
      return value;
    }

    // Fail safely when a stale/cached page omitted the early bootstrap.
    const root = document?.documentElement;
    if (!root) return value;
    root.dataset.theme = value;
    root.style.colorScheme = value;
    if (persist) {
      try { global.localStorage?.setItem('davidPiThemeV1', value); } catch (_error) { /* optional storage */ }
    }
    global.__davidPiTheme = {key: 'davidPiThemeV1', value};
    syncThemeControls(value);
    return value;
  }

  global.addEventListener?.('davidpi-themechange', event => syncThemeControls(event.detail?.theme));

  function mountThemeControl(options = {}) {
    const ownerDocument = options.document || document;
    if (!ownerDocument?.body || ownerDocument.querySelector('.davidpi-preferences')) return null;
    const footer = ownerDocument.createElement('footer');
    footer.className = 'davidpi-preferences';
    const label = ownerDocument.createElement('span');
    label.className = 'davidpi-preferences-label';
    label.id = nextId('davidpi-theme-label');
    label.textContent = 'Appearance';
    const group = ownerDocument.createElement('div');
    group.className = 'davidpi-theme-control';
    group.setAttribute('role', 'radiogroup');
    group.setAttribute('aria-labelledby', label.id);
    ['light', 'dark'].forEach(theme => {
      const button = ownerDocument.createElement('button');
      button.type = 'button';
      button.className = 'davidpi-theme-choice';
      button.dataset.themeChoice = theme;
      button.setAttribute('role', 'radio');
      button.textContent = theme === 'light' ? '☀ Light' : '☾ Dark';
      button.addEventListener('click', () => applyTheme(theme, {persist: true}));
      group.appendChild(button);
    });
    group.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      const choices = [...group.querySelectorAll('.davidpi-theme-choice')];
      const current = Math.max(0, choices.indexOf(event.target));
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? choices.length - 1
        : (current + (event.key === 'ArrowRight' ? 1 : -1) + choices.length) % choices.length;
      event.preventDefault();
      applyTheme(choices[index].dataset.themeChoice, {persist: true});
      choices[index].focus();
    });
    footer.append(label, group);
    ownerDocument.body.appendChild(footer);
    applyTheme(ownerDocument.documentElement.dataset.theme || 'light');
    return footer;
  }

  function mountSharedShell(options = {}) {
    const ownerDocument = options.document || document;
    if (!ownerDocument?.querySelector || !ownerDocument?.createElement) return null;
    const main = ownerDocument.querySelector('main');
    if (main && !ownerDocument.querySelector('.davidpi-skip-link')) {
      if (!main.id) main.id = nextId('davidpi-main');
      if (!main.hasAttribute('tabindex')) main.setAttribute('tabindex', '-1');
      const skip = ownerDocument.createElement('a');
      skip.className = 'davidpi-skip-link';
      skip.href = `#${main.id}`;
      skip.textContent = 'Skip to content';
      ownerDocument.body?.prepend(skip);
    }

    const currentPath = (options.currentPath || global.location?.pathname || '/').replace(/\/$/, '') || '/';
    if (currentPath === '/' || !ROUTES.some(route => currentPath === route.path || currentPath.startsWith(`${route.path}/`))) return null;
    if (ownerDocument.querySelector('.davidpi-app-button')) return null;
    const header = ownerDocument.querySelector('.module-bar, .photo-bar, .chat-header');
    if (!header) return null;

    const actionHost = ownerDocument.createElement('div');
    actionHost.className = 'davidpi-header-actions';
    [...header.children].slice(2).forEach(child => actionHost.appendChild(child));
    const trigger = ownerDocument.createElement('button');
    trigger.type = 'button';
    trigger.className = 'davidpi-app-button';
    trigger.setAttribute('aria-label', 'Open app switcher');
    trigger.setAttribute('title', 'Apps');
    trigger.innerHTML = '<span aria-hidden="true">▦</span>';
    actionHost.appendChild(trigger);
    header.appendChild(actionHost);

    const dialog = ownerDocument.createElement('dialog');
    dialog.className = 'davidpi-app-drawer';
    const titleId = nextId('davidpi-app-drawer-title');
    dialog.setAttribute('aria-labelledby', titleId);
    const heading = ownerDocument.createElement('header');
    const title = ownerDocument.createElement('div');
    title.innerHTML = `<p></p><h2 id="${titleId}">Open another app</h2>`;
    title.querySelector('p').textContent = serverName;
    const close = ownerDocument.createElement('button');
    close.type = 'button';
    close.className = 'davidpi-app-drawer-close';
    close.setAttribute('aria-label', 'Close app switcher');
    close.textContent = '×';
    heading.append(title, close);
    const navigation = ownerDocument.createElement('div');
    navigation.className = 'davidpi-app-drawer-body';
    createAppSwitcher({document: ownerDocument, currentPath, container: navigation});
    dialog.append(heading, navigation);
    ownerDocument.body?.appendChild(dialog);

    const closeDialog = () => { if (dialog.open || dialog.__davidPiMobileHost) dialog.close(); };
    trigger.addEventListener('click', () => {
      dialog.showModal();
      if (!dialog.__davidPiMobileHost) activateModal(dialog, {returnFocus: trigger, initialFocus: '.davidpi-app-drawer-close'});
    });
    close.addEventListener('click', closeDialog);
    dialog.addEventListener('cancel', event => { event.preventDefault(); closeDialog(); });
    dialog.addEventListener('click', event => { if (event.target === dialog) closeDialog(); });
    dialog.addEventListener('close', () => deactivateModal(dialog));
    return {header, actionHost, trigger, dialog};
  }

  const AUDIO_SCOPE = /^[0-9a-f]{32}$/i;
  const AUDIO_SESSION = /^[0-9a-z-]{12,96}$/i;
  const AUDIO_COMMANDS = new Set(['play', 'pause', 'back', 'forward', 'stop', 'expand']);

  function audioSessionId() {
    const values = new Uint32Array(4);
    try {
      if (typeof global.crypto?.getRandomValues !== 'function') throw new Error('random unavailable');
      global.crypto.getRandomValues(values);
    }
    catch (_error) { values.set([Date.now(), Math.random() * 0xffffffff, generatedId++, 1]); }
    return `audio-${[...values].map(value => Math.floor(value).toString(36)).join('-')}`;
  }

  function normalizeAudioState(value) {
    if (!value || typeof value !== 'object' || !AUDIO_SESSION.test(String(value.session || ''))) return null;
    const finite = (candidate, fallback = 0) => {
      const number = Number(candidate);
      return Number.isFinite(number) && number >= 0 ? number : fallback;
    };
    return {
      session: String(value.session),
      title: String(value.title || 'Audiobook').slice(0, 180),
      author: String(value.author || 'Unknown author').slice(0, 160),
      playing: Boolean(value.playing),
      minimized: Boolean(value.minimized),
      position: finite(value.position),
      duration: finite(value.duration),
      claimed_at: Math.floor(finite(value.claimed_at)),
      updated_at: finite(value.updated_at, Date.now()),
    };
  }

  function compareAudioAuthority(left, right) {
    const first = normalizeAudioState(left), second = normalizeAudioState(right);
    if (!first) return second ? -1 : 0;
    if (!second) return 1;
    if (first.claimed_at !== second.claimed_at) return first.claimed_at > second.claimed_at ? 1 : -1;
    if (first.session === second.session) return 0;
    return first.session > second.session ? 1 : -1;
  }

  function createPersistentAudio(options = {}) {
    const ownerDocument = options.document === undefined ? document : options.document;
    const scopeValue = options.scope === undefined
      ? ownerDocument?.querySelector?.('meta[name="audiobook-progress-scope"]')?.content
      : options.scope;
    const scope = AUDIO_SCOPE.test(String(scopeValue || '')) ? String(scopeValue).toLowerCase() : '';
    const Channel = options.BroadcastChannel === undefined ? global.BroadcastChannel : options.BroadcastChannel;
    const timers = options.timers || global;
    const now = typeof options.now === 'function' ? options.now : () => Date.now();
    const staleAfterMs = Math.max(3000, Number(options.staleAfterMs || 7000));
    let channel = null, source = null, remoteState = null, dock = null, staleTimer = null, lastClaimedAt = 0;

    try { if (scope && typeof Channel === 'function') channel = new Channel(`david-pi-audio:${scope}`); }
    catch (_error) { channel = null; }

    function post(message) {
      try { channel?.postMessage?.({version: 1, ...message}); }
      catch (_error) { /* Browser media controls remain available without tab coordination. */ }
    }

    function formatTime(seconds) {
      const total = Math.max(0, Math.floor(Number(seconds) || 0));
      const hours = Math.floor(total / 3600), minutes = Math.floor((total % 3600) / 60), remainder = total % 60;
      return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}` : `${minutes}:${String(remainder).padStart(2, '0')}`;
    }

    function makeButton(label, className, command) {
      const button = ownerDocument.createElement('button');
      button.type = 'button'; button.className = className; button.setAttribute('aria-label', label);
      button.addEventListener('click', () => invoke(command));
      return button;
    }

    function ensureDock() {
      if (dock || !ownerDocument?.body || !ownerDocument.createElement) return dock;
      const root = ownerDocument.createElement('aside');
      root.className = 'davidpi-audio-dock'; root.hidden = true;
      root.setAttribute('aria-label', 'Audiobook player');
      const returnButton = makeButton('Return to audiobook player', 'davidpi-audio-copy', 'expand');
      const title = ownerDocument.createElement('strong'), status = ownerDocument.createElement('span');
      // The elapsed time changes every two seconds. Keep that ticking copy out
      // of a live region so screen readers announce controls and user actions,
      // not an endless stream of progress updates.
      returnButton.append(title, status);
      const back = makeButton('Go back 15 seconds', 'davidpi-audio-skip', 'back'); back.textContent = '−15';
      const toggle = makeButton('Pause audiobook', 'davidpi-audio-toggle', 'toggle'); toggle.textContent = 'Ⅱ';
      const forward = makeButton('Skip ahead 30 seconds', 'davidpi-audio-skip', 'forward'); forward.textContent = '+30';
      const stop = makeButton('Stop audiobook', 'davidpi-audio-stop', 'stop'); stop.textContent = '×';
      root.append(returnButton, back, toggle, forward, stop); ownerDocument.body.append(root);
      dock = {root, returnButton, title, status, toggle};
      return dock;
    }

    function render(state, isLocalSource = false) {
      const normalized = normalizeAudioState(state);
      const controls = ensureDock();
      if (!controls) return normalized;
      const visible = Boolean(normalized && (!isLocalSource || normalized.minimized));
      controls.root.hidden = !visible;
      ownerDocument.body.classList?.toggle?.('davidpi-audio-dock-visible', visible);
      if (!visible) return normalized;
      controls.title.textContent = normalized.title;
      controls.status.textContent = `${normalized.playing ? 'Playing' : 'Paused'} · ${formatTime(normalized.position)}${normalized.duration ? ` of ${formatTime(normalized.duration)}` : ''} · ${normalized.author}`;
      controls.toggle.textContent = normalized.playing ? 'Ⅱ' : '▶';
      controls.toggle.setAttribute('aria-label', normalized.playing ? 'Pause audiobook' : 'Play audiobook');
      controls.toggle.dataset.command = normalized.playing ? 'pause' : 'play';
      return normalized;
    }

    function currentState() {
      if (!source) return remoteState;
      let snapshot = {};
      try { snapshot = source.getState?.() || {}; } catch (_error) { snapshot = {}; }
      return normalizeAudioState({...snapshot, session: source.session, claimed_at: source.claimedAt, minimized: source.minimized, updated_at: now()});
    }

    function publish() {
      const state = currentState();
      if (!state || !source) return null;
      render(state, true); post({type: 'state', state}); return state;
    }

    function hideRemote(session = null) {
      if (session && remoteState?.session !== session) return;
      remoteState = null;
      if (!source) render(null, false);
    }

    function armStaleTimer(session) {
      if (staleTimer !== null) timers.clearTimeout?.(staleTimer);
      staleTimer = timers.setTimeout?.(() => { staleTimer = null; hideRemote(session); }, staleAfterMs) ?? null;
    }

    function adoptRemote(state) {
      if (remoteState && compareAudioAuthority(state, remoteState) < 0) return false;
      remoteState = state; render(state, false); armStaleTimer(state.session); return true;
    }

    function yieldSource(winner) {
      if (!source) return false;
      const displaced = source;
      if (displaced.heartbeat !== null) timers.clearInterval?.(displaced.heartbeat);
      source = null;
      adoptRemote(winner);
      // Clear only controls for the displaced session. A remote that already
      // observed the winner will ignore this deliberately stale ending.
      post({type: 'ended', session: displaced.session, superseded_by: winner.session});
      try { displaced.onTakeover?.(winner); }
      catch (_error) { /* Authority has already moved; a failed UI callback cannot revive the loser. */ }
      return true;
    }

    function receive(message) {
      const value = message?.data === undefined ? message : message.data;
      if (!value || value.version !== 1) return false;
      if (value.type === 'hello') { if (source) publish(); return true; }
      if (value.type === 'command' && source && value.session === source.session && AUDIO_COMMANDS.has(value.command)) {
        try { source.onCommand?.(value.command); } catch (_error) { /* A media command must not break the page. */ }
        return true;
      }
      if (value.type === 'ended') { hideRemote(String(value.session || '')); return true; }
      if (value.type !== 'state' && value.type !== 'claim') return false;
      const state = normalizeAudioState(value.state);
      if (!state) return false;
      if (source) {
        const incumbent = currentState();
        if (state.session === incumbent?.session) return true;
        if (compareAudioAuthority(state, incumbent) > 0) return yieldSource(state);
        // Simultaneous claims use timestamp then session ID as a total order.
        // Reasserting the winner makes the losing claimant yield even when its
        // original claim arrived while it still considered itself remote.
        publish(); return true;
      }
      return adoptRemote(state);
    }

    function invoke(command) {
      const selected = command === 'toggle'
        ? ((currentState()?.playing) ? 'pause' : 'play')
        : command;
      if (!AUDIO_COMMANDS.has(selected)) return false;
      const state = currentState();
      if (!state) return false;
      if (source) {
        try { source.onCommand?.(selected); } catch (_error) { return false; }
      } else {
        post({type: 'command', session: state.session, command: selected});
        if (selected === 'expand') {
          try { global.opener?.focus?.(); } catch (_error) { /* The return control remains harmless. */ }
        }
      }
      return true;
    }

    function releaseSource({announce = true} = {}) {
      if (!source) return;
      const session = source.session;
      if (source.heartbeat !== null) timers.clearInterval?.(source.heartbeat);
      source = null;
      if (announce) post({type: 'ended', session});
      render(remoteState, false);
    }

    function claim({getState, onCommand, onTakeover} = {}) {
      releaseSource();
      if (staleTimer !== null) timers.clearTimeout?.(staleTimer);
      staleTimer = null; remoteState = null;
      const observedNow = Math.max(0, Math.floor(Number(now()) || Date.now()));
      lastClaimedAt = Math.max(lastClaimedAt + 1, observedNow);
      source = {session: audioSessionId(), claimedAt: lastClaimedAt, getState, onCommand, onTakeover, minimized: false, heartbeat: null};
      const claimedSession = source.session;
      if (typeof timers.setInterval === 'function') source.heartbeat = timers.setInterval(publish, 2000);
      post({type: 'claim', state: currentState()});
      // A higher simultaneous claim can displace this source synchronously in
      // BroadcastChannel test doubles, or asynchronously in a real browser.
      if (source?.session !== claimedSession) return null;
      publish();
      return {
        get session() { return claimedSession; },
        publish() { return source?.session === claimedSession ? publish() : null; },
        setMinimized(value) {
          if (source?.session !== claimedSession) return false;
          source.minimized = Boolean(value); publish(); return true;
        },
        release(options) {
          if (source?.session !== claimedSession) return false;
          releaseSource(options); return true;
        },
      };
    }

    function routeIsInternal(href) {
      try {
        const url = new URL(href, global.location?.href || 'https://david-pi.invalid/');
        const origin = global.location?.origin || url.origin;
        if (url.origin !== origin || url.searchParams.has('download')) return false;
        return ROUTES.some(route => route.path === '/'
          ? url.pathname === '/'
          : url.pathname === route.path || url.pathname.startsWith(`${route.path}/`));
      } catch (_error) { return false; }
    }

    function openRoute(href) {
      if (!source || !routeIsInternal(href) || typeof global.open !== 'function') return false;
      let opened = null;
      try { opened = global.open(new URL(href, global.location.href).href, 'david-pi-browse'); }
      catch (_error) { return false; }
      if (!opened) return false;
      try { opened.focus?.(); } catch (_error) { /* Opening succeeded even if focus is unavailable. */ }
      return true;
    }

    if (channel) {
      channel.addEventListener?.('message', receive);
      if (!channel.addEventListener) channel.onmessage = receive;
      post({type: 'hello'});
    }
    return Object.freeze({scope, supported: Boolean(channel), claim, publish, invoke, receive, routeIsInternal, openRoute});
  }

  global.DavidPiModal = Object.freeze({activate: activateModal, deactivate: deactivateModal, copySemantics: copyModalSemantics, focusableElements, trapFocus: trapModalFocus});
  global.AsyncPanel = Object.freeze({create: createAsyncPanel});
  global.DavidPiFilterGroup = Object.freeze({create: createFilterGroup});
  global.DavidPiForm = Object.freeze({enhance: enhanceForm});
  global.DavidPiAnnouncer = Object.freeze({create: createAnnouncer});
  global.DavidPiAppSwitcher = Object.freeze({routes: ROUTES, create: createAppSwitcher, mount: mountSharedShell});
  global.DavidPiTheme = Object.freeze({apply: applyTheme, mount: mountThemeControl});
  const persistentAudio = createPersistentAudio();
  global.DavidPiPersistentAudio = Object.freeze({...persistentAudio, create: createPersistentAudio});
  if (document?.querySelector && document?.createElement) {
    const mountSharedUi = () => { mountSharedShell(); mountThemeControl(); };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountSharedUi, {once: true});
    else mountSharedUi();
  }
})(globalThis);
