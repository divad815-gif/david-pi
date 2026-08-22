const csrf = document.querySelector('meta[name="csrf-token"]').content;
let cuisines = JSON.parse(document.body.dataset.cuisines || '[]');
const state = {tab:'want_to_go', items:[], editId:null, reviewId:null, deleteId:null, margMonth:null, margaritas:[], placePhotos:[], placePhotoIndex:0};
const $ = (selector) => document.querySelector(selector);

async function api(url, options={}) {
  options.headers = {...options.headers, 'X-CSRF-Token':csrf};
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || 'Something went wrong.');
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
  const list = $('#restaurantList');
  const query = $('#restaurantSearch').value;
  const sort = $('#restaurantSort').value;
  try {
    const data = await api(`/api/places/restaurants?${new URLSearchParams({view:state.tab, q:query, sort})}`);
    updateCuisineOptions(data.cuisines);
    state.items = data.restaurants;
    list.replaceChildren(...state.items.map(restaurantCard));
    $('#placesEmpty').hidden = state.items.length !== 0;
    $('#placesEmpty h2').textContent = state.tab === 'reviewed' ? 'No reviews yet.' : 'Start a restaurant list.';
    $('#placesEmpty p').textContent = state.tab === 'reviewed' ? 'Check off a place after you go and leave a review.' : 'Add somewhere you both want to try.';
  } catch (error) { list.textContent = error.message; }
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
      image.addEventListener('click', () => openPlacePhotos(item, index));
      image.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openPlacePhotos(item, index); } });
      gallery.append(image);
    });
    article.append(gallery);
  }
  const top = document.createElement('div'); top.className = 'restaurant-card-top';
  if (item.status === 'want_to_go') {
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
  const edit = Object.assign(document.createElement('button'), {textContent:'Edit'});
  edit.addEventListener('click', () => openRestaurant(item));
  if (item.status === 'reviewed') {
    const rereview = Object.assign(document.createElement('button'), {textContent:'Update review'});
    rereview.addEventListener('click', () => openReview(item)); actions.append(rereview);
  }
  const remove = Object.assign(document.createElement('button'), {className:'danger-text', textContent:'Remove'});
  remove.addEventListener('click', () => { state.deleteId = item.id; $('#placeConfirm').showModal(); });
  actions.append(edit, remove); article.append(actions); return article;
}

function openRestaurant(item=null) {
  state.editId = item?.id || null;
  $('#restaurantSheetTitle').textContent = item ? 'Edit restaurant' : 'Add a restaurant';
  $('#saveRestaurant').textContent = item ? 'Save changes' : 'Add to our list';
  $('#restaurantName').value = item?.name || ''; $('#restaurantNotes').value = item?.notes || '';
  createCuisineChips($('.editor-cuisines'), item?.cuisines || []);
  $('#restaurantCustomCuisine').value = '';
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
function openPlacePhotos(item, index=0) {
  state.placePhotos = item.photos || [];
  state.placePhotoIndex = Math.max(0, Math.min(index, state.placePhotos.length - 1));
  renderPlacePhoto();
  $('#placePhotoViewer').showModal();
}
function renderPlacePhoto() {
  const photo = state.placePhotos[state.placePhotoIndex];
  if (!photo) return;
  $('#placePhotoImage').src = photo.url;
  $('#placePhotoImage').alt = `Restaurant photo ${state.placePhotoIndex + 1} of ${state.placePhotos.length}`;
  $('#placePhotoCount').textContent = `${state.placePhotoIndex + 1} of ${state.placePhotos.length}`;
  $('#previousPlacePhoto').disabled = state.placePhotos.length < 2;
  $('#nextPlacePhoto').disabled = state.placePhotos.length < 2;
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
  [...selectedCuisines($('.editor-cuisines')), ...custom].forEach((name)=>form.append('cuisines',name));
  editorRemovedPhotos().forEach((id)=>form.append('remove_photo_ids',id));
  files.forEach((file)=>form.append('images',file));
  try {
    await api(state.editId ? `/api/places/restaurants/${state.editId}` : '/api/places/restaurants', {
      method:state.editId ? 'PUT' : 'POST', body:form
    });
    $('#restaurantSheet').close(); clearSelectedPhotoPreviews();
    const uploadNotice = files.length ? `${files.length} ${files.length === 1 ? 'photo' : 'photos'} uploaded. ` : '';
    toast(`${uploadNotice}${state.editId ? 'Restaurant updated.' : 'Restaurant added.'}`); await loadRestaurants();
  } catch (error) { $('#restaurantMessage').textContent = error.message; }
  finally { setBusy(button, false); }
}
function editorRemovedPhotos() {
  return [...$('#restaurantPhotoEditor').querySelectorAll('input[data-remove-photo]:checked')].map((input)=>input.value);
}
function openReview(item) {
  state.reviewId = item.id; $('#reviewRestaurantName').textContent = item.name;
  $('#reviewRating').value = item.rating || 4; updateRating($('#reviewRating'), $('#reviewStars'));
  $('#reviewDate').value = item.visited_at || new Date().toISOString().slice(0,10);
  $('#reviewText').value = item.review || ''; $('#reviewMessage').textContent = ''; $('#reviewSheet').showModal();
}
function updateRating(input, output) { output.textContent = stars(Number(input.value)); }
async function saveReview() {
  const button = $('#saveReview'); setBusy(button, true, 'Saving…');
  try {
    await api(`/api/places/restaurants/${state.reviewId}/review`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rating:Number($('#reviewRating').value), review:$('#reviewText').value, visited_at:$('#reviewDate').value})});
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
  const data = await api('/api/places/margaritas'); state.margaritas = data.margaritas;
  $('#margGrid').replaceChildren(...data.margaritas.map((item) => {
    const button = document.createElement('button'); button.className = 'marg-card';
    if (item.image_url) { const img = Object.assign(document.createElement('img'), {src:`${item.image_url}?v=${encodeURIComponent(item.updated_at || '')}`, alt:'', loading:'lazy'}); button.append(img); }
    else { const art = Object.assign(document.createElement('span'), {className:'marg-fallback', textContent:'⌁'}); button.append(art); }
    const copy = document.createElement('span'); copy.className = 'marg-copy';
    const month = document.createElement('strong'); month.textContent = item.month_name;
    const name = document.createElement('b'); name.textContent = item.name || 'Add this month';
    const rating = document.createElement('small'); rating.textContent = stars(item.rating);
    copy.append(month, name, rating); button.append(copy); button.addEventListener('click', () => openMarg(item)); return button;
  }));
}
function openMarg(item) {
  state.margMonth = item.month; $('#margMonth').textContent = item.month_name; $('#margName').value = item.name || '';
  $('#margRating').value = item.rating || 0; updateRating($('#margRating'), $('#margStars'));
  $('#margReview').value = item.review || ''; $('#margImage').value = ''; $('#removeMargImage').checked = false;
  $('#removePhotoWrap').hidden = !item.has_image; $('#margMessage').textContent = ''; $('#margSheet').showModal();
}
async function saveMarg() {
  const button = $('#saveMarg'); setBusy(button, true, 'Saving…');
  const form = new FormData(); form.append('name', $('#margName').value); form.append('rating', $('#margRating').value === '0' ? '' : $('#margRating').value);
  form.append('review', $('#margReview').value); form.append('remove_image', $('#removeMargImage').checked ? 'true' : 'false');
  if ($('#margImage').files[0]) form.append('image', $('#margImage').files[0]);
  try {
    await api(`/api/places/margaritas/${state.margMonth}`, {method:'PUT', body:form});
    $('#margSheet').close(); toast('Margarita saved.'); await loadMargaritas();
  } catch (error) { $('#margMessage').textContent = error.message; }
  finally { setBusy(button, false); }
}

$('#placesTabs').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-tab]'); if (!button) return;
  state.tab = button.dataset.tab; [...$('#placesTabs').children].forEach((node) => node.classList.toggle('selected', node === button));
  const restaurants = ['want_to_go','reviewed'].includes(state.tab);
  $('#restaurantPanel').hidden = !restaurants; $('#chooserPanel').hidden = state.tab !== 'choose'; $('#margaritaPanel').hidden = state.tab !== 'margaritas';
  if (restaurants) { $('#restaurantSort').hidden = state.tab !== 'reviewed'; await loadRestaurants(); }
  if (state.tab === 'margaritas') await loadMargaritas();
});
$('#openRestaurant').addEventListener('click', () => openRestaurant());
$('#emptyAddRestaurant').addEventListener('click', () => openRestaurant());
$('#closeRestaurant').addEventListener('click', () => $('#restaurantSheet').close());
$('#saveRestaurant').addEventListener('click', saveRestaurant);
$('#restaurantImage').addEventListener('change', renderSelectedPhotoPreviews);
$('#closePlacePhotos').addEventListener('click', () => $('#placePhotoViewer').close());
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
$('#cancelPlaceDelete').addEventListener('click', () => $('#placeConfirm').close());
$('#acceptPlaceDelete').addEventListener('click', async () => {
  try { await api(`/api/places/restaurants/${state.deleteId}`, {method:'DELETE'}); $('#placeConfirm').close(); toast('Restaurant removed.'); await loadRestaurants(); }
  catch (error) { toast(error.message); }
});
createCuisineChips($('.editor-cuisines')); createCuisineChips($('.chooser-cuisines')); $('#restaurantSort').hidden = true; loadRestaurants();
