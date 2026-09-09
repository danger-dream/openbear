import {KIND_LABELS,referenceUrl} from './codec.js';
export const ICON_PATHS = {
  doc:'M7 3h7l4 4v14H7z M14 3v5h5 M10 12h5 M10 16h5',
  mem:'M9 5a3 3 0 0 0-5 3 4 4 0 0 0 0 8 3 3 0 0 0 5 3V5z M15 5a3 3 0 0 1 5 3 4 4 0 0 1 0 8 3 3 0 0 1-5 3V5z M7 10h2 M15 14h2',
  secret:'M14 6a5 5 0 1 1-2 8l-7 7H2v-3l7-7a5 5 0 0 1 5-5z M17 9h.01',
  chat:'M4 4h16v12H9l-5 4V4z M8 8h8 M8 12h5',
  turn:'M8 6H4v4 M4 6a8 8 0 1 1-1 9 M8 11h8 M8 15h5',
  message:'M4 5h16v13H8l-4 3V5z M8 9h8 M8 13h5',
};
export function escapeReferenceHtml(value) {return String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
export function referenceIconHtml(kind) {return `<svg class="reference-chip__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ICON_PATHS[kind] || ICON_PATHS.doc}"></path></svg>`;}
export function referenceCandidateDetail(item) {
  let detail = KIND_LABELS[item.kind] || '';
  if (item.kind === 'mem' || item.kind === 'doc') detail = item.preview ?? '摘要待同步';
  if (item.kind === 'mem' || item.kind === 'doc') detail ||= '暂无正文';
  if (item.kind === 'secret') {
    const keys = Array.isArray(item.fieldKeys) ? item.fieldKeys.filter(key => typeof key === 'string' && key.trim()) : null;
    detail = keys ? (keys.length ? `字段：${keys.join(' · ')}` : '暂无字段') : '字段名待同步';
  }
  return detail + (item.archived ? ' · 已归档' : '');
}
export function referenceScopeLabel(ref) {return ref.kind==='chat' ? (ref.scope==='recent'?`最近${ref.turns || 20}轮`:'全文') : '';}
export function referenceTitle(ref) {return `${KIND_LABELS[ref.kind] || '引用'}：${ref.label}${referenceScopeLabel(ref)?' · '+referenceScopeLabel(ref):''}${ref.kind==='secret'?' · 发送时将向当前模型提供凭证':''}`;}
export function referenceChipOpen(ref) {return `<span class="reference-chip reference-chip--${ref.kind}" data-reference="${escapeReferenceHtml(referenceUrl(ref))}" data-reference-label="${escapeReferenceHtml(ref.label)}" role="button" tabindex="0" title="${escapeReferenceHtml(referenceTitle(ref))}">${referenceIconHtml(ref.kind)}<span class="reference-chip__label">`;}
export function referenceChipClose(ref) {return `</span>${referenceScopeLabel(ref)?`<small class="reference-chip__scope">${escapeReferenceHtml(referenceScopeLabel(ref))}</small>`:''}</span>`;}
