(function installSavedDavidPiTheme(global) {
  'use strict';

  const key = 'davidPiThemeV1';
  const cookieKey = 'davidPiThemeV1';
  const colors = Object.freeze({light: '#fffaf2', dark: '#0f1518'});
  const document = global.document;

  function normalize(theme) {
    return theme === 'dark' ? 'dark' : 'light';
  }

  function readSaved() {
    try {
      const saved = global.localStorage?.getItem(key);
      if (saved === 'light' || saved === 'dark') return saved;
    } catch (_error) {
      // Storage can be unavailable in private or embedded browsing contexts.
    }
    return readCookie() || normalize(document?.documentElement?.dataset?.theme);
  }

  function readCookie() {
    try {
      const prefix = `${encodeURIComponent(cookieKey)}=`;
      for (const part of String(document?.cookie || '').split(';')) {
        const candidate = part.trim();
        if (!candidate.startsWith(prefix)) continue;
        const value = decodeURIComponent(candidate.slice(prefix.length));
        if (value === 'light' || value === 'dark') return value;
      }
    } catch (_error) {
      // The cookie is only a non-sensitive pre-paint fallback.
    }
    return null;
  }

  function writeCookie(value) {
    try {
      const secure = global.location?.protocol === 'https:' ? '; Secure' : '';
      document.cookie = `${encodeURIComponent(cookieKey)}=${encodeURIComponent(value)}; Path=/; Max-Age=31536000; SameSite=Lax${secure}`;
    } catch (_error) {
      // localStorage remains authoritative when cookies are unavailable.
    }
  }

  function metadata(name) {
    if (!document?.querySelectorAll) return [];
    return [...document.querySelectorAll(`meta[name="${name}"]`)];
  }

  function notify(value) {
    if (typeof global.dispatchEvent !== 'function' || typeof global.CustomEvent !== 'function') return;
    global.dispatchEvent(new global.CustomEvent('davidpi-themechange', {detail: {theme: value}}));
  }

  const controller = {
    key,
    colors,
    value: 'light',
    apply(theme, {persist = false, announce = true} = {}) {
      const value = normalize(theme);
      const root = document?.documentElement;
      if (root) {
        root.dataset.theme = value;
        root.style.colorScheme = value;
      }
      const themeMetadata = metadata('theme-color');
      // Android owns this native strip. Keep its background and icon-contrast
      // hint stable even when the document switches its independent theme.
      themeMetadata[0]?.setAttribute('content', colors.light);
      themeMetadata[0]?.removeAttribute?.('media');
      themeMetadata.slice(1).forEach(meta => meta.remove?.());
      metadata('color-scheme')[0]?.setAttribute('content', value);
      document?.querySelectorAll?.('meta[name="apple-mobile-web-app-status-bar-style"]')?.forEach(meta => {
        meta.setAttribute('content', value === 'dark' ? 'black-translucent' : 'default');
      });
      if (persist) {
        try { global.localStorage?.setItem(key, value); } catch (_error) { /* optional storage */ }
        writeCookie(value);
      }
      controller.value = value;
      if (announce) notify(value);
      return value;
    },
    refresh({announce = true} = {}) {
      return controller.apply(readSaved(), {announce});
    },
  };

  // One parser-authored light-safe theme hint preserves readable status icons
  // on Android hosts. Only document colors follow the saved preference;
  // the native system-bar hint stays cream in both modes.
  global.__davidPiTheme = controller;
  controller.refresh({announce: false});

  global.addEventListener?.('storage', event => {
    if (event.key === key && (event.newValue === null || event.newValue === 'dark' || event.newValue === 'light')) {
      controller.apply(event.newValue);
    }
  });
  global.addEventListener?.('pageshow', () => controller.refresh());
  if (document?.readyState === 'loading') {
    document.addEventListener?.('DOMContentLoaded', () => controller.refresh(), {once: true});
  }
  document?.addEventListener?.('visibilitychange', () => {
    if (document.visibilityState === 'visible') controller.refresh();
  });
})(globalThis);
