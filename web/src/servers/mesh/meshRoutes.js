// Own routes (自建路线): from one machine to another through the relays listed, and back the same relays
// reversed. The routes of the same two machines, whichever way round they are written, are that pair's
// in their order, at most MAX_PER_PAIR: the nodes take the first whose links are all up, the next when it
// goes down, the automatic routes when none is up. An end is a relay, or a member taking all its entry
// relays at once (同时连上), linked to the relay next to it. No requests here.
import { hmText } from './meshAuto.js';
import { addLink, planLink } from './meshGraph.js';
import { isRelay, uid } from './meshHelpers.js';

export const MAX_PER_PAIR = 3;
export const MAX_VIA = 8;
const find = (work, id) => (work?.members || []).find(m => m.serverId === id);
export const nodesOf = p => [p.from, ...(p.via || []), p.to];
// The route's machines from src on.
export const nodesFrom = (p, src) => p.from === src ? nodesOf(p) : nodesOf(p).reverse();
export const ofPair = (p, a, b) => (p.from === a && p.to === b) || (p.from === b && p.to === a);
export const pairRoutes = (paths, a, b) => (paths || []).filter(p => ofPair(p, a, b));
// Whether a route goes through the same machines as nodes, either way round.
export function sameRoute(p, nodes) {
  const n = nodesOf(p).join('>');
  return n === nodes.join('>') || n === [...nodes].reverse().join('>');
}

// The pairs sid has routes in, each with its routes in order; the pairs as their first routes come.
export function pairsOf(paths, sid) {
  const out = new Map();
  for (const p of paths || []) {
    if (p.from !== sid && p.to !== sid) continue;
    const other = p.from === sid ? p.to : p.from;
    if (!out.has(other)) out.set(other, []);
    out.get(other).push(p);
  }
  return [...out].map(([other, routes]) => ({other, routes}));
}

// Moves a route up (-1) or down (1) among its pair's, leaving the other routes where they are.
export function moveInPair(paths, route, delta) {
  const i = paths.indexOf(route);
  if (i < 0) return false;
  for (let j = i + delta; j >= 0 && j < paths.length; j += delta) {
    if (ofPair(paths[j], route.from, route.to)) {
      [paths[i], paths[j]] = [paths[j], paths[i]];
      return true;
    }
  }
  return false;
}

function relaysLinked(work, a, b) {
  if ((work.relayLinks?.mode || 'full') === 'full') return true;
  return (work.relayLinks.pairs || []).some(p => (p.a === a && p.b === b) || (p.a === b && p.b === a));
}

// What one segment of a route lacks in the draft, and what saving the route does about it: null when
// nothing; else {kind: 'entry'|'relay', a, b, member?, relay?, add (a link is to be made), text}, or
// {error, text} for a segment no route can have.
export function gap(work, a, b) {
  const ma = find(work, a), mb = find(work, b);
  if (!ma || !mb || ma.removing || mb.removing) return {error: true, text: `${(ma || mb)?.name || '有一台'} 不在组网里`};
  if (isRelay(ma) && isRelay(mb)) return relaysLinked(work, a, b) ? null : {kind: 'relay', a, b, add: true, text: `新建 ${ma.name} 和 ${mb.name} 之间的中转线路`};
  if (!isRelay(ma) && !isRelay(mb)) return {error: true, text: `${ma.name} 和 ${mb.name} 都不是中转：路线中间只能经过中转`};
  const [m, r] = isRelay(ma) ? [mb, ma] : [ma, mb];
  const has = (m.entry?.candidates || []).some(c => c.relay === r.serverId);
  const all = m.entry?.mode === 'upstreams';
  if (has && all) return null;
  const text = [!has && `${r.name} 加为 ${m.name} 的入口中转`, !all && `${m.name} 改为「同时连上」`].filter(Boolean).join('，');
  return {kind: 'entry', a, b, member: m.serverId, relay: r.serverId, add: !has, text};
}

export const gapsOf = (work, nodes) => nodes.slice(1).map((b, i) => gap(work, nodes[i], b));

// Makes what the segments of nodes lack: an entry relay of a member end, its transport chosen by the
// controller as the line rules and limits allow, the member then taking all its entry relays at once; a
// relay link otherwise. Changes nothing and returns {error} when one cannot be made.
export function connect(work, nodes) {
  const gaps = gapsOf(work, nodes).filter(Boolean);
  const bad = gaps.find(g => g.error);
  if (bad) return {error: bad.text};
  const plans = [];
  for (const g of gaps.filter(x => x.add)) {
    const plan = g.kind === 'entry' ? planLink(work, g.member, g.relay, 'entry') : planLink(work, g.a, g.b, 'relay');
    if (plan.error) return {error: plan.error};
    plans.push(plan);
  }
  for (const plan of plans) {
    addLink(work, plan);
    if (plan.kind === 'entry') find(work, plan.member).entry.candidates.at(-1).auto = ['transport'];
    else work.relayLinks.pairs.at(-1).auto = ['transport', 'dialer', 'bias'];
  }
  for (const g of gaps.filter(x => x.kind === 'entry')) {
    const m = find(work, g.member);
    if (m.entry.mode !== 'upstreams') m.entry = {...m.entry, mode: 'upstreams', autoReturn: true};
  }
  return {};
}

// A new route from src to dst through via, last among its pair's, its links made: {route} or {error}.
export function addRoute(work, src, dst, via) {
  const nodes = [src, ...via, dst];
  if (!via.length || via.length > MAX_VIA) return {error: `途经中转要有 1～${MAX_VIA} 台`};
  const same = pairRoutes(work.paths, src, dst);
  if (same.length >= MAX_PER_PAIR) return {error: `这一对已经有 ${MAX_PER_PAIR} 条自建路线，先删掉一条`};
  if (same.some(p => sameRoute(p, nodes))) return {error: '已经有这条自建路线了'};
  const done = connect(work, nodes);
  if (done.error) return done;
  const route = {id: uid('p'), on: true, from: src, to: dst, via: [...via]};
  work.paths = [...(work.paths || []), route];
  return {route};
}

// A route through other relays, in its place: {} or {error}.
export function changeRoute(work, route, via) {
  const nodes = [route.from, ...via, route.to];
  if (!via.length || via.length > MAX_VIA) return {error: `途经中转要有 1～${MAX_VIA} 台`};
  if (pairRoutes(work.paths, route.from, route.to).some(p => p !== route && sameRoute(p, nodes))) return {error: '已经有这条自建路线了'};
  const done = connect(work, nodes);
  if (done.error) return done;
  route.via = [...via];
  return {};
}

// Where a route is, from an end's side, as pathsView has it: {tone, text}. src is the end looked from.
export function routeState(p, view, src, name = id => id) {
  if (p.on === false) return {tone: 'off', text: '已停用'};
  if (!view) return {tone: 'off', text: '未保存'};
  if (view.problems?.length) return {tone: 'bad', text: view.problems[0].message};
  if (view.single?.length) return {tone: 'warn', text: '不会生效：有节点版本旧，这一对只用第 1 条'};
  const st = (src === p.from ? view.states?.to : view.states?.back) || view.states?.to;
  switch (st?.state) {
    case 'pinned': return {tone: 'ok', text: '使用中'};
    case 'standby': return {tone: 'info', text: '备用'};
    case 'fallback': return {tone: 'bad', text: st.down ? `断开（${st.down.map(name).join(' 和 ')} 之间）` : st.silent ? '包过不去（各段线路都在）' : '断开'};
    case 'waiting': {
      const t = hmText(st.until);
      return {tone: 'warn', text: `刚恢复，${t ? `${t} 起` : '稳定后'}可用`};
    }
    case 'starting': return st.silent ? {tone: 'warn', text: '包过不去（各段线路都在）'} : {tone: 'info', text: '正在连接'};
    default: return {tone: 'off', text: '未应用'};
  }
}

// The round trip of a route's links as last measured, or null while one is not.
export function routeRtt(view) {
  const segs = view?.segments || [];
  return segs.length && segs.every(s => Number.isFinite(s.rttMs)) ? Math.round(segs.reduce((n, s) => n + s.rttMs, 0) * 10) / 10 : null;
}
