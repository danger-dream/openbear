<script setup>
import { computed } from 'vue';
import { ElMessageBox } from 'element-plus';
import { errorText } from './serverHelpers.js';

// An install that stopped because the machine already runs a node: connected here as another
// server (machine_has_node: never taken over, open that one instead), or a node this controller
// does not know (machine_has_other_node: replaced with force after asking).
const props = defineProps({install: {type: Object, default: null}, busy: Boolean});
const emit = defineEmits(['open', 'force']);
const code = computed(() => props.install?.state === 'failed' ? props.install.error : '');
const taken = computed(() => code.value === 'machine_has_node');
const other = computed(() => code.value === 'machine_has_other_node');
const existing = computed(() => props.install?.existing || {});
async function force() {
  const id = existing.value.nodeId ? `（${existing.value.nodeId}）` : '';
  try {
    await ElMessageBox.confirm(`这台机器上已经装有其他节点${id}，可能属于别的主控。覆盖安装会替换掉它，原来管理它的主控将失去这台机器。确定要覆盖吗？`, '仍然覆盖安装',
      {confirmButtonText: '覆盖安装', cancelButtonText: '取消', type: 'warning'});
  } catch { return; }
  emit('force');
}
</script>
<template>
  <div v-if="taken || other" class="ix-notice sv-conflict" data-tone="warn" role="alert">
    <span>{{ install.message || errorText(code) }}</span>
    <span class="sv-conflict-actions">
      <button v-if="taken && existing.nodeId" type="button" class="ix-text-button" @click="emit('open', existing.nodeId)">打开「{{ existing.name || '那台服务器' }}」</button>
      <button v-if="other" type="button" class="ix-text-button ob-danger" :disabled="busy" @click="force">仍然覆盖安装</button>
    </span>
  </div>
</template>
