import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from './browserTestServer.mjs';
import {createPendingBackend} from './pendingConversationsFixture.mjs';
const require=createRequire(import.meta.url);
let chromium;
try {({chromium}=require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core'));} catch {}
const entry=`import '/src/monaco-worker.js'; import '/src/theme.js'; import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css'; import App from '/src/App.vue'; const app=createApp(App); app.use(ElementPlus); for(const [name,component] of Object.entries(Icons))app.component(name,component);app.mount('#app');`;
const html='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><script>window.__OPENBEAR_AUTH_MODE__="controller"</script></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:pending-test"></script></body></html>';

// Layout smoke of the pending-work entry and panel on the real App in Chromium: the phone strip
// leaves the composer on screen, the desktop hides the phone entry, the panel does not overflow
// in light and dark, the phone panel's header drag is captured, and the "new conversation" toast
// does not take taps. Counts, texts, navigation to the exact form, the kept draft and the
// real-time clearing: pendingConversations.dom.test.mjs.
test('pending work entry and panel layout on desktop and phone', {skip:!chromium,timeout:180000},async t=>{
  const fixture=await startBrowserTestServer(t,{name:'pending-conversations',entries:['virtual:pending-test'],plugins:[{
    name:'pending-test',resolveId(id){if(id==='virtual:pending-test')return '\0virtual:pending-test';},load(id){if(id==='\0virtual:pending-test')return entry;},
    configureServer(server){server.middlewares.use((req,res,next)=>{if(req.url?.startsWith('/chat')){res.setHeader('content-type','text/html');res.end(html);}else next();});},
  }]});
  let browser;
  t.after(async()=>{try{await browser?.close();}finally{await fixture.close();}});
  browser=await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  // Desktop and the narrowest phone; 390px followed the same phone branches.
  for(const width of [1440,320]) await t.test(`${width}px`,{timeout:60000},async t=>{
    const context=await browser.newContext({viewport:{width,height:844},hasTouch:width<761,isMobile:width<761});
    t.after(()=>context.close());
    const page=await context.newPage(),errors=[],backend=createPendingBackend();
    page.setDefaultTimeout(10000);page.setDefaultNavigationTimeout(30000);
    page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(()=>{localStorage.setItem('openbear:conversation-sidebar-view','folders');localStorage.setItem('openbear.notifications.prompt.v1','dismissed');});
    await page.route('**/api/**',async route=>{
      const request=route.request(),url=new URL(request.url());
      const {status,json}=backend.handle({path:url.pathname,method:request.method(),query:Object.fromEntries(url.searchParams)});
      await route.fulfill({status,contentType:'application/json',body:JSON.stringify(json)});
    });
    await page.routeWebSocket('**/api/**',backend.socket);
    await page.goto(fixture.base+'/chat?id=current');
    await page.waitForFunction(()=>document.querySelector('.recent-waiting-count')?.textContent.includes('待处理 1'),{timeout:30000});
    const phone=width<761;
    if(phone){
      assert.ok(await page.locator('.composer-shell').evaluate(el=>el.getBoundingClientRect().bottom<=innerHeight+1),'pending strip must not push the composer below the viewport');
      await page.locator('.mobile-pending-entry').click();
    }else{
      assert.equal(await page.locator('.mobile-pending-entry').isVisible(),false);
      await page.locator('.recent-waiting-count').click();
    }
    const panel=page.locator('.pending-conversations');
    await panel.waitFor({state:'visible'});
    await panel.locator('[data-interaction-id="choose-two"]').waitFor();
    for(const dark of [false,true]){
      await page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
      assert.ok(await panel.evaluate(el=>el.scrollWidth<=el.clientWidth+1));
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    }
    await page.evaluate(()=>document.documentElement.classList.remove('dark'));
    if(phone){
      await panel.locator('.pending-header').click({position:{x:40,y:20}});
      const box=await panel.locator('.pending-header').boundingBox();
      const cdp=await context.newCDPSession(page);
      const touch=(y)=>[{x:box.x+40,y:box.y+y,id:1}];
      await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:touch(20)});
      await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:touch(75)});
      await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:touch(135)});
      assert.ok(await panel.evaluate(el=>el.classList.contains('is-dragging')),'header drag is captured after the enter animation');
      await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
      await cdp.detach();
    }else await page.keyboard.press('Escape');
    await panel.waitFor({state:'hidden'});
    if(phone){
      // The "new conversation" toast covers the phone title bar; it must not take the tap.
      await page.getByRole('button',{name:'打开导航',exact:true}).filter({visible:true}).click();
      await page.locator('.sidebar-new-session').click();
      await page.waitForURL(url=>url.searchParams.get('id')==='local:new');
      assert.equal(await page.locator('.el-message').first().evaluate(el=>getComputedStyle(el).pointerEvents),'none');
    }
    assert.deepEqual(errors,[]);
  });
});
