import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from './browserTestServer.mjs';
import {createToolFileBackend, uuid} from './toolFileOpenFixture.mjs';

const require = createRequire(import.meta.url);
let chromium;
try {({chromium} = require('./testing/playwright.cjs'));} catch {}
const entry = `import '/src/monaco-worker.js'; import '/src/theme.js'; import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css'; import App from '/src/App.vue'; const app=createApp(App); app.use(ElementPlus); for(const [name,component] of Object.entries(Icons))app.component(name,component);app.mount('#app');`;
const html = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><script>window.__OPENBEAR_AUTH_MODE__="controller"</script></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:conversation-time-test"></script></body></html>';
const agentEntry = `import {createApp,h} from 'vue'; import '/src/style.css'; import '/src/dark-theme.css'; import AgentActivityList from '/src/views/consoleView/AgentActivityList.vue'; const yesterday=new Date(); yesterday.setDate(yesterday.getDate()-1); yesterday.setHours(10,15,20,0); const lines=[{key:'old-a',kind:'tool_call_started',ts:yesterday.getTime()/1000,toolName:'Read',detail:{name:'Read',arguments:'{\\"path\\":\\"/tmp/example\\"}',resultText:'实际结果'}},{key:'old-b',kind:'event',ts:yesterday.getTime()/1000+1,message:'已继续执行'}]; createApp({render:()=>h('main',{style:'padding:16px'},[h(AgentActivityList,{lines})])}).mount('#app');`;
const agentHtml = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:agent-time-test"></script></body></html>';

function datedOperations(operations, timestamp, suffix, offset) {
  const turn = `turn-${suffix}`;
  return operations.map(op => ({...op, opId: `${op.opId}-${suffix}`, turnId: turn, turnUuid: turn,
    runRootTurnId: turn, targetId: turn, runId: turn, displaySeq: op.displaySeq + offset,
    createdAtMs: timestamp + op.displaySeq * 1000, updatedAtMs: timestamp + op.displaySeq * 1000,
    terminalAtMs: timestamp + op.displaySeq * 1000,
    payload: {...op.payload, runId: turn, turnId: turn},
  }));
}

test('conversation dates fit the actual desktop and phone layouts', {skip: !chromium, timeout: 180000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'conversation-time', entries: ['virtual:conversation-time-test', 'virtual:agent-time-test'], plugins: [{
    name: 'conversation-time-test',
    resolveId(id) {if (['virtual:conversation-time-test', 'virtual:agent-time-test'].includes(id)) return `\0${id}`;},
    load(id) {if (id === '\0virtual:conversation-time-test') return entry; if (id === '\0virtual:agent-time-test') return agentEntry;},
    configureServer(server) {server.middlewares.use((req, res, next) => {if (req.url?.startsWith('/chat') || req.url?.startsWith('/agent')) {res.setHeader('content-type', 'text/html'); res.end(req.url.startsWith('/agent') ? agentHtml : html);} else next();});},
  }]});
  let browser;
  t.after(async () => {try {await browser?.close();} finally {await fixture.close();}});
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  for (const width of [1440, 320]) await t.test(`${width}px`, {timeout: 60000}, async t => {
    const phone = width < 761;
    const context = await browser.newContext({viewport: {width, height: 844}, hasTouch: phone, isMobile: phone});
    t.after(() => context.close());
    const page = await context.newPage(), errors = [], backend = createToolFileBackend();
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {localStorage.setItem('openbear.notifications.prompt.v1', 'dismissed');});
    // The page's clock held at noon of the day the times are of: a run across midnight sees the same days.
    const noon = new Date(); noon.setHours(12, 0, 0, 0);
    await page.clock.setFixedTime(noon);
    const yesterday = new Date(noon); yesterday.setDate(yesterday.getDate() - 1); yesterday.setHours(10, 15, 0, 0);
    const today = new Date(noon); today.setHours(10, 15, 0, 0);
    const oldMs = yesterday.getTime(), todayMs = today.getTime();
    const date = `${yesterday.getFullYear()}-${String(yesterday.getMonth()+1).padStart(2,'0')}-${String(yesterday.getDate()).padStart(2,'0')}`;
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const response = backend.handle({path: url.pathname, query: Object.fromEntries(url.searchParams)});
      if (url.pathname === `/api/conversations/${uuid}/state`) {
        response.json.operations = [...datedOperations(response.json.operations, oldMs, 'old', 0), ...datedOperations(response.json.operations, todayMs, 'today', 100)];
      }
      if (response.json !== undefined) return route.fulfill({status: response.status, contentType: 'application/json', body: JSON.stringify(response.json)});
      return route.fulfill({status: response.status, headers: response.headers, body: response.body});
    });
    await page.routeWebSocket('**/api/**', ws => {
      const url = new URL(ws.url());
      if (url.pathname === '/api/events/ws') ws.send(JSON.stringify({type: 'snapshot', epoch: 'test', seq: 1, items: [], includeArchived: false, treeStatus: backend.handle({path: '/api/conversation-tree/status'}).json}));
      ws.onMessage(message => {
        if (message === 'ping') return ws.send('pong');
        let packet; try {packet = JSON.parse(message);} catch {return;}
        if (packet.type === 'conversation-overview' && packet.conversationUuid) ws.send(JSON.stringify({type: 'conversation-overview', conversationUuid: packet.conversationUuid, subscriptionId: packet.subscriptionId,
          overview: {conversationUuid: uuid, title: '工具读写文件', status: '就绪', running: false, lastMessageAtMs: oldMs, usage: {}, duration: {}, calls: {}, messageCount: 2}}));
      });
    });
    await page.goto(`${fixture.base}/chat?id=${uuid}`);
    await page.locator('.user-message-time').first().waitFor({state: 'visible', timeout: 30000});
    const userTimes = page.locator('.user-message-time');
    await page.waitForFunction(() => document.querySelectorAll('.user-message-time').length === 2);
    assert.match(await userTimes.nth(0).innerText(), new RegExp(date));
    assert.match(await userTimes.nth(1).innerText(), /^\d{2}:\d{2}:\d{2}$/);
    const replies = page.locator('.assistant-message-time');
    assert.match(await replies.nth(0).innerText(), new RegExp(date));
    assert.doesNotMatch(await replies.nth(1).innerText(), /\d{4}-\d{2}-\d{2}/);
    for (const dark of [false, true]) {
      await page.evaluate(dark => document.documentElement.classList.toggle('dark', dark), dark);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'page does not scroll sideways');
      for (const selector of ['.user-message-meta', '.assistant-message-meta']) {
        const rows = await page.locator(selector).all();
        for (const row of rows) assert.ok(await row.evaluate(el => el.scrollWidth <= el.clientWidth + 1), `${selector} fits`);
      }
    }
    if (!phone) {
      await page.getByRole('button', {name: /已工作/}).first().click();
      const badge = page.locator('.time-float-dated').first();
      await badge.locator('..').hover();
      const badgeBox = await badge.boundingBox();
      assert.ok(badgeBox && badgeBox.x >= 0 && badgeBox.x + badgeBox.width <= width, 'dated process badge stays in the viewport');
      await page.waitForFunction(() => Number(getComputedStyle(document.querySelector('.time-float-dated')).opacity) > 0.95);
      await page.locator(`[data-tree-id="recent:${uuid}"]`).hover();
      const overview = page.locator('.conversation-overview');
      await overview.waitFor({state: 'visible'});
      await overview.locator('.overview-last-message time').waitFor({state: 'visible'});
      assert.match(await overview.locator('.overview-last-message').innerText(), new RegExp(date));
      assert.match(await overview.innerText(), /累计耗时/);
      assert.ok(await overview.evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    }
    await page.goto(`${fixture.base}/agent`);
    const dateHeading = page.locator('.activity-date');
    await dateHeading.waitFor({state: 'visible'});
    assert.equal(await dateHeading.count(), 1);
    assert.equal((await dateHeading.innerText()).trim(), date);
    await page.locator('.activity-tool-call summary').click();
    assert.match(await page.locator('.activity-tool-arguments').innerText(), /实际结果/);
    for (const dark of [false, true]) {
      await page.evaluate(dark => document.documentElement.classList.toggle('dark', dark), dark);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'expanded Agent activity fits');
      assert.ok(await page.locator('.activity-list').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    }
    assert.deepEqual(errors, []);
  });
});
