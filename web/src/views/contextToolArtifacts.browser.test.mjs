import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from '../browserTestServer.mjs';
import {createSettingsBackend} from './contextToolArtifactsFixture.mjs';

let chromium;
try { ({chromium} = createRequire(import.meta.url)(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}
const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"><script>window.__OPENBEAR_AUTH_MODE__="controller";</script></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:context-settings-test"></script></body></html>';
const entry = `import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import '/src/theme.js'; import 'element-plus/dist/index.css'; import '/src/style.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css'; import View from '/src/views/SettingsView.vue'; createApp(View).use(ElementPlus).mount('#app');`;

// Layout smoke of the tool-result setting row in Chromium: after editing it, the page does not
// scroll sideways and its text stays distinguishable from its background, on a light phone and a
// dark desktop. The request it sends and the older-server path: contextToolArtifacts.dom.test.mjs.
test('tool result setting real mobile/desktop light/dark layout', {skip:!chromium, timeout:180000}, async t=>{
  // The shared fixture owns the Vite cache, including removal after an interrupted run.
  const fixture = await startBrowserTestServer(t, {name:'context-settings', entries:['virtual:context-settings-test'], warmupTimeoutMs:150000,
    plugins:[{name:'context-settings-test', resolveId(id){if(id==='virtual:context-settings-test')return '\0virtual:context-settings-test';},
      load(id){if(id==='\0virtual:context-settings-test')return entry;}, configureServer(server){server.middlewares.use((req,res,next)=>{
        if(req.url?.split('?')[0]==='/__test'){res.setHeader('content-type','text/html');res.end(html);}else next();
      });}}]});
  let browser;
  t.after(async()=>{try{await browser?.close();}finally{await fixture.close();}});
  const base=fixture.base;
  browser=await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  // The new setting row is checked at phone and desktop width, in light and dark
  // (the only theme assertion is its text/background contrast).
  for(const [width,theme] of [[390,'light'],[1280,'dark']]){
    await t.test(`${width}px ${theme}`, {timeout:60000}, async t=>{
      const context=await browser.newContext({viewport:{width,height:844}});
      t.after(()=>context.close());
      await context.addInitScript(theme=>localStorage.setItem('openbear.theme.v1',theme),theme);
      const page=await context.newPage(), errors=[], backend=createSettingsBackend();
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('**/*',async route=>{
        const request=route.request(), url=new URL(request.url());
        if(url.origin!==base)return route.abort();
        if(!url.pathname.startsWith('/api/'))return route.continue();
        const {status,json}=backend.handle({path:url.pathname,method:request.method(),body:request.postDataJSON()});
        await route.fulfill({status,contentType:'application/json',body:JSON.stringify(json)});
      });
      await page.goto(base+'/__test');
      const row=page.locator('.settings-row').filter({has:page.locator('h4',{hasText:'工具结果落盘阈值'})});
      await row.waitFor();
      await row.locator('.value-pill').click();
      await row.locator('input').fill('0');
      await row.locator('input').press('Enter');
      await row.locator('.value-pill').filter({hasText:'关闭（0 token）'}).waitFor();
      const dimensions=await page.evaluate(()=>({width:innerWidth,body:document.body.scrollWidth,root:document.documentElement.scrollWidth}));
      assert.ok(dimensions.body<=width+1&&dimensions.root<=width+1,JSON.stringify(dimensions));
      const colors=await row.evaluate(el=>({background:getComputedStyle(el).backgroundColor,text:getComputedStyle(el.querySelector('p')).color}));
      assert.notEqual(colors.background,colors.text);
      assert.deepEqual(errors,[]);
    });
  }
});
