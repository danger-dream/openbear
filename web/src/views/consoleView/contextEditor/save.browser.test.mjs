import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdirSync} from 'node:fs';
import {startBrowserTestServer, waitForBrowserReady} from '../../../browserTestServer.mjs';
import {createContextSaveBackend} from './saveFixture.mjs';
const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}
const shots = process.env.OPENBEAR_CONTEXT_SHOTS || '';
const html = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app" style="height:100dvh;position:relative"></div><script type="module" src="/@id/__x00__virtual:context-save-test"></script></body></html>';
const entry = `import '/src/monaco-worker.js';import '/src/theme.js';import {createApp,h,ref} from 'vue';import ElementPlus from 'element-plus';import 'element-plus/dist/index.css';import '/src/style.css';import '/src/mobile-inputs.css';import '/src/mobile-overlays.css';import ContextEditor from '/src/views/consoleView/ContextEditor.vue';
if(window.__dark)document.documentElement.classList.add('dark');
createApp({setup(){const editor=ref(null);return()=>[h('button',{onClick:()=>editor.value.open()},'Open context editor'),h(ContextEditor,{ref:editor,conversationUuid:'original',onSaved:value=>window.saved=value,onCreated:value=>window.created=value})];}}).use(ElementPlus).mount('#app');`;
// In Chromium: the confirm dialog inside the screen, no sideways scroll, 16px title input on
// phones, light and dark (layout smoke); and the whole flow with the keyboard up (390×320), where
// the editor's measured height switches it to panes. What the flow sends and shows on desktop,
// phone and dark: save.dom.test.mjs.
test('context editor save controls fit desktop, phone, dark and a phone with the keyboard up', {skip:!chromium,timeout:180000,concurrency:4}, async t => {
  // The four setups run side by side, one page each.
  const fixture = await startBrowserTestServer(t,{name:'context-save',entries:['virtual:context-save-test','/src/views/consoleView/ContextEditor.vue'],plugins:[{name:'context-save-test',resolveId(id){if(id==='virtual:context-save-test')return '\0virtual:context-save-test';},load(id){if(id==='\0virtual:context-save-test')return entry;},configureServer(server){server.middlewares.use((req,res,next)=>{if(req.url==='/__context-save'){res.setHeader('content-type','text/html');res.end(html);}else next();});}}]});
  let browser;
  t.after(async()=>{try{await browser?.close();}finally{await fixture.close();}});
  browser=await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  if(shots)mkdirSync(shots,{recursive:true});
  await Promise.all([['desktop',1440,900,false],['phone',390,844,false],['dark',360,780,true],['keyboard',390,320,false]].map(([name,width,height,dark]) =>
    t.test(name,{timeout:60000},async t=>{
      const full=name==='keyboard';
      const context=await browser.newContext({viewport:{width,height},hasTouch:width<760,isMobile:width<760});t.after(()=>context.close());
      const page=await context.newPage(),errors=[],backend=createContextSaveBackend(),{writes}=backend;
      page.on('pageerror',error=>errors.push(error.message));
      await page.addInitScript(dark=>{window.__dark=dark;},dark);
      await page.route('**/api/conversations/original/context-editor**',async route=>{
        const request=route.request();
        const {status,json}=backend.handle({path:new URL(request.url()).pathname,method:request.method(),body:request.postDataJSON()});
        await route.fulfill({status,contentType:'application/json',body:JSON.stringify(json)});
      });
      await page.goto(fixture.base+'/__context-save');
      await waitForBrowserReady(page,page.getByRole('button',{name:'Open context editor',exact:true}),errors);
      await page.getByRole('button',{name:'Open context editor',exact:true}).click();
      await page.locator('.context-editor .ce-workspace').waitFor();
      if(name==='keyboard')await page.getByRole('button',{name:'目录',exact:true}).click();
      await page.getByRole('checkbox',{name:'选择 a',exact:true}).check();
      await page.getByRole('button',{name:'删除…',exact:true}).click();
      await page.getByRole('dialog').getByRole('button',{name:'确认删除',exact:true}).click();
      if(name==='keyboard')await page.getByRole('button',{name:'操作',exact:true}).click();
      if(full)assert.equal(await page.getByRole('button',{name:'保存到原会话',exact:true}).isDisabled(),true);
      await page.getByRole('button',{name:'校验并预览',exact:true}).click();
      if(name==='keyboard')await page.getByRole('button',{name:'正文',exact:true}).click();
      await page.locator('.ce-help[role="status"]').filter({hasText:'自动处理 25 组'}).waitFor();
      if(full)assert.equal(await page.locator('.ce-help[role="status"]').count(),1);
      if(name==='keyboard')await page.getByRole('button',{name:'操作',exact:true}).click();
      if(full)assert.equal(await page.getByRole('button',{name:'保存副本',exact:true}).isDisabled(),false);
      await page.getByRole('button',{name:'保存到原会话',exact:true}).click();
      const dialog=page.getByRole('dialog',{name:'保存到原会话',exact:true});
      await dialog.waitFor();if(full)assert.equal(writes.length,0);
      if(full)assert.match(await dialog.innerText(),/原始聊天和工具执行记录不变/);
      await page.waitForFunction(()=>document.getAnimations().every(a=>a.playState!=='running'));
      const bounds=await dialog.boundingBox();assert.ok(bounds.x>=0 && bounds.x+bounds.width<=width+1);
      if(shots)await page.screenshot({path:`${shots}/${name}-confirm.png`,animations:'disabled'});
      await dialog.getByRole('button',{name:'保存到原会话',exact:true}).click();
      await page.waitForFunction(()=>window.saved?.conversationUuid==='original');
      if(full){assert.equal(writes[0].body.working.entries.length,1);assert.equal(writes[0].body.revision,0);assert.equal(writes[0].body.snapshotToken,'initial');}
      if(full)assert.match(await page.locator('.ce-status').innerText(),/已保存到原会话/);
      if(full)assert.equal(await page.locator('.context-editor').count(),1);
      const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);assert.equal(overflow,false);
      if(width<760)assert.equal(await page.getByRole('textbox',{name:'副本标题',exact:true}).evaluate(el=>getComputedStyle(el).fontSize),'16px');
      // A server-side conflict leaves the current editor available instead of navigating away.
      if(full){
        await page.getByRole('button',{name:'校验并预览',exact:true}).click();backend.conflict();
        await page.getByRole('button',{name:'保存到原会话',exact:true}).click();
        await page.getByRole('dialog',{name:'保存到原会话',exact:true}).getByRole('button',{name:'保存到原会话',exact:true}).click();
        await page.getByRole('alert').filter({hasText:'409'}).waitFor();
        assert.equal(await page.locator('.context-editor').count(),1);
        await page.getByRole('textbox',{name:'副本标题',exact:true}).fill('Context copy');
        await page.getByRole('button',{name:'保存副本',exact:true}).click();
        await page.waitForFunction(()=>window.created?.conversationUuid==='copy');
        assert.equal(writes.at(-1).method,'branch');assert.equal(writes.at(-1).body.title,'Context copy');
      }
      // The existing generic Monaco worker does not implement JSON language services.
      const knownWorkerError=/Missing requestHandler or method: (?:findDocumentColors|findDocumentSymbols|getFoldingRanges|resetSchema)/;
      assert.deepEqual(errors.filter(error=>!knownWorkerError.test(error)),[]);
      if(errors.length)t.diagnostic(`Existing Monaco JSON-worker warnings: ${errors.length}`);
    })));
});
