<script setup>
import {computed,nextTick,ref,watch} from 'vue';
import FloatingPanel from './FloatingPanel.vue';
import {referenceCatalog,searchReferences,includeArchivedReferences,acceptReferenceSizes,referencePolicyOptions} from './catalog.js';
import {applyConversationPolicy,policyReasonText,policyReasonTitle} from './sizePolicy.js';
import {REFERENCE_MIME,KIND_LABELS,normalizeReference,referenceToken} from './codec.js';
import {referenceIconHtml,referenceCandidateDetail} from './presentation.js';
import {Api} from '../api.js';
import {conversationMessages} from './conversationMessages.js';
const props=defineProps({open:Boolean,anchor:{default:null},placement:{type:String,default:'top-start'},kind:{type:String,default:''},query:{type:String,default:''},searchable:Boolean,currentConversation:{type:String,default:''},selectedReferences:{type:Array,default:()=>[]},allowDrag:Boolean});
const emit=defineEmits(['select','close','enter','leave']);
const list=ref(null),searchInput=ref(null),searchOpen=ref(false),localQuery=ref(''),filter=ref(''),selected=ref(0),mode=ref('content');
const child=ref(null),childQuery=ref(''),checked=ref(new Set()),selectionError=ref('');
const historyConversation=ref(''),historyRows=ref([]),historyItems=ref([]),historyMore=ref(false),historyOffset=ref(0),historyBusy=ref(false),historyError=ref('');
let historyGeneration=0,dragging=false,parentSelected=0,parentSearchOpen=false;
const tabs=[['','全部'],['mem','记忆'],['doc','文档'],['secret','凭证'],['chat','会话'],['history','历史']];
const queryText=computed({get:()=>child.value?childQuery.value:localQuery.value,set:value=>{if(child.value)childQuery.value=value;else localQuery.value=value;selected.value=0;}});
const activeKind=computed(()=>props.kind||filter.value);
const historyMode=computed(()=>Boolean(child.value)||activeKind.value==='history');
const results=computed(()=>child.value
 ? (childQuery.value?searchReferences(childQuery.value,{items:historyItems.value,limit:historyItems.value.length}):historyItems.value)
 : searchReferences(queryText.value,{kind:historyMode.value?'':activeKind.value,currentConversation:props.currentConversation,items:historyMode.value?historyItems.value:null,limit:40}));
const groups=computed(()=>{const values=[];let group=null;for(const [index,item] of results.value.entries()){const name=item.group||KIND_LABELS[item.kind],key=item.groupKey||name;if(group?.key!==key){group={key,name,turn:item.turnReference,rows:[]};values.push(group);}group.rows.push({item,index});}return values;});
watch(()=>props.open,open=>{
 historyGeneration++;historyBusy.value=false;
 if(open){searchOpen.value=props.searchable;localQuery.value=props.query;filter.value=props.kind||'';selected.value=0;mode.value='content';child.value=null;childQuery.value='';checked.value=new Set();selectionError.value='';historyRows.value=[];historyItems.value=[];historyMore.value=false;historyError.value='';historyConversation.value=props.currentConversation;}
},{immediate:true});
watch(()=>props.query,value=>{localQuery.value=value;if(child.value)back();selected.value=0;});
watch(results,()=>{selected.value=Math.min(selected.value,Math.max(0,results.value.length-1));});
async function loadHistory(append=false){
 const id=child.value?.id||historyConversation.value||props.currentConversation;
 if(!id||id==='local:new'){historyError.value='先选择一个已有会话，再选择其中的问答或消息';return;}
 const generation=++historyGeneration;historyBusy.value=true;historyError.value='';
 try{const data=await Api.referenceHistory(id,append?historyOffset.value:0);if(generation!==historyGeneration)return;acceptReferenceSizes(data.items,data.conversationContentLimit);historyRows.value=append?[...historyRows.value,...data.items]:data.items;historyItems.value=child.value?conversationMessages(historyRows.value):historyRows.value;historyMore.value=Boolean(data.hasMore);historyOffset.value=data.nextOffset||0;}
 catch{if(generation===historyGeneration)historyError.value='历史目录暂时无法读取';}
 finally{if(generation===historyGeneration)historyBusy.value=false;}
}
function withMode(item){return normalizeReference({...item,...(item.roleLabel?{label:`${item.roleLabel} · ${item.label}`} : {}),mode:mode.value});}
function choose(item=results.value[selected.value]){
 const items=child.value&&checked.value.size?historyItems.value.filter(row=>checked.value.has(row.key)):[item];
 const values=applyPolicy(items.filter(Boolean));
 if(values.length)emit('select',values.length===1?values[0]:values);
}
// An explicit group action selects that turn alone, never the checked messages.
function applyPolicy(items){return applyConversationPolicy(items.map(withMode),props.selectedReferences||[],referencePolicyOptions());}
function candidateState(item){const items=child.value&&item.kind==='message'?historyItems.value.filter(row=>checked.value.has(row.key)||row.key===item.key):[item];return applyPolicy(items)[items.findIndex(row=>row.key===item.key)]||applyPolicy([item])[0];}
function candidateWarning(item){return mode.value==='content'?policyReasonText(candidateState(item)?.modeReason):'';}
function chooseTurn(item){if(item?.kind!=='turn')return;const value=applyPolicy([item])[0];if(value)emit('select',value);}
function scrollSelected(){nextTick(()=>list.value?.querySelector('.reference-picker-option.is-selected')?.scrollIntoView({block:'nearest'}));}
function move(direction){
 const count=results.value.length;
 if(direction>0&&child.value&&selected.value===Math.max(0,count-1)&&historyMore.value){
  if(historyBusy.value)return;
  const current=child.value.id;
  void loadHistory(true).then(()=>{if(child.value?.id===current&&selected.value===Math.max(0,count-1)&&results.value.length>count){selected.value=count;scrollSelected();}});
  return;
 }
 selected.value=(selected.value+direction+Math.max(1,count))%Math.max(1,count);scrollSelected();
}
function clickItem(item,index){selected.value=index;if(child.value){toggle(item);list.value?.focus({preventScroll:true});}else choose(item);}
function toggle(item=results.value[selected.value]){
 if(!child.value||!item)return;
 const next=new Set(checked.value);
 if(next.has(item.key))next.delete(item.key);
 else if(next.size>=30){selectionError.value='一次最多选择 30 条消息';return;}
 else next.add(item.key);
 checked.value=next;selectionError.value='';
}
function back(){
 if(!child.value){emit('close');return;}
 const restoreFocus=searchOpen.value||parentSearchOpen;
 historyGeneration++;historyBusy.value=false;child.value=null;childQuery.value='';checked.value=new Set();historyRows.value=[];historyItems.value=[];historyMore.value=false;historyError.value='';selectionError.value='';
 selected.value=parentSelected;searchOpen.value=parentSearchOpen;
 if(restoreFocus)nextTick(()=>(parentSearchOpen?searchInput.value:list.value)?.focus({preventScroll:true}));
}
async function toggleSearch(){
 searchOpen.value=!searchOpen.value;
 await nextTick();
 if(searchOpen.value)searchInput.value?.focus({preventScroll:true});
 else list.value?.focus({preventScroll:true});
}
function key(event){
 if(!props.open)return false;
 if(event.defaultPrevented||event.isComposing||event.keyCode===229)return false;
 const input=event.target?.tagName==='INPUT';
 const nativeControl=['BUTTON','SELECT','TEXTAREA','A'].includes(event.target?.tagName)||event.target?.closest?.('button,select,textarea,a[href]')||(input&&!['','text','search'].includes(event.target.type||''));
 let handled=true;
 if(event.ctrlKey&&event.altKey&&!event.metaKey&&!event.shiftKey&&(event.code==='Digit1'||event.key==='1'))mode.value=mode.value==='content'?'mention':'content';
 else if(event.key==='Escape')back();
 else if(nativeControl||event.target?.closest?.('[data-reference-mode-switch]')||event.ctrlKey||event.altKey||event.metaKey)return false;
 else if(event.key==='ArrowDown'||event.key==='ArrowUp'){move(event.key==='ArrowDown'?1:-1);if(child.value&&input)list.value?.focus({preventScroll:true});}
 else if(event.key==='ArrowRight'&&!child.value&&results.value[selected.value]?.kind==='chat')inspectHistory(results.value[selected.value]);
 else if(event.key==='ArrowLeft'&&child.value&&!input)back();
 else if(event.key===' '&&child.value&&!input)toggle();
 else if(event.key==='Tab'){
  // Only forward Tab in the editor/list accepts a candidate. Inputs and
  // native controls retain focus navigation, including all Shift+Tab paths.
  if(event.shiftKey||input||(!results.value.length&&!checked.value.size))return false;
  choose();
 }
 else if(event.key==='Enter')choose();
 else handled=false;
 if(handled){event.preventDefault();event.stopPropagation();}
 return handled;
}
function inspectHistory(item){
 parentSelected=selected.value;parentSearchOpen=searchOpen.value;searchOpen.value=false;child.value=item;childQuery.value='';checked.value=new Set();selectionError.value='';selected.value=0;historyRows.value=[];historyItems.value=[];historyMore.value=false;
 void loadHistory();
 if(parentSearchOpen)nextTick(()=>list.value?.focus({preventScroll:true}));
}
function changeTab(value){
 historyGeneration++;historyBusy.value=false;child.value=null;checked.value=new Set();filter.value=value;selected.value=0;historyRows.value=[];historyItems.value=[];historyMore.value=false;historyError.value='';
 if(value==='history')void loadHistory();
}
function dragStart(event,item){dragging=true;const value=applyPolicy([item])[0];event.dataTransfer.effectAllowed='copy';event.dataTransfer.setData(REFERENCE_MIME,JSON.stringify(value));event.dataTransfer.setData('text/plain',referenceToken(value));}
function dragEnd(){dragging=false;emit('close');}
defineExpose({move,choose,key,results});
</script>
<template>
 <FloatingPanel :open="open" :anchor="anchor" :placement="placement" :width="360" :height="430" label="选择引用内容" @keydown="key" @close="emit('close')" @enter="emit('enter')" @leave="!dragging&&emit('leave')">
  <div class="reference-picker-heading">
   <button v-if="child" type="button" class="reference-picker-icon-button" aria-label="返回会话候选" :title="'返回 · '+child.label+'（Esc）'" @mousedown.prevent @click="back"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m10 3-5 5 5 5"/></svg></button>
   <input v-if="searchOpen" ref="searchInput" v-model="queryText" class="reference-picker-search-input" :placeholder="child?'搜索输入 / 输出…':'名称 / 拼音 / *…'" aria-label="搜索引用内容" title="支持中文、拼音、首字母、* 通配符及空格" />
   <strong v-else class="reference-picker-title" :title="child?.label||(kind?KIND_LABELS[kind]:'引用内容')">{{child?.label||(kind?KIND_LABELS[kind]:'引用内容')}}</strong>
   <span v-if="!referenceCatalog.connected||referenceCatalog.stale" class="reference-picker-sync" role="status" :aria-label="referenceCatalog.stale?'目录同步中':'正在连接'" :title="referenceCatalog.stale?'目录同步中':'正在连接'"></span>
   <div class="reference-picker-mode-switch" :class="{'is-mention':mode==='mention'}" data-reference-mode-switch role="group" aria-label="引用模式" title="Ctrl + Alt + 1 切换引用模式"><button type="button" :aria-pressed="mode==='content'" @mousedown.prevent @click="mode='content'">引用内容</button><button type="button" :aria-pressed="mode==='mention'" @mousedown.prevent @click="mode='mention'">仅提及</button></div>
   <button type="button" class="reference-picker-icon-button" :class="{'is-active':searchOpen||Boolean(child?childQuery:localQuery!==query)}" :aria-label="searchOpen?'收起搜索':'搜索引用内容'" :aria-expanded="searchOpen" :title="searchOpen?'收起搜索（保留搜索词）':'搜索（支持空格和 * 通配符）'" @mousedown.prevent @click="toggleSearch"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="6.8" cy="6.8" r="4.3"/><path d="m10 10 3.7 3.7"/></svg></button>
   <button type="button" class="reference-picker-icon-button" aria-label="关闭引用选择器" title="关闭（Esc）" @mousedown.prevent @click="emit('close')"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 4 8 8m0-8-8 8"/></svg></button>
  </div>
  <div v-if="!kind&&!child" class="reference-picker-tabs"><button v-for="[value,label] in tabs" :key="value" type="button" :class="{'is-active':filter===value}" @mousedown.prevent @click="changeTab(value)">{{label}}</button></div>
  <div ref="list" class="reference-picker-list" role="listbox" aria-label="引用候选" :aria-multiselectable="child?'true':undefined" tabindex="-1">
   <template v-for="(group,groupIndex) in groups" :key="`${group.name}:${groupIndex}`"><div class="reference-picker-group" :class="{'has-turn-action':child&&group.turn}"><span>{{group.name}}</span><button v-if="child&&group.turn" type="button" class="reference-picker-range" :aria-label="(candidateState(group.turn)?.mode==='mention'?'提及整轮：':'引用整轮：')+group.name" :title="policyReasonTitle(candidateState(group.turn))||'选择本轮全部输入与输出；不包含其他轮次'" @mousedown.prevent @click.stop="chooseTurn(group.turn)">{{candidateState(group.turn)?.mode==='mention'?'提及整轮':'引用整轮'}}</button></div>
    <div v-for="{item,index} in group.rows" :key="item.key" class="reference-picker-option" :class="{'is-selected':index===selected,'is-checked':child&&checked.has(item.key)}" role="option" :aria-selected="child?checked.has(item.key):index===selected" :draggable="allowDrag&&!child" @dragstart="dragStart($event,item)" @dragend="dragEnd" @mousedown="(!allowDrag||child) && $event.preventDefault()" @pointermove="selected=index" @click="clickItem(item,index)">
     <span v-if="child" class="reference-picker-check" aria-hidden="true">{{checked.has(item.key)?'✓':''}}</span><span v-else class="reference-picker-icon" v-html="referenceIconHtml(item.kind)"></span><span class="reference-picker-copy" :class="{'is-message':child}"><small v-if="child" class="reference-picker-role" :class="{'is-output':item.role==='assistant'}">{{item.roleLabel}}</small><strong :title="item.label">{{item.label}}</strong><small v-if="!child||candidateWarning(item)" :class="{'reference-picker-size-warning':candidateWarning(item)}" :title="candidateWarning(item)?policyReasonTitle(candidateState(item)):referenceCandidateDetail(item)"><template v-if="candidateWarning(item)"><span class="reference-picker-warning-icon" aria-hidden="true">⚠</span><span class="reference-picker-warning-text">{{candidateWarning(item)}}</span></template><template v-else>{{referenceCandidateDetail(item)}}</template></small></span>
     <button v-if="item.kind==='chat'" type="button" class="reference-picker-range" title="选择该会话中的输入或输出（→）" @mousedown.prevent @click.stop="inspectHistory(item)">消息 ›</button>
    </div>
   </template>
   <div v-if="!results.length" class="reference-picker-empty">{{historyError||(historyBusy?'正在读取历史…':historyMode?'没有匹配消息':!referenceCatalog.ready?'正在同步目录…':'没有匹配内容')}}</div>
   <button v-if="historyMode&&historyMore" type="button" class="reference-picker-more" :disabled="historyBusy" @click="loadHistory(true)">{{historyBusy?'加载中…':'更多历史'}}</button>
   <button v-else-if="historyError" type="button" class="reference-picker-more" @click="loadHistory()">重试</button>
  </div>
  <div class="reference-picker-footer">
   <template v-if="child"><span class="reference-picker-selection-count" role="status" :title="selectionError||'↑↓ 移动，空格多选；未勾选时插入高亮消息；Esc 返回'">{{selectionError||(checked.size?'已选 '+checked.size+' 条':'↑↓ 移动 · 空格多选')}}</span><button type="button" class="reference-picker-insert" :disabled="!checked.size&&!results.length" title="Enter / Tab 插入；未勾选时插入当前高亮消息" @mousedown.prevent @click="choose()">{{mode==='mention'?'提及':'引用'}}<kbd>↵ / Tab</kbd></button></template>
   <template v-else><label><input type="checkbox" :checked="referenceCatalog.includeArchived" @change="includeArchivedReferences($event.target.checked)"/>包含已归档</label><span class="reference-picker-key-hint" title="↑↓ 选择，→ 展开会话消息，Enter / Tab 插入，Esc 关闭">↑↓ · → 展开 · ↵ / Tab</span></template>
  </div>
 </FloatingPanel>
</template>
<style>
.reference-picker-heading{display:flex;align-items:center;gap:5px;min-height:34px;padding:4px 8px;box-sizing:border-box;border-bottom:1px solid rgba(128,139,155,.13)}
.reference-picker-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;font-weight:600}
.reference-picker-icon-button{display:flex;flex:none;align-items:center;justify-content:center;width:23px;height:24px;padding:4px;border:0;border-radius:5px;background:none;color:#87919d;cursor:pointer}
.reference-picker-icon-button svg{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.reference-picker-icon-button:hover,.reference-picker-icon-button.is-active{background:rgba(103,139,188,.12);color:#3b679c}
.reference-picker-mode-switch{position:relative;display:grid;grid-template-columns:1fr 1fr;flex:none;box-sizing:border-box;width:116px;height:24px;padding:2px;border-radius:6px;background:rgba(128,139,155,.12)}
.reference-picker-mode-switch::before{content:'';position:absolute;left:2px;top:2px;bottom:2px;width:calc((100% - 4px)/2);border-radius:4px;background:#fff;box-shadow:0 1px 3px rgba(30,40,55,.12);transition:transform .12s ease}
.reference-picker-mode-switch.is-mention::before{transform:translateX(100%)}
.reference-picker-mode-switch button{position:relative;min-width:0;padding:0;border:0;border-radius:4px;background:transparent;font:inherit;font-size:11px;line-height:20px;color:#87919d;white-space:nowrap;cursor:pointer}
.reference-picker-mode-switch button[aria-pressed="true"]{color:#3b679c}
.reference-picker-mode-switch button:focus-visible{outline:1px solid rgba(74,132,204,.7);outline-offset:-1px}
.reference-picker-search-input{box-sizing:border-box;flex:1;min-width:0;width:0;height:24px;padding:3px 5px;border:1px solid rgba(128,139,155,.24);border-radius:5px;background:rgba(128,139,155,.055);outline:none;font:inherit;font-size:12px;color:inherit}
.reference-picker-search-input:focus{border-color:rgba(74,132,204,.65)}
.reference-picker-sync{flex:none;width:5px;height:5px;border-radius:50%;background:#aa804a}
.reference-picker-tabs{display:flex;gap:3px;padding:4px 9px;border-bottom:1px solid rgba(128,139,155,.13)}
.reference-picker-tabs button{flex:1;padding:4px 2px;border:0;border-radius:5px;font:inherit;font-size:11px;color:inherit;opacity:.7;background:none;white-space:nowrap}
.reference-picker-tabs button.is-active{background:rgba(103,139,188,.14);opacity:1;color:#3b679c}
.reference-picker-list{flex:1 1 0;overflow-y:auto;overscroll-behavior:contain;min-height:0;padding:4px 5px;scrollbar-width:thin;outline:none}
.reference-picker-list:focus-visible{box-shadow:inset 0 0 0 1px rgba(74,132,204,.5)}
.reference-picker-group{padding:6px 7px 3px;font-size:10px;color:#89919c;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.reference-picker-group.has-turn-action{display:flex;align-items:center;gap:8px}.reference-picker-group.has-turn-action>span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}.reference-picker-group .reference-picker-range{flex:none;cursor:pointer}
.reference-picker-option{display:flex;align-items:center;gap:8px;padding:7px;border-radius:6px;cursor:pointer;user-select:none;min-height:41px}
.reference-picker-option.is-selected{background:rgba(102,140,186,.12)}
.reference-picker-icon{flex:none;color:#7d8c9f}.reference-picker-icon svg{width:16px;height:16px}
.reference-picker-check{display:flex;flex:none;align-items:center;justify-content:center;width:13px;height:13px;border:1px solid rgba(128,139,155,.45);border-radius:3px;font-size:10px}
.is-checked .reference-picker-check{background:#527dad;border-color:#527dad;color:white}
.reference-picker-copy{min-width:0;display:flex;flex:1;flex-direction:column;gap:2px}
.reference-picker-copy strong{font-weight:500;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.reference-picker-copy small{font-size:10px;color:#8a939f;line-height:1.5;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.reference-picker-copy small.reference-picker-size-warning{display:flex;gap:4px;align-items:center;min-width:0;white-space:nowrap;grid-column:1/-1;color:#aa804a}.reference-picker-warning-icon{flex:none}.reference-picker-warning-text{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.reference-picker-copy.is-message{display:grid;grid-template-columns:auto minmax(0,1fr);column-gap:7px;align-items:start}
.reference-picker-copy.is-message strong{white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;line-height:1.5}
.reference-picker-copy small.reference-picker-role{font-size:10px;line-height:18px;color:#8a939f}
.reference-picker-copy small.reference-picker-role.is-output{color:#527dad}
.reference-picker-range{border:0;border-radius:4px;background:rgba(128,139,155,.1);padding:3px 5px;color:inherit;font-size:10px}
.reference-picker-empty{padding:22px 14px;text-align:center;color:#89919c;font-size:12px}
.reference-picker-more{display:block;width:100%;padding:8px;border:0;background:none;color:inherit;font-size:11px}
.reference-picker-footer{display:flex;align-items:center;justify-content:space-between;gap:8px;min-height:30px;padding:3px 10px;box-sizing:border-box;border-top:1px solid rgba(128,139,155,.13);font-size:10px;color:#8b929c}
.reference-picker-footer label{display:flex;align-items:center;gap:4px;white-space:nowrap}.reference-picker-footer input{width:11px;height:11px;margin:0}
.reference-picker-key-hint{white-space:nowrap}.reference-picker-selection-count{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.reference-picker-insert{display:flex;flex:none;align-items:center;gap:8px;height:24px;padding:2px 7px;border:1px solid rgba(128,139,155,.2);border-radius:5px;background:rgba(103,139,188,.1);font:inherit;font-size:11px;color:#3b679c;cursor:pointer}
.reference-picker-insert kbd{font:inherit;font-size:10px;opacity:.65}.reference-picker-insert:disabled{opacity:.45;cursor:default}
.reference-picker-heading,.reference-picker-tabs,.reference-picker-footer{flex-shrink:0}
html.dark .reference-picker-mode-switch{background:#30343d}html.dark .reference-picker-mode-switch::before{background:#4a515d}html.dark .reference-picker-mode-switch button[aria-pressed="true"]{color:#e4ebf5}
@media(prefers-reduced-motion:reduce){.reference-picker-mode-switch::before{transition:none}}
html.dark .reference-picker-tabs button.is-active,html.dark .reference-picker-icon-button:hover,html.dark .reference-picker-icon-button.is-active{color:#b5cbea;background:rgba(139,175,221,.14)}
html.dark .reference-picker-copy small.reference-picker-role.is-output,html.dark .reference-picker-insert{color:#b5cbea}
html.dark .reference-picker-option.is-selected{background:rgba(139,175,221,.13)}
@media(max-width:640px){.reference-picker-option{padding:9px 8px;min-height:46px}.reference-picker-tabs button{padding:7px 2px}.reference-picker-key-hint{font-size:9px}}
</style>
