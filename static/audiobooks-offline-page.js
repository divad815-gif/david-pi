'use strict';

const offlineApi = window.DavidPiBrowserAudiobooks;
const offlineGrid = document.querySelector('#offlineAudiobookGrid');
const offlineState = document.querySelector('#offlineShelfState');
const offlineAudio = document.querySelector('#offlineAudio');
const offlineConnection = document.querySelector('#offlineConnection');
const offlineCompleteCount = document.querySelector('#offlineCompleteCount');
const offlinePartialCount = document.querySelector('#offlinePartialCount');
const offlineAvailableSpace = document.querySelector('#offlineAvailableSpace');
const offlineStorageNote = document.querySelector('#offlineStorageNote');
const offlinePartials = document.querySelector('#offlinePartials');
const offlinePartialList = document.querySelector('#offlinePartialList');
const removeAllOfflineBooks = document.querySelector('#removeAllOfflineBooks');
const testOfflineBooks = document.querySelector('#testOfflineBooks');

let offlineActive = null;
let offlineLastSecond = -1;
let offlineGeneration = 0;
let offlineMetadataListener = null;
let offlinePlaybackSource = null;
let offlineRestoring = true;

function offlineTime(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  return `${Math.floor(total / 3600)}:${String(Math.floor(total % 3600 / 60)).padStart(2, '0')}`;
}

function offlineBytes(bytes) {
  if (!Number.isFinite(Number(bytes)) || Number(bytes) < 0) return 'Unavailable';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value < 10 && unit ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function updateConnectionState() {
  const online = navigator.onLine !== false;
  offlineConnection.textContent = online
    ? 'Online · downloads available'
    : 'Offline · saved books still play';
  offlineConnection.classList.toggle('is-offline', !online);
}

function clearOfflineMetadataListener() {
  if (offlineMetadataListener) {
    offlineAudio.removeEventListener('loadedmetadata', offlineMetadataListener);
    offlineMetadataListener = null;
  }
}

function closeOfflinePlayer() {
  saveOfflinePosition(true);
  offlineRestoring=true;
  offlineGeneration+=1;clearOfflineMetadataListener();
  offlineAudio.pause();
  offlineAudio.removeAttribute('src');
  offlineAudio.removeAttribute('aria-label');
  offlineAudio.load();
  if (offlinePlaybackSource) offlineApi.releasePlaybackSource(offlinePlaybackSource);
  offlinePlaybackSource = null;
  offlineActive = null;
  offlineLastSecond = -1;
  document.querySelector('#offlinePlayer').hidden = true;
  document.querySelector('#offlinePlayerTitle').textContent = '';
  document.querySelector('#offlinePlayerAuthor').textContent = '';
}

async function playOfflineBook(book) {
  closeOfflinePlayer();
  const generation = offlineGeneration;
  let source = null;
  try {
    source = await offlineApi.managerPlaybackSource(book.id);
    if (!source) throw new Error('This offline copy is not verified for playback.');
    if (generation !== offlineGeneration) { offlineApi.releasePlaybackSource(source); return; }
    offlinePlaybackSource = source;
    offlineActive = book;
    offlineAudio.src = source.playback_url;
    offlineAudio.setAttribute('aria-label', `Playing ${book.title} offline`);
    document.querySelector('#offlinePlayerTitle').textContent = book.title;
    document.querySelector('#offlinePlayerAuthor').textContent = book.author || 'Unknown author';
    document.querySelector('#offlinePlayer').hidden = false;
    offlineMetadataListener = () => {
      clearOfflineMetadataListener();
      if(generation!==offlineGeneration||offlineActive!==book)return;
      if (
        book.position_seconds > 0
        && Number.isFinite(offlineAudio.duration)
        && book.position_seconds < offlineAudio.duration - 10
      ) offlineAudio.currentTime = book.position_seconds;
      offlineRestoring=false;
      offlineAudio.play().catch(() => {
        if (generation === offlineGeneration && offlineActive === book) {
          offlineState.textContent = 'The book is ready. Tap play to begin.';
        }
      });
    };
    offlineAudio.addEventListener('loadedmetadata', offlineMetadataListener);
    offlineAudio.load();
  } catch (error) {
    if (source) {
      if (source === offlinePlaybackSource) offlinePlaybackSource = null;
      offlineApi.releasePlaybackSource(source);
    }
    if (generation === offlineGeneration) {
      offlineState.textContent = error.message || 'This offline copy could not be opened.';
    }
  }
}

function renderReadiness(inventory) {
  offlineCompleteCount.textContent = String(inventory.complete_count);
  offlinePartialCount.textContent = String(inventory.partial_count);
  offlineAvailableSpace.textContent = inventory.available_bytes === null
    ? 'Not reported' : offlineBytes(inventory.available_bytes);

  const storageParts = [];
  if (inventory.complete_bytes) storageParts.push(`${offlineBytes(inventory.complete_bytes)} saved for complete books`);
  if (inventory.usage_bytes !== null) storageParts.push(`${offlineBytes(inventory.usage_bytes)} used by this site`);
  if (inventory.persistent === true) storageParts.push('storage protection is enabled');
  else if (inventory.persistent === false) storageParts.push('the browser may clear copies when space is tight');
  else storageParts.push('storage protection was not reported');
  if (inventory.has_unresolved_data) storageParts.push('some incomplete browser data can be cleared with Remove all');
  offlineStorageNote.textContent = `${storageParts.join(' · ')}.`;

  offlinePartials.hidden = inventory.partials.length === 0;
  offlinePartialList.replaceChildren(...inventory.partials.map(book => {
    const row = document.createElement('article');
    row.className = 'offline-partial-card';
    const title = document.createElement('strong');
    title.textContent = book.title;
    const progress = document.createElement('span');
    const expected = Number(book.expected_bytes) || 0;
    const downloaded = Number(book.downloaded_bytes) || 0;
    const percent = expected ? Math.min(100, Math.round(downloaded * 100 / expected)) : 0;
    progress.textContent = `${percent}% saved · ${offlineBytes(downloaded)} of ${offlineBytes(expected)}`;
    row.append(title, progress);
    return row;
  }));
}

function offlineBookCard(book) {
  const card = document.createElement('article');
  card.className = 'offline-audiobook-card';
  const title = document.createElement('strong');
  title.textContent = book.title;
  const author = document.createElement('p');
  author.textContent = book.author || 'Unknown author';
  const progress = document.createElement('p');
  progress.textContent = `Resume at ${offlineTime(book.position_seconds)}`;
  const actions = document.createElement('div');
  actions.className = 'offline-audiobook-actions';
  const play = document.createElement('button');
  play.type = 'button';
  play.textContent = book.position_seconds > 1 ? 'Resume' : 'Play';
  play.setAttribute('aria-label', `${play.textContent} ${book.title} offline`);
  play.addEventListener('click', () => playOfflineBook(book));
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.textContent = 'Remove copy';
  remove.className = 'danger-text';
  remove.setAttribute('aria-label', `Remove offline copy of ${book.title}`);
  remove.addEventListener('click', async () => {
    if (!confirm(`Remove the browser copy of “${book.title}”? The David-Pi original is unchanged.`)) return;
    remove.disabled = true;
    try {
      if (offlineActive?.id === book.id) closeOfflinePlayer();
      await offlineApi.remove(book.id);
      await renderOfflineShelf();
    } catch (error) {
      offlineState.textContent = error.message || 'The browser copy could not be fully removed.';
    } finally { remove.disabled = false; }
  });
  actions.append(play, remove);
  card.append(title, author, progress, actions);
  return card;
}

async function renderOfflineShelf() {
  offlineGrid.setAttribute('aria-busy', 'true');
  updateConnectionState();
  try {
    if (!offlineApi?.supported()) {
      offlineState.textContent = 'This browser does not support the private offline storage used by David-Pi.';
      offlineStorageNote.textContent = 'Use the Android app for dependable offline playback on this device.';
      return;
    }
    const inventory = await offlineApi.inventory();
    renderReadiness(inventory);
    offlineState.textContent = inventory.books.length
      ? `${inventory.books.length} complete ${inventory.books.length === 1 ? 'book is' : 'books are'} ready without internet.`
      : 'No complete browser copies are saved on this device.';
    removeAllOfflineBooks.hidden = !inventory.has_data;
    testOfflineBooks.hidden = inventory.books.length === 0;
    testOfflineBooks.dataset.bookIds = inventory.books.map(book => book.id).join(',');
    offlineGrid.replaceChildren(...inventory.books.map(offlineBookCard));
  } catch (error) {
    offlineState.textContent = error.message || 'Saved books could not be read.';
    offlineStorageNote.textContent = 'Offline readiness could not be verified in this browser.';
  } finally { offlineGrid.setAttribute('aria-busy', 'false'); }
}

function saveOfflinePosition(force = false) {
  if (!offlineActive || offlineRestoring || !Number.isFinite(offlineAudio.currentTime)) return;
  const second = Math.floor(offlineAudio.currentTime);
  if (!force && offlineLastSecond >= 0 && Math.abs(second - offlineLastSecond) < 5) return;
  offlineLastSecond = second;
  const completed = offlineAudio.duration > 0 && offlineAudio.currentTime >= offlineAudio.duration - 15;
  offlineActive.position_seconds = offlineAudio.currentTime;
  const queue = window.DavidPiAudiobookProgress.create(offlineActive.progress_scope);
  try { queue.mark(offlineActive.id, offlineAudio.currentTime, completed, Date.now(), offlineActive); }
  catch (error) { offlineState.textContent = error.message; }
  offlineApi.updatePosition(offlineActive.id, offlineAudio.currentTime, completed, offlineActive.progress_scope).catch(error => {
    offlineState.textContent = error.message || 'Playback position could not be saved in this browser.';
  });
}
offlineAudio.addEventListener('timeupdate', () => saveOfflinePosition());
for (const event of ['pause', 'seeked', 'ended']) offlineAudio.addEventListener(event, () => saveOfflinePosition(true));

testOfflineBooks.addEventListener('click', async () => {
  const ids = (testOfflineBooks.dataset.bookIds || '').split(',').filter(Boolean);
  testOfflineBooks.disabled = true;
  offlineState.textContent = `Testing ${ids.length} saved ${ids.length === 1 ? 'book' : 'books'} without using the network…`;
  try {
    const inventory = await offlineApi.inventory();
    const books = new Map(inventory.books.map(book => [book.id, book]));
    for (const id of ids) {
      const book = books.get(id);
      const source = book ? await offlineApi.managerPlaybackSource(id) : null;
      if (!source) throw new Error('A saved copy is no longer verified for playback.');
      offlineApi.releasePlaybackSource(source);
    }
    offlineState.textContent = `Offline test passed for ${ids.length} ${ids.length === 1 ? 'book' : 'books'}.`;
  } catch (error) {
    offlineState.textContent = error.message || 'The offline playback test failed.';
  } finally { testOfflineBooks.disabled = false; }
});

removeAllOfflineBooks.addEventListener('click', async () => {
  if (!confirm('Remove every audiobook saved in this browser profile, including incomplete copies? The originals on David-Pi are unchanged.')) return;
  removeAllOfflineBooks.disabled = true;
  try {
    closeOfflinePlayer();
    await offlineApi.removeAll();
    await renderOfflineShelf();
    offlineState.textContent = 'All browser copies were removed.';
  } catch (error) {
    offlineState.textContent = error.message || 'Some browser copies could not be removed.';
  } finally { removeAllOfflineBooks.disabled = false; }
});

window.addEventListener('online', updateConnectionState);
window.addEventListener('offline', updateConnectionState);
window.addEventListener('beforeunload', () => {
  offlineGeneration += 1;
  clearOfflineMetadataListener();
});

if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
renderOfflineShelf();
