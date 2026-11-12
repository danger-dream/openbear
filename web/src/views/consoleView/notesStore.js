import { computed, inject, provide, reactive, ref, watch } from 'vue';
import { Api, apiError } from '../../api.js';

export const USER_NOTES_KEY = Symbol('openbear:user-notes');

export function createUserNotesStore(conversationUuidRef, { enabledRef } = {}) {
  const state = reactive({
    sessionNotes: [],
    globalSheets: [],
    activeScope: 'session', // 'session' | 'global'
    activeSheetUuid: '',
    loading: false,
    saving: false,
    error: '',
    loadedConversationUuid: null,
  });

  const desktopOpen = ref(false);
  const desktopPinned = ref(false);
  const desktopMinimized = ref(false);
  const mobileOpen = ref(false);
  const draftQuote = ref(null); // { opId, turnUuid, turnNumber, quote, conversationUuid }

  let requestSeq = 0;

  const usableConversationUuid = computed(() => {
    const raw = String(conversationUuidRef?.value || '').trim();
    return raw && !raw.startsWith('local:') ? raw : '';
  });

  const activeSheet = computed(() => {
    const sheets = state.globalSheets;
    if (!sheets.length) return null;
    return sheets.find((s) => s.sheetUuid === state.activeSheetUuid) || sheets[0];
  });

  const sessionCount = computed(() => state.sessionNotes.length);
  const globalNoteCount = computed(() =>
    state.globalSheets.reduce((sum, s) => sum + (Array.isArray(s.notes) ? s.notes.length : 0), 0),
  );
  const badgeCount = computed(() =>
    usableConversationUuid.value ? sessionCount.value : globalNoteCount.value,
  );

  function syncActiveSheet(sheets) {
    if (!Array.isArray(sheets) || !sheets.length) {
      state.activeSheetUuid = '';
      return;
    }
    if (!sheets.some((s) => s.sheetUuid === state.activeSheetUuid)) {
      state.activeSheetUuid = sheets[0].sheetUuid;
    }
  }

  async function refresh(force = false) {
    if (enabledRef && !enabledRef.value) return;
    const conv = usableConversationUuid.value;
    if (!force && state.loadedConversationUuid === conv && state.globalSheets.length) return;
    const seq = ++requestSeq;
    state.loading = true;
    state.error = '';
    try {
      const data = await Api.notes(conv);
      if (seq !== requestSeq) return;
      state.sessionNotes = Array.isArray(data?.sessionNotes) ? data.sessionNotes : [];
      state.globalSheets = Array.isArray(data?.globalSheets) ? data.globalSheets : [];
      state.loadedConversationUuid = conv;
      syncActiveSheet(state.globalSheets);
      if (!conv && state.activeScope === 'session') {
        state.activeScope = 'global';
      }
    } catch (err) {
      if (seq === requestSeq) state.error = apiError(err);
    } finally {
      if (seq === requestSeq) state.loading = false;
    }
  }

  watch(
    () => [usableConversationUuid.value, enabledRef ? enabledRef.value : true],
    ([conv, enabled], [prevConv] = []) => {
      if (!enabled) {
        desktopOpen.value = false;
        mobileOpen.value = false;
        draftQuote.value = null;
        return;
      }
      if (conv !== prevConv) {
        draftQuote.value = null;
        state.sessionNotes = [];
        if (!conv) state.activeScope = 'global';
        else if (!prevConv && state.activeScope === 'global' && !desktopPinned.value) {
          state.activeScope = 'session';
        }
      }
      if (conv) void refresh(true);
    },
    { immediate: true },
  );

  async function createNote({
    scope = state.activeScope,
    sheetUuid = '',
    opId = '',
    turnUuid = '',
    turnNumber = 0,
    quote = '',
    content = '',
  } = {}) {
    const resolvedScope = scope === 'session' && !usableConversationUuid.value ? 'global' : scope;
    const targetSheetUuid = resolvedScope === 'global' ? (sheetUuid || activeSheet.value?.sheetUuid || '') : '';
    state.saving = true;
    try {
      const data = await Api.createNote({
        scope: resolvedScope,
        conversationUuid: usableConversationUuid.value,
        sheetUuid: targetSheetUuid,
        opId,
        turnUuid,
        turnNumber,
        quote,
        content,
      });
      const created = data?.note;
      if (created) {
        if (created.scope === 'session') {
          state.sessionNotes = [created, ...state.sessionNotes.filter((n) => n.noteUuid !== created.noteUuid)];
        } else {
          if (!state.globalSheets.length) {
            await refresh(true);
          } else {
            const sheet = state.globalSheets.find((s) => s.sheetUuid === created.sheetUuid) || state.globalSheets[0];
            if (sheet) {
              sheet.notes = [created, ...(sheet.notes || []).filter((n) => n.noteUuid !== created.noteUuid)];
              state.activeSheetUuid = sheet.sheetUuid;
            }
          }
        }
      }
      return created;
    } finally {
      state.saving = false;
    }
  }

  async function updateNote(noteUuid, changes) {
    state.saving = true;
    try {
      const data = await Api.updateNote(noteUuid, changes);
      const updated = data?.note;
      if (updated) {
        if (updated.scope === 'session') {
          state.sessionNotes = state.sessionNotes.map((n) => (n.noteUuid === noteUuid ? updated : n));
        } else {
          for (const sheet of state.globalSheets) {
            const idx = (sheet.notes || []).findIndex((n) => n.noteUuid === noteUuid);
            if (idx >= 0) {
              if (sheet.sheetUuid === updated.sheetUuid) {
                sheet.notes.splice(idx, 1, updated);
              } else {
                sheet.notes.splice(idx, 1);
                const dst = state.globalSheets.find((s) => s.sheetUuid === updated.sheetUuid);
                if (dst) dst.notes = [updated, ...(dst.notes || [])];
              }
            }
          }
        }
      }
      return updated;
    } finally {
      state.saving = false;
    }
  }

  async function copyToGlobal(noteUuid, sheetUuid = '') {
    state.saving = true;
    try {
      const target = sheetUuid || activeSheet.value?.sheetUuid || '';
      const data = await Api.copyNoteToGlobal(noteUuid, target ? { sheetUuid: target } : {});
      const created = data?.note;
      if (created) {
        const sheet = state.globalSheets.find((s) => s.sheetUuid === created.sheetUuid) || state.globalSheets[0];
        if (sheet) {
          sheet.notes = [created, ...(sheet.notes || [])];
          state.activeSheetUuid = sheet.sheetUuid;
        } else {
          await refresh(true);
        }
      }
      return created;
    } finally {
      state.saving = false;
    }
  }

  async function removeNote(noteUuid) {
    await Api.deleteNote(noteUuid);
    state.sessionNotes = state.sessionNotes.filter((n) => n.noteUuid !== noteUuid);
    for (const sheet of state.globalSheets) {
      if (Array.isArray(sheet.notes)) {
        sheet.notes = sheet.notes.filter((n) => n.noteUuid !== noteUuid);
      }
    }
  }

  async function createSheet(title) {
    const data = await Api.createNoteSheet({ title });
    const sheet = data?.sheet;
    if (sheet) {
      state.globalSheets = [...state.globalSheets, sheet];
      state.activeSheetUuid = sheet.sheetUuid;
      state.activeScope = 'global';
    }
    return sheet;
  }

  async function renameSheet(sheetUuid, title) {
    const data = await Api.updateNoteSheet(sheetUuid, { title });
    const updated = data?.sheet;
    if (updated) {
      state.globalSheets = state.globalSheets.map((s) =>
        s.sheetUuid === sheetUuid ? { ...s, title: updated.title, updatedAtMs: updated.updatedAtMs } : s,
      );
    }
    return updated;
  }

  async function removeSheet(sheetUuid) {
    const data = await Api.deleteNoteSheet(sheetUuid);
    await refresh(true);
    if (data?.activeSheetUuid) state.activeSheetUuid = data.activeSheetUuid;
  }

  function openPanel({ scope = '', mobile = false, pin = false } = {}) {
    if (scope === 'session' && usableConversationUuid.value) state.activeScope = 'session';
    else if (scope === 'global') state.activeScope = 'global';
    if (!state.globalSheets.length) void refresh(true);
    if (mobile) {
      mobileOpen.value = true;
    } else {
      desktopOpen.value = true;
      desktopMinimized.value = false;
      if (pin) desktopPinned.value = true;
    }
  }

  function beginAnnotatedNote(payload, { mobile = false } = {}) {
    const targetScope = usableConversationUuid.value ? 'session' : 'global';
    draftQuote.value = {
      opId: String(payload?.opId || ''),
      turnUuid: String(payload?.turnUuid || ''),
      turnNumber: Number(payload?.turnNumber || 0),
      quote: String(payload?.quote || '').slice(0, 8000),
      conversationUuid: usableConversationUuid.value,
    };
    openPanel({ scope: targetScope, mobile, pin: !mobile });
  }

  function clearDraftQuote() {
    draftQuote.value = null;
  }

  const store = {
    state,
    usableConversationUuid,
    activeSheet,
    sessionCount,
    globalNoteCount,
    badgeCount,
    desktopOpen,
    desktopPinned,
    desktopMinimized,
    mobileOpen,
    draftQuote,
    refresh,
    createNote,
    updateNote,
    copyToGlobal,
    removeNote,
    createSheet,
    renameSheet,
    removeSheet,
    openPanel,
    beginAnnotatedNote,
    clearDraftQuote,
  };
  provide(USER_NOTES_KEY, store);
  return store;
}

export function useUserNotes() {
  return inject(USER_NOTES_KEY, null);
}
