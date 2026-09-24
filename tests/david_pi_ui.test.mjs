import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../static/david-pi-ui.js', import.meta.url), 'utf8');

class FakeElement {
  constructor(ownerDocument = null) {
    this.ownerDocument = ownerDocument;
    this.attributes = new Map();
    this.dataset = {};
    this.hidden = false;
    this.inert = false;
    this.listeners = new Map();
    this.children = [];
    this.focused = false;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  hasAttribute(name) { return this.attributes.has(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  removeEventListener(name) { this.listeners.delete(name); }
  querySelectorAll() { return this.children; }
  querySelector() { return null; }
  contains(element) { return element === this || this.children.includes(element); }
  closest() { return null; }
  focus() { this.focused = true; if (this.ownerDocument) this.ownerDocument.activeElement = this; }
}

function fixture() {
  const document = {activeElement: null};
  const context = {document, setTimeout: callback => callback(), URL, location: {href: 'https://david-pi.test/audiobooks', origin: 'https://david-pi.test', pathname: '/audiobooks'}};
  context.globalThis = context;
  vm.runInNewContext(source, context);
  return {context, document};
}

test('publishes the framework-neutral primitives and stable route registry', () => {
  const {context} = fixture();
  for (const name of ['DavidPiModal', 'AsyncPanel', 'DavidPiFilterGroup', 'DavidPiForm', 'DavidPiAnnouncer', 'DavidPiAppSwitcher', 'DavidPiPersistentAudio']) {
    assert.equal(typeof context[name], 'object', name);
  }
  assert.deepEqual(
    [...context.DavidPiAppSwitcher.routes].map(route => route.path),
    ['/', '/photos', '/mytube', '/audiobooks', '/files', '/notes', '/recipes', '/chat', '/movies', '/places', '/games', '/assistant', '/status', '/device-backup'],
  );
  assert.equal(Object.isFrozen(context.DavidPiAppSwitcher.routes), true);
  assert.equal(typeof context.DavidPiAppSwitcher.mount, 'function');
});

test('theme control delegates persistence and system metadata to the early bootstrap', () => {
  const {context} = fixture();
  const calls = [];
  context.__davidPiTheme = {
    apply(theme, options) { calls.push([theme, options]); return theme; },
  };
  assert.equal(context.DavidPiTheme.apply('dark', {persist: true}), 'dark');
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'dark');
  assert.equal(calls[0][1].persist, true);
});

class FakeChannel {
  static peers = new Map();
  constructor(name) {
    this.name = name; this.listener = null;
    const peers = FakeChannel.peers.get(name) || [];
    peers.push(this); FakeChannel.peers.set(name, peers);
  }
  addEventListener(name, listener) { if (name === 'message') this.listener = listener; }
  postMessage(message) {
    for (const peer of FakeChannel.peers.get(this.name) || []) {
      if (peer !== this) peer.listener?.({data: message});
    }
  }
}

function persistentFixture({now = () => 1000} = {}) {
  FakeChannel.peers.clear();
  const {context} = fixture();
  const timers = {
    intervals: new Map(), timeouts: [], next: 1,
    setInterval(callback) { const id=this.next++;this.intervals.set(id,callback);return id; },
    clearInterval(id) { this.intervals.delete(id); },
    setTimeout(callback) { this.timeouts.push(callback);return this.timeouts.length; },
    clearTimeout() {},
  };
  const create = () => context.DavidPiPersistentAudio.create({
    scope: 'a'.repeat(32), document: null, BroadcastChannel: FakeChannel, timers, now,
  });
  return {context, timers, create};
}

test('new claims displace, stop, and retarget controls away from the old audiobook', () => {
  let instant = 1000;
  const {create} = persistentFixture({now: () => instant});
  const firstCommands = [], secondCommands = [], takeovers = [];
  const firstHost = create();
  const first = firstHost.claim({
    getState: () => ({title:'First', playing:true}),
    onCommand: command => firstCommands.push(command),
    onTakeover: winner => takeovers.push(`first:${winner.session}`),
  });
  const remote = create();
  instant = 1001;
  const secondHost = create();
  const second = secondHost.claim({
    getState: () => ({title:'Second', playing:true}),
    onCommand: command => secondCommands.push(command),
    onTakeover: winner => takeovers.push(`second:${winner.session}`),
  });
  assert.ok(first); assert.ok(second);
  assert.deepEqual(takeovers, [`first:${second.session}`]);
  assert.equal(first.release(), false);
  assert.equal(remote.invoke('pause'), true);
  assert.deepEqual(firstCommands, []);
  assert.deepEqual(secondCommands, ['pause']);
  remote.receive({version:1,type:'ended',session:first.session});
  assert.equal(remote.invoke('play'), true);
  assert.deepEqual(secondCommands, ['pause', 'play']);
});

test('simultaneous claims converge on one deterministic session', () => {
  const {create} = persistentFixture({now: () => 5000});
  const commands = {first:[], second:[]}, takeovers = [];
  const firstHost=create(), secondHost=create();
  const first=firstHost.claim({getState:()=>({title:'First'}),onCommand:value=>commands.first.push(value),onTakeover:()=>takeovers.push('first')});
  const second=secondHost.claim({getState:()=>({title:'Second'}),onCommand:value=>commands.second.push(value),onTakeover:()=>takeovers.push('second')});
  const winner = first.session > (second?.session || '') ? 'first' : 'second';
  const loser = winner === 'first' ? 'second' : 'first';
  assert.deepEqual(takeovers, [loser]);
  const remote=create();
  assert.equal(remote.invoke('play'), true);
  assert.deepEqual(commands[winner], ['play']);
  assert.deepEqual(commands[loser], []);
});

test('audio dock ticking status is not a screen-reader live region', () => {
  const dockSource = source.slice(source.indexOf('function ensureDock()'), source.indexOf('function render(', source.indexOf('function ensureDock()')));
  assert.doesNotMatch(dockSource, /status\.setAttribute\(['"]aria-live/);
});

test('persistent audio rejects foreign commands, releases hosts, and protects replacement sessions', () => {
  const {create} = persistentFixture();
  const commands = [];
  const host = create();
  const first = host.claim({getState: () => ({title:'Book', playing:true, position:12}), onCommand: command => commands.push(command)});
  assert.equal(host.receive({version:1,type:'command',session:'foreign',command:'pause'}), false);
  assert.deepEqual(commands, []);
  const remote = create();
  assert.equal(remote.invoke('pause'), true);
  assert.deepEqual(commands, ['pause']);
  assert.equal(remote.invoke('play'), true);
  assert.equal(remote.invoke('pause'), true);
  assert.deepEqual(commands.slice(-2), ['play', 'pause']);
  const second = host.claim({getState: () => ({title:'Other', playing:false}), onCommand: command => commands.push(`second:${command}`)});
  assert.equal(first.release(), false);
  assert.equal(remote.invoke('pause'), true);
  assert.equal(remote.invoke('play'), true);
  assert.deepEqual(commands.slice(-2), ['second:pause', 'second:play']);
  assert.equal(host.receive({version:1,type:'command',session:second.session,command:'expand'}), true);
  assert.equal(commands.at(-1), 'second:expand');
  assert.equal(second.release(), true);
  assert.equal(host.receive({version:1,type:'command',session:second.session,command:'play'}), false);
});

test('persistent audio stale state expires and internal route gating fails closed', () => {
  const {context, timers, create} = persistentFixture();
  const remote = create();
  remote.receive({version:1,type:'state',state:{session:'session-12345678',title:'Book',playing:true,position:3}});
  assert.equal(remote.invoke('pause'), true);
  timers.timeouts.at(-1)();
  assert.equal(remote.invoke('pause'), false);

  const host = create();
  host.claim({getState: () => ({title:'Book'}), onCommand() {}});
  assert.equal(host.routeIsInternal('/files'), true);
  assert.equal(host.routeIsInternal('/photos/collections'), true);
  assert.equal(host.routeIsInternal('/files?download=1'), false);
  assert.equal(host.routeIsInternal('/logout'), false);
  assert.equal(host.routeIsInternal('https://example.com/files'), false);
  const opened = [];
  context.open = (url, name) => { opened.push([url,name]); return {focus() {}}; };
  assert.equal(host.openRoute('/recipes'), true);
  assert.deepEqual(opened, [['https://david-pi.test/recipes','david-pi-browse']]);
  context.open = () => null;
  assert.equal(host.openRoute('/games'), false);
});

test('AsyncPanel rejects stale completions and exposes busy/error states', () => {
  const {context} = fixture();
  const root = new FakeElement();
  const loading = new FakeElement();
  const content = new FakeElement();
  const empty = new FakeElement();
  const noResults = new FakeElement();
  const error = new FakeElement();
  const announced = [];
  const panel = context.AsyncPanel.create({
    root, loading, content, empty, noResults, error,
    announcer: {polite: message => announced.push(['polite', message]), alert: message => announced.push(['alert', message])},
  });
  panel.begin('new');
  assert.equal(root.getAttribute('aria-busy'), 'true');
  assert.equal(loading.hidden, false);
  assert.equal(panel.success('old'), false);
  assert.equal(panel.state, 'loading');
  assert.equal(panel.success('new', {empty: true, message: 'Nothing here.'}), true);
  assert.equal(panel.state, 'empty');
  assert.equal(root.getAttribute('aria-busy'), 'false');
  panel.begin('failed');
  assert.equal(panel.fail('failed', new Error('Try again.')), true);
  assert.equal(error.textContent, 'Try again.');
  assert.deepEqual(announced.at(-1), ['alert', 'Try again.']);
});

test('DavidPiModal copies names, isolates siblings, and restores prior state', () => {
  const {context, document} = fixture();
  const sourceDialog = new FakeElement(document);
  sourceDialog.setAttribute('aria-labelledby', 'dialog-title');
  sourceDialog.setAttribute('aria-describedby', 'dialog-copy');
  const host = new FakeElement(document);
  const background = new FakeElement(document);
  const backdrop = new FakeElement(document);
  const returnFocus = new FakeElement(document);
  const firstControl = new FakeElement(document);
  host.children = [firstControl];
  document.activeElement = returnFocus;
  document.body = {children: [background, backdrop, host]};

  context.DavidPiModal.copySemantics(sourceDialog, host);
  assert.equal(host.getAttribute('aria-labelledby'), 'dialog-title');
  assert.equal(host.getAttribute('aria-describedby'), 'dialog-copy');
  assert.equal(host.getAttribute('aria-modal'), 'true');

  context.DavidPiModal.activate(host, {returnFocus, exclusions: [backdrop]});
  assert.equal(background.inert, true);
  assert.equal(background.getAttribute('aria-hidden'), 'true');
  assert.equal(backdrop.inert, false);
  assert.equal(context.DavidPiModal.deactivate(host), true);
  assert.equal(background.inert, false);
  assert.equal(background.hasAttribute('aria-hidden'), false);
  assert.equal(returnFocus.focused, true);
});

test('DavidPiModal cycles focus at both edges', () => {
  const {context, document} = fixture();
  const root = new FakeElement(document);
  const first = new FakeElement(document);
  const last = new FakeElement(document);
  root.children = [first, last];
  document.activeElement = last;
  let prevented = false;
  assert.equal(context.DavidPiModal.trapFocus(root, {key: 'Tab', shiftKey: false, preventDefault: () => { prevented = true; }}), true);
  assert.equal(prevented, true);
  assert.equal(first.focused, true);

  document.activeElement = first;
  prevented = false;
  assert.equal(context.DavidPiModal.trapFocus(root, {key: 'Tab', shiftKey: true, preventDefault: () => { prevented = true; }}), true);
  assert.equal(prevented, true);
  assert.equal(last.focused, true);
});
