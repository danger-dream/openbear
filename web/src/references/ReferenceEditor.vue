<script setup>
import {computed,nextTick,onBeforeUnmount,onMounted,ref,shallowRef,shallowReactive,watch} from 'vue';
import {Editor,EditorContent,VueNodeViewRenderer} from '@tiptap/vue-3';
import {Extension,Node} from '@tiptap/core';
import {history,undo,redo,closeHistory} from '@tiptap/pm/history';
import {TextSelection,Plugin} from '@tiptap/pm/state';
import ReferenceNodeView from './ReferenceNodeView.vue';
import ReferencePicker from './ReferencePicker.vue';
import {documentToText,textToDocument,normalizeReference,referenceToken,referenceUrl,REFERENCE_MIME,referenceFromUrl,referencesInText,referenceErrorText} from './codec.js';
import {referenceCatalog,referenceItem,acceptReferenceSizes,referencePolicyOptions} from './catalog.js';
import {enforceNewReferencePolicy,applyPreviewBindings,referenceOrder,SIZE_POLICY_META} from './editorPolicy.js';
import {policyReasonText} from './sizePolicy.js';
import {Api} from '../api.js';
import {mentionAtPosition} from './mention.js';
import './reference.css';

const props=defineProps({modelValue:{type:String,default:''},currentConversation:{type:String,default:''},placeholder:{type:String,default:'在这里输入消息，按 Enter 发送'},disabled:Boolean});
const emit=defineEmits(['update:modelValue','send','paste','height-change']);
const editor=shallowRef(null),host=ref(null),picker=ref(null),mention=ref(null),dragHover=ref(false),validation=ref(null),checking=ref(false);
let lastSelection=null,previewGeneration=0,previewTimer=null,policyOrder=[];
const currentReferences=computed(()=>referencesInText(props.modelValue));
const sizeWarnings=computed(()=>[...new Set(currentReferences.value.map(item=>policyReasonText(item.modeReason)).filter(Boolean))].map(text=>text.replace('将仅提及','已仅提及')));
const publicPreviewKey=computed(()=>currentReferences.value.map(item=>{
 const source=referenceItem(['turn','message'].includes(item.kind)?{kind:'chat',id:item.id}:item);
 // A running conversation's status changes are not requests to reread its
 // entire History. Sending/inspecting always resolves the latest source.
 const revision=item.kind==='chat'?Boolean(source):(source?.revision||source?.updatedAt||'');
 return referenceToken(item)+'|'+revision+'|'+(source?.label||'');
}).join('\n'));
const DocumentNode=Node.create({name:'doc',topNode:true,content:'paragraph+'});
const Paragraph=Node.create({name:'paragraph',group:'block',content:'inline*',parseHTML:()=>[{tag:'p'}],renderHTML:()=>['p',0]});
const Text=Node.create({name:'text',group:'inline'});
const HardBreak=Node.create({name:'hardBreak',group:'inline',inline:true,selectable:false,parseHTML:()=>[{tag:'br'}],renderHTML:()=>['br']});
const Reference=Node.create({
 name:'reference',group:'inline',inline:true,atom:true,selectable:true,draggable:false,
 addStorage(){return {invalid:shallowReactive(new Set())};},
 addAttributes(){return Object.fromEntries(['kind','id','label','scope','turns','itemId','mode','modeReason','bodyChars','contentLimit'].map(key=>[key,{default:key==='scope'?'full':''}]));},
 parseHTML(){return [{tag:'span[data-reference]',getAttrs:element=>referenceFromUrl(element.getAttribute('data-reference'),element.getAttribute('data-reference-label')||element.textContent)}];},
 renderHTML({node}){return ['span',{'data-reference':referenceUrl(node.attrs),class:'reference-inline-node'},node.attrs.label];},
 addNodeView(){return VueNodeViewRenderer(ReferenceNodeView);},
});
const Undo=Extension.create({name:'referenceHistory',addProseMirrorPlugins(){return [history()];},addKeyboardShortcuts(){return {'Mod-z':()=>undo(this.editor.state,this.editor.view.dispatch),'Mod-Shift-z':()=>redo(this.editor.state,this.editor.view.dispatch),'Mod-y':()=>redo(this.editor.state,this.editor.view.dispatch)};}});

const SizePolicy=Extension.create({name:'referenceSizePolicy',addProseMirrorPlugins(){return [new Plugin({appendTransaction(transactions,oldState,state){if(!transactions.some(tr=>tr.docChanged)||transactions.some(tr=>tr.getMeta(SIZE_POLICY_META)))return null;return enforceNewReferencePolicy(state,oldState.doc,referencePolicyOptions());}})];}});

function notify(){emit('height-change');}
function updateMention(){
 const instance=editor.value;
 if(!instance||instance.view.composing||!instance.isFocused){mention.value=null;return;}
 const {selection}=instance.state;
 if(!selection.empty){mention.value=null;return;}
 const match=mentionAtPosition(selection.$from);
 if(!match){mention.value=null;return;}
 const coords=instance.view.coordsAtPos(selection.from);
 mention.value={...match,anchor:{x:coords.left,y:coords.top,left:coords.left,right:coords.right,top:coords.top,bottom:coords.bottom,width:Math.max(1,coords.right-coords.left),height:coords.bottom-coords.top}};
}
function closeMention(){mention.value=null;}
function insertReference(input,range=null){
 const values=(Array.isArray(input)?input:[input]).map(normalizeReference).filter(Boolean),instance=editor.value;
 if(!values.length||!instance||props.disabled)return;
 const target=range||mention.value||lastSelection||instance.state.selection;
 const from=Number(target.from),to=Number(target.to ?? target.from);
 instance.view.dispatch(closeHistory(instance.state.tr));
 instance.chain().focus().insertContentAt({from:Math.min(from,instance.state.doc.content.size),to:Math.min(to,instance.state.doc.content.size)},values.flatMap(value=>[{type:'reference',attrs:value},{type:'text',text:' '}])).run();
 instance.view.dispatch(closeHistory(instance.state.tr));
 closeMention();notify();
}
function onKey(view,event){
 if(event.isComposing||view.composing||event.keyCode===229)return false;
 if(mention.value&&picker.value?.key(event))return true;
 if(event.key==='Enter'){
  event.preventDefault();
  if(event.shiftKey){editor.value.commands.insertContent({type:'hardBreak'});return true;}
  emit('send');return true;
 }
 return false;
}
function onPaste(view,event){
 if(event.clipboardData?.files?.length||[...(event.clipboardData?.items||[])].some(item=>item.kind==='file')){emit('paste',event);return true;}
 const custom=event.clipboardData?.getData(REFERENCE_MIME);
 if(custom){try{const value=JSON.parse(custom);if(Array.isArray(value)){const content=value.flatMap(node=>node?.type==='text'?[{type:'text',text:String(node.text||'')}]:node?.type==='hardBreak'?[{type:'hardBreak'}]:node?.type==='reference'&&normalizeReference(node.attrs)?[{type:'reference',attrs:normalizeReference(node.attrs)}]:[]);editor.value.commands.insertContent(content);event.preventDefault();return true;}if(normalizeReference(value)){insertReference(value);event.preventDefault();return true;}}catch{}}
 const text=event.clipboardData?.getData('text/plain');
 if(text){event.preventDefault();editor.value.commands.insertContent(textToDocument(text).content[0].content);return true;}
 return false;
}
function copy(event){
 const instance=editor.value;if(!instance||instance.state.selection.empty)return;
 const slice=instance.state.selection.content();
 const json=slice.content.toJSON();
 const nodes=json?.[0]?.type==='paragraph'?json.flatMap(node=>node.content||[]):json;
 const value=documentToText({content:[{type:'paragraph',content:nodes||[]}]});
 event.clipboardData?.setData('text/plain',value);
 event.clipboardData?.setData(REFERENCE_MIME,JSON.stringify(nodes||[]));
 event.preventDefault();
 if(event.type==='cut')instance.commands.deleteSelection();
}
function dragOver(event){if([...event.dataTransfer?.types||[]].includes(REFERENCE_MIME)){event.preventDefault();event.dataTransfer.dropEffect='copy';dragHover.value=true;}}
function drop(event){
 dragHover.value=false;const raw=event.dataTransfer?.getData(REFERENCE_MIME);
 if(!raw){if(event.dataTransfer?.getData('application/x-openbear-tree')){event.preventDefault();event.stopPropagation();}return;}
 event.preventDefault();event.stopPropagation();
 try{const value=JSON.parse(raw);const pos=editor.value.view.posAtCoords({left:event.clientX,top:event.clientY})?.pos;insertReference(value,pos!==undefined?{from:pos,to:pos}:lastSelection);}catch{}
}
function externalInsert(event){insertReference(event.detail?.reference||event.detail);}
function insertText(text){const instance=editor.value;if(instance)instance.commands.insertContent(textToDocument(String(text)).content[0].content);}
function focus(){editor.value?.commands.focus('end',{scrollIntoView:false});}
function adjustHeight(){notify();}
async function preview(){
 const generation=++previewGeneration;
 if(!currentReferences.value.length){validation.value=null;checking.value=false;editor.value?.storage.reference.invalid.clear();return;}
 checking.value=true;
 const instance=editor.value,snapshot=instance?.state.doc,text=instance?documentToText(instance.getJSON()):props.modelValue;
 try{const result=await Api.referencePreview(text,props.currentConversation,policyOrder);if(generation===previewGeneration){
  if(snapshot&&instance&&!instance.state.doc.eq(snapshot)){clearTimeout(previewTimer);previewTimer=setTimeout(preview,250);return;}
  validation.value=result;acceptReferenceSizes(result.bindings||result.items,result.conversationContentLimit);instance?.storage.reference.invalid.clear();
  if(snapshot&&instance){const tr=applyPreviewBindings(instance.state,snapshot,result.bindings||[]);if(tr)instance.view.dispatch(tr);}
 }}
 catch(error){if(generation===previewGeneration){validation.value=error?.response?.data||{ok:false,error:'引用目录暂时不可用'};const detail=validation.value.referenceError;if(detail?.code==='reference_unavailable'&&detail.key)editor.value?.storage.reference.invalid.add(detail.key);}}
 finally{if(generation===previewGeneration)checking.value=false;}
}
watch(publicPreviewKey,()=>{clearTimeout(previewTimer);previewTimer=setTimeout(preview,250);});
onMounted(()=>{
 editor.value=new Editor({
  extensions:[DocumentNode,Paragraph,Text,HardBreak,Reference,Undo,SizePolicy],content:textToDocument(props.modelValue),editable:!props.disabled,
  editorProps:{attributes:{class:'reference-editor-content',role:'textbox','aria-multiline':'true','aria-label':'消息编辑框','data-placeholder':props.placeholder},handleKeyDown:onKey,handlePaste:onPaste,handleDOMEvents:{copy:(_v,event)=>{copy(event);return true;},cut:(_v,event)=>{copy(event);return true;},compositionend:()=>{nextTick(updateMention);return false;}}},
  onUpdate:({editor:instance})=>{policyOrder=referenceOrder(policyOrder,instance.state.doc);emit('update:modelValue',documentToText(instance.getJSON()));nextTick(updateMention);notify();},
  onSelectionUpdate:({editor:instance})=>{lastSelection={from:instance.state.selection.from,to:instance.state.selection.to};updateMention();},
  onFocus:()=>updateMention(),onBlur:()=>{if(!mention.value)closeMention();},
 });
 policyOrder=referenceOrder([],editor.value.state.doc);
 window.addEventListener('openbear:insert-reference',externalInsert);if(currentReferences.value.length)void preview();
});
watch(()=>props.modelValue,value=>{const instance=editor.value;if(instance&&documentToText(instance.getJSON())!==String(value||'')){instance.commands.setContent(textToDocument(value),{emitUpdate:false});policyOrder=referenceOrder([],instance.state.doc);lastSelection=null;closeMention();notify();}});
watch(()=>props.disabled,value=>editor.value?.setEditable(!value));
onBeforeUnmount(()=>{previewGeneration++;clearTimeout(previewTimer);window.removeEventListener('openbear:insert-reference',externalInsert);editor.value?.destroy();});
function getReferenceOrder(){return [...policyOrder];}
defineExpose({focus,adjustHeight,insertText,insertReference,editor,getReferenceOrder});
</script>
<template>
 <div ref="host" class="reference-editor" :class="{'is-drag-over':dragHover,'has-reference-error':validation?.ok===false}" @dragover.capture="dragOver" @dragleave="dragHover=false" @drop.capture="drop">
  <EditorContent v-if="editor" :editor="editor"/>
  <span v-if="editor?.isEmpty" class="reference-editor-placeholder" aria-hidden="true">{{placeholder}}</span>
  <div v-if="validation?.ok===false" class="reference-editor-error" role="status">{{referenceErrorText(validation.error)}}{{validation.referenceError?.label?'：'+validation.referenceError.label:''}}</div>
  <div v-if="sizeWarnings.length" class="reference-editor-size-warning" role="status">{{sizeWarnings.join('；')}}</div>
  <ReferencePicker :selected-references="currentReferences" ref="picker" :open="Boolean(mention)" :anchor="mention?.anchor" :query="mention?.query||''" :kind="mention?.kind||''" :current-conversation="currentConversation" @select="insertReference" @close="closeMention"/>
 </div>
</template>
<style>
.reference-editor{position:relative;min-width:0;width:100%;color:inherit;border-radius:0;background:transparent}
.reference-editor-content{box-sizing:border-box;min-height:3.4rem;max-height:13.5rem;overflow-y:auto;outline:none!important;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;line-height:1.65;font-family:inherit;font-size:14px;padding:0.45rem 0.75rem;border:0;background:transparent;color:#111827;scrollbar-width:thin}
.reference-editor-content p{margin:0!important;line-height:inherit;min-height:1.6em}
.reference-editor-placeholder{position:absolute;inset:0;padding:0.45rem 0.75rem;pointer-events:none;color:#a1a1aa;font-size:14px;line-height:1.65}
.reference-editor.is-drag-over{outline:1px dashed rgba(70,125,195,.65);outline-offset:5px}
.reference-inline-node{display:inline;vertical-align:baseline;white-space:normal}
.reference-editor-size-warning{color:#aa804a;font-size:10px;line-height:1.4;padding-top:3px}
.reference-editor-error{color:#ad6565;font-size:10px;line-height:1.4;padding-top:3px}
html.dark .reference-editor-placeholder{color:#71717a}
html.dark .reference-editor-content{color:#e5e7eb}
</style>
