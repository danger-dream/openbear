<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, provide, reactive, ref, toRaw, watch } from 'vue';
import { useVueFlow, VueFlow } from '@vue-flow/core';
import '@vue-flow/core/dist/style.css';
import { ElMessage } from 'element-plus';
import { ArrowUpRight, Cable, Link2, Maximize, Minus, MousePointer2, Network, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, Plus, ShieldCheck, X } from '@lucide/vue';
import { flag } from '../serverHelpers.js';
import { MeshApi } from './meshApi.js';
import { directPeers, install, layout, loadPeers, mesh, meshError, meshErrorText, place, removeFromMesh, ruleHover, save, saveLayout, summaryPeers } from './meshStore.js';
import { ALERT_BADGES, codeText, directLabel, directLive, directReason, errorMessage, errorsUnder, HOP_VIA, isPunched, isRelay, isSite, LINK_STATES, msText, nameOf, needsPunch, nodeAlerts, nodeState,
  LINE_STATES, lineOf, lossText, MISSING_TEXT, PROGRESS, progressOf, protoText, reachOf, relayInUse, relayMetric, routedLinks, rtt, ruleSentence, sameExit, transportLabel, upgradeLabel, VERDICT_NOTES, verdictBadge, warningsUnder } from './meshHelpers.js';
import { addLink, alignTo, avoidBend, customRelayLinks, DRAG_TYPE, drawnRule, edgeGeometry, entryHow, entryLook, legendText, lineIdOf, linkKindLabel, nextSlot, pathEdges, phoneLayout, planLink, punchedRelayLink, relayPairs, ruleEnds, ruleNodes, SNAP_PX,
  selectionTitle, standbyNote, TOOLS } from './meshGraph.js';
import MeshChanges from './MeshChanges.vue';
import MeshEdge from './MeshEdge.vue';
import MeshInspector from './MeshInspector.vue';
import MeshNode from './MeshNode.vue';
import MeshServers from './MeshServers.vue';
import MeshSheet from './MeshSheet.vue';
import { usePoll } from './usePoll.js';

// The mesh as a board: servers on the left, the network in the middle, the
// selection's properties on the right. Phones get the same board with touch
// controls and the panel in a bottom sheet.
const props = defineProps({phone: Boolean, active: Boolean, items: {type: Array, default: () => []}, focus: {type: Object, default: null}});
// Whether the mesh page is on screen: a network test in the member panel stops when it is left.
provide('meshActive', computed(() => props.active));
const emit = defineEmits(['open-server', 'apply']);
const flow = useVueFlow('openbear-mesh');
const NODE_W = 184, NODE_H = 64, PHONE_W = 150, PHONE_H = 52, PHONE_MIN_ZOOM = 0.9;
const TOOL_ICONS = {select: MousePointer2, entry: ArrowUpRight, relay: Network, direct: Link2, rule: ShieldCheck};
const tool = ref('select'), sel = ref(null), showRules = ref(false), sheet = ref(''), canvas = ref(null);
const dragLine = ref(null), linking = ref(null), notice = ref(null), noticeEl = ref(null), flash = ref(''), dropping = ref(false), now = ref(Date.now());
// Path viewing: pick the target (pathPick), then the path from GET /mesh/path, refreshed while shown.
const pathPick = ref(null), path = ref(null);
const pending = reactive({}), phoneMoved = reactive({});
let noticeTimer, flashTimer, clock, fitted = false;

const data = computed(() => mesh.data);
const work = computed(() => mesh.work);
const members = computed(() => work.value?.members || []);
const regionOf = id => props.items.find(i => i.id === id)?.region;
const installing = computed(() => {
  const list = [...(data.value?.installing || []), ...Object.values(pending)];
  const seen = new Set(members.value.map(m => m.serverId));
  return list.filter(x => x?.serverId && !seen.has(x.serverId) && seen.add(x.serverId));
});
const ids = computed(() => [...members.value.map(m => m.serverId), ...installing.value.map(x => x.serverId)]);
const relayId = id => isRelay(members.value.find(m => m.serverId === id));
const overall = computed(() => {
  const live = members.value.filter(m => !m.removing);
  const states = live.map(m => data.value?.nodes?.[m.serverId]?.state);
  const failed = live.filter(m => data.value?.nodes?.[m.serverId]?.state === 'failed' || progressOf(data.value, m.serverId)?.state === 'failed').length;
  const bad = states.filter(s => ['degraded', 'offline'].includes(s)).length, busy = states.filter(s => s === 'installing').length + installing.value.length;
  const missing = states.filter(s => s === 'missing').length;
  if (missing) return ['bad', `${missing} 台组网服务不见了`];
  if (failed) return ['bad', `${failed} 台失败`];
  if (bad) return ['bad', `${bad} 台异常`];
  if (busy) return ['warn', `${busy} 台安装中`];
  if (states.length && states.every(s => s === 'ready')) return ['ok', '全部就绪'];
  return ['off', data.value?.applied?.revision ? '部分未就绪' : '尚未应用'];
});

// Nodes without a position go to the end of their row (relays on top); nobody chose that place, so it is not saved.
watch(() => ids.value.join(','), () => {
  const all = ids.value;
  for (const id of all.filter(id => !layout.positions[id])) {
    const here = Object.fromEntries(all.filter(x => layout.positions[x]).map(x => [x, layout.positions[x]]));
    place(id, nextSlot(here, all.filter(x => x !== id && relayId(x) === relayId(id)), relayId(id)), false);
  }
}, {immediate: true});

// Phones show their own top-down layout (moves stay on this page); desktops the saved one.
const phonePlaces = computed(() => props.phone ? {...phoneLayout(ids.value, relayId), ...phoneMoved} : {});
const posOf = id => (props.phone ? phonePlaces.value[id] : layout.positions[id]) || null;
const nodeSize = () => props.phone ? [PHONE_W, PHONE_H] : [NODE_W, NODE_H];

// ---- what the board draws ----
function badgeOf(m) {
  const id = m.serverId, node = data.value?.nodes?.[id], progress = progressOf(data.value, id), [tone, label] = nodeState(data.value, id);
  // Skipped by every rollout until it is reinstalled: the state says more than the step.
  if (node?.state === 'missing') return [tone, label, MISSING_TEXT];
  const step = data.value?.apply?.running || progress?.state === 'failed' ? progress : null;
  if (step) return [PROGRESS[step.state]?.[0] || 'off', PROGRESS[step.state]?.[1] || step.state, step.error ? `失败：${meshErrorText(step.error)}` : ''];
  if (!['failed', 'installing'].includes(node?.state) && upgradeLabel(node)) return ['warn', upgradeLabel(node)];
  return tone === 'ok' ? null : [tone, label];
}
// Board objects are kept while they read the same (by their JSON): a poll that changes nothing, or a
// node being dragged, then hands the board the same objects and nothing is drawn again.
function reuse(cache, next, id, value, key = JSON.stringify(value)) {
  const old = cache.get(id), hit = old && old.key === key ? old : {key, value};
  next.set(id, hit);
  return hit.value;
}
const sameList = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
// What each node shows, apart from where it is.
let lookCache = new Map();
const nodeLooks = computed(() => {
  const selected = sel.value?.kind === 'node' ? sel.value.id : '', from = dragLine.value?.from || linking.value?.from || '';
  const target = dragLine.value?.target || '';
  const onPath = pathNodes.value, next = new Map(), out = new Map();
  members.value.forEach((m, index) => {
    const [tone, label] = nodeState(data.value, m.serverId);
    const reach = reachOf(data.value, m.serverId), verdict = verdictBadge(reach), alerts = m.removing ? [] : nodeAlerts(data.value, m, members.value);
    const alert = alerts[0] ? (ALERT_BADGES[alerts[0].code] || ['bad', codeText(alerts[0].code) || alerts[0].code]) : null;
    out.set(m.serverId, reuse(lookCache, next, m.serverId, {name: m.name, flag: flag(regionOf(m.serverId)), address: m.address, relay: isRelay(m), site: isSite(m), gateway: Boolean(m.gateway), subnets: m.gateway?.subnets || [],
        tone, title: `${m.name} · ${label}`, rtt: isRelay(m) ? '' : rtt(data.value?.nodes?.[m.serverId]?.summary), badge: badgeOf(m), removing: m.removing,
        reach: verdict && !m.removing ? [...verdict, `可达性：${verdict[1]}。${VERDICT_NOTES[reach.verdict] || VERDICT_NOTES.unknown}`] : null,
        alert: alert ? [alert[0], alert[1], alerts.map(a => a.text).join('\n')] : null,
        problems: errorsUnder(mesh.errors, `members[${index}]`).length, selected: selected === m.serverId, source: from === m.serverId || pathPick.value?.from === m.serverId,
        target: target === m.serverId ? (dragLine.value.plan?.error ? 'bad' : 'ok') : '', ports: !props.phone && tool.value === 'select' && !m.removing,
        onPath: Boolean(onPath?.has(m.serverId)), dim: dimmedNode(m.serverId, onPath), compact: props.phone}));
  });
  for (const x of installing.value) {
    const secs = Math.max(0, Math.round((now.value - (x.startedAt || now.value)) / 1000));
    out.set(x.serverId, reuse(lookCache, next, x.serverId, {name: x.name || nameOf([], x.serverId), flag: flag(regionOf(x.serverId)), installing: true, tone: 'warn', badge: ['warn', `安装中 ${secs} 秒`],
      selected: selected === x.serverId, title: `${x.name} · 正在安装组网服务`, dim: dimmedNode(x.serverId, onPath), compact: props.phone}));
  }
  lookCache = next;
  return out;
});
// The nodes with their places: a drag moves one, the others stay the same objects.
let nodeCache = new Map(), shownNodes = [];
const nodes = computed(() => {
  const looks = nodeLooks.value, selected = sel.value?.kind === 'node' ? sel.value.id : '', member = new Set(members.value.map(m => m.serverId)), next = new Map(), list = [];
  for (const id of ids.value) {
    const data = looks.get(id);
    if (!data) continue;
    const pos = posOf(id) || {x: 0, y: 0}, draggable = member.has(id) ? (props.phone ? selected === id : tool.value === 'select') : !props.phone, old = nodeCache.get(id);
    const node = old && old.data === data && old.draggable === draggable && old.position.x === pos.x && old.position.y === pos.y ? old
      : {id, type: 'mesh', position: {x: pos.x, y: pos.y}, draggable, data};
    next.set(id, node); list.push(node);
  }
  nodeCache = next;
  return sameList(list, shownNodes) ? shownNodes : (shownNodes = list);
});

const LOOK_TITLES = {active: '在用', primary: '首选', standby: '备用', bad: '不可用', punching: '正在打洞'};
// Older controllers send no punch warnings: the board tells itself, unless the two share one exit (one LAN) or are already through.
const bothSymmetric = (a, b, live = null) => live?.state !== 'direct' && !sameExit(data.value, a, b) && [a, b].every(id => reachOf(data.value, id)?.verdict === 'symmetric');
// A link between relays: dialled or punched, with what its ends measure (summary.routing) and,
// when punched, the punch state (summary.direct). Labels only once there is something to say.
function relayLook(id, a, b, pair, selected, path = '') {
  const punched = punchedRelayLink(work.value, a, b), metric = relayMetric(data.value, a, b);
  const live = punched ? directLive(directPeers(a, id), directPeers(b, id), a, b) : null, bias = Number(pair?.bias) || 0;
  let label, why = '', look = '';
  if (metric && metric.state !== 'up') {
    label = LINK_STATES[metric.state][1]; look = metric.state;
    why = metric.state === 'down' ? '探测连续没有回应，选路先绕开它' : '最近反复断开，稳定后自动恢复';
  }
  else if (live && live.state !== 'direct') {
    label = live.state === 'punching' ? '打洞中' : '未打通';
    why = live.state === 'punching' ? directReason(live) : '打洞没有成功，暂时经其他中转绕行，会定时重试';
  } else {
    const ms = msText(metric?.rttMs ?? live?.rttMs);
    const fixed = pair && !punched && !ms ? [transportLabel(pair.transport), Number(pair.cost) > 1 ? `代价 ${pair.cost}` : ''] : [ms];
    label = pair || ms || punched ? [punched ? '打洞' : '拨号', ...fixed].filter(Boolean).join(' · ') : '';
  }
  if (selected && why && label) label = `${label} · ${why}`;
  // The controller's warnings (mesh_punch_impossible) or, from older ones, what the board sees itself.
  const warn = [...(path ? warningsUnder(data.value, path) : []), punched && !Array.isArray(data.value?.warnings) && bothSymmetric(a, b, live) ? codeText('mesh_punch_impossible') : ''];
  const notes = [...new Set([why, ...warn, bias ? `偏好 ${bias > 0 ? '+' : ''}${bias} ms` : ''].filter(Boolean))];
  return {label, look, live: live?.state || '', title: notes.join('；'), method: punched ? '打洞' : '拨号'};
}
// What carries traffic, worked out once per GET /mesh: the links between relays the relays route
// through, and every line's state and role.
// (GET /mesh replaces the state whole, so its plain copy is read: nothing in it is tracked one by one.)
const usage = computed(() => {
  const state = toRaw(data.value), lines = Array.isArray(state?.lines) ? state.lines : [];
  return {routed: routedLinks(state), lines: new Map(lines.filter(l => l?.id).map(l => [l.id, l]))};
});
const pairLine = (a, b) => usage.value.lines.get(`relay:${[a, b].sort().join('|')}`);
// A line not in use (a standby entry, a relay link nothing is routed through) is drawn quiet: thin,
// faint and without its label until it or one of its ends is pointed at or selected. A line with a
// problem is never quiet.
const IDLE_ROLES = ['standby', 'cold', 'unused'];
function entryQuiet(look, line) {
  if (look.problem || ['bad', 'failing', 'punching', 'active'].includes(look.look) || line?.state === 'down') return false;
  return look.standby || IDLE_ROLES.includes(line?.role) || IDLE_ROLES.includes(line?.state);
}
function relayQuiet(r, line, a, b) {
  if (r.look || (r.live && r.live !== 'direct') || line?.state === 'down') return false;
  return relayInUse(usage.value.routed, line, a, b) === false;
}
// The links and what they show, apart from what is in focus; where their labels go is worked out below.
const edgeRaw = computed(() => {
  const w = work.value, out = [], present = new Set(ids.value);
  if (!w) return [];
  const selId = sel.value?.kind === 'edge' ? sel.value.id : '', ruleId = sel.value?.kind === 'rule' ? sel.value.id : '', rulesId = sel.value?.kind === 'rules' ? sel.value.id : '';
  const push = (id, source, target, d) => present.has(source) && present.has(target) && source !== target && out.push({id, source, target, type: 'mesh', data: {...d, selected: d.selected ?? id === selId}});
  for (const m of members.value) {
    if (isRelay(m)) continue;
    (m.entry?.candidates || []).forEach((c, i) => {
      const look = entryLook(w, data.value, mesh.errors, m, i), relayName = nameOf(members.value, c.relay);
      // Entries to a site relay say LAN or internet once through, punching until then; a failing standby says why on the line.
      const how = entryHow(look, c.transport), id = `entry:${m.serverId}:${c.id}`;
      // Standbys carry nothing: drawn quiet, back to normal while the line (or one of its ends) is in focus.
      const quiet = entryQuiet(look, usage.value.lines.get(`entry:${m.serverId}|${c.relay}|${c.transport}`)) && id !== selId;
      push(id, m.serverId, c.relay, {kind: 'entry', look: look.look, quiet, arrow: 'end', label: [`${i + 1}`, how, look.failing].filter(Boolean).join(' · '), flash: flash.value === id,
        title: look.problem || (look.failing ? `${relayName} ${look.role}：${look.failing}` : [look.opened ? `${look.role}，经${look.via || '打洞'}接入` : LOOK_TITLES[look.look], look.standby ? standbyNote(look) : ''].filter(Boolean).join('；')),
        aria: `${m.name} 的第 ${i + 1} 个入口：${relayName} · ${how}，${look.problem || (look.failing ? `${look.role}，${look.failing}` : look.role)}${look.standby ? `，${standbyNote(look)}` : ''}`});
    });
  }
  for (const [a, b] of relayPairs(w)) {
    const id = `relay:${[a, b].sort().join('~')}`, r = relayLook(id, a, b, null, id === selId);
    push(id, a, b, {kind: 'relay', look: r.look || 'auto', live: r.live, quiet: id !== selId && relayQuiet(r, pairLine(a, b), a, b), label: r.label, title: r.title, aria: `中转互联（${r.method}，自动）：${nameOf(members.value, a)} 和 ${nameOf(members.value, b)}${r.title ? `，${r.title}` : ''}`});
  }
  if (w.relayLinks?.mode === 'custom') (w.relayLinks.pairs || []).forEach((p, i) => {
    const id = `pair:${i}`, r = relayLook(id, p.a, p.b, p, id === selId, `relayLinks.pairs[${i}]`), bad = errorsUnder(mesh.errors, `relayLinks.pairs[${i}]`).map(errorMessage);
    push(id, p.a, p.b, {kind: 'relay', look: bad.length ? 'bad' : r.look, live: bad.length ? '' : r.live, quiet: !bad.length && id !== selId && relayQuiet(r, pairLine(p.a, p.b), p.a, p.b), label: r.label, title: bad.join('；') || r.title, flash: flash.value === id,
      aria: `中转互联（${r.method}）：${nameOf(members.value, p.a)} 和 ${nameOf(members.value, p.b)}${bad.length ? `，${bad.join('；')}` : r.title ? `，${r.title}` : ''}`});
  });
  // Punched (UDP) direct links follow what their ends report: direct, punching or relayed.
  (w.directLinks || []).forEach((p, i) => {
    const id = `direct:${i}`, errors = errorsUnder(mesh.errors, `directLinks[${i}]`).map(errorMessage);
    const punch = needsPunch(data.value, p.a, p.b);
    const live = !errors.length && isPunched(p) ? directLive(directPeers(p.a, id), directPeers(p.b, id), p.a, p.b) : null, why = live ? directReason(live, punch) : '';
    const hopeless = [...warningsUnder(data.value, `directLinks[${i}]`), isPunched(p) && !Array.isArray(data.value?.warnings) && bothSymmetric(p.a, p.b, live) ? codeText('mesh_punch_impossible') : ''];
    push(id, p.a, p.b, {kind: 'direct', look: errors.length ? 'bad' : '', live: live?.state || '', flash: flash.value === id,
      label: live ? directLabel(live, id === selId, punch) : `直连 · ${transportLabel(p.transport)}`, title: errors.join('；') || [...new Set([why, ...hopeless].filter(Boolean))].join('；'),
      aria: `直连：${nameOf(members.value, p.a)} 和 ${nameOf(members.value, p.b)}${errors.length ? `，${errors.join('；')}` : why ? `，${why}` : ''}`});
  });
  // Rules that name machines: one arrow per pair of nodes, several rules on a pair merged into one.
  const pairs = new Map();
  (w.rules || []).forEach((r, i) => {
    if (!drawnRule(r, members.value)) return;
    if (!showRules.value && r.id !== ruleId && !ruleEnds(r, members.value).some(e => `rules:${[e.a, e.b].sort().join('|')}` === rulesId)) return;
    const bad = errorsUnder(mesh.errors, `rules[${i}]`).length > 0;
    for (const e of ruleEnds(r, members.value)) {
      const key = [e.a, e.b].sort().join('|'), p = pairs.get(key) || {a: e.a, b: e.b, rules: [], ways: new Set(), bad: false};
      p.rules.push({r, i}); p.ways.add(`${e.a}>${e.b}`); if (e.both) p.ways.add(`${e.b}>${e.a}`); p.bad ||= bad;
      pairs.set(key, p);
    }
  });
  for (const [key, p] of pairs) {
    const both = p.ways.size > 1, [way] = p.ways, [source, target] = both ? [p.a, p.b] : way.split('>');
    const names = `${nameOf(members.value, source)} ${both ? '和' : '访问'} ${nameOf(members.value, target)}`;
    if (p.rules.length === 1) {
      const {r, i} = p.rules[0];
      push(`rule:${r.id}:${key}`, source, target, {kind: 'rule', rule: r.id, look: p.bad ? 'bad' : '', arrow: both ? 'both' : 'end', flash: flash.value === r.id,
        label: r.proto === 'icmp' ? 'ping' : protoText(r).replace('所有端口', '全部'), selected: r.id === ruleId, aria: `访问规则 ${i + 1}：${names}`});
    } else push(`rules:${key}`, source, target, {kind: 'rule', rules: p.rules.map(x => x.r.id), look: p.bad ? 'bad' : '', arrow: both ? 'both' : 'end',
      label: `${p.rules.length} 条规则`, selected: rulesId === `rules:${key}` || p.rules.some(x => x.r.id === ruleId), flash: p.rules.some(x => x.r.id === flash.value),
      aria: `${p.rules.length} 条访问规则：${names}`});
  }
  // Links between the same two nodes bend apart, their labels spread along the line.
  const groups = new Map();
  for (const e of out) { const key = [e.source, e.target].sort().join('|'); groups.set(key, [...(groups.get(key) || []), e]); }
  for (const group of groups.values()) group.forEach((e, i) => {
    const sign = e.source < e.target ? 1 : -1;
    const lane = i - (group.length - 1) / 2;
    e.data.offset = lane * 34 * sign;
    e.data.labelAt = Math.min(0.78, Math.max(0.22, 0.5 + lane * 0.2 * sign));
  });
  return out;
});
// The node pointed at (desktop) or else selected, with the nodes it links to: its links come forward
// with their labels, the rest steps back. A shown path or a rule in focus comes first.
const hoverNode = ref('');
const nodeFocus = computed(() => {
  // While a path is being picked or shown, that is what the board is about.
  if (path.value || pathPick.value) return null;
  const id = (!props.phone && hoverNode.value) || (sel.value?.kind === 'node' ? sel.value.id : '');
  if (!id || !ids.value.includes(id)) return null;
  const nodes = new Set([id]);
  for (const e of edgeRaw.value) if (e.source === id) nodes.add(e.target); else if (e.target === id) nodes.add(e.source);
  return {id, nodes};
});
function dimmedNode(id, onPath) {
  if (onPath) return !onPath.has(id);
  if (focus.value) return !focus.value.nodes.has(id);
  return Boolean(nodeFocus.value) && !nodeFocus.value.nodes.has(id);
}
const edgeBase = computed(() => {
  const raw = edgeRaw.value, rule = focus.value, near = rule ? null : nodeFocus.value;
  // A path being shown: its links stand out, everything else steps back.
  const onPath = path.value?.hops?.length ? pathEdges(raw, path.value.hops) : null;
  if (!onPath && !rule && !near) return raw;
  return raw.map(e => {
    const d = {...e.data};
    if (onPath) { d.onPath = onPath.has(e.id); d.dim = !d.onPath; if (d.onPath) d.quiet = false; }
    // While a rule is in focus, everything else steps back.
    else if (rule) d.dim = d.kind !== 'rule' || !(d.rule ? d.rule === rule.id : d.rules?.includes(rule.id) || e.id === rule.id);
    else { const mine = e.source === near.id || e.target === near.id; d.dim = !mine; if (mine) d.quiet = false; }
    return {...e, data: d};
  });
});
// Where every node's box is now (the size it was drawn at, else the default).
function boxOf(id) {
  const [w0, h0] = nodeSize(), p = posOf(id), d = flow.findNode(id)?.dimensions, w = d?.width || w0, h = d?.height || h0;
  return p ? {cx: p.x + w / 2, cy: p.y + h / 2, w, h} : null;
}
const pairOf = e => [e.source, e.target].sort().join('|');
// Links that would run under another node bow around it (one bow for all the links of a pair, so they
// stay apart). Worked out when nodes or links change, not while a node is dragged: the links keep
// their bows then (and follow the node), and are worked out again once it is dropped.
// The pairs of nodes with links between them and how many: the same text after most polls, so what
// depends on it is not worked out again.
const linkedPairs = computed(() => {
  const lanes = new Map();
  for (const e of edgeRaw.value) lanes.set(pairOf(e), (lanes.get(pairOf(e)) || 0) + 1);
  return JSON.stringify([...lanes].sort());
});
let arcSpots = new Map(), arcKey = '';
const arcs = computed(() => {
  if (dragging.value) return arcSpots;
  const pairs = JSON.parse(linkedPairs.value), all = ids.value.map(id => [id, boxOf(id)]).filter(([, b]) => b);
  const key = JSON.stringify([pairs, all]);
  if (key === arcKey) return arcSpots;
  arcKey = key;
  const boxes = new Map(all), next = new Map();
  for (const [pair, n] of pairs) {
    const [a, b] = pair.split('|'), A = boxes.get(a), B = boxes.get(b);
    if (!A || !B) continue;
    // A phone's two columns leave no room for wide bows: those links stay straight, as before.
    const bend = avoidBend(A, B, all.filter(([id]) => id !== a && id !== b).map(([, x]) => x), 10 + (n - 1) * 17, props.phone ? 112 : undefined);
    if (bend) next.set(pair, bend);
  }
  return (arcSpots = next);
});
// A link's bow, from its source to its target.
const arcOf = (e, map = arcs.value) => (map.get(pairOf(e)) || 0) * (e.source < e.target ? 1 : -1);
// Labels are spread out by where the nodes are; while a node is dragged they keep their places along
// their lines (and move with them), and are spread again once it is dropped.
const dragging = ref(false);
let labelSpots = new Map(), labelKey = '';
const labelPlaces = computed(() => {
  if (dragging.value) return labelSpots;
  // Worked out again only when the links or the boxes they avoid are not as they were (a poll mostly leaves both).
  const list = edgeBase.value, bows = arcs.value, [w0, h0] = nodeSize();
  const key = JSON.stringify([props.phone, w0, h0, list.map(e => [e.id, e.source, e.target, e.data]), [...bows],
    ids.value.map(id => { const p = posOf(id), d = flow.findNode(id)?.dimensions; return [id, p?.x, p?.y, d?.width, d?.height]; })]);
  if (key !== labelKey) { labelKey = key; labelSpots = spreadLabels(list, bows); }
  return labelSpots;
});
let edgeCache = new Map(), shownEdges = [];
const edges = computed(() => {
  const places = labelPlaces.value, bows = arcs.value, next = new Map(), list = [];
  for (const e of edgeBase.value) {
    const data = {...e.data, offset: (e.data.offset || 0) + arcOf(e, bows), labelHidden: false, ...places.get(e.id)};
    const key = `${e.source}>${e.target}:${JSON.stringify(data)}`, old = edgeCache.get(e.id);
    let edge = old?.key === key ? old.edge : null;
    if (!edge) {
      const made = edge = {id: e.id, source: e.source, target: e.target, type: 'mesh', data};
      data.select = () => pick(selectionOfEdge(made));
    }
    next.set(e.id, {key, edge}); list.push(edge);
  }
  edgeCache = next;
  return sameList(list, shownEdges) ? shownEdges : (shownEdges = list);
});

// Links that cross (two site relays each dialling the other's public relay) put their
// labels in the same place: a later label slides along its own line to a free spot.
// Returns {edge id: {labelAt, labelHidden}}.
function spreadLabels(list, bows) {
  const out = new Map();
  const char = props.phone ? 12 : 11;
  // Where MeshEdge draws the label (bent, bowed and arched links included).
  const spot = (e, k) => { const a = boxOf(e.source), b = boxOf(e.target); return a && b ? edgeGeometry(a, b, (e.data.offset || 0) + arcOf(e, bows), k).label : null; };
  const placed = [], boxes = ids.value.map(boxOf).filter(Boolean);
  // What matters most gets a place first: the selection, the path, problems, then the rest; what steps back comes last.
  const weight = e => e.data.selected || e.data.flash ? 0 : e.data.onPath ? 1 : e.data.dim ? 4 : ['bad', 'suppressed', 'failing'].includes(e.data.look) ? 2 : 3;
  for (const e of [...list].sort((a, b) => weight(a) - weight(b))) {
    // A quiet line shows no label (pointing at it brings it up where it is).
    if (!e.data.label || e.data.quiet) continue;
    const width = [...e.data.label].reduce((n, ch) => n + (ch.charCodeAt(0) > 255 ? char : char * 0.6), 0) + 16;
    // Clear of the labels already placed and of every node (the ends included).
    const clear = p => !placed.some(o => Math.abs(o.x - p.x) < (o.width + width) / 2 + 4 && Math.abs(o.y - p.y) < 26)
      && !boxes.some(b => Math.abs(b.cx - p.x) < (b.w + width) / 2 + 2 && Math.abs(b.cy - p.y) < b.h / 2 + 14);
    let k = e.data.labelAt, p = spot(e, k);
    if (!p) continue;
    if (!clear(p)) {
      const free = [0.34, 0.66, 0.42, 0.58, 0.26, 0.74, 0.2, 0.8, 0.14, 0.86].map(x => [x, spot(e, x)]).find(([, q]) => clear(q));
      if (free) [k, p] = free;
      // A phone has no room to spare: with nowhere to go the label is left out (the line still shows the
      // state and selects on a tap; selecting brings the label back first).
      else if (props.phone && weight(e) > 2) { out.set(e.id, {labelHidden: true}); continue; }
    }
    if (k !== e.data.labelAt) out.set(e.id, {labelAt: k});
    placed.push({...p, width});
  }
  return out;
}
const selectionOfEdge = e => e.data?.rules ? {kind: 'rules', id: e.id} : e.data?.kind === 'rule' ? {kind: 'rule', id: e.data.rule} : {kind: 'edge', id: e.id};

// ---- paths ----
// The nodes of the path being shown; null while none is.
const pathNodes = computed(() => path.value?.hops?.length ? new Set(path.value.hops.map(h => h.node)) : null);
function startPath(from = '') {
  stopPath(); linking.value = null; sheet.value = '';
  pathPick.value = {from};
}
function stopPath() {
  pathPoll.stop();
  path.value = null; pathPick.value = null;
}
function showPath(src, dst, seed = null) {
  pathPick.value = null;
  path.value = {src, dst, subnet: seed?.subnet || '', hops: seed?.hops || [], rttMs: seed?.rttMs ?? null, stale: Boolean(seed?.stale), loading: !seed, seeded: Boolean(seed), error: '', unsupported: false};
  void pathPoll.run();
}
// A row of a node's routes: that path on the board at once (from the list), then kept fresh. On a phone the sheet steps aside.
function showRoute(r) {
  stopPath(); linking.value = null;
  showPath(r.src, r.dst, r);
  if (props.phone) sheet.value = '';
}
const pathKey = computed(() => path.value ? `${path.value.src}>${path.value.dst}${path.value.subnet ? `>${path.value.subnet}` : ''}` : '');
// Refreshed every 5 seconds while shown, so a change of route shows by itself; without a path endpoint it is not asked again.
const pathPoll = usePoll(async current => {
  const p = path.value;
  try {
    const r = await MeshApi.path(p.src, p.dst);
    if (!current()) return;
    // Each hop: how it is reached from the one before (the source's via is '') and that segment's rtt.
    const hops = Array.isArray(r?.hops) ? r.hops.filter(h => h?.node).map(h => ({node: h.node, via: h.via || '', rttMs: Number.isFinite(h.rttMs) ? h.rttMs : null})) : [];
    Object.assign(p, {hops, rttMs: Number.isFinite(r?.rttMs) ? r.rttMs : null, stale: Boolean(r?.stale), loading: false, error: ''});
  } catch (e) {
    if (!current()) return;
    // An older controller has no such endpoint; a member only in the draft has no path yet.
    const code = e?.response?.data?.error;
    // A path from the routes list stays as listed when there is no path endpoint to refresh it from.
    if (e?.response?.status === 404 && !['mesh_member_not_found', 'server_not_found'].includes(code)) { Object.assign(p, p.seeded ? {loading: false, final: true} : {unsupported: true, final: true, loading: false, hops: []}); return; }
    Object.assign(p, {loading: false, hops: code === 'mesh_member_not_found' ? [] : p.hops,
      error: code === 'mesh_member_not_found' ? '起点或终点还没有应用到网格，应用后才能查看路径' : meshError(e)});
  }
}, {interval: () => path.value && !path.value.final ? 5000 : 0});
// A path that breaks on the way lists the hops up to the break.
const pathBroken = computed(() => { const p = path.value; return Boolean(p?.hops.length) && p.hops.at(-1).node !== p.dst; });
const pathTotal = computed(() => {
  const p = path.value;
  if (!p?.hops.length || pathBroken.value) return '';
  return p.rttMs === null ? '总延迟未知' : `总延迟约 ${msText(p.rttMs)}`;
});
// The rule in focus (pointed at in the legend or the panel, else selected) and the members it touches.
const focus = computed(() => {
  const rules = work.value?.rules || [], s = sel.value;
  const id = ruleHover.id || (s?.kind === 'rule' ? s.id : '');
  const rule = rules.find(r => r.id === id);
  if (rule) return {id: rule.id, nodes: ruleNodes(rule, members.value)};
  if (s?.kind === 'rules') return {id: s.id, nodes: new Set(s.id.replace(/^rules:/, '').split('|'))};
  return null;
});
// Lines drawn quiet get a line in the legend saying what they are.
const hasQuiet = computed(() => edgeRaw.value.some(e => e.data.quiet));
// Rules for everyone, tags or networks: listed in a corner while the rule layer is on.
const legend = computed(() => {
  if (!showRules.value) return [];
  const live = members.value.filter(m => !m.removing);
  return (work.value?.rules || []).map((r, i) => ({r, i})).filter(({r}) => !drawnRule(r, members.value)).map(({r, i}) => ({id: r.id, index: i,
    text: legendText(r, live),
    bad: errorsUnder(mesh.errors, `rules[${i}]`).length > 0}));
});

// ---- selection ----
function pick(next, {sheetOpen = true} = {}) {
  sel.value = next;
  if (props.phone) sheet.value = next && sheetOpen ? 'inspect' : '';
}
function select(next) {
  if (next?.kind === 'node' && next.id) center(next.id);
  pick(next);
}
function center(id) {
  const p = posOf(id), [w, h] = nodeSize();
  if (p) flow.setCenter(p.x + w / 2, p.y + h / 2, {zoom: Math.max(flow.viewport.value.zoom, props.phone ? PHONE_MIN_ZOOM : 0.8), duration: 250});
}
const sheetTitle = computed(() => sheet.value === 'add' ? '添加服务器' : sheet.value === 'overview' ? '网格设置与访问规则' : selectionTitle(sel.value, work.value, installing.value));

// ---- folding the side columns (desktop) ----
// The server list and the properties panel can each fold away, remembered on this browser. The list
// folds to a rail with the tools; with the panel folded, a selection shows a small card on the board.
const FOLD_KEY = 'openbear.mesh.folded';
function readFolds() {
  try { const v = JSON.parse(localStorage.getItem(FOLD_KEY) || '{}'); return {palette: v?.palette === true, panel: v?.panel === true}; }
  catch { return {palette: false, panel: false}; }
}
const folded = reactive(readFolds());
watch(() => ({...folded}), v => { try { localStorage.setItem(FOLD_KEY, JSON.stringify(v)); } catch { /* private mode */ } });
// The card: what the selection is and how it is doing, with the way to the full panel.
const peek = computed(() => {
  const s = sel.value;
  if (props.phone || !folded.panel || !s) return null;
  const title = selectionTitle(s, work.value, installing.value);
  if (s.kind === 'node') {
    const m = members.value.find(x => x.serverId === s.id);
    if (!m) return installing.value.some(x => x.serverId === s.id) ? {title, rows: [{text: '正在安装组网服务', tone: 'warn', dot: true}]} : null;
    const [tone, label] = nodeState(data.value, m.serverId), badge = badgeOf(m), alerts = m.removing ? [] : nodeAlerts(data.value, m, members.value);
    const ms = isRelay(m) ? '' : rtt(data.value?.nodes?.[m.serverId]?.summary);
    return {title, flag: flag(regionOf(m.serverId)), rows: [
      {text: label, tone, dot: true, extra: [isSite(m) ? '站点中转' : isRelay(m) ? '中转' : '成员', m.address, ms ? `延迟 ${ms}` : ''].filter(Boolean).join(' · ')},
      ...(badge && badge[0] !== 'ok' && (badge[2] || badge[1]) !== label ? [{text: badge[2] || badge[1], tone: badge[0]}] : []),
      ...alerts.slice(0, 2).map(a => ({text: a.text, tone: 'warn'})),
      ...(alerts.length > 2 ? [{text: `另有 ${alerts.length - 2} 项需要注意`, tone: 'warn'}] : [])]};
  }
  if (s.kind === 'edge') {
    const e = edges.value.find(x => x.id === s.id), line = lineOf(data.value, lineIdOf(work.value, s.id));
    if (!e) return null;
    const [tone, state] = line ? LINE_STATES[line.state] || LINE_STATES.unknown : [];
    return {title, rows: [{text: e.data.aria || e.data.label}, ...(line ? [{text: [state, msText(line.rttMs), Number(line.lossRate) > 0 ? `丢包 ${lossText(line.lossRate)}` : ''].filter(Boolean).join(' · '), tone, dot: true}] : []),
      // Its problem, when it has one (the rest of the hover text repeats the first row).
      ...(e.data.look === 'bad' && e.data.title && !String(e.data.aria || '').includes(e.data.title) ? [{text: e.data.title, tone: 'bad'}] : [])]};
  }
  const live = members.value.filter(m => !m.removing);
  if (s.kind === 'rule') { const r = (work.value?.rules || []).find(x => x.id === s.id); return r ? {title, rows: [{text: ruleSentence(r, live)}, ...(r.note ? [{text: r.note, muted: true}] : [])]} : null; }
  if (s.kind === 'rules') {
    const pair = s.id.replace(/^rules:/, '').split('|'), list = (work.value?.rules || []).filter(r => drawnRule(r, members.value) && ruleEnds(r, members.value).some(e => pair.includes(e.a) && pair.includes(e.b)));
    return {title, rows: [...list.slice(0, 3).map(r => ({text: ruleSentence(r, live)})), ...(list.length > 3 ? [{text: `另有 ${list.length - 3} 条`, muted: true}] : [])]};
  }
  return null;
});

// ---- notices on the board ----
// A notice never covers a node, a link label or the board's own controls: it
// goes to the first free place among the preferred ones (top centre for results,
// next to the pointer for reasons), else the free place nearest to them.
const OBSTACLES = '.vue-flow__node, .mesh-edge-label, .mesh-banner, .mesh-fab-row, .mesh-fab, .mesh-dock, .mesh-legend, .mesh-path, .mesh-peek';
function freeSpot(size, wanted) {
  const box = canvas.value?.getBoundingClientRect();
  if (!box) return wanted[0];
  const pad = 6, rects = [...canvas.value.querySelectorAll(OBSTACLES)].map(el => el.getBoundingClientRect())
    .map(r => ({l: r.left - box.left - pad, t: r.top - box.top - pad, r: r.right - box.left + pad, b: r.bottom - box.top + pad}));
  const clamp = p => ({x: Math.min(Math.max(p.x, 8), Math.max(8, box.width - size.w - 8)), y: Math.min(Math.max(p.y, 8), Math.max(8, box.height - size.h - 8))});
  const free = p => rects.every(r => p.x + size.w <= r.l || p.x >= r.r || p.y + size.h <= r.t || p.y >= r.b);
  const tries = wanted.map(clamp);
  const hit = tries.find(free);
  if (hit) return hit;
  const grid = [];
  for (let y = 8; y + size.h <= box.height - 8; y += 16) for (let x = 8; x + size.w <= box.width - 8; x += 16) grid.push({x, y});
  const near = tries[0];
  return grid.filter(free).sort((a, b) => Math.hypot(a.x - near.x, a.y - near.y) - Math.hypot(b.x - near.x, b.y - near.y))[0] || near;
}
async function notify(text, at, tone = 'bad') {
  clearTimeout(noticeTimer);
  notice.value = {text, tone, x: 0, y: 0, placed: false};
  await nextTick();
  const el = noticeEl.value, box = canvas.value?.getBoundingClientRect();
  if (!el || !box || !notice.value) return;
  const size = {w: el.offsetWidth, h: el.offsetHeight}, mid = (box.width - size.w) / 2;
  const banner = canvas.value.querySelector('.mesh-banner, .mesh-fab-row')?.getBoundingClientRect();
  const top = banner ? banner.bottom - box.top + 8 : 10;
  const wanted = at ? [{x: at.x + 14, y: at.y + 16}, {x: at.x - size.w / 2, y: at.y + 22}, {x: at.x - size.w / 2, y: at.y - size.h - 22}, {x: at.x - size.w - 14, y: at.y + 16}, {x: mid, y: top}]
    : [{x: mid, y: top}, {x: mid, y: box.height - size.h - (props.phone ? 64 : 12)}];
  Object.assign(notice.value, freeSpot(size, wanted), {placed: true});
  noticeTimer = setTimeout(() => { notice.value = null; }, tone === 'ok' ? 2400 : 4200);
}
function flashLink(id) {
  clearTimeout(flashTimer);
  flash.value = id;
  flashTimer = setTimeout(() => { flash.value = ''; }, 1600);
}
const local = e => { const box = canvas.value.getBoundingClientRect(); return {x: e.clientX - box.left, y: e.clientY - box.top}; };

// ---- connecting ----
function connect(from, to, at) {
  const plan = planLink(work.value, from, to, linking.value?.tool || tool.value);
  if (plan.error) { void notify(plan.error, at); return false; }
  const next = addLink(work.value, plan);
  void save();
  if (plan.kind === 'rule') showRules.value = true;
  pick(next);
  flashLink(next.id);
  void notify(`已添加${linkKindLabel(plan.kind)}${next.opened?.length ? `，并为 ${next.opened.join('、')} 开启 UDP 监听` : ''}`, null, 'ok');
  return true;
}
const targetAt = (x, y) => { const el = document.elementFromPoint(x, y)?.closest('.vue-flow__node'); return el && canvas.value?.contains(el) ? el.dataset.id : ''; };
function portDown(e) {
  if (props.phone || e.button !== 0 || linking.value) return;
  const port = e.target.closest('.mesh-port'), el = e.target.closest('.vue-flow__node');
  if (!el || (!port && tool.value === 'select')) return;
  const id = el.dataset.id;
  if (!members.value.some(m => m.serverId === id && !m.removing)) return;
  e.preventDefault(); e.stopPropagation();
  const start = local({clientX: (port || el).getBoundingClientRect().left + (port || el).getBoundingClientRect().width / 2, clientY: (port || el).getBoundingClientRect().top + (port || el).getBoundingClientRect().height / 2});
  dragLine.value = {from: id, x1: start.x, y1: start.y, x2: start.x, y2: start.y, target: '', plan: null};
  window.addEventListener('pointermove', portMove);
  window.addEventListener('pointerup', portUp);
  window.addEventListener('pointercancel', portCancel);
}
function portMove(e) {
  const line = dragLine.value;
  if (!line) return;
  const p = local(e), target = targetAt(e.clientX, e.clientY);
  Object.assign(line, {x2: p.x, y2: p.y, target: target && target !== line.from ? target : ''});
  line.plan = line.target ? planLink(work.value, line.from, line.target, tool.value) : null;
  // The tip sits under the node being pointed at (above it near the bottom), clear of its title.
  const el = line.target && canvas.value?.querySelector(`.vue-flow__node[data-id="${CSS.escape(line.target)}"]`), box = canvas.value?.getBoundingClientRect();
  if (el && box) {
    const r = el.getBoundingClientRect(), below = r.bottom - box.top + 8 + 40 < box.height;
    line.tip = {x: r.left - box.left + r.width / 2, y: below ? r.bottom - box.top + 8 : r.top - box.top - 8, above: !below};
  } else line.tip = null;
}
function portCancel() {
  window.removeEventListener('pointermove', portMove); window.removeEventListener('pointerup', portUp); window.removeEventListener('pointercancel', portCancel);
  dragLine.value = null;
}
function portUp(e) {
  const line = dragLine.value;
  portCancel();
  const target = line && targetAt(e.clientX, e.clientY);
  if (line && target && target !== line.from) connect(line.from, target, local(e));
}
// Tap to connect: the panel's "连接到…" or the phone toolbar; the next tapped node is the other end.
function startLinking(from = '') {
  stopPath();
  linking.value = {from, tool: props.phone ? 'select' : tool.value};
  sheet.value = '';
}
function stopLinking() { linking.value = null; }

// ---- the board's events ----
function nodeClick({node, event}) {
  if (dragLine.value) return;
  if (pathPick.value) {
    if (!members.value.some(m => m.serverId === node.id)) return;
    if (!pathPick.value.from) { pathPick.value.from = node.id; return; }
    if (node.id !== pathPick.value.from) showPath(pathPick.value.from, node.id);
    return;
  }
  if (linking.value) {
    if (!linking.value.from) { linking.value.from = node.id; return; }
    if (node.id === linking.value.from) return;
    const from = linking.value.from;
    linking.value = null;
    connect(from, node.id, canvas.value ? local(event) : null);
    return;
  }
  // Phones: the first tap selects and opens the panel; a selected node can then be dragged.
  pick({kind: 'node', id: node.id});
}
function edgeClick({edge}) {
  if (linking.value || pathPick.value) return;
  pick(selectionOfEdge(edge));
}
function paneClick() {
  if (linking.value) { linking.value = null; return; }
  if (pathPick.value) { pathPick.value = null; return; }
  stopPath();
  pick(null);
}
// While a node is dragged it lines up with the others: near another's edge or centre it snaps there
// (a few screen pixels, whatever the zoom) and a guide shows what it lines up with. Touch drags too.
const guides = ref([]);
function snapped(n) {
  const [w0, h0] = nodeSize(), size = id => { const d = flow.findNode(id)?.dimensions; return {w: d?.width || w0, h: d?.height || h0}; };
  const others = ids.value.filter(id => id !== n.id).map(id => { const p = posOf(id); return p && {x: p.x, y: p.y, ...size(id)}; }).filter(Boolean);
  const r = alignTo({x: n.position.x, y: n.position.y, ...size(n.id)}, others, SNAP_PX / (flow.viewport.value?.zoom || 1));
  if (JSON.stringify(r.guides) !== JSON.stringify(guides.value)) guides.value = r.guides;
  return {x: r.x, y: r.y};
}
function nodeDragStart() { dragging.value = true; clearTimeout(hoverTimer); }
// Pointing at a node (desktop, choosing): a short pause first, so passing over nodes does not flicker the board.
let hoverTimer;
function nodeEnter({node}) {
  if (props.phone) return;
  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(() => { if (!dragging.value && tool.value === 'select' && !linking.value && !pathPick.value && !dragLine.value) hoverNode.value = node.id; }, 90);
}
function nodeLeave() {
  if (props.phone) return;
  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(() => { if (!dragging.value) hoverNode.value = ''; }, 90);
}
function nodeDrag(e) {
  const list = e.nodes || [e.node];
  for (const n of list) {
    const p = list.length === 1 ? snapped(n) : {x: n.position.x, y: n.position.y};
    // The board shows where it snapped at once, not only after the next pointer move.
    if (p.x !== n.position.x || p.y !== n.position.y) n.position = {...p};
    if (props.phone) phoneMoved[n.id] = p; else place(n.id, p);
  }
}
function nodeDragStop(e) { nodeDrag(e); if (guides.value.length) guides.value = []; dragging.value = false; if (!props.phone) saveLayout(); }
// Guides in the canvas's own pixels, a little past the boxes they join.
const guideLines = computed(() => {
  const v = flow.viewport.value || {x: 0, y: 0, zoom: 1}, sx = x => x * v.zoom + v.x, sy = y => y * v.zoom + v.y;
  return guides.value.map(g => g.axis === 'x' ? {x1: sx(g.at), x2: sx(g.at), y1: sy(g.from) - 12, y2: sy(g.to) + 12} : {x1: sx(g.from) - 12, x2: sx(g.to) + 12, y1: sy(g.at), y2: sy(g.at)});
});
function initialized() {
  if (fitted) return;
  fitted = true;
  void nextTick(() => fit(0));
}
// Phones never zoom out below readable text: what does not fit is reached by moving the board,
// starting from the top (relays) and centred across.
function fit(duration = 250) {
  if (!props.phone) return flow.fitView({padding: 0.25, maxZoom: 1, duration});
  const box = canvas.value?.getBoundingClientRect(), places = ids.value.map(posOf).filter(Boolean);
  if (!box || !places.length) return flow.fitView({padding: 0.18, minZoom: PHONE_MIN_ZOOM, maxZoom: 1, duration});
  const left = Math.min(...places.map(p => p.x)), right = Math.max(...places.map(p => p.x)) + PHONE_W;
  const top = Math.min(...places.map(p => p.y)), bottom = Math.max(...places.map(p => p.y)) + PHONE_H;
  const room = {w: box.width - 24, h: box.height - 140};
  const zoom = Math.min(1, Math.max(PHONE_MIN_ZOOM, Math.min(room.w / (right - left), room.h / (bottom - top))));
  const x = (box.width - (right - left) * zoom) / 2 - left * zoom;
  const tall = (bottom - top) * zoom > room.h, y = tall ? 64 - top * zoom : 64 + (room.h - (bottom - top) * zoom) / 2 - top * zoom;
  return flow.setViewport({x, y, zoom}, {duration});
}
const zoom = computed(() => Math.round((flow.viewport.value?.zoom || 1) * 100));
// The dot grid moves and scales with the board.
const grid = computed(() => { const v = flow.viewport.value || {x: 0, y: 0, zoom: 1}, size = 22 * v.zoom; return {backgroundSize: `${size}px ${size}px`, backgroundPosition: `${v.x}px ${v.y}px`}; });

// ---- adding servers ----
function dragOver(e) {
  if (![...(e.dataTransfer?.types || [])].includes(DRAG_TYPE)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
  dropping.value = true;
}
function drop(e) {
  dropping.value = false;
  const id = e.dataTransfer?.getData(DRAG_TYPE);
  if (!id) return;
  e.preventDefault();
  const p = flow.screenToFlowCoordinate({x: e.clientX, y: e.clientY});
  void join(id, {x: p.x - NODE_W / 2, y: p.y - NODE_H / 2}, local(e));
}
function slotFor() {
  const here = Object.fromEntries(ids.value.filter(x => layout.positions[x]).map(x => [x, layout.positions[x]]));
  return nextSlot(here, ids.value.filter(x => !relayId(x)), false);
}
async function join(id, position = slotFor(), at = null) {
  const row = data.value?.eligible?.find(e => e.serverId === id);
  if (!row || pending[id]) return;
  if (!row.ok) { void notify(`${row.name} 不能加入：${row.reason || '原因未知'}`, at); return; }
  pending[id] = {serverId: id, name: row.name, startedAt: Date.now()};
  // Phones place it in their own layout; the desktop gives it the next free place there.
  if (!props.phone) { place(id, position); saveLayout(0); }
  sheet.value = '';
  if (props.phone) void nextTick(() => center(id));
  const result = await install([id]);
  delete pending[id];
  const item = result?.items?.find(i => i.serverId === id);
  if (!result || (item && !item.ok)) {
    if (!props.phone) { place(id, null); saveLayout(0); }
    const why = item ? item.reason || meshErrorText(item.error) || '原因未知' : mesh.error || '原因未知';
    void notify(`${row.name} 没有加入：${why}`, at);
    return;
  }
  if (!sel.value || props.phone) pick({kind: 'node', id}, {sheetOpen: false});
  ElMessage.success(`${row.name} 已加入草稿，应用后连入网格`);
}

// ---- deleting ----
function removeEdge(id) {
  const w = work.value, [kind, ...rest] = id.split(':'), key = rest.join(':');
  if (kind === 'entry') {
    const [memberId, candId] = key.split(':'), m = members.value.find(x => x.serverId === memberId), i = m?.entry?.candidates?.findIndex(c => c.id === candId) ?? -1;
    if (i >= 0) m.entry.candidates.splice(i, 1);
  } else if (kind === 'relay') {
    customRelayLinks(w, key.split('~'));
    ElMessage.info('已改为逐对设置中转连接，并删掉了这一对');
  } else if (kind === 'pair') w.relayLinks.pairs.splice(Number(key), 1);
  else if (kind === 'direct') w.directLinks.splice(Number(key), 1);
  void save();
}
async function removeSelected() {
  const s = sel.value;
  if (!s) return;
  if (s.kind === 'edge') { removeEdge(s.id); pick(null); return; }
  if (s.kind === 'rule') { const i = work.value.rules.findIndex(r => r.id === s.id); if (i >= 0) work.value.rules.splice(i, 1); void save(); pick(null); return; }
  const m = members.value.find(x => x.serverId === s.id);
  if (m && !m.removing && await removeFromMesh(m)) pick(null);
}

function keydown(e) {
  if (!props.active || props.phone || e.defaultPrevented) return;
  // Typing keeps its keys; a checkbox or radio does not.
  const t = e.target;
  if (t?.closest?.('textarea, select, [contenteditable="true"], .el-overlay, .el-message-box')) return;
  if (t?.tagName === 'INPUT' && !['checkbox', 'radio', 'button'].includes(t.type)) return;
  if (document.querySelector('.el-overlay-message-box, .el-message-box')) return;
  if (e.key === 'Escape') {
    if (dragLine.value) portCancel();
    else if (linking.value) stopLinking();
    else if (pathPick.value || path.value) stopPath();
    else if (sel.value) pick(null);
    else if (tool.value !== 'select') tool.value = 'select';
    return;
  }
  if ((e.key === 'Delete' || e.key === 'Backspace') && sel.value) { e.preventDefault(); void removeSelected(); }
}

const linkingText = computed(() => {
  const l = linking.value;
  if (!l) return '';
  const what = l.tool && l.tool !== 'select' ? linkKindLabel(l.tool) : '连线';
  return l.from ? `${what}：点另一台服务器，把 ${nameOf(members.value, l.from)} 连过去` : `${what}：先点起点服务器`;
});
const pathPickText = computed(() => pathPick.value?.from ? `查看路径：点目标服务器，看 ${nameOf(members.value, pathPick.value.from)} 怎么到它` : '查看路径：先点起点服务器');
const toolHint = computed(() => TOOLS.find(([key]) => key === tool.value)?.[2] || '');
watch(tool, () => { dragLine.value = null; linking.value = null; pathPick.value = null; });
// A path whose ends left the mesh goes away; hidden tabs stop asking.
watch(() => Boolean(path.value) && !(ids.value.includes(path.value.src) && ids.value.includes(path.value.dst)), gone => { if (gone) stopPath(); });
watch(() => [props.focus?.nonce, Boolean(data.value)], () => {
  const id = props.focus?.serverId;
  if (id && data.value && members.value.some(m => m.serverId === id)) void nextTick(() => select({kind: 'node', id}));
}, {immediate: true});
// A selection whose object went away (discarded, deleted elsewhere) is dropped.
watch(() => [sel.value, work.value], () => {
  const s = sel.value;
  if (!s || !work.value) return;
  const gone = s.kind === 'node' ? !ids.value.includes(s.id) : s.kind === 'rule' ? !work.value.rules.some(r => r.id === s.id) : !edges.value.some(e => e.id === s.id);
  if (gone) sel.value = null;
});
// Older controllers leave the direct state out of GET /mesh: read those ends' member detail while the board is shown.
const punchEnds = computed(() => [...new Set((work.value?.directLinks || []).filter(isPunched).flatMap(p => [p.a, p.b]))]
  .filter(id => members.value.some(m => m.serverId === id) && !summaryPeers(id)).sort().join(','));
const peerPoll = usePoll(() => loadPeers(punchEnds.value.split(',')), {interval: () => punchEnds.value ? 8000 : 0});
watch(punchEnds, () => void peerPoll.run(), {immediate: true});
watch(() => installing.value.length, n => { clearInterval(clock); if (n) clock = setInterval(() => { now.value = Date.now(); }, 1000); }, {immediate: true});
onMounted(() => window.addEventListener('keydown', keydown));
onBeforeUnmount(() => { window.removeEventListener('keydown', keydown); portCancel(); clearInterval(clock); clearTimeout(noticeTimer); clearTimeout(flashTimer); clearTimeout(hoverTimer); stopPath(); });
defineExpose({select, join, fit});
</script>
<template>
  <div class="mesh-board" :class="{'is-phone': phone, 'is-palette-folded': !phone && folded.palette, 'is-panel-folded': !phone && folded.panel}">
    <!-- Folded: a rail with the way back and the tools, so connecting still works. -->
    <aside v-if="!phone && folded.palette" class="mesh-palette mesh-rail" aria-label="组件">
      <button type="button" class="mesh-tb" aria-label="展开服务器列表" title="展开服务器列表" aria-expanded="false" @click="folded.palette = false"><PanelLeftOpen :size="16" /></button>
      <i class="mesh-rail-sep" aria-hidden="true"></i>
      <div class="mesh-rail-tools" role="radiogroup" aria-label="连接工具">
        <button v-for="[key, label, hint] in TOOLS" :key="key" type="button" role="radio" class="mesh-tb" :aria-checked="tool === key" :aria-label="label" :title="`${label}：${hint}`" @click="tool = key">
          <component :is="TOOL_ICONS[key]" :size="16" aria-hidden="true" />
        </button>
      </div>
    </aside>
    <aside v-else-if="!phone" class="mesh-palette" aria-label="服务器和组件">
      <MeshServers :items="items" :installing="pending" :selected="sel?.kind === 'node' ? sel.id : ''" @select="id => select({kind: 'node', id})" @add="id => join(id)">
        <template #aside>
          <button type="button" class="mesh-tb" aria-label="收起服务器列表" title="收起服务器列表" aria-expanded="true" @click="folded.palette = true"><PanelLeftClose :size="16" /></button>
        </template>
      </MeshServers>
      <section class="mesh-tools" aria-label="组件">
        <h3>组件</h3>
        <div class="mesh-tool-list" role="radiogroup" aria-label="连接工具">
          <button v-for="[key, label, hint] in TOOLS" :key="key" type="button" role="radio" class="mesh-tool" :aria-checked="tool === key" :title="hint" @click="tool = key">
            <component :is="TOOL_ICONS[key]" :size="15" aria-hidden="true" /><span>{{ label }}</span>
          </button>
        </div>
        <p class="instance-muted mesh-tool-hint">{{ toolHint }}</p>
      </section>
    </aside>

    <section class="mesh-stage" aria-label="画板">
      <header class="mesh-stage-head">
        <p class="mesh-summary">
          <span class="mono">{{ data.settings?.prefix }}</span>
          <span>{{ members.length }} 台成员</span>
          <span>{{ members.filter(isRelay).length }} 台中转</span>
          <span v-if="members.length || installing.length" class="ob-dot" :data-tone="overall[0]">{{ overall[1] }}</span>
        </p>
        <div v-if="!phone" class="mesh-toolbar" role="toolbar" aria-label="画板工具">
          <span v-if="layout.unavailable" class="instance-muted mesh-layout-note" title="主控还不能保存布局">位置只在本页保留</span>
          <button type="button" class="mesh-tb" aria-label="缩小" title="缩小" @click="flow.zoomOut({duration: 150})"><Minus :size="15" /></button>
          <span class="mesh-tb-zoom" aria-live="off">{{ zoom }}%</span>
          <button type="button" class="mesh-tb" aria-label="放大" title="放大" @click="flow.zoomIn({duration: 150})"><Plus :size="15" /></button>
          <button type="button" class="mesh-tb" aria-label="适应画面" title="适应画面" @click="fit()"><Maximize :size="15" /></button>
          <i class="mesh-tb-sep" aria-hidden="true"></i>
          <button type="button" class="mesh-tb is-text" :aria-pressed="showRules" title="在画板上显示访问规则" @click="showRules = !showRules"><ShieldCheck :size="15" /><span>访问规则</span></button>
          <button type="button" class="mesh-tb" :aria-label="folded.panel ? '展开属性面板' : '收起属性面板'" :title="folded.panel ? '展开属性面板' : '收起属性面板，画板更大'" :aria-expanded="!folded.panel"
            @click="folded.panel = !folded.panel"><PanelRightOpen v-if="folded.panel" :size="15" /><PanelRightClose v-else :size="15" /></button>
        </div>
      </header>

      <div ref="canvas" class="mesh-canvas" :style="grid" :class="{'is-dropping': dropping, 'is-tool': tool !== 'select' || linking}" @pointerdown.capture="portDown" @dragover="dragOver" @dragleave="dropping = false" @drop="drop">
        <VueFlow id="openbear-mesh" :nodes="nodes" :edges="edges" :min-zoom="0.25" :max-zoom="2" :elements-selectable="false" :nodes-connectable="false" :delete-key-code="null"
          :selection-key-code="null" :multi-selection-key-code="null" :zoom-on-double-click="false" :node-drag-threshold="phone ? 6 : 2" :pan-on-drag="true" :zoom-on-pinch="true"
          @node-click="nodeClick" @edge-click="edgeClick" @pane-click="paneClick" @node-mouse-enter="nodeEnter" @node-mouse-leave="nodeLeave" @node-drag-start="nodeDragStart" @node-drag="nodeDrag" @node-drag-stop="nodeDragStop" @nodes-initialized="initialized">
          <template #node-mesh="p"><MeshNode :id="p.id" :data="p.data" /></template>
          <template #edge-mesh="p"><MeshEdge :id="p.id" :source-node="p.sourceNode" :target-node="p.targetNode" :data="p.data" /></template>
        </VueFlow>
        <svg v-if="guideLines.length" class="mesh-guides" aria-hidden="true">
          <line v-for="(g, i) in guideLines" :key="i" :x1="g.x1" :y1="g.y1" :x2="g.x2" :y2="g.y2" />
        </svg>
        <svg v-if="dragLine" class="mesh-drag-line" :data-bad="dragLine.plan?.error ? true : undefined" aria-hidden="true">
          <line :x1="dragLine.x1" :y1="dragLine.y1" :x2="dragLine.x2" :y2="dragLine.y2" />
        </svg>
        <p v-if="dragLine?.plan" class="mesh-drag-tip" :data-tone="dragLine.plan.error ? 'bad' : 'ok'" :data-at="dragLine.tip ? (dragLine.tip.above ? 'above' : 'below') : undefined"
          :style="dragLine.tip ? {left: dragLine.tip.x + 'px', top: dragLine.tip.y + 'px'} : {left: dragLine.x2 + 'px', top: dragLine.y2 + 'px'}">
          {{ dragLine.plan.error || `松开添加${linkKindLabel(dragLine.plan.kind)}` }}
        </p>

        <div v-if="!members.length && !installing.length" class="mesh-canvas-empty">
          <p>{{ phone ? '还没有组网。点右下角的＋添加服务器，至少两台。' : '还没有组网。把左侧的服务器拖到这里，至少两台。' }}</p>
        </div>
        <p v-if="linking" class="mesh-banner" role="status">{{ linkingText }}<button type="button" class="ix-text-button" @click="stopLinking">取消</button></p>
        <p v-else-if="pathPick" class="mesh-banner" role="status">{{ pathPickText }}<button type="button" class="ix-text-button" @click="stopPath">取消</button></p>
        <p v-else-if="!phone && tool !== 'select'" class="mesh-banner" role="status">{{ toolHint }}<button type="button" class="ix-text-button" @click="tool = 'select'">完成（Esc）</button></p>
        <Transition name="mesh-fade">
          <p v-if="notice" ref="noticeEl" class="mesh-notice" :data-tone="notice.tone" role="status" :style="{left: notice.x + 'px', top: notice.y + 'px', visibility: notice.placed ? 'visible' : 'hidden'}">{{ notice.text }}</p>
        </Transition>
        <section v-if="path" class="mesh-path" aria-label="路径">
          <header class="mesh-path-head">
            <h3>{{ nameOf(members, path.src) }} 到 {{ path.subnet || nameOf(members, path.dst) }} 的路径</h3>
            <button type="button" class="mesh-path-close" aria-label="关闭路径" title="关闭（Esc）" @click="stopPath"><X :size="14" /></button>
          </header>
          <p v-if="path.unsupported" class="instance-muted">主控还不支持查看路径，升级主控后可用。</p>
          <p v-else-if="path.loading" class="instance-muted" role="status">正在读取…</p>
          <p v-else-if="!path.hops.length" class="instance-muted">{{ path.error ? `读不到路径：${path.error}` : '现在没有可用的路径。' }}</p>
          <template v-else>
            <ol class="mesh-path-hops">
              <li v-for="(h, i) in path.hops" :key="i + h.node">
                <span v-if="i" class="mesh-path-via">{{ [HOP_VIA[h.via] || h.via || '经', h.rttMs !== null ? msText(h.rttMs) : ''].filter(Boolean).join(' ') }}<span aria-hidden="true"> →</span></span>
                <button type="button" class="mesh-path-node" @click="select({kind: 'node', id: h.node})">{{ nameOf(members, h.node) }}</button>
              </li>
            </ol>
            <p v-if="pathBroken" class="mesh-path-broken">在 {{ nameOf(members, path.hops.at(-1).node) }} 之后断开，现在到不了 {{ nameOf(members, path.dst) }}</p>
            <p v-else class="mesh-path-total"><strong>{{ pathTotal }}</strong><span v-if="path.stale" class="ob-warn">数据不完整：有的线路没有上报，路径和延迟可能不准</span></p>
            <p v-if="path.error" class="instance-muted">暂时读不到最新路径（{{ path.error }}），稍后重试。</p>
          </template>
        </section>
        <!-- The panel folded away: what is selected, in short, with the way back to the panel. -->
        <section v-if="peek" class="mesh-peek" aria-label="选中的对象">
          <header class="mesh-peek-head">
            <span v-if="peek.flag" class="sv-flag" aria-hidden="true">{{ peek.flag }}</span>
            <h3 :title="peek.title">{{ peek.title }}</h3>
            <button type="button" class="mesh-path-close" aria-label="取消选择" title="取消选择（Esc）" @click="pick(null)"><X :size="14" /></button>
          </header>
          <p v-for="(r, i) in peek.rows" :key="i" class="mesh-peek-row" :class="{'instance-muted': r.muted}">
            <span v-if="r.dot" class="ob-dot" :data-tone="r.tone || 'off'">{{ r.text }}</span><span v-else :class="r.tone ? `ob-${r.tone === 'bad' ? 'bad' : 'warn'}` : ''">{{ r.text }}</span>
            <span v-if="r.extra" class="instance-muted">{{ r.extra }}</span>
          </p>
          <button type="button" class="ix-text-button mesh-peek-open" @click="folded.panel = false"><PanelRightOpen :size="14" aria-hidden="true" />展开面板查看详情</button>
        </section>
        <section v-if="legend.length || (hasQuiet && !phone)" class="mesh-legend" aria-label="图例">
          <p v-if="hasQuiet && !phone" class="mesh-legend-note"><i class="mesh-legend-quiet" aria-hidden="true"></i>细淡线：备用或暂时没走流量的线路</p>
          <h3 v-if="legend.length">不画成连线的规则</h3>
          <ul v-if="legend.length" aria-label="不画成连线的访问规则">
            <li v-for="l in legend" :key="l.id">
              <button type="button" :data-selected="(sel?.kind === 'rule' && sel.id === l.id) || undefined" :data-bad="l.bad || undefined" :title="l.text" :aria-label="`访问规则 ${l.index + 1}：${l.text}`"
                @click="pick({kind: 'rule', id: l.id})" @mouseenter="ruleHover.id = l.id" @mouseleave="ruleHover.id = ''"><span class="instance-muted">{{ l.index + 1 }}</span>{{ l.text }}</button>
            </li>
          </ul>
        </section>

        <template v-if="phone">
          <div class="mesh-fab-row" role="toolbar" aria-label="画板工具">
            <button type="button" class="mesh-tb" aria-label="缩小" @click="flow.zoomOut({duration: 150})"><Minus :size="16" /></button>
            <button type="button" class="mesh-tb" aria-label="放大" @click="flow.zoomIn({duration: 150})"><Plus :size="16" /></button>
            <button type="button" class="mesh-tb" aria-label="适应画面" @click="fit()"><Maximize :size="16" /></button>
            <button type="button" class="mesh-tb" aria-label="访问规则图层" :aria-pressed="showRules" @click="showRules = !showRules"><ShieldCheck :size="16" /></button>
            <button type="button" class="mesh-tb" aria-label="连接" :aria-pressed="Boolean(linking)" @click="linking ? stopLinking() : startLinking(sel?.kind === 'node' ? sel.id : '')"><Cable :size="16" /></button>
          </div>
          <button type="button" class="mesh-fab" aria-label="添加服务器" @click="sheet = 'add'"><Plus :size="20" /></button>
          <div class="mesh-dock">
            <template v-if="sel && !linking && !pathPick">
              <span class="mesh-dock-title">{{ selectionTitle(sel, work, installing) }}<small v-if="sel.kind === 'node'" class="instance-muted">已选中，可拖动</small></span>
              <button type="button" class="ix-text-button" @click="sheet = 'inspect'">属性</button>
              <template v-if="sel.kind === 'node' && members.some(m => m.serverId === sel.id)">
                <button type="button" class="ix-text-button" @click="startLinking(sel.id)">连接到…</button>
                <button type="button" class="ix-text-button" @click="startPath(sel.id)">路径…</button>
              </template>
              <button type="button" class="mesh-dock-close" aria-label="取消选择" @click="pick(null)"><X :size="16" /></button>
            </template>
            <button v-else-if="!linking && !pathPick" type="button" class="mesh-dock-entry" @click="sheet = 'overview'">网格设置与访问规则<span aria-hidden="true">›</span></button>
          </div>
        </template>
      </div>
      <!-- Under the canvas: it grows and shrinks from the bottom, so the nodes never jump. -->
      <MeshChanges @apply="emit('apply')" @member="id => select({kind: 'node', id})" />
      <p v-if="mesh.error" role="alert" class="instance-error mesh-error">{{ mesh.error }}</p>
      <p v-if="mesh.loadError" role="status" class="instance-muted mesh-error">暂时读不到最新状态（{{ mesh.loadError }}），稍后自动重试。</p>
    </section>

    <aside v-if="!phone && !folded.panel" class="mesh-inspector-col" aria-label="属性">
      <MeshInspector :sel="sel" :shown-path="pathKey" :items="items" :installing="installing" @select="select" @open-server="emit('open-server', $event)" @connect="startLinking" @path="startPath" @show-path="showRoute" @remove="removeSelected" />
    </aside>

    <MeshSheet v-if="phone" :model-value="Boolean(sheet)" :title="sheetTitle" :phone-size="sheet === 'add' ? 'auto' : '86%'" :panel-class="`mesh-${sheet || 'inspect'}-sheet`" @update:model-value="v => { if (!v) sheet = ''; }">
      <MeshServers v-if="sheet === 'add'" :items="items" phone :installing="pending" @add="id => join(id)" @select="id => select({kind: 'node', id})" />
      <MeshInspector v-else :sel="sheet === 'overview' ? null : sel" phone :shown-path="pathKey" :items="items" :installing="installing" @select="select" @open-server="emit('open-server', $event)" @connect="startLinking" @path="startPath" @show-path="showRoute" @remove="removeSelected" />
    </MeshSheet>
  </div>
</template>
