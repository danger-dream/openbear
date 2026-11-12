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
import * as Icons from '@element-plus/icons-vue';
import 'element-plus/dist/index.css';
import 'element-plus/theme-chalk/dark/css-vars.css';
import '/src/style.css'; import '/src/dark-theme.css';
import AgentEventCard from '/src/views/consoleView/AgentEventCard.vue';
import {Api} from '/src/api.js';
const count=Number(new URLSearchParams(location.search).get('count')||1);
const tasks=Array.from({length:count},(_,i)=>({
 taskUuid:'task-'+(i+1),agentId:'agent-fixture',sessionKind:'independent',sessionTurn:i+1,
 title:i===0?'长标题'.repeat(45)+'末尾':'第 '+(i+1)+' 条实例指派',status:'completed',
 grantedTools:['Bash','Read','Write','mcp__parrot__web_search'],
 contextSource:{state:'restored',taskUuid:'task-previous',revision:i+1},planMode:'direct',
}));
const session={agentId:'agent-fixture',sessionKind:'independent',status:'active',lastTaskUuid:tasks.at(-1).taskUuid,turnCount:count,canContinue:true,contextRevision:count};
Api.rathAgentInstance=async()=>({ok:true,legacy:false,agentSession:session,tasks});
Api.rathTaskEvents=async()=>({ok:true,events:[],total:0,hasMore:false});
Api.rathTaskPlan=async()=>({ok:true,state:{phase:'direct'},versions:[],steps:[]});
const event={kind:'tool',calls:[{name:'Agent',arguments:'{}'}],result:{},livePayload:{task:tasks.at(-1),agentSession:session}};
const props={event,conversationUuid:'test-conversation',turnId:'test-turn',index:0,detailKey:(...v)=>v.join(':'),isDetailOpen:()=>true,onDetailsToggle:()=>{}};
const app=createApp({render:()=>h('main',{style:'max-width:900px;margin:0 auto;padding:8px'},[h(AgentEventCard,props)])});
app.use(ElementPlus); for(const [name,component] of Object.entries(Icons))app.component(name,component); app.mount('#app');
`;
const html = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:agent-instance-layout"></script></body></html>';

test('instance assignment cards keep their natural height and remain scrollable', {skip: !chromium, timeout: 180000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'agent-instance-layout', entries: ['virtual:agent-instance-layout'], plugins: [{
    name: 'agent-instance-layout',
    resolveId(id) {if (id === 'virtual:agent-instance-layout') return '\0virtual:agent-instance-layout';},
    load(id) {if (id === '\0virtual:agent-instance-layout') return entry;},
    configureServer(server) {server.middlewares.use((req, res, next) => {if (req.url?.startsWith('/fixture')) {res.setHeader('content-type', 'text/html'); res.end(html);} else next();});},
  }]});
  let browser;
  t.after(async () => {try {await browser?.close();} finally {await fixture.close();}});
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  for (const width of [1440, 320]) for (const count of [1, 27]) await t.test(`${width}px, ${count} assignments`, {timeout: 45000}, async t => {
    const phone = width < 761;
    const context = await browser.newContext({viewport: {width, height: 844}, hasTouch: phone, isMobile: phone});
    t.after(() => context.close());
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`${fixture.base}/fixture?count=${count}`);
    if (phone) await page.getByLabel('Agent 详情分类').selectOption('instance');
    else await page.getByRole('tab', {name: /实例指派/}).click();
    const list = page.locator('.instance-assignment-list'), cards = list.locator('.instance-assignment');
    await page.waitForFunction(expected => document.querySelectorAll('.instance-assignment').length === expected, count);
    assert.equal(await cards.count(), count);
    for (const dark of [false, true]) {
      await page.evaluate(dark => document.documentElement.classList.toggle('dark', dark), dark);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'no page-wide horizontal overflow');
      assert.ok(await list.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'list fits its available width');
      for (const card of await cards.all()) {
        assert.ok(await card.evaluate(el => el.scrollHeight <= el.clientHeight + 1), 'assignment content is not clipped');
        assert.ok(await card.locator('header strong').evaluate(el => el.scrollWidth <= el.clientWidth + 1 && el.scrollHeight <= el.clientHeight + 1), 'long title wraps fully');
      }
    }
    assert.match(await cards.first().innerText(), /末尾/);
    assert.match(await cards.first().innerText(), /任务.*来源.*上下文/s);
    assert.match(await cards.first().innerText(), /本轮能力.*Bash.*Read.*Write/s);
    if (count > 1) {
      const scroll = phone ? page.locator('.agent-tab-panel') : list;
      assert.ok(await scroll.evaluate(el => el.scrollHeight > el.clientHeight), 'many assignments have a scrollable reading surface');
      await scroll.evaluate(el => {el.scrollTop = el.scrollHeight;});
      const last = await cards.last().boundingBox(), surface = await scroll.boundingBox();
      assert.ok(last && surface && last.y + last.height <= surface.y + surface.height + 1, 'last assignment is reachable');
    }
    assert.deepEqual(errors, []);
  });
});
