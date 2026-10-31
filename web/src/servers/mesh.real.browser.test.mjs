import test from 'node:test';
import assert from 'node:assert/strict';
import {existsSync, mkdirSync, readFileSync, statSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {createRequire} from 'node:module';
import {startBrowserTestServer, waitForBrowserReady} from '../browserTestServer.mjs';
const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}

// A review of the mesh page against responses exported from a running controller.
// OPENBEAR_MESH_REAL names the export directory (mesh.json, routes-<id>.json, member-<id>.json,
// and when present servers.json, trend-<id>.json, addresses-<id>.json); without it this file does
// nothing. The export holds real addresses and keys: it is read in place and never copied into the repository.
// OPENBEAR_MESH_SHOTS takes screenshots; OPENBEAR_MESH_TEXTS writes every text shown.
// OPENBEAR_MESH_REAL_MODES picks modes (desktop-light, desktop-dark, phone-light, phone-dark, edge-desktop-light,
// edge-phone-light, edge-phone-dark); OPENBEAR_MESH_REAL_MEMBERS picks members by name (comma separated);
// OPENBEAR_MESH_REAL_TABS picks tabs by key (status, entry, settings, peers, events, probe) and then leaves out links and rules.
// Screenshots: <mode>-<member>-<tab key>.png at the top of the panel as it opens, -end.png scrolled to its bottom
// when it is longer than the screen; tab keys stay the same whatever the tab is called, so two runs compare file by file.
const real = process.env.OPENBEAR_MESH_REAL || '';
const shots = process.env.OPENBEAR_MESH_SHOTS || '';
const texts = process.env.OPENBEAR_MESH_TEXTS || '';
const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:mesh-real"></script></body></html>';
const entry = `import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import '/src/theme.js'; import 'element-plus/dist/index.css'; import '/src/style.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css';
if (window.__dark) document.documentElement.classList.add('dark');
const component = (await import('/src/views/ServersView.vue')).default;
const app=createApp(component); app.use(ElementPlus); for (const [key,icon] of Object.entries(Icons)) app.component(key,icon); app.mount('#app');`;
// Times move with the clock: everything is shifted by how long ago the export was taken, so "5 分钟前" reads as it did then.
const exported = real && existsSync(join(real, 'mesh.json')) ? statSync(join(real, 'mesh.json')).mtimeMs : 0;
function shift(value, key, delta) {
  if (Array.isArray(value)) return value.map(v => shift(v, '', delta));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, shift(v, k, delta)]));
  if (typeof value === 'number' && value > 1.6e12 && value < 2.1e12) return value + delta;
  if (typeof value === 'string' && /UnixNano$/.test(key) && /^\d{18,20}$/.test(value)) return String(BigInt(value) + BigInt(Math.round(delta)) * 1000000n);
  return value;
}
const read = name => { const file = join(real, name); return existsSync(file) ? shift(JSON.parse(readFileSync(file, 'utf8')), '', Date.now() - exported) : null; };
// The names tabs had and have: the screenshots are named by what a tab is for.
const TAB_KEYS = {状态: 'status', 概览: 'status', 配置: 'settings', 设置: 'settings', 入口: 'entry', 对端: 'peers', 连接: 'peers', 事件: 'events', 测试: 'probe'};

// The server list: servers.json when exported, else a plain online entry per member, its region from its name.
const REGION = [[/SG|新加坡/i, 'SG'], [/LA|Los Angeles|US/, 'US'], [/./, 'CN']];
function server(m) {
  const region = REGION.find(([re]) => re.test(m.name))[1];
  const live = {online: true, connectedAtMs: Date.now() - 3600000, version: '0.13.1', caps: {exec: true, terminal: true}, at: Date.now(), m: null, features: ['mesh']};
  return {id: m.serverId, name: m.name, local: false, ssh: null, usedBy: [], region, install: null, access: null, live,
    profile: {group: '', region, sort: 0, billing: {price: 0, currency: '$', cycle: 30, startedAt: '', expiresAt: '', autoRenew: false}, traffic: {limit: 0, type: 'max', resetDay: 0}},
    node: {enrolledAtMs: Date.now() - 86400000, lastSeenAtMs: Date.now(), online: true, version: '0.13.1', latest: '0.13.1', updateAvailable: false, caps: {exec: true, terminal: true}, info: {platform: m.platform}, geo: null}, traffic: null};
}

// The edge cases, laid over the export: an offline member, a long name, many entries and a failed rollout.
// Picked by what they are, not by name: whatever the export calls its machines.
const LONG = 'Relay-Berlin-DE 法兰克福备用线路 Premium 10G 长名字测试';
let edgeMembers = [];
function edges(view, items) {
  const offline = view.members.find(m => m.role === 'member' && m.platform === 'linux' && m.serverId !== 'local');
  const berlin = view.members.find(m => m.role === 'relay' && m.relayMode !== 'site'), mac = view.members.find(m => m.platform === 'darwin');
  edgeMembers = [offline?.name, berlin && LONG, mac?.name].filter(Boolean);
  if (offline) {
    view.nodes[offline.serverId] = {...(view.nodes[offline.serverId] || {}), state: 'offline'};
    const item = items.find(i => i.id === offline.serverId);
    if (item) { item.live = null; if (item.node) item.node.online = false; }
  }
  if (berlin) { berlin.name = LONG; const item = items.find(i => i.id === berlin.serverId); if (item) item.name = LONG; }
  if (mac) {
    const relays = view.members.filter(m => m.role === 'relay' && !mac.entry.candidates.some(c => c.relay === m.serverId));
    relays.slice(0, 3).forEach((r, i) => mac.entry.candidates.push({id: `edge${i}`, relay: r.serverId, transport: ['tcp', 'udp', 'tls'][i]}));
  }
  view.apply = {running: false, revision: view.applied?.revision || 1, progress: [
    ...(offline ? [{serverId: offline.serverId, state: 'offline'}] : []), ...(berlin ? [{serverId: berlin.serverId, state: 'failed', error: 'apply_timeout'}] : []),
    ...view.members.filter(m => m !== offline && m !== berlin).map(m => ({serverId: m.serverId, state: 'done'}))]};
}

// The exported responses as they are; a draft save is echoed back as the new draft with a change listed,
// so the change bar can be seen. What was not exported answers 404 and the page goes without.
function realMesh({edge = false} = {}) {
  const view = read('mesh.json');
  const exportedItems = read('servers.json')?.items;
  const items = Array.isArray(exportedItems) && exportedItems.length ? exportedItems : view.members.map(server);
  if (edge) edges(view, items);
  let draft = null;
  const current = () => draft ? {...view, ...draft, draft: {dirty: true, changes: [{kind: 'rules', text: '修改访问规则'}]}} : view;
  return {
    view, items,
    handle(method, path, body, query) {
      const q = new URLSearchParams(query);
      if (method === 'GET' && path === '') return [200, current()];
      if (method === 'PUT' && path === '/draft') { draft = body; return [200, current()]; }
      if (method === 'POST' && path === '/discard') { draft = null; return [200, current()]; }
      if (method === 'PUT' && path === '/layout') return [200, {ok: true, layout: view.layout}];
      if (method === 'GET' && path === '/routes') { const r = read(`routes-${q.get('src')}.json`); return r ? [200, r] : [404, {ok: false, error: 'mesh_member_not_found'}]; }
      if (method === 'GET' && path === '/path') {
        const r = read(`routes-${q.get('src')}.json`)?.routes?.find(x => x.dst === q.get('dst'));
        return r ? [200, {ok: true, hops: r.hops, rttMs: r.rttMs, stale: r.stale}] : [404, {ok: false, error: 'mesh_member_not_found'}];
      }
      if (method === 'GET' && /^\/members\/[\w-]+\/trend$/.test(path)) { const r = read(`trend-${path.split('/')[2]}.json`); return r ? [200, r] : [404, {ok: false, error: 'not_found'}]; }
      if (method === 'GET' && /^\/members\/[\w-]+$/.test(path)) { const m = read(`member-${path.split('/').pop()}.json`); return m ? [200, m] : [404, {ok: false, error: 'not_found'}]; }
      return [404, {ok: false, error: 'not_found'}];
    },
  };
}

test('mesh page with exported real data: every node, tab, link, the rules layer, settings and the change bar', {skip: !chromium || !real || !existsSync(join(real, 'mesh.json')), timeout: 1800000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'mesh-real', entries: ['virtual:mesh-real', '/src/views/ServersView.vue', '/src/servers/mesh/MeshView.vue', '/src/servers/mesh/MeshBoard.vue'], plugins: [{name: 'mesh-real-entry', resolveId(id) { if (id === 'virtual:mesh-real') return '\0virtual:mesh-real'; }, load(id) { if (id === '\0virtual:mesh-real') return entry; }, configureServer(s) { s.middlewares.use((req, res, next) => { if (req.url?.split('?')[0] === '/__real') { res.setHeader('content-type', 'text/html'); res.end(html); } else next(); }); }}]});
  let browser;
  t.after(async () => { try { await browser?.close(); } finally { await fixture.close(); } });
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  if (shots) mkdirSync(shots, {recursive: true});
  if (texts) mkdirSync(texts, {recursive: true});

  async function open(t, {width, height, dark, edge}) {
    const context = await browser.newContext({viewport: {width, height}, deviceScaleFactor: 1, colorScheme: dark ? 'dark' : 'light', hasTouch: width < 760, isMobile: width < 760});
    t.after(() => context.close());
    const page = await context.newPage(), errors = [], mesh = realMesh({edge}), items = mesh.items;
    page.setDefaultTimeout(6000);
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(isDark => { window.__dark = isDark; sessionStorage.setItem('openbear.servers.tabs', JSON.stringify({tabs: [], active: 'mesh'})); }, dark);
    await page.route(/^https:\/\/(open\.er-api\.com|api\.frankfurter\.dev)\//, route => route.fulfill({status: 503, body: ''}));
    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url()), path = url.pathname, method = req.method();
      const raw = req.postDataBuffer();
      const body = raw && req.headers()['content-type']?.includes('json') ? JSON.parse(raw.toString()) : null;
      let status = 200, data = {ok: true};
      const net = /^\/api\/servers\/([^/]+)\/net\/addresses$/.exec(path);
      if (path.startsWith('/api/servers/mesh')) [status, data] = mesh.handle(method, path.slice('/api/servers/mesh'.length), body, url.search);
      else if (net) { const id = decodeURIComponent(net[1]), a = items.find(i => i.id === id)?.live ? read(`addresses-${id}.json`) : null; [status, data] = a ? [200, a] : [409, {ok: false, error: 'node_offline'}]; }
      else if (path === '/api/servers' && method === 'GET') data = {ok: true, items, settings: {accessPoints: [], defaultAccess: '', timezone: 'Asia/Shanghai'}, node: {available: true, version: '0.13.1'}};
      else if (path === '/api/servers/live') data = {ok: true, at: Date.now(), items: Object.fromEntries(items.map(i => [i.id, i.live]))};
      else if (path.endsWith('/metrics')) data = {ok: true, step: 3, points: {t: [], cpu: [], steal: [], mem: [], rxRate: [], txRate: [], ioRead: [], ioWrite: [], load: [], tcp: [], udp: []}};
      else [status, data] = [404, {ok: false, error: 'not_found'}];
      await route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    });
    await page.goto(fixture.base + '/__real', {timeout: 60000});
    await waitForBrowserReady(page, page.locator('#app > *').first(), errors);
    await page.locator('.vue-flow__node').first().waitFor();
    await page.waitForTimeout(600);
    return {page, mesh, errors};
  }
  const lines = [];
  const note = (where, text) => lines.push(`### ${where}\n${text.trim()}\n`);
  const slug = name => name.replace(/[^\w\u4e00-\u9fff-]+/g, '_');
  const wanted = (process.env.OPENBEAR_MESH_REAL_MEMBERS || '').split(',').map(s => s.trim()).filter(Boolean);
  const wantedTabs = (process.env.OPENBEAR_MESH_REAL_TABS || '').split(',').map(s => s.trim()).filter(Boolean);

  const only = (process.env.OPENBEAR_MESH_REAL_MODES || '').split(',').filter(Boolean);
  for (const [mode, width, height, dark, edge] of [['desktop-light', 1440, 900, false], ['desktop-dark', 1440, 900, true], ['phone-light', 390, 844, false], ['phone-dark', 390, 844, true],
    ['edge-desktop-light', 1440, 900, false, true], ['edge-phone-light', 390, 844, false, true], ['edge-phone-dark', 390, 844, true, true]]) {
    if (only.length ? !only.includes(mode) : edge) continue;
    await t.test(mode, {timeout: 900000}, async t => {
      const {page, mesh, errors} = await open(t, {width, height, dark, edge});
      const phone = width < 760;
      const panel = phone ? page.locator('.mesh-sheet') : page.locator('.mesh-inspector-col');
      const scroller = phone ? '.mesh-sheet-body' : '.mesh-inspector-col';
      const closeSheet = async () => { if (phone && await page.locator('.mesh-sheet').count()) { await page.locator('.mesh-sheet').getByRole('button', {name: '关闭'}).first().click(); await page.locator('.mesh-sheet').waitFor({state: 'detached'}).catch(() => {}); } };
      const scrollTo = where => page.evaluate(([sel, where]) => { const el = document.querySelector(sel); if (!el) return false; el.scrollTop = where === 'end' ? el.scrollHeight : 0; return el.scrollHeight > el.clientHeight + 40; }, [scroller, where]);
      const shot = async name => { if (shots) { await page.waitForTimeout(400); await page.screenshot({path: `${shots}/${name}.png`}); } };
      // The panel as it opens, and its end when it runs past the screen.
      const shotPanel = async name => {
        if (!shots) return;
        await scrollTo('top');
        await shot(name);
        if (await scrollTo('end')) { await page.waitForTimeout(200); await shot(`${name}-end`); }
        await scrollTo('top');
      };
      const expandAll = async () => {
        for (const more of await panel.locator('summary').all()) await more.click().catch(() => {});
        for (const more of await panel.getByRole('button', {name: /^展开|更多|显示全部|高级/}).all()) await more.click().catch(() => {});
      };
      const record = !dark || phone;
      await shot(`${mode}-board`);
      if (record) note(`${mode} board`, (await page.locator('.mesh-board, .mesh-view').first().innerText()) + '\n[labels]\n' + await page.locator('.mesh-edge-label').evaluateAll(l => l.map(e => `${e.textContent.trim()} [${e.getAttribute('aria-label') || ''}] [${e.title || ''}]${e.dataset.standby ? ' standby' : ''}${e.dataset.look ? ' look=' + e.dataset.look : ''}`).join('\n')));
      // Every node: its card, then each tab of its properties.
      const EDGE_MEMBERS = edgeMembers;
      for (const m of mesh.view.members) {
        if (edge ? !EDGE_MEMBERS.includes(m.name) : wanted.length && !wanted.some(w => m.name.includes(w))) continue;
        const card = page.locator(`.vue-flow__node[data-id="${m.serverId}"]`);
        if (record) note(`${mode} card ${m.name}`, (await card.innerText()) + `\n[title] ${await card.locator('[title]').evaluateAll(l => l.map(e => e.title).join(' | '))}`);
        // A card under the dock or the tool row is tapped through to the node all the same.
        if (phone) { await card.dispatchEvent('click'); await page.waitForTimeout(500); if (!await page.locator('.mesh-sheet').count()) await page.getByRole('button', {name: '属性'}).click(); }
        else await page.getByRole('button', {name: `选中 ${m.name}`}).click();
        await panel.getByRole('tablist').first().getByRole('tab').first().waitFor();
        await page.waitForTimeout(900);
        const tabs = await panel.getByRole('tablist').first().getByRole('tab').evaluateAll(l => l.map(e => e.textContent.trim()));
        for (const tab of tabs) {
          const key = TAB_KEYS[tab.replace(/\s.*$/, '')] || slug(tab);
          if (wantedTabs.length && !wantedTabs.includes(key)) continue;
          await panel.getByRole('tablist').first().getByRole('tab', {name: tab, exact: true}).click();
          await page.waitForTimeout(600);
          await shotPanel(`${mode}-${slug(m.name).slice(0, 40)}-${key}`);
          await expandAll();
          if (record) note(`${mode} ${m.name} / ${tab}`, (await panel.innerText()) + `\n[title] ${await panel.locator('[title]').evaluateAll(l => l.map(e => e.title).filter(Boolean).join(' | '))}`);
          if (edge) await shotPanel(`${mode}-${slug(m.name).slice(0, 40)}-${key}-open`);
        }
        await closeSheet();
      }
      if (!phone) await page.keyboard.press('Escape');
      // Every link: its label and its properties.
      if (wantedTabs.length) { assert.deepEqual(errors, []); return; }
      const links = edge ? [] : await page.locator('.vue-flow__edge').evaluateAll(l => l.map(e => e.getAttribute('data-id')));
      for (const id of links) try {
        const link = page.locator(`.vue-flow__edge[data-id="${id}"]`);
        await link.dispatchEvent('click');
        await page.waitForTimeout(500);
        const aria = await page.locator('.mesh-edge-label[data-selected]').evaluateAll(l => l.map(e => `${e.textContent.trim()} [${e.getAttribute('aria-label') || ''}] [${e.title || ''}]`).join(' / '));
        if (phone && !await page.locator('.mesh-sheet').count() && await page.getByRole('button', {name: '属性'}).count()) { await page.getByRole('button', {name: '属性'}).click(); await page.waitForTimeout(500); }
        if (await panel.count()) {
          await page.waitForTimeout(400);
          await shotPanel(`${mode}-link-${slug(id).slice(0, 60)}`);
          await expandAll();
          if (record) note(`${mode} link ${id}`, `[label] ${aria}\n` + await panel.innerText());
        }
        await closeSheet();
        if (!phone) await page.keyboard.press('Escape');
      } catch (error) { note(`${mode} link ${id}`, `[could not open] ${error.message.split('\n')[0]}`); await closeSheet().catch(() => {}); }
      // Rules layer, grid settings, one rule, and the change bar after an edit.
      if (phone) {
        if (await page.locator('.mesh-dock-close').count()) await page.locator('.mesh-dock-close').click();
        await page.getByRole('button', {name: '访问规则图层'}).click();
        await shot(`${mode}-rules-layer`);
        await page.getByRole('button', {name: '访问规则图层'}).click();
        await page.locator('.mesh-dock-entry').click();
        await page.waitForTimeout(600);
        await shotPanel(`${mode}-overview`);
        await expandAll();
        if (record) note(`${mode} overview`, await panel.innerText());
        if (!edge) {
          await panel.getByRole('button', {name: '编辑第 2 条规则'}).click();
          await page.waitForTimeout(600);
          await shotPanel(`${mode}-rule`);
          if (record) note(`${mode} rule`, await panel.innerText());
          await panel.getByRole('textbox', {name: '备注'}).fill('真机测试：允许 SSH');
          await panel.getByRole('textbox', {name: '备注'}).press('Enter');
          await panel.getByRole('textbox', {name: '备注'}).blur();
          await page.waitForTimeout(800);
          await closeSheet();
          await page.waitForTimeout(400);
          await shot(`${mode}-changes-bar`);
          if (await page.locator('.mesh-dock-close').count()) await page.locator('.mesh-dock-close').click();
          await page.locator('.mesh-dock-entry').click();
          await page.waitForTimeout(600);
          await shotPanel(`${mode}-changes`);
          if (record) note(`${mode} changes`, await panel.innerText());
        }
        await closeSheet();
      } else {
        await page.locator('.mesh-toolbar').getByRole('button', {name: '访问规则'}).click();
        await page.waitForTimeout(500);
        if (record) note(`${mode} rules layer`, (await page.locator('.mesh-board, .mesh-view').first().innerText()) + '\n' + await panel.innerText());
        await shot(`${mode}-rules-layer`);
        await page.locator('.mesh-toolbar').getByRole('button', {name: '访问规则'}).click();
        await page.keyboard.press('Escape');
        await page.waitForTimeout(500);
        await shotPanel(`${mode}-settings`);
        await expandAll();
        if (record) note(`${mode} settings`, await panel.innerText());
        if (!edge) {
          await panel.getByRole('button', {name: '编辑第 2 条规则'}).click();
          await page.waitForTimeout(600);
          await shotPanel(`${mode}-rule`);
          if (record) note(`${mode} rule`, await panel.innerText());
          await panel.getByRole('textbox', {name: '备注'}).fill('真机测试：允许 SSH');
          await panel.getByRole('textbox', {name: '备注'}).press('Enter');
          await panel.getByRole('textbox', {name: '备注'}).blur();
          await page.waitForTimeout(800);
          await page.keyboard.press('Escape');
          await page.waitForTimeout(500);
          await shotPanel(`${mode}-changes`);
          if (record) note(`${mode} changes`, await panel.innerText());
        }
      }
      assert.deepEqual(errors, []);
    });
  }
  if (texts) writeFileSync(join(texts, 'texts.md'), lines.join('\n'));
});
