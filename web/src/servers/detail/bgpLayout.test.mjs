// BGP 上游's layout on real RIS shapes: columns, the band, folding a long column, no box on a line that passes
// through its column, the paths lit through an AS, the phone's tree and the line said under the graph.
import test from 'node:test';
import assert from 'node:assert/strict';
import BGP from '../testing/bgpSamples.json' with {type: 'json'};
import { BOX, bgpInfo, bgpLayout, bgpTree, pathThrough } from './bgpLayout.js';

const at = (layout, asn) => layout.nodes.find(n => n.asn === asn);

test('columns: the origin, its upstreams a step each, the Tier 1 band, a Tier 1 met only behind another further right', () => {
  const l = bgpLayout(BGP.prime);
  const col = asn => (at(l, asn).x - BOX.pad) / (BOX.w + BOX.channel) | 0;
  assert.deepEqual([400618, 140096, 9886, 17676].map(col), [0, 1, 2, 3]);
  assert.ok(at(l, 2914).x === at(l, 3491).x && at(l, 1299).x > at(l, 2914).x, 'NTT and PCCW in the band, Arelion behind NTT');
  assert.deepEqual(l.nodes.filter(n => n.kind === 'origin').map(n => n.asn), [400618]);
  assert.ok(l.band.x < at(l, 2914).x && l.band.x + l.band.w > at(l, 1299).x + BOX.w, 'the band takes in both columns');
  // Boxes never overlap; every line ends at its target's left edge.
  const boxes = l.nodes.map(n => [n.x, n.y, n.x + n.w, n.y + n.h]);
  for (const [i, a] of boxes.entries()) for (const b of boxes.slice(i + 1)) assert.ok(a[2] <= b[0] || b[2] <= a[0] || a[3] <= b[1] || b[3] <= a[1], `${a} and ${b}`);
  for (const line of l.lines) assert.ok(line.d.endsWith(`H${l.nodes.find(n => n.key === line.to).x - 2}`), line.d);
  // Fifteen Tier 1s in a column stay under 500 pixels.
  assert.ok(bgpLayout(BGP.ovh).height < 500 && bgpLayout(BGP.google).height < 500);
});

test('a line through a column it skips has no box at its height there', () => {
  // OVH reaches five Tier 1s itself and the rest through Telstra, one column further.
  const l = bgpLayout(BGP.ovh), origin = at(l, 16276), telstra = at(l, 4637);
  assert.ok(telstra.x > origin.x && Math.abs(telstra.y - origin.y) >= BOX.pitch - 2, 'Telstra off the line the origin draws across its column');
});

test('a column of more upstreams than fit: the busiest stay, the rest are one box', () => {
  const nodes = [{asn: 1, name: 'origin'}, ...Array.from({length: 10}, (_, i) => ({asn: 100 + i, name: `up${i}`})), {asn: 3356, name: 'Lumen', tier1: true}];
  const edges = Array.from({length: 10}, (_, i) => [{from: 1, to: 100 + i, peers: 10 - i}, {from: 100 + i, to: 3356, peers: 10 - i}]).flat();
  const l = bgpLayout({origins: [1], nodes, edges}, {most: 4});
  assert.deepEqual(l.nodes.filter(n => n.kind === 'transit').map(n => n.asn).sort(), [100, 101, 102]);
  const folded = l.nodes.find(n => n.kind === 'more');
  assert.equal(folded.name, '另 7 个上游');
  assert.deepEqual(folded.more.map(m => m.asn).sort(), [103, 104, 105, 106, 107, 108, 109]);
  assert.equal(l.lines.filter(x => x.to === folded.key).length, 1, 'their lines merged into one');
});

test('the paths through an AS, the phone tree, and what is said of an AS', () => {
  const l = bgpLayout(BGP.ovh), lit = pathThrough(l.lines, '4637');
  assert.deepEqual([...lit.keys].sort(), ['1299', '16276', '3257', '3356', '3491', '4637', '6461', '6762']);
  const [ovh] = bgpTree(BGP.ovh);
  assert.deepEqual([ovh.asn, ovh.origin, ovh.tier1.map(t => t.name), ovh.behind.map(b => [b.via.name, b.tier1.map(t => t.name)])],
    [16276, true, ['Cogent', 'NTT', 'DTAG', 'TATA', 'Telxius'], [['TATA', ['AT&T']], ['Telxius', ['Orange', 'Liberty']]]]);
  assert.deepEqual([ovh.transit[0].asn, ovh.transit[0].tier1.length], [4637, 6]);
  assert.deepEqual(bgpInfo(BGP.ovh, 4637), {title: 'AS4637 Telstra', rows: ['上游', '来自 AS16276 OVH · 44 个观测点']});
  assert.deepEqual(bgpInfo(BGP.ovh, 16276).rows, ['宣告 203.0.113.0/24', '221 条路径经过 Tier 1（共看到 333 条）']);
});
