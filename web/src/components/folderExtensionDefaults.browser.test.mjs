import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdirSync} from 'node:fs';
import {startBrowserTestServer} from '../browserTestServer.mjs';

const require = createRequire(import.meta.url);
let chromium;
try {({chromium} = require('../testing/playwright.cjs'));} catch {}
// The extensions tab as the folder properties dialog lays it out: the recent-list switch above the defaults.
const entry = `
import {createApp,h,ref} from 'vue';
import ElementPlus,{ElDialog,ElSwitch} from 'element-plus';
import 'element-plus/dist/index.css';
import 'element-plus/theme-chalk/dark/css-vars.css';
import '/src/style.css'; import '/src/dark-theme.css'; import '/src/mobile-overlays.css';
import '/src/components/conversationTreeProperties.css';
import FolderExtensionDefaults from '/src/components/FolderExtensionDefaults.vue';
const catalog=[
 {key:'workitems',label:'任务面板',hint:'记录问题、决策、进展和验收依据',tokens:760,requires:[]},
 {key:'agents',label:'Agent 协作',hint:'把工作交给独立的 Agent 并监督它们',tokens:5600,requires:[]},
 {key:'conversations',label:'会话协作',hint:'向其他会话发请求，组建和带领会话小组；会一并开启 Agent 协作，用它等待对方的结果',tokens:1900,requires:['agents']},
 {key:'browser',label:'浏览器',hint:'操作远程浏览器和服务器节点的浏览器',tokens:1200,requires:[]},
 {key:'mcp:parrot',label:'parrot',hint:'MCP 服务：'+'web_search, web_fetch, image_generate, image_edit, '.repeat(3),tokens:12500,requires:[]},
];
const top=new URLSearchParams(location.search).has('top');
const inherited={enabled:['workitems','agents'],autoAllow:true,explicit:true,from:'F0'};
const value=ref({inherit:!top,enabled:top?[]:[...inherited.enabled],autoAllow:false});
const hide=ref(false);
window.__value=()=>JSON.parse(JSON.stringify(value.value));
createApp({render:()=>h(ElDialog,{modelValue:true,title:'目录属性',width:'min(820px, calc(100vw - 24px))',class:'folder-properties-dialog',appendToBody:true},()=>
 h('div',{class:'property-tab-panel'},[
  h('section',{class:'property-section'},[h('label',{class:'property-switch'},[
   h('span',{class:'property-switch-text'},[h('span','不显示在最近会话'),h('small','此目录及其子目录的会话不显示在最近会话，运行和未读状态不受影响。')]),
   h(ElSwitch,{modelValue:hide.value,'onUpdate:modelValue':v=>hide.value=v,'aria-label':'不显示在最近会话'})])]),
  h(FolderExtensionDefaults,{modelValue:value.value,'onUpdate:modelValue':v=>value.value=v,catalog,inherited,inheritedFrom:'OpenBear',canInherit:!top}),
 ]))}).use(ElementPlus).mount('#app');
`;
const html = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:folder-extensions"></script></body></html>';
const shots = process.env.OPENBEAR_FOLDER_EXTENSION_SHOTS;

test('the folder extensions tab spaces its switches and shows each extension with its token estimate', {skip: !chromium, timeout: 180000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'folder-extensions', entries: ['virtual:folder-extensions'], plugins: [{
    name: 'folder-extensions',
    resolveId(id) {if (id === 'virtual:folder-extensions') return '\0virtual:folder-extensions';},
    load(id) {if (id === '\0virtual:folder-extensions') return entry;},
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
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`${fixture.base}/fixture`);
    await page.evaluate(dark => document.documentElement.classList.toggle('dark', dark), dark);
    const dialog = page.locator('.folder-properties-dialog');
    await dialog.getByText('新会话默认扩展').waitFor();
    const rows = dialog.locator('.folder-extension-row');
    const checked = () => rows.evaluateAll(els => els.filter(el => el.querySelector('.el-checkbox.is-checked')).map(el => el.querySelector('.folder-extension-text > span').textContent));
    // Inheriting keeps the list open and shows what comes from above, read-only.
    assert.match(await dialog.locator('.property-switch', {hasText: '继承上级目录'}).innerText(), /沿用「OpenBear」的设置/);
    assert.match(await dialog.locator('.folder-extension-list-head').innerText(), /继承「OpenBear」，不能单独修改/);
    assert.equal(await rows.count(), 5);
    assert.deepEqual(await checked(), ['任务面板', 'Agent 协作']);
    assert.equal(await dialog.locator('.folder-extension-row .el-checkbox.is-disabled').count(), 5);
    assert.equal(await dialog.locator('.folder-extension-auto .el-switch').evaluate(el => el.classList.contains('is-checked') && el.classList.contains('is-disabled')), true);
    await rows.filter({hasText: '浏览器'}).click({force: true});
    assert.deepEqual(await checked(), ['任务面板', 'Agent 协作'], 'a click while inheriting changes nothing');
    assert.deepEqual(await dialog.locator('.folder-extension-tokens').allInnerTexts(), ['≈760', '≈5.6K', '≈1.9K', '≈1.2K', '≈12.5K']);
    assert.match(await dialog.locator('.folder-extension-total').innerText(), /已选 2 项\s+每次请求约多 6.4K token/);
    if (shots) {
      await page.evaluate(() => Promise.all(document.getAnimations().map(a => a.finished.catch(() => {}))));
      await dialog.screenshot({path: `${shots}/${phone ? 'phone' : 'desktop'}-${dark ? 'dark' : 'light'}-inherited.png`});
    }

    await dialog.locator('.property-switch', {hasText: '继承上级目录'}).locator('.el-switch').click();
    assert.match(await dialog.locator('.folder-extension-list-head').innerText(), /勾选的扩展会出现在新会话的工具列表里/);
    assert.equal(await dialog.locator('.folder-extension-row .el-checkbox.is-disabled').count(), 0);
    await rows.filter({hasText: '会话协作'}).locator('.el-checkbox').click();
    assert.deepEqual((await page.evaluate(() => window.__value())).enabled, ['workitems', 'agents', 'conversations']);
    assert.match(await dialog.locator('.folder-extension-total').innerText(), /已选 3 项\s+每次请求约多 8.3K token/);

    // Every switch sits apart from its text; nothing in the dialog scrolls sideways.
    const layout = await page.evaluate(() => [...document.querySelectorAll('.folder-properties-dialog .property-switch')].map(row => {
      const text = row.querySelector('.property-switch-text').getBoundingClientRect(), sw = row.querySelector('.el-switch').getBoundingClientRect();
      return {title: row.querySelector('.property-switch-text > span').textContent, gap: sw.left - text.right, overlapY: sw.top < text.bottom && sw.bottom > text.top};
    }));
    assert.deepEqual(layout.map(r => r.title), ['不显示在最近会话', '继承上级目录', '自动允许模型开启扩展']);
    for (const row of layout) assert.ok(row.gap >= 16 && row.overlapY, `${row.title}: switch ${row.gap}px from its text`);
    const overflow = await page.evaluate(() => [...document.querySelectorAll('.folder-properties-dialog *')].filter(el => el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).overflowX !== 'visible').map(el => el.className));
    assert.deepEqual(overflow, []);
    const hintLines = await page.evaluate(() => { const el = [...document.querySelectorAll('.folder-extension-text small')].at(-1); return Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight)); });
    assert.ok(hintLines <= 2, `a long description is cut to two lines, got ${hintLines}`);
    const tokenRight = await page.evaluate(() => [...document.querySelectorAll('.folder-extension-row')].map(row => Math.round(row.getBoundingClientRect().right - row.querySelector('.folder-extension-tokens').getBoundingClientRect().right)));
    assert.ok(new Set(tokenRight).size === 1, `token estimates line up on the right: ${tokenRight}`);
    if (shots) {
      await page.evaluate(() => Promise.all(document.getAnimations().map(a => a.finished.catch(() => {}))));
      await dialog.screenshot({path: `${shots}/${phone ? 'phone' : 'desktop'}-${dark ? 'dark' : 'light'}.png`});
    }
    assert.deepEqual(errors, []);
  });
  await t.test('a top-level folder has no inherit switch and edits its own setting', {timeout: 45000}, async t => {
    const context = await browser.newContext({viewport: {width: 1440, height: 900}});
    t.after(() => context.close());
    const page = await context.newPage();
    await page.goto(`${fixture.base}/fixture?top=1`);
    const dialog = page.locator('.folder-properties-dialog');
    await dialog.getByText('扩展工具', {exact: true}).waitFor();
    assert.equal(await dialog.getByText('继承上级目录').count(), 0);
    assert.deepEqual((await dialog.locator('.property-switch .property-switch-text > span').allInnerTexts()), ['不显示在最近会话', '自动允许模型开启扩展']);
    await dialog.locator('.folder-extension-row', {hasText: '浏览器'}).locator('.el-checkbox').click();
    assert.deepEqual(await page.evaluate(() => window.__value()), {inherit: false, enabled: ['browser'], autoAllow: false});
    if (shots) await dialog.screenshot({path: `${shots}/desktop-light-top.png`});
  });
});
