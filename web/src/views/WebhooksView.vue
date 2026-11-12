<script setup>
import {computed, onBeforeUnmount, ref, watch} from 'vue';
import {Api} from '../api.js';
import {ElMessageBox} from 'element-plus';
import WebhookStatistics from '../components/webhooks/WebhookStatistics.vue';
import WebhookEventDialog from '../components/webhooks/WebhookEventDialog.vue';
import {statusLabel, ENDPOINT_STATES} from '../components/webhooks/webhookConfig.js';
import '../components/webhooks/webhooks.css';
const emit = defineEmits(['open-webhook-properties','mobile-header-ready']);
const search = ref(''), state = ref(''), scopeType = ref(''), scopeId = ref(''), endpointId = ref('');
const items = ref([]), waits = ref([]), loading = ref(false), error = ref(''), summary = ref(null), cursor = ref(null), previous = ref([]), currentCursor = ref(null);
const waitsCursor = ref(null), waitsPrevious = ref([]), waitsCurrent = ref(null), waitsLoading = ref(false), waitsError = ref('');
const eventOpen = ref(false), eventId = ref('');
const targets = ref([]), targetsError = ref('');
const scopeTargets = computed(() => targets.value.filter(item => !scopeType.value || item.type === scopeType.value));
let targetsGeneration = 0;
async function loadTargets() {
  const current = ++targetsGeneration; targetsError.value = '';
  try { const result = await Api.webhookTargets({}); if (current === targetsGeneration) targets.value = result.items || []; }
  catch (exception) { if (current === targetsGeneration) targetsError.value = exception?.response?.data?.message || exception.message; }
}
function selectScope(id) { const item = targets.value.find(value => value.id === id); scopeType.value = item?.type || scopeType.value; scopeId.value = id || ''; }
const waitOpen = ref(false), selectedWait = ref(null), waitBusy = ref(false), waitError = ref('');
let cancelIntent = null;
function inspectWait(wait) { selectedWait.value=wait;waitError.value='';waitOpen.value=true; }
async function cancelSelectedWait() {
  const wait=selectedWait.value;if(waitBusy.value || !wait?.canCancel)return;
  try {await ElMessageBox.confirm('取消后不会被迟到事件唤醒；已认领事件保留原归属，不偷偷回流普通队列。','取消外部等待',{type:'warning',confirmButtonText:'取消此等待',cancelButtonText:'保留等待',closeOnClickModal:false});}catch{return;}
  waitBusy.value=true;waitError.value='';
  if(cancelIntent?.waitId!==wait.waitId || cancelIntent?.version!==wait.version)cancelIntent={waitId:wait.waitId,version:wait.version,requestId:crypto.randomUUID()};
  try {const value=await Api.controlWebhookWait(wait.waitId,{action:'cancel',expectedVersion:wait.version,requestId:cancelIntent.requestId,reason:'用户在总览取消'});selectedWait.value=value.wait;await Promise.all([load(currentCursor.value),loadWaits(waitsCurrent.value)]);}
  catch(exception){waitError.value=exception?.response?.data?.message || exception.message;}
  finally{waitBusy.value=false;}
}
let generation = 0, abort;
async function load(next = null) {
  const run = ++generation; abort?.abort(); abort = new AbortController(); loading.value = true; error.value = '';
  try {
    const params = {search:search.value, effectiveStatus:state.value, scopeType:scopeType.value, scopeId:scopeId.value, limit:30,cursor:next};
    const [entries,stats] = await Promise.all([Api.webhooks(params,{signal:abort.signal}),Api.webhookStatistics({...params,view:'overview'},{signal:abort.signal})]);
    if (run !== generation) return;
    items.value = entries.items || []; summary.value = stats.overview; cursor.value = entries.nextCursor; currentCursor.value = next;
  } catch (exception) { if (run === generation && !abort.signal.aborted) error.value = exception?.response?.data?.message || exception.message; }
  finally { if (run === generation) loading.value = false; }
}
let waitsGeneration = 0, waitsAbort;
async function loadWaits(next = null) {
  const run = ++waitsGeneration; waitsAbort?.abort(); waitsAbort = new AbortController(); waitsLoading.value = true; waitsError.value = '';
  try {
    const data = await Api.webhookWaits({search:search.value,effectiveStatus:state.value,scopeType:scopeType.value,scopeId:scopeId.value,active:true,limit:30,cursor:next},{signal:waitsAbort.signal});
    if (run !== waitsGeneration) return;
    waits.value = data.items || []; waitsCursor.value = data.nextCursor; waitsCurrent.value = next;
  } catch (exception) { if (run === waitsGeneration && !waitsAbort.signal.aborted) waitsError.value = exception?.response?.data?.message || exception.message; }
  finally { if (run === waitsGeneration) waitsLoading.value = false; }
}
function nextWaitsPage() { waitsPrevious.value.push(waitsCurrent.value); loadWaits(waitsCursor.value); }
function previousWaitsPage() { loadWaits(waitsPrevious.value.pop() ?? null); }
function filter() { previous.value = []; waitsPrevious.value = []; load(); loadWaits(); }
function nextPage() { previous.value.push(currentCursor.value); load(cursor.value); }
function previousPage() { load(previous.value.pop() ?? null); }
function inspect(id) { eventId.value = id; eventOpen.value = true; }
watch(scopeType, value => { if (scopeId.value && !targets.value.some(item => item.id === scopeId.value && (!value || item.type === value))) scopeId.value = ''; });
watch([scopeType,scopeId], () => { endpointId.value = ''; });
watch([state,scopeType,scopeId],filter);
load(); loadWaits(); loadTargets();
onBeforeUnmount(() => {generation++;targetsGeneration++;waitsGeneration++;abort?.abort();waitsAbort?.abort();});
const fmt = value => value == null ? '—' : value.toLocaleString();
</script>
<template>
  <div class="wh-overview">
    <header class="statistics-header"><div class="statistics-title"><h1>触发器总览</h1><p>查看入口、积压与当前等待；配置仍在目录／会话属性中</p></div><div class="statistics-filters"><el-input v-model="search" clearable placeholder="搜索入口或所属对象" aria-label="搜索触发器" @keyup.enter="filter" @clear="filter" /><el-select v-model="state" clearable placeholder="全部有效状态" aria-label="有效状态筛选"><el-option v-for="value in ENDPOINT_STATES" :key="value" :value="value" :label="statusLabel(value)" /></el-select><el-button :loading="loading" @click="filter">刷新</el-button></div></header>
    <div class="statistics-scroll">
      <div class="wh-editor-toolbar"><el-select :model-value="scopeId" clearable filterable placeholder="全部目录与会话" aria-label="所属目录或会话筛选" @update:model-value="selectScope"><el-option v-for="item in scopeTargets" :key="`${item.type}:${item.id}`" :value="item.id" :label="`${item.type === 'folder' ? '目录' : '会话'} · ${item.path || item.title || item.name}`" /></el-select><span v-if="scopeId" class="wh-muted">按入口所属对象筛选，不按处理目标混算。</span></div><p v-if="targetsError" class="wh-error-text" role="alert">{{ targetsError }} <el-button link @click="loadTargets">重载筛选对象</el-button></p>
      <div v-if="error" class="wh-alert" role="alert">{{ error }}<el-button @click="load()">重新加载</el-button></div>
      <el-skeleton v-if="loading && !summary" :rows="7" animated />
      <template v-else>
        <section class="kpi-grid" aria-label="外部触发关键指标"><article v-for="[label,value,note] in [['有效入口',summary?.activeEndpoints,'按当前控制状态'],['待处理事件',summary?.pendingEvents,'持久积压，不静默丢弃'],['需要关注',summary?.needsReview,'失败、未知与协议不完整']]" :key="label" class="kpi-card"><div class="kpi-top"><span>{{ label }}</span><span class="kpi-icon" aria-hidden="true">◇</span></div><div class="kpi-value">{{ fmt(value) }}</div><div class="kpi-foot">{{ note }}</div></article></section>
        <section class="panel"><div class="panel-head"><div class="panel-title"><h2>入口列表</h2><p>所属对象与处理目标分开显示；查看属性直接编辑同一份配置</p></div><el-select v-model="scopeType" clearable aria-label="所属对象类型" placeholder="目录与会话"><el-option value="folder" label="目录入口" /><el-option value="conversation" label="会话入口" /></el-select></div><div class="table-scroll"><table><thead><tr><th>入口／所属对象</th><th>处理位置</th><th>实际状态</th><th>规则</th><th>积压</th><th>管理</th></tr></thead><tbody><tr v-for="item in items" :key="item.id"><td><strong>{{ item.name }}</strong><small>{{ item.scope.path || `${item.scope.type} · ${item.scope.id}` }}</small></td><td>{{ item.target.mode === 'newConversation' ? '新建会话' : item.target.path || item.target.conversationId }}</td><td><span class="status-pill">{{ statusLabel(item.effectiveStatus) }}</span><small>{{ item.pauseReasons?.map(statusLabel).join('；') }}</small></td><td>v{{ item.revision }}</td><td>{{ fmt(item.pendingEvents) }}</td><td><el-button link @click="emit('open-webhook-properties',item)">查看属性</el-button><el-button link @click="endpointId = item.id">统计</el-button></td></tr></tbody></table></div><p v-if="!items.length" class="wh-empty">暂无符合条件的入口。请从目录或会话属性创建；不会自动启用子节点。</p><div class="wh-pagination"><el-button :disabled="!previous.length" @click="previousPage">上一页</el-button><el-button :disabled="!cursor" @click="nextPage">下一页</el-button></div></section>
        <section class="panel"><div class="panel-head"><div class="panel-title"><h2>当前等待与阻塞</h2><p>认领与交付不同，等待不会占模型执行槽，也不会开放第二个普通主控</p></div></div><div v-for="wait in waits" :key="wait.waitId" class="waiting-row"><div><strong>{{ wait.waitId }}</strong><small>{{ wait.endpointId }} · {{ wait.deadline ? `截止 ${new Date(wait.deadline).toLocaleString()}` : '无超时' }}</small></div><span class="status-pill">{{ statusLabel(wait.state) }}</span><span>认领 {{ Array.isArray(wait.claimed) ? wait.claimed.length : wait.claimed ?? '—' }} / 交付 {{ Array.isArray(wait.delivered) ? wait.delivered.length : wait.delivered ?? '—' }}</span><el-button link @click="inspectWait(wait)">详情</el-button></div><p v-if="waitsError" class="wh-error-text" role="alert">{{ waitsError }}<el-button @click="loadWaits(waitsCurrent)">重读等待</el-button></p><p v-if="!waits.length && !waitsLoading" class="wh-empty">没有符合筛选条件的活动等待。</p><div class="wh-pagination" aria-label="当前等待分页"><el-button :disabled="!waitsPrevious.length || waitsLoading" @click="previousWaitsPage">上一页等待</el-button><el-button :disabled="!waitsCursor || waitsLoading" @click="nextWaitsPage">下一页等待</el-button></div></section>
        <section class="panel"><div class="panel-head"><div class="panel-title"><h2>事件、阶段与业务统计</h2><p>{{ endpointId ? `入口 ${endpointId}` : '全部入口' }} · 原始事实与业务声明分别查看</p></div><el-button v-if="endpointId" link @click="endpointId = ''">全部入口</el-button></div><WebhookStatistics :endpoint-id="endpointId || undefined" :scope-type="scopeType || undefined" :scope-id="scopeId || undefined" :global="!endpointId" /></section>
      </template>
    </div><WebhookEventDialog v-model="eventOpen" :event-id="eventId" />
    <el-dialog v-model="waitOpen" title="外部等待详情" width="min(760px, calc(100vw - 24px))" append-to-body class="mobile-viewport-dialog wh-event-dialog" :close-on-click-modal="!waitBusy" :close-on-press-escape="!waitBusy" :show-close="!waitBusy"><div v-if="selectedWait" class="wh-event-content"><h3>{{ selectedWait.waitId }} · {{ statusLabel(selectedWait.state) }}</h3><p>入口 {{ selectedWait.endpointId }} · 原轮 {{ selectedWait.assignmentId }}</p><p>{{ selectedWait.deadline ? `截止 ${new Date(selectedWait.deadline).toLocaleString()}` : '无超时' }}。认领和交付不等于业务已完成，等待不会开放第二个普通主控。</p><h3>匹配条件</h3><pre>{{ JSON.stringify(selectedWait.match,null,2) }}</pre><h3>已认领事件</h3><p v-if="!selectedWait.claimed?.length">尚未认领任何事件。</p><div class="wh-actions"><el-button v-for="id in selectedWait.claimed || []" :key="id" link @click="inspect(id)">{{ id }}</el-button></div><h3>已交付事件</h3><pre>{{ JSON.stringify(selectedWait.delivered || [],null,2) }}</pre><p v-if="selectedWait.pauseReasons?.length" class="wh-warning">{{ selectedWait.pauseReasons.map(statusLabel).join('；') }}</p><p v-if="waitError" class="wh-error-text" role="alert">{{ waitError }}</p></div><template #footer><el-button v-if="selectedWait?.canCancel" type="danger" plain :loading="waitBusy" @click="cancelSelectedWait">取消此等待…</el-button><el-button :disabled="waitBusy" @click="waitOpen=false">关闭</el-button></template></el-dialog>
  </div>
</template>
<style scoped>
.wh-overview{min-width:0;min-height:0;flex:1;display:flex;flex-direction:column;overflow:hidden;background:var(--ob-bg);color:var(--ob-text)}
.statistics-header{z-index:5;display:flex;min-height:72px;flex:none;align-items:center;justify-content:space-between;gap:20px;padding:12px 24px;border-bottom:1px solid var(--ob-border);background:color-mix(in srgb,var(--ob-surface) 76%,transparent);backdrop-filter:blur(18px)}
.statistics-title h1{margin:0;color:var(--ob-text-strong);font-size:18px;line-height:1.25;letter-spacing:-.02em}.statistics-title p{margin:4px 0 0;color:var(--ob-text-subtle);font-size:12.5px}
.statistics-filters{display:flex;align-items:center;justify-content:flex-end;flex-wrap:wrap;gap:8px}.statistics-filters .el-input{width:190px}.statistics-filters .el-select{width:150px}
.statistics-scroll{min-height:0;flex:1;overflow:auto;padding:18px 24px 30px}.kpi-grid{display:grid;grid-template-columns:repeat(3,minmax(142px,1fr));gap:10px;margin-bottom:10px}.kpi-card{position:relative;min-height:116px;overflow:hidden;padding:14px 14px 11px;border:1px solid var(--ob-border);border-radius:13px;background:var(--ob-surface);box-shadow:var(--ob-shadow-panel)}.kpi-top{display:flex;align-items:center;justify-content:space-between;gap:6px;color:var(--ob-text-subtle);font-size:12.5px}.kpi-icon{display:grid;width:24px;height:24px;place-items:center;border-radius:7px;background:var(--ob-blue-soft);color:var(--ob-blue)}.kpi-value{margin-top:8px;color:var(--ob-text-strong);font-size:23px;font-weight:680;line-height:1;letter-spacing:-.035em;font-variant-numeric:tabular-nums}.kpi-foot{margin-top:9px;color:var(--ob-text-subtle);font-size:12px}
.panel{min-width:0;margin-top:10px;border:1px solid var(--ob-border);border-radius:14px;background:var(--ob-surface);box-shadow:var(--ob-shadow-panel)}.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:15px 16px 10px}.panel-title h2{margin:0;color:var(--ob-text-strong);font-size:15px;font-weight:650;line-height:1.25}.panel-title p{margin:4px 0 0;color:var(--ob-text-subtle);font-size:12.5px;line-height:1.45}.panel-head .el-select{width:150px;flex:none}
.table-scroll{overflow-x:auto;width:100%}table{width:100%;border-collapse:collapse;text-align:left;font-size:12px;white-space:nowrap}th{padding:10px 16px;font-weight:500;color:var(--ob-text-subtle);background:var(--ob-surface-soft)}td{padding:13px 16px;border-top:1px solid var(--ob-border-soft)}td small,.waiting-row small{display:block;color:var(--ob-text-subtle);font-size:11px;max-width:280px;white-space:normal;overflow-wrap:anywhere;margin-top:3px}.status-pill{display:inline-flex;padding:3px 6px;border-radius:5px;background:var(--ob-hover);font-size:11px;color:var(--ob-text-subtle)}.waiting-row{display:flex;gap:16px;align-items:center;padding:12px 16px;border-top:1px solid var(--ob-border-soft);font-size:12px;overflow-wrap:anywhere}.waiting-row>div{flex:1;min-width:0}.panel :deep(.wh-statistics){padding:6px 16px 16px;overflow:visible}.wh-pagination{padding:12px 16px}
@media(max-width:760px){.statistics-header{flex-wrap:wrap;padding:14px;gap:12px}.statistics-title h1{font-size:17px}.statistics-filters{justify-content:flex-start;width:100%}.statistics-filters .el-input{flex:1;min-width:120px}.statistics-filters .el-select{width:120px}.statistics-scroll{padding:12px}.kpi-grid{display:flex;overflow-x:auto}.kpi-card{flex:0 0 155px}.waiting-row{flex-wrap:wrap;gap:8px}.panel-head{flex-wrap:wrap}.panel :deep(.wh-statistics){padding:6px 12px 12px}}
</style>
