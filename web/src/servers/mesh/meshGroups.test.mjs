import test from 'node:test';
import assert from 'node:assert/strict';
import { arrange, borderPoint, bundlesOf, chipPlaces, clusterBox, containerOf, zoneBox } from './meshGroups.js';

const member = (id, cands) => ({serverId: id, role: 'member', entry: {mode: 'failover', candidates: cands.map(([cid, relay]) => ({id: cid, relay, transport: 'udp'}))}});

test('a member is drawn in the relay it enters by now, else its first candidate', () => {
  const relays = new Set(['sg', 'berlin']);
  const ovh = member('ovh', [['c1', 'berlin'], ['c2', 'sg']]);
  assert.deepEqual(containerOf(ovh, relays, {relay: 'sg', candidate: 'c2'}), {relay: 'sg', candidate: 'c2'});
  assert.deepEqual(containerOf(ovh, relays, null), {relay: 'berlin', candidate: 'c1'});
  // An entry to a relay that is not on the board, no candidates, or a relay itself: on its own.
  assert.deepEqual(containerOf(ovh, new Set(['sg'])), null);
  assert.equal(containerOf(member('x', []), relays), null);
  assert.equal(containerOf({serverId: 'sg', role: 'relay'}, relays), null);
  const up = {serverId: 'up', role: 'member', entry: {mode: 'upstreams', candidates: [{id: 'c1', relay: 'berlin', transport: 'udp'}, {id: 'c2', relay: 'sg', transport: 'tcp'}]}};
  assert.deepEqual(containerOf(up, relays, null), {relay: 'berlin', candidate: 'c1'});
  assert.deepEqual(containerOf(up, new Set(['sg']), null), {relay: 'sg', candidate: 'c2'});
});

test('members stack under their relay inside its box', () => {
  assert.deepEqual(chipPlaces({x: 100, y: 50}, 80, 2), [{x: 108, y: 138}, {x: 108, y: 172}]);
  // A relay announcing networks shows them under itself: its members start below that.
  assert.equal(chipPlaces({x: 0, y: 0}, 80, 1, true)[0].y, 114);
  assert.deepEqual(clusterBox({x: 100, y: 50}, 184, 80, 2), {x: 92, y: 42, w: 200, h: 168});
  assert.deepEqual(clusterBox({x: 0, y: 0}, 184, 80, 0), {x: -8, y: -8, w: 200, h: 96});
});

test('linked units sit next to each other, the same every time', () => {
  const units = ['a', 'b', 'c', 'd', 'e', 'f'].map(id => ({id, w: 184, h: 80, zone: ''}));
  const links = [['a', 'd', 5], ['b', 'e', 5], ['c', 'f', 5], ['a', 'b', 1]];
  const first = arrange(units, links), again = arrange([...units].reverse(), [...links].reverse());
  assert.deepEqual(first, again);
  const at = first.units, gap = (x, y) => Math.abs(at[x].x - at[y].x) + Math.abs(at[x].y - at[y].y);
  // Each strongly linked pair is one step apart, no two units share a place.
  for (const [x, y] of [['a', 'd'], ['b', 'e'], ['c', 'f']]) assert.ok(gap(x, y) <= 224 + 120, `${x}-${y} ${gap(x, y)}`);
  assert.equal(new Set(Object.values(at).map(p => `${p.x},${p.y}`)).size, 6);
});

test('groups are boxes of their own, linked groups placed side by side, no unit outside its box', () => {
  const units = [
    {id: 'cn1', w: 200, h: 160, zone: 'CN'}, {id: 'cn2', w: 184, h: 80, zone: 'CN'}, {id: 'cn3', w: 184, h: 80, zone: 'CN'},
    {id: 'de1', w: 200, h: 120, zone: 'DE'}, {id: 'us1', w: 200, h: 200, zone: 'US'}, {id: 'sg1', w: 184, h: 80, zone: 'SG'}];
  const links = [['cn1', 'cn2'], ['cn2', 'cn3'], ['cn1', 'de1'], ['cn1', 'us1'], ['cn3', 'sg1', 3], ['sg1', 'us1']];
  const {units: at, zones} = arrange(units, links, {grouped: true});
  assert.deepEqual(Object.keys(zones).sort(), ['CN', 'DE', 'SG', 'US']);
  for (const u of units) {
    const z = zones[u.zone], p = at[u.id];
    assert.ok(p.x >= z.x && p.y >= z.y && p.x + u.w <= z.x + z.w && p.y + u.h <= z.y + z.h, `${u.id} inside ${u.zone}`);
  }
  const boxes = Object.values(zones);
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const a = boxes[i], b = boxes[j];
    assert.ok(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y, 'groups do not overlap');
  }
  // Without grouping the zones are not used.
  assert.deepEqual(arrange(units, links).zones, {});
});

test('three groups linked to each other are not lined up, so no line runs across the middle one', () => {
  const units = [{id: 'a', w: 200, h: 100, zone: 'A'}, {id: 'b', w: 400, h: 300, zone: 'B'}, {id: 'c', w: 200, h: 100, zone: 'C'}];
  const {zones} = arrange(units, [['a', 'b', 10], ['b', 'c', 9], ['a', 'c', 1]], {grouped: true});
  const mid = z => ({x: z.x + z.w / 2, y: z.y + z.h / 2}), [p, q, r] = ['A', 'B', 'C'].map(k => mid(zones[k]));
  assert.ok(Math.abs((q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)) > 1, 'not on one line');
});

test('the links between two groups merge into one, with the fastest line in use', () => {
  const zone = {a: 'CN', b: 'CN', c: 'DE', d: 'DE', e: 'US'};
  const e = (id, source, target, data) => ({id, source, target, data: {kind: 'relay', ...data}});
  const list = bundlesOf([
    e('1', 'a', 'c', {rttMs: 170}), e('2', 'b', 'd', {rttMs: 160, quiet: true}), e('3', 'c', 'a', {rttMs: 180}),
    e('4', 'a', 'b', {rttMs: 2}), e('5', 'a', 'e', {rttMs: 175, look: 'down'}), e('6', 'a', 'x', {}),
    {id: 'r', source: 'a', target: 'c', data: {kind: 'rule'}}], id => zone[id]);
  assert.deepEqual(list.map(b => [b.key, b.edges, b.best, b.bad]), [['CN\nDE', ['1', '2', '3'], 170, false], ['CN\nUS', ['5'], 175, true]]);
  // Only idle lines: their time is shown, and the merged line is quiet like them.
  const idle = bundlesOf([e('1', 'a', 'c', {rttMs: 160, quiet: true})], id => zone[id])[0];
  assert.deepEqual([idle.best, idle.quiet, list[0].quiet], [160, true, false]);
});

test('group boxes and where a line leaves one', () => {
  assert.deepEqual(zoneBox([{x: 0, y: 40, w: 100, h: 50}, {x: 150, y: 0, w: 50, h: 50}]), {x: -20, y: -50, w: 240, h: 160});
  assert.equal(zoneBox([]), null);
  assert.deepEqual(borderPoint({x: 0, y: 0, w: 100, h: 100}, {x: 250, y: 50}), {x: 100, y: 50});
  assert.deepEqual(borderPoint({x: 0, y: 0, w: 100, h: 100}, {x: 50, y: -150}), {x: 50, y: 0});
});
