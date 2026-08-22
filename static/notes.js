const csrf = document.querySelector('meta[name="csrf-token"]').content;
const grid = document.querySelector('#noteGrid');
const empty = document.querySelector('#notesEmpty');
const editor = document.querySelector('#noteEditor');
const titleInput = document.querySelector('#noteTitle');
const bodyInput = document.querySelector('#noteBody');
const typeInput = document.querySelector('#noteType');
const visibilityInput = document.querySelector('#noteVisibility');
const ownerLabel = document.querySelector('#noteOwnerLabel');
const tagsInput = document.querySelector('#noteTags');
const checklistEditor = document.querySelector('#checklistEditor');
const checklistItems = document.querySelector('#checklistItems');
const saveState = document.querySelector('#saveState');
const toast = document.querySelector('#platformToast');
const noteMenu = document.querySelector('#noteMenu');
const noteMenuButton = document.querySelector('#noteMenuButton');
let activeView = 'all', activeNote = null, saveTimer = null, dirty = false, loadingGeneration = 0, pendingConfirm = null;
async function api(url, options = {}) {
  options.headers = {...options.headers, 'X-CSRF-Token': csrf};
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) { const error = new Error(data.error || 'Something went wrong.'); error.data = data; throw error; }
  return data;
}
function showToast(text) { toast.textContent = text; toast.hidden = false; clearTimeout(showToast.timer); showToast.timer = setTimeout(() => { toast.hidden = true; }, 2600); }
function noteSummary(note) {
  if (note.note_type === 'checklist') {
    const done = note.checklist.filter((item) => item.done).length;
    return note.checklist.length ? `${done} of ${note.checklist.length} complete` : 'Empty checklist';
  }
  return note.body || 'Empty note';
}
function noteCard(note) {
  const button = document.createElement('button'); button.className = `note-card${note.pinned ? ' pinned' : ''}`; button.type = 'button';
  const heading = document.createElement('h3'); heading.textContent = note.title || 'Untitled';
  const copy = document.createElement('p'); copy.textContent = noteSummary(note);
  const footer = document.createElement('footer');
  const owner = document.createElement('span'); owner.className = 'owner'; owner.textContent = `${note.visibility === 'private' ? 'Only me · ' : ''}Added by ${note.owner_display}`;
  const time = document.createElement('span'); time.textContent = new Date(note.updated_at).toLocaleDateString([], {month: 'short', day: 'numeric'});
  footer.append(owner, time); button.append(heading, copy, footer); button.addEventListener('click', () => openNote(note.id)); return button;
}
async function loadNotes() {
  const generation = ++loadingGeneration;
  const query = new URLSearchParams({view: activeView, q: document.querySelector('#noteSearch').value});
  const data = await api(`/api/notes?${query}`);
  if (generation !== loadingGeneration) return;
  grid.innerHTML = ''; data.notes.forEach((note) => grid.append(noteCard(note)));
  empty.hidden = data.notes.length !== 0;
}
function checklistValue() {
  return [...checklistItems.querySelectorAll('.checklist-row')].map((row) => ({id: row.dataset.id, text: row.querySelector('[type=text]').value, done: row.querySelector('[type=checkbox]').checked})).filter((item) => item.text.trim());
}
function addChecklistRow(item = {}) {
  const row = document.createElement('div'); row.className = 'checklist-row'; row.dataset.id = item.id || crypto.randomUUID();
  const check = document.createElement('input'); check.type = 'checkbox'; check.checked = Boolean(item.done); check.setAttribute('aria-label', 'Completed');
  const text = document.createElement('input'); text.type = 'text'; text.value = item.text || ''; text.placeholder = 'List item';
  const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '×'; remove.setAttribute('aria-label', 'Remove item');
  [check, text].forEach((control) => control.addEventListener('input', changed)); remove.addEventListener('click', () => { row.remove(); changed(); });
  row.append(check, text, remove); checklistItems.append(row); return text;
}
function renderEditor(note) {
  activeNote = note; titleInput.value = note.title; bodyInput.value = note.body; typeInput.value = note.note_type; visibilityInput.value = note.visibility; ownerLabel.textContent = `Added by ${note.owner_display}`; tagsInput.value = note.tags.join(', ');
  titleInput.scrollLeft = 0;
  checklistItems.innerHTML = ''; note.checklist.forEach(addChecklistRow); updateType(); dirty = false; saveState.textContent = 'Saved';
  const trashButton = document.querySelector('[data-note-action=trash]');
  trashButton.textContent = note.deleted_at ? 'Delete forever' : 'Move to Recently Deleted';
  const pinButton = document.querySelector('[data-note-action=pin]');
  const archiveButton = document.querySelector('[data-note-action=archive]');
  const restoreButton = document.querySelector('[data-note-action=restore]');
  pinButton.textContent = note.pinned ? 'Unpin' : 'Pin';
  archiveButton.textContent = note.archived ? 'Unarchive' : 'Archive';
  pinButton.hidden = Boolean(note.deleted_at);
  archiveButton.hidden = Boolean(note.deleted_at);
  restoreButton.hidden = !note.deleted_at;
}
async function openNote(id) { const data = await api(`/api/notes/${id}`); renderEditor(data.note); editor.showModal(); setTimeout(() => titleInput.focus(), 30); }
async function createNote() {
  const data = await api('/api/notes', {method: 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({visibility:'shared'})});
  renderEditor(data.note); editor.showModal(); setTimeout(() => bodyInput.focus(), 30);
}
function changed() { dirty = true; saveState.textContent = 'Unsaved'; clearTimeout(saveTimer); saveTimer = setTimeout(saveNote, 700); }
async function saveNote() {
  if (!activeNote || !dirty) return;
  dirty = false; saveState.textContent = 'Saving…';
  const payload = {version: activeNote.version, title:titleInput.value, body:bodyInput.value, visibility:visibilityInput.value, note_type:typeInput.value, tags:tagsInput.value, checklist:checklistValue()};
  try {
    const data = await api(`/api/notes/${activeNote.id}`, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    activeNote = data.note; saveState.textContent = 'Saved'; await loadNotes();
  } catch (error) {
    dirty = true; saveState.textContent = error.data?.conflict ? 'Changed elsewhere' : 'Couldn’t save';
    if (error.data?.conflict) showToast('This note changed elsewhere. Your text is still here.');
  }
}
function updateType() { const checklist = typeInput.value === 'checklist'; bodyInput.hidden = checklist; checklistEditor.hidden = !checklist; }
function closeNoteMenu() { noteMenu.hidden = true; noteMenuButton.setAttribute('aria-expanded', 'false'); }
async function closeEditor() { clearTimeout(saveTimer); closeNoteMenu(); if (dirty) await saveNote(); if (!dirty) { editor.close(); activeNote = null; } }
[titleInput, bodyInput, typeInput, visibilityInput, tagsInput].forEach((control) => control.addEventListener('input', () => {
  if (control === typeInput) updateType();
  changed();
}));
titleInput.addEventListener('blur', () => { titleInput.scrollLeft = 0; });
document.querySelector('#addChecklistItem').addEventListener('click', () => { const input = addChecklistRow(); changed(); input.focus(); });
document.querySelector('#newNote').addEventListener('click', createNote); document.querySelector('#emptyNewNote').addEventListener('click', createNote); document.querySelector('#closeNote').addEventListener('click', closeEditor);
editor.addEventListener('cancel', (event) => { event.preventDefault(); closeEditor(); });
editor.addEventListener('close', closeNoteMenu);
noteMenuButton.setAttribute('aria-expanded', 'false');
noteMenuButton.addEventListener('click', (event) => {
  event.stopPropagation();
  noteMenu.hidden = !noteMenu.hidden;
  noteMenuButton.setAttribute('aria-expanded', String(!noteMenu.hidden));
});
document.addEventListener('pointerdown', (event) => {
  if (!noteMenu.hidden && !noteMenu.contains(event.target) && !noteMenuButton.contains(event.target)) closeNoteMenu();
});
document.querySelectorAll('[data-note-action]').forEach((button) => button.addEventListener('click', async () => {
  closeNoteMenu();
  let action = button.dataset.noteAction;
  try {
    if (action === 'pin') action = activeNote.pinned ? 'unpin' : 'pin';
    if (action === 'archive') action = activeNote.archived ? 'unarchive' : 'archive';
    if (action === 'restore') {
      await api(`/api/notes/${activeNote.id}/state`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action})});
      editor.close(); activeNote = null; await loadNotes(); showToast('Note restored.'); return;
    }
    if (action === 'trash' && activeNote.deleted_at) {
      pendingConfirm = async () => { await api(`/api/notes/${activeNote.id}`, {method: 'DELETE'}); editor.close(); await loadNotes(); showToast('Note permanently deleted.'); };
      document.querySelector('#noteConfirmTitle').textContent = 'Delete forever?'; document.querySelector('#noteConfirmText').textContent = 'This note cannot be recovered.'; document.querySelector('#noteConfirm').showModal(); return;
    }
    if (action === 'trash') {
      pendingConfirm = async () => { await api(`/api/notes/${activeNote.id}/state`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action})}); editor.close(); await loadNotes(); showToast('Moved to Recently Deleted.'); };
      document.querySelector('#noteConfirmTitle').textContent = 'Move this note?'; document.querySelector('#noteConfirmText').textContent = 'You can restore it from Recently Deleted for 30 days.'; document.querySelector('#noteConfirm').showModal(); return;
    }
    await api(`/api/notes/${activeNote.id}/state`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action})}); editor.close(); await loadNotes(); showToast(action.includes('archive') ? 'Archive updated.' : 'Pin updated.');
  } catch (error) {
    showToast(`${error.message} Nothing was changed.`);
  }
}));
document.querySelector('#cancelNoteConfirm').addEventListener('click', () => { pendingConfirm = null; document.querySelector('#noteConfirm').close(); });
document.querySelector('#acceptNoteConfirm').addEventListener('click', async () => { const button = document.querySelector('#acceptNoteConfirm'); button.disabled = true; try { if (pendingConfirm) await pendingConfirm(); document.querySelector('#noteConfirm').close(); } catch (error) { showToast(error.message); } finally { pendingConfirm = null; button.disabled = false; } });
document.querySelector('#noteViews').addEventListener('click', (event) => { const button = event.target.closest('[data-view]'); if (!button) return; activeView = button.dataset.view; document.querySelectorAll('[data-view]').forEach((item) => item.classList.toggle('selected', item === button)); loadNotes(); });
let searchTimer; document.querySelector('#noteSearch').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadNotes, 250); });
loadNotes().catch((error) => showToast(error.message));
