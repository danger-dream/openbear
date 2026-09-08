import test from 'node:test';
import assert from 'node:assert/strict';
import {reactive} from 'vue';
import {resolveTreeDrop, treeItemParent} from './conversationTreeInteractions.js';
const folder=(id,parentId='',extra={})=>({kind:'folder',id,folderId:id,parentId,createdAt:10,displayOrder:10,...extra});
const conv=(id,folderId='a',extra={})=>({kind:'conversation',id,conversationUuid:id,folderId,createdAt:10,displayOrder:10,...extra});
const a=folder('a'), b=folder('b'), child=folder('child','a'), deep=folder('deep','child');
const first=conv('first','a',{displayOrder:1}), second=conv('second','a',{displayOrder:2}), third=conv('third','a',{displayOrder:3});
const known=[a,b,child,deep,first,second,third];
for(const row of [a,first]) for(const ratio of [.1,.5,.9]) {
 test(`same stable ${row.kind} identity is no-op after reactive cloning at ${ratio}`,()=>{
  assert.equal(resolveTreeDrop(reactive({...row}),{...row},ratio,known),null);
 });
}
test('unchanged adjacent before/after placement and own-parent drop are no-ops',()=>{
 assert.equal(resolveTreeDrop(first,second,.1,known),null);
 assert.equal(resolveTreeDrop(second,first,.9,known),null);
 assert.equal(resolveTreeDrop(second,a,.5,known),null);
});
test('explicit different same-level position preserves anchors',()=>{
 assert.deepEqual(resolveTreeDrop(third,first,.1,known),{zone:'before',targetFolderId:'a',beforeId:'',afterId:'first'});
 assert.deepEqual(resolveTreeDrop(first,third,.9,known),{zone:'after',targetFolderId:'a',beforeId:'third',afterId:''});
});
test('folder-center move has no ordering anchor; row-edge move preserves explicit position',()=>{
 assert.deepEqual(resolveTreeDrop(first,b,.5,known),{zone:'inside',targetFolderId:'b',beforeId:'',afterId:''});
 const destination=conv('destination','b');
 assert.deepEqual(resolveTreeDrop(first,destination,.9,[...known,destination]),{zone:'after',targetFolderId:'b',beforeId:'destination',afterId:''});
 assert.equal(first.createdAt,10);assert.equal(first.pinned,undefined);
});
test('pin group cannot be silently crossed, but a pinned item can enter a new folder',()=>{
 const pinned=conv('pinned','a',{pinned:true});
 assert.equal(resolveTreeDrop(pinned,first,.1,[...known,pinned]),null);
 assert.equal(resolveTreeDrop(first,pinned,.9,[...known,pinned]),null);
 assert.equal(resolveTreeDrop(pinned,b,.5,[...known,pinned]).targetFolderId,'b');
 assert.equal(pinned.pinned,true);
});
test('moving a directory into itself or any descendant is forbidden at centers and row edges',()=>{
 for(const ratio of [.1,.5,.9]) assert.equal(resolveTreeDrop(a,deep,ratio,known),null);
 assert.equal(resolveTreeDrop(a,a,.5,known),null);
});
test('temporary node accepts conversations and root blank area accepts nested directories',()=>{
 assert.equal(resolveTreeDrop(first,{kind:'system',systemNode:'temporary',id:'__temporary'},.5,known).targetFolderId,'');
 assert.equal(resolveTreeDrop(child,{kind:'root',id:'__root'},.5,known).targetFolderId,'');
 assert.equal(resolveTreeDrop(first,{kind:'root',id:'__root'},.5,known),null);
 assert.equal(resolveTreeDrop(a,{kind:'root',id:'__root'},.5,known),null);
});
test('empty folder row is a destination; an archive or incompatible row is never a destination',()=>{
 assert.equal(resolveTreeDrop(first,{kind:'empty',parentId:'b'},.5,known).targetFolderId,'b');
 assert.equal(resolveTreeDrop(first,{kind:'system',systemNode:'archive',id:'__archive'},.5,known),null);
 assert.equal(resolveTreeDrop(first,b,.1,known),null);
 assert.equal(resolveTreeDrop(first,{...second,archived:true},.5,known),null);
});
test('local drafts, archived sources, and search targets do not start a move',()=>{
 assert.equal(resolveTreeDrop({...first,local:true},b,.5,known),null);
 assert.equal(resolveTreeDrop({...first,archived:true},b,.5,known),null);
 assert.equal(resolveTreeDrop(first,{...b,search:true},.5,known),null);
});
test('parent identity distinguishes a root folder from its own id and archive virtual parent',()=>{
 assert.equal(treeItemParent(a),'');
 assert.equal(treeItemParent({...first,parentId:'__archive'}),'a');
});
