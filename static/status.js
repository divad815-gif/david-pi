const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat();
const labels = {
  portal: 'Portal', external_drive: 'Data Drive', storage: 'Storage',
  backups: 'Backups', temperature_power: 'Temperature & Power',
  tailscale: 'Tailscale', pihole: 'Pi-hole', background_jobs: 'Background Jobs',
  services: 'Services', updates: 'Updates',
};
const stateLabels = {healthy:'Healthy', warning:'Needs attention', critical:'Action needed', unavailable:'Unavailable'};

function prettyKey(key) {
  return key.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function prettyValue(value, key = '') {
  if (value === null || value === undefined || value === '') return 'Unavailable';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (Array.isArray(value)) return value.map((item) => prettyValue(item)).join(' · ');
  if (typeof value === 'number') {
    if (key.endsWith('_bytes')) return `${(value / 1048576).toFixed(value > 104857600 ? 0 : 1)} MB`;
    return number.format(value);
  }
  if (typeof value === 'string' && /^\d{4}-\d\d-\d\dT/.test(value)) {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? value : date.toLocaleString([], {dateStyle:'medium', timeStyle:'short'});
  }
  return String(value);
}

function appendDetails(container, details, prefix = '', depth = 0) {
  if (!details || typeof details !== 'object' || depth > 2) return;
  Object.entries(details).forEach(([key, value]) => {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const heading = document.createElement('h4');
      heading.textContent = prettyKey(key);
      container.append(heading);
      appendDetails(container, value, `${prefix}${key}.`, depth + 1);
      return;
    }
    const row = document.createElement('div');
    const term = document.createElement('dt');
    const description = document.createElement('dd');
    term.textContent = prettyKey(key);
    description.textContent = prettyValue(value, key);
    row.append(term, description);
    container.append(row);
  });
}

function createHealthCard(key, card) {
  const article = document.createElement('article');
  article.className = 'health-card';
  article.dataset.state = card.state || 'unavailable';
  const header = document.createElement('div');
  header.className = 'health-card-heading';
  const title = document.createElement('h2');
  title.textContent = labels[key] || prettyKey(key);
  const badge = document.createElement('span');
  badge.className = 'health-state';
  badge.textContent = stateLabels[card.state] || stateLabels.unavailable;
  header.append(title, badge);
  const summary = document.createElement('p');
  summary.textContent = card.summary || 'No summary is available.';
  const disclosure = document.createElement('details');
  const disclosureTitle = document.createElement('summary');
  disclosureTitle.textContent = 'Show details';
  const list = document.createElement('dl');
  list.className = 'health-detail-list';
  appendDetails(list, card.details || {});
  const evidence = document.createElement('p');
  evidence.className = 'health-evidence';
  evidence.textContent = `Check: ${card.evidence_code || 'UNAVAILABLE'}`;
  disclosure.append(disclosureTitle, list, evidence);
  if (card.recommended_action) {
    const action = document.createElement('p');
    action.className = 'health-action';
    action.textContent = card.recommended_action;
    disclosure.append(action);
  }
  article.append(header, summary, disclosure);
  return article;
}

function renderDatabases(databases) {
  const target = $('databaseList');
  target.replaceChildren();
  let healthy = 0;
  databases.forEach((database) => {
    const row = document.createElement('article');
    row.dataset.state = database.present && database.last_integrity_check === 'passed' ? 'healthy' : 'warning';
    const heading = document.createElement('strong');
    heading.textContent = database.name;
    const meta = document.createElement('span');
    meta.textContent = database.present
      ? `${prettyValue(database.size_bytes, 'size_bytes')} · backup ${prettyValue(database.last_backup)} · check ${database.last_integrity_check}`
      : 'Missing';
    row.append(heading, meta);
    target.append(row);
    if (row.dataset.state === 'healthy') healthy += 1;
  });
  $('databaseState').textContent = `${healthy}/${databases.length} healthy`;
}

function healthLabel(state) {
  return state === 'good' ? 'Healthy' : state === 'warning' ? 'Watch' : state === 'critical' ? 'Action needed' : 'Unavailable';
}

function renderClassicMetric(name, value, detail, percent, state) {
  $(`${name}Metric`).dataset.health = state;
  $(`${name}Value`).textContent = value;
  $(`${name}Detail`).textContent = detail;
  $(`${name}Health`).textContent = healthLabel(state);
  $(`${name}Meter`).style.width = `${Math.max(0, Math.min(100, Number(percent) || 0))}%`;
}

async function loadClassicOverview() {
  try {
    const response = await fetch('/api/status', {cache:'no-store'});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error('unavailable');
    const states = data.health?.metrics || {};
    renderClassicMetric('cpu', `${data.cpu}%`, `Load ${data.load1} · ${data.load5} · ${data.load15}`, data.cpu, states.cpu || 'unknown');
    renderClassicMetric('memory', `${data.memory}%`, `${data.memory_used_gb} of ${data.memory_total_gb} GB used`, data.memory, states.memory || 'unknown');
    renderClassicMetric('storage', `${data.disk_used}%`, `${data.disk_free_gb} GB free of ${data.disk_total_gb} GB`, data.disk_used, states.disk || 'unknown');
    const uptimeDays = Math.floor((data.uptime || 0) / 86400);
    const uptimeHours = Math.floor(((data.uptime || 0) % 86400) / 3600);
    renderClassicMetric('temperature', data.temperature == null ? '—' : `${data.temperature}°C`, `Up for ${uptimeDays}d ${uptimeHours}h`, data.temperature == null ? 0 : data.temperature / 80 * 100, states.temperature || 'unknown');
    $('classicUpdated').textContent = `Updated ${new Date().toLocaleTimeString([], {hour:'numeric', minute:'2-digit'})}`;
  } catch (_) {
    ['cpu','memory','storage','temperature'].forEach((name) => {
      $(`${name}Metric`).dataset.health = 'unknown';
      $(`${name}Health`).textContent = 'Unavailable';
    });
    $('classicUpdated').textContent = 'Live readings are temporarily unavailable.';
  }
}

async function loadSummary() {
  try {
    const response = await fetch('/api/status/summary', {cache:'no-store'});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error('unavailable');
    const cards = $('healthCards');
    cards.replaceChildren(...Object.entries(data.subsystems).map(([key, card]) => createHealthCard(key, card)));
    $('healthHero').dataset.state = data.state;
    $('overallPill').dataset.state = data.state;
    $('overallPill').querySelector('span').textContent = stateLabels[data.state] || stateLabels.unavailable;
    $('overallTitle').textContent = data.state === 'healthy' ? 'Everything looks good.'
      : data.state === 'critical' ? 'David-Pi needs attention.'
      : data.stale ? 'The latest snapshot is stale.'
      : 'A few things need watching.';
    const generated = new Date(data.generated_at);
    $('lastUpdated').textContent = `Updated ${generated.toLocaleString([], {dateStyle:'medium', timeStyle:'short'})}${data.stale ? ' · collector is late' : ''}`;
    renderDatabases(data.databases || []);
  } catch (_) {
    $('healthHero').dataset.state = 'unavailable';
    $('overallPill').dataset.state = 'unavailable';
    $('overallPill').querySelector('span').textContent = 'Unavailable';
    $('overallTitle').textContent = 'Health details are unavailable.';
    $('lastUpdated').textContent = 'The portal is open, but its private host snapshot could not be read.';
  }
}

function drawHistory(points, metric) {
  const svg = $('historyChart');
  svg.replaceChildren();
  if (points.length < 2) {
    $('historyEmpty').hidden = false;
    return;
  }
  $('historyEmpty').hidden = true;
  const values = points.map((point) => Number(point.value));
  let minimum = Math.min(...values);
  let maximum = Math.max(...values);
  if (['memory','swap','hdd','microsd','container_memory'].includes(metric)) { minimum = 0; maximum = 100; }
  if (metric === 'temperature') { minimum = Math.min(30, minimum); maximum = Math.max(80, maximum); }
  if (minimum === maximum) { minimum -= 1; maximum += 1; }
  const first = points[0].timestamp;
  const last = points.at(-1).timestamp;
  const pathData = points.map((point, index) => {
    const x = 34 + ((point.timestamp - first) / Math.max(last - first, 1)) * 832;
    const y = 225 - ((Number(point.value) - minimum) / (maximum - minimum)) * 190;
    return `${index ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  svg.innerHTML = `<g class="grid-lines"><line x1="34" y1="35" x2="866" y2="35"/><line x1="34" y1="130" x2="866" y2="130"/><line x1="34" y1="225" x2="866" y2="225"/></g><g class="chart-labels"><text x="34" y="27">${maximum.toFixed(1)}</text><text x="34" y="252">${minimum.toFixed(1)}</text></g><path class="status-history-line" d="${pathData}"/>`;
}

async function loadHistory() {
  try {
    const metric = $('historyMetric').value;
    const range = $('historyRange').value;
    const response = await fetch(`/api/status/history?metric=${encodeURIComponent(metric)}&range=${encodeURIComponent(range)}`, {cache:'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error('history');
    drawHistory(data.points || [], metric);
  } catch (_) {
    $('historyEmpty').hidden = false;
    $('historyEmpty').textContent = 'History is temporarily unavailable.';
  }
}

$('historyMetric').addEventListener('change', loadHistory);
$('historyRange').addEventListener('change', loadHistory);

const shutdownDialog = $('safeShutdownDialog');
const shutdownForm = $('safeShutdownForm');
const shutdownPassword = $('shutdownPassword');
const shutdownError = $('shutdownError');
const shutdownConfirm = $('safeShutdownConfirm');

function closeShutdownDialog() {
  shutdownForm.reset();
  shutdownError.textContent = '';
  shutdownDialog.close();
}

$('safeShutdownButton').addEventListener('click', () => {
  shutdownError.textContent = '';
  shutdownDialog.showModal();
  window.setTimeout(() => shutdownPassword.focus(), 50);
});
$('safeShutdownCancel').addEventListener('click', closeShutdownDialog);
$('safeShutdownClose').addEventListener('click', closeShutdownDialog);
shutdownDialog.addEventListener('cancel', (event) => {
  event.preventDefault();
  closeShutdownDialog();
});
shutdownForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  shutdownError.textContent = '';
  shutdownConfirm.disabled = true;
  shutdownConfirm.textContent = 'Starting shutdown…';
  try {
    const response = await fetch('/api/system/shutdown', {
      method: 'POST',
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
      },
      body: JSON.stringify({password: shutdownPassword.value}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'David-Pi could not start a safe shutdown.');
    closeShutdownDialog();
    $('safeShutdownPanel').dataset.accepted = 'true';
    $('safeShutdownSummary').textContent = 'David-Pi is shutting down. Wait for its activity light to stop before removing power.';
    $('safeShutdownButton').hidden = true;
  } catch (error) {
    shutdownError.textContent = error.message;
  } finally {
    shutdownConfirm.disabled = false;
    shutdownConfirm.textContent = 'Shut down safely';
  }
});

loadSummary();
loadClassicOverview();
loadHistory();
setInterval(loadSummary, 180000);
setInterval(loadClassicOverview, 180000);
setInterval(loadHistory, 300000);
