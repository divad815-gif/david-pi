const csrf = document.querySelector('meta[name="csrf-token"]').content;
const grid = document.querySelector('#movieGrid');
const empty = document.querySelector('#moviesEmpty');
const toast = document.querySelector('#platformToast');
let view = 'all';
let mediaType = 'movie';
let searchGeneration = 0;
let removeId = null;

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
  available.textContent = availabilityText(item);
  const meta = document.createElement('small');
  meta.textContent = `Added by ${item.added_by[0].toUpperCase()}${item.added_by.slice(1)}${item.last_checked ? ` · Checked ${new Date(item.last_checked).toLocaleDateString()}` : ''}`;
  const actions = document.createElement('div');
  actions.className = 'movie-card-actions';
  const watched = document.createElement('button');
  watched.textContent = item.watched ? 'Mark unwatched' : 'Mark watched';
  watched.addEventListener('click', async () => {
    await api(`/api/movies/${item.id}/watched`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({watched:!item.watched})});
    await loadMovies();
  });
  const remove = document.createElement('button');
  remove.className = 'danger-text';
  remove.textContent = 'Remove';
  remove.addEventListener('click', () => {
    removeId = item.id;
    document.querySelector('#movieConfirm h2').textContent = `Remove this ${typeName()}?`;
    document.querySelector('#movieConfirm').showModal();
  });
  actions.append(watched, remove);
  body.append(title, overview, available, meta, actions);
  article.append(poster, body);
  return article;
}

async function loadMovies() {
  const q = document.querySelector('#watchlistSearch').value;
  const data = await api(`/api/movies?${new URLSearchParams({view, q, type:mediaType})}`);
  grid.replaceChildren(...data.movies.map(movieCard));
  empty.hidden = data.movies.length !== 0;
  empty.querySelector('p').textContent = `Add a ${typeName()} you both want to see.`;
  empty.querySelector('button').textContent = `Add a ${typeName()}`;
  const available = data.movies.filter((item) => item.available_now).length;
  document.querySelector('#availabilitySummary').textContent = available
    ? `${available} watchlist ${available === 1 ? typeName() : typeName(true)} included with your services.`
    : 'See what’s ready on your services.';
}

function openSearch() {
  document.querySelector('#movieSearchMessage').textContent = '';
  document.querySelector('#movieSearchResults').replaceChildren();
  document.querySelector('#movieSearchInput').value = '';
  document.querySelector('#movieSearchSheet').showModal();
  setTimeout(() => document.querySelector('#movieSearchInput').focus(), 40);
}

document.querySelector('#openMovieSearch').addEventListener('click', openSearch);
document.querySelector('#emptyAddMovie').addEventListener('click', openSearch);
document.querySelector('#closeMovieSearch').addEventListener('click', () => document.querySelector('#movieSearchSheet').close());

let movieSearchTimer;
document.querySelector('#movieSearchInput').addEventListener('input', () => {
  clearTimeout(movieSearchTimer);
  movieSearchTimer = setTimeout(searchMovies, 350);
});

async function searchMovies() {
  const query = document.querySelector('#movieSearchInput').value.trim();
  const generation = ++searchGeneration;
  const results = document.querySelector('#movieSearchResults');
  const message = document.querySelector('#movieSearchMessage');
  message.textContent = '';
  if (query.length < 2) { results.replaceChildren(); return; }
  results.replaceChildren();
  message.textContent = `Searching for ${typeName(true)}…`;
  try {
    const data = await api(`/api/movies/search?${new URLSearchParams({q:query, type:mediaType})}`);
    if (generation !== searchGeneration) return;
    message.textContent = '';
    results.replaceChildren();
    if (!data.results.length) { message.textContent = data.message || 'No matches found.'; return; }
    data.results.forEach((item) => {
      const row = document.createElement('article');
      row.tabIndex = 0;
      row.setAttribute('role', 'button');
      row.setAttribute('aria-label', `Add ${item.title} to the watchlist`);
      const poster = posterNode(item.poster_url, 'search-poster-fallback');
      const copy = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = `${item.title}${item.release_year ? ` (${item.release_year})` : ''}`;
      const overview = document.createElement('small');
      overview.textContent = item.overview || 'No description available.';
      const add = document.createElement('button');
      add.type = 'button';
      add.textContent = 'Add';
      const choose = () => addMovie(item, add);
      add.addEventListener('click', (event) => { event.stopPropagation(); choose(); });
      row.addEventListener('click', choose);
      row.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); choose(); }
      });
      copy.append(title, overview);
      row.append(poster, copy, add);
      results.append(row);
    });
  } catch (error) {
    if (generation !== searchGeneration) return;
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
    await api('/api/movies', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...item, media_type:mediaType})});
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

document.querySelector('#mediaTypeTabs').addEventListener('click', (event) => {
  const button = event.target.closest('[data-type]');
  if (!button || button.dataset.type === mediaType) return;
  mediaType = button.dataset.type;
  view = 'all';
  searchGeneration += 1;
  document.querySelectorAll('#mediaTypeTabs button').forEach((item) => item.classList.toggle('selected', item === button));
  document.querySelectorAll('#movieViews button').forEach((item) => item.classList.toggle('selected', item.dataset.view === 'all'));
  document.querySelector('#movieSearchInput').placeholder = `${typeName()} title`;
  document.querySelector('#watchlistSearch').placeholder = `Search our ${typeName()} watchlist`;
  document.querySelector('#movieSearchSheet h2').textContent = `Add a ${typeName()}`;
  document.querySelector('#movieSearchHelp').textContent = `Search TMDB, then tap Add beside the ${typeName()} you want.`;
  document.querySelector('#movieSearchMessage').textContent = '';
  document.querySelector('#manualMovieSheet h2').textContent = `${typeName()[0].toUpperCase()}${typeName().slice(1)} details`;
  document.querySelector('#movieSearchResults').replaceChildren();
  loadMovies();
});

document.querySelector('#movieViews').addEventListener('click', (event) => {
  const button = event.target.closest('[data-view]');
  if (!button) return;
  view = button.dataset.view;
  document.querySelectorAll('#movieViews button').forEach((item) => item.classList.toggle('selected', item === button));
  loadMovies();
});
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
    showToast(`${data.available} watchlist ${data.available === 1 ? typeName() : typeName(true)} ${data.available === 1 ? 'is' : 'are'} included.`);
    await loadMovies();
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = 'Check availability';
  }
});

document.querySelector('#openSubscriptions').addEventListener('click', async () => {
  const data = await api('/api/movies/subscriptions');
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
});
document.querySelector('#closeSubscriptions').addEventListener('click', () => document.querySelector('#subscriptionsSheet').close());
document.querySelector('#saveSubscriptions').addEventListener('click', async () => {
  const ids = [...document.querySelectorAll('#subscriptionList input:checked')].map((input) => Number(input.value));
  await api('/api/movies/subscriptions', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({provider_ids:ids})});
  document.querySelector('#subscriptionsSheet').close();
  showToast('Services saved.');
  await loadMovies();
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
    await api(`/api/movies/${removeId}`, {method:'DELETE'});
    document.querySelector('#movieConfirm').close();
    showToast('Removed from watchlist.');
    await loadMovies();
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    removeId = null;
  }
});

loadMovies().catch((error) => showToast(error.message));
