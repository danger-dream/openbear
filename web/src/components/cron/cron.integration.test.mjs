import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import postcss from 'postcss';
import {parse} from '@vue/compiler-sfc';
import {ref,reactive,computed,nextTick,watch,effectScope} from 'vue';
import * as config from './cronConfig.js';
import {createCronEditor,createCronActions,createQuery} from './useCron.js';
import {runDefaultOption,RUN_DEFAULT_INHERIT} from '../folderRunDefaults.js';
const {clone,defaultConfig,draftOf,validateDraft}=config;
const read=path=>fs.readFileSync(new URL(path,import.meta.url),'utf8');
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const flush=async()=>{for(let i=0;i<16;i++)await Promise.resolve();};
const models=[{key:'fast',thinkingLevels:['off','high'],supportsFast:true,defaultThinkingLevel:'high'},{key:'plain',thinkingLevels:[],supportsFast:false}];
function fixture(overrides={}){
  const calls=[];let sequence=0;
  const job={id:'J1',folderId:'F1',name:'Daily',description:'',revision:1,enabled:false,config:defaultConfig()};
  const api={
    cronJob:async()=>({job:clone(job)}),cronEnvironment:async()=>({runtimes:[{runtime:'python',available:true,path:'/python'},{runtime:'node',available:true,path:'/node'}],defaultCwd:'/workspace'}),
    rathOptions:async()=>({models}),cronFolders:async()=>({items:[{id:'F1',name:'One'},{id:'F2',name:'Two'}]}),conversationFolderProperties:async()=>({runDefaults:{fallback:{mainModel:'fast',mainThinkingLevel:'high',mainFastMode:true}}}),
    createCronJob:async body=>{calls.push(['create',clone(body)]);return {job:{...clone(job),...clone(body),id:'J2'}};},
    updateCronJob:async(id,body)=>{calls.push(['update',id,clone(body)]);return {job:{...clone(job),...clone(body),revision:body.expectedRevision+1}};},
    cronPreview:async body=>{calls.push(['preview',clone(body)]);return {times:['2026-12-01T01:00:00Z']};},
    controlCronJob:async(id,body)=>{calls.push(['control',id,clone(body)]);return {job:{...clone(job),enabled:body.enabled,revision:2}};},
    runCronJob:async(id,body)=>{calls.push(['run',id,clone(body)]);return {run:{id:'R1',conversationId:'C1'}};},
    cronDeleteImpact:async(id,body)=>{calls.push(['impact',id,clone(body)]);return {impact:{activeRuns:2,retainedRuns:7},confirmationToken:'delete-token'};},
    deleteCronJob:async(id,body)=>{calls.push(['delete',id,clone(body)]);return {job:{...clone(job),scheduleState:'deleted'}};},
    ...overrides,
  };
  const ids={requestId:()=>`request-${++sequence}`};
  return {api,calls,job,editor:createCronEditor(api,ids),actions:createCronActions(api,ids)};
}

test('defaults follow Cron contract, without Webhook state or default enabled execution',()=>{
  const c=defaultConfig();assert.equal(c.pre.timeoutSeconds,30);assert.equal(c.pre.onError,'fail');assert.deepEqual(c.post.onOutcomes,config.OUTCOMES);assert.equal(c.notifications.mode,'inherit');assert.equal(c.totalTokensBudget,null);
  assert.equal(draftOf(null).enabled,false);for(const key of ['batching','queue','target','credentials','concurrency'])assert.equal(key in c,false);
});
test('three independent inheritance fields preserve false and convert explicit inherit to null',()=>{
  let c={mainModel:null,mainThinkingLevel:null,mainFastMode:null};
  c=config.changeModel(c,'mainFastMode',runDefaultOption(false));assert.equal(c.mainFastMode,false);
  const folder={fallback:{mainModel:'fast',mainThinkingLevel:'high',mainFastMode:true}};
  assert.equal(config.effectiveModel(c,folder,models).mainFastMode,false);assert.equal(config.inheritedModel(c,folder,models).mainFastMode,true);
  c=config.changeModel(c,'mainFastMode',RUN_DEFAULT_INHERIT);assert.deepEqual(c,{mainModel:null,mainThinkingLevel:null,mainFastMode:null});
  const d=draftOf(null,'F1');d.name='Task';d.config.runConfig={mainModel:'plain',mainThinkingLevel:'high',mainFastMode:true};
  assert.deepEqual(validateDraft(d,{models,folder}).filter(x=>x.path.startsWith('runConfig.')).map(x=>x.path),['runConfig.mainThinkingLevel','runConfig.mainFastMode']);
});
test('budgets reject zero, negatives, fractional Tokens; null is unlimited and missing cost differs from zero',()=>{
  const d=draftOf(null,'F1');d.name='Task';assert.deepEqual(validateDraft(d),[]);
  d.config.totalTokensBudget=1.5;d.config.costBudgetUsd=0;d.config.timeoutSeconds=-1;
  assert.equal(validateDraft(d).length,3);d.config.totalTokensBudget=100;d.config.costBudgetUsd=.001;d.config.timeoutSeconds=1;assert.deepEqual(validateDraft(d),[]);
  assert.equal(config.money(0),'$0');assert.equal(config.money(0.00000001),'$0.00000001');assert.equal(config.money(null),'—');assert.equal(config.number(1234567),'1,234,567');assert.equal(config.number(0),'0');
});
test('schedule normalization supports at/every/cron and validates timezone and explicit offset',()=>{
  const c=defaultConfig().schedule;assert.deepEqual(config.scheduleErrors(c),[]);c.expression='0 9 * *';assert.equal(config.scheduleErrors(c)[0].path,'schedule.expression');
  Object.assign(c,{kind:'at',at:'2026-12-01T09:00:00+08:00'});assert.deepEqual(config.scheduleErrors(c),[]);assert.equal(config.schedulePayload(c).expression,null);
  c.at='2026-12-01T09:00';assert.equal(config.scheduleErrors(c)[0].path,'schedule.at');
  Object.assign(c,{kind:'every',everySeconds:30,anchorAt:null});assert.deepEqual(config.scheduleErrors(c),[]);assert.equal(config.schedulePayload(c).anchorAt,null);
  c.everySeconds=0;assert.equal(config.scheduleErrors(c)[0].path,'schedule.everySeconds');c.timezone='Not/AZone';assert.equal(config.scheduleErrors(c)[0].path,'schedule.timezone');
});
test('scripts persist disabled code and validate retries, onError, environment and outcome selection',()=>{
  const d=draftOf(null,'F1');d.name='Task';d.config.pre.code='  preserved\n';assert.deepEqual(validateDraft(d),[]);
  d.config.pre.enabled=true;d.config.pre.retry.maxAttempts=0;d.config.pre.retry.backoffSeconds=[2,NaN];d.config.post.enabled=true;d.config.post.code='cleanup()';d.config.post.onOutcomes=['normal'];
  const errors=validateDraft(d,{environment:{runtimes:[{runtime:'python',available:false}]}}).map(x=>x.path);
  for(const key of ['pre.retry.maxAttempts','pre.retry.backoffSeconds','pre.runtime','post.onOutcomes'])assert.ok(errors.includes(key),key);
  d.config.pre.retry={maxAttempts:3,backoffSeconds:[0,2,10]};d.config.pre.onError='continue';d.config.post.onOutcomes=['failed','cancelled'];assert.deepEqual(validateDraft(d),[]);assert.equal(d.config.pre.code,'  preserved\n');
});
test('create saves disabled by default, complete config and stable requestId; update never changes folderId',async()=>{
  const {editor,calls}=fixture();await editor.load('','F1');editor.draft.name='New';assert.equal(await editor.save(),true);
  const body=calls[0][1];assert.equal(body.enabled,false);assert.equal(body.folderId,'F1');assert.equal(body.requestId,'request-1');assert.deepEqual(body.config.runConfig,{mainModel:null,mainThinkingLevel:null,mainFastMode:null});
  editor.draft.config.notifications={mode:'both',errorsOnly:true};editor.draft.config.runConfig.mainFastMode=false;editor.draft.config.totalTokensBudget=123456;
  assert.equal(await editor.save(),true);assert.equal(calls[1][2].expectedRevision,1);assert.equal('folderId' in calls[1][2],false);assert.equal(calls[1][2].config.runConfig.mainFastMode,false);assert.equal(calls[1][2].config.notifications.errorsOnly,true);
});
test('CAS failure keeps full draft; latest-version review is explicit and requires another save',async()=>{
  const io=fixture({updateCronJob:async()=>{throw {response:{status:409,data:{message:'conflict',currentRevision:4}}};}});
  await io.editor.load('J1');io.editor.draft.name='Local';io.editor.draft.config.post.code='kept';assert.equal(await io.editor.save(),false);assert.equal(io.editor.job.value.revision,1);assert.equal(io.editor.draft.name,'Local');assert.equal(io.editor.dirty.value,true);
  io.api.cronJob=async()=>({job:{...clone(io.job),name:'Remote',revision:4}});assert.equal(await io.editor.prepareMerge(),true);assert.equal(io.editor.draft.name,'Local');assert.equal(io.editor.conflict.value.current.name,'Remote');assert.equal(io.editor.job.value.revision,4);assert.equal(io.editor.draft.config.post.code,'kept');
  let payload;io.api.updateCronJob=async(_id,body)=>{payload=body;return {job:{...clone(io.job),...body,revision:5}};};assert.equal(await io.editor.save(),true);assert.equal(payload.expectedRevision,4);assert.equal(payload.name,'Local');
});
test('lost save response retries same requestId; changed intent gets new ID',async()=>{
  const seen=[];const {editor}=fixture({updateCronJob:async(_id,body)=>{seen.push(body);throw Error('offline');}});await editor.load('J1');editor.draft.name='Local';await editor.save();await editor.save();assert.equal(seen[0].requestId,seen[1].requestId);
  editor.draft.description='changed';await editor.save();assert.notEqual(seen[1].requestId,seen[2].requestId);assert.equal(editor.draft.name,'Local');
});
test('single-flight save and older responses cannot overwrite later typing or resurrect closed editor',async()=>{
  const wait=deferred(),io=fixture({updateCronJob:()=>wait.promise});await io.editor.load('J1');io.editor.draft.name='Submitted';const save=io.editor.save();assert.equal(await io.editor.save(),false);assert.equal(await io.editor.load('J2'),false);
  io.editor.draft.name='Typed later';wait.resolve({job:{...clone(io.job),name:'Submitted',revision:2}});assert.equal(await save,true);assert.equal(io.editor.draft.name,'Typed later');assert.equal(io.editor.dirty.value,true);
  const later=deferred();io.api.updateCronJob=()=>later.promise;const pending=io.editor.save();io.editor.dispose();later.resolve({job:{...clone(io.job),name:'Disposed'}});assert.equal(await pending,false);assert.equal(io.editor.draft.name,'Typed later');
});
test('preview really calls contract calculator, clears stale results and never runs business',async()=>{
  const io=fixture();await io.editor.load('J1');assert.equal(await io.editor.previewNext(),true);assert.deepEqual(io.calls[0],['preview',{schedule:defaultConfig().schedule,count:5}]);assert.equal(io.editor.preview.data.value.times.length,1);
  const pending=deferred();io.api.cronPreview=()=>pending.promise;const load=io.editor.previewNext();io.editor.preview.clear();pending.resolve({times:['old']});assert.equal(await load,false);assert.equal(io.editor.preview.data.value,null);
});
test('latest query/folder inheritance wins; loading failures do not silently fall back to system',async()=>{
  const old=deferred();const q=createQuery(id=>id===1?old.promise:Promise.resolve({id}));const p=q.load(1);await q.load(2);old.resolve({id:1});await p;assert.equal(q.data.value.id,2);q.dispose();
  const io=fixture();await io.editor.load('J1');const folder=deferred();io.api.conversationFolderProperties=id=>id==='F1'?folder.promise:Promise.resolve({runDefaults:{local:{mainModel:'plain'}}});const previous=io.editor.loadInheritance('F1');await io.editor.loadInheritance('F2');folder.resolve({runDefaults:{local:{mainModel:'fast'}}});await previous;assert.equal(io.editor.folder.value.local.mainModel,'plain');
  io.api.conversationFolderProperties=async()=>{throw Error('unavailable');};await io.editor.loadInheritance();assert.equal(await io.editor.save(),false);assert.equal(io.editor.inheritanceError.value,'unavailable');
});
test('run/control use revision and requestId; lost response does not create a second intent',async()=>{
  const io=fixture();await io.actions.perform('control',io.job,{enabled:true});assert.equal(io.calls[0][2].enabled,true);assert.equal(io.calls[0][2].expectedRevision,1);
  const seen=[];io.api.runCronJob=async(_id,body)=>{seen.push(body);throw Error('lost');};await io.actions.perform('run',io.job);await io.actions.perform('run',io.job);assert.equal(seen[0].requestId,seen[1].requestId);assert.deepEqual(Object.keys(seen[0]).sort(),['expectedRevision','requestId']);
});
test('deletion requires impact token, presents preserved runs, reuses requestId when response is lost',async()=>{
  const io=fixture();assert.equal(await io.actions.deletePrepared(),null);assert.equal(await io.actions.prepareDelete(io.job),true);
  assert.deepEqual(io.calls[0],['impact','J1',{expectedRevision:1}]);assert.equal(io.actions.preparation.value.impact.activeRuns,2);assert.equal(io.actions.preparation.value.impact.retainedRuns,7);
  const seen=[];io.api.deleteCronJob=async(id,body)=>{seen.push(body);throw Error('lost');};await io.actions.deletePrepared();await io.actions.deletePrepared();assert.equal(seen[0].confirmationToken,'delete-token');assert.equal(seen[0].requestId,seen[1].requestId);assert.equal('stopRunning' in seen[0],false);
});

function harness(path,{props={},Api={},...extra}={}){
  const descriptor=parse(read(path)).descriptor;
  const source=descriptor.scriptSetup.content.replace(/^import .*;\n/gm,'');
  const events=[],messages=[],cleanup=[];
  const context={...config,createQuery,createCronActions,createCronEditor,ref,reactive,computed,nextTick,watch:()=>{},onBeforeUnmount:fn=>cleanup.push(fn),defineProps:()=>props,defineEmits:()=> (...args)=>events.push(args),defineExpose:()=>{},
    ElMessage:{success:value=>messages.push(value)},ElMessageBox:{confirm:async()=>{}},Api,crypto:{randomUUID:()=> 'request-test'},window:{addEventListener(){},removeEventListener(){}},console,...extra};
  vm.createContext(context);vm.runInContext(source,context);
  return {context,events,messages,cleanup,run:code=>vm.runInContext(code,context)};
}
test('actual CronEditor keeps cancelled-close draft and navigates invalid fields without saving',async()=>{
  const io=fixture(),h=harness('./CronEditor.vue',{props:{jobId:'J1',folderId:'F1'},Api:io.api});await flush();h.run("draft.name='Local'");
  h.context.ElMessageBox.confirm=async()=>{throw Error('cancel');};assert.equal(await h.run('canLeave()'),false);await h.run('close()');assert.equal(h.events.length,0);
  h.run('draft.config.totalTokensBudget=0');assert.equal(await h.run('save()'),false);assert.equal(h.run('tab.value'),'basic');assert.equal(io.calls.length,0);
  h.run('draft.config.totalTokensBudget=null');h.context.ElMessageBox.confirm=async()=>{};assert.equal(await h.run('save()'),true);assert.equal(h.events[0][0],'saved');assert.equal(await h.run('canLeave()'),true);h.cleanup.forEach(fn=>fn());
});
test('actual view confirms real execution, cancellation sends no API call, disabling does not stop runs',async()=>{
  const io=fixture(),h=harness('../../views/CronView.vue',{props:{folderId:'F1'},Api:{...io.api,cronJobs:async()=>({items:[],total:0})}});h.context.target=io.job;
  h.context.ElMessageBox.confirm=async()=>{throw Error('cancel');};await h.run('run(target)');assert.equal(io.calls.length,0);
  h.context.ElMessageBox.confirm=async()=>{};await h.run('run(target)');assert.equal(io.calls[0][0],'run');assert.equal(h.run('runId.value'),'R1');assert.equal(h.run('refreshKey.value'),1);
  await h.run('control({...target,enabled:true})');assert.equal(io.calls[1][2].enabled,false);
  await h.run('prepareDelete(target)');assert.equal(h.run('deleteOpen.value'),true);await h.run('remove()');assert.equal(h.run('deleteOpen.value'),false);h.cleanup.forEach(fn=>fn());
});
test('actual history sends folder/job/time/status/offset filters; stats omit status and preserve zeros',async()=>{
  const calls=[];const h=harness('./CronHistory.vue',{props:{folderId:'F1',jobId:'J1'},Api:{cronRuns:async params=>{calls.push(['runs',params]);return {items:[],total:65};},cronStatistics:async params=>{calls.push(['stats',params]);return {runs:0,costUsd:0};}}});
  h.run("start.value='2026-10-01T09:00';end.value='2026-10-02T09:00';status.value='failed';filter()");await flush();assert.equal(calls[0][1].folderId,'F1');assert.equal(calls[0][1].jobId,'J1');assert.equal(calls[0][1].status,'failed');assert.match(calls[0][1].start,/Z$/);assert.equal('status' in calls[1][1],false);
  h.run('page(30)');await flush();assert.equal(calls[2][1].offset,30);assert.equal(h.run('summary.value.costUsd'),0);
  h.run("end.value='2026-09-01T09:00';filter()");assert.equal(calls.length,3);assert.match(h.run('rangeError.value'),/开始时间/);h.cleanup.forEach(fn=>fn());
});
test('script backoff text retains trailing separator while binding numeric contract values',()=>{
  const script=defaultConfig().pre;const h=harness('./CronScriptEditor.vue',{props:{script,phase:'pre',environment:{},errors:[]}});
  h.run("backoff('2, ')");assert.equal(h.run('backoffText.value'),'2, ');assert.deepEqual(clone(script.retry.backoffSeconds),[2]);
  h.run("backoff('2, 10, 30')");assert.deepEqual(clone(script.retry.backoffSeconds),[2,10,30]);
});
test('real schedule watcher invalidates results on edits; preview response cannot repopulate stale times',async()=>{
  const io=fixture(),scope=effectScope();const h=scope.run(()=>harness('./CronEditor.vue',{props:{jobId:'J1',folderId:'F1'},Api:io.api,watch}));await flush();await h.run('editor.previewNext()');assert.equal(h.run('previewData.value.times.length'),1);
  h.run("draft.config.schedule.expression='0 10 * * *'");await nextTick();assert.equal(h.run('previewData.value'),null);scope.stop();h.cleanup.forEach(fn=>fn());
});
test('directory context menu opens its Cron list directly above properties, with no dialog entry',async()=>{
  const source=read('../ConversationTree.vue');const start=source.indexOf('async function runMenuAction(action)');const end=source.indexOf('function dragStart(',start);const events=[],properties=[];
  const state={menu:ref({open:true,row:{kind:'folder',folderId:'F1'}}),closeMenu(){state.menu.value.open=false;state.menu.value.row=null;},showProperties:async row=>properties.push(row),window:{dispatchEvent:event=>events.push(event)},CustomEvent:class{constructor(type,init){this.type=type;this.detail=init.detail;}},ElMessage:{error:assert.fail},apiError:error=>error};
  vm.createContext(state);vm.runInContext(source.slice(start,end),state);
  await vm.runInContext("runMenuAction('cron')",state);assert.equal(state.menu.value.open,false);assert.equal(events[0].type,'openbear:open-cron');assert.equal(events[0].detail.folderId,'F1');assert.equal(properties.length,0);
  for(const row of [{kind:'system',systemNode:'temporary'},{kind:'root'},{kind:'conversation',folderId:'F1'}]){state.menu.value={open:true,row};await vm.runInContext("runMenuAction('cron')",state);}assert.equal(events.length,1);
  state.menu.value={open:true,row:{kind:'folder',folderId:'F2'}};await vm.runInContext("runMenuAction('properties')",state);assert.equal(properties[0].folderId,'F2');
  const folderMenu=source.slice(source.indexOf('<template v-if="menu.row?.kind === \'folder\'">'),source.indexOf('<template v-else-if="[\'system\', \'root\']'));
  assert.match(folderMenu,/runMenuAction\('cron'\)[^\n]*定时任务[^\n]*\n\s*<button[^\n]*runMenuAction\('properties'\)/);
  assert.doesNotMatch(source.slice(source.indexOf('<el-dialog v-model="propertiesDialog"')),/openFolderCron|定时任务/);
  assert.doesNotMatch(read('../ConversationPropertiesDialog.vue'),/openFolderCron|open-cron/);
});
test('interval units load exact existing seconds and save seconds without changing the API',async()=>{
  assert.deepEqual(config.INTERVAL_UNITS.map(item=>item.label),['秒','分','时','天']);
  for(const [seconds,unit] of [[45,1],[90,1],[900,60],[7200,3600],[172800,86400]])assert.equal(config.intervalUnitFor(seconds),unit);
  const io=fixture();io.job.config.schedule={kind:'every',timezone:'Asia/Shanghai',everySeconds:7200,anchorAt:'2026-10-05T09:00:00+08:00'};
  const scope=effectScope();const h=scope.run(()=>harness('./CronEditor.vue',{props:{jobId:'J1',folderId:'F1'},Api:io.api,watch}));await flush();
  assert.equal(h.run('intervalUnit.value'),3600);assert.equal(h.run('intervalAmount.value'),2);assert.equal(h.run('dirty.value'),false);
  h.run('setIntervalUnit(86400)');assert.equal(h.run('draft.config.schedule.everySeconds'),172800);
  h.run('intervalAmount.value=1.5;setIntervalUnit(60)');assert.equal(h.run('draft.config.schedule.everySeconds'),90);
  assert.equal(await h.run('save()'),true);assert.equal(io.calls[0][2].config.schedule.everySeconds,90);assert.equal(io.calls[0][2].config.schedule.anchorAt,'2026-10-05T09:00:00+08:00');
  h.run('intervalAmount.value=null;setIntervalUnit(3600)');assert.equal(h.run('draft.config.schedule.everySeconds'),null);
  h.run('intervalAmount.value=.07');assert.equal(h.run('draft.config.schedule.everySeconds'),252);
  scope.stop();h.cleanup.forEach(fn=>fn());
});
test('datetime picker preserves existing instants, emits ISO only on selection, and can clear an anchor',()=>{
  const props={modelValue:'2026-12-01T09:12:34+08:00'},h=harness('./CronDateTime.vue',{props});
  assert.equal(h.run('selected.value.toISOString()'),'2026-12-01T01:12:34.000Z');assert.equal(h.events.length,0);assert.equal(props.modelValue,'2026-12-01T09:12:34+08:00');
  h.run("selected.value=new Date('2026-12-02T17:20:30+08:00')");assert.deepEqual(h.events[0],['update:modelValue','2026-12-02T09:20:30.000Z']);
  h.run('selected.value=null');assert.deepEqual(h.events[1],['update:modelValue',null]);
  assert.equal(config.pickerDate('invalid'),null);assert.equal(config.pickerDate(null),null);assert.equal(config.pickerIso(new Date('invalid')),null);
  assert.match(read('./CronDateTime.vue'),/<el-date-picker[^>]*type="datetime"/);
});
test('model settings belong to basic information and instruction errors select their own editor tab',async()=>{
  const source=read('./CronEditor.vue');const basic=source.slice(source.indexOf('<section v-show="tab===\'basic\'"'),source.indexOf('<section v-show="tab===\'schedule\'"'));
  const instructions=source.slice(source.indexOf('<section v-show="tab===\'instructions\'"'),source.indexOf('<section v-show="tab===\'scripts\'"'));
  assert.match(basic,/<ModelSettings/);assert.doesNotMatch(instructions,/<ModelSettings/);assert.match(instructions,/<AdaptiveMdEditor/);assert.deepEqual(config.TABS.map(item=>item[0]),['basic','schedule','instructions','scripts']);
  const io=fixture(),h=harness('./CronEditor.vue',{props:{jobId:'J1',folderId:'F1'},Api:io.api});await flush();h.run('draft.enabled=true');await h.run('save()');assert.equal(h.run('tab.value'),'instructions');
  h.run("draft.config.instructions='Run bounded task';draft.config.runConfig.mainModel='gone'");await h.run('save()');assert.equal(h.run('tab.value'),'basic');h.cleanup.forEach(fn=>fn());
});
test('Cron layout fixes native input shrink, tab spacing, full-width model controls and aligned editors',()=>{
  const css=postcss.parse(read('./cron.css'));
  const rules=selector=>{const values={};css.walkRules(rule=>{if(rule.parent.type==='root'&&rule.selectors.includes(selector))rule.walkDecls(d=>values[d.prop]=d.value);});return values;};
  const numberInput=rules('.cron-root .wh-field-value>input[type="number"]');assert.equal(numberInput.flex,'1 1 0%');assert.equal(numberInput.width,'0');assert.equal(numberInput['min-width'],'0');assert.equal(rules('.cron-root .wh-unit').flex,'none');
  assert.equal(rules('.cron-section+.cron-section')['margin-top'],undefined);assert.equal(rules('.cron-section>.cron-grid:first-child')['margin-top'],'0');assert.equal(rules('.cron-editor-scroll')['padding'],'16px 20px');
  assert.equal(rules('.cron-root .cron-model-settings').display,'grid');assert.equal(rules('.cron-root .cron-model-settings .el-select').width,'100%');assert.equal(rules('.cron-root .cron-model-settings.model-settings.main-only .run-setting-row')['flex-direction'],'column');
  assert.equal(rules('.cron-section-instructions').flex,'1');assert.equal(rules('.cron-instructions').height,undefined);assert.equal(rules('.cron-instructions').flex,'1');assert.equal(rules('.cron-editor-scroll.is-instructions').overflow,'hidden');
  assert.equal(rules('.cron-script-grid')['align-items'],'stretch');assert.equal(rules('.cron-script-source>.cron-code').flex,'1');assert.equal(rules('.cron-script-source>.cron-code').height,undefined);
  assert.match(read('./CronScriptEditor.vue'),/<dl class="cron-env-grid">/);assert.doesNotMatch(read('./CronScriptEditor.vue'),/<details/);
});
test('API methods use only Cron endpoints, encoded IDs, DELETE body, and exact preview schema',async()=>{
  const calls=[];const transport={interceptors:{request:{use(){}},response:{use(){}}}};for(const method of ['get','post','patch','delete','put'])transport[method]=async(...args)=>{calls.push([method,...args]);return {data:{ok:true}};};
  const context={axios:{create:()=>transport},window:{location:{pathname:'/cron'}},uploadFilesViaHttp(){}};vm.createContext(context);vm.runInContext(read('../../api.js').replace(/^import .*;\n/gm,'').replaceAll('export ',''),context);
  await vm.runInContext("Api.cronJobs({folderId:'F1',enabled:false,limit:30,offset:30})",context);assert.equal(calls[0][1],'/cron/jobs');assert.equal(calls[0][2].params.enabled,false);
  for(const name of ['cronJob','updateCronJob','controlCronJob','runCronJob','cronDeleteImpact','deleteCronJob','cronRun'])await vm.runInContext(`Api.${name}('id/encoded',{requestId:'r',confirmationToken:'token'})`,context);
  assert.ok(calls.slice(1).every(call=>call[1].includes('id%2Fencoded')));const deletion=calls.find(call=>call[0]==='delete');assert.equal(deletion[2].data.confirmationToken,'token');
  await vm.runInContext("Api.cronPreview({schedule:{kind:'every',everySeconds:60},count:5})",context);assert.equal(calls.at(-1)[1],'/cron/preview-next');assert.equal(calls.at(-1)[2].count,5);
  await vm.runInContext('Api.cronHolidays()',context);assert.deepEqual(calls.at(-1).slice(0,2),['get','/cron/holidays']);
  await vm.runInContext('Api.syncCronHolidays()',context);assert.deepEqual(calls.at(-1).slice(0,2),['post','/cron/holidays/sync']);
});
test('Tokens shorthand is case-insensitive, decimal-scaled and sent as numeric budget',async()=>{
  const cases=[['10m',10000000],['100M',100000000],['1024k',1024000],['2K',2000],['1b',1000000000],['1.5B',1500000000],['1.001k',1001],['.5m',500000],[' 10 M ',10000000],['1200',1200],[1234,1234],['',null],['   ',null],[null,null]];
  const io=fixture();await io.editor.load('J1');
  for(const [text,expected] of cases){
    assert.equal(config.parseTokenBudget(text),expected,String(text));
    io.editor.draft.config.totalTokensBudget=text;
    assert.equal(await io.editor.save(),true,String(text));
    assert.equal(io.calls.at(-1)[2].config.totalTokensBudget,expected);
  }
});
test('invalid Tokens text cannot silently become unlimited or reach the API',async()=>{
  const io=fixture();await io.editor.load('J1');
  for(const value of ['wat','10mb','1kk','-2m','0','0b','1.5','0.0001k','1e6',Infinity,9007199254740992]){
    io.editor.draft.config.totalTokensBudget=value;
    assert.equal(await io.editor.save(),false,String(value));
    assert.ok(io.editor.errors.value.some(item=>item.path==='totalTokensBudget'));
  }
  assert.equal(io.calls.length,0);
});
test('directory is required; timeout is optional but validates positive finite values at field and save level',async()=>{
  const io=fixture();await io.editor.load();io.editor.draft.name='Task';
  io.editor.validateField('folderId');assert.equal(io.editor.errors.value[0].path,'folderId');
  assert.equal(await io.editor.save(),false);assert.equal(io.calls.length,0);
  io.editor.draft.folderId='F1';io.editor.validateField('folderId');assert.equal(io.editor.errors.value.length,0);
  for(const value of [0,-1,NaN,Infinity,'abc']){
    io.editor.draft.config.timeoutSeconds=value;io.editor.validateField('timeoutSeconds');
    assert.match(io.editor.errors.value[0].message,/大于 0/);assert.equal(await io.editor.save(),false);
  }
  for(const value of [null,.5,600]){
    io.editor.draft.config.timeoutSeconds=value;io.editor.validateField('timeoutSeconds');
    assert.equal(io.editor.errors.value.length,0);assert.equal(await io.editor.save(),true);
    assert.equal(io.calls.at(-1).at(-1).config.timeoutSeconds,value);
  }
});
test('shared field forwards required/filterable/placeholder and distinguishes malformed numeric input from blank',()=>{
  const h=harness('../webhooks/WebhookField.vue',{props:{type:'number'},useId:()=>1,nullableNumber:value=>value===''?null:Number(value)});
  h.run("update({target:{value:'',validity:{badInput:true}}})");assert.ok(Number.isNaN(h.events[0][1]));
  h.run("update({target:{value:'',validity:{badInput:false}}})");assert.equal(h.events[1][1],null);
  h.run("update({target:{value:'600',validity:{badInput:false}}})");assert.equal(h.events[2][1],600);
  const source=read('../webhooks/WebhookField.vue');
  for(const attribute of [':placeholder="placeholder"',':filterable="filterable"',':aria-required="required"',':required="required"'])assert.ok(source.includes(attribute));
  const editor=read('./CronEditor.vue');assert.match(editor,/<Field[^>]*label="所属目录" required filterable placeholder="请选择所属目录"/);
  assert.match(editor,/<Field[^>]*label="单次执行超时"[^>]*placeholder="例如 600"/);
});
test('timezone select lists searchable IANA names, Chinese labels and retains configured aliases',()=>{
  const options=config.timezoneOptions('Etc/GMT-8');
  assert.equal(new Set(options.map(item=>item.value)).size,options.length);
  assert.ok(options.some(item=>item.value==='Asia/Shanghai'&&item.label.includes('北京')));
  assert.ok(options.some(item=>item.value==='Etc/GMT-8'));
  assert.ok(config.timezoneOptions('Asia/Kathmandu',{}).some(item=>item.value==='Asia/Kathmandu'));
  assert.match(read('./CronEditor.vue'),/<Field[^>]*label="规则时区" filterable :options="timezones"/);
  assert.match(read('./CronEditor.vue'),/0 9 \* \* 1-5/);
});
test('page Tabs default to calendar, retain jobs/history and preserve filters',async()=>{
  const io=fixture(),h=harness('../../views/CronView.vue',{props:{folderId:'F1'},Api:{...io.api,cronJobs:async()=>({items:[],total:0})}});
  h.context.target=io.job;assert.equal(h.run('activeTab.value'),'calendar');assert.equal(h.run('historyVisited.value'),false);
  h.run("search.value='Keep';openHistory(target)");await flush();assert.equal(h.run('activeTab.value'),'history');assert.equal(h.run('historyJob.value.id'),'J1');assert.equal(h.run('historyVisited.value'),true);
  await h.run("navigatePageTabs({key:'Home',preventDefault(){}})");assert.equal(h.run('activeTab.value'),'calendar');assert.equal(h.run('search.value'),'Keep');
  await h.run("navigatePageTabs({key:'ArrowRight',preventDefault(){}})");assert.equal(h.run('activeTab.value'),'jobs');
  await h.run("navigatePageTabs({key:'ArrowRight',preventDefault(){}})");assert.equal(h.run('activeTab.value'),'history');
  const source=read('../../views/CronView.vue');
  assert.match(source,/<section v-show="activeTab==='jobs'"[^>]*role="tabpanel"/);
  assert.match(source,/<div v-show="activeTab==='history'"[^>]*role="tabpanel"/);
  h.cleanup.forEach(fn=>fn());
});
test('Cron ordinary and primary buttons match header visual tokens, not Element defaults',()=>{
  function declarations(source,selector){const css=postcss.parse(source),values={};css.walkRules(rule=>{if(rule.parent.type==='root'&&rule.selectors.includes(selector))rule.walkDecls(d=>values[d.prop]=d.value);});return values;}
  const header=parse(read('../AdminPageHeader.vue')).descriptor.styles[0].content;
  const expected=declarations(header,'.admin-page-header :deep(.el-button)');
  const actual=declarations(read('./cron.css'),'.cron-root .el-button:not(.is-link)');
  for(const key of ['height','min-height','padding','border','border-radius','background','color','font-size','font-weight','box-shadow'])assert.equal(actual[key],expected[key],key);
  const primary=declarations(read('./cron.css'),'.cron-root .el-button--primary:not(.is-link)');
  assert.deepEqual(primary,declarations(header,'.admin-header-primary :deep(.el-button)'));
  assert.doesNotMatch(read('../../views/CronView.vue'),/type="primary" plain/);
});
test('no Cron stop control exists',()=>{
  for(const path of ['./CronEditor.vue','./CronDateTime.vue','./CronScriptEditor.vue','./CronHistory.vue','./CronRunDialog.vue','../../views/CronView.vue']){
    assert.doesNotMatch(read(path),/conversationStop|stopCron|cronStop|\/stop/,path);
  }
});
