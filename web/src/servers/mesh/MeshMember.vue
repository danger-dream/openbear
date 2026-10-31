<script setup>
import { computed, inject, onMounted, ref, watch } from 'vue';
import { relativeTime, timeLabel } from '../../instances/instanceHelpers.js';
import { bytes, flag, noAutofill } from '../serverHelpers.js';
import { MeshApi } from './meshApi.js';
import { keepPeers, mesh, meshError, meshErrorText, reinstallMesh, reinstalling, removeFromMesh, save } from './meshStore.js';
import { addressProblem, candLine, candParts, CAND_STATES, codeText, MISSING_TEXT, directLine, entryHealth, errorMessage, errorsAt, errorsUnder, eventRows, exportsOf, exportText, FILTERINGS, firewallIssue, firewallKinds, gatewayNetProblem, isRelay, isSite, listens, listenWarnings, MAPPINGS, nameOf,
  nodeAlerts, peerKeys, portProblem, routeRows, routineEvent, nodeState, PROGRESS, progressOf, reachOf, recommendedMode, RELAY_MODES, rtt, securityStats, TRANSPORTS, transportLabel, upgradeLabel, VERDICT_NOTES, verdictBadge, warningItems } from './meshHelpers.js';
import { ArrowUpRight, Cable, CircleAlert, Globe, Route, ShieldAlert, TriangleAlert, Unplug } from '@lucide/vue';
import MeshChips from './MeshChips.vue';
import MeshEntries from './MeshEntries.vue';
import MeshField from './MeshField.vue';
import MeshHelp from './MeshHelp.vue';
import MeshTrend from './MeshTrend.vue';
import NetAddresses from '../NetAddresses.vue';
import DdnsCard from '../../ddns/DdnsCard.vue';
import NetProbe from '../NetProbe.vue';
import { usePoll } from './usePoll.js';

// The selected member in the panel. A summary that stays at the top whatever the tab (state,
// role, address, how it is connected, what needs attention), then tabs: its status, entries,
// settings, peers, events and network tests. Settings are written to the draft as soon as a field is committed.
const props = defineProps({member: {type: Object, required: true}, phone: Boolean, region: {type: String, default: ''}, tab: {type: String, default: ''}, shownPath: {type: String, default: ''},
  items: {type: Array, default: () => []}});
const emit = defineEmits(['open-server', 'removed', 'connect', 'path', 'show-path', 'select']);
const tab = ref(props.tab || 'overview'), detail = ref(null), detailError = ref(''), busy = ref(false);
watch(() => props.tab, value => { if (value) tab.value = value; });
// The test tab stays mounted once opened, so a test keeps running (and its results stay) while
// the other tabs are looked at; it stops when the panel closes or the mesh page is left.
const meshActive = inject('meshActive', computed(() => true));
const probeSeen = ref(tab.value === 'probe');
watch(tab, value => { if (value === 'probe') probeSeen.value = true; });
const online = computed(() => { const item = props.items.find(i => i.id === props.member.serverId); return item ? Boolean(item.live) : true; });
// Offline: what it reported last is shown as such, not as how it is now.

const m = computed(() => props.member);
const id = computed(() => m.value.serverId);
const members = computed(() => mesh.work?.members || []);
const index = computed(() => members.value.indexOf(m.value));
const at = field => errorsAt(mesh.errors, `members[${index.value}].${field}`);
const under = field => errorsUnder(mesh.errors, `members[${index.value}].${field}`).map(e => e.message || e.text || e.error).filter(Boolean);
const relay = computed(() => isRelay(m.value));
const linux = computed(() => (m.value.platform || 'linux') === 'linux');
const PLATFORMS = {linux: 'Linux', windows: 'Windows', darwin: 'macOS'};
const ports = computed(() => ({udp: 51999, tcp: 52000, tls: 52001, ...(mesh.work?.settings?.ports || {})}));
const node = computed(() => mesh.data?.nodes?.[id.value] || null);
const state = computed(() => nodeState(mesh.data, id.value));
const step = computed(() => progressOf(mesh.data, id.value));
const offline = computed(() => node.value?.state === 'offline' || !online.value);
// Members that enter through this relay.
const users = computed(() => members.value.filter(x => !isRelay(x) && x.entry?.candidates?.some(c => c.relay === id.value)));
// Most used first: a member's entries come before its settings; a relay has no entries.
const tabs = computed(() => [['overview', '状态'], !relay.value && ['entry', '入口', ['entry']],
  ['settings', '配置', ['role', 'relayMode', 'address', 'publicEndpoint', 'listen', 'tags', 'uploadLimitMbps', 'gateway']], ['peers', '对端'], ['events', '事件'], ['probe', '测试']].filter(Boolean)
  .map(([key, label, fields = []]) => ({key, label, bad: fields.some(f => errorsUnder(mesh.errors, `members[${index.value}].${f}`).length)})));
// A relay has no entry tab: a tab asked for that is not there opens the status.
watch(tabs, list => { if (!list.some(t => t.key === tab.value)) tab.value = 'overview'; }, {immediate: true});

const status = computed(() => detail.value?.status || {});
const peers = computed(() => status.value.peers || []);
// Newest first by time: seq starts over when the mesh service restarts, and the controller adds its
// own events with the seq of the node's last one. Then seq, then arrival; keys include the position.
const allEvents = computed(() => (detail.value?.events || status.value.events || []).map((e, i) => ({...e, key: `${e.seq ?? ''}-${e.kind || ''}-${e.at ?? ''}-${i}`, i}))
  .sort((a, b) => ((b.at ?? 0) - (a.at ?? 0)) || ((b.seq ?? 0) - (a.seq ?? 0)) || b.i - a.i));
// Routine records (a standby entry's trial getting through, relays confirming an entry) are folded away.
const showRoutine = ref(false);
const routine = computed(() => allEvents.value.filter(e => routineEvent(e, m.value, health.value)));
const routineCount = computed(() => routine.value.reduce((n, e) => n + (Number(e.repeat) > 1 ? Number(e.repeat) : 1), 0));
const events = computed(() => showRoutine.value ? allEvents.value : allEvents.value.filter(e => !routine.value.includes(e)));
const keys = computed(() => peerKeys(detail.value?.config));
// Repeats folded, runs of route changes as one row; a run opens to its changes.
const eventList = computed(() => eventRows(events.value, m.value, members.value, keys.value));
const openRuns = ref(new Set());
function toggleRun(key) { const next = new Set(openRuns.value); if (!next.delete(key)) next.add(key); openRuns.value = next; }
// A run already counts its changes in its text: only when it began.
const since = r => Number.isFinite(r.firstAt) && r.firstAt < r.at ? `最早 ${relativeTime(r.firstAt, Date.now())}` : '';
const repeatText = r => r.items ? since(r) : [`重复 ${r.repeat} 次`, since(r)].filter(Boolean).join('，');
// A peer that is a cold standby entry (this member's, or a member's to this relay): its reconnects are the trials.
function coldPeer(p) {
  if (p.kind === 'entry') return (m.value.entry?.candidates || []).some(c => c.relay === p.id && health.value[c.id]?.state === 'untested');
  if (!relay.value) return false;
  const other = members.value.find(x => x.serverId === p.id);
  const h = entryHealth(mesh.data, p.id);
  return Boolean(other?.entry?.candidates?.some(c => c.relay === id.value && h[c.id]?.state === 'untested'));
}
const handshakeMs = p => p?.lastHandshakeUnixNano ? Number(p.lastHandshakeUnixNano) / 1e6 : 0;
// A handshake within three minutes: the tunnel is alive (WireGuard renews every two while in use).
const fresh = p => handshakeMs(p) > 0 && Date.now() - handshakeMs(p) < 180000;
const PEER_KINDS = {entry: '入口', relay: '中转', direct: '直连', member: '成员'};
// Peers grouped by how they are connected, the ones in use first in each group.
const peerGroups = computed(() => {
  const kinds = [...new Set(['entry', 'relay', 'direct', 'member', ...peers.value.map(p => p.kind)])];
  return kinds.map(kind => ({kind, label: PEER_KINDS[kind] || kind || '其他', list: peers.value.filter(p => p.kind === kind)
    .map((p, i) => ({p, i})).sort((a, b) => (Number(b.p.active !== false) - Number(a.p.active !== false)) || a.i - b.i).map(x => x.p)})).filter(g => g.list.length);
});
// What the member actually uses, next to what it is configured to use.
const connection = computed(() => {
  if (relay.value) {
    const linked = peers.value.filter(p => p.kind === 'relay');
    return linked.length ? {text: `连着 ${linked.filter(p => handshakeMs(p)).length}/${linked.length} 台中转`, names: linked.map(p => nameOf(members.value, p.id)).join('、')} : null;
  }
  const actual = peers.value.find(p => p.kind === 'entry' && p.active !== false);
  const summary = status.value.entry || node.value?.summary?.entry;
  const candidates = m.value.entry?.candidates || [];
  const expected = candidates.find(c => c.id === summary?.candidate) || candidates[0];
  if (!actual && !summary?.relay) return null;
  const peer = actual?.id || summary.relay, transport = actual?.transport || summary.transport;
  const text = [nameOf(members.value, peer), transportLabel(transport), actual?.endpoint].filter(Boolean).join(' · ');
  const differ = expected && (peer !== expected.relay ? `配置的是 ${nameOf(members.value, expected.relay)}` : transport && transport !== expected.transport ? `配置的是 ${transportLabel(expected.transport)}` : '');
  const backup = expected && candidates[0] && expected.id !== candidates[0].id ? '正在用备用入口' : '';
  return {text, short: `经 ${[nameOf(members.value, peer), transportLabel(transport)].filter(Boolean).join(' · ')}`,
    note: [backup, differ ? `与配置不一致：${differ}` : ''].filter(Boolean).join('；'), tone: differ || backup ? 'warn' : ''};
});
const lastHandshake = computed(() => Math.max(0, ...peers.value.map(handshakeMs)));
// How others reach it (the detail adds mapping and observers), its entries' health and its alerts.
const reach = computed(() => reachOf(mesh.data, id.value, detail.value?.status));
const verdict = computed(() => verdictBadge(reach.value));
const observers = computed(() => (Array.isArray(reach.value?.observers) ? reach.value.observers : []).filter(o => o?.id));
// A relay observer is named by id, or (from some nodes) by its address: name it either way.
const observerName = o => {
  if (o.kind !== 'relay') return o.id;
  if (members.value.some(x => x.serverId === o.id)) return nameOf(members.value, o.id);
  const host = String(o.id).replace(/^\[|\](?=:)|:\d+$/g, '');
  return members.value.find(x => [x.publicEndpoint, x.publicEndpointAuto].some(a => a && String(a).replace(/:\d+$/, '') === host))?.name || o.id;
};
const health = computed(() => entryHealth(mesh.data, id.value, detail.value?.status));
const reported = computed(() => Object.keys(health.value).length > 0);
// Its entries as configured, with what the member reports of each (nothing yet before it does).
const candidates = computed(() => relay.value ? [] : (m.value.entry?.candidates || []).map((c, i) => ({c, i, h: health.value[c.id] || null,
  how: isSite(members.value.find(r => r.serverId === c.relay)) && c.transport === 'udp' ? '打洞' : transportLabel(c.transport), parts: candParts(health.value[c.id] || null)})));
// The members that enter through this relay, and whether it is the entry they use now.
const riders = computed(() => relay.value ? users.value.map(u => {
  const i = u.entry.candidates.findIndex(c => c.relay === id.value), h = entryHealth(mesh.data, u.serverId)[u.entry.candidates[i].id];
  return {id: u.serverId, name: u.name, order: i + 1, state: h?.state || ''};
}) : []);
const alerts = computed(() => m.value.removing ? [] : nodeAlerts(mesh.data, m.value, members.value, detail.value?.status));
const site = computed(() => isSite(m.value));
const recommended = computed(() => recommendedMode(reach.value));
// The controller sends a new revision only to machines whose configuration it changes: an older
// revision on a ready machine that this rollout did not fail or skip is its current one.
const current = computed(() => {
  const n = node.value, p = progressOf(mesh.data, id.value);
  return n?.state === 'ready' && !n.error && !mesh.data?.apply?.running && !['failed', 'offline', 'pending', 'applying'].includes(p?.state);
});
// The address the node reports (seen by the controller) against its measured exit: a proxy in the way shows here.
const exitDiffers = computed(() => {
  const exit = String(reach.value?.publicAddr || '').replace(/^\[|\](?=:)|:\d+$/g, '');
  return m.value.publicEndpoint || !m.value.publicEndpointAuto || !exit || exit === m.value.publicEndpointAuto ? '' : exit;
});
const roleText = computed(() => [relay.value ? (site.value ? '站点中转' : '中转') : '成员', m.value.gateway ? '网关' : ''].filter(Boolean).join(' · '));
// A gateway's networks and who gets each (GET /mesh exports, by the draft's rules: changes as rules are edited).
// Empty for an older controller, which says nothing about it: the networks are listed plainly then.
const gatewayNets = computed(() => (m.value.gateway?.subnets || []).map(net => ({net, ids: exportsOf(mesh.data, id.value, net)})).filter(x => x.ids)
  .map(x => ({...x, text: exportText(x.ids, members.value)})));
const plainNets = computed(() => m.value.gateway && !gatewayNets.value.length ? (m.value.gateway.subnets || []) : []);

// Where its traffic goes (GET /mesh/routes): a few rows, the rest behind a tap; a row shows that path on the board.
const ROUTE_ROWS = 4, NET_ROWS = 3;
const routes = ref(null), routesError = ref(''), routesState = ref('loading'), routesOpen = ref(false);
const routeList = computed(() => routes.value ? routeRows(routes.value, id.value, members.value, mesh.data) : null);
const routeHidden = computed(() => routeList.value ? Math.max(0, routeList.value.routes.length - ROUTE_ROWS) + Math.max(0, routeList.value.subnets.length - NET_ROWS) : 0);
const shownRoutes = computed(() => routeList.value ? (routesOpen.value ? routeList.value.routes : routeList.value.routes.slice(0, ROUTE_ROWS)) : []);
const shownNets = computed(() => routeList.value ? (routesOpen.value ? routeList.value.subnets : routeList.value.subnets.slice(0, NET_ROWS)) : []);
const routeKey = r => `${id.value}>${r.dst || r.gateway}${r.subnet ? `>${r.subnet}` : ''}`;
function showRoute(r) {
  emit('show-path', {src: id.value, dst: r.dst || r.gateway, subnet: r.subnet || '', hops: r.hops, rttMs: r.rttMs, stale: r.stale, key: routeKey(r)});
}
// Every 10 seconds while the overview tab is open; an older controller without the endpoint is not asked again.
const routePoll = usePoll(async () => {
  if (tab.value !== 'overview') return;
  try { routes.value = await MeshApi.routes(id.value); routesState.value = 'ok'; routesError.value = ''; }
  catch (e) {
    const code = e?.response?.data?.error;
    // An older controller has no such endpoint: the section stays away. A member only in the draft has no routes yet.
    if (e?.response?.status === 404 && code !== 'mesh_member_not_found') { routesState.value = 'unsupported'; return; }
    if (code === 'mesh_member_not_found') { routes.value = null; routesState.value = 'draft'; }
    else { routesError.value = meshError(e); if (!routes.value) routesState.value = 'error'; }
  }
}, {interval: () => routesState.value === 'unsupported' ? 0 : 10000});
watch(tab, value => { if (value === 'overview') void routePoll.run(); });

// The mesh service on the machine: its version, a running or queued upgrade, and the last failed one (already rolled back).
const service = computed(() => {
  const n = node.value, error = n?.upgrade?.error;
  if (!n?.dataVersion && !upgradeLabel(n) && !error) return null;
  const reason = error ? meshErrorText(error).replace(/，已回退$/, '') : '';
  return {version: n.dataVersion || '', upgrading: upgradeLabel(n), failed: error ? `升级失败，已回退到原版本：${reason}` : '', at: Number(n.upgrade?.at) || 0};
});
const memory = computed(() => node.value?.summary?.memory || null);
const cpu = computed(() => Number.isFinite(status.value.resources?.cpuPercent) ? status.value.resources.cpuPercent : null);
// The figures under the status: two to a row, a long one across.
const stats = computed(() => {
  const applied = mesh.data?.applied?.revision, mine = node.value?.appliedRevision, entry = status.value.entry || node.value?.summary?.entry, mem = memory.value;
  const behind = mine && applied && mine < applied;
  return [
    {label: '平台', value: PLATFORMS[m.value.platform] || m.value.platform},
    {label: '最近握手', value: lastHandshake.value ? relativeTime(lastHandshake.value, Date.now()) : '', title: lastHandshake.value ? timeLabel(lastHandshake.value) : ''},
    {label: '入口切换', value: !relay.value && entry ? `${entry.switches || 0} 次` : ''},
    {label: '内存', value: mem?.rssBytes ? `${bytes(mem.rssBytes)}${mem.limitBytes ? ` / ${bytes(mem.limitBytes)}` : ''}` : '', cls: mem?.level === 'over' ? 'ob-bad' : mem?.level === 'near' ? 'ob-warn' : '',
      title: mem?.limitBytes ? `上限 ${bytes(mem.limitBytes)}` : ''},
    {label: 'CPU', value: cpu.value !== null ? `${Math.round(cpu.value * 10) / 10}%` : ''},
    {label: '资源档位', value: status.value.resources?.pendingProfile ? '待生效' : '', wide: true, noteCls: 'ob-warn',
      note: status.value.resources?.pendingProfile ? '角色已生效，收发缓冲和工作线程等固定资源在组网服务下次重启或升级时加载。' : ''},
    // Linux: the firewalls the node found and lets the mesh through.
    {label: '主机防火墙', value: m.value.platform === 'linux' ? firewallKinds(status.value.hostFirewall) : ''},
    {label: '配置版本', value: mine ? `第 ${mine} 版` : '', wide: Boolean(behind),
      note: behind ? (current.value ? `第 ${mine + 1 < applied ? `${mine + 1}～` : ''}${applied} 版没有改到它` : `最新是第 ${applied} 版`) : '', noteCls: behind && !current.value ? 'ob-warn' : ''},
    service.value && {label: '组网服务', value: service.value.version || '未知', mono: true, tag: service.value.upgrading, wide: Boolean(service.value.failed), note: service.value.failed, noteCls: 'ob-bad',
      at: service.value.failed ? service.value.at : 0},
    ...securityStats(status.value, allEvents.value, m.value, members.value, keys.value),
  ].filter(s => s && s.value);
});
// Things on the machine that keep the mesh from working as configured: one line
// each, worst first, the explanation behind a tap.
const HINT_ICONS = {memory: TriangleAlert, routes: Route, firewall: ShieldAlert, mesh_standby_unhealthy: Unplug, mesh_exit_inconsistent: Globe};
const hints = computed(() => {
  const list = [], skipped = status.value.skippedRoutes || [], mem = memory.value;
  for (const a of alerts.value) {
    if (a.code === 'mesh_standby_unhealthy') {
      const dead = candidates.value.filter(x => x.h?.state === 'failing');
      list.push({key: a.code, rank: 0.5, tone: 'bad', text: `备用入口不可用：${a.text}`, detail: codeText(a.code),
        lines: dead.map(x => `${nameOf(members.value, x.c.relay)} · ${x.how}：${candLine(x.h)}`)});
    } else if (a.code === 'mesh_exit_inconsistent') {
      const seen = observers.value.filter(o => o.ok && o.addr).map(o => `${observerName(o)} 看到 ${o.addr}`);
      list.push({key: a.code, rank: 1.5, tone: 'warn', text: '出口被代理改写',
        detail: `各观测点看到的出口地址不一致${seen.length ? `（${seen.join('；')}）` : ''}，可能经过透明代理或有多条出口线路。这台机器的公网地址不可靠，不会用来打洞。`});
    } else list.push({key: a.code, rank: 1.8, tone: 'warn', text: a.text, detail: ''});
  }
  // The controller's warnings about this member: they block nothing.
  // Advice and predictions, not faults: in plain text, the first clause as the line and the rest behind a tap.
  const seen = new Set();
  for (const w of warningItems(mesh.data, `members[${index.value}]`)) {
    if (seen.has(w.text) || list.some(h => h.text === w.text)) continue;
    seen.add(w.text);
    const head = w.code === 'mesh_udp_port_shared' ? '建议：同一个 NAT 后的两台用了同一个 UDP 端口' : w.text.split(/[，。；]/)[0];
    list.push({key: `warning:${w.text}`, rank: 4, tone: 'info', text: head, detail: head === w.text ? '' : w.text});
  }
  const usage = mem ? `${bytes(mem.rssBytes)} / ${bytes(mem.limitBytes)}` : '';
  if (mem?.level === 'over') list.push({key: 'memory', rank: 0, tone: 'bad', text: `内存超过上限（${usage}）`, detail: '组网服务用的内存超过了上限，可能被系统重启，重启时网格会短暂断开。'});
  else if (mem?.level === 'near') list.push({key: 'memory', rank: 1, tone: 'warn', text: `内存接近上限（${usage}）`, detail: '组网服务用的内存接近上限，继续增长会被系统重启。'});
  if (skipped.length) list.push({key: 'routes', rank: 2, tone: 'warn', text: `${skipped.length} 条网格路由没有加上`, detail: `本机没有加上：${skipped.join('、')}。去往这些网段的流量不经过网格。`});
  if (m.value.platform === 'darwin' && status.value.hostFirewall === 'on') list.push({key: 'firewall', rank: 3, tone: 'warn', text: 'macOS 防火墙已开启',
    detail: '可能拦截从网格进来的连接。需要被访问时，在系统设置的防火墙选项里允许组网服务接收传入连接。'});
  for (const issue of status.value.hostFirewallIssues || []) list.push({key: `firewall:${issue.code}:${issue.detail || ''}`, rank: 3, tone: 'warn', ...firewallIssue(issue)});
  return list.sort((a, b) => a.rank - b.rank);
});
const problem = computed(() => meshErrorText(node.value?.error || status.value.error || (step.value?.state === 'failed' ? step.value.error : '')));
// The summary's count of what needs attention: faults and warnings, not advice.
const attention = computed(() => (problem.value ? 1 : 0) + hints.value.filter(h => h.tone !== 'info').length + errorsUnder(mesh.errors, `members[${index.value}]`).length);
const showStep = computed(() => Boolean(step.value && (mesh.data?.apply?.running || step.value.state === 'failed')));
// Its mesh service is gone: nothing reaches it until it is put back.
const missing = computed(() => node.value?.state === 'missing');
const reinstallError = ref('');
watch(missing, value => { if (!value) reinstallError.value = ''; });
async function reinstall() {
  reinstallError.value = '';
  reinstallError.value = await reinstallMesh(m.value);
}
const headTabs = ref(null);
function lookAtProblems() {
  tab.value = 'overview';
  headTabs.value?.closest('.mesh-inspector-col, .mesh-sheet-body')?.scrollTo?.({top: 0, behavior: 'smooth'});
}

// The member's live detail, every 5 seconds.
const detailPoll = usePoll(async () => {
  try { detail.value = await MeshApi.member(id.value); detailError.value = ''; keepPeers(id.value, detail.value); }
  catch (e) { detailError.value = meshError(e); }
}, {interval: 5000});

function setRelay(on) {
  m.value.role = on ? 'relay' : 'member';
  // Behind NAT (by the reachability check) it starts as a site relay.
  if (on && recommended.value) m.value.relayMode = recommended.value;
  if (!on) delete m.value.relayMode;
  if (on && (isSite(m.value) ? !listens(m.value, 'udp') : !TRANSPORTS.some(([t]) => listens(m.value, t)))) m.value.listen = {udp: 0, tcp: 0, tls: 0, ...(m.value.listen || {}), udp: ports.value.udp};
  if (!on && !m.value.entry) m.value.entry = {mode: 'fixed', candidates: [], autoReturn: true};
  if (!on && listens(m.value, 'tls')) m.value.listen = {...m.value.listen, tls: 0};
  void save();
}
// A site relay punches: it needs UDP.
function setMode(mode) {
  m.value.relayMode = mode;
  if (mode === 'site' && !listens(m.value, 'udp')) m.value.listen = {udp: 0, tcp: 0, tls: 0, ...(m.value.listen || {}), udp: ports.value.udp};
  void save();
}
// A site relay has to reach at least one public relay to exchange addresses through.
const lonelySite = computed(() => {
  if (!site.value) return false;
  const others = members.value.filter(x => x !== m.value && isRelay(x) && !isSite(x) && !x.removing);
  const links = mesh.work?.relayLinks;
  if (links?.mode !== 'custom') return !others.length;
  return !(links.pairs || []).some(p => others.some(o => (p.a === id.value && p.b === o.serverId) || (p.b === id.value && p.a === o.serverId)));
});
const modeErrors = computed(() => (mesh.errors || []).filter(e => e.path?.startsWith(`members[${index.value}].relayMode`)
  || (e.error === 'mesh_site_relay_needs_public_link' && (e.path === `members[${index.value}]` || e.path?.startsWith(`members[${index.value}].`)))).map(errorMessage));
function setListen(transport, on) {
  m.value.listen = {udp: 0, tcp: 0, tls: 0, ...(m.value.listen || {}), [transport]: on ? ports.value[transport] : 0};
  void save();
}
// A port is checked as typed and written to the draft only when it is one the controller takes.
const portText = ref({}), portLocal = ref({});
// Keyed by value: a saved draft coming back (new objects, same values) does not wipe what is being typed.
watch(() => `${id.value}|${JSON.stringify(m.value.listen || {})}`, () => { portText.value = {}; portLocal.value = {}; });
function typePort(transport, value) {
  portText.value = {...portText.value, [transport]: value};
  portLocal.value = {...portLocal.value, [transport]: String(value).trim() ? portProblem(value, m.value.listen, transport) : ''};
}
function setPort(transport, value) {
  const problem = portProblem(value, m.value.listen, transport);
  portLocal.value = {...portLocal.value, [transport]: problem};
  if (problem) return;
  m.value.listen = {...(m.value.listen || {}), [transport]: Number(value)};
  void save();
}
const portErrors = t => [...(portLocal.value[t] ? [portLocal.value[t]] : []), ...at(`listen.${t}`)];
// mesh_port_in_use by the port it is about; mesh_port_unchecked for the member as a whole.
const portWarnings = computed(() => listenWarnings(mesh.data, index.value));
// The mesh address, likewise: inside the prefix, not its first or last address, not a control address, nobody else's.
const addressText = ref(m.value.address || ''), addressLocal = ref('');
watch(() => `${id.value}|${m.value.address || ''}`, () => { addressText.value = m.value.address || ''; addressLocal.value = ''; });
const checkAddress = value => addressProblem(value, mesh.work?.settings, members.value, m.value);
function typeAddress(value) {
  addressText.value = value;
  // Said while typing once it reads as a whole address (or cannot become one).
  const text = value.trim();
  addressLocal.value = text && (/^\d+\.\d+\.\d+\.\d+$/.test(text) || /[^\d.]/.test(text)) ? checkAddress(text) : '';
}
function setAddress() {
  const text = addressText.value.trim();
  addressLocal.value = checkAddress(text);
  if (addressLocal.value || text === (m.value.address || '')) return;
  m.value.address = text;
  void save();
}
const addressErrors = computed(() => addressLocal.value ? [addressLocal.value] : at('address'));
function setLimit(value) { m.value.uploadLimitMbps = value === '' ? null : Number(value); void save(); }
function setGateway(on) { m.value.gateway = on ? {subnets: [], masquerade: true} : null; void save(); }
const netProblem = (value, {typing = false} = {}) => gatewayNetProblem(value, {settings: mesh.work?.settings, members: members.value, self: m.value, typing});
// Tags others already use (the controller's list when it sends one), offered as typed.
const knownTags = computed(() => Array.isArray(mesh.data?.tags) ? mesh.data.tags
  : [...new Set([...(mesh.data?.members || []), ...members.value].filter(x => x.serverId !== id.value).flatMap(x => x.tags || []))]);
// The LAN networks the machine is on (GET /mesh members[].subnetSuggestions, from its node's localPrefixes).
const lanNets = computed(() => {
  const listed = (mesh.data?.members || []).find(x => x.serverId === id.value)?.subnetSuggestions ?? m.value.subnetSuggestions;
  return (Array.isArray(listed) ? listed : []).filter(net => !netProblem(net)).map(value => ({value, note: '本机所在的局域网'}));
});
const MASQUERADE_HELP = '开启后，组网成员访问这个局域网时，局域网里的设备看到的来源是网关这台机器的地址，所以不用在路由器上加回程路由；'
  + '关闭后能看到真实来源，但需要在局域网路由器上把组网网段指向这台网关。';
const LISTEN_HELP = {relay: '成员按入口里选的传输方式连进来，用到哪种就开哪种；成员之间打洞直连要至少一台中转开启 UDP。',
  member: 'UDP 直连两端都要开启 UDP 监听；TCP 直连只有被主动连接的一端需要。只有中转可以开启 TLS 监听。'};

async function remove() {
  busy.value = true;
  const removed = await removeFromMesh(m.value);
  busy.value = false;
  if (removed) emit('removed');
}
function restore() { m.value.removing = false; void save(); }

onMounted(() => { void detailPoll.run(); void routePoll.run(); });
</script>
<template>
  <div class="mesh-member">
    <!-- Stays at the top while the tabs below scroll: what the member is and how it is doing. -->
    <div ref="headTabs" class="mesh-member-tabs">
      <div class="mesh-member-summary" aria-label="摘要">
        <span class="mesh-member-line">
          <span v-if="flag(region)" class="sv-flag" aria-hidden="true">{{ flag(region) }}</span>
          <span class="ob-dot" :data-tone="state[0]">{{ state[1] }}</span>
          <span class="mesh-member-role">{{ roleText }}</span>
          <span class="mono mesh-member-address" :title="m.address ? '网格地址' : ''">{{ m.address || '地址待分配' }}</span>
        </span>
        <span class="mesh-member-line">
          <span v-if="m.removing" class="pb-tag" data-tone="bad">待移除</span>
          <span v-else-if="showStep" class="pb-tag" :data-tone="PROGRESS[step.state]?.[0]">{{ PROGRESS[step.state]?.[1] || step.state }}</span>
          <template v-if="relay">
            <span v-if="connection" class="mesh-member-via" :title="connection.names">{{ connection.text }}</span>
            <span v-if="users.length" class="mesh-member-via">{{ users.length }} 台成员经它接入</span>
          </template>
          <template v-else-if="connection">
            <span class="mesh-member-via" :class="offline ? 'instance-muted' : connection.tone === 'warn' ? 'ob-warn' : ''" :title="connection.note">{{ offline ? `离线前${connection.short}` : connection.short }}</span>
            <span v-if="!offline && rtt(node?.summary)" class="mesh-member-rtt">{{ rtt(node?.summary) }}</span>
          </template>
          <span v-else-if="!(m.entry?.candidates || []).length" class="mesh-member-via ob-warn">未设置入口</span>
          <span v-if="verdict" class="pb-tag" :data-tone="verdict[0]" :title="VERDICT_NOTES[reach.verdict] || VERDICT_NOTES.unknown">{{ verdict[1] }}</span>
          <button v-if="attention" type="button" class="pb-tag mesh-member-attention" data-tone="warn" :aria-label="`${attention} 项需要注意，查看`" @click="lookAtProblems"><TriangleAlert :size="11" aria-hidden="true" />{{ attention }} 项需注意</button>
        </span>
      </div>
      <div class="sv-ranges" role="tablist" aria-label="成员详情">
        <button v-for="t in tabs" :key="t.key" type="button" role="tab" :aria-selected="tab === t.key" @click="tab = t.key">{{ t.label }}<i v-if="t.bad" class="mesh-tab-alert" aria-label="有需要修改的地方"></i></button>
      </div>
    </div>

    <section v-if="tab === 'overview'" class="mesh-pane mesh-status" aria-label="状态">
      <p v-if="m.removing" class="ix-notice mesh-problem" data-tone="warn"><span>应用后从网格移除。</span><button type="button" class="ix-text-button" @click="restore">撤销移除</button></p>
      <div v-if="missing && !m.removing" class="ix-notice mesh-problem mesh-missing" data-tone="bad" role="status">
        <span>{{ problem || MISSING_TEXT }}。组网暂停给它下发配置，其他成员也连不上它。</span>
        <span class="mesh-missing-action">
          <el-button size="small" type="primary" :loading="Boolean(reinstalling[id])" :disabled="Boolean(reinstalling[id]) || !online" @click="reinstall">{{ reinstalling[id] ? '正在重新安装…' : '重新安装组网服务' }}</el-button>
          <small v-if="!online && !reinstalling[id]" class="instance-muted">节点离线，连上后才能重新安装</small>
          <small v-else-if="reinstalling[id]" class="instance-muted">正在机器上安装，最多约 3 分钟</small>
        </span>
        <small v-if="reinstallError" class="ob-bad mesh-missing-error" role="alert">没有重新安装：{{ reinstallError }}</small>
      </div>
      <p v-else-if="problem" class="ix-notice mesh-problem" data-tone="bad"><span>{{ problem }}</span></p>
      <ul v-if="hints.length" class="mesh-hints" aria-label="提示">
        <li v-for="h in hints" :key="h.key" :data-tone="h.tone">
          <details v-if="h.detail || h.lines?.length">
            <summary><component :is="HINT_ICONS[h.key.split(':')[0]] || CircleAlert" :size="14" aria-hidden="true" /><span>{{ h.text }}</span></summary>
            <p v-if="h.detail">{{ h.detail }}</p>
            <p v-for="l in h.lines || []" :key="l" class="mesh-hint-line">{{ l }}</p>
          </details>
          <p v-else class="mesh-hint-plain"><component :is="HINT_ICONS[h.key.split(':')[0]] || CircleAlert" :size="14" aria-hidden="true" /><span>{{ h.text }}</span></p>
        </li>
      </ul>
      <div v-if="!m.removing" class="mesh-actions">
        <button type="button" class="ix-text-button" @click="emit('connect', id)"><Cable :size="13" aria-hidden="true" />连接到…</button>
        <button type="button" class="ix-text-button" @click="emit('path', id)"><Route :size="13" aria-hidden="true" />查看到…的路径</button>
        <button type="button" class="ix-text-button" @click="emit('open-server', id)"><ArrowUpRight :size="13" aria-hidden="true" />打开服务器</button>
      </div>

      <!-- How it is connected: a member's entries as they are now; a relay's links and the members entering through it. -->
      <section v-if="!relay" class="mesh-group mesh-cand-health" aria-label="入口">
        <header class="mesh-group-head"><h4>入口</h4><span class="instance-muted">{{ m.entry?.mode === 'failover' ? '自动切换' : '固定' }}{{ offline && reported ? ' · 离线前的状态' : '' }}</span><span class="ob-grow" />
          <button type="button" class="ix-text-button" @click="tab = 'entry'">{{ candidates.length ? '调整' : '添加入口' }}</button></header>
        <p v-if="!candidates.length" class="instance-muted mesh-group-empty">还没有入口：它连不进网格。</p>
        <ol v-else class="mesh-cand-list" :data-stale="offline || undefined">
          <li v-for="x in candidates" :key="x.c.id" :data-tone="x.h ? CAND_STATES[x.h.state][0] : 'off'" class="mesh-cand-open" role="button" tabindex="0"
            :aria-label="`第 ${x.i + 1} 个入口（${nameOf(members, x.c.relay) || '未选中转'} · ${x.how}）：查看这条线路和它的稳定性`"
            @click="emit('select', {kind: 'edge', id: `entry:${id}:${x.c.id}`})" @keydown.enter.prevent="emit('select', {kind: 'edge', id: `entry:${id}:${x.c.id}`})">
            <span class="mesh-cand-no">{{ x.i + 1 }}</span>
            <span class="mesh-cand-name"><strong>{{ nameOf(members, x.c.relay) || '未选中转' }}</strong><small>{{ x.how }}</small></span>
            <span class="mesh-cand-now"><span v-if="x.h" class="pb-tag" :data-tone="CAND_STATES[x.h.state][0] === 'off' ? undefined : CAND_STATES[x.h.state][0]">{{ x.parts.label }}</span>
              <small v-if="x.h && x.parts.detail">{{ x.parts.detail }}</small><small v-else-if="!x.h">{{ reported ? '没有上报' : '' }}</small></span>
          </li>
        </ol>
        <p v-if="connection" class="mesh-connection"><span class="instance-muted">{{ offline ? '离线前' : '实际' }}</span><span>{{ connection.text }}</span><small v-if="connection.note" :class="connection.tone === 'warn' ? 'ob-warn' : ''">{{ connection.note }}</small></p>
      </section>
      <section v-else class="mesh-group" aria-label="连接">
        <header class="mesh-group-head"><h4>连接</h4></header>
        <dl class="mesh-kv">
          <div><dt>中转</dt><dd>{{ connection ? `${connection.text}：${connection.names}` : '还没有连上其他中转' }}</dd></div>
          <div><dt>接入成员</dt>
            <dd v-if="riders.length" class="mesh-riders">
              <span v-for="r in riders" :key="r.id" :data-tone="r.state ? CAND_STATES[r.state][0] : undefined">{{ r.name }}<small>{{ r.state ? CAND_STATES[r.state][1] : `第 ${r.order} 个入口` }}</small></span>
            </dd>
            <dd v-else class="instance-muted">没有成员把它当入口</dd>
          </div>
        </dl>
      </section>

      <section v-if="verdict || gatewayNets.length || plainNets.length" class="mesh-group" aria-label="网络">
        <header class="mesh-group-head"><h4>网络</h4></header>
        <div v-if="verdict" class="mesh-reach">
          <p class="mesh-reach-line"><span class="pb-tag" :data-tone="verdict[0]">{{ verdict[1] }}</span>
            <span class="mono" title="真实出口：从组网的 UDP 端口向观测点查到的地址">出口 {{ reach.publicAddr || '没有测到' }}</span></p>
          <small v-if="reach.consistent === false" class="ob-warn">出口被代理改写：各观测点看到的地址不一致</small>
          <details class="mesh-observers">
            <summary>{{ [observers.length ? `观测点 ${observers.filter(o => o.ok).length}/${observers.length} 个有回应` : '', reach.checkedAt ? `检测于 ${relativeTime(reach.checkedAt, Date.now())}` : ''].filter(Boolean).join(' · ') || '详情' }}</summary>
            <p class="instance-muted">{{ VERDICT_NOTES[reach.verdict] || VERDICT_NOTES.unknown }}{{ [reach.mapping && `映射：${MAPPINGS[reach.mapping] || reach.mapping}`, reach.filtering && `过滤：${FILTERINGS[reach.filtering] || reach.filtering}`].filter(Boolean).map(t => `。${t}`).join('') }}</p>
            <ul v-if="observers.length">
              <li v-for="o in observers" :key="o.id" :data-ok="o.ok || undefined">
                <span class="mesh-observer-name">{{ observerName(o) }}<span class="pb-tag is-plain">{{ o.kind === 'relay' ? '中转' : 'STUN' }}</span></span>
                <span class="mono">{{ o.ok ? o.addr || '有回应' : '没有回应' }}</span>
              </li>
            </ul>
          </details>
        </div>
        <div v-if="gatewayNets.length" class="mesh-exports">
          <span class="mesh-exports-title">网关网段</span>
          <span v-for="g in gatewayNets" :key="g.net" class="mesh-export"><span class="mono">{{ g.net }}</span><small :class="g.ids.length ? '' : 'ob-warn'">{{ g.text }}</small></span>
        </div>
        <p v-else-if="plainNets.length" class="mesh-plain-nets"><span class="mesh-exports-title">网关网段</span><span class="mono">{{ plainNets.join('、') }}</span></p>
      </section>

      <dl v-if="stats.length" class="mesh-stats" aria-label="运行情况">
        <div v-for="s in stats" :key="s.label" :class="{'is-wide': s.wide}">
          <dt>{{ s.label }}</dt>
          <dd><span :class="[s.cls, {mono: s.mono}]" :title="s.title || undefined">{{ s.value }}</span><span v-if="s.tag" class="pb-tag" data-tone="warn">{{ s.tag }}</span>
            <small v-if="s.note" :class="s.noteCls">{{ s.note }}<time v-if="s.at" :title="timeLabel(s.at)">（{{ relativeTime(s.at, Date.now()) }}）</time></small></dd>
        </div>
      </dl>
      <p v-if="detailError" class="instance-muted mesh-detail-error">读不到实时状态：{{ detailError }}</p>

      <section v-if="routesState !== 'unsupported' && !m.removing" class="mesh-routes" aria-label="路由">
        <header class="mesh-trend-head"><h4>路由</h4><span v-if="routeList && (routeList.routes.length || routeList.subnets.length)" class="instance-muted">点一行在画板上查看</span></header>
        <p v-if="routesState === 'loading'" class="instance-muted" role="status">正在读取…</p>
        <p v-else-if="routesState === 'draft'" class="instance-muted">应用到网格后，这里显示它去各处实际走的路线。</p>
        <p v-else-if="routesState === 'error'" class="instance-muted">读不到路由：{{ routesError }}</p>
        <template v-else-if="routeList">
          <p v-if="!routeList.routes.length && !routeList.subnets.length" class="instance-muted">还没有路由。</p>
          <ul v-if="shownRoutes.length" class="mesh-route-list">
            <li v-for="r in shownRoutes" :key="r.key">
              <button type="button" class="mesh-route" :aria-pressed="shownPath === routeKey(r)" :data-broken="r.broken || undefined" @click="showRoute(r)">
                <span class="mesh-route-name">{{ r.title }}</span>
                <span class="mesh-route-through">{{ r.through }}<span v-if="r.stale && !r.broken" class="ob-warn"> · 数据不完整</span></span>
                <span class="mesh-route-rtt">{{ r.rtt }}</span>
              </button>
            </li>
          </ul>
          <template v-if="shownNets.length">
            <h5 class="mesh-route-sub">网段</h5>
            <ul class="mesh-route-list">
              <li v-for="r in shownNets" :key="r.key">
                <button type="button" class="mesh-route" :aria-pressed="shownPath === routeKey(r)" :data-broken="r.broken || undefined" @click="showRoute(r)">
                  <span class="mesh-route-name mono">{{ r.title }}</span>
                  <span class="mesh-route-through">{{ r.through }}<span v-if="r.stale && !r.broken" class="ob-warn"> · 数据不完整</span></span>
                  <span class="mesh-route-rtt">{{ r.rtt }}</span>
                </button>
              </li>
            </ul>
          </template>
          <button v-if="routeHidden" type="button" class="ix-text-button mesh-fold-toggle" :aria-expanded="routesOpen" @click="routesOpen = !routesOpen">{{ routesOpen ? '收起' : `展开其余 ${routeHidden} 条` }}</button>
          <p v-if="routesError" class="instance-muted">暂时读不到最新路由（{{ routesError }}），稍后重试。</p>
        </template>
      </section>
      <MeshTrend :server-id="id" />
      <NetAddresses :server-id="id" :online="online" title="本机地址" :limit="4" />
      <DdnsCard :server-id="id" active panel />
      <div class="mesh-member-foot">
        <span class="ob-grow" />
        <button v-if="m.removing" type="button" class="ix-text-button" @click="restore">撤销移除</button>
        <button v-else type="button" class="ix-text-button ob-danger" :disabled="busy" @click="remove">从组网移除…</button>
      </div>
    </section>

    <MeshEntries v-else-if="tab === 'entry'" class="mesh-pane" :member="m" :index="index" :phone="phone" :health="health" />

    <form v-else-if="tab === 'settings'" class="instance-form mesh-pane mesh-config" autocomplete="off" aria-label="设置" @submit.prevent>
      <section class="mesh-group" aria-label="基本">
        <h4 class="mesh-group-title">基本</h4>
        <MeshField label="标签" help="用于画板展示和访问规则里的分组，例如“公司”。">
          <MeshChips v-model="m.tags" label="添加标签" placeholder="添加标签" :max="20" :maxlength="40" :suggestions="knownTags" @change="save" />
          <small v-for="e in under('tags')" :key="e" class="ob-bad">{{ e }}</small>
        </MeshField>
        <MeshField label="发送速率" help="限制它发往网格的速率。不设时，线路跑满后外部线路上的排队延迟没有保证；建议按这条线路上行带宽的九成设置。">
          <div class="ob-inline mesh-rate">
            <input v-bind="noAutofill" type="number" inputmode="decimal" min="1" step="any" :value="m.uploadLimitMbps ?? ''" placeholder="不限" aria-label="发送速率" @change="setLimit($event.target.value)" />
            <small class="sv-unit-label">Mbit/s</small>
          </div>
          <small v-for="e in at('uploadLimitMbps')" :key="e" class="ob-bad">{{ e }}</small>
        </MeshField>
      </section>

      <section class="mesh-group" aria-label="网络">
        <h4 class="mesh-group-title">网络</h4>
        <MeshField label="网格地址" help="它在网格里的地址，在地址段里；留空时应用后自动分配。">
          <input v-bind="noAutofill" :value="addressText" class="mono" inputmode="decimal" maxlength="15" :class="{invalid: addressErrors.length}" placeholder="留空自动分配" aria-label="网格地址"
            :aria-invalid="addressErrors.length > 0 || undefined" @input="typeAddress($event.target.value)" @change="setAddress" />
          <small v-for="e in addressErrors" :key="e" class="ob-bad">{{ e }}</small>
        </MeshField>
        <MeshField label="对外地址" help="别的成员连它用的 IP 或域名。留空用节点上报的公网 IP；有端口映射时填写。">
          <input v-model.trim="m.publicEndpoint" v-bind="noAutofill" class="mono" :class="{invalid: at('publicEndpoint').length}" :placeholder="m.publicEndpointAuto ? `自动：${m.publicEndpointAuto}` : '自动（还没有上报公网 IP）'" aria-label="对外地址" @change="save" />
          <small v-for="e in at('publicEndpoint')" :key="e" class="ob-bad">{{ e }}</small>
          <small v-if="site && !at('publicEndpoint').length" class="instance-muted">站点中转不对外宣告地址，这里填写的地址不会使用。</small>
          <small v-if="!site && exitDiffers" class="ob-warn">上报的 {{ m.publicEndpointAuto }} 和实测出口 {{ exitDiffers }} 不同（可能经过了代理），别的成员按自动地址会连不上它。</small>
        </MeshField>
        <MeshField label="监听" :help="relay ? LISTEN_HELP.relay : LISTEN_HELP.member">
          <div class="mesh-listen">
            <div v-for="[t, label] in TRANSPORTS.filter(([x]) => relay || x !== 'tls' || listens(m, x))" :key="t" class="mesh-listen-item" :data-on="listens(m, t) || undefined">
              <label class="ob-check"><input type="checkbox" :checked="listens(m, t)" :aria-label="`监听 ${label}`" @change="setListen(t, $event.target.checked)" />{{ label }}</label>
              <input v-if="listens(m, t)" v-bind="noAutofill" inputmode="numeric" maxlength="5" class="mono" :class="{invalid: portErrors(t).length}" :value="portText[t] ?? m.listen[t]"
                :placeholder="String(ports[t])" :aria-label="`${label} 端口`" :aria-invalid="portErrors(t).length > 0 || undefined" @input="typePort(t, $event.target.value)" @change="setPort(t, $event.target.value)" />
              <span v-else class="mesh-listen-off">未开启</span>
            </div>
            <template v-for="[t] in TRANSPORTS" :key="`note-${t}`">
              <template v-if="listens(m, t)">
                <small v-for="e in portErrors(t)" :key="e" class="ob-bad mesh-listen-note" :data-port="t">{{ e }}</small>
                <small v-for="w in portWarnings[t] || []" :key="w.text" class="ob-warn mesh-listen-note" :data-port="t">{{ w.text }}</small>
              </template>
            </template>
          </div>
          <small v-for="e in at('listen')" :key="e" class="ob-bad">{{ e }}</small>
          <small v-for="w in portWarnings[''] || []" :key="w.text" class="instance-muted">{{ w.text }}</small>
        </MeshField>
      </section>

      <section class="mesh-group" aria-label="角色">
        <h4 class="mesh-group-title">角色</h4>
        <template v-if="linux">
          <MeshField label="中转" help="接受其他成员的连接并替它们转发流量。">
            <label class="ob-check"><input type="checkbox" :checked="relay" @change="setRelay($event.target.checked)" /><span>作为中转</span></label>
            <small v-if="relay && users.length" class="ob-warn">{{ users.length }} 台成员把它当入口，关掉后它们要换入口</small>
            <small v-for="e in at('role')" :key="e" class="ob-bad">{{ e }}</small>
          </MeshField>
          <MeshField v-if="relay" label="中转类型" :help="RELAY_MODES.map(([, l, d]) => `${l}：${d}`).join('\n')">
            <div class="mesh-choice mesh-relay-mode" role="radiogroup" aria-label="中转类型">
              <label v-for="[value, label] in RELAY_MODES" :key="value"><input type="radio" name="mesh-relay-mode" :value="value" :checked="(site ? 'site' : 'public') === value" @change="setMode(value)" />{{ label }}<span v-if="recommended === value" class="pb-tag" data-tone="ok">推荐</span></label>
            </div>
            <small v-if="recommended && recommended !== (site ? 'site' : 'public')" class="ob-warn">可达性检测结果是“{{ verdict[1] }}”，建议设为{{ RELAY_MODES.find(([v]) => v === recommended)[1] }}。</small>
            <small v-if="lonelySite" class="ob-warn">{{ codeText('mesh_site_relay_needs_public_link') }}</small>
            <small v-for="e in modeErrors.filter(e => !lonelySite || e !== codeText('mesh_site_relay_needs_public_link'))" :key="e" class="ob-bad">{{ e }}</small>
          </MeshField>
          <MeshField label="网关" help="把它所在局域网的网段接入网格。在访问规则的目标里写明这些网段后，其他成员才能访问其中的机器。">
            <label class="ob-check"><input type="checkbox" :checked="Boolean(m.gateway)" @change="setGateway($event.target.checked)" /><span>把局域网网段接入网格</span></label>
            <template v-if="m.gateway">
              <MeshChips v-model="m.gateway.subnets" label="添加网关网段" :placeholder="lanNets.length ? '选择或输入网段' : '192.168.1.0/24'" mono live :maxlength="18"
                :validate="netProblem" :suggestions="lanNets" @change="save" />
              <small v-for="e in under('gateway')" :key="e" class="ob-bad">{{ e }}</small>
              <div class="mesh-masq">
                <label class="ob-check"><input v-model="m.gateway.masquerade" type="checkbox" @change="save" /><span>地址转换（IP 伪装）</span></label>
                <MeshHelp :text="MASQUERADE_HELP" label="地址转换" />
              </div>
              <small v-if="!m.gateway.masquerade" class="instance-muted">需要在局域网路由器上把组网网段 {{ mesh.work?.settings?.prefix || '' }} 指向这台网关</small>
            </template>
          </MeshField>
        </template>
        <template v-else>
          <p class="instance-muted mesh-config-note">中转和网关只能由 Linux 担任，这台是 {{ PLATFORMS[m.platform] || m.platform }}，只能作为成员。</p>
          <small v-for="e in [...at('role'), ...under('gateway')]" :key="e" class="ob-bad">{{ e }}</small>
        </template>
      </section>
    </form>

    <section v-else-if="tab === 'peers'" class="mesh-pane" aria-label="对端">
      <p v-if="!detail && !detailError" class="instance-muted" role="status">正在读取…</p>
      <p v-else-if="!peers.length" class="instance-muted">{{ detailError ? `读不到：${detailError}` : '还没有对端。应用后，这里实时显示它和每个对端的连接。' }}</p>
      <template v-else>
        <p v-if="detailError" class="instance-muted">暂时读不到最新状态：{{ detailError }}</p>
        <section v-for="g in peerGroups" :key="g.kind" class="mesh-peer-group" :aria-label="g.label">
          <h4 class="mesh-group-title">{{ g.label }}<span class="instance-muted">{{ g.list.length }}</span></h4>
          <ul class="mesh-peers">
            <li v-for="p in g.list" :key="p.id + p.kind" :data-active="p.active !== false">
              <span class="mesh-peer-name"><strong :title="nameOf(members, p.id)">{{ nameOf(members, p.id) }}</strong><span v-if="p.transport" class="mesh-peer-transport pb-tag is-plain">{{ transportLabel(p.transport) }}</span></span>
              <time class="mesh-peer-time" :data-fresh="fresh(p) || undefined" :title="handshakeMs(p) ? `最近握手 ${timeLabel(handshakeMs(p))}` : ''">{{ handshakeMs(p) ? relativeTime(handshakeMs(p), Date.now()) : '未握手' }}</time>
              <span class="mesh-peer-endpoint mono" :title="p.localAddr ? `本机 ${p.localAddr}` : ''">{{ p.endpoint || '—' }}</span>
              <span class="mesh-peer-traffic">↓ {{ bytes(p.rxBytes || 0) }} · ↑ {{ bytes(p.txBytes || 0) }}</span>
              <span v-if="coldPeer(p) || p.reconnects" class="mesh-peer-note">{{ coldPeer(p) ? `冷备，定时试连${p.reconnects ? ` ${p.reconnects} 次` : ''}` : `重连 ${p.reconnects} 次` }}</span>
              <span v-if="directLine(p.direct)" class="mesh-peer-direct" :data-state="p.direct.state">{{ directLine(p.direct) }}</span>
            </li>
          </ul>
        </section>
      </template>
    </section>

    <section v-else-if="tab === 'events'" class="mesh-pane" aria-label="事件">
      <p v-if="!detail && !detailError" class="instance-muted" role="status">正在读取…</p>
      <p v-else-if="!allEvents.length" class="instance-muted">{{ detailError ? `读不到：${detailError}` : '还没有切换、重连或错误。' }}</p>
      <template v-else>
      <p v-if="routine.length" class="instance-muted mesh-routine-note">
        <span>{{ showRoutine ? '含 ' : '已隐藏 ' }}{{ routineCount }} 条例行记录：备用入口定时试连、中转之间确认接入等</span>
        <button type="button" class="ix-text-button" :aria-expanded="showRoutine" @click="showRoutine = !showRoutine">{{ showRoutine ? '隐藏' : '显示' }}</button>
      </p>
      <ol v-if="eventList.length" class="mesh-events">
        <li v-for="r in eventList" :key="r.key" :data-tone="r.tone" :data-kind="r.kind">
          <span class="sv-log-source">{{ r.label }}</span>
          <div class="mesh-event-body">
            <span v-if="r.text" class="mesh-event-detail">{{ r.text }}</span>
            <span v-if="r.repeat > 1 || r.items || r.raw" class="mesh-event-more">
              <small v-if="r.repeat > 1 && repeatText(r)" class="mesh-event-repeat">{{ repeatText(r) }}</small>
              <button v-if="r.items" type="button" class="ix-text-button" :aria-expanded="openRuns.has(r.key)" @click="toggleRun(r.key)">{{ openRuns.has(r.key) ? '收起' : `展开 ${r.items.length} 条` }}</button>
              <details v-if="r.raw" class="mesh-event-raw"><summary>原文</summary><code>{{ r.raw }}</code></details>
            </span>
            <ol v-if="r.items && openRuns.has(r.key)" class="mesh-event-items">
              <li v-for="x in r.items" :key="x.key"><span>{{ x.text }}<small v-if="x.repeat > 1" class="mesh-event-repeat"> · {{ x.repeat }} 次</small></span><time :title="timeLabel(x.at)">{{ relativeTime(x.at, Date.now()) }}</time></li>
            </ol>
          </div>
          <time :title="timeLabel(r.at)">{{ relativeTime(r.at, Date.now()) }}</time>
        </li>
      </ol>
      </template>
    </section>

    <section v-if="probeSeen" v-show="tab === 'probe'" class="mesh-pane" aria-label="测试">
      <NetProbe :server-id="id" :servers="items" :online="online" :active="meshActive" board :shown-path="shownPath" @show-path="emit('show-path', $event)" />
    </section>
  </div>
</template>
