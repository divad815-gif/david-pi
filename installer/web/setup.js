'use strict';

// These helpers also run in the dependency-free browser behavior tests.
(function () {
  const stages = [
    {phase: 'preparing selected storage', label: 'Prepare your storage'},
    {phase: 'saving local keys and module settings', label: 'Save settings and local keys'},
    {phase: 'starting selected services', label: 'Start your selected features'},
    {phase: 'checking selected services', label: 'Check that your features are ready'},
    {phase: 'opening your home server', label: 'Open your private website'},
  ];
  const validJobId = value => /^[0-9a-f]{32}$/.test(value || '');
  function privateOrigin(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && !url.username && !url.password && !url.search && !url.hash && url.pathname === '/' ? url.origin : null;
    } catch { return null; }
  }
  function readyMatches(ready, metadata, expected, pageOrigin) {
    const origin = privateOrigin(expected.origin);
    return Boolean(ready?.ok === true && expected.instance_id && origin && origin === pageOrigin &&
      metadata?.instance_id === expected.instance_id && privateOrigin(metadata.public_url) === origin);
  }
  function chooseTimezone(zones, browser, server) {
    return [browser, server, 'UTC', zones[0]].find(zone => zones.includes(zone)) || 'UTC';
  }
  function filterTimezones(zones, search, selected) {
    const query = search.trim().toLowerCase().replaceAll('_', ' ');
    const matches = zones.filter(zone => zone.replaceAll('_', ' ').toLowerCase().includes(query));
    return {matches, options: selected && !matches.includes(selected) ? [selected, ...matches] : matches};
  }
  function capacity(bytes) {
    if (!Number.isFinite(bytes) || bytes < 0) return 'capacity unavailable';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let value = bytes, unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    return `${value.toLocaleString('en', {maximumFractionDigits: unit > 1 ? 1 : 0})} ${units[unit]}`;
  }
  function storageLabel(choice) {
    return `${choice.label}${choice.system_disk ? ' · system disk' : ''} · ${capacity(choice.free_bytes)} free of ${capacity(choice.total_bytes)}`;
  }
  function backupChoices(choices, selectedData) {
    const data = choices.find(choice => choice.id === selectedData);
    return choices.filter(choice => !data || choice.device_id !== data.device_id);
  }
  function selectedStorage(choices, dataId, backupId, manual) {
    const selection = {}, data = choices.find(choice => choice.id === dataId);
    if (dataId !== 'manual' && !data) throw new Error('Choose an available storage location, or use the advanced folder option.');
    const parent = (dataId === 'manual' ? manual.parent : data.parent).trim().replace(/\/+$/, '');
    if (!parent && (dataId === 'manual' ? manual.parent : data.parent) !== '/') throw new Error('Enter an existing storage parent folder.');
    const mode = dataId === 'manual' ? manual.mode : data.mode;
    if (data) selection.data = data.id;
    let backup = null;
    if (backupId !== 'skip') {
      const choice = choices.find(item => item.id === backupId);
      if (backupId !== 'manual' && !choice) throw new Error('Choose an available independent backup location, or choose “Skip backups for now”.');
      if (data && choice && data.device_id === choice.device_id) throw new Error('Choose a different drive for independent backups.');
      const raw = backupId === 'manual' ? manual.backup : choice.parent;
      if (!raw.trim()) throw new Error('Enter an existing backup parent folder, or choose “Skip backups for now”.');
      backup = `${raw.trim().replace(/\/+$/, '')}/david-pi-backups`;
      if (choice) selection.backup = choice.id;
    }
    return {storage: {mode, data_root: `${parent}/david-pi-data`, backup_root: backup}, selection};
  }
  function completedStages(phase, previous = 0) {
    return Math.max(previous, stages.findIndex(stage => stage.phase === phase), 0);
  }
  function watchInstallation(options) {
    let timer, stopped = false, failures = 0, reference, version = 0;
    const schedule = options.schedule || setTimeout;
    const cancel = options.cancel || clearTimeout;
    const emit = (kind, extra = {}) => options.onChange({kind, ...reference, failures, ...extra});
    async function ready() {
      try { return await options.verifyReady(); } catch { return false; }
    }
    async function tick(generation = version) {
      if (stopped || generation !== version) return;
      try {
        const job = await options.fetchJob(reference.job_id);
        if (stopped || generation !== version) return;
        failures = 0;
        reference.created_at = job.created_at || reference.created_at;
        reference.completed = completedStages(job.phase, reference.completed);
        options.remember(reference);
        if (job.state === 'complete' || ['failed', 'interrupted'].includes(job.state)) {
          const verified = await ready();
          if (stopped || generation !== version) return;
          if (verified) { stopped = true; options.forget(); emit('complete', {completed: stages.length}); return; }
          if (['failed', 'interrupted'].includes(job.state)) { stopped = true; emit('failed', {job}); return; }
          emit('confirming', {job});
        } else { emit('working', {job}); }
      } catch {
        if (stopped || generation !== version) return;
        failures++;
        const verified = await ready();
        if (stopped || generation !== version) return;
        if (verified) { stopped = true; options.forget(); emit('complete', {completed: stages.length}); return; }
        if (failures >= 60) { stopped = true; emit('unreachable'); return; }
        emit('reconnecting');
      }
      if (!stopped && generation === version) timer = schedule(() => tick(generation), Math.min(10000, 2000 + failures * 1000));
    }
    return {
      start(value) {
        if (!validJobId(value.job_id)) throw new Error('The saved setup reference is invalid. Check setup in the server terminal.');
        cancel(timer); version++; stopped = false; failures = 0;
        reference = {job_id: value.job_id, created_at: value.created_at || Date.now() / 1000, completed: Math.min(4, Math.max(0, value.completed || 0))};
        options.remember(reference); emit('working'); return tick();
      },
      stop() { stopped = true; version++; cancel(timer); },
    };
  }
  const helpers = {stages, privateOrigin, readyMatches, chooseTimezone, filterTimezones, capacity, storageLabel, backupChoices, selectedStorage, completedStages, watchInstallation};
  if (typeof module !== 'undefined' && module.exports) module.exports = helpers;
  if (typeof document === 'undefined') return;

  let state, choices = [], timezones = [], currentReference, watcher;
  const $ = id => document.getElementById(id);
  const labels = {media:'Media library',files:'Files',notes:'Notes',movies:'Movie Night',recipes:'Recipes',places:'Places',audiobooks:'Audiobooks',mytube:'MyTube',chat:'Household chat',games:'Games',assistant:'Local assistant',device_backup:'Phone Backup'};
  const error = message => {$('error').textContent = message; $('error').hidden = false;};
  const message = (id, text) => {if ($(id).textContent !== text) $(id).textContent = text;};
  const storedKey = () => `davidPiSetupJob:${state.instance_id}`;
  function remember(reference) {
    currentReference = reference;
    try {
      sessionStorage.setItem(storedKey(), JSON.stringify(reference));
      sessionStorage.setItem('davidPiSetupCurrent', JSON.stringify({instance_id: state.instance_id, origin: state.origin}));
    } catch { /* Server-side job discovery still works when browser storage is unavailable. */ }
  }
  function savedReference() {
    try { return JSON.parse(sessionStorage.getItem(storedKey()) || 'null'); } catch { return null; }
  }
  function forget() {
    try { sessionStorage.removeItem(storedKey()); sessionStorage.removeItem('davidPiSetupCurrent'); } catch { /* Optional browser storage. */ }
  }
  async function api(path, body) {
    const options = {cache: 'no-store', credentials: 'same-origin', redirect: 'error'};
    if (body !== undefined) Object.assign(options, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state?.csrf || ''}, body: JSON.stringify(body)});
    const response = await fetch(path, options);
    let data;
    try { data = await response.json(); } catch { throw new Error('The setup service is reconnecting. Try again shortly.'); }
    if (!response.ok) { const failure = new Error(data.error || 'This step could not finish.'); failure.status = response.status; throw failure; }
    return data;
  }
  async function verifyReady() {
    const [ready, metadata] = await Promise.all([api('/ready'), api('/api/installation')]);
    return readyMatches(ready, metadata, state, location.origin);
  }
  function option(value, text, disabled = false) {
    const item = document.createElement('option'); item.value = value; item.textContent = text; item.disabled = disabled; return item;
  }
  function renderTimezones() {
    const selected = $('timezone').value;
    const result = filterTimezones(timezones, $('timezone-search').value, selected);
    $('timezone').replaceChildren(...result.options.map(zone => option(zone, zone.replaceAll('_', ' ') + (zone === selected && !result.matches.includes(zone) ? ' (current selection)' : ''))));
    if (selected) $('timezone').value = selected;
    $('timezone-results').textContent = $('timezone-search').value.trim() ? `${result.matches.length} matching timezones. Your selection stays unchanged until you choose another.` : '';
  }
  function populateStorage(select, values, value, isBackup) {
    const options = [option('', isBackup ? 'Choose a backup location or skip' : 'Choose a storage location', true)];
    if (isBackup) options.push(option('skip', 'Skip backups for now — configure later'));
    options.push(...values.map(choice => option(choice.id, storageLabel(choice))));
    options.push(option('manual', 'Advanced: enter an existing folder path'));
    if (value && !options.some(item => item.value === value)) options.push(option(value, 'Previous choice is unavailable — choose another location', true));
    select.replaceChildren(...options); select.value = value;
  }
  function updateStorageHelp() {
    const dataId = $('storage-choice').value, backupId = $('backup-choice').value;
    const data = choices.find(choice => choice.id === dataId), backup = choices.find(choice => choice.id === backupId);
    $('storage-manual').hidden = dataId !== 'manual'; $('backup-manual').hidden = backupId !== 'manual';
    $('storage-mode').disabled = dataId !== 'manual'; $('storage-parent').disabled = dataId !== 'manual'; $('storage-parent').required = dataId === 'manual';
    $('backup-parent').disabled = backupId !== 'manual'; $('backup-parent').required = backupId === 'manual';
    $('storage-help').textContent = data ? `${capacity(data.free_bytes)} free of ${capacity(data.total_bytes)} · ${data.parent.replace(/\/+$/, '')}/david-pi-data${data.system_disk ? ' · This shares space with the server’s operating system.' : ''}` : (dataId === 'manual' ? 'Enter your existing folder below. Its filesystem will be checked before setup.' : 'Available space is checked again when you start setup. Only supported, prepared local storage is listed.');
    const available = backupChoices(choices, dataId);
    $('backup-help').textContent = backupId === 'skip' ? `Backups will show “not configured” until you add a destination.${available.length ? '' : ' No separate supported backup drive was found.'}` : (backup ? `${capacity(backup.free_bytes)} free of ${capacity(backup.total_bytes)} · ${backup.parent.replace(/\/+$/, '')}/david-pi-backups · A restore remains unverified until you test it.` : 'Only drives separate from your selected content drive are offered. Choose a location or skip for now.');
  }
  function renderStorage(initial = false) {
    const selectedData = $('storage-choice').value;
    const selectedBackup = initial ? 'skip' : $('backup-choice').value;
    populateStorage($('storage-choice'), choices, selectedData, false);
    populateStorage($('backup-choice'), backupChoices(choices, selectedData), selectedBackup, true);
    $('storage-warning').textContent = state.storage?.warning || (!choices.length ? 'No supported prepared storage was found. Connect and mount a local ext4 drive, then refresh, or use an existing ext4 folder through the advanced option.' : '');
    $('storage-warning').hidden = !$('storage-warning').textContent;
    updateStorageHelp();
  }
  function showProgress(reference, focus = true) {
    $('claim').hidden = true; $('wizard').hidden = true; $('progress').hidden = false;
    $('private-address').value = state.origin;
    if (focus) $('progress-title').focus();
    watcher?.stop();
    watcher = watchInstallation({fetchJob: id => api(`/api/job?id=${encodeURIComponent(id)}`), verifyReady, remember, forget, onChange: renderProgress});
    watcher.start(reference).catch(e => error(e.message));
  }
  function renderProgress(status) {
    const complete = status.kind === 'complete';
    $('setup-progress').value = status.completed;
    $('progress-label').textContent = `${status.completed} of ${stages.length} steps complete`;
    $('progress-steps').replaceChildren(...stages.map((stage, index) => {
      const item = document.createElement('li'), text = document.createElement('span'), detail = document.createElement('span');
      item.dataset.state = index < status.completed ? 'complete' : index === status.completed ? 'current' : 'waiting';
      if (index === status.completed && !complete) item.setAttribute('aria-current', 'step');
      text.textContent = stage.label; detail.className = 'step-status';
      detail.textContent = index < status.completed ? 'Complete' : index === status.completed ? (['failed','unreachable'].includes(status.kind) ? 'Needs attention' : 'In progress') : 'Waiting';
      text.append(detail); item.append(text); return item;
    }));
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - status.created_at));
    $('elapsed').textContent = `${complete ? 'Setup took' : 'Elapsed'}: ${Math.floor(seconds / 60)} min ${seconds % 60} sec.`;
    $('retry-status').hidden = !['failed', 'unreachable'].includes(status.kind);
    $('recovery').hidden = !['failed', 'unreachable'].includes(status.kind);
    $('open').hidden = !complete;
    if (complete) {
      $('error').hidden = true; $('progress-title').textContent = 'Your home server is ready';
      message('phase', 'All setup checks passed. You can open your home server.');
      $('progress-help').textContent = 'Bookmark your private address so it is easy to find next time.';
      $('open').href = privateOrigin(state.origin) + '/';
    } else if (status.kind === 'failed') {
      error(status.job.error || 'Setup was interrupted. Check the saved operation in the server terminal.');
      message('phase', 'Setup needs attention.');
      $('progress-help').textContent = 'Your setup choices are saved. Check the error above before trying recovery.';
    } else if (status.kind === 'unreachable') {
      message('phase', 'The browser could not reconnect to your server.');
      $('progress-help').textContent = 'Check that this device is still connected to the same Tailscale network. This page cannot yet confirm whether setup finished.';
    } else if (status.kind === 'reconnecting') {
      message('phase', 'Reconnecting as your private website starts…');
      $('progress-help').textContent = 'A brief connection change is expected at the last step. We will confirm that this is your server and that its features are ready.';
    } else {
      message('phase', status.kind === 'confirming' ? 'Confirming your private website is ready…' : (stages.find(stage => stage.phase === status.job?.phase)?.label || 'Preparing to start setup…'));
      $('progress-help').textContent = seconds >= 60 ? 'First startup can take several minutes, especially in a virtual machine. The bar advances when a step finishes. Keep the server powered on; refreshing this page is safe.' : 'Keep the server powered on. You can refresh this page and return to the saved setup progress.';
    }
  }
  async function load() {
    state = await api('/api/setup');
    if (!privateOrigin(state.origin) || privateOrigin(state.origin) !== location.origin) throw new Error('The setup address does not match this page. Open the exact private HTTPS link shown in the server terminal.');
    $('claim').hidden = true; $('wizard').hidden = false;
    timezones = [...new Set(state.timezones?.length ? state.timezones : [state.timezone || 'UTC', 'UTC'])].sort();
    let browserZone; try { browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { /* Use server default. */ }
    const selected = chooseTimezone(timezones, browserZone, state.timezone);
    $('timezone').replaceChildren(...timezones.map(zone => option(zone, zone.replaceAll('_', ' ')))); $('timezone').value = selected;
    $('timezone-help').textContent = selected === browserZone ? 'Suggested from this browser. Choose the timezone your household uses; you can change it later.' : 'Choose the timezone your household uses. The server’s default is selected where available.';
    $('address').replaceChildren(document.createTextNode('Your private address: '));
    const link = document.createElement('a'); link.href = state.origin; link.textContent = state.origin;
    $('address').append(link, document.createTextNode(` · Administrator: ${state.admin}`));
    choices = state.storage?.choices || []; renderStorage(true);
    const container = $('modules'); container.replaceChildren();
    Object.entries(labels).forEach(([key, label]) => {
      const item = document.createElement('label'); item.className = 'check';
      const input = document.createElement('input'); input.type = 'checkbox'; input.id = `module-${key}`; input.checked = true;
      item.append(input, document.createTextNode(label)); container.append(item);
    });
    $('module-chat').addEventListener('change', () => {if (!$('module-chat').checked) $('web-push').checked = false;});
    $('web-push').addEventListener('change', () => {if ($('web-push').checked) $('module-chat').checked = true;});
    $('module-media').addEventListener('change', () => {if (!$('module-media').checked) $('module-device_backup').checked = false;});
    $('module-device_backup').addEventListener('change', () => {if ($('module-device_backup').checked) $('module-media').checked = true;});
    const job = state.active_job, saved = savedReference();
    if (job && validJobId(job.id)) showProgress({job_id: job.id, created_at: job.created_at, completed: job.id === saved?.job_id ? saved.completed : 0}, false);
    else if (saved && validJobId(saved.job_id)) showProgress(saved, false);
  }
  $('claim-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button'); button.disabled = true;
    try {await api('/api/claim', {token: $('token').value.trim()}); $('token').value = ''; await load(); $('error').hidden = true;}
    catch (e) {error(e.message);} finally {button.disabled = false;}
  });
  $('timezone-search').addEventListener('input', renderTimezones);
  $('storage-choice').addEventListener('change', () => {
    const values = backupChoices(choices, $('storage-choice').value), old = $('backup-choice').value;
    const preserved = ['manual', 'skip', ''].includes(old) || values.some(choice => choice.id === old) ? old : '';
    populateStorage($('backup-choice'), values, preserved, true); updateStorageHelp();
  });
  $('backup-choice').addEventListener('change', updateStorageHelp);
  $('refresh-storage').addEventListener('click', async () => {
    $('refresh-storage').disabled = true;
    try {
      const fresh = await api('/api/setup');
      if (fresh.instance_id !== state.instance_id || fresh.origin !== state.origin) throw new Error('The server identity changed. Reopen the private setup link from its terminal.');
      state.storage = fresh.storage; state.csrf = fresh.csrf; choices = fresh.storage?.choices || []; renderStorage();
      $('error').hidden = true;
    } catch (e) {error(e.message);} finally {$('refresh-storage').disabled = false;}
  });
  $('copy-address').addEventListener('click', async () => {
    try {await navigator.clipboard.writeText(state.origin); $('copy-status').textContent = 'Address copied.';}
    catch {$('private-address').focus(); $('private-address').select(); $('copy-status').textContent = 'Select and copy the address above.';}
  });
  $('retry-status').addEventListener('click', () => {$('error').hidden = true; showProgress(currentReference, false);});
  document.querySelectorAll('[data-skip]').forEach(button => button.addEventListener('click', () => {$(button.dataset.skip).value = ''; button.textContent = 'Skipped — configure later in settings';}));
  document.querySelectorAll('[data-test]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try {await api('/api/test-integration', {name: button.dataset.test, credential: $(button.dataset.test === 'movies' ? 'tmdb' : 'mealdb').value.trim()}); button.textContent = 'Connection verified';}
    catch (e) {error(e.message);} finally {button.disabled = false;}
  }));
  $('wizard').addEventListener('submit', async event => {
    event.preventDefault(); $('error').hidden = true; $('install').disabled = true;
    try {
      const modules = {};
      Object.keys(labels).forEach(key => {modules[key] = $(`module-${key}`).checked ? 'enabled' : 'disabled';});
      ['movies', 'recipes'].forEach(key => {if (modules[key] !== 'disabled') modules[key] = $(key === 'movies' ? 'tmdb' : 'mealdb').value.trim() ? 'connected' : 'manual';}); modules.pihole = 'disabled';
      const selected = selectedStorage(choices, $('storage-choice').value, $('backup-choice').value, {parent: $('storage-parent').value, backup: $('backup-parent').value, mode: $('storage-mode').value});
      const configuration = {schema_version: 1, instance_id: state.instance_id, display_name: $('display-name').value.trim(), hostname: state.hostname, public_url: state.origin, timezone: $('timezone').value, country: $('country').value.toUpperCase(), members: [{login: state.admin, name: $('owner-name').value.trim(), role: 'admin'}], storage: selected.storage, modules, integrations: {web_push: $('web-push').checked}};
      const result = await api('/api/install', {configuration, storage_selection: selected.selection, secrets: {TMDB_API_READ_TOKEN: $('tmdb').value.trim(), THEMEALDB_API_KEY: $('mealdb').value.trim()}});
      $('tmdb').value = ''; $('mealdb').value = '';
      showProgress({job_id: result.job_id, created_at: Date.now() / 1000, completed: 0});
    } catch (e) {error(e.message); $('install').disabled = false;}
  });
  load().catch(failure => {
    let resumed = false;
    // During the Serve handoff, setup endpoints disappear. A saved reference lets
    // an already-open page verify the portal without trusting metadata alone.
    try {
      const previous = JSON.parse(sessionStorage.getItem('davidPiSetupCurrent') || 'null');
      if (previous && privateOrigin(previous.origin) === location.origin) {
        state = previous; const saved = savedReference();
        if (saved && validJobId(saved.job_id)) { showProgress(saved, false); resumed = true; }
      }
    } catch { /* A fresh browser remains on the claim screen. */ }
    if (!resumed && (state || failure.status !== 403)) error(failure.message);
  });
})();
