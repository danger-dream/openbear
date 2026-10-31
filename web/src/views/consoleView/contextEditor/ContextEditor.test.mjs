import test, {after} from 'node:test';
import assert from 'node:assert/strict';
import {readFile, unlink, writeFile} from 'node:fs/promises';
import {compileScript, compileTemplate, parse} from '@vue/compiler-sfc';
import {effectScope, nextTick, reactive} from 'vue';
import {Api} from '../../../api.js';
const sourceFile = new URL('../ContextEditor.vue', import.meta.url);
const generated = new URL(`../.ContextEditor.test-${process.pid}.mjs`, import.meta.url);
const text = await readFile(sourceFile,'utf8');
const styles = await readFile(new URL('./style.css', import.meta.url),'utf8');
const {descriptor,errors}=parse(text,{filename:'ContextEditor.vue'});
assert.deepEqual(errors,[]);
assert.deepEqual(compileTemplate({source:descriptor.template.content, filename:'ContextEditor.vue',id:'ce-test'}).errors,[]);
let Component;
try { const compiled=compileScript(descriptor,{id:'ce-test'}).content.replace("import './contextEditor/style.css';",'').replace("onBeforeUnmount(() => { disposed = true; generation++; emit('open-change', false); });",'void 0;');
  await writeFile(generated,compiled); ({default:Component}=await import(generated.href));
} finally { await unlink(generated).catch(()=>{}); }
const original={};for(const k of ['contextEditor','previewContextEditor','saveContextEditorDraft','createContextEditorBranch','saveContextEditor']) original[k]=Api[k];
after(()=>{for(const [k,v] of Object.entries(original)) Api[k]=v;});
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
function sample(id='u'){return {format:'openbear-context-edit/1',revision:1,origin:{protocol:'responses'},runtime:{frozen:true},system:`system-${id}`,tools:[],entries:[{entryId:id,message:{role:'user',content:'hello'}}]};}
function reply(key, draft=null){return {ok:true,baseline:sample(key),snapshotToken:`token-${key}`,draft,models:[{label:'OpenAI',model:'model',protocol:'responses'}],targetModel:'model'};}
function setup(key='one'){const scope=effectScope(), props=reactive({conversationUuid:key,busy:false}), events=[];let exposed;
 const vm=scope.run(()=>Component.setup(props,{expose: value=>{exposed=value;},emit:(...args)=>events.push(args)}));return {scope,props,vm,exposed,events};}
const settle=async()=>{await Promise.resolve();await nextTick();};
test('keyboard-sized editor uses actual available bounds, restores normal layout, and releases observers', async()=>{
 const priorWindow=globalThis.window, priorObserver=globalThis.ResizeObserver;let observer,disconnected=false;
 let bounds={width:390,height:796},coarse=true;
 const win=Object.assign(new EventTarget(),{visualViewport:new EventTarget(),matchMedia:()=>({matches:coarse})});
 globalThis.window=win;globalThis.ResizeObserver=class {constructor(fn){observer=fn;}observe(){}disconnect(){disconnected=true;}};
 const {scope,vm}=setup();
 try {
  vm.editorShell.value={getBoundingClientRect:()=>bounds};await settle();assert.equal(vm.compactViewport.value,false);
  for(const width of [320,360,375,390,412,430,844]){bounds={width,height:212};observer();assert.equal(vm.compactViewport.value,true);}
  vm.compactPanel.value='outline';bounds={width:390,height:796};observer();assert.equal(vm.compactViewport.value,false);assert.equal(vm.compactPanel.value,'document');
  coarse=false;bounds={width:1280,height:390};observer();assert.equal(vm.compactViewport.value,false);
  scope.stop();assert.equal(disconnected,true);
 }finally{scope.stop();if(priorWindow===undefined)delete globalThis.window;else globalThis.window=priorWindow;if(priorObserver===undefined)delete globalThis.ResizeObserver;else globalThis.ResizeObserver=priorObserver;}
});
test('compact panel navigation preserves draft and invalid raw edits; choosing a message returns to its editor',async()=>{
 Api.contextEditor=async key=>reply(key);const {scope,vm,exposed}=setup();
 try{
  exposed.open();await settle();vm.setField('system','retained draft');vm.compactViewport.value=true;
  vm.switchCompactPanel('controls');assert.equal(vm.compactPanel.value,'controls');assert.equal(vm.doc.value.system,'retained draft');
  vm.switchCompactPanel('outline');vm.choose('one');assert.equal(vm.selected.value,'one');assert.equal(vm.compactPanel.value,'document');
  vm.switchTab('raw');vm.raw.value='invalid';vm.switchCompactPanel('controls');assert.equal(vm.compactPanel.value,'document');assert.ok(vm.rawError.value);
  vm.raw.value='{"role":"user","content":"edited"}';vm.switchCompactPanel('controls');assert.equal(vm.doc.value.entries[0].message.content,'edited');assert.equal(vm.compactPanel.value,'controls');
 }finally{scope.stop();}
});
test('complete Claude native blocks use existing text/call editors without mixing Responses items',async()=>{
 const data=reply('one');data.baseline.origin.protocol='claude';data.models[0].protocol='claude';
 data.baseline.entries=[{entryId:'a',message:{role:'assistant',content:'AB',tool_calls:[{id:'c1',name:'Read',arguments:'{ "path": "old" }'}],native_output_items:[{type:'thinking',thinking:'keep',signature:'sig'},{type:'text',text:'A',citations:[{keep:1}]},{type:'tool_use',id:'c1',name:'Read',input:{path:'old'},vendor:'keep'},{type:'text',text:'B'}]}}];
 Api.contextEditor=async()=>data;const {scope,vm,exposed}=setup();
 try{
  exposed.open();await settle();vm.choose('a');assert.equal(vm.sequence.value.ordered,true);assert.match(vm.orderNote.value,/调整会写入实际请求/);
  vm.openBlock(vm.sequence.value.rows[1]);assert.equal(vm.entityEditor.value.json,false);assert.equal(vm.entityEditor.value.text,'A');vm.entityEditor.value.text='C';vm.saveEntity();assert.equal(vm.entry.value.message.content,'CB');
  assert.deepEqual(vm.entry.value.message.native_output_items[1].citations,[{keep:1}]);
  vm.openBlock(vm.sequence.value.rows[2]);assert.equal(vm.entityEditor.value.kind,'call');vm.entityEditor.value.text='{"path":"new"}';vm.saveEntity();
  assert.deepEqual(vm.entry.value.message.native_output_items[2].input,{path:'new'});assert.equal(vm.entry.value.message.native_output_items[2].vendor,'keep');
  vm.revealBody();assert.equal(vm.entityEditor.value.data.type,'text');vm.entityEditor.value.text='D';vm.saveEntity();assert.equal(vm.entry.value.message.content,'CBD');
  vm.addCall();vm.entityEditor.value.data.name='Read';vm.entityEditor.value.text='{}';vm.saveEntity();assert.equal(vm.entry.value.message.native_output_items.at(-1).type,'tool_use');
  assert.deepEqual(vm.entry.value.message.native_output_items[0],{type:'thinking',thinking:'keep',signature:'sig'});assert.equal(vm.entityError.value,'');
 }finally{scope.stop();}
});

test('older Safari can add a message and tool call using getRandomValues without randomUUID', async()=>{
 const descriptor=Object.getOwnPropertyDescriptor(globalThis,'crypto'), native=globalThis.crypto;
 Object.defineProperty(globalThis,'crypto',{configurable:true,value:{getRandomValues:bytes=>native.getRandomValues(bytes)}});
 const {scope,vm,exposed}=setup();
 try {
  Api.contextEditor=async key=>reply(key);exposed.open();await settle();
  vm.addEntry('assistant');assert.match(vm.selected.value,/^edited-[0-9a-f-]{14}4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(vm.entry.value.message.role,'assistant');
  vm.addCall();assert.match(vm.entityEditor.value.data.id,/^call_[0-9a-f-]{14}4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  vm.entityEditor.value.data.name='Read';vm.entityEditor.value.text='{}';vm.saveEntity();
  assert.equal(vm.entityError.value,'');assert.equal(vm.entry.value.message.tool_calls[0].name,'Read');
 } finally {scope.stop();Object.defineProperty(globalThis,'crypto',descriptor);}
});
test('switching reads nothing until opened; drafts are isolated and late responses never overwrite another conversation',async()=>{
 const first=deferred(),reads=[];Api.contextEditor=key=>{reads.push(key);return key==='one'?first.promise:Promise.resolve(reply(key));};
 const {scope,props,vm,exposed}=setup();exposed.open();assert.equal(vm.loading.value,true);
 props.conversationUuid='two';await settle();assert.deepEqual(reads,['one']);assert.equal(vm.opened.value,false);assert.equal(vm.loading.value,false);
 exposed.open();await settle();assert.deepEqual(reads,['one','two']);assert.equal(vm.doc.value.system,'system-two');
 first.resolve(reply('one'));await settle();assert.equal(vm.doc.value.system,'system-two');
 vm.setField('system','draft-two');assert.equal(vm.doc.value.system,'draft-two');exposed.close();exposed.open();assert.equal(vm.doc.value.system,'draft-two');
 props.conversationUuid='one';await settle();exposed.open();await settle();assert.equal(vm.doc.value.system,'system-one');
 props.conversationUuid='two';await settle();assert.equal(vm.doc.value.system,'draft-two');scope.stop();
});
test('server preview is prerequisite; mutations/target changes invalidate it; creation only emits actual server conversation',async()=>{
 Api.contextEditor=key=>Promise.resolve(reply(key));const previews=[];Api.previewContextEditor=async(key,data)=>{previews.push({key,data});return {ok:true,issues:[],payload:{model:data.targetModel,input:[]},document:data.working};};
 const created=[];Api.createContextEditorBranch=async(key,data)=>{created.push({key,data});return {ok:true,conversation:{uuid:'new-branch'}};};
 const {scope,vm,exposed,events}=setup();exposed.open();await settle();
 vm.branchTitle.value='branch';await vm.createBranch();assert.equal(created.length,0);assert.match(vm.error.value,/预览/);
 await vm.preview();assert.equal(vm.previewReady.value,true);assert.deepEqual(previews[0].data.working.entries[0].message.content,'hello');
 vm.setField('system','modified');assert.equal(vm.previewReady.value,false);
 await vm.createBranch();assert.equal(created.length,0);
 await vm.preview();vm.setting('targetModel','other');assert.equal(vm.previewReady.value,false);
 await vm.preview();await vm.createBranch();assert.equal(created.length,1);assert.equal(created[0].data.working.system,'modified');assert.deepEqual(events.filter(e=>e[0]==='created'),[['created',{uuid:'new-branch'}]]);assert.equal(vm.opened.value,false);scope.stop();
});
test('late preview invalidated by edit; server errors/raw/busy block creation; 409 preserves working',async()=>{
 Api.contextEditor=key=>Promise.resolve(reply(key));const late=deferred();Api.previewContextEditor=()=>late.promise;
 const {scope,vm,props,exposed}=setup();exposed.open();await settle();
 const preview=vm.preview();vm.setField('system','keep');late.resolve({ok:true,issues:[],payload:{input:['stale']}});await preview;assert.equal(vm.previewReady.value,false);assert.equal(vm.doc.value.system,'keep');
 Api.previewContextEditor=async()=>({ok:true,issues:[{severity:'error',text:'blocked'}],payload:null,document:null});await vm.preview();assert.equal(vm.serverErrors.value.length,1);
 vm.branchTitle.value='branch';Api.createContextEditorBranch=()=>{throw Error('must not call');};await vm.createBranch();assert.match(vm.error.value,/载荷|错误/);
 vm.setting('mode','raw');await vm.createBranch();assert.match(vm.error.value,/原始实验稿/);
 Api.saveContextEditorDraft=async()=>{throw {response:{status:409,data:{message:'revision conflict'}}};};await vm.saveDraft();assert.match(vm.error.value,/409/);assert.equal(vm.doc.value.system,'keep');
 props.busy=true;exposed.close();exposed.open();assert.equal(vm.opened.value,false);scope.stop();
});
test('server draft revision is used verbatim; late save cannot overwrite another session',async()=>{
 const draft={baseline:sample('one'),working:sample('one'),snapshotToken:'source-1',revision:7,mode:'raw',targetModel:'model'};
 draft.working.system='restored';Api.contextEditor=key=>Promise.resolve(key==='one'?reply(key,draft):reply(key));
 const save=deferred(),requests=[];Api.saveContextEditorDraft=(key,data)=>{requests.push({key,data});return save.promise;};
 const {scope,props,vm,exposed}=setup();exposed.open();await settle();
 assert.equal(vm.doc.value.system,'restored');assert.equal(vm.state.value.mode,'raw');
 vm.setField('system','new draft');const p=vm.saveDraft();assert.equal(requests[0].data.revision,7);assert.equal(requests[0].data.snapshotToken,'source-1');
 props.conversationUuid='two';await settle();exposed.open();await settle();save.resolve({ok:true,draft:{revision:8}});await p;
 assert.equal(vm.doc.value.system,'system-two');assert.equal(vm.state.value.revision,0);
 props.conversationUuid='one';await settle();exposed.open();await settle();assert.equal(vm.doc.value.system,'new draft');assert.equal(vm.state.value.revision,8);scope.stop();
});
test('preview retains actionable validation without a permanent inspector, including null payload and local repair navigation',async()=>{
 Api.contextEditor=key=>Promise.resolve(reply(key));
 Api.previewContextEditor=async()=>({ok:true,payload:null,document:null,issues:[
   {severity:'error',code:'INVALID_ROLE',text:'请修复用户消息',entryId:'one',path:'entries[0].message.role'},
   {severity:'warning',code:'PREFIX',text:'系统前缀已改变',path:'system'},
 ]});
 const {scope,vm,exposed}=setup();exposed.open();await settle();await vm.preview();
 assert.equal(vm.tab.value,'preview');assert.equal(vm.payloadText.value,'服务器未返回可预览的 payload');
 assert.deepEqual(vm.serverIssues.value.map(i=>i.text),['请修复用户消息','系统前缀已改变']);
 assert.equal(vm.issueTarget(vm.serverIssues.value[0]),'one');vm.inspectIssue(vm.serverIssues.value[0]);
 assert.equal(vm.selected.value,'one');assert.equal(vm.tab.value,'structured');
 vm.inspectIssue(vm.serverIssues.value[1]);assert.equal(vm.selected.value,'system');
 vm.choose('one');vm.setField('role','invalid');vm.switchTab('preview');
 assert.equal(vm.previewReady.value,false);assert.ok(vm.hints.value.some(i=>i.entryId==='one' && i.severity==='error'));
 const local=vm.hints.value.find(i=>i.entryId==='one');vm.inspectIssue(local);
 assert.equal(vm.selected.value,'one');assert.equal(vm.tab.value,'structured');
 assert.match(text,/<section aria-label="预览校验详情">[\s\S]*v-for="\(issue,i\) in serverIssues"[\s\S]*v-for="\(issue,i\) in hints"/);
 assert.match(text,/v-if="issue\.path">\{\{ issue\.path \}\}/);
 assert.match(text,/@click="inspectIssue\(issue\)"/);
 assert.ok(!text.includes('ce-inspector'));
 assert.match(styles,/grid-template-columns: clamp\(230px,23%,300px\) minmax\(0,1fr\)/);
 assert.match(styles,/\.ce-preview-validation \{/);
 scope.stop();
});
test('save original requires fresh preview and confirmation, keeps identity and updates the clean baseline',async()=>{
 Api.contextEditor=async key=>reply(key);Api.previewContextEditor=async(key,data)=>({ok:true,payload:{input:[]},issues:[],document:data.working});
 const requests=[],wait=deferred();Api.saveContextEditor=(key,data)=>{requests.push({key,data});return wait.promise;};
 const {scope,vm,exposed,events}=setup();exposed.open();await settle();
 vm.setField('system','edited');vm.askSaveOriginal();assert.equal(vm.dialog.value,null);assert.match(vm.error.value,/预览/);
 await vm.preview();assert.equal(vm.saveReady.value,true);vm.askSaveOriginal();assert.equal(vm.dialog.value.kind,'apply');assert.equal(requests.length,0);
 const saving=vm.confirmDelete();assert.equal(requests.length,1);assert.equal(requests[0].key,'one');assert.equal(requests[0].data.revision,0);assert.equal(requests[0].data.snapshotToken,'token-one');
 vm.setField('system','must not race');assert.equal(vm.doc.value.system,'edited');assert.equal(vm.saveReady.value,false);
 const result={...reply('one'),baseline:structuredClone(requests[0].data.working),snapshotToken:'saved-token',draft:{revision:1},conversation:{conversationUuid:'one'}};wait.resolve(result);await saving;
 assert.equal(vm.doc.value.system,'edited');assert.equal(vm.state.value.snapshotToken,'saved-token');assert.equal(vm.state.value.revision,1);assert.equal(vm.changes.value.length,0);assert.equal(vm.opened.value,true);assert.equal(vm.branchTitle.value,'');
 assert.deepEqual(events.filter(e=>e[0]==='saved'),[['saved',{conversationUuid:'one'}]]);assert.equal(events.filter(e=>e[0]==='created').length,0);assert.match(vm.notice.value,/原始聊天/);
 assert.match(text,/保存副本/);assert.match(text,/保存到原会话/);scope.stop();
});
test('original save rejects busy, raw, stale and invalid contexts; conflicts preserve the working copy',async()=>{
 Api.contextEditor=async key=>reply(key);Api.previewContextEditor=async()=>({ok:true,payload:{input:[]},issues:[]});let calls=0;
 Api.saveContextEditor=async()=>{calls++;throw {response:{status:409,data:{error:'context_source_changed'}}};};
 const {scope,vm,props,exposed}=setup();exposed.open();await settle();vm.setField('system','keep');await vm.preview();
 props.busy=true;await vm.saveOriginal();assert.equal(calls,0);props.busy=false;
 vm.setting('mode','raw');await vm.saveOriginal();assert.equal(calls,0);vm.setting('mode','compatible');await vm.preview();
 vm.askSaveOriginal();vm.setField('system','new edit');await vm.confirmDelete();assert.equal(calls,0);assert.match(vm.error.value,/预览/);
 await vm.preview();await vm.saveOriginal();assert.equal(calls,1);assert.match(vm.error.value,/409/);assert.equal(vm.doc.value.system,'new edit');
 vm.state.value={...vm.state.value,latestSource:reply('one')};await vm.saveOriginal();assert.equal(calls,1);assert.match(vm.error.value,/新内容/);scope.stop();
});
test('late original-save completion updates only its own unchanged cached editor',async()=>{
 let applied=null;Api.contextEditor=async key=>key==='one' && applied ? applied : reply(key);Api.previewContextEditor=async()=>({ok:true,payload:{input:[]},issues:[]});const wait=deferred();Api.saveContextEditor=()=>wait.promise;
 const {scope,vm,props,exposed,events}=setup();exposed.open();await settle();vm.setField('system','saved edit');await vm.preview();const saved=vm.saveOriginal();
 props.conversationUuid='two';await settle();exposed.open();await settle();assert.equal(vm.doc.value.system,'system-two');applied={...reply('one'),baseline:{...sample('one'),system:'saved edit'},snapshotToken:'applied',draft:{revision:1},conversation:{conversationUuid:'one'}};wait.resolve(applied);await saved;
 assert.equal(vm.doc.value.system,'system-two');assert.equal(events.filter(e=>e[0]==='saved').length,0);
 props.conversationUuid='one';await settle();exposed.open();await settle();assert.equal(vm.doc.value.system,'saved edit');assert.equal(vm.state.value.snapshotToken,'applied');scope.stop();
});
test('automatic reasoning repair is informational, leaves the editor lossless and does not block either save target',async()=>{
 const data=reply('one');data.baseline.entries.push({entryId:'signed',message:{role:'assistant',content:'answer',reasoning:'historic thinking',signature:'historic signature'}});
 Api.contextEditor=async()=>data;const repairText='框架已自动处理 25 组受编辑影响的历史思考、签名和密文';
 Api.previewContextEditor=async()=>({ok:true,payload:{input:[{role:'assistant',content:'answer'}]},issues:[{code:'reasoning_cleanup',severity:'warning',entryId:'',text:repairText}]});
 const {scope,vm,exposed}=setup();exposed.open();await settle();vm.setField('system','edited prefix');await vm.preview();
 assert.equal(vm.automaticRepair.value.text,repairText);assert.equal(vm.serverIssues.value.length,1);assert.equal(vm.serverErrors.value.length,0);assert.equal(vm.saveReady.value,true);
 assert.equal(vm.doc.value.entries[1].message.signature,'historic signature');assert.equal(vm.doc.value.entries[1].message.reasoning,'historic thinking');
 assert.equal(vm.issueTarget(vm.automaticRepair.value),'');vm.askSaveOriginal();assert.equal(vm.dialog.value.kind,'apply');
 assert.match(text,/v-if="automaticRepair" class="ce-help" role="status"/);scope.stop();
});

test('tool table filters retain original indexes; dialogs are transactional and preserve unknown fields',async()=>{
 const data=reply('one');data.baseline.tools=Array.from({length:25},(_,i)=>({name:`Tool${i}`,description:`Description ${i}`,parameters:{type:'object',properties:{value:{type:'string'}},additionalProperties:false},unknown:{keep:i}}));
 Api.contextEditor=async()=>data;
 const {scope,vm,exposed}=setup();exposed.open();await settle();
 vm.toolSearch.value='Tool24';assert.equal(vm.toolRows.value.length,1);assert.equal(vm.toolRows.value[0].index,24);
 vm.openEntity('tool',24);vm.entityEditor.value.data.name='Unsaved';assert.equal(vm.doc.value.tools[24].name,'Tool24');vm.entityEditor.value=null;
 vm.openEntity('tool',24);vm.entityEditor.value.data.description='edited';vm.entityEditor.value.text='invalid';vm.saveEntity();assert.ok(vm.entityError.value);assert.equal(vm.doc.value.tools[24].description,'Description 24');
 vm.entityEditor.value.text='{"type":"object","properties":{"v":{"type":"number"}}}';vm.entityView('raw');assert.equal(JSON.parse(vm.entityEditor.value.raw).unknown.keep,24);
 vm.entityView('fields');vm.saveEntity();assert.equal(vm.entityEditor.value,null);assert.equal(vm.doc.value.tools[24].description,'edited');assert.equal(vm.doc.value.tools[24].unknown.keep,24);assert.equal(vm.doc.value.tools[24].parameters.properties.v.type,'number');
 assert.equal(vm.state.value.undo.length,1);vm.history('undo');assert.equal(vm.doc.value.tools[24].description,'Description 24');vm.history('redo');assert.equal(vm.doc.value.tools[24].description,'edited');
 vm.addTool();vm.entityEditor.value=null;assert.equal(vm.doc.value.tools.length,25);
 vm.addTool();vm.entityEditor.value.data.name='New';vm.saveEntity();assert.equal(vm.doc.value.tools.length,26);assert.equal(vm.doc.value.tools[25].name,'New');scope.stop();
});

test('tool-call and opaque-field dialogs keep message associations and native representation coherent',async()=>{
 const data=reply('one');data.baseline.entries.push({entryId:'assistant',message:{role:'assistant',content:null,reasoning:'thinking',signature:'opaque',tool_calls:[{id:'c1',name:'Read',arguments:'{}',unknown:7}],native_output_items:[{type:'function_call',call_id:'c1',name:'Read',arguments:'{}',status:'completed'}]}},{entryId:'result',message:{role:'tool',name:'Read',tool_call_id:'c1',content:'{"ok":true}'}});
 Api.contextEditor=async()=>data;const {scope,vm,exposed}=setup();exposed.open();await settle();vm.choose('assistant');
 assert.equal(vm.snippet(vm.entry.value.message),'Read');assert.deepEqual(vm.currentPairs.value[0].results,['result']);
 vm.openEntity('call',0);vm.entityEditor.value.text='{"path":"/example"}';vm.saveEntity();assert.equal(vm.entityError.value,'');assert.equal(vm.entry.value.message.content,null);assert.equal(vm.entry.value.message.tool_calls[0].unknown,7);assert.equal(vm.entry.value.message.native_output_items[0].arguments,'{"path":"/example"}');
 vm.openEntity('field',-1,'reasoning');vm.entityEditor.value.text='modified thinking';vm.saveEntity();assert.equal(vm.entry.value.message.reasoning,'modified thinking');assert.equal(vm.entry.value.message.signature,'opaque');
 vm.openEntity('field',-1,'signature');vm.entityEditor.value.text='';vm.saveEntity();assert.equal(vm.entry.value.message.signature,'');
 vm.choose('result');assert.equal(vm.currentPairs.value[0].entryId,'assistant');assert.equal(vm.contentLanguage(vm.entry.value.message.content),'json');scope.stop();
});

test('text commits target the originating message; duplicate and stale cross-conversation commits are ignored',async()=>{
 Api.contextEditor=async key=>reply(key);const {scope,vm,props,exposed}=setup();exposed.open();await settle();
 vm.choose('one');vm.choose('system');vm.commitText({scope:'one',id:'one',field:'content',value:'last edit'});
 assert.equal(vm.doc.value.entries[0].message.content,'last edit');assert.equal(vm.doc.value.system,'system-one');assert.equal(vm.state.value.undo.length,1);
 vm.commitText({scope:'one',id:'one',field:'content',value:'last edit'});assert.equal(vm.state.value.undo.length,1);
 props.conversationUuid='two';await settle();exposed.open();await settle();vm.commitText({scope:'one',id:'system',field:'system',value:'stale'});assert.equal(vm.doc.value.system,'system-two');scope.stop();
});

test('invalid raw JSON stays visible; valid raw applies before navigation and server save',async()=>{
 Api.contextEditor=async key=>reply(key);const saved=[];Api.saveContextEditorDraft=async(key,data)=>{saved.push(data);return {ok:true,draft:{revision:1}};};
 const {scope,vm,exposed}=setup();exposed.open();await settle();vm.switchTab('raw');vm.raw.value='{invalid';vm.choose('one');assert.equal(vm.selected.value,'system');assert.ok(vm.rawError.value);vm.switchTab('preview');assert.equal(vm.tab.value,'raw');await vm.saveDraft();assert.equal(saved.length,0);
 vm.raw.value='{"system":"latest"}';await vm.saveDraft();assert.equal(saved[0].working.system,'latest');assert.equal(vm.doc.value.system,'latest');vm.choose('one');assert.equal(vm.selected.value,'one');
 vm.raw.value='{"role":"user","content":"edited JSON","unknown":true}';vm.switchTab('structured');assert.equal(vm.entry.value.message.content,'edited JSON');assert.equal(vm.entry.value.message.unknown,true);scope.stop();
});

test('editor open/close events suspend chat surfaces without destroying composer state',async()=>{
 Api.contextEditor=async key=>reply(key);const {scope,vm,exposed,events}=setup();exposed.open();await settle();assert.deepEqual(events[0],['open-change',true]);exposed.close();assert.deepEqual(events.at(-1),['open-change',false]);
 const consoleSource=await readFile(new URL('../ConsoleView.vue',import.meta.url),'utf8');
 assert.match(consoleSource,/<ConsoleComposer\s+v-show="!contextEditorOpen"\s+:inert="contextEditorOpen"/);
 assert.match(consoleSource,/@open-change="contextEditorOpen = \$event"/);
 assert.match(consoleSource,/<aside class="console-controls" v-show="!contextEditorOpen" :inert="contextEditorOpen"/);
 assert.match(text,/class="ce-outline-link"[^\n]*<svg/);assert.match(text,/<table class="ce-table ce-tools-table">/);
 assert.match(text,/<el-dialog[^\n]*append-to-body destroy-on-close/);
 assert.match(text,/v-if="entry.message.content != null"/);
 assert.match(styles,/\.ce-editor-fill \{ flex: 1; min-height: 0/);scope.stop();
});

test('ordered block list edits actual native order, saves it, invalidates preview and supports undo',async()=>{
 const data=reply('one');data.baseline.entries.push({entryId:'a',message:{role:'assistant',content:'beforeafter',tool_calls:[{id:'c',name:'Read',arguments:'{}'}],native_output_items:[{type:'reasoning',encrypted_content:'opaque'},{type:'message',content:[{type:'output_text',text:'before'}]},{type:'function_call',call_id:'c',name:'Read',arguments:'{}'},{type:'message',content:[{type:'output_text',text:'after'}]}]}},{entryId:'t',message:{role:'tool',tool_call_id:'c',content:'result'}});
 Api.contextEditor=async()=>data;let saved;Api.saveContextEditorDraft=async(key,payload)=>{saved=payload;return {ok:true,draft:{revision:1}};};
 Api.previewContextEditor=async()=>({ok:true,payload:{input:[]},issues:[]});
 const {scope,vm,exposed}=setup();exposed.open();await settle();vm.choose('a');
 assert.equal(vm.sequence.value.ordered,true);assert.deepEqual(vm.sequence.value.rows.map(b=>b.type),['reasoning','message','function_call','message']);
 await vm.preview();vm.switchTab('structured');assert.equal(vm.previewReady.value,true);
 vm.shiftBlock(vm.sequence.value.rows[3],-1);assert.equal(vm.previewReady.value,false);
 assert.deepEqual(vm.entry.value.message.native_output_items.map(b=>b.type),['reasoning','message','message','function_call']);
 await vm.saveDraft();assert.deepEqual(saved.working.entries[1].message.native_output_items,vm.entry.value.message.native_output_items);
 vm.history('undo');assert.equal(vm.entry.value.message.native_output_items[2].type,'function_call');
 vm.openBlock(vm.sequence.value.rows[3]);assert.equal(vm.entityEditor.value.json,false);assert.equal(vm.entityEditor.value.text,'after');vm.entityEditor.value.text='edited';
 assert.equal(vm.entry.value.message.content,'beforeafter');vm.saveEntity();assert.equal(vm.entry.value.message.content,'beforeedited');
 assert.equal(vm.entry.value.message.native_output_items[1].content[0].text,'before');
 vm.openBlock(vm.sequence.value.rows[2]);assert.equal(vm.entityEditor.value.kind,'call');vm.entityEditor.value.text='{"path":"x"}';vm.saveEntity();
 assert.equal(vm.entry.value.message.native_output_items[3].content[0].text,'edited');
 assert.deepEqual(vm.currentPairs.value[0].results,['t']);
 vm.addCall();vm.entityEditor.value.data.name='New';vm.entityEditor.value.data.vendor='keep';vm.saveEntity();
 assert.equal(vm.sequence.value.rows.at(-1).label,'工具调用 · New');assert.equal(vm.entry.value.message.tool_calls.at(-1).vendor,'keep');
 vm.history('undo');assert.equal(vm.sequence.value.rows.length,4);
 vm.choose('t');assert.equal(vm.entry.value.message.role,'tool');
 assert.match(text,/<ol class="ce-sequence" aria-label="消息内容块">/);assert.doesNotMatch(text,/ce-message-panes|activeMessagePane/);scope.stop();
});

test('field-only assistant is explicitly unordered while same-array call moves remain available',async()=>{
 const data=reply('one');data.baseline.entries.push({entryId:'a',message:{role:'assistant',content:'body',reasoning:'thought',tool_calls:[{id:'c1',name:'Read',arguments:'{}'},{id:'c2',name:'Read',arguments:'{}'}]}});
 Api.contextEditor=async()=>data;const {scope,vm,exposed}=setup();exposed.open();await settle();vm.choose('a');
 assert.equal(vm.sequence.value.ordered,false);assert.match(vm.orderNote.value,/未记录完整交错顺序/);
 assert.equal(vm.canMoveBlock(vm.entry.value.message,'call-0',-1),false);
 vm.shiftBlock(vm.sequence.value.rows[2],1);assert.deepEqual(vm.entry.value.message.tool_calls.map(c=>c.id),['c2','c1']);scope.stop();
});

test('difference picker mounts one pair and falls back to the next change after restoring',async()=>{
 Api.contextEditor=async()=>reply('one');const {scope,vm,exposed}=setup();exposed.open();await settle();vm.setField('system','new system');vm.choose('one');vm.setField('content','new message');
 assert.equal(vm.activeDiff.value.id,'system');vm.diffId.value='one';assert.equal(vm.activeDiff.value.id,'one');vm.restore(vm.activeDiff.value);assert.equal(vm.activeDiff.value.id,'system');vm.restore(vm.activeDiff.value);assert.equal(vm.activeDiff.value,undefined);
 assert.match(text,/aria-label="选择差异项"/);assert.doesNotMatch(text,/<details v-for="item in changes"/);scope.stop();
});

test('reopening revalidates a cached snapshot and adopts new turns when the editor is untouched',async()=>{
 let current=reply('one'),reads=0;Api.contextEditor=async()=>{reads++;return structuredClone(current);};
 const {scope,vm,exposed}=setup();exposed.open();await settle();assert.equal(vm.doc.value.entries.length,1);exposed.close();
 current.snapshotToken='token-three';current.baseline.entries.push({entryId:'two',message:{role:'user',content:'second turn'}},{entryId:'three',message:{role:'user',content:'third turn'}});
 exposed.open();await settle();assert.equal(reads,2);assert.equal(vm.doc.value.entries.length,3);assert.equal(vm.state.value.snapshotToken,'token-three');scope.stop();
});

test('source advance preserves local edits until explicitly accepting the latest snapshot',async()=>{
 let current=reply('one');Api.contextEditor=async()=>structuredClone(current);
 const {scope,vm,exposed}=setup();exposed.open();await settle();vm.setField('system','keep my edit');exposed.close();
 current.snapshotToken='token-new';current.baseline.entries.push({entryId:'two',message:{role:'user',content:'new turn'}});
 exposed.open();await settle();assert.equal(vm.doc.value.system,'keep my edit');assert.equal(vm.doc.value.entries.length,1);assert.equal(vm.state.value.latestSource.baseline.entries.length,2);
 vm.askLatestSource();assert.equal(vm.dialog.value.kind,'refresh');vm.dialog.value=null;assert.equal(vm.doc.value.system,'keep my edit');
 vm.askLatestSource();vm.confirmDelete();assert.equal(vm.doc.value.system,'system-one');assert.equal(vm.doc.value.entries.length,2);assert.equal(vm.state.value.snapshotToken,'token-new');assert.equal(vm.state.value.latestSource,null);
 exposed.close();exposed.open();await settle();assert.equal(vm.doc.value.entries.length,2);scope.stop();
});

test('untouched saved old snapshot follows new turns; edited saved draft is retained with a stale-source notice',async()=>{
 const draft={baseline:sample('one'),working:sample('one'),snapshotToken:'old',revision:4,mode:'compatible',targetModel:'model'};
 const current=reply('one',draft);current.snapshotToken='new';current.baseline.entries.push({entryId:'two',message:{role:'user',content:'second turn'}});
 Api.contextEditor=async()=>structuredClone(current);
 const first=setup();first.exposed.open();await settle();assert.equal(first.vm.doc.value.entries.length,2);assert.equal(first.vm.state.value.revision,4);assert.equal(first.vm.state.value.latestSource,null);first.scope.stop();
 draft.working.system='saved edit';const second=setup();second.exposed.open();await settle();assert.equal(second.vm.doc.value.system,'saved edit');assert.equal(second.vm.doc.value.entries.length,1);assert.equal(second.vm.state.value.latestSource.baseline.entries.length,2);
 second.vm.askLatestSource();second.vm.confirmDelete();assert.equal(second.vm.doc.value.entries.length,2);assert.equal(second.vm.state.value.revision,4);
 second.exposed.close();second.exposed.open();await settle();assert.equal(second.vm.doc.value.entries.length,2,'old server draft must not reappear after an explicit refresh');assert.equal(draft.working.system,'saved edit','refresh must not mutate the server draft');second.scope.stop();
});

test('refresh failures retain the cached draft and edits completed during a read are not lost',async()=>{
 Api.contextEditor=async()=>reply('one');const {scope,vm,exposed}=setup();exposed.open();await settle();exposed.close();
 const next=deferred();Api.contextEditor=()=>next.promise;exposed.open();vm.setField('system','edit while loading');const result=reply('one');result.snapshotToken='new';next.resolve(result);await settle();assert.equal(vm.doc.value.system,'edit while loading');assert.ok(vm.state.value.latestSource);
 exposed.close();Api.contextEditor=async()=>{throw Error('network unavailable');};exposed.open();await settle();assert.equal(vm.doc.value.system,'edit while loading');assert.match(vm.error.value,/未能核对最新上下文/);scope.stop();
});
