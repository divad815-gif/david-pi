const csrf = document.querySelector('meta[name="csrf-token"]').content;
const grid = document.querySelector('#movieGrid');
const empty = document.querySelector('#moviesEmpty');
const toast = document.querySelector('#platformToast');
const moviesPanel = window.AsyncPanel.create({
  root: '#moviesPanel', loading: '#moviesLoading', content: '#movieGrid',
  empty: '#moviesEmpty', noResults: '#moviesNoResults', error: '#moviesError',
});
const moviesAnnouncer = window.DavidPiAnnouncer.create();
const resultCount = document.querySelector('#movieResultCount');
const moviesMore = document.querySelector('#moviesMore');
const moviesPanelRoot = document.querySelector('#moviesPanel');
const pageSize = 24;
let view = 'all';
let mediaType = 'movie';
let searchGeneration = 0;
let loadingGeneration = 0;
let loadedCount = 0;
let removeTarget = null;
let subscriptionVersion = null;
let activeListController = null;
let activeSearchController = null;

const typeName = (plural = false) => mediaType === 'tv'
  ? (plural ? 'TV shows' : 'TV show')
  : (plural ? 'movies' : 'movie');

async function api(url, options = {}) {
  options.headers = {...options.headers, 'X-CSRF-Token': csrf};
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Something went wrong.');
  return data;
}

function showToast(text) {
  toast.textContent = text;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, 3000);
}

function availabilityText(item) {
  const included = item.availability?.filter((provider) => provider.kind === 'included' && provider.subscribed) || [];
  return included.length
    ? `Included with ${included.map((provider) => provider.provider_name).join(', ')}`
    : item.last_checked ? 'Not included with your selected services' : 'Availability not checked';
}

function posterNode(url, fallbackClass = 'poster-fallback') {
  if (!url) return Object.assign(document.createElement('div'), {className: fallbackClass, textContent: '▶'});
  const image = Object.assign(document.createElement('img'), {
    src: url, alt: '', loading: 'lazy', decoding: 'async', referrerPolicy: 'no-referrer',
  });
  image.addEventListener('error', () => {
    image.replaceWith(Object.assign(document.createElement('div'), {className: fallbackClass, textContent: '▶'}));
  }, {once: true});
  return image;
}

function movieCard(item) {
  const article = document.createElement('article');
  article.className = 'movie-card';
  const poster = posterNode(item.poster_url);
  const body = document.createElement('div');
  body.className = 'movie-copy';
  const title = document.createElement('h3');
  title.textContent = `${item.title}${item.release_year ? ` (${item.release_year})` : ''}`;
  const overview = document.createElement('p');
  overview.textContent = item.overview || 'No description yet.';
  const available = document.createElement('strong');
  available.className = item.available_now ? 'available-copy' : 'availability-copy';
  available.textContent = item.deleted_at ? 'In trash · tap Restore to return it' : availabilityText(item);
  const meta = document.createElement('small');
  meta.textContent = `${item.owner_name ? `Added by ${item.owner_name}` : 'Shared legacy title'}${item.last_checked ? ` · Checked ${new Date(item.last_checked).toLocaleDateString()}` : ''}`;
  const actions = document.createElement('div');
  actions.className = 'movie-card-actions';
  if (item.deleted_at && item.can_restore) {
    const restore = document.createElement('button');
    restore.type = 'button'; restore.textContent = 'Restore';
    restore.addEventListener('click', async () => {
      restore.disabled = true;
      try { await api(`/api/movies/${item.id}/restore`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({version:item.version})}); showToast(`${item.title} restored.`); await loadMovies(); }
      catch (error) { showToast(error.message); restore.disabled = false; }
    });
    actions.append(restore);
  } else {
    const watched = document.createElement('button');
    watched.type = 'button';
    watched.textContent = item.watched ? 'Mark unwatched' : 'Mark watched';
    watched.addEventListener('click', async () => {
      watched.disabled = true;
      try {
        await api(`/api/movies/${item.id}/watched`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({watched:!item.watched, state_version:item.state_version})});
        await loadMovies();
      } catch (error) {
        showToast(error.message);
      } finally {
        watched.disabled = false;
      }
    });
    actions.append(watched);
  }
  if (!item.deleted_at && item.can_edit) {
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'danger-text';
    remove.textContent = 'Move to trash';
    remove.addEventListener('click', () => {
      removeTarget = item;
      document.querySelector('#movieConfirm h2').textContent = `Move this ${typeName()} to trash?`;
      document.querySelector('#movieConfirm').showModal();
    });
    actions.append(remove);
  } else if (!item.deleted_at && item.legacy_read_only) {
    const notice = document.createElement('small');
    notice.textContent = 'Shared legacy title · read-only until ownership is reviewed';
    actions.append(notice);
  }
  body.append(title, overview, available, meta, actions);
  article.append(poster, body);
  return article;
}

async function loadMovies({append = false} = {}) {
  const generation = ++loadingGeneration;
  activeListController?.abort();
  activeListController = new AbortController();
  const q = document.querySelector('#watchlistSearch').value.trim();
  const offset = append ? loadedCount : 0;
  if (append) {
    moviesPanelRoot.setAttribute('aria-busy', 'true');
    moviesMore.hidden = false;
    moviesMore.disabled = true;
    moviesMore.textContent = 'Loading more…';
  } else {
    loadedCount = 0;
    moviesMore.hidden = true;
    moviesPanel.begin(generation);
    resultCount.hidden = true;
  }
  try {
    const data = await api(`/api/movies?${new URLSearchParams({view, q, type:mediaType, limit:pageSize, offset})}`, {signal: activeListController.signal});
    if (generation !== loadingGeneration) return;
    const movies = Array.isArray(data.movies) ? data.movies : [];
    const cards = movies.map(movieCard);
    if (append) grid.append(...cards);
    else grid.replaceChildren(...cards);
    loadedCount = offset + movies.length;
    empty.querySelector('h2').textContent = view === 'trash' ? 'Trash is empty.' : 'Your watchlist is ready.';
    empty.querySelector('p').textContent = view === 'trash' ? `Removed ${typeName(true)} remain recoverable for at least 30 days.` : `Add a ${typeName()} your household wants to see.`;
    empty.querySelector('button').hidden = view === 'trash';
    empty.querySelector('button').textContent = `Add a ${typeName()}`;
    const count = Number.isInteger(data.count) ? data.count : movies.length;
    resultCount.textContent = `${count} ${count === 1 ? typeName() : typeName(true)}`;
    resultCount.hidden = false;
    const filtered = Boolean(q) || view !== 'all';
    if (append) moviesPanelRoot.setAttribute('aria-busy', 'false');
    else moviesPanel.success(generation, {empty: count === 0 && !filtered, noResults: count === 0 && filtered});
    const hasMore = typeof data.has_more === 'boolean' ? data.has_more : loadedCount < count;
    moviesMore.hidden = !hasMore;
    moviesMore.disabled = false;
    moviesMore.textContent = 'Load more';
    moviesAnnouncer.polite(count
      ? `${loadedCount} of ${count} ${typeName(true)} loaded.`
      : (filtered ? 'No matching titles.' : `No ${typeName(true)} yet.`));
    const available = Number.isInteger(data.available_count)
      ? data.available_count
      : movies.filter((item) => item.available_now).length;
    document.querySelector('#availabilitySummary').textContent = view === 'trash'
      ? 'Only titles you added appear here, and you can restore them.'
      : available
      ? `${available} watchlist ${available === 1 ? typeName() : typeName(true)} included with your services.`
      : 'See what’s ready on your services.';
  } catch (error) {
    if (error.name === 'AbortError' || generation !== loadingGeneration) return;
    if (append) {
      moviesPanelRoot.setAttribute('aria-busy', 'false');
      moviesMore.hidden = false;
      moviesMore.disabled = false;
      moviesMore.textContent = 'Try loading more';
      showToast(`More titles could not be loaded. ${error.message}`);
      moviesAnnouncer.alert(`More titles could not be loaded. ${error.message}`);
    } else {
      moviesMore.hidden = true;
      document.querySelector('#moviesErrorText').textContent = error.message;
      moviesPanel.transition('error');
      moviesAnnouncer.alert(`Watchlist could not be loaded. ${error.message}`);
    }
  }
}

function openSearch() {
  activeSearchController?.abort();
  searchGeneration += 1;
  document.querySelector('#movieSearchMessage').textContent = '';
  document.querySelector('#movieSearchResults').replaceChildren();
  document.querySelector('#movieSearchInput').value = '';
  document.querySelector('#movieVisibility').value = 'shared';
  if(window.DavidPiInstallation?.modules?.movies==='manual'){document.querySelector('#manualMovieMessage').textContent='';document.querySelector('#manualMovieSheet').showModal();document.querySelector('#manualMovieTitle').focus();return;}
  document.querySelector('#movieSearchSheet').showModal();
  setTimeout(() => document.querySelector('#movieSearchInput').focus(), 40);
}

document.querySelector('#openMovieSearch').addEventListener('click', openSearch);
document.querySelector('#emptyAddMovie').addEventListener('click', openSearch);
document.querySelector('#closeMovieSearch').addEventListener('click', () => {
  activeSearchController?.abort();
  document.querySelector('#movieSearchSheet').close();
});

let movieSearchTimer;
document.querySelector('#movieSearchInput').addEventListener('input', () => {
  clearTimeout(movieSearchTimer);
  movieSearchTimer = setTimeout(searchMovies, 350);
});

async function searchMovies() {
  const query = document.querySelector('#movieSearchInput').value.trim();
  const generation = ++searchGeneration;
  activeSearchController?.abort();
  activeSearchController = new AbortController();
  const results = document.querySelector('#movieSearchResults');
  const message = document.querySelector('#movieSearchMessage');
  message.textContent = '';
  if (query.length < 2) { results.replaceChildren(); return; }
  results.replaceChildren();
  message.textContent = `Searching for ${typeName(true)}…`;
  try {
    const data = await api(`/api/movies/search?${new URLSearchParams({q:query, type:mediaType})}`, {signal: activeSearchController.signal});
    if (generation !== searchGeneration) return;
    message.textContent = '';
    results.replaceChildren();
    if (!data.results.length) { message.textContent = data.message || 'No matches found.'; return; }
    data.results.forEach((item) => {
      const row = document.createElement('article');
      const poster = posterNode(item.poster_url, 'search-poster-fallback');
      const copy = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = `${item.title}${item.release_year ? ` (${item.release_year})` : ''}`;
      const overview = document.createElement('small');
      overview.textContent = item.overview || 'No description available.';
      const add = document.createElement('button');
      add.type = 'button';
      add.textContent = 'Add';
      add.setAttribute('aria-label', `Add ${item.title} to the watchlist`);
      add.addEventListener('click', () => addMovie(item, add));
      copy.append(title, overview);
      row.append(poster, copy, add);
      results.append(row);
    });
  } catch (error) {
    if (error.name === 'AbortError' || generation !== searchGeneration) return;
    results.replaceChildren();
    message.textContent = error.message;
  }
}

async function addMovie(item, button) {
  if (button.disabled) return;
  const message = document.querySelector('#movieSearchMessage');
  button.disabled = true;
  const originalText = button.textContent;
  button.textContent = 'Adding…';
  message.textContent = `Adding ${item.title}…`;
  try {
    await api('/api/movies', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...item, media_type:mediaType, visibility:document.querySelector('#movieVisibility').value})});
    message.textContent = '';
    document.querySelector('#movieSearchSheet').close();
    showToast(`${item.title} added to your watchlist.`);
    await loadMovies();
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

document.querySelector('#manualMovieButton').addEventListener('click', () => {
  document.querySelector('#movieSearchSheet').close();
  document.querySelector('#manualMovieSheet').showModal();
  document.querySelector('#manualMovieTitle').value = document.querySelector('#movieSearchInput').value;
});
document.querySelector('#closeManualMovie').addEventListener('click', () => document.querySelector('#manualMovieSheet').close());
document.querySelector('#saveManualMovie').addEventListener('click', async () => {
  const button = document.querySelector('#saveManualMovie');
  button.disabled = true;
  try {
    await api('/api/movies', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
      media_type:mediaType,
      title:document.querySelector('#manualMovieTitle').value,
      release_year:document.querySelector('#manualMovieYear').value,
      overview:document.querySelector('#manualMovieOverview').value,
      visibility:document.querySelector('#movieVisibility').value,
    })});
    document.querySelector('#manualMovieSheet').close();
    showToast('Added to your watchlist.');
    await loadMovies();
  } catch (error) {
    document.querySelector('#manualMovieMessage').textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

window.DavidPiFilterGroup.create('#mediaTypeTabs', {selector: '[data-type]', onChange: (button) => {
  if (button.dataset.type === mediaType) return;
  mediaType = button.dataset.type;
  view = 'all';
  searchGeneration += 1;
  movieViewFilters.select(document.querySelector('#movieViews [data-view="all"]'), false);
  document.querySelector('#movieSearchInput').placeholder = `${typeName()} title`;
  document.querySelector('#watchlistSearch').placeholder = `Search our ${typeName()} watchlist`;
  document.querySelector('#movieSearchSheet h2').textContent = `Add a ${typeName()}`;
  document.querySelector('#movieSearchHelp').textContent = `Search TMDB, then tap Add beside the ${typeName()} you want.`;
  document.querySelector('#movieSearchMessage').textContent = '';
  document.querySelector('#manualMovieSheet h2').textContent = `${typeName()[0].toUpperCase()}${typeName().slice(1)} details`;
  document.querySelector('#movieSearchResults').replaceChildren();
  loadMovies();
}});

const movieViewFilters = window.DavidPiFilterGroup.create('#movieViews', {selector: '[data-view]', onChange: (button) => {
  view = button.dataset.view;
  loadMovies();
}});
let listTimer;
document.querySelector('#watchlistSearch').addEventListener('input', () => {
  clearTimeout(listTimer);
  listTimer = setTimeout(loadMovies, 250);
});

document.querySelector('#checkMovies').addEventListener('click', async () => {
  const button = document.querySelector('#checkMovies');
  button.disabled = true;
  button.textContent = 'Checking…';
  try {
    const data = await api(`/api/movies/check?type=${mediaType}`, {method:'POST'});
    if (data.failed) {
      showToast(`${data.checked} checked · ${data.failed} could not be checked. Previous availability is kept for those titles.`);
    } else {
      showToast(`${data.available} watchlist ${data.available === 1 ? typeName() : typeName(true)} ${data.available === 1 ? 'is' : 'are'} included.`);
    }
    await loadMovies();
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = 'Check availability';
  }
});

document.querySelector('#openSubscriptions').addEventListener('click', async () => {
  const button = document.querySelector('#openSubscriptions');
  button.disabled = true;
  try {
    const data = await api('/api/movies/subscriptions');
    subscriptionVersion = data.version;
    const list = document.querySelector('#subscriptionList');
    list.replaceChildren();
    data.subscriptions.forEach((service) => {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = service.provider_id;
      input.checked = service.enabled;
      label.append(input, document.createTextNode(service.name));
      list.append(label);
    });
    document.querySelector('#subscriptionsSheet').showModal();
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
});
document.querySelector('#closeSubscriptions').addEventListener('click', () => document.querySelector('#subscriptionsSheet').close());
document.querySelector('#saveSubscriptions').addEventListener('click', async () => {
  const button = document.querySelector('#saveSubscriptions');
  const ids = [...document.querySelectorAll('#subscriptionList input:checked')].map((input) => Number(input.value));
  button.disabled = true;
  try {
    const result = await api('/api/movies/subscriptions', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({provider_ids:ids, version:subscriptionVersion})});
    subscriptionVersion = result.version;
    document.querySelector('#subscriptionsSheet').close();
    showToast('Services saved.');
    await loadMovies();
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
});
document.querySelector('#pickMovie').addEventListener('click', async () => {
  try {
    const data = await api(`/api/movies/pick?type=${mediaType}`);
    const pick = document.querySelector('#moviePick');
    pick.innerHTML = '<h2></h2><p></p><strong></strong>';
    pick.querySelector('h2').textContent = data.movie.title;
    pick.querySelector('p').textContent = data.movie.overview || 'Tonight’s choice.';
    pick.querySelector('strong').textContent = data.prioritized_available ? 'Available with one of your services' : `Picked from your unwatched ${typeName()} list`;
    document.querySelector('#moviePickSheet').showModal();
  } catch (error) {
    showToast(error.message);
  }
});
document.querySelector('#closeMoviePick').addEventListener('click', () => document.querySelector('#moviePickSheet').close());
document.querySelector('#cancelMovieConfirm').addEventListener('click', () => document.querySelector('#movieConfirm').close());
document.querySelector('#acceptMovieConfirm').addEventListener('click', async () => {
  const button = document.querySelector('#acceptMovieConfirm');
  button.disabled = true;
  try {
    await api(`/api/movies/${removeTarget.id}`, {method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({version:removeTarget.version})});
    document.querySelector('#movieConfirm').close();
    showToast('Moved to trash and restorable for at least 30 days.');
    await loadMovies();
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    removeTarget = null;
  }
});

document.querySelector('#moviesRetry').addEventListener('click', loadMovies);
moviesMore.addEventListener('click', () => loadMovies({append: true}));
loadMovies();
