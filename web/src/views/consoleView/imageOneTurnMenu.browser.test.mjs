import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from '../../browserTestServer.mjs';

const require = createRequire(import.meta.url);
let chromium;
try {({chromium} = require('../../testing/playwright.cjs'));} catch {}
const entry = `
import {createApp,h,ref} from 'vue';
import ElementPlus from 'element-plus';
import 'element-plus/dist/index.css';
import 'element-plus/theme-chalk/dark/css-vars.css';
import '/src/style.css'; import '/src/dark-theme.css'; import '/src/mobile-overlays.css';
import ImageOneTurnMenu from '/src/views/consoleView/ImageOneTurnMenu.vue';
const uuid=new URLSearchParams(location.search).get('uuid')||'conv-1';
const value=ref({enabled:false,includeUser:true,agents:true});
window.__changes=[];
const change=patch=>{window.__changes.push(patch);value.value={...value.value,...patch};};
const trigger=({label,open,on})=>h('button',{type:'button',class:['tool-btn','image-one-turn-trigger',{'is-on':on}],'aria-label':label,'aria-expanded':String(open)},['img',on?h('span',{class:'image-one-turn-mark'},'1'):null]);
createApp({render:()=>h('div',{class:'composer-toolbar',style:'position:fixed;left:8px;right:8px;bottom:8px;display:flex'},[h('div',{class:'composer-actions'},[
 h(ImageOneTurnMenu,{value:value.value,draft:uuid.startsWith('local:'),conversationUuid:uuid,onChange:change},{trigger})])])}).use(ElementPlus).mount('#app');
`;
const html = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:image-one-turn-menu"></script></body></html>';
const plugin = {
  name: 'image-one-turn-menu',
  resolveId(id) {if (id === 'virtual:image-one-turn-menu') return '\0virtual:image-one-turn-menu';},
  load(id) {if (id === '\0virtual:image-one-turn-menu') return entry;},
  configureServer(server) {server.middlewares.use((req, res, next) => {if (req.url?.startsWith('/fixture')) {res.setHeader('content-type', 'text/html'); res.end(html);} else next();});},
};

test('the 图片只看一轮 panel gates its follow-up switches and reports each change on desktop and phone', {skip: !chromium, timeout: 180000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'image-one-turn-menu', entries: ['virtual:image-one-turn-menu'], plugins: [plugin]});
  let browser;
  t.after(async () => {try {await browser?.close();} finally {await fixture.close();}});
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  for (const width of [1440, 390]) for (const dark of [false, true]) await t.test(`${width}px ${dark ? 'dark' : 'light'}`, {timeout: 45000}, async t => {
    const phone = width < 761;
    const context = await browser.newContext({viewport: {width, height: 844}, hasTouch: phone, isMobile: phone});
    t.after(() => context.close());
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`${fixture.base}/fixture`);
    await page.evaluate(dark => document.documentElement.classList.toggle('dark', dark), dark);
    await page.getByRole('button', {name: '图片只看一轮：关闭'}).click();
    const panel = page.locator(phone ? '.image-one-turn-sheet .iot-panel' : '.image-one-turn-popper .iot-panel');
    await panel.waitFor({state: 'visible'});
    assert.match(await panel.locator('.iot-note').innerText(), /本会话 · 关闭/);
    const rows = panel.locator('.iot-row');
    assert.equal(await rows.count(), 3);
    const disabled = index => rows.nth(index).locator('.el-switch').evaluate(el => el.classList.contains('is-disabled'));
    // Off: the follow-up switches keep their values but cannot change.
    assert.deepEqual([await disabled(0), await disabled(1), await disabled(2)], [false, true, true]);
    await rows.nth(1).locator('.el-switch').click({force: true});
    assert.deepEqual(await page.evaluate(() => window.__changes), []);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'no page-wide horizontal overflow');
    assert.ok(await panel.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'panel fits its width');
    for (const row of await rows.all()) assert.ok(await row.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'row fits');
    if (phone) for (const row of await rows.all()) assert.ok((await row.boundingBox()).height >= 44, 'rows are comfortable touch targets');

    await rows.nth(0).locator('.el-switch').click();
    await page.getByRole('button', {name: '图片只看一轮：已开启'}).waitFor({state: 'attached'});
    assert.match(await panel.locator('.iot-note').innerText(), /本会话 · 已开启/);
    assert.equal(await page.locator('.image-one-turn-mark').innerText(), '1');
    assert.deepEqual([await disabled(1), await disabled(2)], [false, false]);
    await rows.nth(1).locator('.el-switch').click();
    await rows.nth(2).locator('.el-switch').click();
    assert.deepEqual(await page.evaluate(() => window.__changes), [{enabled: true}, {includeUser: false}, {agents: false}]);
    if (phone) {
      // A downward drag on the header follows the finger and closes the sheet.
      const head = await panel.locator('.iot-sheet-head').boundingBox();
      const x = head.x + head.width / 2 - 60, y = head.y + head.height / 2;
      await page.mouse.move(x, y); await page.mouse.down();
      for (let dy = 10; dy <= 160; dy += 15) await page.mouse.move(x, y + dy);
      await page.mouse.up();
      await page.locator('.image-one-turn-sheet').waitFor({state: 'hidden'});
    }
    assert.deepEqual(errors, []);
  });
});

test('a new conversation says its choice takes effect with the first message', {skip: !chromium, timeout: 120000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'image-one-turn-menu-draft', entries: ['virtual:image-one-turn-menu'], plugins: [plugin]});
  let browser;
  t.after(async () => {try {await browser?.close();} finally {await fixture.close();}});
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  const page = await browser.newPage({viewport: {width: 1280, height: 800}});
  await page.goto(`${fixture.base}/fixture?uuid=local:draft-1`);
  await page.getByRole('button', {name: '图片只看一轮：关闭'}).click();
  const panel = page.locator('.image-one-turn-popper .iot-panel');
  await panel.waitFor({state: 'visible'});
  assert.match(await panel.locator('.iot-note').innerText(), /发送第一条消息时生效/);
});
