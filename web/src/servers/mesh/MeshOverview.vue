<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { ArrowLeftRight, Maximize, Minus, Plus, X } from '@lucide/vue';
import { ensureFlagFont, flag, regionName } from '../serverHelpers.js';
import { MeshApi } from './meshApi.js';
import { mesh, meshError } from './meshStore.js';
import { isRelay, isSite, msText, nameOf, nodeState, transportLabel } from './meshHelpers.js';
import { canPinRoute, fixedRouteInfo } from './meshAuto.js';
import { containerOf } from './meshGroups.js';
import { usePoll } from './usePoll.js';
import MeshNodeSelect from './MeshNodeSelect.vue';

// The overview: relays as cards gathered by country, each member drawn under the relay it enters by when
// that relay is open (several may be), laid out from real sizes so nothing overlaps; the lines between
// relays coloured by round trip, and the route between any two machines.
const props = defineProps({phone: Boolean, active: Boolean, items: {type: Array, default: () => []}});
const emit = defineEmits(['open-machine', 'join', 'connect', 'pin-path']);

const CARD_W = 204, CHIP_H = 26, CHIP_GAP = 4, COL_GAP = 46, ZPAD = 16, ZHEAD = 32, ZGAP = 64, ROW_MAX = 2000, CARD_SPACE = 22;
const VIEW_KEY = 'openbear.mesh.overview';
const saved = (() => { try { return JSON.parse(localStorage.getItem(VIEW_KEY) || '{}') || {}; } catch { return {}; } })();
const layers = reactive({lines: saved.layers?.lines !== false, rtt: saved.layers?.rtt !== false, direct: saved.layers?.direct === true});
const open = reactive(new Set(Array.isArray(saved.open) ? saved.open : []));
const zoff = reactive(saved.zoff && typeof saved.zoff === 'object' ? saved.zoff : {});
watch(() => [JSON.stringify(layers), [...open].join(','), JSON.stringify(zoff)], () => {
  try { localStorage.setItem(VIEW_KEY, JSON.stringify({layers, open: [...open], zoff})); } catch { /* private mode */ }
});

const view = reactive({x: 0, y: 0, z: 1}), board = ref(null), size = reactive({w: 1200, h: 700});
const focusId = ref(''), hoverId = ref(''), linking = ref(''), search = ref('');
const route = reactive({from: '', to: '', picking: false});
let fitted = false;

const data = computed(() => mesh.data);
const work = computed(() => mesh.work);
const live = computed(() => (work.value?.members || []).filter(m => !m.removing));
const byId = computed(() => new Map(live.value.map(m => [m.serverId, m])));
const itemRegions = computed(() => new Map(props.items.map(i => [i.id, i.region || ''])));
const regionOf = id => String(byId.value.get(id)?.region || itemRegions.value.get(id) || '').toUpperCase();
const nm = id => nameOf(live.value, id);
const relaySet = computed(() => new Set(live.value.filter(isRelay).map(m => m.serverId)));
const fresh = id => !data.value?.nodes?.[id]?.appliedRevision;
// The relay each member is drawn under: in upstreams mode its first upstream on the board;
// otherwise the one it enters by now, else its first candidate.
const homes = computed(() => {
  const out = new Map();
  for (const m of live.value) {
    const c = containerOf(m, relaySet.value, m.entry?.mode === 'upstreams' ? null : data.value?.nodes?.[m.serverId]?.summary?.entry);
    if (c) out.set(m.serverId, c.relay);
  }
  return out;
});
const ridersOf = computed(() => {
  const out = new Map();
  for (const [m, r] of homes.value) (out.get(r) || out.set(r, []).get(r)).push(m);
  for (const list of out.values()) list.sort((a, b) => Number(fresh(b)) - Number(fresh(a)) || nm(a).localeCompare(nm(b), 'zh'));
  return out;
});
const riders = id => ridersOf.value.get(id) || [];

// ---- lines ----
const pk = (a, b) => a < b ? `${a}|${b}` : `${b}|${a}`;
const lines = computed(() => data.value?.lines || []);
const lineAt = computed(() => new Map(lines.value.map(l => [`${l.kind}:${pk(l.a, l.b)}`, l])));
const lineOf = (kind, a, b) => lineAt.value.get(`${kind}:${pk(a, b)}`) || null;
const listedPairs = computed(() => new Map((work.value?.relayLinks?.pairs || []).map(p => [pk(p.a, p.b), p])));
// The relay links as applied (with their measured round trips), and those only in the draft so far.
const relayEdges = computed(() => {
  const out = new Map(), relays = relaySet.value;
  for (const l of lines.value) if (l.kind === 'relay' && relays.has(l.a) && relays.has(l.b)) out.set(pk(l.a, l.b), {a: l.a, b: l.b, transport: l.transport, rtt: l.rttMs, state: l.state, draft: false});
  const ids = [...relays], full = (work.value?.relayLinks?.mode || 'full') === 'full';
  const drafts = full ? ids.flatMap((a, i) => ids.slice(i + 1).filter(b => fresh(a) || fresh(b)).map(b => ({a, b, transport: 'udp'}))) : [...listedPairs.value.values()];
  for (const p of drafts) if (relays.has(p.a) && relays.has(p.b) && !out.has(pk(p.a, p.b))) out.set(pk(p.a, p.b), {a: p.a, b: p.b, transport: p.transport, rtt: null, draft: true});
  for (const [k, e] of out) e.bias = listedPairs.value.get(k)?.bias || 0;
  return [...out.values()];
});
const directEdges = computed(() => {
  const out = new Map();
  for (const l of lines.value) if (l.kind === 'direct') out.set(pk(l.a, l.b), {a: l.a, b: l.b, transport: l.transport, rtt: l.rttMs});
  for (const d of work.value?.directLinks || []) if (!out.has(pk(d.a, d.b))) out.set(pk(d.a, d.b), {a: d.a, b: d.b, transport: d.transport, rtt: null, draft: true});
  return [...out.values()].filter(e => byId.value.has(e.a) && byId.value.has(e.b));
});
const entryLine = (member, relay) => lineOf('entry', member, relay);
const band = ms => !Number.isFinite(ms) ? 'unk' : ms < 50 ? 'good' : ms < 130 ? 'mid' : 'far';

// ---- cards ----
function funcs(m) {
  const out = [];
  const nets = m.gateway?.subnets || [];
  if (nets.length) out.push(['lan', `${m.gateway.backup ? '备用 ' : '共享 '}${nets[0]}${nets.length > 1 ? ` +${nets.length - 1}` : ''}`]);
  if (m.proxy?.exit) out.push(['exit', '出口']);
  if (m.proxy?.ports?.length) out.push(['ap', `代理 ${m.proxy.ports.map(p => `:${p.port}`).join(' ')}`]);
  return out;
}
const roleText = m => isRelay(m) ? (isSite(m) ? '站点中转' : '中转') : '成员';
const cardH = id => 58 + (funcs(byId.value.get(id)).length ? 24 : 0) + (riders(id).length ? 26 : 0);
// Relays shown open: those opened by hand, and those the route, the selection or a newcomer needs seen.
const openNow = computed(() => {
  const out = new Set(open);
  const need = id => { const r = homes.value.get(id); if (r) out.add(r); };
  for (const h of pathHops.value) need(h.node);
  need(focusId.value);
  for (const m of live.value) if (fresh(m.serverId)) need(m.serverId);
  return out;
});
// Each zone is a country: its relays, and members entering by no relay on the board.
const zoneList = computed(() => {
  const groups = new Map();
  for (const m of live.value) {
    if (!isRelay(m) && homes.value.has(m.serverId)) continue;
    const key = regionOf(m.serverId) || '?';
    (groups.get(key) || groups.set(key, []).get(key)).push(m.serverId);
  }
  return [...groups.entries()].map(([key, ids]) => ({key, ids: ids.sort((a, b) => Number(isRelay(byId.value.get(b))) - Number(isRelay(byId.value.get(a))) || riders(b).length - riders(a).length || nm(a).localeCompare(nm(b), 'zh'))}))
    .sort((a, b) => b.ids.reduce((n, id) => n + 1 + riders(id).length, 0) - a.ids.reduce((n, id) => n + 1 + riders(id).length, 0) || a.key.localeCompare(b.key));
});
const layout = computed(() => {
  const L = {cards: new Map(), chips: new Map(), zones: []}, opened = openNow.value;
  let x = 0, y = 0, rowH = 0;
  for (const {key, ids} of zoneList.value) {
    const cols = props.phone ? 1 : ids.length >= 3 ? 2 : ids.length, colY = Array(cols).fill(0);
    const w = ZPAD * 2 + cols * CARD_W + (cols - 1) * COL_GAP;
    if (!props.phone && x > 0 && x + w > ROW_MAX) { x = 0; y += rowH + ZGAP; rowH = 0; }
    const off = zoff[key] || {dx: 0, dy: 0}, zx = x + off.dx, zy = y + off.dy;
    for (const id of ids) {
      const col = colY.indexOf(Math.min(...colY)), cx = zx + ZPAD + col * (CARD_W + COL_GAP), cy = zy + ZHEAD + colY[col], h = cardH(id);
      L.cards.set(id, {x: cx, y: cy, w: CARD_W, h});
      let bh = h;
      if (opened.has(id)) {
        riders(id).forEach((m, i) => L.chips.set(m, {x: cx + 14, y: cy + h + 6 + i * (CHIP_H + CHIP_GAP), w: CARD_W - 14, h: CHIP_H, relay: id}));
        bh += 6 + riders(id).length * (CHIP_H + CHIP_GAP);
      }
      colY[col] += bh + CARD_SPACE;
    }
    const h = ZHEAD + Math.max(...colY) - CARD_SPACE + ZPAD;
    L.zones.push({key, label: key === '?' ? '地区未知' : regionName(key) || key, flag: key === '?' ? '' : flag(key), x: zx, y: zy, w, h,
      relays: ids.filter(id => isRelay(byId.value.get(id))).length, members: ids.reduce((n, id) => n + riders(id).length + (isRelay(byId.value.get(id)) ? 0 : 1), 0)});
    if (props.phone) { y += h + ZGAP; } else { x += w + ZGAP; rowH = Math.max(rowH, h); }
  }
  const zs = L.zones.length ? L.zones : [{x: 0, y: 0, w: 400, h: 200}];
  L.box = {x0: Math.min(...zs.map(z => z.x)) - 40, y0: Math.min(...zs.map(z => z.y)) - 40, x1: Math.max(...zs.map(z => z.x + z.w)) + 40, y1: Math.max(...zs.map(z => z.y + z.h)) + 40};
  return L;
});
const boxOf = id => layout.value.chips.get(id) || layout.value.cards.get(id) || layout.value.cards.get(homes.value.get(id)) || null;
const anchor = id => { const b = boxOf(id); return b ? {x: b.x + b.w / 2, y: b.y + b.h / 2, b} : null; };
function clip(p, q) {
  const b = p.b, dx = q.x - p.x, dy = q.y - p.y;
  if (!dx && !dy) return {x: p.x, y: p.y};
  const t = Math.min(b.w / 2 / Math.abs(dx || 1e-9), b.h / 2 / Math.abs(dy || 1e-9));
  return {x: p.x + dx * Math.min(t, 1), y: p.y + dy * Math.min(t, 1)};
}

// ---- the route between two machines, as the nodes route it now ----
const pathState = ref(null);
const pathHops = computed(() => route.from && route.to && pathState.value?.hops?.length ? pathState.value.hops : []);
const pathPoll = usePoll(async current => {
  const src = route.from, dst = route.to;
  try {
    const r = await MeshApi.path(src, dst);
    if (!current()) return;
    pathState.value = {hops: (Array.isArray(r?.hops) ? r.hops : []).filter(h => h?.node).map(h => ({node: h.node, via: h.via || '', rttMs: Number.isFinite(h.rttMs) ? h.rttMs : null})),
      rttMs: Number.isFinite(r?.rttMs) ? r.rttMs : null, stale: Boolean(r?.stale), fixed: r?.fixed || null, error: ''};
  } catch (e) {
    if (!current()) return;
    const code = e?.response?.data?.error;
    pathState.value = {hops: [], rttMs: null, error: code === 'mesh_member_not_found' ? '起点或终点还没有应用到网格，应用后才能看路线' : meshError(e)};
  }
}, {interval: () => route.from && route.to ? 5000 : 0, active: computed(() => props.active)});
watch(() => `${route.from}>${route.to}`, () => { pathState.value = null; if (route.from && route.to && route.from !== route.to) void pathPoll.run(); else pathPoll.stop(); });
const segOf = (a, b, via) => {
  const l = via === 'entry' || via === 'lan' ? lineOf('entry', a, b) : via === 'direct' ? lineOf('direct', a, b) : lineOf('relay', a, b) || lineOf('entry', a, b) || lineOf('direct', a, b);
  return l?.transport ? transportLabel(l.transport) : via === 'lan' ? '局域网' : '';
};
const pathSegs = computed(() => pathHops.value.slice(1).map((h, i) => ({from: pathHops.value[i].node, to: h.node, transport: segOf(pathHops.value[i].node, h.node, h.via), rtt: h.rttMs})));
const pathBroken = computed(() => pathHops.value.length > 0 && pathHops.value.at(-1).node !== route.to);
const pathFixed = computed(() => fixedRouteInfo(pathState.value?.fixed));
const canPin = computed(() => !pathBroken.value && canPinRoute(pathState.value, id => relaySet.value.has(id)));
function pinCurrentPath() {
  if (!canPin.value) return;
  emit('pin-path', {src: route.from, dst: route.to, via: pathHops.value.slice(1, -1).map(h => h.node)});
}
const onPath = computed(() => new Set(pathHops.value.map(h => h.node)));

// ---- what is drawn ----
function related(id) {
  const m = byId.value.get(id);
  if (!m) return null;
  const set = new Set([id]);
  if (isRelay(m)) {
    for (const e of relayEdges.value) if (e.a === id || e.b === id) set.add(e.a === id ? e.b : e.a);
    for (const r of riders(id)) set.add(r);
  } else for (const c of m.entry?.candidates || []) set.add(c.relay);
  for (const d of directEdges.value) if (d.a === id || d.b === id) set.add(d.a === id ? d.b : d.a);
  return set;
}
const highlight = computed(() => pathHops.value.length ? onPath.value : related(focusId.value) || (!props.phone ? related(hoverId.value) : null));
const textW = t => [...t].reduce((n, c) => n + (c.charCodeAt(0) > 255 ? 11 : 6.4), 0);
const drawn = computed(() => {
  const rel = highlight.value, touched = focusId.value || (!pathHops.value.length && hoverId.value) || '';
  const pathPairs = new Set(pathSegs.value.map(s => pk(s.from, s.to)));
  const edges = [], labels = [];
  for (const e of relayEdges.value) {
    const a = anchor(e.a), b = anchor(e.b);
    if (!a || !b) continue;
    const touch = touched && (e.a === touched || e.b === touched), onp = pathPairs.has(pk(e.a, e.b));
    if (!layers.lines && !touch && !e.draft && !onp) continue;
    const s = clip(a, b), t = clip(b, a), cls = e.draft ? 'draft' : band(e.rtt);
    edges.push({key: `r:${pk(e.a, e.b)}`, d: `M${s.x},${s.y} L${t.x},${t.y}`, cls: [cls, e.bias > 0 && 'avoid', e.state === 'down' && 'down', touch && 'hi', rel && !(touch || onp) && 'dim'].filter(Boolean).join(' '),
      title: `${nm(e.a)} ⇄ ${nm(e.b)} · ${transportLabel(e.transport)}${Number.isFinite(e.rtt) ? ` · ${msText(e.rtt)}` : e.draft ? ' · 待应用' : ''}${e.bias > 0 ? ' · 少走' : ''}${e.state === 'down' ? ' · 断开' : ''}`});
    if (!onp && Number.isFinite(e.rtt) && (touch || (layers.rtt && !rel))) labels.push({s, t, text: msText(e.rtt), cls: `${cls}${touch ? ' hi' : ''}`, pri: touch ? 0 : 2});
  }
  for (const d of directEdges.value) {
    const touch = touched && (d.a === touched || d.b === touched);
    if (!layers.direct && !touch) continue;
    const a = anchor(d.a), b = anchor(d.b);
    if (!a || !b) continue;
    const s = clip(a, b), t = clip(b, a);
    edges.push({key: `d:${pk(d.a, d.b)}`, d: `M${s.x},${s.y} Q${(s.x + t.x) / 2 + 30},${(s.y + t.y) / 2 - 30} ${t.x},${t.y}`, cls: `direct${rel && !touch && !pathPairs.has(pk(d.a, d.b)) ? ' dim' : ''}`, title: `直连 ${nm(d.a)} ⇄ ${nm(d.b)}`});
  }
  let pathD = '';
  if (pathHops.value.length > 1) {
    const pts = pathHops.value.map(h => anchor(h.node)).filter(Boolean);
    pathD = `M${pts.map(p => `${p.x},${p.y}`).join(' L')}`;
    for (const s of pathSegs.value) {
      const a = anchor(s.from), b = anchor(s.to);
      if (a && b && Math.hypot(a.x - b.x, a.y - b.y) > 60) labels.push({s: clip(a, b), t: clip(b, a), text: `${s.transport} ${Number.isFinite(s.rtt) ? msText(s.rtt) : ''}`.trim(), cls: 'path', pri: -1});
    }
  }
  // Most important first; a label slides along its line to the first spot clear of cards and other labels.
  const taken = [...layout.value.cards.values(), ...layout.value.chips.values()].map(b => ({x0: b.x, y0: b.y, x1: b.x + b.w, y1: b.y + b.h}));
  const hit = r => taken.some(o => r.x0 < o.x1 && r.x1 > o.x0 && r.y0 < o.y1 && r.y1 > o.y0);
  const placed = [];
  for (const l of labels.sort((a, b) => a.pri - b.pri)) {
    if (!l.text) continue;
    const w = textW(l.text) + 12;
    const spots = [.5, .38, .62, .28, .72, .2, .8].map(f => { const x = l.s.x + (l.t.x - l.s.x) * f, y = l.s.y + (l.t.y - l.s.y) * f; return {x, y, r: {x0: x - w / 2, y0: y - 9, x1: x + w / 2, y1: y + 9}}; });
    const free = spots.find(p => !hit(p.r));
    if (!free && l.pri > 0) continue;
    const p = free || spots[0];
    taken.push(p.r);
    placed.push({key: `${placed.length}`, x: p.x, y: p.y, text: l.text, cls: l.cls});
  }
  return {edges, labels: placed, pathD};
});
const cards = computed(() => [...layout.value.cards.entries()].map(([id, b]) => {
  const m = byId.value.get(id), [tone, state] = nodeState(data.value, id);
  return {id, b, m, tone: fresh(id) ? 'warn' : tone, state: fresh(id) ? '待应用' : state, flag: flag(regionOf(id)), funcs: funcs(m), n: riders(id).length, open: openNow.value.has(id), relay: isRelay(m)};
}));
const chips = computed(() => [...layout.value.chips.entries()].map(([id, b]) => {
  const m = byId.value.get(id), [tone] = nodeState(data.value, id), l = entryLine(id, b.relay);
  const cands = m.entry?.candidates || [], c = cands.find(x => x.relay === b.relay);
  const more = m.entry?.mode === 'upstreams' ? cands.filter(x => x.relay !== b.relay && relaySet.value.has(x.relay)).length : 0;
  return {id, b, m, tone: fresh(id) ? 'warn' : tone, flag: flag(regionOf(id)),
    note: fresh(id) ? '待应用' : [transportLabel(l?.transport || c?.transport), Number.isFinite(l?.rttMs) ? msText(l.rttMs) : '', more > 0 ? `+${more}` : ''].filter(Boolean).join(' ')};
}));
const nodeCls = id => ({sel: focusId.value === id || linking.value === id, onpath: onPath.value.has(id), dim: highlight.value && !highlight.value.has(id), draftn: fresh(id)});

// ---- the view ----
const worldStyle = computed(() => ({transform: `translate(${view.x}px, ${view.y}px) scale(${view.z})`}));
const svgBox = computed(() => { const {x0, y0, x1, y1} = layout.value.box; return {x0, y0, w: x1 - x0, h: y1 - y0}; });
function measure() { if (board.value) { size.w = board.value.clientWidth || 1200; size.h = board.value.clientHeight || 700; } }
function fit() {
  measure();
  const {x0, y0, x1, y1} = layout.value.box, bw = x1 - x0, bh = y1 - y0;
  if (props.phone) { const z = Math.min((size.w - 8) / bw, 1.05); Object.assign(view, {z, x: (size.w - bw * z) / 2 - x0 * z, y: 60 - y0 * z}); return; }
  const top = 58, bottom = 30, z = Math.max(.3, Math.min((size.w - 40) / bw, (size.h - top - bottom) / bh, 1.1));
  Object.assign(view, {z, x: (size.w - bw * z) / 2 - x0 * z, y: top + Math.max(0, (size.h - top - bottom - bh * z) / 2) - y0 * z});
}
function zoomAt(f, cx = size.w / 2, cy = size.h / 2) {
  const z = Math.max(.25, Math.min(2.4, view.z * f));
  view.x = cx - (cx - view.x) * z / view.z; view.y = cy - (cy - view.y) * z / view.z; view.z = z;
}
// The selection and what it joins come out from under the card on the right, zooming out if they must.
function reveal(ids) {
  const bs = [...(ids || [])].map(boxOf).filter(Boolean);
  if (!bs.length) return;
  measure();
  const x0 = Math.min(...bs.map(b => b.x)) - 20, x1 = Math.max(...bs.map(b => b.x + b.w)) + 20, y0 = Math.min(...bs.map(b => b.y)) - 40, y1 = Math.max(...bs.map(b => b.y + b.h)) + 20;
  const top = 60, left = 10, w = size.w - (props.phone ? 20 : 350), h = size.h - top - (props.phone ? 280 : 20), v = view;
  if (x0 * v.z + v.x >= left && x1 * v.z + v.x <= left + w && y0 * v.z + v.y >= top && y1 * v.z + v.y <= top + h) return;
  v.z = Math.max(.3, Math.min(v.z, w / (x1 - x0), h / (y1 - y0)));
  v.x = left + (w - (x1 - x0) * v.z) / 2 - x0 * v.z; v.y = top + Math.max(0, (h - (y1 - y0) * v.z) / 2) - y0 * v.z;
}
const mini = computed(() => {
  const {x0, y0, x1, y1} = layout.value.box, s = Math.min(196 / (x1 - x0), 116 / (y1 - y0)), ox = (196 - (x1 - x0) * s) / 2, oy = (116 - (y1 - y0) * s) / 2;
  const f = (x, y) => ({x: (x - x0) * s + ox, y: (y - y0) * s + oy});
  const a = f(-view.x / view.z, -view.y / view.z), b = f((size.w - view.x) / view.z, (size.h - view.y) / view.z);
  return {s, ox, oy, x0, y0, zones: layout.value.zones.map(z => ({key: z.key, ...f(z.x, z.y), w: z.w * s, h: z.h * s})),
    cards: [...layout.value.cards.entries()].map(([id, c]) => ({id, ...f(c.x, c.y), w: c.w * s, h: c.h * s})),
    view: {x: Math.max(1, a.x), y: Math.max(1, a.y), w: Math.max(6, Math.min(194, b.x) - Math.max(1, a.x)), h: Math.max(6, Math.min(114, b.y) - Math.max(1, a.y))}};
});
function miniClick(e) {
  const r = e.currentTarget.getBoundingClientRect(), m = mini.value;
  const wx = ((e.clientX - r.left) * 196 / r.width - m.ox) / m.s + m.x0, wy = ((e.clientY - r.top) * 116 / r.height - m.oy) / m.s + m.y0;
  view.x = size.w / 2 - wx * view.z; view.y = size.h / 2 - wy * view.z;
}

// ---- pointer: pan, zone drag, pinch, wheel ----
const pointers = new Map();
let drag = null, pinch = null;
function down(e) {
  if (e.target.closest('[data-node], button, select, input, .m3-float')) return;
  pointers.set(e.pointerId, {x: e.clientX, y: e.clientY});
  board.value?.setPointerCapture?.(e.pointerId);
  if (pointers.size === 2) {
    const [p, q] = [...pointers.values()];
    pinch = {d: Math.hypot(p.x - q.x, p.y - q.y), z: view.z, cx: (p.x + q.x) / 2, cy: (p.y + q.y) / 2};
    drag = null;
    return;
  }
  const zone = e.target.closest('[data-zone]')?.dataset.zone;
  drag = {x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false, zone, off: zone ? {...(zoff[zone] || {dx: 0, dy: 0})} : null};
}
function move(e) {
  if (!pointers.has(e.pointerId)) return;
  pointers.set(e.pointerId, {x: e.clientX, y: e.clientY});
  if (pinch && pointers.size === 2) {
    const [p, q] = [...pointers.values()], r = board.value.getBoundingClientRect();
    zoomAt(pinch.z * Math.hypot(p.x - q.x, p.y - q.y) / pinch.d / view.z, pinch.cx - r.left, pinch.cy - r.top);
    return;
  }
  if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
  if (drag.zone) zoff[drag.zone] = {dx: drag.off.dx + dx / view.z, dy: drag.off.dy + dy / view.z};
  else { view.x = drag.vx + dx; view.y = drag.vy + dy; }
}
function up(e) {
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinch = null;
  if (!drag) return;
  const d = drag;
  drag = null;
  if (!d.moved && !d.zone) { focusId.value = ''; linking.value = ''; route.picking = false; }
}
function wheel(e) { const r = board.value.getBoundingClientRect(); zoomAt(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX - r.left, e.clientY - r.top); }

// ---- selection ----
function nodeClick(id) {
  if (linking.value) { const from = linking.value; linking.value = ''; if (from !== id) emit('connect', {from, to: id}); return; }
  if (route.picking && route.from && id !== route.from) { route.to = id; route.picking = false; return; }
  focusId.value = id; hoverId.value = '';
  void nextTick(() => reveal(related(id)));
}
function toggle(id) { if (open.has(id)) open.delete(id); else open.add(id); }
const allOpen = computed(() => [...relaySet.value].every(r => !riders(r).length || open.has(r)));
function toggleAll() {
  if (allOpen.value) open.clear(); else for (const r of relaySet.value) if (riders(r).length) open.add(r);
}
function arrange() { for (const k of Object.keys(zoff)) delete zoff[k]; void nextTick(fit); }
function pathFrom(id) { route.from = id; route.to = ''; route.picking = true; focusId.value = ''; }
function pathTo(id) { if (!route.from || route.from === id) { route.from = id; route.picking = true; } else route.to = id; focusId.value = ''; }
function swap() { [route.from, route.to] = [route.to, route.from]; }
function clearPath() { route.from = ''; route.to = ''; route.picking = false; }
function find() {
  const q = search.value.trim();
  const m = live.value.find(x => x.name === q) || live.value.find(x => x.name.includes(q));
  if (m) { focusId.value = m.serverId; void nextTick(() => reveal(new Set([m.serverId]))); }
}
// Open this machine on the board: selected, its relay open, in view.
function show(id) {
  if (!byId.value.has(id)) return;
  const r = homes.value.get(id);
  if (r) open.add(r);
  clearPath();
  focusId.value = id;
  void nextTick(() => reveal(related(id)));
}
// From another page: a route (without its end, the end is picked on the board), or a link started from a machine.
defineExpose({show, pathBetween: (a, b) => { route.from = a; route.to = b || ''; route.picking = !b; focusId.value = ''; },
  linkFrom: id => { linking.value = id; focusId.value = ''; }});

const focusMember = computed(() => byId.value.get(focusId.value) || null);
const focusInfo = computed(() => {
  const m = focusMember.value;
  if (!m) return null;
  const id = m.serverId, [tone, state] = nodeState(data.value, id);
  let conn = '';
  if (isRelay(m)) conn = `${relayEdges.value.filter(e => e.a === id || e.b === id).length} 条线路 · ${riders(id).length} 台成员经它接入`;
  else {
    const r = homes.value.get(id), l = r ? entryLine(id, r) : null;
    const more = m.entry?.mode === 'upstreams' ? (m.entry?.candidates || []).filter(x => x.relay !== r && relaySet.value.has(x.relay)).length : 0;
    conn = r ? `经 ${nm(r)} 接入${more > 0 ? ` +${more}` : ''}${l?.transport ? ` · ${transportLabel(l.transport)}` : ''}${Number.isFinite(l?.rttMs) ? ` · ${msText(l.rttMs)}` : ''}` : (m.entry?.candidates || []).length ? '接入的中转不在画板上' : '还没有设置怎么连进组网';
  }
  return {id, name: m.name, flag: flag(regionOf(id)), region: regionName(regionOf(id)) || '地区未知', role: roleText(m), address: m.address || '应用后分配', funcs: funcs(m),
    tone: fresh(id) ? 'warn' : tone, state: fresh(id) ? '待应用' : state, conn, fresh: fresh(id)};
});
const regionGroups = computed(() => {
  const groups = new Map();
  for (const m of live.value) { const k = regionOf(m.serverId) || '?'; (groups.get(k) || groups.set(k, []).get(k)).push(m); }
  return [...groups.entries()].map(([k, list]) => ({key: k, label: k === '?' ? '地区未知' : regionName(k) || k, list: list.sort((a, b) => a.name.localeCompare(b.name, 'zh'))}))
    .sort((a, b) => b.list.length - a.list.length);
});
const routeOptions = computed(() => regionGroups.value.flatMap(g => g.list.map(m => ({value: m.serverId, label: m.name, region: regionOf(m.serverId), group: g.label}))));

watch(() => layout.value.zones.length, n => { if (n && !fitted) { fitted = true; void nextTick(fit); } });
let observer;
onMounted(() => {
  ensureFlagFont();
  measure();
  if (layout.value.zones.length) { fitted = true; fit(); }
  observer = new ResizeObserver(measure);
  if (board.value) observer.observe(board.value);
});
onBeforeUnmount(() => observer?.disconnect());
</script>
<template>
  <div class="m3-canvas" :class="{'is-phone': phone}">
    <div ref="board" class="m3-vp" @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="up" @wheel.prevent="wheel">
      <div class="m3-world" :style="worldStyle">
        <div v-for="z in layout.zones" :key="z.key" class="m3-zone" :class="{dim: highlight}" :style="{left: `${z.x}px`, top: `${z.y}px`, width: `${z.w}px`, height: `${z.h}px`}">
          <div class="m3-zone-h" :data-zone="z.key" title="拖动可以挪开这一组"><span v-if="z.flag" class="sv-flag" aria-hidden="true">{{ z.flag }}</span>{{ z.label }}
            <span class="m3-faint">{{ [z.relays && `${z.relays} 中转`, z.members && `${z.members} 成员`].filter(Boolean).join(' · ') }}</span></div>
        </div>
        <svg class="m3-edges" :style="{left: `${svgBox.x0}px`, top: `${svgBox.y0}px`}" :width="svgBox.w" :height="svgBox.h" :viewBox="`${svgBox.x0} ${svgBox.y0} ${svgBox.w} ${svgBox.h}`" aria-hidden="true">
          <path v-for="e in drawn.edges" :key="e.key" class="m3-edge" :class="e.cls" :d="e.d"><title>{{ e.title }}</title></path>
          <path v-if="drawn.pathD" class="m3-edge path" :d="drawn.pathD" />
        </svg>
        <div v-for="c in cards" :key="c.id" class="m3-card" :class="[nodeCls(c.id), {site: isSite(c.m), member: !c.relay}]" data-node :data-id="c.id" role="button" tabindex="0"
          :aria-label="`${c.m.name}，${c.relay ? roleText(c.m) : '成员'}，${c.state}`" :style="{left: `${c.b.x}px`, top: `${c.b.y}px`, width: `${c.b.w}px`, height: `${c.b.h}px`}"
          @click="nodeClick(c.id)" @keydown.enter.prevent="nodeClick(c.id)" @mouseenter="hoverId = c.id" @mouseleave="hoverId = ''">
          <div class="m3-card-t"><span v-if="c.flag" class="sv-flag" aria-hidden="true">{{ c.flag }}</span><b>{{ c.m.name }}</b><span class="pb-dot" :data-tone="c.tone" :title="c.state"></span></div>
          <div class="m3-card-m"><span class="mono">{{ c.m.address || '应用后分配' }}</span><span>{{ roleText(c.m) }}</span></div>
          <div v-if="c.funcs.length" class="m3-card-f"><span v-for="[k, t] in c.funcs" :key="k" class="pb-tag is-plain" :data-kind="k">{{ t }}</span></div>
          <button v-if="c.n" type="button" class="m3-card-x" :aria-expanded="c.open" @click.stop="toggle(c.id)">{{ c.open ? `收起 ${c.n} 台成员` : `展开 ${c.n} 台成员` }}</button>
        </div>
        <div v-for="c in chips" :key="c.id" class="m3-chip" :class="nodeCls(c.id)" data-node :data-id="c.id" role="button" tabindex="0" :aria-label="`${c.m.name}，成员`"
          :style="{left: `${c.b.x}px`, top: `${c.b.y}px`, width: `${c.b.w}px`, height: `${c.b.h}px`}" @click="nodeClick(c.id)" @keydown.enter.prevent="nodeClick(c.id)"
          @mouseenter="hoverId = c.id" @mouseleave="hoverId = ''">
          <span class="pb-dot" :data-tone="c.tone" aria-hidden="true"></span><span v-if="c.flag" class="sv-flag" aria-hidden="true">{{ c.flag }}</span><b>{{ c.m.name }}</b><span class="ob-grow" /><span class="m3-faint">{{ c.note }}</span>
        </div>
        <span v-for="l in drawn.labels" :key="l.key" class="m3-label" :class="l.cls" :style="{left: `${l.x}px`, top: `${l.y}px`}">{{ l.text }}</span>
      </div>
    </div>

    <div class="m3-tl">
      <input v-model="search" class="m3-float m3-search" list="m3-names" placeholder="找节点，回车定位" aria-label="找节点" @keydown.enter.prevent="find" @change="find" />
      <datalist id="m3-names"><option v-for="m in live" :key="m.serverId" :value="m.name" /></datalist>
      <div class="m3-ctl">
        <el-button class="m3-float" @click="emit('join')">＋ 加入服务器</el-button>
        <el-button class="m3-float" @click="toggleAll">{{ allOpen ? '全部收起' : '全部展开' }}</el-button>
        <el-button v-if="!phone" class="m3-float" title="把拖开的分组放回原位" @click="arrange">自动排列</el-button>
      </div>
    </div>
    <div class="m3-pathbar m3-float">
      <span class="m3-faint">路线</span>
      <MeshNodeSelect v-model="route.from" :options="routeOptions" placeholder="从…" aria-label="起点" width="170px" />
      <button type="button" class="ix-icon-button" title="交换起点和终点" aria-label="交换起点和终点" @click="swap"><ArrowLeftRight :size="14" /></button>
      <MeshNodeSelect v-model="route.to" :options="routeOptions" placeholder="到…" aria-label="终点" width="170px" />
      <button v-if="route.from || route.to" type="button" class="ix-icon-button" aria-label="清除路线" @click="clearPath"><X :size="14" /></button>
    </div>
    <p v-if="linking" class="m3-pill m3-float" role="status">连接：已选 <b>{{ nm(linking) }}</b>，再点另一个节点<button type="button" class="ix-text-button" @click="linking = ''">取消</button></p>
    <p v-else-if="route.picking" class="m3-pill m3-float" role="status">路线：从 <b>{{ nm(route.from) }}</b> 出发，点终点那个节点<button type="button" class="ix-text-button" @click="clearPath">取消</button></p>

    <div class="m3-tr">
      <div class="m3-float m3-layers" role="group" aria-label="图上显示">
        <button v-for="[k, l] in [['lines', '中转线路'], ['rtt', '延迟数字'], ['direct', '直连']]" :key="k" type="button" :aria-pressed="layers[k]" @click="layers[k] = !layers[k]">{{ l }}</button>
      </div>
      <section v-if="focusInfo" class="m3-float m3-fcard" :aria-label="focusInfo.name">
        <h3><span class="pb-dot" :data-tone="focusInfo.tone" aria-hidden="true"></span><span v-if="focusInfo.flag" class="sv-flag" aria-hidden="true">{{ focusInfo.flag }}</span><span class="m3-ellipsis">{{ focusInfo.name }}</span>
          <span class="ob-grow" /><button type="button" class="ix-icon-button" aria-label="关闭" @click="focusId = ''"><X :size="14" /></button></h3>
        <p class="m3-faint">{{ focusInfo.region }} · {{ focusInfo.role }} · <span class="mono">{{ focusInfo.address }}</span></p>
        <p v-if="focusInfo.funcs.length" class="m3-tags"><span v-for="[k, t] in focusInfo.funcs" :key="k" class="pb-tag is-plain">{{ t }}</span></p>
        <p class="m3-faint">{{ focusInfo.state }}</p>
        <p>{{ focusInfo.conn }}</p>
        <div class="m3-acts">
          <el-button type="primary" size="small" @click="emit('open-machine', focusInfo.id)">打开详情</el-button>
          <template v-if="!focusInfo.fresh">
            <el-button size="small" @click="pathFrom(focusInfo.id)">从这里出发</el-button>
            <el-button size="small" @click="pathTo(focusInfo.id)">到这里</el-button>
          </template>
          <el-button size="small" @click="linking = focusInfo.id; focusId = ''">连接到…</el-button>
        </div>
      </section>
    </div>

    <div v-if="route.from && route.to" class="m3-bc">
      <div class="m3-float m3-ppanel" role="status">
        <p v-if="!pathState" class="m3-faint">正在查路线…</p>
        <p v-else-if="pathState.error" class="ob-warn">{{ pathState.error }}</p>
        <p v-else-if="!pathHops.length" class="ob-warn">{{ nm(route.from) }} 到 {{ nm(route.to) }} 现在没有路线：中间缺少接入或线路。</p>
        <template v-else>
          <p class="m3-hops">
            <template v-for="(h, i) in pathHops" :key="h.node + i">
              <span v-if="i" class="m3-ms">{{ [pathSegs[i - 1].transport, Number.isFinite(pathSegs[i - 1].rtt) ? msText(pathSegs[i - 1].rtt) : ''].filter(Boolean).join(' ') }} →</span>
              <button type="button" class="ix-text-button m3-hop" @click="focusId = h.node"><span v-if="flag(regionOf(h.node))" class="sv-flag" aria-hidden="true">{{ flag(regionOf(h.node)) }}</span>{{ nm(h.node) }}</button>
            </template>
          </p>
          <p class="m3-faint">
            <span v-if="pathFixed" class="pb-tag" :data-tone="pathFixed.tone === 'off' ? undefined : pathFixed.tone">{{ pathFixed.text }}</span>
            <span>{{ pathBroken ? `在 ${nm(pathHops.at(-1).node)} 之后断开` : `共 ${Number.isFinite(pathState.rttMs) ? msText(pathState.rttMs) : '延迟未知'} · ${pathHops.length - 1} 段` }}{{ pathState.stale ? ' · 数据不完整' : '' }} · 按节点上报的实际路由</span>
            <button v-if="canPin" type="button" class="ix-text-button" @click="pinCurrentPath">加为自建路线</button>
          </p>
        </template>
      </div>
    </div>
    <div v-if="!phone" class="m3-bl m3-float m3-legend">
      <span><i class="g"></i>&lt;50ms</span><span><i class="m"></i>50–130ms</span><span><i class="f"></i>&gt;130ms</span><span><i class="d"></i>少走</span><span><i class="t"></i>直连</span><span><i class="p"></i>路线</span><span><i class="n"></i>待应用</span><span>虚线框 站点中转</span>
    </div>
    <div class="m3-br">
      <div class="m3-float m3-zoom">
        <button type="button" class="ix-icon-button" aria-label="放大" @click="zoomAt(1.25)"><Plus :size="15" /></button>
        <span class="m3-z">{{ Math.round(view.z * 100) }}%</span>
        <button type="button" class="ix-icon-button" aria-label="缩小" @click="zoomAt(0.8)"><Minus :size="15" /></button>
        <button type="button" class="ix-icon-button" aria-label="适应画面" @click="fit"><Maximize :size="14" /></button>
      </div>
      <div v-if="!phone" class="m3-float m3-mini">
        <svg viewBox="0 0 196 116" aria-label="小地图" @click="miniClick">
          <rect v-for="z in mini.zones" :key="z.key" class="mz" :x="z.x" :y="z.y" :width="z.w" :height="z.h" rx="2" />
          <rect v-for="c in mini.cards" :key="c.id" class="mc" :class="{on: c.id === focusId}" :x="c.x" :y="c.y" :width="c.w" :height="c.h" />
          <rect class="mv" :x="mini.view.x" :y="mini.view.y" :width="mini.view.w" :height="mini.view.h" rx="3" />
        </svg>
      </div>
    </div>
    <p v-if="!live.length" class="m3-empty instance-muted">还没有节点加入组网。<button type="button" class="ix-text-button" @click="emit('join')">加入服务器</button></p>
  </div>
</template>
