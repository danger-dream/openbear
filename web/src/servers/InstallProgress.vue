<script setup>
import { computed } from 'vue';
import { Check, Close, Loading } from '@element-plus/icons-vue';
import InstallConflict from './InstallConflict.vue';
import { INSTALL_STEPS, errorText } from './serverHelpers.js';

// Steps of an install over SSH, or of a manual command when `manual` is set
// (then only the controller-side milestones can be seen).
const props = defineProps({install: {type: Object, default: null}, manual: {type: Object, default: null}, busy: Boolean});
const emit = defineEmits(['open', 'force']);
const steps = computed(() => {
  if (props.manual) {
    const {registered, online} = props.manual;
    return [
      {key: 'run', label: '在服务器上运行命令', state: registered ? 'done' : 'running'},
      {key: 'enroll', label: '注册节点', state: registered ? 'done' : 'pending'},
      {key: 'online', label: '节点上线', state: online ? 'done' : registered ? 'running' : 'pending'},
    ];
  }
  return props.install?.steps || INSTALL_STEPS.map(([key, label]) => ({key, label, state: 'pending'}));
});
// The machine already runs a node: said once, under the steps, with what to do about it.
const conflict = computed(() => props.install?.state === 'failed' && ['machine_has_node', 'machine_has_other_node'].includes(props.install.error));
const detailOf = step => conflict.value && step.detail === props.install.message ? '' : step.detail;
</script>
<template>
  <div class="ip" role="status" aria-live="polite">
    <ol class="ip-steps">
      <li v-for="step in steps" :key="step.key" :data-state="step.state">
        <span class="ip-mark" aria-hidden="true">
          <el-icon v-if="step.state === 'done'"><Check /></el-icon>
          <el-icon v-else-if="step.state === 'failed'"><Close /></el-icon>
          <el-icon v-else-if="step.state === 'running'" class="is-loading"><Loading /></el-icon>
        </span>
        <span class="ip-label">{{ step.label }}</span>
        <small v-if="detailOf(step)" class="ip-detail mono">{{ detailOf(step) }}</small>
      </li>
    </ol>
    <InstallConflict v-if="conflict" :install="install" :busy="busy" @open="emit('open', $event)" @force="emit('force')" />
    <p v-else-if="install?.state === 'failed'" class="instance-error ip-error">{{ errorText(install.error) }}</p>
    <details v-if="install?.output" class="sv-output"><summary>安装输出</summary><pre>{{ install.output }}</pre></details>
  </div>
</template>
