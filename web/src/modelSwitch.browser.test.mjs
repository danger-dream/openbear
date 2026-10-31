import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from './browserTestServer.mjs';
import {createModelSwitchBackend} from './modelSwitchFixture.mjs';
const require=createRequire(import.meta.url);
let chromium;
try {({chromium}=require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core'));} catch {}
const entry=`import '/src/monaco-worker.js'; import '/src/theme.js'; import {createApp,h} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/mobile-inputs.css'; import ConsoleView from '/src/views/consoleView/ConsoleView.vue'; window.createdConversations=[]; const app=createApp({render:()=>h(ConsoleView,{conversationUuid:'original',onConversationCreated:id=>window.createdConversations.push(id)})}); app.use(ElementPlus); for(const [name,component] of Object.entries(Icons))app.component(name,component);app.mount('#app');`;
const html='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><script>window.__OPENBEAR_AUTH_MODE__="controller"</script></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:model-switch-test"></script></body></html>';

// Layout smoke of the run-config model menu in Chromium: on desktop, a phone and the narrowest
// dark phone the menu opens inside the screen, its model entry is visible and takes the tap, and
// nothing scrolls sideways. What a switch sends and reports in every scenario: modelSwitch.dom.test.mjs.
test('model menu fits desktop, phone and dark phone', {skip:!chromium,timeout:180000}, async t=>{
  const fixture=await startBrowserTestServer(t,{name:'model-switch',entries:['virtual:model-switch-test'],plugins:[{
    name:'model-switch-test',resolveId(id){if(id==='virtual:model-switch-test')return '\0virtual:model-switch-test';},load(id){if(id==='\0virtual:model-switch-test')return entry;},
    configureServer(server){server.middlewares.use((req,res,next)=>{if(req.url?.startsWith('/__model-test')){res.setHeader('content-type','text/html');res.end(html);}else next();});},
  }]});
  let browser;
  t.after(async()=>{try{await browser?.close();}finally{await fixture.close();}});
  browser=await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  for(const width of [1440,390,320]) await t.test(`${width}px${width===320?' dark':''}`,{timeout:60000},async t=>{
    const context=await browser.newContext({viewport:{width,height:900},hasTouch:width<761,isMobile:width<761,colorScheme:width===320?'dark':'light'});
    t.after(()=>context.close());
    const page=await context.newPage(),errors=[],backend=createModelSwitchBackend('idle');
    page.on('pageerror',error=>errors.push(error.message));
    page.setDefaultTimeout(10000);page.setDefaultNavigationTimeout(30000);
    await page.route('**/api/**',async route=>{
      const req=route.request();
      const {status,json}=backend.handle({path:new URL(req.url()).pathname,method:req.method(),body:req.postDataJSON()});
      await route.fulfill({status,contentType:'application/json',body:JSON.stringify(json)});
    });
    await page.routeWebSocket('**/api/**',backend.socket);
    await page.goto(fixture.base+'/__model-test');
    await page.getByText('原会话已经完成的结果',{exact:true}).waitFor({timeout:30000});
    if(width===320)await page.evaluate(()=>document.documentElement.classList.add('dark'));
    await page.getByRole('button',{name:'运行配置',exact:true}).click();
    const option=page.getByRole('button',{name:'claude（target/claude）',exact:true});
    await option.waitFor();
    await page.waitForFunction(()=>document.getAnimations().every(a=>a.playState!=='running'));
    const box=await option.boundingBox();
    assert.ok(box.x>=0&&box.x+box.width<=width+1&&box.y>=0&&box.y+box.height<=901,`model entry inside the screen: ${JSON.stringify(box)}`);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'no sideways scroll with the menu open');
    await option.click();
    await page.waitForFunction(()=>document.querySelector('.run-config-chip-model')?.textContent.includes('claude'));
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'no sideways scroll after switching');
    assert.deepEqual(errors,[]);
  });
});
