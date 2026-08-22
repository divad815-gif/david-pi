const gallery = document.querySelector('#gallery');
const emptyState = document.querySelector('#emptyState');
const loadMore = document.querySelector('#loadMore');
const sheet = document.querySelector('#uploadSheet');
const input = document.querySelector('#photoInput');
const selection = document.querySelector('#selection');
const startUpload = document.querySelector('#startUpload');
const progress = document.querySelector('#progress');
const progressBar = progress.querySelector('span');
const message = document.querySelector('#uploadMessage');
const uploadQueue = document.querySelector('#uploadQueue');
const viewer = document.querySelector('#viewer');
const viewerStage = document.querySelector('#viewerMediaStage');
const viewerImage = document.querySelector('#viewerImage');
const viewerVideo = document.querySelector('#viewerVideo');
const viewerName = document.querySelector('#viewerName');
const downloadOriginal = document.querySelector('#downloadOriginal');
const collectionRail = document.querySelector('#collectionRail');
const collectionToolbar = document.querySelector('#collectionToolbar');
const collectionEditor = document.querySelector('#collectionEditor');
const collectionName = document.querySelector('#collectionName');
const collectionMessage = document.querySelector('#collectionMessage');
const organizeSheet = document.querySelector('#organizeSheet');
const organizerBackdrop = document.querySelector('#organizerBackdrop');
const collectionChecks = document.querySelector('#collectionChecks');
// Capture the server-rendered catalog before any asynchronous organizer work.
// These rows are the fail-safe source in embedded Android WebView: an API or
// cache failure must never turn a valid collection list into an empty sheet.
const serverOrganizerCatalog = [...collectionChecks.querySelectorAll('.server-collection-check')]
  .map((label) => ({
    id: String(label.dataset.collectionId || label.querySelector('input')?.dataset.collectionId || ''),
    name: label.querySelector('span')?.textContent?.trim() || '',
  }))
  .filter((collection) => collection.id && collection.name);
const organizeMessage = document.querySelector('#organizeMessage');
const saveOrganization = document.querySelector('#saveOrganization');
const photoToast = document.querySelector('#photoToast');
const timelineYears = document.querySelector('#timelineYears');
const timelineMonths = document.querySelector('#timelineMonths');
const timelineSummary = document.querySelector('#timelineSummary');
const confirmSheet = document.querySelector('#confirmSheet');
const slideshowSheet = document.querySelector('#slideshowSheet');
const slideshowCollection = document.querySelector('#slideshowCollection');
const slideshowMessage = document.querySelector('#slideshowMessage');
const slideshowProgress = document.querySelector('#slideshowProgress');
const slideshowProgressBar = slideshowProgress.querySelector('span');
const slideshowMusic = document.querySelector('#slideshowMusic');
const slideshowMusicPreview = document.querySelector('#slideshowMusicPreview');
const slideshowMusicCredit = document.querySelector('#slideshowMusicCredit');
let slideshowMusicTracks = [];
let nextCursor = null;
let hasMorePhotos = true;
let loadingPhotos = false;
const pageSize = 30;
let loadedPhotos = [];
let totalPhotos = 0;
let deletedPhotoTotal = 0;
let currentPhotoIndex = -1;
let viewerLoadGeneration = 0;
const viewerPreviewCache = new Map();
const viewerPreviewCacheLimit = 8;
let touchStartX = null;
let touchStartY = null;
let touchSwipeActive = false;
let zoom = 1, panX = 0, panY = 0, pinchDistance = 0, pinchZoom = 1;
let panStartX = 0, panStartY = 0, panOriginX = 0, panOriginY = 0, pinchActive = false;

function applyViewerTransform(animate=false) {
  viewerImage.style.transition = animate ? 'transform 160ms ease' : 'none';
  viewerImage.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;
  viewerImage.classList.toggle('zoomed', zoom > 1.01);
}
function resetViewerZoom(animate=false) {
  zoom = 1; panX = 0; panY = 0; pinchDistance = 0; pinchActive = false;
  applyViewerTransform(animate);
}
function distanceBetween(touches) {
  return Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY);
}

function resetViewerTouch() {
  touchStartX = null;
  touchStartY = null;
  touchSwipeActive = false;
  pinchActive = false;
}

function stopViewerVideo({clearPoster = false} = {}) {
  viewerVideo.pause();
  viewerVideo.removeAttribute('src');
  if (clearPoster) viewerVideo.removeAttribute('poster');
  // Calling load() after clearing src aborts an in-flight media request. This
  // keeps a slow video from blocking or consuming bandwidth after a swipe.
  viewerVideo.load();
}
let collections = [];
let activeCollection = '';
let ownerView = '';
let editorMode = 'create';
let confirmAction = null;
let selectionMode = false;
let selectedIds = new Set();
let rangeSelectionStart = null;
let organizeIds = [];
let organizeBaseline = new Map();
let organizeDesired = new Map();
let organizerSaving = false;
let resumeOrganizer = null;
let organizerReturnFocus = null;
let organizerGeneration = 0;
let organizerCatalog = [];
let organizerWatchdog = null;
let galleryGeneration = 0;
let activePeriod = '';
let timelineData = [];
let galleryDensity = localStorage.getItem('davidPiGalleryDensity') === 'compact' ? 'compact' : 'comfortable';
const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
if (/Android/i.test(navigator.userAgent) && /;\s*wv\)/i.test(navigator.userAgent)) {
  document.documentElement.classList.add('davidpi-android-app-shell');
}

async function api(url, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    options.headers = {...options.headers, 'X-CSRF-Token': csrf};
  }
  const response = await fetch(url, options);
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || 'Something went wrong.');
  return result;
}

function monthLabel(period, includeYear = false) {
  const [year, month] = period.split('-').map(Number);
  return new Intl.DateTimeFormat(undefined, {
    month: 'long', ...(includeYear ? {year: 'numeric'} : {}), timeZone: 'UTC',
  }).format(new Date(Date.UTC(year, month - 1, 1)));
}

function setGalleryDensity(value, preservePosition = true) {
  const position = preservePosition ? captureGalleryPosition() : null;
  galleryDensity = value === 'compact' ? 'compact' : 'comfortable';
  localStorage.setItem('davidPiGalleryDensity', galleryDensity);
  applyGalleryDensity();
  if (position) restoreGalleryPosition(position);
}

function monthHeading(period) {
  const row = document.createElement('div');
  row.className = 'gallery-month-row';
  const heading = document.createElement('h2');
  heading.className = 'gallery-month';
  heading.textContent = monthLabel(period, true);
  const controls = document.createElement('span');
  controls.className = 'gallery-month-zoom';
  const zoomOut = document.createElement('button');
  zoomOut.type = 'button';
  zoomOut.dataset.galleryDensity = 'compact';
  zoomOut.textContent = '−';
  zoomOut.setAttribute('aria-label', `Zoom out near ${heading.textContent}`);
  zoomOut.disabled = galleryDensity === 'compact';
  zoomOut.addEventListener('click', () => setGalleryDensity('compact'));
  const zoomIn = document.createElement('button');
  zoomIn.type = 'button';
  zoomIn.dataset.galleryDensity = 'comfortable';
  zoomIn.textContent = '+';
  zoomIn.setAttribute('aria-label', `Zoom in near ${heading.textContent}`);
  zoomIn.disabled = galleryDensity === 'comfortable';
  zoomIn.addEventListener('click', () => setGalleryDensity('comfortable'));
  controls.append(zoomOut, zoomIn);
  row.append(heading, controls);
  return row;
}

function renderTimeline() {
  const years = new Map();
  timelineData.forEach((entry) => {
    const year = entry.month.slice(0, 4);
    years.set(year, (years.get(year) || 0) + entry.item_count);
  });
  const selectedYear = activePeriod ? activePeriod.slice(0, 4) : '';
  timelineYears.replaceChildren(...[...years.entries()].map(([year, count]) => {
    const group = document.createElement('span');
    group.className = 'timeline-choice';
    const button = document.createElement('button');
    button.type = 'button';
    button.classList.toggle('selected', activePeriod === year);
    button.textContent = `${year} · ${count}`;
    button.addEventListener('click', () => setTimelinePeriod(year));
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'timeline-open';
    open.textContent = 'View larger';
    open.setAttribute('aria-label', `View ${year} in the larger photo grid`);
    open.addEventListener('click', () => openTimelinePeriod(year));
    group.append(button, open);
    return group;
  }));
  const shownMonths = selectedYear
    ? timelineData.filter((entry) => entry.month.startsWith(`${selectedYear}-`))
    : timelineData.slice(0, 12);
  timelineMonths.replaceChildren(...shownMonths.map((entry) => {
    const group = document.createElement('span');
    group.className = 'timeline-choice';
    const button = document.createElement('button');
    button.type = 'button';
    button.classList.toggle('selected', activePeriod === entry.month);
    const name = document.createElement('span');
    name.textContent = monthLabel(entry.month, !selectedYear);
    const count = document.createElement('small');
    count.textContent = `${entry.item_count} item${entry.item_count === 1 ? '' : 's'}`;
    button.append(name, count);
    button.addEventListener('click', () => setTimelinePeriod(entry.month));
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'timeline-open';
    open.textContent = 'View larger';
    open.setAttribute('aria-label', `View ${monthLabel(entry.month, true)} in the larger photo grid`);
    open.addEventListener('click', () => openTimelinePeriod(entry.month));
    group.append(button, open);
    return group;
  }));
  timelineSummary.textContent = activePeriod
    ? (activePeriod.length === 4 ? activePeriod : monthLabel(activePeriod, true))
    : 'All years and months';
}

async function loadTimeline() {
  if (activeCollection === 'deleted') {
    timelineData = [];
    document.querySelector('#mediaTimeline').hidden = true;
    return;
  }
  document.querySelector('#mediaTimeline').hidden = false;
  const query = new URLSearchParams();
  if (ownerView) query.set('view', ownerView);
  if (activeCollection) query.set('collection', activeCollection);
  const result = await api(`/api/photos/timeline?${query}`);
  timelineData = result.months || [];
  renderTimeline();
}

async function setTimelinePeriod(period) {
  activePeriod = activePeriod === period ? '' : period;
  renderTimeline();
  await refreshPhotos();
  document.querySelector('#mediaTimeline').open = false;
  gallery.scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function openTimelinePeriod(period) {
  activePeriod = period;
  galleryDensity = 'comfortable';
  localStorage.setItem('davidPiGalleryDensity', galleryDensity);
  applyGalleryDensity();
  renderTimeline();
  await refreshPhotos();
  document.querySelector('#mediaTimeline').open = false;
  gallery.scrollIntoView({behavior: 'smooth', block: 'start'});
}

document.querySelector('#clearTimeline').addEventListener('click', () => setTimelinePeriod(''));

function applyGalleryDensity() {
  gallery.classList.toggle('compact', galleryDensity === 'compact');
  document.querySelectorAll('[data-density]').forEach((button) => {
    const selected = button.dataset.density === galleryDensity;
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-pressed', selected ? 'true' : 'false');
  });
  // Month controls are created as the paged gallery grows. Refresh every
  // control after either button is used so + and - always remain reversible.
  document.querySelectorAll('[data-gallery-density]').forEach((button) => {
    button.disabled = button.dataset.galleryDensity === galleryDensity;
    button.setAttribute('aria-pressed', button.disabled ? 'true' : 'false');
  });
}

document.querySelectorAll('[data-density]').forEach((button) => button.addEventListener('click', () => {
  setGalleryDensity(button.dataset.density);
}));
applyGalleryDensity();

function openUpload() {
  message.textContent = '';
  const collection = collections.find((item) => item.id === activeCollection);
  const visibility = document.querySelector('#mediaVisibility');
  visibility.value = collection?.visibility === 'private' ? 'private' : 'shared';
  visibility.disabled = collection?.visibility === 'private';
  sheet.showModal();
}

document.querySelector('#openUpload').addEventListener('click', openUpload);
document.querySelectorAll('[data-upload]').forEach((button) => button.addEventListener('click', openUpload));

input.addEventListener('change', () => {
  const count = input.files.length;
  selection.hidden = !count;
  startUpload.hidden = !count;
  selection.textContent = count ? `${count} item${count === 1 ? '' : 's'} selected` : '';
  uploadQueue.hidden = true;
  uploadQueue.innerHTML = '';
});

function uploadFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const data = new FormData();
    data.append('media', file);
    data.append('visibility', document.querySelector('#mediaVisibility').value);
    if (activeCollection && activeCollection !== 'deleted') data.append('collection_id', activeCollection);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    xhr.setRequestHeader('X-CSRF-Token', csrf);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      try {
        const result = JSON.parse(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) resolve(result);
        else reject(new Error(result.error || 'Upload failed'));
      } catch { reject(new Error('Upload failed')); }
    };
    xhr.onerror = () => reject(new Error('Connection lost during upload'));
    xhr.send(data);
  });
}

function queueRow(file) {
  const row = document.createElement('div');
  row.className = 'upload-item';
  const name = document.createElement('strong');
  name.textContent = file.name;
  const status = document.createElement('span');
  status.textContent = 'Waiting';
  row.append(name, status);
  uploadQueue.append(row);
  return status;
}

async function uploadWithRetry(file, status, onProgress) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      status.textContent = attempt ? 'Retrying…' : 'Uploading…';
      const result = await uploadFile(file, onProgress);
      if (result.errors?.length) throw new Error(result.errors[0].reason || 'Could not process this file.');
      status.textContent = result.duplicates?.length ? 'Already here' : 'Added';
      return {result, ok: true};
    } catch (error) {
      if (attempt === 1) {
        status.textContent = 'Failed';
        status.title = error.message;
        return {error, ok: false};
      }
    }
  }
}

startUpload.addEventListener('click', async () => {
  if (!input.files.length) return;
  const files = [...input.files];
  startUpload.disabled = true;
  input.disabled = true;
  progress.hidden = false;
  uploadQueue.hidden = false;
  uploadQueue.innerHTML = '';
  progressBar.style.width = '2%';
  message.textContent = 'Uploading… keep this page open.';
  try {
    const statuses = files.map(queueRow);
    const progressByFile = files.map(() => 0);
    const updateProgress = () => {
      const totalBytes = files.reduce((sum, file) => sum + Math.max(file.size, 1), 0);
      const uploadedBytes = files.reduce((sum, file, index) => sum + Math.max(file.size, 1) * progressByFile[index], 0);
      progressBar.style.width = `${Math.max(2, Math.round(uploadedBytes / totalBytes * 100))}%`;
    };
    const results = new Array(files.length);
    let cursor = 0;
    const worker = async () => {
      while (cursor < files.length) {
        const index = cursor++;
        results[index] = await uploadWithRetry(files[index], statuses[index], (value) => {
          progressByFile[index] = value;
          updateProgress();
        });
        progressByFile[index] = 1;
        updateProgress();
      }
    };
    const hasVideo = files.some((file) => file.type.startsWith('video/') || /\.(mov|m4v|mp4)$/i.test(file.name));
    await Promise.all(Array.from({length: hasVideo ? 1 : Math.min(2, files.length)}, worker));
    const added = results.filter((item) => item.ok && item.result.added?.length).length;
    const duplicates = results.filter((item) => item.ok && item.result.duplicates?.length).length;
    const failed = results.filter((item) => !item.ok).length;
    const bits = [];
    if (added) bits.push(`${added} added`);
    if (duplicates) bits.push(`${duplicates} already here`);
    if (failed) bits.push(`${failed} couldn’t be added`);
    progressBar.style.width = '100%';
    message.textContent = bits.join(' · ') || 'All done.';
    input.value = '';
    selection.hidden = true;
    startUpload.hidden = true;
    await refreshPhotos();
    await loadCollections();
    if (!failed) setTimeout(() => sheet.close(), 1500);
  } catch (error) {
    message.textContent = error.message;
  } finally {
    startUpload.disabled = false;
    input.disabled = false;
  }
});

function viewerPreviewEntry(photo) {
  const url = photo.view || photo.preview;
  let entry = viewerPreviewCache.get(photo.id);
  if (entry?.url === url) {
    viewerPreviewCache.delete(photo.id);
    viewerPreviewCache.set(photo.id, entry);
    return entry;
  }
  const image = new Image();
  image.decoding = 'async';
  image.fetchPriority = 'high';
  entry = {
    url,
    image,
    ready: new Promise((resolve) => {
      image.addEventListener('load', () => resolve(true), {once: true});
      image.addEventListener('error', () => resolve(false), {once: true});
    }),
  };
  image.src = url;
  viewerPreviewCache.set(photo.id, entry);
  while (viewerPreviewCache.size > viewerPreviewCacheLimit) {
    viewerPreviewCache.delete(viewerPreviewCache.keys().next().value);
  }
  return entry;
}

function primeViewerNeighbors(index) {
  [index + 1, index - 1, index + 2].forEach((candidate) => {
    const photo = loadedPhotos[candidate];
    if (photo && !photo.is_video) viewerPreviewEntry(photo);
  });
}

async function upgradeViewerImage(photo, generation) {
  const entry = viewerPreviewEntry(photo);
  const ready = await entry.ready;
  if (!ready || generation !== viewerLoadGeneration || loadedPhotos[currentPhotoIndex]?.id !== photo.id) return;
  viewerImage.src = entry.url;
  viewerStage.classList.remove('loading');
}

async function upgradeViewerPoster(photo, generation) {
  const entry = viewerPreviewEntry(photo);
  const ready = await entry.ready;
  if (!ready || generation !== viewerLoadGeneration || loadedPhotos[currentPhotoIndex]?.id !== photo.id) return;
  viewerVideo.poster = entry.url;
}

function showPhoto(index) {
  if (index < 0 || index >= loadedPhotos.length) return;
  currentPhotoIndex = index;
  const generation = ++viewerLoadGeneration;
  const photo = loadedPhotos[index];
  resetViewerZoom();
  stopViewerVideo();
  viewerVideo.loop = false;
  viewerImage.hidden = photo.is_video;
  viewerVideo.hidden = !photo.is_video;
  if (photo.is_video) {
    viewerStage.classList.remove('loading');
    viewerImage.removeAttribute('src');
    viewerVideo.poster = photo.thumb;
    viewerVideo.src = photo.playback;
    viewerVideo.loop = Boolean(photo.loop_playback);
    viewerVideo.load();
    upgradeViewerPoster(photo, generation);
  } else {
    viewerVideo.removeAttribute('poster');
    viewerImage.src = photo.thumb;
    viewerImage.alt = photo.original_name;
    viewerStage.classList.add('loading');
    upgradeViewerImage(photo, generation);
  }
  primeViewerNeighbors(index);
  viewerName.textContent = `${photo.original_name} · ${photo.visibility === 'private' ? 'Only me' : `Added by ${photo.owner_display}`}`;
  document.querySelector('#viewerPrivacy').textContent = photo.visibility === 'private' ? 'Share' : 'Only me';
  downloadOriginal.href = photo.original;
  const deleted = activeCollection === 'deleted';
  document.querySelector('#organizePhoto').hidden = deleted;
  document.querySelector('#restorePhoto').hidden = !deleted;
  document.querySelector('#deletePhoto').textContent = deleted ? 'Delete forever' : 'Delete';
  document.querySelector('#previousPhoto').disabled = index === 0;
  document.querySelector('#nextPhoto').disabled = index >= totalPhotos - 1;
}

function openPhoto(photoId) {
  const index = loadedPhotos.findIndex((photo) => photo.id === photoId);
  showPhoto(index);
  viewer.showModal();
}

async function movePhoto(direction) {
  const wanted = currentPhotoIndex + direction;
  if (direction > 0 && wanted >= loadedPhotos.length && loadedPhotos.length < totalPhotos) await loadPhotos();
  showPhoto(currentPhotoIndex + direction);
}

function photoCard(photo) {
  const button = document.createElement('button');
  button.className = 'photo';
  button.type = 'button';
  button.dataset.photoId = photo.id;
  button.setAttribute('aria-label', `Open ${photo.original_name}`);
  const image = document.createElement('img');
  image.src = photo.thumb;
  image.alt = '';
  image.loading = 'lazy';
  image.decoding = 'async';
  image.fetchPriority = 'low';
  button.append(image);
  if (photo.is_video) {
    button.classList.add('video-item');
    const badge = document.createElement('span');
    badge.className = 'video-badge';
    badge.textContent = '▶';
    badge.setAttribute('aria-hidden', 'true');
    button.append(badge);
  }
  const check = document.createElement('span');
  check.className = 'photo-check';
  check.setAttribute('aria-hidden', 'true');
  button.append(check);
  button.addEventListener('click', () => selectionMode ? togglePhotoSelection(photo.id, button) : openPhoto(photo.id));
  return button;
}

async function loadPhotos(generation = galleryGeneration) {
  if (loadingPhotos || !hasMorePhotos) return;
  loadingPhotos = true;
  loadMore.disabled = true;
  loadMore.textContent = 'Loading…';
  const query = new URLSearchParams({ limit: pageSize });
  if (nextCursor) query.set('cursor', nextCursor);
  if (ownerView) query.set('view', ownerView);
  if (activeCollection && activeCollection !== 'deleted') query.set('collection', activeCollection);
  if (activePeriod && activeCollection !== 'deleted') query.set('period', activePeriod);
  const endpoint = activeCollection === 'deleted' ? '/api/photos/deleted' : '/api/photos';
  try {
  const result = await api(`${endpoint}?${query}`);
  if (generation !== galleryGeneration) return;
  if (Number.isInteger(result.total)) totalPhotos = result.total;
  emptyState.hidden = totalPhotos !== 0;
  emptyState.querySelector('h2').textContent = activeCollection === 'deleted' ? 'Recently Deleted is empty.' : activeCollection ? 'Nothing in this collection yet.' : 'Your favorite moments belong here.';
  emptyState.querySelector('p').textContent = activeCollection === 'deleted' ? 'Deleted photos stay here for 30 days before being removed forever.' : activeCollection ? 'Open a photo and tap Collections to add it here.' : 'Add a handful or a whole camera roll. We’ll arrange everything by date.';
  emptyState.querySelector('[data-upload]').hidden = Boolean(activeCollection);
  result.photos.forEach((photo) => {
    const previousMonth = loadedPhotos.at(-1)?.captured_at?.slice(0, 7);
    const currentMonth = photo.captured_at?.slice(0, 7);
    if (currentMonth && currentMonth !== previousMonth) {
      gallery.append(monthHeading(currentMonth));
    }
    loadedPhotos.push(photo);
    gallery.append(photoCard(photo));
  });
  nextCursor = result.next_cursor || null;
  hasMorePhotos = Boolean(result.has_more && nextCursor);
  loadMore.hidden = !hasMorePhotos;
  if (!activeCollection) document.querySelector('#allPhotoCount').textContent = `${totalPhotos} item${totalPhotos === 1 ? '' : 's'}`;
  if (activeCollection === 'deleted') document.querySelector('#deletedPhotoCount').textContent = `${totalPhotos} item${totalPhotos === 1 ? '' : 's'}`;
  } finally {
    loadingPhotos = false;
    loadMore.disabled = false;
    loadMore.textContent = 'Show more';
  }
}

async function refreshPhotos() {
  galleryGeneration += 1;
  const generation = galleryGeneration;
  nextCursor = null;
  hasMorePhotos = true;
  loadingPhotos = false;
  loadedPhotos = [];
  totalPhotos = 0;
  gallery.innerHTML = '';
  await loadPhotos(generation);
  updateCollectionToolbar();
}

function captureGalleryPosition() {
  const cards = [...gallery.querySelectorAll('.photo')];
  const anchor = cards.find((card) => card.getBoundingClientRect().bottom > 0);
  return {
    id: anchor?.dataset.photoId || null,
    top: anchor?.getBoundingClientRect().top || 0,
    scrollY: window.scrollY,
  };
}

function restoreGalleryPosition(position) {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const anchor = position.id
      ? gallery.querySelector(`[data-photo-id="${CSS.escape(position.id)}"]`)
      : null;
    if (anchor) window.scrollBy(0, anchor.getBoundingClientRect().top - position.top);
    else window.scrollTo(0, position.scrollY);
  }));
}

function removeLoadedPhotos(ids) {
  const removed = new Set(ids);
  loadedPhotos = loadedPhotos.filter((photo) => !removed.has(photo.id));
  removed.forEach((id) => gallery.querySelector(`[data-photo-id="${CSS.escape(id)}"]`)?.remove());
  totalPhotos = Math.max(0, totalPhotos - removed.size);
  emptyState.hidden = totalPhotos !== 0;
}

function collectionCard(collection) {
  const button = document.createElement('button');
  button.className = `collection-card dynamic${activeCollection === collection.id ? ' selected' : ''}`;
  button.type = 'button';
  button.dataset.collection = collection.id;
  const cover = document.createElement('span');
  cover.className = 'collection-cover';
  if (collection.cover) {
    const image = document.createElement('img');
    image.src = collection.cover;
    image.alt = '';
    cover.append(image);
  } else {
    cover.classList.add('empty-cover');
  }
  const name = document.createElement('strong');
  name.textContent = collection.name;
  const count = document.createElement('small');
  count.textContent = `${collection.visibility === 'private' ? 'Only me · ' : ''}${collection.photo_count} item${collection.photo_count === 1 ? '' : 's'}`;
  button.append(cover, name, count);
  button.addEventListener('click', () => selectCollection(collection.id));
  return button;
}

async function loadCollections() {
  const result = await api(`/api/collections?view=${encodeURIComponent(ownerView)}`);
  collections = result.collections;
  if (activeCollection && activeCollection !== 'deleted' && !collections.some((item) => item.id === activeCollection)) {
    activeCollection = '';
  }
  collectionRail.querySelectorAll('.collection-card.dynamic').forEach((item) => item.remove());
  const deletedCard = collectionRail.querySelector('[data-collection="deleted"]');
  collections.forEach((collection) => collectionRail.insertBefore(collectionCard(collection), deletedCard));
  const picker = document.querySelector('#collectionSelect');
  picker.innerHTML = '<option value="">All media</option>';
  collections.forEach((collection) => {
    const option = document.createElement('option');
    option.value = collection.id;
    option.textContent = collection.name;
    picker.append(option);
  });
  const deletedOption = document.createElement('option');
  deletedOption.value = 'deleted';
  deletedOption.textContent = 'Recently deleted';
  picker.append(deletedOption);
  picker.value = activeCollection;
  collectionRail.querySelectorAll('[data-collection]').forEach((item) => item.classList.toggle('selected', item.dataset.collection === activeCollection));
  api(`/api/photos/deleted?limit=1&view=${encodeURIComponent(ownerView)}`).then((result) => {
    deletedPhotoTotal = result.total;
    document.querySelector('#deletedPhotoCount').textContent = result.total ? `${result.total} item${result.total === 1 ? '' : 's'}` : 'Kept for 30 days';
    updateCollectionToolbar();
  }).catch(() => {});
  updateCollectionToolbar();
}

collectionRail.querySelectorAll('.collection-card:not(.dynamic)').forEach((card) => {
  card.addEventListener('click', () => selectCollection(card.dataset.collection || ''));
});

async function selectCollection(id) {
  exitSelectionMode();
  activeCollection = id;
  activePeriod = '';
  collectionRail.querySelectorAll('[data-collection]').forEach((item) => {
    const selected = item.dataset.collection === id;
    item.classList.toggle('selected', selected);
    if (selected) item.setAttribute('aria-current', 'true'); else item.removeAttribute('aria-current');
  });
  document.querySelector('#collectionSelect').value = id;
  const selectedCard = collectionRail.querySelector(`[data-collection="${CSS.escape(id)}"]`);
  if (selectedCard) selectedCard.scrollIntoView({behavior: 'smooth', inline: 'center', block: 'nearest'});
  updateCollectionToolbar();
  await Promise.all([refreshPhotos(), loadTimeline()]);
}

function navigateCollection(direction) {
  const cards = [...collectionRail.querySelectorAll('[data-collection]')];
  if (!cards.length) return;
  const current = Math.max(cards.findIndex((card) => card.dataset.collection === activeCollection), 0);
  const next = (current + direction + cards.length) % cards.length;
  selectCollection(cards[next].dataset.collection);
}

document.querySelector('#previousCollection').addEventListener('click', () => navigateCollection(-1));
document.querySelector('#nextCollection').addEventListener('click', () => navigateCollection(1));
document.querySelector('#collectionSelect').addEventListener('change', (event) => selectCollection(event.target.value));

function updateCollectionToolbar() {
  const selected = collections.find((collection) => collection.id === activeCollection);
  const allActions = document.querySelector('#allPhotoActions');
  const deletedActions = document.querySelector('#deletedCollectionActions');
  const customActions = document.querySelector('#customCollectionActions');
  collectionToolbar.hidden = false;
  allActions.hidden = activeCollection !== '';
  deletedActions.hidden = activeCollection !== 'deleted';
  customActions.hidden = !selected;
  document.querySelector('#selectedCollectionName').textContent =
    activeCollection === '' ? 'All media' : activeCollection === 'deleted' ? 'Recently deleted' : selected?.name || 'Collection';
  document.querySelector('#selectFromAll').disabled = activeCollection === '' && totalPhotos === 0;
  document.querySelector('#selectDeleted').disabled = activeCollection === 'deleted' && totalPhotos === 0;
  document.querySelector('#restoreAllDeleted').disabled = deletedPhotoTotal === 0;
  document.querySelector('#emptyDeleted').disabled = deletedPhotoTotal === 0;
  if (selected) document.querySelector('#collectionPrivacy').textContent = selected.visibility === 'private' ? 'Share' : 'Only me';
}

function showToast(text) {
  photoToast.textContent = text;
  photoToast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { photoToast.hidden = true; }, 2800);
}

function openCollectionEditor(mode) {
  editorMode = mode;
  collectionMessage.textContent = '';
  const selected = collections.find((collection) => collection.id === activeCollection);
  document.querySelector('#collectionEditorTitle').textContent = mode === 'rename' ? 'Rename collection' : 'New collection';
  document.querySelector('#saveCollection').textContent = mode === 'rename' ? 'Save name' : 'Create collection';
  collectionName.value = mode === 'rename' && selected ? selected.name : '';
  document.querySelector('#collectionVisibility').value = mode === 'rename' && selected ? selected.visibility : 'shared';
  collectionEditor.showModal();
  setTimeout(() => collectionName.focus(), 50);
}

document.querySelector('#newCollection').addEventListener('click', () => openCollectionEditor('create'));
document.querySelector('#renameCollection').addEventListener('click', () => openCollectionEditor('rename'));

async function openSlideshow() {
  slideshowMessage.textContent = 'Loading your media collections…';
  slideshowProgress.hidden = true;
  document.querySelector('#createSlideshow').disabled = true;
  slideshowSheet.showModal();
  try {
    const result = await api('/api/slideshows/options');
    slideshowCollection.innerHTML = '';
    result.collections.forEach((collection) => {
      const option = document.createElement('option');
      option.value = collection.id;
      const parts = [];
      if (collection.image_count) parts.push(`${collection.image_count} photo${collection.image_count === 1 ? '' : 's'}`);
      if (collection.video_count) parts.push(`${collection.video_count} video${collection.video_count === 1 ? '' : 's'}`);
      option.textContent = `${collection.name} · ${parts.join(', ')}`;
      slideshowCollection.append(option);
    });
    slideshowMusicTracks = result.music || [];
    slideshowMusic.innerHTML = '<option value="">No background music</option>';
    slideshowMusicTracks.forEach((track) => {
      const option = document.createElement('option');
      option.value = track.id;
      option.textContent = `${track.title} — ${track.artist}`;
      slideshowMusic.append(option);
    });
    slideshowMusic.value = '';
    slideshowMusicPreview.pause();
    slideshowMusicPreview.removeAttribute('src');
    slideshowMusicPreview.hidden = true;
    slideshowMusicCredit.textContent = 'Choose a track, then preview it before creating the video.';
    if (result.collections.some((collection) => collection.id === activeCollection)) slideshowCollection.value = activeCollection;
    slideshowMessage.textContent = result.collections.length ? '' : 'Create a collection with at least one photo or video first.';
    document.querySelector('#createSlideshow').disabled = !result.collections.length;
  } catch (error) {
    slideshowMessage.textContent = error.message;
  }
}

function setSlideshowBusy(busy) {
  document.querySelector('#createSlideshow').disabled = busy;
  document.querySelector('#cancelSlideshow').disabled = busy;
  document.querySelector('#closeSlideshow').disabled = busy;
  slideshowCollection.disabled = busy;
  document.querySelector('#slideshowDuration').disabled = busy;
  document.querySelector('#slideshowLayout').disabled = busy;
  document.querySelector('#slideshowTransition').disabled = busy;
  slideshowMusic.disabled = busy;
  slideshowMusicPreview.controls = !busy;
  document.querySelector('#slideshowLoop').disabled = busy;
}

slideshowMusic.addEventListener('change', () => {
  const track = slideshowMusicTracks.find((item) => item.id === slideshowMusic.value);
  slideshowMusicPreview.pause();
  if (!track) {
    slideshowMusicPreview.removeAttribute('src');
    slideshowMusicPreview.hidden = true;
    slideshowMusicCredit.textContent = 'Choose a track, then preview it before creating the video.';
    return;
  }
  slideshowMusicPreview.src = track.preview_url;
  slideshowMusicPreview.hidden = false;
  slideshowMusicCredit.textContent = `${track.title} by ${track.artist} · ${track.license}`;
});

async function watchSlideshow(jobId) {
  while (true) {
    const job = await api(`/api/slideshows/${jobId}`);
    slideshowProgressBar.style.width = `${Math.max(5, job.progress || 0)}%`;
    slideshowMessage.textContent = job.message || 'Creating your video…';
    if (job.status === 'completed') {
      setSlideshowBusy(false);
      slideshowSheet.close();
      await loadCollections();
      const videos = collections.find((collection) => collection.name.toLowerCase() === 'videos');
      if (videos) await selectCollection(videos.id);
      showToast('Your slideshow was saved in Videos.');
      return;
    }
    if (job.status === 'failed') {
      setSlideshowBusy(false);
      slideshowMessage.textContent = `${job.error || 'The slideshow could not be created.'} Nothing was removed.`;
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
}

document.querySelector('#openSlideshow').addEventListener('click', openSlideshow);
function closeSlideshow() {
  slideshowMusicPreview.pause();
  slideshowSheet.close();
}
document.querySelector('#closeSlideshow').addEventListener('click', closeSlideshow);
document.querySelector('#cancelSlideshow').addEventListener('click', closeSlideshow);
document.querySelector('#createSlideshow').addEventListener('click', async () => {
  const button = document.querySelector('#createSlideshow');
  setSlideshowBusy(true);
  button.textContent = 'Creating…';
  slideshowProgress.hidden = false;
  slideshowProgressBar.style.width = '5%';
  slideshowMessage.textContent = 'Getting your media ready…';
  try {
    const result = await api('/api/slideshows', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        collection_id: slideshowCollection.value,
        duration_seconds: Number(document.querySelector('#slideshowDuration').value),
        layout: document.querySelector('#slideshowLayout').value,
        transition: document.querySelector('#slideshowTransition').value,
        music_id: slideshowMusic.value,
        loop_playback: document.querySelector('#slideshowLoop').checked,
      }),
    });
    await watchSlideshow(result.id);
  } catch (error) {
    setSlideshowBusy(false);
    slideshowMessage.textContent = `${error.message} Nothing was changed.`;
  } finally {
    button.textContent = 'Create video';
  }
});
document.querySelector('#createFromOrganize').addEventListener('click', () => {
  if (organizerSaving) return;
  resumeOrganizer = { ids: [...organizeIds], desired: new Map(organizeDesired), focus: organizerReturnFocus };
  organizerGeneration += 1;
  hideOrganizerSheet();
  openCollectionEditor('organize-create');
});

document.querySelector('#saveCollection').addEventListener('click', async () => {
  const name = collectionName.value.trim();
  if (!name) { collectionMessage.textContent = 'Give the collection a name.'; return; }
  const button = document.querySelector('#saveCollection');
  button.disabled = true;
  try {
    if (editorMode === 'rename') {
      await api(`/api/collections/${activeCollection}`, { method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name, visibility:document.querySelector('#collectionVisibility').value}) });
    } else {
      const created = await api('/api/collections', { method: 'POST', headers: {'Content-Type':'application/json'}, body:JSON.stringify({name,visibility:document.querySelector('#collectionVisibility').value}) });
      await loadCollections();
      collectionEditor.close();
      if (editorMode === 'organize-create' && resumeOrganizer) {
        const pending = resumeOrganizer;
        resumeOrganizer = null;
        await openOrganizer(pending.ids, pending.desired, created.id, pending.focus);
        return;
      }
      activeCollection = created.id;
    }
    collectionEditor.close();
    await loadCollections();
    await selectCollection(activeCollection);
  } catch (error) {
    collectionMessage.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

collectionName.addEventListener('keydown', (event) => { if (event.key === 'Enter') document.querySelector('#saveCollection').click(); });

async function cancelCollectionEditor() {
  collectionEditor.close();
  if (editorMode === 'organize-create' && resumeOrganizer) {
    const pending = resumeOrganizer;
    resumeOrganizer = null;
    await openOrganizer(pending.ids, pending.desired, null, pending.focus);
  }
}

document.querySelector('#closeCollectionEditor').addEventListener('click', cancelCollectionEditor);
collectionEditor.addEventListener('cancel', (event) => { event.preventDefault(); cancelCollectionEditor(); });

function askConfirmation({title, text, action, button = 'Delete'}) {
  document.querySelector('#confirmTitle').textContent = title;
  document.querySelector('#confirmText').textContent = text;
  document.querySelector('#acceptConfirm').textContent = button;
  confirmAction = action;
  confirmSheet.showModal();
}

document.querySelector('#cancelConfirm').addEventListener('click', () => confirmSheet.close());
document.querySelector('#acceptConfirm').addEventListener('click', async () => {
  const button = document.querySelector('#acceptConfirm');
  button.disabled = true;
  try {
    if (confirmAction) await confirmAction();
    confirmSheet.close();
  } catch (error) {
    document.querySelector('#confirmText').textContent = `${error.message} Nothing was changed.`;
  }
  finally { button.disabled = false; confirmAction = null; }
});

document.querySelector('#deleteCollection').addEventListener('click', () => {
  const selected = collections.find((collection) => collection.id === activeCollection);
  if (!selected) return;
  askConfirmation({
    title: `Delete “${selected.name}”?`,
    text: 'The collection will disappear, but every item inside it will remain safely in All media.',
    action: async () => {
      await api(`/api/collections/${selected.id}`, {method: 'DELETE'});
      activeCollection = '';
      await loadCollections();
      await refreshPhotos();
    },
  });
});

function updateSelectionBar() {
  const bar = document.querySelector('#selectionBar');
  bar.hidden = !selectionMode;
  document.querySelector('#selectionCount').textContent = `${selectedIds.size} selected`;
  document.querySelector('#batchOrganize').hidden = activeCollection === 'deleted';
  document.querySelector('#batchPrivacy').hidden = activeCollection === 'deleted';
  document.querySelector('#batchShare').hidden = activeCollection === 'deleted';
  document.querySelector('#batchRestore').hidden = activeCollection !== 'deleted';
  document.querySelector('#batchDelete').textContent = activeCollection === 'deleted' ? 'Delete forever' : 'Delete';
  document.querySelector('#toggleSelect').textContent = selectionMode ? 'Cancel' : 'Select';
  document.body.classList.toggle('selecting', selectionMode);
}

function togglePhotoSelection(photoId, card) {
  if (rangeSelectionStart === -1) {
    rangeSelectionStart = loadedPhotos.findIndex((photo) => photo.id === photoId);
    selectedIds.add(photoId);
    card.classList.add('chosen');
    card.setAttribute('aria-pressed', 'true');
    document.querySelector('#selectMediaRange').textContent = 'Tap end';
    showToast('Now tap the last item in the range.');
    updateSelectionBar();
    return;
  }
  if (rangeSelectionStart !== null) {
    const end = loadedPhotos.findIndex((photo) => photo.id === photoId);
    if (end >= 0) {
      const [first, last] = [rangeSelectionStart, end].sort((a, b) => a - b);
      loadedPhotos.slice(first, last + 1).forEach((photo) => selectedIds.add(photo.id));
      gallery.querySelectorAll('.photo').forEach((item) => {
        const chosen = selectedIds.has(item.dataset.photoId);
        item.classList.toggle('chosen', chosen);
        item.setAttribute('aria-pressed', chosen ? 'true' : 'false');
      });
      rangeSelectionStart = null;
      document.querySelector('#selectMediaRange').textContent = 'Select range';
      showToast(`${last - first + 1} items selected in that range.`);
      updateSelectionBar();
      return;
    }
  }
  if (selectedIds.has(photoId)) selectedIds.delete(photoId); else selectedIds.add(photoId);
  card.classList.toggle('chosen', selectedIds.has(photoId));
  card.setAttribute('aria-pressed', selectedIds.has(photoId) ? 'true' : 'false');
  updateSelectionBar();
}

function exitSelectionMode() {
  selectionMode = false;
  rangeSelectionStart = null;
  document.querySelector('#selectMediaRange').textContent = 'Select range';
  selectedIds.clear();
  gallery.querySelectorAll('.photo.chosen').forEach((card) => card.classList.remove('chosen'));
  updateSelectionBar();
}

document.querySelector('#toggleSelect').addEventListener('click', () => {
  if (selectionMode) exitSelectionMode();
  else { selectionMode = true; updateSelectionBar(); }
});

function beginSelection() {
  if (!selectionMode) {
    selectionMode = true;
    updateSelectionBar();
  }
  gallery.scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function loadSelectionLimit() {
  const button = document.querySelector('#selectAllMedia');
  button.disabled = true;
  try {
    while (
      loadedPhotos.length < totalPhotos
      && loadedPhotos.length < 500
      && hasMorePhotos
    ) await loadPhotos();
  } finally {
    button.disabled = false;
  }
}

document.querySelector('#selectAllMedia').addEventListener('click', async () => {
  await loadSelectionLimit();
  loadedPhotos.slice(0, 500).forEach((photo) => selectedIds.add(photo.id));
  gallery.querySelectorAll('.photo').forEach((card) => {
    const chosen = selectedIds.has(card.dataset.photoId);
    card.classList.toggle('chosen', chosen);
    card.setAttribute('aria-pressed', chosen ? 'true' : 'false');
  });
  updateSelectionBar();
  showToast(totalPhotos > 500 ? 'The first 500 items are selected. Work with those, then select the next group.' : `${selectedIds.size} items selected.`);
});

document.querySelector('#selectMediaRange').addEventListener('click', async () => {
  const button = document.querySelector('#selectMediaRange');
  if (rangeSelectionStart !== null) {
    rangeSelectionStart = null;
    button.textContent = 'Select range';
    showToast('Range selection cancelled.');
    return;
  }
  await loadSelectionLimit();
  if (!loadedPhotos.length) return;
  rangeSelectionStart = -1;
  button.textContent = 'Tap start';
  showToast('Tap the first item in the range.');
});

document.querySelector('#addFromAll').addEventListener('click', openUpload);
document.querySelector('#selectFromAll').addEventListener('click', beginSelection);
document.querySelector('#selectDeleted').addEventListener('click', beginSelection);
document.querySelector('#restoreAllDeleted').addEventListener('click', () => {
  if (!deletedPhotoTotal) return;
  const count = deletedPhotoTotal;
  askConfirmation({
    title: `Restore ${count} item${count === 1 ? '' : 's'}?`,
    text: 'They will return to All media and to their existing collections.',
    button: 'Restore all',
    action: async () => {
      const result = await api('/api/photos/restore-all', {method: 'POST'});
      await refreshPhotos();
      await loadCollections();
      showToast(`${result.count} item${result.count === 1 ? '' : 's'} restored.`);
    },
  });
});
document.querySelector('#emptyDeleted').addEventListener('click', () => {
  if (!deletedPhotoTotal) return;
  const count = deletedPhotoTotal;
  askConfirmation({
    title: `Delete ${count} item${count === 1 ? '' : 's'} forever?`,
    text: 'Every photo in Recently Deleted will be permanently removed from David-Pi. This cannot be undone.',
    button: 'Delete forever',
    action: async () => {
      const result = await api('/api/photos/purge-all', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({confirmation: 'empty-recently-deleted'}),
      });
      await refreshPhotos();
      await loadCollections();
      showToast(`${result.count} item${result.count === 1 ? '' : 's'} permanently deleted.`);
    },
  });
});

function organizationChanges() {
  const changes = [];
  organizeDesired.forEach((desired, collectionId) => {
    const original = organizeBaseline.get(collectionId);
    if (desired === original || desired === 'mixed') return;
    changes.push({ collection_id: collectionId, action: desired === 'all' ? 'add' : 'remove' });
  });
  return changes;
}

function updateOrganizerSave() {
  saveOrganization.disabled = organizerSaving || organizationChanges().length === 0;
}

function organizerCollectionFallback() {
  const merged = new Map();
  serverOrganizerCatalog.forEach((collection) => merged.set(collection.id, collection));
  try {
    const serverCatalog = JSON.parse(document.querySelector('#organizerCollectionCatalog')?.textContent || '[]');
    serverCatalog.forEach((collection) => merged.set(String(collection.id), collection));
  } catch (_error) {}
  collections.forEach((collection) => merged.set(collection.id, collection));
  // The collection picker and rail are already filtered by the server for the
  // current viewer. Reading them gives Android WebView a second, independent
  // source when its organizer request is restored from an empty/stale cache.
  document.querySelectorAll('#collectionSelect option').forEach((option) => {
    if (!option.value || option.value === 'deleted') return;
    if (!merged.has(option.value)) merged.set(option.value, {id: option.value, name: option.textContent});
  });
  collectionRail.querySelectorAll('.collection-card.dynamic[data-collection]').forEach((card) => {
    const id = card.dataset.collection;
    const name = card.querySelector('strong')?.textContent?.trim();
    if (id && name && !merged.has(id)) merged.set(id, {id, name});
  });
  return [...merged.values()];
}

function renderOrganizerCollections(membershipCollections) {
  const liveChecks = organizeSheet.querySelector('#collectionChecks') || collectionChecks;
  const catalog = membershipCollections.length ? membershipCollections : organizerCollectionFallback();
  const existingRows = new Map(
    [...liveChecks.querySelectorAll('.collection-check')].map((label) => [
      String(label.dataset.collectionId || label.querySelector('input')?.dataset.collectionId || ''),
      label,
    ]),
  );
  liveChecks.querySelectorAll('.loading-copy').forEach((item) => item.remove());
  if (!catalog.length) {
    const empty = document.createElement('p');
    empty.className = 'loading-copy';
    empty.textContent = 'No collections yet. Create your first one below.';
    liveChecks.append(empty);
    return;
  }
  catalog.forEach((collection) => {
    const id = String(collection.id);
    let label = existingRows.get(id);
    let checkbox;
    let name;
    if (label) {
      existingRows.delete(id);
      checkbox = label.querySelector('input');
      name = label.querySelector('span');
    } else {
      label = document.createElement('label');
      label.className = 'collection-check';
      label.dataset.collectionId = id;
      checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      name = document.createElement('span');
      label.append(checkbox, name);
      liveChecks.append(label);
    }
    const desired = organizeDesired.get(collection.id);
    checkbox.checked = desired === 'all';
    checkbox.indeterminate = desired === 'mixed';
    checkbox.dataset.collectionId = id;
    name.textContent = collection.name;
    checkbox.onchange = () => {
      organizeDesired.set(id, checkbox.checked ? 'all' : 'none');
      updateOrganizerSave();
    };
  });
  // Never remove a server-rendered row here. The page response is already the
  // visibility-filtered catalog. A late, partial WebView/API response must not
  // erase collections that the server authorized and rendered.
}

function ensureOrganizerCollectionsVisible() {
  if (!organizerIsOpen() || !organizerCatalog.length) return;
  const liveChecks = organizeSheet.querySelector('#collectionChecks') || collectionChecks;
  if (liveChecks.querySelectorAll('.collection-check').length === organizerCatalog.length) return;
  renderOrganizerCollections(organizerCatalog);
}

function startOrganizerWatchdog() {
  if (organizerWatchdog) organizerWatchdog.disconnect();
  organizerWatchdog = new MutationObserver(() => ensureOrganizerCollectionsVisible());
  organizerWatchdog.observe(organizeSheet, {childList: true, subtree: true});
  requestAnimationFrame(ensureOrganizerCollectionsVisible);
  setTimeout(ensureOrganizerCollectionsVisible, 150);
  setTimeout(ensureOrganizerCollectionsVisible, 700);
}

function organizerRoot() {
  return organizeSheet;
}

function organizerIsOpen() {
  return !organizeSheet.hidden;
}

function showOrganizerSheet() {
  if (organizerBackdrop.parentElement !== document.body) document.body.append(organizerBackdrop);
  if (organizeSheet.parentElement !== document.body) document.body.append(organizeSheet);
  organizerBackdrop.hidden = false;
  organizeSheet.hidden = false;
  const viewport = window.visualViewport;
  const height = Math.max(420, Math.round((viewport?.height || window.innerHeight) - 32));
  const width = Math.max(280, Math.min(500, Math.round((viewport?.width || window.innerWidth) - 24)));
  // Android System WebView can resolve a vh-based max-height to 0 after this
  // sheet is reparented into document.body. The authorized collection rows
  // remain in the DOM, but the zero-height grid paints underneath the buttons.
  // Resolve the limit to pixels from the live visual viewport instead. This is
  // also valid in ordinary browsers and keeps large collection lists scrollable.
  const collectionListHeight = Math.max(140, Math.round((viewport?.height || window.innerHeight) * 0.4));
  collectionChecks.style.setProperty('max-height', `${collectionListHeight}px`, 'important');
  organizeSheet.style.setProperty('position', 'fixed', 'important');
  organizeSheet.style.setProperty('z-index', '2147483000', 'important');
  organizeSheet.style.setProperty('top', `${Math.round((viewport?.offsetTop || 0) + 16)}px`, 'important');
  organizeSheet.style.setProperty('left', '50%', 'important');
  organizeSheet.style.setProperty('bottom', 'auto', 'important');
  organizeSheet.style.setProperty('width', `${width}px`, 'important');
  organizeSheet.style.setProperty('height', `${height}px`, 'important');
  organizeSheet.style.setProperty('min-height', '0', 'important');
  organizeSheet.style.setProperty('max-height', `${height}px`, 'important');
  organizeSheet.style.setProperty('margin', '0', 'important');
  organizeSheet.style.setProperty('transform', 'translateX(-50%)', 'important');
  organizeSheet.style.setProperty('overflow-y', 'auto', 'important');
  document.documentElement.classList.add('organizer-is-open');
  requestAnimationFrame(() => document.querySelector('#closeOrganizer')?.focus({preventScroll: true}));
}

function hideOrganizerSheet() {
  organizeSheet.hidden = true;
  organizerBackdrop.hidden = true;
  document.documentElement.classList.remove('organizer-is-open');
  if (organizerWatchdog) organizerWatchdog.disconnect();
}

async function openOrganizer(ids = null, previousDesired = null, selectCollectionId = null, returnFocus = null) {
  organizerGeneration += 1;
  const generation = organizerGeneration;
  organizeIds = ids || (currentPhotoIndex >= 0 ? [loadedPhotos[currentPhotoIndex].id] : []);
  if (!organizeIds.length) return;
  organizerReturnFocus = returnFocus || document.activeElement;
  const batch = organizeIds.length > 1 || selectionMode;
  organizeMessage.textContent = '';
  organizerSaving = false;
  saveOrganization.textContent = 'Save changes';
  saveOrganization.disabled = true;
  document.querySelector('#createFromOrganize').disabled = true;
  document.querySelector('#organizeTitle').textContent = batch ? `Organize ${organizeIds.length} items` : 'Organize media';
  document.querySelector('#organizeCopy').textContent = 'Choose where the selected media belongs, then save your changes.';
  const immediateCatalog = organizerCollectionFallback().map((collection) => ({
    ...collection, state: collection.id === activeCollection ? 'all' : 'none',
  }));
  organizerCatalog = immediateCatalog;
  organizeBaseline = new Map(immediateCatalog.map((collection) => [collection.id, collection.state]));
  organizeDesired = new Map(organizeBaseline);
  renderOrganizerCollections(immediateCatalog);
  showOrganizerSheet();
  startOrganizerWatchdog();
  try {
    const result = await api('/api/collections/membership-state', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids: organizeIds}),
    });
    if (generation !== organizerGeneration) return;
    // The collection rail is already an authorized list for this viewer. Keep
    // it as a defensive fallback if a WebView/proxy response ever arrives
    // without membership rows, rather than presenting an empty organizer.
    let authorizedCollections = Array.isArray(result.collections) ? result.collections : [];
    if (!authorizedCollections.length) {
      const listings = await Promise.allSettled([
        api(`/api/collections?view=${encodeURIComponent(ownerView)}`),
        api('/api/collections?view=mine'),
      ]);
      const merged = new Map();
      listings.forEach((listing) => {
        if (listing.status !== 'fulfilled') return;
        (listing.value.collections || []).forEach((collection) => merged.set(collection.id, collection));
      });
      authorizedCollections = [...merged.values()];
    }
    const fallbackCollections = organizerCollectionFallback();
    const collectionCatalog = new Map(fallbackCollections.map((collection) => [collection.id, collection]));
    authorizedCollections.forEach((collection) => collectionCatalog.set(collection.id, collection));
    const membershipCollections = [...collectionCatalog.values()].length
      ? [...collectionCatalog.values()].map((collection) => ({
          ...collection,
          state: collection.state || (collection.id === activeCollection ? 'all' : 'none'),
        }))
      : fallbackCollections.map((collection) => ({
          id: collection.id,
          name: collection.name,
          state: collection.id === activeCollection ? 'all' : 'none',
        }));
    organizeBaseline = new Map(membershipCollections.map((collection) => [collection.id, collection.state]));
    organizeDesired = new Map(membershipCollections.map((collection) => [collection.id, previousDesired?.get(collection.id) || collection.state]));
    if (selectCollectionId) organizeDesired.set(selectCollectionId, 'all');
    organizerCatalog = membershipCollections;
    renderOrganizerCollections(membershipCollections);
    // Re-render against the element that is actually mounted after the sheet is
    // promoted. This is deliberately redundant: Android System WebView has
    // intermittently discarded children during the move to document.body.
    requestAnimationFrame(() => ensureOrganizerCollectionsVisible());
    document.querySelector('#createFromOrganize').disabled = false;
    updateOrganizerSave();
  } catch (error) {
    if (generation !== organizerGeneration) return;
    if (immediateCatalog.length) {
      organizerCatalog = immediateCatalog;
      renderOrganizerCollections(immediateCatalog);
      organizeMessage.textContent = 'Collections loaded. Existing membership could not be checked; review selections before saving.';
    } else {
      renderOrganizerCollections([]);
      organizeMessage.textContent = 'Collections could not be loaded. Please close this window and try again.';
    }
    document.querySelector('#createFromOrganize').disabled = false;
  }
}

document.querySelector('#organizePhoto').addEventListener('click', () => openOrganizer());
document.querySelector('#batchOrganize').addEventListener('click', () => openOrganizer([...selectedIds]));

function closeOrganizer() {
  if (organizerSaving) return;
  organizerGeneration += 1;
  hideOrganizerSheet();
  organizeIds = [];
  organizeBaseline.clear();
  organizeDesired.clear();
  const focus = organizerReturnFocus;
  organizerReturnFocus = null;
  if (focus && document.contains(focus)) setTimeout(() => focus.focus(), 0);
}

document.querySelector('#closeOrganizer').addEventListener('click', closeOrganizer);
document.querySelector('#cancelOrganizer').addEventListener('click', closeOrganizer);
organizerBackdrop.addEventListener('click', closeOrganizer);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && organizerIsOpen()) {
    event.preventDefault();
    closeOrganizer();
  }
});

saveOrganization.addEventListener('click', async () => {
  const changes = organizationChanges();
  if (!changes.length || organizerSaving) return;
  const savedPosition = captureGalleryPosition();
  const savedOrganizeIds = [...organizeIds];
  const returnFocus = organizerReturnFocus;
  organizerSaving = true;
  saveOrganization.disabled = true;
  saveOrganization.textContent = 'Saving…';
  organizerRoot().querySelectorAll('input, button').forEach((control) => { if (control !== saveOrganization) control.disabled = true; });
  try {
    const result = await api('/api/collections/membership', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids: organizeIds, changes}),
    });
    let confirmation = 'Collection changes saved.';
    if (changes.length === 1) {
      const collection = collections.find((item) => item.id === changes[0].collection_id);
      const count = changes[0].action === 'add' ? result.added : result.removed;
      const verb = changes[0].action === 'add' ? 'added to' : 'removed from';
      if (collection) confirmation = `${count} item${count === 1 ? '' : 's'} ${verb} ${collection.name}.`;
    }
    hideOrganizerSheet();
    organizerGeneration += 1;
    organizeIds = [];
    organizeBaseline.clear();
    organizeDesired.clear();
    await loadCollections();
    const removedFromOpenCollection = activeCollection && activeCollection !== 'deleted'
      && changes.some((change) => change.collection_id === activeCollection && change.action === 'remove');
    if (removedFromOpenCollection) {
      if (viewer.open) viewer.close();
      removeLoadedPhotos(savedOrganizeIds);
      updateCollectionToolbar();
      if (hasMorePhotos) await loadPhotos();
    }
    if (selectionMode) exitSelectionMode();
    restoreGalleryPosition(savedPosition);
    showToast(confirmation);
    organizerReturnFocus = null;
    if (returnFocus && document.contains(returnFocus)) setTimeout(() => returnFocus.focus(), 0);
  } catch (error) {
    organizeMessage.textContent = `${error.message} Nothing was changed.`;
  } finally {
    organizerSaving = false;
    saveOrganization.textContent = 'Save changes';
    organizerRoot().querySelectorAll('input, button').forEach((control) => { control.disabled = false; });
    updateOrganizerSave();
  }
});

async function restore(ids) {
  await api('/api/photos/restore', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids})});
  if (viewer.open) viewer.close();
  exitSelectionMode();
  await refreshPhotos();
  await loadCollections();
}

document.querySelector('#restorePhoto').addEventListener('click', () => {
  if (currentPhotoIndex >= 0) restore([loadedPhotos[currentPhotoIndex].id]);
});
document.querySelector('#viewerPrivacy').addEventListener('click', async () => {
  if (currentPhotoIndex < 0) return;
  const privacyButton = document.querySelector('#viewerPrivacy');
  const photo = loadedPhotos[currentPhotoIndex];
  const visibility = photo.visibility === 'private' ? 'shared' : 'private';
  privacyButton.disabled = true;
  try {
    await api('/api/photos/visibility', {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:[photo.id],visibility})});
    photo.visibility = visibility;
    showToast(visibility === 'private' ? 'Media is now private.' : 'Media shared.');

    // "All" contains shared media only. Remove a newly private item locally so
    // the gallery stays truthful, but keep the viewer open on the next item.
    if (visibility === 'private' && ownerView === '') {
      const removedIndex = currentPhotoIndex;
      loadedPhotos.splice(removedIndex, 1);
      totalPhotos = Math.max(0, totalPhotos - 1);
      gallery.querySelector(`[data-photo-id="${CSS.escape(photo.id)}"]`)?.remove();
      if (!activeCollection) {
        document.querySelector('#allPhotoCount').textContent = `${totalPhotos} item${totalPhotos === 1 ? '' : 's'}`;
      }
      if (loadedPhotos.length < totalPhotos && hasMorePhotos) await loadPhotos();
      if (loadedPhotos.length) {
        showPhoto(Math.min(removedIndex, loadedPhotos.length - 1));
      } else {
        viewer.close();
        emptyState.hidden = totalPhotos !== 0;
      }
    } else {
      showPhoto(currentPhotoIndex);
    }
    loadCollections().catch(() => {});
  } catch (error) {
    showToast(error.message);
  } finally {
    privacyButton.disabled = false;
  }
});
document.querySelector('#batchPrivacy').addEventListener('click', async () => {
  const ids=[...selectedIds]; if(!ids.length)return;
  const savedPosition=captureGalleryPosition();
  await api('/api/photos/visibility',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids,visibility:'private'})});
  if(ownerView==='') removeLoadedPhotos(ids); else loadedPhotos.forEach((photo)=>{if(ids.includes(photo.id))photo.visibility='private';});
  showToast(`${ids.length} item${ids.length===1?' is':'s are'} now private.`); exitSelectionMode(); await loadCollections(); restoreGalleryPosition(savedPosition);
});
document.querySelector('#batchShare').addEventListener('click', async () => {
  const ids=[...selectedIds]; if(!ids.length)return;
  const savedPosition=captureGalleryPosition();
  await api('/api/photos/visibility',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids,visibility:'shared'})});
  loadedPhotos.forEach((photo)=>{if(ids.includes(photo.id))photo.visibility='shared';});
  showToast(`${ids.length} item${ids.length===1?' was':'s were'} shared.`); exitSelectionMode(); await loadCollections(); restoreGalleryPosition(savedPosition);
});
document.querySelector('#batchRestore').addEventListener('click', () => {
  if (selectedIds.size) restore([...selectedIds]);
});

document.querySelector('#deletePhoto').addEventListener('click', () => {
  if (currentPhotoIndex < 0) return;
  const photo = loadedPhotos[currentPhotoIndex];
  const permanent = activeCollection === 'deleted';
  askConfirmation({
    title: permanent ? 'Delete forever?' : 'Move to Recently Deleted?',
    text: permanent ? `“${photo.original_name}” will be permanently removed from the Pi. This cannot be undone.` : `“${photo.original_name}” can be restored for the next 30 days.`,
    button: permanent ? 'Delete forever' : 'Move photo',
    action: async () => {
      const result = await api(permanent ? '/api/photos/purge' : '/api/photos/trash', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids: [photo.id]})});
      if (result.count !== 1) throw new Error(permanent ? 'This item could not be permanently deleted.' : 'This item could not be moved.');
      viewer.close();
      await refreshPhotos();
      await loadCollections();
      showToast(permanent ? 'Item permanently deleted.' : 'Item moved to Recently Deleted.');
    },
  });
});

document.querySelector('#batchDelete').addEventListener('click', () => {
  const ids = [...selectedIds];
  if (!ids.length) return;
  const permanent = activeCollection === 'deleted';
  askConfirmation({
    title: permanent ? `Delete ${ids.length} items forever?` : `Delete ${ids.length} items?`,
    text: permanent ? 'These originals will be permanently removed from the Pi. This cannot be undone.' : 'They will remain in Recently Deleted for 30 days and can be restored.',
    button: permanent ? 'Delete forever' : 'Move items',
    action: async () => {
      const savedPosition = captureGalleryPosition();
      const result = await api(permanent ? '/api/photos/purge' : '/api/photos/trash', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids})});
      if (result.count !== ids.length) {
        const completed = Number(result.count || 0);
        throw new Error(`${completed} of ${ids.length} items were changed. Refresh and try the remaining items.`);
      }
      exitSelectionMode();
      removeLoadedPhotos(ids);
      if (hasMorePhotos) await loadPhotos();
      await loadCollections();
      restoreGalleryPosition(savedPosition);
      showToast(permanent ? `${ids.length} item${ids.length === 1 ? '' : 's'} permanently deleted.` : `${ids.length} item${ids.length === 1 ? '' : 's'} moved to Recently Deleted.`);
    },
  });
});

document.querySelector('#collectionPrivacy').addEventListener('click', async () => {
  const selected=collections.find((collection)=>collection.id===activeCollection); if(!selected)return;
  const visibility=selected.visibility==='private'?'shared':'private';
  await api(`/api/collections/${selected.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:selected.name,visibility})});
  showToast(visibility==='private'?'Collection is now private. Its existing media stayed unchanged.':'Collection shared.');
  await loadCollections(); updateCollectionToolbar();
});
document.querySelector('#mediaOwners').addEventListener('click',async(event)=>{
  const button=event.target.closest('[data-owner]'); if(!button)return;
  ownerView=button.dataset.owner||''; activePeriod=''; document.querySelectorAll('#mediaOwners [data-owner]').forEach((item)=>item.classList.toggle('selected',item===button));
  await loadCollections(); await Promise.all([refreshPhotos(),loadTimeline()]);
});

loadMore.addEventListener('click', () => loadPhotos());
const galleryLoader = new IntersectionObserver((entries) => {
  if (entries.some((entry) => entry.isIntersecting) && hasMorePhotos) loadPhotos();
}, {rootMargin: '700px 0px'});
galleryLoader.observe(loadMore);
let galleryScrollQueued = false;
window.addEventListener('scroll', () => {
  if (galleryScrollQueued) return;
  galleryScrollQueued = true;
  requestAnimationFrame(() => {
    galleryScrollQueued = false;
    if (!hasMorePhotos || loadingPhotos || loadMore.hidden) return;
    if (loadMore.getBoundingClientRect().top < window.innerHeight + 1000) loadPhotos();
  });
}, {passive: true});
document.querySelector('#closeViewer').addEventListener('click', () => viewer.close());
document.querySelector('#previousPhoto').addEventListener('click', () => movePhoto(-1));
document.querySelector('#nextPhoto').addEventListener('click', () => movePhoto(1));
viewer.addEventListener('click', (event) => { if (event.target === viewer) viewer.close(); });
viewer.addEventListener('close', () => {
  viewerLoadGeneration += 1;
  currentPhotoIndex = -1;
  viewerStage.classList.remove('loading');
  viewerImage.removeAttribute('src');
  stopViewerVideo({clearPoster: true});
  resetViewerZoom();
  resetViewerTouch();
});
viewerStage.addEventListener('touchstart', (event) => {
  if (!viewerImage.hidden && event.touches.length === 2) {
    pinchActive = true; pinchDistance = distanceBetween(event.touches); pinchZoom = zoom;
    event.preventDefault(); return;
  }
  if (event.touches.length !== 1) return;
  const touch = event.touches[0];
  touchStartX = touch.clientX; touchStartY = touch.clientY;
  touchSwipeActive = false;
  if (!viewerImage.hidden) {
    panStartX = touch.clientX; panStartY = touch.clientY; panOriginX = panX; panOriginY = panY;
  }
}, {passive:false, capture:true});
viewerStage.addEventListener('touchmove', (event) => {
  if (!viewerImage.hidden && event.touches.length === 2) {
    zoom = Math.max(1, Math.min(5, pinchZoom * distanceBetween(event.touches) / Math.max(1, pinchDistance)));
    if (zoom <= 1.01) { panX = 0; panY = 0; }
    applyViewerTransform(); event.preventDefault(); return;
  }
  if (!viewerImage.hidden && event.touches.length === 1 && zoom > 1.01) {
    const touch = event.touches[0];
    const maxX = viewerStage.clientWidth * (zoom - 1) / 2;
    const maxY = viewerStage.clientHeight * (zoom - 1) / 2;
    panX = Math.max(-maxX, Math.min(maxX, panOriginX + touch.clientX - panStartX));
    panY = Math.max(-maxY, Math.min(maxY, panOriginY + touch.clientY - panStartY));
    applyViewerTransform(); event.preventDefault();
    return;
  }
  if (event.touches.length === 1 && touchStartX !== null) {
    const touch = event.touches[0];
    const dx = touch.clientX - touchStartX;
    const dy = touch.clientY - touchStartY;
    if (Math.abs(dx) > 12 && Math.abs(dx) > Math.abs(dy) * 1.2) {
      touchSwipeActive = true;
      event.preventDefault();
    }
  }
}, {passive:false, capture:true});
viewerStage.addEventListener('touchend', (event) => {
  if (event.touches.length) return;
  if (zoom <= 1.01 && !pinchActive && touchStartX !== null) {
    const touch = event.changedTouches[0], dx = touch.clientX - touchStartX, dy = touch.clientY - touchStartY;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.25) {
      event.preventDefault();
      event.stopPropagation();
      movePhoto(dx > 0 ? -1 : 1);
    }
  }
  if (zoom <= 1.01) resetViewerZoom(true);
  resetViewerTouch();
}, {passive:false, capture:true});
viewerStage.addEventListener('touchcancel', resetViewerTouch, {capture:true});
viewerImage.addEventListener('dblclick', () => {
  if (zoom > 1.01) resetViewerZoom(true);
  else { zoom = 2.5; panX = 0; panY = 0; applyViewerTransform(true); }
});
document.addEventListener('keydown', (event) => {
  if (!viewer.open) return;
  if (event.key === 'ArrowLeft') movePhoto(-1);
  if (event.key === 'ArrowRight') movePhoto(1);
});

if (new URLSearchParams(location.search).has('upload')) openUpload();
Promise.all([loadCollections(), loadPhotos(), loadTimeline()]).catch(() => {
  emptyState.hidden = false;
  emptyState.querySelector('p').textContent = 'The gallery could not load. Please try again in a moment.';
});
