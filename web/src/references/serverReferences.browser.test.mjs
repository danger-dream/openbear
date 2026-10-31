import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdirSync} from 'node:fs';
import {startBrowserTestServer} from '../browserTestServer.mjs';
const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}
const shots = process.env.OPENBEAR_SCREENSHOT_DIR || '';

// The real composer, picker, inspector and transcript renderer; only /api is a fixture.
const OLD = '检查 [旧机器](openbear://ref/server/deleted-9) 和 [香港一号](openbear://ref/server/hk-1) 的磁盘';
const entry = `
import '/src/monaco-worker.js';
import '/src/theme.js';
import {createApp, h, ref} from 'vue';
import ElementPlus from 'element-plus';
import 'element-plus/dist/index.css';
import 'element-plus/theme-chalk/dark/css-vars.css';
import '/src/style.css';
import '/src/dark-theme.css';
import '/src/admin-mobile.css';
import '/src/mobile-overlays.css';
import '/src/mobile-inputs.css';
import ConsoleComposer from '/src/views/consoleView/ConsoleComposer.vue';
import ConsoleMarkdown from '/src/views/consoleView/ConsoleMarkdown.vue';
import ReferenceInspector from '/src/references/ReferenceInspector.vue';
import {applyCatalogPacket} from '/src/references/catalog.js';
import {documentToText} from '/src/references/codec.js';
applyCatalogPacket({type:'snapshot',epoch:'fixture',seq:1,includeArchived:false,conversationContentLimit:20000,items:[
 {key:'doc:7',kind:'doc',id:'7',name:'deploy',label:'部署手册',group:'运维',preview:'安装与升级步骤'},
 {key:'mem:3',kind:'mem',id:'3',name:'ops.ports',label:'常用端口',group:'运维',preview:'SSH 22，面板 8090'}]});
const draft = ref('');
window.__draft = () => draft.value;
createApp({setup(){ return () => h('main', {class:'fixture-chat'}, [
  h('section', {class:'fixture-history'}, [h('div', {class:'fixture-user'}, [h(ConsoleMarkdown, {text:${JSON.stringify(OLD)}})])]),
  h('div', {class:'fixture-composer'}, [h(ConsoleComposer, {draft:draft.value, 'onUpdate:draft':v=>draft.value=v, conversationUuid:'conv-1', canSend:true, currentModel:'openai/gpt'})]),
  h(ReferenceInspector, {currentConversation:'conv-1'}),
]); }}).use(ElementPlus).mount('#app');
`;
const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"><style>html,body,#app{height:100%;margin:0}.fixture-chat{display:flex;flex-direction:column;height:100%;background:var(--ob-surface)}.fixture-history{flex:1;overflow:auto;padding:16px;display:flex;flex-direction:column;align-items:flex-end}.fixture-user{max-width:min(560px,86%);padding:8px 12px;border-radius:12px;background:var(--ob-surface-soft);font-size:14px}.fixture-composer{max-width:820px;width:100%;margin:0 auto;box-sizing:border-box;padding:0 8px 8px}</style></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:server-references-test"></script></body></html>';

const ROWS = [
 {id:'local',label:'本机',group:'',region:'CN',platform:'linux',os:'Ubuntu 24.04',ip:'203.0.113.10',meshAddress:'100.96.0.1',local:true,online:true,nodeInstalled:true,canExec:true,sshLogin:false},
 {id:'hk-1',label:'香港一号',group:'生产',region:'HK',platform:'linux',os:'Debian 12',ip:'198.51.100.7',meshAddress:'100.96.0.3',local:false,online:true,nodeInstalled:true,canExec:true,sshLogin:true},
 {id:'hk-2',label:'香港二号（备用节点，名称很长用于验证换行与省略）',group:'生产',region:'HK',platform:'linux',os:'Debian 12',ip:'198.51.100.8',meshAddress:'',local:false,online:false,nodeInstalled:true,canExec:true,sshLogin:true},
 {id:'tokyo-2',label:'Tokyo Edge',group:'边缘',region:'JP',platform:'windows',os:'Windows Server 2022',ip:'192.0.2.9',meshAddress:'100.96.0.9',local:false,online:true,nodeInstalled:true,canExec:false,sshLogin:false},
 {id:'sg-1',label:'新加坡',group:'',region:'SG',platform:'',os:'',ip:'',meshAddress:'',local:false,online:false,nodeInstalled:false,canExec:false,sshLogin:false},
].map(r => ({...r, key:`server:${r.id}`, kind:'server', name:r.id, searchText:[r.group,r.region,r.ip,r.meshAddress,r.platform,r.os,r.local?'本机 local':''].filter(Boolean).join(' ')}));
const FACTS = {serverId:'hk-1',name:'香港一号',group:'生产',region:'HK',platform:'linux',os:'Debian 12',arch:'amd64',node:{installed:true,online:true,exec:true,version:'1.4.0'},sshLogin:true,sshStatus:'ok',canExec:true,execVia:'node',publicIp:'198.51.100.7',meshAddress:'100.96.0.3',exists:true,observedAt:'2026-10-28 19:40:00 +0800',usage:'可用 Servers 工具按 serverId 直接操作（exec、list_dir、read_file、write_file、job_start 等）；节点离线但有 SSH 登录时，按 serverId 执行的 exec 会改走 SSH。'};

function refsIn(text) { return [...String(text).matchAll(/\]\(openbear:\/\/ref\/server\/([^)?\s]+)/g)].map(m => m[1]); }

// Kept in the browser: typing '@' into the real TipTap editor, tapping picker rows and inserting
// capsules that become the draft text, and opening the inspector from a capsule, are real input
// in a contenteditable. The same rules as plain logic (search by group/IP, row details,
// tap-to-check, inserted tokens, deleted marks) already run without a browser in
// serverReferences.test.mjs. The three setups run side by side, one page each.
test('@ server references: search, multi-select, capsules, inspector and deleted marks on desktop and phone', {skip:!chromium, timeout:240000, concurrency:3}, async t => {
  if (shots) mkdirSync(shots, {recursive:true});
  const fixture = await startBrowserTestServer(t, {name:'server-references', entries:['virtual:server-references-test'], plugins:[{
    name:'server-references-test',
    resolveId(id){ if (id === 'virtual:server-references-test') return '\0virtual:server-references-test'; },
    load(id){ if (id === '\0virtual:server-references-test') return entry; },
    configureServer(server){ server.middlewares.use((req, res, next) => { if ((req.url || '').split('?')[0] === '/chat') { res.setHeader('content-type', 'text/html'); res.end(html); } else next(); }); },
  }], warmupTimeoutMs:120000});
  let browser;
  t.after(async () => { try { await browser?.close(); } finally { await fixture.close(); } });
  browser = await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless:true, args:['--no-sandbox']});

  async function open(t, {width, height=844, dark=false}) {
    const phone = width < 761;
    const context = await browser.newContext({viewport:{width, height}, hasTouch:phone, isMobile:phone, deviceScaleFactor:phone?2:1});
    t.after(() => context.close());
    const page = await context.newPage(), errors = [], calls = [];
    page.setDefaultTimeout(15000);
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(dark => { localStorage.setItem('openbear.notifications.prompt.v1', 'dismissed'); localStorage.setItem('openbear.theme.v1', dark ? 'dark' : 'light'); }, dark);
    await page.route('**/api/**', async route => {
      const request = route.request(), p = new URL(request.url()).pathname;
      calls.push(p);
      let data = {ok:true, items:[]};
      if (p === '/api/references/servers') data = {ok:true, enabled:true, items:ROWS};
      else if (p === '/api/references/preview') {
        const ids = refsIn(request.postDataJSON().text);
        data = {ok:true, items:ids.map(id => ({kind:'server', id, key:`server:${id}::full:`, exists:ROWS.some(r => r.id === id)})), bindings:[], estimatedTokens:200, bodyChars:0, conversationContentLimit:20000};
      } else if (p === '/api/references/inspect') {
        const id = refsIn(request.postDataJSON().text)[0];
        data = {ok:true, frozen:false, previewTruncated:false, conversationContentLimit:20000,
          item:{kind:'server', id, label:'香港一号', scope:'full', key:`server:${id}::full:`, sourceLabel:'香港一号', exists:true, sensitive:false, estimatedTokens:180},
          content:JSON.stringify(FACTS)};
      }
      await route.fulfill({status:200, contentType:'application/json', body:JSON.stringify(data)});
    });
    await page.goto(`${fixture.base}/chat`);
    await page.locator('.reference-editor-content').waitFor();
    return {page, errors, calls, phone};
  }
  const shot = async (page, name) => { if (shots) await page.screenshot({path:`${shots}/${name}.png`}); };
  const panel = page => page.locator('.reference-floating-panel[aria-label="选择引用内容"]');

  await Promise.all([{width:1280, height:860, dark:false, tag:'desktop'}, {width:390, dark:false, tag:'phone'}, {width:390, dark:true, tag:'phone-dark'}].map(({width, dark, tag}) => t.test(tag, {timeout:60000}, async t => {
    const {page, errors, calls, phone} = await open(t, {width, dark});
    const editor = page.locator('.reference-editor-content');
    if (dark) {
      // Dark changes only colours: check the phone layout here; the steps below run in phone and desktop.
      await editor.tap();
      await page.keyboard.type('@');
      await panel(page).waitFor();
      const box = await panel(page).boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width + 0.5, 'the picker never overflows the viewport horizontally');
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'no sideways scroll with the picker open');
      await page.locator('.reference-picker-tabs button', {hasText:'服务器'}).tap();
      await panel(page).locator('.reference-picker-option').nth(4).waitFor();
      for (const row of await panel(page).locator('.reference-picker-option').all()) assert.ok((await row.boundingBox()).height >= 44, 'rows keep a phone-sized touch target');
      await shot(page, `${tag}-picker`);
      assert.deepEqual(errors, []);
      return;
    }
    // The transcript marks a deleted server once the directory is known; the existing one stays normal.
    await page.locator('.fixture-user .reference-chip.is-missing').waitFor();
    assert.equal(await page.locator('.fixture-user .reference-chip.is-missing').count(), 1);
    assert.match(await page.locator('.fixture-user .reference-chip.is-missing').innerText(), /旧机器\s*已不存在/);
    assert.equal(await page.locator('.fixture-user .reference-chip--server:not(.is-missing)').count(), 1);

    // 1. @ opens the picker; 全部 already contains servers next to other resources.
    if (phone) await editor.tap(); else await editor.click();
    await page.keyboard.type('帮我在这几台装 htop：@');
    await panel(page).waitFor();
    await page.locator('.reference-picker-tabs button', {hasText:'服务器'}).waitFor();
    assert.ok(await panel(page).locator('.reference-picker-option', {hasText:'香港一号'}).count() >= 1);
    const box = await panel(page).boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= width + 0.5, 'the picker never overflows the viewport horizontally');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'no sideways scroll with the picker open');
    await shot(page, `${tag}-1-picker-all`);

    // 2. Search by group narrows to servers of that group.
    await page.keyboard.type('生产');
    await panel(page).locator('.reference-picker-option', {hasText:'香港二号'}).waitFor();
    // Other kinds may still fuzzy-match; the servers shown are exactly the 生产 group.
    const serverRows = (await panel(page).locator('.reference-picker-option').allInnerTexts()).filter(text => /198\.51\.100|203\.0\.113|192\.0\.2|离线|在线/.test(text));
    assert.equal(serverRows.length, 2);
    assert.ok(serverRows.every(text => text.includes('生产')));
    await shot(page, `${tag}-2-search-group`);
    for (let i = 0; i < 2; i++) await page.keyboard.press("Backspace");

    // 3. 服务器 tab: tap rows to check several, state/platform/group/IP on each row.
    const tab = page.locator('.reference-picker-tabs button', {hasText:'服务器'});
    if (phone) await tab.tap(); else await tab.click();
    await panel(page).locator('.reference-picker-option').nth(4).waitFor();
    assert.equal(await panel(page).locator('.reference-picker-option').count(), 5);
    assert.equal(await panel(page).locator('.reference-picker-status.is-online').count(), 3);
    assert.match(await panel(page).locator('.reference-picker-option').nth(2).innerText(), /离线 · 可走 SSH · linux · 生产 · 198\.51\.100\.8/);
    if (phone) for (const row of await panel(page).locator('.reference-picker-option').all()) assert.ok((await row.boundingBox()).height >= 44, 'rows keep a phone-sized touch target');
    for (const label of ['香港一号', 'Tokyo Edge']) {
      const row = panel(page).locator('.reference-picker-option', {hasText:label});
      if (phone) await row.tap(); else await row.click();
    }
    await panel(page).locator('.reference-picker-selection-count', {hasText:'已选 2 台'}).waitFor();
    assert.equal(await panel(page).locator('.reference-picker-option.is-checked').count(), 2);
    await shot(page, `${tag}-3-server-tab-multi`);

    // 4. Insert: one capsule per server, the editor keeps the prose, the draft has stable ids.
    const insert = panel(page).locator('.reference-picker-insert');
    if (phone) await insert.tap(); else await insert.click();
    await panel(page).waitFor({state:'detached'});
    assert.equal(await page.locator('.reference-editor .reference-chip--server').count(), 2);
    const draft = await page.evaluate(() => window.__draft());
    assert.deepEqual(refsIn(draft), ['hk-1', 'tokyo-2']);
    assert.match(draft, /^帮我在这几台装 htop：\[香港一号\]\(openbear:\/\/ref\/server\/hk-1\) \[Tokyo Edge\]\(openbear:\/\/ref\/server\/tokyo-2\) $/);
    await shot(page, `${tag}-4-capsules`);

    // 5. A capsule opens the inspector with the facts the model will get.
    const chip = page.locator('.reference-editor .reference-chip--server').first();
    if (phone) await chip.tap(); else await chip.click();
    await page.locator('.reference-inspector-content').waitFor();
    assert.match(await page.locator('.reference-inspector-content').innerText(), /"serverId": "hk-1"[\s\S]*"execVia": "node"[\s\S]*Servers 工具/);
    await shot(page, `${tag}-5-inspector`);
    await page.keyboard.press('Escape');

    assert.ok(calls.includes('/api/references/servers'));
    assert.deepEqual(errors, []);
  })));
});
