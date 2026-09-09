<script setup>
import {computed,nextTick,ref,watch} from 'vue';
import FloatingPanel from './FloatingPanel.vue';
import {referenceCatalog,searchReferences,includeArchivedReferences} from './catalog.js';
import {REFERENCE_MIME,KIND_LABELS,normalizeReference,referenceToken} from './codec.js';
import {referenceIconHtml,referenceCandidateDetail} from './presentation.js';
import {Api} from '../api.js';
const props=defineProps({open:Boolean,anchor:{default:null},placement:{type:String,default:'top-start'},kind:{type:String,default:''},query:{type:String,default:''},searchable:Boolean,currentConversation:{type:String,default:''},allowDrag:Boolean});
const emit=defineEmits(['select','close','enter','leave']);
const localQuery=ref(''),filter=ref(''),selected=ref(0),historyConversation=ref(''),historyItems=ref([]),historyMore=ref(false),historyOffset=ref(0),historyBusy=ref(false),historyError=ref('');
let historyGeneration=0,dragging=false;
const tabs=[['','全部'],['mem','记忆'],['doc','文档'],['secret','凭证'],['chat','会话'],['history','历史']];
const queryText=computed(()=>props.searchable?localQuery.value:props.query);
const activeKind=computed(()=>props.kind||filter.value);
const historyMode=computed(()=>activeKind.value==='history');
const results=computed(()=>searchReferences(queryText.value,{kind:historyMode.value?'':activeKind.value,currentConversation:props.currentConversation,items:historyMode.value?historyItems.value:null,limit:40}));
const groups=computed(()=>{const values=[];let group=null;for(const [index,item] of results.value.entries()){const name=item.group||KIND_LABELS[item.kind];if(group?.name!==name){group={name,rows:[]};values.push(group);}group.rows.push({item,index});}return values;});
watch(()=>props.open,open=>{if(open){localQuery.value='';filter.value=props.kind||'';selected.value=0;historyItems.value=[];historyConversation.value=props.currentConversation;}else historyGeneration++;});
watch(results,()=>{selected.value=Math.min(selected.value,Math.max(0,results.value.length-1));});
watch(historyMode,active=>{if(active)void loadHistory();});
async function loadHistory(append=false){
 const id=historyConversation.value||props.currentConversation;
 if(!id||id==='local:new'){historyError.value='先选择一个已有会话，再选择其中的问答或消息';return;}
 const generation=++historyGeneration;historyBusy.value=true;historyError.value='';
 try{const data=await Api.referenceHistory(id,append?historyOffset.value:0);if(generation!==historyGeneration)return;historyItems.value=append?[...historyItems.value,...data.items]:data.items;historyMore.value=Boolean(data.hasMore);historyOffset.value=data.nextOffset||0;}
 catch{if(generation===historyGeneration)historyError.value='历史目录暂时无法读取';}
 finally{if(generation===historyGeneration)historyBusy.value=false;}
}
function choose(item=results.value[selected.value]){const value=normalizeReference(item);if(value)emit('select',value);}
function move(direction){selected.value=(selected.value+direction+Math.max(1,results.value.length))%Math.max(1,results.value.length);nextTick(()=>document.querySelector('.reference-picker-option.is-selected')?.scrollIntoView({block:'nearest'}));}
function key(event){if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();move(event.key==='ArrowDown'?1:-1);}else if(event.key==='Enter'){event.preventDefault();choose();}}
function inspectHistory(item){historyConversation.value=item.id;filter.value='history';void loadHistory();}
function dragStart(event,item){dragging=true;event.dataTransfer.effectAllowed='copy';event.dataTransfer.setData(REFERENCE_MIME,JSON.stringify(normalizeReference(item)));event.dataTransfer.setData('text/plain',referenceToken(item));}
function dragEnd(){dragging=false;emit('close');}
defineExpose({move,choose,results});
</script>
<template>
 <FloatingPanel :open="open" :anchor="anchor" :placement="placement" :width="360" label="选择引用内容" @close="emit('close')" @enter="emit('enter')" @leave="!dragging&&emit('leave')">
  <div class="reference-picker-heading"><strong>{{kind?KIND_LABELS[kind]:'引用内容'}}</strong><span :class="{'is-offline':referenceCatalog.stale}">{{referenceCatalog.stale?'目录同步中':referenceCatalog.connected?'实时目录':'正在连接'}}</span><button type="button" aria-label="关闭引用选择器" @click="emit('close')">×</button></div>
  <div v-if="searchable" class="reference-picker-search"><input v-model="localQuery" placeholder="搜索名称 / 拼音…" aria-label="搜索引用内容" @keydown="key" /></div>
  <div v-if="!kind" class="reference-picker-tabs"><button v-for="[value,label] in tabs" :key="value" type="button" :class="{'is-active':filter===value}" @mousedown.prevent @click="filter=value;selected=0">{{label}}</button></div>
  <div class="reference-picker-list" role="listbox" aria-label="引用候选">
   <template v-for="(group,groupIndex) in groups" :key="`${group.name}:${groupIndex}`"><div class="reference-picker-group">{{group.name}}</div>
    <div v-for="{item,index} in group.rows" :key="item.key" class="reference-picker-option" :class="{'is-selected':index===selected}" role="option" :aria-selected="index===selected" :draggable="allowDrag" @dragstart="dragStart($event,item)" @dragend="dragEnd" @mousedown="!allowDrag && $event.preventDefault()" @pointermove="selected=index" @click="choose(item)">
     <span class="reference-picker-icon" v-html="referenceIconHtml(item.kind)"></span><span class="reference-picker-copy"><strong>{{item.label}}</strong><small :title="referenceCandidateDetail(item)">{{referenceCandidateDetail(item)}}</small></span>
     <button v-if="item.kind==='chat'&&!kind" type="button" class="reference-picker-range" title="选择该会话中的问答或消息" @click.stop="inspectHistory(item)">片段</button>
    </div>
   </template>
   <div v-if="!results.length" class="reference-picker-empty">{{historyError||(historyBusy?'正在读取历史…':!referenceCatalog.ready?'正在同步目录…':'没有匹配内容')}}</div>
   <button v-if="historyMode&&historyMore" type="button" class="reference-picker-more" :disabled="historyBusy" @click="loadHistory(true)">{{historyBusy?'加载中…':'更多历史'}}</button>
  </div>
  <div class="reference-picker-footer"><label><input type="checkbox" :checked="referenceCatalog.includeArchived" @change="includeArchivedReferences($event.target.checked)"/>包含已归档</label><span>↑↓ 选择 · Enter 插入</span></div>
 </FloatingPanel>
</template>
<style>
.reference-picker-heading{display:flex;align-items:center;gap:8px;padding:10px 12px 8px}.reference-picker-heading strong{font-weight:600;font-size:12px}.reference-picker-heading span{margin-left:auto;font-size:10px;color:#87919d}.reference-picker-heading .is-offline{color:#aa804a}.reference-picker-heading button{border:0;background:none;color:inherit;font-size:17px;line-height:18px;padding:0 2px}
.reference-picker-search{padding:0 10px 8px}.reference-picker-search input{box-sizing:border-box;width:100%;border:1px solid rgba(128,139,155,.24);border-radius:6px;background:rgba(128,139,155,.055);padding:6px 8px;outline:none;font:inherit;color:inherit}.reference-picker-search input:focus{border-color:rgba(74,132,204,.65)}
.reference-picker-tabs{display:flex;gap:3px;padding:0 9px 8px;border-bottom:1px solid rgba(128,139,155,.13)}.reference-picker-tabs button{flex:1;padding:4px 2px;border:0;border-radius:5px;font:inherit;font-size:11px;color:inherit;opacity:.7;background:none;white-space:nowrap}.reference-picker-tabs button.is-active{background:rgba(103,139,188,.14);opacity:1;color:#3b679c}
.reference-picker-list{overflow-y:auto;overscroll-behavior:contain;min-height:40px;padding:4px 5px;scrollbar-width:thin}.reference-picker-group{padding:6px 7px 3px;font-size:10px;color:#89919c;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.reference-picker-option{display:flex;align-items:center;gap:8px;padding:7px;border-radius:6px;cursor:pointer;user-select:none;min-height:41px}.reference-picker-option.is-selected{background:rgba(102,140,186,.12)}.reference-picker-icon{flex:none;color:#7d8c9f}.reference-picker-icon svg{width:16px;height:16px}.reference-picker-copy{min-width:0;display:flex;flex:1;flex-direction:column;gap:2px}.reference-picker-copy strong{font-weight:500;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.reference-picker-copy small{font-size:10px;color:#8a939f;line-height:1.5;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.reference-picker-range{border:0;border-radius:4px;background:rgba(128,139,155,.1);padding:3px 5px;color:inherit;font-size:10px}.reference-picker-empty{padding:22px 14px;text-align:center;color:#89919c;font-size:12px}.reference-picker-more{display:block;width:100%;padding:8px;border:0;background:none;color:inherit;font-size:11px}.reference-picker-footer{display:flex;justify-content:space-between;gap:8px;padding:8px 11px;border-top:1px solid rgba(128,139,155,.13);font-size:10px;color:#8b929c}.reference-picker-footer label{display:flex;align-items:center;gap:4px}.reference-picker-footer input{width:11px;height:11px;margin:0}
html.dark .reference-picker-tabs button.is-active{color:#b5cbea;background:rgba(139,175,221,.14)}html.dark .reference-picker-option.is-selected{background:rgba(139,175,221,.13)}
@media(max-width:640px){.reference-picker-option{padding:9px 8px;min-height:46px}.reference-picker-tabs button{padding:7px 2px}.reference-picker-footer>span{display:none}}
</style>
