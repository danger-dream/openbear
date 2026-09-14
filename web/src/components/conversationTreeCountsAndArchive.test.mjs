import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {computed, nextTick, reactive, ref} from 'vue';
import {treeItemId as rowId, treeItemParent, compareTreeItems, resolveTreeDrop} from './conversationTreeInteractions.js';
const source=fs.readFileSync(new URL('./ConversationTree.vue',import.meta.url),'utf8');
const script=source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import[\s\S]*?;\n/gm,'');
function harness(Api={}){
 const context=vm.createContext({computed,nextTick,reactive,ref,rowId,treeItemParent,compareTreeItems,resolveTreeDrop,Api,apiError:String,referenceCatalog:{connected:false,ready:false},
 // Stub only the new UI import seam; all original tree/count/archive assertions remain intact.
 defineLazyView:()=>({}),
 defineProps:()=>({activeConversationUuid:'',draftConversation:null}),defineEmits:()=>()=>{},defineExpose(){},watch(){},onMounted(){},onBeforeUnmount(){},ElMessage:{error(){},warning(){}},document:{querySelector:()=>null},CSS:{escape:s=>s}});
 vm.runInContext(script+'\nglobalThis.tree={rootFolders,stateFor,setExpanded,expanded,applyStatus,statusAdjustedRow,searchRows,query,runSearch,showMove,moveUnarchive,moveUpdateSnapshots,moveTreeItem,loading};',context);return context.tree;
}
const folder=(id,count=0)=>({kind:'folder',folderId:id,id,parentId:'',name:id,conversationCount:count});
const archived={kind:'conversation',conversationUuid:'old',id:'old',folderId:'a',archived:true};

test('count-only WS updates every known folder and search hit without loading or expanding branches',()=>{
 const t=harness();t.rootFolders.value=[folder('a',4),folder('b',9)];t.stateFor('a').items=[folder('nested',4)];t.searchRows.value=[folder('a',4)];t.setExpanded('a',true);
 t.applyStatus({items:[],folderRunningCounts:{a:1},folderConversationCounts:{a:0,b:13,nested:0}});
 assert.deepEqual(t.rootFolders.value.map(row=>row.conversationCount),[0,13]);assert.equal(t.stateFor('a').items[0].conversationCount,0);assert.equal(t.searchRows.value[0].conversationCount,0);
 assert.deepEqual([...t.expanded.value],['a']);assert.equal(t.loading.value,false);assert.equal(t.stateFor('a').loading,false);assert.equal(t.rootFolders.value[0].runningDescendantCount,1);
 t.applyStatus({items:[],folderRunningCounts:{}});assert.deepEqual(t.rootFolders.value.map(row=>row.conversationCount),[0,13]);
 assert.equal(t.statusAdjustedRow(folder('unknown',7)).conversationCount,7);
});

test('late folder search response uses current WS count including zero',async()=>{
 const t=harness({conversationTreeSearch:async()=>({items:[folder('a',25)]})});t.query.value='a';t.applyStatus({folderConversationCounts:{a:0}});await t.runSearch();assert.equal(t.searchRows.value[0].conversationCount,0);
});

test('archive move options reset to checked for every dialog opening',async()=>{
 const t=harness({conversationTreeFolders:async()=>({items:[]})});await t.showMove(archived);assert.equal(t.moveUnarchive.value,true);assert.equal(t.moveUpdateSnapshots.value,true);
 t.moveUnarchive.value=false;t.moveUpdateSnapshots.value=false;await t.showMove(archived);assert.equal(t.moveUnarchive.value,true);assert.equal(t.moveUpdateSnapshots.value,true);
 assert.match(source,/v-if="moveMode === 'move' && moveRow\?\.kind === 'conversation' && moveRow\?\.archived" class="move-archive-options"/);
});

for(const unarchive of [false,true])for(const updateSnapshots of [false,true])test(`archive options are submitted directly without a duplicate choice: ${unarchive}/${updateSnapshots}`,async()=>{
 const requests=[];const t=harness({moveConversationTreeItem:async request=>{requests.push(request);return {};},conversationTreeMoveImpact:()=>{throw Error('must not ask again');}});
 assert.equal(await t.moveTreeItem(archived,'b','','',{unarchive,updateSnapshots}),true);
 assert.equal(requests.length,1);assert.equal(requests[0].unarchive,unarchive);assert.equal(requests[0].updateSnapshots,updateSnapshots);
});

test('same-folder archived restore or prompt refresh is not mistaken for a no-op',async()=>{
 const requests=[];const t=harness({moveConversationTreeItem:async request=>{requests.push(request);return {};}});
 for(const options of [{unarchive:true,updateSnapshots:true},{unarchive:true,updateSnapshots:false},{unarchive:false,updateSnapshots:true}])await t.moveTreeItem(archived,'a','','',options);
 assert.equal(requests.length,3);await t.moveTreeItem(archived,'a','','',{unarchive:false,updateSnapshots:false});assert.equal(requests.length,3);
});

test('ordinary conversation moves retain the existing impact choice and do not send archive options',async()=>{
 let impacts=0,request;const t=harness({conversationTreeMoveImpact:async()=>{impacts++;return {affectedCount:0};},moveConversationTreeItem:async value=>{request=value;return {};}});
 await t.moveTreeItem({...archived,archived:false},'b');assert.equal(impacts,1);assert.equal(request.updateSnapshots,false);assert.equal('unarchive' in request,false);
});
