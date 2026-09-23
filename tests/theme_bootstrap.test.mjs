import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../static/theme-bootstrap.js', import.meta.url), 'utf8');

class FakeMeta {
  constructor(attributes = {}) { this.attributes = new Map(Object.entries(attributes)); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  remove() { this.removed = true; }
}

function parserAuthoredMetadata() {
  return {
    'theme-color': [
      new FakeMeta({name: 'theme-color', content: '#fffaf2'}),
    ],
    'color-scheme': [new FakeMeta({name: 'color-scheme', content: 'light'})],
  };
}

function fixture(saved = 'light', initial = parserAuthoredMetadata(), {throwOnRead = false} = {}) {
  const metas = new Map();
  Object.entries(initial).forEach(([name, values]) => metas.set(name, values));
  const documentListeners = new Map();
  const globalListeners = new Map();
  const stored = new Map(saved === null ? [] : [['davidPiThemeV1', saved]]);
  const document = {
    readyState: 'loading',
    visibilityState: 'visible',
    cookie: '',
    documentElement: {dataset: {}, style: {}},
    head: {
      children: [...metas.values()].flat(),
      appendChild(meta) {
        this.children.push(meta);
        const name = meta.getAttribute('name');
        metas.set(name, [...(metas.get(name) || []), meta]);
      },
    },
    createElement(name) {
      assert.equal(name, 'meta');
      return new FakeMeta();
    },
    querySelectorAll(selector) {
      const match = /^meta\[name="([a-z-]+)"\]$/.exec(selector);
      return match ? [...(metas.get(match[1]) || [])] : [];
    },
    addEventListener(name, listener) { documentListeners.set(name, listener); },
  };
  const context = {
    document,
    localStorage: {
      getItem(key) {
        if (throwOnRead) throw new Error('blocked');
        return stored.get(key) ?? null;
      },
      setItem: (key, value) => stored.set(key, String(value)),
    },
    addEventListener(name, listener) { globalListeners.set(name, listener); },
    dispatchEvent(event) { this.dispatched.push(event); },
    dispatched: [],
    CustomEvent: class CustomEvent { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    location: {protocol: 'https:'},
  };
  context.globalThis = context;
  vm.runInNewContext(source, context);
  return {context, document, documentListeners, globalListeners, metas, stored};
}

test('saved dark page preference keeps the native theme-color hint light', () => {
  const {context, document, metas} = fixture('dark');
  assert.equal(context.__davidPiTheme.value, 'dark');
  assert.equal(document.documentElement.dataset.theme, 'dark');
  assert.equal(document.documentElement.style.colorScheme, 'dark');
  assert.deepEqual(document.head.children.map(meta => meta.getAttribute('name')), ['theme-color', 'color-scheme']);
  assert.equal(metas.get('theme-color')[0].getAttribute('content'), '#fffaf2');
  assert.equal(metas.get('theme-color')[0].getAttribute('media'), null);
  assert.equal(metas.get('color-scheme')[0].getAttribute('content'), 'dark');
});

test('invalid or unavailable storage keeps the safe light default', () => {
  assert.equal(fixture('unknown').document.documentElement.dataset.theme, 'light');
  assert.equal(fixture(null, parserAuthoredMetadata(), {throwOnRead: true}).document.documentElement.dataset.theme, 'light');
});

test('bootstrap never creates late theme metadata when a stale page omits it', () => {
  const {context, document, metas} = fixture('dark', {});
  assert.equal(context.__davidPiTheme.value, 'dark');
  assert.deepEqual(document.head.children, []);
  assert.equal(metas.has('theme-color'), false);
  assert.equal(metas.has('color-scheme'), false);
});

test('preference changes persist document theme without changing the Android bar', () => {
  const theme = new FakeMeta(), scheme = new FakeMeta(), apple = new FakeMeta();
  const {context, document, metas, stored} = fixture('light', {
    'theme-color': [theme],
    'color-scheme': [scheme],
    'apple-mobile-web-app-status-bar-style': [apple],
  });
  assert.equal(context.__davidPiTheme.apply('dark', {persist: true}), 'dark');
  assert.equal(stored.get('davidPiThemeV1'), 'dark');
  assert.equal(theme.getAttribute('content'), '#fffaf2');
  assert.equal(theme.getAttribute('media'), null);
  assert.equal(scheme.getAttribute('content'), 'dark');
  assert.equal(apple.getAttribute('content'), 'black-translucent');
  assert.match(document.cookie, /^davidPiThemeV1=dark;/);
  assert.equal(context.dispatched.at(-1).detail.theme, 'dark');
});

test('cookie is a fallback while a valid local preference stays authoritative', () => {
  const cookieFixture = fixture(null);
  cookieFixture.document.cookie = 'davidPiThemeV1=dark';
  cookieFixture.context.__davidPiTheme.refresh({announce: false});
  assert.equal(cookieFixture.context.__davidPiTheme.value, 'dark');

  const localFixture = fixture('light');
  localFixture.document.cookie = 'davidPiThemeV1=dark';
  localFixture.context.__davidPiTheme.refresh({announce: false});
  assert.equal(localFixture.context.__davidPiTheme.value, 'light');
});

test('cross-tab, page restore, and visible resume reapply the saved preference', () => {
  const {context, document, documentListeners, globalListeners, metas, stored} = fixture('light');
  globalListeners.get('storage')({key: 'davidPiThemeV1', newValue: 'dark'});
  assert.equal(metas.get('theme-color')[0].getAttribute('content'), '#fffaf2');

  stored.set('davidPiThemeV1', 'light');
  globalListeners.get('pageshow')();
  assert.equal(metas.get('color-scheme')[0].getAttribute('content'), 'light');

  stored.set('davidPiThemeV1', 'dark');
  document.visibilityState = 'visible';
  documentListeners.get('visibilitychange')();
  assert.equal(metas.get('theme-color')[0].getAttribute('content'), '#fffaf2');

  globalListeners.get('storage')({key: 'davidPiThemeV1', newValue: null});
  assert.equal(document.documentElement.dataset.theme, 'light');
});
