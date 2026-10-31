import * as promptPolicy from '../promptPolicy.js';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {parse,compileScript} from '@vue/compiler-sfc';
import * as Vue from 'vue';
import {renderToString} from 'vue/server-renderer';
import * as cfg from './webhookConfig.js';
import * as runDefaults from '../folderRunDefaults.js';
const displaySource=fs.readFileSync(new URL('../../views/consoleView/display.js',import.meta.url),'utf8');
const displayScope={};vm.createContext(displayScope);vm.runInContext(displaySource.slice(displaySource.indexOf('export function modelThinkingLevels('),displaySource.indexOf('export function thinkingDesc(')).replaceAll('export function','function'),displayScope);
const {modelThinkingLevels,thinkingLabel}=displayScope;
import {createWebhookEditor} from './useWebhookEditor.js';
const {computed,ref,reactive,nextTick,watch,effectScope,proxyRefs,compile,createSSRApp,h}=Vue;
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {resolve,promise};};
const clone=value=>JSON.parse(JSON.stringify(value));
const message={success(){},warning(){},error(){}};
const shell={inheritAttrs:false,setup(_,{slots}){return ()=>h('div',[slots.default?.(),slots.footer?.()]);}};
const components={PromptPolicyEditor:shell,PromptImpactDialog:shell,AdaptiveMdEditor:shell,WebhookKey:shell,WebhookField:shell,WebhookScriptEditor:shell,WebhookCollectionSettings:shell,WebhookStatistics:shell,WebhookEventDialog:shell,WebhookStatisticsConfig:shell,ModelSettings:shell,WebhookAdvancedSettings:shell,WebhookEditor:shell};
let seq=0;
function harness(name,{props={},api={},live=false,extra={},confirm=async()=>{}}={}) {
 const descriptor=parse(fs.readFileSync(new URL(name,import.meta.url),'utf8')).descriptor;
 const cleanup=[],inputs=reactive(props),scope=effectScope();
 const ctx={...Vue,...promptPolicy,...cfg,...runDefaults,modelThinkingLevels,thinkingLabel,...components,watch:live?watch:()=>{},AbortController,crypto:{randomUUID:()=>`ui-intent-${++seq}`},
 window:{addEventListener(){},removeEventListener(){}},navigator:{},console,onBeforeUnmount:fn=>cleanup.push(fn),
 inject:(_key,fallback)=>fallback,defineProps:()=>inputs,defineEmits:()=>extra.onEmit || (()=>{}),defineExpose(){},
 ElMessage:message,ElMessageBox:{confirm},Api:api,apiError:e=>e.message,createWebhookEditor,...extra};
 vm.createContext(ctx);scope.run(()=>vm.runInContext(descriptor.scriptSetup.content.replace(/^import .*;\n/gm,''),ctx));
 const run=code=>vm.runInContext(code,ctx);
 function bindings(){const all=compileScript(descriptor,{id:'regression'}).bindings;for(const [key,type] of Object.entries(all))if(type==='props'&&!Object.hasOwn(inputs,key))inputs[key]=undefined;const names=Object.keys(all).filter(key=>all[key]!=='props');return proxyRefs({...run(`({${names.join(',')}})`),...inputs});}
 async function html(children={}) {
   const values=bindings();Object.assign(values,{$slots:{}},children); const draw=compile(descriptor.template.content);
   const app=createSSRApp({render(){return draw.call(this,values,[]);}});
   for(const name of ['ElDialog','ElButton','ElInput','ElSelect','ElOption','ElSkeleton','ElSwitch','ElCheckbox','ElCheckboxGroup','ElRadioGroup','ElRadio','ElRadioButton','ElTag','ElTreeSelect']) if (!children[name]) app.component(name,shell);
   for(const [name,component] of Object.entries({...components,...children}))app.component(name,component);
   app.config.warnHandler=()=>{};return renderToString(app);
 }
 return {ctx,run,html,props:inputs,close(){cleanup.forEach(fn=>fn());scope.stop();}};
}
const scope={type:'folder',id:'F1'};
const endpoint={id:'E-UI',scope,revision:1,controlRevision:0,enabled:false,name:'Orders',description:'',config:cfg.defaultConfig(scope)};
const api={webhooks:async()=>({items:[{id:endpoint.id}]}),webhook:async()=>({endpoint:clone(endpoint)}),webhookEnvironment:async()=>({}),webhookTargets:async()=>({items:[]})};


test('UI06/07 target mode emits null; folder leaf disabled through actual Element Plus handler and value defense',async()=>{
 const ed=harness('./WebhookEditor.vue',{props:{scope},api});ed.run("draft.config.target={mode:'fixedConversation',conversationId:'C1'};selectTargetMode('newConversation')");
 assert.equal(ed.run('draft.config.target.conversationId'),null);assert.deepEqual(cfg.configErrors(clone(ed.run('draft.config'))),[]);
 const {useTree}=await import('element-plus/es/components/tree-select/src/tree.mjs');
 const data=cfg.targetTree([{type:'folder',id:'empty',name:'Empty'},{type:'conversation',id:'C1',title:'Target'}]);let selected=null;
 const tree=useTree(reactive({data,cacheData:[],props:{},modelValue:null,showCheckbox:false,checkStrictly:false,renderAfterExpand:false}),{attrs:{},slots:{},emit(){}},{select:ref({states:{options:new Map([['folder:empty',{value:'folder:empty'}]])},handleOptionSelect:value=>{selected=value.value;}}),tree:ref(null),key:ref('value')});
 tree.onNodeClick(data[0],{isLeaf:true},{});assert.equal(selected,null);
 ed.run("targets.value=[{type:'folder',id:'empty'},{type:'conversation',id:'C1'}];selectTarget('empty');selectTarget('folder:empty')");assert.equal(ed.run('draft.config.target.conversationId'),null);
 ed.run("selectTarget('C1')");assert.equal(ed.run('draft.config.target.conversationId'),'C1');ed.close();
});

test('UI04 saving folder properties calls the trigger leave guard and retains dialog if declined',async()=>{
 const source=parse(fs.readFileSync(new URL('../ConversationTree.vue',import.meta.url),'utf8')).descriptor.scriptSetup.content;
 const save=source.slice(source.indexOf('async function saveProperties()'),source.indexOf('async function loadAllFolders()'));
 let guards=0,writes=0;const ctx={...promptPolicy,propertyModelsLoaded:ref(false),runDefaultsChanged:ref(false),propertiesRequestGeneration:1,propertiesSaveGeneration:1,propertiesSaving:ref(false),propertiesDialog:ref(true),propertiesForm:{folderId:'F1',temporary:false,workspaceDir:'',promptMarkdown:'changed'},webhookEditor:ref({dirty:true,canLeave:async()=>{guards++;return false;}}),ElMessage:message,apiError:e=>e.message,Api:{conversationFolderPropertiesImpact:async()=>({affectedCount:0}),updateConversationFolderProperties:async()=>{writes++;return {folder:{}};}},chooseImpact:async()=>false,window:{dispatchEvent(){}},CustomEvent:class{},mergeLocatedFolders(){},emitRows(){}};
 vm.createContext(ctx);vm.runInContext(save,ctx);await vm.runInContext('saveProperties()',ctx);assert.equal(guards,1);assert.equal(writes,1);assert.equal(ctx.propertiesDialog.value,true);
 ctx.webhookEditor.value.canLeave=async()=>true;await vm.runInContext('saveProperties()',ctx);assert.equal(ctx.propertiesDialog.value,false);
});

test('UI09 all six nested limits show inherited/effective/hard values and local/remote errors locate the field',async()=>{
 const config=cfg.defaultConfig(scope),environment={limits:{ingress:{requestsPerMinute:6000,},queue:{maxEvents:10000,maxBytes:100000},concurrency:{preScripts:4,postScripts:3,autoModelRuns:2}}};
 for(const field of Object.keys(config.limits)){const info=cfg.endpointLimit(environment,config,field);assert.ok(info.defaultValue>0 && info.maximum>0 && info.effective>0);config.limits[field]=info.maximum+1;assert.ok(cfg.configErrors(config,{environment}).some(item=>item.path===`limits.${field}`));config.limits[field]=null;}
 config.target={mode:'fixedConversation',conversationId:'C1'};assert.equal(cfg.endpointLimit(environment,config,'autoModelConcurrency').effective,1);
 const ed=createWebhookEditor({...api,updateWebhook:async()=>{throw {response:{status:422,data:{code:'system_limit_exceeded',details:{field:'limits.pendingEvents',maximum:9}}}};}});await ed.load(scope);ed.draft.name='changed';assert.equal(await ed.save(),false);assert.equal(ed.errors.value[0].path,'limits.pendingEvents');assert.equal(ed.errors.value[0].tab,'advanced');ed.dispose();
});

test('UI01/05 detail reads own nextCursor and submits selected wait version, not event version',async()=>{
 const calls=[];const ed=harness('./WebhookEventDialog.vue',{props:{modelValue:true,eventId:'E'},api:{webhookEvent:async()=>({event:{eventId:'E',version:88,waitCandidates:[{waitId:'W2'}]}}),webhookWaits:async(params)=>{calls.push(params);return params.cursor?{items:[{waitId:'W2',version:7}],nextCursor:null}:{items:[{waitId:'W1',version:3}],nextCursor:'W1'};},controlWebhookWait:async(id,body)=>calls.push({id,...body})}});
 await ed.run('load()');await ed.run('moreWaits()');ed.run("selectedWait.value='W2'");await ed.run('resolveMatch()');
 assert.equal(calls[1].cursor,'W1');assert.equal(calls[2].id,'W2');assert.equal(calls[2].action,'resolve_match');assert.equal(calls[2].expectedVersion,7);assert.equal(calls[2].eventId,'E');ed.close();
});

test('UI05 overview endpoint and wait page cursors advance independently, filters reset both',async()=>{
 const calls=[];const ed=harness('../../views/WebhooksView.vue',{api:{webhooks:async p=>{calls.push(['entries',clone(p)]);return {items:[],nextCursor:'ENTRY'};},webhookStatistics:async()=>({overview:{}}),webhookWaits:async p=>{calls.push(['waits',clone(p)]);return {items:[],nextCursor:'WAIT'};},webhookTargets:async()=>({items:[]})}});
 await ed.run('load()');await ed.run('loadWaits()');ed.run('nextPage()');await ed.run('loadWaits()');ed.run('nextWaitsPage()');await nextTick();
 assert.ok(calls.some(([kind,p])=>kind==='entries'&&p.cursor==='ENTRY'));assert.ok(calls.some(([kind,p])=>kind==='waits'&&p.cursor==='WAIT'));assert.ok(!calls.some(([kind,p])=>kind==='waits'&&p.cursor==='ENTRY'));
 ed.run("scopeType.value='folder';scopeId.value='F1';filter()");await nextTick();assert.equal(calls.at(-1)[1].cursor,null);assert.equal(calls.at(-1)[1].scopeId,'F1');ed.close();
});

test('UI10/11/14 actual detail template retains exact raw text, framework disposition, script result and purged state',async()=>{
 const ed=harness('./WebhookEventDialog.vue',{props:{modelValue:true,eventId:'E'}});
 ed.ctx.fixture={eventId:'E',raw:{content:' {"duplicate":1,"duplicate":2} ',body:{duplicate:2}},receipts:[{source:'framework',disposition:'not_delivered',outcome:null}],stages:[{stage:'pre',attempts:[{attemptId:'T',resultStatus:'retained',result:{decision:'continue',reason:'REASON',aggregation_key:'GROUP'}}]}]};
 ed.run("event.value=fixture;tab.value='raw'");let html=await ed.html();assert.ok(html.includes(' {&quot;duplicate&quot;:1,&quot;duplicate&quot;:2} '));assert.ok(html.includes('解析后的正文（非原文）'));
 ed.run("tab.value='results'");html=await ed.html();assert.ok(html.includes('框架处置：未交付'));
 ed.run("tab.value='stages'");html=await ed.html();assert.ok(html.includes('REASON')&&html.includes('GROUP')&&html.includes('脚本声明'));
 ed.run("event.value.stages[0].attempts[0].resultStatus='purged'");html=await ed.html();assert.ok(html.includes('脚本结果快照已按保留策略清理'));assert.ok(!html.includes('GROUP'));ed.close();
});

test('UI12 script error expands narrow parameters and exposes actual matching field props',async()=>{
 const config=cfg.defaultConfig(scope);config.pre.enabled=true;config.pre.code='x';
 const ed=harness('./WebhookScriptEditor.vue',{props:{config,phase:'pre',environment:{},errors:[]},live:true});ed.props.errors=[{path:'pre.timeoutSeconds',message:'too long'}];await nextTick();assert.equal(ed.run('parametersOpen.value'),true);
 // Execute the parent's actual template and observe props delivered to the field (not source-string assertions).
 const fields=[];const field={props:['path','label','modelValue','error'],setup(props){return ()=>{fields.push(props.path);return h('input',{'data-field':props.path,value:props.modelValue,'aria-invalid':!!props.error});};}};
 let html=await ed.html({WebhookField:field});assert.ok(fields.includes('pre.timeoutSeconds'));assert.ok(html.includes('parameters-open'));
 ed.props.errors=[{path:'post.onOutcomes',message:'pick a result'}];ed.props.phase='post';await nextTick();html=await ed.html({WebhookField:field});assert.ok(html.includes('data-field="post.onOutcomes"'));assert.ok(html.includes('pick a result'));ed.close();
});

test('statistics separate reported/estimated/unclassified/unknown evidence and do not render unavailable zeros as business facts',async()=>{
 const ed=harness('./WebhookStatistics.vue',{props:{endpointId:'E'}});ed.ctx.fixture={range:{coverageStatus:'complete'},overview:{usage:{costUsd:.6,providerReportedCostUsd:.1,estimatedCostUsd:.2,unclassifiedCostUsd:.3,costSources:{providerReported:1,estimated:2,unclassified:3,unknown:4},unknownCostCalls:4,ledgerDeletedCalls:1,missingLedgerCalls:4,coverage:'incomplete',warnings:['estimated_cost','missing_billing_evidence']}}};ed.run('data.value=fixture');let html=await ed.html();
 for(const text of ['提供方实报 USD','本地估算 USD','来源未分类 USD','历史计费证据缺失','实报 1 次；估算 2 次','费用未知 4 次'])assert.ok(html.includes(text),text);
 ed.run("data.value={range:{coverageStatus:'unavailable',coverageStart:null,gaps:[]},overview:{accepted:0,modelBatches:0},warnings:['retention_gap:metrics']}");html=await ed.html();assert.ok(html.includes('无法判定业务数量'));assert.ok(!html.includes('wh-kpi-grid'));ed.close();
});


test('S01 scope name is the clean initial name and blank submit falls back without enabling',async()=>{
 let submitted;const ed=createWebhookEditor({...api,webhooks:async()=>({items:[]}),createWebhook:async body=>{submitted=body;return {endpoint:{...endpoint,...body}};}},{defaultName:()=> '当前目录'});
 await ed.load(scope);assert.equal(ed.draft.name,'当前目录');assert.equal(ed.dirty.value,false);ed.draft.name='  ';await ed.save();assert.equal(submitted.name,'当前目录');assert.equal(submitted.enabled,false);ed.dispose();
});

test('S02 actual target request retains scope and API sibling order; old responses cannot replace a new scope',async()=>{
 const first=deferred(),calls=[];const ed=harness('./WebhookEditor.vue',{props:{scope},api:{webhookTargets:async params=>{calls.push(params);return calls.length===1?first.promise:{items:[{type:'conversation',id:'new'}]};}}});
 const old=ed.run('loadTargets()');ed.props.scope={type:'conversation',id:'C2'};await ed.run('loadTargets()');first.resolve({items:[{type:'conversation',id:'old'}]});await old;
 assert.deepEqual(clone(calls.map(({scopeType,scopeId})=>({scopeType,scopeId}))),[{scopeType:'folder',scopeId:'F1'},{scopeType:'conversation',scopeId:'C2'}]);assert.equal(ed.run('targets.value[0].id'),'new');ed.close();
 const tree=cfg.targetTree([{type:'folder',id:'root',name:'Root'},{type:'folder',id:'z',parentId:'root',name:'Zulu'},{type:'conversation',id:'z1',parentId:'z',title:'Zulu'},{type:'conversation',id:'a1',parentId:'z',title:'Alpha'},{type:'folder',id:'a',parentId:'root',name:'Alpha'},{type:'conversation',id:'archive',parentId:'root',archived:true}]);
 assert.deepEqual(tree[0].children.map(x=>x.value),['folder:z','folder:a']);assert.deepEqual(tree[0].children[0].children.map(x=>x.value),['z1','a1']);
});

const modelOptions=[{key:'capable',label:'全能力模型',thinkingLevels:['low','high'],defaultThinkingLevel:'high',supportsFast:true},{key:'plain',label:'普通模型',thinkingLevels:[],supportsFast:false}];
test('S03 new-conversation model values inherit individually and explicit false survives; fixed mode clears overrides',async()=>{
 const ed=harness('./WebhookEditor.vue',{props:{scope},api:{rathOptions:async()=>({models:modelOptions}),conversationFolderProperties:async()=>({runDefaults:{local:{mainModel:'capable',mainFastMode:true},inherited:{mainThinkingLevel:'low'},fallback:{mainModel:'plain',mainFastMode:false}}})}});
 await ed.run('loadModelOptions()');assert.equal(ed.run('targetEffective.value.mainModel'),'capable');assert.equal(ed.run('targetEffective.value.mainThinkingLevel'),'low');assert.equal(ed.run('targetEffective.value.mainFastMode'),true);
 ed.run(`setTargetRunConfig('mainFastMode',${JSON.stringify(runDefaults.runDefaultOption(false))})`);assert.equal(ed.run('draft.config.target.runConfig.mainFastMode'),false);assert.equal(ed.run('targetEffective.value.mainFastMode'),false);assert.equal(ed.run('targetInherited.value.mainFastMode'),true);
 ed.run("setTargetRunConfig('mainFastMode','inherit')");assert.equal(ed.run('draft.config.target.runConfig.mainFastMode'),null);assert.equal(ed.run('targetEffective.value.mainFastMode'),true);
 ed.run(`setTargetRunConfig('mainModel',${JSON.stringify(runDefaults.runDefaultOption('plain'))})`);assert.equal(ed.run('targetEffective.value.mainThinkingLevel'),'off');assert.equal(ed.run('targetEffective.value.mainFastMode'),false);
 ed.run("selectTargetMode('fixedConversation')");assert.equal(ed.run('draft.config.target.runConfig'),null);ed.run("selectTargetMode('newConversation')");assert.deepEqual(clone(ed.run('draft.config.target.runConfig')),{mainModel:null,mainThinkingLevel:null,mainFastMode:null});ed.close();
});

test('S04 actual shared model template distinguishes false/inherit, capability controls and compression strategy',async()=>{
 const emitted=[],selects=[],options=[];
 const ed=harness('../ModelSettings.vue',{props:{modelValue:{mainModel:'capable',mainFastMode:false},effective:{mainModel:'capable',mainThinkingLevel:'low',mainFastMode:false,agentModel:''},inherited:{mainFastMode:true},models:modelOptions,inheritable:true,inheritLabel:'继承目录',disabled:false,showAgent:true,showStrategy:true},extra:{onEmit:(...args)=>emitted.push(args)}});
 const select={inheritAttrs:false,setup(_,{attrs,slots}){selects.push({...attrs});return ()=>h('select',slots.default?.());}},option={inheritAttrs:false,setup(_,{attrs}){options.push({...attrs});return ()=>h('option',attrs.label);}};
 let html=await ed.html({ElSelect:select,ElOption:option});assert.match(html,/上下文压缩/);assert.ok(selects.some(x=>x['data-run-default-field']==='contextStrategy'));assert.ok(options.some(x=>x.value==='inherit' && x.label==='继承目录 · 开启'));assert.ok(options.some(x=>x.value==='value:false' && x.label==='关闭'));assert.ok(selects.some(x=>x['data-run-default-field']==='mainFastMode' && x['model-value']==='value:false'));
 ed.run("changed('mainFastMode','inherit')");assert.deepEqual(emitted,[['change','mainFastMode','inherit']]);
 ed.props.effective={mainModel:'plain',agentModel:'plain'};selects.length=0;html=await ed.html({ElSelect:select});assert.ok(!selects.some(x=>['mainThinkingLevel','mainFastMode','agentThinkLevel','agentFastMode'].includes(x['data-run-default-field'])));assert.match(html,/当前模型不支持/);ed.close();
});

test('property template disables context and model controls plus save after a failed initial read',async()=>{
 const captured={};const ed=harness('../ConversationPropertiesDialog.vue',{props:{modelValue:true,conversation:{conversationUuid:'C1'}},api:{conversationProperties:async()=>{throw Error('offline');},rathOptions:async()=>({models:modelOptions})},extra:{runConfigFromResponse:()=>null}});
 await ed.run('load()');
 const control=name=>({inheritAttrs:false,setup(_,{attrs}){captured[name]={...attrs};return ()=>h('div');}});
 const button={inheritAttrs:false,setup(_,{attrs,slots}){return ()=>h('button',attrs,slots.default?.());}};
 const html=await ed.html({PromptPolicyEditor:control('context'),ModelSettings:control('models'),ElButton:button});
 assert.equal(captured.context.disabled,true);assert.equal(captured.models.disabled,true);assert.match(html,/<button[^>]*disabled[^>]*>保存上下文配置<\/button>/);ed.close();
});

test('shared thinking selector renders effective main level and truthful Agent follow label without changing inheritance values',async()=>{
 const ed=harness('../ModelSettings.vue',{props:{modelValue:{mainModel:'capable',mainThinkingLevel:'low',agentModel:'',agentThinkLevel:'',agentFastMode:null},effective:{},models:modelOptions,inheritable:true,inherited:{agentThinkLevel:''},inheritLabel:'继承目录',showAgent:true,showStrategy:true}});
 const option={inheritAttrs:false,setup(_,{attrs}){return ()=>h('option',{value:attrs.value},attrs.label);}};
 const html=await ed.html({ElOption:option});assert.doesNotMatch(html,/当前不可用/);assert.match(html,/跟随主会话（不支持时用模型默认）/);
 assert.equal(ed.run("options('agent','Thinking')[0].value"),'');assert.equal(ed.run("label('agentThinkLevel','')"),'跟随主会话（不支持时用模型默认）');assert.equal(ed.run("current('agentThinkLevel')"),'value:""');assert.equal(ed.run("inheritText('agentThinkLevel')"),'继承目录 · 跟随主会话（不支持时用模型默认）');ed.close();
});

test('S05 local text stays editable and inherited preview is passed separately to the policy editor',async()=>{
 const captured=[];const ed=harness('../ConversationPropertiesDialog.vue',{props:{modelValue:true,conversation:{conversationUuid:'C1'}},extra:{runConfigFromResponse:()=>({})}});
 ed.run("contextReady.value=true;context.value={contextText:'',inheritedContext:'parent text'}");
 const editor={inheritAttrs:false,setup(_,{attrs}){captured.push({...attrs});return ()=>h('textarea',attrs.modelValue.text);}};
 let html=await ed.html({PromptPolicyEditor:editor});assert.equal(captured.length,1);assert.equal(captured[0].modelValue.text,'');assert.equal(captured[0].disabled,false);assert.equal(captured[0]['inherited-policy'].text,'parent text');assert.ok(!html.includes('当前系统快照'));
 ed.run("contextEditorText.value='new draft'");assert.equal(ed.run('contextEditorText.value'),'new draft');captured.length=0;await ed.html({PromptPolicyEditor:editor});assert.equal(captured[0].modelValue.text,'new draft');assert.equal(captured[0]['inherited-policy'].text,'parent text');ed.close();
});

test('S06 real advanced and collection fields round-trip MB decimals, null, errors and retained advanced capabilities',async()=>{
 const config=cfg.defaultConfig(scope),environment={limits:{queue:{maxBytes:4*cfg.BYTES_PER_MB},batching:{maxBatchBytes:2*cfg.BYTES_PER_MB}}},fields=[];
 const field={inheritAttrs:false,setup(_,{attrs}){fields.push({...attrs});return ()=>h('label',attrs.label);}};
 const ed=harness('./WebhookAdvancedSettings.vue',{props:{config,environment,errors:[],schemaText:'',schemaError:''}});await ed.html({WebhookField:field});let capacity=fields.find(x=>x.path==='limits.pendingBytes');assert.equal(capacity.unit,'MB');assert.equal(capacity.step,'any');assert.equal(capacity.modelValue ?? capacity['model-value'],null);assert.match(capacity.hint,/系统 4 MB/);capacity['onUpdate:modelValue'](1.125);assert.equal(config.limits.pendingBytes,1179648);assert.equal(ed.run("limitValue('pendingBytes')"),1.125);capacity['onUpdate:modelValue']('');assert.equal(config.limits.pendingBytes,null);capacity['onUpdate:modelValue'](0);assert.equal(config.limits.pendingBytes,0);assert.match(cfg.configErrors(config).find(x=>x.path==='limits.pendingBytes').message,/MB/);
 for(const path of ['processing.resultSchema','notifications.digestSeconds'])assert.ok(fields.some(x=>x.path===path));
 fields.length=0;const collection=harness('./WebhookCollectionSettings.vue',{props:{config,environment,errors:[]}});await collection.html({WebhookField:field});capacity=fields.find(x=>x.path==='batching.maxBytes');assert.equal(capacity.unit,'MB');assert.equal(capacity['model-value'],.0625);capacity['onUpdate:modelValue'](.5);assert.equal(config.batching.maxBytes,524288);capacity['onUpdate:modelValue']('');assert.equal(config.batching.maxBytes,null);assert.ok(cfg.configErrors(config).some(x=>x.path==='batching.maxBytes'));ed.close();collection.close();
});

test('S07 config and submission drop deleted product keys while preserving template, schema, notification and scripts',async()=>{
 const incoming={...cfg.defaultConfig(scope),budget:{maxCostUsd:1}};Object.assign(incoming.pre,{sourceMode:'file',entryFile:'/old',cwd:'/old',env:[{secretRef:'old'}]});Object.assign(incoming.batching,{groupBy:['body.id'],missingGroupValue:'hold'});incoming.limits.burst=9;incoming.processing={...incoming.processing,eventTemplate:'[[ events ]]',resultSchema:{type:'object',properties:{status:{type:'string'}}}};incoming.notifications={policy:'errorsDigest',digestSeconds:60};incoming.pre.code='saved code';
 let submitted;const ed=createWebhookEditor({...api,webhook:async()=>({endpoint:{...endpoint,config:incoming}}),updateWebhook:async(_id,body)=>{submitted=body;return {endpoint:{...endpoint,...body}};}});await ed.load(scope);assert.equal(await ed.save(),true);
 assert.ok(!('budget' in submitted.config));assert.ok(!('groupBy' in submitted.config.batching));assert.ok(!('missingGroupValue' in submitted.config.batching));assert.ok(!('burst' in submitted.config.limits));for(const key of ['sourceMode','entryFile','cwd','env','interpreter'])assert.ok(!(key in submitted.config.pre));assert.equal(submitted.config.pre.code,'saved code');assert.deepEqual({...submitted.config.processing,allowServerChanges:false},incoming.processing);assert.equal(Object.hasOwn(submitted.config.processing,'allowServerChanges'),false);assert.deepEqual(submitted.config.notifications,incoming.notifications);ed.dispose();
});

test('S08 receive status remains understandable while processing is paused',()=>{
 const ed=harness('./WebhookEditor.vue',{props:{scope},api});ed.run("endpoint.value={effectiveStatus:'paused'}");assert.equal(ed.run('receivingStatus.value'),'正在接收 · 处理已暂停');ed.run("endpoint.value={effectiveStatus:'disabled'}");assert.equal(ed.run('receivingStatus.value'),'不接收 · 入口未启用');ed.close();
});
test('U2 an endpoint may change servers and instances only once ticked; the box explains the risk and is submitted',()=>{
  const config=cfg.defaultConfig({type:'folder',id:'F1'});
  assert.equal(config.processing.allowServerChanges,false);
  config.processing.allowServerChanges=true;assert.equal(cfg.submittedConfig(config).processing.allowServerChanges,true);
  const template=parse(fs.readFileSync(new URL('./WebhookEditor.vue',import.meta.url),'utf8')).descriptor.template.content;
  assert.match(template,/<el-checkbox v-model="draft\.config\.processing\.allowServerChanges">允许改动服务器和实例<\/el-checkbox>/);
  assert.match(template,/'事件处理无人值守，可能被事件内容或服务器返回的内容误导而执行改动/);
  assert.match(template,/'未勾选时，处理事件时只能查看服务器和实例。'/);
});
