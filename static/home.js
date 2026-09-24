const health = document.querySelector('#homeHealth');
const RECENT_STORAGE_PREFIX = 'david-pi-recent-modules-v2:';
const LEGACY_RECENT_KEY = 'david-pi-recent-modules-v1';
const MAX_RECENTS = 3;
const recentScope = document.body.dataset.recentScope || '';
const RECENT_KEY = /^[0-9a-f]{32}$/.test(recentScope) ? `${RECENT_STORAGE_PREFIX}${recentScope}` : null;
const routeByPath = new Map(window.DavidPiAppSwitcher.routes.filter((route) => route.path !== '/').map((route) => [route.path, route]));
document.body.removeAttribute('data-recent-scope');

function discardUnscopedHistory() {
  try {
    // Version 1 was shared by every person using this browser. Never migrate it.
    localStorage.removeItem(LEGACY_RECENT_KEY);
  } catch (_error) {
    // Navigation and the directory remain usable when storage is unavailable.
  }
}

function recentPaths() {
  if (!RECENT_KEY) return [];
  try {
    const value = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
    if (!Array.isArray(value)) return [];
    const paths = [];
    value.forEach((path) => {
      if (routeByPath.has(path) && !paths.includes(path) && paths.length < MAX_RECENTS) paths.push(path);
    });
    return paths;
  } catch (_error) {
    return [];
  }
}

function writeRecent(paths) {
  if (!RECENT_KEY) return;
  const safePaths = paths.filter((path, index) => routeByPath.has(path) && paths.indexOf(path) === index).slice(0, MAX_RECENTS);
  try {
    if (safePaths.length) localStorage.setItem(RECENT_KEY, JSON.stringify(safePaths));
    else localStorage.removeItem(RECENT_KEY);
  } catch (_error) {
    // Recent shortcuts are optional browser-local convenience state.
  }
}

function remember(path) {
  if (!routeByPath.has(path)) return;
  writeRecent([path, ...recentPaths().filter((item) => item !== path)]);
}

function focusAppsHeading() {
  document.querySelector('#moduleDirectoryTitle')?.focus();
}

function renderRecent(focusIndex = null) {
  const paths = recentPaths();
  const section = document.querySelector('#homeRecent');
  const target = document.querySelector('#homeRecentGrid');
  target.replaceChildren();
  if (!paths.length) {
    section.hidden = true;
    return;
  }
  paths.forEach((path) => {
    const route = routeByPath.get(path);
    const item = document.createElement('li');
    item.className = 'home-recent-card';
    const link = document.createElement('a');
    link.className = 'home-recent-link'; link.href = path; link.dataset.module = '';
    const copy = document.createElement('span'); copy.className = 'home-recent-copy';
    const label = document.createElement('strong'); label.textContent = route.label;
    const hint = document.createElement('small'); hint.textContent = 'Open again';
    const arrow = document.createElement('span'); arrow.className = 'arrow'; arrow.setAttribute('aria-hidden', 'true'); arrow.textContent = '→';
    copy.append(label, hint); link.append(copy, arrow);
    const dismiss = document.createElement('button');
    dismiss.type = 'button'; dismiss.className = 'home-recent-dismiss'; dismiss.dataset.dismissRecent = path;
    dismiss.setAttribute('aria-label', `Remove ${route.label} from Recent apps`);
    dismiss.title = `Remove ${route.label}`; dismiss.textContent = '×';
    item.append(link, dismiss); target.append(item);
  });
  section.hidden = false;
  if (Number.isInteger(focusIndex)) {
    const buttons = target.querySelectorAll('.home-recent-dismiss');
    buttons[Math.min(focusIndex, buttons.length - 1)]?.focus();
  }
}

function dismissRecent(path) {
  const paths = recentPaths();
  const index = paths.indexOf(path);
  if (index < 0) return;
  const route = routeByPath.get(path);
  const remaining = paths.filter((item) => item !== path);
  writeRecent(remaining);
  renderRecent(remaining.length ? index : null);
  document.querySelector('#homeRecentStatus').textContent = `${route.label} removed from Recent apps.`;
  if (!remaining.length) focusAppsHeading();
}

document.addEventListener('click', (event) => {
  const dismiss = event.target.closest?.('button[data-dismiss-recent]');
  if (dismiss) {
    dismissRecent(dismiss.dataset.dismissRecent);
    return;
  }
  const link = event.target.closest?.('a[data-module]');
  if (link) remember(new URL(link.href, location.href).pathname);
});

document.querySelector('#homeClearRecent').addEventListener('click', () => {
  writeRecent([]);
  renderRecent();
  document.querySelector('#homeRecentStatus').textContent = 'Recent apps cleared.';
  focusAppsHeading();
});

discardUnscopedHistory();
renderRecent();

fetch('/api/status/summary', { cache: 'no-store' })
  .then((response) => {
    if (!response.ok) throw new Error('Health unavailable');
    return response.json();
  })
  .then((data) => {
    if (!data.ok || !data.state) throw new Error('Health unavailable');
    const state = data.stale ? 'unavailable' : data.state;
    document.querySelector('#homeAttention').hidden = state === 'healthy';
    health.dataset.health = {healthy:'good', warning:'warning', critical:'critical'}[state] || 'unknown';
    health.lastChild.textContent = ` ${{healthy:'Everything looks good', warning:'Server needs attention', critical:`${window.davidPiServerName || 'Server'} needs attention`}[state] || 'Health check unavailable'}`;
  })
  .catch(() => {
    health.dataset.health = 'unknown';
    health.lastChild.textContent = ' Health check unavailable';
    document.querySelector('#homeAttention').hidden = false;
  });
