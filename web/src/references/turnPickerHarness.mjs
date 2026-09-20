// Test-only bridge: real History API pages -> real Picker script/template actions.
// Used by the Python contract test; no browser, provider or production DB access.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import vm from 'node:vm';
import {computed,nextTick,ref,reactive,watch,compile,proxyRefs} from 'vue';
import {parse} from '@vue/compiler-sfc';
import * as codec from './codec.js';
import {searchReferences,acceptReferenceSizes,referencePolicyOptions} from './catalog.js';
import * as sizePolicy from './sizePolicy.js';
import {conversationMessages} from './conversationMessages.js';

const descriptor=parse(readFileSync(new URL('./ReferencePicker.vue',import.meta.url),'utf8')).descriptor;
const settled=async()=>{await new Promise(setImmediate);await nextTick();};
const plain=value=>JSON.parse(JSON.stringify(value));

export async function exerciseTurnPicker({source,currentConversation,pages,mode,splitTurnId,filteredTurnId,filterQuery}) {
 const props=reactive({open:true,kind:'chat',query:'',searchable:false,currentConversation,allowDrag:false});
 const stops=[],emitted=[],requests=[];
 const chats=[source];
 const context=vm.createContext({computed,nextTick,ref,watch:(...args)=>{const stop=watch(...args);stops.push(stop);return stop;},
  defineProps:()=>props,defineEmits:()=>(...args)=>emitted.push(args),defineExpose:()=>{},
  referenceCatalog:{ready:true,connected:true,stale:false,includeArchived:false},includeArchivedReferences:()=>{},
  searchReferences:(query,options)=>searchReferences(query,{...options,items:options.items||chats}),...codec,...sizePolicy,acceptReferenceSizes,referencePolicyOptions,conversationMessages,
  Api:{referenceHistory:async(id,offset=0)=>{requests.push([id,offset]);assert.equal(id,source.id);const index=offset===0?0:1;assert.equal(offset,index?pages[0].nextOffset:0);return pages[index];}},
 });
 const run=code=>vm.runInContext(code,context);
 const key=(key,target={tagName:'DIV'})=>{context.event={key,target,preventDefault(){this.defaultPrevented=true;},stopPropagation(){}};return run('key(event)');};
 try {
  vm.runInContext(descriptor.scriptSetup.content.replace(/^import .*?;\n/gm,''),context);
  assert.equal(key('ArrowRight'),true);await settled();
  context.chosenMode=mode;run('mode.value=chosenMode');
  assert.ok(run('results.value.length>0&&results.value.every(row=>row.kind==="message")'));
  const firstMessage=plain(run('results.value[0]'));
  key(' ');assert.equal(run('checked.value.size'),1);

  // Compile the actual group heading, not a second implementation of its button.
  const start=descriptor.template.content.indexOf('<div class="reference-picker-group"');
  assert.notEqual(start,-1);
  const end=descriptor.template.content.indexOf('</div>',start)+6;
  const renderHeading=compile(descriptor.template.content.slice(start,end));
  function activateTurn(turnId) {
   const groups=run('groups.value');
   const group=groups.find(group=>group.rows.some(({item})=>item.groupKey===`turn:${source.id}:${turnId}`));
   assert.ok(group,'the actual directory contains the selected turn group');
   // Before the fix this heading has no button. Fail there, rather than assuming
   // that a callable helper alone makes an action reachable in the real UI.
   const scope=run('({child,mode,candidateState,policyReasonTitle})');
   if(run('typeof chooseTurn')==='function')scope.chooseTurn=run('chooseTurn');
   const tree=renderHeading(proxyRefs({...scope,group}),[]);
   const button=tree.children?.find?.(node=>node.type==='button');
   assert.ok(button,'cross-conversation group must expose an explicit whole-turn button');
   assert.equal(button.props.type,'button');
   assert.equal(button.children,mode==='mention'?'提及整轮':'引用整轮');
   assert.ok(button.props['aria-label'].includes(group.name));
   assert.equal(key('Enter',{tagName:'BUTTON'}),false);
   assert.equal(context.event.defaultPrevented,undefined);
   const count=emitted.length;
   button.props.onClick({stopPropagation(){}});
   assert.equal(emitted.length,count+1);
   const [event,selected]=plain(emitted.at(-1));
   assert.equal(event,'select');assert.equal(Array.isArray(selected),false);
   assert.equal(selected.kind,'turn');assert.equal(selected.id,source.id);assert.equal(selected.itemId,turnId);
   assert.equal(selected.mode||'content',mode);assert.equal(selected.label,group.name);
   assert.equal(run('checked.value.size'),1,'whole-turn action must not expand or replace message checks');
   const token=codec.referenceToken(selected);
   assert.equal(codec.documentToText(codec.textToDocument(token)),token);
   return {selected,token};
  }
  // The input of this split turn is still on page two; the explicit whole-turn
  // action must use turnUuid, not synthesize a selection from loaded messages.
  const splitTurn=activateTurn(splitTurnId);
  await run('loadHistory(true)');await settled();
  const expectedMessages=pages.flatMap(page=>page.items).filter(item=>item.kind==='message');
  assert.equal(run('results.value.length'),expectedMessages.length);
  assert.equal(run('new Set(results.value.map(item=>item.key)).size'),expectedMessages.length);
  assert.equal(run('checked.value.size'),1);
  context.filterQuery=filterQuery;run('queryText.value=filterQuery');await settled();
  const filteredTurn=activateTurn(filteredTurnId);
  const filteredMessage=plain(run('results.value[results.value.length-1]'));
  run('toggle(results.value[results.value.length-1]);choose()');
  const selectedMessages=plain(emitted.at(-1)[1]);
  assert.deepEqual(selectedMessages.map(item=>item.itemId),[firstMessage.itemId,filteredMessage.itemId]);
  assert.ok(selectedMessages.every(item=>item.kind==='message'&&(item.mode||'content')===mode));
  assert.deepEqual(requests,[[source.id,0],[source.id,pages[0].nextOffset]]);
  return {splitTurn,filteredTurn,selectedMessages};
 } finally {for(const stop of stops)stop();}
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href) {
 console.log(JSON.stringify(await exerciseTurnPicker(JSON.parse(readFileSync(0,'utf8')))));
}
