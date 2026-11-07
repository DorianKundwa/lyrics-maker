/**
 * Lyric Editor — Shared State, Undo/Redo Stack, Time Helpers
 * Exposes: window.LE
 */
(function () {
  'use strict';

  const MAX_UNDO = 50;

  window.LE = {
    // ── State ────────────────────────────────────────────────────────────────
    jobId:            null,
    segments:         [],     // [{index, start, end, text, y_offset, words}]
    originalSegments: null,
    totalDuration:    0,
    fontName:         'Arial',
    fontSize:         72,
    selectedIndex:    -1,

    // ── Internal ─────────────────────────────────────────────────────────────
    _undoStack:        [],
    _redoStack:        [],
    _changeListeners:  [],
    _selectListeners:  [],

    // ── Time Helpers ─────────────────────────────────────────────────────────

    /** Convert seconds to display string "m:ss.cc" */
    toDisplay(secs) {
      const s = Math.max(0, +secs || 0);
      const m = Math.floor(s / 60);
      const rem = (s - m * 60).toFixed(2).padStart(5, '0');
      return `${m}:${rem}`;
    },

    /**
     * Parse "m:ss.cc" or "ss.cc" back to seconds.
     * Returns null when the text is not a usable time: NaN would become null in
     * JSON and poison the saved alignment (nothing can render a null start).
     */
    fromDisplay(str) {
      const t = String(str || '').trim();
      if (!t) return 0;
      const parts = t.split(':');
      if (parts.length === 2) {
        const mins = parseFloat(parts[0]);
        const secs = parseFloat(parts[1]);
        if (!isFinite(mins) || !isFinite(secs)) return null;
        return Math.max(0, mins * 60 + secs);
      }
      const secs = parseFloat(t);
      if (!isFinite(secs)) return null;
      return Math.max(0, secs);
    },

    // ── Undo / Redo ──────────────────────────────────────────────────────────

    _snap() {
      return JSON.stringify(this.segments);
    },

    pushUndo() {
      this._undoStack.push(this._snap());
      if (this._undoStack.length > MAX_UNDO) this._undoStack.shift();
      this._redoStack = [];
      this._syncUndoBtns();
    },

    /** Persist like any other edit; LE_Actions loads after this file. */
    _autoSave() {
      if (typeof LE_Actions !== 'undefined') LE_Actions.scheduleAutoSave();
    },

    undo() {
      if (!this._undoStack.length) return;
      this._redoStack.push(this._snap());
      this.segments = JSON.parse(this._undoStack.pop());
      this._syncUndoBtns();
      this.notify();
      this._autoSave();
    },

    redo() {
      if (!this._redoStack.length) return;
      this._undoStack.push(this._snap());
      this.segments = JSON.parse(this._redoStack.pop());
      this._syncUndoBtns();
      this.notify();
      this._autoSave();
    },

    _syncUndoBtns() {
      const u = document.getElementById('leUndo');
      const r = document.getElementById('leRedo');
      if (u) u.disabled = !this._undoStack.length;
      if (r) r.disabled = !this._redoStack.length;
    },

    // ── Event Bus ────────────────────────────────────────────────────────────

    /** Register a listener called with (segments) on any data change */
    onChange(fn) { this._changeListeners.push(fn); },

    /** Broadcast segment changes to all listeners */
    notify() { this._changeListeners.forEach(fn => fn(this.segments)); },

    /** Register a listener called with (index) on row selection */
    onSelect(fn) { this._selectListeners.push(fn); },

    /** Select a segment; notifies all selection listeners */
    select(i) {
      this.selectedIndex = i;
      this._selectListeners.forEach(fn => fn(i));
    },
  };
})();
