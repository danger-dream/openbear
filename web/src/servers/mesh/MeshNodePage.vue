<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { ArrowUp, ArrowUpRight, ChevronLeft, Map as MapIcon, X } from '@lucide/vue';
import { relativeTime } from '../../instances/instanceHelpers.js';
import { bytes, flag, noAutofill, regionName } from '../serverHelpers.js';
import { MeshApi } from './meshApi.js';
import { keepPeers, mesh, meshError, meshErrorText, reinstallMesh, reinstalling, removeFromMesh, save } from './meshStore.js';
import { addressProblem, codeText, errorMessage, errorsAt, errorsUnder, FILTERINGS, firewallIssue, firewallKinds, isRelay, isSite, listens, listenWarnings, MAPPINGS,
  MISSING_TEXT, msText, nameOf, nodeAlerts, nodeState, portProblem, progressOf, PROGRESS, reachOf, recommendedMode, rowButton, ruleWords, securityStats, TRANSPORTS, transportLabel,
  uid, upgradeLabel, VERDICT_NOTES, verdictBadge, warningItems, WARNINGS_TO_FIX } from './meshHelpers.js';
import { changeRole } from './meshGraph.js';
import { directEdgeId, relayEdgeId } from './meshEdit.js';
import { usePoll } from './usePoll.js';
import MeshAddress from './MeshAddress.vue';
import MeshChips from './MeshChips.vue';
import MeshFr from './MeshFr.vue';
import MeshNodeDiag from './MeshNodeDiag.vue';
import MeshNodeLan from './MeshNodeLan.vue';
import MeshNodeProxy from './MeshNodeProxy.vue';
import MeshNodeRoutes from './MeshNodeRoutes.vue';
import MeshNodeSelect from './MeshNodeSelect.vue';
import MeshNodeName from './MeshNodeName.vue';
import MeshSideNames from './MeshSideNames.vue';
import NetAddresses from '../NetAddresses.vue';
import DdnsCard from '../../ddns/DdnsCard.vue';

// A node's page: its sections on the left, every setting in the middle (the only part that scrolls), and on
// wide screens what it is joined to, its changes not applied yet and what needs a look on the right.
const props = defineProps({id: {type: String, required: true}, section: {type: String, default: ''}, phone: Boolean, items: {type: Array, default: () => []}, shownPath: {type: String, default: ''},
  routeSeed: {type: Object, default: null}});
const emit = defineEmits(['back', 'open', 'board', 'edit', 'apply', 'open-server', 'show-path', 'lines', 'rules', 'pin-path']);
const members = computed(() => mesh.work?.members || []);
const live = computed(() => members.value.filter(x => !x.removing));
const m = computed(() => members.value.find(x => x.serverId === props.id) || null);
const index = computed(() => members.value.indexOf(m.value));
const data = computed(() => mesh.data);
const regionOf = sid => String(members.value.find(x => x.serverId === sid)?.region || props.items.find(i => i.id === sid)?.region || '').toUpperCase();
const flagOf = sid => flag(regionOf(sid));
const nm = sid => nameOf(members.value, sid);
const optionOf = (m, note = '') => ({value: m.serverId, label: m.name, region: regionOf(m.serverId), note});
const region = computed(() => regionOf(props.id));
const relay = computed(() => isRelay(m.value));
const site = computed(() => isSite(m.value));
const linux = computed(() => (m.value?.platform || 'linux') === 'linux');
const PLATFORMS = {linux: 'Linux', windows: 'Windows', darwin: 'macOS'};
const node = computed(() => data.value?.nodes?.[props.id] || null);
const fresh = computed(() => !node.value?.appliedRevision);
const online = computed(() => { const item = props.items.find(i => i.id === props.id); return item ? Boolean(item.live) : true; });
const offline = computed(() => node.value?.state === 'offline' || !online.value);
const step = computed(() => progressOf(data.value, props.id));
const state = computed(() => {
  if (m.value?.removing) return ['off', '待移除（应用后从组网卸载）'];
  if (fresh.value) return ['warn', '在草稿里，应用后连进组网'];
  if (step.value && data.value?.apply?.running && step.value.state !== 'done') return PROGRESS[step.value.state] || nodeState(data.value, props.id);
  return nodeState(data.value, props.id);
});
const roleText = computed(() => relay.value ? (m.value.front?.length ? '前置机' : site.value ? '站点中转' : '中转') : '成员');
const features = computed(() => {
  const out = [];
  if (m.value?.gateway?.subnets?.length) out.push(['lan', m.value.gateway.backup ? '备用网关' : '共享局域网']);
  if (m.value?.proxy?.exit) out.push(['exit', '出口']);
  if (m.value?.proxy?.ports?.length) out.push(['ap', `代理端口 ${m.value.proxy.ports.map(p => `:${p.port}`).join(' ')}`]);
  return out;
});
const auto = computed(() => data.value?.auto || {});
const isAuto = (obj, field) => (obj?.auto || []).includes(field);

// ---- sections, and which one is on screen ----
const SECS = [['overview', '概况'], ['role', '角色'], ['connect', '怎么连进组网'], ['lan', '共享局域网'], ['proxy', '出口和代理端口'], ['access', '访问'],
  ['net', '网络参数'], ['diag', '诊断'], ['danger', '移除']];
const body = ref(null), current = ref('overview');
function go(key) {
  current.value = key;
  void nextTick(() => {
    const el = body.value?.querySelector(`#m3-sec-${key}`);
    if (el) body.value.scrollTo({top: el.offsetTop - 8, behavior: 'smooth'});
  });
}
function spy() {
  const top = body.value?.scrollTop || 0;
  let at = 'overview';
  for (const [k] of SECS) { const el = body.value?.querySelector(`#m3-sec-${k}`); if (el && el.offsetTop - 40 <= top) at = k; }
  if (body.value && body.value.scrollTop + body.value.clientHeight >= body.value.scrollHeight - 4) at = SECS.at(-1)[0];
  current.value = at;
}
watch(() => props.id, () => { current.value = 'overview'; body.value?.scrollTo({top: 0}); });
watch(() => [props.id, props.section], () => { if (props.section) go(props.section); });
onMounted(() => { if (props.section) go(props.section); });

// ---- the node's live detail, every 5 seconds ----
const detail = ref(null), detailError = ref('');
// Another node opened meanwhile (the page is kept): a late answer for the last one is dropped, not shown or kept as this one's.
const detailPoll = usePoll(async current => {
  const id = props.id;
  try {
    const d = await MeshApi.member(id);
    if (!current() || id !== props.id) return;
    detail.value = d; detailError.value = ''; keepPeers(id, d);
  } catch (e) { if (current() && id === props.id) detailError.value = meshError(e); }
}, {interval: 5000});
watch(() => props.id, () => { detail.value = null; void detailPoll.run(); });
onMounted(() => { void detailPoll.run(); });
const status = computed(() => detail.value?.status || {});
const peers = computed(() => status.value.peers || []);
const handshakeMs = p => p?.lastHandshakeUnixNano ? Number(p.lastHandshakeUnixNano) / 1e6 : 0;
const lines = computed(() => data.value?.lines || []);
const lineOf = (kind, a, b) => lines.value.find(l => (!kind || l.kind === kind) && ((l.a === a && l.b === b) || (l.a === b && l.b === a)));
const relays = computed(() => live.value.filter(isRelay));
const riders = computed(() => relay.value ? live.value.filter(x => !isRelay(x) && x.entry?.candidates?.some(c => c.relay === props.id)) : []);
// What it uses now: a member's entry, a relay's links.
const nowEntry = computed(() => {
  const actual = peers.value.find(p => p.kind === 'entry' && p.active !== false);
  const summary = status.value.entry || node.value?.summary?.entry;
  return actual?.id || summary?.relay ? {relay: actual?.id || summary.relay, transport: actual?.transport || summary?.transport} : null;
});
const relayLines = computed(() => lines.value.filter(l => l.kind === 'relay' && (l.a === props.id || l.b === props.id)));
const connText = computed(() => {
  if (!m.value) return '';
  if (relay.value) {
    const up = relayLines.value.filter(l => l.state === 'up').length;
    return [relayLines.value.length ? `${up === relayLines.value.length ? '' : `${up}/`}${relayLines.value.length} 条线路` : '还没有线路', riders.value.length ? `${riders.value.length} 台成员经它接入` : ''].filter(Boolean).join(' · ');
  }
  const e = nowEntry.value;
  if (!e) return (m.value.entry?.candidates || []).length ? (offline.value ? '离线' : '还没连上') : '还没有设置接入的中转';
  const l = lineOf('entry', props.id, e.relay);
  return [`经 ${nm(e.relay)}`, transportLabel(e.transport || l?.transport), msText(l?.rttMs)].filter(Boolean).join(' · ');
});

// ---- overview ----
const reach = computed(() => reachOf(data.value, props.id, detail.value?.status));
const verdict = computed(() => verdictBadge(reach.value));
const observers = computed(() => (Array.isArray(reach.value?.observers) ? reach.value.observers : []).filter(o => o?.id));
const observerName = o => {
  if (o.kind !== 'relay') return o.id;
  if (members.value.some(x => x.serverId === o.id)) return nm(o.id);
  const host = String(o.id).replace(/^\[|\](?=:)|:\d+$/g, '');
  return members.value.find(x => [x.publicEndpoint, x.publicEndpointAuto].some(a => a && String(a).replace(/:\d+$/, '') === host))?.name || o.id;
};
// Inbound filtering is measured through a public relay it enters by; a member entering only by site relays on its own LAN has none.
const reachNote = computed(() => {
  const r = reach.value;
  if (r?.verdict === 'unknown' && r.filtering === 'unknown' && !relay.value && (m.value?.entry?.candidates || []).length
    && m.value.entry.candidates.every(c => isSite(relayOf(c.relay)))) return '它只经同一局域网里的站点中转接入，没有公网中转能替它测入站过滤；它连组网不靠公网地址，不影响使用';
  return VERDICT_NOTES[r?.verdict] || '';
});
const service = computed(() => {
  const n = node.value, error = n?.upgrade?.error;
  if (!n?.dataVersion && !upgradeLabel(n) && !error) return null;
  return {version: n.dataVersion || '', upgrading: upgradeLabel(n), failed: error ? `升级失败，已回退到原版本：${meshErrorText(error).replace(/，已回退$/, '')}` : ''};
});
const memory = computed(() => node.value?.summary?.memory || null);
const cpu = computed(() => Number.isFinite(status.value.resources?.cpuPercent) ? status.value.resources.cpuPercent : null);
const current2 = computed(() => {
  const n = node.value, p = progressOf(data.value, props.id);
  return n?.state === 'ready' && !n.error && !data.value?.apply?.running && !['failed', 'offline', 'pending', 'applying'].includes(p?.state);
});
const allEvents = computed(() => detail.value?.events || status.value.events || []);
const facts = computed(() => {
  if (!m.value) return [];
  const applied = data.value?.applied?.revision, mine = node.value?.appliedRevision, mem = memory.value, behind = mine && applied && mine < applied;
  const last = Math.max(0, ...peers.value.map(handshakeMs));
  return [
    {label: '状态', value: state.value[1]},
    {label: '组网地址', value: m.value.address || '应用后自动分配', mono: true},
    {label: '公网地址', value: m.value.announce === false ? '不对外宣告' : m.value.publicEndpoint || m.value.publicEndpointAuto || '—', mono: true},
    {label: '现在怎么连着', value: connText.value},
    {label: '可达性', value: verdict.value ? verdict.value[1] : '还没测出来', note: reachNote.value},
    {label: '组网程序', value: service.value?.version || (fresh.value ? '刚安装' : '—'), mono: true, note: service.value?.upgrading || service.value?.failed || '', tone: service.value?.failed ? 'bad' : ''},
    {label: '内存', value: mem?.rssBytes ? `${bytes(mem.rssBytes)}${mem.limitBytes ? ` / 告警线 ${bytes(mem.limitBytes)}` : ''}` : '—', tone: mem?.level === 'over' ? 'bad' : mem?.level === 'near' ? 'warn' : ''},
    {label: 'CPU', value: cpu.value !== null ? `${Math.round(cpu.value * 10) / 10}%` : '—'},
    {label: '平台', value: PLATFORMS[m.value.platform] || m.value.platform},
    last && {label: '最近握手', value: relativeTime(last, Date.now())},
    mine && {label: '配置版本', value: `第 ${mine} 版`, note: behind ? (current2.value ? `第 ${mine + 1 < applied ? `${mine + 1}～` : ''}${applied} 版没有改到它` : `最新是第 ${applied} 版`) : '', tone: behind && !current2.value ? 'warn' : ''},
    m.value.platform === 'linux' && firewallKinds(status.value.hostFirewall) && {label: '主机防火墙', value: firewallKinds(status.value.hostFirewall)},
    ...securityStats(status.value, allEvents.value, m.value, members.value, null),
  ].filter(f => f && f.value);
});
const problem = computed(() => meshErrorText(node.value?.error || status.value.error || (step.value?.state === 'failed' ? step.value.error : '')));
const missing = computed(() => node.value?.state === 'missing');
const reinstallError = ref('');
async function reinstall() { reinstallError.value = ''; reinstallError.value = await reinstallMesh(m.value); }
// What needs a look, each with the section that deals with it.
const attention = computed(() => {
  if (!m.value || m.value.removing) return [];
  const out = [];
  for (const e of errorsUnder(mesh.errors, `members[${index.value}]`)) {
    const p = e.path || '';
    out.push({tone: 'bad', text: errorMessage(e), sec: /entry/.test(p) ? 'connect' : /gateway|takeover/.test(p) ? 'lan' : /proxy/.test(p) ? 'proxy' : /role|relayMode/.test(p) ? 'role' : 'net'});
  }
  for (const a of nodeAlerts(data.value, m.value, members.value, detail.value?.status, mesh.work)) {
    out.push({tone: 'warn', text: a.code === 'mesh_standby_unhealthy' ? `备用入口不可用：${a.text}` : a.text, detail: codeText(a.code), sec: a.code === 'mesh_standby_unhealthy' ? 'connect' : 'overview'});
  }
  // Warnings asking for something to be done; advice as advice; a route the node keeps is told in its 共享局域网 section.
  const seen = new Set();
  for (const w of warningItems(data.value, `members[${index.value}]`)) {
    if (seen.has(w.text) || w.code === 'mesh_gateway_route_conflict') continue;
    seen.add(w.text);
    out.push({tone: WARNINGS_TO_FIX.has(w.code) ? 'warn' : 'info', text: WARNINGS_TO_FIX.has(w.code) ? w.text : `建议：${w.text}`, sec: 'net'});
  }
  const mem = memory.value, usage = mem ? `${bytes(mem.rssBytes)} / ${bytes(mem.limitBytes)}` : '';
  if (mem?.level === 'over') out.push({tone: 'bad', text: `内存超过告警线（${usage}），可能被系统重启，重启时组网会短暂断开`, sec: 'diag'});
  else if (mem?.level === 'near') out.push({tone: 'warn', text: `内存接近告警线（${usage}）`, sec: 'diag'});
  const skipped = status.value.skippedRoutes || [];
  if (skipped.length) out.push({tone: 'warn', text: `${skipped.length} 条组网路由没有加上：${skipped.join('、')}，去往这些网段的流量不经过组网`, sec: 'diag'});
  if (m.value.platform === 'darwin' && status.value.hostFirewall === 'on') out.push({tone: 'warn', text: 'macOS 防火墙已开启，可能拦截从组网进来的连接', sec: 'overview'});
  for (const issue of status.value.hostFirewallIssues || []) { const f = firewallIssue(issue); out.push({tone: 'warn', text: f.text, detail: f.detail, sec: 'overview'}); }
  return out;
});

const needs = computed(() => attention.value.filter(a => a.tone !== 'info'));

// ---- role ----
const recommended = computed(() => recommendedMode(reach.value));
const ports = computed(() => ({udp: 51999, tcp: 52000, tls: 52001, ...(mesh.work?.settings?.ports || {})}));
function setRelay(on) {
  if (on === relay.value) return;
  const moved = changeRole(mesh.work, props.id, on);
  m.value.role = on ? 'relay' : 'member';
  if (on && recommended.value) m.value.relayMode = recommended.value;
  if (!on) delete m.value.relayMode;
  if (on && (isSite(m.value) ? !listens(m.value, 'udp') : !TRANSPORTS.some(([t]) => listens(m.value, t)))) m.value.listen = {udp: 0, tcp: 0, tls: 0, ...(m.value.listen || {}), udp: ports.value.udp};
  if (!on && !m.value.entry) m.value.entry = {mode: 'failover', candidates: [], autoReturn: true};
  if (!on && listens(m.value, 'tls')) m.value.listen = {...m.value.listen, tls: 0};
  void save();
  const said = on
    ? [moved.entries.length && `${moved.entries.join('、')} 和它的直连改成了经它接入（排在最后，它们现在用的中转不变）`, moved.links.length && `它原来接入的中转改成了线路：${moved.links.join('、')}`]
    : [moved.links.length && `它的线路改成了接入的中转：${moved.links.join('、')}`, moved.directs.length && `经它接入的 ${moved.directs.join('、')} 改成了和它直连`];
  if (said.some(Boolean)) ElMessage({type: 'info', message: said.filter(Boolean).join('；'), duration: 8000, showClose: true});
}
const modeAuto = computed(() => auto.value.relayMode?.[props.id] || null);
const MODE_TEXT = {public: '公网中转', site: '站点中转'};
function setMode(mode) {
  m.value.auto = (m.value.auto || []).filter(f => f !== 'relayMode');
  m.value.relayMode = mode;
  if (mode === 'site' && !listens(m.value, 'udp')) m.value.listen = {udp: 0, tcp: 0, tls: 0, ...(m.value.listen || {}), udp: ports.value.udp};
  void save();
}
function backToAuto(field) { m.value.auto = [...new Set([...(m.value.auto || []), field])]; void save(); }
const modeErrors = computed(() => (mesh.errors || []).filter(e => e.path?.startsWith(`members[${index.value}].relayMode`)
  || (e.error === 'mesh_site_relay_needs_public_link' && (e.path === `members[${index.value}]` || e.path?.startsWith(`members[${index.value}].`)))).map(errorMessage));
const frontExits = computed(() => (m.value?.front || []).map(nm).join('、'));

// ---- how it connects: a member's entries and direct links, a relay's links and riders ----
const entryErrors = computed(() => errorsUnder(mesh.errors, `members[${index.value}].entry`).map(errorMessage));
const relayOf = sid => members.value.find(x => x.serverId === sid);
const candAuto = c => auto.value.candidates?.[`${props.id}|${c.relay}`]?.transport || null;
const isUpstreams = computed(() => (m.value?.entry?.mode || 'upstreams') === 'upstreams');
const candRows = computed(() => (m.value?.entry?.candidates || []).map((c, i) => {
  const r = relayOf(c.relay), l = lineOf('entry', props.id, c.relay), a = candAuto(c), autoOn = isAuto(c, 'transport');
  const errs = errorsUnder(mesh.errors, `members[${index.value}].entry.candidates[${i}]`).map(errorMessage);
  return {c, i, name: nm(c.relay), flag: flagOf(c.relay), inUse: nowEntry.value?.relay === c.relay, site: isSite(r),
    measured: Number.isFinite(l?.rttMs) ? `实测 ${msText(l.rttMs)}` : fresh.value ? '应用后实测' : '还没有实测',
    autoText: a && (autoOn || a.value === c.transport) ? `自动：${a.reason}` : '', changed: !autoOn && a && a.value !== c.transport ? a : null,
    errors: errs, allowed: t => !r || listens(r, t)};
}));
function setCandTransport(c, t) { c.transport = t; c.auto = (c.auto || []).filter(f => f !== 'transport'); void save(); }
function moveCand(i) { const list = m.value.entry.candidates; list.splice(i - 1, 0, list.splice(i, 1)[0]); void save(); }
function dropCand(i) { m.value.entry.candidates.splice(i, 1); void save(); }
function toManual(item) { delete item.origin; delete item.reason; delete item.since; void save(); }
function setMemberDirect(on) { m.value.direct = on; void save(); }
const addable = computed(() => relays.value.filter(r => r.serverId !== props.id && !(m.value?.entry?.candidates || []).some(c => c.relay === r.serverId)));
function addCand(sid) {
  if (!sid) return;
  const r = relayOf(sid);
  if (!m.value.entry) m.value.entry = {mode: 'upstreams', candidates: [], autoReturn: true};
  const t = isSite(r) ? 'udp' : ['udp', 'tcp', 'tls'].find(x => listens(r, x)) || 'udp';
  m.value.entry.candidates.push({relay: sid, transport: t, auto: ['transport']});
  void save();
}
// How a member uses its entry relays, one of three the user picks and can change back; the mesh never changes it by itself.
const ENTRY_MODES = [['upstreams', '同时连上', '全部同时连着，去每个目标自动挑最快的入口；自动调整和自建路线只用得上这种'],
  ['failover', '主备', '先用第一个，连不上换下一个'], ['fixed', '只用第一个', '只连第一个，排在后面的不用']];
const entryMode = computed(() => m.value?.entry?.mode || 'upstreams');
const TRANSPORT_TIPS = {udp: '最快', tcp: '更稳，UDP 被限速或丢包多时用', tls: '看起来像 HTTPS，最不容易被干扰'};
function setEntryMode(mode) {
  const entry = m.value.entry || {candidates: [], autoReturn: true};
  if (entry.mode === mode) return;
  m.value.entry = {...entry, mode, autoReturn: entry.autoReturn !== false};
  void save();
}
function setAutoReturn(on) { m.value.entry.autoReturn = on; void save(); }
const directs = computed(() => (mesh.work?.directLinks || []).map((d, idx) => ({d, idx})).filter(({d}) => d.a === props.id || d.b === props.id).map(({d, idx}) => {
  const other = d.a === props.id ? d.b : d.a, l = lineOf('direct', props.id, other);
  const errs = errorsUnder(mesh.errors, `directLinks[${idx}]`).map(errorMessage);
  return {d, idx, other, measured: Number.isFinite(l?.rttMs) ? `实测 ${msText(l.rttMs)}` : '应用后测', errors: errs};
}));
const directable = computed(() => live.value.filter(x => x.serverId !== props.id && !isRelay(x) && !directs.value.some(d => d.other === x.serverId)));
function addDirect(sid) { if (!sid) return; mesh.work.directLinks = [...(mesh.work.directLinks || []), {a: props.id, b: sid, transport: 'udp', dialer: 'a'}]; void save(); }
function setDirect(d, t) { d.transport = t; void save(); }
function dropDirect(d) { mesh.work.directLinks = mesh.work.directLinks.filter(x => x !== d); void save(); }
const linkMode = computed(() => mesh.work?.relayLinks?.mode || 'full');
const pairOf = other => (mesh.work?.relayLinks?.pairs || []).find(p => (p.a === props.id && p.b === other) || (p.b === props.id && p.a === other)) || null;
const linkRows = computed(() => {
  if (!relay.value) return [];
  const others = new Set(relayLines.value.map(l => l.a === props.id ? l.b : l.a));
  for (const p of mesh.work?.relayLinks?.pairs || []) if (p.a === props.id || p.b === props.id) others.add(p.a === props.id ? p.b : p.a);
  return [...others].filter(o => isRelay(relayOf(o))).map(o => {
    const p = pairOf(o), l = lineOf('relay', props.id, o), a = auto.value.pairs?.[[props.id, o].sort().join('|')] || null;
    const dialer = p ? (p.dialer === 'b' ? p.b : p.a) : a?.dialer?.value ? (a.dialer.value === 'b' ? [props.id, o].sort()[1] : [props.id, o].sort()[0]) : '';
    const bias = p?.bias || 0, transport = p?.transport || l?.transport || a?.transport?.value || 'udp';
    const changedT = p && !isAuto(p, 'transport') && a?.transport && a.transport.value !== p.transport ? a.transport : null;
    const pIdx = p ? (mesh.work?.relayLinks?.pairs || []).indexOf(p) : -1;
    const errs = pIdx >= 0 ? errorsUnder(mesh.errors, `relayLinks.pairs[${pIdx}]`).map(errorMessage) : [];
    return {o, p, rtt: Number.isFinite(l?.rttMs) ? `实测 ${msText(l.rttMs)}` : '还没有实测', rttValue: Number.isFinite(l?.rttMs) ? l.rttMs : 1e9, state: l?.state || '',
      dialer: !dialer ? '' : dialer === props.id ? '它主动连对方' : '对方主动连它', bias: bias > 0 ? '少走' : bias < 0 ? '优先' : '正常', transport, changedT,
      errors: errs, punched: site.value && isSite(relayOf(o)), allowed: t => listens(relayOf(dialer === props.id ? o : props.id) || {}, t), edge: relayEdgeId(mesh.work, props.id, o)};
  }).sort((x, y) => x.rttValue - y.rttValue);
});
const linkable = computed(() => relays.value.filter(r => r.serverId !== props.id && !linkRows.value.some(x => x.o === r.serverId)));
function setLinkTransport(p, t) { p.transport = t; p.auto = (p.auto || []).filter(f => f !== 'transport'); void save(); }
function dropLink(p) { mesh.work.relayLinks.pairs = mesh.work.relayLinks.pairs.filter(x => x !== p); void save(); }
function addLink(sid) {
  if (!sid) return;
  mesh.work.relayLinks.pairs = [...(mesh.work.relayLinks.pairs || []), {a: props.id, b: sid, transport: 'udp', cost: 1, dialer: 'a', bias: 0, auto: ['transport', 'dialer', 'bias']}];
  void save();
}

// ---- access: the rules it is named in, on either side ----
const covers = side => side?.all || (side?.members || []).includes(props.id) || (side?.tags || []).some(t => (m.value?.tags || []).includes(t));
const ruleRows = side => (mesh.work?.rules || []).filter(r => covers(r[side])).map(r => {
  const [, , , dst, what] = ruleWords(r, live.value);
  // The other side: what it may reach, or who may reach it.
  const other = side === 'src' ? (dst ? r.dst : null) : r.src;
  return {id: r.id, side: other, nets: side === 'src' ? 'subnets' : 'prefixes', what: what || '', note: r.note || ''};
});
const canReach = computed(() => ruleRows('src'));
const reachedBy = computed(() => ruleRows('dst'));
function newRule(side) {
  const rule = {id: uid('r'), src: {all: false, members: [], tags: [], prefixes: []}, dst: {all: false, members: [], tags: [], subnets: []}, proto: 'any', ports: [], note: ''};
  rule[side].members = [props.id];
  mesh.work.rules = [...(mesh.work.rules || []), rule];
  void save();
  emit('edit', {kind: 'rule', id: rule.id});
}

// ---- network ----
const at = field => errorsAt(mesh.errors, `members[${index.value}].${field}`);
const listenAuto = computed(() => auto.value.listen?.[props.id] || null);
const openText = listen => TRANSPORTS.filter(([t]) => Number(listen?.[t]) > 0).map(([t, label]) => `${label} ${listen[t]}`).join('、') || '都不开';
const sameOpen = (a, b) => TRANSPORTS.every(([t]) => (Number(a?.[t]) > 0) === (Number(b?.[t]) > 0));
const listenChanged = computed(() => !isAuto(m.value, 'listen') && listenAuto.value && !sameOpen(listenAuto.value.value, m.value.listen));
function setListen(t, on) {
  m.value.auto = (m.value.auto || []).filter(f => f !== 'listen');
  m.value.listen = {udp: 0, tcp: 0, tls: 0, ...(m.value.listen || {}), [t]: on ? ports.value[t] : 0};
  void save();
}
const portText = ref({}), portLocal = ref({});
watch(() => `${props.id}|${JSON.stringify(m.value?.listen || {})}`, () => { portText.value = {}; portLocal.value = {}; });
function setPort(t, value) {
  const problem = portProblem(value, m.value.listen, t);
  portLocal.value = {...portLocal.value, [t]: problem};
  if (problem) return;
  m.value.listen = {...(m.value.listen || {}), [t]: Number(value)};
  void save();
}
const portErrors = t => [...(portLocal.value[t] ? [portLocal.value[t]] : []), ...at(`listen.${t}`)];
const portWarnings = computed(() => listenWarnings(data.value, index.value));
const addressText = ref(''), addressLocal = ref('');
watch(() => `${props.id}|${m.value?.address || ''}`, () => { addressText.value = m.value?.address || ''; addressLocal.value = ''; }, {immediate: true});
function setAddress() {
  const text = addressText.value.trim();
  addressLocal.value = addressProblem(text, mesh.work?.settings, members.value, m.value);
  if (addressLocal.value || text === (m.value.address || '')) return;
  m.value.address = text;
  void save();
}
const limitText = ref('');
watch(() => `${props.id}|${m.value?.uploadLimitMbps ?? ''}`, () => { limitText.value = m.value?.uploadLimitMbps ?? ''; }, {immediate: true});
function addressAuto() { m.value.publicEndpoint = ''; delete m.value.announce; delete m.value.publicPorts; void save(); }
function setLimit() { const v = String(limitText.value).trim(); m.value.uploadLimitMbps = v === '' ? null : Number(v); void save(); }
const knownTags = computed(() => Array.isArray(data.value?.tags) ? data.value.tags
  : [...new Set(members.value.filter(x => x.serverId !== props.id).flatMap(x => x.tags || []))]);

// ---- removal ----
const busy = ref(false);
async function remove() { busy.value = true; const removed = await removeFromMesh(m.value); busy.value = false; if (removed) emit('back'); }
function restore() { m.value.removing = false; void save(); }

// ---- the right column ----
const joined = computed(() => {
  if (!m.value) return [];
  const list = relay.value
    ? linkRows.value.map(x => ({id: x.o, rtt: x.rttValue, t: x.transport, kind: 'line'}))
    : [...(m.value.entry?.candidates || []).map((c, i) => ({id: c.relay, rtt: lineOf('entry', props.id, c.relay)?.rttMs, t: c.transport, kind: i && !isUpstreams.value ? 'backup' : 'entry'})),
      ...directs.value.map(d => ({id: d.other, rtt: lineOf('direct', props.id, d.other)?.rttMs, t: d.d.transport, kind: 'direct'}))];
  return list.map((x, i) => ({...x, y: 17 + i * 24, tone: x.kind === 'backup' ? 'ride' : !Number.isFinite(x.rtt) || x.rtt >= 1e9 ? 'ride' : x.rtt < 50 ? 'good' : x.rtt < 130 ? 'mid' : 'far'}));
});
const short = name => name.length > 14 ? `${name.slice(0, 13)}…` : name;
const mapH = computed(() => Math.max(70, joined.value.length * 24 + 10));
const changes = computed(() => (data.value?.draft?.changes || []).filter(c => c.serverId === props.id));
</script>
<template>
  <section v-if="m" class="m3-node" :class="{'is-phone': phone}" :aria-label="m.name">
    <header class="m3-node-top">
      <el-button size="small" @click="emit('back')"><ChevronLeft :size="14" />返回节点列表</el-button>
      <h2><span v-if="flag(region)" class="sv-flag" aria-hidden="true">{{ flag(region) }}</span>{{ m.name }}</h2>
      <span class="m3-node-meta">
        <span class="ob-dot" :data-tone="state[0]">{{ state[1] }}</span>
        <span>{{ regionName(region) || '地区未知' }}</span><span>{{ PLATFORMS[m.platform] || m.platform }}</span>
        <span v-if="relay" class="m3-tag" data-kind="relay">{{ roleText }}</span><span v-else>成员</span>
        <span v-for="[k, l] in features" :key="k" class="m3-tag" :data-kind="k">{{ l }}</span>
      </span>
      <span class="ob-grow" />
      <el-button v-if="!fresh" size="small" @click="emit('board', id)"><MapIcon :size="13" />在画板上看</el-button>
      <el-button size="small" @click="emit('open-server', id)"><ArrowUpRight :size="13" />打开服务器页</el-button>
    </header>
    <p v-if="fresh && !m.removing" class="m3-banner"><span>这个节点在<b>草稿</b>里：组网服务已经装好，但还没连进组网。下面的设置都可以先改好，然后点「应用到网格」，它就会按这些设置连上。</span>
      <el-button size="small" type="primary" :disabled="Boolean(mesh.errors.length)" @click="emit('apply')">应用到网格</el-button></p>
    <div class="m3-node-grid">
      <nav class="m3-node-nav" aria-label="小节">
        <button v-for="[k, l] in SECS" :key="k" type="button" :aria-current="current === k ? 'true' : undefined" @click="go(k)">{{ l }}</button>
      </nav>
      <div ref="body" class="m3-node-body" @scroll.passive="spy">
        <!-- 概况 -->
        <section id="m3-sec-overview" class="m3-dsec" aria-label="概况">
          <h3>概况</h3>
          <p v-if="m.removing" class="m3-callout" data-tone="warn"><span>应用后从组网移除，节点上的组网服务会被卸载。</span><button type="button" class="ix-text-button" @click="restore">撤销移除</button></p>
          <div v-if="missing && !m.removing" class="m3-callout" data-tone="bad" role="status">
            <span>{{ problem || MISSING_TEXT }}。组网暂停给它下发配置，其他节点也连不上它。<small v-if="reinstallError" class="ob-bad">没有重新安装：{{ reinstallError }}</small></span>
            <el-button size="small" type="primary" :loading="Boolean(reinstalling[id])" :disabled="Boolean(reinstalling[id]) || !online" @click="reinstall">{{ reinstalling[id] ? '正在重新安装…' : '重新安装组网服务' }}</el-button>
          </div>
          <p v-else-if="problem" class="m3-callout" data-tone="bad"><span>{{ problem }}</span></p>
          <p v-for="(a, i) in attention" :key="i" class="m3-callout" :data-tone="a.tone"><span>{{ a.text }}<small v-if="a.detail">{{ a.detail }}</small></span>
            <button v-if="a.sec !== 'overview'" type="button" class="ix-text-button" @click="go(a.sec)">去处理</button></p>
          <dl class="m3-kvs">
            <div v-for="f in facts" :key="f.label"><dt>{{ f.label }}</dt><dd :class="[{mono: f.mono}, f.tone === 'bad' ? 'ob-bad' : f.tone === 'warn' ? 'ob-warn' : '']">{{ f.value }}<small v-if="f.note" class="m3-faint">：{{ f.note }}</small></dd></div>
          </dl>
          <details v-if="verdict" class="m3-details">
            <summary>可达性检测详情</summary>
            <p class="m3-hint">对外地址 <span class="mono">{{ reach.publicAddr || '没有测到' }}</span>{{ [reach.mapping && ` · 端口映射：${MAPPINGS[reach.mapping] || reach.mapping}`, reach.filtering && ` · 入站过滤：${FILTERINGS[reach.filtering] || reach.filtering}`].filter(Boolean).join('') }}{{ reach.checkedAt ? ` · 检测于 ${relativeTime(reach.checkedAt, Date.now())}` : '' }}。每 10 分钟测一次，网络变化时立即测。</p>
            <ul v-if="observers.length" class="m3-observers">
              <li v-for="o in observers" :key="o.id" :data-ok="o.ok || undefined"><span>{{ observerName(o) }}<span class="m3-faint">{{ o.kind === 'relay' ? ' 中转' : ' STUN' }}</span></span><span class="mono">{{ o.ok ? o.addr || '有回应' : '没有回应' }}</span></li>
            </ul>
          </details>
          <p v-if="detailError" class="m3-hint">读不到实时状态：{{ detailError }}</p>
          <MeshNodeRoutes v-if="!m.removing" :id="id" :phone="phone" :fresh="fresh" :region-of="regionOf" :seed="routeSeed" @show-path="emit('show-path', $event)" />
        </section>

        <!-- 角色 -->
        <section id="m3-sec-role" class="m3-dsec" aria-label="角色">
          <h3>角色</h3>
          <p class="m3-explain">决定这个节点在组网里的位置。</p>
          <MeshFr label="角色">
            <div class="m3-seg" role="radiogroup" aria-label="角色">
              <button type="button" role="radio" :aria-checked="!relay" @click="setRelay(false)">成员</button>
              <button type="button" role="radio" :aria-checked="relay" :disabled="!linux" :title="linux ? '' : '中转只能是 Linux'" @click="setRelay(true)">中转</button>
            </div>
            <template #hint><b>成员</b>只自己用，经入口中转连进组网；<b>中转</b>还替别人转发，和其他中转连成线路。{{ linux ? '' : `这台是 ${PLATFORMS[m.platform]}，只能当成员。` }}</template>
          </MeshFr>
          <MeshFr v-if="relay" label="中转类型" :auto="modeAuto ? {value: MODE_TEXT[modeAuto.value] || modeAuto.value, why: modeAuto.reason} : null"
            :changed="Boolean(modeAuto) && !isAuto(m, 'relayMode') && modeAuto.value !== (m.relayMode || 'public')" restorable :errors="modeErrors" @restore="backToAuto('relayMode')">
            <div class="m3-seg" role="radiogroup" aria-label="中转类型">
              <button v-for="[k, l] in Object.entries(MODE_TEXT)" :key="k" type="button" role="radio" :aria-checked="(m.relayMode || 'public') === k" @click="setMode(k)">{{ l }}</button>
            </div>
            <template #hint><b>公网中转</b>：有公网地址，别人能直接连它。<b>站点中转</b>：在家里或公司的 NAT 后面，别人连不进来，只能由它主动连公网中转，或者和别的站点中转打洞。</template>
          </MeshFr>
          <MeshFr v-if="linux" label="前置机">
            <span v-if="frontExits">是：只主动连它的出口 {{ frontExits }}</span><span v-else class="m3-faint">不是</span>
            <template #hint>前置机是专线的入口端（比如服务商给的国内专线机），只和它的出口（最多 2 台）连线，不接受别人连入，线路默认 TLS。在「服务器」页这台的资料里设置出口地址就成为前置机。
              <button type="button" class="ix-text-button" @click="emit('open-server', id)">去服务器资料设置</button></template>
          </MeshFr>
        </section>

        <!-- 怎么连进组网 -->
        <section id="m3-sec-connect" class="m3-dsec" aria-label="怎么连进组网">
          <h3>怎么连进组网</h3>
          <p class="m3-explain">{{ relay ? '中转之间的线路，以及谁经它接入。' : '经哪几台入口中转连进组网，以及和哪些成员直连。' }}</p>
          <template v-if="!relay">
            <MeshFr label="入口中转" :sub="`${candRows.length} 台`" :errors="entryErrors">
              <div class="m3-ctl">
                <div class="m3-seg" role="radiogroup" aria-label="怎么用这些入口">
                  <button v-for="[k, l] in ENTRY_MODES" :key="k" type="button" role="radio" :aria-checked="entryMode === k" @click="setEntryMode(k)">{{ l }}</button>
                </div>
                <button v-if="entryMode === 'failover'" type="button" class="m3-sw" role="switch" :aria-checked="m.entry?.autoReturn !== false" @click="setAutoReturn(m.entry.autoReturn === false)"><i></i><span>第一个恢复后切回</span></button>
              </div>
              <p class="m3-hint">{{ ENTRY_MODES.find(([k]) => k === entryMode)?.[2] }}<span v-if="entryMode === 'fixed' && candRows.length > 1" class="ob-warn">；排在后面的 {{ candRows.length - 1 }} 台现在没用上</span></p>
              <ul class="m3-nitems">
                <li v-for="r in candRows" :key="r.c.id || r.c.relay">
                  <span v-if="!isUpstreams" class="m3-no">{{ r.i + 1 }}</span>
                  <span class="m3-nitems-main"><b><MeshNodeName :id="r.c.relay" /></b>
                    <span v-if="r.c.origin === 'auto'" class="m3-tag">自动加的</span>
                    <template v-if="!isUpstreams && r.i"><span class="m3-faint"> {{ entryMode === 'fixed' ? '不用' : '备用' }}</span></template>
                    <span v-else-if="!isUpstreams && r.inUse" class="m3-tag" data-kind="lan">使用中</span>
                    <small>{{ r.measured }}<template v-if="r.c.origin === 'auto' && r.c.reason"> · <span :title="r.c.reason">{{ r.c.reason }}</span></template></small>
                    <small v-for="e in r.errors" :key="e" class="ob-bad">{{ e }}</small></span>
                  <button v-if="r.c.origin === 'auto'" type="button" class="ix-text-button" @click="toManual(r.c)">转成手动</button>
                  <span v-if="r.site" class="m3-tag">UDP 打洞</span>
                  <div v-else class="m3-seg" role="radiogroup" :aria-label="`经 ${r.name} 用什么协议`">
                    <button v-for="[t, l] in TRANSPORTS" :key="t" type="button" role="radio" :aria-checked="r.c.transport === t" :disabled="!r.allowed(t)" :title="r.allowed(t) ? TRANSPORT_TIPS[t] : '那台中转没开这个协议'" @click="setCandTransport(r.c, t)">{{ l }}</button>
                  </div>
                  <button v-if="!isUpstreams" type="button" class="ix-icon-button" :disabled="!r.i" aria-label="上移" title="上移" @click="moveCand(r.i)"><ArrowUp :size="15" /></button>
                  <button type="button" class="ix-icon-button" aria-label="移除" title="移除" @click="dropCand(r.i)"><X :size="15" /></button>
                </li>
                <li v-if="!candRows.length" class="ob-warn">还没有入口中转，它连不进组网</li>
              </ul>
              <div class="m3-ctl"><MeshNodeSelect :options="addable.map(r => optionOf(r, regionName(regionOf(r.serverId))))" placeholder="＋ 添加入口中转…" aria-label="添加入口中转" width="280px" @change="addCand" /></div>
              <button v-if="isUpstreams" type="button" class="m3-sw m3-entry-auto" role="switch" :aria-checked="m.direct !== false" aria-label="允许自动加入口" @click="setMemberDirect(m.direct === false)"><i></i><span>允许自动调整给它加更近的入口</span></button>
            </MeshFr>
            <MeshFr label="和其他成员直连" :sub="directs.length ? `${directs.length} 台` : ''">
              <ul v-if="directs.length" class="m3-nitems">
                <li v-for="x in directs" :key="x.other">
                  <span class="m3-nitems-main"><b><MeshNodeName :id="x.other" /></b>
                    <span v-if="x.d.origin === 'auto'" class="m3-tag">自动加的</span>
                    <small>{{ x.measured }}<template v-if="x.d.origin === 'auto' && x.d.reason"> · <span :title="x.d.reason">{{ x.d.reason }}</span></template></small>
                    <small v-for="e in x.errors" :key="e" class="ob-bad">{{ e }}</small></span>
                  <button v-if="x.d.origin === 'auto'" type="button" class="ix-text-button" @click="toManual(x.d)">转成手动</button>
                  <div class="m3-seg" role="radiogroup" :aria-label="`和 ${nm(x.other)} 直连用什么协议`">
                    <button v-for="[t, l] in [['udp', 'UDP 打洞'], ['tcp', 'TCP'], ['tls', 'TLS']]" :key="t" type="button" role="radio" :aria-checked="x.d.transport === t" @click="setDirect(x.d, t)">{{ l }}</button>
                  </div>
                  <button type="button" class="ix-text-button" @click="emit('edit', {kind: 'edge', id: directEdgeId(mesh.work, id, x.other)})">更多设置</button>
                  <button type="button" class="ix-icon-button" aria-label="删除直连" title="删除" @click="dropDirect(x.d)"><X :size="15" /></button>
                </li>
              </ul>
              <div class="m3-ctl"><MeshNodeSelect :options="directable.map(x => optionOf(x, regionName(regionOf(x.serverId))))" placeholder="＋ 和另一台成员直连（不经中转）…" aria-label="和另一台成员直连" width="280px" @change="addDirect" /></div>
            </MeshFr>
          </template>
          <template v-else>
            <MeshFr label="和其他中转的线路" :sub="`${linkRows.length} 条`" hint="中转之间连成线路，组网按实测延迟自动选最快的路。「更多设置」里能改谁主动连、偏好和代价。">
              <ul class="m3-nitems">
                <li v-for="x in linkRows" :key="x.o">
                  <span class="m3-nitems-main"><b><MeshNodeName :id="x.o" /></b>
                    <span v-if="x.p?.origin === 'auto'" class="m3-tag">自动</span>
                    <small>{{ x.rtt }}{{ x.dialer ? ` · ${x.dialer}` : '' }} · 偏好{{ x.bias }}<span v-if="x.state === 'down'" class="ob-bad"> · 断开</span><span v-if="x.changedT" class="ob-warn"> · 协议已改（自动是 {{ transportLabel(x.changedT.value) }}）</span><template v-if="x.p?.origin === 'auto' && x.p.reason"> · {{ x.p.reason }}</template></small>
                    <small v-for="e in x.errors" :key="e" class="ob-bad">{{ e }}</small></span>
                  <button v-if="x.p?.origin === 'auto'" type="button" class="ix-text-button" @click="toManual(x.p)">转成手动</button>
                  <span v-if="x.punched" class="m3-tag">UDP 打洞</span>
                  <div v-else-if="x.p" class="m3-seg" role="radiogroup" :aria-label="`和 ${nm(x.o)} 的线路用什么协议`">
                    <button v-for="[t, l] in TRANSPORTS" :key="t" type="button" role="radio" :aria-checked="x.p.transport === t" :disabled="!x.allowed(t)" @click="setLinkTransport(x.p, t)">{{ l }}</button>
                  </div>
                  <span v-else class="m3-tag">{{ transportLabel(x.transport) }}</span>
                  <el-button v-if="x.edge" size="small" @click="emit('edit', {kind: 'edge', id: x.edge})">更多设置</el-button>
                  <button v-if="x.p && (linkMode === 'custom' || x.p.origin === 'auto')" type="button" class="ix-icon-button" aria-label="删除线路" title="删除" @click="dropLink(x.p)"><X :size="15" /></button>
                </li>
                <li v-if="!linkRows.length" class="ob-warn">还没有线路：它和别的中转不通</li>
              </ul>
              <div v-if="linkMode === 'custom'" class="m3-ctl"><MeshNodeSelect :options="linkable.map(r => optionOf(r, regionName(regionOf(r.serverId))))" placeholder="＋ 新建到另一台中转的线路…" aria-label="新建线路" width="300px" @change="addLink" /></div>
              <p v-else class="m3-callout" data-tone="info">线路页选了「{{ linkMode === 'auto' ? '自动生成' : '所有中转两两相连' }}」，上面这些线路是自动生成的。<button type="button" class="ix-text-button" @click="emit('lines', id)">去线路页</button></p>
            </MeshFr>
            <MeshFr label="经它接入的成员" hint="要让某台成员经它接入，到那台成员的「怎么连进组网」里添加。">
              <template v-if="riders.length">
                <button v-for="r in riders" :key="r.serverId" type="button" class="m3-rel" @click="emit('open', r.serverId)">
                  <span class="ob-grow"><span v-if="flagOf(r.serverId)" class="sv-flag" aria-hidden="true">{{ flagOf(r.serverId) }}</span>{{ r.name }}</span>
                  <span class="m3-faint">{{ transportLabel(r.entry.candidates.find(c => c.relay === id)?.transport) }}</span><span class="m3-chev">›</span>
                </button>
              </template>
              <span v-else class="m3-faint">没有成员经它接入</span>
            </MeshFr>
          </template>
        </section>

        <MeshNodeLan :member="m" :index="index" :items="items" @edit="emit('edit', $event)" @open="emit('open', $event)" @rules="emit('rules')" />
        <MeshNodeProxy :member="m" :index="index" :phone="phone" @group="g => emit('edit', {kind: 'group', id: g})" />

        <!-- 访问 -->
        <section id="m3-sec-access" class="m3-dsec" aria-label="访问">
          <h3>访问</h3>
          <p class="m3-explain">组网默认谁都不通，靠访问规则放行。这里列出和它有关的规则，点开就能改。</p>
          <MeshFr label="它能访问">
            <div v-for="r in canReach" :key="r.id" class="m3-rel" v-bind="rowButton(() => emit('edit', {kind: 'rule', id: r.id}))"><span class="ob-grow"><MeshSideNames v-if="r.side" :side="r.side" :nets="r.nets" :members="live" /><template v-else>它们之间互相访问</template> · {{ r.what }}<small v-if="r.note" class="m3-faint m3-block">{{ r.note }}</small></span><span class="m3-chev">›</span></div>
            <span v-if="!canReach.length" class="ob-warn">什么都访问不了</span>
            <p class="m3-hint"><button type="button" class="ix-text-button" @click="newRule('src')">＋ 允许它访问…</button></p>
          </MeshFr>
          <MeshFr label="谁能访问它">
            <div v-for="r in reachedBy" :key="r.id" class="m3-rel" v-bind="rowButton(() => emit('edit', {kind: 'rule', id: r.id}))"><span class="ob-grow"><MeshSideNames v-if="r.side" :side="r.side" :nets="r.nets" :members="live" /><template v-else>它们之间互相访问</template> · {{ r.what }}<small v-if="r.note" class="m3-faint m3-block">{{ r.note }}</small></span><span class="m3-chev">›</span></div>
            <span v-if="!reachedBy.length" class="m3-faint">谁都访问不了它</span>
            <p class="m3-hint"><button type="button" class="ix-text-button" @click="newRule('dst')">＋ 允许别人访问它…</button></p>
          </MeshFr>
        </section>

        <!-- 网络参数 -->
        <section id="m3-sec-net" class="m3-dsec" aria-label="网络参数">
          <h3>网络参数</h3>
          <p class="m3-explain">一般不用改。每一项都显示自动值和原因，改过的可以恢复自动。</p>
          <MeshFr label="监听端口" sub="别人连它用" :auto="listenAuto ? {value: openText(listenAuto.value), why: listenAuto.reason} : null" :changed="Boolean(listenChanged)" restorable @restore="backToAuto('listen')">
            <div v-for="[t, l] in TRANSPORTS" :key="t" class="m3-ctl m3-listen">
              <button type="button" class="m3-sw" role="switch" :aria-checked="listens(m, t)" :aria-label="`监听 ${l}`" :disabled="t === 'tls' && !relay" @click="setListen(t, !listens(m, t))"><i></i><span class="m3-sw-label">{{ l }}</span></button>
              <input v-if="listens(m, t)" v-bind="noAutofill" class="m3-input m3-short mono" inputmode="numeric" maxlength="5" :value="portText[t] ?? m.listen[t]" :aria-label="`${l} 端口`"
                @input="portText = {...portText, [t]: $event.target.value}" @change="setPort(t, $event.target.value)" />
              <span v-else class="m3-faint">不监听</span>
              <small v-for="e in portErrors(t)" :key="e" class="ob-bad">{{ e }}</small>
            </div>
            <small v-for="w in portWarnings" :key="w.text || w" class="ob-warn m3-block">{{ w.text || w }}</small>
            <template #hint>别的节点主动连它时用的协议和端口。只有被别人连的节点才需要开；成员开 UDP 是为了打洞直连。{{ relay ? '' : '只有中转可以开 TLS。' }}</template>
          </MeshFr>
          <MeshFr label="对外地址" sub="别人连它用" :auto="{value: `公网 IPv4 ${m.publicEndpointAuto || ''}`, why: '节点上报的公网 IP，变了会跟着变'}" :changed="Boolean(m.publicEndpoint) || m.announce === false" restorable @restore="addressAuto">
            <MeshAddress :member="m" :item="items.find(i => i.id === id)" :online="online" :site="site" :errors="at('publicEndpoint')" name="m3-node-address" @change="save" />
          </MeshFr>
          <MeshFr label="组网地址" :hint="`它在组网里的 IP，要在地址段 ${mesh.work?.settings?.prefix || ''} 里。`" :errors="addressLocal ? [addressLocal] : at('address')">
            <input v-model="addressText" v-bind="noAutofill" class="m3-input m3-mid mono" placeholder="应用后自动分配" aria-label="组网地址" @change="setAddress" />
          </MeshFr>
          <MeshFr label="发送速率限制" :auto="{value: '不限', why: '不限速'}" :changed="m.uploadLimitMbps != null" restorable @restore="limitText = ''; setLimit()"
            hint="限制它往组网里发的速度。上行带宽小的节点（比如家里宽带）建议设成上行带宽的九成，跑满时其他连接的延迟不会暴涨。">
            <div class="m3-ctl"><input v-model="limitText" v-bind="noAutofill" class="m3-input m3-short" type="number" min="1" step="any" placeholder="不限" aria-label="发送速率" @change="setLimit" /><span class="m3-faint">Mbit/s</span></div>
          </MeshFr>
          <MeshFr label="标签" hint="访问规则里可以按标签选一组节点，比如「cn」「office」。">
            <MeshChips v-model="m.tags" label="添加标签" placeholder="＋ 标签" :suggestions="knownTags.map(value => ({value}))" @change="save" />
          </MeshFr>
          <MeshFr label="网卡和动态域名">
            <details class="m3-details"><summary>展开查看这台的网卡地址和绑定的动态域名</summary>
              <NetAddresses :server-id="id" :online="online" title="本机地址" :limit="6" />
              <DdnsCard :server-id="id" active panel />
            </details>
          </MeshFr>
        </section>

        <MeshNodeDiag :member="m" :items="items" :detail="detail" :detail-error="detailError" :shown-path="shownPath" @show-path="emit('show-path', $event)" @pin-path="emit('pin-path', $event)" />

        <!-- 移除 -->
        <section id="m3-sec-danger" class="m3-dsec" aria-label="移除">
          <h3>移除</h3>
          <MeshFr label="重新安装组网服务" hint="组网服务坏了或被删了时用，会生成新密钥并通知其他节点。">
            <el-button size="small" :loading="Boolean(reinstalling[id])" :disabled="Boolean(reinstalling[id]) || !online || fresh" @click="reinstall">重新安装</el-button>
            <small v-if="reinstallError" class="ob-bad m3-block">没有重新安装：{{ reinstallError }}</small>
          </MeshFr>
          <MeshFr label="从组网移除" hint="移除也进草稿，应用后才会从这个节点卸载组网服务；它负责的线路、接入、规则一并去掉。">
            <el-button v-if="m.removing" size="small" @click="restore">撤销移除</el-button>
            <el-button v-else size="small" type="danger" plain :disabled="busy" @click="remove">从组网移除…</el-button>
          </MeshFr>
        </section>
      </div>
      <aside v-if="!phone" class="m3-node-side" aria-label="相关">
        <section class="m3-box">
          <h4>直接相连的{{ relay ? '中转' : '节点' }}<span class="ob-grow" /><span class="m3-faint">点名字打开</span></h4>
          <p v-if="!joined.length" class="m3-faint">还没有和别的节点连着。</p>
          <svg v-else class="m3-relmap" :viewBox="`0 0 272 ${mapH}`" role="img" :aria-label="`和 ${m.name} 直接相连的节点`">
            <g v-for="n in joined" :key="n.id + n.kind">
              <path :class="n.tone" :d="`M28,${mapH / 2} C58,${mapH / 2} 52,${n.y} 80,${n.y}`" fill="none" />
              <g class="m3-relmap-node" role="button" tabindex="0" :aria-label="`打开 ${nm(n.id)}`" @click="emit('open', n.id)" @keydown.enter="emit('open', n.id)">
                <circle class="n" cx="84" :cy="n.y" r="4.5" />
                <text x="93" :y="n.y + 4">{{ flagOf(n.id) }} {{ short(nm(n.id)) }}<title>{{ nm(n.id) }}</title><tspan class="ms" dx="6">{{ transportLabel(n.t) }} {{ Number.isFinite(n.rtt) && n.rtt < 1e9 ? msText(n.rtt) : '' }}{{ n.kind === 'backup' ? ' · 备用' : n.kind === 'direct' ? ' · 直连' : '' }}</tspan></text>
              </g>
            </g>
            <circle class="c" cx="22" :cy="mapH / 2" r="9" />
            <text class="ms" x="22" :y="mapH / 2 + 22" text-anchor="middle">本机</text>
          </svg>
          <p v-if="riders.length" class="m3-hint">另有 {{ riders.length }} 台成员经它接入：<template v-for="(r, i) in riders.slice(0, 6)" :key="r.serverId">{{ i ? '、' : '' }}<MeshNodeName :id="r.serverId" /></template>{{ riders.length > 6 ? ' 等' : '' }}</p>
          <div v-if="!fresh" class="m3-ctl m3-box-ctl"><el-button size="small" @click="emit('board', id)">在画板上看</el-button><el-button size="small" @click="emit('lines', relay ? id : '')">{{ relay ? '只看它的线路' : '线路页' }}</el-button></div>
        </section>
        <section class="m3-box">
          <h4>这台的未应用改动<span class="m3-tag" :data-kind="changes.length || fresh ? 'warn' : undefined">{{ changes.length }}</span></h4>
          <ul v-if="changes.length || fresh" class="m3-plain">
            <li v-if="fresh && !changes.length">新加入，还没有应用到网格</li>
            <li v-for="(c, i) in changes" :key="i">{{ c.text }}</li>
          </ul>
          <p v-else class="m3-faint">没有。改动会先进草稿，点顶部「应用到网格」才发到节点上。</p>
          <div v-if="changes.length || fresh" class="m3-ctl m3-box-ctl"><el-button size="small" type="primary" :disabled="Boolean(mesh.errors.length) || mesh.saving" @click="emit('apply')">应用到网格</el-button></div>
        </section>
        <section v-if="needs.length || problem" class="m3-box">
          <h4>需要注意</h4>
          <p v-if="problem" class="m3-hint ob-bad">{{ problem }}</p>
          <p v-for="(a, i) in needs" :key="i" class="m3-hint" :class="a.tone === 'bad' ? 'ob-bad' : ''">{{ a.text }} <button type="button" class="ix-text-button" @click="go(a.sec)">去处理</button></p>
        </section>
      </aside>
    </div>
  </section>
  <section v-else class="m3-node">
    <p class="instance-muted">这个节点已经不在组网里了。<button type="button" class="ix-text-button" @click="emit('back')">回到节点列表</button></p>
  </section>
</template>
