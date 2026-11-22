// A snapshot of what the user sees, sent with each page assistant message and returned
// by Page(action=read). Everything here works on any page; components can add precision
// through pageRegistry (subjects, selections, actions).
import { currentPageInfo, liveActions, liveSelections, liveSubjects, rendered } from './pageRegistry.js';

export const ASSISTANT_ROOT = '[data-assistant-root]';
const OVERLAY_SELECTOR = [
  '.el-overlay .el-dialog', '.el-overlay .el-drawer', '.el-overlay .el-message-box',
  '[role="dialog"]', '[aria-modal="true"]', '[data-assistant-overlay]',
].join(',');
const TITLE_SELECTOR = '.el-dialog__title, .el-drawer__title, .el-message-box__title, [data-assistant-title], header h1, header h2, h1, h2, h3';

let lastSelection = {text: '', at: 0};

function insideAssistant(node) {
  const el = node?.nodeType === 1 ? node : node?.parentElement;
  return Boolean(el?.closest?.(ASSISTANT_ROOT));
}

// A click into the assistant panel collapses the page selection; keep the last one the user made outside.
export function trackSelection() {
  const update = () => {
    const selection = document.getSelection?.();
    if (!selection) return;
    if (selection.rangeCount && insideAssistant(selection.anchorNode)) return;
    const text = String(selection.toString() || '').trim();
    if (text) lastSelection = {text, at: Date.now()};
    else if (selection.anchorNode && !insideAssistant(selection.anchorNode)) lastSelection = {text: '', at: Date.now()};
  };
  document.addEventListener('selectionchange', update);
  return () => document.removeEventListener('selectionchange', update);
}

export function clean(text) {
  return String(text || '').replace(/[ \t\f\v\u00a0]+/g, ' ').replace(/\s*\n\s*/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
}

function zIndexOf(el) {
  let z = 0;
  for (let node = el; node && node !== document.body; node = node.parentElement) {
    const value = Number.parseInt(window.getComputedStyle(node).zIndex, 10);
    if (Number.isFinite(value)) z = Math.max(z, value);
  }
  return z;
}

function overlayKind(el) {
  if (el.matches('.el-drawer, .el-drawer *') || el.closest('.el-drawer')) return 'drawer';
  if (el.closest('.el-message-box')) return 'message-box';
  if (/sheet/i.test(el.className?.toString?.() || '')) return 'sheet';
  return 'dialog';
}

function overlayTitle(el) {
  const label = el.getAttribute('aria-label');
  if (label) return clean(label);
  const by = el.getAttribute('aria-labelledby');
  const labelled = by && document.getElementById(by);
  if (labelled?.textContent) return clean(labelled.textContent);
  const heading = el.querySelector(TITLE_SELECTOR);
  return clean(heading?.textContent || '').slice(0, 120);
}

// Open dialogs, drawers and panels, topmost first; the assistant's own panel is not one of them.
export function openOverlays() {
  const seen = new Set();
  const found = [];
  let order = 0;
  for (const el of document.querySelectorAll(OVERLAY_SELECTOR)) {
    order += 1;
    if (insideAssistant(el) || el.closest('.el-popper')) continue;
    const root = el.closest('.el-dialog, .el-drawer, .el-message-box') || el;
    if (seen.has(root) || !rendered(root)) continue;
    if ([...seen].some(other => other.contains(root) || root.contains(other))) continue;
    seen.add(root);
    found.push({element: root, kind: overlayKind(root), title: overlayTitle(root), z: zIndexOf(root), order});
  }
  return found.sort((a, b) => (b.z - a.z) || (b.order - a.order));
}

function mainArea() {
  return document.querySelector('main.app-main') || document.querySelector('main') || document.body;
}

function textOf(el, limit) {
  if (!el) return '';
  const copy = clean(el.innerText || el.textContent || '');
  return copy.length > limit ? `${copy.slice(0, limit)}…` : copy;
}

function scopeOf(element, overlays) {
  const top = overlays[0]?.element;
  return top && element && top.contains(element) ? 'overlay' : 'page';
}

function depthOf(element) {
  let depth = 0;
  for (let node = element; node; node = node.parentElement) depth += 1;
  return depth;
}

// The topmost overlay's subjects first, then the innermost: a settings group before the settings page around it.
function orderedSubjects(overlays) {
  return liveSubjects()
    .map(item => ({...item, scope: scopeOf(item.element, overlays), depth: depthOf(item.element)}))
    .sort((a, b) => (a.scope === b.scope ? b.depth - a.depth : a.scope === 'overlay' ? -1 : 1));
}

function currentSelection() {
  const fromComponents = liveSelections();
  if (fromComponents.length) return {text: fromComponents[0].text, source: fromComponents[0].source};
  return lastSelection.text ? {text: lastSelection.text, source: 'page'} : null;
}

// region: 'overlay' (the topmost one) or 'page'; default the topmost overlay when one is open.
export function regionElement(region = '') {
  const overlays = openOverlays();
  if (region === 'page' || !overlays.length) return {element: mainArea(), of: 'page', title: '', overlays};
  return {element: overlays[0].element, of: 'overlay', title: overlays[0].title, overlays};
}

export function capturePageContext({visibleChars = 4000, region = ''} = {}) {
  const info = currentPageInfo();
  const target = regionElement(region);
  const overlays = target.overlays;
  const subjects = orderedSubjects(overlays).map(({element: _element, depth: _depth, ...item}) => item);
  const selection = currentSelection();
  const actions = liveActions().map(item => ({name: item.name, description: item.description, ...(item.params ? {params: item.params} : {})}));
  const page = {key: info.key || '', label: info.label || document.title || '', path: `${location.pathname}${location.search}`};
  const parts = [page.label, overlays[0]?.title, subjects[0]?.label].filter(Boolean);
  return {
    page,
    ...(overlays.length ? {overlays: overlays.map(({kind, title}) => ({kind, title}))} : {}),
    ...(subjects.length ? {subjects} : {}),
    ...(selection ? {selection} : {}),
    visible: {of: target.of, ...(target.title ? {title: target.title} : {}), text: textOf(target.element, visibleChars)},
    ...(actions.length ? {actions} : {}),
    label: [...new Set(parts)].join(' › '),
  };
}

// Where the user is, for the assistant: the page and its innermost main object, never a dialog's.
// {key, label, path, open?}: open names the object so the user can be taken back to it.
export function currentScope() {
  const info = currentPageInfo();
  const page = info.key || 'page';
  const main = orderedSubjects(openOverlays()).find(item => item.main && item.scope === 'page' && item.id !== undefined && item.id !== null && item.id !== '');
  const path = `${location.pathname}${location.search}`;
  if (!main) return {key: page, label: info.label || page, path};
  return {key: `${page}|${main.kind}:${main.id}`, label: [info.label, main.label].filter(Boolean).join(' · '), path,
    open: {kind: main.kind, id: String(main.id)}};
}

// A short summary for the panel: what the next message will carry.
export function contextSummary() {
  const info = currentPageInfo();
  const overlays = openOverlays();
  const subjects = orderedSubjects(overlays);
  const selection = currentSelection();
  return {
    page: info.label || '',
    overlay: overlays[0]?.title || (overlays.length ? '弹窗' : ''),
    subject: subjects[0]?.label || '',
    subjectCount: subjects.length,
    selection: selection ? selection.text.slice(0, 200) : '',
    selectionSource: selection?.source || '',
    actions: liveActions().map(item => item.name),
  };
}
