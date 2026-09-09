<script setup>
import {onBeforeUnmount,onMounted,ref,shallowRef} from 'vue';
import FloatingPanel from './FloatingPanel.vue';
import ReferenceCapsule from './ReferenceCapsule.vue';
import {Api} from '../api.js';
import {normalizeReference,referenceErrorText,referenceToken} from './codec.js';
const props=defineProps({currentConversation:{type:String,default:''}});
const open=ref(false),reference=ref(null),anchor=shallowRef(null),details=ref(null),busy=ref(false),error=ref(''),editable=ref(false),scope=ref('full'),turns=ref(20);
let update=null,generation=0,bundleId='';
async function load(){const current=++generation;busy.value=true;error.value='';details.value=null;try{const result=await Api.referenceInspect(referenceToken(reference.value),props.currentConversation,bundleId);if(current===generation)details.value=result;}catch(e){if(current===generation)error.value=referenceErrorText(e?.response?.data?.error||'引用暂时无法读取');}finally{if(current===generation)busy.value=false;}}
function show(event){const detail=event.detail||{};const value=normalizeReference(detail.reference);if(!value)return;reference.value=value;anchor.value=detail.anchor;update=typeof detail.updateReference==='function'?detail.updateReference:null;editable.value=Boolean(update);scope.value=value.scope||'full';turns.value=value.turns||20;bundleId=String(detail.bundleId||'');open.value=true;void load();}
function close(){generation++;open.value=false;update=null;}
function applyScope(){if(!update)return;reference.value={...reference.value,scope:scope.value,...(scope.value==='recent'?{turns:Number(turns.value)||20}:{turns:''})};update(reference.value);void load();}
onMounted(()=>window.addEventListener('openbear:inspect-reference',show));onBeforeUnmount(()=>{generation++;window.removeEventListener('openbear:inspect-reference',show);});
</script>
<template><FloatingPanel :open="open" :anchor="anchor" placement="top-start" :width="420" label="引用详情" @close="close">
 <div class="reference-inspector-head"><ReferenceCapsule v-if="reference" :reference="reference"/><button type="button" aria-label="关闭引用详情" @click="close">×</button></div>
 <div v-if="editable&&reference?.kind==='chat'" class="reference-inspector-scope"><label>注入范围 <select v-model="scope" @change="applyScope"><option value="full">全文</option><option value="recent">最近若干轮</option></select></label><input v-if="scope==='recent'" v-model="turns" type="number" min="1" max="50" aria-label="引用轮数" @change="applyScope"/></div>
 <div v-if="busy" class="reference-inspector-empty">正在读取引用信息…</div>
 <div v-else-if="error" class="reference-inspector-empty is-error">{{error}}</div>
 <template v-else-if="details"><div class="reference-inspector-meta">{{details.frozen?'本条消息引用时的版本':'当前来源'}} · 约 {{Number(details.item?.estimatedTokens||0).toLocaleString()}} Tokens</div><div v-if="details.item?.sensitive" class="reference-inspector-sensitive">凭证会在发送时提供给当前模型。这里不展示或复制凭证值，草稿与胶囊只保存引用。</div><pre v-else class="reference-inspector-content">{{details.content||'内容为空'}}</pre><div v-if="details.previewTruncated" class="reference-inspector-meta">此处仅展示部分预览，不代表发送内容被截断。</div></template>
</FloatingPanel></template>
<style>
.reference-inspector-head{display:flex;align-items:center;gap:10px;padding:11px 12px;border-bottom:1px solid rgba(128,139,155,.15)}.reference-inspector-head>button{margin-left:auto;border:0;background:none;color:inherit;font-size:18px;padding:0 3px}.reference-inspector-scope{display:flex;gap:8px;align-items:center;padding:9px 12px;font-size:11px}.reference-inspector-scope select,.reference-inspector-scope input{color:inherit;background:transparent;border:1px solid rgba(128,139,155,.3);border-radius:5px;font:inherit;padding:3px 5px}.reference-inspector-scope input{width:55px}.reference-inspector-meta{font-size:10px;color:#8a929e;padding:7px 12px}.reference-inspector-content{margin:0;padding:4px 12px 12px;overflow:auto;white-space:pre-wrap;word-break:break-word;font-family:inherit;font-size:12px;line-height:1.65}.reference-inspector-empty{padding:20px 12px;text-align:center;color:#8a929e}.reference-inspector-empty.is-error{color:#bc7373}.reference-inspector-sensitive{padding:10px 12px 17px;color:#a67b3c;font-size:12px;line-height:1.7}html.dark .reference-inspector-sensitive{color:#d2b27d}html.dark .reference-inspector-scope select{background:#23262d}
</style>
