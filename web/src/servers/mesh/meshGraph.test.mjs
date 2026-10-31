import test from 'node:test';
import assert from 'node:assert/strict';
import { addLink, alignTo, avoidBend, drawnRule, edgeGeometry, legendText, lineIdOf, nextSlot, phoneLayout, planLink, ruleEnds, ruleNodes } from './meshGraph.js';

const m = (serverId, role, extra = {}) => ({serverId, name: serverId.toUpperCase(), role, listen: {udp: role === 'relay' ? 1 : 0, tcp: 0, tls: 0}, entry: role === 'relay' ? null : {mode: 'fixed', candidates: []}, tags: [], ...extra});
const work = () => ({members: [m('r1', 'relay', {listen: {udp: 0, tcp: 2, tls: 0}}), m('r2', 'relay'), m('a', 'member', {tags: ['x']}), m('b', 'member', {listen: {udp: 0, tcp: 3, tls: 0}})],
  relayLinks: {mode: 'full', pairs: []}, directLinks: [], rules: []});

test('links follow the roles at both ends, with the reason when they cannot be made', () => {
  const w = work();
  // Either direction makes the member's entry; the relay's first open transport is used.
  assert.deepEqual(planLink(w, 'r1', 'a'), {kind: 'entry', member: 'a', relay: 'r1', transport: 'tcp'});
  addLink(w, planLink(w, 'a', 'r1'));
  assert.match(planLink(w, 'a', 'r1').error, /开启的传输方式都已经是 A 的入口/);
  assert.match(planLink(w, 'r1', 'r2').error, /两两相连/);
  w.relayLinks.mode = 'custom';
  assert.equal(planLink(w, 'r1', 'r2').kind, 'relay');
  // Direct links: UDP (hole-punched) by default; UDP listening is turned on at both ends.
  assert.deepEqual(planLink(w, 'a', 'b'), {kind: 'direct', a: 'a', b: 'b', transport: 'udp'});
  const made = addLink(w, planLink(w, 'a', 'b'));
  assert.deepEqual(made.opened, ['A', 'B']);
  assert.deepEqual([w.members[2].listen.udp, w.members[3].listen], [51999, {udp: 51999, tcp: 3, tls: 0}]);
  assert.match(planLink(w, 'b', 'a').error, /已经直连/);
  w.directLinks = [];
  assert.match(planLink(w, 'a', 'r2', 'direct').error, /R2 是中转/);
  assert.match(planLink(w, 'a', 'b', 'entry').error, /B 不是中转/);
  assert.match(planLink(w, 'a', 'a').error, /它自己/);
  w.members[3].removing = true;
  assert.match(planLink(w, 'a', 'b').error, /正在移除/);
  const sel = addLink(w, planLink(w, 'a', 'r2', 'rule'));
  assert.equal(sel.kind, 'rule');
  assert.deepEqual([w.rules[0].src.members, w.rules[0].dst.members], [['a'], ['r2']]);
});

test('rules cover members, tags and everyone; layout rows', () => {
  const w = work();
  assert.deepEqual(ruleEnds({src: {tags: ['x']}, dst: {members: ['b']}}, w.members), [{a: 'a', b: 'b', both: false}]);
  // All members on both sides: every pair, both ways; a side with nothing chosen covers nobody.
  assert.equal(ruleEnds({src: {all: true}, dst: {all: true}}, w.members).length, 6);
  assert.ok(ruleEnds({src: {all: true}, dst: {all: true}}, w.members).every(e => e.both));
  assert.deepEqual(ruleEnds({src: {}, dst: {}}, w.members), []);
  assert.deepEqual([...ruleNodes({src: {all: true}, dst: {members: ['b']}}, w.members)].sort(), w.members.map(m => m.serverId).sort());
  // Without a saved layout each node goes to the end of its row: relays on top, members below.
  assert.deepEqual(nextSlot({}, [], true), {x: 0, y: 0});
  assert.deepEqual(nextSlot({r1: {x: 0, y: 0}}, ['r1'], true), {x: 230, y: 0});
  assert.deepEqual(nextSlot({r1: {x: 0, y: 0}, r2: {x: 230, y: 0}}, [], false), {x: 0, y: 210});
  assert.deepEqual(nextSlot({r1: {x: -115, y: 0}, a: {x: 0, y: 210}}, ['a']), {x: 230, y: 210});
  assert.deepEqual(nextSlot({a: {x: 0, y: 100}}, [], true), {x: 0, y: -110});
});

test('only rules that name machines become arrows; phones lay out top-down', () => {
  const w = work();
  w.members[2].gateway = {subnets: ['192.168.1.0/24']};
  const side = (o = {}) => ({members: [], tags: [], prefixes: [], subnets: [], ...o});
  assert.equal(drawnRule({src: side({members: ['a']}), dst: side({subnets: ['192.168.1.0/24']})}, w.members), true);
  assert.equal(drawnRule({src: side(), dst: side()}, w.members), false, 'everyone');
  assert.equal(drawnRule({src: side({all: true}), dst: side({all: true})}, w.members), false, 'all members: in the legend');
  assert.equal(drawnRule({src: side({members: ['a']}), dst: side({all: true})}, w.members), false, 'all members as the target');
  assert.equal(drawnRule({src: side({tags: ['x']}), dst: side({members: ['b']})}, w.members), false, 'a tag');
  assert.equal(drawnRule({src: side({members: ['a']}), dst: side({subnets: ['10.9.0.0/16']})}, w.members), false, 'a network behind no gateway');
  assert.deepEqual([...ruleNodes({src: side({tags: ['x']}), dst: side({members: ['b']})}, w.members)].sort(), ['a', 'b']);
  const p = phoneLayout(['r1', 'r2', 'a', 'b', 'c'], id => id.startsWith('r'));
  assert.deepEqual(p, {r1: {x: 0, y: 0}, r2: {x: 170, y: 0}, a: {x: 0, y: 156}, b: {x: 170, y: 156}, c: {x: 85, y: 288}});
});

test('entries: LAN or internet once a punched entry is through, punching until then; standbys while another is in use', async () => {
  const { entryHow, entryLook } = await import('./meshGraph.js');
  const relay = (serverId, mode) => m(serverId, 'relay', {relayMode: mode, listen: {udp: 1, tcp: 1, tls: 0}});
  const mac = m('mac', 'member', {entry: {mode: 'failover', candidates: [{id: 'c1', relay: 'toy', transport: 'udp'}, {id: 'c2', relay: 'sg', transport: 'tcp'}]}});
  const w = {members: [relay('toy', 'site'), relay('sg', 'public'), mac]};
  const data = health => ({nodes: {mac: {summary: {entry: {candidate: health[0].state === 'active' ? 'c1' : ''}, entryHealth: health}}}});
  const look = (health, i) => entryLook(w, data(health), [], mac, i);
  const lan = [{id: 'c1', state: 'active', via: 'lan'}, {id: 'c2', state: 'ready', via: ''}];
  assert.equal(entryHow(look(lan, 0), 'udp'), '局域网');
  assert.equal(entryHow(look([{...lan[0], via: 'public'}, lan[1]], 0), 'udp'), '公网');
  assert.equal(entryHow(look([{id: 'c1', state: 'punching', via: ''}, {id: 'c2', state: 'active', via: ''}], 0), 'udp'), '打洞中');
  // Ordinary candidates keep their transport; without any report a punched one only says it punches.
  assert.equal(entryHow(look(lan, 1), 'tcp'), 'TCP');
  assert.equal(entryHow(entryLook(w, {nodes: {}}, [], mac, 0), 'udp'), '打洞');
  assert.deepEqual([look(lan, 0).standby, look(lan, 1).standby], [false, true]);
  assert.equal(entryLook(w, {nodes: {}}, [], mac, 1).standby, false, 'nothing known in use: no standby');
});

test('real-data review: cold standbys say so; a rule among the same machines goes to the legend', async () => {
  const { drawnRule, entryLook, standbyNote } = await import('./meshGraph.js');
  const relay = id => m(id, 'relay', {listen: {udp: 1, tcp: 1, tls: 0}});
  const mac = m('mac', 'member', {entry: {mode: 'failover', candidates: [{id: 'c1', relay: 'r1', transport: 'udp'}, {id: 'c2', relay: 'r2', transport: 'tcp'}, {id: 'c3', relay: 'r3', transport: 'tcp'}]}});
  const w = {members: [relay('r1'), relay('r2'), relay('r3'), mac]};
  const data = {nodes: {mac: {summary: {entry: {candidate: 'c1'}, entryHealth: [{id: 'c1', state: 'active'}, {id: 'c2', state: 'ready'}, {id: 'c3', state: 'untested'}]}}}};
  assert.deepEqual([1, 2].map(i => standbyNote(entryLook(w, data, [], mac, i))), ['热备，平时不走流量', '冷备，平时不连，只定时试连']);
  const ids = w.members.map(x => x.serverId);
  assert.equal(drawnRule({src: {members: ids, tags: [], prefixes: []}, dst: {members: ids, tags: [], subnets: []}, proto: 'icmp'}, w.members), false);
  assert.equal(drawnRule({src: {members: ['r1', 'mac'], tags: [], prefixes: []}, dst: {members: ['mac', 'r1'], tags: [], subnets: []}, proto: 'icmp'}, w.members), true);
  assert.equal(drawnRule({src: {members: ['r1'], tags: [], prefixes: []}, dst: {members: ids, tags: [], subnets: []}, proto: 'icmp'}, w.members), true);
});

test('an entry through a relay whose mesh service is gone says so', async () => {
  const { entryLook } = await import('./meshGraph.js');
  const w = work();
  w.members[2].entry = {mode: 'fixed', candidates: [{id: 'c1', relay: 'r2', transport: 'udp'}]};
  const look = entryLook(w, {nodes: {r2: {state: 'missing', error: 'mesh_service_missing'}}}, [], w.members[2], 0);
  assert.equal(look.problem, 'R2 的组网服务不见了');
  assert.equal(look.look, 'bad');
});

test('dragging lines a node up with the others: edges and centres snap when close, with guides spanning what lines up', () => {
  const others = [{x: 0, y: 0, w: 184, h: 64}, {x: 300, y: 200, w: 184, h: 64}];
  // 4 px from the first one's left edge and 3 px from the second one's top: both axes snap.
  const r = alignTo({x: 4, y: 203, w: 184, h: 64}, others, 6);
  assert.deepEqual([r.x, r.y], [0, 200]);
  assert.deepEqual(r.guides, [{axis: 'x', at: 0, from: 0, to: 264}, {axis: 'y', at: 200, from: 0, to: 484}]);
  // Centres: a narrower box whose centre is near the other's centre.
  const c = alignTo({x: 40, y: 400, w: 100, h: 40}, others, 6);
  assert.equal(c.x + 50, 92);
  // Out of reach: where it was, no guides; the threshold is in board units (6 screen px / zoom).
  assert.deepEqual(alignTo({x: 20, y: 120, w: 184, h: 64}, others, 6), {x: 20, y: 120, guides: []});
  assert.equal(alignTo({x: 20, y: 120, w: 184, h: 64}, others, 6 / 0.25).x, 0);
});

test('board links map to the controller\'s line ids; the legend names every gateway network', () => {
  const w = work();
  w.members[2].entry.candidates = [{id: 'c1', relay: 'r2', transport: 'udp'}];
  w.relayLinks = {mode: 'custom', pairs: [{a: 'r2', b: 'r1', transport: 'udp'}]};
  w.directLinks = [{a: 'b', b: 'a', transport: 'udp'}];
  assert.equal(lineIdOf(w, 'entry:a:c1'), 'entry:a|r2|udp');
  assert.equal(lineIdOf(w, 'relay:r2~r1'), 'relay:r1|r2');
  assert.equal(lineIdOf(w, 'pair:0'), 'relay:r1|r2');
  assert.equal(lineIdOf(w, 'direct:0'), 'direct:a|b');
  assert.equal(lineIdOf(w, 'entry:a:gone'), '');
  assert.equal(lineIdOf(w, 'rule:r1:a|b'), '');
  w.members[3].gateway = {subnets: ['192.168.1.0/24']};
  const side = (o = {}) => ({all: false, allSubnets: false, members: [], tags: [], prefixes: [], subnets: [], ...o});
  const both = {src: side({all: true, allSubnets: true}), dst: side({all: true}), proto: 'icmp', ports: []};
  assert.equal(legendText(both, w.members), '所有成员、所有网关网段 → 所有成员 · ping');
  assert.equal(legendText({src: side({members: ['a']}), dst: side({allSubnets: true, subnets: ['10.0.0.0/8']}), proto: 'tcp', ports: [[22, 22]]}, w.members), 'A → 所有网关网段、10.0.0.0/8 · TCP 22');
  assert.equal(legendText({src: side({all: true}), dst: side({all: true}), proto: 'icmp', ports: []}, w.members), '所有成员 → 所有成员 · ping');
  // Every gateway network on a side is never an arrow: it goes to the legend, like everyone.
  assert.equal(drawnRule({src: side({members: ['a']}), dst: side({allSubnets: true})}, w.members), false);
  assert.equal(drawnRule({src: side({members: ['a']}), dst: side({subnets: ['192.168.1.0/24']})}, w.members), true);
  // Highlighting covers the gateways of every network.
  assert.deepEqual([...ruleNodes({src: side({members: ['a']}), dst: side({allSubnets: true})}, w.members)].sort(), ['a', 'b']);
});

test('a link that would run under another node bows round it, just enough, to the emptier side', () => {
  const box = (x, y, w = 184, h = 82) => ({cx: x + w / 2, cy: y + h / 2, w, h});
  // On the curve as MeshEdge draws it, never inside a box (plus the margin).
  const clear = (a, b, bend, others, pad) => Array.from({length: 201}, (_, i) => i / 200).every(k => {
    const {s, c, t} = edgeGeometry(a, b, bend), x = (1 - k) ** 2 * s.x + 2 * k * (1 - k) * c.x + k ** 2 * t.x, y = (1 - k) ** 2 * s.y + 2 * k * (1 - k) * c.y + k ** 2 * t.y;
    return others.every(o => Math.abs(x - o.cx) >= o.w / 2 + pad || Math.abs(y - o.cy) >= o.h / 2 + pad);
  });
  const sg = box(20, 260), zgo = box(320, 260), vmiss = box(620, 260);
  // Nothing in the way: straight.
  assert.equal(avoidBend(sg, vmiss, []), 0);
  assert.equal(avoidBend(sg, vmiss, [box(320, 500)]), 0);
  // Zgo right between them: a bow that clears it by the margin, no wider than needed.
  const bend = avoidBend(sg, vmiss, [zgo]);
  assert.ok(bend && clear(sg, vmiss, bend, [zgo], 10), `bend ${bend}`);
  assert.ok(!clear(sg, vmiss, bend - Math.sign(bend) * 12, [zgo], 10), 'a smaller bow would not do');
  // Nodes below the line: it bows up (edgeGeometry bends a left-to-right link down for a positive bend).
  const up = avoidBend(sg, vmiss, [zgo, box(170, 420), box(470, 420)]);
  assert.ok(up < 0 && clear(sg, vmiss, up, [zgo, box(170, 420), box(470, 420)], 10), `bend ${up}`);
  // A phone allows only small bows: with no way round within them the link stays straight.
  const a = box(0, 0, 150, 52), b = box(0, 400, 150, 52), between = [box(0, 132, 150, 52), box(0, 264, 150, 52)];
  assert.equal(avoidBend(a, b, between, 10, 112), 0);
  // In a tight grid, the narrow way between two nodes is found with a smaller margin.
  const P = {sg: [20, 260], zgo: [320, 260], vmiss: [620, 260], home: [20, 490], toy: [320, 490], mac: [620, 490], ks: [20, 60], sea: [320, 60], lax: [620, 60], berlin: [20, 690], win: [620, 690]};
  const others = Object.entries(P).filter(([id]) => id !== 'win' && id !== 'sg').map(([, [x, y]]) => box(x, y));
  const narrow = avoidBend(box(...P.win), box(...P.sg), others);
  assert.ok(narrow && clear(box(...P.win), box(...P.sg), narrow, others, 4), `bend ${narrow}`);
});
