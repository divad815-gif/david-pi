(function (root) {
  'use strict';
  const copy = value => JSON.parse(JSON.stringify(value));
  function draft(note) {
    const tags = Array.isArray(note.tags) ? note.tags : String(note.tags || '').split(',');
    const unique = [];
    for (const value of tags) {
      const tag = String(value).replace(/\s+/g, ' ').trim().slice(0, 30);
      if (tag && !unique.some(item => item.toLowerCase() === tag.toLowerCase())) unique.push(tag);
    }
    return {
      title: String(note.title || '').replace(/\0/g, '').slice(0, 200),
      body: String(note.body || '').replace(/\0/g, '').slice(0, 100000),
      visibility: note.visibility, note_type: note.note_type, tags: unique.slice(0, 12),
      checklist: (note.checklist || []).slice(0, 100).filter(item => String(item.text || '').trim()).map(item => ({
        id: String(item.id), text: String(item.text).replace(/\0/g, '').trim().slice(0, 500), done: Boolean(item.done)
      }))
    };
  }
  const same = (left, right) => JSON.stringify(draft(left)) === JSON.stringify(draft(right));
  class NoteSaveSession {
    constructor(note, {save, changed = () => {}, store = () => {}}) {
      this.id = note.id; this.note = copy(note); this.value = draft(note);
      this.save = save; this.changed = changed; this.store = store;
      this.revision = 0; this.acknowledged = 0; this.pending = null;
      this.conflict = null; this.error = ''; this.draftId = `${note.id}:${Date.now()}:${Math.random()}`;
    }
    get dirty() { return this.revision !== this.acknowledged; }
    emit() { this.store(this.dirty ? this.snapshot() : null); this.changed(this); }
    snapshot() { return {id: this.id, draftId: this.draftId, version: this.note.version, value: copy(this.value)}; }
    edit(value) { this.value = draft(value); this.revision++; this.error = ''; this.emit(); }
    restore(saved) {
      if (!saved || saved.id !== this.id || !saved.value) return;
      this.value = draft(saved.value); this.draftId = saved.draftId || this.draftId; this.revision++;
      if (same(this.value, this.note)) this.acknowledged = this.revision;
      else if (saved.version !== this.note.version) this.conflict = copy(this.note);
      this.emit();
    }
    useLatest() {
      if (!this.conflict) return;
      this.note = copy(this.conflict); this.value = draft(this.note); this.conflict = null;
      this.revision++; this.acknowledged = this.revision; this.error = ''; this.emit();
    }
    keepDraft() {
      if (!this.conflict) return;
      this.note = copy(this.conflict); this.conflict = null; this.error = ''; this.emit();
    }
    async flush() {
      if (this.pending) return this.pending;
      if (!this.dirty || this.conflict) return !this.dirty;
      // One request per note. Edits during the request coalesce into its next save.
      this.pending = this.drain(); this.emit();
      try { return await this.pending; }
      finally { this.pending = null; this.emit(); }
    }
    async drain() {
      while (this.dirty && !this.conflict) {
        const revision = this.revision, payload = {...copy(this.value), version: this.note.version};
        try {
          const result = await this.save(this.id, payload, this.draftId);
          if (!result?.note || result.note.id !== this.id || result.note.version <= payload.version || !same(payload, result.note)) {
            throw new Error('The saved text could not be confirmed. Your draft is kept here.');
          }
          this.note = copy(result.note); this.acknowledged = revision; this.error = '';
        } catch (error) {
          const latest = error.data?.latest;
          if (latest?.id === this.id && latest.version > payload.version && same(payload, latest)) {
            // The server committed but its first response was lost.
            this.note = copy(latest); this.acknowledged = revision; this.error = '';
          } else {
            this.conflict = latest?.id === this.id ? copy(latest) : null;
            this.error = error.message || 'Could not save. Your draft is kept here.';
            this.emit(); return false;
          }
        }
        this.emit();
      }
      return !this.dirty;
    }
  }
  root.NoteSaveSession = NoteSaveSession;
  if (typeof module !== 'undefined') module.exports = {NoteSaveSession, noteDraft: draft};
})(typeof globalThis !== 'undefined' ? globalThis : this);
