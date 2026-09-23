const grid = document.querySelector('#collectionsPageGrid');
const status = document.querySelector('#collectionsPageStatus');
const errorPanel = document.querySelector('#collectionsPageError');
const retry = document.querySelector('#retryCollections');

async function collectionApi(url) {
  const response = await fetch(url, {headers: {'Accept': 'application/json'}});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || 'Collections could not load.');
  return body;
}

function collectionLink(collection) {
  const link = document.createElement('a');
  link.className = 'collection-card collection-page-card';
  link.href = `/photos?collection=${encodeURIComponent(collection.id)}`;
  const cover = document.createElement('span');
  cover.className = 'collection-cover';
  if (collection.cover) {
    const image = document.createElement('img');
    image.src = collection.cover;
    image.alt = '';
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
  link.append(cover, name, count);
  return link;
}

async function loadCollectionPage() {
  retry.disabled = true;
  errorPanel.hidden = true;
  grid.hidden = false;
  grid.setAttribute('aria-busy', 'true');
  status.hidden = false;
  status.textContent = 'Loading collections…';
  try {
    const [visible, mine] = await Promise.all([
      collectionApi('/api/collections'),
      collectionApi('/api/collections?view=mine'),
    ]);
    const merged = new Map();
    [...(visible.collections || []), ...(mine.collections || [])].forEach((item) => merged.set(item.id, item));
    const collections = [...merged.values()].sort((left, right) => (
      left.name.localeCompare(right.name, undefined, {sensitivity: 'base'}) || left.id.localeCompare(right.id)
    ));
    grid.replaceChildren(...collections.map(collectionLink));
    status.textContent = collections.length
      ? `${collections.length} collection${collections.length === 1 ? '' : 's'}`
      : 'No collections yet. Create one from Media.';
  } catch (_error) {
    grid.hidden = true;
    status.hidden = true;
    errorPanel.hidden = false;
  } finally {
    grid.setAttribute('aria-busy', 'false');
    retry.disabled = false;
  }
}

retry.addEventListener('click', loadCollectionPage);
loadCollectionPage();
