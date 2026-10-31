import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync} from 'node:fs';
import {createRequire} from 'node:module';
import {startBrowserTestServer, waitForBrowserReady} from '../browserTestServer.mjs';
import {fakeDdns, handleApi} from './ddnsFixture.mjs';
const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}
const shots = process.env.OPENBEAR_DDNS_SHOTS || '';
const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:ddns-test"></script></body></html>';
// window.__card mounts the card of a server's detail page instead of the page; the page sits as in the
// servers page's tab body, window.__focus being a link to it.
const entry = `import {createApp, h} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import '/src/theme.js'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css';
if (window.__dark) document.documentElement.classList.add('dark');
const View = (await import('/src/ddns/DdnsView.vue')).default, Card = (await import('/src/ddns/DdnsCard.vue')).default;
await import('/src/instances/instances.css'); await import('/src/servers/servers.css');
const root = window.__card ? {render: () => h('div', {class: 'instance-ui servers-ui', style: 'padding:16px;max-width:720px'}, [h(Card, {serverId: window.__card, active: true})])}
  : {render: () => h('div', {class: 'instance-ui servers-ui sv-body', style: 'height:100vh'}, [h(View, {active: true, focus: window.__focus || null})])};
const app = createApp(root); app.use(ElementPlus); for (const [key, icon] of Object.entries(Icons)) app.component(key, icon); app.mount('#app');`;

// Layout smoke of the 动态域名 page in real Chromium: desktop, phone and dark without sideways
// scroll, compact phone rows, chips scrolling inside their row, bottom sheets with a drag to close,
// 16px inputs (no focus zoom), Enter making a new line, pickers inside the screen, aligned card
// values. The page's requests, texts, counts and states: ddns.dom.test.mjs.
test('动态域名 page: list, details, forms, node picker, phone and dark', {skip: !chromium, timeout: 600000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'ddns', entries: ['virtual:ddns-test', '/src/ddns/DdnsView.vue', '/src/ddns/DdnsCard.vue'], plugins: [{name: 'ddns-entry',
    resolveId(id) { if (id === 'virtual:ddns-test') return '\0virtual:ddns-test'; }, load(id) { if (id === '\0virtual:ddns-test') return entry; },
    configureServer(s) { s.middlewares.use((req, res, next) => { if (req.url?.split('?')[0] === '/__test') { res.setHeader('content-type', 'text/html'); res.end(html); } else next(); }); }}]});
  let browser;
  t.after(async () => { try { await browser?.close(); } finally { await fixture.close(); } });
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  if (shots) mkdirSync(shots, {recursive: true});

  async function setup(t, {width = 1440, height = 900, dark = false, card = '', path = '/__test', focus = null} = {}) {
    const phone = width < 760;
    const context = await browser.newContext({viewport: {width, height}, deviceScaleFactor: phone ? 2 : 1, colorScheme: dark ? 'dark' : 'light', hasTouch: phone, isMobile: phone});
    t.after(() => context.close().catch(() => {}));
    const page = await context.newPage(), errors = [], ddns = fakeDdns();
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(([isDark, card, focus]) => { window.__dark = isDark; window.__card = card; window.__focus = focus; }, [dark, card, focus]);
    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url()), method = req.method();
      const raw = req.postDataBuffer();
      const body = raw && req.headers()['content-type']?.includes('json') ? JSON.parse(raw.toString()) : null;
      const [status, data] = handleApi(ddns, method, url.pathname, body, url.searchParams);
      await route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    });
    await page.goto(fixture.base + path);
    await waitForBrowserReady(page, page.locator('#app > *').first(), errors);
    if (!card) await page.locator('.dd-table tbody tr').first().waitFor();
    return {page, ddns, errors, close: async () => { assert.deepEqual(errors, []); await context.close(); }};
  }
  const noOverflow = async (page, label = '') => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1 && document.body.scrollWidth <= innerWidth + 1), `page scrolls horizontally ${label}`);
  const shot = async (page, name) => { if (shots) { await page.waitForTimeout(450); await page.screenshot({path: `${shots}/${name}.png`}); } };
  const rows = page => page.locator('.dd-table tbody tr');
  const dialog = page => page.locator('.dd-dialog');
  // Waits until no CSS transition or animation runs (a sheet or dialog finished opening).
  const settled = page => page.waitForFunction(() => document.getAnimations().every(a => a.playState !== 'running'));

  // Requests, texts, counts and states of the desktop flows (lists, filters, details, forms,
  // the node picker, the provider form): ddns.dom.test.mjs.
  await t.test('desktop: list, details drawer, add forms and the node picker fit, light and dark', async t => {
    for (const dark of [false, true]) {
      const {page, close} = await setup(t, {dark});
      await noOverflow(page, `list ${dark}`);
      await rows(page).filter({hasText: 'hk.example.net'}).click();
      const drawer = page.locator('.dd-drawer');
      await drawer.locator('.dd-events li').first().waitFor();
      await settled(page);
      const box = await drawer.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= 1441, 'drawer inside the window');
      await noOverflow(page, `drawer ${dark}`);
      await drawer.locator('.dd-close').click();
      await drawer.waitFor({state: 'hidden'});
      await page.getByRole('button', {name: '添加解析'}).click();
      const d = dialog(page); await d.waitFor();
      await settled(page);
      await d.getByRole('button', {name: /^来源节点：/}).click();
      await page.locator('.dd-node-panel').waitFor();
      await settled(page);
      const panel = await page.locator('.dd-node-panel').boundingBox();
      assert.ok(panel.x >= 0 && panel.x + panel.width <= 1441 && panel.y + panel.height <= 901, 'picker inside the window');
      assert.ok(await page.locator('.dd-node-panel [role="option"]').first().isVisible());
      await page.getByRole('combobox', {name: '搜索节点'}).press('Escape');
      await noOverflow(page, `add record ${dark}`);
      await shot(page, `desktop-${dark ? 'dark-' : ''}add-record`);
      await close();
    }
  });

  await t.test('phone: compact list, bottom sheets with a drag to close, 16px inputs, no sideways scroll', async t => {
    const {page, close} = await setup(t, {width: 390, height: 844});
    await noOverflow(page, 'list');
    // Rows are two lines: name + type + state, then source and value.
    const first = rows(page).first();
    const box = await first.boundingBox();
    assert.ok(box.height < 70, `row height ${box.height}`);
    // (the row's words: ddns.dom.test.mjs)
    const [nameBox, stateBox, valueBox] = await Promise.all(['.dd-c-domain', '.dd-c-state', '.dd-c-val'].map(s => first.locator(s).boundingBox()));
    assert.ok(Math.abs(nameBox.y - stateBox.y) < 8 && valueBox.y > nameBox.y + 10, 'state beside the name, source and value below');
    const fontSize = sel => page.locator(sel).first().evaluate(el => getComputedStyle(el).fontSize);
    assert.equal(await fontSize('.dd-search input'), '16px');
    // The accounts are a row of chips scrolled inside itself.
    assert.ok(await page.locator('.dd-providers').evaluate(el => el.scrollWidth > el.clientWidth));
    await page.locator('.dd-prov[data-account="a_cf"]').click();
    await page.locator('.dd-acct-bar').waitFor();
    await noOverflow(page, 'account bar');
    await shot(page, 'phone-list');
    await page.locator('.dd-prov').first().click();
    // Details in the bottom sheet.
    await rows(page).filter({hasText: 'hk.example.net'}).click();
    const sheet = page.locator('.mesh-sheet.dd-sheet:visible');
    await sheet.waitFor();
    await sheet.locator('.dd-events li').first().waitFor();
    await noOverflow(page, 'detail sheet');
    assert.ok(await sheet.getByRole('button', {name: '关闭'}).isVisible(), 'a close button');
    await shot(page, 'phone-detail');
    // Drag the header down: it follows, then slides away.
    await settled(page);
    const head = await sheet.locator('.mesh-sheet-head').boundingBox();
    await page.mouse.move(head.x + 60, head.y + 10);
    await page.mouse.down();
    await page.mouse.move(head.x + 60, head.y + 60, {steps: 5});
    assert.ok(await sheet.evaluate(el => el.classList.contains('is-dragging') && /translateY\(\d+/.test(el.style.transform)), 'follows the finger');
    await page.mouse.move(head.x + 60, head.y + 200, {steps: 5});
    await page.mouse.up();
    await sheet.waitFor({state: 'hidden'});
    // The add form in a sheet: inputs at 16px, nothing wider than the screen.
    await page.getByRole('button', {name: '添加解析'}).click();
    await sheet.waitFor();
    for (const sel of ['.dd-sheet textarea.dd-domains', '.dd-sheet .dd-adv select']) {
      if (sel.includes('select')) await sheet.locator('.dd-adv > summary').click();
      assert.equal(await fontSize(sel), '16px', sel);
    }
    await sheet.getByRole('textbox', {name: /域名/}).fill('take.example.com\nnew-very-long-subdomain-name-for-layout.example.com');
    await sheet.locator('.dd-pr[data-action]').first().waitFor();
    await noOverflow(page, 'add sheet');
    // Enter makes a new line in the domains box; nothing is sent.
    const before = (await sheet.getByRole('textbox', {name: /域名/}).inputValue()).split('\n').length;
    await sheet.getByRole('textbox', {name: /域名/}).press('End');
    await sheet.getByRole('textbox', {name: /域名/}).press('Enter');
    assert.equal((await sheet.getByRole('textbox', {name: /域名/}).inputValue()).split('\n').length, before + 1);
    // The node picker fits the screen.
    await sheet.getByRole('button', {name: /^来源节点：/}).click();
    const panel = await page.locator('.dd-node-panel').boundingBox();
    assert.ok(panel.x >= 0 && panel.x + panel.width <= 391, 'picker inside the screen');
    await page.getByRole('combobox', {name: '搜索节点'}).press('Escape').catch(() => {});
    await page.locator('.dd-node-panel').waitFor({state: 'hidden'}).catch(() => {});
    await shot(page, 'phone-add-record');
    await sheet.getByRole('button', {name: '关闭'}).click();
    await sheet.waitFor({state: 'hidden'});
    // 添加服务商 in a sheet.
    await page.locator('.dd-prov-add').click();
    await sheet.waitFor();
    assert.equal(await fontSize('.dd-sheet .dd-provider-search'), '16px');
    await sheet.locator('.dd-pcard[data-type="aliyun"]').click();
    assert.equal(await fontSize('.dd-sheet #dd-cred-access_key_id'), '16px');
    await noOverflow(page, 'provider sheet');
    await shot(page, 'phone-add-provider');
    await close();
  });

  await t.test('dark theme and the phone in dark', async t => {
    for (const [width, height, name] of [[1440, 900, 'desktop'], [390, 844, 'phone']]) {
      const {page, close} = await setup(t, {width, height, dark: true});
      assert.ok(await page.evaluate(() => document.documentElement.classList.contains('dark')));
      const bg = await page.locator('.dd-table tbody tr td').first().evaluate(el => getComputedStyle(el.closest('.dd-page')).color);
      assert.notEqual(bg, 'rgb(0, 0, 0)', 'text is light on dark');
      await rows(page).filter({hasText: 'old.example.org'}).click();
      await page.locator(width < 760 ? '.dd-sheet' : '.dd-drawer').locator('.dd-events li').first().waitFor();
      await shot(page, `${name}-dark-detail`);
      await close();
    }
  });

  // The card's records, its links and "no card for a non-node": ddns.dom.test.mjs.
  await t.test('a server\'s detail card: values line up; fits the phone', async t => {
    const {page, close} = await setup(t, {card: 'home'});
    const card = page.locator('.dd-card');
    await card.locator('.dd-card-row').first().waitFor();
    const lefts = await card.locator('.dd-card-value').evaluateAll(els => els.map(el => Math.round(el.getBoundingClientRect().left)));
    assert.equal(new Set(lefts).size, 1, `values line up: ${lefts}`);
    await shot(page, 'card');
    await close();
    const phone = await setup(t, {card: 'home', width: 390, height: 844});
    await phone.page.locator('.dd-card-row').first().waitFor();
    await noOverflow(phone.page, 'card');
    await shot(phone.page, 'phone-card');
    await phone.close();
  });

});
