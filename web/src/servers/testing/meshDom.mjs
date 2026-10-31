// The mesh board (servers page, 组网 tab) in happy-dom, with the same fake controller as the
// browser tests (testing/meshBackend.mjs). setup(t, options) takes the options of the browser
// tests' setup() and returns the same handles: {page, calls, mesh, puts, last, slowGets,
// layoutPuts, saved, close}. There is no layout: Vue Flow gets fixed node sizes from
// testing/flowSizes (see below) so it draws nodes, edges and labels; positions are not checked.
import assert from 'node:assert/strict';
import {window} from '../../domTestEnv.mjs';
import {network} from '../../testing/fakeNetwork.mjs';
import './serversStubs.mjs';
import {mountApp} from '../../testing/mountApp.mjs';
import {sleep, settle, scaleTimers} from '../../testing/domPage.mjs';
import {fakeMesh, SERVERS, server} from './meshBackend.mjs';

import './flowLayout.mjs';

// Polls and other long timers run 10 times faster; page.waitForTimeout(ms) waits the same page time.
const SCALE = 10;
scaleTimers(SCALE);

const {h, KeepAlive, ref} = await import('vue');
// Each setup() gets its own copy of the app's modules (vueLoader's ?fresh), as a new page would:
// the mesh store and the board's caches are module state.
let copies = 0;
// setup() returns the copy's number as `fresh`, for a test that imports an app module itself.
const loadView = async () => (await import(`../../views/ServersView.vue?fresh=${++copies}`)).default;

export async function setup(t, {width = 1440, height = 900, dark = false, tabs = [], active = 'mesh', failing = '', upgrades = false, old = false, missing = false, punch = false, legacyDirect = false, reach = false, noPath = false, routes = false, oldRoutes = false, noisy = false, gone = '', lines = false, allSubnets = false, secure = false, firewall = false, init = null, keepAlive = false} = {}) {
  if (routes) reach = true;
  const calls = [], mesh = fakeMesh({failing, upgrades, old, punch, legacyDirect, reach, noPath, routes, oldRoutes, noisy, gone, lines, allSubnets, secure, firewall}), items = SERVERS();
  if (punch) items.push(server('office', 'Office', {region: 'CN'}), server('cafe', 'Cafe', {region: 'SG'}), server('nas', 'NAS', {region: 'CN'}));
  if (reach) items.push(server('sg', 'SG', {region: 'SG'}), server('toy', 'TOY', {region: 'CN'}), server('lab', 'Lab', {region: 'CN'}));
  let slow = 0;
  network.reset();
  network.external = async () => ({status: 503, body: ''});
  network.handler = async ({path, method, body, url}) => {
    calls.push({path, method, body, at: Date.now(), query: url.search});
    let status = 200, data = {ok: true};
    if (path.startsWith('/api/servers/mesh')) [status, data] = missing ? [404, {ok: false, error: 'not_found'}] : await mesh.handle(method, path.slice('/api/servers/mesh'.length), body, url.search);
    else if (path === '/api/servers' && method === 'GET') data = {ok: true, items, settings: {accessPoints: [], defaultAccess: '', timezone: 'Asia/Shanghai'}, node: {available: true, version: '0.13.1'}};
    else if (path === '/api/servers/live') data = {ok: true, at: Date.now(), items: Object.fromEntries(items.filter(i => i.live).map(i => [i.id, i.live]))};
    else if (path.endsWith('/metrics')) data = {ok: true, step: 3, points: {t: [], cpu: [], steal: [], mem: [], rxRate: [], txRate: [], ioRead: [], ioWrite: [], load: [], tcp: [], udp: []}};
    else if (/^\/api\/servers\/[\w-]+$/.test(path)) data = {ok: true, item: {...items.find(i => i.id === path.split('/')[3]), terminalLog: [], execLog: []}};
    const json = JSON.parse(JSON.stringify(data));
    if (slow && method === 'GET' && path === '/api/servers/mesh') await new Promise(resolve => setTimeout(resolve, slow));
    return {status, json};
  };
  window.sessionStorage.clear();
  window.localStorage.clear();
  window.sessionStorage.setItem('openbear.servers.tabs', JSON.stringify({tabs, active}));
  if (init) init();
  const shown = ref(true);
  window.__showServers = value => { shown.value = value; };
  const Other = {name: 'OtherPage', render: () => h('div', {id: 'other-page'}, '会话')};
  const ServersView = await loadView();
  const root = keepAlive ? {render: () => h(KeepAlive, {include: ['ServersView']}, [shown.value ? h(ServersView) : h(Other)])} : ServersView;
  let {page, unmount} = mountApp(t, root, {width, height, dark, network, touch: width < 760});
  // A reload keeps the storage and mounts the page afresh.
  page.reload = async () => { await unmount({check: false}); ({unmount} = mountApp(t, root, {width, height, dark, network, touch: width < 760})); await page.locator('#app > *').first().waitFor({state: 'attached'}); };
  page.waitForTimeout = async ms => { await sleep(Number(ms) >= 200 ? Math.max(20, ms / SCALE) : ms); await settle(); };
  await page.locator('#app > *').first().waitFor({state: 'attached'});
  if (active === 'mesh' && !missing) await page.locator('.vue-flow__node').first().waitFor();
  const puts = () => calls.filter(c => c.method === 'PUT' && c.path === '/api/servers/mesh/draft');
  return {page, calls, mesh, puts, fresh: copies, last: () => puts().at(-1)?.body, slowGets: ms => { slow = ms; },
    layoutPuts: () => calls.filter(c => c.method === 'PUT' && c.path === '/api/servers/mesh/layout'),
    // Wait for a draft save that satisfies `check`.
    saved: async (check, label = 'draft save') => { for (let i = 0; i < 100; i++) { const hit = puts().find(c => { try { return check(c.body); } catch { return false; } }); if (hit) return hit.body; await sleep(20); await settle(); } assert.fail(`no ${label}`); },
    close: () => unmount()};
}

export const node = (page, id) => page.locator(`.vue-flow__node[data-id="${id}"]`);
export const label = (page, text) => page.locator('.mesh-edge-label', {hasText: new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`)});
export const inspector = page => page.locator('.mesh-inspector-col');
export const title = page => page.locator('.mesh-inspector-head > h2');
export const memberOf = (body, id) => body.members.find(m => m.serverId === id);
export const dimmed = page => page.locator('.mesh-node').evaluateAll(list => list.filter(el => el.hasAttribute('data-dim')).map(el => el.closest('.vue-flow__node').dataset.id).sort());
export const texts = async locator => (await locator.allInnerTexts()).map(s => s.replace(/\s+/g, ' ').trim());
export const until = async (page, check, what, ms = 9000) => { const end = Date.now() + ms; while (Date.now() < end) { if (await check()) return; await sleep(20); await settle(); } assert.fail(`timed out: ${what}`); };
