import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse,compileScript} from '@vue/compiler-sfc';
import * as config from './cronConfig.js';
import * as cron from './useCron.js';
import * as defaults from '../folderRunDefaults.js';

const read=path=>fs.readFileSync(new URL(path,import.meta.url),'utf8');
const clone=config.clone;
const flush=async()=>{for(let i=0;i<24;i++)await Vue.nextTick();};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const models=[{key:'fast',thinkingLevels:['off','high'],supportsFast:true,defaultThinkingLevel:'high'},{key:'plain',thinkingLevels:[],supportsFast:false}];
function fixture(overrides={}){
  const job={id:'J1',folderId:'F1',name:'Daily',description:'',revision:1,enabled:false,config:config.defaultConfig()};
  const calls=[];
  const api={cronJob:async()=>({job:clone(job)}),cronEnvironment:async()=>({runtimes:[]}),rathOptions:async()=>({models}),cronFolders:async()=>({items:[{id:'F1',name:'One'}]}),
    conversationFolderProperties:async()=>({runDefaults:{fallback:{mainModel:'fast',mainThinkingLevel:'high',mainFastMode:true}}}),
    updateCronJob:async(_id,body)=>{calls.push(clone(body));return {job:{...clone(job),...body,revision:2}};},...overrides};
  return {job,api,calls};
}
function harness(path,{props={},...extra}={}){
  const descriptor=parse(read(path)).descriptor,scope=Vue.effectScope(),cleanups=[],events=[];
  const ctx={...Vue,...config,...cron,...defaults,defineProps:()=>props,defineEmits:()=> (...args)=>events.push(args),defineExpose:()=>{},onBeforeUnmount:fn=>cleanups.push(fn),
    ElMessage:{success(){}},ElMessageBox:{confirm:async()=>{}},window:{addEventListener(){},removeEventListener(){}},crypto:{randomUUID:()=> 'test-request'},...extra};
  vm.createContext(ctx);scope.run(()=>vm.runInContext(descriptor.scriptSetup.content.replace(/^import .*;\n/gm,''),ctx));
  return {run:code=>vm.runInContext(code,ctx),ctx,descriptor,events,close(){cleanups.forEach(fn=>fn());scope.stop();}};
}
const appSource=read('../../App.vue');
function between(source,start,end){const a=source.indexOf(start),b=source.indexOf(end,a+start.length);assert.ok(a>=0&&b>a);return source.slice(a,b);}
function navigation(guard,urls=['/docs','/cron?folderId=F1'],position=urls.length-1){
  let index=position;const entries=urls.map((url,i)=>({url,state:{openbearRoutePosition:i,kept:'history-state'}})),moves=[];
  const location={};
  const locate=()=>{const url=new URL(entries[index].url,'https://example.invalid');Object.assign(location,{href:url.href,pathname:url.pathname,search:url.search});};
  locate();let ctx;
  const history={get state(){return entries[index].state;},pushState(state,_,url){entries.splice(index+1);entries.push({state,url});index++;locate();},replaceState(state,_,url){entries[index]={state,url};locate();},
    go(delta){moves.push(delta);queueMicrotask(()=>{const next=index+delta;if(next<0||next>=entries.length)return;index=next;locate();void vm.runInContext('handleHistoryNavigation()',ctx);});}};
  ctx=vm.createContext({...Vue,URL,URLSearchParams,window:{location,history},isLoginPath:false,active:Vue.ref('cron'),sidebarOpen:Vue.ref(true),
    cronFolderId:Vue.ref('F1'),cronLeaveGuard:Vue.shallowRef(guard),activeConversationUuid:Vue.ref('old-chat'),memoryType:Vue.ref('identity'),settingsSection:Vue.ref('channels'),closeConversationMenu(){},
    suppressConversationOpenUntil:0,ElMessage:{error:assert.fail},apiError:e=>e});
  vm.runInContext(between(appSource,'const pageToPath =','const desktopNav =')+between(appSource,"const ROUTE_POSITION =",'function fmtTime(')
    +between(appSource,'async function openConversation(row)','async function renameConversation('),ctx);
  vm.runInContext('applyRouteFromLocation({replaceUnknown:true})',ctx);
  return {ctx,entries,moves,history,location,run:code=>vm.runInContext(code,ctx),get index(){return index;}};
}
async function editorGuard(api,confirm){
  const ed=harness('./CronEditor.vue',{props:{jobId:'J1',folderId:'F1'},Api:api,ElMessageBox:{confirm}});await flush();ed.run("draft.name='Local draft'");
  const view=harness('../../views/CronView.vue',{props:{folderId:'F1'},Api:{...api,cronJobs:async()=>({items:[],total:0})}});
  view.ctx.editor={canLeave:()=>ed.run('canLeave()')};view.run('editorOpen.value=true;editorRef.value=editor');
  const guard=view.events.find(([name])=>name==='cron-leave-guard')[1];
  return {ed,view,guard,close(){view.close();ed.close();}};
}

test('02 real Cron editor guard reaches App through lazy-view event; cancelled page/chat/push navigation preserves draft and URL',async()=>{
  const io=fixture();let confirms=0;const g=await editorGuard(io.api,async()=>{confirms++;throw Error('cancel');});
  const n=navigation(g.guard),original=clone(n.entries);
  for(const action of ["selectNav('settings')","openConversation({conversationUuid:'next-chat'})","navigateToUrl('/chat?id=pushed')"]){
    await n.run(action);assert.equal(n.ctx.active.value,'cron');assert.equal(n.location.pathname,'/cron');assert.equal(n.ctx.activeConversationUuid.value,'old-chat');
  }
  assert.equal(confirms,3);assert.equal(g.ed.run('draft.name'),'Local draft');assert.equal(g.ed.run('dirty.value'),true);assert.deepEqual(n.entries,original);
  assert.match(appSource,/@cron-leave-guard="cronLeaveGuard = \$event"/);
  // The wrapper forwards listeners as attrs; a parent ref would not reach this child.
  assert.match(read('../../lazyView.js'),/h\(view.value, attrs, slots\)/);
  g.close();assert.equal(g.view.events.at(-1)[0],'cron-leave-guard');assert.equal(g.view.events.at(-1)[1],null);
});
test('02 cancelled Back restores the original entry, then accepted Back/Forward retain native history',async()=>{
  const io=fixture();let allow=false;const g=await editorGuard(io.api,async()=>{if(!allow)throw Error('cancel');});
  const n=navigation(g.guard),original=clone(n.entries);
  n.history.go(-1);await flush();assert.equal(n.index,1);assert.equal(n.location.pathname,'/cron');assert.equal(n.ctx.active.value,'cron');assert.equal(g.ed.run('dirty.value'),true);assert.deepEqual(n.entries,original);
  allow=true;n.history.go(-1);await flush();assert.equal(n.index,0);assert.equal(n.ctx.active.value,'docs');assert.equal(n.location.pathname,'/docs');assert.deepEqual(n.entries,original);
  n.history.go(1);await flush();assert.equal(n.index,1);assert.equal(n.ctx.active.value,'cron');assert.equal(n.ctx.cronFolderId.value,'F1');
  await n.run("selectNav('settings')");assert.equal(n.ctx.active.value,'settings');assert.equal(n.location.pathname,'/settings');g.close();
});
test('02 cancelled Forward and rapid history changes restore the correct entry with a single confirmation',async()=>{
  const wait=deferred(),io=fixture();let confirms=0;const g=await editorGuard(io.api,()=>{confirms++;return wait.promise;});
  const n=navigation(g.guard,['/docs','/cron?folderId=F1','/settings'],1),original=clone(n.entries);
  n.history.go(1);await flush();assert.equal(n.index,2);assert.equal(n.ctx.active.value,'cron');
  n.history.go(-2);await flush();assert.equal(n.index,0);assert.equal(confirms,1);
  wait.reject(Error('cancel'));await flush();assert.equal(n.index,1);assert.equal(n.location.pathname,'/cron');assert.equal(n.ctx.active.value,'cron');assert.deepEqual(n.entries,original);g.close();
});
test('02 saving blocks navigation and Back without dropping the in-flight save result',async()=>{
  const wait=deferred(),io=fixture({updateCronJob:()=>wait.promise});let confirms=0;
  const g=await editorGuard(io.api,async()=>{confirms++;});const n=navigation(g.guard);
  const save=g.ed.run('save()');assert.equal(g.ed.run('saving.value'),true);
  await n.run("selectNav('docs')");n.history.go(-1);await flush();assert.equal(n.index,1);assert.equal(n.ctx.active.value,'cron');assert.equal(confirms,0);
  wait.resolve({job:{...clone(io.job),name:'Local draft',revision:2}});assert.equal(await save,true);assert.equal(g.ed.run('job.value.revision'),2);assert.equal(g.ed.run('dirty.value'),false);
  await n.run("selectNav('docs')");assert.equal(n.ctx.active.value,'docs');assert.equal(n.location.pathname,'/docs');g.close();
});
test('02 permission is rechecked if saving begins while a leave confirmation is open',async()=>{
  const confirm=deferred(),saveResult=deferred(),io=fixture({updateCronJob:()=>saveResult.promise});
  const g=await editorGuard(io.api,()=>confirm.promise),n=navigation(g.guard);
  const leave=n.run("selectNav('docs')");const save=g.ed.run('save()');confirm.resolve();await leave;
  assert.equal(n.ctx.active.value,'cron');assert.equal(g.ed.run('saving.value'),true);
  saveResult.resolve({job:{...clone(io.job),name:'Local draft',revision:2}});await save;g.close();
});
test('02 non-Cron routes remain synchronous and history cancellation is not applied to them',async()=>{
  let checks=0;const n=navigation(()=>{checks++;return false;},['/chat?id=A','/docs']);
  n.run("selectNav('settings')");assert.equal(n.ctx.active.value,'settings');assert.equal(n.location.pathname,'/settings');assert.equal(checks,0);
  n.history.go(-1);await flush();assert.equal(n.ctx.active.value,'docs');assert.deepEqual(n.moves,[-1]);assert.equal(checks,0);
});

async function modelMarkup(runConfig,repairUnsupported=true){
  const props={modelValue:config.overrides(runConfig),effective:config.effectiveModel(runConfig,{},models),inherited:{},models,inheritable:true,repairUnsupported,showAgent:false,showStrategy:false,disabled:false,inheritLabel:'继承目录'};
  const h=harness('../ModelSettings.vue',{props,modelThinkingLevels:model=>model?.thinkingLevels || [],thinkingLabel:value=>value});
  const bindings=compileScript(h.descriptor,{id:'model-fix'}).bindings,names=Object.keys(bindings).filter(name=>bindings[name]!=='props');
  const setup=Vue.proxyRefs({...h.run(`({${names.join(',')}})`),...props}),draw=Vue.compile(h.descriptor.template.content);
  const app=Vue.createSSRApp({render(){return draw.call(this,setup,[]);}});
  app.component('ElSelect',{setup(_,{attrs,slots}){return()=>Vue.h('select',attrs,slots.default?.());}});
  app.component('ElOption',{setup(_,{attrs}){return()=>Vue.h('option',attrs,attrs.label);}});app.config.warnHandler=()=>{};
  const html=await renderToString(app);h.close();return html;
}
test('09 new model stays selected while incompatible overrides can be repaired individually, including explicit false',async()=>{
  const io=fixture(),ed=harness('./CronEditor.vue',{props:{jobId:'J1',folderId:'F1'},Api:io.api});await flush();
  ed.ctx.selection=defaults.runDefaultOption('plain');ed.run("draft.config.runConfig={mainModel:'fast',mainThinkingLevel:'high',mainFastMode:true};setModel('mainModel',selection)");
  let rc=clone(ed.run('draft.config.runConfig')),html=await modelMarkup(rc);
  for(const field of ['mainModel','mainThinkingLevel','mainFastMode'])assert.ok(html.includes(`data-run-default-field="${field}"`));
  assert.match(html,/当前不可用/);assert.equal(await ed.run('save()'),false);
  ed.ctx.selection=defaults.RUN_DEFAULT_INHERIT;ed.run("setModel('mainThinkingLevel',selection)");
  ed.ctx.selection=defaults.runDefaultOption(false);ed.run("setModel('mainFastMode',selection)");
  assert.equal(await ed.run('save()'),true);assert.deepEqual(io.calls.at(-1).config.runConfig,{mainModel:'plain',mainThinkingLevel:null,mainFastMode:false});
  rc=clone(ed.run('draft.config.runConfig'));html=await modelMarkup(rc);assert.match(html,/data-run-default-field="mainFastMode"/);
  ed.ctx.selection=defaults.RUN_DEFAULT_INHERIT;ed.run("setModel('mainFastMode',selection)");assert.equal(await ed.run('save()'),true);
  assert.deepEqual(io.calls.at(-1).config.runConfig,{mainModel:'plain',mainThinkingLevel:null,mainFastMode:null});
  assert.match(read('./CronEditor.vue'),/inheritable repair-unsupported/);ed.close();
});
test('09 unsupported-model recovery is opt-in and preserves other ModelSettings consumers',async()=>{
  const html=await modelMarkup({mainModel:'plain',mainThinkingLevel:'high',mainFastMode:true},false);
  assert.doesNotMatch(html,/data-run-default-field="mainThinkingLevel"|data-run-default-field="mainFastMode"/);assert.match(html,/当前模型不支持/);
});

test('10 unapplied dates never affect paging, status, retries or directory refresh; applying updates runs and statistics together',async()=>{
  const calls=[],props=Vue.reactive({folderId:'F1',jobId:'J1',refreshKey:0});
  const h=harness('./CronHistory.vue',{props,Api:{cronRuns:async p=>{calls.push(['runs',clone(p)]);return {items:[],total:90};},cronStatistics:async p=>{calls.push(['stats',clone(p)]);return {runs:10};}}});await flush();
  h.run("start.value='2026-10-01T00:00';end.value='2026-10-02T00:00';filter()");await flush();
  const applied=calls.filter(([kind])=>kind==='stats').at(-1)[1],statsCount=calls.filter(([kind])=>kind==='stats').length;
  h.run("start.value='2026-10-03T00:00';end.value='2026-10-04T00:00';status.value='failed'");await flush();h.run('page(30);loadRuns()');await flush();
  assert.equal(calls.filter(([kind])=>kind==='stats').length,statsCount);
  for(const [,p] of calls.slice(-3)){assert.equal(p.start,applied.start);assert.equal(p.end,applied.end);assert.equal(p.status,'failed');}
  props.folderId='F2';props.refreshKey++;await flush();
  for(const [kind,p] of calls.slice(-2)){assert.equal(p.folderId,'F2');assert.equal(p.start,applied.start);if(kind==='stats')assert.equal('status' in p,false);}
  h.run('filter()');await flush();const [runs,stats]=calls.slice(-2);assert.equal(runs[1].start,stats[1].start);assert.notEqual(stats[1].start,applied.start);assert.equal(runs[1].offset,0);assert.equal('status' in stats[1],false);
  h.run("end.value='2026-09-01T00:00';filter()");assert.match(h.run('rangeError.value'),/开始时间/);h.run('page(30)');await flush();assert.equal(calls.at(-1)[1].start,stats[1].start);h.close();
});

for(const phase of ['pre','post'])for(const enabled of [false,true])test(`11 ${phase} enabled=${enabled}: invalid numeric fields are blocked locally and exact contract boundaries remain valid`,async()=>{
  const io=fixture(),editor=cron.createCronEditor(io.api,{requestId:()=> 'script-test'});await editor.load('J1');
  const s=editor.draft.config[phase];s.enabled=enabled;s.code=enabled?'print(1)':'';
  const cases=[['timeoutSeconds',null],['timeoutSeconds',0],['timeoutSeconds',3601],['timeoutSeconds',NaN],['retry.maxAttempts',0],['retry.maxAttempts',6],['retry.maxAttempts',1.5],['retry.backoffSeconds',[-1]],['retry.backoffSeconds',[3601]],['retry.backoffSeconds',[NaN]]];
  for(const [field,value] of cases){
    const obj=field.startsWith('retry.')?s.retry:s,key=field.split('.').at(-1),old=obj[key];obj[key]=value;
    assert.equal(await editor.save(),false,`${field}=${value}`);assert.ok(editor.errors.value.some(e=>e.path===`${phase}.${field}`&&e.tab==='scripts'));obj[key]=old;
  }
  assert.equal(io.calls.length,0);
  for(const timeout of [.0001,3600]){
    const current=editor.draft.config[phase];current.timeoutSeconds=timeout;current.retry={maxAttempts:5,backoffSeconds:[0,3600]};assert.equal(await editor.save(),true);
    assert.equal(io.calls.at(-1).config[phase].timeoutSeconds,timeout);assert.deepEqual(io.calls.at(-1).config[phase].retry,{maxAttempts:5,backoffSeconds:[0,3600]});
  }
  editor.dispose();
});
test('11 disabled post errors select post tab and focus its actual numeric field without discarding code',async()=>{
  const io=fixture(),h=harness('./CronEditor.vue',{props:{jobId:'J1',folderId:'F1'},Api:io.api});await flush();let focus='',selector='';
  h.ctx.rootElement={querySelector:s=>{selector=s;return {focus:()=>{focus=s;}};}};
  h.run("root.value=rootElement;draft.config.post.code='kept';draft.config.post.enabled=false;draft.config.post.timeoutSeconds=null;tab.value='basic'");
  assert.equal(await h.run('save()'),false);assert.equal(h.run('tab.value'),'scripts');assert.equal(h.run('phase.value'),'post');assert.match(selector,/post\.timeoutSeconds/);assert.equal(focus,selector);assert.equal(io.calls.length,0);
  h.run('draft.config.post.timeoutSeconds=30');assert.equal(await h.run('save()'),true);assert.equal(io.calls[0].config.post.code,'kept');assert.equal(io.calls[0].config.post.enabled,false);
  const source=read('./CronScriptEditor.vue');assert.match(source,/:max="3600"/);assert.match(source,/:max="5"/);assert.match(source,/\$\{phase\}\.timeoutSeconds/);h.close();
});

for(const initialTotal of [31,61])test(`16 deleting the last of ${initialTotal} filtered jobs clamps to the last valid page with identical filters`,async()=>{
  const io=fixture(),calls=[];let total=initialTotal;const previousOffset=initialTotal-1,nextOffset=previousOffset-30;
  const h=harness('../../views/CronView.vue',{props:{folderId:'F1'},Api:{...io.api,cronJobs:async p=>{calls.push(clone(p));return {items:p.offset<total?[clone(io.job)]:[],total};},cronDeleteImpact:async()=>({impact:{},confirmationToken:'token'}),deleteCronJob:async()=>{total--;return {job:io.job};}}});await flush();
  h.run("search.value='Daily';enabled.value=false");await flush();h.run(`page(${previousOffset})`);await flush();h.ctx.target=io.job;
  await h.run('prepareDelete(target)');await h.run('remove()');await flush();
  assert.equal(h.run('offset.value'),nextOffset);assert.equal(h.run('data.value.total'),initialTotal-1);assert.equal(h.run('data.value.items.length'),1);
  assert.deepEqual(calls.slice(-2).map(p=>p.offset),[previousOffset,nextOffset]);for(const p of calls.slice(-2)){assert.equal(p.folderId,'F1');assert.equal(p.search,'Daily');assert.equal(p.enabled,false);}
  total=0;h.run('refresh()');await flush();assert.equal(h.run('offset.value'),0);assert.equal(h.run('data.value.total'),0);h.close();
});
test('16 stale out-of-range responses and disposed views cannot rewind a newer folder filter',async()=>{
  const wait=deferred(),io=fixture(),props=Vue.reactive({folderId:'F1'}),calls=[];
  const h=harness('../../views/CronView.vue',{props,Api:{...io.api,cronJobs:async p=>{calls.push(clone(p));return p.offset===60?wait.promise:{items:[io.job],total:1};}}});await flush();
  h.run('page(60)');props.folderId='F2';await flush();wait.resolve({items:[],total:30});await flush();assert.equal(h.run('offset.value'),0);assert.equal(calls.at(-1).folderId,'F2');assert.equal(h.run('data.value.total'),1);
  const late=deferred();h.ctx.Api.cronJobs=()=>late.promise;h.run('page(30)');h.close();late.resolve({items:[],total:0});await flush();assert.equal(h.run('offset.value'),30);assert.equal(h.run('data.value'),null);
});
test('U2 a task may change servers and instances only once ticked; the box explains the risk and is saved with the task',async()=>{
  assert.equal(config.defaultConfig().allowServerChanges,false);
  const io=fixture();const ed=harness('./CronEditor.vue',{props:{jobId:'J1',folderId:'F1'},Api:io.api});await flush();
  assert.equal(ed.run('draft.config.allowServerChanges'),false);
  ed.run('draft.config.allowServerChanges=true');assert.equal(await ed.run('save()'),true);
  assert.equal(io.calls.at(-1).config.allowServerChanges,true);ed.close();
  const template=ed.descriptor.template.content;
  assert.match(template,/<el-checkbox v-model="draft\.config\.allowServerChanges"[^>]*>允许改动服务器和实例<\/el-checkbox>/);
  assert.match(template,/draft\.config\.allowServerChanges \? '定时任务无人值守，可能被服务器返回的内容误导而执行改动/);
  assert.match(template,/'未勾选时，执行中只能查看服务器和实例。'/);
});
