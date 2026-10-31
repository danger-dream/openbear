<script setup>
import {computed, nextTick, onBeforeUnmount, ref, watch} from 'vue';
import {ElMessage, ElMessageBox} from 'element-plus';
import {Api} from '../../api.js';
import AdaptiveMdEditor from '../AdaptiveMdEditor.vue';
import WebhookField from './WebhookField.vue';
import WebhookKey from './WebhookKey.vue';
import WebhookScriptEditor from './WebhookScriptEditor.vue';
import WebhookCollectionSettings from './WebhookCollectionSettings.vue';
import WebhookStatistics from './WebhookStatistics.vue';
import {createWebhookEditor} from './useWebhookEditor.js';
import {TRIGGER_TABS, statusLabel, tabKey, targetTree} from './webhookConfig.js';
import ModelSettings from '../ModelSettings.vue';
import {normalizedRunDefaults, updateRunDefault} from '../folderRunDefaults.js';
import WebhookAdvancedSettings from './WebhookAdvancedSettings.vue';
import './webhooks.css';
const props = defineProps({scope: {type: Object, required: true}, title: String, active: {type: Boolean, default: true}});
const emit = defineEmits(['cancel', 'saved']);
const editor = createWebhookEditor(Api, {defaultName:()=>props.title || ''});
const {draft, endpoint, environment, loading, loaded, saving, dirty: ruleDirty, error, errors, conflict} = editor;
const schemaInput = ref(null), schemaError = ref('');
const schemaText = computed(() => schemaInput.value ?? (draft.config.processing.resultSchema ? JSON.stringify(draft.config.processing.resultSchema, null, 2) : ''));
const dirty = computed(() => ruleDirty.value || Boolean(schemaError.value));
function updateSchema(value) {
  schemaInput.value = value;
  try {
    const parsed = value.trim() ? JSON.parse(value) : null;
    if (parsed !== null && (typeof parsed !== 'object' || Array.isArray(parsed))) throw new Error('结果 Schema 必须是 JSON 对象');
    draft.config.processing.resultSchema = parsed; schemaError.value = '';
  } catch { schemaError.value = '结果 Schema 必须是有效 JSON 对象；原输入已保留，尚未保存'; }
}
function discardDraft() { schemaInput.value = null; schemaError.value = ''; editor.discard(); }
const tab = ref('connection'), phase = ref('pre'), root = ref(null);
const targets = ref([]), targetError = ref(''), targetsLoading = ref(false);
const copiedUrl = ref(false);
const rotateOpen = ref(false), graceSeconds = ref(0), actionBusy = ref(false);
const deleteOpen = ref(false), stopRunning = ref(false), preparation = ref(null), preparing = ref(false), preparationError = ref('');
let preparationGeneration = 0;
watch(() => [rotateOpen.value, deleteOpen.value, graceSeconds.value, stopRunning.value], async () => {
  const current = ++preparationGeneration; preparation.value = null; preparationError.value = '';
  if ((!rotateOpen.value && !deleteOpen.value) || !endpoint.value) return;
  preparing.value = true;
  try {
    const options = {expectedControlRevision:endpoint.value.controlRevision};
    const result = rotateOpen.value
      ? await Api.rotateWebhookKey(endpoint.value.id, {prepare:true,graceSeconds:graceSeconds.value,...options})
      : await Api.webhookDeleteImpact(endpoint.value.id, {stopRunning:stopRunning.value,...options});
    if (current === preparationGeneration) preparation.value = {...result,requestId:crypto.randomUUID()};
  } catch (exception) { if (current === preparationGeneration) preparationError.value = editor.message(exception); }
  finally { if (current === preparationGeneration) preparing.value = false; }
});
const modelOptions = ref([]), folderDefaults = ref({}), modelsError = ref(''), modelsLoading = ref(false);
let modelGeneration = 0;
const targetOverrides = computed(() => Object.fromEntries(Object.entries(draft.config.target.runConfig || {}).filter(([,value]) => value != null)));
const targetEffective = computed(() => normalizedRunDefaults({local:targetOverrides.value,inherited:{...folderDefaults.value.inherited,...folderDefaults.value.local},fallback:folderDefaults.value.fallback,resolved:folderDefaults.value.resolved},modelOptions.value));
const targetInherited = computed(() => Object.fromEntries(['mainModel','mainThinkingLevel','mainFastMode'].map(field => {
  const local = {...targetOverrides.value}; delete local[field];
  return [field,normalizedRunDefaults({local,inherited:{...folderDefaults.value.inherited,...folderDefaults.value.local},fallback:folderDefaults.value.fallback,resolved:folderDefaults.value.resolved},modelOptions.value)[field]];
})));
async function loadModelOptions() {
  const run = ++modelGeneration; modelsError.value = ''; modelOptions.value = []; folderDefaults.value = {};
  if (props.scope.type !== 'folder') return;
  modelsLoading.value = true;
  try {
    const [options,folder] = await Promise.all([Api.rathOptions(),Api.conversationFolderProperties(props.scope.id)]);
    if (run !== modelGeneration) return;
    modelOptions.value = options.models || []; folderDefaults.value = folder.runDefaults || {};
  } catch (exception) { if (run === modelGeneration) modelsError.value = editor.message(exception); }
  finally { if (run === modelGeneration) modelsLoading.value = false; }
}
function setTargetRunConfig(field,selection) {
  const values = updateRunDefault(targetOverrides.value,field,selection);
  draft.config.target.runConfig = {mainModel:values.mainModel ?? null,mainThinkingLevel:values.mainThinkingLevel ?? null,mainFastMode:values.mainFastMode ?? null};
  if (field === 'mainModel') {
    const model = modelOptions.value.find(item => item.key === targetEffective.value.mainModel);
    if (draft.config.target.runConfig.mainThinkingLevel != null && !model?.thinkingLevels?.includes(draft.config.target.runConfig.mainThinkingLevel)) draft.config.target.runConfig.mainThinkingLevel = null;
    if (draft.config.target.runConfig.mainFastMode === true && !model?.supportsFast) draft.config.target.runConfig.mainFastMode = null;
  }
}
let targetGeneration = 0, targetAbort;
const tree = computed(() => targetTree(targets.value));
const selectedTarget = computed(() => targets.value.find(item => (item.id || item.conversationUuid) === draft.config.target.conversationId));
const endpointUrl = computed(() => endpoint.value?.url || '');
const receivingStatus = computed(() => !endpoint.value ? '保存后生成接收地址' : ({receiving:'正在接收',paused:'正在接收 · 处理已暂停',globalDisabled:'不接收 · 系统已关闭',disabled:'不接收 · 入口未启用',deleted:'不接收 · 入口已删除'})[endpoint.value.effectiveStatus] || statusLabel(endpoint.value.effectiveStatus));
const errorFor = path => errors.value.find(item => item.path === path)?.message;
watch([() => props.scope.type, () => props.scope.id], () => { tab.value = 'connection'; schemaInput.value = null; schemaError.value = ''; editor.load(props.scope); loadTargets(); loadModelOptions(); }, {immediate: true});
async function loadTargets(query = '') {
  const generation = ++targetGeneration; targetAbort?.abort(); targetAbort = new AbortController();
  targetsLoading.value = true; targetError.value = '';
  try {
    const result = await Api.webhookTargets({search: query, scopeType:props.scope.type, scopeId:props.scope.id, selectedId: draft.config.target.conversationId || undefined}, {signal: targetAbort.signal});
    if (generation === targetGeneration) targets.value = result.items || [];
  } catch (exception) { if (generation === targetGeneration && !targetAbort.signal.aborted) targetError.value = editor.message(exception); }
  finally { if (generation === targetGeneration) targetsLoading.value = false; }
}
function selectTarget(value) {
  const item = targets.value.find(item => (item.id || item.conversationUuid) === value);
  if (!item || !['conversation'].includes(item.kind || item.type) || item.available === false || item.selectable === false || item.archived || item.deleted) return;
  draft.config.target.conversationId = value;
}
function selectTargetMode(value) {
  draft.config.target.mode = value;
  if (value === 'newConversation') { draft.config.target.conversationId = null; draft.config.target.runConfig = {mainModel:null,mainThinkingLevel:null,mainFastMode:null}; }
  else draft.config.target.runConfig = null;
  draft.config.target.conversationRetention = {keepLast:null,keepDays:null};
}
function retentionNeedsPreview(id,target) {
  const next = target.conversationRetention || {};
  const saved = endpoint.value?.config?.target?.conversationRetention || {};
  const values = [next.keepLast, next.keepDays];
  if (!id || target.mode !== 'newConversation' || values.every(value => value == null)) return false;
  if (values.some(value => value != null && !(Number.isInteger(value) && value > 0))) return false;
  return next.keepLast !== (saved.keepLast ?? null) || next.keepDays !== (saved.keepDays ?? null);
}
async function confirmRetention({id,config}) {
  if (!retentionNeedsPreview(id,config.target)) return true;
  const next = config.target.conversationRetention;
  const {count} = await Api.previewConversationRetention({kind: 'webhook', id, retention: {keepLast: next.keepLast ?? null, keepDays: next.keepDays ?? null}});
  if (!count) return true;
  try { await ElMessageBox.confirm(`保存后会立即删除这个入口创建的 ${count} 个旧会话，删除后不可恢复。`, '确认会话保留', {type: 'warning', confirmButtonText: '保存并清理', cancelButtonText: '取消', closeOnClickModal: false}); return true; }
  catch { return false; }
}
async function navigate(event) {
  const next = tabKey(event, tab.value, TRIGGER_TABS); if (next === tab.value) return;
  tab.value = next; await nextTick(); root.value?.querySelector(`[data-wh-tab="${next}"]`)?.focus();
}
async function save(enable) {
  if (schemaError.value) {
    tab.value = 'advanced'; await nextTick();
    const field = root.value?.querySelector('[data-field="processing.resultSchema"]');
    const details = field?.closest('details'); if (details) details.open = true;
    field?.scrollIntoView?.({block:'nearest'}); field?.querySelector('input, textarea')?.focus(); return false;
  }
  const submittedSchema = schemaInput.value;
  const success = await editor.save(enable, {confirm:confirmRetention});
  if (success) { if (schemaInput.value === submittedSchema && !schemaError.value) schemaInput.value = null; ElMessage.success('规则已保存；实际接收与派发状态见连接页'); emit('saved', endpoint.value); }
  else if (errors.value.length) {
    const first = errors.value[0]; tab.value = first.tab;
    if (first.path.startsWith('pre.') || first.path.startsWith('post.')) phase.value = first.path.split('.')[0];
    await nextTick(); const field = root.value?.querySelector(`[data-field="${first.path}"]`);
    const details = field?.closest('details'); if (details) details.open = true;
    field?.scrollIntoView?.({block: 'nearest'}); field?.querySelector('input, textarea, button, [tabindex]')?.focus();
  }
  return success;
}
async function canLeave() {
  if (saving.value || actionBusy.value) return false;
  if (!dirty.value) return true;
  try {
    await ElMessageBox.confirm('有未保存的触发器规则。即时暂停／停止不会因放弃草稿而撤销。', '保留本次编辑？', {confirmButtonText: '保存后离开', cancelButtonText: '放弃更改', distinguishCancelAndClose: true, closeOnClickModal: false});
    const saved = await save(); return saved && !dirty.value;
  } catch (action) { if (action === 'cancel') { discardDraft(); return true; } return false; }
}
async function cancel() { if (await canLeave()) emit('cancel'); }
async function copy(value) {
  try { if (!navigator.clipboard?.writeText) throw new Error('当前环境不支持复制'); await navigator.clipboard.writeText(value); ElMessage.success('已复制'); copiedUrl.value = true; }
  catch { ElMessage.warning('复制失败，请选中地址手动复制'); }
}
async function control(action, options = {}) {
  const effects = {pause: options.scope === 'model' ? '仅暂停尚未开始的自动模型阶段；仍接收，独立前置脚本不受此开关影响。当前模型阶段可收尾。' : '仍接收新事件；尚未开始的阶段暂停，当前阶段可收尾。', resume: options.scope === 'model' ? '仅解除手动模型暂停；全局、派发及未知结果阻断仍需分别处理。' : '保留积压并重新检查有效期、目标和撤销状态。未知／中断任务不会自动重演。', stop: options.scope === 'current' ? '取消当前轮及子任务／等待，暂停后续自动模型派发。独立前置脚本未必停止；外部效果不能回滚。' : '取消此入口当前脚本、运行和等待，并暂停后续派发。接收开关不变，外部效果不能回滚。'};
  try { await ElMessageBox.confirm(effects[action], action === 'resume' ? '恢复派发' : action === 'pause' ? '暂停派发' : '停止处理', {type: 'warning', confirmButtonText: '确认此范围', cancelButtonText: '取消', closeOnClickModal: false, autofocus: false}); }
  catch { return; }
  await editor.control(action, options);
}
async function rotate() {
  if (actionBusy.value || !endpoint.value) return;
  actionBusy.value = true; error.value = '';
  try {
    if (!preparation.value?.confirmationToken) throw new Error('请先取得此次操作的影响预览');
    const data = await Api.rotateWebhookKey(endpoint.value.id, {confirmationToken:preparation.value.confirmationToken,requestId:preparation.value.requestId});
    if (data.endpoint) endpoint.value = data.endpoint;
    rotateOpen.value = false;
  } catch (exception) { error.value = editor.message(exception); }
  finally { actionBusy.value = false; }
}
async function remove() {
  if (actionBusy.value || !endpoint.value) return;
  actionBusy.value = true;
  try {
    if (!preparation.value?.confirmationToken) throw new Error('请先取得此次删除范围的影响预览');
    const data = await Api.deleteWebhook(endpoint.value.id, {confirmationToken:preparation.value.confirmationToken,requestId:preparation.value.requestId});
    if (data.ok === false) throw new Error(data.message || '删除失败');
    deleteOpen.value = false; editor.adopt(null); emit('saved', null); ElMessage.success('入口已删除；历史处理记录保留');
  } catch (exception) { error.value = editor.message(exception); }
  finally { actionBusy.value = false; }
}
function unload(event) { if (dirty.value || saving.value || actionBusy.value) { event.preventDefault(); event.returnValue = ''; } }
window.addEventListener('beforeunload', unload);
onBeforeUnmount(() => { editor.dispose(); modelGeneration++; preparationGeneration++; targetGeneration++; targetAbort?.abort(); window.removeEventListener('beforeunload', unload); });
defineExpose({canLeave, save, dirty, saving, endpoint});
</script>
<template>
  <div ref="root" class="wh-editor" >
    <div v-if="loading" class="wh-loading" role="status"><el-skeleton :rows="6" animated /></div>
    <div v-else-if="!loaded" class="wh-empty"><p role="alert">{{ error || '入口尚未加载' }}</p><el-button @click="editor.load(scope)">重新加载</el-button></div>
    <template v-else>
      <div v-if="error && !conflict" class="wh-alert" role="alert">{{ error }}</div>
      <div v-if="conflict" class="wh-alert" role="alert"><p>{{ conflict.mergeReady ? '已读取最新保存基准，尚未提交。请对照下方最新配置手动合并草稿，再点击保存；保存将提交完整草稿。' : '配置已被其他操作更新。草稿已保留，不会自动覆盖；请读取最新版后手动合并，或放弃草稿。' }}当前版本 {{ conflict.currentRevision ?? '未知' }}。</p><p v-if="error">{{ error }}</p><details v-if="conflict.details"><summary>查看最新配置（用于对照合并）</summary><pre>{{ JSON.stringify(conflict.details,null,2) }}</pre></details><el-button size="small" :disabled="saving" @click="discardDraft(); editor.load(scope)">放弃草稿并重载</el-button><el-button size="small" :loading="saving" @click="editor.prepareMerge()">{{ conflict.mergeReady ? '重新读取最新版' : '保留草稿手动合并' }}</el-button></div>
      <div v-if="errors.length" class="wh-alert" role="alert">{{ errors[0].message }}<span v-if="errors.length > 1">（另有 {{ errors.length - 1 }} 项）</span></div>
      <div class="wh-editor-layout">
        <nav class="wh-navigation" role="tablist" aria-label="触发器分类" @keydown="navigate">
          <button v-for="[id,label] in TRIGGER_TABS" :id="`wh-tab-${id}`" :key="id" :data-wh-tab="id" role="tab" :aria-selected="tab === id" :aria-controls="`wh-panel-${id}`" :tabindex="tab === id ? 0 : -1" @click="tab = id">{{ label }}</button>
        </nav>
        <main class="wh-editor-main">
          <section v-show="tab === 'connection'" id="wh-panel-connection" class="wh-form-scroll" role="tabpanel" aria-labelledby="wh-tab-connection">
            <section class="wh-section">
              <div class="wh-section-heading"><h3>连接</h3><el-switch v-model="draft.enabled" active-text="启用入口（保存后生效）" /></div>
              <p role="status">接收状态：<strong>{{ receivingStatus }}</strong></p>
              <WebhookField v-model="draft.name" label="入口名称" :placeholder="title" /><WebhookField v-model="draft.description" label="说明（可选）" />
              <div class="wh-field"><label>接收地址</label><div class="wh-copy-row"><input readonly :value="endpointUrl || '保存后生成独立入口；不继承到子目录／会话'" aria-label="接收地址" @focus="$event.target.select()" /><el-button :disabled="!endpointUrl" @click="copy(endpointUrl)">复制</el-button></div><small>{{ endpoint?.publicUrlVerified ? '使用已配置的外部地址；仍须由你配置反向代理。' : '地址未验证公网可达；不会自动开放防火墙或配置反向代理。' }}</small></div>
              <WebhookKey v-if="active && tab === 'connection'" :endpoint-id="endpoint?.id" :revision="endpoint?.controlRevision" :busy="saving || actionBusy" @rotate="rotateOpen = true" />
            </section>
            <section class="wh-section"><h3>事件在哪个会话处理</h3>
              <p v-if="scope.type === 'conversation'">固定当前会话：<b>{{ title || scope.id }}</b>。新批次排队，不在当前工具执行中途硬插入。</p>
              <template v-else><el-radio-group :model-value="draft.config.target.mode" @update:model-value="selectTargetMode"><el-radio value="newConversation">新建会话</el-radio><el-radio value="fixedConversation">固定会话</el-radio></el-radio-group><p>新建会话的模型设置可单独选择；固定会话沿用它自己的设置。</p>
                <div v-if="draft.config.target.mode === 'fixedConversation'" class="wh-field" data-field="target.conversationId"><label for="wh-target">目标会话</label><el-tree-select id="wh-target" :model-value="draft.config.target.conversationId" :data="tree" filterable :check-strictly="false" :render-after-expand="false" node-key="value" :loading="targetsLoading" :filter-method="loadTargets" placeholder="搜索并选择具体会话" :aria-invalid="Boolean(errorFor('target.conversationId'))" @update:model-value="selectTarget" /><small :class="{'wh-error-text': errorFor('target.conversationId') || targetError}">{{ errorFor('target.conversationId') || targetError || selectedTarget?.path || '只显示当前目录及子目录内未归档的会话。' }}</small></div>
                <div v-else class="wh-target-models"><p v-if="modelsError" class="wh-error-text" role="alert">{{ modelsError }}<el-button link @click="loadModelOptions">重新读取模型</el-button></p><ModelSettings :model-value="targetOverrides" :effective="targetEffective" :inherited="targetInherited" :models="modelOptions" inheritable inherit-label="继承目录" :show-agent="false" :show-strategy="false" :disabled="modelsLoading || Boolean(modelsError)" @change="setTargetRunConfig" /><p>每项留空先继承目录，再用系统默认。只对之后新建的会话生效。</p></div>
                <div v-if="draft.config.target.mode === 'newConversation' && draft.config.target.conversationRetention" class="wh-retention"><h4>会话保留</h4><div class="wh-form-grid"><WebhookField v-model="draft.config.target.conversationRetention.keepLast" path="target.conversationRetention.keepLast" type="number" nullable :step="1" :min="1" label="保留最近" unit="个" :error="errorFor('target.conversationRetention.keepLast')" /><WebhookField v-model="draft.config.target.conversationRetention.keepDays" path="target.conversationRetention.keepDays" type="number" nullable :step="1" :min="1" label="保留天数" unit="天" :error="errorFor('target.conversationRetention.keepDays')" /></div><p>超过任一条件的旧会话会自动删除，不可恢复；留空不清理。置顶、归档、移出本目录或你发过消息的会话会保留。</p></div>
              </template>
            </section>
            <section v-if="endpoint" class="wh-section"><div class="wh-actions"><el-button :disabled="saving" @click="control(endpoint.dispatchPaused ? 'resume' : 'pause')">{{ endpoint.dispatchPaused ? '恢复处理…' : '暂停处理…' }}</el-button><el-button :disabled="saving" type="danger" plain @click="control('stop', {scope:'all'})">停止处理…</el-button></div><p>暂停时仍接收消息；关闭上方启用开关并保存，才会停止接收。</p></section>
          </section>
          <section v-show="tab === 'processing'" id="wh-panel-processing" class="wh-processing" role="tabpanel" aria-labelledby="wh-tab-processing">
            <div class="wh-instructions" data-field="processing.instructions"><AdaptiveMdEditor v-model="draft.config.processing.instructions" language="markdown" completion-mode="none" /></div>
            <div class="wh-server-changes" data-field="processing.allowServerChanges"><el-checkbox v-model="draft.config.processing.allowServerChanges">允许改动服务器和实例</el-checkbox><p :class="{'is-risk': draft.config.processing.allowServerChanges}">{{ draft.config.processing.allowServerChanges ? '事件处理无人值守，可能被事件内容或服务器返回的内容误导而执行改动（运行命令、写文件、操作节点和实例）。只在确实需要时开启。' : '未勾选时，处理事件时只能查看服务器和实例。' }}</p></div>

          </section>
          <section v-show="tab === 'scripts'" id="wh-panel-scripts" class="wh-fill" role="tabpanel" aria-labelledby="wh-tab-scripts"><WebhookScriptEditor v-model:phase="phase" :config="draft.config" :environment="environment" :errors="errors" /></section>
          <section v-show="tab === 'batching'" id="wh-panel-batching" class="wh-fill" role="tabpanel" aria-labelledby="wh-tab-batching"><WebhookCollectionSettings :config="draft.config" :environment="environment" :errors="errors" /></section>
          <section v-if="tab === 'statistics'" id="wh-panel-statistics" class="wh-fill" role="tabpanel" aria-labelledby="wh-tab-statistics"><WebhookStatistics :endpoint-id="endpoint?.id" :statistics="draft.config.statistics" editable @update:statistics="draft.config.statistics = $event" /></section>
          <section v-show="tab === 'advanced'" id="wh-panel-advanced" class="wh-fill" role="tabpanel" aria-labelledby="wh-tab-advanced"><WebhookAdvancedSettings :config="draft.config" :environment="environment" :errors="errors" :schema-text="schemaText" :schema-error="schemaError" @schema-change="updateSchema"><details v-if="endpoint" class="wh-section"><summary>诊断与入口管理</summary><p>规则 v{{ endpoint.revision }} · 未完成 {{ endpoint.pendingEvents }} 条 · 活动等待 {{ endpoint.activeWaits }} 项</p><p v-if="endpoint.pauseReasons?.length">{{ endpoint.pauseReasons.map(statusLabel).join('；') }}</p><div class="wh-actions"><el-button :disabled="saving" @click="control(endpoint.modelPaused ? 'resume' : 'pause', {scope:'model'})">{{ endpoint.modelPaused ? '恢复自动模型…' : '暂停自动模型…' }}</el-button><el-button :disabled="saving" @click="control('stop', {scope:'current'})">停止当前轮次…</el-button><el-button type="danger" link :loading="actionBusy" @click="deleteOpen = true">删除入口…</el-button></div></details></WebhookAdvancedSettings></section>
        </main>
      </div>
      <footer class="wh-footer"><span role="status">{{ dirty ? '有未保存更改 · ' : '' }}规则仅对新事件生效</span><div><el-button :disabled="saving || actionBusy" @click="cancel">取消</el-button><el-button :loading="saving" :disabled="actionBusy" @click="save()">保存</el-button><el-button type="primary" :loading="saving" :disabled="actionBusy" @click="save(true)">保存并启用</el-button></div></footer>
    </template>
    <el-dialog v-model="rotateOpen" title="轮换入口凭据" width="min(480px, calc(100vw - 24px))" append-to-body class="mobile-viewport-dialog" :close-on-click-modal="false"><p>URL 不变；默认旧 Key 立即失效。宽限允许迁移生产者，但期间两把 Key 都能收件。</p><WebhookField v-model="graceSeconds" type="number" :min="0" :max="3600" :step="1" label="旧 Key 宽限" unit="秒" hint="0 = 立即失效；最多 3600 秒。" /><p v-if="preparing" role="status">正在核对本次影响…</p><p v-if="preparationError" role="alert">{{ preparationError }}</p><p v-if="preparation">影响已核对；新 Key 加密保存，可随时查看和复制，{{ graceSeconds ? `旧 Key 在 ${graceSeconds} 秒后失效` : '旧 Key 立即失效' }}。</p><template #footer><el-button :disabled="actionBusy" @click="rotateOpen = false">取消</el-button><el-button type="primary" :loading="actionBusy" :disabled="preparing || !preparation?.confirmationToken" @click="rotate">确认轮换</el-button></template></el-dialog>
    <el-dialog v-model="deleteOpen" title="删除入口" width="min(520px, calc(100vw - 24px))" append-to-body class="mobile-viewport-dialog" :close-on-click-modal="false" :close-on-press-escape="!actionBusy"><p>撤销凭据、拒绝新请求并取消未派发工作。保留历史、费用、统计和绑定对象；外部效果不会回滚。</p><el-checkbox v-model="stopRunning" :disabled="actionBusy">同时停止已经运行的脚本、模型及等待</el-checkbox><p v-if="preparing" role="status">正在核对选定范围…</p><p v-if="preparationError" role="alert">{{ preparationError }}</p><p v-if="preparation">未完成 {{ preparation.impact?.pendingEvents ?? '—' }} 条 · 活动等待 {{ preparation.impact?.activeWaits ?? '—' }} 项</p><template #footer><el-button :disabled="actionBusy" @click="deleteOpen = false">取消</el-button><el-button type="danger" :loading="actionBusy" :disabled="preparing || !preparation?.confirmationToken" @click="remove">确认删除此入口</el-button></template></el-dialog>

  </div>
</template>
