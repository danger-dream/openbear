// Regression tests for the release review: real Vue components in happy-dom; only the API,
// Leaflet, observer sizes and the unlock polling clock are simulated. No live services.
import test from 'node:test';
import assert from 'node:assert/strict';
import {window} from '../domTestEnv.mjs';
import './testing/serversStubs.mjs';
import {createApp, h, reactive, nextTick} from 'vue';
import ElementPlus from 'element-plus';
const {Api} = await import('../api.js');
const BgpSection = (await import('./detail/BgpSection.vue')).default;
const BgpGraph = (await import('./detail/BgpGraph.vue')).default;
const UnlockSection = (await import('./detail/UnlockSection.vue')).default;
const TrafficSection = (await import('./detail/TrafficSection.vue')).default;
const IpQuality = (await import('./detail/IpQuality.vue')).default;

async function settle() {
  await nextTick();
  await new Promise(resolve => setImmediate(resolve));
  await nextTick();
}
function mount(t, Component, values) {
  const props = reactive(values), host = document.createElement('div');
  document.body.append(host);
  const app = createApp({render: () => h(Component, props)});
  const errors = [];
  app.config.errorHandler = error => errors.push(error);
  app.use(ElementPlus);
  app.mount(host);
  let closed = false;
  const close = () => { if (!closed) { closed = true; app.unmount(); host.remove(); } };
  t.after(() => { close(); assert.deepEqual(errors, []); });
  return {props, host, close, errors};
}
function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
}
function pollingClock(t) {
  const timers = new Map(), realSet = globalThis.setTimeout, realClear = globalThis.clearTimeout;
  let scheduled = 0;
  t.mock.method(globalThis, 'setTimeout', (fn, ms, ...args) => {
    if (ms !== 4000) return realSet(fn, ms, ...args);
    const id = {poll: ++scheduled}; timers.set(id, fn); return id;
  });
  t.mock.method(globalThis, 'clearTimeout', id => { if (!timers.delete(id)) realClear(id); });
  return {timers, count: () => scheduled};
}
const graph = {origins: [1], nodes: [{asn: 1, name: 'Origin'}, {asn: 2, name: 'Transit'}, {asn: 3, name: 'Tier 1', tier1: true}],
  edges: [{from: 1, to: 2, peers: 10}, {from: 2, to: 3, peers: 10}]};

test('BGP IPv6-only: automatically loads IPv6 and shows the sole selected family', async t => {
  const calls = [];
  t.mock.method(Api, 'serverBgp', async (id, family) => {
    calls.push([id, family]);
    return family === 4 ? {ok: true, family, families: [6], state: 'no_address'}
      : {ok: true, family, families: [6], state: 'ok', prefix: '2001:db8::/32', ...graph};
  });
  const c = mount(t, BgpSection, {server: {id: 'v6'}, bare: true, active: true});
  await settle();
  assert.deepEqual(calls, [['v6', 4], ['v6', 6]]);
  assert.equal(c.host.querySelector('[role=tab][aria-selected=true]').textContent, 'IPv6');
  assert.match(c.host.textContent, /2001:db8::\/32/);
  assert.doesNotMatch(c.host.textContent, /没有公网 IPv4/);
  assert.ok(c.host.querySelector('.bg-graph'));
});

test('BGP with no public address does not loop trying address families', async t => {
  const api = t.mock.method(Api, 'serverBgp', async () => ({ok: true, family: 4, families: [], state: 'no_address'}));
  const c = mount(t, BgpSection, {server: {id: 'none'}, bare: true, active: true});
  await settle();
  assert.equal(api.mock.callCount(), 1);
  assert.equal(c.host.querySelector('[role=tab]'), null);
  assert.match(c.host.textContent, /没有公网 IPv4/);
});

test('UnlockSection: late running response after unmount cannot recreate the timer', async t => {
  const clock = pollingClock(t), pending = deferred();
  const api = t.mock.method(Api, 'serverUnlockFull', () => pending.promise);
  const c = mount(t, UnlockSection, {server: {id: 'a'}, active: true, now: Date.now()});
  await settle();
  assert.equal(api.mock.callCount(), 1);
  c.close();
  pending.resolve({ok: true, running: true});
  await settle();
  assert.equal(clock.count(), 0);
  assert.equal(clock.timers.size, 0);
  assert.equal(api.mock.callCount(), 1);
});

test('UnlockSection: deactivation invalidates responses, reactivation owns one polling chain', async t => {
  const clock = pollingClock(t), old = deferred(), fresh = deferred();
  let calls = 0;
  const api = t.mock.method(Api, 'serverUnlockFull', () => ++calls === 1 ? old.promise : fresh.promise);
  const c = mount(t, UnlockSection, {server: {id: 'a'}, active: true, unlockBusy: true, now: Date.now()});
  await settle();
  c.props.active = false;
  c.props.unlockBusy = false;
  await settle();
  assert.equal(api.mock.callCount(), 1, 'recheck completion while inactive must not request');
  c.props.active = true;
  await settle();
  assert.equal(api.mock.callCount(), 2);
  old.resolve({ok: true, running: true});
  await settle();
  assert.equal(clock.count(), 0, 'old activation cannot schedule a poll');
  fresh.resolve({ok: true, running: true});
  await settle();
  assert.equal(clock.timers.size, 1);
  c.props.active = false;
  await settle();
  assert.equal(clock.timers.size, 0, 'deactivation cancels an existing poll');
});

test('traffic: UTC browser labels Shanghai day/month/hour across month and year boundaries', async t => {
  const previous = process.env.TZ;
  process.env.TZ = 'UTC';
  t.after(() => { if (previous === undefined) delete process.env.TZ; else process.env.TZ = previous; });
  const stamp = date => Date.parse(`${date}T00:00:00+08:00`) / 1000;
  t.mock.method(Api, 'serverTrafficStats', async (id, unit) => ({ok: true, unit, points: [
    {t: stamp('2026-11-01'), label: unit === 'day' ? '2026-11-01' : unit === 'month' ? '2026-11' : '', rx: 10, tx: 20},
    {t: stamp('2027-01-01'), label: unit === 'day' ? '2027-01-01' : unit === 'month' ? '2027-01' : '', rx: 30, tx: 40},
  ]}));
  const c = mount(t, TrafficSection, {server: {id: 'a'}, active: true, now: Date.now(), timezone: 'Asia/Shanghai'});
  await settle();
  const labels = () => [...c.host.querySelectorAll('.tc-x span')].map(el => el.textContent);
  assert.deepEqual(labels(), ['11/1', '1/1（累计中）']);
  const pick = async text => { [...c.host.querySelectorAll('button')].find(b => b.textContent === text).click(); await settle(); };
  await pick('按月');
  assert.deepEqual(labels(), ['11 月', '2027 年 1 月（累计中）']);
  await pick('按小时');
  assert.deepEqual(labels(), ['11/1 00:00', '1/1 00:00（累计中）']);
  c.props.timezone = 'UTC';
  await settle();
  assert.deepEqual(labels(), ['10/31 16:00', '12/31 16:00（累计中）'], 'settings changes update the hour labels');
});

test('BgpGraph: phone -> desktop -> phone -> desktop rebinds size observer and scales', async t => {
  const observers = [];
  const originalObserver = globalThis.ResizeObserver;
  t.after(() => { globalThis.ResizeObserver = originalObserver; });
  globalThis.ResizeObserver = class {
    constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this); }
    observe(el) { this.el = el; }
    disconnect() { this.disconnected = true; }
  };
  let boxWidth = 240;
  const realRect = window.HTMLElement.prototype.getBoundingClientRect;
  t.mock.method(window.HTMLElement.prototype, 'getBoundingClientRect', function () {
    return this.classList.contains('bg-graph') ? {width: boxWidth} : realRect.call(this);
  });
  const c = mount(t, BgpGraph, {data: graph, phone: true});
  await settle();
  assert.equal(observers.length, 0);
  const scale = () => Number(c.host.querySelector('.bg-canvas').style.transform.match(/scale\(([^)]+)\)/)[1]);
  c.props.phone = false;
  await settle();
  assert.equal(observers.length, 1);
  const first = observers[0], firstElement = first.el;
  const naturalWidth = parseFloat(c.host.querySelector('.bg-canvas').style.width);
  assert.equal(scale(), 240 / naturalWidth, 'reads new element size immediately');
  first.callback([{contentRect: {width: 300}}]);
  await settle();
  assert.equal(scale(), 300 / naturalWidth);
  c.props.phone = true;
  await settle();
  assert.deepEqual(c.errors, []);
  assert.equal(first.disconnected, true);
  boxWidth = 200;
  c.props.phone = false;
  await settle();
  assert.equal(observers.length, 2);
  assert.notEqual(observers[1].el, firstElement);
  assert.equal(scale(), 200 / naturalWidth);
  c.close();
  assert.equal(observers[1].disconnected, true);
});

test('IP quality: changed location remounts map and marker; unchanged location preserves map', async t => {
  let location = {lat: 35, lon: 139, country: 'JP'};
  t.mock.method(Api, 'serverIpQuality', async () => ({ok: true, report: {location: {...location}, atMs: Date.now(), mail: {}, risk: {}, factors: []}, changes: []}));
  t.mock.method(Api, 'serverIpIntel', async () => ({ok: true}));
  const c = mount(t, IpQuality, {server: {id: 'a'}, active: true, now: Date.now()});
  // IpMap is asynchronously imported by the real parent.
  for (let i = 0; i < 100 && !c.host.querySelector('.leaflet-container'); i++) {
    await new Promise(r => setTimeout(r, 10)); await settle();
  }
  const map = () => c.host.querySelector('.leaflet-container');
  assert.ok(map());
  const first = map();
  assert.equal(first.dataset.center, '35.00,139.00');
  const reload = async () => { c.props.active = false; await settle(); c.props.active = true; await settle(); };
  await reload();
  assert.equal(map(), first, 'same position does not discard user zoom');
  location = {lat: 40, lon: -74, country: 'US'};
  await reload();
  assert.notEqual(map(), first);
  assert.equal(first.classList.contains('leaflet-container'), false, 'old map removed');
  assert.equal(map().dataset.center, '40.00,-74.00');
  assert.equal(map().querySelector('.ipm-pin').dataset.at, '40.00,-74.00');
  const second = map();
  location = {lat: null, lon: null, country: 'GB'};
  await reload();
  assert.notEqual(map(), second);
  assert.ok(map().dataset.bounds, 'country-only reports reset bounds as well');
});
