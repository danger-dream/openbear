import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from './browserTestServer.mjs';
import {createSelectionBackend} from './conversationSelectionFixture.mjs';
const require=createRequire(import.meta.url);
let chromium;
try {({chromium}=require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core'));} catch {}
const shots=process.env.OPENBEAR_SCREENSHOT_DIR || '';
const entry=`import '/src/monaco-worker.js'; import '/src/theme.js'; import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css'; import App from '/src/App.vue'; const app=createApp(App); app.use(ElementPlus); for(const [name,component] of Object.entries(Icons))app.component(name,component);app.mount('#app');`;
const html='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><script>window.__OPENBEAR_AUTH_MODE__="controller"</script></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:selection-test"></script></body></html>';

// Layout smoke of tree multi-select on the real App in Chromium: with 59 of 61 rows selected the
// selection bar stays inside the viewport, the tree does not scroll sideways and no bar button
// clips its label, in light and dark, on desktop and the narrowest phone. Taps, the folder-wide
// pick across pages, unticking and the batch delete: conversationSelection.dom.test.mjs.
test('tree multi-select bar and tree fit desktop and phone, light and dark', {skip:!chromium,timeout:180000},async t=>{
  const fixture=await startBrowserTestServer(t,{name:'conversation-selection',entries:['virtual:selection-test'],plugins:[{
    name:'selection-test',resolveId(id){if(id==='virtual:selection-test')return '\0virtual:selection-test';},load(id){if(id==='\0virtual:selection-test')return entry;},
    configureServer(server){server.middlewares.use((req,res,next)=>{if(req.url?.startsWith('/chat')){res.setHeader('content-type','text/html');res.end(html);}else next();});},
  }]});
  let browser;
  t.after(async()=>{try{await browser?.close();}finally{await fixture.close();}});
  browser=await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  for(const width of [1440,320]) await t.test(`${width}px`,{timeout:60000},async t=>{
    const phone=width<761;
    const context=await browser.newContext({viewport:{width,height:844},hasTouch:phone,isMobile:phone});
    t.after(()=>context.close());
    const page=await context.newPage(),errors=[],backend=createSelectionBackend();
    page.setDefaultTimeout(10000);page.setDefaultNavigationTimeout(30000);
    page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(()=>{localStorage.setItem('openbear:conversation-sidebar-view','folders');localStorage.setItem('openbear.notifications.prompt.v1','dismissed');});
    await page.route('**/api/**',async route=>{
      const request=route.request(),url=new URL(request.url());
      const {status,json}=backend.handle({path:url.pathname,method:request.method(),query:Object.fromEntries(url.searchParams),body:request.postDataJSON()});
      await route.fulfill({status,contentType:'application/json',body:JSON.stringify(json)});
    });
    await page.routeWebSocket('**/api/**',backend.socket);
    await page.goto(fixture.base+'/chat?id=manual');
    if(phone)await page.getByRole('button',{name:'打开导航',exact:true}).filter({visible:true}).click();
    await page.locator('[data-tree-id="event-0"]').waitFor({state:'visible',timeout:30000});
    await page.getByRole('button',{name:'多选会话',exact:true}).click();
    const bar=page.locator('.selection-bar');
    await bar.waitFor({state:'visible'});
    await bar.getByRole('button',{name:'更多选择方式'}).click();
    await page.getByRole('menuitem',{name:'只选本目录中自动创建的'}).click();
    await page.waitForFunction(()=>document.querySelector('.selection-count')?.textContent==='已选 60 个');
    await page.locator('[data-tree-id="event-3"] .tree-check').click();
    await page.waitForFunction(()=>document.querySelector('.selection-count')?.textContent==='已选 59 个');
    for(const dark of [false,true]){
      await page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
      const box=await bar.boundingBox();
      assert.ok(box.y+box.height<=844+1,'selection bar stays inside the viewport');
      assert.ok(await page.locator('.conversation-tree').evaluate(el=>el.scrollWidth<=el.clientWidth+1));
      assert.ok(await bar.evaluate(el=>[...el.querySelectorAll('button')].every(button=>button.scrollWidth<=button.clientWidth+1)));
      if(shots)await page.screenshot({path:`${shots}/multiselect-${width}-${dark?'dark':'light'}.png`});
    }
    assert.ok(await bar.getByRole('button',{name:'删除'}).isEnabled());
    assert.deepEqual(errors,[]);
  });
});
