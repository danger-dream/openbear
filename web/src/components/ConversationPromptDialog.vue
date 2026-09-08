<script setup>
import { onBeforeUnmount, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Api, apiError } from "../api";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  conversation: { type: Object, default: null },
});
const emit = defineEmits(["update:modelValue"]);
const preview = ref(null);
const loading = ref(false);
const saving = ref(false);
const errorText = ref("");
let generation = 0;

function promptError(error) {
  const code = error?.response?.data?.error;
  return {
    busy: "会话正在运行或处理其他操作，本次未更新，也不会延后自动更新。请在空闲后再试。",
    prompt_preview_changed: "模板、记忆或会话快照已变化。请重新预览，核对新的差异后再更新。",
    prompt_render_failed: "当前配置无法生成有效提示词，原快照已保留。请检查模板后再试。",
    session_not_found: "这个会话尚无可更新的会话状态。",
  }[code] || apiError(error);
}
function close() {
  if (saving.value) return;
  generation++;
  preview.value = null;
  emit("update:modelValue", false);
}
async function loadPreview() {
  const uuid = props.conversation?.conversationUuid;
  if (!props.modelValue || !uuid || props.conversation?.local || uuid.startsWith("local:")) return;
  const token = ++generation;
  preview.value = null;
  errorText.value = "";
  loading.value = true;
  try {
    const data = await Api.conversationSystemPromptPreview(uuid);
    if (token === generation && props.modelValue) preview.value = data;
  } catch (error) {
    if (token === generation && props.modelValue) errorText.value = promptError(error);
  } finally {
    if (token === generation) loading.value = false;
  }
}
async function save() {
  if (saving.value || loading.value || !preview.value?.changed) return;
  const reviewed = preview.value;
  const token = generation;
  saving.value = true;
  errorText.value = "";
  try {
    const result = await Api.updateConversationSystemPrompt(reviewed.conversationUuid, {
      confirmed: true, beforeHash: reviewed.beforeHash, afterHash: reviewed.afterHash,
    });
    if (token !== generation) return;
    ElMessage.success(result.updated ? "系统提示词已更新，下次发送生效；聊天历史保持不变。" : "系统提示词已是当前版本，无需重复更新。");
    saving.value = false;
    close();
  } catch (error) {
    if (token === generation) {
      preview.value = null;
      errorText.value = promptError(error);
    }
  } finally {
    if (token === generation) saving.value = false;
  }
}
watch(() => [props.modelValue, props.conversation?.conversationUuid], ([open]) => {
  generation++;
  preview.value = null;
  errorText.value = "";
  loading.value = false;
  saving.value = false;
  if (open) void loadPreview();
}, { immediate: true });
onBeforeUnmount(() => { generation++; });
</script>

<template>
  <el-dialog :model-value="modelValue" @update:model-value="value => { if (!value) close(); }"
    title="更新会话系统提示词" width="min(720px, calc(100vw - 24px))" append-to-body
    :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving"
    class="conversation-prompt-dialog">
    <div class="prompt-refresh-copy" v-loading="loading">
      <strong class="prompt-conversation-name">{{ conversation?.title || '所选会话' }}</strong>
      <p>只更新这个会话：使用当前主模板、记忆和目录属性重新组装完整系统提示词。提示词与 Provider continuation 缓存将失效，下一轮可能增加输入开销和延迟。</p>
      <p>聊天历史、摘要、任务记忆和文件不会删除，Agent 快照不受影响。运行中的会话不能更新，也不会自动延后更新。</p>
      <p v-if="loading" role="status">正在生成预览…</p>
      <p v-if="errorText" class="prompt-refresh-error" role="alert">{{ errorText }}</p>
      <template v-if="preview">
        <p v-if="!preview.changed" role="status">当前快照与重新组装的提示词完全一致，无需更新。</p>
        <template v-else>
          <p>字符数：{{ preview.beforeChars.toLocaleString() }} → {{ preview.afterChars.toLocaleString() }}</p>
          <details class="prompt-refresh-diff">
            <summary>查看提示词差异</summary>
            <pre>{{ preview.diff }}</pre>
          </details>
        </template>
      </template>
    </div>
    <template #footer>
      <el-button :disabled="saving" @click="close">取消</el-button>
      <el-button v-if="errorText" :disabled="saving" :loading="loading" @click="loadPreview">重新预览</el-button>
      <el-button type="primary" :loading="saving" :disabled="loading || !preview?.changed" @click="save">更新此会话</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.prompt-refresh-copy { min-height: 130px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.65; }
.prompt-conversation-name { display: block; color: var(--el-text-color-primary); overflow-wrap: anywhere; }
.prompt-refresh-copy p { margin: 10px 0; }
.prompt-refresh-error { color: var(--el-color-danger); }
.prompt-refresh-diff { margin-top: 12px; border: 1px solid var(--el-border-color-light); border-radius: 8px; background: var(--el-fill-color-lighter); }
.prompt-refresh-diff summary { padding: 8px 10px; cursor: pointer; color: var(--el-text-color-primary); }
.prompt-refresh-diff pre { max-height: 40vh; overflow: auto; margin: 0; padding: 10px; border-top: 1px solid var(--el-border-color-light); color: var(--el-text-color-primary); white-space: pre-wrap; overflow-wrap: anywhere; font: 11px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; }
</style>
