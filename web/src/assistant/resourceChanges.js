// A tool changed an object a page may be showing (memory, credential, document, note,
// setting, template, model): the server announces it on the global channel and the page
// reloads it and highlights it where it is listed.
import { nextTick, onBeforeUnmount, onMounted } from 'vue';
import { flash } from './pageActions.js';

export function onResourceChanged(kinds, handler) {
  const wanted = new Set(Array.isArray(kinds) ? kinds : [kinds]);
  const listener = (event) => {
    const detail = event?.detail || {};
    if (wanted.has(detail.kind)) handler(detail);
  };
  window.addEventListener('openbear:resource-changed', listener);
  return () => window.removeEventListener('openbear:resource-changed', listener);
}

// In a component: reload(detail) refreshes the data; rows carrying data-ob-key="<kind>:<id>"
// (or any of keys(detail)) are highlighted once the refreshed list has rendered.
export function useResourceChanges(kinds, reload, {keys = null} = {}) {
  let stop = null;
  onMounted(() => {
    stop = onResourceChanged(kinds, async (detail) => {
      try { await reload(detail); } catch { return; }
      await nextTick();
      const wanted = keys ? keys(detail) : [`${detail.kind}:${detail.id}`, `${detail.kind}:${detail.itemId || ''}`];
      for (const key of wanted.filter(Boolean)) {
        document.querySelectorAll(`[data-ob-key="${CSS.escape(String(key))}"]`).forEach(el => flash(el));
      }
    });
  });
  onBeforeUnmount(() => stop?.());
}
