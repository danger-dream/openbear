import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from './browserTestServer.mjs';
import {createToolFileBackend, uuid} from './toolFileOpenFixture.mjs';
const require=createRequire(import.meta.url);
let chromium;
try {({chromium}=require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core'));} catch {}
const shots=process.env.OPENBEAR_SCREENSHOT_DIR || '';
// Let open/close transitions settle so a screenshot shows the final state.
const capture=async(page,path)=>{if(!shots)return;await page.waitForTimeout(500);await page.screenshot({path:`${shots}/${path}`});};
const entry=`import '/src/monaco-worker.js'; import '/src/theme.js'; import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css'; import App from '/src/App.vue'; const app=createApp(App); app.use(ElementPlus); for(const [name,component] of Object.entries(Icons))app.component(name,component);app.mount('#app');`;
const html='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><script>window.__OPENBEAR_AUTH_MODE__="controller"</script></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:tool-file-test"></script></body></html>';

// Layout smoke in Chromium: the open button follows the file name on its line, the phone preview
// is full screen, the image really decodes, and the code preview is captured in light and dark.
// Which rows get the button, the preview's texts, highlighting, the unexpanded row and re-reading
// the file: toolFileOpen.dom.test.mjs.
test('a file tool row opens its file in the preview window without expanding the row', {skip:!chromium,timeout:180000},async t=>{
  const fixture=await startBrowserTestServer(t,{name:'tool-file-open',entries:['virtual:tool-file-test'],plugins:[{
    name:'tool-file-test',resolveId(id){if(id==='virtual:tool-file-test')return '\0virtual:tool-file-test';},load(id){if(id==='\0virtual:tool-file-test')return entry;},
    configureServer(server){server.middlewares.use((req,res,next)=>{if(req.url?.startsWith('/chat')){res.setHeader('content-type','text/html');res.end(html);}else next();});},
  }]});
  let browser;
  t.after(async()=>{try{await browser?.close();}finally{await fixture.close();}});
  browser=await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  for(const width of [1440,390]) await t.test(`${width}px`,{timeout:60000},async t=>{
    const phone=width<761;
    const context=await browser.newContext({viewport:{width,height:844},hasTouch:phone,isMobile:phone});
    t.after(()=>context.close());
    const page=await context.newPage(),errors=[],backend=createToolFileBackend();
    page.setDefaultTimeout(10000);page.setDefaultNavigationTimeout(30000);
    page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(()=>{localStorage.setItem('openbear.notifications.prompt.v1','dismissed');});
    await page.route('**/api/**',async route=>{
      const url=new URL(route.request().url());
      const {status,json,headers,body}=backend.handle({path:url.pathname,query:Object.fromEntries(url.searchParams)});
      if(json!==undefined)return route.fulfill({status,contentType:'application/json',body:JSON.stringify(json)});
      return route.fulfill({status,headers,body});
    });
    await page.routeWebSocket('**/api/**',backend.socket);
    await page.goto(`${fixture.base}/chat?id=${uuid}`);
    const rowOf=name=>page.locator('.conversation-process').filter({has:page.locator('.process-preview',{hasText:name})});
    const readRow=rowOf('shot.png'),writeRow=rowOf('benchmark_package.py');
    // A finished turn keeps its tool rows inside the collapsed work summary.
    const work=page.getByRole('button',{name:/已工作/});
    await work.waitFor({state:'visible',timeout:30000});
    await work.click();
    await readRow.waitFor({state:'visible'});
    await readRow.locator('.process-file-open').waitFor();
    // The button follows the file name on the same line rather than the far edge.
    const preview=await readRow.locator('.process-preview').boundingBox(),button=await readRow.locator('.process-file-open').boundingBox();
    assert.ok(button.x>=preview.x+preview.width && button.x-(preview.x+preview.width)<=10,JSON.stringify({preview,button}));
    assert.ok(Math.abs((button.y+button.height/2)-(preview.y+preview.height/2))<4,JSON.stringify({preview,button}));
    await capture(page,`tool-file-row-${width}.png`);

    await readRow.locator('.process-file-open').click();
    const dialog=page.locator('.artifact-preview-dialog');
    await dialog.waitFor({state:'visible'});
    await page.waitForFunction(()=>document.querySelector('.artifact-preview-image-stage img')?.naturalWidth>0);
    if(phone){
      const box=await dialog.boundingBox();
      assert.ok(box.width>=width-1,'phone preview is full screen');
    }
    await capture(page,`tool-file-image-${width}.png`);
    await page.keyboard.press('Escape');
    await dialog.waitFor({state:'hidden'});

    await writeRow.locator('.process-file-open').click();
    await dialog.waitFor({state:'visible'});
    await dialog.locator('.artifact-preview-source code',{hasText:'def main'}).waitFor();
    for(const dark of [false,true]){
      await page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`code preview scrolls sideways (${dark?'dark':'light'})`);
      await capture(page,`tool-file-code-${width}-${dark?'dark':'light'}.png`);
    }
    assert.deepEqual(errors,[]);
  });
});
