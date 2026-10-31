// The mesh board in Chromium: page, fake controller and helpers shared by mesh.*.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync} from 'node:fs';
import {startBrowserTestServer, waitForBrowserReady} from '../../browserTestServer.mjs';
import {createRequire} from 'node:module';
export const require = createRequire(import.meta.url);
export let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}
export const shots = process.env.OPENBEAR_MESH_SHOTS || '';
export const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:mesh-test"></script></body></html>';
export const entry = `import {createApp, h, KeepAlive, ref} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import '/src/theme.js'; import 'element-plus/dist/index.css'; import '/src/style.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css';
if (window.__dark) document.documentElement.classList.add('dark');
const component = (await import('/src/views/ServersView.vue')).default;
// With __keepAlive the page can be put away and back, as the app's other pages do to it.
const shown = ref(true); window.__showServers = value => { shown.value = value; };
const Other = {name: 'OtherPage', render: () => h('div', {id: 'other-page'}, '会话')};
const root = window.__keepAlive ? {render: () => h(KeepAlive, {include: ['ServersView']}, [shown.value ? h(component) : h(Other)])} : component;
const app=createApp(root); app.use(ElementPlus); for (const [key,icon] of Object.entries(Icons)) app.component(key,icon); app.mount('#app');`;

import {GB, server, SERVERS, member, fakeMesh} from './meshBackend.mjs';
export {GB, server, SERVERS, member, fakeMesh};

// Starts Vite and Chromium for one test file (closed after it) and returns {setup}.
export async function startMeshBrowser(t) {
  const fixture = await startBrowserTestServer(t, {name: 'mesh', entries: ['virtual:mesh-test', '/src/views/ServersView.vue', '/src/servers/mesh/MeshView.vue', '/src/servers/mesh/MeshBoard.vue'], plugins: [{name: 'mesh-test-entry', resolveId(id) { if (id === 'virtual:mesh-test') return '\0virtual:mesh-test'; }, load(id) { if (id === '\0virtual:mesh-test') return entry; }, configureServer(s) { s.middlewares.use((req, res, next) => { if (req.url?.split('?')[0] === '/__test') { res.setHeader('content-type', 'text/html'); res.end(html); } else next(); }); }}]});
  let browser;
  t.after(async () => { try { await browser?.close(); } finally { await fixture.close(); } });
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  if (shots) mkdirSync(shots, {recursive: true});

  async function setup(t, {width = 1440, height = 900, dark = false, tabs = [], active = 'mesh', failing = '', upgrades = false, old = false, missing = false, punch = false, legacyDirect = false, reach = false, noPath = false, routes = false, oldRoutes = false, noisy = false, gone = '', lines = false, allSubnets = false, secure = false, firewall = false, init = null, keepAlive = false} = {}) {
    if (routes) reach = true;
    const context = await browser.newContext({viewport: {width, height}, deviceScaleFactor: 1, colorScheme: dark ? 'dark' : 'light', hasTouch: width < 760, isMobile: width < 760});
    t.after(() => context.close());
    const page = await context.newPage(), calls = [], errors = [], mesh = fakeMesh({failing, upgrades, old, punch, legacyDirect, reach, noPath, routes, oldRoutes, noisy, gone, lines, allSubnets, secure, firewall}), items = SERVERS();
    if (punch) items.push(server('office', 'Office', {region: 'CN'}), server('cafe', 'Cafe', {region: 'SG'}), server('nas', 'NAS', {region: 'CN'}));
    if (reach) items.push(server('sg', 'SG', {region: 'SG'}), server('toy', 'TOY', {region: 'CN'}), server('lab', 'Lab', {region: 'CN'}));
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(([isDark, state, alive]) => { window.__dark = isDark; window.__keepAlive = alive; sessionStorage.setItem('openbear.servers.tabs', state); }, [dark, JSON.stringify({tabs, active}), keepAlive]);
    // slowGets(ms): GET /mesh answers as it stands when asked, but only `ms` later.
    let slow = 0;
    if (init) await page.addInitScript(init);
    await page.route(/^https:\/\/(open\.er-api\.com|api\.frankfurter\.dev)\//, route => route.fulfill({status: 503, body: ''}));
    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url()), path = url.pathname, method = req.method();
      const raw = req.postDataBuffer();
      const body = raw && req.headers()['content-type']?.includes('json') ? JSON.parse(raw.toString()) : null;
      calls.push({path, method, body, at: Date.now(), query: url.search});
      let status = 200, data = {ok: true};
      if (path.startsWith('/api/servers/mesh')) [status, data] = missing ? [404, {ok: false, error: 'not_found'}] : await mesh.handle(method, path.slice('/api/servers/mesh'.length), body, url.search);
      else if (path === '/api/servers' && method === 'GET') data = {ok: true, items, settings: {accessPoints: [], defaultAccess: '', timezone: 'Asia/Shanghai'}, node: {available: true, version: '0.13.1'}};
      else if (path === '/api/servers/live') data = {ok: true, at: Date.now(), items: Object.fromEntries(items.filter(i => i.live).map(i => [i.id, i.live]))};
      else if (path.endsWith('/metrics')) data = {ok: true, step: 3, points: {t: [], cpu: [], steal: [], mem: [], rxRate: [], txRate: [], ioRead: [], ioWrite: [], load: [], tcp: [], udp: []}};
      else if (/^\/api\/servers\/[\w-]+$/.test(path)) data = {ok: true, item: {...items.find(i => i.id === path.split('/')[3]), terminalLog: [], execLog: []}};
      const text = JSON.stringify(data);
      if (slow && method === 'GET' && path === '/api/servers/mesh') await new Promise(resolve => setTimeout(resolve, slow));
      await route.fulfill({status, contentType: 'application/json', body: text});
    });
    await page.goto(fixture.base + '/__test');
    await waitForBrowserReady(page, page.locator('#app > *').first(), errors);
    if (active === 'mesh' && !missing) { await page.locator('.vue-flow__node').first().waitFor(); await settled(page); }
    const puts = () => calls.filter(c => c.method === 'PUT' && c.path === '/api/servers/mesh/draft');
    return {page, calls, mesh, puts, last: () => puts().at(-1)?.body, slowGets: ms => { slow = ms; },
      layoutPuts: () => calls.filter(c => c.method === 'PUT' && c.path === '/api/servers/mesh/layout'),
      // Wait for a draft save that satisfies `check`.
      saved: async (check, label = 'draft save') => { for (let i = 0; i < 60; i++) { const hit = puts().find(c => { try { return check(c.body); } catch { return false; } }); if (hit) return hit.body; await page.waitForTimeout(100); } assert.fail(`no ${label}`); },
      close: async () => { await context.close(); assert.deepEqual(errors, []); }};
  }
  return {setup};
}

export const noOverflow = async page => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'page scrolls horizontally');
// Running transitions and animations (sheets sliding in, notices, the board fitting its view) have
// finished, and two frames have been drawn since.
export const frames = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
// Endless animations (a punching line's flowing dashes) are left out. With a locator, only that
// element's own animations (a notice sliding in) are waited for, not others' (a label's flash).
export const settled = async (page, locator = null) => {
  if (locator) await locator.evaluate(el => Promise.all(el.getAnimations({subtree: true}).filter(a => a.effect?.getComputedTiming().endTime !== Infinity).map(a => a.finished.catch(() => {}))));
  else await page.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getComputedTiming().endTime !== Infinity).map(a => a.finished.catch(() => {}))));
  await frames(page);
};
export const shot = async (page, name) => { if (shots) { await settled(page); await page.screenshot({path: `${shots}/${name}.png`}); } };
export const node = (page, id) => page.locator(`.vue-flow__node[data-id="${id}"]`);
export const label = (page, text) => page.locator('.mesh-edge-label', {hasText: new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`)});
export const inspector = page => page.locator('.mesh-inspector-col');
export const title = page => page.locator('.mesh-inspector-head > h2');
export const centre = async locator => { const b = await locator.boundingBox(); return {x: b.x + b.width / 2, y: b.y + b.height / 2}; };
export const memberOf = (body, id) => body.members.find(m => m.serverId === id);
// A notice must not cover any node or link label.
export async function clearOfBoard(page, notice) {
  const box = await notice.boundingBox();
  const others = await page.locator('.vue-flow__node, .mesh-edge-label').evaluateAll(list => list.map(el => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height, what: el.dataset.id || el.textContent}; }));
  for (const r of others) assert.ok(box.x + box.width <= r.x || box.x >= r.x + r.w || box.y + box.height <= r.y || box.y >= r.y + r.h, `notice ${JSON.stringify(box)} covers ${r.what}`);
}
export const dimmed = page => page.locator('.mesh-node').evaluateAll(list => list.filter(el => el.hasAttribute('data-dim')).map(el => el.closest('.vue-flow__node').dataset.id).sort());
export async function dragPort(page, from, to, side = 'right') {
  await node(page, from).hover();
  const port = await centre(node(page, from).locator(`.mesh-port[data-side=${side}]`));
  const target = await centre(node(page, to));
  await page.mouse.move(port.x, port.y);
  await page.mouse.down();
  await page.mouse.move((port.x + target.x) / 2, (port.y + target.y) / 2, {steps: 4});
  await page.mouse.move(target.x, target.y, {steps: 4});
  await page.mouse.up();
}
export async function dragBody(page, from, to) {
  const a = await centre(node(page, from)), b = await centre(node(page, to));
  await page.mouse.move(a.x, a.y);
  await page.mouse.down();
  await page.mouse.move(b.x, b.y, {steps: 8});
  await page.mouse.up();
}
// ---- canvas: alignment, folding columns, line ends and stability ----
// Touch on the phone, through the browser's own touch input.
export async function touchDrag(page, from, to, steps = 10) {
  const cdp = await page.context().newCDPSession(page);
  const point = p => [{x: Math.round(p.x), y: Math.round(p.y), id: 1}];
  await cdp.send('Input.dispatchTouchEvent', {type: 'touchStart', touchPoints: point(from)});
  // The drag starts once the finger has moved past the phone's threshold (6 px); from there the node follows.
  await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: point({x: from.x, y: from.y + 8})});
  from = {x: from.x, y: from.y + 8}; to = {x: to.x, y: to.y + 8};
  for (let i = 1; i <= steps; i++) {
    await cdp.send('Input.dispatchTouchEvent', {type: 'touchMove', touchPoints: point({x: from.x + (to.x - from.x) * i / steps, y: from.y + (to.y - from.y) * i / steps})});
    await page.waitForTimeout(16);
  }
  return {end: async () => { await cdp.send('Input.dispatchTouchEvent', {type: 'touchEnd', touchPoints: []}); await cdp.detach(); }};
}
export const guideCount = page => page.locator('.mesh-guides line').count();
// ---- third batch: reachability, site relays, measured relay links, paths ----
export const texts = async locator => (await locator.allInnerTexts()).map(s => s.replace(/\s+/g, ' ').trim());
export const until = async (page, check, what, ms = 9000) => { for (let i = 0; i < ms / 100; i++) { if (await check()) return; await page.waitForTimeout(100); } assert.fail(`timed out: ${what}`); };
