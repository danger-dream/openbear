import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {ref,watch} from 'vue';
import {referenceToken,referencesInText} from './codec.js';
const source=fs.readFileSync(new URL('../views/consoleView/ConsoleView.vue',import.meta.url),'utf8');
const start=source.indexOf('async function deleteTurnSuffix(turn) {');
const end=source.indexOf('async function send()',start);
assert.ok(start >= 0 && end > start, 'extract the real suffix deletion handler');
const code=source.slice(start,end);
const original='先读 '+referenceToken({kind:'doc',id:'17',label:'部署文档'})+' 再看 '+referenceToken({kind:'chat',id:'source',label:'原会话',scope:'recent',turns:7});
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
const flush=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
function harness({draft='',confirm=async()=>{},remove=async()=>({deletedRootTurns:['turn']}),load=async()=>{}}={}){
 const calls={deleted:[],saved:[],closed:0,loaded:0,errors:[]};
 const turn={user:{turnUuid:'turn',content:original,attachments:[]}};
 const ctx=vm.createContext({componentMounted:true,activeConversationUuid:ref('current'),isLocalConversation:ref(false),deletingTurnUuid:ref(''),running:ref(false),turns:ref([turn]),draft:ref(draft),draftEditRevision:0,lastFrameSeq:ref(1),plainText:x=>x,
  ElMessageBox:{confirm},ElMessage:{warning(){},success(){},error:x=>calls.errors.push(x)},apiError:String,
  Api:{deleteConversationTurnSuffix:async (...args)=>{calls.deleted.push(args);return remove();}},
  closeWs:()=>calls.closed++,toolDetailCache:{reset(){}},load:async()=>{calls.loaded++;await load();},emit(){},
  setDraftForConversation:(...args)=>calls.saved.push(args),adjustComposerHeight(){},focusComposer:async()=>{},turn});
 const stop=watch(ctx.draft,()=>{ctx.draftEditRevision++;},{flush:'sync'});
 vm.runInContext(code,ctx);
 return {ctx,calls,run:()=>vm.runInContext('deleteTurnSuffix(turn)',ctx),stop};
}
test('restart restores canonical reference nodes with exact IDs, order and scope',async()=>{
 const h=harness();try{await h.run();assert.equal(h.ctx.draft.value,original);const refs=referencesInText(h.ctx.draft.value);assert.deepEqual(refs.map(r=>r.id),['17','source']);assert.equal(refs[1].turns,7);assert.equal(h.calls.saved.length,1);}finally{h.stop();}
});
test('reference-only existing draft is not empty and is never overwritten',async()=>{
 const draft=referenceToken({kind:'mem',id:'4',label:'保留这条引用'});const h=harness({draft});try{await h.run();assert.equal(h.ctx.draft.value,draft);assert.equal(h.calls.saved.length,0);}finally{h.stop();}
});
test('cancelled restart preserves original editor state and never calls DELETE',async()=>{
 const h=harness({draft:original,confirm:async()=>{throw 'cancel';}});try{await h.run();assert.equal(h.ctx.draft.value,original);assert.equal(h.calls.deleted.length,0);}finally{h.stop();}
});
test('failed restart does not create a recovered draft or discard current references',async()=>{
 const h=harness({draft:original,remove:async()=>{throw Error('failure');}});try{await h.run();assert.equal(h.ctx.draft.value,original);assert.equal(h.calls.saved.length,0);assert.equal(h.calls.errors.length,1);}finally{h.stop();}
});
test('edits made while DELETE is pending survive even if user clears them again',async()=>{
 const pending=deferred(),h=harness({remove:()=>pending.promise});try{const job=h.run();await flush();h.ctx.draft.value='new edit';h.ctx.draft.value='';pending.resolve({});await job;assert.equal(h.ctx.draft.value,'');assert.equal(h.calls.saved.length,0);}finally{h.stop();}
});
test('switching conversations while DELETE is pending never closes or fills the new editor',async()=>{
 const pending=deferred(),h=harness({remove:()=>pending.promise});try{const job=h.run();await flush();h.ctx.activeConversationUuid.value='other';h.ctx.draft.value='other draft';pending.resolve({});await job;assert.equal(h.ctx.draft.value,'other draft');assert.equal(h.calls.closed,0);assert.equal(h.calls.loaded,0);}finally{h.stop();}
});
test('switching during history refresh never refills the newly selected conversation',async()=>{
 const pending=deferred(),h=harness({load:()=>pending.promise});try{const job=h.run();await flush();h.ctx.activeConversationUuid.value='other';pending.resolve();await job;assert.equal(h.calls.saved.length,0);}finally{h.stop();}
});
