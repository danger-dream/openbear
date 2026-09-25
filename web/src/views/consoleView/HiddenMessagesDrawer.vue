<script setup>
import {computed, onBeforeUnmount, onMounted, ref, watch} from 'vue';
import {Hide, View, RefreshLeft} from '@element-plus/icons-vue';
import {ElMessage, ElMessageBox} from 'element-plus';
import {Api, apiError} from '../../api.js';
import ConsoleMarkdown from './ConsoleMarkdown.vue';
import {useMessageVisibility} from './messageVisibility.js';
const props = defineProps({conversationUuid: {type: String, default: ''}});
const visibility = useMessageVisibility();
const {managing, items, busy} = visibility;
const phone = ref(false), previewId = ref(''), preview = ref(null), loadingId = ref('');
let media, request = 0;
const labels = {user_message: '用户消息', assistant_message: 'AI 回复', reasoning: '思考过程', tool: '工具过程', agent: 'Agent 卡片', user_interaction: '交互卡片', context_compaction: '上下文整理', model_retry: '重试记录', notice: '状态记录'};
const payload = computed(() => preview.value?.payload || {});
const previewText = computed(() => {
  const p = payload.value;
  if (['user_message', 'assistant_message', 'reasoning'].includes(preview.value?.opType)) return String(p.text || p.content || p.reasoning || '');
  return [p.text, p.content, p.arguments || p.args, p.resultText || p.result, p.preview].filter(Boolean).map(value => typeof value === 'string' ? value : JSON.stringify(value, null, 2)).join('\n\n') || JSON.stringify(p, null, 2);
});
const files = computed(() => Array.isArray(payload.value.attachments) ? payload.value.attachments : []);
const fileUrl = file => file.previewUrl || file.contentUrl || file.downloadUrl || '';
const time = value => value ? new Date(value).toLocaleString('zh-CN', {hour12: false}) : '';
function closePreview() { request++; previewId.value = ''; preview.value = null; loadingId.value = ''; }
function updateMedia() { phone.value = media.matches; }
onMounted(() => { media = window.matchMedia('(max-width: 760px)'); updateMedia(); media.addEventListener('change', updateMedia); });
onBeforeUnmount(() => { request++; media?.removeEventListener('change', updateMedia); });
watch(() => props.conversationUuid, closePreview);
watch(managing, value => { if (!value) closePreview(); });
watch(items, value => { if (previewId.value && !value.some(item => item.opId === previewId.value)) closePreview(); });
async function showPreview(id) {
  if (previewId.value === id) { closePreview(); return; }
  const seq = ++request, uuid = props.conversationUuid;
  previewId.value = id; preview.value = null; loadingId.value = id;
  try {
    const data = await Api.hiddenMessagePreview(uuid, id);
    if (seq === request && uuid === props.conversationUuid && managing.value) preview.value = data.operation;
  } catch (error) { if (seq === request) { closePreview(); ElMessage.error(apiError(error)); } }
  finally { if (seq === request) loadingId.value = ''; }
}
async function restoreAll() {
  const uuid = props.conversationUuid;
  try { await ElMessageBox.confirm('这些内容将重新出现在所有设备的聊天页面中。', '恢复全部隐藏内容？', {confirmButtonText: '全部恢复', cancelButtonText: '取消', type: 'info'}); }
  catch { return; }
  if (uuid === props.conversationUuid && managing.value) await visibility.restoreAll();
}
</script>

<template>
  <el-drawer v-model="managing" :direction="phone ? 'btt' : 'rtl'" :size="phone ? 'min(78dvh, 44rem)' : '420px'" title="隐藏内容" append-to-body destroy-on-close class="hidden-messages-drawer">
    <div class="hidden-drawer-layout">
      <div class="hidden-drawer-intro"><Hide/><p>只隐藏展示，不删除记录，也不改变 AI 上下文。<br/>查看原文仅在本机预览；恢复会同步到所有设备。</p></div>
      <div class="hidden-drawer-summary"><span>{{ items.length }} 条隐藏内容</span><button v-if="items.length" type="button" :disabled="busy" @click="restoreAll">全部恢复</button></div>
      <div class="hidden-message-list">
        <div v-if="!items.length" class="hidden-empty"><Hide/><strong>没有隐藏的内容</strong><p>在消息的 ··· 菜单中选择“隐藏这条内容”。</p></div>
        <article v-for="item in items" :key="item.opId" class="hidden-message-item">
          <header><strong>{{ labels[item.type] || '消息' }}</strong><time>{{ time(item.createdAtMs) }}</time></header>
          <div class="hidden-message-actions">
            <button type="button" :disabled="busy" :aria-expanded="previewId === item.opId" @click="showPreview(item.opId)"><View/>{{ previewId === item.opId ? '收起原文' : '查看原文' }}</button>
            <button type="button" :disabled="busy" @click="visibility.restore([item.opId])"><RefreshLeft/>恢复显示</button>
          </div>
          <div v-if="previewId === item.opId" class="hidden-message-preview">
            <span class="hidden-preview-label">本机临时预览 · 尚未恢复</span>
            <p v-if="loadingId === item.opId">正在读取…</p>
            <template v-else>
              <ConsoleMarkdown v-if="previewText" :text="previewText" :reference-bundle-id="payload.referenceBundleId || ''" :references="payload.references || []"/>
              <div v-for="(file, index) in files" :key="file.id || index" class="hidden-preview-file">
                <img v-if="String(file.mimeType || '').startsWith('image/') || file.kind === 'image'" :src="fileUrl(file)" :alt="file.fileName || file.name || '图片附件'"/>
                <a v-else :href="fileUrl(file) || undefined" target="_blank" rel="noopener noreferrer">{{ file.fileName || file.name || '附件' }}</a>
              </div>
            </template>
          </div>
        </article>
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.hidden-drawer-layout { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.hidden-drawer-intro { display: flex; align-items: flex-start; gap: 10px; padding: 12px; border-radius: 12px; background: var(--el-fill-color-light); color: var(--el-text-color-secondary); }
.hidden-drawer-intro svg { width: 17px; height: 17px; flex: none; margin-top: 2px; }
.hidden-drawer-intro p { margin: 0; font-size: 12px; line-height: 1.8; }
.hidden-drawer-summary { display: flex; justify-content: space-between; align-items: center; min-height: 52px; font-size: 12px; color: var(--el-text-color-secondary); }
.hidden-drawer-summary button { min-height: 44px; border: 0; background: transparent; color: var(--el-color-primary); cursor: pointer; }
.hidden-message-list { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding-bottom: 16px; }
.hidden-message-item { padding: 14px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.hidden-message-item header { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; align-items: center; }
.hidden-message-item strong { font-size: 13px; color: var(--el-text-color-primary); font-weight: 600; }
.hidden-message-item time { font-size: 11px; color: var(--el-text-color-secondary); font-variant-numeric: tabular-nums; }
.hidden-message-actions { display: flex; gap: 8px; margin-top: 8px; }
.hidden-message-actions button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 38px; padding: 0 10px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; background: transparent; color: var(--el-text-color-regular); font-size: 12px; cursor: pointer; }
.hidden-message-actions button:hover { background: var(--el-fill-color-light); }
.hidden-message-actions svg { width: 14px; height: 14px; }
.hidden-message-preview { max-height: 38dvh; overflow: auto; margin-top: 12px; padding: 12px; border: 1px dashed var(--el-border-color); border-radius: 10px; font-size: 13px; overflow-wrap: anywhere; }
.hidden-preview-label { display: block; margin-bottom: 10px; color: var(--el-text-color-secondary); font-size: 11px; }
.hidden-preview-file img { display: block; max-width: 100%; max-height: 200px; object-fit: contain; margin-top: 8px; border-radius: 8px; }
.hidden-empty { display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 54px 8px; text-align: center; color: var(--el-text-color-secondary); }
.hidden-empty svg { width: 30px; height: 30px; opacity: .5; }
.hidden-empty p { font-size: 12px; margin: 0; }
button:disabled { opacity: .5; cursor: default; }
button:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
@media (max-width: 760px) { .hidden-message-actions button { min-height: 44px; } }
</style>
<style>
.hidden-messages-drawer .el-drawer__header { margin: 0; padding: 16px; }
.hidden-messages-drawer .el-drawer__title { font-size: 15px; font-weight: 600; }
.hidden-messages-drawer .el-drawer__close-btn { width: 44px; height: 44px; }
.hidden-messages-drawer .el-drawer__body { min-height: 0; overflow: hidden; padding: 0 16px max(16px, env(safe-area-inset-bottom, 0px)); }
@media (max-width: 760px) { .hidden-messages-drawer { border-radius: 18px 18px 0 0; } }
</style>
