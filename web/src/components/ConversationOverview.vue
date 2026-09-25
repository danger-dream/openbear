<script setup>
import {computed,onBeforeUnmount,ref,watch} from 'vue';
import {ChatLineRound,Close} from '@element-plus/icons-vue';
import FloatingPanel from '../references/FloatingPanel.vue';
import {referenceCatalog,watchConversationOverview} from '../references/catalog.js';
import {fmtTokens,fmtNum,fmtCost} from '../views/consoleView/display.js';
import {ledgerTokenParts,totalSessionDurationMs} from '../views/consoleView/ledgerUsage.js';
import {formatOverviewDuration,vOverviewElapsed as vElapsed} from './conversationOverviewFormat.js';

const props=defineProps({open:Boolean,anchor:{default:null},row:{type:Object,default:null}});
const emit=defineEmits(['close','enter','leave']);
const data=ref(null),error=ref(''),fresh=ref(false);
// Instance-owned cache disappears with the logged-in tree. A reopen always
// requests a fresh snapshot. Refreshes update the existing slots silently,
// rather than inserting/removing status rows around the same figures.
const cache=new Map();let unsubscribe=null;
const stale=computed(()=>!fresh.value||!referenceCatalog.connected||referenceCatalog.stale);
const tokenParts=computed(()=>data.value?.usage?ledgerTokenParts(data.value.usage):null);
const duration=computed(()=>{
 const value=data.value;if(!value)return null;
 return totalSessionDurationMs({timelineTotalDurationMs:value.duration?.timelineTotalDurationMs,
  modelCalls:[{total_time_ms:value.duration?.modelCallsMs||0}],liveMs:value.duration?.liveMs||0,ledgerUsage:value.usage||{}});
});
const configuration=computed(()=>data.value?.configuration||null);
const thinkingText=computed(()=>{
 const value=configuration.value;
 if(!value||value.supportsThinking===null)return '—';
 return value.supportsThinking ? (value.thinkingLevel||'—') : '不可调节';
});
const fastText=computed(()=>{
 const value=configuration.value;
 if(!value||value.supportsFast===null)return '—';
 return value.supportsFast ? (value.fastMode?'开启':'关闭') : '不支持';
});
const currentRunning=computed(()=>!error.value && Boolean(data.value?.running ?? props.row?.running ?? (props.row?.status==='running')));
const stateLabel=computed(()=>{
 if(error.value)return error.value;
 if(!referenceCatalog.connected)return '连接已断开';
 return data.value?.status || props.row?.currentStatus || (currentRunning.value?'运行中':'—');
});
function release(){unsubscribe?.();unsubscribe=null;}
watch(()=>[props.open,props.row?.conversationUuid],([open,uuid])=>{
 release();error.value='';fresh.value=false;
 if(!open||!uuid){data.value=null;return;}
 data.value=cache.get(uuid)||null;
 unsubscribe=watchConversationOverview(uuid,packet=>{
  if(packet.error){
   error.value=packet.error==='not_found'?'会话已删除或不可访问':'统计暂时无法读取';fresh.value=false;
   if(packet.error==='not_found'){cache.delete(uuid);data.value=null;}
   return;
  }
  data.value=packet.overview;fresh.value=true;error.value='';
  cache.delete(uuid);cache.set(uuid,packet.overview);
  while(cache.size>32)cache.delete(cache.keys().next().value);
 });
},{immediate:true});
watch(()=>referenceCatalog.connected,value=>{if(!value)fresh.value=false;});
watch(()=>referenceCatalog.ready,value=>{if(!value){cache.clear();data.value=null;fresh.value=false;}});
onBeforeUnmount(release);
</script>

<template>
 <FloatingPanel :open="open" :anchor="anchor" placement="right-start" :width="360" label="会话统计概览" @close="emit('close')" @enter="emit('enter')" @leave="emit('leave')">
  <div class="conversation-overview">
   <header class="overview-heading">
    <ChatLineRound class="overview-heading-icon"/>
    <div><strong>{{data?.title||row?.title||'会话概览'}}</strong><p :title="data?.path||row?.path">{{data?.path||row?.path||'临时会话'}}<span v-if="data?.archived"> · 已归档</span></p></div>
    <button type="button" aria-label="关闭会话概览" @click="emit('close')"><Close/></button>
   </header>
   <section class="overview-configuration" aria-label="当前会话配置" title="当前会话配置；运行中修改的设置在下一轮生效">
    <div class="overview-model"><span>会话模型</span><strong data-overview-model :title="configuration?.model">{{configuration?.modelName||configuration?.model||'—'}}</strong></div>
    <div class="overview-modes"><span>思考强度 <b data-overview-thinking>{{thinkingText}}</b></span><span :class="{'fast-enabled':configuration?.fastMode}">Fast <b data-overview-fast>{{fastText}}</b></span></div>
   </section>
   <div class="overview-state" :class="{'is-running':currentRunning&&referenceCatalog.connected,'has-error':Boolean(error)}" role="status">
    <span class="overview-state-label" :title="stateLabel"><i></i><span>{{stateLabel}}</span></span>
    <span v-if="currentRunning" class="overview-elapsed">已运行 <strong v-elapsed="{startAt:data?.startedAtMs,active:!stale,fallback:'—'}">—</strong></span>
   </div>
    <section class="overview-token-section">
     <div class="overview-total"><span>累计 Tokens</span><strong data-overview-metric="tokens">{{tokenParts?fmtTokens(tokenParts.input+tokenParts.output):'—'}}</strong></div>
     <dl class="overview-token-parts">
      <div><dt>输入</dt><dd>{{tokenParts?fmtTokens(tokenParts.input):'—'}}</dd></div>
      <div><dt>输出</dt><dd>{{tokenParts?fmtTokens(tokenParts.output):'—'}}</dd></div>
      <div><dt>缓存读取</dt><dd>{{data?.usage?fmtTokens(data.usage.cache_read_tokens):'—'}}</dd></div>
      <div><dt>缓存写入</dt><dd>{{data?.usage?fmtTokens(data.usage.cache_write_tokens):'—'}}</dd></div>
     </dl>
    </section>
    <div class="overview-duration"><span>累计耗时</span><strong data-overview-metric="duration">{{formatOverviewDuration(duration)}}</strong></div>
    <dl class="overview-counts">
     <div><dt>消息条数</dt><dd data-overview-metric="messages">{{data?fmtNum(data.messageCount):'—'}}</dd></div>
     <div><dt>模型调用</dt><dd data-overview-metric="models">{{data?fmtNum(data.calls?.model):'—'}}</dd></div>
     <div><dt>工具调用</dt><dd data-overview-metric="tools">{{data?fmtNum(data.calls?.tool):'—'}}</dd></div>
    </dl>
    <div class="overview-cost"><span>总花费</span><strong data-overview-metric="cost">{{data?.usage?fmtCost(data.usage.cost_usd):'—'}}</strong></div>

  </div>
 </FloatingPanel>
</template>

<style>
.conversation-overview{--overview-muted:var(--ob-text-subtle);--overview-line:var(--ob-border);padding:13px 15px 10px;font-size:12px;line-height:1.5;font-variant-numeric:tabular-nums;overflow:auto;scrollbar-width:none;-ms-overflow-style:none}
.conversation-overview::-webkit-scrollbar{display:none;width:0;height:0}
.overview-heading{display:flex;align-items:flex-start;gap:8px}
.overview-heading-icon{flex:none;width:17px;height:17px;color:var(--ob-text-subtle);margin-top:2px}
.overview-heading>div{flex:1;min-width:0}
.overview-heading strong{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-size:14px;font-weight:600;line-height:1.5;overflow-wrap:anywhere}
.overview-heading p{margin:2px 0 0;font-size:11px;color:var(--overview-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.overview-heading button{display:grid;place-items:center;flex:none;width:20px;height:20px;padding:3px;border:0;border-radius:4px;background:none;color:var(--overview-muted);cursor:pointer}
.overview-heading button:hover{background:var(--ob-hover)}
.overview-heading button svg{width:13px;height:13px}
.overview-configuration{margin-top:10px;padding:9px 10px;border:1px solid var(--overview-line);border-radius:7px;background:var(--ob-surface-soft)}
.overview-model{display:flex;align-items:baseline;gap:12px;min-width:0}
.overview-model>span{flex:none;font-size:11px;color:var(--overview-muted)}
.overview-model strong{min-width:0;font-size:12px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.overview-modes{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:4px;font-size:11px;color:var(--overview-muted)}
.overview-modes b{margin-left:4px;font-weight:500;color:var(--ob-text)}
.overview-modes .fast-enabled b{color:var(--ob-blue)}
.overview-state{display:flex;align-items:center;justify-content:space-between;gap:3px 12px;min-height:18px;margin:10px 0;padding:0 1px;font-size:12px;color:var(--overview-muted)}
.overview-state-label{display:flex;align-items:center;gap:6px;min-width:0}
.overview-state-label>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.overview-state-label i{flex:none;width:5px;height:5px;border-radius:50%;background:currentColor}
.overview-state.is-running{color:var(--ob-blue)}
.overview-elapsed{flex:none;white-space:nowrap;font-size:11px;color:var(--overview-muted)}
.overview-elapsed strong{font-weight:500;color:inherit}
.overview-token-section{padding:10px 1px;border-top:1px solid var(--overview-line);border-bottom:1px solid var(--overview-line)}
.overview-total,.overview-duration,.overview-cost{display:flex;align-items:baseline;justify-content:space-between;gap:14px}
.overview-total>span,.overview-duration>span,.overview-cost>span{font-size:12px;color:var(--overview-muted);white-space:nowrap}
.overview-total strong,.overview-cost strong{font-size:16px;font-weight:600;line-height:1.5;letter-spacing:0}
.overview-token-parts{display:grid;grid-template-columns:1fr 1fr;gap:3px 22px;margin:7px 0 0;font-size:11px}
.overview-token-parts>div{display:flex;justify-content:space-between;align-items:baseline;gap:8px;min-width:0}
.overview-token-parts dt{color:var(--overview-muted);white-space:nowrap}
.overview-token-parts dd{margin:0;font-weight:500;text-align:right}
.overview-duration{padding:10px 1px}
.overview-duration strong{font-size:12px;font-weight:500;text-align:right}
.overview-counts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:0;padding:8px 0 10px;border-bottom:1px solid var(--overview-line)}
.overview-counts>div{min-width:0;padding-left:14px;border-left:1px solid var(--overview-line)}
.overview-counts>div:first-child{padding-left:1px;border-left:0}
.overview-counts dt{font-size:11px;color:var(--overview-muted)}
.overview-counts dd{margin:3px 0 0;font-size:16px;line-height:1.5;font-weight:500;overflow-wrap:anywhere}
.overview-cost{padding:10px 1px 0}
.overview-state.has-error{color:var(--ob-text)}

</style>
