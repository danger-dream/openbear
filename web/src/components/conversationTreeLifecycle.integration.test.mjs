import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {ref,nextTick} from 'vue';
import {isLocalConversationRow as isLocalConversation,normalizeConversationRows} from '../conversationOrdering.js';
const app=fs.readFileSync(new URL('../App.vue',import.meta.url),'utf8');
const tree=fs.readFileSync(new URL('./ConversationTree.vue',import.meta.url),'utf8');
const consoleSource=fs.readFileSync(new URL('../views/consoleView/ConsoleView.vue',import.meta.url),'utf8');
function between(source,start,end){const a=source.indexOf(start),b=source.indexOf(end,a+start.length);assert.ok(a>=0&&b>a,`${start}..${end}`);return source.slice(a,b);}
function defer(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};}
const conv=(id,folderId='a',extra={})=>({kind:'conversation',conversationUuid:id,folderId,title:id,createdAt:1,...extra});
const local=()=>conv('local:new','a',{local:true});
function harness({rows=[local(),conv('saved')],active='local:new',confirm=async()=>{},remove=async()=>{},children=async()=>({items:[]}),mounted=true}={}){
 const calls={deletes:[],clears:[],forgotten:[],reveals:[],refresh:[],success:[],error:[],warning:[],confirms:[],lookups:[]};
 const storage=new Map([['openbear.console.drafts.v1',JSON.stringify({'local:new':'discard this',saved:'retain this',other:'also retain'})]]);
 const ctx=vm.createContext({ref,nextTick,isLocalConversation,normalizeConversationRows,
  conversations:ref(rows),activeConversationUuid:ref(active),selectedFolderId:ref('a'),draftFolderId:ref('a'),active:ref('console'),LOCAL_CONVERSATION_UUID:'local:new',deletingConversations:new Set(),
  window:{localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)}},
  conversationTreeRef:ref({forgetConversation:id=>calls.forgotten.push(id),revealConversation:async id=>calls.reveals.push(id),revealDraft:async id=>calls.reveals.push('draft:'+id)}),
  consoleViewRef:ref(mounted?{discardConversationDraft:id=>calls.clears.push(id)}:null),
  attachmentDrafts:{remove:async()=>{}},
  ElMessageBox:{confirm:(...args)=>{calls.confirms.push(args);return confirm();}},
  ElMessage:Object.fromEntries(['success','error','warning'].map(k=>[k,v=>calls[k].push(v)])),apiError:String,
  Api:{deleteConversation:async id=>{calls.deletes.push(id);return remove(id);},conversationTreeChildren:async options=>{calls.lookups.push(options);return children(options);}},
  conversationTitle:r=>r.title||'新会话',isRunning:r=>Boolean(r?.running||r?.status==='running'),closeSidebar(){},syncRoute(){},refreshConsoleAfterPropSync:async()=>{},
  loadConversations:async options=>calls.refresh.push(options),
 });
 vm.runInContext(`
 function setConversationsIfChanged(rows){conversations.value=normalizeConversationRows(rows);}
 function setDraftFolderId(id){draftFolderId.value=id;}
 ${between(app,'function localConversation(', 'function currentRouteConversationUuid(')}
 ${between(app,'function focusLocalConversation(', 'async function startConsoleNewSession(')}
 ${between(app,'function handleTreeFolderRemoved(', 'async function handleTreeOpen(')}
 ${between(app,'function currentDraftConversation(', 'async function openConversation(')}
 ${between(app,'function discardConversationDraft(', 'function closeConversationMenu(')}
 `,ctx);
 return{ctx,calls,storage,run:s=>vm.runInContext(s,ctx),remove:row=>{ctx.target=row;return vm.runInContext('deleteConversation(target)',ctx);}};
}

test('tree delete delegates to the App lifecycle owner for both draft and persisted rows',()=>{
 const emitted=[];const ctx=vm.createContext({running:()=>false,emit:(...a)=>emitted.push(a),ElMessage:{warning(){}},row:local()});
 vm.runInContext(between(tree,'function removeConversation(','async function showProperties(')+';removeConversation(row);',ctx);
 assert.equal(emitted[0][0],'delete-conversation');assert.equal(emitted[0][1].conversationUuid,'local:new');
 assert.match(app,/@delete-conversation="deleteConversation"/);
 assert.match(app,/@folder-removed="handleTreeFolderRemoved"/);
 assert.match(app,/ref="consoleViewRef"/);
});
test('confirmed active local deletion never calls DELETE; clears draft and selects saved sibling',async()=>{
 const h=harness();await h.remove(local());assert.deepEqual(h.calls.deletes,[]);assert.deepEqual(h.calls.clears,['local:new']);assert.equal(h.ctx.activeConversationUuid.value,'saved');assert.equal(h.ctx.draftFolderId.value,'');assert.equal(h.ctx.conversations.value.some(isLocalConversation),false);assert.equal(h.calls.confirms.length,1);
});
test('cancelled deletion preserves rows, active selection and all draft data',async()=>{
 const h=harness({confirm:async()=>{throw 'cancel';}});await h.remove(local());assert.equal(h.ctx.conversations.value.length,2);assert.equal(h.ctx.activeConversationUuid.value,'local:new');assert.deepEqual(h.calls.deletes,[]);assert.deepEqual(h.calls.clears,[]);assert.deepEqual(h.calls.forgotten,[]);
});
test('persisted deletion failure does not change selection, caches or draft data',async()=>{
 const row=conv('saved');const h=harness({rows:[row,local()],active:'saved',remove:async()=>{throw Error('network failure');}});await h.remove(row);assert.equal(h.ctx.activeConversationUuid.value,'saved');assert.equal(h.ctx.conversations.value.length,2);assert.deepEqual(h.calls.clears,[]);assert.deepEqual(h.calls.forgotten,[]);assert.equal(h.calls.error.length,1);
});
test('persisted current deletion opens next same-level conversation, never reloads deleted UUID',async()=>{
 const row=conv('saved');const h=harness({rows:[conv('elsewhere','b'),row,conv('next'),conv('archive','a',{archived:true})],active:'saved'});await h.remove(row);assert.deepEqual(h.calls.deletes,['saved']);assert.equal(h.ctx.activeConversationUuid.value,'next');assert.deepEqual(h.calls.reveals,['next']);assert.deepEqual(h.calls.refresh,[]);
});
test('deleting inactive draft retains current conversation and uses targeted cleanup',async()=>{
 const h=harness({active:'saved'});await h.remove(local());assert.equal(h.ctx.activeConversationUuid.value,'saved');assert.deepEqual(h.calls.lookups,[]);assert.deepEqual(h.calls.clears,['local:new']);assert.deepEqual(h.calls.reveals,[]);
});
test('deleting inactive persisted conversation does not steal focus or follow removed item',async()=>{
 const h=harness({rows:[conv('saved'),conv('other')],active:'other'});await h.remove(conv('saved'));assert.equal(h.ctx.activeConversationUuid.value,'other');assert.equal(h.calls.refresh[0].trackActive,false);assert.deepEqual(h.calls.reveals,[]);
});
test('last local draft removal opens a fresh temporary draft without a DELETE request',async()=>{
 const h=harness({rows:[local()]});await h.remove(local());await nextTick();assert.deepEqual(h.calls.deletes,[]);assert.equal(h.ctx.activeConversationUuid.value,'local:new');assert.equal(h.ctx.conversations.value[0].folderId,'');assert.deepEqual(h.calls.clears,['local:new']);assert.ok(h.calls.reveals.includes('draft:'));
});
test('same-level successor lookup crosses folder-only pagination without fetching archive',async()=>{
 const h=harness({rows:[local(),conv('different','b')],children:async ({cursor})=>cursor?{items:[conv('hidden')]}:{items:[{kind:'folder',folderId:'nested'}],hasMore:true,nextCursor:'50'}});await h.remove(local());assert.equal(h.ctx.activeConversationUuid.value,'hidden');assert.equal(h.calls.lookups.length,2);assert.equal(h.calls.lookups[1].cursor,'50');assert.ok(h.calls.lookups.every(x=>x.parentId==='a'&&!x.systemNode));
});
test('pending confirmation is deduplicated and runtime rechecked before deletion',async()=>{
 const pending=defer();const row=conv('saved');const h=harness({rows:[row],active:'saved',confirm:()=>pending.promise});const first=h.remove(row);await h.remove(row);h.ctx.conversations.value[0].running=true;pending.resolve();await first;assert.equal(h.calls.confirms.length,1);assert.deepEqual(h.calls.deletes,[]);assert.equal(h.calls.warning.length,1);
});
test('late deletion response cannot steal focus after user navigates',async()=>{
 const pending=defer();const row=conv('saved');const h=harness({rows:[row,conv('other')],active:'saved',remove:()=>pending.promise});const done=h.remove(row);await nextTick();h.ctx.activeConversationUuid.value='other';pending.resolve();await done;assert.equal(h.ctx.activeConversationUuid.value,'other');assert.deepEqual(h.calls.reveals,[]);
});
test('unmounted console cleanup removes only the matching persistent draft',async()=>{
 const h=harness({mounted:false,active:'saved'});await h.remove(local());assert.deepEqual(JSON.parse(h.storage.get('openbear.console.drafts.v1')),{saved:'retain this',other:'also retain'});
});
test('denied browser storage does not prevent in-memory draft removal',async()=>{
 const h=harness({mounted:false,active:'saved'});h.ctx.window.localStorage.getItem=()=>{throw Error('denied');};await h.remove(local());assert.equal(h.ctx.conversations.value.some(isLocalConversation),false);assert.equal(h.calls.error.length,0);
});
test('confirmed directory deletion reparents direct local draft and keeps its identity/content',async()=>{
 const h=harness();const draft=h.ctx.conversations.value.find(isLocalConversation);h.run("handleTreeFolderRemoved({folderId:'a',targetFolderId:'b'})");await nextTick();assert.equal(h.ctx.draftFolderId.value,'b');assert.equal(h.ctx.conversations.value.find(isLocalConversation).conversationUuid,draft.conversationUuid);assert.equal(h.ctx.conversations.value.find(isLocalConversation).folderId,'b');assert.deepEqual(h.calls.clears,[]);assert.equal(h.ctx.selectedFolderId.value,'b');assert.ok(h.calls.reveals.includes('draft:b'));
});
test('directory deletion leaves descendant-folder and unrelated drafts in their own folder',()=>{
 const h=harness({rows:[{...local(),folderId:'child'},conv('saved')]});h.ctx.draftFolderId.value='child';h.run("handleTreeFolderRemoved({folderId:'a',targetFolderId:'b'})");assert.equal(h.ctx.draftFolderId.value,'child');assert.equal(h.ctx.conversations.value.find(isLocalConversation).folderId,'child');assert.deepEqual(h.calls.clears,[]);
});
test('mounted editor clearing is targeted and removes active text before navigation saves it',()=>{
 const cleared=[],attachments=[],released=[],forgotten=[];const props={conversationUuid:'local:new'};
 const hydration=new Map([['other',{}],['local:new',{}],['kept',{}]]);
 const ctx=vm.createContext({props,attachmentHydrations:hydration,activeAttachmentKey:()=>props.conversationUuid,attachmentRestoring:ref(true),draft:ref('deleted text'),draftKey:s=>s,clearDraftForConversation:id=>cleared.push(id),clearAttachments:()=>attachments.push(true),releaseStashedAttachments:id=>released.push(id),attachmentDrafts:{remove:async id=>{forgotten.push(id);}},adjustComposerHeight(){},defineExpose(){}});
 vm.runInContext(between(consoleSource,'function discardConversationDraft(','defineExpose('),ctx);vm.runInContext("discardConversationDraft('other')",ctx);assert.equal(ctx.draft.value,'deleted text');assert.equal(attachments.length,0);vm.runInContext("discardConversationDraft('local:new')",ctx);assert.equal(ctx.draft.value,'');assert.equal(attachments.length,1);assert.deepEqual(cleared,['other','local:new']);
 // A deleted conversation loses its parked files and its stored copy whether or not it is the one on screen.
 assert.deepEqual(released,['other','local:new']);assert.deepEqual(forgotten,['other','local:new']);
 assert.deepEqual([...hydration.keys()],['kept']);assert.equal(ctx.attachmentRestoring.value,false);
});
test('mutation refresh reruns current search, and cancelled folder removal emits no migration',async()=>{
 const calls=[];const ctx=vm.createContext({moveRow:ref({kind:'folder',folderId:'a',name:'A'}),moveMode:ref('delete'),moveFolderId:ref('b'),moveBusy:ref(false),moveDialog:ref(true),Api:{deleteConversationFolder:async (_,payload)=>{calls.push(payload);return{};}},ElMessageBox:{confirm:async()=>{throw 'cancel';}},ElMessage:{error(){throw Error('unexpected');}},apiError:String});
 vm.runInContext(between(tree,'async function submitMove(','async function moveTreeItem('),ctx);await vm.runInContext('submitMove()',ctx);assert.equal(calls.length,1);assert.equal(calls[0].impactOnly,true);assert.equal(ctx.moveDialog.value,true);
 assert.match(between(tree,'async function invalidateAndRefresh(','async function refreshAffected('),/if \(query\.value\.trim\(\)\) await runSearch\(\)/);
});
test('removal invalidates old branch/search responses and releases search loading state',()=>{
 const branches={a:{items:[conv('removed'),conv('kept')],generation:2,loading:true}};
 const ctx=vm.createContext({branchState:branches,loading:ref(true),activityItems:ref([conv('removed'),conv('kept')]),recentItems:ref([conv('removed'),conv('kept')]),searchRows:ref([conv('removed'),conv('kept')]),searchLoading:ref(true),emitRows(){}});
 vm.runInContext(`let bootstrapGeneration=1,searchGeneration=1;${between(tree,'function forgetConversation(','function forgetFolder(')};forgetConversation('removed');`,ctx);
 assert.equal(branches.a.generation,3);assert.equal(branches.a.loading,false);assert.deepEqual(branches.a.items.map(x=>x.conversationUuid),['kept']);assert.equal(ctx.searchLoading.value,false);assert.equal(ctx.searchRows.value.length,1);assert.equal(vm.runInContext('searchGeneration',ctx),2);assert.deepEqual(ctx.activityItems.value.map(row=>row.conversationUuid),['kept']);assert.deepEqual(ctx.recentItems.value.map(row=>row.conversationUuid),['kept']);
});
test('local draft submitted while removal confirmation is open is not deleted as a server conversation',async()=>{
 const pending=defer();const h=harness({confirm:()=>pending.promise});const job=h.remove(local());h.ctx.conversations.value=[conv('persisted')];h.ctx.activeConversationUuid.value='persisted';pending.resolve();await job;assert.deepEqual(h.calls.deletes,[]);assert.deepEqual(h.calls.clears,[]);assert.equal(h.ctx.activeConversationUuid.value,'persisted');
});
