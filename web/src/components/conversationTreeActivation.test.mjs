import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {ref} from 'vue';
const source=fs.readFileSync(new URL('./ConversationTree.vue',import.meta.url),'utf8');
function between(start,end){return source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));}
function harness(load=async()=>{}){
 const expanded=new Set(),events=[],requests=[],branches=new Map();
 const context=vm.createContext({selectedFolderId:ref('previous'),archiveUnlocked:ref(false),closeOverview(){},
  isExpanded:id=>expanded.has(id),setExpanded:(id,value)=>value?expanded.add(id):expanded.delete(id),
  stateFor:(id='',system='')=>{const key=id+system;if(!branches.has(key))branches.set(key,{parentId:id,systemNode:system,loading:false});return branches.get(key);},
  invalidateBranch:(id,system)=>{branches.get(id+system).loading=false;},
  loadChildren:async(id='',system='')=>{requests.push([id,system]);return load(id,system);},emit:(...args)=>events.push(args),
 });
 vm.runInContext(between('async function toggleRow(', 'async function revealDraft(')+between('async function activateRow(', 'async function locateAndOpen('),context);
 return {context,expanded,events,requests,branches,activate:row=>context.activateRow(row),toggle:row=>context.toggleRow(row)};
}
for(const id of ['root','root/child/grandchild'])test('folder label toggles and selects exact new-conversation target: '+id,async()=>{
 const h=harness(),row={kind:'folder',folderId:id};await h.activate(row);assert.ok(h.expanded.has(id));assert.equal(h.context.selectedFolderId.value,id);assert.equal(h.requests.length,1);assert.ok(h.events.every(e=>e[0]==='selected-folder'));
 await h.activate(row);assert.ok(!h.expanded.has(id));assert.equal(h.context.selectedFolderId.value,id);assert.equal(h.requests.length,1);
});
test('chevron still toggles once without changing selected folder',async()=>{
 const h=harness(),row={kind:'folder',folderId:'target'};await h.toggle(row);assert.ok(h.expanded.has('target'));assert.equal(h.context.selectedFolderId.value,'previous');assert.equal(h.events.length,0);
 assert.match(source,/@click\.stop="toggleRow\(row\)"/);
});
test('system node behavior remains unchanged and archived is only loaded on explicit open',async()=>{
 const h=harness();assert.equal(h.context.archiveUnlocked.value,false);await h.activate({kind:'system',id:'__temporary',systemNode:'temporary'});assert.ok(h.expanded.has('__temporary'));assert.equal(h.context.selectedFolderId.value,'');
 await h.activate({kind:'system',id:'__archive',systemNode:'archive'});assert.ok(h.expanded.has('__archive'));assert.equal(h.context.archiveUnlocked.value,true);assert.equal(h.context.selectedFolderId.value,'');assert.deepEqual(h.requests,[['','temporary'],['','archive']]);
 await h.activate({kind:'system',id:'__archive',systemNode:'archive'});assert.ok(!h.expanded.has('__archive'));assert.equal(h.requests.length,2);
});
test('collapsing a folder during load stays collapsed when the old request finishes',async()=>{
 let resolve;const pending=new Promise(done=>resolve=done);const h=harness(()=>pending),row={kind:'folder',folderId:'a'};
 const opening=h.activate(row);h.context.stateFor('a').loading=true;await h.activate(row);assert.ok(!h.expanded.has('a'));assert.equal(h.context.stateFor('a').loading,false);resolve();await opening;assert.ok(!h.expanded.has('a'));
});
test('conversation leaf still opens without toggling a branch',async()=>{
 const h=harness(),row={kind:'conversation',conversationUuid:'chat-a',folderId:'a'};await h.activate(row);assert.equal(h.expanded.size,0);assert.equal(h.requests.length,0);assert.deepEqual(h.events.map(e=>e[0]),['selected-folder','open']);
});
