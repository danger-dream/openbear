import { reactive, readonly } from 'vue';

const state = reactive({ known: false, enabled: false, contextChars: 4000 });

export const assistantSettings = readonly(state);

// Shown only when the controller reports the switch on: one without the page assistant
// sends no such setting and has no backend for it.
export function applyAssistantSettings(values) {
  state.enabled = values?.['assistant.enabled'] === true;
  const chars = Number(values?.['assistant.contextChars']);
  state.contextChars = Number.isFinite(chars) && chars > 0 ? chars : 4000;
  state.known = true;
}

export function resetAssistantSettingsForTest() {
  Object.assign(state, { known: false, enabled: false, contextChars: 4000 });
}
