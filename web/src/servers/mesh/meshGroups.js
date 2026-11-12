// Grouping the board (desktop): members drawn inside the relay they enter by, servers gathered into
// groups (by country or by the server's own group), the links between two groups merged into one, and
// an arrangement that puts linked units and linked groups next to each other. No requests here.
import { isRelay } from './meshHelpers.js';

export const GROUPINGS = [['none', '不分组'], ['region', '按国家或地区'], ['continent', '按大洲'], ['group', '按服务器分组']];
const CONTINENTS = {
  亚洲: 'CN HK MO TW JP KR KP MN SG MY TH VN PH ID KH LA MM BN TL IN PK BD LK NP BT MV AF IR IQ SY JO LB IL PS SA AE QA BH KW OM YE TR GE AM AZ KZ UZ TM KG TJ CY',
  欧洲: 'GB IE FR DE NL BE LU CH AT IT ES PT AD MC SM VA MT GR SE NO DK FI IS EE LV LT PL CZ SK HU SI HR BA RS ME MK AL BG RO MD UA BY RU LI XK GI IM JE GG FO',
  北美洲: 'US CA MX GT BZ SV HN NI CR PA CU JM HT DO PR BS BB TT AG DM GD KN LC VC BM GL',
  南美洲: 'BR AR CL PE CO VE EC BO PY UY GY SR GF',
  大洋洲: 'AU NZ PG FJ SB VU NC PF WS TO KI FM MH PW NR TV GU',
  非洲: 'ZA EG NG KE MA DZ TN LY ET GH CI SN CM UG TZ AO MZ ZM ZW BW NA MU SC RW MG SD',
};
const CONTINENT_OF = new Map(Object.entries(CONTINENTS).flatMap(([name, codes]) => codes.split(' ').map(code => [code, name])));
// The continent a country (ISO code) is in, '' when not known.
export const continentOf = code => CONTINENT_OF.get(String(code || '').toUpperCase()) || '';
export const CHIP_W = 168, CHIP_H = 30, CHIP_STEP = 34, CLUSTER_PAD = 8, NETS_H = 26;
export const ZONE_PAD = 20, ZONE_HEAD = 30;

// The relay a member is drawn in, and which of its candidates that line is: the entry it uses now, else
// its first candidate. Relays, members without candidates and members entering by a relay not on the
// board stand on their own.
export function containerOf(member, relays, active = null) {
  if (!member || isRelay(member)) return null;
  const cands = member.entry?.candidates || [];
  const now = active?.relay ? cands.find(c => c.relay === active.relay && c.id === active.candidate) || cands.find(c => c.relay === active.relay) : null;
  const first = member.entry?.mode === 'upstreams' ? cands.find(c => relays.has(c.relay)) : cands[0];
  const pick = now || first;
  return pick && relays.has(pick.relay) ? {relay: pick.relay, candidate: pick.id} : null;
}

// Where a relay's members go: stacked under it, inside its card's box.
export function chipPlaces(relayPos, relayH, count, nets = false) {
  const top = relayPos.y + relayH + (nets ? NETS_H + 8 : 8);
  return Array.from({length: count}, (_, i) => ({x: relayPos.x + CLUSTER_PAD, y: top + i * CHIP_STEP}));
}
export function clusterBox(relayPos, relayW, relayH, count, nets = false) {
  const bottom = count ? chipPlaces(relayPos, relayH, count, nets).at(-1).y + CHIP_H : relayPos.y + relayH + (nets ? NETS_H : 0);
  return {x: relayPos.x - CLUSTER_PAD, y: relayPos.y - CLUSTER_PAD, w: relayW + 2 * CLUSTER_PAD, h: bottom - relayPos.y + 2 * CLUSTER_PAD};
}

// Places items on a grid so that linked ones sit close and their lines do not run across other items:
// every placement tried for a few items, else the most linked first, each next one in the free cell
// that costs least, then swaps while one helps. The same input always gives the same places.
// items [{id, w, h}], weight(a, b) -> number; returns {places: {id: {x, y}}, w, h}.
const CROSS = 2;
function gridPlace(items, weight, {aspect = 1.4, gapX = 40, gapY = 40} = {}) {
  const n = items.length;
  if (!n) return {places: {}, w: 0, h: 0};
  let cols = Math.max(1, Math.min(n, Math.ceil(Math.sqrt(n * aspect)))), rows = Math.ceil(n / cols);
  // Three or more never stand in one row: the line between the two ends would run across the middle one.
  if (n >= 3 && rows < 2) { rows = 2; cols = Math.ceil(n / 2) + (n % 2 ? 0 : 1); }
  const ids = items.map(i => i.id);
  const total = id => ids.reduce((s, o) => s + (o === id ? 0 : weight(id, o)), 0);
  const order = [...ids].sort((a, b) => total(b) - total(a) || (a < b ? -1 : 1));
  const links = [];
  for (let i = 0; i < order.length; i++) for (let j = i + 1; j < order.length; j++) { const w = weight(order[i], order[j]); if (w) links.push([order[i], order[j], w]); }
  const cells = Array.from({length: rows * cols}, (_, k) => ({r: Math.floor(k / cols), c: k % cols}));
  const mid = {r: (rows - 1) / 2, c: (cols - 1) / 2}, far = k => Math.hypot(cells[k].r - mid.r, cells[k].c - mid.c);
  // A line's length, and how many other items it runs across (counted only while there are few items).
  const crossing = n <= 16;
  function across(p, q, at) {
    let hits = 0;
    for (const k of at.values()) {
      if (k === p || k === q) continue;
      const a = cells[p], b = cells[q], x = cells[k], dr = b.r - a.r, dc = b.c - a.c, len2 = dr * dr + dc * dc;
      const t = Math.max(0, Math.min(1, ((x.r - a.r) * dr + (x.c - a.c) * dc) / len2));
      if (Math.hypot(a.r + t * dr - x.r, a.c + t * dc - x.c) < 0.45) hits++;
    }
    return hits;
  }
  function cost(at) {
    let s = 0;
    for (const [a, b, w] of links) {
      const p = at.get(a), q = at.get(b);
      if (p === undefined || q === undefined) continue;
      s += w * (Math.hypot(cells[p].r - cells[q].r, cells[p].c - cells[q].c) + (crossing ? CROSS * across(p, q, at) : 0));
    }
    return s;
  }
  let at = new Map();
  if (n <= 7 && cells.length <= 9) {
    // Few items: every placement, the cheapest kept (ties: the first found, nearer the middle first).
    const byMiddle = cells.map((_, k) => k).sort((a, b) => far(a) - far(b) || a - b);
    let best = null, bestCost = Infinity;
    const trial = new Map();
    (function place(i) {
      const sofar = cost(trial);
      if (sofar >= bestCost - 1e-9) return;
      if (i === order.length) { best = new Map(trial); bestCost = sofar; return; }
      for (const k of byMiddle) {
        if ([...trial.values()].includes(k)) continue;
        trial.set(order[i], k); place(i + 1); trial.delete(order[i]);
      }
    })(0);
    at = best;
  } else {
    const taken = new Set();
    for (const id of order) {
      let best = -1, bestCost = Infinity;
      for (let k = 0; k < cells.length; k++) {
        if (taken.has(k)) continue;
        at.set(id, k);
        const c = cost(at) + far(k) * 1e-3;
        at.delete(id);
        if (c < bestCost - 1e-9) { best = k; bestCost = c; }
      }
      at.set(id, best); taken.add(best);
    }
    // Swaps (with another item or an empty cell) while one makes it cheaper.
    const who = new Map([...at].map(([id, k]) => [k, id]));
    let current = cost(at);
    for (let round = 0; round < 12; round++) {
      let improved = false;
      for (const id of order) {
        for (let k = 0; k < cells.length; k++) {
          const mine = at.get(id), other = who.get(k);
          if (k === mine) continue;
          at.set(id, k); if (other) at.set(other, mine);
          const next = cost(at);
          if (next < current - 1e-9) {
            current = next; improved = true;
            who.set(k, id); if (other) who.set(mine, other); else who.delete(mine);
          } else { at.set(id, mine); if (other) at.set(other, k); }
        }
      }
      if (!improved) break;
    }
  }
  // Columns as wide as their widest item, rows as tall as their tallest; empty ones take no room.
  const size = new Map(items.map(i => [i.id, i]));
  const colW = Array(cols).fill(-1), rowH = Array(rows).fill(-1);
  for (const [id, k] of at) { colW[cells[k].c] = Math.max(colW[cells[k].c], size.get(id).w); rowH[cells[k].r] = Math.max(rowH[cells[k].r], size.get(id).h); }
  const xs = [], ys = [];
  let x = 0; for (let c = 0; c < cols; c++) { xs.push(x); if (colW[c] >= 0) x += colW[c] + gapX; }
  let y = 0; for (let r = 0; r < rows; r++) { ys.push(y); if (rowH[r] >= 0) y += rowH[r] + gapY; }
  const places = {};
  for (const [id, k] of at) {
    const {r, c} = cells[k], it = size.get(id);
    // Centred in its column, at the top of its row.
    places[id] = {x: xs[c] + (colW[c] - it.w) / 2, y: ys[r]};
  }
  return {places, w: Math.max(0, x - gapX), h: Math.max(0, y - gapY)};
}

// The arrangement: units [{id, w, h, zone}] (zone '' when not grouped), links [[a, b, weight]] between
// units. Units of one group are placed together, the groups next to the groups they link to most.
// Returns {units: {id: {x, y}}, zones: {key: {x, y, w, h}}} with each unit's top left corner.
export function arrange(units, links, {grouped = false, gap = 40, zoneGap = 80} = {}) {
  const w = new Map();
  const add = (a, b, x) => { if (a === b) return; const k = a < b ? `${a}\n${b}` : `${b}\n${a}`; w.set(k, (w.get(k) || 0) + x); };
  for (const [a, b, x = 1] of links) add(a, b, x);
  const weight = (a, b) => w.get(a < b ? `${a}\n${b}` : `${b}\n${a}`) || 0;
  const zoneOf = new Map(units.map(u => [u.id, grouped ? u.zone || '' : '']));
  const keys = [...new Set(units.map(u => zoneOf.get(u.id)))].sort();
  const inner = new Map(), out = {units: {}, zones: {}};
  for (const key of keys) inner.set(key, gridPlace(units.filter(u => zoneOf.get(u.id) === key), weight, {aspect: 1.4, gapX: gap, gapY: gap}));
  if (!grouped) {
    for (const [id, p] of Object.entries(inner.get('')?.places || {})) out.units[id] = p;
    return out;
  }
  const zoneWeight = new Map();
  for (const [k, x] of w) {
    const [a, b] = k.split('\n'), za = zoneOf.get(a), zb = zoneOf.get(b);
    if (za === undefined || zb === undefined || za === zb) continue;
    const zk = za < zb ? `${za}\n${zb}` : `${zb}\n${za}`;
    zoneWeight.set(zk, (zoneWeight.get(zk) || 0) + x);
  }
  const boxes = keys.map(key => ({id: key, w: inner.get(key).w + 2 * ZONE_PAD, h: inner.get(key).h + 2 * ZONE_PAD + ZONE_HEAD}));
  const outer = gridPlace(boxes, (a, b) => zoneWeight.get(a < b ? `${a}\n${b}` : `${b}\n${a}`) || 0, {aspect: 1.8, gapX: zoneGap, gapY: zoneGap});
  for (const box of boxes) {
    const o = outer.places[box.id];
    out.zones[box.id] = {x: o.x, y: o.y, w: box.w, h: box.h};
    for (const [id, p] of Object.entries(inner.get(box.id).places)) out.units[id] = {x: o.x + ZONE_PAD + p.x, y: o.y + ZONE_PAD + ZONE_HEAD + p.y};
  }
  return out;
}

// A group's box around the boxes of its units, with room for its title.
export function zoneBox(boxes) {
  if (!boxes.length) return null;
  const x = Math.min(...boxes.map(b => b.x)), y = Math.min(...boxes.map(b => b.y));
  const r = Math.max(...boxes.map(b => b.x + b.w)), bottom = Math.max(...boxes.map(b => b.y + b.h));
  return {x: x - ZONE_PAD, y: y - ZONE_PAD - ZONE_HEAD, w: r - x + 2 * ZONE_PAD, h: bottom - y + 2 * ZONE_PAD + ZONE_HEAD};
}

// The links between two groups, merged: one entry per pair of groups with the links in it.
// edges [{id, source, target, data}], zoneOf(nodeId) -> key | undefined. Rules are never merged.
export function bundlesOf(edges, zoneOf) {
  const out = new Map();
  for (const e of edges) {
    if (e.data?.kind === 'rule') continue;
    const za = zoneOf(e.source), zb = zoneOf(e.target);
    if (za === undefined || zb === undefined || za === zb) continue;
    const [a, b] = za < zb ? [za, zb] : [zb, za], key = `${a}\n${b}`;
    const bundle = out.get(key) || {key, a, b, edges: [], live: null, idle: null, bad: false, quiet: true};
    bundle.edges.push(e.id);
    // Only lines that carry nothing: drawn quiet like they are.
    if (!e.data?.quiet) bundle.quiet = false;
    const ms = e.data?.rttMs, side = e.data?.quiet ? 'idle' : 'live';
    if (Number.isFinite(ms) && ms > 0) bundle[side] = bundle[side] === null ? ms : Math.min(bundle[side], ms);
    if (['bad', 'down', 'suppressed', 'failing'].includes(e.data?.look)) bundle.bad = true;
    out.set(key, bundle);
  }
  // The fastest line that carries traffic; the idle ones only when none has a time.
  return [...out.values()].map(({live, idle, ...b}) => ({...b, best: live ?? idle}));
}

// Where a line between two boxes leaves the first one, aimed at the second's centre.
export function borderPoint(box, toward) {
  const cx = box.x + box.w / 2, cy = box.y + box.h / 2, dx = toward.x - cx, dy = toward.y - cy;
  if (!dx && !dy) return {x: cx, y: cy};
  const s = Math.min(dx ? (box.w / 2) / Math.abs(dx) : Infinity, dy ? (box.h / 2) / Math.abs(dy) : Infinity);
  return {x: cx + dx * s, y: cy + dy * s};
}
