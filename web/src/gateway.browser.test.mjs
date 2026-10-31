import test from 'node:test';
import assert from 'node:assert/strict';
import {startBrowserTestServer, waitForBrowserReady} from './browserTestServer.mjs';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
let chromium, WebSocketServer;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); ({WebSocketServer} = require('/opt/openclaw/node_modules/ws')); } catch {}
const uuid = '11111111-1111-4111-8111-111111111111', fileId='22222222-2222-4222-8222-222222222222';
const artifact = `/api/conversations/${uuid}/artifacts/${fileId}/content`;
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jhS4AAAAASUVORK5CYII=', 'base64');

// Actual browser requests, cookies, byte streams and sockets go to an isolated
// HTTP fixture enforcing the wire protocol. No route interception or online API.
// Kept in the browser on purpose: HttpOnly cookies, real HTTP byte streams, WebSocket upgrades,
// image loads, IndexedDB, other tabs and the service worker are what these tests check, plus the
// real main.js bootstrap. The same rules as plain logic (calls carry the binding, URLs that get
// it, push owner, refused rebinding, notification for another instance) run without a browser in
// gateway.dom.test.mjs.
test('gateway real frontend / HTTP / WS / file / cross-account boundary', {skip: !chromium || !WebSocketServer, timeout: 240000}, async t => {
  const calls=[], forwarded=[], sessions = new Map(), sockets = new Set(); let serial=0, mode='gateway', modeFailure=false;
  const mint = user => {const cookie=`cookie-${++serial}`;const row={cookie,mode:'gateway',instanceId:user,username:user,contextToken:`page-${serial}`,chatId:1,expiresAt:Math.floor(Date.now()/1000)+3600};sessions.set(cookie,row);return row;};
  const sessionFor = req => sessions.get((req.headers.cookie || '').match(/openbear_gateway_session=([^;]+)/)?.[1]);
  const reply = (res,status,data) => {res.writeHead(status,{'content-type':'application/json','cache-control':'no-store'});res.end(JSON.stringify(data));};
  const end = session => {sessions.delete(session.cookie);for(const socket of sockets) if(socket.session===session) socket.close(1008,'revoked');};
  const fixture=await startBrowserTestServer(t,{name:'gateway',authMode:()=>mode,entries:['/src/main.js','/src/App.vue','/src/views/SessionsView.vue'],plugins:[{name:'gateway-wire-fixture',configureServer(server){server.middlewares.use(async(req,res,next)=>{
    const url=new URL(req.url,'http://fixture');if(!url.pathname.startsWith('/api/'))return next();
    const chunks=[];for await(const chunk of req)chunks.push(chunk);let body={};try{body=JSON.parse(Buffer.concat(chunks).toString()||'{}');}catch{}
    const session=sessionFor(req), context=req.headers['x-openbear-context'] || url.searchParams.get('_ob_ctx');calls.push({path:url.pathname,method:req.method,context,cookie:session?.cookie,body});
    if(url.pathname==='/api/auth/mode')return reply(res,200,{ok:true,mode});
    if(modeFailure&&url.pathname==='/api/auth/session')return reply(res,503,{ok:false});
    if(url.pathname==='/api/auth/password/login'){
      if(session)return reply(res,409,{ok:false,error:'logout_required'});
      if(body.password!=='fixture-password')return reply(res,401,{ok:false,error:'invalid_credentials'});
      const row=mint(body.username);res.setHeader('set-cookie',`openbear_gateway_session=${row.cookie}; Path=/; HttpOnly; SameSite=Lax`);return reply(res,200,{ok:true});
    }
    if(!session && mode==='gateway')return reply(res,401,{ok:false,error:'authentication_required'});
    if(mode==='gateway' && (url.pathname!=='/api/auth/session'||context) && context!==session.contextToken)return reply(res,409,{ok:false,error:'session_context_changed'});
    if(url.pathname==='/api/auth/session')return reply(res,200,{ok:true,...session});
    if(url.pathname==='/api/auth/logout'||url.pathname==='/api/auth/password/change'){
      if(url.pathname.endsWith('/change') && (body.currentPassword!=='fixture-password'||!body.password||'newPassword' in body))return reply(res,403,{ok:false,error:'invalid_credentials'});
      end(session);res.setHeader('set-cookie','openbear_gateway_session=; Path=/; Max-Age=0');return reply(res,200,{ok:true});
    }
    forwarded.push({instance:session?.instanceId,path:url.pathname,body});
    if(url.pathname===artifact){res.writeHead(200,{'content-type':url.searchParams.has('text')?'text/plain':'image/png','cache-control':'no-store'});return res.end(url.searchParams.has('text')?'file-content':png);}
    if(url.pathname==='/api/slow-stream'){res.writeHead(200,{'content-type':'text/plain'});res.write('first');const timer=setInterval(()=>res.write('part'),50);res.on('close',()=>clearInterval(timer));return;}
    if(url.pathname==='/api/push/key')return reply(res,200,{ok:true,publicKey:'AQID'});
    if(url.pathname==='/api/push/status')return reply(res,200,{ok:true,enabled:true});
    if(url.pathname==='/api/auth/sessions')return reply(res,200,{ok:true,items:[{id:'opaque-session-id',createdAt:1,expiresAt:9999999999,lastSeenAt:1,ip:'127.0.0.1',userAgent:'Chrome',current:true}]});
    if(url.pathname==='/api/channels')return reply(res,200,{ok:true,providers:[],primaryModel:''});
    if(url.pathname==='/api/settings')return reply(res,200,{ok:true,values:{},providerCount:1});
    if(url.pathname==='/api/system/version')return reply(res,200,{ok:true});
    if(url.pathname==='/api/rath/options')return reply(res,200,{ok:true,models:[],agents:[]});
    if(url.pathname.includes('/conversation-tree/'))return reply(res,200,{ok:true,rootFolders:[],items:[],recentItems:[],running:{items:[],recentItems:[]},folders:[],conversations:[],activeCount:1});
    return reply(res,200,{ok:true,items:[]});
  });}}]});
  const {server,base}=fixture;
  let browser, wss;
  t.after(async()=>{try {await browser?.close();for(const ws of sockets)ws.terminate();if(wss)await new Promise(r=>wss.close(r));} finally {await fixture.close();}});
  wss=new WebSocketServer({noServer:true});
  server.httpServer.on('upgrade',(req,socket,head)=>{
    const url=new URL(req.url,base);if(!url.pathname.startsWith('/api/'))return;
    const session=sessionFor(req);const context=url.searchParams.get('_ob_ctx');calls.push({path:url.pathname,method:'WS',context,cookie:session?.cookie});
    if(!session||context!==session.contextToken){socket.end('HTTP/1.1 409 Conflict\r\nConnection: close\r\n\r\n');return;}
    wss.handleUpgrade(req,socket,head,ws=>{ws.session=session;sockets.add(ws);forwarded.push({instance:session.instanceId,path:url.pathname});ws.send(JSON.stringify({type:'snapshot',epoch:'fixture',seq:1,includeArchived:false,items:[]}));ws.on('message',data=>{if(String(data)==='hello')ws.send('hello');});ws.on('close',()=>sockets.delete(ws));});
  });
  browser=await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  async function setup(t,user='family-a',width=390){const context=await browser.newContext({viewport:{width,height:844}});t.after(()=>context.close());const row=user?mint(user):null;if(row)await context.addCookies([{name:'openbear_gateway_session',value:row.cookie,url:base,httpOnly:true,sameSite:'Lax'}]);await context.addInitScript(user=>{if(user)localStorage.setItem('openbear.gateway.'+encodeURIComponent(user)+'.openbear.notifications.prompt.v1','dismissed');},user);const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(base+'/settings?section=sessions');await waitForBrowserReady(page,user?page.getByText('修改账号与密码',{exact:true}):page.getByLabel('账号',{exact:true}),errors);return {context,page,row,errors,async close(){await context.close();assert.deepEqual(errors,[]);}};}
  async function boundary(page){await page.locator('.session-boundary').waitFor();assert.equal(await page.getByText('修改账号与密码',{exact:true}).count(),0);}
  await t.test('bootstrap precedes all business calls; real main mounts gateway password/settings on HTTP and retains old modes', {timeout:60000}, async (t)=>{
    const h=await setup(t);await h.page.waitForFunction(async()=> (await import('/src/references/catalog.js')).referenceCatalog.connected);const own=calls.filter(c=>c.cookie===h.row.cookie);assert.ok(!calls.some(c=>c.path==='/api/auth/mode'));assert.equal(own[0].path,'/api/auth/session');assert.ok(own.slice(1).every(c=>c.context===h.row.contextToken));assert.ok(calls.some(c=>c.method==='WS'&&c.context===h.row.contextToken));assert.equal(await h.page.getByText('实例管理',{exact:true}).count(),0);assert.ok(await h.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await h.close();
    const login=await setup(t,null,320);await login.page.getByLabel('账号',{exact:true}).fill('family-a');await login.page.getByLabel('密码',{exact:true}).fill('fixture-password');assert.ok(await login.page.getByLabel('密码',{exact:true}).evaluate(el=>parseFloat(getComputedStyle(el).fontSize)>=16));await login.page.getByRole('button',{name:'登录',exact:true}).click();await login.page.waitForURL(url=>url.pathname==='/'||url.pathname==='/chat');await login.close();
    t.after(()=>{mode='gateway';});for(const old of ['controller','managed']){mode=old;const c=await browser.newContext();t.after(()=>c.close());const p=await c.newPage();await p.goto(base+'/login');await p.getByText(old==='controller'?'输入 Web Secret Key 后，需要在 Telegram 中二次确认。':'使用此实例的账号密码登录。').waitFor();await c.close();}mode='gateway';
  });
  await t.test('axios, native fetch, WS, Markdown images, attachment previews and download links carry the ORIGINAL binding over the wire', {timeout:60000}, async (t)=>{
    const h=await setup(t);
    const result=await h.page.evaluate(async({artifact,uuid})=>{
      const boundary=await import('/src/sessionBoundary.js'),{Api,conversationWsUrl}=await import('/src/api.js');
      await Api.conversations();const response=await boundary.contextFetch(artifact+'?text=1');const text=await response.text();
      const {renderMarkdown}=await import('/src/views/consoleView/markdown.js');const markup=renderMarkdown(`![test](${artifact})\n\n[download](${artifact}?download=1)`);
      const target=document.createElement('div');target.id='test-resources';target.innerHTML=markup;document.body.append(target);await Promise.all([...target.querySelectorAll('img')].map(img=>new Promise(r=>{if(img.complete)r();else{img.onload=r;img.onerror=r;}})));
      const ws=boundary.contextWebSocket(conversationWsUrl(uuid));await new Promise((resolve,reject)=>{ws.onopen=()=>{ws.send('hello');};ws.onmessage=e=>{if(e.data==='hello')resolve();};ws.onerror=reject;});ws.close();
      return {text};
    },{artifact,uuid});
    // URL rewriting itself (download links, Markdown, other origins, blobs): gateway.dom.test.mjs.
    assert.equal(result.text,'file-content');assert.ok(calls.some(c=>c.path===artifact&&c.context===h.row.contextToken));assert.ok(calls.some(c=>c.path===`/api/conversations/${uuid}/ws`&&c.context===h.row.contextToken));await h.close();
  });
  await t.test('new cookie cannot rebind an old page: reject before upstream, stop streams/WS, erase only its own drafts, no replay', {timeout:60000}, async (t)=>{
    const h=await setup(t,'family-a');
    await h.page.evaluate(async()=>{
      const b=await import('/src/sessionBoundary.js');b.userStorage(localStorage).setItem('openbear.console.drafts.v1','{"local:new":"A draft"}');b.userStorage(sessionStorage).setItem('panel','A');localStorage.setItem('controller-untouched','keep');localStorage.setItem('openbear.gateway.family-b.keep','B');
      const {createAttachmentDraftStorage}=await import('/src/views/consoleView/attachmentDraftStorage.js');await createAttachmentDraftStorage().save('local:new',[{id:'A-file',file:new File(['secret A'],'a.txt')}]);
      const response=await b.contextFetch('/api/slow-stream');window.__stream=response.text().then(()=>false,()=>true);
    });
    const replacement=mint('family-b');await h.context.addCookies([{name:'openbear_gateway_session',value:replacement.cookie,url:base,httpOnly:true,sameSite:'Lax'}]);const before=forwarded.length;
    await h.page.evaluate(async()=>{try{await(await import('/src/api.js')).Api.conversations();}catch{}});await boundary(h.page);assert.equal(forwarded.slice(before).filter(x=>x.path==='/api/conversations').length,0);
    const result=await h.page.evaluate(async()=>{const b=await import('/src/sessionBoundary.js');try{await(await import('/src/api.js')).Api.createConversation({title:'NEVER REPLAY'});}catch{};await b.initializePageSession();return {blocked:b.pageSession.blocked,id:b.pageSession.instanceId,stream:await window.__stream,keys:Object.keys(localStorage),sessionKeys:Object.keys(sessionStorage),other:localStorage.getItem('controller-untouched')};});
    assert.equal(result.blocked,true);assert.equal(result.id,'family-a');assert.equal(result.stream,true);assert.equal(result.other,'keep');assert.ok(!result.keys.some(x=>x.startsWith('openbear.gateway.family-a.')));assert.ok(!result.sessionKeys.some(x=>x.startsWith('openbear.gateway.family-a.')));assert.ok(result.keys.includes('openbear.gateway.family-b.keep'));assert.equal(forwarded.filter(x=>x.body?.title==='NEVER REPLAY').length,0);
    await h.page.waitForFunction(async()=>!(await indexedDB.databases()).some(x=>x.name==='openbear.console.attachmentDrafts.gateway.family-a'));
    const fresh=await h.context.newPage();await fresh.goto(base+'/settings?section=sessions');await fresh.getByText('修改账号与密码',{exact:true}).waitFor();assert.equal(await fresh.evaluate(async()=>{const b=await import('/src/sessionBoundary.js');return b.userStorage(localStorage).getItem('openbear.console.drafts.v1');}),null);await h.close();
  });
  await t.test('logout synchronizes tabs, password JSON stays compatible, old notifications cannot open another instance', {timeout:60000}, async (t)=>{
    const h=await setup(t,'family-a');const second=await h.context.newPage();await second.goto(base+'/settings?section=sessions');await second.getByText('修改账号与密码',{exact:true}).waitFor();
    await h.page.getByRole('button',{name:'退出本机',exact:true}).click();await h.page.getByRole('button',{name:'退出当前设备',exact:true}).click();await second.locator('.session-boundary').waitFor();await h.page.waitForURL(base+'/login');assert.ok(calls.some(c=>c.path==='/api/auth/logout'&&c.context===h.row.contextToken));await h.close();
    const change=await setup(t);await change.page.getByText('修改账号与密码',{exact:true}).click();for(const name of ['当前密码','新密码','确认新密码'])await change.page.getByLabel(name,{exact:true}).fill('fixture-password');await change.page.getByRole('button',{name:'修改并退出所有设备',exact:true}).click();await change.page.waitForURL(base+'/login');const req=calls.findLast(c=>c.path==='/api/auth/password/change');assert.deepEqual(req.body,{currentPassword:'fixture-password',password:'fixture-password'});await change.close();
    const other=await setup(t,'family-b');await other.page.goto(base+`/chat?id=${uuid}&_ob_instance=family-a`);await boundary(other.page);assert.equal(await other.page.evaluate(async()=> (await import('/src/sessionBoundary.js')).pageSession.blocked),true);await other.close();
  });
  await t.test('service worker retains the instance boundary for old and mismatched notifications', {timeout:60000}, async (t)=>{
    // The push client's requests and owner handover: gateway.dom.test.mjs.
    const h=await setup(t,'family-a');
    const workerEvent=h.context.waitForEvent('serviceworker');await h.page.evaluate(async()=>{const r=await navigator.serviceWorker.register('/openbear-push-sw.js',{scope:'/'});await navigator.serviceWorker.ready;r.active.postMessage({type:'openbear:push-boundary',gateway:true,instanceId:'family-a'});});const worker=await workerEvent;
    const result=await worker.evaluate(async()=>{
      for(let i=0;i<100&&(await boundaryState()).instanceId!=='family-a';i++)await new Promise(r=>setTimeout(r,10));
      const notices=[],opened=[];Object.defineProperty(self.registration,'showNotification',{value:async(title,options)=>notices.push({title,options})});Object.defineProperty(self.clients,'matchAll',{value:async()=>[]});Object.defineProperty(self.clients,'openWindow',{value:async url=>opened.push(url)});
      async function fire(type,fields){let done;const event=new Event(type);Object.assign(event,fields,{waitUntil:p=>{done=p;}});self.dispatchEvent(event);await done;}
      await fire('push',{data:{json:()=>({instanceId:'family-b',body:'B SECRET',conversationUuid:'b'})}});
      await fire('push',{data:{json:()=>({instanceId:'family-a',body:'A own task',conversationUuid:'a'})}});
      await fire('notificationclick',{notification:{close(){},data:notices[0].options.data}});
      await fire('notificationclick',{notification:{close(){},data:{url:'/chat?id=legacy'}}});
      return {notices,opened,state:await boundaryState()};
    });
    assert.equal(result.state.instanceId,'family-a');assert.ok(!result.notices[0].options.body.includes('B SECRET'));assert.equal(result.notices[1].options.body,'A own task');assert.equal(new URL(result.opened[0]).searchParams.get('_ob_instance'),'family-b');assert.equal(new URL(result.opened[1]).pathname,'/login');await h.close();
  });
  await t.test('session/network failure does not start business or silently downgrade gateway login', {timeout:60000}, async (t)=>{
    t.after(()=>{modeFailure=false;});modeFailure=true;const context=await browser.newContext();t.after(()=>context.close());const p=await context.newPage();const before=calls.length;await p.goto(base+'/login');await p.getByRole('button',{name:'重试连接',exact:true}).waitFor();assert.equal(await p.getByText('Telegram',{exact:false}).count(),0);assert.deepEqual(calls.slice(before).map(c=>c.path),['/api/auth/session']);modeFailure=false;await p.getByRole('button',{name:'重试连接',exact:true}).click();await p.getByLabel('账号',{exact:true}).waitFor();await context.close();
  });
});
