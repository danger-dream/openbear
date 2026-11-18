import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdirSync} from 'node:fs';
import {startBrowserTestServer} from '../browserTestServer.mjs';

const require = createRequire(import.meta.url);
let chromium;
try {({chromium} = require('../testing/playwright.cjs'));} catch {}
// The system default block as the settings page shows it, against a stubbed extensions API.
const entry = `
import {createApp,h} from 'vue';
import ElementPlus from 'element-plus';
import 'element-plus/dist/index.css';
import 'element-plus/theme-chalk/dark/css-vars.css';
import '/src/style.css'; import '/src/dark-theme.css';
import SystemExtensionDefaults from '/src/components/SystemExtensionDefaults.vue';
createApp({render:()=>h('main',{style:'max-width:960px;margin:0 auto;padding:12px'},[h(SystemExtensionDefaults)])}).use(ElementPlus).mount('#app');
`;
const html = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:system-extensions"></script></body></html>';
const shots = process.env.OPENBEAR_SYSTEM_EXTENSION_SHOTS;
const catalog = [
  {key: 'workitems', label: '任务面板', hint: '记录问题、决策、进展和验收依据', tokens: 760, requires: []},
  {key: 'agents', label: 'Agent 协作', hint: '把工作交给独立的 Agent 并监督它们', tokens: 5600, requires: []},
  {key: 'conversations', label: '会话协作', hint: '向其他会话发请求，组建和带领会话小组；会一并开启 Agent 协作，用它等待对方的结果', tokens: 1900, requires: ['agents']},
  {key: 'mcp:parrot', label: 'parrot', hint: 'MCP 服务：' + 'web_search, web_fetch, image_generate, image_edit, '.repeat(3), tokens: 12500, requires: []},
];

function payload(stored) {
  const enabled = stored ? stored.enabled : [];
  const value = {enabled, autoAllow: Boolean(stored?.autoAllow), from: stored ? '__system' : '', explicit: Boolean(stored)};
  return {ok: true, default: {...value, own: Boolean(stored)}, inherited: {enabled: [], autoAllow: false, from: '', explicit: false}, canInherit: false,
    extensions: catalog.map(item => ({...item, enabled: enabled.includes(item.key)}))};
}

test('the system extension default saves each switch and fits desktop and phone widths', {skip: !chromium, timeout: 180000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'system-extensions', entries: ['virtual:system-extensions'], plugins: [{
    name: 'system-extensions',
    resolveId(id) {if (id === 'virtual:system-extensions') return '\0virtual:system-extensions';},
    load(id) {if (id === '\0virtual:system-extensions') return entry;},
    configureServer(server) {server.middlewares.use((req, res, next) => {if (req.url?.startsWith('/fixture')) {res.setHeader('content-type', 'text/html'); res.end(html);} else next();});},
  }]});
  let browser;
  t.after(async () => {try {await browser?.close();} finally {await fixture.close();}});
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  if (shots) mkdirSync(shots, {recursive: true});
  for (const width of [1440, 390]) for (const dark of [false, true]) await t.test(`${width}px ${dark ? 'dark' : 'light'}`, {timeout: 45000}, async t => {
    const phone = width < 761;
    const context = await browser.newContext({viewport: {width, height: 900}, hasTouch: phone, isMobile: phone});
    t.after(() => context.close());
    const page = await context.newPage(), errors = [], saved = [];
    let stored = null;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/conversation-folders/__system/extensions', async route => {
      if (route.request().method() === 'PUT') {
        stored = route.request().postDataJSON();
        saved.push(stored);
      }
      await route.fulfill({json: payload(stored)});
    });
    await page.goto(`${fixture.base}/fixture`);
    await page.evaluate(dark => document.documentElement.classList.toggle('dark', dark), dark);
    const block = page.locator('.system-extensions');
    await block.locator('.system-extensions__row').first().waitFor();
    assert.equal(await block.locator('.system-extensions__state').innerText(), '未设置');
    assert.match(await block.locator('.system-extensions__foot').innerText(), /还没有设置，新会话的扩展全部关闭/);
    const row = label => block.locator('.system-extensions__list .system-extensions__row').filter({has: page.getByText(label, {exact: true})});
    const on = () => block.locator('.system-extensions__list .system-extensions__row').evaluateAll(els => els.filter(el => el.querySelector('.el-switch.is-checked')).map(el => el.querySelector('.system-extensions__text > span').textContent));

    // An extension comes with what it requires and goes with it.
    await row('会话协作').locator('.el-switch').click();
    await block.locator('.system-extensions__state', {hasText: '已开启 2 项'}).waitFor();
    assert.deepEqual(saved.at(-1), {enabled: ['agents', 'conversations'], autoAllow: false});
    assert.deepEqual(await on(), ['Agent 协作', '会话协作']);
    assert.match(await block.locator('.system-extensions__foot').innerText(), /已选 2 项\s+每次请求约多 7.5K token/);
    await block.locator('.system-extensions__auto .el-switch').click();
    await page.waitForFunction(() => document.querySelector('.system-extensions__auto .el-switch')?.classList.contains('is-checked'));
    assert.deepEqual(saved.at(-1), {enabled: ['agents', 'conversations'], autoAllow: true});
    if (shots) {
      await page.evaluate(() => Promise.all(document.getAnimations().map(a => a.finished.catch(() => {}))));
      await block.screenshot({path: `${shots}/${phone ? 'phone' : 'desktop'}-${dark ? 'dark' : 'light'}.png`});
    }
    await row('Agent 协作').locator('.el-switch').click();
    await block.locator('.system-extensions__state', {hasText: '已开启 0 项'}).waitFor();
    assert.deepEqual(saved.at(-1), {enabled: [], autoAllow: true});

    // Nothing scrolls sideways; switches stay clear of their text; long descriptions stop at two lines.
    const overflow = await page.evaluate(() => [...document.querySelectorAll('.system-extensions, .system-extensions *')].filter(el => el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).overflowX !== 'visible').map(el => el.className));
    assert.deepEqual(overflow, []);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'the page does not scroll sideways');
    const gaps = await page.evaluate(() => [...document.querySelectorAll('.system-extensions__row')].map(el => el.querySelector('.el-switch').getBoundingClientRect().left - el.querySelector('.system-extensions__text').getBoundingClientRect().right));
    for (const gap of gaps) assert.ok(gap >= 8, `switch ${gap}px from its text`);
    const hintLines = await page.evaluate(() => { const el = [...document.querySelectorAll('.system-extensions__text small')].at(-2); return Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight)); });
    assert.ok(hintLines <= 2, `a long description is cut to two lines, got ${hintLines}`);
    assert.deepEqual(errors, []);
  });
});
