import { transportLabel } from './meshHelpers.js';

// Line rules as the controller reads them (app/servers/mesh_auto.py): which links or entries, between
// which two sides, and what they set. Used to say what a rule reaches while it is being edited.
export const RULE_APPLIES = [['lines', '中转线路'], ['entries', '成员接入'], ['both', '两者']];
export const SIDE_KINDS = [['any', '任意节点'], ['region', '地区'], ['tag', '标签'], ['machines', '节点']];
export const BIAS_LEVELS = [['prefer', '优先', -30], ['normal', '正常', 0], ['avoid', '少走', 100]];
export const biasLevel = v => v < 0 ? 'prefer' : v > 0 ? 'avoid' : 'normal';
export const blankRule = () => ({id: '', name: '新规则', on: true, applies: 'lines', a: {kind: 'any'}, b: {kind: 'any'}, order: ['udp', 'tcp', 'tls'], bias: null, dialer: null});
export const blankLimit = () => ({id: '', name: '新限制', on: true, a: {kind: 'any'}, b: {kind: 'any'}, relayEnd: true, transports: null, via: null, manualOnly: false});

export function sideMatches(sel, m, regionOf) {
  const kind = sel?.kind || 'any';
  if (kind === 'any') return true;
  if (kind === 'region') {
    const region = regionOf(m.serverId), inside = Boolean(region) && (sel.regions || []).includes(region);
    // Not these regions: a machine whose region is unknown is not known to be outside them.
    return sel.not ? Boolean(region) && !inside : inside;
  }
  if (kind === 'tag') return (m.tags || []).some(t => (sel.tags || []).includes(t));
  return (sel.ids || []).includes(m.serverId);
}
// Whether rule covers x and y (either way round) for kind 'lines' or 'entries'.
export const covers = (rule, x, y, kind, regionOf) => rule.on !== false && [kind, 'both'].includes(rule.applies)
  && ((sideMatches(rule.a, x, regionOf) && sideMatches(rule.b, y, regionOf)) || (sideMatches(rule.a, y, regionOf) && sideMatches(rule.b, x, regionOf)));
const acts = rule => ['order', 'bias', 'dialer'].some(f => rule[f] != null);
// The relay pairs and entry candidates a rule decides something for, the first enabled rule winning per field.
export function reach(rules, rule, pairs, members, regionOf) {
  const byId = new Map(members.map(m => [m.serverId, m])), at = rules.indexOf(rule);
  const first = (x, y, kind) => ['order', 'bias', 'dialer'].some(f => rule[f] != null && !rules.slice(0, at).some(r => r[f] != null && covers(r, x, y, kind, regionOf)) && covers(rule, x, y, kind, regionOf));
  const lines = acts(rule) ? pairs.filter(p => byId.has(p.a) && byId.has(p.b) && first(byId.get(p.a), byId.get(p.b), 'lines')) : [];
  let entries = 0;
  if (rule.order != null) for (const m of members) for (const c of m.entry?.candidates || []) if (byId.has(c.relay) && first(m, byId.get(c.relay), 'entries')) entries++;
  return {lines, entries};
}
export function sideText(sel, name, regionText) {
  const kind = sel?.kind || 'any';
  if (kind === 'any') return '任意节点';
  if (kind === 'region') return (sel.regions || []).length ? `地区${sel.not ? '不是' : '是'}${sel.regions.map(regionText).join('、')}` : '地区（还没选）';
  if (kind === 'tag') return (sel.tags || []).length ? `标签 ${sel.tags.join('、')}` : '标签（还没选）';
  return (sel.ids || []).length ? sel.ids.map(name).join('、') : '节点（还没选）';
}
export function ruleSentence(rule, name, regionText) {
  const out = [];
  if (rule.order?.length) out.push(`协议按 ${rule.order.map(transportLabel).join(' → ')} 的顺序，用对方开着的第一个`);
  if (rule.bias != null) out.push(`选路时${rule.bias > 0 ? `少走（+${rule.bias}ms）` : rule.bias < 0 ? `优先（${rule.bias}ms）` : '正常'}`);
  if (rule.dialer) out.push(`${rule.dialer === 'a' ? '「一端」' : '「另一端」'}主动连，不接受对方连入`);
  const use = Object.fromEntries(RULE_APPLIES)[rule.applies] || '';
  return `${use === '两者' ? '中转线路和成员接入' : use}里，一端是 ${sideText(rule.a, name, regionText)}、另一端是 ${sideText(rule.b, name, regionText)} 时：${out.join('；') || '（还没选要做什么）'}`;
}

export const limitCrosses = (limit, x, y, regionOf) => limit.on !== false
  && ((sideMatches(limit.a, x, regionOf) && sideMatches(limit.b, y, regionOf)) || (sideMatches(limit.a, y, regionOf) && sideMatches(limit.b, x, regionOf)));

export function limitReach(limit, pairs, directs, members, regionOf) {
  const byId = new Map(members.map(m => [m.serverId, m]));
  const cross = (a, b) => byId.has(a) && byId.has(b) && limitCrosses(limit, byId.get(a), byId.get(b), regionOf);
  const lines = (pairs || []).filter(p => cross(p.a, p.b));
  const dir = (directs || []).filter(d => cross(d.a, d.b));
  let entries = 0;
  for (const m of members) for (const c of m.entry?.candidates || []) if (cross(m.serverId, c.relay)) entries++;
  return {lines, directs: dir, entries, total: lines.length + dir.length + entries};
}

export function limitSentence(limit, name, regionText) {
  const out = [];
  if (limit.relayEnd) out.push('至少一端是中转');
  if (limit.transports?.length) out.push(`只允许 ${limit.transports.map(transportLabel).join('、')}`);
  if (limit.via?.length) out.push(`只能经过 ${limit.via.map(name).join('、')}`);
  else if (limit.via) out.push('只能经过指定节点（还没选）');
  if (limit.manualOnly) out.push('只允许手动建立');
  return `一端是 ${sideText(limit.a, name, regionText)}、另一端是 ${sideText(limit.b, name, regionText)} 时：${out.join('；') || '（还没选跨界条件）'}`;
}

export function hmText(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return '';
  const d = new Date(n);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// Text and tone for a route / path query result's `fixed` field: the own route its source takes, else
// the first of the pair's while none is up.
export function fixedRouteInfo(fixed) {
  if (!fixed) return null;
  const back = fixed.back ? '（回程）' : '', rank = fixed.rank ? `第 ${fixed.rank} 条` : '';
  if (fixed.state === 'pinned') return {tone: 'ok', text: `按自建路线${rank}${back}`};
  if (fixed.state === 'fallback') return {tone: 'warn', text: `自建路线都不通，正走自动选路${back}`};
  if (fixed.state === 'waiting') {
    const t = hmText(fixed.until);
    return {tone: 'warn', text: `自建路线刚恢复，${t ? `${t} 起` : '稳定后'}切回${back}`};
  }
  if (fixed.state === 'starting') return {tone: 'info', text: `自建路线正在连接${back}`};
  return {tone: 'off', text: `自建路线${rank}${back}`};
}

// Whether a route/path result can become an own route: not on one now, not stale or broken, and 1 to 8
// relays in between.
export function canPinRoute(res, isRelayId = () => true) {
  if (!res || res.fixed?.state === 'pinned' || res.stale) return false;
  const hops = Array.isArray(res.hops) ? res.hops : [];
  if (hops.length < 3) return false;
  const mids = hops.slice(1, -1).map(h => typeof h === 'string' ? h : h?.node).filter(Boolean);
  return mids.length >= 1 && mids.length <= 8 && new Set(mids).size === mids.length && mids.every(id => isRelayId(id));
}
