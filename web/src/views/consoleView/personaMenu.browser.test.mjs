import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from '../../browserTestServer.mjs';
const require=createRequire(import.meta.url);
let chromium;
try {({chromium}=require('../../testing/playwright.cjs'));} catch {}
const entry=`
import {createApp,h,ref} from 'vue';
import ElementPlus from 'element-plus';
import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css';
import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css';
import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css';
import ConsoleComposer from '/src/views/consoleView/ConsoleComposer.vue';
const selected=ref('9'),draft=ref('');window.__sends=0;
window.__select=value=>selected.value=value;window.__draft=()=>draft.value;
createApp({render:()=>h('div',{style:'position:fixed;bottom:0;left:0;right:0;max-width:820px;margin:auto;padding:8px;box-sizing:border-box'},[
 h(ConsoleComposer,{conversationUuid:'persona-review',promptTemplate:selected.value,promptTemplateMissing:selected.value==='9',promptTemplateLocked:true,
 currentModel:'openai/gpt',draft:draft.value,'onUpdate:draft':value=>draft.value=value,canSend:true,onSend:()=>window.__sends++})
])}).use(ElementPlus).mount('#app');`;
const html='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:persona-review"></script></body></html>';

test('real composer persona attention and explicit main selection work on desktop and phone without blocking', {skip:!chromium,timeout:240000}, async t=>{
 const fixture=await startBrowserTestServer(t,{name:'persona-review',entries:['virtual:persona-review'],plugins:[{
  name:'persona-review',resolveId:id=>id==='virtual:persona-review'?'\0virtual:persona-review':undefined,
  load:id=>id==='\0virtual:persona-review'?entry:undefined,
  configureServer(server){server.middlewares.use((req,res,next)=>{if(req.url==='/fixture'){res.setHeader('content-type','text/html');res.end(html);}else next();});},
 }]});
 let browser;t.after(async()=>{try{await browser?.close();}finally{await fixture.close();}});
 browser=await chromium.launch({executablePath:process.env.OPENBEAR_CHROME||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
 for(const width of [1440,390]) for(const dark of [false,true]) await t.test(`${width}px ${dark?'dark':'light'}`,async t=>{
  const phone=width<761,context=await browser.newContext({viewport:{width,height:844},isMobile:phone,hasTouch:phone});t.after(()=>context.close());
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(12000);
  await page.route('**/api/**',route=>{
   const path=new URL(route.request().url()).pathname;
   return route.fulfill({json:path==='/api/prompt-templates/choices'?{active:{id:'7',name:'当前主模板',chars:4},items:[{id:'8',name:'写作'}],existingIds:['7','8']}:{ok:true,items:[],catalog:[],enabled:[],effective:[]}});
  });
  await page.goto(`${fixture.base}/fixture`);
  await page.evaluate(d=>document.documentElement.classList.toggle('dark',d),dark);
  async function openPersona(){
   if(phone){await page.getByRole('button',{name:'功能',exact:true}).click();await page.locator('.tools-row').filter({hasText:'人设'}).click();}
   else await page.locator('.persona-menu-trigger').click();
  }
  if(phone){
   await page.getByRole('button',{name:'功能',exact:true}).click();
   const row=page.locator('.tools-row').filter({hasText:'人设'});
   assert.equal(await row.locator('.tools-attention').innerText(),'!');
   await row.click();
  }else{
   assert.equal(await page.locator('.persona-menu-trigger .is-attention').innerText(),'!');
   await openPersona();
  }
  const panel=page.locator(phone?'.persona-sheet .persona-panel':'.persona-menu-popper .persona-panel');
  await panel.waitFor({state:'visible'});
  assert.match(await panel.locator('[aria-selected="true"]').innerText(),/模板 #9/);
  assert.ok(await panel.evaluate(el=>el.scrollWidth<=el.clientWidth+1));
  if(phone) await page.getByRole('button',{name:'关闭人设',exact:true}).click(); else await page.locator('.persona-menu-trigger').click();
  await panel.waitFor({state:'hidden'});
  await page.evaluate(()=>window.__select('7'));
  await openPersona();await panel.waitFor({state:'visible'});
  assert.equal(await panel.locator('[aria-selected="true"]').count(),1);
  assert.match(await panel.locator('[aria-selected="true"]').innerText(),/当前主模板/);
  assert.equal(await panel.locator('.persona-row').filter({hasText:'已选定此模板'}).count(),1);
  if(phone) await page.getByRole('button',{name:'关闭人设',exact:true}).click(); else await page.locator('.persona-menu-trigger').click();
  await panel.waitFor({state:'hidden'});
  if(phone){
   const editor=page.locator('[contenteditable="true"]').first();await editor.fill('line one');await editor.press('Enter');await editor.pressSequentially('line two');
   assert.equal(await page.evaluate(()=>window.__sends),0,'phone Enter remains newline, never sends');
   assert.match(await page.evaluate(()=>window.__draft()),/line one\nline two/);
  }
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  assert.deepEqual(errors,[]);
 });
});
