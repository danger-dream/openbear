import { reactive, readonly } from 'vue';

const state = reactive({
  known: false,
  enabled: true,
  selectionPopover: true,
});

export const notesSettings = readonly(state);

export function applyNotesSettings(values) {
  state.enabled = values?.['ui.notesEnabled'] !== false;
  state.selectionPopover = values?.['ui.notesSelectionPopover'] !== false;
  state.known = true;
}

export function setNotesSelectionPopover(enabled) {
  state.selectionPopover = Boolean(enabled);
}

export function notesSelectionPopoverEnabled() {
  return Boolean(state.enabled && state.selectionPopover);
}

export function resetNotesSettingsForTest() {
  Object.assign(state, { known: false, enabled: true, selectionPopover: true });
}
