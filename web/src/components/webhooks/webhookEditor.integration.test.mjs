import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {parse} from '@vue/compiler-sfc';
import {computed,ref,reactive,nextTick,watch,effectScope} from 'vue';
import {createWebhookEditor} from './useWebhookEditor.js';
import {normalizedRunDefaults, updateRunDefault} from '../folderRunDefaults.js';
import {defaultConfig,configErrors,clone,targetTree,tabKey,nullableNumber,TRIGGER_TABS} from './webhookConfig.js';
const scope = {type:'folder',id:'F1'};
const deferred = () => {let resolve,reject;const promise = new Promise((yes,no)=>{resolve=yes;reject=no;});return {resolve,reject,promise};};
function fixture(overrides={}) {
  let id=0; const calls=[];
  const endpoint={id:'E1',scope,revision:1,controlRevision:1,enabled:false,name:'Orders',description:'',config:defaultConfig(scope),credential:{hasCredential:true,prefix:'obw_'},pauseReasons:[]};
  const api={
    async webhooks(){return {items:[{id:'E1'}]};},async webhook(){return {endpoint:clone(endpoint)};},async webhookEnvironment(){return {runtimes:[{runtime:'python',available:true}],limits:{batching:{maxBatchEvents:1000,maxBatchBytes:262144},scripts:{maxTimeoutSeconds:600}}};},
    async createWebhook(body){calls.push(['create',clone(body)]);return {endpoint:{...endpoint,...clone(body),revision:1},credential:{key:'TEST-ONLY-KEY',recoverable:true,displayOnce:false}};},
    async updateWebhook(_id,body){calls.push(['update',clone(body)]);return {endpoint:{...endpoint,...clone(body),revision:body.expectedRevision+1}};},
    async controlWebhook(_id,body){calls.push(['control',clone(body)]);return {endpoint:{...endpoint,controlRevision:2,dispatchPaused:true}};},...overrides,
  };
  return {editor:createWebhookEditor(api,{requestId:()=>`request-${++id}`}),api,calls,endpoint};
}
test('F01/F03 defaults match contract and a conversation can only target itself',()=>{
  const folder=defaultConfig(scope),conversation=defaultConfig({type:'conversation',id:'C1'});
  assert.equal(folder.target.mode,'newConversation');assert.equal(folder.pre.enabled,false);assert.equal(folder.expiry.ttlSeconds,null);
  assert.deepEqual(TRIGGER_TABS.map(x=>x[1]),['连接','处理指令','脚本','收集与排队','统计','高级']);
  assert.deepEqual(conversation.target,{mode:'fixedConversation',conversationId:'C1',runConfig:null});
  conversation.target.conversationId='C2';assert.ok(configErrors(conversation,{scope:{type:'conversation',id:'C1'}}).some(x=>x.path==='target.conversationId'));
});
test('F08 OR batching accepts max wait smaller than idle; null versus zero preserves field semantics',()=>{
  const config=defaultConfig(scope);config.batching.idleSeconds=30;config.batching.maxWaitSeconds=2;
  assert.deepEqual(configErrors(config),[]);assert.equal(nullableNumber(''),null);assert.equal(nullableNumber('0'),0);
  config.expiry.ttlSeconds=0;assert.ok(configErrors(config).some(x=>x.path==='expiry.ttlSeconds'));
  config.expiry.ttlSeconds=null;assert.deepEqual(configErrors(config),[]);
});
test('F07 disabled scripts retain code, enabled environment and retry contracts are enforced',()=>{
  const config=defaultConfig(scope);config.pre.code='  preserved\n';config.pre.retry.maxAttempts=3;
  assert.deepEqual(configErrors(config),[]);
  config.pre.enabled=true;
  let errors=configErrors(config,{environment:{runtimes:[{runtime:'python',available:false}]}});
  assert.ok(errors.some(x=>x.path==='pre.runtime'));assert.ok(errors.some(x=>x.path==='pre.retry.idempotencyDeclaration'));
  config.pre.retry.idempotencyDeclaration='actionKey deduplicated by downstream';
  errors=configErrors(config);assert.deepEqual(errors,[]);assert.equal(config.pre.code,'  preserved\n');
});
test('F07/F08 metric definitions, source timestamps, own echoes and hard limits validate independently',()=>{
  const config=defaultConfig(scope);config.expiry.ttlSeconds=2;config.expiry.basis='sourceField';config.correlation.ignoreOwnEcho=true;
  config.statistics.metricDefinitions=[{name:'custom_duration',type:'histogram',histogramBuckets:[1,1]}];config.batching.maxEvents=200;
  const errors=configErrors(config,{environment:{limits:{batching:{maxBatchEvents:100}}}});
  for(const path of ['expiry.timestampPath','correlation.originPath','batching.maxEvents','statistics.metricDefinitions.0'])assert.ok(errors.some(x=>x.path===path),path);
});
test('F03 target tree keeps folders expandable and excludes archived conversations',()=>{
  const tree=targetTree([{type:'folder',id:'F1',name:'Root'},{type:'folder',id:'F2',name:'Child',parentId:'F1'},{type:'conversation',id:'C1',title:'Valid',parentId:'F2',available:true},{type:'conversation',id:'C2',title:'Archived',parentId:'F2',available:false}]);
  assert.equal(tree[0].selectable,false);assert.equal(tree[0].children[0].children[0].value,'C1');assert.equal(tree[0].children[0].children.length,1);
});
test('F04 loading and internal edits never save automatically; enable and revision are one request',async()=>{
  const {editor,calls}=fixture();await editor.load(scope);editor.draft.enabled=true;editor.draft.config.processing.instructions='Handle only configured order callbacks';
  assert.equal(editor.dirty.value,true);assert.equal(calls.length,0);assert.equal(await editor.save(),true);
  assert.equal(calls.length,1);assert.equal(calls[0][1].enabled,true);assert.equal(calls[0][1].expectedRevision,1);assert.equal(calls[0][1].expectedControlRevision,1);assert.equal(editor.dirty.value,false);
});
test('F04 failed and CAS-conflicted writes preserve draft and do not advance versions',async()=>{
  const failure={response:{status:409,data:{message:'版本冲突',currentRevision:3,details:{current:{name:'Other'}}}}};
  const {editor}=fixture({updateWebhook:async()=>{throw failure;}});await editor.load(scope);editor.draft.name='Keep me';
  assert.equal(await editor.save(),false);assert.equal(editor.draft.name,'Keep me');assert.equal(editor.endpoint.value.revision,1);assert.equal(editor.conflict.value.currentRevision,3);assert.equal(editor.dirty.value,true);
});
test('F04 explicit merge reads latest revisions but preserves edits and never submits automatically',async()=>{
  const {editor,api,endpoint,calls}=fixture();await editor.load(scope);editor.draft.name='Keep local';editor.draft.config.processing.instructions='Local instructions';
  const newest={...clone(endpoint),name:'Other name',revision:3,controlRevision:4};
  api.webhook=async()=>({endpoint:newest});editor.conflict.value={currentRevision:3};
  assert.equal(await editor.prepareMerge(),true);assert.equal(calls.length,0);assert.equal(editor.draft.name,'Keep local');assert.equal(editor.draft.config.processing.instructions,'Local instructions');
  assert.equal(editor.endpoint.value.revision,3);assert.equal(editor.endpoint.value.controlRevision,4);assert.equal(editor.conflict.value.details.current.name,'Other name');assert.equal(editor.dirty.value,true);
  assert.equal(await editor.save(),true);assert.equal(calls[0][1].expectedRevision,3);assert.equal(calls[0][1].expectedControlRevision,4);assert.equal(calls[0][1].name,'Keep local');
});
test('F04 failed and unmounted conflict refresh cannot destroy local draft or advance its baseline',async()=>{
  const {editor,api,endpoint}=fixture();await editor.load(scope);editor.draft.name='Keep local';editor.conflict.value={currentRevision:3};api.webhook=async()=>{throw new Error('offline');};
  assert.equal(await editor.prepareMerge(),false);assert.equal(editor.endpoint.value.revision,1);assert.equal(editor.draft.name,'Keep local');assert.equal(editor.error.value,'offline');assert.equal(editor.conflict.value.currentRevision,3);
  const pending=deferred();api.webhook=()=>pending.promise;const refresh=editor.prepareMerge();editor.dispose();pending.resolve({endpoint:{...endpoint,revision:3}});
  assert.equal(await refresh,false);assert.equal(editor.endpoint.value.revision,1);assert.equal(editor.draft.name,'Keep local');
});
test('F04 uncertain write retry uses same request ID; changing intent obtains a new one',async()=>{
  const seen=[];const {editor}=fixture({updateWebhook:async(_id,body)=>{seen.push(body.requestId);throw new Error('connection lost');}});
  await editor.load(scope);editor.draft.name='First';await editor.save();await editor.save();assert.equal(seen[0],seen[1]);editor.draft.name='Second';await editor.save();assert.notEqual(seen[1],seen[2]);
});
test('F04 save is single flight, scope cannot change while pending, later edits survive',async()=>{
  const pending=deferred();const {editor,endpoint}=fixture({updateWebhook:()=>pending.promise});await editor.load(scope);editor.draft.name='Submitted';
  const save=editor.save();assert.equal(await editor.save(),false);assert.equal(await editor.load({type:'folder',id:'F2'}),false);editor.draft.name='Typed later';
  pending.resolve({endpoint:{...endpoint,name:'Submitted',revision:2}});assert.equal(await save,true);assert.equal(editor.draft.name,'Typed later');assert.equal(editor.endpoint.value.revision,2);assert.equal(editor.dirty.value,true);
});
test('F05 immediate controls preserve rule edits and discarding them does not undo pause',async()=>{
  const {editor,calls}=fixture();await editor.load(scope);editor.draft.config.processing.instructions='Unsaved';
  await editor.control('pause');assert.equal(calls[0][0],'control');assert.equal(editor.draft.config.processing.instructions,'Unsaved');assert.equal(editor.endpoint.value.dispatchPaused,true);
  editor.discard();assert.equal(editor.endpoint.value.dispatchPaused,true);assert.equal(editor.draft.config.processing.instructions,'');assert.equal(editor.dirty.value,false);
});
test('F06 recoverable credentials never enter editor state or block leaving and reloading',async()=>{
  const {editor}=fixture({webhooks:async()=>({items:[]})});await editor.load(scope);await editor.save();
  assert.equal(editor.oneTimeKey,undefined);assert.ok(!JSON.stringify(editor.draft).includes('TEST-ONLY-KEY'));assert.ok(!JSON.stringify(editor.endpoint.value).includes('TEST-ONLY-KEY'));
  assert.equal(await editor.load({type:'folder',id:'F2'}),true);editor.dispose();
});
test('F12 older list response cannot overwrite a new target',async()=>{
  const first=deferred();const {editor}=fixture({webhooks:params=>params.scopeId==='F1'?first.promise:Promise.resolve({items:[]})});
  const old=editor.load(scope);await editor.load({type:'conversation',id:'C2'});first.resolve({items:[{id:'E1'}]});await old;
  assert.equal(editor.scope.value.id,'C2');assert.equal(editor.draft.config.target.conversationId,'C2');assert.equal(editor.endpoint.value,null);
});
test('F12 unmounted save response cannot retain secrets or alter state',async()=>{
  const pending=deferred();const {editor,endpoint}=fixture({webhooks:async()=>({items:[]}),createWebhook:()=>pending.promise});await editor.load(scope);
  const result=editor.save();editor.dispose();pending.resolve({endpoint,credential:{key:'do-not-resurrect'}});assert.equal(await result,false);assert.equal(editor.oneTimeKey,undefined);assert.equal(editor.endpoint.value,null);
});
test('F16 keyboard tabs wrap and Home/End do not fabricate click side effects',()=>{
  let prevented=0;const event=key=>({key,preventDefault(){prevented++;}});
  assert.equal(tabKey(event('ArrowLeft'),'connection',TRIGGER_TABS),'advanced');assert.equal(tabKey(event('Home'),'scripts',TRIGGER_TABS),'connection');assert.equal(tabKey(event('End'),'scripts',TRIGGER_TABS),'advanced');assert.equal(tabKey(event('Tab'),'scripts',TRIGGER_TABS),'scripts');assert.equal(prevented,3);
});

// Compile the actual SFCs, and execute their own action functions with deterministic IO.
const read=name=>fs.readFileSync(new URL(name,import.meta.url),'utf8');
function actionHarness(overrides={}) {
  const descriptor=parse(read('./WebhookEditor.vue')).descriptor;
  const source=descriptor.scriptSetup.content.replace(/^import .*;\n/gm,'');
  const calls=[],messages=[];
  const ctx={normalizedRunDefaults,updateRunDefault,computed,ref,reactive,nextTick,createWebhookEditor,TRIGGER_TABS,parseLines:value=>value.split(','),statusLabel:x=>x,tabKey,targetTree,
    defineProps:()=>({scope,title:'Orders',active:true}),defineEmits:()=>()=>{},defineExpose:()=>{},watch:()=>{},onBeforeUnmount:()=>{},
    Api:{},
    ElMessage:{success:value=>messages.push(value),warning:value=>messages.push(value)},ElMessageBox:{confirm:async()=>{}},
    AbortController,crypto:{randomUUID:()=> 'request1'},navigator:{clipboard:{writeText:async()=>{throw new Error('denied');}}},window:{addEventListener(){},removeEventListener(){}},console,...overrides,
  };vm.createContext(ctx);vm.runInContext(source,ctx);return {ctx,calls,messages,run:code=>vm.runInContext(code,ctx)};
}
test('F04 invalid result-schema text stays dirty and blocks saving the previous schema',async()=>{
  const {run,calls}=actionHarness();run("editor.adopt(null);updateSchema('{invalid');tab.value='scripts'");
  assert.equal(run('schemaText.value'),'{invalid');assert.equal(run('dirty.value'),true);assert.equal(await run('save()'),false);assert.equal(run('tab.value'),'advanced');
  run("updateSchema('{\"type\":\"object\"}')");assert.equal(run('schemaError.value'),'');assert.equal(run('draft.config.processing.resultSchema.type'),'object');
  run("updateSchema('[1]')");assert.match(run('schemaError.value'),/对象/);run('discardDraft()');assert.equal(run('schemaText.value'),'');assert.equal(run('dirty.value'),false);
});
test('F04 schema typed while a save is pending survives the older successful response',async()=>{
  const pending=deferred();const io=fixture({updateWebhook:()=>pending.promise});const {run}=actionHarness({Api:io.api});await run('editor.load(props.scope)');run("draft.name='Submitted'");
  const result=run('save()');run("updateSchema('{typing')");pending.resolve({endpoint:{...io.endpoint,name:'Submitted',revision:2}});assert.equal(await result,true);assert.equal(run('schemaText.value'),'{typing');assert.equal(run('dirty.value'),true);
});
test('F06 saved editor can leave without a key acknowledgement; URL copy failure remains explicit',async()=>{
  const {run,messages}=actionHarness();run('editor.adopt(null)');assert.equal(await run('canLeave()'),true);
  await run("copy('/webhook/fixture')");assert.match(messages[0],/复制失败/);
});
test('F04/F15 a parent rerender with the same scope never reloads or resets the actual editor',async()=>{
  const props=reactive({scope:{...scope},title:'Orders',active:true});const effects=effectScope();
  const io=fixture();let loads=0;const api={...io.api,webhooks:async()=>{loads++;return {items:[]};},webhookTargets:async()=>({items:[]})};
  const {run}=effects.run(()=>actionHarness({watch,defineProps:()=>props,Api:api}));
  await nextTick();await nextTick();
  run("tab.value='scripts';draft.config.pre.code='unsaved indentation\\n  keep'");
  props.scope={...scope};await nextTick();await nextTick();
  assert.equal(loads,1);assert.equal(run('tab.value'),'scripts');assert.equal(run('draft.config.pre.code'),'unsaved indentation\n  keep');
  effects.stop();run('editor.dispose()');
});
