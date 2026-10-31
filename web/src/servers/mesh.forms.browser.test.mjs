import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync} from 'node:fs';
import {startBrowserTestServer, waitForBrowserReady} from '../browserTestServer.mjs';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}
const shots = process.env.OPENBEAR_MESH_SHOTS || '';
const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:mesh-forms-test"></script></body></html>';
const entry = `import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import '/src/theme.js'; import 'element-plus/dist/index.css'; import '/src/style.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css';
if (window.__dark) document.documentElement.classList.add('dark');
const component = (await import('/src/views/ServersView.vue')).default;
const app=createApp(component); app.use(ElementPlus); for (const [key,icon] of Object.entries(Icons)) app.component(key,icon); app.mount('#app');`;

import {GB, server, member, VIEW_KEYS, fakeMesh} from './testing/meshFormsFixture.mjs';

test('mesh member settings and rule sides: tags, help tooltips, address, ports, gateway networks, all gateway networks', {skip: !chromium, timeout: 600000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'mesh-forms', entries: ['virtual:mesh-forms-test', '/src/views/ServersView.vue', '/src/servers/mesh/MeshView.vue', '/src/servers/mesh/MeshBoard.vue'], plugins: [{name: 'mesh-forms-entry', resolveId(id) { if (id === 'virtual:mesh-forms-test') return '\0virtual:mesh-forms-test'; }, load(id) { if (id === '\0virtual:mesh-forms-test') return entry; }, configureServer(s) { s.middlewares.use((req, res, next) => { if (req.url?.split('?')[0] === '/__test') { res.setHeader('content-type', 'text/html'); res.end(html); } else next(); }); }}]});
  let browser;
  t.after(async () => { try { await browser?.close(); } finally { await fixture.close(); } });
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  if (shots) mkdirSync(shots, {recursive: true});

  async function setup(t, {width = 1440, height = 900, dark = false} = {}) {
    const phone = width < 760;
    const context = await browser.newContext({viewport: {width, height}, deviceScaleFactor: 1, colorScheme: dark ? 'dark' : 'light', hasTouch: phone, isMobile: phone});
    t.after(() => context.close());
    const page = await context.newPage(), calls = [], errors = [], mesh = fakeMesh();
    const items = [server('berlin', 'Berlin'), server('lax', 'LAX', {region: 'US'}), server('home', 'Home', {region: 'CN'})];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(([isDark]) => { window.__dark = isDark; sessionStorage.setItem('openbear.servers.tabs', JSON.stringify({tabs: [], active: 'mesh'})); }, [dark]);
    await page.route(/^https:\/\/(open\.er-api\.com|api\.frankfurter\.dev)\//, route => route.fulfill({status: 503, body: ''}));
    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url()), path = url.pathname, method = req.method();
      const raw = req.postDataBuffer();
      const body = raw && req.headers()['content-type']?.includes('json') ? JSON.parse(raw.toString()) : null;
      calls.push({path, method, body});
      let status = 200, data = {ok: true};
      if (path.startsWith('/api/servers/mesh')) [status, data] = mesh.handle(method, path.slice('/api/servers/mesh'.length), body);
      else if (path === '/api/servers' && method === 'GET') data = {ok: true, items, settings: {accessPoints: [], defaultAccess: '', timezone: 'Asia/Shanghai'}, node: {available: true, version: '0.13.1'}};
      else if (path === '/api/servers/live') data = {ok: true, at: Date.now(), items: Object.fromEntries(items.map(i => [i.id, i.live]))};
      else if (path.endsWith('/metrics')) data = {ok: true, step: 3, points: {t: [], cpu: [], steal: [], mem: [], rxRate: [], txRate: [], ioRead: [], ioWrite: [], load: [], tcp: [], udp: []}};
      await route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    });
    await page.goto(fixture.base + '/__test');
    await waitForBrowserReady(page, page.locator('#app > *').first(), errors);
    await page.locator('.vue-flow__node').first().waitFor();
    // The board has fitted its view (finite animations done).
    await page.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getComputedTiming().endTime !== Infinity).map(a => a.finished.catch(() => {}))));
    const puts = () => calls.filter(c => c.method === 'PUT' && c.path === '/api/servers/mesh/draft');
    return {page, calls, mesh, puts,
      saved: async (check, label = 'draft save') => { for (let i = 0; i < 60; i++) { const hit = puts().find(c => { try { return check(c.body); } catch { return false; } }); if (hit) return hit.body; await page.waitForTimeout(100); } assert.fail(`no ${label}`); },
      close: async () => { await context.close(); assert.deepEqual(errors, []); }};
  }
  const noOverflow = async page => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'page scrolls horizontally');
  const shot = async (page, name) => { if (shots) { await page.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getComputedTiming().endTime !== Infinity).map(a => a.finished.catch(() => {})))); await page.screenshot({path: `${shots}/${name}.png`}); } };
  const node = (page, id) => page.locator(`.vue-flow__node[data-id="${id}"]`);
  const memberOf = (body, id) => body.members.find(m => m.serverId === id);
  const tip = (page, text) => page.getByRole('tooltip').filter({hasText: text});
  await t.test('desktop: tags, tooltips, address and ports checked as typed, gateway networks', {timeout: 120000}, async t => {
    const h = await setup(t);
    const {page} = h;
    await node(page, 'home').click();
    const panel = page.locator('.mesh-inspector-col');
    await panel.getByRole('tab', {name: '配置'}).click();

    // Help: a tooltip on hover; the form below does not move.
    const address = panel.getByRole('textbox', {name: '网格地址'});
    const y = (await address.boundingBox()).y;
    await panel.getByRole('button', {name: '标签：说明'}).hover();
    await tip(page, '用于画板展示和访问规则里的分组').waitFor();
    assert.equal((await address.boundingBox()).y, y, 'the form stays put');
    await panel.getByRole('button', {name: '网格地址：说明'}).hover();
    await tip(page, '留空').waitFor();
    await tip(page, '用于画板展示').waitFor({state: 'hidden'});
    // From the keyboard too.
    await panel.getByRole('button', {name: '发送速率：说明'}).focus();
    await tip(page, '发往网格的速率').waitFor();
    await shot(page, 'desktop-light-help-tooltip');
    await page.mouse.move(5, 5);

    // Tags: suggestions from the other members, Enter, comma, leaving the field; Backspace and ✕ remove.
    const tags = panel.getByRole('combobox', {name: '添加标签'});
    await tags.click();
    const list = page.getByRole('listbox', {name: '添加标签：候选'});
    await list.getByRole('option').first().waitFor();
    await tags.pressSequentially('公');
    await tags.press('ArrowDown');
    await tags.press('Enter');
    await h.saved(d => memberOf(d, 'home').tags.join() === '家,公司', 'tag from a suggestion');
    await shot(page, 'desktop-light-tags');
    await tags.pressSequentially('测试,');
    await h.saved(d => memberOf(d, 'home').tags.join() === '家,公司,测试', 'tag by comma');
    await tags.pressSequentially('临时');
    await address.click();
    await h.saved(d => memberOf(d, 'home').tags.join() === '家,公司,测试,临时', 'tag on blur');
    await tags.click();
    await tags.press('Backspace');
    await h.saved(d => memberOf(d, 'home').tags.join() === '家,公司,测试', 'backspace removes the last');
    await panel.getByRole('button', {name: '删除 家'}).click();
    await h.saved(d => memberOf(d, 'home').tags.join() === '公司,测试', '✕ removes one');

    // Address: said as typed; only good ones reach the draft.
    const putsBefore = h.puts().length;
    const addressError = panel.locator('.mesh-field', {has: page.getByRole('textbox', {name: '网格地址'})}).locator('.ob-bad');
    for (const [value, text] of [['100.96.0.1', '和 Berlin 的地址重复'], ['100.97.0.8', '不在组网网段 100.96.0.0/16 内'], ['100.96.0.0', '100.96.0.0 是组网网段的网络地址，不能分给成员'],
      ['100.96.255.255', '100.96.255.255 是组网网段的广播地址，不能分给成员'], ['100.96.255.7', '100.96.255.0/24 留作控制地址，请换一个'], ['100.96.0.x', '写成 IPv4 地址，例如 100.96.0.10；留空自动分配']]) {
      await address.fill(value);
      assert.equal(await addressError.innerText(), text, value);
    }
    await address.press('Tab');
    await address.focus();
    await shot(page, 'desktop-light-address-error');
    await address.fill('100.96.');
    await address.fill('100.96.0.20');
    await address.press('Tab');
    await h.saved(d => memberOf(d, 'home').address === '100.96.0.20', 'a good address');
    // What only the controller knows comes back by path, in its words, under the field.
    await address.fill('100.96.0.99');
    await address.press('Tab');
    await addressError.filter({hasText: '这个地址给主控的网关留着，请换一个'}).waitFor();
    await address.fill('');
    await address.press('Tab');
    await h.saved(d => memberOf(d, 'home').address === '', 'empty: assigned on apply');
    await addressError.waitFor({state: 'detached'});

    // Ports: 1–65535, not another transport's; the port another program holds says so, and that apply will refuse it.
    const udp = panel.getByRole('textbox', {name: 'UDP 端口'});
    const udpNotes = panel.locator('.mesh-listen-note[data-port=udp]');
    await shot(page, 'desktop-light-port-in-use');
    const before = h.puts().length;
    await udp.fill('70000');
    await udpNotes.filter({hasText: '端口在 1–65535 之间'}).waitFor();
    await udp.fill('0');
    await udpNotes.filter({hasText: '端口在 1–65535 之间'}).waitFor();
    await udp.fill('52000');
    await udpNotes.filter({hasText: '和 TCP 端口相同'}).waitFor();
    await udp.press('Tab');
    await udp.fill('51888');
    await udp.press('Tab');
    await h.saved(d => memberOf(d, 'home').listen.udp === 51888, 'a good port');
    // The controller checks the new port when the draft is saved: the warning about 51999 goes.
    await udpNotes.filter({hasText: 'dnsmasq'}).waitFor({state: 'detached'});

    // Gateway: networks typed like tags, checked as typed, the machine's LAN networks offered.
    await panel.getByRole('checkbox', {name: '把局域网网段接入网格'}).check();
    const nets = panel.getByRole('combobox', {name: '添加网关网段'});
    await panel.locator('.mesh-field', {has: page.getByRole('combobox', {name: '添加网关网段'})}).getByText('网关至少要有一个网段').waitFor();
    await nets.click();
    const offered = page.getByRole('listbox', {name: '添加网关网段：候选'});
    await offered.getByRole('option').first().waitFor();
    // 10.20.5.0/24 is inside LAX's 10.20.0.0/16: not offered.
    await shot(page, 'desktop-light-gateway-suggest');
    const netError = panel.locator('.mesh-chips', {has: page.getByRole('combobox', {name: '添加网关网段'})}).locator('.ob-bad');
    await nets.pressSequentially('192.168.');
    for (const [value, text] of [['192.168.1.5/24', '192.168.1.5/24 不是网段的起始地址，应写成 192.168.1.0/24'], ['100.96.8.0/24', '和组网网段 100.96.0.0/16 重叠'],
      ['10.20.8.0/24', '和 LAX 的网段 10.20.0.0/16 重叠'], ['192.168.1.0/33', '写成网段，例如 192.168.1.0/24'], ['lan', '写成网段，例如 192.168.1.0/24']]) {
      await nets.fill(value);
      assert.equal(await netError.innerText(), text, value);
    }
    await nets.fill('');
    await offered.getByRole('option').first().click();
    await h.saved(d => memberOf(d, 'home').gateway?.subnets.join() === '192.168.50.0/24', 'a suggested network');
    await nets.pressSequentially('10.30.0.0/16,');
    await h.saved(d => memberOf(d, 'home').gateway.subnets.join() === '192.168.50.0/24,10.30.0.0/16', 'a network by comma');
    await nets.pressSequentially('172.16.5.1/24');
    await nets.press('Enter');
    await nets.fill('172.16.5.0/24');
    await address.click();
    await h.saved(d => memberOf(d, 'home').gateway.subnets.length === 3, 'a network on blur');
    // IP masquerade, explained in a tooltip; off, it says what the router needs.
    const masq = panel.getByRole('checkbox', {name: '地址转换（IP 伪装）'});
    await panel.getByRole('button', {name: '地址转换：说明'}).hover();
    await tip(page, '局域网里的设备看到的来源是网关这台机器的地址，所以不用在路由器上加回程路由').waitFor();
    await shot(page, 'desktop-light-masquerade');
    await masq.uncheck();
    await h.saved(d => memberOf(d, 'home').gateway.masquerade === false, 'masquerade off');
    await panel.getByText('需要在局域网路由器上把组网网段 100.96.0.0/16 指向这台网关').waitFor();
    await noOverflow(page);
    await h.close();
  });

  await t.test('phone, light and dark: tooltips by tap, tag suggestions, 16px inputs, rule sides in the sheet', {timeout: 150000}, async t => {
    for (const dark of [false, true]) {
      const h = await setup(t, {width: 390, height: 844, dark});
      const {page} = h;
      await node(page, 'home').tap();
      const sheet = page.locator('.mesh-inspect-sheet');
      await sheet.locator('.mesh-sheet-head h2', {hasText: 'Home'}).waitFor();
      await sheet.getByRole('tab', {name: '配置'}).tap();
      const tags = sheet.getByRole('combobox', {name: '添加标签'});
      await tags.waitFor();
      for (const input of await sheet.locator('input:not([type=checkbox]):not([type=radio])').all()) assert.ok(await input.evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16), 'inputs at 16px');
      // A tap opens the explanation, a tap elsewhere closes it; it does not push the form.
      const address = sheet.getByRole('textbox', {name: '网格地址'});
      const y = (await address.boundingBox()).y;
      const toggle = sheet.getByRole('button', {name: '标签：说明'});
      const box = await toggle.boundingBox();
      assert.ok(box.width >= 30 && box.height >= 30, `ⓘ ${box.width}x${box.height}`);
      await toggle.tap();
      await tip(page, '用于画板展示和访问规则里的分组').waitFor();
      assert.equal((await address.boundingBox()).y, y);
      await shot(page, `phone-${dark ? 'dark' : 'light'}-help-tooltip`);
      await sheet.locator('.mesh-group-title', {hasText: '网络'}).tap();
      await tip(page, '用于画板展示和访问规则里的分组').waitFor({state: 'hidden'});
      await toggle.tap();
      await tip(page, '用于画板展示和访问规则里的分组').waitFor();
      await toggle.tap();
      await tip(page, '用于画板展示和访问规则里的分组').waitFor({state: 'hidden'});
      // Tag suggestions by tap.
      await tags.tap();
      const list = page.getByRole('listbox', {name: '添加标签：候选'});
      await list.getByRole('option', {name: '欧洲'}).waitFor();
      const option = await list.getByRole('option', {name: '欧洲'}).boundingBox();
      assert.ok(option.height >= 36, `option ${option.height}px tall`);
      await shot(page, `phone-${dark ? 'dark' : 'light'}-tag-suggestions`);
      await list.getByRole('option', {name: '欧洲'}).tap();
      await h.saved(d => memberOf(d, 'home').tags.join() === '家,欧洲', 'phone tag');
      // Gateway networks and the masquerade tooltip.
      await sheet.getByRole('checkbox', {name: '把局域网网段接入网格'}).tap();
      const nets = sheet.getByRole('combobox', {name: '添加网关网段'});
      await nets.tap();
      await page.getByRole('listbox', {name: '添加网关网段：候选'}).getByRole('option').first().tap();
      await h.saved(d => memberOf(d, 'home').gateway?.subnets.join() === '192.168.50.0/24', 'phone gateway network');
      await sheet.getByRole('button', {name: '地址转换：说明'}).tap();
      await tip(page, '不用在路由器上加回程路由').waitFor();
      await nets.scrollIntoViewIfNeeded();
      await shot(page, `phone-${dark ? 'dark' : 'light'}-gateway`);
      await sheet.getByRole('button', {name: '地址转换：说明'}).tap();
      // The port in use is said under the port, and nothing goes sideways.
      await sheet.locator('.mesh-listen-note[data-port=udp]', {hasText: 'dnsmasq'}).scrollIntoViewIfNeeded();
      await shot(page, `phone-${dark ? 'dark' : 'light'}-ports`);
      assert.ok(await sheet.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'sheet scrolls horizontally');
      await noOverflow(page);
      await h.close();
      // Rules in the overview sheet: both boxes on each side, the hint gone with all gateway networks.
      const p = await setup(t, {width: 390, height: 844, dark});
      await p.page.locator('.mesh-dock').getByRole('button', {name: '网格设置与访问规则'}).tap();
      await p.page.locator('.mesh-overview-sheet').getByRole('button', {name: '＋ 添加规则'}).tap();
      const box2 = p.page.getByRole('checkbox', {name: '目标：所有网关网段'});
      await box2.waitFor();
      const ruleSheet = p.page.locator('.mesh-sheet', {has: box2});
      await ruleSheet.getByRole('combobox', {name: '目标：添加成员或标签'}).selectOption('members:lax');
      await ruleSheet.getByText('LAX 只包括这台机器，不包括它的网段').waitFor();
      await box2.tap();
      await p.saved(d => d.rules[0]?.dst.allSubnets, 'phone dst all gateway networks');
      await ruleSheet.getByText('LAX 只包括这台机器，不包括它的网段').waitFor({state: 'detached'});
      const row = await ruleSheet.locator('.mesh-side-all').nth(3).boundingBox();
      assert.ok(row.height >= 36, `box row ${row.height}px`);
      assert.ok(await ruleSheet.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'rule editor fits the width');
      await shot(p.page, `phone-${dark ? 'dark' : 'light'}-rule-sides`);
      await noOverflow(p.page);
      await p.close();
    }
  });
});
