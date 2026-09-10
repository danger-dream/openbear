import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdir, readFile} from 'node:fs/promises';
import {chromium} from 'playwright-core';
import postcss from 'postcss';
import tailwindcss from 'tailwindcss';
import tailwindConfig from '../../tailwind.config.js';
import {browserExecutable, componentBundle, webRoot} from '../../test-support/realBrowser.mjs';

const executablePath = browserExecutable();
const screenshots = process.env.FOLDER_DEFAULTS_SCREENSHOTS || '';

test('folder defaults preserve sparse semantics, stale-dialog safety and readable responsive operation', {skip: !executablePath, timeout: 120000}, async t => {
  const bundle = await componentBundle(`
    import {createApp} from 'vue';
    import ElementPlus from 'element-plus';
    import Tree from './src/components/ConversationTree.vue';
    import {Api} from './src/api.js';
    const calls = [], events = [];
    const folders = [
      {kind:'folder',id:'folder-a',folderId:'folder-a',parentId:'',name:'目录 A',path:'目录 A',conversationCount:0,childFolderCount:0},
      {kind:'folder',id:'folder-b',folderId:'folder-b',parentId:'parent-b',name:'目录 B',path:'项目 / 目录 B',conversationCount:0,childFolderCount:0},
    ];
    const models = [
      {key:'model-fast',name:'GPT-6 Astra',provider:'provider-x',thinkingLevels:['off','medium','high'],defaultThinkingLevel:'medium',supportsFast:true},
      {key:'model-slow',name:'GPT-5.6 Sol',provider:'provider-y',thinkingLevels:['low'],defaultThinkingLevel:'low',supportsFast:false},
      {key:'model-plain',name:'无思考模型',provider:'provider-y',thinkingLevels:[],defaultThinkingLevel:'',supportsFast:false},
    ];
    let releaseA, impactCount = 0, failOptions = false;
    const state = {
      workspaceDir:'', promptMarkdown:'',
      local:{mainModel:'model-slow'},
      inherited:{mainModel:'model-fast',mainThinkingLevel:'high',mainFastMode:true,agentModel:'model-slow',agentThinkLevel:'high',agentFastMode:true},
      fallback:{mainModel:'model-fast',mainThinkingLevel:'medium',mainFastMode:false,agentModel:'',agentThinkLevel:'',agentFastMode:null},
    };
    const clone = value => value===undefined?undefined:JSON.parse(JSON.stringify(value));
    const model = key => models.find(item=>item.key===key);
    function resolvedDefaults() {
      const selected={...state.fallback};
      for(const layer of [state.inherited,state.local]) for(const [key,value] of Object.entries(layer)) {
        if(['mainModel','agentModel'].includes(key)&&value&&!model(value))continue;
        selected[key]=value;
      }
      const main=model(selected.mainModel),mainLevels=main?.thinkingLevels||[];
      const mainThinkingLevel=mainLevels.length?(mainLevels.includes(selected.mainThinkingLevel)?selected.mainThinkingLevel:main.defaultThinkingLevel):'off';
      const mainFastMode=selected.mainFastMode===true&&Boolean(main?.supportsFast);
      const agentModel=selected.agentModel||'',agent=model(agentModel||selected.mainModel),agentLevels=agent?.thinkingLevels||[];
      const agentThinkLevel=selected.agentThinkLevel&&agentLevels.includes(selected.agentThinkLevel)?selected.agentThinkLevel:'';
      const agentFastMode=selected.agentFastMode===false?false:selected.agentFastMode===true&&agent?.supportsFast?true:null;
      return {mainModel:selected.mainModel,mainThinkingLevel,mainFastMode,agentModel,agentThinkLevel,agentFastMode};
    }
    function properties(id) {
      return {name:id==='__temporary'?'临时会话':id==='folder-a'?'目录 A':'目录 B',path:id==='__temporary'?'临时会话':id==='folder-a'?'目录 A':'项目 / 目录 B',
        workspace:{local:state.workspaceDir,effective:state.workspaceDir||'/srv/inherited',sourcePath:state.workspaceDir?'项目 / 目录 B':'项目'},
        prompt:{local:state.promptMarkdown,effective:'## 来自父目录的真实提示词\\n\\n保留旧预览。',sourcePath:'项目'},
        runDefaults:{local:clone(state.local),inherited:clone(state.inherited),effective:{...state.inherited,...state.local},sources:{
          mainModel:{folderId:'parent-b',path:'项目'},mainThinkingLevel:{folderId:'parent-b',path:'项目'},mainFastMode:{folderId:'parent-b',path:'项目'},
          agentModel:{folderId:'parent-b',path:'项目'},agentThinkLevel:{folderId:'parent-b',path:'项目'},agentFastMode:{folderId:'parent-b',path:'项目'},
        },fallback:clone(state.fallback),resolved:resolvedDefaults()},
      };
    }
    Api.conversationTreeBootstrap = async () => ({rootFolders:folders,activeCount:0,archivedCount:0,temporaryCount:0,running:{items:[],folderRunningCounts:{},folderConversationCounts:{}},initialExpandedPaths:[]});
    Api.conversationTreeChildren = async () => ({items:[],hasMore:false,nextCursor:''});
    Api.conversationTreeStatus = async () => ({items:[],folderRunningCounts:{},folderConversationCounts:{}});
    Api.rathOptions = async () => {if(failOptions)throw Error('options unavailable');return {models};};
    Api.conversationFolderProperties = async id => {
      calls.push({kind:'properties',id});
      if(id==='folder-a') await new Promise(resolve => releaseA=resolve);
      return properties(id);
    };
    Api.conversationFolderPropertiesImpact = async (id,payload) => {calls.push({kind:'impact',id,payload:clone(payload)});return impactCount++===0?{affectedCount:2,updatableCount:2,runningCount:0,archivedCount:0}:{affectedCount:0};};
    Api.updateConversationFolderProperties = async (id,payload) => {calls.push({kind:'update',id,payload:clone(payload)});state.workspaceDir=payload.workspaceDir;state.promptMarkdown=payload.promptMarkdown;if(Object.hasOwn(payload,'runDefaults'))state.local=clone(payload.runDefaults);return {updatedCount:payload.updateSnapshots?2:0,skippedRunningCount:0};};
    Api.locateConversationFolderInTree = async id => {calls.push({kind:'locate',id});if(id==='__temporary')throw Error('temporary is not a folder');return {folderItems:folders.filter(f=>f.folderId===id)};};
    window.addEventListener('openbear:folder-properties-changed',event=>events.push(clone(event.detail)));
    createApp(Tree).use(ElementPlus).mount('#app');
    window.folderFixture={calls,events,state,releaseA:()=>releaseA?.(),setLocal:value=>state.local=clone(value),failOptions:value=>failOptions=value};
  `);
  const baseCss = (await postcss([tailwindcss({...tailwindConfig, content: tailwindConfig.content.map(path => `${webRoot}/${path}`)})]).process(await readFile(webRoot + '/src/style.css', 'utf8'), {from: webRoot + '/src/style.css'})).css;
  const css = (await Promise.all(['node_modules/element-plus/dist/index.css', 'node_modules/element-plus/theme-chalk/dark/css-vars.css'].map(path => readFile(webRoot + '/' + path, 'utf8')))).join('\n') + '\n' + baseCss + '\n' + await readFile(webRoot + '/src/dark-theme.css', 'utf8') + '\n' + await readFile(webRoot + '/src/components/conversationTreeProperties.css', 'utf8');
  const server = createServer((req, res) => {
    if (req.url === '/bundle.js') { res.writeHead(200, {'Content-Type': 'text/javascript'}); return res.end(bundle); }
    if (req.url === '/style.css') { res.writeHead(200, {'Content-Type': 'text/css'}); return res.end(css); }
    res.end('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/style.css"></head><body><div id="app" style="width:320px;height:700px;display:flex"></div><script src="/bundle.js"></script></body></html>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  t.after(async () => { await browser?.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
  browser = await chromium.launch({executablePath, headless: true, args: ['--no-sandbox']});
  const page = await browser.newPage({viewport: {width: 1280, height: 900}}), errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.setDefaultTimeout(10000);
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.locator('[data-tree-id="folder-a"]').waitFor();

  const openProperties = async id => {
    await page.locator(`[data-tree-id="${id}"]`).click({button: 'right'});
    if (id === '__temporary') {
      const menu = page.locator('[data-conversation-tree-menu]');
      assert.deepEqual(await menu.getByRole('menuitem').allTextContents(), ['新建临时会话', '新建根级目录', '临时会话属性']);
      assert.equal(await menu.getByRole('menuitem').last().evaluate(element => element.previousElementSibling?.tagName), 'HR');
      if(screenshots)await page.screenshot({path:screenshots+'/temporary-menu.png'});
    }
    await page.getByRole('menuitem', {name: id==='__temporary'?'临时会话属性':'目录属性', exact: true}).click();
    await page.locator('.folder-properties-dialog').waitFor();
  };
  const selectValue = async (field, label) => {
    const control = page.locator(`[data-run-default-field="${field}"]`);
    const input = control.getByRole('combobox');
    await control.locator('.el-select__wrapper').click();
    await control.locator('[role="combobox"][aria-expanded="true"]').waitFor({state:'attached'});
    // Bind to this select's listbox, not another select's still-leaving popper.
    // Fixed sleeps are unreliable when CI runs all browser suites in parallel.
    const list = page.locator(`[id="${await input.getAttribute('aria-controls')}"]`);
    await list.locator('.el-select-dropdown__item').filter({hasText: label}).first().click();
    await control.locator('[role="combobox"][aria-expanded="false"]').waitFor({state:'attached'});
    await list.waitFor({state:'hidden'});
  };
  const selectedText = field => page.locator(`[data-run-default-field="${field}"] .el-select__placeholder`).innerText();

  await openProperties('folder-a');
  await page.getByRole('button', {name: '取消', exact: true}).click();
  await page.locator('.folder-properties-dialog').waitFor({state: 'hidden'});
  await openProperties('folder-b');
  const contextTab = page.getByRole('tab', {name:'目录上下文',exact:true});
  const defaultsTab = page.getByRole('tab', {name:'新会话默认',exact:true});
  await contextTab.focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await defaultsTab.getAttribute('aria-selected'),'true');
  assert.equal(await defaultsTab.getAttribute('tabindex'),'0');
  await page.keyboard.press('Home');
  assert.equal(await contextTab.getAttribute('aria-selected'),'true');
  await page.keyboard.press('End');
  assert.equal(await defaultsTab.getAttribute('aria-selected'),'true');
  await page.locator('[data-run-default-field="mainModel"]:not(.is-disabled)').waitFor();
  await page.waitForFunction(()=>getComputedStyle(document.querySelector('.folder-properties-dialog [role="tab"][aria-selected="true"]')).backgroundColor==='rgb(255, 255, 255)');
  const sheetStyle=await page.locator('.folder-properties-dialog').evaluate(element=>({background:getComputedStyle(element).backgroundColor,segment:getComputedStyle(element.querySelector('[role="tab"][aria-selected="true"]')).backgroundColor,tabFont:getComputedStyle(element.querySelector('[role="tab"]')).fontSize,legacyTabs:element.querySelectorAll('.el-tabs').length}));
  assert.deepEqual(sheetStyle,{background:'rgb(245, 245, 247)',segment:'rgb(255, 255, 255)',tabFont:'14px',legacyTabs:0});
  await page.evaluate(() => folderFixture.releaseA());
  await page.waitForTimeout(80);
  assert.match(await page.locator('.folder-properties-heading p').innerText(), /目录 B/, 'late folder A response cannot replace folder B');
  const initialDefaults = await page.locator('.run-default-groups').innerText();
  assert.match(initialDefaults, /上级目录 · 项目/);
  assert.match(initialDefaults, /继承 high → 实际 low/, 'inherited main thinking is shown separately from its normalized value');
  assert.match(initialDefaults, /继承 开启 → 实际 关闭/, 'inherited unsupported main Fast shows the actual disabled value');
  assert.match(initialDefaults, /继承 high → 实际 模型默认/, 'incompatible Agent thinking shows model-default normalization');
  assert.match(initialDefaults, /继承 开启 → 实际 跟随主会话/, 'unsupported Agent Fast shows follow normalization');
  await page.locator('[data-run-default-field="mainThinkingLevel"]').click();
  await page.locator('.el-select__popper:visible .el-select-dropdown__item').first().waitFor();
  let thinkingChoices = await page.locator('.el-select__popper:visible .el-select-dropdown__item').allTextContents();
  assert.deepEqual(thinkingChoices.map(text=>text.trim()), ['继承目录','low'], 'a thinking model without off never offers off');
  const choiceStyle=await page.locator('.folder-properties-popover.el-popper:visible').evaluate(element=>({radius:getComputedStyle(element).borderRadius,font:getComputedStyle(element.querySelector('.el-select-dropdown__item')).fontSize,arrows:element.querySelectorAll('.el-popper__arrow').length}));
  assert.deepEqual(choiceStyle,{radius:'10px',font:'14px',arrows:0});
  if(screenshots){await mkdir(screenshots,{recursive:true});await page.screenshot({path:screenshots+'/choice-menu.png'});}
  await page.keyboard.press('Escape');
  await page.waitForTimeout(220);
  await selectValue('mainModel', '无思考模型');
  await page.waitForTimeout(500);
  await page.locator('[data-run-default-field="mainThinkingLevel"]').click();
  const plainThinkingPopper = page.locator('.el-select__popper:visible').last();
  await plainThinkingPopper.locator('.el-select-dropdown__item').first().waitFor();
  thinkingChoices = await plainThinkingPopper.locator('.el-select-dropdown__item').allTextContents();
  assert.deepEqual(thinkingChoices.map(text=>text.trim()), ['继承目录','关闭（off）'], 'a model without thinking levels offers the backend-valid off value');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(220);

  await selectValue('mainModel', 'GPT-6 Astra');
  await selectValue('mainThinkingLevel', 'high');
  await selectValue('mainFastMode', '开启');
  await selectValue('mainModel', 'GPT-5.6 Sol');
  assert.match(await selectedText('mainThinkingLevel'), /low/, 'model switch repairs an incompatible local thinking override');
  assert.equal((await selectedText('mainFastMode')).trim(), '关闭', 'model switch repairs unsupported local Fast without deleting the override');
  await page.locator('[data-run-default-field="mainFastMode"]').click();
  assert.ok(await page.locator('.el-select__popper:visible .el-select-dropdown__item').filter({hasText: '开启'}).first().evaluate(element => element.classList.contains('is-disabled')));
  await page.keyboard.press('Escape');
  await page.waitForTimeout(220);
  await selectValue('mainModel', 'GPT-6 Astra');
  await selectValue('mainThinkingLevel', 'high');
  await selectValue('mainFastMode', '关闭');
  await selectValue('agentModel', '跟随主模型');
  await selectValue('agentThinkLevel', '跟随模型默认');
  await selectValue('agentFastMode', '跟随主会话 Fast');

  const dialog = page.locator('.folder-properties-dialog');
  const typography = await dialog.evaluate(element => {
    const size = selector => Number.parseFloat(getComputedStyle(element.querySelector(selector)).fontSize);
    return {label: size('.run-setting-row > h4'), source: size('.run-field-hint'), button: size('.property-footer-buttons .el-button'), select: size('.run-choice-value')};
  });
  assert.ok(typography.label >= 14 && typography.button >= 14 && typography.select >= 14, JSON.stringify(typography));
  assert.ok(typography.source >= 13, JSON.stringify(typography));
  if (screenshots) {
    await mkdir(screenshots, {recursive: true});
    await page.screenshot({path: screenshots + '/desktop-defaults.png'});
  }
  await page.setViewportSize({width: 390, height: 844});
  const narrow = await dialog.evaluate(element => { const rect = element.getBoundingClientRect(), footer = element.querySelector('.el-dialog__footer').getBoundingClientRect(); return {scrollWidth: element.scrollWidth, width: rect.width, left: rect.left, right: rect.right, bottom: rect.bottom, footerBottom: footer.bottom}; });
  assert.ok(narrow.scrollWidth <= narrow.width + 1, JSON.stringify(narrow));
  assert.ok(narrow.left >= 0 && narrow.right <= 390.5 && narrow.bottom <= 844.5 && narrow.footerBottom <= 844.5, JSON.stringify(narrow));
  if (screenshots) await page.screenshot({path: screenshots + '/narrow-defaults.png'});
  await page.setViewportSize({width: 1280, height: 900});

  await page.getByRole('tab', {name: '目录上下文', exact: true}).click();
  await page.waitForTimeout(120);
  const workspace = page.getByRole('textbox', {name: /本节点工作目录/});
  await workspace.fill('/srv/project-b');
  const contextGeometry = await page.evaluate(() => {
    const editor = document.querySelector('.folder-prompt-editor').getBoundingClientRect();
    const disclosure = document.querySelector('.effective-disclosure').getBoundingClientRect();
    const summary = document.querySelector('.effective-disclosure summary').getBoundingClientRect();
    const topElement = document.elementFromPoint(summary.left + 8, summary.top + 8);
    return {editor:{top:editor.top,bottom:editor.bottom,height:editor.height},disclosure:{top:disclosure.top,bottom:disclosure.bottom,height:disclosure.height},summary:{top:summary.top,bottom:summary.bottom},topElement:topElement?.className || topElement?.tagName};
  });
  assert.ok(contextGeometry.editor.bottom <= contextGeometry.disclosure.top + 1, JSON.stringify(contextGeometry));
  await page.locator('.effective-disclosure summary').click();
  assert.match(await page.locator('.effective-disclosure pre').innerText(), /来自父目录的真实提示词/);
  await page.getByRole('button', {name: '保存', exact: true}).click();
  const impact = page.getByRole('dialog', {name: /是否同时更新已有会话/});
  await impact.getByRole('button', {name: '保存并更新可更新会话', exact: true}).click();
  await dialog.waitFor({state: 'hidden'});
  const firstUpdate = await page.evaluate(() => folderFixture.calls.filter(call => call.kind === 'update')[0]);
  assert.equal(firstUpdate.id, 'folder-b');
  assert.equal(firstUpdate.payload.workspaceDir, '/srv/project-b');
  assert.equal(firstUpdate.payload.updateSnapshots, true);
  assert.deepEqual(firstUpdate.payload.runDefaults, {mainModel: 'model-fast', mainThinkingLevel: 'high', mainFastMode: false, agentModel: '', agentThinkLevel: '', agentFastMode: null});
  assert.deepEqual(await page.evaluate(() => folderFixture.events), [{folderId: 'folder-b'}]);

  await openProperties('folder-b');
  await page.getByRole('tab', {name: '新会话默认', exact: true}).click();
  await selectValue('mainFastMode', '继承目录');
  await page.getByRole('button', {name: '保存', exact: true}).click();
  await dialog.waitFor({state: 'hidden'});
  const secondUpdate = await page.evaluate(() => folderFixture.calls.filter(call => call.kind === 'update')[1]);
  assert.equal(Object.hasOwn(secondUpdate.payload.runDefaults, 'mainFastMode'), false, 'inherit removes only that field');
  assert.equal(secondUpdate.payload.runDefaults.agentModel, '');
  assert.equal(secondUpdate.payload.runDefaults.agentFastMode, null);

  await openProperties('folder-b');
  await page.getByRole('tab', {name: '新会话默认', exact: true}).click();
  await page.getByRole('button', {name: '全部恢复继承', exact: true}).click();
  await page.getByRole('button', {name: '保存', exact: true}).click();
  await dialog.waitFor({state: 'hidden'});
  const thirdUpdate = await page.evaluate(() => folderFixture.calls.filter(call => call.kind === 'update')[2]);
  assert.deepEqual(thirdUpdate.payload.runDefaults, {}, 'empty object clears every local default override');

  const staleDefaults = {mainModel: 'removed-model', agentModel: 'removed-agent', mainFastMode: false, agentFastMode: null};
  await page.evaluate(value => folderFixture.setLocal(value), staleDefaults);
  for (const id of ['folder-b', '__temporary']) {
    await openProperties(id);
    await page.getByRole('tab', {name: '新会话默认', exact: true}).click();
    assert.match(await page.locator('.run-default-groups').innerText(), /模型已移除/);
    await page.getByRole('tab', {name: id === '__temporary' ? '注入上下文' : '目录上下文', exact: true}).click();
    if (id !== '__temporary') await page.getByRole('textbox', {name: /本节点工作目录/}).fill('/srv/removed-model-context');
    await dialog.locator('.folder-prompt-editor .native-edit-context, .folder-prompt-editor textarea.inputarea').focus();
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.insertText('模型失效后仍可保存上下文 · ' + id);
    await page.getByRole('button', {name: '保存', exact: true}).click();
    await dialog.waitFor({state: 'hidden'});
    const saved = await page.evaluate(() => folderFixture.calls.filter(call => call.kind === 'update').at(-1));
    const preview = await page.evaluate(() => folderFixture.calls.filter(call => call.kind === 'impact').at(-1));
    assert.equal(saved.id, id);
    assert.equal(saved.payload.promptMarkdown, '模型失效后仍可保存上下文 · ' + id);
    assert.equal(Object.hasOwn(saved.payload, 'runDefaults'), false, 'context-only saves must not revalidate or rewrite old defaults');
    assert.equal(Object.hasOwn(preview.payload, 'runDefaults'), false, 'impact and save use the same sparse payload');
    assert.equal(Object.hasOwn(saved.payload, 'workspaceDir'), id !== '__temporary');
    assert.deepEqual(await page.evaluate(() => folderFixture.state.local), staleDefaults);
  }

  await openProperties('folder-b');
  await page.getByRole('tab', {name: '新会话默认', exact: true}).click();
  await selectValue('mainFastMode', '开启');
  const updateCount = await page.evaluate(() => folderFixture.calls.filter(call => call.kind === 'update').length);
  const impactCount = await page.evaluate(() => folderFixture.calls.filter(call => call.kind === 'impact').length);
  await page.getByRole('button', {name: '保存', exact: true}).click();
  await page.getByText('主会话模型 removed-model 已不可用，请更换或恢复继承', {exact:true}).waitFor();
  assert.equal(await page.evaluate(() => folderFixture.calls.filter(call => call.kind === 'update').length), updateCount, 'editing defaults still requires a valid model');
  assert.equal(await page.evaluate(() => folderFixture.calls.filter(call => call.kind === 'impact').length), impactCount);
  await selectValue('mainFastMode', '关闭');
  await page.getByRole('button', {name: '保存', exact: true}).click();
  await dialog.waitFor({state: 'hidden'});
  assert.equal(await page.evaluate(() => Object.hasOwn(folderFixture.calls.filter(call => call.kind === 'update').at(-1).payload, 'runDefaults')), false, 'changing a field back to its loaded value is not a defaults edit');
  assert.deepEqual(await page.evaluate(() => folderFixture.state.local), staleDefaults);

  // Parent/capability changes can also invalidate stored thinking/Fast without
  // changing the user's local configuration. Context editing must stay usable.
  const staleCapabilities = {mainThinkingLevel:'high',mainFastMode:true,agentThinkLevel:'high',agentFastMode:true};
  await page.evaluate(value => {folderFixture.setLocal(value);folderFixture.state.inherited.mainModel='model-slow';}, staleCapabilities);
  await openProperties('folder-b');
  await page.getByRole('textbox', {name: /本节点工作目录/}).fill('/srv/changed-parent-context');
  await page.getByRole('button', {name: '保存', exact: true}).click();
  await dialog.waitFor({state:'hidden'});
  assert.equal(await page.evaluate(() => Object.hasOwn(folderFixture.calls.filter(call => call.kind === 'update').at(-1).payload, 'runDefaults')), false);
  assert.deepEqual(await page.evaluate(() => folderFixture.state.local), staleCapabilities);

  const preservedDefaults = {mainModel:'model-fast',mainThinkingLevel:'high',mainFastMode:false,agentModel:'',agentThinkLevel:'',agentFastMode:null};
  await page.evaluate(value => {folderFixture.setLocal(value);folderFixture.failOptions(true);}, preservedDefaults);
  await openProperties('folder-b');
  await page.getByRole('tab', {name: '新会话默认', exact: true}).click();
  await page.getByText('模型选项暂不可用，运行默认保持只读', {exact: true}).waitFor();
  assert.ok(await page.locator('[data-run-default-field="mainModel"] .el-select__wrapper').evaluate(element => element.classList.contains('is-disabled') || element.getAttribute('aria-disabled') === 'true'));
  await page.getByRole('tab', {name: '目录上下文', exact: true}).click();
  await page.getByRole('textbox', {name: /本节点工作目录/}).fill('/srv/options-failed-save');
  const beforeFallbackSave = await page.evaluate(() => folderFixture.calls.filter(call => call.kind === 'update').length);
  await page.getByRole('button', {name: '保存', exact: true}).click();
  await dialog.waitFor({state: 'hidden'});
  const fallbackUpdate = await page.evaluate(() => folderFixture.calls.filter(call => call.kind === 'update').at(-1));
  const fallbackImpact = await page.evaluate(() => folderFixture.calls.filter(call => call.kind === 'impact').at(-1));
  assert.equal(await page.evaluate(() => folderFixture.calls.filter(call => call.kind === 'update').length), beforeFallbackSave + 1);
  assert.equal(fallbackUpdate.payload.workspaceDir, '/srv/options-failed-save');
  assert.equal(Object.hasOwn(fallbackImpact.payload, 'runDefaults'), false, 'impact preview also preserves unknown run defaults when options fail');
  assert.equal(Object.hasOwn(fallbackUpdate.payload, 'runDefaults'), false, 'options failure omits runDefaults so existing sparse values remain untouched');
  assert.deepEqual(await page.evaluate(() => folderFixture.state.local), preservedDefaults);

  // Match the user's real scenario: all six fields inherit and both model names
  // must be readable without repeating provider/key metadata beside the control.
  await page.evaluate(() => {
    folderFixture.failOptions(false);
    folderFixture.state.local = {};
    folderFixture.state.inherited = {};
    folderFixture.state.fallback = {mainModel:'model-fast',mainThinkingLevel:'high',mainFastMode:false,agentModel:'model-slow',agentThinkLevel:'low',agentFastMode:false};
  });
  await page.setViewportSize({width:820,height:650});
  await openProperties('folder-b');
  await page.getByRole('tab', {name:'新会话默认',exact:true}).click();
  const readability = await dialog.evaluate(element => {
    const cells = [...element.querySelectorAll('.run-default-cell')];
    return {height:element.getBoundingClientRect().height, models:cells.filter(cell=>cell.querySelector('[data-run-default-field$="Model"]')).map(cell=>{
      const control=cell.querySelector('.el-select'), value=cell.querySelector('.run-choice-value');
      return {width:control.getBoundingClientRect().width,text:value.textContent.trim(),clipped:value.scrollWidth>value.clientWidth+1};
    }), inherited:element.querySelectorAll('.run-choice-inherit').length};
  });
  assert.ok(readability.height<540, JSON.stringify(readability));
  assert.equal(readability.inherited,6);
  assert.deepEqual(readability.models.map(model=>model.text),['GPT-6 Astra','GPT-5.6 Sol']);
  assert.ok(readability.models.every(model=>model.width>=275&&!model.clipped),JSON.stringify(readability));
  if(screenshots) await page.screenshot({path:screenshots+'/desktop-defaults.png'});
  await page.evaluate(()=>document.documentElement.classList.add('dark'));
  assert.equal(await dialog.evaluate(element=>getComputedStyle(element).backgroundColor),'rgb(41, 41, 44)');
  if(screenshots) await page.screenshot({path:screenshots+'/dark-defaults.png'});
  await page.evaluate(()=>document.documentElement.classList.remove('dark'));
  await page.setViewportSize({width:390,height:844});
  const mobile=await dialog.evaluate(element=>({width:element.getBoundingClientRect().width,scroll:element.scrollWidth,minControl:Math.min(...[...element.querySelectorAll('.run-default-cell .el-select')].map(control=>control.getBoundingClientRect().width))}));
  assert.ok(mobile.scroll<=mobile.width+1&&mobile.minControl>=250,JSON.stringify(mobile));
  if(screenshots)await page.screenshot({path:screenshots+'/narrow-defaults.png'});
  await page.getByRole('button', {name:'取消',exact:true}).click();
  await dialog.waitFor({state:'hidden'});

  // Temporary properties reuse this same panel, but must not expose or submit
  // a workspace, pretend to inherit a parent, or locate a non-existent folder.
  await page.setViewportSize({width:820,height:650});
  await page.evaluate(()=>{folderFixture.state.workspaceDir='/must-not-submit';folderFixture.state.promptMarkdown='';});
  await openProperties('__temporary');
  assert.equal(await dialog.locator('h2').innerText(),'临时会话属性');
  await page.getByRole('tab',{name:'注入上下文',exact:true}).waitFor();
  assert.equal(await dialog.locator('[aria-labelledby="folder-workspace-label"]').count(),0);
  assert.equal(await dialog.locator('.effective-disclosure').count(),0);
  await dialog.locator('.folder-prompt-editor .native-edit-context, .folder-prompt-editor textarea.inputarea').focus();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText('临时会话专属上下文');
  if(screenshots)await page.screenshot({path:screenshots+'/temporary-context.png'});
  await page.getByRole('tab',{name:'新会话默认',exact:true}).click();
  assert.deepEqual(await dialog.locator('.run-choice-inherit').allTextContents(),Array(6).fill('默认'));
  await selectValue('mainModel','GPT-5.6 Sol');
  await selectValue('mainThinkingLevel','low');
  await selectValue('mainFastMode','关闭');
  await selectValue('agentModel','跟随主模型');
  await selectValue('agentThinkLevel','跟随模型默认');
  await selectValue('agentFastMode','关闭');
  if(screenshots)await page.screenshot({path:screenshots+'/temporary-defaults.png'});
  await page.setViewportSize({width:390,height:844});
  const tempMobile=await dialog.evaluate(element=>({width:element.getBoundingClientRect().width,scroll:element.scrollWidth,bottom:element.querySelector('.el-dialog__footer').getBoundingClientRect().bottom}));
  assert.ok(tempMobile.scroll<=tempMobile.width+1&&tempMobile.bottom<=844.5,JSON.stringify(tempMobile));
  if(screenshots)await page.screenshot({path:screenshots+'/temporary-narrow.png'});
  await page.getByRole('button',{name:'保存',exact:true}).click();
  await dialog.waitFor({state:'hidden'});
  const temporaryUpdate=await page.evaluate(()=>folderFixture.calls.filter(call=>call.kind==='update').at(-1));
  assert.equal(temporaryUpdate.id,'__temporary');
  assert.deepEqual(temporaryUpdate.payload,{promptMarkdown:'临时会话专属上下文',runDefaults:{mainModel:'model-slow',mainThinkingLevel:'low',mainFastMode:false,agentModel:'',agentThinkLevel:'',agentFastMode:false},updateSnapshots:false});
  assert.deepEqual(await page.evaluate(()=>folderFixture.events.at(-1)),{folderId:'__temporary'});
  assert.equal(await page.evaluate(()=>folderFixture.calls.some(call=>call.kind==='locate'&&call.id==='__temporary')),false);

  await openProperties('__temporary');
  await page.getByRole('tab',{name:'新会话默认',exact:true}).click();
  await page.getByRole('button',{name:'全部使用原有默认',exact:true}).click();
  await page.getByRole('button',{name:'保存',exact:true}).click();
  await dialog.waitFor({state:'hidden'});
  assert.deepEqual(await page.evaluate(()=>folderFixture.calls.filter(call=>call.kind==='update').at(-1).payload),{promptMarkdown:'临时会话专属上下文',runDefaults:{},updateSnapshots:false});
  await openProperties('folder-b');
  await page.getByRole('textbox',{name:/本节点工作目录/}).waitFor();
  assert.equal(await dialog.locator('h2').innerText(),'目录属性');
  assert.deepEqual(errors, []);
});
