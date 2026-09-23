const gallery = document.querySelector('#gallery');
const emptyState = document.querySelector('#emptyState');
const galleryStatus = document.querySelector('#galleryStatus');
const gallerySkeleton = document.querySelector('#gallerySkeleton');
const retryGallery = document.querySelector('#retryGallery');
const retryGalleryPage = document.querySelector('#retryGalleryPage');
const galleryPageError = document.querySelector('#galleryPageError');
const galleryErrorTitle = document.querySelector('#galleryErrorTitle');
const galleryErrorCopy = document.querySelector('#galleryErrorCopy');
const galleryTitle = document.querySelector('#galleryTitle');
const galleryEyebrow = document.querySelector('#galleryEyebrow');
const galleryResultCount = document.querySelector('#galleryResultCount');
const mediaFilters = document.querySelector('#mediaFilters');
const mediaFilterSummary = document.querySelector('#mediaFilterSummary');
const emptyBrowseAll = document.querySelector('#emptyBrowseAll');
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
const viewerCaptionText = document.querySelector('#viewerCaptionText');
const viewerCaptionEditor = document.querySelector('#viewerCaptionEditor');
const viewerCaption = document.querySelector('#viewerCaption');
const viewerCaptionCount = document.querySelector('#viewerCaptionCount');
const viewerCaptionStatus = document.querySelector('#viewerCaptionStatus');
const viewerMeta = document.querySelector('#viewerMeta');
const viewerDetails = document.querySelector('#viewerDetails');
const viewerCaptionToggle = document.querySelector('#viewerCaptionToggle');
const viewerCaptionToggleLabel = document.querySelector('#viewerCaptionToggleLabel');
const viewerCaptionBadge = document.querySelector('#viewerCaptionBadge');
const viewerCaptionPreview = document.querySelector('#viewerCaptionPreview');
const viewerFavorite = document.querySelector('#viewerFavorite');
const downloadOriginal = document.querySelector('#downloadOriginal');
const viewerZoomControls = document.querySelector('#viewerZoomControls');
const viewerZoomOut = document.querySelector('#viewerZoomOut');
const viewerZoomIn = document.querySelector('#viewerZoomIn');
const viewerZoomReset = document.querySelector('#viewerZoomReset');
const viewerZoomLevel = document.querySelector('#viewerZoomLevel');
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
    version: Number(label.dataset.collectionVersion || 0),
  }))
  .filter((collection) => collection.id && collection.name);
const organizeMessage = document.querySelector('#organizeMessage');
const saveOrganization = document.querySelector('#saveOrganization');
const photoToast = document.querySelector('#photoToast');
const timelineYears = document.querySelector('#timelineYears');
const timelineMonths = document.querySelector('#timelineMonths');
const timelineSummary = document.querySelector('#timelineSummary');
const mediaTimeline = document.querySelector('#mediaTimeline');
const confirmSheet = document.querySelector('#confirmSheet');
const slideshowSheet = document.querySelector('#slideshowSheet');
const slideshowCollection = document.querySelector('#slideshowCollection');
const slideshowMessage = document.querySelector('#slideshowMessage');
const slideshowProgress = document.querySelector('#slideshowProgress');
const slideshowProgressBar = slideshowProgress.querySelector('span');
const slideshowMusic = document.querySelector('#slideshowMusic');
const slideshowMusicPreview = document.querySelector('#slideshowMusicPreview');
const slideshowMusicCredit = document.querySelector('#slideshowMusicCredit');
const gridZoomOut = document.querySelector('#gridZoomOut');
const gridZoomIn = document.querySelector('#gridZoomIn');
const gridDensityLabel = document.querySelector('#gridDensityLabel');
const gridDensityMode = document.querySelector('#gridDensityMode');
let slideshowMusicTracks = [];
let slideshowCollections = [];
let nextCursor = null;
let hasMorePhotos = true;
let loadingPhotos = false;
let loadedPhotos = [];
let loadedPhotoIds = new Set();
let gallerySeenCursors = new Set();
let totalPhotos = 0;
let deletedPhotoTotal = 0;
let currentPhotoIndex = -1;
let viewerLoadGeneration = 0;
const viewerPreviewCache = new Map();
const viewerPreviewCacheLimit = 8;
let touchStartX = null;
let touchStartY = null;
let touchSwipeActive = false;
let zoom = 1, panX = 0, panY = 0, pinchDistance = 0;
let panStartX = 0, panStartY = 0, panOriginX = 0, panOriginY = 0, pinchActive = false;
let viewerRenditionRank = 0;
let viewerRenditionTimer = null;
let lastViewerTapAt = 0;
let pointerPanId = null;
const viewerTouchPointers = new Map();
let viewerGesturePinched = false;
let viewerPinchCenter = null;

function activeViewerPhoto() {
  return loadedPhotos[currentPhotoIndex] || null;
}

function updateViewerZoomControls() {
  const percent = Math.round(zoom * 100);
  viewerZoomLevel.value = `${percent}%`;
  viewerZoomLevel.textContent = `${percent}%`;
  viewerZoomOut.disabled = zoom <= 1.01;
  viewerZoomIn.disabled = zoom >= 4.99;
  viewerZoomReset.disabled = zoom <= 1.01;
  viewerZoomControls.hidden = viewerImage.hidden;
}

function applyViewerTransform(animate=false) {
  viewerImage.style.transition = animate ? 'transform 160ms ease' : 'none';
  viewerImage.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;
  viewerImage.classList.toggle('zoomed', zoom > 1.01);
  updateViewerZoomControls();
  scheduleViewerRendition();
}

function clampViewerPan() {
  if (zoom <= 1.01) {
    panX = 0;
    panY = 0;
    return;
  }
  const maxX = viewerStage.clientWidth * (zoom - 1) / 2;
  const maxY = viewerStage.clientHeight * (zoom - 1) / 2;
  panX = Math.max(-maxX, Math.min(maxX, panX));
  panY = Math.max(-maxY, Math.min(maxY, panY));
}

function resetViewerZoom(animate=false) {
  zoom = 1; panX = 0; panY = 0; pinchDistance = 0; pinchActive = false;
  applyViewerTransform(animate);
}

function setViewerZoom(value, animate=false, focalPoint=null) {
  const previousZoom = zoom;
  const nextZoom = Math.max(1, Math.min(5, Number(value) || 1));
  if (focalPoint && previousZoom > 0 && nextZoom !== previousZoom) {
    const bounds = viewerStage.getBoundingClientRect();
    const focalX = focalPoint.x - (bounds.left + bounds.width / 2);
    const focalY = focalPoint.y - (bounds.top + bounds.height / 2);
    const ratio = nextZoom / previousZoom;
    panX = focalX - ratio * (focalX - panX);
    panY = focalY - ratio * (focalY - panY);
  }
  zoom = nextZoom;
  clampViewerPan();
  applyViewerTransform(animate);
}

function desiredViewerRendition(photo) {
  if (!photo || photo.is_video) return null;
  if (zoom >= 3.25 && photo.full) return {url: photo.full, rank: 3};
  if (zoom >= 1.75 && photo.detail) return {url: photo.detail, rank: 2};
  return null;
}

function scheduleViewerRendition() {
  clearTimeout(viewerRenditionTimer);
  const photo = activeViewerPhoto();
  const desired = desiredViewerRendition(photo);
  if (!desired || desired.rank <= viewerRenditionRank) return;
  const generation = viewerLoadGeneration;
  viewerRenditionTimer = setTimeout(async () => {
    const image = new Image();
    image.decoding = 'async';
    const ready = new Promise((resolve) => {
      image.addEventListener('load', () => resolve(true), {once: true});
      image.addEventListener('error', () => resolve(false), {once: true});
    });
    image.src = desired.url;
    if (!await ready) return;
    if (generation !== viewerLoadGeneration || activeViewerPhoto()?.id !== photo.id) return;
    if (desired.rank <= viewerRenditionRank) return;
    viewerImage.src = desired.url;
    viewerRenditionRank = desired.rank;
  }, 180);
}
function resetViewerTouch() {
  touchStartX = null;
  touchStartY = null;
  touchSwipeActive = false;
  pinchActive = false;
  viewerGesturePinched = false;
  viewerPinchCenter = null;
  viewerTouchPointers.clear();
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
const requestedCollection = new URLSearchParams(location.search).get('collection') || '';
let activeCollection = requestedCollection;
let collectionUnavailableNoticePending = Boolean(requestedCollection && requestedCollection !== 'deleted');
let ownerView = 'visible';
let mediaKind = 'all';
let favoriteOnly = false;
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
let galleryAbortController = null;
let galleryRetryMode = 'initial';
let activePeriod = '';
let timelineData = [];
const savedDensityLevel = Number.parseInt(localStorage.getItem('davidPiGalleryDensityV2'), 10);
const legacyDensity = localStorage.getItem('davidPiGalleryDensity');
let galleryDensityLevel = Number.isInteger(savedDensityLevel)
  ? Math.max(0, Math.min(3, savedDensityLevel))
  : legacyDensity === 'compact' ? 3 : 0;
const galleryDensityApi = window.DavidPiGalleryDensityGesture;
const galleryWindowApi = window.DavidPiGalleryWindow;
const galleryDensityAnchor = galleryDensityApi.createAnchorPreserver({
  capture: captureGalleryPosition,
  restore: restoreGalleryPositionNow,
});
const galleryAutoPageGate = galleryDensityApi.createIntersectionPageGate();
const galleryTopSpacer = document.createElement('div');
galleryTopSpacer.className = 'gallery-window-spacer gallery-window-spacer-top';
galleryTopSpacer.setAttribute('aria-hidden', 'true');
const galleryBottomSpacer = document.createElement('div');
galleryBottomSpacer.className = 'gallery-window-spacer gallery-window-spacer-bottom';
galleryBottomSpacer.setAttribute('aria-hidden', 'true');
let galleryDocumentTop = 0;
let galleryMeasuredWidth = 0;
let galleryMeasuredViewportHeight = 0;
let galleryWindowFocusIndex = null;
let galleryWindowRange = null;
const galleryWindowManager = galleryWindowApi.createWindowManager({
  cardCap: (columns) => galleryWindowApi.DEFAULT_CARD_CAPS[columns],
  overscanBefore: galleryWindowApi.DEFAULT_OVERSCAN_BEFORE,
  overscanAfter: galleryWindowApi.DEFAULT_OVERSCAN_AFTER,
  mount: mountGalleryWindowRow,
  unmount: unmountGalleryWindowRow,
  update: updateGalleryWindowRow,
  setSpacers: (top, bottom, range) => {
    galleryTopSpacer.style.height = `${Math.max(0, top)}px`;
    galleryBottomSpacer.style.height = `${Math.max(0, bottom)}px`;
    gallery.dataset.mountedPhotos = String(range.mountedPhotos || 0);
    galleryWindowRange = range;
  },
});
const galleryWindowFrame = galleryWindowApi.createFrameScheduler(renderGalleryWindowNow);
const galleryOriginFrame = galleryWindowApi.createFrameScheduler(resyncGalleryWindowOrigin);
const galleryMetadataRequests = galleryWindowApi.createGenerationRequestGate(galleryGeneration);
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
  if (!response.ok) {
    const error = new Error(apiErrorMessage(result, 'Something went wrong.'));
    error.status = response.status;
    error.data = result;
    throw error;
  }
  return result;
}

function versionedPhotoItems(ids) {
  const wanted = new Set(ids);
  return loadedPhotos
    .filter((photo) => wanted.has(photo.id))
    .map((photo) => ({id: photo.id, version: photo.version}));
}

function applyReturnedMediaVersions(result) {
  const versions = new Map((result?.items || []).map((item) => [item.id, item.version]));
  loadedPhotos.forEach((photo) => {
    if (versions.has(photo.id)) photo.version = versions.get(photo.id);
  });
}

function apiErrorMessage(result, fallback) {
  if (typeof result?.error === 'string' && result.error.trim()) return result.error;
  if (typeof result?.error?.message === 'string' && result.error.message.trim()) return result.error.message;
  return fallback;
}

function monthLabel(period, includeYear = false) {
  const [year, month] = period.split('-').map(Number);
  return new Intl.DateTimeFormat(undefined, {
    month: 'long', ...(includeYear ? {year: 'numeric'} : {}), timeZone: 'UTC',
  }).format(new Date(Date.UTC(year, month - 1, 1)));
}

function galleryCollectionName() {
  if (activeCollection === 'deleted') return 'Recently deleted';
  if (!activeCollection) return 'All media';
  return collections.find((collection) => collection.id === activeCollection)?.name || 'Collection';
}

function updateGallerySummary() {
  // Text, filters, skeletons, and collection controls all sit above the
  // virtualized gallery. Coalesce one post-layout origin check without adding
  // geometry reads to the scroll hot path or changing paging intent.
  queueGalleryOriginResync();
  const collectionName = galleryCollectionName();
  galleryEyebrow.textContent = ownerView === 'mine'
    ? 'Added by me'
    : ownerView === 'shared' ? 'Shared library' : 'Your library';
  galleryTitle.textContent = activePeriod
    ? (activePeriod.length === 4 ? activePeriod : monthLabel(activePeriod, true))
    : favoriteOnly ? 'Favorites'
    : mediaKind === 'photo' ? 'Photos'
    : mediaKind === 'video' ? 'Videos'
    : collectionName;
  if (activePeriod) galleryEyebrow.textContent = collectionName;
  const defaultContext = !activeCollection && !activePeriod && !favoriteOnly && mediaKind === 'all';
  document.querySelector('#galleryContext').classList.toggle('visually-hidden', defaultContext);
  document.querySelector('.gallery-results').classList.toggle('gallery-default-context', defaultContext);
  const appliedFilters = [];
  if (mediaKind !== 'all') appliedFilters.push(mediaKind === 'photo' ? 'Photos' : 'Videos');
  if (favoriteOnly) appliedFilters.push('Favorites');
  if (activePeriod) appliedFilters.push(activePeriod.length === 4 ? activePeriod : monthLabel(activePeriod, true));
  mediaFilterSummary.textContent = appliedFilters.join(' · ') || 'All types · Any date';
  mediaFilters.classList.toggle('has-active-filters', appliedFilters.length > 0);
  // Parallel collection/timeline requests may settle after the photo request.
  // Keep the explicit offline state truthful until the user retries it.
  if (!retryGallery.hidden && loadedPhotos.length === 0) {
    galleryResultCount.textContent = 'Unavailable';
    return;
  }
  if (loadingPhotos && loadedPhotos.length === 0) {
    galleryResultCount.textContent = 'Loading…';
  } else if (totalPhotos === 0) {
    galleryResultCount.textContent = 'No items';
  } else if (loadedPhotos.length < totalPhotos) {
    galleryResultCount.textContent = `${loadedPhotos.length.toLocaleString()} of ${totalPhotos.toLocaleString()}`;
  } else {
    galleryResultCount.textContent = `${totalPhotos.toLocaleString()} item${totalPhotos === 1 ? '' : 's'}`;
  }
}

function configureEmptyState() {
  const heading = emptyState.querySelector('h2');
  const copy = emptyState.querySelector('p');
  const upload = emptyState.querySelector('[data-upload]');
  emptyBrowseAll.hidden = true;
  upload.hidden = false;
  if (activeCollection === 'deleted') {
    heading.textContent = 'Recently Deleted is empty.';
    copy.textContent = 'Deleted photos stay here until an owner restores or permanently deletes them.';
    upload.hidden = true;
  } else if (favoriteOnly) {
    heading.textContent = 'No favorites in this view.';
    copy.textContent = 'Open any visible photo or video and choose Favorite to keep it close.';
    upload.hidden = true;
    emptyBrowseAll.textContent = 'Show all media';
    emptyBrowseAll.hidden = false;
  } else if (activePeriod) {
    heading.textContent = 'No moments in this date range.';
    copy.textContent = 'Choose another month or return to the complete library.';
    upload.hidden = true;
    emptyBrowseAll.textContent = 'Show all dates';
    emptyBrowseAll.hidden = false;
  } else if (activeCollection) {
    heading.textContent = 'Nothing in this collection yet.';
    copy.textContent = 'Add new media here, or browse the library and organize existing items.';
    emptyBrowseAll.textContent = 'Browse all media';
    emptyBrowseAll.hidden = false;
  } else if (ownerView === 'mine') {
    heading.textContent = 'You haven’t added any media yet.';
    copy.textContent = 'Add photos or videos and they’ll appear here automatically.';
  } else {
    heading.textContent = 'Your favorite moments belong here.';
    copy.textContent = 'Add a handful or a whole camera roll. We’ll arrange everything by date.';
  }
}

function gridColumnCount() {
  return galleryDensityApi.modeForLevel(galleryDensityLevel).columns;
}

function setGalleryDensity(value, preservePosition = true) {
  const nextLevel = galleryDensityApi.modeForLevel(value).level;
  if (selectionMode && nextLevel !== 0) {
    showToast('Finish selecting before switching to browse-only density.');
    return false;
  }
  if (nextLevel === galleryDensityLevel) {
    applyGalleryDensity();
    return false;
  }
  // A layout collapse must never spend a scroll gesture that happened before
  // the density change. Automatic pages require fresh user intent below.
  galleryAutoPageGate.revokeIntent();
  const change = () => {
    galleryDensityLevel = nextLevel;
    localStorage.setItem('davidPiGalleryDensityV2', String(galleryDensityLevel));
    applyGalleryDensity();
  };
  if (preservePosition) galleryDensityAnchor.mutate(change);
  else change();
  return true;
}

function pointerDistance(points) {
  return Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
}

function pointerCenter(points) {
  return {x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2};
}

function createGalleryDensityGesture() {
  return window.DavidPiGalleryDensityGesture.create({
    getLevel: () => galleryDensityLevel,
    setLevel: (level) => setGalleryDensity(level),
    now: () => performance.now(),
  });
}
const galleryDensityGesture = createGalleryDensityGesture();
const galleryTouchDensityGesture = createGalleryDensityGesture();
let galleryTouchPinchActive = false;

function captureGalleryPointers(pointerIds) {
  pointerIds.forEach((pointerId) => {
    try { gallery.setPointerCapture?.(pointerId); } catch (_) { /* Pointer already ended. */ }
  });
}

function endGalleryPointer(event, releaseCapture = true) {
  galleryDensityGesture.pointerEnd(event.pointerId);
  if (releaseCapture && gallery.hasPointerCapture?.(event.pointerId)) {
    gallery.releasePointerCapture(event.pointerId);
  }
}

gallery.addEventListener('pointerdown', (event) => {
  if (event.pointerType !== 'touch'||galleryTouchPinchActive) return;
  const result = galleryDensityGesture.pointerDown({
    id: event.pointerId,
    x: event.clientX,
    y: event.clientY,
  });
  if (!result.consume) return;
  captureGalleryPointers(result.captureIds);
  event.preventDefault();
});

gallery.addEventListener('pointermove', (event) => {
  if (event.pointerType !== 'touch'||galleryTouchPinchActive) return;
  const result = galleryDensityGesture.pointerMove({
    id: event.pointerId,
    x: event.clientX,
    y: event.clientY,
  });
  captureGalleryPointers(result.captureIds || []);
  if (result.consume) event.preventDefault();
});

gallery.addEventListener('pointerup', (event) => {if(!galleryTouchPinchActive)endGalleryPointer(event);});
gallery.addEventListener('pointercancel', (event) => {if(!galleryTouchPinchActive)endGalleryPointer(event);});
gallery.addEventListener('lostpointercapture', (event) => endGalleryPointer(event, false));

function galleryTouchPoint(touch) {
  return {id:touch.identifier,x:touch.clientX,y:touch.clientY};
}
function beginGalleryTouchPinch(touches) {
  galleryDensityGesture.cancelAll();
  galleryTouchDensityGesture.cancelAll();
  const first=galleryTouchDensityGesture.pointerDown(galleryTouchPoint(touches[0]));
  const second=galleryTouchDensityGesture.pointerDown(galleryTouchPoint(touches[1]));
  // Track any accepted two-touch sequence. If the fingertips initially land
  // very close together the controller claims it as soon as they separate;
  // this also survives Android cancelling an earlier one-finger pointer pan.
  galleryTouchPinchActive=Boolean(first.accepted&&second.accepted);
  return galleryTouchPinchActive;
}
gallery.addEventListener('touchstart',(event)=>{
  if(event.touches.length===2&&beginGalleryTouchPinch(event.touches))event.preventDefault();
  else if(galleryTouchPinchActive)event.preventDefault();
},{passive:false});
gallery.addEventListener('touchmove',(event)=>{
  if(!galleryTouchPinchActive||event.touches.length!==2){if(galleryTouchPinchActive)event.preventDefault();return;}
  let consumed=false;
  for(const touch of event.touches){
    const result=galleryTouchDensityGesture.pointerMove(galleryTouchPoint(touch));
    consumed=consumed||result.consume;
  }
  if(consumed)event.preventDefault();
},{passive:false});
gallery.addEventListener('touchend',(event)=>{
  if(!galleryTouchPinchActive)return;
  galleryTouchDensityGesture.cancelAll();
  galleryTouchPinchActive=false;
  if(event.touches.length===2)beginGalleryTouchPinch(event.touches);
},{passive:false});
gallery.addEventListener('touchcancel',(event)=>{
  galleryTouchDensityGesture.cancelAll();
  galleryTouchPinchActive=false;
},{passive:false});
window.addEventListener('blur',()=>{
  galleryDensityGesture.cancelAll();
  galleryTouchDensityGesture.cancelAll();
  galleryTouchPinchActive=false;
});
gallery.addEventListener('click', (event) => {
  if (!galleryDensityGesture.shouldSuppressClick()&&!galleryTouchDensityGesture.shouldSuppressClick()) return;
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);
gallery.addEventListener('click', (event) => {
  const card = event.target.closest?.('.photo');
  if (!card || !gallery.contains(card)) return;
  if (!galleryDensityApi.modeForLevel(galleryDensityLevel).interactive) {
    event.preventDefault();
    return;
  }
  if (selectionMode) togglePhotoSelection(card.dataset.photoId, card);
  else openPhoto(card.dataset.photoId);
});
gallery.addEventListener('error', (event) => {
  const image = event.target;
  if (!image.matches?.('.photo img') || !image.parentNode) return;
  const fallback = document.createElement('span');
  fallback.className = 'photo-preview-fallback';
  fallback.textContent = 'Preview unavailable';
  image.replaceWith(fallback);
}, true);

function monthHeading(period) {
  const row = document.createElement('div');
  row.className = 'gallery-month-row';
  const heading = document.createElement('h2');
  heading.className = 'gallery-month';
  heading.textContent = monthLabel(period, true);
  row.append(heading);
  return row;
}

function ensureGalleryWindowStructure() {
  gallery.dataset.windowed = 'true';
  if (
    galleryTopSpacer.parentNode === gallery
    && galleryBottomSpacer.parentNode === gallery
  ) return;
  galleryWindowManager.releaseAll();
  gallery.replaceChildren(galleryTopSpacer, galleryBottomSpacer);
}

function measureGalleryWindow() {
  const bounds = gallery.getBoundingClientRect();
  galleryMeasuredWidth = Math.max(1, gallery.clientWidth || bounds.width || window.innerWidth);
  galleryMeasuredViewportHeight = Math.max(1, window.innerHeight || 1);
  galleryDocumentTop = bounds.top + window.scrollY;
  return galleryMeasuredWidth;
}

function galleryWindowModel() {
  const columns = gridColumnCount();
  const gap = galleryWindowApi.gapForColumns(columns);
  return galleryWindowApi.buildRowModel(loadedPhotos, {
    columns,
    gap,
    width: galleryMeasuredWidth || measureGalleryWindow(),
    viewportHeight: galleryMeasuredViewportHeight || window.innerHeight || 1,
    cardCap: galleryWindowApi.DEFAULT_CARD_CAPS[columns],
  });
}

function rebuildGalleryWindow({preserveRows = false, position = null, append = false} = {}) {
  ensureGalleryWindowStructure();
  const previousWidth = galleryMeasuredWidth;
  const previousHeight = galleryMeasuredViewportHeight;
  measureGalleryWindow();
  const previous = galleryWindowManager.model();
  if (append && previous.columns === gridColumnCount()
      && previousWidth === galleryMeasuredWidth && previousHeight === galleryMeasuredViewportHeight) {
    const extended = galleryWindowApi.appendRowModel(previous, loadedPhotos);
    galleryWindowManager.setModel(extended.model, {preserveRows: true, appendFrom: extended.changedFrom});
  } else {
    galleryWindowManager.setModel(galleryWindowModel(), {preserveRows});
  }
  if (position) restoreGalleryPositionNow(position);
  else renderGalleryWindowNow();
}

function renderGalleryWindowNow() {
  if (galleryTopSpacer.parentNode !== gallery) return;
  const viewportHeight = Math.max(1, window.innerHeight || 1);
  const model = galleryWindowManager.model();
  if (Math.abs(viewportHeight - model.viewportHeight) > 0.5) {
    const bounds = gallery.getBoundingClientRect();
    galleryDocumentTop = bounds.top + window.scrollY;
    const position = captureGalleryPosition();
    galleryMeasuredWidth = Math.max(1, gallery.clientWidth || bounds.width || window.innerWidth);
    galleryMeasuredViewportHeight = viewportHeight;
    galleryWindowManager.setModel(galleryWindowModel());
    restoreGalleryPositionNow(position);
    return galleryWindowManager.range();
  }
  const relativeScroll = Math.max(0, window.scrollY - galleryDocumentTop);
  const range = galleryWindowManager.update(relativeScroll, viewportHeight);
  if (galleryWindowFocusIndex !== null) {
    const cards = [...gallery.querySelectorAll('.photo')];
    const nearest = cards.sort((left, right) => (
      Math.abs(Number(left.dataset.photoIndex) - galleryWindowFocusIndex)
      - Math.abs(Number(right.dataset.photoIndex) - galleryWindowFocusIndex)
    ))[0];
    galleryWindowFocusIndex = null;
    nearest?.focus({preventScroll: true});
  }
  return range;
}

function queueGalleryWindowRender() {
  galleryWindowFrame.request();
}

function queueGalleryOriginResync() {
  galleryOriginFrame.request();
}

function resyncGalleryWindowOrigin() {
  if (galleryTopSpacer.parentNode !== gallery) return;
  const bounds = gallery.getBoundingClientRect();
  const nextWidth = Math.max(1, gallery.clientWidth || bounds.width || window.innerWidth);
  const nextViewportHeight = Math.max(1, window.innerHeight || 1);
  const geometryChanged = (
    Math.abs(nextWidth - galleryMeasuredWidth) > 0.5
    || Math.abs(nextViewportHeight - galleryMeasuredViewportHeight) > 0.5
  );
  galleryDocumentTop = bounds.top + window.scrollY;
  if (geometryChanged) {
    // Capture against the corrected document origin, then rebuild arithmetic
    // rows if an above-gallery layout change also changed available width.
    const position = captureGalleryPosition();
    galleryMeasuredWidth = nextWidth;
    galleryMeasuredViewportHeight = nextViewportHeight;
    galleryWindowManager.setModel(galleryWindowModel());
    restoreGalleryPositionNow(position);
    return;
  }
  galleryMeasuredWidth = nextWidth;
  galleryMeasuredViewportHeight = nextViewportHeight;
  renderGalleryWindowNow();
}

function mountGalleryWindowRow(row, rowIndex, successor, range) {
  let node;
  if (row.type === 'month') {
    node = monthHeading(row.period);
    node.classList.add('gallery-window-row', 'gallery-window-month-row');
  } else {
    node = document.createElement('div');
    node.className = 'gallery-window-row gallery-window-photo-row';
    node.style.setProperty('--gallery-row-columns', String(gridColumnCount()));
    node.style.setProperty('--gallery-row-gap', `${galleryWindowApi.gapForColumns(gridColumnCount())}px`);
    node.style.setProperty('--gallery-row-card-height', `${row.cardHeight}px`);
    row.itemIndexes.forEach((itemIndex, columnIndex) => {
      const photo = loadedPhotos[itemIndex];
      if (photo) node.append(photoCard(photo, {
        itemIndex,
        rowIndex,
        columnIndex,
        range,
      }));
    });
  }
  node.dataset.windowRow = String(rowIndex);
  node.style.height = `${row.height}px`;
  gallery.insertBefore(node, successor || galleryBottomSpacer);
  return node;
}

function updateGalleryWindowRow(node, row, rowIndex, range) {
  if (row.type !== 'photos') return;
  node.querySelectorAll('.photo').forEach((card, columnIndex) => {
    const image = card.querySelector('img');
    if (!image) return;
    const hints = galleryWindowApi.thumbnailHintsForRow(rowIndex, range, columnIndex);
    image.loading = hints.loading;
    image.fetchPriority = hints.fetchPriority;
  });
}

function unmountGalleryWindowRow(node) {
  const focused = document.activeElement;
  if (focused && node.contains(focused)) {
    galleryWindowFocusIndex = Number(focused.dataset.photoIndex || 0);
    // Move focus deliberately before removing its card. The next reconciliation
    // moves it to the nearest visible native button without changing scroll.
    gallery.tabIndex = -1;
    gallery.focus({preventScroll: true});
    galleryStatus.textContent = 'Media focus moved with the visible gallery window.';
  }
  node.querySelectorAll('img').forEach((image) => {
    image.removeAttribute('src');
    image.removeAttribute('srcset');
  });
  node.replaceChildren();
  node.remove();
}

function galleryMetadataBatchSize() {
  const columns = gridColumnCount();
  return galleryWindowApi.metadataBatchSize({
    columns,
    gap: galleryWindowApi.gapForColumns(columns),
    width: galleryMeasuredWidth || measureGalleryWindow(),
    viewportHeight: window.innerHeight || 1,
  });
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
    button.setAttribute('aria-pressed', activePeriod === year ? 'true' : 'false');
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
    button.setAttribute('aria-pressed', activePeriod === entry.month ? 'true' : 'false');
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
  updateGallerySummary();
}

async function loadTimeline(generation = galleryGeneration) {
  if (activeCollection === 'deleted') {
    timelineData = [];
    document.querySelector('#mediaTimeline').hidden = true;
    return;
  }
  document.querySelector('#mediaTimeline').hidden = false;
  const query = new URLSearchParams();
  query.set('scope', ownerView);
  if (mediaKind !== 'all') query.set('kind', mediaKind);
  if (favoriteOnly) query.set('favorite', 'true');
  if (activeCollection) query.set('collection', activeCollection);
  const result = await api(`/api/photos/timeline?${query}`);
  if (generation !== galleryGeneration) return;
  timelineData = result.months || [];
  renderTimeline();
}

async function refreshTimelineIfOpen(generation = galleryGeneration) {
  if (!mediaTimeline.open) return false;
  await loadTimeline(generation);
  return true;
}

mediaTimeline.addEventListener('toggle', () => {
  queueGalleryOriginResync();
  if (!mediaTimeline.open) return;
  const generation = galleryGeneration;
  timelineSummary.textContent = 'Loading dates…';
  refreshTimelineIfOpen(generation).catch(() => {
    if (generation === galleryGeneration) timelineSummary.textContent = 'Dates unavailable · close and retry';
  });
});

mediaFilters.addEventListener('toggle', () => {
  queueGalleryOriginResync();
});

async function setTimelinePeriod(period) {
  activePeriod = activePeriod === period ? '' : period;
  renderTimeline();
  await refreshPhotos();
  document.querySelector('#mediaTimeline').open = false;
  gallery.scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function openTimelinePeriod(period) {
  activePeriod = period;
  renderTimeline();
  await refreshPhotos();
  document.querySelector('#mediaTimeline').open = false;
  gallery.scrollIntoView({behavior: 'smooth', block: 'start'});
}

document.querySelector('#clearTimeline').addEventListener('click', () => setTimelinePeriod(''));

function updateGalleryDensityControls() {
  gridZoomOut.disabled = galleryDensityLevel === 0;
  gridZoomIn.disabled = galleryDensityLevel === 3 || selectionMode;
  gridZoomIn.title = selectionMode ? 'Finish selecting to use browse-only density' : '';
}

function applyGalleryDensity() {
  const mode = galleryDensityApi.modeForLevel(galleryDensityLevel);
  gallery.dataset.densityLevel = String(mode.level);
  gallery.dataset.interactionMode = mode.mode;
  gridDensityLabel.value = `${mode.columns} across`;
  gridDensityLabel.textContent = `${mode.columns} across`;
  gridDensityMode.textContent = mode.interactive
    ? 'Tap to open or select'
    : 'Browse only · switch to 3 across to open or select';
  updateGalleryDensityControls();
  // Row membership and height change with density. Rebuild the compact row
  // model, then mount only the new visible/overscan window.
  rebuildGalleryWindow();
}

gridZoomOut.addEventListener('click', () => setGalleryDensity(galleryDensityLevel - 1));
gridZoomIn.addEventListener('click', () => setGalleryDensity(galleryDensityLevel + 1));
applyGalleryDensity();

function openUpload() {
  message.textContent = '';
  const collection = collections.find((item) => item.id === activeCollection);
  if (collection && !collection.is_mine) {
    showToast('Add media to your library first, then organize it into this shared collection.');
    return;
  }
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
        else reject(new Error(apiErrorMessage(result, 'Upload failed')));
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
  progress.setAttribute('aria-valuenow', '0');
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
      progress.setAttribute('aria-valuenow', String(Math.round(uploadedBytes / totalBytes * 100)));
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
    progress.setAttribute('aria-valuenow', '100');
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
  if (generation !== viewerLoadGeneration || loadedPhotos[currentPhotoIndex]?.id !== photo.id) return;
  if (ready && viewerRenditionRank < 2) {
    viewerImage.src = entry.url;
    viewerRenditionRank = 1;
  }
  // Keep the thumbnail usable when a larger rendition fails instead of
  // leaving a permanent spinner and blur over the viewer.
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
  clearTimeout(viewerRenditionTimer);
  viewerRenditionRank = 0;
  resetViewerZoom();
  stopViewerVideo();
  viewerVideo.loop = false;
  viewerImage.hidden = photo.is_video;
  viewerVideo.hidden = !photo.is_video;
  viewerZoomControls.hidden = photo.is_video;
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
  const viewerPrivacy = document.querySelector('#viewerPrivacy');
  viewerPrivacy.textContent = photo.visibility === 'private' ? 'Share' : 'Only me';
  downloadOriginal.href = photo.original;
  const deleted = activeCollection === 'deleted';
  const mytubeButton = document.querySelector('#addToMytube');
  mytubeButton.hidden = deleted || !photo.is_video || !photo.is_mine;
  mytubeButton.disabled = false;
  mytubeButton.textContent = photo.mytube_linked ? 'Remove from MyTube' : 'Add to MyTube';
  if (!mytubeButton.hidden && photo.mytube_linked === undefined) {
    const expectedId = photo.id;
    api(`/api/mytube/media-links/${encodeURIComponent(photo.id)}`)
      .then((result) => {
        photo.mytube_linked = Boolean(result.linked);
        if (loadedPhotos[currentPhotoIndex]?.id === expectedId) {
          mytubeButton.textContent = photo.mytube_linked ? 'Remove from MyTube' : 'Add to MyTube';
        }
      })
      .catch(() => {});
  }
  viewerPrivacy.hidden = deleted || !photo.is_mine;
  document.querySelector('#organizePhoto').hidden = deleted || photo.ownership_status !== 'owned';
  document.querySelector('#restorePhoto').hidden = !deleted || !photo.is_mine;
  const deletePhoto = document.querySelector('#deletePhoto');
  deletePhoto.hidden = !photo.is_mine || deleted;
  deletePhoto.textContent = deleted ? 'Delete forever' : 'Delete';
  viewerFavorite.hidden = deleted;
  viewerFavorite.setAttribute('aria-pressed', photo.favorite ? 'true' : 'false');
  viewerFavorite.textContent = photo.favorite ? '★ Favorited' : '☆ Favorite';
  viewerCaptionText.textContent = photo.caption || 'No caption';
  viewerCaptionText.hidden = Boolean(photo.is_mine && !deleted);
  viewerCaptionEditor.hidden = !photo.is_mine || deleted;
  viewerCaption.value = photo.caption || '';
  viewerCaptionCount.textContent = `${Array.from(viewerCaption.value).length.toLocaleString()} / 1,000`;
  viewerCaptionStatus.textContent = '';
  updateViewerCaptionDisclosure(photo,deleted);
  setViewerCaptionExpanded(false);
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

function updateFavoriteBadge(photo) {
  const card = gallery.querySelector(`[data-photo-id="${CSS.escape(photo.id)}"]`);
  if (!card) return;
  card.querySelector('.photo-favorite')?.remove();
  if (photo.favorite) {
    const badge = document.createElement('span');
    badge.className = 'photo-favorite';
    badge.textContent = '★';
    badge.setAttribute('aria-label', 'Favorite');
    card.append(badge);
  }
}

async function setViewerFavorite() {
  const photo = loadedPhotos[currentPhotoIndex];
  if (!photo || activeCollection === 'deleted') return;
  const desired = !photo.favorite;
  viewerFavorite.disabled = true;
  try {
    const result = await api(`/api/photos/${encodeURIComponent(photo.id)}/favorite`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        favorite: desired,
        state_version: photo.state_version,
        media_version: photo.version,
      }),
    });
    photo.favorite = result.favorite;
    photo.state_version = result.state_version;
    viewerFavorite.setAttribute('aria-pressed', photo.favorite ? 'true' : 'false');
    viewerFavorite.textContent = photo.favorite ? '★ Favorited' : '☆ Favorite';
    updateFavoriteBadge(photo);
    if (favoriteOnly && !photo.favorite) {
      const removedId = photo.id;
      viewer.close();
      removeLoadedPhotos([removedId]);
      showToast('Removed from your favorites.');
    } else {
      showToast(photo.favorite ? 'Added to your favorites.' : 'Removed from your favorites.');
    }
  } catch (error) {
    showToast(error.message);
  } finally {
    viewerFavorite.disabled = false;
  }
}

async function saveViewerCaption() {
  const photo = loadedPhotos[currentPhotoIndex];
  if (!photo || !photo.is_mine || activeCollection === 'deleted') return;
  const button = document.querySelector('#saveViewerCaption');
  button.disabled = true;
  viewerCaptionStatus.textContent = 'Saving caption…';
  try {
    const result = await api(`/api/photos/${encodeURIComponent(photo.id)}/caption`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        caption: viewerCaption.value,
        caption_version: photo.caption_version,
        media_version: photo.version,
      }),
    });
    photo.caption = result.caption;
    photo.caption_version = result.caption_version;
    viewerCaption.value = result.caption;
    viewerCaptionText.textContent = result.caption || 'No caption';
    updateViewerCaptionDisclosure(photo,false);
    viewerCaptionStatus.textContent = 'Caption saved.';
    setViewerCaptionExpanded(false);
    showToast('Caption saved.');
  } catch (error) {
    viewerCaptionStatus.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

viewerFavorite.addEventListener('click', setViewerFavorite);
function setViewerCaptionExpanded(expanded, focusEditor=false) {
  const open=Boolean(expanded);
  viewerDetails.hidden=!open;
  viewerCaptionToggle.setAttribute('aria-expanded',String(open));
  viewerMeta.classList.toggle('details-expanded',open);
  if(open&&focusEditor&&!viewerCaptionEditor.hidden)requestAnimationFrame(()=>viewerCaption.focus());
}
function updateViewerCaptionDisclosure(photo,deleted) {
  const hasCaption=Boolean(String(photo?.caption||'').trim());
  const editable=Boolean(photo?.is_mine&&!deleted);
  viewerCaptionToggleLabel.textContent=editable?(hasCaption?'Edit caption':'Add caption'):(hasCaption?'View caption':'Details');
  viewerCaptionBadge.textContent=hasCaption?'1':'0';
  viewerCaptionBadge.setAttribute('aria-label',hasCaption?'1 caption':'No caption');
  viewerCaptionToggle.setAttribute('aria-label',`${viewerCaptionToggleLabel.textContent} · ${hasCaption?'1 caption':'no caption'}`);
  viewerCaptionPreview.textContent=hasCaption?photo.caption:'';
  viewerCaptionPreview.hidden=!hasCaption;
}
viewerCaptionToggle.addEventListener('click',()=>setViewerCaptionExpanded(viewerCaptionToggle.getAttribute('aria-expanded')!=='true',true));
document.querySelector('#saveViewerCaption').addEventListener('click', saveViewerCaption);
viewerCaption.addEventListener('input', () => {
  viewerCaptionCount.textContent = `${Array.from(viewerCaption.value).length.toLocaleString()} / 1,000`;
  viewerCaptionStatus.textContent = '';
});
viewerCaption.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault();
    saveViewerCaption();
  }
});

function photoCard(photo, windowPosition) {
  const button = document.createElement('button');
  button.className = 'photo';
  button.type = 'button';
  button.dataset.photoId = photo.id;
  button.dataset.photoName = photo.original_name;
  button.dataset.photoIndex = String(windowPosition.itemIndex);
  const mode = galleryDensityApi.applyCardMode(button, photo.original_name, galleryDensityLevel);
  if (selectionMode) {
    galleryWindowApi.applySelectionState(button, selectedIds.has(photo.id), true);
  }
  const image = document.createElement('img');
  const requestHints = galleryWindowApi.thumbnailHintsForRow(
    windowPosition.rowIndex,
    windowPosition.range,
    windowPosition.columnIndex,
  );
  image.alt = '';
  image.width = 320;
  image.height = 320;
  image.loading = requestHints.loading;
  image.decoding = 'async';
  image.fetchPriority = requestHints.fetchPriority;
  // Configure scheduling hints before src so the browser sees the visible vs
  // overscan priority when it queues this mounted thumbnail.
  image.src = photo.thumb;
  button.append(image);
  // Browse-only densities intentionally use thumbnail-only rows. Density
  // changes rebuild the row window, restoring full controls at 3-across.
  if (!mode.interactive) return button;
  if (photo.is_video) {
    button.classList.add('video-item');
    const badge = document.createElement('span');
    badge.className = 'video-badge';
    badge.textContent = '▶';
    badge.setAttribute('aria-hidden', 'true');
    button.append(badge);
  }
  if (photo.favorite) {
    const favorite = document.createElement('span');
    favorite.className = 'photo-favorite';
    favorite.textContent = '★';
    favorite.setAttribute('aria-label', 'Favorite');
    button.append(favorite);
  }
  const check = document.createElement('span');
  check.className = 'photo-check';
  check.setAttribute('aria-hidden', 'true');
  button.append(check);
  return button;
}

function loadPhotos(generation = galleryGeneration) {
  return galleryMetadataRequests.run(generation, () => performPhotoLoad(generation));
}

async function performPhotoLoad(generation) {
  if (loadingPhotos || !hasMorePhotos) return;
  loadingPhotos = true;
  gallery.setAttribute('aria-busy', 'true');
  galleryStatus.textContent = loadedPhotos.length ? 'Loading more media…' : 'Loading media…';
  if (!loadedPhotos.length) gallerySkeleton.hidden = false;
  updateGallerySummary();
  galleryPageError.hidden = true;
  loadMore.disabled = true;
  loadMore.textContent = 'Loading…';
  const requestCursor = nextCursor;
  const query = new URLSearchParams({limit: String(galleryMetadataBatchSize())});
  if (requestCursor) query.set('cursor', requestCursor);
  query.set('scope', ownerView);
  if (mediaKind !== 'all') query.set('kind', mediaKind);
  if (favoriteOnly) query.set('favorite', 'true');
  if (activeCollection && activeCollection !== 'deleted') query.set('collection', activeCollection);
  if (activePeriod && activeCollection !== 'deleted') query.set('period', activePeriod);
  const endpoint = activeCollection === 'deleted' ? '/api/photos/deleted' : '/api/photos';
  try {
    const result = await api(`${endpoint}?${query}`, {signal: galleryAbortController?.signal});
    if (generation !== galleryGeneration) return false;
    if (Number.isInteger(result.total)) totalPhotos = result.total;
    emptyState.hidden = totalPhotos !== 0;
    configureEmptyState();
    const responseCursor = result.next_cursor || null;
    if (!galleryWindowApi.isSafeNextCursor(
      requestCursor,
      responseCursor,
      result.has_more,
      gallerySeenCursors,
    )) {
      const error = new Error('Media paging returned a repeated position. Refresh before loading more.');
      error.code = 'gallery_cursor_repeated';
      throw error;
    }
    const appendPosition = loadedPhotos.length ? captureGalleryPosition() : null;
    const additions = galleryWindowApi.uniqueMetadataItems(result.photos, loadedPhotoIds);
    loadedPhotos.push(...additions);
    if (responseCursor) gallerySeenCursors.add(responseCursor);
    nextCursor = responseCursor;
    hasMorePhotos = Boolean(result.has_more && nextCursor);
    loadMore.hidden = !hasMorePhotos;
    rebuildGalleryWindow({preserveRows: true, position: appendPosition, append: true});
    galleryStatus.textContent = `${loadedPhotos.length} of ${totalPhotos} item${totalPhotos === 1 ? '' : 's'} loaded.`;
    updateGallerySummary();
    if (
      !activeCollection && !activePeriod
      && mediaKind === 'all' && !favoriteOnly
    ) document.querySelector('#allPhotoCount').textContent = `${totalPhotos} item${totalPhotos === 1 ? '' : 's'}`;
    if (activeCollection === 'deleted') document.querySelector('#deletedPhotoCount').textContent = `${totalPhotos} item${totalPhotos === 1 ? '' : 's'}`;
    return true;
  } catch (error) {
    if (error?.name === 'AbortError' || generation !== galleryGeneration) return false;
    if (error?.code === 'gallery_cursor_repeated') {
      hasMorePhotos = false;
      loadMore.hidden = true;
    }
    showGalleryLoadError(
      error?.code === 'gallery_cursor_repeated'
        ? 'initial'
        : loadedPhotos.length ? 'page' : 'initial',
    );
    return false;
  } finally {
    if (generation === galleryGeneration) {
      loadingPhotos = false;
      gallery.setAttribute('aria-busy', 'false');
      gallerySkeleton.hidden = true;
      loadMore.disabled = false;
      loadMore.textContent = 'Show more';
      if (retryGallery.hidden && galleryPageError.hidden) updateGallerySummary();
    }
  }
}

async function refreshPhotos() {
  galleryAbortController?.abort();
  galleryDensityAnchor.cancel();
  galleryAutoPageGate.reset();
  galleryAbortController = typeof AbortController === 'function' ? new AbortController() : null;
  galleryGeneration += 1;
  const generation = galleryGeneration;
  galleryMetadataRequests.setGeneration(generation);
  nextCursor = null;
  hasMorePhotos = true;
  loadingPhotos = false;
  loadedPhotos = [];
  loadedPhotoIds = new Set();
  gallerySeenCursors = new Set();
  totalPhotos = 0;
  emptyState.hidden = true;
  retryGallery.hidden = true;
  galleryPageError.hidden = true;
  galleryWindowFrame.cancel();
  galleryWindowManager.releaseAll();
  gallery.replaceChildren(galleryTopSpacer, galleryBottomSpacer);
  rebuildGalleryWindow();
  updateGallerySummary();
  const loaded = await loadPhotos(generation);
  updateCollectionToolbar();
  return loaded;
}

function captureGalleryPosition() {
  const model = galleryWindowManager.model();
  const relativeScroll = Math.max(0, window.scrollY - galleryDocumentTop);
  let rowIndex = galleryWindowApi.rowAtOffset(model, relativeScroll);
  while (rowIndex < model.rows.length && model.rows[rowIndex].type !== 'photos') rowIndex += 1;
  const itemIndex = model.rows[rowIndex]?.itemIndexes?.[0];
  const photo = Number.isInteger(itemIndex) ? loadedPhotos[itemIndex] : null;
  const rowOffset = model.rows[rowIndex]?.offset || 0;
  return {
    id: photo?.id || null,
    top: galleryDocumentTop + rowOffset - window.scrollY,
    scrollY: window.scrollY,
  };
}

function restoreGalleryPositionNow(position) {
  const location = position.id
    ? galleryWindowManager.model().photoLocations.get(String(position.id))
    : null;
  if (location) {
    window.scrollTo(0, Math.max(0, galleryDocumentTop + location.offset - position.top));
  } else {
    window.scrollTo(0, position.scrollY);
  }
  renderGalleryWindowNow();
}

function restoreGalleryPosition(position) {
  requestAnimationFrame(() => requestAnimationFrame(() => restoreGalleryPositionNow(position)));
}

function removeLoadedPhotos(ids) {
  const removed = new Set(ids);
  loadedPhotos = loadedPhotos.filter((photo) => !removed.has(photo.id));
  removed.forEach((id) => loadedPhotoIds.delete(id));
  rebuildGalleryWindow();
  totalPhotos = Math.max(0, totalPhotos - removed.size);
  emptyState.hidden = totalPhotos !== 0;
  if (totalPhotos === 0) configureEmptyState();
  updateGallerySummary();
}

function collectionCard(collection) {
  const button = document.createElement('button');
  button.className = `collection-card dynamic${activeCollection === collection.id ? ' selected' : ''}`;
  button.type = 'button';
  button.dataset.collection = collection.id;
  if (activeCollection === collection.id) button.setAttribute('aria-current', 'true');
  const cover = document.createElement('span');
  cover.className = 'collection-cover';
  if (collection.cover) {
    const image = document.createElement('img');
    image.src = collection.cover;
    image.alt = '';
    image.width = 284;
    image.height = 186;
    image.loading = 'lazy';
    image.decoding = 'async';
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
  const requestedOwnerView = ownerView;
  const collectionViews = requestedOwnerView === 'visible'
    ? ['', 'mine']
    : [requestedOwnerView === 'mine' ? 'mine' : ''];
  const listings = await Promise.all(collectionViews.map((view) => (
    api(`/api/collections?view=${encodeURIComponent(view)}`)
  )));
  if (requestedOwnerView !== ownerView) return;
  const collectionMap = new Map();
  listings.forEach((listing) => {
    (listing.collections || []).forEach((collection) => {
      collectionMap.set(collection.id, collection);
    });
  });
  collections = [...collectionMap.values()].sort((left, right) => (
    left.name.localeCompare(right.name, undefined, {sensitivity: 'base'})
    || left.id.localeCompare(right.id)
  ));
  if (activeCollection && activeCollection !== 'deleted' && !collections.some((item) => item.id === activeCollection)) {
    activeCollection = '';
    const cleanUrl = new URL(location.href);
    cleanUrl.searchParams.delete('collection');
    history.replaceState(null, '', cleanUrl);
    if (collectionUnavailableNoticePending) showToast('That collection is unavailable.');
  }
  collectionUnavailableNoticePending = false;
  collectionRail.querySelectorAll('.collection-card.dynamic').forEach((item) => item.remove());
  const deletedCard = collectionRail.querySelector('[data-collection="deleted"]');
  collections.forEach((collection) => collectionRail.insertBefore(collectionCard(collection), deletedCard));
  collectionRail.querySelectorAll('[data-collection]').forEach((item) => {
    const selected = item.dataset.collection === activeCollection;
    item.classList.toggle('selected', selected);
    if (selected) item.setAttribute('aria-current', 'true'); else item.removeAttribute('aria-current');
  });
  api(`/api/photos/deleted?limit=1&scope=${encodeURIComponent(requestedOwnerView)}`).then((result) => {
    if (requestedOwnerView !== ownerView) return;
    deletedPhotoTotal = result.owned_total;
    document.querySelector('#deletedPhotoCount').textContent = result.total ? `${result.total} item${result.total === 1 ? '' : 's'}` : 'Restorable for 30+ days';
    updateCollectionToolbar();
  }).catch(() => {});
  updateCollectionToolbar();
  updateGallerySummary();
}

collectionRail.querySelectorAll('.collection-card:not(.dynamic)').forEach((card) => {
  card.addEventListener('click', () => selectCollection(card.dataset.collection || ''));
});

async function selectCollection(id) {
  exitSelectionMode();
  activeCollection = id;
  activePeriod = '';
  if (id === 'deleted') {
    mediaKind = 'all';
    favoriteOnly = false;
  }
  syncDiscoveryControls();
  collectionRail.querySelectorAll('[data-collection]').forEach((item) => {
    const selected = item.dataset.collection === id;
    item.classList.toggle('selected', selected);
    if (selected) item.setAttribute('aria-current', 'true'); else item.removeAttribute('aria-current');
  });
  const selectedCard = collectionRail.querySelector(`[data-collection="${CSS.escape(id)}"]`);
  if (selectedCard) selectedCard.scrollIntoView({behavior: 'smooth', inline: 'center', block: 'nearest'});
  updateCollectionToolbar();
  updateGallerySummary();
  try {
    await Promise.all([refreshPhotos(), refreshTimelineIfOpen()]);
  } catch (_error) {
    showGalleryLoadError('initial');
  }
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

function updateCollectionToolbar() {
  const selected = collections.find((collection) => collection.id === activeCollection);
  const allActions = document.querySelector('#allPhotoActions');
  const deletedActions = document.querySelector('#deletedCollectionActions');
  const customActions = document.querySelector('#customCollectionActions');
  // Header Add and Select already cover the default library. Keep collection
  // management and recoverable-trash actions only in their relevant context.
  collectionToolbar.hidden = activeCollection === '';
  allActions.hidden = activeCollection !== '';
  deletedActions.hidden = activeCollection !== 'deleted';
  customActions.hidden = !selected || !selected.is_mine;
  document.querySelector('#selectedCollectionName').textContent =
    activeCollection === '' ? 'All media' : activeCollection === 'deleted' ? 'Recently deleted' : selected?.name || 'Collection';
  document.querySelector('#selectFromAll').disabled = activeCollection === '' && totalPhotos === 0;
  document.querySelector('#selectDeleted').disabled = activeCollection === 'deleted' && totalPhotos === 0;
  document.querySelector('#restoreAllDeleted').disabled = deletedPhotoTotal === 0;
  document.querySelector('#emptyDeleted').disabled = true;
  if (selected) document.querySelector('#collectionPrivacy').textContent = selected.visibility === 'private' ? 'Share' : 'Only me';
  updateGallerySummary();
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

let slideshowSubmitting = false;
let slideshowStateRevision = 0;
let slideshowDialogLifecycle = null;

function slideshowIsOpen() {
  return slideshowSheet.open || Boolean(slideshowSheet.__davidPiMobileHost);
}

function pauseSlideshowPreview() {
  slideshowMusicPreview.pause();
}

function setSlideshowBusy(busy, {accepted = false, submitting = false} = {}) {
  const create = document.querySelector('#createSlideshow');
  const cancel = document.querySelector('#cancelSlideshow');
  create.disabled = busy || !slideshowCollections.length;
  create.textContent = submitting ? 'Submitting…' : accepted ? 'Video queued' : 'Create video';
  cancel.disabled = submitting;
  cancel.textContent = accepted ? 'Close' : 'Cancel';
  document.querySelector('#closeSlideshow').disabled = submitting;
  slideshowCollection.disabled = busy;
  document.querySelector('#slideshowDuration').disabled = busy;
  document.querySelector('#slideshowLayout').disabled = busy;
  document.querySelector('#slideshowTransition').disabled = busy;
  slideshowMusic.disabled = busy;
  slideshowMusicPreview.controls = !busy;
  document.querySelector('#slideshowLoop').disabled = busy;
}

function renderSlideshowJobState(state) {
  slideshowStateRevision += 1;
  slideshowMessage.textContent = state.message || '';
  slideshowProgressBar.style.width = `${Math.max(0, Math.min(100, Number(state.progress) || 0))}%`;
  slideshowProgress.setAttribute('aria-valuenow', String(Math.max(0, Math.min(100, Number(state.progress) || 0))));
  if (state.phase === 'submitting') {
    slideshowProgress.hidden = false;
    setSlideshowBusy(true, {submitting: true});
  } else if (state.active) {
    slideshowProgress.hidden = false;
    setSlideshowBusy(true, {accepted: true});
  } else {
    slideshowProgress.hidden = state.phase !== 'completed';
    setSlideshowBusy(false);
  }
}

async function handleSlideshowCompletion(job) {
  const wasOpen = slideshowIsOpen();
  setSlideshowBusy(false);
  if (wasOpen) slideshowDialogLifecycle.close();
  let refreshed = false;
  try {
    await loadCollections();
    refreshed = true;
    if (wasOpen) {
      const videos = collections.find((collection) => collection.name.toLowerCase() === 'videos');
      if (videos) await selectCollection(videos.id);
    } else if (!activeCollection) {
      await refreshPhotos();
    }
  } catch (_error) {
    // Publication already succeeded. A later manual refresh can recover the view.
  }
  showToast(refreshed
    ? 'Your slideshow was saved in Videos.'
    : 'Your slideshow was saved in Videos. Refresh to see it.');
}

function handleSlideshowFailure(job) {
  setSlideshowBusy(false);
  if (!slideshowIsOpen()) showToast(job.message || 'The slideshow job could not be checked.');
}

const slideshowJobController = window.DavidPiSlideshowJobs?.create({
  submit: (payload) => api('/api/slideshows', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  }),
  poll: (jobId) => api(`/api/slideshows/${jobId}`),
  onState: renderSlideshowJobState,
  onComplete: handleSlideshowCompletion,
  onFailure: handleSlideshowFailure,
});

slideshowDialogLifecycle = window.DavidPiSlideshowJobs?.bindDialogLifecycle({
  dialog: slideshowSheet,
  pause: pauseSlideshowPreview,
  isSubmitting: () => slideshowSubmitting,
  onBlockedClose: () => {
    slideshowMessage.textContent = 'Waiting for David-Pi to accept the video job…';
  },
}) || {
  close() {
    pauseSlideshowPreview();
    if (slideshowIsOpen()) slideshowSheet.close();
  },
};

async function openSlideshow() {
  const activeJob = slideshowJobController?.activeJobId();
  if (activeJob) renderSlideshowJobState(slideshowJobController.snapshot());
  else {
    slideshowMessage.textContent = 'Loading your media collections…';
    slideshowProgress.hidden = true;
    setSlideshowBusy(true);
  }
  slideshowSheet.showModal();
  if (!slideshowJobController) {
    slideshowMessage.textContent = 'The video controls did not load. Refresh this page before creating a video.';
    return;
  }
  slideshowJobController.resume();
  const stateRevision = slideshowStateRevision;
  try {
    const result = await api('/api/slideshows/options');
    slideshowCollections = result.collections || [];
    slideshowCollection.innerHTML = '';
    slideshowCollections.forEach((collection) => {
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
    if (!slideshowJobController.activeJobId()) {
      slideshowMusic.value = '';
      pauseSlideshowPreview();
      slideshowMusicPreview.removeAttribute('src');
      slideshowMusicPreview.hidden = true;
      slideshowMusicCredit.textContent = 'Choose a track, then preview it before creating the video.';
      if (result.collections.some((collection) => collection.id === activeCollection)) slideshowCollection.value = activeCollection;
      if (stateRevision === slideshowStateRevision) {
        slideshowMessage.textContent = result.collections.length ? '' : 'Create a collection with at least one photo or video first.';
      }
      setSlideshowBusy(false);
    } else {
      setSlideshowBusy(true, {accepted: true});
    }
  } catch (error) {
    if (!slideshowJobController.activeJobId() && stateRevision === slideshowStateRevision) {
      slideshowMessage.textContent = error.message;
      setSlideshowBusy(false);
    }
  }
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

document.querySelector('#openSlideshow').addEventListener('click', openSlideshow);
function closeSlideshow() {
  slideshowDialogLifecycle.close();
}
document.querySelector('#closeSlideshow').addEventListener('click', closeSlideshow);
document.querySelector('#cancelSlideshow').addEventListener('click', closeSlideshow);
document.querySelector('#createSlideshow').addEventListener('click', async () => {
  if (!slideshowJobController) {
    slideshowMessage.textContent = 'The video controls did not load. Refresh this page before creating a video.';
    return;
  }
  if (slideshowJobController.activeJobId()) {
    renderSlideshowJobState(slideshowJobController.snapshot());
    slideshowJobController.resume();
    return;
  }
  slideshowSubmitting = true;
  pauseSlideshowPreview();
  setSlideshowBusy(true, {submitting: true});
  try {
    const result = await slideshowJobController.submit({
      collection_id: slideshowCollection.value,
      collection_version: slideshowCollections.find(
        (collection) => collection.id === slideshowCollection.value,
      )?.version,
      duration_seconds: Number(document.querySelector('#slideshowDuration').value),
      layout: document.querySelector('#slideshowLayout').value,
      transition: document.querySelector('#slideshowTransition').value,
      music_id: slideshowMusic.value,
      loop_playback: document.querySelector('#slideshowLoop').checked,
    });
    if (!result.accepted) setSlideshowBusy(false);
  } finally {
    slideshowSubmitting = false;
    if (slideshowJobController.activeJobId()) setSlideshowBusy(true, {accepted: true});
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
      const selected = collections.find((collection) => collection.id === activeCollection);
      await api(`/api/collections/${activeCollection}`, { method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name, visibility:document.querySelector('#collectionVisibility').value, version:selected?.version}) });
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
      await api(`/api/collections/${selected.id}`, {method: 'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({version:selected.version})});
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
  const selectedPhotos = loadedPhotos.filter((photo) => selectedIds.has(photo.id));
  const onlyOwned = !selectedPhotos.length || selectedPhotos.every((photo) => photo.is_mine);
  const allClaimed = !selectedPhotos.length || selectedPhotos.every((photo) => photo.ownership_status === 'owned');
  document.querySelector('#batchOrganize').hidden = activeCollection === 'deleted' || !allClaimed;
  document.querySelector('#batchPrivacy').hidden = activeCollection === 'deleted' || !onlyOwned;
  document.querySelector('#batchShare').hidden = activeCollection === 'deleted' || !onlyOwned;
  document.querySelector('#batchRestore').hidden = activeCollection !== 'deleted' || !onlyOwned;
  const batchDelete = document.querySelector('#batchDelete');
  batchDelete.hidden = !onlyOwned || activeCollection === 'deleted';
  batchDelete.textContent = 'Delete';
  document.querySelector('#selectAllMedia').textContent = totalPhotos > 500 ? 'Select first 500' : 'Select all';
  document.querySelector('#toggleSelect').textContent = selectionMode ? 'Cancel' : 'Select';
  document.querySelector('#toggleSelect').setAttribute('aria-pressed', selectionMode ? 'true' : 'false');
  gallery.querySelectorAll('.photo').forEach((card) => {
    if (selectionMode) card.setAttribute('aria-pressed', selectedIds.has(card.dataset.photoId) ? 'true' : 'false');
    else card.removeAttribute('aria-pressed');
  });
  document.body.classList.toggle('selecting', selectionMode);
  updateGalleryDensityControls();
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

function enterSelectionMode() {
  const switchedToInteractive = galleryDensityLevel !== 0;
  if (switchedToInteractive) setGalleryDensity(0);
  selectionMode = true;
  updateSelectionBar();
  if (switchedToInteractive) {
    showToast('Switched to 3 across so media can be selected.');
  }
}

document.querySelector('#toggleSelect').addEventListener('click', () => {
  if (selectionMode) exitSelectionMode();
  else enterSelectionMode();
});

function beginSelection() {
  if (!selectionMode) enterSelectionMode();
  requestAnimationFrame(() => gallery.scrollIntoView({behavior: 'smooth', block: 'start'}));
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
  showToast('Permanent deletion stays unavailable until backup and retention verification passes.');
});

function organizationChanges() {
  const changes = [];
  organizeDesired.forEach((desired, collectionId) => {
    const original = organizeBaseline.get(collectionId);
    if (desired === original || desired === 'mixed') return;
    const collection = organizerCatalog.find((item) => item.id === collectionId);
    changes.push({
      collection_id: collectionId,
      collection_version: collection?.version,
      action: desired === 'all' ? 'add' : 'remove',
    });
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
  // The collection rail is already filtered by the server for the current
  // viewer. Reading it gives Android WebView an independent fallback when its
  // organizer request is restored from an empty or stale cache.
  collectionRail.querySelectorAll('.collection-card.dynamic[data-collection]').forEach((card) => {
    const id = card.dataset.collection;
    const name = card.querySelector('strong')?.textContent?.trim();
    if (id && name && !merged.has(id)) merged.set(id, {id, name});
  });
  return [...merged.values()];
}

function renderOrganizerCollections(membershipCollections) {
  const liveChecks = organizerRoot().querySelector('#collectionChecks') || collectionChecks;
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
  const liveChecks = organizerRoot().querySelector('#collectionChecks') || collectionChecks;
  if (liveChecks.querySelectorAll('.collection-check').length === organizerCatalog.length) return;
  renderOrganizerCollections(organizerCatalog);
}

function startOrganizerWatchdog() {
  if (organizerWatchdog) organizerWatchdog.disconnect();
  organizerWatchdog = new MutationObserver(() => ensureOrganizerCollectionsVisible());
  organizerWatchdog.observe(organizerRoot(), {childList: true, subtree: true});
  requestAnimationFrame(ensureOrganizerCollectionsVisible);
  setTimeout(ensureOrganizerCollectionsVisible, 150);
  setTimeout(ensureOrganizerCollectionsVisible, 700);
}

function organizerRoot() {
  return organizeSheet.__davidPiMobileHost || organizeSheet;
}

function organizerIsOpen() {
  return organizeSheet.open || Boolean(organizeSheet.__davidPiMobileHost);
}

function showOrganizerSheet() {
  // Use the same modal/top-layer host as the viewer and collection editor.
  // A fixed section cannot ever appear above a native modal, regardless of z-index.
  if (!organizerIsOpen()) organizeSheet.showModal();
  document.documentElement.classList.add('organizer-is-open');
  requestAnimationFrame(() => document.querySelector('#closeOrganizer')?.focus({preventScroll: true}));
}

function hideOrganizerSheet() {
  if (organizerIsOpen()) organizeSheet.close();
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
  document.querySelector('#organizeTitle').textContent = batch ? `Organize ${organizeIds.length} item${organizeIds.length === 1 ? '' : 's'}` : 'Organize media';
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
        api(`/api/collections?view=${encodeURIComponent(ownerView === 'mine' ? 'mine' : '')}`),
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
organizeSheet.addEventListener('cancel', (event) => {
  event.preventDefault();
  closeOrganizer();
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
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({items: versionedPhotoItems(organizeIds), changes}),
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
  await api('/api/photos/restore', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({items:versionedPhotoItems(ids)})});
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
    const result = await api('/api/photos/visibility', {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:versionedPhotoItems([photo.id]),visibility})});
    applyReturnedMediaVersions(result);
    photo.visibility = visibility;
    showToast(visibility === 'private' ? 'Media is now private.' : 'Media shared.');

    // The Shared view contains shared media only. Remove a newly private item locally so
    // the gallery stays truthful, but keep the viewer open on the next item.
    if (visibility === 'private' && ownerView === 'shared') {
      const removedIndex = currentPhotoIndex;
      loadedPhotos.splice(removedIndex, 1);
      loadedPhotoIds.delete(photo.id);
      totalPhotos = Math.max(0, totalPhotos - 1);
      rebuildGalleryWindow();
      if (
        !activeCollection && !activePeriod
        && mediaKind === 'all' && !favoriteOnly
      ) {
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
document.querySelector('#addToMytube').addEventListener('click', async () => {
  const photo = loadedPhotos[currentPhotoIndex];
  const button = document.querySelector('#addToMytube');
  if (!photo?.is_video || !photo.is_mine) return;
  button.disabled = true;
  try {
    if (photo.mytube_linked) {
      await api(`/api/mytube/media-links/${encodeURIComponent(photo.id)}`, {method: 'DELETE'});
      photo.mytube_linked = false;
      showToast('Removed from MyTube. The original video is unchanged.');
    } else {
      await api('/api/mytube/media-links', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({media_id: photo.id}),
      });
      photo.mytube_linked = true;
      showToast('Added to MyTube.');
    }
    button.textContent = photo.mytube_linked ? 'Remove from MyTube' : 'Add to MyTube';
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
});
document.querySelector('#batchPrivacy').addEventListener('click', async () => {
  const ids=[...selectedIds]; if(!ids.length)return;
  const savedPosition=captureGalleryPosition();
  const result = await api('/api/photos/visibility',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:versionedPhotoItems(ids),visibility:'private'})});
  applyReturnedMediaVersions(result);
  if(ownerView==='shared') removeLoadedPhotos(ids); else loadedPhotos.forEach((photo)=>{if(ids.includes(photo.id))photo.visibility='private';});
  showToast(`${ids.length} item${ids.length===1?' is':'s are'} now private.`); exitSelectionMode(); await loadCollections(); restoreGalleryPosition(savedPosition);
});
document.querySelector('#batchShare').addEventListener('click', async () => {
  const ids=[...selectedIds]; if(!ids.length)return;
  const savedPosition=captureGalleryPosition();
  const result = await api('/api/photos/visibility',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:versionedPhotoItems(ids),visibility:'shared'})});
  applyReturnedMediaVersions(result);
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
  if (permanent) {
    showToast('Permanent deletion stays unavailable until backup and retention verification passes.');
    return;
  }
  askConfirmation({
    title: permanent ? 'Delete forever?' : 'Move to Recently Deleted?',
    text: permanent ? `“${photo.original_name}” will be permanently removed from the Pi. This cannot be undone.` : `“${photo.original_name}” stays in Recently Deleted until an owner restores or permanently deletes it.`,
    button: permanent ? 'Delete forever' : 'Move photo',
    action: async () => {
      const result = await api('/api/photos/trash', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({items:versionedPhotoItems([photo.id])})});
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
  if (permanent) {
    showToast('Permanent deletion stays unavailable until backup and retention verification passes.');
    return;
  }
  askConfirmation({
    title: permanent ? `Delete ${ids.length} items forever?` : `Delete ${ids.length} items?`,
    text: permanent ? 'These originals will be permanently removed from the Pi. This cannot be undone.' : 'They will remain in Recently Deleted until an owner restores or permanently deletes them.',
    button: permanent ? 'Delete forever' : 'Move items',
    action: async () => {
      const savedPosition = captureGalleryPosition();
      const result = await api('/api/photos/trash', {method: 'POST', headers: {'Content-Type':'application/json'}, body:JSON.stringify({items:versionedPhotoItems(ids)})});
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
  await api(`/api/collections/${selected.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:selected.name,visibility,version:selected.version})});
  showToast(visibility==='private'?'Collection is now private. Its existing media stayed unchanged.':'Collection shared.');
  await loadCollections(); updateCollectionToolbar();
});

function syncDiscoveryControls() {
  document.querySelector('.media-discovery').hidden = activeCollection === 'deleted';
  mediaFilters.hidden = activeCollection === 'deleted';
  document.querySelectorAll('[data-kind]').forEach((button) => {
    const selected = button.dataset.kind === mediaKind;
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-pressed', selected ? 'true' : 'false');
  });
  const favoriteButton = document.querySelector('[data-favorite]');
  favoriteButton.classList.toggle('selected', favoriteOnly);
  favoriteButton.setAttribute('aria-pressed', favoriteOnly ? 'true' : 'false');
}

async function refreshDiscovery() {
  exitSelectionMode();
  updateGallerySummary();
  try {
    await Promise.all([refreshPhotos(), refreshTimelineIfOpen()]);
  } catch (_error) {
    showGalleryLoadError('initial');
  }
}

document.querySelector('.media-smart-views').addEventListener('click', async (event) => {
  const kindButton = event.target.closest('[data-kind]');
  const favoriteButton = event.target.closest('[data-favorite]');
  if (!kindButton && !favoriteButton) return;
  if (kindButton) {
    if (kindButton.dataset.kind === mediaKind) return;
    mediaKind = kindButton.dataset.kind;
  } else {
    favoriteOnly = !favoriteOnly;
  }
  syncDiscoveryControls();
  await refreshDiscovery();
});

syncDiscoveryControls();
document.querySelector('#mediaOwners').addEventListener('click',async(event)=>{
  const button=event.target.closest('[data-owner]'); if(!button)return;
  ownerView=button.dataset.owner||'visible'; activePeriod=''; document.querySelectorAll('#mediaOwners [data-owner]').forEach((item)=>{const selected=item===button;item.classList.toggle('selected',selected);item.setAttribute('aria-pressed',selected?'true':'false');});
  updateGallerySummary();
  try {
    await Promise.all([loadCollections(),refreshPhotos(),refreshTimelineIfOpen()]);
  } catch (_error) {
    showGalleryLoadError('initial');
  }
});

loadMore.addEventListener('click', () => loadPhotos());
function updateGalleryAutoPage(isIntersecting) {
  galleryAutoPageGate.update(
    isIntersecting,
    hasMorePhotos && !loadingPhotos && !loadMore.hidden,
    () => loadPhotos(),
  );
}

function galleryLoadMoreIsNearViewport() {
  const bounds = loadMore.getBoundingClientRect();
  return bounds.top <= window.innerHeight + 64 && bounds.bottom >= 0;
}

let galleryViewportCheckQueued = false;
function queueGalleryViewportCheck() {
  if (galleryViewportCheckQueued) return;
  galleryViewportCheckQueued = true;
  requestAnimationFrame(() => {
    galleryViewportCheckQueued = false;
    updateGalleryAutoPage(galleryLoadMoreIsNearViewport());
  });
}

function grantGalleryAutoPageIntent() {
  galleryAutoPageGate.grantIntent();
  queueGalleryViewportCheck();
}

const galleryUsesIntersectionObserver = typeof window.IntersectionObserver === 'function';
if (galleryUsesIntersectionObserver) {
  const galleryLoader = new window.IntersectionObserver((entries) => {
    updateGalleryAutoPage(entries.some((entry) => entry.isIntersecting));
  }, {rootMargin: '0px 0px 64px 0px'});
  galleryLoader.observe(loadMore);
}

// One passive scroll path drives the arithmetic row window. Paging remains
// observer-driven; only legacy browsers reuse this frame for the sentinel
// fallback, and neither path invents user intent.
window.addEventListener('scroll', () => {
  queueGalleryWindowRender();
  if (!galleryUsesIntersectionObserver && galleryAutoPageGate.hasIntent()) {
    queueGalleryViewportCheck();
  }
}, {passive: true});

const galleryResizeFrame = galleryWindowApi.createFrameScheduler(() => {
  const previousWidth = galleryMeasuredWidth;
  const previousViewportHeight = galleryMeasuredViewportHeight;
  measureGalleryWindow();
  const position = captureGalleryPosition();
  if (
    Math.abs(galleryMeasuredWidth - previousWidth) > 0.5
    || Math.abs(galleryMeasuredViewportHeight - previousViewportHeight) > 0.5
  ) {
    galleryWindowManager.setModel(galleryWindowModel());
    restoreGalleryPositionNow(position);
  } else {
    renderGalleryWindowNow();
  }
});
window.addEventListener('resize', () => galleryResizeFrame.request(), {passive: true});

let galleryWheelGestureActive = false;
let galleryWheelGestureTimer = null;
window.addEventListener('wheel', (event) => {
  if (!event.target.closest?.('.gallery-shell') || viewer.open) return;
  if (!galleryWheelGestureActive) {
    galleryWheelGestureActive = true;
    grantGalleryAutoPageIntent();
  }
  clearTimeout(galleryWheelGestureTimer);
  galleryWheelGestureTimer = setTimeout(() => { galleryWheelGestureActive = false; }, 240);
}, {passive: true});

let galleryTouchScroll = null;
window.addEventListener('touchstart', (event) => {
  if (
    event.touches.length !== 1
    || !event.target.closest?.('.gallery-shell')
    || viewer.open
  ) {
    galleryTouchScroll = null;
    return;
  }
  const touch = event.touches[0];
  galleryTouchScroll = {id: touch.identifier, y: touch.clientY, granted: false};
}, {passive: true, capture: true});
window.addEventListener('touchmove', (event) => {
  if (!galleryTouchScroll || event.touches.length !== 1 || galleryTouchScroll.granted) return;
  const touch = event.touches[0];
  if (touch.identifier !== galleryTouchScroll.id) return;
  if (Math.abs(touch.clientY - galleryTouchScroll.y) < 8) return;
  galleryTouchScroll.granted = true;
  grantGalleryAutoPageIntent();
}, {passive: true, capture: true});
window.addEventListener('touchend', () => { galleryTouchScroll = null; }, {passive: true, capture: true});
window.addEventListener('touchcancel', () => { galleryTouchScroll = null; }, {passive: true, capture: true});

window.addEventListener('keydown', (event) => {
  if (
    event.defaultPrevented || event.repeat || event.altKey || event.ctrlKey || event.metaKey
    || !['ArrowDown', 'PageDown', 'End', ' '].includes(event.key)
    || event.target.closest?.('input, textarea, select, button, [contenteditable="true"]')
    || viewer.open
  ) return;
  grantGalleryAutoPageIntent();
});
document.querySelector('#closeViewer').addEventListener('click', () => viewer.close());
document.querySelector('#previousPhoto').addEventListener('click', () => movePhoto(-1));
document.querySelector('#nextPhoto').addEventListener('click', () => movePhoto(1));
viewer.addEventListener('click', (event) => { if (event.target === viewer) viewer.close(); });
viewer.addEventListener('close', () => {
  viewerLoadGeneration += 1;
  clearTimeout(viewerRenditionTimer);
  pointerPanId = null;
  currentPhotoIndex = -1;
  viewerStage.classList.remove('loading');
  viewerImage.removeAttribute('src');
  stopViewerVideo({clearPoster: true});
  resetViewerZoom();
  resetViewerTouch();
  setViewerCaptionExpanded(false);
});
viewerZoomOut.addEventListener('click', () => setViewerZoom(zoom - .5, true));
viewerZoomIn.addEventListener('click', () => setViewerZoom(zoom + .5, true));
viewerZoomReset.addEventListener('click', () => resetViewerZoom(true));
viewerStage.addEventListener('wheel', (event) => {
  if (viewerImage.hidden) return;
  event.preventDefault();
  setViewerZoom(zoom + (event.deltaY < 0 ? .25 : -.25));
}, {passive: false});
viewerImage.addEventListener('pointerdown', (event) => {
  if (event.pointerType !== 'mouse' || event.button !== 0 || zoom <= 1.01) return;
  pointerPanId = event.pointerId;
  panStartX = event.clientX;
  panStartY = event.clientY;
  panOriginX = panX;
  panOriginY = panY;
  viewerImage.setPointerCapture(event.pointerId);
  event.preventDefault();
});
viewerImage.addEventListener('pointermove', (event) => {
  if (pointerPanId !== event.pointerId || zoom <= 1.01) return;
  panX = panOriginX + event.clientX - panStartX;
  panY = panOriginY + event.clientY - panStartY;
  clampViewerPan();
  applyViewerTransform();
});
function endPointerPan(event) {
  if (pointerPanId !== event.pointerId) return;
  pointerPanId = null;
  if (viewerImage.hasPointerCapture(event.pointerId)) viewerImage.releasePointerCapture(event.pointerId);
}
viewerImage.addEventListener('pointerup', endPointerPan);
viewerImage.addEventListener('pointercancel', endPointerPan);
viewerStage.addEventListener('pointerdown', (event) => {
  if (event.pointerType !== 'touch') return;
  viewerTouchPointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
  viewerStage.setPointerCapture?.(event.pointerId);
  if (viewerTouchPointers.size === 1) {
    touchStartX = event.clientX;
    touchStartY = event.clientY;
    touchSwipeActive = false;
    viewerGesturePinched = false;
  } else if (viewerTouchPointers.size === 2 && !viewerImage.hidden) {
    const points = [...viewerTouchPointers.values()];
    pinchActive = true;
    viewerGesturePinched = true;
    pinchDistance = pointerDistance(points);
    viewerPinchCenter = pointerCenter(points);
    event.preventDefault();
  }
}, {capture: true});

viewerStage.addEventListener('pointermove', (event) => {
  const previous = viewerTouchPointers.get(event.pointerId);
  if (!previous) return;
  viewerTouchPointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
  if (!viewerImage.hidden && viewerTouchPointers.size === 2) {
    const points = [...viewerTouchPointers.values()];
    const center = pointerCenter(points);
    const distance = pointerDistance(points);
    if (viewerPinchCenter) {
      panX += center.x - viewerPinchCenter.x;
      panY += center.y - viewerPinchCenter.y;
    }
    setViewerZoom(zoom * distance / Math.max(1, pinchDistance || distance), false, center);
    pinchDistance = distance;
    viewerPinchCenter = center;
    pinchActive = true;
    viewerGesturePinched = true;
    event.preventDefault();
    return;
  }
  if (!viewerImage.hidden && viewerTouchPointers.size === 1 && zoom > 1.01) {
    panX += event.clientX - previous.x;
    panY += event.clientY - previous.y;
    clampViewerPan();
    applyViewerTransform();
    event.preventDefault();
    return;
  }
  if (viewerTouchPointers.size === 1 && zoom <= 1.01 && touchStartX !== null) {
    const dx = event.clientX - touchStartX;
    const dy = event.clientY - touchStartY;
    if (Math.abs(dx) > 12 && Math.abs(dx) > Math.abs(dy) * 1.2) {
      touchSwipeActive = true;
      event.preventDefault();
    }
  }
}, {passive: false, capture: true});

function finishViewerTouchPointer(event, cancelled = false) {
  if (!viewerTouchPointers.has(event.pointerId)) return;
  const wasPinched = viewerGesturePinched;
  viewerTouchPointers.delete(event.pointerId);
  if (viewerStage.hasPointerCapture?.(event.pointerId)) viewerStage.releasePointerCapture(event.pointerId);
  if (viewerTouchPointers.size === 1) {
    pinchActive = false;
    pinchDistance = 0;
    viewerPinchCenter = null;
    return;
  }
  if (viewerTouchPointers.size) return;
  if (!cancelled && !wasPinched && zoom <= 1.01 && touchStartX !== null) {
    const dx = event.clientX - touchStartX;
    const dy = event.clientY - touchStartY;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.25) {
      event.preventDefault();
      event.stopPropagation();
      movePhoto(dx > 0 ? -1 : 1);
    } else if (!viewerImage.hidden && !touchSwipeActive && Math.hypot(dx, dy) < 12) {
      const now = Date.now();
      if (now - lastViewerTapAt < 320) {
        event.preventDefault();
        if (zoom > 1.01) resetViewerZoom(true);
        else setViewerZoom(2.5, true, {x: event.clientX, y: event.clientY});
        lastViewerTapAt = 0;
      } else {
        lastViewerTapAt = now;
      }
    }
  }
  if (zoom <= 1.01) resetViewerZoom(true);
  resetViewerTouch();
}

viewerStage.addEventListener('pointerup', (event) => finishViewerTouchPointer(event), {capture: true});
viewerStage.addEventListener('pointercancel', (event) => finishViewerTouchPointer(event, true), {capture: true});
window.addEventListener('resize', () => {
  clampViewerPan();
  applyViewerTransform();
}, {passive: true});
viewerImage.addEventListener('dblclick', () => {
  if (zoom > 1.01) resetViewerZoom(true);
  else setViewerZoom(2.5, true);
});
document.addEventListener('keydown', (event) => {
  if (!viewer.open) return;
  if (event.target.closest('input, textarea, select, button, a')) return;
  if (event.key === 'ArrowLeft') movePhoto(-1);
  if (event.key === 'ArrowRight') movePhoto(1);
  if (event.key === '+' || event.key === '=') setViewerZoom(zoom + .5, true);
  if (event.key === '-') setViewerZoom(zoom - .5, true);
  if (event.key === '0' || event.key.toLowerCase() === 'f') resetViewerZoom(true);
});

if (new URLSearchParams(location.search).has('upload')) openUpload();
function showGalleryLoadError(mode = 'initial') {
  galleryRetryMode = mode;
  galleryStatus.textContent = 'The gallery could not load.';
  if (loadedPhotos.length) {
    galleryErrorTitle.textContent = mode === 'page'
      ? 'More media could not load.'
      : 'Some library controls could not load.';
    galleryErrorCopy.textContent = 'Everything already on screen is still available. Retry when the connection is ready.';
    galleryPageError.hidden = false;
    loadMore.hidden = true;
    return;
  }
  emptyState.hidden = false;
  emptyState.querySelector('h2').textContent = 'Media is unavailable right now.';
  emptyState.querySelector('p').textContent = 'The gallery could not load. Please try again in a moment.';
  emptyState.querySelector('[data-upload]').hidden = true;
  emptyBrowseAll.hidden = true;
  retryGallery.hidden = false;
  galleryResultCount.textContent = 'Unavailable';
}

async function loadInitialGallery() {
  retryGallery.hidden = true;
  galleryPageError.hidden = true;
  emptyState.hidden = true;
  galleryStatus.textContent = 'Loading media…';
  try {
    const requestedCollection = activeCollection;
    const collectionsRequest = loadCollections().then(() => true).catch(() => false);
    let photosLoaded = await refreshPhotos();
    const collectionsLoaded = await collectionsRequest;
    queueGalleryOriginResync();
    // Collection deep links historically recovered to All media when a
    // collection was removed or no longer visible. Keep that behavior even
    // though the initial photo and collection requests now start together.
    if (collectionsLoaded && requestedCollection && activeCollection !== requestedCollection) {
      photosLoaded = await refreshPhotos();
    }
    if (!photosLoaded) return;
    if (!collectionsLoaded) {
      showToast('Collections could not load. Your photos remain available.');
    }
  } catch {
    showGalleryLoadError();
  }
}

retryGallery.addEventListener('click', loadInitialGallery);
retryGalleryPage.addEventListener('click', async () => {
  galleryPageError.hidden = true;
  if (galleryRetryMode === 'page' && loadedPhotos.length) await loadPhotos();
  else await loadInitialGallery();
});
emptyBrowseAll.addEventListener('click', async () => {
  if (favoriteOnly) {
    favoriteOnly = false;
    syncDiscoveryControls();
    await refreshDiscovery();
  } else if (activePeriod) await setTimelinePeriod('');
  else await selectCollection('');
});
loadInitialGallery().finally(() => slideshowJobController?.resume());
