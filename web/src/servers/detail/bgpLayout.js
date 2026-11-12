// BGP 上游 laid out the way bgp.tools draws it, left to right: the origin, the upstreams it goes through
// (a column per step), then the Tier 1 networks in a band, a Tier 1 reached only behind another one further
// right inside it. Boxes are one line (AS number and name) so fifteen Tier 1s stay under 500 pixels; a column
// of upstreams longer than `most` keeps the ones most peers go through and folds the rest into one box.
// Lines leave a box on its right, turn in the channel before the target's column (one upright per source, so
// a source's lines share a trunk) and come into the target from the left; no box sits on a line that passes
// through its column. Everything in CSS pixels at scale 1.
export const BOX = {w: 140, h: 26, pitch: 32, channel: 56, pad: 14, label: 22};

export function bgpLayout(data, {most = 8} = {}) {
  const {w, h, pitch, channel, pad, label} = BOX;
  const origins = new Set(data.origins || []);
  const nodes = new Map((data.nodes || []).map(n => [String(n.asn), {key: String(n.asn), asn: n.asn, name: n.name || '', tier1: n.tier1, origin: origins.has(n.asn)}]));
  for (const asn of origins) if (!nodes.has(String(asn))) nodes.set(String(asn), {key: String(asn), asn, name: data.originNames?.[asn] || '', tier1: false, origin: true});
  let edges = (data.edges || []).map(e => ({from: String(e.from), to: String(e.to), peers: e.peers})).filter(e => nodes.has(e.from) && nodes.has(e.to));

  // Columns: upstreams by their fewest steps from an origin; the band after the last of them.
  const out = key => edges.filter(e => e.from === key), into = key => edges.filter(e => e.to === key);
  const step = new Map([...nodes.values()].filter(n => n.origin).map(n => [n.key, 0]));
  for (const queue = [...step.keys()]; queue.length;) {
    const key = queue.shift();
    for (const e of out(key)) if (!nodes.get(e.to).tier1 && !step.has(e.to)) { step.set(e.to, step.get(key) + 1); queue.push(e.to); }
  }
  const last = Math.max(0, ...[...nodes.values()].filter(n => !n.tier1).map(n => step.get(n.key) ?? 1));
  const band = last + 1;
  const firstT1 = new Set(edges.filter(e => !nodes.get(e.from).tier1 && nodes.get(e.to).tier1).map(e => e.to));
  for (const n of nodes.values()) n.col = n.tier1 ? (firstT1.has(n.key) ? band : band + 1) : n.origin ? 0 : (step.get(n.key) ?? 1);
  const weight = key => into(key).reduce((s, e) => s + e.peers, 0);

  // A column of more upstreams than fit: the busiest stay, the rest become one box.
  for (let col = 1; col <= last; col++) {
    const here = [...nodes.values()].filter(n => n.col === col).sort((a, b) => weight(b.key) - weight(a.key));
    if (here.length <= most) continue;
    const folded = here.slice(most - 1), key = `more-${col}`;
    nodes.set(key, {key, asn: null, name: `另 ${folded.length} 个上游`, col, more: folded.map(n => ({asn: n.asn, name: n.name}))});
    const gone = new Set(folded.map(n => n.key));
    for (const k of gone) nodes.delete(k);
    const merged = new Map();
    for (const e of edges) {
      const from = gone.has(e.from) ? key : e.from, to = gone.has(e.to) ? key : e.to, id = `${from}>${to}`;
      merged.set(id, {from, to, peers: (merged.get(id)?.peers || 0) + e.peers});
    }
    edges = [...merged.values()];
  }

  const list = [...nodes.values()];
  const column = col => list.filter(n => n.col === col);
  const cols = Math.max(...list.map(n => n.col)) + 1;
  const mid = y => y + h / 2;

  // The band's first column: grouped by the source most peers came from (origins first, then by column), by AS number within.
  const sourceOf = key => into(key).sort((a, b) => b.peers - a.peers)[0]?.from;
  const rank = key => { const n = nodes.get(key); return n ? n.col * 1e7 + (n.origin ? 0 : 1e6 - Math.min(weight(key), 999999)) : 1e9; };
  const t1 = column(band).sort((a, b) => rank(sourceOf(a.key)) - rank(sourceOf(b.key)) || a.asn - b.asn);
  t1.forEach((n, i) => { n.y = i * pitch; });
  // Behind the band's first column: beside the one each came by.
  stack(column(band + 1).map(n => ({n, want: nodes.get(sourceOf(n.key))?.y ?? 0})), []);
  // Upstreams from the last column back: at the middle of what they lead to.
  const centre = key => { const ys = out(key).map(e => nodes.get(e.to).y).filter(y => y != null); return ys.length ? ys.reduce((s, y) => s + y, 0) / ys.length : null; };
  for (let col = last; col >= 0; col--) {
    column(col).forEach(n => { n.want = centre(n.key) ?? 0; });
    stack(column(col).map(n => ({n, want: n.want})), []);
  }
  // A line from an earlier column runs through the ones it skips at its source's height: no box there.
  for (let col = 1; col <= last; col++) {
    const through = edges.filter(e => nodes.get(e.from).col < col && nodes.get(e.to).col > col).map(e => nodes.get(e.from).y);
    stack(column(col).map(n => ({n, want: n.want})), through);
  }

  function stack(items, blocked) {
    let floor = -Infinity;
    for (const item of items.sort((a, b) => a.want - b.want)) {
      let y = Math.max(item.want, floor);
      for (let moved = true; moved;) {
        moved = false;
        for (const b of blocked) if (y > b - pitch + 2 && y < b + pitch - 2) { y = b + pitch; moved = true; }
      }
      item.n.y = y; floor = y + pitch;
    }
  }

  // Into place: the band's label above everything, then x by column.
  const top = Math.min(...list.map(n => n.y));
  const colX = col => pad + (col >= band ? 10 : 0) + col * (w + channel);
  for (const n of list) { n.y = Math.round(n.y - top + pad + label); n.x = colX(n.col); }
  const bottom = Math.max(...list.map(n => n.y)) + h;

  // Lines: an upright per source in the channel before the target column, sources left to right by height.
  const lines = [];
  for (let col = 1; col < cols; col++) {
    const coming = edges.filter(e => nodes.get(e.to).col === col);
    const sources = [...new Set(coming.map(e => e.from))].sort((a, b) => nodes.get(a).y - nodes.get(b).y || nodes.get(a).col - nodes.get(b).col);
    const left = colX(col) - channel + 10, right = colX(col) - 14 - (col === band ? 10 : 0);
    sources.forEach((key, i) => {
      const x = Math.round(sources.length === 1 ? (left + right) / 2 : left + (right - left) * i / (sources.length - 1));
      const from = nodes.get(key), ys = mid(from.y);
      for (const e of coming.filter(c => c.from === key)) {
        const to = nodes.get(e.to), yt = mid(to.y), end = to.x - 2;
        const d = ys === yt ? `M${from.x + w} ${ys}H${end}` : `M${from.x + w} ${ys}H${x}V${yt}H${end}`;
        lines.push({from: e.from, to: e.to, peers: e.peers, d});
      }
    });
  }

  const inBand = list.filter(n => n.col >= band);
  const bandBox = inBand.length ? {x: colX(band) - 10, y: Math.min(...inBand.map(n => n.y)) - label, w: colX(Math.max(...inBand.map(n => n.col))) + w + 10 - (colX(band) - 10),
    h: Math.max(...inBand.map(n => n.y)) + h + 10 - (Math.min(...inBand.map(n => n.y)) - label)} : null;
  const starts = column(0);
  const originBox = starts.length ? {x: pad - 6, y: Math.min(...starts.map(n => n.y)) - label + 2, w: w + 12, h: Math.max(...starts.map(n => n.y)) + h + 6 - (Math.min(...starts.map(n => n.y)) - label + 2)} : null;
  const width = Math.max(...list.map(n => n.x + w), bandBox ? bandBox.x + bandBox.w : 0) + pad;
  const height = Math.max(bottom + 10, bandBox ? bandBox.y + bandBox.h : 0, originBox ? originBox.y + originBox.h : 0) + pad;
  return {width, height, band: bandBox, origin: originBox,
    nodes: list.map(n => ({key: n.key, asn: n.asn, name: n.name, x: n.x, y: n.y, w, h, kind: n.origin ? 'origin' : n.tier1 ? 'tier1' : n.more ? 'more' : 'transit', more: n.more || null})),
    lines};
}

// Every line on a path through key: what leads to it and what it leads to.
export function pathThrough(lines, key) {
  const on = new Set(), up = [key], down = [key], seenUp = new Set(up), seenDown = new Set(down);
  while (up.length) { const k = up.pop(); for (const l of lines) if (l.to === k) { on.add(l); if (!seenUp.has(l.from)) { seenUp.add(l.from); up.push(l.from); } } }
  while (down.length) { const k = down.pop(); for (const l of lines) if (l.from === k) { on.add(l); if (!seenDown.has(l.to)) { seenDown.add(l.to); down.push(l.to); } } }
  return {lines: on, keys: new Set([...seenUp, ...seenDown])};
}

// The same network for a phone, as a tree: each AS under the one most peers reached it from, the upstreams it
// goes through as branches, the Tier 1s it reaches as a row, and a Tier 1 met only behind another named with it.
export function bgpTree(data) {
  const names = new Map((data.nodes || []).map(n => [n.asn, n]));
  const lines = (data.edges || []).filter(e => e.from !== e.to);
  const parent = new Map();
  for (const e of lines) if (!parent.has(e.to) || e.peers > parent.get(e.to).peers) parent.set(e.to, e);
  const kids = asn => lines.filter(e => parent.get(e.to) === e && e.from === asn).map(e => ({...item(e.to), peers: e.peers}));
  const item = asn => ({asn, name: names.get(asn)?.name || data.originNames?.[asn] || '', tier1: Boolean(names.get(asn)?.tier1)});
  const seen = new Set();
  const grow = asn => {
    seen.add(asn);
    const below = kids(asn).filter(k => !seen.has(k.asn));
    const tier1 = below.filter(k => k.tier1).sort((a, b) => a.asn - b.asn);
    return {...item(asn), transit: below.filter(k => !k.tier1).sort((a, b) => b.peers - a.peers).map(k => ({...grow(k.asn), peers: k.peers})),
      tier1, behind: tier1.map(t => ({via: t, tier1: kids(t.asn).filter(k => k.tier1).sort((a, b) => a.asn - b.asn)})).filter(b => b.tier1.length)};
  };
  return (data.origins || []).map(asn => ({...grow(asn), origin: true}));
}

// What is said of an AS under the graph: where its lines come from and how many peers each carries.
export function bgpInfo(data, asn) {
  const node = (data.nodes || []).find(n => n.asn === asn);
  const name = a => { const n = (data.nodes || []).find(x => x.asn === a); return `AS${a}${n?.name ? ` ${n.name}` : data.originNames?.[a] ? ` ${data.originNames[a]}` : ''}`; };
  if ((data.origins || []).includes(asn)) return {title: name(asn), rows: [`宣告 ${data.prefix}`, `${data.via} 条路径经过 Tier 1（共看到 ${data.routes} 条）`]};
  const into = (data.edges || []).filter(e => e.to === asn).sort((a, b) => b.peers - a.peers);
  return {title: name(asn), rows: [node?.tier1 ? 'Tier 1' : '上游', ...into.map(e => `来自 ${name(e.from)} · ${e.peers} 个观测点`)]};
}
