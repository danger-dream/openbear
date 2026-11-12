import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from '../../browserTestServer.mjs';

const require = createRequire(import.meta.url);
let chromium;
try {({chromium} = require('../../testing/playwright.cjs'));} catch {}
const entry = `
import {createApp,h} from 'vue';
import ElementPlus from 'element-plus';
import 'element-plus/dist/index.css';
import 'element-plus/theme-chalk/dark/css-vars.css';
import '/src/style.css'; import '/src/dark-theme.css'; import '/src/mobile-overlays.css';
import ExtensionsMenu from '/src/views/consoleView/ExtensionsMenu.vue';
import {Api} from '/src/api.js';
import {extensionDraft} from '/src/views/consoleView/extensionDrafts.js';
window.__draft=()=>extensionDraft('local:draft-1');
const rows=[
 {key:'workitems',label:'任务面板',hint:'记录问题、决策、进展和验收依据',tokens:2100,enabled:true},
 {key:'agents',label:'Agent 协作',hint:'把工作交给独立的 Agent 并监督它们',tokens:5600,enabled:true},
 {key:'browser',label:'浏览器',hint:'操作远程浏览器和服务器节点的浏览器',tokens:900,enabled:false},
 {key:'mcp:parrot',label:'parrot',hint:'MCP 服务：'+'web_search, web_fetch, image_generate, image_edit, video_generate, video_status, '.repeat(3),tokens:1800,enabled:false},
];
const state={ok:true,stored:true,autoAllow:false,revision:1,extensions:rows};
window.__calls=[];
Api.conversationExtensions=async()=>JSON.parse(JSON.stringify(state));
Api.updateConversationExtensions=async(_id,body)=>{
 window.__calls.push(body);
 if((body.disable||[]).includes('agents')) throw {response:{status:409,data:{error:'extension_in_use',message:'Agent 协作正在使用：还有 1 个 Agent 在运行'}}};
 for(const row of rows){if((body.enable||[]).includes(row.key))row.enabled=true;if((body.disable||[]).includes(row.key))row.enabled=false;}
 if(typeof body.autoAllow==='boolean')state.autoAllow=body.autoAllow;
 state.revision+=1; return JSON.parse(JSON.stringify(state));
};
Api.folderExtensions=async()=>({default:{enabled:['workitems'],autoAllow:false},extensions:rows.map(r=>({...r,enabled:false}))});
const uuid=new URLSearchParams(location.search).get('uuid')||'conv-1';
const trigger=({label,open})=>h('button',{type:'button',class:'tool-btn','aria-label':label,'aria-expanded':String(open)},'ext');
createApp({render:()=>h('div',{class:'composer-toolbar',style:'position:fixed;left:8px;right:8px;bottom:8px;display:flex'},[h('div',{class:'composer-actions'},[h(ExtensionsMenu,{conversationUuid:uuid,folderId:'F1'},{trigger})])])}).use(ElementPlus).mount('#app');
`;
const html = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:extensions-menu"></script></body></html>';

test('the composer extension panel lists, switches and explains refusals on desktop and phone', {skip: !chromium, timeout: 180000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'extensions-menu', entries: ['virtual:extensions-menu'], plugins: [{
    name: 'extensions-menu',
    resolveId(id) {if (id === 'virtual:extensions-menu') return '\0virtual:extensions-menu';},
    load(id) {if (id === '\0virtual:extensions-menu') return entry;},
    configureServer(server) {server.middlewares.use((req, res, next) => {if (req.url?.startsWith('/fixture')) {res.setHeader('content-type', 'text/html'); res.end(html);} else next();});},
  }]});
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
    const button = page.getByRole('button', {name: '扩展（已开启 2 个）'});
    await button.waitFor();
    await button.click();
    const panel = page.locator(phone ? '.extensions-sheet .ext-panel' : '.extensions-menu-popper .ext-panel');
    await panel.waitFor({state: 'visible'});
    const rows = panel.locator('.ext-row');
    assert.equal(await rows.count(), 4);
    assert.match(await rows.nth(1).innerText(), /Agent 协作[\s\S]*≈5\.6K/);
    // Long hints stay inside the panel; nothing scrolls the page sideways.
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'no page-wide horizontal overflow');
    assert.ok(await panel.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'panel fits its width');
    for (const row of await rows.all()) assert.ok(await row.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'row fits');
    if (phone) for (const row of await rows.all()) assert.ok((await row.boundingBox()).height >= 44, 'rows are comfortable touch targets');
    // Turning one on updates the row and the badge.
    await rows.nth(2).locator('.el-switch').click();
    await page.getByRole('button', {name: '扩展（已开启 3 个）'}).waitFor({state: 'attached'});
    assert.deepEqual(await page.evaluate(() => window.__calls.at(-1)), {enable: ['browser']});
    // A refused change names the reason and keeps the switch on.
    await rows.nth(1).locator('.el-switch').click();
    await panel.locator('.ext-error').waitFor();
    assert.match(await panel.locator('.ext-error').innerText(), /还有 1 个 Agent 在运行/);
    assert.ok(await rows.nth(1).locator('.el-switch').evaluate(el => el.classList.contains('is-checked')));
    await panel.locator('.ext-auto .el-switch').click();
    await page.waitForFunction(() => window.__calls.at(-1)?.autoAllow === true);
    if (phone) {
      // A downward drag on the header follows the finger and closes the sheet.
      const head = await panel.locator('.ext-sheet-head').boundingBox();
      const x = head.x + head.width / 2 - 60, y = head.y + head.height / 2;
      await page.mouse.move(x, y); await page.mouse.down();
      for (let dy = 10; dy <= 160; dy += 15) await page.mouse.move(x, y + dy);
      await page.mouse.up();
      await page.locator('.extensions-sheet').waitFor({state: 'hidden'});
    }
    assert.deepEqual(errors, []);
  });
});

test('before its first message a new conversation starts from the folder default and keeps its choices locally', {skip: !chromium, timeout: 120000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'extensions-menu-draft', entries: ['virtual:extensions-menu'], plugins: [{
    name: 'extensions-menu',
    resolveId(id) {if (id === 'virtual:extensions-menu') return '\0virtual:extensions-menu';},
    load(id) {if (id === '\0virtual:extensions-menu') return entry;},
    configureServer(server) {server.middlewares.use((req, res, next) => {if (req.url?.startsWith('/fixture')) {res.setHeader('content-type', 'text/html'); res.end(html);} else next();});},
  }]});
  let browser;
  t.after(async () => {try {await browser?.close();} finally {await fixture.close();}});
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  const page = await browser.newPage({viewport: {width: 1280, height: 800}});
  await page.goto(`${fixture.base}/fixture?uuid=local:draft-1`);
  await page.getByRole('button', {name: '扩展（已开启 1 个）'}).click();
  const panel = page.locator('.extensions-menu-popper .ext-panel');
  await panel.waitFor({state: 'visible'});
  assert.match(await panel.locator('.ext-note').innerText(), /发送第一条消息时生效/);
  await panel.locator('.ext-row').nth(2).locator('.el-switch').click();
  await panel.locator('.ext-auto .el-switch').click();
  assert.deepEqual(await page.evaluate(() => window.__draft()), {enabled: ['workitems', 'browser'], autoAllow: true});
  assert.deepEqual(await page.evaluate(() => window.__calls), []);  // nothing is saved before the conversation exists
});
