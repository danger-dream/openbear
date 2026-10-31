import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from './browserTestServer.mjs';
import {createToolFileBackend, uuid} from './toolFileOpenFixture.mjs';
const require=createRequire(import.meta.url);
let chromium;
try {({chromium}=require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core'));} catch {}
const shots=process.env.OPENBEAR_SCREENSHOT_DIR || '';
const capture=async(page,path)=>{if(!shots)return;await page.waitForTimeout(500);await page.screenshot({path:`${shots}/${path}`});};
const entry=`import '/src/monaco-worker.js'; import '/src/theme.js'; import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css'; import App from '/src/App.vue'; const app=createApp(App); app.use(ElementPlus); for(const [name,component] of Object.entries(Icons))app.component(name,component);app.mount('#app');`;
const html='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><script>window.__OPENBEAR_AUTH_MODE__="controller"</script></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:work-items-test"></script></body></html>';

const long='#2.3 改测试时发现两处产品代码问题，按范围没改，留给你决定：① 数据库共享读连接上，别的任务的查询执行到一半时，紧接着的读会拿到写入前的旧数据。bench 创建运行在负载下 720 次里出现过 1 次 404。② app/collab/store.py 的 one() 没在 finally 里关闭游标。详见 workspace/artifacts/reports/test-slim-python-slow.md。';
const leaf=(number,title,status,extra={})=>({number,title,status,effectiveStatus:status,statusLabel:'',statusReason:'',children:[],top:false,revision:1,progress:null,updatedAtMs:1000,createdAtMs:1000,description:'',criteria:[],scopeMethod:'',evidence:[],...extra});
const task={...leaf('3','测试改造中发现的产品问题','discussing',{top:true,description:long}),children:[leaf('3.1','共享读连接拿到旧数据','todo'),leaf('3.2','one() 没在 finally 里关闭游标','todo')],progress:{done:0,total:2,dropped:0,ask:0,blocked:0}};
const events=[
  {eventUuid:'e1',kind:'raise',label:'提出',actor:'user',actorLabel:'你',createdAtMs:Date.UTC(2026,9,31,3,49),summary:long,source:{opId:'msg:m1'},data:{quote:'这测试太傻了，有没有什么解决方案，跑浏览器还有真起节点……有这个必要吗'}},
  {eventUuid:'e2',kind:'note',label:'备注',actor:'controller',actorLabel:'主会话',createdAtMs:Date.UTC(2026,9,31,3,49),summary:'复现脚本已从 /tmp 拷到工作区保存：workspace/artifacts/reports/repro/db-read-after-write.py（原路径 /tmp/slowprof/dbg/ryw.py）。',source:{opId:'msg:m1'},
    data:{files:[{kind:'file',path:'workspace/artifacts/reports/repro/a-rather-long-directory-name/another-level/db-read-after-write-reproduction-script.py'},{kind:'file',path:'/etc/nginx/nginx.conf',server:'BeroHost-Fra'}]}},
];

function workItemsBackend() {
  const base=createToolFileBackend();
  return {...base, handle(request) {
    if(request.path===`/api/conversations/${uuid}/work-items`)
      return {status:200,json:{ok:true,tasks:[task],focus:null,running:false,turnFocuses:[],outline:{text:'<work-items>\n#3 测试改造中发现的产品问题 [讨论中] 0/2\n</work-items>',tokens:40}}};
    if(request.path===`/api/conversations/${uuid}/work-items/3`)
      return {status:200,json:{ok:true,item:{...task,ancestors:[],events}}};
    return base.handle(request);
  }};
}

// The drawer's scroll areas run to its edges: the scrollbar sits on the drawer's right edge, and
// nothing in a task's detail (long text, file paths, evidence rows) scrolls sideways.
test('the task drawer scrolls edge to edge without sideways overflow', {skip:!chromium,timeout:180000},async t=>{
  const fixture=await startBrowserTestServer(t,{name:'work-items',entries:['virtual:work-items-test'],plugins:[{
    name:'work-items-test',resolveId(id){if(id==='virtual:work-items-test')return '\0virtual:work-items-test';},load(id){if(id==='\0virtual:work-items-test')return entry;},
    configureServer(server){server.middlewares.use((req,res,next)=>{if(req.url?.startsWith('/chat')){res.setHeader('content-type','text/html');res.end(html);}else next();});},
  }]});
  let browser;
  t.after(async()=>{try{await browser?.close();}finally{await fixture.close();}});
  browser=await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  for(const width of [1440,390]) await t.test(`${width}px`,{timeout:60000},async t=>{
    const phone=width<761;
    const context=await browser.newContext({viewport:{width,height:844},hasTouch:phone,isMobile:phone});
    t.after(()=>context.close());
    const page=await context.newPage(),errors=[],backend=workItemsBackend();
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
    await page.getByRole('button',{name:/已工作/}).waitFor({state:'visible',timeout:30000});
    if(phone){
      await page.getByRole('button',{name:'更多会话操作'}).click();
      await page.locator('.mobile-tool-actions button:has(span:text-is("任务"))').click();
    } else {
      await page.locator('.work-items-entry').click();
    }
    const drawer=page.locator('.work-items-drawer');
    await drawer.locator('.wi-card').waitFor();
    const settled=()=>page.waitForFunction(()=>{const node=document.querySelector('.work-items-drawer');return node && !node.getAnimations().length && Math.abs(node.getBoundingClientRect().right-innerWidth)<1;});
    await settled();
    const edges=async()=>page.evaluate(()=>{
      const panel=document.querySelector('.work-items-drawer').getBoundingClientRect();
      const scroll=[...document.querySelectorAll('.work-items-drawer .wi-scroll')].find(node=>node.offsetParent);
      const box=scroll.getBoundingClientRect();
      return {panelLeft:panel.left,panelRight:panel.right,left:box.left,right:box.right,sideways:scroll.scrollWidth-scroll.clientWidth};
    });
    let box=await edges();
    assert.ok(Math.abs(box.right-box.panelRight)<1 && Math.abs(box.left-box.panelLeft)<1,`list scrolls edge to edge ${JSON.stringify(box)}`);
    await capture(page,`work-items-list-${width}.png`);

    await drawer.locator('.wi-card-main').click();
    await drawer.locator('.wi-tl li').first().waitFor();
    box=await edges();
    assert.ok(Math.abs(box.right-box.panelRight)<1,`detail scrolls at the drawer edge ${JSON.stringify(box)}`);
    assert.equal(box.sideways,0,'nothing in the detail scrolls sideways');
    // Children with nothing to expand line up with the text above, not behind a toggle's empty room.
    const indent=await page.evaluate(()=>{
      const text=document.querySelector('.work-items-drawer .wi-blk p').getBoundingClientRect().left;
      const icon=document.querySelector('.work-items-drawer .wi-kids .wi-row .wi-status-icon').getBoundingClientRect().left;
      return icon-text;
    });
    assert.ok(indent<16,`child rows are not pushed in (${indent}px)`);
    // The raise entry shows the user's words; the description above is not repeated under it.
    assert.equal(await drawer.locator('.wi-tl li').first().locator('.wi-tlb').count(),0);
    await drawer.locator('.wi-tl li').first().locator('.wi-quote',{hasText:'这测试太傻了'}).waitFor();
    // A file on another server names it and cannot be opened.
    const remote=drawer.locator('.wi-evi',{hasText:'/etc/nginx/nginx.conf'});
    await remote.locator('.wi-server',{hasText:'服务器 BeroHost-Fra'}).waitFor();
    assert.equal(await remote.locator('.wi-op').count(),0);
    for(const dark of [false,true]){
      await page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
      await capture(page,`work-items-detail-${width}-${dark?'dark':'light'}.png`);
    }
    if(phone){
      // A bottom sheet: a dimmed strip stays above it to tap, and dragging its header down closes it.
      const top=await drawer.evaluate(node=>node.getBoundingClientRect().top);
      assert.ok(top>=48,`room above the sheet to tap (${top}px)`);
      const head=await drawer.locator('.wi-head h2').boundingBox();
      await page.mouse.move(head.x+20,head.y+5);await page.mouse.down();
      for(const dy of [10,40,80,140])await page.mouse.move(head.x+20,head.y+5+dy);
      assert.ok(await drawer.evaluate(node=>node.classList.contains('is-dragging')),'the sheet follows the drag');
      await page.mouse.up();
      await drawer.waitFor({state:'hidden'});
      await page.getByRole('button',{name:'更多会话操作'}).click();
      await page.locator('.mobile-tool-actions button:has(span:text-is("任务"))').click();
      await drawer.locator('.wi-tl, .wi-card').first().waitFor();
      await settled();
      await page.mouse.click(width/2,20);
      await drawer.waitFor({state:'hidden'});
    }
    assert.deepEqual(errors,[]);
  });
});

// A running scope on a task without children: the composer's progress line and the task card both
// say it is being worked on.
test('a running task without children shows as being worked on', {skip:!chromium,timeout:120000},async t=>{
  const fixture=await startBrowserTestServer(t,{name:'work-items-running',entries:['virtual:work-items-test'],plugins:[{
    name:'work-items-test',resolveId(id){if(id==='virtual:work-items-test')return '\0virtual:work-items-test';},load(id){if(id==='\0virtual:work-items-test')return entry;},
    configureServer(server){server.middlewares.use((req,res,next)=>{if(req.url?.startsWith('/chat')){res.setHeader('content-type','text/html');res.end(html);}else next();});},
  }]});
  let browser;
  t.after(async()=>{try{await browser?.close();}finally{await fixture.close();}});
  browser=await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  const context=await browser.newContext({viewport:{width:1440,height:844}});
  const page=await context.newPage(),errors=[],base=createToolFileBackend();
  page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>{localStorage.setItem('openbear.notifications.prompt.v1','dismissed');});
  const lone={...leaf('4','优化模型拦截配置界面','in_progress',{top:true}),progress:null};
  await page.route('**/api/**',async route=>{
    const url=new URL(route.request().url());
    if(url.pathname===`/api/conversations/${uuid}/work-items`)
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true,tasks:[lone],running:true,
        focus:{numbers:['4'],current:'',basis:{human:true,createdAtMs:Date.UTC(2026,9,31,17,6)},createdAtMs:Date.UTC(2026,9,31,17,6)},turnFocuses:[],outline:{text:'',tokens:0}})});
    const {status,json,headers,body}=base.handle({path:url.pathname,query:Object.fromEntries(url.searchParams)});
    if(json!==undefined)return route.fulfill({status,contentType:'application/json',body:JSON.stringify(json)});
    return route.fulfill({status,headers,body});
  });
  await page.routeWebSocket('**/api/**',base.socket);
  await page.goto(`${fixture.base}/chat?id=${uuid}`);
  const dock=page.locator('.wi-dock');
  await dock.waitFor({state:'visible',timeout:30000});
  await dock.locator('.wi-dock-now',{hasText:'正在做'}).waitFor();
  await dock.locator('.wi-dock-cur',{hasText:'#4 优化模型拦截配置界面'}).waitFor();
  await dock.locator('.wi-dock-main').click();
  await dock.locator('.wi-dock-row.is-current',{hasText:'#4'}).waitFor();
  await capture(page,'work-items-running-dock.png');
  await page.locator('.work-items-entry').click();
  await page.locator('.work-items-drawer .wi-card .wi-now',{hasText:'正在做'}).waitFor();
  await page.locator('.work-items-drawer .wi-focus-cur',{hasText:'#4'}).waitFor();
  await capture(page,'work-items-running-drawer.png');
  assert.deepEqual(errors,[]);
});
