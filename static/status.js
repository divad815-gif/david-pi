const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat();
const labels = {
  portal: 'Portal', external_drive: 'External Drive', storage: 'Storage',
  backups: 'Backups', temperature_power: 'Temperature & Power',
  tailscale: 'Tailscale', pihole: 'Pi-hole', background_jobs: 'Background Jobs',
  services: 'Services', updates: 'Updates', access_control: 'Access Control',
};
const stateLabels = {healthy:'Healthy', warning:'Needs attention', critical:'Action needed', unavailable:'Unavailable', disabled:'Disabled', not_configured:'Not configured'};
const historyMetrics = {
  temperature: {label:'Temperature', unit:'°C', digits:1},
  load: {label:'One-minute load', unit:'', digits:2},
  memory: {label:'RAM use', unit:'%', digits:1},
  swap: {label:'Swap use', unit:'%', digits:1},
  hdd: {label:'External drive use', unit:'%', digits:1},
  microsd: {label:'MicroSD use', unit:'%', digits:1},
  container_memory: {label:'Portal memory', unit:'%', digits:1},
  health_latency: {label:'Health response', unit:' ms', digits:0},
  queue: {label:'Queue depth', unit:' jobs', digits:0},
  backup: {label:'Backup state', unit:'', digits:0},
};
let historyGeneration = 0;
let historyController = null;

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
  $('databaseCount').textContent = `${databases.length} ${databases.length === 1 ? 'database' : 'databases'}`;
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

function percentValue(value) {
  return value === null || value === undefined ? '—' : `${value}%`;
}

function capacityDetail(used, total, unit, unavailable) {
  if (used === null || used === undefined || total === null || total === undefined) return unavailable;
  return `${used} of ${total} ${unit}`;
}

async function loadClassicOverview() {
  try {
    const response = await fetch('/api/status', {cache:'no-store'});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error('unavailable');
    const states = data.health?.metrics || {};
    const loadAvailable = [data.load1, data.load5, data.load15].every(value => value !== null && value !== undefined);
    renderClassicMetric('cpu', percentValue(data.cpu), loadAvailable ? `Load ${data.load1} · ${data.load5} · ${data.load15}` : 'CPU and load readings unavailable', data.cpu, states.cpu || 'unknown');
    renderClassicMetric('memory', percentValue(data.memory), capacityDetail(data.memory_used_gb, data.memory_total_gb, 'GB used', 'RAM reading unavailable'), data.memory, states.memory || 'unknown');
    const storageDetail = data.disk_free_gb == null || data.disk_total_gb == null ? 'Storage reading unavailable' : `${data.disk_free_gb} GB free of ${data.disk_total_gb} GB`;
    renderClassicMetric('storage', percentValue(data.disk_used), storageDetail, data.disk_used, states.disk || 'unknown');
    const hostUptime = data.host_uptime ?? data.uptime;
    const uptimeDetail = hostUptime == null ? 'Host uptime unavailable' : `Host up ${Math.floor(hostUptime / 86400)}d ${Math.floor((hostUptime % 86400) / 3600)}h`;
    renderClassicMetric('temperature', data.temperature == null ? '—' : `${data.temperature}°C`, uptimeDetail, data.temperature == null ? 0 : data.temperature / 80 * 100, states.temperature || 'unknown');
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
    const attention = Object.entries(data.subsystems).filter(([, card]) => !['healthy','disabled','not_configured'].includes(card.state));
    $('attentionPanel').hidden = !attention.length && !data.stale;
    $('attentionItems').replaceChildren(...attention.map(([key, card]) => {
      const item = document.createElement('li');
      item.textContent = `${labels[key] || key}: ${card.summary}`;
      return item;
    }));
    const recovery = data.subsystems.backups?.details?.independent_data_backup || {};
    $('recoverySummary').textContent = `Local backup: ${prettyValue(recovery.last_success)}. Restore test: ${prettyValue(recovery.last_restoration_test)}. Free backup space: ${recovery.capacity?.free_gb ?? 'Unavailable'} GB. ${data.subsystems.backups?.details?.offsite_backup?.local_only_risk || ''}`;
    $('healthHero').dataset.state = data.state;
    $('overallPill').dataset.state = data.state;
    $('overallPill').querySelector('span').textContent = stateLabels[data.state] || stateLabels.unavailable;
    $('overallTitle').textContent = data.state === 'healthy' ? 'Everything looks good.'
      : data.state === 'critical' ? `${window.davidPiServerName || 'Server'} needs attention.`
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

function historyValue(value, metric) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 'Unavailable';
  if (metric === 'backup') return ['Healthy', 'Warning', 'Critical', 'Unavailable'][Math.round(numeric)] || 'Unavailable';
  const info = historyMetrics[metric] || {unit:'', digits:1};
  return `${numeric.toFixed(info.digits)}${info.unit}`;
}

function normalizedHistory(points) {
  if (!Array.isArray(points)) return [];
  return points.map((point) => ({timestamp:Number(point.timestamp), value:Number(point.value)}))
    .filter((point) => Number.isFinite(point.timestamp) && Number.isFinite(point.value))
    .sort((left, right) => left.timestamp - right.timestamp);
}

function boundedHistoryRows(points, maximum = 48) {
  if (points.length <= maximum) return points;
  const selected = [];
  for (let index = 0; index < maximum; index += 1) {
    selected.push(points[Math.round(index * (points.length - 1) / (maximum - 1))]);
  }
  return selected;
}

function renderHistoryDetails(points, metric, sampled = false) {
  const info = historyMetrics[metric] || {label:prettyKey(metric)};
  const rows = boundedHistoryRows(points);
  $('historyRows').replaceChildren(...rows.map((point) => {
    const row = document.createElement('tr');
    const time = document.createElement('td');
    const value = document.createElement('td');
    time.textContent = new Date(point.timestamp * 1000).toLocaleString([], {dateStyle:'medium', timeStyle:'short'});
    value.textContent = historyValue(point.value, metric);
    row.append(time, value);
    return row;
  }));
  $('historyCaption').textContent = `${info.label} readings${sampled ? ' (server-sampled)' : ''}`;
  $('historyValuesSummary').textContent = points.length > rows.length
    ? `View ${rows.length} representative values from ${points.length} readings`
    : `View ${points.length} recorded ${points.length === 1 ? 'value' : 'values'}`;
  if (!points.length) {
    $('historySummary').textContent = `No ${info.label.toLowerCase()} readings are available for this range yet.`;
    return;
  }
  const values = points.map((point) => point.value);
  const first = points[0].value;
  const last = points.at(-1).value;
  const delta = last - first;
  let direction = Math.abs(delta) < 0.001 ? 'held steady' : delta > 0 ? 'rose' : 'fell';
  if (metric === 'backup' && Math.abs(delta) >= 0.001) direction = delta > 0 ? 'worsened' : 'improved';
  $('historySummary').textContent = `${info.label} ${direction} from ${historyValue(first, metric)} to ${historyValue(last, metric)}. Range ${historyValue(Math.min(...values), metric)} to ${historyValue(Math.max(...values), metric)} across ${points.length} ${points.length === 1 ? 'reading' : 'readings'}.`;
}

function svgElement(name, attributes = {}, text = '') {
  const element = document.createElementNS('http://www.w3.org/2000/svg', name);
  Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
  if (text) element.textContent = text;
  return element;
}

function drawHistory(rawPoints, metric, sampled = false) {
  const points = normalizedHistory(rawPoints);
  const svg = $('historyChart');
  svg.replaceChildren();
  renderHistoryDetails(points, metric, sampled);
  if (points.length < 2) {
    $('historyEmpty').hidden = false;
    $('historyEmptyText').textContent = points.length ? 'One reading is available; a trend needs at least two.' : 'History will fill in every five minutes.';
    $('historyRetry').hidden = true;
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
  const info = historyMetrics[metric] || {label:prettyKey(metric)};
  const title = svgElement('title', {id:'historyChartTitle'}, `${info.label} history`);
  const description = svgElement('desc', {id:'historyChartDescription'}, $('historySummary').textContent);
  const grid = svgElement('g', {class:'grid-lines'});
  [35, 130, 225].forEach((y) => grid.append(svgElement('line', {x1:34, y1:y, x2:866, y2:y})));
  const chartLabels = svgElement('g', {class:'chart-labels'});
  chartLabels.append(
    svgElement('text', {x:34, y:27}, historyValue(maximum, metric)),
    svgElement('text', {x:34, y:237}, historyValue(minimum, metric)),
    svgElement('text', {x:34, y:255}, new Date(first * 1000).toLocaleDateString([], {month:'short', day:'numeric'})),
    svgElement('text', {x:866, y:255, 'text-anchor':'end'}, new Date(last * 1000).toLocaleDateString([], {month:'short', day:'numeric'})),
  );
  svg.append(title, description, grid, chartLabels, svgElement('path', {class:'status-history-line', d:pathData}));
}

async function loadHistory() {
  const generation = ++historyGeneration;
  historyController?.abort();
  historyController = new AbortController();
  const metric = $('historyMetric').value;
  const range = $('historyRange').value;
  $('historySummary').textContent = `Loading ${(historyMetrics[metric]?.label || prettyKey(metric)).toLowerCase()} history…`;
  $('historyRetry').hidden = true;
  try {
    const response = await fetch(`/api/status/history?metric=${encodeURIComponent(metric)}&range=${encodeURIComponent(range)}`, {cache:'no-store', signal:historyController.signal});
    const data = await response.json();
    if (!response.ok) throw new Error('history');
    if (generation !== historyGeneration) return;
    drawHistory(data.points || [], metric, Boolean(data.sampled));
  } catch (error) {
    if (error.name === 'AbortError' || generation !== historyGeneration) return;
    $('historyEmpty').hidden = false;
    $('historyEmptyText').textContent = 'History is temporarily unavailable.';
    $('historyRetry').hidden = false;
    $('historySummary').textContent = 'History could not be loaded. The rest of the status page is still available.';
    $('historyRows').replaceChildren();
    $('historyValuesSummary').textContent = 'No recorded values available';
  }
}

$('historyMetric').addEventListener('change', loadHistory);
$('historyRange').addEventListener('change', loadHistory);
$('historyRetry').addEventListener('click', loadHistory);

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
    if (!response.ok) throw new Error(data.error || 'The server could not start a safe shutdown.');
    closeShutdownDialog();
    $('safeShutdownPanel').dataset.accepted = 'true';
    $('safeShutdownSummary').textContent = 'The server is shutting down. Wait for its activity light to stop before removing power.';
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
