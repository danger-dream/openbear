<script setup>
defineProps({modelValue: Boolean, action: {type: String, default: '保存'}, impact: Object});
const emit = defineEmits(['choose']);
</script>
<template>
  <el-dialog :model-value="modelValue" class="mobile-viewport-dialog" width="min(600px, calc(100vw - 24px))" append-to-body :show-close="false" :close-on-click-modal="false" :close-on-press-escape="true" title="是否同时更新已有会话的系统提示词？" @update:model-value="value => { if (!value) emit('choose', null); }">
    <div class="prompt-impact-copy">
      <p>此次{{ action }}影响 <strong>{{ impact?.affectedCount || 0 }}</strong> 个已有会话：可更新 {{ impact?.updatableCount || 0 }} 个；运行中 {{ impact?.runningCount || 0 }} 个，本次将跳过。<span v-if="impact?.archivedCount">其中已归档 {{ impact.archivedCount }} 个。</span></p>
      <p>更新会按保存后的配置和各会话自己的当前参数重新组装系统提示词，工具定义同步为当前版本。这会使对应提示词／Provider continuation 缓存失效，可能增加后续输入开销和延迟。</p>
      <p>聊天历史、文件和记忆不会删除。Agent 模板与快照不受影响。运行中目标在提交锁内重查，跳过后不会自动延后更新。</p>
    </div>
    <template #footer><div class="prompt-impact-actions"><el-button @click="emit('choose', null)">取消</el-button><el-button @click="emit('choose', false)">仅{{ action }}，不更新</el-button><el-button type="primary" @click="emit('choose', true)">{{ action }}并更新可更新会话</el-button></div></template>
  </el-dialog>
</template>
<style scoped>
.prompt-impact-copy { color:var(--el-text-color-secondary); font-size:12px; line-height:1.65; }
.prompt-impact-actions { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:8px; }
.prompt-impact-actions .el-button { margin:0; }
</style>
