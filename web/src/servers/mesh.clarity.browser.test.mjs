import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync} from 'node:fs';
import {createRequire} from 'node:module';
import {startBrowserTestServer, waitForBrowserReady} from '../browserTestServer.mjs';
const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}

// How the board keeps a real-sized mesh readable: lines in use stand out, lines not in use are
// drawn quiet without labels until they or their ends are pointed at, links that would run under
// another node bow around it. The mesh is laid out as a running one is: six relays (two of them
// site relays), ten relay links, six members with one to three entries and a punched direct link.
// OPENBEAR_MESH_SHOTS takes screenshots.
const shots = process.env.OPENBEAR_MESH_SHOTS || '';
const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:mesh-clarity"></script></body></html>';
const entry = `import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import '/src/theme.js'; import 'element-plus/dist/index.css'; import '/src/style.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css';
if (window.__dark) document.documentElement.classList.add('dark');
const component = (await import('/src/views/ServersView.vue')).default;
const app=createApp(component); app.use(ElementPlus); for (const [key,icon] of Object.entries(Icons)) app.component(key,icon); app.mount('#app');`;

import {GB, server, NODES, PAIRS, ROUTES, ENTRIES, SYMMETRIC, clarityMesh} from './testing/meshClarityFixture.mjs';

test('mesh board clarity: lines in use stand out, idle ones stay quiet, links bow around nodes', {skip: !chromium, timeout: 600000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'mesh-clarity', entries: ['virtual:mesh-clarity', '/src/views/ServersView.vue', '/src/servers/mesh/MeshView.vue', '/src/servers/mesh/MeshBoard.vue'], plugins: [{name: 'mesh-clarity-entry', resolveId(id) { if (id === 'virtual:mesh-clarity') return '\0virtual:mesh-clarity'; }, load(id) { if (id === '\0virtual:mesh-clarity') return entry; }, configureServer(s) { s.middlewares.use((req, res, next) => { if (req.url?.split('?')[0] === '/__clarity') { res.setHeader('content-type', 'text/html'); res.end(html); } else next(); }); }}]});
  let browser;
  t.after(async () => { try { await browser?.close(); } finally { await fixture.close(); } });
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  if (shots) mkdirSync(shots, {recursive: true});

  async function open(t, {width = 1440, height = 1000, dark = false, broken = false} = {}) {
    const context = await browser.newContext({viewport: {width, height}, deviceScaleFactor: 1, colorScheme: dark ? 'dark' : 'light', hasTouch: width < 760, isMobile: width < 760});
    t.after(() => context.close());
    const page = await context.newPage(), errors = [], view = clarityMesh({broken});
    const items = NODES.map(([id, name, region]) => server(id, name, region, id === 'mac' ? 'darwin' : id === 'win' ? 'windows' : 'linux'));
    page.on('pageerror', error => errors.push(error.message));
    // The panel folded away on the desktop: the board as wide as it gets.
    await page.addInitScript(isDark => { window.__dark = isDark; sessionStorage.setItem('openbear.servers.tabs', JSON.stringify({tabs: [], active: 'mesh'})); localStorage.setItem('openbear.mesh.folded', JSON.stringify({palette: true, panel: true})); }, dark);
    await page.route(/^https:\/\/(open\.er-api\.com|api\.frankfurter\.dev)\//, route => route.fulfill({status: 503, body: ''}));
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url()), path = url.pathname, method = route.request().method();
      let status = 200, data = {ok: true};
      if (path === '/api/servers/mesh' && method === 'GET') data = view;
      else if (path === '/api/servers/mesh/layout') data = {ok: true, layout: view.layout};
      else if (/^\/api\/servers\/mesh\/members\/[\w-]+$/.test(path)) data = {ok: true, status: {peers: []}, events: []};
      else if (path.startsWith('/api/servers/mesh')) [status, data] = [404, {ok: false, error: 'not_found'}];
      else if (path === '/api/servers' && method === 'GET') data = {ok: true, items, settings: {accessPoints: [], defaultAccess: '', timezone: 'Asia/Shanghai'}, node: {available: true, version: '0.13.1'}};
      else if (path === '/api/servers/live') data = {ok: true, at: Date.now(), items: Object.fromEntries(items.map(i => [i.id, i.live]))};
      else if (path.endsWith('/metrics')) data = {ok: true, step: 3, points: {t: [], cpu: [], steal: [], mem: [], rxRate: [], txRate: [], ioRead: [], ioWrite: [], load: [], tcp: [], udp: []}};
      else [status, data] = [404, {ok: false, error: 'not_found'}];
      await route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    });
    await page.goto(fixture.base + '/__clarity', {timeout: 60000});
    await waitForBrowserReady(page, page.locator('#app > *').first(), errors);
    await page.locator('.vue-flow__node').nth(NODES.length - 1).waitFor();
    // The board has fitted its view (finite animations done) and drawn every line.
    await page.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getComputedTiming().endTime !== Infinity).map(a => a.finished.catch(() => {}))));
    await page.locator(`.mesh-edge-label[data-edge="${'direct:0'}"]`).waitFor();
    return {page, close: async () => { await context.close(); assert.deepEqual(errors, []); }};
  }
  const node = (page, id) => page.locator(`.vue-flow__node[data-id="${id}"]`);
  const edge = (page, id) => page.locator(`[data-id="${id}"] .mesh-edge`);
  const labelOf = (page, id) => page.locator(`.mesh-edge-label[data-edge="${id}"]`);
  const quietOnes = page => page.locator('.mesh-edge[data-quiet]').evaluateAll(list => list.map(el => el.closest('[data-id]').dataset.id).sort());
  const dimmedNodes = page => page.locator('.mesh-node').evaluateAll(list => list.filter(el => el.hasAttribute('data-dim')).map(el => el.closest('.vue-flow__node').dataset.id).sort());
  const shot = async (page, name) => { if (shots) { await page.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getComputedTiming().endTime !== Infinity).map(a => a.finished.catch(() => {})))); await page.screenshot({path: `${shots}/${name}.png`}); } };
  // Points along a link's drawn line, in page pixels.
  const along = (page, id, ks) => page.locator(`[data-id="${id}"] .mesh-edge-line`).evaluate((el, ks) => {
    const len = el.getTotalLength(), m = el.getScreenCTM();
    return ks.map(k => { const p = el.getPointAtLength(len * k); return {x: p.x * m.a + p.y * m.c + m.e, y: p.x * m.b + p.y * m.d + m.f}; });
  }, ks);
  // The relay links by their ends (pair:<index> in the order above).
  const pair = (a, b) => `pair:${PAIRS.findIndex(p => [p[0], p[1]].sort().join('|') === [a, b].sort().join('|'))}`;
  const QUIET = [pair('rex', 'sg'), pair('toy', 'rex'), pair('toy', 'berlin'), 'entry:sea:c2', 'entry:lax02:c2', 'entry:mac:c2', 'entry:mac:c3', 'entry:win:c2', 'entry:win:c3'].sort();
  // Which lines are quiet, which carry labels and what pointing at or selecting a node does are in
  // mesh.clarity.dom.test.mjs; here what needs a real layout: how quiet lines are drawn, the arc
  // round Rex and what a click on it hits, pointing at a thin line, phone and dark.
  await t.test('desktop: quiet lines, the arc round Rex, a node pointed at brings its lines forward', async t => {
    const {page, close} = await open(t);
    // Not in use: quiet, drawn thin and faint.
    const thin = await page.locator(`[data-id="${pair('rex', 'sg')}"] .mesh-edge-line`).evaluate(el => [getComputedStyle(el).strokeWidth, getComputedStyle(el).strokeOpacity]);
    assert.ok(parseFloat(thin[0]) <= 1 && parseFloat(thin[1]) <= 0.4, `quiet line ${thin}`);

    // SG–VLINE runs straight through Rex's row: it bows round Rex, clear of its box.
    const rex = await node(page, 'rex').boundingBox(), sgVmiss = pair('vline', 'sg');
    const points = await along(page, sgVmiss, Array.from({length: 41}, (_, i) => i / 40));
    for (const p of points) assert.ok(p.x < rex.x - 4 || p.x > rex.x + rex.width + 4 || p.y < rex.y - 4 || p.y > rex.y + rex.height + 4, `SG–VLINE at ${JSON.stringify(p)} runs under Rex`);
    const [apex] = await along(page, sgVmiss, [0.5]);
    assert.ok(apex.y < rex.y || apex.y > rex.y + rex.height, 'the arc passes above or below Rex');
    // Its label sits on the arc, and the arc is what a click hits.
    const tag = await labelOf(page, sgVmiss).boundingBox(), onArc = await along(page, sgVmiss, Array.from({length: 81}, (_, i) => i / 80));
    assert.ok(onArc.some(p => Math.abs(p.x - (tag.x + tag.width / 2)) < 3 && Math.abs(p.y - (tag.y + tag.height / 2)) < 3), 'label on the arc');
    // A click on the arc, away from the label, selects the link; one where the straight line was selects Rex.
    // The arc's own hit area is on top along its bowed middle (where no other line or label crosses it).
    const middle = await along(page, sgVmiss, Array.from({length: 21}, (_, i) => 0.3 + i * 0.02));
    const onTop = [];
    for (const p of middle) {
      const top = await page.evaluate(({x, y}) => { const el = document.elementFromPoint(x, y); return el?.classList.contains('mesh-edge-hit') ? el.closest('[data-id]')?.dataset.id : el?.className?.baseVal ?? el?.className; }, p);
      if (top === sgVmiss && (p.x < tag.x - 6 || p.x > tag.x + tag.width + 6)) onTop.push(p);
    }
    assert.ok(onTop.length >= 4, `the arc is clickable along its middle (${onTop.length} points)`);
    const hit = onTop[Math.floor(onTop.length / 2)];
    await page.mouse.click(hit.x, hit.y);
    await page.locator('.mesh-peek h3', {hasText: '中转互联'}).waitFor();
    assert.equal(await edge(page, sgVmiss).getAttribute('data-selected'), 'true');
    await page.keyboard.press('Escape');
    await page.mouse.click(rex.x + rex.width / 2, rex.y + rex.height / 2);
    await page.locator('.mesh-peek h3', {hasText: 'Rex.LAX'}).waitFor();
    // An empty spot of the board clears the selection (and the focus ring).
    await page.locator('.vue-flow__pane').click({position: {x: 20, y: 20}});
    await page.locator('.mesh-peek').waitFor({state: 'detached'});
    await page.mouse.move(5, 5);
    await shot(page, 'desktop-light');

    // Pointing at a quiet line (thin as it is) shows it as usual, label included.
    const [mid] = await along(page, pair('toy', 'berlin'), [0.4]);
    await page.mouse.move(mid.x, mid.y);
    await labelOf(page, pair('toy', 'berlin')).waitFor();
    assert.equal(await edge(page, pair('toy', 'berlin')).getAttribute('data-quiet'), null);
    await page.mouse.move(5, 5);
    await labelOf(page, pair('toy', 'berlin')).waitFor({state: 'detached'});
    if (shots) {
      await node(page, 'rex').hover();
      await page.waitForFunction(() => document.querySelectorAll('.mesh-node[data-dim]').length > 0);
      await shot(page, 'desktop-light-hover-zgo');
      await page.mouse.move(5, 5);
      await node(page, 'mac').click();
      await page.mouse.move(5, 5);
      await shot(page, 'desktop-light-select-mac');
    }
    await close();
  });

  await t.test('desktop: a line with a problem is never quiet', async t => {
    const {page, close} = await open(t, {broken: true});
    // TOY–Berlin carries nothing and is down: drawn at full strength.
    const line = await page.locator(`[data-id="${pair('toy', 'berlin')}"] .mesh-edge-line`).evaluate(el => parseFloat(getComputedStyle(el).strokeOpacity));
    assert.ok(line >= 0.8, `down line opacity ${line}`);
    await close();
  });

  await t.test('desktop dark', async t => {
    const {page, close} = await open(t, {dark: true});
    await shot(page, 'desktop-dark');
    await node(page, 'sg').hover();
    await page.waitForFunction(() => document.querySelectorAll('.mesh-node[data-dim]').length > 0);
    await shot(page, 'desktop-dark-hover-sg');
    await close();
  });

  await t.test('phone: quiet lines and arcs too; a tap on a node brings its lines forward', async t => {
    for (const dark of [false, true]) {
      const {page, close} = await open(t, {width: 390, height: 844, dark});
      await shot(page, `phone-${dark ? 'dark' : 'light'}`);
      // A real tap on a node selects it (and opens the sheet).
      await node(page, 'rex').tap();
      const sheet = page.locator('.mesh-inspect-sheet');
      await sheet.waitFor();
      await sheet.getByRole('button', {name: '关闭'}).tap();
      await sheet.waitFor({state: 'hidden'});
      await shot(page, `phone-${dark ? 'dark' : 'light'}-select-zgo`);
      await page.locator('.mesh-dock').getByRole('button', {name: '取消选择'}).tap();
      await page.waitForFunction(() => !document.querySelector('.mesh-node[data-dim]'));
      await close();
    }
  });
});
