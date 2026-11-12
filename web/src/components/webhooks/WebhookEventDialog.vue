<script setup>
import {computed, nextTick, onBeforeUnmount, ref, watch} from 'vue';
import {ElMessageBox} from 'element-plus';
import {Api} from '../../api.js';
import {statusLabel, tabKey, parseLines} from './webhookConfig.js';
const props = defineProps({modelValue: Boolean, eventId: String});
const emit = defineEmits(['update:modelValue']);
const event = ref(null), loading = ref(false), error = ref(''), tab = ref('stages'), busy = ref(false);
const waitCursor = ref(null), waitsLoading = ref(false);
const waits = ref([]), verification = ref(''), retryStage = ref(''), selectedWait = ref('');
const verifyStage = ref(''), verifyOutcome = ref('unknown'), evidence = ref(''), verifyResult = ref('');
const stageLabels = {pre:'前置脚本',model:'模型／工具阶段',post:'后置结果回写'};
const phaseLabels = {pre_queue:'前置排队',pre_execute:'前置执行',aggregation:'收集与聚合',model_queue:'模型排队',model_tool:'模型／工具执行',human_wait:'人工确认等待',external_wait:'外部事件等待',post_queue:'后置排队',post_execute:'后置执行',delivery:'结果交付',end_to_end:'端到端'};
const measurementLabels = {event:'事件',batch:'批次',assignment:'整轮任务',attempt:'执行尝试',call:'物理调用'};
const phaseDuration = value => value == null ? '尚无结束记录' : `${Number(value).toLocaleString('zh-CN',{maximumFractionDigits:3})} 秒`;
const observationLabel = state => ({pending:'等待补交',applied:'已写入统计',rejected:'观测被拒绝'})[state] || state;
const count = value => Array.isArray(value) ? value.length : value ?? '—';
const intents = new Map();
function actionBody(action, body) {
  const fingerprint = JSON.stringify([props.eventId,action,body]);
  if (!intents.has(fingerprint)) intents.set(fingerprint,crypto.randomUUID());
  return {...body,requestId:intents.get(fingerprint)};
}
const evidenceRefs = computed(() => parseLines(evidence.value));
const tabs = [['stages','阶段链路'],['raw','原始材料'],['results','业务结果'],['waits','归属与等待']];
const time = value => value ? new Date(value).toLocaleString('zh-CN', {hour12: false}) : '—';
const asText = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
const needsReview = computed(() => event.value?.reviewRequired || event.value?.effectState === 'unknown');
let generation = 0, abort;
async function load() {
  const run = ++generation; abort?.abort(); abort = new AbortController(); event.value = null; waits.value = []; waitCursor.value = null; waitsLoading.value = false; error.value = '';
  if (!props.modelValue || !props.eventId) return;
  loading.value = true;
  try {
    const value = await Api.webhookEvent(props.eventId, {signal: abort.signal});
    if (run !== generation) return; event.value = value.event;
    const detail = await Api.webhookWaits({eventId: props.eventId}, {signal: abort.signal});
    if (run === generation) { waits.value = detail.items || []; waitCursor.value = detail.nextCursor; }
  } catch (exception) { if (run === generation && !abort.signal.aborted) error.value = exception?.response?.data?.message || exception.message; }
  finally { if (run === generation) loading.value = false; }
}
async function moreWaits() {
  if (!waitCursor.value || waitsLoading.value) return;
  const run = generation; waitsLoading.value = true;
  try {
    const detail = await Api.webhookWaits({eventId:props.eventId,cursor:waitCursor.value}, {signal:abort.signal});
    if (run === generation) { waits.value.push(...(detail.items || [])); waitCursor.value = detail.nextCursor; }
  } catch (exception) { if (run === generation && !abort.signal.aborted) error.value = exception?.response?.data?.message || exception.message; }
  finally { if (run === generation) waitsLoading.value = false; }
}
watch(() => [props.modelValue, props.eventId], () => { tab.value = 'stages'; verification.value = ''; evidence.value = ''; verifyResult.value = ''; verifyStage.value = ''; verifyOutcome.value = 'unknown'; retryStage.value = ''; intents.clear(); load(); }, {immediate: true});
onBeforeUnmount(() => { generation++; abort?.abort(); });
async function navigate(e) { const target = e.currentTarget; tab.value = tabKey(e, tab.value, tabs); await nextTick(); target?.querySelector(`[data-tab="${tab.value}"]`)?.focus(); }
async function retry() {
  if (!retryStage.value || busy.value) return;
  if (needsReview.value && !verification.value.trim()) { error.value = '先核实前次外部效果并填写依据；不能一键重跑未知动作。'; return; }
  const labels = {pre:'仅前置阶段',post:'仅后置阶段，不重跑模型和前置',model:'仅选定未完成事件，不重跑成功前置',delivery:'仅结果交付，不重复业务'};
  try { await ElMessageBox.confirm(`${labels[retryStage.value]}。对象：${props.eventId}。已有成功步骤不会重做；服务端仍会核验状态与版本。`, '确认阶段专属重试', {type:'warning', confirmButtonText:'重试此阶段',cancelButtonText:'取消',closeOnClickModal:false,autofocus:false}); } catch { return; }
  busy.value = true; error.value = '';
  try { await Api.retryWebhookEvent(props.eventId, {stage:retryStage.value, expectedVersion:event.value.version, verification:verification.value || undefined, requestId:actionBody('retry',{stage:retryStage.value,expectedVersion:event.value.version,verification:verification.value}).requestId}); await load(); }
  catch (exception) { error.value = exception?.response?.data?.message || exception.message; }
  finally { busy.value = false; }
}
async function cancelWait(wait) {
  try { await ElMessageBox.confirm('取消登记后不会被迟到事件唤醒；已认领但未交付事件保留原归属和原因，不自动回流普通队列。', '取消此项等待', {type:'warning',confirmButtonText:'取消等待',cancelButtonText:'保留',closeOnClickModal:false,autofocus:false}); } catch { return; }
  busy.value = true;
  try { await Api.controlWebhookWait(wait.waitId, {action:'cancel',expectedVersion:wait.version,reason:'用户从事件详情取消',requestId:crypto.randomUUID()}); await load(); }
  catch (exception) { error.value = exception?.response?.data?.message || exception.message; }
  finally { busy.value = false; }
}
async function resolveMatch() {
  if (!selectedWait.value || busy.value) return;
  try { await ElMessageBox.confirm(`只把此事件交给等待 ${selectedWait.value}；不会广播、随机选择或另起普通任务。`, '解决匹配冲突', {confirmButtonText:'确认此归属',cancelButtonText:'取消',closeOnClickModal:false}); } catch { return; }
  busy.value = true;
  try {
    const wait = waits.value.find(item => item.waitId === selectedWait.value);
    if (!wait) throw new Error('请先加载候选等待所在页，读取等待版本后再确认归属');
    await Api.controlWebhookWait(wait.waitId, actionBody('resolve_match',{action:'resolve_match',eventId:props.eventId,expectedVersion:wait.version})); await load();
  }
  catch (exception) { error.value = exception?.response?.data?.message || exception.message; }
  finally { busy.value = false; }
}
async function verify() {
  if (busy.value || !verifyStage.value || !verification.value.trim() || !evidenceRefs.value.length) return;
  let result;
  try { result = verifyResult.value.trim() ? JSON.parse(verifyResult.value) : undefined; }
  catch { error.value = '核实结果必须是有效 JSON，或留空。'; return; }
  const body = actionBody('verify',{expectedVersion:event.value.version,stage:verifyStage.value,outcome:verifyOutcome.value,reason:verification.value,evidenceRefs:evidenceRefs.value,result});
  try { await ElMessageBox.confirm(`为此事件的${stageLabels[verifyStage.value]}追加人工核实声明：${statusLabel(verifyOutcome.value)}。只记录核实并收尾，不重放原业务；历史声明保留。`, '记录外部核实结果', {type:'warning',confirmButtonText:'记录核实结果',cancelButtonText:'取消',closeOnClickModal:false}); } catch { return; }
  busy.value = true; error.value = '';
  try { await Api.verifyWebhookEvent(props.eventId,body); await load(); }
  catch (exception) { error.value = exception?.response?.data?.message || exception.message; }
  finally { busy.value = false; }
}
async function rebind() {
  if (busy.value || !event.value?.canRebind) return;
  busy.value = true; error.value = '';
  try {
    const endpoint = (await Api.webhook(event.value.endpointId)).endpoint;
    const prepared = await Api.rebindWebhookEvents(endpoint.id,{prepare:true,eventIds:[props.eventId],expectedRevision:endpoint.revision,toRevision:endpoint.revision});
    try { await ElMessageBox.confirm(`仅此事件尚未执行部分改用当前规则 v${endpoint.revision}。接收版本、有效期和成功前置结果不变；不重跑已完成业务。影响：${JSON.stringify(prepared.impact)}`, '调整旧积压处理版本', {type:'warning',confirmButtonText:'确认此范围',cancelButtonText:'取消',closeOnClickModal:false}); } catch { return; }
    await Api.rebindWebhookEvents(endpoint.id,actionBody('rebind',{confirmationToken:prepared.confirmationToken})); await load();
  } catch (exception) { error.value = exception?.response?.data?.message || exception.message; }
  finally { busy.value = false; }
}
async function recoverAssignment(assignment) {
  if (busy.value || !assignment.canRecover || !verification.value.trim() || !evidenceRefs.value.length) return;
  busy.value = true; error.value = '';
  try {
    const prepared = await Api.recoverWebhookAssignment(assignment.assignmentId,{prepare:true,expectedVersion:assignment.version,decision:'finalizeInterrupted',reason:verification.value,evidenceRefs:evidenceRefs.value});
    try { await ElMessageBox.confirm(`确认已核实原运行已停止，仅作异常终结，不恢复／重演未知运行。影响：${JSON.stringify(prepared.impact)}`, '异常终结原轮次', {type:'warning',confirmButtonText:'确认异常终结',cancelButtonText:'取消',closeOnClickModal:false}); } catch { return; }
    await Api.recoverWebhookAssignment(assignment.assignmentId,actionBody('recover',{confirmationToken:prepared.confirmationToken}));await load();
  } catch (exception) { error.value = exception?.response?.data?.message || exception.message; }
  finally { busy.value = false; }
}
function notificationNeedsVerification(assignment) {
  return Boolean(assignment.notificationVerificationRequired || assignment.notificationChannels?.some(channel => channel.verificationRequired || channel.state === 'unknown'));
}
async function retryNotification(assignment) {
  if (busy.value || !assignment.canRetryNotification) return;
  if (notificationNeedsVerification(assignment)) { error.value='实际发送结果未知，需先核实，未自动重试。不能通过补交适配通知重发此渠道。';return; }
  if (assignment.notificationEffectState === 'unknown' && !verification.value.trim()) { error.value='先核实通知是否已发送，填写依据后才能补交。';return; }
  const body=actionBody('notification',{assignmentId:assignment.assignmentId,expectedVersion:assignment.version,verification:verification.value || undefined});delete body.assignmentId;
  try { await ElMessageBox.confirm('只补交此轮通知，不重跑前置、模型或后置。若前次发送成功但回执不明，请先核实，避免重复通知。','单独补交通知',{type:'warning',confirmButtonText:'仅补交通知',cancelButtonText:'取消',closeOnClickModal:false}); } catch { return; }
  busy.value=true;error.value='';
  try {await Api.retryWebhookNotification(assignment.assignmentId,body);await load();}
  catch(exception){error.value=exception?.response?.data?.message || exception.message;}
  finally {busy.value=false;}
}
</script>
<template>
  <el-dialog :model-value="modelValue" title="事件处理详情" width="min(900px, calc(100vw - 24px))" append-to-body class="mobile-viewport-dialog wh-event-dialog" :close-on-click-modal="!busy" :close-on-press-escape="!busy" :show-close="!busy" @update:model-value="emit('update:modelValue',$event)">
    <el-skeleton v-if="loading" :rows="7" animated />
    <div v-if="error" class="wh-alert" role="alert">{{ error }}<el-button :disabled="busy" @click="load">重新读取</el-button></div>
    <template v-if="event">
      <div class="wh-event-identity"><code>{{ event.eventId }}</code><span class="wh-pill">{{ statusLabel(event.state) }}</span><span v-if="event.reviewRequired" class="wh-pill wh-warning">需要核实</span><p>首次接收 {{ time(event.receivedAt) }} · 序号 {{ event.receiveSeq }} · 接收 v{{ event.receivedRevision }} / 处理 v{{ event.processingRevision }} · {{ event.expiresAt ? `有效至 ${time(event.expiresAt)}` : '不过期' }}</p><p>{{ event.scopePath || event.endpointId }} <span v-if="event.reason"> · {{ event.reason }}</span></p></div>
      <nav class="wh-inline-tabs" role="tablist" aria-label="事件详情分类" @keydown="navigate"><button v-for="[id,label] in tabs" :key="id" role="tab" :data-tab="id" :aria-selected="tab === id" :tabindex="tab === id ? 0 : -1" @click="tab = id">{{ label }}</button></nav>
      <div v-if="tab === 'stages'" class="wh-event-content">
        <p>模型单次响应、逐事件报告、整轮终结、结果交付是不同事实。当前阶段成功不表示整条链都成功。</p>
        <section class="wh-section"><h3>阶段时间线 · 框架计时</h3><p>共享批次／整轮耗时只列一次，不乘以事件数；人工确认和外部等待单列，不算作模型慢。计时记录不是外部业务成功证明。</p><ol class="wh-timeline"><li v-for="span in event.phaseSpans || []" :key="span.spanId"><h3>{{ phaseLabels[span.phase] || span.phase }} <span class="wh-pill">{{ measurementLabels[span.measurementLevel] || span.measurementLevel }}{{ span.shared ? ' · 共享' : '' }}</span></h3><p>{{ time(span.startedAt) }} → {{ time(span.endedAt) }} · {{ phaseDuration(span.durationSeconds) }}</p><p v-if="span.endReason">结束原因：{{ span.endReason }}</p><p v-if="span.attemptId">执行尝试 <code>{{ span.attemptId }}</code></p></li></ol><p v-if="!event.phaseSpans?.length" class="wh-empty">暂无可用阶段计时，不将缺失显示为零耗时。</p></section>
        <h3>脚本执行记录</h3><ol class="wh-timeline"><li v-for="(stage,index) in event.stages || []" :key="stage.jobId || stage.stage || index"><h3>{{ stage.label || stageLabels[stage.stage] || stage.stage }} <span class="wh-pill">{{ statusLabel(stage.state) }}</span></h3><p>{{ time(stage.startedAt || stage.queuedAt) }} → {{ time(stage.terminalAt) }} · 规则 v{{ stage.revision ?? '—' }}</p><p v-if="stage.error" class="wh-error-text">{{ stage.error.code || stage.error }} {{ stage.error.message }}</p><details v-for="attempt in stage.attempts || []" :key="attempt.attemptId"><summary>尝试 {{ attempt.attemptNo }} · {{ attempt.attemptId }} · {{ statusLabel(attempt.state) }}</summary><dl class="wh-value-grid"><div><dt>解释器</dt><dd>{{ attempt.execution?.interpreter || '—' }}</dd></div><div><dt>实际 cwd</dt><dd>{{ attempt.execution?.cwd || '—' }}</dd></div><div><dt>效果已知程度</dt><dd>{{ attempt.sideEffectState || '未知' }}</dd></div><div><dt>退出码</dt><dd>{{ attempt.exitCode ?? '—' }}</dd></div></dl><p v-if="attempt.errorClass || attempt.errorSummary" class="wh-error-text">{{ attempt.errorClass }} · {{ attempt.errorSummary }}</p><p v-if="attempt.logStatus === 'purged'" class="wh-empty">脚本日志已按保留策略清理，不表示当时没有输出。</p><template v-else><pre>{{ attempt.stderr || '没有可用日志' }}</pre><p v-if="attempt.stderrTruncated">日志已截断；不表示完整进程输出。</p></template><p v-if="attempt.resultStatus === 'purged'" class="wh-empty">脚本结果快照已按保留策略清理，原处理状态仍保留。</p><template v-else-if="attempt.result != null"><h4>脚本协议结果（脚本声明）</h4><pre>{{ asText(attempt.result) }}</pre></template><p v-else class="wh-empty">尚无可用脚本协议结果，不表示业务成功。</p></details></li></ol>
        <p v-if="!event.stages?.length" class="wh-empty">没有脚本阶段记录。202 仅表示持久收件；模型运行另见下方。</p>
        <section v-for="assignment in event.assignments || []" :key="assignment.assignmentId" class="wh-section"><h3>模型轮次 · {{ statusLabel(assignment.state) }}</h3><p>归属 {{ assignment.assignmentId }} · 会话 {{ assignment.conversationId || '—' }} · 正式终结 {{ time(assignment.finalizedAt) }}</p><p v-for="run in assignment.runs || []" :key="run.runId"><code>{{ run.runId }}</code> · {{ run.relationKind }} · 计费归属 {{ run.billingScope }}</p><p v-if="!assignment.runs?.length">尚无关联模型运行；不代表业务已完成。</p><p v-if="assignment.terminalReason">{{ assignment.terminalReason }}</p><p v-if="assignment.processingStatus === 'purged'" class="wh-empty">轮次处理快照已按保留策略清理，归属与计费记录仍保留。</p></section>
        <section v-if="event.verifyStages?.length" class="wh-section"><h3>记录外部核实结果 · 不重跑</h3><p>请先到外部系统确认前次效果。这里追加管理员声明，保留脚本／模型的原始报告，不伪装框架独立证明。</p><el-select v-model="verifyStage" aria-label="核实阶段" placeholder="选择允许核实的阶段"><el-option v-for="stage in event.verifyStages" :key="stage" :value="stage" :label="stageLabels[stage] || stage" /></el-select><el-select v-model="verifyOutcome" aria-label="核实结论"><el-option v-for="outcome in ['unknown','completed','skipped','failed']" :key="outcome" :value="outcome" :label="statusLabel(outcome)" /></el-select><el-input v-model="verification" type="textarea" aria-label="核实说明" placeholder="外部系统查询结果、核实时间和结论" /><el-input v-model="evidence" type="textarea" aria-label="核实证据引用" placeholder="每行一个外部操作 ID 或可验证证据地址，必填" /><el-input v-model="verifyResult" type="textarea" aria-label="核实结构化结果" placeholder="可选：结果 JSON；不会执行其中内容" /><el-button :loading="busy" :disabled="!verifyStage || !verification.trim() || !evidenceRefs.length" @click="verify">记录核实结果…</el-button></section>
        <section v-if="event.canRebind" class="wh-section"><h3>调整此积压的后续规则</h3><p>只将此事件未执行部分绑定到入口当前规则，先查看影响再确认；不更改接收版本、有效期或重跑成功前置。</p><el-button :loading="busy" @click="rebind">查看当前规则影响…</el-button></section>
        <section v-if="event.retryStages?.length" class="wh-section"><h3>只重试未完成阶段</h3><el-select v-model="retryStage" placeholder="选择服务端允许的阶段" aria-label="重试阶段"><el-option v-for="stage in event.retryStages" :key="stage" :value="stage" :label="({pre:'仅前置',post:'仅后置',model:'仅此未完成事件的模型阶段',delivery:'仅交付'})[stage] || stage" /></el-select><p v-if="needsReview" class="wh-warning">前次效果未知，必须先在外部系统核实。若原动作已经完成，应记录核实结果而不是重演。</p><el-input v-if="needsReview" v-model="verification" type="textarea" aria-label="外部结果核实依据" placeholder="外部操作 ID、查询结果及可验证依据" /><el-button :loading="busy" :disabled="!retryStage || (needsReview && !verification.trim())" @click="retry">确认重试范围…</el-button></section>
      </div>
      <div v-else-if="tab === 'raw'" class="wh-event-content"><template v-if="event.payloadStatus === 'retained' || event.raw"><h3>原始 Query</h3><pre>{{ asText(event.raw?.query) }}</pre><h3>原始正文 · {{ event.raw?.contentType }}</h3><pre>{{ event.raw?.content ?? '原始正文不可用' }}</pre><details><summary>解析后的正文（非原文）</summary><pre>{{ asText(event.raw?.body) }}</pre></details><h3>前置派生材料（原文不被覆盖）</h3><pre>{{ asText(event.derived) || '没有派生材料' }}</pre></template><p v-else class="wh-empty">正文不可用：{{ event.payloadStatus || '未提供' }}。身份、指纹及处理记录仍独立保留，不把清理后的正文伪装成空请求。</p></div>
      <div v-else-if="tab === 'results'" class="wh-event-content"><p>model / script 是业务结果声明；framework 是异常处置或自动计量。框架不通用证明外部业务成功。</p><section v-if="event.observations?.length" class="wh-section"><h3>观测写入状态</h3><p>观测失败只影响统计完整性，不回滚已发生的业务，也不因此重新执行业务。</p><div v-for="observation in event.observations" :key="observation.operationId" class="wh-section"><h3>{{ observationLabel(observation.state) }} <span class="wh-pill">{{ observation.source }}</span></h3><p><code>{{ observation.operationId }}</code></p><p>登记 {{ time(observation.createdAt) }} · 写入 {{ time(observation.appliedAt) }}</p><p v-if="observation.errorReason" class="wh-warning">{{ observation.errorReason }}</p></div></section><section v-for="receipt in event.receipts || []" :key="receipt.receiptId || receipt.id" class="wh-section"><h3>{{ receipt.source === 'framework' ? '框架处置：' + statusLabel(receipt.disposition || receipt.outcome) : statusLabel(receipt.outcome) }} <span class="wh-pill">{{ receipt.source }}</span></h3><p>{{ receipt.summary || receipt.reason }}</p><p>回执版本 {{ receipt.version ?? receipt.resultVersion }} · {{ time(receipt.reportedAt) }}</p><pre v-if="receipt.result">{{ asText(receipt.result) }}</pre><ul><li v-for="reference in receipt.evidenceRefs || []" :key="reference"><code>{{ reference }}</code></li></ul></section><p v-if="!event.receipts?.length" class="wh-empty">尚未报告结果；不代表已完成。</p></div>
      <div v-else class="wh-event-content"><section v-if="event.assignments?.some(item => item.canRecover || (item.canRetryNotification && !notificationNeedsVerification(item)))" class="wh-section"><h3>恢复操作的核实依据</h3><el-input v-model="verification" type="textarea" aria-label="轮次或通知核实说明" placeholder="核实原运行／通知的当前状态；不会自动重演业务" /><el-input v-model="evidence" type="textarea" aria-label="轮次核实证据引用" placeholder="每行一个可验证的证据引用；异常终结必须填写" /></section><h3>批次与完整轮次归属</h3><section v-for="assignment in event.assignments || []" :key="assignment.assignmentId" class="wh-section"><p><code>{{ assignment.assignmentId }}</code> · {{ statusLabel(assignment.state) }}</p><p>初始批次 {{ assignment.initialBatchId || '人工轮次' }} · 来源 {{ assignment.originKind }} · 认领 {{ time(assignment.claimedAt) }} · 首次交付 {{ time(assignment.deliveredAt) }}</p><p>轮次终结 {{ time(assignment.finalizedAt) }} · 通知适配入队 {{ assignment.notificationState || '—' }} · 适配效果 {{ assignment.notificationEffectState || '—' }}</p><p>此处 delivered 只表示已入通知队列／已发布会话事件，不代表外部渠道已送达。渠道未知效果仍须核实，补交不会重跑业务。</p><div v-for="channel in assignment.notificationChannels || []" :key="channel.channel" class="wh-section"><h4>渠道发送记录 · {{ channel.channel }}</h4><p>实际渠道状态：{{ channel.state || '未提供' }}</p><p v-if="channel.error" class="wh-error-text">{{ asText(channel.error) }}</p></div><p v-if="notificationNeedsVerification(assignment)" class="wh-warning" role="alert">实际发送结果未知，需先核实，未自动重试。适配入队不表示实际发送成功；此状态不提供直接补发。</p><div class="wh-actions"><el-button v-if="assignment.canRecover" :disabled="busy || !verification.trim() || !evidenceRefs.length" @click="recoverAssignment(assignment)">核实后异常终结…</el-button><el-button v-if="assignment.canRetryNotification && !notificationNeedsVerification(assignment)" :disabled="busy" @click="retryNotification(assignment)">仅补交通知…</el-button></div><details><summary>完整归属／快照</summary><pre>{{ asText(assignment) }}</pre></details></section>
        <h3>外部等待</h3><section v-for="wait in waits" :key="wait.waitId" class="wh-section"><h3>{{ wait.waitId }} <span class="wh-pill">{{ statusLabel(wait.state) }}</span></h3><p>入口 {{ wait.endpointId }} · 归属 {{ wait.assignmentId }} · 游标 {{ wait.afterSeq ?? '—' }}</p><p>{{ wait.deadline ? `期限 ${time(wait.deadline)}` : '无超时' }} · 已认领 {{ count(wait.claimed) }} / 已交付 {{ count(wait.delivered) }}（不是业务完成）</p><pre>{{ asText(wait.match) }}</pre><details><summary>认领／交付事件清单</summary><pre>{{ asText({claimed:wait.claimed,delivered:wait.delivered}) }}</pre></details><p v-if="wait.pauseReasons?.length" class="wh-warning">{{ wait.pauseReasons.map(statusLabel).join('；') }}</p><el-button v-if="wait.canCancel" :disabled="busy" @click="cancelWait(wait)">取消等待…</el-button></section><p v-if="!waits.length">没有与此事件关联的等待。</p>
        <el-button v-if="waitCursor" :loading="waitsLoading" @click="moreWaits">加载更多等待</el-button><section v-if="event.waitCandidates?.length" class="wh-section"><h3>等待匹配冲突</h3><p>事件暂存，尚未交付；选择明确归属，不广播或回落普通任务。</p><el-select v-model="selectedWait" aria-label="冲突等待候选" placeholder="选择合法等待"><el-option v-for="item in event.waitCandidates" :key="item.waitId" :value="item.waitId" :disabled="!waits.some(wait => wait.waitId === item.waitId)" :label="`${item.waitId} · ${item.assignmentId || ''}`" /></el-select><el-button :disabled="!selectedWait || busy" @click="resolveMatch">确认归属…</el-button></section>
      </div>
    </template>
    <template #footer><el-button :disabled="busy" @click="emit('update:modelValue',false)">关闭</el-button></template>
  </el-dialog>
</template>
