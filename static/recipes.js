const csrf = document.querySelector('meta[name="csrf-token"]').content;
const grid = document.querySelector('#recipeGrid'), toast = document.querySelector('#platformToast');
const loadMoreRecipes = document.querySelector('#loadMoreRecipes');
const recipePanel = window.AsyncPanel.create({root: '#recipePanel', loading: '#recipesLoading', content: '#recipeGrid', empty: '#recipesEmpty', noResults: '#recipesNoResults', error: '#recipesError'});
const recipeAnnouncer = window.DavidPiAnnouncer.create();
const recipeResultCount = document.querySelector('#recipeResultCount');
const recipePageError = document.querySelector('#recipePageError');
let libraryMeal = '', libraryView = 'active', recommendationMeal = 'main', editingRecipe = null, deleteTarget = null, recipeOffset = 0, recipeTotal = 0, recipeLoadGeneration = 0, recipeController = null, recipeAppending = false;
const recipePageSize = 30;
const renderedRecipeIds = new Set();
let recipeRefreshPending = false;
const sectionLabels = {breakfast: 'Breakfast', main: 'Lunch & dinner', dessert: 'Desserts'};
const recipeReviewSheet = document.querySelector('#recipeReviewSheet'), recipeReviewList = document.querySelector('#recipeReviewList'), recipeReviewMessage = document.querySelector('#recipeReviewMessage'), openRecipeReview = document.querySelector('#openRecipeReview');
async function api(url, options = {}) { options.headers = {...options.headers, 'X-CSRF-Token': csrf}; const response = await fetch(url, options); const data = await response.json().catch(() => ({})); if (!response.ok) { const error = new Error(data.error || 'Something went wrong.'); error.data = data; throw error; } return data; }
function showToast(text) { toast.textContent = text; toast.hidden = false; clearTimeout(showToast.timer); showToast.timer = setTimeout(() => { toast.hidden = true; }, 3000); }
async function loadRecipeReviewCount({summary=false}={}) { const data = await api(`/api/recipes/quality-review${summary?'?summary=1':''}`); const count = Number(data.counts?.pending || 0); openRecipeReview.textContent = count ? `Review suggestions (${count})` : 'Review library'; return data; }
function renderRecipeReview(data) {
  recipeReviewList.replaceChildren();
  const proposals = data.proposals || [];
  if (!proposals.length) { recipeReviewMessage.textContent = 'No section changes are waiting for your review.'; return; }
  recipeReviewMessage.textContent = `${proposals.length} ${proposals.length === 1 ? 'suggestion' : 'suggestions'} — nothing changes until you choose Accept.`;
  proposals.forEach((proposal) => {
    const card = document.createElement('article'); card.className = 'recipe-review-card';
    const title = document.createElement('h3'); title.textContent = proposal.recipe_title;
    const change = document.createElement('p'); change.textContent = `${sectionLabels[proposal.current] || proposal.current} → ${sectionLabels[proposal.proposed] || proposal.proposed}`;
    const clues = [...(proposal.evidence?.title_keywords || []), ...(proposal.evidence?.tag_keywords || [])];
    const reason = document.createElement('p'); reason.textContent = clues.length ? `${proposal.evidence.confidence} confidence · matched ${[...new Set(clues)].join(', ')}` : `${proposal.evidence?.confidence || 'Review'} confidence`;
    const actions = document.createElement('div'); actions.className = 'recipe-review-actions';
    const reject = document.createElement('button'); reject.type = 'button'; reject.textContent = 'Keep current';
    const accept = document.createElement('button'); accept.type = 'button'; accept.className = 'big-button'; accept.textContent = 'Accept change';
    async function decide(action) {
      reject.disabled = true; accept.disabled = true;
      try {
        await api(`/api/recipes/quality-review/${proposal.id}`, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action, version:proposal.version})});
        showToast(action === 'accept' ? `${proposal.recipe_title} moved to ${sectionLabels[proposal.proposed] || proposal.proposed}.` : 'Kept the current section.');
        const refreshed = await loadRecipeReviewCount(); renderRecipeReview(refreshed); if (action === 'accept') await loadRecipes();
      } catch (error) { recipeReviewMessage.textContent = `${error.message} Nothing else was changed.`; reject.disabled = false; accept.disabled = false; }
    }
    reject.addEventListener('click', () => decide('reject')); accept.addEventListener('click', () => decide('accept'));
    actions.append(reject, accept); card.append(title, change, reason, actions); recipeReviewList.append(card);
  });
}
openRecipeReview.addEventListener('click', async () => { recipeReviewMessage.textContent = 'Loading suggestions…'; recipeReviewList.replaceChildren(); recipeReviewSheet.showModal(); try { renderRecipeReview(await loadRecipeReviewCount()); } catch (error) { recipeReviewMessage.textContent = error.message; } });
document.querySelector('#closeRecipeReview').addEventListener('click', () => recipeReviewSheet.close());
document.querySelector('#refreshRecipeReview').addEventListener('click', async (event) => { const button = event.currentTarget; button.disabled = true; recipeReviewMessage.textContent = 'Scanning without changing your recipes…'; try { const result = await api('/api/recipes/quality-review/refresh', {method:'POST'}); renderRecipeReview(await loadRecipeReviewCount()); if (!result.pending) recipeReviewMessage.textContent = 'Review complete. No likely section mistakes were found in your recipes.'; } catch (error) { recipeReviewMessage.textContent = `${error.message} No recipe was changed.`; } finally { button.disabled = false; } });
function recipeCard(recipe) { const button = document.createElement('button'); button.className = 'recipe-card'; button.type = 'button'; button.setAttribute('aria-label', recipe.deleted_at ? `Restore ${recipe.title}` : `Open ${recipe.title}`); const art = recipe.image ? Object.assign(document.createElement('img'), {src: recipe.image, alt: '', loading: 'lazy', decoding:'async', width:640, height:480, fetchPriority:'low'}) : Object.assign(document.createElement('div'), {className: 'recipe-fallback', textContent: '⌁'}); const copy = document.createElement('div'); const title = document.createElement('h3'); title.textContent = recipe.title; const meta = document.createElement('p'); meta.textContent = [recipe.deleted_at ? 'In trash' : null, recipe.total_minutes ? `${recipe.total_minutes} min` : null, sectionLabels[recipe.meal_type] || recipe.meal_type, recipe.favorite ? 'Favorite' : null].filter(Boolean).join(' · '); const tags = document.createElement('small'); tags.textContent = recipe.deleted_at ? 'Tap to restore this recipe' : (recipe.tags.slice(0, 3).join(' · ') || recipe.description); copy.append(title, meta, tags); button.append(art, copy); button.addEventListener('click', () => recipe.deleted_at ? restoreRecipe(recipe, button) : openRecipe(recipe.id)); return window.RecipeWeek.decorate(recipe, button); }
async function restoreRecipe(recipe, button) { button.disabled = true; try { await api(`/api/recipes/${recipe.id}/restore`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({version:recipe.version})}); showToast(`${recipe.title} restored.`); await loadRecipes(); } catch (error) { showToast(error.message); button.disabled = false; } }
function updateRecipeFacets(facets = {}) { const sections = facets.sections || {}; const counts = {All: facets.total, Breakfast: sections.breakfast, Main: sections.main, Dessert: sections.dessert}; Object.entries(counts).forEach(([name, value]) => { const output = document.querySelector(`#recipeCount${name}`); if (output) output.textContent = Number(value || 0).toLocaleString(); }); const quality = document.querySelector('#recipeMetadataSummary'); const total = Number(facets.total || 0), timed = Number(facets.timed || 0), servings = Number(facets.with_servings || 0), other = Number(sections.other || 0); quality.textContent = total ? `${timed.toLocaleString()} with cooking time · ${servings.toLocaleString()} with servings${other ? ` · ${other.toLocaleString()} need a section review` : ''}` : ''; quality.hidden = !total; const quick = document.querySelector('#quickFilter'), quickLabel = document.querySelector('#quickFilterLabel'); quick.disabled = !timed; if (!timed) { quick.checked = false; quickLabel.title = 'Add a cooking time to a recipe to use this filter.'; } else { quickLabel.removeAttribute('title'); } }
async function loadRecipes({append = false, preserve = false} = {}) {
  if(append&&recipeAppending)return;
  const search = document.querySelector('#recipeSearch').value.trim();
  const desiredCount = preserve ? Math.max(recipePageSize, recipeOffset) : recipePageSize;
  const focusedRecipe = document.activeElement?.closest('[data-recipe-id]')?.dataset.recipeId;
  if (!append) {
    recipeController?.abort(); recipeController = new AbortController(); recipeAppending = false;
    ++recipeLoadGeneration;
    if (!preserve) { recipeOffset = 0; recipeTotal = 0; recipePanel.begin(recipeLoadGeneration); recipeResultCount.hidden = true; }
    else document.querySelector('#recipePanel').setAttribute('aria-busy', 'true');
    recipePageError.hidden = true; loadMoreRecipes.hidden = true;
  } else recipeAppending=true;
  const generation = recipeLoadGeneration;
  loadMoreRecipes.disabled = true;
  if (append) loadMoreRecipes.textContent = 'Loading…';
  try {
    const offset = append ? recipeOffset : 0;
    const data = await api(`/api/recipes?${new URLSearchParams({q: search, meal: libraryMeal, view:libraryView, limit: Math.min(desiredCount, 100), offset, summary:append?0:1})}`, {signal: recipeController.signal});
    if (generation !== recipeLoadGeneration) return;
    while (preserve && data.has_more && data.recipes.length < desiredCount) {
      const next = await api(`/api/recipes?${new URLSearchParams({q:search, meal:libraryMeal, view:libraryView, limit:Math.min(100, desiredCount - data.recipes.length), offset:data.recipes.length, summary:0})}`, {signal:recipeController.signal});
      if (generation !== recipeLoadGeneration) return;
      data.recipes.push(...next.recipes); data.has_more = next.has_more && next.recipes.length > 0;
    }
    if (!append) { grid.replaceChildren(); renderedRecipeIds.clear(); recipeTotal=Number(data.total||0); }
    const fragment = document.createDocumentFragment();
    data.recipes.forEach(recipe => {
      if (renderedRecipeIds.has(recipe.id)) return;
      renderedRecipeIds.add(recipe.id);
      const card = recipeCard(recipe); card.dataset.recipeId = recipe.id; fragment.append(card);
    });
    grid.append(fragment);
    recipeOffset = offset + data.recipes.length;
    const shown = renderedRecipeIds.size;
    recipeResultCount.textContent = recipeTotal > shown ? `${shown.toLocaleString()} of ${recipeTotal.toLocaleString()} recipes` : `${shown.toLocaleString()} ${shown === 1 ? 'recipe' : 'recipes'}`;
    recipeResultCount.hidden = false; loadMoreRecipes.hidden = !data.has_more; recipePageError.hidden = true;
    if(data.facets)updateRecipeFacets(data.facets);
    document.querySelector('#recipesEmpty h2').textContent = libraryView === 'trash' ? 'Trash is empty.' : 'No recipes yet.';
    document.querySelector('#recipesEmpty p').textContent = libraryView === 'trash' ? 'Recipes you move here remain recoverable for at least 30 days.' : 'Add one by hand or import a recipe URL you already know.';
    document.querySelector('#emptyNewRecipe').hidden = libraryView === 'trash';
    if (preserve) recipePanel.transition(shown ? 'content' : (search || libraryMeal ? 'noResults' : 'empty'));
    else recipePanel.success(undefined, {empty: recipeOffset === 0 && !search && !libraryMeal, noResults: recipeOffset === 0 && Boolean(search || libraryMeal)});
    if (preserve && focusedRecipe) [...grid.querySelectorAll('[data-recipe-id]')].find(card => card.dataset.recipeId === focusedRecipe)?.querySelector('.recipe-card')?.focus({preventScroll:true});
    recipeAnnouncer.polite(recipeOffset ? `${recipeOffset} recipes loaded.` : (search || libraryMeal ? 'No matching recipes.' : 'No recipes yet.'));
  } catch (error) {
    if (error.name === 'AbortError' || generation !== recipeLoadGeneration) return;
    if ((append || preserve) && recipeOffset) { document.querySelector('#recipePageErrorText').textContent = error.message; recipePageError.dataset.retry = preserve ? 'refresh' : 'append'; recipePageError.hidden = false; }
    else { document.querySelector('#recipesErrorText').textContent = error.message; recipePanel.transition('error'); }
    recipeAnnouncer.alert(`Recipes could not be loaded. ${error.message}`);
  } finally {
    if (generation === recipeLoadGeneration) { recipeAppending=false; document.querySelector('#recipePanel').setAttribute('aria-busy', 'false'); loadMoreRecipes.disabled = false; loadMoreRecipes.textContent = 'Load more recipes'; }
  }
}
async function openRecipe(id) {
  try {
  const data = await api(`/api/recipes/${id}`), recipe = data.recipe, content = document.querySelector('#recipeViewContent');
  content.replaceChildren();
  if (recipe.image) { const image = document.createElement('img'); image.src = recipe.image; image.alt = ''; content.append(image); }
  const heading = document.createElement('h1'); heading.textContent = recipe.title;
  const meta = document.createElement('p'); meta.className = 'cooking-meta'; meta.textContent = [recipe.servings ? (/^\d+(?:[–-]\d+)?$/.test(String(recipe.servings).trim()) ? `${recipe.servings} servings` : recipe.servings) : null, recipe.total_minutes ? `${recipe.total_minutes} minutes` : null, sectionLabels[recipe.meal_type] || recipe.meal_type, recipe.owner_name ? `Added by ${recipe.owner_name}` : 'Shared legacy recipe'].filter(Boolean).join(' · ');
  const ingredients = document.createElement('section'); ingredients.innerHTML = '<h2>Ingredients</h2>';
  const ul = document.createElement('ul'); recipe.ingredients.forEach((item) => { const li = document.createElement('li'); li.textContent = item; ul.append(li); }); ingredients.append(ul);
  const steps = document.createElement('section'); steps.innerHTML = '<h2>Instructions</h2>';
  const ol = document.createElement('ol'); recipe.instructions.forEach((item) => { const li = document.createElement('li'); li.textContent = item; ol.append(li); }); steps.append(ol);
  const actions = document.createElement('div'); actions.className = 'cooking-actions';
  const favorite = document.createElement('button'); favorite.textContent = recipe.favorite ? '★ Favorite' : '☆ Add favorite';
  favorite.addEventListener('click', async () => {
    favorite.disabled = true;
    try {
      const result = await api(`/api/recipes/${recipe.id}/favorite`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({favorite:!recipe.favorite, state_version:recipe.state_version})});
      recipe.favorite = result.favorite; recipe.state_version = result.state_version; favorite.textContent = recipe.favorite ? '★ Favorite' : '☆ Add favorite';
      showToast(recipe.favorite ? 'Added to your favorites.' : 'Removed from your favorites.');
    } catch (error) { showToast(error.message); }
    finally { favorite.disabled = false; }
  });
  const made = document.createElement('button'); made.textContent = 'Mark as made'; made.addEventListener('click', async () => { made.disabled = true; try { const result = await api(`/api/recipes/${recipe.id}/made`, {method: 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({state_version:recipe.state_version})}); recipe.state_version = result.state_version; showToast('Marked as made.'); } catch (error) { showToast(error.message); } finally { made.disabled = false; } });
  actions.append(favorite, made);
  if (recipe.can_edit) {
    const edit = document.createElement('button'); edit.textContent = 'Edit recipe'; edit.addEventListener('click', () => { document.querySelector('#recipeView').close(); openEditor(recipe); });
    const remove = document.createElement('button'); remove.className = 'danger-text'; remove.textContent = 'Move to trash'; remove.addEventListener('click', () => { deleteTarget = recipe; document.querySelector('#recipeDeleteError').hidden = true; document.querySelector('#recipeConfirm').showModal(); });
    actions.append(edit, remove);
  } else if (recipe.legacy_read_only) {
    const notice = document.createElement('small'); notice.textContent = 'This shared legacy recipe stays read-only until ownership is reviewed.'; actions.append(notice);
  }
  content.append(heading, meta, ingredients, steps);
  if (recipe.source_url) { const source = document.createElement('a'); source.href = recipe.source_url; source.target = '_blank'; source.rel = 'noopener noreferrer'; source.textContent = `Source: ${recipe.source_name || 'Original recipe'}`; content.append(source); }
  content.append(actions); document.querySelector('#recipeView').showModal();
  } catch (error) { showToast(`Recipe could not be opened. ${error.message}`); }
}
function openEditor(recipe = null) { editingRecipe = recipe; document.querySelector('#recipeEditorHeading').textContent = recipe ? 'Edit recipe' : 'New recipe'; document.querySelector('#recipeTitle').value = recipe?.title || ''; document.querySelector('#recipeMeal').value = recipe?.meal_type || 'main'; document.querySelector('#recipeMinutes').value = recipe?.total_minutes || ''; document.querySelector('#recipeDescription').value = recipe?.description || ''; document.querySelector('#recipeServings').value = recipe?.servings || ''; document.querySelector('#recipeIngredients').value = recipe?.ingredients.join('\n') || ''; document.querySelector('#recipeInstructions').value = recipe?.instructions.join('\n') || ''; document.querySelector('#recipeTags').value = recipe?.tags.join(', ') || ''; document.querySelector('#recipeFavorite').checked = Boolean(recipe?.favorite); document.querySelector('#recipeVisibility').value = recipe?.visibility || 'shared'; document.querySelector('#recipeEditorMessage').textContent = ''; document.querySelector('#recipeEditor').showModal(); }
function closeEditor() { document.querySelector('#recipeEditor').close(); editingRecipe = null; }
document.querySelector('#openRecipeEditor').addEventListener('click', () => openEditor()); document.querySelector('#emptyNewRecipe').addEventListener('click', () => openEditor()); document.querySelector('#closeRecipeEditor').addEventListener('click', closeEditor); document.querySelector('#cancelRecipeEditor').addEventListener('click', closeEditor);
document.querySelector('#saveRecipe').addEventListener('click', async () => { const button = document.querySelector('#saveRecipe'); button.disabled = true; const payload = {title: document.querySelector('#recipeTitle').value, meal_type: document.querySelector('#recipeMeal').value, total_minutes: document.querySelector('#recipeMinutes').value, description: document.querySelector('#recipeDescription').value, servings: document.querySelector('#recipeServings').value, ingredients: document.querySelector('#recipeIngredients').value.split('\n'), instructions: document.querySelector('#recipeInstructions').value.split('\n'), tags: document.querySelector('#recipeTags').value, favorite: document.querySelector('#recipeFavorite').checked, visibility:document.querySelector('#recipeVisibility').value, version:editingRecipe?.version, state_version:editingRecipe?.state_version}; try { await api(editingRecipe ? `/api/recipes/${editingRecipe.id}` : '/api/recipes', {method: editingRecipe ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)}); closeEditor(); showToast('Recipe saved.'); await loadRecipes(); } catch (error) { document.querySelector('#recipeEditorMessage').textContent = `${error.message} Nothing was changed.`; } finally { button.disabled = false; } });
document.querySelector('#closeRecipeView').addEventListener('click', () => document.querySelector('#recipeView').close());
document.querySelector('#openImport').addEventListener('click', () => document.querySelector('#importSheet').showModal()); document.querySelector('#closeImport').addEventListener('click', () => document.querySelector('#importSheet').close());
const discoverSheet = document.querySelector('#discoverSheet'), discoverResults = document.querySelector('#discoverResults'), discoverMessage = document.querySelector('#discoverMessage');
function renderDiscoverResults(results) {
  discoverResults.innerHTML = '';
  if (!results.length) { discoverMessage.textContent = 'No matching recipes found. Try another search.'; return; }
  discoverMessage.textContent = '';
  [...new Map(results.map(recipe => [recipe.mealdb_id, recipe])).values()].forEach((recipe) => {
    const card = document.createElement('article'); card.className = 'discover-card';
    const image = recipe.image_url ? Object.assign(document.createElement('img'), {src: recipe.image_url, alt: '', loading: 'lazy'}) : Object.assign(document.createElement('div'), {className: 'recipe-fallback', textContent: '⌁'});
    const copy = document.createElement('div'), title = document.createElement('h3'), meta = document.createElement('p'), add = document.createElement('button');
    title.textContent = recipe.title; meta.textContent = [recipe.area, recipe.category].filter(Boolean).join(' · ');
    add.type = 'button'; add.textContent = 'Add'; add.addEventListener('click', async () => {
      add.disabled = true; add.textContent = 'Adding…';
      try {
        await api('/api/recipes/import-mealdb', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mealdb_id: recipe.mealdb_id, meal_type: document.querySelector('#discoverMeal').value})});
        add.textContent = 'Added'; showToast(`${recipe.title} added to your recipes.`); await loadRecipes();
      } catch (error) { add.disabled = Boolean(error.data?.duplicate); add.textContent = error.data?.duplicate ? 'Already added' : 'Try again'; discoverMessage.textContent = error.message; }
    });
    copy.append(title, meta); card.append(image, copy, add); discoverResults.append(card);
  });
}
let discoveryGeneration = 0, discoveryController = null;
async function discover(url) {
  const generation = ++discoveryGeneration;
  discoveryController?.abort(); discoveryController = new AbortController();
  discoverMessage.textContent = 'Looking for recipes…'; discoverResults.innerHTML = '';
  try { const data = await api(url, {signal:discoveryController.signal}); if (generation === discoveryGeneration) renderDiscoverResults(data.results); } catch (error) { if (generation === discoveryGeneration && error.name !== 'AbortError') discoverMessage.textContent = error.message; }
}
document.querySelector('#openDiscover').addEventListener('click', () => { discoverMessage.textContent = ''; discoverResults.innerHTML = ''; discoverSheet.showModal(); setTimeout(() => document.querySelector('#discoverSearch').focus(), 50); });
document.querySelector('#closeDiscover').addEventListener('click', () => discoverSheet.close());
document.querySelector('#surpriseRecipe').addEventListener('click', () => discover('/api/recipes/discover/random'));
let discoverTimer; document.querySelector('#discoverSearch').addEventListener('input', (event) => { clearTimeout(discoverTimer); ++discoveryGeneration; discoveryController?.abort(); const query = event.target.value.trim(); if (query.length < 2) { discoverResults.innerHTML = ''; discoverMessage.textContent = query ? 'Type at least two letters.' : ''; return; } discoverTimer = setTimeout(() => discover(`/api/recipes/discover?${new URLSearchParams({q: query})}`), 350); });
document.querySelector('#startImport').addEventListener('click', async () => { const button = document.querySelector('#startImport'), urls = [...new Set(document.querySelector('#importUrls').value.split(/\s+/).filter(Boolean))].slice(0, 20), progress = document.querySelector('#importProgress'); if (!urls.length) { document.querySelector('#importSummary').textContent = 'Paste at least one recipe URL.'; return; } button.disabled = true; progress.innerHTML = ''; let imported = 0, duplicates = 0, failed = 0; for (const url of urls) { const row = document.createElement('div'); const name = document.createElement('strong'); name.textContent = url; const state = document.createElement('span'); state.textContent = 'Importing…'; row.append(name, state); progress.append(row); try { await api('/api/recipes/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url, meal_type: document.querySelector('#importMeal').value})}); state.textContent = 'Imported'; imported += 1; } catch (error) { if (error.data?.duplicate) { state.textContent = 'Already here'; duplicates += 1; } else { state.textContent = 'Failed'; state.title = error.message; failed += 1; } } } document.querySelector('#importSummary').textContent = `${imported} imported · ${duplicates} duplicates · ${failed} failed`; button.disabled = false; await loadRecipes(); });
window.DavidPiFilterGroup.create('#recommendationMeals', {onChange: (button) => { recommendationMeal = button.dataset.meal; }});
async function recommendations() {
  const buttons = [document.querySelector('#recommendRecipes'), document.querySelector('#refreshRecommendations')];
  buttons.forEach(button => { button.disabled = true; });
  try {
    const params = new URLSearchParams({meal: recommendationMeal, quick: document.querySelector('#quickFilter').checked ? 1 : 0, favorite: document.querySelector('#favoriteFilter').checked ? 1 : 0, ingredient: document.querySelector('#ingredientFilter').value});
    const data = await api(`/api/recipes/recommend?${params}`), target = document.querySelector('#recommendationGrid');
    target.replaceChildren(); [...new Map(data.recipes.map(recipe => [recipe.id, recipe])).values()].forEach(recipe => target.append(recipeCard(recipe)));
    document.querySelector('#recommendations').hidden = false;
    document.querySelector('#recommendations').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
    if (!data.recipes.length) showToast('No recipes match those filters yet.');
  } catch (error) { showToast(`Ideas could not be loaded. ${error.message}`); }
  finally { buttons.forEach(button => { button.disabled = false; }); }
}
document.querySelector('#recommendRecipes').addEventListener('click', recommendations); document.querySelector('#refreshRecommendations').addEventListener('click', recommendations);
window.DavidPiFilterGroup.create('#recipeMeals', {selector: '[data-library-meal]', onChange: (button) => { libraryMeal = button.dataset.libraryMeal; loadRecipes(); }});
window.DavidPiFilterGroup.create('#recipeLibraryViews', {selector:'button[data-library-view]', onChange:(button) => { libraryView = button.dataset.libraryView; loadRecipes(); }});
loadMoreRecipes.addEventListener('click', () => loadRecipes({append: true}));
document.querySelector('#retryRecipes').addEventListener('click', () => loadRecipes()); document.querySelector('#retryRecipePage').addEventListener('click', () => loadRecipes(recipePageError.dataset.retry === 'refresh' ? {preserve:true} : {append:true}));
let searchTimer; document.querySelector('#recipeSearch').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadRecipes, 250); });
document.querySelector('#cancelRecipeDelete').addEventListener('click', () => { deleteTarget = null; document.querySelector('#recipeConfirm').close(); });
document.querySelector('#acceptRecipeDelete').addEventListener('click', async () => {
  const button = document.querySelector('#acceptRecipeDelete'), target = deleteTarget;
  if (!target || button.disabled) return;
  button.disabled = true;
  const failure = document.querySelector('#recipeDeleteError');
  failure.hidden = true;
  try {
    await api(`/api/recipes/${target.id}`, {method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({version:target.version})});
    deleteTarget = null; document.querySelector('#recipeConfirm').close(); document.querySelector('#recipeView').close();
    showToast('Recipe moved to trash and can be restored for at least 30 days.'); await loadRecipes();
  } catch (error) { failure.textContent = error.message; failure.hidden = false; }
  finally { button.disabled = false; }
});
loadRecipes().catch((error) => showToast(error.message));
const scheduleRecipeReviewCount=window.requestIdleCallback||((callback)=>setTimeout(callback,250));
scheduleRecipeReviewCount(()=>loadRecipeReviewCount({summary:true}).catch(() => { openRecipeReview.textContent = 'Review library'; }));
function refreshLibraryWhenReady() {
  if (!recipeRefreshPending || document.visibilityState !== 'visible' || document.querySelector('dialog[open]') || document.activeElement?.matches('input, textarea, select')) return;
  recipeRefreshPending = false; loadRecipes({preserve:true});
}
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { recipeRefreshPending = true; refreshLibraryWhenReady(); } });
window.addEventListener('pageshow', event => { if (event.persisted) { recipeRefreshPending = true; refreshLibraryWhenReady(); } });
document.addEventListener('close', refreshLibraryWhenReady, true);
document.addEventListener('focusout', () => { if (recipeRefreshPending) setTimeout(refreshLibraryWhenReady, 0); });
