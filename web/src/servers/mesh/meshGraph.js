// The board's model: which links two nodes can have, where nodes go when there
// is no saved layout, and which members a rule connects. No requests here.
import { ALL_MEMBERS, candErrorText, CAND_STATES, entryHealth, errorsUnder, isRelay, isSite, listens, mutualRule, protoText, sideParts, TRANSPORTS, transportLabel, uid } from './meshHelpers.js';

export const TOOLS = [
  ['select', '选择', '拖动节点，点选查看属性；从节点边上的圆点拖到另一台，按两端角色自动连线'],
  ['entry', '入口', '成员连到中转：从成员拖到中转'],
  ['relay', '中转互联', '两台中转之间的连接'],
  ['direct', '直连', '两台普通成员之间不经过中转'],
  ['rule', '访问规则', '从来源拖到目标，允许来源访问目标'],
];
export const MAX_CANDIDATES = 8;
export const DRAG_TYPE = 'application/x-openbear-mesh-server';
const KINDS = {entry: '入口', relay: '中转互联', direct: '直连', rule: '访问规则'};
const find = (work, id) => (work?.members || []).find(m => m.serverId === id);
const samePair = (p, a, b) => (p.a === a && p.b === b) || (p.a === b && p.b === a);
const openOn = (m, allowed = TRANSPORTS.map(([t]) => t)) => allowed.filter(t => listens(m, t));

// What dragging from one node to another makes, or why it makes nothing.
export function planLink(work, fromId, toId, tool = 'select') {
  const a = find(work, fromId), b = find(work, toId);
  if (!a || !b) return {error: '只能连到网格里的服务器'};
  if (a === b) return {error: '不能连到它自己'};
  const gone = [a, b].find(m => m.removing);
  if (gone) return {error: `${gone.name} 正在移除，不能再连线`};
  const ra = isRelay(a), rb = isRelay(b);
  const kind = tool === 'select' ? (ra && rb ? 'relay' : ra || rb ? 'entry' : 'direct') : tool;
  if (kind === 'entry') {
    if (ra && rb) return {error: `${a.name} 和 ${b.name} 都是中转；入口是成员连到中转，中转之间用“中转互联”`};
    if (!ra && !rb) return {error: `入口要连到中转，${b.name} 不是中转；两台成员之间用“直连”`};
    const member = ra ? b : a, relay = ra ? a : b;
    const cands = member.entry?.candidates || [];
    if (cands.length >= MAX_CANDIDATES) return {error: `${member.name} 已经有 ${MAX_CANDIDATES} 个入口，最多这么多`};
    const used = new Set(cands.filter(c => c.relay === relay.serverId).map(c => c.transport));
    // A site relay is reached by punching: UDP only.
    if (isSite(relay)) return used.has('udp') ? {error: `${member.name} 已经有到 ${relay.name} 的入口（站点中转只能打洞连接）`} : {kind, member: member.serverId, relay: relay.serverId, transport: 'udp'};
    const open = openOn(relay);
    const transport = open.find(t => !used.has(t)) || (used.size ? '' : 'udp');
    if (!transport) return {error: `${relay.name} 开启的传输方式都已经是 ${member.name} 的入口`};
    return {kind, member: member.serverId, relay: relay.serverId, transport};
  }
  if (kind === 'relay') {
    if (!ra || !rb) return {error: `中转互联的两端都要是中转，${(ra ? b : a).name} 不是`};
    if (work.relayLinks?.mode !== 'custom') return {error: '所有中转已经两两相连。要单独设置，先选中一条中转连线，关掉“所有中转两两相连”'};
    if ((work.relayLinks.pairs || []).some(p => samePair(p, a.serverId, b.serverId))) return {error: `${a.name} 和 ${b.name} 已经连着`};
    // Two site relays punch (UDP); a site relay dials the public one.
    if (isSite(a) && isSite(b)) return {kind, a: a.serverId, b: b.serverId, transport: 'udp', punch: true};
    const dialer = isSite(b) && !isSite(a) ? 'b' : 'a', passive = dialer === 'a' ? b : a;
    return {kind, a: a.serverId, b: b.serverId, transport: openOn(passive)[0] || 'udp', dialer};
  }
  if (kind === 'direct') {
    if (ra || rb) return {error: `直连只用于两台普通成员，${(ra ? a : b).name} 是中转；成员连中转用“入口”`};
    if ((work.directLinks || []).some(p => samePair(p, a.serverId, b.serverId))) return {error: `${a.name} 和 ${b.name} 已经直连`};
    // UDP by default: hole-punched, so neither end needs an address reachable from outside.
    return {kind, a: a.serverId, b: b.serverId, transport: 'udp'};
  }
  if (kind === 'rule') return {kind, src: a.serverId, dst: b.serverId};
  return {error: '先选一种连接'};
}

// Writes a plan into the draft; returns what to select afterwards.
export function addLink(work, plan) {
  if (plan.kind === 'entry') {
    const member = find(work, plan.member);
    member.entry ||= {mode: 'fixed', candidates: [], autoReturn: true};
    const c = {id: uid('c'), relay: plan.relay, transport: plan.transport};
    member.entry.candidates.push(c);
    return {kind: 'edge', id: `entry:${plan.member}:${c.id}`};
  }
  if (plan.kind === 'relay') {
    work.relayLinks.pairs = [...(work.relayLinks.pairs || []), {a: plan.a, b: plan.b, transport: plan.transport, cost: 1, dialer: plan.dialer || 'a'}];
    return {kind: 'edge', id: `pair:${work.relayLinks.pairs.length - 1}`, opened: plan.punch ? openUdp(work, [plan.a, plan.b]) : []};
  }
  if (plan.kind === 'direct') {
    work.directLinks.push({a: plan.a, b: plan.b, transport: plan.transport, dialer: 'a'});
    return {kind: 'edge', id: `direct:${work.directLinks.length - 1}`, opened: plan.transport === 'udp' ? openUdp(work, [plan.a, plan.b]) : []};
  }
  const rule = {id: uid('r'), src: {all: false, members: [plan.src], tags: [], prefixes: []}, dst: {all: false, members: [plan.dst], tags: [], subnets: []}, proto: 'any', ports: [], note: ''};
  work.rules.push(rule);
  return {kind: 'rule', id: rule.id};
}
// Punching needs UDP listening on both ends; turn it on where it is off. Returns the names.
function openUdp(work, ids) {
  const opened = [];
  for (const m of ids.map(id => find(work, id))) {
    if (!m || listens(m, 'udp')) continue;
    m.listen = {udp: 0, tcp: 0, tls: 0, ...(m.listen || {}), udp: Number(work.settings?.ports?.udp) || 51999};
    opened.push(m.name);
  }
  return opened;
}
// A link between two site relays is punched; the others are dialled.
export const punchedRelayLink = (work, a, b) => isSite(find(work, a)) && isSite(find(work, b));
export const linkKindLabel = kind => KINDS[kind] || '';
const EDGE_KINDS = {entry: 'entry', relay: 'relay', pair: 'relay', direct: 'direct'};
export const edgeKind = id => EDGE_KINDS[String(id).split(':')[0]] || '';
// The panel's title for a selection {kind: 'node' | 'edge' | 'rule', id}.
export function selectionTitle(sel, work, installing = []) {
  if (!sel) return '网格';
  if (sel.kind === 'node') return find(work, sel.id)?.name || installing.find(x => x.serverId === sel.id)?.name || '服务器';
  if (sel.kind === 'edge') return KINDS[edgeKind(sel.id)] || '连线';
  if (sel.kind === 'rules') return `${sel.id.replace(/^rules:/, '').split('|').map(id => find(work, id)?.name || id).join(' 和 ')} 之间的规则`;
  return `访问规则 ${(work?.rules || []).findIndex(r => r.id === sel.id) + 1}`;
}

// Every pair of relays when relay links are automatic.
export function relayPairs(work) {
  const relays = (work?.members || []).filter(m => isRelay(m) && !m.removing);
  if (work?.relayLinks?.mode === 'custom') return [];
  return relays.flatMap((a, i) => relays.slice(i + 1).map(b => [a.serverId, b.serverId]));
}
// Turning automatic relay links off keeps what they meant, minus one pair when deleting it.
export function customRelayLinks(work, without = null) {
  const pairs = relayPairs(work).filter(([a, b]) => !without || !samePair({a, b}, ...without));
  // A site relay dials the public one.
  work.relayLinks = {mode: 'custom', pairs: pairs.map(([a, b]) => ({a, b, transport: 'udp', cost: 1, dialer: isSite(find(work, b)) && !isSite(find(work, a)) ? 'b' : 'a'}))};
}

// Who a rule side covers on the board: every member with all, else chosen members and members
// with the tags; and gateways of chosen networks (of every network with allSubnets). Nothing chosen covers nobody.
function sideMembers(side, members, nets) {
  if (side?.all) return members.map(m => m.serverId);
  const ids = new Set(side?.members || []);
  if (side?.allSubnets) members.filter(m => m.gateway?.subnets?.length).forEach(m => ids.add(m.serverId));
  for (const tag of side?.tags || []) members.filter(m => (m.tags || []).includes(tag)).forEach(m => ids.add(m.serverId));
  for (const net of side?.[nets] || []) members.filter(m => (m.gateway?.subnets || []).includes(net)).forEach(m => ids.add(m.serverId));
  return [...ids].filter(id => members.some(m => m.serverId === id));
}
// A rule is drawn as arrows only when both sides name machines: chosen members,
// or networks behind a gateway. Everyone, every gateway network, tags and other networks go to the legend,
// so a rule for all members does not turn into an arrow between every pair.
function pointed(side, members, nets) {
  if (side?.all || side?.allSubnets || side?.tags?.length || !(side?.members?.length || side?.[nets]?.length)) return false;
  return (side[nets] || []).every(net => members.some(m => (m.gateway?.subnets || []).includes(net)));
}
// Machines listed on both sides (all of them reaching each other) would be an arrow between
// every pair too: three or more go to the legend as well.
export function drawnRule(rule, members) {
  const live = members.filter(m => !m.removing);
  if (mutualRule(rule) && sideMembers(rule.src, live, 'prefixes').length > 2) return false;
  return pointed(rule.src, live, 'prefixes') && pointed(rule.dst, live, 'subnets');
}
// The members a rule touches on either side, for highlighting.
export function ruleNodes(rule, members) {
  const live = members.filter(m => !m.removing);
  return new Set([...sideMembers(rule.src, live, 'prefixes'), ...sideMembers(rule.dst, live, 'subnets')]);
}
// [{a, b, both}] for one rule: a may reach b (both: and back).
export function ruleEnds(rule, members) {
  const live = members.filter(m => !m.removing), src = sideMembers(rule.src, live, 'prefixes'), dst = sideMembers(rule.dst, live, 'subnets');
  const pairs = new Map();
  for (const a of src) for (const b of dst) {
    if (a === b) continue;
    const key = [a, b].sort().join('>'), seen = pairs.get(key);
    if (seen) seen.both ||= seen.a !== a; else pairs.set(key, {a, b, both: false});
  }
  return [...pairs.values()];
}

// The board's links along a path (GET /mesh/path hops): for each step the link of the
// kind the hop names, the one in use when a member has several to the same relay.
const VIA_KINDS = {direct: ['direct'], entry: ['entry'], relay: ['relay'], lan: ['entry', 'direct', 'relay']};
export function pathEdges(edges, hops) {
  const out = new Set();
  for (let i = 1; i < (hops || []).length; i++) {
    const u = hops[i - 1].node, v = hops[i].node;
    const between = edges.filter(e => e.data?.kind !== 'rule' && ((e.source === u && e.target === v) || (e.source === v && e.target === u)));
    const kinds = VIA_KINDS[hops[i].via] || ['entry', 'relay', 'direct'];
    const pick = kinds.map(k => between.filter(e => e.data.kind === k)).find(l => l.length) || between;
    const best = pick.find(e => e.data.look === 'active') || pick[0];
    if (best) out.add(best.id);
  }
  return out;
}

// Without a saved position a node goes to the end of its row (nextSlot): relays on top, members below.
export const GAP_X = 230, ROW_Y = 210;
// Phones lay the board out down the screen: relays on top, then members, two to a
// row, so it reads without dragging sideways. Not saved: the saved layout is the desktop's.
export const PHONE_GAP_X = 170, PHONE_ROW_Y = 132;
export function phoneLayout(ids, isRelayId) {
  const out = {};
  let y = 0;
  for (const group of [ids.filter(isRelayId), ids.filter(id => !isRelayId(id))]) {
    if (!group.length) continue;
    for (let i = 0; i < group.length; i += 2) {
      const row = group.slice(i, i + 2);
      row.forEach((id, j) => { out[id] = {x: row.length === 1 ? PHONE_GAP_X / 2 : j * PHONE_GAP_X, y}; });
      y += PHONE_ROW_Y;
    }
    y += 24;
  }
  return out;
}
// The next free place at the end of a row (relays on top, members below), for
// nodes nobody has placed yet.
export function nextSlot(positions, rowIds, relay = false) {
  const row = rowIds.map(id => positions[id]).filter(Boolean);
  if (!row.length) {
    const all = Object.values(positions).filter(Boolean);
    if (!all.length) return {x: 0, y: relay ? 0 : ROW_Y};
    const ys = all.map(p => p.y);
    return {x: Math.min(...all.map(p => p.x)), y: relay ? Math.min(...ys) - ROW_Y : Math.max(...ys) + ROW_Y};
  }
  const last = row.reduce((a, b) => (b.x > a.x ? b : a));
  return {x: last.x + GAP_X, y: last.y};
}

// How an entry candidate looks: in use, the configured first, a standby, broken (the
// settings) or failing (its last try, as the member reports), or punching to a site relay.
export function entryLook(work, data, errors, member, index, status = null) {
  const c = member.entry.candidates[index], relay = find(work, c.relay);
  const summary = data?.nodes?.[member.serverId]?.summary?.entry, health = entryHealth(data, member.serverId, status)[c.id] || null;
  const relayState = data?.nodes?.[c.relay]?.state;
  const memberIndex = work.members.indexOf(member);
  let problem = '';
  if (!relay) problem = '这台中转已经不在网格里';
  else if (!isRelay(relay)) problem = `${relay.name} 已经不是中转`;
  else if (isSite(relay) && c.transport !== 'udp') problem = `${relay.name} 是站点中转，只能打洞（UDP）连接`;
  else if (!listens(relay, c.transport)) problem = `${relay.name} 没有开启 ${transportLabel(c.transport)} 监听`;
  else if (errorsUnder(errors, `members[${memberIndex}].entry.candidates[${index}]`).length) problem = '设置需要修改';
  else if (relayState === 'missing') problem = `${relay.name} 的组网服务不见了`;
  else if (['offline', 'failed'].includes(relayState)) problem = `${relay.name} ${relayState === 'offline' ? '离线' : '失败'}`;
  const inUse = health?.state === 'active' || (Boolean(summary?.candidate) && summary.candidate === c.id);
  const look = problem ? 'bad' : inUse ? 'active' : health?.state === 'failing' ? 'failing' : health?.state === 'punching' ? 'punching'
    : !summary?.candidate && index === 0 ? 'primary' : 'standby';
  const role = inUse ? '在用' : member.entry.mode === 'failover' ? (index === 0 ? '首选' : '备用') : index === 0 ? '首选' : '不使用（固定模式只用第一个）';
  const failing = health?.state === 'failing' ? candErrorText(health.lastError) || CAND_STATES.failing[1] : '';
  // Through to a site relay (in use or ready as standby): over the LAN or the internet, as the member reports it.
  const punched = isSite(relay), opened = punched && ['active', 'ready'].includes(health?.state);
  // A standby carries nothing while another candidate is in use.
  const anyInUse = (member.entry.candidates || []).some(x => entryHealth(data, member.serverId, status)[x.id]?.state === 'active') || Boolean(summary?.candidate);
  const standby = !problem && !inUse && anyInUse;
  // A hot standby is kept connected; a cold one (untested) is only tried now and then.
  return {look, problem, role, health, failing, punched, opened, via: opened ? ENTRY_VIA[health.via] || '' : '', standby, cold: standby && health?.state === 'untested'};
}
export const standbyNote = look => look.cold ? '冷备，平时不连，只定时试连' : '热备，平时不走流量';
export const standbyDetail = look => look.cold ? '冷备：平时不保持连接，只定时试连；前面的入口都不可用时才切过来' : '热备：保持连接、平时不走流量，在用的入口断开时切过来';
// Entries reached by punching say how they got through: LAN or internet once open, punching until then.
export const ENTRY_VIA = {lan: '局域网', public: '公网', configured: '指定地址', routed: '内网转发'};
export function entryHow(look, transport) {
  if (!look.punched) return transportLabel(transport);
  if (look.opened) return look.via || '打洞';
  // Without a report (an older controller) it only says that it punches.
  return look.health || look.look === 'punching' ? '打洞中' : '打洞';
}

// A link drawn between the borders of two boxes ({w, h, cx, cy}): bent by `offset` when several
// links join the same two nodes, bowed out when the nodes nearly touch so the label clears
// both. The label sits at `k` along the curve. Used to draw links and to place their labels.
const EDGE_GAP = 3;
function borderOf(b, tx, ty) {
  const dx = tx - b.cx, dy = ty - b.cy;
  if (!dx && !dy) return {x: b.cx, y: b.cy};
  const s = Math.min((b.w / 2 + EDGE_GAP) / Math.abs(dx || 1e-9), (b.h / 2 + EDGE_GAP) / Math.abs(dy || 1e-9));
  return {x: b.cx + dx * s, y: b.cy + dy * s};
}
export function edgeGeometry(a, b, offset = 0, k = 0.5) {
  const dx = b.cx - a.cx, dy = b.cy - a.cy, len = Math.hypot(dx, dy) || 1;
  const s0 = borderOf(a, b.cx, b.cy), t0 = borderOf(b, a.cx, a.cy), short = Math.hypot(t0.x - s0.x, t0.y - s0.y) < 56;
  const away = Math.abs(dx) >= Math.abs(dy) ? -Math.sign(dx) || 1 : Math.sign(dy) || 1;
  const bend = (offset || 0) + (short ? away * (Math.max(a.h, b.h) / 2 + 29) : 0);
  const c = {x: (a.cx + b.cx) / 2 - dy / len * bend, y: (a.cy + b.cy) / 2 + dx / len * bend};
  const s = borderOf(a, c.x, c.y), t = borderOf(b, c.x, c.y), at = (p, q, r) => (1 - k) ** 2 * p + 2 * k * (1 - k) * q + k ** 2 * r;
  return {s, c, t, label: {x: at(s.x, c.x, t.x), y: at(s.y, c.y, t.y)}};
}

// A link that would run under another node bows around it: the bend (as edgeGeometry takes it, from a
// to b) that keeps the curve `margin` clear of every box in `others`, on the side with fewer nodes, no
// more than it needs. 0 when the straight line is clear. In a tight grid the way between two nodes
// can be narrow: without one at `margin`, a finer search with a smaller margin.
const ARC_STEPS = Array.from({length: 22}, (_, i) => 36 + i * 12), FINE_STEPS = Array.from({length: 43}, (_, i) => 36 + i * 6);
function segmentHitsBox(x1, y1, x2, y2, r) {
  // Liang–Barsky: whether the segment enters the rectangle.
  let t0 = 0, t1 = 1;
  const dx = x2 - x1, dy = y2 - y1;
  for (const [p, q] of [[-dx, x1 - r.l], [dx, r.r - x1], [-dy, y1 - r.t], [dy, r.b - y1]]) {
    if (!p) { if (q < 0) return false; continue; }
    const t = q / p;
    if (p < 0) { if (t > t1) return false; if (t > t0) t0 = t; } else { if (t < t0) return false; if (t < t1) t1 = t; }
  }
  return true;
}
// How many of `rects` the curve runs into, counting no further than `limit`.
function curveHits(a, b, bend, rects, limit = Infinity) {
  const {s, c, t} = edgeGeometry(a, b, bend), N = 16, xs = [], ys = [];
  // The curve stays inside the box of its three points: rectangles outside it are passed over.
  const l = Math.min(s.x, c.x, t.x), r = Math.max(s.x, c.x, t.x), top = Math.min(s.y, c.y, t.y), bottom = Math.max(s.y, c.y, t.y);
  let n = 0;
  for (const q of rects) {
    if (q.r < l || q.l > r || q.b < top || q.t > bottom) continue;
    if (!xs.length) for (let i = 0; i <= N; i++) { const k = i / N; xs.push((1 - k) ** 2 * s.x + 2 * k * (1 - k) * c.x + k ** 2 * t.x); ys.push((1 - k) ** 2 * s.y + 2 * k * (1 - k) * c.y + k ** 2 * t.y); }
    for (let i = 0; i < N; i++) {
      if (Math.max(xs[i], xs[i + 1]) < q.l || Math.min(xs[i], xs[i + 1]) > q.r || Math.max(ys[i], ys[i + 1]) < q.t || Math.min(ys[i], ys[i + 1]) > q.b) continue;
      if (segmentHitsBox(xs[i], ys[i], xs[i + 1], ys[i + 1], q)) { if (++n >= limit) return n; break; }
    }
  }
  return n;
}
// `most`: the largest bow tried (a phone's narrow board has no room for wide ones). Without a clear
// way round, a slight bow that crosses fewer nodes, else the straight line: a wide bow that still
// crosses only adds to the tangle.
const SLIGHT = 84;
export function avoidBend(a, b, others, margin = 10, most = ARC_STEPS.at(-1)) {
  const dx = b.cx - a.cx, dy = b.cy - a.cy, len = Math.hypot(dx, dy) || 1, nx = -dy / len, ny = dx / len;
  // Only boxes near the line can be in the way (the widest bow reaches about half its bend from the line).
  const reach = Math.min(most, len) / 2 + margin;
  const near = others.map(o => ({o, along: ((o.cx - a.cx) * dx + (o.cy - a.cy) * dy) / len, side: (o.cx - (a.cx + b.cx) / 2) * nx + (o.cy - (a.cy + b.cy) / 2) * ny}))
    .filter(x => x.along > -x.o.w && x.along < len + x.o.w && Math.abs(x.side) < reach + Math.max(x.o.w, x.o.h) / 2);
  if (!near.length) return 0;
  const rectsAt = pad => near.map(({o}) => ({l: o.cx - o.w / 2 - pad, r: o.cx + o.w / 2 + pad, t: o.cy - o.h / 2 - pad, b: o.cy + o.h / 2 + pad}));
  const rects = rectsAt(margin);
  const straight = curveHits(a, b, 0, rects);
  if (!straight) return 0;
  // The side with fewer nodes near the line first; with as many on both, whichever needs the smaller bow.
  const plus = near.filter(x => x.side > 0).length, minus = near.filter(x => x.side < 0).length, first = plus <= minus ? 1 : -1;
  const order = list => {
    const steps = list.filter(m => m <= most && m <= Math.max(len * 0.9, list[0]));
    return plus === minus ? steps.flatMap(m => [first * m, -first * m]) : [...steps.map(m => first * m), ...steps.map(m => -first * m)];
  };
  let best = {bend: 0, hits: straight};
  for (const bend of order(ARC_STEPS)) {
    // Past a slight bow only a clear way counts: one hit is enough to pass on.
    const slight = Math.abs(bend) <= SLIGHT, hits = curveHits(a, b, bend, rects, slight ? best.hits : 1);
    if (!hits) return bend;
    if (slight && hits < best.hits) best = {bend, hits};
  }
  const tight = rectsAt(Math.max(4, margin - 6));
  for (const bend of order(FINE_STEPS)) if (!curveHits(a, b, bend, tight, 1)) return bend;
  return best.bend;
}

// A rule as the board's legend lists it: 「所有成员、所有网关网段 → 所有成员 · ping」. Every gateway
// network (allSubnets, those added later included) goes right after 所有成员, before what is chosen by name.
export const ALL_SUBNETS = '所有网关网段';
function legendSide(side, members, nets) {
  const parts = sideParts(side, members, nets).map(p => p.text).filter(t => t !== ALL_SUBNETS);
  if (side?.allSubnets) parts.splice(side?.all ? parts.indexOf(ALL_MEMBERS) + 1 : 0, 0, ALL_SUBNETS);
  if (!parts.length) return '（未选择）';
  return parts.length > 3 ? `${parts.slice(0, 2).join('、')} 等 ${parts.length} 项` : parts.join('、');
}
export function legendText(rule, members) {
  const what = rule.proto === 'icmp' ? 'ping' : protoText(rule).replace('所有端口', '全部');
  const src = legendSide(rule.src, members, 'prefixes');
  if (mutualRule(rule) && Boolean(rule.src?.allSubnets) === Boolean(rule.dst?.allSubnets)) return `${src} 互通 · ${what}`;
  return `${src} → ${legendSide(rule.dst, members, 'subnets')} · ${what}`;
}

// The controller's id for the line a board link stands for (GET /mesh lines[].id): relay:<a>|<b>
// and direct:<a>|<b> with the two serverIds sorted, entry:<member>|<relay>|<transport>. '' for rules.
export function lineIdOf(work, edgeId) {
  const [kind, ...rest] = String(edgeId).split(':'), key = rest.join(':');
  const pair = (a, b) => [a, b].sort().join('|');
  if (kind === 'entry') {
    const [memberId, candId] = key.split(':'), c = find(work, memberId)?.entry?.candidates?.find(x => x.id === candId);
    return c ? `entry:${memberId}|${c.relay}|${c.transport}` : '';
  }
  if (kind === 'relay') { const [a, b] = key.split('~'); return a && b ? `relay:${pair(a, b)}` : ''; }
  if (kind === 'pair') { const p = work?.relayLinks?.pairs?.[Number(key)]; return p ? `relay:${pair(p.a, p.b)}` : ''; }
  if (kind === 'direct') { const p = work?.directLinks?.[Number(key)]; return p ? `direct:${pair(p.a, p.b)}` : ''; }
  return '';
}

// Alignment while dragging a node: a box {x, y, w, h} (board units) snaps to the left edges, centres
// and right edges (top, middle, bottom) of the other boxes when one is within `threshold`, each axis on
// its own. Returns where it goes and the guides to draw: {axis: 'x', at, from, to} is a vertical line
// at x = at from y = from to y = to (axis 'y' a horizontal one), spanning every box on it.
export const SNAP_PX = 6;
export function alignTo(box, others, threshold = SNAP_PX) {
  const lines = {x: b => [b.x, b.x + b.w / 2, b.x + b.w], y: b => [b.y, b.y + b.h / 2, b.y + b.h]};
  const best = {x: null, y: null};
  for (const axis of ['x', 'y']) for (const o of others) for (const v of lines[axis](box)) for (const at of lines[axis](o)) {
    const d = at - v;
    if (Math.abs(d) <= threshold && (!best[axis] || Math.abs(d) < Math.abs(best[axis].d))) best[axis] = {d, at};
  }
  const out = {x: box.x + (best.x?.d || 0), y: box.y + (best.y?.d || 0), guides: []};
  const placed = {...box, x: out.x, y: out.y};
  for (const axis of ['x', 'y']) {
    if (!best[axis]) continue;
    const on = [placed, ...others.filter(o => lines[axis](o).some(at => Math.abs(at - best[axis].at) < 0.5))];
    const [lo, hi] = axis === 'x' ? ['y', 'h'] : ['x', 'w'];
    out.guides.push({axis, at: best[axis].at, from: Math.min(...on.map(b => b[lo])), to: Math.max(...on.map(b => b[lo] + b[hi]))});
  }
  return out;
}
