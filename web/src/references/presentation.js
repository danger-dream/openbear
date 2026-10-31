import {KIND_LABELS,referenceUrl} from './codec.js';
import {policyReasonTitle} from './sizePolicy.js';
// servers.js registers the real probe; markdown rendering itself stays free of Vue/state imports.
let serverPresence = () => 'unknown';
export function setServerPresenceProbe(probe) { serverPresence = typeof probe === 'function' ? probe : () => 'unknown'; }
let serverFlag = () => '';
export function setServerFlagProbe(probe) { serverFlag = typeof probe === 'function' ? probe : () => ''; }
export const ICON_PATHS = {
  doc:'M7 3h7l4 4v14H7z M14 3v5h5 M10 12h5 M10 16h5',
  mem:'M9 5a3 3 0 0 0-5 3 4 4 0 0 0 0 8 3 3 0 0 0 5 3V5z M15 5a3 3 0 0 1 5 3 4 4 0 0 1 0 8 3 3 0 0 1-5 3V5z M7 10h2 M15 14h2',
  secret:'M14 6a5 5 0 1 1-2 8l-7 7H2v-3l7-7a5 5 0 0 1 5-5z M17 9h.01',
  chat:'M4 4h16v12H9l-5 4V4z M8 8h8 M8 12h5',
  turn:'M8 6H4v4 M4 6a8 8 0 1 1-1 9 M8 11h8 M8 15h5',
  message:'M4 5h16v13H8l-4 3V5z M8 9h8 M8 13h5',
  server:'M4 4h16v6H4z M4 14h16v6H4z M8 7h.01 M8 17h.01 M12 7h4 M12 17h4',
};
export function escapeReferenceHtml(value) {return String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
export function referenceIconHtml(kind) {return `<svg class="reference-chip__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ICON_PATHS[kind] || ICON_PATHS.doc}"></path></svg>`;}
// A server shows its region's flag when the directory knows it; otherwise the kind's icon.
export function referenceLeadHtml(ref) {const mark=ref?.kind==='server'?serverFlag(ref.id):'';return mark?`<span class="reference-chip__flag" aria-hidden="true">${escapeReferenceHtml(mark)}</span>`:referenceIconHtml(ref?.kind);}
export function referenceCandidateDetail(item) {
  let detail = KIND_LABELS[item.kind] || '';
  if (item.kind === 'mem' || item.kind === 'doc') detail = item.preview ?? '摘要待同步';
  if (item.kind === 'mem' || item.kind === 'doc') detail ||= '暂无正文';
  if (item.kind === 'secret') {
    const keys = Array.isArray(item.fieldKeys) ? item.fieldKeys.filter(key => typeof key === 'string' && key.trim()) : null;
    detail = keys ? (keys.length ? `字段：${keys.join(' · ')}` : '暂无字段') : '字段名待同步';
  }
  if (item.kind === 'server') detail = serverCandidateDetail(item);
  return detail + (item.archived ? ' · 已归档' : '');
}
// State first (it decides what can be done), then platform, group and address.
export function serverStateText(item) {
  if (item.online) return item.canExec ? '在线' : '在线 · 未开放命令';
  if (item.nodeInstalled) return item.sshLogin ? '离线 · 可走 SSH' : '离线';
  return item.sshLogin ? '仅 SSH' : '未装节点';
}
function serverCandidateDetail(item) {
  return [serverStateText(item), item.platform, item.serverGroup || '未分组', item.ip || item.meshAddress].filter(Boolean).join(' · ');
}
function serverMissing(ref) {return ref.kind==='server' && serverPresence(ref.id)==='missing';}
export function referenceScopeLabel(ref) {
  const scope = ref.mode==='mention' ? '仅提及' : ref.kind==='chat' ? (ref.scope==='recent'?`最近${ref.turns || 20}轮`:'全文') : '';
  return serverMissing(ref) ? [scope,'已不存在'].filter(Boolean).join(' · ') : scope;
}
export function referenceTitle(ref) {return `${KIND_LABELS[ref.kind] || '引用'}：${ref.label}${referenceScopeLabel(ref)?' · '+referenceScopeLabel(ref):''}${ref.mode==='mention'?' · 只提供定位，不附正文':ref.kind==='secret'?' · 发送时将向当前模型提供凭证':ref.kind==='server'?(serverMissing(ref)?' · 已删除或无权访问':' · 发送时提供该服务器的实时状态'):''}${ref.modeReason?' · '+policyReasonTitle(ref).replace('将仅提及','已仅提及'):''}`;}
export function referenceChipOpen(ref) {return `<span class="reference-chip reference-chip--${ref.kind}${serverMissing(ref)?' is-missing':''}" data-reference="${escapeReferenceHtml(referenceUrl(ref))}" data-reference-label="${escapeReferenceHtml(ref.label)}" role="button" tabindex="0" title="${escapeReferenceHtml(referenceTitle(ref))}">${referenceLeadHtml(ref)}<span class="reference-chip__label">`;}
export function referenceChipClose(ref) {return `</span>${referenceScopeLabel(ref)?`<small class="reference-chip__scope">${escapeReferenceHtml(referenceScopeLabel(ref))}</small>`:''}</span>`;}
