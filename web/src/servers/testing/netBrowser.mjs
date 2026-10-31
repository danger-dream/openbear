// Server network tests in Chromium: page, fake backend and helpers shared by net.*.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync} from 'node:fs';
import {startBrowserTestServer, waitForBrowserReady} from '../../browserTestServer.mjs';
import {createRequire} from 'node:module';
export const require = createRequire(import.meta.url);
export let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}
export const shots = process.env.OPENBEAR_NET_SHOTS || '';
export const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:net-test"></script></body></html>';
export const entry = `import {createApp, h, KeepAlive, ref} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import '/src/theme.js'; import 'element-plus/dist/index.css'; import '/src/style.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css';
if (window.__dark) document.documentElement.classList.add('dark');
const component = (await import('/src/views/ServersView.vue')).default;
const shown = ref(true); window.__showServers = value => { shown.value = value; };
const Other = {name: 'OtherPage', render: () => h('div', {id: 'other-page'}, '会话')};
const root = {render: () => h(KeepAlive, {include: ['ServersView']}, [shown.value ? h(component) : h(Other)])};
const app=createApp(root); app.use(ElementPlus); for (const [key,icon] of Object.entries(Icons)) app.component(key,icon); app.mount('#app');`;

import {GB, server, SERVERS, fakeMesh, GEO, ADDRESSES, MESH_ADDR, fakeNet, addresses, probe, press, waitFor, run, rowTexts} from './netBackend.mjs';
export {GB, server, SERVERS, fakeMesh, GEO, ADDRESSES, MESH_ADDR, fakeNet, addresses, probe, press, waitFor, run, rowTexts};

// Starts Vite and Chromium for one test file (closed after it) and returns {setup}.
export async function startNetBrowser(t) {
  const fixture = await startBrowserTestServer(t, {name: 'net', entries: ['virtual:net-test', '/src/views/ServersView.vue', '/src/servers/ServerDetail.vue', '/src/servers/mesh/MeshView.vue', '/src/servers/mesh/MeshBoard.vue'], plugins: [{name: 'net-test-entry', resolveId(id) { if (id === 'virtual:net-test') return '\0virtual:net-test'; }, load(id) { if (id === '\0virtual:net-test') return entry; }, configureServer(s) { s.middlewares.use((req, res, next) => { if (req.url?.split('?')[0] === '/__test') { res.setHeader('content-type', 'text/html'); res.end(html); } else next(); }); }}]});
  let browser;
  t.after(async () => { try { await browser?.close(); } finally { await fixture.close(); } });
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  if (shots) mkdirSync(shots, {recursive: true});

  // ddns: the controller has a 动态域名 record with home as its source.
  async function setup(t, {width = 1440, height = 900, dark = false, tabs = ['home'], active = 'home', ddns = false} = {}) {
    const phone = width < 760;
    const context = await browser.newContext({viewport: {width, height}, deviceScaleFactor: phone ? 2 : 1, colorScheme: dark ? 'dark' : 'light', hasTouch: phone, isMobile: phone});
    t.after(() => context.close());
    await context.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: fixture.base});
    const page = await context.newPage(), calls = [], errors = [], mesh = fakeMesh(), net = fakeNet(mesh), items = SERVERS();
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(([isDark, state]) => { window.__dark = isDark; sessionStorage.setItem('openbear.servers.tabs', state); }, [dark, JSON.stringify({tabs, active})]);
    await page.route(/^https:\/\/(open\.er-api\.com|api\.frankfurter\.dev)\//, route => route.fulfill({status: 503, body: ''}));
    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url()), path = url.pathname, method = req.method();
      const raw = req.postDataBuffer();
      const body = raw && req.headers()['content-type']?.includes('json') ? JSON.parse(raw.toString()) : null;
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
      await route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    });
    await page.goto(fixture.base + '/__test');
    await waitForBrowserReady(page, page.locator('#app > *').first(), errors);
    const of = (method, re) => calls.filter(c => c.method === method && re.test(c.path));
    return {page, calls, net, of, phone,
      polls: pid => of('GET', new RegExp(`/net/probes/${pid}$`)).length,
      stops: pid => of('DELETE', new RegExp(`/net/probes/${pid}$`)).length,
      close: async () => { await context.close(); assert.deepEqual(errors, []); }};
  }
  return {setup};
}

export const shot = async (page, name) => { if (shots) { await page.evaluate(() => Promise.all(document.getAnimations().map(a => a.finished.catch(() => {})))); await page.screenshot({path: `${shots}/${name}.png`}); } };
export const noOverflow = async page => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'the page scrolls sideways');
