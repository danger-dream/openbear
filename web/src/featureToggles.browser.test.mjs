import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from './browserTestServer.mjs';
import {featureApi, createFeatureState} from './featureTogglesFixture.mjs';
const require=createRequire(import.meta.url);
let chromium;
try {({chromium}=require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core'));} catch {}
const shots=process.env.OPENBEAR_SCREENSHOT_DIR || '';

// Layout smoke of the settings switch and the hidden-page notice in the real App, on desktop,
// phone and dark. What the menus list, the requests and the links are checked without a
// browser in featureToggles.dom.test.mjs.
const entry=`import '/src/monaco-worker.js'; import '/src/theme.js'; import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css'; import App from '/src/App.vue'; const app=createApp(App); app.use(ElementPlus); for(const [name,component] of Object.entries(Icons))app.component(name,component);app.mount('#app');`;
const html='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><script>window.__OPENBEAR_AUTH_MODE__="controller"</script></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:feature-toggles-test"></script></body></html>';
const PAGES=['/chat','/settings','/instances','/servers'];

test('实例/主机 switch and hidden-page notice: desktop, phone and dark layout', {skip:!chromium,timeout:120000}, async t=>{
  const state=createFeatureState();
  const fixture=await startBrowserTestServer(t,{name:'feature-toggles',entries:['virtual:feature-toggles-test','/src/views/SettingsHubView.vue','/src/views/SettingsView.vue','/src/views/FeatureDisabledView.vue'],plugins:[{
    name:'feature-toggles-test',resolveId(id){if(id==='virtual:feature-toggles-test')return '\0virtual:feature-toggles-test';},load(id){if(id==='\0virtual:feature-toggles-test')return entry;},
    configureServer(server){server.middlewares.use((req,res,next)=>{const path=(req.url||'').split('?')[0];if(PAGES.includes(path)){res.setHeader('content-type','text/html');res.end(html);}else next();});},
  }]});
  let browser;
  t.after(async()=>{try{await browser?.close();}finally{await fixture.close();}});
  browser=await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});

  async function open(t,{width,dark=false,path}){ // dark: the stored theme the page starts with
    const phone=width<761;
    const context=await browser.newContext({viewport:{width,height:844},hasTouch:phone,isMobile:phone});
    t.after(()=>context.close());
    const page=await context.newPage(),errors=[];
    page.setDefaultTimeout(10000);page.setDefaultNavigationTimeout(30000);
    page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(dark=>{localStorage.setItem('openbear.notifications.prompt.v1','dismissed');localStorage.setItem('openbear.theme.v1',dark?'dark':'light');},dark);
    await page.route('**/api/**',async route=>{
      const request=route.request(),url=new URL(request.url());
      const {status,json}=await featureApi(state,{path:url.pathname,method:request.method(),body:request.postDataJSON()});
      await route.fulfill({status,contentType:'application/json',body:JSON.stringify(json)});
    });
    await page.routeWebSocket('**/api/**',socket=>{
      if(new URL(socket.url()).pathname==='/api/events/ws')socket.send(JSON.stringify({type:'snapshot',epoch:'test',seq:1,items:[],includeArchived:false}));
      socket.onMessage(message=>{if(message==='ping')socket.send('pong');});
    });
    await page.goto(fixture.base+path);
    return {page,phone,errors};
  }
  const noOverflow=page=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1);
  Object.assign(state,{controller:'new',values:{'features.instances':false,'features.servers':false}});

  // One page per width; light and dark by the theme class, then the hidden page in the same
  // context (its module cache makes the second load cheap).
  const themed=(page,dark)=>page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
  for(const width of [1440,390]) await t.test(`${width}px light and dark`,async t=>{
    const tag=width<761?'mobile':'desktop';
    const h=await open(t,{width,path:'/settings?section=system-settings'});
    const row=h.page.locator('.settings-row',{hasText:'启用主机'});
    await row.waitFor();
    for(const dark of [false,true]){
      const theme=dark?'dark':'light';
      await themed(h.page,dark);
      assert.ok(await noOverflow(h.page),`settings ${theme}`);
      assert.ok(await row.locator('.mac-toggle').isVisible());
      if(shots)await h.page.screenshot({path:`${shots}/settings-${tag}-${theme}.png`});
    }
    if(shots){
      if(h.phone){
        await h.page.getByRole('button',{name:'打开导航',exact:true}).filter({visible:true}).first().click();
        await h.page.getByRole('button',{name:'资源与设置',exact:true}).click();
        await h.page.locator('.sidebar-resources-grid').filter({visible:true}).waitFor();
        await h.page.screenshot({path:`${shots}/nav-${tag}-off-dark.png`});
      } else await h.page.screenshot({path:`${shots}/nav-${tag}-off-dark.png`,clip:{x:0,y:0,width:320,height:844}});
    }
    // The notice a direct link to a hidden page shows, with its button reachable.
    await h.page.goto(fixture.base+'/servers');
    await h.page.getByText('主机功能未开启',{exact:true}).waitFor();
    for(const dark of [false,true]){
      const theme=dark?'dark':'light';
      await themed(h.page,dark);
      assert.ok(await noOverflow(h.page),`hidden page ${theme}`);
      assert.ok(await h.page.getByRole('button',{name:'开启主机'}).isVisible());
      if(shots)await h.page.screenshot({path:`${shots}/hidden-servers-${tag}-${theme}.png`});
    }
    assert.deepEqual(h.errors,[]);
  });
});
