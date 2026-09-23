(() => {
  'use strict';
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const $ = (id) => document.getElementById(id);
  const root = $('weeklyPlanner'), picker = $('plannerPicker');
  const drafts = new Map(), extraDrafts = new Map(), expanded = new Set();
  let plan = null, phase = 'loading', generation = 0, choiceGeneration = 0;
  let controller = null, choiceController = null, choiceRows = [], choicesLoading = false;
  $('plannerWeek').value = window.RecipeWeek.value;

  async function api(url, options = {}) {
    options.headers = {...options.headers, 'X-CSRF-Token': csrf};
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.error || 'Something went wrong.');
      error.data = data;
      throw error;
    }
    return data;
  }
  const selectedWeek = () => $('plannerWeek').value;
  const draftKey = (item) => `${plan.week}:${item.id}`;
  const canChange = () => phase === 'ready' && plan?.week === selectedWeek();

  function setPhase(next, message) {
    phase = next;
    root.dataset.state = next;
    root.setAttribute('aria-busy', String(next === 'loading' || next === 'saving'));
    $('plannerStatus').textContent = message;
    $('plannerRetry').hidden = next !== 'error' && next !== 'conflict';
    $('plannerWeek').disabled = next === 'saving';
    syncControls();
  }
  function syncControls() {
    const ready = canChange();
    root.querySelectorAll('[data-plan-mutation]').forEach(control => { control.disabled = !ready; });
    $('plannerSearch').disabled = !ready;
    $('plannerChoices').querySelectorAll('button').forEach(control => {
      control.disabled = !ready || choicesLoading || control.dataset.selected === 'true';
    });
  }
  function button(text, fn) {
    const b = document.createElement('button');
    b.type = 'button'; b.textContent = text; b.dataset.planMutation = '';
    b.addEventListener('click', fn);
    return b;
  }
  function row(text, subtitle) {
    const r = document.createElement('div'), copy = document.createElement('div');
    r.className = 'planner-row'; copy.className = 'planner-copy'; copy.textContent = text;
    if (subtitle) { const s = document.createElement('small'); s.textContent = subtitle; copy.append(s); }
    r.append(copy);
    return r;
  }
  function focusSnapshot() {
    const input = document.activeElement;
    if (!input?.dataset.draftKey) return null;
    return {key: input.dataset.draftKey, start: input.selectionStart, end: input.selectionEnd};
  }
  function restoreFocus(snapshot) {
    if (!snapshot || !canChange()) return;
    const input = [...root.querySelectorAll('[data-draft-key]')].find(item => item.dataset.draftKey === snapshot.key);
    if (!input) return;
    input.focus({preventScroll: true});
    input.setSelectionRange(snapshot.start, snapshot.end);
  }
  function editor(r, item) {
    const key = draftKey(item);
    if (!drafts.has(key)) drafts.set(key, item.text);
    r.classList.add('is-editing');
    r.querySelector('.planner-row-actions').hidden = true;
    const copy = r.querySelector('.planner-copy'), input = document.createElement('input');
    input.type = 'text'; input.maxLength = 1000; input.value = drafts.get(key);
    input.dataset.planMutation = ''; input.dataset.draftKey = key;
    input.setAttribute('aria-label', 'Grocery item');
    input.addEventListener('input', () => drafts.set(key, input.value));
    const actions = document.createElement('div'); actions.className = 'planner-edit-actions';
    const save = button('Save', () => change({action: 'edit_item', item_id: item.id, text: input.value}));
    actions.append(save, button('Cancel', () => { drafts.delete(key); render(); }));
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); save.click(); }
      if (event.key === 'Escape') { event.preventDefault(); drafts.delete(key); render(); }
    });
    copy.replaceChildren(input, actions);
    syncControls();
    return input;
  }
  function clearPlan() {
    for (const id of ['plannerRecipes', 'plannerItems', 'plannerRemoved']) $(id).replaceChildren();
    $('plannerCount').textContent = '';
  }
  function render() {
    clearPlan();
    if (!plan) return;
    const titles = Object.fromEntries(plan.recipes.map(recipe => [recipe.id, recipe.title]));
    for (const recipe of plan.recipes) {
      const r = row(recipe.title);
      r.append(button('Remove recipe', () => change({action: 'remove_recipe', recipe_id: recipe.id})));
      $('plannerRecipes').append(r);
    }
    const active = plan.items.filter(item => !item.removed);
    $('plannerCount').textContent = `(${active.filter(item => !item.checked).length} left)`;
    for (const removed of [false, true]) {
      // Keep the existing unit-aware grocery grouping and original recipe amounts.
      for (const group of GroceryGroups.group(plan.items.filter(item => Boolean(item.removed) === removed))) {
        const box = document.createElement('section'); box.className = 'grocery-group';
        const heading = document.createElement('h4');
        heading.textContent = `${group.label} · ${group.recipeCount} ${group.recipeCount === 1 ? 'recipe' : 'recipes'}${group.extras ? ` + ${group.extras} extra` : ''}`;
        const summary = document.createElement('p'); summary.textContent = group.summary;
        const controls = document.createElement('div'); controls.className = 'grocery-group-controls';
        const apply = fields => change({action: 'edit_items', item_ids: group.items.map(item => item.id), ...fields});
        if (removed) controls.append(button('Restore group', () => apply({removed: false})));
        else {
          const check = document.createElement('input'); check.type = 'checkbox'; check.dataset.planMutation = '';
          check.checked = group.items.every(item => item.checked);
          check.indeterminate = !check.checked && group.items.some(item => item.checked);
          check.setAttribute('aria-label', `Got all ${group.label}`);
          check.addEventListener('change', () => apply({checked: check.checked}));
          controls.append(check, button('Remove group', () => apply({removed: true})));
        }
        const details = document.createElement('details'), label = document.createElement('summary');
        label.textContent = 'Recipe amounts & edits'; details.append(label);
        const openKey = `${plan.week}:${removed}:${group.key}`;
        details.open = expanded.has(openKey);
        details.addEventListener('toggle', () => {
          if (!details.isConnected) return;
          if (details.open) expanded.add(openKey); else expanded.delete(openKey);
        });
        for (const item of group.items) {
          const r = row(item.text, titles[item.recipe_id] || 'Extra item');
          if (item.removed) {
            r.append(button('Restore', () => change({action: 'edit_item', item_id: item.id, removed: false})));
            details.append(r); continue;
          }
          r.classList.toggle('is-checked', item.checked);
          const check = document.createElement('input'); check.type = 'checkbox'; check.dataset.planMutation = '';
          check.checked = item.checked; check.setAttribute('aria-label', `Got ${item.text}`);
          check.addEventListener('change', () => change({action: 'edit_item', item_id: item.id, checked: check.checked}));
          const actions = document.createElement('div'); actions.className = 'planner-row-actions';
          actions.append(button('Edit', () => editor(r, item).focus()), button('Remove', () => change({action: 'edit_item', item_id: item.id, removed: true})));
          r.prepend(check); r.append(actions);
          if (drafts.has(draftKey(item))) { editor(r, item); details.open = true; }
          details.append(r);
        }
        box.append(heading, summary, controls, details);
        $(removed ? 'plannerRemoved' : 'plannerItems').append(box);
      }
    }
    syncControls();
  }
  async function load() {
    if (phase === 'saving') return;
    const week = selectedWeek(), g = ++generation, focused = focusSnapshot();
    controller?.abort(); controller = new AbortController();
    choiceController?.abort(); ++choiceGeneration; choicesLoading = false;
    setPhase('loading', 'Loading week…');
    if (plan?.week !== week) { plan = null; clearPlan(); }
    $('plannerExtra').value = extraDrafts.get(week) || '';
    $('plannerExtra').dataset.draftKey = `${week}:extra`;
    $('plannerChoices').replaceChildren();
    try {
      const data = await api(`/api/recipes/weekly-plan?week=${encodeURIComponent(week)}`, {signal: controller.signal});
      if (g !== generation) return;
      plan = data.plan;
      $('plannerWeek').value = plan.week;
      render(); setPhase('ready', 'Shared changes save automatically.'); restoreFocus(focused);
      if (picker.open) choices();
    } catch (error) {
      if (g !== generation || error.name === 'AbortError') return;
      setPhase('error', `${error.message} Reload this week to continue. Your drafts are kept.`);
    }
  }
  async function change(payload) {
    if (!canChange()) return false;
    const week = plan.week, version = plan.version, focused = focusSnapshot();
    setPhase('saving', 'Saving for both of you…');
    try {
      const data = await api('/api/recipes/weekly-plan', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...payload, week, version})
      });
      if (payload.action === 'edit_item' && 'text' in payload) drafts.delete(`${week}:${payload.item_id}`);
      if (payload.action === 'add_item') { extraDrafts.delete(week); $('plannerExtra').value = ''; }
      plan = data.plan;
      render(); setPhase('ready', 'Saved for both of you.'); renderChoices(); restoreFocus(focused);
      return true;
    } catch (error) {
      if (error.data?.conflict && error.data.plan?.week === week) plan = error.data.plan;
      render();
      const message = error.data?.conflict
        ? 'This week changed elsewhere. Reload to review the latest list. Your drafts are kept.'
        : `${error.message} Reload this week to continue. Your drafts are kept.`;
      setPhase(error.data?.conflict ? 'conflict' : 'error', message);
      $('plannerPickerStatus').textContent = message;
      return false;
    }
  }
  function renderChoices() {
    $('plannerChoices').replaceChildren();
    for (const recipe of choiceRows) {
      const r = row(recipe.title, recipe.servings ? `Servings: ${recipe.servings}` : '');
      const selected = Boolean(plan?.recipes.some(item => item.id === recipe.id));
      const b = button(selected ? 'Added' : 'Add to week', () => change({action: 'add_recipe', recipe_id: recipe.id}));
      b.dataset.selected = String(selected); r.append(b); $('plannerChoices').append(r);
    }
    syncControls();
  }
  async function choices() {
    if (!canChange()) return;
    const g = ++choiceGeneration, week = plan.week;
    choiceController?.abort(); choiceController = new AbortController(); choicesLoading = true;
    $('plannerPickerStatus').textContent = 'Loading shared recipes…'; syncControls();
    try {
      const data = await api(`/api/recipes/weekly-plan?${new URLSearchParams({week, choices: '1', q: $('plannerSearch').value})}`, {signal: choiceController.signal});
      if (g !== choiceGeneration || week !== selectedWeek()) return;
      choiceRows = [...new Map(data.choices.map(recipe => [recipe.id, recipe])).values()];
      choicesLoading = false; renderChoices();
      $('plannerPickerStatus').textContent = data.choices.length === 50 ? 'Showing 50 recipes. Search to narrow the list.' : data.choices.length ? '' : 'No shared recipes match.';
    } catch (error) {
      if (g !== choiceGeneration || error.name === 'AbortError') return;
      // Old search results must not remain actionable after a failed search.
      choiceRows = []; choicesLoading = false; renderChoices();
      $('plannerPickerStatus').textContent = `${error.message} Change your search to try again.`;
    }
  }
  $('plannerChoose').addEventListener('click', () => { if (canChange()) { picker.showModal(); choices(); } });
  $('plannerClose').addEventListener('click', () => picker.close());
  let timer;
  $('plannerSearch').addEventListener('input', () => {
    clearTimeout(timer); choiceController?.abort(); ++choiceGeneration;
    choicesLoading = true; syncControls(); timer = setTimeout(choices, 220);
  });
  $('plannerWeek').addEventListener('change', () => {
    $('plannerWeek').value = window.RecipeWeek.select(selectedWeek()); load();
  });
  $('plannerRetry').addEventListener('click', load);
  $('plannerExtra').addEventListener('input', () => extraDrafts.set(selectedWeek(), $('plannerExtra').value));
  $('plannerAdd').addEventListener('submit', event => { event.preventDefault(); change({action: 'add_item', text: $('plannerExtra').value}); });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') load(); });
  window.addEventListener('pageshow', event => { if (event.persisted) load(); });
  load();
})();
