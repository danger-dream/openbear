import { reactive, readonly } from 'vue';

// Optional pages behind a settings switch (系统设置 → 可选功能).
export const FEATURE_SETTINGS = Object.freeze({ instances: 'features.instances', servers: 'features.servers' });
export const FEATURE_LABELS = Object.freeze({ instances: '实例', servers: '主机' });

const state = reactive({ known: false, instances: true, servers: true });
export const featureToggles = readonly(state);

export function isFeaturePage(key) {
  return Object.prototype.hasOwnProperty.call(FEATURE_SETTINGS, key);
}

// Only an explicit false turns a page off: an older controller sends no such
// setting and a not-yet-decided one sends null, and both mean "on".
export function featureEnabledIn(values, key) {
  return values?.[FEATURE_SETTINGS[key]] !== false;
}

export function applyFeatureSettings(values) {
  for (const key of Object.keys(FEATURE_SETTINGS)) state[key] = featureEnabledIn(values, key);
  state.known = true;
}

// Without readable settings keep every page reachable rather than hide one by mistake.
export function featureSettingsUnavailable() {
  if (!state.known) applyFeatureSettings(null);
}

export function featurePageListed(key) {
  return !isFeaturePage(key) || (state.known && state[key]);
}

export function featurePageState(key) {
  if (!isFeaturePage(key)) return 'on';
  if (!state.known) return 'pending';
  return state[key] ? 'on' : 'off';
}

export function resetFeatureTogglesForTest() {
  Object.assign(state, { known: false, instances: true, servers: true });
}
