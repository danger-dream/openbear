// What the page assistant can learn from a component beyond the visible text: which
// objects it shows (subjects), text the user selected inside it, and page actions it
// lets the assistant run. Registrations follow the component: they end when it unmounts
// or a kept-alive page is hidden, and they only count while their element is rendered.
import { getCurrentInstance, onActivated, onBeforeUnmount, onDeactivated, onMounted, unref } from 'vue';

const REFERENCE_KINDS = new Set(['mem', 'doc', 'note', 'secret', 'chat', 'server']);
const subjects = new Map();
const selections = new Map();
const actions = new Map();
let pageInfo = () => ({});
let seq = 0;

export function setPageInfo(provider) { pageInfo = typeof provider === 'function' ? provider : () => ({}); }
export function currentPageInfo() { try { return pageInfo() || {}; } catch { return {}; } }

export function referenceUrl(kind, id) {
  return REFERENCE_KINDS.has(kind) && id !== undefined && id !== null && id !== ''
    ? `openbear://ref/${kind}/${encodeURIComponent(String(id))}` : '';
}

function elementOf(target, instance) {
  const value = unref(target);
  const el = value?.$el || value || instance?.proxy?.$el;
  return el?.nodeType === 1 ? el : el?.parentElement || null;
}

export function rendered(el) {
  if (!el?.isConnected) return false;
  if (el.closest?.('[inert]')) return false;
  const rects = el.getClientRects?.();
  if (!rects || !rects.length) return false;
  const style = window.getComputedStyle(el);
  return style.visibility !== 'hidden' && style.display !== 'none';
}

function bind(registry, entry, el, when = null) {
  const instance = getCurrentInstance();
  const id = `${++seq}`;
  const add = () => registry.set(id, {...entry, when, el: () => elementOf(el, instance)});
  const remove = () => registry.delete(id);
  if (instance) {
    onMounted(add);
    onActivated(add);
    onDeactivated(remove);
    onBeforeUnmount(remove);
  } else add();
  return remove;
}

// get() returns {kind, id, label, details?, role?, main?}, a list of them, or nothing.
// Reference kinds (server, mem, doc, note, secret, chat) are sent as openbear://ref links
// the backend can resolve; other kinds (template, setting, model, process…) as plain facts.
// main: the object the page is about (a server's detail, the note being edited). The page
// assistant keeps a conversation per page and main object; without one, per page.
export function usePageSubject(get, {el = null} = {}) {
  return bind(subjects, {get}, el);
}

// get() returns the user's current selection inside this component, e.g. a terminal's.
// when() limits a selection or action to the moment it applies, e.g. the active one of several terminal panes.
export function usePageSelection(get, {el = null, source = 'page', when = null} = {}) {
  return bind(selections, {get, source}, el, when);
}

// run(args) performs the action and returns a JSON-able result. Actions must not submit,
// delete or confirm on the user's behalf.
export function usePageAction(name, {description = '', params = null, run}, {el = null, when = null} = {}) {
  return bind(actions, {name, description, params, run}, el, when);
}

function live(registry) {
  const out = [];
  for (const entry of registry.values()) {
    if (entry.when && !entry.when()) continue;
    const el = entry.el();
    if (el && !rendered(el)) continue;
    out.push({...entry, element: el});
  }
  return out;
}

export function liveSubjects() {
  const out = [];
  for (const entry of live(subjects)) {
    let value;
    try { value = entry.get(); } catch { value = null; }
    for (const item of (Array.isArray(value) ? value : [value]).filter(Boolean)) {
      if (!item.kind) continue;
      const ref = referenceUrl(item.kind, item.id);
      out.push({...item, ...(ref ? {ref} : {}), element: entry.element});
    }
  }
  return out;
}

export function liveSelections() {
  const out = [];
  for (const entry of live(selections)) {
    let text = '';
    try { text = String(entry.get() || ''); } catch { text = ''; }
    if (text.trim()) out.push({text, source: entry.source, element: entry.element});
  }
  return out;
}

export function liveActions() {
  return live(actions);
}
