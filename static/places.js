const csrf = document.querySelector('meta[name="csrf-token"]').content;
let cuisines = JSON.parse(document.body.dataset.cuisines || '[]');
const state = {tab:'want_to_go', items:[], editItem:null, reviewItem:null, deleteItem:null, margItem:null, margaritas:[], placePhotos:[], placePhotoIndex:0, placePhotoTrigger:null};
const $ = (selector) => document.querySelector(selector);
const placesPanel = window.AsyncPanel.create({
  root:'#restaurantResults', loading:'#restaurantsLoading', content:'#restaurantList',
  empty:'#placesEmpty', noResults:'#placesNoResults', error:'#placesError',
});
const margaritasPanel = window.AsyncPanel.create({
  root:'#margaritaResults', loading:'#margaritasLoading', content:'#margGrid', error:'#margaritasError',
});
const placesAnnouncer = window.DavidPiAnnouncer.create();
let restaurantGeneration = 0;
let margaritaGeneration = 0;
let restaurantController = null;
let margaritaController = null;
let margaritaCalendar = 'legacy';
let margaritaConflict = null;
function calendarUrl(path, calendar = margaritaCalendar) {
  const url = new URL(path, location.origin);
  if (calendar !== 'legacy') url.searchParams.set('year', calendar);
  return `${url.pathname}${url.search}`;
}
function updateCalendarChoices(years = []) {
  const current = new Date().getFullYear();
  const choices = new Set([current - 1, current, current + 1, ...years]);
  if (margaritaCalendar !== 'legacy') choices.add(Number(margaritaCalendar));
  $('#margCalendar').replaceChildren(new Option('Year unconfirmed', 'legacy'),
    ...[...choices].sort((a, b) => b - a).map(year => new Option(String(year), String(year))));
  $('#margCalendar').value = margaritaCalendar;
  $('#margAssignYear').hidden = margaritaCalendar !== 'legacy';
  $('#margPreviousYear').disabled = margaritaCalendar === '1900';
  $('#margNextYear').disabled = margaritaCalendar === '2200';
}

async function api(url, options={}) {
  options.headers = {...options.headers, 'X-CSRF-Token':csrf};
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || 'Something went wrong.');
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}
function toast(text) {
  const node = $('#platformToast'); node.textContent = text; node.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { node.hidden = true; }, 2800);
}
function stars(value) {
  if (!value) return 'Not rated';
  const whole = Math.floor(value), half = value % 1 ? '½' : '';
  return `${'★'.repeat(whole)}${half}${'☆'.repeat(5 - Math.ceil(value))} · ${value}`;
}
function createCuisineChips(container, selected=[]) {
  container.replaceChildren();
  cuisines.forEach((name) => {
    const label = document.createElement('label');
    const input = Object.assign(document.createElement('input'), {type:'checkbox', value:name, checked:selected.includes(name)});
    label.append(input, document.createTextNode(name)); container.append(label);
  });
}
function selectedCuisines(container) {
  return [...container.querySelectorAll('input:checked')].map((input) => input.value);
}
function updateCuisineOptions(nextCuisines) {
  if (!Array.isArray(nextCuisines)) return;
  cuisines = nextCuisines;
  const editorSelected = selectedCuisines($('.editor-cuisines'));
  const chooserSelected = selectedCuisines($('.chooser-cuisines'));
  createCuisineChips($('.editor-cuisines'), editorSelected);
  createCuisineChips($('.chooser-cuisines'), chooserSelected);
}
function setBusy(button, busy, label) {
  button.disabled = busy; if (busy) { button.dataset.label = button.textContent; button.textContent = label; }
  else if (button.dataset.label) button.textContent = button.dataset.label;
}

async function loadRestaurants() {
  const generation = ++restaurantGeneration;
  restaurantController?.abort();
  restaurantController = new AbortController();
  const list = $('#restaurantList');
  const query = $('#restaurantSearch').value.trim();
  const sort = $('#restaurantSort').value;
  placesPanel.begin(generation);
  $('#restaurantResultCount').hidden = true;
  try {
    const data = await api(`/api/places/restaurants?${new URLSearchParams({view:state.tab, q:query, sort})}`, {signal:restaurantController.signal});
    if (generation !== restaurantGeneration) return;
    updateCuisineOptions(data.cuisines);
    state.items = Array.isArray(data.restaurants) ? data.restaurants : [];
    list.replaceChildren(...state.items.map(restaurantCard));
    $('#placesEmpty h2').textContent = state.tab === 'trash' ? 'Trash is empty.' : (state.tab === 'reviewed' ? 'No reviews yet.' : 'Start a restaurant list.');
    $('#placesEmpty p').textContent = state.tab === 'trash' ? 'Restaurants you remove remain recoverable for at least 30 days.' : (state.tab === 'reviewed' ? 'Check off a place after you go and leave a review.' : 'Add somewhere you both want to try.');
    $('#emptyAddRestaurant').hidden = state.tab === 'trash';
    const count = Number.isInteger(data.count) ? data.count : state.items.length;
    $('#restaurantResultCount').textContent = `${count} ${count === 1 ? 'restaurant' : 'restaurants'}`;
    $('#restaurantResultCount').hidden = false;
    placesPanel.success(generation, {empty:count === 0 && !query, noResults:count === 0 && Boolean(query)});
    placesAnnouncer.polite(count ? `${count} restaurants loaded.` : (query ? 'No matching restaurants.' : $('#placesEmpty h2').textContent));
  } catch (error) {
    if (error.name === 'AbortError' || generation !== restaurantGeneration) return;
    $('#placesErrorText').textContent = error.message;
    placesPanel.transition('error');
    placesAnnouncer.alert(`Restaurants could not be loaded. ${error.message}`);
  }
}
function restaurantCard(item) {
  const article = document.createElement('article'); article.className = 'restaurant-card';
  if (item.photos?.length) {
    const gallery = document.createElement('div'); gallery.className = 'restaurant-photo-gallery'; gallery.setAttribute('aria-label', `${item.name} photos`);
    item.photos.forEach((photo, index) => {
      const image = Object.assign(document.createElement('img'), {src:`${photo.url}?v=${encodeURIComponent(item.updated_at || '')}`, alt:`${item.name} photo ${index + 1} of ${item.photos.length}`, loading:'lazy'});
      image.tabIndex = 0;
      image.setAttribute('role', 'button');
      image.setAttribute('aria-label', `Open ${item.name} photo ${index + 1}`);
      image.addEventListener('click', () => openPlacePhotos(item, index, image));
      image.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openPlacePhotos(item, index, image); } });
      gallery.append(image);
    });
    article.append(gallery);
  }
  const top = document.createElement('div'); top.className = 'restaurant-card-top';
  if (item.status === 'want_to_go' && item.can_review) {
    const check = Object.assign(document.createElement('button'), {className:'went-check', textContent:'✓'});
    check.setAttribute('aria-label', `We went to ${item.name}`); check.title = 'We went here';
    check.addEventListener('click', () => openReview(item));
    top.append(check);
  }
  const copy = document.createElement('div'); copy.className = 'restaurant-copy';
  const title = document.createElement('h3'); title.textContent = item.name;
  const chips = document.createElement('p'); chips.className = 'restaurant-cuisines'; chips.textContent = item.cuisines.join(' · ') || 'Food type not added';
  copy.append(title, chips);
  if (item.status === 'reviewed') {
    const rating = document.createElement('strong'); rating.className = 'rating-copy'; rating.textContent = stars(item.rating);
    const visited = document.createElement('small'); visited.textContent = `Visited ${new Date(`${item.visited_at}T12:00:00`).toLocaleDateString()}${item.reviewed_by_name ? ` · Reviewed by ${item.reviewed_by_name}` : ''}`;
    copy.append(rating, visited);
    if (item.review) { const review = document.createElement('blockquote'); review.textContent = item.review; copy.append(review); }
  } else if (item.notes) {
    const note = document.createElement('p'); note.className = 'restaurant-notes'; note.textContent = item.notes; copy.append(note);
  }
  top.append(copy); article.append(top);
  const actions = document.createElement('div'); actions.className = 'restaurant-actions';
  if (item.deleted_at && item.can_restore) {
    const restore = Object.assign(document.createElement('button'), {textContent:'Restore'});
    restore.addEventListener('click', async () => {
      restore.disabled = true;
      try { await api(`/api/places/restaurants/${item.id}/restore`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({version:item.version})}); toast(`${item.name} restored.`); await loadRestaurants(); }
      catch (error) { toast(error.message); restore.disabled = false; }
    });
    actions.append(restore);
  } else if (item.can_edit) {
    const edit = Object.assign(document.createElement('button'), {textContent:'Edit'});
    edit.addEventListener('click', () => openRestaurant(item)); actions.append(edit);
  }
  if (!item.deleted_at && item.status === 'reviewed' && item.can_review) {
    const rereview = Object.assign(document.createElement('button'), {textContent:'Update review'});
    rereview.addEventListener('click', () => openReview(item)); actions.append(rereview);
  }
  if (!item.deleted_at && item.can_edit) {
    const remove = Object.assign(document.createElement('button'), {className:'danger-text', textContent:'Move to trash'});
    remove.addEventListener('click', () => { state.deleteItem = item; $('#placeConfirm').showModal(); });
    actions.append(remove);
  } else if (item.legacy_read_only) {
    const notice = document.createElement('small'); notice.textContent = 'Shared legacy restaurant · read-only until ownership is reviewed'; actions.append(notice);
  }
  article.append(actions); return article;
}

function openRestaurant(item=null) {
  state.editItem = item;
  $('#restaurantSheetTitle').textContent = item ? 'Edit restaurant' : 'Add a restaurant';
  $('#saveRestaurant').textContent = item ? 'Save changes' : 'Add to our list';
  $('#restaurantName').value = item?.name || ''; $('#restaurantNotes').value = item?.notes || '';
  const knownCuisines = new Set(cuisines.map((name) => name.toLocaleLowerCase()));
  const selected = (item?.cuisines || []).filter((name) => knownCuisines.has(name.toLocaleLowerCase()));
  const custom = (item?.cuisines || []).filter((name) => !knownCuisines.has(name.toLocaleLowerCase()));
  createCuisineChips($('.editor-cuisines'), selected);
  $('#restaurantCustomCuisine').value = custom.join(', ');
  $('#restaurantImage').value = '';
  clearSelectedPhotoPreviews();
  const editor = $('#restaurantPhotoEditor'); editor.replaceChildren();
  (item?.photos || []).forEach((photo, index) => {
    const figure = document.createElement('figure');
    const image = Object.assign(document.createElement('img'), {src:`${photo.url}?v=${encodeURIComponent(item.updated_at || '')}`, alt:`Existing photo ${index + 1}`});
    const label = document.createElement('label');
    const input = Object.assign(document.createElement('input'), {type:'checkbox', value:photo.id});
    input.dataset.removePhoto = 'true'; label.append(input, document.createTextNode('Remove')); figure.append(image, label); editor.append(figure);
  });
  editor.hidden = !editor.children.length;
  $('#restaurantMessage').textContent = ''; $('#restaurantSheet').showModal();
  setTimeout(() => $('#restaurantName').focus(), 40);
}
function clearSelectedPhotoPreviews() {
  $('#restaurantNewPhotoSelection').hidden = true;
  $('#restaurantImageLabel').textContent = 'Choose photos';
}
function renderSelectedPhotoPreviews() {
  clearSelectedPhotoPreviews();
  const files = [...$('#restaurantImage').files];
  if (!files.length) return;
  $('#restaurantSelectedPhotoCount').textContent = `${files.length} ${files.length === 1 ? 'photo' : 'photos'} selected and ready`;
  $('#restaurantNewPhotoSelection').hidden = false;
  $('#restaurantImageLabel').textContent = 'Change selected photos';
}
function openPhotoViewer(photos, index=0, trigger=document.activeElement) {
  if (!photos.length) return;
  state.placePhotos = photos;
  state.placePhotoIndex = Math.max(0, Math.min(index, state.placePhotos.length - 1));
  state.placePhotoTrigger = trigger;
  renderPlacePhoto();
  $('#placePhotoViewer').showModal();
  $('#closePlacePhotos').focus();
}
function openPlacePhotos(item, index=0, trigger=document.activeElement) {
  const photos = (item.photos || []).map((photo, photoIndex) => ({
    src:`${photo.url}?v=${encodeURIComponent(item.updated_at || '')}`,
    alt:`${item.name}, photo ${photoIndex + 1} of ${item.photos.length}`,
    caption:item.photos.length > 1 ? `${item.name} · Photo ${photoIndex + 1} of ${item.photos.length}` : item.name,
  }));
  openPhotoViewer(photos, index, trigger);
}
function margaritaPhotoCaption(item) {
  return item.name?.trim() ? `${item.month_name} · ${item.name.trim()}` : `${item.month_name} · Chili's Margarita of the Month`;
}
function openMargaritaPhoto(item, trigger=document.activeElement) {
  if (!item.image_url) return;
  const caption = margaritaPhotoCaption(item);
  openPhotoViewer([{
    src:`${item.image_url}${item.image_url.includes('?') ? '&' : '?'}v=${encodeURIComponent(item.updated_at || '')}`,
    alt:`Photo of ${caption}`,
    caption,
  }], 0, trigger);
}
function renderPlacePhoto() {
  const photo = state.placePhotos[state.placePhotoIndex];
  if (!photo) return;
  $('#placePhotoImage').src = photo.src;
  $('#placePhotoImage').alt = photo.alt;
  $('#placePhotoCaption').textContent = photo.caption;
  $('#placePhotoCount').textContent = `${state.placePhotoIndex + 1} of ${state.placePhotos.length}`;
  const hasMultiple = state.placePhotos.length > 1;
  $('#previousPlacePhoto').hidden = !hasMultiple;
  $('#nextPlacePhoto').hidden = !hasMultiple;
  $('#placePhotoCount').hidden = !hasMultiple;
}
function movePlacePhoto(direction) {
  if (state.placePhotos.length < 2) return;
  state.placePhotoIndex = (state.placePhotoIndex + direction + state.placePhotos.length) % state.placePhotos.length;
  renderPlacePhoto();
}
async function saveRestaurant() {
  const button = $('#saveRestaurant'); setBusy(button, true, 'Saving…');
  const custom = $('#restaurantCustomCuisine').value.split(',').map((v) => v.trim()).filter(Boolean);
  const files = [...$('#restaurantImage').files];
  const retainedExisting = $('#restaurantPhotoEditor').querySelectorAll('figure').length - editorRemovedPhotos().length;
  if (retainedExisting + files.length > 12) {
    $('#restaurantMessage').textContent = `Keep or select no more than 12 photos total. You currently have ${retainedExisting + files.length}.`;
    setBusy(button, false); return;
  }
  const form = new FormData(); form.append('name',$('#restaurantName').value); form.append('notes',$('#restaurantNotes').value);
  if (state.editItem) form.append('version', state.editItem.version);
  [...selectedCuisines($('.editor-cuisines')), ...custom].forEach((name)=>form.append('cuisines',name));
  editorRemovedPhotos().forEach((id)=>form.append('remove_photo_ids',id));
  files.forEach((file)=>form.append('images',file));
  try {
    await api(state.editItem ? `/api/places/restaurants/${state.editItem.id}` : '/api/places/restaurants', {
      method:state.editItem ? 'PUT' : 'POST', body:form
    });
    $('#restaurantSheet').close(); clearSelectedPhotoPreviews();
    const uploadNotice = files.length ? `${files.length} ${files.length === 1 ? 'photo' : 'photos'} uploaded. ` : '';
    toast(`${uploadNotice}${state.editItem ? 'Restaurant updated.' : 'Restaurant added.'}`); await loadRestaurants();
  } catch (error) { $('#restaurantMessage').textContent = error.message; }
  finally { setBusy(button, false); }
}
function editorRemovedPhotos() {
  return [...$('#restaurantPhotoEditor').querySelectorAll('input[data-remove-photo]:checked')].map((input)=>input.value);
}
function openReview(item) {
  state.reviewItem = item; $('#reviewRestaurantName').textContent = item.name;
  $('#reviewRating').value = item.rating || 4; updateRating($('#reviewRating'), $('#reviewStars'));
  $('#reviewDate').value = item.visited_at || new Date().toISOString().slice(0,10);
  $('#reviewText').value = item.review || ''; $('#reviewMessage').textContent = ''; $('#reviewSheet').showModal();
}
function updateRating(input, output) { output.textContent = stars(Number(input.value)); }
async function saveReview() {
  const button = $('#saveReview'); setBusy(button, true, 'Saving…');
  try {
    await api(`/api/places/restaurants/${state.reviewItem.id}/review`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rating:Number($('#reviewRating').value), review:$('#reviewText').value, visited_at:$('#reviewDate').value, version:state.reviewItem.version})});
    $('#reviewSheet').close(); toast('Review saved.'); await loadRestaurants();
  } catch (error) { $('#reviewMessage').textContent = error.message; }
  finally { setBusy(button, false); }
}

async function chooseRestaurant() {
  const button = $('#chooseRestaurant'); setBusy(button, true, 'Choosing…');
  try {
    const data = await api('/api/places/choose', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({pool:$('input[name="choicePool"]:checked').value, cuisines:selectedCuisines($('.chooser-cuisines'))})});
    const item = data.restaurant, result = $('#choiceResult'); result.replaceChildren();
    const kicker = Object.assign(document.createElement('p'), {className:'sheet-kicker', textContent:`Chosen from ${data.pool_size} ${data.pool_size === 1 ? 'match' : 'matches'}`});
    const title = document.createElement('h2'); title.textContent = item.name;
    const details = document.createElement('p'); details.textContent = `${item.cuisines.join(' · ') || 'Food type not added'}${item.rating ? ` · ${stars(item.rating)}` : ''}`;
    result.append(kicker, title, details); result.hidden = false;
  } catch (error) { toast(error.message); }
  finally { setBusy(button, false); }
}

async function loadMargaritas() {
  const generation = ++margaritaGeneration;
  margaritaController?.abort();
  margaritaController = new AbortController();
  margaritasPanel.begin(generation);
  $('#margaritaMigrationNotice').hidden = true;
  try {
    const data = await api(calendarUrl('/api/places/margaritas'), {signal:margaritaController.signal});
    if (generation !== margaritaGeneration) return;
    updateCalendarChoices(data.years || []);
    state.margaritas = Array.isArray(data.margaritas) ? data.margaritas : [];
    $('#margaritaMigrationNotice').hidden = !state.margaritas.some((item) => item.migration_conflict);
    $('#margGrid').replaceChildren(...state.margaritas.map((item) => {
      const card = document.createElement('article'); card.className = 'marg-card';
      if (item.image_url) {
        const photoButton = document.createElement('button'); photoButton.type = 'button'; photoButton.className = 'marg-photo-open';
        photoButton.setAttribute('aria-label', `Open photo: ${margaritaPhotoCaption(item)}`);
        const img = Object.assign(document.createElement('img'), {src:`${item.image_url}${item.image_url.includes('?') ? '&' : '?'}v=${encodeURIComponent(item.updated_at || '')}`, alt:'', loading:'lazy'});
        photoButton.append(img); photoButton.addEventListener('click', () => openMargaritaPhoto(item, photoButton)); card.append(photoButton);
      } else {
        const art = Object.assign(document.createElement('span'), {className:'marg-fallback', textContent:'⌁'}); art.setAttribute('aria-hidden', 'true'); card.append(art);
      }
      const copy = document.createElement('span'); copy.className = 'marg-copy';
      const month = document.createElement('strong'); month.textContent = item.month_name;
      const name = document.createElement('b');
      name.textContent = item.migration_conflict ? 'Older saves need review' : (item.name || 'Add this month');
      const rating = document.createElement('small'); rating.textContent = stars(item.rating);
      copy.append(month, name, rating); card.append(copy);
      const actions = document.createElement('span'); actions.className = 'marg-actions';
      const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'marg-edit'; edit.textContent = 'Edit';
      if (item.migration_conflict) {
        edit.disabled = true; edit.textContent = 'Review needed';
        edit.setAttribute('aria-label', `${item.month_name} margarita needs household review`);
      } else {
        edit.setAttribute('aria-label', `Edit ${item.month_name} margarita`);
        edit.addEventListener('click', () => openMarg(item));
      }
      actions.append(edit); card.append(actions); return card;
    }));
    margaritasPanel.success(generation);
    placesAnnouncer.polite('Margarita calendar loaded.');
    return true;
  } catch (error) {
    if (error.name === 'AbortError' || generation !== margaritaGeneration) return;
    $('#margaritasErrorText').textContent = error.message;
    margaritasPanel.transition('error');
    placesAnnouncer.alert(`Margaritas could not be loaded. ${error.message}`);
    return false;
  }
}
function openMarg(item) {
  margaritaConflict = null; $('#margConflict').hidden = true;
  state.margItem = item; $('#margMonth').textContent = `${item.month_name}${item.calendar && item.calendar !== 'legacy' ? ` ${item.calendar}` : ' · Existing calendar'}`; $('#margName').value = item.name || '';
  $('#margRating').value = item.rating || 0; updateRating($('#margRating'), $('#margStars'));
  $('#margReview').value = item.review || ''; $('#margImage').value = ''; $('#removeMargImage').checked = false;
  $('#removePhotoWrap').hidden = !item.has_image; $('#margMessage').textContent = '';
  if (!$('#margSheet').open) $('#margSheet').showModal();
}
async function saveMarg() {
  const button = $('#saveMarg'); setBusy(button, true, 'Saving…');
  const form = new FormData(); form.append('name', $('#margName').value); form.append('rating', $('#margRating').value === '0' ? '' : $('#margRating').value);
  form.append('version', state.margItem.version);
  form.append('review', $('#margReview').value); form.append('remove_image', $('#removeMargImage').checked ? 'true' : 'false');
  if ($('#margImage').files[0]) form.append('image', $('#margImage').files[0]);
  try {
    await api(calendarUrl(`/api/places/margaritas/${state.margItem.month}`, state.margItem.calendar || 'legacy'), {method:'PUT', body:form});
    $('#margSheet').close(); toast('Margarita saved.'); await loadMargaritas();
  } catch (error) {
    if (error.status === 409 && error.data?.conflict) {
      const month = state.margItem.month;
      const reloaded = await loadMargaritas();
      if (!reloaded) {
        $('#margMessage').textContent = 'This month changed elsewhere, but the current calendar could not be reloaded. Close this editor and try again when connected.';
        return;
      }
      if (error.data?.review_required) {
        $('#margSheet').close();
        toast('This month needs household review before editing.');
      } else {
        margaritaConflict = state.margaritas.find((item) => item.month === month);
        $('#margMessage').textContent = 'This month changed elsewhere. Your draft and selected photo are still here.';
        $('#margConflictCopy').textContent = `Saved version: ${margaritaConflict?.name || 'No name'} — ${margaritaConflict?.review || 'No notes'}`;
        $('#margConflict').hidden = !margaritaConflict;
      }
    } else {
      $('#margMessage').textContent = error.message;
    }
  }
  finally { setBusy(button, false); }
}

$('#margCalendar').addEventListener('change', () => { margaritaCalendar = $('#margCalendar').value; loadMargaritas(); });
for (const [selector, direction] of [['#margPreviousYear', -1], ['#margNextYear', 1]]) {
  $(selector).addEventListener('click', () => {
    const year = margaritaCalendar === 'legacy' ? new Date().getFullYear() : Number(margaritaCalendar) + direction;
    margaritaCalendar = String(Math.max(1900, Math.min(2200, year)));
    updateCalendarChoices(); loadMargaritas();
  });
}
$('#margAssignYearValue').value = String(new Date().getFullYear());
$('#margAssignYearSave').addEventListener('click', async () => {
  const button = $('#margAssignYearSave'); button.disabled = true;
  try {
    const result = await api('/api/places/margaritas/assign-year', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({year: $('#margAssignYearValue').value, versions: Object.fromEntries(state.margaritas.map(item => [item.month, item.version]))})});
    margaritaCalendar = String(result.year); updateCalendarChoices(); await loadMargaritas(); toast('Calendar copied. The original is preserved.');
  } catch (error) { $('#margAssignMessage').textContent = error.message; }
  finally { button.disabled = false; }
});
$('#margUseSaved').addEventListener('click', () => { if (margaritaConflict) openMarg(margaritaConflict); });
$('#margKeepDraft').addEventListener('click', () => {
  if (!margaritaConflict) return;
  state.margItem = margaritaConflict; margaritaConflict = null; $('#margConflict').hidden = true; saveMarg();
});
window.DavidPiFilterGroup.create('#placesTabs', {selector:'button[data-tab]', onChange:async (button) => {
  state.tab = button.dataset.tab; [...$('#placesTabs').children].forEach((node) => node.classList.toggle('selected', node === button));
  const restaurants = ['want_to_go','reviewed','trash'].includes(state.tab);
  $('#restaurantPanel').hidden = !restaurants; $('#chooserPanel').hidden = state.tab !== 'choose'; $('#margaritaPanel').hidden = state.tab !== 'margaritas';
  if (restaurants) { $('#restaurantSort').hidden = state.tab !== 'reviewed'; await loadRestaurants(); }
  if (state.tab === 'margaritas') await loadMargaritas();
}});
$('#openRestaurant').addEventListener('click', () => openRestaurant());
$('#emptyAddRestaurant').addEventListener('click', () => openRestaurant());
$('#closeRestaurant').addEventListener('click', () => $('#restaurantSheet').close());
$('#saveRestaurant').addEventListener('click', saveRestaurant);
$('#restaurantImage').addEventListener('change', renderSelectedPhotoPreviews);
$('#closePlacePhotos').addEventListener('click', () => $('#placePhotoViewer').close());
$('#placePhotoViewer').addEventListener('close', () => {
  const trigger = state.placePhotoTrigger;
  state.placePhotoTrigger = null;
  $('#placePhotoImage').removeAttribute('src');
  if (trigger?.isConnected) trigger.focus();
});
$('#previousPlacePhoto').addEventListener('click', () => movePlacePhoto(-1));
$('#nextPlacePhoto').addEventListener('click', () => movePlacePhoto(1));
$('#placePhotoViewer').addEventListener('keydown', (event) => {
  if (event.key === 'ArrowLeft') movePlacePhoto(-1);
  if (event.key === 'ArrowRight') movePlacePhoto(1);
});
let placePhotoTouchX = null;
$('#placePhotoStage').addEventListener('touchstart', (event) => { placePhotoTouchX = event.changedTouches[0].clientX; }, {passive:true});
$('#placePhotoStage').addEventListener('touchend', (event) => {
  if (placePhotoTouchX === null) return;
  const delta = event.changedTouches[0].clientX - placePhotoTouchX; placePhotoTouchX = null;
  if (Math.abs(delta) >= 48) movePlacePhoto(delta < 0 ? 1 : -1);
}, {passive:true});
$('#closeReview').addEventListener('click', () => $('#reviewSheet').close());
$('#saveReview').addEventListener('click', saveReview);
$('#reviewRating').addEventListener('input', () => updateRating($('#reviewRating'), $('#reviewStars')));
$('#margRating').addEventListener('input', () => updateRating($('#margRating'), $('#margStars')));
$('#closeMarg').addEventListener('click', () => $('#margSheet').close());
$('#saveMarg').addEventListener('click', saveMarg);
$('#chooseRestaurant').addEventListener('click', chooseRestaurant);
$('#restaurantSort').addEventListener('change', loadRestaurants);
let searchTimer; $('#restaurantSearch').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadRestaurants, 220); });
$('#placesRetry').addEventListener('click', loadRestaurants);
$('#margaritasRetry').addEventListener('click', loadMargaritas);
$('#cancelPlaceDelete').addEventListener('click', () => $('#placeConfirm').close());
$('#acceptPlaceDelete').addEventListener('click', async () => {
  try { await api(`/api/places/restaurants/${state.deleteItem.id}`, {method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({version:state.deleteItem.version})}); $('#placeConfirm').close(); toast('Restaurant moved to trash and restorable for at least 30 days.'); await loadRestaurants(); }
  catch (error) { toast(error.message); }
});
createCuisineChips($('.editor-cuisines')); createCuisineChips($('.chooser-cuisines')); $('#restaurantSort').hidden = true; loadRestaurants();
