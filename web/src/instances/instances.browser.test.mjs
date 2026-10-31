import test from 'node:test';
import assert from 'node:assert/strict';
import {startBrowserTestServer, waitForBrowserReady} from '../browserTestServer.mjs';
import {createInstancesBackend} from './instancesFixture.mjs';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}
const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:instance-test"></script></body></html>';
const entry = `import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import '/src/theme.js'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css';
const view = window.__view;
if (['settings','sessions'].includes(view)) await (await import('/src/authMode.js')).loadAuthMode();
const component = view === 'login' || location.pathname === '/login' ? (await import('/src/views/LoginView.vue')).default : view === 'app' ? (await import('/src/App.vue')).default : view === 'settings' ? (await import('/src/views/SettingsHubView.vue')).default : view === 'sessions' ? (await import('/src/views/SessionsView.vue')).default : (await import('/src/views/InstancesView.vue')).default;
window.__opened=[]; window.__folders=[]; const app=createApp(component, {section:'system-settings','onOpen-conversation': id => window.__opened.push(id),'onOpen-folder': id => window.__folders.push(id)}); app.use(ElementPlus); for (const [key,icon] of Object.entries(Icons)) app.component(key,icon); app.mount('#app');`;

// Layout smoke of the instance screens in real Chromium: no sideways scroll on desktop, phone and
// dark, panels and menus inside the screen, finger-sized rows, 16px inputs (no focus zoom).
// Requests, texts, counts and navigation of the same flows: instances.dom.test.mjs.
test('instance frontend real layout on desktop, phone and dark', {skip: !chromium, timeout: 180000}, async t => {
  let pageMode='controller';
  const serverFixture = await startBrowserTestServer(t,{name:'instances',authMode:()=>pageMode,entries:['virtual:instance-test','/src/App.vue','/src/views/InstancesView.vue','/src/views/SettingsHubView.vue'],plugins:[{name:'instance-test-entry',resolveId(id){if(id === 'virtual:instance-test') return '\0virtual:instance-test';}, load(id){if(id === '\0virtual:instance-test') return entry;},configureServer(server){server.middlewares.use((req,res,next)=>{if(['/__test','/login','/instances','/chat'].includes(req.url?.split('?')[0])){res.setHeader('content-type','text/html');res.end(html.replace('<head>',`<head><script>window.__OPENBEAR_AUTH_MODE__=${JSON.stringify(pageMode)};</script>`));}else next();});}}]});
  const {base}=serverFixture;
  let browser;
  t.after(async () => {try {await browser?.close();} finally {await serverFixture.close();}});
  browser = await chromium.launch({executablePath:process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  async function setup(t,{view='instances',mode='controller',width=390,...options}={}) {
    pageMode=mode;
    const context = await browser.newContext({viewport:{width,height:844},deviceScaleFactor:1});
    t.after(()=>context.close());
    const page = await context.newPage(), errors = [], backend = createInstancesBackend({mode,...options});
    await page.addInitScript(view => {window.__view=view;}, view);
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', async route => {
      const req=route.request(), url=new URL(req.url());
      const {status,json}=backend.handle({path:url.pathname,method:req.method(),body:req.postDataJSON(),query:Object.fromEntries(url.searchParams)});
      await route.fulfill({status,contentType:'application/json',body:JSON.stringify(json)});
    });
    const goto = async (path=view==='login'?'/login':view==='app'?'/instances':'/__test') => {
      await page.goto(base + path);
      await waitForBrowserReady(page,page.locator('#app > *').first(),errors);
    };
    const close = async () => {await context.close();assert.deepEqual(errors,[]);};
    return {page,goto,close};
  }
  const click = (page,name) => page.getByRole('button',{name,exact:true}).click();
  async function pickServer(page,name) {
    await page.getByRole('button',{name:/^运行位置：/}).click();
    await page.locator('.ss-option').filter({has:page.locator('.ss-name',{hasText:name})}).click();
  }
  const themed = (page,dark) => page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
  // No sideways scroll, in light and in dark.
  async function noOverflow(page,label='') {
    for (const dark of [false,true]) { await themed(page,dark); assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1),`${label} ${dark?'dark':'light'}`); }
    await themed(page,false);
  }
  const fontAtLeast16 = locator => locator.evaluate(el=>parseFloat(getComputedStyle(el).fontSize)>=16);
  const inside = (box,width) => box.left>=0&&box.right<=width+1;

  await t.test('phone 320: managed login, create form, blocked migration note and trust button', {timeout:60000}, async t => {
    const login=await setup(t,{view:'login',mode:'managed',width:320});await login.goto();
    assert.ok(await fontAtLeast16(login.page.getByLabel('密码',{exact:true})),'320px password input avoids focus zoom');
    await login.page.getByLabel('账号',{exact:true}).waitFor();
    await noOverflow(login.page,'login');await login.close();
    const account=await setup(t,{view:'sessions',mode:'managed',width:320});await account.goto();await account.page.locator('.sessions-content').waitFor();
    await account.page.getByText('修改账号与密码',{exact:true}).click();await account.page.getByLabel('确认新密码',{exact:true}).waitFor();
    await noOverflow(account.page,'account');await account.close();
    const h=await setup(t,{width:320,itemPatch:{authMode:'gateway',networkMigration:{pending:false,blockers:['node_upgrade_required','server_mesh_required']}},observed:{peer:'10.0.0.5',header:'X-Forwarded-For',atMs:Date.now()-60000,trusted:false}});await h.goto();
    await click(h.page,'创建实例');
    const dialog=h.page.getByRole('dialog');await dialog.waitFor();
    assert.ok(await dialog.evaluate(el=>el.getBoundingClientRect().right<=innerWidth+1),'create dialog inside the phone');
    assert.ok(await fontAtLeast16(h.page.getByLabel('实例名称')));
    await noOverflow(h.page,'create');
    await h.page.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
    await h.page.locator('.instance-row').click();
    const note=h.page.getByRole('note');await note.waitFor();
    const card=await note.evaluate(el=>({note:el.getBoundingClientRect().right,card:el.closest('.ix-card').getBoundingClientRect().right}));
    assert.ok(card.note<=card.card,JSON.stringify(card));
    await noOverflow(h.page,'blocked migration');
    if(process.env.OPENBEAR_INSTANCE_SHOTS)await h.page.screenshot({path:`${process.env.OPENBEAR_INSTANCE_SHOTS}/migration-blocked-320.png`,fullPage:true});
    await h.goto();
    await h.page.locator('.ob-strip').getByRole('button',{name:'设置',exact:true}).click();
    const gateway=h.page.getByRole('dialog',{name:'访问网关'});await gateway.waitFor();
    const trust=gateway.getByRole('button',{name:'信任这个代理',exact:true});
    assert.ok((await trust.boundingBox()).height>=30);
    await noOverflow(h.page,'gateway settings');
    await h.close();
  });

  await t.test('phone 390: list, empty gateway form, settings, picker and More menu fit', {timeout:60000}, async t => {
    const settings=await setup(t,{view:'settings',width:390});await settings.goto();await settings.page.getByText('最大轮次',{exact:true}).waitFor();
    await noOverflow(settings.page,'settings');await settings.close();
    const empty=await setup(t,{gatewayOn:false,instances:false,width:390});await empty.goto();
    await click(empty.page,'启用访问网关');
    await empty.page.getByLabel('访问地址').fill('https://bear.example.com');
    await empty.page.getByText('请自行把 bear.example.com 映射到',{exact:false}).waitFor();
    await noOverflow(empty.page,'enable gateway');await empty.close();
    const h=await setup(t,{width:390,itemPatch:{authMode:'gateway',networkMigration:{pending:false,blockers:[]}}});await h.goto();
    await h.page.locator('.instance-row').waitFor();
    await noOverflow(h.page,'list');
    await click(h.page,'创建实例');
    await h.page.getByRole('button',{name:/^运行位置：/}).click();
    const panel=h.page.locator('.ss-panel');await panel.waitFor();
    assert.ok(inside(await panel.evaluate(el=>el.closest('.reference-floating-panel').getBoundingClientRect().toJSON()),390),'server picker inside the phone');
    assert.ok(await panel.locator('.ss-option').first().evaluate(el=>el.getBoundingClientRect().height>=42),'finger-sized server rows');
    if(process.env.OPENBEAR_INSTANCE_SHOTS){await h.page.waitForTimeout(300);await h.page.screenshot({path:`${process.env.OPENBEAR_INSTANCE_SHOTS}/server-picker-390.png`});}
    await h.goto();
    await h.page.locator('.instance-row').click();
    await h.page.getByRole('button',{name:'更多操作',exact:true}).click();
    const menu=h.page.locator('.ix-more-menu');await menu.getByRole('menuitem',{name:'迁移网络…'}).waitFor();
    // Measured once the opening zoom has finished.
    await h.page.waitForFunction(()=>!document.querySelector('.ix-more-menu[class*="-enter-active"]'));
    for(const b of await menu.getByRole('menuitem').evaluateAll(els=>els.map(el=>el.getBoundingClientRect().toJSON()))){assert.ok(inside(b,390),JSON.stringify(b));assert.ok(b.height>=44,JSON.stringify(b));}
    if(process.env.OPENBEAR_INSTANCE_SHOTS){await h.page.waitForTimeout(300);await h.page.screenshot({path:`${process.env.OPENBEAR_INSTANCE_SHOTS}/more-menu-390.png`});}
    await menu.getByRole('menuitem',{name:'更换密钥…'}).click();
    await h.page.locator('.el-message-box').waitFor();
    await noOverflow(h.page,'confirm box');
    await h.close();
  });

  await t.test('desktop 1280: App shell, add-server radios, picker and More menu rows', {timeout:60000}, async t => {
    const app=await setup(t,{view:'app',width:1280});await app.goto();
    await app.page.locator('.instances-page').waitFor();
    await noOverflow(app.page,'app');await app.close();
    const h=await setup(t,{width:1280,itemPatch:{authMode:'gateway',networkMigration:{pending:false,blockers:[]}}});await h.goto();
    await click(h.page,'创建实例');
    await h.page.getByRole('button',{name:/^运行位置：/}).click();
    const panel=h.page.locator('.ss-panel');await panel.waitFor();
    assert.ok(inside(await panel.evaluate(el=>el.closest('.reference-floating-panel').getBoundingClientRect().toJSON()),1280));
    if(process.env.OPENBEAR_INSTANCE_SHOTS){await h.page.waitForTimeout(300);await h.page.screenshot({path:`${process.env.OPENBEAR_INSTANCE_SHOTS}/server-picker-1280.png`});}
    await panel.locator('.ss-option').filter({has:h.page.locator('.ss-name',{hasText:'添加服务器…'})}).click();
    const server=h.page.getByRole('dialog',{name:'添加服务器'});await server.waitFor();
    // Each auth option stays on one line with a checkbox-sized radio.
    const radios=await server.locator('.ob-radios label').evaluateAll(labels=>labels.map(label=>({input:Math.round(label.querySelector('input').getBoundingClientRect().width),height:Math.round(label.getBoundingClientRect().height)})));
    assert.equal(radios.length,3);for(const r of radios){assert.equal(r.input,16,JSON.stringify(radios));assert.ok(r.height<=24,JSON.stringify(radios));}
    await noOverflow(h.page,'add server');
    await h.goto();
    await h.page.locator('.instance-row').click();
    await h.page.getByText('🇯🇵云服务器 · 端口 18970',{exact:true}).waitFor();
    await noOverflow(h.page,'detail');
    await h.page.getByRole('button',{name:'更多操作',exact:true}).click();
    const menu=h.page.locator('.ix-more-menu');await menu.getByRole('menuitem',{name:'迁移网络…'}).waitFor();
    await h.page.waitForFunction(()=>!document.querySelector('.ix-more-menu[class*="-enter-active"]'));
    // Desktop keeps the stock menu rows.
    for(const b of await menu.getByRole('menuitem').evaluateAll(els=>els.map(el=>el.getBoundingClientRect().toJSON()))){assert.ok(inside(b,1280),JSON.stringify(b));assert.ok(b.height<40,JSON.stringify(b));}
    if(process.env.OPENBEAR_INSTANCE_SHOTS){await h.page.waitForTimeout(300);await h.page.screenshot({path:`${process.env.OPENBEAR_INSTANCE_SHOTS}/more-menu-1280.png`});}
    await h.close();
  });
});
