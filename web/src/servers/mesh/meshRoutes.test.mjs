import test from 'node:test';
import assert from 'node:assert/strict';
import { addRoute, changeRoute, connect, gap, gapsOf, moveInPair, nodesFrom, pairsOf, routeRtt, routeState } from './meshRoutes.js';

const m = (serverId, role, extra = {}) => ({serverId, name: serverId.toUpperCase(), role, listen: {udp: 0, tcp: role === 'relay' ? 2 : 0, tls: 0},
  entry: role === 'relay' ? null : {mode: 'failover', candidates: []}, tags: [], ...extra});
const cand = (relay, id = `c-${relay}`) => ({id, relay, transport: 'tcp'});
const work = () => ({
  members: [m('r1', 'relay'), m('r2', 'relay'), m('r3', 'relay'),
    m('toy', 'member', {entry: {mode: 'failover', candidates: [cand('r1')], autoReturn: true}}),
    m('sg', 'member', {entry: {mode: 'upstreams', candidates: [cand('r3')], autoReturn: true}}), m('mac', 'member')],
  relayLinks: {mode: 'custom', pairs: [{a: 'r1', b: 'r2', transport: 'tcp', dialer: 'a'}]}, directLinks: [], rules: [], paths: []});
const route = (id, from, to, via, on = true) => ({id, on, from, to, via});

test('a node\'s routes go by the other end, either way written, each pair\'s in order', () => {
  const paths = [route('p1', 'toy', 'sg', ['r1', 'r3']), route('p2', 'mac', 'r2', ['r1']), route('p3', 'sg', 'toy', ['r3', 'r2', 'r1']), route('p4', 'toy', 'mac', ['r1'])];
  assert.deepEqual(pairsOf(paths, 'toy').map(g => [g.other, g.routes.map(p => p.id)]), [['sg', ['p1', 'p3']], ['mac', ['p4']]]);
  assert.deepEqual(nodesFrom(paths[2], 'toy'), ['toy', 'r1', 'r2', 'r3', 'sg']);
  // Moving the second toy-sg route up passes over the routes of other pairs.
  assert.equal(moveInPair(paths, paths[2], -1), true);
  assert.deepEqual(paths.map(p => p.id), ['p3', 'p2', 'p1', 'p4']);
  assert.equal(moveInPair(paths, paths[0], -1), false);
});

test('what a route lacks reads as what saving it does', () => {
  const w = work();
  assert.equal(gap(w, 'r1', 'r2'), null);
  assert.deepEqual(gap(w, 'r2', 'r3'), {kind: 'relay', a: 'r2', b: 'r3', add: true, text: '新建 R2 和 R3 之间的中转线路'});
  assert.equal(gap(w, 'toy', 'r1').text, 'TOY 改为「同时连上」');
  assert.equal(gap(w, 'toy', 'r2').text, 'R2 加为 TOY 的入口中转，TOY 改为「同时连上」');
  assert.equal(gap(w, 'r3', 'sg'), null);
  assert.equal(gap(w, 'r2', 'sg').text, 'R2 加为 SG 的入口中转');
  assert.equal(gap(w, 'toy', 'sg').error, true);
  // Every relay is linked to every other one while they all are.
  w.relayLinks.mode = 'full';
  assert.equal(gap(w, 'r2', 'r3'), null);
});

test('saving a route makes its links and has its member ends take all their entry relays', () => {
  const w = work();
  assert.deepEqual(connect(w, ['toy', 'r2', 'r3', 'sg']), {});
  const toy = w.members.find(x => x.serverId === 'toy');
  assert.equal(toy.entry.mode, 'upstreams');
  assert.deepEqual(toy.entry.candidates.map(c => [c.relay, c.auto]), [['r1', undefined], ['r2', ['transport']]]);
  assert.deepEqual(w.relayLinks.pairs.at(-1), {a: 'r2', b: 'r3', transport: 'tcp', cost: 1, dialer: 'a', auto: ['transport', 'dialer', 'bias']});
  assert.deepEqual(gapsOf(w, ['toy', 'r2', 'r3', 'sg']), [null, null, null]);
  // A segment no route can have changes nothing.
  const before = JSON.stringify(w);
  assert.match(connect(w, ['mac', 'r1', 'toy', 'sg']).error, /都不是中转/);
  assert.equal(JSON.stringify(w), before);
});

test('a pair takes three routes, none twice either way round', () => {
  const w = work();
  assert.ok(addRoute(w, 'toy', 'sg', ['r1', 'r3']).route);
  assert.equal(addRoute(w, 'sg', 'toy', ['r3', 'r1']).error, '已经有这条自建路线了');
  assert.ok(addRoute(w, 'sg', 'toy', ['r3', 'r2', 'r1']).route);
  assert.ok(addRoute(w, 'toy', 'sg', ['r2', 'r3']).route);
  assert.match(addRoute(w, 'toy', 'sg', ['r1', 'r2', 'r3']).error, /已经有 3 条/);
  assert.match(addRoute(w, 'toy', 'sg', []).error, /1～8 台/);
  assert.deepEqual(w.paths.map(p => [p.from, p.to, p.via.join(',')]), [['toy', 'sg', 'r1,r3'], ['sg', 'toy', 'r3,r2,r1'], ['toy', 'sg', 'r2,r3']]);
  assert.ok(w.paths.every(p => /^p[a-z0-9]+$/.test(p.id) && p.on));
  // Through other relays, in its place.
  assert.equal(changeRoute(w, w.paths[2], ['r1', 'r3']).error, '已经有这条自建路线了');
  assert.deepEqual(changeRoute(w, w.paths[2], ['r2', 'r1', 'r3']), {});
  assert.deepEqual(w.paths[2].via, ['r2', 'r1', 'r3']);
});

test('where a route is reads from the end looked from', () => {
  const p = route('p1', 'toy', 'sg', ['r1', 'r3']);
  const name = id => id.toUpperCase();
  const view = states => ({id: 'p1', problems: [], single: [], states, segments: [{rttMs: 1.5}, {rttMs: 30}, {rttMs: 2}]});
  assert.deepEqual(routeState(p, view({to: {state: 'pinned'}, back: {state: 'standby'}}), 'toy', name), {tone: 'ok', text: '使用中'});
  assert.deepEqual(routeState(p, view({to: {state: 'pinned'}, back: {state: 'standby'}}), 'sg', name), {tone: 'info', text: '备用'});
  assert.deepEqual(routeState(p, view({to: {state: 'fallback', down: ['r1', 'r3']}}), 'toy', name), {tone: 'bad', text: '断开（R1 和 R3 之间）'});
  assert.deepEqual(routeState(p, view({to: {state: 'fallback', silent: true}}), 'toy', name), {tone: 'bad', text: '包过不去（各段线路都在）'});
  assert.deepEqual(routeState(p, view({to: {state: 'starting', silent: true}}), 'toy', name), {tone: 'warn', text: '包过不去（各段线路都在）'});
  const until = new Date(2026, 10, 11, 18, 5).getTime();
  assert.deepEqual(routeState(p, view({to: {state: 'waiting', until}}), 'toy', name), {tone: 'warn', text: '刚恢复，18:05 起可用'});
  assert.deepEqual(routeState(p, view({to: {state: 'unknown'}}), 'toy', name), {tone: 'off', text: '未应用'});
  assert.deepEqual(routeState({...p, on: false}, view({}), 'toy', name), {tone: 'off', text: '已停用'});
  assert.deepEqual(routeState(p, {...view({}), problems: [{message: '起点 TOY 的入口中转里没有 R1'}]}, 'toy', name), {tone: 'bad', text: '起点 TOY 的入口中转里没有 R1'});
  assert.equal(routeState(p, {...view({}), single: ['r3']}, 'toy', name).tone, 'warn');
  assert.equal(routeRtt(view({})), 33.5);
  assert.equal(routeRtt({segments: [{rttMs: 1}, {rttMs: null}]}), null);
});
