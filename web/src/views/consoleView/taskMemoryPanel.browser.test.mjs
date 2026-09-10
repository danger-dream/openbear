import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile, mkdir} from 'node:fs/promises';
import {chromium} from 'playwright-core';
import postcss from 'postcss';
import tailwindcss from 'tailwindcss';
import {browserExecutable, componentBundle, webRoot} from '../../../test-support/realBrowser.mjs';

const executablePath = browserExecutable();
const body = '## 发布约束\n\n保留 **配置和历史**，不重启其他服务。\n\n```js\nconst literal = "<not-html>";\n' + 'x'.repeat(500) + '\n```\n\n' + '长正文只在阅读区滚动。\n\n'.repeat(55);

test('memory panel supports readable scopes, lazy reading, safe editing, search, pagination and responsive layouts', {skip: !executablePath, timeout: 120000}, async t => {
  const bundle = await componentBundle(`
    import {createApp, h, ref, provide, nextTick} from 'vue';
    import ElementPlus from 'element-plus';
    import Drawer from './src/views/consoleView/TaskMemoryDrawer.vue';
    import {Api} from './src/api.js';
    import {TASK_MEMORY_CHANGED_EVENT_KEY} from './src/views/consoleView/taskMemoryUiState.js';
    const conversation = ref('conversation-fixture'), changed = ref(null), calls = [];
    let heldDetail, holdDetail = false, heldSave, holdSave = false, queryHold, heldQuery = null;
    const tasks = [
      {taskUuid:'task-internal-aaa',taskShortId:'internal',name:'研究助手',title:'界面优化与交互检查',status:'completed',updatedAt:1788970000},
      {taskUuid:'task-internal-bbb',taskShortId:'internal',name:'研究助手',title:'发布检查与验收',status:'running',updatedAt:1788970100},
    ];
    function make(i) { return {memoryUuid:'mem_internal_'+i,name:i===0?'发布约束':'工作记录 '+i,description:'记录重要决定与验收结果。',body:${JSON.stringify(body)},revision:1,autoReinjectCatalog:true,visibleToAgents:false,createdBy:'main-controller',sourceTurnUuid:'turn-internal-uuid',sourceRunUuid:'run-internal-uuid',sizeBytes:4500,updatedAt:1788970000,deletedAt:i>=3?1788970020:0}; }
    const rows = Array.from({length:63},(_,i)=>make(i));
    const privateRows = {'task-internal-aaa':[{...make(100),name:'界面检查结论',deletedAt:0}], 'task-internal-bbb':[{...make(101),name:'发布验收结论',deletedAt:0}]};
    const scope = p => p?.scopeType==='agent_task' ? (privateRows[p.taskUuid]||[]) : rows;
    const metadata = ({body,...item}) => ({...item});
    const error = status => Object.assign(new Error('fixture'),{response:{status,data:{error:'fixture'}}});
    Api.taskMemories = async (uuid,p={}) => {
      calls.push({kind:'list',uuid,...p});
      const all=scope(p),q=String(p.query||'');
      const selected=all.filter(i=>(p.includeDeleted||!i.deletedAt)&&(!q||(i.name+' '+i.description+' '+i.body).includes(q)));
      const data={items:selected.slice(p.offset||0,(p.offset||0)+(p.limit||50)).map(metadata),total:selected.length,activeTotal:all.filter(i=>!i.deletedAt).length};
      if(queryHold===q){queryHold=null;await new Promise(r=>heldQuery=r);}
      return data;
    };
    Api.taskMemoryTasks = async uuid => ({tasks});
    Api.taskMemoryPreview = async () => ({catalogXml:'<conversation-memory><memory id="mem_internal_0">实际目录原文</memory></conversation-memory>',estimatedRuntimeTokens:215,maxRuntimeTokens:1500});
    Api.taskMemory = async (uuid,id,p) => {
      calls.push({kind:'detail',uuid,id,...p});
      const row=scope(p).find(i=>i.memoryUuid===id);if(!row)throw error(404);
      const data={memory:{...row}};
      if(holdDetail){holdDetail=false;await new Promise(r=>heldDetail=r);}
      return data;
    };
    Api.createTaskMemory = async (uuid,p) => {calls.push({kind:'create',uuid,...p});const row={...make(999),...p,memoryUuid:'mem_created',deletedAt:0};scope(p).unshift(row);return {memory:{...row}};};
    Api.updateTaskMemory = async (uuid,id,p) => {
      calls.push({kind:'update',uuid,id,...p});const row=scope(p).find(i=>i.memoryUuid===id);
      if(!row)throw error(404);if(row.revision!==p.revision)throw error(409);
      Object.assign(row,p,{revision:row.revision+1});
      changed.value={type:'task_memory.changed',conversationUuid:uuid,scopeType:p.scopeType,taskUuid:p.taskUuid||'',memoryUuid:id,action:'update',revision:row.revision};
      if(holdSave){holdSave=false;await new Promise(r=>heldSave=r);}
      return {memory:{...row}};
    };
    Api.deleteTaskMemory = async (uuid,id,p) => {calls.push({kind:'delete',uuid,id,...p});const row=scope(p).find(i=>i.memoryUuid===id);if(row.revision!==p.revision)throw error(409);row.deletedAt=1788971000;row.revision++;return {memory:{...row}};};
    Api.restoreTaskMemory = async (uuid,id,p) => {calls.push({kind:'restore',uuid,id,...p});const row=scope(p).find(i=>i.memoryUuid===id);if(row.revision!==p.revision)throw error(409);row.deletedAt=0;row.revision++;return {memory:{...row}};};
    createApp({setup(){provide(TASK_MEMORY_CHANGED_EVENT_KEY,changed);return ()=>h(Drawer,{conversationUuid:conversation.value});}}).use(ElementPlus).mount('#app');
    window.memoryFixture={calls,rows,tasks,async switchConversation(uuid){conversation.value=uuid;await nextTick();},holdDetail(){holdDetail=true;},releaseDetail(){heldDetail?.();},holdSave(){holdSave=true;},releaseSave(){heldSave?.();},holdQuery(q){queryHold=q;},releaseQuery(){heldQuery?.();},async change(){rows[0].revision++;changed.value={type:'task_memory.changed',conversationUuid:conversation.value,scopeType:'conversation',taskUuid:'',memoryUuid:rows[0].memoryUuid,action:'update',revision:rows[0].revision};await nextTick();}};
  `);
  const baseCss = (await postcss([tailwindcss(webRoot+'/tailwind.config.js')]).process(await readFile(webRoot+'/src/style.css','utf8'),{from:webRoot+'/src/style.css'})).css;
  const css = (await Promise.all(['node_modules/element-plus/dist/index.css','node_modules/element-plus/theme-chalk/dark/css-vars.css'].map(p=>readFile(webRoot+'/'+p,'utf8')))).join('\n') + '\n' + baseCss + '\n' + await readFile(webRoot+'/src/dark-theme.css','utf8');
  const server = createServer((req,res)=>{
    if(req.url==='/bundle.js'){res.writeHead(200,{'Content-Type':'text/javascript'});return res.end(bundle);}
    if(req.url==='/style.css'){res.writeHead(200,{'Content-Type':'text/css'});return res.end(css);}
    res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/style.css"></head><body><div id="app"></div><script src="/bundle.js"></script></body></html>');
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  let browser;
  t.after(async()=>{await browser?.close();server.closeAllConnections();await new Promise(r=>server.close(r));});
  browser = await chromium.launch({executablePath,headless:true,args:['--no-sandbox']});
  const page = await browser.newPage({viewport:{width:1280,height:900}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.setDefaultTimeout(7000);
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.locator('.task-memory-entry').click();
  const drawer=page.locator('.task-memory-drawer'), editor=page.locator('.task-memory-editor');
  const settle = () => page.evaluate(async () => { await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))); await Promise.all(document.getAnimations().filter(a=>Number.isFinite(a.effect?.getComputedTiming().endTime)).map(a=>a.finished.catch(()=>{}))); });
  await drawer.getByRole('button',{name:'查看 发布约束',exact:true}).waitFor();
  assert.doesNotMatch(await drawer.innerText(), /mem_internal|turn-internal|run-internal|TASK MEMORY|安全模型边界/);
  assert.equal(await page.evaluate(()=>memoryFixture.calls.filter(c=>c.kind==='detail').length),0,'listing never reads bodies');
  assert.equal(await drawer.locator('.preview-content').count(),0,'raw catalog is collapsed');
  await drawer.locator('.preview-toggle').click();
  assert.match(await drawer.locator('.preview-content pre').innerText(), /mem_internal_0/,'actual server catalog remains available');
  await drawer.locator('.preview-toggle').click();
  await drawer.getByRole('button',{name:'查看 发布约束',exact:true}).click();
  await drawer.locator('.memory-body h2').waitFor();
  assert.equal(await editor.count(),0,'read does not open editor');
  assert.equal(await drawer.locator('.memory-body h2').innerText(),'发布约束');
  assert.doesNotMatch(await drawer.innerText(),/mem_internal|turn-internal|run-internal/);
  await drawer.locator('.memory-technical summary').click();
  assert.match(await drawer.locator('.memory-technical').innerText(),/mem_internal_0/);
  await drawer.locator('.memory-technical summary').click();
  await drawer.getByRole('button',{name:'编辑',exact:true}).click();
  await editor.locator('#task-memory-name').fill('未保存的名字');
  await page.keyboard.press('Escape');
  await page.getByRole('button',{name:'继续编辑',exact:true}).click();
  assert.equal(await editor.locator('#task-memory-name').inputValue(),'未保存的名字');
  await editor.getByRole('button',{name:'取消',exact:true}).click();
  await page.getByRole('button',{name:'放弃修改',exact:true}).click();
  await editor.waitFor({state:'hidden'});
  assert.equal(await page.evaluate(()=>memoryFixture.calls.filter(c=>c.kind==='update').length),0);
  await drawer.getByRole('button',{name:'编辑',exact:true}).click();
  await editor.locator('#task-memory-name').fill('发布前检查');
  await editor.locator('#task-memory-body').fill('## 新正文\n\n保持真实内容。');
  await page.evaluate(()=>memoryFixture.holdSave());
  await editor.getByRole('button',{name:'保存',exact:true}).click();
  await page.waitForFunction(()=>memoryFixture.calls.some(c=>c.kind==='update'));
  await page.evaluate(()=>memoryFixture.releaseSave());
  await editor.waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.querySelector('.memory-detail h3')?.textContent==='发布前检查');
  const saved=await page.evaluate(()=>memoryFixture.calls.find(c=>c.kind==='update'));
  assert.equal(saved.revision,1);assert.equal(saved.scopeType,'conversation');assert.equal(saved.visibleToAgents,false);
  assert.equal(saved.body,'## 新正文\n\n保持真实内容。');
  await drawer.getByRole('button',{name:'返回列表',exact:true}).click();
  const search=drawer.getByRole('textbox',{name:'搜索任务记忆'});
  await search.fill('不存在的词');
  await drawer.getByText('没有找到匹配的记忆',{exact:true}).waitFor();
  await search.fill('发布前');
  await drawer.getByRole('button',{name:'查看 发布前检查',exact:true}).waitFor();
  assert.equal(await drawer.locator('.memory-row').count(),1);
  await page.evaluate(()=>memoryFixture.holdQuery('工作记录'));
  await search.fill('工作记录');
  await page.waitForFunction(()=>memoryFixture.calls.some(c=>c.kind==='list'&&c.query==='工作记录'));
  await search.fill('发布前');
  await page.evaluate(()=>memoryFixture.releaseQuery());
  await drawer.getByRole('button',{name:'查看 发布前检查',exact:true}).waitFor();
  assert.equal(await drawer.locator('.memory-row').count(),1,'late query does not replace current matches');
  await search.fill('');
  await page.waitForFunction(()=>document.querySelectorAll('.memory-row').length===3);
  await drawer.getByRole('button',{name:'删除 发布前检查',exact:true}).click();
  await page.locator('.task-memory-confirm').getByRole('button',{name:'取消',exact:true}).click();
  assert.equal(await page.evaluate(()=>memoryFixture.calls.filter(c=>c.kind==='delete').length),0);
  await drawer.getByRole('button',{name:'删除 发布前检查',exact:true}).click();
  await page.locator('.task-memory-confirm').getByRole('button',{name:'删除',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.memory-row').length===2);
  await drawer.locator('label[for="task-memory-show-deleted"]').click();
  await drawer.locator('.memory-pagination').waitFor();
  await drawer.getByRole('button',{name:'恢复 发布前检查',exact:true}).click();
  await page.waitForFunction(()=>memoryFixture.rows[0].deletedAt===0);
  await drawer.locator('.btn-next').click();
  await drawer.getByRole('button',{name:'查看 工作记录 50',exact:true}).waitFor();
  assert.equal(await drawer.locator('.memory-row').count(),13,'deleted history beyond first 50 is reachable');
  await search.fill('发布前');
  await drawer.getByRole('button',{name:'查看 发布前检查',exact:true}).waitFor();
  assert.equal(await drawer.locator('.memory-row').count(),1,'search resets pagination');
  await drawer.getByRole('tab',{name:'Agent 记忆',exact:true}).click();
  await drawer.getByRole('button',{name:'查看 界面检查结论',exact:true}).waitFor();
  assert.doesNotMatch(await drawer.innerText(),/task-internal|internal/);
  await drawer.locator('.el-select__wrapper').click();
  await drawer.getByRole('combobox').fill('发布检查');
  await page.locator('.task-memory-task-select-popper').getByText('发布检查与验收',{exact:true}).click();
  await drawer.getByRole('button',{name:'查看 发布验收结论',exact:true}).waitFor();
  assert.ok((await drawer.locator('.el-select__selected-item').allTextContents()).join('').includes('发布检查与验收'));
  assert.ok(await page.evaluate(()=>memoryFixture.calls.some(c=>c.kind==='list'&&c.taskUuid==='task-internal-bbb')));
  await drawer.getByRole('button',{name:'新增',exact:true}).click();
  assert.equal(await editor.locator('#task-memory-visible-agents').count(),0,'Agent memory never exposes conversation sharing switch');
  await editor.locator('#task-memory-name').fill('新增 Agent 记忆');
  await editor.locator('#task-memory-body').fill('私有任务内容');
  await editor.getByRole('button',{name:'保存',exact:true}).click();
  await editor.waitFor({state:'hidden'});
  assert.equal(await page.evaluate(()=>memoryFixture.calls.find(c=>c.kind==='create').taskUuid),'task-internal-bbb');
  assert.equal(await page.evaluate(()=>Object.hasOwn(memoryFixture.calls.find(c=>c.kind==='create'),'visibleToAgents')),false);
  await drawer.getByRole('button',{name:'返回列表',exact:true}).click();
  await page.evaluate(()=>memoryFixture.holdDetail());
  await drawer.getByRole('button',{name:'查看 发布验收结论',exact:true}).click();
  await drawer.getByRole('tab',{name:'会话记忆',exact:true}).click();
  await page.evaluate(()=>memoryFixture.releaseDetail());
  await drawer.getByRole('button',{name:'查看 发布前检查',exact:true}).waitFor();
  assert.equal(await drawer.locator('.memory-detail').count(),0,'late detail cannot reopen another scope');

  for(const dark of [false,true]) for(const viewport of [{width:1280,height:900},{width:390,height:844},{width:320,height:430}]) {
    await page.setViewportSize(viewport);
    await page.evaluate(d=>document.documentElement.classList.toggle('dark',d),dark);
    for(const tab of ['会话记忆','Agent 记忆']) {
      await drawer.getByRole('tab',{name:tab,exact:true}).click();
      await drawer.locator('.memory-row-main').first().waitFor();
      await settle();
      const geometry=await drawer.evaluate(el=>{const list=el.querySelector('.memory-list').getBoundingClientRect(),rect=el.getBoundingClientRect();return {width:el.scrollWidth,height:el.scrollHeight,clientHeight:el.clientHeight,listHeight:list.height,bottom:rect.bottom,closeTop:el.querySelector('.drawer-close').getBoundingClientRect().top};});
      assert.ok(geometry.width<=viewport.width+1,JSON.stringify({tab,viewport,dark,geometry}));
      assert.ok(geometry.height<=geometry.clientHeight+1,JSON.stringify({tab,viewport,dark,geometry}));
      assert.ok(geometry.listHeight>=75,JSON.stringify({tab,viewport,dark,geometry}));
      await drawer.locator('.memory-row-main').last().click();
      await drawer.locator('.memory-body').waitFor();
      assert.equal(await drawer.locator('.memory-detail h3').evaluate(el=>getComputedStyle(el).color),dark?'rgb(228, 228, 231)':'rgb(24, 24, 27)');
      const toolbarBefore=await drawer.locator('.memory-detail-toolbar').boundingBox();
      await drawer.locator('.memory-detail-scroll').evaluate(el=>el.scrollTop=el.scrollHeight);
      assert.deepEqual(await drawer.locator('.memory-detail-toolbar').boundingBox(),toolbarBefore);
      assert.ok(await drawer.evaluate(el=>el.scrollWidth<=innerWidth+1),'long code never widens drawer');
      await drawer.getByRole('button',{name:'编辑',exact:true}).click();
      await editor.locator('#task-memory-name').waitFor();
      await settle();
      const visual = await page.evaluate(() => {
        const read = selector => { const s=getComputedStyle(document.querySelector(selector)); return {font:s.fontFamily,size:s.fontSize,background:s.backgroundColor,color:s.color,height:s.height}; };
        return {body:read('body'),row:read('.memory-row-main'),heading:read('.memory-drawer-header h2'),search:read('.memory-toolbar input'),name:read('#task-memory-name'),text:read('#task-memory-body'),primary:read('.memory-detail-toolbar .primary-action'),save:read('.task-memory-editor .el-button--primary'),cancel:read('.task-memory-editor .el-button:not(.el-button--primary)')};
      });
      for (const key of ['row','heading','search','name','text','primary','save','cancel']) assert.equal(visual[key].font,visual.body.font,`${key} uses application font: ${JSON.stringify(visual)}`);
      assert.equal(visual.primary.background,visual.save.background,`primary actions share the application palette: ${JSON.stringify(visual)}`);
      assert.equal(visual.primary.size,visual.save.size,'primary action typography is consistent');
      const footer=await editor.locator('.el-dialog__footer').boundingBox();
      assert.ok(footer.y+footer.height<=viewport.height+1,'save controls inside viewport');
      await editor.locator('.el-dialog__body').evaluate(el=>el.scrollTop=el.scrollHeight);
      assert.deepEqual(await editor.locator('.el-dialog__footer').boundingBox(),footer);
      await editor.getByRole('button',{name:'取消',exact:true}).click();
      await editor.waitFor({state:'hidden'});
      await drawer.getByRole('button',{name:'返回列表',exact:true}).click();
      if(process.env.MEMORY_PANEL_SCREENSHOTS && viewport.width!==320) {
        await mkdir(process.env.MEMORY_PANEL_SCREENSHOTS,{recursive:true});
        await page.screenshot({path:process.env.MEMORY_PANEL_SCREENSHOTS+'/'+(dark?'dark':'light')+'-'+viewport.width+'-'+(tab==='会话记忆'?'conversation':'agent')+'.png'});
      }
    }
  }
  await page.evaluate(()=>memoryFixture.switchConversation('conversation-other'));
  await drawer.waitFor({state:'hidden'});
  assert.deepEqual(errors,[]);
});
