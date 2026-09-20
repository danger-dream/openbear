import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {computed,ref,shallowRef,shallowReactive,reactive,watch,nextTick,compile,proxyRefs,createSSRApp,h} from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse} from '@vue/compiler-sfc';
import {Node,Extension,getSchema} from '@tiptap/core';
import {EditorState,TextSelection,Plugin} from '@tiptap/pm/state';
import {Fragment} from '@tiptap/pm/model';
import {closeHistory} from '@tiptap/pm/history';
import * as codec from './codec.js';
import * as catalog from './catalog.js';
import * as policy from './sizePolicy.js';
import * as editorPolicy from './editorPolicy.js';
import * as presentation from './presentation.js';
import {conversationMessages} from './conversationMessages.js';
import {renderMarkdown} from './rendererHarness.mjs';

const read=name=>readFileSync(new URL(name,import.meta.url),'utf8');
const between=(s,a,b)=>s.slice(s.indexOf(a),s.indexOf(b,s.indexOf(a)));
const plain=value=>JSON.parse(JSON.stringify(value));
const settled=async()=>{await new Promise(setImmediate);await nextTick();};
const pickerSource=parse(read('./ReferencePicker.vue')).descriptor;
const editorSource=read('./ReferenceEditor.vue');
const chat=(id,bodyChars)=>({kind:'chat',id,key:'chat:'+id,label:'会话 '+id,scope:'full',bodyChars});
function picker(t,{items=[chat('long',20001)],selected=[],pages=[],limit=20000}={}){
 catalog.applyCatalogPacket({type:'snapshot',epoch:'size-test',seq:1,includeArchived:false,items,conversationContentLimit:limit});
 const props=reactive({open:true,anchor:null,placement:'top-start',kind:'chat',query:'',currentConversation:'current',selectedReferences:selected,searchable:false,allowDrag:true});
 const emitted=[];
 const ctx=vm.createContext({...codec,...catalog,...policy,...presentation,conversationMessages,computed,ref,nextTick,
  watch:(...args)=>{const stop=watch(...args);t.after(stop);return stop;},defineProps:()=>props,defineEmits:()=>(...args)=>emitted.push(args),defineExpose:()=>{},
  Api:{referenceHistory:async(_id,offset)=>pages[offset?1:0]},
 });
 vm.runInContext(pickerSource.scriptSetup.content.replace(/^import .*?;\n/gm,''),ctx);
 const run=code=>vm.runInContext(code,ctx);
 const key=key=>{ctx.event={key,target:{tagName:'DIV'},preventDefault(){this.defaultPrevented=true;},stopPropagation(){}};return run('key(event)');};
 const names='props,emit,list,searchInput,searchOpen,toggleSearch,localQuery,filter,selected,mode,child,childQuery,checked,queryText,results,groups,tabs,historyMode,historyError,historyBusy,historyMore,selectionError,referenceCatalog,KIND_LABELS,referenceIconHtml,referenceCandidateDetail,includeArchivedReferences,key,back,changeTab,choose,clickItem,toggle,inspectHistory,loadHistory,dragStart,dragEnd,dragging,chooseTurn,candidateState,candidateWarning,policyReasonTitle';
 const render=()=>renderToString(createSSRApp({render:compile(pickerSource.template.content),setup:()=>({...props,...run(`({${names}})`)}),components:{FloatingPanel:{render(){return h('section',{},this.$slots.default?.());}}}}));
 function row(item=run('results.value[0]')){
  const start=pickerSource.template.content.indexOf('<div v-for="{item,index} in group.rows"');
  const end=pickerSource.template.content.indexOf('\n   </template>',start);
  const tree=compile(pickerSource.template.content.slice(start,end))(proxyRefs({...props,...run(`({${names}})`),group:{rows:[{item,index:0}]}}),[]);
  return tree.children[0];
 }
 return {props,ctx,run,key,emitted,render,row};
}

test('actual @ row renders a single-line size warning; mouse and Enter insert mention without changing global mode',async t=>{
 for(const accept of ['click','Enter']){
  const p=picker(t),html=await p.render();
  assert.match(html,/reference-picker-warning-icon/);assert.match(html,/会话过长，将仅提及/);
  assert.match(html,/20,001 字符/);assert.match(html,/20,000 字符/);
  if(accept==='click')p.row().props.onClick();else p.key('Enter');
  assert.equal(p.emitted[0][1].mode,'mention');assert.equal(p.emitted[0][1].modeReason,'conversation_too_long');
  assert.equal(p.run('mode.value'),'content');
  p.run("mode.value='mention'");assert.doesNotMatch(await p.render(),/reference-picker-warning-icon/);
 }
 assert.match(pickerSource.styles[0].content,/reference-picker-warning-icon\{flex:none\}/);
 assert.match(pickerSource.styles[0].content,/reference-picker-warning-text\{[^}]*text-overflow:ellipsis;white-space:nowrap/);
});

test('aggregate warning, 20000/20001 boundary, duplicates and independent mention use the same server-supplied policy',async t=>{
 const old=chat('old',12000),fit=chat('fit',8000),large=chat('large',8001);
 const p=picker(t,{items:[large],selected:[old]});
 assert.match(await p.render(),/合计超限，将仅提及/);p.key('Enter');assert.equal(p.emitted[0][1].modeReason,'conversation_total_limit');
 assert.equal(old.mode,undefined);
 const result=policy.applyConversationPolicy([old,old,{...old,mode:'mention'},fit,large],[],{limit:20000});
 assert.deepEqual(result.map(r=>r.mode||'content'),['content','content','mention','content','mention']);
 assert.equal(policy.applyConversationPolicy([chat('exact',20000)],[],{limit:20000})[0].mode,undefined);
 // Deliberately vary the supplied cap: no second hardcoded frontend constant.
 assert.equal(policy.applyConversationPolicy([chat('small',8)],[],{limit:7})[0].mode,'mention');
 const duplicate=picker(t,{items:[old],selected:[old,old,{...old,mode:'mention'}]});
 assert.doesNotMatch(await duplicate.render(),/reference-picker-warning-icon/);
});

test('message multi-select remains stable across search/page order and short selections survive an oversized chat',async t=>{
 const turn={kind:'turn',id:'A',itemId:'T',key:'turn:A:T',label:'选中轮',bodyChars:21000};
 const one={kind:'message',id:'A',itemId:'first',key:'message:A:first',label:'输入 first',group:'用户消息',bodyChars:12000};
 const two={kind:'message',id:'A',itemId:'second',key:'message:A:second',label:'输出 second',group:'助手回答',bodyChars:9000};
 const p=picker(t,{items:[chat('A',21000)],pages:[{items:[turn,one],hasMore:true,nextOffset:2,conversationContentLimit:20000},{items:[two],hasMore:false,nextOffset:3,conversationContentLimit:20000}]});
 p.key('ArrowRight');await settled();p.key(' ');await p.run('loadHistory(true)');
 p.run("queryText.value='second'");await settled();
 assert.match(await p.render(),/合计超限，将仅提及/);
 p.key(' ');p.key('Enter');
 assert.deepEqual(p.emitted[0][1].map(r=>[r.itemId,r.mode||'content']),[['first','content'],['second','mention']]);
 assert.equal(p.run('mode.value'),'content');
 p.run('chooseTurn(groups.value[0].turn)');assert.equal(p.emitted.at(-1)[1].modeReason,'conversation_too_long');
 p.run('checked.value=new Set();choose()');assert.equal(p.emitted.at(-1)[1].mode,undefined);
});

function editor(text){
 const ctx=vm.createContext({...codec,...editorPolicy,Node,Extension,Plugin,shallowReactive,VueNodeViewRenderer:()=>{},ReferenceNodeView:{},
  referencePolicyOptions:catalog.referencePolicyOptions,closeHistory,props:{disabled:false},mention:ref(null),dragHover:ref(false),lastSelection:null,closeMention(){},notify(){},emit(){}});
 vm.runInContext(between(editorSource,'const DocumentNode=','const Undo='),ctx);
 vm.runInContext(between(editorSource,'const SizePolicy=','function notify()'),ctx);
 const schema=vm.runInContext('getSchema([DocumentNode,Paragraph,Text,HardBreak,Reference])',Object.assign(ctx,{getSchema}));
 const plugins=vm.runInContext('SizePolicy.config.addProseMirrorPlugins()',ctx);
 let state=EditorState.create({schema,doc:schema.nodeFromJSON(codec.textToDocument(text)),plugins});
 const dispatch=tr=>{state=state.applyTransaction(tr).state;};
 const instance={get state(){return state;},getJSON:()=>state.doc.toJSON(),storage:{reference:{invalid:new Set()}},view:{dispatch,posAtCoords:()=>({pos:1})},
  commands:{insertContent:content=>{dispatch(state.tr.replaceWith(state.selection.from,state.selection.to,Fragment.fromArray(content.map(n=>schema.nodeFromJSON(n)))));}},
  chain(){return {focus(){return this;},insertContentAt(range,content){this.range=range;this.content=content;return this;},run(){dispatch(state.tr.replaceWith(this.range.from,this.range.to,Fragment.fromArray(this.content.map(n=>schema.nodeFromJSON(n)))));return true;}};}};
 ctx.editor=shallowRef(instance);
 vm.runInContext(between(editorSource,'function insertReference(','function onKey('),ctx);
 vm.runInContext(between(editorSource,'function onPaste(','function copy('),ctx);
 vm.runInContext(between(editorSource,'function drop(','function externalInsert('),ctx);
 return {ctx,instance,schema,dispatch,run:code=>vm.runInContext(code,ctx),text:()=>codec.documentToText(state.doc.toJSON())};
}

test('real Editor insertion at the FRONT reserves old content, for picker insertion, pasted tokens and drag/drop',()=>{
 const old=chat('old',15000),added=chat('added',6000);
 catalog.applyCatalogPacket({type:'snapshot',epoch:'editor',seq:1,includeArchived:false,items:[old,added],conversationContentLimit:20000});
 const original='当前问题 '+codec.referenceToken(old)+' 普通尾文';
 for(const entry of ['insert','paste','drop']){
  const e=editor(original);e.ctx.input=added;
  if(entry==='insert')e.run('insertReference(input,{from:1,to:1})');
  else if(entry==='paste'){
   e.dispatch(e.instance.state.tr.setSelection(TextSelection.create(e.instance.state.doc,1)));
   e.ctx.event={clipboardData:{getData:type=>type==='text/plain'?codec.referenceToken(added)+' ':''},preventDefault(){}};
   e.run('onPaste(editor.value.view,event)');
  }else{
   e.ctx.event={dataTransfer:{getData:type=>type===codec.REFERENCE_MIME?JSON.stringify(added):''},preventDefault(){},stopPropagation(){},clientX:0,clientY:0};
   e.run('drop(event)');
  }
  const refs=codec.referencesInText(e.text());assert.equal(refs[0].mode,'mention');assert.equal(refs[0].modeReason,'conversation_total_limit');assert.equal(refs[1].mode,undefined);
  assert.equal(e.text(),codec.referenceToken({...added,mode:'mention',modeReason:'conversation_total_limit'})+' '+original);
 }
});

test('real Inspector scope/mode update uses the shared policy and can shrink an automatic mention',()=>{
 const big={...chat('big',25000),recentTurnChars:[200,24800]},old=chat('old',19000);
 catalog.applyCatalogPacket({type:'snapshot',epoch:'inspect',seq:1,includeArchived:false,items:[big,old],conversationContentLimit:20000});
 const e=editor(codec.referenceToken({...big,mode:'mention',modeReason:'conversation_too_long'})+' '+codec.referenceToken(old));
 const getNode=()=>editorPolicy.referenceNodes(e.instance.state.doc)[0];
 const c=vm.createContext({...codec,...editorPolicy,...policy,referencePolicyOptions:catalog.referencePolicyOptions,
  props:{editor:e.instance,getPos:()=>getNode().pos,updateAttributes:value=>e.dispatch(e.instance.state.tr.setNodeMarkup(getNode().pos,undefined,{...getNode().ref,...value}))}});
 vm.runInContext(between(read('./ReferenceNodeView.vue'),'function updateReference(','function inspect('),c);
 const s=vm.createContext({ref,reference:ref({...getNode().ref,bodyChars:25000}),scope:ref('recent'),turns:ref(1),mode:ref('mention'),load(){},update:vm.runInContext('updateReference',c)});
 vm.runInContext(between(read('./ReferenceInspector.vue'),'function applyMode(','onMounted('),s);
 vm.runInContext('applyScope()',s);
 assert.equal(getNode().ref.mode,'');assert.equal(getNode().ref.scope,'recent');assert.equal(getNode().ref.turns,1);
 vm.runInContext("scope.value='full';applyScope()",s);assert.equal(getNode().ref.mode,'mention');
 assert.equal(editorPolicy.referenceNodes(e.instance.state.doc)[1].ref.mode,'');
});

test('a late real preview response never overwrites pending ordinary input; reference order survives front insertion',async()=>{
 const old=chat('old',12000),added=chat('added',8001);
 catalog.stopReferenceCatalog({clear:true});
 catalog.applyCatalogPacket({type:'snapshot',epoch:'late',seq:1,includeArchived:false,items:[],conversationContentLimit:20000});
 const original='用户原问题 '+codec.referenceToken(old),e=editor(original);
 const order=editorPolicy.referenceOrder([],e.instance.state.doc);
 e.ctx.input=added;e.run('insertReference(input,{from:1,to:1})');
 const nextOrder=editorPolicy.referenceOrder(order,e.instance.state.doc);
 assert.equal(nextOrder[0],codec.referenceKey(old));
 let finish,scheduled=0;
 Object.assign(e.ctx,{previewGeneration:0,previewTimer:null,policyOrder:nextOrder,currentReferences:ref(codec.referencesInText(e.text())),checking:ref(false),validation:ref(null),acceptReferenceSizes:catalog.acceptReferenceSizes,
  props:{currentConversation:'current'},setTimeout(){scheduled++;return 1;},clearTimeout(){},
  Api:{referencePreview:(_text,_conv,keys)=>{assert.deepEqual(plain(keys),nextOrder);return new Promise(resolve=>finish=resolve);}}});
 vm.runInContext(between(editorSource,'async function preview()','watch(publicPreviewKey'),e.ctx);
 const request=e.run('preview()');
 e.dispatch(e.instance.state.tr.insertText(' 等待时新增的正文',e.instance.state.doc.content.size-1));const pending=e.text();
 finish({ok:true,bindings:[{...added,key:codec.referenceKey({...added,mode:'mention'}),requestedKey:codec.referenceKey(added),mode:'mention',modeReason:'conversation_total_limit'}],conversationContentLimit:20000});
 await request;assert.equal(e.text(),pending);assert.equal(scheduled,1);
});

test('history renderer, plain-text chips and copy codecs use effective bindings and preserve normal Markdown',()=>{
 const value=chat('source',20001),binding={...value,mode:'mention',modeReason:'conversation_too_long',contentLimit:20000};
 const text='原问题 '+codec.referenceToken(value)+' 后文';
 const html=renderMarkdown(text,{references:[binding]});
 assert.match(html,/仅提及/);assert.match(html,/会话过长，已仅提及/);assert.match(html,/20,001/);assert.match(html,/原问题/);assert.match(html,/后文/);
 const source=read('./ReferencePlainText.vue');
 const c=vm.createContext({computed,...codec,props:{text,references:[binding]}});
 vm.runInContext(between(source,'const parts=','function inspect('),c);
 const actual=vm.runInContext('parts.value.find(part=>part.type==="reference").attrs',c);
 assert.equal(actual.mode,'mention');assert.equal(codec.referenceFromUrl(codec.referenceUrl(actual)).modeReason,'conversation_too_long');
 assert.equal(codec.documentToText(codec.textToDocument('普通 **Markdown**\n\n- 原文')), '普通 **Markdown**\n\n- 原文');
});
