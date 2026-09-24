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
const notesPanel = window.AsyncPanel.create({
  root: '#notesPanel', loading: '#notesLoading', content: '#noteGrid',
  empty: '#notesEmpty', noResults: '#notesNoResults', error: '#notesError'
});
const notesAnnouncer = window.DavidPiAnnouncer.create();
const resultCount = document.querySelector('#noteResultCount');
const retryButton = document.querySelector('#notesRetry');
let activeView = 'all', activeNote = null, activeSession = null, saveTimer = null, dirty = false, loadingGeneration = 0, pendingConfirm = null, activeListController = null;
let openGeneration = 0, openingController = null, creating = false, notesOffset = 0, notesTotal = 0, notesAppending = false;
const noteSessions = new Map(), noteIds = new Set(), notesMore = document.querySelector('#notesMore');
const draftPrefix = `david-pi:notes:${document.querySelector('meta[name="notes-draft-scope"]').content}:`;
let draftStorageAvailable = true;
function storeDraft(id, value) {
  try { if (value) sessionStorage.setItem(draftPrefix + id, JSON.stringify(value)); else sessionStorage.removeItem(draftPrefix + id); }
  catch (_) { draftStorageAvailable = false; }
}
function readDraft(id) { try { return JSON.parse(sessionStorage.getItem(draftPrefix + id) || 'null'); } catch (_) { return null; } }
async function api(url, options = {}) {
  options.headers = {...options.headers, 'X-CSRF-Token': csrf};
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
  const abort = () => controller.abort(); options.signal?.addEventListener('abort', abort, {once:true});
  if (options.signal?.aborted) controller.abort();
  try {
    const response = await fetch(url, {...options, signal:controller.signal});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error(data.error || 'Something went wrong.'); error.data = data; throw error; }
    return data;
  }
  finally { clearTimeout(timer); options.signal?.removeEventListener('abort', abort); }
}
function showToast(text) { toast.textContent = text; toast.hidden = false; clearTimeout(showToast.timer); showToast.timer = setTimeout(() => { toast.hidden = true; }, 2600); }
function noteSummary(note) {
  return note.summary || 'Empty note';
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
async function loadNotes({append = false} = {}) {
  if (append && notesAppending) return;
  if (!append) { ++loadingGeneration; notesOffset = 0; notesIdsReset(); activeListController?.abort(); activeListController = new AbortController(); }
  const generation = loadingGeneration;
  notesAppending = append; notesMore.disabled = true;
  const search = document.querySelector('#noteSearch').value.trim();
  const query = new URLSearchParams({view: activeView, q: search, limit:40, offset:notesOffset});
  if (!append) { notesPanel.begin(generation); resultCount.hidden = true; notesMore.hidden = true; }
  try {
    const data = await api(`/api/notes?${query}`, {signal: activeListController.signal});
    if (generation !== loadingGeneration) return;
    if (!append) { grid.innerHTML = ''; notesTotal = data.total ?? data.count ?? data.notes.length; }
    data.notes.forEach(note => { if (!noteIds.has(note.id)) { noteIds.add(note.id); grid.append(noteCard(note)); } });
    notesOffset = data.next_offset ?? (notesOffset + data.notes.length); notesMore.hidden = !data.has_more;
    const count = notesTotal;
    resultCount.textContent = data.has_more ? `${noteIds.size} of ${count} notes` : `${count} ${count === 1 ? 'note' : 'notes'}`;
    resultCount.hidden = false;
    notesPanel.success(generation, {empty: count === 0 && !search, noResults: count === 0 && Boolean(search)});
    notesAnnouncer.polite(count ? `${count} notes loaded.` : (search ? 'No matching notes.' : 'No notes yet.'));
  } catch (error) {
    if (error.name === 'AbortError' || generation !== loadingGeneration) return;
    document.querySelector('#notesErrorText').textContent = error.message;
    if (!append) notesPanel.transition('error'); else showToast(`More notes could not load. ${error.message}`);
    notesAnnouncer.alert(`Notes could not be loaded. ${error.message}`);
  } finally { if (generation === loadingGeneration) { notesAppending = false; notesMore.disabled = false; } }
}
function notesIdsReset() { noteIds.clear(); }
function checklistValue() {
  return [...checklistItems.querySelectorAll('.checklist-row')].map((row) => ({id: row.dataset.id, text: row.querySelector('[type=text]').value, done: row.querySelector('[type=checkbox]').checked})).filter((item) => item.text.trim());
}
function addChecklistRow(item = {}) {
  const row = document.createElement('div'); row.className = 'checklist-row'; row.dataset.id = item.id || crypto.randomUUID();
  const check = document.createElement('input'); check.type = 'checkbox'; check.checked = Boolean(item.done); check.setAttribute('aria-label', 'Completed');
  const text = document.createElement('input'); text.type = 'text'; text.maxLength = 500; text.value = item.text || ''; text.placeholder = 'List item'; text.setAttribute('aria-label', 'Checklist item');
  const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '×'; remove.setAttribute('aria-label', 'Remove item');
  [check, text].forEach((control) => control.addEventListener('input', changed)); remove.addEventListener('click', () => { row.remove(); changed(); });
  const editable = !activeNote || (activeNote.can_edit && !activeNote.deleted_at);
  check.disabled = !editable; text.disabled = !editable; remove.hidden = !editable;
  row.append(check, text, remove); checklistItems.append(row); return text;
}
function renderEditor(note, attach = true) {
  activeNote = note; titleInput.value = note.title; bodyInput.value = note.body; typeInput.value = note.note_type; visibilityInput.value = note.visibility; ownerLabel.textContent = `Added by ${note.owner_display}${note.can_edit ? '' : ' · Read-only'}`; tagsInput.value = note.tags.join(', ');
  titleInput.scrollLeft = 0;
  checklistItems.innerHTML = ''; note.checklist.forEach(addChecklistRow); updateType(); dirty = false; saveState.textContent = note.can_edit ? 'Saved' : 'Read-only';
  [titleInput, bodyInput, typeInput, visibilityInput, tagsInput].forEach(control => { control.disabled = !note.can_edit || Boolean(note.deleted_at); });
  document.querySelector('#addChecklistItem').hidden = !note.can_edit || Boolean(note.deleted_at);
  document.querySelector('#addChecklistItem').disabled = !note.can_edit || Boolean(note.deleted_at);
  noteMenuButton.hidden = !note.can_edit; noteMenuButton.disabled = !note.can_edit;
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
  if (attach) {
    let session = noteSessions.get(note.id);
    if (!session || (!session.dirty && !session.pending)) {
      session = new NoteSaveSession(note, {
        save: (id, payload) => api(`/api/notes/${id}`, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}),
        changed: updateSaveState, store: value => storeDraft(note.id, value)
      });
      noteSessions.set(note.id, session); session.restore(readDraft(note.id));
    }
    if (session.dirty && !session.pending && note.version !== session.note.version) session.conflict = note;
    activeSession = session;
    if (session.dirty) renderEditor({...session.note, ...session.value}, false);
    updateSaveState(session);
  }
}
function updateSaveState(session) {
  if (session !== activeSession) return;
  activeNote = session.note; dirty = session.dirty;
  saveState.textContent = !activeNote.can_edit ? 'Read-only' : activeNote.deleted_at ? 'Restore to edit' : session.conflict ? 'Changed elsewhere' : session.error ? 'Couldn’t save' : session.pending ? 'Saving…' : session.dirty ? 'Unsaved' : 'Saved';
  const help = document.querySelector('#noteSaveHelp'); help.hidden = !(session.error || session.conflict);
  document.querySelector('#noteSaveMessage').textContent = session.conflict ? 'Your draft and the saved version differ. Compare them before choosing.' : session.error;
  document.querySelector('#noteConflictActions').hidden = !session.conflict;
  document.querySelector('#retryNoteSave').hidden = Boolean(session.conflict);
  const latest = session.conflict;
  document.querySelector('#noteKeepDraft').hidden = Boolean(latest?.deleted_at);
  document.querySelector('#noteLatestText').textContent = latest ? `${latest.title}\n\n${latest.body}\n${latest.checklist.map(item => `${item.done ? '☑' : '☐'} ${item.text}`).join('\n')}` : '';
}
async function openNote(id) {
  const generation = ++openGeneration; openingController?.abort(); openingController = new AbortController();
  clearTimeout(saveTimer); if (activeSession) await activeSession.flush();
  if (generation !== openGeneration) return;
  try { const data = await api(`/api/notes/${id}`, {signal:openingController.signal}); if (generation !== openGeneration) return;
    renderEditor(data.note); if (!editor.open) editor.showModal(); setTimeout(() => (data.note.can_edit ? titleInput : document.querySelector('#closeNote')).focus(), 30);
  } catch (error) { if (generation === openGeneration) showToast(`Note could not open. ${error.message}`); }
}
async function createNote() {
  if (creating) return; creating = true; const generation = ++openGeneration;
  try { if (activeSession) await activeSession.flush();
    const data = await api('/api/notes', {method: 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({visibility:'shared'})});
    if (generation !== openGeneration) { loadNotes(); return; }
    renderEditor(data.note); if (!editor.open) editor.showModal(); setTimeout(() => bodyInput.focus(), 30);
  } catch (error) { showToast(`Note could not be created. ${error.message}`); } finally { creating = false; }
}
function changed() {
  if (!activeNote?.can_edit || activeNote.deleted_at || !activeSession || activeSession.statePending) return;
  activeSession.edit({title:titleInput.value, body:bodyInput.value, visibility:visibilityInput.value, note_type:typeInput.value, tags:tagsInput.value, checklist:checklistValue()});
  clearTimeout(saveTimer); const session = activeSession; saveTimer = setTimeout(() => saveNote(session), 700);
}
async function saveNote(session = activeSession) {
  if (!session?.note.can_edit) return true;
  const saved = await session.flush(); if (saved) await loadNotes(); return saved;
}
function updateType() { const checklist = typeInput.value === 'checklist'; bodyInput.hidden = checklist; checklistEditor.hidden = !checklist; }
function closeNoteMenu() { noteMenu.hidden = true; noteMenuButton.setAttribute('aria-expanded', 'false'); }
function setNoteStateBusy(session, busy) {
  session.statePending = busy;
  if (activeSession !== session) return;
  [titleInput, bodyInput, typeInput, visibilityInput, tagsInput].forEach(control => { control.disabled = busy || !session.note.can_edit || Boolean(session.note.deleted_at); });
  noteMenuButton.disabled = busy || !session.note.can_edit;
  checklistItems.querySelectorAll('input,button').forEach(control => { control.disabled = busy || !session.note.can_edit || Boolean(session.note.deleted_at); });
  document.querySelector('#addChecklistItem').disabled = busy;
}
async function applyNoteAction(session, action, permanent = false) {
  if (session.statePending) return;
  if (!await session.flush()) { showToast('Save or resolve your draft before changing this note.'); return; }
  if (session.statePending) return;
  const target = {...session.note};
  setNoteStateBusy(session, true);
  try {
    await api(permanent ? `/api/notes/${target.id}` : `/api/notes/${target.id}/state`, {
      method:permanent ? 'DELETE' : 'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify(permanent ? {confirm:'permanently delete',version:target.version} : {action,version:target.version})
    });
    if (noteSessions.get(target.id) === session) noteSessions.delete(target.id);
    if (activeSession === session) {
      ++openGeneration; editor.close(); activeNote = null; activeSession = null; dirty = false;
    }
    await loadNotes();
    showToast(permanent ? 'Note permanently deleted.' : action === 'trash' ? 'Moved to Recently Deleted.' : action === 'restore' ? 'Note restored.' : action.includes('archive') ? 'Archive updated.' : 'Pin updated.');
  } finally { setNoteStateBusy(session, false); }
}
async function closeEditor() {
  const generation = ++openGeneration, session = activeSession;
  openingController?.abort(); clearTimeout(saveTimer); closeNoteMenu();
  if (session) await session.flush();
  if (generation !== openGeneration || session !== activeSession) return;
  if (session?.dirty && !draftStorageAvailable) { showToast('Draft storage is full. Copy your text before leaving this note.'); return; }
  if (session?.dirty) showToast('Your draft is kept in this tab. Open the note to continue.');
  editor.close(); activeNote = null; activeSession = null; dirty = false; loadNotes();
}
[titleInput, bodyInput, typeInput, visibilityInput, tagsInput].forEach((control) => control.addEventListener('input', () => {
  if (control === typeInput) updateType();
  changed();
}));
titleInput.addEventListener('blur', () => { titleInput.scrollLeft = 0; });
document.querySelector('#addChecklistItem').addEventListener('click', () => { if (checklistItems.children.length >= 100) { showToast('This checklist has 100 items. Start another note for more.'); return; } const input = addChecklistRow(); changed(); input.focus(); });
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
    clearTimeout(saveTimer); const session = activeSession;
    if (!session || !await session.flush() || session !== activeSession) { showToast('Save or resolve your draft before changing this note.'); return; }
    if (session.statePending) return;
    if (action === 'pin') action = session.note.pinned ? 'unpin' : 'pin';
    if (action === 'archive') action = session.note.archived ? 'unarchive' : 'archive';
    if (action === 'trash' && session.note.deleted_at) {
      pendingConfirm = () => applyNoteAction(session, action, true);
      document.querySelector('#noteConfirmTitle').textContent = 'Request permanent deletion?'; document.querySelector('#noteConfirmText').textContent = 'Deletion remains paused until a protected backup newer than this trash action is verified.'; document.querySelector('#noteConfirm').showModal(); return;
    }
    if (action === 'trash') {
      pendingConfirm = () => applyNoteAction(session, action);
      document.querySelector('#noteConfirmTitle').textContent = 'Move this note?'; document.querySelector('#noteConfirmText').textContent = 'It stays in Recently Deleted until you restore or permanently delete it.'; document.querySelector('#noteConfirm').showModal(); return;
    }
    await applyNoteAction(session, action);
  } catch (error) {
    showToast(error.message);
  }
}));
document.querySelector('#cancelNoteConfirm').addEventListener('click', () => { pendingConfirm = null; document.querySelector('#noteConfirm').close(); });
document.querySelector('#acceptNoteConfirm').addEventListener('click', async () => { const button = document.querySelector('#acceptNoteConfirm'); button.disabled = true; try { if (pendingConfirm) await pendingConfirm(); document.querySelector('#noteConfirm').close(); } catch (error) { showToast(error.message); } finally { pendingConfirm = null; button.disabled = false; } });
window.DavidPiFilterGroup.create('#noteViews', {onChange: (button) => { activeView = button.dataset.view; loadNotes(); }});
retryButton.addEventListener('click', loadNotes);
notesMore.addEventListener('click', () => loadNotes({append:true}));
document.querySelector('#retryNoteSave').addEventListener('click', () => saveNote());
document.querySelector('#noteUseLatest').addEventListener('click', () => { activeSession?.useLatest(); if (activeSession) { renderEditor(activeSession.note, false); updateSaveState(activeSession); } });
document.querySelector('#noteKeepDraft').addEventListener('click', () => { activeSession?.keepDraft(); saveNote(); });
document.querySelector('#noteCopyDraft').addEventListener('click', async () => {
  if (!activeSession) return;
  try { await navigator.clipboard.writeText(JSON.stringify(activeSession.value, null, 2)); showToast('Draft copied.'); }
  catch (_) { showToast('Copy is unavailable. Select your text and copy it manually.'); }
});
window.addEventListener('beforeunload', event => { if ([...noteSessions.values()].some(session => session.dirty || session.pending)) { event.preventDefault(); event.returnValue = ''; } });
window.addEventListener('pagehide', () => { clearTimeout(saveTimer); if (activeSession) { activeSession.emit(); activeSession.flush(); } });
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') loadNotes(); else if (activeSession) activeSession.flush(); });
window.addEventListener('pageshow', event => { if (event.persisted) loadNotes(); });
let searchTimer; document.querySelector('#noteSearch').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadNotes, 250); });
loadNotes().catch((error) => showToast(error.message));
