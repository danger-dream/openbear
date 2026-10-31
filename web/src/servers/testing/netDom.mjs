// Server network (addresses, ping / tcping / trace on the detail page and in the mesh member
// panel, 动态域名) in happy-dom, with the browser tests' fake controller (testing/netBackend.mjs).
// setup(t, options) takes the options of the browser tests' setup() and returns the same handles:
// {page, calls, net, of, phone, polls, stops, close}. Long timers run 10 times faster
// (scaleTimers); page.waitForTimeout(ms) waits the same page time.
import assert from 'node:assert/strict';
import {window} from '../../domTestEnv.mjs';
import {network} from '../../testing/fakeNetwork.mjs';
import './serversStubs.mjs';
import {mountApp} from '../../testing/mountApp.mjs';
import {sleep, settle, scaleTimers} from '../../testing/domPage.mjs';
import {fakeMesh, fakeNet, SERVERS} from './netBackend.mjs';
import './flowLayout.mjs';

const SCALE = 10;
scaleTimers(SCALE);
const {h, KeepAlive, ref} = await import('vue');
// Each setup() gets its own copy of the app's modules (vueLoader's ?fresh), as a new page would.
let copies = 0;
// setup() returns the copy's number as `fresh`, for a test that imports an app module itself.
const loadView = async () => (await import(`../../views/ServersView.vue?fresh=${++copies}`)).default;

// The page writes to the clipboard (the browser test grants clipboard-write); kept here for checks.
export const clipboard = {text: ''};
Object.defineProperty(window.navigator, 'clipboard', {configurable: true, value: {writeText: async text => { clipboard.text = String(text); }, readText: async () => clipboard.text}});

// ddns: the controller has a 动态域名 record with home as its source.
export async function setup(t, {width = 1440, height = 900, dark = false, tabs = ['home'], active = 'home', ddns = false} = {}) {
  const phone = width < 760;
  const calls = [], mesh = fakeMesh(), net = fakeNet(mesh), items = SERVERS();
  network.reset();
  network.external = async () => ({status: 503, body: ''});
  network.handler = async ({path, method, body, url}) => {
    calls.push({path, method, body, at: Date.now()});
    let status = 200, data = {ok: true};
    const netPath = /^\/api\/servers\/([\w-]+)\/net(\/.*)$/.exec(path);
    if (path.startsWith('/api/servers/mesh')) [status, data] = mesh.handle(method, path.slice('/api/servers/mesh'.length), url.search);
    else if (netPath) [status, data] = net.handle(method, netPath[1], netPath[2], body);
    else if (path === '/api/servers' && method === 'GET') data = {ok: true, items, settings: {accessPoints: [], defaultAccess: '', timezone: 'Asia/Shanghai'}, node: {available: true, version: '0.14.0'}};
    else if (path === '/api/servers/live') data = {ok: true, at: Date.now(), items: Object.fromEntries(items.filter(i => i.live).map(i => [i.id, i.live]))};
    else if (path.endsWith('/metrics')) data = {ok: true, step: 3, points: {t: [], cpu: [], steal: [], mem: [], rxRate: [], txRate: [], ioRead: [], ioWrite: [], load: [], tcp: [], udp: []}};
    else if (path.endsWith('/browser')) data = {ok: true, browser: null};
    else if (ddns && path === '/api/ddns') data = {ok: true, nodes: [{serverId: 'home', name: '家', region: 'CN', online: true, ddns: true}], records: [{id: 'r1', name: 'home.example.com', type: 'A', state: 'ok', stateText: '正常',
      desired: '192.0.2.117', remote: '192.0.2.117', source: {kind: 'node', serverId: 'home', method: 'auto', resolvedMethod: 'url_cn'}, backup: null}]};
    else if (/^\/api\/servers\/[\w-]+$/.test(path)) data = {ok: true, item: {...items.find(i => i.id === path.split('/')[3]), terminalLog: [], execLog: []}};
    return {status, json: JSON.parse(JSON.stringify(data))};
  };
  window.sessionStorage.clear();
  window.localStorage.clear();
  window.sessionStorage.setItem('openbear.servers.tabs', JSON.stringify({tabs, active}));
  clipboard.text = '';
  const ServersView = await loadView();
  // Kept alive like in the app, so leaving the page and coming back can be checked.
  const shown = ref(true);
  window.__showServers = value => { shown.value = value; };
  const Other = {name: 'OtherPage', render: () => h('div', {id: 'other-page'}, '会话')};
  const root = {render: () => h(KeepAlive, {include: ['ServersView']}, [shown.value ? h(ServersView) : h(Other)])};
  const {page, unmount} = mountApp(t, root, {width, height, dark, network, touch: phone});
  page.waitForTimeout = async ms => { await sleep(Number(ms) >= 200 ? Math.max(20, ms / SCALE) : ms); await settle(); };
  await page.locator('#app > *').first().waitFor({state: 'attached'});
  const of = (method, re) => calls.filter(c => c.method === method && re.test(c.path));
  return {page, calls, net, of, phone, fresh: copies,
    polls: pid => of('GET', new RegExp(`/net/probes/${pid}$`)).length,
    stops: pid => of('DELETE', new RegExp(`/net/probes/${pid}$`)).length,
    close: () => unmount()};
}
