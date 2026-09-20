import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {computed,nextTick,ref,shallowRef,reactive,shallowReactive,watch,compile,createSSRApp,h} from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse,compileTemplate} from '@vue/compiler-sfc';
import {Node,getSchema} from '@tiptap/core';
import {EditorState,TextSelection} from '@tiptap/pm/state';
import {history,closeHistory,undo,redo} from '@tiptap/pm/history';
import {Fragment} from '@tiptap/pm/model';
import * as codec from './codec.js';
import {conversationMessages} from './conversationMessages.js';
import {searchReferences,acceptReferenceSizes,referencePolicyOptions} from './catalog.js';
import * as sizePolicy from './sizePolicy.js';
import {referenceIconHtml,referenceCandidateDetail,referenceScopeLabel,referenceTitle} from './presentation.js';
const read=name=>readFileSync(new URL(name,import.meta.url),'utf8');
const plain=value=>JSON.parse(JSON.stringify(value));
const settled=async()=>{await new Promise(setImmediate);await nextTick();};
const pickerSource=parse(read('./ReferencePicker.vue')).descriptor;
const between=(s,a,b)=>s.slice(s.indexOf(a),s.indexOf(b,s.indexOf(a)));
const chats=[{kind:'chat',key:'chat:A',id:'A',label:'会话 A',group:'会话'},{kind:'chat',key:'chat:B',id:'B',label:'会话 B',group:'会话'}];
const turns=Array.from({length:3},(_,i)=>({kind:'turn',key:`turn:A:t${i}`,id:'A',itemId:`t${i}`,label:`第${i}轮问答`,group:'整轮问答'}));
const historyDirectory=turns.flatMap((turn,i)=>[turn,
 {kind:'message',key:`message:A:assistant:t${i}`,id:'A',itemId:`assistant:t${i}`,label:`第${i}轮模型结果`,group:'助手回答'},
 {kind:'message',key:`message:A:user:t${i}`,id:'A',itemId:`user:t${i}`,label:`第${i}轮输入`,group:'用户消息'}]);
const messageItems=conversationMessages(historyDirectory);
const selectedMessage=item=>codec.normalizeReference({...item,label:`${item.roleLabel} · ${item.label}`});
function picker(t,options={}){
 const props=reactive({open:true,anchor:null,placement:'top-start',kind:'chat',query:'',searchable:false,currentConversation:'current',allowDrag:false,...options.props});
 const emitted=[],requests=[],catalog=reactive({items:chats,ready:true,stale:false,connected:true,includeArchived:false});
 const context=vm.createContext({computed,ref,nextTick,watch:(...args)=>{const stop=watch(...args);t.after(stop);return stop;},
  defineProps:()=>props,defineEmits:()=>(...args)=>emitted.push(args),defineExpose:()=>{},
  referenceCatalog:catalog,searchReferences:(q,opts)=>searchReferences(q,{...opts,items:opts.items||catalog.items}),includeArchivedReferences:value=>catalog.includeArchived=value,
  ...codec,...sizePolicy,acceptReferenceSizes,referencePolicyOptions,referenceIconHtml,referenceCandidateDetail,conversationMessages,
  Api:{referenceHistory:async(...args)=>{requests.push(args);return options.history?options.history(...args):{items:historyDirectory,hasMore:false,nextOffset:9};}},
 });
 vm.runInContext(pickerSource.scriptSetup.content.replace(/^import .*?;\n/gm,''),context);
 const run=code=>vm.runInContext(code,context);
 const key=(key,extra={})=>{context.event={key,preventDefault(){this.defaultPrevented=true;},stopPropagation(){this.stopped=true;},...extra};return run('key(event)');};
 return {props,emitted,requests,run,key,context};
}
test('real picker → opens input/output messages under @chat/, Space multi-selects and Enter/Tab insert only selected exact messages',async t=>{
 for(const accept of ['Enter','Tab']){
  const p=picker(t);assert.equal(p.run('mode.value'),'content');
  assert.equal(p.key('ArrowRight'),true);await settled();
  assert.deepEqual(p.requests,[['A',0]]);assert.equal(p.run('checked.value.size'),0);
  p.key(' ');p.key('ArrowDown');p.key(' ');p.key('ArrowDown');
  assert.equal(p.key(accept),true);assert.equal(p.context.event.defaultPrevented,true);
  assert.deepEqual(plain(p.emitted[0]),['select',messageItems.slice(0,2).map(selectedMessage)]);
 }
});
test('unselected child inserts highlit message, never the whole turn/chat; empty/loading list inserts nothing',async t=>{
 const p=picker(t);p.key('ArrowRight');p.key('Tab');assert.deepEqual(p.emitted,[]);
 await settled();p.key('ArrowDown');p.key('Tab');assert.equal(p.emitted[0][1].itemId,'user:t0');assert.equal(p.emitted[0][1].kind,'message');
 const output=picker(t);output.key('ArrowRight');await settled();output.key('Tab');assert.deepEqual(plain(output.emitted[0][1]),selectedMessage(messageItems[0]));
});
test('mode shortcut preserves query and selections; digits, IME and native select keys remain usable',async t=>{
 const p=picker(t,{props:{query:'会话'}});p.key('ArrowRight');await settled();p.key(' ');
 assert.equal(p.key('1'),false);assert.equal(p.key('2'),false);
 assert.equal(p.key('1',{ctrlKey:true,altKey:true,code:'Digit1',isComposing:true}),false);
 assert.equal(p.key('ArrowDown',{target:{tagName:'SELECT'}}),false);
 assert.equal(p.key('1',{ctrlKey:true,altKey:true,code:'Digit1'}),true);
 assert.equal(p.run('mode.value'),'mention');assert.equal(p.run('localQuery.value'),'会话');assert.equal(p.run('checked.value.size'),1);
 p.key('Enter');assert.equal(p.emitted[0][1].mode,'mention');
});
test('Esc closes one layer at a time, restores original candidate, and reopening uses the default mode',async t=>{
 const p=picker(t);p.key('ArrowDown');p.key('ArrowRight');await settled();p.key(' ');
 p.key('1',{ctrlKey:true,altKey:true});p.key('Escape');await settled();
 assert.equal(p.run('child.value'),null);assert.equal(p.run('selected.value'),1);assert.deepEqual(p.emitted,[]);
 p.key('Escape');assert.deepEqual(p.emitted,[['close']]);
 p.props.open=false;await settled();assert.equal(p.key('1',{ctrlKey:true,altKey:true}),false);p.props.open=true;await settled();
 assert.equal(p.run('mode.value'),'content');assert.equal(p.run('checked.value.size'),0);
 const floating=read('./FloatingPanel.vue');let closed=0;
 const ctx=vm.createContext({emit:()=>closed++});vm.runInContext(between(floating,'function key(','watch(()=>[props.open'),ctx);
 ctx.event={key:'Escape',defaultPrevented:true};vm.runInContext('key(event)',ctx);assert.equal(closed,0);
 ctx.event={key:'Escape'};vm.runInContext('key(event)',ctx);assert.equal(closed,1);
});
test('independent search accepts spaces, search arrows enter child list for Space selection, and filtered-out checks survive',async t=>{
 const p=picker(t);p.key('ArrowRight');await settled();p.key(' ');
 p.run("queryText.value='第1*结果'");await settled();assert.equal(p.run('results.value.length'),1);
 assert.equal(p.key(' ',{target:{tagName:'INPUT'}}),false);
 let focused=0;p.context.listNode={focus:()=>focused++,querySelector:()=>null};p.run('list.value=listNode');p.key('ArrowDown',{target:{tagName:'INPUT'}});assert.equal(focused,1);
 p.key(' ');p.key('Tab');assert.deepEqual(plain(p.emitted[0][1]).map(r=>r.itemId),['assistant:t0','assistant:t1']);
 const q=picker(t);q.run("queryText.value='会话 A'");await settled();assert.equal(q.run('results.value[0].id'),'A');
});
test('history pagination retains selections and rejects a late response after back/close',async t=>{
 let finish;
 const p=picker(t,{history:(_id,offset)=>offset?new Promise(resolve=>finish=resolve):{items:historyDirectory.slice(0,2),hasMore:true,nextOffset:2}});
 p.key('ArrowRight');await settled();p.key(' ');p.key('ArrowDown');
 assert.equal(p.run('checked.value.size'),1);assert.deepEqual(p.requests[1],['A',2]);finish({items:historyDirectory.slice(2,5),hasMore:false,nextOffset:5});await settled();
 assert.equal(p.run('selected.value'),1);assert.equal(p.run('historyItems.value[1].groupKey'),turns[0].key);assert.equal(p.run('historyItems.value[2].groupKey'),turns[1].key);p.key(' ');p.key('Enter');assert.deepEqual(plain(p.emitted[0][1]).map(r=>r.itemId),['assistant:t0','user:t0']);
 const late=picker(t,{history:()=>new Promise(resolve=>finish=resolve)});late.key('ArrowRight');late.key('Escape');finish({items:historyDirectory,hasMore:true,nextOffset:9});await settled();
 assert.equal(late.run('historyItems.value.length'),0);assert.equal(late.run('historyBusy.value'),false);
});
test('original history tab keeps individual message choices and drag preserves mention mode',async t=>{
 const p=picker(t,{props:{kind:'',allowDrag:true},history:()=>({items:[{...turns[0],kind:'message',itemId:'user:t0'}],hasMore:false})});
 p.run("changeTab('history')");await settled();assert.deepEqual(p.requests,[['current',0]]);
 p.run("mode.value='mention';choose()");assert.equal(p.emitted[0][1].kind,'message');assert.equal(p.emitted[0][1].mode,'mention');
 const data={};p.context.drag={dataTransfer:{setData:(k,v)=>data[k]=v}};p.context.item=chats[0];p.run('dragStart(drag,item)');
 assert.equal(JSON.parse(data[codec.REFERENCE_MIME]).mode,'mention');assert.match(data['text/plain'],/mode=mention/);
});
test('real picker template exposes manual mode, visible @chat drilldown, multi-select checks and explicit insert action',async t=>{
 const p=picker(t);const names='props,emit,list,searchInput,searchOpen,toggleSearch,localQuery,filter,selected,mode,child,childQuery,checked,queryText,results,groups,tabs,historyMode,historyError,historyBusy,historyMore,selectionError,referenceCatalog,KIND_LABELS,referenceIconHtml,referenceCandidateDetail,includeArchivedReferences,key,back,changeTab,choose,clickItem,toggle,inspectHistory,loadHistory,dragStart,dragEnd,dragging,chooseTurn,candidateState,candidateWarning,policyReasonTitle';
 const render=async()=>renderToString(createSSRApp({render:compile(pickerSource.template.content),setup:()=>({...p.props,...p.run(`({${names}})`)}),components:{FloatingPanel:{render(){return h('section',{},this.$slots.default?.());}}}}));
 let html=await render();assert.doesNotMatch(html,/<input[^>]+class="reference-picker-search-input"/);assert.doesNotMatch(html,/class="reference-picker-(?:mode|breadcrumb|selection|search)"/);assert.match(html,/aria-label="引用模式"/);assert.match(html,/aria-pressed="true"[^>]*>引用内容/);assert.match(html,/aria-pressed="false"[^>]*>仅提及/);assert.equal(p.run('mode.value'),'content');assert.doesNotMatch(pickerSource.template.content,/<select/);assert.match(html,/消息 ›/);assert.match(html,/height="430"/);assert.match(html,/Enter \/ Tab/);
 p.key('ArrowRight');await settled();p.key(' ');html=await render();assert.match(html,/aria-multiselectable="true"/);assert.match(html,/已选 1 条/);assert.match(html,/用户输入/);assert.match(html,/模型输出/);assert.match(html,/第0轮模型结果/);assert.match(html,/height="430"/);assert.match(html,/aria-selected="true"/);assert.match(html,/返回会话候选/);assert.doesNotMatch(html,/<input[^>]+class="reference-picker-search-input"/);assert.doesNotMatch(html,/class="reference-picker-(?:mode|breadcrumb|selection|search)"/);assert.equal((html.match(/class="reference-picker-footer"/g)||[]).length,1);
 await p.run('toggleSearch()');html=await render();assert.match(html,/<input[^>]+class="reference-picker-search-input"/);assert.match(html,/height="430"/);assert.equal((html.match(/class="reference-picker-heading"/g)||[]).length,1);assert.equal(p.run('checked.value.size'),1);
 for(const file of ['ReferencePicker.vue','ReferenceEditor.vue','ReferenceInspector.vue','FloatingPanel.vue']){
  const descriptor=parse(read('./'+file)).descriptor;assert.deepEqual(compileTemplate({source:descriptor.template.content,filename:file,id:file}).errors,[]);
 }
});
test('mode segmented switch clicks directly in both directions and focused controls do not insert references',async t=>{
 const template=between(pickerSource.template.content,'<div class="reference-picker-mode-switch"','</div>')+'</div>';
 const render=compile(template),state=reactive({mode:'content'});
 let tree=render(state,[]),buttons=tree.children.filter(node=>node.type==='button');assert.equal(buttons.length,2);assert.equal(buttons[0].props['aria-pressed'],true);
 let prevented=0;buttons[1].props.onMousedown({preventDefault(){prevented++;}});assert.equal(prevented,1);
 buttons[1].props.onClick();assert.equal(state.mode,'mention');tree=render(state,[]);assert.match(tree.props.class,/is-mention/);buttons=tree.children.filter(node=>node.type==='button');assert.equal(buttons[1].props['aria-pressed'],true);
 buttons[0].props.onClick();assert.equal(state.mode,'content');
 const p=picker(t);p.key('ArrowRight');await settled();p.key(' ');const target={tagName:'BUTTON',closest:selector=>selector==='[data-reference-mode-switch]'?{}:null};
 for(const key of ['Enter',' ','Tab'])assert.equal(p.key(key,{target}),false);assert.deepEqual(p.emitted,[]);assert.equal(p.run('checked.value.size'),1);
 p.key('1',{target,ctrlKey:true,altKey:true});assert.equal(p.run('mode.value'),'mention');assert.equal(p.run('checked.value.size'),1);
});
test('header search is opt-in for @, preserves filters and checks, and retains keyboard focus when entering/backing out of messages',async t=>{
 const p=picker(t);assert.equal(p.run('searchOpen.value'),false);
 let searchFocus=0,listFocus=0;p.context.searchNode={focus:()=>searchFocus++};p.context.listNode={focus:()=>listFocus++,querySelector:()=>null};p.run('searchInput.value=searchNode;list.value=listNode');
 await p.run('toggleSearch()');assert.equal(searchFocus,1);p.run("queryText.value='会话 A'");
 p.key('ArrowRight');await settled();assert.equal(p.run('searchOpen.value'),false);assert.equal(listFocus,1);p.key(' ');
 await p.run('toggleSearch()');p.run("queryText.value='第1*结果'");await settled();
 await p.run('toggleSearch()');assert.equal(p.run('childQuery.value'),'第1*结果');assert.equal(p.run('checked.value.size'),1);
 p.key('Escape');await settled();assert.equal(p.run('searchOpen.value'),true);assert.equal(p.run('localQuery.value'),'会话 A');assert.equal(searchFocus,3);
 const shelf=picker(t,{props:{searchable:true}});assert.equal(shelf.run('searchOpen.value'),true);
});
test('message projection separates inputs and outputs by exact turn, including identical user prompts',async t=>{
 const rows=historyDirectory.map(item=>item.kind==='turn'?{...item,label:'继续'}:item);
 const messages=conversationMessages(rows);assert.equal(messages.length,6);assert.ok(messages.every(item=>item.kind==='message'));
 assert.deepEqual(messages.slice(0,2).map(item=>item.roleLabel),['模型输出','用户输入']);
 const p=picker(t,{history:()=>({items:rows,hasMore:false})});p.key('ArrowRight');await settled();
 assert.equal(p.run('groups.value.length'),3);assert.equal(p.run('checked.value.size'),0);
 p.key('Tab');assert.equal(p.emitted[0][1].kind,'message');assert.equal(p.emitted[0][1].itemId,'assistant:t0');assert.match(p.emitted[0][1].label,/^模型输出/);
});
test('real floating positioning fixes the picker height across content states, clamps small viewports, and leaves other panels auto-sized',async()=>{
 const source=read('./FloatingPanel.vue'),props={open:true,anchor:{},placement:'top-start',width:360,height:430};
 const viewport={width:1000,height:900},panel={style:{},naturalHeight:50,get offsetHeight(){return parseFloat(this.style.height)||this.naturalHeight;}};
 const c=vm.createContext({props,window:{visualViewport:viewport,innerWidth:1000,innerHeight:900},innerHeight:900,panel:shallowRef(panel),style:ref({}),
  computePosition:async(_anchor,element)=>({x:20,y:800-element.offsetHeight}),offset:()=>{},flip:()=>{},shift:()=>{}});
 vm.runInContext('let positionGeneration=0;'+between(source,'async function position()','function schedule()'),c);
 for(const naturalHeight of [50,150,700,0]){panel.naturalHeight=naturalHeight;await vm.runInContext('position()',c);assert.equal(c.style.value.height,'430px');assert.equal(c.style.value.top,'370px');}
 viewport.height=320;await vm.runInContext('position()',c);assert.equal(c.style.value.height,'288px');
 props.height=0;panel.naturalHeight=100;await vm.runInContext('position()',c);assert.equal(c.style.value.height,'');
 assert.match(pickerSource.styles[0].content,/\.reference-picker-list\{flex:1 1 0;overflow-y:auto;overscroll-behavior:contain;min-height:0;/);
});
test('real editor batch insertion replaces only @query, preserves mode, and is one undo/redo action without sending',()=>{
 const source=read('./ReferenceEditor.vue');let schema;
 const c=vm.createContext({...codec,Node,shallowReactive,VueNodeViewRenderer:()=>{},ReferenceNodeView:{},ref,getSchema,closeHistory,
  props:{disabled:false},mention:ref({from:3,to:6}),lastSelection:null,closeMention(){},notify(){}});
 vm.runInContext(between(source,'const DocumentNode=','const Undo='),c);
 schema=vm.runInContext('getSchema([DocumentNode,Paragraph,Text,HardBreak,Reference])',c);
 let state=EditorState.create({schema,doc:schema.nodeFromJSON(codec.textToDocument('前文@ab 后文')),plugins:[history()]});
 const instance={get state(){return state;},view:{dispatch:tr=>{state=state.apply(tr);}},chain(){return {focus(){return this;},insertContentAt(range,content){this.range=range;this.content=content;return this;},run(){const tr=state.tr.replaceWith(this.range.from,this.range.to,Fragment.fromArray(this.content.map(n=>schema.nodeFromJSON(n))));instance.view.dispatch(tr);return true;}};}};
 c.editor=shallowRef(instance);vm.runInContext(between(source,'function insertReference(','function onKey('),c);
 c.inputs=[{...turns[0],mode:'mention'},turns[2]];vm.runInContext('insertReference(inputs)',c);
 const result=codec.documentToText(state.doc.toJSON());assert.equal(result,'前文'+c.inputs.map(codec.referenceToken).join(' ')+'  后文');
 assert.deepEqual(codec.referencesInText(result).map(r=>r.mode||'content'),['mention','content']);
 assert.equal(undo(state,instance.view.dispatch),true);assert.equal(codec.documentToText(state.doc.toJSON()),'前文@ab 后文');
 assert.equal(redo(state,instance.view.dispatch),true);assert.equal(codec.documentToText(state.doc.toJSON()),result);
});
test('mention mode survives six-kind codecs, keys and presentation without altering old content references',()=>{
 const refs=['doc','mem','secret','chat','turn','message'].map(kind=>({kind,id:['doc','mem','secret'].includes(kind)?'17':'A',label:'来源',scope:kind==='chat'?'recent':'full',...(kind==='chat'?{turns:7}:{}),...(['turn','message'].includes(kind)?{itemId:'t1'}:{})}));
 for(const r of refs){const mention={...r,mode:'mention'};assert.equal(codec.referenceKey(mention),codec.referenceKey(r)+':mention');assert.deepEqual(codec.referenceFromUrl(codec.referenceUrl(mention),r.label),mention);const text=[r,mention,mention].map(codec.referenceToken).join(' ');assert.equal(codec.referencesInText(text).length,2);assert.equal(codec.documentToText(codec.textToDocument(text)),text);assert.equal(referenceScopeLabel(mention),'仅提及');assert.doesNotMatch(referenceTitle(mention),/提供凭证/);}
 for(const query of ['mode=unknown','mode=','mode=mention&mode=content'])assert.equal(codec.referenceFromUrl('openbear://ref/doc/17?'+query),null);assert.equal(codec.normalizeReference({...refs[0],mode:'other'}),null);
 assert.deepEqual(codec.referenceFromUrl('openbear://ref/doc/17?mode=content','来源'),refs[0]);
});
test('wildcards match ordered Chinese, pinyin and initials across text and spaces, without fuzzy false positives',()=>{
 const item={key:'doc:1',kind:'doc',id:'1',label:'带空格的名称，可以在选择器自己的搜索框里搜索',name:'example'};
 const english={key:'doc:2',kind:'doc',id:'2',label:'OpenBear Build Guide v1.2',name:'build'};
 for(const query of ['dkg*ss','DKG**SS','带空格*搜索','daikongge*sousuo','dkg * ss','*dkg*ss*','带空格的名称 * 搜索'])assert.ok(searchReferences(query,{items:[item]}).length,query);
 for(const query of ['ss*dkg','dkg*zzzzz','dkg[.*ss'])assert.equal(searchReferences(query,{items:[item]}).length,0,query);
 for(const query of ['OpenBear * Guide','build*1.2','openbear build'])assert.ok(searchReferences(query,{items:[english]}).length,query);
});
test('native buttons activate their real template actions, never the highlit candidate',async t=>{
 const p=picker(t);p.key('ArrowRight');await settled();
 const names='emit,child,childQuery,back,searchOpen,queryText,KIND_LABELS,referenceCatalog,mode,toggleSearch,localQuery';
 const heading=between(pickerSource.template.content,'<div class="reference-picker-heading">','<div v-if="!kind&&!child"');
 const render=compile(heading);
 const state={...p.props,...p.run(`({${names}})`)};
 // compile() expects proxy-ref values, as the real component render context does.
 const {proxyRefs}=await import('vue');const tree=render(proxyRefs(state),[]);
 const buttons=tree.children.filter(n=>n.type==='button');
 for(const button of buttons){
  const target={tagName:'BUTTON',closest:()=>null};
  for(const key of ['Enter',' ','Tab']){assert.equal(p.key(key,{target}),false);assert.equal(p.context.event.defaultPrevented,undefined);}
 }
 assert.deepEqual(p.emitted,[]);
 const backButton=buttons.find(n=>n.props['aria-label']==='返回会话候选');
 backButton.props.onClick();assert.equal(p.run('child.value'),null);
 const closeButton=buttons.find(n=>n.props['aria-label']==='关闭引用选择器');
 closeButton.props.onClick();assert.deepEqual(p.emitted,[['close']]);
});
test('Tab traverses inputs/controls, Shift+Tab never inserts, editor/list forward Tab still accepts',async t=>{
 const p=picker(t);p.key('ArrowRight');await settled();
 for(const target of [{tagName:'INPUT',type:'text'},{tagName:'INPUT',type:'checkbox'},{tagName:'BUTTON'},{tagName:'SELECT'}]){
  for(const shiftKey of [false,true])assert.equal(p.key('Tab',{target,shiftKey}),false);
 }
 for(const target of [{tagName:'DIV'},{tagName:'INPUT',type:'text'}])assert.equal(p.key('Tab',{target,shiftKey:true}),false);
 assert.equal(p.key('Enter',{target:{tagName:'INPUT',type:'checkbox'}}),false);
 assert.deepEqual(p.emitted,[]);
 assert.equal(p.key('Tab',{target:{tagName:'DIV'}}),true);assert.equal(p.emitted.length,1);
 const ime=picker(t);for(const extra of [{isComposing:true},{keyCode:229}])for(const key of ['Enter','Tab','Escape'])assert.equal(ime.key(key,extra),false);
 assert.deepEqual(ime.emitted,[]);
});
test('filtered empty child can page with Down, retains checks, and empty Tab remains focus navigation',async t=>{
 const p=picker(t,{history:(_id,offset)=>({items:offset?historyDirectory.slice(3):historyDirectory.slice(0,3),hasMore:!offset,nextOffset:offset+3})});
 p.key('ArrowRight');await settled();p.key(' ');
 p.run("queryText.value='第1*结果'");await settled();assert.equal(p.run('results.value.length'),0);
 assert.equal(p.key('Tab',{target:{tagName:'INPUT'}}),false);assert.deepEqual(p.emitted,[]);
 p.key('ArrowDown');await settled();assert.deepEqual(p.requests,[['A',0],['A',3]]);
 assert.equal(p.run('results.value.length'),1);assert.equal(p.run('selected.value'),0);assert.equal(p.run('checked.value.size'),1);
 p.key(' ');p.key('Enter');assert.deepEqual(plain(p.emitted[0][1]).map(r=>r.itemId),['assistant:t0','assistant:t1']);
 const empty=picker(t,{history:()=>({items:[],hasMore:false})});empty.key('ArrowRight');await settled();
 assert.equal(empty.key('Tab'),false);assert.deepEqual(empty.emitted,[]);
});
